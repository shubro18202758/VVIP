"""Constructs feature vectors for ML anomaly detection."""

from __future__ import annotations

import math

import numpy as np
import polars as pl


class FeatureBuilder:
    """Builds 8-dimensional feature vectors for Isolation Forest.

    Features:
        0: speed_normalized — speed / segment speed limit
        1: congestion_idx — raw congestion index [0..1]
        2: hour_sin — sin(2π * hour / 24)
        3: hour_cos — cos(2π * hour / 24)
        4: dow_sin — sin(2π * day_of_week / 7)
        5: dow_cos — cos(2π * day_of_week / 7)
        6: speed_delta — change from previous observation for this segment
        7: neighbor_speed_ratio — speed / avg neighbor speed (1.0 if unavailable)
    """

    @staticmethod
    def build_features(
        df: pl.DataFrame,
        speed_limits: dict[int, float] | None = None,
        neighbor_speeds: dict[int, float] | None = None,
    ) -> np.ndarray:
        """Build feature matrix from a Polars DataFrame of observations.

        Args:
            df: observations with columns: timestamp_ms, segment_id, speed_kmh,
                congestion_index
            speed_limits: segment_id → speed limit mapping (default 60.0)
            neighbor_speeds: segment_id → avg neighbor speed (default = own speed)

        Returns:
            (N, 8) float32 numpy array
        """
        speed_limits = speed_limits or {}
        neighbor_speeds = neighbor_speeds or {}
        default_limit = 60.0

        n = len(df)
        features = np.zeros((n, 8), dtype=np.float32)

        timestamps = df["timestamp_ms"].to_numpy()
        segment_ids = df["segment_id"].to_numpy()
        speeds = df["speed_kmh"].to_numpy()
        congestions = df["congestion_index"].to_numpy()

        prev_speed: dict[int, float] = {}

        for i in range(n):
            ts_ms = int(timestamps[i])
            seg_id = int(segment_ids[i])
            speed = float(speeds[i])
            congestion = float(congestions[i])

            limit = speed_limits.get(seg_id, default_limit)
            features[i, 0] = speed / limit if limit > 0 else 0.0
            features[i, 1] = congestion

            # Cyclic hour encoding
            hour = (ts_ms // 3_600_000) % 24
            features[i, 2] = math.sin(2 * math.pi * hour / 24)
            features[i, 3] = math.cos(2 * math.pi * hour / 24)

            # Cyclic day-of-week encoding (0=Monday)
            day = (ts_ms // 86_400_000) % 7
            features[i, 4] = math.sin(2 * math.pi * day / 7)
            features[i, 5] = math.cos(2 * math.pi * day / 7)

            # Speed delta
            if seg_id in prev_speed:
                features[i, 6] = speed - prev_speed[seg_id]
            prev_speed[seg_id] = speed

            # Neighbor speed ratio
            nbr = neighbor_speeds.get(seg_id, speed)
            features[i, 7] = speed / nbr if nbr > 0 else 1.0

        return features
