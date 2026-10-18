-- 007_corridor_events.sql
-- Event-agnostic corridor events (generalization migration, core stream).
--
-- A corridor_event is a planned operation that takes control of part of the
-- road network for a window — the generalization of convoy.movements
-- (001_spatial_init.sql) covering all eight use cases: protected movements,
-- processions, mass gatherings, races, ceremonials, egress surges,
-- emergency evacuations and work zones.
--
-- Five orthogonal axes (architecture plan):
--   (1) protection_class  'Z+' | 'Z' | 'Y' | 'X' | 'none'   (none = public
--       event, no protectee; the VVIP base case keeps 'Z+' and behaves
--       identically — this migration is additive and backfills from
--       vvip_class, never rewrites it)
--   (2) crowd_class       'LOW'..'EXTREME' (first-class attribute)
--   (3) event_kind        8 archetypes; selects closure-policy/dossier
--                         templates, never code paths
--   (4) footprint         route spines as segment-id arrays + optional zone
--   (5) phases            (label, window, closure_policy) tuples
--
-- Also:
--   * corridor.protocol_params — DB mirror of the canonical
--     shared/schemas/protocol_params.json (seeded; the JSON remains the
--     single human-edited source, this table is the auditable copy)
--   * adaptive audit tables gain protection_class (005 CHECKs relaxed via
--     idempotent drop/re-add, backfilled from vvip_class)
--   * traffic.incidents gains event_id + additive event-domain incident
--     kinds alongside 'vvip_hold' (004 CHECK relaxed the same way)
--   * traffic.memory_docs gains event_id (soft, no FK — memory may outlive
--     events, same rule as movement_id)
--
-- Idempotent and additive-only: safe on fresh installs (initdb mount runs
-- 001..N in order, then the ledger re-runs them — every statement below is a
-- a no-op on re-run) and on existing installs (scripts/migrate.py ledger).
-- 001-006 are byte-immutable (checksum ledger); everything here is new.
--
-- GRAMMAR CONSTRAINT: these files are consumed BOTH by psql (initdb mount,
-- scripts/migrate.py) and by naive `;`-splitters (tests/test_migration_pgvector
-- applies them statement-by-statement via asyncpg). They must therefore stay
-- in the plain-statement subset: no dollar-quoted DO blocks, no block
-- comments, and no ';' inside string literals. Guarded-CHECK relaxation uses
-- `ALTER TABLE ... DROP CONSTRAINT IF EXISTS` (identical semantics to the
-- DO-block IF EXISTS pattern, single splittable statement).

-- =============================================================================
-- EVENTS SCHEMA
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS events;

CREATE TABLE IF NOT EXISTS events.corridor_events (
    event_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_kind       TEXT NOT NULL CHECK (event_kind IN (
                         'protected_movement', 'procession', 'mass_gathering',
                         'race', 'ceremonial', 'egress_surge',
                         'emergency_evacuation', 'work_zone'
                     )),
    protection_class TEXT NOT NULL DEFAULT 'none' CHECK (protection_class IN (
                         'Z+', 'Z', 'Y', 'X', 'none'
                     )),
    crowd_class      TEXT NOT NULL DEFAULT 'LOW' CHECK (crowd_class IN (
                         'LOW', 'MEDIUM', 'HIGH', 'EXTREME'
                     )),
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'planning' CHECK (status IN (
                         'planning', 'approved', 'active',
                         'completed', 'cancelled'
                     )),
    -- Optional soft link back to a legacy convoy movement this event
    -- generalizes (no FK: either side may be created independently).
    movement_id      UUID,
    planned_start    TIMESTAMPTZ,
    planned_end      TIMESTAMPTZ,
    actual_start     TIMESTAMPTZ,
    actual_end       TIMESTAMPTZ,
    -- Optional zone polygon (GeoJSON-free: PostGIS geometry like 001 uses)
    zone_geom        GEOMETRY(Polygon, 4326),
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (planned_end IS NULL OR planned_start IS NULL
           OR planned_end >= planned_start)
);

CREATE INDEX IF NOT EXISTS idx_corridor_events_status_start
    ON events.corridor_events (status, planned_start);

CREATE INDEX IF NOT EXISTS idx_corridor_events_kind
    ON events.corridor_events (event_kind);

-- ── Route spines: ordered segment-id arrays (planned_routes pattern) ────────

CREATE TABLE IF NOT EXISTS events.event_routes (
    route_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id         UUID NOT NULL REFERENCES events.corridor_events(event_id)
                         ON DELETE CASCADE,
    route_rank       SMALLINT NOT NULL DEFAULT 1,
    label            TEXT NOT NULL DEFAULT 'spine',
    segment_ids      BIGINT[] NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_event_routes_event
    ON events.event_routes (event_id, route_rank);

-- ── Phases: (label, window, closure_policy) from minutes to multi-week ──────

CREATE TABLE IF NOT EXISTS events.event_phases (
    phase_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id         UUID NOT NULL REFERENCES events.corridor_events(event_id)
                         ON DELETE CASCADE,
    phase_rank       SMALLINT NOT NULL DEFAULT 1,
    label            TEXT NOT NULL,
    window_start     TIMESTAMPTZ NOT NULL,
    window_end       TIMESTAMPTZ NOT NULL,
    closure_policy   JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (window_end >= window_start)
);

CREATE INDEX IF NOT EXISTS idx_event_phases_event
    ON events.event_phases (event_id, window_start);

-- =============================================================================
-- CORRIDOR.PROTOCOL_PARAMS — auditable DB mirror of the canonical JSON
-- (shared/schemas/protocol_params.json remains the single human-edited
-- source; scripts/export_protocol_params.py generates the runtime modules).
-- =============================================================================

CREATE TABLE IF NOT EXISTS corridor.protocol_params (
    protection_class        TEXT PRIMARY KEY
                            CHECK (protection_class IN ('Z+','Z','Y','X','none')),
    advance_closure_floor_sec INTEGER,   -- NULL = no floor (X, none)
    security_points         INTEGER NOT NULL,
    min_lanes               INTEGER NOT NULL,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO corridor.protocol_params (
    protection_class, advance_closure_floor_sec, security_points, min_lanes
) VALUES
    ('Z+',   180, 6, 6),
    ('Z',    120, 4, 4),
    ('Y',     60, 2, 2),
    ('X',   NULL, 0, 1),
    ('none', NULL, 0, 0)
ON CONFLICT (protection_class) DO NOTHING;

-- =============================================================================
-- ADAPTIVE AUDIT TABLES — protection_class (005 CHECKs relaxed idempotently)
-- =============================================================================

-- situation_log: add protection_class, backfill from vvip_class, and relax
-- the legacy vvip_class CHECK so public events ('none') can be audited.
-- (DROP CONSTRAINT IF EXISTS: the plain-statement equivalent of the
-- DO-block IF-EXISTS guard — see GRAMMAR CONSTRAINT in the header.)
ALTER TABLE adaptive.situation_log
    ADD COLUMN IF NOT EXISTS protection_class TEXT;

ALTER TABLE adaptive.situation_log
    DROP CONSTRAINT IF EXISTS situation_log_protection_class_check;

ALTER TABLE adaptive.situation_log
    ADD CONSTRAINT situation_log_protection_class_check
    CHECK (protection_class IS NULL OR protection_class IN
           ('Z+', 'Z', 'Y', 'X', 'none'));

ALTER TABLE adaptive.situation_log
    DROP CONSTRAINT IF EXISTS situation_log_vvip_class_check;

ALTER TABLE adaptive.situation_log
    ADD CONSTRAINT situation_log_vvip_class_check
    CHECK (vvip_class IN ('Z+', 'Z', 'Y', 'X', 'none'));

UPDATE adaptive.situation_log
SET protection_class = vvip_class
WHERE protection_class IS NULL;

-- param_overrides: same treatment.
ALTER TABLE adaptive.param_overrides
    ADD COLUMN IF NOT EXISTS protection_class TEXT;

ALTER TABLE adaptive.param_overrides
    DROP CONSTRAINT IF EXISTS param_overrides_protection_class_check;

ALTER TABLE adaptive.param_overrides
    ADD CONSTRAINT param_overrides_protection_class_check
    CHECK (protection_class IS NULL OR protection_class IN
           ('Z+', 'Z', 'Y', 'X', 'none'));

ALTER TABLE adaptive.param_overrides
    DROP CONSTRAINT IF EXISTS param_overrides_vvip_class_check;

ALTER TABLE adaptive.param_overrides
    ADD CONSTRAINT param_overrides_vvip_class_check
    CHECK (vvip_class IN ('Z+', 'Z', 'Y', 'X', 'none'));

UPDATE adaptive.param_overrides
SET protection_class = vvip_class
WHERE protection_class IS NULL;

-- =============================================================================
-- TRAFFIC.INCIDENTS — event linkage + additive event-domain kinds
-- ('vvip_hold' stays; these are ADDITIVE members of the 004 CHECK)
-- =============================================================================

ALTER TABLE traffic.incidents
    ADD COLUMN IF NOT EXISTS event_id UUID;

ALTER TABLE traffic.incidents
    DROP CONSTRAINT IF EXISTS incidents_incident_kind_check;

ALTER TABLE traffic.incidents
    ADD CONSTRAINT incidents_incident_kind_check CHECK (incident_kind IN (
        'congestion_spike', 'route_blockage', 'signal_failure',
        'protest', 'accident', 'weather', 'vvip_hold', 'other',
        -- event-agnostic additions (007):
        'crowd_surge', 'egress_surge', 'ambulance_hold', 'work_zone'
    ));

CREATE INDEX IF NOT EXISTS idx_incidents_event
    ON traffic.incidents (event_id)
    WHERE event_id IS NOT NULL;

-- memory_docs: event linkage (soft — memory may outlive events).
ALTER TABLE traffic.memory_docs
    ADD COLUMN IF NOT EXISTS event_id UUID;

CREATE INDEX IF NOT EXISTS idx_memory_docs_event
    ON traffic.memory_docs (event_id)
    WHERE event_id IS NOT NULL;
