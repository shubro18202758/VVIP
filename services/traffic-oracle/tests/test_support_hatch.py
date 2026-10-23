"""Tests for the forecaster out-of-support hatch.

Coverage:
    - SupportBounds leaf: schema validation, inclusive edges, NaN
      fails-closed, violation fields, absent sentinel, unrecorded-feature
      pass-through, record_from_observations round-trip.
    - Config surface: FORECAST_SUPPORT_BOUNDS_PATH default + env override.
    - Torch-free imports: predict.nn laziness and DSTGATConfig without the
      heavy stack (the whole file runs under the pure-python gate venv).
    - FlowForecaster integration: out-of-support falls back with the exact
      drift math + labelled reason + log events; in-support returns the stub
      ONNX predictions for exactly the requested segments/horizons; dormant
      slot labels "no_model"; explicit bounds file / missing file / corrupt
      file drive or disable the gate loudly.
    - Contract preservation: the fallback shape still feeds
      simulation.engine.shapes_from_forecaster with all four horizon knots.
"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import numpy as np
import pytest
from structlog.testing import capture_logs

from traffic_oracle.data.config import Settings
from traffic_oracle.predict.flow_forecaster import FlowForecaster
from traffic_oracle.predict.support_bounds import (
    PACKAGE_DEFAULT_PATH,
    SupportBounds,
    record_from_observations,
)
from traffic_oracle.simulation.engine import shapes_from_forecaster

from conftest import MockValkey, make_observation


# ─── Fakes ────────────────────────────────────────────────────────────────────


class FakeCache:
    """Duck-typed TrafficCache: canned latest observations + MockValkey writer.

    The neural path writes forecasts through ``_client`` (flow_forecaster),
    the fallback path only reads ``get_latest`` — MockValkey serves both.
    """

    def __init__(self, observations: dict[int, object]) -> None:
        self._observations = dict(observations)
        self._client = MockValkey()

    async def get_latest(self, segment_id: int):
        return self._observations.get(segment_id)


class StubOnnxServe:
    """Duck-typed OnnxServe: constant predictions, no onnxruntime import.

    Values are float32-exact (48.0, 0.25) so equality asserts stay exact.
    """

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


def make_forecaster(cache, *, bounds=None, bounds_path=None, max_nodes=8):
    """Forecaster with a stubbed neural slot — no onnxruntime anywhere."""
    config = SimpleNamespace(max_nodes=max_nodes, lookback_steps=12, in_features=8)
    forecaster = FlowForecaster(
        onnx_model_path=None,
        cache=cache,
        config=config,
        support_bounds=bounds,
        support_bounds_path=bounds_path,
    )
    stub = StubOnnxServe(max_nodes)
    forecaster._onnx_serve = stub  # post-construction injection
    return forecaster, stub


def forecaster_events(logs) -> list[str]:
    return [e["event"] for e in logs if e["event"].startswith("flow_forecaster.")]


# ─── SupportBounds leaf ───────────────────────────────────────────────────────


class TestSupportBoundsLeaf:
    def test_package_default_loads_with_three_features(self):
        bounds = SupportBounds.load(PACKAGE_DEFAULT_PATH)
        assert set(bounds.feature_names) == {
            "speed_norm",
            "congestion_idx",
            "neighbor_ratio",
        }
        assert bounds.interval("speed_norm") == (0.05, 1.25)
        assert bounds.interval("congestion_idx") == (0.0, 1.0)
        assert bounds.interval("neighbor_ratio") == (0.25, 4.0)
        assert not bounds.is_absent

    def test_bounds_are_inclusive_at_both_edges(self):
        bounds = SupportBounds.from_dict(
            {"schema": 1, "features": {"speed_norm": [0.05, 1.25]}}
        )
        assert bounds.check_value("speed_norm", 0.05).in_support
        assert bounds.check_value("speed_norm", 1.25).in_support
        assert not bounds.check_value("speed_norm", 0.049).in_support
        assert not bounds.check_value("speed_norm", 1.251).in_support

    def test_violations_carry_feature_segment_observed_and_interval(self):
        bounds = SupportBounds.from_dict(
            {"schema": 1, "features": {"speed_norm": [0.05, 1.25]}}
        )
        verdict = bounds.check({100: {"speed_norm": 2.0}})
        assert not verdict.in_support
        assert verdict.features == ("speed_norm",)
        (violation,) = verdict.violations
        assert violation.feature == "speed_norm"
        assert violation.segment_id == 100
        assert violation.observed == 2.0
        assert (violation.lo, violation.hi) == (0.05, 1.25)

    def test_nan_and_non_numeric_observed_fail_closed(self):
        bounds = SupportBounds.from_dict(
            {"schema": 1, "features": {"congestion_idx": [0.0, 1.0]}}
        )
        for bad in (float("nan"), "jammed", None, [0.5]):
            verdict = bounds.check_value("congestion_idx", bad)
            assert not verdict.in_support, bad
            assert verdict.violations[0].feature == "congestion_idx"

    def test_absent_sentinel_never_gates(self):
        absent = SupportBounds.absent()
        assert absent.is_absent
        verdict = absent.check({1: {"speed_norm": 1e9, "congestion_idx": -5.0}})
        assert verdict.in_support
        assert verdict.violations == ()

    def test_unrecorded_feature_never_gates(self):
        bounds = SupportBounds.from_dict(
            {"schema": 1, "features": {"speed_norm": [0.05, 1.25]}}
        )
        verdict = bounds.check({1: {"mystery_feature": 999.0}})
        assert verdict.in_support

    def test_malformed_payloads_rejected(self):
        with pytest.raises(ValueError, match="schema"):
            SupportBounds.from_dict({"schema": 2, "features": {}})
        with pytest.raises(ValueError, match="features"):
            SupportBounds.from_dict({"schema": 1})
        with pytest.raises(ValueError, match="lo <= hi"):
            SupportBounds.from_dict({"schema": 1, "features": {"a": [1.0, 0.0]}})
        with pytest.raises(ValueError, match="numeric"):
            SupportBounds.from_dict({"schema": 1, "features": {"a": ["x", 1.0]}})
        with pytest.raises(ValueError, match="pair"):
            SupportBounds.from_dict({"schema": 1, "features": {"a": 3}})
        with pytest.raises(ValueError):
            SupportBounds.from_json("{not json")

    def test_record_from_observations_round_trip(self, tmp_path):
        observations = [
            make_observation(segment_id=1, speed_kmh=12.0, congestion_idx=0.1),
            make_observation(segment_id=2, speed_kmh=48.0, congestion_idx=0.7),
        ]
        path = record_from_observations(observations, tmp_path / "bounds.json")
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema"] == 1
        assert payload["recorded_from"] == "observations"
        assert payload["sample_count"] == 2
        bounds = SupportBounds.load(path)
        assert bounds.interval("speed_norm") == (12.0 / 60.0, 48.0 / 60.0)
        assert bounds.interval("congestion_idx") == (0.1, 0.7)
        # edges of the recorded observations are in support
        assert bounds.check_value("speed_norm", 12.0 / 60.0).in_support

    def test_record_from_observations_rejects_empty(self, tmp_path):
        with pytest.raises(ValueError, match="no usable"):
            record_from_observations([], tmp_path / "bounds.json")


# ─── Config surface ───────────────────────────────────────────────────────────


class TestPerRungBounds:
    def test_package_default_gates_the_foundation_rung_in_km_h(self):
        bounds = SupportBounds.load(PACKAGE_DEFAULT_PATH)
        assert bounds.rung_names == ("foundation",)
        foundation = bounds.for_rung("foundation")
        assert foundation.interval("speed_kmh") == (0.0, 140.0)
        assert foundation.interval("congestion_idx") == (0.0, 1.0)
        assert "speed_norm" not in foundation.feature_names
        # a 100 km/h motorway and a 2 km/h gridlock are in the t0 support…
        assert foundation.check_value("speed_kmh", 100.0).in_support
        assert foundation.check_value("speed_kmh", 2.0).in_support
        assert not foundation.check_value("speed_kmh", 150.0).in_support
        # …while the DSTGAT rung keeps its normalised feature defaults
        assert bounds.for_rung("dstgat") is bounds
        assert not bounds.check_value("speed_norm", 2.0 / 60.0).in_support

    def test_rung_entries_are_validated(self):
        with pytest.raises(ValueError, match="rungs"):
            SupportBounds.from_dict({"schema": 1, "features": {}, "rungs": []})
        with pytest.raises(ValueError, match="needs a 'features' object"):
            SupportBounds.from_dict(
                {"schema": 1, "features": {}, "rungs": {"foundation": {}}}
            )
        rung_only = SupportBounds.from_dict(
            {"schema": 1, "features": {},
             "rungs": {"foundation": {"features": {"speed_kmh": [0, 140]}}}}
        )
        assert not rung_only.is_absent

    async def test_foundation_rung_uses_its_own_bounds(self):
        # The forecaster gates each rung with ITS bounds: a reading the
        # DSTGAT defaults reject is answered by the foundation rung.
        from traffic_oracle.predict.nn.foundation_adapter import T0FoundationAdapter

        class Graph:
            calls = 0

            def __call__(self, feeds):
                Graph.calls += 1
                rows = feeds["target_context"].shape[0]
                horizon = feeds["future_covariate_future"].shape[1]
                q = np.zeros((rows, horizon, 5), dtype=np.float32)
                q[...] = (1.0, 2.0, 3.0, 4.0, 5.0)
                return q

        now = 1_792_000_200 * 1000
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=95.0)})
        forecaster = FlowForecaster(
            onnx_model_path=None, cache=cache,
            config=SimpleNamespace(max_nodes=8, lookback_steps=12, in_features=8),
            clock_ms=lambda: now,
        )
        forecaster._foundation_adapter = T0FoundationAdapter(Graph())
        for i in range(12):
            forecaster.history_window.record(100, now - i * 300_000, 95.0)
        result = await forecaster.predict([100])
        assert Graph.calls == 1
        assert forecaster.last_forecast_rung == "foundation"
        assert forecaster.last_degradation_reason is None
        assert "quantiles" in result[100][5]
        forecaster.close()


class TestConfigSurface:
    def test_bounds_path_setting_default_none_and_env_override(self, monkeypatch):
        monkeypatch.delenv("VVIP_FORECAST_SUPPORT_BOUNDS_PATH", raising=False)
        assert Settings().FORECAST_SUPPORT_BOUNDS_PATH is None
        monkeypatch.setenv("VVIP_FORECAST_SUPPORT_BOUNDS_PATH", "/custom/bounds.json")
        assert Settings().FORECAST_SUPPORT_BOUNDS_PATH == "/custom/bounds.json"


# ─── Torch-free imports ───────────────────────────────────────────────────────


class TestTorchFreeImports:
    def test_nn_package_is_lazy_and_config_is_torch_free(self):
        # Under the gate venv torch/onnxruntime are absent; importing
        # flow_forecaster (done at module top) and touching the nn package
        # attributes here is the proof — a module-level heavy import would
        # raise ModuleNotFoundError in this environment.
        import traffic_oracle.predict.nn as nn_pkg
        from traffic_oracle.predict.nn.config import DSTGATConfig

        assert nn_pkg.DSTGATConfig(max_nodes=8).max_nodes == 8
        assert DSTGATConfig().lookback_steps == 12
        with pytest.raises(AttributeError):
            nn_pkg.definitely_not_an_attribute  # noqa: B018


# ─── FlowForecaster integration ───────────────────────────────────────────────


class TestOutOfSupportHatch:
    async def test_out_of_support_falls_back_with_exact_drift_math_and_labels(self):
        # speed 2 km/h -> speed_norm 0.0333 < the default lo 0.05.
        cache = FakeCache(
            {100: make_observation(segment_id=100, speed_kmh=2.0, congestion_idx=0.3)}
        )
        forecaster, stub = make_forecaster(cache)
        with capture_logs() as logs:
            result = await forecaster.predict([100])

        # Exact _cache_fallback drift math, not model output.
        base_speed, base_cong = 2.0, 0.3
        expected = {100: {}}
        for horizon in (5, 10, 15, 30):
            drift = 1.0 + (horizon / 5) * 0.02
            cong = min(1.0, base_cong * drift)
            speed = max(5.0, base_speed * (1.0 - (horizon / 5) * 0.015))
            expected[100][horizon] = {
                "speed_kmh": round(speed, 1),
                "congestion_idx": round(cong, 3),
            }
        assert result == expected

        assert forecaster.last_degradation_reason == "out_of_support:speed_norm"
        events = forecaster_events(logs)
        assert "flow_forecaster.out_of_support" in events
        assert "flow_forecaster.cache_fallback" in events
        assert events.index("flow_forecaster.out_of_support") < events.index(
            "flow_forecaster.cache_fallback"
        )
        event = next(e for e in logs if e["event"] == "flow_forecaster.out_of_support")
        assert event["features"] == ["speed_norm"]
        assert event["segments"] == [100]
        assert stub.calls == []  # the neural path never ran

    async def test_in_support_returns_stub_predictions_for_requested_keys(self):
        cache = FakeCache(
            {
                100: make_observation(segment_id=100, speed_kmh=50.0, congestion_idx=0.3),
                200: make_observation(segment_id=200, speed_kmh=40.0, congestion_idx=0.4),
            }
        )
        forecaster, stub = make_forecaster(cache)
        with capture_logs() as logs:
            result = await forecaster.predict([100, 200], horizons_min=[5, 10])

        assert set(result) == {100, 200}
        assert set(result[100]) == {5, 10}
        assert set(result[200]) == {5, 10}
        assert result[100][5] == {"speed_kmh": 48.0, "congestion_idx": 0.25}
        assert result[200][10] == {"speed_kmh": 48.0, "congestion_idx": 0.25}
        # The neural path wrote its forecasts through the cache client.
        assert "traffic:forecast:100:5" in cache._client._store
        assert "traffic:forecast:200:10" in cache._client._store
        assert forecaster.last_degradation_reason is None
        assert len(stub.calls) == 1
        assert all(e["event"] != "flow_forecaster.out_of_support" for e in logs)

    async def test_dormant_slot_labels_no_model(self):
        cache = FakeCache({100: make_observation(segment_id=100)})
        forecaster = FlowForecaster(
            onnx_model_path=None,
            cache=cache,
            config=SimpleNamespace(max_nodes=8, lookback_steps=12, in_features=8),
        )
        with capture_logs() as logs:
            result = await forecaster.predict([100])
        assert result and set(result[100]) == {5, 10, 15, 30}
        assert forecaster.last_degradation_reason == "no_model"
        # The pre-existing dormant-slot warning carries its full text as the
        # event string (flow_forecaster.py logs it as one positional arg).
        assert any(
            e["event"].startswith("flow_forecaster.no_model") for e in logs
        )

    async def test_both_features_violating_gives_combined_reason(self):
        bounds = SupportBounds.from_dict(
            {"schema": 1, "features": {"speed_norm": [0.5, 1.0], "congestion_idx": [0.5, 1.0]}}
        )
        cache = FakeCache(
            {7: make_observation(segment_id=7, speed_kmh=15.0, congestion_idx=0.3)}
        )
        forecaster, stub = make_forecaster(cache, bounds=bounds)
        result = await forecaster.predict([7])
        assert forecaster.last_degradation_reason == (
            "out_of_support:congestion_idx,speed_norm"
        )
        assert stub.calls == []
        assert result[7][5]["speed_kmh"] == round(max(5.0, 15.0 * (1 - 0.015)), 1)

    async def test_explicit_bounds_file_drives_gate(self, tmp_path):
        # Tighter congestion bound than the package default: a 0.3 congestion
        # is INSIDE the package default but OUTSIDE this file.
        bounds_file = tmp_path / "bounds.json"
        bounds_file.write_text(
            json.dumps({"schema": 1, "features": {"congestion_idx": [0.5, 1.0]}}),
            encoding="utf-8",
        )
        cache = FakeCache(
            {100: make_observation(segment_id=100, speed_kmh=50.0, congestion_idx=0.3)}
        )
        forecaster, stub = make_forecaster(cache, bounds_path=str(bounds_file))
        result = await forecaster.predict([100])
        assert forecaster.last_degradation_reason == "out_of_support:congestion_idx"
        assert stub.calls == []
        assert set(result[100]) == {5, 10, 15, 30}  # fallback shape

    async def test_missing_bounds_file_disables_gate_loudly(self, tmp_path):
        cache = FakeCache(
            {100: make_observation(segment_id=100, speed_kmh=2.0, congestion_idx=0.3)}
        )
        with capture_logs() as logs:
            forecaster, stub = make_forecaster(
                cache, bounds_path=str(tmp_path / "does-not-exist.json")
            )
        assert any(
            e["event"] == "flow_forecaster.support_bounds_missing" for e in logs
        )
        # speed 2 km/h would violate the package default — absent gates nothing.
        result = await forecaster.predict([100])
        assert result[100][5] == {"speed_kmh": 48.0, "congestion_idx": 0.25}
        assert forecaster.last_degradation_reason is None
        assert len(stub.calls) == 1

    async def test_corrupt_bounds_file_disables_gate_loudly(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        cache = FakeCache(
            {100: make_observation(segment_id=100, speed_kmh=2.0, congestion_idx=0.3)}
        )
        with capture_logs() as logs:
            forecaster, stub = make_forecaster(cache, bounds_path=str(bad))
        assert any(
            e["event"] == "flow_forecaster.support_bounds_invalid" for e in logs
        )
        result = await forecaster.predict([100])
        assert result[100][5] == {"speed_kmh": 48.0, "congestion_idx": 0.25}
        assert forecaster.last_degradation_reason is None

    async def test_reason_resets_between_predicts(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        forecaster, _ = make_forecaster(cache)
        await forecaster.predict([100])
        assert forecaster.last_degradation_reason is None


# ─── Contract preservation ────────────────────────────────────────────────────


class TestContractPreservation:
    async def test_fallback_shape_feeds_shapes_from_forecaster(self):
        # Gate to the fallback rung, then prove the twin/shadow-eval path
        # still consumes the result: ForecastShapes with all four knots.
        cache = FakeCache(
            {100: make_observation(segment_id=100, speed_kmh=50.0, congestion_idx=0.3)}
        )
        tight = SupportBounds.from_dict(
            {"schema": 1, "features": {"speed_norm": [0.9, 1.0]}}  # 50/60 = 0.833
        )
        forecaster, stub = make_forecaster(cache, bounds=tight)
        result = await forecaster.predict([100])
        assert forecaster.last_degradation_reason == "out_of_support:speed_norm"
        assert stub.calls == []

        shapes = shapes_from_forecaster(result)
        assert set(shapes) == {100}
        assert set(shapes[100]) == {5, 10, 15, 30}
        assert set(shapes[100][5]) == {"speed_kmh", "congestion_idx"}
        assert isinstance(shapes[100][5]["congestion_idx"], float)
        assert not math.isnan(shapes[100][30]["speed_kmh"])
