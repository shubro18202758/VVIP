"""Decision-memory feeder tests — the C3 write side the gate/latch read.

Root cause this suite pins (live-audit finding tier0-cache-never-hits-live):
nothing in the running stack ever POSTed /embed/documents for the decider's
own checkpoints, so traffic.memory_docs held zero docs of the searched
kinds (incident/scenario_run/aar/decision) and every live checkpoint read
``miss_novel`` forever — the tier-0 gate could never reach its own cache.

Coverage:
    (A) MemoryClient.record_decision over httpx.MockTransport: the exact
        /embed/documents contract (kind=decision, one document carrying
        ref_id/title/body/movement_id/metadata) returns True; 500s,
        malformed payloads and transport errors return False — the method
        NEVER raises (a feeder outage must not break a checkpoint).
    (B) DeciderBrain feeder wiring: a SERVED checkpoint (tier "decider",
        confident or low-confidence alike — low-confidence answers still
        ship with a Gemma draft) records the situation; the ref_id mirrors
        the tier-0 key contract (movement/trigger/questions signature, NOT
        the raw state doc) so re-decided situations upsert one stable doc;
        withheld (novelty_latched), rate-limited, degraded and tier-0-hit
        results never record; a memory client without a write side
        (duck-typed fakes) keeps working; a raising/recording-failing
        client never breaks the checkpoint; metric deltas per outcome.
    (C) Ordering guarantee: the feeder runs AFTER the checkpoint's own
        /search — the first occurrence of a situation still reads as novel
        (Wave-1 latch semantics preserved; the corpus only ever learns a
        situation the decider has already decided).

Reuses the test_novelty_latch.py fakes by import (FakeMemoryClient now
counts record_decision calls; ScriptedDecider counts decides).
"""

from __future__ import annotations

import json

import httpx
from prometheus_client import REGISTRY
from test_novelty_latch import (
    FakeMemoryClient,
    ScriptedDecider,
    brain_with,
    memory_ok,
    questions,
)

from convoy_brain.decider import DeciderBrain, _questions_signature
from convoy_brain.memory.memory_client import MemoryClient, MemorySearchResult

TRIGGER = "pre_movement"  # tier-0 eligible, the trigger the plan flow uses


def _metric(name: str, labels: dict[str, str] | None = None) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return value if value is not None else 0.0


# ─── (A) MemoryClient.record_decision over MockTransport ──────────────────────


def recording_client_with(handler) -> MemoryClient:
    return MemoryClient(
        base_url="http://127.0.0.1:8085",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class TestRecordDecisionClient:
    async def test_posts_the_embed_contract_and_returns_true(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"embedded": 1, "doc_ids": ["d1"]})

        client = recording_client_with(handler)
        ok = await client.record_decision(
            "TRIGGER: pre_movement\nsituation text",
            movement_id="mv-1",
            ref_id="decision:mv-1:pre_movement:abcd0123",
            title="decider pre_movement checkpoint",
            metadata={"trigger": "pre_movement", "tier": "decider"},
        )
        assert ok is True
        assert len(seen) == 1
        assert seen[0].url.path == "/embed/documents"
        payload = json.loads(seen[0].content)
        assert payload["kind"] == "decision"
        assert len(payload["documents"]) == 1
        doc = payload["documents"][0]
        assert doc["ref_id"] == "decision:mv-1:pre_movement:abcd0123"
        assert doc["title"] == "decider pre_movement checkpoint"
        assert doc["body"] == "TRIGGER: pre_movement\nsituation text"
        assert doc["movement_id"] == "mv-1"
        assert doc["metadata"]["trigger"] == "pre_movement"
        await client.aclose()

    async def test_http_500_returns_false_never_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client = recording_client_with(handler)
        ok = await client.record_decision(
            "state", movement_id="mv", ref_id="r", title="t"
        )
        assert ok is False
        await client.aclose()

    async def test_transport_error_returns_false_never_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("embedding host down")

        client = recording_client_with(handler)
        ok = await client.record_decision(
            "state", movement_id="mv", ref_id="r", title="t"
        )
        assert ok is False
        await client.aclose()

    async def test_malformed_payload_returns_false(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unexpected": "shape"})

        client = recording_client_with(handler)
        ok = await client.record_decision(
            "state", movement_id="mv", ref_id="r", title="t"
        )
        assert ok is False
        await client.aclose()


# ─── (B) DeciderBrain feeder wiring ───────────────────────────────────────────


class TestFeederWiring:
    async def test_served_confident_checkpoint_records_the_situation(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95),
            memory_client=memory, min_interval_sec=0,
        )
        result = await brain.checkpoint(
            "mv-1", TRIGGER, "TRIGGER: pre_movement\nplans", questions()
        )
        assert result.tier == "decider"
        assert len(memory.recorded) == 1
        row = memory.recorded[0]
        assert row["state_doc"] == "TRIGGER: pre_movement\nplans"
        assert row["movement_id"] == "mv-1"
        # ref_id mirrors the tier-0 key contract: movement + trigger +
        # canonical questions signature (NOT the raw per-tick state doc).
        expected_ref = (
            f"decision:mv-1:{TRIGGER}:{_questions_signature(questions())[:16]}"
        )
        assert row["ref_id"] == expected_ref
        assert row["title"].startswith("decider pre_movement")
        assert row["metadata"]["tier"] == "decider"
        assert row["metadata"]["needs_gemma_draft"] is False
        json.dumps(row["metadata"])  # metadata must stay JSON-safe

    async def test_served_low_confidence_checkpoint_still_records(self):
        # A low-confidence answer is still SERVED (with a Gemma draft) — the
        # situation is on record for the read side. The tier-0 ANSWER cache
        # stays gated (decider.py:531-539) — recording is not laundering.
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.42),
            memory_client=memory, min_interval_sec=0,
        )
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "decider"
        assert any(
            a["type"] == "low_confidence_decision" for a in result.operator_alerts
        )
        assert len(memory.recorded) == 1
        assert memory.recorded[0]["metadata"]["needs_gemma_draft"] is True
        assert not brain._tier0_cache  # the answer gate is untouched

    async def test_repeat_checkpoint_upserts_the_same_ref_id(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.42), memory_client=memory, min_interval_sec=0
        )
        await brain.checkpoint("mv-1", TRIGGER, "s-tick-1", questions(), force=True)
        await brain.checkpoint("mv-1", TRIGGER, "s-tick-2", questions(), force=True)
        assert len(memory.recorded) == 2
        # Same (movement, trigger, questions) -> same upsert key: the second
        # decide refreshed ONE doc (embedding service upserts on kind+ref_id)
        # instead of duplicating corpus rows.
        assert memory.recorded[0]["ref_id"] == memory.recorded[1]["ref_id"]

    async def test_different_trigger_or_questions_is_a_different_ref_id(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory, min_interval_sec=0
        )
        await brain.checkpoint("mv-1", "phase_transition", "s", questions(), force=True)
        other_questions = {
            "next_tool": {
                "type": "choice", "instructions": "pick", "options": ["x", "y"],
            }
        }
        await brain.checkpoint(
            "mv-1", TRIGGER, "s", other_questions, force=True
        )
        assert memory.recorded[0]["ref_id"] != memory.recorded[1]["ref_id"]

    async def test_withheld_checkpoint_records_its_situation(self):
        # The self-lock fix: a confidently-answered NOVEL situation is withheld
        # but its SITUATION is recorded (withheld=true) — before, it was never
        # recorded and therefore latched forever.
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.10)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory, min_interval_sec=0
        )
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "novelty_latched"
        assert len(memory.recorded) == 1
        row = memory.recorded[0]
        assert row["state_doc"] == "state"
        assert row["metadata"]["withheld"] is True
        assert row["metadata"]["tier"] == "novelty_latched"
        assert "withheld" in row["title"]
        assert not brain._tier0_cache  # withheld answers never enter the cache

    async def test_repeat_of_a_recorded_situation_stops_latching(self):
        class LearningMemory(FakeMemoryClient):
            """Corpus double: a recorded state doc becomes a covered hit."""

            async def search(self, state_doc, *, movement_id=None, top_k=5):
                self.calls += 1
                if any(r["state_doc"] == state_doc for r in self.recorded):
                    return memory_ok(best_score=0.95, kind="decision")
                return memory_ok(best_score=None, hits=0)

        memory = LearningMemory()
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory, min_interval_sec=0
        )
        first = await brain.checkpoint("mv-1", "operator_chat", "same question", questions())
        second = await brain.checkpoint(
            "mv-1", "operator_chat", "same question", questions(), force=True
        )
        assert first.tier == "novelty_latched"
        assert second.tier == "decider"  # on record now — the 27B is trusted
        assert memory.calls == 2  # the latch memo was dropped after the record
        assert [r["metadata"]["withheld"] for r in memory.recorded] == [True, False]

    async def test_failed_withheld_record_keeps_the_memo(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.10)])
        memory.record_returns = False
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory, min_interval_sec=0
        )
        await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        await brain.checkpoint("mv-1", TRIGGER, "state", questions(), force=True)
        assert memory.calls == 1  # nothing landed, so no reason to re-search

    async def test_rate_limited_and_degraded_never_record(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory,
            min_interval_sec=60.0,
        )
        first = await brain.checkpoint("mv-1", TRIGGER, "s1", questions())
        assert first.tier == "decider"
        assert len(memory.recorded) == 1
        limited = await brain.checkpoint("mv-1", TRIGGER, "s2", questions())
        assert limited.tier == "rate_limited"
        assert len(memory.recorded) == 1  # no decide happened, no record

        brain._set_degradation("test degradation")
        degraded = await brain.checkpoint(
            "mv-1", TRIGGER, "s3", questions(), force=True
        )
        assert degraded.tier == "gemma"
        assert len(memory.recorded) == 1

    async def test_tier0_cache_hit_never_records(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory, min_interval_sec=0
        )
        await brain.checkpoint("mv-1", TRIGGER, "s1", questions())
        assert len(memory.recorded) == 1
        hit = await brain.checkpoint(
            "mv-1", TRIGGER, "s2-same-kind", questions(), force=True
        )
        assert hit.tier == "tier0_cache_hit"
        assert len(memory.recorded) == 1  # no new decide — nothing to record

    async def test_client_without_write_side_still_checkpoints(self):
        class SearchOnlyMemory:  # duck-typed pre-feeder fake / older client
            def __init__(self):
                self.calls = 0

            async def search(self, state_doc, *, movement_id=None, top_k=5):
                self.calls += 1
                return memory_ok(best_score=0.62)

        memory = SearchOnlyMemory()
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory, min_interval_sec=0
        )
        before = _metric(
            "vvip_decider_memory_record_events_total",
            {"outcome": "skipped_unconfigured"},
        )
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "decider"  # the checkpoint is unaffected
        assert (
            _metric(
                "vvip_decider_memory_record_events_total",
                {"outcome": "skipped_unconfigured"},
            )
            - before
        ) == 1

    async def test_raising_record_never_breaks_the_checkpoint(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        memory.record_error = RuntimeError("embedder down")
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory, min_interval_sec=0
        )
        before = _metric(
            "vvip_decider_memory_record_events_total", {"outcome": "error"}
        )
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "decider"
        assert result.answers  # the served answers survived the feeder error
        assert (
            _metric(
                "vvip_decider_memory_record_events_total", {"outcome": "error"}
            )
            - before
        ) == 1

    async def test_recorded_outcome_metric_delta(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.95), memory_client=memory, min_interval_sec=0
        )
        before = _metric(
            "vvip_decider_memory_record_events_total", {"outcome": "recorded"}
        )
        await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert (
            _metric(
                "vvip_decider_memory_record_events_total", {"outcome": "recorded"}
            )
            - before
        ) == 1


# ─── (C) Ordering: the feeder never feeds the search it follows ───────────────


class TestFeederOrdering:
    async def test_first_occurrence_reads_empty_corpus_then_feeds(self):
        # Zero-hit search result = what a live empty corpus produces. The
        # tier-0 consult runs BEFORE the decide, so the search that read
        # zero hits cannot have seen the doc this checkpoint then records —
        # a situation's first occurrence still reads as novel.
        memory = FakeMemoryClient(results=[
            MemorySearchResult(
                status="ok", best_score=None, best_strategy=None,
                best_doc_id=None, best_kind=None, best_title=None,
                hits=0, maxsim_present=False, latency_ms=5.0,
            )
        ])
        brain = DeciderBrain(
            ScriptedDecider(confidence=0.42), memory_client=memory, min_interval_sec=0
        )
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "decider"  # served (low confidence, with draft)
        assert result.audit["tier0_cache"]["status"] == "miss_novel"
        assert result.audit["tier0_cache"]["hits"] == 0
        assert memory.calls == 1  # exactly one search, run before the record
        assert len(memory.recorded) == 1  # the record lands only afterwards

    async def test_brain_without_memory_client_counts_skipped(self):
        brain = brain_with(None)  # no memory client wired at all
        before = _metric(
            "vvip_decider_memory_record_events_total", {"outcome": "skipped_no_client"}
        )
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "decider"
        assert (
            _metric(
                "vvip_decider_memory_record_events_total",
                {"outcome": "skipped_no_client"},
            )
            - before
        ) == 1


# ─── (D) VVIP_MEMORY_RECORD — the feeder's off switch (WP-14 T14) ─────────────


class TestFeederOffSwitch:
    """Sandbox brains must never add rows to the corpus the embedding service
    writes (a previous wave leaked sandbox decisions into the LIVE
    traffic.memory_docs). VVIP_MEMORY_RECORD=0 turns the write side off:
    checkpoints still SEARCH (latch and gate unchanged), nothing is recorded,
    and every skip counts as outcome 'skipped_disabled'."""

    def test_env_switch_parsing(self):
        from convoy_brain.config import decision_memory_record_enabled

        assert decision_memory_record_enabled({}) is True
        for off in ("0", "false", "No", " off "):
            assert decision_memory_record_enabled({"VVIP_MEMORY_RECORD": off}) is False
        for on in ("1", "true", "yes", ""):
            assert decision_memory_record_enabled({"VVIP_MEMORY_RECORD": on}) is True

    async def test_disabled_feeder_searches_but_never_records(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.88)])
        brain = brain_with(memory, memory_record_enabled=False)
        assert brain.memory_record_enabled is False
        before = _metric(
            "vvip_decider_memory_record_events_total", {"outcome": "skipped_disabled"})
        result = await brain.checkpoint("mv-1", TRIGGER, "state", questions())
        assert result.tier == "decider"
        assert memory.calls == 1  # the novelty search still ran
        assert memory.recorded == []
        assert _metric(
            "vvip_decider_memory_record_events_total", {"outcome": "skipped_disabled"}
        ) == before + 1

    async def test_disabled_feeder_also_skips_withheld_records(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.05)])  # novel → latched
        brain = brain_with(memory, memory_record_enabled=False)
        result = await brain.checkpoint("mv-1", TRIGGER, "novel state", questions())
        assert result.tier == "novelty_latched"
        assert memory.recorded == []

    def test_env_off_reaches_the_brain_and_health(self, monkeypatch):
        from conftest import FakeContextStore
        from starlette.testclient import TestClient

        from convoy_brain import __main__ as brain_main

        for name in ("MODEL_HOST_URL", "LLAMA_BASE_URL", "TRAFFIC_ORACLE_URL",
                     "EMBEDDING_SERVICE_URL"):
            monkeypatch.setenv(name, "http://127.0.0.1:1")
        monkeypatch.setenv("VVIP_MEMORY_RECORD", "0")
        monkeypatch.setattr(brain_main, "ConvoyContextStore",
                            lambda valkey_url: FakeContextStore())
        app = brain_main.create_app()
        with TestClient(app) as client:
            assert app.state.decider_brain.memory_record_enabled is False
            body = client.get("/health").json()
        assert body["strategic_tier"]["memory_record"] == "disabled"
