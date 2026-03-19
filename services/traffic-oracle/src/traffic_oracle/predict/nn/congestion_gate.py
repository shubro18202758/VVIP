"""Congestion Gate — soft regime-switching between free-flow and congested dynamics.

Traffic exhibits fundamentally different dynamics at low vs high congestion.
Free-flow traffic follows predictable speed-density relationships while
congested regimes involve shockwaves and capacity drops that require different
modeling. The CongestionGate learns a per-node gating function that blends
two separate linear transforms.
"""

from __future__ import annotations

import torch
import torch.nn as nn


class CongestionGate(nn.Module):
    """Soft gating layer that routes node features through regime-specific transforms.

    For each node, learns a scalar gate g ∈ [0,1]:
        h_out = σ(g) * W_congested @ h + (1 - σ(g)) * W_freeflow @ h

    The gate classifier learns to detect congested conditions from the hidden
    representation, allowing downstream graph attention to operate on
    regime-appropriate features.

    Input:  (B, N, D)
    Output: (gated_output (B, N, D), gate_values (B, N, 1))
    """

    def __init__(self, hidden_dim: int = 64) -> None:
        super().__init__()
        self.gate_classifier = nn.Linear(hidden_dim, 1)
        self.W_freeflow = nn.Linear(hidden_dim, hidden_dim, bias=False)
        self.W_congested = nn.Linear(hidden_dim, hidden_dim, bias=False)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """Forward pass.

        Args:
            x: (B, N, D) node features

        Returns:
            Tuple of (gated output (B, N, D), gate values (B, N, 1))
        """
        g = torch.sigmoid(self.gate_classifier(x))  # (B, N, 1)
        h_free = self.W_freeflow(x)
        h_cong = self.W_congested(x)
        h_out = g * h_cong + (1.0 - g) * h_free
        return h_out, g
