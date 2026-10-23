"""Security Liaison Agent — Gemma 4 12B tactical tier.

Ensures convoy route plans comply with security protocols for each VVIP
classification level. Hard lane-count violations are computed deterministically
BEFORE the model call and are always authoritative — the model can add
context, never waive a hard violation.

Compliance is a property of the data, not of the narrative: a route is
compliant iff every segment's attributes could be fetched and every segment
meets the protection class's lane floor (``MIN_LANES``, the canonical
protocol table). The tactical review is advisory — its findings ride along as
warnings and recommendations; a "violation" the model raises that the
segment attributes do not support is kept as a labelled, model-raised
warning instead of flipping the verdict (the payload carries no market,
chokepoint or crowd data the model could have verified). The security score
is likewise grounded: lane-floor compliance blended with the road-class mix,
on a 0..1 scale; the model's own number is kept for audit only.

:func:`fetch_segment_facts` / :func:`lane_audit` are shared with the route
planner, which audits every candidate before ranking (one segment-details
fetch per segment per plan, through the workflow's cache).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from convoy_brain.agents.base import AgentSpec, structured_agent_call
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.protocol_params_gen import MIN_LANES

logger = structlog.get_logger(__name__)

SECURITY_LIAISON_PROMPT = """\
You are the Security Liaison agent for the Corridor Event Orchestration Platform.
Your role is to validate route plans against protection and safety protocols.

Security rules by protection class:
- Z+: Minimum 6-lane roads, no market areas, flyovers preferred, full closure required
- Z: Minimum 4-lane roads, avoid narrow corridors, partial closure acceptable
- Y: Standard roads acceptable, speed restriction sufficient on most segments
- X: Minimal restrictions, signal priority only
- none (public event): no protectee; validate crowd-safety instead — hardened
  perimeters, staged ingress/egress capacity, emergency-vehicle lanes

You validate:
1. ROAD WIDTH: Route segments meet minimum lane requirements (protected classes)
2. CHOKEPOINTS: Identify and flag narrow points, underpasses, market areas
3. ALTERNATIVE EXITS: Ensure escape routes exist at regular intervals
4. CROWD RISK: Flag segments near stadiums, markets, protest-prone areas —
   critical for processions, gatherings, races and egress surges

Pre-detected lane violations arrive in the payload and are FINAL — a route
with any critical violation is non-compliant regardless of your narrative.
Only reference segment IDs present in the payload, and only claim what the
segment attributes in the payload support.

Respond with ONE compact single-line JSON object — no line breaks, no
indentation; at most 6 violations, 4 warnings and 3 recommendations, each
text under 120 characters:
{"compliant":bool,"violations":[{"segment_id":int,"rule":str,"detail":str,"severity":"low|medium|high|critical"}],"warnings":[{"segment_id":int,"concern":str}],"recommendations":[str],"security_score":float,"confidence":"high|medium|low"}
"""

SPEC = AgentSpec(
    name="security_liaison",
    system_prompt=SECURITY_LIAISON_PROMPT,
    schema_name="security_liaison",
)


#: Road-class security weight on a 0..1 scale (the oracle router's
#: ROAD_CLASS_SCORES / 100 — wider, access-controlled classes score higher).
_ROAD_CLASS_SECURITY = {
    "motorway": 1.0,
    "trunk": 0.85,
    "primary": 0.7,
    "secondary": 0.5,
    "tertiary": 0.3,
    "residential": 0.1,
}

#: Segment attributes kept for audits and model prompts (geometry dropped —
#: it is bulky and carries nothing the lane rule needs).
_FACT_KEYS = (
    "segment_id", "road_name", "road_class", "lanes",
    "speed_limit_kmh", "oneway", "length_m",
)


def min_lanes_for(vvip_class: str) -> int:
    """Lane floor for a protection class (canonical MIN_LANES; unknown -> 2)."""
    return int(MIN_LANES.get(vvip_class, 2))


async def fetch_segment_facts(
    executor: ToolExecutor,
    segment_ids: list[int],
    cache: dict[int, dict] | None = None,
) -> dict[int, dict]:
    """Fetch per-segment attributes once per workflow (concurrent, cached).

    Failures are recorded as ``{"segment_id": sid, "error": ...}`` rows so
    the lane audit can mark the segment unverified instead of silently
    treating it as compliant.
    """
    store = cache if cache is not None else {}
    wanted = [int(s) for s in dict.fromkeys(segment_ids) if s is not None]
    missing = [sid for sid in wanted if sid not in store]

    async def one(sid: int) -> tuple[int, dict]:
        try:
            details = await executor.execute("query_segment_details", {"segment_id": sid})
        except Exception as exc:
            logger.warning(
                "security_liaison.segment_lookup_failed", segment_id=sid, error=str(exc)
            )
            return sid, {"segment_id": sid, "error": str(exc)[:200]}
        if not isinstance(details, dict) or details.get("error"):
            return sid, {"segment_id": sid, "error": "segment details unavailable"}
        facts = {key: details.get(key) for key in _FACT_KEYS}
        facts["segment_id"] = sid
        return sid, facts

    if missing:
        for sid, facts in await asyncio.gather(*(one(s) for s in missing)):
            store[sid] = facts
    return {sid: store[sid] for sid in wanted if sid in store}


def lane_audit(
    facts: dict[int, dict],
    segment_ids: list[int],
    vvip_class: str,
) -> dict:
    """Deterministic protocol check of one route against the lane floor.

    Returns ``{compliant, violations, warnings, unverified, security_score,
    min_lanes}``. Violations are critical ROAD_WIDTH rows (and high-severity
    UNVERIFIED rows for segments whose attributes could not be fetched).
    One-way segments draw a warning for Z+/Z (escort and reserve vehicles
    cannot double back along them).
    """
    min_lanes = min_lanes_for(vvip_class)
    violations: list[dict] = []
    warnings: list[dict] = []
    unverified: list[int] = []
    class_scores: list[float] = []
    lane_ok = 0
    known = 0
    for sid in segment_ids:
        row = facts.get(int(sid)) if sid is not None else None
        lanes = row.get("lanes") if isinstance(row, dict) else None
        if not isinstance(row, dict) or row.get("error") or not isinstance(lanes, (int, float)):
            unverified.append(sid)
            violations.append({
                "segment_id": sid,
                "rule": "UNVERIFIED",
                "detail": (
                    f"Segment {sid}: attributes could not be fetched — the "
                    f"{min_lanes}-lane floor for {vvip_class} is unverified"
                ),
                "severity": "high",
            })
            continue
        known += 1
        name = row.get("road_name") or f"segment {sid}"
        class_scores.append(_ROAD_CLASS_SECURITY.get(str(row.get("road_class")), 0.5))
        if lanes < min_lanes:
            violations.append({
                "segment_id": sid,
                "rule": "ROAD_WIDTH",
                "detail": (
                    f"Segment {sid} ({name}) has {int(lanes)} lanes, minimum "
                    f"{min_lanes} required for {vvip_class}"
                ),
                "severity": "critical",
                "lanes": int(lanes),
                "min_lanes": min_lanes,
                "road_name": row.get("road_name"),
            })
        else:
            lane_ok += 1
        if row.get("oneway") and vvip_class in ("Z+", "Z"):
            warnings.append({
                "segment_id": sid,
                "concern": (
                    f"{name} is one-way — escort and reserve vehicles cannot "
                    f"double back along it; stage the Plan B exit elsewhere"
                ),
            })

    if known:
        score = 0.6 * (lane_ok / len(segment_ids)) + 0.4 * (sum(class_scores) / known)
    else:
        score = 0.0
    return {
        "compliant": bool(segment_ids) and not violations,
        "violations": violations,
        "warnings": warnings,
        "unverified": unverified,
        "security_score": round(score, 3),
        "min_lanes": min_lanes,
    }


def hard_violation_summary(violations: list[dict], vvip_class: str) -> str:
    """Operator-facing one-liner for a set of deterministic violations."""
    width = [v for v in violations if v.get("rule") == "ROAD_WIDTH"]
    unverified = [v for v in violations if v.get("rule") == "UNVERIFIED"]
    parts = []
    if width:
        ids = ", ".join(str(v.get("segment_id")) for v in width)
        parts.append(
            f"{len(width)} segment(s) below the {min_lanes_for(vvip_class)}-lane "
            f"floor for {vvip_class} ({ids})"
        )
    if unverified:
        parts.append(f"{len(unverified)} segment(s) unverified")
    return "; ".join(parts) if parts else "no protocol violations"


class SecurityLiaisonAgent:
    """Tactical agent for security protocol compliance validation."""

    def __init__(
        self,
        bridge: LlamaBridge,
        executor: ToolExecutor,
        *,
        segment_cache: dict[int, dict] | None = None,
    ) -> None:
        self._bridge = bridge
        self._executor = executor
        self._segment_cache = segment_cache if segment_cache is not None else {}
        logger.info("security_liaison_agent.init")

    async def audit_route(self, route_segment_ids: list[int], vvip_class: str) -> dict:
        """Deterministic-only validation (no model call) — the re-route loop's verdict."""
        facts = await fetch_segment_facts(
            self._executor, route_segment_ids, self._segment_cache
        )
        audit = lane_audit(facts, route_segment_ids, vvip_class)
        return {
            "compliant": audit["compliant"],
            "violations": audit["violations"],
            "warnings": audit["warnings"],
            "recommendations": [],
            "security_score": audit["security_score"],
            "confidence": "high" if not audit["unverified"] else "medium",
            "_tier": "deterministic",
        }

    async def validate_route(
        self,
        route_segment_ids: list[int],
        vvip_class: str,
    ) -> dict:
        """Validate a proposed route against security protocols."""
        logger.info(
            "security_liaison.validate",
            segments=len(route_segment_ids),
            vvip_class=vvip_class,
        )

        # 1. Deterministic hard checks — lane counts from real segment data
        facts = await fetch_segment_facts(
            self._executor, route_segment_ids, self._segment_cache
        )
        audit = lane_audit(facts, route_segment_ids, vvip_class)
        hard_violations = audit["violations"]
        segment_details = [
            facts[int(s)] for s in route_segment_ids
            if s is not None and int(s) in facts and not facts[int(s)].get("error")
        ]

        def heuristic() -> dict:
            return {
                "compliant": audit["compliant"],
                "violations": hard_violations,
                "warnings": audit["warnings"],
                "recommendations": [
                    "Tactical review unavailable; deterministic lane-floor and "
                    "one-way checks applied."
                ],
                "security_score": audit["security_score"],
                "confidence": "medium",
            }

        # 2. Gemma comprehensive review (heuristic floor)
        user_prompt = json.dumps({
            "task": "validate_route_security",
            "vvip_class": vvip_class,
            "min_lanes_required": audit["min_lanes"],
            "route_segment_ids": route_segment_ids,
            "segment_details": segment_details,
            "pre_detected_violations": hard_violations,
            "pre_detected_warnings": audit["warnings"],
        }, default=str)

        result, tier = await structured_agent_call(
            self._bridge,
            SPEC,
            user_prompt,
            heuristic,
            repair_hint="keys: compliant (bool), violations[], warnings[], recommendations[], security_score (number), confidence",
        )
        return _merge_review(result, tier, audit)


def _merge_review(result: dict, tier: str, audit: dict) -> dict:
    """Fold the tactical review onto the deterministic audit (audit wins)."""
    hard = audit["violations"]
    hard_ids = {v.get("segment_id") for v in hard}
    warnings: list[dict] = list(audit["warnings"])
    seen = {(w.get("segment_id"), w.get("concern")) for w in warnings}

    def add_warning(segment_id: Any, concern: str) -> None:
        key = (segment_id, concern)
        if concern and key not in seen:
            seen.add(key)
            warnings.append({"segment_id": segment_id, "concern": concern})

    if tier != "heuristic":
        for row in result.get("warnings", []) or []:
            if isinstance(row, dict):
                add_warning(row.get("segment_id"), str(row.get("concern") or ""))
        for row in result.get("violations", []) or []:
            if not isinstance(row, dict) or row.get("segment_id") in hard_ids:
                continue
            # Model-raised, not supported by the segment attributes: advisory.
            detail = row.get("detail") or row.get("rule") or "concern"
            add_warning(
                row.get("segment_id"),
                f"model-raised (unverified): {row.get('rule', 'RULE')} — {detail}",
            )

    recommendations = [
        str(r) for r in (result.get("recommendations") or []) if isinstance(r, (str, int, float))
    ]
    merged = {
        "compliant": audit["compliant"],
        "violations": hard,
        "warnings": warnings,
        "recommendations": recommendations,
        "security_score": audit["security_score"],
        "confidence": result.get("confidence") if result.get("confidence") in (
            "high", "medium", "low"
        ) else "medium",
        "_tier": tier,
    }
    model_score = result.get("security_score")
    if tier != "heuristic" and isinstance(model_score, (int, float)) and not isinstance(
        model_score, bool
    ):
        # The model mixes 0..1 and 0..100 scales; normalize, keep for audit only.
        merged["model_security_score"] = round(
            float(model_score) / 100.0 if model_score > 1.0 else float(model_score), 3
        )
    return merged
