"""Factored DQN agent for per-signal corridor control.

Uses independent Q-networks per signal to avoid the 3^K action-space
explosion.  Each signal has a tiny Linear(6→32→3) network (~291 params).
At K=20 the total footprint is ~5,820 parameters (~23 KB).

Exploration: ε-greedy with linear decay 1.0 → 0.05 over 500 episodes.
Replay buffer: ring buffer of 10,000 transitions (~5 MB).
Target network: hard copy every 100 episodes.
"""

from __future__ import annotations

import copy
import random
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import structlog
import torch
import torch.nn as nn
import torch.optim as optim

from traffic_oracle.runtime.memory_profiler import profile_memory

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Per-signal Q-network
# ---------------------------------------------------------------------------

class SignalQNet(nn.Module):
    """Tiny Q-network for a single traffic signal.

    Input:  6-dim observation (phase, time_in_phase, queue, approach_rate,
            convoy_distance, convoy_eta — all normalised [0, 1]).
    Output: Q-values for 3 actions (hold / extend / skip).
    """

    def __init__(self, obs_dim: int = 6, hidden: int = 32, n_actions: int = 3) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(obs_dim, hidden),
            nn.ReLU(),
            nn.Linear(hidden, n_actions),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # noqa: D401
        return self.net(x)


# ---------------------------------------------------------------------------
# Replay buffer
# ---------------------------------------------------------------------------

@dataclass
class Transition:
    """Single environment transition for experience replay."""

    obs: np.ndarray       # (K, 6)
    action: np.ndarray    # (K,)
    reward: float
    next_obs: np.ndarray  # (K, 6)
    done: bool


class ReplayBuffer:
    """Fixed-size ring buffer storing Transition tuples."""

    def __init__(self, capacity: int = 10_000) -> None:
        self._buf: deque[Transition] = deque(maxlen=capacity)

    def push(self, t: Transition) -> None:
        self._buf.append(t)

    def sample(self, batch_size: int) -> list[Transition]:
        return random.sample(list(self._buf), min(batch_size, len(self._buf)))

    def __len__(self) -> int:
        return len(self._buf)


# ---------------------------------------------------------------------------
# Factored DQN Agent
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Hyperparameters for FactoredDQNAgent."""

    n_signals: int = 10
    obs_dim: int = 6
    hidden: int = 32
    n_actions: int = 3
    lr: float = 1e-3
    gamma: float = 0.99
    eps_start: float = 1.0
    eps_end: float = 0.05
    eps_decay_episodes: int = 500
    replay_capacity: int = 10_000
    batch_size: int = 64
    target_update_episodes: int = 100


class FactoredDQNAgent:
    """Factored DQN with one independent Q-net per traffic signal.

    Parameters
    ----------
    config : AgentConfig
        Hyperparameter container.
    seed : int | None
        RNG seed for reproducibility.
    """

    def __init__(
        self,
        config: AgentConfig | None = None,
        seed: int | None = None,
    ) -> None:
        self.cfg = config or AgentConfig()
        self._rng = np.random.default_rng(seed)
        if seed is not None:
            torch.manual_seed(seed)
            random.seed(seed)

        # Per-signal online + target networks
        self._online: list[SignalQNet] = [
            SignalQNet(self.cfg.obs_dim, self.cfg.hidden, self.cfg.n_actions)
            for _ in range(self.cfg.n_signals)
        ]
        self._target: list[SignalQNet] = [
            copy.deepcopy(net) for net in self._online
        ]
        for net in self._target:
            net.eval()

        # Per-signal optimizers
        self._optimizers: list[optim.Adam] = [
            optim.Adam(net.parameters(), lr=self.cfg.lr)
            for net in self._online
        ]

        self._replay = ReplayBuffer(self.cfg.replay_capacity)
        self._episode = 0

        logger.info(
            "factored_dqn.init",
            n_signals=self.cfg.n_signals,
            params_per_signal=sum(p.numel() for p in self._online[0].parameters()),
            total_params=sum(
                p.numel() for net in self._online for p in net.parameters()
            ),
        )

    # ------ exploration ------

    @property
    def epsilon(self) -> float:
        """Current exploration rate (linearly decayed)."""
        frac = min(1.0, self._episode / max(1, self.cfg.eps_decay_episodes))
        return self.cfg.eps_start + frac * (self.cfg.eps_end - self.cfg.eps_start)

    # ------ action selection ------

    @profile_memory
    def select_action(self, obs: np.ndarray, *, greedy: bool = False) -> np.ndarray:
        """Select actions for all K signals.

        Args:
            obs: (K, 6) observation from the environment.
            greedy: If True, always pick argmax (no exploration).

        Returns:
            (K,) int array of actions in {0, 1, 2}.
        """
        actions = np.zeros(self.cfg.n_signals, dtype=np.int64)
        eps = 0.0 if greedy else self.epsilon

        for i, net in enumerate(self._online):
            if self._rng.random() < eps:
                actions[i] = self._rng.integers(0, self.cfg.n_actions)
            else:
                with torch.no_grad():
                    q = net(torch.as_tensor(obs[i], dtype=torch.float32))
                    actions[i] = int(q.argmax().item())
        return actions

    # ------ experience ------

    def store_transition(self, t: Transition) -> None:
        """Add transition to replay buffer."""
        self._replay.push(t)

    # ------ training step ------

    @profile_memory
    def train_step(self) -> float:
        """Sample a mini-batch and update all per-signal networks.

        Returns:
            Mean TD loss across signals.
        """
        if len(self._replay) < self.cfg.batch_size:
            return 0.0

        batch = self._replay.sample(self.cfg.batch_size)
        B = len(batch)

        # Stack batch into tensors  (B, K, 6), (B, K), (B,), (B, K, 6), (B,)
        obs_t = torch.as_tensor(
            np.stack([t.obs for t in batch]), dtype=torch.float32,
        )
        act_t = torch.as_tensor(
            np.stack([t.action for t in batch]), dtype=torch.int64,
        )
        rew_t = torch.as_tensor(
            np.array([t.reward for t in batch]), dtype=torch.float32,
        )
        next_obs_t = torch.as_tensor(
            np.stack([t.next_obs for t in batch]), dtype=torch.float32,
        )
        done_t = torch.as_tensor(
            np.array([t.done for t in batch]), dtype=torch.float32,
        )

        total_loss = 0.0

        for i in range(self.cfg.n_signals):
            # Online Q(s, a) for signal i
            q_values = self._online[i](obs_t[:, i, :])          # (B, 3)
            q_taken = q_values.gather(1, act_t[:, i].unsqueeze(1)).squeeze(1)  # (B,)

            # Target max Q(s', a') for signal i
            with torch.no_grad():
                q_next = self._target[i](next_obs_t[:, i, :])   # (B, 3)
                q_next_max = q_next.max(dim=1).values            # (B,)

            # TD target: shared reward, per-signal discount
            target = rew_t + self.cfg.gamma * q_next_max * (1.0 - done_t)

            loss = nn.functional.mse_loss(q_taken, target)
            self._optimizers[i].zero_grad()
            loss.backward()
            self._optimizers[i].step()

            total_loss += loss.item()

        return total_loss / self.cfg.n_signals

    # ------ target update ------

    def update_target(self) -> None:
        """Hard copy online weights → target networks."""
        for online, target in zip(self._online, self._target):
            target.load_state_dict(online.state_dict())
        logger.debug("factored_dqn.target_update", episode=self._episode)

    def on_episode_end(self) -> None:
        """Call at the end of each training episode."""
        self._episode += 1
        if self._episode % self.cfg.target_update_episodes == 0:
            self.update_target()

    # ------ persistence ------

    def save(self, path: str) -> None:
        """Save all per-signal online networks to a single checkpoint."""
        state = {
            f"signal_{i}": net.state_dict()
            for i, net in enumerate(self._online)
        }
        state["config"] = {
            "n_signals": self.cfg.n_signals,
            "obs_dim": self.cfg.obs_dim,
            "hidden": self.cfg.hidden,
            "n_actions": self.cfg.n_actions,
            "episode": self._episode,
        }
        torch.save(state, path)
        logger.info("factored_dqn.save", path=path, episode=self._episode)

    def load(self, path: str) -> None:
        """Restore per-signal networks and episode counter from checkpoint."""
        state = torch.load(path, weights_only=True)
        meta = state.get("config", {})
        self._episode = meta.get("episode", 0)
        for i, net in enumerate(self._online):
            key = f"signal_{i}"
            if key in state:
                net.load_state_dict(state[key])
        self.update_target()
        logger.info("factored_dqn.load", path=path, episode=self._episode)

    # ------ ONNX export ------

    def export_onnx(self, output_dir: str) -> list[str]:
        """Export each per-signal Q-network to a separate ONNX file.

        Returns:
            List of output ONNX paths.
        """
        from pathlib import Path

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []

        for i, net in enumerate(self._online):
            net.eval()
            onnx_path = str(out / f"signal_q_{i}.onnx")
            dummy = torch.randn(1, self.cfg.obs_dim)
            torch.onnx.export(
                net,
                dummy,
                onnx_path,
                input_names=["observation"],
                output_names=["q_values"],
                opset_version=18,
                dynamic_axes={"observation": {0: "batch"}, "q_values": {0: "batch"}},
            )
            paths.append(onnx_path)

        logger.info("factored_dqn.export_onnx", count=len(paths), dir=output_dir)
        return paths
