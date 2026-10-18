"""Vehicle watchlist API — earmarks, sightings, traces (watchlist-anpr, 010).

HTTP surface (contract C1 shapes; all SQL lives in store.py, fully
parameter-bound):
    POST /api/v1/vehicles/{plate}/earmark        watch|flagged + lawful basis
    POST /api/v1/vehicles/{vehicle_id}/earmark/clear
    GET  /api/v1/vehicles?earmark=watch|flagged  active-earmark vehicles with
                                                 last sighting + source badge
    GET  /api/v1/vehicles/{vehicle_id}/trace?window_sec=
    GET  /api/v1/vehicles/{vehicle_id}/sightings?limit=

ETHICS RAILS (C6) enforced here, not by convention:
    * an earmark requires an enumerated lawful_basis and a retention_days
      value capped at 90 (mirrored by the migration 010 CHECK);
      retention_until is computed server-side — a client cannot stretch it.
    * every sighting response carries its ``source`` ('emulated_anpr' in
      this build) so the emulated badge can never be dropped by a view.
    * expired earmarks are filtered in the QUERY path — expiry is enforced
      on read, never promised by a cron.
    * the sightings/trace paths reap rows past retention (parameter-bound
      DELETE) before selecting.
    * vehicles only: no person-directed field exists on any path.
"""

from __future__ import annotations

import json
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from traffic_oracle.vehicles import store
from traffic_oracle.vehicles.sources import SOURCE_EMULATED

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/vehicles", tags=["vehicles"])

#: Default sighting retention (days) — bound as a timedelta into the reap.
SIGHTING_RETENTION_DAYS_DEFAULT = 30

#: Lawful-basis codes accepted for an earmark (India enforcement context:
#: Motor Vehicles Act 1988 s.177/s.183/s.207 powers, BNSS 2023 police
#: powers incl. pursuit, DPDP Act 2023 purpose-limitation).  The list is
#: intentionally small and government-capability framed.
LAWFUL_BASIS_CODES = (
    "mva_1988_s177",       # Motor Vehicles Act — general penalty/notice powers
    "mva_1988_s183",       # speed-limit enforcement
    "mva_1988_s207",       # power to inspect vehicles
    "bnss_2023_s94",       # police power to pursue / intercept
    "stolen_vehicle_fir",  # registered FIR naming the plate
)

EARMARK_STATES = ("watch", "flagged")


class EarmarkRequest(BaseModel):
    state: str = Field(description="watch | flagged")
    reason: str = Field(min_length=3, max_length=500)
    lawful_basis: str = Field(
        description=f"one of {LAWFUL_BASIS_CODES} (C6: NOT NULL, enumerated)",
    )
    retention_days: int = Field(
        default=30, ge=1, le=90, description="earmark retention horizon (C6 cap 90)"
    )
    earmarked_by: str = Field(default="operator", min_length=1, max_length=120)

    @field_validator("state")
    @classmethod
    def _valid_state(cls, v: str) -> str:
        if v not in EARMARK_STATES:
            raise ValueError(f"state must be one of {EARMARK_STATES}")
        return v

    @field_validator("lawful_basis")
    @classmethod
    def _valid_basis(cls, v: str) -> str:
        if v not in LAWFUL_BASIS_CODES:
            raise ValueError(
                f"lawful_basis must be one of {LAWFUL_BASIS_CODES} (migration 010: "
                "lawful_basis NOT NULL — an earmark never exists without one)"
            )
        return v


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    return pool


def _uuid_or_422(value: str) -> _uuid.UUID:
    try:
        return _uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(422, f"invalid uuid {value!r}") from exc


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _position(lon: Any, lat: Any) -> list[float]:
    return [round(float(lon), 6), round(float(lat), 6)]


# ────────────────────────────────────────────────────────────────────────────
# Earmark
# ────────────────────────────────────────────────────────────────────────────


@router.post("/{plate}/earmark")
async def earmark_vehicle(plate: str, req: EarmarkRequest, request: Request) -> dict:
    """Earmark a plate as watch|flagged with a lawful basis + retention limit.

    Creates the vehicle row on first sight (vehicles only — never persons).
    retention_until is computed server-side from NOW() + retention_days.
    """
    pool = _pool(request)
    now = datetime.now(timezone.utc)
    horizon = timedelta(days=req.retention_days)
    retention_until = now + horizon
    async with pool.acquire() as conn:
        vehicle = await store.upsert_vehicle_by_plate(
            conn, plate, "car", now
        )
        row = await store.insert_earmark(
            conn,
            vehicle["vehicle_id"],
            req.state,
            req.reason,
            req.lawful_basis,
            req.retention_days,
            req.earmarked_by,
            now,
            retention_until,
        )
    logger.info(
        "vehicles.earmarked",
        plate=plate,
        state=req.state,
        lawful_basis=req.lawful_basis,
        retention_days=req.retention_days,
    )
    return {
        "vehicle_id": str(vehicle["vehicle_id"]),
        "plate": plate,
        "earmark_id": str(row["earmark_id"]),
        "state": req.state,
        "reason": req.reason,
        "lawful_basis": req.lawful_basis,
        "earmarked_at": _iso(row["earmarked_at"]),
        "retention_until": _iso(row["retention_until"]),
        "retention_days": req.retention_days,
    }


@router.post("/{vehicle_id}/earmark/clear")
async def clear_earmark(vehicle_id: str, request: Request) -> dict:
    """Clear every ACTIVE earmark of a vehicle (human decision, audited)."""
    pool = _pool(request)
    vid = _uuid_or_422(vehicle_id)
    async with pool.acquire() as conn:
        row = await store.clear_active_earmarks(conn, vid)
    if row is None:
        raise HTTPException(404, f"no active earmark for vehicle {vehicle_id}")
    return {"vehicle_id": vehicle_id, "cleared_earmark_id": str(row["earmark_id"])}


# ────────────────────────────────────────────────────────────────────────────
# Watchlist listing (C1 shape)
# ────────────────────────────────────────────────────────────────────────────


@router.get("")
async def list_vehicles(
    request: Request,
    earmark: str | None = Query(default=None),
) -> dict:
    """Vehicles with their ACTIVE earmark + last sighting (C1 shape).

    ``earmark`` filters to watch|flagged; expiry is enforced in the query
    path (C6), so an expired earmark never surfaces here.
    """
    if earmark is not None and earmark not in EARMARK_STATES:
        raise HTTPException(422, f"earmark filter must be one of {EARMARK_STATES}")
    pool = _pool(request)
    async with pool.acquire() as conn:
        rows = await store.list_earmarked_vehicles(conn, earmark)
        ids = [r["vehicle_id"] for r in rows]
        last: dict = {}
        if ids:
            srows = await store.last_sightings_by_ids(conn, ids)
            for sr in srows:
                last[str(sr["vehicle_id"])] = {
                    "t": _iso(sr["t"]),
                    "position": _position(sr["lon"], sr["lat"]),
                    "source": sr["source"],
                    "camera_id": sr["camera_id"],
                    "matched_plate_hit": sr["matched_plate_hit"],
                }
    vehicles = []
    for r in rows:
        vehicles.append(
            {
                "vehicle_id": str(r["vehicle_id"]),
                "plate": r["plate"],
                "vehicle_class": r["vehicle_class"],
                "earmark_state": r["state"],
                "earmark_reason": r["reason"],
                "lawful_basis": r["lawful_basis"],
                "retention_until": _iso(r["retention_until"]),
                "last_sighting": last.get(str(r["vehicle_id"])),
                "source": SOURCE_EMULATED,  # badge: this build's only feed
            }
        )
    return {"vehicles": vehicles, "source": SOURCE_EMULATED}


# ────────────────────────────────────────────────────────────────────────────
# Trace + sightings
# ────────────────────────────────────────────────────────────────────────────


@router.get("/{vehicle_id}/sightings")
async def vehicle_sightings(
    vehicle_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    """Raw sightings for a vehicle, newest first, after retention reap."""
    pool = _pool(request)
    vid = _uuid_or_422(vehicle_id)
    now = datetime.now(timezone.utc)
    reap_cutoff = now - timedelta(days=SIGHTING_RETENTION_DAYS_DEFAULT)
    async with pool.acquire() as conn:
        await store.reap_expired_sightings(conn, reap_cutoff)
        rows = await store.fetch_sightings(conn, vid, limit)
    return {
        "vehicle_id": vehicle_id,
        "sightings": [
            {
                "sighting_id": str(r["sighting_id"]),
                "plate": r["plate"],
                "camera_id": r["camera_id"],
                "t": _iso(r["t"]),
                "position": _position(r["lon"], r["lat"]),
                "speed_kmh": r["speed_kmh"],
                "heading": r["heading"],
                "vehicle_class": r["vehicle_class"],
                "source": r["source"],
                "run_id": r["run_id"],
                "matched_plate_hit": r["matched_plate_hit"],
            }
            for r in rows
        ],
        "source": SOURCE_EMULATED,
    }


@router.get("/{vehicle_id}/trace")
async def vehicle_trace(
    vehicle_id: str,
    request: Request,
    window_sec: int = Query(default=3600, ge=60, le=86400),
) -> dict:
    """Timed trace for the canvas polyline layer (C1 shape).

    GeoJSON LineString (oldest -> newest) + timed positions, each carrying
    its source so the emulated badge travels with every point.
    """
    pool = _pool(request)
    vid = _uuid_or_422(vehicle_id)
    now = datetime.now(timezone.utc)
    reap_cutoff = now - timedelta(days=SIGHTING_RETENTION_DAYS_DEFAULT)
    trace_cutoff = now - timedelta(seconds=window_sec)
    async with pool.acquire() as conn:
        await store.reap_expired_sightings(conn, reap_cutoff)
        rows = await store.fetch_trace(conn, vid, trace_cutoff)
    if not rows:
        raise HTTPException(404, f"no sightings for vehicle {vehicle_id}")
    coords = [_position(r["lon"], r["lat"]) for r in rows]
    return {
        "vehicle_id": vehicle_id,
        "window_sec": window_sec,
        "geometry": {"type": "LineString", "coordinates": coords},
        "positions": [
            {
                "t": _iso(r["t"]),
                "position": _position(r["lon"], r["lat"]),
                "speed_kmh": r["speed_kmh"],
                "source": r["source"],  # 'emulated_anpr' in this build
                "camera_id": r["camera_id"],
            }
            for r in rows
        ],
        "source": SOURCE_EMULATED,
    }


# ────────────────────────────────────────────────────────────────────────────
# Ingest: persist AnprReads from any AnprSource (idempotent)
# ────────────────────────────────────────────────────────────────────────────


async def persist_read(conn: Any, read) -> str | None:
    """Persist one AnprRead (vehicles/sources.AnprRead) as a sighting row.

    Coerces the deterministic read_id to UUID and serializes the point as
    GeoJSON BEFORE binding — store.persist_read binds every value
    positionally.  Returns the sighting_id, or None on idempotent replay.
    """
    vehicle = await store.upsert_vehicle_by_plate(
        conn, read.plate, read.vehicle_class, read.t
    )
    read_uuid = _uuid.UUID(read.read_id)
    point_json = json.dumps(
        {"type": "Point", "coordinates": [read.lon, read.lat]}
    )
    return await store.persist_read(conn, read, vehicle["vehicle_id"],
                                    point_json, read_uuid)


__all__ = [
    "router",
    "EarmarkRequest",
    "LAWFUL_BASIS_CODES",
    "EARMARK_STATES",
    "SIGHTING_RETENTION_DAYS_DEFAULT",
    "persist_read",
]
