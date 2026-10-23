-- 019_hourly_aggregates_synthetic.sql
-- Synthetic hourly rollup, kept apart from the field baseline (WP-19,
-- wave 3, contract C-P).
--
-- SCHEMA ONLY, additive, idempotent: safe on fresh installs (before the
-- seed) and on the ledgered live install.  Creates one new table and
-- touches nothing that exists.
--
--   traffic.hourly_aggregates_synthetic - the same columns and key as
--   001's traffic.hourly_aggregates, rolled up hourly by the oracle's
--   partition maintenance (traffic_oracle/ingest/partitions.py) from
--   data_quality = 'synthetic' observations only: the integrated
--   signal-ingress feeds (simulated) and the event-emulator showcase.
--   GET /api/v1/traffic/historical serves it as synthetic_aggregates,
--   every row labelled data_quality 'synthetic', NEXT TO (never merged
--   into) the real aggregates.  Rows older than the observation retention
--   window are deleted by the same maintenance pass.
--
--   traffic.hourly_aggregates stays REAL-only: it is the twin's field
--   calibration input (simulation/calibration.py), the pattern matcher's
--   and the vector store's history.  NOTHING that calibrates, trains or
--   fits may read this table - the table comment says so where an
--   operator will see it, and the oracle's test suite fails if any
--   calibration, twin, pattern or vector SQL names it.
--
-- Ethics rails (C6): aggregate speeds per road segment only - no vehicle,
-- plate, frame or person-directed column.
--
-- Grammar constraint (007 header): consumed by naive ';'-splitters - no
-- ';' inside string literals, no dollar-quoted bodies, no block comments.

CREATE TABLE IF NOT EXISTS traffic.hourly_aggregates_synthetic (
    segment_id      BIGINT NOT NULL,
    hour_utc        TIMESTAMPTZ NOT NULL,
    avg_speed_kmh   REAL,
    p50_speed_kmh   REAL,
    p95_congestion  REAL,
    observation_cnt INTEGER,
    PRIMARY KEY (segment_id, hour_utc)
);

CREATE INDEX IF NOT EXISTS idx_hourly_aggregates_synthetic_hour
    ON traffic.hourly_aggregates_synthetic (hour_utc);

COMMENT ON TABLE traffic.hourly_aggregates_synthetic IS
    'synthetic rollup — never a calibration input';
