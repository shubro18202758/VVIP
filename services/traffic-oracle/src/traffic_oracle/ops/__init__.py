"""Traffic-oracle ops surfacing — read-only boards over the fleet's audit trails.

Currently home to the degradation ledger: a taxonomy of recurring
degradation/failure causes mined from convoy-brain's ``decisions_log``
(the per-movement audit trail persisted to Valkey by the brain's
ConvoyContextStore).  Pure analysis lives in ``degradation_taxonomy``;
the HTTP surface lives in ``router``.
"""
