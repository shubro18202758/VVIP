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
    - Ladder order: each rung's support gate BEFORE that rung; foundation
      BEFORE DSTGAT; heuristic last. Out-of-support => no model call.
      The foundation rung is gated in km/h (a 2 km/h gridlock is in its
      support), the DSTGAT rung by its normalised defaults.
    - Warm-up: the time-binned window needs >= 12 observed 5-min steps,
      so a cold window degrades loudly ("foundation_input:
      history_too_short") to the next rung; after warm-up the foundation
      rows carry the contract ``quantiles`` objects.  predict() never
      writes the window (the ingest history feed does).
    - Review fixes pinned: a NaN-emitting graph degrades LOUDLY with
      foundation_input:quantiles_not_finite (never an escaped ValueError);
      partially off-contract horizon requests are answered for the
      on-contract subset WITH a dropped-horizons warning.
    - Runtime: inference on the foundation worker thread (never the event
      loop); results cached per window version; mixed batches label their
      heuristic rows; anomalous latest rows feed nothing; the input
      provenance (synthetic = simulated feed) rides ForecastMeta.
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
from traffic_oracle.data.models import DataQuality
from traffic_oracle.predict.nn.foundation_adapter import (
    BIN_SEC,
    MIN_CONTEXT_STEPS,
    FoundationInputError,
    T0FoundationAdapter,
)

from conftest import MockValkey, make_observation

#: Fixed wall clock (mid-bin) every forecaster in this file reads.
BIN_MS = BIN_SEC * 1000
NOW_MS = 1_792_000_200 * 1000 + 100 * BIN_MS + BIN_MS // 2


def warm_window(forecaster, sid=100, steps=MIN_CONTEXT_STEPS, speed=50.0,
                quality="real"):
    """Fill the forecaster's window with ``steps`` 5-min bins ending NOW.

    This is what the ingest history feed does in production; predict()
    itself never writes the window.
    """
    window = forecaster.history_window
    for i in range(steps):
        ts = NOW_MS - (steps - 1 - i) * BIN_MS
        window.record(sid, ts, speed, quality)


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
        self.threads: list[str] = []

    def __call__(self, feeds: dict) -> np.ndarray:
        import threading

        self.calls.append(feeds)
        self.threads.append(threading.current_thread().name)
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
        clock_ms=lambda: NOW_MS,
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
            # 150 km/h: outside the foundation's km/h envelope (0-140)
            {100: make_observation(segment_id=100, speed_kmh=150.0)}
        )
        graph = StubGraph()
        forecaster, dstgat = make_forecaster(cache, foundation=graph, dstgat=True)
        warm_window(forecaster)
        with capture_logs() as logs:
            result = await forecaster.predict([100])

        assert graph.calls == [] and dstgat.calls == []  # NO model ran
        assert forecaster.last_degradation_reason == "out_of_support:speed_kmh"
        assert forecaster.last_forecast_rung == "heuristic"
        assert forecaster.last_quantile_reason == "quantiles_absent:heuristic_rung"
        assert result[100] == drift_expected(150.0, 0.3)  # exact drift math
        events = forecaster_events(logs)
        assert events.index("flow_forecaster.out_of_support") < events.index(
            "flow_forecaster.cache_fallback"
        )

    async def test_gridlock_is_in_support_for_the_km_h_foundation_rung(self):
        # 2 km/h (speed_norm 0.033) was gated out by the DSTGAT feature
        # defaults — exactly when forecasts matter; the foundation rung's
        # own km/h bounds keep it.
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=2.0)})
        graph = StubGraph()
        forecaster, dstgat = make_forecaster(cache, foundation=graph, dstgat=True)
        warm_window(forecaster, speed=2.0)
        result = await forecaster.predict([100])
        assert len(graph.calls) == 1 and dstgat.calls == []
        assert forecaster.last_forecast_rung == "foundation"
        assert forecaster.last_degradation_reason is None
        assert "quantiles" in result[100][5]

    async def test_foundation_preferred_over_dstgat_once_warm(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, dstgat = make_forecaster(cache, foundation=graph, dstgat=True)
        warm_window(forecaster)
        await forecaster.predict([100])

        assert len(graph.calls) == 1
        assert dstgat.calls == []
        assert forecaster.last_forecast_rung == "foundation"
        assert forecaster.last_degradation_reason is None
        assert forecaster.last_quantile_reason is None

    async def test_warm_up_degrades_loudly_to_dstgat(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, dstgat = make_forecaster(cache, foundation=graph, dstgat=True)
        warm_window(forecaster, steps=1)  # one 5-min step recorded
        with capture_logs() as logs:
            result = await forecaster.predict([100])

        assert graph.calls == []  # window not ready: rung refused
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
        result = await forecaster.predict([100])  # window empty

        assert graph.calls == []
        assert forecaster.last_degradation_reason == "foundation_input:history_empty"
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
    async def test_warm_rows_carry_contract_quantiles_and_exact_points(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        warm_window(forecaster)
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

    async def test_per_segment_free_flow_drives_derived_congestion(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        forecaster, _ = make_forecaster(cache, foundation=StubGraph())
        forecaster.set_free_flow({100: 100.0})  # a 100 km/h motorway
        warm_window(forecaster)
        result = await forecaster.predict([100])
        assert result[100][5]["congestion_idx"] == pytest.approx(1.0 - 41.0 / 100.0)

    async def test_cache_stores_point_fields_only_no_quantiles(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        warm_window(forecaster)
        await forecaster.predict([100])

        cached = cache._client._store["traffic:forecast:100:5"]
        assert cached == '{"speed_kmh":41.0,"congestion_idx":0.317}'
        assert "quantiles" not in cached  # lossy point-only cache by design

    async def test_requested_horizon_subset_returns_exactly_that_subset(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        warm_window(forecaster)
        result = await forecaster.predict([100], horizons_min=[5, 30])

        assert set(result[100]) == {5, 30}
        assert set(result[100][5]) == {"speed_kmh", "congestion_idx", "quantiles"}

    async def test_determinism_and_cache_per_window_version(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        warm_window(forecaster)
        first = await forecaster.predict([100])
        second = await forecaster.predict([100])
        assert first == second
        assert len(graph.calls) == 1  # same window version: one inference
        forecaster.history_window.set_bin(
            100, forecaster.history_window.bin_of(NOW_MS), 49.0, count=5
        )
        await forecaster.predict([100])
        assert len(graph.calls) == 2  # new data, new inference

    async def test_inference_runs_on_the_worker_thread_not_the_loop(self):
        import threading

        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        warm_window(forecaster)
        await forecaster.predict([100])
        assert graph.threads and graph.threads[0].startswith("foundation")
        assert graph.threads[0] != threading.current_thread().name
        forecaster.close()

    async def test_wired_rows_satisfy_the_consumer_contract(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        warm_window(forecaster)
        result = await forecaster.predict([100])
        for h, row in result[100].items():
            bands, reason = validated_bands(row)
            assert reason is None
            assert bands is not None
            assert bands["speed_kmh"]["p50"] == row["speed_kmh"]

    async def test_mixed_batch_labels_its_heuristic_rows(self):
        cache = FakeCache({
            100: make_observation(segment_id=100, speed_kmh=50.0),
            200: make_observation(segment_id=200, speed_kmh=30.0),
        })
        forecaster, _ = make_forecaster(cache, foundation=StubGraph())
        warm_window(forecaster, sid=100)  # 200 is still warming up
        result, meta = await forecaster.predict_with_meta([100, 200])
        assert "quantiles" in result[100][5]
        assert result[200] == drift_expected(30.0, 0.3)
        assert meta.rung == "foundation"
        assert meta.foundation_segments == (100,)
        assert meta.fallback_segments == (200,)
        assert meta.quantile_reason == "quantiles_absent:heuristic_rung"

    async def test_anomalous_latest_row_never_feeds_a_rung(self):
        # Segment 200's newest cached row is Tier-1 anomalous.  The
        # foundation rung never reads that row — its binned history holds
        # no anomalous sample — so it may answer 200 from that clean
        # history; the flagged segment is labelled either way.
        cache = FakeCache({
            100: make_observation(segment_id=100, speed_kmh=50.0),
            200: make_observation(segment_id=200, speed_kmh=30.0,
                                  data_quality=DataQuality.ANOMALOUS),
        })
        forecaster, _ = make_forecaster(cache, foundation=StubGraph())
        warm_window(forecaster, sid=100)
        warm_window(forecaster, sid=200)
        result, meta = await forecaster.predict_with_meta([100, 200])
        assert set(result) == {100, 200}
        assert meta.foundation_segments == (100, 200)
        assert "quantiles" in result[200][5]  # a foundation row, not 30 km/h
        assert meta.anomalous_latest == (200,)
        assert meta.anomalous_excluded == ()
        provenance = meta.provenance()
        assert provenance["anomalous_latest"] == [200]
        assert provenance["anomalous_excluded"] == []

    async def test_anomalous_segment_without_clean_history_gets_no_row(self):
        cache = FakeCache({
            100: make_observation(segment_id=100, speed_kmh=50.0),
            200: make_observation(segment_id=200, speed_kmh=30.0,
                                  data_quality=DataQuality.ANOMALOUS),
        })
        forecaster, _ = make_forecaster(cache, foundation=StubGraph())
        warm_window(forecaster, sid=100)  # 200 has no ready history
        result, meta = await forecaster.predict_with_meta([100, 200])
        # the heuristic rung would project the anomalous 30 km/h row: no
        assert set(result) == {100}
        assert meta.anomalous_excluded == (200,)
        assert meta.anomalous_latest == (200,)
        assert meta.provenance()["anomalous_excluded"] == [200]

    async def test_heuristic_rung_never_projects_an_anomalous_row(self):
        cache = FakeCache({
            100: make_observation(segment_id=100, speed_kmh=50.0),
            200: make_observation(segment_id=200, speed_kmh=30.0,
                                  data_quality=DataQuality.ANOMALOUS),
        })
        forecaster, _ = make_forecaster(cache)  # no model rung at all
        result, meta = await forecaster.predict_with_meta([100, 200])
        assert set(result) == {100}
        assert meta.rung == "heuristic"
        assert meta.anomalous_excluded == (200,)

    async def test_simulated_feed_provenance_is_reported(self):
        cache = FakeCache({
            100: make_observation(segment_id=100, speed_kmh=50.0,
                                  data_quality=DataQuality.SYNTHETIC),
        })
        forecaster, _ = make_forecaster(cache, foundation=StubGraph())
        warm_window(forecaster, quality="synthetic")
        _result, meta = await forecaster.predict_with_meta([100])
        assert meta.input_quality == {100: "synthetic"}
        provenance = meta.provenance()
        assert provenance["simulated_feed"] is True
        assert provenance["input_quality"] == {"synthetic": 1}
        assert provenance["rung"] == "foundation"


# ─── Review fixes: escape paths and labelling ────────────────────────────────


class TestReviewFixes:
    async def test_nan_graph_output_degrades_loudly_not_crashes(self):
        # Reviewer repro: a NaN-emitting graph used to escape the rung as
        # an uncaught ValueError (make_quantiles' plain-exception path);
        # it must instead label foundation_input:quantiles_not_finite and
        # fall through to the next rung.
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = NaNStubGraph()
        forecaster, dstgat = make_forecaster(cache, foundation=graph, dstgat=True)
        warm_window(forecaster)  # window ready for the NaN shot

        result = await forecaster.predict([100])  # must NOT raise

        assert forecaster.last_degradation_reason == (
            "foundation_input:quantiles_not_finite"
        )
        assert forecaster.last_forecast_rung == "dstgat"  # fell through
        assert forecaster.last_quantile_reason == "quantiles_absent:dstgat_point_head"
        assert result[100][5] == {"speed_kmh": 48.0, "congestion_idx": 0.25}
        assert len(dstgat.calls) == 1

    async def test_nan_graph_without_dstgat_degrades_to_heuristic(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        forecaster, _ = make_forecaster(cache, foundation=NaNStubGraph())
        warm_window(forecaster)
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

    async def test_predict_never_writes_the_window(self):
        # The old window grew one sample per predict() call, so twelve
        # calls over 35 s became "an hour" of context.  Only the ingest
        # history feed (observation-time bins) writes it now — in support
        # or not, no predict() call adds a step.
        cache = FakeCache({
            100: make_observation(segment_id=100, speed_kmh=50.0),
            200: make_observation(segment_id=200, speed_kmh=150.0),
        })
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        for _ in range(20):  # far past the 12-step floor
            await forecaster.predict([100, 200])
        assert forecaster.history_window.segments() == []
        assert graph.calls == []
        assert forecaster.last_degradation_reason == "foundation_input:history_empty"

    async def test_partially_off_contract_horizons_answered_and_labelled(self):
        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        warm_window(forecaster)
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


# ─── The ingest history feed (predict/history_feed.py) ───────────────────────


class FeedPool:
    """asyncpg-shaped fake: 300 s bin aggregates + speed limits."""

    def __init__(self, bins, limits):
        self.bins = bins
        self.limits = limits
        self.bin_args: list[tuple] = []

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def fetch(self, sql, *args):
        if "FROM traffic.observations" in sql:
            assert "IS DISTINCT FROM 'anomalous'" in sql  # Tier-1 never enters
            self.bin_args.append(args)
            return self.bins
        if "FROM corridor.road_segments" in sql:
            return self.limits
        raise AssertionError(sql)


class TestHistoryFeed:
    def _bins(self, sid, steps, synthetic=True):
        newest = NOW_MS // BIN_MS
        return [
            {"segment_id": sid, "bin": newest - (steps - 1 - i), "speed": 40.0 + i,
             "n": 30, "n_real": 0 if synthetic else 30,
             "n_synthetic": 30 if synthetic else 0}
            for i in range(steps)
        ]

    async def test_backfill_makes_the_rung_ready_and_labels_the_feed(self):
        from traffic_oracle.predict.history_feed import HistoryFeed

        cache = FakeCache({100: make_observation(segment_id=100, speed_kmh=50.0)})
        graph = StubGraph()
        forecaster, _ = make_forecaster(cache, foundation=graph)
        pool = FeedPool(self._bins(100, MIN_CONTEXT_STEPS),
                        [{"segment_id": 100, "speed_limit_kmh": 80}])
        feed = HistoryFeed(pool, forecaster)
        await feed.load_free_flow()
        assert await feed.refresh(hours=8.0) == MIN_CONTEXT_STEPS
        # the bin width is bound as a parameter (observation-time grid)
        assert pool.bin_args[0][1] == float(BIN_SEC)
        result, meta = await forecaster.predict_with_meta([100])
        assert meta.rung == "foundation" and len(graph.calls) == 1
        assert meta.input_quality == {100: "synthetic"}
        # derived congestion uses the segment's own 80 km/h free flow
        assert result[100][5]["congestion_idx"] == pytest.approx(1.0 - 41.0 / 80.0)
        health = feed.health()
        assert health["simulated_feed"] is True
        assert health["last_refresh_rows_quality"]["synthetic"] == 30 * MIN_CONTEXT_STEPS
        assert health["window"]["segments"] == 1

    async def test_refresh_reaggregates_the_open_bin_in_place(self):
        from traffic_oracle.predict.history_feed import HistoryFeed

        forecaster, _ = make_forecaster(FakeCache({}), foundation=StubGraph())
        bins = self._bins(100, 2)
        pool = FeedPool(bins, [])
        feed = HistoryFeed(pool, forecaster)
        await feed.refresh()
        bins[-1] = {**bins[-1], "speed": 10.0, "n": 31}
        await feed.refresh()
        window = forecaster.history_window
        assert window.observed_steps(100) == 2  # still two steps, not three
        assert window._series[100][NOW_MS // BIN_MS].mean == 10.0


class TestHttpProvenance:
    def test_predict_flow_reports_rung_and_simulated_feed(self):
        from fastapi.testclient import TestClient

        import traffic_oracle.__main__ as main

        cache = FakeCache({
            100: make_observation(segment_id=100, speed_kmh=50.0,
                                  data_quality=DataQuality.SYNTHETIC),
        })
        forecaster, _ = make_forecaster(cache, foundation=StubGraph())
        warm_window(forecaster, quality="synthetic")
        app = main.create_app()
        app.state.forecaster = forecaster
        body = TestClient(app).post(
            "/api/v1/predict/flow", json={"segment_ids": [100]}
        ).json()
        assert set(body["predictions"]["100"]["5"]) == {
            "speed_kmh", "congestion_idx", "quantiles",
        }
        prov = body["provenance"]
        assert prov["rung"] == "foundation"
        assert prov["simulated_feed"] is True
        assert prov["input_quality"] == {"synthetic": 1}
        forecaster.close()
