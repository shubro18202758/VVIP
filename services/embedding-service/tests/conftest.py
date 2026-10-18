"""Shared fixtures for embedding-service tests.

All tests run against fakes — no live model, no live HTTP. Only the
pgvector migration tests touch a real (disposable, docker-managed)
database; they skip when docker is unavailable.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

# Make embedding_service and traffic_oracle importable without installation.
_HERE = Path(__file__).resolve().parent          # .../embedding-service/tests
_SERVICE_SRC = _HERE.parent / "src"               # .../embedding-service/src
_REPO = _HERE.parents[2]                          # .../VVIP
_TRAFFIC_ORACLE_SRC = _REPO / "services" / "traffic-oracle" / "src"
for _p in (str(_SERVICE_SRC), str(_TRAFFIC_ORACLE_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

from embedding_service import config
from embedding_service.encoders import (
    FakeDenseEncoder,
    FakeLateInteractionEncoder,
    TokenVecs,
    validate_dense_vector,
    validate_token_vectors,
)
from embedding_service.store import MemoryDocRow, MemoryStore


# ─── Mock asyncpg pool (records SQL + args, returns canned rows) ──────────────


class MockConnection:
    def __init__(self, fetch_result=None, fetchrow_result=None):
        self._fetch_result = fetch_result or []
        self._fetchrow_result = fetchrow_result
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        self.executed.append(("fetch", query, args))
        return self._fetch_result

    async def fetchrow(self, query, *args):
        self.executed.append(("fetchrow", query, args))
        return self._fetchrow_result

    async def execute(self, query, *args):
        self.executed.append(("execute", query, args))
        return None


class MockPool:
    def __init__(self, fetch_result=None, fetchrow_result=None):
        self.connection = MockConnection(fetch_result, fetchrow_result)
        self.executed = self.connection.executed

    def acquire(self):
        return _Ctx(self.connection)


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def mock_pool() -> MockPool:
    return MockPool(
        fetchrow_result={"doc_id": uuid.uuid4(), "incident_id": 7, "n": 0}
    )


# ─── In-memory semantic store (drives retrieval/API tests) ────────────────────


class FakeMemoryStore(MemoryStore):
    """Same method surface as MemoryStore, executed in memory.

    recall_by_dense ranks by true cosine over stored dense vectors, so the
    hybrid pipeline above it (MaxSim rerank) is tested on real math.
    """

    def __init__(self):
        super().__init__(pool=None)
        self.docs: dict[str, MemoryDocRow] = {}
        self.token_map: dict[str, TokenVecs | None] = {}
        self.incidents: list[dict] = []
        self.links: dict[int, str] = {}
        self._next_incident = 1

    async def upsert_document(self, *, kind, ref_id, title, body, metadata,
                              dense, token_vecs, movement_id=None,
                              occurred_at=None):
        if kind not in config.MEMORY_DOC_KINDS:
            raise ValueError(f"kind {kind!r} not allowed")
        doc_id = next(
            (
                d.doc_id
                for d in self.docs.values()
                if d.kind == kind and d.ref_id == ref_id
            ),
            str(uuid.uuid4()),
        )
        self.docs[doc_id] = MemoryDocRow(
            doc_id=doc_id,
            kind=kind,
            ref_id=ref_id,
            title=title,
            body=body,
            metadata=metadata or {},
            movement_id=movement_id,
            occurred_at=occurred_at,
            token_count=len(token_vecs) if token_vecs else None,
            dense=list(dense) if dense else None,
            dense_score=None,
        )
        self.token_map[doc_id] = token_vecs
        return doc_id

    async def recall_by_dense(self, dense, *, kinds=None, recall_k=100):
        from embedding_service.maxsim import cosine

        kind_set = set(kinds) if kinds else set(config.MEMORY_DOC_KINDS)
        scored = []
        for doc in self.docs.values():
            if doc.dense is None or doc.kind not in kind_set:
                continue
            scored.append((cosine(dense, doc.dense), doc))
        scored.sort(key=lambda pair: -pair[0])
        out = []
        for score, doc in scored[:recall_k]:
            out.append(
                MemoryDocRow(
                    doc_id=doc.doc_id,
                    kind=doc.kind,
                    ref_id=doc.ref_id,
                    title=doc.title,
                    body=doc.body,
                    metadata=doc.metadata,
                    movement_id=doc.movement_id,
                    occurred_at=doc.occurred_at,
                    token_count=doc.token_count,
                    dense=doc.dense,
                    dense_score=score,
                )
            )
        return out

    async def fetch_token_vectors(self, doc_ids):
        return {d: self.token_map.get(d) for d in doc_ids}

    async def insert_incident(self, *, incident_kind, severity, summary,
                              segment_ids=None, movement_id=None,
                              details=None, outcome=None,
                              scenario_run_id=None, source="live",
                              occurred_at=None):
        if incident_kind not in config.INCIDENT_KINDS:
            raise ValueError(f"incident_kind {incident_kind!r} not allowed")
        if severity not in config.INCIDENT_SEVERITIES:
            raise ValueError(f"severity {severity!r} not allowed")
        if source not in config.INCIDENT_SOURCES:
            raise ValueError(f"source {source!r} not allowed")
        incident_id = self._next_incident
        self._next_incident += 1
        self.incidents.append(
            {
                "incident_id": incident_id,
                "incident_kind": incident_kind,
                "severity": severity,
                "movement_id": movement_id,
                "segment_ids": list(segment_ids or []),
                "summary": summary,
                "details": details or {},
                "outcome": outcome,
                "scenario_run_id": scenario_run_id,
                "source": source,
                "memory_doc_id": None,
                "occurred_at": occurred_at,
            }
        )
        return incident_id

    async def link_memory_doc(self, incident_id, doc_id):
        self.links[incident_id] = doc_id
        for row in self.incidents:
            if row["incident_id"] == incident_id:
                row["memory_doc_id"] = doc_id

    async def recent_incidents(self, *, limit=20, kinds=None):
        rows = self.incidents
        if kinds:
            rows = [r for r in rows if r["incident_kind"] in set(kinds)]
        return rows[-limit:][::-1]

    async def count_documents(self):
        return len(self.docs)


# ─── Standard fake stacks ─────────────────────────────────────────────────────


@pytest.fixture
def fake_store() -> FakeMemoryStore:
    return FakeMemoryStore()


@pytest.fixture
def late_encoder() -> FakeLateInteractionEncoder:
    return FakeLateInteractionEncoder()


@pytest.fixture
def dense_encoder() -> FakeDenseEncoder:
    return FakeDenseEncoder()


@pytest.fixture
def retriever(fake_store, late_encoder, dense_encoder):
    from embedding_service.retrieval import HybridRetriever

    return HybridRetriever(fake_store, late_encoder, dense_encoder)


@pytest.fixture
def api(fake_store, late_encoder, dense_encoder):
    from embedding_service.api import EmbeddingApi
    from embedding_service.incidents import IncidentIngestor
    from embedding_service.retrieval import HybridRetriever

    r = HybridRetriever(fake_store, late_encoder, dense_encoder)
    ingestor = IncidentIngestor(fake_store, late_encoder, dense_encoder)
    return EmbeddingApi(store=fake_store, retriever=r, ingestor=ingestor)


__all__ = [
    "FakeMemoryStore",
    "MockPool",
    "validate_dense_vector",
    "validate_token_vectors",
]
