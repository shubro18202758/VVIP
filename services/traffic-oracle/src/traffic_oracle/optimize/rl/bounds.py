"""Protocol bounds for online (DQN) parameter adaptation.

The plan pins DQN as *online-with-bounds*: whatever the agent proposes,
the values handed to the real application points must respect the same
protocol floors the rules-based ParameterAdapter enforces.  This module
is the single delegation point — RL code calls these helpers instead of
re-deriving bounds, so the two paths can never drift.
"""

from __future__ import annotations

from traffic_oracle.adaptive.enums import SecurityTier
from traffic_oracle.adaptive.policy import (
    ADVANCE_CLOSURE_FLOOR_SEC,
    BASELINE_ADVANCE_CLOSURE_SEC,
    BASELINE_CONGESTION_SPIKE_THRESHOLD,
    BASELINE_GPS_POLL_INTERVAL_SEC,
    CONGESTION_THRESHOLD_MAX,
    CONGESTION_THRESHOLD_MIN,
    GPS_POLL_MAX_SEC,
    GPS_POLL_MIN_SEC,
    clamp_advance_closure,
)

ADVANCE_CLOSURE_MIN_SEC = 30     # search-space lower bound (protocol may raise)
ADVANCE_CLOSURE_MAX_SEC = 600    # search-space upper bound


def bound_advance_closure_sec(value: float, vvip_class: str | SecurityTier) -> int:
    """Clamp an agent-proposed advance closure to [floor, ADVANCE_CLOSURE_MAX_SEC]."""
    clamped = int(round(float(value)))
    clamped = max(clamped, ADVANCE_CLOSURE_MIN_SEC)
    clamped = min(clamped, ADVANCE_CLOSURE_MAX_SEC)
    return clamp_advance_closure(clamped, vvip_class)


def bound_congestion_spike_threshold(value: float) -> float:
    """Clamp an agent-proposed spike threshold to the engineering range."""
    v = float(value)
    v = max(v, CONGESTION_THRESHOLD_MIN)
    v = min(v, CONGESTION_THRESHOLD_MAX)
    return round(v, 2)


def bound_gps_poll_interval_sec(value: float) -> float:
    """Clamp an agent-proposed GPS poll interval to the engineering range."""
    v = float(value)
    v = max(v, GPS_POLL_MIN_SEC)
    v = min(v, GPS_POLL_MAX_SEC)
    return round(v, 2)


def baseline_snapshot(vvip_class: str | SecurityTier) -> dict:
    """Baseline (fail-closed) parameter snapshot for an RL sanity check."""
    tier = (
        vvip_class if isinstance(vvip_class, SecurityTier)
        else SecurityTier.from_code(vvip_class)
    )
    floor = ADVANCE_CLOSURE_FLOOR_SEC[tier]
    return {
        "advance_closure_sec": clamp_advance_closure(
            BASELINE_ADVANCE_CLOSURE_SEC, tier
        ),
        "congestion_spike_threshold": BASELINE_CONGESTION_SPIKE_THRESHOLD,
        "gps_poll_interval_sec": BASELINE_GPS_POLL_INTERVAL_SEC,
        "floor_applied": floor is not None and floor > BASELINE_ADVANCE_CLOSURE_SEC,
    }


__all__ = [
    "ADVANCE_CLOSURE_FLOOR_SEC",
    "ADVANCE_CLOSURE_MAX_SEC",
    "ADVANCE_CLOSURE_MIN_SEC",
    "baseline_snapshot",
    "bound_advance_closure_sec",
    "bound_congestion_spike_threshold",
    "bound_gps_poll_interval_sec",
    "clamp_advance_closure",
]
