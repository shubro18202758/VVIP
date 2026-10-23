"""Updates Valkey cache with latest per-segment observations.

The hot cache ``traffic:latest:{segment_id}`` (TTL 300 s) is what
``POST /api/v1/traffic/live`` serves and what the flow forecaster reads as
its live input.  One pipelined round trip per decoded batch; only the
newest row per segment is written, and rows already older than the TTL are
skipped (they would be stale on arrival and could overwrite a fresher one).

LIVE vs EMULATED (never last-writer-wins across planes): rows whose
canonical ``origin`` is 'emulated' — the event-emulator's showcase batches
on ``corridor.traffic.emulated`` — go to their OWN key space,
``traffic:emulated:latest:{segment_id}`` (same TTL, the same JSON plus
``"origin": "emulated"``), and never overwrite the live plane's row.  So
emulated traffic cannot drive a REAL corridor event's AMA band, the
forecaster or the live map: only the situation engine's EMULATED events
read it (adaptive/engine_loop.py), and /traffic/live serves it labelled,
beside — never as — the live observations.
"""

from __future__ import annotations

import json
import time

import polars as pl
import structlog

from traffic_oracle.data.cache import TrafficCache
from traffic_oracle.data.models import DataQuality, FeedSource, TrafficObservation
from traffic_oracle.ingest.normalize import ORIGIN_EMULATED, ensure_canonical

logger = structlog.get_logger()

#: traffic:latest:{sid} TTL — a segment with no fresh row for 5 minutes
#: drops out of the live picture instead of showing a frozen value.
LATEST_TTL_SEC = 300

#: The live plane's latest-row key space (data/cache.py TrafficCache).
LATEST_PREFIX = "traffic:latest:"

#: The event-emulator's own latest-row key space (outside ``traffic:latest:*``
#: so no live-plane scan — /health cache_keys, the gap detector — counts it).
EMULATED_LATEST_PREFIX = "traffic:emulated:latest:"


def live_latest_key(segment_id: int) -> str:
    """``traffic:latest:{sid}`` — the live plane's latest row."""
    return f"{LATEST_PREFIX}{segment_id}"


def emulated_latest_key(segment_id: int) -> str:
    """``traffic:emulated:latest:{sid}`` — the emulator's latest row."""
    return f"{EMULATED_LATEST_PREFIX}{segment_id}"


def emulated_observation_json(obs: TrafficObservation) -> str:
    """The cached JSON of an EMULATED row: the live row's exact shape (so
    every TrafficObservation reader parses it) plus ``origin``."""
    return json.dumps(
        {**obs.model_dump(mode="json"), "origin": ORIGIN_EMULATED}, separators=(",", ":")
    )


class CacheWriter:
    """Updates Valkey with the latest observation per segment from each batch."""

    def __init__(self, cache: TrafficCache, ttl_sec: int = LATEST_TTL_SEC) -> None:
        self._cache = cache
        self._ttl_sec = ttl_sec
        #: rows written per plane since start (surfaced by tests / tooling)
        self.written: dict[str, int] = {"live": 0, "emulated": 0}

    async def update_from_batch(self, df: pl.DataFrame, now_ms: int | None = None) -> int:
        """Cache the latest observation per segment. Returns count of updates.

        Accepts either a canonical frame (ingest/normalize.py) or a raw
        decoded wire batch (normalised here first).  Live rows land on
        ``traffic:latest:{sid}``, emulated rows on
        ``traffic:emulated:latest:{sid}`` — each plane keeps its own
        latest row per segment.
        """
        if df.is_empty():
            return 0
        df = ensure_canonical(df)
        if df.is_empty():
            return 0

        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        fresh = df.filter(pl.col("timestamp_ms") >= now_ms - self._ttl_sec * 1000)
        if fresh.is_empty():
            return 0

        count = 0
        for origin, plane in (
            ("live", fresh.filter(pl.col("origin") != ORIGIN_EMULATED)),
            ("emulated", fresh.filter(pl.col("origin") == ORIGIN_EMULATED)),
        ):
            if plane.is_empty():
                continue
            # Keep only the latest observation per segment (per plane)
            latest = (
                plane.sort("timestamp_ms").group_by("segment_id", maintain_order=True).last()
            )
            observations = [
                TrafficObservation(
                    timestamp_ms=row["timestamp_ms"],
                    lon=row["lon"],
                    lat=row["lat"],
                    segment_id=row["segment_id"],
                    speed_kmh=row["speed_kmh"],
                    congestion_idx=row["congestion_idx"],
                    source=FeedSource(row["source"]),
                    data_quality=DataQuality(row["data_quality"]),
                    confidence=row["confidence"],
                )
                for row in latest.iter_rows(named=True)
            ]
            if origin == "live":
                n = await self._cache.set_observations(observations, ttl_sec=self._ttl_sec)
            else:
                n = await self._set_emulated(observations)
            self.written[origin] += n
            count += n

        logger.debug("cache updated", segments=count)
        return count

    async def _set_emulated(self, observations: list[TrafficObservation]) -> int:
        """Emulated rows → their own key space, ONE pipelined round trip
        (the same non-transactional pipeline TrafficCache uses)."""
        client = self._cache._client  # noqa: SLF001 — the cache's own client
        async with client.pipeline(transaction=False) as pipe:
            for obs in observations:
                pipe.set(
                    emulated_latest_key(obs.segment_id),
                    emulated_observation_json(obs),
                    ex=self._ttl_sec,
                )
            await pipe.execute()
        return len(observations)


__all__ = [
    "EMULATED_LATEST_PREFIX",
    "LATEST_PREFIX",
    "LATEST_TTL_SEC",
    "CacheWriter",
    "emulated_latest_key",
    "emulated_observation_json",
    "live_latest_key",
]
