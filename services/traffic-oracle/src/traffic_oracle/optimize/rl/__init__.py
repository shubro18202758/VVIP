"""Reinforcement learning signal optimization for convoy green-wave control.

Import-graph note: ``agent`` and ``trainer`` pull torch, so they resolve
lazily (PEP 562 ``__getattr__``) — importing ``optimize.rl.environment`` or
``optimize.rl.bounds`` never requires torch, and the torch-free gate venv
loads this package.  ``FactoredDQNAgent`` / ``SignalRLTrainer`` import
torch on first attribute access exactly as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from traffic_oracle.optimize.rl.bounds import (
    baseline_snapshot,
    bound_advance_closure_sec,
    bound_congestion_spike_threshold,
    bound_gps_poll_interval_sec,
)
from traffic_oracle.optimize.rl.environment import (
    GYMNASIUM_AVAILABLE,
    SignalCorridorEnv,
    TwinCorridorEnv,
)
from traffic_oracle.optimize.rl.reward import compute_signal_reward

if TYPE_CHECKING:  # pragma: no cover — static type checkers only
    from traffic_oracle.optimize.rl.agent import FactoredDQNAgent
    from traffic_oracle.optimize.rl.trainer import SignalRLTrainer

#: torch-backed symbols → defining submodule (resolved on first access).
_LAZY_TORCH_SYMBOLS = {
    "FactoredDQNAgent": "traffic_oracle.optimize.rl.agent",
    "SignalRLTrainer": "traffic_oracle.optimize.rl.trainer",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_TORCH_SYMBOLS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value  # cache: later lookups skip __getattr__
    return value


__all__ = [
    "FactoredDQNAgent",
    "GYMNASIUM_AVAILABLE",
    "SignalCorridorEnv",
    "SignalRLTrainer",
    "TwinCorridorEnv",
    "baseline_snapshot",
    "bound_advance_closure_sec",
    "bound_congestion_spike_threshold",
    "bound_gps_poll_interval_sec",
    "compute_signal_reward",
]
