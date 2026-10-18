"""Hysteresis controller for situation-class transitions.

Problem: raw classification is a step function of noisy inputs; feeding
it straight into policy would make route-scoring weights and diversion
timing oscillate (and fire a decider checkpoint on every flap — plan
cadence trigger #3 is an adaptive ``situation_class`` change).

Solution: asymmetric confirmation counts plus a minimum dwell time.

* Escalation (band worsens) requires ``up_confirm`` *consecutive*
  observations at the higher band — except BLACK, which is immediate.
* De-escalation (band improves) requires ``down_confirm`` *consecutive*
  observations at the lower band **and** ``min_dwell_sec`` elapsed since
  the last transition.
* Any observation that disagrees with the pending candidate resets the
  candidate counter to 1 for whatever that observation was.

The controller is deterministic given the sequence of (observation,
timestamp) pairs; time is injected by the caller (monotonic seconds).
"""

from __future__ import annotations

from dataclasses import dataclass

from traffic_oracle.adaptive.enums import SituationClass


@dataclass(frozen=True)
class HysteresisConfig:
    """Tuning knobs; defaults are the shipping values."""

    up_confirm: int = 2
    down_confirm: int = 4
    min_dwell_sec: float = 120.0
    black_immediate: bool = True

    def __post_init__(self) -> None:
        if self.up_confirm < 1 or self.down_confirm < 1:
            raise ValueError("confirmation counts must be >= 1")
        if self.min_dwell_sec < 0:
            raise ValueError("min_dwell_sec must be >= 0")


@dataclass
class HysteresisState:
    """Mutable tracking state (one per movement)."""

    current: SituationClass = SituationClass.GREEN
    initialized: bool = False
    last_transition_monotonic: float = 0.0
    candidate: SituationClass | None = None
    candidate_count: int = 0


@dataclass(frozen=True)
class HysteresisResult:
    """Outcome of one evaluation."""

    effective: SituationClass        # class to act on after this observation
    observed: SituationClass         # raw classification this tick
    transitioned: bool               # effective changed this tick
    pending: SituationClass | None   # candidate awaiting confirmation
    pending_count: int


class HysteresisController:
    """Filters a stream of observed classes into a stable effective class."""

    def __init__(
        self,
        config: HysteresisConfig | None = None,
        state: HysteresisState | None = None,
    ) -> None:
        self.config = config or HysteresisConfig()
        self.state = state or HysteresisState()

    def evaluate(
        self, observed: SituationClass, now_monotonic: float
    ) -> HysteresisResult:
        """Fold one observation into the effective class stream."""
        st = self.state

        if not st.initialized:
            st.current = observed
            st.initialized = True
            st.last_transition_monotonic = now_monotonic
            st.candidate = None
            st.candidate_count = 0
            return HysteresisResult(observed, observed, False, None, 0)

        if observed is st.current:
            # Agreement re-arms the state; nothing pending.
            st.candidate = None
            st.candidate_count = 0
            return HysteresisResult(st.current, observed, False, None, 0)

        # Track/refresh the candidate streak.
        if st.candidate is observed:
            st.candidate_count += 1
        else:
            st.candidate = observed
            st.candidate_count = 1

        required: int
        if observed.rank > st.current.rank:
            # Escalation.
            required = self.config.up_confirm
            if (
                self.config.black_immediate
                and observed is SituationClass.BLACK
            ):
                required = 1
            if st.candidate_count >= required:
                return self._transition(observed, now_monotonic)
        else:
            # De-escalation: confirmation count AND dwell time.
            required = self.config.down_confirm
            dwell_ok = (
                now_monotonic - st.last_transition_monotonic
            ) >= self.config.min_dwell_sec
            if st.candidate_count >= required and dwell_ok:
                return self._transition(observed, now_monotonic)

        return HysteresisResult(
            st.current,
            observed,
            False,
            st.candidate,
            st.candidate_count,
        )

    def _transition(
        self, target: SituationClass, now_monotonic: float
    ) -> HysteresisResult:
        st = self.state
        previous = st.current
        st.current = target
        st.last_transition_monotonic = now_monotonic
        st.candidate = None
        st.candidate_count = 0
        _ = previous  # available for callers that want from/to auditing
        return HysteresisResult(target, target, True, None, 0)

    def snapshot(self) -> HysteresisState:
        """Copy of current state (for status endpoints)."""
        return HysteresisState(
            current=self.state.current,
            initialized=self.state.initialized,
            last_transition_monotonic=self.state.last_transition_monotonic,
            candidate=self.state.candidate,
            candidate_count=self.state.candidate_count,
        )


__all__ = [
    "HysteresisConfig",
    "HysteresisController",
    "HysteresisResult",
    "HysteresisState",
]
