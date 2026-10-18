"""Gymnasium environments for corridor RL training.

Two environments live here:

``SignalCorridorEnv``
    The original K-signal green-wave environment (K ≤ 20 signals,
    5-second step resolution, 600-step episodes).  Logic unchanged from
    v1; only the import surface was reworked.

``TwinCorridorEnv``
    A thin gymnasium adapter around the simulation stream's plain-Python
    ``simulation.twin.TwinEnv`` (contract C5): the twin exposes
    ``reset(seed) -> obs``, ``step(action) -> (obs, reward, done, info)``
    and an ``is_stub: bool`` flag, and must never import gymnasium
    itself.  This wrapper owns all gymnasium vocabulary (spaces, the
    5-tuple step contract, truncation bookkeeping).

Import-graph note (red-team F3): ``gymnasium`` is an adaptive-stream
dependency declared in pyproject.toml, but this module remains importable
without it — ``GYMNASIUM_AVAILABLE`` is False and constructing either
environment then raises a clear RuntimeError instead of an ImportError at
module import time.  This is what lets the traffic-oracle import graph
load in minimal environments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import structlog

from traffic_oracle.optimize.rl.reward import compute_signal_reward

try:  # pragma: no cover - depends on environment
    import gymnasium as gym
    from gymnasium import spaces

    GYMNASIUM_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    gym = None  # type: ignore[assignment]
    spaces = None  # type: ignore[assignment]
    GYMNASIUM_AVAILABLE = False

logger = structlog.get_logger(__name__)

# Base class that keeps the module importable when gymnasium is absent.
_GymEnvBase = gym.Env if GYMNASIUM_AVAILABLE else object


def _require_gymnasium() -> None:
    if not GYMNASIUM_AVAILABLE:
        raise RuntimeError(
            "gymnasium is required for RL environments but is not "
            "installed in this environment (it is declared in "
            "services/traffic-oracle/pyproject.toml)"
        )


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


class SignalCorridorEnv(_GymEnvBase):
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
        _require_gymnasium()
        if GYMNASIUM_AVAILABLE:
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
        if GYMNASIUM_AVAILABLE:
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
                sig.queue_vehicles = max(0.0, sig.queue_vehicles - discharged)

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


# ---------------------------------------------------------------------------
# TwinEnv adapter (contract C5)
# ---------------------------------------------------------------------------


class TwinCorridorEnv(_GymEnvBase):
    """Gymnasium wrapper around the simulation stream's plain TwinEnv.

    The twin is a black box with the C5 surface:
        reset(seed) -> obs
        step(action) -> (obs, reward, done, info)
        is_stub: bool
    This adapter translates to the gymnasium 5-tuple
    ``(obs, reward, terminated, truncated, info)``; ``truncated`` is
    raised when the wrapper's own ``max_steps`` budget is exhausted
    (the twin only reports ``done``).

    Spaces are built lazily from the first observation, so the wrapper
    works regardless of the twin's observation encoding (any object
    convertible to a float array).
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        twin: Any | None = None,
        max_steps: int = MAX_STEPS,
        seed: int | None = None,
    ) -> None:
        _require_gymnasium()
        if GYMNASIUM_AVAILABLE:
            super().__init__()
        self._twin = twin
        self._max_steps = max_steps
        self._seed = seed
        self._step_count = 0
        self.observation_space: Any = None
        self.action_space: Any = None
        if twin is not None and getattr(twin, "is_stub", False):
            raise RuntimeError(
                "TwinCorridorEnv cannot train on a stub twin "
                "(simulation.twin.TwinEnv.is_stub is True)"
            )

    # -- twin construction -----------------------------------------------------

    @classmethod
    def from_simulation(cls, **kwargs: Any) -> TwinCorridorEnv:
        """Build a TwinEnv from the simulation package (defensive import).

        Returns None-equivalent failure as RuntimeError: callers in the
        adaptive stream must fail closed, but an RL *training* entrypoint
        fails loudly instead.
        """
        try:
            from traffic_oracle.simulation.twin import TwinEnv  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "simulation.twin.TwinEnv is not available (simulation "
                "stream not landed or not installed)"
            ) from exc
        twin = TwinEnv(**kwargs) if kwargs else TwinEnv()
        return cls(twin=twin)

    @property
    def twin_is_stub(self) -> bool:
        return bool(getattr(self._twin, "is_stub", False))

    # -- gym API ----------------------------------------------------------------

    def _coerce_obs(self, obs: Any) -> np.ndarray:
        arr = np.asarray(obs, dtype=np.float32)
        if arr.ndim == 0:
            arr = arr.reshape(1)
        return arr

    def _build_spaces(self, obs: np.ndarray) -> None:
        if self.observation_space is None and spaces is not None:
            self.observation_space = spaces.Box(
                low=-np.inf,
                high=np.inf,
                shape=obs.shape,
                dtype=np.float32,
            )
            # Action space must be supplied by the caller (twin semantics
            # are simulation-owned); default to a Discrete(1) placeholder
            # so gym API contract checks still pass.
            self.action_space = spaces.Discrete(1)

    def set_action_space(self, action_space: Any) -> None:
        """Inject the twin-appropriate action space (caller-owned)."""
        self.action_space = action_space

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict | None = None,
    ) -> tuple[np.ndarray, dict]:
        if GYMNASIUM_AVAILABLE:
            super().reset(seed=seed)
        if self._twin is None:
            raise RuntimeError("TwinCorridorEnv has no twin to reset")
        eff_seed = seed if seed is not None else self._seed
        obs = self._twin.reset(seed=eff_seed) if eff_seed is not None else self._twin.reset()
        self._step_count = 0
        obs_arr = self._coerce_obs(obs)
        self._build_spaces(obs_arr)
        info = {} if options is None else dict(options)
        return obs_arr, info

    def step(self, action: Any) -> tuple[np.ndarray, float, bool, bool, dict]:
        if self._twin is None:
            raise RuntimeError("TwinCorridorEnv has no twin to step")
        self._step_count += 1
        obs, reward, done, info = self._twin.step(action)
        terminated = bool(done)
        truncated = (not terminated) and self._step_count >= self._max_steps
        return (
            self._coerce_obs(obs),
            float(reward),
            terminated,
            truncated,
            dict(info) if info else {},
        )


__all__ = [
    "ARRIVAL_RATE_VPS",
    "AVG_VEHICLE_LENGTH_M",
    "ConvoyState",
    "GYMNASIUM_AVAILABLE",
    "MAX_QUEUE_VEHICLES",
    "MAX_STEPS",
    "QUEUE_DISCHARGE_RATE_VPS",
    "SignalCorridorEnv",
    "SignalState",
    "STEP_DURATION_SEC",
    "TwinCorridorEnv",
]
