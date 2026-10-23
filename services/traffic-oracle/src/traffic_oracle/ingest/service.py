"""Live data-plane service: NATS → normalise → Valkey hot cache + PostGIS.

Started from the oracle lifespan (``__main__.py``) once the asyncpg pool and
the Valkey clients exist, and drained before the situation engine stops.
One supervised NATS connection (``Settings.NATS_URL``; retried forever, so
NATS may come up after the oracle) carries four subscriptions:

    corridor.traffic.live       signal-ingress Arrow IPC (utf8 ids, uint8 ordinals)
    corridor.traffic.emulated   event-emulator Arrow IPC (int64 ids, int8 ordinals),
                                persisted with data_quality 'synthetic'
    corridor.traffic.anomaly    Tier-1 anomaly JSON (contract C-H) → anomaly_log
    corridor.units.emulated     emulated agency units (contract C-A) → Valkey

Per traffic message: ArrowDecoder → normalize_batch → CacheWriter
(``traffic:latest:{sid}``, TTL 300 s — what POST /api/v1/traffic/live and
the flow forecaster read) + the DbWriter buffer (one set-based INSERT every
5 s or 2000 rows).  Partition maintenance (ingest/partitions.py) runs
BEFORE the first subscription and then hourly.  Every stage is counted and
surfaced on ``/health`` → ``ingest``: a dark data plane must be visible,
never silent.  ``last_msg_age_sec`` there is the LIVE subject's freshness
only — a running emulator never makes a dead signal-ingress look fresh
(``emulated_last_msg_age_sec`` reports the emulator separately) — and error
fields carry exception class names only (full text goes to the log).
Message handlers never raise: a failure is counted per subject
(``callback_errors``) instead of reaching nats-py's ``error_cb``, where it
would read as a connection error.  Emulated subjects can be switched off
with ``VVIP_INGEST_EMULATED=false``; nothing emulated is ever relabelled
live.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import structlog

from traffic_oracle.anomaly.stream_detector import SUBJECT_ANOMALY, AnomalyStreamConsumer
from traffic_oracle.data.cache import TrafficCache
from traffic_oracle.data.models import DataQuality
from traffic_oracle.ingest.arrow_decoder import ArrowDecoder
from traffic_oracle.ingest.cache_writer import CacheWriter
from traffic_oracle.ingest.db_writer import DbWriter
from traffic_oracle.ingest.nats_consumer import SUBJECT_EMULATED, SUBJECT_LIVE, NatsConnection
from traffic_oracle.ingest.normalize import ORIGIN_EMULATED, normalize_batch
from traffic_oracle.ingest.partitions import PartitionManager, seconds_until_next_run
from traffic_oracle.ingest.units_consumer import (
    REPLAY_HEADER,
    SUBJECT_UNITS_EMULATED,
    UnitsConsumer,
)
from traffic_oracle.metrics import (
    INGEST_MESSAGES_TOTAL,
    INGEST_ROWS_DROPPED_TOTAL,
    OBSERVATIONS_PROCESSED,
)

logger = structlog.get_logger(__name__)

#: Accepted observation timestamp window: rows stamped more than 10 min in
#: the future (clock skew beyond reason — Tier-1 flags them) or older than
#: the retention window never reach the cache or the DB.
MAX_FUTURE_MS = 10 * 60 * 1000
_RATE_WINDOW_SEC = 60.0
_WRITTEN_WINDOW_SEC = 300.0
_CACHE_KEYS_TTL_SEC = 10.0
_PARTITION_STARTUP_TIMEOUT_SEC = 60.0
_LOG_EVERY_SEC = 30.0


@dataclass
class SubjectStats:
    """Per-subject message accounting for /health."""

    messages: int = 0
    rows: int = 0
    errors: int = 0
    replays: int = 0
    last_msg_monotonic: float | None = None
    last_msg_t: str | None = None
    recent: deque = field(default_factory=deque)

    def touch(self, now: float) -> None:
        self.messages += 1
        self.last_msg_monotonic = now
        self.last_msg_t = (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            )
        )
        self.recent.append(now)
        self.prune(now)

    def prune(self, now: float) -> None:
        while self.recent and now - self.recent[0] > _RATE_WINDOW_SEC:
            self.recent.popleft()

    def snapshot(self, now: float) -> dict:
        self.prune(now)
        return {
            "messages": self.messages,
            "msgs_per_min": len(self.recent),
            "last_msg_age_sec": (
                round(now - self.last_msg_monotonic, 1)
                if self.last_msg_monotonic is not None
                else None
            ),
            "last_msg_t": self.last_msg_t,
            "rows": self.rows,
            "errors": self.errors,
            "replays": self.replays,
        }


class IngestService:
    """Supervised NATS consumer feeding the Valkey hot cache and PostGIS."""

    def __init__(
        self,
        *,
        nats_url: str,
        pool: Any,
        cache: TrafficCache,
        valkey_client: Any,
        flush_interval_sec: float = 5.0,
        flush_rows: int = 2000,
        retention_days: int = 14,
        emulated: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._cache = cache
        self._connection = NatsConnection(nats_url)
        self._db_writer = DbWriter(
            pool, flush_interval_sec=flush_interval_sec, flush_rows=flush_rows
        )
        self._cache_writer = CacheWriter(cache)
        self._anomalies = AnomalyStreamConsumer(pool)
        self._units = UnitsConsumer(valkey_client)
        self._partitions = PartitionManager(pool, retention_days=retention_days)
        self._max_age_ms = int(retention_days) * 86_400_000
        self._emulated = emulated
        self.subjects: list[str] = [SUBJECT_LIVE, SUBJECT_ANOMALY]
        if emulated:
            self.subjects[1:1] = [SUBJECT_EMULATED]
            self.subjects.append(SUBJECT_UNITS_EMULATED)
        self._stats: dict[str, SubjectStats] = {s: SubjectStats() for s in self.subjects}
        self.dropped: Counter = Counter()
        # Writer-side drop counters already folded into ``dropped``.
        self._db_dropped_seen: Counter = Counter()
        self._anomaly_dropped_seen: Counter = Counter()
        self._written: deque[tuple[float, int]] = deque()
        self._cache_errors = 0
        self._callback_errors = 0
        self._flush_errors = 0
        self._last_flush_error: str | None = None
        self._last_log: dict[str, float] = {}
        self._cache_keys: tuple[float, int | None] | None = None
        self._stopping = asyncio.Event()
        self._flush_now = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._flusher_task: asyncio.Task | None = None
        self._started_monotonic: float | None = None
        self._subscribed = False

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Launch the supervisor task (idempotent)."""
        if any(not t.done() for t in self._tasks):
            return
        self._stopping.clear()
        self._started_monotonic = self._clock()
        self._tasks = [asyncio.create_task(self._supervise(), name="ingest-supervisor")]
        logger.info(
            "ingest.service.started",
            nats=self._connection.url,
            subjects=self.subjects,
        )

    async def stop(self, timeout: float = 10.0) -> None:
        """Drain NATS, stop loops, flush buffers — bounded by ``timeout``.

        Order matters: NATS is drained first (in-flight callbacks finish
        buffering), then the flusher is WOKEN and awaited — never cancelled
        mid-INSERT, which could drop its swapped-out batch — and a final
        flush picks up anything left.
        """
        self._stopping.set()
        await self._connection.close(timeout=min(5.0, timeout))
        flusher, self._flusher_task = self._flusher_task, None
        if flusher is not None and not flusher.done():
            self._flush_now.set()
            try:
                await asyncio.wait_for(asyncio.shield(flusher), timeout=timeout)
            except Exception:  # noqa: BLE001 — stuck (DB hung): bounded shutdown wins
                flusher.cancel()
        for task in self._tasks:
            if task is not flusher:
                task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        self._subscribed = False
        try:
            await asyncio.wait_for(self._flush_once(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — shutdown must not raise
            logger.warning(
                "ingest.service.final_flush_failed",
                error=f"{type(exc).__name__}: {exc}",
                pending_rows=self._db_writer.pending_rows,
            )
        logger.info("ingest.service.stopped", rows_written=self._db_writer.rows_written_total)

    async def _supervise(self) -> None:
        # 1. Partitions BEFORE the consumer: today's rows must land in a
        #    real daily partition, not DEFAULT.  Bounded — a slow DB delays
        #    ingest, never blocks it forever.
        try:
            await asyncio.wait_for(
                self._partitions.run(), timeout=_PARTITION_STARTUP_TIMEOUT_SEC
            )
        except Exception as exc:  # noqa: BLE001 — logged, ingest proceeds
            logger.warning(
                "ingest.partitions.startup_failed", error=f"{type(exc).__name__}: {exc}"
            )
        self._flusher_task = asyncio.create_task(self._flusher(), name="ingest-flusher")
        self._tasks.append(self._flusher_task)
        self._tasks.append(
            asyncio.create_task(self._maintenance_loop(), name="ingest-partitions")
        )
        # 2. Connect + subscribe; re-establish if the client ever closes.
        while not self._stopping.is_set():
            nc = await self._connection.connect(self._stopping)
            if nc is None:
                return
            try:
                await self._subscribe(nc)
            except Exception as exc:  # noqa: BLE001 — retried after close
                logger.warning(
                    "ingest.service.subscribe_failed", error=f"{type(exc).__name__}: {exc}"
                )
                await self._connection.close()
                await asyncio.sleep(1.0)
                continue
            stop_wait = asyncio.create_task(self._stopping.wait())
            closed_wait = asyncio.create_task(self._connection.closed.wait())
            done, pending = await asyncio.wait(
                {stop_wait, closed_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            self._subscribed = False
            if self._stopping.is_set():
                return
            logger.warning("ingest.service.connection_closed_reconnecting")
            await asyncio.sleep(1.0)

    async def _subscribe(self, nc: Any) -> None:
        handlers = {
            SUBJECT_LIVE: self._on_traffic,
            SUBJECT_EMULATED: self._on_traffic,
            SUBJECT_ANOMALY: self._on_anomaly,
            SUBJECT_UNITS_EMULATED: self._on_unit,
        }
        for subject in self.subjects:
            await nc.subscribe(
                subject,
                cb=handlers[subject],
                pending_msgs_limit=8192,
                pending_bytes_limit=64 * 1024 * 1024,
            )
        self._subscribed = True
        logger.info("subscribed to NATS subjects", subjects=self.subjects)

    # -- message handlers ----------------------------------------------------------

    def _count_drops(self, stage: str, reasons: Counter) -> None:
        for reason, n in reasons.items():
            if n:
                self.dropped[f"{stage}:{reason}"] += n
                INGEST_ROWS_DROPPED_TOTAL.labels(stage=stage, reason=reason).inc(n)

    def _log_limited(self, event: str, **kw: Any) -> None:
        now = self._clock()
        if now - self._last_log.get(event, -1e9) >= _LOG_EVERY_SEC:
            self._last_log[event] = now
            logger.warning(event, **kw)

    def _received(self, msg: Any) -> SubjectStats:
        stats = self._stats[msg.subject]
        stats.touch(self._clock())
        INGEST_MESSAGES_TOTAL.labels(subject=msg.subject).inc()
        if msg.headers and REPLAY_HEADER in msg.headers:
            stats.replays += 1
        return stats

    def _callback_failed(self, msg: Any, exc: Exception) -> None:
        """Count a handler failure on its subject (handlers never raise)."""
        self._callback_errors += 1
        stats = self._stats.get(getattr(msg, "subject", ""))
        if stats is not None:
            stats.errors += 1
        self._log_limited(
            "ingest.callback_failed",
            subject=getattr(msg, "subject", None),
            error=f"{type(exc).__name__}: {exc}",
        )

    async def _on_traffic(self, msg: Any) -> None:
        try:
            stats = self._received(msg)
            await self.ingest_traffic_payload(msg.subject, msg.data, stats)
        except Exception as exc:  # noqa: BLE001 — never into nats error_cb
            self._callback_failed(msg, exc)

    async def ingest_traffic_payload(
        self, subject: str, data: bytes, stats: SubjectStats | None = None
    ) -> int:
        """Decode + normalise one Arrow IPC payload; cache it and buffer it.

        Returns the number of canonical rows accepted (0 on a dropped
        payload).  Never raises: failures are counted per subject.
        """
        stats = stats or self._stats.get(subject) or SubjectStats()
        try:
            decoded = ArrowDecoder.decode(data)
        except Exception as exc:  # noqa: BLE001 — a corrupt payload is counted
            stats.errors += 1
            self._count_drops("decode", Counter({"invalid_arrow_ipc": 1}))
            self._log_limited(
                "ingest.traffic.decode_failed",
                subject=subject,
                error=f"{type(exc).__name__}: {exc}",
            )
            return 0
        now_ms = int(time.time() * 1000)
        emulated = subject == SUBJECT_EMULATED
        batch = normalize_batch(
            decoded,
            force_quality=DataQuality.SYNTHETIC if emulated else None,
            # the plane is named explicitly: the emulated cache plane
            # (traffic:emulated:latest:{sid}) never rests on normalize's
            # forced-quality => 'emulated' inference
            origin=ORIGIN_EMULATED if emulated else None,
            now_ms=now_ms,
            max_future_ms=MAX_FUTURE_MS,
            max_age_ms=self._max_age_ms,
        )
        self._count_drops("normalize", batch.dropped)
        if batch.rows == 0:
            return 0
        stats.rows += batch.rows
        try:
            await self._cache_writer.update_from_batch(batch.frame, now_ms=now_ms)
        except Exception as exc:  # noqa: BLE001 — DB write still proceeds
            self._cache_errors += 1
            self._log_limited(
                "ingest.traffic.cache_failed", error=f"{type(exc).__name__}: {exc}"
            )
        self._db_writer.add(batch.frame)
        if self._db_writer.pending_rows >= self._db_writer.flush_rows:
            self._flush_now.set()
        return batch.rows

    async def _on_anomaly(self, msg: Any) -> None:
        try:
            stats = self._received(msg)
            stats.rows += self._anomalies.handle(msg.data)
        except Exception as exc:  # noqa: BLE001 — per-message isolation
            self._callback_failed(msg, exc)

    async def _on_unit(self, msg: Any) -> None:
        try:
            stats = self._received(msg)
            if await self._units.handle(msg.data, headers=msg.headers):
                stats.rows += 1
        except Exception as exc:  # noqa: BLE001 — per-message isolation
            self._callback_failed(msg, exc)

    # -- background loops ------------------------------------------------------------

    async def _flusher(self) -> None:
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._flush_now.wait(), timeout=self._db_writer.flush_interval_sec
                )
            self._flush_now.clear()
            try:
                await self._flush_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — rows stay buffered
                self._flush_errors += 1
                self._last_flush_error = type(exc).__name__
                self._log_limited(
                    "ingest.flush_failed",
                    error=f"{type(exc).__name__}: {exc}",
                    pending_rows=self._db_writer.pending_rows,
                )

    async def _flush_once(self) -> None:
        error: Exception | None = None
        try:
            result = await self._db_writer.flush()
        except Exception as exc:  # noqa: BLE001 — anomalies still flush
            error = exc
        else:
            if result.written:
                self._written.append((self._clock(), result.written))
                OBSERVATIONS_PROCESSED.inc(result.written)
            self._prune_written(self._clock())
        # Fold writer-side drops (unknown_segment at flush, buffer_overflow
        # at add time) into the service counters exactly once.
        current = Counter(self._db_writer.dropped)
        self._count_drops("db", current - self._db_dropped_seen)
        self._db_dropped_seen = current
        try:
            await self._anomalies.flush()
        except Exception as exc:  # noqa: BLE001
            error = error or exc
        # Anomaly rows lost after queueing (unknown segment at insert, buffer
        # overflow during a DB outage) are drops too; parse-time skips stay
        # in the anomalies counters only.
        lost = Counter(
            {k: self._anomalies.counters[k] for k in ("unknown_segment", "buffer_overflow")}
        )
        self._count_drops("anomaly", lost - self._anomaly_dropped_seen)
        self._anomaly_dropped_seen = lost
        if error is not None:
            raise error
        self._last_flush_error = None

    async def _maintenance_loop(self) -> None:
        while not self._stopping.is_set():
            delay = seconds_until_next_run(datetime.now(timezone.utc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            if self._stopping.is_set():
                return
            await self._partitions.run()

    # -- health ------------------------------------------------------------------------

    async def _cache_key_count(self) -> int | None:
        now = self._clock()
        if self._cache_keys is not None and now - self._cache_keys[0] < _CACHE_KEYS_TTL_SEC:
            return self._cache_keys[1]
        try:
            count: int | None = await asyncio.wait_for(
                self._cache.count_latest_keys(), timeout=1.0
            )
        except Exception:  # noqa: BLE001 — health never raises
            count = None
        self._cache_keys = (now, count)
        return count

    def _prune_written(self, now: float) -> None:
        while self._written and now - self._written[0][0] > _WRITTEN_WINDOW_SEC:
            self._written.popleft()

    async def health(self) -> dict:
        """The /health ``ingest`` block (never raises)."""
        now = self._clock()
        self._prune_written(now)
        subjects = {s: st.snapshot(now) for s, st in self._stats.items()}
        connected = self._connection.is_connected
        report = self._partitions.last_report
        return {
            "enabled": True,
            "connected": connected,
            # nats-py re-subscribes after a reconnect; while disconnected
            # nothing is being delivered, so nothing is "subscribed".
            "subscribed": self._subscribed and connected,
            "nats_url": self._connection.url,
            "subjects": subjects,
            "msgs_per_min": sum(v["msgs_per_min"] for v in subjects.values()),
            # Freshness of the LIVE observation plane (signal-ingress); the
            # emulator is reported on its own and never stands in for it.
            "last_msg_age_sec": subjects[SUBJECT_LIVE]["last_msg_age_sec"],
            "emulated_last_msg_age_sec": (
                subjects[SUBJECT_EMULATED]["last_msg_age_sec"]
                if SUBJECT_EMULATED in subjects
                else None
            ),
            "rows_written_5m": sum(n for _, n in self._written),
            "cache_keys": await self._cache_key_count(),
            "rows_written_total": self._db_writer.rows_written_total,
            "pending_rows": self._db_writer.pending_rows,
            "rows_dropped": dict(sorted(self.dropped.items())),
            "flush_errors": self._flush_errors,
            "last_flush_error": self._last_flush_error,
            "cache_errors": self._cache_errors,
            "callback_errors": self._callback_errors,
            "anomalies": {
                "rows_written_total": self._anomalies.rows_written_total,
                "pending_rows": self._anomalies.pending_rows,
                "counters": dict(sorted(self._anomalies.counters.items())),
            },
            "units": {
                "written_total": self._units.written_total,
                "dropped": dict(sorted(self._units.dropped.items())),
            },
            "nats": {
                "disconnects": self._connection.disconnects,
                "reconnects": self._connection.reconnects,
                "connect_errors": self._connection.connect_attempt_errors,
                "slow_consumer_errors": self._connection.slow_consumer_errors,
                "last_error": self._connection.last_error,
            },
            "partitions": report.to_dict() if report is not None else None,
            "uptime_sec": (
                round(now - self._started_monotonic, 1)
                if self._started_monotonic is not None
                else None
            ),
        }


def disabled_health() -> dict:
    """The /health ``ingest`` block when VVIP_INGEST_ENABLED=false."""
    return {
        "enabled": False,
        "connected": False,
        "subscribed": False,
        "subjects": {},
        "msgs_per_min": 0,
        "last_msg_age_sec": None,
        "emulated_last_msg_age_sec": None,
        "rows_written_5m": 0,
        "cache_keys": None,
    }


__all__ = ["IngestService", "MAX_FUTURE_MS", "SubjectStats", "disabled_health"]
