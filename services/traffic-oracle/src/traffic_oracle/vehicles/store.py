"""Parameter-bound SQL for the vehicle watchlist (migration 010 tables).

Every statement is a frozen literal executed with positional $n
placeholders (asyncpg) — the exact discipline of adaptive/persistence.py
(single-line query constants, plain-name arguments) and
traffic_oracle/__main__.py (inline literals).  Callers pass native values
(uuid.UUID, datetime, timedelta, int, float, str, list) prepared BEFORE
the call; nothing user-controlled is ever shaped into SQL text.  Intervals
bind as datetime.timedelta parameters (asyncpg maps them to PostgreSQL
interval natively).  Mutating statements carry RETURNING so every call is
row-returning through the same parameterized path.

TIME INTEGRITY: every read path (last sighting, raw sightings, trace)
filters ``t <= NOW()`` — a future-stamped row can never surface as the
vehicle's position.

LAWFUL DISCLOSURE (C6, parity with the city surfaces): every statement
here that serves an INDIVIDUAL read to a read path (raw sightings, trace,
latest time, the watchlist's last sighting) composes
``traffic_oracle.city.common.SQL_LAWFUL_READ`` — the one predicate the
feed, the camera dossier and the sighting dossier already apply (the
sighting alias is ``s``).  Such a read leaves this module only while its
vehicle holds an ACTIVE earmark, or when it is the evidence of an
enforcement recommendation; the rule is composed, never re-stated, so the
raw read paths and the city surfaces cannot drift apart.
:func:`disclosure_basis` evaluates the same predicate to decide whether a
vehicle discloses anything at all.  The ingest seam's own idempotency
lookup (:func:`fetch_sighting_brief` — the read a client is re-posting)
discloses nothing new and stays as contract C-E defines it.

RETENTION (C6) — :class:`RetentionReaper`, at most once per
REAP_MIN_INTERVAL_SEC per process (and one process at a time, through a
transaction-scoped advisory lock), never on every request:
    (1) sightings past the default horizon of vehicles with NO active
        earmark;
    (2) sightings of a vehicle with an ACTIVE earmark past the LONGER of
        the default horizon and the earmark's retention_days — an earmark
        (lawful basis, cap 90 days) can only EXTEND how long a vehicle's
        sightings are kept, never cut them below what every other vehicle
        gets;
    (3) vehicle rows left with no sightings, no earmark history and no
        recommendation, unseen for the default horizon.
A sighting referenced by a DECIDED (approved/rejected) recommendation is
never reaped — that row is the challan's evidence and its audit trail.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import structlog

from traffic_oracle.city.common import SQL_LAWFUL_READ
from traffic_oracle.vehicles.sources import AnprRead

logger = structlog.get_logger(__name__)

__all__ = [
    "upsert_vehicle_by_plate",
    "vehicle_id_by_plate",
    "vehicle_exists",
    "insert_earmark",
    "clear_active_earmarks",
    "list_earmarked_vehicles",
    "active_earmarks_by_plate",
    "last_sightings_by_ids",
    "latest_sighting_t",
    "reap_expired_sightings",
    "disclosure_basis",
    "DisclosureBasis",
    "fetch_sightings",
    "fetch_trace",
    "fetch_sighting_brief",
    "recommendations_for_sighting",
    "persist_read",
    "RetentionReaper",
    "REAPER",
    "REAP_MIN_INTERVAL_SEC",
    "SIGHTING_RETENTION_DAYS_DEFAULT",
    "REAP_DEFAULT_SQL",
    "REAP_EARMARKED_SQL",
    "REAP_ORPHAN_VEHICLES_SQL",
    "SQL_DISCLOSURE_BASIS",
    "SQL_LAWFUL_LAST_SIGHTINGS",
    "SQL_LAWFUL_LATEST_T",
    "SQL_LAWFUL_SIGHTINGS",
    "SQL_LAWFUL_TRACE",
]

# ────────────────────────────────────────────────────────────────────────────
# Individual-read statements — each composes SQL_LAWFUL_READ (C6 parity)
# ────────────────────────────────────────────────────────────────────────────

#: A vehicle's standing to disclose individual reads, in ONE round trip:
#: ``earmarked`` is SQL_LAWFUL_READ evaluated for a hypothetical read of the
#: vehicle that is NO recommendation's evidence (sighting_id NULL) — true
#: exactly while the vehicle holds an active earmark, by the very predicate
#: the reads are filtered with; ``lawful_reads`` says whether any of its
#: persisted (non-future) reads passes the predicate.
SQL_DISCLOSURE_BASIS = "SELECT (SELECT " + SQL_LAWFUL_READ + " FROM (SELECT $1::uuid AS vehicle_id, NULL::uuid AS sighting_id) s) AS earmarked, EXISTS (SELECT 1 FROM corridor.vehicle_sightings s WHERE s.vehicle_id = $1::uuid AND s.t <= NOW() AND " + SQL_LAWFUL_READ + ") AS lawful_reads"  # noqa: E501

#: Raw reads of one vehicle, newest first — lawful ones only.
SQL_LAWFUL_SIGHTINGS = "SELECT s.sighting_id, s.plate, s.camera_id, s.t, ST_X(s.position) AS lon, ST_Y(s.position) AS lat, s.speed_kmh, s.heading, s.vehicle_class, s.source, s.run_id, s.matched_plate_hit FROM corridor.vehicle_sightings s WHERE s.vehicle_id = $1 AND s.t <= NOW() AND " + SQL_LAWFUL_READ + " ORDER BY s.t DESC LIMIT $2"  # noqa: E501

#: Trace points of one vehicle in [cutoff, NOW()], oldest first — lawful only.
SQL_LAWFUL_TRACE = "SELECT s.t, ST_X(s.position) AS lon, ST_Y(s.position) AS lat, s.speed_kmh, s.source, s.camera_id FROM corridor.vehicle_sightings s WHERE s.vehicle_id = $1 AND s.t >= $2 AND s.t <= NOW() AND " + SQL_LAWFUL_READ + " ORDER BY s.t ASC"  # noqa: E501

#: Newest lawful, non-future read time (the trace's 'quiet' label) — never
#: the time of a read the vehicle may not disclose.
SQL_LAWFUL_LATEST_T = "SELECT max(s.t) AS latest_t FROM corridor.vehicle_sightings s WHERE s.vehicle_id = $1 AND s.t <= NOW() AND " + SQL_LAWFUL_READ  # noqa: E501

#: Last lawful read per vehicle (the watchlist listing; its vehicles hold
#: active earmarks, and an earmark cleared between the two queries now
#: withdraws the read here too).
SQL_LAWFUL_LAST_SIGHTINGS = "SELECT DISTINCT ON (s.vehicle_id) s.vehicle_id, s.t, ST_X(s.position) AS lon, ST_Y(s.position) AS lat, s.source, s.camera_id, s.matched_plate_hit FROM corridor.vehicle_sightings s WHERE s.vehicle_id = ANY($1) AND s.t <= NOW() AND " + SQL_LAWFUL_READ + " ORDER BY s.vehicle_id, s.t DESC"  # noqa: E501

#: Retention reap statements (C6).  Every DELETE RETURNs the reaped ids
#: (parameterized, row-returning) and spares sightings that a DECIDED
#: recommendation cites.
#: (1) rows past the default horizon for vehicles with NO active earmark;
#: (2) rows of actively earmarked vehicles older than
#:     GREATEST(retention_days, default days) — $1 = the default days;
#: (3) orphaned vehicle rows (no sightings, earmarks or recommendations).
REAP_DEFAULT_SQL = "DELETE FROM corridor.vehicle_sightings s WHERE s.t < $1 AND NOT EXISTS (SELECT 1 FROM corridor.vehicle_earmarks e WHERE e.vehicle_id = s.vehicle_id AND e.cleared_at IS NULL AND e.retention_until > NOW()) AND NOT EXISTS (SELECT 1 FROM events.enforcement_recommendations r WHERE r.sighting_id = s.sighting_id AND r.status <> 'pending') RETURNING s.sighting_id"  # noqa: E501
REAP_EARMARKED_SQL = "DELETE FROM corridor.vehicle_sightings s USING (SELECT ve.vehicle_id, max(ve.retention_days) AS retention_days FROM corridor.vehicle_earmarks ve WHERE ve.cleared_at IS NULL AND ve.retention_until > NOW() GROUP BY ve.vehicle_id) e WHERE e.vehicle_id = s.vehicle_id AND s.t < NOW() - make_interval(days => GREATEST(e.retention_days, $1::int)) AND NOT EXISTS (SELECT 1 FROM events.enforcement_recommendations r WHERE r.sighting_id = s.sighting_id AND r.status <> 'pending') RETURNING s.sighting_id"  # noqa: E501
REAP_ORPHAN_VEHICLES_SQL = "DELETE FROM corridor.vehicles v WHERE v.last_seen < $1 AND NOT EXISTS (SELECT 1 FROM corridor.vehicle_sightings s WHERE s.vehicle_id = v.vehicle_id) AND NOT EXISTS (SELECT 1 FROM corridor.vehicle_earmarks e WHERE e.vehicle_id = v.vehicle_id) AND NOT EXISTS (SELECT 1 FROM events.enforcement_recommendations r WHERE r.vehicle_id = v.vehicle_id) RETURNING v.vehicle_id"  # noqa: E501

#: Cross-process serialization of the reap (pg_try_advisory_xact_lock key).
REAP_ADVISORY_LOCK_KEY = 7310010
REAP_LOCK_SQL = "SELECT pg_try_advisory_xact_lock($1) AS got"

#: The reap runs at most this often per process (seconds).
REAP_MIN_INTERVAL_SEC = 600.0

#: Default sighting retention horizon (days) for vehicles without an
#: active earmark — the router's reap cutoff and the ingest seam's
#: oldest acceptable read share this one value.
SIGHTING_RETENTION_DAYS_DEFAULT = 30


async def reap_expired_sightings(conn: Any, default_cutoff) -> int:
    """Run the three retention reaps; returns total rows removed.

    ``default_cutoff`` is the precomputed horizon datetime (bound as a
    timestamptz parameter — no interval parameter needs type inference).
    Callers run this inside a transaction (:class:`RetentionReaper`).
    """
    r1 = await conn.fetch(REAP_DEFAULT_SQL, default_cutoff)
    r2 = await conn.fetch(REAP_EARMARKED_SQL, SIGHTING_RETENTION_DAYS_DEFAULT)
    r3 = await conn.fetch(REAP_ORPHAN_VEHICLES_SQL, default_cutoff)
    return len(r1) + len(r2) + len(r3)


class RetentionReaper:
    """Throttled, serialized retention reap (C6 — never per request).

    ``maybe_reap`` is cheap to call from any request path: it does nothing
    until REAP_MIN_INTERVAL_SEC has passed since the last attempt, never
    runs twice concurrently in one process, and takes a transaction-scoped
    advisory lock so two oracle processes never reap at once.  A failed
    attempt still backs off for the full interval (a broken reap must not
    turn every request into a failing DELETE) — and stays VISIBLE:
    :meth:`status` (GET /api/v1/vehicles/retention) reports the last
    attempt, its outcome and the consecutive-failure count, so a broken
    DPDP control can never switch itself off silently.
    """

    def __init__(
        self,
        min_interval_sec: float = REAP_MIN_INTERVAL_SEC,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = float(min_interval_sec)
        self._clock = clock
        self._last_attempt: float | None = None
        self._running = False
        self.last_result: dict | None = None
        self.last_attempt_at: datetime | None = None   # wall clock
        self.last_success_at: datetime | None = None
        self.consecutive_failures = 0

    def due(self) -> bool:
        if self._running:
            return False
        if self._last_attempt is None:
            return True
        return (self._clock() - self._last_attempt) >= self._interval

    def reset(self) -> None:
        """Forget the last attempt (tests / operator tooling)."""
        self._last_attempt = None
        self.last_result = None
        self.last_attempt_at = None
        self.last_success_at = None
        self.consecutive_failures = 0

    def status(self) -> dict:
        """The reaper's own health, for the ethics / retention panel.

        ``healthy`` is False only after a FAILED attempt (an attempt that
        found the lock held by another process is not a failure).  The
        timestamps are datetimes; the router serializes them.
        """
        return {
            "min_interval_sec": self._interval,
            "due": self.due(),
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
            "last_result": self.last_result,
            "consecutive_failures": self.consecutive_failures,
            "healthy": self.consecutive_failures == 0,
        }

    async def maybe_reap(self, conn: Any, default_cutoff) -> int | None:
        """Reap if due; returns rows removed, or None when not run."""
        if not self.due():
            return None
        self._running = True
        self._last_attempt = self._clock()
        self.last_attempt_at = datetime.now(timezone.utc)
        try:
            async with conn.transaction():
                lock = await conn.fetchrow(REAP_LOCK_SQL, REAP_ADVISORY_LOCK_KEY)
                if lock is None or not lock["got"]:
                    self.last_result = {"ran": False, "reason": "locked elsewhere"}
                    return None
                removed = await reap_expired_sightings(conn, default_cutoff)
            self.last_result = {"ran": True, "removed": removed}
            self.last_success_at = self.last_attempt_at
            self.consecutive_failures = 0
            if removed:
                logger.info("vehicles.retention_reaped", removed=removed)
            return removed
        except Exception as exc:  # noqa: BLE001 - retention must not fail a read
            self.last_result = {"ran": False, "failed": True, "reason": repr(exc)}
            self.consecutive_failures += 1
            logger.error(
                "vehicles.retention_reap_failed",
                error=repr(exc),
                consecutive_failures=self.consecutive_failures,
            )
            return None
        finally:
            self._running = False


#: The process-wide reaper the vehicles router and the ingest seam share.
REAPER = RetentionReaper()


async def upsert_vehicle_by_plate(conn: Any, plate: str, vehicle_class: str,
                                  seen_at: Any) -> Any:
    """Create-or-touch the vehicle row (vehicles only — never persons).

    first_seen/last_seen only ever widen, so a late-delivered older read
    cannot move a vehicle's last_seen backwards.
    """
    return await conn.fetchrow(
        """
        INSERT INTO corridor.vehicles (plate, vehicle_class, first_seen, last_seen)
        VALUES ($1, $2, $3, $3)
        ON CONFLICT (plate) DO UPDATE
            SET first_seen = LEAST(corridor.vehicles.first_seen, EXCLUDED.first_seen),
                last_seen = GREATEST(corridor.vehicles.last_seen, EXCLUDED.last_seen),
                updated_at = NOW()
        RETURNING vehicle_id
        """,
        plate,
        vehicle_class,
        seen_at,
    )


async def vehicle_exists(conn: Any, vehicle_id) -> bool:
    row = await conn.fetchrow(
        """
        SELECT vehicle_id FROM corridor.vehicles WHERE vehicle_id = $1
        """,
        vehicle_id,
    )
    return row is not None


async def vehicle_id_by_plate(conn: Any, plate: str) -> Any:
    return await conn.fetchrow(
        """
        SELECT vehicle_id FROM corridor.vehicles WHERE plate = $1
        """,
        plate,
    )


async def insert_earmark(conn: Any, vehicle_id, state: str, reason: str,
                         lawful_basis: str, retention_days: int,
                         earmarked_by: str, earmarked_at, retention_until) -> Any:
    return await conn.fetchrow(
        """
        INSERT INTO corridor.vehicle_earmarks
            (vehicle_id, state, reason, lawful_basis, retention_days,
             earmarked_by, earmarked_at, retention_until)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING earmark_id, earmarked_at, retention_until
        """,
        vehicle_id,
        state,
        reason,
        lawful_basis,
        retention_days,
        earmarked_by,
        earmarked_at,
        retention_until,
    )


async def clear_active_earmarks(conn: Any, vehicle_id,
                                officer: str | None = None) -> list:
    """Clear every ACTIVE earmark of a vehicle, recording who cleared it.

    ``cleared_by`` arrives with migration 012; against an older schema the
    clear still happens (expiry semantics are unchanged) and the caller
    reports that the officer could not be recorded.
    """
    import asyncpg  # local: only this fallback needs the exception type

    try:
        return await conn.fetch(
            """
            UPDATE corridor.vehicle_earmarks
            SET cleared_at = NOW(), cleared_by = $2
            WHERE vehicle_id = $1 AND cleared_at IS NULL
              AND retention_until > NOW()
            RETURNING earmark_id, cleared_at, cleared_by
            """,
            vehicle_id,
            officer,
        )
    except asyncpg.exceptions.UndefinedColumnError:
        return await conn.fetch(
            """
            UPDATE corridor.vehicle_earmarks SET cleared_at = NOW()
            WHERE vehicle_id = $1 AND cleared_at IS NULL
              AND retention_until > NOW()
            RETURNING earmark_id, cleared_at, NULL::text AS cleared_by
            """,
            vehicle_id,
        )


async def list_earmarked_vehicles(conn: Any, state: str | None) -> list:
    """Active-earmark vehicles (expiry enforced in the query path, C6).

    ``state`` None lists all; the two branches are frozen literals selected
    only by None-ness — the value itself binds as $1 in the state branch.
    """
    if state is None:
        return await conn.fetch(
            """
            SELECT v.vehicle_id, v.plate, v.vehicle_class, v.first_seen,
                   v.last_seen, e.state, e.reason, e.lawful_basis,
                   e.retention_until
            FROM corridor.vehicles v
            JOIN LATERAL (
                SELECT state, reason, lawful_basis, retention_until
                FROM corridor.vehicle_earmarks ve
                WHERE ve.vehicle_id = v.vehicle_id AND ve.cleared_at IS NULL
                  AND ve.retention_until > NOW()
                ORDER BY ve.earmarked_at DESC LIMIT 1
            ) e ON TRUE
            ORDER BY v.plate
            """
        )
    return await conn.fetch(
        """
        SELECT v.vehicle_id, v.plate, v.vehicle_class, v.first_seen,
               v.last_seen, e.state, e.reason, e.lawful_basis,
               e.retention_until
        FROM corridor.vehicles v
        JOIN LATERAL (
            SELECT state, reason, lawful_basis, retention_until
            FROM corridor.vehicle_earmarks ve
            WHERE ve.vehicle_id = v.vehicle_id AND ve.cleared_at IS NULL
              AND ve.retention_until > NOW()
            ORDER BY ve.earmarked_at DESC LIMIT 1
        ) e ON TRUE
        WHERE e.state = $1
        ORDER BY v.plate
        """,
        state,
    )


async def active_earmarks_by_plate(conn: Any, plate: str) -> list:
    """ACTIVE earmarks of the vehicle carrying ``plate`` (normalized)."""
    return await conn.fetch(
        """
        SELECT e.earmark_id, e.state, e.lawful_basis
        FROM corridor.vehicles v
        JOIN corridor.vehicle_earmarks e ON e.vehicle_id = v.vehicle_id
        WHERE v.plate = $1 AND e.cleared_at IS NULL
          AND e.retention_until > NOW()
        ORDER BY e.earmarked_at DESC
        """,
        plate,
    )


async def last_sightings_by_ids(conn: Any, vehicle_ids: list) -> list:
    """Newest lawful, non-future read of each vehicle in ``vehicle_ids``."""
    return await conn.fetch(SQL_LAWFUL_LAST_SIGHTINGS, vehicle_ids)


@dataclass(frozen=True)
class DisclosureBasis:
    """Whether a vehicle may disclose individual reads, and why (C6).

    ``earmarked``: an ACTIVE earmark — every read of the vehicle is lawful.
    ``lawful_reads``: at least one persisted read passes SQL_LAWFUL_READ
    (with no earmark: the evidence reads of its recommendations).
    """

    earmarked: bool
    lawful_reads: bool

    @property
    def discloses(self) -> bool:
        """False ⇒ nothing about the vehicle's reads may be said at all."""
        return self.earmarked or self.lawful_reads

    @property
    def label(self) -> str:
        """``active_earmark`` (the whole trail) | ``evidence_only``."""
        return "active_earmark" if self.earmarked else "evidence_only"


async def disclosure_basis(conn: Any, vehicle_id) -> DisclosureBasis:
    """The vehicle's lawful basis to disclose individual reads (one query).

    An unknown vehicle id has none — it answers exactly like a known
    vehicle without an earmark or evidence, so the read paths never reveal
    whether a vehicle was ever seen.
    """
    row = await conn.fetchrow(SQL_DISCLOSURE_BASIS, vehicle_id)
    if row is None:
        return DisclosureBasis(earmarked=False, lawful_reads=False)
    return DisclosureBasis(
        earmarked=bool(row["earmarked"]), lawful_reads=bool(row["lawful_reads"])
    )


async def latest_sighting_t(conn: Any, vehicle_id) -> Any:
    """Newest lawful, non-future read time of the vehicle (None: none)."""
    row = await conn.fetchrow(SQL_LAWFUL_LATEST_T, vehicle_id)
    return row["latest_t"] if row is not None else None


async def fetch_sightings(conn: Any, vehicle_id, limit: int) -> list:
    """The vehicle's lawful, non-future reads, newest first."""
    return await conn.fetch(SQL_LAWFUL_SIGHTINGS, vehicle_id, limit)


async def fetch_trace(conn: Any, vehicle_id, cutoff) -> list:
    """Lawful trace rows for the vehicle in [cutoff, NOW()], oldest first."""
    return await conn.fetch(SQL_LAWFUL_TRACE, vehicle_id, cutoff)


async def fetch_sighting_brief(conn: Any, sighting_id) -> Any:
    """The persisted read behind an ingest idempotency key (or None)."""
    return await conn.fetchrow(
        """
        SELECT sighting_id, vehicle_id, plate, camera_id, t, source,
               vehicle_class, matched_plate_hit
        FROM corridor.vehicle_sightings
        WHERE sighting_id = $1
        """,
        sighting_id,
    )


async def recommendations_for_sighting(conn: Any, sighting_id) -> list:
    return await conn.fetch(
        """
        SELECT recommendation_id, rule_id, status
        FROM events.enforcement_recommendations
        WHERE sighting_id = $1
        ORDER BY rule_id
        """,
        sighting_id,
    )


async def persist_read(conn: Any, read: AnprRead, vehicle_id, point_json: str,
                       read_uuid) -> str | None:
    """Persist one AnprRead as an idempotent sighting row.

    All twelve values bind positionally; the geometry crosses the wire as
    a GeoJSON string parameter parsed server-side by ST_GeomFromGeoJSON.
    Returns the sighting_id, or None when the read was already persisted
    (idempotent replay — deterministic read ids make replays no-ops).
    """
    row = await conn.fetchrow(
        """
        INSERT INTO corridor.vehicle_sightings
            (sighting_id, vehicle_id, plate, camera_id, t, position,
             speed_kmh, heading, vehicle_class, source, run_id,
             matched_plate_hit)
        VALUES ($1, $2, $3, $4, $5,
                ST_SetSRID(ST_GeomFromGeoJSON($6), 4326),
                $7, $8, $9, $10, $11, $12)
        ON CONFLICT (sighting_id) DO NOTHING
        RETURNING sighting_id
        """,
        read_uuid,
        vehicle_id,
        read.plate,
        read.camera_id,
        read.t,
        point_json,
        read.speed_kmh,
        read.heading,
        read.vehicle_class,
        read.source,
        read.run_id,
        read.matched_plate_hit,
    )
    if row is None:
        return None
    return str(row["sighting_id"])
