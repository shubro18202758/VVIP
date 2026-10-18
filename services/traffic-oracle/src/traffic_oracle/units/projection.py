"""PURE deterministic emulated-unit movement projection.

The emulated-units contract: a dispatched unit's rendered position is a
TOTAL FUNCTION of (dispatch identity, approved_at, now) — never random,
never stateful.  Same dispatch + same clock reading ⇒ same position on
every observer (ops board, canvas, tests), which is what makes the
emulation honest to replay and audit.

Determinism inputs:

* ``dispatch_seed(dispatch_id)`` — stable 31-bit seed derived from the
  dispatch UUID (blake2b, not Python's randomized ``hash``).  It selects
  the unit's speed inside its band and a small lateral bearing offset so
  multiple units on one route do not stack into one dot.
* ``travel_state(path_costs, elapsed_sec, speed_kmh)`` — pure kinematics
  along the graph path (cumulative edge costs in seconds → progress).

Production seam (documented, not wired here): when a live ``agency_unit``
frame ingest lands (emulator tape → NATS ``corridor.traffic.emulated`` →
oracle ingest), observed frame positions take precedence and this
projection becomes the fallback/interpolation between frames.  Until
then the projection IS the emulated source and every surfaced unit
carries ``emulated: true`` + ``source: 'emulated_dispatch_projection'``.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Sequence

# Emulated kinematics bands (deterministic per dispatch seed).
_SPEED_BAND_KMH = (32.0, 48.0)      # urban response band
_BEARING_OFFSET_MAX_DEG = 12.0      # lateral offset so units don't stack
_SPEED_M_PER_SEC = 1000.0 / 3600.0


def dispatch_seed(dispatch_id: str) -> int:
    """Stable 31-bit seed from a dispatch UUID (blake2b — never Python hash)."""
    digest = hashlib.blake2b(
        str(dispatch_id).encode("utf-8"), digest_size=4
    ).digest()
    return int.from_bytes(digest, "big") & 0x7FFFFFFF


def seeded_speed_kmh(seed: int) -> float:
    """Deterministic speed inside the emulated band, from the seed."""
    frac = (seed % 1000) / 1000.0
    lo, hi = _SPEED_BAND_KMH
    return round(lo + frac * (hi - lo), 2)


def seeded_bearing_offset_deg(seed: int) -> float:
    """Deterministic small lateral offset in [-max, +max] degrees."""
    frac = ((seed >> 8) % 1000) / 1000.0
    return round((frac * 2.0 - 1.0) * _BEARING_OFFSET_MAX_DEG, 3)


@dataclass(frozen=True)
class TravelState:
    """Where a unit is along its path — pure function of elapsed time."""

    segment_index: int          # index into the path points
    progress: float             # 0..1 along the whole path
    position: tuple[float, float]
    remaining_sec: float        # ETA to the waypoint (0 when arrived)
    arrived: bool


def travel_state(
    path_points: Sequence[Sequence[float]],
    path_costs_sec: Sequence[float],
    elapsed_sec: float,
    speed_kmh: float,
) -> TravelState | None:
    """Interpolate a unit along a graph path (pure).

    ``path_points``: [(lon, lat), ...] junction coordinates along the
    route (at least 1).  ``path_costs_sec``: cumulative travel cost at
    each point AFTER the first (same length or length-1; missing costs
    default to great-circle distance at the given speed).
    ``elapsed_sec``: seconds since the unit started moving (>= 0).
    """
    if not path_points:
        return None
    pts = [(float(p[0]), float(p[1])) for p in path_points]
    if len(pts) == 1:
        return TravelState(
            segment_index=0,
            progress=1.0,
            position=pts[0],
            remaining_sec=0.0,
            arrived=True,
        )

    speed_ms = max(0.1, speed_kmh * _SPEED_M_PER_SEC)
    # Cumulative seconds at each point (costs supplied, else derived).
    cum: list[float] = [0.0]
    for i in range(1, len(pts)):
        if i - 1 < len(path_costs_sec):
            leg = max(0.0, float(path_costs_sec[i - 1]))
        else:
            leg = _haversine_m(pts[i - 1], pts[i]) / speed_ms
        cum.append(cum[-1] + leg)
    total = cum[-1]
    if total <= 0.0:
        return TravelState(0, 1.0, pts[-1], 0.0, True)

    traveled = max(0.0, float(elapsed_sec))
    if traveled >= total:
        return TravelState(
            len(pts) - 1, 1.0, pts[-1], 0.0, True
        )

    # Find the leg containing `traveled` (cum is non-decreasing).
    seg = 0
    for i in range(1, len(cum)):
        if traveled <= cum[i]:
            seg = i
            break
    leg_start, leg_end = cum[seg - 1], cum[seg]
    frac = 0.0 if leg_end <= leg_start else (traveled - leg_start) / (leg_end - leg_start)
    frac = min(1.0, max(0.0, frac))
    lon0, lat0 = pts[seg - 1]
    lon1, lat1 = pts[seg]
    pos = (lon0 + (lon1 - lon0) * frac, lat0 + (lat1 - lat0) * frac)
    return TravelState(
        segment_index=seg - 1,
        progress=round(traveled / total, 5),
        position=(round(pos[0], 6), round(pos[1], 6)),
        remaining_sec=round(total - traveled, 1),
        arrived=False,
    )


def offset_position(
    position: tuple[float, float], bearing_offset_deg: float
) -> tuple[float, float]:
    """Nudge a position sideways along its bearing (visual de-stacking).

    The offset is applied perpendicular to the local north axis scaled by
    cos(lat) so it stays visually isotropic at Gujarat latitudes.  Pure.
    """
    lon, lat = position
    rad = math.radians(bearing_offset_deg)
    perp_lon = -math.sin(rad) * 0.00020  # ~22 m at this latitude
    perp_lat = math.cos(rad) * 0.00020
    return (round(lon + perp_lon, 6), round(lat + perp_lat, 6))


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon0, lat0 = map(math.radians, a)
    lon1, lat1 = map(math.radians, b)
    dlat = lat1 - lat0
    dlon = lon1 - lon0
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat0) * math.cos(lat1) * math.sin(dlon / 2) ** 2
    )
    return 2.0 * 6371000.0 * math.asin(min(1.0, math.sqrt(h)))


__all__ = [
    "TravelState",
    "dispatch_seed",
    "offset_position",
    "seeded_bearing_offset_deg",
    "seeded_speed_kmh",
    "travel_state",
]
