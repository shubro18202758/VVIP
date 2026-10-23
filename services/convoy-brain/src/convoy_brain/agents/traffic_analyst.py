"""Traffic Analyst Agent — Gemma 4 12B tactical tier.

Monitors live traffic conditions across the corridor and provides
situational awareness to the command center. Data gathering is deterministic
(MCP tools); Gemma synthesizes the situation report under a JSON schema,
with a deterministic heuristic fallback when the tactical tier is down.

A corridor with zero live observations is reported deterministically as
``unknown`` (unverified) without a model call: there is nothing to narrate,
and the empty narration was the single slowest step of a plan (~45 s of
Gemma on an empty payload). A failing oracle tool degrades the report the
same way instead of aborting the plan.
"""

from __future__ import annotations

import json

import structlog

from convoy_brain.agents.base import AgentSpec, structured_agent_call
from convoy_brain.llm.heuristics import (
    forecast_rows,
    heuristic_traffic_assessment,
    live_observation_rows,
)
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

Respond with ONE compact single-line JSON object — no line breaks, no
indentation; at most 6 risk_segments (each issue under 160 characters),
the recommendation under 400 characters, indices to 2 decimals:
{"overall_status":"green|amber|red","congestion_summary":{"avg_congestion_idx":float,"peak_congestion_idx":float},"risk_segments":[{"segment_id":int,"issue":str,"severity":"low|medium|high|critical"}],"trend":"improving|stable|deteriorating","recommendation":str,"confidence":"high|medium|low"}
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
        try:
            segments_result = await self._executor.execute("query_segments_in_bbox", {
                "min_lon": corridor_bbox[0],
                "min_lat": corridor_bbox[1],
                "max_lon": corridor_bbox[2],
                "max_lat": corridor_bbox[3],
            })
        except Exception as exc:
            return _degraded(f"corridor segment lookup failed: {exc}")
        segments = _coerce_segment_list(segments_result)

        if not segments:
            result = heuristic_traffic_assessment(None)
            result.update({"_tier": "deterministic", "segments": 0})
            return result

        segment_ids = [s for s in segments[:200]]  # Cap at 200 for context budget

        # 2. Fetch live traffic data
        try:
            live_data = await self._executor.execute("get_live_traffic", {
                "segment_ids": segment_ids,
            })
        except Exception as exc:
            return _degraded(f"live traffic lookup failed: {exc}", segments=len(segment_ids))
        observations = live_observation_rows(live_data)
        if not observations:
            # Nothing to narrate: a silent feed is "unknown", decided without
            # a tactical call (and without a forecast, which has no input).
            logger.info(
                "traffic_analyst_agent.no_observations",
                segments=len(segment_ids),
            )
            result = heuristic_traffic_assessment(None)
            result.update({"_tier": "deterministic", "segments": len(segment_ids)})
            return result

        # 3. Fetch forecast for T+15 min (non-fatal)
        try:
            forecast_data = await self._executor.execute("predict_traffic_flow", {
                "segment_ids": [r["segment_id"] for r in observations][:200],
                "horizons_min": [15],
            })
        except Exception as exc:
            logger.warning("traffic_analyst_agent.forecast_failed", error=str(exc))
            forecast_data = {}

        # 4. Ask Gemma to synthesize the situation report (heuristic floor)
        user_prompt = json.dumps({
            "task": "assess_corridor_conditions",
            "corridor_bbox": list(corridor_bbox),
            "num_segments": len(segment_ids),
            "segments_with_live_data": len(observations),
            "live_traffic": observations,
            "forecast_t15": [
                {k: r[k] for k in ("segment_id", "congestion_idx", "speed_kmh")}
                for r in forecast_rows(forecast_data)
            ],
        }, default=str)

        result, tier = await structured_agent_call(
            self._bridge,
            SPEC,
            user_prompt,
            lambda: heuristic_traffic_assessment(live_data, forecast_data),
            repair_hint="keys: overall_status, congestion_summary{{avg_congestion_idx, peak_congestion_idx}}, risk_segments[], trend, recommendation, confidence",
        )
        result["_tier"] = tier
        result["observations"] = len(observations)
        result["segments"] = len(segment_ids)
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
            # Each fetch is isolated: a failing tool leaves its slot empty
            # (recorded under tool_errors) and the check still answers.
            errors: dict[str, str] = {}
            try:
                forecast = await self._executor.execute("predict_traffic_flow", {
                    "segment_ids": route_segment_ids,
                    "horizons_min": [5, 10, 15, 30],
                })
            except Exception as exc:
                errors["predict_traffic_flow"] = str(exc)[:200]
                forecast = None
            try:
                live = await self._executor.execute("get_live_traffic", {
                    "segment_ids": route_segment_ids,
                })
            except Exception as exc:
                errors["get_live_traffic"] = str(exc)[:200]
                live = None
            tool_data["forecast"] = forecast
            tool_data["live_traffic"] = live
            if errors:
                logger.warning("traffic_analyst_agent.feasibility_degraded", errors=errors)
                tool_data["tool_errors"] = errors

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


def _degraded(reason: str, *, segments: int | None = None) -> dict:
    """Unknown-status report for a failed data fetch (never aborts a plan)."""
    logger.warning("traffic_analyst_agent.degraded", reason=reason[:200])
    result = heuristic_traffic_assessment(None)
    result["recommendation"] = (
        f"Corridor assessment degraded — {reason[:200]}. Conditions unverified."
    )
    result["_tier"] = "deterministic"
    result["degraded"] = True
    if segments is not None:
        result["segments"] = segments
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
