"""NATS seam — showcase-only synthetic batches on ``corridor.traffic.emulated``.

NEVER ``corridor.traffic.live`` (the real-feed subject is reserved for the
real signal-ingress; contract C5).  Batches are Arrow IPC RecordBatches in
the decoded schema of the live plane (ingest/cache_writer.py,
ingest/db_writer.py):

    timestamp_ms int64 | lon float64 | lat float64 | segment_id int64 |
    speed_kmh float64 | congestion_index float64 | source int8 |
    data_quality int8 | confidence float64

so a consumer of the emulated subject decodes with the same ArrowDecoder —
and reads every row's data_quality as SYNTHETIC (an outlier: ANOMALOUS),
never REAL.  Each row sits ON its segment (the midpoint along the line).
Replay batches carry the ``X-Emulation-Replay`` NATS message header — the
single tell for a REPLAY; live batches carry none.

Agency-unit positions ride ``corridor.units.emulated`` as clearly labelled
JSON (contract C-A; the oracle mirrors them to ``units:emulated:*``).
ANPR reads do NOT ride NATS: they enter through the oracle's ingest seam
(contract C-E), which computes hits and rules.

Connection: connected in the API lifespan and reconnected forever by the
client once up.  The FIRST connect is bounded by ``connect_budget_sec``
(nats-py's own initial connect retries forever when reconnects are
unlimited, which would hang the lifespan on a dead broker), and a failed
attempt is retried at most every ``connect_backoff_sec`` — so a dead broker
never stalls the boot, a probe or a frame loop.  A publish while
disconnected is NOT buffered and reported delivered — it is refused, so
per-frame delivery stays honest.  :meth:`probe` is a real PING/PONG round
trip.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from contextlib import suppress
from typing import Any, Callable

import pyarrow as pa
import structlog
from nats.aio.client import Client as NatsClient

from event_emulator.twin_seam import REPLAY_HEADER

logger = structlog.get_logger(__name__)

__all__ = [
    "ANOMALOUS_CONGESTION",
    "DQ_ANOMALOUS",
    "DQ_SYNTHETIC",
    "EMULATED_SCHEMA",
    "EMULATED_SUBJECT",
    "NatsSeam",
    "UNITS_EMULATED_SUBJECT",
]

EMULATED_SUBJECT = "corridor.traffic.emulated"

#: Agency-unit positions (contract C-A) — clearly-labelled emulated JSON.
#: NEVER the live subjects (corridor.traffic.live is the real ingress's).
UNITS_EMULATED_SUBJECT = "corridor.units.emulated"

# Feed source ordinals (ingest/cache_writer.py _SOURCE_MAP) — emulated
# probes present as fleet GPS.
_SOURCE_FLEET_GPS = 2

# data_quality ordinals — the shared wire map (signal-ingress
# DataQuality::as_u8 = traffic_oracle DATA_QUALITY_ORDINALS): 0 REAL,
# 1 ANOMALOUS, 2 SYNTHETIC.  An emulated row is never REAL: every row is
# labelled SYNTHETIC on the wire, the congestion-outlier marker ANOMALOUS —
# so any consumer of this subject reads the label honestly without relying
# on the oracle's ingest, which persists the whole subject as 'synthetic'.
DQ_ANOMALOUS = 1
DQ_SYNTHETIC = 2
#: congestion_index above which an emulated row carries the ANOMALOUS marker
ANOMALOUS_CONGESTION = 0.85

EMULATED_SCHEMA = pa.schema(
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


def build_batch(
    t_epoch_ms: int,
    rows: list[tuple[int, float, float, float, float]],
) -> bytes:
    """Serialize one synthetic observation batch to Arrow IPC bytes.

    ``rows``: (segment_id, lon, lat, speed_kmh, congestion_index).  Every
    row is labelled data_quality SYNTHETIC (2), a congestion outlier
    ANOMALOUS (1) — never REAL (0).
    """
    columns = {
        "timestamp_ms": pa.array([t_epoch_ms] * len(rows), type=pa.int64()),
        "lon": pa.array([r[1] for r in rows], type=pa.float64()),
        "lat": pa.array([r[2] for r in rows], type=pa.float64()),
        "segment_id": pa.array([r[0] for r in rows], type=pa.int64()),
        "speed_kmh": pa.array([r[3] for r in rows], type=pa.float64()),
        "congestion_index": pa.array([r[4] for r in rows], type=pa.float64()),
        "source": pa.array([_SOURCE_FLEET_GPS] * len(rows), type=pa.int8()),
        "data_quality": pa.array(
            [DQ_ANOMALOUS if r[4] > ANOMALOUS_CONGESTION else DQ_SYNTHETIC for r in rows],
            type=pa.int8(),
        ),
        "confidence": pa.array([0.9] * len(rows), type=pa.float64()),
    }
    batch = pa.record_batch(columns, schema=EMULATED_SCHEMA)
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, EMULATED_SCHEMA) as writer:
        writer.write_batch(batch)
    return sink.getvalue()


class NatsSeam:
    """Publishes synthetic batches / unit docs to the emulated subjects."""

    def __init__(
        self,
        nats_url: str,
        subject: str = EMULATED_SUBJECT,
        *,
        connect_backoff_sec: float = 10.0,
        connect_budget_sec: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
        client_factory: Callable[[], Any] = NatsClient,
    ) -> None:
        self._nats_url = nats_url
        self._subject = subject
        self._nc: Any = None
        self._backoff = connect_backoff_sec
        self._budget = connect_budget_sec
        self._clock = clock
        self._client_factory = client_factory
        self._last_attempt: float | None = None
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._nc is not None and bool(self._nc.is_connected)

    async def _on_error(self, exc: Exception) -> None:
        # nats-py reports every failed (re)connect attempt here; keep the
        # last one for /health instead of a traceback per attempt
        self.last_error = f"{type(exc).__name__}: {exc}"

    async def connect(self, *, force: bool = False) -> bool:
        """Connect (throttled, bounded).  Once up, the client reconnects
        forever on its own."""
        if self._nc is not None and not self._nc.is_closed:
            return bool(self._nc.is_connected)
        now = self._clock()
        if (not force and self._last_attempt is not None
                and now - self._last_attempt < self._backoff):
            return False
        self._last_attempt = now
        nc = self._client_factory()
        try:
            await asyncio.wait_for(
                nc.connect(
                    self._nats_url,
                    name="event-emulator",
                    connect_timeout=2,
                    allow_reconnect=True,
                    max_reconnect_attempts=-1,
                    reconnect_time_wait=2,
                    error_cb=self._on_error,
                ),
                timeout=self._budget,
            )
        except Exception as exc:  # noqa: BLE001 - degradation, not fatal
            if isinstance(exc, asyncio.TimeoutError):
                reason = f"no broker answered within {self._budget:g} s"
                if self.last_error:
                    reason += f" ({self.last_error})"
                self.last_error = reason
            else:
                self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("nats_seam.connect_failed", url=self._nats_url,
                           error=self.last_error)
            with suppress(Exception):
                await nc.close()
            self._nc = None
            return False
        self._nc = nc
        self.last_error = None
        return True

    async def probe(self, timeout: float = 1.0) -> bool:
        """PING/PONG round trip; reconnects (throttled) when down."""
        if not self.connected and not await self.connect():
            return False
        try:
            await self._nc.flush(timeout=timeout)
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    async def _ready(self) -> bool:
        if self.connected:
            return True
        await self.connect()
        return self.connected

    async def _send(self, subject: str, payload: bytes, replay_run_id: int | None) -> bool:
        """One publish; a client-side refusal (closed / buffer full) is a
        failed delivery, never an exception into the caller's loop."""
        if not await self._ready():
            return False
        headers = {REPLAY_HEADER: str(replay_run_id)} if replay_run_id is not None else None
        try:
            await self._nc.publish(subject, payload, headers=headers)
        except Exception as exc:  # noqa: BLE001 - reported as not delivered
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        return True

    async def publish(
        self, rows: list[tuple[int, float, float, float, float]], replay_run_id: int | None = None
    ) -> bool:
        payload = build_batch(int(time.time() * 1000), rows)
        return await self._send(self._subject, payload, replay_run_id)

    async def publish_json(
        self, subject: str, doc: dict, replay_run_id: int | None = None
    ) -> bool:
        """Publish one clearly-labelled JSON document (agency units, C-A).

        Every document carries ``source: 'emulated'`` and the replay header
        when it is a replay — the same single-tell rule as the Arrow
        batches.  Subjects are fixed constants; the document is serialized
        with sort_keys for byte stability.
        """
        body = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return await self._send(subject, body, replay_run_id)

    async def close(self) -> None:
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception as exc:  # noqa: BLE001 - closing a dead link
                logger.debug("nats_seam.drain_failed", error=str(exc))
            self._nc = None
