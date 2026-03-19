"""Shared test fixtures for traffic-oracle test suite."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import polars as pl
import pytest

from traffic_oracle.data.models import (
    DataQuality,
    FeedSource,
    TrafficObservation,
)


# ─── Time helpers ─────────────────────────────────────────────────────────────


def now_ms() -> int:
    """Current UTC time in epoch milliseconds."""
    return int(time.time() * 1000)


# ─── Sample data factories ────────────────────────────────────────────────────


def make_observation(
    segment_id: int = 100,
    speed_kmh: float = 50.0,
    congestion_idx: float = 0.3,
    source: FeedSource = FeedSource.FLEET_GPS,
    data_quality: DataQuality = DataQuality.REAL,
    confidence: float = 1.0,
    timestamp_ms: int | None = None,
) -> TrafficObservation:
    return TrafficObservation(
        timestamp_ms=timestamp_ms or now_ms(),
        lon=77.2090,
        lat=28.6139,
        segment_id=segment_id,
        speed_kmh=speed_kmh,
        congestion_idx=congestion_idx,
        source=source,
        data_quality=data_quality,
        confidence=confidence,
    )


@pytest.fixture
def sample_observation() -> TrafficObservation:
    return make_observation()


@pytest.fixture
def sample_observations() -> list[TrafficObservation]:
    """10 observations across 3 segments with varying speeds."""
    obs = []
    for i in range(10):
        obs.append(
            make_observation(
                segment_id=100 + (i % 3),
                speed_kmh=30.0 + i * 5.0,
                congestion_idx=0.2 + (i % 5) * 0.1,
                timestamp_ms=now_ms() + i * 1000,
            )
        )
    return obs


# ─── Polars DataFrame fixtures ───────────────────────────────────────────────


@pytest.fixture
def traffic_df() -> pl.DataFrame:
    """Sample traffic DataFrame matching the Arrow IPC schema."""
    n = 20
    base_ts = now_ms()
    return pl.DataFrame(
        {
            "timestamp_ms": [base_ts + i * 5000 for i in range(n)],
            "segment_id": [100 + (i % 5) for i in range(n)],
            "speed_kmh": [40.0 + (i % 10) * 3.0 for i in range(n)],
            "congestion_index": [0.2 + (i % 8) * 0.08 for i in range(n)],
            "lon": [77.2 + i * 0.001 for i in range(n)],
            "lat": [28.6 + i * 0.001 for i in range(n)],
            "source": [i % 4 for i in range(n)],
            "data_quality": [0] * n,
            "confidence": [1.0] * n,
        }
    )


@pytest.fixture
def normal_traffic_features() -> np.ndarray:
    """(100, 8) feature matrix of normal traffic patterns."""
    rng = np.random.default_rng(42)
    features = np.zeros((100, 8), dtype=np.float32)
    features[:, 0] = rng.normal(0.8, 0.1, 100)  # speed_normalized
    features[:, 1] = rng.normal(0.3, 0.05, 100)  # congestion_idx
    features[:, 2] = rng.uniform(-1, 1, 100)  # hour_sin
    features[:, 3] = rng.uniform(-1, 1, 100)  # hour_cos
    features[:, 4] = rng.uniform(-1, 1, 100)  # dow_sin
    features[:, 5] = rng.uniform(-1, 1, 100)  # dow_cos
    features[:, 6] = rng.normal(0, 2, 100)  # speed_delta
    features[:, 7] = rng.normal(1.0, 0.1, 100)  # neighbor_speed_ratio
    return features


# ─── Mock asyncpg pool ────────────────────────────────────────────────────────


class MockConnection:
    """Mock asyncpg connection for testing database queries."""

    def __init__(self, fetch_result: list | None = None, fetchrow_result: dict | None = None):
        self._fetch_result = fetch_result or []
        self._fetchrow_result = fetchrow_result
        self.executed_queries: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args) -> list:
        self.executed_queries.append((query, args))
        return self._fetch_result

    async def fetchrow(self, query: str, *args) -> dict | None:
        self.executed_queries.append((query, args))
        return self._fetchrow_result

    async def fetchval(self, query: str, *args):
        self.executed_queries.append((query, args))
        return 1

    async def execute(self, query: str, *args) -> None:
        self.executed_queries.append((query, args))

    async def executemany(self, query: str, args: list) -> None:
        self.executed_queries.append((query, tuple(args)))


class MockPool:
    """Mock asyncpg Pool that yields MockConnection via acquire()."""

    def __init__(
        self,
        fetch_result: list | None = None,
        fetchrow_result: dict | None = None,
    ):
        self.connection = MockConnection(fetch_result, fetchrow_result)

    def acquire(self):
        return MockPoolAcquire(self.connection)


class MockPoolAcquire:
    """Context manager that yields MockConnection."""

    def __init__(self, conn: MockConnection):
        self._conn = conn

    async def __aenter__(self) -> MockConnection:
        return self._conn

    async def __aexit__(self, *args) -> None:
        pass


@pytest.fixture
def mock_pool() -> MockPool:
    return MockPool()


# ─── Mock Valkey client ───────────────────────────────────────────────────────


class MockValkey:
    """In-memory mock of valkey.asyncio.Valkey for testing cache operations."""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._ttls: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = value
        if ex is not None:
            self._ttls[key] = ex

    async def ttl(self, key: str) -> int:
        if key not in self._store:
            return -2  # key does not exist
        if key not in self._ttls:
            return -1  # no expiry
        return self._ttls[key]

    async def aclose(self) -> None:
        self._store.clear()
        self._ttls.clear()

    def seed(self, key: str, value: str, ttl: int | None = None) -> None:
        """Helper to pre-populate the mock store."""
        self._store[key] = value
        if ttl is not None:
            self._ttls[key] = ttl


@pytest.fixture
def mock_valkey() -> MockValkey:
    return MockValkey()
