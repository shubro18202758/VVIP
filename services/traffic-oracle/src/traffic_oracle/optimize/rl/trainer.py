"""RL training loop orchestrator for signal corridor optimisation.

Manages the episode/step loop, replay-buffer interaction, periodic target
updates, checkpoint saving, and optional ONNX policy export.

Thread budget: 2 threads (1 env step, 1 network update).
Memory budget: ~50 MB peak (replay buffer + 2× tiny nets).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import structlog

from traffic_oracle.optimize.rl.agent import (
    AgentConfig,
    FactoredDQNAgent,
    Transition,
)
from traffic_oracle.optimize.rl.environment import SignalCorridorEnv
from traffic_oracle.runtime.memory_profiler import profile_memory

logger = structlog.get_logger(__name__)


@dataclass
class TrainingMetrics:
    """Summary statistics from a completed training run."""

    episodes_trained: int
    best_episode: int
    best_cumulative_reward: float
    final_epsilon: float
    avg_reward_last_100: float
    avg_steps_last_100: float
    checkpoint_path: str | None = None
    onnx_paths: list[str] | None = None


class SignalRLTrainer:
    """Episodic trainer for the FactoredDQN signal agent.

    Parameters
    ----------
    n_signals : int
        Number of traffic signals in the corridor.
    corridor_length_m : float
        Corridor length in metres.
    convoy_speed_kmh : float
        Default convoy speed.
    agent_config : AgentConfig | None
        Override default agent hyperparameters.
    seed : int | None
        RNG seed for reproducibility.
    checkpoint_dir : str
        Directory for model checkpoints.
    """

    def __init__(
        self,
        n_signals: int = 10,
        corridor_length_m: float = 5000.0,
        convoy_speed_kmh: float = 60.0,
        agent_config: AgentConfig | None = None,
        seed: int | None = None,
        checkpoint_dir: str = "models/rl",
    ) -> None:
        self._n_signals = n_signals
        self._corridor_length_m = corridor_length_m
        self._convoy_speed_kmh = convoy_speed_kmh
        self._seed = seed
        self._checkpoint_dir = Path(checkpoint_dir)
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)

        # Build config with correct signal count
        cfg = agent_config or AgentConfig()
        cfg.n_signals = n_signals
        self._agent = FactoredDQNAgent(config=cfg, seed=seed)

        self._env = SignalCorridorEnv(
            n_signals=n_signals,
            corridor_length_m=corridor_length_m,
            convoy_speed_kmh=convoy_speed_kmh,
            seed=seed,
        )
        logger.info(
            "rl_trainer.init",
            n_signals=n_signals,
            corridor_length_m=corridor_length_m,
            convoy_speed_kmh=convoy_speed_kmh,
        )

    @profile_memory
    async def train(self, max_episodes: int = 1000) -> TrainingMetrics:
        """Run the full training loop.

        Args:
            max_episodes: Maximum training episodes.

        Returns:
            TrainingMetrics with best-episode stats and checkpoint paths.
        """
        best_reward = -float("inf")
        best_episode = 0
        reward_history: list[float] = []
        steps_history: list[float] = []

        for ep in range(1, max_episodes + 1):
            ep_reward, ep_steps = self._run_episode()
            reward_history.append(ep_reward)
            steps_history.append(ep_steps)

            self._agent.on_episode_end()

            # Checkpoint best policy
            if ep_reward > best_reward:
                best_reward = ep_reward
                best_episode = ep
                self._agent.save(str(self._checkpoint_dir / "best_policy.pt"))

            # Periodic logging
            if ep % 50 == 0:
                recent = reward_history[-100:]
                recent_steps = steps_history[-100:]
                logger.info(
                    "rl_trainer.progress",
                    episode=ep,
                    epsilon=round(self._agent.epsilon, 3),
                    avg_reward_100=round(float(np.mean(recent)), 2),
                    avg_steps_100=round(float(np.mean(recent_steps)), 1),
                    best_reward=round(best_reward, 2),
                    best_episode=best_episode,
                    replay_size=len(self._agent._replay),
                )

        # Save final checkpoint
        final_path = str(self._checkpoint_dir / "final_policy.pt")
        self._agent.save(final_path)

        # Export to ONNX
        onnx_dir = str(self._checkpoint_dir / "onnx")
        onnx_paths = self._agent.export_onnx(onnx_dir)

        recent_rewards = reward_history[-100:] if reward_history else [0.0]
        recent_steps = steps_history[-100:] if steps_history else [0.0]

        metrics = TrainingMetrics(
            episodes_trained=max_episodes,
            best_episode=best_episode,
            best_cumulative_reward=best_reward,
            final_epsilon=self._agent.epsilon,
            avg_reward_last_100=float(np.mean(recent_rewards)),
            avg_steps_last_100=float(np.mean(recent_steps)),
            checkpoint_path=final_path,
            onnx_paths=onnx_paths,
        )

        logger.info(
            "rl_trainer.complete",
            episodes=max_episodes,
            best_reward=round(best_reward, 2),
            best_episode=best_episode,
            avg_reward_100=round(metrics.avg_reward_last_100, 2),
        )
        return metrics

    def _run_episode(self) -> tuple[float, int]:
        """Execute a single training episode.

        Returns:
            (cumulative_reward, steps)
        """
        obs, _ = self._env.reset()
        cumulative_reward = 0.0
        steps = 0

        while True:
            action = self._agent.select_action(obs)
            next_obs, reward, terminated, truncated, _info = self._env.step(action)

            self._agent.store_transition(Transition(
                obs=obs.copy(),
                action=action.copy(),
                reward=reward,
                next_obs=next_obs.copy(),
                done=terminated or truncated,
            ))

            # Learn from experience
            self._agent.train_step()

            cumulative_reward += reward
            steps += 1
            obs = next_obs

            if terminated or truncated:
                break

        return cumulative_reward, steps

    def evaluate(self, n_episodes: int = 10) -> dict:
        """Evaluate current policy without exploration.

        Returns:
            Dict with mean/std reward and steps.
        """
        rewards = []
        steps_list = []

        for _ in range(n_episodes):
            obs, _ = self._env.reset()
            ep_reward = 0.0
            steps = 0

            while True:
                action = self._agent.select_action(obs, greedy=True)
                obs, reward, terminated, truncated, _ = self._env.step(action)
                ep_reward += reward
                steps += 1
                if terminated or truncated:
                    break

            rewards.append(ep_reward)
            steps_list.append(steps)

        return {
            "mean_reward": float(np.mean(rewards)),
            "std_reward": float(np.std(rewards)),
            "mean_steps": float(np.mean(steps_list)),
            "std_steps": float(np.std(steps_list)),
            "n_episodes": n_episodes,
        }
