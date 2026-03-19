"""Live Escort Monitoring Workflow — real-time convoy tracking and adaptation.

Active during convoy movement. Continuously:
    1. Tracks convoy GPS position
    2. Monitors traffic ahead of convoy
    3. Activates diversions just-in-time as convoy approaches each segment
    4. Adapts timing if convoy speed deviates from plan
    5. Handles incidents and re-routes if necessary

Implemented as a LangGraph StateGraph with a cyclic monitoring loop.
The graph re-enters the monitor node on each GPS tick until the convoy
reaches its destination or the movement is cancelled.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, TypedDict

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from convoy_brain.agents.diversion_coordinator import DiversionCoordinatorAgent
from convoy_brain.agents.route_planner import RoutePlannerAgent
from convoy_brain.agents.traffic_analyst import TrafficAnalystAgent
from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.memory.convoy_context import ConvoyContextStore
from convoy_brain.ollama_bridge import OllamaBridge

logger = structlog.get_logger(__name__)

# GPS update interval (seconds)
GPS_POLL_INTERVAL_SEC = 1.0

# Diversion activation threshold: activate when convoy is within this many
# seconds of a segment (per Indian VVIP protocol, max 180s for Z+)
ACTIVATION_LOOKAHEAD_SEC = 120

# Congestion spike threshold for triggering re-route assessment
CONGESTION_SPIKE_THRESHOLD = 0.8

# Maximum iterations before forcing exit (safety valve: ~30 min at 1s ticks)
MAX_MONITOR_ITERATIONS = 1800


class LiveEscortState(TypedDict, total=False):
    """State schema for the live escort monitoring workflow."""
    movement_id: str
    destination: tuple[float, float] | None
    iteration: int
    convoy_position: list[float] | None
    convoy_speed_kmh: float
    movement_status: str
    active_diversions: list[str]
    traffic_ahead: dict[str, Any]
    incident_detected: bool
    segments_ahead: list[int]
    diversion_action: str
    timing_adaptation: dict[str, Any]
    deactivation: dict[str, Any]
    reroute_result: dict[str, Any]
    escort_complete: bool
    total_iterations: int
    final_status: str


# ---------------------------------------------------------------------------
# Node functions
# ---------------------------------------------------------------------------

async def _receive_gps(state: dict, config: RunnableConfig) -> dict:
    """Node: Receive latest convoy GPS position from context store."""
    ctx_store: ConvoyContextStore = config["configurable"]["context_store"]
    movement_id = state["movement_id"]

    ctx = await ctx_store.get(movement_id)
    if ctx is None:
        return {"convoy_position": None, "convoy_speed_kmh": 0.0, "movement_status": "lost"}

    return {
        "convoy_position": ctx.convoy_position,
        "convoy_speed_kmh": ctx.convoy_speed_kmh,
        "active_diversions": ctx.active_diversions,
        "movement_status": ctx.status,
    }


async def _monitor_ahead(state: dict, config: RunnableConfig) -> dict:
    """Node: Check traffic conditions ahead of convoy and detect issues."""
    agents = config["configurable"]["agents"]
    executor: ToolExecutor = agents["executor"]

    position = state.get("convoy_position")
    if position is None:
        return {"traffic_ahead": {}, "incident_detected": False}

    # Query segments ahead of convoy (wider box in direction of travel)
    ahead_segments = await executor.execute("query_segments_in_bbox", {
        "min_lon": position[0] - 0.005,
        "min_lat": position[1] - 0.005,
        "max_lon": position[0] + 0.015,
        "max_lat": position[1] + 0.015,
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

    if not seg_ids:
        return {"traffic_ahead": {}, "incident_detected": False, "segments_ahead": []}

    # Fetch live traffic for segments ahead
    live_traffic = await executor.execute("get_live_traffic", {
        "segment_ids": seg_ids,
    })

    # Check for congestion spikes
    incident_detected = False
    if isinstance(live_traffic, dict):
        for seg_data in live_traffic.get("segments", []):
            if isinstance(seg_data, dict):
                congestion = seg_data.get("congestion_idx", 0)
                if isinstance(congestion, (int, float)) and congestion > CONGESTION_SPIKE_THRESHOLD:
                    incident_detected = True
                    break

    return {
        "traffic_ahead": live_traffic,
        "incident_detected": incident_detected,
        "segments_ahead": seg_ids,
    }


def _escort_router(state: dict) -> str:
    """Conditional edge: decide next action based on monitoring results."""
    movement_status = state.get("movement_status")
    iteration = state.get("iteration", 0)

    # Terminal conditions
    if movement_status in ("completed", "cancelled"):
        return "finalize_escort"
    if iteration >= MAX_MONITOR_ITERATIONS:
        return "finalize_escort"
    if state.get("convoy_position") is None:
        return "finalize_escort"

    # Incident → re-route
    if state.get("incident_detected", False):
        return "handle_incident"

    # Normal → manage diversions
    return "manage_diversions"


async def _manage_diversions(state: dict, config: RunnableConfig) -> dict:
    """Node: Activate/deactivate diversions based on convoy position."""
    agents = config["configurable"]["agents"]
    coordinator: DiversionCoordinatorAgent = agents["diversion_coordinator"]
    movement_id = state["movement_id"]
    position = state.get("convoy_position")

    if position is None:
        return {"diversion_action": "no_position"}

    # Adapt diversion timing based on current position
    timing_result = await coordinator.adapt_timing(movement_id, tuple(position))

    # Deactivate diversions on segments the convoy has passed
    active = state.get("active_diversions", [])
    segments_ahead = set(str(s) for s in state.get("segments_ahead", []))
    passed = [s for s in active if s not in segments_ahead]

    deactivation_result = {}
    if passed:
        deactivation_result = await coordinator.deactivate_passed_segments(
            movement_id, [int(s) for s in passed],
        )

    logger.info(
        "live_escort.diversions_managed",
        movement_id=movement_id,
        adapted=len(timing_result.get("directives", [])),
        deactivated=len(deactivation_result.get("deactivated", [])),
    )
    return {
        "diversion_action": "managed",
        "timing_adaptation": timing_result,
        "deactivation": deactivation_result,
    }


async def _handle_incident(state: dict, config: RunnableConfig) -> dict:
    """Node: Handle detected incident by considering emergency re-route."""
    agents = config["configurable"]["agents"]
    analyst: TrafficAnalystAgent = agents["traffic_analyst"]
    planner: RoutePlannerAgent = agents["route_planner"]
    coordinator: DiversionCoordinatorAgent = agents["diversion_coordinator"]
    ctx_store: ConvoyContextStore = config["configurable"]["context_store"]

    movement_id = state["movement_id"]
    position = state.get("convoy_position")

    logger.warning(
        "live_escort.incident_detected",
        movement_id=movement_id,
        position=position,
    )

    ctx = await ctx_store.get(movement_id)
    vvip_class = ctx.vvip_class if ctx else "Z"

    # Assess whether re-route is needed
    if position:
        bbox = (
            position[0] - 0.01, position[1] - 0.01,
            position[0] + 0.02, position[1] + 0.02,
        )
        assessment = await analyst.assess_corridor(bbox)

        if assessment.get("overall_status") == "red":
            # Emergency re-route from current position to destination
            destination = state.get("destination")
            if destination:
                reroute_result = await planner.analyze_routes(
                    movement_id=movement_id,
                    origin=tuple(position),
                    destination=destination,
                    vvip_class=vvip_class,
                )

                # Update context with new route
                primary = reroute_result.get("primary_route")
                if primary and ctx:
                    ctx.selected_route_id = primary.get("route_id")
                    ctx.alerts.append({
                        "type": "emergency_reroute",
                        "timestamp": time.time(),
                        "reason": "incident_ahead",
                        "new_route": primary.get("route_id"),
                    })
                    await ctx_store.put(ctx)

                    # Activate diversions for new route
                    new_segments = primary.get("segment_ids", [])
                    if new_segments:
                        await coordinator.activate_diversions(movement_id, new_segments)

                logger.warning(
                    "live_escort.emergency_reroute",
                    movement_id=movement_id,
                    new_route=primary.get("route_id") if primary else None,
                )
                return {"diversion_action": "rerouted", "reroute_result": reroute_result}

    return {"diversion_action": "incident_monitored"}


async def _tick_wait(state: dict, config: RunnableConfig) -> dict:
    """Node: Wait for next GPS poll interval before looping back."""
    await asyncio.sleep(GPS_POLL_INTERVAL_SEC)
    return {"iteration": state.get("iteration", 0) + 1}


async def _finalize_escort(state: dict, config: RunnableConfig) -> dict:
    """Node: Mark escort as complete, log final state."""
    ctx_store: ConvoyContextStore = config["configurable"]["context_store"]
    movement_id = state["movement_id"]

    ctx = await ctx_store.get(movement_id)
    if ctx and ctx.status == "active":
        ctx.status = "completed"
        ctx.decisions_log.append({
            "phase": "live_escort",
            "action": "escort_complete",
            "iterations": state.get("iteration", 0),
            "timestamp": time.time(),
        })
        await ctx_store.put(ctx)

    logger.info(
        "live_escort.finalized",
        movement_id=movement_id,
        iterations=state.get("iteration", 0),
        final_status=state.get("movement_status"),
    )
    return {
        "escort_complete": True,
        "total_iterations": state.get("iteration", 0),
        "final_status": state.get("movement_status"),
    }


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_live_escort_graph() -> StateGraph:
    """Construct the LangGraph StateGraph for live escort monitoring."""
    graph = StateGraph(LiveEscortState)

    graph.add_node("receive_gps", _receive_gps)
    graph.add_node("monitor_ahead", _monitor_ahead)
    graph.add_node("manage_diversions", _manage_diversions)
    graph.add_node("handle_incident", _handle_incident)
    graph.add_node("tick_wait", _tick_wait)
    graph.add_node("finalize_escort", _finalize_escort)

    # Entry: receive GPS → monitor → conditional
    graph.set_entry_point("receive_gps")
    graph.add_edge("receive_gps", "monitor_ahead")

    graph.add_conditional_edges(
        "monitor_ahead",
        _escort_router,
        {
            "manage_diversions": "manage_diversions",
            "handle_incident": "handle_incident",
            "finalize_escort": "finalize_escort",
        },
    )

    # After managing diversions or incident → tick → loop back
    graph.add_edge("manage_diversions", "tick_wait")
    graph.add_edge("handle_incident", "tick_wait")
    graph.add_edge("tick_wait", "receive_gps")

    graph.add_edge("finalize_escort", END)

    return graph


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_live_escort_workflow(
    movement_id: str,
    *,
    destination: tuple[float, float] | None = None,
    bridge: OllamaBridge | None = None,
    executor: ToolExecutor | None = None,
    context_store: ConvoyContextStore | None = None,
) -> dict:
    """Execute the live escort monitoring loop.

    Runs continuously until the convoy reaches its destination or
    the movement is cancelled.

    This workflow is the most VRAM-sensitive phase because it requires
    concurrent access to:
        - Ollama (Qwen) for agent reasoning
        - ONNX flow forecaster for short-horizon predictions
        - Valkey for real-time state updates

    The GpuArbiter manages VRAM allocation during this phase.

    Args:
        movement_id: UUID of the active movement
        destination: (lon, lat) convoy destination for re-routing
        bridge: OllamaBridge instance (required)
        executor: ToolExecutor instance (required)
        context_store: ConvoyContextStore instance (required)

    Returns:
        Escort completion report with iteration count and final status
    """
    if bridge is None or executor is None or context_store is None:
        raise ValueError(
            "bridge, executor, and context_store are required. "
            "Pass them as keyword arguments."
        )

    logger.info("live_escort_workflow.start", movement_id=movement_id)

    # Mark movement as active
    ctx = await context_store.get(movement_id)
    if ctx:
        ctx.status = "active"
        await context_store.put(ctx)

    traffic_analyst = TrafficAnalystAgent(bridge, executor)
    route_planner = RoutePlannerAgent(bridge, executor)
    diversion_coordinator = DiversionCoordinatorAgent(bridge, executor, context_store)

    graph = build_live_escort_graph()
    app = graph.compile()

    initial_state = {
        "movement_id": movement_id,
        "destination": destination,
        "iteration": 0,
    }

    config = {
        "configurable": {
            "agents": {
                "traffic_analyst": traffic_analyst,
                "route_planner": route_planner,
                "diversion_coordinator": diversion_coordinator,
                "executor": executor,
            },
            "context_store": context_store,
            "thread_id": f"live_escort_{movement_id}",
        },
    }

    result = await app.ainvoke(initial_state, config=config)

    logger.info(
        "live_escort_workflow.complete",
        movement_id=movement_id,
        iterations=result.get("total_iterations"),
    )
    return {
        "movement_id": movement_id,
        "escort_complete": result.get("escort_complete", False),
        "total_iterations": result.get("total_iterations", 0),
        "final_status": result.get("final_status"),
    }
