"""corridor-store seam — event rows, planned routes, and the runs ledger.

Every statement is inline, single-line, and parameter-bound (psycopg ``%s``
placeholders — the identical discipline as scripts/migrate.py:165); no SQL
is ever assembled with f-strings, format, or concatenation.  Geometries
cross the boundary as GeoJSON strings bound as parameters and are parsed
server-side by ST_GeomFromGeoJSON.  Movement identity is deterministic
(uuid5 of use_case+seed+run) and creation is an idempotent upsert, so a
replay re-animates the SAME event row instead of forking one.

What this seam no longer does (the oracle owns it, through its HTTP seams):
* corridor-event STATUS changes after creation — 'active' at t0 and
  'completed' at the end go through POST /api/v1/events/{id}/status
  (contract C-F), so the status ledger and the dispatch auto-close run;
* ANPR sightings — reads go through POST /api/v1/vehicles/sightings
  (contract C-E), which computes hits and e-challan rules itself.
Creation stays a direct upsert because an emulator event's id is
deterministic (run-scoped uuid5); its creation row in
``events.event_status_log`` (migration 013) is written in the same
transaction, exactly as the oracle's own POST /api/v1/events does.

Read-only reference queries serve the cause model: segment geometry, the
registered camera sites (corridor.cameras, migration 012) with the limit
and signal program that govern each site — the same nearest-segment
(300 m) and nearest-programmed-junction (40 m) attribution the oracle's
e-challan engine uses — and the open dispatches that hand a patrol unit
over to the oracle's dispatch projection.

Geometry contract C-K: what the emulator ANIMATES (patrol paths,
:meth:`CorridorStore.segment_paths`) follows the road a map draws —
COALESCE(display_geom, geom), migration 015, probed so a pre-015 store
keeps the schematic line; what it ATTRIBUTES or anchors (per-segment
anchors, a movement's route line and end points, camera sites, junction
legs) keeps the routing ``geom`` the oracle attributes reads to.  Display
lines start and end on the routing endpoints, so chaining segments into
one path works the same either way.  An anchor lies ON its road: the
point halfway along the line, ST_LineInterpolatePoint(geom, 0.5) — the
anchor signal-ingress gives its feeds — never ST_Centroid, which on a
curved OpenStreetMap road (WP-20) can sit a kilometre off it.

The cached anchors and paths are keyed by a fingerprint of the network
(probed every _NETWORK_REPROBE_SEC): a network changed under a running
emulator — the WP-20 conversion of a live store, a re-seed — is re-read
within that window, and a restart always reads it afresh.
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger(__name__)

__all__ = [
    "BOOT_ID",
    "CorridorStore",
    "LEDGER_STATUSES",
    "SYSTEM_ACTOR",
    "line_geojson",
    "point_geojson",
    "runner_identity",
    "utcnow",
]

#: The named system actor every emulator lifecycle row carries (C-F).
SYSTEM_ACTOR = "event-emulator (system)"

#: Statuses the 009 CHECK on emulator.emulation_runs.status accepts.
LEDGER_STATUSES = ("running", "completed", "failed", "stopped")

#: Where a C-G run status the ledger CHECK does not (yet) accept is kept:
#: the column takes the nearest accepted value and spec.final_status holds
#: the true one (the API reports final_status first).  'interrupted' joins
#: the CHECK with migration 016_emulation_runs_interrupted.sql (wave 3 —
#: no earlier migration widens it); the re-probe below picks it up on a
#: running emulator.
_LEDGER_FALLBACK = {"interrupted": "stopped"}

#: A CHECK that refused a status is probed again after this long, so a
#: running process picks up a migration that widened it (no restart).
_LEDGER_REPROBE_SEC = 300.0

#: How long the display_geom column probe (migration 015) is trusted.
_DISPLAY_REPROBE_SEC = 300.0

#: How long the road network's fingerprint is trusted.  The cached
#: per-segment anchors and paths are keyed by it, so a network changed
#: under a running emulator is re-read within this window.
_NETWORK_REPROBE_SEC = 60.0

#: Statuses under which an event (or a dispatch's event) is still live.
_LIVE_EVENT_STATUSES = ["planning", "approved", "active"]

#: Dispatch statuses that hold a unit for its event.
_OPEN_DISPATCH_STATUSES = ["approved", "en_route", "on_scene"]


def point_geojson(lon: float, lat: float) -> str:
    """GeoJSON Point as a parameter-safe JSON string."""
    return json.dumps({"type": "Point", "coordinates": [lon, lat]})


def line_geojson(points: list[tuple[float, float]]) -> str:
    """GeoJSON LineString as a parameter-safe JSON string."""
    return json.dumps({"type": "LineString", "coordinates": [[p[0], p[1]] for p in points]})


def _uuid(value: str) -> uuid.UUID:
    return uuid.UUID(value)


def _json_or(value: Any, default: Any) -> Any:
    if isinstance(value, (bytes, str)):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


#: A fresh id per emulator process (see :func:`runner_identity`).
BOOT_ID = uuid.uuid4().hex


def runner_identity(port: int | None = None) -> dict[str, Any]:
    """Which process owns a run row: host, serving port and a per-process
    boot id (pid for diagnostics).

    Two live emulators can never serve the same host:port, so a 'running'
    row stamped with THIS host:port but another boot id was left by an
    earlier process at this address — a restart — and the reaper closes it
    at once.  Every other row is judged by its heartbeat alone.
    """
    doc: dict[str, Any] = {"host": socket.gethostname(), "pid": os.getpid(), "boot": BOOT_ID}
    if port is not None:
        doc["port"] = int(port)
    return doc


class CorridorStore:
    """Async wrapper over the corridor-store PostGIS pool."""

    def __init__(self, pool: AsyncConnectionPool, runner: dict[str, Any] | None = None) -> None:
        self._pool = pool
        self.runner: dict[str, Any] = dict(runner) if runner else runner_identity()
        self._anchors: dict[int, tuple[float, float]] = {}
        self._paths: dict[int, list[tuple[float, float]]] = {}
        #: the key the cached anchors / paths were read under: (display
        #: geometry present, network fingerprint) — see :meth:`network_key`
        self._cache_key: tuple[bool, str] | None = None
        #: (key, probed at monotonic) and when network_key last failed to probe
        self._network_probe: tuple[tuple[bool, str], float] | None = None
        self._network_failed_at = float("-inf")
        #: (column present, probed at monotonic)
        self._display_probe: tuple[bool, float] | None = None
        self._status_log: bool | None = None
        self._ledger_accepted: dict[str, tuple[bool, float]] = {}

    # -- health --------------------------------------------------------------

    async def probe(self, timeout: float = 2.0) -> bool:
        """A real round trip (SELECT 1), not object existence."""
        async with self._pool.connection(timeout=timeout) as conn:
            await conn.execute("SELECT 1")
        return True

    # -- reference data ------------------------------------------------------

    def _network_fresh(self) -> bool:
        probe = self._network_probe
        return probe is not None and time.monotonic() - probe[1] < _NETWORK_REPROBE_SEC

    async def _network(self, conn: AsyncConnection) -> tuple[bool, str]:
        """(display geometry present, network fingerprint), probed at most
        every _NETWORK_REPROBE_SEC.  A key that differs from the one the
        cached anchors and paths were read under drops both caches."""
        if self._network_fresh():
            assert self._network_probe is not None
            return self._network_probe[0]
        display = await self.display_geometry_supported(conn)
        async with conn.cursor() as cur:
            if display:
                await cur.execute("SELECT count(*), md5(string_agg(segment_id::text || ':' || md5(ST_AsBinary(geom)) || ':' || COALESCE(md5(ST_AsBinary(display_geom)), '-'), ',' ORDER BY segment_id)) FROM corridor.road_segments")  # noqa: E501
            else:
                await cur.execute("SELECT count(*), md5(string_agg(segment_id::text || ':' || md5(ST_AsBinary(geom)), ',' ORDER BY segment_id)) FROM corridor.road_segments")  # noqa: E501
            row = await cur.fetchone()
        key = (display, f"{row[0]}:{row[1]}" if row else "")
        self._network_probe = (key, time.monotonic())
        if key != self._cache_key:
            if self._cache_key is not None:
                logger.info("store.network_changed", segments=row[0] if row else None,
                            display_geometry=display)
            self._anchors.clear()
            self._paths.clear()
            self._cache_key = key
        return key

    async def network_key(self) -> tuple[bool, str] | None:
        """The key the cached geometry is read under (probed at most every
        _NETWORK_REPROBE_SEC).  A consumer that keeps geometry derived from
        :meth:`segment_paths` (the living city's patrol lines) re-reads it
        when the key changes.  Never raises: a store that cannot answer
        keeps the last key, and is asked again a window later."""
        now = time.monotonic()
        if self._network_fresh() or now - self._network_failed_at < _NETWORK_REPROBE_SEC:
            return self._cache_key
        try:
            async with self._pool.connection(timeout=2.0) as conn:
                return await self._network(conn)
        except Exception as exc:  # noqa: BLE001 - the cached geometry stands
            self._network_failed_at = now
            logger.warning("store.network_probe_failed", error=str(exc))
            return self._cache_key

    async def segment_midpoints(
        self, segment_ids: list[int]
    ) -> dict[int, tuple[float, float]]:
        """Each segment's on-road anchor (lon, lat): the point halfway ALONG
        its routing line, ST_LineInterpolatePoint(geom, 0.5) — the anchor
        signal-ingress gives its feeds (scripts/gen_ingress_segments.py).
        Per-segment probe rows sit here, ON the road they are attributed
        to.  Cached per segment under the network key."""
        if self._network_fresh() and all(s in self._anchors for s in segment_ids):
            return {s: self._anchors[s] for s in segment_ids}
        async with self._pool.connection() as conn:
            await self._network(conn)
            missing = [s for s in segment_ids if s not in self._anchors]
            if missing:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT segment_id, ST_X(ST_LineInterpolatePoint(geom, 0.5)) AS lon, ST_Y(ST_LineInterpolatePoint(geom, 0.5)) AS lat FROM corridor.road_segments WHERE segment_id = ANY(%s)", (missing,))  # noqa: E501
                    rows = await cur.fetchall()
                for sid, lon, lat in rows:
                    self._anchors[int(sid)] = (float(lon), float(lat))
        return {s: self._anchors[s] for s in segment_ids if s in self._anchors}

    async def route_geometry(self, segment_ids: list[int]) -> dict[str, Any]:
        """What orienting a route in its direction of travel takes
        (tape.orient_route): each segment's ROUTING line (geom — what the
        planner routes and the oracle attributes on, C-K) and one-way flag,
        and for every consecutive pair the junction point(s)
        segment_adjacency records the movement through.

        ``{"lines": {sid: [(lon, lat), ...]}, "oneway": {sid: bool},
        "vias": [[(lon, lat), ...] per consecutive pair]}``
        """
        ids = [int(s) for s in segment_ids]
        via_rows: list[tuple[Any, ...]] = []
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT segment_id, COALESCE(oneway, FALSE), ST_AsGeoJSON(geom) FROM corridor.road_segments WHERE segment_id = ANY(%s)", (ids,))  # noqa: E501
                rows = await cur.fetchall()
                if len(ids) > 1:
                    await cur.execute("SELECT p.ord, ST_X(j.geom), ST_Y(j.geom) FROM unnest(%s::bigint[], %s::bigint[]) WITH ORDINALITY AS p(from_id, to_id, ord) JOIN corridor.segment_adjacency a ON a.from_segment_id = p.from_id AND a.to_segment_id = p.to_id JOIN corridor.junctions j ON j.junction_id = a.via_junction_id ORDER BY p.ord, a.via_junction_id", (ids[:-1], ids[1:]))  # noqa: E501
                    via_rows = await cur.fetchall()
        lines: dict[int, list[tuple[float, float]]] = {}
        oneway: dict[int, bool] = {}
        for sid, one, gj in rows:
            coords = _json_or(gj, {}).get("coordinates") or []
            lines[int(sid)] = [(float(c[0]), float(c[1])) for c in coords]
            oneway[int(sid)] = bool(one)
        vias: list[list[tuple[float, float]]] = [[] for _ in ids[1:]]
        for ordinal, lon, lat in via_rows:
            vias[int(ordinal) - 1].append((float(lon), float(lat)))
        return {"lines": lines, "oneway": oneway, "vias": vias}

    async def display_geometry_supported(self, conn: AsyncConnection) -> bool:
        """Whether corridor.road_segments carries migration 015's
        display_geom (probed, trusted for _DISPLAY_REPROBE_SEC)."""
        now = time.monotonic()
        if self._display_probe is None or now - self._display_probe[1] >= _DISPLAY_REPROBE_SEC:
            async with conn.cursor() as cur:
                await cur.execute("SELECT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = to_regclass('corridor.road_segments') AND a.attname = 'display_geom' AND NOT a.attisdropped)")  # noqa: E501
                row = await cur.fetchone()
            self._display_probe = (bool(row and row[0]), now)
        return self._display_probe[0]

    async def segment_paths(
        self, segment_ids: list[int]
    ) -> dict[int, list[tuple[float, float]]]:
        """Each segment's DRAWN polyline (lon, lat): COALESCE(display_geom,
        geom) — patrols ride the real road where migration 015 holds an
        OpenStreetMap trace, the schematic line elsewhere and on a pre-015
        store (contract C-K).  Endpoints equal the schematic ones, so
        chain_polyline joins either.  Cached per segment under the network
        key: dropped when the store's basis (015 landing) or the network
        itself (the WP-20 conversion) changes under a running emulator."""
        if self._network_fresh() and all(s in self._paths for s in segment_ids):
            return {s: self._paths[s] for s in segment_ids}
        async with self._pool.connection() as conn:
            display, _fingerprint = await self._network(conn)
            missing = [s for s in segment_ids if s not in self._paths]
            if missing:
                async with conn.cursor() as cur:
                    if display:
                        await cur.execute("SELECT segment_id, ST_AsGeoJSON(COALESCE(display_geom, geom)) FROM corridor.road_segments WHERE segment_id = ANY(%s)", (missing,))  # noqa: E501
                    else:
                        await cur.execute("SELECT segment_id, ST_AsGeoJSON(geom) FROM corridor.road_segments WHERE segment_id = ANY(%s)", (missing,))  # noqa: E501
                    rows = await cur.fetchall()
                for sid, gj in rows:
                    coords = _json_or(gj, {}).get("coordinates") or []
                    self._paths[int(sid)] = [(float(c[0]), float(c[1])) for c in coords]
        return {s: self._paths[s] for s in segment_ids if s in self._paths}

    async def camera_sites(self) -> list[dict[str, Any]]:
        """Registered ANPR sites with what governs a read placed on them.

        Per site: the segment the oracle attributes a read to (nearest road
        segment within 300 m) with its limit, direction and one-way flag,
        and the nearest PROGRAMMED junction within 40 m (the e-challan
        engine's stop-line radius) — None when no program governs the site.
        Empty when the registry (migration 012) is absent.
        """
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT to_regclass('corridor.cameras') IS NOT NULL")
                row = await cur.fetchone()
                if not row or not row[0]:
                    return []
                await cur.execute("SELECT c.camera_id, c.kind, ST_X(c.geom) AS lon, ST_Y(c.geom) AS lat, c.junction_id, c.segment_id AS registered_segment_id, c.heading_deg, seg.segment_id, seg.speed_limit_kmh, seg.oneway, seg.road_name, seg.bearing_deg, sig.junction_id AS signal_junction_id FROM corridor.cameras c LEFT JOIN LATERAL (SELECT rs.segment_id, rs.speed_limit_kmh, rs.oneway, rs.road_name, degrees(ST_Azimuth(ST_StartPoint(rs.geom), ST_EndPoint(rs.geom))) AS bearing_deg FROM corridor.road_segments rs WHERE ST_DWithin(rs.geom::geography, c.geom::geography, 300) ORDER BY rs.geom <-> c.geom LIMIT 1) seg ON TRUE LEFT JOIN LATERAL (SELECT j.junction_id FROM corridor.junctions j JOIN corridor.signal_programs p ON p.junction_id = j.junction_id WHERE ST_DWithin(j.geom::geography, c.geom::geography, 40) ORDER BY j.geom <-> c.geom LIMIT 1) sig ON TRUE WHERE c.kind = 'anpr' AND c.status = 'active' ORDER BY c.camera_id")  # noqa: E501
                cols = [d.name for d in cur.description]
                rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(zip(cols, r))
            out.append({
                "camera_id": str(d["camera_id"]),
                "lon": float(d["lon"]),
                "lat": float(d["lat"]),
                "junction_id": int(d["junction_id"]) if d["junction_id"] is not None else None,
                "registered_segment_id": (
                    int(d["registered_segment_id"])
                    if d["registered_segment_id"] is not None else None
                ),
                "heading_deg": float(d["heading_deg"]) if d["heading_deg"] is not None else None,
                "segment_id": int(d["segment_id"]) if d["segment_id"] is not None else None,
                "speed_limit_kmh": (
                    float(d["speed_limit_kmh"]) if d["speed_limit_kmh"] is not None else None
                ),
                "oneway": bool(d["oneway"]),
                "road_name": d["road_name"],
                "bearing_deg": float(d["bearing_deg"]) if d["bearing_deg"] is not None else None,
                "signal_junction_id": (
                    int(d["signal_junction_id"]) if d["signal_junction_id"] is not None else None
                ),
            })
        return out

    async def signal_programs(self) -> dict[int, dict[str, Any]]:
        """Every junction's CURRENT program (the city writer may change them)."""
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT junction_id, cycle_sec, offset_sec, phases FROM corridor.signal_programs")  # noqa: E501
                rows = await cur.fetchall()
        return {
            int(jid): {
                "cycle_sec": int(cycle),
                "offset_sec": int(offset or 0),
                "phases": _json_or(phases, []),
            }
            for jid, cycle, offset, phases in rows
        }

    async def junction_legs(self, junction_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        """Each junction's legs with the bearing AWAY from it — the same
        25 m endpoint snap and first-vertex azimuth the e-challan engine
        uses to map a heading onto a program movement."""
        if not junction_ids:
            return {}
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT j.junction_id, s.segment_id, s.road_class, degrees(CASE WHEN ST_Distance(ST_StartPoint(s.geom)::geography, j.geom::geography) <= ST_Distance(ST_EndPoint(s.geom)::geography, j.geom::geography) THEN ST_Azimuth(ST_PointN(s.geom, 1)::geography, ST_PointN(s.geom, 2)::geography) ELSE ST_Azimuth(ST_PointN(s.geom, ST_NPoints(s.geom))::geography, ST_PointN(s.geom, ST_NPoints(s.geom) - 1)::geography) END) AS bearing_deg FROM corridor.junctions j JOIN corridor.road_segments s ON s.geom && ST_Expand(j.geom, 0.001) AND (ST_DWithin(ST_StartPoint(s.geom)::geography, j.geom::geography, 25) OR ST_DWithin(ST_EndPoint(s.geom)::geography, j.geom::geography, 25)) WHERE j.junction_id = ANY(%s) ORDER BY j.junction_id, s.segment_id", (list(junction_ids),))  # noqa: E501
                rows = await cur.fetchall()
        legs: dict[int, list[dict[str, Any]]] = {}
        for jid, sid, road_class, bearing in rows:
            if bearing is None:
                continue
            legs.setdefault(int(jid), []).append({
                "segment_id": int(sid),
                "road_class": str(road_class),
                "bearing_deg": round(float(bearing), 2),
            })
        return legs

    async def segment_junctions(self) -> dict[int, list[tuple[int, bool]]]:
        """segment -> [(junction_id, signal_control)] over segment_adjacency."""
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT DISTINCT a.from_segment_id, a.via_junction_id, COALESCE(j.signal_control, FALSE) FROM corridor.segment_adjacency a JOIN corridor.junctions j ON j.junction_id = a.via_junction_id ORDER BY 1, 2")  # noqa: E501
                rows = await cur.fetchall()
        out: dict[int, list[tuple[int, bool]]] = {}
        for sid, jid, signal in rows:
            out.setdefault(int(sid), []).append((int(jid), bool(signal)))
        return out

    async def segment_junction_ids(self) -> dict[int, set[int]]:
        """segment -> every junction it meets in segment_adjacency, either
        direction (a one-way segment meets its start junction only as the
        TO side of a turn)."""
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT from_segment_id, via_junction_id FROM corridor.segment_adjacency UNION SELECT to_segment_id, via_junction_id FROM corridor.segment_adjacency")  # noqa: E501
                rows = await cur.fetchall()
        out: dict[int, set[int]] = {}
        for sid, jid in rows:
            out.setdefault(int(sid), set()).add(int(jid))
        return out

    async def dispatched_units(self, unit_ids: list[str]) -> set[str]:
        """Units holding an approved / en-route / on-scene dispatch of a LIVE
        event — the oracle's dispatch projection drives those, not a beat."""
        if not unit_ids:
            return set()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT to_regclass('corridor.dispatches') IS NOT NULL")
                row = await cur.fetchone()
                if not row or not row[0]:
                    return set()
                await cur.execute("SELECT DISTINCT d.unit_id FROM corridor.dispatches d JOIN events.corridor_events e ON e.event_id = d.event_id WHERE d.unit_id = ANY(%s) AND d.status = ANY(%s) AND e.status = ANY(%s)", (list(unit_ids), _OPEN_DISPATCH_STATUSES, _LIVE_EVENT_STATUSES))  # noqa: E501
                rows = await cur.fetchall()
        return {str(r[0]) for r in rows}

    # -- event rows (the cause the adaptive engine loop polls) ---------------

    async def upsert_movement(
        self,
        movement_id: str,
        vvip_class: str,
        planned_start: datetime,
        origin: tuple[float, float],
        destination: tuple[float, float],
    ) -> None:
        """Create-or-reanimate the convoy.movements row (engine_loop.py:40).

        The row is the emulator's own EMULATED movement: a re-animation (a
        replay re-posting its tape) moves its origin and destination too —
        to where the route line drawn on TODAY's roads starts and ends —
        never keeping the ends an earlier network gave it."""
        async with self._pool.connection() as conn:
            await conn.execute("INSERT INTO convoy.movements (movement_id, vvip_class, status, origin_geom, destination_geom, planned_start) VALUES (%s, %s, 'planning', ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s) ON CONFLICT (movement_id) DO UPDATE SET status = 'planning', origin_geom = EXCLUDED.origin_geom, destination_geom = EXCLUDED.destination_geom, planned_start = EXCLUDED.planned_start, updated_at = NOW()", (_uuid(movement_id), vvip_class, point_geojson(origin[0], origin[1]), point_geojson(destination[0], destination[1]), planned_start))  # noqa: E501

    async def route_problems(self, segment_ids: list[int]) -> list[str]:
        """Why ``segment_ids`` is not a legal path on the store's CURRENT
        road network — [] when it is one: every segment exists and every
        consecutive pair is an edge of corridor.road_graph (migration 012:
        the movements the router drives, a one-way only its own way).  A
        tape recorded on an earlier network — before the WP-20 conversion
        put the roads on OpenStreetMap — can fail this: replaying it would
        re-animate its event on a line that bridges the gaps with straight
        jumps off the roads (runner.replay_refusal)."""
        ids = [int(s) for s in segment_ids]
        if not ids:
            return ["no route recorded"]
        edges: set[tuple[int, int]] = set()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT segment_id FROM corridor.road_segments WHERE segment_id = ANY(%s)", (ids,))  # noqa: E501
                known = {int(r[0]) for r in await cur.fetchall()}
                if len(ids) > 1:
                    await cur.execute("SELECT p.from_id, p.to_id FROM unnest(%s::bigint[], %s::bigint[]) AS p(from_id, to_id) WHERE EXISTS (SELECT 1 FROM corridor.road_graph g WHERE g.source = p.from_id AND g.target = p.to_id)", (ids[:-1], ids[1:]))  # noqa: E501
                    edges = {(int(a), int(b)) for a, b in await cur.fetchall()}
        problems: list[str] = []
        unknown = [s for s in ids if s not in known]
        if unknown:
            problems.append(f"unknown segments {unknown}")
        problems += [f"{a} -> {b} is not a legal move" for a, b in zip(ids, ids[1:])
                     if a in known and b in known and (a, b) not in edges]
        return problems

    async def replace_route(
        self, movement_id: str, segment_ids: list[int], points: list[tuple[float, float]]
    ) -> None:
        """(Re)write the planned_routes spine for the movement (route_rank 1)."""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM convoy.planned_routes WHERE movement_id = %s", (_uuid(movement_id),))  # noqa: E501
                await conn.execute("INSERT INTO convoy.planned_routes (movement_id, route_rank, segment_ids, route_geom) VALUES (%s, 1, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))", (_uuid(movement_id), segment_ids, line_geojson(points)))  # noqa: E501

    async def set_movement_status(self, movement_id: str, status: str) -> None:
        """The legacy convoy.movements row is the emulator's own (no C-F)."""
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
        spec_doc.setdefault("runner", self.runner)
        spec_doc.setdefault("heartbeat_at", utcnow().isoformat())
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("INSERT INTO emulator.emulation_runs (spec_hash, spec, seed, t0_anchor, clock_scale, status, tape) VALUES (%s, CAST(%s AS jsonb), %s, %s, %s, 'running', CAST(%s AS jsonb)) RETURNING run_id", (spec_hash, json.dumps(spec_doc, sort_keys=True), seed, t0_anchor, clock_scale, tape_json))  # noqa: E501
                row = await cur.fetchone()
        assert row is not None
        return int(row[0])

    async def update_run_tape(self, run_id: int, tape_json: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("UPDATE emulator.emulation_runs SET tape = CAST(%s AS jsonb) WHERE run_id = %s", (tape_json, run_id))  # noqa: E501

    async def heartbeat_run(self, run_id: int, doc: dict) -> None:
        """Merge liveness + progress into the running row's spec."""
        patch = dict(doc)
        patch["heartbeat_at"] = utcnow().isoformat()
        async with self._pool.connection() as conn:
            await conn.execute("UPDATE emulator.emulation_runs SET spec = spec || CAST(%s AS jsonb) WHERE run_id = %s AND status = 'running'", (json.dumps(patch, sort_keys=True, default=str), run_id))  # noqa: E501

    async def _ledger_accepts(self, status: str) -> bool:
        """Does the status CHECK accept ``status``?  A yes is kept for the
        process; a no is probed again after _LEDGER_REPROBE_SEC (a migration
        may widen the CHECK under a running emulator)."""
        if status in LEDGER_STATUSES:
            return True
        known = self._ledger_accepted.get(status)
        now = time.monotonic()
        if known is None or (not known[0] and now - known[1] >= _LEDGER_REPROBE_SEC):
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c WHERE c.conrelid = 'emulator.emulation_runs'::regclass AND c.contype = 'c' AND pg_get_constraintdef(c.oid) LIKE %s", ("%status%",))  # noqa: E501
                    rows = await cur.fetchall()
            quoted = "'" + status + "'"
            known = (any(quoted in str(r[0]) for r in rows), now)
            self._ledger_accepted[status] = known
        return known[0]

    async def finish_run(self, run_id: int, status: str, doc: dict) -> str:
        """Close a run row: final status + ended_at + the summary ``doc``
        (delivery, capped frame_log, twin summary …) merged into spec.

        Returns the value written to the status column — ``status`` itself,
        or its nearest CHECK-accepted value when the ledger predates it
        (spec.final_status always carries the true C-G status).
        """
        column = status
        if not await self._ledger_accepts(status):
            column = _LEDGER_FALLBACK.get(status, "failed")
        patch = dict(doc)
        patch["final_status"] = status
        async with self._pool.connection() as conn:
            await conn.execute("UPDATE emulator.emulation_runs SET status = %s, ended_at = NOW(), spec = spec || CAST(%s AS jsonb) WHERE run_id = %s", (column, json.dumps(patch, sort_keys=True, default=str), run_id))  # noqa: E501
        return column

    async def fetch_run(self, run_id: int) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT run_id, spec_hash, spec, seed, t0_anchor, clock_scale, status, tape, created_at, ended_at FROM emulator.emulation_runs WHERE run_id = %s", (run_id,))  # noqa: E501
                cols = [d.name for d in cur.description]
                row = await cur.fetchone()
        return dict(zip(cols, row)) if row else None

    async def fetch_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recent runs, newest first — without the capped frame log (the
        detail route serves it) so a 10 s poll stays small."""
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT run_id, spec_hash, spec - 'frame_log' AS spec, seed, t0_anchor, clock_scale, status, created_at, ended_at FROM emulator.emulation_runs ORDER BY run_id DESC LIMIT %s", (limit,))  # noqa: E501
                cols = [d.name for d in cur.description]
                rows = await cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]

    async def running_runs(self) -> list[dict[str, Any]]:
        """Every row still marked running, with its owner's last heartbeat."""
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT run_id, spec->>'use_case_id' AS use_case_id, spec->>'heartbeat_at' AS heartbeat_at, spec->'progress' AS progress, spec->>'mode' AS mode, spec->'runner' AS runner, created_at FROM emulator.emulation_runs WHERE status = 'running' ORDER BY run_id")  # noqa: E501
                cols = [d.name for d in cur.description]
                rows = await cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]

    async def finished_run_ids(self, run_ids: list[int]) -> set[int]:
        """Which of ``run_ids`` have left 'running' (or no longer exist)."""
        if not run_ids:
            return set()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT run_id FROM emulator.emulation_runs WHERE run_id = ANY(%s) AND status = 'running'", (list(run_ids),))  # noqa: E501
                still = {int(r[0]) for r in await cur.fetchall()}
        return {r for r in run_ids if r not in still}

    # -- generalized event rows (core's migration 007 + 013 ledger) ----------
    # events.corridor_events is the durable generalized cause record; the
    # legacy convoy.movements row (what today's running engine loop polls,
    # engine_loop.py:40) is written alongside it and soft-linked via
    # corridor_events.movement_id (007's no-FK rule).

    async def _has_status_log(self, conn: AsyncConnection) -> bool:
        if self._status_log is None:
            async with conn.cursor() as cur:
                await cur.execute("SELECT to_regclass('events.event_status_log') IS NOT NULL")
                row = await cur.fetchone()
            self._status_log = bool(row and row[0])
        return self._status_log

    async def upsert_corridor_event(
        self,
        event_id: str,
        movement_id: str | None,
        event_kind: str,
        protection_class: str,
        crowd_class: str,
        title: str,
        planned_start: datetime,
        planned_end: datetime,
        metadata_doc: dict,
        description: str = "",
        note: str = "event created",
    ) -> str | None:
        """Create (or re-animate) the event in 'planning' + its ledger row.

        Returns the status the row had BEFORE (None for a new event).  A
        re-animation (a replay re-posting the original tape) resets the row
        to 'planning' with fresh windows and clears actual_start/end; the
        ledger records it as its own row (from the old status, by the
        emulator, with ``note``) — never silently.
        """
        meta = json.dumps(metadata_doc, sort_keys=True)
        eid = _uuid(event_id)
        mid = _uuid(movement_id) if movement_id else None
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute("SELECT status FROM events.corridor_events WHERE event_id = %s FOR UPDATE", (eid,))  # noqa: E501
                    prior = await cur.fetchone()
                await conn.execute("INSERT INTO events.corridor_events (event_id, event_kind, protection_class, crowd_class, title, description, status, movement_id, planned_start, planned_end, metadata) VALUES (%s, %s, %s, %s, %s, %s, 'planning', %s, %s, %s, CAST(%s AS jsonb)) ON CONFLICT (event_id) DO UPDATE SET status = 'planning', title = EXCLUDED.title, description = EXCLUDED.description, planned_start = EXCLUDED.planned_start, planned_end = EXCLUDED.planned_end, actual_start = NULL, actual_end = NULL, metadata = EXCLUDED.metadata, updated_at = NOW()", (eid, event_kind, protection_class, crowd_class, title, description, mid, planned_start, planned_end, meta))  # noqa: E501
                if await self._has_status_log(conn):
                    await conn.execute("INSERT INTO events.event_status_log (event_id, from_status, to_status, actor, note) VALUES (%s, %s, 'planning', %s, %s)", (eid, prior[0] if prior else None, SYSTEM_ACTOR, note))  # noqa: E501
        return str(prior[0]) if prior else None

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

    async def live_emulator_events(self) -> list[dict[str, Any]]:
        """Emulator-created events that are still live (janitor input)."""
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT event_id, status, metadata->>'run_id' AS run_id, metadata->>'mode' AS mode, planned_end, updated_at FROM events.corridor_events WHERE metadata->>'source' = 'event-emulator' AND status = ANY(%s) ORDER BY updated_at", (_LIVE_EVENT_STATUSES,))  # noqa: E501
                cols = [d.name for d in cur.description]
                rows = await cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
