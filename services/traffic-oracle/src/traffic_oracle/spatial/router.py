"""Spatial router — road segments with how each line was drawn (WP-17, WP-20).

Three reads, mounted by ``__main__`` through a guarded include ahead of the
inline Spatial handlers (contract C-S — those stay as the fallback when this
package is absent):

    GET /api/v1/spatial/segments?min_lon&min_lat&max_lon&max_lat
        bbox read (snake_case; the deck's segment feed, the brain's
        query_segments_in_bbox tool)
    GET /api/v1/spatial/segments/{segment_id}
        one segment (the brain's GPS emulation walks this geometry)
    GET /api/traffic/segments?min_lon&min_lat&max_lon&max_lat
        bbox read in the deck's camelCase shape (CorridorMap)

Contract C-K (display geometry):
  * ``geometry`` is COALESCE(display_geom, geom): what a map DRAWS follows
    the real road wherever migration 015's ``display_geom`` holds a
    validated OpenStreetMap trace, else the routing line.  Its endpoints
    and orientation equal the routing ones (015 CHECK), so
    endpoint-chaining consumers keep working unchanged.
  * additive fields, never a changed meaning: ``geometry_basis`` says how
    the line drawn was made —
        'osm'          the ROUTING geom itself is the OpenStreetMap road
                       (migration 020 ``geom_source`` 'osm': the path along
                       the OSM ways between the segment's real junction
                       nodes, WP-20) and no display line overrides it;
        'osm_display'  a 015 display line follows the road while routing
                       stays on a hand-placed line;
        'schematic'    the hand-placed seed line, drawn as is —
    ``display_source`` ('osm_way_stitch' | 'osrm_route' | null: only an
    'osm_display' line has one) and — only when the basis is 'osm_display'
    — the ``schematic_geometry`` routing actually uses.
  * ``length_m`` (single segment) stays the ROUTING geodesic length (geom):
    the cost length every planner uses.  ``display_length_m`` is the length
    of the geometry drawn — the same number for an 'osm' segment.
  * the bbox filter matches either geometry, so a segment whose display
    line bends into the box is not lost.  It matches ENVELOPES (&&), and
    the real network leans on that: the brain's planner snaps a trip end
    to the nearest row its small box returns, and a POI off the 49 roads
    (Narendra Modi Stadium, 553 m from Motera Stadium Road) is found only
    because the road's envelope covers it.
  * the ``display_geom`` and ``geom_source`` columns are probed (cached
    300 s): a store without migration 015 answers every segment
    'schematic', one without 020 never 'osm' — never a 500.

Read-only: every statement is a SELECT.  Ethics rails (C6): road geometry
only — no plate, camera or person field.
"""

from __future__ import annotations

import json
import time
from typing import Any

import asyncpg
import structlog
from fastapi import APIRouter, HTTPException, Query, Request

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["spatial"])

#: geometry_basis vocabulary (C-K).
BASIS_OSM = "osm"
BASIS_OSM_DISPLAY = "osm_display"
BASIS_SCHEMATIC = "schematic"

#: The geom_source value of a segment whose routing geom IS the OSM road (020).
GEOM_SOURCE_OSM = "osm"

#: How long a column probe is trusted (a store migrating at runtime is
#: picked up within this).
DISPLAY_PROBE_TTL_SEC = 300.0

_SQL_DISPLAY_COLUMN = (
    "SELECT EXISTS (SELECT 1 FROM pg_attribute a "
    "WHERE a.attrelid = to_regclass('corridor.road_segments') "
    "AND a.attname = 'display_geom' AND NOT a.attisdropped) AS present"
)

_SQL_NETWORK_COLUMN = (
    "SELECT EXISTS (SELECT 1 FROM pg_attribute a "
    "WHERE a.attrelid = to_regclass('corridor.road_segments') "
    "AND a.attname = 'geom_source' AND NOT a.attisdropped) AS present"
)

# migration 015 present: draw display_geom where validated, else geom.
# LIMIT 2000 is the inline handlers' bound (the city network is 49 rows).
_SQL_BBOX_DISPLAY = """
SELECT segment_id, osm_way_id, road_name, road_class, lanes, speed_limit_kmh, oneway,
       ST_AsGeoJSON(COALESCE(display_geom, geom))::json AS geojson,
       CASE WHEN display_geom IS NULL THEN NULL ELSE ST_AsGeoJSON(geom)::json END
           AS schematic_geojson,
       display_geom IS NOT NULL AS has_display,
       display_geom_source
FROM corridor.road_segments
WHERE geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)
   OR display_geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)
ORDER BY segment_id
LIMIT 2000
"""

# migrations 015 + 020: the same, plus how geom itself was drawn
_SQL_BBOX_NETWORK = """
SELECT segment_id, osm_way_id, road_name, road_class, lanes, speed_limit_kmh, oneway,
       ST_AsGeoJSON(COALESCE(display_geom, geom))::json AS geojson,
       CASE WHEN display_geom IS NULL THEN NULL ELSE ST_AsGeoJSON(geom)::json END
           AS schematic_geojson,
       display_geom IS NOT NULL AS has_display,
       display_geom_source,
       geom_source
FROM corridor.road_segments
WHERE geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)
   OR display_geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)
ORDER BY segment_id
LIMIT 2000
"""

# pre-015 store: the schematic line is all there is
_SQL_BBOX_SCHEMATIC = """
SELECT segment_id, osm_way_id, road_name, road_class, lanes, speed_limit_kmh, oneway,
       ST_AsGeoJSON(geom)::json AS geojson,
       NULL::json AS schematic_geojson,
       FALSE AS has_display,
       NULL::text AS display_geom_source
FROM corridor.road_segments
WHERE geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)
ORDER BY segment_id
LIMIT 2000
"""

_SQL_ONE_DISPLAY = """
SELECT segment_id, osm_way_id, road_name, road_class, lanes, speed_limit_kmh, oneway,
       ST_AsGeoJSON(COALESCE(display_geom, geom))::json AS geojson,
       CASE WHEN display_geom IS NULL THEN NULL ELSE ST_AsGeoJSON(geom)::json END
           AS schematic_geojson,
       display_geom IS NOT NULL AS has_display,
       display_geom_source,
       ST_Length(geom::geography) AS length_m,
       ST_Length(COALESCE(display_geom, geom)::geography) AS display_length_m
FROM corridor.road_segments
WHERE segment_id = $1
"""

_SQL_ONE_NETWORK = """
SELECT segment_id, osm_way_id, road_name, road_class, lanes, speed_limit_kmh, oneway,
       ST_AsGeoJSON(COALESCE(display_geom, geom))::json AS geojson,
       CASE WHEN display_geom IS NULL THEN NULL ELSE ST_AsGeoJSON(geom)::json END
           AS schematic_geojson,
       display_geom IS NOT NULL AS has_display,
       display_geom_source,
       geom_source,
       ST_Length(geom::geography) AS length_m,
       ST_Length(COALESCE(display_geom, geom)::geography) AS display_length_m
FROM corridor.road_segments
WHERE segment_id = $1
"""

_SQL_ONE_SCHEMATIC = """
SELECT segment_id, osm_way_id, road_name, road_class, lanes, speed_limit_kmh, oneway,
       ST_AsGeoJSON(geom)::json AS geojson,
       NULL::json AS schematic_geojson,
       FALSE AS has_display,
       NULL::text AS display_geom_source,
       ST_Length(geom::geography) AS length_m,
       ST_Length(geom::geography) AS display_length_m
FROM corridor.road_segments
WHERE segment_id = $1
"""

#: (monotonic expiry, column present) — one entry each, process-wide.
_DISPLAY_PROBE: list[tuple[float, bool]] = []
_NETWORK_PROBE: list[tuple[float, bool]] = []


async def _probe(conn: Any, cache: list[tuple[float, bool]], sql: str) -> bool:
    now = time.monotonic()
    if cache and cache[0][0] > now:
        return cache[0][1]
    present = bool(await conn.fetchval(sql))
    cache[:] = [(now + DISPLAY_PROBE_TTL_SEC, present)]
    return present


async def display_supported(conn: Any) -> bool:
    """Whether corridor.road_segments carries migration 015's display_geom."""
    return await _probe(conn, _DISPLAY_PROBE, _SQL_DISPLAY_COLUMN)


async def network_supported(conn: Any) -> bool:
    """Whether corridor.road_segments carries migration 020's geom_source."""
    return await _probe(conn, _NETWORK_PROBE, _SQL_NETWORK_COLUMN)


async def _segment_sql(conn: Any, network_sql: str, display_sql: str, schematic_sql: str) -> str:
    if not await display_supported(conn):
        return schematic_sql
    return network_sql if await network_supported(conn) else display_sql


def reset_display_probe() -> None:
    """Forget the cached column probes (tests / operator tooling)."""
    _DISPLAY_PROBE.clear()
    _NETWORK_PROBE.clear()


def _geojson(value: Any) -> dict | None:
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else value


def _basis(row: Any) -> str:
    """'osm_display' (a 015 line overrides geom) > 'osm' (geom IS the OSM
    road, 020) > 'schematic'."""
    if row["has_display"]:
        return BASIS_OSM_DISPLAY
    if row.get("geom_source") == GEOM_SOURCE_OSM:
        return BASIS_OSM
    return BASIS_SCHEMATIC


def segment_doc(row: Any) -> dict:
    """One segment in the /api/v1/spatial shape (C-K additive fields)."""
    basis = _basis(row)
    doc = {
        "segment_id": row["segment_id"],
        "osm_way_id": row["osm_way_id"],
        "road_name": row["road_name"],
        "road_class": row["road_class"],
        "lanes": row["lanes"],
        "speed_limit_kmh": row["speed_limit_kmh"],
        "oneway": row["oneway"],
        "geometry": _geojson(row["geojson"]),
        "geometry_basis": basis,
        "display_source": row["display_geom_source"] if basis == BASIS_OSM_DISPLAY else None,
    }
    if basis == BASIS_OSM_DISPLAY:
        doc["schematic_geometry"] = _geojson(row["schematic_geojson"])
    return doc


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def _internal_error(event: str) -> HTTPException:
    """Log the active exception and answer a generic 500 (no driver text)."""
    logger.exception(event)
    return HTTPException(status_code=500, detail=f"{event}: internal error")


async def _bbox_rows(request: Request, bbox: tuple[float, float, float, float]) -> list:
    async with _pool(request).acquire() as conn:
        sql = await _segment_sql(conn, _SQL_BBOX_NETWORK, _SQL_BBOX_DISPLAY, _SQL_BBOX_SCHEMATIC)
        return await conn.fetch(sql, *bbox)


@router.get("/api/v1/spatial/segments")
async def spatial_segments(
    request: Request,
    min_lon: float = Query(...),
    min_lat: float = Query(...),
    max_lon: float = Query(...),
    max_lat: float = Query(...),
) -> dict:
    """Segments whose schematic OR display line meets the bbox."""
    try:
        rows = await _bbox_rows(request, (min_lon, min_lat, max_lon, max_lat))
        return {"segments": [segment_doc(r) for r in rows]}
    except Exception as exc:
        raise _internal_error("spatial_segments.failed") from exc


@router.get("/api/v1/spatial/segments/{segment_id}")
async def segment_detail(request: Request, segment_id: int) -> dict:
    """One segment: drawn geometry + the routing length."""
    try:
        async with _pool(request).acquire() as conn:
            sql = await _segment_sql(conn, _SQL_ONE_NETWORK, _SQL_ONE_DISPLAY, _SQL_ONE_SCHEMATIC)
            row = await conn.fetchrow(sql, segment_id)
        if not row:
            raise HTTPException(status_code=404, detail="Segment not found")
        doc = segment_doc(row)
        doc["length_m"] = round(row["length_m"], 1)
        doc["display_length_m"] = round(row["display_length_m"], 1)
        return doc
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("segment_detail.failed") from exc


@router.get("/api/traffic/segments")
async def traffic_segments(
    request: Request,
    min_lon: float = Query(...),
    min_lat: float = Query(...),
    max_lon: float = Query(...),
    max_lat: float = Query(...),
) -> list[dict]:
    """Road segments in bbox — consumed by command-deck CorridorMap."""
    try:
        rows = await _bbox_rows(request, (min_lon, min_lat, max_lon, max_lat))
        out = []
        for r in rows:
            basis = _basis(r)
            geometry = _geojson(r["geojson"]) or {}
            doc = {
                "segmentId": r["segment_id"],
                "osmWayId": r["osm_way_id"],
                "roadName": r["road_name"],
                "roadClass": r["road_class"],
                "lanes": r["lanes"],
                "speedLimitKmh": r["speed_limit_kmh"],
                "oneway": r["oneway"],
                "geometry": geometry.get("coordinates", []),
                "geometryBasis": basis,
                "displaySource": r["display_geom_source"] if basis == BASIS_OSM_DISPLAY else None,
            }
            if basis == BASIS_OSM_DISPLAY:
                doc["schematicGeometry"] = (_geojson(r["schematic_geojson"]) or {}).get(
                    "coordinates", []
                )
            out.append(doc)
        return out
    except Exception as exc:
        raise _internal_error("traffic_segments.failed") from exc


__all__ = [
    "BASIS_OSM",
    "BASIS_OSM_DISPLAY",
    "BASIS_SCHEMATIC",
    "DISPLAY_PROBE_TTL_SEC",
    "GEOM_SOURCE_OSM",
    "display_supported",
    "network_supported",
    "reset_display_probe",
    "router",
    "segment_doc",
]
