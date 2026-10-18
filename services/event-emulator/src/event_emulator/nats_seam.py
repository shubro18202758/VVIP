"""NATS seam — showcase-only synthetic batches on ``corridor.traffic.emulated``.

NEVER ``corridor.traffic.live`` (the real-feed subject is reserved for the
real signal-ingress; contract C5).  Batches are Arrow IPC RecordBatches in
the decoded schema of the live plane (ingest/cache_writer.py,
ingest/db_writer.py):

    timestamp_ms int64 | lon float64 | lat float64 | segment_id int64 |
    speed_kmh float64 | congestion_index float64 | source int8 |
    data_quality int8 | confidence float64

so a consumer of the emulated subject decodes with the same ArrowDecoder.
Replay batches carry the ``X-Emulation-Replay`` NATS message header — the
single tell; live batches carry none.
"""

from __future__ import annotations

import io
import json
import time

import nats
import pyarrow as pa
import structlog

from event_emulator.twin_seam import REPLAY_HEADER

logger = structlog.get_logger(__name__)

__all__ = ["NatsSeam", "EMULATED_SCHEMA", "EMULATED_SUBJECT", "ANPR_EMULATED_SUBJECT", "UNITS_EMULATED_SUBJECT"]

EMULATED_SUBJECT = "corridor.traffic.emulated"

#: Watchlist plane subjects (contract C2) — clearly-labelled emulated JSON
#: documents: camera reads and agency-unit positions.  NEVER the live
#: subjects (corridor.traffic.live is reserved for the real signal-ingress).
ANPR_EMULATED_SUBJECT = "corridor.anpr.emulated"
UNITS_EMULATED_SUBJECT = "corridor.units.emulated"

# Feed source ordinals (ingest/cache_writer.py _SOURCE_MAP) — emulated
# probes present as fleet GPS; data_quality 0 = REAL, 1 = ANOMALOUS.
_SOURCE_FLEET_GPS = 2

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

    ``rows``: (segment_id, lon, lat, speed_kmh, congestion_index).
    """
    columns = {
        "timestamp_ms": pa.array([t_epoch_ms] * len(rows), type=pa.int64()),
        "lon": pa.array([r[1] for r in rows], type=pa.float64()),
        "lat": pa.array([r[2] for r in rows], type=pa.float64()),
        "segment_id": pa.array([r[0] for r in rows], type=pa.int64()),
        "speed_kmh": pa.array([r[3] for r in rows], type=pa.float64()),
        "congestion_index": pa.array([r[4] for r in rows], type=pa.float64()),
        "source": pa.array([_SOURCE_FLEET_GPS] * len(rows), type=pa.int8()),
        "data_quality": pa.array([1 if r[4] > 0.85 else 0 for r in rows], type=pa.int8()),
        "confidence": pa.array([0.9] * len(rows), type=pa.float64()),
    }
    batch = pa.record_batch(columns, schema=EMULATED_SCHEMA)
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, EMULATED_SCHEMA) as writer:
        writer.write_batch(batch)
    return sink.getvalue()


class NatsSeam:
    """Publishes synthetic batches to the emulated showcase subject."""

    def __init__(self, nats_url: str, subject: str = EMULATED_SUBJECT) -> None:
        self._nats_url = nats_url
        self._subject = subject
        self._nc: nats.aio.client.Client | None = None

    async def connect(self) -> bool:
        try:
            self._nc = await nats.connect(self._nats_url)
            return True
        except Exception as exc:  # noqa: BLE001 - degradation, not fatal
            logger.warning("nats_seam.connect_failed", url=self._nats_url, error=str(exc))
            self._nc = None
            return False

    async def publish(
        self, rows: list[tuple[int, float, float, float, float]], replay_run_id: int | None = None
    ) -> bool:
        if self._nc is None and not await self.connect():
            return False
        assert self._nc is not None
        payload = build_batch(int(time.time() * 1000), rows)
        headers = {REPLAY_HEADER: str(replay_run_id)} if replay_run_id is not None else None
        await self._nc.publish(self._subject, payload, headers=headers)
        return True

    async def publish_json(
        self, subject: str, doc: dict, replay_run_id: int | None = None
    ) -> bool:
        """Publish one clearly-labelled JSON document (watchlist plane).

        Used by the anpr_read / agency_unit deliveries (contract C2): every
        document carries ``source: 'emulated_anpr'`` / ``'emulated'`` and
        the replay header when it is a replay — the same single-tell rule
        as the Arrow batches.  Subjects are fixed constants (config), the
        document is serialized with sort_keys for byte stability.
        """
        if self._nc is None and not await self.connect():
            return False
        assert self._nc is not None
        body = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {REPLAY_HEADER: str(replay_run_id)} if replay_run_id is not None else None
        await self._nc.publish(subject, body, headers=headers)
        return True

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None
