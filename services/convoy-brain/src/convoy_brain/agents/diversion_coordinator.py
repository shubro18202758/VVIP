"""Diversion Coordinator Agent — orchestrates multi-agency diversion execution.

Translates diversion plans from the traffic-oracle into actionable
coordination directives for traffic police, transport departments,
and security agencies. Manages the lifecycle of diversions during
active convoy movements.
"""

from __future__ import annotations

import json

import structlog

from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.memory.convoy_context import ConvoyContextStore
from convoy_brain.ollama_bridge import OllamaBridge

logger = structlog.get_logger(__name__)

DIVERSION_COORDINATOR_PROMPT = """\
You are the Diversion Coordinator agent for the VVIP Convoy Orchestration Platform.
Your role is to manage traffic diversions during active convoy movements.

You coordinate between:
- Traffic Police: Signal overrides, manual traffic direction
- Transport Department: Public transport rerouting, information boards
- Security Agencies: Perimeter control, VIP route clearance

Your responsibilities:
1. ACTIVATE: Issue diversion activation commands at computed times
2. MONITOR: Track diversion compliance and queue buildup
3. ADAPT: Adjust diversion timing if convoy is ahead/behind schedule
4. DEACTIVATE: Restore normal traffic flow segment-by-segment as convoy passes

Critical rule: Public traffic restriction on any segment must NOT exceed
the minimum necessary duration. Target: activate no more than 120 seconds
before convoy arrival at each segment.

Respond with structured JSON:
{
  "directives": [
    {
      "segment_id": int,
      "action": "activate|hold|deactivate",
      "agency": "traffic_police|transport|security",
      "timing_sec": int,
      "detail": str
    }
  ],
  "queue_alerts": [{"segment_id": int, "estimated_queue_m": float, "action": str}],
  "overall_status": str,
  "confidence": "high|medium|low"
}
"""


class DiversionCoordinatorAgent:
    """LLM agent managing real-time diversion execution across agencies."""

    def __init__(
        self,
        bridge: OllamaBridge,
        executor: ToolExecutor,
        context_store: ConvoyContextStore,
    ) -> None:
        self._bridge = bridge
        self._executor = executor
        self._context_store = context_store
        self._system_prompt = DIVERSION_COORDINATOR_PROMPT
        logger.info("diversion_coordinator_agent.init")

    async def activate_diversions(
        self, movement_id: str, segment_ids: list[int],
    ) -> dict:
        """Activate diversions for segments as convoy approaches.

        Returns activation status and coordination directives.
        """
        logger.info(
            "diversion_coordinator.activate",
            movement_id=movement_id,
            segments=len(segment_ids),
        )

        # 1. Get diversion plans from traffic-oracle
        diversion_plan = await self._executor.execute("plan_diversions", {
            "route_segment_ids": segment_ids,
        })

        # 2. Get live traffic on affected segments for queue estimation
        live_traffic = await self._executor.execute("get_live_traffic", {
            "segment_ids": segment_ids,
        })

        # 3. Fetch convoy context for position/speed
        ctx = await self._context_store.get(movement_id)
        convoy_info = {}
        if ctx:
            convoy_info = {
                "convoy_position": list(ctx.convoy_position) if ctx.convoy_position else None,
                "convoy_speed_kmh": ctx.convoy_speed_kmh,
                "active_diversions": ctx.active_diversions,
            }

        # 4. Ask LLM to generate activation directives
        user_prompt = json.dumps({
            "task": "generate_activation_directives",
            "movement_id": movement_id,
            "segment_ids": segment_ids,
            "diversion_plan": diversion_plan,
            "live_traffic": live_traffic,
            "convoy_state": convoy_info,
        }, default=str)

        response = await self._bridge.generate(
            system_prompt=self._system_prompt,
            user_prompt=user_prompt,
        )

        try:
            result = json.loads(response)
            # Update context with activated diversions
            if ctx:
                activated = [
                    str(d["segment_id"])
                    for d in result.get("directives", [])
                    if d.get("action") == "activate"
                ]
                ctx.active_diversions = list(
                    set(ctx.active_diversions) | set(activated)
                )
                await self._context_store.put(ctx)
            return result
        except json.JSONDecodeError:
            logger.warning("diversion_coordinator.parse_failed", raw_len=len(response))
            return {
                "directives": [],
                "queue_alerts": [],
                "overall_status": "Directive generation failed — manual coordination required.",
                "confidence": "low",
            }

    async def adapt_timing(
        self, movement_id: str, convoy_position: tuple[float, float],
    ) -> dict:
        """Adjust diversion timing based on real-time convoy position.

        If convoy is running late, delay upcoming diversions.
        If running early, advance activation of next segments.
        """
        logger.info(
            "diversion_coordinator.adapt",
            movement_id=movement_id,
            position=convoy_position,
        )

        # 1. Fetch current convoy context
        ctx = await self._context_store.get(movement_id)
        if ctx is None:
            return {
                "directives": [],
                "overall_status": "No convoy context found.",
                "confidence": "low",
            }

        # 2. Get live traffic ahead of convoy
        # Use bbox around convoy position for nearby segments
        ahead_segments = await self._executor.execute("query_segments_in_bbox", {
            "min_lon": convoy_position[0] - 0.005,
            "min_lat": convoy_position[1] - 0.005,
            "max_lon": convoy_position[0] + 0.01,  # Wider ahead
            "max_lat": convoy_position[1] + 0.01,
        })
        seg_list = (
            ahead_segments.get("segments", [])
            if isinstance(ahead_segments, dict)
            else ahead_segments if isinstance(ahead_segments, list) else []
        )
        seg_ids = [
            s["segment_id"] if isinstance(s, dict) else s
            for s in seg_list[:50]
        ]

        live_traffic = {}
        if seg_ids:
            live_traffic = await self._executor.execute("get_live_traffic", {
                "segment_ids": seg_ids,
            })

        # 3. Ask LLM for timing adaptation
        user_prompt = json.dumps({
            "task": "adapt_diversion_timing",
            "movement_id": movement_id,
            "convoy_position": list(convoy_position),
            "convoy_speed_kmh": ctx.convoy_speed_kmh,
            "active_diversions": ctx.active_diversions,
            "selected_route": ctx.selected_route_id,
            "segments_ahead": seg_ids,
            "live_traffic_ahead": live_traffic,
        }, default=str)

        response = await self._bridge.generate(
            system_prompt=self._system_prompt,
            user_prompt=user_prompt,
        )

        try:
            result = json.loads(response)
            # Update convoy position in context
            ctx.convoy_position = convoy_position
            await self._context_store.put(ctx)
            return result
        except json.JSONDecodeError:
            return {
                "directives": [],
                "overall_status": "Timing adaptation failed — maintain current schedule.",
                "confidence": "low",
            }

    async def deactivate_passed_segments(
        self, movement_id: str, passed_segment_ids: list[int],
    ) -> dict:
        """Deactivate diversions on segments the convoy has already passed."""
        logger.info(
            "diversion_coordinator.deactivate",
            movement_id=movement_id,
            segments=len(passed_segment_ids),
        )

        ctx = await self._context_store.get(movement_id)
        if ctx is None:
            return {"deactivated": [], "status": "no_context"}

        passed_strs = {str(s) for s in passed_segment_ids}
        deactivated = [s for s in ctx.active_diversions if s in passed_strs]
        ctx.active_diversions = [s for s in ctx.active_diversions if s not in passed_strs]
        await self._context_store.put(ctx)

        logger.info(
            "diversion_coordinator.deactivated",
            movement_id=movement_id,
            count=len(deactivated),
            remaining=len(ctx.active_diversions),
        )
        return {
            "deactivated": deactivated,
            "remaining_active": ctx.active_diversions,
            "status": "ok",
        }
