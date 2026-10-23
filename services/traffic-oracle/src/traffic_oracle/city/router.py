"""City fusion HTTP surface (contract C-C) — mounted by coordination.py.

    GET /api/v1/cameras                       ANPR/CCTV layer (locations + counts)
    GET /api/v1/city/entities?kinds=&bbox=    fused snapshot of the city
    GET /api/v1/city/entity/{kind}/{id}       dossier: entity + related + audit
    GET /api/v1/city/feed?since=&limit=&kinds=  time-ordered narrative
    GET /api/v1/events/{event_id}/audit?limit=&since=
                                              the feed scoped to one event
                                              (+ additive ``entries`` bridge)

Read-only, parameter-bound, no outbound HTTP.  Every id is validated
before it reaches SQL (uuid kinds, positive ints, bounded slugs → 422 on
anything else); an unknown entity or event is a 404 ``{detail}``.  The
Valkey-backed sources (patrol units, convoy GPS, brain decisions) degrade
to a labelled ``unavailable`` when no client is wired — never a 500.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request

from traffic_oracle.city.cameras import load_cameras, public_camera
from traffic_oracle.city.common import (
    DOSSIER_KINDS,
    ENTITY_KINDS,
    EPOCH,
    FEED_KINDS,
    iso_z,
    parse_bbox,
    parse_entity_id,
    parse_kinds,
    utc,
)
from traffic_oracle.city.dossier import build_dossier
from traffic_oracle.city.entities import build_snapshot
from traffic_oracle.city.feed import GLOBAL_LOOKBACK, collect_feed, event_audit

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["city"])


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    return pool


def _valkey(request: Request) -> Any:
    return getattr(request.app.state, "adaptive_valkey", None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# GET /cameras
# ---------------------------------------------------------------------------


@router.get("/cameras")
async def cameras(request: Request) -> dict:
    """Every ANPR/CCTV camera: location, kind, source, 15-minute counters.

    Locations and counts ONLY (C6): no plate, frame or image key exists
    on this surface.
    """
    pool = _pool(request)
    now = _now()
    async with pool.acquire() as conn:
        cams, meta = await load_cameras(conn, now)
    return {
        "cameras": [public_camera(c) for c in cams],
        "count": len(cams),
        "registry": meta["registry"],
        "window_sec": meta["window_sec"],
        "as_of": iso_z(now),
    }


# ---------------------------------------------------------------------------
# GET /city/entities
# ---------------------------------------------------------------------------


@router.get("/city/entities")
async def city_entities(
    request: Request,
    kinds: str | None = Query(default=None, max_length=256, description="CSV of kinds"),
    bbox: str | None = Query(
        default=None, max_length=128, description="minLon,minLat,maxLon,maxLat"
    ),
) -> dict:
    """The fused city snapshot (contract C-C)."""
    try:
        wanted = parse_kinds(kinds, ENTITY_KINDS)
        box = parse_bbox(bbox)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    pool = _pool(request)
    async with pool.acquire() as conn:
        return await build_snapshot(
            conn,
            kinds=wanted,
            bbox=box,
            now=_now(),
            valkey=_valkey(request),
            graph_db=getattr(request.app.state, "graph_db", None),
        )


# ---------------------------------------------------------------------------
# GET /city/entity/{kind}/{id}
# ---------------------------------------------------------------------------


@router.get("/city/entity/{kind}/{entity_id}")
async def city_entity_dossier(
    kind: str,
    entity_id: str,
    request: Request,
    audit_limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """One entity's dossier: itself, 1-hop related entities, audit trail."""
    if kind not in DOSSIER_KINDS:
        raise HTTPException(
            status_code=422, detail=f"kind must be one of {list(DOSSIER_KINDS)}"
        )
    try:
        ident = parse_entity_id(kind, entity_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid {kind} id: {exc}") from exc
    pool = _pool(request)
    async with pool.acquire() as conn:
        dossier = await build_dossier(
            conn,
            kind,
            ident,
            now=_now(),
            valkey=_valkey(request),
            graph_db=getattr(request.app.state, "graph_db", None),
            audit_limit=audit_limit,
        )
    if dossier is None:
        raise HTTPException(status_code=404, detail=f"{kind}:{ident} not found")
    return dossier


# ---------------------------------------------------------------------------
# GET /city/feed
# ---------------------------------------------------------------------------


def _since(since: datetime | None, default: datetime) -> datetime:
    """Wire timestamps are UTC (contract): a naive ``since`` means UTC."""
    return utc(since) if since is not None else default


@router.get("/city/feed")
async def city_feed(
    request: Request,
    since: Annotated[datetime | None, Query(description="items with t > since")] = None,
    limit: int = Query(default=100, ge=1, le=500),
    kinds: str | None = Query(default=None, max_length=128, description="CSV of feed kinds"),
) -> dict:
    """The city narrative, newest first (contract C-C)."""
    try:
        wanted = parse_kinds(kinds, FEED_KINDS)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    now = _now()
    pool = _pool(request)
    async with pool.acquire() as conn:
        items, sources = await collect_feed(
            conn,
            since=_since(since, now - GLOBAL_LOOKBACK),
            limit=limit,
            kinds=wanted,
            valkey=_valkey(request),
        )
    return {"as_of": iso_z(now), "items": items, "sources": sources}


# ---------------------------------------------------------------------------
# GET /events/{event_id}/audit
# ---------------------------------------------------------------------------


@router.get("/events/{event_id}/audit")
async def event_audit_route(
    event_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    since: Annotated[datetime | None, Query()] = None,
) -> dict:
    """The feed filtered to one event and its movement / vehicle /
    recommendation / dispatch keys (contract C-C).

    ``items`` is the contract field (newest first).  ``entries`` is an
    additive deploy-order bridge: the SAME FeedItems oldest-first, the
    shape the pre-C-C deck reader takes (``resp.entries``, newest last) —
    so its AI audit panel stays truthful until every deck reads
    ``items``.
    """
    try:
        eid = _uuid.UUID(event_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid event uuid") from exc
    pool = _pool(request)
    async with pool.acquire() as conn:
        audit = await event_audit(
            conn,
            eid,
            since=_since(since, EPOCH),
            limit=limit,
            valkey=_valkey(request),
        )
    if audit is None:
        raise HTTPException(status_code=404, detail=f"event {event_id} not found")
    audit["entries"] = list(reversed(audit["items"]))
    return audit


__all__ = ["router"]
