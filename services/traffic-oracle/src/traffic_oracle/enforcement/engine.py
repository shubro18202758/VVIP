"""E-challan evaluation pipeline — context assembly + PENDING persistence.

One pipeline, two callers:
    * POST /api/v1/enforcement/evaluate {sighting_id}   (operator / replay)
    * POST /api/v1/vehicles/sightings                    (the ANPR ingest
      seam, vehicles/sighting_processor.py — every accepted read is
      evaluated as it lands, so R7 is computed live, not by a script)

CONTEXT ASSEMBLY (deterministic, read-only, parameter-bound):
    * the sighting + its vehicle's ACTIVE earmark (expiry enforced in the
      query path): its state AND its lawful basis — an earmark resting on
      a stolen-vehicle FIR switches the rules to the R_STOLEN_SIGHTING
      evidence branch (no e-challan for the victim's car, contract C-N);
    * the segment under the read position (nearest road segment within
      SEGMENT_ATTRIBUTION_TOL_M — honest None when none matches);
    * the signal program of the nearest signalized junction within
      JUNCTION_ATTRIBUTION_TOL_M (a stop-line distance, not a district)
      plus that junction's approach legs (bearings away from the junction)
      so the rules can map the read's heading onto the movement the
      program actually defines — and decide only when the read's own
      segment IS that approach leg (rules.read_approach);
    * active closure windows of corridor events whose CURRENT event_routes
      cover the read's segment (parameter-bound window + coverage test; a
      spine superseded by a re-route — migration 018 — covers nothing).

TIME INTEGRITY: a sighting stamped more than FUTURE_TOLERANCE_SEC after
the oracle's clock is refused (:class:`FutureSightingError` -> HTTP 409) —
no recommendation may ever predate its own violation; the decision
endpoint allows no skew at all (``check_not_future(..., tolerance_sec=0)``).

PERSISTENCE: fired rules land as PENDING rows only (idempotent on
(sighting_id, rule_id)); the decision endpoint is still the ONLY path to
approved/rejected and it requires a human officer (C6).  Under a stolen-
vehicle earmark exactly one R_STOLEN_SIGHTING row lands per read and the
outcome's context says ``challan_withheld: true``.

Import discipline: this module depends only on ``rules`` (pure) and the
import-light ``city.common`` probes, so both the enforcement router and
the vehicles package can import it without an import cycle.
"""

from __future__ import annotations

import json
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

import structlog

from traffic_oracle.city.common import routes_supersede_supported
from traffic_oracle.enforcement import rules

logger = structlog.get_logger(__name__)

__all__ = [
    "SEGMENT_ATTRIBUTION_TOL_M",
    "JUNCTION_ATTRIBUTION_TOL_M",
    "LEG_SNAP_TOL_M",
    "FUTURE_TOLERANCE_SEC",
    "SQL_STOLEN_EARMARK",
    "ActiveEarmark",
    "FutureSightingError",
    "EvaluationOutcome",
    "load_sighting",
    "parse_phases",
    "check_not_future",
    "evaluate_sighting",
]

#: How far (meters) a read position may sit from a road segment to be
#: attributed to that segment for the speed rule.
SEGMENT_ATTRIBUTION_TOL_M = 300.0

#: How far (meters) a signalized junction may sit from the read to govern
#: it — a stop-line camera distance.  (It was 400 m: a district, which let
#: reads a block away inherit a junction's red.)
JUNCTION_ATTRIBUTION_TOL_M = 40.0

#: A segment endpoint within this distance of a junction is one of the
#: junction's legs (the same 25 m snap the seed topology uses).
LEG_SNAP_TOL_M = 25.0

#: Clock-skew allowance: a sighting may be stamped at most this far after
#: the oracle's NOW() (camera/NTP drift); beyond it the read is refused.
FUTURE_TOLERANCE_SEC = 30.0

#: SQL predicate over an ``events.enforcement_recommendations`` row aliased
#: ``r``: its vehicle holds an ACTIVE stolen-vehicle earmark (expiry
#: enforced in the query path).  The decision endpoint composes it to
#: refuse approving an e-challan of the victim's car (C-N), the city
#: surfaces to label one withheld.
SQL_STOLEN_EARMARK = "EXISTS (SELECT 1 FROM corridor.vehicle_earmarks ve WHERE ve.vehicle_id = r.vehicle_id AND ve.cleared_at IS NULL AND ve.retention_until > NOW() AND ve.lawful_basis = 'stolen_vehicle_fir')"  # noqa: E501


class ActiveEarmark(NamedTuple):
    """The vehicle's ACTIVE earmark as the rules see it (all None: none).

    A vehicle can hold several active earmarks; a stolen-vehicle FIR one
    wins (``lawful_basis`` then says so) — a later 'watch' earmark must
    never switch the e-challan rules back on for the victim's car.
    """

    state: str | None = None
    lawful_basis: str | None = None
    fir_ref: str | None = None

    @property
    def stolen(self) -> bool:
        return self.lawful_basis == rules.STOLEN_VEHICLE_BASIS


class FutureSightingError(Exception):
    """The sighting's timestamp lies in the future beyond the tolerance."""

    def __init__(self, t: datetime, now: datetime,
                 tolerance_sec: float = FUTURE_TOLERANCE_SEC) -> None:
        self.t = t
        self.now = now
        self.tolerance_sec = tolerance_sec
        super().__init__(
            f"sighting t={t.isoformat()} is "
            f"{(t - now).total_seconds():.0f} s after the oracle clock "
            f"({now.isoformat()}); "
            + (
                f"reads more than {tolerance_sec:.0f} s in the future are "
                "refused — a recommendation can never predate its violation"
                if tolerance_sec > 0
                else "nothing may be decided before the violation it records "
                f"— retry after {t.isoformat()}"
            )
        )


@dataclass
class EvaluationOutcome:
    """What one evaluation fired and persisted."""

    sighting_id: str
    vehicle_id: Any
    fired: tuple[rules.Recommendation, ...] = ()
    created: list[dict] = field(default_factory=list)
    context: dict = field(default_factory=dict)


def check_not_future(t: datetime, now: datetime,
                     tolerance_sec: float = FUTURE_TOLERANCE_SEC) -> None:
    """Raise FutureSightingError when ``t`` > now + ``tolerance_sec``.

    Ingest and evaluation allow FUTURE_TOLERANCE_SEC of camera clock skew;
    a human DECISION allows none (tolerance 0), so the audit trail can
    never show an approval stamped before the violation it approves.
    """
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    if t > now + timedelta(seconds=tolerance_sec):
        raise FutureSightingError(t, now, tolerance_sec)


def _audit_entry(actor: str, action: str, at: datetime, note: str = "") -> dict:
    return {"actor": actor, "action": action, "at": at.isoformat(), "note": note}


def parse_phases(raw: Any) -> tuple[tuple[int, tuple[str, ...]], ...]:
    """006 JSONB phases -> the rules module's frozen tuple-of-tuples."""
    phases_raw = json.loads(raw) if isinstance(raw, str) else raw
    phases: list[tuple[int, tuple[str, ...]]] = []
    for ph in phases_raw or []:
        green = int(ph.get("green_sec", 0))
        movements = tuple(str(m) for m in ph.get("movements", []))
        phases.append((green, movements))
    return tuple(phases)


# ────────────────────────────────────────────────────────────────────────────
# Context assembly (read-only, parameter-bound)
# ────────────────────────────────────────────────────────────────────────────


async def load_sighting(conn: Any, sighting_uuid: _uuid.UUID) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT s.sighting_id, s.vehicle_id, s.plate, s.camera_id, s.t,
               ST_X(s.position) AS lon, ST_Y(s.position) AS lat,
               s.speed_kmh, s.heading, s.vehicle_class, s.source,
               s.matched_plate_hit
        FROM corridor.vehicle_sightings s
        WHERE s.sighting_id = $1
        """,
        sighting_uuid,
    )
    return dict(row) if row else None


async def _active_earmark_state(conn: Any, vehicle_id) -> ActiveEarmark:
    """(state, lawful_basis[, fir_ref]) of the vehicle's ACTIVE earmark.

    Expiry is enforced in the query path.  A stolen-vehicle FIR earmark
    outranks any other active earmark, newest first otherwise; the FIR
    reference is extracted from its reason (rules.fir_reference — only the
    reference, never the officer's free text).
    """
    row = await conn.fetchrow(
        """
        SELECT state, lawful_basis, reason FROM corridor.vehicle_earmarks
        WHERE vehicle_id = $1 AND cleared_at IS NULL
          AND retention_until > NOW()
        ORDER BY (lawful_basis = 'stolen_vehicle_fir') DESC, earmarked_at DESC
        LIMIT 1
        """,
        vehicle_id,
    )
    if not row:
        return ActiveEarmark()
    basis = row["lawful_basis"]
    return ActiveEarmark(
        state=row["state"],
        lawful_basis=basis,
        fir_ref=(
            rules.fir_reference(row["reason"])
            if basis == rules.STOLEN_VEHICLE_BASIS
            else None
        ),
    )


async def _nearest_segment(conn: Any, lon: float, lat: float,
                           tol_m: float) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT segment_id, road_name, speed_limit_kmh,
               ST_Distance(geom::geography,
                           ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography)
                   AS dist_m
        FROM corridor.road_segments
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint($1, $2), 4326)
        LIMIT 1
        """,
        lon,
        lat,
    )
    if row is None or float(row["dist_m"]) > tol_m:
        return None
    return dict(row)


async def _nearest_signal(conn: Any, lon: float, lat: float,
                          tol_m: float) -> tuple[int, dict, float] | None:
    """(junction_id, program row, distance) of the nearest programmed junction."""
    row = await conn.fetchrow(
        """
        SELECT j.junction_id, p.cycle_sec, p.phases, p.offset_sec,
               ST_Distance(j.geom::geography,
                           ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography)
                   AS dist_m
        FROM corridor.junctions j
        JOIN corridor.signal_programs p ON p.junction_id = j.junction_id
        ORDER BY j.geom <-> ST_SetSRID(ST_MakePoint($1, $2), 4326)
        LIMIT 1
        """,
        lon,
        lat,
    )
    if row is None or float(row["dist_m"]) > tol_m:
        return None
    return int(row["junction_id"]), dict(row), float(row["dist_m"])


async def _junction_legs(conn: Any, junction_id: int) -> tuple[rules.JunctionLeg, ...]:
    """Every segment with an endpoint at the junction, with the bearing of
    the leg pointing AWAY from the junction (geodesic azimuth of the first
    vertex pair from the junction end)."""
    rows = await conn.fetch(
        """
        SELECT s.segment_id, s.road_class, s.road_name,
               degrees(CASE
                   WHEN ST_Distance(ST_StartPoint(s.geom)::geography, j.geom::geography)
                        <= ST_Distance(ST_EndPoint(s.geom)::geography, j.geom::geography)
                   THEN ST_Azimuth(ST_PointN(s.geom, 1)::geography,
                                   ST_PointN(s.geom, 2)::geography)
                   ELSE ST_Azimuth(ST_PointN(s.geom, ST_NPoints(s.geom))::geography,
                                   ST_PointN(s.geom, ST_NPoints(s.geom) - 1)::geography)
               END) AS bearing_deg
        FROM corridor.junctions j
        JOIN corridor.road_segments s
          ON s.geom && ST_Expand(j.geom, 0.001)
         AND (ST_DWithin(ST_StartPoint(s.geom)::geography, j.geom::geography, $2)
              OR ST_DWithin(ST_EndPoint(s.geom)::geography, j.geom::geography, $2))
        WHERE j.junction_id = $1
        ORDER BY s.segment_id
        """,
        junction_id,
        LEG_SNAP_TOL_M,
    )
    return tuple(
        rules.JunctionLeg(
            segment_id=int(r["segment_id"]),
            road_class=str(r["road_class"]),
            bearing_deg=round(float(r["bearing_deg"]), 2),
            road_name=r["road_name"],
        )
        for r in rows
        if r["bearing_deg"] is not None
    )


#: Active corridor events whose route covers a segment at an instant — the
#: pre-018 statement, and the 018 one that skips superseded spines (a
#: re-routed event no longer closes its old route).  DISTINCT: a segment
#: on two current spines of one event is one closure.
_SQL_COVERING_CLOSURES = "SELECT DISTINCT e.event_id, e.title, e.event_kind, p.window_start, p.window_end FROM events.corridor_events e JOIN events.event_routes r ON r.event_id = e.event_id JOIN events.event_phases p ON p.event_id = e.event_id WHERE e.status = 'active' AND $1 = ANY(r.segment_ids) AND p.window_start <= $2 AND p.window_end >= $2 ORDER BY e.event_id, p.window_start"  # noqa: E501

_SQL_COVERING_CLOSURES_CURRENT = "SELECT DISTINCT e.event_id, e.title, e.event_kind, p.window_start, p.window_end FROM events.corridor_events e JOIN events.event_routes r ON r.event_id = e.event_id JOIN events.event_phases p ON p.event_id = e.event_id WHERE e.status = 'active' AND $1 = ANY(r.segment_ids) AND r.superseded_at IS NULL AND p.window_start <= $2 AND p.window_end >= $2 ORDER BY e.event_id, p.window_start"  # noqa: E501


async def _covering_closures(conn: Any, segment_id: int, t: datetime) -> list[dict]:
    """Active corridor events whose CURRENT route covers ``segment_id`` at
    time ``t`` (superseded spines skipped once migration 018 is in)."""
    sql = (
        _SQL_COVERING_CLOSURES_CURRENT
        if await routes_supersede_supported(conn)
        else _SQL_COVERING_CLOSURES
    )
    rows = await conn.fetch(sql, segment_id, t)
    return [dict(r) for r in rows]


# ────────────────────────────────────────────────────────────────────────────
# Evaluation
# ────────────────────────────────────────────────────────────────────────────


async def evaluate_sighting(
    conn: Any,
    sighting_uuid: _uuid.UUID,
    *,
    now: datetime | None = None,
    srow: dict | None = None,
) -> EvaluationOutcome | None:
    """Assemble context, run the pure rules, persist fired rules PENDING.

    Returns None when the sighting does not exist.  Raises
    :class:`FutureSightingError` for a sighting stamped beyond the clock
    tolerance — checked BEFORE any rule runs.  Idempotent: re-evaluating
    a sighting creates nothing new (ON CONFLICT (sighting_id, rule_id)).

    Under an ACTIVE stolen-vehicle earmark the rules return ONE
    R_STOLEN_SIGHTING (the read + the penal rules it would have fired,
    withheld) and that is all that is persisted — the context says
    ``challan_withheld: true`` and names the withheld rule ids.
    """
    now = now or datetime.now(timezone.utc)
    if srow is None:
        srow = await load_sighting(conn, sighting_uuid)
    if srow is None:
        return None
    check_not_future(srow["t"], now)

    lon, lat = float(srow["lon"]), float(srow["lat"])
    earmark = await _active_earmark_state(conn, srow["vehicle_id"])
    stolen = (
        rules.StolenVehicleContext(fir_ref=earmark.fir_ref, earmark_state=earmark.state)
        if earmark.stolen
        else None
    )
    seg = await _nearest_segment(conn, lon, lat, SEGMENT_ATTRIBUTION_TOL_M)
    sig = await _nearest_signal(conn, lon, lat, JUNCTION_ATTRIBUTION_TOL_M)
    legs: tuple[rules.JunctionLeg, ...] = ()
    if sig is not None:
        legs = await _junction_legs(conn, sig[0])

    closures: tuple[rules.ClosureContext, ...] = ()
    if seg is not None:
        covering = await _covering_closures(conn, int(seg["segment_id"]), srow["t"])
        closures = tuple(
            rules.ClosureContext(
                event_id=str(c["event_id"]),
                title=c["title"],
                event_kind=c["event_kind"],
                window_start=c["window_start"],
                window_end=c["window_end"],
                covers_position=True,  # route covered the read's segment
            )
            for c in covering
        )

    sighting = rules.SightingContext(
        sighting_id=str(srow["sighting_id"]),
        plate=srow["plate"],
        camera_id=srow["camera_id"],
        t=srow["t"],
        lon=lon,
        lat=lat,
        source=srow["source"],
        speed_kmh=srow["speed_kmh"],
        heading=srow["heading"],
        matched_plate_hit=srow["matched_plate_hit"],
        earmark_state=earmark.state,
    )
    segment_ctx = (
        rules.SegmentContext(
            segment_id=int(seg["segment_id"]),
            speed_limit_kmh=(
                float(seg["speed_limit_kmh"])
                if seg["speed_limit_kmh"] is not None
                else None
            ),
            road_name=seg["road_name"],
        )
        if seg is not None
        else None
    )
    signal_ctx = (
        rules.SignalContext(
            junction_id=sig[0],
            cycle_sec=int(sig[1]["cycle_sec"]),
            phases=parse_phases(sig[1]["phases"]),
            offset_sec=int(sig[1]["offset_sec"] or 0),
            legs=legs,
        )
        if sig is not None
        else None
    )

    fired = rules.evaluate_rules(
        sighting, segment_ctx, signal_ctx, closures, stolen=stolen
    )

    created: list[dict] = []
    for rec in fired:
        note = (
            "deterministic rule evaluation — e-challan withheld, vehicle reported stolen"
            if rec.rule_id == "R_STOLEN_SIGHTING"
            else "deterministic rule evaluation"
        )
        audit = [_audit_entry("traffic-oracle", "created:pending", now, note)]
        row = await conn.fetchrow(
            """
            INSERT INTO events.enforcement_recommendations
                (sighting_id, vehicle_id, rule_id, recommendation,
                 evidence, status, audit_tail)
            VALUES ($1, $2, $3, $4, $5::jsonb, 'pending', $6::jsonb)
            ON CONFLICT (sighting_id, rule_id) DO NOTHING
            RETURNING recommendation_id, created_at
            """,
            sighting_uuid,
            srow["vehicle_id"],
            rec.rule_id,
            rec.recommendation,
            json.dumps(rec.evidence, sort_keys=True),
            json.dumps(audit),
        )
        if row is not None:
            created.append(
                {
                    "recommendation_id": str(row["recommendation_id"]),
                    "rule_id": rec.rule_id,
                    "recommendation": rec.recommendation,
                    "evidence": rec.evidence,
                    "status": "pending",
                    "created_at": (
                        row["created_at"].isoformat()
                        if row["created_at"] is not None
                        else None
                    ),
                }
            )

    approach = (
        rules.read_approach(
            sighting.heading,
            segment_ctx.segment_id if segment_ctx is not None else None,
            signal_ctx,
        )
        if signal_ctx is not None
        else None
    )
    context = {
        "segment_id": int(seg["segment_id"]) if seg is not None else None,
        "junction_id": sig[0] if sig is not None else None,
        "junction_dist_m": round(sig[2], 1) if sig is not None else None,
        # None: no signal decision (no approach, or the read is not ON it)
        "movement": approach.movement if approach is not None else None,
        "approach_segment_id": (
            approach.approach_segment_id if approach is not None else None
        ),
        "earmark_state": earmark.state,
        "closures": len(closures),
        # additive (C-N): the earmark's lawful basis, and — for a vehicle
        # reported stolen — that no e-challan was proposed and which penal
        # rules the read would have fired
        "lawful_basis": earmark.lawful_basis,
        "challan_withheld": stolen is not None,
        "withheld_rules": (
            [w["rule_id"] for w in fired[0].evidence.get("withheld_rules", [])]
            if stolen is not None and fired
            else []
        ),
    }
    logger.info(
        "enforcement.evaluated",
        sighting_id=str(sighting_uuid),
        fired=len(fired),
        created=len(created),
        junction_id=context["junction_id"],
        movement=context["movement"],
        challan_withheld=context["challan_withheld"],
    )
    return EvaluationOutcome(
        sighting_id=str(sighting_uuid),
        vehicle_id=srow["vehicle_id"],
        fired=fired,
        created=created,
        context=context,
    )
