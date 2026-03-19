"""Spatial interpolation using inverse-distance weighting (IDW)."""

from __future__ import annotations

import structlog
import valkey.asyncio as valkey_async

from traffic_oracle.data.models import TrafficObservation

logger = structlog.get_logger()


class SpatialInterpolator:
    """Estimates traffic for a segment using IDW from neighbor segments."""

    def __init__(self, client: valkey_async.Valkey) -> None:
        self._client = client

    async def interpolate(
        self, segment_id: int, neighbor_ids: list[int]
    ) -> dict | None:
        """IDW-interpolate speed and congestion from cached neighbor observations.

        Returns dict with speed_kmh and congestion_idx, or None if no neighbors have data.
        """
        speeds = []
        congestions = []
        weights = []

        for i, nbr_id in enumerate(neighbor_ids):
            raw = await self._client.get(f"traffic:latest:{nbr_id}")
            if raw is None:
                continue
            obs = TrafficObservation.model_validate_json(raw)
            # Weight: inverse of hop distance (1-indexed)
            w = 1.0 / (i + 1)
            speeds.append(obs.speed_kmh * w)
            congestions.append(obs.congestion_idx * w)
            weights.append(w)

        if not weights:
            return None

        total_w = sum(weights)
        return {
            "speed_kmh": sum(speeds) / total_w,
            "congestion_idx": sum(congestions) / total_w,
        }
