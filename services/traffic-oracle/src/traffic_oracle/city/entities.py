"""City snapshot — GET /api/v1/city/entities (contract C-C).

One fused, geolocated picture of the city, assembled server-side from
tables and Valkey keys that already exist (the client never simulates):

    kind            source                                       position
    event           events.corridor_events (planning|approved|   ON the rank-1 spine: its
                    active) + labelled route spines + latest     point nearest the spine's
                    situation band                               centroid (zone centroid as
                                                                 fallback)
    vehicle         ACTIVE earmarks only (lawful basis, C6)       last sighting
    sighting        last 8 reads per FLAGGED vehicle              the read
    camera          city/cameras.py (registry or derived)         the camera
    junction        corridor.junctions + signal program phase     the junction
                    (signals.plans.active_phase)
    unit            units/router.collect_units: dispatch          projected / patrol frame
                    projection of live events + Valkey
                    units:emulated:* (contract C-A)
    convoy          Valkey convoy:position:* (contract C-B);      GPS / emulated GPS
                    linked to its event only through a canonical
                    uuid event_id
    dispatch        open dispatches of live events                waypoint junction
    recommendation  pending | approved e-challans                 the evidence sighting
    anomaly         traffic.anomaly_log, last 15 min, latest      the segment
                    per segment

Every query runs on ONE connection, batched per kind (no per-row
queries; the unit projection caches its graph paths), so the snapshot
stays well under the 150 ms poll budget.  A kind whose source fails is
reported in ``feeds[kind].status`` and the rest of the picture still
renders — degradation is visible, never a blank 500.

Entities without a known position are not emitted (they are counted in
``feeds[kind].unlocated``): every snapshot entity has lon/lat.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

import structlog

from traffic_oracle.city.cameras import load_cameras
from traffic_oracle.city.common import (
    ENTITY_KINDS,
    as_dict,
    as_uuid,
    city_entity,
    display_geom_supported,
    event_source,
    finite,
    in_bbox,
    is_slug,
    iso_z,
    json_doc,
    label_source,
    mask_plate,
    mask_plates_in_text,
    parse_ts,
    routes_supersede_supported,
    scan_mget,
    subject_key,
    urn,
    valid_lonlat,
)
from traffic_oracle.enforcement.engine import SQL_STOLEN_EARMARK
from traffic_oracle.enforcement.rules import CHALLAN_WITHHELD_TEXT, is_penal
from traffic_oracle.signals.plans import active_phase, green_ratio
from traffic_oracle.signals.writer import parse_phases
from traffic_oracle.units.agencies import agency_label
from traffic_oracle.units.router import collect_units

logger = structlog.get_logger(__name__)

#: Contract C-B: escort GPS mirror (producer WP-12, wave 2 — absence is a
#: normal, labelled state until then).
CONVOY_POSITION_PREFIX = "convoy:position:"
MAX_CONVOY_KEYS = 200

#: Camera hops per flagged vehicle (the trace on the canvas).
SIGHTINGS_PER_VEHICLE = 8

#: Statuses of events that are still live.
LIVE_EVENT_STATUSES = ("planning", "approved", "active")

#: ``props.position_basis`` of an event drawn on its rank-1 spine: the point
#: OF the spine nearest the spine's centroid (on the road, not the centroid
#: itself — an earlier build said 'spine_centroid' and drew it off the road).
SPINE_MARKER_BASIS = "spine_point_nearest_centroid"
#: ... of a dispatch whose officer named no waypoint junction: drawn at its
#: event's marker (an earlier build said 'event_spine_centroid').
DISPATCH_AT_EVENT_BASIS = "event_marker"

# ---------------------------------------------------------------------------
# Read SQL (module constants — parameter-bound, read-only)
# ---------------------------------------------------------------------------

_SQL_LIVE_EVENTS = "SELECT e.event_id, e.event_kind, e.protection_class, e.crowd_class, e.title, e.status, e.movement_id, e.planned_start, e.planned_end, e.actual_start, e.actual_end, e.metadata, e.created_at, e.updated_at, ST_X(ST_Centroid(e.zone_geom)) AS zone_lon, ST_Y(ST_Centroid(e.zone_geom)) AS zone_lat FROM events.corridor_events e WHERE e.status IN ('planning', 'approved', 'active') ORDER BY e.planned_start NULLS LAST, e.created_at LIMIT 200"  # noqa: E501

#: Labelled spines, each with its MARKER: the point ON the spine's road
#: segments nearest their centroid, ST_ClosestPoint(spine, centroid).  The
#: centroid alone sits off the road — on the curved OpenStreetMap roads
#: (WP-20) from metres to kilometres (a loop's centroid lands inside it).
_SQL_EVENT_SPINES = "SELECT r.event_id, r.route_rank, r.label, r.segment_ids, ST_X(c.pt) AS lon, ST_Y(c.pt) AS lat FROM events.event_routes r LEFT JOIN LATERAL (SELECT ST_ClosestPoint(g.line, ST_Centroid(g.line)) AS pt FROM (SELECT ST_Collect(rs.geom) AS line FROM corridor.road_segments rs WHERE rs.segment_id = ANY(r.segment_ids)) g) c ON TRUE WHERE r.event_id = ANY($1::uuid[]) ORDER BY r.event_id, r.route_rank"  # noqa: E501

#: Migration 018 (probed — common.routes_supersede_supported): the CURRENT
#: spines with their re-route variant, and the superseded ones.
_SQL_EVENT_SPINES_CURRENT = "SELECT r.event_id, r.route_rank, r.label, r.segment_ids, r.variant, ST_X(c.pt) AS lon, ST_Y(c.pt) AS lat FROM events.event_routes r LEFT JOIN LATERAL (SELECT ST_ClosestPoint(g.line, ST_Centroid(g.line)) AS pt FROM (SELECT ST_Collect(rs.geom) AS line FROM corridor.road_segments rs WHERE rs.segment_id = ANY(r.segment_ids)) g) c ON TRUE WHERE r.event_id = ANY($1::uuid[]) AND r.superseded_at IS NULL ORDER BY r.event_id, r.route_rank, r.created_at DESC"  # noqa: E501

_SQL_EVENT_SPINE_HISTORY = "SELECT r.event_id, r.route_rank, r.label, r.segment_ids, r.variant, r.created_by, r.superseded_at FROM events.event_routes r WHERE r.event_id = ANY($1::uuid[]) AND r.superseded_at IS NOT NULL ORDER BY r.event_id, r.superseded_at, r.route_rank"  # noqa: E501

#: Latest situation band per adaptive subject key (index-backed).
_SQL_SUBJECT_BANDS = "SELECT DISTINCT ON (l.movement_id) l.movement_id, l.situation_class, l.situation_code, l.severity_score, l.observed_at FROM adaptive.situation_log l WHERE l.movement_id = ANY($1::text[]) ORDER BY l.movement_id, l.observed_at DESC"  # noqa: E501

#: Vehicles with an ACTIVE earmark (expiry enforced in the query, C6).
_SQL_EARMARKED_VEHICLES = "SELECT DISTINCT ON (ve.vehicle_id) ve.vehicle_id, v.plate, v.vehicle_class, ve.state, ve.reason, ve.lawful_basis, ve.retention_until, ve.earmarked_at, ve.earmarked_by FROM corridor.vehicle_earmarks ve JOIN corridor.vehicles v ON v.vehicle_id = ve.vehicle_id WHERE ve.cleared_at IS NULL AND ve.retention_until > NOW() ORDER BY ve.vehicle_id, ve.earmarked_at DESC"  # noqa: E501

#: Last N reads per vehicle (lateral over the (vehicle_id, t) index);
#: future-dated reads have not happened yet and are excluded.
_SQL_RECENT_SIGHTINGS = "SELECT v.vid AS vehicle_id, s.sighting_id, s.plate, s.camera_id, s.t, ST_X(s.position) AS lon, ST_Y(s.position) AS lat, s.speed_kmh, s.heading, s.vehicle_class, s.source, s.run_id, s.matched_plate_hit FROM unnest($1::uuid[]) AS v(vid) CROSS JOIN LATERAL (SELECT x.sighting_id, x.plate, x.camera_id, x.t, x.position, x.speed_kmh, x.heading, x.vehicle_class, x.source, x.run_id, x.matched_plate_hit FROM corridor.vehicle_sightings x WHERE x.vehicle_id = v.vid AND x.t <= NOW() ORDER BY x.t DESC LIMIT $2) s ORDER BY v.vid, s.t DESC"  # noqa: E501

#: Every junction + its program (to_jsonb keeps 012's name columns optional).
_SQL_JUNCTIONS = "SELECT j.junction_id, j.junction_type, j.signal_control, to_jsonb(j) - 'geom' AS doc, ST_X(j.geom) AS lon, ST_Y(j.geom) AS lat, sp.cycle_sec, sp.phases, sp.offset_sec, sp.source AS program_source, sp.updated_at AS program_updated_at FROM corridor.junctions j LEFT JOIN corridor.signal_programs sp ON sp.junction_id = j.junction_id ORDER BY j.junction_id"  # noqa: E501

#: Open dispatches of LIVE events, located at their waypoint junction.
_SQL_LIVE_DISPATCHES = "SELECT d.dispatch_id, d.event_id, d.agency, d.unit_id, d.waypoint_junction, d.status, d.requested_by, d.decided_by, d.approved_at, d.created_at, d.updated_at, ST_X(j.geom) AS lon, ST_Y(j.geom) AS lat, ev.title AS event_title, ev.metadata AS event_metadata FROM corridor.dispatches d JOIN events.corridor_events ev ON ev.event_id = d.event_id LEFT JOIN corridor.junctions j ON j.junction_id = d.waypoint_junction WHERE d.status IN ('requested', 'approved', 'en_route', 'on_scene') AND ev.status IN ('planning', 'approved', 'active') ORDER BY d.created_at DESC LIMIT 200"  # noqa: E501

#: Pending + approved e-challans located at their evidence sighting, with
#: whether the vehicle is reported stolen right now (C-N: withheld).
_SQL_OPEN_RECOMMENDATIONS = "SELECT r.recommendation_id, r.sighting_id, r.vehicle_id, r.rule_id, r.recommendation, r.evidence, r.status, r.decided_by, r.decided_at, r.created_at, s.camera_id, s.t AS sighting_t, s.source AS sighting_source, s.plate, ST_X(s.position) AS lon, ST_Y(s.position) AS lat, " + SQL_STOLEN_EARMARK + " AS stolen_vehicle FROM events.enforcement_recommendations r LEFT JOIN corridor.vehicle_sightings s ON s.sighting_id = r.sighting_id WHERE r.status IN ('pending', 'approved') ORDER BY (r.status = 'pending') DESC, COALESCE(r.decided_at, r.created_at) DESC LIMIT 300"  # noqa: E501

#: Latest anomaly per segment in the last 15 minutes (+ count), driven by
#: the (segment_id, timestamp_utc) index — bounded by the segment count.
_SQL_RECENT_ANOMALIES = "SELECT rs.segment_id, rs.road_name, ST_X(ST_PointOnSurface(rs.geom)) AS lon, ST_Y(ST_PointOnSurface(rs.geom)) AS lat, a.anomaly_id, a.timestamp_utc, a.anomaly_type, a.severity, a.details, a.flagged_at, c.n_15m FROM corridor.road_segments rs JOIN LATERAL (SELECT x.anomaly_id, x.timestamp_utc, x.anomaly_type, x.severity, x.details, x.flagged_at FROM traffic.anomaly_log x WHERE x.segment_id = rs.segment_id AND x.timestamp_utc > NOW() - interval '15 minutes' ORDER BY x.timestamp_utc DESC LIMIT 1) a ON TRUE JOIN LATERAL (SELECT count(*) AS n_15m FROM traffic.anomaly_log y WHERE y.segment_id = rs.segment_id AND y.timestamp_utc > NOW() - interval '15 minutes') c ON TRUE ORDER BY a.timestamp_utc DESC LIMIT 200"  # noqa: E501

#: Migration 015 (C-K, probed — common.display_geom_supported): the marker
#: sits ON the drawn road, COALESCE(display_geom, geom).
_SQL_RECENT_ANOMALIES_DISPLAY = _SQL_RECENT_ANOMALIES.replace(
    "ST_PointOnSurface(rs.geom)", "ST_PointOnSurface(COALESCE(rs.display_geom, rs.geom))"
)


# ---------------------------------------------------------------------------
# Converters (pure; shared with the dossier)
# ---------------------------------------------------------------------------


def spine_doc(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    """``[{label, segment_ids, rank, variant, lon, lat}]`` for one event's
    CURRENT routes (``variant`` 'reroute-N' | 'plan_b' on a spine a route
    update wrote, migration 018 — None otherwise)."""
    out: list[dict] = []
    for r in rows:
        point = valid_lonlat(r["lon"], r["lat"])
        out.append(
            {
                "label": r["label"],
                "rank": int(r["route_rank"]),
                "segment_ids": [int(s) for s in (r["segment_ids"] or [])],
                "variant": r.get("variant"),
                "lon": point[0] if point else None,
                "lat": point[1] if point else None,
            }
        )
    return out


def history_doc(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    """``[{label, variant, segment_ids, rank, created_by, superseded_at}]``
    — the spines route updates superseded (C-O), oldest change first."""
    return [
        {
            "label": r["label"],
            "variant": r.get("variant"),
            "rank": int(r["route_rank"]),
            "segment_ids": [int(s) for s in (r["segment_ids"] or [])],
            "created_by": r.get("created_by"),
            "superseded_at": iso_z(r.get("superseded_at")),
        }
        for r in rows
    ]


async def load_event_spines(
    conn: Any, ids: list[Any]
) -> tuple[dict[str, list[Any]], dict[str, list[Any]]]:
    """(current spine rows, superseded spine rows) per event id — two
    batched reads on a 018 store, one (and no history) before it."""
    current: dict[str, list[Any]] = {}
    history: dict[str, list[Any]] = {}
    if not ids:
        return current, history
    supersede = await routes_supersede_supported(conn)
    sql = _SQL_EVENT_SPINES_CURRENT if supersede else _SQL_EVENT_SPINES
    for r in await conn.fetch(sql, ids):
        current.setdefault(str(r["event_id"]), []).append(r)
    if supersede:
        for r in await conn.fetch(_SQL_EVENT_SPINE_HISTORY, ids):
            history.setdefault(str(r["event_id"]), []).append(r)
    return current, history


def event_entity(
    row: Mapping[str, Any],
    spines: list[dict],
    band_row: Mapping[str, Any] | None,
    now: datetime,
    *,
    links: Iterable[str] = (),
    route_history: list[dict] | None = None,
) -> dict:
    meta = as_dict(row["metadata"])
    point = next(
        ((s["lon"], s["lat"]) for s in sorted(spines, key=lambda s: s["rank"])
         if s["lon"] is not None),
        None,
    )
    # ON the route: the spine's point nearest its centroid (_SQL_EVENT_SPINES)
    basis = SPINE_MARKER_BASIS
    if point is None:
        zone = valid_lonlat(row.get("zone_lon"), row.get("zone_lat"))
        point, basis = (zone, "zone_centroid") if zone else (None, None)
    band = band_row["situation_class"] if band_row is not None else None
    band_t = band_row["observed_at"] if band_row is not None else None
    updated = row["updated_at"]
    t = max((x for x in (updated, band_t) if x is not None), default=None)
    rank1 = next((s for s in sorted(spines, key=lambda s: s["rank"])), None)
    seg_links = [urn("segment", sid) for sid in (rank1["segment_ids"] if rank1 else [])]
    meta_links = []
    for key, kind in (("vehicle_id", "vehicle"), ("recommendation_id", "recommendation")):
        value = as_uuid(meta.get(key))
        if value is not None:
            meta_links.append(urn(kind, str(value)))
    props = {
        "event_kind": row["event_kind"],
        "title": row["title"],
        "status": row["status"],
        "protection_class": row["protection_class"],
        "crowd_class": row.get("crowd_class"),
        "route_spines": [
            {"label": s["label"], "segment_ids": s["segment_ids"], "variant": s.get("variant")}
            for s in spines
        ],
        # additive (C-O): spines a route update superseded (never deleted)
        "route_history": list(route_history or []),
        "movement_id": str(row["movement_id"]) if row.get("movement_id") else None,
        "planned_start": iso_z(row.get("planned_start")),
        "planned_end": iso_z(row.get("planned_end")),
        "actual_start": iso_z(row.get("actual_start")),
        "actual_end": iso_z(row.get("actual_end")),
        "position_basis": basis,
        "band_t": iso_z(band_t),
        "band_code": band_row["situation_code"] if band_row is not None else None,
        "metadata": {
            k: meta.get(k)
            for k in ("vehicle_id", "recommendation_id", "source", "scenario_id",
                      "use_case_id", "mode", "run_id", "fir", "opened_by")
            if meta.get(k) is not None
        },
    }
    return city_entity(
        "event",
        str(row["event_id"]),
        mask_plates_in_text(row["title"]),
        point[0] if point else None,
        point[1] if point else None,
        now=now,
        state=row["status"],
        band=band,
        source=event_source(meta),
        t=t,
        links=[*meta_links, *links, *seg_links],
        props=props,
    )


def vehicle_entity(
    row: Mapping[str, Any],
    last: Mapping[str, Any] | None,
    now: datetime,
    *,
    links: Iterable[str] = (),
    earmarked: bool = True,
    reads_source: str | None = None,
) -> dict:
    """One vehicle.  Its badge is its last LAWFUL read's source; with no
    such read (reads withheld — no active earmark) it is ``reads_source``,
    an aggregate over ALL its reads that discloses no individual read
    ('emulated' when every read is emulated), else 'live'."""
    plate = row["plate"]
    props = {
        "plate": plate,
        "plate_masked": mask_plate(plate),
        "vehicle_class": row.get("vehicle_class"),
        "earmark_state": row.get("state") if earmarked else None,
        "lawful_basis": row.get("lawful_basis") if earmarked else None,
        "earmark_reason": row.get("reason") if earmarked else None,
        "retention_until": iso_z(row.get("retention_until")) if earmarked else None,
        "last_sighting_id": str(last["sighting_id"]) if last else None,
        "last_camera_id": last["camera_id"] if last else None,
        "last_speed_kmh": finite(last["speed_kmh"]) if last else None,
        "last_heading": finite(last["heading"]) if last else None,
        "matched_plate_hit": last["matched_plate_hit"] if last else None,
    }
    return city_entity(
        "vehicle",
        str(row["vehicle_id"]),
        mask_plate(plate) or "vehicle",
        last["lon"] if last else None,
        last["lat"] if last else None,
        now=now,
        state=row.get("state") if earmarked else "not_earmarked",
        source=label_source(last["source"]) if last else (reads_source or "live"),
        t=last["t"] if last else None,
        links=links,
        props=props,
    )


def sighting_entity(row: Mapping[str, Any], now: datetime, *, hop: int | None = None) -> dict:
    plate_masked = mask_plate(row["plate"])
    return city_entity(
        "sighting",
        str(row["sighting_id"]),
        f"{row['camera_id']} · {plate_masked}",
        row["lon"],
        row["lat"],
        now=now,
        state=row["matched_plate_hit"] or "read",
        source=label_source(row["source"]),
        t=row["t"],
        links=[urn("vehicle", str(row["vehicle_id"])), urn("camera", row["camera_id"])],
        props={
            "plate": row["plate"],
            "plate_masked": plate_masked,
            "camera_id": row["camera_id"],
            "vehicle_id": str(row["vehicle_id"]),
            "speed_kmh": finite(row["speed_kmh"]),
            "heading": finite(row["heading"]),
            "vehicle_class": row.get("vehicle_class"),
            "matched_plate_hit": row["matched_plate_hit"],
            "run_id": row.get("run_id"),
            "read_source": row["source"],
            "hop": hop,
        },
    )


def camera_entity(cam: Mapping[str, Any], now: datetime) -> dict:
    links = [urn("junction", cam["junction_id"])] if cam.get("junction_id") else []
    if cam.get("segment_id"):
        links.append(urn("segment", cam["segment_id"]))
    return city_entity(
        "camera",
        cam["camera_id"],
        cam.get("label") or cam["camera_id"],
        cam["lon"],
        cam["lat"],
        now=now,
        state=cam.get("status"),
        source=label_source(cam.get("source")),
        t=cam.get("_last_read_dt"),
        links=links,
        props={
            "camera_kind": cam.get("kind"),
            "camera_source": cam.get("source"),
            "registered": cam.get("registered"),
            "junction_id": cam.get("junction_id"),
            "segment_id": cam.get("segment_id"),
            "heading_deg": cam.get("heading_deg"),
            "reads_15m": cam.get("reads_15m", 0),
            "hits_15m": cam.get("hits_15m", 0),
            "last_read_t": cam.get("last_read_t"),
        },
    )


def junction_entity(row: Mapping[str, Any], now: datetime) -> dict:
    doc = as_dict(row.get("doc"))
    jid = int(row["junction_id"])
    name = doc.get("name") if isinstance(doc.get("name"), str) else None
    props: dict[str, Any] = {
        "junction_type": row["junction_type"],
        "signal_control": bool(row["signal_control"]),
        "name": name,
        "name_gu": doc.get("name_gu") if isinstance(doc.get("name_gu"), str) else None,
        "program_source": row["program_source"],
    }
    state = "unsignalled" if not row["signal_control"] else "no_program"
    t = None
    if row["cycle_sec"] is not None:
        phases = parse_phases(row["phases"])  # same parse as /signals/state
        cycle = int(row["cycle_sec"])
        offset = int(row["offset_sec"] or 0)
        phase = active_phase(phases, cycle, offset, now.timestamp())
        props.update(
            {
                "cycle_sec": cycle,
                "offset_sec": offset,
                "green_ratio": round(green_ratio(phases, cycle), 4),
                "active_phase": phase,
                # The phase is the stored program's clock evaluated now —
                # not controller telemetry; its age is the program's age.
                "phase_basis": "program_clock",
                "program_updated_at": iso_z(row["program_updated_at"]),
            }
        )
        state = "lost" if phase.get("lost") else "green"
        t = row["program_updated_at"]
    label = name or f"J{jid} · {row['junction_type']}"
    return city_entity(
        "junction",
        jid,
        label,
        row["lon"],
        row["lat"],
        now=now,
        state=state,
        source="live",
        t=t,
        props=props,
    )


def unit_entity(unit: Mapping[str, Any], now: datetime) -> dict:
    pos = unit.get("position")
    located = isinstance(pos, (list, tuple)) and len(pos) == 2
    lon, lat = (pos[0], pos[1]) if located else (None, None)
    links = []
    if unit.get("dispatch_id"):
        links.append(urn("dispatch", unit["dispatch_id"]))
    if unit.get("assigned_event_id"):
        links.append(urn("event", unit["assigned_event_id"]))
    if unit.get("waypoint_junction"):
        links.append(urn("junction", unit["waypoint_junction"]))
    # A projected position is evaluated at the snapshot instant; a patrol
    # frame carries its own time (None when the producer sent none).
    patrol = unit.get("source") == "emulated_patrol"
    t = parse_ts(unit.get("t")) if patrol else now
    return city_entity(
        "unit",
        unit["unit_id"],
        unit.get("label") or str(unit["unit_id"]),
        lon,
        lat,
        now=now,
        state=unit.get("status"),
        source="emulated",  # every unit on this surface is emulated (badged)
        t=t,
        links=links,
        props={
            "agency": unit.get("agency"),
            "agency_label": unit.get("agency_label") or agency_label(str(unit.get("agency"))),
            "unit_source": unit.get("source"),
            "dispatch_id": unit.get("dispatch_id"),
            "assigned_event_id": unit.get("assigned_event_id"),
            "waypoint_junction": unit.get("waypoint_junction"),
            "eta_sec": unit.get("eta_sec"),
            "progress": unit.get("progress"),
            "arrived": unit.get("arrived"),
            "speed_kmh": unit.get("speed_kmh"),
            "heading": unit.get("heading"),
            "run_id": unit.get("run_id"),
            "emulated": True,
            # a unit approved on several live dispatches stays VISIBLE
            "double_booked": bool(unit.get("double_booked")),
            "other_dispatch_ids": [
                d["dispatch_id"] for d in unit.get("other_dispatches") or []
            ],
        },
    )


def convoy_entity(key: str, doc: Mapping[str, Any], now: datetime) -> dict | None:
    """One C-B escort position; None when it fails validation.

    C-B types ``event_id`` as ``str|null``: only a value that parses as a
    uuid is a corridor-event link (emitted canonical, lowercase).  Any
    other string is kept visible as ``props.event_ref`` and never linked
    or looked up — a producer slug can neither 500 a dossier nor mint a
    dangling ``event:<slug>`` URN.
    """
    mid = doc.get("movement_id") or key[len(CONVOY_POSITION_PREFIX):]
    point = valid_lonlat(doc.get("lon"), doc.get("lat"))
    if not is_slug(mid) or point is None:
        return None
    speed = finite(doc.get("speed_kmh"))
    raw_event = doc.get("event_id")
    event_uuid = as_uuid(raw_event.strip()) if isinstance(raw_event, str) else None
    event_id = str(event_uuid) if event_uuid is not None else None
    event_ref = (
        raw_event.strip()[:64]
        if isinstance(raw_event, str) and raw_event.strip() and event_uuid is None
        else None
    )
    segment_id = finite(doc.get("segment_id"))
    links = []
    if event_id:
        links.append(urn("event", event_id))
    if segment_id is not None:
        links.append(urn("segment", int(segment_id)))
    return city_entity(
        "convoy",
        mid,
        f"Convoy {mid[:8] if as_uuid(mid) else mid}",
        point[0],
        point[1],
        now=now,
        state=("moving" if speed > 1.0 else "holding") if speed is not None else "tracking",
        source=label_source(doc.get("source")),
        t=parse_ts(doc.get("t")),
        links=links,
        props={
            "movement_id": mid,
            "event_id": event_id,
            "event_ref": event_ref,
            "speed_kmh": speed,
            "heading": finite(doc.get("heading")),
            "progress": finite(doc.get("progress")),
            "segment_id": int(segment_id) if segment_id is not None else None,
            "position_source": doc.get("source"),
        },
    )


def dispatch_entity(
    row: Mapping[str, Any],
    now: datetime,
    *,
    fallback: tuple[float, float] | None = None,
) -> dict:
    point = valid_lonlat(row["lon"], row["lat"])
    basis = "waypoint_junction" if point else None
    if point is None and fallback is not None:
        # no waypoint named: drawn at its event's own marker
        point, basis = fallback, DISPATCH_AT_EVENT_BASIS
    wp = row["waypoint_junction"]
    links = [urn("event", str(row["event_id"])), urn("unit", row["unit_id"])]
    if wp:
        links.append(urn("junction", int(wp)))
    target = f"J{int(wp)}" if wp else "event"
    return city_entity(
        "dispatch",
        str(row["dispatch_id"]),
        f"{agency_label(row['agency'])} {row['unit_id']} → {target}",
        point[0] if point else None,
        point[1] if point else None,
        now=now,
        state=row["status"],
        source=event_source(as_dict(row.get("event_metadata"))),
        t=row["updated_at"],
        links=links,
        props={
            "agency": row["agency"],
            "agency_label": agency_label(row["agency"]),
            "unit_id": row["unit_id"],
            "event_id": str(row["event_id"]),
            "event_title": row.get("event_title"),
            "waypoint_junction": int(wp) if wp else None,
            "requested_by": row["requested_by"],
            "decided_by": row["decided_by"],
            "approved_at": iso_z(row["approved_at"]),
            "position_basis": basis,
            "unit_emulated": True,
        },
    )


def recommendation_entity(row: Mapping[str, Any], now: datetime) -> dict:
    """One recommendation.  A stolen vehicle's R_STOLEN_SIGHTING — and a
    still-PENDING penal row of a vehicle reported stolen — says 'e-challan
    withheld' (C-N): the label, ``props.challan_withheld`` and, for the
    penal row, that a human can only reject it now (``approvable:
    false``).  A penal row decided before the vehicle was reported stolen
    keeps its decision and is flagged ``vehicle_reported_stolen``."""
    evidence = as_dict(row["evidence"])
    plate = row.get("plate") or evidence.get("plate")
    plate_masked = mask_plate(plate)
    links = [urn("vehicle", str(row["vehicle_id"]))]
    if row["sighting_id"] is not None:
        links.append(urn("sighting", str(row["sighting_id"])))
    if row.get("camera_id"):
        links.append(urn("camera", row["camera_id"]))
    segment_id = finite(evidence.get("segment_id"))
    if segment_id is not None:
        links.append(urn("segment", int(segment_id)))
    status = row["status"]
    rule_id = row["rule_id"]
    sighting_rule = rule_id == "R_STOLEN_SIGHTING"
    stolen = bool(row.get("stolen_vehicle"))
    # a PENDING penal row of a stolen car can only be rejected now; one
    # decided before the stolen-vehicle rule existed keeps its decision and
    # is flagged for review — it was not withheld, and is never relabelled
    penal_withheld = is_penal(rule_id) and stolen and status == "pending"
    withheld = sighting_rule or penal_withheld
    label = f"{rule_id} · {str(status).upper()} · {plate_masked}"
    if withheld:
        label += " · e-challan withheld"
    elif is_penal(rule_id) and stolen:
        label += " · vehicle reported stolen"
    return city_entity(
        "recommendation",
        str(row["recommendation_id"]),
        label,
        row["lon"],
        row["lat"],
        now=now,
        state=status,
        source=label_source(row.get("sighting_source") or evidence.get("source")),
        t=row["decided_at"] or row["created_at"],
        links=links,
        props={
            "rule_id": row["rule_id"],
            "status": status,
            "recommendation": row["recommendation"],
            "decided_by": row["decided_by"],
            "decided_at": iso_z(row["decided_at"]),
            "created_at": iso_z(row["created_at"]),
            "vehicle_id": str(row["vehicle_id"]),
            "sighting_id": str(row["sighting_id"]) if row["sighting_id"] else None,
            "camera_id": row.get("camera_id"),
            "plate": plate,
            "plate_masked": plate_masked,
            "evidence": {
                k: evidence.get(k)
                for k in ("speed_kmh", "speed_limit_kmh", "excess_kmh", "tolerance_kmh",
                          "road_name", "segment_id", "junction_id", "t", "fir_ref")
                if evidence.get(k) is not None
            },
            "human_decision_required": status == "pending",
            # additive (C-N): no e-challan for the victim's car — the
            # sighting rule withholds it, a penal row of a vehicle reported
            # stolen can only be REJECTED (approving it is a 409)
            "rule_kind": "evidence" if sighting_rule else "penal",
            "vehicle_reported_stolen": stolen or sighting_rule,
            "challan_withheld": withheld,
            "withheld_text": CHALLAN_WITHHELD_TEXT if withheld else None,
            "withheld_rules": [
                w.get("rule_id")
                for w in evidence.get("withheld_rules") or []
                if isinstance(w, Mapping)
            ]
            if sighting_rule
            else [],
            "approvable": status == "pending" and not penal_withheld,
        },
    )


def anomaly_quality(details: Mapping[str, Any]) -> str | None:
    """The data quality of the reading BEHIND an anomaly, lower-cased.

    signal-ingress relabels every flagged reading 'Anomalous' and keeps the
    pre-flag quality as ``feed_quality`` (C-H extension): a flag raised on
    the simulated feed is 'synthetic', never 'real'.  Older docs carry only
    ``data_quality``; None when neither says.
    """
    for key in ("feed_quality", "data_quality"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def anomaly_entity(row: Mapping[str, Any], now: datetime) -> dict:
    details = as_dict(row["details"])
    src = details.get("source")
    return city_entity(
        "anomaly",
        int(row["anomaly_id"]),
        f"{row['anomaly_type']} · {row['severity']}",
        row["lon"],
        row["lat"],
        now=now,
        state=row["severity"],
        source=label_source(src) if isinstance(src, str) else "live",
        t=row["timestamp_utc"],
        links=[urn("segment", int(row["segment_id"]))],
        props={
            "anomaly_type": row["anomaly_type"],
            "severity": row["severity"],
            "segment_id": int(row["segment_id"]),
            "road_name": row.get("road_name"),
            "count_15m": int(row["n_15m"]) if row.get("n_15m") is not None else None,
            "flagged_at": iso_z(row.get("flagged_at")),
            "anomaly_flags": details.get("anomaly_flags")
            if isinstance(details.get("anomaly_flags"), list) else None,
            # additive (C-Q): the flagged reading's own quality (synthetic
            # on the simulated feed) and the feed that produced it
            "data_quality": anomaly_quality(details),
            "feed_source": src if isinstance(src, str) else None,
        },
    )


# ---------------------------------------------------------------------------
# Snapshot assembly (memoized per request on one connection)
# ---------------------------------------------------------------------------


class SourceUnavailableError(RuntimeError):
    """A source that is not wired in this process (e.g. no Valkey client)."""


class Snapshot:
    """Per-request loader cache: every source is read at most once."""

    def __init__(self, conn: Any, *, now: datetime, valkey: Any, graph_db: Any) -> None:
        self.conn = conn
        self.now = now
        self.valkey = valkey
        self.graph_db = graph_db
        self._memo: dict[str, Any] = {}
        #: extra per-kind feed facts (camera registry, patrol feed status)
        self.feed_extra: dict[str, dict] = {}

    async def _once(self, key: str, factory: Any) -> Any:
        if key not in self._memo:
            self._memo[key] = await factory()
        return self._memo[key]

    # -- shared sources -------------------------------------------------
    async def live_events(self) -> list[Any]:
        return await self._once("events", lambda: self.conn.fetch(_SQL_LIVE_EVENTS))

    async def spines(self) -> dict[str, list[dict]]:
        """CURRENT spines per live event (superseded ones are history)."""
        return (await self._spine_sets())[0]

    async def route_history(self) -> dict[str, list[dict]]:
        """Superseded spines per live event (C-O; empty before 018)."""
        return (await self._spine_sets())[1]

    async def _spine_sets(self) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        async def load() -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
            rows = await self.live_events()
            current, history = await load_event_spines(
                self.conn, [r["event_id"] for r in rows]
            )
            return (
                {eid: spine_doc(rs) for eid, rs in current.items()},
                {eid: history_doc(rs) for eid, rs in history.items()},
            )

        return await self._once("spines", load)

    async def event_points(self) -> dict[str, tuple[float, float]]:
        async def load() -> dict[str, tuple[float, float]]:
            out: dict[str, tuple[float, float]] = {}
            for eid, spines in (await self.spines()).items():
                for s in sorted(spines, key=lambda s: s["rank"]):
                    if s["lon"] is not None:
                        out[eid] = (s["lon"], s["lat"])
                        break
            return out

        return await self._once("event_points", load)

    async def dispatch_rows(self) -> list[Any]:
        return await self._once("dispatches", lambda: self.conn.fetch(_SQL_LIVE_DISPATCHES))

    async def earmarked(self) -> list[Any]:
        return await self._once("earmarked", lambda: self.conn.fetch(_SQL_EARMARKED_VEHICLES))

    async def recent_sightings(self) -> dict[str, list[Any]]:
        async def load() -> dict[str, list[Any]]:
            ids = [r["vehicle_id"] for r in await self.earmarked()]
            out: dict[str, list[Any]] = {}
            if ids:
                for r in await self.conn.fetch(_SQL_RECENT_SIGHTINGS, ids, SIGHTINGS_PER_VEHICLE):
                    out.setdefault(str(r["vehicle_id"]), []).append(r)
            return out

        return await self._once("sightings", load)

    async def open_recommendations(self) -> list[Any]:
        return await self._once(
            "recommendations", lambda: self.conn.fetch(_SQL_OPEN_RECOMMENDATIONS)
        )

    async def convoy_docs(self) -> list[tuple[str, dict]]:
        async def load() -> list[tuple[str, dict]]:
            if self.valkey is None:
                raise SourceUnavailableError("valkey client unavailable")
            pairs, _ = await scan_mget(
                self.valkey, CONVOY_POSITION_PREFIX, max_keys=MAX_CONVOY_KEYS
            )
            return [(k, d) for k, d in ((k, json_doc(raw)) for k, raw in pairs) if d]

        return await self._once("convoys", load)

    # -- per-kind builders ----------------------------------------------
    async def build_event(self) -> list[dict]:
        rows = await self.live_events()
        spines = await self.spines()
        history = await self.route_history()
        subject_keys: list[str] = []
        for r in rows:
            subject_keys.append(str(r["event_id"]))
            if r["movement_id"] is not None:
                subject_keys.append(str(r["movement_id"]))
        bands: dict[str, Any] = {}
        if subject_keys:
            for b in await self.conn.fetch(_SQL_SUBJECT_BANDS, subject_keys):
                bands[str(b["movement_id"])] = b
        dispatch_links: dict[str, list[str]] = {}
        for d in await self.dispatch_rows():
            dispatch_links.setdefault(str(d["event_id"]), []).append(
                urn("dispatch", str(d["dispatch_id"]))
            )
        # Escort frames link to an event through their CANONICAL event_id or
        # their movement id — derived from the validated convoy entity, so a
        # link never points at a frame the snapshot itself dropped.
        convoy_links: dict[str, list[str]] = {}
        try:
            for key, doc in await self.convoy_docs():
                conv = convoy_entity(key, doc, self.now)
                if conv is None:
                    continue
                if conv["props"]["event_id"]:
                    convoy_links.setdefault(conv["props"]["event_id"], []).append(conv["urn"])
                convoy_links.setdefault(subject_key(conv["id"]), []).append(conv["urn"])
        except Exception:  # noqa: BLE001 — convoy links are optional garnish
            convoy_links = {}
        out: list[dict] = []
        for r in rows:
            eid = str(r["event_id"])
            mid = str(r["movement_id"]) if r["movement_id"] is not None else None
            band_row = bands.get(eid) or (bands.get(mid) if mid else None)
            links = dispatch_links.get(eid, []) + convoy_links.get(eid, [])
            if mid:
                links += convoy_links.get(mid, [])
            out.append(
                event_entity(
                    r,
                    spines.get(eid, []),
                    band_row,
                    self.now,
                    links=links,
                    route_history=history.get(eid, []),
                )
            )
        return out

    async def build_vehicle(self) -> list[dict]:
        sightings = await self.recent_sightings()
        rec_links: dict[str, list[str]] = {}
        for rec in await self.open_recommendations():
            rec_links.setdefault(str(rec["vehicle_id"]), []).append(
                urn("recommendation", str(rec["recommendation_id"]))
            )
        pursuit_links: dict[str, list[str]] = {}
        for ev in await self.live_events():
            vid = as_uuid(as_dict(ev["metadata"]).get("vehicle_id"))
            if vid is not None:
                pursuit_links.setdefault(str(vid), []).append(urn("event", str(ev["event_id"])))
        out: list[dict] = []
        for r in await self.earmarked():
            vid = str(r["vehicle_id"])
            reads = sightings.get(vid, [])
            hops = reads if r["state"] == "flagged" else reads[:1]
            links = [urn("sighting", str(s["sighting_id"])) for s in hops]
            links += [urn("camera", s["camera_id"]) for s in hops]
            links += rec_links.get(vid, []) + pursuit_links.get(vid, [])
            out.append(vehicle_entity(r, reads[0] if reads else None, self.now, links=links))
        return out

    async def build_sighting(self) -> list[dict]:
        sightings = await self.recent_sightings()
        out: list[dict] = []
        for r in await self.earmarked():
            if r["state"] != "flagged":
                continue  # camera hops are drawn for FLAGGED vehicles only
            for hop, s in enumerate(sightings.get(str(r["vehicle_id"]), []), start=1):
                out.append(sighting_entity(s, self.now, hop=hop))
        return out

    async def build_camera(self) -> list[dict]:
        cameras, meta = await load_cameras(self.conn, self.now)
        self.feed_extra["camera"] = {"registry": meta["registry"]}
        return [camera_entity(c, self.now) for c in cameras]

    async def build_junction(self) -> list[dict]:
        return [junction_entity(r, self.now) for r in await self.conn.fetch(_SQL_JUNCTIONS)]

    async def build_unit(self) -> list[dict]:
        units, meta = await collect_units(
            self.conn, graph_db=self.graph_db, valkey=self.valkey, now=self.now
        )
        self.feed_extra["unit"] = {
            "sources": meta["sources"],
            "patrol_feed": meta["patrol_feed"],
            "conflicts": meta["conflicts"],
        }
        return [unit_entity(u, self.now) for u in units]

    async def build_convoy(self) -> list[dict]:
        out: list[dict] = []
        for key, doc in await self.convoy_docs():
            ent = convoy_entity(key, doc, self.now)
            if ent is not None:
                out.append(ent)
        return out

    async def build_dispatch(self) -> list[dict]:
        points = await self.event_points()
        return [
            dispatch_entity(r, self.now, fallback=points.get(str(r["event_id"])))
            for r in await self.dispatch_rows()
        ]

    async def build_recommendation(self) -> list[dict]:
        return [recommendation_entity(r, self.now) for r in await self.open_recommendations()]

    async def build_anomaly(self) -> list[dict]:
        sql = (
            _SQL_RECENT_ANOMALIES_DISPLAY
            if await display_geom_supported(self.conn)
            else _SQL_RECENT_ANOMALIES
        )
        return [anomaly_entity(r, self.now) for r in await self.conn.fetch(sql)]


async def build_snapshot(
    conn: Any,
    *,
    kinds: tuple[str, ...] = ENTITY_KINDS,
    bbox: tuple[float, float, float, float] | None = None,
    now: datetime,
    valkey: Any = None,
    graph_db: Any = None,
) -> dict:
    """The fused snapshot (contract C-C): ``{as_of, entities, feeds}``."""
    started = time.perf_counter()
    snap = Snapshot(conn, now=now, valkey=valkey, graph_db=graph_db)
    entities: list[dict] = []
    feeds: dict[str, dict] = {}
    for kind in ENTITY_KINDS:
        if kind not in kinds:
            continue
        builder = getattr(snap, "build_" + kind)
        status = "ok"
        error: str | None = None
        try:
            built = await builder()
        except SourceUnavailableError as exc:
            built, status, error = [], "unavailable", str(exc)
        except Exception as exc:  # noqa: BLE001 — one dead source never blanks the city
            logger.warning("city.entities_source_failed", kind=kind, error=repr(exc))
            built, status, error = [], "error", type(exc).__name__
        located = [e for e in built if e["lon"] is not None and e["lat"] is not None]
        unlocated = len(built) - len(located)
        if bbox is not None:
            located = [e for e in located if in_bbox(e, bbox)]
        entities.extend(located)
        times = [parse_ts(e["t"]) for e in located if e["t"]]
        newest = max((t for t in times if t is not None), default=None)
        feeds[kind] = {
            "count": len(located),
            "newest_t": iso_z(newest),
            "age_sec": round(max(0.0, (now - newest).total_seconds()), 1) if newest else None,
            "unlocated": unlocated,
            "status": status,
        }
        if error:
            feeds[kind]["error"] = error
        feeds[kind].update(snap.feed_extra.get(kind, {}))
    return {
        "as_of": iso_z(now),
        "entities": entities,
        "feeds": feeds,
        "took_ms": round((time.perf_counter() - started) * 1000.0, 1),
    }


__all__ = [
    "CONVOY_POSITION_PREFIX",
    "DISPATCH_AT_EVENT_BASIS",
    "MAX_CONVOY_KEYS",
    "SIGHTINGS_PER_VEHICLE",
    "SPINE_MARKER_BASIS",
    "Snapshot",
    "SourceUnavailableError",
    "anomaly_entity",
    "anomaly_quality",
    "build_snapshot",
    "camera_entity",
    "convoy_entity",
    "dispatch_entity",
    "event_entity",
    "history_doc",
    "junction_entity",
    "load_event_spines",
    "recommendation_entity",
    "sighting_entity",
    "spine_doc",
    "unit_entity",
    "vehicle_entity",
]
