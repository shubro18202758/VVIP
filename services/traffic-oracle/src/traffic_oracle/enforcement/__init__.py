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
         (sighting_id, rule_id)).  409 for a sighting stamped more than
         30 s after the oracle clock.  (The ANPR ingest seam —
         POST /api/v1/vehicles/sightings — runs the same pipeline on every
         accepted read; this endpoint remains for replays and operators.)

APPROVAL MODEL (C6, hard):
    * rows are created 'pending' ONLY — there is no code path that creates
      an approved row;
    * the ONLY transition to approved/rejected is the decision endpoint,
      which requires a human ``officer`` identity and appends an audit
      entry to the row's audit_tail (append-only JSONB);
    * every state change is audited: creation, decision, with actor and
      timestamp — the audit trail the mandate requires.

STOLEN VEHICLES (C-N, hard): approving a PENAL recommendation (an
e-challan: R_SPD_SEGMENT, R_SIGNAL_VIOLATION, R_CLOSED_CORRIDOR_ENTRY) of
a vehicle that holds an ACTIVE stolen-vehicle earmark is refused with 409
``{detail: 'e-challan withheld — vehicle reported stolen', reason:
'stolen_vehicle_fir'}`` — the registered owner is the victim.  This also
covers penal rows created BEFORE the earmark.  Rejecting stays allowed (a
withheld challan is dismissed with its audit entry), and the
R_STOLEN_SIGHTING evidence row of the same vehicle is decided normally
(approve = the authorised basis of a pursuit).

AUTHENTICATED ACTOR (WP-16): when the deck forwards a signed-in operator
as the percent-encoded ``x-veer-officer`` header, that officer IS the
decider (``decided_by`` and the audit entry's ``actor``, with
``actor_source: "x-veer-officer"``); a differing body ``officer`` is kept
as the entry's ``claimed_officer``.  Without the header nothing changes —
the body officer decides, as before.  The header is trusted only because
the oracle binds loopback (WP-08) and is honoured only on a loopback
connection — one seam, shared with the dispatch channel
(units/router.py ``authenticated_officer``).

TIME INTEGRITY (hard): no recommendation may predate its violation —
    /evaluate refuses future-dated sightings (409, beyond 30 s of camera
    clock skew) and the decision endpoint refuses to decide a
    recommendation whose sighting still lies in the future AT ALL (409, no
    skew allowance — an approval is never stamped before its violation).

CONTEXT ASSEMBLY for /evaluate lives in ``enforcement/engine.py`` (shared
with the ingest seam): the sighting + ACTIVE earmark, the segment under
the read, the nearest signalized junction within a 40 m stop-line radius
with its approach legs, and the active closure windows covering the read.
"""

from __future__ import annotations

import json
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from traffic_oracle.city.common import clean_text, named_actor
from traffic_oracle.enforcement import engine, rules
from traffic_oracle.enforcement.engine import (
    FUTURE_TOLERANCE_SEC,
    JUNCTION_ATTRIBUTION_TOL_M,
    SEGMENT_ATTRIBUTION_TOL_M,
    SQL_STOLEN_EARMARK,
    FutureSightingError,
)
from traffic_oracle.units.router import attributed_actor, authenticated_officer

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/enforcement", tags=["enforcement"])

# SEGMENT_ATTRIBUTION_TOL_M (300 m) and JUNCTION_ATTRIBUTION_TOL_M (40 m,
# a stop-line radius — it was 400 m) are defined in engine.py, the single
# context-assembly site, and re-exported here.

_STATUSES = ("pending", "approved", "rejected")

#: The recommendation being decided, locked against a concurrent decision,
#: with its sighting time (time integrity) and whether its vehicle is
#: reported stolen RIGHT NOW (C-N: no e-challan for the victim's car).
_SQL_DECISION_ROW = "SELECT r.recommendation_id, r.status, r.audit_tail, r.rule_id, r.vehicle_id, s.t AS sighting_t, " + SQL_STOLEN_EARMARK + " AS stolen_vehicle FROM events.enforcement_recommendations r LEFT JOIN corridor.vehicle_sightings s ON s.sighting_id = r.sighting_id WHERE r.recommendation_id = $1 FOR UPDATE OF r"  # noqa: E501


class _ChallanWithheldError(Exception):
    """Approving a penal recommendation of a vehicle reported stolen."""

    def __init__(self, rule_id: str) -> None:
        super().__init__(rule_id)
        self.rule_id = rule_id


class DecisionRequest(BaseModel):
    decision: str = Field(description="approve | reject")
    officer: str | None = Field(
        default=None,
        min_length=2,
        max_length=120,
        description=(
            "human officer identity — mandatory (C6: no auto-approval) unless "
            "the deck-verified x-veer-officer header names the decider"
        ),
    )
    note: str = Field(default="", max_length=500)

    @field_validator("decision")
    @classmethod
    def _valid_decision(cls, v: str) -> str:
        if v not in ("approve", "reject"):
            raise ValueError("decision must be 'approve' or 'reject'")
        return v

    @field_validator("note")
    @classmethod
    def _valid_note(cls, v: str) -> str:
        # A NUL (or a bidi override) is a 422 here, never a 500 from the
        # JSONB audit_tail write.
        cleaned = clean_text(v, field_name="note")
        return cleaned if cleaned is not None else ""

    @field_validator("officer")
    @classmethod
    def _valid_officer(cls, v: str | None) -> str | None:
        # The named-actor rule every write surface shares: whitespace
        # collapsed, 2..120 characters, no control / bidi characters (a NUL
        # is a 422 here, never a 500 from the JSONB audit write).
        return named_actor(v, min_len=2, max_len=120)


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


def _audit_entry(
    actor: str, action: str, at: datetime, note: str = "", detail: dict | None = None
) -> dict:
    """One audit_tail entry; ``detail`` (the actor's provenance —
    ``actor_source``, ``claimed_officer``) is merged in when present."""
    entry = {"actor": actor, "action": action, "at": at.isoformat(), "note": note}
    if detail:
        entry.update({k: v for k, v in detail.items() if k != "actor"})
    return entry


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
                   v.plate, p.event_id AS pursuit_event_id, p.status AS pursuit_status
            FROM events.enforcement_recommendations r
            LEFT JOIN corridor.vehicles v ON v.vehicle_id = r.vehicle_id
            LEFT JOIN LATERAL (
                SELECT e.event_id, e.status FROM events.corridor_events e
                WHERE e.event_kind = 'vehicle_pursuit'
                  AND e.metadata->>'recommendation_id' = r.recommendation_id::text
                ORDER BY e.created_at DESC LIMIT 1
            ) p ON TRUE
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
                # NULL once the sighting aged out (FK ON DELETE SET NULL,
                # migration 012) — the evidence JSONB keeps the read.
                "sighting_id": (
                    str(r["sighting_id"]) if r["sighting_id"] is not None else None
                ),
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
                # the pursuit this approval authorised (C6: one approval, one
                # pursuit) — a closed one means the approval is spent
                "pursuit": (
                    {"event_id": str(r.get("pursuit_event_id")), "status": r.get("pursuit_status")}
                    if r.get("pursuit_event_id") is not None
                    else None
                ),
            }
        )
    return {"recommendations": recs, "status": status}


@router.post("/recommendations/{recommendation_id}/decision")
async def decide_recommendation(
    recommendation_id: str, req: DecisionRequest, request: Request
) -> Any:
    """The human approval flow — the ONLY pending -> decided transition.

    Requires a human officer identity — the deck-verified
    ``x-veer-officer`` header when present, else the body ``officer`` —
    appends an audit entry; only 'approved' rows surface as challans
    anywhere in the system.  Approving an e-challan of a vehicle reported
    stolen is a 409 (C-N); rejecting it is allowed.
    """
    pool = _pool(request)
    try:
        rid = _uuid.UUID(recommendation_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "invalid recommendation uuid") from exc
    officer, provenance = attributed_actor(authenticated_officer(request), req.officer)
    new_status = "approved" if req.decision == "approve" else "rejected"
    now = datetime.now(timezone.utc)
    entry = _audit_entry(officer, f"decision:{new_status}", now, req.note, provenance)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(_SQL_DECISION_ROW, rid)
                if current is None:
                    raise HTTPException(
                        404, f"recommendation {recommendation_id} not found"
                    )
                sighting_t = current["sighting_t"]
                if sighting_t is not None:
                    try:
                        # no skew allowance: the decision must follow the read
                        engine.check_not_future(sighting_t, now, tolerance_sec=0.0)
                    except FutureSightingError as exc:
                        raise HTTPException(
                            409,
                            "cannot decide a recommendation before its violation: "
                            + str(exc),
                        ) from exc
                if current["status"] != "pending":
                    raise HTTPException(
                        409,
                        f"recommendation already {current['status']} "
                        "(decisions are final — the audit trail is append-only)",
                    )
                if (
                    new_status == "approved"
                    and rules.is_penal(current["rule_id"])
                    and current["stolen_vehicle"]
                ):
                    # the registered owner of a stolen car is the victim:
                    # nothing is written, the row stays pending (rejectable)
                    raise _ChallanWithheldError(str(current["rule_id"]))
                row, tail = await _record_decision(
                    conn, current, rid, new_status, officer, now, entry
                )
    except _ChallanWithheldError as exc:
        logger.warning(
            "enforcement.challan_withheld",
            recommendation_id=str(rid),
            rule_id=exc.rule_id,
            officer=officer,
        )
        return JSONResponse(
            status_code=409,
            content={
                "detail": rules.CHALLAN_WITHHELD_TEXT,
                "reason": rules.STOLEN_VEHICLE_BASIS,
                "recommendation_id": str(rid),
                "rule_id": exc.rule_id,
            },
        )
    if row is None:  # lost a race inside the transaction
        raise HTTPException(409, "recommendation already decided")
    logger.info(
        "enforcement.decision",
        recommendation_id=str(rid),
        status=new_status,
        officer=officer,
        actor_source=(provenance or {}).get("actor_source", "body"),
        claimed_officer=(provenance or {}).get("claimed_officer"),
    )
    return {
        "recommendation_id": str(row["recommendation_id"]),
        "status": row["status"],
        "decided_by": row["decided_by"],
        "decided_at": _iso(row["decided_at"]),
        "audit_tail": tail,
    }


async def _record_decision(
    conn: Any,
    current: Any,
    rid: _uuid.UUID,
    new_status: str,
    officer: str,
    now: datetime,
    entry: dict,
) -> tuple[Any, list]:
    """Append the decision entry and flip the status (inside the caller's
    transaction, the row locked FOR UPDATE).  Returns (row | None, tail)."""
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
        officer,
        now,
        json.dumps(tail),
        rid,
    )
    return row, tail


@router.post("/evaluate")
async def evaluate_sighting(req: EvaluateRequest, request: Request) -> dict:
    """Run the deterministic rules for one persisted sighting.

    Persists whatever fires as PENDING recommendations, idempotent on
    (sighting_id, rule_id).  Context assembly degrades honestly: missing
    speed limit / signal program / closures simply mean the corresponding
    rule does not fire.  A sighting stamped more than 30 s after the
    oracle clock is refused with 409 — nothing is evaluated or persisted.
    """
    pool = _pool(request)
    sighting_uuid = _uuid.UUID(req.sighting_id)
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        srow = await engine.load_sighting(conn, sighting_uuid)
        if srow is None:
            raise HTTPException(404, f"sighting {req.sighting_id} not found")
        try:
            async with conn.transaction():
                outcome = await engine.evaluate_sighting(
                    conn, sighting_uuid, now=now, srow=srow
                )
        except FutureSightingError as exc:
            logger.warning(
                "enforcement.future_sighting_refused",
                sighting_id=req.sighting_id,
                t=exc.t.isoformat(),
            )
            raise HTTPException(409, str(exc)) from exc
    assert outcome is not None  # srow existed
    withheld = bool(outcome.context.get("challan_withheld"))
    return {
        "sighting_id": req.sighting_id,
        # the penal rules always run; under a stolen-vehicle earmark their
        # outcome is folded into the R_STOLEN_SIGHTING evidence rule
        "rules_run": list(rules.ALL_RULE_IDS if withheld else rules.RULE_IDS),
        "fired": [r.rule_id for r in outcome.fired],
        "created": outcome.created,
        "already_present": len(outcome.fired) - len(outcome.created),
        "context": outcome.context,
        "source": srow["source"],
        # additive (C-N)
        "challan_withheld": withheld,
        "withheld_rules": list(outcome.context.get("withheld_rules") or []),
        "note": (
            rules.CHALLAN_WITHHELD_TEXT
            + " — the sighting is recorded for a human officer's decision"
            if withheld
            else "recommendations are PENDING — only a human officer decision approves"
        ),
    }


__all__ = [
    "router",
    "DecisionRequest",
    "EvaluateRequest",
    "SEGMENT_ATTRIBUTION_TOL_M",
    "JUNCTION_ATTRIBUTION_TOL_M",
    "FUTURE_TOLERANCE_SEC",
]
