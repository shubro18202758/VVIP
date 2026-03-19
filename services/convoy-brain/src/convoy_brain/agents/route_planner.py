"""Route Planner Agent — LLM-driven convoy route selection and comparison.

This agent reasons over route candidates produced by the CorridorRouter
(traffic-oracle) and makes contextual recommendations. It considers:
    - Current and predicted traffic conditions
    - Security requirements for the VVIP class
    - Historical patterns (time of day, day of week)
    - Ongoing events or construction
    - Multi-route diversification for unpredictability
"""

from __future__ import annotations

import json

import structlog

from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.ollama_bridge import OllamaBridge

logger = structlog.get_logger(__name__)

ROUTE_PLANNER_SYSTEM_PROMPT = """\
You are the Route Planner agent for the VVIP Convoy Orchestration Platform.
Your role is to analyze route candidates and recommend the optimal convoy path.

You have access to the following tools:
- find_convoy_routes: Multi-objective route search (OR-Tools MIP solver)
- predict_traffic_flow: Forecast speed/congestion at T+5/10/15/30 min
- predict_eta: Estimate convoy travel time
- query_segment_details: Road attributes (width, class, speed limit, lanes)
- plan_diversions: Per-segment closure plans with timing and queue estimates
- evaluate_scenarios: Compare routes by total disruption

When recommending routes, always consider:
1. SECURITY: Wider roads with fewer chokepoints are preferred for higher VVIP classes
2. DISRUPTION: Minimize the number of affected public traffic segments
3. TIMING: Consider time-of-day traffic patterns
4. BACKUP: Always recommend at least one alternate route

Respond with structured JSON:
{
  "primary_route": {"route_id": str, "segment_ids": [int], "score": float, "reason": str},
  "alternate_routes": [{"route_id": str, "segment_ids": [int], "score": float, "reason": str}],
  "rejected_routes": [{"route_id": str, "reason": str}],
  "overall_reasoning": str,
  "confidence": "high|medium|low"
}
"""


# Minimum lanes by VVIP class for pre-filtering
_MIN_LANES = {"Z+": 6, "Z": 4, "Y": 2, "X": 1}


class RoutePlannerAgent:
    """LLM agent specialized in convoy route analysis and recommendation."""

    def __init__(self, bridge: OllamaBridge, executor: ToolExecutor) -> None:
        self._bridge = bridge
        self._executor = executor
        self._system_prompt = ROUTE_PLANNER_SYSTEM_PROMPT
        logger.info("route_planner_agent.init")

    async def analyze_routes(
        self,
        movement_id: str,
        origin: tuple[float, float],
        destination: tuple[float, float],
        vvip_class: str,
        avoid_segments: list[int] | None = None,
    ) -> dict:
        """Analyze and recommend convoy routes for a planned movement.

        Args:
            movement_id: UUID of the convoy movement
            origin: (lon, lat) of movement origin
            destination: (lon, lat) of movement destination
            vvip_class: Security class (Z+, Z, Y, X)
            avoid_segments: Optional segments to exclude

        Returns:
            Structured recommendation with primary route, alternates, and reasoning
        """
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
            }

        # 3. Collect all unique segment IDs for forecast
        all_seg_ids: list[int] = []
        for route in candidates:
            segs = route.get("segment_ids", []) if isinstance(route, dict) else []
            all_seg_ids.extend(segs)
        unique_seg_ids = list(set(all_seg_ids))[:200]

        # 4. Fetch traffic forecast
        forecast = await self._executor.execute("predict_traffic_flow", {
            "segment_ids": unique_seg_ids,
            "horizons_min": [5, 15, 30],
        })

        # 5. Ask LLM to evaluate and rank
        user_prompt = json.dumps({
            "task": "analyze_and_rank_routes",
            "movement_id": movement_id,
            "vvip_class": vvip_class,
            "min_lanes_required": _MIN_LANES.get(vvip_class, 2),
            "origin_segment": origin_id,
            "destination_segment": dest_id,
            "route_candidates": candidates,
            "traffic_forecast": forecast,
        }, default=str)

        response = await self._bridge.generate(
            system_prompt=self._system_prompt,
            user_prompt=user_prompt,
        )

        try:
            return json.loads(response)
        except json.JSONDecodeError:
            logger.warning("route_planner.parse_failed", raw_len=len(response))
            return {
                "primary_route": candidates[0] if candidates else None,
                "alternate_routes": candidates[1:],
                "rejected_routes": [],
                "overall_reasoning": "LLM analysis failed; returning unranked MIP solver output.",
                "confidence": "low",
            }


def _extract_segment_id(result: dict | list) -> int | None:
    """Extract the first segment ID from a spatial query result."""
    segments = result if isinstance(result, list) else result.get("segments", [])
    if segments and isinstance(segments[0], dict):
        return segments[0].get("segment_id")
    if segments and isinstance(segments[0], int):
        return segments[0]
    return None
