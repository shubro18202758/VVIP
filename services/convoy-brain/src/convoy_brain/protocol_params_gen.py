"""GENERATED — DO NOT EDIT BY HAND (contract C1).

Regenerate with:  python scripts/export_protocol_params.py
Canonical source: VVIP/shared/schemas/protocol_params.json (generator input
only; never read at runtime).  A pytest in this service regenerates the
module text and asserts it matches this file — hand edits fail the tier.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Baseline constants — identical across every service (fail-closed floor)
# ---------------------------------------------------------------------------

BASELINE_ADVANCE_CLOSURE_SEC: int = 120
BASELINE_CONGESTION_SPIKE_THRESHOLD: float = 0.8
BASELINE_GPS_POLL_INTERVAL_SEC: float = 1.0
BASELINE_SCORING_WEIGHTS: dict[str, float] = {
        "time": 0.3,
        "disruption": 0.4,
        "security": 0.2,
        "complexity": 0.1,
}

#: Robust-routing objective knob (P90 tail costs): ``quantile`` selects the
#: tail used for the time/disruption cost tables (clamped to
#: [ROBUST_QUANTILE_MIN, ROBUST_QUANTILE_MAX] — never a sub-median or a
#: degenerate >0.99 tail); the two weights are the robust objective's
#: time/disruption coefficients; ``enabled=false`` reverts the router to
#: today's point objective exactly (auditable one-key disable).
BASELINE_ROBUST_ROUTING: dict[str, float | bool] = {
        "enabled": True,
        "quantile": 0.9,
        "time_weight": 0.3,
        "disruption_weight": 0.4,
}

# ---------------------------------------------------------------------------
# Hard engineering bounds applied to every published parameter set
# ---------------------------------------------------------------------------

GPS_POLL_MIN_SEC: float = 1.0
GPS_POLL_MAX_SEC: float = 5.0
CONGESTION_THRESHOLD_MIN: float = 0.1
CONGESTION_THRESHOLD_MAX: float = 0.8
ROBUST_QUANTILE_MIN: float = 0.5
ROBUST_QUANTILE_MAX: float = 0.99

# ---------------------------------------------------------------------------
# Per-protection-class protocol tables (protection classes generalize the
# legacy VVIP tier axis: 'none' is a public/civic event with no protectee).
# ---------------------------------------------------------------------------

#: Order from mildest to most severe (rank index).
PROTECTION_CLASS_ORDER: tuple[str, ...] = ('Z+', 'Z', 'Y', 'X', 'none')

#: Protocol floor on advance_closure_sec by protection class (None = no floor).
ADVANCE_CLOSURE_FLOOR_SEC: dict[str, int | None] = {
    "Z+": 180,
    "Z": 120,
    "Y": 60,
    "X": None,
    "none": None,
}

#: Severity points contributed to the situation score by protection class.
SECURITY_POINTS: dict[str, int] = {
    "Z+": 6,
    "Z": 4,
    "Y": 2,
    "X": 0,
    "none": 0,
}

#: Minimum escortable lanes by protection class (route validation floor).
MIN_LANES: dict[str, int] = {
    "Z+": 6,
    "Z": 4,
    "Y": 2,
    "X": 1,
    "none": 0,
}



__all__ = [
    "ADVANCE_CLOSURE_FLOOR_SEC",
    "BASELINE_ADVANCE_CLOSURE_SEC",
    "BASELINE_CONGESTION_SPIKE_THRESHOLD",
    "BASELINE_GPS_POLL_INTERVAL_SEC",
    "BASELINE_ROBUST_ROUTING",
    "BASELINE_SCORING_WEIGHTS",
    "CONGESTION_THRESHOLD_MAX",
    "CONGESTION_THRESHOLD_MIN",
    "GPS_POLL_MAX_SEC",
    "GPS_POLL_MIN_SEC",
    "MIN_LANES",
    "PROTECTION_CLASS_ORDER",
    "ROBUST_QUANTILE_MAX",
    "ROBUST_QUANTILE_MIN",
    "SECURITY_POINTS",
]
