"""Valkey seam — the emulator is the writer of ``adaptive:inputs:{id}``.

The adaptive engine loop (traffic_oracle/adaptive/engine_loop.py:63-64,182-206)
is the ONLY reader of these keys today (repo grep; signal-ingress publishes
NATS Arrow only), so live demos on the inputs path are emulator-fed by
design — declared property, contract C3.  The doc body matches the reader
shape EXACTLY; provenance rides a separate sidecar key the reader never
touches, so the doc itself stays byte-identical to a real feed's.

The client is created lazily and kept even when the first PING fails (a
lost boot race): its connection pool reconnects on the next command, so
the seam heals on its own and :meth:`probe` reports the real state.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
import valkey.asyncio as valkey_async

logger = structlog.get_logger(__name__)

__all__ = ["ValkeySeam", "inputs_key", "source_key"]


def inputs_key(movement_id: str) -> str:
    """Exact key the engine loop reads (engine_loop.inputs_key)."""
    return f"adaptive:inputs:{movement_id}"


def source_key(movement_id: str) -> str:
    """Provenance sidecar (never read by the engine loop)."""
    return f"emulator:source:{movement_id}"


class ValkeySeam:
    """Writes cause docs into the live adaptive inputs path."""

    def __init__(self, client: valkey_async.Valkey, ttl_sec: int = 30) -> None:
        self._client = client
        self._ttl_sec = ttl_sec

    async def write_inputs_doc(self, movement_id: str, doc: dict[str, Any]) -> None:
        await self._client.set(
            inputs_key(movement_id), json.dumps(doc, sort_keys=True), ex=self._ttl_sec
        )

    async def write_source(
        self, movement_id: str, run_id: int | None, use_case_id: str, replay: bool
    ) -> None:
        await self._client.set(
            source_key(movement_id),
            json.dumps(
                {
                    "run_id": run_id,
                    "use_case_id": use_case_id,
                    "replay": replay,
                },
                sort_keys=True,
            ),
            ex=self._ttl_sec,
        )

    async def probe(self) -> bool:
        """A real PING (raises on a dead link; the caller reports False)."""
        return bool(await self._client.ping())

    async def close(self) -> None:
        await self._client.aclose()
