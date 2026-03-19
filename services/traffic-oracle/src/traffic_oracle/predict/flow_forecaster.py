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
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import structlog

from traffic_oracle.data.cache import TrafficCache
from traffic_oracle.data.graph_queries import CorridorGraphDB
from traffic_oracle.predict.nn.dstgat import DSTGATConfig
from traffic_oracle.runtime.memory_profiler import profile_memory
from traffic_oracle.runtime.onnx_serve import OnnxServe

logger = structlog.get_logger(__name__)

HORIZONS = [5, 10, 15, 30]  # minutes


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
    ) -> None:
        self._onnx_path = onnx_model_path
        self._cache = cache
        self._graph_db = graph_db
        self._arbiter = arbiter
        self._onnx_serve: OnnxServe | None = None
        self._config = DSTGATConfig()

        if onnx_model_path and Path(onnx_model_path).exists():
            self._onnx_serve = OnnxServe(onnx_model_path, arbiter)

        logger.info("flow_forecaster.init", onnx_path=onnx_model_path)

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

        if self._onnx_serve is None:
            logger.warning("flow_forecaster.no_model, using cache fallback")
            return await self._cache_fallback(segment_ids, horizons_min)

        N_max = self._config.max_nodes
        T = self._config.lookback_steps
        N = min(len(segment_ids), N_max)
        active_ids = segment_ids[:N]

        # Build node features from cache
        node_features = np.zeros((1, N_max, T, 8), dtype=np.float32)
        if self._cache is not None:
            for i, sid in enumerate(active_ids):
                obs = await self._cache.get_latest(sid)
                if obs is not None:
                    speed_norm = obs.speed_kmh / 60.0
                    # Fill last timestep (most recent)
                    node_features[0, i, T - 1, 0] = speed_norm
                    node_features[0, i, T - 1, 1] = obs.congestion_idx
                    node_features[0, i, T - 1, 7] = 1.0  # neighbor ratio default

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

        # Cache forecasts
        if self._cache is not None:
            for sid, preds in result.items():
                for horizon, vals in preds.items():
                    key = f"traffic:forecast:{sid}:{horizon}"
                    await self._cache._client.set(
                        key,
                        f'{{"speed_kmh":{vals["speed_kmh"]:.1f},"congestion_idx":{vals["congestion_idx"]:.3f}}}',
                        ex=60,
                    )

        logger.info(
            "flow_forecaster.predict",
            segments=len(active_ids),
            horizons=horizons_min,
        )
        return result

    async def _cache_fallback(
        self,
        segment_ids: list[int],
        horizons_min: list[int],
    ) -> dict:
        """Fallback prediction using cached live data when no ONNX model is available.

        Projects current traffic conditions forward with slight temporal decay,
        simulating realistic near-term forecast degradation.
        """
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
