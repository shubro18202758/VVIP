"""Valkey mirroring for the adaptive layer.

Keys published (contract C4 + decider cadence trigger #3):

    adaptive:params:{movement_id}   params JSON, TTL 30 s — written by the
                                    situation-engine loop's decision path
                                    when the decision changes, and re-SET
                                    with the same payload every ~10 s so
                                    the key lapses only when the loop does
                                    (never by a GET /adaptive/params reader)
    situation:changed               JSON notification on every effective
                                    situation_class transition, TTL 60 s

The sink accepts any async client with a ``set(key, value, ex=...)``
method (valkey.asyncio in production, the mock in tests).
"""

from __future__ import annotations

import structlog

from traffic_oracle.adaptive.service import (
    PARAMS_MIRROR_TTL_SEC,
    SITUATION_CHANGED_TTL_SEC,
)

logger = structlog.get_logger(__name__)


def params_key(movement_id: str) -> str:
    return f"adaptive:params:{movement_id}"


SITUATION_CHANGED_KEY = "situation:changed"


class AdaptiveValkeySink:
    """``SinkPort`` implementation backed by a valkey asyncio client."""

    def __init__(self, client: object) -> None:
        self._client = client

    async def publish_params(self, movement_id: str, payload_json: str) -> None:
        await self._client.set(  # type: ignore[attr-defined]
            params_key(movement_id), payload_json, ex=PARAMS_MIRROR_TTL_SEC
        )
        logger.debug(
            "adaptive.params_mirrored", movement_id=movement_id, ttl=PARAMS_MIRROR_TTL_SEC
        )

    async def publish_situation_change(
        self, movement_id: str, from_code: str, to_code: str, payload_json: str
    ) -> None:
        await self._client.set(  # type: ignore[attr-defined]
            SITUATION_CHANGED_KEY, payload_json, ex=SITUATION_CHANGED_TTL_SEC
        )
        logger.info(
            "adaptive.situation_changed",
            movement_id=movement_id,
            situation_from=from_code,
            situation_to=to_code,
        )


class LoggingValkeySink(AdaptiveValkeySink):
    """Sink used when Valkey is unreachable: logs and swallows errors.

    Mirroring is a *mirror*, not a source of truth — the C4 HTTP
    response remains authoritative, so an outage must not break
    param serving.  Every dropped write is logged for observability.
    """

    def __init__(self, client: object | None) -> None:
        super().__init__(client)
        self.dropped_writes = 0

    async def publish_params(self, movement_id: str, payload_json: str) -> None:
        try:
            await super().publish_params(movement_id, payload_json)
        except Exception as exc:  # noqa: BLE001 - mirror must never raise
            self.dropped_writes += 1
            logger.warning(
                "adaptive.params_mirror_dropped",
                movement_id=movement_id,
                error=str(exc),
                dropped=self.dropped_writes,
            )

    async def publish_situation_change(
        self, movement_id: str, from_code: str, to_code: str, payload_json: str
    ) -> None:
        try:
            await super().publish_situation_change(
                movement_id, from_code, to_code, payload_json
            )
        except Exception as exc:  # noqa: BLE001
            self.dropped_writes += 1
            logger.warning(
                "adaptive.situation_change_dropped",
                movement_id=movement_id,
                error=str(exc),
                dropped=self.dropped_writes,
            )


__all__ = [
    "AdaptiveValkeySink",
    "LoggingValkeySink",
    "SITUATION_CHANGED_KEY",
    "params_key",
]
