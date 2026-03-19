"""Tests for pgvector-based TrafficVectorStore."""

from __future__ import annotations

import pytest

from traffic_oracle.data.vector_store import TrafficVectorStore

from conftest import MockPool


# ─── TrafficVectorStore Tests ─────────────────────────────────────────────────


class TestTrafficVectorStore:
    async def test_store_embedding_executes_query(self):
        pool = MockPool()
        store = TrafficVectorStore(pool)

        embedding = [0.1, 0.2, 0.3, 0.4] * 6  # 24-dim
        await store.store_embedding(segment_id=100, pattern_type="daily", embedding=embedding)

        assert len(pool.connection.executed_queries) == 1
        query, args = pool.connection.executed_queries[0]
        assert "segment_embeddings" in query
        assert "INSERT" in query.upper()
        assert args[0] == 100
        assert args[1] == "daily"

    async def test_store_embedding_formats_vector(self):
        pool = MockPool()
        store = TrafficVectorStore(pool)

        embedding = [1.0, 2.5, -0.3]
        await store.store_embedding(segment_id=42, pattern_type="weekly", embedding=embedding)

        _, args = pool.connection.executed_queries[0]
        vec_str = args[2]
        assert vec_str == "[1.0,2.5,-0.3]"

    async def test_find_similar_segments(self):
        mock_rows = [
            {"segment_id": 200, "pattern_type": "daily", "distance": 0.1},
            {"segment_id": 201, "pattern_type": "daily", "distance": 0.3},
        ]
        pool = MockPool(fetch_result=mock_rows)
        store = TrafficVectorStore(pool)

        results = await store.find_similar_segments(embedding=[0.5] * 24, k=10)

        assert len(results) == 2
        assert results[0]["segment_id"] == 200
        assert results[0]["distance"] == 0.1

    async def test_find_similar_query_params(self):
        pool = MockPool(fetch_result=[])
        store = TrafficVectorStore(pool)

        await store.find_similar_segments(embedding=[1.0, 2.0], k=5)

        query, args = pool.connection.executed_queries[0]
        assert "<->" in query  # L2 distance operator
        assert args[0] == "[1.0,2.0]"
        assert args[1] == 5

    async def test_compute_daily_profile(self):
        mock_rows = [
            {"hour_of_day": h, "avg_speed_kmh": 50.0 - h, "avg_congestion_idx": h * 0.04, "observation_count": 100}
            for h in range(24)
        ]
        pool = MockPool(fetch_result=mock_rows)
        store = TrafficVectorStore(pool)

        profile = await store.compute_daily_profile(segment_id=100)

        assert len(profile) == 24
        assert profile[0]["hour_of_day"] == 0
        assert profile[23]["hour_of_day"] == 23

    async def test_compute_daily_profile_query_params(self):
        pool = MockPool(fetch_result=[])
        store = TrafficVectorStore(pool)

        await store.compute_daily_profile(segment_id=42)

        query, args = pool.connection.executed_queries[0]
        assert "hourly_aggregates" in query
        assert args == (42,)

    async def test_compute_daily_profile_empty(self):
        pool = MockPool(fetch_result=[])
        store = TrafficVectorStore(pool)

        profile = await store.compute_daily_profile(segment_id=999)
        assert profile == []
