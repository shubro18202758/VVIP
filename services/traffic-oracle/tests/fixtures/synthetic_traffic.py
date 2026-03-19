"""Synthetic traffic data generators for test scenarios."""

from __future__ import annotations

import time

import numpy as np
import polars as pl


def generate_normal_traffic(
    n_segments: int = 10,
    n_steps: int = 20,
    base_speed: float = 50.0,
    speed_std: float = 5.0,
) -> pl.DataFrame:
    """Generate normal (non-anomalous) traffic data.

    Returns a DataFrame with columns matching the Arrow IPC schema.
    """
    rng = np.random.default_rng(42)
    base_ts = int(time.time() * 1000)
    total = n_segments * n_steps

    rows = {
        "timestamp_ms": [],
        "segment_id": [],
        "speed_kmh": [],
        "congestion_index": [],
        "lon": [],
        "lat": [],
        "source": [],
    }

    for seg in range(n_segments):
        for step in range(n_steps):
            rows["timestamp_ms"].append(base_ts + step * 60_000)
            rows["segment_id"].append(100 + seg)
            rows["speed_kmh"].append(float(rng.normal(base_speed, speed_std)))
            rows["congestion_index"].append(float(np.clip(rng.normal(0.3, 0.1), 0, 1)))
            rows["lon"].append(77.2 + seg * 0.01)
            rows["lat"].append(28.6 + seg * 0.01)
            rows["source"].append(seg % 4)

    return pl.DataFrame(rows)


def generate_anomalous_traffic(n: int = 10) -> pl.DataFrame:
    """Generate traffic data with injected anomalies.

    Anomaly types: extreme speed, negative speed, extreme congestion.
    """
    base_ts = int(time.time() * 1000)
    speeds = [200.0, -10.0, 0.0, 999.0, 50.0, 50.0, 50.0, 300.0, -5.0, 150.0][:n]
    congestions = [0.3, 0.5, 1.0, 0.0, 0.99, 0.01, 0.5, 0.95, 0.3, 0.8][:n]

    return pl.DataFrame(
        {
            "timestamp_ms": [base_ts + i * 1000 for i in range(n)],
            "segment_id": [100 + (i % 3) for i in range(n)],
            "speed_kmh": speeds,
            "congestion_index": congestions,
            "lon": [77.2] * n,
            "lat": [28.6] * n,
            "source": [0] * n,
        }
    )


def generate_gap_scenario(
    n_segments: int = 5,
    gap_segments: list[int] | None = None,
) -> tuple[list[int], list[int]]:
    """Generate a gap scenario: some segments have data, others are stale.

    Returns (all_segment_ids, stale_segment_ids).
    """
    all_ids = list(range(100, 100 + n_segments))
    gap_segments = gap_segments or all_ids[3:]
    return all_ids, gap_segments
