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
        await orch.create_session("s1", movement_id="m-42", vvip_class="Z")
        await orch.process_turn("s1", "status?")
        decisions = fake_store.contexts["m-42"].decisions_log
        assert any(d.get("phase") == "orchestrator_turn" for d in decisions)
        entry = next(d for d in decisions if d.get("phase") == "orchestrator_turn")
        assert entry["tiers_used"] and entry["confidence"]

    async def test_low_confidence_decider_answer_alerts_operator(self, fake_store):
        bridge = ScriptBridge(replies=[
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
