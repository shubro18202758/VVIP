"""Batch-inserts decoded traffic observations into PostGIS."""

from __future__ import annotations

import asyncpg
import polars as pl
import structlog

logger = structlog.get_logger()


class DbWriter:
    """Writes Polars DataFrames of traffic observations to PostGIS via COPY."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def write_batch(self, df: pl.DataFrame) -> int:
        """Insert a batch of observations into traffic.observations.

        Returns the number of rows inserted.
        """
        if df.is_empty():
            return 0

        records = []
        for row in df.iter_rows(named=True):
            records.append((
                row["timestamp_ms"],
                row["lon"],
                row["lat"],
                str(row["segment_id"]),
                row["speed_kmh"],
                row["congestion_index"],
                row["source"],
                row.get("data_quality", 0),
                row.get("confidence", 1.0),
            ))

        query = """
            INSERT INTO traffic.observations
                (timestamp_ms, lon, lat, segment_id, speed_kmh, congestion_idx,
                 source, data_quality, confidence)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """

        async with self._pool.acquire() as conn:
            await conn.executemany(query, records)

        logger.debug("wrote observation batch", rows=len(records))
        return len(records)
