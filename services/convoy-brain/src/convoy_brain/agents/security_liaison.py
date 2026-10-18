"""Security Liaison Agent — Gemma 4 12B tactical tier.

Ensures convoy route plans comply with security protocols for each VVIP
classification level. Hard lane-count violations are computed deterministically
BEFORE the model call and are always authoritative — the model can add
context, never waive a hard violation.
"""

from __future__ import annotations

import json

import structlog

from convoy_brain.agents.base import AgentSpec, structured_agent_call
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.mcp.server import ToolExecutor

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
Only reference segment IDs present in the payload.

Respond with structured JSON:
{
  "compliant": bool,
  "violations": [{"segment_id": int, "rule": str, "detail": str, "severity": str}],
  "warnings": [{"segment_id": int, "concern": str}],
  "recommendations": [str],
  "security_score": float,
  "confidence": "high|medium|low"
}
"""

SPEC = AgentSpec(
    name="security_liaison",
    system_prompt=SECURITY_LIAISON_PROMPT,
    schema_name="security_liaison",
)


# Minimum lanes by protection class (sourced from the canonical protocol
# params; 'none' imposes no protectee lane floor — crowd safety governs).
_MIN_LANES = {"Z+": 6, "Z": 4, "Y": 2, "X": 1, "none": 0}


class SecurityLiaisonAgent:
    """Tactical agent for security protocol compliance validation."""

    def __init__(self, bridge: LlamaBridge, executor: ToolExecutor) -> None:
        self._bridge = bridge
        self._executor = executor
        logger.info("security_liaison_agent.init")

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

        min_lanes = _MIN_LANES.get(vvip_class, 2)

        # 1. Deterministic hard checks — lane counts from real segment data
        segment_details = []
        hard_violations = []

        for seg_id in route_segment_ids:
            try:
                details = await self._executor.execute(
                    "query_segment_details", {"segment_id": seg_id},
                )
            except Exception as exc:
                logger.warning(
                    "security_liaison.segment_lookup_failed",
                    segment_id=seg_id,
                    error=str(exc),
                )
                continue
            if isinstance(details, dict) and not details.get("error"):
                segment_details.append(details)
                lanes = details.get("lanes", 0)
                if isinstance(lanes, (int, float)) and lanes < min_lanes:
                    hard_violations.append({
                        "segment_id": seg_id,
                        "rule": "ROAD_WIDTH",
                        "detail": (
                            f"Segment has {lanes} lanes, minimum {min_lanes} "
                            f"required for {vvip_class}"
                        ),
                        "severity": "critical",
                    })

        def heuristic() -> dict:
            return {
                "compliant": len(hard_violations) == 0,
                "violations": hard_violations,
                "warnings": [],
                "recommendations": [
                    "Tactical analysis unavailable; only deterministic lane checks applied."
                ],
                "security_score": 0.0 if hard_violations else 0.5,
                "confidence": "low",
            }

        # 2. Gemma comprehensive review (heuristic floor)
        user_prompt = json.dumps({
            "task": "validate_route_security",
            "vvip_class": vvip_class,
            "min_lanes_required": min_lanes,
            "route_segment_ids": route_segment_ids,
            "segment_details": segment_details,
            "pre_detected_violations": hard_violations,
        }, default=str)

        result, tier = await structured_agent_call(
            self._bridge,
            SPEC,
            user_prompt,
            heuristic,
            repair_hint="keys: compliant (bool), violations[], warnings[], recommendations[], security_score (number), confidence",
        )

        # Hard violations are always included and always authoritative.
        existing_ids = {v.get("segment_id") for v in result.get("violations", [])}
        for hv in hard_violations:
            if hv["segment_id"] not in existing_ids:
                result.setdefault("violations", []).append(hv)
        if hard_violations:
            result["compliant"] = False
        result["_tier"] = tier
        return result
