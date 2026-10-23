"""Hysteresis controller for situation-class transitions.

Problem: raw classification is a step function of noisy inputs; feeding
it straight into policy would make route-scoring weights and diversion
timing oscillate (and fire a decider checkpoint on every flap — plan
cadence trigger #3 is an adaptive ``situation_class`` change).

Solution: asymmetric confirmation STREAKS plus a minimum dwell time.

* Escalation (band worsens) requires ``up_confirm`` *consecutive*
  observations strictly ABOVE the effective band — except BLACK, which is
  immediate.  The band then rises to the LOWEST band seen in that streak
  (every observation of the streak agrees the situation is at least that
  bad), so an AMBER/RED flicker above GREEN escalates to AMBER instead of
  never escalating at all.
* De-escalation (band improves) requires ``down_confirm`` *consecutive*
  observations strictly BELOW the effective band **and** ``min_dwell_sec``
  elapsed since the last transition.  The band then falls to the HIGHEST
  band seen in that streak (the most severe recent reading still bounds
  it).  Counting "strictly below" rather than "the same lower band" is
  what breaks the old RED latch: after RED, a recovery that alternates
  AMBER/GREEN used to reset the candidate on every tick and kept the
  event RED indefinitely (and RED_SUSTAINED could hand a recovered
  movement to humans).  It now settles on AMBER — the honest band.
* An observation AT the effective band ends both streaks; an observation
  on the other side of the band ends the opposite streak.

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
    """Mutable tracking state (one per movement).

    ``candidate`` is the band the open streak would move to (the lowest
    band of an up-streak, the highest band of a down-streak) and
    ``candidate_count`` the streak length; both are None/0 when no streak
    is open.  ``direction`` is +1 for an up-streak, -1 for a down-streak.
    """

    current: SituationClass = SituationClass.GREEN
    initialized: bool = False
    last_transition_monotonic: float = 0.0
    candidate: SituationClass | None = None
    candidate_count: int = 0
    direction: int = 0


@dataclass(frozen=True)
class HysteresisResult:
    """Outcome of one evaluation."""

    effective: SituationClass        # class to act on after this observation
    observed: SituationClass         # raw classification this tick
    transitioned: bool               # effective changed this tick
    pending: SituationClass | None   # band the open streak would adopt
    pending_count: int               # length of the open streak


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
            self._clear_streak()
            return HysteresisResult(observed, observed, False, None, 0)

        if observed is st.current:
            # Agreement ends any open streak; nothing pending.
            self._clear_streak()
            return HysteresisResult(st.current, observed, False, None, 0)

        direction = 1 if observed.rank > st.current.rank else -1
        if st.direction == direction and st.candidate is not None:
            st.candidate_count += 1
            # Up-streak: the band every reading agrees on is the LOWEST
            # above-band reading; down-streak: the HIGHEST below-band one.
            if (observed.rank - st.candidate.rank) * direction < 0:
                st.candidate = observed
        else:
            st.direction = direction
            st.candidate = observed
            st.candidate_count = 1

        if direction > 0:
            if self.config.black_immediate and observed is SituationClass.BLACK:
                return self._transition(SituationClass.BLACK, observed, now_monotonic)
            if st.candidate_count >= self.config.up_confirm:
                return self._transition(st.candidate, observed, now_monotonic)
        else:
            dwell_ok = (
                now_monotonic - st.last_transition_monotonic
            ) >= self.config.min_dwell_sec
            if st.candidate_count >= self.config.down_confirm and dwell_ok:
                return self._transition(st.candidate, observed, now_monotonic)

        return HysteresisResult(
            st.current,
            observed,
            False,
            st.candidate,
            st.candidate_count,
        )

    def _clear_streak(self) -> None:
        st = self.state
        st.candidate = None
        st.candidate_count = 0
        st.direction = 0

    def _transition(
        self,
        target: SituationClass,
        observed: SituationClass,
        now_monotonic: float,
    ) -> HysteresisResult:
        st = self.state
        st.current = target
        st.last_transition_monotonic = now_monotonic
        self._clear_streak()
        return HysteresisResult(target, observed, True, None, 0)

    def snapshot(self) -> HysteresisState:
        """Copy of current state (for status endpoints)."""
        return HysteresisState(
            current=self.state.current,
            initialized=self.state.initialized,
            last_transition_monotonic=self.state.last_transition_monotonic,
            candidate=self.state.candidate,
            candidate_count=self.state.candidate_count,
            direction=self.state.direction,
        )


__all__ = [
    "HysteresisConfig",
    "HysteresisController",
    "HysteresisResult",
    "HysteresisState",
]
