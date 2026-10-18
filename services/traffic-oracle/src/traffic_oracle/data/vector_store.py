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
        """Compute 24-hour speed profile from hourly aggregates.

        C11 fix: the previous query selected columns that do not exist
        (hour_of_day / avg_congestion_idx / observation_count); the actual
        traffic.hourly_aggregates columns (001_spatial_init.sql:88-96) are
        hour_utc / avg_speed_kmh / p50_speed_kmh / p95_congestion /
        observation_cnt. Aggregate across all days per UTC hour, keeping the
        historical output keys stable:

          hour_of_day        ← EXTRACT(HOUR FROM hour_utc)
          avg_speed_kmh      ← AVG(avg_speed_kmh)
          avg_congestion_idx ← AVG(p95_congestion)
          observation_count  ← SUM(observation_cnt)
        """
        query = """
            SELECT EXTRACT(HOUR FROM hour_utc)::INT AS hour_of_day,
                   AVG(avg_speed_kmh)::float8 AS avg_speed_kmh,
                   AVG(p95_congestion)::float8 AS avg_congestion_idx,
                   COALESCE(SUM(observation_cnt), 0)::bigint AS observation_count
            FROM traffic.hourly_aggregates
            WHERE segment_id = $1
            GROUP BY 1
            ORDER BY 1;
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, segment_id)
        return [dict(r) for r in rows]

    async def store_memory_embedding(
        self, segment_id: int, model_name: str, embedding: list[float]
    ) -> None:
        """Insert or update a model-derived segment embedding.

        C11 fix: model embeddings (e.g. DSTGAT GAT layer) previously tried
        traffic.segment_embeddings with pattern_type='gat_l3', violating the
        CHECK constraint in 002_graph_extensions.sql (only daily_profile /
        weekly_profile / event_response / incident_response are allowed).
        They now live in traffic.memory_segment_embeddings (migration 004),
        keyed by model name, dimension 64 (DSTGATConfig.hidden_dim).
        """
        query = """
            INSERT INTO traffic.memory_segment_embeddings
                (segment_id, model_name, embedding)
            VALUES ($1, $2, $3::vector)
            ON CONFLICT (segment_id, model_name)
            DO UPDATE SET embedding = EXCLUDED.embedding,
                          computed_at = NOW();
        """
        vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
        async with self._pool.acquire() as conn:
            await conn.fetchrow(query, segment_id, model_name, vec_str)

    async def find_similar_memory_segments(
        self, embedding: list[float], model_name: str, k: int = 10
    ) -> list[dict]:
        """Find K nearest model-derived segment embeddings (cosine, HNSW)."""
        query = """
            SELECT segment_id, model_name,
                   embedding <=> $1::vector AS distance
            FROM traffic.memory_segment_embeddings
            WHERE model_name = $2
            ORDER BY embedding <=> $1::vector
            LIMIT $3;
        """
        vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, vec_str, model_name, k)
        return [dict(r) for r in rows]
