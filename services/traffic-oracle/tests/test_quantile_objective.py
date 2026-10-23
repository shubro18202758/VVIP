"""Tests for the P90-robust routing objective (optimize/quantile_objective)
and its thin integrations (corridor_router, mip_solver, predict/eta).

Coverage (pure gate-venv — no torch/onnx/onnxruntime anywhere):
    - Leaf: monotone streams (the PRODUCER knot convention: p10 <= p50 <=
      p90 per field, speed p10 = slow tail) shift the objective with
      hand-computed pinned values using the EXACT mip_solver point math;
      the robust level q selects the ADVERSE tail (congestion at q, speed
      at 1-q — q=0.90 prices speed p10 + congestion p90, the producer's
      slow scenario); knot levels are returned bit-for-bit (no float
      round-trip); non-monotone/malformed rows degrade per-segment (or
      whole-stream under strict) with labelled reasons; absent/empty/
      partial streams no-op to today's point path bit-for-bit; the knob
      is clamped to the registered bounds and resolves from the generated
      protocol module.
    - Producer-convention cross-checks: rows built by the REAL producer
      (predict.forecast_quantiles.make_quantiles) are accepted by both
      the canonical consumer (validated_bands) and this leaf, and drive
      the router end-to-end; leaf validity == validated_bands validity in
      both directions (no convention clash can recur silently).
    - Mapping layer: knot values pass through UNCOERCED — bools, strings
      and shape-broken objects reach the leaf and get the
      invalid_quantiles:<sid> label (never silently coerced or degraded
      to "absent").
    - ETA band: full coverage yields honest p10/p50/p90 seconds (time
      quantiles: p90_sec from the slow speed tail); any gap degrades with
      a reason.
    - MIP overlay: robust tables REPLACE the objective's time/disruption
      terms (route flips when the tail slows one branch) and return q
      totals; tables built from point values with baseline weights give a
      byte-identical objective; absent tables leave the solve unchanged.
    - Router: quantile-bearing forecast rows drive the robust objective +
      candidate p90 overlays; absent streams keep today's candidates and
      reasons labelled; the knob disables cleanly.
    - predict/eta: the production provider factory (wired in the service
      lifespan) maps forecaster rows through the shared mapping layer;
      additive-only band fields with honest degradation; the fallback
      branch never gets a band; /optimize/routes gains the overlay keys
      only when present.
"""

from __future__ import annotations

import pytest

from traffic_oracle.adaptive.protocol_params_gen import (
    BASELINE_ROBUST_ROUTING,
    ROBUST_QUANTILE_MAX,
    ROBUST_QUANTILE_MIN,
)
from traffic_oracle.optimize.corridor_router import (
    CorridorRouter,
    _segment_quantiles_from_row,
)
from traffic_oracle.optimize.mip_solver import ConvoyMIPSolver
from traffic_oracle.optimize.quantile_objective import (
    SCALE,
    VOLUME_PROXY,
    SegmentQuantiles,
    bound_robust_quantile,
    eta_band_seconds,
    resolve_robust_knob,
    robust_edge_costs,
)
from traffic_oracle.predict.forecast_quantiles import make_quantiles, validated_bands


def travel_sec(length_m: float, speed_kmh: float) -> float:
    """The exact point-path formula (mip_solver.py:149), for hand pins."""
    return length_m / max(speed_kmh * 1000 / 3600, 0.1)


def disruption(congestion: float, road_class: str, t: float) -> float:
    return congestion * VOLUME_PROXY[road_class] * t


def make_row(p10s, p50s, p90s, p10c, p50c, p90c):
    """Hand-built contract row: knots NON-DECREASING per field (speed p10
    = slow tail — the producer convention)."""
    return SegmentQuantiles(
        segment_id=0, horizon_min=5,
        speed_kmh_by_q={"p10": p10s, "p50": p50s, "p90": p90s},
        congestion_idx_by_q={"p10": p10c, "p50": p50c, "p90": p90c},
    )


# ─── Leaf: monotone streams shift the objective ──────────────────────────────


class TestRobustEdgeCosts:
    ATTRS = {
        1: {"length_m": 1000.0, "road_class": "motorway"},
        2: {"length_m": 500.0, "road_class": "secondary"},
    }

    def test_monotone_stream_pins_exact_math_and_p90_shift(self):
        # Speed knots ascending (p10=40 slow tail); robust q=0.9 selects
        # speed p10 + congestion p90 — the adverse scenario.
        stream = {
            1: make_row(40.0, 50.0, 60.0, 0.1, 0.2, 0.4),
            2: make_row(20.0, 40.0, 44.0, 0.1, 0.1, 0.2),
        }
        result = robust_edge_costs(
            self.ATTRS, {1: 50.0, 2: 40.0}, {1: 0.2, 2: 0.1}, stream, 0.9
        )
        assert result.source == "quantile"
        assert result.reason is None

        # Hand-computed pins (exact formula, speed guard not hit):
        t1_p50, t1_p90 = travel_sec(1000.0, 50.0), travel_sec(1000.0, 40.0)
        t2_p50, t2_p90 = travel_sec(500.0, 40.0), travel_sec(500.0, 20.0)
        assert result.time_p50_sec == pytest.approx(t1_p50 + t2_p50)
        assert result.time_p90_sec == pytest.approx(t1_p90 + t2_p90)
        assert result.time_p90_sec > result.time_p50_sec  # the tail shifts time
        assert result.disruption_p90 > result.disruption_p50
        assert result.disruption_p50 == pytest.approx(
            disruption(0.2, "motorway", t1_p50) + disruption(0.1, "secondary", t2_p50)
        )
        assert result.disruption_p90 == pytest.approx(
            disruption(0.4, "motorway", t1_p90) + disruption(0.2, "secondary", t2_p90)
        )
        # CP-SAT integer tables: ×SCALE truncation, exactly as mip_solver.
        assert result.time_q_scaled == {
            1: int(t1_p90 * SCALE), 2: int(t2_p90 * SCALE)
        }
        assert result.disruption_q_scaled == {
            1: int(disruption(0.4, "motorway", t1_p90) * SCALE),
            2: int(disruption(0.2, "secondary", t2_p90) * SCALE),
        }
        assert result.quantile_level == 0.9

    def test_speed_guard_hits_at_dead_tail_speeds(self):
        # Speed p10 = 0.0 (slow tail) -> guard floor 0.1 m/s at q=0.9.
        stream = {1: make_row(0.0, 30.0, 50.0, 0.1, 0.2, 0.9)}
        result = robust_edge_costs({1: self.ATTRS[1]}, {1: 30.0}, {1: 0.2}, stream, 0.9)
        assert result.time_p90_sec == pytest.approx(1000.0 / 0.1)
        assert result.per_segment[1].time_p50_sec == pytest.approx(travel_sec(1000.0, 30.0))

    def test_knot_levels_return_bit_for_bit(self):
        # q=0.9 selects the p10 knot exactly (no float round-trip), q=0.5
        # the p50 knot; the selected speeds ARE the knot objects' values.
        attrs = {1: {"length_m": 1000.0, "road_class": "motorway"}}
        stream = {1: make_row(40.0, 50.0, 60.0, 0.1, 0.2, 0.4)}
        at_p90 = robust_edge_costs(attrs, {1: 50.0}, {1: 0.2}, stream, 0.9)
        t_p10 = travel_sec(1000.0, 40.0)
        assert at_p90.per_segment[1].time_q_sec == t_p10  # exact equality
        at_median = robust_edge_costs(attrs, {1: 50.0}, {1: 0.2}, stream, 0.5)
        assert at_median.per_segment[1].time_q_sec == travel_sec(1000.0, 50.0)
        assert at_median.per_segment[1].time_q_sec == (
            at_median.per_segment[1].time_p50_sec
        )

    def test_absent_and_empty_streams_are_exact_noops(self):
        point_speeds = {1: 50.0, 2: 40.0}
        point_cong = {1: 0.2, 2: 0.1}
        baseline = robust_edge_costs(self.ATTRS, point_speeds, point_cong, None)
        empty = robust_edge_costs(self.ATTRS, point_speeds, point_cong, {})
        for result in (baseline, empty):
            assert result.source == "point"
            assert result.reason == "no_quantile_stream"
            assert result.time_p50_sec == result.time_p90_sec
            assert result.disruption_p50 == result.disruption_p90
            assert result.time_p50_sec == pytest.approx(
                travel_sec(1000.0, 50.0) + travel_sec(500.0, 40.0)
            )
        assert baseline == empty  # bit-for-bit no-op

    def test_partial_coverage_keeps_uncovered_segments_on_point(self):
        stream = {1: make_row(25.0, 50.0, 60.0, 0.1, 0.2, 0.8)}
        result = robust_edge_costs(
            self.ATTRS, {1: 50.0, 2: 40.0}, {1: 0.2, 2: 0.1}, stream, 0.9
        )
        assert result.source == "quantile"
        assert result.reason == "partial_coverage:1/2"
        covered, uncovered = result.per_segment[1], result.per_segment[2]
        assert covered.source == "quantile"
        assert covered.time_q_sec == pytest.approx(travel_sec(1000.0, 25.0))
        assert uncovered.source == "point"
        assert uncovered.reason is None
        assert uncovered.time_q_sec == uncovered.time_p50_sec == pytest.approx(
            travel_sec(500.0, 40.0)
        )

    def test_descending_speed_knots_degrade_with_label(self):
        # Descending speed (p10 fastest) violates the producer convention
        # (p10 <= p50 <= p90) — the exact shape the canonical consumer
        # rejects with quantiles_non_monotone:speed_kmh.
        stream = {1: make_row(60.0, 50.0, 40.0, 0.1, 0.2, 0.4)}
        result = robust_edge_costs({1: self.ATTRS[1]}, {1: 50.0}, {1: 0.2}, stream, 0.9)
        assert result.source == "point"
        assert result.reason == "non_monotonic:1"
        seg = result.per_segment[1]
        assert seg.source == "point"
        assert seg.reason == "non_monotonic:1"
        # Point-equal outputs (today's numbers, not the stream's).
        assert seg.time_q_sec == seg.time_p50_sec == pytest.approx(travel_sec(1000.0, 50.0))

    def test_non_monotone_congestion_knots_degrade_with_label(self):
        stream = {1: make_row(40.0, 50.0, 60.0, 0.5, 0.2, 0.4)}  # 0.5 > 0.2
        result = robust_edge_costs({1: self.ATTRS[1]}, {1: 50.0}, {1: 0.2}, stream, 0.9)
        assert result.source == "point"
        assert result.reason == "non_monotonic:1"

    def test_malformed_rows_label_invalid_quantiles(self):
        nan_row = SegmentQuantiles(
            1, 5,
            {"p10": 40.0, "p50": float("nan"), "p90": 60.0},
            {"p10": 0.1, "p50": 0.2, "p90": 0.4},
        )
        missing = SegmentQuantiles(
            1, 5, {"p10": 40.0, "p50": 50.0}, {"p10": 0.1, "p50": 0.2, "p90": 0.4}
        )
        garbage = SegmentQuantiles(
            1, 5,
            {"p10": "slow", "p50": 50.0, "p90": 60.0},
            {"p10": 0.1, "p50": 0.2, "p90": 0.4},
        )
        boolean = SegmentQuantiles(
            1, 5,
            {"p10": True, "p50": 50.0, "p90": 60.0},   # bools are not forecasts
            {"p10": 0.1, "p50": 0.2, "p90": 0.4},
        )
        for row in (nan_row, missing, garbage, boolean):
            result = robust_edge_costs(
                {1: self.ATTRS[1]}, {1: 50.0}, {1: 0.2}, {1: row}, 0.9
            )
            assert result.source == "point"
            assert result.reason == "invalid_quantiles:1"
            assert result.per_segment[1].time_q_sec == pytest.approx(
                travel_sec(1000.0, 50.0)
            )

    def test_strict_flag_degrades_whole_stream(self):
        stream = {
            1: make_row(40.0, 50.0, 60.0, 0.1, 0.2, 0.4),   # clean
            2: make_row(50.0, 60.0, 40.0, 0.1, 0.1, 0.2),   # non-monotone speed
        }
        lax = robust_edge_costs(
            self.ATTRS, {1: 50.0, 2: 40.0}, {1: 0.2, 2: 0.1}, stream, 0.9
        )
        assert lax.source == "quantile"
        assert lax.reason == "non_monotonic:2"
        assert lax.per_segment[1].source == "quantile"

        strict = robust_edge_costs(
            self.ATTRS, {1: 50.0, 2: 40.0}, {1: 0.2, 2: 0.1}, stream, 0.9, strict=True
        )
        assert strict.source == "point"
        assert strict.reason == "non_monotonic:2"
        assert all(c.source == "point" for c in strict.per_segment.values())
        # Whole-stream degradation == today's point outputs exactly.
        noop = robust_edge_costs(
            self.ATTRS, {1: 50.0, 2: 40.0}, {1: 0.2, 2: 0.1}, None
        )
        assert strict.time_q_scaled == noop.time_q_scaled
        assert strict.disruption_q_scaled == noop.disruption_q_scaled

    def test_point_defaults_match_solver_get_fallbacks(self):
        # Segment absent from the point tables: solver defaults 40 km/h /
        # 0.3 congestion / secondary volume (mip_solver.py:142-146).
        result = robust_edge_costs(
            {1: {"length_m": 500.0, "road_class": "secondary"}}, {}, {}, None
        )
        t = travel_sec(500.0, 40.0)
        assert result.time_p50_sec == pytest.approx(t)
        assert result.disruption_p50 == pytest.approx(0.3 * 2.0 * t)


# ─── Producer-convention cross-checks (the clash guard) ─────────────────────


class TestProducerConvention:
    """The leaf must accept exactly what the tree's only quantile producer
    emits and the canonical consumer accepts — and reject what it rejects.

    Pins the review finding: the leaf previously enforced the OPPOSITE
    speed-knot orientation, so producer-conformant rows were mislabeled
    non_monotonic and the robust objective could never engage.
    """

    ATTRS = {7: {"length_m": 1000.0, "road_class": "motorway"}}

    def test_producer_rows_are_accepted_and_price_the_slow_scenario(self):
        producer_row = {
            "speed_kmh": 30.0,
            "congestion_idx": 0.5,
            "quantiles": make_quantiles(25.0, 30.0, 35.0, 60.0),
        }
        bands, reason = validated_bands(producer_row)
        assert reason is None and bands is not None  # canonical consumer: ACCEPT

        sq = _segment_quantiles_from_row(7, 5, producer_row)
        result = robust_edge_costs(self.ATTRS, {7: 30.0}, {7: 0.5}, {7: sq}, 0.9)
        assert result.source == "quantile"  # leaf: ACCEPT (was 'point' pre-fix)
        assert result.reason is None
        # q=0.90 prices the producer's slow scenario: speed p10 (25) with
        # congestion p90 (derived from the slow bound: 1 - 25/60).
        t_slow = travel_sec(1000.0, 25.0)
        assert result.per_segment[7].time_q_sec == pytest.approx(t_slow)
        assert result.per_segment[7].disruption_q == pytest.approx(
            (1.0 - 25.0 / 60.0) * 5.0 * t_slow
        )
        assert result.per_segment[7].time_p50_sec == pytest.approx(
            travel_sec(1000.0, 30.0)
        )

    def test_leaf_validity_matches_validated_bands_both_directions(self):
        shapes = {
            "producer": {
                "speed_kmh": 30.0, "congestion_idx": 0.5,
                "quantiles": make_quantiles(25.0, 30.0, 35.0, 60.0),
            },
            "descending_speed": {  # the old leaf-only convention
                "speed_kmh": 50.0, "congestion_idx": 0.2,
                "quantiles": {
                    "speed_kmh": {"p10": 55.0, "p50": 50.0, "p90": 45.0},
                    "congestion_idx": {"p10": 0.1, "p50": 0.2, "p90": 0.3},
                },
            },
            "non_monotone_congestion": {
                "speed_kmh": 30.0, "congestion_idx": 0.2,
                "quantiles": {
                    "speed_kmh": {"p10": 25.0, "p50": 30.0, "p90": 35.0},
                    "congestion_idx": {"p10": 0.5, "p50": 0.2, "p90": 0.4},
                },
            },
        }
        expected_valid = {"producer": True, "descending_speed": False,
                          "non_monotone_congestion": False}
        for name, row in shapes.items():
            bands, reason = validated_bands(row)
            sq = _segment_quantiles_from_row(7, 5, row)
            result = robust_edge_costs(self.ATTRS, {7: 30.0}, {7: 0.2}, {7: sq}, 0.9)
            assert result.source == ("quantile" if expected_valid[name] else "point"), name
            assert (bands is not None) == expected_valid[name], (name, reason)
            # Where both reject, the leaf labels the offending segment.
            if not expected_valid[name]:
                assert result.reason is not None and ":7" in result.reason, name

    async def test_producer_rows_drive_the_router_end_to_end(self):
        # Foundation-shaped rows (make_quantiles-built) through the whole
        # corridor_router path: the P90 objective must ENGAGE, not degrade.
        router, _ = router_fixture(producer_rows=True)
        candidates = await router.find_routes(1, 4)
        assert router.last_objective_reason == "partial_coverage:3/4"
        best = candidates[0]
        assert best.segment_ids == [1, 3, 4]  # rerouted away from the slow tail
        # q=0.9: seg1 speed p10=45 (8 s) + seg3 point @30 (12 s) + seg4 (8 s).
        assert best.estimated_time_p90_sec == 28
        # Congestion p90 derives from the slow bounds: 1-45/60, 1-30/60, 1-45/60.
        assert best.disruption_p90 == pytest.approx(
            (1 - 45 / 60) * 5.0 * 8.0
            + 0.5 * 5.0 * 12.0
            + (1 - 45 / 60) * 5.0 * 8.0
        )


# ─── Mapping layer (corridor_router._segment_quantiles_from_row) ────────────


class TestMappingLayer:
    def test_absent_object_returns_none(self):
        assert _segment_quantiles_from_row(1, 5, {"speed_kmh": 50.0}) is None
        assert _segment_quantiles_from_row(1, 5, {}) is None
        assert _segment_quantiles_from_row(1, 5, "not a row") is None

    def test_values_pass_through_uncoerced(self):
        row = {
            "quantiles": {
                "speed_kmh": {"p10": True, "p50": "50.0", "p90": 60.0},
                "congestion_idx": {"p10": 0.1, "p50": 0.2, "p90": 0.4},
            }
        }
        sq = _segment_quantiles_from_row(3, 5, row)
        assert sq is not None
        # No float() coercion in the mapping layer: the raw values arrive.
        assert sq.speed_kmh_by_q["p10"] is True
        assert sq.speed_kmh_by_q["p50"] == "50.0"
        # ...and the leaf's strict validation labels them, not coerces them.
        result = robust_edge_costs(
            {3: {"length_m": 1000.0, "road_class": "motorway"}},
            {3: 50.0}, {3: 0.2}, {3: sq}, 0.9,
        )
        assert result.source == "point"
        assert result.reason == "invalid_quantiles:3"

    def test_shape_broken_object_labels_invalid_not_absent(self):
        row = {"quantiles": {"speed_kmh": "not-a-mapping"}}
        sq = _segment_quantiles_from_row(3, 5, row)
        assert sq is not None  # present-but-broken reaches the leaf
        result = robust_edge_costs(
            {3: {"length_m": 1000.0, "road_class": "motorway"}},
            {3: 50.0}, {3: 0.2}, {3: sq}, 0.9,
        )
        assert result.reason == "invalid_quantiles:3"  # not partial_coverage

    def test_extra_knot_keys_are_ignored_not_fatal(self):
        row = {
            "quantiles": {
                "speed_kmh": {"p10": 40.0, "p50": 50.0, "p90": 60.0, "p99": 70.0},
                "congestion_idx": {"p10": 0.1, "p50": 0.2, "p90": 0.4},
            }
        }
        sq = _segment_quantiles_from_row(3, 5, row)
        result = robust_edge_costs(
            {3: {"length_m": 1000.0, "road_class": "motorway"}},
            {3: 50.0}, {3: 0.2}, {3: sq}, 0.9,
        )
        assert result.source == "quantile"
        assert result.per_segment[3].time_q_sec == pytest.approx(
            travel_sec(1000.0, 40.0)
        )


# ─── Leaf: knob clamp + interpolation ────────────────────────────────────────


class TestKnobAndInterpolation:
    def test_quantile_clamped_to_registered_bounds(self):
        assert ROBUST_QUANTILE_MIN == 0.5
        assert ROBUST_QUANTILE_MAX == 0.99
        assert bound_robust_quantile(0.05) == 0.5   # never sub-median
        assert bound_robust_quantile(0.42) == 0.5
        assert bound_robust_quantile(0.75) == 0.75
        assert bound_robust_quantile(0.9) == 0.9
        assert bound_robust_quantile(0.999) == 0.99  # never a degenerate tail
        # A non-finite knob is a configuration error — loud, never clamped.
        with pytest.raises(ValueError, match="finite"):
            bound_robust_quantile(float("nan"))
        with pytest.raises(ValueError, match="finite"):
            bound_robust_quantile(float("inf"))

    def test_resolve_knob_defaults_to_generated_baseline(self):
        resolved = resolve_robust_knob(None)
        assert resolved["enabled"] is True
        assert resolved["quantile"] == float(BASELINE_ROBUST_ROUTING["quantile"])
        assert resolved["time_weight"] == BASELINE_ROBUST_ROUTING["time_weight"]
        assert resolved["disruption_weight"] == BASELINE_ROBUST_ROUTING["disruption_weight"]
        # Unknown keys are dropped (fail-closed to the registered shape).
        weird = resolve_robust_knob({"quantile": 0.1, "mystery": 9, "enabled": False})
        assert set(weird) == {"enabled", "quantile", "time_weight", "disruption_weight"}
        assert weird["enabled"] is False
        assert weird["quantile"] == 0.5  # clamped, not 0.1

    def test_objective_weights_guarded_to_unit_interval(self):
        resolved = resolve_robust_knob(
            {"time_weight": 5.0, "disruption_weight": -1.0}
        )
        assert resolved["time_weight"] == 1.0
        assert resolved["disruption_weight"] == 0.0

    def test_interpolation_between_knots_is_piecewise_linear(self):
        stream = {1: make_row(30.0, 50.0, 70.0, 0.1, 0.3, 0.5)}
        attrs = {1: {"length_m": 1000.0, "road_class": "motorway"}}
        mid = robust_edge_costs(attrs, {1: 50.0}, {1: 0.3}, stream, 0.7)
        # q=0.7: congestion at 0.7 (halfway p50..p90 -> 0.4); speed at
        # 1-0.7=0.3 (halfway p10..p50 -> 40).
        assert mid.per_segment[1].time_q_sec == pytest.approx(travel_sec(1000.0, 40.0))
        assert mid.per_segment[1].disruption_q == pytest.approx(
            0.4 * 5.0 * travel_sec(1000.0, 40.0)
        )
        # Above the outermost knots the honest answer clamps to the knot:
        # q=0.99 reads speed level 0.01 -> clamped to the p10 (slow) knot;
        # it never extrapolates a slower speed than the stream supports.
        beyond = robust_edge_costs(attrs, {1: 50.0}, {1: 0.3}, stream, 0.99)
        assert beyond.per_segment[1].time_q_sec == pytest.approx(travel_sec(1000.0, 30.0))
        assert beyond.per_segment[1].disruption_q == pytest.approx(
            0.5 * 5.0 * travel_sec(1000.0, 30.0)
        )


# ─── Leaf: ETA band ──────────────────────────────────────────────────────────


class TestETABand:
    # Ascending speed knots: p10 slow, p90 fast.
    STREAM = {
        1: make_row(40.0, 50.0, 60.0, 0.1, 0.2, 0.4),
        2: make_row(32.0, 40.0, 48.0, 0.1, 0.1, 0.3),
    }
    LENGTHS = {1: 1000.0, 2: 500.0}

    def test_full_coverage_gives_ordered_band(self):
        outcome = eta_band_seconds(self.LENGTHS, self.STREAM, [1, 2])
        assert outcome.reason is None
        band = outcome.band
        assert band.source == "quantile"
        # Time quantiles: p10_sec <- FAST speed tail (p90), p90_sec <- SLOW
        # speed tail (p10).
        assert band.p10_sec == pytest.approx(travel_sec(1000, 60) + travel_sec(500, 48))
        assert band.p50_sec == pytest.approx(travel_sec(1000, 50) + travel_sec(500, 40))
        assert band.p90_sec == pytest.approx(travel_sec(1000, 40) + travel_sec(500, 32))
        assert band.p10_sec < band.p50_sec < band.p90_sec

    def test_missing_row_or_length_degrades_honestly(self):
        missing_row = eta_band_seconds(self.LENGTHS, self.STREAM, [1, 2, 3])
        assert missing_row.band is None
        assert missing_row.reason == "partial_coverage:2/3"
        missing_length = eta_band_seconds({1: 1000.0}, self.STREAM, [1, 2])
        assert missing_length.band is None
        assert missing_length.reason == "partial_coverage:1/2"
        empty = eta_band_seconds(self.LENGTHS, {}, [1, 2])
        assert empty.band is None
        assert empty.reason == "no_quantile_stream"

    def test_non_monotone_row_degrades_whole_band(self):
        bad = {1: self.STREAM[1], 2: make_row(50.0, 60.0, 40.0, 0.1, 0.1, 0.2)}
        outcome = eta_band_seconds(self.LENGTHS, bad, [1, 2])
        assert outcome.band is None
        assert outcome.reason == "non_monotonic:2"

    def test_producer_rows_give_ordered_band(self):
        row = {
            "quantiles": make_quantiles(25.0, 30.0, 35.0, 60.0),
        }
        sq = _segment_quantiles_from_row(7, 5, row)
        outcome = eta_band_seconds({7: 1000.0}, {7: sq}, [7])
        band = outcome.band
        assert outcome.reason is None
        assert band.p90_sec == pytest.approx(travel_sec(1000, 25))
        assert band.p50_sec == pytest.approx(travel_sec(1000, 30))
        assert band.p10_sec == pytest.approx(travel_sec(1000, 35))
        assert band.p10_sec < band.p50_sec < band.p90_sec


# ─── MIP solver overlay ──────────────────────────────────────────────────────


def linear_fixture():
    """1 → 2 → 3, all motorway, point speed 50, congestion 0.2."""
    attrs = {
        s: {"length_m": 1000.0, "road_class": "motorway", "lanes": 3}
        for s in (1, 2, 3)
    }
    edges = [(1, 2), (2, 3)]
    speeds = {s: 50.0 for s in attrs}
    cong = {s: 0.2 for s in attrs}
    return attrs, edges, speeds, cong


class TestMIPSolverRobustOverlay:
    WEIGHTS = {"time": 0.3, "disruption": 0.4, "security": 0.2, "complexity": 0.1}

    def test_absent_tables_solve_unchanged_with_pinned_numbers(self):
        attrs, edges, speeds, cong = linear_fixture()
        solver = ConvoyMIPSolver(self.WEIGHTS)
        result = solver.solve(edges, attrs, 1, 3, speeds, cong)
        assert result.segment_ids == [1, 2, 3]
        assert result.solve_status == "optimal"
        # Cross-module regression pin: solver edge costs are keyed by the
        # edge's SOURCE segment (mip_solver.py:141-150), so the path
        # [1,2,3] totals cover segments 1 and 2 — exactly the leaf's
        # no-op totals over those segments.
        leaf = robust_edge_costs({s: attrs[s] for s in (1, 2)}, speeds, cong, None)
        assert result.total_time_sec == pytest.approx(leaf.time_p50_sec)
        assert result.disruption_score == pytest.approx(leaf.disruption_p50)
        assert result.total_time_q_sec is None
        assert result.disruption_q is None

    def test_point_valued_tables_with_baseline_weights_are_byte_identical(self):
        attrs, edges, speeds, cong = linear_fixture()
        noop = robust_edge_costs(attrs, speeds, cong, None)
        solver = ConvoyMIPSolver(self.WEIGHTS)
        plain = solver.solve(edges, attrs, 1, 3, speeds, cong)
        overlay = solver.solve(
            edges, attrs, 1, 3, speeds, cong,
            robust_time_costs=noop.time_q_scaled,
            robust_disruption_costs=noop.disruption_q_scaled,
            robust_weights={"time": 0.3, "disruption": 0.4},
        )
        assert overlay.segment_ids == plain.segment_ids
        assert overlay.objective_value == pytest.approx(plain.objective_value)
        assert overlay.total_time_sec == plain.total_time_sec  # point meaning kept
        # q totals are per-edge (keyed by the edge's source segment), so
        # path [1,2,3] covers segments 1 and 2 — 2 × 72 s here.
        assert overlay.total_time_q_sec == pytest.approx(
            noop.per_segment[1].time_q_sec + noop.per_segment[2].time_q_sec
        )

    def test_robust_tables_flip_the_route_and_report_q_totals(self):
        # Two parallel branches: via-2 is faster at the point run, but the
        # slow tail (speed p10) on segment 2 is so bad that via-3 wins
        # under the robust objective.
        attrs = {
            s: {"length_m": 100.0, "road_class": "motorway", "lanes": 3}
            for s in (1, 2, 3, 4)
        }
        edges = [(1, 2), (2, 4), (1, 3), (3, 4)]
        speeds = {1: 50.0, 2: 50.0, 3: 30.0, 4: 50.0}
        cong = {s: 0.2 for s in attrs}
        stream = {
            1: make_row(45.0, 50.0, 55.0, 0.1, 0.2, 0.3),
            2: make_row(5.0, 50.0, 55.0, 0.1, 0.2, 0.5),   # slow tail on 2
            4: make_row(45.0, 50.0, 55.0, 0.1, 0.2, 0.3),
        }
        result = robust_edge_costs(attrs, speeds, cong, stream, 0.9)

        solver = ConvoyMIPSolver(self.WEIGHTS)
        point_run = solver.solve(edges, attrs, 1, 4, speeds, cong)
        assert point_run.segment_ids == [1, 2, 4]  # today's behaviour
        assert point_run.total_time_q_sec is None

        robust_run = solver.solve(
            edges, attrs, 1, 4, speeds, cong,
            robust_time_costs=result.time_q_scaled,
            robust_disruption_costs=result.disruption_q_scaled,
            robust_weights={"time": 0.3, "disruption": 0.4},
        )
        assert robust_run.segment_ids == [1, 3, 4]  # the P90 objective reroutes
        # q totals from the overlay tables along the chosen path (edges
        # (1,3),(3,4) → source segments 1 and 3):
        assert robust_run.total_time_q_sec == pytest.approx(
            travel_sec(100, 45) + travel_sec(100, 30)
        )
        # Point totals keep today's meaning even under the robust
        # objective (per-edge over source segments 1 and 3).
        assert robust_run.total_time_sec == pytest.approx(
            travel_sec(100, 50) + travel_sec(100, 30)
        )


# ─── Router integration ──────────────────────────────────────────────────────


class FakeGraphConn:
    """Dispatches fetch() on the SQL text: road_segments vs adjacency."""

    def __init__(self, seg_rows, adj_rows):
        self._seg_rows = seg_rows
        self._adj_rows = adj_rows

    async def fetch(self, query, *args):
        if "corridor.road_segments" in query:
            return self._seg_rows
        return self._adj_rows


class FakeGraphPool:
    def __init__(self, seg_rows, adj_rows):
        self._conn = FakeGraphConn(seg_rows, adj_rows)

    def acquire(self):
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        pass


class FakeGraphDB:
    def __init__(self, seg_rows, adj_rows, nodes):
        self._pool = FakeGraphPool(seg_rows, adj_rows)
        self._nodes = nodes

    async def multi_hop_neighbors(self, segment_id, hops=1):
        return [n for n in self._nodes if n != segment_id]

    async def k_shortest_paths(self, origin, destination, k):
        return []


class StubForecaster:
    """Duck-typed FlowForecaster returning contract-shaped rows.

    Rows carry the point fields always and the optional ``quantiles``
    object per the forecast-quantile contract; the payload is
    horizon-keyed exactly like the real ForecastProvider output.
    """

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    async def predict(self, segment_ids, horizons_min):
        self.calls.append((list(segment_ids), list(horizons_min)))
        return {
            sid: {h: dict(self._rows[sid]) for h in horizons_min}
            for sid in segment_ids
            if sid in self._rows
        }


def router_fixture(with_quantiles=False, robust_routing=None, producer_rows=False):
    seg_rows = [
        {"segment_id": 1, "road_class": "motorway", "lanes": 3,
         "speed_limit_kmh": 60, "length_m": 100.0},
        {"segment_id": 2, "road_class": "motorway", "lanes": 3,
         "speed_limit_kmh": 60, "length_m": 100.0},
        {"segment_id": 3, "road_class": "motorway", "lanes": 3,
         "speed_limit_kmh": 60, "length_m": 100.0},
        {"segment_id": 4, "road_class": "motorway", "lanes": 3,
         "speed_limit_kmh": 60, "length_m": 100.0},
    ]
    adj_rows = [
        {"from_segment_id": 1, "to_segment_id": 2},
        {"from_segment_id": 2, "to_segment_id": 4},
        {"from_segment_id": 1, "to_segment_id": 3},
        {"from_segment_id": 3, "to_segment_id": 4},
    ]
    rows = {
        1: {"speed_kmh": 50.0, "congestion_idx": 0.2},
        2: {"speed_kmh": 50.0, "congestion_idx": 0.2},
        3: {"speed_kmh": 30.0, "congestion_idx": 0.2},
        4: {"speed_kmh": 50.0, "congestion_idx": 0.2},
    }
    if producer_rows:
        # Producer-conformant bands via the REAL make_quantiles: p50 is
        # the point speed, congestion derived from the speed bounds.
        rows[1]["quantiles"] = make_quantiles(45.0, 50.0, 55.0, 60.0)
        rows[2]["quantiles"] = make_quantiles(5.0, 50.0, 55.0, 60.0)
        rows[4]["quantiles"] = make_quantiles(45.0, 50.0, 55.0, 60.0)
        rows[3] = {"speed_kmh": 30.0, "congestion_idx": 0.5}  # point-only
    elif with_quantiles:
        # Hand-built bands (ascending knots, hand-set congestion).
        rows[1]["quantiles"] = {
            "speed_kmh": {"p10": 45.0, "p50": 50.0, "p90": 55.0},
            "congestion_idx": {"p10": 0.1, "p50": 0.2, "p90": 0.3},
        }
        rows[2]["quantiles"] = {
            "speed_kmh": {"p10": 5.0, "p50": 50.0, "p90": 55.0},
            "congestion_idx": {"p10": 0.1, "p50": 0.2, "p90": 0.5},
        }
        rows[4]["quantiles"] = {
            "speed_kmh": {"p10": 45.0, "p50": 50.0, "p90": 55.0},
            "congestion_idx": {"p10": 0.1, "p50": 0.2, "p90": 0.3},
        }
    graph_db = FakeGraphDB(seg_rows, adj_rows, nodes=[1, 2, 3, 4])
    forecaster = StubForecaster(rows)
    router = CorridorRouter(
        graph_db=graph_db, forecaster=forecaster, robust_routing=robust_routing
    )
    return router, forecaster


class KspGraphDB(FakeGraphDB):
    """FakeGraphDB whose k-shortest-paths repeat the MIP optimum."""

    def __init__(self, *args, paths):
        super().__init__(*args)
        self._paths = paths
        self.k_requested: list[int] = []

    async def k_shortest_paths(self, origin, destination, k):
        self.k_requested.append(k)
        return [[{"node": n} for n in path] for path in self._paths[:k]]


class TestCandidateDeduplication:
    async def test_identical_candidates_are_returned_once(self):
        router, _ = router_fixture(with_quantiles=False)
        seg_rows = router._graph_db._pool._conn._seg_rows
        adj_rows = router._graph_db._pool._conn._adj_rows
        graph = KspGraphDB(
            seg_rows, adj_rows, [1, 2, 3, 4],
            paths=[[1, 2, 4], [1, 2, 4], [1, 3, 4]],  # KSP #1 == MIP optimum
        )
        router._graph_db = graph
        candidates = await router.find_routes(1, 4, max_candidates=3)
        paths = [tuple(c.segment_ids) for c in candidates]
        assert paths.count((1, 2, 4)) == 1
        assert set(paths) == {(1, 2, 4), (1, 3, 4)}
        assert len(paths) == len(set(paths))
        # one extra KSP path is asked for so de-duplication still leaves
        # max_candidates distinct routes when the graph has them
        assert graph.k_requested == [3]


class TestCorridorRouterRobust:
    async def test_no_quantile_stream_preserves_today_behaviour(self):
        router, forecaster = router_fixture(with_quantiles=False)
        candidates = await router.find_routes(1, 4)
        assert router.last_objective_reason == "no_quantile_stream"
        assert forecaster.calls[0][1] == [5]  # single 5-min point fetch, as today
        best = candidates[0]
        assert best.segment_ids == [1, 2, 4]
        assert best.estimated_time_sec == 21  # 3 × 7.2 s, today's number
        assert best.estimated_time_p90_sec is None
        assert best.disruption_p90 is None
        assert all(c.estimated_time_p90_sec is None for c in candidates)

    async def test_quantile_stream_drives_robust_objective(self):
        router, _ = router_fixture(with_quantiles=True)
        candidates = await router.find_routes(1, 4)
        assert router.last_objective_reason == "partial_coverage:3/4"
        best = candidates[0]
        assert best.segment_ids == [1, 3, 4]  # rerouted away from the slow tail
        # Overlays: 8.0 (seg1 @p10=45) + 12.0 (seg3 point @30) + 8.0 (seg4).
        assert best.estimated_time_p90_sec == 28
        assert best.disruption_p90 == pytest.approx(
            0.3 * 5.0 * 8.0 + 0.2 * 5.0 * 12.0 + 0.3 * 5.0 * 8.0
        )
        # Today's keys keep their point meaning.
        assert best.estimated_time_sec == 26  # 7.2 + 12.0 + 7.2

    async def test_disabled_knob_reverts_to_today_exactly(self):
        router, _ = router_fixture(
            with_quantiles=True, robust_routing={"enabled": False}
        )
        candidates = await router.find_routes(1, 4)
        assert router.last_objective_reason == "robust_routing_disabled"
        best = candidates[0]
        assert best.segment_ids == [1, 2, 4]  # point route, quantiles ignored
        assert best.estimated_time_sec == 21
        assert best.estimated_time_p90_sec is None

    async def test_non_monotone_stream_degrades_to_point(self):
        router, forecaster = router_fixture(with_quantiles=False)
        # Inject a non-monotone row for segment 2 (speed knots descend:
        # p10 > p50 — the convention violation).
        forecaster._rows[2]["quantiles"] = {
            "speed_kmh": {"p10": 60.0, "p50": 50.0, "p90": 55.0},
            "congestion_idx": {"p10": 0.1, "p50": 0.2, "p90": 0.3},
        }
        candidates = await router.find_routes(1, 4)
        assert router.last_objective_reason == "non_monotonic:2"
        best = candidates[0]
        assert best.segment_ids == [1, 2, 4]
        assert best.estimated_time_p90_sec is None

    async def test_invalid_wire_rows_label_invalid_quantiles(self):
        # The mapping layer no longer coerces: a boolean knot from a buggy
        # producer reaches the leaf and is labelled, not silently accepted.
        router, forecaster = router_fixture(with_quantiles=False)
        forecaster._rows[2]["quantiles"] = {
            "speed_kmh": {"p10": True, "p50": 50.0, "p90": 55.0},
            "congestion_idx": {"p10": 0.1, "p50": 0.2, "p90": 0.3},
        }
        candidates = await router.find_routes(1, 4)
        assert router.last_objective_reason == "invalid_quantiles:2"
        assert candidates[0].estimated_time_p90_sec is None


# ─── predict/eta band + optimize/routes additive keys ────────────────────────


class FittedPredictor:
    """Duck-typed ETAPredictor: fitted, constant 600 s answer."""

    _is_fitted = True

    def predict(self, features):
        return [600.0]


class UnfittedPredictor:
    _is_fitted = False


class FakeBandPool:
    def __init__(self, rows):
        self._rows = rows

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def fetch(self, query, *args):
        return self._rows


def make_band_provider(stream):
    async def provider(segment_ids, horizon_min):
        return {sid: stream[sid] for sid in segment_ids if sid in stream}

    return provider


ETA_PAYLOAD = {
    "route_length_m": 300.0, "num_segments": 3, "avg_predicted_speed": 50.0,
    "avg_predicted_congestion": 0.2, "hour": 10, "dow": 2,
}


class TestPredictEtaBand:
    def _client(self, predictor, provider=None, pool=None):
        from fastapi.testclient import TestClient
        import traffic_oracle.__main__ as main

        app = main.create_app()
        app.state.eta_predictor = predictor
        if provider is not None:
            app.state.forecast_quantiles = provider
        if pool is not None:
            app.state.pool = pool
        return TestClient(app)  # bare: lifespan NOT run

    def test_band_present_from_quantile_stream(self):
        stream = {
            1: make_row(40.0, 50.0, 60.0, 0.1, 0.2, 0.4),
            2: make_row(40.0, 50.0, 60.0, 0.1, 0.2, 0.4),
        }
        pool = FakeBandPool(
            [{"segment_id": 1, "length_m": 1000.0}, {"segment_id": 2, "length_m": 500.0}]
        )
        client = self._client(FittedPredictor(), make_band_provider(stream), pool)
        resp = client.post(
            "/api/v1/predict/eta", json={**ETA_PAYLOAD, "segment_ids": [1, 2]}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["eta_seconds"] == 600.0 and body["model"] == "histgbt"
        assert "eta_band_reason" not in body
        band = body["eta_band"]
        assert band["source"] == "quantile"
        # Time quantiles: p10 <- fast tail (60), p90 <- slow tail (40).
        assert band["p10_sec"] == pytest.approx(
            travel_sec(1000, 60) + travel_sec(500, 60), abs=0.1
        )
        assert band["p50_sec"] == pytest.approx(
            travel_sec(1000, 50) + travel_sec(500, 50), abs=0.1
        )
        assert band["p90_sec"] == pytest.approx(
            travel_sec(1000, 40) + travel_sec(500, 40), abs=0.1
        )
        assert band["p10_sec"] < band["p50_sec"] < band["p90_sec"]

    def test_band_requested_but_unavailable_labels_reason(self):
        client = self._client(FittedPredictor())  # no provider, no pool
        resp = client.post(
            "/api/v1/predict/eta", json={**ETA_PAYLOAD, "segment_ids": [1, 2]}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "eta_band" not in body
        assert body["eta_band_reason"] == "no_quantile_stream"
        assert body["eta_seconds"] == 600.0  # point payload untouched
        assert "eta_point_in_band" not in body and "eta_band_basis" not in body

    def test_non_monotone_provider_degrades_with_label(self):
        stream = {
            1: make_row(40.0, 50.0, 60.0, 0.1, 0.2, 0.4),
            2: make_row(50.0, 60.0, 40.0, 0.1, 0.2, 0.4),  # non-monotone speed
        }
        pool = FakeBandPool(
            [{"segment_id": 1, "length_m": 1000.0}, {"segment_id": 2, "length_m": 500.0}]
        )
        client = self._client(FittedPredictor(), make_band_provider(stream), pool)
        resp = client.post(
            "/api/v1/predict/eta", json={**ETA_PAYLOAD, "segment_ids": [1, 2]}
        )
        body = resp.json()
        assert "eta_band" not in body
        assert body["eta_band_reason"] == "non_monotonic:2"

    def test_band_is_independent_of_the_point_model(self):
        # The band needs only the quantile stream + lengths: an unfitted
        # HistGBT (model=fallback) no longer suppresses it.
        stream = {1: make_row(40.0, 50.0, 60.0, 0.1, 0.2, 0.4)}
        pool = FakeBandPool([{"segment_id": 1, "length_m": 1000.0}])
        client = self._client(UnfittedPredictor(), make_band_provider(stream), pool)
        resp = client.post(
            "/api/v1/predict/eta", json={**ETA_PAYLOAD, "segment_ids": [1]}
        )
        body = resp.json()
        assert body["model"] == "fallback"
        assert body["eta_band"] == body["eta_band_seconds"]
        band = body["eta_band_seconds"]
        assert band["p10_sec"] == pytest.approx(travel_sec(1000, 60), abs=0.1)
        assert band["p90_sec"] == pytest.approx(travel_sec(1000, 40), abs=0.1)
        assert "eta_band_reason" not in body

    def test_band_basis_and_point_agreement_are_labelled(self):
        # The point (request length/speed features) and the band (live
        # per-segment forecast) stand on different inputs: the response says
        # so, and whether the point lies inside the band, so a caller never
        # shows "ETA 450 s (band 364-401 s)" unexplained.
        stream = {1: make_row(40.0, 50.0, 60.0, 0.1, 0.2, 0.4)}
        pool = FakeBandPool([{"segment_id": 1, "length_m": 1000.0}])
        client = self._client(UnfittedPredictor(), make_band_provider(stream), pool)
        inside = client.post(
            "/api/v1/predict/eta",
            json={**ETA_PAYLOAD, "route_length_m": 1000.0, "segment_ids": [1]},
        ).json()
        assert inside["eta_band_basis"] == "live_segment_forecast"
        assert inside["eta_seconds"] == pytest.approx(72.0, abs=0.1)  # 1 km @ 50
        assert inside["eta_point_in_band"] is True
        outside = client.post(
            "/api/v1/predict/eta", json={**ETA_PAYLOAD, "segment_ids": [1]}
        ).json()  # 300 m point vs a 1 km segment band
        assert outside["eta_point_in_band"] is False
        assert outside["eta_seconds"] < outside["eta_band_seconds"]["p10_sec"]

    def test_fallback_without_a_stream_labels_the_band_reason(self):
        client = self._client(UnfittedPredictor())  # no provider, no pool
        body = client.post(
            "/api/v1/predict/eta", json={**ETA_PAYLOAD, "segment_ids": [1]}
        ).json()
        assert body["model"] == "fallback"
        assert body["eta_band_reason"] == "no_quantile_stream"
        assert "eta_band_seconds" not in body

    def test_fallback_without_segment_ids_stays_point_only(self):
        client = self._client(UnfittedPredictor())
        body = client.post("/api/v1/predict/eta", json=ETA_PAYLOAD).json()
        assert set(body) == {"eta_seconds", "model"}

    def test_no_band_fields_without_segment_ids(self):
        client = self._client(FittedPredictor())
        resp = client.post("/api/v1/predict/eta", json=ETA_PAYLOAD)
        body = resp.json()
        assert set(body) == {"eta_seconds", "model"}

    def test_optimize_routes_additive_keys_only_when_present(self):
        from fastapi.testclient import TestClient
        import traffic_oracle.__main__ as main

        router, _ = router_fixture(with_quantiles=True)
        app = main.create_app()
        app.state.router = router
        client = TestClient(app)
        resp = client.post(
            "/api/v1/optimize/routes",
            json={"origin_segment": 1, "destination_segment": 4},
        )
        assert resp.status_code == 200
        routes = resp.json()["routes"]
        assert routes, "MIP must produce at least one candidate"
        best = routes[0]
        assert best["segment_ids"] == [1, 3, 4]
        assert best["estimated_time_sec"] == 26  # today's key, point meaning
        assert best["estimated_time_p90_sec"] == 28
        assert "disruption_p90" in best

        router_off, _ = router_fixture(with_quantiles=False)
        app.state.router = router_off
        resp = client.post(
            "/api/v1/optimize/routes",
            json={"origin_segment": 1, "destination_segment": 4},
        )
        today = resp.json()["routes"][0]
        assert set(today) == {
            "route_id", "segment_ids", "total_distance_m", "estimated_time_sec",
            "disruption_score", "security_score", "composite_score",
        }
        assert today["estimated_time_sec"] == 21


# ─── Production provider wiring (lifespan) ───────────────────────────────────


class TestForecastQuantilesProvider:
    def test_lifespan_assigns_the_production_provider(self):
        # Symbolic pin (the TestMainWiring pattern): the lifespan wires
        # app.state.forecast_quantiles from the live forecaster — the
        # band path is production wiring, never test-only.
        import traffic_oracle.__main__ as main

        source = open(main.__file__, encoding="utf-8").read()
        assert "app.state.forecast_quantiles = build_forecast_quantiles_provider(" \
            in source

    async def test_provider_maps_rows_through_the_shared_layer(self):
        import traffic_oracle.__main__ as main

        rows = {
            1: {"speed_kmh": 30.0, "congestion_idx": 0.5,
                "quantiles": make_quantiles(25.0, 30.0, 35.0, 60.0)},
            2: {"speed_kmh": 40.0, "congestion_idx": 0.3},   # point-only row
            3: {"speed_kmh": 40.0, "congestion_idx": 0.3,
                "quantiles": {"speed_kmh": "broken"}},        # malformed row
        }
        provider = main.build_forecast_quantiles_provider(StubForecaster(rows))
        stream = await provider([1, 2, 3, 4], 5)
        # Point-only segment absent (degrades with the labelled reason);
        # malformed row PRESENT with empty knots (labelled downstream).
        assert set(stream) == {1, 3}
        assert stream[1].speed_kmh_by_q["p10"] == 25.0
        assert stream[3].speed_kmh_by_q == {}
        # Both rows consume identically through the leaf:
        attrs = {1: {"length_m": 1000.0, "road_class": "motorway"},
                 3: {"length_m": 1000.0, "road_class": "motorway"}}
        result = robust_edge_costs(attrs, {1: 30.0, 3: 40.0}, {1: 0.5, 3: 0.3},
                                   stream, 0.9)
        assert result.source == "quantile"
        assert result.reason == "invalid_quantiles:3"
        assert result.per_segment[1].time_q_sec == pytest.approx(
            travel_sec(1000.0, 25.0)
        )

    async def test_provider_band_end_to_end_with_producer_rows(self):
        # The full production path: factory provider + graph-DB lengths ->
        # an honest band with p10 < p50 < p90 from producer-conformant rows.
        import traffic_oracle.__main__ as main

        rows = {
            sid: {"speed_kmh": 30.0, "congestion_idx": 0.5,
                  "quantiles": make_quantiles(25.0, 30.0, 35.0, 60.0)}
            for sid in (1, 2)
        }
        provider = main.build_forecast_quantiles_provider(StubForecaster(rows))
        lengths = {1: 1000.0, 2: 500.0}
        stream = await provider([1, 2], 5)
        outcome = eta_band_seconds(lengths, stream, [1, 2])
        assert outcome.reason is None
        assert outcome.band.p90_sec == pytest.approx(
            travel_sec(1000, 25) + travel_sec(500, 25)
        )
        assert outcome.band.p10_sec == pytest.approx(
            travel_sec(1000, 35) + travel_sec(500, 35)
        )


# ─── Gate purity ─────────────────────────────────────────────────────────────


class TestGatePurity:
    def test_leaf_imports_without_heavy_stacks(self):
        # Importing the leaf, the producer-contract module and the
        # integrations (this file's module-scope imports) loads no torch,
        # onnxruntime or gymnasium.  Probed in a FRESH interpreter: the gate
        # venv carries gymnasium for the RL environment tests, which import
        # it earlier in the same session, so this session's sys.modules
        # says nothing about this chain.
        import os
        import subprocess
        import sys
        from pathlib import Path

        probe = (
            "import sys\n"
            "import traffic_oracle.adaptive.protocol_params_gen\n"
            "import traffic_oracle.optimize.corridor_router\n"
            "import traffic_oracle.optimize.mip_solver\n"
            "import traffic_oracle.optimize.quantile_objective\n"
            "import traffic_oracle.predict.forecast_quantiles\n"
            "print(sorted(m for m in ('torch', 'onnxruntime', 'gymnasium')"
            " if m in sys.modules))\n"
        )
        src = Path(__file__).resolve().parents[1] / "src"
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": str(src)},
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "[]"

    def test_leaf_source_stays_pure(self):
        import traffic_oracle.optimize.quantile_objective as leaf

        source = open(leaf.__file__, encoding="utf-8").read()
        # Import statements only (the docstring references optimize.rl in
        # prose as the thing the leaf must never touch).
        assert "from traffic_oracle.optimize.rl" not in source  # torch chain
        assert "import torch" not in source
        assert "from traffic_oracle.predict" not in source      # forecaster
        assert "import numpy" not in source  # stdlib + structlog + gen only
