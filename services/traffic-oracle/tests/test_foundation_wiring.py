"""Tests for the foundation-rung wiring inside FlowForecaster (wave-2).

All gate-venv pure python — no torch/onnxruntime: the foundation rung is
injected as a duck-typed T0FoundationAdapter over a stub graph, the DSTGAT
rung as the hatch file's StubOnnxServe pattern.

Coverage:
    - Config surface: FOUNDATION_MODEL_PATH default None + env override.
    - Loader wiring: dormant when unset; dormant (no crash) when the path
      is set but missing; loaded when set and present (loader stubbed —
      the real loader pulls onnxruntime, out of the gate venv); a
      present-but-CORRUPT artifact disables the rung loudly and leaves
      the forecaster serving (fail-open, support-bounds philosophy).
    - Ladder order: support gate BEFORE any model rung; foundation BEFORE
      DSTGAT; heuristic last. Out-of-support => no model call at all.
    - Warm-up: the rolling window needs >= 12 samples, so the first calls
      degrade loudly ("foundation_input:history_too_short") to the next
      rung; after warm-up the foundation rows carry the contract
      ``quantiles`` objects.
    - Review fixes pinned: a NaN-emitting graph degrades LOUDLY with
      foundation_input:quantiles_not_finite (never an escaped ValueError);
      out-of-support observations never enter the rolling window;
      partially off-contract horizon requests are answered for the
      on-contract subset WITH a dropped-horizons warning.
    - Labels: last_degradation_reason / last_quantile_reason /
      last_forecast_rung are exact on every path; "no_model" semantics for
      the fully-dormant slot are preserved verbatim.
    - Cache: foundation rows are cached POINT-ONLY (exact JSON format, no
      quantiles riding the lossy cache).
    - Consumer contract (predict/forecast_quantiles.validated_bands):
      absent/malformed/non-monotone degrade to point with labelled
      reasons; valid rows validate.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from structlog.testing import capture_logs

from traffic_oracle.data.config import Settings
from traffic_oracle.predict.flow_forecaster import FlowForecaster
from traffic_oracle.predict.forecast_quantiles import (
    make_quantiles,
    validated_bands,
)
from traffic_oracle.predict.nn.foundation_adapter import (
    MIN_CONTEXT_STEPS,
    FoundationInputError,
    T0FoundationAdapter,
)

from conftest import MockValkey, make_observation


# ─── Fakes ────────────────────────────────────────────────────────────────────


class FakeCache:
    """Duck-typed TrafficCache: canned latest observations + MockValkey writer."""

    def __init__(self, observations: dict[int, object]) -> None:
        self._observations = dict(observations)
        self._client = MockValkey()

    async def get_latest(self, segment_id: int):
        return self._observations.get(segment_id)


class StubOnnxServe:
    """Duck-typed OnnxServe: constant point predictions, records calls."""

    def __init__(self, max_nodes: int, speed: float = 48.0, congestion: float = 0.25):
        self._max_nodes = max_nodes
        self._speed = speed
        self._congestion = congestion
        self.calls: list[dict] = []

    async def infer(self, inputs: dict) -> dict:
        self.calls.append({name: getattr(v, "shape", None) for name, v in inputs.items()})
        predictions = np.zeros((1, self._max_nodes, 4, 2), dtype=np.float32)
        predictions[..., 0] = self._speed
        predictions[..., 1] = self._congestion
        return {"predictions": predictions}


class StubGraph:
    """Duck-typed t0 graph: constant monotone bands around 40 km/h.

    p50 = 40 + decode_step keeps the horizon->column mapping observable;
    values are float32-exact so equality asserts stay exact.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, feeds: dict) -> np.ndarray:
        self.calls.append(feeds)
        rows = feeds["target_context"].shape[0]
        horizon = feeds["future_covariate_future"].shape[1]
        q = np.zeros((rows, horizon, 5), dtype=np.float32)
        for step in range(1, horizon + 1):
            p50 = 40.0 + step
            q[:, step - 1, :] = (p50 - 4.0, p50 - 2.0, p50, p50 + 2.0, p50 + 4.0)
        return q


class NaNStubGraph(StubGraph):
    """Graph emitting NaN quantiles — the reviewer-repro'd escape route.

    NaN defeats comparison-based monotonicity checks vacuously and
    survives max(nan, 0.0); the adapter must reject it with its own
      foundation_input:quantiles_not_finite
      label instead of letting a ValueError escape the rung boundary.
    """

    def __call__(self, feeds: dict) -> np.ndarray:
        self.calls.append(feeds)
        return np.full(
            (feeds["target_context"].shape[0],
             feeds["future_covariate_future"].shape[1], 5),
            np.nan,
            dtype=np.float32,
        )


def make_forecaster(
    cache,
    *,
    foundation: StubGraph | None = None,
    dstgat: bool = False,
    bounds=None,
    max_nodes: int = 8,
):
    """Forecaster with stubbed model rungs — no onnxruntime anywhere.

    ``foundation`` injects a stubbed foundation adapter (the rung wiring
    under test); ``dstgat`` additionally injects the DSTGAT stub so
    fall-through targets exist.
    """
    forecaster = FlowForecaster(
        onnx_model_path=None,
        cache=cache,
        config=SimpleNamespace(max_nodes=max_nodes, lookback_steps=12, in_features=8),
        support_bounds=bounds,
    )
    if foundation is not None:
        forecaster._foundation_adapter = T0FoundationAdapter(foundation)
    if dstgat:
        stub = StubOnnxServe(max_nodes)
        forecaster._onnx_serve = stub
        return forecaster, stub
    return forecaster, None


def forecaster_events(logs) -> list[str]:
    return [e["event"] for e in logs if isinstance(e.get("event"), str)
            and e["event"].startswith("flow_forecaster.")]


def drift_expected(base_speed: float, base_cong: float):
    """Exact _cache_fallback drift math (mirrors the hatch file)."""
    expected = {}
    for horizon in (5, 10, 15, 30):
        drift = 1.0 + (horizon / 5) * 0.02
        cong = min(1.0, base_cong * drift)
        speed = max(5.0, base_speed * (1.0 - (horizon / 5) * 0.015))
        expected[horizon] = {
            "speed_kmh": round(speed, 1),
            "congestion_idx": round(cong, 3),
        }
    return expected


# ─── Config surface ───────────────────────────────────────────────────────────


class TestConfigSurface:
    def test_foundation_model_path_defaults_to_none(self):
        assert Settings().FOUNDATION_MODEL_PATH is None

    def test_env_override_respected(self, monkeypatch):
        monkeypatch.setenv("VVIP_FOUNDATION_MODEL_PATH", "/opt/t0/model.onnx")
        assert Settings().FOUNDATION_MODEL_PATH == "/opt/t0/model.onnx"
        monkeypatch.delenv("VVIP_FOUNDATION_MODEL_PATH")
        assert Settings().FOUNDATION_MODEL_PATH is None


# ─── Loader wiring ────────────────────────────────────────────────────────────


class TestLoaderWiring:
    def test_unset_path_keeps_rung_dormant(self):
        forecaster = FlowForecaster(onnx_model_path=None)
        assert forecaster._foundation_adapter is None
        assert forecaster.last_forecast_rung is None

    def test_missing_file_keeps_rung_dormant_without_crashing(self, tmp_path):
        forecaster = FlowForecaster(
            onnx_model_path=None,
            foundation_model_path=str(tmp_path / "does-not-exist.onnx"),
        )
        assert forecaster._foundation_adapter is None

    def test_present_path_loads_through_the_lazy_loader(self, tmp_path, monkeypatch):
        # The real loader pulls onnxruntime (wave2/deployment); here it is
        # stubbed so the gate venv proves the WIRING: path set + file
        # present => loader invoked => adapter installed.
        artifact = tmp_path / "t0.onnx"
        artifact.write_bytes(b"stub")
        loaded: list[Path] = []
        sentinel = object()

        def fake_loader(path):
            loaded.append(path)
            return sentinel

        monkeypatch.setattr(FlowForecaster, "_load_foundation", staticmethod(fake_loader))
        forecaster = FlowForecaster(
            onnx_model_path=None, foundation_model_path=str(artifact)
        )
        assert loaded == [artifact]
        assert forecaster._foundation_adapter is sentinel


# ─── Ladder: gate -> foundation -> DSTGAT -> heuristic ────────────────────────


class TestLadderOrder:
    async def test_gate_runs_before_any_model_rung(self):
        cache = FakeCache(
            {100: make_observation(segment_id=100, speed_kmh=2.0)}  # 2/60 out of support
        )
        graph = StubGraph()
        forecaster, dstgat = make_forecaster(cache, foundation=graph, dstgat=True)
        with capture_logs() as logs:
            result = await forecaster.predict([100])

        assert graph.calls == [] and dstgat.calls == []  # NO model ran
        assert forecaster.last_degradation_reason == "out_of_support:speed_norm"
        assert forecaster.last_forecast_rung == "heuristic"
        assert forecaster.last_quantile_reason == "quantiles_absent:heuristic_rung"
        assert result[100] == drift_expected(2.0, 0.3)  # exact drift math
        events = forecaster_events(logs)
        assert events.index("flow_forecaster.out_of_support") < events.index(
            "flow_forecaster.cache_fallback"
        )

    async def test_foundation_preferred_over_dstgat_once_warm(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, dstgat = make_forecaster(cache, foundation=graph, dstgat=True)
        for _ in range(MIN_CONTEXT_STEPS):
            await forecaster.predict([100])

        # The 12th call's window holds 12 samples -> foundation engages and
        # DSTGAT only ever served the 11 warm-up calls.
        assert len(graph.calls) == 1
        assert len(dstgat.calls) == MIN_CONTEXT_STEPS - 1
        assert forecaster.last_forecast_rung == "foundation"
        assert forecaster.last_degradation_reason is None
        assert forecaster.last_quantile_reason is None

    async def test_warm_up_degrades_loudly_to_dstgat(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, dstgat = make_forecaster(cache, foundation=graph, dstgat=True)
        with capture_logs() as logs:
            result = await forecaster.predict([100])  # first call: 1 sample

        assert graph.calls == []  # window had 1 sample: rung refused
        assert len(dstgat.calls) == 1  # fell through to DSTGAT
        assert forecaster.last_degradation_reason == "foundation_input:history_too_short"
        assert forecaster.last_forecast_rung == "dstgat"
        assert forecaster.last_quantile_reason == "quantiles_absent:dstgat_point_head"
        assert result[100][5] == {"speed_kmh": 48.0, "congestion_idx": 0.25}
        assert "flow_forecaster.foundation_input" in forecaster_events(logs)

    async def test_warm_up_degrades_to_heuristic_without_dstgat(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        result = await forecaster.predict([100])

        assert graph.calls == []
        assert forecaster.last_degradation_reason == "foundation_input:history_too_short"
        assert forecaster.last_forecast_rung == "heuristic"
        assert result[100] == drift_expected(50.0, 0.3)

    async def test_fully_dormant_slot_labels_no_model_verbatim(self):
        cache = FakeCache({100: make_observation(segment_id=100)})
        forecaster, _ = make_forecaster(cache)  # neither rung wired
        with capture_logs() as logs:
            result = await forecaster.predict([100])

        assert result and set(result[100]) == {5, 10, 15, 30}
        assert forecaster.last_degradation_reason == "no_model"
        assert any(
            e["event"].startswith("flow_forecaster.no_model") for e in logs
        )
        assert forecaster.last_forecast_rung == "heuristic"


# ─── Foundation rows through the wired forecaster ────────────────────────────


class TestFoundationRows:
    async def warm(self, cache, forecaster, sid=100):
        for _ in range(MIN_CONTEXT_STEPS):
            await forecaster.predict([sid])

    async def test_warm_rows_carry_contract_quantiles_and_exact_points(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        await self.warm(cache, forecaster)
        result = await forecaster.predict([100])

        assert set(result[100]) == {5, 10, 15, 30}
        expected_p50 = {5: 41.0, 10: 42.0, 15: 43.0, 30: 46.0}
        for h, p50 in expected_p50.items():
            row = result[100][h]
            assert set(row) == {"speed_kmh", "congestion_idx", "quantiles"}
            assert row["speed_kmh"] == p50
            assert row["congestion_idx"] == pytest.approx(1.0 - p50 / 60.0)
            band = row["quantiles"]["speed_kmh"]
            assert (band["p10"], band["p50"], band["p90"]) == (p50 - 4.0, p50, p50 + 4.0)

    async def test_cache_stores_point_fields_only_no_quantiles(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        await self.warm(cache, forecaster)
        await forecaster.predict([100])

        cached = cache._client._store["traffic:forecast:100:5"]
        assert cached == '{"speed_kmh":41.0,"congestion_idx":0.317}'
        assert "quantiles" not in cached  # lossy point-only cache by design

    async def test_requested_horizon_subset_returns_exactly_that_subset(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        await self.warm(cache, forecaster)
        result = await forecaster.predict([100], horizons_min=[5, 30])

        assert set(result[100]) == {5, 30}
        assert set(result[100][5]) == {"speed_kmh", "congestion_idx", "quantiles"}

    async def test_determinism_identical_consecutive_calls(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        await self.warm(cache, forecaster)
        first = await forecaster.predict([100])
        second = await forecaster.predict([100])
        assert first == second

    async def test_wired_rows_satisfy_the_consumer_contract(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        await self.warm(cache, forecaster)
        result = await forecaster.predict([100])
        for h, row in result[100].items():
            bands, reason = validated_bands(row)
            assert reason is None
            assert bands is not None
            assert bands["speed_kmh"]["p50"] == row["speed_kmh"]


# ─── Review fixes: escape paths and labelling ────────────────────────────────


class TestReviewFixes:
    async def warm(self, forecaster, sid=100, calls=None):
        n = calls if calls is not None else MIN_CONTEXT_STEPS
        for _ in range(n):
            await forecaster.predict([sid])

    async def test_nan_graph_output_degrades_loudly_not_crashes(self):
        # Reviewer repro: a NaN-emitting graph used to escape the rung as
        # an uncaught ValueError (make_quantiles' plain-exception path);
        # it must instead label foundation_input:quantiles_not_finite and
        # fall through to the next rung.
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = NaNStubGraph()
        forecaster, dstgat = make_forecaster(cache, foundation=graph, dstgat=True)
        await self.warm(forecaster)  # window ready for the NaN shot

        result = await forecaster.predict([100])  # must NOT raise

        assert forecaster.last_degradation_reason == (
            "foundation_input:quantiles_not_finite"
        )
        assert forecaster.last_forecast_rung == "dstgat"  # fell through
        assert forecaster.last_quantile_reason == "quantiles_absent:dstgat_point_head"
        assert result[100][5] == {"speed_kmh": 48.0, "congestion_idx": 0.25}
        # DSTGAT served the 12 warm-up calls (short window) plus this NaN
        # fall-through — 13 total, none of them a crash.
        assert len(dstgat.calls) == MIN_CONTEXT_STEPS + 1

    async def test_nan_graph_without_dstgat_degrades_to_heuristic(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        forecaster, _ = make_forecaster(cache, foundation=NaNStubGraph())
        await self.warm(forecaster)
        result = await forecaster.predict([100])  # must NOT raise

        assert forecaster.last_degradation_reason == (
            "foundation_input:quantiles_not_finite"
        )
        assert forecaster.last_forecast_rung == "heuristic"
        assert result[100] == drift_expected(50.0, 0.3)

    def test_corrupt_artifact_disables_rung_loudly_startup_continues(self, tmp_path):
        # Reviewer repro: a present-but-corrupt artifact used to raise
        # (InvalidProtobuf with the real runtime) out of the constructor.
        # Fail-open instead: loud warning, rung dormant, forecaster serves.
        corrupt = tmp_path / "corrupt_t0.onnx"
        corrupt.write_bytes(b"this is not a protobuf" * 64)
        cache = FakeCache({100: make_observation(segment_id=100)})
        with capture_logs() as logs:
            forecaster = FlowForecaster(
                onnx_model_path=None,
                cache=cache,
                config=SimpleNamespace(
                    max_nodes=8, lookback_steps=12, in_features=8
                ),
                foundation_model_path=str(corrupt),
            )
        assert forecaster._foundation_adapter is None  # rung disabled
        assert any(
            e["event"] == "flow_forecaster.foundation_load_failed" for e in logs
        )

    async def test_corrupt_artifact_forecaster_still_predicts(self, tmp_path):
        corrupt = tmp_path / "corrupt_t0.onnx"
        corrupt.write_bytes(b"\x00garbage")
        cache = FakeCache({100: make_observation(segment_id=100)})
        forecaster = FlowForecaster(
            onnx_model_path=None,
            cache=cache,
            config=SimpleNamespace(max_nodes=8, lookback_steps=12, in_features=8),
            foundation_model_path=str(corrupt),
        )
        result = await forecaster.predict([100])
        assert result and forecaster.last_degradation_reason == "no_model"

    async def test_out_of_support_calls_never_feed_the_window(self):
        # Review finding: pushes used to happen BEFORE the support gate, so
        # out-of-support observations would silently feed future foundation
        # batches. Now only in-support observations are recorded.
        out_of_support = FakeCache(
            {100: make_observation(segment_id=100, speed_kmh=2.0)}
        )
        graph = StubGraph()
        forecaster, _ = make_forecaster(out_of_support, foundation=graph)
        for _ in range(20):  # far past the 12-step floor
            await forecaster.predict([100])
        assert forecaster.last_degradation_reason == "out_of_support:speed_norm"
        assert not forecaster._foundation_window.ready(100)  # window stayed cold
        assert graph.calls == []

        # Swap in an in-support cache: the rung engages only after its OWN
        # 12 in-support samples — prior out-of-support calls contributed 0.
        in_support = FakeCache(
            {100: make_observation(segment_id=100, speed_kmh=50.0)}
        )
        forecaster._cache = in_support
        for _ in range(MIN_CONTEXT_STEPS - 1):
            await forecaster.predict([100])
        assert not forecaster._foundation_window.ready(100)
        await forecaster.predict([100])  # 12th in-support sample
        assert forecaster._foundation_window.ready(100)
        assert len(graph.calls) == 1
        assert forecaster.last_forecast_rung == "foundation"

    async def test_partially_off_contract_horizons_answered_and_labelled(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        await self.warm(forecaster)
        with capture_logs() as logs:
            result = await forecaster.predict([100], horizons_min=[5, 60])

        assert set(result[100]) == {5}  # on-contract subset answered
        assert set(result[100][5]) == {"speed_kmh", "congestion_idx", "quantiles"}
        event = next(
            (e for e in logs
             if e["event"] == "flow_forecaster.foundation_horizons_dropped"),
            None,
        )
        assert event is not None, "dropped horizons must be labelled, not silent"
        assert event["dropped"] == [60]
        assert event["answered"] == [5]





class TestConsumerContract:
    def test_absent_quantiles_degrade_to_point_with_label(self):
        bands, reason = validated_bands({"speed_kmh": 40.0, "congestion_idx": 0.3})
        assert bands is None
        assert reason == "quantiles_absent"

    def test_valid_row_validates_exactly(self):
        row = {
            "speed_kmh": 30.0,
            "congestion_idx": 0.5,
            "quantiles": make_quantiles(25.0, 30.0, 35.0, 60.0),
        }
        bands, reason = validated_bands(row)
        assert reason is None
        assert bands == row["quantiles"]
        # Derived congestion band reverses through the decreasing transform.
        assert bands["congestion_idx"] == {
            "p10": 1.0 - 35.0 / 60.0, "p50": 0.5, "p90": 1.0 - 25.0 / 60.0,
        }

    def test_malformed_shapes_fail_with_labelled_reasons(self):
        base = {"speed_kmh": 40.0, "congestion_idx": 0.3}
        for bad, fragment in (
            ({"quantiles": "nope"}, "not_a_mapping"),
            ({"quantiles": {"speed_kmh": {}}}, "fields"),
            ({"quantiles": {"speed_kmh": {"p10": 1.0}, "congestion_idx": {}}}, "levels"),
        ):
            bands, reason = validated_bands({**base, **bad})
            assert bands is None
            assert reason is not None and reason.startswith("quantiles_malformed")

    def test_non_monotone_falls_back_to_point_with_field_label(self):
        row = {
            "speed_kmh": 30.0,
            "congestion_idx": 0.5,
            "quantiles": {
                "speed_kmh": {"p10": 31.0, "p50": 30.0, "p90": 35.0},
                "congestion_idx": {"p10": 0.1, "p50": 0.5, "p90": 0.9},
            },
        }
        bands, reason = validated_bands(row)
        assert bands is None
        assert reason == "quantiles_non_monotone:speed_kmh"

    def test_producer_make_quantiles_refuses_bad_bands_loudly(self):
        # Non-monotone AND non-finite bands are both refused with the
        # rung-boundary FoundationInputError (a plain ValueError would
        # escape `except FoundationInputError` in flow_forecaster).
        with pytest.raises(FoundationInputError, match="speed_bands_not_monotone"):
            make_quantiles(35.0, 30.0, 25.0, 60.0)
        with pytest.raises(FoundationInputError, match="speed_bands_not_finite"):
            make_quantiles(float("nan"), 30.0, 35.0, 60.0)
        with pytest.raises(FoundationInputError, match="speed_bands_not_finite"):
            make_quantiles(25.0, float("inf"), 35.0, 60.0)
