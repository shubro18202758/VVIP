"""Entity dossier — GET /api/v1/city/entity/{kind}/{id} (contract C-C).

``{entity, related: [{relation, entity}], audit: FeedItem[]}`` for one
URN: the entity itself (any lifecycle state — a completed event or a
cancelled dispatch still has a dossier), its 1-hop neighbours through
keys that already exist, and its merged audit trail (city/feed.py,
scoped in SQL to the entity's own keys).

    kind            related (relation)                      1-hop key
    event           vehicle / pursued_vehicle,               corridor_events.metadata
                    authorizing_recommendation               .vehicle_id / .recommendation_id
                    dispatch, unit                           dispatches.event_id
                    route_segment                            event_routes.segment_ids (rank 1)
                    signal_junction                          signal_program_history.event_id
                    convoy                                   Valkey convoy:position:{movement}
    vehicle         sighting, seen_by_camera                 vehicle_sightings.vehicle_id /
                                                             camera_id
                    recommendation                           enforcement_recommendations.vehicle_id
                    pursuit / event                          corridor_events.metadata.vehicle_id
    sighting        vehicle, camera, recommendation          sighting keys
    recommendation  vehicle, evidence_sighting, camera,      sighting_id / vehicle_id,
                    segment, pursuit                         evidence.segment_id, metadata
    camera          junction, recent_hit                     cameras.junction_id, hits at camera
    junction        adjacent_segment, dispatch_waypoint,     segment_adjacency, dispatches
                    camera, signal_event                     .waypoint_junction, cameras, history
    unit            dispatch, assigned_event                 dispatches.unit_id / event_id
    dispatch        event, unit, waypoint_junction           dispatch keys
    convoy          event                                    event_id / movement_id
    anomaly         segment                                  anomaly_log.segment_id
    segment         adjacent_junction, spine_of, anomaly     adjacency, spines, anomaly_log

Privacy rail (C6): an individual read surfaces only under
``SQL_LAWFUL_READ`` (city/common.py) — its vehicle holds an ACTIVE
earmark, or the read is the evidence of an enforcement recommendation.
So a vehicle WITHOUT an active earmark (never earmarked, cleared, or
lapsed) gets no sighting trail anywhere in its dossier — not in
``related``, not in ``audit`` (the feed's ANPR source applies the same
predicate) — only the reads that are evidence of its recommendations; a
camera dossier lists lawful ANPR HITS, never every passing read; and a
sighting dossier for a withheld read is a 404.

Every id that reaches a ``::uuid`` bind is canonical: C-B escort frames
may carry any string as ``event_id`` (contract), and a non-uuid one is
never followed (``props.event_ref`` keeps it visible, unlinked).

Provenance (C-Q / C-K): a vehicle whose reads are withheld is badged from
an AGGREGATE over its reads ('emulated' when every read is emulated) —
never a default 'live', never an individual read; a segment says how its
geometry was drawn (``geometry_basis`` 'osm_display' when migration 015's
display geometry exists for it, 'osm' when its routing geometry IS the
OpenStreetMap road — migration 020's ``geom_source`` — else 'schematic',
the spatial router's vocabulary) and carries its latest
hot-cache observation (``latest_observation``: speed, congestion, data
quality, age — the live plane, plus the emulator's row labelled when one
exists); anomaly and segment anchors sit on the drawn road.  An event's
dossier reads its CURRENT spines (018) and carries its route_history.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

import structlog

from traffic_oracle.city.cameras import load_cameras
from traffic_oracle.city.common import (
    EPOCH,
    SQL_LAWFUL_READ,
    age_sec,
    as_dict,
    as_uuid,
    city_entity,
    display_geom_supported,
    finite,
    json_doc,
    parse_ts,
    routes_supersede_supported,
    scan_mget,
    subject_key,
    urn,
)
from traffic_oracle.city.entities import (
    _SQL_RECENT_SIGHTINGS,
    _SQL_SUBJECT_BANDS,
    CONVOY_POSITION_PREFIX,
    MAX_CONVOY_KEYS,
    anomaly_entity,
    camera_entity,
    convoy_entity,
    dispatch_entity,
    event_entity,
    history_doc,
    junction_entity,
    load_event_spines,
    recommendation_entity,
    sighting_entity,
    spine_doc,
    unit_entity,
    vehicle_entity,
)
from traffic_oracle.city.feed import FeedScope, collect_feed
from traffic_oracle.enforcement.engine import SQL_STOLEN_EARMARK
from traffic_oracle.ingest.cache_writer import emulated_latest_key, live_latest_key
from traffic_oracle.units.agencies import agency_label
from traffic_oracle.units.router import collect_units

logger = structlog.get_logger(__name__)

#: Bounds on the related lists (a dossier is a page, not an export).
MAX_RELATED_SIGHTINGS = 20
MAX_RELATED = 64

# ---------------------------------------------------------------------------
# Read SQL (module constants — parameter-bound, read-only)
# ---------------------------------------------------------------------------

_SQL_DOSSIER_EVENTS = "SELECT e.event_id, e.event_kind, e.protection_class, e.crowd_class, e.title, e.status, e.movement_id, e.planned_start, e.planned_end, e.actual_start, e.actual_end, e.metadata, e.created_at, e.updated_at, ST_X(ST_Centroid(e.zone_geom)) AS zone_lon, ST_Y(ST_Centroid(e.zone_geom)) AS zone_lat FROM events.corridor_events e WHERE e.event_id = ANY($1::uuid[]) ORDER BY e.created_at DESC"  # noqa: E501

#: Events citing a vehicle or recommendation in their metadata (pursuits).
_SQL_DOSSIER_EVENTS_CITING = "SELECT e.event_id FROM events.corridor_events e WHERE e.metadata->>'vehicle_id' = ANY($1::text[]) OR e.metadata->>'recommendation_id' = ANY($2::text[]) ORDER BY e.created_at DESC LIMIT 20"  # noqa: E501

#: Events linked to a convoy movement (soft movement_id link or the id itself).
_SQL_DOSSIER_EVENTS_FOR_MOVEMENT = "SELECT e.event_id FROM events.corridor_events e WHERE e.movement_id = $1::uuid OR e.event_id = $1::uuid ORDER BY e.created_at DESC LIMIT 20"  # noqa: E501

_SQL_DOSSIER_VEHICLES = "SELECT v.vehicle_id, v.plate, v.vehicle_class, v.first_seen, v.last_seen, em.state, em.reason, em.lawful_basis, em.retention_until, em.earmarked_at, em.earmarked_by FROM corridor.vehicles v LEFT JOIN LATERAL (SELECT ve.state, ve.reason, ve.lawful_basis, ve.retention_until, ve.earmarked_at, ve.earmarked_by FROM corridor.vehicle_earmarks ve WHERE ve.vehicle_id = v.vehicle_id AND ve.cleared_at IS NULL AND ve.retention_until > NOW() ORDER BY ve.earmarked_at DESC LIMIT 1) em ON TRUE WHERE v.vehicle_id = ANY($1::uuid[])"  # noqa: E501

#: Individual reads by id — only those lawful to surface (C6 predicate):
#: evidence reads always pass, any other read only under an active earmark.
_SQL_DOSSIER_SIGHTINGS = "SELECT s.vehicle_id, s.sighting_id, s.plate, s.camera_id, s.t, ST_X(s.position) AS lon, ST_Y(s.position) AS lat, s.speed_kmh, s.heading, s.vehicle_class, s.source, s.run_id, s.matched_plate_hit FROM corridor.vehicle_sightings s WHERE s.sighting_id = ANY($1::uuid[]) AND " + SQL_LAWFUL_READ + " ORDER BY s.t DESC"  # noqa: E501

#: Lawful ANPR HITS at one camera (never every passing read — C6).
_SQL_DOSSIER_CAMERA_HITS = "SELECT s.vehicle_id, s.sighting_id, s.plate, s.camera_id, s.t, ST_X(s.position) AS lon, ST_Y(s.position) AS lat, s.speed_kmh, s.heading, s.vehicle_class, s.source, s.run_id, s.matched_plate_hit FROM corridor.vehicle_sightings s WHERE s.camera_id = $1 AND s.matched_plate_hit IS NOT NULL AND s.t <= NOW() AND " + SQL_LAWFUL_READ + " ORDER BY s.t DESC LIMIT 10"  # noqa: E501

_SQL_DOSSIER_RECOMMENDATIONS = "SELECT r.recommendation_id, r.sighting_id, r.vehicle_id, r.rule_id, r.recommendation, r.evidence, r.status, r.decided_by, r.decided_at, r.created_at, s.camera_id, s.t AS sighting_t, s.source AS sighting_source, s.plate, ST_X(s.position) AS lon, ST_Y(s.position) AS lat, " + SQL_STOLEN_EARMARK + " AS stolen_vehicle FROM events.enforcement_recommendations r LEFT JOIN corridor.vehicle_sightings s ON s.sighting_id = r.sighting_id WHERE r.recommendation_id = ANY($1::uuid[]) OR r.vehicle_id = ANY($2::uuid[]) OR r.sighting_id = ANY($3::uuid[]) ORDER BY r.created_at DESC LIMIT 50"  # noqa: E501

#: The provenance of a vehicle's reads as ONE aggregate (no read, time,
#: camera or position): every read emulated → 'emulated'.  It badges a
#: vehicle whose individual reads are withheld (C6) instead of a default.
_SQL_VEHICLE_READS_PROVENANCE = "SELECT s.vehicle_id, bool_and(s.source LIKE 'emulated%') AS all_emulated FROM corridor.vehicle_sightings s WHERE s.vehicle_id = ANY($1::uuid[]) GROUP BY s.vehicle_id"  # noqa: E501

_SQL_DOSSIER_JUNCTIONS = "SELECT j.junction_id, j.junction_type, j.signal_control, to_jsonb(j) - 'geom' AS doc, ST_X(j.geom) AS lon, ST_Y(j.geom) AS lat, sp.cycle_sec, sp.phases, sp.offset_sec, sp.source AS program_source, sp.updated_at AS program_updated_at FROM corridor.junctions j LEFT JOIN corridor.signal_programs sp ON sp.junction_id = j.junction_id WHERE j.junction_id = ANY($1::bigint[]) ORDER BY j.junction_id"  # noqa: E501

_SQL_DOSSIER_DISPATCHES = "SELECT d.dispatch_id, d.event_id, d.agency, d.unit_id, d.waypoint_junction, d.status, d.requested_by, d.decided_by, d.approved_at, d.created_at, d.updated_at, ST_X(j.geom) AS lon, ST_Y(j.geom) AS lat, ev.title AS event_title, ev.metadata AS event_metadata, ev.status AS event_status FROM corridor.dispatches d JOIN events.corridor_events ev ON ev.event_id = d.event_id LEFT JOIN corridor.junctions j ON j.junction_id = d.waypoint_junction WHERE d.dispatch_id = ANY($1::uuid[]) OR d.event_id = ANY($2::uuid[]) OR d.unit_id = ANY($3::text[]) OR d.waypoint_junction = ANY($4::bigint[]) ORDER BY d.created_at DESC LIMIT 50"  # noqa: E501

_SQL_DOSSIER_SEGMENTS = "SELECT rs.segment_id, rs.road_name, rs.road_class, rs.lanes, rs.speed_limit_kmh, rs.oneway, ST_X(ST_PointOnSurface(rs.geom)) AS lon, ST_Y(ST_PointOnSurface(rs.geom)) AS lat, ST_Length(rs.geom::geography) AS length_m FROM corridor.road_segments rs WHERE rs.segment_id = ANY($1::bigint[]) ORDER BY rs.segment_id"  # noqa: E501

#: Migration 015 (C-K, probed): the anchor sits ON the drawn road and the
#: row says which geometry that is.  length_m stays the routing length.
#: geom_source (migration 020: 'osm' when the routing geom IS the
#: OpenStreetMap road) is read through to_jsonb, so a store without 020
#: answers NULL instead of failing the statement.
_SQL_DOSSIER_SEGMENTS_DISPLAY = "SELECT rs.segment_id, rs.road_name, rs.road_class, rs.lanes, rs.speed_limit_kmh, rs.oneway, ST_X(ST_PointOnSurface(COALESCE(rs.display_geom, rs.geom))) AS lon, ST_Y(ST_PointOnSurface(COALESCE(rs.display_geom, rs.geom))) AS lat, ST_Length(rs.geom::geography) AS length_m, (rs.display_geom IS NOT NULL) AS has_display_geom, (to_jsonb(rs) ->> 'geom_source') AS geom_source FROM corridor.road_segments rs WHERE rs.segment_id = ANY($1::bigint[]) ORDER BY rs.segment_id"  # noqa: E501

_SQL_SEGMENT_JUNCTIONS = "SELECT DISTINCT sa.via_junction_id AS junction_id FROM corridor.segment_adjacency sa WHERE sa.from_segment_id = $1 OR sa.to_segment_id = $1"  # noqa: E501

_SQL_JUNCTION_SEGMENTS = "SELECT sa.from_segment_id AS segment_id FROM corridor.segment_adjacency sa WHERE sa.via_junction_id = $1 UNION SELECT sa.to_segment_id FROM corridor.segment_adjacency sa WHERE sa.via_junction_id = $1"  # noqa: E501

#: Live events whose spines run over a segment.
_SQL_SEGMENT_EVENTS = "SELECT DISTINCT r.event_id FROM events.event_routes r JOIN events.corridor_events e ON e.event_id = r.event_id WHERE $1::bigint = ANY(r.segment_ids) AND e.status IN ('planning', 'approved', 'active')"  # noqa: E501

#: Migration 018 (probed): only CURRENT spines — a segment an event was
#: re-routed off is no longer 'spine_of' it.
_SQL_SEGMENT_EVENTS_CURRENT = "SELECT DISTINCT r.event_id FROM events.event_routes r JOIN events.corridor_events e ON e.event_id = r.event_id WHERE $1::bigint = ANY(r.segment_ids) AND r.superseded_at IS NULL AND e.status IN ('planning', 'approved', 'active')"  # noqa: E501

#: Events a junction's signal plan served (C7 ledger).
_SQL_JUNCTION_SIGNAL_EVENTS = "SELECT DISTINCT h.event_id FROM corridor.signal_program_history h WHERE h.junction_id = $1 AND h.event_id IS NOT NULL"  # noqa: E501

#: Junctions whose signal history references an event.
_SQL_EVENT_SIGNAL_JUNCTIONS = "SELECT DISTINCT h.junction_id FROM corridor.signal_program_history h WHERE h.event_id = $1"  # noqa: E501

_SQL_DOSSIER_ANOMALY = "SELECT a.anomaly_id, a.segment_id, a.timestamp_utc, a.anomaly_type, a.severity, a.details, a.flagged_at, rs.road_name, ST_X(ST_PointOnSurface(rs.geom)) AS lon, ST_Y(ST_PointOnSurface(rs.geom)) AS lat, NULL::bigint AS n_15m FROM traffic.anomaly_log a LEFT JOIN corridor.road_segments rs ON rs.segment_id = a.segment_id WHERE a.anomaly_id = $1"  # noqa: E501

_SQL_SEGMENT_ANOMALIES = "SELECT a.anomaly_id, a.segment_id, a.timestamp_utc, a.anomaly_type, a.severity, a.details, a.flagged_at, rs.road_name, ST_X(ST_PointOnSurface(rs.geom)) AS lon, ST_Y(ST_PointOnSurface(rs.geom)) AS lat, NULL::bigint AS n_15m FROM traffic.anomaly_log a JOIN corridor.road_segments rs ON rs.segment_id = a.segment_id WHERE a.segment_id = $1 AND a.timestamp_utc > NOW() - interval '15 minutes' ORDER BY a.timestamp_utc DESC LIMIT 10"  # noqa: E501

#: Migration 015 (C-K, probed): anomaly anchors on the drawn road.
_DISPLAY_POINT = "ST_PointOnSurface(COALESCE(rs.display_geom, rs.geom))"
_SQL_DOSSIER_ANOMALY_DISPLAY = _SQL_DOSSIER_ANOMALY.replace(
    "ST_PointOnSurface(rs.geom)", _DISPLAY_POINT
)
_SQL_SEGMENT_ANOMALIES_DISPLAY = _SQL_SEGMENT_ANOMALIES.replace(
    "ST_PointOnSurface(rs.geom)", _DISPLAY_POINT
)

#: How much of a segment's latest hot-cache row a dossier shows.
_LATEST_OBSERVATION_KEYS = ("speed_kmh", "congestion_idx", "data_quality", "source")


def latest_observation(raw: Any, now: datetime) -> dict | None:
    """One hot-cache row (``traffic:latest:{sid}`` JSON) → the dossier's
    ``{speed_kmh, congestion_idx, data_quality, source, t, age_sec}``;
    None when absent or unreadable."""
    doc = json_doc(raw)
    if not doc:
        return None
    ts_ms = finite(doc.get("timestamp_ms"))
    t = parse_ts(ts_ms / 1000.0) if ts_ms is not None else None
    out = {k: doc.get(k) for k in _LATEST_OBSERVATION_KEYS}
    out["speed_kmh"] = finite(out["speed_kmh"])
    out["congestion_idx"] = finite(out["congestion_idx"])
    out["t"] = t.isoformat() if t is not None else None
    out["age_sec"] = age_sec(t, now)
    return out


def segment_entity(
    row: Mapping[str, Any],
    now: datetime,
    *,
    observation: dict | None = None,
    emulated_observation: dict | None = None,
) -> dict:
    """One road segment.  ``geometry_basis`` says how it is drawn (C-K,
    the spatial router's vocabulary): 'osm_display' when 015's display
    geometry exists for it, 'osm' when its routing geometry IS the
    OpenStreetMap road (020 ``geom_source`` 'osm'), else 'schematic' (a
    hand-placed seed line)."""
    road = row["road_name"]
    return city_entity(
        "segment",
        int(row["segment_id"]),
        road or f"segment {int(row['segment_id'])}",
        row["lon"],
        row["lat"],
        now=now,
        state=row["road_class"],
        source="live",
        props={
            "road_name": road,
            "road_class": row["road_class"],
            "lanes": row["lanes"],
            "speed_limit_kmh": row["speed_limit_kmh"],
            "oneway": row["oneway"],
            "length_m": round(float(row["length_m"]), 1) if row["length_m"] is not None else None,
            # additive (C-K / C-Q)
            "geometry_basis": (
                "osm_display" if row.get("has_display_geom")
                else "osm" if row.get("geom_source") == "osm"
                else "schematic"
            ),
            "latest_observation": observation,
            "latest_emulated_observation": emulated_observation,
        },
    )


def _rel(relation: str, entities: Iterable[dict]) -> list[dict]:
    return [{"relation": relation, "entity": e} for e in entities]


class _Dossier:
    """Per-request dossier builder over one connection."""

    def __init__(self, conn: Any, *, now: datetime, valkey: Any, graph_db: Any) -> None:
        self.conn = conn
        self.now = now
        self.valkey = valkey
        self.graph_db = graph_db
        self._units: list[dict] | None = None
        self._cameras: list[dict] | None = None
        self.event_rows: dict[str, Any] = {}

    # -- loaders --------------------------------------------------------
    async def events(self, ids: list[str]) -> list[dict]:
        # Only canonical uuids reach the ::uuid[] bind (ids can come from
        # producer payloads — a slug must never become a 500).
        ids = list(dict.fromkeys(str(u) for u in map(as_uuid, ids) if u is not None))
        if not ids:
            return []
        rows = await self.conn.fetch(_SQL_DOSSIER_EVENTS, ids)
        if not rows:
            return []
        for r in rows:
            self.event_rows[str(r["event_id"])] = r
        spines, history = await load_event_spines(
            self.conn, [r["event_id"] for r in rows]
        )
        keys: list[str] = []
        for r in rows:
            keys.append(str(r["event_id"]))
            if r["movement_id"] is not None:
                keys.append(str(r["movement_id"]))
        bands = {str(b["movement_id"]): b for b in await self.conn.fetch(_SQL_SUBJECT_BANDS, keys)}
        out: list[dict] = []
        for r in rows:
            eid = str(r["event_id"])
            mid = str(r["movement_id"]) if r["movement_id"] is not None else None
            band = bands.get(eid) or (bands.get(mid) if mid else None)
            out.append(
                event_entity(
                    r,
                    spine_doc(spines.get(eid, [])),
                    band,
                    self.now,
                    route_history=history_doc(history.get(eid, [])),
                )
            )
        return out

    async def vehicles(self, ids: list[str]) -> list[tuple[Any, dict, list[Any]]]:
        """(row, entity, recent sightings) per vehicle — the trail only for
        actively earmarked vehicles (C6)."""
        if not ids:
            return []
        rows = await self.conn.fetch(_SQL_DOSSIER_VEHICLES, ids)
        earmarked_ids = [r["vehicle_id"] for r in rows if r["state"] is not None]
        reads: dict[str, list[Any]] = {}
        if earmarked_ids:
            for s in await self.conn.fetch(
                _SQL_RECENT_SIGHTINGS, earmarked_ids, MAX_RELATED_SIGHTINGS
            ):
                reads.setdefault(str(s["vehicle_id"]), []).append(s)
        # Vehicles with no lawful last read are badged from an aggregate
        # over their reads (never a default 'live', never one read).
        unbadged = [r["vehicle_id"] for r in rows if not reads.get(str(r["vehicle_id"]))]
        provenance: dict[str, str] = {}
        if unbadged:
            for p in await self.conn.fetch(_SQL_VEHICLE_READS_PROVENANCE, unbadged):
                provenance[str(p["vehicle_id"])] = (
                    "emulated" if p["all_emulated"] else "live"
                )
        out: list[tuple[Any, dict, list[Any]]] = []
        for r in rows:
            vid = str(r["vehicle_id"])
            trail = reads.get(vid, [])
            earmarked = r["state"] is not None
            ent = vehicle_entity(
                r,
                trail[0] if trail else None,
                self.now,
                earmarked=earmarked,
                reads_source=provenance.get(vid),
            )
            out.append((r, ent, trail))
        return out

    async def sightings(self, ids: list[str]) -> list[Any]:
        return list(await self.conn.fetch(_SQL_DOSSIER_SIGHTINGS, ids)) if ids else []

    async def recommendations(
        self, *, ids: list[str] = (), vehicle_ids: list[str] = (), sighting_ids: list[str] = ()
    ) -> list[Any]:
        if not (ids or vehicle_ids or sighting_ids):
            return []
        return list(
            await self.conn.fetch(
                _SQL_DOSSIER_RECOMMENDATIONS, list(ids), list(vehicle_ids), list(sighting_ids)
            )
        )

    async def dispatch_rows(
        self,
        *,
        ids: list[str] = (),
        event_ids: list[str] = (),
        unit_ids: list[str] = (),
        junction_ids: list[int] = (),
    ) -> list[Any]:
        if not (ids or event_ids or unit_ids or junction_ids):
            return []
        return list(
            await self.conn.fetch(
                _SQL_DOSSIER_DISPATCHES,
                list(ids),
                list(event_ids),
                list(unit_ids),
                list(junction_ids),
            )
        )

    async def junctions(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        return [
            junction_entity(r, self.now)
            for r in await self.conn.fetch(_SQL_DOSSIER_JUNCTIONS, ids)
        ]

    async def segments(self, ids: list[int], *, observations: bool = False) -> list[dict]:
        """Segment entities (display anchor + geometry_basis once 015 is in;
        with ``observations``, each one's latest hot-cache rows)."""
        if not ids:
            return []
        sql = (
            _SQL_DOSSIER_SEGMENTS_DISPLAY
            if await display_geom_supported(self.conn)
            else _SQL_DOSSIER_SEGMENTS
        )
        rows = await self.conn.fetch(sql, ids[:MAX_RELATED])
        latest: dict[int, tuple[dict | None, dict | None]] = {}
        if observations:
            latest = await self.latest_observations([int(r["segment_id"]) for r in rows])
        return [
            segment_entity(
                r,
                self.now,
                observation=latest.get(int(r["segment_id"]), (None, None))[0],
                emulated_observation=latest.get(int(r["segment_id"]), (None, None))[1],
            )
            for r in rows
        ]

    async def latest_observations(
        self, sids: list[int]
    ) -> dict[int, tuple[dict | None, dict | None]]:
        """(live row, emulator row) per segment from the hot cache — the
        live plane ``traffic:latest:{sid}`` and the emulator's own key
        space, kept apart (ingest/cache_writer.py).  {} without Valkey."""
        if self.valkey is None or not sids:
            return {}
        keys = [live_latest_key(s) for s in sids]
        keys += [emulated_latest_key(s) for s in sids]
        try:
            raws = await self.valkey.mget(keys)
        except Exception as exc:  # noqa: BLE001 — a dead Valkey hides the reading, not the dossier
            logger.warning("city.dossier_latest_read_failed", error=repr(exc))
            return {}
        n = len(sids)
        out: dict[int, tuple[dict | None, dict | None]] = {}
        for i, sid in enumerate(sids):
            live = latest_observation(raws[i], self.now)
            emulated = latest_observation(raws[n + i], self.now)
            if emulated is not None:
                emulated.update({"origin": "emulated", "data_quality": "synthetic"})
            if live is not None:
                live["origin"] = "live"
            out[sid] = (live, emulated)
        return out

    async def cameras(self) -> list[dict]:
        if self._cameras is None:
            self._cameras, _ = await load_cameras(self.conn, self.now)
        return self._cameras

    async def camera_entities(self, ids: Iterable[str]) -> list[dict]:
        wanted = list(dict.fromkeys(ids))
        by_id = {c["camera_id"]: c for c in await self.cameras()}
        return [camera_entity(by_id[c], self.now) for c in wanted if c in by_id]

    async def units(self) -> list[dict]:
        if self._units is None:
            self._units, _ = await collect_units(
                self.conn, graph_db=self.graph_db, valkey=self.valkey, now=self.now
            )
        return self._units

    async def convoy_doc(self, movement_id: str) -> dict | None:
        if self.valkey is None:
            return None
        try:
            return json_doc(await self.valkey.get(CONVOY_POSITION_PREFIX + movement_id))
        except Exception as exc:  # noqa: BLE001 — a dead Valkey hides the marker, not the dossier
            logger.warning("city.dossier_convoy_read_failed", error=repr(exc))
            return None

    async def convoys_for(self, event_id: str, movement_id: str | None) -> list[dict]:
        """Live C-B escort frames serving an event (by event_id, or by the
        event's movement / own id as the convoy movement key)."""
        if self.valkey is None:
            return []
        try:
            pairs, _ = await scan_mget(
                self.valkey, CONVOY_POSITION_PREFIX, max_keys=MAX_CONVOY_KEYS
            )
        except Exception as exc:  # noqa: BLE001 — a dead Valkey hides the marker, not the dossier
            logger.warning("city.dossier_convoy_scan_failed", error=repr(exc))
            return []
        keys = {subject_key(m) for m in (movement_id, event_id) if m}
        out: list[dict] = []
        for key, raw in pairs:
            doc = json_doc(raw)
            if not doc:
                continue
            # Match on the VALIDATED frame: its canonical event_id (a slug
            # is never a link) or its movement id.
            conv = convoy_entity(key, doc, self.now)
            if conv is None:
                continue
            if conv["props"]["event_id"] == event_id or subject_key(conv["id"]) in keys:
                out.append(conv)
        return out

    def dispatch_entities(self, rows: Iterable[Any], points: Mapping[str, Any]) -> list[dict]:
        out: list[dict] = []
        for r in rows:
            ent = dispatch_entity(r, self.now, fallback=points.get(str(r["event_id"])))
            ent["props"]["event_status"] = r["event_status"]
            out.append(ent)
        return out

    @staticmethod
    def event_points(events: Iterable[dict]) -> dict[str, tuple[float, float]]:
        return {
            e["id"]: (e["lon"], e["lat"]) for e in events if e["lon"] is not None
        }

    # -- per-kind dossiers ---------------------------------------------
    async def event(self, eid: str) -> tuple[dict, list[dict], FeedScope] | None:
        found = await self.events([eid])
        if not found:
            return None
        ent = found[0]
        row = self.event_rows[eid]
        meta = as_dict(row["metadata"])
        related: list[dict] = []
        # Metadata is free-form on events created before the pursuit chain
        # was validated: only well-formed ids are followed.
        vid = str(as_uuid(meta.get("vehicle_id"))) if as_uuid(meta.get("vehicle_id")) else None
        rid = (
            str(as_uuid(meta.get("recommendation_id")))
            if as_uuid(meta.get("recommendation_id")) else None
        )
        if vid:
            rel = "pursued_vehicle" if row["event_kind"] == "vehicle_pursuit" else "vehicle"
            related += _rel(rel, [e for _, e, _ in await self.vehicles([vid])])
        if rid:
            related += _rel(
                "authorizing_recommendation",
                [recommendation_entity(r, self.now)
                 for r in await self.recommendations(ids=[rid])],
            )
        drows = await self.dispatch_rows(event_ids=[eid])
        related += _rel("dispatch", self.dispatch_entities(drows, self.event_points([ent])))
        related += _rel(
            "unit",
            [unit_entity(u, self.now) for u in await self.units()
             if u.get("assigned_event_id") == eid],
        )
        spines = ent["props"]["route_spines"]
        if spines:
            related += _rel("route_segment", await self.segments(spines[0]["segment_ids"]))
        signal_junctions = [
            int(r["junction_id"])
            for r in await self.conn.fetch(_SQL_EVENT_SIGNAL_JUNCTIONS, eid)
        ]
        related += _rel("signal_junction", await self.junctions(signal_junctions))
        movement = str(row["movement_id"]) if row["movement_id"] is not None else None
        related += _rel("convoy", await self.convoys_for(eid, movement))
        ent["links"] = list(dict.fromkeys(
            ent["links"] + [r["entity"]["urn"] for r in related]
        ))
        scope = FeedScope(
            event_ids=[eid],
            movement_ids=[m for m in (eid, movement) if m],
            vehicle_ids=[vid] if vid else [],
            recommendation_ids=[rid] if rid else [],
            dispatch_ids=[str(d["dispatch_id"]) for d in drows],
        )
        return ent, related, scope

    async def vehicle(self, vid: str) -> tuple[dict, list[dict], FeedScope] | None:
        found = await self.vehicles([vid])
        if not found:
            return None
        row, ent, trail = found[0]
        recs = await self.recommendations(vehicle_ids=[vid])
        if row["state"] is None:
            # Not earmarked: only the reads that are evidence of a
            # recommendation (C6 — no lawful basis for a trail).
            evidence_ids = [str(r["sighting_id"]) for r in recs if r["sighting_id"]]
            trail = await self.sightings(evidence_ids)
            ent["props"]["privacy_note"] = (
                "no active earmark — sighting trail withheld; evidence reads only"
            )
        related: list[dict] = []
        related += _rel("sighting", [sighting_entity(s, self.now) for s in trail])
        related += _rel(
            "seen_by_camera", await self.camera_entities(s["camera_id"] for s in trail)
        )
        related += _rel("recommendation", [recommendation_entity(r, self.now) for r in recs])
        citing = [
            str(r["event_id"])
            for r in await self.conn.fetch(
                _SQL_DOSSIER_EVENTS_CITING,
                [vid],
                [str(r["recommendation_id"]) for r in recs],
            )
        ]
        events = await self.events(citing)
        for e in events:
            rel = "pursuit" if e["props"]["event_kind"] == "vehicle_pursuit" else "event"
            related += _rel(rel, [e])
        ent["links"] = list(dict.fromkeys(
            ent["links"] + [r["entity"]["urn"] for r in related]
        ))
        scope = FeedScope(
            vehicle_ids=[vid],
            recommendation_ids=[str(r["recommendation_id"]) for r in recs],
            event_ids=citing,
            movement_ids=citing,
        )
        return ent, related, scope

    async def sighting(self, sid: str) -> tuple[dict, list[dict], FeedScope] | None:
        rows = await self.sightings([sid])
        if not rows:
            return None
        s = rows[0]
        ent = sighting_entity(s, self.now)
        related: list[dict] = []
        related += _rel("vehicle", [e for _, e, _ in await self.vehicles([str(s["vehicle_id"])])])
        related += _rel("camera", await self.camera_entities([s["camera_id"]]))
        recs = await self.recommendations(sighting_ids=[sid])
        related += _rel("recommendation", [recommendation_entity(r, self.now) for r in recs])
        scope = FeedScope(
            sighting_ids=[sid],
            recommendation_ids=[str(r["recommendation_id"]) for r in recs],
        )
        return ent, related, scope

    async def recommendation(self, rid: str) -> tuple[dict, list[dict], FeedScope] | None:
        recs = await self.recommendations(ids=[rid])
        recs = [r for r in recs if str(r["recommendation_id"]) == rid]
        if not recs:
            return None
        r = recs[0]
        ent = recommendation_entity(r, self.now)
        related: list[dict] = []
        related += _rel("vehicle", [e for _, e, _ in await self.vehicles([str(r["vehicle_id"])])])
        sid = str(r["sighting_id"]) if r["sighting_id"] else None
        if sid:
            evidence = await self.sightings([sid])
            related += _rel("evidence_sighting", [sighting_entity(s, self.now) for s in evidence])
        if r["camera_id"]:
            related += _rel("camera", await self.camera_entities([r["camera_id"]]))
        seg = finite(as_dict(r["evidence"]).get("segment_id"))
        if seg is not None:
            related += _rel("segment", await self.segments([int(seg)]))
        citing = [
            str(x["event_id"])
            for x in await self.conn.fetch(_SQL_DOSSIER_EVENTS_CITING, [], [rid])
        ]
        for e in await self.events(citing):
            rel = "pursuit" if e["props"]["event_kind"] == "vehicle_pursuit" else "event"
            related += _rel(rel, [e])
        scope = FeedScope(
            recommendation_ids=[rid],
            sighting_ids=[sid] if sid else [],
            event_ids=citing,
        )
        return ent, related, scope

    async def camera(self, cid: str) -> tuple[dict, list[dict], FeedScope] | None:
        found = await self.camera_entities([cid])
        if not found:
            return None
        ent = found[0]
        related: list[dict] = []
        jid = ent["props"].get("junction_id")
        if jid:
            related += _rel("junction", await self.junctions([int(jid)]))
        hits = await self.conn.fetch(_SQL_DOSSIER_CAMERA_HITS, cid)
        related += _rel("recent_hit", [sighting_entity(s, self.now) for s in hits])
        return ent, related, FeedScope(camera_ids=[cid])

    async def junction(self, jid: int) -> tuple[dict, list[dict], FeedScope] | None:
        found = await self.junctions([jid])
        if not found:
            return None
        ent = found[0]
        related: list[dict] = []
        segs = [int(r["segment_id"]) for r in await self.conn.fetch(_SQL_JUNCTION_SEGMENTS, jid)]
        related += _rel("adjacent_segment", await self.segments(sorted(segs)))
        drows = [
            d for d in await self.dispatch_rows(junction_ids=[jid])
            if d["status"] in ("requested", "approved", "en_route", "on_scene")
            and d["event_status"] in ("planning", "approved", "active")
        ]
        related += _rel("dispatch_waypoint", self.dispatch_entities(drows, {}))
        cams = [c["camera_id"] for c in await self.cameras() if c.get("junction_id") == jid]
        related += _rel("camera", await self.camera_entities(cams))
        signal_events = [
            str(r["event_id"])
            for r in await self.conn.fetch(_SQL_JUNCTION_SIGNAL_EVENTS, jid)
        ]
        related += _rel("signal_event", await self.events(signal_events))
        return ent, related, FeedScope(junction_ids=[jid])

    async def unit(self, uid: str) -> tuple[dict, list[dict], FeedScope] | None:
        live = next((u for u in await self.units() if u["unit_id"] == uid), None)
        drows = await self.dispatch_rows(unit_ids=[uid])
        if live is None and not drows:
            return None
        if live is not None:
            ent = unit_entity(live, self.now)
        else:
            latest = drows[0]
            ent = city_entity(
                "unit",
                uid,
                f"{agency_label(latest['agency'])} · {uid}",
                None,
                None,
                now=self.now,
                state="not_fielded",
                source="emulated",
                t=latest["updated_at"],
                links=[urn("dispatch", str(latest["dispatch_id"]))],
                props={
                    "agency": latest["agency"],
                    "last_dispatch_status": latest["status"],
                    "emulated": True,
                },
            )
        event_ids = list(dict.fromkeys(str(d["event_id"]) for d in drows))
        events = await self.events(event_ids[:10])
        related = _rel("dispatch", self.dispatch_entities(drows[:10], self.event_points(events)))
        if live is not None and live.get("assigned_event_id"):
            related += _rel(
                "assigned_event",
                [e for e in events if e["id"] == live["assigned_event_id"]],
            )
        return ent, related, FeedScope(unit_ids=[uid])

    async def dispatch(self, did: str) -> tuple[dict, list[dict], FeedScope] | None:
        drows = [d for d in await self.dispatch_rows(ids=[did]) if str(d["dispatch_id"]) == did]
        if not drows:
            return None
        d = drows[0]
        events = await self.events([str(d["event_id"])])
        ent = self.dispatch_entities([d], self.event_points(events))[0]
        related = _rel("event", events)
        unit = next(
            (u for u in await self.units() if u.get("dispatch_id") == did), None
        )
        if unit is not None:
            related += _rel("unit", [unit_entity(unit, self.now)])
        if d["waypoint_junction"]:
            related += _rel(
                "waypoint_junction", await self.junctions([int(d["waypoint_junction"])])
            )
        return ent, related, FeedScope(dispatch_ids=[did])

    async def convoy(self, mid: str) -> tuple[dict, list[dict], FeedScope] | None:
        doc = await self.convoy_doc(mid)
        ent = convoy_entity(CONVOY_POSITION_PREFIX + mid, doc, self.now) if doc else None
        event_ids: list[str] = []
        movement_uuid = as_uuid(mid)
        if movement_uuid is not None:  # slug movement ids never link an event
            event_ids = [
                str(r["event_id"])
                for r in await self.conn.fetch(_SQL_DOSSIER_EVENTS_FOR_MOVEMENT, movement_uuid)
            ]
        if ent is not None and ent["props"].get("event_id"):
            # convoy_entity only ever emits a CANONICAL uuid here.
            event_ids = list(dict.fromkeys([ent["props"]["event_id"], *event_ids]))
        if ent is None and not event_ids:
            return None
        events = await self.events(event_ids)
        if ent is None:
            emulated = any(e["source"] == "emulated" for e in events)
            ent = city_entity(
                "convoy",
                mid,
                f"Convoy {mid[:8] if movement_uuid else mid}",
                None,
                None,
                now=self.now,
                state="no_position",
                source="emulated" if emulated else "live",
                links=[e["urn"] for e in events],
                props={"movement_id": mid, "note": "no live convoy:position frame"},
            )
        return ent, _rel("event", events), FeedScope(movement_ids=[mid], event_ids=event_ids)

    async def anomaly(self, aid: int) -> tuple[dict, list[dict], FeedScope | None] | None:
        sql = (
            _SQL_DOSSIER_ANOMALY_DISPLAY
            if await display_geom_supported(self.conn)
            else _SQL_DOSSIER_ANOMALY
        )
        row = await self.conn.fetchrow(sql, aid)
        if row is None:
            return None
        return (
            anomaly_entity(row, self.now),
            _rel("segment", await self.segments([int(row["segment_id"])])),
            None,
        )

    async def segment(self, sid: int) -> tuple[dict, list[dict], FeedScope | None] | None:
        found = await self.segments([sid], observations=True)
        if not found:
            return None
        ent = found[0]
        related: list[dict] = []
        jids = [int(r["junction_id"]) for r in await self.conn.fetch(_SQL_SEGMENT_JUNCTIONS, sid)]
        related += _rel("adjacent_junction", await self.junctions(sorted(jids)))
        events_sql = (
            _SQL_SEGMENT_EVENTS_CURRENT
            if await routes_supersede_supported(self.conn)
            else _SQL_SEGMENT_EVENTS
        )
        events = [str(r["event_id"]) for r in await self.conn.fetch(events_sql, sid)]
        related += _rel("spine_of", await self.events(events))
        anomalies_sql = (
            _SQL_SEGMENT_ANOMALIES_DISPLAY
            if await display_geom_supported(self.conn)
            else _SQL_SEGMENT_ANOMALIES
        )
        anomalies = await self.conn.fetch(anomalies_sql, sid)
        related += _rel("anomaly", [anomaly_entity(r, self.now) for r in anomalies])
        return ent, related, None


async def build_dossier(
    conn: Any,
    kind: str,
    ident: str | int,
    *,
    now: datetime,
    valkey: Any = None,
    graph_db: Any = None,
    audit_limit: int = 50,
) -> dict | None:
    """The dossier for ``kind:ident`` or None when the entity is unknown."""
    builder = _Dossier(conn, now=now, valkey=valkey, graph_db=graph_db)
    result = await getattr(builder, kind)(ident)
    if result is None:
        return None
    entity, related, scope = result
    audit: list[dict] = []
    audit_sources: dict[str, dict] = {}
    if scope is not None:
        audit, audit_sources = await collect_feed(
            conn, since=EPOCH, limit=audit_limit, scope=scope, valkey=valkey
        )
    return {
        "entity": entity,
        "related": related,
        "audit": audit,
        "audit_sources": audit_sources,
    }


__all__ = ["MAX_RELATED", "MAX_RELATED_SIGHTINGS", "build_dossier", "segment_entity"]
