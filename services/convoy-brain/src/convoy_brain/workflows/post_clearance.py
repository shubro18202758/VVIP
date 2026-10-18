"""Post-Event Restoration Workflow — restores normal traffic after the
corridor event releases the network.

Manages the orderly restoration of traffic flow:
    1. Deactivates remaining diversions in reverse order
    2. Monitors queue dissipation on affected segments (numeric loop)
    3. Generates post-movement report with actual vs planned metrics

Implemented as a plain async pipeline with the recovery monitoring loop
(previously a graph); the phase transition is an allowed decider-checkpoint
trigger, but the loop itself stays numeric-only.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from convoy_brain.adaptive_params import AdaptiveParamsClient
from convoy_brain.agents.diversion_coordinator import DiversionCoordinatorAgent
from convoy_brain.decider import DeciderBrain
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.memory.convoy_context import ConvoyContextStore

logger = structlog.get_logger(__name__)

# Congestion level below which a segment is considered "recovered"
RECOVERY_CONGESTION_THRESHOLD = 0.3

# Poll interval while waiting for queue dissipation
RECOVERY_POLL_INTERVAL_SEC = 5.0

# Maximum recovery iterations before declaring timeout (~10 min at 5s)
MAX_RECOVERY_ITERATIONS = 120


async def run_post_clearance_workflow(
    movement_id: str,
    *,
    bridge: LlamaBridge | None = None,
    executor: ToolExecutor | None = None,
    context_store: ConvoyContextStore | None = None,
    decider_brain: DeciderBrain | None = None,
    adaptive_client: AdaptiveParamsClient | None = None,
    max_recovery_iterations: int | None = None,
) -> dict:
    """Execute post-clearance traffic restoration.

    Deactivates remaining diversions, monitors queue dissipation on affected
    segments, and generates a post-movement report with actual vs planned
    metrics.

    Args:
        movement_id: UUID of the completed movement
        bridge: LlamaBridge instance (required)
        executor: ToolExecutor instance (required)
        context_store: ConvoyContextStore instance (required)
        decider_brain: strategic tier (optional; phase-transition checkpoint)
        adaptive_client: adaptive-parameter client (optional; unused numerically
            today but accepted for signature symmetry)
        max_recovery_iterations: test override for the recovery safety valve

    Returns:
        Post-movement report with disruption metrics and lessons learned
    """
    if bridge is None or executor is None or context_store is None:
        raise ValueError(
            "bridge, executor, and context_store are required. "
            "Pass them as keyword arguments."
        )

    logger.info("post_clearance_workflow.start", movement_id=movement_id)

    diversion_coordinator = DiversionCoordinatorAgent(bridge, executor, context_store)

    # -- Phase transition checkpoint (clear phase) --------------------------------
    if decider_brain is not None:
        state_doc = DeciderBrain.build_state_doc(
            trigger="phase_transition",
            summary=f"Convoy movement {movement_id} entering post-clearance restoration.",
            context={"phase": "clear"},
        )
        checkpoint = await decider_brain.checkpoint(
            movement_id=movement_id,
            trigger="phase_transition",
            state_doc=state_doc,
            questions={
                "restoration_posture": {
                    "type": "choice",
                    "instructions": (
                        "The convoy has passed. Choose the public-traffic "
                        "restoration posture."
                    ),
                    "options": [
                        "immediate_full_restoration",
                        "staged_reverse_order_restoration",
                        "signal_assisted_recovery",
                    ],
                },
            },
            force=True,
        )
        await context_store.append_decision(movement_id, checkpoint.audit)
        for alert in checkpoint.operator_alerts:
            await context_store.append_decision(
                movement_id, dict(alert, phase="post_clearance_alert")
            )

    # -- Deactivate diversions in reverse activation order -------------------------
    ctx = await context_store.get(movement_id)
    active_diversions = list(ctx.active_diversions) if ctx else []

    if not active_diversions:
        deactivated_segments: list[int] = []
        affected_segments: list[int] = []
        logger.info("post_clearance.no_active_diversions", movement_id=movement_id)
    else:
        reversed_segments = list(reversed(active_diversions))
        segment_ids = [int(s) for s in reversed_segments]
        result = await diversion_coordinator.deactivate_passed_segments(
            movement_id, segment_ids
        )
        deactivated_segments = result.get("deactivated", [])
        affected_segments = segment_ids
        logger.info(
            "post_clearance.diversions_deactivated",
            movement_id=movement_id,
            count=len(segment_ids),
            deactivated=len(deactivated_segments),
        )

    # -- Monitor recovery (numeric loop) ---------------------------------------------
    cap = (
        max_recovery_iterations if max_recovery_iterations is not None
        else MAX_RECOVERY_ITERATIONS
    )
    segments_recovered: list[int] = []
    segments_congested: list[int] = affected_segments
    recovery_iteration = 0

    while affected_segments and recovery_iteration < cap:
        recovery_iteration += 1
        live_traffic = await executor.execute("get_live_traffic", {
            "segment_ids": affected_segments,
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
            # Data unavailable — treat as recovered to avoid an infinite loop.
            recovered = affected_segments
            still_congested = []

        segments_recovered = recovered
        segments_congested = still_congested

        logger.info(
            "post_clearance.recovery_check",
            movement_id=movement_id,
            iteration=recovery_iteration,
            recovered=len(recovered),
            congested=len(still_congested),
        )

        if not still_congested:
            break
        await asyncio.sleep(RECOVERY_POLL_INTERVAL_SEC)
        affected_segments = [s for s in affected_segments if s in set(still_congested)]

    # -- Report ------------------------------------------------------------------------
    report = await _generate_report(
        movement_id=movement_id,
        ctx=await context_store.get(movement_id),
        executor=executor,
        affected=segments_congested or segments_recovered,
        deactivated_count=len(deactivated_segments),
        segments_recovered=segments_recovered,
        segments_congested=segments_congested,
        recovery_iteration=recovery_iteration,
    )

    ctx = await context_store.get(movement_id)
    if ctx:
        ctx.status = "completed"
        ctx.decisions_log.append({
            "phase": "post_clearance",
            "action": "clearance_complete",
            "recovery_iterations": recovery_iteration,
            "segments_recovered": len(segments_recovered),
            "segments_still_congested": len(segments_congested),
            "timestamp": time.time(),
        })
        await context_store.put(ctx)

    logger.info(
        "post_clearance_workflow.complete",
        movement_id=movement_id,
        status="completed",
    )
    return report


async def _generate_report(
    *,
    movement_id: str,
    ctx: Any,
    executor: ToolExecutor,
    affected: list[int],
    deactivated_count: int,
    segments_recovered: list[int],
    segments_congested: list[int],
    recovery_iteration: int,
) -> dict:
    decisions = ctx.decisions_log if ctx else []

    pre_movement_entry = next(
        (d for d in decisions if d.get("phase") == "pre_movement"), None
    )
    escort_entry = next(
        (d for d in decisions if d.get("phase") == "live_escort"), None
    )

    planning_timestamp = pre_movement_entry.get("timestamp") if pre_movement_entry else None
    escort_timestamp = escort_entry.get("timestamp") if escort_entry else None

    historical_data: dict = {}
    if affected:
        try:
            historical_data = await executor.execute("get_historical_pattern", {
                "segment_ids": affected[:20],  # Cap to avoid oversized queries
            })
        except Exception:
            logger.warning("post_clearance.historical_query_failed", movement_id=movement_id)

    return {
        "movement_id": movement_id,
        "vvip_class": ctx.vvip_class if ctx else "unknown",
        "selected_route_id": ctx.selected_route_id if ctx else None,
        "total_affected_segments": len(affected),
        "segments_recovered": len(segments_recovered),
        "segments_still_congested": len(segments_congested),
        "recovery_iterations": recovery_iteration,
        "recovery_time_sec": recovery_iteration * RECOVERY_POLL_INTERVAL_SEC,
        "diversions_deactivated": deactivated_count,
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
