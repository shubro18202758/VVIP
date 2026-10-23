-- 018_event_routes_supersede.sql
-- Corridor-event route updates: superseded spines + their provenance
-- (WP-19, wave 3, contract C-O).
--
-- SCHEMA ONLY, additive, idempotent: safe on fresh installs (before the
-- seed) and on the ledgered live install.  Touches only 007's
-- events.event_routes.
--
--   events.event_routes gains three nullable columns:
--     superseded_at  when POST /api/v1/events/{id}/routes replaced this
--                    spine (NULL = the spine is current).  Rows are never
--                    deleted: a superseded spine stays as the event's
--                    route_history, so an after-action review sees every
--                    route the operation ran on and when it changed.
--     variant        the re-route label of a spine written by that
--                    endpoint ('reroute-N' | 'plan_b'), NULL for the
--                    spines the event was created with.
--     created_by     the named actor (the deck-verified officer, or a
--                    system actor such as 'convoy-brain (system)') who
--                    wrote the spine through that endpoint, NULL for
--                    spines written at creation (event_status_log names
--                    the creator).
--   idx_event_routes_current - the readers (situation engine, city
--   snapshot, dossier, e-challan closure test, unit projection) read the
--   CURRENT spines only (superseded_at IS NULL).
--
-- Nothing is back-filled: every existing spine is current, exactly as
-- before.  The oracle probes superseded_at before using it, so its
-- readers work on a store without this migration.
--
-- Ethics rails (C6): created_by holds duty-officer or system-actor names
-- only - no plate, frame or citizen-directed column is added.
--
-- Grammar constraint (007 header): consumed by naive ';'-splitters - no
-- ';' inside string literals, no dollar-quoted bodies, no block comments.

ALTER TABLE events.event_routes
    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;

ALTER TABLE events.event_routes
    ADD COLUMN IF NOT EXISTS variant TEXT;

ALTER TABLE events.event_routes
    ADD COLUMN IF NOT EXISTS created_by TEXT;

CREATE INDEX IF NOT EXISTS idx_event_routes_current
    ON events.event_routes (event_id, route_rank)
    WHERE superseded_at IS NULL;
