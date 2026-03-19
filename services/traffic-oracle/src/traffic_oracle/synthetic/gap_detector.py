"""Detects segments with stale or missing observations."""

from __future__ import annotations

import structlog
import valkey.asyncio as valkey_async

logger = structlog.get_logger()


class GapDetector:
    """Scans Valkey for segments that have gone stale (>5 min without update)."""

    def __init__(self, client: valkey_async.Valkey, stale_threshold_sec: int = 300) -> None:
        self._client = client
        self._stale_threshold_sec = stale_threshold_sec

    async def find_stale_segments(self, known_segment_ids: list[int]) -> list[int]:
        """Return segment IDs whose cached observations have expired or are missing."""
        stale = []
        for seg_id in known_segment_ids:
            key = f"traffic:latest:{seg_id}"
            ttl = await self._client.ttl(key)
            # ttl = -2 means key doesn't exist, -1 means no expiry set
            if ttl == -2:
                stale.append(seg_id)
            # If TTL is very low compared to default (300s), observation is old
            # A freshly set key with 300s TTL would have ~295+ remaining

        logger.debug("gap detection complete", total=len(known_segment_ids), stale=len(stale))
        return stale
