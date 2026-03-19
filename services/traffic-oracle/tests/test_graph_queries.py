"""Tests for pgRouting graph query wrappers."""

from __future__ import annotations

import pytest

from traffic_oracle.data.graph_queries import CorridorGraphDB

from conftest import MockPool


# ─── CorridorGraphDB Tests ────────────────────────────────────────────────────


class TestCorridorGraphDB:
    def _make_path_rows(self, n: int = 5) -> list[dict]:
        return [
            {"seq": i, "node": i * 10, "edge": i + 1, "cost": 5.0, "agg_cost": i * 5.0}
            for i in range(n)
        ]

    def _make_ksp_rows(self, k: int = 2, steps: int = 3) -> list[dict]:
        rows = []
        for pid in range(1, k + 1):
            for seq in range(steps):
                rows.append(
                    {
                        "seq": pid * 100 + seq,
                        "path_id": pid,
                        "path_seq": seq,
                        "node": pid * 10 + seq,
                        "edge": seq + 1,
                        "cost": 3.0,
                        "agg_cost": seq * 3.0,
                    }
                )
        return rows

    async def test_shortest_path_returns_rows(self):
        expected = self._make_path_rows(4)
        pool = MockPool(fetch_result=expected)
        db = CorridorGraphDB(pool)

        result = await db.shortest_path(source=1, target=100)

        assert len(result) == 4
        assert result[0]["seq"] == 0
        assert result[0]["node"] == 0
        assert result[-1]["agg_cost"] == 15.0

    async def test_shortest_path_empty(self):
        pool = MockPool(fetch_result=[])
        db = CorridorGraphDB(pool)

        result = await db.shortest_path(source=1, target=999)
        assert result == []

    async def test_shortest_path_query_params(self):
        pool = MockPool(fetch_result=[])
        db = CorridorGraphDB(pool)

        await db.shortest_path(source=42, target=99)

        query, args = pool.connection.executed_queries[0]
        assert "pgr_dijkstra" in query
        assert args == (42, 99)

    async def test_k_shortest_paths_groups_by_path_id(self):
        rows = self._make_ksp_rows(k=3, steps=4)
        pool = MockPool(fetch_result=rows)
        db = CorridorGraphDB(pool)

        result = await db.k_shortest_paths(source=1, target=100, k=3)

        assert len(result) == 3
        for path in result:
            assert len(path) == 4

    async def test_k_shortest_paths_query_params(self):
        pool = MockPool(fetch_result=[])
        db = CorridorGraphDB(pool)

        await db.k_shortest_paths(source=10, target=50, k=5)

        query, args = pool.connection.executed_queries[0]
        assert "pgr_KSP" in query
        assert args == (10, 50, 5)

    async def test_multi_hop_neighbors(self):
        rows = [{"seg": 200}, {"seg": 201}, {"seg": 202}]
        pool = MockPool(fetch_result=rows)
        db = CorridorGraphDB(pool)

        result = await db.multi_hop_neighbors(segment_id=100, hops=3)

        assert result == [200, 201, 202]

    async def test_multi_hop_neighbors_query_params(self):
        pool = MockPool(fetch_result=[])
        db = CorridorGraphDB(pool)

        await db.multi_hop_neighbors(segment_id=42, hops=5)

        query, args = pool.connection.executed_queries[0]
        assert "segment_adjacency" in query
        assert args == (42, 5)

    async def test_isochrone(self):
        expected = self._make_path_rows(3)
        pool = MockPool(fetch_result=expected)
        db = CorridorGraphDB(pool)

        result = await db.isochrone(segment_id=100, max_cost_sec=600.0)

        assert len(result) == 3
        query, args = pool.connection.executed_queries[0]
        assert "pgr_drivingDistance" in query
        assert args == (100, 600.0)

    async def test_isochrone_default_cost(self):
        pool = MockPool(fetch_result=[])
        db = CorridorGraphDB(pool)

        await db.isochrone(segment_id=100)

        _, args = pool.connection.executed_queries[0]
        assert args == (100, 300.0)
