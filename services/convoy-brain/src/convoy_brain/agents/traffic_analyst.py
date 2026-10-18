"""Traffic Analyst Agent — Gemma 4 12B tactical tier.

Monitors live traffic conditions across the corridor and provides
situational awareness to the command center. Data gathering is deterministic
(MCP tools); Gemma synthesizes the situation report under a JSON schema,
with a deterministic heuristic fallback when the tactical tier is down.
"""

from __future__ import annotations

import json

import structlog

from convoy_brain.agents.base import AgentSpec, structured_agent_call
from convoy_brain.llm.heuristics import heuristic_traffic_assessment
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.mcp.server import ToolExecutor

logger = structlog.get_logger(__name__)

TRAFFIC_ANALYST_SYSTEM_PROMPT = """\
You are the Traffic Analyst agent for the Corridor Event Orchestration Platform.
Your role is to continuously assess corridor traffic conditions and flag risks
for the active corridor event (protected movement or public event).

You have access to:
- get_live_traffic: Real-time speed and congestion for segments
- predict_traffic_flow: Forecast speed/congestion at T+5/10/15/30 min
- get_historical_pattern: Historical traffic patterns by day/hour
- query_segments_in_bbox: Find segments in a geographic bounding box

Your responsibilities:
1. MONITOR: Identify congestion spikes, incidents, and anomalies
2. FORECAST: Assess whether conditions are improving or deteriorating
3. ALERT: Flag segments where current conditions deviate significantly from forecast
4. ADVISE: Recommend timing adjustments for planned events and phase windows

Always quantify your assessments (congestion index, queue lengths, ETAs).
Base every number on the tool data provided in the prompt — never invent
segment IDs, speeds, or congestion values.

Respond with structured JSON:
{
  "overall_status": "green|amber|red",
  "congestion_summary": {"avg_congestion_idx": float, "peak_congestion_idx": float},
  "risk_segments": [{"segment_id": int, "issue": str, "severity": str}],
  "trend": "improving|stable|deteriorating",
  "recommendation": str,
  "confidence": "high|medium|low"
}
"""

SPEC = AgentSpec(
    name="traffic_analyst",
    system_prompt=TRAFFIC_ANALYST_SYSTEM_PROMPT,
    schema_name="traffic_analyst",
)


class TrafficAnalystAgent:
    """Tactical agent for real-time corridor traffic situation assessment."""

    def __init__(self, bridge: LlamaBridge, executor: ToolExecutor) -> None:
        self._bridge = bridge
        self._executor = executor
        logger.info("traffic_analyst_agent.init")

    async def assess_corridor(
        self, corridor_bbox: tuple[float, float, float, float],
    ) -> dict:
        """Assess current traffic conditions across the corridor.

        Returns structured situation report with risk flags. Falls back to
        the deterministic assessment when the tactical tier is unavailable.
        """
        logger.info("traffic_analyst_agent.assess", bbox=corridor_bbox)

        # 1. Find segments in the corridor
        segments_result = await self._executor.execute("query_segments_in_bbox", {
            "min_lon": corridor_bbox[0],
            "min_lat": corridor_bbox[1],
            "max_lon": corridor_bbox[2],
            "max_lat": corridor_bbox[3],
        })
        segments = _coerce_segment_list(segments_result)

        if not segments:
            return heuristic_traffic_assessment(None)

        segment_ids = [s for s in segments[:200]]  # Cap at 200 for context budget

        # 2. Fetch live traffic data
        live_data = await self._executor.execute("get_live_traffic", {
            "segment_ids": segment_ids,
        })

        # 3. Fetch forecast for T+15 min
        forecast_data = await self._executor.execute("predict_traffic_flow", {
            "segment_ids": segment_ids,
            "horizons_min": [15],
        })

        # 4. Ask Gemma to synthesize the situation report (heuristic floor)
        user_prompt = json.dumps({
            "task": "assess_corridor_conditions",
            "corridor_bbox": list(corridor_bbox),
            "num_segments": len(segment_ids),
            "live_traffic": live_data,
            "forecast_t15": forecast_data,
        }, default=str)

        result, tier = await structured_agent_call(
            self._bridge,
            SPEC,
            user_prompt,
            lambda: heuristic_traffic_assessment(live_data, forecast_data),
            repair_hint="keys: overall_status, congestion_summary{{avg_congestion_idx, peak_congestion_idx}}, risk_segments[], trend, recommendation, confidence",
        )
        result["_tier"] = tier
        return result

    async def check_movement_feasibility(
        self,
        movement_id: str,
        planned_departure: str,
        route_segment_ids: list[int] | None = None,
    ) -> dict:
        """Evaluate whether current conditions support a planned departure time."""
        logger.info(
            "traffic_analyst_agent.feasibility",
            movement_id=movement_id,
            departure=planned_departure,
        )

        tool_data: dict = {"movement_id": movement_id, "departure": planned_departure}
        forecast = None

        if route_segment_ids:
            forecast = await self._executor.execute("predict_traffic_flow", {
                "segment_ids": route_segment_ids,
                "horizons_min": [5, 10, 15, 30],
            })
            live = await self._executor.execute("get_live_traffic", {
                "segment_ids": route_segment_ids,
            })
            tool_data["forecast"] = forecast
            tool_data["live_traffic"] = live

        def heuristic() -> dict:
            assessment = heuristic_traffic_assessment(
                tool_data.get("live_traffic"), forecast
            )
            red = assessment["overall_status"] == "red"
            return {
                "go": not red,
                "reasoning": assessment["recommendation"],
                "confidence": assessment["confidence"],
            }

        user_prompt = json.dumps({
            "task": "check_movement_feasibility",
            **tool_data,
        }, default=str)

        result, tier = await structured_agent_call(
            self._bridge,
            SPEC,
            user_prompt,
            heuristic,
            repair_hint='keys: go (boolean), reasoning (string), confidence ("high"|"medium"|"low")',
        )
        result["_tier"] = tier
        return result


def _coerce_segment_list(result: dict | list) -> list:
    if isinstance(result, dict):
        result = result.get("segments", [])
    if not isinstance(result, list):
        return []
    out = []
    for item in result:
        if isinstance(item, dict) and item.get("segment_id") is not None:
            out.append(item["segment_id"])
        elif isinstance(item, int):
            out.append(item)
    return out
