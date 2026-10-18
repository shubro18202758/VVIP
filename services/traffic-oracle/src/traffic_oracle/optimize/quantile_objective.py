"""Robust (P90) routing objective — quantile-aware edge costs.

Pure leaf (Wave-2 hatch pattern, mirrors ``predict/support_bounds.py``):
imports only the stdlib, structlog, and the generated protocol-params
module.  It NEVER touches ``optimize.rl`` (rl/__init__.py pulls torch) or
the forecaster — quantile streams arrive as plain ``SegmentQuantiles``
values built by the ONE shared thin mapping layer
(``corridor_router._segment_quantiles_from_row``, reused by the
predict/eta provider), so a producer shape change adapts that layer
alone, not this leaf.

Forecast-quantile contract (producer and consumer code to this, exactly):
per-horizon forecast rows MAY carry an optional ``quantiles`` object with
the shape ``quantiles: {speed_kmh: {p10, p50, p90}, congestion_idx:
{p10, p50, p90}}`` alongside the point fields — knots are the shared
``predict/forecast_quantiles`` convention, NON-DECREASING per field
(p10 <= p50 <= p90; for speed p10 is the SLOW tail, for congestion p90
is the congested tail).  Producers treat the object as optional (absent
= point-only).  Consumers — this leaf — MUST degrade to point forecasts
with an explicit labelled reason when quantiles are absent, and MUST
validate monotonicity and fall back to point values when violated.

The robust-quantile knob ``q`` (>= 0.5) selects the ADVERSE tail of each
banded field: congestion at level q (ascending adverse) and speed at
level 1-q (the slow side of an ascending band).  q = 0.90 therefore
prices the producer's slow scenario — speed p10 with congestion p90 —
and can never select a better-than-median speed or a lower-than-median
congestion.

Cost math is byte-identical to today's point path in
``optimize/mip_solver.py``:

* travel time   ``travel_sec = length_m / max(speed * 1000 / 3600, 0.1)``
  (mip_solver.py:149 — the 0.1 m/s floor guards zero/dead speeds)
* disruption    ``congestion * volume_proxy[road_class] * travel_sec``
  (mip_solver.py:153-156)
* CP-SAT needs integer costs, so scaled tables multiply by ``SCALE = 1000``
  (mip_solver.py:136).  The constants are locals inside ``solve`` there;
  they are re-declared here with the values pinned by tests that
  hand-compute the exact numbers.

No-op semantics mirror ``FlowForecaster.last_degradation_reason``
(flow_forecaster.py:104-108): reasons ride log events and result objects,
never the forecast payload (the ForecastProvider contract is exactly
point-shaped).  Absent/empty stream → ``source="point"``,
``reason="no_quantile_stream"`` and outputs identical to today's point
path; partial coverage → ``reason="partial_coverage:<n>/<m>"`` (n of m
routing-area segments carry usable quantiles) with uncovered segments on
point values; monotonicity/shape violations degrade the offending segment
(or the whole stream under ``strict=True``) to point with
``"non_monotonic:<sid>"`` / ``"invalid_quantiles:<sid>"``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

import structlog

from traffic_oracle.adaptive.protocol_params_gen import (
    BASELINE_ROBUST_ROUTING,
    ROBUST_QUANTILE_MAX,
    ROBUST_QUANTILE_MIN,
)

logger = structlog.get_logger(__name__)

#: Knot names in increasing quantile order.
Q_KNOTS: tuple[str, ...] = ("p10", "p50", "p90")

#: Probability level of each knot (piecewise-linear interpolation domain).
KNOT_LEVELS: dict[str, float] = {"p10": 0.10, "p50": 0.50, "p90": 0.90}

#: Integer cost scaling for CP-SAT — MUST equal ``SCALE`` inside
#: ``ConvoyMIPSolver.solve`` (mip_solver.py:136).  Pinned by tests.
SCALE: int = 1000

#: Traffic-volume proxy by road class — MUST equal the ``volume_proxy``
#: table inside ``ConvoyMIPSolver.solve`` (mip_solver.py:153-154).
VOLUME_PROXY: dict[str, float] = {
    "motorway": 5.0, "trunk": 4.0, "primary": 3.0,
    "secondary": 2.0, "tertiary": 1.5, "residential": 1.0,
}

#: Point-path defaults — MUST equal the ``.get`` fallbacks inside
#: ``ConvoyMIPSolver.solve`` (mip_solver.py:142-146).
DEFAULT_LENGTH_M: float = 500.0
DEFAULT_ROAD_CLASS: str = "secondary"
DEFAULT_SPEED_KMH: float = 40.0
DEFAULT_CONGESTION: float = 0.3


# ---------------------------------------------------------------------------
# Knob resolution + engineering-bounds clamp (bounds.py:37-42 pattern)
# ---------------------------------------------------------------------------


def bound_robust_quantile(value: float) -> float:
    """Clamp a robust-routing quantile to the engineering range.

    The floor (0.5) means the objective can never run a non-robust
    sub-median tail; the ceiling (0.99) bars a degenerate far tail the
    three-knot stream cannot honestly support.  Non-finite values raise
    (a NaN knob is a configuration error, never a silent clamp).
    """
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"robust quantile must be finite, got {value!r}")
    v = max(v, ROBUST_QUANTILE_MIN)
    v = min(v, ROBUST_QUANTILE_MAX)
    return round(v, 2)


def resolve_robust_knob(knob: Mapping[str, float | bool] | None) -> dict:
    """Merge a caller knob over the canonical baseline, clamped.

    Returns ``{"enabled": bool, "quantile": float, "time_weight": float,
    "disruption_weight": float}``.  Unknown keys are dropped (fail-closed
    to the registered shape); ``quantile`` goes through
    ``bound_robust_quantile``; the two objective weights are guarded to
    [0, 1] at code level (no JSON engineering bounds are registered for
    them — nothing on the wire passes them today; registering them in
    protocol_params.json is the follow-up if that changes).
    """
    merged: dict = dict(BASELINE_ROBUST_ROUTING)
    if knob is not None:
        for key in ("enabled", "quantile", "time_weight", "disruption_weight"):
            if key in knob:
                merged[key] = knob[key]
    merged["enabled"] = bool(merged.get("enabled", True))
    merged["quantile"] = bound_robust_quantile(float(merged.get("quantile", 0.9)))
    for weight_key in ("time_weight", "disruption_weight"):
        w = float(merged.get(weight_key, 0.0))
        merged[weight_key] = min(max(w, 0.0), 1.0)
    return merged


# ---------------------------------------------------------------------------
# Input / result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentQuantiles:
    """Quantile knots for one segment at one horizon (producer-agnostic).

    ``speed_kmh_by_q`` / ``congestion_idx_by_q`` map knot name → value;
    knots are the exact contract keys ``p10``/``p50``/``p90``, expected
    NON-DECREASING per field (the ``predict.forecast_quantiles``
    producer convention — speed p10 is the slow tail).  Values are
    validated by this module (never trusted blind): non-numeric,
    boolean, or non-finite knots label the row ``invalid_quantiles``.
    """

    segment_id: int
    horizon_min: int
    speed_kmh_by_q: Mapping[str, float]
    congestion_idx_by_q: Mapping[str, float]


@dataclass(frozen=True)
class RobustSegmentCost:
    """Per-segment costs at the p50 (point) and robust-quantile rungs."""

    segment_id: int
    time_p50_sec: float
    time_q_sec: float
    disruption_p50: float
    disruption_q: float
    source: str            # "quantile" | "point"
    reason: str | None     # per-segment degradation label, None when clean


@dataclass(frozen=True)
class RobustCostResult:
    """Stream-level outcome of ``robust_edge_costs``.

    ``time_p90_sec`` / ``disruption_p90`` are the totals at the knobbed
    adverse tail (default 0.90 = the producer's slow scenario: speed p10
    with congestion p90; the field keeps the p90 name for wire
    stability); the ``*_p50`` totals are today's point-path meaning.
    """

    per_segment: dict[int, RobustSegmentCost] = field(default_factory=dict)
    time_p50_sec: float = 0.0
    time_p90_sec: float = 0.0
    disruption_p50: float = 0.0
    disruption_p90: float = 0.0
    source: str = "point"                # "quantile" | "point"
    reason: str | None = None
    quantile_level: float = 0.9          # effective (clamped) level
    #: CP-SAT integer cost tables (×SCALE), keyed by segment_id, holding
    #: the q value for covered segments and the point value otherwise.
    time_q_scaled: dict[int, int] = field(default_factory=dict)
    disruption_q_scaled: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ETABand:
    """Honest ETA band from a quantile stream (all knots, full coverage)."""

    p10_sec: float
    p50_sec: float
    p90_sec: float
    source: str = "quantile"


@dataclass(frozen=True)
class ETABandResult:
    """``eta_band_seconds`` outcome: a band, or None + labelled reason."""

    band: ETABand | None
    reason: str | None   # None iff the band is present


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _finite_number(value: object) -> float | None:
    """Numeric + finite check (bools excluded — they are not forecasts)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _knots(mapping: Mapping[str, float], name: str) -> tuple[float, float, float] | None:
    """Extract the (p10, p50, p90) knot triple, or None if malformed."""
    triple = []
    for knot in Q_KNOTS:
        v = _finite_number(mapping.get(knot))
        if v is None:
            logger.debug("quantile_objective.malformed_knot", field=name, knot=knot)
            return None
        triple.append(v)
    return (triple[0], triple[1], triple[2])


_KnotTriple = tuple[float, float, float]


def _validate_segment(sq: SegmentQuantiles) -> tuple[_KnotTriple, _KnotTriple] | None:
    """Return (speed_knots, congestion_knots) when the row is usable.

    Usable = well-formed AND monotone in the PRODUCER's knot convention
    (predict/forecast_quantiles.make_quantiles + validated_bands, pinned
    by tests/test_foundation_wiring.py): every banded field is
    non-decreasing in q (p10 <= p50 <= p90).  For speed that means p10 is
    the SLOW tail and p90 the fast tail; for congestion p90 is the
    congested tail.  The producer derives congestion p10 from the fast
    speed bound, so one physical scenario (slow) is (speed p10,
    congestion p90) — exactly the pair the robust tail selects below.
    """
    speeds = _knots(sq.speed_kmh_by_q, "speed_kmh")
    cong = _knots(sq.congestion_idx_by_q, "congestion_idx")
    if speeds is None or cong is None:
        return None
    if not (speeds[0] <= speeds[1] <= speeds[2]):
        return None
    if not (cong[0] <= cong[1] <= cong[2]):
        return None
    return speeds, cong


def _value_at_level(knots: _KnotTriple, level: float) -> float:
    """Piecewise-linear knot interpolation over ASCENDING knots.

    ``level`` is clamped into [0.10, 0.90]: beyond the outer knots the
    three-knot stream carries no information, so the honest answer is the
    outermost knot (never an extrapolated tail).  Exact knot levels
    return the knot value bit-for-bit (no float round-trip).
    """
    lo = min(max(level, KNOT_LEVELS["p10"]), KNOT_LEVELS["p90"])
    if lo <= KNOT_LEVELS["p10"]:
        return knots[0]
    if lo >= KNOT_LEVELS["p90"]:
        return knots[2]
    if lo == KNOT_LEVELS["p50"]:
        return knots[1]
    if lo <= KNOT_LEVELS["p50"]:
        frac = (lo - KNOT_LEVELS["p10"]) / (KNOT_LEVELS["p50"] - KNOT_LEVELS["p10"])
        return knots[0] + (knots[1] - knots[0]) * frac
    frac = (lo - KNOT_LEVELS["p50"]) / (KNOT_LEVELS["p90"] - KNOT_LEVELS["p50"])
    return knots[1] + (knots[2] - knots[1]) * frac


def _speed_at_robust_level(speeds: _KnotTriple, level: float) -> float:
    """Speed at the ADVERSE tail for robust level ``level`` (>= 0.5).

    Speed is ASCENDING in q with p10 the slow tail, so the adverse (slow)
    side is the LOW quantile: robust level q reads speed at 1 - q.  With
    the knob floor q >= 0.5 the selected speed is never faster than the
    median; q = 0.90 reads the p10 knot exactly (the producer's slow
    scenario); q above 0.9 clamps to p10 — no extrapolation.
    """
    return _value_at_level(speeds, 1.0 - level)


def _travel_sec(length_m: float, speed_kmh: float) -> float:
    """Exact mip_solver point-path travel time (mip_solver.py:149)."""
    return length_m / max(speed_kmh * 1000 / 3600, 0.1)


def _disruption(congestion: float, road_class: str, travel_sec: float) -> float:
    """Exact mip_solver point-path disruption (mip_solver.py:153-156)."""
    return congestion * VOLUME_PROXY.get(road_class, 2.0) * travel_sec


def _is_non_monotone(sq: SegmentQuantiles) -> bool:
    """True when the row is well-formed but violates knot ordering."""
    speeds = _knots(sq.speed_kmh_by_q, "speed_kmh")
    cong = _knots(sq.congestion_idx_by_q, "congestion_idx")
    if speeds is None or cong is None:
        return False  # malformed, not non-monotone
    return not (speeds[0] <= speeds[1] <= speeds[2]) or not (
        cong[0] <= cong[1] <= cong[2]
    )


def _degradation_reason(sid: int, stream: Mapping[int, SegmentQuantiles]) -> str | None:
    """Per-segment label for a row present in the stream but unusable."""
    if sid not in stream:
        return None
    return (
        "non_monotonic:%d" % sid
        if _is_non_monotone(stream[sid])
        else "invalid_quantiles:%d" % sid
    )


def _segment_point_costs(
    attrs: Mapping[str, object],
    point_speed: float | None,
    point_congestion: float | None,
) -> tuple[float, float]:
    """Today's point-path costs for one segment (defaults included)."""
    length_m = float(attrs.get("length_m", DEFAULT_LENGTH_M))
    road_class = str(attrs.get("road_class", DEFAULT_ROAD_CLASS))
    speed = point_speed if point_speed is not None else DEFAULT_SPEED_KMH
    congestion = (
        point_congestion if point_congestion is not None else DEFAULT_CONGESTION
    )
    t = _travel_sec(length_m, speed)
    return t, _disruption(congestion, road_class, t)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def robust_edge_costs(
    segment_attrs: Mapping[int, Mapping[str, object]],
    point_speeds: Mapping[int, float],
    point_congestion: Mapping[int, float],
    quantiles: Mapping[int, SegmentQuantiles] | None,
    quantile_level: float = 0.9,
    strict: bool = False,
) -> RobustCostResult:
    """Robust (quantile-weighted) per-segment time/disruption cost tables.

    Args:
        segment_attrs: segment_id → {length_m, road_class, ...} (the
            routing-area attrs the MIP solver loads).
        point_speeds / point_congestion: today's point forecast tables
            (segment_id → value); the exact dicts ``corridor_router``
            already builds.  Missing entries use the solver defaults.
        quantiles: optional segment_id → SegmentQuantiles stream; None or
            empty degrades the whole call to the point path.
        quantile_level: robust tail (clamped via ``bound_robust_quantile``
            before use).
        strict: when True, any invalid/non-monotone row degrades the WHOLE
            stream to point (labelled with every offending segment).

    Returns:
        RobustCostResult with per-segment p50/point and q costs, the
        CP-SAT-ready ``time_q_scaled``/``disruption_q_scaled`` tables
        (q value for covered segments, point value otherwise), and the
        labelled no-op semantics described in the module docstring.
    """
    level = bound_robust_quantile(quantile_level)
    stream = quantiles or {}

    per_segment: dict[int, RobustSegmentCost] = {}
    time_q_scaled: dict[int, int] = {}
    disruption_q_scaled: dict[int, int] = {}
    covered = 0
    invalid_sids: list[int] = []

    for sid in sorted(segment_attrs):
        attrs = segment_attrs[sid]
        t_point, d_point = _segment_point_costs(
            attrs, point_speeds.get(sid), point_congestion.get(sid)
        )

        usable = None
        if sid in stream:
            validated = _validate_segment(stream[sid])
            if validated is None:
                invalid_sids.append(sid)
            else:
                usable = validated

        if usable is None:
            per_segment[sid] = RobustSegmentCost(
                segment_id=sid,
                time_p50_sec=t_point,
                time_q_sec=t_point,
                disruption_p50=d_point,
                disruption_q=d_point,
                source="point",
                reason=_degradation_reason(sid, stream),
            )
            time_q_scaled[sid] = int(t_point * SCALE)
            disruption_q_scaled[sid] = int(d_point * SCALE)
            continue

        speeds, cong = usable
        covered += 1
        # Adverse-tail selection: congestion is ascending with the bad
        # tail at high q, speed is ascending with the bad (slow) tail at
        # LOW q — so the robust level reads congestion at q and speed at
        # 1-q (see _speed_at_robust_level).  q = 0.90 therefore selects
        # the producer's slow scenario: (speed p10, congestion p90).
        speed_q = _speed_at_robust_level(speeds, level)
        cong_q = _value_at_level(cong, level)
        road_class = str(attrs.get("road_class", DEFAULT_ROAD_CLASS))
        length_m = float(attrs.get("length_m", DEFAULT_LENGTH_M))
        t_q = _travel_sec(length_m, speed_q)
        d_q = _disruption(cong_q, road_class, t_q)

        per_segment[sid] = RobustSegmentCost(
            segment_id=sid,
            time_p50_sec=_travel_sec(length_m, speeds[1]),
            time_q_sec=t_q,
            disruption_p50=_disruption(cong[1], road_class,
                                       _travel_sec(length_m, speeds[1])),
            disruption_q=d_q,
            source="quantile",
            reason=None,
        )
        time_q_scaled[sid] = int(t_q * SCALE)
        disruption_q_scaled[sid] = int(d_q * SCALE)

    total = len(segment_attrs)

    # Strict mode: any violation poisons the whole stream.
    if strict and invalid_sids:
        logger.warning(
            "quantile_objective.strict_degradation",
            segments=sorted(invalid_sids),
            quantile_level=level,
        )
        for sid in per_segment:
            t_point, d_point = _segment_point_costs(
                segment_attrs[sid],
                point_speeds.get(sid),
                point_congestion.get(sid),
            )
            per_segment[sid] = RobustSegmentCost(
                segment_id=sid,
                time_p50_sec=t_point,
                time_q_sec=t_point,
                disruption_p50=d_point,
                disruption_q=d_point,
                source="point",
                reason=_degradation_reason(sid, stream),
            )
            time_q_scaled[sid] = int(t_point * SCALE)
            disruption_q_scaled[sid] = int(d_point * SCALE)
        covered = 0

    reason: str | None = None
    if not stream:
        reason = "no_quantile_stream"
    elif invalid_sids:
        reason = ",".join(
            _degradation_reason(s, stream) or "invalid_quantiles:%d" % s
            for s in sorted(invalid_sids)
        )
    elif covered < total:
        reason = "partial_coverage:%d/%d" % (covered, total)

    result = RobustCostResult(
        per_segment=per_segment,
        time_p50_sec=sum(c.time_p50_sec for c in per_segment.values()),
        time_p90_sec=sum(c.time_q_sec for c in per_segment.values()),
        disruption_p50=sum(c.disruption_p50 for c in per_segment.values()),
        disruption_p90=sum(c.disruption_q for c in per_segment.values()),
        source="quantile" if covered > 0 else "point",
        reason=reason,
        quantile_level=level,
        time_q_scaled=time_q_scaled,
        disruption_q_scaled=disruption_q_scaled,
    )

    if result.source == "quantile":
        logger.info(
            "quantile_objective.robust_costs",
            segments=total,
            covered=covered,
            quantile_level=level,
            reason=result.reason,
        )
    else:
        logger.info(
            "quantile_objective.point_fallback",
            segments=total,
            reason=result.reason or "no_quantile_stream",
        )
    return result


def eta_band_seconds(
    lengths: Mapping[int, float],
    quantiles: Mapping[int, SegmentQuantiles],
    requested_ids: list[int] | tuple[int, ...],
) -> ETABandResult:
    """Honest p10/p50/p90 ETA band over the requested segments.

    A band is returned ONLY on full coverage: every requested segment must
    have a length and a usable (well-formed, monotone) quantile row.  A
    band over half a route would mislabel the estimate, so any gap —
    missing length, missing row, invalid/non-monotone knots — degrades to
    ``band=None`` with the labelled reason.

    Band edges are quantiles of TRAVEL TIME: with the producer's
    ascending speed knots (p10 slow, p90 fast), the slow (long-time) tail
    is speed p10 and the fast (short-time) tail is speed p90 — so
    ``p90_sec`` sums travel at speed p10, ``p10_sec`` sums travel at
    speed p90, and ``p10_sec <= p50_sec <= p90_sec`` always holds.
    Per-knot seconds use the exact point-path travel-time math
    (``length / max(speed*1000/3600, 0.1)``).
    """
    if not requested_ids:
        return ETABandResult(band=None, reason="no_quantile_stream")
    if not quantiles:
        return ETABandResult(band=None, reason="no_quantile_stream")

    missing_length: list[int] = []
    missing_row: list[int] = []
    invalid: list[int] = []
    totals = [0.0, 0.0, 0.0]

    for sid in requested_ids:
        length = _finite_number(lengths.get(sid))
        if length is None:
            missing_length.append(sid)
            continue
        row = quantiles.get(sid)
        if row is None:
            missing_row.append(sid)
            continue
        validated = _validate_segment(row)
        if validated is None:
            invalid.append(sid)
            continue
        speeds, _cong = validated
        # Time-quantile ordering (slow tail first): p90_sec <- speed p10,
        # p50_sec <- speed p50, p10_sec <- speed p90.
        totals[0] += _travel_sec(length, speeds[2])
        totals[1] += _travel_sec(length, speeds[1])
        totals[2] += _travel_sec(length, speeds[0])

    if missing_length or missing_row or invalid:
        if invalid:
            reason = ",".join(
                _degradation_reason(s, quantiles) or "invalid_quantiles:%d" % s
                for s in sorted(invalid)
            )
        elif missing_row:
            covered = len(requested_ids) - len(missing_row) - len(missing_length)
            reason = "partial_coverage:%d/%d" % (covered, len(requested_ids))
        else:
            reason = "partial_coverage:%d/%d" % (
                len(requested_ids) - len(missing_length), len(requested_ids)
            )
        logger.info(
            "quantile_objective.eta_band_unavailable",
            reason=reason,
            requested=len(requested_ids),
        )
        return ETABandResult(band=None, reason=reason)

    return ETABandResult(
        band=ETABand(
            p10_sec=totals[0], p50_sec=totals[1], p90_sec=totals[2]
        ),
        reason=None,
    )


__all__ = [
    "DEFAULT_CONGESTION",
    "DEFAULT_LENGTH_M",
    "DEFAULT_ROAD_CLASS",
    "DEFAULT_SPEED_KMH",
    "ETABand",
    "ETABandResult",
    "KNOT_LEVELS",
    "Q_KNOTS",
    "SCALE",
    "RobustCostResult",
    "RobustSegmentCost",
    "SegmentQuantiles",
    "VOLUME_PROXY",
    "bound_robust_quantile",
    "eta_band_seconds",
    "resolve_robust_knob",
    "robust_edge_costs",
]
