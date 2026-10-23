"""traffic.observations partition + retention management (no pg_partman).

001 created ``traffic.observations`` PARTITION BY RANGE (timestamp_utc) with
only a DEFAULT partition and a comment promising pg_partman, which is not
installed.  Without this module every live row lands in
``observations_default`` and a later CREATE PARTITION over a range already
holding default rows FAILS.  Run at startup (before the consumer
subscribes) and then hourly by the ingest service:

1. **Create** daily UTC partitions ``traffic.observations_YYYY_MM_DD`` for
   today .. today+``days_ahead``.  A day already covered by any partition
   (whatever its name — e.g. the seed's ``observations_2026_10_13``) is
   skipped.  Rows already parked in DEFAULT for that day are moved into the
   new partition inside the same transaction, so creation never trips over
   them.  The parent is locked ACCESS EXCLUSIVE up front for those few
   milliseconds (CREATE ... PARTITION OF needs that lock anyway; taking it
   first means a concurrent insert can never hold the parent while waiting
   on DEFAULT — no lock-upgrade deadlock), bounded by ``lock_timeout``.
2. **Retain**: detach + drop daily partitions whose upper bound is older
   than ``retention_days``; purge DEFAULT rows older than the same cutoff.
3. **Purge anomalies**: delete ``traffic.anomaly_log`` rows detected
   (``COALESCE(flagged_at, timestamp_utc)``) before the same retention
   cutoff, oldest first, in bounded batches — ``anomaly_batch_rows`` per
   transaction, at most ``anomaly_max_batches`` per pass, each batch under
   the advisory lock — so a backlog drains over a few hourly passes
   instead of one long-locking DELETE.  Migration 014's detection-time
   index serves the batch selection.
4. **Roll up** the previous complete UTC hour into
   ``traffic.hourly_aggregates`` (idempotent upsert).  Only
   ``data_quality = 'real'`` rows are aggregated: anomalous rows are flagged
   untrustworthy and synthetic (emulated showcase) rows must not leak into
   the historical baseline that /traffic/historical and the twin's replay
   calibration read.
5. **Roll up the synthetic plane** into its OWN table,
   ``traffic.hourly_aggregates_synthetic`` (migration 019, probed — a
   pre-019 store simply skips this step): the same hour from
   ``data_quality = 'synthetic'`` rows only, so /traffic/historical can
   show the simulated feed's history LABELLED, beside the real aggregates
   and never merged into them.  Nothing that calibrates reads it.  The
   FIRST pass after start also catches up the synthetic hours missing
   inside the retention window (at most :data:`SYNTHETIC_CATCHUP_HOURS`,
   newest first, bounded by :data:`SYNTHETIC_CATCHUP_BUDGET_SEC` so the
   startup pass stays inside the ingest service's startup bound; whatever
   is left is continued by the next pass).  Synthetic rollup rows older
   than the retention cutoff are deleted in the same step.

Every DDL / retention step takes a transaction-scoped advisory lock, so two
oracle instances on one database never race the DDL or the purge.
Partition names and bounds are derived from dates only — no external input
reaches the DDL text.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import asyncpg
import structlog

logger = structlog.get_logger(__name__)

#: pg_advisory_xact_lock key for observation-partition maintenance.
_ADVISORY_LOCK_KEY = 0x0B5E_7A71

_PARENT = "traffic.observations"
_DEFAULT_PARTITION = "traffic.observations_default"
_SAFE_IDENT_RE = re.compile(r"^[a-z0-9_]+$")

#: Column list moved between DEFAULT and a new partition (explicit, never *).
_COLUMNS = (
    "observation_id, segment_id, timestamp_utc, speed_kmh, congestion_idx, "
    "source, geom, data_quality, confidence"
)

LIST_PARTITIONS_SQL = """
SELECT c.relname AS name,
       c.relispartition AND pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT' AS is_default,
       (regexp_match(pg_get_expr(c.relpartbound, c.oid),
                     'FROM \\(''([^'']+)''\\)'))[1]::timestamptz AS lower_bound,
       (regexp_match(pg_get_expr(c.relpartbound, c.oid),
                     'TO \\(''([^'']+)''\\)'))[1]::timestamptz AS upper_bound
FROM pg_inherits i
JOIN pg_class c ON c.oid = i.inhrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE i.inhparent = 'traffic.observations'::regclass
  AND n.nspname = 'traffic'
ORDER BY lower_bound NULLS FIRST
"""

#: One bounded retention batch: the oldest ``$2`` anomaly rows detected
#: before ``$1`` (the detection-time index picks them, the primary key
#: deletes them — no sequential scan of the log).
PURGE_ANOMALIES_SQL = """
DELETE FROM traffic.anomaly_log
WHERE anomaly_id = ANY (ARRAY(
    SELECT anomaly_id FROM traffic.anomaly_log
    WHERE COALESCE(flagged_at, timestamp_utc) < $1::timestamptz
    ORDER BY COALESCE(flagged_at, timestamp_utc)
    LIMIT $2
))
"""

#: Anomaly-log retention batch size and per-pass batch budget.
ANOMALY_PURGE_BATCH_ROWS = 5000
ANOMALY_PURGE_MAX_BATCHES = 20

ROLLUP_HOUR_SQL = """
INSERT INTO traffic.hourly_aggregates
    (segment_id, hour_utc, avg_speed_kmh, p50_speed_kmh, p95_congestion,
     observation_cnt)
SELECT segment_id,
       $1::timestamptz,
       AVG(speed_kmh),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY speed_kmh),
       percentile_cont(0.95) WITHIN GROUP (ORDER BY congestion_idx),
       COUNT(*)
FROM traffic.observations
WHERE timestamp_utc >= $1::timestamptz
  AND timestamp_utc < $1::timestamptz + interval '1 hour'
  AND data_quality = 'real'
GROUP BY segment_id
ON CONFLICT (segment_id, hour_utc) DO UPDATE SET
    avg_speed_kmh = EXCLUDED.avg_speed_kmh,
    p50_speed_kmh = EXCLUDED.p50_speed_kmh,
    p95_congestion = EXCLUDED.p95_congestion,
    observation_cnt = EXCLUDED.observation_cnt
"""

#: The synthetic plane's own rollup table (migration 019) — never a
#: calibration input; probed before use.
SYNTHETIC_TABLE = "traffic.hourly_aggregates_synthetic"

#: The same hour from SIMULATED-feed rows only, into the synthetic table.
ROLLUP_SYNTHETIC_HOUR_SQL = """
INSERT INTO traffic.hourly_aggregates_synthetic
    (segment_id, hour_utc, avg_speed_kmh, p50_speed_kmh, p95_congestion,
     observation_cnt)
SELECT segment_id,
       $1::timestamptz,
       AVG(speed_kmh),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY speed_kmh),
       percentile_cont(0.95) WITHIN GROUP (ORDER BY congestion_idx),
       COUNT(*)
FROM traffic.observations
WHERE timestamp_utc >= $1::timestamptz
  AND timestamp_utc < $1::timestamptz + interval '1 hour'
  AND data_quality = 'synthetic'
GROUP BY segment_id
ON CONFLICT (segment_id, hour_utc) DO UPDATE SET
    avg_speed_kmh = EXCLUDED.avg_speed_kmh,
    p50_speed_kmh = EXCLUDED.p50_speed_kmh,
    p95_congestion = EXCLUDED.p95_congestion,
    observation_cnt = EXCLUDED.observation_cnt
"""

#: Hours already rolled up into the synthetic table inside a window.
SYNTHETIC_HOURS_SQL = (
    "SELECT DISTINCT hour_utc FROM traffic.hourly_aggregates_synthetic "
    "WHERE hour_utc >= $1::timestamptz AND hour_utc < $2::timestamptz"
)

#: Synthetic rollup rows past the retention cutoff.
PURGE_SYNTHETIC_SQL = (
    "DELETE FROM traffic.hourly_aggregates_synthetic WHERE hour_utc < $1::timestamptz"
)

#: Probe for migration 019.
SYNTHETIC_TABLE_EXISTS_SQL = "SELECT to_regclass('traffic.hourly_aggregates_synthetic') IS NOT NULL"  # noqa: E501

#: Startup catch-up: at most this many missing synthetic hours (7 days) ...
SYNTHETIC_CATCHUP_HOURS = 7 * 24
#: ... and at most this much wall time per pass (the ingest service bounds
#: its whole startup maintenance pass at 60 s); the rest continues next pass.
SYNTHETIC_CATCHUP_BUDGET_SEC = 20.0


def partition_name(day: date) -> str:
    """``observations_YYYY_MM_DD`` (unqualified) for a UTC day."""
    return f"observations_{day:%Y_%m_%d}"


def _day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _iso_z(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _literal(ts: datetime) -> str:
    """Timestamptz literal for partition DDL (bounds cannot be bind params)."""
    return "'" + ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00") + "'"


@dataclass
class MaintenanceReport:
    """What one maintenance pass did (surfaced on /health)."""

    ran_at: str
    created: list[str] = field(default_factory=list)
    rows_moved_from_default: int = 0
    dropped: list[str] = field(default_factory=list)
    default_rows_purged: int = 0
    anomaly_rows_purged: int = 0
    # True when the batch budget ran out with old anomaly rows left (the
    # next hourly pass continues), False when drained; None when the pass
    # reached no verdict (a peer instance held the maintenance lock).
    anomaly_backlog: bool | None = None
    rollup_hour: str | None = None
    rollup_rows: int = 0
    # synthetic plane (019): None = table absent (pre-019 store)
    synthetic_rollup_rows: int | None = None
    synthetic_catchup_hours: int = 0
    synthetic_catchup_rows: int = 0
    synthetic_catchup_pending: int = 0
    synthetic_rows_purged: int = 0
    skipped_locked: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ran_at": self.ran_at,
            "created": list(self.created),
            "rows_moved_from_default": self.rows_moved_from_default,
            "dropped": list(self.dropped),
            "default_rows_purged": self.default_rows_purged,
            "anomaly_rows_purged": self.anomaly_rows_purged,
            "anomaly_backlog": self.anomaly_backlog,
            "rollup_hour": self.rollup_hour,
            "rollup_rows": self.rollup_rows,
            "synthetic_rollup": {
                "table_present": self.synthetic_rollup_rows is not None,
                "rollup_rows": self.synthetic_rollup_rows,
                "catchup_hours": self.synthetic_catchup_hours,
                "catchup_rows": self.synthetic_catchup_rows,
                "catchup_pending_hours": self.synthetic_catchup_pending,
                "rows_purged": self.synthetic_rows_purged,
                "calibration_input": False,
            },
            "skipped_locked": self.skipped_locked,
            "errors": list(self.errors),
        }


class PartitionManager:
    """Creates, retains and rolls up ``traffic.observations`` partitions."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        days_ahead: int = 2,
        retention_days: int = 14,
        lock_timeout_ms: int = 3000,
        anomaly_batch_rows: int = ANOMALY_PURGE_BATCH_ROWS,
        anomaly_max_batches: int = ANOMALY_PURGE_MAX_BATCHES,
    ) -> None:
        if retention_days < 1:
            raise ValueError("retention_days must be >= 1")
        if anomaly_batch_rows < 1 or anomaly_max_batches < 1:
            raise ValueError("anomaly purge batches must be >= 1 row and >= 1 batch")
        self._pool = pool
        self._days_ahead = max(0, int(days_ahead))
        self._retention_days = int(retention_days)
        self._lock_timeout_ms = int(lock_timeout_ms)
        self._anomaly_batch_rows = int(anomaly_batch_rows)
        self._anomaly_max_batches = int(anomaly_max_batches)
        self.last_report: MaintenanceReport | None = None
        #: synthetic hours still owed by the bounded catch-up (None until
        #: the first pass has scanned the window)
        self._synthetic_owed: list[datetime] | None = None
        self._synthetic_catchup_hours = SYNTHETIC_CATCHUP_HOURS
        self._synthetic_catchup_budget_sec = SYNTHETIC_CATCHUP_BUDGET_SEC

    def retention_cutoff(self, now: datetime) -> datetime:
        """Start of the oldest UTC day kept: observations partitions ending
        at or before it are dropped, anomaly rows detected before it purged."""
        return _day_start(now.astimezone(timezone.utc).date()) - timedelta(
            days=self._retention_days
        )

    async def run(self, now: datetime | None = None) -> MaintenanceReport:
        """One full maintenance pass; never raises (errors are reported)."""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        report = MaintenanceReport(ran_at=_iso_z(now))
        for step in (
            self._ensure_partitions,
            self._drop_expired,
            self._purge_anomalies,
            self._rollup,
            self._rollup_synthetic,
        ):
            try:
                await step(now, report)
            except Exception as exc:  # noqa: BLE001 — each step isolated
                # Class name only: the report is served on /health; the
                # driver message (SQL, relation names) goes to the log.
                report.errors.append(f"{step.__name__}: {type(exc).__name__}")
                logger.warning(
                    "ingest.partitions.step_failed",
                    step=step.__name__,
                    error=f"{type(exc).__name__}: {exc}",
                )
        self.last_report = report
        logger.info(
            "ingest.partitions.maintenance",
            created=report.created,
            moved=report.rows_moved_from_default,
            dropped=report.dropped,
            purged=report.default_rows_purged,
            anomalies_purged=report.anomaly_rows_purged,
            anomaly_backlog=report.anomaly_backlog,
            rollup_hour=report.rollup_hour,
            rollup_rows=report.rollup_rows,
            synthetic_rollup_rows=report.synthetic_rollup_rows,
            synthetic_catchup_hours=report.synthetic_catchup_hours,
            synthetic_catchup_pending=report.synthetic_catchup_pending,
            errors=len(report.errors),
        )
        return report

    async def _locked(self, conn: asyncpg.Connection) -> bool:
        await conn.execute(f"SET LOCAL lock_timeout = {self._lock_timeout_ms}")
        return bool(
            await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", _ADVISORY_LOCK_KEY)
        )

    async def _ensure_partitions(self, now: datetime, report: MaintenanceReport) -> None:
        today = now.date()
        wanted = [today + timedelta(days=i) for i in range(self._days_ahead + 1)]
        async with self._pool.acquire() as conn:
            for day in wanted:
                lo, hi = _day_start(day), _day_start(day + timedelta(days=1))
                async with conn.transaction():
                    if not await self._locked(conn):
                        report.skipped_locked = True
                        return
                    rows = await conn.fetch(LIST_PARTITIONS_SQL)
                    if any(
                        r["lower_bound"] is not None
                        and r["lower_bound"] < hi
                        and r["upper_bound"] > lo
                        for r in rows
                    ):
                        continue  # day already covered by some partition
                    has_default = any(r["is_default"] for r in rows)
                    moved = 0
                    await conn.execute(f"LOCK TABLE {_PARENT} IN ACCESS EXCLUSIVE MODE")
                    if has_default:
                        # Park rows already in DEFAULT for this day, create
                        # the partition, then route them back through the
                        # parent into it — all in one transaction.
                        await conn.execute(
                            "CREATE TEMP TABLE _obs_partition_move "
                            "(LIKE traffic.observations) ON COMMIT DROP"
                        )
                        moved = _status_count(
                            await conn.execute(
                                f"WITH moved AS (DELETE FROM {_DEFAULT_PARTITION} "
                                f"WHERE timestamp_utc >= $1 AND timestamp_utc < $2 "
                                f"RETURNING {_COLUMNS}) "
                                f"INSERT INTO _obs_partition_move ({_COLUMNS}) "
                                f"SELECT {_COLUMNS} FROM moved",
                                lo,
                                hi,
                            )
                        )
                    name = partition_name(day)
                    await conn.execute(
                        f"CREATE TABLE traffic.{name} PARTITION OF {_PARENT} "
                        f"FOR VALUES FROM ({_literal(lo)}) TO ({_literal(hi)})"
                    )
                    if moved:
                        await conn.execute(
                            f"INSERT INTO {_PARENT} ({_COLUMNS}) "
                            f"SELECT {_COLUMNS} FROM _obs_partition_move"
                        )
                    if has_default:
                        # ON COMMIT DROP only fires at a top-level commit;
                        # drop explicitly so the next day's block (or a
                        # caller's enclosing transaction) can recreate it.
                        await conn.execute("DROP TABLE _obs_partition_move")
                report.created.append(f"traffic.{name}")
                report.rows_moved_from_default += moved

    async def _drop_expired(self, now: datetime, report: MaintenanceReport) -> None:
        cutoff = self.retention_cutoff(now)
        async with self._pool.acquire() as conn, conn.transaction():
            if not await self._locked(conn):
                report.skipped_locked = True
                return
            rows = await conn.fetch(LIST_PARTITIONS_SQL)
            for r in rows:
                if r["is_default"] or r["upper_bound"] is None:
                    continue
                if r["upper_bound"] > cutoff:
                    continue
                name = r["name"]
                if not _SAFE_IDENT_RE.match(name):
                    # Catalog names are trusted, but DDL text is only
                    # ever built from plain identifiers.
                    report.errors.append(f"skipped unsafe partition name {name!r}")
                    continue
                await conn.execute(
                    f"ALTER TABLE {_PARENT} DETACH PARTITION traffic.{name}"
                )
                await conn.execute(f"DROP TABLE traffic.{name}")
                report.dropped.append(f"traffic.{name}")
            if any(r["is_default"] for r in rows):
                report.default_rows_purged += _status_count(
                    await conn.execute(
                        f"DELETE FROM {_DEFAULT_PARTITION} WHERE timestamp_utc < $1",
                        cutoff,
                    )
                )

    async def _purge_anomalies(self, now: datetime, report: MaintenanceReport) -> None:
        """Delete anomaly_log rows detected before the retention cutoff.

        Oldest first, ``anomaly_batch_rows`` per transaction (each under
        the advisory lock, so a peer instance's pass waits its turn rather
        than racing), at most ``anomaly_max_batches`` per pass: a backlog
        drains over successive hourly passes and is reported, never
        swept in one long-locking statement.
        """
        cutoff = self.retention_cutoff(now)
        async with self._pool.acquire() as conn:
            for _ in range(self._anomaly_max_batches):
                async with conn.transaction():
                    if not await self._locked(conn):
                        report.skipped_locked = True
                        return
                    deleted = _status_count(
                        await conn.execute(
                            PURGE_ANOMALIES_SQL, cutoff, self._anomaly_batch_rows
                        )
                    )
                report.anomaly_rows_purged += deleted
                if deleted < self._anomaly_batch_rows:
                    report.anomaly_backlog = False
                    return
        # Every batch came back full: older rows may remain for the next pass.
        report.anomaly_backlog = True

    async def _rollup(self, now: datetime, report: MaintenanceReport) -> None:
        hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        async with self._pool.acquire() as conn:
            status = await conn.execute(ROLLUP_HOUR_SQL, hour)
        report.rollup_hour = _iso_z(hour)
        report.rollup_rows = _status_count(status)

    async def _rollup_synthetic(self, now: datetime, report: MaintenanceReport) -> None:
        """The previous hour of the SIMULATED feed into its own table, the
        bounded catch-up of missing hours, and the same retention cutoff.

        Skipped (``table_present: false`` on /health) on a store without
        migration 019.  The real rollup above is untouched by any of it.
        """
        hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        cutoff = self.retention_cutoff(now)
        async with self._pool.acquire() as conn:
            if not await conn.fetchval(SYNTHETIC_TABLE_EXISTS_SQL):
                return
            report.synthetic_rollup_rows = _status_count(
                await conn.execute(ROLLUP_SYNTHETIC_HOUR_SQL, hour)
            )
            if self._synthetic_owed is None:
                # first pass after start: every complete hour inside the
                # retention window (at most 7 days) that has no row yet
                start = max(cutoff, hour - timedelta(hours=self._synthetic_catchup_hours))
                have = {
                    r["hour_utc"].astimezone(timezone.utc)
                    for r in await conn.fetch(SYNTHETIC_HOURS_SQL, start, hour)
                }
                owed: list[datetime] = []
                h = hour - timedelta(hours=1)
                while h >= start:
                    if h not in have:
                        owed.append(h)  # newest first
                    h -= timedelta(hours=1)
                self._synthetic_owed = owed
            deadline = time.monotonic() + self._synthetic_catchup_budget_sec
            owed = self._synthetic_owed
            while owed and time.monotonic() < deadline:
                missing = owed.pop(0)
                if missing < cutoff:
                    continue  # aged out of the window while owed
                report.synthetic_catchup_rows += _status_count(
                    await conn.execute(ROLLUP_SYNTHETIC_HOUR_SQL, missing)
                )
                report.synthetic_catchup_hours += 1
            report.synthetic_catchup_pending = len(owed)
            report.synthetic_rows_purged = _status_count(
                await conn.execute(PURGE_SYNTHETIC_SQL, cutoff)
            )


def _status_count(status: object) -> int:
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


def seconds_until_next_run(now: datetime, offset_sec: int = 90) -> float:
    """Seconds until ``offset_sec`` past the next UTC hour (rollup cadence).

    The rollup covers the PREVIOUS complete hour, so running a little after
    the boundary lets the observation buffer (5 s flush) land first.
    """
    now = now.astimezone(timezone.utc)
    nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(seconds=offset_sec)
    if nxt <= now:
        nxt += timedelta(hours=1)
    return max(1.0, (nxt - now).total_seconds())


__all__ = [
    "ANOMALY_PURGE_BATCH_ROWS",
    "ANOMALY_PURGE_MAX_BATCHES",
    "LIST_PARTITIONS_SQL",
    "MaintenanceReport",
    "PURGE_ANOMALIES_SQL",
    "PartitionManager",
    "ROLLUP_HOUR_SQL",
    "partition_name",
    "seconds_until_next_run",
]
