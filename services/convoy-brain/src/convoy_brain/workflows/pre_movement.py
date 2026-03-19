"""Pre-Movement Planning Workflow — multi-agent planning pipeline.

Orchestrates the full planning sequence before a VVIP convoy moves:

    1. Traffic Analyst assesses current corridor conditions
    2. Route Planner generates and evaluates route candidates
    3. Security Liaison validates routes against protocol
    4. Diversion Coordinator plans segment-level diversions
    5. Scenario Simulator compares alternatives
    6. Final recommendation presented to command center

This is a LangGraph StateGraph where each node is an agent invocation
and edges represent the planning sequence with conditional branching.
"""

from __future__ import annotations

import time
from typing import Any, TypedDict

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from convoy_brain.agents.diversion_coordinator import DiversionCoordinatorAgent
from convoy_brain.agents.route_planner import RoutePlannerAgent
from convoy_brain.agents.security_liaison import SecurityLiaisonAgent
from convoy_brain.agents.traffic_analyst import TrafficAnalystAgent
from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.memory.convoy_context import ConvoyContext, ConvoyContextStore
from convoy_brain.ollama_bridge import OllamaBridge

logger = structlog.get_logger(__name__)

# Maximum security re-route attempts before proceeding with warnings
MAX_SECURITY_RETRIES = 2

# Corridor bounding box padding (degrees) around origin+destination
CORRIDOR_PAD_DEG = 0.02


class PreMovementState(TypedDict, total=False):
    """State schema for the pre-movement planning workflow."""
    movement_id: str
    origin: tuple[float, float]
    destination: tuple[float, float]
    vvip_class: str
    planned_departure: str
    avoid_segments: list[int] | None
    security_retries: int
    corridor_assessment: dict[str, Any]
    route_candidates: dict[str, Any]
    security_validation: dict[str, Any]
    diversion_plans: dict[str, Any]
    scenario_result: dict[str, Any]
    final_recommendation: dict[str, Any]


# ---------------------------------------------------------------------------
# Node functions
# ---------------------------------------------------------------------------

async def _assess_corridor(state: dict, config: RunnableConfig) -> dict:
    """Node: Traffic Analyst assesses corridor conditions."""
    agents = config["configurable"]["agents"]
    analyst: TrafficAnalystAgent = agents["traffic_analyst"]

    origin = state["origin"]
    destination = state["destination"]

    bbox = (
        min(origin[0], destination[0]) - CORRIDOR_PAD_DEG,
        min(origin[1], destination[1]) - CORRIDOR_PAD_DEG,
        max(origin[0], destination[0]) + CORRIDOR_PAD_DEG,
        max(origin[1], destination[1]) + CORRIDOR_PAD_DEG,
    )

    assessment = await analyst.assess_corridor(bbox)
    logger.info(
        "pre_movement.corridor_assessed",
        movement_id=state["movement_id"],
        status=assessment.get("overall_status"),
    )
    return {"corridor_assessment": assessment}


async def _plan_routes(state: dict, config: RunnableConfig) -> dict:
    """Node: Route Planner generates and ranks route candidates."""
    agents = config["configurable"]["agents"]
    planner: RoutePlannerAgent = agents["route_planner"]

    result = await planner.analyze_routes(
        movement_id=state["movement_id"],
        origin=state["origin"],
        destination=state["destination"],
        vvip_class=state["vvip_class"],
        avoid_segments=state.get("avoid_segments"),
    )
    logger.info(
        "pre_movement.routes_planned",
        movement_id=state["movement_id"],
        primary=result.get("primary_route") is not None,
        alternates=len(result.get("alternate_routes", [])),
    )
    return {"route_candidates": result}


async def _validate_security(state: dict, config: RunnableConfig) -> dict:
    """Node: Security Liaison validates the primary route against protocol."""
    agents = config["configurable"]["agents"]
    liaison: SecurityLiaisonAgent = agents["security_liaison"]

    routes = state.get("route_candidates", {})
    primary = routes.get("primary_route")

    if primary is None:
        return {
            "security_validation": {
                "compliant": False,
                "violations": [],
                "warnings": [],
                "recommendations": ["No primary route to validate."],
                "security_score": 0.0,
                "confidence": "low",
            },
        }

    segment_ids = primary.get("segment_ids", [])
    result = await liaison.validate_route(segment_ids, state["vvip_class"])

    logger.info(
        "pre_movement.security_validated",
        movement_id=state["movement_id"],
        compliant=result.get("compliant"),
        violations=len(result.get("violations", [])),
    )
    return {
        "security_validation": result,
        "security_retries": state.get("security_retries", 0),
    }


def _security_router(state: dict) -> str:
    """Conditional edge: route back to planner if non-compliant."""
    validation = state.get("security_validation", {})
    retries = state.get("security_retries", 0)

    if validation.get("compliant", False):
        return "plan_diversions"

    if retries >= MAX_SECURITY_RETRIES:
        logger.warning(
            "pre_movement.security_max_retries",
            movement_id=state.get("movement_id"),
            retries=retries,
        )
        return "plan_diversions"  # Proceed with warnings

    return "reroute"


async def _reroute_with_constraints(state: dict, config: RunnableConfig) -> dict:
    """Node: Feed security violations back to Route Planner as avoid list."""
    validation = state.get("security_validation", {})
    violations = validation.get("violations", [])

    current_avoid = list(state.get("avoid_segments") or [])
    for v in violations:
        seg_id = v.get("segment_id")
        if seg_id is not None and seg_id not in current_avoid:
            current_avoid.append(seg_id)

    retries = state.get("security_retries", 0) + 1
    logger.info(
        "pre_movement.reroute",
        movement_id=state["movement_id"],
        avoid_count=len(current_avoid),
        retry=retries,
    )
    return {
        "avoid_segments": current_avoid,
        "security_retries": retries,
    }


async def _plan_diversions(state: dict, config: RunnableConfig) -> dict:
    """Node: Diversion Coordinator plans diversions for the selected route."""
    agents = config["configurable"]["agents"]
    coordinator: DiversionCoordinatorAgent = agents["diversion_coordinator"]

    routes = state.get("route_candidates", {})
    primary = routes.get("primary_route")

    if primary is None:
        return {"diversion_plans": {"directives": [], "overall_status": "no_route"}}

    segment_ids = primary.get("segment_ids", [])
    result = await coordinator.activate_diversions(
        movement_id=state["movement_id"],
        segment_ids=segment_ids,
    )

    logger.info(
        "pre_movement.diversions_planned",
        movement_id=state["movement_id"],
        directives=len(result.get("directives", [])),
    )
    return {"diversion_plans": result}


async def _evaluate_scenarios(state: dict, config: RunnableConfig) -> dict:
    """Node: Scenario evaluation via ToolExecutor (traffic-oracle backend)."""
    agents = config["configurable"]["agents"]
    executor: ToolExecutor = agents["executor"]

    routes = state.get("route_candidates", {})
    primary = routes.get("primary_route")
    alternates = routes.get("alternate_routes", [])

    scenarios = []
    for route in [primary, *alternates]:
        if route is None:
            continue
        scenarios.append({
            "route_id": route.get("route_id", "unknown"),
            "segment_ids": route.get("segment_ids", []),
            "departure_time": state.get("planned_departure"),
        })

    if not scenarios:
        return {"scenario_result": {"scenarios": [], "best": None}}

    result = await executor.execute("evaluate_scenarios", {"scenarios": scenarios})

    logger.info(
        "pre_movement.scenarios_evaluated",
        movement_id=state["movement_id"],
        count=len(scenarios),
    )
    return {"scenario_result": result}


async def _finalize(state: dict, config: RunnableConfig) -> dict:
    """Node: Compose final recommendation and persist to context store."""
    ctx_store: ConvoyContextStore = config["configurable"]["context_store"]

    routes = state.get("route_candidates", {})
    primary = routes.get("primary_route")
    security = state.get("security_validation", {})
    diversions = state.get("diversion_plans", {})
    scenario = state.get("scenario_result", {})
    corridor = state.get("corridor_assessment", {})

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
    }

    # Persist approved plan to convoy context
    ctx = await ctx_store.get(state["movement_id"])
    if ctx is None:
        ctx = ConvoyContext(
            movement_id=state["movement_id"],
            vvip_class=state["vvip_class"],
        )
    ctx.status = recommendation["status"]
    if primary:
        ctx.selected_route_id = primary.get("route_id")
    ctx.decisions_log.append({
        "phase": "pre_movement",
        "recommendation": recommendation["status"],
        "security_compliant": recommendation["security_compliant"],
        "timestamp": time.time(),
    })
    await ctx_store.put(ctx)

    logger.info(
        "pre_movement.finalized",
        movement_id=state["movement_id"],
        status=recommendation["status"],
    )
    return {"final_recommendation": recommendation}


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_pre_movement_graph() -> StateGraph:
    """Construct the LangGraph StateGraph for pre-movement planning."""
    graph = StateGraph(PreMovementState)

    graph.add_node("assess_corridor", _assess_corridor)
    graph.add_node("plan_routes", _plan_routes)
    graph.add_node("validate_security", _validate_security)
    graph.add_node("reroute", _reroute_with_constraints)
    graph.add_node("plan_diversions", _plan_diversions)
    graph.add_node("evaluate_scenarios", _evaluate_scenarios)
    graph.add_node("finalize", _finalize)

    # Linear pipeline with conditional security loop
    graph.set_entry_point("assess_corridor")
    graph.add_edge("assess_corridor", "plan_routes")
    graph.add_edge("plan_routes", "validate_security")

    graph.add_conditional_edges(
        "validate_security",
        _security_router,
        {
            "plan_diversions": "plan_diversions",
            "reroute": "reroute",
        },
    )
    graph.add_edge("reroute", "plan_routes")
    graph.add_edge("plan_diversions", "evaluate_scenarios")
    graph.add_edge("evaluate_scenarios", "finalize")
    graph.add_edge("finalize", END)

    return graph


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_pre_movement_workflow(
    movement_id: str,
    origin: tuple[float, float],
    destination: tuple[float, float],
    vvip_class: str,
    planned_departure: str,
    *,
    bridge: OllamaBridge | None = None,
    executor: ToolExecutor | None = None,
    context_store: ConvoyContextStore | None = None,
) -> dict:
    """Execute the full pre-movement planning workflow.

    Args:
        movement_id: UUID of the planned movement
        origin: (lon, lat) convoy origin
        destination: (lon, lat) convoy destination
        vvip_class: Z+, Z, Y, or X
        planned_departure: ISO 8601 datetime string
        bridge: OllamaBridge instance (required)
        executor: ToolExecutor instance (required)
        context_store: ConvoyContextStore instance (required)

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

    traffic_analyst = TrafficAnalystAgent(bridge, executor)
    route_planner = RoutePlannerAgent(bridge, executor)
    security_liaison = SecurityLiaisonAgent(bridge, executor)
    diversion_coordinator = DiversionCoordinatorAgent(bridge, executor, context_store)

    graph = build_pre_movement_graph()
    app = graph.compile()

    initial_state = {
        "movement_id": movement_id,
        "origin": origin,
        "destination": destination,
        "vvip_class": vvip_class,
        "planned_departure": planned_departure,
        "avoid_segments": None,
        "security_retries": 0,
    }

    config = {
        "configurable": {
            "agents": {
                "traffic_analyst": traffic_analyst,
                "route_planner": route_planner,
                "security_liaison": security_liaison,
                "diversion_coordinator": diversion_coordinator,
                "executor": executor,
            },
            "context_store": context_store,
            "thread_id": f"pre_movement_{movement_id}",
        },
    }

    result = await app.ainvoke(initial_state, config=config)

    logger.info(
        "pre_movement_workflow.complete",
        movement_id=movement_id,
        status=result.get("final_recommendation", {}).get("status"),
    )
    return result.get("final_recommendation", {"status": "error", "error": "No recommendation produced"})


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
