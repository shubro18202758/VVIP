"""POST /chat/stream — contract C-D over the real FastAPI route.

The audit reproduced the deck's only chat path running no tool and
streaming '{"tool_calls": []}' as the answer: the stream drove Gemma
directly (no decider, no argument validation, no audit) and the system
message carrying the tool-calling instructions was dropped. The stream is
now a thin NDJSON adapter over Orchestrator.process_turn; these tests read
the wire exactly like the deck's parser does.
"""

from __future__ import annotations

import json

import pytest

from convoy_brain import __main__ as brain_main
from convoy_brain.config import DECIDER_MODEL_ID, GEMMA_MODEL_ID
from convoy_brain.decider import DeciderBrain
from convoy_brain.memory.convoy_context import ConvoyContext
from convoy_brain.orchestrator import Orchestrator, chunk_prose, final_prose
from conftest import FakeContextStore, FakeExecutor
from test_orchestrator import ScriptBridge, ScriptDecider

FINAL = {
    "action": "traffic_report",
    "reasoning": "Segments 1012 and 1015 are free-flowing at about 41 km/h; "
                 "1005 has no live reading.",
    "tool_calls_made": ["get_live_traffic"],
    "confidence": "medium",
    "data": {},
    "protocol_assessment": {"asl_status": "not_assessed", "protocol_violations": [],
                            "threat_level": "nominal", "plan_b_ready": False},
}
LIVE = {"observations": [
    {"segment_id": 1012, "congestion_idx": 0.2, "speed_kmh": 42.0},
    {"segment_id": 1015, "congestion_idx": 0.25, "speed_kmh": 40.0},
]}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("MODEL_HOST_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("LLAMA_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("TRAFFIC_ORACLE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("EMBEDDING_SERVICE_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(brain_main, "ConvoyContextStore", lambda valkey_url: FakeContextStore())
    return brain_main.create_app()


def stream(client, body: dict) -> list[dict]:
    with client.stream("POST", "/chat/stream", json=body) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        lines = [line for line in response.iter_lines() if line.strip()]
    # compact wire form: the acceptance grep for '"type":"tool_call"' matches
    assert all(line.startswith('{"type":"') for line in lines)
    return [json.loads(line) for line in lines]


def wire(app, bridge, executor, decider, store=None):
    store = store or app.state.context_store
    app.state.orchestrator = Orchestrator(
        bridge, executor, store, decider_brain=DeciderBrain(decider, min_interval_sec=0)
    )
    return store


def tokens(events: list[dict]) -> str:
    return "".join(e["data"] for e in events if e["type"] == "token")


class TestStreamContract:
    def test_tool_round_trip_and_prose(self, app):
        from starlette.testclient import TestClient

        with TestClient(app) as client:
            executor = FakeExecutor(routes={"get_live_traffic": LIVE})
            wire(app, ScriptBridge(replies=[
                json.dumps({"arguments": {"segment_ids": [1012, 1015, 1005]}}),
                json.dumps(FINAL),
            ]), executor, ScriptDecider(["get_live_traffic", "final_answer"]))
            events = stream(client, {
                "message": "What is the live traffic on segments 1012, 1015 and 1005?"
            })

        kinds = [e["type"] for e in events]
        assert kinds[-1] == "done"
        assert kinds.index("tool_call") < kinds.index("tool_result") < kinds.index("token")

        thoughts = [e["data"] for e in events if e["type"] == "thought"]
        assert thoughts and all(
            set(t) >= {"stepIndex", "text", "tier"} for t in thoughts
        )
        assert [t["stepIndex"] for t in thoughts] == list(range(1, len(thoughts) + 1))
        assert {t["tier"] for t in thoughts} <= {"decider", "gemma", "heuristic"}
        assert thoughts[0]["tier"] == "decider"

        call = next(e["data"] for e in events if e["type"] == "tool_call")
        assert call["name"] == "get_live_traffic"
        assert call["args"] == {"segment_ids": [1012, 1015, 1005]}
        # legacy keys the current deck parser reads
        assert call["toolName"] == "get_live_traffic" and call["callId"] == call["id"]
        assert call["arguments"] == call["args"] and call["state"] == "running"
        assert executor.calls == [("get_live_traffic", {"segment_ids": [1012, 1015, 1005]})]

        result = next(e["data"] for e in events if e["type"] == "tool_result")
        assert result["ok"] is True and result["id"] == call["id"]
        assert "2 observation(s)" in result["summary"]
        assert result["result"] == LIVE
        assert result["state"] == "success" and isinstance(result["durationMs"], int)

        text = tokens(events)
        assert text == FINAL["reasoning"]
        assert not text.lstrip().startswith("{")
        assert all(isinstance(e["data"], str) for e in events if e["type"] == "token")

        done = events[-1]["data"]
        assert done["session_id"]
        assert done["tiers_used"][0] == "decider:" + DECIDER_MODEL_ID
        assert "gemma:" + GEMMA_MODEL_ID in done["tiers_used"]
        assert done["degraded"] is False
        assert done["operator_alerts"] == []

    def test_json_inside_reasoning_is_unwrapped_never_streamed_raw(self, app):
        from starlette.testclient import TestClient

        wrapped = dict(FINAL, reasoning=json.dumps({"answer": "Corridor is clear."}))
        with TestClient(app) as client:
            wire(app, ScriptBridge(replies=[json.dumps(wrapped)]), FakeExecutor(),
                 ScriptDecider("final_answer"))
            events = stream(client, {"message": "status?"})
        assert tokens(events) == "Corridor is clear."

    def test_tactical_failure_streams_honest_deterministic_prose(self, app):
        from starlette.testclient import TestClient

        with TestClient(app) as client:
            executor = FakeExecutor(routes={"get_live_traffic": {"observations": []}})
            wire(app, ScriptBridge(fail=True), executor,
                 ScriptDecider(["get_live_traffic", "final_answer"]))
            events = stream(client, {"message": "live traffic on segments 1012 and 1015?"})
        text = tokens(events)
        assert not text.startswith("{")
        assert "no live observations for segments 1012, 1015" in text
        assert "Both model tiers" not in text  # the decider was up
        done = events[-1]["data"]
        assert done["degraded"] is True
        assert "heuristic" in done["tiers_used"]
        assert done["tiers_used"][0].startswith("decider:")

    def test_low_confidence_alert_is_a_string(self, app):
        from starlette.testclient import TestClient

        with TestClient(app) as client:
            wire(app, ScriptBridge(replies=[
                json.dumps({"tool_calls": [{"name": "final_answer"}]}),  # Gemma agrees
                json.dumps(FINAL),
            ]), FakeExecutor(), ScriptDecider("final_answer", confidence=0.3))
            events = stream(client, {"message": "status?"})
        done = events[-1]["data"]
        assert done["operator_alerts"]
        assert all(isinstance(a, str) for a in done["operator_alerts"])
        assert done["operator_alerts"][0].startswith("low_confidence_decision")
        thoughts = [e["data"]["text"] for e in events if e["type"] == "thought"]
        assert any("agrees" in t for t in thoughts)

    def test_session_continues_with_history(self, app):
        from starlette.testclient import TestClient

        bridge = ScriptBridge(replies=[json.dumps(FINAL), json.dumps(FINAL)])
        with TestClient(app) as client:
            wire(app, bridge, FakeExecutor(), ScriptDecider("final_answer"))
            first = stream(client, {"message": "first question"})
            sid = first[-1]["data"]["session_id"]
            second = stream(client, {"message": "follow-up", "session_id": sid})
            assert second[-1]["data"]["session_id"] == sid
            state = app.state.orchestrator.get_session(sid)
            assert state is not None and state.turns == 2
            roles = [m.role for m in state.messages]
            assert roles.count("user") == 2 and roles.count("assistant") == 2

    def test_movement_turn_is_audited(self, app):
        from starlette.testclient import TestClient

        with TestClient(app) as client:
            store = app.state.context_store
            store.contexts["mv-9"] = ConvoyContext(movement_id="mv-9", vvip_class="Z")
            wire(app, ScriptBridge(replies=[json.dumps(FINAL)]), FakeExecutor(),
                 ScriptDecider("final_answer"))
            stream(client, {"message": "status?", "movement_id": "mv-9"})
            rows = store.contexts["mv-9"].decisions_log
        turns = [r for r in rows if r.get("phase") == "orchestrator_turn"]
        assert len(turns) == 1
        assert turns[0]["tiers"][0].startswith("decider:")

    def test_failure_becomes_an_error_event(self, app):
        from starlette.testclient import TestClient

        class Broken(Orchestrator):
            async def process_turn(self, session_id, user_input, *, on_event=None):
                raise RuntimeError("context store offline")

        with TestClient(app) as client:
            app.state.orchestrator = Broken(ScriptBridge(), FakeExecutor(),
                                            app.state.context_store)
            events = stream(client, {"message": "status?"})
        assert events == [{"type": "error", "data": "context store offline"}]


class TestProseHelpers:
    def test_chunks_rejoin_exactly(self):
        text = "Segments 1012 and 1015 are free-flowing; 1005 has no live reading."
        chunks = chunk_prose(text, size=12)
        assert len(chunks) > 1 and "".join(chunks) == text

    def test_final_prose_never_returns_json(self):
        raw = final_prose({"action": "x", "reasoning": '{"tool_calls": []}'})
        assert raw.startswith("No narrative")
        assert final_prose({"action": "x", "reasoning": "Plain answer."}) == "Plain answer."
