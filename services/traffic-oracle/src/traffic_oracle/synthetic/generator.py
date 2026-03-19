"""Synthetic data generator — orchestrates the gap-filling cascade."""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import structlog
import valkey.asyncio as valkey_async

from traffic_oracle.synthetic.confidence_scorer import score_confidence
from traffic_oracle.synthetic.gap_detector import GapDetector
from traffic_oracle.synthetic.pattern_matcher import PatternMatcher
from traffic_oracle.synthetic.spatial_interpolator import SpatialInterpolator

logger = structlog.get_logger()


class SyntheticDataGenerator:
    """Four-method cascade for filling traffic data gaps.

    Priority order:
        1. Historical match (same hour/dow aggregate)
        2. Spatial interpolation (IDW from neighbors)
        3. Temporal pattern (TODO: ARIMA/ETS in future phase)
        4. Default fallback (road-class-based defaults)
    """

    # Road-class default speeds (km/h) for fallback
    _DEFAULTS: dict[str, float] = {
        "motorway": 80.0,
        "trunk": 60.0,
        "primary": 50.0,
        "secondary": 40.0,
        "tertiary": 35.0,
        "residential": 25.0,
    }

    def __init__(
        self,
        pool: asyncpg.Pool,
        valkey_client: valkey_async.Valkey,
    ) -> None:
        self._pool = pool
        self._gap_detector = GapDetector(valkey_client)
        self._pattern_matcher = PatternMatcher(pool)
        self._interpolator = SpatialInterpolator(valkey_client)

    async def fill_gaps(
        self,
        segment_ids: list[int],
        neighbor_map: dict[int, list[int]],
    ) -> list[dict]:
        """Run the cascade for all stale segments. Returns generated observations."""
        stale = await self._gap_detector.find_stale_segments(segment_ids)
        if not stale:
            return []

        now = datetime.now(timezone.utc)
        results = []

        for seg_id in stale:
            obs = await self._try_cascade(seg_id, now, neighbor_map.get(seg_id, []))
            if obs:
                await self._persist(obs)
                results.append(obs)

        logger.info("synthetic gap fill complete", filled=len(results), stale=len(stale))
        return results

    async def _try_cascade(
        self, segment_id: int, now: datetime, neighbors: list[int]
    ) -> dict | None:
        # Method 1: Historical match
        hist = await self._pattern_matcher.match_historical(segment_id, now)
        if hist:
            return self._make_observation(
                segment_id, now, hist["speed_kmh"], hist["congestion_idx"],
                "historical_match", [segment_id],
            )

        # Method 2: Spatial interpolation
        spatial = await self._interpolator.interpolate(segment_id, neighbors)
        if spatial:
            return self._make_observation(
                segment_id, now, spatial["speed_kmh"], spatial["congestion_idx"],
                "spatial_interpolation", neighbors,
            )

        # Method 3: Temporal pattern (placeholder — ARIMA in future phase)
        # Falls through to default

        # Method 4: Default fallback
        speed = self._DEFAULTS.get("secondary", 40.0)
        congestion = 0.5
        return self._make_observation(
            segment_id, now, speed, congestion,
            "default_fallback", [],
        )

    @staticmethod
    def _make_observation(
        segment_id: int,
        ts: datetime,
        speed: float,
        congestion: float,
        method: str,
        source_segments: list[int],
    ) -> dict:
        return {
            "segment_id": segment_id,
            "timestamp_utc": ts,
            "speed_kmh": speed,
            "congestion_idx": congestion,
            "generation_method": method,
            "confidence": score_confidence(method),
            "source_segments": source_segments,
        }

    async def _persist(self, obs: dict) -> None:
        """Write synthetic observation to traffic.synthetic_observations."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO traffic.synthetic_observations
                    (segment_id, timestamp_utc, speed_kmh, congestion_idx,
                     generation_method, confidence, source_segments)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                obs["segment_id"],
                obs["timestamp_utc"],
                obs["speed_kmh"],
                obs["congestion_idx"],
                obs["generation_method"],
                obs["confidence"],
                obs["source_segments"],
            )
