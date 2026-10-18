"""DSTGAT — Dynamic Spatio-Temporal Graph Attention Network.

Assembles temporal convolution, congestion gating, and dynamic graph attention
into a unified multi-horizon traffic prediction model.

Architecture (83,574 parameters, ~327KB FP32, ~82KB INT8):
    CausalTCNBlock → TemporalAttentionPooling → CongestionGate → GAT×3 → MultiHorizonHead

Input:  (B, N, T=12, 8)  — 12 five-minute steps, 8 features from FeatureBuilder
Output: (B, N, 4, 2)     — (speed, congestion) at T+5, T+10, T+15, T+30 min
"""

from __future__ import annotations

import torch
import torch.nn as nn

from traffic_oracle.predict.nn.congestion_gate import CongestionGate
from traffic_oracle.predict.nn.config import DSTGATConfig
from traffic_oracle.predict.nn.graph_attention import DynamicGATLayer
from traffic_oracle.predict.nn.temporal_conv import (
    CausalTCNBlock,
    TemporalAttentionPooling,
)

__all__ = ["DSTGAT", "DSTGATConfig"]

# DSTGATConfig is defined in the torch-free nn/config.py and re-exported
# here for compatibility with `from ...nn.dstgat import DSTGATConfig`.


class DSTGAT(nn.Module):
    """Dynamic Spatio-Temporal Graph Attention Network.

    Congestion-aware traffic prediction model that distinguishes between
    free-flow and congested regimes via learned gating, then propagates
    information through the road graph with congestion-weighted attention.
    """

    def __init__(self, config: DSTGATConfig | None = None) -> None:
        super().__init__()
        cfg = config or DSTGATConfig()
        self.config = cfg

        # Temporal feature extraction
        self.tcn = CausalTCNBlock(cfg.in_features, cfg.hidden_dim, cfg.tcn_kernel_size)
        self.temporal_pool = TemporalAttentionPooling(cfg.hidden_dim, cfg.temporal_attention_heads)

        # Regime-switching gate
        self.congestion_gate = CongestionGate(cfg.hidden_dim)

        # Spatial graph attention stack
        self.gat_layers = nn.ModuleList(
            [
                DynamicGATLayer(cfg.hidden_dim, cfg.gat_heads, cfg.dropout)
                for _ in range(cfg.gat_layers)
            ]
        )

        # Multi-horizon prediction heads
        self.horizon_heads = nn.ModuleList(
            [nn.Linear(cfg.hidden_dim, cfg.n_outputs) for _ in range(cfg.n_horizons)]
        )

    def forward(
        self,
        node_features: torch.Tensor,
        adjacency: torch.Tensor,
        node_mask: torch.Tensor | None = None,
    ) -> torch.Tensor:
        """Forward pass.

        Args:
            node_features: (B, N, T, F) temporal features per node
            adjacency: (N, N) binary adjacency matrix
            node_mask: (B, N) boolean mask, True = padding node

        Returns:
            (B, N, H, 2) predictions — (speed_kmh, congestion_idx) per horizon
        """
        # Temporal encoding: (B, N, T, 8) → (B, N, T, 64)
        h = self.tcn(node_features)

        # Temporal pooling: (B, N, T, 64) → (B, N, 64)
        temporal_mask = None
        if node_mask is not None:
            temporal_mask = node_mask.unsqueeze(-1).expand_as(
                h[:, :, :, 0]
            )  # (B, N, T)
        h = self.temporal_pool(h, temporal_mask)

        # Congestion gating: (B, N, 64) → (B, N, 64), (B, N, 1)
        h, gate_values = self.congestion_gate(h)

        # Graph attention stack: (B, N, 64) → (B, N, 64)
        for gat in self.gat_layers:
            h = gat(h, adjacency, gate_values)

        # Multi-horizon prediction: (B, N, 64) → (B, N, H, 2)
        preds = torch.stack([head(h) for head in self.horizon_heads], dim=2)

        # Mask padded nodes
        if node_mask is not None:
            preds = preds.masked_fill(node_mask.unsqueeze(-1).unsqueeze(-1), 0.0)

        return preds
