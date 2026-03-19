"""Security Liaison Agent — ensures convoy route plans comply with
security protocols for each VVIP classification level.

VVIP Classes (Indian protocol):
    Z+ : Highest threat level — PM, President, visiting heads of state
    Z  : High threat — cabinet ministers, chief ministers
    Y  : Medium threat — senior officials, governors
    X  : Standard — VIPs requiring traffic coordination
"""

from __future__ import annotations

import json

import structlog

from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.ollama_bridge import OllamaBridge

logger = structlog.get_logger(__name__)

SECURITY_LIAISON_PROMPT = """\
You are the Security Liaison agent for the VVIP Convoy Orchestration Platform.
Your role is to validate route plans against security protocols.

Security rules by VVIP class:
- Z+: Minimum 6-lane roads, no market areas, flyovers preferred, full closure required
- Z: Minimum 4-lane roads, avoid narrow corridors, partial closure acceptable
- Y: Standard roads acceptable, speed restriction sufficient on most segments
- X: Minimal restrictions, signal priority only

You validate:
1. ROAD WIDTH: Route segments meet minimum lane requirements
2. CHOKEPOINTS: Identify and flag narrow points, underpasses, market areas
3. ALTERNATIVE EXITS: Ensure escape routes exist at regular intervals
4. CROWD RISK: Flag segments near stadiums, markets, protest-prone areas

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

# Minimum lanes by VVIP class
_MIN_LANES = {"Z+": 6, "Z": 4, "Y": 2, "X": 1}


class SecurityLiaisonAgent:
    """LLM agent for security protocol compliance validation."""

    def __init__(self, bridge: OllamaBridge, executor: ToolExecutor) -> None:
        self._bridge = bridge
        self._executor = executor
        self._system_prompt = SECURITY_LIAISON_PROMPT
        logger.info("security_liaison_agent.init")

    async def validate_route(
        self,
        route_segment_ids: list[int],
        vvip_class: str,
    ) -> dict:
        """Validate a proposed route against security protocols.

        Returns compliance assessment with violations and recommendations.
        """
        logger.info(
            "security_liaison.validate",
            segments=len(route_segment_ids),
            vvip_class=vvip_class,
        )

        min_lanes = _MIN_LANES.get(vvip_class, 2)

        # 1. Fetch detailed attributes for each segment
        segment_details = []
        hard_violations = []

        for seg_id in route_segment_ids:
            details = await self._executor.execute(
                "query_segment_details", {"segment_id": seg_id},
            )
            if isinstance(details, dict) and not details.get("error"):
                segment_details.append(details)
                # Pre-check: hard lane-count violation
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

        # 2. Ask LLM for comprehensive security review
        user_prompt = json.dumps({
            "task": "validate_route_security",
            "vvip_class": vvip_class,
            "min_lanes_required": min_lanes,
            "route_segment_ids": route_segment_ids,
            "segment_details": segment_details,
            "pre_detected_violations": hard_violations,
        }, default=str)

        response = await self._bridge.generate(
            system_prompt=self._system_prompt,
            user_prompt=user_prompt,
        )

        try:
            result = json.loads(response)
            # Ensure hard violations are always included
            existing_ids = {
                v.get("segment_id") for v in result.get("violations", [])
            }
            for hv in hard_violations:
                if hv["segment_id"] not in existing_ids:
                    result.setdefault("violations", []).append(hv)
            # Override compliance if hard violations exist
            if hard_violations:
                result["compliant"] = False
            return result
        except json.JSONDecodeError:
            logger.warning("security_liaison.parse_failed", raw_len=len(response))
            return {
                "compliant": len(hard_violations) == 0,
                "violations": hard_violations,
                "warnings": [],
                "recommendations": ["LLM analysis failed; only automated lane checks applied."],
                "security_score": 0.0 if hard_violations else 0.5,
                "confidence": "low",
            }
