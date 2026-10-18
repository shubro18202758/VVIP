"""Enforcement API — e-challan recommendations + human approval (010).

HTTP surface (all SQL parameter-bound; the approval flow is the audit
chain):
    GET  /api/v1/enforcement/recommendations?status=pending|approved|rejected
         ?status=approved is the ONLY place approved challans are exposed.
    POST /api/v1/enforcement/recommendations/{id}/decision
         {decision: approve|reject, officer} — the human approval flow.
    POST /api/v1/enforcement/evaluate {sighting_id}
         run the deterministic rules for one persisted sighting and persist
         the fired recommendations as PENDING rows (idempotent on
         (sighting_id, rule_id)).

APPROVAL MODEL (C6, hard):
    * rows are created 'pending' ONLY — there is no code path that creates
      an approved row;
    * the ONLY transition to approved/rejected is the decision endpoint,
      which requires a human ``officer`` identity and appends an audit
      entry to the row's audit_tail (append-only JSONB);
    * every state change is audited: creation, decision, with actor and
      timestamp — the audit trail the mandate requires.

CONTEXT ASSEMBLY for /evaluate (deterministic, read-only):
    * the sighting + its vehicle's ACTIVE earmark (expiry enforced in the
      query path);
    * the segment under the read position (nearest road segment centroid
      within tolerance — parameter-bound, honest None when none matches);
    * the signal program of the nearest signalized junction (006 table;
      honest None when the fleet has none — R_SIGNAL_VIOLATION then does
      not fire);
    * active closure windows of corridor events whose event_routes cover
      the read's segment (parameter-bound window + coverage test).
"""

from __future__ import annotations

import json
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from traffic_oracle.enforcement import rules
from traffic_oracle.vehicles.sources import SOURCE_EMULATED

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/enforcement", tags=["enforcement"])

#: How far (meters) a read position may sit from a segment centroid to be
#: attributed to that segment for the speed rule.
SEGMENT_ATTRIBUTION_TOL_M = 300.0

#: How far (meters) a junction may sit from the read to govern it.
JUNCTION_ATTRIBUTION_TOL_M = 400.0

_STATUSES = ("pending", "approved", "rejected")


class DecisionRequest(BaseModel):
    decision: str = Field(description="approve | reject")
    officer: str = Field(
        min_length=2,
        max_length=120,
        description="human officer identity — mandatory (C6: no auto-approval)",
    )
    note: str = Field(default="", max_length=500)

    @field_validator("decision")
    @classmethod
    def _valid_decision(cls, v: str) -> str:
        if v not in ("approve", "reject"):
            raise ValueError("decision must be 'approve' or 'reject'")
        return v


class EvaluateRequest(BaseModel):
    sighting_id: str = Field(description="uuid of a persisted sighting")

    @field_validator("sighting_id")
    @classmethod
    def _valid_uuid(cls, v: str) -> str:
        try:
            _uuid.UUID(v)
        except (ValueError, TypeError) as exc:
            raise ValueError("sighting_id must be a uuid") from exc
        return v


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    return pool


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _audit_entry(actor: str, action: str, at: datetime, note: str = "") -> dict:
    return {"actor": actor, "action": action, "at": at.isoformat(), "note": note}


# ────────────────────────────────────────────────────────────────────────────
# Context assembly (read-only, parameter-bound)
# ────────────────────────────────────────────────────────────────────────────


async def _load_sighting(conn: Any, sighting_uuid: _uuid.UUID) -> dict | None:
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


async def _active_earmark_state(conn: Any, vehicle_id) -> str | None:
    row = await conn.fetchrow(
        """
        SELECT state FROM corridor.vehicle_earmarks
        WHERE vehicle_id = $1 AND cleared_at IS NULL
          AND retention_until > NOW()
        ORDER BY earmarked_at DESC LIMIT 1
        """,
        vehicle_id,
    )
    return row["state"] if row else None


async def _nearest_segment(conn: Any, lon: float, lat: float,
                           tol_m: float) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT segment_id, road_name, speed_limit_kmh,
               ST_Distance(geom, ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography)
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
                          tol_m: float) -> tuple[int, dict] | None:
    """(junction_id, program row) of the nearest signalized junction."""
    row = await conn.fetchrow(
        """
        SELECT j.junction_id, p.cycle_sec, p.phases, p.offset_sec,
               ST_Distance(j.geom, ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography)
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
    return int(row["junction_id"]), dict(row)


async def _covering_closures(conn: Any, segment_id: int, t: datetime) -> list[dict]:
    """Active corridor events whose route covers ``segment_id`` at time ``t``."""
    rows = await conn.fetch(
        """
        SELECT e.event_id, e.title, e.event_kind, p.window_start, p.window_end
        FROM events.corridor_events e
        JOIN events.event_routes r ON r.event_id = e.event_id
        JOIN events.event_phases p ON p.event_id = e.event_id
        WHERE e.status = 'active'
          AND $1 = ANY(r.segment_ids)
          AND p.window_start <= $2 AND p.window_end >= $2
        ORDER BY e.event_id, p.window_start
        """,
        segment_id,
        t,
    )
    return [dict(r) for r in rows]


def _parse_phases(raw: Any) -> tuple[tuple[int, tuple[str, ...]], ...]:
    """006 JSONB phases -> the rules module's frozen tuple-of-tuples."""
    if isinstance(raw, str):
        with_suppress = json.loads(raw)
    else:
        with_suppress = raw
    phases: list[tuple[int, tuple[str, ...]]] = []
    for ph in with_suppress or []:
        green = int(ph.get("green_sec", 0))
        movements = tuple(str(m) for m in ph.get("movements", []))
        phases.append((green, movements))
    return tuple(phases)


# ────────────────────────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────────────────────────


@router.get("/recommendations")
async def list_recommendations(
    request: Request,
    status: str = Query(default="pending"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    """Recommendations by status.  ``status=approved`` = the challan list —
    the ONLY exposure of approved rows (C6)."""
    if status not in _STATUSES:
        raise HTTPException(422, f"status must be one of {_STATUSES}")
    pool = _pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.recommendation_id, r.sighting_id, r.vehicle_id, r.rule_id,
                   r.recommendation, r.evidence, r.status, r.decided_by,
                   r.decided_at, r.audit_tail, r.created_at,
                   v.plate
            FROM events.enforcement_recommendations r
            JOIN corridor.vehicles v ON v.vehicle_id = r.vehicle_id
            WHERE r.status = $1
            ORDER BY r.created_at DESC LIMIT $2
            """,
            status,
            limit,
        )
    recs = []
    for r in rows:
        evidence = r["evidence"]
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        tail = r["audit_tail"]
        if isinstance(tail, str):
            tail = json.loads(tail)
        recs.append(
            {
                "recommendation_id": str(r["recommendation_id"]),
                "sighting_id": str(r["sighting_id"]),
                "vehicle_id": str(r["vehicle_id"]),
                "plate": r["plate"],
                "rule_id": r["rule_id"],
                "recommendation": r["recommendation"],
                "evidence": evidence,
                "status": r["status"],
                "decided_by": r["decided_by"],
                "decided_at": _iso(r["decided_at"]),
                "audit_tail": tail,
                "created_at": _iso(r["created_at"]),
            }
        )
    return {"recommendations": recs, "status": status}


@router.post("/recommendations/{recommendation_id}/decision")
async def decide_recommendation(
    recommendation_id: str, req: DecisionRequest, request: Request
) -> dict:
    """The human approval flow — the ONLY pending -> decided transition.

    Requires a human officer identity; appends an audit entry; only
    'approved' rows surface as challans anywhere in the system.
    """
    pool = _pool(request)
    try:
        rid = _uuid.UUID(recommendation_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "invalid recommendation uuid") from exc
    new_status = "approved" if req.decision == "approve" else "rejected"
    now = datetime.now(timezone.utc)
    entry = _audit_entry(req.officer, f"decision:{new_status}", now, req.note)
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                """
                SELECT recommendation_id, status, audit_tail
                FROM events.enforcement_recommendations
                WHERE recommendation_id = $1
                FOR UPDATE
                """,
                rid,
            )
            if current is None:
                raise HTTPException(404, f"recommendation {recommendation_id} not found")
            if current["status"] != "pending":
                raise HTTPException(
                    409,
                    f"recommendation already {current['status']} "
                    "(decisions are final — the audit trail is append-only)",
                )
            tail = current["audit_tail"]
            if isinstance(tail, str):
                tail = json.loads(tail)
            tail = list(tail or []) + [entry]
            row = await conn.fetchrow(
                """
                UPDATE events.enforcement_recommendations
                SET status = $1, decided_by = $2, decided_at = $3,
                    audit_tail = $4::jsonb
                WHERE recommendation_id = $5 AND status = 'pending'
                RETURNING recommendation_id, status, decided_by, decided_at
                """,
                new_status,
                req.officer,
                now,
                json.dumps(tail),
                rid,
            )
    if row is None:  # lost a race inside the transaction
        raise HTTPException(409, "recommendation already decided")
    logger.info(
        "enforcement.decision",
        recommendation_id=str(rid),
        status=new_status,
        officer=req.officer,
    )
    return {
        "recommendation_id": str(row["recommendation_id"]),
        "status": row["status"],
        "decided_by": row["decided_by"],
        "decided_at": _iso(row["decided_at"]),
        "audit_tail": tail,
    }


@router.post("/evaluate")
async def evaluate_sighting(req: EvaluateRequest, request: Request) -> dict:
    """Run the deterministic rules for one persisted sighting.

    Persists whatever fires as PENDING recommendations, idempotent on
    (sighting_id, rule_id).  Context assembly degrades honestly: missing
    speed limit / signal program / closures simply mean the corresponding
    rule does not fire.
    """
    pool = _pool(request)
    sighting_uuid = _uuid.UUID(req.sighting_id)
    async with pool.acquire() as conn:
        srow = await _load_sighting(conn, sighting_uuid)
        if srow is None:
            raise HTTPException(404, f"sighting {req.sighting_id} not found")
        earmark = await _active_earmark_state(conn, srow["vehicle_id"])
        seg = await _nearest_segment(
            conn, float(srow["lon"]), float(srow["lat"]), SEGMENT_ATTRIBUTION_TOL_M
        )
        sig = await _nearest_signal(
            conn, float(srow["lon"]), float(srow["lat"]), JUNCTION_ATTRIBUTION_TOL_M
        )
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
            lon=float(srow["lon"]),
            lat=float(srow["lat"]),
            source=srow["source"],
            speed_kmh=srow["speed_kmh"],
            heading=srow["heading"],
            matched_plate_hit=srow["matched_plate_hit"],
            earmark_state=earmark,
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
                phases=_parse_phases(sig[1]["phases"]),
                offset_sec=int(sig[1]["offset_sec"] or 0),
            )
            if sig is not None
            else None
        )

        fired = rules.evaluate_rules(sighting, segment_ctx, signal_ctx, closures)

        created: list[dict] = []
        now = datetime.now(timezone.utc)
        for rec in fired:
            audit = [_audit_entry("traffic-oracle", "created:pending", now,
                                  "deterministic rule evaluation")]
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
                        "created_at": _iso(row["created_at"]),
                    }
                )

    logger.info(
        "enforcement.evaluated",
        sighting_id=req.sighting_id,
        fired=len(fired),
        created=len(created),
    )
    return {
        "sighting_id": req.sighting_id,
        "rules_run": list(rules.RULE_IDS),
        "fired": [r.rule_id for r in fired],
        "created": created,
        "already_present": len(fired) - len(created),
        "source": SOURCE_EMULATED,
        "note": "recommendations are PENDING — only a human officer decision approves",
    }


__all__ = ["router", "DecisionRequest", "EvaluateRequest"]
