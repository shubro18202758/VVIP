-- 017_enforcement_stolen_sighting.sql
-- Stolen-vehicle e-challan suppression: the R_STOLEN_SIGHTING evidence rule
-- (WP-19, wave 3, contract C-N).
--
-- SCHEMA ONLY, additive, idempotent: safe on fresh installs (before the
-- seed) and on the ledgered live install.  Touches only 010's
-- events.enforcement_recommendations.
--
--   events.enforcement_recommendations.rule_id gains 'R_STOLEN_SIGHTING'.
--   A vehicle whose ACTIVE earmark carries the lawful basis
--   stolen_vehicle_fir is the VICTIM's car: a camera read of it is
--   evidence for the investigating officer, never an e-challan to its
--   registered owner.  Under such an earmark the oracle's deterministic
--   rules (traffic_oracle/enforcement/rules.py) persist ONE
--   R_STOLEN_SIGHTING per read - the read plus the penal rules it would
--   have fired, recorded as withheld - instead of R_SPD_SEGMENT /
--   R_SIGNAL_VIOLATION / R_CLOSED_CORRIDOR_ENTRY, and the decision
--   endpoint refuses to approve any penal recommendation of that vehicle
--   (409 'e-challan withheld - vehicle reported stolen').  A human officer
--   still decides every R_STOLEN_SIGHTING row (approve = the authorised
--   basis of a pursuit, reject = dismissed) - nothing is auto-approved.
--   Existing rows keep their rule ids: the three penal ids 010 allowed are
--   all still allowed, so re-adding the CHECK validates them.
--
-- Ethics rails (C6): no column is added - vehicles only, plates as read,
-- no person-directed field.
--
-- Grammar constraint (007 header): consumed by naive ';'-splitters - no
-- ';' inside string literals, no dollar-quoted bodies, no block comments.
-- Re-applying is a no-op: the guarded DROP removes the constraint the
-- previous apply added and the ADD recreates it identically.

ALTER TABLE events.enforcement_recommendations
    DROP CONSTRAINT IF EXISTS enforcement_recommendations_rule_id_check;

ALTER TABLE events.enforcement_recommendations
    ADD CONSTRAINT enforcement_recommendations_rule_id_check CHECK (rule_id IN (
        'R_SPD_SEGMENT',
        'R_SIGNAL_VIOLATION',
        'R_CLOSED_CORRIDOR_ENTRY',
        'R_STOLEN_SIGHTING'
    ));
