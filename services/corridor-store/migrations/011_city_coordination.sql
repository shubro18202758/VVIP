-- 011_city_coordination.sql
-- City coordination layer (signalling-janrakshak stream).
--
-- Two disjoint additions owned by this migration:
--
--   1. corridor.dispatches + corridor.dispatch_events
--      The agency dispatch channel.  A dispatch mirrors the green-corridor
--      request pattern: an operator (or an event-driven recommendation)
--      REQUESTS a unit of an agency (Jan Rakshak first-class); a HUMAN duty
--      officer APPROVES or DEFERS; the lifecycle then walks
--      en_route -> on_scene -> completed (or cancelled).  Every state
--      change appends one row to the append-only corridor.dispatch_events
--      audit tail (actor, from/to, note, timestamp) — the same audit
--      posture as adaptive.situation_log.
--
--   2. corridor.signal_program_history
--      Safety ledger for the city signal-plan writer (contract C7).
--      corridor.signal_programs is PK(junction_id): a writer commit with
--      source='calibrated' REPLACES the seeded osm_assumed row.  The FIRST
--      calibrated write per junction therefore snapshots the prior program
--      here, and POST /api/v1/signals/restore rebuilds the osm_assumed
--      rows from those snapshots (demo state pollution is reversible).
--      rows with kind='restore' record each restore act (append-only).
--
-- Ethics rails (C6): dispatches reference VEHICLES only through the event
-- metadata (event_id soft-links events.corridor_events); no person fields
-- exist in this schema.
--
-- Idempotent and additive-only: applied by VVIP/scripts/migrate.py in
-- lexical order after 001-010.  Grammar constraint (007 header): consumed
-- by naive ';'-splitters — no ';' inside string literals, no dollar-quoted
-- bodies, no block comments.

-- =============================================================================
-- AGENCY DISPATCH CHANNEL
-- =============================================================================

CREATE TABLE IF NOT EXISTS corridor.dispatches (
    dispatch_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id          UUID NOT NULL REFERENCES events.corridor_events(event_id)
                          ON DELETE CASCADE,
    -- Jan Rakshak is a first-class agency actor; the list is closed so the
    -- canvas/ops board can badge every unit deterministically.
    agency            TEXT NOT NULL CHECK (agency IN (
                          'jan_rakshak', 'police', 'traffic',
                          'ambulance', 'fire'
                      )),
    unit_id           TEXT NOT NULL,
    -- Waypoint the unit is dispatched to (interception choke junction,
    -- zone corner, incident node).  Nullable: a dispatch may be zone-scoped.
    waypoint_junction BIGINT REFERENCES corridor.junctions(junction_id),
    -- requested -> (approved | deferred) by a HUMAN officer; approved ->
    -- en_route -> on_scene -> completed; cancelled from any live state.
    status            TEXT NOT NULL DEFAULT 'requested' CHECK (status IN (
                          'requested', 'approved', 'deferred',
                          'en_route', 'on_scene', 'completed', 'cancelled'
                      )),
    requested_by      TEXT NOT NULL DEFAULT 'operator',
    decided_by        TEXT,
    request_payload   JSONB NOT NULL DEFAULT '{}'::jsonb,
    decision_note     TEXT,
    approved_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dispatches_event
    ON corridor.dispatches (event_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_dispatches_agency_status
    ON corridor.dispatches (agency, status);

CREATE INDEX IF NOT EXISTS idx_dispatches_unit
    ON corridor.dispatches (unit_id, created_at DESC);

-- Append-only audit tail: one row per dispatch state change.
CREATE TABLE IF NOT EXISTS corridor.dispatch_events (
    seq           BIGSERIAL PRIMARY KEY,
    dispatch_id   UUID NOT NULL REFERENCES corridor.dispatches(dispatch_id)
                      ON DELETE CASCADE,
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    actor         TEXT NOT NULL DEFAULT 'system',
    note          TEXT,
    at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dispatch_events_dispatch
    ON corridor.dispatch_events (dispatch_id, seq);

-- =============================================================================
-- SIGNAL PROGRAM SAFETY LEDGER (C7)
-- =============================================================================

CREATE TABLE IF NOT EXISTS corridor.signal_program_history (
    history_id   BIGSERIAL PRIMARY KEY,
    junction_id  BIGINT NOT NULL REFERENCES corridor.junctions(junction_id)
                     ON DELETE CASCADE,
    -- Soft link to the corridor event the plan application served (NULL =
    -- a city-wide band application with no single event).  No FK: history
    -- must outlive event rows.
    event_id     UUID,
    kind         TEXT NOT NULL DEFAULT 'snapshot' CHECK (kind IN (
                     'snapshot', 'restore'
                 )),
    -- The PRIOR program, verbatim, at the moment of the first calibrated
    -- write to this junction (source here is the PRIOR row's source).
    cycle_sec    INTEGER NOT NULL CHECK (cycle_sec BETWEEN 40 AND 240),
    phases       JSONB NOT NULL CHECK (jsonb_typeof(phases) = 'array'),
    offset_sec   INTEGER NOT NULL,
    source       TEXT NOT NULL CHECK (source IN ('osm_assumed', 'calibrated')),
    reason       TEXT NOT NULL DEFAULT '',
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signal_program_history_junction
    ON corridor.signal_program_history (junction_id, captured_at);

CREATE INDEX IF NOT EXISTS idx_signal_program_history_event
    ON corridor.signal_program_history (event_id, captured_at);
