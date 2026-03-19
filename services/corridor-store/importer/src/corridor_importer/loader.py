"""Bulk loads extracted road network data into PostGIS."""

from __future__ import annotations

import asyncpg
import structlog

from corridor_importer.schema_mapper import MappedJunction, MappedSegment
from corridor_importer.topology_builder import AdjacencyEdge

logger = structlog.get_logger()


class BulkLoader:
    """Loads segments, junctions, and adjacency into PostGIS via asyncpg."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: asyncpg.Connection | None = None

    async def connect(self) -> None:
        self._conn = await asyncpg.connect(self._dsn)
        logger.info("bulk loader connected", dsn=self._dsn)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    def _get_conn(self) -> asyncpg.Connection:
        if self._conn is None:
            raise RuntimeError("Not connected — call connect() first")
        return self._conn

    async def load_segments(self, segments: list[MappedSegment]) -> int:
        """Bulk insert road segments into corridor.road_segments."""
        conn = self._get_conn()
        records = [
            (
                seg.osm_way_id,
                seg.road_class,
                seg.lanes,
                seg.speed_limit_kmh,
                seg.oneway,
                seg.geom_wkt,
            )
            for seg in segments
        ]

        await conn.executemany(
            """
            INSERT INTO corridor.road_segments
                (osm_way_id, road_class, lanes, speed_limit_kmh, oneway, geom)
            VALUES ($1, $2, $3, $4, $5, ST_GeomFromText($6, 4326))
            ON CONFLICT (osm_way_id) DO NOTHING
            """,
            records,
        )
        logger.info("loaded road segments", count=len(records))
        return len(records)

    async def load_junctions(self, junctions: list[MappedJunction]) -> int:
        """Bulk insert junctions into corridor.junctions."""
        conn = self._get_conn()
        records = [
            (j.osm_node_id, j.lon, j.lat, j.junction_type)
            for j in junctions
        ]

        await conn.executemany(
            """
            INSERT INTO corridor.junctions
                (osm_node_id, geom, junction_type)
            VALUES ($1, ST_SetSRID(ST_MakePoint($2, $3), 4326), $4)
            ON CONFLICT (osm_node_id) DO NOTHING
            """,
            records,
        )
        logger.info("loaded junctions", count=len(records))
        return len(records)

    async def load_adjacency(self, edges: list[AdjacencyEdge]) -> int:
        """Bulk insert adjacency edges into corridor.segment_adjacency."""
        conn = self._get_conn()
        records = [
            (e.from_segment_id, e.to_segment_id, e.via_node, e.turn_cost_sec)
            for e in edges
        ]

        await conn.executemany(
            """
            INSERT INTO corridor.segment_adjacency
                (from_segment_id, to_segment_id, via_junction_id, turn_cost_sec)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT DO NOTHING
            """,
            records,
        )
        logger.info("loaded adjacency edges", count=len(records))
        return len(records)
