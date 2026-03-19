"""pgRouting graph query wrappers for corridor routing."""

from __future__ import annotations

import asyncpg
import structlog

logger = structlog.get_logger()


class CorridorGraphDB:
    """PostGIS + pgRouting query interface for the corridor road graph."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def shortest_path(
        self, source: int, target: int
    ) -> list[dict]:
        """Find shortest path using pgr_dijkstra on corridor.road_graph."""
        # Embed validated ints directly to avoid pgRouting function overload ambiguity
        query = f"""
            SELECT seq, node, edge, cost, agg_cost
            FROM pgr_dijkstra(
                'SELECT id, source, target, cost, reverse_cost FROM corridor.road_graph',
                {int(source)}::bigint, {int(target)}::bigint, directed := true
            )
            ORDER BY seq;
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query)
        return [dict(r) for r in rows]

    async def k_shortest_paths(
        self, source: int, target: int, k: int = 5
    ) -> list[list[dict]]:
        """Find K shortest paths using pgr_KSP."""
        # Embed validated ints directly to avoid pgRouting function overload ambiguity
        query = f"""
            SELECT seq, path_id, path_seq, node, edge, cost, agg_cost
            FROM pgr_KSP(
                'SELECT id, source, target, cost, reverse_cost FROM corridor.road_graph',
                {int(source)}::bigint, {int(target)}::bigint, {int(k)}::integer, directed := true
            )
            ORDER BY path_id, path_seq;
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query)

        paths: dict[int, list[dict]] = {}
        for r in rows:
            pid = r["path_id"]
            paths.setdefault(pid, []).append(dict(r))
        return list(paths.values())

    async def multi_hop_neighbors(
        self, segment_id: int, hops: int = 3
    ) -> list[int]:
        """Find segments reachable within N hops via adjacency graph."""
        query = """
            WITH RECURSIVE neighbors AS (
                SELECT to_segment_id AS seg, 1 AS depth
                FROM corridor.segment_adjacency
                WHERE from_segment_id = $1
                UNION
                SELECT sa.to_segment_id, n.depth + 1
                FROM corridor.segment_adjacency sa
                JOIN neighbors n ON sa.from_segment_id = n.seg
                WHERE n.depth < $2
            )
            SELECT DISTINCT seg FROM neighbors;
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, segment_id, hops)
        return [r["seg"] for r in rows]

    async def isochrone(
        self, segment_id: int, max_cost_sec: float = 300.0
    ) -> list[dict]:
        """Compute driving-distance isochrone from a segment."""
        # Embed validated values directly to avoid pgRouting function overload ambiguity
        query = f"""
            SELECT seq, node, edge, cost, agg_cost
            FROM pgr_drivingDistance(
                'SELECT id, source, target, cost, reverse_cost FROM corridor.road_graph',
                {int(segment_id)}::bigint, {float(max_cost_sec)}::float, directed := true
            )
            ORDER BY agg_cost;
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query)
        return [dict(r) for r in rows]
