"""City signal coordination — package surface (signalling-janrakshak stream).

Submodules:
    gate    — shared/exclusive asyncio gate excluding signal-plan writer
              commits while any twin simulation / shadow-eval is in flight
              (contract C7, in-process: the twin runs inside traffic-oracle).
    plans   — PURE deterministic band→city-plan mapping over
              corridor.signal_programs rows (documented table; no I/O).
    writer  — the gated city-plan writer (default OFF via
              VVIP_SIGNAL_POLICY_WRITER) + snapshot/restore orchestration.
    router  — HTTP surface:
                  GET  /api/v1/signals/state    (C1 canvas contract)
                  GET  /api/v1/signals/plans    (writer posture + last plan)
                  POST /api/v1/signals/plans/apply
                  POST /api/v1/signals/restore
                  GET  /api/v1/signals/history
"""

from traffic_oracle.signals.gate import signal_policy_gate, twin_in_flight
from traffic_oracle.signals.plans import (
    BAND_PLAN_TABLE,
    active_phase,
    city_band_from_status,
    green_ratio,
    plan_city,
)
from traffic_oracle.signals.writer import writer_enabled

__all__ = [
    "BAND_PLAN_TABLE",
    "active_phase",
    "city_band_from_status",
    "green_ratio",
    "plan_city",
    "signal_policy_gate",
    "twin_in_flight",
    "writer_enabled",
]
