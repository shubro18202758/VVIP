"""Tests for the ingestion pipeline: ArrowDecoder, normalisation, DbWriter,
CacheWriter, and the forecaster's per-segment input gate.

Both producer wire schemas are exercised byte-for-byte: signal-ingress
(pipeline/batch.rs — utf8 segment ids, float32 values, uint8 ordinals) and
the event-emulator showcase (nats_seam.py — int64 ids, float64 values, int8
ordinals).

The DB tier (TestDbWriterLiveSchema) runs only when VVIP_DATABASE_URL points
at a reachable PostGIS: it PREPARES the real INSERT against the real schema
inside a transaction that is always rolled back, and reads the CHECK
vocabularies from the catalog (read-only).  EXECUTING the insert (still
rolled back) additionally requires VEER_TEST_DB_WRITES=1 — scratch
databases only.
"""

from __future__ import annotations

import io
import os
import re
import time
from types import SimpleNamespace

import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.ipc
import pytest

from traffic_oracle.data.models import (
    DATA_QUALITY_ORDINALS,
    FEED_SOURCE_ORDINALS,
    DataQuality,
    FeedSource,
)
from traffic_oracle.ingest.arrow_decoder import ArrowDecoder
from traffic_oracle.ingest.normalize import CANONICAL_SCHEMA, normalize_batch

from conftest import MockValkey, make_observation


# ─── Wire builders (exact producer schemas) ──────────────────────────────────

LIVE_SCHEMA = pa.schema(  # signal-ingress pipeline/batch.rs traffic_schema()
    [
        pa.field("timestamp_ms", pa.int64(), nullable=False),
        pa.field("lon", pa.float64(), nullable=False),
        pa.field("lat", pa.float64(), nullable=False),
        pa.field("segment_id", pa.utf8(), nullable=False),
        pa.field("speed_kmh", pa.float32(), nullable=False),
        pa.field("congestion_index", pa.float32(), nullable=False),
        pa.field("source", pa.uint8(), nullable=False),
        pa.field("data_quality", pa.uint8(), nullable=False),
        pa.field("confidence", pa.float32(), nullable=False),
    ]
)

EMULATED_SCHEMA = pa.schema(  # event-emulator nats_seam.EMULATED_SCHEMA
    [
        ("timestamp_ms", pa.int64()),
        ("lon", pa.float64()),
        ("lat", pa.float64()),
        ("segment_id", pa.int64()),
        ("speed_kmh", pa.float64()),
        ("congestion_index", pa.float64()),
        ("source", pa.int8()),
        ("data_quality", pa.int8()),
        ("confidence", pa.float64()),
    ]
)


def _ipc(schema: pa.Schema, columns: dict) -> bytes:
    batch = pa.record_batch(
        [pa.array(columns[f.name], type=f.type) for f in schema], schema=schema
    )
    buf = io.BytesIO()
    writer = pa.ipc.new_stream(buf, schema)
    writer.write_batch(batch)
    writer.close()
    return buf.getvalue()


def live_ipc(rows: list[tuple]) -> bytes:
    """rows: (ts_ms, segment_id:str, speed, congestion, source_u8, quality_u8)."""
    return _ipc(
        LIVE_SCHEMA,
        {
            "timestamp_ms": [r[0] for r in rows],
            "lon": [72.58 for _ in rows],
            "lat": [23.02 for _ in rows],
            "segment_id": [r[1] for r in rows],
            "speed_kmh": [r[2] for r in rows],
            "congestion_index": [r[3] for r in rows],
            "source": [r[4] for r in rows],
            "data_quality": [r[5] for r in rows],
            "confidence": [0.9 for _ in rows],
        },
    )


def emulated_ipc(rows: list[tuple]) -> bytes:
    """rows: (ts_ms, segment_id:int, speed, congestion, quality_i8)."""
    return _ipc(
        EMULATED_SCHEMA,
        {
            "timestamp_ms": [r[0] for r in rows],
            "lon": [72.57 for _ in rows],
            "lat": [23.03 for _ in rows],
            "segment_id": [r[1] for r in rows],
            "speed_kmh": [r[2] for r in rows],
            "congestion_index": [r[3] for r in rows],
            "source": [2 for _ in rows],
            "data_quality": [r[4] for r in rows],
            "confidence": [0.9 for _ in rows],
        },
    )


def _make_ipc_bytes(n: int = 5) -> bytes:
    base_ts = int(time.time() * 1000)
    return live_ipc(
        [
            (base_ts + i * 1000, str(100 + i % 3), 40.0 + i * 5.0, 0.3 + i * 0.05, i % 4, 0)
            for i in range(n)
        ]
    )


def now_ms() -> int:
    return int(time.time() * 1000)


# ─── ArrowDecoder Tests ──────────────────────────────────────────────────────


class TestArrowDecoder:
    def test_decode_returns_polars_df(self):
        df = ArrowDecoder.decode(_make_ipc_bytes(5))
        assert isinstance(df, pl.DataFrame)
        assert len(df) == 5

    def test_decode_preserves_columns(self):
        df = ArrowDecoder.decode(_make_ipc_bytes(3))
        assert set(df.columns) == {
            "timestamp_ms", "lon", "lat", "segment_id", "speed_kmh",
            "congestion_index", "source", "data_quality", "confidence",
        }

    def test_decode_preserves_values(self):
        df = ArrowDecoder.decode(_make_ipc_bytes(1))
        assert df["confidence"][0] == pytest.approx(0.9)
        assert df["data_quality"][0] == 0
        assert df["source"][0] < 4

    def test_decode_multiple_rows(self):
        assert len(ArrowDecoder.decode(_make_ipc_bytes(100))) == 100

    def test_roundtrip_speed_values(self):
        speeds = [45.5, 60.0, 88.3]
        data = live_ipc([(1000 + i, str(100 + i), s, 0.3, 0, 0) for i, s in enumerate(speeds)])
        decoded = ArrowDecoder.decode(data)["speed_kmh"].to_list()
        for expected, actual in zip(speeds, decoded, strict=True):
            assert abs(expected - actual) < 0.01


# ─── Normalisation (both producer schemas) ───────────────────────────────────


class TestNormalize:
    def test_live_schema_utf8_ids_and_uint8_ordinals(self):
        ts = now_ms()
        df = ArrowDecoder.decode(
            live_ipc([(ts, "1001", 40.0, 0.3, 0, 0), (ts, " 1002", 30.0, 0.5, 2, 1)])
        )
        assert df.schema["segment_id"] == pl.String
        assert df.schema["source"] == pl.UInt8
        batch = normalize_batch(df)
        assert dict(batch.frame.schema) == CANONICAL_SCHEMA
        assert batch.frame["segment_id"].to_list() == [1001, 1002]
        assert batch.frame["source"].to_list() == ["government_traffic", "fleet_gps"]
        assert batch.frame["data_quality"].to_list() == ["real", "anomalous"]
        assert not batch.dropped

    def test_emulated_schema_int64_ids_int8_ordinals_forced_synthetic(self):
        ts = now_ms()
        df = ArrowDecoder.decode(
            emulated_ipc([(ts, 1001, 22.0, 0.9, 1), (ts, 1015, 45.0, 0.2, 0)])
        )
        assert df.schema["segment_id"] == pl.Int64
        assert df.schema["data_quality"] == pl.Int8
        batch = normalize_batch(df, force_quality=DataQuality.SYNTHETIC)
        assert batch.frame["segment_id"].to_list() == [1001, 1015]
        assert batch.frame["source"].to_list() == ["fleet_gps", "fleet_gps"]
        # Provenance wins over the emulator's own anomalous marker.
        assert batch.frame["data_quality"].to_list() == ["synthetic", "synthetic"]

    def test_ordinal_maps_mirror_the_rust_enums_and_db_vocabulary(self):
        # pipeline/batch.rs source match + DataQuality::as_u8
        assert {k: v.value for k, v in FEED_SOURCE_ORDINALS.items()} == {
            0: "government_traffic", 1: "mapping_api", 2: "fleet_gps", 3: "crowdsource",
        }
        assert {k: v.value for k, v in DATA_QUALITY_ORDINALS.items()} == {
            0: "real", 1: "anomalous", 2: "synthetic",
        }
        # observations.source CHECK (001) spells the government feed 'government'.
        assert {s.db_value for s in FeedSource} == {
            "government", "mapping_api", "fleet_gps", "crowdsource",
        }
        assert FeedSource.GOVERNMENT_TRAFFIC.db_value == "government"

    def test_unrepresentable_rows_are_dropped_and_counted(self):
        ts = now_ms()
        df = ArrowDecoder.decode(
            live_ipc(
                [
                    (ts, "1001", 40.0, 0.3, 0, 0),           # ok
                    (ts, "abc", 40.0, 0.3, 0, 0),            # invalid id
                    (ts, "1002", 40.0, 0.3, 7, 0),           # unknown source
                    (ts, "1003", 40.0, 0.3, 0, 9),           # unknown quality
                    (ts, "1004", float("nan"), 0.3, 0, 0),   # NaN speed
                    (ts, "1005", -3.0, 0.3, 0, 0),           # negative speed
                    (ts + 3_600_000, "1006", 40.0, 0.3, 0, 0),  # 1 h in the future
                    (ts - 86_400_000 * 30, "1007", 40.0, 0.3, 0, 0),  # 30 days old
                ]
            )
        )
        batch = normalize_batch(
            df, now_ms=ts, max_future_ms=600_000, max_age_ms=14 * 86_400_000
        )
        assert batch.frame["segment_id"].to_list() == [1001]
        assert batch.dropped == {
            "invalid_segment_id": 1,
            "unknown_source": 1,
            "unknown_quality": 1,
            "invalid_speed": 2,
            "future_timestamp": 1,
            "stale_timestamp": 1,
        }
        assert batch.received_rows == 8

    def test_impossible_speeds_are_dropped_not_stored(self):
        # float64 emulator speeds beyond float32 cannot be stored in the REAL
        # column at all; anything above MAX_SPEED_KMH is no road reading.
        from traffic_oracle.ingest.normalize import MAX_SPEED_KMH

        ts = now_ms()
        df = ArrowDecoder.decode(
            emulated_ipc(
                [
                    (ts, 1001, 190.0, 0.3, 2, 0),    # implausible but kept (Tier-1 flags it)
                    (ts, 1002, 301.0, 0.3, 2, 0),    # impossible
                    (ts, 1003, 1e39, 0.3, 2, 0),     # beyond REAL range
                ]
            )
        )
        batch = normalize_batch(df, force_quality=DataQuality.SYNTHETIC, now_ms=ts)
        assert batch.frame["segment_id"].to_list() == [1001]
        assert batch.dropped == {"invalid_speed": 2}
        assert MAX_SPEED_KMH == 300.0

    def test_congestion_and_confidence_clamped_into_check_range(self):
        df = pl.DataFrame(
            {
                "timestamp_ms": [now_ms(), now_ms()],
                "lon": [72.5, 72.5],
                "lat": [23.0, 23.0],
                "segment_id": [1001, 1002],
                "speed_kmh": [30.0, 30.0],
                "congestion_index": [1.4, -0.2],
                "source": [1, 3],
                "data_quality": [0, 0],
                "confidence": [1.7, float("nan")],
            }
        )
        frame = normalize_batch(df).frame
        assert frame["congestion_idx"].to_list() == [1.0, 0.0]
        assert frame["confidence"].to_list() == [1.0, 1.0]

    def test_missing_columns_drop_the_whole_batch_loudly(self):
        df = pl.DataFrame({"segment_id": ["1001"], "speed_kmh": [30.0]})
        batch = normalize_batch(df)
        assert batch.rows == 0
        assert batch.dropped == {"missing_columns": 1}


# ─── DbWriter (fake pool that behaves like the real statement) ───────────────


class RecordingConn:
    """Executes nothing; answers the INSERT command tag the real statement
    would (rows on segments in ``known`` survive the EXISTS filter)."""

    def __init__(self, known: set[int], fail: bool = False) -> None:
        self.known = known
        self.fail = fail
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, query: str, *args):
        self.calls.append((query, args))
        if self.fail:
            raise ConnectionError("db down")
        return f"INSERT 0 {sum(1 for sid in args[0] if sid in self.known)}"


class RecordingPool:
    def __init__(self, known=frozenset({1001, 1002, 1003}), fail: bool = False) -> None:
        self.conn = RecordingConn(set(known), fail)

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


class PoisonConn(RecordingConn):
    """Refuses (like PostgreSQL's REAL overflow) any statement carrying a
    speed beyond float32 range; optionally goes down after N statements."""

    def __init__(self, fail_after: int | None = None) -> None:
        super().__init__({1001, 1002, 1003})
        self.fail_after = fail_after
        self.statements = 0

    async def execute(self, query: str, *args):
        import asyncpg

        self.statements += 1
        if self.fail_after is not None and self.statements > self.fail_after:
            raise ConnectionError("db down")
        if any(v > 3.4e38 for v in args[2]):
            raise asyncpg.exceptions.NumericValueOutOfRangeError(
                "value out of range: overflow"
            )
        return await super().execute(query, *args)


class PoisonPool(RecordingPool):
    def __init__(self, fail_after: int | None = None) -> None:
        self.conn = PoisonConn(fail_after)


def with_speeds(frame: pl.DataFrame, speeds: dict[int, float]) -> pl.DataFrame:
    values = frame["speed_kmh"].to_list()
    for i, v in speeds.items():
        values[i] = v
    return frame.with_columns(pl.Series("speed_kmh", values, dtype=pl.Float64))


def canonical(rows: list[tuple]) -> pl.DataFrame:
    """rows: (segment_id, source_ordinal, quality_ordinal)."""
    ts = now_ms()
    df = pl.DataFrame(
        {
            "timestamp_ms": [ts] * len(rows),
            "lon": [72.58] * len(rows),
            "lat": [23.02] * len(rows),
            "segment_id": [str(r[0]) for r in rows],
            "speed_kmh": [33.0] * len(rows),
            "congestion_index": [0.4] * len(rows),
            "source": [r[1] for r in rows],
            "data_quality": [r[2] for r in rows],
            "confidence": [0.9] * len(rows),
        }
    )
    return normalize_batch(df).frame


class TestDbWriter:
    async def test_insert_targets_the_real_observations_schema(self):
        from traffic_oracle.ingest.db_writer import INSERT_OBSERVATIONS_SQL, DbWriter

        pool = RecordingPool()
        written = await DbWriter(pool).write_batch(canonical([(1001, 0, 0), (1002, 2, 1)]))
        assert written == 2
        (query, args), = pool.conn.calls
        assert query == INSERT_OBSERVATIONS_SQL
        cols = re.search(r"INSERT INTO traffic\.observations\s*\(([^)]*)\)", query).group(1)
        assert "timestamp_ms" not in cols  # the pre-fix UndefinedColumnError
        assert "timestamp_utc" in cols and "geom" in cols
        assert "to_timestamp(r.timestamp_ms / 1000.0)" in query
        assert "ST_SetSRID(ST_MakePoint(r.lon, r.lat), 4326)" in query
        assert "corridor.road_segments" in query  # unknown segments filtered
        seg_ids, _ts, _speed, _cong, sources, qualities, *_rest = args
        assert seg_ids == [1001, 1002] and all(isinstance(s, int) for s in seg_ids)
        assert sources == ["government", "fleet_gps"]  # never 'government_traffic'
        assert qualities == ["real", "anomalous"]  # never an ordinal

    async def test_unknown_segments_are_dropped_and_counted(self):
        from traffic_oracle.ingest.db_writer import DbWriter

        writer = DbWriter(RecordingPool(known={1001}))
        written = await writer.write_batch(canonical([(1001, 0, 0), (99999, 0, 0)]))
        assert written == 1
        assert writer.dropped["unknown_segment"] == 1
        assert writer.rows_written_total == 1

    async def test_raw_wire_frame_is_normalised_before_insert(self):
        from traffic_oracle.ingest.db_writer import DbWriter

        pool = RecordingPool()
        df = ArrowDecoder.decode(live_ipc([(now_ms(), "1003", 20.0, 0.6, 3, 0)]))
        assert await DbWriter(pool).write_batch(df) == 1
        assert pool.conn.calls[0][1][4] == ["crowdsource"]

    async def test_write_empty_batch(self):
        from traffic_oracle.ingest.db_writer import DbWriter

        pool = RecordingPool()
        empty = pl.DataFrame(schema=CANONICAL_SCHEMA)
        assert await DbWriter(pool).write_batch(empty) == 0
        assert pool.conn.calls == []

    async def test_buffer_flushes_by_row_count_or_interval(self):
        from traffic_oracle.ingest.db_writer import DbWriter

        writer = DbWriter(RecordingPool(), flush_interval_sec=5.0, flush_rows=3)
        writer.add(canonical([(1001, 0, 0)]))
        t0 = time.monotonic()
        assert not writer.flush_due(now=t0)  # 1 row, interval not elapsed
        assert writer.flush_due(now=t0 + 6.0)  # interval elapsed
        writer.add(canonical([(1002, 0, 0), (1003, 0, 0)]))
        assert writer.pending_rows == 3
        assert writer.flush_due(now=t0)  # row threshold reached
        result = await writer.flush()
        assert (result.submitted, result.written) == (3, 3)
        assert writer.pending_rows == 0
        assert not writer.flush_due(now=t0 + 60)  # nothing pending

    async def test_failed_flush_keeps_rows_for_the_next_attempt(self):
        from traffic_oracle.ingest.db_writer import DbWriter

        pool = RecordingPool(fail=True)
        writer = DbWriter(pool)
        writer.add(canonical([(1001, 0, 0), (1002, 0, 0)]))
        with pytest.raises(ConnectionError):
            await writer.flush()
        assert writer.pending_rows == 2
        assert writer.last_error == "ConnectionError"  # class name, not driver text
        pool.conn.fail = False
        result = await writer.flush()
        assert result.written == 2 and writer.pending_rows == 0
        assert writer.last_error is None

    def test_buffer_cap_drops_oldest_rows_and_counts_them(self):
        from traffic_oracle.ingest.db_writer import DbWriter

        writer = DbWriter(RecordingPool(), max_buffer_rows=3)
        writer.add(canonical([(1001, 0, 0), (1002, 0, 0)]))
        writer.add(canonical([(1003, 0, 0), (1001, 1, 0)]))
        assert writer.pending_rows == 3
        assert writer.dropped["buffer_overflow"] == 1

    async def test_a_content_refused_row_is_isolated_not_retried_forever(self):
        from traffic_oracle.ingest.db_writer import DbWriter

        pool = PoisonPool()
        writer = DbWriter(pool)
        # 9 good rows around one the schema cannot hold (a canonical frame
        # from an unknown future producer — normalize would have dropped it).
        writer.add(with_speeds(canonical([(1001, 0, 0)] * 10), {4: 1e39}))
        result = await writer.flush()  # no exception: progress, not a stall
        assert (result.submitted, result.written) == (9, 9)
        assert writer.dropped["rejected_row"] == 1
        assert writer.pending_rows == 0
        assert writer.rows_written_total == 9
        assert writer.last_error == "NumericValueOutOfRangeError"
        assert pool.conn.statements <= 1 + 2 * 4 + 1  # first try + bisection
        # Later batches flow again.
        writer.add(canonical([(1002, 0, 0)]))
        assert (await writer.flush()).written == 1

    async def test_isolation_is_budgeted_but_every_flush_makes_progress(self):
        from traffic_oracle.ingest.db_writer import DbWriter

        pool = PoisonPool()
        writer = DbWriter(pool, isolation_budget=1)  # floor: 2·⌈log2 16⌉ + 2 = 12
        writer.add(
            with_speeds(canonical([(1001, 0, 0)] * 16), {0: 1e39, 5: 1e39, 10: 1e39, 15: 1e39})
        )
        flushes = 0
        while writer.pending_rows:
            before = (writer.dropped["rejected_row"], writer.rows_written_total)
            statements = pool.conn.statements
            await writer.flush()
            flushes += 1
            # bounded work (first try + budget) and never a no-progress pass
            assert pool.conn.statements - statements <= 1 + 12
            assert writer.dropped["rejected_row"] > before[0]
            assert flushes < 10
        assert flushes > 1  # the budget really bounded the first pass
        assert writer.dropped["rejected_row"] == 4
        assert writer.rows_written_total == 12

    async def test_connection_loss_mid_isolation_keeps_the_rest_buffered(self):
        from traffic_oracle.ingest.db_writer import DbWriter

        pool = PoisonPool(fail_after=2)  # 1st: refused whole, 2nd: half ok, then down
        writer = DbWriter(pool)
        writer.add(with_speeds(canonical([(1001, 0, 0)] * 8), {7: 1e39}))
        with pytest.raises(ConnectionError):
            await writer.flush()
        assert writer.rows_written_total == 4  # the clean half stays written
        assert writer.pending_rows == 4  # the half with the bad row is kept
        assert writer.last_error == "ConnectionError"
        pool.conn.fail_after = None
        await writer.flush()
        assert writer.rows_written_total == 7 and writer.dropped["rejected_row"] == 1


# ─── CacheWriter (pipelined, latest per segment) ──────────────────────────────


class PipelineValkey(MockValkey):
    """MockValkey + the non-transactional pipeline the cache writer uses."""

    def pipeline(self, transaction: bool = True):
        store = self

        class _Pipe:
            def __init__(self) -> None:
                self.ops: list[tuple] = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def set(self, key, value, ex=None):
                self.ops.append((key, value, ex))
                return self

            async def execute(self):
                for key, value, ex in self.ops:
                    await store.set(key, value, ex=ex)
                return [True] * len(self.ops)

        return _Pipe()

    async def scan_iter(self, match: str = "*", count: int | None = None):
        prefix = match.rstrip("*")
        for key in list(self._store):
            if key.startswith(prefix):
                yield key


def make_cache(valkey):
    from traffic_oracle.data.cache import TrafficCache

    cache = TrafficCache.__new__(TrafficCache)
    cache._client = valkey
    return cache


class TestCacheWriter:
    async def test_update_from_batch_caches_latest_per_segment(self):
        from traffic_oracle.data.models import TrafficObservation
        from traffic_oracle.ingest.cache_writer import CacheWriter

        valkey = PipelineValkey()
        writer = CacheWriter(make_cache(valkey))
        ts = now_ms()
        df = ArrowDecoder.decode(
            live_ipc(
                [
                    (ts - 2000, "1001", 50.0, 0.3, 2, 0),
                    (ts, "1001", 20.0, 0.7, 2, 1),  # newest for 1001
                    (ts - 1000, "1002", 60.0, 0.2, 0, 0),
                ]
            )
        )
        assert await writer.update_from_batch(df) == 2
        cached = TrafficObservation.model_validate_json(valkey._store["traffic:latest:1001"])
        assert cached.speed_kmh == 20.0
        assert cached.data_quality is DataQuality.ANOMALOUS
        assert valkey._ttls["traffic:latest:1001"] == 300
        gov = TrafficObservation.model_validate_json(valkey._store["traffic:latest:1002"])
        assert gov.source is FeedSource.GOVERNMENT_TRAFFIC
        assert await make_cache(valkey).count_latest_keys() == 2

    async def test_rows_older_than_the_ttl_never_overwrite_the_cache(self):
        from traffic_oracle.ingest.cache_writer import CacheWriter

        valkey = PipelineValkey()
        writer = CacheWriter(make_cache(valkey))
        df = ArrowDecoder.decode(live_ipc([(now_ms() - 600_000, "1001", 50.0, 0.3, 2, 0)]))
        assert await writer.update_from_batch(df) == 0
        assert "traffic:latest:1001" not in valkey._store

    async def test_update_empty_batch(self):
        from traffic_oracle.ingest.cache_writer import CacheWriter

        valkey = PipelineValkey()
        writer = CacheWriter(make_cache(valkey))
        assert await writer.update_from_batch(pl.DataFrame(schema=CANONICAL_SCHEMA)) == 0


# ─── Live-schema tier (rolled back; read-only unless explicitly opted in) ─────


_DSN = os.environ.get("VVIP_DATABASE_URL")


@pytest.fixture
async def schema_conn():
    if not _DSN:
        pytest.skip("VVIP_DATABASE_URL not set (live-schema contract tier)")
    asyncpg = pytest.importorskip("asyncpg")
    try:
        conn = await asyncpg.connect(_DSN, timeout=5)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database not reachable: {type(exc).__name__}")
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()  # ALWAYS rolled back
        await conn.close()


async def _check_values(conn, table: str, column: str) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c
        WHERE c.conrelid = $1::regclass AND c.contype = 'c'
        """,
        table,
    )
    for r in rows:
        if re.search(rf"\b{column}\b", r["def"]):
            return set(re.findall(r"'([a-z_]+)'::text", r["def"]))
    return set()


class TestDbWriterLiveSchema:
    async def test_insert_prepares_against_the_live_schema(self, schema_conn):
        from traffic_oracle.anomaly.stream_detector import INSERT_ANOMALIES_SQL
        from traffic_oracle.ingest.db_writer import INSERT_OBSERVATIONS_SQL

        # Parse + describe against the real catalog: column names, the
        # bigint/text/float8 parameter types and the PostGIS calls resolve.
        stmt = await schema_conn.prepare(INSERT_OBSERVATIONS_SQL)
        assert [t.name for t in stmt.get_parameters()] == [
            "int8[]", "int8[]", "float8[]", "float8[]", "text[]",
            "text[]", "float8[]", "float8[]", "float8[]",
        ]
        anomaly_stmt = await schema_conn.prepare(INSERT_ANOMALIES_SQL)
        assert [t.name for t in anomaly_stmt.get_parameters()] == [
            "int8[]", "int8[]", "text[]", "text[]", "jsonb[]",
        ]

    async def test_mapped_vocabularies_satisfy_the_check_constraints(self, schema_conn):
        from traffic_oracle.anomaly.stream_detector import ANOMALY_FLAG_VOCABULARY, FLAG_SEVERITY

        sources = await _check_values(schema_conn, "traffic.observations", "source")
        qualities = await _check_values(schema_conn, "traffic.observations", "data_quality")
        anomaly_types = await _check_values(schema_conn, "traffic.anomaly_log", "anomaly_type")
        severities = await _check_values(schema_conn, "traffic.anomaly_log", "severity")
        assert sources >= {s.db_value for s in FeedSource}
        assert "government_traffic" not in sources
        assert qualities >= {q.value for q in DataQuality}
        assert anomaly_types >= ANOMALY_FLAG_VOCABULARY
        assert "anomalous" not in anomaly_types  # never writable as a type
        assert severities >= set(FLAG_SEVERITY.values())

    @pytest.mark.skipif(
        os.environ.get("VEER_TEST_DB_WRITES") != "1",
        reason="executing inserts (even rolled back) needs VEER_TEST_DB_WRITES=1 (scratch DB)",
    )
    async def test_insert_executes_and_filters_unknown_segments(self, schema_conn):
        from traffic_oracle.ingest.db_writer import DbWriter

        known = await schema_conn.fetchval("SELECT min(segment_id) FROM corridor.road_segments")
        assert known is not None

        class _OneConnPool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self):
                        return schema_conn

                    async def __aexit__(self, *exc):
                        return False

                return _Ctx()

        writer = DbWriter(_OneConnPool())
        frame = canonical([(known, 0, 0), (known, 3, 2), (987654321, 1, 0)])
        assert await writer.write_batch(frame) == 2
        assert writer.dropped["unknown_segment"] == 1
        rows = await schema_conn.fetch(
            "SELECT source, data_quality, ST_SRID(geom) AS srid FROM traffic.observations "
            "WHERE segment_id = $1 AND timestamp_utc > now() - interval '1 minute' "
            "ORDER BY source",
            known,
        )
        assert {(r["source"], r["data_quality"], r["srid"]) for r in rows} >= {
            ("government", "real", 4326),
            ("crowdsource", "synthetic", 4326),
        }

    @pytest.mark.skipif(
        os.environ.get("VEER_TEST_DB_WRITES") != "1",
        reason="executing inserts (even rolled back) needs VEER_TEST_DB_WRITES=1 (scratch DB)",
    )
    async def test_postgres_refusal_of_one_row_is_isolated(self, schema_conn):
        # The REAL error class from the server (REAL overflow) drives the
        # bisection; each statement runs in a SAVEPOINT so the outer,
        # always-rolled-back transaction survives the refusals.
        from traffic_oracle.ingest.db_writer import DbWriter

        known = await schema_conn.fetchval("SELECT min(segment_id) FROM corridor.road_segments")

        class _SavepointConn:
            async def execute(self, query, *args):
                async with schema_conn.transaction():
                    return await schema_conn.execute(query, *args)

        class _Pool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self):
                        return _SavepointConn()

                    async def __aexit__(self, *exc):
                        return False

                return _Ctx()

        writer = DbWriter(_Pool())
        writer.add(with_speeds(canonical([(known, 0, 0)] * 6), {2: 1e39}))
        result = await writer.flush()
        assert result.written == 5
        assert writer.dropped["rejected_row"] == 1
        assert writer.last_error == "NumericValueOutOfRangeError"


# ─── Forecaster input gate (consumes the ingest hot cache) ───────────────────


class _FakeCache:
    def __init__(self, observations: dict) -> None:
        self._observations = observations
        self._client = MockValkey()

    async def get_latest(self, segment_id: int):
        return self._observations.get(segment_id)


class _StubOnnx:
    def __init__(self, max_nodes: int) -> None:
        self.max_nodes = max_nodes
        self.calls: list[dict] = []

    async def infer(self, inputs: dict) -> dict:
        self.calls.append(inputs)
        predictions = np.zeros((1, self.max_nodes, 4, 2), dtype=np.float32)
        predictions[..., 0] = 48.0
        predictions[..., 1] = 0.25
        return {"predictions": predictions}


def _forecaster(observations: dict):
    from traffic_oracle.predict.flow_forecaster import FlowForecaster

    forecaster = FlowForecaster(
        onnx_model_path=None,
        cache=_FakeCache(observations),
        config=SimpleNamespace(max_nodes=8, lookback_steps=12, in_features=8),
    )
    stub = _StubOnnx(8)
    forecaster._onnx_serve = stub
    return forecaster, stub


class TestForecasterPerSegmentSupportGate:
    async def test_only_the_violating_segment_falls_to_heuristic_rows(self):
        forecaster, stub = _forecaster(
            {
                100: make_observation(segment_id=100, speed_kmh=50.0, congestion_idx=0.3),
                200: make_observation(segment_id=200, speed_kmh=84.6, congestion_idx=0.1),
            }
        )
        result = await forecaster.predict([100, 200])
        # Model rung ran for the in-support segment only.
        assert len(stub.calls) == 1
        mask = stub.calls[0]["node_mask"][0]
        assert list(mask[:2]) == [False, True]  # one real node
        assert result[100][5] == {"speed_kmh": 48.0, "congestion_idx": 0.25}
        # 84.6 km/h motorway reading (> 75 = 1.25 × 60) → heuristic row.
        speed = round(max(5.0, 84.6 * (1 - 0.015)), 1)
        assert result[200][5] == {"speed_kmh": speed, "congestion_idx": round(0.1 * 1.02, 3)}
        assert forecaster.last_forecast_rung == "dstgat"
        assert forecaster.last_degradation_reason == "out_of_support:speed_norm"
        assert forecaster.last_out_of_support_segments == [200]

    async def test_whole_batch_abstains_only_when_nothing_is_in_support(self):
        forecaster, stub = _forecaster(
            {
                100: make_observation(segment_id=100, speed_kmh=2.0),
                200: make_observation(segment_id=200, speed_kmh=95.0),
            }
        )
        result = await forecaster.predict([100, 200, 300])  # 300 unobserved
        assert stub.calls == []
        assert forecaster.last_forecast_rung == "heuristic"
        assert set(result) == {100, 200}
        assert forecaster.last_out_of_support_segments == [100, 200]

    async def test_out_of_support_segment_never_feeds_the_foundation_window(self):
        forecaster, _stub = _forecaster(
            {
                100: make_observation(segment_id=100, speed_kmh=50.0),
                200: make_observation(segment_id=200, speed_kmh=1.0),
            }
        )
        for _ in range(3):
            await forecaster.predict([100, 200])
        # WP-11: predict() never writes the foundation window (neither the
        # in-support nor the out-of-support segment) — the ingest history
        # feed (predict/history_feed.py) fills it from traffic.observations
        # on the 5-min observation-time grid.
        assert forecaster.history_window.segments() == []

    async def test_reason_and_segments_reset_between_calls(self):
        forecaster, _ = _forecaster(
            {100: make_observation(segment_id=100, speed_kmh=50.0)}
        )
        forecaster.last_out_of_support_segments = [7]
        await forecaster.predict([100])
        assert forecaster.last_out_of_support_segments == []
        assert forecaster.last_degradation_reason is None
