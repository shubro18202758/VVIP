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

STOLEN VEHICLES (C-N): an approved R_STOLEN_SIGHTING is a sighting of a
vehicle reported stolen — evidence for police control's (human-initiated)
pursuit, NEVER a fine: the registered owner is the victim, so the oracle
withholds every e-challan.  The routing table sends it to police_control,
and a deterministic post-step holds every tier to that: any directive for a
plate with an approved stolen sighting goes to police_control and carries
no challan / fine wording, whatever the model drafted.
"""

from __future__ import annotations

import json
import re
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
- R_STOLEN_SIGHTING is a STOLEN vehicle's sighting: route it to
  police_control as pursuit support (pursuits are human-initiated). It is
  NEVER a fine — the e-challan is withheld because the vehicle is reported
  stolen; never direct a challan or a fine to a stolen vehicle's owner.
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

#: The oracle's evidence rule for a vehicle reported stolen (C-N).
STOLEN_SIGHTING_RULE = "R_STOLEN_SIGHTING"

#: Rule id -> the agency that owns serving it (deterministic routing table).
#: A stolen vehicle's sighting is police control's (pursuit support), never
#: traffic police's challan desk.
_RULE_AGENCY = {
    "R_SPD_SEGMENT": "traffic_police",
    "R_SIGNAL_VIOLATION": "traffic_police",
    "R_CLOSED_CORRIDOR_ENTRY": "police_control",
    STOLEN_SIGHTING_RULE: "police_control",
}

#: Penalty wording (a stolen car's owner is the victim): a directive for a
#: stolen plate that mentions one at all gets the deterministic hand-off.
_PENALTY_RE = re.compile(r"challan|\bfines?\b|\bfined\b|penalt", re.IGNORECASE)


_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2}


def _plate_key(plate: object) -> str:
    return re.sub(r"[\s-]", "", str(plate or "")).upper()


def _fir_of(row: dict[str, Any]) -> str | None:
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    fir = evidence.get("fir_ref")
    return str(fir) if fir else None


def stolen_sighting_action(row: dict[str, Any]) -> str:
    """The hand-off for an approved R_STOLEN_SIGHTING: pursuit support, no fine."""
    fir = _fir_of(row)
    return (
        f"stolen vehicle sighted{f' (FIR {fir})' if fir else ''} — police control: "
        "support the human-initiated pursuit from this sighting; e-challan "
        "withheld (vehicle reported stolen), no fine to the owner"
    )


def guard_stolen_directives(
    directives: list[dict[str, Any]], approved: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Hold any tier's directives to C-N (pure).

    A directive for a plate with an approved R_STOLEN_SIGHTING is routed to
    police_control, and challan / fine wording on it is replaced by the
    pursuit-support hand-off (an e-challan approved before the earmark is
    withheld, never served on the victim).  Exact duplicates the rewrite
    creates are dropped.  Returns (directives, rewritten count).
    """
    stolen = {
        _plate_key(r.get("plate")): r for r in approved
        if str(r.get("rule_id")) == STOLEN_SIGHTING_RULE and r.get("plate")
    }
    if not stolen:
        return directives, 0
    out: list[dict[str, Any]] = []
    kept: dict[tuple[str, str, str], int] = {}
    rewritten = 0
    for d in directives:
        if not isinstance(d, dict):
            continue
        row = stolen.get(_plate_key(d.get("vehicle_plate")))
        if row is not None:
            action = str(d.get("action") or "")
            fixed = dict(d)
            fixed["agency"] = "police_control"
            if _PENALTY_RE.search(action):
                fixed["action"] = stolen_sighting_action(row)
            if fixed != d:
                rewritten += 1
            d = fixed
        key = (str(d.get("agency")), _plate_key(d.get("vehicle_plate")), str(d.get("action")))
        if key in kept:
            # one hand-off per plate and action, at the higher priority
            first = out[kept[key]]
            if _PRIORITY_RANK.get(str(d.get("priority")), 0) > _PRIORITY_RANK.get(
                str(first.get("priority")), 0
            ):
                first["priority"] = d.get("priority")
            continue
        kept[key] = len(out)
        out.append(d)
    return out, rewritten


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
                rule = str(row.get("rule_id"))
                agency = _RULE_AGENCY.get(rule, "traffic_police")
                stolen = rule == STOLEN_SIGHTING_RULE
                directives.append(
                    {
                        "agency": agency,
                        "action": (
                            stolen_sighting_action(row) if stolen
                            else str(row.get("recommendation", "approved e-challan"))
                        ),
                        "vehicle_plate": str(row.get("plate", "unknown")),
                        "priority": "high" if stolen else "medium",
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
        # C-N on every tier: a stolen vehicle's plate is never handed a fine
        directives, stolen_guarded = guard_stolen_directives(
            list(result.get("directives") or []), approved
        )
        result["directives"] = directives
        result["audit"] = {
            "actor": "community_liaison",
            "action": "draft_agency_directives",
            "approved_input_count": len(approved),
            "pursuit_included": pursuit_event is not None,
            "tier": tier,
            "stolen_guarded": stolen_guarded,
            "note": (
                "inputs were already human-approved; this agent drafts the "
                "hand-off only — it never approves or escalates a penalty"
            ),
        }
        return result


__all__ = [
    "AGENCIES",
    "SPEC",
    "STOLEN_SIGHTING_RULE",
    "CommunityLiaisonAgent",
    "guard_stolen_directives",
    "stolen_sighting_action",
]
