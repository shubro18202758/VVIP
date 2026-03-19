"""Historical pattern matching for synthetic data generation."""

from __future__ import annotations

from datetime import datetime

import asyncpg
import structlog

logger = structlog.get_logger()


class PatternMatcher:
    """Queries hourly aggregates for historically typical traffic patterns."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def match_historical(
        self, segment_id: int, target_time: datetime
    ) -> dict | None:
        """Find the historical average for this segment at this hour/dow.

        Returns dict with speed_kmh and congestion_idx, or None if insufficient data.
        """
        hour = target_time.hour
        dow = target_time.weekday()

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT avg_speed_kmh, avg_congestion_idx, observation_count
                FROM traffic.hourly_aggregates
                WHERE segment_id = $1 AND hour_of_day = $2 AND day_of_week = $3
                """,
                segment_id, hour, dow,
            )

        if row is None or row["observation_count"] < 10:
            return None

        return {
            "speed_kmh": float(row["avg_speed_kmh"]),
            "congestion_idx": float(row["avg_congestion_idx"]),
        }
