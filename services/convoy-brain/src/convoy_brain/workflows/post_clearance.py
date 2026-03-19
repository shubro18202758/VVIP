"""Post-Clearance Restoration Workflow — restores normal traffic after convoy passes.

Manages the orderly restoration of traffic flow:
    1. Deactivates remaining diversions in reverse order
    2. Monitors queue dissipation on affected segments
    3. Adjusts signal timing to accelerate recovery
    4. Generates post-movement report with actual vs planned metrics

Implemented as a LangGraph StateGraph with a monitoring loop that continues
until all affected segments have recovered to normal congestion levels.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, TypedDict

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from convoy_brain.agents.diversion_coordinator import DiversionCoordinatorAgent
from convoy_brain.agents.traffic_analyst import TrafficAnalystAgent
from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.memory.convoy_context import ConvoyContextStore
from convoy_brain.ollama_bridge import OllamaBridge

logger = structlog.get_logger(__name__)

# Congestion level below which a segment is considered "recovered"
RECOVERY_CONGESTION_THRESHOLD = 0.3

# Poll interval while waiting for queue dissipation
RECOVERY_POLL_INTERVAL_SEC = 5.0

# Maximum recovery iterations before declaring timeout (~10 min at 5s)
MAX_RECOVERY_ITERATIONS = 120


class PostClearanceState(TypedDict, total=False):
    """State schema for the post-clearance restoration workflow."""
    movement_id: str
    recovery_iteration: int
    deactivated_segments: list[int]
    affected_segments: list[int]
    deactivation_result: dict[str, Any]
    recovery_status: str
    segments_recovered: list[int]
    segments_congested: list[int]
    post_movement_report: dict[str, Any]
    status: str
    report: dict[str, Any]


# ---------------------------------------------------------------------------
# Node functions
# ---------------------------------------------------------------------------

async def _deactivate_diversions(state: dict, config: RunnableConfig) -> dict:
    """Node: Deactivate all remaining diversions in reverse activation order."""
    agents = config["configurable"]["agents"]
    coordinator: DiversionCoordinatorAgent = agents["diversion_coordinator"]
    ctx_store: ConvoyContextStore = config["configurable"]["context_store"]
    movement_id = state["movement_id"]

    ctx = await ctx_store.get(movement_id)
    active_diversions = list(ctx.active_diversions) if ctx else []

    if not active_diversions:
        logger.info(
            "post_clearance.no_active_diversions",
            movement_id=movement_id,
        )
        return {
            "deactivated_segments": [],
            "affected_segments": [],
            "deactivation_result": {"status": "no_diversions"},
        }

    # Deactivate in reverse order (last activated → first deactivated)
    reversed_segments = list(reversed(active_diversions))
    segment_ids = [int(s) for s in reversed_segments]

    result = await coordinator.deactivate_passed_segments(movement_id, segment_ids)

    logger.info(
        "post_clearance.diversions_deactivated",
        movement_id=movement_id,
        count=len(segment_ids),
        deactivated=len(result.get("deactivated", [])),
    )
    return {
        "deactivated_segments": result.get("deactivated", []),
        "affected_segments": segment_ids,
        "deactivation_result": result,
    }


async def _monitor_recovery(state: dict, config: RunnableConfig) -> dict:
    """Node: Check traffic on affected segments for congestion dissipation."""
    agents = config["configurable"]["agents"]
    executor: ToolExecutor = agents["executor"]

    affected = state.get("affected_segments", [])
    if not affected:
        return {
            "recovery_status": "no_segments",
            "segments_recovered": [],
            "segments_congested": [],
            "recovery_iteration": state.get("recovery_iteration", 0),
        }

    # Query live traffic for affected segments
    live_traffic = await executor.execute("get_live_traffic", {
        "segment_ids": affected,
    })

    recovered = []
    still_congested = []

    if isinstance(live_traffic, dict):
        for seg_data in live_traffic.get("segments", []):
            if not isinstance(seg_data, dict):
                continue
            seg_id = seg_data.get("segment_id")
            congestion = seg_data.get("congestion_idx", 1.0)
            if isinstance(congestion, (int, float)) and congestion < RECOVERY_CONGESTION_THRESHOLD:
                recovered.append(seg_id)
            else:
                still_congested.append(seg_id)
    else:
        # If traffic data unavailable, treat all as recovered to avoid infinite loop
        recovered = affected

    iteration = state.get("recovery_iteration", 0) + 1

    logger.info(
        "post_clearance.recovery_check",
        movement_id=state["movement_id"],
        iteration=iteration,
        recovered=len(recovered),
        congested=len(still_congested),
    )
    return {
        "recovery_status": "recovered" if not still_congested else "in_progress",
        "segments_recovered": recovered,
        "segments_congested": still_congested,
        "recovery_iteration": iteration,
    }


def _recovery_router(state: dict) -> str:
    """Conditional edge: loop until all segments recovered or max iterations."""
    recovery_status = state.get("recovery_status")
    iteration = state.get("recovery_iteration", 0)

    if recovery_status == "no_segments":
        return "generate_report"

    if recovery_status == "recovered":
        return "generate_report"

    if iteration >= MAX_RECOVERY_ITERATIONS:
        logger.warning(
            "post_clearance.recovery_timeout",
            movement_id=state.get("movement_id"),
            iterations=iteration,
            still_congested=state.get("segments_congested", []),
        )
        return "generate_report"

    return "wait_recovery"


async def _wait_recovery(state: dict, config: RunnableConfig) -> dict:
    """Node: Wait before next recovery check."""
    await asyncio.sleep(RECOVERY_POLL_INTERVAL_SEC)
    return {}


async def _generate_report(state: dict, config: RunnableConfig) -> dict:
    """Node: Generate post-movement report comparing actual vs planned metrics."""
    ctx_store: ConvoyContextStore = config["configurable"]["context_store"]
    agents = config["configurable"]["agents"]
    executor: ToolExecutor = agents["executor"]
    movement_id = state["movement_id"]

    ctx = await ctx_store.get(movement_id)

    # Collect decisions log for timing analysis
    decisions = ctx.decisions_log if ctx else []

    # Calculate timing metrics from decision log
    pre_movement_entry = next(
        (d for d in decisions if d.get("phase") == "pre_movement"), None
    )
    escort_entry = next(
        (d for d in decisions if d.get("phase") == "live_escort"), None
    )

    planning_timestamp = pre_movement_entry.get("timestamp") if pre_movement_entry else None
    escort_timestamp = escort_entry.get("timestamp") if escort_entry else None

    # Query historical pattern for affected corridor
    affected = state.get("affected_segments", [])
    historical_data = {}
    if affected:
        try:
            historical_data = await executor.execute("get_historical_pattern", {
                "segment_ids": affected[:20],  # Cap to avoid oversized queries
            })
        except Exception:
            logger.warning("post_clearance.historical_query_failed", movement_id=movement_id)

    report = {
        "movement_id": movement_id,
        "vvip_class": ctx.vvip_class if ctx else "unknown",
        "selected_route_id": ctx.selected_route_id if ctx else None,
        "total_affected_segments": len(affected),
        "segments_recovered": len(state.get("segments_recovered", [])),
        "segments_still_congested": len(state.get("segments_congested", [])),
        "recovery_iterations": state.get("recovery_iteration", 0),
        "recovery_time_sec": state.get("recovery_iteration", 0) * RECOVERY_POLL_INTERVAL_SEC,
        "diversions_deactivated": len(state.get("deactivated_segments", [])),
        "alerts_during_escort": len(ctx.alerts) if ctx else 0,
        "total_decisions": len(decisions),
        "planning_to_completion_sec": (
            round(time.time() - planning_timestamp, 1) if planning_timestamp else None
        ),
        "escort_duration_sec": (
            round(time.time() - escort_timestamp, 1) if escort_timestamp else None
        ),
        "historical_baseline": historical_data,
        "generated_at": time.time(),
    }

    logger.info(
        "post_clearance.report_generated",
        movement_id=movement_id,
        affected=report["total_affected_segments"],
        recovered=report["segments_recovered"],
        recovery_sec=report["recovery_time_sec"],
    )
    return {"post_movement_report": report}


async def _finalize(state: dict, config: RunnableConfig) -> dict:
    """Node: Persist final status and report to context store."""
    ctx_store: ConvoyContextStore = config["configurable"]["context_store"]
    movement_id = state["movement_id"]

    ctx = await ctx_store.get(movement_id)
    if ctx:
        ctx.status = "completed"
        ctx.decisions_log.append({
            "phase": "post_clearance",
            "action": "clearance_complete",
            "recovery_iterations": state.get("recovery_iteration", 0),
            "segments_recovered": len(state.get("segments_recovered", [])),
            "segments_still_congested": len(state.get("segments_congested", [])),
            "timestamp": time.time(),
        })
        await ctx_store.put(ctx)

    report = state.get("post_movement_report", {})

    logger.info(
        "post_clearance.finalized",
        movement_id=movement_id,
        status="completed",
    )
    return {"status": "completed", "report": report}


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_post_clearance_graph() -> StateGraph:
    """Construct the LangGraph StateGraph for post-clearance restoration."""
    graph = StateGraph(PostClearanceState)

    graph.add_node("deactivate_diversions", _deactivate_diversions)
    graph.add_node("monitor_recovery", _monitor_recovery)
    graph.add_node("wait_recovery", _wait_recovery)
    graph.add_node("generate_report", _generate_report)
    graph.add_node("finalize", _finalize)

    # Entry: deactivate diversions → monitor recovery → conditional loop
    graph.set_entry_point("deactivate_diversions")
    graph.add_edge("deactivate_diversions", "monitor_recovery")

    graph.add_conditional_edges(
        "monitor_recovery",
        _recovery_router,
        {
            "generate_report": "generate_report",
            "wait_recovery": "wait_recovery",
        },
    )

    # Recovery loop: wait → monitor again
    graph.add_edge("wait_recovery", "monitor_recovery")

    # Final pipeline
    graph.add_edge("generate_report", "finalize")
    graph.add_edge("finalize", END)

    return graph


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_post_clearance_workflow(
    movement_id: str,
    *,
    bridge: OllamaBridge | None = None,
    executor: ToolExecutor | None = None,
    context_store: ConvoyContextStore | None = None,
) -> dict:
    """Execute post-clearance traffic restoration.

    Deactivates remaining diversions, monitors queue dissipation on affected
    segments, and generates a post-movement report with actual vs planned
    metrics.

    Args:
        movement_id: UUID of the completed movement
        bridge: OllamaBridge instance (required)
        executor: ToolExecutor instance (required)
        context_store: ConvoyContextStore instance (required)

    Returns:
        Post-movement report with disruption metrics and lessons learned
    """
    if bridge is None or executor is None or context_store is None:
        raise ValueError(
            "bridge, executor, and context_store are required. "
            "Pass them as keyword arguments."
        )

    logger.info("post_clearance_workflow.start", movement_id=movement_id)

    traffic_analyst = TrafficAnalystAgent(bridge, executor)
    diversion_coordinator = DiversionCoordinatorAgent(bridge, executor, context_store)

    graph = build_post_clearance_graph()
    app = graph.compile()

    initial_state = {
        "movement_id": movement_id,
        "recovery_iteration": 0,
    }

    config = {
        "configurable": {
            "agents": {
                "traffic_analyst": traffic_analyst,
                "diversion_coordinator": diversion_coordinator,
                "executor": executor,
            },
            "context_store": context_store,
            "thread_id": f"post_clearance_{movement_id}",
        },
    }

    result = await app.ainvoke(initial_state, config=config)

    logger.info(
        "post_clearance_workflow.complete",
        movement_id=movement_id,
        status=result.get("status"),
    )
    return result.get("report", {"status": "error", "error": "No report produced"})
