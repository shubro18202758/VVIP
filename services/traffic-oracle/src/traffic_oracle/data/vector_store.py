"""pgvector-based traffic pattern embedding store."""

from __future__ import annotations

import asyncpg
import structlog

logger = structlog.get_logger()


class TrafficVectorStore:
    """Stores and queries segment traffic pattern embeddings via pgvector."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def store_embedding(
        self, segment_id: int, pattern_type: str, embedding: list[float]
    ) -> None:
        """Insert or update a segment pattern embedding."""
        query = """
            INSERT INTO traffic.segment_embeddings (segment_id, pattern_type, embedding)
            VALUES ($1, $2, $3::vector)
            ON CONFLICT (segment_id, pattern_type)
            DO UPDATE SET embedding = EXCLUDED.embedding;
        """
        vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
        async with self._pool.acquire() as conn:
            await conn.execute(query, segment_id, pattern_type, vec_str)

    async def find_similar_segments(
        self, embedding: list[float], k: int = 10
    ) -> list[dict]:
        """Find K nearest segment embeddings using HNSW index."""
        query = """
            SELECT segment_id, pattern_type,
                   embedding <-> $1::vector AS distance
            FROM traffic.segment_embeddings
            ORDER BY embedding <-> $1::vector
            LIMIT $2;
        """
        vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, vec_str, k)
        return [dict(r) for r in rows]

    async def compute_daily_profile(self, segment_id: int) -> list[dict]:
        """Compute 24-hour speed profile from hourly aggregates."""
        query = """
            SELECT hour_of_day, avg_speed_kmh, avg_congestion_idx, observation_count
            FROM traffic.hourly_aggregates
            WHERE segment_id = $1
            ORDER BY hour_of_day;
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, segment_id)
        return [dict(r) for r in rows]
