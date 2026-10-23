"""Diversion Coordinator Agent — Gemma 4 12B tactical tier.

Translates diversion plans from traffic-oracle into coordination directives
for traffic police, transport, and security agencies. The adaptive tier's
``advance_closure_sec`` (protocol-floored per VVIP class — contract C4) is
threaded into the ``plan_diversions`` MCP call here; the deterministic
fallback builds directives straight from the plan.

A closure window of 0 s (X class; also a class without a closure floor) is
signal priority only: the coordinator emits ``signal_priority`` directives
for the route segments and never a closure, so nothing enters the closure
bookkeeping (``active_diversions``).

Closure bookkeeping is atomic (field-scoped context updates) and follows
route order: a closure is lifted only once the convoy has passed its
segment, never because the segment fell outside a box around the convoy.

The tactical prompt carries COMPACT rows only (segment, closure type, detour
status, activation time, queue, live congestion) and asks for ONE compact
single-line JSON reply with directives for the 6 most critical rows (≤120
characters each) and at most 4 queue alerts — the whole oracle plan plus
the raw live feed used to overflow the reply cap, and on the Z+ plan even
8 directives plus 8 echoed queue alerts ran to 704 tokens (30 s). The
schema bounds that reply and the call's budget is its bounded maximum
(agents/base.agent_budget), so the first reply is never cut; every other
plan row is covered by the deterministic floor, and the status line is
computed from the final directive set (heuristics.diversion_posture), never
narrated. Every directive, the model's and the floor's alike, is then held
to its plan row's detour facts (heuristics.apply_detour_facts): an
impractical detour reads as hold / meter with the oracle's reason, a
partial one says so, a found one names its alternative segments.
"""

from __future__ import annotations

import json

import structlog

from convoy_brain.agents.base import AgentSpec, structured_agent_call
from convoy_brain.config import AdaptiveParams, baseline_params
from convoy_brain.llm.heuristics import (
    apply_detour_facts,
    diversion_posture,
    diversion_rows,
    heuristic_diversion_directives,
    live_observation_rows,
)
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.memory.convoy_context import (
    ConvoyContext,
    ConvoyContextStore,
    mutate_context,
)

logger = structlog.get_logger(__name__)

#: Directive action for a signal-priority-only class (no closure).
SIGNAL_PRIORITY_ACTION = "signal_priority"


def signal_priority_directives(segment_ids: list[int], vvip_class: str) -> dict:
    """Green-wave directives for a 0 s closure window — never a closure."""
    directives = [
        {
            "segment_id": int(sid),
            "action": SIGNAL_PRIORITY_ACTION,
            "agency": "traffic_police",
            "timing_sec": 0,
            "detail": (
                f"Signal priority (green wave) on segment {int(sid)} as the "
                f"{vvip_class} convoy approaches — no closure; public traffic keeps moving."
            ),
        }
        for sid in dict.fromkeys(segment_ids)
        if sid is not None
    ]
    return {
        "directives": directives,
        "queue_alerts": [],
        "overall_status": (
            f"{len(directives)} signal-priority directive(s); {vvip_class} protocol "
            "has a 0 s closure window (signal priority only, no closures)."
        ),
        "confidence": "high",
    }


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

Each plan row states its detour_status: 'found' (a detour exists),
'impractical' / 'no_path' / 'dead_end' (no usable detour: hold or meter the
cross traffic, never invent a diversion). Write directives for the 6 most
critical closures at most (no usable detour and the longest queues first);
the platform issues the other closures itself. Keep each detail under 120
characters (about 18 words) and cite the numbers (queue metres, congestion,
activation time). Raise at most 4 queue_alerts, for the longest queues;
each action is a short traffic-police instruction such as "staff the
junction for queue discharge".

Respond with ONE compact single-line JSON object — no line breaks, no
indentation:
{"directives":[{"segment_id":int,"action":"activate|hold|deactivate","agency":"traffic_police|transport|security","timing_sec":int,"detail":str}],"queue_alerts":[{"segment_id":int,"estimated_queue_m":float,"action":str}],"confidence":"high|medium|low"}
"""

SPEC = AgentSpec(
    name="diversion_coordinator",
    system_prompt=DIVERSION_COORDINATOR_PROMPT,
    schema_name="diversion_coordinator",
)


def compact_plan_rows(diversion_plan: dict | None, live_traffic: dict | None) -> list[dict]:
    """The prompt's view of the oracle plan: one compact row per closure.

    ``segment_id, diversion_type, detour_status, activate_at,
    estimated_queue_m, congestion`` — no geometry, no alternative-segment
    lists, no raw feed. Rows are ordered most critical first (no usable
    detour, then the longest queue), so a reply limited to 8 directives
    covers the closures that matter.
    """
    congestion = {r["segment_id"]: r["congestion_idx"] for r in live_observation_rows(live_traffic)}
    rows = []
    for row in diversion_rows(diversion_plan):
        sid = row.get("segment_id")
        if sid is None:
            continue
        try:
            key = int(sid)
        except (TypeError, ValueError):
            continue
        queue = row.get("estimated_queue_m")
        rows.append({
            "segment_id": key,
            "diversion_type": row.get("diversion_type"),
            "detour_status": row.get("detour_status"),
            "activate_at": row.get("activate_at"),
            "estimated_queue_m": round(float(queue)) if isinstance(queue, (int, float)) else None,
            "congestion": round(congestion[key], 2) if key in congestion else None,
        })
    rows.sort(key=lambda r: (
        0 if r["detour_status"] in ("impractical", "no_path", "dead_end") else 1,
        -(r["estimated_queue_m"] or 0),
    ))
    return rows


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
        before being threaded into the plan_diversions call. Without an
        adaptive parameter set the fail-closed baseline is floored the same
        way, so X class is signal-priority-only either way.
        """
        logger.info(
            "diversion_coordinator.activate",
            movement_id=movement_id,
            segments=len(segment_ids),
        )

        ctx = await self._context_store.get(movement_id)
        vvip_class = vvip_class or (ctx.vvip_class if ctx else "Y")
        advance_closure_sec = (
            self._adaptive or baseline_params()
        ).floored_advance_closure_sec(vvip_class)

        # 1. Get diversion plans from traffic-oracle, threading the
        #    protocol-floored advance closure window through the MCP call.
        #    A failed fetch degrades to an empty plan (never aborts).
        plan_args: dict = {
            "route_segment_ids": segment_ids,
            "advance_closure_sec": advance_closure_sec,
        }
        diversion_plan = await self._safe_tool("plan_diversions", plan_args)

        if advance_closure_sec == 0:
            # Signal priority only: no closure directive, no Gemma narration,
            # nothing enters the closure bookkeeping.
            result = signal_priority_directives(segment_ids, vvip_class)
            result["diversion_plan_rows"] = len(
                (diversion_plan or {}).get("diversions") or []
            ) if isinstance(diversion_plan, dict) else 0
            result["_tier"] = "deterministic"
            return result

        # 2. Get live traffic on affected segments for queue estimation
        live_traffic = await self._safe_tool("get_live_traffic", {
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
                diversion_plan,
                advance_closure_sec if advance_closure_sec is not None else 120,
            )

        # 4. Gemma generates the coordination directives (heuristic floor)
        user_prompt = json.dumps({
            "task": "generate_activation_directives",
            "movement_id": movement_id,
            "vvip_class": vvip_class,
            "advance_closure_sec": advance_closure_sec,
            "plan_rows": compact_plan_rows(diversion_plan, live_traffic),
            "convoy_state": convoy_info,
            "instruction": "directives for the 6 most critical closures at most",
        }, default=str)

        result, tier = await structured_agent_call(
            self._bridge,
            SPEC,
            user_prompt,
            heuristic,
            repair_hint=(
                "keys: directives[]{segment_id, action, agency, timing_sec, detail}, "
                "queue_alerts[], overall_status, confidence"
            ),
        )

        # The oracle's plan is the floor: a planned segment the narration left
        # without ANY directive gets the deterministic one (labelled), so a
        # terse model answer can never leave part of the route unprotected.
        # A directive the model did write for a segment is kept as written.
        if tier != "heuristic":
            directives = [d for d in result.get("directives") or [] if isinstance(d, dict)]
            covered = {str(d.get("segment_id")) for d in directives}
            filled = [
                dict(d, source="deterministic_floor")
                for d in heuristic()["directives"]
                if str(d["segment_id"]) not in covered
            ]
            if filled:
                result["directives"] = [*directives, *filled]
                result["floor_filled_segment_ids"] = [d["segment_id"] for d in filled]
                logger.info(
                    "diversion_coordinator.floor_filled",
                    movement_id=movement_id,
                    model_directives=len(directives),
                    filled=len(filled),
                )
            else:
                result["directives"] = directives
            # The model's text is held to the oracle's detour facts too.
            result["directives"] = apply_detour_facts(result["directives"], diversion_plan)
            # The status line describes the FINAL directive set, floor rows
            # included, so it is computed, never narrated: the model sees and
            # writes only its 6 most critical closures, and its own summary
            # of that subset read "6 full closures and 1 partial closure with
            # no detour available" for an 18-closure plan (live Z+ run).
            result["overall_status"] = diversion_posture(
                result["directives"], result.get("queue_alerts"))

        # Persist activated segments to the movement context — only segments
        # this call was about (the route or the oracle's plan rows), merged
        # into the FRESH document, so a model-invented id never becomes a
        # closure and a concurrent write is never lost.
        planned = {str(s) for s in segment_ids}
        if isinstance(diversion_plan, dict):
            planned |= {
                str(r.get("segment_id")) for r in diversion_plan.get("diversions") or []
                if isinstance(r, dict) and r.get("segment_id") is not None
            }
        activated = [
            str(d["segment_id"])
            for d in result.get("directives", [])
            if isinstance(d, dict) and d.get("action") == "activate"
            and d.get("segment_id") is not None and str(d["segment_id"]) in planned
        ]
        if activated:
            def add(c: ConvoyContext) -> bool:
                merged = list(dict.fromkeys([*c.active_diversions, *activated]))
                if merged == c.active_diversions:
                    return False
                c.active_diversions = merged
                return True

            await mutate_context(self._context_store, movement_id, add)
        result["_tier"] = tier
        return result

    async def _safe_tool(self, name: str, args: dict) -> dict | None:
        """One oracle call, isolated: a failure is logged and reads as no data."""
        try:
            result = await self._executor.execute(name, args)
        except Exception as exc:
            logger.warning("diversion_coordinator.tool_failed", tool=name, error=str(exc)[:200])
            return None
        return result if isinstance(result, dict) else None

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

        # 1. Segments around the convoy (isolated: a failed lookup reads as none)
        ahead_segments = await self._safe_tool("query_segments_in_bbox", {
            "min_lon": convoy_position[0] - 0.005,
            "min_lat": convoy_position[1] - 0.005,
            "max_lon": convoy_position[0] + 0.01,  # Wider ahead
            "max_lat": convoy_position[1] + 0.01,
        })
        seg_list = (ahead_segments or {}).get("segments", [])
        seg_ids = [
            s["segment_id"] if isinstance(s, dict) else s
            for s in (seg_list if isinstance(seg_list, list) else [])[:50]
        ]

        live_traffic: dict = {}
        if seg_ids:
            live_traffic = await self._safe_tool("get_live_traffic", {
                "segment_ids": seg_ids,
            }) or {}

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
        result.setdefault("overall_status", diversion_posture(
            [d for d in result.get("directives") or [] if isinstance(d, dict)],
            result.get("queue_alerts")))
        # The convoy position is written only by the GPS accept path
        # (store.update_position): writing this call's snapshot back could
        # move the convoy to where it was before the model answered.
        result["_tier"] = tier
        return result

    async def tick_update(
        self,
        movement_id: str,
        convoy_position: tuple[float, float],
        segments_ahead: list[int],
        *,
        route_segment_ids: list[int] | None = None,
    ) -> dict:
        """LLM-free per-tick bookkeeping: lift closures the convoy has passed.

        ``segments_ahead`` is the convoy's current segment and the route
        after it, in route order; ``route_segment_ids`` is the whole route.
        A closure is lifted only when its segment is ON the route and not
        ahead of the convoy any more (passed). Closures off the route, and
        every closure when the route order is unknown, are kept — lifting a
        closure right before the convoy arrives is the failure to avoid.
        The GPS tick loop never touches a model (red-team F8) and never
        writes ``convoy_position`` back (the GPS accept path owns it).
        """
        if route_segment_ids is None:
            ctx = await self._context_store.get(movement_id)
            if ctx is None:
                return {"deactivated": [], "status": "no_context"}
            return {
                "deactivated": [],
                "remaining_active": list(ctx.active_diversions),
                "status": "no_route_order",
            }

        on_route = {str(s) for s in route_segment_ids}
        ahead = {str(s) for s in segments_ahead}
        outcome: dict = {"deactivated": [], "remaining_active": []}

        def lift(c: ConvoyContext) -> bool:
            passed = [s for s in c.active_diversions if s in on_route and s not in ahead]
            outcome["deactivated"] = passed
            outcome["remaining_active"] = [s for s in c.active_diversions if s not in passed]
            if not passed:
                return False
            c.active_diversions = outcome["remaining_active"]
            return True

        ctx = await mutate_context(self._context_store, movement_id, lift)
        if ctx is None:
            return {"deactivated": [], "status": "no_context"}
        if outcome["deactivated"]:
            logger.info(
                "diversion_coordinator.passed_lifted",
                movement_id=movement_id,
                lifted=outcome["deactivated"],
                remaining=len(outcome["remaining_active"]),
            )
        return {**outcome, "status": "ok"}

    async def deactivate_passed_segments(
        self, movement_id: str, passed_segment_ids: list[int],
    ) -> dict:
        """Deactivate diversions on segments the convoy has already passed."""
        logger.info(
            "diversion_coordinator.deactivate",
            movement_id=movement_id,
            segments=len(passed_segment_ids),
        )

        passed_strs = {str(s) for s in passed_segment_ids}
        outcome: dict = {"deactivated": [], "remaining_active": []}

        def lift(c: ConvoyContext) -> bool:
            outcome["deactivated"] = [s for s in c.active_diversions if s in passed_strs]
            outcome["remaining_active"] = [
                s for s in c.active_diversions if s not in passed_strs
            ]
            if not outcome["deactivated"]:
                return False
            c.active_diversions = outcome["remaining_active"]
            return True

        ctx = await mutate_context(self._context_store, movement_id, lift)
        if ctx is None:
            return {"deactivated": [], "status": "no_context"}

        logger.info(
            "diversion_coordinator.deactivated",
            movement_id=movement_id,
            count=len(outcome["deactivated"]),
            remaining=len(outcome["remaining_active"]),
        )
        return {**outcome, "status": "ok"}
