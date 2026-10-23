"""NATS stream consumer for Tier-1 anomaly events.

signal-ingress publishes every Tier-1-flagged observation as JSON on
``corridor.traffic.anomaly`` (normalize/schema_align.rs).  Contract C-H adds
``anomaly_flags: string[]`` drawn from the detector's flag names
(anomaly/flags.rs ``AnomalyFlag::name``), which are exactly the
``traffic.anomaly_log.anomaly_type`` CHECK vocabulary (003) minus the
Tier-2 ``isolation_forest`` type:

    future_timestamp, stale_data, speed_jump, phantom_reading,
    speed_zscore, congestion_outlier, road_class_mismatch, spatial_mismatch

Each valid flag becomes one ``traffic.anomaly_log`` row (severity mirrors
flags.rs ``AnomalyFlag::severity``), limited to ONE row per (segment, flag,
minute of ARRIVAL) — a stuck sensor flagged every second must not flood
the log, and keying on arrival (not the claimed timestamp) keeps a sensor
that repeats one stale or bogus timestamp rate-limited too.  A row's
``timestamp_utc`` is the observation time, clamped to the arrival time for
future-dated claims (the claimed ``timestamp_ms`` stays in ``details``), so
a bogus future reading cannot pin itself to the top of the feed.  The
observation's ``data_quality`` ('Anomalous') is NEVER written as an
anomaly_type (it violates the CHECK); a message without any valid flag
(e.g. from a pre-C-H producer) is counted and skipped, never guessed.
Rows on segments unknown to ``corridor.road_segments`` are filtered by the
insert itself and counted.  Writes are buffered and flushed by the ingest
service as one set-based statement; during a DB outage the buffer holds at
most ``max_pending_rows`` (oldest dropped first, counted as
``buffer_overflow``).  Payloads are parsed as strict JSON — a ``NaN`` /
``Infinity`` constant would be refused by the ``jsonb`` column and stall
every retry of its batch — and ids/timestamps must fit their columns.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from typing import Any, Callable

import asyncpg
import structlog

from traffic_oracle.metrics import ANOMALY_DETECTIONS

logger = structlog.get_logger()

SUBJECT_ANOMALY = "corridor.traffic.anomaly"

#: Tier-1 flag name → anomaly_log severity (mirrors signal-ingress flags.rs).
FLAG_SEVERITY: dict[str, str] = {
    "future_timestamp": "high",
    "stale_data": "low",
    "speed_jump": "medium",
    "phantom_reading": "high",
    "speed_zscore": "medium",
    "congestion_outlier": "medium",
    "road_class_mismatch": "low",
    "spatial_mismatch": "high",
}
ANOMALY_FLAG_VOCABULARY = frozenset(FLAG_SEVERITY)

_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")
_BIGINT_MAX = 2**63 - 1

INSERT_ANOMALIES_SQL = """
INSERT INTO traffic.anomaly_log
    (segment_id, timestamp_utc, anomaly_type, severity, details)
SELECT r.segment_id, to_timestamp(r.timestamp_ms / 1000.0), r.anomaly_type,
       r.severity, r.details
FROM unnest($1::bigint[], $2::bigint[], $3::text[], $4::text[], $5::jsonb[])
     AS r(segment_id, timestamp_ms, anomaly_type, severity, details)
WHERE EXISTS (
    SELECT 1 FROM corridor.road_segments s WHERE s.segment_id = r.segment_id
)
"""


def normalize_flag(raw: Any) -> str | None:
    """One producer flag → CHECK vocabulary name, or None when unknown.

    Accepts the contract spelling plus the Rust variant spelling
    (``SpeedJump``) and hyphen/space variants; anything else is unknown.
    """
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name:
        return None
    if any(c.isupper() for c in name):
        name = _CAMEL_RE.sub("_", name)
    name = name.lower().replace("-", "_").replace(" ", "_")
    if name == "speed_z_score":
        name = "speed_zscore"
    return name if name in ANOMALY_FLAG_VOCABULARY else None


def _segment_id(raw: Any) -> int | None:
    """A positive id that fits ``bigint`` (utf8 ids from signal-ingress too)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().isdigit() and len(raw.strip()) <= 19:
        value = int(raw.strip())
    else:
        return None
    return value if 0 < value <= _BIGINT_MAX else None


def _reject_constant(name: str) -> Any:
    """``json.loads`` hook: NaN/Infinity are not JSON (nor storable as jsonb)."""
    raise ValueError(f"non-standard JSON constant {name}")


class AnomalyStreamConsumer:
    """Maps corridor.traffic.anomaly payloads onto traffic.anomaly_log rows."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        dedupe_window_min: int = 2,
        clock: Callable[[], float] = time.time,
        max_pending_rows: int = 10_000,
    ) -> None:
        self._pool = pool
        self._dedupe_window_min = max(1, int(dedupe_window_min))
        self._clock = clock
        self._max_pending_rows = max(1, int(max_pending_rows))
        # arrival-minute bucket → {(segment_id, flag)} already queued/written
        self._seen: dict[int, set[tuple[int, str]]] = {}
        self._rows: list[tuple[int, int, str, str, str]] = []
        #: Skip/drop counters by reason (no_valid_flag, unknown_flag,
        #: duplicate, invalid_json, invalid_segment_id, invalid_timestamp,
        #: unknown_segment, buffer_overflow).
        self.counters: Counter = Counter()
        self.rows_written_total = 0
        self.last_error: str | None = None

    @property
    def pending_rows(self) -> int:
        return len(self._rows)

    def handle(self, data: bytes) -> int:
        """Parse one NATS payload and queue its rows. Returns rows queued."""
        try:
            obs = json.loads(data, parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError, RecursionError):
            self.counters["invalid_json"] += 1
            return 0
        if not isinstance(obs, dict):
            self.counters["invalid_json"] += 1
            return 0
        return self.queue(obs)

    def queue(self, obs: dict) -> int:
        """Queue anomaly_log rows for one decoded observation document."""
        raw_flags = obs.get("anomaly_flags")
        flags: list[str] = []
        if isinstance(raw_flags, list):
            for raw in raw_flags:
                name = normalize_flag(raw)
                if name is None:
                    self.counters["unknown_flag"] += 1
                elif name not in flags:
                    flags.append(name)
        if not flags:
            self.counters["no_valid_flag"] += 1
            return 0

        segment_id = _segment_id(obs.get("segment_id"))
        if segment_id is None:
            self.counters["invalid_segment_id"] += 1
            return 0
        ts_ms = obs.get("timestamp_ms")
        if (
            isinstance(ts_ms, bool)
            or not isinstance(ts_ms, (int, float))
            or (isinstance(ts_ms, float) and not math.isfinite(ts_ms))
            or ts_ms <= 0
        ):
            self.counters["invalid_timestamp"] += 1
            return 0
        arrival_ms = int(self._clock() * 1000)
        ts_ms = min(int(ts_ms), arrival_ms)  # never a future-dated row

        minute = arrival_ms // 60_000
        self._prune(minute)
        seen = self._seen.setdefault(minute, set())
        try:
            details = json.dumps(obs, separators=(",", ":"), default=str, allow_nan=False)
        except ValueError:  # e.g. 1e400 parsed to inf
            details = None
        if details is None or "\\u0000" in details:  # jsonb refuses NUL
            self.counters["invalid_json"] += 1
            return 0
        queued = 0
        for flag in flags:
            key = (segment_id, flag)
            if key in seen:
                self.counters["duplicate"] += 1
                continue
            seen.add(key)
            severity = FLAG_SEVERITY[flag]
            self._rows.append((segment_id, ts_ms, flag, severity, details))
            ANOMALY_DETECTIONS.labels(anomaly_type=flag, severity=severity).inc()
            queued += 1
        self._enforce_cap()
        return queued

    def _enforce_cap(self) -> None:
        overflow = len(self._rows) - self._max_pending_rows
        if overflow > 0:
            del self._rows[:overflow]
            self.counters["buffer_overflow"] += overflow

    def _prune(self, minute: int) -> None:
        horizon = minute - self._dedupe_window_min
        for bucket in [b for b in self._seen if b < horizon]:
            del self._seen[bucket]

    async def flush(self) -> int:
        """Insert queued rows in one statement. Returns rows written.

        On failure the rows stay queued for the next flush and the error
        propagates to the caller (the ingest service logs + surfaces it).
        """
        if not self._rows:
            return 0
        rows, self._rows = self._rows, []
        try:
            async with self._pool.acquire() as conn:
                status = await conn.execute(
                    INSERT_ANOMALIES_SQL,
                    [r[0] for r in rows],
                    [r[1] for r in rows],
                    [r[2] for r in rows],
                    [r[3] for r in rows],
                    [r[4] for r in rows],
                )
        except Exception as exc:
            self._rows = rows + self._rows
            self._enforce_cap()
            self.last_error = type(exc).__name__
            raise
        self.last_error = None
        try:
            written = int(str(status).rsplit(" ", 1)[-1])
        except ValueError:
            written = 0
        if written < len(rows):
            self.counters["unknown_segment"] += len(rows) - written
        self.rows_written_total += written
        logger.debug("anomaly rows written", rows=written, queued=len(rows))
        return written
