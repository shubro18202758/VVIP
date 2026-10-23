"""Decider novelty-latch tests — confident-but-novel abstention, honestly.

Coverage:
    (A) MemoryClient over httpx.MockTransport: every failure mode collapses
        to status='unavailable' (transport, 404/503, non-JSON, malformed
        results); valid parse reads the best MaxSim row and sends the
        contract request body; empty results; all-dense_fallback rows carry
        no comparable score; non-platform URLs rejected at construction.
    (B) DeciderBrain latch: novel+confident withholds with an audit row and
        operator alert; covered proceeds; low-confidence never
        double-escalates; unreachable memory disables the latch honestly
        WITHOUT benching the strategic tier; unconfigured memory is silent;
        degraded/rate-limited short-circuits never call memory; cadence
        still applies to latched checkpoints; the memo suppresses chained
        searches (never memoizing failures); timeouts are bounded.
    (C) Integration: the orchestrator falls through a latched checkpoint to
        the tactical tier (pinned: 'novelty_latched' is never trusted) and
        surfaces the alert; the live-escort incident path persists BOTH the
        audit row and the operator alert to decisions_log.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from convoy_brain.config import (
    NOVELTY_MEMORY_TOP_K,
    NOVELTY_SIMILARITY_FLOOR,
)
from convoy_brain.decider import DeciderBrain
from convoy_brain.http_guard import UnsafeUrlError
from convoy_brain.llm.decider_client import DeciderAnswer, DeciderUnavailable
from convoy_brain.memory.memory_client import MemoryClient, MemorySearchResult

from conftest import FakeExecutor
from test_orchestrator import ScriptBridge, ScriptDecider, run_turn


# ─── Fakes ────────────────────────────────────────────────────────────────────


class ScriptedDecider:
    """Deterministic DeciderClient stand-in (confidence configurable).

    noul answers default to confidence=None — mirroring the model-host wire
    contract (decider_runtime.py: "autojev noul answers carry no
    confidence"), which the decider excludes from confidence gating.
    """

    def __init__(
        self,
        confidence: float = 0.95,
        error: Exception | None = None,
        noul_confidence: float | None = None,
    ):
        self.confidence = confidence
        self.noul_confidence = noul_confidence
        self.error = error
        self.decide_calls = 0

    @property
    def p95_decide_latency_ms(self) -> float:
        return 100.0

    async def decide(self, state, questions, request_id=None):
        self.decide_calls += 1
        if self.error:
            raise self.error
        return {
            name: DeciderAnswer(
                name=name,
                type=spec["type"],
                value="final_answer" if spec["type"] == "choice" else 0.9,
                probability=0.9,
                confidence=(
                    self.noul_confidence if spec["type"] == "noul"
                    else self.confidence
                ),
            )
            for name, spec in questions.items()
        }

    async def health(self, timeout_sec=3.0):
        return {"loaded": True, "p95_decide_latency_ms": 100.0}

    async def warmup(self):
        return True


def memory_ok(
    best_score: float | None = 0.62,
    hits: int = 1,
    maxsim_present: bool = True,
    strategy: str | None = "maxsim",
    doc_id: str | None = "doc-7",
    kind: str | None = "incident",
) -> MemorySearchResult:
    return MemorySearchResult(
        status="ok",
        best_score=best_score,
        best_strategy=strategy,
        best_doc_id=doc_id,
        best_kind=kind,
        best_title="Ring-road closure AAR",
        hits=hits,
        maxsim_present=maxsim_present,
        latency_ms=12.5,
    )


def memory_unavailable(error: str = "503") -> MemorySearchResult:
    return MemorySearchResult(
        status="unavailable",
        best_score=None,
        best_strategy=None,
        best_doc_id=None,
        best_kind=None,
        best_title=None,
        hits=0,
        maxsim_present=False,
        latency_ms=3.0,
        error=error,
    )


class FakeMemoryClient:
    """Plain-class memory client stand-in (counts calls, scripts results)."""

    def __init__(self, results=None, error: Exception | None = None, delay: float = 0.0):
        self.results = list(results or [])  # consumed in order; last repeats
        self.error = error
        self.delay = delay
        self.calls = 0
        self.seen: list[dict] = []
        self.recorded: list[dict] = []
        self.record_error: Exception | None = None
        self.record_returns: bool = True

    async def search(self, state_doc, *, movement_id=None, top_k=5):
        self.calls += 1
        self.seen.append(
            {"query": state_doc, "movement_id": movement_id, "top_k": top_k}
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        if self.results:
            if len(self.results) > 1:
                return self.results.pop(0)
            return self.results[0]
        return memory_ok()

    async def record_decision(self, state_doc, *, movement_id, ref_id, title,
                              metadata=None, timeout_sec=5.0):
        if self.record_error:
            raise self.record_error
        self.recorded.append({
            "state_doc": state_doc,
            "movement_id": movement_id,
            "ref_id": ref_id,
            "title": title,
            "metadata": metadata or {},
        })
        return self.record_returns


class ScriptedClock:
    """Mutable monotonic clock — no sleeps, deterministic TTL expiry."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def questions() -> dict:
    return {
        "next_tool": {
            "type": "choice",
            "instructions": "pick",
            "options": ["find_convoy_routes", "final_answer"],
        },
    }


def brain_with(memory=None, *, confidence=0.95, **kwargs) -> DeciderBrain:
    kwargs.setdefault("min_interval_sec", 0)  # cadence never interferes
    return DeciderBrain(ScriptedDecider(confidence), memory_client=memory, **kwargs)


# ─── (A) MemoryClient over MockTransport ─────────────────────────────────────


def client_with(handler) -> MemoryClient:
    return MemoryClient(
        base_url="http://127.0.0.1:8085",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class TestMemoryClientFailClosed:
    async def test_transport_error_is_unavailable_never_raises(self):
        def handler(request):
            raise httpx.ConnectError("no memory service")

        result = await client_with(handler).search("TRIGGER: incident")
        assert result.status == "unavailable"
        assert result.hits == 0
        assert result.error and "no memory service" in result.error
        assert result.to_audit()["status"] == "unavailable"

    async def test_http_error_statuses_are_unavailable(self):
        for status in (404, 503):
            result = await client_with(lambda r, s=status: httpx.Response(s)).search(
                "TRIGGER: incident"
            )
            assert result.status == "unavailable", status

    async def test_non_json_200_is_unavailable(self):
        result = await client_with(
            lambda r: httpx.Response(200, text="not json")
        ).search("TRIGGER: incident")
        assert result.status == "unavailable"

    async def test_missing_or_malformed_results_is_unavailable(self):
        for payload in ({}, {"results": "nope"}, {"results": None}):
            result = await client_with(
                lambda r, p=payload: httpx.Response(200, json=p)
            ).search("TRIGGER: incident")
            assert result.status == "unavailable", payload


class TestMemoryClientParsing:
    async def test_valid_parse_reads_best_maxsim_and_request_shape(self):
        seen: dict = {}

        def handler(request):
            seen.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={
                "query": "TRIGGER: incident", "query_token_count": 4,
                "recalled": 12, "latency_ms": 8.0,
                "results": [
                    {"doc_id": "d2", "kind": "aar", "ref_id": "r2",
                     "title": "Monsoon AAR", "snippet": "…", "score": 0.71,
                     "maxsim_score": 0.58, "dense_score": 0.6,
                     "strategy": "maxsim", "token_count": 300,
                     "occurred_at": "2026-09-01T00:00:00Z", "metadata": {}},
                    {"doc_id": "d1", "kind": "incident", "ref_id": "r1",
                     "title": "Old incident", "snippet": "…", "score": 0.9,
                     "maxsim_score": 0.31, "dense_score": 0.9,
                     "strategy": "maxsim", "token_count": 120,
                     "occurred_at": "2026-08-01T00:00:00Z", "metadata": {}},
                ],
            })

        result = await client_with(handler).search(
            "TRIGGER: incident", movement_id="mv-1"
        )
        assert result.status == "ok"
        assert result.hits == 2
        # BEST maxsim across rows — not results[0] ordering. best_score is
        # the NORMALIZED value (raw / query_token_count — the 2026-10-19
        # calibration; 0.58 raw over 4 query tokens → 0.145); the raw
        # service value is preserved on best_score_raw.
        assert result.best_score == 0.58 / 4
        assert result.best_score_raw == 0.58
        assert result.best_doc_id == "d2"
        assert result.best_kind == "aar"
        assert result.best_ref_id == "r2"  # the best row's, like doc_id/kind
        assert result.maxsim_present is True
        # Request carried the contract body.
        assert seen["query"] == "TRIGGER: incident"
        assert seen["kinds"] == ["incident", "scenario_run", "aar", "decision"]
        assert seen["top_k"] == NOVELTY_MEMORY_TOP_K
        assert seen["movement_id"] == "mv-1"

    async def test_empty_results_is_ok_with_zero_hits(self):
        result = await client_with(
            lambda r: httpx.Response(200, json={"results": []})
        ).search("TRIGGER: incident")
        assert result.status == "ok"
        assert result.hits == 0
        assert result.best_score is None
        assert result.best_ref_id is None
        assert result.maxsim_present is False

    async def test_a_row_without_a_ref_id_reports_none(self):
        result = await client_with(lambda r: httpx.Response(200, json={
            "query_token_count": 2,
            "results": [{"doc_id": "d1", "kind": "decision", "title": "t",
                         "maxsim_score": 1.2, "strategy": "maxsim"}],
        })).search("TRIGGER: pre_movement")
        assert result.best_doc_id == "d1"
        assert result.best_ref_id is None

    async def test_missing_query_token_count_passes_raw_through(self):
        # Fail-open legacy shape: a response without query_token_count (an
        # older embedding service) leaves the RAW maxsim as best_score —
        # documented passthrough, never a raise.
        def handler(request):
            return httpx.Response(200, json={
                "results": [
                    {"doc_id": "d1", "kind": "decision", "ref_id": "r1",
                     "title": "t", "snippet": "s", "score": 3.2,
                     "maxsim_score": 3.2, "dense_score": 0.5,
                     "strategy": "maxsim", "token_count": 40,
                     "occurred_at": None, "metadata": {}},
                ],
            })

        result = await client_with(handler).search("TRIGGER: incident")
        assert result.status == "ok"
        assert result.best_score == 3.2
        assert result.best_score_raw == 3.2

    async def test_all_dense_fallback_rows_carry_no_comparable_score(self):
        # maxsim_score None on every row — the scales must never be mixed.
        result = await client_with(lambda r: httpx.Response(200, json={
            "results": [
                {"doc_id": "d1", "kind": "pattern", "score": 0.9,
                 "maxsim_score": None, "dense_score": 0.9,
                 "strategy": "dense_fallback", "token_count": 10,
                 "occurred_at": "2026-08-01T00:00:00Z"},
                {"doc_id": "d2", "kind": "incident", "score": 0.4,
                 "maxsim_score": None, "dense_score": 0.4,
                 "strategy": "dense_fallback", "token_count": 10,
                 "occurred_at": "2026-08-02T00:00:00Z"},
            ]
        })).search("TRIGGER: incident")
        assert result.status == "ok"
        assert result.hits == 2
        assert result.best_score is None
        assert result.maxsim_present is False

    def test_non_platform_url_rejected_at_construction(self):
        with pytest.raises(UnsafeUrlError):
            MemoryClient(base_url="http://example.com:8085")


# ─── (B) DeciderBrain latch ──────────────────────────────────────────────────


class TestLatchDecisions:
    async def test_novel_confident_withholds_with_alert_and_audit(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.10, hits=3)])
        brain = brain_with(memory, confidence=0.95)
        result = await brain.checkpoint(
            "mv-1", "operator_chat", "TRIGGER: incident near India Gate", questions()
        )
        assert result.tier == "novelty_latched"
        assert result.degraded_reason and "withheld" in result.degraded_reason
        assert "0.1000" in result.degraded_reason  # best maxsim, rounded
        alert = next(
            a for a in result.operator_alerts
            if a["type"] == "novel_situation_confident_answer"
        )
        assert alert["action"] == "escalated_to_tactical_and_operator"
        assert alert["best_score"] == 0.1
        assert alert["similarity_floor"] == NOVELTY_SIMILARITY_FLOOR
        assert alert["max_confidence"] == 0.95
        assert alert["hits"] == 3
        assert result.audit["tier"] == "novelty_latched"
        assert result.audit["novelty_latch"]["status"] == "novel"
        assert result.audit["novelty_latch"]["escalate"] is True
        assert result.audit["needs_gemma_draft"] is True
        assert result.audit["answers"]  # withheld evidence stays in the audit
        assert memory.calls == 1
        assert brain.degraded is False  # never touches _set_degradation

    async def test_zero_hits_is_novel(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=None, hits=0)])
        brain = brain_with(memory)
        result = await brain.checkpoint("mv-1", "operator_chat", "state", questions())
        assert result.tier == "novelty_latched"
        assert "no similar memory" in result.degraded_reason
        assert result.audit["novelty_latch"]["hits"] == 0

    async def test_covered_confident_proceeds_uninterrupted(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.62)])
        brain = brain_with(memory)
        result = await brain.checkpoint("mv-1", "operator_chat", "state", questions())
        assert result.tier == "decider"
        assert result.degraded_reason is None
        assert result.operator_alerts == []
        latch = result.audit["novelty_latch"]
        assert latch["status"] == "covered"
        assert latch["best_doc_id"] == "doc-7"
        assert latch["best_kind"] == "incident"
        assert result.audit["needs_gemma_draft"] is False

    async def test_low_confidence_never_double_escalates(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.10)])
        brain = brain_with(memory, confidence=0.42)
        result = await brain.checkpoint("mv-1", "operator_chat", "state", questions())
        assert result.tier == "decider"
        assert [a["type"] for a in result.operator_alerts] == [
            "low_confidence_decision"
        ]
        assert result.audit["novelty_latch"]["status"] == "skipped_low_confidence"
        assert memory.calls == 0  # the check itself is skipped

    async def test_all_dense_fallback_is_insufficient_evidence_not_novel(self):
        memory = FakeMemoryClient(
            results=[memory_ok(best_score=None, hits=2, maxsim_present=False,
                               strategy="dense_fallback")]
        )
        brain = brain_with(memory)
        result = await brain.checkpoint("mv-1", "operator_chat", "state", questions())
        assert result.tier == "decider"
        assert result.operator_alerts == []
        assert result.audit["novelty_latch"]["status"] == "insufficient_evidence"

    async def test_raising_memory_disables_latch_honestly(self):
        memory = FakeMemoryClient(error=RuntimeError("embedding host down"))
        brain = brain_with(memory)
        result = await brain.checkpoint("mv-1", "operator_chat", "state", questions())
        assert result.tier == "decider"  # strategic tier still answers
        assert brain.degraded is False    # memory outage never benches it
        assert any(
            a["type"] == "novelty_check_unavailable" for a in result.operator_alerts
        )
        latch = result.audit["novelty_latch"]
        assert latch["status"] == "disabled_unreachable"
        assert "embedding host down" in latch["error"]

    async def test_unavailable_status_result_disables_latch_honestly(self):
        memory = FakeMemoryClient(results=[memory_unavailable("503 from /search")])
        brain = brain_with(memory)
        result = await brain.checkpoint("mv-1", "operator_chat", "state", questions())
        assert result.tier == "decider"
        assert result.audit["novelty_latch"]["status"] == "disabled_unreachable"

    async def test_unconfigured_memory_is_silent_disabled(self):
        # Guards the pinned default-ctor behaviour (test_decider_brain.py).
        brain = DeciderBrain(ScriptedDecider(confidence=0.93))
        assert brain.novelty_latch_enabled is False
        result = await brain.checkpoint("mv-1", "operator_chat", "state", questions())
        assert result.tier == "decider"
        assert result.operator_alerts == []
        assert result.audit["novelty_latch"]["status"] == "disabled_not_configured"


class TestLatchShortCircuits:
    async def test_degraded_brain_never_calls_memory(self):
        memory = FakeMemoryClient()
        brain = DeciderBrain(
            ScriptedDecider(error=DeciderUnavailable("connection refused")),
            memory_client=memory,
            min_interval_sec=0,
        )
        result = await brain.checkpoint("mv-1", "operator_chat", "state", questions())
        assert result.tier == "gemma"
        assert memory.calls == 0

    async def test_rate_limited_checkpoint_never_calls_memory(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.8)])
        brain = DeciderBrain(
            ScriptedDecider(), memory_client=memory, min_interval_sec=60.0
        )
        await brain.checkpoint("mv-1", "operator_chat", "state", questions())
        assert memory.calls == 1
        second = await brain.checkpoint(
            "mv-1", "operator_chat", "state", questions()
        )
        assert second.tier == "rate_limited"
        assert memory.calls == 1

    async def test_latched_checkpoint_still_consumes_cadence(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.1)])
        brain = DeciderBrain(
            ScriptedDecider(), memory_client=memory, min_interval_sec=60.0
        )
        first = await brain.checkpoint("mv-1", "operator_chat", "state", questions())
        assert first.tier == "novelty_latched"
        second = await brain.checkpoint(
            "mv-1", "operator_chat", "state", questions()
        )
        assert second.tier == "rate_limited"


class TestLatchMemo:
    async def test_same_movement_and_state_searches_once(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.8)])
        clock = ScriptedClock(1000.0)
        brain = brain_with(memory, clock=clock)
        await brain.checkpoint("m1", "operator_chat", "same state", questions())
        await brain.checkpoint(
            "m1", "operator_chat", "same state", questions(), force=True
        )
        assert memory.calls == 1

    async def test_different_state_doc_searches_again(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.8)])
        brain = brain_with(memory)
        await brain.checkpoint("m1", "operator_chat", "state A", questions())
        await brain.checkpoint(
            "m1", "operator_chat", "state B", questions(), force=True
        )
        assert memory.calls == 2

    async def test_ttl_expiry_via_injected_clock_re_searches(self):
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.8)])
        clock = ScriptedClock(1000.0)
        brain = brain_with(memory, clock=clock)
        await brain.checkpoint("m1", "operator_chat", "same state", questions())
        await brain.checkpoint(
            "m1", "operator_chat", "same state", questions(), force=True
        )
        assert memory.calls == 1
        clock.now = 2000.0  # past NOVELTY_MEMO_TTL_SEC
        await brain.checkpoint(
            "m1", "operator_chat", "same state", questions(), force=True
        )
        assert memory.calls == 2

    async def test_unavailable_results_are_never_memoized(self):
        memory = FakeMemoryClient(results=[memory_unavailable("503")])
        brain = brain_with(memory)
        await brain.checkpoint("m1", "operator_chat", "same state", questions())
        await brain.checkpoint(
            "m1", "operator_chat", "same state", questions(), force=True
        )
        assert memory.calls == 2
        assert brain.degraded is False

    async def test_exceptions_are_never_memoized(self):
        memory = FakeMemoryClient(error=RuntimeError("boom"))
        brain = brain_with(memory)
        await brain.checkpoint("m1", "operator_chat", "same state", questions())
        await brain.checkpoint(
            "m1", "operator_chat", "same state", questions(), force=True
        )
        assert memory.calls == 2


class TestLatchTimeout:
    async def test_slow_search_is_cancelled_and_degrades_honestly(self):
        memory = FakeMemoryClient(results=[memory_ok()], delay=1.0)
        brain = DeciderBrain(
            ScriptedDecider(),
            memory_client=memory,
            min_interval_sec=0,
            novelty_timeout_sec=0.05,
        )
        started = time.monotonic()
        result = await brain.checkpoint("m1", "operator_chat", "state", questions())
        elapsed = time.monotonic() - started
        assert elapsed < 0.5  # wait_for cancelled the 1.0 s fake
        assert result.tier == "decider"
        assert result.audit["novelty_latch"]["status"] == "disabled_unreachable"
        assert any(
            a["type"] == "novelty_check_unavailable" for a in result.operator_alerts
        )


# ─── (C) Integration ─────────────────────────────────────────────────────────


class TestOrchestratorEscalation:
    async def test_latched_checkpoint_falls_through_to_gemma(self, fake_store):
        # The decider scripts "final_answer" (confident). If the orchestrator
        # TRUSTED a latched pick, the turn would end with no tool call; the
        # correct fall-through runs Gemma selection -> tool -> args -> final.
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
        memory = FakeMemoryClient(results=[memory_ok(best_score=0.1)])
        from convoy_brain.orchestrator import Orchestrator

        orch = Orchestrator(
            bridge, executor, fake_store,
            decider_brain=DeciderBrain(
                ScriptDecider("final_answer", confidence=0.9),
                memory_client=memory,
                min_interval_sec=0,
            ),
        )
        result = await run_turn(orch, "What is the current traffic on segment 5?")
        assert "decider:novelty_latched" in result["tiers_used"]
        assert "decider:decider" not in result["tiers_used"]  # never trusted
        assert [c["name"] for c in result["tool_calls"]] == ["get_live_traffic"]
        assert any(
            a["type"] == "novel_situation_confident_answer"
            for a in result.get("operator_alerts", [])
        )


class TestLiveEscortAlertPersistence:
    async def test_latched_incident_persists_audit_and_operator_alert(
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

        ctx = ConvoyContext(movement_id="es9", vvip_class="Z+", status="active")
        ctx.convoy_position = (72.55, 23.02)
        await fake_store.put(ctx)

        memory = FakeMemoryClient(results=[memory_ok(best_score=0.05)])
        brain = DeciderBrain(ScriptDecider(tool="emergency_reroute"),
                             memory_client=memory)
        result = await run_live_escort_workflow(
            movement_id="es9",
            bridge=bridge,
            executor=executor,
            context_store=fake_store,
            decider_brain=brain,
            max_iterations=1,
        )
        assert result["escort_complete"] is True

        decisions = fake_store.contexts["es9"].decisions_log
        audits = [
            d for d in decisions
            if d.get("phase") == "decider_checkpoint"
            and d.get("novelty_latch", {}).get("status") == "novel"
        ]
        assert audits, "latched audit row never reached decisions_log"
        alerts = [
            d for d in decisions
            if d.get("type") == "novel_situation_confident_answer"
        ]
        assert alerts, "novelty operator alert never reached decisions_log"
        assert alerts[0]["phase"] == "live_escort_alert"
