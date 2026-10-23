"""Embedding-memory client for the decider's novelty latch (contract C3).

The embedding service (default :8085) owns encoding: the brain POSTs the
plain-text state document to ``/search`` and reads back MaxSim-ranked past
situations — the brain needs no local model. This client never raises:
every transport/HTTP/parse failure collapses to ``status='unavailable'`` so
the decider can disable its novelty latch honestly per checkpoint instead
of benching the strategic tier.

Write side (decision-memory feeder): :meth:`record_decision` POSTs one
served decider checkpoint's state document to ``/embed/documents`` with
``kind='decision'`` — the corpus half of NOVELTY_SEARCH_KINDS that nothing
else in the stack feeds. Without it the novelty latch and the tier-0 gate
read a corpus that never gains a decision doc, so every checkpoint is
``miss_novel`` on live traffic no matter how routine. The feeder records
SITUATIONS (the state doc), never answers: which answers may be served
stays gated in decider.py (low-confidence/latched answers never enter the
tier-0 ANSWER cache; recording the situation on record does not launder
anything). It is fire-and-log — like ``search`` it never raises, so a
memory outage can never break a checkpoint.

Structurally a clone of :class:`convoy_brain.adaptive_params.AdaptiveParamsClient`
(constructor URL guard via ``assert_platform_service_url``, lazy client,
``aclose``).

Score scales: ``maxsim_score`` and dense ``dense_fallback`` scores are NOT
comparable (embedding-service retrieval.py documents this). This client
therefore only reports ``best_score`` over rows that carry a MaxSim score;
when every row fell back to dense scores, ``maxsim_present`` is False and
``best_score`` is None — the caller must treat that as "insufficient
evidence", never as novel.

MaxSim NORMALIZATION (calibrated 2026-10-19): the service's raw MaxSim is a
SUM over query tokens of the best cosine per token (maxsim.py), so its
scale is unbounded by query length — the 172-char gate state doc's
self-match measured 55.40 raw. The brain's NOVELTY (0.35) and TIER0 (0.75)
floors are 0..1 fractions, so this client normalizes BEFORE the decider
sees a score: ``best_score = raw / query_token_count`` (the response's
``query_token_count``), i.e. the mean best-match cosine over the query's
tokens — a self-match is ~1.0, unrelated situations sit well below.
``best_score_raw`` keeps the service's raw value for audit/calibration.
When the response omits ``query_token_count`` (older service), the raw
score passes through unchanged and the floors compare raw — documented
fail-open, same as today's behavior pre-normalization.

Cost control (documented equivalents — do not "optimize" these away):
    - The decider memoizes one search per ``(movement_id, state_doc)`` for
      ``NOVELTY_MEMO_TTL_SEC``, which suppresses the orchestrator's chained
      ``force=True`` tool-selection checkpoints (up to ``MAX_TOOL_ROUNDS=6``
      per operator turn) — one embedding search per distinct situation, not
      per checkpoint.
    - Deliberately REJECTED as an equivalent: ``GET /incidents/recent`` on
      this same service. Recency cannot answer "is this situation novel";
      only similarity can. Do not replace the /search call with it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import structlog

from convoy_brain.config import (
    DECISION_MEMORY_RECORD_TIMEOUT_SEC,
    NOVELTY_MEMORY_TOP_K,
    NOVELTY_SEARCH_TIMEOUT_SEC,
)
from convoy_brain.http_guard import assert_platform_service_url

logger = structlog.get_logger(__name__)

#: Past-situation kinds plus the decider's own decisions (contract C3).
NOVELTY_SEARCH_KINDS = ["incident", "scenario_run", "aar", "decision"]


@dataclass(frozen=True)
class MemorySearchResult:
    """One /search outcome; ``status='unavailable'`` rows carry no evidence."""

    status: str  # 'ok' | 'unavailable'
    best_score: float | None  # best NORMALIZED maxsim (raw/query_token_count), None if none
    best_strategy: str | None
    best_doc_id: str | None
    best_kind: str | None
    best_title: str | None
    hits: int
    maxsim_present: bool
    latency_ms: float
    error: str | None = None
    best_score_raw: float | None = None  # service-side raw MaxSim (audit/calibration)
    # The best row's ref_id: for kind=decision it is the feeder's
    # ``decision:{movement_id}:{trigger}:{signature}``, so the decider can tell
    # a movement's OWN decision docs from another movement's.
    best_ref_id: str | None = None

    def to_audit(self) -> dict:
        """JSON-safe audit view (round(…, 4) convention of decider.py)."""
        return {
            "status": self.status,
            "best_score": round(self.best_score, 4) if self.best_score is not None else None,
            "best_strategy": self.best_strategy,
            "best_doc_id": self.best_doc_id,
            "best_kind": self.best_kind,
            "hits": self.hits,
            "maxsim_present": self.maxsim_present,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


def _ok_result(
    *,
    rows: list[dict],
    latency_ms: float,
    query_token_count: int | None = None,
) -> MemorySearchResult:
    """Fold a valid ``results`` list into the audit-shaped result.

    Raw MaxSim is normalized to 0..1 by ``query_token_count`` when the
    response provides it (see the module docstring's calibration note);
    absent token counts pass the raw score through (fail-open).
    """
    best: dict | None = None
    best_score: float | None = None
    maxsim_present = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        score = row.get("maxsim_score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue  # dense_fallback row — scores must not be mixed
        maxsim_present = True
        if best_score is None or float(score) > best_score:
            best_score = float(score)
            best = row
    raw = best_score
    normalized = best_score
    if best_score is not None and isinstance(query_token_count, int) and query_token_count > 0:
        normalized = best_score / query_token_count
    return MemorySearchResult(
        status="ok",
        best_score=normalized,
        best_strategy=str(best.get("strategy")) if best else None,
        best_doc_id=str(best.get("doc_id")) if best else None,
        best_kind=str(best.get("kind")) if best else None,
        best_title=str(best.get("title")) if best else None,
        hits=len(rows),
        maxsim_present=maxsim_present,
        latency_ms=latency_ms,
        best_score_raw=raw,
        best_ref_id=(
            str(best["ref_id"]) if best and best.get("ref_id") is not None else None
        ),
    )


class MemoryClient:
    """Thin async client for the embedding service's POST /search."""

    def __init__(
        self,
        base_url: str,
        timeout_sec: float = NOVELTY_SEARCH_TIMEOUT_SEC,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = assert_platform_service_url(base_url.rstrip("/"))
        self._timeout = timeout_sec
        self._owns_client = client is None
        self._client = client
        logger.info("memory_client.init", base_url=self._base_url)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(
        self,
        state_doc: str,
        *,
        movement_id: str | None = None,
        top_k: int = NOVELTY_MEMORY_TOP_K,
    ) -> MemorySearchResult:
        """Search recent memory for situations similar to ``state_doc``.

        All failures (transport, 4xx/5xx, non-JSON, missing/malformed
        ``results``) return ``status='unavailable'`` — this method never
        raises.
        """
        started = time.monotonic()
        try:
            client = await self._ensure_client()
            resp = await client.post(
                f"{self._base_url}/search",
                json={
                    "query": state_doc,
                    "kinds": NOVELTY_SEARCH_KINDS,
                    "top_k": top_k,
                    "movement_id": movement_id,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            return self._unavailable(str(exc), started)

        latency_ms = round((time.monotonic() - started) * 1000, 1)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            return self._unavailable("malformed payload: no results list", started)
        query_token_count = payload.get("query_token_count")
        if not isinstance(query_token_count, int) or isinstance(query_token_count, bool):
            query_token_count = None
        return _ok_result(
            rows=payload["results"],
            latency_ms=latency_ms,
            query_token_count=query_token_count,
        )

    def _unavailable(self, error: str, started: float) -> MemorySearchResult:
        latency_ms = round((time.monotonic() - started) * 1000, 1)
        logger.info("memory_client.search_unavailable", error=error[:200])
        return MemorySearchResult(
            status="unavailable",
            best_score=None,
            best_strategy=None,
            best_doc_id=None,
            best_kind=None,
            best_title=None,
            hits=0,
            maxsim_present=False,
            latency_ms=latency_ms,
            error=error[:200],
        )

    async def record_decision(
        self,
        state_doc: str,
        *,
        movement_id: str,
        ref_id: str,
        title: str,
        metadata: dict | None = None,
        timeout_sec: float = DECISION_MEMORY_RECORD_TIMEOUT_SEC,
    ) -> bool:
        """Upsert one served decider checkpoint into the memory corpus.

        Posts the checkpoint's state document to ``/embed/documents`` with
        ``kind='decision'`` (a NOVELTY_SEARCH_KINDS member) so the corpus
        gains the situations the decider has actually decided — the read
        side (novelty latch + tier-0 similarity leg) then has something to
        recall on repeat traffic. ``ref_id`` is the caller's stable
        upsert key (decider.py derives it from movement/trigger/questions
        signature, mirroring the tier-0 cache key), so a re-decided
        situation refreshes its doc in place instead of duplicating rows.

        Like :meth:`search` this NEVER raises: any transport/HTTP/parse
        failure returns False (the caller logs + counts a metric and moves
        on — a feeder outage must never break a checkpoint).
        """
        started = time.monotonic()
        try:
            client = await self._ensure_client()
            resp = await client.post(
                f"{self._base_url}/embed/documents",
                json={
                    "kind": "decision",
                    "documents": [
                        {
                            "ref_id": ref_id,
                            "title": title,
                            "body": state_doc,
                            "movement_id": movement_id,
                            "metadata": metadata or {},
                        }
                    ],
                },
                timeout=timeout_sec,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.info(
                "memory_client.record_decision_unavailable", error=str(exc)[:200]
            )
            return False
        latency_ms = round((time.monotonic() - started) * 1000, 1)
        embedded = payload.get("embedded") if isinstance(payload, dict) else None
        if not isinstance(embedded, int) or embedded < 1:
            logger.info(
                "memory_client.record_decision_malformed",
                payload=str(payload)[:200],
            )
            return False
        logger.info(
            "memory_client.record_decision_ok",
            ref_id=ref_id,
            movement_id=movement_id,
            latency_ms=latency_ms,
        )
        return True
