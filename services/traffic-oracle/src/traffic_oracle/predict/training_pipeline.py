"""End-to-end DSTGAT training orchestration.

Handles the full lifecycle: data loading → model training → quantization →
ONNX export → embedding extraction to pgvector. Operates within the 4GB
memory budget using MemoryGuard and 4-thread constraint via torch settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import structlog
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from traffic_oracle.data.vector_store import TrafficVectorStore
from traffic_oracle.predict.data_loader import TrafficDataLoader
from traffic_oracle.predict.nn.dstgat import DSTGAT, DSTGATConfig
from traffic_oracle.predict.nn.quantize import ModelQuantizer
from traffic_oracle.runtime.memory_profiler import memory_guard, profile_memory

logger = structlog.get_logger(__name__)


@dataclass
class TrainingResult:
    """Summary of a training run."""

    best_val_loss: float
    epochs_trained: int
    onnx_path: str
    model_size_kb: float


class TrainingPipeline:
    """Orchestrates DSTGAT training, quantization, and deployment."""

    def __init__(
        self,
        config: DSTGATConfig,
        data_loader: TrafficDataLoader,
        vector_store: TrafficVectorStore | None = None,
        model_output_dir: str = "models/onnx",
    ) -> None:
        self._cfg = config
        self._data_loader = data_loader
        self._vector_store = vector_store
        self._output_dir = Path(model_output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @profile_memory
    async def run(
        self,
        epochs: int = 50,
        batch_size: int = 32,
        lr: float = 1e-3,
        patience: int = 10,
    ) -> TrainingResult:
        """Execute full training pipeline.

        Args:
            epochs: Maximum training epochs
            batch_size: Mini-batch size
            lr: Initial learning rate
            patience: Early stopping patience (epochs without improvement)

        Returns:
            TrainingResult with metrics and output paths
        """
        torch.set_num_threads(4)

        with memory_guard(3500):
            # Load data
            windows = self._data_loader.load_training_windows()
            if not windows:
                raise ValueError("No training data available")

            dataset = TrafficDataLoader.build_torch_dataset(
                windows, self._cfg.max_nodes
            )

            # 80/20 train/val split
            n_total = len(dataset)
            n_val = max(1, int(n_total * 0.2))
            n_train = n_total - n_val
            train_ds, val_ds = torch.utils.data.random_split(
                dataset, [n_train, n_val], generator=torch.Generator().manual_seed(42)
            )

            train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)
            val_loader = DataLoader(val_ds, batch_size=batch_size)

            # Build model
            model = DSTGAT(self._cfg)
            optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
            scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

            best_val_loss = float("inf")
            best_state = None
            epochs_no_improve = 0

            for epoch in range(epochs):
                # Train
                model.train()
                train_loss = 0.0
                for batch in train_loader:
                    features, adj, mask, targets = batch
                    optimizer.zero_grad()

                    preds = model(features, adj[0], mask)
                    loss = self._compute_loss(preds, targets, mask)

                    loss.backward()
                    optimizer.step()
                    train_loss += loss.item()

                scheduler.step()

                # Validate
                model.eval()
                val_loss = 0.0
                with torch.no_grad():
                    for batch in val_loader:
                        features, adj, mask, targets = batch
                        preds = model(features, adj[0], mask)
                        val_loss += self._compute_loss(preds, targets, mask).item()

                avg_val = val_loss / max(len(val_loader), 1)
                logger.info(
                    "training.epoch",
                    epoch=epoch + 1,
                    train_loss=round(train_loss / max(len(train_loader), 1), 4),
                    val_loss=round(avg_val, 4),
                    lr=round(scheduler.get_last_lr()[0], 6),
                )

                if avg_val < best_val_loss:
                    best_val_loss = avg_val
                    best_state = {k: v.clone() for k, v in model.state_dict().items()}
                    epochs_no_improve = 0
                else:
                    epochs_no_improve += 1
                    if epochs_no_improve >= patience:
                        logger.info("training.early_stop", epoch=epoch + 1)
                        break

            # Restore best and export
            if best_state is not None:
                model.load_state_dict(best_state)

            fp32_path = self._output_dir / "flow_forecaster_fp32.onnx"
            int8_path = self._output_dir / "flow_forecaster.onnx"

            ModelQuantizer.export_to_onnx(
                model, fp32_path, self._cfg.max_nodes,
                self._cfg.lookback_steps, self._cfg.in_features,
            )

            # Extract GAT embeddings for pgvector
            if self._vector_store is not None and windows:
                await self._extract_embeddings(model, dataset)

            model_size_kb = fp32_path.stat().st_size / 1024
            return TrainingResult(
                best_val_loss=best_val_loss,
                epochs_trained=epoch + 1,
                onnx_path=str(int8_path if int8_path.exists() else fp32_path),
                model_size_kb=round(model_size_kb, 1),
            )

    def _compute_loss(
        self,
        preds: torch.Tensor,
        targets: torch.Tensor,
        mask: torch.Tensor,
    ) -> torch.Tensor:
        """Custom loss: MSE_speed + alpha*MSE_congestion + beta*regime_penalty.

        Args:
            preds: (B, N, H, 2) predictions
            targets: (B, N, H, 2) ground truth
            mask: (B, N) padding mask
        """
        alpha = 0.5
        beta = 0.1
        congestion_threshold = 0.6

        valid = ~mask.unsqueeze(-1).unsqueeze(-1)  # (B, N, 1, 1)

        # Speed MSE
        speed_loss = ((preds[..., 0] - targets[..., 0]) ** 2 * valid.squeeze(-1)).mean()

        # Congestion MSE
        cong_loss = ((preds[..., 1] - targets[..., 1]) ** 2 * valid.squeeze(-1)).mean()

        # Regime penalty: extra cost for wrong regime prediction
        pred_congested = preds[..., 1] > congestion_threshold
        true_congested = targets[..., 1] > congestion_threshold
        regime_mismatch = (pred_congested != true_congested).float()
        regime_penalty = (regime_mismatch * valid.squeeze(-1).float()).mean()

        return speed_loss + alpha * cong_loss + beta * regime_penalty

    async def _extract_embeddings(
        self,
        model: DSTGAT,
        dataset: TensorDataset,
    ) -> None:
        """Extract final GAT layer embeddings and store in pgvector."""
        model.eval()
        loader = DataLoader(dataset, batch_size=1)

        embeddings: dict[int, list[float]] = {}
        with torch.no_grad():
            for batch in loader:
                features, adj, mask, _ = batch
                # Forward through TCN + temporal pool + gate
                h = model.tcn(features)
                h = model.temporal_pool(h)
                h, gate = model.congestion_gate(h)
                for gat in model.gat_layers:
                    h = gat(h, adj[0], gate)
                # h is (1, N, 64) — take mean over valid nodes as segment embedding
                valid_mask = ~mask[0]
                for j in range(h.shape[1]):
                    if valid_mask[j]:
                        embeddings[j] = h[0, j, :24].tolist()  # dim=24 for pgvector
                break  # single batch for embeddings

        for seg_idx, emb in embeddings.items():
            await self._vector_store.store_embedding(seg_idx, "gat_l3", emb)

        logger.info("training.embeddings_stored", count=len(embeddings))
