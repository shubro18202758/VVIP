"""Tests for the degradation ledger — decisions_log taxonomy + endpoint.

Coverage:
    * classify_row: every cause family the decisions_log schema actually
      carries (Wave-1 latch firings, tier-0 gate misses, tactical
      fallbacks, cadence refusals, operator alerts, failed orchestrator
      turns) and the healthy rows that must NEVER classify
      (decider/tier0_cache_hit checkpoints, covered/skipped latch rows,
      lifecycle bookkeeping, successful turns).
    * build_taxonomy: aggregation across movements (count, first/last
      seen, sample_ref pinned to the earliest row), deterministic
      count-desc/cause-asc ordering, timestamp-less rows tolerated,
      malformed rows skipped without crashing, JSON-safe output, and the
      EMPTY case — no contexts, no rows, only healthy rows — returning
      an honest empty taxonomy with the scan counts that prove it.
    * HTTP surface (GET /api/v1/ops/degradation-taxonomy): 503 when the
      Valkey client is absent, the empty-log honest payload against a
      seeded-but-empty Valkey, a populated scan (contexts/rows counts,
      corrupt-payload skipping, truncation flag), and consistency with
      the pure module over the same seed.
    * the scanner reads contexts with batched MGET (one round trip per
      100 keys, never one GET per movement) and is shared with the city
      feed (``scan_contexts`` / ``read_contexts``).

Everything here is pure python (stdlib + fastapi/httpx only) — the file
runs under the gate venv with PYTHONPATH=src, same pattern as
test_units_dispatch.py. No database, no network: Valkey is a fake.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from traffic_oracle.ops.degradation_taxonomy import build_taxonomy, classify_row

# ─── Row factories (the exact shapes the writers emit) ───────────────────────


def checkpoint(
    *,
    tier: str = "decider",
    novelty_status: str = "covered",
    tier0_status: str | None = "miss_ineligible_trigger",
    reason: str | None = None,
    ts: float = 1_000.0,
    movement_id: str = "m-1",
    escalate: bool = False,
    hits: int = 3,
    best_score: float | None = 0.6,
) -> dict:
    """One decider_checkpoint row, exactly as decider.py writes it.

    Defaults reproduce a HEALTHY strategic checkpoint: a real /decide
    answer (tier "decider") whose situation was memory-covered.  The
    novelty/tier0 sub-rows keep the uniform key set of the real writers.
    """
    row: dict[str, Any] = {
        "phase": "decider_checkpoint",
        "trigger": "incident_detected",
        "movement_id": movement_id,
        "tier": tier,
        "novelty_latch": {
            "status": novelty_status,
            "escalate": escalate,
            "best_score": best_score,
            "floor": 0.35,
            "hits": hits,
            "strategy": None,
            "best_doc_id": "doc-1",
            "best_kind": "after_action",
            "memo": False,
            "error": None,
        },
        "latency_ms": 812,
        "timestamp": ts,
    }
    if tier0_status is not None:
        row["tier0_cache"] = {
            "status": tier0_status,
            "best_score": best_score,
            "floor": 0.5,
            "hits": hits,
            "best_doc_id": "doc-1",
            "best_kind": "after_action",
            "age_sec": None,
            "latency_saved_ms": None,
            "origin_state_doc_sha256": None,
            "origin_timestamp": None,
            "memo": False,
            "error": None,
        }
    if reason is not None:
        row["reason"] = reason
    return row


def latched(ts: float = 1_000.0, movement_id: str = "m-1") -> dict:
    """The Wave-1 novelty-latch firing (decider.py checkpoint audit)."""
    return checkpoint(
        tier="novelty_latched",
        novelty_status="novel",
        escalate=True,
        tier0_status="miss_novel",
        hits=0,
        best_score=None,
        ts=ts,
        movement_id=movement_id,
    )


def alert(
    alert_type: str,
    *,
    phase: str = "live_escort_alert",
    ts: float = 1_000.0,
) -> dict:
    """One operator-alert row (dict(alert, phase=...) in decisions_log)."""
    return {
        "type": alert_type,
        "movement_id": "m-1",
        "trigger": "incident_detected",
        "action": "escalated_to_tactical_and_operator",
        "timestamp": ts,
        "phase": phase,
    }


def orchestrator_turn(action: str = "final", ts: float = 1_000.0) -> dict:
    """One orchestrator_turn row (orchestrator.py append_decision)."""
    return {
        "session_id": "s-1",
        "phase": "orchestrator_turn",
        "action": action,
        "confidence": "low" if action != "final" else "high",
        "tiers_used": ["decider:decider"],
        "tool_calls_total": 2,
        "operator_alerts": 0,
        "turn_duration_sec": 1.2,
        "timestamp": ts,
    }


# ─── classify_row ─────────────────────────────────────────────────────────────


class TestClassifyRow:
    def test_healthy_decider_checkpoint_never_classifies(self):
        assert classify_row(checkpoint()) == []

    def test_tier0_cache_hit_never_classifies(self):
        row = checkpoint(tier="tier0_cache_hit", tier0_status="hit")
        assert classify_row(row) == []

    def test_latch_firing_is_the_novel_cause(self):
        assert classify_row(latched()) == ["novelty_latch:novel", "tier0:miss_novel"]

    def test_tactical_fallback_carries_its_tier(self):
        row = checkpoint(
            tier="gemma",
            novelty_status="skipped_low_confidence",
            tier0_status=None,
            reason="decider degraded: /decider/health p95 7200 ms exceeds 5000 ms",
        )
        assert classify_row(row) == ["checkpoint_fallback:gemma"]

    def test_unwritten_and_future_fallback_tiers_surface_not_vanish(self):
        """Negative fallback rule: "unavailable" appears only in the
        CheckpointResult comment (no writer emits it), but a tier that is
        neither healthy nor handled elsewhere classifies anyway — a future
        fallback tier must surface instead of silently vanishing."""
        for tier in ("unavailable", "degraded_probe"):
            row = checkpoint(tier=tier, tier0_status=None, reason="x")
            assert classify_row(row) == [f"checkpoint_fallback:{tier}"]

    def test_missing_tier_never_classifies(self):
        row = checkpoint(tier0_status=None)
        del row["tier"]
        assert classify_row(row) == []

    def test_rate_limited_refusal_is_its_own_cause(self):
        row = checkpoint(
            tier="rate_limited",
            novelty_status="skipped_low_confidence",
            tier0_status=None,
            reason="rate-limited: last checkpoint <30s ago for movement m-1",
        )
        assert classify_row(row) == ["checkpoint_rate_limited"]

    def test_memory_outage_disables_the_latch_honestly(self):
        row = checkpoint(
            novelty_status="disabled_unreachable",
            tier0_status="miss_no_similarity_evidence",
        )
        assert classify_row(row) == [
            "novelty_latch:disabled_unreachable",
            "tier0:miss_no_similarity_evidence",
        ]

    def test_insufficient_evidence_is_a_limitation_signal(self):
        row = checkpoint(
            novelty_status="insufficient_evidence",
            tier0_status="miss_no_similarity_evidence",
        )
        assert classify_row(row) == [
            "novelty_latch:insufficient_evidence",
            "tier0:miss_no_similarity_evidence",
        ]

    @pytest.mark.parametrize(
        "status",
        [
            "miss_no_entry",
            "miss_ttl_expired",
            "miss_novel",
            "miss_below_routine_floor",
            "miss_no_similarity_evidence",
        ],
    )
    def test_every_armed_gate_miss_status_counts(self, status):
        assert f"tier0:{status}" in classify_row(checkpoint(tier0_status=status))

    @pytest.mark.parametrize(
        "status", ["miss_ineligible_trigger", "disabled_kill_switch"]
    )
    def test_config_state_tier0_statuses_never_classify(self, status):
        """Ineligible-trigger refusals and the kill switch are not failures —
        counting them would drown the ledger in non-degradation noise."""
        assert classify_row(checkpoint(tier0_status=status)) == []

    def test_covered_and_not_configured_latch_statuses_never_classify(self):
        assert classify_row(checkpoint(tier0_status=None, novelty_status="covered")) == []
        assert (
            classify_row(
                checkpoint(tier0_status=None, novelty_status="disabled_not_configured")
            )
            == []
        )

    @pytest.mark.parametrize(
        "phase",
        [
            "operator_alert",
            "live_escort_alert",
            "pre_movement_alert",
            "post_clearance_alert",
            # A future writer's phase — the shape rule (``type`` present on
            # a non-checkpoint row) must classify it without a module edit.
            "some_new_flow_alert",
        ],
    )
    @pytest.mark.parametrize(
        "alert_type",
        [
            "decider_unavailable",
            "novel_situation_confident_answer",
            "novelty_check_unavailable",
            "low_confidence_decision",
        ],
    )
    def test_alert_rows_classify_by_type(self, alert_type, phase):
        assert classify_row(alert(alert_type, phase=phase)) == [f"alert:{alert_type}"]

    def test_failed_turn_actions_classify(self):
        for action in ("max_rounds_exceeded", "timeout", "error"):
            assert classify_row(orchestrator_turn(action)) == [f"orchestrator:{action}"]

    def test_successful_turn_never_classifies(self):
        assert classify_row(orchestrator_turn("route_recommendation")) == []

    def test_lifecycle_rows_never_classify(self):
        assert classify_row({"phase": "pre_movement", "recommendation": "approved", "timestamp": 1.0}) == []
        assert classify_row({"phase": "live_escort", "action": "escort_complete", "timestamp": 2.0}) == []
        assert classify_row({"phase": "post_clearance", "action": "clearance_complete", "timestamp": 3.0}) == []

    def test_malformed_rows_are_skipped_not_crashed(self):
        assert classify_row("not a dict") == []  # type: ignore[arg-type]
        assert classify_row({}) == []
        # Sub-rows corrupted to non-mappings must not break classification.
        row = latched()
        row["novelty_latch"] = "corrupt"
        row["tier0_cache"] = 7
        assert classify_row(row) == ["novelty_latch:novel"]

    def test_phase_less_row_with_type_still_classifies(self):
        # Legacy/defensive shape: alert row that lost its phase tag.
        row = alert("decider_unavailable")
        del row["phase"]
        assert classify_row(row) == ["alert:decider_unavailable"]

    def test_alert_only_log_is_not_falsely_empty(self):
        """Regression (live-proven): the real decisions_log today carries
        ONLY alert rows — the pre-movement flow stores the checkpoint
        audit in its state dict instead of appending it, so
        pre_movement_alert rows are that flow's ONLY degradation evidence.
        A log of pure low_confidence_decision / decider_unavailable alert
        rows across many movements must produce entries, never the false
        EmptyState a phase-allowlist classifier showed."""
        rows = [alert("low_confidence_decision", phase="pre_movement_alert", ts=1.0 + i)
                for i in range(20)]
        rows.append(alert("decider_unavailable", phase="pre_movement_alert", ts=2.0))
        rows.append(alert("decider_unavailable", phase="pre_movement_alert", ts=3.0))
        result = build_taxonomy([(f"m-{i:02d}", rows[i:i + 1]) for i in range(len(rows))])
        by_cause = {e["cause"]: e for e in result["entries"]}
        assert set(by_cause) == {
            "alert:low_confidence_decision",
            "alert:decider_unavailable",
        }
        assert by_cause["alert:low_confidence_decision"]["count"] == 20
        assert by_cause["alert:decider_unavailable"]["count"] == 2
        assert result["contexts_scanned"] == 22
        assert result["rows_scanned"] == 22


# ─── build_taxonomy ───────────────────────────────────────────────────────────


class TestBuildTaxonomy:
    def test_empty_contexts_is_an_honest_empty_taxonomy(self):
        result = build_taxonomy([])
        assert result == {"entries": [], "contexts_scanned": 0, "rows_scanned": 0}

    def test_context_with_no_rows_counts_the_context_not_causes(self):
        result = build_taxonomy([("m-1", []), ("m-2", [])])
        assert result["entries"] == []
        assert result["contexts_scanned"] == 2
        assert result["rows_scanned"] == 0

    def test_only_healthy_rows_stay_empty(self):
        rows = [
            checkpoint(ts=1.0),
            checkpoint(tier="tier0_cache_hit", tier0_status="hit", ts=2.0),
            orchestrator_turn("final", ts=3.0),
        ]
        result = build_taxonomy([("m-1", rows)])
        assert result["entries"] == []
        assert result["contexts_scanned"] == 1
        assert result["rows_scanned"] == 3

    def test_aggregation_across_movements_and_rows(self):
        contexts = [
            ("m-1", [latched(ts=100.0, movement_id="m-1"), latched(ts=300.0, movement_id="m-1")]),
            ("m-2", [latched(ts=200.0, movement_id="m-2")]),
        ]
        result = build_taxonomy(contexts)
        novel = {e["cause"]: e for e in result["entries"]}["novelty_latch:novel"]
        assert novel["count"] == 3
        assert novel["first_seen"] == 100.0
        assert novel["last_seen"] == 300.0
        # sample_ref pins the EARLIEST row and names its movement.
        assert novel["sample_ref"] == "m-1:decider_checkpoint:100.000"
        assert result["contexts_scanned"] == 2
        assert result["rows_scanned"] == 3

    def test_entry_shape_is_exactly_the_ledger_contract(self):
        result = build_taxonomy([("m-1", [latched()])])
        assert len(result["entries"]) == 2
        for entry in result["entries"]:
            assert set(entry) == {"cause", "count", "first_seen", "last_seen", "sample_ref"}

    def test_sorting_is_count_desc_then_cause_asc(self):
        rows = [
            latched(ts=1.0),                                # novelty_latch:novel + tier0:miss_novel
            checkpoint(tier="gemma", tier0_status=None, ts=2.0),
            checkpoint(tier="gemma", tier0_status=None, ts=3.0),
            alert("decider_unavailable", ts=4.0),
        ]
        causes = [e["cause"] for e in build_taxonomy([("m-1", rows)])["entries"]]
        assert causes == [
            "checkpoint_fallback:gemma",  # count 2 first
            "alert:decider_unavailable",  # count 1, alphabetical
            "novelty_latch:novel",
            "tier0:miss_novel",
        ]

    def test_timestamp_less_rows_count_without_seen(self):
        row = checkpoint(tier="gemma", tier0_status=None)
        del row["timestamp"]
        result = build_taxonomy([("m-1", [row, dict(row)])])
        entry = result["entries"][0]
        assert entry["count"] == 2
        assert entry["first_seen"] is None
        assert entry["last_seen"] is None
        assert entry["sample_ref"] == "m-1:decider_checkpoint:?"

    def test_first_parsable_timestamp_backfills_seen(self):
        row1 = checkpoint(tier="gemma", tier0_status=None)
        del row1["timestamp"]
        row2 = checkpoint(tier="gemma", tier0_status=None, ts=50.0)
        row3 = checkpoint(tier="gemma", tier0_status=None, ts=25.0)
        result = build_taxonomy([("m-1", [row1, row2, row3])])
        entry = result["entries"][0]
        assert entry["count"] == 3
        assert entry["first_seen"] == 25.0
        assert entry["last_seen"] == 50.0

    def test_result_is_json_safe(self):
        rows = [latched(ts=1.0), alert("decider_unavailable", ts=2.0)]
        payload = json.dumps(build_taxonomy([("m-1", rows)]))
        assert "novelty_latch:novel" in payload

    def test_deterministic_for_same_input(self):
        contexts = [("m-1", [latched(ts=1.0), checkpoint(ts=2.0)])]
        assert build_taxonomy(contexts) == build_taxonomy(
            [(mid, list(rows)) for mid, rows in contexts]
        )

    def test_mixed_degradation_log_end_to_end(self):
        """The ledger the Wave-1 + tier-0 demo scenario actually leaves."""
        contexts = [
            (
                "es9",
                [
                    latched(ts=10.0, movement_id="es9"),
                    alert("novel_situation_confident_answer", ts=10.1),
                    checkpoint(ts=11.0),
                    checkpoint(tier="gemma", tier0_status=None,
                               reason="decider degraded: model-host unreachable", ts=12.0),
                    alert("decider_unavailable", phase="operator_alert", ts=12.1),
                    checkpoint(novelty_status="disabled_unreachable",
                               tier0_status="miss_no_similarity_evidence", ts=13.0),
                ],
            ),
            ("es-t0", [checkpoint(tier="tier0_cache_hit", tier0_status="hit", ts=20.0)]),
        ]
        result = build_taxonomy(contexts)
        by_cause = {e["cause"]: e for e in result["entries"]}
        assert set(by_cause) == {
            "novelty_latch:novel",
            "tier0:miss_novel",
            "alert:novel_situation_confident_answer",
            "checkpoint_fallback:gemma",
            "alert:decider_unavailable",
            "novelty_latch:disabled_unreachable",
            "tier0:miss_no_similarity_evidence",
        }
        assert result["contexts_scanned"] == 2
        assert result["rows_scanned"] == 7
        assert by_cause["checkpoint_fallback:gemma"]["sample_ref"].startswith("es9:")


# ─── HTTP surface ─────────────────────────────────────────────────────────────

pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from traffic_oracle.__main__ import create_app  # noqa: E402
from traffic_oracle.ops.router import (  # noqa: E402
    CONTEXT_KEY_PREFIX,
    MAX_CONTEXTS,
    read_contexts,
    scan_contexts,
)


class FakeLedgerValkey:
    """Duck-typed valkey client: scan_iter + mget (+ get) over a dict store."""

    def __init__(self, payloads: dict[str, str] | None = None) -> None:
        self._payloads = dict(payloads or {})
        self.get_calls = 0
        self.mget_batches: list[int] = []

    def seed_context(self, movement_id: str, context: dict) -> None:
        self._payloads[f"{CONTEXT_KEY_PREFIX}{movement_id}"] = json.dumps(context)

    async def get(self, key: str) -> str | None:
        self.get_calls += 1
        return self._payloads.get(key)

    async def mget(self, keys: list[str]) -> list[str | None]:
        self.mget_batches.append(len(keys))
        return [self._payloads.get(k) for k in keys]

    def scan_iter(self, match: str = "*", count: int = 10) -> Any:
        prefix = match.rstrip("*")
        keys = sorted(k for k in self._payloads if k.startswith(prefix))

        async def gen() -> AsyncIterator[str]:
            for key in keys:
                yield key

        return gen()


def _context(movement_id: str, decisions_log: list[dict]) -> dict:
    return {"movement_id": movement_id, "vvip_class": "Z+", "status": "active",
            "decisions_log": decisions_log}


class TestDegradationTaxonomyEndpoint:
    async def test_503_when_valkey_client_absent(self):
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            res = await c.get("/api/v1/ops/degradation-taxonomy")
        assert res.status_code == 503

    async def test_empty_log_is_an_honest_empty_taxonomy(self):
        app = create_app()
        valkey = FakeLedgerValkey()
        valkey.seed_context("m-empty", _context("m-empty", []))
        app.state.adaptive_valkey = valkey
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            res = await c.get("/api/v1/ops/degradation-taxonomy")
        assert res.status_code == 200
        body = res.json()
        assert body["entries"] == []
        assert body["contexts_scanned"] == 1
        assert body["rows_scanned"] == 0
        assert body["truncated"] is False
        assert body["contexts_skipped_corrupt"] == 0
        assert "decisions_log" in body["source"]

    async def test_populated_scan_matches_the_pure_module(self):
        contexts = {
            "es9": _context("es9", [latched(ts=10.0, movement_id="es9"),
                                    checkpoint(ts=11.0)]),
            "m-2": _context("m-2", [checkpoint(tier="gemma", tier0_status=None, ts=20.0)]),
        }
        valkey = FakeLedgerValkey()
        for mid, ctx in contexts.items():
            valkey.seed_context(mid, ctx)
        app = create_app()
        app.state.adaptive_valkey = valkey
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            res = await c.get("/api/v1/ops/degradation-taxonomy")
        assert res.status_code == 200
        body = res.json()
        expected = build_taxonomy(
            [(ctx["movement_id"], ctx["decisions_log"]) for ctx in contexts.values()]
        )
        assert body["entries"] == expected["entries"]
        assert body["contexts_scanned"] == 2
        assert body["rows_scanned"] == 3
        assert body["generated_at"]

    async def test_corrupt_contexts_are_skipped_and_counted(self):
        valkey = FakeLedgerValkey()
        valkey.seed_context("m-ok", _context("m-ok", [latched(ts=1.0, movement_id="m-ok")]))
        valkey._payloads[f"{CONTEXT_KEY_PREFIX}m-bad"] = "{not json"
        valkey._payloads[f"{CONTEXT_KEY_PREFIX}m-wrongtype"] = "[1, 2, 3]"
        app = create_app()
        app.state.adaptive_valkey = valkey
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            res = await c.get("/api/v1/ops/degradation-taxonomy")
        assert res.status_code == 200
        body = res.json()
        assert body["contexts_scanned"] == 1
        assert body["contexts_skipped_corrupt"] == 2
        assert [e["cause"] for e in body["entries"]] == [
            "novelty_latch:novel", "tier0:miss_novel",
        ]

    async def test_truncation_flag_beyond_the_scan_bound(self):
        valkey = FakeLedgerValkey()
        for i in range(MAX_CONTEXTS + 3):
            valkey.seed_context(f"m-{i:04d}", _context(f"m-{i:04d}", []))
        app = create_app()
        app.state.adaptive_valkey = valkey
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            res = await c.get("/api/v1/ops/degradation-taxonomy")
        assert res.status_code == 200
        body = res.json()
        assert body["truncated"] is True
        assert body["contexts_scanned"] == MAX_CONTEXTS

    async def test_scan_is_batched_mget_not_per_key_get(self):
        valkey = FakeLedgerValkey()
        for i in range(250):
            valkey.seed_context(f"m-{i:04d}", _context(f"m-{i:04d}", [latched(ts=float(i))]))
        app = create_app()
        app.state.adaptive_valkey = valkey
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            res = await c.get("/api/v1/ops/degradation-taxonomy")
        assert res.status_code == 200
        assert res.json()["contexts_scanned"] == 250
        assert valkey.get_calls == 0
        assert valkey.mget_batches == [100, 100, 50]

    async def test_shared_scanner_returns_whole_payloads(self):
        valkey = FakeLedgerValkey()
        valkey.seed_context("m-1", _context("m-1", [latched(ts=1.0, movement_id="m-1")]))
        valkey._payloads[f"{CONTEXT_KEY_PREFIX}m-lost-id"] = json.dumps({"decisions_log": []})
        valkey._payloads[f"{CONTEXT_KEY_PREFIX}m-bad"] = "{not json"
        contexts, truncated, corrupt = await scan_contexts(valkey)
        assert truncated == 0 and corrupt == 1
        by_id = dict(contexts)
        assert by_id["m-1"]["vvip_class"] == "Z+"          # the full payload, not just rows
        assert by_id["m-lost-id"]["decisions_log"] == []   # key names the subject
        picked, corrupt = await read_contexts(
            valkey, [f"{CONTEXT_KEY_PREFIX}m-1", f"{CONTEXT_KEY_PREFIX}gone"])
        assert [m for m, _ in picked] == ["m-1"] and corrupt == 0

