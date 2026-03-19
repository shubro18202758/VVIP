-- 003_anomaly_tracking.sql
-- Anomaly logging, synthetic observation storage, and data quality tracking.
-- Supports two-tier anomaly detection (Rust inline + Python Isolation Forest).

-- =============================================================================
-- TRAFFIC SCHEMA: Anomaly log
-- =============================================================================

CREATE TABLE traffic.anomaly_log (
    anomaly_id      BIGSERIAL PRIMARY KEY,
    segment_id      BIGINT NOT NULL,
    timestamp_utc   TIMESTAMPTZ NOT NULL,
    anomaly_type    TEXT NOT NULL CHECK (anomaly_type IN (
        'future_timestamp', 'stale_data', 'speed_jump', 'phantom_reading',
        'speed_zscore', 'congestion_outlier', 'road_class_mismatch',
        'spatial_mismatch', 'isolation_forest'
    )),
    severity        TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
    details         JSONB DEFAULT '{}',
    flagged_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_anomaly_log_segment_time
    ON traffic.anomaly_log (segment_id, timestamp_utc DESC);
CREATE INDEX idx_anomaly_log_type
    ON traffic.anomaly_log (anomaly_type);

-- =============================================================================
-- TRAFFIC SCHEMA: Synthetic observations (gap-filling generated data)
-- =============================================================================

CREATE TABLE traffic.synthetic_observations (
    synthetic_id        BIGSERIAL PRIMARY KEY,
    segment_id          BIGINT NOT NULL,
    timestamp_utc       TIMESTAMPTZ NOT NULL,
    speed_kmh           REAL NOT NULL,
    congestion_idx      REAL NOT NULL CHECK (congestion_idx BETWEEN 0.0 AND 1.0),
    generation_method   TEXT NOT NULL CHECK (generation_method IN (
        'historical_match', 'spatial_interpolation', 'temporal_pattern', 'default_fallback'
    )),
    confidence          REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    source_segments     BIGINT[],
    generated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_synthetic_segment_time
    ON traffic.synthetic_observations (segment_id, timestamp_utc DESC);

-- =============================================================================
-- Extend existing observations table with data quality tracking
-- =============================================================================

ALTER TABLE traffic.observations
    ADD COLUMN IF NOT EXISTS data_quality TEXT DEFAULT 'real'
        CHECK (data_quality IN ('real', 'anomalous', 'synthetic'));

ALTER TABLE traffic.observations
    ADD COLUMN IF NOT EXISTS confidence REAL DEFAULT 1.0
        CHECK (confidence BETWEEN 0.0 AND 1.0);
