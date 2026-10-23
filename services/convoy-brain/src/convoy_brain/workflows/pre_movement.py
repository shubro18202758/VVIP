"""Pre-Event Planning Workflow — multi-agent planning pipeline.

Orchestrates the full planning sequence before a corridor event goes live
(a protected movement, procession, race, ceremonial, egress surge,
evacuation or work zone — the event-agnostic pipeline):

    1. Adaptive parameters resolve (fail-closed to protocol baselines)
    2. Traffic Analyst ∥ Route Planner                         (concurrent —
       independent, and llama-server serves two slots; the analyst does not
       narrate a corridor with zero live observations)
    3. Security re-route loop on the deterministic lane audit: the best
       candidate set seen is kept together with its violations; violating
       segments are avoided on re-route (at most MAX_SECURITY_RETRIES); the
       loop stops as soon as the planner returns nothing, and an
       unresolvable class floor yields status 'conditional' with the reason
    4. Decider checkpoint over the ACTUAL ranked candidates (+ delay_departure)
       — strategic tier, <=1500-token state; each candidate is offered under a
       stable CONTENT label (a short hash of its segment sequence — never the
       oracle's per-request route_id, so identical plans ask the identical
       question set and the tier-0 routine cache can hit); its answer is
       resolved back to that candidate and applied (primary / alternate
       reordered, or status 'hold'); low-confidence picks need the tactical
       ranking's agreement; the audit row (with the label -> route_id map)
       lands in decisions_log
    5. Security review ∥ Diversion Coordinator ∥ Scenario evaluation on the
       final primary (Gemma tier ×2 + numeric tier)
    6. Finalize: the plan document (contract C-I) is fused into the city
       picture — registered as a corridor event, superseding (cancelling) the
       movement's previous event on re-plan — and persisted to the context
       in ONE atomic field update (mutate_context): a GPS fix or a chat
       turn's audit row written while the plan ran is never lost to a stale
       whole-document write. The overall reasoning ends with a 'Basis:'
       sentence of concrete factors (lane floor, segments avoided, forecast
       congestion, closure window, detours) — never generic prose alone.

PLAN_DEADLINE_SEC bounds the tactical tier: once the budget is spent every
remaining agent runs its deterministic floor instead of a model call, so a
slow or saturated llama-server can no longer stretch a plan past the deck
proxy's patience.

Implemented as a plain async pipeline (the node structure and conditional
security-retry loop of the previous graph, without the graph dependency).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable
from typing import Any

import structlog

from convoy_brain.adaptive_params import AdaptiveParamsClient
from convoy_brain.agents.diversion_coordinator import DiversionCoordinatorAgent
from convoy_brain.agents.route_planner import NO_COMPLIANT_LEAD, RoutePlannerAgent
from convoy_brain.agents.security_liaison import (
    SecurityLiaisonAgent,
    hard_violation_summary,
    min_lanes_for,
)
from convoy_brain.agents.traffic_analyst import TrafficAnalystAgent
from convoy_brain.city_link import CityLink, current_event_id
from convoy_brain.config import CONFIDENCE_FLOOR, DECIDE_TIMEOUT_SEC, baseline_params
from convoy_brain.decider import MAX_ROUTE_OPTIONS, CheckpointResult, DeciderBrain
from convoy_brain.llm.llama_bridge import LlamaBridge, LlamaUnavailable
from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.memory.convoy_context import ConvoyContext, ConvoyContextStore, mutate_context

logger = structlog.get_logger(__name__)

# Maximum security re-route attempts before proceeding with warnings
MAX_SECURITY_RETRIES = 2

# Corridor bounding box padding (degrees) around origin+destination
CORRIDOR_PAD_DEG = 0.02

#: Wall budget for a plan's tactical-tier calls (seconds).
PLAN_DEADLINE_SEC = 120.0
#: A tactical call is not started with less budget left than this.
MIN_TACTICAL_CALL_SEC = 2.0

DELAY_DEPARTURE = "delay_departure"

#: Decider option labels for the route checkpoint: ROUTE_LABEL_PREFIX + the
#: first ROUTE_LABEL_HEX hex digits of sha256 over the candidate's ORDERED
#: segment sequence (its physical path). Never the oracle's route_id — the
#: oracle mints it per request (``{source}_{uuid}``), and since the question
#: set is part of the tier-0 cache key, identical plans never shared a key and
#: the routine cache could never hit. Never a rank either — a cached "second
#: option" would select whichever route ranks second today. Same path, same
#: label on every request; a different path, a different label: an answer
#: cached for one candidate set is served only to that identical set, and it
#: resolves to the same physical route it was decided for.
ROUTE_LABEL_PREFIX = "route_"
ROUTE_LABEL_HEX = 12


class _DeadlineBridge:
    """Tactical bridge bounded by the plan deadline.

    Duck-types :class:`LlamaBridge`. Past the deadline a call is not made at
    all; a call in flight when the budget runs out is cancelled. Both raise
    :class:`LlamaUnavailable`, which every agent already maps onto its
    deterministic floor — "degrade the remaining agents to heuristics".
    """

    def __init__(self, bridge: LlamaBridge, deadline: float) -> None:
        self._bridge = bridge
        self._deadline = deadline
        self.skipped = 0
        self.cut = 0

    def remaining(self) -> float:
        return self._deadline - time.monotonic()

    async def generate(self, *args: Any, **kwargs: Any) -> str:
        return await self._bounded(self._bridge.generate(*args, **kwargs))

    async def generate_with_meta(self, *args: Any, **kwargs: Any) -> tuple[str, str | None]:
        """``(content, finish_reason)`` under the same deadline as generate().

        Defined here (never forwarded through __getattr__, which would
        bypass the deadline); a bridge without it reports no finish reason.
        """
        with_meta = getattr(self._bridge, "generate_with_meta", None)
        if with_meta is None:
            return await self._bounded(self._bridge.generate(*args, **kwargs)), None
        return await self._bounded(with_meta(*args, **kwargs))

    async def _bounded(self, call: Awaitable[Any]) -> Any:
        remaining = self.remaining()
        if remaining < MIN_TACTICAL_CALL_SEC:
            self.skipped += 1
            if asyncio.iscoroutine(call):
                call.close()  # never awaited: no 'coroutine was never awaited' warning
            raise LlamaUnavailable("plan deadline reached — tactical call skipped")
        try:
            return await asyncio.wait_for(call, timeout=remaining)
        except asyncio.TimeoutError as exc:
            self.cut += 1
            raise LlamaUnavailable(
                f"plan deadline reached mid-call ({remaining:.0f}s budget left)"
            ) from exc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)


async def _guarded(stage: str, awaitable: Awaitable[dict], fallback: dict) -> dict:
    """Run one stage; an exception degrades it to ``fallback`` (labelled)."""
    try:
        return await awaitable
    except Exception as exc:
        logger.error("pre_movement.stage_failed", stage=stage, error=str(exc))
        result = dict(fallback)
        result["_error"] = f"{stage} failed: {str(exc)[:200]}"
        return result


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
    city_link: CityLink | None = None,
    deadline_sec: float = PLAN_DEADLINE_SEC,
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
        city_link: corridor-event writer (optional; fusion skipped when absent)
        deadline_sec: tactical-tier wall budget for the whole plan

    Returns:
        The plan document (contract C-I): routes, protocol verdict, decider
        pick, diversions, scenario comparison and the registered event id.
    """
    if bridge is None or executor is None or context_store is None:
        raise ValueError(
            "bridge, executor, and context_store are required. "
            "Pass them as keyword arguments."
        )

    started = time.monotonic()
    timings: dict[str, float] = {}

    def mark(stage: str, since: float) -> float:
        now = time.monotonic()
        timings[stage] = round(now - since, 2)
        return now

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
    advance_closure_sec = (adaptive or baseline_params()).floored_advance_closure_sec(vvip_class)

    tactical = _DeadlineBridge(bridge, started + deadline_sec)
    segment_cache: dict[int, dict] = {}
    traffic_analyst = TrafficAnalystAgent(tactical, executor)
    route_planner = RoutePlannerAgent(
        tactical, executor, adaptive=adaptive, segment_cache=segment_cache
    )
    security_liaison = SecurityLiaisonAgent(tactical, executor, segment_cache=segment_cache)
    diversion_coordinator = DiversionCoordinatorAgent(
        tactical, executor, context_store, adaptive=adaptive
    )

    state: dict[str, Any] = {
        "movement_id": movement_id,
        "origin": origin,
        "destination": destination,
        "vvip_class": vvip_class,
        "planned_departure": planned_departure,
        "adaptive": adaptive,
        "advance_closure_sec": advance_closure_sec,
        "decider_checkpoint": None,
        "decider_pick": None,
    }

    # -- 2. Corridor assessment ∥ route planning ------------------------------
    t = time.monotonic()
    bbox = (
        min(origin[0], destination[0]) - CORRIDOR_PAD_DEG,
        min(origin[1], destination[1]) - CORRIDOR_PAD_DEG,
        max(origin[0], destination[0]) + CORRIDOR_PAD_DEG,
        max(origin[1], destination[1]) + CORRIDOR_PAD_DEG,
    )
    no_route = {
        "primary_route": None, "alternate_routes": [], "rejected_routes": [],
        "overall_reasoning": "Route planning failed.", "confidence": "low",
        "_tier": "deterministic",
    }
    state["corridor_assessment"], routing = await asyncio.gather(
        _guarded(
            "corridor_assessment",
            traffic_analyst.assess_corridor(bbox),
            {"overall_status": "unknown", "confidence": "low", "_tier": "deterministic"},
        ),
        _guarded(
            "route_planning",
            route_planner.analyze_routes(
                movement_id=movement_id,
                origin=origin,
                destination=destination,
                vvip_class=vvip_class,
            ),
            no_route,
        ),
    )
    t = mark("corridor_and_routing", t)
    logger.info(
        "pre_movement.corridor_assessed",
        movement_id=movement_id,
        status=state["corridor_assessment"].get("overall_status"),
        tier=state["corridor_assessment"].get("_tier"),
    )

    # -- 3. Security re-route loop (deterministic lane audit) -----------------
    avoid: list[int] = []
    attempts: list[dict] = []
    best: dict | None = None
    reroute_exhausted = False
    for attempt in range(MAX_SECURITY_RETRIES + 1):
        primary = routing.get("primary_route")
        logger.info(
            "pre_movement.routes_planned",
            movement_id=movement_id,
            attempt=attempt,
            primary=primary is not None,
            alternates=len(routing.get("alternate_routes", [])),
        )
        if primary is None:
            attempts.append({
                "attempt": attempt,
                "avoid_segments": list(avoid),
                "primary_route_id": None,
                "reason": routing.get("overall_reasoning"),
            })
            if attempt > 0:
                reroute_exhausted = True
            break  # stop retrying once the planner returns nothing

        audit = await security_liaison.audit_route(primary["segment_ids"], vvip_class)
        attempts.append({
            "attempt": attempt,
            "avoid_segments": list(avoid),
            "primary_route_id": primary["route_id"],
            "compliant": audit["compliant"],
            "violations": len(audit["violations"]),
        })
        logger.info(
            "pre_movement.security_validated",
            movement_id=movement_id,
            compliant=audit["compliant"],
            violations=len(audit["violations"]),
        )
        candidate = {"routing": routing, "audit": audit, "attempt": attempt}
        if best is None or _audit_rank(audit) > _audit_rank(best["audit"]):
            best = candidate
        if audit["compliant"]:
            break
        if attempt >= MAX_SECURITY_RETRIES:
            logger.warning(
                "pre_movement.security_max_retries",
                movement_id=movement_id,
                retries=attempt,
            )
            reroute_exhausted = True
            break

        # Feed hard violations back to the planner as an avoid list
        new_avoid = [
            v["segment_id"] for v in audit["violations"]
            if v.get("rule") == "ROAD_WIDTH" and v.get("segment_id") not in avoid
        ]
        if not new_avoid:
            break  # nothing a re-route could change
        avoid.extend(new_avoid)
        logger.info(
            "pre_movement.reroute",
            movement_id=movement_id,
            avoid_count=len(avoid),
            retry=attempt + 1,
        )
        routing = await _guarded(
            "route_planning",
            route_planner.analyze_routes(
                movement_id=movement_id,
                origin=origin,
                destination=destination,
                vvip_class=vvip_class,
                avoid_segments=avoid,
            ),
            no_route,
        )
    t = mark("security_loop", t)

    if best is not None:
        routing = best["routing"]
        state["protocol_audit"] = best["audit"]
    else:
        state["protocol_audit"] = None
    state["route_candidates"] = routing
    state["security_reroutes"] = attempts
    state["avoided_segments"] = list(avoid)
    if best is not None and not best["audit"]["compliant"]:
        # The verdict sentence is stated once: the routing reasoning (the
        # deterministic floor, or a narration echoing it) may carry it.
        state["reroute_note"] = _reroute_note(
            best, attempts, vvip_class, reroute_exhausted,
            include_lead=NO_COMPLIANT_LEAD not in str(routing.get("overall_reasoning") or ""),
        )

    # -- 4. Strategic tier: decider checkpoint over the ranked routes ---------
    routes = _route_list(routing)
    if decider_brain is not None and routes:
        options = _route_options(routes)
        checkpoint = await _route_checkpoint(
            decider_brain,
            movement_id=movement_id,
            vvip_class=vvip_class,
            planned_departure=planned_departure,
            corridor=state["corridor_assessment"],
            options=options,
            remaining=tactical.remaining(),
        )
        routes, pick = _apply_decider_pick(checkpoint, routes, vvip_class, options)
        audit_row = dict(checkpoint.audit)
        # The question set names candidates by content label; the row keeps
        # the label -> oracle route_id map so the audit trail resolves them.
        audit_row["route_labels"] = {label: route["route_id"] for label, route in options}
        audit_row["decider_pick"] = pick
        state["decider_checkpoint"] = audit_row
        state["decider_pick"] = pick
        # Persisted now (no concurrent context writer runs at this point).
        await context_store.append_decision(movement_id, audit_row)
        for alert in checkpoint.operator_alerts:
            await context_store.append_decision(
                movement_id, dict(alert, phase="pre_movement_alert")
            )
        routing = dict(routing, primary_route=routes[0], alternate_routes=routes[1:])
        state["route_candidates"] = routing
    t = mark("decider_checkpoint", t)

    # -- 5. Security review ∥ diversions ∥ scenarios on the final primary -----
    primary = routing.get("primary_route")
    if primary is None:
        state["security_validation"] = {
            "compliant": False,
            "violations": [],
            "warnings": [],
            "recommendations": ["No primary route to validate."],
            "security_score": 0.0,
            "confidence": "low",
            "_tier": "deterministic",
        }
        state["diversion_plans"] = {"directives": [], "overall_status": "no_route"}
        state["scenario_result"] = {"results": [], "best": None, "error": None}
    else:
        segment_ids = primary["segment_ids"]
        (
            state["security_validation"],
            state["diversion_plans"],
            state["scenario_result"],
        ) = await asyncio.gather(
            _guarded(
                "security_review",
                security_liaison.validate_route(segment_ids, vvip_class),
                {"compliant": False,
                 "violations": [{
                     "segment_id": None, "rule": "UNVERIFIED", "severity": "high",
                     "detail": "security review failed — protocol compliance unverified",
                 }],
                 "warnings": [], "recommendations": [], "security_score": 0.0,
                 "confidence": "low", "_tier": "deterministic"},
            ),
            _guarded(
                "diversion_planning",
                _coordinate_diversions(
                    diversion_coordinator,
                    executor,
                    movement_id=movement_id,
                    segment_ids=segment_ids,
                    vvip_class=vvip_class,
                    advance_closure_sec=advance_closure_sec,
                ),
                {"directives": [], "overall_status": "diversion planning failed",
                 "confidence": "low", "_tier": "deterministic"},
            ),
            _evaluate_scenarios(executor, routes, planned_departure),
        )
        logger.info(
            "pre_movement.diversions_planned",
            movement_id=movement_id,
            directives=len(state["diversion_plans"].get("directives", [])),
        )
        logger.info(
            "pre_movement.scenarios_evaluated",
            movement_id=movement_id,
            count=len(state["scenario_result"].get("results", [])),
        )
    t = mark("review_diversions_scenarios", t)

    # -- 6. Finalize ---------------------------------------------------------------
    state["timings_sec"] = timings
    state["deadline"] = {
        "budget_sec": deadline_sec,
        "tactical_calls_skipped": tactical.skipped,
        "tactical_calls_cut": tactical.cut,
    }
    recommendation = await _finalize(state, context_store, city_link, started)
    logger.info(
        "pre_movement_workflow.complete",
        movement_id=movement_id,
        status=recommendation["status"],
        event_id=recommendation.get("event_id"),
        duration_sec=recommendation["timings_sec"].get("total"),
    )
    return recommendation


# ---------------------------------------------------------------------------
# Security loop helpers
# ---------------------------------------------------------------------------


def _audit_rank(audit: dict) -> tuple:
    """Order audits best-first: compliant, fewer critical, fewer total, safer."""
    violations = audit.get("violations", [])
    critical = sum(1 for v in violations if v.get("severity") == "critical")
    return (
        bool(audit.get("compliant")),
        -critical,
        -len(violations),
        float(audit.get("security_score") or 0.0),
    )


def _reroute_note(
    best: dict,
    attempts: list[dict],
    vvip_class: str,
    exhausted: bool,
    *,
    include_lead: bool = True,
) -> str:
    """The conditional plan's re-route story; ``include_lead=False`` leaves
    out the 'No fully compliant route: …' verdict the reasoning already states."""
    summary = hard_violation_summary(best["audit"]["violations"], vvip_class)
    reroutes = max(0, len(attempts) - 1)
    text = f"{NO_COMPLIANT_LEAD} {summary}." if include_lead else ""
    if reroutes:
        last = attempts[-1]
        if last.get("primary_route_id") is None:
            text += (
                f" Re-routing around those segments found no connected alternative "
                f"({reroutes} attempt(s)); returning the best candidate set as "
                "conditional for an operator waiver."
            )
        else:
            text += (
                f" {reroutes} re-route attempt(s) did not clear the floor; returning the "
                "best candidate set as conditional for an operator waiver."
            )
    else:
        text += " Returning the candidate set as conditional for an operator waiver."
    return text.strip()


def _route_list(routing: dict) -> list[dict]:
    primary = routing.get("primary_route")
    if primary is None:
        return []
    return [primary, *[r for r in routing.get("alternate_routes", []) if isinstance(r, dict)]]


# ---------------------------------------------------------------------------
# Strategic checkpoint (after routing, over the actual candidates)
# ---------------------------------------------------------------------------


def _segment_sequence(route: dict) -> str:
    """Canonical text of a candidate's ORDERED segment sequence (its path).

    Integral ids are normalized (``1001``, ``"1001"`` and ``1001.0`` name the
    same segment); anything else is kept verbatim, never truncated onto a
    neighbouring id (``1001.5`` is not ``1001``). The order is kept — the
    reverse traversal is another route.
    """
    parts: list[str] = []
    for sid in route.get("segment_ids") or []:
        if isinstance(sid, bool) or (isinstance(sid, float) and not sid.is_integer()):
            parts.append(repr(sid))
            continue
        try:
            parts.append(str(int(sid)))
        except (TypeError, ValueError, OverflowError):
            parts.append(str(sid))
    return ",".join(parts)


def _sequence_digest(route: dict) -> str:
    return hashlib.sha256(_segment_sequence(route).encode("utf-8")).hexdigest()


def route_option_label(route: dict) -> str:
    """The decider option label of one candidate: content-derived, deterministic.

    Depends on the segment sequence ONLY — never on the oracle's route_id, its
    source, the rank, or per-tick figures (ETA, scores). Within one plan
    :func:`_route_options` lengthens a label only on a short-digest collision
    between two different paths.
    """
    return ROUTE_LABEL_PREFIX + _sequence_digest(route)[:ROUTE_LABEL_HEX]


def _route_options(routes: list[dict]) -> list[tuple[str, dict]]:
    """``(label, route)`` per DISTINCT physical candidate, tactical rank order.

    The same candidate list yields the same labels on every request. A
    candidate whose segment sequence repeats a higher-ranked one's is the same
    physical route: it is folded into that candidate's option, never offered
    twice (two identical options would only split the readout's probability
    mass), and a pick of the shared label resolves to the higher-ranked
    candidate. Two DIFFERENT sequences whose short digests collide take a
    longer prefix of their own digest, so label -> route is one-to-one within
    a plan. At most MAX_ROUTE_OPTIONS - 1 routes (DELAY_DEPARTURE takes the
    last option slot).
    """
    options: list[tuple[str, dict]] = []
    owners: dict[str, str] = {}  # label -> the segment sequence it names
    for route in routes:
        sequence = _segment_sequence(route)
        digest = _sequence_digest(route)
        width = ROUTE_LABEL_HEX
        while (
            owners.get(ROUTE_LABEL_PREFIX + digest[:width], sequence) != sequence
            and width < len(digest)
        ):
            width += 4
        label = ROUTE_LABEL_PREFIX + digest[:width]
        if label in owners:
            continue  # the same path as a higher-ranked candidate
        owners[label] = sequence
        options.append((label, route))
    return options[: MAX_ROUTE_OPTIONS - 1]


def _route_questions(options: list[tuple[str, dict]]) -> dict[str, dict]:
    """The route_strategy question set: candidate labels + DELAY_DEPARTURE."""
    return DeciderBrain.pre_movement_questions(
        [label for label, _ in options] + [DELAY_DEPARTURE]
    )


def _route_state_doc(
    *,
    vvip_class: str,
    planned_departure: str,
    corridor: dict,
    options: list[tuple[str, dict]],
) -> str:
    """The checkpoint's state document; candidate lines carry the option labels."""
    floor = min_lanes_for(vvip_class)
    lines = [
        f"Pre-movement route decision: {vvip_class} protected movement departing "
        f"{planned_departure}.",
        f"Corridor status: {corridor.get('overall_status', 'unknown')} "
        f"({corridor.get('observations', 0)} live observations on "
        f"{corridor.get('segments', 'n/a')} corridor segments).",
        f"Candidates in tactical rank order (lane floor {floor} for {vvip_class}):",
    ]
    for label, route in options:
        violations = route.get("lane_violations") or []
        tier = route.get("route_tier", "unknown")
        lines.append(
            f"- {label}: {len(route.get('segment_ids', []))} segments, "
            f"{(route.get('distance_m') or 0) / 1000:.1f} km, ETA "
            f"{route.get('estimated_time_sec', 0) / 60:.0f} min, weighted score "
            f"{(route.get('weighted_score') or 0):.2f}, {tier}"
            + (f" ({len(violations)} segment(s) below the floor)" if violations else "")
        )
    lines.append(f"Option {DELAY_DEPARTURE}: hold the plan and depart later.")
    return "\n".join(lines)


async def _route_checkpoint(
    decider_brain: DeciderBrain,
    *,
    movement_id: str,
    vvip_class: str,
    planned_departure: str,
    corridor: dict,
    options: list[tuple[str, dict]],
    remaining: float,
) -> CheckpointResult:
    """route_strategy over the candidates' content labels (+ delay_departure),
    deadline-bounded. ``options`` comes from :func:`_route_options`."""
    state_doc = DeciderBrain.build_state_doc(
        trigger="pre_movement",
        summary=_route_state_doc(
            vvip_class=vvip_class,
            planned_departure=planned_departure,
            corridor=corridor,
            options=options,
        ),
        context={"phase": "pre_movement", "vvip_class": vvip_class},
        pre_movement=True,
    )
    questions = _route_questions(options)
    budget = min(DECIDE_TIMEOUT_SEC + 5.0, remaining)
    if budget < 1.0:
        return _skipped_checkpoint(movement_id, "plan deadline reached before the checkpoint")
    try:
        return await asyncio.wait_for(
            decider_brain.checkpoint(
                movement_id=movement_id,
                trigger="pre_movement",
                state_doc=state_doc,
                questions=questions,
                force=True,
            ),
            timeout=budget,
        )
    except asyncio.TimeoutError:
        return _skipped_checkpoint(
            movement_id, f"checkpoint exceeded the {budget:.0f}s plan budget"
        )
    except Exception as exc:  # a malformed question set must not sink the plan
        logger.error("pre_movement.checkpoint_failed", error=str(exc))
        return _skipped_checkpoint(movement_id, f"checkpoint failed: {str(exc)[:200]}")


def _skipped_checkpoint(movement_id: str, reason: str) -> CheckpointResult:
    audit = {
        "phase": "decider_checkpoint",
        "trigger": "pre_movement",
        "movement_id": movement_id,
        "tier": "unavailable",
        "reason": reason,
        "timestamp": time.time(),
    }
    alert = {
        "type": "decider_unavailable",
        "movement_id": movement_id,
        "trigger": "pre_movement",
        "reason": reason,
        "action": "tactical ranking kept",
        "timestamp": time.time(),
    }
    return CheckpointResult(tier="unavailable", degraded_reason=reason,
                            operator_alerts=[alert], audit=audit)


def _apply_decider_pick(
    checkpoint: CheckpointResult,
    routes: list[dict],
    vvip_class: str,
    options: list[tuple[str, dict]] | None = None,
) -> tuple[list[dict], dict | None]:
    """Act on the decider's route_strategy answer; returns (routes, decider_pick).

    The answer is an option LABEL (:func:`_route_options`; recomputed from
    ``routes`` when not given), resolved back to the candidate it names: the
    pick's ``route_id`` is that candidate's oracle route_id (None for
    ``delay_departure`` or an answer naming no candidate) and ``choice`` the
    label as answered — by the 27B or, verbatim, by the tier-0 cache.

    Applied when the strategic tier actually answered (tier decider or a
    tier-0 cache hit) and the pick is confident; a pick below the
    confidence floor is applied only when it agrees with the tactical
    ranking. A withheld (novelty-latched) pick, or one that would promote a
    route breaking the class's lane floor over a compliant one, is recorded
    but not applied. ``delay_departure`` holds the plan.
    """
    answer = checkpoint.answers.get("route_strategy")
    if answer is None or checkpoint.tier not in ("decider", "tier0_cache_hit", "novelty_latched"):
        return routes, None

    if options is None:
        options = _route_options(routes)
    choice = str(answer.value)
    chosen = dict(options).get(choice)
    confidence = answer.confidence
    pick: dict[str, Any] = {
        "route_id": chosen["route_id"] if chosen is not None else None,
        "choice": choice,
        "confidence": round(float(confidence), 4) if confidence is not None else None,
        "tier": checkpoint.tier,
        "applied": False,
        "hold": False,
        "reason": "",
    }
    current = routes[0]["route_id"]
    # routes[0] always heads the options: it is first and folds into nothing.
    primary_label = options[0][0] if options else None
    low_confidence = confidence is not None and confidence < CONFIDENCE_FLOOR

    if checkpoint.tier == "novelty_latched":
        pick["reason"] = (
            "withheld: novel situation (no similar memory on record) — tactical ranking kept"
        )
        return routes, pick
    if choice != DELAY_DEPARTURE and chosen is None:
        pick["reason"] = f"pick {choice!r} is not a candidate — tactical ranking kept"
        return routes, pick
    if low_confidence and choice != primary_label:
        pick["reason"] = (
            f"confidence {confidence:.2f} below the {CONFIDENCE_FLOOR:.2f} floor and the "
            f"pick disagrees with the tactical primary {current} — tactical ranking kept"
        )
        return routes, pick
    if choice == DELAY_DEPARTURE:
        pick["applied"] = True
        pick["hold"] = True
        pick["reason"] = "strategic tier chose to delay departure — plan held"
        return routes, pick
    if choice == primary_label:
        pick["applied"] = True
        pick["reason"] = (
            "strategic tier confirms the tactical primary"
            + (" (low confidence, tactical agreement)" if low_confidence else "")
        )
        return routes, pick

    chosen_id = chosen["route_id"]
    if chosen.get("route_tier") != "compliant" and any(
        r.get("route_tier") == "compliant" for r in routes
    ):
        pick["reason"] = (
            f"{chosen_id} breaks the {vvip_class} lane floor while a compliant candidate "
            "exists — protocol is authoritative, tactical ranking kept"
        )
        return routes, pick
    reordered = [chosen] + [r for r in routes if r["route_id"] != chosen_id]
    pick["applied"] = True
    pick["reason"] = f"strategic tier promoted {chosen_id} over {current}"
    return reordered, pick


# ---------------------------------------------------------------------------
# Diversions (the coordinator narrates only a non-empty oracle plan)
# ---------------------------------------------------------------------------


async def _coordinate_diversions(
    coordinator: DiversionCoordinatorAgent,
    executor: ToolExecutor,
    *,
    movement_id: str,
    segment_ids: list[int],
    vvip_class: str,
    advance_closure_sec: int,
) -> dict:
    """Diversion directives for the final primary.

    The oracle's diversion plan is checked first: when it holds no diversion
    rows there is nothing to coordinate, and a tactical call would only
    narrate boilerplate closures the road graph never produced (it also cost
    ~40 s per plan). Otherwise the coordinator runs as before (it fetches the
    plan itself, with the same protocol-floored advance closure).
    """
    try:
        plan = await executor.execute("plan_diversions", {
            "route_segment_ids": segment_ids,
            "advance_closure_sec": advance_closure_sec,
        })
    except Exception as exc:
        logger.warning("pre_movement.diversion_plan_failed", error=str(exc))
        plan = None
    rows = plan.get("diversions") if isinstance(plan, dict) else None
    if isinstance(rows, list) and not rows:
        return {
            "directives": [],
            "queue_alerts": [],
            "overall_status": (
                "The oracle diversion planner returned no diversions for this route "
                "— no closures to coordinate."
            ),
            "confidence": "low",
            "_tier": "deterministic",
        }
    return await coordinator.activate_diversions(
        movement_id=movement_id,
        segment_ids=segment_ids,
        vvip_class=vvip_class,
    )


# ---------------------------------------------------------------------------
# Scenario evaluation (numeric tier)
# ---------------------------------------------------------------------------


async def _evaluate_scenarios(
    executor: ToolExecutor, routes: list[dict], planned_departure: str
) -> dict:
    """Compare the candidates' public disruption on the oracle's simulator.

    Scenario keys follow the oracle's ScenarioEvalRequest/scenario_sim
    contract (``scenario_id``, ``route_segment_ids``); ``route_id`` rides
    along as an alias. One shape in every case: ``{results, best, error}``.
    """
    if not routes:
        return {"results": [], "best": None, "error": None}
    scenarios = []
    for index, route in enumerate(routes):
        label = "primary" if index == 0 else f"alt-{index}"
        names = [n for n in route.get("road_names") or [] if n]
        scenarios.append({
            "scenario_id": route["route_id"],
            "route_id": route["route_id"],
            "route_segment_ids": route["segment_ids"],
            "route_name": label + (f": {names[0]} → {names[-1]}" if names else ""),
            "departure_time": planned_departure,
        })
    try:
        result = await executor.execute("evaluate_scenarios", {"scenarios": scenarios})
    except Exception as exc:
        logger.warning("pre_movement.scenarios_failed", error=str(exc))
        return {"results": [], "best": None, "error": str(exc)[:200]}
    rows = result.get("results", []) if isinstance(result, dict) else []
    rows = [r for r in rows if isinstance(r, dict)]
    best = None
    if rows:
        best = min(
            rows,
            key=lambda r: r.get("total_disruption_vehicle_hours")
            if isinstance(r.get("total_disruption_vehicle_hours"), (int, float)) else float("inf"),
        ).get("scenario_id")
    return {"results": rows, "best": best, "error": None}


# ---------------------------------------------------------------------------
# Finalize: plan document, context, city picture
# ---------------------------------------------------------------------------


async def _finalize(
    state: dict,
    ctx_store: ConvoyContextStore,
    city_link: CityLink | None,
    started: float,
) -> dict:
    """Compose the plan document (C-I), fuse it into the city picture, persist."""
    routes = state.get("route_candidates", {})
    primary = routes.get("primary_route")
    security = state.get("security_validation", {})
    diversions = state.get("diversion_plans", {})
    scenario = state.get("scenario_result", {})
    corridor = state.get("corridor_assessment", {})
    adaptive = state.get("adaptive")
    pick = state.get("decider_pick")

    # Precedence: a protocol violation outranks the decider's hold — waiting
    # does not widen a road, so a non-compliant plan stays 'conditional'
    # (operator waiver first) and the hold rides along in ``hold``.
    hold = bool(pick and pick.get("hold"))
    if primary is None:
        status = "no_route"
    elif not security.get("compliant", False):
        status = "conditional"
    elif hold:
        status = "hold"
    else:
        status = "approved"

    reasoning_parts = [str(routes.get("overall_reasoning") or "").strip()]
    if state.get("reroute_note"):
        reasoning_parts.append(state["reroute_note"])
    if status == "conditional" and not state.get("reroute_note") and security.get("violations"):
        reasoning_parts.append(
            "Conditional: "
            + hard_violation_summary(security["violations"], state["vvip_class"]) + "."
        )
    if pick and pick.get("reason"):
        if hold and status == "conditional":
            pick = dict(pick, reason=(
                "strategic tier chose to delay departure — recorded; the plan stays "
                "conditional until the protocol violations are waived"
            ))
        reasoning_parts.append("Decider: " + pick["reason"] + ".")
    basis = _plan_basis(state, primary, security, diversions, scenario, corridor)
    if basis:
        reasoning_parts.append(basis)
    overall_reasoning = " ".join(p for p in reasoning_parts if p)

    plan: dict[str, Any] = {
        "movement_id": state["movement_id"],
        "vvip_class": state["vvip_class"],
        "planned_departure": state.get("planned_departure"),
        "status": status,
        "hold": hold,
        "corridor_status": corridor.get("overall_status"),
        "corridor_assessment": {
            "overall_status": corridor.get("overall_status"),
            "recommendation": corridor.get("recommendation"),
            "observations": corridor.get("observations"),
            "segments": corridor.get("segments"),
            "tier": corridor.get("_tier"),
        },
        "primary_route": primary,
        "alternate_routes": routes.get("alternate_routes", []),
        "rejected_routes": routes.get("rejected_routes", []),
        "overall_reasoning": overall_reasoning,
        "routing_tier": routes.get("_tier"),
        "security_compliant": bool(security.get("compliant", False)) and primary is not None,
        "security_violations": security.get("violations", []),
        "security_warnings": security.get("warnings", []),
        "security_recommendations": security.get("recommendations", []),
        "security_score": security.get("security_score", 0.0),
        "diversion_directives": diversions.get("directives", []),
        "diversion_status": diversions.get("overall_status"),
        "scenario_comparison": scenario,
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
            "advance_closure_sec": state.get("advance_closure_sec"),
        } if adaptive else None,
        "decider_checkpoint": state.get("decider_checkpoint"),
        "decider_pick": (
            {k: pick[k] for k in ("route_id", "confidence", "tier", "choice", "applied", "reason")}
            if pick else None
        ),
        "tiers": {
            "corridor": corridor.get("_tier"),
            "routing": routes.get("_tier"),
            "security": security.get("_tier"),
            "diversions": diversions.get("_tier"),
            "decider": (state.get("decider_checkpoint") or {}).get("tier"),
        },
        # Why a tier fell to its deterministic floor (cut at the token cap,
        # unparseable reply, tactical tier down, repair skipped on budget).
        "tier_notes": {
            name: stage.get("_fallback_reason")
            for name, stage in (
                ("corridor", corridor), ("routing", routes),
                ("security", security), ("diversions", diversions),
            )
            if isinstance(stage, dict) and stage.get("_fallback_reason")
        },
        "security_reroutes": state.get("security_reroutes", []),
        "snap": _snap_summary(routes.get("snap")),
        "event_id": None,
        "city_event": None,
        "deadline": state.get("deadline"),
    }

    # A snapshot for the reads below only — the write is field-scoped.
    snapshot = await ctx_store.get(state["movement_id"])

    # -- fusion into the city picture (never fails the plan) -----------------
    previous_event = current_event_id(snapshot) if snapshot is not None else None
    registration: dict = {"status": "skipped", "event_id": None,
                          "reason": "city link not configured"}
    if city_link is not None:
        label = getattr(snapshot, "label", None)
        registration = await city_link.register_plan(
            plan,
            event_kind=(
                getattr(snapshot, "event_kind", "protected_movement") or "protected_movement"
            ),
            crowd_class=getattr(snapshot, "crowd_class", "LOW") or "LOW",
            advance_closure_sec=state.get("advance_closure_sec"),
            **({"label": label} if label else {}),
        )
    superseded = None
    if city_link is not None and previous_event and previous_event != registration.get("event_id"):
        superseded = await city_link.cancel_event(
            previous_event,
            note=(
                f"superseded by re-plan of movement {state['movement_id']}"
                + (
                    f" (new event {registration['event_id']})"
                    if registration.get("event_id") else ""
                )
            ),
        )
    plan["event_id"] = registration.get("event_id")
    plan["city_event"] = dict(registration, superseded=superseded)
    if snapshot is not None and getattr(snapshot, "label", None):
        plan["label"] = snapshot.label

    # -- context: ONE atomic field update ---------------------------------------
    # The rows are built BEFORE the mutation: ``apply`` may run more than once
    # (a lost WATCH race re-reads the fresh document and re-applies), so it
    # only assigns fields and appends these prepared rows.
    plan["timings_sec"] = dict(state.get("timings_sec") or {})
    plan["timings_sec"]["total"] = round(time.monotonic() - started, 2)
    selected_route_id = primary.get("route_id") if primary else None
    active_diversions = sorted({
        str(d["segment_id"]) for d in diversions.get("directives", [])
        if isinstance(d, dict) and d.get("action") == "activate" and d.get("segment_id") is not None
    })
    now = time.time()
    plan_row = {
        "phase": "pre_movement",
        "recommendation": status,
        "security_compliant": plan["security_compliant"],
        "primary_route_id": selected_route_id,
        "primary_segment_ids": list(primary.get("segment_ids", [])) if primary else [],
        "primary_road_names": (
            [n for n in primary.get("road_names", []) if n] if primary else []
        ),
        "alternate_route_ids": [r.get("route_id") for r in plan["alternate_routes"]],
        "decider_pick": plan["decider_pick"],
        "event_id": plan["event_id"],
        "timings_sec": plan["timings_sec"],
        "timestamp": now,
    }
    event_row = {
        "phase": "city_event",
        "action": registration.get("status"),
        "event_id": registration.get("event_id"),
        "reason": registration.get("reason"),
        "superseded_event_id": previous_event if superseded else None,
        "superseded_status": superseded.get("status") if superseded else None,
        "timestamp": now,
    }

    def apply(ctx: ConvoyContext) -> None:
        ctx.status = "planning" if status == "no_route" else status
        if adaptive is not None:
            ctx.situation_class = adaptive.situation_class
        ctx.selected_route_id = selected_route_id
        ctx.active_diversions = list(active_diversions)
        ctx.event_id = plan["event_id"]
        ctx.decisions_log.append(dict(plan_row))
        ctx.decisions_log.append(dict(event_row))

    await mutate_context(
        ctx_store,
        state["movement_id"],
        apply,
        create=lambda: ConvoyContext(
            movement_id=state["movement_id"], vvip_class=state["vvip_class"]
        ),
    )
    return plan


def _plan_basis(
    state: dict,
    primary: dict | None,
    security: dict,
    diversions: dict,
    scenario: dict,
    corridor: dict,
) -> str:
    """'Basis: …' — the concrete factors behind the plan, from the data.

    The lane floor and the primary's violations, the segments avoided on
    re-route, the forecast on the primary, the closure window and the
    detours, the least-disruptive scenario and the live feed's state. Every
    clause is omitted when its data is absent (nothing invented).
    """
    if primary is None:
        return ""
    vvip_class = state.get("vvip_class") or "?"
    parts: list[str] = []
    violations = [v for v in security.get("violations") or [] if isinstance(v, dict)]
    floor = min_lanes_for(vvip_class)
    if floor:
        parts.append(
            f"{vvip_class} lane floor {floor} lanes — "
            + (f"{len(violations)} segment(s) of the primary below it" if violations
               else "the primary meets it")
        )
    avoided = [str(s) for s in state.get("avoided_segments") or []]
    if avoided:
        parts.append("segments avoided on re-route: " + ", ".join(avoided[:8]))
    forecast = primary.get("forecast") if isinstance(primary.get("forecast"), dict) else None
    if forecast and forecast.get("mean_congestion") is not None:
        parts.append(
            f"forecast mean congestion {forecast['mean_congestion']:.2f} at "
            f"T+{forecast.get('horizon_min')} min"
            + (
                f" ({forecast['mean_speed_kmh']:.0f} km/h)"
                if forecast.get("mean_speed_kmh") else ""
            )
        )
    distance = primary.get("distance_m") or primary.get("total_distance_m")
    eta = primary.get("estimated_time_sec")
    if distance and eta:
        parts.append(f"primary {float(distance) / 1000:.1f} km, ETA {float(eta) / 60:.0f} min")
    directives = [d for d in diversions.get("directives") or [] if isinstance(d, dict)]
    closures = [d for d in directives if d.get("action") == "activate"]
    advance = state.get("advance_closure_sec")
    if closures:
        held = sum(
            1 for d in directives
            if d.get("detour_status") in ("impractical", "no_path", "dead_end")
        )
        parts.append(
            f"closures activate {advance} s before arrival on {len(closures)} segment(s)"
            + (f", {held} with no practical detour (hold / meter)" if held else "")
        )
    elif advance == 0:
        parts.append("signal priority only (0 s closure window)")
    # Only alerts with a numeric queue: a null or text estimate (a reply off
    # the grammar ladder) is no figure and must not fail the plan's finalize.
    queues = [
        q for q in diversions.get("queue_alerts") or []
        if isinstance(q, dict) and isinstance(q.get("estimated_queue_m"), (int, float))
        and not isinstance(q.get("estimated_queue_m"), bool)
    ]
    if queues:
        worst = max(queues, key=lambda q: q["estimated_queue_m"])
        parts.append(
            f"longest queue {worst['estimated_queue_m']:.0f} m on segment "
            f"{worst.get('segment_id')}"
        )
    rows = [r for r in scenario.get("results") or [] if isinstance(r, dict)]
    best = next((r for r in rows if r.get("scenario_id") == scenario.get("best")), None)
    if best and isinstance(best.get("total_disruption_vehicle_hours"), (int, float)):
        parts.append(
            f"least-disruptive scenario {best.get('scenario_id')} at "
            f"{best['total_disruption_vehicle_hours']:.1f} vehicle-hours"
        )
    observations = corridor.get("observations")
    if isinstance(observations, int):
        parts.append(
            f"corridor {corridor.get('overall_status', 'unknown')} on {observations} "
            "live observation(s)"
            if observations else "no live observations on the corridor (feed silent)"
        )
    return ("Basis: " + "; ".join(parts) + ".") if parts else ""


def _snap_summary(snap: Any) -> dict | None:
    if not isinstance(snap, dict) or not snap:
        return None
    return {
        "origin": snap.get("origin"),
        "destination": snap.get("destination"),
        "origin_segment": snap.get("origin_segment"),
        "destination_segment": snap.get("destination_segment"),
        "route_attempts": snap.get("route_attempts"),
        "error": snap.get("error"),
    }


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
