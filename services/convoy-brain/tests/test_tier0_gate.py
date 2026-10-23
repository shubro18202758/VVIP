"""Decider tier-0 gate tests — routine checkpoints served with ZERO 27B calls.

Coverage:
    (A) Gate decisions: routine hit (decisive assertion: the scripted 27B
        host's decide_calls stays at the priming count — zero additional
        /decide calls), answers/audit shape + origin provenance, cadence
        consumption; cross-movement isolation (a different movement's
        first-ever checkpoint NEVER hits another movement's entry — the
        embedding service ranks by movement but does not filter on it);
        changed-but-known situations below the routine floor miss (the
        routine bar is strictly tighter than the novelty floor); novelty
        forces a miss (Wave-1 semantics preserved end-to-end); TTL expiry
        forces a miss via the injected clock (no wall-clock dependence);
        kill switch preserves today-behavior exactly; ineligible trigger
        (operator_chat) never consults memory nor cache; memory outage /
        all-dense results fail OPEN (decide called, never degraded, honest
        audit row); config.tier0_enabled env parsing + floor separation.
    (B) Write side: low-confidence, latched (novel), and unavailable-memory
        checkpoints never store; max-entries FIFO eviction; a stored
        pre_movement answer is also filed for the movement's own decision
        docs, so its first recurrence hits whichever own doc matches
        (escort triggers keep their filing; movement scoping, the exact
        question set, the TTL and the FIFO bound hold).
    (C) Metrics: REGISTRY.get_sample_value DELTAS across a hit (counters are
        process-global and never reset between tests — absolute values are
        meaningless; this convention is new to this file); below-routine
        misses get their own outcome label; the decide-duration histogram
        gains no observation from a hit (seeded first, so non-vacuous).
    (D) Integration: the live-escort incident path persists a
        tier0_cache_hit audit row to decisions_log; the second incident
        checkpoint within the TTL is served by the gate with no additional
        /decide call for that checkpoint.

Reuses the test_novelty_latch.py fakes by import (ScriptedDecider counts
decides; FakeMemoryClient counts /search calls; ScriptedClock drives TTL).
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from prometheus_client import REGISTRY

from convoy_brain import config
from convoy_brain.config import TIER0_ELIGIBLE_TRIGGERS, TIER0_ROUTINE_SIMILARITY_FLOOR
from convoy_brain.decider import DeciderBrain, _is_own_decision_ref

from conftest import FakeExecutor
from test_novelty_latch import (
    FakeMemoryClient,
    ScriptedClock,
    ScriptedDecider,
    brain_with,
    memory_ok,
    questions,
)

# The eligible trigger used throughout: incident_detected (operator_chat is
# excluded in v1 — the orchestrator trusts only tier 'decider').
TRIGGER = "incident_detected"


def _metric(name: str, labels: dict[str, str] | None = None) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return value if value is not None else 0.0


async def _prime(brain: DeciderBrain, memory: FakeMemoryClient, *, state: str = "s1"):
    """One real covered+confident checkpoint that stores into the cache."""
    first = await brain.checkpoint("mv-1", TRIGGER, state, questions())
    assert first.tier == "decider"
    assert memory.calls >= 1
    return first


# ─── (A) Gate decisions ───────────────────────────────────────────────────────


class TestGateDecisions:
    async def test_routine_hit_makes_zero_decide_calls(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88, doc_id="doc-7")])
        scripted = ScriptedDecider(confidence=0.95)
        brain = DeciderBrain(
            scripted, memory_client=memory, min_interval_sec=0, clock=ScriptedClock(1000.0)
        )
        first = await _prime(brain, memory)
        decide_after_prime = scripted.decide_calls
        assert decide_after_prime == 1
        assert brain._tier0_cache  # the priming checkpoint stored an entry

        second = await brain.checkpoint(
            "mv-1", TRIGGER, "s2 (same situation kind, fresh per-tick values)",
            questions(), force=True,
        )
        assert second.tier == "tier0_cache_hit"
        # DECISIVE: the mocked 27B host received ZERO calls for the hit.
        assert scripted.decide_calls == decide_after_prime
        # Answers are the cached ones, rebuilt as DeciderAnswer objects
        # (consumers do result.answers.get('next_tool').value).
        answer = second.answers.get("next_tool")
        assert answer is not None and answer.value == first.answers["next_tool"].value
        assert answer.probability == first.answers["next_tool"].probability
        assert answer.confidence == 0.95
        assert answer.type == "choice"
        # A hit is not an escalation.
        assert second.operator_alerts == []
        assert second.degraded_reason is None
        row = second.audit["tier0_cache"]
        assert row["status"] == "hit"
        assert row["best_doc_id"] == "doc-7"
        assert row["best_kind"] == "incident"
        assert row["floor"] == TIER0_ROUTINE_SIMILARITY_FLOOR
        assert row["age_sec"] is not None and row["age_sec"] >= 0
        assert row["latency_saved_ms"] is not None
        # Provenance: the row identifies which stored decide produced the
        # served answers (auditor can diff the serving state_doc's hash
        # against the priming one).
        assert row["origin_state_doc_sha256"] is not None
        assert len(row["origin_state_doc_sha256"]) == 64
        assert row["origin_timestamp"] is not None and row["origin_timestamp"] > 0
        assert second.audit["tier"] == "tier0_cache_hit"
        assert second.audit["needs_gemma_draft"] is False
        assert second.audit["answers"]["next_tool"]["confidence"] == 0.95
        # decider_checkpoint rows keep ONE shape: the hit row carries the
        # similarity leg it actually ran (covered), same key as miss rows.
        assert second.audit["novelty_latch"]["status"] == "covered"
        # The gate's cost contract: one embedding /search per DISTINCT state
        # document, hit or miss (s2 differs from s1, so this searched again).
        assert memory.calls == 2
        json.dumps(second.audit)  # decisions_log rows must be JSON-safe

    async def test_cross_movement_never_hits_another_movements_entry(self):
        # Regression (reviewer probe): the embedding service ranks by movement
        # but does NOT filter on it, so a different movement's situation can
        # surface the SAME best_doc_id. The cache key must be movement-scoped
        # or mv-2's FIRST-EVER checkpoint would be served (and audited) mv-1's
        # decision with the 27B never seeing mv-2's situation.
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88, doc_id="doc-7")])
        scripted = ScriptedDecider(confidence=0.95)
        brain = DeciderBrain(scripted, memory_client=memory, min_interval_sec=0)
        await _prime(brain, memory)  # mv-1 on corridor (72.55, 23.02)
        assert scripted.decide_calls == 1

        # mv-2's first-ever checkpoint on a different corridor, same trigger,
        # same questions, memory still surfaces doc-7 as the best match.
        other = await brain.checkpoint(
            "mv-2", TRIGGER,
            "TRIGGER: incident_detected\nCongestion 0.95 at (72.84, 19.06).",
            questions(),
        )
        assert other.tier == "decider"  # NOT tier0_cache_hit
        assert other.audit["tier0_cache"]["status"] == "miss_no_entry"
        assert scripted.decide_calls == 2  # the 27B saw mv-2's situation
        # mv-2's audit records its OWN decide, not mv-1's cached answer.
        assert other.audit["novelty_latch"]["status"] == "covered"
        # mv-1 still hits afterwards — scoping removed nothing.
        again = await brain.checkpoint(
            "mv-1", TRIGGER, "s-later", questions(), force=True
        )
        assert again.tier == "tier0_cache_hit"
        assert scripted.decide_calls == 2

    async def test_changed_below_routine_situation_misses_reviewer_probe(self):
        # Regression (reviewer probe): after priming on a high-congestion
        # 2-diversion situation, a materially changed situation (lower
        # congestion, different road, no diversions) whose best maxsim is
        # 0.36 — above the 0.35 novelty floor, so 'not novel', but far below
        # the routine bar — must NOT be served the earlier answer.
        memory = FakeMemoryClient(results=[
            memory_ok(best_score=0.88, doc_id="doc-7"),
            memory_ok(best_score=0.36, doc_id="doc-7"),
        ])
        scripted = ScriptedDecider(confidence=0.95)
        brain = DeciderBrain(scripted, memory_client=memory, min_interval_sec=0)
        await _prime(
            brain, memory,
            state="Congestion 0.95 at India Gate, 2 diversions active",
        )
        changed = await brain.checkpoint(
            "mv-1", TRIGGER,
            "Congestion 0.41 at Airport Link, no diversions",
            questions(), force=True,
        )
        assert changed.tier == "decider"  # the 27B decided the new situation
        assert scripted.decide_calls == 2
        assert changed.audit["tier0_cache"]["status"] == "miss_below_routine_floor"

    async def test_hit_consumes_cadence_like_a_real_decide(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        brain = DeciderBrain(
            ScriptedDecider(), memory_client=memory, min_interval_sec=60.0
        )
        await _prime(brain, memory)
        marked_after_prime = brain._last_checkpoint_at.get("mv-1")
        assert marked_after_prime is not None

        hit = await brain.checkpoint(
            "mv-1", TRIGGER, "s2", questions(), force=True
        )
        assert hit.tier == "tier0_cache_hit"
        assert brain._last_checkpoint_at.get("mv-1") >= marked_after_prime

        # Behavioral mirror of test_novelty_latch cadence: an immediate
        # non-forced call is rate_limited and touches neither memory nor
        # the model host.
        calls_before = memory.calls
        third = await brain.checkpoint("mv-1", TRIGGER, "s3", questions())
        assert third.tier == "rate_limited"
        assert memory.calls == calls_before

    async def test_novelty_forces_a_miss_wave1_semantics_preserved(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.10, hits=3)])
        scripted = ScriptedDecider(confidence=0.95)
        brain = DeciderBrain(scripted, memory_client=memory, min_interval_sec=0)
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "novelty_latched"
        assert scripted.decide_calls == 1  # the miss reached the 27B
        assert result.audit["tier0_cache"]["status"] == "miss_novel"
        assert result.audit["novelty_latch"]["status"] == "novel"
        assert not brain._tier0_cache  # nothing stored

    async def test_below_routine_floor_forces_a_miss(self):
        # Covered by the latch floor (0.35) but below the DEFAULT routine bar
        # (0.75): known situation, not similar enough to serve a cached
        # answer for — a distinct outcome from genuinely novel.
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.62)])
        scripted = ScriptedDecider(confidence=0.95)
        brain = DeciderBrain(scripted, memory_client=memory, min_interval_sec=0)
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "decider"
        assert scripted.decide_calls == 1
        assert result.audit["tier0_cache"]["status"] == "miss_below_routine_floor"
        assert not brain._tier0_cache  # write side mirrors the routine bar

    async def test_ttl_expiry_forces_a_miss_and_restore_refreshes(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        scripted = ScriptedDecider(confidence=0.95)
        clock = ScriptedClock(1000.0)
        brain = DeciderBrain(
            scripted, memory_client=memory, min_interval_sec=0,
            clock=clock, tier0_ttl_sec=300.0,
        )
        await _prime(brain, memory)
        assert scripted.decide_calls == 1

        clock.now = 1000.0 + 300.0 + 1.0  # past the TTL — no sleeps
        expired = await brain.checkpoint(
            "mv-1", TRIGGER, "s2", questions(), force=True
        )
        assert expired.tier == "decider"  # fell through to a real decide
        assert scripted.decide_calls == 2
        row = expired.audit["tier0_cache"]
        assert row["status"] == "miss_ttl_expired"
        assert row["age_sec"] >= 301.0
        assert not brain._tier0_cache or all(
            e["stored_at"] == clock.now for e in brain._tier0_cache.values()
        )

        # The re-store refreshed stored_at: an immediate same-clock consult hits.
        hit = await brain.checkpoint(
            "mv-1", TRIGGER, "s3", questions(), force=True
        )
        assert hit.tier == "tier0_cache_hit"
        assert scripted.decide_calls == 2  # unchanged — served from cache

    async def test_kill_switch_preserves_today_behavior_exactly(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.62)])
        scripted = ScriptedDecider(confidence=0.95)
        brain = DeciderBrain(
            scripted, memory_client=memory, min_interval_sec=0, tier0_enabled=False
        )
        first = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert first.tier == "decider"
        assert first.operator_alerts == []
        assert first.audit["tier0_cache"]["status"] == "disabled_kill_switch"
        assert "novelty_latch" in first.audit  # the latch itself still runs
        second = await brain.checkpoint(
            "mv-1", TRIGGER, "state2", questions(), force=True
        )
        assert second.tier == "decider"
        assert scripted.decide_calls == 2  # every checkpoint consults the 27B
        assert not brain._tier0_cache  # the write side is off too

    async def test_ineligible_trigger_never_consults_memory_nor_cache(self):
        # Low confidence keeps the Wave-1 latch silent too, so memory.calls
        # isolates the gate: neither leg runs for operator_chat.
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.62)])
        scripted = ScriptedDecider(confidence=0.42)
        brain = DeciderBrain(scripted, memory_client=memory, min_interval_sec=0)
        result = await brain.checkpoint("mv-1", "operator_chat", "state", questions())
        assert result.tier == "decider"
        assert memory.calls == 0
        assert result.audit["tier0_cache"]["status"] == "miss_ineligible_trigger"

        # Confident operator_chat: the latch searches once, the gate never
        # adds a second /search, and nothing enters the cache.
        memory2 = FakeMemoryClient(results=[memory_ok(best_score=0.62)])
        brain2 = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory2, min_interval_sec=0
        )
        result2 = await brain2.checkpoint(
            "mv-1", "operator_chat", "state", questions()
        )
        assert result2.tier == "decider"
        assert memory2.calls == 1
        assert brain2._tier0_cache == {}

    async def test_memory_outage_fails_open_never_degrades(self):
        memory = FakeMemoryClient(error=RuntimeError("embedding host down"))
        scripted = ScriptedDecider(confidence=0.95)
        brain = DeciderBrain(scripted, memory_client=memory, min_interval_sec=0)
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "decider"  # fail OPEN to the 27B
        assert scripted.decide_calls == 1
        assert brain.degraded is False  # never _set_degradation
        assert result.audit["tier0_cache"]["status"] == "miss_no_similarity_evidence"
        assert not brain._tier0_cache  # unavailable memory never stores

    async def test_all_dense_fallback_is_no_similarity_evidence(self):
        memory = FakeMemoryClient(results=[
            memory_ok(best_score=None, hits=2, maxsim_present=False,
                      strategy="dense_fallback")
        ])
        scripted = ScriptedDecider(confidence=0.95)
        brain = DeciderBrain(scripted, memory_client=memory, min_interval_sec=0)
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "decider"
        assert scripted.decide_calls == 1
        assert result.audit["tier0_cache"]["status"] == "miss_no_similarity_evidence"

    async def test_unconfigured_memory_misses_open(self):
        scripted = ScriptedDecider(confidence=0.95)
        brain = DeciderBrain(scripted, min_interval_sec=0)
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "decider"
        assert scripted.decide_calls == 1
        assert result.audit["tier0_cache"]["status"] == "miss_no_similarity_evidence"

    async def test_tier0_enabled_property_mirrors_latch_property(self):
        brain = brain_with(FakeMemoryClient())
        assert brain.tier0_enabled is True
        assert DeciderBrain(ScriptedDecider(), tier0_enabled=False).tier0_enabled is False


class TestTier0EnabledEnvParsing:
    def test_default_and_falsy_values_keep_the_gate_on(self):
        assert config.tier0_enabled({}) is True
        assert config.tier0_enabled({"DECIDER_TIER0_DISABLE": ""}) is True
        assert config.tier0_enabled({"DECIDER_TIER0_DISABLE": "0"}) is True
        assert config.tier0_enabled({"DECIDER_TIER0_DISABLE": "false"}) is True

    def test_truthy_values_turn_the_gate_off(self):
        assert config.tier0_enabled({"DECIDER_TIER0_DISABLE": "1"}) is False
        assert config.tier0_enabled({"DECIDER_TIER0_DISABLE": "true"}) is False
        assert config.tier0_enabled({"DECIDER_TIER0_DISABLE": "TRUE"}) is False

    def test_env_reads_process_environ_by_default(self, monkeypatch):
        monkeypatch.setenv("DECIDER_TIER0_DISABLE", "1")
        assert config.tier0_enabled() is False
        monkeypatch.delenv("DECIDER_TIER0_DISABLE")
        assert config.tier0_enabled() is True

    def test_operator_chat_is_excluded_from_eligible_triggers(self):
        assert "operator_chat" not in TIER0_ELIGIBLE_TRIGGERS
        assert TRIGGER in TIER0_ELIGIBLE_TRIGGERS

    def test_routine_floor_is_structurally_tighter_than_novelty(self):
        # "Routine" must be a strictly stronger condition than "not novel":
        # the shipped default separates the two bars by a wide margin so a
        # hit requires strong resemblance, not merely a failed novelty test.
        # Both remain UNCALIBRATED placeholders pending empirical
        # calibration against seeded memory.
        assert config.TIER0_ROUTINE_SIMILARITY_FLOOR > config.NOVELTY_SIMILARITY_FLOOR
        assert config.TIER0_ROUTINE_SIMILARITY_FLOOR >= 0.75


# ─── (B) Write side ──────────────────────────────────────────────────────────


class TestWriteSide:
    async def test_low_confidence_checkpoint_never_stores(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.62)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.42), memory_client=memory, min_interval_sec=0
        )
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "decider"
        assert any(
            a["type"] == "low_confidence_decision" for a in result.operator_alerts
        )
        assert brain._tier0_cache == {}

    async def test_latched_novel_checkpoint_never_stores(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.10)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory, min_interval_sec=0
        )
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "novelty_latched"
        assert brain._tier0_cache == {}

    async def test_unavailable_memory_never_stores(self):
        memory = FakeMemoryClient(error=RuntimeError("embedding host down"))
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory,
            min_interval_sec=0,
        )
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "decider"
        assert result.audit["novelty_latch"]["status"] == "disabled_unreachable"
        assert brain._tier0_cache == {}

    async def test_kill_switch_disables_the_write_side(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.62)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory,
            min_interval_sec=0, tier0_enabled=False,
        )
        await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert brain._tier0_cache == {}

    async def test_max_entries_fifo_eviction(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        scripted = ScriptedDecider(confidence=0.95)
        brain = DeciderBrain(
            scripted, memory_client=memory, min_interval_sec=0, tier0_max_entries=2
        )
        # Three distinct cache keys: different question sets (distinct qsig).
        variants = [
            {"next_tool": {"type": "choice", "instructions": "pick",
                           "options": ["a", "final_answer"]}},
            {"next_tool": {"type": "choice", "instructions": "pick",
                           "options": ["b", "final_answer"]}},
            {"next_tool": {"type": "choice", "instructions": "pick",
                           "options": ["c", "final_answer"]}},
        ]
        for i, qs in enumerate(variants):
            result = await brain.checkpoint(
                "mv-1", TRIGGER, f"state-{i}", qs, force=True
            )
            assert result.tier == "decider"
        assert len(brain._tier0_cache) == 2  # bounded — third evicted the first

        # The FIRST key was evicted (FIFO): re-asking it misses and decides.
        re_first = await brain.checkpoint(
            "mv-1", TRIGGER, "state-0", variants[0], force=True
        )
        assert re_first.tier == "decider"
        assert scripted.decide_calls == 4
        # The LAST key still hits without a decide.
        re_last = await brain.checkpoint(
            "mv-1", TRIGGER, "state-2", variants[2], force=True
        )
        assert re_last.tier == "tier0_cache_hit"
        assert scripted.decide_calls == 4

    async def test_different_trigger_or_doc_id_is_a_different_key(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88, doc_id="doc-7")])
        scripted = ScriptedDecider(confidence=0.95)
        brain = DeciderBrain(scripted, memory_client=memory, min_interval_sec=0)
        await _prime(brain, memory)

        # Same situation kind, different trigger: ineligible→eligible mix.
        phase = await brain.checkpoint(
            "mv-1", "phase_transition", "s2", questions(), force=True
        )
        assert phase.tier == "decider"  # different key — no hit

        # best_doc_id churn (memory re-index) degrades to a safe miss.
        memory.results = [memory_ok(best_score=0.62, doc_id="doc-99")]
        churned = await brain.checkpoint(
            "mv-1", TRIGGER, "s3", questions(), force=True
        )
        assert churned.tier == "decider"
        assert scripted.decide_calls == 3


class OwnDocMemory:
    """Memory that knows which decision docs the feeder recorded.

    Until ``movement_id`` has recorded one, the best match is a FOREIGN
    decision doc (another movement's plan — live, 2026-10-24, movement
    2c223e77 plan 1); afterwards it is one of the movement's own docs, with
    the ref_id the feeder wrote. ``pick`` chooses which own doc (``"first"``
    or ``"last"`` recorded) — two own docs can tie.
    """

    FOREIGN_REF = "decision:another-movement:pre_movement:0123456789abcdef"

    def __init__(self, pick: str = "last") -> None:
        self.pick = pick
        self.recorded: list[dict] = []
        self.calls = 0

    async def search(self, state_doc, *, movement_id=None, top_k=5):
        self.calls += 1
        own = [r for r in self.recorded if r["movement_id"] == movement_id]
        if not own:
            return replace(memory_ok(best_score=0.82, doc_id="foreign-9", kind="decision"),
                           best_ref_id=self.FOREIGN_REF)
        doc = own[0] if self.pick == "first" else own[-1]
        return replace(memory_ok(best_score=0.83, doc_id=doc["doc_id"], kind="decision"),
                       best_ref_id=doc["ref_id"])

    async def record_decision(self, state_doc, *, movement_id, ref_id, title, metadata=None,
                              timeout_sec=5.0):
        if not any(r["ref_id"] == ref_id for r in self.recorded):  # upsert on ref_id
            self.recorded.append({"ref_id": ref_id, "movement_id": movement_id,
                                  "doc_id": f"own-{len(self.recorded) + 1}"})
        return True


def choice_questions(*options: str) -> dict:
    return {"next_tool": {"type": "choice", "instructions": "pick",
                          "options": [*options, "final_answer"]}}


class TestOwnDocFiling:
    """A pre_movement answer is also filed for the movement's own decision docs.

    Live (2026-10-24, movement 2c223e77): plan 1's search named a FOREIGN
    decision doc; once the feeder upserted plan 1's own doc, plans 2 and 3
    named an own doc, so the entry plan 1 stored under the foreign doc was
    unreachable. For pre_movement the answer is also served when the best
    memory is any decision doc this movement recorded for that trigger.
    """

    @staticmethod
    def brain(memory, scripted=None, **kwargs) -> DeciderBrain:
        return DeciderBrain(
            scripted or ScriptedDecider(confidence=0.95), memory_client=memory,
            min_interval_sec=0, clock=ScriptedClock(1000.0), memory_record_enabled=True,
            **kwargs,
        )

    async def test_the_first_recurrence_hits_through_an_own_doc(self):
        memory, scripted = OwnDocMemory(), ScriptedDecider(confidence=0.95)
        brain = self.brain(memory, scripted)
        stored = {"outcome": "stored"}
        stored_before = _metric("vvip_decider_tier0_cache_events_total", stored)
        first = await brain.checkpoint("mv-1", "pre_movement", "plan 1", questions())
        assert first.tier == "decider"
        assert first.audit["tier0_cache"]["best_doc_id"] == "foreign-9"
        assert first.audit["novelty_latch"]["best_ref_id"] == OwnDocMemory.FOREIGN_REF
        # One answer, two filings; the "stored" event counts the decide once.
        assert len(brain._tier0_cache) == 2
        assert _metric("vvip_decider_tier0_cache_events_total", stored) - stored_before == 1

        # The live figures moved (another state doc): the search names own-1.
        second = await brain.checkpoint("mv-1", "pre_movement", "plan 2", questions())
        assert second.tier == "tier0_cache_hit"
        assert second.audit["tier0_cache"]["best_doc_id"] == "own-1"
        assert second.audit["novelty_latch"]["best_ref_id"] == memory.recorded[0]["ref_id"]
        assert second.answers["next_tool"].value == first.answers["next_tool"].value
        assert scripted.decide_calls == 1
        assert len(memory.recorded) == 1  # a hit records nothing

    @pytest.mark.parametrize("pick", ["first", "last"])
    async def test_whichever_own_doc_matches_the_question_set_decides(self, pick):
        memory, scripted = OwnDocMemory(pick=pick), ScriptedDecider(confidence=0.95)
        brain = self.brain(memory, scripted)
        three, five = choice_questions("a", "b", "c"), choice_questions("a", "b", "c", "d", "e")
        await brain.checkpoint("mv-1", "pre_movement", "plan 1 (3)", three)
        second = await brain.checkpoint("mv-1", "pre_movement", "plan 2 (5)", five)
        assert second.tier == "decider"  # a different question set never hits
        # Two own docs on record now; set 3 is served whichever one matches.
        third = await brain.checkpoint("mv-1", "pre_movement", "plan 3 (3)", three)
        assert third.tier == "tier0_cache_hit"
        assert scripted.decide_calls == 2
        # A question set never decided misses, own doc or not.
        fresh = await brain.checkpoint("mv-1", "pre_movement", "plan 4", choice_questions("z"))
        assert fresh.tier == "decider"
        assert scripted.decide_calls == 3

    async def test_without_the_filing_the_first_answer_is_unreachable(self):
        # The filing every other trigger keeps: the second checkpoint names
        # the own doc, finds nothing under it, and decides again.
        memory, scripted = OwnDocMemory(), ScriptedDecider(confidence=0.95)
        brain = self.brain(memory, scripted, tier0_own_doc_triggers=frozenset())
        await brain.checkpoint("mv-1", "pre_movement", "plan 1", questions())
        assert len(brain._tier0_cache) == 1
        second = await brain.checkpoint("mv-1", "pre_movement", "plan 2", questions())
        assert second.tier == "decider"
        assert second.audit["tier0_cache"]["status"] == "miss_no_entry"
        assert scripted.decide_calls == 2

    async def test_escort_triggers_keep_their_filing(self):
        memory, scripted = OwnDocMemory(), ScriptedDecider(confidence=0.95)
        brain = self.brain(memory, scripted)
        await brain.checkpoint("mv-1", TRIGGER, "incident 1", questions())
        assert [key[3] for key in brain._tier0_cache] == ["foreign-9"]
        second = await brain.checkpoint("mv-1", TRIGGER, "incident 2", questions())
        assert second.tier == "decider"
        assert scripted.decide_calls == 2

    async def test_the_filing_stays_movement_scoped(self):
        memory, scripted = OwnDocMemory(), ScriptedDecider(confidence=0.95)
        brain = self.brain(memory, scripted)
        await brain.checkpoint("mv-1", "pre_movement", "plan 1", questions())
        # mv-2 matching mv-1's own doc: the ref names mv-1, the key mv-2.
        memory.recorded[0]["movement_id"] = "mv-2"
        other = await brain.checkpoint("mv-2", "pre_movement", "plan 1", questions())
        assert other.audit["novelty_latch"]["best_ref_id"].startswith("decision:mv-1:")
        assert other.tier == "decider"
        assert other.audit["tier0_cache"]["status"] == "miss_no_entry"
        assert scripted.decide_calls == 2

    def test_only_this_movements_trigger_refs_count_as_own(self):
        sig = "0123456789abcdef"
        assert _is_own_decision_ref(f"decision:mv-1:pre_movement:{sig}", "mv-1", "pre_movement")
        for ref in (
            None, 42, "",
            f"decision:mv-2:pre_movement:{sig}",            # another movement
            f"decision:mv-1x:pre_movement:{sig}",           # a longer movement id
            f"decision:mv-1:incident_detected:{sig}",       # another trigger
            f"decision:mv-1:pre_movement:{sig[:-1]}",       # not a signature
            f"decision:mv-1:pre_movement:{sig.upper()}",
            # a movement id that itself holds ':' cannot pass for mv-1's
            f"decision:mv-1:pre_movement:x:pre_movement:{sig}",
            f"incident:mv-1:pre_movement:{sig}",
        ):
            assert not _is_own_decision_ref(ref, "mv-1", "pre_movement"), ref

    async def test_another_movements_decision_doc_never_reads_the_filing(self):
        memory, scripted = OwnDocMemory(), ScriptedDecider(confidence=0.95)
        brain = self.brain(memory, scripted)
        await brain.checkpoint("mv-1", "pre_movement", "plan 1", questions())

        async def a_third_movements_plan(state_doc, *, movement_id=None, top_k=5):
            return replace(
                memory_ok(best_score=0.83, doc_id="foreign-7", kind="decision"),
                best_ref_id="decision:mv-3:pre_movement:fedcba9876543210",
            )

        memory.search = a_third_movements_plan
        second = await brain.checkpoint("mv-1", "pre_movement", "plan 2", questions())
        assert second.tier == "decider"
        assert second.audit["tier0_cache"]["status"] == "miss_no_entry"
        assert scripted.decide_calls == 2

    async def test_a_non_decision_best_doc_never_reads_the_filing(self):
        memory, scripted = OwnDocMemory(), ScriptedDecider(confidence=0.95)
        brain = self.brain(memory, scripted)
        await brain.checkpoint("mv-1", "pre_movement", "plan 1", questions())
        own_ref = memory.recorded[0]["ref_id"]

        async def incident_with_an_own_looking_ref(state_doc, *, movement_id=None, top_k=5):
            return replace(memory_ok(best_score=0.83, doc_id="inc-1", kind="incident"),
                           best_ref_id=own_ref)

        memory.search = incident_with_an_own_looking_ref
        second = await brain.checkpoint("mv-1", "pre_movement", "plan 2", questions())
        assert second.tier == "decider"
        assert scripted.decide_calls == 2

    async def test_nothing_is_filed_without_a_store(self):
        # Low confidence: never cached, in neither filing.
        memory = OwnDocMemory()
        brain = self.brain(memory, ScriptedDecider(confidence=0.42))
        result = await brain.checkpoint("mv-1", "pre_movement", "plan 1", questions())
        assert result.tier == "decider" and len(memory.recorded) == 1
        assert brain._tier0_cache == {}
        # Latched (novel): recorded as withheld, never cached.
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.10, doc_id="far")])
        brain = self.brain(memory)
        result = await brain.checkpoint("mv-1", "pre_movement", "plan 1", questions())
        assert result.tier == "novelty_latched"
        assert brain._tier0_cache == {}

    async def test_an_expired_answer_is_never_served_through_the_filing(self):
        memory, scripted = OwnDocMemory(), ScriptedDecider(confidence=0.95)
        clock = ScriptedClock(1000.0)
        brain = DeciderBrain(scripted, memory_client=memory, min_interval_sec=0, clock=clock,
                             memory_record_enabled=True, tier0_ttl_sec=300.0)
        await brain.checkpoint("mv-1", "pre_movement", "plan 1", questions())
        clock.now += 300.0
        late = await brain.checkpoint("mv-1", "pre_movement", "plan 2", questions())
        assert late.tier == "decider"
        assert late.audit["tier0_cache"]["status"] == "miss_ttl_expired"
        assert scripted.decide_calls == 2

    async def test_the_filing_respects_the_fifo_bound(self):
        memory = OwnDocMemory()
        brain = self.brain(memory, tier0_max_entries=1)
        await brain.checkpoint("mv-1", "pre_movement", "plan 1", questions())
        # Bounded: the movement-level filing (inserted last) is the one kept.
        assert [key[3] for key in brain._tier0_cache] == ["decision:mv-1:pre_movement:"]


# ─── (C) Metrics (deltas only — counters are process-global across tests) ────


class TestMetrics:
    async def test_hit_increments_event_and_latency_saved_deltas(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        scripted = ScriptedDecider(confidence=0.95)
        brain = DeciderBrain(
            scripted, memory_client=memory, min_interval_sec=0, clock=ScriptedClock(1000.0)
        )
        await _prime(brain, memory)
        origin_latency = next(iter(brain._tier0_cache.values()))["origin_latency_ms"]
        assert isinstance(origin_latency, int) and origin_latency >= 0

        hits_before = _metric("vvip_decider_tier0_cache_events_total", {"outcome": "hit"})
        saved_before = _metric("vvip_decider_tier0_latency_saved_ms_total")
        second = await brain.checkpoint(
            "mv-1", TRIGGER, "s2", questions(), force=True
        )
        assert second.tier == "tier0_cache_hit"
        assert (
            _metric("vvip_decider_tier0_cache_events_total", {"outcome": "hit"})
            - hits_before
        ) == 1
        assert (
            _metric("vvip_decider_tier0_latency_saved_ms_total") - saved_before
        ) == float(origin_latency)

    async def test_store_increments_stored_delta(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory, min_interval_sec=0
        )
        stored_before = _metric(
            "vvip_decider_tier0_cache_events_total", {"outcome": "stored"}
        )
        await brain.checkpoint("mv-m", TRIGGER, "state", questions())
        assert (
            _metric("vvip_decider_tier0_cache_events_total", {"outcome": "stored"})
            - stored_before
        ) == 1

    async def test_below_routine_floor_gets_its_own_outcome_label(self):
        # 'miss_novel' must mean NOVEL — a known-but-not-routine situation is
        # a distinct outcome, so a miss_novel graph cannot conflate the two.
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.62)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory, min_interval_sec=0
        )
        novel_before = _metric(
            "vvip_decider_tier0_cache_events_total", {"outcome": "miss_novel"}
        )
        below_before = _metric(
            "vvip_decider_tier0_cache_events_total",
            {"outcome": "miss_below_routine_floor"},
        )
        result = await brain.checkpoint("mv-l", TRIGGER, "state", questions())
        assert result.audit["tier0_cache"]["status"] == "miss_below_routine_floor"
        assert (
            _metric(
                "vvip_decider_tier0_cache_events_total",
                {"outcome": "miss_below_routine_floor"},
            )
            - below_before
        ) == 1
        assert (
            _metric("vvip_decider_tier0_cache_events_total", {"outcome": "miss_novel"})
            - novel_before
        ) == 0

    async def test_hits_stay_out_of_the_decide_duration_histogram(self):
        # DECIDER_CHECKPOINT_DURATION must keep meaning "real /decide
        # latency": a hit adds NO observation. Non-vacuous: the count is
        # seeded with one real observation first (nothing in src observes
        # into this histogram today — track_decider_checkpoint is the
        # sanctioned production wrapper), so the assertion protects any
        # future caller of that wrapper, not an always-zero counter.
        from convoy_brain.metrics import track_decider_checkpoint

        sample = "vvip_decider_checkpoint_duration_seconds_count"
        before = _metric(sample)
        async with track_decider_checkpoint(question_types=["choice"]):
            pass  # seed exactly one observation
        seeded = _metric(sample)
        assert seeded - before == 1

        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory,
            min_interval_sec=0, clock=ScriptedClock(1000.0),
        )
        await _prime(brain, memory)
        hit = await brain.checkpoint("mv-1", TRIGGER, "s2", questions(), force=True)
        assert hit.tier == "tier0_cache_hit"
        assert _metric(sample) == seeded  # the hit added NO observation


# ─── (E) Calibrated confidence gate (2026-10-19 authorization) ────────────────


class TestCalibratedConfidenceGate:
    """Pins the calibration authorized 2026-10-19.

    Live measurements (POST :8083/decide, exact pre-movement questions):
    route_strategy 0.5033, disruption_tolerance 0.4696, urgency (noul)
    carries NO confidence (model-host contract). The previous state —
    CONFIDENCE_FLOOR=0.6 plus None→0.0 coercion for noul — mislabeled every
    live pre-movement checkpoint low_confidence and permanently blocked the
    tier-0 write side. Now: floor 0.45 (config, env VVIP_CONFIDENCE_FLOOR),
    noul excluded from aggregation (unknown is neither pass nor fail).
    """

    PRE_MOVEMENT_QS = {
        "route_strategy": {
            "type": "choice", "instructions": "pick",
            "options": ["primary", "alternate", "delay_departure"],
        },
        "urgency": {"type": "noul", "instructions": "how urgent"},
        "disruption_tolerance": {
            "type": "score", "instructions": "tolerance", "scale": [0.0, 1.0],
        },
    }

    async def test_noul_absence_does_not_block_caching(self):
        # Live-shaped answers: choice 0.5033 / score 0.4696 (both above the
        # calibrated 0.45 floor), noul confidence None. The checkpoint is
        # CONFIDENT, stores, and the identical repeat HITS — the exact live
        # behavior the calibration enables.
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        scripted = ScriptedDecider(confidence=0.5033)  # noul → None by default
        brain = DeciderBrain(
            scripted, memory_client=memory, min_interval_sec=0,
            confidence_floor=0.45,
        )
        first = await brain.checkpoint(
            "mv-c1", "pre_movement", "state", self.PRE_MOVEMENT_QS
        )
        assert first.tier == "decider"
        assert first.audit["needs_gemma_draft"] is False
        assert first.operator_alerts == []
        # The noul answer's absent confidence is labeled honestly (null),
        # not fabricated as 0.0.
        assert first.audit["answers"]["urgency"]["confidence"] is None
        assert brain._tier0_cache  # stored

        repeat = await brain.checkpoint(
            "mv-c1", "pre_movement", "state-2-same-kind",
            self.PRE_MOVEMENT_QS, force=True,
        )
        assert repeat.tier == "tier0_cache_hit"
        assert scripted.decide_calls == 1  # the repeat never called the 27B
        assert repeat.audit["answers"]["urgency"]["confidence"] is None

    async def test_genuinely_low_choice_confidence_never_caches(self):
        # Noul presence must not ENABLE caching either: a genuinely
        # low-confidence choice answer (0.40 < 0.45) never stores even with
        # the noul excluded.
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.40), memory_client=memory,
            min_interval_sec=0, confidence_floor=0.45,
        )
        result = await brain.checkpoint(
            "mv-c2", "pre_movement", "state", self.PRE_MOVEMENT_QS
        )
        assert result.tier == "decider"
        assert any(
            a["type"] == "low_confidence_decision" for a in result.operator_alerts
        )
        assert brain._tier0_cache == {}

    async def test_below_floor_similarity_normalized_0_60_never_hits(self):
        # Normalized-scale discrimination: a best doc at 0.60 (covered by
        # the 0.35 novelty floor, below the 0.75 routine bar) must NOT serve
        # or store — "routine" stays strictly stronger than "not novel".
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.60)])
        scripted = ScriptedDecider(confidence=0.95)
        brain = DeciderBrain(scripted, memory_client=memory, min_interval_sec=0)
        first = await brain.checkpoint(
            "mv-c3", "pre_movement", "state", self.PRE_MOVEMENT_QS
        )
        assert first.audit["tier0_cache"]["status"] == "miss_below_routine_floor"
        assert brain._tier0_cache == {}  # write side mirrors the routine bar
        second = await brain.checkpoint(
            "mv-c3", "pre_movement", "state", self.PRE_MOVEMENT_QS, force=True
        )
        assert second.audit["tier0_cache"]["status"] == "miss_below_routine_floor"
        assert scripted.decide_calls == 2

    async def test_confidence_floor_default_is_the_calibrated_constant(self):
        from convoy_brain.config import CONFIDENCE_FLOOR
        assert CONFIDENCE_FLOOR == 0.45
        brain = DeciderBrain(ScriptedDecider())
        assert brain._confidence_floor == 0.45


# ─── (D) Integration — live escort incident path ─────────────────────────────


class TestLiveEscortTier0Persistence:
    async def test_second_incident_within_ttl_hits_cache_with_one_decide(
        self, fake_store
    ):
        from convoy_brain.memory.convoy_context import ConvoyContext
        from convoy_brain.workflows.live_escort import run_live_escort_workflow
        from test_agents import StubBridge
        from test_workflows import ROUTES

        bridge = StubBridge(fail=True)
        routes = dict(ROUTES)
        # One live call per tick (legacy route-less monitor): congested at
        # ticks 1 and 3, clear at tick 2 — two separate crossings.
        levels = iter([0.95, 0.2, 0.95])

        def live(args):
            return {"segments": [
                {"segment_id": 1001, "congestion_idx": next(levels), "speed_kmh": 5}
            ]}

        routes["get_live_traffic"] = live
        executor = FakeExecutor(routes=routes)

        ctx = ConvoyContext(movement_id="es-t0", vvip_class="Z+", status="active")
        ctx.convoy_position = (72.55, 23.02)
        await fake_store.put(ctx)

        scripted = ScriptedDecider(confidence=0.95)
        memory = FakeMemoryClient(
            results=[memory_ok(best_score=0.88, doc_id="doc-7")]
        )
        brain = DeciderBrain(
            scripted, memory_client=memory, min_interval_sec=0,
            tier0_ttl_sec=300.0,
        )
        result = await run_live_escort_workflow(
            movement_id="es-t0",
            bridge=bridge,
            executor=executor,
            context_store=fake_store,
            decider_brain=brain,
            max_iterations=3,  # one incident checkpoint per crossing (rising edge)
            poll_interval_sec=0,
            incident_cooldown_sec=0,
            clear_confirm_sec=0,  # the single clear reading at tick 2 is a falling edge
        )
        assert result["escort_complete"] is True

        decisions = fake_store.contexts["es-t0"].decisions_log
        checkpoints = [
            d for d in decisions
            if d.get("phase") == "decider_checkpoint"
            and d.get("trigger") == "incident_detected"
        ]
        assert len(checkpoints) == 2
        assert checkpoints[0]["tier"] == "decider"
        assert checkpoints[0]["tier0_cache"]["status"] == "miss_no_entry"
        assert checkpoints[1]["tier"] == "tier0_cache_hit"
        assert checkpoints[1]["tier0_cache"]["status"] == "hit"
        assert checkpoints[1]["tier0_cache"]["best_doc_id"] == "doc-7"
        # ONE real decide: the first crossing; the second crossing's
        # checkpoint was served entirely by the gate.
        assert scripted.decide_calls == 1
        # Each crossing is logged once as an observation row (no separate
        # congestion_threshold_crossed checkpoint any more).
        crossings = [d for d in decisions if d.get("action") == "congestion_crossing"]
        assert [c["trigger"] for c in crossings] == ["congestion_threshold_crossed"] * 2
        for row in checkpoints:
            json.dumps(row)  # decisions_log rows must stay JSON-safe
