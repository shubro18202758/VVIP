"""Two-tier orchestrator tests — decider->Gemma->heuristics fallback chain."""

from __future__ import annotations

import json

import pytest

from convoy_brain.decider import DeciderBrain
from convoy_brain.llm.decider_client import DeciderAnswer, DeciderUnavailable
from convoy_brain.llm.llama_bridge import LlamaBridge, LlamaUnavailable
from convoy_brain.orchestrator import Orchestrator, _arguments_valid
from conftest import FakeExecutor, FakeContextStore


class ScriptDecider:
    """Returns scripted tool choices in order (last one repeats)."""

    def __init__(self, tool="find_convoy_routes", confidence=0.9, error=None):
        self.tools = [tool] if isinstance(tool, str) else list(tool)
        self.confidence = confidence
        self.error = error
        self._i = 0

    async def decide(self, state, questions, request_id=None):
        if self.error:
            raise self.error
        if self._i < len(self.tools) - 1:
            choice = self.tools[self._i]
            self._i += 1
        else:
            choice = self.tools[-1]
        return {
            name: DeciderAnswer(
                name=name, type=spec["type"],
                value=(choice if name == "next_tool" else 0.9),
                probability=0.9, confidence=self.confidence,
            )
            for name, spec in questions.items()
        }

    async def health(self, timeout_sec=3.0):
        return {"loaded": True, "p95_decide_latency_ms": 100.0}

    async def warmup(self):
        return True


class ScriptBridge:
    """Deterministic stand-in for the Gemma bridge."""

    def __init__(self, replies: list[str] | None = None, fail: bool = False):
        # replies are popped per generate() call in order
        self.replies = list(replies or [])
        self.fail = fail
        self.calls: list[dict] = []

    async def generate(self, system_prompt, user_prompt, json_mode=True,
                       suppress_thinking=True, **kwargs):
        self.calls.append({
            "system": system_prompt, "user": user_prompt,
            "schema": kwargs.get("schema_name"),
            "extra": kwargs.get("extra_messages") or [],
        })
        if self.fail:
            raise LlamaUnavailable("llama down")
        if self.replies:
            return self.replies.pop(0)
        return "{}"


async def run_turn(orchestrator, message="Plan a Z+ route from A to B"):
    session_id = "s1"
    await orchestrator.create_session(session_id, movement_id=None, vvip_class="Z")
    try:
        return await orchestrator.process_turn(session_id, message)
    finally:
        orchestrator.end_session(session_id)


FINAL_OK = {
    "action": "answered", "reasoning": "All clear on the corridor.", "tool_calls_made": [],
    "confidence": "high", "data": {}, "protocol_assessment": {
        "asl_status": "not_assessed", "protocol_violations": [],
        "threat_level": "nominal", "plan_b_ready": False},
}


class TestStrategicSelection:
    async def test_decider_picks_tool_gemma_drafts_args(self, fake_store):
        # Chained selection checkpoints inside one turn bypass the 5 s cadence
        # gap (one operator_chat event), so round 2 goes straight back to the
        # decider: reply script is [args, final] with no gemma-select turn.
        bridge = ScriptBridge(replies=[
            json.dumps({"arguments": {"origin_segment": 1001, "destination_segment": 1040}}),
            json.dumps({"action": "route_ready", "reasoning": "done", "tool_calls_made": [],
                        "confidence": "high", "data": {}, "protocol_assessment": {
                            "asl_status": "complete", "protocol_violations": [],
                            "threat_level": "nominal", "plan_b_ready": True}}),
        ])
        executor = FakeExecutor(routes={
            "find_convoy_routes": {"routes": [{"route_id": "r1", "segment_ids": [1, 2]}]},
        })
        orch = Orchestrator(bridge, executor, fake_store,
                            decider_brain=DeciderBrain(
                                ScriptDecider(["find_convoy_routes", "final_answer"])))
        result = await run_turn(orch)

        assert result["action"] == "route_ready"
        assert [c["name"] for c in result["tool_calls"]] == ["find_convoy_routes"]
        assert result["tool_calls"][0]["arguments"]["origin_segment"] == 1001
        # The executor saw the Gemma-drafted arguments
        assert executor.calls[0][1]["origin_segment"] == 1001
        assert "decider:decider" in result["tiers_used"]
        assert "gemma_args:gemma" in result["tiers_used"]

    async def test_decider_final_answer_skips_tools(self, fake_store):
        bridge = ScriptBridge(replies=[
            json.dumps({"action": "answer", "reasoning": "no tools needed",
                        "tool_calls_made": [], "confidence": "medium", "data": {},
                        "protocol_assessment": {"asl_status": "not_assessed",
                                                "protocol_violations": [],
                                                "threat_level": "nominal",
                                                "plan_b_ready": False}}),
        ])
        executor = FakeExecutor()
        orch = Orchestrator(bridge, executor, fake_store,
                            decider_brain=DeciderBrain(ScriptDecider("final_answer")))
        result = await run_turn(orch)
        assert result["action"] == "answer"
        assert executor.calls == []
        assert result["tool_calls"] == []


class TestFallbackChain:
    async def test_decider_down_falls_to_gemma_selection(self, fake_store):
        # Gemma tool-selection reply, then args, then final
        bridge = ScriptBridge(replies=[
            json.dumps({"tool_calls": [{"name": "get_live_traffic",
                                        "arguments": {"segment_ids": [5]}}]}),
            json.dumps({"arguments": {"segment_ids": [5]}}),
            json.dumps({"action": "traffic_report", "reasoning": "ok",
                        "tool_calls_made": [], "confidence": "medium", "data": {},
                        "protocol_assessment": {"asl_status": "not_assessed",
                                                "protocol_violations": [],
                                                "threat_level": "nominal",
                                                "plan_b_ready": False}}),
        ])
        executor = FakeExecutor(routes={"get_live_traffic": {"segments": []}})
        orch = Orchestrator(bridge, executor, fake_store,
                            decider_brain=DeciderBrain(
                                ScriptDecider(error=DeciderUnavailable("down"))))
        result = await run_turn(orch, "What is the current traffic on segment 5?")
        assert [c["name"] for c in result["tool_calls"]] == ["get_live_traffic"]
        assert "decider:gemma" in result["tiers_used"]  # decider tier fell back

    async def test_both_models_down_falls_to_heuristics(self, fake_store):
        bridge = ScriptBridge(fail=True)
        executor = FakeExecutor(routes={
            "get_live_traffic": {"segments": []},
        })
        orch = Orchestrator(bridge, executor, fake_store,
                            decider_brain=DeciderBrain(
                                ScriptDecider(error=DeciderUnavailable("down"))))
        result = await run_turn(orch, "What is the current traffic on the ring road?")
        # heuristic keyword selection picked a tool, heuristic arguments ran it
        assert [c["name"] for c in result["tool_calls"]] == ["get_live_traffic"]
        assert result["tiers_used"][-1] == "heuristic_final"
        assert result["confidence"] == "low"
        assert result["data"]["tier"] == "heuristic"

    async def test_repair_round_fixes_bad_arguments(self, fake_store):
        # Round-2 selection is a forced checkpoint (mid-turn chaining), so the
        # scripted decider moves to final_answer and the reply script stays
        # [bad args, repaired args, final].
        bridge = ScriptBridge(replies=[
            json.dumps({"arguments": {}}),  # missing required keys
            json.dumps({"arguments": {"origin_segment": 1, "destination_segment": 2}}),
            json.dumps({"action": "planned", "reasoning": "x", "tool_calls_made": [],
                        "confidence": "high", "data": {}, "protocol_assessment": {
                            "asl_status": "not_assessed", "protocol_violations": [],
                            "threat_level": "nominal", "plan_b_ready": False}}),
        ])
        executor = FakeExecutor(routes={"find_convoy_routes": {"routes": []}})
        orch = Orchestrator(bridge, executor, fake_store,
                            decider_brain=DeciderBrain(
                                ScriptDecider(["find_convoy_routes", "final_answer"])))
        result = await run_turn(orch)
        assert result["tool_calls"][0]["arguments"] == {
            "origin_segment": 1, "destination_segment": 2,
        }
        assert any(t.startswith("gemma_args:gemma") for t in result["tiers_used"])
        assert len(executor.calls) == 1  # repaired args executed exactly once


class TestAuditTrail:
    async def test_decision_logged_with_tier(self, fake_store):
        bridge = ScriptBridge(replies=[
            json.dumps({"action": "answered", "reasoning": "r", "tool_calls_made": [],
                        "confidence": "high", "data": {}, "protocol_assessment": {
                            "asl_status": "not_assessed", "protocol_violations": [],
                            "threat_level": "nominal", "plan_b_ready": False}}),
        ])
        orch = Orchestrator(bridge, FakeExecutor(), fake_store,
                            decider_brain=DeciderBrain(ScriptDecider("final_answer")))
        from convoy_brain.memory.convoy_context import ConvoyContext

        await fake_store.put(ConvoyContext(movement_id="m-42", vvip_class="Z"))
        await orch.create_session("s1", movement_id="m-42", vvip_class="Z")
        await orch.process_turn("s1", "status?")
        decisions = fake_store.contexts["m-42"].decisions_log
        assert any(d.get("phase") == "orchestrator_turn" for d in decisions)
        entry = next(d for d in decisions if d.get("phase") == "orchestrator_turn")
        assert entry["tiers_used"] and entry["confidence"]

    async def test_unknown_movement_is_not_bound_or_minted(self, fake_store):
        bridge = ScriptBridge(replies=[json.dumps(FINAL_OK)])
        orch = Orchestrator(bridge, FakeExecutor(), fake_store,
                            decider_brain=DeciderBrain(ScriptDecider("final_answer")))
        state = await orch.create_session("s1", movement_id="ghost", vvip_class="Z")
        await orch.process_turn("s1", "status?")
        assert state.movement_id is None
        assert "ghost" not in fake_store.contexts  # no empty "unknown" context

    async def test_low_confidence_decider_answer_alerts_operator(self, fake_store):
        bridge = ScriptBridge(replies=[
            json.dumps({"tool_calls": [{"name": "final_answer"}]}),  # Gemma confirms
            json.dumps({"action": "ok", "reasoning": "r", "tool_calls_made": [],
                        "confidence": "high", "data": {}, "protocol_assessment": {
                            "asl_status": "not_assessed", "protocol_violations": [],
                            "threat_level": "nominal", "plan_b_ready": False}}),
        ])
        orch = Orchestrator(bridge, FakeExecutor(), fake_store,
                            decider_brain=DeciderBrain(
                                ScriptDecider("final_answer", confidence=0.3)))
        result = await run_turn(orch)
        assert any(
            a["type"] == "low_confidence_decision"
            for a in result.get("operator_alerts", [])
        )


class TestArgumentValidation:
    def test_missing_required(self):
        schema = {"required": ["segment_ids"], "properties": {
            "segment_ids": {"type": "array"}}}
        assert not _arguments_valid({}, schema)
        assert _arguments_valid({"segment_ids": [1]}, schema)

    def test_type_mismatch(self):
        schema = {"required": ["segment_ids"], "properties": {
            "segment_ids": {"type": "array"}}}
        assert not _arguments_valid({"segment_ids": "1,2"}, schema)

    def test_extra_keys_allowed(self):
        schema = {"required": [], "properties": {}}
        assert _arguments_valid({"anything": True}, schema)


class TestTimeoutGuard:
    async def test_tool_error_is_contained(self, fake_store):
        from convoy_brain.mcp.server import ToolExecutionError

        bridge = ScriptBridge(replies=[
            json.dumps({"arguments": {"segment_ids": [1]}}),
            # Round-2 selection is a forced mid-turn checkpoint: the decider
            # script already answers final_answer, so the next reply is final.
            json.dumps({"action": "after_error", "reasoning": "tool failed, handled",
                        "tool_calls_made": [], "confidence": "low", "data": {},
                        "protocol_assessment": {"asl_status": "not_assessed",
                                                "protocol_violations": [],
                                                "threat_level": "nominal",
                                                "plan_b_ready": False}}),
        ])
        executor = FakeExecutor(routes={
            "get_live_traffic": ToolExecutionError("oracle 500"),
        })
        orch = Orchestrator(bridge, executor, fake_store,
                            decider_brain=DeciderBrain(
                                ScriptDecider(["get_live_traffic", "final_answer"])))
        result = await run_turn(orch, "traffic now please")
        # retried once, then the error result is fed into the final answer
        assert len(executor.calls) == 2
        assert result["action"] == "after_error"


class TestConfidenceGate:
    """A low-confidence decider pick needs Gemma's agreement (decider.py:20-21)."""

    async def test_agreement_acts_on_the_pick(self, fake_store):
        bridge = ScriptBridge(replies=[
            json.dumps({"tool_calls": [{"name": "get_live_traffic"}]}),  # agree
            json.dumps({"arguments": {"segment_ids": [7]}}),
            json.dumps({"tool_calls": []}),  # round 2: agree "enough data"
            json.dumps(FINAL_OK),
        ])
        executor = FakeExecutor(routes={"get_live_traffic": {"observations": []}})
        orch = Orchestrator(bridge, executor, fake_store, decider_brain=DeciderBrain(
            ScriptDecider(["get_live_traffic", "final_answer"], confidence=0.3)))
        result = await run_turn(orch, "live traffic on segment 7")
        assert [c["name"] for c in result["tool_calls"]] == ["get_live_traffic"]
        assert "decider:confirmed_by_gemma" in result["tiers_used"]
        assert not any(a["type"] == "decider_gemma_disagreement"
                       for a in result.get("operator_alerts", []))
        assert result["action"] == "answered" and result["degraded"] is False

    async def test_disagreement_prefers_gemma_and_is_audited(self, fake_store):
        bridge = ScriptBridge(replies=[
            json.dumps({"tool_calls": [{"name": "predict_traffic_flow"}]}),  # disagree
            json.dumps({"arguments": {"segment_ids": [7]}}),
            json.dumps({"tool_calls": []}),  # round 2: agree "enough data"
            json.dumps(FINAL_OK),
        ])
        executor = FakeExecutor(routes={"predict_traffic_flow": {"predictions": {}}})
        orch = Orchestrator(bridge, executor, fake_store, decider_brain=DeciderBrain(
            ScriptDecider(["get_live_traffic", "final_answer"], confidence=0.3)))
        result = await run_turn(orch, "traffic on segment 7 soon")
        assert [c["name"] for c in result["tool_calls"]] == ["predict_traffic_flow"]
        alert = next(a for a in result["operator_alerts"]
                     if a["type"] == "decider_gemma_disagreement")
        assert alert["decider_pick"] == "get_live_traffic"
        assert alert["gemma_pick"] == "predict_traffic_flow"


class TestRetryPolicy:
    async def test_4xx_is_not_retried(self, fake_store):
        from convoy_brain.mcp.server import ToolExecutionError

        bridge = ScriptBridge(replies=[
            json.dumps({"arguments": {"segment_ids": [1]}}),
            json.dumps(FINAL_OK),
        ])
        executor = FakeExecutor(routes={"get_live_traffic": ToolExecutionError(
            "oracle HTTP 422", status_code=422, retryable=False)})
        orch = Orchestrator(bridge, executor, fake_store, decider_brain=DeciderBrain(
            ScriptDecider(["get_live_traffic", "final_answer"])))
        await run_turn(orch, "traffic now please")
        assert len(executor.calls) == 1


class TestPromptAssembly:
    async def test_tactical_calls_carry_the_session_system_prompt(self, fake_store):
        from convoy_brain.mcp.prompts import TOOL_CALLING_INSTRUCTION

        bridge = ScriptBridge(replies=[
            json.dumps({"arguments": {"segment_ids": [1]}}),
            json.dumps(FINAL_OK),
        ])
        executor = FakeExecutor(routes={"get_live_traffic": {"observations": []}})
        orch = Orchestrator(bridge, executor, fake_store, decider_brain=DeciderBrain(
            ScriptDecider(["get_live_traffic", "final_answer"])))
        await run_turn(orch, "live traffic on segment 1")
        args_call = next(c for c in bridge.calls if c["schema"] == "tool_arguments")
        assert TOOL_CALLING_INSTRUCTION.strip()[:40] in args_call["system"]
        assert all(m["role"] != "system" for m in args_call["extra"])

    async def test_final_transcript_excludes_system_and_fits_budget(self, fake_store):
        from convoy_brain.mcp.prompts import VVIP_AGENT_SYSTEM_PROMPT
        from convoy_brain.orchestrator import PROMPT_TOKEN_BUDGET, estimate_prompt_tokens

        huge = {"observations": [
            {"segment_id": 1000 + i, "congestion_idx": 0.31, "speed_kmh": 40.5,
             "timestamp_ms": 1760000000000 + i, "lon": 72.5 + i / 1000, "lat": 23.0}
            for i in range(400)
        ]}
        bridge = ScriptBridge(replies=[
            json.dumps({"arguments": {"segment_ids": [1, 2]}}),
            json.dumps({"arguments": {"segment_ids": [3, 4]}}),
            json.dumps(FINAL_OK),
        ])
        executor = FakeExecutor(routes={"get_live_traffic": lambda a: huge})
        orch = Orchestrator(bridge, executor, fake_store, decider_brain=DeciderBrain(
            ScriptDecider(["get_live_traffic", "get_live_traffic", "final_answer"])))
        await run_turn(orch, "live traffic on segments 1 2 3 4")
        final = next(c for c in bridge.calls if c["schema"] == "orchestrator_final")
        transcript = final["extra"][0]["content"]
        assert VVIP_AGENT_SYSTEM_PROMPT[:200] not in transcript  # never duplicated
        assert transcript.startswith("[OPERATOR REQUEST]")
        assert transcript.count("[TOOL RESULT get_live_traffic]") == 2
        total = (estimate_prompt_tokens(final["system"]) + estimate_prompt_tokens(transcript)
                 + estimate_prompt_tokens(final["user"]))
        assert total <= PROMPT_TOKEN_BUDGET

    def test_token_estimate_is_conservative_for_json(self):
        from convoy_brain.orchestrator import estimate_prompt_tokens

        # 600 chars of observation JSON measured 309 tokens on llama-server.
        sample = json.dumps({"observations": [
            {"timestamp_ms": 1760000000000, "lon": 72.5798, "lat": 23.0254,
             "segment_id": 1015, "speed_kmh": 32.5, "congestion_idx": 0.41,
             "source": "fleet_gps", "data_quality": "real", "confidence": 0.9}] * 3})
        assert estimate_prompt_tokens(sample) >= 309


class TestSessions:
    async def test_open_session_resumes_and_resets_turn_state(self, fake_store):
        bridge = ScriptBridge(replies=[json.dumps(FINAL_OK), json.dumps(FINAL_OK)])
        orch = Orchestrator(bridge, FakeExecutor(), fake_store,
                            decider_brain=DeciderBrain(ScriptDecider("final_answer"),
                                                       min_interval_sec=0))
        first = await orch.open_session(None, None, "Z")
        await orch.process_turn(first.session_id, "one")
        again = await orch.open_session(first.session_id, None, None)
        assert again is first
        result = await orch.process_turn(first.session_id, "two")
        assert result["tool_calls"] == []  # per-turn audit, not cumulative
        assert first.turns == 2

    async def test_expired_session_is_replaced(self, fake_store, monkeypatch):
        import convoy_brain.orchestrator as orch_mod

        orch = Orchestrator(ScriptBridge(), FakeExecutor(), fake_store)
        state = await orch.open_session("client-sid-1")
        assert state.session_id == "client-sid-1"  # caller id kept when well-formed
        state.last_used -= orch_mod.SESSION_TTL_SEC + 1
        fresh = await orch.open_session("client-sid-1")
        assert fresh is not state
        assert fresh.session_id == "client-sid-1"

    async def test_switching_movement_starts_a_new_session(self, fake_store):
        from convoy_brain.memory.convoy_context import ConvoyContext

        await fake_store.put(ConvoyContext(movement_id="m-a", vvip_class="Z"))
        await fake_store.put(ConvoyContext(movement_id="m-b", vvip_class="Y"))
        orch = Orchestrator(ScriptBridge(), FakeExecutor(), fake_store)
        a = await orch.open_session(None, "m-a")
        same = await orch.open_session(a.session_id, None)  # omitted -> keep binding
        b = await orch.open_session(a.session_id, "m-b")
        assert same is a and b is not a
        assert b.movement_id == "m-b" and b.vvip_class == "Y"

    async def test_malformed_session_id_gets_a_fresh_uuid(self, fake_store):
        orch = Orchestrator(ScriptBridge(), FakeExecutor(), fake_store)
        state = await orch.open_session("bad id with spaces")
        assert state.session_id != "bad id with spaces"


class TestEventCallback:
    async def test_events_follow_the_turn(self, fake_store):
        events: list[tuple[str, dict]] = []

        async def sink(kind, data):
            events.append((kind, data))

        bridge = ScriptBridge(replies=[
            json.dumps({"arguments": {"segment_ids": [1]}}),
            json.dumps(FINAL_OK),
        ])
        executor = FakeExecutor(routes={"get_live_traffic": {"observations": []}})
        orch = Orchestrator(bridge, executor, fake_store, decider_brain=DeciderBrain(
            ScriptDecider(["get_live_traffic", "final_answer"])))
        await orch.create_session("s1")
        result = await orch.process_turn("s1", "traffic on segment 1", on_event=sink)
        kinds = [k for k, _ in events]
        assert kinds == ["thought", "thought", "tool_call", "tool_result", "thought", "thought"]
        assert events[0][1]["tier"] == "decider"
        assert result["tiers"] == ["decider:pplx-decider-v1-27b", "gemma:aditi-ps18-v7h"]

    async def test_a_failing_sink_never_breaks_the_turn(self, fake_store):
        async def sink(kind, data):
            raise RuntimeError("client went away")

        bridge = ScriptBridge(replies=[json.dumps(FINAL_OK)])
        orch = Orchestrator(bridge, FakeExecutor(), fake_store,
                            decider_brain=DeciderBrain(ScriptDecider("final_answer")))
        await orch.create_session("s1")
        result = await orch.process_turn("s1", "status?", on_event=sink)
        assert result["action"] == "answered"


class TestMovementGrounding:
    async def test_movement_context_carries_the_planned_segments(self, fake_store):
        from convoy_brain.memory.convoy_context import ConvoyContext

        ctx = ConvoyContext(movement_id="m-p", vvip_class="Z", selected_route_id="r1")
        ctx.decisions_log.append({
            "phase": "pre_movement", "recommendation": "approved",
            "primary_segment_ids": [1005, 1013],
            "primary_road_names": ["Relief Road", "Tilak Road"], "event_id": "e-1",
        })
        await fake_store.put(ctx)
        orch = Orchestrator(ScriptBridge(), FakeExecutor(), fake_store)
        state = await orch.create_session("s", movement_id="m-p")
        assert "Selected Route Segments: [1005, 1013]" in state.system_prompt
        assert "Relief Road → Tilak Road" in state.system_prompt
        assert "Registered Corridor Event: e-1" in state.answer_system_prompt


class TestFollowUpGrounding:
    async def test_follow_up_sees_the_previous_exchange(self, fake_store):
        class RecordingDecider(ScriptDecider):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.states: list[str] = []

            async def decide(self, state, questions, request_id=None):
                self.states.append(state)
                return await super().decide(state, questions, request_id)

        huge = {"observations": [
            {"segment_id": 1000 + i, "congestion_idx": 0.31, "speed_kmh": 40.5,
             "timestamp_ms": 1760000000000 + i} for i in range(400)
        ]}
        first_answer = dict(FINAL_OK, reasoning="Segment 1014 has 4 lanes and a 50 km/h limit.")
        bridge = ScriptBridge(replies=[
            json.dumps(first_answer),
            json.dumps({"arguments": {"segment_ids": [1014]}}),
            json.dumps(FINAL_OK),
        ])
        decider = RecordingDecider(["final_answer", "get_live_traffic", "final_answer"])
        orch = Orchestrator(bridge, FakeExecutor(routes={"get_live_traffic": lambda a: huge}),
                            fake_store, decider_brain=DeciderBrain(decider))
        await orch.create_session("s1")
        await orch.process_turn("s1", "Lane details for segment 1014")
        await orch.process_turn("s1", "Is that enough lanes for a Z+ movement?")

        assert "earlier_in_session" not in decider.states[0]
        assert "Segment 1014 has 4 lanes" in decider.states[1]
        final = [c for c in bridge.calls if c["schema"] == "orchestrator_final"][-1]
        transcript = final["extra"][0]["content"]
        assert transcript.startswith("[EARLIER IN THIS SESSION]")
        assert "Segment 1014 has 4 lanes" in transcript
        # the canonical floors sit next to the facts (Z+ needs 6 lanes)
        assert "minimum lanes: Z+ 6, Z 4, Y 2, X 1" in transcript


class TestPickConfidenceGate:
    async def test_confident_pick_with_low_auxiliary_score_is_acted_on(self, fake_store):
        """Live trace: next_tool 0.81 / decision_confidence 0.14 — the pick
        stands (no tactical override), and the auxiliary score's low reading
        is AUDIT material, not an operator alert (#105): no confirmation ran,
        nothing waits on a human. The decisions_log turn row carries it."""
        from convoy_brain.memory.convoy_context import ConvoyContext
        from convoy_brain.orchestrator import alert_text

        class SplitDecider(ScriptDecider):
            async def decide(self, state, questions, request_id=None):
                answers = await super().decide(state, questions, request_id)
                answers["next_tool"].confidence = 0.81
                answers["decision_confidence"].confidence = 0.14
                return answers

        bridge = ScriptBridge(replies=[json.dumps(FINAL_OK)])
        orch = Orchestrator(bridge, FakeExecutor(), fake_store,
                            decider_brain=DeciderBrain(SplitDecider("final_answer")))
        await fake_store.put(ConvoyContext(movement_id="m-aux", vvip_class="Z"))
        await orch.create_session("s1", movement_id="m-aux", vvip_class="Z")
        result = await orch.process_turn("s1", "is that enough lanes?")
        assert [c["schema"] for c in bridge.calls] == ["orchestrator_final"]  # no confirm call
        assert result["action"] == "answered"
        assert not any(a["type"] == "low_confidence_decision"
                       for a in result.get("operator_alerts", []))
        assert [alert_text(a) for a in result.get("operator_alerts", [])] == []
        assert result["aux_low_confidence"][0]["questions"] == {"decision_confidence": 0.14}
        assert result["aux_low_confidence"][0]["pick_confidence"] == 0.81
        log = fake_store.contexts["m-aux"].decisions_log
        turn = next(d for d in log if d.get("phase") == "orchestrator_turn")
        assert turn["aux_low_confidence"][0]["questions"] == {"decision_confidence": 0.14}
        assert turn["operator_alerts"] == 0
        assert not any(d.get("phase") == "operator_alert" for d in log)

    async def test_alert_text_says_confirmation_only_when_one_ran(self):
        from convoy_brain.orchestrator import alert_text

        base = {"type": "low_confidence_decision", "floor": 0.45,
                "questions": {"next_tool": 0.31}}
        assert alert_text(dict(base, confirmation_ran=True)).endswith(
            "tactical confirmation required")
        assert "tactical confirmation required" not in alert_text(base)
        assert "recorded for audit" in alert_text(base)

    async def test_low_pick_alert_marks_the_confirmation(self, fake_store):
        """Pick 0.3 < floor: the confirmation runs, and the alert says so."""
        from convoy_brain.orchestrator import alert_text

        bridge = ScriptBridge(replies=[
            json.dumps({"tool_calls": [{"name": "final_answer"}]}),  # Gemma confirms
            json.dumps(FINAL_OK),
        ])
        orch = Orchestrator(bridge, FakeExecutor(), fake_store,
                            decider_brain=DeciderBrain(
                                ScriptDecider("final_answer", confidence=0.3)))
        result = await run_turn(orch)
        texts = [alert_text(a) for a in result.get("operator_alerts", [])]
        assert texts and all(t.endswith("tactical confirmation required") for t in texts
                             if t.startswith("low_confidence_decision"))
