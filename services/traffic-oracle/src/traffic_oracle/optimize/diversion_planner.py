"""Diversion Planner — generates pre-computed diversion strategies for
road segments affected by convoy movement.

For each segment along a planned convoy route, computes:
    - Alternative routes for diverted public traffic
    - Optimal activation/deactivation timing (minimize closure window)
    - Estimated queue formation and dissipation times

A planner without its heuristic (or whose corridor graph cannot be loaded)
raises :class:`DiversionUnavailableError` with a labelled reason — the
API answers 503 with that reason instead of a silent empty plan that reads
as "no diversions needed".

A closure whose only ways round are longer than the practicality limit
(max(5 km, 6 × its length) — constructive_heuristic.detour_limit_m) is
planned with ``detour_status='impractical'``: the closure window and queue
estimate stand, no alternative segments are offered, and
``detour_reason`` says to hold or meter traffic instead.  ``entry_pinned``
/ ``exit_pinned`` say whether each end of the detour was fixed by the
route (else the detour may cover only part of the closed segment).

Rolling closures: a detour never runs over a route segment closed at the
same time (overlapping closure windows); one that leans on a route segment
open during this closure lists it in ``uses_route_segment_ids`` (C-R).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import structlog

from traffic_oracle.optimize.constructive_heuristic import (
    DEFAULT_SEGMENT_LENGTH_M,
    DETOUR_IMPRACTICAL,
    DiversionHeuristic,
)
from traffic_oracle.predict.flow_forecaster import FlowForecaster
from traffic_oracle.runtime.memory_profiler import profile_memory

logger = structlog.get_logger(__name__)

#: Naive departure times are Ahmedabad local time (same rule as the twin's
#: simulation/router.py _coerce_h).
IST = timezone(timedelta(hours=5, minutes=30))


def coerce_departure_time(value: str | datetime | None) -> datetime | None:
    """ISO-8601 string/datetime → aware datetime (naive ⇒ IST).

    Raises ValueError for an unparseable string.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise ValueError(f"departure_time must be an ISO-8601 string, got {type(value).__name__}")
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=IST)


class DiversionUnavailableError(RuntimeError):
    """Diversion planning cannot run; ``reason`` is a stable label."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class DiversionPlan:
    """Diversion strategy for a single road segment during convoy passage."""

    segment_id: int
    diversion_type: str  # full_closure | partial_closure | speed_restriction
    activate_at: datetime
    deactivate_at: datetime
    alt_segment_ids: list[int]
    estimated_queue_m: float
    dissipation_time_sec: int
    # additive: the detour walk's length (None without one; for an
    # impractical closure the cheapest walk's, not offered), whether a
    # detour is offered, why not (found | no_path | dead_end | impractical),
    # the junctions the detour joins and any single-junction segments it
    # turns around on
    detour_distance_m: float | None = None
    detour_found: bool = True
    detour_status: str = "found"
    detour_from_junction_id: int | None = None
    detour_to_junction_id: int | None = None
    turnaround_segment_ids: list[int] = field(default_factory=list)
    # additive (WP-16): each end of the detour fixed by the route (False ⇒
    # chosen among several junctions — the detour may be partial), the
    # practicality limit it was held to, and the labelled reason
    entry_pinned: bool | None = None
    exit_pinned: bool | None = None
    detour_limit_m: float | None = None
    detour_reason: str | None = None
    # additive (WP-17, contract C-R): convoy-route segments the detour runs
    # over — each open while this closure is (a route segment closed at the
    # same time is never part of a detour); the /optimize/diversions
    # serializer reads it with getattr(p, "uses_route_segment_ids", [])
    uses_route_segment_ids: list[int] = field(default_factory=list)


@dataclass
class DiversionPlanResult:
    """Plans plus the honesty side channel for one planning request."""

    plans: list[DiversionPlan]
    unplanned_segment_ids: list[int] = field(default_factory=list)
    lengths_source: str = "corridor.road_segments"
    speeds_source: str = "forecaster"


class DiversionPlanner:
    """Computes segment-level diversion strategies for convoy routes.

    CPU-only computation. Uses corridor graph + traffic forecasts to
    minimize the total public disruption window for each affected segment.
    """

    def __init__(
        self,
        heuristic: DiversionHeuristic | None = None,
        forecaster: FlowForecaster | None = None,
    ) -> None:
        self._heuristic = heuristic
        self._forecaster = forecaster

    @property
    def available(self) -> bool:
        return self._heuristic is not None

    @profile_memory
    async def plan(
        self,
        route_segment_ids: list[int],
        convoy_speed_kmh: float = 60.0,
        advance_closure_sec: int = 120,
        segment_lengths: dict[int, float] | None = None,
        departure_time: datetime | None = None,
    ) -> DiversionPlanResult:
        """Generate diversion plans for all segments along a convoy route.

        Args:
            route_segment_ids: Ordered segment IDs of the planned convoy route
            convoy_speed_kmh: Expected convoy travel speed
            advance_closure_sec: How many seconds before convoy arrival to close segment
            segment_lengths: Segment ID → length in meters (None → real
                lengths from corridor.road_segments via the heuristic's graph)
            departure_time: Convoy departure time

        Raises:
            DiversionUnavailableError: no heuristic wired, or the
                corridor graph could not be loaded.
        """
        if self._heuristic is None:
            logger.warning("diversion_planner.no_heuristic")
            raise DiversionUnavailableError(
                "no_heuristic", "diversion planner has no heuristic wired"
            )
        if not route_segment_ids:
            return DiversionPlanResult(plans=[])

        try:
            known_lengths = await self._heuristic.segment_lengths(route_segment_ids)
        except Exception as exc:  # noqa: BLE001 — labelled, never a silent []
            logger.warning(
                "diversion_planner.graph_unavailable",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise DiversionUnavailableError(
                "graph_unavailable", f"corridor graph could not be loaded ({type(exc).__name__})"
            ) from exc

        lengths_source = "caller"
        if segment_lengths is None:
            lengths_source = "corridor.road_segments"
            segment_lengths = dict(known_lengths)
            for sid in route_segment_ids:
                segment_lengths.setdefault(sid, DEFAULT_SEGMENT_LENGTH_M)

        # Per-segment traverse speeds: the 5-min forecast where the
        # forecaster has one, else the convoy speed (labelled in the result).
        predicted_speeds: dict[int, float] = {}
        speeds_source = "convoy_speed"
        if self._forecaster:
            forecasts = await self._forecaster.predict(route_segment_ids, [5])
            for sid, preds in forecasts.items():
                if 5 in preds:
                    predicted_speeds[sid] = preds[5]["speed_kmh"]
            if predicted_speeds:
                covered = sum(1 for sid in route_segment_ids if sid in predicted_speeds)
                speeds_source = (
                    "forecaster" if covered == len(route_segment_ids) else "forecaster_partial"
                )

        # Default speeds to convoy_speed
        for sid in route_segment_ids:
            if sid not in predicted_speeds:
                predicted_speeds[sid] = convoy_speed_kmh

        diversion_routes = await self._heuristic.compute_diversions(
            convoy_route=route_segment_ids,
            predicted_speeds=predicted_speeds,
            segment_lengths=segment_lengths,
            departure_time=departure_time,
            advance_closure_sec=advance_closure_sec,
        )

        plans = []
        for dr in diversion_routes:
            # Determine closure type based on queue length
            if dr.estimated_queue_m > 500:
                diversion_type = "full_closure"
            elif dr.estimated_queue_m > 100:
                diversion_type = "partial_closure"
            else:
                diversion_type = "speed_restriction"

            plans.append(DiversionPlan(
                segment_id=dr.blocked_segment_id,
                diversion_type=diversion_type,
                activate_at=dr.activate_at,
                deactivate_at=dr.deactivate_at,
                alt_segment_ids=dr.alt_segment_ids,
                estimated_queue_m=dr.estimated_queue_m,
                dissipation_time_sec=dr.dissipation_time_sec,
                detour_distance_m=dr.detour_distance_m,
                detour_found=dr.detour_found,
                detour_status=dr.detour_status,
                detour_from_junction_id=dr.detour_from_junction_id,
                detour_to_junction_id=dr.detour_to_junction_id,
                turnaround_segment_ids=dr.turnaround_segment_ids,
                entry_pinned=dr.entry_pinned,
                exit_pinned=dr.exit_pinned,
                detour_limit_m=dr.detour_limit_m,
                detour_reason=dr.detour_reason,
                uses_route_segment_ids=list(dr.uses_route_segment_ids),
            ))

        planned = {p.segment_id for p in plans}
        unplanned = [sid for sid in route_segment_ids if sid not in planned]
        logger.info(
            "diversion_planner.plan",
            segments=len(route_segment_ids),
            diversions=len(plans),
            full_closures=sum(1 for p in plans if p.diversion_type == "full_closure"),
            without_detour=sum(1 for p in plans if not p.detour_found),
            impractical=[p.segment_id for p in plans if p.detour_status == DETOUR_IMPRACTICAL],
            unplanned=unplanned,
        )
        return DiversionPlanResult(
            plans=plans,
            unplanned_segment_ids=unplanned,
            lengths_source=lengths_source,
            speeds_source=speeds_source,
        )

    async def plan_diversions(
        self,
        route_segment_ids: list[int],
        convoy_speed_kmh: float = 60.0,
        advance_closure_sec: int = 120,
        segment_lengths: dict[int, float] | None = None,
        departure_time: datetime | None = None,
    ) -> list[DiversionPlan]:
        """Plans only (see :meth:`plan`); raises DiversionUnavailableError."""
        result = await self.plan(
            route_segment_ids=route_segment_ids,
            convoy_speed_kmh=convoy_speed_kmh,
            advance_closure_sec=advance_closure_sec,
            segment_lengths=segment_lengths,
            departure_time=departure_time,
        )
        return result.plans
