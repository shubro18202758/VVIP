"""Adaptive surfacing — situation_log + param_overrides reads + the AMA card (R9).

Three read-only endpoints, mounted under ``/api/v1`` by ``__main__``:

    GET /adaptive/situation_log?event_id=&limit=
         The ops board's PRIMARY band rung (store.svelte.ts pollBand →
         client.ts fetchSituationLog).  Reads adaptive.situation_log
         (migration 005/007) newest-first; the response shape matches what
         the deck parses: rows carrying situation_class + timestamp_utc +
         severity_score under an ``entries`` key (the client also accepts
         a bare array / ``log`` / ``rows``).

    GET /adaptive/overrides?event_id=&limit=
         The shadow-eval evidence trail (contract C-C): every published
         parameter set from adaptive.param_overrides, newest first — the
         params, their source (baseline|retrieved|learned), the twin
         shadow verdict and its precise detail (e.g. a fail-closed
         ``shadow_context_missing``).  This is the proof behind "no
         non-baseline plan without a passing twin shadow-eval".

    GET /adaptive/module
         The named 'Active Movement Adaptation' module card (R9): a
         static descriptor of the EXISTING adaptive layer — its stages,
         protocol floors, endpoints and fail-closed posture.  Pure
         documentation-as-data; no behavior change (the R9 ruling: surface,
         don't rebuild).

This module changes no adaptive behavior: it only reads the audit tables
the adaptive layer already writes (parameter-bound, module constants).

Provenance: every situation_log entry and override row carries an
additive ``emulated`` flag — true when the subject denotes an EMULATED
corridor event (the emulator's scenarios), resolved in one batched query
(city/common.resolve_subjects).  The override row's ``source`` keeps its
contract meaning (the PARAMS source: baseline|retrieved|learned), so the
live/emulated badge rides beside it rather than overloading it.

Input provenance (C-Q): each situation_log entry also says what its band
STOOD ON, read from the logged inputs JSON the engine loop writes —
``inputs_source`` (live_cache | emulator | inputs_doc),
``simulated_feed`` (true when a simulated-feed or emulated row was
behind it), ``data_quality`` (real | synthetic | mixed) and
``inputs_origin`` (live | emulated | mixed).  All null on rows written
before the loop logged them; the boolean is cast only when the JSON value
IS a boolean, so one odd row can never 503 the band rung.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Query, Request

from traffic_oracle.adaptive.policy import ADVANCE_CLOSURE_FLOOR_SEC
from traffic_oracle.city.common import (
    ResolvedSubjects,
    as_dict,
    as_uuid,
    iso_z,
    resolve_subjects,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/adaptive", tags=["adaptive"])

_SQL_SITUATION_LOG = "SELECT log_id, movement_id, situation_class, observed_class, situation_code, severity_score, transitioned, protection_class, observed_at, inputs->>'source' AS inputs_source, CASE WHEN jsonb_typeof(inputs->'simulated_feed') = 'boolean' THEN (inputs->>'simulated_feed')::boolean END AS simulated_feed, inputs->>'data_quality' AS data_quality, inputs->>'inputs_origin' AS inputs_origin FROM adaptive.situation_log WHERE ($1::text IS NULL OR movement_id = $1::text) ORDER BY observed_at DESC LIMIT $2"  # noqa: E501

_SQL_PARAM_OVERRIDES = "SELECT o.override_id, o.movement_id, o.vvip_class, o.protection_class, o.situation_class, o.params, o.source, o.shadow_eval, o.evidence, o.published_at FROM adaptive.param_overrides o WHERE ($1::text IS NULL OR o.movement_id = $1::text) ORDER BY o.published_at DESC, o.override_id DESC LIMIT $2"  # noqa: E501

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
        "overrides": "/api/v1/adaptive/overrides",
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

    # Provenance is garnish on the PRIMARY band rung: if it cannot be
    # resolved the rows still serve, with emulated=null (unknown) — never
    # a guessed "live".
    resolved: ResolvedSubjects | None
    try:
        resolved = await resolve_subjects(pool, [r["movement_id"] for r in rows])
    except Exception as exc:  # noqa: BLE001
        logger.warning("adaptive.situation_log_provenance_unavailable", error=repr(exc))
        resolved = None

    entries = [
        {
            "movement_id": r["movement_id"],
            "situation_class": r["situation_class"],
            # the raw per-tick reading before hysteresis: differs from the
            # committed class while a band change is still dwelling
            "observed_class": r.get("observed_class"),
            "situation_code": r["situation_code"],
            "severity_score": r["severity_score"],
            "transitioned": bool(r["transitioned"]),
            "protection_class": r["protection_class"],
            "timestamp_utc": (
                r["observed_at"].isoformat() if r["observed_at"] else None
            ),
            "emulated": (
                resolved.source_of(r["movement_id"]) == "emulated"
                if resolved is not None
                else None
            ),
            # additive (C-Q): what the band stood on (null on older rows)
            "inputs_source": r.get("inputs_source"),
            "simulated_feed": r.get("simulated_feed"),
            "data_quality": r.get("data_quality"),
            "inputs_origin": r.get("inputs_origin"),
        }
        for r in rows
    ]
    return {"entries": entries, "count": len(entries), "subject": subject}


# ---------------------------------------------------------------------------
# GET /adaptive/overrides — the shadow-eval evidence trail (contract C-C)
# ---------------------------------------------------------------------------


@router.get("/overrides")
async def param_overrides(
    request: Request,
    event_id: str | None = Query(default=None, max_length=128),
    movement_id: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    """Newest-first published parameter sets with their shadow verdicts.

    ``event_id`` / ``movement_id`` are the same adaptive subject key
    (events generalize movements); either filters, event_id wins.  Each
    row names the corridor event it belongs to (``event_id``) when the
    subject is one — directly, or through an event's soft movement link.
    """
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    subject = event_id or movement_id
    if subject is not None and as_uuid(subject) is not None:
        subject = str(as_uuid(subject))  # subject keys are canonical uuids
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SQL_PARAM_OVERRIDES, subject, limit)
            resolved = await resolve_subjects(conn, [r["movement_id"] for r in rows])
    except Exception as exc:  # noqa: BLE001
        logger.warning("adaptive.param_overrides_unavailable", error=repr(exc))
        raise HTTPException(
            status_code=503, detail="param_overrides unavailable"
        ) from exc

    overrides = []
    for r in rows:
        evidence = as_dict(r["evidence"])
        params = as_dict(r["params"])
        events = resolved.events_of(r["movement_id"])
        published = iso_z(r["published_at"])
        overrides.append(
            {
                "published_at": published,
                "event_id": events[0] if events else None,
                "movement_id": r["movement_id"],
                "situation_class": r["situation_class"],
                "params": params,
                "source": r["source"],
                "shadow_eval": r["shadow_eval"],
                "shadow_eval_detail": evidence.get("shadow_eval_detail"),
                # additive: the evidence the ops band panel renders
                "override_id": int(r["override_id"]),
                "timestamp_utc": published,
                "protection_class": r["protection_class"] or r["vvip_class"],
                "floor_applied": evidence.get("floor_applied"),
                "fail_closed_reason": evidence.get("fail_closed_reason"),
                "degradation_tier": evidence.get("degradation_tier"),
                "situation_code": evidence.get("situation_code"),
                "emulated": resolved.source_of(r["movement_id"]) == "emulated",
            }
        )
    return {"overrides": overrides, "count": len(overrides), "subject": subject}


# ---------------------------------------------------------------------------
# GET /adaptive/module — the R9 module card
# ---------------------------------------------------------------------------


@router.get("/module")
async def ama_module(request: Request) -> dict:
    """The named 'Active Movement Adaptation' module descriptor (R9)."""
    return AMA_MODULE


__all__ = ["AMA_MODULE", "router"]
