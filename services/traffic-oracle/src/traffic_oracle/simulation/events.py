"""Stochastic road events for the twin: breakdowns, waterlogging, rally /
market spillover, signal faults.

All sampling goes through :class:`~traffic_oracle.simulation.rng.SeededRng`,
so an event schedule is a pure function of (seed, network, config, clock).
Events are drawn as Poisson arrivals per (segment, type) with rates modulated
by time-of-day, day-of-week, weather, and scenario overrides, then given
duration/severity draws from labelled distributions.

Effect model (how an active event degrades a segment):
    - capacity_factor:    multiplies saturation capacity
    - lanes_blocked:      integer lane loss (applied before capacity)
    - speed_cap_kmh:      hard speed ceiling while active (waterlogging)
    - extra_demand_veh_h: added arrivals (rally / market spillover)
    - green_ratio_factor: multiplies signal green time (signal fault)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from traffic_oracle.simulation.network import SegmentSpec, SimNetwork
from traffic_oracle.simulation.rng import SeededRng
from traffic_oracle.simulation.tod import (
    hour_demand_multiplier,
    to_ist,
    waterlogging_rate_multiplier,
)


class EventType(str, Enum):
    BREAKDOWN = "vehicle_breakdown"
    WATERLOGGING = "waterlogging"
    RALLY = "rally_spillover"
    MARKET = "market_spillover"
    SIGNAL_FAULT = "signal_fault"
    # Additive (007 / contract C4): deterministic counterpart of the
    # replication-random ambulance Poisson in engine.py — an emergency
    # vehicle crossing the corridor on a known schedule.  Carries no
    # capacity effect; the engine charges it closure/queue delay.
    EMERGENCY_VEHICLE = "emergency_vehicle"


class EventSource(str, Enum):
    STOCHASTIC = "stochastic"
    SCHEDULED = "scheduled"   # scenario-forced (rally, planted breakdown, ...)


# Base event rates per segment-hour, by road class (labelled assumptions).
# Breakdowns scale with exposure (flow × class); faults are rare; rallies are
# never spontaneous (schedule-only); market spillover only near bazaar segments.
_BREAKDOWN_RATE_PER_H: dict[str, float] = {
    "motorway": 0.075,
    "trunk": 0.060,
    "primary": 0.045,
    "secondary": 0.030,
    "residential": 0.015,
    "service": 0.010,
}
_SIGNAL_FAULT_RATE_PER_H = 0.006          # per signalized segment-hour
_WATERLOGGING_BASE_RATE_PER_H = 0.030     # per low-lying segment-hour, monsoon-scaled
_MARKET_BASE_RATE_PER_H = 0.050           # per market-adjacent segment-hour (trading hours)
_RALLY_BASE_RATE_PER_H = 0.0              # schedule-only

# Duration distributions (minutes): (mu=ln(median), sigma, lo_min, hi_min).
_DUR_BREAKDOWN = (math.log(22.0), 0.60, 5.0, 120.0)
_DUR_WATERLOGGING = (math.log(75.0), 0.75, 15.0, 480.0)
_DUR_SIGNAL_FAULT = (math.log(18.0), 0.70, 4.0, 90.0)
_DUR_MARKET = (math.log(150.0), 0.35, 60.0, 360.0)


@dataclass(frozen=True)
class RoadEvent:
    """One concrete event instance on one segment."""

    event_type: EventType
    source: EventSource
    segment_id: int
    start_sec: float          # seconds after simulation t=0
    duration_sec: float
    severity: float           # 0..1
    lanes_blocked: int
    capacity_factor: float
    speed_cap_kmh: float | None
    extra_demand_veh_h: float
    green_ratio_factor: float
    label: str = ""

    @property
    def end_sec(self) -> float:
        return self.start_sec + self.duration_sec

    def active(self, t_sec: float) -> bool:
        return self.start_sec <= t_sec < self.end_sec

    def to_dict(self) -> dict:
        return {
            "type": self.event_type.value,
            "source": self.source.value,
            "segment_id": self.segment_id,
            "start_sec": round(self.start_sec, 1),
            "duration_sec": round(self.duration_sec, 1),
            "severity": round(self.severity, 3),
            "lanes_blocked": self.lanes_blocked,
            "label": self.label,
        }


@dataclass
class EventConfig:
    """Rates + behaviour knobs; every multiplier is scenario-controllable."""

    breakdown_rate_multiplier: float = 1.0
    waterlogging_rate_multiplier: float = 1.0
    signal_fault_rate_multiplier: float = 1.0
    market_rate_multiplier: float = 1.0
    rally_rate_multiplier: float = 0.0          # rallies are schedule-only
    # Sixth override (additive, 007 / contract C4): scales the replication-
    # random ambulance Poisson rate in engine.py.  1.0 = today's shipping
    # rate (unchanged for every pre-existing scenario).
    ambulance_rate_multiplier: float = 1.0
    disable_all: bool = False

    def rate_multiplier(self, etype: EventType) -> float:
        return {
            EventType.BREAKDOWN: self.breakdown_rate_multiplier,
            EventType.WATERLOGGING: self.waterlogging_rate_multiplier,
            EventType.RALLY: self.rally_rate_multiplier,
            EventType.MARKET: self.market_rate_multiplier,
            EventType.SIGNAL_FAULT: self.signal_fault_rate_multiplier,
            EventType.EMERGENCY_VEHICLE: 1.0,   # schedule-only (see below)
        }[etype]


@dataclass
class ScheduledEvent:
    """A scenario-forced event (deterministic schedule, seeded severity)."""

    event_type: EventType
    segment_id: int
    start_sec: float
    duration_sec: float
    severity: float = 0.7
    label: str = ""
    extra_demand_veh_h: float = 0.0

    def resolve(self, rng: SeededRng, seg: SegmentSpec) -> RoadEvent:
        """Turn the schedule into a full RoadEvent with seeded jitter."""
        sev = min(max(self.severity, 0.05), 1.0)
        if self.event_type is EventType.EMERGENCY_VEHICLE:
            # No capacity effect — an emergency vehicle crossing on a
            # known schedule; the engine charges it closure/queue delay
            # exactly like the random ambulance Poisson draws.
            return RoadEvent(
                event_type=self.event_type, source=EventSource.SCHEDULED,
                segment_id=self.segment_id, start_sec=self.start_sec,
                duration_sec=self.duration_sec, severity=sev,
                lanes_blocked=0, capacity_factor=1.0,
                speed_cap_kmh=None, extra_demand_veh_h=0.0,
                green_ratio_factor=1.0,
                label=self.label or "scheduled emergency vehicle crossing",
            )
        if self.event_type is EventType.BREAKDOWN:
            lanes = 2 if sev > 0.8 else 1
            return RoadEvent(
                event_type=self.event_type, source=EventSource.SCHEDULED,
                segment_id=self.segment_id, start_sec=self.start_sec,
                duration_sec=self.duration_sec, severity=sev,
                lanes_blocked=min(lanes, max(seg.lanes - 1, 1)),
                capacity_factor=1.0 - 0.55 * sev,
                speed_cap_kmh=None, extra_demand_veh_h=0.0,
                green_ratio_factor=1.0, label=self.label or "scheduled breakdown",
            )
        if self.event_type is EventType.WATERLOGGING:
            return RoadEvent(
                event_type=self.event_type, source=EventSource.SCHEDULED,
                segment_id=self.segment_id, start_sec=self.start_sec,
                duration_sec=self.duration_sec, severity=sev,
                lanes_blocked=0,
                capacity_factor=1.0 - 0.6 * sev,
                speed_cap_kmh=6.0 + 16.0 * (1.0 - sev),
                extra_demand_veh_h=0.0,
                green_ratio_factor=1.0, label=self.label or "scheduled waterlogging",
            )
        if self.event_type is EventType.SIGNAL_FAULT:
            return RoadEvent(
                event_type=self.event_type, source=EventSource.SCHEDULED,
                segment_id=self.segment_id, start_sec=self.start_sec,
                duration_sec=self.duration_sec, severity=sev,
                lanes_blocked=0, capacity_factor=1.0,
                speed_cap_kmh=None, extra_demand_veh_h=0.0,
                green_ratio_factor=max(0.15, 1.0 - 0.85 * sev),
                label=self.label or "scheduled signal fault",
            )
        # Rally / market spillover
        demand = self.extra_demand_veh_h if self.extra_demand_veh_h else 400.0 + 700.0 * sev
        return RoadEvent(
            event_type=self.event_type, source=EventSource.SCHEDULED,
            segment_id=self.segment_id, start_sec=self.start_sec,
            duration_sec=self.duration_sec, severity=sev,
            lanes_blocked=0, capacity_factor=1.0,
            speed_cap_kmh=None, extra_demand_veh_h=demand,
            green_ratio_factor=1.0,
            label=self.label or f"scheduled {self.event_type.value}",
        )


@dataclass
class EventSchedule:
    """Sorted, immutable-in-spirit event list for one replication."""

    events: list[RoadEvent] = field(default_factory=list)

    def active_at(self, t_sec: float) -> list[RoadEvent]:
        return [e for e in self.events if e.active(t_sec)]

    def by_segment(self) -> dict[int, list[RoadEvent]]:
        out: dict[int, list[RoadEvent]] = {}
        for e in self.events:
            out.setdefault(e.segment_id, []).append(e)
        return out

    def __len__(self) -> int:
        return len(self.events)


def sample_events(
    rng: SeededRng,
    network: SimNetwork,
    segment_ids: list[int],
    window_sec: float,
    start_dt: datetime,
    config: EventConfig,
    weather,
    scheduled: list[ScheduledEvent] | None = None,
) -> EventSchedule:
    """Draw the event schedule for one replication.

    Poisson arrivals per (segment, event-type): rate = base(class) ×
    hour multiplier × config multiplier × weather factor, recomputed each hour
    of the window (thinning over hour blocks keeps the TOD realism exact
    instead of averaging it away).
    """
    events: list[RoadEvent] = []
    if config.disable_all and not scheduled:
        return EventSchedule(events=[])

    hour_block = 3600.0
    n_blocks = max(1, int(window_sec // hour_block) + (1 if window_sec % hour_block else 0))
    water_mult = waterlogging_rate_multiplier(weather) * config.rate_multiplier(
        EventType.WATERLOGGING
    )

    for sid in segment_ids:
        seg = network.get(sid)
        if seg is None:
            continue
        for block in range(n_blocks):
            t0 = block * hour_block
            blk_dt = to_ist(start_dt + timedelta(seconds=t0))
            hour, dow = blk_dt.hour, blk_dt.weekday()
            tod_mult = max(hour_demand_multiplier(hour, dow), 0.05)

            # ── breakdowns ────────────────────────────────────────────
            rate = (
                _BREAKDOWN_RATE_PER_H.get(seg.road_class, 0.02)
                * config.rate_multiplier(EventType.BREAKDOWN)
                * (0.5 + 0.5 * tod_mult)      # exposure scales with demand
            )
            _draw_poisson_events(
                rng, events, EventType.BREAKDOWN, EventSource.STOCHASTIC,
                seg, t0, hour_block, rate, _DUR_BREAKDOWN, weather,
            )

            # ── waterlogging (monsoon-gated, low-lying weighted) ──────
            if seg.is_low_lying:
                rate = _WATERLOGGING_BASE_RATE_PER_H * water_mult
            else:
                rate = _WATERLOGGING_BASE_RATE_PER_H * water_mult * 0.05
            _draw_poisson_events(
                rng, events, EventType.WATERLOGGING, EventSource.STOCHASTIC,
                seg, t0, hour_block, rate, _DUR_WATERLOGGING, weather,
            )

            # ── signal faults (signalized segments only) ──────────────
            if seg.signalized:
                rate = (
                    _SIGNAL_FAULT_RATE_PER_H
                    * config.rate_multiplier(EventType.SIGNAL_FAULT)
                    * (0.6 + 0.4 * tod_mult)
                )
                _draw_poisson_events(
                    rng, events, EventType.SIGNAL_FAULT, EventSource.STOCHASTIC,
                    seg, t0, hour_block, rate, _DUR_SIGNAL_FAULT, weather,
                )

            # ── market spillover (bazaar segments, trading hours) ─────
            if seg.is_market_adjacent and 10 <= hour <= 21:
                rate = (
                    _MARKET_BASE_RATE_PER_H
                    * config.rate_multiplier(EventType.MARKET)
                    * (1.0 if dow >= 5 or dow == 4 else 0.7)  # weekend/Fri heavier
                )
                _draw_poisson_events(
                    rng, events, EventType.MARKET, EventSource.STOCHASTIC,
                    seg, t0, hour_block, rate, _DUR_MARKET, weather,
                )

            # ── rally: spontaneous rate is zero (schedule-only) ───────
            # Scheduled rallies are resolved below.

    for sched in scheduled or []:
        seg = network.get(sched.segment_id)
        if seg is None:
            continue
        events.append(sched.resolve(rng, seg))

    events.sort(key=lambda e: (e.start_sec, e.segment_id, e.event_type.value))
    return EventSchedule(events=events)


def _draw_poisson_events(
    rng: SeededRng,
    out: list[RoadEvent],
    etype: EventType,
    source: EventSource,
    seg: SegmentSpec,
    block_start_sec: float,
    block_sec: float,
    rate_per_h: float,
    dur_params: tuple[float, float, float, float],
    weather,
) -> None:
    """Draw Poisson(λ·Δh) events inside one hour block."""
    lam = rate_per_h * (block_sec / 3600.0)
    if lam <= 0:
        return
    n = rng.poisson(lam)
    mu, sigma, dmin, dmax = dur_params
    for _ in range(n):
        start = block_start_sec + rng.uniform(0.0, block_sec)
        dur_min = min(max(rng.lognormal(mu, sigma), dmin), dmax)
        sev = min(max(rng.uniform(0.15, 1.0), 0.05), 1.0)
        out.append(_build_event(etype, source, seg, start, dur_min * 60.0, sev))


def _build_event(
    etype: EventType,
    source: EventSource,
    seg: SegmentSpec,
    start_sec: float,
    duration_sec: float,
    severity: float,
) -> RoadEvent:
    """Materialize an event's traffic effect from its type + severity."""
    if etype is EventType.BREAKDOWN:
        lanes = 2 if severity > 0.8 else 1
        return RoadEvent(
            event_type=etype, source=source, segment_id=seg.segment_id,
            start_sec=start_sec, duration_sec=duration_sec, severity=severity,
            lanes_blocked=min(lanes, max(seg.lanes - 1, 1)),
            capacity_factor=1.0 - 0.55 * severity,
            speed_cap_kmh=None, extra_demand_veh_h=0.0, green_ratio_factor=1.0,
            label="stochastic breakdown",
        )
    if etype is EventType.WATERLOGGING:
        return RoadEvent(
            event_type=etype, source=source, segment_id=seg.segment_id,
            start_sec=start_sec, duration_sec=duration_sec, severity=severity,
            lanes_blocked=0,
            capacity_factor=1.0 - 0.6 * severity,
            speed_cap_kmh=6.0 + 16.0 * (1.0 - severity),
            extra_demand_veh_h=0.0, green_ratio_factor=1.0,
            label="stochastic waterlogging",
        )
    if etype is EventType.SIGNAL_FAULT:
        return RoadEvent(
            event_type=etype, source=source, segment_id=seg.segment_id,
            start_sec=start_sec, duration_sec=duration_sec, severity=severity,
            lanes_blocked=0, capacity_factor=1.0, speed_cap_kmh=None,
            extra_demand_veh_h=0.0,
            green_ratio_factor=max(0.15, 1.0 - 0.85 * severity),
            label="stochastic signal fault",
        )
    if etype is EventType.MARKET:
        return RoadEvent(
            event_type=etype, source=source, segment_id=seg.segment_id,
            start_sec=start_sec, duration_sec=duration_sec, severity=severity,
            lanes_blocked=0, capacity_factor=1.0, speed_cap_kmh=None,
            extra_demand_veh_h=(150.0 + 550.0 * severity),
            green_ratio_factor=1.0, label="stochastic market spillover",
        )
    # Rally spillover
    return RoadEvent(
        event_type=etype, source=source, segment_id=seg.segment_id,
        start_sec=start_sec, duration_sec=duration_sec, severity=severity,
        lanes_blocked=0, capacity_factor=1.0, speed_cap_kmh=None,
        extra_demand_veh_h=(300.0 + 900.0 * severity),
        green_ratio_factor=1.0, label="stochastic rally spillover",
    )
