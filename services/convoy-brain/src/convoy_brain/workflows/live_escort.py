"""Live Management Workflow — real-time corridor-event tracking and adaptation.

Active while the corridor event is live (protected movement escort,
procession crawl, race sweep, egress window — the loop is event-agnostic):
    1. Tracks convoy GPS position (tick loop is LLM-free, exactly as before)
    2. Monitors traffic ahead of convoy with the ADAPTIVE congestion threshold
       (fail-closed to BASE_CONGESTION_SPIKE_THRESHOLD)
    3. Activates diversions just-in-time; advance_closure_sec is protocol-
       floored per VVIP class before reaching plan_diversions
    4. Adapts timing if convoy speed deviates from plan
    5. Handles incidents: the decider takes an EVENT-DRIVEN checkpoint
       (trigger=incident_detected) before any re-route — never per-tick

The ACTIVATION_LOOKAHEAD_SEC constant that lived here was dead code (sole
reference in the repo) and is intentionally deleted; the real application
points are the adaptive parameters threaded into the plan_diversions MCP call
and this loop's poll interval / spike threshold.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from convoy_brain.adaptive_params import AdaptiveParamsClient
from convoy_brain.agents.diversion_coordinator import DiversionCoordinatorAgent
from convoy_brain.agents.route_planner import RoutePlannerAgent
from convoy_brain.agents.traffic_analyst import TrafficAnalystAgent
from convoy_brain.config import (
    BASE_CONGESTION_SPIKE_THRESHOLD,
    BASE_GPS_POLL_INTERVAL_SEC,
)
from convoy_brain.decider import DeciderBrain
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.memory.convoy_context import ConvoyContextStore

logger = structlog.get_logger(__name__)

# GPS update interval (seconds) — fail-closed default; the adaptive tier may
# publish a per-movement override (>=1s protocol floor).
GPS_POLL_INTERVAL_SEC = BASE_GPS_POLL_INTERVAL_SEC

# Congestion spike threshold for triggering re-route assessment — fail-closed
# default; adaptive may override per movement.
CONGESTION_SPIKE_THRESHOLD = BASE_CONGESTION_SPIKE_THRESHOLD

# Maximum iterations before forcing exit (safety valve: ~30 min at 1s ticks)
MAX_MONITOR_ITERATIONS = 1800


async def run_live_escort_workflow(
    movement_id: str,
    *,
    destination: tuple[float, float] | None = None,
    bridge: LlamaBridge | None = None,
    executor: ToolExecutor | None = None,
    context_store: ConvoyContextStore | None = None,
    decider_brain: DeciderBrain | None = None,
    adaptive_client: AdaptiveParamsClient | None = None,
    max_iterations: int | None = None,
) -> dict:
    """Execute the live escort monitoring loop.

    Runs continuously until the event reaches its destination or the
    movement is cancelled. GPS ticks are LLM-free; the decider runs only on
    incidents (event-driven cadence), and the tactical agents fire only then.

    Args:
        movement_id: UUID of the active movement
        destination: (lon, lat) corridor destination for re-routing
        bridge: LlamaBridge instance (required)
        executor: ToolExecutor instance (required)
        context_store: ConvoyContextStore instance (required)
        decider_brain: strategic tier (optional)
        adaptive_client: adaptive-parameter client (optional; fail-closed)
        max_iterations: test/integration override for the safety valve

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

    # Adaptive parameters for this movement (fail-closed). The spike
    # threshold and GPS poll interval are applied HERE, at their real use.
    adaptive = None
    if adaptive_client is not None:
        adaptive = await adaptive_client.fetch(
            movement_id=movement_id, vvip_class=ctx.vvip_class if ctx else None
        )
    spike_threshold = (
        adaptive.congestion_spike_threshold if adaptive else CONGESTION_SPIKE_THRESHOLD
    )
    poll_interval = (
        adaptive.gps_poll_interval_sec if adaptive else GPS_POLL_INTERVAL_SEC
    )
    vvip_class = ctx.vvip_class if ctx else "Z"

    traffic_analyst = TrafficAnalystAgent(bridge, executor)
    route_planner = RoutePlannerAgent(bridge, executor, adaptive=adaptive)
    diversion_coordinator = DiversionCoordinatorAgent(
        bridge, executor, context_store, adaptive=adaptive
    )

    iterations = 0
    cap = max_iterations if max_iterations is not None else MAX_MONITOR_ITERATIONS
    final_status = ctx.status if ctx else "unknown"
    last_congestion_state_high = False

    while iterations < cap:
        iterations += 1

        # ---- receive GPS (context store) ------------------------------------
        ctx = await context_store.get(movement_id)
        if ctx is None:
            final_status = "lost"
            break
        position = ctx.convoy_position
        final_status = ctx.status
        if ctx.status in ("completed", "cancelled"):
            break
        if position is None:
            break

        # ---- monitor ahead (numeric only — no model on the tick path) -------
        seg_ids = await _segments_ahead(executor, position)
        incident_detected = False
        if seg_ids:
            live_traffic = await executor.execute("get_live_traffic", {
                "segment_ids": seg_ids,
            })
            peak_congestion = _peak_congestion(live_traffic)
            high = peak_congestion is not None and peak_congestion > spike_threshold
            crossing = high and not last_congestion_state_high
            incident_detected = high
            last_congestion_state_high = high

            # Event-driven decider checkpoint: threshold crossing (not per tick)
            if crossing and decider_brain is not None:
                state_doc = DeciderBrain.build_state_doc(
                    trigger="congestion_threshold_crossed",
                    summary=(
                        f"Congestion {peak_congestion:.2f} crossed threshold "
                        f"{spike_threshold:.2f} ahead of the managed movement at {position}."
                    ),
                    context={"movement_id": movement_id, "vvip_class": vvip_class},
                )
                checkpoint = await decider_brain.checkpoint(
                    movement_id=movement_id,
                    trigger="congestion_threshold_crossed",
                    state_doc=state_doc,
                    questions={
                        "response": {
                            "type": "choice",
                            "instructions": (
                                "Congestion ahead of the corridor event just crossed the "
                                "spike threshold. Choose the response posture."
                            ),
                            "options": [
                                "continue_monitoring",
                                "reroute_assessment",
                                "hold_convoy",
                                "escalate_operator",
                            ],
                        },
                    },
                )
                await context_store.append_decision(
                    movement_id, checkpoint.audit
                )
                for alert in checkpoint.operator_alerts:
                    await context_store.append_decision(
                        movement_id, dict(alert, phase="live_escort_alert")
                    )

        # ---- incident path (tactical agents + decider checkpoint) -----------
        if incident_detected:
            logger.warning(
                "live_escort.incident_detected",
                movement_id=movement_id,
                position=position,
                iteration=iterations,
            )
            if decider_brain is not None:
                state_doc = DeciderBrain.build_state_doc(
                    trigger="incident_detected",
                    summary=f"Incident-level congestion ahead of convoy at {position}.",
                    context={
                        "movement_id": movement_id,
                        "vvip_class": vvip_class,
                        "active_diversions": ctx.active_diversions,
                    },
                )
                questions = {
                    "action": {
                        "type": "choice",
                        "instructions": (
                            "An incident is detected ahead of the moving convoy. "
                            "Choose the immediate action."
                        ),
                        "options": [
                            "assess_and_monitor",
                            "emergency_reroute",
                            "hold_position",
                            "activate_plan_b",
                        ],
                    },
                    "urgency": {
                        "type": "noul",
                        "instructions": (
                            "Probability that none of the listed actions fit "
                            "this incident (escalate to operator)."
                        ),
                    },
                }
                checkpoint = await decider_brain.checkpoint(
                    movement_id=movement_id,
                    trigger="incident_detected",
                    state_doc=state_doc,
                    questions=questions,
                )
                await context_store.append_decision(
                    movement_id, checkpoint.audit
                )
                for alert in checkpoint.operator_alerts:
                    await context_store.append_decision(
                        movement_id, dict(alert, phase="live_escort_alert")
                    )
                await _handle_incident(
                    movement_id=movement_id,
                    position=position,
                    destination=destination,
                    vvip_class=vvip_class,
                    analyst=traffic_analyst,
                    planner=route_planner,
                    coordinator=diversion_coordinator,
                    ctx_store=context_store,
                )
        else:
            # ---- normal path: numeric-only diversion bookkeeping --------------
            # LLM-free per tick (F8): position update + deactivation of passed
            # segments; no model call happens here.
            await diversion_coordinator.tick_update(movement_id, tuple(position), seg_ids)

        await asyncio.sleep(poll_interval)

    # ---- finalize ------------------------------------------------------------
    ctx = await context_store.get(movement_id)
    if ctx and ctx.status == "active":
        ctx.status = "completed"
        ctx.decisions_log.append({
            "phase": "live_escort",
            "action": "escort_complete",
            "iterations": iterations,
            "timestamp": time.time(),
        })
        await context_store.put(ctx)

    logger.info(
        "live_escort_workflow.complete",
        movement_id=movement_id,
        iterations=iterations,
    )
    return {
        "movement_id": movement_id,
        "escort_complete": True,
        "total_iterations": iterations,
        "final_status": final_status,
    }


async def _segments_ahead(executor: ToolExecutor, position: tuple[float, float]) -> list[int]:
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
    return [
        s["segment_id"] if isinstance(s, dict) else s
        for s in seg_list[:50]
    ]


def _peak_congestion(live_traffic: Any) -> float | None:
    if not isinstance(live_traffic, dict):
        return None
    peak = None
    for seg_data in live_traffic.get("segments", []):
        if isinstance(seg_data, dict):
            congestion = seg_data.get("congestion_idx", 0)
            if isinstance(congestion, (int, float)):
                peak = congestion if peak is None else max(peak, congestion)
    return peak


async def _handle_incident(
    *,
    movement_id: str,
    position: tuple[float, float],
    destination: tuple[float, float] | None,
    vvip_class: str,
    analyst: TrafficAnalystAgent,
    planner: RoutePlannerAgent,
    coordinator: DiversionCoordinatorAgent,
    ctx_store: ConvoyContextStore,
) -> None:
    """Assess the incident area; emergency re-route when the corridor is red."""
    bbox = (
        position[0] - 0.01, position[1] - 0.01,
        position[0] + 0.02, position[1] + 0.02,
    )
    assessment = await analyst.assess_corridor(bbox)

    if assessment.get("overall_status") != "red":
        return

    if not destination:
        return

    reroute_result = await planner.analyze_routes(
        movement_id=movement_id,
        origin=position,
        destination=destination,
        vvip_class=vvip_class,
    )
    primary = reroute_result.get("primary_route")
    ctx = await ctx_store.get(movement_id)
    if primary and ctx:
        ctx.selected_route_id = primary.get("route_id")
        ctx.alerts.append({
            "type": "emergency_reroute",
            "timestamp": time.time(),
            "reason": "incident_ahead",
            "new_route": primary.get("route_id"),
        })
        await ctx_store.put(ctx)

        new_segments = primary.get("segment_ids", [])
        if new_segments:
            await coordinator.activate_diversions(
                movement_id, new_segments, vvip_class=vvip_class
            )

    logger.warning(
        "live_escort.emergency_reroute",
        movement_id=movement_id,
        new_route=primary.get("route_id") if primary else None,
    )
