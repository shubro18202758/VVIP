"""Neural network subpackage — DSTGAT congestion-aware prediction model.

Lazy by design (PEP 562): importing ``traffic_oracle.predict.nn`` must NOT
pull torch/onnxruntime, so the pure-python gate venv can import anything
that only needs :class:`DSTGATConfig` (flow_forecaster, tests, the CLI).
The heavy modules (``dstgat`` and friends) load on first attribute access
instead. ``DSTGATConfig`` itself is served from the torch-free
``nn/config.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = ["DSTGAT", "DSTGATConfig"]

if TYPE_CHECKING:  # pragma: no cover — static type checkers only
    from traffic_oracle.predict.nn.config import DSTGATConfig
    from traffic_oracle.predict.nn.dstgat import DSTGAT


def __getattr__(name: str):
    if name == "DSTGATConfig":
        # Torch-free dataclass — hand it out without the heavy stack.
        from traffic_oracle.predict.nn.config import DSTGATConfig

        return DSTGATConfig
    if name == "DSTGAT":
        from traffic_oracle.predict.nn.dstgat import DSTGAT

        return DSTGAT
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
