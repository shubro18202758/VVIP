"""SQL hygiene for MemoryStore: parameter binding, whitelists, codecs.

Uses the recording MockPool — no database. A source scan additionally
asserts no f-string/format/percent SQL construction exists in the store
module (C12).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from embedding_service import config
from embedding_service.pgvector_codec import (
    parse_vector_array_text,
    parse_vector_text,
    vector_array_to_param,
    vector_array_to_text,
    vector_to_text,
)
from embedding_service.store import MemoryStore

STORE_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "embedding_service" / "store.py"
)


class TestSqlHygiene:
    def test_store_source_has_no_dynamic_sql_construction(self):
        source = STORE_PATH.read_text()
        # No f-strings, .format(, or % interpolation touching SQL constants.
        forbidden = [
            re.compile(r"f\"\"\""),          # f-string triple quotes
            re.compile(r"SQL\s*=\s*f\""),    # SQL = f"..."
            re.compile(r"\.format\("),
            re.compile(r"SQL\s*%\s*\("),
            re.compile(r"SQL\s*\+\s*"),
        ]
        for pattern in forbidden:
            assert not pattern.search(source), f"forbidden pattern {pattern.pattern}"

    def test_every_statement_uses_placeholders(self):
        source = STORE_PATH.read_text()
        statements = re.findall(r"^([A-Z_]+_SQL)\s*=", source, re.M)
        assert len(statements) >= 7  # all named statements present
        for name in statements:
            block = re.search(rf'{name}\s*=\s*"""(.*?)"""', source, re.S).group(1)
            # COUNT_DOCS_SQL takes no parameters by design; every other
            # statement must bind at least $1.
            if name == "COUNT_DOCS_SQL":
                continue
            assert "$1" in block, f"{name} has no bound parameters"

    async def test_upsert_binds_all_ten_params(self, mock_pool):
        store = MemoryStore(mock_pool)
        tokens = [[0.25] * config.TOKEN_VEC_DIM for _ in range(3)]
        dense = [0.001] * config.DENSE_DIM
        await store.upsert_document(
            kind="incident", ref_id="incident:1", title="t", body="b",
            metadata={"a": 1}, dense=dense, token_vecs=tokens,
        )
        kind, query, args = mock_pool.executed[0]
        assert kind == "fetchrow"
        assert "$1" in query and "$10" in query
        assert args[0] == "incident"
        assert args[1] == "incident:1"
        assert args[5] == vector_to_text(dense)
        # asyncpg only accepts a LIST for $n::vector(128)[] (verified
        # against pgvector 0.8.6) — never a single array-literal string.
        assert args[6] == vector_array_to_param(tokens)
        assert isinstance(args[6], list)
        assert args[7] == 3

    async def test_upsert_rejects_non_whitelisted_kind(self, mock_pool):
        store = MemoryStore(mock_pool)
        with pytest.raises(ValueError, match="whitelist"):
            await store.upsert_document(
                kind="secret_chat", ref_id="x", title="t", body="b",
                metadata=None, dense=None, token_vecs=None,
            )
        assert mock_pool.executed == []

    async def test_recall_binds_dense_kinds_limit(self, mock_pool):
        store = MemoryStore(mock_pool)
        await store.recall_by_dense([0.5] * config.DENSE_DIM, kinds=["incident"], recall_k=7)
        kind, query, args = mock_pool.executed[0]
        assert "<=> $1::vector" in query
        assert args[0] == vector_to_text([0.5] * config.DENSE_DIM)
        assert args[1] == ["incident"]
        assert args[2] == 7

    async def test_incident_whitelists_enforced_before_sql(self, mock_pool):
        store = MemoryStore(mock_pool)
        with pytest.raises(ValueError, match="incident_kind"):
            await store.insert_incident(
                incident_kind="buffer_overflow", severity="low", summary="x"
            )
        with pytest.raises(ValueError, match="severity"):
            await store.insert_incident(
                incident_kind="protest", severity="apocalyptic", summary="x"
            )
        with pytest.raises(ValueError, match="source"):
            await store.insert_incident(
                incident_kind="protest", severity="low", summary="x", source="spoofed"
            )
        assert mock_pool.executed == []

    async def test_recent_incidents_defaults_to_empty_filter(self, mock_pool):
        store = MemoryStore(mock_pool)
        await store.recent_incidents(limit=5)
        _, query, args = mock_pool.executed[0]
        assert "cardinality($1::text[]) = 0" in query
        assert args == ([], 5)


class TestPgvectorCodecs:
    def test_vector_text_roundtrip(self):
        vec = [0.1, -0.2, 3.5]
        text = vector_to_text(vec)
        assert text.startswith("[") and text.endswith("]")
        assert parse_vector_text(text) == pytest.approx(vec)

    def test_array_text_roundtrip(self):
        tokens = [[0.1] * 128, [-0.2] * 128]
        text = vector_array_to_text(tokens)
        assert text.startswith('{"[') and text.endswith(']"}')
        parsed = parse_vector_array_text(text)
        assert len(parsed) == 2
        assert parsed[0] == pytest.approx([0.1] * 128)

    def test_matches_postgres_array_literal_format(self):
        # Exact format produced by pgvector 0.8.6 (verified against a live
        # container: SELECT vs::text → {"[0.01,...]","[0.01,...]"})
        text = '{"[0.1,0.2]","[0.3,0.4]"}'
        parsed = parse_vector_array_text(text)
        assert parsed == [[0.1, 0.2], [0.3, 0.4]]

    def test_null_and_empty(self):
        assert parse_vector_array_text(None) is None
        assert parse_vector_array_text("NULL") is None
        assert parse_vector_array_text("{}") == []
