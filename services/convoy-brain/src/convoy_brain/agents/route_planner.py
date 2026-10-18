"""Route Planner Agent — Gemma 4 12B tactical tier with brain-side scoring.

This agent reasons over route candidates produced by the CorridorRouter
(traffic-oracle OR-Tools MIP). The adaptive tier's ``scoring_weights`` are
applied HERE, brain-side, when ranking ``find_convoy_routes`` candidates
(contract C4 — no traffic-oracle optimize/ edit needed): candidates are
deterministically ranked first, then Gemma narrates the recommendation
against that ranking. The deterministic ranking is also the fallback floor.
"""

from __future__ import annotations

import json

import structlog

from convoy_brain.agents.base import AgentSpec, structured_agent_call
from convoy_brain.config import AdaptiveParams
from convoy_brain.llm.heuristics import heuristic_route_ranking
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.mcp.server import ToolExecutor

logger = structlog.get_logger(__name__)

ROUTE_PLANNER_SYSTEM_PROMPT = """\
You are the Route Planner agent for the Corridor Event Orchestration Platform.
Your role is to analyze route/spine candidates and recommend the optimal
corridor for the event — a protected movement, procession, race, ceremonial,
egress surge, evacuation run or work zone.

You have access to the following tools:
- find_convoy_routes: Multi-objective route search (OR-Tools MIP solver)
- predict_traffic_flow: Forecast speed/congestion at T+5/10/15/30 min
- predict_eta: Estimate travel time along the corridor
- query_segment_details: Road attributes (width, class, speed limit, lanes)
- plan_diversions: Per-segment closure plans with timing and queue estimates
- evaluate_scenarios: Compare routes by total disruption

When recommending routes, always consider:
1. PROTECTION: Wider roads with fewer chokepoints are preferred for higher protection classes
2. CROWD: For public events, prefer spines with hardened perimeters and staged ingress capacity
3. DISRUPTION: Minimize the number of affected public traffic segments
4. TIMING: Consider time-of-day traffic patterns and event phase windows
5. BACKUP: Always recommend at least one alternate route

The candidates arrive pre-ranked by a weighted score (time / disruption /
security / complexity). Respect that ordering unless a hard protection rule
(minimum lanes for the protection class) rejects a candidate — then say so
explicitly in the reason. Use only segment IDs that appear in the candidates.

Respond with structured JSON:
{
  "primary_route": {"route_id": str, "segment_ids": [int], "score": float, "reason": str},
  "alternate_routes": [{"route_id": str, "segment_ids": [int], "score": float, "reason": str}],
  "rejected_routes": [{"route_id": str, "reason": str}],
  "overall_reasoning": str,
  "confidence": "high|medium|low"
}
"""

SPEC = AgentSpec(
    name="route_planner",
    system_prompt=ROUTE_PLANNER_SYSTEM_PROMPT,
    schema_name="route_planner",
)


# Minimum lanes by VVIP class for pre-filtering
_MIN_LANES = {"Z+": 6, "Z": 4, "Y": 2, "X": 1}


class RoutePlannerAgent:
    """Tactical agent specialized in convoy route analysis and recommendation."""

    def __init__(
        self,
        bridge: LlamaBridge,
        executor: ToolExecutor,
        adaptive: AdaptiveParams | None = None,
    ) -> None:
        self._bridge = bridge
        self._executor = executor
        self._adaptive = adaptive
        logger.info("route_planner_agent.init")

    async def analyze_routes(
        self,
        movement_id: str,
        origin: tuple[float, float],
        destination: tuple[float, float],
        vvip_class: str,
        avoid_segments: list[int] | None = None,
    ) -> dict:
        """Analyze and recommend convoy routes for a planned movement."""
        logger.info(
            "route_planner_agent.analyze",
            movement_id=movement_id,
            vvip_class=vvip_class,
        )

        # 1. Resolve origin/destination to nearest segments
        origin_segs = await self._executor.execute("query_segments_in_bbox", {
            "min_lon": origin[0] - 0.002,
            "min_lat": origin[1] - 0.002,
            "max_lon": origin[0] + 0.002,
            "max_lat": origin[1] + 0.002,
        })
        dest_segs = await self._executor.execute("query_segments_in_bbox", {
            "min_lon": destination[0] - 0.002,
            "min_lat": destination[1] - 0.002,
            "max_lon": destination[0] + 0.002,
            "max_lat": destination[1] + 0.002,
        })

        origin_id = _extract_segment_id(origin_segs)
        dest_id = _extract_segment_id(dest_segs)

        if origin_id is None or dest_id is None:
            return {
                "primary_route": None,
                "alternate_routes": [],
                "rejected_routes": [],
                "overall_reasoning": "Could not resolve origin or destination to road segments.",
                "confidence": "low",
                "_tier": "deterministic",
            }

        # 2. Fetch route candidates from MIP solver
        routes_result = await self._executor.execute("find_convoy_routes", {
            "origin_segment": origin_id,
            "destination_segment": dest_id,
            "max_candidates": 5,
            "avoid_segments": avoid_segments or [],
        })
        candidates = (
            routes_result.get("routes", [])
            if isinstance(routes_result, dict) else
            routes_result if isinstance(routes_result, list) else []
        )

        if not candidates:
            return {
                "primary_route": None,
                "alternate_routes": [],
                "rejected_routes": [],
                "overall_reasoning": "MIP solver returned no route candidates.",
                "confidence": "low",
                "_tier": "deterministic",
            }

        # 3. Rank candidates brain-side with the adaptive scoring weights (C4)
        weights = (self._adaptive.scoring_weights if self._adaptive else None) or {}
        ranked = heuristic_route_ranking(candidates, weights)

        # 4. Collect unique segment IDs for forecast
        all_seg_ids: list[int] = []
        for route in ranked:
            segs = route.get("segment_ids", []) if isinstance(route, dict) else []
            all_seg_ids.extend(segs)
        unique_seg_ids = list(set(all_seg_ids))[:200]

        # 5. Fetch traffic forecast
        forecast = await self._executor.execute("predict_traffic_flow", {
            "segment_ids": unique_seg_ids,
            "horizons_min": [5, 15, 30],
        })

        # 6. Gemma narrates the recommendation; deterministic ranking is the floor
        def heuristic() -> dict:
            primary = ranked[0] if ranked else None
            return {
                "primary_route": primary,
                "alternate_routes": ranked[1:],
                "rejected_routes": [],
                "overall_reasoning": (
                    f"Weighted ranking (time/disruption/security/complexity = "
                    f"{json.dumps(weights, sort_keys=True)}); tactical narration "
                    f"unavailable, returning deterministic ranking."
                ),
                "confidence": "low",
            }

        user_prompt = json.dumps({
            "task": "analyze_and_rank_routes",
            "movement_id": movement_id,
            "vvip_class": vvip_class,
            "min_lanes_required": _MIN_LANES.get(vvip_class, 2),
            "origin_segment": origin_id,
            "destination_segment": dest_id,
            "scoring_weights": weights,
            "pre_ranked_candidates": ranked,
            "traffic_forecast": forecast,
        }, default=str)

        result, tier = await structured_agent_call(
            self._bridge,
            SPEC,
            user_prompt,
            heuristic,
            repair_hint="keys: primary_route, alternate_routes[], rejected_routes[], overall_reasoning, confidence",
        )
        result["_tier"] = tier
        result["_ranked_candidates"] = ranked
        return result


def _extract_segment_id(result: dict | list) -> int | None:
    """Extract the first segment ID from a spatial query result."""
    segments = result if isinstance(result, list) else result.get("segments", [])
    if segments and isinstance(segments[0], dict):
        return segments[0].get("segment_id")
    if segments and isinstance(segments[0], int):
        return segments[0]
    return None
