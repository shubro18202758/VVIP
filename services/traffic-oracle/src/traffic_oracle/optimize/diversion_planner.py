"""Diversion Planner — generates pre-computed diversion strategies for
road segments affected by convoy movement.

For each segment along a planned convoy route, computes:
    - Alternative routes for diverted public traffic
    - Optimal activation/deactivation timing (minimize closure window)
    - Estimated queue formation and dissipation times
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog

from traffic_oracle.optimize.constructive_heuristic import DiversionHeuristic
from traffic_oracle.predict.flow_forecaster import FlowForecaster
from traffic_oracle.runtime.memory_profiler import profile_memory

logger = structlog.get_logger(__name__)


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

    @profile_memory
    async def plan_diversions(
        self,
        route_segment_ids: list[int],
        convoy_speed_kmh: float = 60.0,
        advance_closure_sec: int = 120,
        segment_lengths: dict[int, float] | None = None,
        departure_time: datetime | None = None,
    ) -> list[DiversionPlan]:
        """Generate diversion plans for all segments along a convoy route.

        Args:
            route_segment_ids: Ordered segment IDs of the planned convoy route
            convoy_speed_kmh: Expected convoy travel speed
            advance_closure_sec: How many seconds before convoy arrival to close segment
            segment_lengths: Segment ID → length in meters (fetched if None)
            departure_time: Convoy departure time

        Returns:
            Per-segment diversion plans with timing and alternative routes
        """
        if not route_segment_ids:
            return []

        # Get predicted speeds for route segments
        predicted_speeds: dict[int, float] = {}
        if self._forecaster:
            forecasts = await self._forecaster.predict(route_segment_ids, [5])
            for sid, preds in forecasts.items():
                if 5 in preds:
                    predicted_speeds[sid] = preds[5]["speed_kmh"]

        # Default speeds to convoy_speed
        for sid in route_segment_ids:
            if sid not in predicted_speeds:
                predicted_speeds[sid] = convoy_speed_kmh

        # Default segment lengths
        if segment_lengths is None:
            segment_lengths = {sid: 500.0 for sid in route_segment_ids}

        # Delegate to heuristic for detour computation
        if self._heuristic:
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
                ))

            logger.info(
                "diversion_planner.plan",
                segments=len(route_segment_ids),
                diversions=len(plans),
                full_closures=sum(1 for p in plans if p.diversion_type == "full_closure"),
            )
            return plans

        logger.warning("diversion_planner.no_heuristic")
        return []
