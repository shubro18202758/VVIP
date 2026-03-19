"""NATS consumer for Arrow IPC traffic snapshots."""

from __future__ import annotations

import nats
import structlog

logger = structlog.get_logger()

SUBJECT_LIVE = "corridor.traffic.live"


class TrafficConsumer:
    """Subscribes to NATS and yields raw Arrow IPC bytes."""

    def __init__(self, nats_url: str = "nats://localhost:4222") -> None:
        self._nats_url = nats_url
        self._nc: nats.aio.client.Client | None = None

    async def connect(self) -> None:
        self._nc = await nats.connect(self._nats_url)
        logger.info("NATS consumer connected", url=self._nats_url)

    async def close(self) -> None:
        if self._nc:
            await self._nc.drain()
            self._nc = None

    async def subscribe(self):
        """Yields raw Arrow IPC bytes from NATS messages."""
        if self._nc is None:
            raise RuntimeError("Not connected — call connect() first")

        sub = await self._nc.subscribe(SUBJECT_LIVE)
        logger.info("subscribed to NATS subject", subject=SUBJECT_LIVE)

        async for msg in sub.messages:
            yield msg.data
