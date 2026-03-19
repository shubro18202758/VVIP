"""Traffic Analyst Agent — LLM-driven real-time traffic situation assessment.

Monitors live traffic conditions across the corridor and provides
natural-language situational awareness to the command center.
Identifies anomalies, incidents, and unusual patterns that may
impact planned or active convoy movements.
"""

from __future__ import annotations

import json

import structlog

from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.ollama_bridge import OllamaBridge

logger = structlog.get_logger(__name__)

TRAFFIC_ANALYST_SYSTEM_PROMPT = """\
You are the Traffic Analyst agent for the VVIP Convoy Orchestration Platform.
Your role is to continuously assess corridor traffic conditions and flag risks.

You have access to:
- get_live_traffic: Real-time speed and congestion for segments
- predict_traffic_flow: Forecast speed/congestion at T+5/10/15/30 min
- get_historical_pattern: Historical traffic patterns by day/hour
- query_segments_in_bbox: Find segments in a geographic bounding box

Your responsibilities:
1. MONITOR: Identify congestion spikes, incidents, and anomalies
2. FORECAST: Assess whether conditions are improving or deteriorating
3. ALERT: Flag segments where current conditions deviate significantly from forecast
4. ADVISE: Recommend timing adjustments for planned movements

Always quantify your assessments (congestion index, queue lengths, ETAs).
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


class TrafficAnalystAgent:
    """LLM agent for real-time corridor traffic situation assessment."""

    def __init__(self, bridge: OllamaBridge, executor: ToolExecutor) -> None:
        self._bridge = bridge
        self._executor = executor
        self._system_prompt = TRAFFIC_ANALYST_SYSTEM_PROMPT
        logger.info("traffic_analyst_agent.init")

    async def assess_corridor(
        self, corridor_bbox: tuple[float, float, float, float],
    ) -> dict:
        """Assess current traffic conditions across the corridor.

        Returns structured situation report with risk flags.
        """
        logger.info("traffic_analyst_agent.assess", bbox=corridor_bbox)

        # 1. Find segments in the corridor
        segments_result = await self._executor.execute("query_segments_in_bbox", {
            "min_lon": corridor_bbox[0],
            "min_lat": corridor_bbox[1],
            "max_lon": corridor_bbox[2],
            "max_lat": corridor_bbox[3],
        })
        segments = (
            segments_result.get("segments", [])
            if isinstance(segments_result, dict)
            else segments_result if isinstance(segments_result, list) else []
        )

        if not segments:
            return {
                "overall_status": "red",
                "congestion_summary": {"avg_congestion_idx": 0, "peak_congestion_idx": 0},
                "risk_segments": [],
                "trend": "stable",
                "recommendation": "No segments found in corridor bounding box.",
                "confidence": "low",
            }

        segment_ids = [
            s["segment_id"] if isinstance(s, dict) else s
            for s in segments[:200]  # Cap at 200 for inference budget
        ]

        # 2. Fetch live traffic data
        live_data = await self._executor.execute("get_live_traffic", {
            "segment_ids": segment_ids,
        })

        # 3. Fetch forecast for T+15 min
        forecast_data = await self._executor.execute("predict_traffic_flow", {
            "segment_ids": segment_ids,
            "horizons_min": [15],
        })

        # 4. Ask LLM to synthesize the situation report
        user_prompt = json.dumps({
            "task": "assess_corridor_conditions",
            "corridor_bbox": list(corridor_bbox),
            "num_segments": len(segment_ids),
            "live_traffic": live_data,
            "forecast_t15": forecast_data,
        }, default=str)

        response = await self._bridge.generate(
            system_prompt=self._system_prompt,
            user_prompt=user_prompt,
        )

        try:
            return json.loads(response)
        except json.JSONDecodeError:
            logger.warning("traffic_analyst.parse_failed", raw_len=len(response))
            return {
                "overall_status": "amber",
                "congestion_summary": {"avg_congestion_idx": 0, "peak_congestion_idx": 0},
                "risk_segments": [],
                "trend": "stable",
                "recommendation": "Assessment produced unparseable response.",
                "confidence": "low",
                "raw_response": response[:500],
            }

    async def check_movement_feasibility(
        self,
        movement_id: str,
        planned_departure: str,
        route_segment_ids: list[int] | None = None,
    ) -> dict:
        """Evaluate whether current conditions support a planned departure time.

        Returns go/no-go recommendation with reasoning.
        """
        logger.info(
            "traffic_analyst_agent.feasibility",
            movement_id=movement_id,
            departure=planned_departure,
        )

        tool_data: dict = {"movement_id": movement_id, "departure": planned_departure}

        # Fetch predictions for route segments if provided
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

        user_prompt = json.dumps({
            "task": "check_movement_feasibility",
            **tool_data,
        }, default=str)

        response = await self._bridge.generate(
            system_prompt=self._system_prompt,
            user_prompt=user_prompt,
        )

        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return {
                "go": False,
                "reasoning": "Feasibility check produced unparseable response.",
                "confidence": "low",
            }
