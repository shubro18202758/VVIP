"""Adaptive surfacing — situation_log reads + the AMA module card (R9).

Two read-only endpoints, mounted under ``/api/v1`` by ``__main__``:

    GET /adaptive/situation_log?event_id=&limit=
         The ops board's PRIMARY band rung (store.svelte.ts pollBand →
         client.ts fetchSituationLog).  Reads adaptive.situation_log
         (migration 005/007) newest-first; the response shape matches what
         the deck parses: rows carrying situation_class + timestamp_utc +
         severity_score under an ``entries`` key (the client also accepts
         a bare array / ``log`` / ``rows``).

    GET /adaptive/module
         The named 'Active Movement Adaptation' module card (R9): a
         static descriptor of the EXISTING adaptive layer — its stages,
         protocol floors, endpoints and fail-closed posture.  Pure
         documentation-as-data; no behavior change (the R9 ruling: surface,
         don't rebuild).

This module changes no adaptive behavior: it only reads the audit tables
the adaptive layer already writes (parameter-bound, module constants).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Query, Request

from traffic_oracle.adaptive.enums import SecurityTier
from traffic_oracle.adaptive.policy import ADVANCE_CLOSURE_FLOOR_SEC

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/adaptive", tags=["adaptive"])

_SQL_SITUATION_LOG = "SELECT log_id, movement_id, situation_class, situation_code, severity_score, transitioned, protection_class, observed_at FROM adaptive.situation_log WHERE ($1::text IS NULL OR movement_id = $1::text) ORDER BY observed_at DESC LIMIT $2"  # noqa: E501

# ---------------------------------------------------------------------------
# R9 — the named module descriptor (data, not behavior)
# ---------------------------------------------------------------------------

AMA_MODULE: dict = {
    "module": "Active Movement Adaptation",
    "code": "AMA",
    "requirement": "R9",
    "summary": (
        "The city's live self-adjusting layer: every active corridor event "
        "is continuously classified into a situation band, the band moves "
        "through hysteresis before anything changes, protocol floors bound "
        "every published parameter, and any non-baseline plan must first "
        "pass a twin shadow-evaluation — failing closed to baseline."
    ),
    "stages": [
        {
            "name": "Situation",
            "detail": (
                "SituationEngine classifies each active movement/event from "
                "live feed scores (congestion, weather, crowd, anomaly, GPS "
                "age) into GREEN/AMBER/RED/BLACK."
            ),
        },
        {
            "name": "Hysteresis",
            "detail": (
                "Bands do not flicker: a class change must persist through "
                "its dwell window before it is adopted (hysteresis.py)."
            ),
        },
        {
            "name": "Protocol floors",
            "detail": (
                "advance_closure_sec floors Z+ 180 / Z 120 / Y 60 s (X and "
                "'none' none) are applied to EVERY publish, baseline "
                "included (policy.py)."
            ),
        },
        {
            "name": "Twin shadow-eval",
            "detail": (
                "Any deviation from baseline must beat the baseline in a "
                "paired digital-twin run (disruption + ambulance delay "
                "tolerances) or the engine fails closed to baseline "
                "(shadow.py, twin.py)."
            ),
        },
        {
            "name": "Audited persistence",
            "detail": (
                "Every observation and every published param set lands in "
                "adaptive.situation_log / adaptive.param_overrides "
                "(persistence.py) and mirrors to Valkey for the decider."
            ),
        },
        {
            "name": "City signal plans",
            "detail": (
                "The city band (most severe active subject) drives the "
                "gated city signal-plan writer — /api/v1/signals/plans "
                "(default OFF, twin shadow-evaluated, snapshot+restore)."
            ),
        },
    ],
    "protocol_floors_advance_closure_sec": {
        tier.code: floor for tier, floor in ADVANCE_CLOSURE_FLOOR_SEC.items()
    },
    "endpoints": {
        "params": "/api/v1/adaptive/params",
        "status": "/api/v1/adaptive/status",
        "observe": "/api/v1/adaptive/observe",
        "situation_log": "/api/v1/adaptive/situation_log",
        "module_card": "/api/v1/adaptive/module",
        "city_signal_plans": "/api/v1/signals/plans",
    },
    "fail_closed": True,
    "per_junction_rl": "corridor-scoped (untouched by the city writer)",
}


# ---------------------------------------------------------------------------
# GET /adaptive/situation_log
# ---------------------------------------------------------------------------


@router.get("/situation_log")
async def situation_log(
    request: Request,
    event_id: str | None = Query(default=None, max_length=128),
    movement_id: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=120, ge=1, le=500),
) -> dict:
    """Newest-first situation-band audit rows (board primary rung).

    ``event_id`` and ``movement_id`` are the same subject key (events
    generalize movements); either filters, event_id wins.
    """
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    subject = event_id or movement_id
    try:
        rows = await pool.fetch(_SQL_SITUATION_LOG, subject, limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("adaptive.situation_log_unavailable", error=repr(exc))
        raise HTTPException(
            status_code=503, detail="situation_log unavailable"
        ) from exc

    entries = [
        {
            "movement_id": r["movement_id"],
            "situation_class": r["situation_class"],
            "situation_code": r["situation_code"],
            "severity_score": r["severity_score"],
            "transitioned": bool(r["transitioned"]),
            "protection_class": r["protection_class"],
            "timestamp_utc": (
                r["observed_at"].isoformat() if r["observed_at"] else None
            ),
        }
        for r in rows
    ]
    return {"entries": entries, "count": len(entries), "subject": subject}


# ---------------------------------------------------------------------------
# GET /adaptive/module — the R9 module card
# ---------------------------------------------------------------------------


@router.get("/module")
async def ama_module(request: Request) -> dict:
    """The named 'Active Movement Adaptation' module descriptor (R9)."""
    return AMA_MODULE


__all__ = ["AMA_MODULE", "router"]
