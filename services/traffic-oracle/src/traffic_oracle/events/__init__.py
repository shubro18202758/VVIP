"""Event-agnostic corridor events — HTTP surface (core stream, 007 + 013 + 018).

    POST /api/v1/events                  create (spines + phases atomically)
    GET  /api/v1/events/active           live events for the ops board
    GET  /api/v1/events?status=&since=&limit=
                                         history (same row shape as /active)
    POST /api/v1/events/{id}/status      lifecycle transition (contract C-F)
    POST /api/v1/events/{id}/routes      route update (contract C-O, 018):
                                         a re-route spine becomes the
                                         event's primary; the spine it
                                         replaces is SUPERSEDED, never
                                         deleted (route_history)

A corridor event is a planned operation that takes control of part of the
road network for a window — the generalization of a convoy movement across
the eight use-case archetypes.  Rows live in ``events.corridor_events`` (+
``event_routes`` spines + ``event_phases`` windows, migration 007).

Lifecycle (contract C-F; pure table, enforced + unit-tested):

    planning → approved | active | cancelled
    approved → active | cancelled
    active   → completed | cancelled
    completed | cancelled → (terminal)

Every status — the creation row included — is one row of the append-only
``events.event_status_log`` (migration 013) carrying the named actor.  A
transition commits in ONE transaction: the status UPDATE (+ actual_start /
actual_end), its log row, and — when the event closes — the walk of its
open dispatches to completed/cancelled (units/router.py
``close_event_dispatches``, audited as ``system:event_closed``), so a
closed event never keeps a unit on scene.

Pursuits (C6): a ``vehicle_pursuit`` is accepted only when its metadata
names a vehicle with an ACTIVE earmark and a recommendation for that same
vehicle APPROVED by a human officer — for a vehicle reported stolen that
is its R_STOLEN_SIGHTING (C-N: a pursuit is authorised by a sighting,
never by an e-challan).  Pursuits are human-initiated, never opened by
the system on its own.

Route updates (C-O): ``POST /{id}/routes`` {variant 'reroute-N' |
'plan_b', segment_ids (1..64, each a corridor.road_segments id),
make_primary (default true), officer?, note?} runs in ONE transaction —
the event row locked FOR UPDATE (409 unless planning/approved/active),
the current primary SUPERSEDED (``superseded_at``, migration 018), the
new spine inserted as the 'primary' at rank 1 with its ``variant`` and
``created_by``, and one ``event_status_log`` row (from = to = the
current status) narrating the change.  Every reader of event_routes
skips superseded spines; this module also returns them as the additive
``route_history`` (and each current spine's ``variant``).

Attribution (WP-16 seam, units/router.py): the deck-verified
``x-veer-officer`` header — honoured on a loopback connection only — IS
the actor of a lifecycle transition, an event creation and a route
update; a differing body ``officer`` is kept as the claimed officer in
the ledger row's note (event_status_log has no detail column) and in the
response (``actor_source``, ``claimed_officer``).  Without the header the
body officer is the actor, exactly as before.

Input hygiene: officers follow the shared named-actor rule (2..64 chars,
no control or bidi characters — city/common.named_actor); notes and every
text the create body carries are refused with a 422 when they hold a NUL
(Postgres cannot store one) instead of failing the write with a 500; a
``movement_id`` must be a uuid (the column is ``uuid``).
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid as _uuid
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from traffic_oracle.adaptive.enums import CrowdDensity, SecurityTier
from traffic_oracle.city.common import (
    as_uuid,
    clean_text,
    find_nul,
    iso_z,
    named_actor,
    relation_exists,
    routes_supersede_supported,
    utc,
)
from traffic_oracle.enforcement import rules as enforcement_rules
from traffic_oracle.enforcement.engine import SQL_STOLEN_EARMARK
from traffic_oracle.units.router import (
    attributed_actor,
    authenticated_officer,
    close_event_dispatches,
    note_with_claim,
)

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

#: Event statuses (migration 007 CHECK, same order).
EVENT_STATUSES: tuple[str, ...] = (
    "planning",
    "approved",
    "active",
    "completed",
    "cancelled",
)

#: Statuses that are still live (board, canvas, situation engine).
LIVE_EVENT_STATUSES: tuple[str, ...] = ("planning", "approved", "active")

#: Lifecycle transitions (contract C-F) — pure table, mirrored in tests.
EVENT_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "planning": ("approved", "active", "cancelled"),
    "approved": ("active", "cancelled"),
    "active": ("completed", "cancelled"),
    "completed": (),
    "cancelled": (),
}

#: Closing statuses: the event's open dispatches are walked shut.
CLOSING_STATUSES: tuple[str, ...] = ("completed", "cancelled")

#: The migration-013 ledger (probed so a missing migration degrades to a
#: clear 503 instead of failing event creation).
STATUS_LOG = "events.event_status_log"

_ACTIVE_EVENTS_SQL = (
    "SELECT e.event_id, e.event_kind, e.protection_class, e.crowd_class, "
    "e.title, e.description, e.status, e.movement_id, "
    "e.planned_start, e.planned_end, e.actual_start, e.actual_end, "
    "e.metadata, e.created_at, e.updated_at, "
    "ST_AsGeoJSON(e.zone_geom) AS zone_geojson "
    "FROM events.corridor_events e "
    "WHERE e.status IN ('planning', 'approved', 'active') "
    "ORDER BY e.planned_start NULLS LAST LIMIT 100"
)

#: History: optional status set + updated-since, newest first.
_SQL_EVENTS_HISTORY = "SELECT e.event_id, e.event_kind, e.protection_class, e.crowd_class, e.title, e.description, e.status, e.movement_id, e.planned_start, e.planned_end, e.actual_start, e.actual_end, e.metadata, e.created_at, e.updated_at, ST_AsGeoJSON(e.zone_geom) AS zone_geojson FROM events.corridor_events e WHERE (cardinality($1::text[]) = 0 OR e.status = ANY($1::text[])) AND ($2::timestamptz IS NULL OR e.updated_at >= $2::timestamptz) ORDER BY e.updated_at DESC, e.event_id LIMIT $3"  # noqa: E501

_SQL_EVENT_ROUTES = "SELECT r.event_id, r.label, r.segment_ids FROM events.event_routes r WHERE r.event_id = ANY($1::uuid[]) ORDER BY r.event_id, r.route_rank"  # noqa: E501

#: Migration 018 (probed — routes_supersede_supported): the CURRENT spines
#: with their re-route provenance, and the superseded ones (route_history).
_SQL_EVENT_ROUTES_CURRENT = "SELECT r.event_id, r.label, r.segment_ids, r.route_rank, r.variant, r.created_by, r.created_at FROM events.event_routes r WHERE r.event_id = ANY($1::uuid[]) AND r.superseded_at IS NULL ORDER BY r.event_id, r.route_rank, r.created_at"  # noqa: E501

_SQL_EVENT_ROUTE_HISTORY = "SELECT r.event_id, r.label, r.segment_ids, r.route_rank, r.variant, r.created_by, r.created_at, r.superseded_at FROM events.event_routes r WHERE r.event_id = ANY($1::uuid[]) AND r.superseded_at IS NOT NULL ORDER BY r.event_id, r.superseded_at, r.route_rank"  # noqa: E501

#: The current spines of ONE event, locked for a route update.
_SQL_CURRENT_ROUTES_FOR_UPDATE = "SELECT r.route_id, r.route_rank, r.label, r.variant, r.segment_ids FROM events.event_routes r WHERE r.event_id = $1 AND r.superseded_at IS NULL ORDER BY r.route_rank, r.created_at FOR UPDATE"  # noqa: E501

#: Which of a route update's segment ids exist (the rest are a 422).
_SQL_KNOWN_SEGMENTS = "SELECT rs.segment_id FROM corridor.road_segments rs WHERE rs.segment_id = ANY($1::bigint[])"  # noqa: E501

_SQL_EVENT_PHASES = "SELECT p.event_id, p.label, p.window_start, p.window_end, p.closure_policy FROM events.event_phases p WHERE p.event_id = ANY($1::uuid[]) ORDER BY p.event_id, p.window_start, p.phase_rank"  # noqa: E501

_SQL_EVENT_FOR_UPDATE = "SELECT e.event_id, e.status, e.event_kind, e.title FROM events.corridor_events e WHERE e.event_id = $1 FOR UPDATE"  # noqa: E501

#: Pursuit chain checks (C6): the recommendation row (locked against a
#: concurrent decision) and the vehicle's active earmark count.
#: The pursuit's authorising recommendation, with its rule and whether the
#: vehicle is CURRENTLY reported stolen (an active FIR earmark — C-N: such a
#: pursuit is authorised by the R_STOLEN_SIGHTING, never by an e-challan).
_SQL_PURSUIT_RECOMMENDATION = "SELECT r.recommendation_id, r.vehicle_id, r.status, r.decided_by, r.rule_id, " + SQL_STOLEN_EARMARK + " AS stolen_vehicle FROM events.enforcement_recommendations r WHERE r.recommendation_id = $1 FOR SHARE"  # noqa: E501

_SQL_ACTIVE_EARMARKS = "SELECT count(*) FROM corridor.vehicle_earmarks e WHERE e.vehicle_id = $1 AND e.cleared_at IS NULL AND e.retention_until > NOW()"  # noqa: E501

#: The pursuit an approval already authorised, if any (C6: one officer
#: approval authorises ONE pursuit — a later pursuit needs a later approval).
_SQL_PURSUIT_OF_RECOMMENDATION = "SELECT e.event_id::text FROM events.corridor_events e WHERE e.event_kind = 'vehicle_pursuit' AND e.metadata->>'recommendation_id' = $1 ORDER BY e.created_at DESC LIMIT 1"  # noqa: E501


def event_inputs_key(event_id: str) -> str:
    """Valkey key the situation-engine loop reads per event (contract C3)."""
    return "adaptive:inputs:" + str(event_id)


def _coerce_ist(dt: datetime) -> datetime:
    """Naive datetimes are interpreted as IST (UTC+5:30), like the twin."""
    if dt.tzinfo is None:
        from datetime import timedelta, timezone

        return dt.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
    return dt


def _officer(v: str | None) -> str | None:
    """Trim an officer identity: the shared named-actor rule (C6, C-F) —
    2..64 characters, no control or bidi characters."""
    return named_actor(v)


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
    # ── lifecycle ledger (013, additive) ─────────────────────────────────
    officer: str | None = Field(
        default=None,
        description=(
            "who opened the event (2..64 chars); recorded as the actor of "
            "the creation row in events.event_status_log"
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

    @field_validator("officer")
    @classmethod
    def _valid_officer(cls, v: str | None) -> str | None:
        return _officer(v)

    @field_validator("movement_id")
    @classmethod
    def _valid_movement_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        canon = as_uuid(v.strip())
        if canon is None:
            raise ValueError("movement_id must be a uuid (corridor_events.movement_id)")
        return str(canon)

    @model_validator(mode="after")
    def _no_nul_anywhere(self) -> CorridorEventRequest:
        """No NUL in any text the event persists (TEXT and JSONB refuse it)."""
        texts = {
            "title": self.title,
            "description": self.description,
            "scenario_id": self.scenario_id,
            "route_spines": [s.label for s in self.route_spines],
            "phases": [{"label": p.label, "closure_policy": p.closure_policy} for p in self.phases],
            "metadata": self.metadata,
            "zone_geojson": self.zone_geojson,
        }
        where = find_nul(texts)
        if where:
            raise ValueError(f"{where} must not contain NUL characters")
        return self


class EventStatusRequest(BaseModel):
    """POST /events/{id}/status — one lifecycle move by a named actor.

    ``officer`` is required unless the deck-verified ``x-veer-officer``
    header names the actor; with the header it is only a CLAIM (kept in
    the ledger note when it differs), so a claim may be as short as one
    character while the recorded actor is always held to 2..64.
    """

    to_status: str
    officer: str | None = Field(
        default=None,
        description=(
            "2..64 chars — the named human (or system) actor; optional when "
            "the deck-verified x-veer-officer header names the actor"
        ),
    )
    note: str | None = Field(default=None, max_length=500)

    @field_validator("to_status")
    @classmethod
    def _valid_to_status(cls, v: str) -> str:
        if v not in ("approved", "active", "completed", "cancelled"):
            raise ValueError("to_status must be approved|active|completed|cancelled")
        return v

    @field_validator("officer")
    @classmethod
    def _valid_officer(cls, v: str | None) -> str | None:
        return named_actor(v, min_len=1)

    @field_validator("note")
    @classmethod
    def _valid_note(cls, v: str | None) -> str | None:
        return clean_text(v, field_name="note")


#: Re-route variants (C-O): 'reroute-N' (N = 1..999) or 'plan_b'.
ROUTE_VARIANT_RE = re.compile(r"^(?:reroute-[1-9][0-9]{0,2}|plan_b)$")


class RouteUpdateRequest(BaseModel):
    """POST /events/{id}/routes — one re-route by a named actor (C-O)."""

    variant: str = Field(description="'reroute-N' (N = 1..999) or 'plan_b'")
    segment_ids: list[int] = Field(
        min_length=1,
        max_length=64,
        description="the new spine, in travel order (corridor.road_segments ids)",
    )
    make_primary: bool = Field(
        default=True,
        description=(
            "true: supersede the current primary and make this the event's "
            "primary spine (rank 1); false: add it as an alternate spine"
        ),
    )
    officer: str | None = Field(
        default=None,
        description=(
            "2..64 chars — who re-routes; optional when the deck-verified "
            "x-veer-officer header names the actor"
        ),
    )
    note: str | None = Field(default=None, max_length=500)

    @field_validator("variant")
    @classmethod
    def _valid_variant(cls, v: str) -> str:
        variant = v.strip()
        if not ROUTE_VARIANT_RE.fullmatch(variant):
            raise ValueError("variant must be 'reroute-N' (N = 1..999) or 'plan_b'")
        return variant

    @field_validator("segment_ids")
    @classmethod
    def _valid_segments(cls, v: list[int]) -> list[int]:
        bad = [s for s in v if s <= 0]
        if bad:
            raise ValueError(f"segment_ids must be positive road-segment ids (got {bad[:5]})")
        return v

    @field_validator("officer")
    @classmethod
    def _valid_officer(cls, v: str | None) -> str | None:
        return named_actor(v, min_len=1)

    @field_validator("note")
    @classmethod
    def _valid_note(cls, v: str | None) -> str | None:
        return clean_text(v, field_name="note")


def _human_actor(request: Request, body_officer: str | None) -> tuple[str, dict | None]:
    """``(actor, provenance)`` for one write — header first (units/router
    ``attributed_actor``).  The recorded actor always meets the named-actor
    rule (2..64): a one-character body officer is only ever a claim."""
    actor, detail = attributed_actor(authenticated_officer(request), body_officer)
    if detail is None:
        try:
            named_actor(actor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return actor, detail


def _provenance_fields(detail: dict | None) -> dict:
    """The additive response fields naming where the actor came from."""
    return {
        "actor_source": (detail or {}).get("actor_source", "body"),
        "claimed_officer": (detail or {}).get("claimed_officer"),
    }


def _creation_actor(
    req: CorridorEventRequest,
    metadata: dict[str, Any],
    header_officer: str | None = None,
) -> str:
    """Who opened the event: the deck-verified officer header, else the
    officer field, else the metadata's opened_by / source tag (brain,
    emulator), else the operator API."""
    if header_officer:
        return header_officer
    if req.officer:
        return req.officer
    for key in ("opened_by", "source"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:64]
    return "operator"


async def _validate_pursuit(conn: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    """C6 chain for a vehicle_pursuit: earmarked vehicle + a recommendation
    for that same vehicle approved by a human officer (for a vehicle
    reported stolen: its R_STOLEN_SIGHTING — C-N).  Returns metadata with
    the two ids canonicalized; raises 422 with the precise broken link.
    The texts speak of a sighting authorising a pursuit: a pursuit is
    never a fine, and no e-challan is ever its basis for a stolen car."""
    raw_vehicle = metadata.get("vehicle_id")
    raw_rec = metadata.get("recommendation_id")
    try:
        vehicle_id = _uuid.UUID(str(raw_vehicle)) if raw_vehicle else None
    except ValueError:
        vehicle_id = None
    if vehicle_id is None:
        raise HTTPException(
            422,
            "vehicle_pursuit requires metadata.vehicle_id (uuid of an earmarked "
            "vehicle) — a pursuit is human-initiated from a sighting a duty "
            "officer approved (C6)",
        )
    try:
        rec_id = _uuid.UUID(str(raw_rec)) if raw_rec else None
    except ValueError:
        rec_id = None
    if rec_id is None:
        raise HTTPException(
            422,
            "vehicle_pursuit requires metadata.recommendation_id (uuid of the "
            "sighting recommendation a human officer APPROVED for this vehicle "
            "— R_STOLEN_SIGHTING for a vehicle reported stolen) — C6",
        )
    earmarks = await conn.fetchval(_SQL_ACTIVE_EARMARKS, vehicle_id)
    if not earmarks:
        raise HTTPException(
            422,
            f"vehicle {vehicle_id} has no active earmark — a pursuit needs a "
            "lawful-basis earmark that is neither cleared nor expired (C6)",
        )
    rec = await conn.fetchrow(_SQL_PURSUIT_RECOMMENDATION, rec_id)
    if rec is None:
        raise HTTPException(422, f"recommendation {rec_id} not found")
    if rec["status"] != "approved":
        raise HTTPException(
            422,
            f"recommendation {rec_id} is {rec['status']} — a pursuit is "
            "authorised only by a sighting a human officer APPROVED (C6)",
        )
    if str(rec["vehicle_id"]) != str(vehicle_id):
        raise HTTPException(
            422,
            f"recommendation {rec_id} concerns a different vehicle than "
            f"metadata.vehicle_id {vehicle_id}",
        )
    if rec.get("stolen_vehicle") and enforcement_rules.is_penal(rec.get("rule_id")):
        # C-N: an approval decided before (or regardless of) the theft report
        # is not the officer's authorisation to pursue a stolen car
        raise HTTPException(
            422,
            f"recommendation {rec_id} ({rec.get('rule_id')}) is not a sighting "
            "authorisation — a pursuit of a vehicle reported stolen is "
            "authorised only by its R_STOLEN_SIGHTING, approved by a human "
            "officer (C-N)",
        )
    prior = await conn.fetchval(_SQL_PURSUIT_OF_RECOMMENDATION, str(rec_id))
    if prior:
        raise HTTPException(
            422,
            f"recommendation {rec_id} already authorised pursuit {prior} — one "
            "officer approval authorises one pursuit; approve a newer sighting "
            "to open another (C6)",
        )
    out = dict(metadata)
    out["vehicle_id"] = str(vehicle_id)
    out["recommendation_id"] = str(rec_id)
    return out


@router.post("")
async def create_event(req: CorridorEventRequest, request: Request) -> dict:
    """Create a corridor event (spines + phases + creation ledger row
    persisted atomically).

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

    header_officer = authenticated_officer(request)
    actor = _creation_actor(req, metadata_doc, header_officer)
    claim: dict | None = None
    if header_officer is not None:
        claim = {"actor": header_officer, "actor_source": "x-veer-officer"}
        if req.officer and req.officer != header_officer:
            claim["claimed_officer"] = req.officer
    elif not req.officer:
        # opened by a machine path (brain / emulator metadata) or the bare
        # operator API — never presented as a body-named human
        claim = {"actor_source": "metadata" if actor != "operator" else "default"}
    async with pool.acquire() as conn:
        ledger = await relation_exists(conn, STATUS_LOG)
        async with conn.transaction():
            if req.event_kind == "vehicle_pursuit":
                metadata_doc = await _validate_pursuit(conn, metadata_doc)

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

            if ledger:
                creation_log = "INSERT INTO events.event_status_log (event_id, from_status, to_status, actor, note) VALUES ($1, NULL, 'planning', $2, $3)"  # noqa: E501
                await conn.execute(
                    creation_log, event_id, actor, note_with_claim("event created", claim)
                )

    logger.info(
        "events.created",
        event_id=str(event_id),
        event_kind=req.event_kind,
        protection_class=req.protection_class,
        crowd_class=req.crowd_class,
        spines=len(req.route_spines),
        phases=len(req.phases),
        actor=actor,
        **_provenance_fields(claim),
    )
    return {
        "event_id": str(event_id),
        "event_kind": req.event_kind,
        "protection_class": SecurityTier.from_code(req.protection_class).code,
        "crowd_class": CrowdDensity.from_code(req.crowd_class).code,
        "status": "planning",
        "inputs_key": event_inputs_key(str(event_id)),
        "actor": actor,
        # additive (WP-19): where the actor came from
        **_provenance_fields(claim),
    }


# ---------------------------------------------------------------------------
# Read shape shared by /active and the history list
# ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _spine_out(rr: Any, *, extended: bool = False, history: bool = False) -> dict:
    """One event_routes row → the wire spine (label + segment_ids, plus the
    additive 018 fields when the store carries them: ``extended``)."""
    spine = {"label": rr["label"], "segment_ids": [int(s) for s in rr["segment_ids"]]}
    if extended:
        spine.update(
            {
                "rank": int(rr["route_rank"]),
                "variant": rr["variant"],
                "created_by": rr["created_by"],
                "created_at": _iso(rr["created_at"]),
            }
        )
    if history:
        spine["superseded_at"] = _iso(rr["superseded_at"])
    return spine


async def load_spines(
    conn: Any, ids: list[Any]
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """(current spines, route_history) per event id, two batched reads.

    Without migration 018 every spine is current and the history is
    empty — exactly the pre-018 answer."""
    current: dict[str, list[dict]] = {}
    history: dict[str, list[dict]] = {}
    if not ids:
        return current, history
    if await routes_supersede_supported(conn):
        for rr in await conn.fetch(_SQL_EVENT_ROUTES_CURRENT, ids):
            current.setdefault(str(rr["event_id"]), []).append(
                _spine_out(rr, extended=True)
            )
        for rr in await conn.fetch(_SQL_EVENT_ROUTE_HISTORY, ids):
            history.setdefault(str(rr["event_id"]), []).append(
                _spine_out(rr, extended=True, history=True)
            )
    else:
        for rr in await conn.fetch(_SQL_EVENT_ROUTES, ids):
            current.setdefault(str(rr["event_id"]), []).append(_spine_out(rr))
    return current, history


async def _event_rows_out(conn: Any, rows: list[Any]) -> list[dict]:
    """Event rows + their spines (current + superseded) and phase timeline
    (batched reads, never per row)."""
    ids = [r["event_id"] for r in rows]
    routes_by_event: dict[str, list[dict]] = {}
    history_by_event: dict[str, list[dict]] = {}
    phases_by_event: dict[str, list[dict]] = {}
    if ids:
        routes_by_event, history_by_event = await load_spines(conn, ids)
        for pr in await conn.fetch(_SQL_EVENT_PHASES, ids):
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
                "planned_start": _iso(r["planned_start"]),
                "planned_end": _iso(r["planned_end"]),
                # additive (013 lifecycle): when it really ran
                "actual_start": _iso(r["actual_start"]),
                "actual_end": _iso(r["actual_end"]),
                "updated_at": _iso(r["updated_at"]),
                "metadata": meta if isinstance(meta, dict) else {},
                "zone_geojson": zone_geojson,  # additive C1 field (optional)
                "routes": routes_by_event.get(event_id, []),
                # additive (C-O, 018): spines a route update superseded
                "route_history": history_by_event.get(event_id, []),
                "phases": phases_by_event.get(event_id, []),
                "inputs_key": event_inputs_key(event_id),
            }
        )
    return events


@router.get("/active")
async def active_events(request: Request) -> dict:
    """Active corridor events with spines and phase timeline (ops board)."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    async with pool.acquire() as conn:
        rows = await conn.fetch(_ACTIVE_EVENTS_SQL)
        events = await _event_rows_out(conn, rows)
    return {"events": events}


@router.get("")
async def list_events(
    request: Request,
    status: str | None = Query(
        default=None, max_length=64, description="status or CSV of statuses"
    ),
    since: Annotated[
        datetime | None, Query(description="only events updated at/after this instant")
    ] = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    """Event history for after-action review (contract C-F), newest first."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    statuses: list[str] = []
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        bad = [s for s in statuses if s not in EVENT_STATUSES]
        if bad:
            raise HTTPException(
                422, f"status must be among {list(EVENT_STATUSES)} (got {bad})"
            )
    # Query timestamps follow the wire contract (UTC); naive means UTC here.
    since_dt = utc(since) if since is not None else None
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SQL_EVENTS_HISTORY, statuses, since_dt, limit)
        events = await _event_rows_out(conn, rows)
    return {"events": events, "count": len(events)}


# ---------------------------------------------------------------------------
# POST /events/{id}/status — lifecycle transition (contract C-F)
# ---------------------------------------------------------------------------


class _IllegalTransitionError(Exception):
    def __init__(self, from_status: str) -> None:
        super().__init__(from_status)
        self.from_status = from_status


@router.post("/{event_id}/status")
async def transition_event(
    event_id: str, req: EventStatusRequest, request: Request
) -> Any:
    """Move one event along the lifecycle table, atomically with its
    ledger row and (on close) its dispatches' release.

    The actor is the deck-verified ``x-veer-officer`` header when present
    (loopback only), else the body officer; a differing body officer is
    kept as the claimed officer in the ledger note and the response."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    try:
        eid = _uuid.UUID(event_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid event uuid") from exc
    actor, detail = _human_actor(request, req.officer)
    note = note_with_claim(req.note, detail)

    status_update = "UPDATE events.corridor_events SET status = $2, actual_start = CASE WHEN $2 = 'active' THEN COALESCE(actual_start, NOW()) ELSE actual_start END, actual_end = CASE WHEN $2 IN ('completed', 'cancelled') THEN NOW() ELSE actual_end END, updated_at = NOW() WHERE event_id = $1 AND status = $3 RETURNING actual_start, actual_end"  # noqa: E501
    log_insert = "INSERT INTO events.event_status_log (event_id, from_status, to_status, actor, note) VALUES ($1, $2, $3, $4, $5) RETURNING seq, at"  # noqa: E501

    closed: list[dict] = []
    try:
        async with pool.acquire() as conn:
            if not await relation_exists(conn, STATUS_LOG):
                raise HTTPException(
                    503,
                    "events.event_status_log missing — apply migration 013 "
                    "(scripts/migrate.py) before lifecycle transitions",
                )
            async with conn.transaction():
                current = await conn.fetchrow(_SQL_EVENT_FOR_UPDATE, eid)
                if current is None:
                    raise HTTPException(404, f"event {event_id} not found")
                from_status = str(current["status"])
                if req.to_status not in EVENT_TRANSITIONS.get(from_status, ()):
                    raise _IllegalTransitionError(from_status)
                moved = await conn.fetchrow(status_update, eid, req.to_status, from_status)
                if moved is None:  # unreachable under FOR UPDATE; never silent
                    raise _IllegalTransitionError(from_status)
                log_row = await conn.fetchrow(
                    log_insert, eid, from_status, req.to_status, actor, note
                )
                if req.to_status in CLOSING_STATUSES:
                    closed = await close_event_dispatches(
                        conn,
                        str(eid),
                        req.to_status,
                        f"event {req.to_status} by {actor}",
                    )
    except _IllegalTransitionError as exc:
        allowed = list(EVENT_TRANSITIONS.get(exc.from_status, ()))
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"illegal transition {exc.from_status} → {req.to_status}"
                    + (f"; allowed: {allowed}" if allowed else "; status is terminal")
                ),
                "from_status": exc.from_status,
                "allowed": allowed,
            },
        )

    logger.info(
        "events.transition",
        event_id=str(eid),
        from_status=from_status,
        to_status=req.to_status,
        actor=actor,
        dispatches_closed=len(closed),
        **_provenance_fields(detail),
    )
    return {
        "event_id": str(eid),
        "from_status": from_status,
        "to_status": req.to_status,
        "at": iso_z(log_row["at"]),
        "actor": actor,
        # additive
        "seq": int(log_row["seq"]),
        "actual_start": iso_z(moved["actual_start"]),
        "actual_end": iso_z(moved["actual_end"]),
        "closed_dispatches": closed,
        "note": note,
        **_provenance_fields(detail),
    }


# ---------------------------------------------------------------------------
# POST /events/{id}/routes — route update (contract C-O, migration 018)
# ---------------------------------------------------------------------------


class _RouteUpdateRefusedError(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


def _supersede_targets(current: list[Any]) -> list[Any]:
    """The spines a new primary replaces: every current spine labelled
    'primary'; for an event created without one (an emulator 'spine', a
    'sweep belt'), its de-facto primary — the lowest-ranked current spine."""
    primaries = [r for r in current if r["label"] == "primary"]
    if primaries:
        return primaries
    if not current:
        return []
    top = min(int(r["route_rank"]) for r in current)
    return [r for r in current if int(r["route_rank"]) == top]


def _route_note(
    variant: str,
    segment_ids: list[int],
    make_primary: bool,
    superseded: list[Any],
    note: str | None,
) -> str:
    """The ledger narrative of one route update (event_status_log.note)."""
    path = f"{segment_ids[0]}→{segment_ids[-1]}" if len(segment_ids) > 1 else str(segment_ids[0])
    what = "is now the primary spine" if make_primary else "added as an alternate spine"
    text = f"route update: {variant} {what} ({len(segment_ids)} segments, {path})"
    if superseded:
        replaced = ", ".join(sorted({str(r["variant"] or r["label"]) for r in superseded}))
        text += f"; supersedes {replaced}"
    if note:
        text += f" — {note}"
    return text


@router.post("/{event_id}/routes")
async def update_event_routes(
    event_id: str, req: RouteUpdateRequest, request: Request
) -> Any:
    """Re-route a live event (C-O): one transaction — the event locked FOR
    UPDATE (409 unless planning/approved/active), the current primary
    superseded, the new spine inserted as the 'primary' at rank 1 with its
    variant and author, and one ledger row.  Returns the event's current
    spines plus its route_history.  503 before migration 018."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    try:
        eid = _uuid.UUID(event_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid event uuid") from exc
    actor, detail = _human_actor(request, req.officer)
    segment_ids = list(req.segment_ids)

    supersede_update = "UPDATE events.event_routes SET superseded_at = NOW() WHERE route_id = ANY($1::uuid[]) AND superseded_at IS NULL RETURNING route_id, route_rank, label, variant, segment_ids, superseded_at"  # noqa: E501
    route_insert = "INSERT INTO events.event_routes (event_id, route_rank, label, segment_ids, variant, created_by) VALUES ($1, $2, $3, $4, $5, $6) RETURNING route_id, created_at"  # noqa: E501
    event_touch = "UPDATE events.corridor_events SET updated_at = NOW() WHERE event_id = $1"  # noqa: E501
    log_insert = "INSERT INTO events.event_status_log (event_id, from_status, to_status, actor, note) VALUES ($1, $2, $2, $3, $4) RETURNING seq, at"  # noqa: E501

    try:
        async with pool.acquire() as conn:
            if not await routes_supersede_supported(conn):
                raise HTTPException(
                    503,
                    "events.event_routes.superseded_at missing — apply migration 018 "
                    "(scripts/migrate.py) before route updates",
                )
            ledger = await relation_exists(conn, STATUS_LOG)
            known = {
                int(r["segment_id"])
                for r in await conn.fetch(_SQL_KNOWN_SEGMENTS, segment_ids)
            }
            unknown = [s for s in dict.fromkeys(segment_ids) if s not in known]
            if unknown:
                raise HTTPException(
                    422,
                    f"segment_ids {unknown[:10]} are not corridor.road_segments ids "
                    "— a spine runs on known road segments only",
                )
            async with conn.transaction():
                current_event = await conn.fetchrow(_SQL_EVENT_FOR_UPDATE, eid)
                if current_event is None:
                    raise HTTPException(404, f"event {event_id} not found")
                status = str(current_event["status"])
                if status not in LIVE_EVENT_STATUSES:
                    raise _RouteUpdateRefusedError(status)
                current = list(await conn.fetch(_SQL_CURRENT_ROUTES_FOR_UPDATE, eid))
                superseded: list[Any] = []
                if req.make_primary:
                    targets = _supersede_targets(current)
                    if targets:
                        superseded = list(
                            await conn.fetch(
                                supersede_update, [r["route_id"] for r in targets]
                            )
                        )
                    rank, label = 1, "primary"
                else:
                    rank = max((int(r["route_rank"]) for r in current), default=0) + 1
                    label = req.variant
                await conn.fetchrow(
                    route_insert, eid, rank, label, segment_ids, req.variant, actor
                )
                await conn.execute(event_touch, eid)
                ledger_note = note_with_claim(
                    _route_note(
                        req.variant, segment_ids, req.make_primary, superseded, req.note
                    ),
                    detail,
                )
                log_row = (
                    await conn.fetchrow(log_insert, eid, status, actor, ledger_note)
                    if ledger
                    else None
                )
                routes, history = await load_spines(conn, [eid])
    except _RouteUpdateRefusedError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"event is {exc.status} — only a planning, approved or active "
                    "event can be re-routed"
                ),
                "status": exc.status,
                "allowed_statuses": list(LIVE_EVENT_STATUSES),
            },
        )

    logger.info(
        "events.route_updated",
        event_id=str(eid),
        variant=req.variant,
        make_primary=req.make_primary,
        segments=len(segment_ids),
        superseded=len(superseded),
        actor=actor,
        **_provenance_fields(detail),
    )
    return {
        "event_id": str(eid),
        "status": status,
        "variant": req.variant,
        "make_primary": req.make_primary,
        "routes": routes.get(str(eid), []),
        "route_history": history.get(str(eid), []),
        "superseded": [
            {
                "label": r["label"],
                "variant": r["variant"],
                "rank": int(r["route_rank"]),
                "segment_ids": [int(s) for s in r["segment_ids"]],
                "superseded_at": _iso(r["superseded_at"]),
            }
            for r in superseded
        ],
        "actor": actor,
        **_provenance_fields(detail),
        "seq": int(log_row["seq"]) if log_row is not None else None,
        "at": iso_z(log_row["at"]) if log_row is not None else None,
        "inputs_key": event_inputs_key(str(eid)),
    }


__all__ = [
    "CLOSING_STATUSES",
    "EVENT_KINDS",
    "EVENT_STATUSES",
    "EVENT_TRANSITIONS",
    "LIVE_EVENT_STATUSES",
    "ROUTE_VARIANT_RE",
    "STATUS_LOG",
    "event_inputs_key",
    "load_spines",
    "router",
]
