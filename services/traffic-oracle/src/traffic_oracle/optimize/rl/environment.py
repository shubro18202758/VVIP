"""Gymnasium-compatible corridor signal environment for RL training.

Simulates K ≤ 20 traffic signals along a convoy corridor.  The agent
controls signal phases to create a green wave for the convoy while
minimizing disruption to public traffic.

Step resolution: 5 simulated seconds.
Max episode length: 600 steps (50 minutes simulated).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import gymnasium as gym
import numpy as np
import structlog
from gymnasium import spaces

from traffic_oracle.optimize.rl.reward import compute_signal_reward

logger = structlog.get_logger(__name__)

# Physical constants used in the simplified queuing model
ARRIVAL_RATE_VPS = 0.3  # vehicles per second arriving at each signal
QUEUE_DISCHARGE_RATE_VPS = 0.5  # vehicles per second leaving on green
AVG_VEHICLE_LENGTH_M = 8.0  # average queue spacing per vehicle
STEP_DURATION_SEC = 5  # simulated seconds per env step
MAX_STEPS = 600  # 50 minutes of simulated time
MAX_QUEUE_VEHICLES = 100  # cap per signal to prevent numerical blowup


@dataclass
class SignalState:
    """Internal state for a single traffic signal."""

    phase: int = 0  # 0=green for convoy direction, 1=red
    time_in_phase_sec: float = 0.0
    queue_vehicles: float = 0.0
    green_duration_sec: float = 30.0  # default green phase length
    red_duration_sec: float = 30.0
    position_m: float = 0.0  # distance from corridor start


@dataclass
class ConvoyState:
    """Tracks the convoy's progress along the corridor."""

    position_m: float = 0.0  # current position along corridor
    speed_mps: float = 16.67  # ~60 km/h default
    stopped: bool = False
    delay_this_step_sec: float = 0.0


class SignalCorridorEnv(gym.Env):
    """Simulated corridor of K traffic signals with convoy traversal.

    Observation per signal (K, 6):
        [0] current_phase         — 0=green for convoy, 1=red
        [1] time_in_phase         — seconds in current phase (normalized /60)
        [2] queue_length          — queue in meters (normalized /500)
        [3] approaching_vehicles  — arrival rate proxy (normalized)
        [4] convoy_distance       — meters to convoy (normalized /5000, 0 if passed)
        [5] seconds_until_convoy  — ETA to this signal (normalized /300)

    Action per signal: MultiDiscrete([3]*K)
        0 = hold current phase
        1 = extend green by 5 seconds
        2 = skip to next phase (toggle green↔red)
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        n_signals: int = 10,
        corridor_length_m: float = 5000.0,
        convoy_speed_kmh: float = 60.0,
        seed: int | None = None,
    ) -> None:
        super().__init__()

        self.n_signals = min(n_signals, 20)
        self.corridor_length_m = corridor_length_m
        self.convoy_speed_mps = convoy_speed_kmh * 1000.0 / 3600.0

        self.observation_space = spaces.Box(
            low=0.0, high=1.0,
            shape=(self.n_signals, 6),
            dtype=np.float32,
        )
        self.action_space = spaces.MultiDiscrete([3] * self.n_signals)

        self._signals: list[SignalState] = []
        self._convoy = ConvoyState()
        self._step_count = 0
        self._total_public_delay = 0.0
        self._rng = np.random.default_rng(seed)

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict | None = None,
    ) -> tuple[np.ndarray, dict]:
        """Reset episode: re-initialize signals and place convoy at start."""
        super().reset(seed=seed)
        if seed is not None:
            self._rng = np.random.default_rng(seed)

        # Place signals evenly along corridor with small jitter
        spacing = self.corridor_length_m / (self.n_signals + 1)
        self._signals = []
        for i in range(self.n_signals):
            jitter = self._rng.uniform(-spacing * 0.1, spacing * 0.1)
            pos = spacing * (i + 1) + jitter
            initial_phase = self._rng.integers(0, 2)
            self._signals.append(SignalState(
                phase=int(initial_phase),
                time_in_phase_sec=self._rng.uniform(0, 20),
                queue_vehicles=self._rng.uniform(0, 10),
                green_duration_sec=25.0 + self._rng.uniform(0, 15),
                red_duration_sec=25.0 + self._rng.uniform(0, 15),
                position_m=float(np.clip(pos, 0, self.corridor_length_m)),
            ))
        self._signals.sort(key=lambda s: s.position_m)

        self._convoy = ConvoyState(
            position_m=0.0,
            speed_mps=self.convoy_speed_mps,
            stopped=False,
            delay_this_step_sec=0.0,
        )
        self._step_count = 0
        self._total_public_delay = 0.0

        return self._get_obs(), self._get_info()

    def step(
        self, action: np.ndarray,
    ) -> tuple[np.ndarray, float, bool, bool, dict]:
        """Advance simulation by one step (5 simulated seconds).

        Args:
            action: (K,) array with values in {0, 1, 2} per signal.

        Returns:
            (obs, reward, terminated, truncated, info)
        """
        self._step_count += 1

        # 1. Apply actions to signal phases
        self._apply_actions(action)

        # 2. Advance signal timers and natural phase cycling
        self._tick_signals()

        # 3. Simulate public traffic queuing
        public_delay = self._simulate_public_traffic()

        # 4. Move convoy
        convoy_delay, convoy_stopped, red_violations = self._advance_convoy()

        # 5. Compute green-wave fraction
        green_wave = self._compute_green_wave()

        # 6. Max queue length
        max_queue_m = max(
            s.queue_vehicles * AVG_VEHICLE_LENGTH_M for s in self._signals
        )

        # 7. Reward
        reward = compute_signal_reward(
            convoy_delay_sec=convoy_delay,
            public_delay_vehicle_sec=public_delay,
            max_queue_length_m=max_queue_m,
            green_wave_continuity=green_wave,
            red_light_violations=red_violations,
            convoy_stopped=convoy_stopped,
        )

        # Termination: convoy passed all signals
        terminated = self._convoy.position_m >= self.corridor_length_m
        truncated = self._step_count >= MAX_STEPS

        return self._get_obs(), float(reward), terminated, truncated, self._get_info()

    def _apply_actions(self, action: np.ndarray) -> None:
        """Apply agent actions to signal phases."""
        for i, sig in enumerate(self._signals):
            act = int(action[i])
            if act == 1:
                # Extend green: add 5 seconds to current green duration
                if sig.phase == 0:
                    sig.green_duration_sec += 5.0
            elif act == 2:
                # Skip to next phase
                sig.phase = 1 - sig.phase
                sig.time_in_phase_sec = 0.0

    def _tick_signals(self) -> None:
        """Advance signal timers by STEP_DURATION_SEC and cycle phases."""
        for sig in self._signals:
            sig.time_in_phase_sec += STEP_DURATION_SEC
            cycle_len = (
                sig.green_duration_sec if sig.phase == 0
                else sig.red_duration_sec
            )
            if sig.time_in_phase_sec >= cycle_len:
                sig.phase = 1 - sig.phase
                sig.time_in_phase_sec = 0.0

    def _simulate_public_traffic(self) -> float:
        """Simplified queuing model for public traffic at each signal.

        Returns total vehicle-seconds of delay this step.
        """
        total_delay = 0.0
        for sig in self._signals:
            if sig.phase == 1:  # red for convoy direction = green for cross
                # Queue on convoy-direction approach grows
                arrivals = ARRIVAL_RATE_VPS * STEP_DURATION_SEC
                sig.queue_vehicles = min(
                    sig.queue_vehicles + arrivals, MAX_QUEUE_VEHICLES
                )
            else:
                # Green: discharge queue
                discharged = min(
                    QUEUE_DISCHARGE_RATE_VPS * STEP_DURATION_SEC,
                    sig.queue_vehicles,
                )
                sig.queue_vehicles = max(0, sig.queue_vehicles - discharged)

            # Delay = queue_vehicles * step_duration (vehicle-seconds)
            total_delay += sig.queue_vehicles * STEP_DURATION_SEC

        self._total_public_delay += total_delay
        return total_delay

    def _advance_convoy(self) -> tuple[float, bool, int]:
        """Move convoy forward, checking signal phases.

        Returns:
            (delay_sec, stopped, red_violations)
        """
        delay = 0.0
        stopped = False
        red_violations = 0

        # Check each signal the convoy may reach this step
        travel_m = self._convoy.speed_mps * STEP_DURATION_SEC

        for sig in self._signals:
            # Skip signals already passed
            if sig.position_m <= self._convoy.position_m:
                continue
            # Skip signals not yet reached this step
            if sig.position_m > self._convoy.position_m + travel_m:
                break

            # Convoy reaches this signal
            if sig.phase == 1:  # red for convoy
                red_violations += 1
                # Convoy stops: loses remaining travel
                remaining = sig.position_m - self._convoy.position_m
                time_to_signal = remaining / max(self._convoy.speed_mps, 0.1)
                delay = STEP_DURATION_SEC - time_to_signal
                stopped = True
                self._convoy.position_m = sig.position_m - 1.0  # wait before signal
                self._convoy.stopped = True
                self._convoy.delay_this_step_sec = delay
                return delay, stopped, red_violations

        # No red encountered — full travel
        self._convoy.position_m += travel_m
        self._convoy.stopped = False
        self._convoy.delay_this_step_sec = 0.0
        return delay, stopped, red_violations

    def _compute_green_wave(self) -> float:
        """Fraction of upcoming signals currently green for the convoy."""
        upcoming = [
            s for s in self._signals
            if s.position_m > self._convoy.position_m
        ]
        if not upcoming:
            return 1.0
        greens = sum(1 for s in upcoming if s.phase == 0)
        return greens / len(upcoming)

    def _get_obs(self) -> np.ndarray:
        """Build (K, 6) normalized observation array."""
        obs = np.zeros((self.n_signals, 6), dtype=np.float32)
        for i, sig in enumerate(self._signals):
            distance = max(0.0, sig.position_m - self._convoy.position_m)
            eta = distance / max(self._convoy.speed_mps, 0.1)

            obs[i, 0] = float(sig.phase)
            obs[i, 1] = min(sig.time_in_phase_sec / 60.0, 1.0)
            obs[i, 2] = min(sig.queue_vehicles * AVG_VEHICLE_LENGTH_M / 500.0, 1.0)
            obs[i, 3] = min(ARRIVAL_RATE_VPS / 1.0, 1.0)  # normalized arrival
            obs[i, 4] = min(distance / 5000.0, 1.0)
            obs[i, 5] = min(eta / 300.0, 1.0)

        return obs

    def _get_info(self) -> dict:
        """Auxiliary info dict for logging / debugging."""
        return {
            "step": self._step_count,
            "convoy_position_m": round(self._convoy.position_m, 1),
            "convoy_stopped": self._convoy.stopped,
            "total_public_delay": round(self._total_public_delay, 1),
            "max_queue_m": round(
                max(s.queue_vehicles * AVG_VEHICLE_LENGTH_M for s in self._signals), 1
            ) if self._signals else 0.0,
        }
