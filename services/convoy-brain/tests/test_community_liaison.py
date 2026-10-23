"""Community Liaison Agent tests — enforcement tail of the watchlist flow.

Pins (C6/C0):
* the agent registers through the guarded agents/__init__ import;
* the deterministic heuristic floor routes rule_id -> the owning agency and
  keeps the emulated_anpr label in the directive context;
* the structured call carries an audit record shaped for the orchestrator's
  decisions_log and states that it never approves anything;
* a missing module degrades to None (not a broken package);
* C-N: an approved R_STOLEN_SIGHTING routes to police_control as pursuit
  support and NEVER reads as a fine — on the heuristic floor and on the
  model tier alike (a deterministic post-step), and a stale e-challan for
  the same plate is withheld, never served on the victim.
"""

from __future__ import annotations

import asyncio

import pytest


def test_agent_importable_via_package():
    from convoy_brain import agents

    assert agents.CommunityLiaisonAgent is not None
    assert "CommunityLiaisonAgent" in agents.__all__


def _approved_row(rule_id="R_SPD_SEGMENT", plate="GJ01EM1234"):
    return {
        "recommendation_id": "00000000-0000-0000-0000-000000000001",
        "plate": plate,
        "rule_id": rule_id,
        "recommendation": f"e-challan recommended: {plate} read at 95 km/h",
        "status": "approved",
        "decided_by": "Inspector R. Patel",
        "decided_at": "2026-11-05T05:30:00+00:00",
        "evidence": {"source": "emulated_anpr"},
    }


class _UnavailableBridge:
    """Bridge whose generate() always raises LlamaUnavailable -> floor."""

    async def generate(self, **kw):  # pragma: no cover - never reached
        from convoy_brain.llm.llama_bridge import LlamaUnavailable

        raise LlamaUnavailable("test bridge: model tier unavailable")


class TestHeuristicFloor:
    def test_routing_table_maps_rules_to_agencies(self):
        from convoy_brain.agents.community_liaison import (
            CommunityLiaisonAgent,
            _RULE_AGENCY,
        )

        agent = CommunityLiaisonAgent(_UnavailableBridge())
        result = asyncio.run(
            agent.draft_directives([_approved_row()])
        )
        assert result["_tier"] == "heuristic"
        d = result["directives"][0]
        assert d["agency"] == _RULE_AGENCY["R_SPD_SEGMENT"] == "traffic_police"
        assert d["vehicle_plate"] == "GJ01EM1234"
        assert "emulated_anpr" in d["context"]
        assert "Inspector R. Patel" in d["context"]

    def test_closed_corridor_routes_to_police_control(self):
        from convoy_brain.agents.community_liaison import CommunityLiaisonAgent

        agent = CommunityLiaisonAgent(_UnavailableBridge())
        result = asyncio.run(
            agent.draft_directives([_approved_row(rule_id="R_CLOSED_CORRIDOR_ENTRY")])
        )
        assert result["directives"][0]["agency"] == "police_control"

    def test_pursuit_event_adds_high_priority_police_directive(self):
        from convoy_brain.agents.community_liaison import CommunityLiaisonAgent

        agent = CommunityLiaisonAgent(_UnavailableBridge())
        pursuit = {
            "event_id": "00000000-0000-0000-0000-000000000009",
            "metadata": {"plate": "GJ27EM9001"},
        }
        result = asyncio.run(agent.draft_directives([], pursuit_event=pursuit))
        (d,) = result["directives"]
        assert d["agency"] == "police_control"
        assert d["priority"] == "high"
        assert d["vehicle_plate"] == "GJ27EM9001"

    def test_audit_record_is_decisions_log_shaped(self):
        from convoy_brain.agents.community_liaison import CommunityLiaisonAgent

        agent = CommunityLiaisonAgent(_UnavailableBridge())
        result = asyncio.run(agent.draft_directives([_approved_row()]))
        audit = result["audit"]
        assert audit["actor"] == "community_liaison"
        assert audit["action"] == "draft_agency_directives"
        assert audit["approved_input_count"] == 1
        assert "never approves" in audit["note"]

    def test_vehicles_only_no_person_fields_in_output(self):
        from convoy_brain.agents.community_liaison import CommunityLiaisonAgent

        agent = CommunityLiaisonAgent(_UnavailableBridge())
        result = asyncio.run(agent.draft_directives([_approved_row()]))
        import json

        blob = json.dumps(result).lower()
        for banned in ("face", "aadhaar", "driver_name", "person_name"):
            assert banned not in blob


def _stolen_row(plate="GJ27EM9001"):
    return {
        "recommendation_id": "00000000-0000-0000-0000-000000000002",
        "plate": plate,
        "rule_id": "R_STOLEN_SIGHTING",
        "recommendation": (
            "stolen vehicle (FIR 4401/2026) sighted at EM-CAM-03 — "
            "e-challan withheld, vehicle reported stolen"
        ),
        "status": "approved",
        "decided_by": "Inspector R. Patel",
        "decided_at": "2026-11-05T05:31:00+00:00",
        "evidence": {"source": "emulated_anpr", "fir_ref": "4401/2026", "challan_withheld": True},
    }


class _ReplyBridge:
    """Bridge answering one fixed JSON draft (the model tier)."""

    def __init__(self, reply: dict):
        self.reply = reply

    async def generate(self, **kw):
        import json

        return json.dumps(self.reply)


_FINE_RE = r"challan(?! withheld)|\bfine\b(?! to the owner)|penalt"


class TestStolenVehicleSighting:
    def test_routing_table_sends_it_to_police_control(self):
        from convoy_brain.agents.community_liaison import _RULE_AGENCY

        assert _RULE_AGENCY["R_STOLEN_SIGHTING"] == "police_control"

    def test_heuristic_floor_is_pursuit_support_never_a_fine(self):
        import re

        from convoy_brain.agents.community_liaison import CommunityLiaisonAgent

        agent = CommunityLiaisonAgent(_UnavailableBridge())
        result = asyncio.run(agent.draft_directives([_stolen_row()]))
        (d,) = result["directives"]
        assert d["agency"] == "police_control" and d["priority"] == "high"
        assert "pursuit" in d["action"] and "FIR 4401/2026" in d["action"]
        assert "e-challan withheld" in d["action"]
        assert not re.search(_FINE_RE, d["action"], re.IGNORECASE)
        assert "emulated_anpr" in d["context"]

    def test_a_model_draft_is_held_to_the_rule(self):
        import re

        from convoy_brain.agents.community_liaison import CommunityLiaisonAgent

        draft = {
            "directives": [
                {"agency": "traffic_police", "action": "serve the e-challan for the read",
                 "vehicle_plate": "GJ 27 EM 9001", "priority": "medium",
                 "context": "approved; source emulated_anpr"},
                {"agency": "traffic_police", "action": "issue e-challan for 95 km/h",
                 "vehicle_plate": "GJ01EM1234", "priority": "medium",
                 "context": "approved; source emulated_anpr"},
            ],
            "summary": "2 directives",
            "confidence": "high",
        }
        agent = CommunityLiaisonAgent(_ReplyBridge(draft))
        result = asyncio.run(agent.draft_directives([_stolen_row(), _approved_row()]))
        assert result["_tier"] != "heuristic"
        stolen, ordinary = result["directives"]
        assert stolen["agency"] == "police_control"
        assert not re.search(_FINE_RE, stolen["action"], re.IGNORECASE)
        assert "pursuit" in stolen["action"]
        # an ordinary vehicle's approved e-challan is untouched
        assert ordinary == draft["directives"][1]
        assert result["audit"]["stolen_guarded"] == 1

    def test_a_stale_e_challan_on_the_stolen_plate_is_withheld(self):
        import re

        from convoy_brain.agents.community_liaison import CommunityLiaisonAgent

        # a penal row approved BEFORE the stolen earmark, beside the sighting
        stale = _approved_row(plate="GJ27EM9001")
        agent = CommunityLiaisonAgent(_UnavailableBridge())
        result = asyncio.run(agent.draft_directives([stale, _stolen_row()]))
        assert result["directives"], "the sighting still reaches police control"
        for d in result["directives"]:
            assert d["agency"] == "police_control"
            assert not re.search(_FINE_RE, d["action"], re.IGNORECASE), d["action"]
        # the rewrite does not leave two identical hand-offs; the one kept
        # carries the sighting's (high) priority
        keys = [(d["agency"], d["vehicle_plate"], d["action"]) for d in result["directives"]]
        assert len(keys) == len(set(keys)) == 1
        assert result["directives"][0]["priority"] == "high"

