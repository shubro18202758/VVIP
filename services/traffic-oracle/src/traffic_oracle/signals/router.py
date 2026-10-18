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
                                   route_segment_ids?}
         Operator action.  band defaults to the live city band.  Honors
         the full C7 gate chain (flag → exclusive gate → pure plan →
         twin shadow-eval fail-closed → snapshot+commit).

    POST /signals/restore         {event_id? | all: true}
         Rebuild ``osm_assumed`` programs from the earliest snapshot per
         junction (reversible demo state).

    GET  /signals/history?limit=
         The append-only snapshot/restore ledger.

The router never instantiates I/O itself: the asyncpg pool is looked up
from ``app.state.pool``; the city band comes from ``app.state.adaptive``
(the situation engine's live per-subject states).
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from traffic_oracle.signals import writer as signal_writer
from traffic_oracle.signals.gate import signal_policy_gate
from traffic_oracle.signals.plans import (
    BAND_PLAN_TABLE,
    active_phase,
    band_plan_table_doc,
    city_band_from_status,
    green_ratio,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/signals", tags=["signals"])

_VALID_BANDS = ("GREEN", "AMBER", "RED", "BLACK")


def _city_band(request: Request) -> tuple[str, int]:
    """Live city band + subject count from the adaptive service."""
    service = getattr(request.app.state, "adaptive", None)
    if service is None:
        return "GREEN", 0
    try:
        status = service.status()
    except Exception:  # noqa: BLE001 — band telemetry must not 500
        return "GREEN", 0
    movements = status.get("movements", {})
    return city_band_from_status(movements), len(movements)


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

    @field_validator("band")
    @classmethod
    def _valid_band(cls, v: str | None) -> str | None:
        if v is None:
            return None
        normalized = v.strip().upper()
        if normalized not in _VALID_BANDS:
            raise ValueError(f"band must be one of {_VALID_BANDS}")
        return normalized


class RestoreRequest(BaseModel):
    """POST /signals/restore — rebuild osm_assumed programs."""

    event_id: str | None = Field(default=None, max_length=64)
    all: bool = False


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
    city_band, subjects = _city_band(request)
    return {
        "junctions": junctions,
        "count": len(junctions),
        "city_band": city_band,
        "band_subjects": subjects,
        "writer_enabled": signal_writer.writer_enabled(),
        "epoch_sec": now_sec,
    }


# ---------------------------------------------------------------------------
# GET /signals/plans — writer posture (ops board)
# ---------------------------------------------------------------------------


@router.get("/plans")
async def signals_plans(request: Request) -> dict:
    city_band, subjects = _city_band(request)
    return {
        "writer_enabled": signal_writer.writer_enabled(),
        "writer_flag_env": signal_writer.WRITER_FLAG_ENV,
        "city_band": city_band,
        "band_subjects": subjects,
        "band_plan_table": band_plan_table_doc(),
        "last_plan": signal_writer.last_plan_report(),
        "gate": signal_policy_gate.snapshot(),
        "scope": "city (all programmed junctions); per-junction RL stays corridor-scoped",  # noqa: E501
    }


# ---------------------------------------------------------------------------
# POST /signals/plans/apply — the gated writer
# ---------------------------------------------------------------------------


@router.post("/plans/apply")
async def signals_plans_apply(req: PlanApplyRequest, request: Request) -> dict:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    band = req.band
    if band is None:
        band, _ = _city_band(request)
    report = await signal_writer.apply_city_plan(
        pool,
        band=band,
        event_id=req.event_id,
        junction_ids=req.junction_ids,
        route_segment_ids=req.route_segment_ids,
    )
    if not report.get("applied"):
        # Fail-closed outcomes are 409 (operator-visible, not a 5xx):
        # the writer refused by design, nothing was written.
        raise HTTPException(status_code=409, detail=report)
    return report


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
    result = await signal_writer.restore_programs(
        pool, event_id=req.event_id, restore_all=req.all
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


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


__all__ = ["router"]
