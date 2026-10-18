-- 006_simulation_runs.sql
-- Simulation twin persistence + junction signal programs (contracts C5/C6/C12).
--
-- Idempotent and additive-only: safe to re-run on existing installs
-- (applied by VVIP/scripts/migrate.py in lexical order after 001-005).
--
-- C6 note: corridor.junctions.signal_control is BOOLEAN (001_spatial_init.sql:44)
-- and carries no phase data. Signal timing lives HERE, in
-- corridor.signal_programs, seeded with road-class assumptions
-- (source = 'osm_assumed'); /api/v1/simulate/replay fits cycle/splits against
-- observed queueing and flips source -> 'calibrated'. The twin consumes
-- signal_programs, never junctions.signal_control.

-- =============================================================================
-- SIMULATION SCHEMA: twin run + calibration ledgers
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS simulation;

CREATE TABLE IF NOT EXISTS simulation.runs (
    run_id              BIGSERIAL PRIMARY KEY,
    scenario_id         TEXT,
    seed                BIGINT NOT NULL,
    route_segment_ids   BIGINT[] NOT NULL,
    departure_time      TIMESTAMPTZ NOT NULL,
    horizon_min         SMALLINT NOT NULL CHECK (horizon_min BETWEEN 1 AND 60),
    n_replications      SMALLINT NOT NULL CHECK (n_replications BETWEEN 1 AND 20),
    closure_policy      JSONB NOT NULL,
    forecast_coupling   TEXT NOT NULL DEFAULT 'internal'
                        CHECK (forecast_coupling IN ('internal', 'dstgat')),
    calibrated          BOOLEAN NOT NULL DEFAULT FALSE,
    metrics             JSONB NOT NULL DEFAULT '{}',   -- per-replication C5 run rows
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_simulation_runs_scenario
    ON simulation.runs (scenario_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_simulation_runs_seed
    ON simulation.runs (seed);

CREATE TABLE IF NOT EXISTS simulation.calibrations (
    calibration_id      BIGSERIAL PRIMARY KEY,
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    n_observations      INTEGER NOT NULL,
    speed_mae_kmh       REAL NOT NULL,
    capacity_scale      REAL NOT NULL DEFAULT 1.0,
    per_road_class_scale JSONB NOT NULL DEFAULT '{}',
    calibrated          BOOLEAN NOT NULL DEFAULT FALSE,
    details             JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_simulation_calibrations_window
    ON simulation.calibrations (window_start DESC);

-- =============================================================================
-- CORRIDOR SCHEMA: junction signal programs (C6)
-- phases: [{"green_sec": 42, "movements": ["NS"]},
--          {"green_sec": 38, "movements": ["EW"]}]  -- +amber lost time = cycle
-- =============================================================================

CREATE TABLE IF NOT EXISTS corridor.signal_programs (
    junction_id     BIGINT NOT NULL REFERENCES corridor.junctions(junction_id) ON DELETE CASCADE,
    cycle_sec       INTEGER NOT NULL CHECK (cycle_sec BETWEEN 40 AND 240),
    phases          JSONB NOT NULL CHECK (jsonb_typeof(phases) = 'array'),
    offset_sec      INTEGER NOT NULL DEFAULT 0 CHECK (offset_sec >= 0),
    source          TEXT NOT NULL DEFAULT 'osm_assumed'
                    CHECK (source IN ('osm_assumed', 'calibrated')),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (junction_id)
);

CREATE INDEX IF NOT EXISTS idx_signal_programs_source
    ON corridor.signal_programs (source);

-- ─── Seed with road-class assumptions ───────────────────────────────────────
-- For every signal_control=TRUE junction with no program yet, derive the
-- dominant road class of its adjacent segments and install the class-default
-- cycle + two-phase split. Junctions with no adjacency default to the
-- residential program (aggregate over zero rows yields one NULL row, which
-- the CASE chain routes to ELSE). Idempotent via ON CONFLICT DO NOTHING.

INSERT INTO corridor.signal_programs (junction_id, cycle_sec, phases, offset_sec, source)
SELECT
    j.junction_id,
    (prog.program->>'cycle_sec')::INTEGER,
    prog.program->'phases',
    -- deterministic per-junction offset within the cycle (progression offset)
    (j.junction_id * 17) % (prog.program->>'cycle_sec')::INTEGER,
    'osm_assumed'
FROM corridor.junctions j
CROSS JOIN LATERAL (
    SELECT
        CASE
            WHEN bool_or(rs.road_class = 'motorway') THEN
                '{"cycle_sec": 90, "phases": [{"green_sec": 44, "movements": ["through"]}, {"green_sec": 34, "movements": ["ramp", "cross"]}]}'::jsonb
            WHEN bool_or(rs.road_class = 'trunk') THEN
                '{"cycle_sec": 100, "phases": [{"green_sec": 47, "movements": ["through"]}, {"green_sec": 38, "movements": ["cross"]}]}'::jsonb
            WHEN bool_or(rs.road_class = 'primary') THEN
                '{"cycle_sec": 110, "phases": [{"green_sec": 50, "movements": ["through"]}, {"green_sec": 42, "movements": ["cross"]}]}'::jsonb
            WHEN bool_or(rs.road_class = 'secondary') THEN
                '{"cycle_sec": 120, "phases": [{"green_sec": 54, "movements": ["through"]}, {"green_sec": 46, "movements": ["cross"]}]}'::jsonb
            ELSE
                '{"cycle_sec": 120, "phases": [{"green_sec": 40, "movements": ["local"]}, {"green_sec": 34, "movements": ["cross"]}]}'::jsonb
        END AS program
    FROM corridor.segment_adjacency sa
    JOIN corridor.road_segments rs ON rs.segment_id = sa.from_segment_id
    WHERE sa.via_junction_id = j.junction_id
) AS prog
WHERE j.signal_control = TRUE
ON CONFLICT (junction_id) DO NOTHING;
