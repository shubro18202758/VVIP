"""Historical aggregates + the synthetic rollup — contract C-P (WP-19, 019).

    GET /api/v1/traffic/historical?segment_id=&hours=&day_of_week=&hour=
                                   &include_synthetic=

Covered here:

    * ``aggregates`` stays the REAL-only baseline (traffic.hourly_aggregates
      — the twin's calibration input) with its pre-C-P row shape;
    * the simulated feed's rollup rides BESIDE it as
      ``synthetic_aggregates`` (every row ``data_quality: 'synthetic'``),
      never merged, with ``provenance`` real | synthetic | none;
    * ``day_of_week`` (0 = Monday) / ``hour`` (IST) filter the same slot
      with a 672 h default look-back, and ``basis`` says how (UTC hour
      buckets placed at their IST midpoint, ``ist_bucket_offset_min`` 30);
    * partition maintenance rolls the synthetic hour into its own table
      (probed — a pre-019 store skips it), catches up missing hours inside
      the retention window (bounded: hours, wall time, newest first),
      applies the same retention cutoff and reports it on /health;
      ROLLUP_HOUR_SQL stays real-only;
    * an unlabelled batch (no data_quality column) is dropped and counted —
      never presumed real;
    * NO calibration, twin, pattern or vector SQL names the synthetic table.

An optional DB tier (VEER_TEST_PG_DSN = a SANDBOX store with 019; never the
live :56434 store) PREPAREs the endpoint's statements and runs one rollup
inside a transaction that is rolled back.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from fastapi.testclient import TestClient

from traffic_oracle import __main__ as main_mod
from traffic_oracle.ingest import partitions as parts
from traffic_oracle.ingest.normalize import normalize_batch
from traffic_oracle.ingest.partitions import (
    ROLLUP_HOUR_SQL,
    ROLLUP_SYNTHETIC_HOUR_SQL,
    PartitionManager,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "traffic_oracle"
SYNTHETIC_TABLE = "hourly_aggregates_synthetic"
NOW = datetime(2026, 10, 22, 21, 30, tzinfo=timezone.utc)


def _row(hour: datetime, speed: float = 50.0) -> dict:
    return {"segment_id": 1001, "hour_utc": hour, "avg_speed_kmh": speed,
            "p50_speed_kmh": speed - 1.0, "p95_congestion": 0.4, "observation_cnt": 409}


class HistConn:
    def __init__(self, real: list[dict], synthetic: list[dict] | None) -> None:
        self.real = real
        self.synthetic = synthetic  # None = a pre-019 store
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: Any):
        self.calls.append((sql, args))
        if sql == main_mod._SQL_HISTORICAL_REAL:
            return self.real
        if sql == main_mod._SQL_HISTORICAL_SYNTHETIC:
            assert self.synthetic is not None, "read a table the probe said is absent"
            return self.synthetic
        raise AssertionError("unexpected fetch: " + sql[:80])

    async def fetchval(self, sql: str, *args: Any):
        assert sql == main_mod._SQL_SYNTHETIC_TABLE_PRESENT
        return self.synthetic is not None


class _Pool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False

        return _A()


def _client(conn: HistConn) -> TestClient:
    app = main_mod.create_app()
    app.state.pool = _Pool(conn)
    return TestClient(app)


H = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc)


# ─── the endpoint ────────────────────────────────────────────────────────────


class TestHistoricalEndpoint:
    def test_real_aggregates_keep_their_shape_and_lead(self):
        conn = HistConn(real=[_row(H)], synthetic=[_row(H, 30.0)])
        body = _client(conn).get("/api/v1/traffic/historical?segment_id=1001").json()
        (agg,) = body["aggregates"]
        assert set(agg) == {"hour_utc", "avg_speed_kmh", "p50_speed_kmh",
                            "p95_congestion", "observation_cnt"}  # unchanged shape
        assert agg["avg_speed_kmh"] == 50.0
        assert body["provenance"] == "real"
        # the synthetic rollup rides beside it, labelled, never merged
        (syn,) = body["synthetic_aggregates"]
        assert syn["data_quality"] == "synthetic" and syn["avg_speed_kmh"] == 30.0
        assert "data_quality" not in agg

    def test_only_synthetic_history_is_labelled_synthetic(self):
        conn = HistConn(real=[], synthetic=[_row(H), _row(H + timedelta(hours=1))])
        body = _client(conn).get("/api/v1/traffic/historical?segment_id=1001").json()
        assert body["aggregates"] == []
        assert body["provenance"] == "synthetic"
        assert len(body["synthetic_aggregates"]) == 2
        assert all(r["data_quality"] == "synthetic" for r in body["synthetic_aggregates"])
        assert "never a calibration input" in body["note"]
        assert body["basis"] == {"kind": "last_n_hours", "day_of_week": None, "hour": None,
                                 "lookback_hours": 24, "ist_bucket_offset_min": 30}

    def test_no_history_at_all_is_none(self):
        body = _client(HistConn(real=[], synthetic=[])).get(
            "/api/v1/traffic/historical?segment_id=1001"
        ).json()
        assert (body["aggregates"], body["synthetic_aggregates"], body["provenance"]) == (
            [], [], "none"
        )

    def test_a_pre_019_store_answers_without_the_synthetic_table(self):
        conn = HistConn(real=[], synthetic=None)
        body = _client(conn).get("/api/v1/traffic/historical?segment_id=1001").json()
        assert body["synthetic_available"] is False and body["synthetic_aggregates"] == []
        assert body["provenance"] == "none"

    def test_include_synthetic_false_reads_the_real_table_only(self):
        conn = HistConn(real=[], synthetic=[_row(H)])
        body = _client(conn).get(
            "/api/v1/traffic/historical?segment_id=1001&include_synthetic=false"
        ).json()
        assert body["synthetic_aggregates"] == [] and body["synthetic_available"] is None
        assert [sql for sql, _ in conn.calls] == [main_mod._SQL_HISTORICAL_REAL]

    def test_a_weekday_hour_slot_defaults_to_four_weeks(self):
        conn = HistConn(real=[], synthetic=[_row(H)])
        before = datetime.now(timezone.utc)
        body = _client(conn).get(
            "/api/v1/traffic/historical?segment_id=1001&day_of_week=3&hour=9"
        ).json()
        assert body["basis"] == {"kind": "same_weekday_hour_ist", "day_of_week": 3,
                                 "hour": 9, "lookback_hours": 672,
                                 "ist_bucket_offset_min": 30}
        (_sql, (segment, cutoff, dow, hour)), _ = conn.calls
        assert (segment, dow, hour) == (1001, 3, 9)
        assert before - timedelta(hours=672, minutes=1) < cutoff < before - timedelta(hours=671)

    def test_an_explicit_lookback_wins_and_hour_alone_is_a_slot(self):
        conn = HistConn(real=[], synthetic=[])
        body = _client(conn).get(
            "/api/v1/traffic/historical?segment_id=1001&hours=168&hour=9"
        ).json()
        assert body["basis"]["kind"] == "same_weekday_hour_ist"
        assert body["basis"]["lookback_hours"] == 168
        assert body["basis"]["day_of_week"] is None

    @pytest.mark.parametrize(
        "query",
        ["day_of_week=7", "day_of_week=-1", "hour=24", "hour=-1", "hours=0", "hours=721"],
    )
    def test_out_of_range_filters_are_422(self, query):
        conn = HistConn(real=[], synthetic=[])
        r = _client(conn).get(f"/api/v1/traffic/historical?segment_id=1001&{query}")
        assert r.status_code == 422
        assert conn.calls == []


class TestHistoricalSql:
    def test_the_real_statement_reads_the_real_table_only(self):
        sql = main_mod._SQL_HISTORICAL_REAL
        assert "FROM traffic.hourly_aggregates " in sql and SYNTHETIC_TABLE not in sql
        assert "FROM traffic.hourly_aggregates_synthetic " in main_mod._SQL_HISTORICAL_SYNTHETIC

    @pytest.mark.parametrize(
        "sql", [main_mod._SQL_HISTORICAL_REAL, main_mod._SQL_HISTORICAL_SYNTHETIC]
    )
    def test_the_slot_is_the_ist_midpoint_of_each_utc_bucket(self, sql):
        flat = " ".join(sql.split())
        midpoint = "(hour_utc + interval '30 minutes') AT TIME ZONE 'Asia/Kolkata'"
        assert f"EXTRACT(ISODOW FROM {midpoint})::int - 1 = $3::int" in flat
        assert f"EXTRACT(HOUR FROM {midpoint})::int = $4::int" in flat
        assert "$3::int IS NULL OR" in flat and "$4::int IS NULL OR" in flat


# ─── the synthetic rollup in partition maintenance ──────────────────────────


class RollupConn:
    def __init__(self, *, present: bool = True, have: set[datetime] | None = None,
                 rows_per_hour: int = 47) -> None:
        self.present = present
        self.have = set(have or ())
        self.rows_per_hour = rows_per_hour
        self.rolled: list[datetime] = []
        self.purged_before: list[datetime] = []

    async def fetchval(self, sql: str, *args: Any):
        assert sql == parts.SYNTHETIC_TABLE_EXISTS_SQL
        return self.present

    async def fetch(self, sql: str, *args: Any):
        assert sql == parts.SYNTHETIC_HOURS_SQL
        start, end = args
        return [{"hour_utc": h} for h in self.have if start <= h < end]

    async def execute(self, sql: str, *args: Any):
        if sql == ROLLUP_SYNTHETIC_HOUR_SQL:
            self.rolled.append(args[0])
            return f"INSERT 0 {self.rows_per_hour}"
        if sql == parts.PURGE_SYNTHETIC_SQL:
            self.purged_before.append(args[0])
            return "DELETE 3"
        raise AssertionError("unexpected execute: " + sql[:60])


class _OnePool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False

        return _A()


def _rollup(conn: RollupConn, manager: PartitionManager | None = None,
            now: datetime = NOW) -> tuple[PartitionManager, parts.MaintenanceReport]:
    manager = manager or PartitionManager(_OnePool(conn))
    report = parts.MaintenanceReport(ran_at="t")
    asyncio.run(manager._rollup_synthetic(now, report))
    return manager, report


class TestSyntheticRollup:
    def test_the_real_rollup_stays_real_only(self):
        flat = " ".join(ROLLUP_HOUR_SQL.split())
        assert "INSERT INTO traffic.hourly_aggregates (" in flat
        assert "AND data_quality = 'real'" in flat and SYNTHETIC_TABLE not in flat

    def test_the_synthetic_rollup_reads_synthetic_rows_into_its_own_table(self):
        flat = " ".join(ROLLUP_SYNTHETIC_HOUR_SQL.split())
        assert "INSERT INTO traffic.hourly_aggregates_synthetic (" in flat
        assert "AND data_quality = 'synthetic'" in flat and "'real'" not in flat

    def test_a_pre_019_store_skips_the_step(self):
        conn = RollupConn(present=False)
        _, report = _rollup(conn)
        assert conn.rolled == [] and report.synthetic_rollup_rows is None
        assert report.to_dict()["synthetic_rollup"]["table_present"] is False

    def test_first_pass_rolls_the_hour_and_catches_up_missing_hours_newest_first(self):
        prev = datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc)
        have = {prev - timedelta(hours=k) for k in range(2, 168)}  # all but 1 h and 168 h ago
        conn = RollupConn(have=have)
        manager, report = _rollup(conn)
        assert conn.rolled[0] == prev  # the previous complete hour first
        assert conn.rolled[1:] == [prev - timedelta(hours=1), prev - timedelta(hours=168)]
        assert report.synthetic_rollup_rows == 47
        assert (report.synthetic_catchup_hours, report.synthetic_catchup_rows) == (2, 94)
        assert report.synthetic_catchup_pending == 0
        assert conn.purged_before == [manager.retention_cutoff(NOW)]
        assert report.to_dict()["synthetic_rollup"] == {
            "table_present": True, "rollup_rows": 47, "catchup_hours": 2,
            "catchup_rows": 94, "catchup_pending_hours": 0, "rows_purged": 3,
            "calibration_input": False,
        }

    def test_the_catch_up_is_bounded_to_seven_days_and_the_retention_window(self):
        conn = RollupConn()
        _, report = _rollup(conn)
        assert report.synthetic_catchup_hours == parts.SYNTHETIC_CATCHUP_HOURS == 168
        conn2 = RollupConn()
        _, report2 = _rollup(conn2, PartitionManager(_OnePool(conn2), retention_days=2))
        # retention 2 days: start of day 20 Oct .. 19:00 on 22 Oct
        assert report2.synthetic_catchup_hours == 2 * 24 + 20
        assert min(conn2.rolled) == datetime(2026, 10, 20, tzinfo=timezone.utc)

    def test_a_wall_time_budget_carries_the_rest_to_the_next_pass(self):
        conn = RollupConn()
        manager = PartitionManager(_OnePool(conn))
        manager._synthetic_catchup_budget_sec = 0.0  # nothing fits this pass
        _, report = _rollup(conn, manager)
        assert report.synthetic_catchup_hours == 0 and report.synthetic_catchup_pending == 168
        manager._synthetic_catchup_budget_sec = 60.0
        _, report = _rollup(conn, manager)  # the next pass drains it
        assert report.synthetic_catchup_hours == 168 and report.synthetic_catchup_pending == 0
        _, report = _rollup(conn, manager)  # and never rescans the window
        assert report.synthetic_catchup_hours == 0

    def test_run_reports_the_step_on_the_health_block(self):
        conn = RollupConn()
        manager = PartitionManager(_OnePool(conn))

        async def noop(now, report):
            return None

        manager._ensure_partitions = noop
        manager._drop_expired = noop
        manager._purge_anomalies = noop
        manager._rollup = noop
        report = asyncio.run(manager.run(now=NOW))
        assert report.errors == []
        assert report.to_dict()["synthetic_rollup"]["rollup_rows"] == 47


# ─── an unlabelled batch is never presumed real ──────────────────────────────


def test_a_batch_without_a_quality_column_is_dropped_and_counted():
    df = pl.DataFrame({
        "timestamp_ms": [1_792_700_000_000], "lon": [72.58], "lat": [23.02],
        "segment_id": ["1001"], "speed_kmh": [40.0], "congestion_index": [0.3],
        "source": ["FleetGps"],
    })
    batch = normalize_batch(df)
    assert batch.rows == 0 and batch.received_rows == 1
    assert dict(batch.dropped) == {"missing_data_quality": 1}


# ─── calibration never reads the synthetic rollup ───────────────────────────


#: The packages whose SQL calibrates, trains, matches patterns or embeds.
CALIBRATION_SOURCES = (
    "simulation/calibration.py",
    "simulation/scheduler.py",
    "synthetic/pattern_matcher.py",
    "data/vector_store.py",
)


class TestCalibrationGuard:
    @pytest.mark.parametrize("rel", CALIBRATION_SOURCES)
    def test_no_calibration_sql_names_the_synthetic_table(self, rel):
        text = (SRC / rel).read_text()
        assert SYNTHETIC_TABLE not in text, rel
        assert "hourly_aggregates" in text, f"{rel} no longer reads the real baseline?"

    @pytest.mark.parametrize("pkg", ["simulation", "synthetic", "predict", "adaptive", "optimize"])
    def test_no_fitting_package_names_it_anywhere(self, pkg):
        hits = [
            str(p.relative_to(SRC))
            for p in (SRC / pkg).rglob("*.py")
            if SYNTHETIC_TABLE in p.read_text()
        ]
        assert hits == [], hits

    def test_only_the_rollup_and_the_labelled_endpoint_name_it(self):
        hits = sorted(
            str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if SYNTHETIC_TABLE in p.read_text()
        )
        assert hits == ["__main__.py", "ingest/partitions.py"]

    def test_the_calibration_reads_are_the_real_table(self):
        from traffic_oracle.simulation import calibration as cal

        for name in ("_SQL_HOURLY_AGGREGATES", "_SQL_FIELD_HOURS"):
            sql = getattr(cal, name)
            assert re.search(r"FROM traffic\.hourly_aggregates\b(?!_)", sql), name


# ─── DB tier (sandbox store with 019; everything rolled back) ───────────────

_DSN = os.environ.get("VEER_TEST_PG_DSN", "")
_LIVE_PORT = ":56434"
db = pytest.mark.skipif(
    not _DSN or _LIVE_PORT in _DSN,
    reason="set VEER_TEST_PG_DSN to a SANDBOX store (001-019 + seed); never the live :56434 store",
)


@db
def test_db_statements_prepare_and_one_rollup_lands_labelled():
    import asyncpg

    async def run():
        conn = await asyncpg.connect(_DSN)
        tx = conn.transaction()
        await tx.start()
        try:
            if not await conn.fetchval(main_mod._SQL_SYNTHETIC_TABLE_PRESENT):
                pytest.skip("migration 019 not applied on this store")
            for sql in (main_mod._SQL_HISTORICAL_REAL, main_mod._SQL_HISTORICAL_SYNTHETIC,
                        ROLLUP_SYNTHETIC_HOUR_SQL, parts.SYNTHETIC_HOURS_SQL,
                        parts.PURGE_SYNTHETIC_SQL):
                await conn.prepare(sql)
            comment = await conn.fetchval(
                "SELECT obj_description('traffic.hourly_aggregates_synthetic'::regclass)"
            )
            assert comment == "synthetic rollup — never a calibration input"
            hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
            await conn.execute(
                "INSERT INTO traffic.observations (segment_id, timestamp_utc, speed_kmh, "
                "congestion_idx, source, data_quality, confidence, geom) VALUES "
                "(1001, $1, 31.0, 0.5, 'fleet_gps', 'synthetic', 0.9, "
                "ST_SetSRID(ST_MakePoint(72.57, 23.02), 4326)), "
                "(1001, $1, 99.0, 0.1, 'fleet_gps', 'real', 0.9, "
                "ST_SetSRID(ST_MakePoint(72.57, 23.02), 4326))",
                hour + timedelta(minutes=5),
            )
            await conn.execute(ROLLUP_SYNTHETIC_HOUR_SQL, hour)
            row = await conn.fetchrow(
                "SELECT avg_speed_kmh, observation_cnt FROM traffic.hourly_aggregates_synthetic "
                "WHERE segment_id = 1001 AND hour_utc = $1", hour
            )
            assert row is not None
            assert row["observation_cnt"] >= 1
            # the real row never reaches the synthetic rollup, nor vice versa
            real = await conn.fetch(
                "SELECT avg_speed_kmh FROM traffic.hourly_aggregates "
                "WHERE segment_id = 1001 AND hour_utc = $1", hour
            )
            assert all(abs(float(r["avg_speed_kmh"]) - 31.0) > 1e-6 for r in real)
            rows = await conn.fetch(main_mod._SQL_HISTORICAL_SYNTHETIC, 1001,
                                    hour - timedelta(hours=1), None, None)
            assert any(r["hour_utc"] == hour for r in rows)
        finally:
            await tx.rollback()
            await conn.close()

    asyncio.run(run())
