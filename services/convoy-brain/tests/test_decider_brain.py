"""DeciderBrain tests — cadence, budgets, degradation, confidence gating."""

from __future__ import annotations

import pytest

from convoy_brain.decider import (
    CHECKPOINT_TRIGGERS,
    DeciderBrain,
    estimate_tokens,
)
from convoy_brain.llm.decider_client import DeciderAnswer, DeciderUnavailable


class FlakyDeciderClient:
    """Configurable stand-in for the DeciderClient."""

    def __init__(self, answers=None, error: Exception | None = None,
                 p95_ms: float = 100.0) -> None:
        self.answers = answers or {}
        self.error = error
        self.decide_calls = 0
        self._p95_ms = p95_ms

    @property
    def p95_decide_latency_ms(self) -> float:
        return self._p95_ms

    async def decide(self, state, questions, request_id=None):
        self.decide_calls += 1
        if self.error:
            raise self.error
        return {
            name: DeciderAnswer(
                name=name,
                type=spec["type"],
                value=self.answers.get(name, {}).get("value", "final_answer"),
                probability=0.9,
                confidence=self.answers.get(name, {}).get("confidence", 0.95),
            )
            for name, spec in questions.items()
        }

    async def health(self, timeout_sec=3.0):
        return {"loaded": True, "p95_decide_latency_ms": self._p95_ms}

    async def warmup(self):
        return True


def questions() -> dict:
    return {
        "next_tool": {
            "type": "choice",
            "instructions": "pick",
            "options": ["find_convoy_routes", "final_answer"],
        },
    }


class TestCadence:
    async def test_rate_limit_blocks_second_immediate_checkpoint(self):
        brain = DeciderBrain(FlakyDeciderClient())
        first = await brain.checkpoint("m1", "operator_chat", "state", questions())
        assert first.tier == "decider"
        second = await brain.checkpoint("m1", "operator_chat", "state", questions())
        assert second.tier == "rate_limited"
        assert second.degraded_reason and "rate-limited" in second.degraded_reason

    async def test_force_bypasses_rate_limit(self):
        brain = DeciderBrain(FlakyDeciderClient())
        await brain.checkpoint("m1", "operator_chat", "state", questions())
        forced = await brain.checkpoint(
            "m1", "phase_transition", "state", questions(), force=True
        )
        assert forced.tier == "decider"

    async def test_per_movement_independence(self):
        brain = DeciderBrain(FlakyDeciderClient())
        await brain.checkpoint("m1", "operator_chat", "state", questions())
        other = await brain.checkpoint("m2", "operator_chat", "state", questions())
        assert other.tier == "decider"

    async def test_unknown_trigger_rejected(self):
        brain = DeciderBrain(FlakyDeciderClient())
        with pytest.raises(ValueError, match="trigger"):
            await brain.checkpoint("m1", "per_tick", "state", questions())


class TestStateBudgets:
    def test_estimate_tokens(self):
        assert estimate_tokens("x" * 400) == 100

    def test_event_state_capped_at_400_tokens(self):
        doc = DeciderBrain.build_state_doc(
            trigger="incident_detected",
            summary="word " * 5000,
        )
        assert estimate_tokens(doc) <= 400

    def test_pre_movement_state_gets_1500_budget(self):
        doc = DeciderBrain.build_state_doc(
            trigger="pre_movement",
            summary="word " * 1000,
            pre_movement=True,
        )
        assert 400 < estimate_tokens(doc) <= 1500

    def test_hard_cap(self):
        doc = DeciderBrain.cap_state_doc("x" * 100_000, 8192)
        assert estimate_tokens(doc) <= 8192


class TestDegradationAndGating:
    async def test_decider_failure_degrades_and_falls_back(self):
        client = FlakyDeciderClient(error=DeciderUnavailable("connection refused"))
        brain = DeciderBrain(client)
        result = await brain.checkpoint("m1", "operator_chat", "state", questions())
        assert result.tier == "gemma"
        assert brain.degraded
        # Subsequent checkpoints short-circuit without calling the client.
        calls_before = client.decide_calls
        await brain.checkpoint("m1", "operator_chat", "state", questions(), force=True)
        assert client.decide_calls == calls_before

    async def test_p95_degradation_via_health(self):
        class SlowP95Client(FlakyDeciderClient):
            """p95 above the degrade line; ``error`` decides probe outcome."""

            def __init__(self, error: Exception | None = None):
                super().__init__(p95_ms=4500.0, error=error)

            async def health(self, timeout_sec=3.0):
                payload = await super().health()
                payload["p95_decide_latency_ms"] = 4500.0
                return payload

        # p95 breach + failing probe -> degrade on fresh bad evidence.
        brain = DeciderBrain(SlowP95Client(error=DeciderUnavailable("probe timeout")))
        await brain.refresh_health()
        assert brain.degraded
        result = await brain.checkpoint("m1", "operator_chat", "state", questions())
        assert result.tier == "gemma"

        # p95 breach + fast healthy probe -> the stale server-side window
        # must NOT bench the strategic tier (sticky-latch regression guard:
        # a degraded brain sends no decides, so its p95 never refreshes).
        brain2 = DeciderBrain(SlowP95Client())
        await brain2.refresh_health()
        assert not brain2.degraded
        result2 = await brain2.checkpoint("m2", "operator_chat", "state", questions())
        assert result2.tier == "decider"

    async def test_low_confidence_triggers_alert_and_gemma_draft_flag(self):
        client = FlakyDeciderClient(answers={
            "next_tool": {"value": "find_convoy_routes", "confidence": 0.42},
        })
        brain = DeciderBrain(client)
        result = await brain.checkpoint("m1", "operator_chat", "state", questions())
        assert result.tier == "decider"
        assert result.audit["needs_gemma_draft"] is True
        assert any(a["type"] == "low_confidence_decision" for a in result.operator_alerts)

    async def test_high_confidence_no_alert(self):
        client = FlakyDeciderClient(answers={
            "next_tool": {"value": "find_convoy_routes", "confidence": 0.93},
        })
        brain = DeciderBrain(client)
        result = await brain.checkpoint("m1", "operator_chat", "state", questions())
        assert result.audit["needs_gemma_draft"] is False
        assert result.operator_alerts == []

    async def test_audit_record_has_distribution_fields(self):
        client = FlakyDeciderClient(answers={
            "next_tool": {"value": "get_live_traffic", "confidence": 0.9},
        })
        brain = DeciderBrain(client)
        result = await brain.checkpoint("m1", "operator_chat", "state", questions())
        answers = result.audit["answers"]["next_tool"]
        assert answers["value"] == "get_live_traffic"
        assert "probability" in answers and "confidence" in answers
        assert result.audit["tier"] == "decider"


class TestQuestionBuilders:
    def test_tool_selection_includes_menu_and_final(self):
        questions = DeciderBrain.tool_selection_question(["get_live_traffic"])
        assert set(questions) == {"next_tool", "decision_confidence"}
        assert questions["next_tool"]["type"] == "choice"
        options = questions["next_tool"]["options"]
        assert "get_live_traffic" in options and "final_answer" in options
        assert questions["decision_confidence"]["type"] == "score"

    def test_pre_movement_questions(self):
        questions = DeciderBrain.pre_movement_questions(["primary", "alternate"])
        assert set(questions) == {"route_strategy", "urgency", "disruption_tolerance"}
        assert questions["route_strategy"]["options"] == ["primary", "alternate"]

    def test_triggers_cover_contract_f8(self):
        for trigger in (
            "incident_detected",
            "congestion_threshold_crossed",
            "situation_changed",
            "phase_transition",
            "operator_chat",
            "pre_movement",
        ):
            assert trigger in CHECKPOINT_TRIGGERS
