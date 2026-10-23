"""Vehicle watchlist API — earmarks, sightings, traces (watchlist-anpr, 010).

HTTP surface (contract C1 shapes; all SQL lives in store.py, fully
parameter-bound):
    POST /api/v1/vehicles/{plate}/earmark        watch|flagged + lawful basis
    POST /api/v1/vehicles/{vehicle_id}/earmark/clear   {officer} recorded
    GET  /api/v1/vehicles?earmark=watch|flagged  active-earmark vehicles with
                                                 last sighting + source badge
    GET  /api/v1/vehicles/{vehicle_id}/trace?window_sec=
    GET  /api/v1/vehicles/{vehicle_id}/sightings?limit=
    POST /api/v1/vehicles/sightings              the ANPR ingest seam
                                                 (contract C-E, see
                                                 sighting_processor.py)
    GET  /api/v1/vehicles/retention              the retention policy and
                                                 the reaper's own health

ETHICS RAILS (C6) enforced here, not by convention:
    * an earmark requires an enumerated lawful_basis and a retention_days
      value capped at 90 (mirrored by the migration 010 CHECK);
      retention_until is computed server-side — a client cannot stretch it.
    * plates are normalized server-side (vehicles/plates.py) before any
      SQL, so an earmark always lands on the vehicle the cameras read.
    * every sighting response carries its ``source`` ('emulated_anpr' for
      the emulator) so the emulated badge can never be dropped by a view.
    * expired earmarks are filtered in the QUERY path — expiry is enforced
      on read, never promised by a cron.
    * an INDIVIDUAL read leaves /sightings and /trace only under the city
      surfaces' own rule (city/common.py SQL_LAWFUL_READ, composed in
      store.py): its vehicle holds an ACTIVE earmark, or the read is the
      evidence of an enforcement recommendation.  A vehicle with neither
      (never earmarked, cleared, lapsed — or an unknown id, answered the
      same way) is a 404 that carries no count, time or position.
    * retention is reaped by store.REAPER — at most once per 10 minutes,
      serialized across processes, and never at the expense of a decided
      e-challan's evidence; an active earmark can only extend a vehicle's
      retention.  A failing reap is reported (GET /retention), never
      silent.
    * no read path returns a sighting stamped in the future.
    * vehicles only: no person-directed field exists on any path.

ATTRIBUTION: who earmarks and who clears is a named human — the
deck-verified ``x-veer-officer`` header first (loopback only,
units/router.py), else the body (``earmarked_by`` / ``officer``, held to
the shared named-actor rule: 2..120 characters, no control or bidi
characters).  A differing body name is reported as ``claimed_officer``.
Without a header or a body name an earmark is recorded as 'operator' —
unless VVIP_REQUIRE_OFFICER=1, where it is a 422.  ``reason`` is free
text refused (422) when it carries a NUL or a bidi control.
"""

from __future__ import annotations

import os
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from traffic_oracle.city.common import clean_text, named_actor
from traffic_oracle.enforcement.engine import FutureSightingError
from traffic_oracle.units.router import attributed_actor, authenticated_officer
from traffic_oracle.vehicles import store
from traffic_oracle.vehicles.plates import InvalidPlate, normalize_plate
from traffic_oracle.vehicles.sighting_processor import (
    AnprReadIn,
    LiveSourceNotEnabled,
    ReadIdConflict,
    StaleReadError,
    ingest_read,
    persist_read,
)
from traffic_oracle.vehicles.sources import SOURCE_EMULATED, SOURCE_LIVE

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/vehicles", tags=["vehicles"])

#: Default sighting retention (days) — bound as a timedelta into the reap
#: (one value, shared with the ingest seam: store.py).
SIGHTING_RETENTION_DAYS_DEFAULT = store.SIGHTING_RETENTION_DAYS_DEFAULT

#: When set to 1, human actions on this router (earmark clear) must name
#: the officer; otherwise an unnamed clear is accepted and recorded as
#: unattributed (the deck sends the officer from WP-05 on).
REQUIRE_OFFICER_ENV = "VVIP_REQUIRE_OFFICER"

#: Lawful-basis codes accepted for an earmark (India enforcement context:
#: Motor Vehicles Act 1988 s.177/s.183/s.207 powers, BNSS 2023 police
#: powers incl. pursuit, DPDP Act 2023 purpose-limitation).  The list is
#: intentionally small and government-capability framed.
LAWFUL_BASIS_CODES = (
    "mva_1988_s177",       # Motor Vehicles Act — general penalty/notice powers
    "mva_1988_s183",       # speed-limit enforcement
    "mva_1988_s207",       # power to inspect vehicles
    "bnss_2023_s94",       # police power to pursue / intercept
    "stolen_vehicle_fir",  # registered FIR naming the plate
)

EARMARK_STATES = ("watch", "flagged")

#: The one answer for a vehicle that may not disclose individual reads
#: (C6) — deliberately count-free and identical for unknown vehicle ids.
NO_LAWFUL_BASIS_DETAIL = "no lawful basis to disclose individual reads for this vehicle"


#: Officer names on this router: the shared named-actor rule, up to 120
#: characters (the columns are TEXT; the deck caps names at 64).
OFFICER_MIN_LEN = 2
OFFICER_MAX_LEN = 120

#: The recorded earmarker when nobody is named (VVIP_REQUIRE_OFFICER unset).
DEFAULT_EARMARKER = "operator"


class EarmarkRequest(BaseModel):
    state: str = Field(description="watch | flagged")
    reason: str = Field(min_length=3, max_length=500)
    lawful_basis: str = Field(
        description=f"one of {LAWFUL_BASIS_CODES} (C6: NOT NULL, enumerated)",
    )
    retention_days: int = Field(
        default=30, ge=1, le=90, description="earmark retention horizon (C6 cap 90)"
    )
    earmarked_by: str | None = Field(
        default=None,
        description=(
            "named officer (2..120 chars); the deck-verified x-veer-officer "
            "header wins; unnamed = 'operator' unless VVIP_REQUIRE_OFFICER=1"
        ),
    )

    @field_validator("state")
    @classmethod
    def _valid_state(cls, v: str) -> str:
        if v not in EARMARK_STATES:
            raise ValueError(f"state must be one of {EARMARK_STATES}")
        return v

    @field_validator("reason")
    @classmethod
    def _valid_reason(cls, v: str) -> str:
        # a NUL / bidi control is a 422, never a 500 (or a reason that
        # reads differently from what was recorded)
        cleaned = clean_text(v, field_name="reason")
        assert cleaned is not None
        return cleaned

    @field_validator("earmarked_by")
    @classmethod
    def _valid_earmarker(cls, v: str | None) -> str | None:
        return named_actor(
            v, min_len=OFFICER_MIN_LEN, max_len=OFFICER_MAX_LEN, field_name="earmarked_by"
        )

    @field_validator("lawful_basis")
    @classmethod
    def _valid_basis(cls, v: str) -> str:
        if v not in LAWFUL_BASIS_CODES:
            raise ValueError(
                f"lawful_basis must be one of {LAWFUL_BASIS_CODES} (migration 010: "
                "lawful_basis NOT NULL — an earmark never exists without one)"
            )
        return v


class ClearEarmarkRequest(BaseModel):
    """Body of POST /{vehicle_id}/earmark/clear — the human who decided."""

    officer: str | None = Field(
        default=None,
        description="duty officer clearing the earmark (recorded as cleared_by)",
    )

    @field_validator("officer")
    @classmethod
    def _valid_officer(cls, v: str | None) -> str | None:
        return named_actor(v, min_len=OFFICER_MIN_LEN, max_len=OFFICER_MAX_LEN)


def _officer_required() -> bool:
    return os.environ.get(REQUIRE_OFFICER_ENV, "").strip() == "1"


def _actor(request: Request, body_officer: str | None) -> tuple[str | None, dict | None]:
    """``(actor, provenance)`` — the deck-verified header first, else the
    body's officer (None when neither names anyone)."""
    header = authenticated_officer(request)
    if header is None:
        return body_officer, None
    return attributed_actor(header, body_officer)


def _provenance(detail: dict | None, actor: str | None) -> dict:
    return {
        "actor_source": (detail or {}).get("actor_source", "body" if actor else None),
        "claimed_officer": (detail or {}).get("claimed_officer"),
    }


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool unavailable")
    return pool


def _uuid_or_422(value: str) -> _uuid.UUID:
    try:
        return _uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(422, f"invalid uuid {value!r}") from exc


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _position(lon: Any, lat: Any) -> list[float]:
    return [round(float(lon), 6), round(float(lat), 6)]


def _badge(sources: list[str]) -> str:
    """Response-level source badge: emulated whenever ANY row is emulated
    (or nothing was returned) — the emulated label can never be lost."""
    if sources and all(s == SOURCE_LIVE for s in sources):
        return SOURCE_LIVE
    return SOURCE_EMULATED


def _reap_cutoff(now: datetime) -> datetime:
    return now - timedelta(days=SIGHTING_RETENTION_DAYS_DEFAULT)


# ────────────────────────────────────────────────────────────────────────────
# Earmark
# ────────────────────────────────────────────────────────────────────────────


@router.post("/{plate}/earmark")
async def earmark_vehicle(plate: str, req: EarmarkRequest, request: Request) -> dict:
    """Earmark a plate as watch|flagged with a lawful basis + retention limit.

    The plate is normalized server-side first ('GJ 27 EM 9001' and
    'GJ27EM9001' are one vehicle).  Creates the vehicle row on first sight
    (vehicles only — never persons).  retention_until is computed
    server-side from NOW() + retention_days.
    """
    try:
        canonical = normalize_plate(plate)
    except InvalidPlate as exc:
        raise HTTPException(422, str(exc)) from exc
    earmarked_by, detail = _actor(request, req.earmarked_by)
    if earmarked_by is None:
        if _officer_required():
            raise HTTPException(
                422,
                "earmarked_by is required — a named officer earmarks a vehicle "
                f"({REQUIRE_OFFICER_ENV}=1)",
            )
        earmarked_by = DEFAULT_EARMARKER
    pool = _pool(request)
    now = datetime.now(timezone.utc)
    horizon = timedelta(days=req.retention_days)
    retention_until = now + horizon
    async with pool.acquire() as conn:
        async with conn.transaction():
            vehicle = await store.upsert_vehicle_by_plate(
                conn, canonical, "car", now
            )
            row = await store.insert_earmark(
                conn,
                vehicle["vehicle_id"],
                req.state,
                req.reason,
                req.lawful_basis,
                req.retention_days,
                earmarked_by,
                now,
                retention_until,
            )
    logger.info(
        "vehicles.earmarked",
        plate=canonical,
        state=req.state,
        lawful_basis=req.lawful_basis,
        retention_days=req.retention_days,
        earmarked_by=earmarked_by,
        claimed_officer=(detail or {}).get("claimed_officer"),
    )
    return {
        "vehicle_id": str(vehicle["vehicle_id"]),
        "plate": canonical,
        "earmark_id": str(row["earmark_id"]),
        "earmark_state": req.state,
        "state": req.state,  # legacy key, same value as earmark_state
        "reason": req.reason,
        "lawful_basis": req.lawful_basis,
        "earmarked_by": earmarked_by,
        "earmarked_at": _iso(row["earmarked_at"]),
        "retention_until": _iso(row["retention_until"]),
        "retention_days": req.retention_days,
        # additive (WP-19): where the earmarker came from — the verified
        # header, the body, or the unnamed 'operator' default
        "actor_source": (detail or {}).get("actor_source")
        or ("body" if req.earmarked_by else "default"),
        "claimed_officer": (detail or {}).get("claimed_officer"),
    }


@router.post("/{vehicle_id}/earmark/clear")
async def clear_earmark(
    vehicle_id: str,
    request: Request,
    req: ClearEarmarkRequest | None = None,
) -> dict:
    """Clear every ACTIVE earmark of a vehicle — a human decision, recorded.

    ``officer`` is stored as the earmark's cleared_by.  It is required
    when VVIP_REQUIRE_OFFICER=1; otherwise an unnamed clear is accepted
    and reported as unattributed.
    """
    req = req or ClearEarmarkRequest()
    officer, detail = _actor(request, req.officer)
    if officer is None and _officer_required():
        raise HTTPException(
            422, "officer is required to clear an earmark (VVIP_REQUIRE_OFFICER=1)"
        )
    pool = _pool(request)
    vid = _uuid_or_422(vehicle_id)
    async with pool.acquire() as conn:
        rows = await store.clear_active_earmarks(conn, vid, officer)
    if not rows:
        raise HTTPException(404, f"no active earmark for vehicle {vehicle_id}")
    cleared_by = rows[0]["cleared_by"]
    logger.info(
        "vehicles.earmark_cleared",
        vehicle_id=vehicle_id,
        cleared=len(rows),
        officer=officer,
        officer_recorded=cleared_by is not None,
        **_provenance(detail, officer),
    )
    return {
        "vehicle_id": vehicle_id,
        "cleared_earmark_id": str(rows[0]["earmark_id"]),
        "cleared_earmark_ids": [str(r["earmark_id"]) for r in rows],
        "cleared_at": _iso(rows[0]["cleared_at"]),
        "cleared_by": cleared_by,
        # False when no officer was named, or the schema predates 012
        "officer_recorded": cleared_by is not None,
        # additive (WP-19): where the clearing officer came from
        **_provenance(detail, officer),
    }


# ────────────────────────────────────────────────────────────────────────────
# Watchlist listing (C1 shape)
# ────────────────────────────────────────────────────────────────────────────


@router.get("")
async def list_vehicles(
    request: Request,
    earmark: str | None = Query(default=None),
) -> dict:
    """Vehicles with their ACTIVE earmark + last sighting (C1 shape).

    ``earmark`` filters to watch|flagged; expiry is enforced in the query
    path (C6), so an expired earmark never surfaces here.  The last
    sighting is the newest LAWFUL one not stamped in the future (the same
    C6 rule as /sightings — an earmark cleared between the two queries
    withdraws it here too).
    """
    if earmark is not None and earmark not in EARMARK_STATES:
        raise HTTPException(422, f"earmark filter must be one of {EARMARK_STATES}")
    pool = _pool(request)
    async with pool.acquire() as conn:
        rows = await store.list_earmarked_vehicles(conn, earmark)
        ids = [r["vehicle_id"] for r in rows]
        last: dict = {}
        if ids:
            srows = await store.last_sightings_by_ids(conn, ids)
            for sr in srows:
                last[str(sr["vehicle_id"])] = {
                    "t": _iso(sr["t"]),
                    "position": _position(sr["lon"], sr["lat"]),
                    "source": sr["source"],
                    "camera_id": sr["camera_id"],
                    "matched_plate_hit": sr["matched_plate_hit"],
                }
    vehicles = []
    for r in rows:
        last_sighting = last.get(str(r["vehicle_id"]))
        vehicles.append(
            {
                "vehicle_id": str(r["vehicle_id"]),
                "plate": r["plate"],
                "vehicle_class": r["vehicle_class"],
                "earmark_state": r["state"],
                "earmark_reason": r["reason"],
                "lawful_basis": r["lawful_basis"],
                "retention_until": _iso(r["retention_until"]),
                "last_sighting": last_sighting,
                "source": _badge(
                    [last_sighting["source"]] if last_sighting else []
                ),
            }
        )
    return {
        "vehicles": vehicles,
        "source": _badge([v["source"] for v in vehicles]),
    }


# ────────────────────────────────────────────────────────────────────────────
# Retention status (C6 / DPDP — the reap must be seen to run)
# ────────────────────────────────────────────────────────────────────────────


@router.get("/retention")
async def retention_status() -> dict:
    """The sighting-retention policy and the reaper's health.

    Read-only and DB-free: what the policy is, when the throttled reap last
    ran, what it removed, and whether it is failing (``healthy`` false,
    ``consecutive_failures`` > 0) — so the ethics panel can show a broken
    DPDP control instead of it switching itself off quietly.
    """
    reaper = store.REAPER.status()
    for key in ("last_attempt_at", "last_success_at"):
        reaper[key] = _iso(reaper[key])
    return {
        "default_retention_days": SIGHTING_RETENTION_DAYS_DEFAULT,
        "earmark_retention_cap_days": 90,
        "policy": (
            "sightings of vehicles without an active earmark are kept "
            f"{SIGHTING_RETENTION_DAYS_DEFAULT} days; an active earmark "
            "(lawful basis) keeps them up to its retention_days (cap 90), "
            "never less than the default; a sighting cited by a decided "
            "e-challan recommendation is kept as its evidence"
        ),
        "reaper": reaper,
    }


# ────────────────────────────────────────────────────────────────────────────
# Trace + sightings
# ────────────────────────────────────────────────────────────────────────────


async def _disclosure_or_404(conn: Any, vid: _uuid.UUID, vehicle_id: str) -> store.DisclosureBasis:
    """The vehicle's lawful basis to disclose reads; 404 when it has none.

    Checked AFTER the retention reap (a reaped read is never disclosed)
    and BEFORE any read is fetched; the fetches compose the same predicate
    again, so an earmark cleared in between only ever narrows the answer.
    """
    basis = await store.disclosure_basis(conn, vid)
    if not basis.discloses:
        logger.info("vehicles.reads_withheld", vehicle_id=vehicle_id)
        raise HTTPException(404, NO_LAWFUL_BASIS_DETAIL)
    return basis


@router.get("/{vehicle_id}/sightings")
async def vehicle_sightings(
    vehicle_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    """Lawful raw sightings for a vehicle, newest first (none in the future).

    With an ACTIVE earmark: every read (``disclosure: active_earmark``).
    Without one: only the reads that are evidence of its enforcement
    recommendations (``disclosure: evidence_only``).  Neither: 404 (C6).
    """
    pool = _pool(request)
    vid = _uuid_or_422(vehicle_id)
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await store.REAPER.maybe_reap(conn, _reap_cutoff(now))
        basis = await _disclosure_or_404(conn, vid, vehicle_id)
        rows = await store.fetch_sightings(conn, vid, limit)
    return {
        "vehicle_id": vehicle_id,
        "disclosure": basis.label,
        "sightings": [
            {
                "sighting_id": str(r["sighting_id"]),
                "plate": r["plate"],
                "camera_id": r["camera_id"],
                "t": _iso(r["t"]),
                "position": _position(r["lon"], r["lat"]),
                "speed_kmh": r["speed_kmh"],
                "heading": r["heading"],
                "vehicle_class": r["vehicle_class"],
                "source": r["source"],
                "run_id": r["run_id"],
                "matched_plate_hit": r["matched_plate_hit"],
            }
            for r in rows
        ],
        "source": _badge([r["source"] for r in rows]),
    }


@router.get("/{vehicle_id}/trace")
async def vehicle_trace(
    vehicle_id: str,
    request: Request,
    window_sec: int = Query(default=3600, ge=60, le=86400),
) -> dict:
    """Timed trace for the canvas polyline layer (C1 shape).

    GeoJSON LineString (oldest -> newest) + timed positions, each carrying
    its source so the emulated badge travels with every point.  Only
    LAWFUL reads are traced (C6, as on /sightings): the whole trail under
    an active earmark, the evidence reads alone without one, and a 404
    for a vehicle with neither (or an unknown id).  A LineString needs
    two positions, so with fewer than two lawful reads in the window
    ``geometry`` is null (never an invalid one-point LineString) while
    ``positions`` still lists the one read.  A vehicle that may disclose
    but has no lawful read inside the window is a 200 with null geometry,
    no positions, ``status: quiet`` and ``latest_t`` (its newest lawful
    read, or null) — 'quiet', not 'not deployed'.
    """
    pool = _pool(request)
    vid = _uuid_or_422(vehicle_id)
    now = datetime.now(timezone.utc)
    trace_cutoff = now - timedelta(seconds=window_sec)
    async with pool.acquire() as conn:
        await store.REAPER.maybe_reap(conn, _reap_cutoff(now))
        basis = await _disclosure_or_404(conn, vid, vehicle_id)
        rows = await store.fetch_trace(conn, vid, trace_cutoff)
        if rows:
            latest_t = rows[-1]["t"]
        else:
            latest_t = await store.latest_sighting_t(conn, vid)
    coords = [_position(r["lon"], r["lat"]) for r in rows]
    return {
        "vehicle_id": vehicle_id,
        "window_sec": window_sec,
        "disclosure": basis.label,
        "status": "tracking" if rows else "quiet",
        # a LineString needs >= 2 positions (RFC 7946); fewer: null
        "geometry": (
            {"type": "LineString", "coordinates": coords} if len(coords) >= 2 else None
        ),
        "positions": [
            {
                "t": _iso(r["t"]),
                "position": _position(r["lon"], r["lat"]),
                "speed_kmh": r["speed_kmh"],
                "source": r["source"],  # 'emulated_anpr' for the emulator
                "camera_id": r["camera_id"],
            }
            for r in rows
        ],
        "count": len(rows),
        "latest_t": _iso(latest_t),
        "latest_age_sec": (
            round((now - latest_t).total_seconds(), 1)
            if latest_t is not None
            else None
        ),
        "source": _badge([r["source"] for r in rows]),
    }


# ────────────────────────────────────────────────────────────────────────────
# Ingest: the ANPR seam (contract C-E)
# ────────────────────────────────────────────────────────────────────────────


@router.post("/sightings")
async def ingest_sighting(body: AnprReadIn, request: Request) -> dict:
    """One ANPR read in -> {sighting_id, vehicle_id, duplicate, hit,
    recommendations[]} (contract C-E).

    The hit is computed from ACTIVE earmarks (never from the feed), the
    plate is normalized, future reads are refused (409), a live source
    label is refused unless the deployment enables live ANPR (422),
    re-delivered reads are idempotent on read_id, and the e-challan rules
    run on every accepted read — creating PENDING recommendations only.
    """
    import asyncpg  # local: the FK-violation type only

    pool = _pool(request)
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await store.REAPER.maybe_reap(conn, _reap_cutoff(now))
        try:
            result = await ingest_read(conn, body, now=now)
        except FutureSightingError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (StaleReadError, ReadIdConflict, LiveSourceNotEnabled) as exc:
            raise HTTPException(422, str(exc)) from exc
        except asyncpg.exceptions.ForeignKeyViolationError as exc:
            raise HTTPException(
                422, "run_id does not name a known emulation run"
            ) from exc
    return result.to_dict()


__all__ = [
    "router",
    "EarmarkRequest",
    "ClearEarmarkRequest",
    "LAWFUL_BASIS_CODES",
    "EARMARK_STATES",
    "NO_LAWFUL_BASIS_DETAIL",
    "SIGHTING_RETENTION_DAYS_DEFAULT",
    "persist_read",
]
