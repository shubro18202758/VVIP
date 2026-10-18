"""corridor-store seam — event rows, planned routes, and the runs ledger.

Every statement is inline, single-line, and parameter-bound (psycopg ``%s``
placeholders — the identical discipline as scripts/migrate.py:165); no SQL
is ever assembled with f-strings, format, or concatenation.  Geometries
cross the boundary as GeoJSON strings bound as parameters and are parsed
server-side by ST_GeomFromGeoJSON.  Movement identity is deterministic
(uuid5 of use_case+seed) and creation is an idempotent upsert, so a replay
re-animates the SAME event row instead of forking one.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger(__name__)

__all__ = ["CorridorStore", "utcnow", "point_geojson", "line_geojson"]


def point_geojson(lon: float, lat: float) -> str:
    """GeoJSON Point as a parameter-safe JSON string."""
    return json.dumps({"type": "Point", "coordinates": [lon, lat]})


def line_geojson(points: list[tuple[float, float]]) -> str:
    """GeoJSON LineString as a parameter-safe JSON string."""
    return json.dumps({"type": "LineString", "coordinates": [[p[0], p[1]] for p in points]})


def _uuid(value: str) -> uuid.UUID:
    return uuid.UUID(value)


class CorridorStore:
    """Async wrapper over the corridor-store PostGIS pool."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool
        self._centroids: dict[int, tuple[float, float]] = {}

    # -- reference data ------------------------------------------------------

    async def segment_centroids(
        self, segment_ids: list[int]
    ) -> dict[int, tuple[float, float]]:
        missing = [s for s in segment_ids if s not in self._centroids]
        if missing:
            async with self._pool.connection() as conn:
                rows = await self._fetch_centroids(conn, missing)
            for r in rows:
                self._centroids[int(r[0])] = (float(r[1]), float(r[2]))
        return {s: self._centroids[s] for s in segment_ids if s in self._centroids}

    @staticmethod
    async def _fetch_centroids(
        conn: AsyncConnection, missing: list[int]
    ) -> list[tuple[Any, ...]]:
        async with conn.cursor() as cur:
            await cur.execute("SELECT segment_id, ST_X(ST_Centroid(geom)) AS lon, ST_Y(ST_Centroid(geom)) AS lat FROM corridor.road_segments WHERE segment_id = ANY(%s)", (missing,))  # noqa: E501
            return await cur.fetchall()

    # -- event rows (the cause the adaptive engine loop polls) ---------------

    async def upsert_movement(
        self,
        movement_id: str,
        vvip_class: str,
        planned_start: datetime,
        origin: tuple[float, float],
        destination: tuple[float, float],
    ) -> None:
        """Create-or-reanimate the convoy.movements row (engine_loop.py:40)."""
        async with self._pool.connection() as conn:
            await conn.execute("INSERT INTO convoy.movements (movement_id, vvip_class, status, origin_geom, destination_geom, planned_start) VALUES (%s, %s, 'planning', ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s) ON CONFLICT (movement_id) DO UPDATE SET status = 'planning', planned_start = EXCLUDED.planned_start, updated_at = NOW()", (_uuid(movement_id), vvip_class, point_geojson(origin[0], origin[1]), point_geojson(destination[0], destination[1]), planned_start))  # noqa: E501

    async def replace_route(
        self, movement_id: str, segment_ids: list[int], points: list[tuple[float, float]]
    ) -> None:
        """(Re)write the planned_routes spine for the movement (route_rank 1)."""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM convoy.planned_routes WHERE movement_id = %s", (_uuid(movement_id),))  # noqa: E501
                await conn.execute("INSERT INTO convoy.planned_routes (movement_id, route_rank, segment_ids, route_geom) VALUES (%s, 1, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))", (_uuid(movement_id), segment_ids, line_geojson(points)))  # noqa: E501

    async def set_movement_status(self, movement_id: str, status: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("UPDATE convoy.movements SET status = %s, updated_at = NOW() WHERE movement_id = %s", (status, _uuid(movement_id)))  # noqa: E501

    # -- runs ledger (emulator.emulation_runs — core's migration 009) -------

    async def insert_run(
        self,
        *,
        use_case_id: str,
        spec: dict,
        spec_hash: str,
        seed: int,
        t0_anchor: datetime,
        clock_scale: float,
        tape_json: str,
        is_replay: bool,
        replay_of_run_id: int | None,
    ) -> int:
        # core's 009 columns: use_case_id / replay lineage ride inside the
        # spec JSONB (the table has no dedicated columns for them).
        spec_doc = dict(spec)
        spec_doc["use_case_id"] = use_case_id
        spec_doc["is_replay"] = is_replay
        spec_doc["replay_of_run_id"] = replay_of_run_id
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("INSERT INTO emulator.emulation_runs (spec_hash, spec, seed, t0_anchor, clock_scale, status, tape) VALUES (%s, CAST(%s AS jsonb), %s, %s, %s, 'running', CAST(%s AS jsonb)) RETURNING run_id", (spec_hash, json.dumps(spec_doc, sort_keys=True), seed, t0_anchor, clock_scale, tape_json))  # noqa: E501
                row = await cur.fetchone()
        assert row is not None
        return int(row[0])

    async def finish_run(self, run_id: int, status: str, delivery: dict) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("UPDATE emulator.emulation_runs SET status = %s, ended_at = NOW(), spec = spec || CAST(%s AS jsonb) WHERE run_id = %s", (status, json.dumps({"delivery": delivery}, sort_keys=True), run_id))  # noqa: E501

    async def fetch_run(self, run_id: int) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT run_id, spec_hash, spec, seed, t0_anchor, clock_scale, status, tape, created_at, ended_at FROM emulator.emulation_runs WHERE run_id = %s", (run_id,))  # noqa: E501
                cols = [d.name for d in cur.description]
                row = await cur.fetchone()
        return dict(zip(cols, row)) if row else None

    async def fetch_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT run_id, spec_hash, spec, seed, t0_anchor, clock_scale, status, created_at, ended_at FROM emulator.emulation_runs ORDER BY run_id DESC LIMIT %s", (limit,))  # noqa: E501
                cols = [d.name for d in cur.description]
                rows = await cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]

    # -- generalized event rows (core's migration 007) -----------------------
    # events.corridor_events is the durable generalized cause record; the
    # legacy convoy.movements row (what today's running engine loop polls,
    # engine_loop.py:40) is written alongside it and soft-linked via
    # corridor_events.movement_id (007's no-FK rule).

    async def upsert_corridor_event(
        self,
        event_id: str,
        movement_id: str,
        event_kind: str,
        protection_class: str,
        crowd_class: str,
        title: str,
        planned_start: datetime,
        planned_end: datetime,
        metadata_doc: dict,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("INSERT INTO events.corridor_events (event_id, event_kind, protection_class, crowd_class, title, status, movement_id, planned_start, planned_end, metadata) VALUES (%s, %s, %s, %s, %s, 'planning', %s, %s, %s, CAST(%s AS jsonb)) ON CONFLICT (event_id) DO UPDATE SET status = 'planning', planned_start = EXCLUDED.planned_start, planned_end = EXCLUDED.planned_end, updated_at = NOW()", ( _uuid(event_id), event_kind, protection_class, crowd_class, title, _uuid(movement_id), planned_start, planned_end, json.dumps(metadata_doc, sort_keys=True)))  # noqa: E501

    async def replace_event_route(self, event_id: str, segment_ids: list[int]) -> None:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM events.event_routes WHERE event_id = %s", (_uuid(event_id),))  # noqa: E501
                await conn.execute("INSERT INTO events.event_routes (event_id, route_rank, label, segment_ids) VALUES (%s, 1, 'spine', %s)", (_uuid(event_id), segment_ids))  # noqa: E501

    async def replace_event_phase(
        self,
        event_id: str,
        label: str,
        window_start: datetime,
        window_end: datetime,
        closure_policy: dict,
    ) -> None:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM events.event_phases WHERE event_id = %s", (_uuid(event_id),))  # noqa: E501
                await conn.execute("INSERT INTO events.event_phases (event_id, phase_rank, label, window_start, window_end, closure_policy) VALUES (%s, 1, %s, %s, %s, CAST(%s AS jsonb))", (_uuid(event_id), label, window_start, window_end, json.dumps(closure_policy, sort_keys=True)))  # noqa: E501

    async def set_event_status(self, event_id: str, status: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("UPDATE events.corridor_events SET status = %s, updated_at = NOW() WHERE event_id = %s", (status, _uuid(event_id)))  # noqa: E501

    # -- watchlist plane (watchlist-anpr stream, migration 010) --------------
    # corridor.vehicles + corridor.vehicle_sightings — the anpr_read
    # delivery targets.  Sightings are idempotent on sighting_id (the
    # deterministic read uuid), so a replay never forks rows.  Every write
    # carries source='emulated_anpr' (the only value this build produces;
    # 'live_anpr' is reserved for a real vendor feed behind the oracle's
    # AnprSource port).

    async def upsert_vehicle(
        self, plate: str, vehicle_class: str, seen_at: datetime
    ) -> uuid.UUID:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("INSERT INTO corridor.vehicles (plate, vehicle_class, first_seen, last_seen) VALUES (%s, %s, %s, %s) ON CONFLICT (plate) DO UPDATE SET last_seen = EXCLUDED.last_seen, updated_at = NOW() RETURNING vehicle_id", (plate, vehicle_class, seen_at, seen_at))  # noqa: E501
                row = await cur.fetchone()
        assert row is not None
        return row[0]

    async def insert_sighting(
        self,
        *,
        sighting_id: str,
        vehicle_id: uuid.UUID,
        plate: str,
        camera_id: str,
        t: datetime,
        lon: float,
        lat: float,
        speed_kmh: float | None,
        heading: float | None,
        vehicle_class: str | None,
        run_id: int | None,
        matched_plate_hit: str | None,
    ) -> bool:
        """Idempotent sighting insert; True when a NEW row landed."""
        point = point_geojson(lon, lat)
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("INSERT INTO corridor.vehicle_sightings (sighting_id, vehicle_id, plate, camera_id, t, position, speed_kmh, heading, vehicle_class, source, run_id, matched_plate_hit) VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s, %s, %s, 'emulated_anpr', %s, %s) ON CONFLICT (sighting_id) DO NOTHING RETURNING sighting_id", (_uuid(sighting_id), vehicle_id, plate, camera_id, t, point, speed_kmh, heading, vehicle_class, run_id, matched_plate_hit))  # noqa: E501
                row = await cur.fetchone()
        return row is not None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
