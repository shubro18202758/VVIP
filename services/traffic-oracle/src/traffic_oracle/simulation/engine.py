"""Simulation engine — seeded, forecast-coupled corridor queueing twin.

Real-life-grade mechanisms (each labelled, each falsifiable via replay):

* **Time-of-day / day-of-week demand**: continuous demand multiplier from
  :mod:`traffic_oracle.simulation.tod` (dual commuter peaks, weekend shape,
  Friday-night extension, monsoon/fog weather dampening).
* **Stochastic events**: Poisson-arrival breakdowns / waterlogging / signal
  faults / market spillover (schedule-forced rallies), all drawn from the
  seeded RNG — see :mod:`traffic_oracle.simulation.events`.
* **Forecast-coupled congestion (bidirectional)**:
    - consumes DSTGAT horizon shapes ``{segment: {5|10|15|30 min:
      {speed_kmh, congestion_idx}}}`` (exactly what
      ``FlowForecaster.predict`` returns, flow_forecaster.py:64-77) as demand
      multipliers interpolated between horizon knots;
    - produces its own horizon shapes at the same knots
      (``forecast_feedback``) in the identical format, so sim state feeds the
      DSTGAT loop (shadow labels / situation engine) rather than only
      consuming it.
* **Rolling closures**: each route segment closes ``advance_closure_sec``
  before convoy entry and reopens after the tail passes — full, partial, or
  signal-hold policy. Public queues build, discharge at saturation flow, and
  recovery is measured against a PAIRED no-closure counterfactual (same
  seed, same event schedule, same demand): the corridor has recovered once
  every simulated segment's queue is back within ε of the counterfactual's
  and its speed within 10 % of it.  A peak-hour corridor that is
  oversaturated with or without the closure no longer "recovers" at the
  horizon by construction — when the closure's excess queue never
  dissipates inside the horizon the run says ``censored: true`` and
  ``recovery_sec`` is the (lower-bound) horizon remainder.
* **Determinism**: one :class:`SeededRng` per replication, derived
  blake2b(seed, replication) — same seed in, byte-identical metrics out.

Queue model per segment per tick Δt:
    arrivals λ(t) [veh/h] = base_peak_demand × tod(t) × forecast_mult(t)
                            + Σ event extra demand
    capacity c(t) [veh/h] = sat_flow × lanes_open × green_ratio
                            × Π event capacity factors × closure factor
    outflow            = min(λ + queue/Δt, c)          (point queue)
    queue'             = queue + (λ − outflow)·Δt/3600
    speed              = queue regime cap | BPR(v/c) on free-flow
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from traffic_oracle.simulation.events import (
    EventConfig,
    EventSchedule,
    EventType,
    ScheduledEvent,
    sample_events,
)
from traffic_oracle.simulation.network import SimNetwork, default_network
from traffic_oracle.simulation.rng import SeededRng, derive_seed
from traffic_oracle.simulation.tod import (
    WeatherState,
    infer_weather_at,
    time_of_day_demand,
    weather_speed_factor,
)

# DSTGAT horizon knots (minutes) — flow_forecaster.HORIZONS.
FORECAST_HORIZONS = (5, 10, 15, 30)

# BPR-like urban speed curve parameters (labelled assumptions).
_BPR_A = 0.55
_BPR_B = 4.2
_QUEUE_SPEED_FRACTION = 0.25     # queued traffic crawls at 25% of adjusted ff
_QUEUE_SPEED_FLOOR_KMH = 4.0
_QUEUE_VEH_THRESHOLD = 0.5

_VEHICLE_SPACING_M = 7.0         # matches frozen scenario_sim.py:30
_TAIL_BUFFER_SEC = 30.0          # convoy tail clearance after last vehicle
_ESCORT_FF_FRACTION = 0.95       # convoy uses ~free-flow on its closed corridor

# Recovery vs the paired no-closure counterfactual (labelled tolerances):
# queue within max(0.5 veh, 5 % of the counterfactual queue) and speed at
# least 90 % of the counterfactual speed, on every simulated segment.
_RECOVERY_QUEUE_EPS_VEH = 0.5
_RECOVERY_QUEUE_REL = 0.05
_RECOVERY_SPEED_FRACTION = 0.90
RECOVERY_BASIS = "paired_no_closure"

VALID_CLOSURE_TYPES = ("full_closure", "partial_closure", "signal_hold")
_CLOSURE_CAPACITY_FACTOR = {
    "full_closure": 0.0,
    "partial_closure": 0.35,
    "signal_hold": 0.60,
}


class ForecastProvider(Protocol):
    """Anything with FlowForecaster.predict's signature (flow_forecaster.py:64)."""

    async def predict(
        self, segment_ids: list[int], horizons_min: list[int] | None = None
    ) -> dict[int, dict[int, dict[str, float]]]: ...


ForecastShapes = dict[int, dict[int, dict[str, float]]]


@dataclass
class ClosurePolicy:
    advance_closure_sec: int = 120
    closure_type: str = "full_closure"

    def __post_init__(self) -> None:
        if self.closure_type not in VALID_CLOSURE_TYPES:
            raise ValueError(
                f"closure_type must be one of {VALID_CLOSURE_TYPES}, "
                f"got {self.closure_type!r}"
            )
        if self.advance_closure_sec < 0:
            raise ValueError("advance_closure_sec must be >= 0")


@dataclass
class SimulationConfig:
    """Engine knobs — all labelled, all overridable per scenario."""

    dt_sec: float = 30.0
    convoy_speed_kmh: float = 60.0
    ambulance_rate_per_h_per_junction: float = 0.35
    queue_spacing_m: float = _VEHICLE_SPACING_M
    tail_buffer_sec: float = _TAIL_BUFFER_SEC
    # Capacity scale fitted by /api/v1/simulate/replay (1.0 until calibrated).
    capacity_scale: float = 1.0
    # Per-road-class scales from the same fit; a class without one uses
    # ``capacity_scale`` (the median of the fitted classes).
    class_capacity_scale: dict[str, float] = field(default_factory=dict)

    def scale_for(self, road_class: str) -> float:
        return float(self.class_capacity_scale.get(road_class, self.capacity_scale))


@dataclass
class ReplicationResult:
    """One Monte-Carlo replication — exactly the C5 run schema + audit extras."""

    replication: int
    total_disruption_vehicle_hours: float
    max_queue_length_m: float
    avg_closure_duration_sec: float
    convoy_transit_time_sec: float
    recovery_sec: float
    ambulance_delay_sec: float
    events: list[dict] = field(default_factory=list)
    n_events: int = 0
    n_ambulances_delayed: int = 0
    recovered: bool = False
    seed: int = 0
    forecast_feedback: dict[int, dict[int, dict[str, float]]] = field(
        default_factory=dict
    )
    # True when the closure's effect had NOT dissipated by the horizon:
    # recovery_sec is then a censored lower bound, never a measurement.
    censored: bool = False
    route_segments_modelled: int = 0
    route_segments_requested: int = 0

    def to_dict(self) -> dict:
        d = {
            "replication": self.replication,
            "total_disruption_vehicle_hours": round(
                self.total_disruption_vehicle_hours, 3
            ),
            "max_queue_length_m": round(self.max_queue_length_m, 1),
            "avg_closure_duration_sec": round(self.avg_closure_duration_sec, 1),
            "convoy_transit_time_sec": round(self.convoy_transit_time_sec, 1),
            "recovery_sec": round(self.recovery_sec, 1),
            "ambulance_delay_sec": round(self.ambulance_delay_sec, 1),
            # additive audit fields:
            "n_events": self.n_events,
            "n_ambulances_delayed": self.n_ambulances_delayed,
            "recovered": self.recovered,
            "censored": self.censored,
            "recovery_basis": RECOVERY_BASIS,
            "route_segments_modelled": self.route_segments_modelled,
            "route_segments_requested": self.route_segments_requested,
            "seed": self.seed,
            "events": self.events[:24],  # cap the audit trail per run
        }
        return d


@dataclass
class _SegState:
    seg_id: int
    queue_veh: float = 0.0
    on_route: bool = False
    # closure bookkeeping
    closure_start: float | None = None
    closure_end: float | None = None
    closures_recorded: list[float] = field(default_factory=list)
    # telemetry
    max_queue_veh: float = 0.0
    speed_now_kmh: float = 0.0


def _forecast_multiplier(shape: dict[int, dict[str, float]] | None, t_sec: float) -> float:
    """Demand multiplier from one segment's DSTGAT horizon knots.

    Maps forecast congestion_idx to a demand scale where idx 0.33 ≈ 1.0
    (neutral), clamped to [0.4, 2.2]. Linear between knots, held at the ends.
    """
    if not shape:
        return 1.0
    knots = sorted((h * 60.0, v.get("congestion_idx", 0.33)) for h, v in shape.items())
    if not knots:
        return 1.0

    def idx_at(t: float) -> float:
        if t <= knots[0][0]:
            return knots[0][1]
        if t >= knots[-1][0]:
            return knots[-1][1]
        for (t0, i0), (t1, i1) in zip(knots, knots[1:], strict=False):
            if t0 <= t <= t1:
                frac = (t - t0) / max(t1 - t0, 1e-9)
                return i0 + (i1 - i0) * frac
        return knots[-1][1]

    return max(0.4, min(2.2, 0.55 + 1.35 * idx_at(t_sec)))


@dataclass
class _ArmOutcome:
    """One simulated arm (closure policy applied, or the counterfactual)."""

    states: dict[int, _SegState]
    total_disruption_vh: float
    max_queue_veh: float
    forecast_feedback: dict[int, dict[int, dict[str, float]]]
    recovered_at: float | None
    queue_traj: list[dict[int, float]]
    speed_traj: list[dict[int, float]]


def _run_arm(
    *,
    network: SimNetwork,
    route_set: set[int],
    sim_ids: list[int],
    closure_windows: dict[int, tuple[float, float]],
    closure_policy: ClosurePolicy,
    schedule: EventSchedule,
    departure_time: datetime,
    horizon_sec: float,
    cfg: SimulationConfig,
    forecast_shapes: ForecastShapes,
    weather_ff: float,
    closures_enabled: bool,
    record: bool,
    counterfactual: _ArmOutcome | None,
    last_closure_end: float,
    route: list[int],
) -> _ArmOutcome:
    """The point-queue tick loop for one arm (see module docstring).

    ``closures_enabled=False`` is the paired no-closure counterfactual: the
    same events, demand and seed with every closure factor at 1.0.  With a
    ``counterfactual`` supplied, recovery is the first tick at/after the
    last closure end where every simulated segment is back within the
    recovery tolerances of the counterfactual at the same tick; the arm
    then stops accumulating (the disruption period is over).
    """
    dt = cfg.dt_sec
    states: dict[int, _SegState] = {}
    for sid in sim_ids:
        st = _SegState(seg_id=sid, on_route=sid in route_set)
        window = closure_windows.get(sid)
        if window is not None:
            st.closure_start, st.closure_end = window
        states[sid] = st

    n_steps = int(math.ceil(horizon_sec / dt))
    total_disruption_vh = 0.0
    max_queue_veh = 0.0
    forecast_feedback: dict[int, dict[int, dict[str, float]]] = {}
    ff_capture_steps = {
        int(round(h * 60.0 / dt)): h for h in FORECAST_HORIZONS
    }
    recovered_at: float | None = None
    queue_traj: list[dict[int, float]] = []
    speed_traj: list[dict[int, float]] = []

    for step in range(n_steps):
        t = step * dt
        now_dt = departure_time + timedelta(seconds=t)
        tod = time_of_day_demand(now_dt)

        active_by_seg: dict[int, list] = {}
        for ev in schedule.events:
            if ev.active(t):
                active_by_seg.setdefault(ev.segment_id, []).append(ev)

        # ── per-segment queueing ─────────────────────────────────────
        for sid in sim_ids:
            st = states[sid]
            seg = network.require(sid)

            # Event effects
            lanes_open = seg.lanes
            cap_factor = 1.0
            extra_demand = 0.0
            speed_cap: float | None = None
            for ev in active_by_seg.get(sid, ()):
                lanes_open = max(1, lanes_open - ev.lanes_blocked)
                cap_factor *= ev.capacity_factor
                # A signal fault cuts the approach's effective green; the
                # green ratio lives inside seg.capacity_veh_h, so it scales
                # capacity directly (it used to be computed and dropped).
                if seg.signalized:
                    cap_factor *= ev.green_ratio_factor
                extra_demand += ev.extra_demand_veh_h
                if ev.speed_cap_kmh is not None:
                    speed_cap = (
                        min(speed_cap, ev.speed_cap_kmh) if speed_cap else ev.speed_cap_kmh
                    )

            # Convoy closure effect (never in the counterfactual arm)
            closed = (
                closures_enabled
                and st.on_route
                and st.closure_start is not None
                and st.closure_start <= t
                and (st.closure_end is None or t < st.closure_end)
            )
            closure_factor = (
                _CLOSURE_CAPACITY_FACTOR[closure_policy.closure_type] if closed else 1.0
            )

            capacity = (
                seg.capacity_veh_h
                * (lanes_open / seg.lanes)
                * cap_factor
                * closure_factor
                * cfg.scale_for(seg.road_class)
            )
            capacity = max(capacity, 0.0)

            fmult = _forecast_multiplier(forecast_shapes.get(sid), t)
            lam = (
                seg.base_peak_demand_veh_h * tod * fmult + extra_demand
            )

            # Point-queue update
            inflow = lam * dt / 3600.0
            outflow_cap = capacity * dt / 3600.0
            demand_total = st.queue_veh + inflow
            outflow = min(demand_total, outflow_cap)
            st.queue_veh = max(0.0, demand_total - outflow)
            st.max_queue_veh = max(st.max_queue_veh, st.queue_veh)
            max_queue_veh = max(max_queue_veh, st.queue_veh)

            # Speed (BPR on free-flow; queue regime floor)
            ff_adj = seg.free_flow_kmh * weather_ff
            if st.queue_veh > _QUEUE_VEH_THRESHOLD:
                v = max(_QUEUE_SPEED_FLOOR_KMH, _QUEUE_SPEED_FRACTION * ff_adj)
            else:
                vc = (lam / capacity) if capacity > 1.0 else 4.0
                v = ff_adj / (1.0 + _BPR_A * (vc ** _BPR_B))
            if speed_cap is not None:
                v = min(v, speed_cap)
            if closed:
                v = min(v, max(_QUEUE_SPEED_FLOOR_KMH, 0.2 * ff_adj))
            st.speed_now_kmh = v

            # Delay accounting vs clear-day free-flow.
            queue_delay_veh_h = st.queue_veh * dt / 3600.0
            kmh_to_ms = 1000.0 / 3600.0
            traverse_ff_h = seg.length_m / (seg.free_flow_kmh * kmh_to_ms) / 3600.0
            traverse_h = (
                seg.length_m / (v * kmh_to_ms) / 3600.0 if v > 0.5 else traverse_ff_h * 3
            )
            moving_delay_veh_h = outflow * max(0.0, traverse_h - traverse_ff_h)
            total_disruption_vh += queue_delay_veh_h + moving_delay_veh_h

        if record:
            queue_traj.append({sid: states[sid].queue_veh for sid in sim_ids})
            speed_traj.append({sid: states[sid].speed_now_kmh for sid in sim_ids})

        # ── forecast feedback capture at DSTGAT knots ────────────────
        if step in ff_capture_steps:
            h = ff_capture_steps[step]
            for sid in route:
                st = states[sid]
                seg = network.require(sid)
                cong = max(0.0, min(1.0, 1.0 - st.speed_now_kmh / max(seg.free_flow_kmh, 1.0)))
                forecast_feedback.setdefault(sid, {})[h] = {
                    "speed_kmh": round(st.speed_now_kmh, 1),
                    "congestion_idx": round(cong, 3),
                }

        # ── recovery vs the paired counterfactual, after the last closure ──
        if (
            counterfactual is not None
            and recovered_at is None
            and t >= last_closure_end
            and step < len(counterfactual.queue_traj)
        ):
            cf_q = counterfactual.queue_traj[step]
            cf_v = counterfactual.speed_traj[step]
            if all(
                abs(states[s].queue_veh - cf_q[s])
                <= max(_RECOVERY_QUEUE_EPS_VEH, _RECOVERY_QUEUE_REL * cf_q[s])
                and states[s].speed_now_kmh >= _RECOVERY_SPEED_FRACTION * cf_v[s]
                for s in sim_ids
            ):
                recovered_at = t
                break

    return _ArmOutcome(
        states=states,
        total_disruption_vh=total_disruption_vh,
        max_queue_veh=max_queue_veh,
        forecast_feedback=forecast_feedback,
        recovered_at=recovered_at,
        queue_traj=queue_traj,
        speed_traj=speed_traj,
    )


def run_replication(
    *,
    replication: int,
    network: SimNetwork,
    route_segment_ids: list[int],
    departure_time: datetime,
    closure_policy: ClosurePolicy,
    horizon_sec: float,
    seed: int,
    config: SimulationConfig | None = None,
    forecast_shapes: ForecastShapes | None = None,
    weather: WeatherState | None = None,
    event_config: EventConfig | None = None,
    scheduled_events: list[ScheduledEvent] | None = None,
    adjacency: dict[int, set[int]] | None = None,
) -> ReplicationResult:
    """Run one seeded replication of the corridor twin.

    Two arms share one seeded event schedule: the no-closure
    counterfactual (recorded per tick) and the closure policy under
    evaluation, whose recovery is measured against it.
    """
    cfg = config or SimulationConfig()
    weather = weather or infer_weather_at(departure_time)
    event_config = event_config or EventConfig()
    forecast_shapes = forecast_shapes or {}

    rng = SeededRng(derive_seed(seed, "replication", replication))

    route = [sid for sid in route_segment_ids if network.get(sid) is not None]
    if not route:
        empty = ReplicationResult(
            replication=replication,
            total_disruption_vehicle_hours=0.0,
            max_queue_length_m=0.0,
            avg_closure_duration_sec=0.0,
            convoy_transit_time_sec=0.0,
            recovery_sec=0.0,
            ambulance_delay_sec=0.0,
            seed=seed,
            route_segments_modelled=0,
            route_segments_requested=len(route_segment_ids),
        )
        return empty

    # Simulated set: route + 1-hop shell (diverted/spilled traffic) + any
    # scheduled event segments. Shell queues count toward disruption.
    sim_ids: list[int] = list(route)
    seen = set(route)
    if adjacency:
        for sid in route:
            for nbr in adjacency.get(sid, ()):  # type: ignore[union-attr]
                if nbr in network.segments and nbr not in seen:
                    sim_ids.append(nbr)
                    seen.add(nbr)
    for ev in scheduled_events or []:
        if ev.segment_id in network.segments and ev.segment_id not in seen:
            sim_ids.append(ev.segment_id)
            seen.add(ev.segment_id)

    schedule: EventSchedule = sample_events(
        rng, network, sim_ids, horizon_sec, departure_time, event_config,
        weather, scheduled_events,
    )

    weather_ff = weather_speed_factor(weather)

    # ── convoy schedule (precomputed — deterministic on a closed corridor) ──
    # The convoy travels its own closed corridor at min(policy speed,
    # weather-adjusted free-flow), so entry/exit times per segment are known
    # upfront and each segment's closure activates advance_closure_sec BEFORE
    # entry — the queue-buildup window the rolling-closure policy is about.
    closure_windows: dict[int, tuple[float, float]] = {}
    convoy_exit_t_by_seg: dict[int, float] = {}
    entry_t = 0.0
    for sid in route:
        seg = network.require(sid)
        ff_convoy = seg.free_flow_kmh * weather_ff * _ESCORT_FF_FRACTION
        v_convoy = max(8.0, min(cfg.convoy_speed_kmh, ff_convoy))
        cross_sec = seg.length_m / (v_convoy * 1000.0 / 3600.0)
        exit_t = entry_t + cross_sec
        convoy_exit_t_by_seg[sid] = exit_t
        closure_windows[sid] = (
            max(0.0, entry_t - closure_policy.advance_closure_sec),
            exit_t + cfg.tail_buffer_sec,
        )
        entry_t = exit_t
    convoy_exit_t = convoy_exit_t_by_seg[route[-1]]
    closure_durations = [end - start for start, end in closure_windows.values()]
    last_closure_end = max(end for _start, end in closure_windows.values())

    arm_kwargs: dict[str, Any] = {
        "network": network,
        "route_set": set(route),
        "sim_ids": sim_ids,
        "closure_windows": closure_windows,
        "closure_policy": closure_policy,
        "schedule": schedule,
        "departure_time": departure_time,
        "horizon_sec": horizon_sec,
        "cfg": cfg,
        "forecast_shapes": forecast_shapes,
        "weather_ff": weather_ff,
        "last_closure_end": last_closure_end,
        "route": route,
    }
    counterfactual = _run_arm(
        closures_enabled=False, record=True, counterfactual=None, **arm_kwargs
    )
    main = _run_arm(
        closures_enabled=True, record=False, counterfactual=counterfactual,
        **arm_kwargs,
    )
    states = main.states
    recovered_at = main.recovered_at

    # Recovery relative to the counterfactual; censored when the closure's
    # effect never dissipated inside the horizon (a lower bound, labelled).
    censored = recovered_at is None
    if censored:
        recovery_sec = max(0.0, horizon_sec - last_closure_end)
    else:
        recovery_sec = max(0.0, recovered_at - last_closure_end)

    # ── ambulances (seeded Poisson crossings at signalized route junctions) ──
    ambulance_delay_sec = 0.0
    n_amb = 0
    for sid in route:
        seg = network.require(sid)
        if not seg.signalized:
            continue
        lam = (
            cfg.ambulance_rate_per_h_per_junction
            * event_config.ambulance_rate_multiplier
            * (horizon_sec / 3600.0)
        )
        n = SeededRng(derive_seed(seed, "replication", replication, "amb", sid)).poisson(lam)
        amb_rng = SeededRng(derive_seed(seed, "rep", replication, "amb_t", sid))
        st = states[sid]
        for _ in range(n):
            t_arr = amb_rng.uniform(0.0, horizon_sec)
            delay = 0.0
            if st.closure_start is not None and st.closure_start <= t_arr and (
                st.closure_end is None or t_arr < st.closure_end
            ):
                wait = (st.closure_end if st.closure_end is not None else horizon_sec) - t_arr
                # Queue discharge head after reopening, capped at 10 min.
                cap_veh_h = seg.capacity_veh_h * cfg.scale_for(seg.road_class)
                discharge = min(st.max_queue_veh / max(cap_veh_h, 1.0) * 3600.0, 600.0)
                delay = wait + 0.35 * discharge
            elif st.max_queue_veh > 2.0 and t_arr <= last_closure_end:
                delay = min(240.0, st.max_queue_veh / max(seg.capacity_veh_h, 1.0) * 3600.0 * 0.25)
            if delay > 0:
                ambulance_delay_sec += delay
                n_amb += 1

    # ── scheduled emergency vehicles (deterministic cause, 007 / C4) ────────
    # The deterministic counterpart of the random ambulance Poisson above:
    # a scenario-forced emergency vehicle on a route segment is charged
    # closure/queue delay with the same physics, seeded only by schedule.
    for sched in scheduled_events or []:
        if sched.event_type is not EventType.EMERGENCY_VEHICLE:
            continue
        if sched.segment_id not in states:
            continue
        st = states[sched.segment_id]
        seg = network.require(sched.segment_id)
        t_arr = sched.start_sec
        delay = 0.0
        if st.closure_start is not None and st.closure_start <= t_arr and (
            st.closure_end is None or t_arr < st.closure_end
        ):
            wait = (st.closure_end if st.closure_end is not None else horizon_sec) - t_arr
            cap_veh_h = seg.capacity_veh_h * cfg.scale_for(seg.road_class)
            discharge = min(st.max_queue_veh / max(cap_veh_h, 1.0) * 3600.0, 600.0)
            delay = wait + 0.35 * discharge
        elif st.max_queue_veh > 2.0 and t_arr <= last_closure_end:
            delay = min(
                240.0, st.max_queue_veh / max(seg.capacity_veh_h, 1.0) * 3600.0 * 0.25
            )
        if delay > 0:
            ambulance_delay_sec += delay
            n_amb += 1

    return ReplicationResult(
        replication=replication,
        total_disruption_vehicle_hours=main.total_disruption_vh,
        max_queue_length_m=main.max_queue_veh * cfg.queue_spacing_m,
        avg_closure_duration_sec=(
            sum(closure_durations) / len(closure_durations) if closure_durations else 0.0
        ),
        convoy_transit_time_sec=convoy_exit_t,
        recovery_sec=recovery_sec,
        ambulance_delay_sec=ambulance_delay_sec,
        events=[e.to_dict() for e in schedule.events],
        n_events=len(schedule.events),
        n_ambulances_delayed=n_amb,
        recovered=not censored,
        seed=seed,
        forecast_feedback=main.forecast_feedback,
        censored=censored,
        route_segments_modelled=len(route),
        route_segments_requested=len(route_segment_ids),
    )


class SimulationEngine:
    """Multi-replication twin runner over a corridor network."""

    def __init__(
        self,
        network: SimNetwork | None = None,
        config: SimulationConfig | None = None,
        adjacency: dict[int, set[int]] | None = None,
    ) -> None:
        self._network = network or default_network()
        self._config = config or SimulationConfig()
        self._adjacency = adjacency if adjacency is not None else self._network.adjacency

    @property
    def network(self) -> SimNetwork:
        return self._network

    def run(
        self,
        *,
        route_segment_ids: list[int],
        departure_time: datetime,
        closure_policy: ClosurePolicy | None = None,
        horizon_min: int = 45,
        n_replications: int = 5,
        seed: int = 20260101,
        weather: WeatherState | None = None,
        event_config: EventConfig | None = None,
        scheduled_events: list[ScheduledEvent] | None = None,
        forecast_shapes: ForecastShapes | None = None,
    ) -> list[ReplicationResult]:
        if horizon_min < 1 or horizon_min > 60:
            raise ValueError("horizon_min must be in [1, 60]")
        if n_replications < 1 or n_replications > 20:
            raise ValueError("n_replications must be in [1, 20]")

        policy = closure_policy or ClosurePolicy()
        horizon_sec = horizon_min * 60.0
        results: list[ReplicationResult] = []
        for rep in range(n_replications):
            results.append(
                run_replication(
                    replication=rep,
                    network=self._network,
                    route_segment_ids=route_segment_ids,
                    departure_time=departure_time,
                    closure_policy=policy,
                    horizon_sec=horizon_sec,
                    seed=seed,
                    config=self._config,
                    forecast_shapes=forecast_shapes,
                    weather=weather,
                    event_config=event_config,
                    scheduled_events=scheduled_events,
                    adjacency=self._adjacency,
                )
            )
        return results


def shapes_from_forecaster(
    forecast: dict[int, dict[int, dict[str, Any]]],
) -> ForecastShapes:
    """Normalize a FlowForecaster.predict() dict into engine forecast shapes.

    FlowForecaster returns ``{sid: {horizon_min: {speed_kmh, congestion_idx}}}``
    with float values (flow_forecaster.py:130-137) — pass through, coercing
    only the value types.
    """
    shapes: ForecastShapes = {}
    for sid, horizons in forecast.items():
        if not isinstance(horizons, dict):
            continue
        norm: dict[int, dict[str, float]] = {}
        for h, vals in horizons.items():
            if not isinstance(vals, dict) or vals.get("congestion_idx") is None:
                continue
            # Explicit None checks: a true 0.0 congestion (free flow) used to
            # read as 0.33 through ``x or 0.33``.
            try:
                cong = float(vals["congestion_idx"])
                speed_raw = vals.get("speed_kmh")
                speed = float(speed_raw) if speed_raw is not None else 0.0
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(cong) and math.isfinite(speed)):
                continue
            norm[int(h)] = {"speed_kmh": speed, "congestion_idx": cong}
        if norm:
            shapes[int(sid)] = norm
    return shapes
