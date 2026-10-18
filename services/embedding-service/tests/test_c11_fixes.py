"""C11 regression tests: the two verified pre-existing defects.

(a) compute_daily_profile selected non-existent columns
    (hour_of_day/avg_congestion_idx/observation_count vs actual
    hour_utc/avg_speed_kmh/p50_speed_kmh/p95_congestion/observation_cnt —
    001_spatial_init.sql:88-96);
(b) training_pipeline stored pattern_type='gat_l3', violating the CHECK
    constraint in 002_graph_extensions.sql:17-19.

The live-SQL halves of both regressions run in test_migration_pgvector.py
against a real pgvector container; this file pins the query text and source
shape without needing torch (training_pipeline only gets py_compile'd —
importing it requires torch, which no test venv here carries).
"""

from __future__ import annotations

import py_compile
import re
from pathlib import Path

import pytest

from traffic_oracle.data.vector_store import TrafficVectorStore

from conftest import MockPool

REPO = Path(__file__).resolve().parents[3]
VECTOR_STORE = REPO / "services" / "traffic-oracle" / "src" / "traffic_oracle" / "data" / "vector_store.py"
TRAINING_PIPELINE = (
    REPO
    / "services"
    / "traffic-oracle"
    / "src"
    / "traffic_oracle"
    / "predict"
    / "training_pipeline.py"
)


class TestC11A_DailyProfileColumns:
    async def test_query_uses_real_columns(self):
        pool = MockPool(fetch_result=[])
        store = TrafficVectorStore(pool)
        await store.compute_daily_profile(segment_id=42)
        query = pool.connection.executed[0][1]
        # Sources REAL columns only (001_spatial_init.sql:88-96):
        assert "EXTRACT(HOUR FROM hour_utc)" in query
        assert "AVG(avg_speed_kmh)" in query
        assert "AVG(p95_congestion)" in query
        assert "SUM(observation_cnt)" in query
        assert "FROM traffic.hourly_aggregates" in query
        # The fictional SOURCE columns must not be read: the old query had
        # bare `SELECT hour_of_day, ..., avg_congestion_idx, observation_count`
        # with no EXTRACT/aggregate over the real columns.
        assert "SELECT hour_of_day," not in query
        assert "SELECT EXTRACT" in query  # alias is computed, not read
        # Grouped into 24 hours, still parameter-bound
        assert "GROUP BY" in query
        assert "$1" in query
        assert pool.connection.executed[0][2] == (42,)

    async def test_query_output_keys_stable(self):
        rows = [
            {
                "hour_of_day": h,
                "avg_speed_kmh": 50.0 - h,
                "avg_congestion_idx": 0.01 * h,
                "observation_count": 60,
            }
            for h in range(24)
        ]
        pool = MockPool(fetch_result=rows)
        store = TrafficVectorStore(pool)
        profile = await store.compute_daily_profile(segment_id=1)
        assert len(profile) == 24
        assert profile[0]["hour_of_day"] == 0
        assert profile[23]["hour_of_day"] == 23


class TestC11B_GatEmbeddingPath:
    async def test_store_memory_embedding_binds_params(self):
        pool = MockPool(fetchrow_result={"incident_id": 1})
        store = TrafficVectorStore(pool)
        await store.store_memory_embedding(
            5001, "dstgat_gat_l3", [0.1] * 64
        )
        query = pool.connection.executed[0][1]
        assert "memory_segment_embeddings" in query
        assert "ON CONFLICT (segment_id, model_name)" in query
        args = pool.connection.executed[0][2]
        assert args[0] == 5001
        assert args[1] == "dstgat_gat_l3"
        assert args[2].startswith("[") and args[2].endswith("]")

    async def test_find_similar_memory_segments_binds_params(self):
        pool = MockPool(fetch_result=[])
        store = TrafficVectorStore(pool)
        await store.find_similar_memory_segments([0.2] * 64, "dstgat_gat_l3", k=4)
        query = pool.connection.executed[0][1]
        assert "memory_segment_embeddings" in query
        assert "<=> $1::vector" in query  # cosine distance on the new table
        assert pool.connection.executed[0][2] == (
            "[0.2" + ",0.2" * 63 + "]",
            "dstgat_gat_l3",
            4,
        )

    def test_training_pipeline_no_longer_writes_gat_l3_to_legacy_table(self):
        source = TRAINING_PIPELINE.read_text()
        # The violating legacy-store call is gone (docstrings may still
        # explain the history, so we assert on call sites, not substrings)…
        assert ".store_embedding(" not in source
        assert "self._vector_store.store_embedding(" not in source
        # …replaced by the memory-table path with real segment ids.
        assert ".store_memory_embedding(" in source
        assert "dstgat_gat_l3" in source
        # _extract_embeddings now receives segment_ids from the training window
        assert "windows[0].segment_ids" in source

    def test_training_pipeline_still_compiles(self):
        py_compile.compile(str(TRAINING_PIPELINE), doraise=True)

    def test_full_64_dim_embedding_kept(self):
        source = TRAINING_PIPELINE.read_text()
        # The old code sliced to :24 for the legacy vector(24) column; the
        # new table is vector(64) and the slice must be gone.
        assert ":24].tolist()" not in source
        assert "h[0, j, :].tolist()" in source


class TestMigrationShape:
    """Static assertions tying the C11 fix to migration 004."""

    def test_migration_creates_memory_segment_embeddings_vector64(self):
        sql = (
            REPO
            / "services"
            / "corridor-store"
            / "migrations"
            / "004_memory_embeddings.sql"
        ).read_text()
        assert "traffic.memory_segment_embeddings" in sql
        assert "vector(64)" in sql
        # additive-only: no DROP/ALTER on pre-existing objects
        for forbidden in ("DROP TABLE traffic.segment_embeddings",
                          "ALTER TABLE traffic.segment_embeddings",
                          "ALTER TABLE traffic.observations",
                          "ALTER TABLE traffic.hourly_aggregates"):
            assert forbidden not in sql

    def test_migration_is_idempotent_by_construction(self):
        sql = (
            REPO
            / "services"
            / "corridor-store"
            / "migrations"
            / "004_memory_embeddings.sql"
        ).read_text()
        create_tables = re.findall(
            r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\.(\w+)", sql
        )
        assert create_tables, "expected CREATE TABLE statements"
        for schema, table in create_tables:
            assert f"CREATE TABLE IF NOT EXISTS {schema}.{table}" in sql
