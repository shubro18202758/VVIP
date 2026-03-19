"""Dynamic Graph Attention Layer — congestion-weighted spatial message passing.

Standard GAT computes attention coefficients using only node features.
DynamicGATLayer augments this with a congestion dissimilarity term
|c_i - c_j| so edges connecting segments in different congestion regimes
receive appropriately modulated attention — critical for identifying
shockwave boundaries and regime transitions.
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F


class DynamicGATLayer(nn.Module):
    """Graph attention layer with congestion-aware edge attention.

    Attention: e_ij = LeakyReLU(a^T [W_s·h_i ‖ W_t·h_j ‖ |c_i - c_j|])

    4 heads × 16 dimensions each = 64-dim output per node.
    Includes residual connection, LayerNorm, and dropout.

    Input:  (B, N, D), adj (N, N), congestion (B, N, 1)
    Output: (B, N, D)
    """

    def __init__(
        self,
        hidden_dim: int = 64,
        n_heads: int = 4,
        dropout: float = 0.1,
    ) -> None:
        super().__init__()
        self.n_heads = n_heads
        self.head_dim = hidden_dim // n_heads  # 16

        self.W_src = nn.Linear(hidden_dim, hidden_dim, bias=False)
        self.W_tgt = nn.Linear(hidden_dim, hidden_dim, bias=False)
        # Attention vector per head: 2*head_dim + 1 (congestion feature)
        self.attn = nn.Parameter(
            torch.randn(n_heads, 2 * self.head_dim + 1)
        )

        self.out_proj = nn.Linear(hidden_dim, hidden_dim)
        self.norm = nn.LayerNorm(hidden_dim)
        self.dropout = nn.Dropout(dropout)

    def forward(
        self,
        x: torch.Tensor,
        adj: torch.Tensor,
        congestion: torch.Tensor,
    ) -> torch.Tensor:
        """Forward pass.

        Args:
            x: (B, N, D) node features
            adj: (N, N) adjacency matrix (1 = connected, 0 = not)
            congestion: (B, N, 1) per-node congestion values from CongestionGate

        Returns:
            (B, N, D) graph-attention-updated features
        """
        B, N, D = x.shape
        H, Dh = self.n_heads, self.head_dim

        h_src = self.W_src(x).view(B, N, H, Dh)  # (B, N, H, Dh)
        h_tgt = self.W_tgt(x).view(B, N, H, Dh)

        # Congestion dissimilarity: |c_i - c_j| for all pairs
        c = congestion.squeeze(-1)  # (B, N)
        c_diff = (c.unsqueeze(2) - c.unsqueeze(1)).abs()  # (B, N, N)

        # Attention coefficients: e_ij per head
        # h_src_i (B, N, 1, H, Dh) broadcast with h_tgt_j (B, 1, N, H, Dh)
        h_i = h_src.unsqueeze(2).expand(B, N, N, H, Dh)
        h_j = h_tgt.unsqueeze(1).expand(B, N, N, H, Dh)
        c_feat = c_diff.unsqueeze(-1).unsqueeze(-1).expand(B, N, N, H, 1)

        cat = torch.cat([h_i, h_j, c_feat], dim=-1)  # (B, N, N, H, 2*Dh+1)
        e = (cat * self.attn).sum(dim=-1)  # (B, N, N, H)
        e = F.leaky_relu(e, negative_slope=0.2)

        # Mask non-edges with -inf
        mask = adj.unsqueeze(0).unsqueeze(-1).expand_as(e)  # (B, N, N, H)
        e = e.masked_fill(mask == 0, float("-inf"))

        alpha = F.softmax(e, dim=2)  # (B, N, N, H) — attention over neighbors
        alpha = self.dropout(alpha)

        # Aggregate: weighted sum of neighbor features
        # alpha (B, N, N, H) × h_tgt (B, N, H, Dh) → (B, N, H, Dh)
        h_tgt_reshaped = h_tgt.unsqueeze(1).expand(B, N, N, H, Dh)
        out = (alpha.unsqueeze(-1) * h_tgt_reshaped).sum(dim=2)  # (B, N, H, Dh)
        out = out.reshape(B, N, D)

        out = self.out_proj(out)
        out = self.dropout(out)
        out = self.norm(out + x)  # residual + LayerNorm
        return out
