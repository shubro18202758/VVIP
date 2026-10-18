"""Pre-Event Planning Workflow — multi-agent planning pipeline.

Orchestrates the full planning sequence before a corridor event goes live
(a protected movement, procession, race, ceremonial, egress surge,
evacuation or work zone — the event-agnostic pipeline):

    1. Adaptive parameters resolve (fail-closed to protocol baselines)
    2. Decider pre-movement checkpoint (strategic tier, <=1500-token state)
    3. Traffic Analyst assesses current corridor conditions   (Gemma tier)
    4. Route Planner generates and evaluates route candidates  (Gemma tier;
       adaptive scoring_weights applied brain-side)
    5. Security Liaison validates routes against protocol      (Gemma tier)
    6. Diversion Coordinator plans segment-level diversions    (Gemma tier;
       protocol-floored advance_closure_sec threaded into plan_diversions)
    7. Scenario Simulator compares alternatives                (numeric tier)
    8. Final recommendation presented to the command center

Implemented as a plain async pipeline (the node structure and conditional
security-retry loop of the previous graph, without the graph dependency).
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from convoy_brain.adaptive_params import AdaptiveParamsClient
from convoy_brain.agents.diversion_coordinator import DiversionCoordinatorAgent
from convoy_brain.agents.route_planner import RoutePlannerAgent
from convoy_brain.agents.security_liaison import SecurityLiaisonAgent
from convoy_brain.agents.traffic_analyst import TrafficAnalystAgent
from convoy_brain.decider import DeciderBrain
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.memory.convoy_context import ConvoyContext, ConvoyContextStore

logger = structlog.get_logger(__name__)

# Maximum security re-route attempts before proceeding with warnings
MAX_SECURITY_RETRIES = 2

# Corridor bounding box padding (degrees) around origin+destination
CORRIDOR_PAD_DEG = 0.02


async def run_pre_movement_workflow(
    movement_id: str,
    origin: tuple[float, float],
    destination: tuple[float, float],
    vvip_class: str,
    planned_departure: str,
    *,
    bridge: LlamaBridge | None = None,
    executor: ToolExecutor | None = None,
    context_store: ConvoyContextStore | None = None,
    decider_brain: DeciderBrain | None = None,
    adaptive_client: AdaptiveParamsClient | None = None,
) -> dict:
    """Execute the full pre-movement planning workflow.

    Args:
        movement_id: UUID of the planned movement
        origin: (lon, lat) corridor origin
        destination: (lon, lat) corridor destination
        vvip_class: Z+, Z, Y, or X
        planned_departure: ISO 8601 datetime string
        bridge: LlamaBridge instance (required)
        executor: ToolExecutor instance (required)
        context_store: ConvoyContextStore instance (required)
        decider_brain: strategic tier (optional; skipped when absent)
        adaptive_client: adaptive-parameter client (optional; fail-closed)

    Returns:
        Complete movement plan with route, diversions, timing, and risk assessment
    """
    if bridge is None or executor is None or context_store is None:
        raise ValueError(
            "bridge, executor, and context_store are required. "
            "Pass them as keyword arguments."
        )

    logger.info(
        "pre_movement_workflow.start",
        movement_id=movement_id,
        vvip_class=vvip_class,
        departure=planned_departure,
    )

    # -- 1. Adaptive parameters (fail-closed; floors applied on use) ---------
    adaptive = None
    if adaptive_client is not None:
        adaptive = await adaptive_client.fetch(movement_id=movement_id, vvip_class=vvip_class)
        logger.info(
            "pre_movement.adaptive_params",
            movement_id=movement_id,
            source=adaptive.source,
            shadow_eval=adaptive.shadow_eval,
            situation_class=adaptive.situation_class,
        )

    traffic_analyst = TrafficAnalystAgent(bridge, executor)
    route_planner = RoutePlannerAgent(bridge, executor, adaptive=adaptive)
    security_liaison = SecurityLiaisonAgent(bridge, executor)
    diversion_coordinator = DiversionCoordinatorAgent(
        bridge, executor, context_store, adaptive=adaptive
    )

    state: dict[str, Any] = {
        "movement_id": movement_id,
        "origin": origin,
        "destination": destination,
        "vvip_class": vvip_class,
        "planned_departure": planned_departure,
        "avoid_segments": None,
        "security_retries": 0,
        "decider_checkpoint": None,
        "adaptive": adaptive,
    }

    # -- 2. Strategic tier: pre-movement decider checkpoint -------------------
    if decider_brain is not None:
        state_doc = DeciderBrain.build_state_doc(
            trigger="pre_movement",
            summary=(
                f"Pre-event planning. Protection class {vvip_class}, departure "
                f"{planned_departure}, origin {origin}, destination {destination}."
            ),
            context={"phase": "pre_movement"},
            pre_movement=True,
        )
        questions = DeciderBrain.pre_movement_questions(
            ["primary", "alternate", "delay_departure"]
        )
        checkpoint = await decider_brain.checkpoint(
            movement_id=movement_id,
            trigger="pre_movement",
            state_doc=state_doc,
            questions=questions,
            force=True,
        )
        state["decider_checkpoint"] = checkpoint.audit
        for alert in checkpoint.operator_alerts:
            await context_store.append_decision(
                movement_id, dict(alert, phase="pre_movement_alert")
            )

    # -- 3. Corridor assessment ------------------------------------------------
    bbox = (
        min(origin[0], destination[0]) - CORRIDOR_PAD_DEG,
        min(origin[1], destination[1]) - CORRIDOR_PAD_DEG,
        max(origin[0], destination[0]) + CORRIDOR_PAD_DEG,
        max(origin[1], destination[1]) + CORRIDOR_PAD_DEG,
    )
    state["corridor_assessment"] = await traffic_analyst.assess_corridor(bbox)
    logger.info(
        "pre_movement.corridor_assessed",
        movement_id=movement_id,
        status=state["corridor_assessment"].get("overall_status"),
    )

    # -- 4/5. Route planning with conditional security-retry loop ---------------
    for _ in range(MAX_SECURITY_RETRIES + 1):
        state["route_candidates"] = await route_planner.analyze_routes(
            movement_id=movement_id,
            origin=origin,
            destination=destination,
            vvip_class=vvip_class,
            avoid_segments=state.get("avoid_segments"),
        )
        logger.info(
            "pre_movement.routes_planned",
            movement_id=movement_id,
            primary=state["route_candidates"].get("primary_route") is not None,
            alternates=len(state["route_candidates"].get("alternate_routes", [])),
        )

        state["security_validation"] = await _validate_security(
            security_liaison, state
        )
        logger.info(
            "pre_movement.security_validated",
            movement_id=movement_id,
            compliant=state["security_validation"].get("compliant"),
            violations=len(state["security_validation"].get("violations", [])),
        )

        if state["security_validation"].get("compliant", False):
            break

        if state["security_retries"] >= MAX_SECURITY_RETRIES:
            logger.warning(
                "pre_movement.security_max_retries",
                movement_id=movement_id,
                retries=state["security_retries"],
            )
            break  # Proceed with warnings

        # Feed violations back to the planner as an avoid list
        current_avoid = list(state.get("avoid_segments") or [])
        for v in state["security_validation"].get("violations", []):
            seg_id = v.get("segment_id")
            if seg_id is not None and seg_id not in current_avoid:
                current_avoid.append(seg_id)
        state["avoid_segments"] = current_avoid
        state["security_retries"] += 1
        logger.info(
            "pre_movement.reroute",
            movement_id=movement_id,
            avoid_count=len(current_avoid),
            retry=state["security_retries"],
        )

    # -- 6. Diversion planning (advance_closure_sec protocol-floored) ----------
    primary = state["route_candidates"].get("primary_route")
    if primary is None:
        state["diversion_plans"] = {"directives": [], "overall_status": "no_route"}
    else:
        state["diversion_plans"] = await diversion_coordinator.activate_diversions(
            movement_id=movement_id,
            segment_ids=primary.get("segment_ids", []),
            vvip_class=vvip_class,
        )
        logger.info(
            "pre_movement.diversions_planned",
            movement_id=movement_id,
            directives=len(state["diversion_plans"].get("directives", [])),
        )

    # -- 7. Scenario evaluation (numeric tier) ----------------------------------
    alternates = state["route_candidates"].get("alternate_routes", [])
    scenarios = []
    for route in [primary, *alternates]:
        if route is None:
            continue
        scenarios.append({
            "route_id": route.get("route_id", "unknown"),
            "segment_ids": route.get("segment_ids", []),
            "departure_time": planned_departure,
        })

    if not scenarios:
        state["scenario_result"] = {"scenarios": [], "best": None}
    else:
        state["scenario_result"] = await executor.execute(
            "evaluate_scenarios", {"scenarios": scenarios}
        )
        logger.info(
            "pre_movement.scenarios_evaluated",
            movement_id=movement_id,
            count=len(scenarios),
        )

    # -- 8. Finalize ---------------------------------------------------------------
    recommendation = await _finalize(state, context_store)
    logger.info(
        "pre_movement_workflow.complete",
        movement_id=movement_id,
        status=recommendation["status"],
    )
    return recommendation


async def _validate_security(
    liaison: SecurityLiaisonAgent, state: dict
) -> dict:
    primary = state.get("route_candidates", {}).get("primary_route")
    if primary is None:
        return {
            "compliant": False,
            "violations": [],
            "warnings": [],
            "recommendations": ["No primary route to validate."],
            "security_score": 0.0,
            "confidence": "low",
        }
    return await liaison.validate_route(primary.get("segment_ids", []), state["vvip_class"])


async def _finalize(state: dict, ctx_store: ConvoyContextStore) -> dict:
    """Compose the final recommendation and persist it to the context store."""
    routes = state.get("route_candidates", {})
    primary = routes.get("primary_route")
    security = state.get("security_validation", {})
    diversions = state.get("diversion_plans", {})
    scenario = state.get("scenario_result", {})
    corridor = state.get("corridor_assessment", {})
    adaptive = state.get("adaptive")

    recommendation = {
        "movement_id": state["movement_id"],
        "vvip_class": state["vvip_class"],
        "planned_departure": state.get("planned_departure"),
        "corridor_status": corridor.get("overall_status"),
        "primary_route": primary,
        "alternate_routes": routes.get("alternate_routes", []),
        "security_compliant": security.get("compliant", False),
        "security_violations": security.get("violations", []),
        "security_warnings": security.get("warnings", []),
        "security_score": security.get("security_score", 0.0),
        "diversion_directives": diversions.get("directives", []),
        "scenario_comparison": scenario,
        "status": "approved" if security.get("compliant", False) else "conditional",
        "confidence": _aggregate_confidence(
            corridor.get("confidence"),
            routes.get("confidence"),
            security.get("confidence"),
            diversions.get("confidence"),
        ),
        "adaptive_params": {
            "source": adaptive.source,
            "shadow_eval": adaptive.shadow_eval,
            "situation_class": adaptive.situation_class,
            "advance_closure_sec": (
                adaptive.floored_advance_closure_sec(state["vvip_class"])
                if adaptive else None
            ),
        } if adaptive else None,
        "decider_checkpoint": state.get("decider_checkpoint"),
    }

    ctx = await ctx_store.get(state["movement_id"])
    if ctx is None:
        ctx = ConvoyContext(movement_id=state["movement_id"], vvip_class=state["vvip_class"])
    ctx.status = recommendation["status"]
    if adaptive is not None:
        ctx.situation_class = adaptive.situation_class
    if primary:
        ctx.selected_route_id = primary.get("route_id")
    ctx.decisions_log.append({
        "phase": "pre_movement",
        "recommendation": recommendation["status"],
        "security_compliant": recommendation["security_compliant"],
        "timestamp": time.time(),
    })
    await ctx_store.put(ctx)
    return recommendation


def _aggregate_confidence(*levels: str | None) -> str:
    """Aggregate multiple confidence levels into a conservative overall."""
    rank = {"high": 3, "medium": 2, "low": 1}
    values = [rank.get(l, 0) for l in levels if l is not None]
    if not values:
        return "low"
    avg = sum(values) / len(values)
    if avg >= 2.5:
        return "high"
    if avg >= 1.5:
        return "medium"
    return "low"
