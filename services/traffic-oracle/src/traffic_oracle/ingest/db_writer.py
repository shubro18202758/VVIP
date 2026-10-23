"""Batch-inserts decoded traffic observations into PostGIS.

Written against the REAL ``traffic.observations`` schema (001 + 003):

    segment_id     bigint            ← canonical int segment id
    timestamp_utc  timestamptz       ← to_timestamp(timestamp_ms / 1000.0)
    geom           geometry(Point)   ← ST_SetSRID(ST_MakePoint(lon, lat), 4326)
    source         text CHECK        ← FeedSource.db_value ('government', ...)
    data_quality   text CHECK        ← 'real' | 'anomalous' | 'synthetic'
    congestion_idx / confidence      ← clamped into [0, 1]

Rows are buffered and flushed every ``flush_interval_sec`` seconds or every
``flush_rows`` rows (whichever first) as ONE set-based statement: the
column arrays ride ``unnest()`` and rows whose segment is not in
``corridor.road_segments`` are filtered by the same statement, so the
insert is atomic and the dropped count is exact (submitted − inserted).
A failed flush keeps its rows buffered for the next attempt (bounded by
``max_buffer_rows``; the oldest rows beyond it are dropped and counted).

A batch the database refuses for its CONTENT (a data exception or a
constraint violation, i.e. a value the schema cannot hold) is never retried
whole — one such row would otherwise stall every later observation behind
it.  The batch is bisected depth-first: halves that insert are kept,
single refused rows are dropped and counted as ``rejected_row``.  A flush
spends at most ``max(isolation_budget, 2·⌈log2 n⌉ + 2)`` statements on it —
always enough to reach and drop at least one refused row, so every flush
makes progress — and anything still unresolved stays buffered for the
next one.  Connection-level failures keep the whole batch buffered.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field

import asyncpg
import polars as pl
import structlog

from traffic_oracle.data.models import FeedSource
from traffic_oracle.ingest.normalize import ensure_canonical

logger = structlog.get_logger()

INSERT_OBSERVATIONS_SQL = """
INSERT INTO traffic.observations
    (segment_id, timestamp_utc, speed_kmh, congestion_idx, source,
     data_quality, confidence, geom)
SELECT r.segment_id,
       to_timestamp(r.timestamp_ms / 1000.0),
       r.speed_kmh,
       LEAST(GREATEST(r.congestion_idx, 0.0), 1.0),
       r.source,
       r.data_quality,
       LEAST(GREATEST(r.confidence, 0.0), 1.0),
       ST_SetSRID(ST_MakePoint(r.lon, r.lat), 4326)
FROM unnest($1::bigint[], $2::bigint[], $3::float8[], $4::float8[], $5::text[],
            $6::text[], $7::float8[], $8::float8[], $9::float8[])
     AS r(segment_id, timestamp_ms, speed_kmh, congestion_idx, source,
          data_quality, confidence, lon, lat)
WHERE EXISTS (
    SELECT 1 FROM corridor.road_segments s WHERE s.segment_id = r.segment_id
)
"""

#: FeedSource enum value (canonical frame) → observations CHECK spelling.
_DB_SOURCE = {s.value: s.db_value for s in FeedSource}

#: Errors that blame the ROWS (SQLSTATE classes 22 data exception and 23
#: integrity constraint violation), not the connection.
_ROW_CONTENT_ERRORS: tuple[type[Exception], ...] = (
    asyncpg.exceptions.DataError,
    asyncpg.exceptions.IntegrityConstraintViolationError,
)


def _rowcount(status: object) -> int:
    """Rows affected from an asyncpg command tag (``'INSERT 0 42'``)."""
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        logger.warning("db_writer.unparseable_status", status=str(status))
        return 0


@dataclass
class _Isolation:
    """Book-keeping for one bisecting flush."""

    budget: int
    submitted: int = 0
    written: int = 0
    rejected: int = 0
    unresolved: list[pl.DataFrame] = field(default_factory=list)
    error: Exception | None = None
    sample: dict | None = None


@dataclass(frozen=True)
class FlushResult:
    """Outcome of one buffered flush."""

    submitted: int
    written: int

    @property
    def dropped_unknown_segment(self) -> int:
        return self.submitted - self.written


class DbWriter:
    """Writes canonical observation frames to PostGIS (buffered, set-based)."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        flush_interval_sec: float = 5.0,
        flush_rows: int = 2000,
        max_buffer_rows: int = 50_000,
        isolation_budget: int = 64,
    ) -> None:
        self._pool = pool
        self.flush_interval_sec = float(flush_interval_sec)
        self.flush_rows = int(flush_rows)
        self._max_buffer_rows = int(max_buffer_rows)
        self._isolation_budget = max(1, int(isolation_budget))
        self._frames: list[pl.DataFrame] = []
        self._pending = 0
        self._last_flush = time.monotonic()
        #: Per-reason drop counters (unknown_segment, buffer_overflow,
        #: rejected_row).
        self.dropped: Counter = Counter()
        self.rows_written_total = 0
        self.last_error: str | None = None

    # -- buffering -------------------------------------------------------------

    @property
    def pending_rows(self) -> int:
        return self._pending

    def add(self, df: pl.DataFrame) -> int:
        """Buffer a frame for the next flush. Returns rows now pending."""
        df = ensure_canonical(df)
        if df.is_empty():
            return self._pending
        self._frames.append(df)
        self._pending += df.height
        self._enforce_cap()
        return self._pending

    def flush_due(self, now: float | None = None) -> bool:
        if self._pending == 0:
            return False
        if self._pending >= self.flush_rows:
            return True
        now = time.monotonic() if now is None else now
        return now - self._last_flush >= self.flush_interval_sec

    def _enforce_cap(self) -> None:
        overflow = self._pending - self._max_buffer_rows
        while overflow > 0 and self._frames:
            head = self._frames[0]
            if head.height <= overflow:
                self._frames.pop(0)
                self._pending -= head.height
                self.dropped["buffer_overflow"] += head.height
                overflow -= head.height
            else:
                self._frames[0] = head.slice(overflow)
                self._pending -= overflow
                self.dropped["buffer_overflow"] += overflow
                overflow = 0

    def _retain(self, frames: list[pl.DataFrame]) -> None:
        """Put unwritten rows back at the FRONT of the buffer (capped)."""
        frames = [f for f in frames if not f.is_empty()]
        self._frames = frames + self._frames
        self._pending += sum(f.height for f in frames)
        self._enforce_cap()

    async def flush(self) -> FlushResult:
        """Insert everything buffered in one statement.

        On a connection-level failure the rows go back to the FRONT of the
        buffer (retried on the next flush) and the error propagates to the
        caller.  A content refusal is isolated row by row (module doc).
        """
        self._last_flush = time.monotonic()
        if not self._frames:
            return FlushResult(0, 0)
        frames, self._frames = self._frames, []
        self._pending = 0
        batch = pl.concat(frames, how="vertical")
        try:
            written = await self._insert(batch)
        except _ROW_CONTENT_ERRORS as exc:
            result = await self._flush_isolating(batch, exc)
        except Exception as exc:
            self._retain(frames)
            self.last_error = type(exc).__name__
            raise
        else:
            result = FlushResult(submitted=batch.height, written=written)
            self.last_error = None
        if result.dropped_unknown_segment:
            self.dropped["unknown_segment"] += result.dropped_unknown_segment
        self.rows_written_total += result.written
        logger.debug(
            "wrote observation batch",
            rows=result.written,
            dropped_unknown_segment=result.dropped_unknown_segment,
        )
        return result

    async def _flush_isolating(self, batch: pl.DataFrame, cause: Exception) -> FlushResult:
        """Bisect a content-refused batch: keep what inserts, drop what never can."""
        budget = max(self._isolation_budget, 2 * batch.height.bit_length() + 2)
        state = _Isolation(budget=budget)
        await self._bisect(batch, state)
        if state.rejected:
            self.dropped["rejected_row"] += state.rejected
        self.last_error = type(state.error or cause).__name__
        logger.warning(
            "db_writer.rows_rejected",
            error=f"{type(cause).__name__}: {cause}",
            rejected=state.rejected,
            written=state.written,
            unresolved=sum(f.height for f in state.unresolved),
            statements=budget - state.budget,
            sample=state.sample,
        )
        if state.unresolved:
            self._retain(state.unresolved)
        if state.error is not None:
            # The connection failed mid-isolation: what was written stays
            # written (counted), the rest is buffered, the caller hears it.
            self.rows_written_total += state.written
            if state.submitted - state.written:
                self.dropped["unknown_segment"] += state.submitted - state.written
            raise state.error
        return FlushResult(submitted=state.submitted, written=state.written)

    async def _bisect(self, batch: pl.DataFrame, state: _Isolation) -> None:
        """``batch`` was refused for its content: drop it if it is one row,
        else try each half (left first — depth-first reaches a bad row in
        at most two statements per level)."""
        if batch.height == 1:
            state.rejected += 1
            if state.sample is None:
                state.sample = {
                    k: batch[k][0] for k in ("segment_id", "timestamp_ms", "speed_kmh", "source")
                }
            return
        mid = batch.height // 2
        await self._isolate(batch.slice(0, mid), state)
        await self._isolate(batch.slice(mid), state)

    async def _isolate(self, batch: pl.DataFrame, state: _Isolation) -> None:
        if state.error is not None or state.budget <= 0:
            state.unresolved.append(batch)
            return
        state.budget -= 1
        try:
            written = await self._insert(batch)
        except _ROW_CONTENT_ERRORS:
            await self._bisect(batch, state)
            return
        except Exception as exc:  # noqa: BLE001 — surfaced by _flush_isolating
            state.error = exc
            state.unresolved.append(batch)
            return
        state.submitted += batch.height
        state.written += written

    # -- immediate write ---------------------------------------------------------

    async def write_batch(self, df: pl.DataFrame) -> int:
        """Insert one batch immediately (no buffering).

        Accepts a canonical or raw decoded frame. Returns the number of rows
        inserted (rows on segments unknown to corridor.road_segments are
        filtered by the statement itself and counted in ``dropped``).
        """
        if df.is_empty():
            return 0
        batch = ensure_canonical(df)
        if batch.is_empty():
            return 0
        written = await self._insert(batch)
        unknown = batch.height - written
        if unknown:
            self.dropped["unknown_segment"] += unknown
        self.rows_written_total += written
        return written

    async def _insert(self, batch: pl.DataFrame) -> int:
        args = (
            batch["segment_id"].to_list(),
            batch["timestamp_ms"].to_list(),
            batch["speed_kmh"].to_list(),
            batch["congestion_idx"].to_list(),
            [_DB_SOURCE[s] for s in batch["source"].to_list()],
            batch["data_quality"].to_list(),
            batch["confidence"].to_list(),
            batch["lon"].to_list(),
            batch["lat"].to_list(),
        )
        async with self._pool.acquire() as conn:
            status = await conn.execute(INSERT_OBSERVATIONS_SQL, *args)
        return _rowcount(status)
