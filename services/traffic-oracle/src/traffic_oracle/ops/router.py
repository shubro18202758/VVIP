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
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import structlog
from fastapi import APIRouter, HTTPException, Request

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


async def _scan_contexts(client: Any) -> tuple[list[tuple[str | None, list[dict]]], int, int]:
    """Read every convoy context's decisions_log from Valkey.

    Returns ``(contexts, truncated_after, corrupt)`` where ``contexts``
    holds ``(movement_id, rows)`` pairs (rows default to [] when the
    payload carries none), ``truncated_after`` is the number of keys the
    scan found beyond :data:`MAX_CONTEXTS`, and ``corrupt`` counts
    payloads that failed to parse.
    """
    keys: list[str] = []
    async for key in client.scan_iter(match=f"{CONTEXT_KEY_PREFIX}*", count=100):
        keys.append(key)
    truncated_after = max(0, len(keys) - MAX_CONTEXTS)

    contexts: list[tuple[str | None, list[dict]]] = []
    corrupt = 0
    for key in keys[:MAX_CONTEXTS]:
        raw = await client.get(key)
        if raw is None:
            continue
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise TypeError("context payload is not an object")
        except (json.JSONDecodeError, TypeError, ValueError):
            corrupt += 1
            continue
        rows = data.get("decisions_log")
        if not isinstance(rows, list):
            rows = []
        movement_id = data.get("movement_id")
        if not isinstance(movement_id, str) or not movement_id:
            # The key still identifies the subject when the payload lost it.
            movement_id = key[len(CONTEXT_KEY_PREFIX):] or None
        contexts.append((movement_id, rows))
    return contexts, truncated_after, corrupt


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


__all__ = ["CONTEXT_KEY_PREFIX", "MAX_CONTEXTS", "router"]
