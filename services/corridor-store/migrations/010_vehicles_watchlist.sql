-- 010_vehicles_watchlist.sql
-- Vehicle backbone: watchlist, emulated ANPR sightings, deterministic
-- e-challan recommendations (watchlist-anpr stream).
--
-- ETHICS RAILS (contract C6, hard):
--   * VEHICLES ONLY — no face/person fields exist anywhere in this schema.
--   * Every earmark carries a lawful_basis (NOT NULL) and a retention
--     horizon (retention_until NOT NULL, CHECKed strictly after
--     earmarked_at, retention_days capped at 90).
--   * Sightings are source-labelled: 'emulated_anpr' (the only value this
--     build ever writes) or 'live_anpr' (reserved for a real vendor feed
--     behind the AnprSource port; nothing writes it today).
--   * Penalties exist ONLY as human-approved recommendations: rows start
--     'pending' and no code path auto-approves — the decision endpoint
--     requires a human officer identity and appends to the audit tail.
--
-- Also relaxes the event-kind CHECKs (contract C3 lockstep, in THIS
-- migration only — 007/008 are byte-immutable per the checksum ledger):
--   corridor_events_event_kind_check   + 'crime_incident' + 'vehicle_pursuit'
--   event_profiles_event_kind_check    + 'crime_incident' + 'vehicle_pursuit'
-- (constraint names are the auto-generated ones, verified live against
-- pg catalogs before this file was written.)
--
-- Idempotent and additive-only (same rules as 007/008): safe on fresh
-- installs and on the ledgered live install.
--
-- GRAMMAR CONSTRAINT: no ';' inside string literals and no dollar-quoted
-- bodies — these files are applied both by psql and by naive ';'-splitters
-- (tests apply them statement-by-statement).  Guarded-CHECK relaxation uses
-- ALTER TABLE ... DROP CONSTRAINT IF EXISTS + re-ADD.

-- =============================================================================
-- CORRIDOR.SCHEMA — vehicles, earmarks, sightings
-- =============================================================================

CREATE TABLE IF NOT EXISTS corridor.vehicles (
    vehicle_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plate         TEXT NOT NULL UNIQUE,
    vehicle_class TEXT NOT NULL DEFAULT 'car' CHECK (vehicle_class IN (
                      'car', 'two_wheeler', 'auto_rickshaw',
                      'bus', 'truck', 'emergency'
                  )),
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Earmarks: watchlist states with lawful basis + retention limit ─────────
-- An ACTIVE earmark is cleared_at IS NULL AND retention_until > NOW() —
-- the query paths enforce expiry (C6), never a cron promise.  Vehicles
-- only: the table references corridor.vehicles exclusively.

CREATE TABLE IF NOT EXISTS corridor.vehicle_earmarks (
    earmark_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id      UUID NOT NULL REFERENCES corridor.vehicles(vehicle_id)
                        ON DELETE CASCADE,
    state           TEXT NOT NULL CHECK (state IN ('watch', 'flagged')),
    reason          TEXT NOT NULL,
    lawful_basis    TEXT NOT NULL,
    retention_days  INTEGER NOT NULL CHECK (retention_days BETWEEN 1 AND 90),
    earmarked_by    TEXT NOT NULL DEFAULT 'operator',
    earmarked_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retention_until TIMESTAMPTZ NOT NULL,
    cleared_at      TIMESTAMPTZ,
    CHECK (retention_until > earmarked_at)
);

CREATE INDEX IF NOT EXISTS idx_vehicle_earmarks_vehicle
    ON corridor.vehicle_earmarks (vehicle_id, earmarked_at DESC);

CREATE INDEX IF NOT EXISTS idx_vehicle_earmarks_active
    ON corridor.vehicle_earmarks (retention_until)
    WHERE cleared_at IS NULL;

-- ── Sightings: one camera read of one plate (the ANPR plane) ───────────────
-- sighting_id is SUPPLIED by the generator (deterministic uuid5 over
-- use_case+seed+camera+plate, contract C2) — the same read re-delivered by
-- a replay upserts idempotently instead of forking rows.
-- matched_plate_hit labels generator-known hits (stolen_vehicle /
-- earmarked_plate) so the demo scenarios carry their evidence inline.

CREATE TABLE IF NOT EXISTS corridor.vehicle_sightings (
    sighting_id      UUID PRIMARY KEY,
    vehicle_id       UUID NOT NULL REFERENCES corridor.vehicles(vehicle_id)
                         ON DELETE CASCADE,
    plate            TEXT NOT NULL,
    camera_id        TEXT NOT NULL,
    t                TIMESTAMPTZ NOT NULL,
    position         GEOMETRY(Point, 4326) NOT NULL,
    speed_kmh        REAL,
    heading          REAL,
    vehicle_class    TEXT,
    source           TEXT NOT NULL CHECK (source IN
                         ('emulated_anpr', 'live_anpr')),
    run_id           BIGINT REFERENCES emulator.emulation_runs(run_id)
                         ON DELETE SET NULL,
    matched_plate_hit TEXT CHECK (matched_plate_hit IS NULL OR matched_plate_hit IN
                         ('stolen_vehicle', 'earmarked_plate')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vehicle_sightings_vehicle_t
    ON corridor.vehicle_sightings (vehicle_id, t DESC);

CREATE INDEX IF NOT EXISTS idx_vehicle_sightings_t
    ON corridor.vehicle_sightings (t);

CREATE INDEX IF NOT EXISTS idx_vehicle_sightings_position
    ON corridor.vehicle_sightings USING GIST (position);

-- =============================================================================
-- EVENTS SCHEMA — enforcement recommendations (e-challan audit trail)
-- =============================================================================

CREATE TABLE IF NOT EXISTS events.enforcement_recommendations (
    recommendation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sighting_id       UUID NOT NULL REFERENCES corridor.vehicle_sightings(sighting_id)
                         ON DELETE CASCADE,
    vehicle_id        UUID NOT NULL,
    rule_id           TEXT NOT NULL CHECK (rule_id IN (
                          'R_SPD_SEGMENT',
                          'R_SIGNAL_VIOLATION',
                          'R_CLOSED_CORRIDOR_ENTRY'
                      )),
    recommendation    TEXT NOT NULL,
    evidence          JSONB NOT NULL DEFAULT '{}'::jsonb,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                          'pending', 'approved', 'rejected'
                      )),
    decided_by        TEXT,
    decided_at        TIMESTAMPTZ,
    -- append-only audit tail: [{actor, action, at, note}] — creation,
    -- every human decision, every state change lands here (C6).
    audit_tail        JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sighting_id, rule_id)
);

CREATE INDEX IF NOT EXISTS idx_enforcement_recommendations_status
    ON events.enforcement_recommendations (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_enforcement_recommendations_vehicle
    ON events.enforcement_recommendations (vehicle_id);

-- =============================================================================
-- EVENT-KIND CHECK RELAXATION (contract C3 — exactly two additions)
-- guarded DROP IF EXISTS + re-ADD with the extended list; 007/008 stay
-- byte-immutable (ledger checksums).
-- =============================================================================

ALTER TABLE events.corridor_events
    DROP CONSTRAINT IF EXISTS corridor_events_event_kind_check;

ALTER TABLE events.corridor_events
    ADD CONSTRAINT corridor_events_event_kind_check CHECK (event_kind IN (
        'protected_movement', 'procession', 'mass_gathering',
        'race', 'ceremonial', 'egress_surge',
        'emergency_evacuation', 'work_zone',
        -- watchlist-anpr additions (010, C3):
        'crime_incident', 'vehicle_pursuit'
    ));

ALTER TABLE events.event_profiles
    DROP CONSTRAINT IF EXISTS event_profiles_event_kind_check;

ALTER TABLE events.event_profiles
    ADD CONSTRAINT event_profiles_event_kind_check CHECK (event_kind IN (
        'protected_movement', 'procession', 'mass_gathering',
        'race', 'ceremonial', 'egress_surge',
        'emergency_evacuation', 'work_zone',
        -- watchlist-anpr additions (010, C3):
        'crime_incident', 'vehicle_pursuit'
    ));

-- =============================================================================
-- EVENT PROFILE — the watchlist demo scenario template (idempotent seed)
-- The stolen-vehicle ANPR sweep: camera reads along the old-city belt with
-- a stolen plate and an earmarked plate among them, plus patrolling
-- agency units (Jan Rakshak).  Emulated end to end, source-labelled.
-- =============================================================================

INSERT INTO events.event_profiles (
    profile_id, event_kind, protection_class, crowd_class, name, description,
    scenario_id, route_segment_ids, departure_time, closure_policy,
    horizon_min, duration_class, template
) VALUES
    ('watchlist_anpr_sweep', 'crime_incident', 'none', 'LOW',
     'Stolen-Vehicle ANPR Sweep — Old-City Belt',
     'A stolen-vehicle report drives an emulated ANPR camera sweep along the '
     'old-city belt. Deterministic camera reads include the stolen plate and '
     'an earmarked plate (both clearly labelled emulated) plus patrolling '
     'Jan Rakshak units. Sightings land in corridor.vehicle_sightings with '
     'source emulated_anpr and drive deterministic e-challan recommendations '
     'that only a human officer can approve.',
     'watchlist_anpr_sweep', ARRAY[1012,1015,1005,1013],
     '2026-11-05T05:00:00Z'::timestamptz,
     '{"advance_closure_sec": 0, "closure_type": "signal_hold"}'::jsonb,
     45, 'minutes',
     '{"n_cameras": 4, "n_background_vehicles": 6, "stolen_plate_hit": true, '
     '"earmarked_plate_hit": true, "n_agency_units": 2, '
     '"seed": 20261105}'::jsonb)
ON CONFLICT (profile_id) DO NOTHING;
