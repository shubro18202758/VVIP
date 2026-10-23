"""Camera layer — GET /api/v1/cameras (contract C-C, mentor directive R2).

Where cameras come from (decided per request, no restart needed):

    registry   ``corridor.cameras`` (WP-03 migration 012) when the table
               exists — registered ANPR/CCTV sites with kind, source,
               junction and heading.  Cameras that delivered reads in the
               last 24 h but are not registered are appended (flagged
               ``registered: false``) so a read never comes from nowhere.
    derived    until 012 lands: one camera per ``camera_id`` seen in
               ``corridor.vehicle_sightings``, located at the centroid of
               its reads (cameras are fixed; reads sit on the camera).

Every camera carries its read counters for the last 15 minutes
(``reads_15m``, ``hits_15m``) and ``last_read_t`` — one lateral query
over the per-camera recency index (migration 013), fresh on every
request.  The camera BASE list (identity, kind, location) is static
between registry edits, so it is rebuilt at most every
``CAMERA_BASE_TTL_SEC`` seconds: a poll never re-aggregates the whole
sightings table to re-derive positions that cannot have moved.

Ethics rail (C6): LOCATIONS AND COUNTS ONLY.  The registry row is read
through a key WHITELIST, so a plate, frame or image column can never
reach the wire even if a later migration adds one.  Reads are counted,
never listed, on this surface.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import structlog

from traffic_oracle.city.common import (
    as_dict,
    finite,
    iso_z,
    relation_exists,
    valid_lonlat,
)

logger = structlog.get_logger(__name__)

CAMERA_REGISTRY = "corridor.cameras"

#: Read-counter window (contract C-C: reads_15m / hits_15m).
READS_WINDOW_SEC = 900

#: Lifetime of the cached camera base list (identity + location only —
#: counters are never cached).
CAMERA_BASE_TTL_SEC = 30.0
_BASE_CACHE: dict[bool, tuple[float, list[dict]]] = {}


def reset_camera_cache() -> None:
    _BASE_CACHE.clear()

CAMERA_KINDS = ("anpr", "cctv")
CAMERA_SOURCES = ("emulated", "osm", "registered")

#: Registry columns are read through an explicit WHITELIST — the dict built
#: in ``_registry_camera`` (id, kind, source, label, location, junction,
#: segment, heading, status).  Any other column (a frame, a plate cache)
#: never leaves this module (C6).
_REGISTRY_LABEL_KEYS = ("label", "name")
_GEOM_KEYS = ("geom", "position", "location", "point")

# ---------------------------------------------------------------------------
# Read SQL (module constants — parameter-bound, read-only)
# ---------------------------------------------------------------------------

#: Registry rows as JSON documents: tolerant of the exact 012 column set
#: (geometry arrives as GeoJSON through PostGIS' jsonb cast).
_SQL_CAMERA_REGISTRY = "SELECT to_jsonb(c) AS doc FROM corridor.cameras c ORDER BY c.camera_id LIMIT 2000"  # noqa: E501

#: Pre-012 fallback: one camera per camera_id at the centroid of its reads.
_SQL_CAMERAS_DERIVED = "SELECT s.camera_id, ST_X(ST_Centroid(ST_Collect(s.position))) AS lon, ST_Y(ST_Centroid(ST_Collect(s.position))) AS lat, bool_or(s.source = 'emulated_anpr') AS any_emulated FROM corridor.vehicle_sightings s GROUP BY s.camera_id ORDER BY s.camera_id LIMIT 2000"  # noqa: E501

#: With a registry: cameras that read in the last 24 h but are not in it.
_SQL_CAMERAS_UNREGISTERED = "SELECT s.camera_id, ST_X(ST_Centroid(ST_Collect(s.position))) AS lon, ST_Y(ST_Centroid(ST_Collect(s.position))) AS lat, bool_or(s.source = 'emulated_anpr') AS any_emulated FROM corridor.vehicle_sightings s WHERE s.t > NOW() - interval '24 hours' AND NOT (s.camera_id = ANY($1::text[])) GROUP BY s.camera_id ORDER BY s.camera_id LIMIT 500"  # noqa: E501

#: Per-camera counters (index-friendly laterals; future-dated reads are
#: not counted as having happened).
_SQL_CAMERA_STATS = "SELECT c.cid AS camera_id, w.reads_15m, w.hits_15m, l.last_read_t FROM unnest($1::text[]) AS c(cid) LEFT JOIN LATERAL (SELECT count(*) AS reads_15m, count(*) FILTER (WHERE s.matched_plate_hit IS NOT NULL) AS hits_15m FROM corridor.vehicle_sightings s WHERE s.camera_id = c.cid AND s.t > NOW() - interval '15 minutes' AND s.t <= NOW()) w ON TRUE LEFT JOIN LATERAL (SELECT max(s.t) AS last_read_t FROM corridor.vehicle_sightings s WHERE s.camera_id = c.cid AND s.t <= NOW()) l ON TRUE"  # noqa: E501


def _point_from_doc(doc: Mapping[str, Any]) -> tuple[float, float] | None:
    """First GeoJSON Point in a registry document (geom preferred)."""
    keys = list(_GEOM_KEYS) + [k for k in doc if k not in _GEOM_KEYS]
    for key in keys:
        value = doc.get(key)
        if isinstance(value, Mapping) and value.get("type") == "Point":
            coords = value.get("coordinates")
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                point = valid_lonlat(coords[0], coords[1])
                if point is not None:
                    return point
    return None


def _int_or_none(value: Any) -> int | None:
    f = finite(value)
    return int(f) if f is not None else None


def _registry_camera(doc: Mapping[str, Any]) -> dict | None:
    cam_id = doc.get("camera_id")
    point = _point_from_doc(doc)
    if not isinstance(cam_id, str) or not cam_id or point is None:
        return None
    kind = doc.get("kind") if doc.get("kind") in CAMERA_KINDS else "anpr"
    source = doc.get("source") if doc.get("source") in CAMERA_SOURCES else "registered"
    label = next(
        (doc[k] for k in _REGISTRY_LABEL_KEYS if isinstance(doc.get(k), str) and doc[k]),
        cam_id,
    )
    return {
        "camera_id": cam_id,
        "label": label,
        "lon": round(point[0], 6),
        "lat": round(point[1], 6),
        "kind": kind,
        "source": source,
        "junction_id": _int_or_none(doc.get("junction_id")),
        "segment_id": _int_or_none(doc.get("segment_id")),
        "heading_deg": finite(doc.get("heading_deg")),
        "status": str(doc.get("status") or "registered"),
        "registered": True,
    }


def _derived_camera(row: Mapping[str, Any], *, registry_present: bool) -> dict | None:
    point = valid_lonlat(row["lon"], row["lat"])
    if point is None:
        return None
    return {
        "camera_id": row["camera_id"],
        "label": row["camera_id"],
        "lon": round(point[0], 6),
        "lat": round(point[1], 6),
        "kind": "anpr",  # every derived camera is known only by its ANPR reads
        # Location provenance: derived from emulated reads → 'emulated'; a
        # camera feeding live reads is a registered ANPR feed by definition.
        "source": "emulated" if row["any_emulated"] else "registered",
        "junction_id": None,
        "segment_id": None,
        "heading_deg": None,
        "status": "unregistered" if registry_present else "derived_from_reads",
        "registered": False,
    }


async def _camera_base(conn: Any, registry_present: bool) -> list[dict]:
    """Identity + location of every camera (TTL-cached; fresh copies)."""
    clock = time.monotonic()
    hit = _BASE_CACHE.get(registry_present)
    if hit is not None and hit[0] > clock:
        return [dict(c) for c in hit[1]]
    cameras: list[dict] = []
    if registry_present:
        for r in await conn.fetch(_SQL_CAMERA_REGISTRY):
            cam = _registry_camera(as_dict(r["doc"]))
            if cam is not None:
                cameras.append(cam)
        known = [c["camera_id"] for c in cameras]
        for r in await conn.fetch(_SQL_CAMERAS_UNREGISTERED, known):
            cam = _derived_camera(r, registry_present=True)
            if cam is not None:
                cameras.append(cam)
    else:
        for r in await conn.fetch(_SQL_CAMERAS_DERIVED):
            cam = _derived_camera(r, registry_present=False)
            if cam is not None:
                cameras.append(cam)
    _BASE_CACHE[registry_present] = (clock + CAMERA_BASE_TTL_SEC, [dict(c) for c in cameras])
    return cameras


async def load_cameras(conn: Any, now: datetime) -> tuple[list[dict], dict]:
    """Every camera (registry or derived) with its 15-minute counters.

    Returns ``(cameras, meta)``; ``meta.registry`` names the source used so
    a consumer can badge 'derived' cameras until the registry lands.
    """
    _ = now  # counters are computed against the DB clock (NOW())
    registry_present = await relation_exists(conn, CAMERA_REGISTRY)
    cameras = await _camera_base(conn, registry_present)

    ids = [c["camera_id"] for c in cameras]
    stats: dict[str, Any] = {}
    if ids:
        for r in await conn.fetch(_SQL_CAMERA_STATS, ids):
            stats[r["camera_id"]] = r
    for cam in cameras:
        s = stats.get(cam["camera_id"])
        cam["reads_15m"] = int(s["reads_15m"] or 0) if s is not None else 0
        cam["hits_15m"] = int(s["hits_15m"] or 0) if s is not None else 0
        last = s["last_read_t"] if s is not None else None
        cam["last_read_t"] = iso_z(last)
        cam["_last_read_dt"] = last
    meta = {
        "registry": CAMERA_REGISTRY if registry_present else "derived_from_sightings",
        "window_sec": READS_WINDOW_SEC,
        "unregistered": sum(1 for c in cameras if not c["registered"]),
    }
    return cameras, meta


def public_camera(cam: Mapping[str, Any]) -> dict:
    """The wire row (drops private helper keys)."""
    return {k: v for k, v in cam.items() if not k.startswith("_")}


__all__ = [
    "CAMERA_BASE_TTL_SEC",
    "CAMERA_KINDS",
    "CAMERA_REGISTRY",
    "CAMERA_SOURCES",
    "READS_WINDOW_SEC",
    "load_cameras",
    "public_camera",
    "reset_camera_cache",
]
