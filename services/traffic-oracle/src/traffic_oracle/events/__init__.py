"""Event-agnostic corridor events — HTTP surface (core stream, 007).

GET /api/v1/events/active — active events for the ops board.  A corridor
event is a planned operation that takes control of part of the road
network for a window — the generalization of a convoy movement across the
eight use-case archetypes.  Rows live in ``events.corridor_events`` (+
``event_routes`` spines + ``event_phases`` windows, migration 007).
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from traffic_oracle.adaptive.enums import CrowdDensity, SecurityTier

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/events", tags=["events"])

#: The archetypes (migration 007 CHECK + the 010 relaxation mirror this list
#: exactly — contract C3 lockstep with corridor-store migration 010 and the
#: emulator's profiles.EVENT_KINDS).  crime_incident and vehicle_pursuit are
#: the watchlist-anpr additions (010): a reported crime driving corridor
#: operations, and a HUMAN-initiated pursuit of an earmarked vehicle.
EVENT_KINDS: tuple[str, ...] = (
    "protected_movement",
    "procession",
    "mass_gathering",
    "race",
    "ceremonial",
    "egress_surge",
    "emergency_evacuation",
    "work_zone",
    # watchlist-anpr additions (010, C3):
    "crime_incident",
    "vehicle_pursuit",
)

_ACTIVE_EVENTS_SQL = (
    "SELECT e.event_id, e.event_kind, e.protection_class, e.crowd_class, "
    "e.title, e.description, e.status, e.movement_id, "
    "e.planned_start, e.planned_end, e.metadata, e.created_at, "
    "ST_AsGeoJSON(e.zone_geom) AS zone_geojson "
    "FROM events.corridor_events e "
    "WHERE e.status IN ('planning', 'approved', 'active') "
    "ORDER BY e.planned_start NULLS LAST LIMIT 100"
)


def event_inputs_key(event_id: str) -> str:
    """Valkey key the situation-engine loop reads per event (contract C3)."""
    return "adaptive:inputs:" + str(event_id)


def _coerce_ist(dt: datetime) -> datetime:
    """Naive datetimes are interpreted as IST (UTC+5:30), like the twin."""
    if dt.tzinfo is None:
        from datetime import timedelta, timezone

        return dt.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
    return dt


class RouteSpineModel(BaseModel):
    label: str = Field(default="spine")
    segment_ids: list[int] = Field(min_length=1, max_length=64)


class PhaseModel(BaseModel):
    label: str
    window_start: datetime
    window_end: datetime
    closure_policy: dict = Field(default_factory=dict)


class CorridorEventRequest(BaseModel):
    event_kind: str
    protection_class: str = Field(
        default="none",
        description="Z+ | Z | Y | X | none — 'none' is a public/civic event",
    )
    crowd_class: str = Field(
        default="LOW", description="LOW | MEDIUM | HIGH | EXTREME"
    )
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    route_spines: list[RouteSpineModel] = Field(min_length=1, max_length=8)
    phases: list[PhaseModel] = Field(default_factory=list, max_length=32)
    planned_start: datetime | None = None
    planned_end: datetime | None = None
    movement_id: str | None = Field(
        default=None, description="soft link to a legacy convoy movement"
    )
    scenario_id: str | None = Field(
        default=None, description="twin scenario-library key (metadata)"
    )
    # ── watchlist-anpr additions (010, additive) ─────────────────────────
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        max_length=16,
        description=(
            "event metadata (crime_incident: incident_type+severity; "
            "vehicle_pursuit: vehicle_id+recommendation_id — pursuits are "
            "human-initiated only, C6)"
        ),
    )
    zone_geojson: dict | None = Field(
        default=None,
        description=(
            "optional zone polygon as GeoJSON (persisted to zone_geom; "
            "additive C1 field surfaced by /events/active)"
        ),
    )

    @field_validator("event_kind")
    @classmethod
    def _valid_kind(cls, v: str) -> str:
        if v not in EVENT_KINDS:
            raise ValueError(f"event_kind must be one of {EVENT_KINDS}")
        return v

    @field_validator("protection_class")
    @classmethod
    def _valid_protection(cls, v: str) -> str:
        SecurityTier.from_code(v)  # raises ValueError on bad codes
        return v

    @field_validator("crowd_class")
    @classmethod
    def _valid_crowd(cls, v: str) -> str:
        CrowdDensity.from_code(v)
        return v


@router.post("")
async def create_event(req: CorridorEventRequest, request: Request) -> dict:
    """Create a corridor event (spines + phases persisted atomically).

    All inserts are parameter-bound (asyncpg ``$n`` placeholders, values
    passed positionally — never interpolated into the SQL text; the same
    contract as adaptive/persistence.py).
    """
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")

    planned_start = req.planned_start
    planned_end = req.planned_end
    if req.phases:
        phase_starts = [_coerce_ist(p.window_start) for p in req.phases]
        phase_ends = [_coerce_ist(p.window_end) for p in req.phases]
        if planned_start is None:
            planned_start = min(phase_starts)
        if planned_end is None:
            planned_end = max(phase_ends)

    metadata_doc: dict[str, Any] = dict(req.metadata)
    if req.scenario_id:
        metadata_doc["scenario_id"] = req.scenario_id

    zone_param: str | None = None
    if req.zone_geojson is not None:
        if req.zone_geojson.get("type") != "Polygon":
            raise HTTPException(422, "zone_geojson must be a GeoJSON Polygon")
        zone_param = json.dumps(req.zone_geojson)

    async with pool.acquire() as conn:
        async with conn.transaction():
            insert_event_query = "INSERT INTO events.corridor_events (event_kind, protection_class, crowd_class, title, description, movement_id, planned_start, planned_end, zone_geom, metadata) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, ST_SetSRID(ST_GeomFromGeoJSON($9), 4326), $10::jsonb) RETURNING event_id, created_at"  # noqa: E501
            row = await conn.fetchrow(
                insert_event_query,
                req.event_kind,
                SecurityTier.from_code(req.protection_class).code,
                CrowdDensity.from_code(req.crowd_class).code,
                req.title,
                req.description,
                req.movement_id,
                _coerce_ist(planned_start) if planned_start else None,
                _coerce_ist(planned_end) if planned_end else None,
                zone_param,
                json.dumps(metadata_doc),
            )
            event_id = row["event_id"]

            insert_route_query = "INSERT INTO events.event_routes (event_id, route_rank, label, segment_ids) VALUES ($1, $2, $3, $4)"  # noqa: E501
            for rank, spine in enumerate(req.route_spines, start=1):
                await conn.execute(
                    insert_route_query,
                    event_id,
                    rank,
                    spine.label,
                    list(spine.segment_ids),
                )

            insert_phase_query = "INSERT INTO events.event_phases (event_id, phase_rank, label, window_start, window_end, closure_policy) VALUES ($1, $2, $3, $4, $5, $6::jsonb)"  # noqa: E501
            for rank, phase in enumerate(req.phases, start=1):
                await conn.execute(
                    insert_phase_query,
                    event_id,
                    rank,
                    phase.label,
                    _coerce_ist(phase.window_start),
                    _coerce_ist(phase.window_end),
                    json.dumps(phase.closure_policy),
                )

    logger.info(
        "events.created",
        event_id=str(event_id),
        event_kind=req.event_kind,
        protection_class=req.protection_class,
        crowd_class=req.crowd_class,
        spines=len(req.route_spines),
        phases=len(req.phases),
    )
    return {
        "event_id": str(event_id),
        "event_kind": req.event_kind,
        "protection_class": SecurityTier.from_code(req.protection_class).code,
        "crowd_class": CrowdDensity.from_code(req.crowd_class).code,
        "status": "planning",
        "inputs_key": event_inputs_key(str(event_id)),
    }


@router.get("/active")
async def active_events(request: Request) -> dict:
    """Active corridor events with spines and phase timeline (ops board)."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")

    routes_by_event: dict[str, list[dict]] = {}
    phases_by_event: dict[str, list[dict]] = {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(_ACTIVE_EVENTS_SQL)
        routes_rows = await conn.fetch(
            "SELECT r.event_id, r.label, r.segment_ids "
            "FROM events.event_routes r "
            "JOIN events.corridor_events e ON e.event_id = r.event_id "
            "WHERE e.status IN ('planning', 'approved', 'active') "
            "ORDER BY r.event_id, r.route_rank"
        )
        for rr in routes_rows:
            routes_by_event.setdefault(str(rr["event_id"]), []).append(
                {"label": rr["label"], "segment_ids": list(rr["segment_ids"])}
            )
        phases_rows = await conn.fetch(
            "SELECT p.event_id, p.label, p.window_start, p.window_end, "
            "p.closure_policy "
            "FROM events.event_phases p "
            "JOIN events.corridor_events e ON e.event_id = p.event_id "
            "WHERE e.status IN ('planning', 'approved', 'active') "
            "ORDER BY p.event_id, p.window_start, p.phase_rank"
        )
        for pr in phases_rows:
            policy = pr["closure_policy"]
            if isinstance(policy, str):  # asyncpg returns JSONB as text
                with contextlib.suppress(json.JSONDecodeError):
                    policy = json.loads(policy)
            phases_by_event.setdefault(str(pr["event_id"]), []).append(
                {
                    "label": pr["label"],
                    "window_start": pr["window_start"].isoformat(),
                    "window_end": pr["window_end"].isoformat(),
                    "closure_policy": policy,
                }
            )

    events: list[dict] = []
    for r in rows:
        event_id = str(r["event_id"])
        zone_geojson = None
        if r["zone_geojson"]:
            zone_geojson = (
                json.loads(r["zone_geojson"])
                if isinstance(r["zone_geojson"], str)
                else r["zone_geojson"]
            )
        meta = r["metadata"]
        if isinstance(meta, str):  # asyncpg returns JSONB as text
            with contextlib.suppress(json.JSONDecodeError):
                meta = json.loads(meta)
        events.append(
            {
                "event_id": event_id,
                "event_kind": r["event_kind"],
                "protection_class": r["protection_class"],
                "crowd_class": r["crowd_class"],
                "title": r["title"],
                "description": r["description"],
                "status": r["status"],
                "movement_id": (
                    str(r["movement_id"]) if r["movement_id"] else None
                ),
                "planned_start": (
                    r["planned_start"].isoformat() if r["planned_start"] else None
                ),
                "planned_end": (
                    r["planned_end"].isoformat() if r["planned_end"] else None
                ),
                "metadata": meta if isinstance(meta, dict) else {},
                "zone_geojson": zone_geojson,  # additive C1 field (optional)
                "routes": routes_by_event.get(event_id, []),
                "phases": phases_by_event.get(event_id, []),
                "inputs_key": event_inputs_key(event_id),
            }
        )

    return {"events": events}


__all__ = [
    "EVENT_KINDS",
    "event_inputs_key",
    "router",
]
