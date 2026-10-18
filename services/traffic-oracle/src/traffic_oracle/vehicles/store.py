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
"""

from __future__ import annotations

from typing import Any

from traffic_oracle.vehicles.sources import AnprRead

__all__ = [
    "upsert_vehicle_by_plate",
    "vehicle_id_by_plate",
    "insert_earmark",
    "clear_active_earmarks",
    "list_earmarked_vehicles",
    "last_sightings_by_ids",
    "reap_expired_sightings",
    "fetch_sightings",
    "fetch_trace",
    "persist_read",
    "REAP_DEFAULT_SQL",
    "REAP_EARMARKED_SQL",
]

#: Retention reap statements (C6: enforced in the read path, never cron).
#: (1) rows past the default horizon for vehicles with NO active earmark;
#: (2) rows past their own vehicle's active-earmark horizon.
#: Both DELETEs RETURN the reaped ids (parameterized, row-returning).
REAP_DEFAULT_SQL = "DELETE FROM corridor.vehicle_sightings s WHERE s.t < $1 AND NOT EXISTS (SELECT 1 FROM corridor.vehicle_earmarks e WHERE e.vehicle_id = s.vehicle_id AND e.cleared_at IS NULL AND e.retention_until > NOW()) RETURNING s.sighting_id"  # noqa: E501
REAP_EARMARKED_SQL = "DELETE FROM corridor.vehicle_sightings s USING corridor.vehicle_earmarks e WHERE e.vehicle_id = s.vehicle_id AND e.cleared_at IS NULL AND e.retention_until > NOW() AND s.created_at > e.retention_until RETURNING s.sighting_id"  # noqa: E501


async def reap_expired_sightings(conn: Any, default_cutoff) -> int:
    """Run both retention reaps; returns total rows removed.

    ``default_cutoff`` is the precomputed horizon datetime (bound as a
    timestamptz parameter — interval arithmetic never touches SQL so no
    type inference is needed).
    """
    r1 = await conn.fetch(REAP_DEFAULT_SQL, default_cutoff)
    r2 = await conn.fetch(REAP_EARMARKED_SQL)
    return len(r1) + len(r2)


async def upsert_vehicle_by_plate(conn: Any, plate: str, vehicle_class: str,
                                  seen_at: Any) -> Any:
    """Create-or-touch the vehicle row (vehicles only — never persons)."""
    return await conn.fetchrow(
        """
        INSERT INTO corridor.vehicles (plate, vehicle_class, first_seen, last_seen)
        VALUES ($1, $2, $3, $3)
        ON CONFLICT (plate) DO UPDATE SET last_seen = $3, updated_at = NOW()
        RETURNING vehicle_id
        """,
        plate,
        vehicle_class,
        seen_at,
    )


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


async def clear_active_earmarks(conn: Any, vehicle_id) -> Any:
    return await conn.fetchrow(
        """
        UPDATE corridor.vehicle_earmarks SET cleared_at = NOW()
        WHERE vehicle_id = $1 AND cleared_at IS NULL
          AND retention_until > NOW()
        RETURNING earmark_id
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


async def last_sightings_by_ids(conn: Any, vehicle_ids: list) -> list:
    return await conn.fetch(
        """
        SELECT DISTINCT ON (s.vehicle_id) s.vehicle_id, s.t,
               ST_X(s.position) AS lon, ST_Y(s.position) AS lat,
               s.source, s.camera_id, s.matched_plate_hit
        FROM corridor.vehicle_sightings s
        WHERE s.vehicle_id = ANY($1)
        ORDER BY s.vehicle_id, s.t DESC
        """,
        vehicle_ids,
    )


async def fetch_sightings(conn: Any, vehicle_id, limit: int) -> list:
    return await conn.fetch(
        """
        SELECT sighting_id, plate, camera_id, t,
               ST_X(position) AS lon, ST_Y(position) AS lat, speed_kmh,
               heading, vehicle_class, source, run_id, matched_plate_hit
        FROM corridor.vehicle_sightings
        WHERE vehicle_id = $1
        ORDER BY t DESC LIMIT $2
        """,
        vehicle_id,
        limit,
    )


async def fetch_trace(conn: Any, vehicle_id, cutoff) -> list:
    """Trace rows for the vehicle at/after the precomputed cutoff."""
    return await conn.fetch(
        """
        SELECT t, ST_X(position) AS lon, ST_Y(position) AS lat,
               speed_kmh, source, camera_id
        FROM corridor.vehicle_sightings
        WHERE vehicle_id = $1
          AND t >= $2
        ORDER BY t ASC
        """,
        vehicle_id,
        cutoff,
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
