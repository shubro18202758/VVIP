"""Ops router — read-only degradation surfaces over shared fleet state.

One endpoint, mounted under ``/api/v1`` by ``__main__``:

    GET /ops/degradation-taxonomy
        The recurring-failure ledger for the /ops board (deck component
        DegradationLedger).  Reads convoy-brain's per-movement
        ``decisions_log`` audit trails straight out of the SHARED Valkey
        (both services are wired to the same ``VVIP_VALKEY_URL`` in every
        compose file) and groups their degradation/failure rows through
        the pure ``degradation_taxonomy`` module.

Honesty contract: the endpoint NEVER invents rows.  No contexts, no
rows, or only healthy rows → an EMPTY taxonomy with the scan counts
that prove it; Valkey unavailable → 503 (the deck panel labels the
source); corrupt context payloads are skipped and counted, never
silently treated as empty movements.

The scanner reads contexts with batched MGET (one round trip per 100
keys, not one GET per movement) and is shared with the city fusion
feed (city/feed.py), which narrates the same decisions_log rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request

from traffic_oracle.city.common import json_doc, mget_values, scan_keys
from traffic_oracle.ops.degradation_taxonomy import build_taxonomy

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/ops", tags=["ops"])

#: Valkey key prefix for convoy-brain's ConvoyContextStore
#: (convoy_brain/memory/convoy_context.py ``CONTEXT_KEY_PREFIX`` — the
#: writer's constant, mirrored here because the oracle must not import
#: convoy-brain to read the trail it publishes).
CONTEXT_KEY_PREFIX = "convoy:context:"

#: Scan bound: contexts beyond this many are reported via ``truncated``
#: instead of silently dropped (24 h TTL on completed movements bounds the
#: keyspace in practice; the flag is the honest escape hatch).
MAX_CONTEXTS = 500


def context_key(movement_id: str) -> str:
    return f"{CONTEXT_KEY_PREFIX}{movement_id}"


async def read_contexts(client: Any, keys: list[str]) -> tuple[list[tuple[str | None, dict]], int]:  # noqa: E501
    """MGET the given context keys; ``((movement_id, payload) pairs, corrupt)``.

    Keys that vanished between scan and read are skipped; payloads that
    are not JSON objects are counted as corrupt, never treated as empty.
    """
    contexts: list[tuple[str | None, dict]] = []
    corrupt = 0
    for key, raw in await mget_values(client, keys):
        if raw is None:
            continue
        data = json_doc(raw)
        if data is None:
            corrupt += 1
            continue
        movement_id = data.get("movement_id")
        if not isinstance(movement_id, str) or not movement_id:
            # The key still identifies the subject when the payload lost it.
            movement_id = key[len(CONTEXT_KEY_PREFIX):] or None
        contexts.append((movement_id, data))
    return contexts, corrupt


async def scan_contexts(client: Any) -> tuple[list[tuple[str | None, dict]], int, int]:
    """Every convoy context payload (sorted keys, bounded by MAX_CONTEXTS).

    Returns ``(contexts, truncated_after, corrupt)``.
    """
    keys, truncated_after = await scan_keys(client, CONTEXT_KEY_PREFIX, max_keys=MAX_CONTEXTS)
    contexts, corrupt = await read_contexts(client, keys)
    return contexts, truncated_after, corrupt


def decisions_of(payload: dict) -> list[dict]:
    rows = payload.get("decisions_log")
    return rows if isinstance(rows, list) else []


async def _scan_contexts(client: Any) -> tuple[list[tuple[str | None, list[dict]]], int, int]:
    """Read every convoy context's decisions_log from Valkey.

    Returns ``(contexts, truncated_after, corrupt)`` where ``contexts``
    holds ``(movement_id, rows)`` pairs (rows default to [] when the
    payload carries none), ``truncated_after`` is the number of keys the
    scan found beyond :data:`MAX_CONTEXTS`, and ``corrupt`` counts
    payloads that failed to parse.
    """
    contexts, truncated_after, corrupt = await scan_contexts(client)
    return [(mid, decisions_of(data)) for mid, data in contexts], truncated_after, corrupt


@router.get("/degradation-taxonomy")
async def degradation_taxonomy(request: Request) -> dict:
    """Recurring degradation/failure causes mined from decisions_log."""
    client = getattr(request.app.state, "adaptive_valkey", None)
    if client is None:
        raise HTTPException(status_code=503, detail="valkey client unavailable")

    try:
        contexts, truncated_after, corrupt = await _scan_contexts(client)
    except Exception as exc:  # noqa: BLE001 — any Valkey failure is a 503
        logger.warning("ops.degradation_taxonomy_scan_failed", error=repr(exc))
        raise HTTPException(
            status_code=503, detail="decisions_log scan failed (valkey unreachable)"
        ) from exc

    taxonomy = build_taxonomy(contexts)
    return {
        **taxonomy,
        "truncated": truncated_after > 0,
        "contexts_skipped_corrupt": corrupt,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": (
            f"valkey {CONTEXT_KEY_PREFIX}* decisions_log "
            "(convoy-brain ConvoyContextStore)"
        ),
    }


__all__ = [
    "CONTEXT_KEY_PREFIX",
    "MAX_CONTEXTS",
    "context_key",
    "decisions_of",
    "read_contexts",
    "router",
    "scan_contexts",
]
