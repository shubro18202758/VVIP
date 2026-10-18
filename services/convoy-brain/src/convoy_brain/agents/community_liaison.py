"""Community Liaison Agent — Jan Rakshak / police coordination tier.

The human-approval tail of the enforcement flow (watchlist-anpr stream,
contract C0/C6): it takes APPROVED e-challan recommendations (a human
officer has already decided — nothing here approves anything) plus any
watchlist context and drafts the agency directive that hands the outcome
to the street-level actor (Jan Rakshak volunteer, traffic police, police
control room).

Like every tactical agent it follows the shared pattern (agents/base.py):
    1. deterministic context assembly (the approved rows, verbatim);
    2. one schema-constrained Gemma call to draft the directive;
    3. one repair round;
    4. deterministic heuristic floor when the model tier is unavailable.

AUDIT CHAIN: the agent's return carries an ``audit`` record (actor, action,
inputs, tier) shaped for the orchestrator's decisions_log — the same
append-only discipline as the recommendation audit tail.  The orchestrator
logs whatever this agent returns; the agent itself never mutates state.

ETHICS (C6/C10): vehicles only; every emulated input is source-labelled;
directives reference plates and events, never persons; the framing is
pro-government capability (assist the officer, never replace judgment).
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from convoy_brain.agents.base import AgentSpec, structured_agent_call
from convoy_brain.llm.llama_bridge import LlamaBridge

logger = structlog.get_logger(__name__)

COMMUNITY_LIAISON_PROMPT = """\
You are the Community Liaison agent for the Corridor Event Orchestration
Platform. Your role is to convert APPROVED enforcement recommendations and
watchlist context into clear, lawful hand-off directives for street-level
agency actors.

Actors you coordinate: Jan Rakshak volunteers (street patrol, citizen
assistance), traffic police (challan service, junction control), police
control room (pursuit support — pursuits are ALWAYS human-initiated).

Hard rules:
- Only APPROVED recommendations reach you — an approved e-challan is the
  result of a human officer decision. You never approve, reject, or
  escalate a penalty yourself.
- Directives reference VEHICLES (plates, sightings) and EVENTS, never
  persons. No face, name, or identity inference.
- Every emulated input carries source 'emulated_anpr' — keep that label
  in the directive so no reader mistakes it for a live feed.
- Pro-government framing: your directive ASSISTS the agency actor.

Respond with structured JSON:
{
  "directives": [{"agency": "jan_rakshak|traffic_police|police_control",
                  "action": str, "vehicle_plate": str, "priority": "low|medium|high",
                  "context": str}],
  "summary": str,
  "confidence": "high|medium|low"
}
"""

SPEC = AgentSpec(
    name="community_liaison",
    system_prompt=COMMUNITY_LIAISON_PROMPT,
    schema_name="community_liaison",
)

#: Agencies the liaison may direct (deterministic floor uses these).
AGENCIES = ("jan_rakshak", "traffic_police", "police_control")

#: Rule id -> the agency that owns serving it (deterministic routing table).
_RULE_AGENCY = {
    "R_SPD_SEGMENT": "traffic_police",
    "R_SIGNAL_VIOLATION": "traffic_police",
    "R_CLOSED_CORRIDOR_ENTRY": "police_control",
}


class CommunityLiaisonAgent:
    """Tactical agent drafting agency directives from APPROVED outcomes."""

    def __init__(self, bridge: LlamaBridge) -> None:
        self._bridge = bridge
        logger.info("community_liaison_agent.init")

    async def draft_directives(
        self,
        approved: list[dict[str, Any]],
        *,
        pursuit_event: dict[str, Any] | None = None,
    ) -> dict:
        """Draft hand-off directives for already-APPROVED recommendations.

        ``approved``: rows in the GET /api/v1/enforcement/recommendations?
        status=approved shape (plate, rule_id, recommendation, evidence,
        decided_by, decided_at).  ``pursuit_event``: optional
        vehicle_pursuit corridor event (human-initiated) to include.

        Returns the agent payload plus the audit record for the brain's
        decisions_log (orchestrator audit chain).
        """
        logger.info(
            "community_liaison.draft",
            approved=len(approved),
            pursuit=pursuit_event is not None,
        )

        def heuristic() -> dict:
            directives = []
            for row in approved:
                agency = _RULE_AGENCY.get(str(row.get("rule_id")), "traffic_police")
                directives.append(
                    {
                        "agency": agency,
                        "action": str(row.get("recommendation", "approved e-challan")),
                        "vehicle_plate": str(row.get("plate", "unknown")),
                        "priority": "medium",
                        "context": (
                            f"approved by {row.get('decided_by', 'officer')} at "
                            f"{row.get('decided_at', 'unknown time')}; evidence "
                            "source emulated_anpr"
                        ),
                    }
                )
            if pursuit_event is not None:
                directives.append(
                    {
                        "agency": "police_control",
                        "action": (
                            "support human-initiated pursuit — interception "
                            "plan available from the oracle"
                        ),
                        "vehicle_plate": str(
                            (pursuit_event.get("metadata") or {}).get(
                                "plate", "unknown"
                            )
                        ),
                        "priority": "high",
                        "context": (
                            f"pursuit event {pursuit_event.get('event_id')}; "
                            "human-initiated per policy"
                        ),
                    }
                )
            return {
                "directives": directives,
                "summary": (
                    f"{len(directives)} directive(s) drafted from "
                    f"{len(approved)} approved recommendation(s)"
                ),
                "confidence": "low",
            }

        user_prompt = json.dumps(
            {
                "task": "draft_agency_directives",
                "approved_recommendations": approved,
                "pursuit_event": pursuit_event,
                "agencies": list(AGENCIES),
                "rule_agency_routing": _RULE_AGENCY,
            },
            default=str,
        )

        result, tier = await structured_agent_call(
            self._bridge,
            SPEC,
            user_prompt,
            heuristic,
            repair_hint=(
                "keys: directives[] with agency/action/vehicle_plate/"
                "priority/context, summary, confidence"
            ),
        )
        result["_tier"] = tier
        result["audit"] = {
            "actor": "community_liaison",
            "action": "draft_agency_directives",
            "approved_input_count": len(approved),
            "pursuit_included": pursuit_event is not None,
            "tier": tier,
            "note": (
                "inputs were already human-approved; this agent drafts the "
                "hand-off only — it never approves or escalates a penalty"
            ),
        }
        return result


__all__ = ["CommunityLiaisonAgent", "SPEC", "AGENCIES"]
