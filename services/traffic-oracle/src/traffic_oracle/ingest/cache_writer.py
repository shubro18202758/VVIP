"""Updates Valkey cache with latest per-segment observations."""

from __future__ import annotations

import polars as pl
import structlog

from traffic_oracle.data.cache import TrafficCache
from traffic_oracle.data.models import DataQuality, FeedSource, TrafficObservation

logger = structlog.get_logger()

# Map source ordinals back to FeedSource enum
_SOURCE_MAP = {
    0: FeedSource.GOVERNMENT_TRAFFIC,
    1: FeedSource.MAPPING_API,
    2: FeedSource.FLEET_GPS,
    3: FeedSource.CROWDSOURCE,
}

_QUALITY_MAP = {
    0: DataQuality.REAL,
    1: DataQuality.ANOMALOUS,
    2: DataQuality.SYNTHETIC,
}


class CacheWriter:
    """Updates Valkey with the latest observation per segment from each batch."""

    def __init__(self, cache: TrafficCache) -> None:
        self._cache = cache

    async def update_from_batch(self, df: pl.DataFrame) -> int:
        """Cache the latest observation per segment. Returns count of updates."""
        if df.is_empty():
            return 0

        # Keep only the latest observation per segment
        latest = df.sort("timestamp_ms", descending=True).unique(subset=["segment_id"])
        count = 0

        for row in latest.iter_rows(named=True):
            obs = TrafficObservation(
                timestamp_ms=row["timestamp_ms"],
                lon=row["lon"],
                lat=row["lat"],
                segment_id=int(row["segment_id"]) if isinstance(row["segment_id"], str) else row["segment_id"],
                speed_kmh=row["speed_kmh"],
                congestion_idx=row["congestion_index"],
                source=_SOURCE_MAP.get(row["source"], FeedSource.FLEET_GPS),
                data_quality=_QUALITY_MAP.get(row.get("data_quality", 0), DataQuality.REAL),
                confidence=row.get("confidence", 1.0),
            )
            await self._cache.set_observation(obs)
            count += 1

        logger.debug("cache updated", segments=count)
        return count
