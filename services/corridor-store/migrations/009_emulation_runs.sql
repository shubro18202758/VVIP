-- 009_emulation_runs.sql
-- Emulator cause-tape ledger (core stream; contract C4).
--
-- A run = {emulation_spec, seed, t0_anchor, clock_scale}; same spec+seed →
-- identical tape (derive_seed blake2b pattern).  This table ledgers the
-- spec-hash and the tape itself for replay (re-posting the tape with a
-- re-anchored t0).
--
-- Deliberately NOT simulation.runs: that table (006, byte-immutable) is
-- outcome-semantics-only (per-replication C5 run rows; 004 derives
-- incidents from it) and nothing in this design writes it.

CREATE SCHEMA IF NOT EXISTS emulator;

CREATE TABLE IF NOT EXISTS emulator.emulation_runs (
    run_id         BIGSERIAL PRIMARY KEY,
    spec_hash      TEXT NOT NULL,        -- sha256 of the canonical spec JSON
    spec           JSONB NOT NULL,       -- the emulation_spec verbatim
    seed           BIGINT NOT NULL,
    t0_anchor      TIMESTAMPTZ NOT NULL,
    clock_scale    REAL NOT NULL DEFAULT 1.0 CHECK (clock_scale > 0),
    status         TEXT NOT NULL DEFAULT 'running' CHECK (status IN (
                       'running', 'completed', 'failed', 'stopped'
                   )),
    tape           JSONB NOT NULL DEFAULT '[]'::jsonb,  -- ordered cause rows
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_emulation_runs_spec
    ON emulator.emulation_runs (spec_hash, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_emulation_runs_status
    ON emulator.emulation_runs (status);
