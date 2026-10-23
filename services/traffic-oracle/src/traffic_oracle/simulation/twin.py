"""TwinEnv — plain-Python RL environment wrapping the corridor twin.

Contract (C5): ``simulation.twin.TwinEnv`` is a PLAIN-Python class with
``reset(seed) -> obs``, ``step(action) -> (obs, reward, done, info)`` and an
``is_stub: bool`` attribute. It carries no dependency on the gymnasium
package (that is adaptive-only — rl/environment.py wraps this class
instead).

Action semantics (both accepted):
    - ``int``  → index into the default action grid below
    - ``dict`` → ``{"advance_closure_sec": int, "closure_type": str}``

Reward: negative weighted cost of one decision epoch —
    reward = -(disruption_veh_h·3600 + 0.5·transit_sec + 4·ambulance_delay_sec)
             / 1000
scaled to a readable magnitude for DQN-style critics.

Observation: flat, JSON-serializable dict of per-segment congestion, queues,
event flags, clock features, and convoy position.
"""

from __future__ import annotations

from typing import Any

from traffic_oracle.simulation.engine import (
    VALID_CLOSURE_TYPES,
    ClosurePolicy,
    ForecastShapes,
    ReplicationResult,
    SimulationConfig,
    SimulationEngine,
    run_replication,
)
from traffic_oracle.simulation.events import EventConfig, ScheduledEvent
from traffic_oracle.simulation.network import SimNetwork, default_network
from traffic_oracle.simulation.rng import derive_seed
from traffic_oracle.simulation.tod import WeatherState, to_ist

# Discrete action grid: (advance_closure_sec, closure_type).
DEFAULT_ACTIONS: tuple[dict[str, Any], ...] = tuple(
    {"advance_closure_sec": adv, "closure_type": ctype}
    for adv in (60, 120, 180)
    for ctype in VALID_CLOSURE_TYPES
)

# Each epoch simulates one full movement; the env ends after `max_epochs`
# epochs so offline DQN training gets multiple episodes per env instance.
_EPOCH_HORIZON_MIN = 45


class TwinEnv:
    """Seedable twin environment — the single object adaptive's DQN wraps."""

    is_stub: bool = False  # real engine behind this env (contract C5)

    def __init__(
        self,
        route_segment_ids: list[int] | None = None,
        departure_time=None,
        network: SimNetwork | None = None,
        config: SimulationConfig | None = None,
        forecast_shapes: ForecastShapes | None = None,
        weather: WeatherState | None = None,
        event_config: EventConfig | None = None,
        scheduled_events: list[ScheduledEvent] | None = None,
        max_epochs: int = 20,
    ) -> None:
        self._network = network or default_network()
        self._engine = SimulationEngine(network=self._network, config=config)
        self._route = list(
            route_segment_ids
            if route_segment_ids is not None
            else [1001, 1002, 1009]  # Ashram spine default (seed network)
        )
        if departure_time is None:
            from datetime import datetime

            departure_time = datetime.fromisoformat("2026-10-13T13:40:00+05:30")
        self._departure_time = departure_time
        self._forecast_shapes = forecast_shapes or {}
        self._weather = weather
        self._event_config = event_config
        self._scheduled_events = scheduled_events
        self._max_epochs = max_epochs

        self._epoch = 0
        self._seed: int | None = None
        self._last_result: ReplicationResult | None = None
        self._cumulative_reward = 0.0

    # ── gym-shaped API (plain python, no gymnasium) ───────────────────

    @property
    def action_space(self) -> dict:
        """Descriptive action-space metadata (no gymnasium dependency)."""
        return {"n": len(DEFAULT_ACTIONS), "actions": [dict(a) for a in DEFAULT_ACTIONS]}

    def reset(self, seed: int | None = None) -> dict:
        """Start a fresh episode; returns the first observation."""
        self._seed = seed if seed is not None else derive_seed("twin-default")
        self._epoch = 0
        self._cumulative_reward = 0.0
        self._last_result = self._simulate(self._current_action_defaults())
        return self._observation()

    def step(self, action: int | dict) -> tuple[dict, float, bool, dict]:
        """Advance one decision epoch with the given closure policy."""
        if self._seed is None:
            raise RuntimeError("call reset(seed) before step()")

        policy_dict = self._resolve_action(action)
        policy = ClosurePolicy(**policy_dict)

        self._last_result = self._simulate(policy)
        reward = self._reward(self._last_result)
        self._cumulative_reward += reward
        self._epoch += 1
        done = self._epoch >= self._max_epochs

        info = {
            "epoch": self._epoch,
            "action": policy_dict,
            "metrics": self._last_result.to_dict(),
            "cumulative_reward": round(self._cumulative_reward, 3),
            # Sim → DSTGAT horizon shapes (same format FlowForecaster emits):
            "forecast_feedback": self._last_result.forecast_feedback,
        }
        return self._observation(), reward, done, info

    # ── internals ─────────────────────────────────────────────────────

    def _current_action_defaults(self) -> ClosurePolicy:
        return ClosurePolicy(advance_closure_sec=120, closure_type="full_closure")

    def _resolve_action(self, action: int | dict) -> dict:
        if isinstance(action, int):
            if not 0 <= action < len(DEFAULT_ACTIONS):
                raise ValueError(
                    f"action index {action} out of range [0, {len(DEFAULT_ACTIONS)})"
                )
            return dict(DEFAULT_ACTIONS[action])
        if isinstance(action, dict):
            adv = int(action.get("advance_closure_sec", 120))
            ctype = str(action.get("closure_type", "full_closure"))
            if ctype not in VALID_CLOSURE_TYPES:
                raise ValueError(f"closure_type must be one of {VALID_CLOSURE_TYPES}")
            return {"advance_closure_sec": adv, "closure_type": ctype}
        raise TypeError(f"action must be int or dict, got {type(action)!r}")

    def _simulate(self, policy: ClosurePolicy) -> ReplicationResult:
        return run_replication(
            replication=self._epoch,
            network=self._network,
            route_segment_ids=self._route,
            departure_time=self._departure_time,
            closure_policy=policy,
            horizon_sec=_EPOCH_HORIZON_MIN * 60.0,
            seed=derive_seed(self._seed, "env", self._epoch),
            config=self._engine._config,  # noqa: SLF001 — same-package twin
            forecast_shapes=self._forecast_shapes,
            weather=self._weather,
            event_config=self._event_config,
            scheduled_events=self._scheduled_events,
            adjacency=self._engine._adjacency,  # noqa: SLF001
        )

    def _reward(self, result: ReplicationResult) -> float:
        cost = (
            result.total_disruption_vehicle_hours * 3600.0
            + 0.5 * result.convoy_transit_time_sec
            + 4.0 * result.ambulance_delay_sec
        )
        return -cost / 1000.0

    def _observation(self) -> dict:
        r = self._last_result
        assert r is not None  # reset() always runs first
        n = len(self._route)
        # Per-segment congestion proxy from the forecast feedback at the
        # nearest knot (5 min), falling back to zeros before first capture.
        cong: list[float] = []
        speed: list[float] = []
        for sid in self._route:
            knot = r.forecast_feedback.get(sid, {}).get(5)
            if knot:
                cong.append(float(knot["congestion_idx"]))
                speed.append(float(knot["speed_kmh"]))
            else:
                cong.append(0.0)
                speed.append(0.0)
        return {
            "epoch": self._epoch,
            "seed": self._seed,
            "n_route_segments": n,
            "segment_congestion": [round(c, 3) for c in cong],
            "segment_speed_kmh": [round(s, 1) for s in speed],
            "max_queue_length_m": round(r.max_queue_length_m, 1),
            # clock features in IST (the demand curves' local time)
            "hour": to_ist(self._departure_time).hour,
            "day_of_week": to_ist(self._departure_time).weekday(),
            "monsoon": bool(self._weather.monsoon) if self._weather else False,
            "n_events_last_epoch": r.n_events,
            "cumulative_reward": round(self._cumulative_reward, 3),
        }
