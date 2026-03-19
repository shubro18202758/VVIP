"""Tests for the ingestion pipeline: ArrowDecoder, DbWriter, CacheWriter."""

from __future__ import annotations

import io
import time

import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.ipc
import pytest

from traffic_oracle.ingest.arrow_decoder import ArrowDecoder

from conftest import MockPool, MockValkey, make_observation


# ─── ArrowDecoder Tests ──────────────────────────────────────────────────────


def _make_ipc_bytes(n: int = 5) -> bytes:
    """Build valid Arrow IPC streaming bytes for testing."""
    schema = pa.schema(
        [
            pa.field("timestamp_ms", pa.int64()),
            pa.field("lon", pa.float64()),
            pa.field("lat", pa.float64()),
            pa.field("segment_id", pa.utf8()),
            pa.field("speed_kmh", pa.float32()),
            pa.field("congestion_index", pa.float32()),
            pa.field("source", pa.uint8()),
            pa.field("data_quality", pa.uint8()),
            pa.field("confidence", pa.float32()),
        ]
    )
    base_ts = int(time.time() * 1000)
    batch = pa.record_batch(
        [
            pa.array([base_ts + i * 1000 for i in range(n)], type=pa.int64()),
            pa.array([77.2 + i * 0.001 for i in range(n)], type=pa.float64()),
            pa.array([28.6 + i * 0.001 for i in range(n)], type=pa.float64()),
            pa.array([str(100 + i % 3) for i in range(n)], type=pa.utf8()),
            pa.array([40.0 + i * 5.0 for i in range(n)], type=pa.float32()),
            pa.array([0.3 + i * 0.05 for i in range(n)], type=pa.float32()),
            pa.array([i % 4 for i in range(n)], type=pa.uint8()),
            pa.array([0] * n, type=pa.uint8()),
            pa.array([1.0] * n, type=pa.float32()),
        ],
        schema=schema,
    )

    buf = io.BytesIO()
    writer = pa.ipc.new_stream(buf, schema)
    writer.write_batch(batch)
    writer.close()
    return buf.getvalue()


class TestArrowDecoder:
    def test_decode_returns_polars_df(self):
        ipc_bytes = _make_ipc_bytes(5)
        df = ArrowDecoder.decode(ipc_bytes)
        assert isinstance(df, pl.DataFrame)
        assert len(df) == 5

    def test_decode_preserves_columns(self):
        ipc_bytes = _make_ipc_bytes(3)
        df = ArrowDecoder.decode(ipc_bytes)
        expected_cols = {
            "timestamp_ms",
            "lon",
            "lat",
            "segment_id",
            "speed_kmh",
            "congestion_index",
            "source",
            "data_quality",
            "confidence",
        }
        assert set(df.columns) == expected_cols

    def test_decode_preserves_values(self):
        ipc_bytes = _make_ipc_bytes(1)
        df = ArrowDecoder.decode(ipc_bytes)
        assert df["confidence"][0] == 1.0
        assert df["data_quality"][0] == 0
        assert df["source"][0] < 4

    def test_decode_multiple_rows(self):
        ipc_bytes = _make_ipc_bytes(100)
        df = ArrowDecoder.decode(ipc_bytes)
        assert len(df) == 100

    def test_roundtrip_speed_values(self):
        """Verify speed values survive Arrow IPC encode → decode."""
        schema = pa.schema(
            [
                pa.field("timestamp_ms", pa.int64()),
                pa.field("lon", pa.float64()),
                pa.field("lat", pa.float64()),
                pa.field("segment_id", pa.utf8()),
                pa.field("speed_kmh", pa.float32()),
                pa.field("congestion_index", pa.float32()),
                pa.field("source", pa.uint8()),
                pa.field("data_quality", pa.uint8()),
                pa.field("confidence", pa.float32()),
            ]
        )
        speeds = [45.5, 60.0, 88.3]
        batch = pa.record_batch(
            [
                pa.array([1000, 2000, 3000], type=pa.int64()),
                pa.array([77.0, 77.1, 77.2], type=pa.float64()),
                pa.array([28.0, 28.1, 28.2], type=pa.float64()),
                pa.array(["100", "101", "102"], type=pa.utf8()),
                pa.array(speeds, type=pa.float32()),
                pa.array([0.3, 0.5, 0.7], type=pa.float32()),
                pa.array([0, 1, 2], type=pa.uint8()),
                pa.array([0, 0, 0], type=pa.uint8()),
                pa.array([1.0, 0.9, 0.8], type=pa.float32()),
            ],
            schema=schema,
        )

        buf = io.BytesIO()
        writer = pa.ipc.new_stream(buf, schema)
        writer.write_batch(batch)
        writer.close()

        df = ArrowDecoder.decode(buf.getvalue())
        decoded_speeds = df["speed_kmh"].to_list()
        for expected, actual in zip(speeds, decoded_speeds):
            assert abs(expected - actual) < 0.01


# ─── DbWriter Tests ──────────────────────────────────────────────────────────


class TestDbWriter:
    async def test_write_batch_returns_count(self, traffic_df: pl.DataFrame):
        from traffic_oracle.ingest.db_writer import DbWriter

        pool = MockPool()
        writer = DbWriter(pool)

        count = await writer.write_batch(traffic_df)
        assert count == len(traffic_df)

    async def test_write_empty_batch(self):
        from traffic_oracle.ingest.db_writer import DbWriter

        pool = MockPool()
        writer = DbWriter(pool)

        empty = pl.DataFrame(
            {
                "timestamp_ms": pl.Series([], dtype=pl.Int64),
                "lon": pl.Series([], dtype=pl.Float64),
                "lat": pl.Series([], dtype=pl.Float64),
                "segment_id": pl.Series([], dtype=pl.Utf8),
                "speed_kmh": pl.Series([], dtype=pl.Float64),
                "congestion_index": pl.Series([], dtype=pl.Float64),
                "source": pl.Series([], dtype=pl.Int64),
                "data_quality": pl.Series([], dtype=pl.Int64),
                "confidence": pl.Series([], dtype=pl.Float64),
            }
        )
        count = await writer.write_batch(empty)
        assert count == 0

    async def test_write_batch_executes_insert_query(self, traffic_df: pl.DataFrame):
        from traffic_oracle.ingest.db_writer import DbWriter

        pool = MockPool()
        writer = DbWriter(pool)

        await writer.write_batch(traffic_df)

        assert len(pool.connection.executed_queries) == 1
        query, _ = pool.connection.executed_queries[0]
        assert "INSERT INTO traffic.observations" in query


# ─── CacheWriter Tests ────────────────────────────────────────────────────────


class TestCacheWriter:
    async def test_update_from_batch_caches_latest(self, mock_valkey: MockValkey):
        from traffic_oracle.data.cache import TrafficCache
        from traffic_oracle.ingest.cache_writer import CacheWriter

        # Monkeypatch the cache to use our mock valkey
        cache = TrafficCache.__new__(TrafficCache)
        cache._client = mock_valkey

        writer = CacheWriter(cache)

        base_ts = int(time.time() * 1000)
        df = pl.DataFrame(
            {
                "timestamp_ms": [base_ts, base_ts + 1000],
                "lon": [77.2, 77.3],
                "lat": [28.6, 28.7],
                "segment_id": ["100", "101"],
                "speed_kmh": [50.0, 60.0],
                "congestion_index": [0.3, 0.5],
                "source": [2, 0],
                "data_quality": [0, 0],
                "confidence": [1.0, 0.9],
            }
        )

        count = await writer.update_from_batch(df)
        assert count == 2

        # Verify data was cached
        raw_100 = await mock_valkey.get("traffic:latest:100")
        assert raw_100 is not None

    async def test_update_empty_batch(self, mock_valkey: MockValkey):
        from traffic_oracle.data.cache import TrafficCache
        from traffic_oracle.ingest.cache_writer import CacheWriter

        cache = TrafficCache.__new__(TrafficCache)
        cache._client = mock_valkey

        writer = CacheWriter(cache)

        empty = pl.DataFrame(
            {
                "timestamp_ms": pl.Series([], dtype=pl.Int64),
                "lon": pl.Series([], dtype=pl.Float64),
                "lat": pl.Series([], dtype=pl.Float64),
                "segment_id": pl.Series([], dtype=pl.Utf8),
                "speed_kmh": pl.Series([], dtype=pl.Float64),
                "congestion_index": pl.Series([], dtype=pl.Float64),
                "source": pl.Series([], dtype=pl.Int64),
                "data_quality": pl.Series([], dtype=pl.Int64),
                "confidence": pl.Series([], dtype=pl.Float64),
            }
        )
        count = await writer.update_from_batch(empty)
        assert count == 0
