"""Community Liaison Agent tests — enforcement tail of the watchlist flow.

Pins (C6/C0):
* the agent registers through the guarded agents/__init__ import;
* the deterministic heuristic floor routes rule_id -> the owning agency and
  keeps the emulated_anpr label in the directive context;
* the structured call carries an audit record shaped for the orchestrator's
  decisions_log and states that it never approves anything;
* a missing module degrades to None (not a broken package).
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
