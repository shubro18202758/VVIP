-- 013_event_lifecycle_audit.sql
-- Corridor-event lifecycle ledger + signal-writer attribution (WP-02).
--
-- Additive, schema-only (no data rows, no back-fill):
--
--   1. events.event_status_log
--      Append-only ledger of every corridor-event status: the creation row
--      (from_status NULL -> 'planning', written by POST /api/v1/events) and
--      every transition planning -> approved -> active -> completed |
--      cancelled (written by POST /api/v1/events/{id}/status in the SAME
--      transaction as the status UPDATE and the auto-close of the event's
--      open dispatches).  actor is the named human officer, or a system
--      actor string such as 'convoy-brain (system)'.  Same audit posture as
--      corridor.dispatch_events (011): every state change is a row.
--
--   2. corridor.signal_program_history.actor
--      The named officer who applied (first-write snapshot rows) or
--      restored (restore rows) a city signal plan.  Nullable: rows written
--      before 013, and writes made without an officer while
--      VVIP_REQUIRE_OFFICER is unset, stay NULL - never back-filled with a
--      guessed name.
--
--   3. corridor.signal_plan_log
--      One row per COMMITTED city signal-plan apply, written in the apply's
--      own transaction: band, junctions written, first-write snapshots
--      taken, the twin shadow verdict and the named actor.  The C7 history
--      ledger only records a junction's FIRST calibrated write (the
--      snapshot a restore needs), so a later apply over already
--      snapshotted junctions - the GREEN -> AMBER -> RED escalation -
--      used to leave no attributed row at all.  Every signal write is now
--      a row with its officer (NULL only under the same rule as 2.).
--
--   4. Read-path indexes for the city fusion surfaces (GET /api/v1/cameras,
--      /api/v1/city/entities, /api/v1/city/feed):
--        idx_vehicle_sightings_camera_t   per-camera recency (reads_15m,
--                                         hits_15m, last_read_t) without a
--                                         table scan per poll;
--        idx_situation_log_transitions    band transitions only (the feed's
--                                         'ama' rows), partial on
--                                         transitioned.
--
-- Ethics rails (C6): the ledger records statuses and named actors of
-- corridor events only - no person-directed or vehicle-identifying field.
--
-- Idempotent (IF NOT EXISTS everywhere) and safe on fresh installs and on
-- the ledgered live install.  Independent of 012: nothing here references
-- objects created after 011.  Grammar constraint (007 header): consumed by
-- naive ';'-splitters - no ';' inside string literals, no dollar-quoted
-- bodies, no block comments.

-- =============================================================================
-- CORRIDOR-EVENT LIFECYCLE LEDGER
-- =============================================================================

CREATE TABLE IF NOT EXISTS events.event_status_log (
    seq           BIGSERIAL PRIMARY KEY,
    event_id      UUID NOT NULL REFERENCES events.corridor_events(event_id)
                      ON DELETE CASCADE,
    -- NULL only on the creation row (the event entered 'planning').
    from_status   TEXT CHECK (from_status IS NULL OR from_status IN (
                      'planning', 'approved', 'active',
                      'completed', 'cancelled'
                  )),
    to_status     TEXT NOT NULL CHECK (to_status IN (
                      'planning', 'approved', 'active',
                      'completed', 'cancelled'
                  )),
    actor         TEXT NOT NULL,
    note          TEXT,
    at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_event_status_log_event
    ON events.event_status_log (event_id, seq);

CREATE INDEX IF NOT EXISTS idx_event_status_log_at
    ON events.event_status_log (at DESC);

-- =============================================================================
-- SIGNAL-WRITER ATTRIBUTION (C7 ledger gains the named actor)
-- =============================================================================

ALTER TABLE corridor.signal_program_history
    ADD COLUMN IF NOT EXISTS actor TEXT;

-- One attributed row per committed apply (event_id carries no FK, like
-- signal_program_history.event_id: a plan may serve no corridor event).
CREATE TABLE IF NOT EXISTS corridor.signal_plan_log (
    plan_id            BIGSERIAL PRIMARY KEY,
    event_id           UUID,
    band               TEXT NOT NULL CHECK (band IN (
                           'GREEN', 'AMBER', 'RED', 'BLACK'
                       )),
    junction_ids       BIGINT[] NOT NULL,
    snapshots_written  INTEGER NOT NULL CHECK (snapshots_written >= 0),
    shadow_eval        TEXT NOT NULL,
    actor              TEXT,
    applied_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signal_plan_log_applied
    ON corridor.signal_plan_log (applied_at DESC);

CREATE INDEX IF NOT EXISTS idx_signal_plan_log_event
    ON corridor.signal_plan_log (event_id, applied_at);

-- =============================================================================
-- CITY FUSION READ-PATH INDEXES
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_vehicle_sightings_camera_t
    ON corridor.vehicle_sightings (camera_id, t DESC);

CREATE INDEX IF NOT EXISTS idx_situation_log_transitions
    ON adaptive.situation_log (observed_at DESC)
    WHERE transitioned;
