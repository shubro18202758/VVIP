"""Scenario Simulator — evaluates "what-if" scenarios for convoy planning.

Answers questions like:
    - "What happens if we route via Sardar Patel Marg at 10:00 vs 10:30?"
    - "How much additional congestion does Route A cause vs Route B?"
    - "What if we reduce advance closure from 3 minutes to 90 seconds?"

Every scenario is delegated to the seeded corridor twin
(:class:`traffic_oracle.simulation.engine.SimulationEngine` — the same
engine behind ``POST /api/v1/simulate/twin``): rolling closures, time-of-day
demand, signalised capacity and forecast-coupled arrivals, over a network
built from ``corridor.road_segments`` with REAL geodesic lengths
(``ST_Length(geom::geography)``), DB signalisation and the DB signal-program
overrides.  The previous closed-form estimate treated a density (veh/km) as
an arrival rate (veh/h) on 500 m default segments and reported ≈0
vehicle-hours for every scenario; it is gone.

Input keys: ``scenario_id`` | ``route_id`` (convoy-brain's spelling),
``route_segment_ids`` | ``segment_ids``, ``departure_time`` (ISO-8601,
naive ⇒ IST), ``convoy_speed_kmh`` (60), ``advance_closure_sec`` (120),
``closure_type`` (full_closure), ``route_name``, ``horizon_min`` (45),
``n_replications`` (5), ``seed`` (20260101 — common random numbers across
the compared scenarios).  The ``/api/v1/evaluate/scenarios`` response keeps
its frozen fields (C13) and adds audit fields additively.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from traffic_oracle.optimize.corridor_router import CorridorRouter
from traffic_oracle.optimize.diversion_planner import (
    DiversionPlanner,
    coerce_departure_time,
)
from traffic_oracle.predict.eta_model import ETAPredictor
from traffic_oracle.predict.flow_forecaster import FlowForecaster
from traffic_oracle.runtime.memory_profiler import profile_memory
from traffic_oracle.simulation.calibration import (
    CALIBRATION,
    apply_signal_overrides,
    load_signal_overrides,
)
from traffic_oracle.simulation.engine import (
    VALID_CLOSURE_TYPES,
    ClosurePolicy,
    SimulationConfig,
    SimulationEngine,
    shapes_from_forecaster,
)
from traffic_oracle.simulation.network import SimNetwork, default_network, from_rows

logger = structlog.get_logger(__name__)

DEFAULT_HORIZON_MIN = 45
DEFAULT_N_REPLICATIONS = 5
DEFAULT_SEED = 20260101

# Network for the twin: real geodesic lengths + signalisation from the
# junction table (a segment is signalised when either endpoint lies within
# 25 m of a signal_control junction — the seed parser's rule).
_SQL_NETWORK_SEGMENTS = """
SELECT s.segment_id, s.road_name, s.road_class, s.lanes, s.speed_limit_kmh,
       s.oneway, ST_Length(s.geom::geography) AS length_m,
       EXISTS (
           SELECT 1 FROM corridor.junctions j
           WHERE j.signal_control
             AND (ST_DWithin(j.geom::geography, ST_StartPoint(s.geom)::geography, 25)
                  OR ST_DWithin(j.geom::geography, ST_EndPoint(s.geom)::geography, 25))
       ) AS signalized
FROM corridor.road_segments s
"""
_SQL_NETWORK_ADJACENCY = (
    "SELECT from_segment_id, to_segment_id FROM corridor.segment_adjacency"
)


class ScenarioInputError(ValueError):
    """A scenario the caller sent malformed (index + reason in the message)."""


@dataclass
class ScenarioResult:
    """Outcome metrics for a simulated convoy movement scenario."""

    scenario_id: str
    route_name: str
    total_disruption_vehicle_hours: float  # Total delay imposed on public traffic
    max_queue_length_m: float
    avg_closure_duration_sec: float
    segments_affected: int
    estimated_complaints_risk: str  # low | medium | high
    convoy_transit_time_sec: int
    # additive audit fields (twin provenance)
    evaluated: bool = True
    error: str | None = None
    engine: str = "twin"
    route_segment_ids: list[int] = field(default_factory=list)
    unknown_segment_ids: list[int] = field(default_factory=list)
    departure_time: str | None = None
    total_disruption_vehicle_hours_p90: float | None = None
    recovery_sec: float | None = None
    ambulance_delay_sec: float | None = None
    n_replications: int = 0
    seed: int | None = None
    forecast_coupling: str | None = None
    network_source: str | None = None
    calibrated: bool | None = None

    def audit_fields(self) -> dict:
        return {
            "evaluated": self.evaluated,
            "error": self.error,
            "engine": self.engine,
            "route_segment_ids": list(self.route_segment_ids),
            "unknown_segment_ids": list(self.unknown_segment_ids),
            "departure_time": self.departure_time,
            "total_disruption_vehicle_hours_p90": self.total_disruption_vehicle_hours_p90,
            "recovery_sec": self.recovery_sec,
            "ambulance_delay_sec": self.ambulance_delay_sec,
            "n_replications": self.n_replications,
            "seed": self.seed,
            "forecast_coupling": self.forecast_coupling,
            "network_source": self.network_source,
            "calibrated": self.calibrated,
        }


@dataclass
class _Scenario:
    """One validated, normalised scenario."""

    scenario_id: str
    route_name: str
    route: list[int]
    departure: datetime
    convoy_speed_kmh: float
    policy: ClosurePolicy
    horizon_min: int
    n_replications: int
    seed: int


_BIGINT_MAX = 2**63 - 1


def _int_list(value: object, index: int) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ScenarioInputError(
            f"scenarios[{index}]: route_segment_ids/segment_ids must be a non-empty list"
        )
    if len(value) > 64:
        raise ScenarioInputError(f"scenarios[{index}]: at most 64 route segments")
    out: list[int] = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, str)):
            raise ScenarioInputError(f"scenarios[{index}]: segment id {v!r:.40} is not an integer")
        try:
            sid = int(v)
        except ValueError as exc:  # incl. Python's int-digits limit
            raise ScenarioInputError(
                f"scenarios[{index}]: segment id {v!r:.40} is not an integer"
            ) from exc
        if not 0 < sid <= _BIGINT_MAX:  # segment ids are positive bigints
            raise ScenarioInputError(f"scenarios[{index}]: segment id {v!r:.40} is out of range")
        out.append(sid)
    return out


def _finite_number(raw: object) -> float | None:
    """A JSON number as a finite float; None for bools, non-numbers, NaN/inf
    and ints too large for a float (``math.isfinite`` raises OverflowError
    on those — a 300-digit seed used to answer 500 instead of 422)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = float(raw)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _bounded_int(raw: object, default: int, lo: int, hi: int, name: str, index: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw  # exact: no float round-trip for large in-range ints
    elif _finite_number(raw) is not None:
        value = int(raw)
    else:
        raise ScenarioInputError(f"scenarios[{index}]: {name} must be a number")
    if not lo <= value <= hi:
        raise ScenarioInputError(f"scenarios[{index}]: {name} must be in [{lo}, {hi}]")
    return value


def normalize_scenario(raw: object, index: int, now: datetime | None = None) -> _Scenario:
    """Validate one scenario dict, accepting both key spellings."""
    if not isinstance(raw, dict):
        raise ScenarioInputError(f"scenarios[{index}]: must be an object")
    sid = raw.get("scenario_id", raw.get("route_id"))
    scenario_id = str(sid) if sid not in (None, "") else f"scenario-{index + 1}"
    route_raw = raw.get("route_segment_ids", raw.get("segment_ids"))
    route = _int_list(route_raw, index)

    try:
        departure = coerce_departure_time(raw.get("departure_time"))
    except ValueError as exc:
        raise ScenarioInputError(
            f"scenarios[{index}]: departure_time must be an ISO-8601 timestamp"
        ) from exc
    if departure is None:
        departure = now or datetime.now(timezone.utc)

    speed = _finite_number(raw.get("convoy_speed_kmh", 60.0))
    if speed is None or not 5.0 <= speed <= 150.0:
        raise ScenarioInputError(f"scenarios[{index}]: convoy_speed_kmh must be in [5, 150]")

    closure_type = raw.get("closure_type", "full_closure")
    if closure_type not in VALID_CLOSURE_TYPES:
        raise ScenarioInputError(
            f"scenarios[{index}]: closure_type must be one of {VALID_CLOSURE_TYPES}"
        )
    advance = _bounded_int(
        raw.get("advance_closure_sec"), 120, 0, 1800, "advance_closure_sec", index
    )
    return _Scenario(
        scenario_id=scenario_id,
        route_name=str(raw.get("route_name") or scenario_id),
        route=route,
        departure=departure,
        convoy_speed_kmh=speed,
        policy=ClosurePolicy(advance_closure_sec=advance, closure_type=closure_type),
        horizon_min=_bounded_int(
            raw.get("horizon_min"), DEFAULT_HORIZON_MIN, 1, 60, "horizon_min", index
        ),
        n_replications=_bounded_int(
            raw.get("n_replications"), DEFAULT_N_REPLICATIONS, 1, 20, "n_replications", index
        ),
        seed=_bounded_int(raw.get("seed"), DEFAULT_SEED, 0, 2**63 - 1, "seed", index),
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _p90(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = 0.9 * (len(ordered) - 1)
    lo, hi = int(math.floor(rank)), int(math.ceil(rank))
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


class ScenarioSimulator:
    """Ranks convoy route/timing options on the seeded corridor twin.

    CPU-only (the twin is a pure-Python point-queue model, ~ms per
    replication).  Forecast coupling comes from the live forecaster when
    wired; the network from PostGIS when a pool is wired, else the seed.
    """

    def __init__(
        self,
        forecaster: FlowForecaster | None = None,
        router: CorridorRouter | None = None,
        diversion_planner: DiversionPlanner | None = None,
        eta_predictor: ETAPredictor | None = None,
        pool: object | None = None,
    ) -> None:
        self._forecaster = forecaster
        self._router = router
        self._diversion_planner = diversion_planner
        self._eta_predictor = eta_predictor
        self._pool = pool

    @profile_memory
    async def evaluate(
        self,
        scenarios: list[dict],
    ) -> list[ScenarioResult]:
        """Evaluate multiple convoy scenarios and rank by minimal disruption.

        Args:
            scenarios: List of scenario configs (see module docstring for the
                accepted keys and defaults).

        Returns:
            Ranked scenario results, best (least disruptive) first; scenarios
            that could not be simulated (no segment known to the network)
            are labelled ``evaluated=False`` and ranked last.

        Raises:
            ScenarioInputError: a scenario is malformed (nothing is run).
        """
        now = datetime.now(timezone.utc)
        normalized = [normalize_scenario(s, i, now) for i, s in enumerate(scenarios)]
        if not normalized:
            return []

        network, network_source, signal_programs = await self._load_network()
        results = [
            await self._evaluate_single(sc, network, network_source)
            for sc in normalized
        ]

        # Rank by least disruption; unevaluable scenarios last.
        results.sort(key=lambda r: (not r.evaluated, r.total_disruption_vehicle_hours))

        best = results[0] if results else None
        logger.info(
            "scenario_simulator.evaluate",
            count=len(results),
            best_id=best.scenario_id if best else None,
            best_disruption_vh=round(best.total_disruption_vehicle_hours, 2) if best else None,
            network=network_source,
            signal_programs_applied=signal_programs,
            unevaluated=sum(1 for r in results if not r.evaluated),
        )
        return results

    async def _load_network(self) -> tuple[SimNetwork, str, int]:
        """PostGIS-backed twin network (real lengths), seed SQL as fallback."""
        network: SimNetwork | None = None
        source = "seed_sql"
        if self._pool is not None:
            try:
                async with self._pool.acquire() as conn:
                    seg_rows = await conn.fetch(_SQL_NETWORK_SEGMENTS)
                    adj_rows = await conn.fetch(_SQL_NETWORK_ADJACENCY)
                if seg_rows:
                    network = from_rows([dict(r) for r in seg_rows])
                    valid = set(network.segments)
                    for r in adj_rows:
                        a, b = int(r["from_segment_id"]), int(r["to_segment_id"])
                        if a in valid and b in valid:
                            network.adjacency.setdefault(a, set()).add(b)
                            network.adjacency.setdefault(b, set()).add(a)
                    source = "corridor.road_segments"
            except Exception as exc:  # noqa: BLE001 — labelled fallback
                logger.warning(
                    "scenario_simulator.network_db_unavailable",
                    error=f"{type(exc).__name__}: {exc}",
                )
                network = None
        if network is None:
            network = default_network()
        applied = 0
        if self._pool is not None:
            applied = apply_signal_overrides(network, await load_signal_overrides(self._pool))
        return network, source, applied

    async def _forecast_shapes(self, route: list[int]) -> tuple[dict, str]:
        if self._forecaster is None:
            return {}, "internal"
        try:
            forecast = await self._forecaster.predict(list(route), [5, 10, 15, 30])
        except Exception as exc:  # noqa: BLE001 — twin runs on its own demand model
            logger.warning("scenario_simulator.forecast_unavailable", error=str(exc))
            return {}, "internal"
        shapes = shapes_from_forecaster(forecast)
        if not shapes:
            return {}, "internal"
        return shapes, getattr(self._forecaster, "last_forecast_rung", None) or "dstgat"

    async def _evaluate_single(
        self, sc: _Scenario, network: SimNetwork, network_source: str
    ) -> ScenarioResult:
        """Run one scenario on the twin and fold its replications."""
        known = [sid for sid in sc.route if network.get(sid) is not None]
        unknown = [sid for sid in sc.route if network.get(sid) is None]
        base = dict(
            scenario_id=sc.scenario_id,
            route_name=sc.route_name,
            route_segment_ids=list(sc.route),
            unknown_segment_ids=unknown,
            departure_time=sc.departure.isoformat(),
            seed=sc.seed,
            network_source=network_source,
            calibrated=CALIBRATION.calibrated,
        )
        if not known:
            return ScenarioResult(
                total_disruption_vehicle_hours=0.0,
                max_queue_length_m=0.0,
                avg_closure_duration_sec=0.0,
                segments_affected=0,
                estimated_complaints_risk="low",
                convoy_transit_time_sec=0,
                evaluated=False,
                error="no_known_segments",
                **base,
            )

        shapes, coupling = await self._forecast_shapes(known)
        engine = SimulationEngine(
            network=network,
            config=SimulationConfig(
                capacity_scale=CALIBRATION.capacity_scale,
                convoy_speed_kmh=sc.convoy_speed_kmh,
            ),
        )
        runs = engine.run(
            route_segment_ids=known,
            departure_time=sc.departure,
            closure_policy=sc.policy,
            horizon_min=sc.horizon_min,
            n_replications=sc.n_replications,
            seed=sc.seed,
            forecast_shapes=shapes,
        )
        disruption = [r.total_disruption_vehicle_hours for r in runs]
        max_queue_m = _mean([r.max_queue_length_m for r in runs])
        total_vh = _mean(disruption)
        return ScenarioResult(
            total_disruption_vehicle_hours=round(total_vh, 3),
            max_queue_length_m=round(max_queue_m, 1),
            avg_closure_duration_sec=round(_mean([r.avg_closure_duration_sec for r in runs]), 1),
            segments_affected=len(known),
            estimated_complaints_risk=self._assess_risk(max_queue_m, total_vh),
            convoy_transit_time_sec=int(round(_mean([r.convoy_transit_time_sec for r in runs]))),
            total_disruption_vehicle_hours_p90=round(_p90(disruption), 3),
            recovery_sec=round(_mean([r.recovery_sec for r in runs]), 1),
            ambulance_delay_sec=round(_mean([r.ambulance_delay_sec for r in runs]), 1),
            n_replications=len(runs),
            forecast_coupling=coupling,
            **base,
        )

    @staticmethod
    def _assess_risk(max_queue_m: float, disruption_veh_hours: float) -> str:
        """Classify complaints risk based on queue length and disruption."""
        if max_queue_m > 1000 or disruption_veh_hours > 50:
            return "high"
        if max_queue_m > 300 or disruption_veh_hours > 10:
            return "medium"
        return "low"
