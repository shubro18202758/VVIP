"""Adaptive policy engine — situation-conditioned parameters with
protocol floors and the C4 fail-closed rule.

Baseline constants (verified in-repo this session):
    advance_closure_sec        = 120   (convoy-brain mcp/server.py:626 default)
    congestion_spike_threshold = 0.8   (convoy-brain workflows/live_escort.py:42)
    gps_poll_interval_sec      = 1.0   (convoy-brain workflows/live_escort.py:35)
    scoring_weights            = time .3 / disruption .4 / security .2 /
                                 complexity .1
                                 (traffic-oracle optimize/corridor_router.py:60-63)

Contract obligations implemented here:

* ``source="baseline"`` publishes params identical to today's constants,
  with one exception: the protocol floor on ``advance_closure_sec``
  (Z+ ≥ 180, Z ≥ 120, Y ≥ 60; X has none) is a *safety clamp applied at
  all times*, including baseline.  When the clamp raises the value the
  response still says ``baseline`` and ``evidence.floor_applied`` is true.
* Any deviation from baseline (``retrieved`` or ``learned``) may only be
  published after a twin shadow-evaluation **pass**; otherwise the engine
  fails closed to baseline and reports ``shadow_eval="unavailable"`` with
  the precise outcome in ``evidence.shadow_eval_detail``.
* Situation-conditioned (candidate) values are monotone in severity:
  advance closure and polling cadence only ever grow, spike sensitivity
  only ever rises (threshold falls), and scoring weights always sum to 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from traffic_oracle.adaptive.enums import (
    ParamSource,
    SecurityTier,
    ShadowEvalStatus,
    SituationClass,
)
from traffic_oracle.adaptive.protocol_params_gen import (
    ADVANCE_CLOSURE_FLOOR_SEC as _GEN_FLOOR_SEC,
)
from traffic_oracle.adaptive.protocol_params_gen import (
    BASELINE_ADVANCE_CLOSURE_SEC as _GEN_BASELINE_ADVANCE,
)
from traffic_oracle.adaptive.protocol_params_gen import (
    BASELINE_CONGESTION_SPIKE_THRESHOLD as _GEN_BASELINE_SPIKE,
)
from traffic_oracle.adaptive.protocol_params_gen import (
    BASELINE_GPS_POLL_INTERVAL_SEC as _GEN_BASELINE_GPS,
)
from traffic_oracle.adaptive.protocol_params_gen import (
    BASELINE_SCORING_WEIGHTS as _GEN_BASELINE_WEIGHTS,
)
from traffic_oracle.adaptive.protocol_params_gen import (
    CONGESTION_THRESHOLD_MAX as _GEN_CONGESTION_MAX,
)
from traffic_oracle.adaptive.protocol_params_gen import (
    CONGESTION_THRESHOLD_MIN as _GEN_CONGESTION_MIN,
)
from traffic_oracle.adaptive.protocol_params_gen import (
    GPS_POLL_MAX_SEC as _GEN_GPS_MAX,
)
from traffic_oracle.adaptive.protocol_params_gen import (
    GPS_POLL_MIN_SEC as _GEN_GPS_MIN,
)

# ---------------------------------------------------------------------------
# Baseline constants — must stay identical to today's shipping values.
# Single source of truth: shared/schemas/protocol_params.json → generated
# module (contract C1); the names below are re-exports for callers/tests.
# ---------------------------------------------------------------------------

BASELINE_ADVANCE_CLOSURE_SEC: int = _GEN_BASELINE_ADVANCE
BASELINE_CONGESTION_SPIKE_THRESHOLD: float = _GEN_BASELINE_SPIKE
BASELINE_GPS_POLL_INTERVAL_SEC: float = _GEN_BASELINE_GPS
BASELINE_SCORING_WEIGHTS: dict[str, float] = dict(_GEN_BASELINE_WEIGHTS)

# Protocol floors for advance_closure_sec by protection class (plan C4:
# 180/120/60 s for Z+/Z/Y; X and 'none' have no protocol floor — n/a).
# Enum-keyed view of the canonical string-keyed table so every internal
# lookup (clamp_advance_closure, candidate_params, rl/bounds) keeps its
# exact call shape; the generated module is the only human-edited source.
ADVANCE_CLOSURE_FLOOR_SEC: dict[SecurityTier, int | None] = {
    SecurityTier.from_code(code): floor
    for code, floor in _GEN_FLOOR_SEC.items()
}

# Hard engineering bounds applied to every published param set.
# GPS_POLL_MIN_SEC = 1.0: the consuming brain (adaptive_params.py) enforces a
# hard protocol floor of one update per second on this parameter; publishing
# anything lower would be silently clamped upstream, so the adaptive layer
# respects the same floor at the source.
GPS_POLL_MIN_SEC: float = _GEN_GPS_MIN
GPS_POLL_MAX_SEC: float = _GEN_GPS_MAX
CONGESTION_THRESHOLD_MIN: float = _GEN_CONGESTION_MIN
CONGESTION_THRESHOLD_MAX: float = _GEN_CONGESTION_MAX  # never less sensitive than today

# Situation-conditioned candidate adjustments (band → deltas).
# advance bonus is added on top of max(baseline, floor).
_BAND_ADVANCE_BONUS_SEC: dict[SituationClass, int] = {
    SituationClass.GREEN: 0,
    SituationClass.AMBER: 0,
    SituationClass.RED: 30,
    SituationClass.BLACK: 60,
}
_BAND_SPIKE_THRESHOLD: dict[SituationClass, float] = {
    SituationClass.GREEN: 0.80,  # baseline
    SituationClass.AMBER: 0.75,
    SituationClass.RED: 0.70,
    SituationClass.BLACK: 0.65,
}
# GPS polling is pinned at today's 1.0 s constant across bands: the protocol
# floor equals the baseline, so no band may poll faster (see GPS_POLL_MIN_SEC).
_BAND_GPS_POLL_SEC: dict[SituationClass, float] = {
    SituationClass.GREEN: 1.0,   # baseline
    SituationClass.AMBER: 1.0,
    SituationClass.RED: 1.0,
    SituationClass.BLACK: 1.0,
}
_BAND_SCORING_WEIGHTS: dict[SituationClass, dict[str, float]] = {
    SituationClass.GREEN: dict(BASELINE_SCORING_WEIGHTS),
    SituationClass.AMBER: {
        "time": 0.25, "disruption": 0.40, "security": 0.25, "complexity": 0.10,
    },
    SituationClass.RED: {
        "time": 0.20, "disruption": 0.35, "security": 0.35, "complexity": 0.10,
    },
    SituationClass.BLACK: {
        "time": 0.15, "disruption": 0.30, "security": 0.40, "complexity": 0.15,
    },
}


def clamp_advance_closure(value: int, vvip_class: SecurityTier | str) -> int:
    """Clamp an advance-closure value to the protocol floor for the tier.

    Shared with ``optimize.rl.bounds`` so the DQN online path obeys the
    same protocol floors as the rules path.
    """
    tier = vvip_class if isinstance(vvip_class, SecurityTier) else SecurityTier.from_code(vvip_class)
    floor = ADVANCE_CLOSURE_FLOOR_SEC[tier]
    if floor is not None and value < floor:
        return floor
    return int(value)


@dataclass(frozen=True)
class AdaptiveParams:
    """A published parameter set (the ``params`` object of the C4 response)."""

    advance_closure_sec: int
    congestion_spike_threshold: float
    gps_poll_interval_sec: float
    scoring_weights: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "scoring_weights", dict(self.scoring_weights or {})
        )

    def to_dict(self) -> dict:
        return {
            "advance_closure_sec": self.advance_closure_sec,
            "congestion_spike_threshold": self.congestion_spike_threshold,
            "gps_poll_interval_sec": self.gps_poll_interval_sec,
            "scoring_weights": dict(self.scoring_weights),
        }

    @classmethod
    def baseline(cls) -> AdaptiveParams:
        """Today's constants, verbatim (no floor clamp — clamp happens at
        publish time because it depends on the security tier)."""
        return cls(
            advance_closure_sec=BASELINE_ADVANCE_CLOSURE_SEC,
            congestion_spike_threshold=BASELINE_CONGESTION_SPIKE_THRESHOLD,
            gps_poll_interval_sec=BASELINE_GPS_POLL_INTERVAL_SEC,
            scoring_weights=dict(BASELINE_SCORING_WEIGHTS),
        )

    def equals_baseline(self) -> bool:
        b = AdaptiveParams.baseline()
        return (
            self.advance_closure_sec == b.advance_closure_sec
            and self.congestion_spike_threshold == b.congestion_spike_threshold
            and self.gps_poll_interval_sec == b.gps_poll_interval_sec
            and self.scoring_weights == b.scoring_weights
        )


@dataclass(frozen=True)
class PolicyDecision:
    """Full C4-shaped decision produced by the policy engine."""

    situation_class: SituationClass
    params: AdaptiveParams
    source: ParamSource
    shadow_eval: ShadowEvalStatus
    shadow_eval_detail: str
    floor_applied: bool
    fail_closed_reason: str | None

    def to_response(self, evidence_extra: dict | None = None) -> dict:
        """Serialize to the exact C4 response shape."""
        evidence: dict = {
            "floor_applied": self.floor_applied,
            "shadow_eval_detail": self.shadow_eval_detail,
            "situation_band": self.situation_class.code,
            "baseline_constants": {
                "advance_closure_sec": BASELINE_ADVANCE_CLOSURE_SEC,
                "congestion_spike_threshold": BASELINE_CONGESTION_SPIKE_THRESHOLD,
                "gps_poll_interval_sec": BASELINE_GPS_POLL_INTERVAL_SEC,
                "scoring_weights": dict(BASELINE_SCORING_WEIGHTS),
            },
        }
        if self.fail_closed_reason is not None:
            evidence["fail_closed_reason"] = self.fail_closed_reason
        if evidence_extra:
            evidence.update(evidence_extra)
        return {
            "situation_class": self.situation_class.code,
            "params": self.params.to_dict(),
            "source": self.source.code,
            "shadow_eval": self.shadow_eval.published,
            "evidence": evidence,
        }


class ParameterAdapter:
    """Computes the parameter set to publish for (band, tier, gate state).

    Stateless and deterministic: every input is explicit.
    """

    def __init__(self) -> None:
        self._candidate_cache: dict[tuple[SituationClass, SecurityTier], AdaptiveParams] = {}

    # -- candidates ---------------------------------------------------------

    def candidate_params(
        self, situation_class: SituationClass, vvip_class: SecurityTier
    ) -> AdaptiveParams:
        """Situation-conditioned (non-baseline) candidate parameter set."""
        key = (situation_class, vvip_class)
        cached = self._candidate_cache.get(key)
        if cached is not None:
            return cached
        base = AdaptiveParams.baseline()
        floor = ADVANCE_CLOSURE_FLOOR_SEC[vvip_class]
        advance = max(
            base.advance_closure_sec,
            floor if floor is not None else 0,
            BASELINE_ADVANCE_CLOSURE_SEC + _BAND_ADVANCE_BONUS_SEC[situation_class],
        )
        spike = min(
            _BAND_SPIKE_THRESHOLD[situation_class],
            BASELINE_CONGESTION_SPIKE_THRESHOLD,
        )
        gps = max(
            min(_BAND_GPS_POLL_SEC[situation_class], base.gps_poll_interval_sec),
            GPS_POLL_MIN_SEC,
        )
        weights = dict(_BAND_SCORING_WEIGHTS[situation_class])
        params = AdaptiveParams(
            advance_closure_sec=advance,
            congestion_spike_threshold=round(spike, 2),
            gps_poll_interval_sec=gps,
            scoring_weights=weights,
        )
        self._candidate_cache[key] = params
        return params

    # -- baseline (floor-clamped) -------------------------------------------

    def baseline_params(self, vvip_class: SecurityTier) -> tuple[AdaptiveParams, bool]:
        """Today's constants with the protocol floor clamp applied.

        Returns (params, floor_applied).
        """
        clamped = clamp_advance_closure(
            BASELINE_ADVANCE_CLOSURE_SEC, vvip_class
        )
        floor_applied = clamped != BASELINE_ADVANCE_CLOSURE_SEC
        return (
            AdaptiveParams(
                advance_closure_sec=clamped,
                congestion_spike_threshold=BASELINE_CONGESTION_SPIKE_THRESHOLD,
                gps_poll_interval_sec=BASELINE_GPS_POLL_INTERVAL_SEC,
                scoring_weights=dict(BASELINE_SCORING_WEIGHTS),
            ),
            floor_applied,
        )

    # -- decision -----------------------------------------------------------

    def decide(
        self,
        situation_class: SituationClass,
        vvip_class: SecurityTier,
        shadow_status: ShadowEvalStatus = ShadowEvalStatus.UNAVAILABLE,
        shadow_detail: str = "twin shadow-eval not attempted",
        requested_source: ParamSource = ParamSource.RETRIEVED,
        force_baseline: bool = False,
    ) -> PolicyDecision:
        """Apply the C4 fail-closed rule and produce the publishable decision.

        Args:
            situation_class: effective (hysteresis-filtered) band.
            vvip_class: security tier of the movement.
            shadow_status: outcome of the twin shadow-eval gate.
            shadow_detail: human-readable explanation for the evidence block.
            requested_source: provenance of the candidate (retrieved/learned).
            force_baseline: hard override (degradation to RULES_ONLY, or any
                other condition that must pin today's constants).
        """
        candidate = self.candidate_params(situation_class, vvip_class)
        baseline, floor_applied = self.baseline_params(vvip_class)

        deviation_requested = not self.candidate_is_baseline_shaped(
            candidate, situation_class, vvip_class
        )

        if force_baseline:
            return PolicyDecision(
                situation_class=situation_class,
                params=baseline,
                source=ParamSource.BASELINE,
                shadow_eval=shadow_status,
                shadow_eval_detail=shadow_detail,
                floor_applied=floor_applied,
                fail_closed_reason="forced_baseline_degradation",
            )

        if not deviation_requested:
            # GREEN (and any band whose candidate collapses to baseline
            # values) needs no twin blessing: it publishes today's numbers.
            return PolicyDecision(
                situation_class=situation_class,
                params=candidate,
                source=ParamSource.BASELINE,
                shadow_eval=shadow_status,
                shadow_eval_detail=shadow_detail,
                floor_applied=floor_applied,
                fail_closed_reason=None,
            )

        if shadow_status is not ShadowEvalStatus.PASSED:
            reason = (
                "shadow_eval_failed_deviation_rejected"
                if shadow_status is ShadowEvalStatus.FAILED
                else "shadow_eval_unavailable_fail_closed"
            )
            return PolicyDecision(
                situation_class=situation_class,
                params=baseline,
                source=ParamSource.BASELINE,
                shadow_eval=shadow_status,
                shadow_eval_detail=shadow_detail,
                floor_applied=floor_applied,
                fail_closed_reason=reason,
            )

        return PolicyDecision(
            situation_class=situation_class,
            params=candidate,
            source=requested_source,
            shadow_eval=shadow_status,
            shadow_eval_detail=shadow_detail,
            floor_applied=floor_applied,
            fail_closed_reason=None,
        )

    @staticmethod
    def candidate_is_baseline_shaped(
        candidate: AdaptiveParams, band: SituationClass, tier: SecurityTier
    ) -> bool:
        """True when the candidate equals the floor-clamped baseline set.

        For GREEN bands the candidate tables intentionally collapse to the
        baseline constants (plus the tier floor); such "deviations" are
        not deviations and never require a twin pass.
        """
        base_clamped = clamp_advance_closure(BASELINE_ADVANCE_CLOSURE_SEC, tier)
        return (
            candidate.advance_closure_sec == base_clamped
            and candidate.congestion_spike_threshold
            == BASELINE_CONGESTION_SPIKE_THRESHOLD
            and candidate.gps_poll_interval_sec == BASELINE_GPS_POLL_INTERVAL_SEC
            and candidate.scoring_weights == BASELINE_SCORING_WEIGHTS
        )


__all__ = [
    "ADVANCE_CLOSURE_FLOOR_SEC",
    "BASELINE_ADVANCE_CLOSURE_SEC",
    "BASELINE_CONGESTION_SPIKE_THRESHOLD",
    "BASELINE_GPS_POLL_INTERVAL_SEC",
    "BASELINE_SCORING_WEIGHTS",
    "CONGESTION_THRESHOLD_MAX",
    "CONGESTION_THRESHOLD_MIN",
    "GPS_POLL_MAX_SEC",
    "GPS_POLL_MIN_SEC",
    "AdaptiveParams",
    "ParameterAdapter",
    "PolicyDecision",
    "clamp_advance_closure",
]
