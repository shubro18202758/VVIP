"""traffic.anomaly_log retention + its detection-time index (WP-16).

The hourly partition maintenance (ingest/partitions.py) now also deletes
anomaly_log rows DETECTED (``COALESCE(flagged_at, timestamp_utc)``) before
the observation retention cutoff — VVIP_OBSERVATION_RETENTION_DAYS, 14 by
default — oldest first, in bounded batches, each batch in its own
transaction under the maintenance advisory lock, reported on /health like
every other step.  Migration 014 indexes the same expression for
GET /anomalies/recent and for the batch selection.

Three layers:

* Fake pool (always run): batching, the per-pass budget and the reported
  backlog, yielding to a peer that holds the lock, the cutoff, failure
  isolation, the /health report shape, Settings → cutoff.
* Migration 014 as text (always run): naive-splitter grammar, schema-only
  additive idempotent statements touching only 003's anomaly log and
  011's dispatch audit (no seed reference), in lockstep with the readers'
  ORDER BY and the dispatch audit writer.
* DB-backed (VEER_TEST_PG_DSN pointing at a SANDBOX store; never the live
  :56434 store): the purge on the real table inside a transaction that is
  always rolled back — a stale_data row (old observation time, recent
  detection) is KEPT — migration 014's index on the real schema, serving
  both statements, and 014 applied to a pre-014 store then re-applied as
  a no-op (also rolled back).
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from traffic_oracle.ingest import partitions as parts
from traffic_oracle.ingest.partitions import (
    ANOMALY_PURGE_BATCH_ROWS,
    ANOMALY_PURGE_MAX_BATCHES,
    PURGE_ANOMALIES_SQL,
    PartitionManager,
)

NOW = datetime(2026, 10, 22, 16, 5, tzinfo=timezone.utc)


# ── fake pool ─────────────────────────────────────────────────────────────────


class _Ctx:
    """``async with`` yielding a fixed value (an acquired conn, a tx)."""

    def __init__(self, value=None) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


class PurgeConn:
    """Answers the purge step's statements; ``deletes`` is the queue of
    row counts successive batches report."""

    def __init__(self, deletes: list[int], lock: bool = True, fail: bool = False) -> None:
        self.deletes = list(deletes)
        self.lock = lock
        self.fail = fail
        self.calls: list[tuple[str, tuple]] = []
        self.transactions = 0
        self.open_tx = 0

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if sql.startswith("SET LOCAL lock_timeout"):
            assert self.open_tx == 1, "SET LOCAL outside a transaction is a no-op"
            return "SET"
        if sql == PURGE_ANOMALIES_SQL:
            assert self.open_tx == 1, "a batch runs inside its own transaction"
            if self.fail:
                raise ConnectionError("db went away")
            return f"DELETE {self.deletes.pop(0) if self.deletes else 0}"
        raise AssertionError("unexpected execute: " + sql[:60])

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        assert "pg_try_advisory_xact_lock" in sql
        return self.lock

    def transaction(self):
        return _CountingTx(self)


class _CountingTx:
    """A transaction on :class:`PurgeConn` that counts itself."""

    def __init__(self, conn: PurgeConn) -> None:
        self.conn = conn

    async def __aenter__(self):
        self.conn.transactions += 1
        self.conn.open_tx += 1
        return self

    async def __aexit__(self, *exc):
        self.conn.open_tx -= 1
        return False


class OnePool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        return _Ctx(self.conn)


def _purge(conn: PurgeConn, **kw) -> parts.MaintenanceReport:
    manager = PartitionManager(OnePool(conn), **kw)
    report = parts.MaintenanceReport(ran_at="t")
    asyncio.run(manager._purge_anomalies(NOW, report))
    return report


def _batches(conn: PurgeConn) -> list[tuple]:
    return [args for sql, args in conn.calls if sql == PURGE_ANOMALIES_SQL]


def test_defaults_are_bounded():
    assert (ANOMALY_PURGE_BATCH_ROWS, ANOMALY_PURGE_MAX_BATCHES) == (5000, 20)


def test_purge_drains_in_bounded_batches():
    conn = PurgeConn(deletes=[5000, 5000, 1234])
    report = _purge(conn)
    assert report.anomaly_rows_purged == 11_234 and report.anomaly_backlog is False
    assert [b[1] for b in _batches(conn)] == [5000, 5000, 5000]
    # one transaction (and one advisory-lock attempt) per batch
    assert conn.transactions == 3
    assert sum("pg_try_advisory_xact_lock" in sql for sql, _ in conn.calls) == 3


def test_a_backlog_beyond_the_budget_is_reported_not_swept():
    conn = PurgeConn(deletes=[100] * 10)
    report = _purge(conn, anomaly_batch_rows=100, anomaly_max_batches=3)
    assert len(_batches(conn)) == 3
    assert report.anomaly_rows_purged == 300 and report.anomaly_backlog is True


def test_purge_yields_to_a_peer_holding_the_lock():
    conn = PurgeConn(deletes=[5000], lock=False)
    report = _purge(conn)
    assert _batches(conn) == [] and report.skipped_locked is True
    assert report.anomaly_rows_purged == 0 and report.anomaly_backlog is None


def test_cutoff_is_the_observation_retention_cutoff():
    conn = PurgeConn(deletes=[0])
    _purge(conn, retention_days=14)
    (cutoff, _limit), = _batches(conn)
    assert cutoff == datetime(2026, 10, 8, tzinfo=timezone.utc)  # start of day − 14 d
    manager = PartitionManager(OnePool(conn), retention_days=3)
    assert manager.retention_cutoff(NOW) == datetime(2026, 10, 19, tzinfo=timezone.utc)


def test_retention_days_come_from_vvip_observation_retention_days(monkeypatch):
    from traffic_oracle.data.config import Settings

    assert Settings().OBSERVATION_RETENTION_DAYS == 14
    monkeypatch.setenv("VVIP_OBSERVATION_RETENTION_DAYS", "7")
    days = Settings().OBSERVATION_RETENTION_DAYS
    manager = PartitionManager(OnePool(PurgeConn([])), retention_days=days)
    assert manager.retention_cutoff(NOW) == datetime(2026, 10, 15, tzinfo=timezone.utc)


def test_a_failing_purge_is_reported_like_any_step_and_isolated():
    conn = PurgeConn(deletes=[], fail=True)
    manager = PartitionManager(OnePool(conn))

    async def noop(now, report):
        return None

    rolled = []

    async def rollup(now, report):
        rolled.append(True)

    manager._ensure_partitions = noop
    manager._drop_expired = noop
    manager._rollup = rollup
    manager._rollup_synthetic = noop  # WP-19's synthetic step (own tests)
    report = asyncio.run(manager.run(now=NOW))
    assert report.errors == ["_purge_anomalies: ConnectionError"]  # class name only
    assert rolled == [True]  # the next step still ran
    assert manager.last_report is report


def test_health_report_carries_the_purge():
    report = parts.MaintenanceReport(ran_at="t", anomaly_rows_purged=42, anomaly_backlog=False)
    body = report.to_dict()
    assert body["anomaly_rows_purged"] == 42 and body["anomaly_backlog"] is False
    assert parts.MaintenanceReport(ran_at="t").to_dict()["anomaly_backlog"] is None


def test_purge_statement_selects_by_detection_time_through_the_primary_key():
    sql = " ".join(PURGE_ANOMALIES_SQL.split())
    assert "COALESCE(flagged_at, timestamp_utc) < $1::timestamptz" in sql
    assert "ORDER BY COALESCE(flagged_at, timestamp_utc) LIMIT $2" in sql
    assert sql.startswith("DELETE FROM traffic.anomaly_log WHERE anomaly_id = ANY (ARRAY(")


def test_batch_bounds_are_validated():
    with pytest.raises(ValueError):
        PartitionManager(OnePool(PurgeConn([])), anomaly_batch_rows=0)
    with pytest.raises(ValueError):
        PartitionManager(OnePool(PurgeConn([])), anomaly_max_batches=0)


# ── migration 014 as text ─────────────────────────────────────────────────────

REPO = Path(__file__).resolve().parents[3]
M014 = REPO / "services" / "corridor-store" / "migrations" / "014_anomaly_log_hygiene.sql"


def _statements(text: str) -> list[str]:
    """The naive splitter (comment lines dropped, split on ';')."""
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


class TestMigration014:
    def test_exists_and_matches_runner_pattern(self):
        assert M014.exists()
        assert re.fullmatch(r"\d{3}_[A-Za-z0-9_]+\.sql", M014.name)

    def test_naive_splitter_safe(self):
        text = M014.read_text()
        body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
        in_literal = False
        for ch in body:
            if ch == "'":
                in_literal = not in_literal
            elif ch == ";" and in_literal:
                pytest.fail("';' inside a string literal breaks naive splitters")
        assert "$$" not in text and "/*" not in text

    def test_schema_only_additive_idempotent_without_seed_references(self):
        stmts = _statements(M014.read_text())
        assert len(stmts) == 2
        for stmt in stmts:
            head = " ".join(stmt.split()).upper()
            assert head.startswith(("CREATE INDEX IF NOT EXISTS", "ALTER TABLE")), head[:60]
            if head.startswith("ALTER TABLE"):
                assert "ADD COLUMN IF NOT EXISTS" in head
            for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP"):
                assert not re.search(rf"\b{verb}\b", head), verb
        # Only 003's anomaly log and 011's dispatch audit: no seed table
        # (segments, junctions, cameras …) is referenced, so a fresh
        # install can run 001-014 before the seed.
        tables = set(re.findall(r"\b(?:ON|TABLE)\s+([a-z_]+\.[a-z_]+)", " ".join(stmts), re.I))
        assert tables == {"traffic.anomaly_log", "corridor.dispatch_events"}

    def test_index_key_is_what_the_readers_order_and_select_by(self):
        flat = " ".join(M014.read_text().split())
        assert (
            "CREATE INDEX IF NOT EXISTS idx_anomaly_log_detected ON traffic.anomaly_log "
            "((COALESCE(flagged_at, timestamp_utc)) DESC, anomaly_id DESC)"
        ) in flat
        purge = " ".join(PURGE_ANOMALIES_SQL.split())
        assert "WHERE COALESCE(flagged_at, timestamp_utc) < $1" in purge

    def test_detail_column_in_lockstep_with_the_dispatch_audit_writer(self):
        from traffic_oracle.units import router as units_router

        assert "ADD COLUMN IF NOT EXISTS detail JSONB" in " ".join(M014.read_text().split())
        writer = inspect.getsource(units_router._audit)
        assert "(dispatch_id, from_status, to_status, actor, note, detail)" in writer
        assert "a.attname = 'detail'" in units_router._SQL_AUDIT_DETAIL_COLUMN


# ── DB-backed (sandbox store only, always rolled back) ────────────────────────

_DSN = os.environ.get("VEER_TEST_PG_DSN", "")
_LIVE_PORT = ":56434/"  # the shared live store — never written by tests

db = pytest.mark.skipif(
    not _DSN or _LIVE_PORT in _DSN,
    reason="set VEER_TEST_PG_DSN to a SANDBOX store (001-014); never the live :56434 store",
)


class _TxPool:
    """One connection inside an OUTER transaction: every PartitionManager
    transaction becomes a savepoint, and the outer one is rolled back."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self):
        return _Ctx(self._conn)


async def _rolled_back(fn):
    import asyncpg

    conn = await asyncpg.connect(_DSN)
    tx = conn.transaction()
    await tx.start()
    try:
        return await fn(conn)
    finally:
        await tx.rollback()  # ALWAYS: nothing persists
        await conn.close()


_INSERT = (
    "INSERT INTO traffic.anomaly_log (segment_id, timestamp_utc, anomaly_type, severity, "
    "details, flagged_at) VALUES ((SELECT min(segment_id) FROM corridor.road_segments), "
    "$1, $2, 'low', '{}'::jsonb, $3) RETURNING anomaly_id"
)


@db
def test_db_purge_removes_only_rows_detected_before_the_cutoff():
    async def scenario(conn):
        now = datetime.now(timezone.utc)
        manager = PartitionManager(_TxPool(conn), retention_days=14,
                                   anomaly_batch_rows=2, anomaly_max_batches=50)
        cutoff = manager.retention_cutoff(now)
        old = cutoff - timedelta(days=3)
        rows = {
            # detected long ago → purged
            "old": await conn.fetchval(_INSERT, old, "speed_zscore", old),
            "old2": await conn.fetchval(_INSERT, old, "speed_jump", old + timedelta(hours=1)),
            # never stamped flagged_at: the observation time stands in
            "old_unflagged": await conn.fetchval(_INSERT, old, "phantom_reading", None),
            # stale_data: OLD observation time, RECENT detection → kept
            "stale_but_recent": await conn.fetchval(_INSERT, old, "stale_data", now),
            "recent": await conn.fetchval(_INSERT, now, "speed_zscore", now),
        }
        report = parts.MaintenanceReport(ran_at="t")
        await manager._purge_anomalies(now, report)
        left = {
            r["anomaly_id"]
            for r in await conn.fetch(
                "SELECT anomaly_id FROM traffic.anomaly_log WHERE anomaly_id = ANY($1::bigint[])",
                list(rows.values()),
            )
        }
        older = await conn.fetchval(
            "SELECT count(*) FROM traffic.anomaly_log "
            "WHERE COALESCE(flagged_at, timestamp_utc) < $1", cutoff,
        )
        return rows, left, report, older

    rows, left, report, older = asyncio.run(_rolled_back(scenario))
    assert left == {rows["stale_but_recent"], rows["recent"]}
    assert report.anomaly_rows_purged >= 3 and report.anomaly_backlog is False
    assert older == 0 and report.errors == []


@db
def test_db_detection_time_index_serves_recent_and_purge():
    """Migration 014's expression index exists on the real schema and is
    usable by GET /anomalies/recent (range + order, no sort) and by the
    purge's batch selection."""

    async def scenario(conn):
        idx = await conn.fetchval(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'traffic' "
            "AND indexname = 'idx_anomaly_log_detected'"
        )
        if idx is None:
            return None, None, None
        # Penalise every alternative (seq / bitmap scans, explicit sorts):
        # the plans below then avoid them exactly when the index can serve
        # the range AND the order — whatever the table's current size.
        for knob in ("enable_seqscan", "enable_bitmapscan", "enable_sort"):
            await conn.execute(f"SET LOCAL {knob} = off")
        recent = await conn.fetch(
            "EXPLAIN SELECT anomaly_id, segment_id, timestamp_utc, anomaly_type, severity, "
            "details, COALESCE(flagged_at, timestamp_utc) AS flagged_at FROM traffic.anomaly_log "
            "WHERE COALESCE(flagged_at, timestamp_utc) >= now() - interval '1 hour' "
            "ORDER BY COALESCE(flagged_at, timestamp_utc) DESC, anomaly_id DESC LIMIT 100"
        )
        purge = await conn.fetch(
            "EXPLAIN " + PURGE_ANOMALIES_SQL.replace("$1::timestamptz", "now()")
            .replace("$2", "5000")
        )
        return idx, "\n".join(r[0] for r in recent), "\n".join(r[0] for r in purge)

    idx, recent, purge = asyncio.run(_rolled_back(scenario))
    if idx is None:
        pytest.skip("migration 014 not applied to this sandbox store")
    assert "COALESCE(flagged_at, timestamp_utc) DESC, anomaly_id DESC" in idx
    assert "idx_anomaly_log_detected" in recent and "Sort" not in recent
    # The batch SELECTION walks the detection-time index in order (range +
    # order from the index: no sort, no scan of the whole log).  Which index
    # the outer DELETE probes by anomaly_id (the primary key, or this
    # index's second key) is the planner's call at a sandbox table's size.
    assert re.search(
        r"Index Scan(?: Backward)? using idx_anomaly_log_detected[^\n]*\n\s+"
        r"Index Cond: \(COALESCE\(flagged_at, timestamp_utc\) <",
        purge,
    ), purge
    assert "Sort" not in purge and "Seq Scan" not in purge, purge


@db
def test_db_014_applies_to_a_pre_014_store_and_reapplies_as_a_no_op():
    """Inside a rolled-back transaction: remove what 014 creates (the
    pre-014 store a deploy meets), apply 014's statements, then apply them
    again — the second pass changes nothing — and find both objects."""

    async def scenario(conn):
        await conn.execute("DROP INDEX IF EXISTS traffic.idx_anomaly_log_detected")
        await conn.execute("ALTER TABLE corridor.dispatch_events DROP COLUMN IF EXISTS detail")
        snapshots = []
        for _ in range(2):
            for stmt in _statements(M014.read_text()):
                await conn.execute(stmt)
            snapshots.append((
                await conn.fetchval(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = 'traffic' "
                    "AND indexname = 'idx_anomaly_log_detected'"
                ),
                await conn.fetchval(
                    "SELECT data_type FROM information_schema.columns WHERE "
                    "table_schema = 'corridor' AND table_name = 'dispatch_events' "
                    "AND column_name = 'detail'"
                ),
            ))
        return snapshots

    first, second = asyncio.run(_rolled_back(scenario))
    assert first == second  # idempotent: the re-apply is a no-op
    idx, column = first
    assert "COALESCE(flagged_at, timestamp_utc) DESC, anomaly_id DESC" in idx
    assert column == "jsonb"
