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
        checkpoints never store; max-entries FIFO eviction.
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

from prometheus_client import REGISTRY

from convoy_brain import config
from convoy_brain.config import TIER0_ELIGIBLE_TRIGGERS, TIER0_ROUTINE_SIMILARITY_FLOOR
from convoy_brain.decider import DeciderBrain

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
        routes["get_live_traffic"] = {
            "segments": [{"segment_id": 1001, "congestion_idx": 0.95, "speed_kmh": 5}]
        }
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
            max_iterations=2,  # incident checkpoint fires once per iteration
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
        # TWO real decides in total — the iteration-1 congestion-crossing
        # checkpoint (a different, also-eligible trigger) plus the iteration-1
        # incident checkpoint — and the iteration-2 incident checkpoint was
        # served entirely by the gate (no third decide).
        assert scripted.decide_calls == 2
        crossing = [
            d for d in decisions
            if d.get("phase") == "decider_checkpoint"
            and d.get("trigger") == "congestion_threshold_crossed"
        ]
        assert len(crossing) == 1  # only iteration 1 (no re-crossing)
        for row in checkpoints:
            json.dumps(row)  # decisions_log rows must stay JSON-safe
