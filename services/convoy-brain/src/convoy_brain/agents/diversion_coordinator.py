"""Diversion Coordinator Agent — Gemma 4 12B tactical tier.

Translates diversion plans from traffic-oracle into coordination directives
for traffic police, transport, and security agencies. The adaptive tier's
``advance_closure_sec`` (protocol-floored per VVIP class — contract C4) is
threaded into the ``plan_diversions`` MCP call here; the deterministic
fallback builds directives straight from the plan.
"""

from __future__ import annotations

import json

import structlog

from convoy_brain.agents.base import AgentSpec, structured_agent_call
from convoy_brain.config import AdaptiveParams
from convoy_brain.llm.heuristics import heuristic_diversion_directives
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.memory.convoy_context import ConvoyContextStore
from convoy_brain.mcp.server import ToolExecutor

logger = structlog.get_logger(__name__)

DIVERSION_COORDINATOR_PROMPT = """\
You are the Diversion Coordinator agent for the Corridor Event Orchestration Platform.
Your role is to manage traffic diversions during active corridor events —
protected movements, processions, races, egress surges, evacuations and
work zones alike.

You coordinate between:
- Traffic Police: Signal overrides, manual traffic direction
- Transport Department: Public transport rerouting, information boards
- Security / Event Agencies: Perimeter control, route and gate clearance

Your responsibilities:
1. ACTIVATE: Issue diversion activation commands at computed times
2. MONITOR: Track diversion compliance and queue buildup
3. ADAPT: Adjust diversion timing if the event is ahead/behind schedule
4. DEACTIVATE: Restore normal traffic flow segment-by-segment as the corridor releases

Critical rule: Public traffic restriction on any segment must NOT exceed
the minimum necessary duration. The advance_closure_sec value in the payload
is the protocol-floored ceiling for the active protection class — never recommend
activating earlier than that ceiling.

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

SPEC = AgentSpec(
    name="diversion_coordinator",
    system_prompt=DIVERSION_COORDINATOR_PROMPT,
    schema_name="diversion_coordinator",
)


class DiversionCoordinatorAgent:
    """Tactical agent managing real-time diversion execution across agencies."""

    def __init__(
        self,
        bridge: LlamaBridge,
        executor: ToolExecutor,
        context_store: ConvoyContextStore,
        adaptive: AdaptiveParams | None = None,
    ) -> None:
        self._bridge = bridge
        self._executor = executor
        self._context_store = context_store
        self._adaptive = adaptive
        logger.info("diversion_coordinator_agent.init")

    async def activate_diversions(
        self,
        movement_id: str,
        segment_ids: list[int],
        vvip_class: str | None = None,
    ) -> dict:
        """Activate diversions for segments as convoy approaches.

        The advance closure window is protocol-floored for the movement's
        VVIP class (Z+ 180s / Z 120s / Y 60s / X 0 — signal priority only)
        before being threaded into the plan_diversions call.
        """
        logger.info(
            "diversion_coordinator.activate",
            movement_id=movement_id,
            segments=len(segment_ids),
        )

        ctx = await self._context_store.get(movement_id)
        vvip_class = vvip_class or (ctx.vvip_class if ctx else "Y")
        advance_closure_sec = (
            self._adaptive.floored_advance_closure_sec(vvip_class)
            if self._adaptive else None
        )

        # 1. Get diversion plans from traffic-oracle, threading the
        #    protocol-floored advance closure window through the MCP call.
        plan_args: dict = {"route_segment_ids": segment_ids}
        if advance_closure_sec is not None:
            plan_args["advance_closure_sec"] = advance_closure_sec
        diversion_plan = await self._executor.execute("plan_diversions", plan_args)

        # 2. Get live traffic on affected segments for queue estimation
        live_traffic = await self._executor.execute("get_live_traffic", {
            "segment_ids": segment_ids,
        })

        # 3. Convoy context for position/speed
        convoy_info = {}
        if ctx:
            convoy_info = {
                "convoy_position": list(ctx.convoy_position) if ctx.convoy_position else None,
                "convoy_speed_kmh": ctx.convoy_speed_kmh,
                "active_diversions": ctx.active_diversions,
            }

        def heuristic() -> dict:
            return heuristic_diversion_directives(
                diversion_plan, advance_closure_sec or 120
            )

        # 4. Gemma generates the coordination directives (heuristic floor)
        user_prompt = json.dumps({
            "task": "generate_activation_directives",
            "movement_id": movement_id,
            "vvip_class": vvip_class,
            "advance_closure_sec": advance_closure_sec,
            "segment_ids": segment_ids,
            "diversion_plan": diversion_plan,
            "live_traffic": live_traffic,
            "convoy_state": convoy_info,
        }, default=str)

        result, tier = await structured_agent_call(
            self._bridge,
            SPEC,
            user_prompt,
            heuristic,
            repair_hint="keys: directives[]{{segment_id, action, agency, timing_sec, detail}}, queue_alerts[], overall_status, confidence",
        )

        # Persist activated segments to the movement context.
        if ctx:
            activated = [
                str(d["segment_id"])
                for d in result.get("directives", [])
                if isinstance(d, dict) and d.get("action") == "activate"
            ]
            ctx.active_diversions = list(set(ctx.active_diversions) | set(activated))
            await self._context_store.put(ctx)
        result["_tier"] = tier
        return result

    async def adapt_timing(
        self, movement_id: str, convoy_position: tuple[float, float],
    ) -> dict:
        """Adjust diversion timing based on real-time convoy position."""
        logger.info(
            "diversion_coordinator.adapt",
            movement_id=movement_id,
            position=convoy_position,
        )

        ctx = await self._context_store.get(movement_id)
        if ctx is None:
            return {
                "directives": [],
                "queue_alerts": [],
                "overall_status": "No convoy context found.",
                "confidence": "low",
            }

        # 1. Segments ahead of convoy
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

        live_traffic: dict = {}
        if seg_ids:
            live_traffic = await self._executor.execute("get_live_traffic", {
                "segment_ids": seg_ids,
            })

        def heuristic() -> dict:
            return {
                "directives": [],
                "queue_alerts": [],
                "overall_status": "Timing adaptation unavailable — maintain current schedule.",
                "confidence": "low",
            }

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

        result, tier = await structured_agent_call(
            self._bridge,
            SPEC,
            user_prompt,
            heuristic,
        )

        # Keep convoy position current in the context store.
        ctx.convoy_position = convoy_position
        await self._context_store.put(ctx)
        result["_tier"] = tier
        return result

    async def tick_update(
        self,
        movement_id: str,
        convoy_position: tuple[float, float],
        segments_ahead: list[int],
    ) -> dict:
        """LLM-free per-tick bookkeeping: position update + deactivation.

        The GPS tick loop must never touch a model (red-team F8); tactical
        reasoning is reserved for the incident path (:meth:`adapt_timing`).
        """
        ctx = await self._context_store.get(movement_id)
        if ctx is None:
            return {"deactivated": [], "status": "no_context"}

        ctx.convoy_position = convoy_position
        ahead = {str(s) for s in segments_ahead}
        passed = [s for s in ctx.active_diversions if s not in ahead]
        if passed:
            ctx.active_diversions = [s for s in ctx.active_diversions if s not in ahead]
        await self._context_store.put(ctx)
        return {
            "deactivated": passed,
            "remaining_active": ctx.active_diversions,
            "status": "ok",
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
