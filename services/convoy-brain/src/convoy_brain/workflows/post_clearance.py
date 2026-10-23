"""Post-Event Restoration Workflow — restores normal traffic after the
corridor event releases the network.

Manages the orderly restoration of traffic flow:
    1. Deactivates remaining diversions in ONE step, in the order the
       decider's restoration posture asks for (reverse activation order by
       default; activation order for an immediate restoration). The brain
       has no signal-control path: a signal-assisted posture is passed to
       the signal officer as an advisory row (the signal plan writer is an
       officer action). ``restoration_applied`` in the report says exactly
       what was done, so a posture is never presented as more than that
    2. Monitors queue dissipation on affected segments (numeric loop over
       the oracle's live observations)
    3. Generates post-movement report with actual vs planned metrics — the
       historical baseline is fetched per segment for the movement's own IST
       weekday and hour over the last 7 days (contract C-P): REAL hourly
       aggregates when there are any; otherwise a synthetic rollup of the
       simulated feed, labelled 'no real history yet — synthetic baseline
       (simulated feed), not used for calibration'; otherwise 'no history'.
       The escort duration comes from the escort's own start/end audit rows

A low-confidence restoration posture keeps the conservative staged default
and is audited as 'officer_review_recommended' / 'conservative_default_kept'
— the brain drafts nothing for /clear, so the trail never promises a draft.

Implemented as a plain async pipeline with the recovery monitoring loop
(previously a graph); the phase transition is an allowed decider-checkpoint
trigger, but the loop itself stays numeric-only. A silent live feed reports
the segments as ``unverified`` — never as recovered.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from convoy_brain.adaptive_params import AdaptiveParamsClient
from convoy_brain.agents.diversion_coordinator import DiversionCoordinatorAgent
from convoy_brain.decider import DeciderBrain
from convoy_brain.llm.heuristics import live_observation_rows
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.memory.convoy_context import (
    ConvoyContext,
    ConvoyContextStore,
    mutate_context,
)

logger = structlog.get_logger(__name__)

# Congestion level below which a segment is considered "recovered"
RECOVERY_CONGESTION_THRESHOLD = 0.3

# Poll interval while waiting for queue dissipation
RECOVERY_POLL_INTERVAL_SEC = 5.0

# Maximum recovery iterations before declaring timeout (~10 min at 5s)
MAX_RECOVERY_ITERATIONS = 120

# Historical-baseline fetches: at most this many segments, this many at once.
BASELINE_MAX_SEGMENTS = 20
BASELINE_CONCURRENCY = 4
# The baseline's lookback (the get_historical_pattern tool sends hours=168:
# one week of the same IST weekday/hour).
BASELINE_WINDOW_HOURS = 168
#: How a baseline built only from the simulated feed is labelled.
SYNTHETIC_BASELINE_LABEL = (
    "no real history yet — synthetic baseline (simulated feed), not used for calibration"
)
REAL_BASELINE_LABEL = "real hourly aggregates, same IST weekday and hour (last 7 days)"
NO_BASELINE_LABEL = "no history"

#: Restoration postures the phase-transition checkpoint offers.
RESTORATION_POSTURES = (
    "immediate_full_restoration",
    "staged_reverse_order_restoration",
    "signal_assisted_recovery",
)
DEFAULT_RESTORATION_POSTURE = "staged_reverse_order_restoration"

_IST = timezone(timedelta(hours=5, minutes=30))


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
    posture = DEFAULT_RESTORATION_POSTURE
    posture_source = "default"
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
                    "options": list(RESTORATION_POSTURES),
                },
            },
            force=True,
        )
        await context_store.append_decision(movement_id, checkpoint.audit)
        for alert in checkpoint.operator_alerts:
            row = dict(alert, phase="post_clearance_alert")
            if row.get("type") == "low_confidence_decision":
                # /clear drafts nothing: a low-confidence posture keeps the
                # staged default and an officer may review it.
                row["action"] = "officer_review_recommended"
                row["outcome"] = "conservative_default_kept"
                row["posture_kept"] = DEFAULT_RESTORATION_POSTURE
            await context_store.append_decision(movement_id, row)
        # A confident answer is applied; a low-confidence, withheld or
        # fallback one leaves the conservative staged default in place.
        answer = checkpoint.answers.get("restoration_posture")
        if (
            checkpoint.tier in ("decider", "tier0_cache_hit")
            and answer is not None
            and answer.value in RESTORATION_POSTURES
            and not checkpoint.audit.get("needs_gemma_draft")
        ):
            posture = str(answer.value)
            posture_source = checkpoint.tier
        elif answer is not None and answer.value in RESTORATION_POSTURES:
            posture_source = f"default (decider suggested {answer.value} at low confidence)"

    # -- Deactivate diversions (order per the restoration posture) -----------------
    ctx = await context_store.get(movement_id)
    active_diversions = list(ctx.active_diversions) if ctx else []

    if not active_diversions:
        deactivated_segments: list[int] = []
        affected_segments: list[int] = []
        logger.info("post_clearance.no_active_diversions", movement_id=movement_id)
    else:
        # Every posture lifts the closures in one step; the staged postures
        # list the last-activated closure first (the order agencies stand
        # their units down in), an immediate restoration keeps activation
        # order.
        ordered = (
            list(active_diversions) if posture == "immediate_full_restoration"
            else list(reversed(active_diversions))
        )
        segment_ids = [int(s) for s in ordered]
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
            posture=posture,
        )

    # -- Monitor recovery (numeric loop) ---------------------------------------------
    cap = (
        max_recovery_iterations if max_recovery_iterations is not None
        else MAX_RECOVERY_ITERATIONS
    )
    segments_recovered: list[int] = []
    segments_congested: list[int] = affected_segments
    segments_unverified: list[int] = []
    recovery_iteration = 0
    restoration_order = list(affected_segments)

    while affected_segments and recovery_iteration < cap:
        recovery_iteration += 1
        try:
            live_traffic = await executor.execute("get_live_traffic", {
                "segment_ids": affected_segments,
            })
        except Exception as exc:
            logger.warning("post_clearance.live_traffic_failed", error=str(exc)[:200])
            live_traffic = None

        rows = {r["segment_id"]: r for r in live_observation_rows(live_traffic)}
        recovered = [
            sid for sid in affected_segments
            if sid in rows and rows[sid]["congestion_idx"] < RECOVERY_CONGESTION_THRESHOLD
        ]
        still_congested = [
            sid for sid in affected_segments
            if sid in rows and rows[sid]["congestion_idx"] >= RECOVERY_CONGESTION_THRESHOLD
        ]
        unverified = [sid for sid in affected_segments if sid not in rows]

        segments_recovered = sorted(set(segments_recovered) | set(recovered))
        segments_congested = still_congested
        segments_unverified = unverified

        logger.info(
            "post_clearance.recovery_check",
            movement_id=movement_id,
            iteration=recovery_iteration,
            recovered=len(recovered),
            congested=len(still_congested),
            unverified=len(unverified),
        )

        if not still_congested:
            # Nothing measurably congested; segments without data stay
            # "unverified" in the report rather than counted as recovered.
            break
        await asyncio.sleep(RECOVERY_POLL_INTERVAL_SEC)
        affected_segments = still_congested

    # -- Report ------------------------------------------------------------------------
    ctx = await context_store.get(movement_id)
    report = await _generate_report(
        movement_id=movement_id,
        ctx=ctx,
        executor=executor,
        affected=restoration_order,
        deactivated_count=len(deactivated_segments),
        segments_recovered=segments_recovered,
        segments_congested=segments_congested,
        segments_unverified=segments_unverified,
        recovery_iteration=recovery_iteration,
    )
    report["restoration_posture"] = posture
    report["restoration_posture_source"] = posture_source
    report["restoration_order"] = restoration_order
    # What the posture actually did — no more than this.
    signal_assist = None
    if posture == "signal_assisted_recovery":
        signal_assist = (
            "recommended to the signal officer (advisory row; the brain has no "
            "signal-control path)"
        )
        event_id = getattr(ctx, "event_id", None) if ctx else None
        await context_store.append_decision(movement_id, {
            "phase": "post_clearance_alert",
            "type": "signal_assist_recommended",
            "action": "apply_signal_recovery_plan",
            "reason": (
                "the decider chose signal-assisted recovery: a signal officer applies the "
                "recovery plan for the restored segments (POST /api/v1/signals/plans/apply"
                + (f" with event_id {event_id}" if event_id else "")
                + ") — the brain does not command signals"
            ),
            "segment_ids": list(restoration_order),
            "event_id": event_id,
            "movement_id": movement_id,
            "timestamp": time.time(),
        })
    report["restoration_applied"] = {
        "steps": 1 if restoration_order else 0,
        "closures_lifted": len(deactivated_segments),
        "order": (
            "activation" if posture == "immediate_full_restoration" else "reverse_activation"
        ),
        "signal_assist": signal_assist,
    }

    def complete(c: ConvoyContext) -> None:
        c.status = "completed"
        c.decisions_log.append({
            "phase": "post_clearance",
            "action": "clearance_complete",
            "restoration_posture": posture,
            "recovery_iterations": recovery_iteration,
            "segments_recovered": len(segments_recovered),
            "segments_still_congested": len(segments_congested),
            "segments_unverified": len(segments_unverified),
            "timestamp": time.time(),
        })

    await mutate_context(context_store, movement_id, complete)

    logger.info(
        "post_clearance_workflow.complete",
        movement_id=movement_id,
        status="completed",
    )
    return report


def _escort_window(decisions: list[dict]) -> tuple[float | None, float | None, int]:
    """(start, end, runs) of the escort of the newest plan in the audit trail.

    An escort resumed after a stop or a brain restart is one escort in
    several runs: the window spans the first run's start to the last run's
    end (an open last run leaves ``end`` None). A re-plan starts a new
    window.
    """
    start = end = None
    runs = 0
    for row in decisions:
        if not isinstance(row, dict):
            continue
        stamp = row.get("timestamp")
        if row.get("phase") == "pre_movement":
            start = end = None
            runs = 0
            continue
        if row.get("phase") != "live_escort" or not isinstance(stamp, (int, float)):
            continue
        action = row.get("action")
        if action == "escort_started":
            runs += 1
            start = stamp if start is None else start
            end = None
        elif action in ("escort_complete", "escort_stopped", "escort_failed") and start is not None:
            end = stamp
    if start is None:
        # Legacy trail (before escort_started rows): the first escort row.
        first = next(
            (d for d in decisions if isinstance(d, dict) and d.get("phase") == "live_escort"
             and isinstance(d.get("timestamp"), (int, float))),
            None,
        )
        start = first.get("timestamp") if first else None
    return start, end, runs


async def _historical_baseline(
    executor: ToolExecutor, segment_ids: list[int], at: datetime,
) -> dict:
    """Per-segment baseline at the movement's own IST weekday and hour.

    The tool takes ONE segment per call (``segment_id``/``day_of_week``/
    ``hour``, hours=168); each call is isolated and the concurrency is
    bounded. Each segment states its provenance: ``real`` (the oracle's
    REAL hourly aggregates), ``synthetic`` (only the labelled rollup of the
    simulated feed — never a calibration input, never shown as real
    history) or ``none``. An oracle without the synthetic rollup or the
    provenance field simply yields real-or-none.
    """
    gate = asyncio.Semaphore(BASELINE_CONCURRENCY)
    dow, hour = at.weekday(), at.hour

    def summary(rows: list[dict]) -> dict:
        speeds = [a["avg_speed_kmh"] for a in rows
                  if isinstance(a.get("avg_speed_kmh"), (int, float))]
        congestion = [a["p95_congestion"] for a in rows
                      if isinstance(a.get("p95_congestion"), (int, float))]
        return {
            "hours": len(rows),
            "avg_speed_kmh": round(sum(speeds) / len(speeds), 1) if speeds else None,
            "p95_congestion": round(max(congestion), 3) if congestion else None,
        }

    async def one(sid: int) -> tuple[int, dict]:
        async with gate:
            try:
                result = await executor.execute("get_historical_pattern", {
                    "segment_id": sid,
                    "day_of_week": dow,
                    "hour": hour,
                })
            except Exception as exc:
                return sid, {"error": str(exc)[:200], "provenance": "none", "hours": 0}
        if not isinstance(result, dict) or result.get("ok") is False:
            detail = result.get("detail") if isinstance(result, dict) else None
            return sid, {"provenance": "none", "hours": 0, "note": detail or "no answer"}
        real = [a for a in result.get("aggregates") or [] if isinstance(a, dict)]
        synthetic = [a for a in result.get("synthetic_aggregates") or [] if isinstance(a, dict)]
        if real:
            return sid, {**summary(real), "provenance": "real", "label": REAL_BASELINE_LABEL}
        if synthetic:
            return sid, {
                **summary(synthetic),
                "provenance": "synthetic",
                "data_quality": "synthetic",
                "label": SYNTHETIC_BASELINE_LABEL,
            }
        return sid, {"hours": 0, "provenance": "none", "label": NO_BASELINE_LABEL}

    wanted = list(dict.fromkeys(int(s) for s in segment_ids))[:BASELINE_MAX_SEGMENTS]
    pairs = await asyncio.gather(*(one(s) for s in wanted))
    by_segment = {str(sid): data for sid, data in pairs}
    real_n = sum(1 for d in by_segment.values() if d.get("provenance") == "real")
    synthetic_n = sum(1 for d in by_segment.values() if d.get("provenance") == "synthetic")
    return {
        "source": "oracle hourly aggregates (traffic.hourly_aggregates — real rows only)",
        "basis": "same IST weekday and hour",
        "window_hours": BASELINE_WINDOW_HOURS,
        "day_of_week": dow,
        "hour_ist": hour,
        "segments": by_segment,
        "segments_with_history": real_n,
        "segments_synthetic_only": synthetic_n,
        "provenance": "real" if real_n else ("synthetic" if synthetic_n else "none"),
        "baseline_label": (
            REAL_BASELINE_LABEL if real_n
            else SYNTHETIC_BASELINE_LABEL if synthetic_n
            else NO_BASELINE_LABEL
        ),
    }


async def _generate_report(
    *,
    movement_id: str,
    ctx: Any,
    executor: ToolExecutor,
    affected: list[int],
    deactivated_count: int,
    segments_recovered: list[int],
    segments_congested: list[int],
    segments_unverified: list[int],
    recovery_iteration: int,
) -> dict:
    decisions = ctx.decisions_log if ctx else []

    pre_movement_entry = next(
        (d for d in decisions if isinstance(d, dict) and d.get("phase") == "pre_movement"), None
    )
    planning_timestamp = pre_movement_entry.get("timestamp") if pre_movement_entry else None
    escort_start, escort_end, escort_runs = _escort_window(decisions)

    historical_data: dict = {}
    if affected:
        moment = datetime.fromtimestamp(escort_start, _IST) if escort_start \
            else datetime.now(_IST)
        historical_data = await _historical_baseline(executor, affected, moment)

    return {
        "movement_id": movement_id,
        "vvip_class": ctx.vvip_class if ctx else "unknown",
        "selected_route_id": ctx.selected_route_id if ctx else None,
        "total_affected_segments": len(affected),
        "segments_recovered": len(segments_recovered),
        "segments_still_congested": len(segments_congested),
        "segments_unverified": len(segments_unverified),
        "recovery_iterations": recovery_iteration,
        # The first check runs at once; each later one follows a poll sleep.
        "recovery_time_sec": max(0, recovery_iteration - 1) * RECOVERY_POLL_INTERVAL_SEC,
        "diversions_deactivated": deactivated_count,
        "alerts_during_escort": len(ctx.alerts) if ctx else 0,
        "total_decisions": len(decisions),
        "planning_to_completion_sec": (
            round(time.time() - planning_timestamp, 1) if planning_timestamp else None
        ),
        "escort_duration_sec": (
            round((escort_end or time.time()) - escort_start, 1) if escort_start else None
        ),
        "escort_ended": escort_end is not None,
        "escort_runs": escort_runs,
        "historical_baseline": historical_data,
        "generated_at": time.time(),
    }
