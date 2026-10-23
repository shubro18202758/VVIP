"""Tests for the foundation adapter (t0-alpha ONNX INT8) — producer side.

All gate-venv pure python — no torch/onnxruntime anywhere: the graph runner
is a duck-typed stub. The adapter's rows follow the wave-2 quantile
contract (predict/forecast_quantiles.py): point fields
{"speed_kmh", "congestion_idx"} plus the OPTIONAL "quantiles" object this
producer always attaches when the artifact's bands are monotone.

Coverage:
    - Row shape: exactly the three contract keys per horizon, quantiles
      object exactly {speed_kmh, congestion_idx} x {p10, p50, p90}.
    - Horizon mapping: contract horizons read decode columns 1/2/3/6 (the
      5-minute grid steps), asserted with exact stub-encoded values.
    - Congestion derivation: point value clip(1 - v/free_flow, 0, 1) with
      exact representable cases; band edges map through the DECREASING
      transform in reversed order (speed p90 -> congestion p10).
    - Monotonicity: bands bracket the point everywhere; crossing bands
      from the graph are a LOUD failure.
    - Input-contract failures: empty/short/ragged/1-D/all-NaN history raise
      FoundationInputError with machine-readable reason tokens — never a
      silent zero-pad.
    - RollingHistoryWindow (300 s bins keyed by OBSERVATION time):
      samples inside one bin average into one step; a re-read of the same
      observation is ignored; a new step appears only when a new bin
      opens; gaps and ragged starts are NaN-padded on one aligned grid;
      not-ready (short or stale) segments are dropped from the batch,
      labelled; the batch fails loudly only when none is ready; anomalous
      samples never enter; synthetic provenance is kept per bin;
      authoritative bin upserts (the DB feed); max-steps clipping.
    - Per-segment free flow: derived congestion uses each segment's own
      free-flow speed when supplied.
    - Determinism: identical inputs give bitwise-identical floats.
    - Contract preservation: rows feed
      simulation.engine.shapes_from_forecaster with all four knots (the
      twin consumes point fields only — quantiles degrade to point there).
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from structlog.testing import capture_logs

from traffic_oracle.predict.nn.foundation_adapter import (
    BIN_SEC,
    CONTRACT_HORIZONS,
    DEFAULT_FREE_FLOW_KMH,
    DEFAULT_WINDOW_STEPS,
    MIN_CONTEXT_STEPS,
    FoundationInputError,
    RollingHistoryWindow,
    T0FoundationAdapter,
    compute_horizon_for,
    derive_congestion_idx,
    session_runner,
)
from traffic_oracle.simulation.engine import shapes_from_forecaster


# ─── Fakes ────────────────────────────────────────────────────────────────────


#: Exact p50 values per 1-based decode step — chosen so every derived
#: point congestion value is float-exact at free_flow 60 (30/45/60 divide
#: cleanly; 90 clips above free flow). Steps 2/4/5 are unused fillers.
SPEED_BY_STEP = {1: 30.0, 2: 45.0, 3: 60.0, 4: 50.0, 5: 50.0, 6: 90.0}


class StubT0Graph:
    """Duck-typed t0 graph runner: deterministic quantiles, records feeds.

    p50 at step ``s`` is ``SPEED_BY_STEP[s]`` (50.0 off the map), p10/p90
    sit 5 km/h either side — so the horizon->column mapping and the band
    extraction are asserted against exact values, not approximations.
    """

    def __init__(self, spread: float = 5.0):
        self.spread = spread
        self.calls: list[dict] = []

    def __call__(self, feeds: dict) -> np.ndarray:
        self.calls.append(feeds)
        rows = feeds["target_context"].shape[0]
        horizon = feeds["future_covariate_future"].shape[1]
        q = np.zeros((rows, horizon, 5), dtype=np.float32)
        for step in range(1, horizon + 1):
            p50 = SPEED_BY_STEP.get(step, 50.0)
            q[:, step - 1, :] = (p50 - self.spread,
                                 p50 - self.spread / 2,
                                 p50,
                                 p50 + self.spread / 2,
                                 p50 + self.spread)
        return q


def history(sid_values: dict[int, float], steps: int = MIN_CONTEXT_STEPS):
    """Per-segment constant speed history at the acceptance-floor length."""
    return {
        sid: np.full(steps, value, dtype=np.float32)
        for sid, value in sid_values.items()
    }


def make_adapter(runner=None) -> T0FoundationAdapter:
    return T0FoundationAdapter(runner or StubT0Graph())


# ─── Row shape: the wave-2 quantile contract ─────────────────────────────────


class TestQuantileContractRows:
    def test_rows_carry_exactly_point_fields_plus_quantiles(self):
        payload = make_adapter().forecast(history({100: 40.0, 200: 55.0}))

        assert set(payload) == {100, 200}
        for sid in (100, 200):
            assert set(payload[sid]) == set(CONTRACT_HORIZONS)
            for h in CONTRACT_HORIZONS:
                row = payload[sid][h]
                # Point fields stay exactly the frozen two keys, plus the
                # optional quantiles object this producer always attaches.
                assert set(row) == {"speed_kmh", "congestion_idx", "quantiles"}
                quant = row["quantiles"]
                assert set(quant) == {"speed_kmh", "congestion_idx"}
                for field in ("speed_kmh", "congestion_idx"):
                    assert set(quant[field]) == {"p10", "p50", "p90"}

    def test_horizon_mapping_reads_decode_columns_1_2_3_6(self):
        payload = make_adapter().forecast(history({100: 40.0}))

        expected_speed = {5: 30.0, 10: 45.0, 15: 60.0, 30: 90.0}
        for h, speed in expected_speed.items():
            row = payload[100][h]
            assert row["speed_kmh"] == speed
            assert row["quantiles"]["speed_kmh"] == {
                "p10": speed - 5.0, "p50": speed, "p90": speed + 5.0,
            }

    def test_congestion_point_derivation_exact_and_clipped(self):
        payload = make_adapter().forecast(history({100: 40.0}))

        assert payload[100][5]["congestion_idx"] == 0.5      # 1 - 30/60
        assert payload[100][10]["congestion_idx"] == 0.25    # 1 - 45/60
        assert payload[100][15]["congestion_idx"] == 0.0     # 1 - 60/60
        assert payload[100][30]["congestion_idx"] == 0.0     # 1 - 90/60 -> clip

    def test_congestion_bands_reverse_through_decreasing_transform(self):
        # Exact-representable fixture: speed bands (0, 30, 60) at every step
        # -> congestion p10 = derive(60) = 0.0, p50 = derive(30) = 0.5,
        #    p90 = derive(0) = 1.0 — reversed edge order keeps monotonicity.
        class CleanBands(StubT0Graph):
            def __call__(self, feeds):
                self.calls.append(feeds)
                rows = feeds["target_context"].shape[0]
                horizon = feeds["future_covariate_future"].shape[1]
                q = np.zeros((rows, horizon, 5), dtype=np.float32)
                q[:, :, 0] = 0.0   # p10 speed
                q[:, :, 2] = 30.0  # p50 speed
                q[:, :, 4] = 60.0  # p90 speed
                return q

        payload = make_adapter(CleanBands()).forecast(history({100: 40.0}))
        row = payload[100][5]
        assert row["congestion_idx"] == 0.5
        assert row["quantiles"]["congestion_idx"] == {
            "p10": 0.0, "p50": 0.5, "p90": 1.0,
        }

    def test_bands_bracket_the_point_everywhere(self):
        payload = make_adapter().forecast(history({100: 40.0, 200: 65.0}))
        for sid in (100, 200):
            for h in CONTRACT_HORIZONS:
                quant = payload[sid][h]["quantiles"]
                speed = payload[sid][h]["speed_kmh"]
                cong = payload[sid][h]["congestion_idx"]
                for field, point in (("speed_kmh", speed),
                                     ("congestion_idx", cong)):
                    band = quant[field]
                    assert band["p10"] <= point, (sid, h, field)
                    assert point <= band["p90"], (sid, h, field)

    def test_negative_median_clips_to_zero_speed_full_congestion(self):
        class NegativeStub(StubT0Graph):
            def __call__(self, feeds):
                self.calls.append(feeds)
                return np.zeros((feeds["target_context"].shape[0], 32, 5),
                                dtype=np.float32) - 3.0

        payload = make_adapter(NegativeStub()).forecast(history({100: 40.0}))
        row = payload[100][5]
        assert row["speed_kmh"] == 0.0
        assert row["congestion_idx"] == 1.0
        assert row["quantiles"]["speed_kmh"] == {"p10": 0.0, "p50": 0.0, "p90": 0.0}

    def test_determinism_identical_inputs_bitwise_identical_output(self):
        adapter = make_adapter()
        hist = history({100: 40.0, 200: 55.0, 300: 12.5})
        assert adapter.forecast(hist) == adapter.forecast(hist)

    def test_rows_feed_shapes_from_forecaster_all_four_knots(self):
        payload = make_adapter().forecast(history({100: 40.0}))
        shapes = shapes_from_forecaster(payload)
        assert set(shapes) == {100}
        assert set(shapes[100]) == {5, 10, 15, 30}
        assert set(shapes[100][5]) == {"speed_kmh", "congestion_idx"}
        assert isinstance(shapes[100][5]["congestion_idx"], float)
        assert not math.isnan(shapes[100][30]["speed_kmh"])


# ─── Input side ───────────────────────────────────────────────────────────────


class TestBuildInputs:
    def test_graph_feeds_shapes_dtypes_and_zero_row_covariates(self):
        stub = StubT0Graph()
        adapter = T0FoundationAdapter(stub)
        hist = {
            100: np.full(12, 40.0, dtype=np.float32),
            200: np.full(12, 55.0, dtype=np.float32),
        }
        feeds = adapter.build_inputs(hist)

        assert feeds["target_context"].shape == (2, 12)
        assert feeds["target_context"].dtype == np.float32
        assert feeds["target_group_ids"].tolist() == [0, 1]
        assert feeds["target_group_ids"].dtype == np.int32
        # Unused covariates are zero-row with the correct context/horizon width.
        assert feeds["future_covariate_context"].shape == (0, 12)
        assert feeds["future_covariate_future"].shape == (0, 32)
        assert feeds["future_covariate_group_ids"].shape == (0,)
        # The graph contract says compute_horizon is a multiple of 32.
        assert feeds["future_covariate_future"].shape[1] % 32 == 0

    def test_interior_nan_is_the_native_missing_marker_and_passes(self):
        series = np.full(16, 40.0, dtype=np.float32)
        series[3] = np.nan
        feeds = make_adapter().build_inputs({100: series})
        assert feeds["target_context"].shape == (1, 16)
        assert math.isnan(feeds["target_context"][0, 3])

    def test_input_contract_failures_are_loud_not_zero_padded(self):
        adapter = make_adapter()
        for reason, hist in (
            ("history_empty", {}),
            ("history_too_short", {100: np.full(11, 40.0)}),
            ("history_not_1d", {100: np.full((3, 12), 40.0)}),
            ("history_all_missing", {100: np.full(12, np.nan)}),
            (
                "history_ragged",
                {100: np.full(12, 40.0), 200: np.full(13, 40.0)},
            ),
        ):
            with pytest.raises(FoundationInputError) as exc:
                adapter.build_inputs(hist)
            # Reason token is machine-readable (first word) for the
            # foundation_input:<reason> degradation label.
            assert str(exc.value).split()[0].rstrip(":") == reason

    def test_minimum_context_boundary_is_inclusive(self):
        feeds = make_adapter().build_inputs(
            {100: np.full(MIN_CONTEXT_STEPS, 40.0)}
        )
        assert feeds["target_context"].shape == (1, MIN_CONTEXT_STEPS)


# ─── Output-side contract failures ────────────────────────────────────────────


class TestReshapeContractFailures:
    @staticmethod
    def _good_tensor():
        return StubT0Graph()({
            "target_context": np.zeros((1, 12), dtype=np.float32),
            "future_covariate_future": np.empty((0, 32), dtype=np.float32),
        })

    def _reshape(self, adapter, quantiles, sids=(100,)):
        return adapter.reshape(quantiles, list(sids))

    def test_crossing_quantiles_fail_loudly(self):
        class CrossingStub(StubT0Graph):
            def __call__(self, feeds):
                self.calls.append(feeds)
                q = super().__call__(feeds)
                q[0, 0, 0] = 99.0  # p10 above p50
                return q

        with pytest.raises(FoundationInputError, match="quantiles_crossing"):
            make_adapter(CrossingStub()).forecast(history({100: 40.0}))

    def test_nan_quantiles_fail_loudly_with_their_own_token(self):
        # NaN defeats comparison-based checks vacuously and survives
        # max(nan, 0.0) — the reviewer-repro'd escape route. The adapter
        # must reject it as a FoundationInputError (rung boundary catches
        # it), never let a NaN row or an escaped ValueError through.
        class NaNStub(StubT0Graph):
            def __call__(self, feeds):
                self.calls.append(feeds)
                return np.full(
                    (feeds["target_context"].shape[0],
                     feeds["future_covariate_future"].shape[1], 5),
                    np.nan,
                    dtype=np.float32,
                )

        with pytest.raises(FoundationInputError, match="quantiles_not_finite"):
            make_adapter(NaNStub()).forecast(history({100: 40.0}))

    def test_rank_row_level_and_horizon_mismatches(self):
        adapter = make_adapter()
        good = self._good_tensor()
        with pytest.raises(FoundationInputError, match="quantiles_wrong_rank"):
            self._reshape(adapter, good[0])
        with pytest.raises(FoundationInputError, match="quantiles_row_mismatch"):
            self._reshape(adapter, good, sids=(100, 200))
        with pytest.raises(FoundationInputError, match="quantiles_level_mismatch"):
            self._reshape(adapter, good[:, :, :4])
        with pytest.raises(FoundationInputError, match="quantiles_horizon_short"):
            self._reshape(adapter, good[:, :3, :])


# ─── RollingHistoryWindow ─────────────────────────────────────────────────────

#: A bin-aligned epoch (ms) so every sample's bin is exact in the tests.
T0_MS = 1_792_000_200 * 1000
BIN_MS = BIN_SEC * 1000


def fill(window, sid, steps, speed=40.0, start_bin=0, every=1, quality="real"):
    """One sample per ``every`` bins, ``steps`` samples, mid-bin stamps."""
    for i in range(steps):
        ts = T0_MS + (start_bin + i * every) * BIN_MS + BIN_MS // 2
        window.record(sid, ts, speed + i, quality)


class TestRollingHistoryWindow:
    def test_bins_are_keyed_by_observation_time_not_by_calls(self):
        window = RollingHistoryWindow()
        # twelve samples taken within ONE five-minute bin are ONE step
        for k in range(12):
            window.record(100, T0_MS + k * 20_000, 40.0 + k)
        assert window.observed_steps(100) == 1
        assert not window.ready(100)
        # the bin holds their mean, not the last value
        hist_bins = window._series[100]
        (only,) = hist_bins.values()
        assert only.mean == pytest.approx(45.5) and only.count == 12

    def test_a_new_step_only_on_a_new_bin_and_rereads_are_ignored(self):
        window = RollingHistoryWindow()
        assert window.record(100, T0_MS + 1_000, 40.0) is True     # new bin
        assert window.record(100, T0_MS + 60_000, 42.0) is False   # same bin
        assert window.record(100, T0_MS + 60_000, 99.0) is False   # re-read: ignored
        assert window._series[100][window.bin_of(T0_MS)].count == 2
        assert window.record(100, T0_MS + BIN_MS + 5, 44.0) is True  # next bin
        assert window.observed_steps(100) == 2

    def test_a_late_sample_lands_in_its_own_earlier_bin(self):
        window = RollingHistoryWindow()
        window.record(100, T0_MS + 2 * BIN_MS + 1, 40.0)
        assert window.record(100, T0_MS + 1, 30.0) is True   # late: earlier bin
        assert window.observed_steps(100) == 2
        small = RollingHistoryWindow(min_steps=1, max_steps=2)
        small.record(100, T0_MS + 10 * BIN_MS, 1.0)
        assert small.record(100, T0_MS, 2.0) is False        # older than the window

    def test_warm_up_then_ready_on_the_five_minute_grid(self):
        window = RollingHistoryWindow()
        fill(window, 100, MIN_CONTEXT_STEPS - 1)
        assert not window.ready(100)
        fill(window, 100, 1, speed=51.0, start_bin=MIN_CONTEXT_STEPS - 1)
        assert window.ready(100)
        hist = window.history([100])
        assert hist[100].shape == (MIN_CONTEXT_STEPS,)
        assert hist[100][-1] == 51.0  # newest last

    def test_gaps_are_nan_padded_on_one_aligned_grid(self):
        window = RollingHistoryWindow()
        fill(window, 100, 14)                       # bins 0..13
        fill(window, 200, 13, start_bin=1)          # bins 1..13 (ragged start)
        fill(window, 300, 12, start_bin=0, every=1)
        del window._series[300][window.bin_of(T0_MS) + 4]  # a hole at bin 4
        window.record(300, T0_MS + 12 * BIN_MS + 9, 33.0)  # bin 12
        window.record(300, T0_MS + 13 * BIN_MS + 9, 34.0)  # bin 13
        hist = window.history([100, 200, 300])
        lengths = {len(v) for v in hist.values()}
        assert lengths == {14}                        # ONE grid: bins 0..13
        assert math.isnan(hist[200][0])               # ragged start padded
        assert math.isnan(hist[300][4])               # gap padded
        assert not np.isnan(hist[100]).any()          # fully observed row
        assert hist[300][-1] == 34.0                  # aligned on the newest bin

    def test_not_ready_segments_are_dropped_and_labelled(self):
        window = RollingHistoryWindow()
        fill(window, 100, MIN_CONTEXT_STEPS)
        fill(window, 200, 3, start_bin=MIN_CONTEXT_STEPS - 3)   # warming up
        fill(window, 300, MIN_CONTEXT_STEPS, start_bin=-20)      # stale feed
        with capture_logs() as logs:
            hist = window.history([100, 200, 300, 999])
        assert set(hist) == {100}
        event = next(
            e for e in logs
            if e["event"] == "foundation_adapter.history_segments_not_ready"
        )
        assert event["segments"] == [200, 300, 999]

    def test_whole_batch_fails_loudly_only_when_none_is_ready(self):
        window = RollingHistoryWindow()
        fill(window, 200, 3)
        with pytest.raises(FoundationInputError, match="history_too_short"):
            window.history([200])
        with pytest.raises(FoundationInputError, match="history_empty"):
            window.history([999])

    def test_wall_clock_staleness_blocks_an_old_window(self):
        window = RollingHistoryWindow()
        fill(window, 100, MIN_CONTEXT_STEPS)
        fresh_now = T0_MS + MIN_CONTEXT_STEPS * BIN_MS
        assert window.ready(100, now_ms=fresh_now)
        stale_now = fresh_now + 10 * BIN_MS
        assert not window.ready(100, now_ms=stale_now)
        with pytest.raises(FoundationInputError, match="history_too_short"):
            window.history([100], now_ms=stale_now)

    def test_anomalous_samples_never_enter_and_provenance_is_kept(self):
        window = RollingHistoryWindow()
        assert window.record(100, T0_MS + 1, 40.0, "anomalous") is False
        assert window.segments() == []
        fill(window, 100, 2, quality="synthetic")
        fill(window, 200, 2, quality="real")
        fill(window, 300, 1, quality="real")
        fill(window, 300, 1, start_bin=1, quality="synthetic")
        assert window.quality([100, 200, 300]) == {
            100: "synthetic", 200: "real", 300: "mixed",
        }

    def test_set_bin_upserts_an_authoritative_aggregate(self):
        window = RollingHistoryWindow()
        v0 = window.version
        window.set_bin(100, 7, 38.5, count=40, synthetic=40)
        window.set_bin(100, 7, 39.5, count=44, synthetic=44)  # refined
        assert window._series[100][7].mean == 39.5
        assert window._series[100][7].count == 44
        assert window.version > v0
        window.set_bin(100, 8, float("nan"), count=3)  # refused
        assert 8 not in window._series[100]

    def test_window_clips_at_max_steps(self):
        window = RollingHistoryWindow(max_steps=16)
        fill(window, 100, 30)
        hist = window.history([100])
        assert hist[100].shape == (16,)
        assert hist[100][0] == 54.0 and hist[100][-1] == 69.0
        assert DEFAULT_WINDOW_STEPS == 96  # 8 h at 5-min steps


class TestPerSegmentFreeFlow:
    def test_congestion_uses_each_segments_own_free_flow(self):
        adapter = make_adapter()
        rows = adapter.forecast(
            history({1: 40.0, 2: 40.0}), free_flow={1: 100.0}
        )
        # step 1 p50 = 30 km/h: motorway (100) -> 0.7, unknown -> 60 -> 0.5
        assert rows[1][5]["congestion_idx"] == pytest.approx(0.7)
        assert rows[2][5]["congestion_idx"] == pytest.approx(0.5)
        band = rows[1][5]["quantiles"]["congestion_idx"]
        assert band["p50"] == pytest.approx(0.7)


# ─── Leaf helpers ─────────────────────────────────────────────────────────────


class TestLeafHelpers:
    def test_compute_horizon_is_multiple_of_32_covering_max_step(self):
        assert compute_horizon_for(CONTRACT_HORIZONS) == 32
        assert compute_horizon_for((5,)) == 32
        assert compute_horizon_for((30,)) == 32

    def test_off_contract_horizon_rejected_at_construction(self):
        with pytest.raises(FoundationInputError, match="horizon_off_contract"):
            T0FoundationAdapter(StubT0Graph(), horizons=(5, 20))

    def test_non_positive_free_flow_rejected(self):
        with pytest.raises(FoundationInputError, match="free_flow_not_positive"):
            T0FoundationAdapter(StubT0Graph(), free_flow_kmh=0.0)
        # Shared derivation raises the plain ValueError (it must not import
        # the adapter — no cycles); FoundationInputError IS-A ValueError.
        with pytest.raises(ValueError, match="free_flow_not_positive"):
            derive_congestion_idx(30.0, -1.0)

    def test_derive_congestion_matches_engine_formula(self):
        assert derive_congestion_idx(30.0, 60.0) == 0.5
        assert derive_congestion_idx(90.0, 60.0) == 0.0
        assert derive_congestion_idx(0.0, 60.0) == 1.0
        assert DEFAULT_FREE_FLOW_KMH == 60.0  # speed_norm/60 convention

    def test_session_runner_wraps_duck_typed_session(self):
        class Session:
            def run(self, names, feeds):
                return [np.zeros((1, 32, 5), dtype=np.float32)]

        runner = session_runner(Session())
        assert runner({}).shape == (1, 32, 5)

    def test_session_runner_fails_loudly_on_no_output(self):
        class EmptySession:
            def run(self, names, feeds):
                return []

        with pytest.raises(FoundationInputError, match="graph_no_output"):
            session_runner(EmptySession())({})
