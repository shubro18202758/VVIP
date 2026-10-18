"""Flow Forecaster — Spatio-temporal traffic flow prediction model.

Uses a lightweight DSTGAT (Dynamic Spatio-Temporal Graph Attention Network)
operating on the corridor road graph to predict traffic speed/congestion
5-30 minutes into the future. ONNX-exported INT8 model (~82KB) runs via
OnnxServe within the strict VRAM budget.

Architecture:
    - Input: Historical traffic observations (speed, congestion) on road segments
    - Graph: Road network topology from PostGIS corridor.segment_adjacency
    - Output: Per-segment speed and congestion forecast at T+5, T+10, T+15, T+30 min

VRAM Policy:
    - Training: Offload to CPU if LLM is active; use gpu_arbiter to acquire GPU lease.
    - Inference: Export to ONNX → run via ONNX Runtime CUDA EP (~300MB VRAM)
    - Fallback: ONNX Runtime CPU EP if GPU memory pressure detected

Out-of-support hatch:
    predict() checks the live observation window against support bounds
    (predict/data/support_bounds.json by default, override via
    FORECAST_SUPPORT_BOUNDS_PATH) BEFORE any model inference; any recorded
    feature outside its interval abstains to the cache fallback with a
    ``flow_forecaster.out_of_support`` warning and
    ``last_degradation_reason = "out_of_support:<features>"`` — never an
    unlabelled model answer off distribution. A missing/corrupt bounds file
    disables the gate loudly and leaves behaviour unchanged.

Foundation rung (wave-2):
    When FOUNDATION_MODEL_PATH (VVIP_FOUNDATION_MODEL_PATH) points at the
    t0-alpha ONNX INT8 artifact (VVIP/models-local/t0/, Apache-2.0), a
    zero-shot quantile forecaster rung sits between the support gate and
    the DSTGAT/ONNX rung: support gate -> foundation -> DSTGAT -> heuristic
    cache fallback. The rung feeds the artifact REAL history — a rolling
    per-segment speed window (predict/nn.foundation_adapter
    .RollingHistoryWindow, one sample per predict() call, >= 12 steps
    before the rung engages) — and degrades loudly to the next rung with
    ``last_degradation_reason = "foundation_input:<reason>"`` whenever the
    artifact's input contract is not met (warm-up, ragged window, crossing
    quantiles). Forecast rows from this rung carry the optional
    ``quantiles`` object of the wave-2 contract
    (predict/forecast_quantiles.py): ``{speed_kmh: {p10,p50,p90},
    congestion_idx: {p10,p50,p90}}`` alongside the unchanged point fields.
    Point-only rungs (DSTGAT head, heuristic) omit ``quantiles`` and label
    the omission on ``last_quantile_reason``; ``last_forecast_rung``
    records which rung produced the returned payload. Side channels ride
    attributes + log events, never the point fields' values.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import structlog

from traffic_oracle.data.cache import TrafficCache
from traffic_oracle.data.graph_queries import CorridorGraphDB
from traffic_oracle.predict.nn.config import DSTGATConfig
from traffic_oracle.predict.nn.foundation_adapter import (
    HORIZON_TO_STEP,
    FoundationInputError,
)
from traffic_oracle.predict.support_bounds import (
    PACKAGE_DEFAULT_PATH,
    SupportBounds,
)
from traffic_oracle.runtime.memory_profiler import profile_memory

if TYPE_CHECKING:  # pragma: no cover — static type checkers only
    # Heavy imports stay OUT of module scope so this module (and everything
    # that imports it) loads in the pure-python gate venv: OnnxServe pulls
    # onnxruntime, and the construction sites below import it lazily — the
    # same pattern train()/export_onnx() already use for torch.
    from traffic_oracle.predict.nn.foundation_adapter import (
        RollingHistoryWindow,
        T0FoundationAdapter,
    )
    from traffic_oracle.runtime.onnx_serve import OnnxServe

logger = structlog.get_logger(__name__)

HORIZONS = [5, 10, 15, 30]  # minutes


def _load_support_bounds(path: str | None) -> tuple[SupportBounds, str]:
    """Resolve the support-bounds reference for the out-of-support gate.

    Explicit path wins; None falls back to the committed package default.
    A missing or malformed file disables the gate LOUDLY (warning + absent
    sentinel) rather than bricking predictions — an operator
    misconfiguration must not take the forecaster down with it.
    """
    resolved = Path(path) if path else PACKAGE_DEFAULT_PATH
    label = str(resolved)
    if not resolved.exists():
        logger.warning("flow_forecaster.support_bounds_missing", path=label)
        return SupportBounds.absent(), label
    try:
        bounds = SupportBounds.from_json(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "flow_forecaster.support_bounds_invalid", path=label, error=str(exc)
        )
        return SupportBounds.absent(), label
    return bounds, label


class FlowForecaster:
    """Spatio-temporal traffic flow prediction using DSTGAT via ONNX.

    Operates on the road segment graph to predict future corridor conditions.
    Exports to ONNX for low-footprint GPU inference.
    """

    def __init__(
        self,
        onnx_model_path: str | None = None,
        cache: TrafficCache | None = None,
        graph_db: CorridorGraphDB | None = None,
        arbiter: object | None = None,
        config: DSTGATConfig | None = None,
        support_bounds_path: str | None = None,
        support_bounds: SupportBounds | None = None,
        foundation_model_path: str | None = None,
    ) -> None:
        self._onnx_path = onnx_model_path
        self._cache = cache
        self._graph_db = graph_db
        self._arbiter = arbiter
        self._onnx_serve: OnnxServe | None = None
        self._config = config or DSTGATConfig()
        #: Reason for the last heuristic-rung answer ("no_model" |
        #: "out_of_support:<features>" | "foundation_input:<reason>"); rides
        #: log events + this attribute, never the forecast payload (the
        #: point fields stay {speed_kmh, congestion_idx} exactly).
        self.last_degradation_reason: str | None = None
        #: Wave-2 quantile contract side channel: None when the returned
        #: rows carry ``quantiles`` objects; otherwise
        #: "quantiles_absent:<producing rung>" labels the point-only answer.
        self.last_quantile_reason: str | None = None
        #: Which rung produced the last returned payload: "foundation" |
        #: "dstgat" | "heuristic" (None before the first predict).
        self.last_forecast_rung: str | None = None

        if onnx_model_path and Path(onnx_model_path).exists():
            # Lazy import keeps onnxruntime out of module scope (see above).
            from traffic_oracle.runtime.onnx_serve import OnnxServe

            self._onnx_serve = OnnxServe(onnx_model_path, arbiter)

        # Foundation rung: a SEPARATE slot from the dormant DSTGAT
        # ONNX_MODEL_PATH. Loaded only when the path is set AND present —
        # absent keeps the rung dormant with "no_model" semantics intact.
        # A PRESENT-but-unloadable artifact (corrupt protobuf, unreadable
        # bytes, missing onnxruntime) disables the rung loudly and leaves
        # behaviour unchanged — the same fail-open philosophy as the
        # support-bounds gate (a misconfigured file must not brick
        # startup; the reviewer-repro'd InvalidProtobuf crash).
        self._foundation_adapter: T0FoundationAdapter | None = None
        if foundation_model_path and Path(foundation_model_path).exists():
            try:
                self._foundation_adapter = self._load_foundation(
                    Path(foundation_model_path)
                )
            except Exception as exc:  # noqa: BLE001 — loader boundary, fails OPEN
                logger.warning(
                    "flow_forecaster.foundation_load_failed",
                    path=foundation_model_path,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        # Window builder owned by the adapter module (see plan): grows one
        # sample per predict() call from the cache's latest observation.
        from traffic_oracle.predict.nn.foundation_adapter import (
            RollingHistoryWindow,
        )

        self._foundation_window = RollingHistoryWindow()

        if support_bounds is not None:
            self._support_bounds = support_bounds
            self._bounds_path = support_bounds.source or "<injected>"
        else:
            self._support_bounds, self._bounds_path = _load_support_bounds(
                support_bounds_path
            )

        logger.info(
            "flow_forecaster.init",
            onnx_path=onnx_model_path,
            foundation_path=foundation_model_path,
            foundation_active=self._foundation_adapter is not None,
            bounds_path=self._bounds_path,
            bounds_features=list(self._support_bounds.feature_names),
        )

    @staticmethod
    def _load_foundation(path: Path) -> T0FoundationAdapter:
        """Load the t0-alpha ONNX INT8 artifact behind the foundation rung.

        Lazy onnxruntime import (module scope stays gate-venv pure). CPU
        execution provider only: measured on this aarch64 host
        (probe_foundation_t0.py); onnxruntime-gpu aarch64 availability is
        unverified, so the arbiter's GPU path is deliberately not wired
        here — the GpuArbiter keeps serving the DSTGAT rung.
        """
        import onnxruntime as ort

        from traffic_oracle.predict.nn.foundation_adapter import (
            T0FoundationAdapter,
            session_runner,
        )

        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 2
        options.graph_optimization_level = (
            ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        )
        session = ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        logger.info(
            "flow_forecaster.foundation_loaded",
            path=str(path),
            provider=session.get_providers()[0],
            size_mb=round(path.stat().st_size / 1_048_576, 1),
        )
        return T0FoundationAdapter(session_runner(session))

    @profile_memory
    async def predict(
        self,
        segment_ids: list[int],
        horizons_min: list[int] | None = None,
    ) -> dict:
        """Predict traffic flow for given segments at specified horizons.

        Args:
            segment_ids: Road segment IDs to forecast
            horizons_min: Prediction horizons in minutes (default: [5, 10, 15, 30])

        Returns:
            Dict mapping segment_id → {horizon_min: {speed_kmh, congestion_idx}}
        """
        if horizons_min is None:
            horizons_min = HORIZONS

        self.last_degradation_reason = None
        self.last_quantile_reason = None
        self.last_forecast_rung = None

        if self._foundation_adapter is None and self._onnx_serve is None:
            logger.warning("flow_forecaster.no_model, using cache fallback")
            self.last_degradation_reason = "no_model"
            return await self._cache_fallback(segment_ids, horizons_min)

        N_max = self._config.max_nodes
        T = self._config.lookback_steps
        N = min(len(segment_ids), N_max)
        active_ids = segment_ids[:N]

        # Build node features from cache
        node_features = np.zeros((1, N_max, T, 8), dtype=np.float32)
        window_values: dict[int, dict[str, float]] = {}
        history_pushes: list[tuple[int, float]] = []
        if self._cache is not None:
            for i, sid in enumerate(active_ids):
                obs = await self._cache.get_latest(sid)
                if obs is not None:
                    speed_norm = obs.speed_kmh / 60.0
                    # Fill last timestep (most recent)
                    node_features[0, i, T - 1, 0] = speed_norm
                    node_features[0, i, T - 1, 1] = obs.congestion_idx
                    node_features[0, i, T - 1, 7] = 1.0  # neighbor ratio default
                    # Exactly the features the runtime path feeds the model.
                    window_values[sid] = {
                        "speed_norm": speed_norm,
                        "congestion_idx": obs.congestion_idx,
                    }
                    history_pushes.append((sid, obs.speed_kmh))

        # Out-of-support hatch: the model answer outside its recorded feature
        # support is not trustworthy — abstain to the heuristic rung, loudly,
        # BEFORE any model inference (foundation and DSTGAT alike).
        # Segments with no cached observation are zero-filled by the loop
        # above and deliberately NOT gated (hatch scoped to recorded bounds
        # on observed values only).
        verdict = self._support_bounds.check(window_values)
        if not verdict.in_support:
            features = list(verdict.features)
            segments = sorted(
                v.segment_id for v in verdict.violations if v.segment_id is not None
            )
            logger.warning(
                "flow_forecaster.out_of_support",
                features=features,
                segments=segments,
                bounds_path=self._bounds_path,
            )
            self.last_degradation_reason = "out_of_support:" + ",".join(features)
            return await self._cache_fallback(segment_ids, horizons_min)

        # Foundation rung history: one REAL sample per IN-SUPPORT predict()
        # call — the rolling window the artifact needs (>= 12 steps before
        # the rung engages; never zero-padded). Recorded only AFTER the
        # support gate passes: an observation the gate just deemed
        # out-of-support must not silently feed future foundation batches
        # (review finding).
        for sid, speed in history_pushes:
            self._foundation_window.push(sid, speed)

        # Foundation rung (wave-2): zero-shot quantile forecaster over the
        # rolling history window — preferred over DSTGAT when wired.
        # Contract failures degrade LOUDLY to the next rung with a labelled
        # reason; they never zero-pad history nor fake bands.
        if self._foundation_adapter is not None:
            try:
                result = self._run_foundation(active_ids, horizons_min)
            except FoundationInputError as exc:
                reason = "foundation_input:" + str(exc).split()[0].rstrip(":")
                logger.warning(
                    "flow_forecaster.foundation_input",
                    reason=reason,
                    error=str(exc),
                )
                self.last_degradation_reason = reason
                # fall through to the next rung below
            else:
                self.last_forecast_rung = "foundation"
                self.last_quantile_reason = None  # rows carry quantiles
                await self._cache_forecast_rows(result)
                logger.info(
                    "flow_forecaster.predict",
                    segments=len(result),
                    horizons=horizons_min,
                    rung="foundation",
                )
                return result

        if self._onnx_serve is None:
            # Foundation rung absent or degraded, and no DSTGAT rung wired:
            # the heuristic is the last resort. last_degradation_reason
            # already carries the foundation_input:<reason> when applicable.
            return await self._cache_fallback(segment_ids, horizons_min)

        # Build adjacency from graph DB
        adjacency = np.zeros((N_max, N_max), dtype=np.float32)
        if self._graph_db is not None:
            seg_to_idx = {sid: i for i, sid in enumerate(active_ids)}
            for sid in active_ids:
                neighbors = await self._graph_db.multi_hop_neighbors(sid, hops=1)
                idx_src = seg_to_idx[sid]
                for nbr in neighbors:
                    if nbr in seg_to_idx:
                        adjacency[idx_src, seg_to_idx[nbr]] = 1.0

        # Node mask: True for padding nodes
        node_mask = np.ones((1, N_max), dtype=bool)
        node_mask[0, :N] = False

        # Run ONNX inference
        inputs = {
            "node_features": node_features,
            "adjacency": adjacency,
            "node_mask": node_mask,
        }
        outputs = await self._onnx_serve.infer(inputs)
        predictions = outputs.get("predictions")  # (1, N_max, 4, 2)

        if predictions is None:
            return {}

        # Unpack into per-segment results
        result = {}
        for i, sid in enumerate(active_ids):
            result[sid] = {}
            for hi, horizon in enumerate(horizons_min[:4]):
                result[sid][horizon] = {
                    "speed_kmh": float(predictions[0, i, hi, 0]),
                    "congestion_idx": float(np.clip(predictions[0, i, hi, 1], 0, 1)),
                }

        # DSTGAT is a point head: point-only rows, labelled as such on the
        # quantile side channel (the optional ``quantiles`` object stays
        # absent — exactly the two frozen point keys per horizon).
        self.last_forecast_rung = "dstgat"
        self.last_quantile_reason = "quantiles_absent:dstgat_point_head"

        # Cache forecasts (point fields only — see _cache_forecast_rows)
        await self._cache_forecast_rows(result)

        logger.info(
            "flow_forecaster.predict",
            segments=len(active_ids),
            horizons=horizons_min,
            rung="dstgat",
        )
        return result

    def _run_foundation(
        self,
        active_ids: list[int],
        horizons_min: list[int],
    ) -> dict:
        """Foundation rung body: rolling window -> adapter -> forecast rows.

        Raises :class:`FoundationInputError` (mapped by the caller to the
        ``foundation_input:<reason>`` label) whenever the artifact's input
        contract is not met — warm-up window (< 12 recorded steps), an
        unobserved-only batch, or crossing quantiles. History comes from
        the rolling window only; this rung never zero-pads.
        """
        horizons = [h for h in horizons_min if h in HORIZON_TO_STEP][:4]
        if not horizons:
            raise FoundationInputError(
                f"horizons_off_contract: {horizons_min} has no horizon in "
                f"{sorted(HORIZON_TO_STEP)}"
            )
        # PARTIALLY off-contract requests (e.g. [5, 60]) are answered for
        # the on-contract subset — but never silently: the dropped horizons
        # are labelled on the log so no caller mistakes a subset for the
        # full request (review finding). The [:4] cap mirrors the DSTGAT
        # rung's positional horizons_min[:4] semantics.
        dropped = [h for h in horizons_min if h not in HORIZON_TO_STEP]
        if dropped:
            logger.warning(
                "flow_forecaster.foundation_horizons_dropped",
                requested=list(horizons_min),
                dropped=dropped,
                answered=horizons,
            )
        history = self._foundation_window.history(active_ids)
        rows = self._foundation_adapter.forecast(history)
        return {
            sid: {h: row for h, row in per.items() if h in horizons}
            for sid, per in rows.items()
        }

    async def _cache_forecast_rows(self, result: dict) -> None:
        """Write the POINT fields of forecast rows to the shared cache.

        Deliberately lossy: the ``quantiles`` objects do NOT ride the cache
        — the ``traffic:forecast:{sid}:{h}`` JSON format is point-only and
        every current reader consumes the point fields; bands are available
        on the returned payload (HTTP /predict/flow) and via the adapter.
        """
        if self._cache is None:
            return
        for sid, preds in result.items():
            for horizon, vals in preds.items():
                key = f"traffic:forecast:{sid}:{horizon}"
                await self._cache._client.set(
                    key,
                    f'{{"speed_kmh":{vals["speed_kmh"]:.1f},"congestion_idx":{vals["congestion_idx"]:.3f}}}',
                    ex=60,
                )

    async def _cache_fallback(
        self,
        segment_ids: list[int],
        horizons_min: list[int],
    ) -> dict:
        """Fallback prediction using cached live data when no ONNX model is available.

        Projects current traffic conditions forward with slight temporal decay,
        simulating realistic near-term forecast degradation. Point-only rows —
        the producing rung is labelled on the quantile side channel.
        """
        self.last_forecast_rung = "heuristic"
        self.last_quantile_reason = "quantiles_absent:heuristic_rung"
        if self._cache is None:
            return {}

        result = {}
        for sid in segment_ids:
            obs = await self._cache.get_latest(sid)
            if obs is None:
                continue
            base_speed = obs.speed_kmh
            base_cong = obs.congestion_idx
            result[sid] = {}
            for horizon in horizons_min[:4]:
                # Apply small temporal drift: congestion rises ~2% per 5min horizon
                drift = 1.0 + (horizon / 5) * 0.02
                predicted_cong = min(1.0, base_cong * drift)
                # Speed decreases slightly as congestion increases
                speed_factor = 1.0 - (horizon / 5) * 0.015
                predicted_speed = max(5.0, base_speed * speed_factor)
                result[sid][horizon] = {
                    "speed_kmh": round(predicted_speed, 1),
                    "congestion_idx": round(predicted_cong, 3),
                }

        logger.info(
            "flow_forecaster.cache_fallback",
            segments=len(result),
            horizons=horizons_min,
        )
        return result

    @profile_memory
    async def train(self, epochs: int = 50, batch_size: int = 32) -> None:
        """Train the DSTGAT model on historical traffic data.

        VRAM Policy: Only train when LLM is idle. Use gpu_arbiter to acquire GPU lease.
        """
        from traffic_oracle.predict.data_loader import TrafficDataLoader
        from traffic_oracle.predict.training_pipeline import TrainingPipeline
        from traffic_oracle.runtime.onnx_serve import OnnxServe

        data_loader = TrafficDataLoader(
            database_url="postgresql://corridor_admin:dev_changeme@localhost:5432/corridor_db",
            config=self._config,
        )

        pipeline = TrainingPipeline(
            config=self._config,
            data_loader=data_loader,
            model_output_dir="models/onnx",
        )

        result = await pipeline.run(epochs=epochs, batch_size=batch_size)

        # Reload ONNX session with new model
        self._onnx_serve = OnnxServe(result.onnx_path, self._arbiter)
        logger.info(
            "flow_forecaster.training_complete",
            val_loss=result.best_val_loss,
            epochs=result.epochs_trained,
            model_kb=result.model_size_kb,
        )

    @profile_memory
    def export_onnx(self, output_path: str) -> None:
        """Export trained PyTorch model to ONNX for low-VRAM inference."""
        from traffic_oracle.predict.nn.dstgat import DSTGAT
        from traffic_oracle.predict.nn.quantize import ModelQuantizer

        model = DSTGAT(self._config)
        ModelQuantizer.export_to_onnx(
            model, output_path,
            self._config.max_nodes,
            self._config.lookback_steps,
            self._config.in_features,
        )
        logger.info("flow_forecaster.export_onnx", path=output_path)
