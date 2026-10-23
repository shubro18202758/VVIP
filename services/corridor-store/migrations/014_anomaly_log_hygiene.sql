-- 014_anomaly_log_hygiene.sql
-- Anomaly-log read path + dispatch audit attribution (WP-16).
--
-- SCHEMA ONLY, additive, idempotent (IF NOT EXISTS everywhere), no seed
-- reference: safe on fresh installs (before the seed) and on the ledgered
-- live install.  Independent of 012/013: it touches only 003's
-- traffic.anomaly_log and 011's corridor.dispatch_events.
--
--   1. idx_anomaly_log_detected — traffic.anomaly_log by DETECTION time.
--      GET /api/v1/anomalies/recent filters and orders on
--      COALESCE(flagged_at, timestamp_utc) (a stale_data anomaly carries an
--      old observation timestamp, so recency is when it was flagged), with
--      anomaly_id as the tie-break: this expression index serves both the
--      range and the order without a sort.  The hourly retention pass
--      (traffic_oracle/ingest/partitions.py) deletes the oldest rows past
--      VVIP_OBSERVATION_RETENTION_DAYS through the same expression.  003's
--      (segment_id, timestamp_utc) index keeps serving the per-segment
--      15-minute reads of the city surfaces.
--
--   2. corridor.dispatch_events.detail — the provenance of an audit row's
--      actor.  When the deck forwards a signed-in operator (the verified
--      x-veer-officer header) the oracle records that officer as the actor
--      and keeps {actor, actor_source, claimed_officer?} here: the body's
--      officer survives as a claim whenever it differs.  Nullable and
--      never back-filled: rows written before 014, and rows attributed
--      from the request body alone, stay NULL.  The oracle reads it as
--      to_jsonb(row) -> 'detail', so its read path does not depend on this
--      migration, and it probes the column before writing it.
--
-- Ethics rails (C6): no plate, frame or citizen-directed column is added -
-- detail carries only duty-officer names (the verified actor and, when it
-- differs, the name the request body claimed).
--
-- Grammar constraint (007 header): consumed by naive ';'-splitters - no
-- ';' inside string literals, no dollar-quoted bodies, no block comments.

-- =============================================================================
-- 1. ANOMALY LOG BY DETECTION TIME
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_anomaly_log_detected
    ON traffic.anomaly_log ((COALESCE(flagged_at, timestamp_utc)) DESC, anomaly_id DESC);

-- =============================================================================
-- 2. DISPATCH AUDIT ATTRIBUTION
-- =============================================================================

ALTER TABLE corridor.dispatch_events
    ADD COLUMN IF NOT EXISTS detail JSONB;
