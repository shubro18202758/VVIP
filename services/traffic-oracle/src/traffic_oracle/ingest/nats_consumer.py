"""NATS connection for the live data plane — one connection, many subjects.

The ingest service (ingest/service.py) owns exactly ONE client per process.
NATS may come up after the oracle (compose start order, a NATS restart), so
the client is created with ``max_reconnect_attempts=-1``: nats-py then
retries the INITIAL connect forever too (every ``reconnect_time_wait``),
and transparently re-subscribes after any later disconnect.  The outer
loop here only guards against non-connectivity failures (bad URL, auth)
with capped exponential backoff.  Connection state, counters and the last
error are kept for /health (the error's class name only — the message,
which can carry hosts/credentials, goes to the log); repeated connect
errors are logged at most once per ``log_every_sec`` so a dark NATS does
not flood the log.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from urllib.parse import urlsplit, urlunsplit

import nats
import structlog

from traffic_oracle.metrics import INGEST_NATS_CONNECTED

logger = structlog.get_logger()

SUBJECT_LIVE = "corridor.traffic.live"
SUBJECT_EMULATED = "corridor.traffic.emulated"


def redact_url(url: str) -> str:
    """Drop any userinfo (``user:pass@``) before a URL reaches logs/health."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid url>"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


class NatsConnection:
    """Supervised NATS client with health-visible connection state."""

    def __init__(
        self,
        url: str,
        name: str = "traffic-oracle-ingest",
        reconnect_time_wait: float = 2.0,
        connect_timeout: float = 2.0,
        log_every_sec: float = 30.0,
    ) -> None:
        self._url = url
        self._name = name
        self._reconnect_time_wait = reconnect_time_wait
        self._connect_timeout = connect_timeout
        self._log_every_sec = log_every_sec
        self._nc: nats.aio.client.Client | None = None
        self._last_error_log = 0.0
        self.closed = asyncio.Event()
        self.connect_attempt_errors = 0
        self.disconnects = 0
        self.reconnects = 0
        self.slow_consumer_errors = 0
        self.last_error: str | None = None
        self.connected_since: float | None = None

    @property
    def url(self) -> str:
        return redact_url(self._url)

    @property
    def client(self) -> nats.aio.client.Client | None:
        return self._nc

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected

    # -- callbacks -----------------------------------------------------------

    async def _error_cb(self, exc: Exception) -> None:
        name = type(exc).__name__
        self.last_error = name
        if "SlowConsumer" in name:
            self.slow_consumer_errors += 1
        else:
            self.connect_attempt_errors += 1
        now = time.monotonic()
        if now - self._last_error_log >= self._log_every_sec:
            self._last_error_log = now
            logger.warning(
                "ingest.nats.error",
                url=self.url,
                error=f"{name}: {exc}",
                connect_errors=self.connect_attempt_errors,
                slow_consumer=self.slow_consumer_errors,
            )

    async def _disconnected_cb(self) -> None:
        self.disconnects += 1
        self.connected_since = None
        INGEST_NATS_CONNECTED.set(0)
        logger.warning("ingest.nats.disconnected", url=self.url)

    async def _reconnected_cb(self) -> None:
        self.reconnects += 1
        self.connected_since = time.monotonic()
        INGEST_NATS_CONNECTED.set(1)
        logger.info("ingest.nats.reconnected", url=self.url)

    async def _closed_cb(self) -> None:
        self.connected_since = None
        INGEST_NATS_CONNECTED.set(0)
        self.closed.set()
        logger.info("ingest.nats.closed", url=self.url)

    # -- lifecycle -------------------------------------------------------------

    async def connect(self, stop: asyncio.Event) -> nats.aio.client.Client | None:
        """Connect, retrying until connected or ``stop`` is set (None then)."""
        backoff = 1.0
        while not stop.is_set():
            self.closed.clear()
            try:
                self._nc = await nats.connect(
                    servers=[self._url],
                    name=self._name,
                    allow_reconnect=True,
                    max_reconnect_attempts=-1,
                    reconnect_time_wait=self._reconnect_time_wait,
                    connect_timeout=self._connect_timeout,
                    error_cb=self._error_cb,
                    disconnected_cb=self._disconnected_cb,
                    reconnected_cb=self._reconnected_cb,
                    closed_cb=self._closed_cb,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — retried with backoff
                self.last_error = type(exc).__name__
                logger.warning(
                    "ingest.nats.connect_failed",
                    url=self.url,
                    error=f"{type(exc).__name__}: {exc}",
                    retry_in_sec=backoff,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2.0, 30.0)
                continue
            self.connected_since = time.monotonic()
            INGEST_NATS_CONNECTED.set(1)
            logger.info("NATS consumer connected", url=self.url)
            return self._nc
        return None

    async def close(self, timeout: float = 5.0) -> None:
        """Drain (finish in-flight callbacks) then close; bounded by timeout."""
        nc, self._nc = self._nc, None
        if nc is None:
            return
        try:
            await asyncio.wait_for(nc.drain(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — shutdown must not hang/raise
            logger.warning("ingest.nats.drain_failed", error=f"{type(exc).__name__}: {exc}")
            with contextlib.suppress(Exception):
                await nc.close()
        INGEST_NATS_CONNECTED.set(0)
