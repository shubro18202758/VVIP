"""Hybrid retrieval: dense ANN recall → late-interaction MaxSim rerank.

This is the T1 MEMORY retrieval path the brain calls for "similar past
situations" (contract C3): bge-m3 dense ANN recall_k=100 over
traffic.memory_docs, then pplx-embed-v2-late MaxSim rerank to top-20.

Documents without token vectors fall back to their dense cosine score,
flagged via ``strategy='dense_fallback'`` — never silently mixed with
MaxSim scores.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Sequence

from . import config
from .encoders import (
    DenseEncoderProtocol,
    LateInteractionEncoder,
    TokenVecs,
    truncate_for_embedding,
    validate_dense_vector,
)
from .maxsim import cosine, maxsim_score
from .store import MemoryDocRow, MemoryStore

DEFAULT_SEARCH_KINDS: tuple[str, ...] = ("incident", "scenario_run", "aar")
"""Kinds included in the brain's 'similar past situations' contract."""


@dataclass(frozen=True)
class SearchHit:
    doc_id: str
    kind: str
    ref_id: str
    title: str
    snippet: str
    score: float
    maxsim_score: float | None
    dense_score: float | None
    strategy: str
    token_count: int | None
    occurred_at: object | None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "kind": self.kind,
            "ref_id": self.ref_id,
            "title": self.title,
            "snippet": self.snippet,
            "score": self.score,
            "maxsim_score": self.maxsim_score,
            "dense_score": self.dense_score,
            "strategy": self.strategy,
            "token_count": self.token_count,
            "occurred_at": (
                self.occurred_at.isoformat() if self.occurred_at else None
            ),
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class SearchResponse:
    query: str
    results: list[SearchHit]
    query_token_count: int
    recalled: int
    latency_ms: float

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "query_token_count": self.query_token_count,
            "recalled": self.recalled,
            "latency_ms": round(self.latency_ms, 2),
            "results": [hit.to_dict() for hit in self.results],
        }


def _snippet(body: str, width: int = 240) -> str:
    body = body.strip()
    return body if len(body) <= width else body[: width - 1] + "…"


class HybridRetriever:
    """Dense recall + MaxSim rerank over the memory store."""

    def __init__(
        self,
        store: MemoryStore,
        late_encoder: LateInteractionEncoder,
        dense_encoder: DenseEncoderProtocol,
    ) -> None:
        self._store = store
        self._late = late_encoder
        self._dense = dense_encoder

    async def search(
        self,
        query: str,
        *,
        kinds: Sequence[str] | None = None,
        top_k: int = config.DEFAULT_TOP_K,
        recall_k: int = config.DEFAULT_RECALL_K,
        movement_id: str | None = None,
    ) -> SearchResponse:
        """Run the hybrid pipeline for one query.

        Args:
            query: free-text situation description from the caller.
            kinds: doc kinds to include (default: the past-situations set).
            top_k: final results after MaxSim rerank (capped at MAX_TOP_K).
            recall_k: dense ANN candidate pool (capped at 100 per C3).
            movement_id: optional filter hint — when the store supports it,
                results are ranked but not filtered (movement is metadata).
        """
        started = time.perf_counter()
        query = truncate_for_embedding(query)
        top_k = max(1, min(int(top_k), config.MAX_TOP_K))
        recall_k = max(top_k, min(int(recall_k), config.DEFAULT_RECALL_K))
        selected_kinds = tuple(kinds) if kinds else DEFAULT_SEARCH_KINDS

        for kind in selected_kinds:
            if kind not in config.MEMORY_DOC_KINDS:
                raise ValueError(
                    f"kind {kind!r} not allowed; whitelist: "
                    f"{config.MEMORY_DOC_KINDS}"
                )

        query_tokens = self._late.encode_query(query)
        dense = validate_dense_vector(
            self._dense.encode([query])[0], dim=self._dense.dim
        )

        recalled: list[MemoryDocRow] = await self._store.recall_by_dense(
            dense, kinds=selected_kinds, recall_k=recall_k
        )
        if movement_id:
            # Prefer same-movement history at equal rank without hiding the
            # rest of the memory (soft preference, applied post-rerank).
            recalled.sort(
                key=lambda r: (r.movement_id != movement_id)
            )
        token_map = await self._store.fetch_token_vectors(
            [r.doc_id for r in recalled]
        )

        hits: list[SearchHit] = []
        for row in recalled:
            tokens: TokenVecs | None = token_map.get(row.doc_id)
            dense_score = cosine(dense, row.dense) if row.dense else 0.0
            if tokens:
                score = maxsim_score(query_tokens, tokens)
                hits.append(
                    SearchHit(
                        doc_id=row.doc_id,
                        kind=row.kind,
                        ref_id=row.ref_id,
                        title=row.title,
                        snippet=_snippet(row.body),
                        score=score,
                        maxsim_score=score,
                        dense_score=dense_score,
                        strategy="maxsim",
                        token_count=row.token_count,
                        occurred_at=row.occurred_at,
                        metadata=row.metadata,
                    )
                )
            else:
                hits.append(
                    SearchHit(
                        doc_id=row.doc_id,
                        kind=row.kind,
                        ref_id=row.ref_id,
                        title=row.title,
                        snippet=_snippet(row.body),
                        score=dense_score,
                        maxsim_score=None,
                        dense_score=dense_score,
                        strategy="dense_fallback",
                        token_count=None,
                        occurred_at=row.occurred_at,
                        metadata=row.metadata,
                    )
                )

        hits.sort(key=lambda h: (-h.score, h.doc_id))
        latency_ms = (time.perf_counter() - started) * 1000.0
        return SearchResponse(
            query=query,
            results=hits[:top_k],
            query_token_count=len(query_tokens),
            recalled=len(recalled),
            latency_ms=latency_ms,
        )

    async def similar_past_situations(
        self, query: str, *, top_k: int = config.DEFAULT_TOP_K
    ) -> SearchResponse:
        """The brain-facing contract: search over past situations by name."""
        return await self.search(query, kinds=DEFAULT_SEARCH_KINDS, top_k=top_k)


__all__ = [
    "DEFAULT_SEARCH_KINDS",
    "HybridRetriever",
    "SearchHit",
    "SearchResponse",
]
