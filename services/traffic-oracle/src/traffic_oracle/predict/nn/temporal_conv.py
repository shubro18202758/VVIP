"""Temporal convolution modules for sequence feature extraction.

CausalTCNBlock: Dilated causal convolutions capturing temporal patterns at
multiple scales without leaking future information.

TemporalAttentionPooling: Learned pooling that compresses variable-length
temporal sequences into fixed-size node representations.
"""

from __future__ import annotations

import torch
import torch.nn as nn


class CausalTCNBlock(nn.Module):
    """Dilated causal temporal convolution block.

    Three Conv1D layers with exponentially increasing dilation (1, 2, 4)
    producing a receptive field of 12 timesteps — matching the lookback window.
    Causal padding ensures no future information leakage.

    Input:  (B, N, T, in_features=8)
    Output: (B, N, T, hidden_dim=64)
    """

    def __init__(
        self,
        in_features: int = 8,
        hidden_dim: int = 64,
        kernel_size: int = 3,
    ) -> None:
        super().__init__()
        self.layers = nn.ModuleList()
        self.norms = nn.ModuleList()
        channels = [in_features, 32, hidden_dim, hidden_dim]
        dilations = [1, 2, 4]

        for i in range(3):
            padding = (kernel_size - 1) * dilations[i]  # causal: left-pad only
            self.layers.append(
                nn.Conv1d(
                    channels[i],
                    channels[i + 1],
                    kernel_size,
                    dilation=dilations[i],
                    padding=padding,
                )
            )
            self.norms.append(nn.BatchNorm1d(channels[i + 1]))

        self.activation = nn.GELU()
        self.residual = nn.Conv1d(in_features, hidden_dim, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass.

        Args:
            x: (B, N, T, F) node temporal features

        Returns:
            (B, N, T, D) transformed features
        """
        B, N, T, F = x.shape
        x = x.reshape(B * N, T, F).transpose(1, 2)  # (B*N, F, T)

        residual = self.residual(x)  # (B*N, D, T)

        h = x
        for conv, norm in zip(self.layers, self.norms):
            h = conv(h)
            h = h[:, :, :T]  # trim causal padding to preserve T
            h = norm(h)
            h = self.activation(h)

        h = h + residual  # residual connection
        h = h.transpose(1, 2).reshape(B, N, T, -1)  # (B, N, T, D)
        return h


class TemporalAttentionPooling(nn.Module):
    """Learned attention pooling over the temporal dimension.

    Uses a learnable query token with multi-head self-attention to compress
    T timesteps into a single vector per node, weighting recent and salient
    timesteps more heavily.

    Input:  (B, N, T, D)
    Output: (B, N, D)
    """

    def __init__(self, hidden_dim: int = 64, n_heads: int = 2) -> None:
        super().__init__()
        self.query = nn.Parameter(torch.randn(1, 1, 1, hidden_dim))
        self.attn = nn.MultiheadAttention(hidden_dim, n_heads, batch_first=True)
        self.norm = nn.LayerNorm(hidden_dim)

    def forward(self, x: torch.Tensor, mask: torch.Tensor | None = None) -> torch.Tensor:
        """Forward pass.

        Args:
            x: (B, N, T, D) temporal features
            mask: optional (B, N, T) boolean mask (True = padding)

        Returns:
            (B, N, D) pooled node representations
        """
        B, N, T, D = x.shape

        # Expand query to match batch and node dims
        q = self.query.expand(B, N, 1, D).reshape(B * N, 1, D)
        kv = x.reshape(B * N, T, D)

        key_padding_mask = None
        if mask is not None:
            key_padding_mask = mask.reshape(B * N, T)

        pooled, _ = self.attn(q, kv, kv, key_padding_mask=key_padding_mask)
        pooled = self.norm(pooled.squeeze(1))  # (B*N, D)
        return pooled.reshape(B, N, D)
