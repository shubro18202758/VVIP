-- 016_emulation_runs_interrupted.sql
-- Emulator run ledger: the 'interrupted' status (WP-19, wave 3).
--
-- SCHEMA ONLY, additive, idempotent: safe on fresh installs (before the
-- seed) and on the ledgered live install.  Touches only 009's
-- emulator.emulation_runs.
--
--   emulator.emulation_runs.status gains 'interrupted' - a run whose
--   process stopped mid-tape (oracle or emulator restart, host reboot)
--   without being stopped by an operator or finishing its tape.  Until
--   this lands the emulator records such runs as 'stopped' (event-emulator
--   store.py probes the CHECK every 5 min and switches over by itself - no
--   restart).  Existing rows keep their status: the four values 009
--   allowed are all still allowed, so re-adding the CHECK validates them.
--
-- Ethics rails (C6): no data column is added - the ledger keeps run
-- metadata and the tape only, never a person-directed field.
--
-- Grammar constraint (007 header): consumed by naive ';'-splitters - no
-- ';' inside string literals, no dollar-quoted bodies, no block comments.
-- Re-applying is a no-op: the guarded DROP removes the constraint the
-- previous apply added and the ADD recreates it identically.

ALTER TABLE emulator.emulation_runs
    DROP CONSTRAINT IF EXISTS emulation_runs_status_check;

ALTER TABLE emulator.emulation_runs
    ADD CONSTRAINT emulation_runs_status_check CHECK (status IN (
        'running', 'completed', 'failed', 'stopped', 'interrupted'
    ));
