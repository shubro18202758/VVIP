"""Agency dispatch HTTP surface — Jan Rakshak first-class (signalling-janrakshak).

The dispatch channel MIRRORS the green-corridor request pattern
(``frontend/src/views/GreenCorridor.jsx`` — one tap requests, the platform
evaluates, a duty officer APPROVES or DEFERS, the window opens):

    POST /api/v1/events/{event_id}/dispatch
         {agency, unit_id, waypoint_junction?, requested_by?}
         → dispatch row, status 'requested' (+ audit row).

    POST /api/v1/dispatches/{dispatch_id}/decision
         {action: 'approve' | 'defer', officer, note?}
         → the HUMAN decision half: 'approved' (window to move) or
           'deferred' (terminal; re-request with a new dispatch).
           No agency unit ever moves without this human approval.

    POST /api/v1/dispatches/{dispatch_id}/status
         {status: en_route|on_scene|completed|cancelled, officer, note?}
         → lifecycle walk after approval.

    GET  /api/v1/dispatches?event_id=&agency=&status=&limit=
         → dispatches with their append-only audit tails (011
           corridor.dispatch_events — every state change is a row).

    GET  /api/v1/units
         → EMULATED agency units for the canvas (C1 contract):
           {unit_id, agency, label, position, waypoint, assigned_event_id,
            status, eta_sec, arrived, emulated: true, source}.  Position is
           the deterministic projection (units/projection.py) of the
           approved dispatch along the corridor graph — a total function of
           (dispatch_id, approved_at, now), badged emulated everywhere.

Transition table (pure, enforced + unit-tested):

    requested  → approved | deferred | cancelled
    approved   → en_route | cancelled
    en_route   → on_scene | cancelled
    on_scene   → completed | cancelled
    deferred | completed | cancelled → (terminal)

SQL hygiene: every WRITE statement is a LOCAL single-line string literal
with $n placeholders at its call site (house style — see
signals/writer.py docstring); reads are module constants.  No outbound
HTTP.  No person fields (C6 — vehicles/agencies only, never persons).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from traffic_oracle.units.agencies import AGENCIES, agency_label, is_agency
from traffic_oracle.units.projection import (
    dispatch_seed,
    offset_position,
    seeded_bearing_offset_deg,
    seeded_speed_kmh,
    travel_state,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["units"])

# Valid lifecycle transitions (pure table — mirrored in tests).
DISPATCH_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "requested": ("approved", "deferred", "cancelled"),
    "approved": ("en_route", "cancelled"),
    "en_route": ("on_scene", "cancelled"),
    "on_scene": ("completed", "cancelled"),
    "deferred": (),
    "completed": (),
    "cancelled": (),
}

# Statuses whose dispatches project LIVE emulated units.
LIVE_UNIT_STATUSES = ("approved", "en_route", "on_scene")

# ---------------------------------------------------------------------------
# Read SQL (module constants — parameter-bound)
# ---------------------------------------------------------------------------

_SQL_ACTIVE_DISPATCHES = "SELECT d.dispatch_id, d.event_id, d.agency, d.unit_id, d.waypoint_junction, d.status, d.requested_by, d.decided_by, d.approved_at, d.created_at FROM corridor.dispatches d WHERE d.status = ANY($1::text[]) ORDER BY d.approved_at NULLS FIRST, d.created_at LIMIT $2"  # noqa: E501

_SQL_EVENT_CHECK = "SELECT event_id, status FROM events.corridor_events WHERE event_id = $1::uuid"  # noqa: E501

_SQL_EVENT_SPINE_START = "SELECT r.segment_ids FROM events.event_routes r WHERE r.event_id = $1::uuid ORDER BY r.route_rank LIMIT 1"  # noqa: E501

# corridor.road_graph nodes are SEGMENT ids (001/seed; pgr_dijkstra over
# road_graph therefore speaks segments, not junctions) — the unit path is
# segment→segment, and the waypoint is resolved to its adjacent segment.
_SQL_JUNCTION_ADJACENT_SEGMENT = "SELECT sa.from_segment_id AS segment_id FROM corridor.segment_adjacency sa WHERE sa.via_junction_id = $1 ORDER BY sa.from_segment_id LIMIT 1"  # noqa: E501

_SQL_SEGMENT_POINTS = "SELECT rs.segment_id, ST_X(ST_PointOnSurface(rs.geom)) AS lon, ST_Y(ST_PointOnSurface(rs.geom)) AS lat FROM corridor.road_segments rs WHERE rs.segment_id = ANY($1::bigint[])"  # noqa: E501

_SQL_JUNCTION_GEOM = "SELECT j.junction_id, ST_X(j.geom) AS lon, ST_Y(j.geom) AS lat FROM corridor.junctions j WHERE j.junction_id = ANY($1::bigint[])"  # noqa: E501

_SQL_DISPATCH_LIST = "SELECT d.dispatch_id, d.event_id, d.agency, d.unit_id, d.waypoint_junction, d.status, d.requested_by, d.decided_by, d.decision_note, d.approved_at, d.created_at, d.updated_at FROM corridor.dispatches d WHERE ($1::uuid IS NULL OR d.event_id = $1::uuid) AND ($2::text IS NULL OR d.agency = $2::text) AND ($3::text IS NULL OR d.status = $3::text) ORDER BY d.created_at DESC LIMIT $4"  # noqa: E501

_SQL_DISPATCH_GET = "SELECT d.dispatch_id, d.event_id, d.agency, d.unit_id, d.waypoint_junction, d.status, d.requested_by, d.decided_by, d.decision_note, d.approved_at, d.created_at, d.updated_at FROM corridor.dispatches d WHERE d.dispatch_id = $1::uuid"  # noqa: E501

_SQL_AUDIT_TAIL = "SELECT e.from_status, e.to_status, e.actor, e.note, e.at FROM corridor.dispatch_events e WHERE e.dispatch_id = $1::uuid ORDER BY e.seq DESC LIMIT $2"  # noqa: E501


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DispatchRequest(BaseModel):
    """POST /events/{id}/dispatch — the REQUEST half of the pattern."""

    agency: str
    unit_id: str = Field(min_length=1, max_length=64)
    waypoint_junction: int | None = Field(default=None, ge=1)
    requested_by: str = Field(default="operator", max_length=64)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("agency")
    @classmethod
    def _valid_agency(cls, v: str) -> str:
        if not is_agency(v):
            valid = sorted(AGENCIES)
            raise ValueError(f"agency must be one of {valid}")
        return v


class DispatchDecisionRequest(BaseModel):
    """POST /dispatches/{id}/decision — the HUMAN approve/defer half."""

    action: str
    officer: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("action")
    @classmethod
    def _valid_action(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in ("approve", "defer"):
            raise ValueError("action must be 'approve' or 'defer'")
        return normalized


class DispatchStatusRequest(BaseModel):
    """POST /dispatches/{id}/status — post-approval lifecycle."""

    status: str
    officer: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in ("en_route", "on_scene", "completed", "cancelled"):
            raise ValueError(
                "status must be en_route|on_scene|completed|cancelled"
            )
        return v


def _coerce_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _audit(conn: Any, dispatch_id: str, from_status: str | None, to_status: str, actor: str, note: str | None) -> None:  # noqa: E501
    """Append one row to the dispatch audit tail (parameter-bound)."""
    audit_insert = "INSERT INTO corridor.dispatch_events (dispatch_id, from_status, to_status, actor, note) VALUES ($1, $2, $3, $4, $5)"  # noqa: E501
    await conn.execute(audit_insert, dispatch_id, from_status, to_status, actor, note)


def _dispatch_out(r: Any) -> dict:
    return {
        "dispatch_id": str(r["dispatch_id"]),
        "event_id": str(r["event_id"]),
        "agency": r["agency"],
        "agency_label": agency_label(r["agency"]),
        "unit_id": r["unit_id"],
        "waypoint_junction": (
            int(r["waypoint_junction"]) if r["waypoint_junction"] else None
        ),
        "status": r["status"],
        "requested_by": r["requested_by"],
        "decided_by": r["decided_by"],
        "decision_note": r["decision_note"],
        "approved_at": (
            _coerce_utc(r["approved_at"]).isoformat() if r["approved_at"] else None
        ),
        "created_at": (
            _coerce_utc(r["created_at"]).isoformat() if r["created_at"] else None
        ),
        "updated_at": (
            _coerce_utc(r["updated_at"]).isoformat() if r["updated_at"] else None
        ),
    }


async def _get_dispatch(conn: Any, dispatch_id: str) -> dict | None:
    row = await conn.fetchrow(_SQL_DISPATCH_GET, dispatch_id)
    return _dispatch_out(row) if row else None


async def _audit_tail(conn: Any, dispatch_id: str, limit: int = 20) -> list[dict]:
    rows = await conn.fetch(_SQL_AUDIT_TAIL, dispatch_id, limit)
    return [
        {
            "from_status": r["from_status"],
            "to_status": r["to_status"],
            "actor": r["actor"],
            "note": r["note"],
            "at": _coerce_utc(r["at"]).isoformat(),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# POST /events/{event_id}/dispatch — REQUEST
# ---------------------------------------------------------------------------


@router.post("/events/{event_id}/dispatch")
async def create_dispatch(
    event_id: str, req: DispatchRequest, request: Request
) -> dict:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    try:
        eid = str(uuid.UUID(event_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid event_id") from exc

    async with pool.acquire() as conn:
        event = await conn.fetchrow(_SQL_EVENT_CHECK, eid)
        if event is None:
            raise HTTPException(status_code=404, detail="event not found")
        if event["status"] not in ("planning", "approved", "active"):
            raise HTTPException(
                status_code=409,
                detail="event is not active (status=" + str(event["status"]) + ")",
            )

        dispatch_insert = "INSERT INTO corridor.dispatches (event_id, agency, unit_id, waypoint_junction, status, requested_by, request_payload) VALUES ($1, $2, $3, $4, 'requested', $5, $6::jsonb) RETURNING dispatch_id, created_at"  # noqa: E501
        payload = {"note": req.note} if req.note else {}
        payload_json = json.dumps(payload)
        row = await conn.fetchrow(
            dispatch_insert,
            eid,
            req.agency,
            req.unit_id,
            req.waypoint_junction,
            req.requested_by,
            payload_json,
        )
        dispatch_id = str(row["dispatch_id"])
        await _audit(conn, dispatch_id, None, "requested", req.requested_by, req.note)

    logger.info(
        "dispatch.requested",
        dispatch_id=dispatch_id,
        agency=req.agency,
        event_id=eid,
        unit=req.unit_id,
    )
    return {
        "dispatch_id": dispatch_id,
        "event_id": eid,
        "agency": req.agency,
        "agency_label": agency_label(req.agency),
        "unit_id": req.unit_id,
        "status": "requested",
        "next": "POST /api/v1/dispatches/{id}/decision (approve|defer) by a duty officer",  # noqa: E501
    }


# ---------------------------------------------------------------------------
# POST /dispatches/{dispatch_id}/decision — the HUMAN approve/defer half
# ---------------------------------------------------------------------------


@router.post("/dispatches/{dispatch_id}/decision")
async def decide_dispatch(
    dispatch_id: str, req: DispatchDecisionRequest, request: Request
) -> dict:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    try:
        did = str(uuid.UUID(dispatch_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid dispatch_id") from exc

    to_status = "approved" if req.action == "approve" else "deferred"
    decision_update = "UPDATE corridor.dispatches SET status = $1, decided_by = $2, decision_note = $3, approved_at = CASE WHEN $1 = 'approved' THEN NOW() ELSE approved_at END, updated_at = NOW() WHERE dispatch_id = $4 AND status = 'requested' RETURNING dispatch_id"  # noqa: E501

    async with pool.acquire() as conn:
        row = await conn.fetchrow(decision_update, to_status, req.officer, req.note, did)
        if row is None:
            current = await _get_dispatch(conn, did)
            if current is None:
                raise HTTPException(status_code=404, detail="dispatch not found")
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "dispatch not in requested state",
                    "status": current["status"],
                },
            )
        await _audit(conn, did, "requested", to_status, req.officer, req.note)
        result = await _get_dispatch(conn, did)
        result["audit"] = await _audit_tail(conn, did)

    logger.info(
        "dispatch.decision",
        dispatch_id=did,
        decision=to_status,
        officer=req.officer,
    )
    return result


# ---------------------------------------------------------------------------
# POST /dispatches/{dispatch_id}/status — lifecycle
# ---------------------------------------------------------------------------


@router.post("/dispatches/{dispatch_id}/status")
async def advance_dispatch(
    dispatch_id: str, req: DispatchStatusRequest, request: Request
) -> dict:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    try:
        did = str(uuid.UUID(dispatch_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid dispatch_id") from exc

    async with pool.acquire() as conn:
        current = await _get_dispatch(conn, did)
        if current is None:
            raise HTTPException(status_code=404, detail="dispatch not found")
        allowed = DISPATCH_TRANSITIONS.get(current["status"], ())
        if req.status not in allowed:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "invalid transition",
                    "from": current["status"],
                    "to": req.status,
                    "allowed": list(allowed),
                },
            )
        status_update = "UPDATE corridor.dispatches SET status = $1, updated_at = NOW() WHERE dispatch_id = $2 AND status = $3 RETURNING dispatch_id"  # noqa: E501
        row = await conn.fetchrow(status_update, req.status, did, current["status"])
        if row is None:
            raise HTTPException(status_code=409, detail="dispatch state changed")
        await _audit(conn, did, current["status"], req.status, req.officer, req.note)
        result = await _get_dispatch(conn, did)
        result["audit"] = await _audit_tail(conn, did)
    return result


# ---------------------------------------------------------------------------
# GET /dispatches — list (ops board)
# ---------------------------------------------------------------------------


@router.get("/dispatches")
async def list_dispatches(
    request: Request,
    event_id: str | None = Query(default=None, max_length=64),
    agency: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    eid = None
    if event_id:
        try:
            eid = str(uuid.UUID(event_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid event_id") from exc
    if agency and not is_agency(agency):
        raise HTTPException(status_code=400, detail="unknown agency")
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SQL_DISPATCH_LIST, eid, agency, status, limit)
        dispatches = [_dispatch_out(r) for r in rows]
        for d in dispatches:
            d["audit"] = await _audit_tail(conn, d["dispatch_id"], limit=10)
    return {"dispatches": dispatches, "count": len(dispatches)}


# ---------------------------------------------------------------------------
# GET /units — emulated agency units (C1 canvas contract)
# ---------------------------------------------------------------------------


@router.get("/units")
async def list_units(request: Request) -> dict:
    """EMULATED units projected from approved dispatches (deterministic).

    Position = projection.dispatch projection along the graph path from the
    event's staging junction to the dispatch waypoint, a pure function of
    (dispatch_id, approved_at, now).  Every unit carries
    ``emulated: true`` and its source label — the badge is never optional.
    """
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")

    rows = await pool.fetch(_SQL_ACTIVE_DISPATCHES, list(LIVE_UNIT_STATUSES), 100)
    if not rows:
        return {"units": [], "count": 0, "source": "emulated_dispatch_projection"}

    now = datetime.now(timezone.utc)
    units: list[dict] = []
    async with pool.acquire() as conn:
        # Caches: segment mid-points and junction geoms for this query.
        seg_cache: dict[int, tuple[float, float]] = {}
        junc_cache: dict[int, tuple[float, float]] = {}

        async def segment_points(ids: list[int]) -> dict[int, tuple[float, float]]:
            missing = [i for i in ids if i not in seg_cache]
            if missing:
                for row in await conn.fetch(_SQL_SEGMENT_POINTS, missing):
                    seg_cache[int(row["segment_id"])] = (
                        float(row["lon"]),
                        float(row["lat"]),
                    )
            return {i: seg_cache[i] for i in ids if i in seg_cache}

        async def junction_lonlat(jid: int) -> tuple[float, float] | None:
            if jid in junc_cache:
                return junc_cache[jid]
            found = await conn.fetch(_SQL_JUNCTION_GEOM, [jid])
            if not found:
                return None
            pt = (float(found[0]["lon"]), float(found[0]["lat"]))
            junc_cache[jid] = pt
            return pt

        graph_db = getattr(request.app.state, "graph_db", None)

        for r in rows:
            dispatch_id = str(r["dispatch_id"])
            waypoint = r["waypoint_junction"]
            approved_at = r["approved_at"]
            unit: dict[str, Any] = {
                "unit_id": r["unit_id"],
                "dispatch_id": dispatch_id,
                "agency": r["agency"],
                "label": agency_label(r["agency"]) + " · " + str(r["unit_id"]),
                "assigned_event_id": str(r["event_id"]),
                "status": r["status"],
                "emulated": True,
                "source": "emulated_dispatch_projection",
            }
            if waypoint is None or approved_at is None:
                # No waypoint/no approval clock: stationary, still honest
                # about being emulated.
                unit.update(
                    {"position": None, "eta_sec": None, "arrived": False}
                )
                units.append(unit)
                continue

            seed = dispatch_seed(dispatch_id)
            speed = seeded_speed_kmh(seed)
            elapsed = max(0.0, (now - _coerce_utc(approved_at)).total_seconds())

            # Path in the road_graph's id space (SEGMENT ids): the event's
            # first spine segment → the waypoint junction's adjacent
            # segment.  Deterministic; degrades to the waypoint itself.
            staging_pt: tuple[float, float] | None = None
            path_pts: list[tuple[float, float]] = []
            path_costs: list[float] = []
            spine = await conn.fetchrow(_SQL_EVENT_SPINE_START, str(r["event_id"]))
            wp_seg_row = await conn.fetchrow(
                _SQL_JUNCTION_ADJACENT_SEGMENT, int(waypoint)
            )
            if spine and spine["segment_ids"] and wp_seg_row and wp_seg_row["segment_id"]:
                first_seg = int(list(spine["segment_ids"])[0])
                wp_seg = int(wp_seg_row["segment_id"])
                start_pt = (await segment_points([first_seg])).get(first_seg)
                if start_pt is not None:
                    staging_pt = start_pt
                if graph_db is not None and wp_seg != first_seg:
                    try:
                        path_rows = await graph_db.shortest_path(first_seg, wp_seg)
                        node_ids = [int(p["node"]) for p in path_rows]
                    except Exception:  # noqa: BLE001 — degrade to staging
                        node_ids = []
                    if len(node_ids) >= 2:
                        pts_by_seg = await segment_points(node_ids)
                        path_pts = [pts_by_seg[n] for n in node_ids if n in pts_by_seg]
                        # pgr edge costs are not wall-seconds (unit-ambiguous);
                        # the projection derives per-leg durations from the
                        # geometry at the unit's seeded speed instead —
                        # deterministic and physically meaningful.
                        path_costs = []

            if len(path_pts) >= 2:
                state = travel_state(path_pts, path_costs, elapsed, speed)
                if state is not None:
                    unit.update(
                        {
                            "position": list(
                                offset_position(
                                    state.position,
                                    seeded_bearing_offset_deg(seed),
                                )
                            ),
                            "progress": state.progress,
                            "eta_sec": state.remaining_sec,
                            "arrived": state.arrived,
                            "speed_kmh": speed,
                        }
                    )
                    units.append(unit)
                    continue

            # No graph path: stage at the spine start (else the waypoint),
            # arrived=True only when the two coincide.
            hold_pt = staging_pt
            if hold_pt is None:
                hold_pt = await junction_lonlat(int(waypoint))
            same_place = False
            wp_seg2 = await conn.fetchrow(
                _SQL_JUNCTION_ADJACENT_SEGMENT, int(waypoint)
            )
            if spine and spine["segment_ids"] and wp_seg2 and wp_seg2["segment_id"]:
                same_place = int(list(spine["segment_ids"])[0]) == int(
                    wp_seg2["segment_id"]
                )
            if hold_pt is not None:
                unit.update(
                    {
                        "position": list(hold_pt),
                        "progress": 1.0 if same_place else 0.0,
                        "eta_sec": 0.0 if same_place else None,
                        "arrived": same_place,
                        "speed_kmh": speed,
                    }
                )
            else:
                unit.update({"position": None, "eta_sec": None, "arrived": False})
            units.append(unit)

    return {
        "units": units,
        "count": len(units),
        "source": "emulated_dispatch_projection",
        "note": "emulated units — deterministic from dispatch_id; live agency_unit frames will take precedence when the C2 ingest lands",  # noqa: E501
    }


__all__ = ["DISPATCH_TRANSITIONS", "LIVE_UNIT_STATUSES", "router"]
