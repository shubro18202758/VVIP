"""DSTGAT hyperparameters — torch-free by design.

``DSTGATConfig`` lives here (not in ``dstgat.py``) so that config consumers
— the ONNX serving path, tests, the pure-python gate venv — never trigger
``import torch``. ``dstgat.py`` re-exports it for backward compatibility;
``predict/nn/__init__.py`` hands it out lazily without touching torch.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DSTGATConfig:
    """Hyperparameters for the DSTGAT model."""

    in_features: int = 8
    hidden_dim: int = 64
    tcn_kernel_size: int = 3
    gat_layers: int = 3
    gat_heads: int = 4
    temporal_attention_heads: int = 2
    n_horizons: int = 4
    n_outputs: int = 2  # (speed, congestion)
    dropout: float = 0.1
    max_nodes: int = 200
    lookback_steps: int = 12
