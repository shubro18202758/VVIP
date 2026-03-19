"""Valkey (Redis-compatible) cache for real-time traffic state."""

from __future__ import annotations

import json

import structlog
import valkey.asyncio as valkey_async

from traffic_oracle.data.models import TrafficObservation

logger = structlog.get_logger()


class TrafficCache:
    """Thin async wrapper around Valkey for per-segment traffic state."""

    def __init__(self, url: str = "redis://localhost:6379") -> None:
        self._client = valkey_async.from_url(url, decode_responses=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def get_latest(self, segment_id: int) -> TrafficObservation | None:
        """Retrieve the latest observation for a segment."""
        raw = await self._client.get(f"traffic:latest:{segment_id}")
        if raw is None:
            return None
        return TrafficObservation.model_validate_json(raw)

    async def set_observation(self, obs: TrafficObservation, ttl_sec: int = 300) -> None:
        """Cache an observation with TTL."""
        key = f"traffic:latest:{obs.segment_id}"
        await self._client.set(key, obs.model_dump_json(), ex=ttl_sec)

    async def get_corridor_summary(self, corridor_id: str) -> dict | None:
        """Retrieve a cached corridor summary."""
        raw = await self._client.get(f"corridor:summary:{corridor_id}")
        if raw is None:
            return None
        return json.loads(raw)

    async def set_corridor_summary(
        self, corridor_id: str, summary: dict, ttl_sec: int = 60
    ) -> None:
        """Cache a corridor summary."""
        key = f"corridor:summary:{corridor_id}"
        await self._client.set(key, json.dumps(summary), ex=ttl_sec)
