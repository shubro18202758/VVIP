"""Vehicle pursuit support — interception choke-junction ranking (010/C3).

A pursuit event is ALWAYS human-initiated (C6): an operator converts an
approved recommendation + earmark into a ``vehicle_pursuit`` corridor
event via POST /api/v1/events (metadata carries vehicle_id and
recommendation_id — the system never opens a pursuit itself).

This module adds the oracle's tactical read for such events:

    GET /api/v1/events/{event_id}/interception-plan

It ranks interception choke junctions AHEAD of the pursued vehicle's
latest (non-future) sighting using the LIVE directed graph — the same
corridor.road_graph the /api/v1/graph endpoints serve — then weights each
reachable junction by its choke class (006 junction_type).

Direction matters: the read's heading is compared with the bearing of the
segment under the read, which picks the DOWNSTREAM end the vehicle is
driving towards.  pgRouting driving distance is seeded from the segments
leaving that downstream junction (each root carrying the remaining
traversal of the sighted segment plus its turn), so the junction the
vehicle has already passed is never ranked at 0 s.  Without a usable
heading both ends are downstream candidates and the plan says so — except
on a one-way segment, whose legal direction decides.

Legality matches corridor.road_graph (migration 012) at every step, not
only inside pgRouting: a root is a LEGAL entry (a one-way segment is only
entered at its start) and a junction is reached over a one-way segment
only at that segment's end.  The raw corridor.segment_adjacency table
still lists the physical movements the directed view excludes (e.g.
Station Road 1014 -> Gandhi Road 1012 at J15, the end of one-way Gandhi
Road), so every read of it here carries the view's predicate.  The one exception is
the observed one: a read heading AGAINST a one-way segment is projected
the way the camera saw it drive and is flagged ``against_oneway``.

Lawful disclosure (C6, the city surfaces' rule): the anchoring read is the
pursued vehicle's latest read that ``city/common.py SQL_LAWFUL_READ``
allows — composed into the statement — so a vehicle whose earmark was
cleared or lapsed is anchored at most on the evidence of its
recommendations (flagged stale), never on a read the platform may no
longer disclose; with no lawful read at all the plan is a 404.

The response states the sighting's age and a ``stale`` flag (older than
STALE_SIGHTING_SEC — the vehicle may be beyond every junction listed) and
marks each junction ``likely_passed`` when its graph time is shorter than
the sighting's age.  Deterministic for a given sighting and graph: fixed
weights, fixed tie-break (junction_id ascending).

Vehicles only (C6): everything keys on the vehicle's sightings and the
road graph; no person-directed data exists on this path.
"""

from __future__ import annotations

import json
import math
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request

from traffic_oracle.city.common import SQL_LAWFUL_READ

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/events", tags=["pursuit"])

#: Choke weight by junction type (001 CHECK values) — fixed and documented.
CHOKE_WEIGHTS: dict[str, float] = {
    "toll_plaza": 1.0,   # controlled stop point — best interception choke
    "signal": 0.9,       # stop line by design
    "intersection": 0.7,
    "roundabout": 0.6,
    "flyover": 0.4,      # no stop line, but limited exits
    "underpass": 0.4,
}

#: Default driving-distance horizon from the latest sighting (seconds of
#: travel time on the graph — the same cost unit as corridor.road_graph).
DEFAULT_MAX_COST_SEC = 600.0

#: Attribution tolerance for the sighting -> nearest segment mapping.
SEGMENT_ATTRIBUTION_TOL_M = 300.0

#: A sighting older than this cannot anchor a plan with confidence: the
#: vehicle may already be past every junction the horizon reaches.
STALE_SIGHTING_SEC = 600.0

#: Heading vs segment bearing: within this many degrees of the segment's
#: direction the vehicle drives towards its END, within this many degrees
#: of the reverse towards its START; in between the direction is unknown.
DIRECTION_TOL_DEG = 75.0

#: A segment endpoint within this distance of a junction ends at it (the
#: same 25 m the 012 road_graph view judges one-way legality with).
_JUNCTION_SNAP_M = 25.0

# ── SQL (parameter-bound constants) ───────────────────────────────────────

#: The pursued vehicle's latest non-future read that is LAWFUL to disclose
#: (C6 — the city surfaces' predicate, composed; the sighting alias is s).
_SQL_LATEST_LAWFUL_SIGHTING = "SELECT s.sighting_id, s.plate, s.camera_id, s.t, ST_X(s.position) AS lon, ST_Y(s.position) AS lat, s.speed_kmh, s.heading, s.source FROM corridor.vehicle_sightings s WHERE s.vehicle_id = $1 AND s.t <= NOW() AND " + SQL_LAWFUL_READ + " ORDER BY s.t DESC LIMIT 1"  # noqa: E501

_SQL_SEED = """
WITH pt AS (SELECT ST_SetSRID(ST_MakePoint($1, $2), 4326) AS g),
seg AS (
    SELECT s.segment_id, s.geom, s.speed_limit_kmh, s.oneway,
           ST_Distance(s.geom::geography, pt.g::geography) AS dist_m,
           ST_LineLocatePoint(s.geom, pt.g) AS frac,
           ST_Length(s.geom::geography) AS len_m
    FROM corridor.road_segments s, pt
    ORDER BY s.geom <-> pt.g
    LIMIT 1
)
SELECT seg.segment_id, seg.dist_m, seg.frac, seg.len_m, seg.speed_limit_kmh,
       COALESCE(seg.oneway, FALSE) AS oneway,
       degrees(ST_Azimuth(
           ST_LineInterpolatePoint(seg.geom, GREATEST(seg.frac - 0.02, 0))::geography,
           ST_LineInterpolatePoint(seg.geom, LEAST(seg.frac + 0.02, 1))::geography
       )) AS bearing_deg,
       (SELECT j.junction_id FROM corridor.junctions j
         WHERE ST_DWithin(j.geom::geography, ST_StartPoint(seg.geom)::geography, $3)
         ORDER BY j.geom <-> ST_StartPoint(seg.geom) LIMIT 1) AS start_junction_id,
       (SELECT j.junction_id FROM corridor.junctions j
         WHERE ST_DWithin(j.geom::geography, ST_EndPoint(seg.geom)::geography, $3)
         ORDER BY j.geom <-> ST_EndPoint(seg.geom) LIMIT 1) AS end_junction_id
FROM seg
"""

# The legal movements out of the sighted segment at the downstream
# junction(s): the 012 road_graph entry rule — a one-way segment is only
# entered at its START ($3 = the 25 m endpoint snap).  The exit from the
# sighted segment itself follows the observed heading.
_SQL_ROOTS = """
SELECT sa.via_junction_id, sa.to_segment_id, sa.turn_cost_sec
FROM corridor.segment_adjacency sa
JOIN corridor.road_segments rt ON rt.segment_id = sa.to_segment_id
JOIN corridor.junctions j ON j.junction_id = sa.via_junction_id
WHERE sa.from_segment_id = $1 AND sa.via_junction_id = ANY($2::bigint[])
  AND (
        NOT COALESCE(rt.oneway, FALSE)
        OR ST_DWithin(ST_StartPoint(rt.geom)::geography, j.geom::geography, $3)
      )
ORDER BY sa.to_segment_id, sa.via_junction_id
"""

# ONE statement ranks every junction: driving distance from the roots
# (each root carries its base cost), arrival at a junction = entering a
# reachable segment + traversing it to that junction, plus the downstream
# junction(s) themselves at the remaining traversal of the sighted segment.
# A one-way segment is traversed to its END only — the 012 view's exit
# rule ($6 = the 25 m endpoint snap) — so no junction is ever reached by
# driving a one-way segment backwards.
_SQL_RANK = """
WITH roots AS (
    SELECT r.root, r.base FROM unnest($1::bigint[], $2::float8[]) AS r(root, base)
),
dd AS (
    SELECT d.start_vid, d.node, d.agg_cost
    FROM pgr_drivingDistance(
        'SELECT id, source, target, cost, reverse_cost FROM corridor.road_graph',
        $1::bigint[], $3::float8, directed := true, equicost := false
    ) d
),
reach AS (
    SELECT dd.node AS segment_id, min(roots.base + dd.agg_cost) AS enter_sec
    FROM dd JOIN roots ON roots.root = dd.start_vid
    GROUP BY dd.node
),
arrive AS (
    SELECT sa.via_junction_id AS junction_id,
           rc.enter_sec
             + ST_Length(rs.geom::geography)
               / NULLIF(rs.speed_limit_kmh * 1000.0 / 3600.0, 0) AS cost_sec
    FROM reach rc
    JOIN corridor.road_segments rs ON rs.segment_id = rc.segment_id
    JOIN corridor.segment_adjacency sa ON sa.from_segment_id = rc.segment_id
    JOIN corridor.junctions ja ON ja.junction_id = sa.via_junction_id
    WHERE NOT COALESCE(rs.oneway, FALSE)
       OR ST_DWithin(ST_EndPoint(rs.geom)::geography, ja.geom::geography, $6)
    UNION ALL
    SELECT d.junction_id, d.cost_sec
    FROM unnest($4::bigint[], $5::float8[]) AS d(junction_id, cost_sec)
),
best AS (
    SELECT junction_id, min(cost_sec) AS cost_sec
    FROM arrive GROUP BY junction_id
)
SELECT j.junction_id, j.junction_type, j.signal_control,
       to_jsonb(j) ->> 'name' AS name,
       ST_X(j.geom) AS lon, ST_Y(j.geom) AS lat, b.cost_sec
FROM best b
JOIN corridor.junctions j ON j.junction_id = b.junction_id
WHERE b.cost_sec <= $3
ORDER BY j.junction_id
"""

# The same ranking when the downstream junction offers no LEGAL onward
# movement (e.g. eastbound over the Ellis Bridge 1039 at J6, where the only
# movement would enter one-way Swami Vivekananda Road 1013 at its end):
# the downstream junction(s) themselves — the vehicle can only stop or
# turn back there.
_SQL_RANK_DOWNSTREAM_ONLY = """
SELECT j.junction_id, j.junction_type, j.signal_control,
       to_jsonb(j) ->> 'name' AS name,
       ST_X(j.geom) AS lon, ST_Y(j.geom) AS lat, d.cost_sec
FROM unnest($1::bigint[], $2::float8[]) AS d(junction_id, cost_sec)
JOIN corridor.junctions j ON j.junction_id = d.junction_id
WHERE d.cost_sec <= $3
ORDER BY j.junction_id
"""


def _angle_diff(a: float, b: float) -> float:
    d = (float(a) - float(b)) % 360.0
    return min(d, 360.0 - d)


def direction_of_travel(
    heading: float | None, segment_bearing: float | None
) -> str:
    """'end' | 'start' | 'unknown' — which end of the sighted segment the
    vehicle drives towards (pure)."""
    if heading is None or segment_bearing is None:
        return "unknown"
    try:
        h = float(heading)
        b = float(segment_bearing)
    except (TypeError, ValueError):
        return "unknown"
    if not (math.isfinite(h) and math.isfinite(b)):
        return "unknown"
    diff = _angle_diff(h, b)
    if diff <= DIRECTION_TOL_DEG:
        return "end"
    if diff >= 180.0 - DIRECTION_TOL_DEG:
        return "start"
    return "unknown"


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    return pool


@router.get("/{event_id}/interception-plan")
async def interception_plan(
    event_id: str,
    request: Request,
    max_cost_sec: float = Query(
        default=DEFAULT_MAX_COST_SEC, ge=30.0, le=1800.0
    ),
) -> dict:
    """Rank interception choke junctions for a vehicle_pursuit event.

    Steps (all parameter-bound, all read-only):
        1. load the pursuit event; 404 unless kind is 'vehicle_pursuit';
        2. read the pursued vehicle's LATEST sighting not stamped in the
           future that is lawful to disclose (metadata.vehicle_id; C6);
        3. attribute the sighting to its nearest road segment and pick the
           downstream end from the read's heading (a one-way segment's
           legal direction when there is no usable heading);
        4. the LEGAL movements leaving the downstream junction become the
           roots (a one-way segment is entered only at its start);
        5. ONE ranking statement: pgRouting driving distance over the
           directed corridor.road_graph from those roots, arrival time per
           junction (a one-way segment reaches only its end junction),
           filtered to the horizon;
        6. score = choke_weight / (1 + graph_cost_sec), rank, tie-break by
           junction_id; age + stale flags on the response.
    """
    pool = _pool(request)
    try:
        eid = _uuid.UUID(event_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "invalid event uuid") from exc

    async with pool.acquire() as conn:
        event = await conn.fetchrow(
            """
            SELECT event_id, event_kind, status, title, metadata
            FROM events.corridor_events
            WHERE event_id = $1
            """,
            eid,
        )
        if event is None:
            raise HTTPException(404, f"event {event_id} not found")
        if event["event_kind"] != "vehicle_pursuit":
            raise HTTPException(
                422,
                f"event {event_id} is a {event['event_kind']!r}, not a "
                "vehicle_pursuit — interception plans exist only for pursuits",
            )
        meta = event["metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        vehicle_id_raw = (meta or {}).get("vehicle_id")
        if not vehicle_id_raw:
            raise HTTPException(
                422,
                "pursuit event carries no metadata.vehicle_id — the pursuit "
                "initiator must name the pursued vehicle",
            )
        vehicle_id = _uuid.UUID(str(vehicle_id_raw))

        latest = await conn.fetchrow(_SQL_LATEST_LAWFUL_SIGHTING, vehicle_id)
        if latest is None:
            raise HTTPException(
                404,
                f"vehicle {vehicle_id} has no lawfully disclosable sighting to "
                "intercept (an active earmark, or a read that is enforcement "
                "evidence, is required — C6)",
            )

        seed = await conn.fetchrow(
            _SQL_SEED,
            float(latest["lon"]),
            float(latest["lat"]),
            _JUNCTION_SNAP_M,
        )
        if seed is None or float(seed["dist_m"]) > SEGMENT_ATTRIBUTION_TOL_M:
            raise HTTPException(
                422,
                "latest sighting is not attributable to any road segment "
                f"within {SEGMENT_ATTRIBUTION_TOL_M:.0f} m",
            )
        seed_segment = int(seed["segment_id"])
        bearing = (
            float(seed["bearing_deg"]) if seed["bearing_deg"] is not None else None
        )
        frac = float(seed["frac"])
        speed_ms = max(float(seed["speed_limit_kmh"] or 0.0), 1.0) / 3.6
        remaining = {
            "end": (1.0 - frac) * float(seed["len_m"]) / speed_ms,
            "start": frac * float(seed["len_m"]) / speed_ms,
        }
        ends = {
            "end": seed["end_junction_id"],
            "start": seed["start_junction_id"],
        }
        oneway = bool(seed["oneway"])
        direction = direction_of_travel(latest["heading"], bearing)
        basis = "heading"
        if direction == "unknown" and oneway and ends["end"] is not None:
            # no usable heading on a one-way segment: its legal direction
            basis = "oneway"
            towards = ["end"]
        elif direction == "unknown" or ends[direction] is None:
            # no usable heading, or the vehicle drives towards a dead end:
            # it can only leave through a junction, so both ends stand
            basis = "unknown" if direction == "unknown" else "dead_end"
            towards = [e for e in ("end", "start") if ends[e] is not None]
        else:
            towards = [direction]
        # observed driving against a one-way segment: projected as seen
        against_oneway = oneway and towards == ["start"]
        downstream = [(int(ends[e]), remaining[e]) for e in towards]
        down_ids = [jid for jid, _ in downstream]
        down_cost = {jid: rem for jid, rem in downstream}

        root_rows = await conn.fetch(
            _SQL_ROOTS, seed_segment, down_ids, _JUNCTION_SNAP_M
        )
        roots: dict[int, float] = {}
        for rr in root_rows:
            base = down_cost[int(rr["via_junction_id"])] + float(rr["turn_cost_sec"] or 0.0)
            seg_id = int(rr["to_segment_id"])
            if seg_id not in roots or base < roots[seg_id]:
                roots[seg_id] = base
        root_ids = sorted(roots)

        if root_ids:
            jrows = await conn.fetch(
                _SQL_RANK,
                root_ids,
                [roots[r] for r in root_ids],
                float(max_cost_sec),
                down_ids,
                [down_cost[j] for j in down_ids],
                _JUNCTION_SNAP_M,
            )
        else:
            jrows = await conn.fetch(
                _SQL_RANK_DOWNSTREAM_ONLY,
                down_ids,
                [down_cost[j] for j in down_ids],
                float(max_cost_sec),
            )

    now = datetime.now(timezone.utc)
    age_sec = max((now - latest["t"]).total_seconds(), 0.0)
    stale = age_sec > STALE_SIGHTING_SEC

    junctions = []
    for j in jrows:
        cost = float(j["cost_sec"])
        weight = CHOKE_WEIGHTS.get(j["junction_type"], 0.5)
        score = weight / (1.0 + cost)
        junctions.append(
            {
                "junction_id": int(j["junction_id"]),
                "name": j["name"],
                "junction_type": j["junction_type"],
                "signal_control": bool(j["signal_control"]),
                "position": [
                    round(float(j["lon"]), 6),
                    round(float(j["lat"]), 6),
                ],
                "graph_cost_sec": round(cost, 1),
                "downstream": int(j["junction_id"]) in down_cost,
                # the vehicle would already have reached it by now
                "likely_passed": cost < age_sec,
                "choke_weight": weight,
                "score": round(score, 6),
            }
        )

    junctions.sort(key=lambda j: (-j["score"], j["junction_id"]))

    return {
        "event_id": event_id,
        "vehicle_id": str(vehicle_id),
        "latest_sighting": {
            "sighting_id": str(latest["sighting_id"]),
            "plate": latest["plate"],
            "camera_id": latest["camera_id"],
            "t": latest["t"].isoformat(),
            "position": [
                round(float(latest["lon"]), 6),
                round(float(latest["lat"]), 6),
            ],
            "speed_kmh": latest["speed_kmh"],
            "heading": latest["heading"],
            "source": latest["source"],  # 'emulated_anpr' for the emulator
        },
        "sighting_age_sec": round(age_sec, 1),
        "stale": stale,
        "stale_after_sec": STALE_SIGHTING_SEC,
        "seed_segment_id": seed_segment,
        "direction_of_travel": {
            "basis": basis,
            "towards_junction_ids": down_ids,
            "segment_bearing_deg": (
                round(bearing, 1) if bearing is not None else None
            ),
            "position_along_segment": round(frac, 4),
            "seed_segment_oneway": oneway,
            "against_oneway": against_oneway,
            # the legal onward movements the plan starts from; empty when
            # the downstream junction offers none (only it is ranked)
            "onward_segment_ids": root_ids,
        },
        "max_cost_sec": max_cost_sec,
        "interception_junctions": junctions[:10],
        "method": (
            "pgr_drivingDistance over the directed corridor.road_graph, "
            "seeded from the LEGAL movements leaving the downstream junction "
            "the sighting's heading points to (each root carrying the "
            "remaining traversal of the sighted segment plus its turn); "
            "one-way segments are entered only at their start and left only "
            "at their end, as in the graph; junctions scored by "
            "choke_weight / (1 + graph_cost_sec), tie-break junction_id"
        ),
        "note": (
            "pursuits are human-initiated (C6); this plan assists the "
            "operator, it never dispatches on its own"
            + (
                " — the latest sighting is STALE: the vehicle may be beyond "
                "every junction listed"
                if stale
                else ""
            )
            + (
                f" — the read shows the vehicle driving AGAINST one-way "
                f"segment {seed_segment}; it is projected as observed"
                if against_oneway
                else ""
            )
        ),
    }


__all__ = [
    "router",
    "CHOKE_WEIGHTS",
    "DEFAULT_MAX_COST_SEC",
    "STALE_SIGHTING_SEC",
    "direction_of_travel",
]
