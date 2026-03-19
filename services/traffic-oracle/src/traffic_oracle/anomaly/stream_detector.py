"""NATS stream consumer for Tier-1 anomaly events."""

from __future__ import annotations

import json

import asyncpg
import nats
import structlog

logger = structlog.get_logger()

SUBJECT_ANOMALY = "corridor.traffic.anomaly"


class AnomalyStreamConsumer:
    """Subscribes to corridor.traffic.anomaly and logs to traffic.anomaly_log."""

    def __init__(self, nats_url: str, pool: asyncpg.Pool) -> None:
        self._nats_url = nats_url
        self._pool = pool
        self._nc: nats.aio.client.Client | None = None

    async def connect(self) -> None:
        self._nc = await nats.connect(self._nats_url)
        logger.info("anomaly stream consumer connected", url=self._nats_url)

    async def close(self) -> None:
        if self._nc:
            await self._nc.drain()
            self._nc = None

    async def run(self) -> None:
        """Subscribe and log anomalies to database indefinitely."""
        if self._nc is None:
            raise RuntimeError("Not connected — call connect() first")

        sub = await self._nc.subscribe(SUBJECT_ANOMALY)
        logger.info("listening for anomaly events", subject=SUBJECT_ANOMALY)

        async for msg in sub.messages:
            try:
                obs = json.loads(msg.data)
                await self._log_anomaly(obs)
            except Exception:
                logger.exception("failed to process anomaly message")

    async def _log_anomaly(self, obs: dict) -> None:
        """Insert anomaly record into traffic.anomaly_log."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO traffic.anomaly_log
                    (segment_id, timestamp_utc, anomaly_type, severity, details)
                VALUES ($1, to_timestamp($2 / 1000.0), $3, $4, $5)
                """,
                int(obs.get("segment_id", 0)),
                obs.get("timestamp_ms", 0),
                obs.get("data_quality", "anomalous"),
                "medium",
                json.dumps(obs),
            )
