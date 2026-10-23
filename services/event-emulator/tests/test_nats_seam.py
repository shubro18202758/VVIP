"""NATS seam tests (fake client — no broker).

The seam must never stall the process on a dead broker: nats-py's own
initial connect retries forever when reconnects are unlimited, so the
first connect is bounded; a failed attempt is retried at most once per
backoff; and a publish the client refuses is a failed delivery (False),
never an exception into a frame loop or a living-city tick.
"""

from __future__ import annotations

import asyncio
import time

import pyarrow as pa

from event_emulator.nats_seam import (
    DQ_ANOMALOUS,
    DQ_SYNTHETIC,
    EMULATED_SCHEMA,
    NatsSeam,
    build_batch,
)
from event_emulator.twin_seam import REPLAY_HEADER


class _Client:
    """The slice of nats.aio.client.Client the seam uses."""

    def __init__(self, *, hang: bool = False, refuse: bool = False,
                 flush_fails: bool = False) -> None:
        self.hang = hang
        self.refuse = refuse
        self.flush_fails = flush_fails
        self.is_connected = False
        self.is_closed = False
        self.sent: list[tuple[str, bytes, dict | None]] = []
        self.connect_kwargs: dict = {}

    async def connect(self, servers, **kwargs) -> None:
        self.connect_kwargs = kwargs
        if self.hang:                       # nats-py on a dead broker
            await asyncio.Event().wait()
        self.is_connected = True

    async def publish(self, subject, payload, headers=None) -> None:
        if self.refuse:
            raise RuntimeError("nats: outbound buffer limit exceeded")
        self.sent.append((subject, payload, headers))

    async def flush(self, timeout=None) -> None:
        if self.flush_fails:
            raise TimeoutError("nats: flush timeout")

    async def close(self) -> None:
        self.is_closed = True
        self.is_connected = False

    async def drain(self) -> None:
        await self.close()


def _seam(*clients: _Client, **kw) -> tuple[NatsSeam, list[_Client]]:
    made: list[_Client] = []
    pool = list(clients)

    def factory() -> _Client:
        c = pool.pop(0) if pool else _Client()
        made.append(c)
        return c

    kw.setdefault("connect_budget_sec", 0.2)
    return NatsSeam("nats://127.0.0.1:4222", client_factory=factory, **kw), made


class TestConnect:
    async def test_a_dead_broker_never_stalls_the_boot(self):
        seam, made = _seam(_Client(hang=True))
        started = time.monotonic()
        assert await seam.connect(force=True) is False
        assert time.monotonic() - started < 1.0
        assert "no broker answered" in (seam.last_error or "")
        assert made[0].is_closed               # the half-made client is closed
        assert seam.connected is False

    async def test_reconnects_forever_once_up(self):
        seam, made = _seam(_Client())
        assert await seam.connect(force=True) is True
        kw = made[0].connect_kwargs
        assert kw["allow_reconnect"] is True and kw["max_reconnect_attempts"] == -1
        assert kw["name"] == "event-emulator"

    async def test_failed_attempts_are_throttled(self):
        seam, made = _seam(_Client(hang=True), connect_backoff_sec=60.0)
        assert await seam.connect(force=True) is False
        started = time.monotonic()
        assert await seam.connect() is False   # inside the backoff: no attempt
        assert await seam.probe() is False
        assert time.monotonic() - started < 0.1
        assert len(made) == 1

    async def test_probe_is_a_round_trip(self):
        seam, _ = _seam(_Client())
        assert await seam.probe() is True
        seam2, _ = _seam(_Client(flush_fails=True))
        assert await seam2.probe() is False
        assert "flush" in (seam2.last_error or "")


class TestPublish:
    async def test_refused_publish_is_not_delivered(self):
        seam, _ = _seam(_Client(refuse=True))
        assert await seam.connect(force=True)
        assert await seam.publish_json("corridor.units.emulated", {"unit_id": "JR"}) is False
        assert await seam.publish([(1001, 72.5, 23.0, 30.0, 0.4)]) is False
        assert "outbound buffer" in (seam.last_error or "")

    async def test_disconnected_publish_is_not_delivered(self):
        seam, _ = _seam(_Client(hang=True), connect_backoff_sec=60.0)
        assert await seam.publish_json("corridor.units.emulated", {"unit_id": "JR"}) is False

    async def test_batches_and_docs_carry_the_replay_tell_only_on_replay(self):
        seam, made = _seam(_Client())
        assert await seam.publish([(1001, 72.5, 23.0, 30.0, 0.4)])
        assert await seam.publish_json("corridor.units.emulated", {"unit_id": "JR"},
                                       replay_run_id=12)
        (s1, batch, h1), (s2, doc, h2) = made[0].sent
        assert s1 == "corridor.traffic.emulated" and h1 is None
        assert s2 == "corridor.units.emulated" and h2 == {REPLAY_HEADER: "12"}
        table = pa.ipc.open_stream(batch).read_all()
        assert table.schema.equals(EMULATED_SCHEMA)
        assert doc == b'{"unit_id":"JR"}'


class TestWireLabels:
    """An emulated row is never REAL on the wire (shared ordinal map:
    0 real, 1 anomalous, 2 synthetic — DATA_QUALITY_ORDINALS)."""

    def test_rows_are_synthetic_and_the_outlier_marker_anomalous(self):
        rows = [(1001, 72.5, 23.0, 30.0, 0.4), (1002, 72.6, 23.1, 8.0, 0.9),
                (1003, 72.7, 23.2, 41.0, 0.0), (1004, 72.8, 23.3, 9.5, 0.85)]
        table = pa.ipc.open_stream(build_batch(1_700_000_000_000, rows)).read_all()
        assert table.column("data_quality").to_pylist() == [
            DQ_SYNTHETIC, DQ_ANOMALOUS, DQ_SYNTHETIC, DQ_SYNTHETIC]
        assert (DQ_ANOMALOUS, DQ_SYNTHETIC) == (1, 2)
        assert 0 not in table.column("data_quality").to_pylist()
        assert set(table.column("source").to_pylist()) == {2}   # fleet GPS ordinal

    async def test_a_published_batch_carries_the_labels(self):
        seam, made = _seam(_Client())
        assert await seam.publish([(1001, 72.5, 23.0, 30.0, 0.4), (1002, 72.6, 23.1, 8.0, 0.9)])
        (_subject, batch, _headers), = made[0].sent
        table = pa.ipc.open_stream(batch).read_all()
        assert table.column("data_quality").to_pylist() == [2, 1]
