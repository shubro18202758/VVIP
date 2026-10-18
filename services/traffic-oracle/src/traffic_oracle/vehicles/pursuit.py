"""Vehicle pursuit support — interception choke-junction ranking (010/C3).

A pursuit event is ALWAYS human-initiated (C6): an operator converts an
approved recommendation + earmark into a ``vehicle_pursuit`` corridor
event via POST /api/v1/events (metadata carries vehicle_id and
recommendation_id — the system never opens a pursuit itself).

This module adds the oracle's tactical read for such events:

    GET /api/v1/events/{event_id}/interception-plan

It ranks interception choke junctions ahead of the pursued vehicle's
LATEST sighting using the LIVE graph (pgRouting driving-distance from the
sighting's nearest segment — the same corridor.road_graph the
/api/v1/graph endpoints serve), then weights each reachable junction by
its choke class (006 junction_type).  Deterministic end to end: fixed
weights, fixed tie-break (junction_id ascending), no clock beyond the
explicit ``max_cost_sec`` horizon.

Vehicles only (C6): everything keys on the vehicle's sightings and the
road graph; no person-directed data exists on this path.
"""

from __future__ import annotations

import json
import uuid as _uuid
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request

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
        2. read the pursued vehicle's LATEST sighting (metadata.vehicle_id);
        3. attribute the sighting to its nearest road segment;
        4. one-to-many pgRouting driving distance over corridor.road_graph
           (the live graph the /api/v1/graph endpoints serve);
        5. join reachable segments to junctions via segment_adjacency,
           score = choke_weight / (1 + agg_cost_sec), rank, tie-break by
           junction_id.
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

        latest = await conn.fetchrow(
            """
            SELECT sighting_id, plate, camera_id, t,
                   ST_X(position) AS lon, ST_Y(position) AS lat,
                   speed_kmh, heading, source
            FROM corridor.vehicle_sightings
            WHERE vehicle_id = $1
            ORDER BY t DESC LIMIT 1
            """,
            vehicle_id,
        )
        if latest is None:
            raise HTTPException(
                404, f"vehicle {vehicle_id} has no sightings to intercept"
            )

        seed = await conn.fetchrow(
            """
            SELECT segment_id,
                   ST_Distance(geom, ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography)
                       AS dist_m
            FROM corridor.road_segments
            ORDER BY geom <-> ST_SetSRID(ST_MakePoint($1, $2), 4326)
            LIMIT 1
            """,
            float(latest["lon"]),
            float(latest["lat"]),
        )
        if seed is None or float(seed["dist_m"]) > SEGMENT_ATTRIBUTION_TOL_M:
            raise HTTPException(
                422,
                "latest sighting is not attributable to any road segment "
                f"within {SEGMENT_ATTRIBUTION_TOL_M:.0f} m",
            )
        seed_segment = int(seed["segment_id"])

        reach = await conn.fetch(
            """
            SELECT d.node AS segment_id, d.agg_cost
            FROM pgr_drivingDistance(
                'SELECT id, source, target, cost, reverse_cost FROM corridor.road_graph',
                $1::bigint, $2::float, directed := true
            ) d
            ORDER BY d.agg_cost
            """,
            seed_segment,
            float(max_cost_sec),
        )
        reachable_ids = [int(r["segment_id"]) for r in reach]
        cost_by_seg = {int(r["segment_id"]): float(r["agg_cost"]) for r in reach}

        junctions = []
        if reachable_ids:
            jrows = await conn.fetch(
                """
                SELECT DISTINCT j.junction_id, j.junction_type, j.signal_control,
                       ST_X(j.geom) AS lon, ST_Y(j.geom) AS lat
                FROM corridor.junctions j
                JOIN corridor.segment_adjacency sa
                  ON sa.via_junction_id = j.junction_id
                WHERE sa.from_segment_id = ANY($1)
                   OR sa.to_segment_id = ANY($1)
                ORDER BY j.junction_id
                """,
                reachable_ids,
            )
            for j in jrows:
                # nearest reachable segment cost for this junction
                best = None
                for sa in await conn.fetch(
                    """
                    SELECT from_segment_id, to_segment_id
                    FROM corridor.segment_adjacency
                    WHERE via_junction_id = $1
                    """,
                    int(j["junction_id"]),
                ):
                    for sid in (int(sa["from_segment_id"]), int(sa["to_segment_id"])):
                        c = cost_by_seg.get(sid)
                        if c is not None and (best is None or c < best):
                            best = c
                if best is None:
                    continue
                weight = CHOKE_WEIGHTS.get(j["junction_type"], 0.5)
                score = weight / (1.0 + best)
                junctions.append(
                    {
                        "junction_id": int(j["junction_id"]),
                        "junction_type": j["junction_type"],
                        "signal_control": bool(j["signal_control"]),
                        "position": [
                            round(float(j["lon"]), 6),
                            round(float(j["lat"]), 6),
                        ],
                        "graph_cost_sec": round(best, 1),
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
            "t": latest["t"].isoformat(),
            "position": [
                round(float(latest["lon"]), 6),
                round(float(latest["lat"]), 6),
            ],
            "speed_kmh": latest["speed_kmh"],
            "heading": latest["heading"],
            "source": latest["source"],  # 'emulated_anpr' this build
        },
        "seed_segment_id": seed_segment,
        "max_cost_sec": max_cost_sec,
        "interception_junctions": junctions[:10],
        "method": (
            "pgr_drivingDistance over corridor.road_graph from the latest "
            "sighting's nearest segment, junctions scored by "
            "choke_weight / (1 + graph_cost_sec), tie-break junction_id"
        ),
        "note": (
            "pursuits are human-initiated (C6); this plan assists the "
            "operator, it never dispatches on its own"
        ),
    }


__all__ = ["router", "CHOKE_WEIGHTS", "DEFAULT_MAX_COST_SEC"]
