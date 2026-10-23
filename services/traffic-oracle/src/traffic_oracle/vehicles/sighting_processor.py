"""ANPR ingest seam — one camera read in, hit + PENDING recommendations out.

    POST /api/v1/vehicles/sightings      (contract C-E; router in
                                          vehicles/__init__.py)

Body (AnprRead on the wire):
    {read_id, plate, camera_id, t, lon, lat, speed_kmh, heading,
     vehicle_class, source: 'emulated_anpr' | 'anpr'}
200 {sighting_id, vehicle_id, duplicate, hit, recommendations[]}
    (+ additive: plate, camera_id, t, source, vehicle_class, rules_fired,
    warnings, challan_withheld, withheld_rules)
409 when t is more than 30 s after the oracle clock
422 for an invalid plate / body, a read older than the sighting
    retention horizon, a read_id re-used for a different read, or a LIVE
    source label on a deployment that has not enabled live ANPR

What the oracle computes — never what the feed asserts:
    * the PLATE is normalized server-side (vehicles/plates.py), so a
      formatting variant cannot fork the vehicle row;
    * the HIT is computed from ACTIVE corridor.vehicle_earmarks at ingest
      time: an earmark whose lawful basis is a stolen-vehicle FIR is a
      'stolen_vehicle' hit, any other active earmark a 'watchlist' hit.
      A ``matched_plate_hit`` label in the body is ignored.  ('pending_fine'
      is part of the C-E vocabulary but is never asserted: this build has no
      e-challan payment ledger to say a fine is outstanding.)
    * PROVENANCE: the source label is the caller's claim, so a LIVE label
      ('anpr' / 'live_anpr') is accepted only where the deployment enables
      a real vendor feed (VVIP_ACCEPT_LIVE_ANPR=1); otherwise it is refused
      — emulated data can never be dressed up as a live camera;
    * an unrecognised vehicle_class never drops a read (and its watchlist
      hit): it is stored as NULL and named in ``warnings``;
    * TIME: future-stamped reads are refused before anything is written;
    * IDEMPOTENCY: the sighting id IS the read_id (a uuid, or uuid5 of a
      vendor read id), so a re-delivered read returns the same sighting_id
      with duplicate=true and re-runs nothing;
    * RULES: every accepted read runs the deterministic e-challan engine
      (enforcement/engine.py) in the same transaction — fired rules land
      as PENDING rows only; a human officer still decides every one (C6).
      A read of a vehicle reported STOLEN (active stolen-vehicle FIR
      earmark) gets no e-challan: ONE R_STOLEN_SIGHTING lands instead and
      the response says ``challan_withheld: true`` with the penal rules
      the read would have fired (``withheld_rules``) — contract C-N.

Persistence goes through :func:`persist_read`, the AnprSource port's sink
(vehicles/sources.py documents the port).  Vehicles only: a read carries a
plate and kinematics — never a face, a frame or a person field.
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, field_validator

from traffic_oracle.enforcement import engine
from traffic_oracle.vehicles import store
from traffic_oracle.vehicles.plates import InvalidPlate, normalize_plate
from traffic_oracle.vehicles.sources import (
    SOURCE_EMULATED,
    SOURCE_LIVE,
    VEHICLE_CLASSES,
    AnprRead,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "AnprReadIn",
    "IngestResult",
    "StaleReadError",
    "ReadIdConflict",
    "LiveSourceNotEnabled",
    "LIVE_ANPR_ENV",
    "SIGHTING_RETENTION_DAYS",
    "hit_for_earmarks",
    "sighting_uuid_for",
    "persist_read",
    "ingest_read",
]

#: Reads older than this are past the sighting retention horizon (C6) and
#: are refused rather than persisted only to be reaped.
SIGHTING_RETENTION_DAYS = store.SIGHTING_RETENTION_DAYS_DEFAULT

#: Set to 1 on a deployment with a real, registered vendor ANPR feed: only
#: then may a read carry a LIVE source label (the label is the caller's
#: claim — without this, an emulator could pass its reads off as live).
LIVE_ANPR_ENV = "VVIP_ACCEPT_LIVE_ANPR"

#: Wire source label (contract C-E) -> schema label (migration 010 CHECK).
SOURCE_WIRE: dict[str, str] = {
    "emulated_anpr": SOURCE_EMULATED,
    "anpr": SOURCE_LIVE,
    "live_anpr": SOURCE_LIVE,
}

#: Hit vocabulary: wire (C-E) <-> stored matched_plate_hit (010 CHECK).
_HIT_TO_DB = {"stolen_vehicle": "stolen_vehicle", "watchlist": "earmarked_plate"}
_HIT_FROM_DB = {"stolen_vehicle": "stolen_vehicle", "earmarked_plate": "watchlist"}

#: Lawful basis that makes an active earmark a stolen-vehicle hit.
_STOLEN_BASIS = "stolen_vehicle_fir"

#: Namespace for sighting ids derived from non-uuid vendor read ids.
_READ_ID_NAMESPACE = _uuid.uuid5(_uuid.NAMESPACE_URL, "veer-anpr-read")

_CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class StaleReadError(Exception):
    """The read is older than the sighting retention horizon."""


class ReadIdConflict(Exception):
    """A read_id was re-used for a read with a different plate/camera."""


class LiveSourceNotEnabled(Exception):
    """A read claims a live camera source on a deployment without one."""


def _live_anpr_enabled() -> bool:
    return os.environ.get(LIVE_ANPR_ENV, "").strip() == "1"


class AnprReadIn(BaseModel):
    """One ANPR camera read on the wire (contract C-E)."""

    read_id: str = Field(min_length=1, max_length=128)
    plate: str = Field(min_length=1, max_length=32)
    camera_id: str = Field(min_length=1, max_length=64)
    t: datetime
    lon: float = Field(ge=-180.0, le=180.0)
    lat: float = Field(ge=-90.0, le=90.0)
    speed_kmh: float | None = Field(default=None, ge=0.0, le=400.0)
    heading: float | None = None
    # free text from the camera; the ingest keeps only the schema's classes
    vehicle_class: str | None = Field(default=None, max_length=32)
    source: Literal["emulated_anpr", "anpr", "live_anpr"]
    run_id: int | None = Field(default=None, ge=1)

    @field_validator("plate")
    @classmethod
    def _plate(cls, v: str) -> str:
        try:
            return normalize_plate(v)
        except InvalidPlate as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("camera_id")
    @classmethod
    def _camera(cls, v: str) -> str:
        if not _CAMERA_ID_RE.match(v):
            raise ValueError(
                "camera_id must be 1-64 chars of [A-Za-z0-9._:-] "
                "starting with a letter or digit"
            )
        return v

    @field_validator("t")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        # contract: ISO-8601 UTC — a naive timestamp is read as UTC
        return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)

    @field_validator("lon", "lat", "speed_kmh")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v

    @field_validator("heading")
    @classmethod
    def _heading(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if not math.isfinite(v):
            raise ValueError("heading must be a finite number of degrees")
        return round(v % 360.0, 3)

    @field_validator("vehicle_class")
    @classmethod
    def _vclass(cls, v: str | None) -> str | None:
        # normalized only: an unknown class is stored as NULL by the ingest
        # (with a warning) rather than refusing the read and losing its hit
        if v is None:
            return None
        v = v.strip().lower()
        return v or None


@dataclass
class IngestResult:
    """The C-E response, plus additive provenance fields."""

    sighting_id: str
    vehicle_id: str | None
    duplicate: bool
    hit: str | None
    plate: str
    camera_id: str
    t: datetime
    source: str
    vehicle_class: str | None = None
    recommendations: list[dict] = field(default_factory=list)
    rules_fired: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    challan_withheld: bool = False
    withheld_rules: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sighting_id": self.sighting_id,
            "vehicle_id": self.vehicle_id,
            "duplicate": self.duplicate,
            "hit": self.hit,
            "recommendations": self.recommendations,
            # additive provenance (C-E consumers ignore unknown fields)
            "plate": self.plate,
            "camera_id": self.camera_id,
            "t": self.t.isoformat(),
            "source": self.source,
            "vehicle_class": self.vehicle_class,
            "rules_fired": self.rules_fired,
            "warnings": self.warnings,
            # additive (C-N): a stolen vehicle's read proposes no e-challan
            "challan_withheld": self.challan_withheld,
            "withheld_rules": self.withheld_rules,
        }


def sighting_uuid_for(read_id: str, source: str, camera_id: str) -> _uuid.UUID:
    """The idempotency key: the read_id itself when it is a uuid (the
    emulator's deterministic uuid5), else uuid5 over (source, camera,
    vendor read id) so vendor ids stay unique per camera."""
    try:
        return _uuid.UUID(read_id)
    except (ValueError, AttributeError, TypeError):
        return _uuid.uuid5(_READ_ID_NAMESPACE, f"{source}|{camera_id}|{read_id}")


def hit_for_earmarks(earmarks: list) -> str | None:
    """Wire hit from the plate's ACTIVE earmark rows (pure)."""
    if not earmarks:
        return None
    if any(e["lawful_basis"] == _STOLEN_BASIS for e in earmarks):
        return "stolen_vehicle"
    return "watchlist"


async def persist_read(conn: Any, read: AnprRead) -> str | None:
    """Persist one AnprRead as a sighting row (the AnprSource port's sink).

    Upserts the vehicle, coerces the read id to the sighting uuid and
    serializes the point as GeoJSON BEFORE binding — store.persist_read
    binds every value positionally.  Returns the sighting_id, or None on
    an idempotent replay.
    """
    vehicle = await store.upsert_vehicle_by_plate(
        conn, read.plate, read.vehicle_class or "car", read.t
    )
    read_uuid = _uuid.UUID(read.read_id)
    point_json = json.dumps(
        {"type": "Point", "coordinates": [read.lon, read.lat]}
    )
    return await store.persist_read(conn, read, vehicle["vehicle_id"],
                                    point_json, read_uuid)


async def _duplicate_result(conn: Any, existing: Any, body: AnprReadIn) -> IngestResult:
    if existing["plate"] != body.plate or existing["camera_id"] != body.camera_id:
        raise ReadIdConflict(
            f"read_id {body.read_id!r} already identifies a read of a different "
            "plate/camera — read ids must be unique per read"
        )
    recs = await store.recommendations_for_sighting(conn, existing["sighting_id"])
    return IngestResult(
        sighting_id=str(existing["sighting_id"]),
        vehicle_id=str(existing["vehicle_id"]),
        duplicate=True,
        hit=_HIT_FROM_DB.get(existing["matched_plate_hit"]),
        plate=existing["plate"],
        camera_id=existing["camera_id"],
        t=existing["t"],
        source=existing["source"],
        vehicle_class=existing["vehicle_class"],
        recommendations=[
            {
                "recommendation_id": str(r["recommendation_id"]),
                "rule_id": r["rule_id"],
                "status": r["status"],
            }
            for r in recs
        ],
        challan_withheld=any(r["rule_id"] == "R_STOLEN_SIGHTING" for r in recs),
    )


async def ingest_read(
    conn: Any, body: AnprReadIn, *, now: datetime | None = None
) -> IngestResult:
    """Normalize -> provenance -> time-check -> idempotency -> hit ->
    persist -> rules.

    Raises LiveSourceNotEnabled (422), FutureSightingError (409),
    StaleReadError (422) and ReadIdConflict (422).  Everything after the
    checks runs in ONE transaction: the sighting, its hit and its PENDING
    recommendations land together or not at all.
    """
    now = now or datetime.now(timezone.utc)
    source = SOURCE_WIRE[body.source]
    if source == SOURCE_LIVE and not _live_anpr_enabled():
        raise LiveSourceNotEnabled(
            f"source {body.source!r} claims a live camera feed, which this "
            f"deployment has not enabled ({LIVE_ANPR_ENV}=1 is set only for "
            "a registered vendor adapter) — emulated reads must say "
            "source 'emulated_anpr'"
        )
    engine.check_not_future(body.t, now)
    if body.t < now - timedelta(days=SIGHTING_RETENTION_DAYS):
        raise StaleReadError(
            f"read t={body.t.isoformat()} is older than the "
            f"{SIGHTING_RETENTION_DAYS}-day sighting retention horizon"
        )
    warnings: list[str] = []
    vehicle_class = body.vehicle_class
    if vehicle_class is not None and vehicle_class not in VEHICLE_CLASSES:
        warnings.append(
            f"vehicle_class {vehicle_class!r} is not one of "
            f"{sorted(VEHICLE_CLASSES)} — stored as null; the read is kept"
        )
        vehicle_class = None
    sid = sighting_uuid_for(body.read_id, source, body.camera_id)

    async with conn.transaction():
        existing = await store.fetch_sighting_brief(conn, sid)
        if existing is not None:
            return await _duplicate_result(conn, existing, body)

        hit = hit_for_earmarks(await store.active_earmarks_by_plate(conn, body.plate))
        read = AnprRead(
            read_id=str(sid),
            plate=body.plate,
            camera_id=body.camera_id,
            t=body.t,
            lon=body.lon,
            lat=body.lat,
            source=source,
            speed_kmh=body.speed_kmh,
            heading=body.heading,
            vehicle_class=vehicle_class,
            run_id=body.run_id,
            matched_plate_hit=_HIT_TO_DB.get(hit) if hit else None,
        )
        persisted = await persist_read(conn, read)
        if persisted is None:
            # a concurrent delivery of the same read committed first
            existing = await store.fetch_sighting_brief(conn, sid)
            if existing is None:  # pragma: no cover - conflict implies a row
                raise ReadIdConflict(f"read {sid} could not be persisted")
            return await _duplicate_result(conn, existing, body)

        outcome = await engine.evaluate_sighting(conn, sid, now=now)

    assert outcome is not None  # the sighting was just persisted
    logger.info(
        "vehicles.sighting_ingested",
        sighting_id=str(sid),
        camera_id=body.camera_id,
        source=source,
        hit=hit,
        fired=[r.rule_id for r in outcome.fired],
        challan_withheld=bool(outcome.context.get("challan_withheld")),
    )
    return IngestResult(
        sighting_id=str(sid),
        vehicle_id=str(outcome.vehicle_id),
        duplicate=False,
        hit=hit,
        plate=body.plate,
        camera_id=body.camera_id,
        t=body.t,
        source=source,
        vehicle_class=vehicle_class,
        recommendations=[
            {
                "recommendation_id": c["recommendation_id"],
                "rule_id": c["rule_id"],
                "status": "pending",
            }
            for c in outcome.created
        ],
        rules_fired=[r.rule_id for r in outcome.fired],
        warnings=warnings,
        challan_withheld=bool(outcome.context.get("challan_withheld")),
        withheld_rules=list(outcome.context.get("withheld_rules") or []),
    )
