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
           corridor.dispatch_events — every state change is a row),
           fetched in ONE batched query for the whole page.

    GET  /api/v1/dispatches/{dispatch_id}
         → ONE dispatch (a row of the list above, same shape) with its
           audit tail; 404 unknown, 422 malformed uuid.  The deck's public
           /driver projection reads its own request here instead of
           filtering a per-event list.

    GET  /api/v1/units
         → EMULATED agency units for the canvas (C1 contract):
           {unit_id, agency, label, position, waypoint, assigned_event_id,
            status, eta_sec, arrived, emulated: true, source}.  Two
           emulated sources, both badged:
             * 'emulated_dispatch_projection' — the deterministic
               projection (units/projection.py) of an approved dispatch
               along the corridor graph, a total function of
               (dispatch_id, approved_at, now).  Only dispatches whose
               EVENT is still live (planning|approved|active) project: a
               completed or cancelled event releases its units.
             * 'emulated_patrol' — emulator patrol frames from Valkey
               ``units:emulated:{unit_id}`` (contract C-A, EX 30).  A
               patrol frame for a dispatched unit takes precedence for the
               POSITION; the dispatch keeps the assignment.
           One row per unit (its latest approval); a unit approved on
           more than one live dispatch is flagged ``double_booked`` with
           its other dispatches, and counted in ``conflicts`` — the
           conflict stays visible, never silently collapsed.
           A projected unit travels from its start to the waypoint
           junction ITSELF (C-N): the graph path ends on the junction's
           nearest approach segment and then the junction point, so it is
           never 'arrived' at t=0 short of the junction.  A pursuit's unit
           starts at its agency's post nearest the waypoint
           (corridor.agency_stations, 012 — ``staged_at``); every other
           event's units stage at the event's first spine segment.

Transition table (pure, enforced + unit-tested):

    requested  → approved | deferred | cancelled
    approved   → en_route | cancelled
    en_route   → on_scene | cancelled
    on_scene   → completed | cancelled
    deferred | completed | cancelled → (terminal)

Every mutation and its audit row commit in ONE transaction — a status
change can never exist without its audit row (C6 "every state change is
a row").  When a corridor event closes, ``close_event_dispatches`` walks
its open dispatches to completed/cancelled inside the event's own
transition transaction, audited by actor ``system:event_closed``.

Authenticated actor (WP-16): the deck forwards a signed-in operator as
the percent-encoded ``x-veer-officer`` header (hooks.server.ts — set from
a verified session, any client copy stripped).  On the HUMAN writes —
the request, the decision and the lifecycle walk — that header, when
present, IS the recorded actor (``requested_by`` / ``decided_by`` / the
audit row's ``actor``); the body ``requested_by`` / ``officer`` becomes a
claim, kept in the audit row's ``detail`` (``claimed_officer``) only when
it differs.  Without the header nothing
changes: the body officer is the actor, as before.  The header is
trusted ONLY because the oracle binds loopback (HOST=127.0.0.1 —
infra/compose.argus-override.yml, WP-08): only processes on this host
reach it, and the deck — the one LAN-facing surface in front of it —
strips any client copy before setting its own.  It is honoured only on a
connection that arrived on a loopback interface (see
:func:`authenticated_officer`); any other proxy ever put in front of the
oracle must strip it too, or the deck must sign it, before it can be
trusted.

SQL hygiene: every WRITE statement is a LOCAL single-line string literal
with $n placeholders at its call site (house style — see
signals/writer.py docstring); reads are module constants.  No outbound
HTTP.  No person fields (C6 — vehicles/agencies only, never persons).
Officers are trimmed and refused (422) when they carry control or bidi
characters; notes and ids likewise when they carry control characters —
a NUL never reaches Postgres as a 500.

Connections: every /units read — including the graph search for a cold
unit path — runs on the ONE connection the request holds; a poll never
holds a second pool connection (nested acquires under concurrent polls
could exhaust the shared pool).
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from traffic_oracle.city.common import (
    clean_text,
    display_geom_supported,
    finite,
    is_slug,
    iso_z,
    json_doc,
    named_actor,
    parse_ts,
    routes_supersede_supported,
    scan_mget,
    valid_lonlat,
)
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

# Statuses that still hold a unit for its event (closed with the event).
OPEN_DISPATCH_STATUSES = ("requested", "approved", "en_route", "on_scene")

# Corridor-event statuses under which dispatches stay live.
LIVE_EVENT_STATUSES = ("planning", "approved", "active")

# Audit actor for dispatches closed by their event's lifecycle.
EVENT_CLOSED_ACTOR = "system:event_closed"

# Emulated unit sources (both badged emulated: true everywhere).
PROJECTION_SOURCE = "emulated_dispatch_projection"
PATROL_SOURCE = "emulated_patrol"

# Contract C-A: emulator patrol frames (producer WP-01 units ingest).
EMULATED_UNIT_KEY_PREFIX = "units:emulated:"
PATROL_STATUSES = ("patrol", "available", "en_route", "on_scene", "returning")
MAX_PATROL_KEYS = 500

# The deck's verified operator (hooks.server.ts OFFICER_HEADER, WP-07):
# percent-encoded (encodeURIComponent — names may be Devanagari or
# Gujarati), set from a signed-in session only.
OFFICER_HEADER = "x-veer-officer"
OFFICER_HEADER_MIN_LEN = 2
OFFICER_HEADER_MAX_LEN = 64

# A '%' that does not start a two-hex-digit escape: not encodeURIComponent.
_BAD_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")

# ---------------------------------------------------------------------------
# Read SQL (module constants — parameter-bound)
# ---------------------------------------------------------------------------

_SQL_ACTIVE_DISPATCHES = "SELECT d.dispatch_id, d.event_id, d.agency, d.unit_id, d.waypoint_junction, d.status, d.requested_by, d.decided_by, d.approved_at, d.created_at FROM corridor.dispatches d JOIN events.corridor_events e ON e.event_id = d.event_id WHERE d.status = ANY($1::text[]) AND e.status IN ('planning', 'approved', 'active') ORDER BY d.approved_at NULLS FIRST, d.created_at LIMIT $2"  # noqa: E501

_SQL_EVENT_CHECK = "SELECT event_id, status FROM events.corridor_events WHERE event_id = $1::uuid FOR SHARE"  # noqa: E501

_SQL_EVENT_SPINE_START = "SELECT r.segment_ids, e.event_kind FROM events.event_routes r JOIN events.corridor_events e ON e.event_id = r.event_id WHERE r.event_id = $1::uuid ORDER BY r.route_rank LIMIT 1"  # noqa: E501

#: Migration 018 (probed): the event's CURRENT first spine — a re-routed
#: event stages its units on the route it now runs, not the superseded one.
_SQL_EVENT_SPINE_START_CURRENT = "SELECT r.segment_ids, e.event_kind FROM events.event_routes r JOIN events.corridor_events e ON e.event_id = r.event_id WHERE r.event_id = $1::uuid AND r.superseded_at IS NULL ORDER BY r.route_rank, r.created_at DESC LIMIT 1"  # noqa: E501

# corridor.road_graph nodes are SEGMENT ids (001/seed; pgr_dijkstra over
# road_graph therefore speaks segments, not junctions) — the unit path is
# segment→segment to the nearest of the waypoint junction's APPROACH
# segments (every segment entering it), then the junction point itself
# (C-N).  The name is the one the city readers' fakes key on.
_SQL_JUNCTION_ADJACENT_SEGMENT = "SELECT DISTINCT sa.from_segment_id AS segment_id FROM corridor.segment_adjacency sa WHERE sa.via_junction_id = $1 ORDER BY 1"  # noqa: E501

#: A pursuit's unit starts at its agency's post nearest the waypoint
#: junction (migration 012 corridor.agency_stations), entering the graph on
#: the segment nearest that post.
_SQL_NEAREST_STATION = "SELECT st.station_id, st.name, ST_X(st.geom) AS lon, ST_Y(st.geom) AS lat, (SELECT rs.segment_id FROM corridor.road_segments rs ORDER BY rs.geom <-> st.geom LIMIT 1) AS segment_id FROM corridor.agency_stations st WHERE st.agency = $1 ORDER BY st.geom <-> (SELECT j.geom FROM corridor.junctions j WHERE j.junction_id = $2) LIMIT 1"  # noqa: E501

_SQL_SEGMENT_POINTS = "SELECT rs.segment_id, ST_X(ST_PointOnSurface(rs.geom)) AS lon, ST_Y(ST_PointOnSurface(rs.geom)) AS lat FROM corridor.road_segments rs WHERE rs.segment_id = ANY($1::bigint[])"  # noqa: E501

#: Migration 015 (C-K, probed): a unit animates ON the drawn road — its
#: anchors sit on the display geometry when one exists (routing keeps geom).
_SQL_SEGMENT_POINTS_DISPLAY = "SELECT rs.segment_id, ST_X(ST_PointOnSurface(COALESCE(rs.display_geom, rs.geom))) AS lon, ST_Y(ST_PointOnSurface(COALESCE(rs.display_geom, rs.geom))) AS lat FROM corridor.road_segments rs WHERE rs.segment_id = ANY($1::bigint[])"  # noqa: E501

_SQL_JUNCTION_GEOM = "SELECT j.junction_id, ST_X(j.geom) AS lon, ST_Y(j.geom) AS lat FROM corridor.junctions j WHERE j.junction_id = ANY($1::bigint[])"  # noqa: E501

#: The routing-graph search of data/graph_queries.CorridorGraphDB
#: .shortest_path (same statement, same ::bigint overload pin), run on the
#: request's HELD connection instead of acquiring a second one from the
#: shared pool — once per approach of the waypoint, the cheapest kept.
_SQL_SHORTEST_PATH = "SELECT seq, node, edge, cost, agg_cost FROM pgr_dijkstra('SELECT id, source, target, cost, reverse_cost FROM corridor.road_graph', $1::bigint, $2::bigint, directed := true) ORDER BY seq"  # noqa: E501

_SQL_DISPATCH_LIST = "SELECT d.dispatch_id, d.event_id, d.agency, d.unit_id, d.waypoint_junction, d.status, d.requested_by, d.decided_by, d.decision_note, d.approved_at, d.created_at, d.updated_at FROM corridor.dispatches d WHERE ($1::uuid IS NULL OR d.event_id = $1::uuid) AND ($2::text IS NULL OR d.agency = $2::text) AND ($3::text IS NULL OR d.status = $3::text) ORDER BY d.created_at DESC LIMIT $4"  # noqa: E501

_SQL_DISPATCH_GET = "SELECT d.dispatch_id, d.event_id, d.agency, d.unit_id, d.waypoint_junction, d.status, d.requested_by, d.decided_by, d.decision_note, d.approved_at, d.created_at, d.updated_at FROM corridor.dispatches d WHERE d.dispatch_id = $1::uuid"  # noqa: E501

_SQL_DISPATCH_GET_FOR_UPDATE = _SQL_DISPATCH_GET + " FOR UPDATE"

# Audit reads carry the row's ``detail`` (migration 014) through
# to_jsonb(row) -> 'detail', which is NULL — not an error — on a store
# that predates the column, so the read path never depends on 014.
_SQL_AUDIT_TAIL = "SELECT e.from_status, e.to_status, e.actor, e.note, e.at, to_jsonb(e) -> 'detail' AS detail FROM corridor.dispatch_events e WHERE e.dispatch_id = $1::uuid ORDER BY e.seq DESC LIMIT $2"  # noqa: E501

# One query for a whole page of audit tails (newest first per dispatch).
_SQL_AUDIT_TAILS = "SELECT t.dispatch_id, t.from_status, t.to_status, t.actor, t.note, t.at, t.detail FROM (SELECT de.dispatch_id, de.seq, de.from_status, de.to_status, de.actor, de.note, de.at, to_jsonb(de) -> 'detail' AS detail, row_number() OVER (PARTITION BY de.dispatch_id ORDER BY de.seq DESC) AS rn FROM corridor.dispatch_events de WHERE de.dispatch_id = ANY($1::uuid[])) t WHERE t.rn <= $2 ORDER BY t.dispatch_id, t.seq DESC"  # noqa: E501

# Whether corridor.dispatch_events has its 014 ``detail`` column (probed
# before the first attributed write, then cached — see _audit).
_SQL_AUDIT_DETAIL_COLUMN = "SELECT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = to_regclass('corridor.dispatch_events') AND a.attname = 'detail' AND NOT a.attisdropped) AS present"  # noqa: E501

# Open dispatches of one event, locked for the event's closing transaction.
_SQL_OPEN_EVENT_DISPATCHES = "SELECT d.dispatch_id, d.unit_id, d.agency, d.status FROM corridor.dispatches d WHERE d.event_id = $1::uuid AND d.status = ANY($2::text[]) ORDER BY d.created_at FOR UPDATE"  # noqa: E501


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DispatchRequest(BaseModel):
    """POST /events/{id}/dispatch — the REQUEST half of the pattern."""

    agency: str
    unit_id: str = Field(min_length=1, max_length=64)
    waypoint_junction: int | None = Field(default=None, ge=1)
    # the requester as the body names it; the deck-verified x-veer-officer
    # wins (this is then only a claim); unnamed without a header: 'operator'
    requested_by: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("agency")
    @classmethod
    def _valid_agency(cls, v: str) -> str:
        if not is_agency(v):
            valid = sorted(AGENCIES)
            raise ValueError(f"agency must be one of {valid}")
        return v

    @field_validator("unit_id", "note")
    @classmethod
    def _clean(cls, v: str | None, info: ValidationInfo) -> str | None:
        return clean_text(v, field_name=info.field_name)

    @field_validator("requested_by")
    @classmethod
    def _valid_requester(cls, v: str | None) -> str | None:
        return named_actor(v, min_len=1, field_name="requested_by")


class DispatchDecisionRequest(BaseModel):
    """POST /dispatches/{id}/decision — the HUMAN approve/defer half.

    ``officer`` is required unless the request carries the deck-verified
    ``x-veer-officer`` header (then the body officer is only a claim).
    """

    action: str
    officer: str | None = Field(default=None, min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("action")
    @classmethod
    def _valid_action(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in ("approve", "defer"):
            raise ValueError("action must be 'approve' or 'defer'")
        return normalized

    @field_validator("officer")
    @classmethod
    def _valid_officer(cls, v: str | None) -> str | None:
        return named_actor(v, min_len=1)

    @field_validator("note")
    @classmethod
    def _clean_note(cls, v: str | None) -> str | None:
        return clean_text(v, field_name="note")


class DispatchStatusRequest(BaseModel):
    """POST /dispatches/{id}/status — post-approval lifecycle.

    ``officer`` as for the decision: required unless ``x-veer-officer``
    names the authenticated actor.
    """

    status: str
    officer: str | None = Field(default=None, min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in ("en_route", "on_scene", "completed", "cancelled"):
            raise ValueError(
                "status must be en_route|on_scene|completed|cancelled"
            )
        return v

    @field_validator("officer")
    @classmethod
    def _valid_officer(cls, v: str | None) -> str | None:
        return named_actor(v, min_len=1)

    @field_validator("note")
    @classmethod
    def _clean_note(cls, v: str | None) -> str | None:
        return clean_text(v, field_name="note")


def _coerce_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _loopback_transport(request: Request) -> bool:
    """True when the request arrived on a loopback interface.

    ``scope["server"]`` is the local address the connection was accepted
    on — unlike ``scope["client"]``, no forwarded-for header can rewrite
    it — so this holds exactly for connections made on this host.
    """
    server = request.scope.get("server")
    host = server[0] if isinstance(server, (tuple, list)) and server else None
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    return addr.is_loopback or bool(mapped is not None and mapped.is_loopback)


def authenticated_officer(request: Request) -> str | None:
    """The deck-verified officer named by ``x-veer-officer`` (or None).

    TRUST: the header is trusted ONLY because the oracle binds loopback
    (WP-08) and the deck — the one LAN-facing surface — sets it from a
    verified session after stripping any client copy.  It is therefore
    honoured only on a connection that arrived on a loopback interface;
    elsewhere it is ignored (and logged) and the request is handled
    exactly as if it were absent.

    A trusted header is decoded once (strict UTF-8 percent-decoding) and
    held to the named-actor rule: whitespace runs collapse, 2..64
    characters, no control or bidi characters.  A malformed one — or one
    sent more than once, which names no single officer — is a 422, never
    silently replaced by the body's officer.
    """
    values = request.headers.getlist(OFFICER_HEADER)
    if not values:
        return None
    if not _loopback_transport(request):
        logger.warning(
            "officer_header.ignored",
            path=request.url.path,
            reason="request did not arrive on a loopback interface",
        )
        return None
    if len(values) > 1:
        raise HTTPException(
            status_code=422, detail=f"{OFFICER_HEADER} must be sent exactly once"
        )
    raw = values[0]
    if not raw.isascii() or _BAD_PERCENT_ESCAPE_RE.search(raw):
        raise HTTPException(
            status_code=422,
            detail=f"{OFFICER_HEADER} must be percent-encoded (encodeURIComponent)",
        )
    try:
        decoded = unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=422, detail=f"{OFFICER_HEADER} is not percent-encoded UTF-8"
        ) from exc
    # Strict before the shared rule: named_actor folds whitespace-class
    # controls (line breaks, separators) into spaces; an identity header
    # carrying one at all is malformed, never silently repaired.
    if any(unicodedata.category(c) == "Cc" for c in decoded):
        raise HTTPException(
            status_code=422, detail=f"{OFFICER_HEADER} must not contain control characters"
        )
    try:
        return named_actor(
            decoded,
            min_len=OFFICER_HEADER_MIN_LEN,
            max_len=OFFICER_HEADER_MAX_LEN,
            field_name=OFFICER_HEADER,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def attributed_actor(
    header_officer: str | None, body_officer: str | None
) -> tuple[str, dict | None]:
    """``(actor, audit detail)`` for one human write (pure).

    Without the header the body officer is the actor and there is no
    detail (unchanged behaviour); with neither, the write is refused
    (422 — no state change without a named human).  With the header, the
    header is the actor and the detail records that it came from the
    verified session — plus the body's ``claimed_officer`` whenever the
    two differ (compared after the same whitespace collapse).
    """
    if header_officer is None:
        if body_officer is None:
            raise HTTPException(
                status_code=422,
                detail="officer is required (a named human makes this decision)",
            )
        return body_officer, None
    detail: dict = {"actor": header_officer, "actor_source": OFFICER_HEADER}
    if body_officer is not None and " ".join(body_officer.split()) != header_officer:
        detail["claimed_officer"] = body_officer
    return header_officer, detail


#: Presence of corridor.dispatch_events.detail (migration 014), cached:
#: (monotonic expiry, present).  A store migrating at runtime is picked up.
_DETAIL_COLUMN_TTL_SEC = 30.0
_DETAIL_COLUMN: list[tuple[float, bool]] = []


async def _audit_detail_supported(conn: Any) -> bool:
    now = time.monotonic()
    if _DETAIL_COLUMN and _DETAIL_COLUMN[0][0] > now:
        return _DETAIL_COLUMN[0][1]
    present = bool(await conn.fetchval(_SQL_AUDIT_DETAIL_COLUMN))
    _DETAIL_COLUMN[:] = [(now + _DETAIL_COLUMN_TTL_SEC, present)]
    return present


def reset_audit_detail_probe() -> None:
    """Forget the cached detail-column probe (tests / operator tooling)."""
    _DETAIL_COLUMN.clear()


def note_with_claim(note: str | None, detail: Mapping[str, Any] | None) -> str | None:
    """``note`` + ``[x-veer-officer: <actor>; claimed officer: <claim>]``
    when the body named a DIFFERENT officer than the verified header.

    Ledgers without a detail column (pre-014 dispatch_events, every
    event_status_log row) carry the claim in the note, so it is never lost.
    """
    claimed = (detail or {}).get("claimed_officer")
    if not claimed:
        return note
    suffix = f"[{OFFICER_HEADER}: {(detail or {}).get('actor')}; claimed officer: {claimed}]"
    return f"{note} {suffix}" if note else suffix


def _note_with_detail(note: str | None, detail: Mapping[str, Any]) -> str | None:
    """Pre-014 stores have no ``detail`` column: a claimed officer then
    travels in the note, so the claim is never lost."""
    return note_with_claim(note, detail)


async def _audit(conn: Any, dispatch_id: str, from_status: str | None, to_status: str, actor: str, note: str | None, detail: Mapping[str, Any] | None = None) -> None:  # noqa: E501
    """Append one row to the dispatch audit tail (parameter-bound).

    Always called inside the transaction of the mutation it records.
    ``detail`` (the actor's provenance, WP-16) goes to the 014 ``detail``
    column; without one the statement is exactly the pre-014 insert.
    """
    if detail is None:
        audit_insert = "INSERT INTO corridor.dispatch_events (dispatch_id, from_status, to_status, actor, note) VALUES ($1, $2, $3, $4, $5)"  # noqa: E501
        await conn.execute(audit_insert, dispatch_id, from_status, to_status, actor, note)
        return
    if await _audit_detail_supported(conn):
        detail_insert = "INSERT INTO corridor.dispatch_events (dispatch_id, from_status, to_status, actor, note, detail) VALUES ($1, $2, $3, $4, $5, $6::jsonb)"  # noqa: E501
        await conn.execute(
            detail_insert, dispatch_id, from_status, to_status, actor, note, json.dumps(detail)
        )
        return
    logger.warning("dispatch.audit_detail_column_missing", migration="014")
    audit_insert = "INSERT INTO corridor.dispatch_events (dispatch_id, from_status, to_status, actor, note) VALUES ($1, $2, $3, $4, $5)"  # noqa: E501
    await conn.execute(
        audit_insert, dispatch_id, from_status, to_status, actor, _note_with_detail(note, detail)
    )


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


def _detail_out(raw: Any) -> dict | None:
    """An audit row's ``detail`` (jsonb text or a mapping) → dict | None."""
    if isinstance(raw, Mapping):
        return dict(raw) or None
    return json_doc(raw) or None


def _audit_row_out(r: Any) -> dict:
    return {
        "from_status": r["from_status"],
        "to_status": r["to_status"],
        "actor": r["actor"],
        "note": r["note"],
        "at": _coerce_utc(r["at"]).isoformat(),
        # additive (WP-16): the actor's provenance — {actor, actor_source,
        # claimed_officer?} when the deck-verified header named it; null
        # for body-attributed and pre-014 rows.
        "detail": _detail_out(r.get("detail")),
    }


async def _audit_tail(conn: Any, dispatch_id: str, limit: int = 20) -> list[dict]:
    rows = await conn.fetch(_SQL_AUDIT_TAIL, dispatch_id, limit)
    return [_audit_row_out(r) for r in rows]


async def _audit_tails(conn: Any, dispatch_ids: list[str], limit: int = 10) -> dict[str, list[dict]]:  # noqa: E501
    """Audit tails for many dispatches in ONE query (no N+1)."""
    if not dispatch_ids:
        return {}
    ids = [uuid.UUID(d) for d in dispatch_ids]
    tails: dict[str, list[dict]] = {}
    for r in await conn.fetch(_SQL_AUDIT_TAILS, ids, limit):
        tails.setdefault(str(r["dispatch_id"]), []).append(_audit_row_out(r))
    return tails


def closure_status(dispatch_status: str, event_to_status: str) -> str:
    """Where an open dispatch goes when its event closes (pure).

    A unit ON SCENE when its event COMPLETES has completed its assignment;
    every other open dispatch (requested, approved, en route — or any
    dispatch of a CANCELLED event) is cancelled.  Both targets are legal
    moves in DISPATCH_TRANSITIONS from every open status.
    """
    if event_to_status == "completed" and dispatch_status == "on_scene":
        return "completed"
    return "cancelled"


async def close_event_dispatches(
    conn: Any, event_id: str, event_to_status: str, note: str | None
) -> list[dict]:
    """Walk the event's open dispatches to completed/cancelled.

    MUST run inside the caller's transaction (the event-status change):
    rows are locked FOR UPDATE, each move is checked against
    DISPATCH_TRANSITIONS and audited with actor ``system:event_closed``.
    Returns ``[{dispatch_id, unit_id, agency, from_status, to_status}]``.
    """
    close_update = "UPDATE corridor.dispatches SET status = $1, updated_at = NOW() WHERE dispatch_id = $2 AND status = $3 RETURNING dispatch_id"  # noqa: E501
    closed: list[dict] = []
    rows = await conn.fetch(
        _SQL_OPEN_EVENT_DISPATCHES, event_id, list(OPEN_DISPATCH_STATUSES)
    )
    for r in rows:
        did = str(r["dispatch_id"])
        from_status = str(r["status"])
        to_status = closure_status(from_status, event_to_status)
        if to_status not in DISPATCH_TRANSITIONS.get(from_status, ()):
            continue  # never an illegal walk, even from a corrupt row
        moved = await conn.fetchrow(close_update, to_status, did, from_status)
        if moved is None:
            continue
        await _audit(conn, did, from_status, to_status, EVENT_CLOSED_ACTOR, note)
        closed.append(
            {
                "dispatch_id": did,
                "unit_id": r["unit_id"],
                "agency": r["agency"],
                "from_status": from_status,
                "to_status": to_status,
            }
        )
    return closed


# ---------------------------------------------------------------------------
# POST /events/{event_id}/dispatch — REQUEST
# ---------------------------------------------------------------------------


@router.post("/events/{event_id}/dispatch")
async def create_dispatch(
    event_id: str, req: DispatchRequest, request: Request
) -> dict:
    """The REQUEST half: a 'requested' row a duty officer must decide.

    The deck-verified ``x-veer-officer`` is the requester (stored as
    ``requested_by`` and as the audit actor, with its provenance in the
    audit ``detail``); a differing body ``requested_by`` is kept there as
    ``claimed_officer``.  Without the header the body names the requester
    ('operator' when it names nobody), exactly as before.
    """
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    try:
        eid = str(uuid.UUID(event_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid event_id") from exc
    header = authenticated_officer(request)
    if header is None:
        requester, detail = req.requested_by or "operator", None
    else:
        requester, detail = attributed_actor(header, req.requested_by)

    dispatch_insert = "INSERT INTO corridor.dispatches (event_id, agency, unit_id, waypoint_junction, status, requested_by, request_payload) VALUES ($1, $2, $3, $4, 'requested', $5, $6::jsonb) RETURNING dispatch_id, created_at"  # noqa: E501
    payload = {"note": req.note} if req.note else {}
    payload_json = json.dumps(payload)
    async with pool.acquire() as conn:
        async with conn.transaction():
            # FOR SHARE: an event closing concurrently cannot slip past
            # this request (its transition takes FOR UPDATE on the row).
            event = await conn.fetchrow(_SQL_EVENT_CHECK, eid)
            if event is None:
                raise HTTPException(status_code=404, detail="event not found")
            if event["status"] not in LIVE_EVENT_STATUSES:
                raise HTTPException(
                    status_code=409,
                    detail="event is not active (status=" + str(event["status"]) + ")",
                )
            row = await conn.fetchrow(
                dispatch_insert,
                eid,
                req.agency,
                req.unit_id,
                req.waypoint_junction,
                requester,
                payload_json,
            )
            dispatch_id = str(row["dispatch_id"])
            await _audit(conn, dispatch_id, None, "requested", requester, req.note, detail)

    logger.info(
        "dispatch.requested",
        dispatch_id=dispatch_id,
        agency=req.agency,
        event_id=eid,
        unit=req.unit_id,
        requested_by=requester,
        actor_source=OFFICER_HEADER if detail else "body",
        claimed_officer=(detail or {}).get("claimed_officer"),
    )
    return {
        "dispatch_id": dispatch_id,
        "event_id": eid,
        "agency": req.agency,
        "agency_label": agency_label(req.agency),
        "unit_id": req.unit_id,
        "requested_by": requester,
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

    actor, detail = attributed_actor(authenticated_officer(request), req.officer)
    to_status = "approved" if req.action == "approve" else "deferred"
    decision_update = "UPDATE corridor.dispatches SET status = $1, decided_by = $2, decision_note = $3, approved_at = CASE WHEN $1 = 'approved' THEN NOW() ELSE approved_at END, updated_at = NOW() WHERE dispatch_id = $4 AND status = 'requested' RETURNING dispatch_id"  # noqa: E501

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(decision_update, to_status, actor, req.note, did)
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
            await _audit(conn, did, "requested", to_status, actor, req.note, detail)
        result = await _get_dispatch(conn, did)
        result["audit"] = await _audit_tail(conn, did)

    logger.info(
        "dispatch.decision",
        dispatch_id=did,
        decision=to_status,
        officer=actor,
        actor_source=OFFICER_HEADER if detail else "body",
        claimed_officer=(detail or {}).get("claimed_officer"),
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

    actor, detail = attributed_actor(authenticated_officer(request), req.officer)
    status_update = "UPDATE corridor.dispatches SET status = $1, updated_at = NOW() WHERE dispatch_id = $2 AND status = $3 RETURNING dispatch_id"  # noqa: E501
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(_SQL_DISPATCH_GET_FOR_UPDATE, did)
            if row is None:
                raise HTTPException(status_code=404, detail="dispatch not found")
            current_status = str(row["status"])
            allowed = DISPATCH_TRANSITIONS.get(current_status, ())
            if req.status not in allowed:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "invalid transition",
                        "from": current_status,
                        "to": req.status,
                        "allowed": list(allowed),
                    },
                )
            moved = await conn.fetchrow(status_update, req.status, did, current_status)
            if moved is None:
                raise HTTPException(status_code=409, detail="dispatch state changed")
            await _audit(conn, did, current_status, req.status, actor, req.note, detail)
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
        tails = await _audit_tails(conn, [d["dispatch_id"] for d in dispatches], limit=10)
    for d in dispatches:
        d["audit"] = tails.get(d["dispatch_id"], [])
    return {"dispatches": dispatches, "count": len(dispatches)}


# ---------------------------------------------------------------------------
# GET /dispatches/{dispatch_id} — one dispatch + its audit tail
# ---------------------------------------------------------------------------


@router.get("/dispatches/{dispatch_id}")
async def get_dispatch(dispatch_id: str, request: Request) -> dict:
    """One dispatch — the row shape of GET /dispatches — with its audit
    tail, newest first (a dispatch has at most one row per lifecycle step,
    so the tail is whole).  404 unknown, 422 malformed uuid."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    try:
        did = str(uuid.UUID(dispatch_id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid dispatch_id (uuid expected)") from exc
    async with pool.acquire() as conn:
        result = await _get_dispatch(conn, did)
        if result is None:
            raise HTTPException(status_code=404, detail="dispatch not found")
        result["audit"] = await _audit_tail(conn, did)
    return result


# ---------------------------------------------------------------------------
# Unit collection — dispatch projection + emulator patrol frames
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _UnitPath:
    """Resolved geometry of one dispatch's emulated route (cacheable)."""

    path_pts: tuple[tuple[float, float], ...]  # start -> ... -> the junction (>= 2)
    hold_pt: tuple[float, float] | None        # where the unit waits without a path
    staged_at: str = "spine_start"             # spine_start | station:<station_id>
    staging_name: str | None = None            # the post's name when staged at one


#: The unit path is a deterministic function of (dispatch, waypoint, event
#: spine, graph).  Cached per dispatch for a few minutes so the 4-8 s canvas
#: polls stop re-running pgr_dijkstra for every fielded unit (a re-routed
#: event's units restage on its new spine when their entry expires).
_PATH_CACHE_TTL_SEC = 300.0
_PATH_CACHE_MAX = 512
_PATH_CACHE: dict[tuple[str, int], tuple[float, _UnitPath]] = {}

#: Event kind whose units come from their agency's post (C-N interception).
PURSUIT_EVENT_KIND = "vehicle_pursuit"


def reset_path_cache() -> None:
    _PATH_CACHE.clear()


class _GeomCache:
    """Per-request segment / junction point lookups on one connection."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.segments: dict[int, tuple[float, float]] = {}
        self.junctions: dict[int, tuple[float, float] | None] = {}

    async def segment_points(self, ids: list[int]) -> dict[int, tuple[float, float]]:
        missing = [i for i in dict.fromkeys(ids) if i not in self.segments]
        if missing:
            sql = (
                _SQL_SEGMENT_POINTS_DISPLAY
                if await display_geom_supported(self.conn)
                else _SQL_SEGMENT_POINTS
            )
            for row in await self.conn.fetch(sql, missing):
                self.segments[int(row["segment_id"])] = (float(row["lon"]), float(row["lat"]))
        return {i: self.segments[i] for i in ids if i in self.segments}

    async def junction_lonlat(self, jid: int) -> tuple[float, float] | None:
        if jid not in self.junctions:
            found = await self.conn.fetch(_SQL_JUNCTION_GEOM, [jid])
            self.junctions[jid] = (
                (float(found[0]["lon"]), float(found[0]["lat"])) if found else None
            )
        return self.junctions[jid]


async def _shortest_path_on(conn: Any, source: int, target: int) -> list[Any]:
    """pgr_dijkstra over corridor.road_graph on the HELD connection.

    Wrapped in its own (sub)transaction: inside a caller's transaction it
    is a savepoint, so a failed search can never poison that transaction.
    """
    async with conn.transaction():
        return list(await conn.fetch(_SQL_SHORTEST_PATH, int(source), int(target)))


def cheapest_path(paths: dict[int, list[Any]]) -> list[int]:
    """The node (segment) ids of the cheapest of several searched paths
    (``{target: pgr rows}``; cost = the path's last agg_cost; an empty
    path — no route — never wins; ties go to the lower target).  Pure."""
    best: tuple[float, int] | None = None
    for target, rows in paths.items():
        if len(rows) < 2:
            continue
        cost = float(rows[-1]["agg_cost"])
        if best is None or (cost, target) < best:
            best = (cost, target)
    if best is None:
        return []
    return [int(r["node"]) for r in paths[best[1]]]


async def _nearest_station(conn: Any, agency: str, waypoint: int) -> dict | None:
    """The agency's post nearest the waypoint junction (None without one,
    or on a store that predates migration 012)."""
    try:
        async with conn.transaction():
            row = await conn.fetchrow(_SQL_NEAREST_STATION, agency, int(waypoint))
    except Exception:  # noqa: BLE001 — no stations table: stage on the spine
        return None
    if row is None or row["lon"] is None or row["lat"] is None:
        return None
    return {
        "station_id": str(row["station_id"]),
        "name": row["name"],
        "point": (float(row["lon"]), float(row["lat"])),
        "segment_id": int(row["segment_id"]) if row["segment_id"] is not None else None,
    }


def _dedupe(points: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    out: list[tuple[float, float]] = []
    for pt in points:
        if not out or out[-1] != pt:
            out.append(pt)
    return tuple(out)


async def _resolve_path(
    geo: _GeomCache, graph_db: Any, event_id: str, waypoint: int, agency: str | None = None,
) -> tuple[_UnitPath, bool]:
    """The unit's path in the road_graph's id space (SEGMENT ids): from its
    start to the nearest APPROACH segment of the waypoint junction, then the
    junction point itself — the unit arrives ON the junction (C-N).
    Returns ``(path, cacheable)`` — a transient graph failure is not cached.

    The start is the event's first spine segment, except for a pursuit: its
    units come from their agency's post nearest the waypoint, not from the
    suspect's last sighting.  A start on an approach segment drives straight
    to the junction.  ``graph_db`` (app.state.graph_db) marks the routing
    graph as wired; the search itself runs on the connection this request
    already holds.  Without a graph path the unit waits at its start.
    """
    conn = geo.conn
    spine_sql = (
        _SQL_EVENT_SPINE_START_CURRENT
        if await routes_supersede_supported(conn)
        else _SQL_EVENT_SPINE_START
    )
    spine = await conn.fetchrow(spine_sql, event_id)
    first_seg = (
        int(list(spine["segment_ids"])[0]) if spine and spine["segment_ids"] else None
    )
    pursuit = bool(spine) and spine.get("event_kind") == PURSUIT_EVENT_KIND
    approaches = [
        int(r["segment_id"])
        for r in await conn.fetch(_SQL_JUNCTION_ADJACENT_SEGMENT, int(waypoint))
        if r["segment_id"] is not None
    ]
    junction = await geo.junction_lonlat(int(waypoint))

    start: tuple[float, float] | None = None
    start_seg: int | None = None
    staged_at, staging_name = "spine_start", None
    station = await _nearest_station(conn, agency, waypoint) if pursuit and agency else None
    if station is not None:
        start, start_seg = station["point"], station["segment_id"]
        staged_at, staging_name = f"station:{station['station_id']}", station["name"]
    elif first_seg is not None:
        start, start_seg = (await geo.segment_points([first_seg])).get(first_seg), first_seg

    path_pts: tuple[tuple[float, float], ...] = ()
    cacheable = True
    if start is not None and junction is not None:
        if start_seg is not None and start_seg in approaches:
            path_pts = _dedupe([start, junction])  # on an approach already
        elif graph_db is not None and start_seg is not None and approaches:
            try:
                node_ids = cheapest_path({
                    target: await _shortest_path_on(conn, start_seg, target)
                    for target in approaches
                })
            except Exception:  # noqa: BLE001 — degrade to the start, retry next poll
                node_ids = []
                cacheable = False
            if node_ids:
                pts_by_seg = await geo.segment_points(node_ids)
                path_pts = _dedupe(
                    [start, *(pts_by_seg[n] for n in node_ids if n in pts_by_seg), junction]
                )
    hold = start if start is not None else junction
    return (
        _UnitPath(path_pts=path_pts, hold_pt=hold, staged_at=staged_at,
                  staging_name=staging_name),
        cacheable,
    )


async def _unit_path(
    geo: _GeomCache, graph_db: Any, dispatch_id: str, event_id: str, waypoint: int,
    agency: str | None = None,
) -> _UnitPath:
    key = (dispatch_id, int(waypoint))
    now = time.monotonic()
    hit = _PATH_CACHE.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    path, cacheable = await _resolve_path(geo, graph_db, event_id, waypoint, agency)
    if cacheable:
        if len(_PATH_CACHE) >= _PATH_CACHE_MAX:
            _PATH_CACHE.clear()
        _PATH_CACHE[key] = (now + _PATH_CACHE_TTL_SEC, path)
    return path


async def _project_dispatch(
    geo: _GeomCache, graph_db: Any, r: Any, now: datetime
) -> dict:
    """One approved dispatch → its emulated unit (deterministic)."""
    dispatch_id = str(r["dispatch_id"])
    waypoint = r["waypoint_junction"]
    approved_at = r["approved_at"]
    unit: dict[str, Any] = {
        "unit_id": r["unit_id"],
        "dispatch_id": dispatch_id,
        "agency": r["agency"],
        "agency_label": agency_label(r["agency"]),
        "label": agency_label(r["agency"]) + " · " + str(r["unit_id"]),
        "assigned_event_id": str(r["event_id"]),
        "waypoint_junction": int(waypoint) if waypoint else None,
        "approved_at": iso_z(approved_at),
        "status": r["status"],
        "emulated": True,
        "source": PROJECTION_SOURCE,
        "double_booked": False,
    }
    if waypoint is None or approved_at is None:
        # No waypoint/no approval clock: stationary, still honest about
        # being emulated.
        unit.update({"position": None, "eta_sec": None, "arrived": False})
        return unit

    seed = dispatch_seed(dispatch_id)
    speed = seeded_speed_kmh(seed)
    elapsed = max(0.0, (now - _coerce_utc(approved_at)).total_seconds())
    path = await _unit_path(
        geo, graph_db, dispatch_id, str(r["event_id"]), int(waypoint), str(r["agency"])
    )
    unit["staged_at"] = path.staged_at
    if path.staging_name:
        unit["staged_at_name"] = path.staging_name

    if len(path.path_pts) >= 2:
        # pgr edge costs are not wall-seconds (unit-ambiguous); the
        # projection derives per-leg durations from the geometry at the
        # unit's seeded speed instead — deterministic and physical.  The
        # last point IS the junction: 'arrived' lands on it, never short.
        state = travel_state(list(path.path_pts), [], elapsed, speed)
        if state is not None:
            unit.update(
                {
                    # the lateral de-stacking offset only while moving: an
                    # arrived unit stands on the junction itself
                    "position": list(
                        state.position if state.arrived
                        else offset_position(state.position, seeded_bearing_offset_deg(seed))
                    ),
                    "progress": state.progress,
                    "eta_sec": state.remaining_sec,
                    "arrived": state.arrived,
                    "speed_kmh": speed,
                }
            )
            return unit

    # No graph path: the unit waits where it starts — never 'arrived'
    # short of the junction, and without an ETA it cannot honestly give.
    if path.hold_pt is not None:
        unit.update(
            {
                "position": list(path.hold_pt),
                "progress": 0.0,
                "eta_sec": None,
                "arrived": False,
                "speed_kmh": speed,
            }
        )
    else:
        unit.update({"position": None, "eta_sec": None, "arrived": False})
    return unit


def patrol_unit(key: str, doc: Mapping[str, Any], now: datetime) -> dict | None:
    """One C-A patrol frame → unit row; None when it fails validation.

    Required: a slug unit_id (doc or key), a registry agency and a finite
    lon/lat.  Optional fields degrade to None; unknown statuses surface as
    'unknown' rather than being guessed.
    """
    unit_id = doc.get("unit_id") or key[len(EMULATED_UNIT_KEY_PREFIX):]
    if not is_slug(unit_id):
        return None
    agency = doc.get("agency")
    if not isinstance(agency, str) or not is_agency(agency):
        return None
    point = valid_lonlat(doc.get("lon"), doc.get("lat"))
    if point is None:
        return None
    status = doc.get("status")
    status = status if status in PATROL_STATUSES else "unknown"
    t = parse_ts(doc.get("t"))
    label = doc.get("label")
    if not isinstance(label, str) or not label.strip():
        label = agency_label(agency) + " · " + unit_id
    run_id = doc.get("run_id")
    return {
        "unit_id": unit_id,
        "dispatch_id": None,
        "agency": agency,
        "agency_label": agency_label(agency),
        "label": label[:120],
        "assigned_event_id": None,
        "waypoint_junction": None,
        "status": status,
        "position": [round(point[0], 6), round(point[1], 6)],
        "heading": finite(doc.get("heading")),
        "speed_kmh": finite(doc.get("speed_kmh")),
        "t": iso_z(t),
        "age_sec": round(max(0.0, (now - t).total_seconds()), 1) if t else None,
        "eta_sec": None,
        "progress": None,
        "arrived": status == "on_scene",
        "run_id": str(run_id) if run_id is not None else None,
        "emulated": True,
        "source": PATROL_SOURCE,
        "double_booked": False,
    }


async def read_patrol_units(valkey: Any, now: datetime) -> tuple[list[dict], dict]:
    """Emulator patrol frames from Valkey (contract C-A), validated."""
    if valkey is None:
        return [], {"status": "unavailable", "keys": 0, "rejected": 0}
    try:
        pairs, truncated_after = await scan_mget(
            valkey, EMULATED_UNIT_KEY_PREFIX, max_keys=MAX_PATROL_KEYS
        )
    except Exception as exc:  # noqa: BLE001 — Valkey down must not 500 /units
        logger.warning("units.patrol_scan_failed", error=repr(exc))
        return [], {"status": "error", "keys": 0, "rejected": 0}
    units: list[dict] = []
    rejected = 0
    for key, raw in pairs:
        doc = json_doc(raw)
        if doc is None:
            if raw is not None:
                rejected += 1
            continue  # expired between SCAN and MGET
        unit = patrol_unit(key, doc, now)
        if unit is None:
            rejected += 1
            continue
        units.append(unit)
    return units, {
        "status": "ok",
        "keys": len(pairs),
        "rejected": rejected,
        "truncated": truncated_after > 0,
    }


async def collect_units(
    conn: Any, *, graph_db: Any, valkey: Any, now: datetime
) -> tuple[list[dict], dict]:
    """Every fielded emulated unit — one row per unit_id.

    Projection rows come from approved dispatches of LIVE events (the
    latest approval wins per unit); a C-A patrol frame for the same unit
    replaces the projected POSITION (the observed frame takes precedence
    over the interpolation, per units/projection.py) while the dispatch
    keeps the assignment.  Units with no dispatch are patrol-only rows.

    A unit approved on SEVERAL live dispatches (double-booked across
    events) is projected for its latest approval, flagged
    ``double_booked: true`` with ``other_dispatches``, and listed in
    ``meta.conflicts`` — one row per unit never hides the conflict.
    """
    rows = await conn.fetch(_SQL_ACTIVE_DISPATCHES, list(LIVE_UNIT_STATUSES), 200)
    geo = _GeomCache(conn)
    held: dict[str, list[Any]] = {}
    for r in rows:  # ordered by approval: the last row per unit is the latest
        held.setdefault(str(r["unit_id"]), []).append(r)
    by_unit: dict[str, dict] = {}
    conflicts: list[dict] = []
    for unit_id, unit_rows in held.items():
        latest = unit_rows[-1]
        unit = await _project_dispatch(geo, graph_db, latest, now)
        if len(unit_rows) > 1:
            others = [
                {
                    "dispatch_id": str(x["dispatch_id"]),
                    "event_id": str(x["event_id"]),
                    "status": x["status"],
                }
                for x in unit_rows[:-1]
            ]
            unit["double_booked"] = True
            unit["other_dispatches"] = others
            conflicts.append(
                {
                    "unit_id": unit_id,
                    "dispatch_ids": [str(x["dispatch_id"]) for x in unit_rows],
                    "event_ids": list(dict.fromkeys(str(x["event_id"]) for x in unit_rows)),
                }
            )
        by_unit[unit_id] = unit
    projected = len(by_unit)

    patrols, patrol_meta = await read_patrol_units(valkey, now)
    merged = 0
    for p in sorted(patrols, key=lambda u: u["unit_id"]):
        current = by_unit.get(p["unit_id"])
        if current is None:
            by_unit[p["unit_id"]] = p
            continue
        merged += 1
        current.update(
            {
                "position": p["position"],
                "heading": p["heading"],
                "speed_kmh": p["speed_kmh"],
                "t": p["t"],
                "age_sec": p["age_sec"],
                "run_id": p["run_id"],
                "eta_sec": None,     # no ETA is derived from a patrol frame
                "progress": None,
                "source": PATROL_SOURCE,
                "position_source": PATROL_SOURCE,
                "assignment_source": "dispatch",
            }
        )
    meta = {
        "sources": {
            PROJECTION_SOURCE: projected - merged,
            PATROL_SOURCE: len(patrols),
        },
        "patrol_feed": patrol_meta,
        "conflicts": conflicts,
    }
    return list(by_unit.values()), meta


# ---------------------------------------------------------------------------
# GET /units — emulated agency units (C1 canvas contract)
# ---------------------------------------------------------------------------


@router.get("/units")
async def list_units(request: Request) -> dict:
    """EMULATED units: dispatch projections of live events + patrol frames.

    Every unit carries ``emulated: true`` and its source label — the badge
    is never optional.  Dispatches of completed/cancelled events project
    nothing (their units are released).
    """
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        units, meta = await collect_units(
            conn,
            graph_db=getattr(request.app.state, "graph_db", None),
            valkey=getattr(request.app.state, "adaptive_valkey", None),
            now=now,
        )
    return {
        "units": units,
        "count": len(units),
        "source": "emulated",
        **meta,
        "note": "emulated units only — dispatch projections of live events (deterministic from dispatch_id) and emulator patrol frames (units:emulated:*); a patrol frame overrides a projected position",  # noqa: E501
    }


__all__ = [
    "DISPATCH_TRANSITIONS",
    "PURSUIT_EVENT_KIND",
    "EMULATED_UNIT_KEY_PREFIX",
    "EVENT_CLOSED_ACTOR",
    "LIVE_EVENT_STATUSES",
    "LIVE_UNIT_STATUSES",
    "OFFICER_HEADER",
    "OPEN_DISPATCH_STATUSES",
    "PATROL_SOURCE",
    "PROJECTION_SOURCE",
    "attributed_actor",
    "authenticated_officer",
    "cheapest_path",
    "close_event_dispatches",
    "closure_status",
    "collect_units",
    "note_with_claim",
    "patrol_unit",
    "read_patrol_units",
    "reset_audit_detail_probe",
    "router",
]
