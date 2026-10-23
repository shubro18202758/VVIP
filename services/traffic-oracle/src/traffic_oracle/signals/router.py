"""Signals HTTP surface — city signal state + gated plan writer (C1/C7).

Endpoints (mounted under ``/api/v1`` by ``__main__``):

    GET  /signals/state
         C1 canvas contract: every programmed junction with lon/lat,
         cycle, active phase, green ratio and source.

    GET  /signals/plans
         Writer posture: enabled flag, city band (aggregated from the
         adaptive layer), the documented band→plan table, the in-process
         last-plan report and the gate occupancy snapshot.

    POST /signals/plans/apply     {band?, event_id?, junction_ids?,
                                   route_segment_ids?, officer?}
         Operator action.  band defaults to the live city band — but only
         while that band is KNOWN: with the situation engine's poll
         failing (stale band) or no adaptive service at all, an apply with
         no explicit band is a 409 'city band unknown — pass band
         explicitly' (never a silent GREEN default).  Honors the full C7
         gate chain (flag → exclusive gate → pure plan → twin shadow-eval
         fail-closed → snapshot+commit).

City-band provenance (C-Q, additive on /state and /plans):
``city_band_since`` (wall time of the last band change),
``city_band_simulated_feed`` (the driving subject's band stands on the
simulated feed / emulated inputs), ``city_band_stale`` and
``city_band_stale_since`` (the engine poll is failing — the band is
unknown, not at rest).  All four are null when no adaptive service is
wired; ``city_band`` keeps its contract value either way.

    POST /signals/restore         {event_id? | all: true, officer?}
         Rebuild ``osm_assumed`` programs from the earliest snapshot per
         junction (reversible demo state).

Attribution (C6 named human actor): ``officer`` (2..64 chars, no control
characters) is persisted on every write — the apply's
``signal_plan_log`` row, its first-write snapshot rows and every restore
row (``signal_program_history.actor``), migration 013.  It is optional so
older decks keep working, and REQUIRED (422 when absent) once
``VVIP_REQUIRE_OFFICER=1``.  The deck-verified ``x-veer-officer`` header
(loopback only — units/router.py) comes first: when present it IS the
recorded actor and a differing body officer is only a claim (in the
response and the log).  ``event_id`` must be a corridor-event uuid (422
otherwise — a fixture slug never reaches the ``::uuid`` cast).

    GET  /signals/history?limit=
         The append-only snapshot/restore ledger.

The router never instantiates I/O itself: the asyncpg pool is looked up
from ``app.state.pool``; the city band comes from ``app.state.adaptive``
(the situation engine's live per-subject states).
"""

from __future__ import annotations

import os
import time
import uuid as _uuid
from typing import Any

import structlog
from asyncpg.exceptions import UndefinedColumnError, UndefinedTableError
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from traffic_oracle.city.common import named_actor
from traffic_oracle.signals import writer as signal_writer
from traffic_oracle.signals.gate import signal_policy_gate
from traffic_oracle.signals.plans import (
    active_phase,
    band_plan_table_doc,
    city_band_from_status,
    green_ratio,
)
from traffic_oracle.units.router import attributed_actor, authenticated_officer

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/signals", tags=["signals"])

_VALID_BANDS = ("GREEN", "AMBER", "RED", "BLACK")

#: When truthy, apply/restore refuse to write without a named officer.
REQUIRE_OFFICER_ENV = "VVIP_REQUIRE_OFFICER"
_TRUTHY = {"1", "true", "yes", "on"}


def officer_required() -> bool:
    """VVIP_REQUIRE_OFFICER=1 ⇒ apply/restore demand a named officer."""
    return os.environ.get(REQUIRE_OFFICER_ENV, "").strip().lower() in _TRUTHY


def _event_uuid(v: str | None) -> str | None:
    """Corridor-event ids are uuids (signal_program_history.event_id)."""
    if v is None:
        return None
    try:
        return str(_uuid.UUID(v.strip()))
    except ValueError as exc:
        raise ValueError(
            "event_id must be a corridor-event uuid (preview fixtures have none)"
        ) from exc


def _officer(v: str | None) -> str | None:
    """The shared named-actor rule (2..64 chars, no control characters)."""
    return named_actor(v)


#: The 409 an apply without an explicit band gets while the band is unknown.
CITY_BAND_UNKNOWN = "city band unknown — pass band explicitly"

#: The additive provenance keys of the city band (C-Q) — all None when no
#: adaptive service is wired (or it cannot answer).
_BAND_PROVENANCE_KEYS = (
    "city_band_since",
    "city_band_simulated_feed",
    "city_band_stale",
    "city_band_stale_since",
)


def _city_band_doc(request: Request) -> dict[str, Any]:
    """The live city band with its provenance, from the adaptive service.

    ``service.city_band()`` is the source (band, since, simulated_feed,
    stale, stale_since); only a service without it falls back to the
    max-band of ``status()['movements']``.  ``known`` is False when no
    service is wired, it cannot answer, or its poll is failing (stale) —
    the band then defaults to GREEN for the read surfaces but never drives
    an apply on its own.
    """
    doc: dict[str, Any] = {"band": "GREEN", "subjects": 0, "known": False}
    doc.update(dict.fromkeys(_BAND_PROVENANCE_KEYS))
    service = getattr(request.app.state, "adaptive", None)
    if service is None:
        return doc
    try:
        city_band = getattr(service, "city_band", None)
        if callable(city_band):
            band = city_band()
            stale = bool(band.get("stale"))
            doc.update(
                {
                    "band": str(band.get("band") or "GREEN"),
                    "subjects": int(band.get("subjects") or 0),
                    "known": not stale,
                    "city_band_since": band.get("since"),
                    "city_band_simulated_feed": band.get("simulated_feed"),
                    "city_band_stale": stale,
                    "city_band_stale_since": band.get("stale_since"),
                }
            )
            return doc
        movements = service.status().get("movements", {})
    except Exception:  # noqa: BLE001 — band telemetry must not 500
        return doc
    doc.update(
        {"band": city_band_from_status(movements), "subjects": len(movements), "known": True}
    )
    return doc


# ---------------------------------------------------------------------------
# Read-only SQL (module constants — parameter-bound, reads only)
# ---------------------------------------------------------------------------

_SQL_STATE = "SELECT sp.junction_id, sp.cycle_sec, sp.phases, sp.offset_sec, sp.source, ST_X(j.geom) AS lon, ST_Y(j.geom) AS lat FROM corridor.signal_programs sp JOIN corridor.junctions j ON j.junction_id = sp.junction_id ORDER BY sp.junction_id"  # noqa: E501


class PlanApplyRequest(BaseModel):
    """POST /signals/plans/apply — operator-triggered city plan."""

    band: str | None = Field(
        default=None, description="GREEN|AMBER|RED|BLACK; default = live city band"
    )
    event_id: str | None = Field(
        default=None,
        description="corridor event the application serves (attribution)",
        max_length=64,
    )
    junction_ids: list[int] | None = Field(
        default=None,
        description="optional scope (default: every programmed junction)",
        max_length=512,
    )
    route_segment_ids: list[int] | None = Field(
        default=None,
        description="optional twin shadow-eval route (default: derived)",
        max_length=64,
    )
    officer: str | None = Field(
        default=None,
        description=(
            "named officer (2..64 chars) recorded on the history rows; "
            "required when VVIP_REQUIRE_OFFICER=1"
        ),
    )

    @field_validator("band")
    @classmethod
    def _valid_band(cls, v: str | None) -> str | None:
        if v is None:
            return None
        normalized = v.strip().upper()
        if normalized not in _VALID_BANDS:
            raise ValueError(f"band must be one of {_VALID_BANDS}")
        return normalized

    @field_validator("event_id")
    @classmethod
    def _valid_event_id(cls, v: str | None) -> str | None:
        return _event_uuid(v)

    @field_validator("officer")
    @classmethod
    def _valid_officer(cls, v: str | None) -> str | None:
        return _officer(v)


class RestoreRequest(BaseModel):
    """POST /signals/restore — rebuild osm_assumed programs."""

    event_id: str | None = Field(default=None, max_length=64)
    all: bool = False
    officer: str | None = Field(
        default=None,
        description="named officer (2..64 chars); required when VVIP_REQUIRE_OFFICER=1",
    )

    @field_validator("event_id")
    @classmethod
    def _valid_event_id(cls, v: str | None) -> str | None:
        return _event_uuid(v)

    @field_validator("officer")
    @classmethod
    def _valid_officer(cls, v: str | None) -> str | None:
        return _officer(v)


def _require_officer(officer: str | None) -> None:
    if officer is None and officer_required():
        raise HTTPException(
            status_code=422,
            detail=(
                "officer is required for city signal writes "
                f"({REQUIRE_OFFICER_ENV}=1) — a named human actor (C6)"
            ),
        )


_LEDGER_MISSING = (
    "signal attribution ledger missing (signal_program_history.actor / "
    "signal_plan_log) — apply migration 013 (scripts/migrate.py) before "
    "signal writes"
)

#: Raised inside the writer's transaction when migration 013 has not been
#: applied: the transaction rolls back, so NOTHING was written.
_LEDGER_ERRORS = (UndefinedColumnError, UndefinedTableError)


# ---------------------------------------------------------------------------
# GET /signals/state — the C1 canvas contract
# ---------------------------------------------------------------------------


@router.get("/state")
async def signals_state(request: Request) -> dict:
    """Every programmed junction: position, cycle, ACTIVE phase, ratio."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    try:
        rows = await pool.fetch(_SQL_STATE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("signals.state_unavailable", error=repr(exc))
        raise HTTPException(
            status_code=503,
            detail="signal programs unavailable (migration 006/011 applied?)",
        ) from exc

    now_sec = time.time()
    junctions: list[dict[str, Any]] = []
    for r in rows:
        phases = signal_writer._parse_phases(r["phases"])  # noqa: SLF001 — same package
        cycle = int(r["cycle_sec"])
        offset = int(r["offset_sec"] or 0)
        phase = active_phase(phases, cycle, offset, now_sec)
        junctions.append(
            {
                "junction_id": int(r["junction_id"]),
                "lon": float(r["lon"]),
                "lat": float(r["lat"]),
                "cycle_sec": cycle,
                "offset_sec": offset,
                "active_phase": phase,
                "green_ratio": round(green_ratio(phases, cycle), 4),
                "source": r["source"],
            }
        )
    band = _city_band_doc(request)
    return {
        "junctions": junctions,
        "count": len(junctions),
        "city_band": band["band"],
        "band_subjects": band["subjects"],
        # additive (C-Q): since / simulated feed / stale (null without AMA)
        **{k: band[k] for k in _BAND_PROVENANCE_KEYS},
        "writer_enabled": signal_writer.writer_enabled(),
        "epoch_sec": now_sec,
    }


# ---------------------------------------------------------------------------
# GET /signals/plans — writer posture (ops board)
# ---------------------------------------------------------------------------


@router.get("/plans")
async def signals_plans(request: Request) -> dict:
    band = _city_band_doc(request)
    return {
        "writer_enabled": signal_writer.writer_enabled(),
        "writer_flag_env": signal_writer.WRITER_FLAG_ENV,
        "city_band": band["band"],
        "band_subjects": band["subjects"],
        # additive (C-Q): since / simulated feed / stale (null without AMA)
        **{k: band[k] for k in _BAND_PROVENANCE_KEYS},
        "band_plan_table": band_plan_table_doc(),
        "last_plan": signal_writer.last_plan_report(),
        "gate": signal_policy_gate.snapshot(),
        "scope": "city (all programmed junctions); per-junction RL stays corridor-scoped",  # noqa: E501
    }


# ---------------------------------------------------------------------------
# POST /signals/plans/apply — the gated writer
# ---------------------------------------------------------------------------


def _signal_actor(request: Request, body_officer: str | None) -> tuple[str | None, dict | None]:
    """``(actor, provenance)`` for a city signal write: the deck-verified
    header first (units/router.py), else the body officer — which may be
    None (unattributed) unless VVIP_REQUIRE_OFFICER=1."""
    header = authenticated_officer(request)
    if header is None:
        _require_officer(body_officer)
        return body_officer, None
    return attributed_actor(header, body_officer)


def _actor_fields(detail: dict | None, actor: str | None) -> dict:
    return {
        "actor": actor,
        "actor_source": (detail or {}).get("actor_source", "body" if actor else None),
        "claimed_officer": (detail or {}).get("claimed_officer"),
    }


@router.post("/plans/apply")
async def signals_plans_apply(req: PlanApplyRequest, request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    actor, detail = _signal_actor(request, req.officer)
    band = req.band
    if band is None:
        city = _city_band_doc(request)
        if not city["known"]:
            # no silent GREEN default: a stale or absent band is UNKNOWN —
            # nothing is planned, nothing written
            return JSONResponse(
                status_code=409,
                content={
                    "detail": CITY_BAND_UNKNOWN,
                    "city_band_stale": city["city_band_stale"],
                    "city_band_stale_since": city["city_band_stale_since"],
                    "adaptive_wired": getattr(request.app.state, "adaptive", None)
                    is not None,
                },
            )
        band = city["band"]
    try:
        report = await signal_writer.apply_city_plan(
            pool,
            band=band,
            event_id=req.event_id,
            junction_ids=req.junction_ids,
            route_segment_ids=req.route_segment_ids,
            actor=actor,
        )
    except _LEDGER_ERRORS as exc:  # transaction rolled back: nothing written
        logger.warning("signals.apply_ledger_missing", error=repr(exc))
        raise HTTPException(status_code=503, detail=_LEDGER_MISSING) from exc
    if not report.get("applied"):
        # Fail-closed outcomes are 409 (operator-visible, not a 5xx):
        # the writer refused by design, nothing was written.
        raise HTTPException(status_code=409, detail=report)
    if detail is not None:
        logger.info("signals.apply_attributed", **_actor_fields(detail, actor))
    return {**report, **_actor_fields(detail, actor)}


# ---------------------------------------------------------------------------
# POST /signals/restore
# ---------------------------------------------------------------------------


@router.post("/restore")
async def signals_restore(req: RestoreRequest, request: Request) -> dict:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    if not req.all and not req.event_id:
        raise HTTPException(
            status_code=400, detail="specify event_id or all=true"
        )
    actor, detail = _signal_actor(request, req.officer)
    try:
        result = await signal_writer.restore_programs(
            pool, event_id=req.event_id, restore_all=req.all, actor=actor
        )
    except _LEDGER_ERRORS as exc:  # transaction rolled back: nothing written
        logger.warning("signals.restore_ledger_missing", error=repr(exc))
        raise HTTPException(status_code=503, detail=_LEDGER_MISSING) from exc
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {**result, **_actor_fields(detail, actor)}


# ---------------------------------------------------------------------------
# GET /signals/history
# ---------------------------------------------------------------------------


@router.get("/history")
async def signals_history(
    request: Request, limit: int = Query(default=50, ge=1, le=200)
) -> dict:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    try:
        rows = await signal_writer.list_history(pool, limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("signals.history_unavailable", error=repr(exc))
        raise HTTPException(
            status_code=503, detail="signal program history unavailable"
        ) from exc
    return {"history": rows, "count": len(rows)}


__all__ = ["CITY_BAND_UNKNOWN", "REQUIRE_OFFICER_ENV", "officer_required", "router"]
