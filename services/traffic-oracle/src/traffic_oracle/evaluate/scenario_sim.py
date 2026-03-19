"""Scenario Simulator — evaluates "what-if" scenarios for convoy planning.

Answers questions like:
    - "What happens if we route via Sardar Patel Marg at 10:00 vs 10:30?"
    - "How much additional congestion does Route A cause vs Route B?"
    - "What if we reduce advance closure from 3 minutes to 90 seconds?"

Uses the flow forecaster + diversion planner to simulate outcomes
without executing real closures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog

from traffic_oracle.optimize.corridor_router import CorridorRouter
from traffic_oracle.optimize.diversion_planner import DiversionPlanner
from traffic_oracle.predict.eta_model import ETAPredictor
from traffic_oracle.predict.flow_forecaster import FlowForecaster
from traffic_oracle.runtime.memory_profiler import profile_memory

logger = structlog.get_logger(__name__)

# Average vehicles per km per lane on urban roads
_DEFAULT_TRAFFIC_DENSITY_VEH_PER_KM = 30.0
# Average vehicle spacing in queue (bumper-to-bumper), metres
_AVG_VEHICLE_QUEUE_SPACING_M = 7.0


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


class ScenarioSimulator:
    """Monte Carlo-style scenario evaluator for convoy route planning.

    Runs CPU-only simulations using traffic forecasts and diversion plans
    to compare route/timing options before committing to a movement plan.
    """

    def __init__(
        self,
        forecaster: FlowForecaster | None = None,
        router: CorridorRouter | None = None,
        diversion_planner: DiversionPlanner | None = None,
        eta_predictor: ETAPredictor | None = None,
    ) -> None:
        self._forecaster = forecaster
        self._router = router
        self._diversion_planner = diversion_planner
        self._eta_predictor = eta_predictor

    @profile_memory
    async def evaluate(
        self,
        scenarios: list[dict],
    ) -> list[ScenarioResult]:
        """Evaluate multiple convoy scenarios and rank by minimal disruption.

        Args:
            scenarios: List of scenario configs, each containing:
                - scenario_id: str
                - route_segment_ids: list[int]
                - departure_time: datetime
                - convoy_speed_kmh: float  (default 60)
                - advance_closure_sec: int  (default 120)
                - route_name: str  (default "unnamed")
                - segment_lengths: dict[int, float] | None

        Returns:
            Ranked scenario results, best (least disruptive) first
        """
        results: list[ScenarioResult] = []

        for scenario in scenarios:
            result = await self._evaluate_single(scenario)
            results.append(result)

        # Rank by least disruption
        results.sort(key=lambda r: r.total_disruption_vehicle_hours)

        logger.info(
            "scenario_simulator.evaluate",
            count=len(scenarios),
            best_id=results[0].scenario_id if results else None,
            best_disruption_vh=round(results[0].total_disruption_vehicle_hours, 2) if results else None,
        )
        return results

    async def _evaluate_single(self, scenario: dict) -> ScenarioResult:
        """Evaluate a single convoy movement scenario."""
        scenario_id: str = scenario.get("scenario_id", "unknown")
        route_segment_ids: list[int] = scenario.get("route_segment_ids", [])
        departure_time: datetime | None = scenario.get("departure_time")
        convoy_speed_kmh: float = scenario.get("convoy_speed_kmh", 60.0)
        advance_closure_sec: int = scenario.get("advance_closure_sec", 120)
        route_name: str = scenario.get("route_name", "unnamed")
        segment_lengths: dict[int, float] | None = scenario.get("segment_lengths")

        if not route_segment_ids:
            return self._empty_result(scenario_id, route_name)

        # 1. Get traffic forecasts for route segments
        predicted_speeds: dict[int, float] = {}
        predicted_congestion: dict[int, float] = {}

        if self._forecaster:
            forecasts = await self._forecaster.predict(route_segment_ids, [5])
            for sid, preds in forecasts.items():
                if 5 in preds:
                    predicted_speeds[sid] = preds[5]["speed_kmh"]
                    predicted_congestion[sid] = preds[5]["congestion_idx"]

        # Fill defaults for segments without forecasts
        for sid in route_segment_ids:
            predicted_speeds.setdefault(sid, convoy_speed_kmh)
            predicted_congestion.setdefault(sid, 0.3)

        # Default segment lengths
        if segment_lengths is None:
            segment_lengths = {sid: 500.0 for sid in route_segment_ids}

        # 2. Compute diversions via diversion planner
        diversion_plans = []
        if self._diversion_planner:
            diversion_plans = await self._diversion_planner.plan_diversions(
                route_segment_ids=route_segment_ids,
                convoy_speed_kmh=convoy_speed_kmh,
                advance_closure_sec=advance_closure_sec,
                segment_lengths=segment_lengths,
                departure_time=departure_time,
            )

        # 3. Simulate queue buildup on affected + diversion segments
        total_disruption_veh_hours = 0.0
        max_queue_m = 0.0
        total_closure_sec = 0.0

        for plan in diversion_plans:
            closure_sec = (plan.deactivate_at - plan.activate_at).total_seconds()
            total_closure_sec += closure_sec

            # Queue from this segment's closure
            queue_m = plan.estimated_queue_m
            max_queue_m = max(max_queue_m, queue_m)

            # Vehicles stopped = queue length / vehicle spacing
            vehicles_queued = queue_m / _AVG_VEHICLE_QUEUE_SPACING_M

            # Avg delay per vehicle = closure_sec + dissipation_time / 2
            avg_delay_sec = closure_sec + plan.dissipation_time_sec / 2.0

            # Vehicle-hours of delay for this segment
            veh_hours = vehicles_queued * avg_delay_sec / 3600.0
            total_disruption_veh_hours += veh_hours

        # If no diversion planner, estimate from segment properties directly
        if not diversion_plans:
            total_disruption_veh_hours, max_queue_m, total_closure_sec = (
                self._estimate_disruption_simple(
                    route_segment_ids,
                    predicted_congestion,
                    segment_lengths,
                    convoy_speed_kmh,
                    advance_closure_sec,
                )
            )

        segments_affected = len(diversion_plans) if diversion_plans else len(route_segment_ids)
        avg_closure_sec = total_closure_sec / max(segments_affected, 1)

        # 4. Estimate convoy transit time
        convoy_transit_sec = self._estimate_transit_time(
            route_segment_ids, segment_lengths, convoy_speed_kmh,
        )

        # 5. Assess complaints risk from peak queue lengths
        risk = self._assess_risk(max_queue_m, total_disruption_veh_hours)

        return ScenarioResult(
            scenario_id=scenario_id,
            route_name=route_name,
            total_disruption_vehicle_hours=round(total_disruption_veh_hours, 3),
            max_queue_length_m=round(max_queue_m, 1),
            avg_closure_duration_sec=round(avg_closure_sec, 1),
            segments_affected=segments_affected,
            estimated_complaints_risk=risk,
            convoy_transit_time_sec=int(convoy_transit_sec),
        )

    def _estimate_disruption_simple(
        self,
        segment_ids: list[int],
        predicted_congestion: dict[int, float],
        segment_lengths: dict[int, float],
        convoy_speed_kmh: float,
        advance_closure_sec: int,
    ) -> tuple[float, float, float]:
        """Fallback disruption estimate without diversion planner."""
        total_veh_hours = 0.0
        max_queue_m = 0.0
        total_closure_sec = 0.0

        for sid in segment_ids:
            length_m = segment_lengths.get(sid, 500.0)
            congestion = predicted_congestion.get(sid, 0.3)

            # Time convoy takes to cross this segment
            traverse_sec = length_m / max(convoy_speed_kmh * 1000 / 3600, 0.1)
            closure_sec = traverse_sec + advance_closure_sec
            total_closure_sec += closure_sec

            # Vehicles arriving during closure
            inflow_rate = _DEFAULT_TRAFFIC_DENSITY_VEH_PER_KM * congestion * 2.0
            vehicles_queued = inflow_rate * (closure_sec / 3600.0)

            # Queue length
            queue_m = vehicles_queued * _AVG_VEHICLE_QUEUE_SPACING_M
            max_queue_m = max(max_queue_m, queue_m)

            # Avg delay per vehicle ≈ half the closure duration
            avg_delay_sec = closure_sec / 2.0
            total_veh_hours += vehicles_queued * avg_delay_sec / 3600.0

        return total_veh_hours, max_queue_m, total_closure_sec

    @staticmethod
    def _estimate_transit_time(
        segment_ids: list[int],
        segment_lengths: dict[int, float],
        convoy_speed_kmh: float,
    ) -> float:
        """Estimate total convoy transit time in seconds."""
        total_dist_m = sum(segment_lengths.get(sid, 500.0) for sid in segment_ids)
        speed_ms = max(convoy_speed_kmh * 1000 / 3600, 0.1)
        return total_dist_m / speed_ms

    @staticmethod
    def _assess_risk(max_queue_m: float, disruption_veh_hours: float) -> str:
        """Classify complaints risk based on queue length and disruption."""
        if max_queue_m > 1000 or disruption_veh_hours > 50:
            return "high"
        if max_queue_m > 300 or disruption_veh_hours > 10:
            return "medium"
        return "low"

    @staticmethod
    def _empty_result(scenario_id: str, route_name: str) -> ScenarioResult:
        return ScenarioResult(
            scenario_id=scenario_id,
            route_name=route_name,
            total_disruption_vehicle_hours=0.0,
            max_queue_length_m=0.0,
            avg_closure_duration_sec=0.0,
            segments_affected=0,
            estimated_complaints_risk="low",
            convoy_transit_time_sec=0,
        )
