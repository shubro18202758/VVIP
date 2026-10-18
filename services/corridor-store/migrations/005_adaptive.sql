-- 005_adaptive.sql
-- Adaptive (T2) audit tables: situation classification log and published
-- parameter overrides.  Idempotent and additive-only; applied by
-- VVIP/scripts/migrate.py (models-owned runner, C10-iv) in lexical order
-- after 001-004.  All runtime writes use parameter-bound INSERTs
-- (traffic_oracle/adaptive/persistence.py) — this file is static DDL.

CREATE SCHEMA IF NOT EXISTS adaptive;

-- =============================================================================
-- adaptive.situation_log — one row per observation fed to the
-- SituationEngine (classifier + hysteresis), for audit and AAR.
-- =============================================================================

CREATE TABLE IF NOT EXISTS adaptive.situation_log (
    log_id              BIGSERIAL PRIMARY KEY,
    movement_id         TEXT NOT NULL,
    vvip_class          TEXT NOT NULL CHECK (vvip_class IN ('Z+', 'Z', 'Y', 'X')),
    situation_code      TEXT NOT NULL,
    situation_class     TEXT NOT NULL CHECK (situation_class IN (
                            'GREEN', 'AMBER', 'RED', 'BLACK'
                        )),
    severity_score      INT  NOT NULL CHECK (severity_score BETWEEN 0 AND 21),
    inputs              JSONB NOT NULL,
    observed_class      TEXT NOT NULL CHECK (observed_class IN (
                            'GREEN', 'AMBER', 'RED', 'BLACK'
                        )),
    transitioned        BOOLEAN NOT NULL DEFAULT FALSE,
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_adaptive_situation_movement_time
    ON adaptive.situation_log (movement_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_adaptive_situation_class_time
    ON adaptive.situation_log (situation_class, observed_at DESC);

-- =============================================================================
-- adaptive.param_overrides — one row per published parameter set (the C4
-- response).  source='baseline' rows record fail-closed publishes verbatim;
-- shadow_eval only ever stores 'passed' or 'unavailable' (the C4 schema
-- values) — a failed shadow-eval is recorded as 'unavailable' with the
-- precise outcome inside evidence.
-- =============================================================================

CREATE TABLE IF NOT EXISTS adaptive.param_overrides (
    override_id         BIGSERIAL PRIMARY KEY,
    movement_id         TEXT NOT NULL,
    vvip_class          TEXT NOT NULL CHECK (vvip_class IN ('Z+', 'Z', 'Y', 'X')),
    situation_class     TEXT NOT NULL CHECK (situation_class IN (
                            'GREEN', 'AMBER', 'RED', 'BLACK'
                        )),
    params              JSONB NOT NULL,
    source              TEXT NOT NULL CHECK (source IN (
                            'baseline', 'retrieved', 'learned'
                        )),
    shadow_eval         TEXT NOT NULL CHECK (shadow_eval IN (
                            'passed', 'unavailable'
                        )),
    evidence            JSONB NOT NULL DEFAULT '{}'::jsonb,
    published_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_adaptive_params_movement_time
    ON adaptive.param_overrides (movement_id, published_at DESC);

CREATE INDEX IF NOT EXISTS idx_adaptive_params_source_time
    ON adaptive.param_overrides (source, published_at DESC);
