"""Reinforcement learning signal optimization for convoy green-wave control."""

from traffic_oracle.optimize.rl.agent import FactoredDQNAgent
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
from traffic_oracle.optimize.rl.trainer import SignalRLTrainer

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
