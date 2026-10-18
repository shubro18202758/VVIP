"""REAL-artifact tier: the t0 foundation rung inside FlowForecaster.

This file deliberately needs onnxruntime + the installed artifact and is
therefore SKIPPED with an explicit reason under the pure-python gate venv
(the verify_all pytest-tier convention for not-pure suites). It RUNS as
part of tier (b) of VVIP/scripts/check_t0_forecaster.sh under
VVIP/.venvs/wave2 against VVIP/models-local/t0/.

What it proves that the gate tier cannot (review finding): the two halves
— the pure wiring (stubbed loader) and the artifact (probe drives the
adapter directly) — are otherwise never validated TOGETHER. Here the REAL
``_load_foundation`` path constructs a REAL onnxruntime session inside the
forecaster and the full ladder runs end-to-end:

    - the real loader engages (adapter installed, foundation_loaded event);
    - warm-up calls degrade loudly to the heuristic rung, point-only;
    - the 12th in-support call engages the foundation rung and its rows
      carry the exact wave-2 contract quantiles, passing the consumer
      validator (validated_bands: p10 <= p50 <= p90 per field, finite);
    - the shared cache stores POINT-ONLY JSON (no quantiles riding it);
    - same-window determinism is bitwise (consecutive predict() calls
      differ by design — the rolling window grows one sample per call);
    - a present-but-CORRUPT artifact disables the rung loudly without
      bricking the forecaster (with the real runtime this exercises the
      onnxruntime InvalidProtobuf failure path).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from traffic_oracle.predict.flow_forecaster import FlowForecaster
from traffic_oracle.predict.forecast_quantiles import validated_bands

# find_spec does NOT import the module — safe at collection time in the
# pure gate venv.
_ORT_SPEC = importlib.util.find_spec("onnxruntime")
# tests/ -> traffic-oracle -> services -> VVIP
ARTIFACT = (
    Path(__file__).resolve().parents[3]
    / "models-local" / "t0" / "t0-alpha-grouped-int8.onnx"
)

pytestmark = pytest.mark.skipif(
    _ORT_SPEC is None or not ARTIFACT.exists(),
    reason="foundation artifact or onnxruntime not present — real-artifact "
           "tier; run under VVIP/.venvs/wave2 (check_t0_forecaster.sh tier b)",
)


class Obs:
    def __init__(self, speed: float) -> None:
        self.speed_kmh = speed
        self.congestion_idx = 0.3


class FakeClient:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


class FakeCache:
    def __init__(self, observations: dict[int, Obs]) -> None:
        self._observations = dict(observations)
        self._client = FakeClient()

    async def get_latest(self, segment_id: int):
        return self._observations.get(segment_id)


def make_forecaster(path) -> FlowForecaster:
    return FlowForecaster(
        onnx_model_path=None,
        cache=FakeCache({100: Obs(50.0)}),
        config=SimpleNamespace(max_nodes=8, lookback_steps=12, in_features=8),
        foundation_model_path=str(path),
    )


class TestRealLoaderInsideForecaster:
    async def test_real_loader_engages(self):
        forecaster = make_forecaster(ARTIFACT)
        assert forecaster._foundation_adapter is not None, (
            "real _load_foundation must install the adapter"
        )

    async def test_warmup_then_engagement_end_to_end(self):
        from traffic_oracle.predict.nn.foundation_adapter import MIN_CONTEXT_STEPS

        forecaster = make_forecaster(ARTIFACT)

        # Warm-up: short window -> loud degrade to the heuristic rung.
        first = await forecaster.predict([100])
        assert forecaster.last_degradation_reason == (
            "foundation_input:history_too_short"
        )
        assert forecaster.last_forecast_rung == "heuristic"
        assert set(first[100][5]) == {"speed_kmh", "congestion_idx"}  # point-only
        for _ in range(MIN_CONTEXT_STEPS - 1):
            await forecaster.predict([100])

        # 12th in-support call: the foundation rung answers with the
        # contract quantiles through the REAL graph.
        result = await forecaster.predict([100])
        assert forecaster.last_forecast_rung == "foundation"
        assert forecaster.last_degradation_reason is None
        assert forecaster.last_quantile_reason is None
        assert set(result[100]) == {5, 10, 15, 30}
        for h, row in result[100].items():
            assert set(row) == {"speed_kmh", "congestion_idx", "quantiles"}
            bands, reason = validated_bands(row)
            assert reason is None and bands is not None, (h, reason)
            band = bands["speed_kmh"]
            assert band["p10"] <= row["speed_kmh"] <= band["p90"]
            # Real forecasts of a constant-50 km/h window stay plausible.
            assert 0.0 < row["speed_kmh"] < 120.0

        # Cache rows stay point-only.
        cached = forecaster._cache._client.store["traffic:forecast:100:5"]
        assert "quantiles" not in cached
        assert cached.startswith('{"speed_kmh":')

        # Same-window determinism (frozen window -> bitwise identical).
        window = forecaster._foundation_window.history([100])
        assert forecaster._foundation_adapter.forecast(window) == (
            forecaster._foundation_adapter.forecast(window)
        )

    async def test_corrupt_artifact_fails_open_with_real_runtime(self, tmp_path):
        corrupt = tmp_path / "corrupt_t0.onnx"
        corrupt.write_bytes(b"definitely not a protobuf" * 64)
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            forecaster = make_forecaster(corrupt)
        assert forecaster._foundation_adapter is None
        assert any(
            e["event"] == "flow_forecaster.foundation_load_failed" for e in logs
        )
        result = await forecaster.predict([100])  # must NOT raise
        assert result and forecaster.last_degradation_reason == "no_model"
