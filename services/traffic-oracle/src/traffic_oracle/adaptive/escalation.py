"""Escalation rules — when the system must hand control to humans.

Deterministic rule table evaluated over (situation, degradation, side
inputs).  Every rule contributes a stable reason code; the highest
escalation level wins.  ``EscalationEngine.evaluate`` is a pure function
of its arguments.
"""

from __future__ import annotations

from dataclasses import dataclass

from traffic_oracle.adaptive.enums import (
    DegradationTier,
    EscalationLevel,
    SecurityTier,
    SituationClass,
)
from traffic_oracle.adaptive.inputs import EscalationInputs


@dataclass(frozen=True)
class EscalationConfig:
    """Rule thresholds; defaults are the shipping values."""

    red_sustain_takeover_sec: float = 300.0   # Z+/Z stuck in RED this long
    gps_stale_sec: float = 30.0               # telemetry age → takeover
    ambulance_delay_notify_sec: float = 60.0
    ambulance_delay_takeover_sec: float = 120.0
    high_tiers: frozenset[SecurityTier] = frozenset(
        {SecurityTier.Z, SecurityTier.Z_PLUS}
    )
    notify_min_band: SituationClass = SituationClass.AMBER


@dataclass(frozen=True)
class EscalationDecision:
    """Result of evaluating the rule table."""

    escalate: bool
    level: EscalationLevel
    reasons: tuple[str, ...]      # stable rule codes, rule order
    handoff: str                  # "none" | "operator"

    def to_dict(self) -> dict:
        return {
            "escalate": self.escalate,
            "level": self.level.code,
            "reasons": list(self.reasons),
            "handoff": self.handoff,
        }


class EscalationEngine:
    """Evaluates the escalation rule table; stateless and deterministic."""

    def __init__(self, config: EscalationConfig | None = None) -> None:
        self.config = config or EscalationConfig()

    def evaluate(
        self,
        situation_class: SituationClass,
        vvip_class: SecurityTier,
        degradation_tier: DegradationTier,
        side_inputs: EscalationInputs | None = None,
    ) -> EscalationDecision:
        side = side_inputs or EscalationInputs()
        cfg = self.config
        reasons: list[str] = []
        level = EscalationLevel.NONE

        def raise_to(new_level: EscalationLevel, reason: str) -> None:
            nonlocal level
            reasons.append(reason)
            if new_level.rank > level.rank:
                level = new_level

        # R1 — BLACK band: unconditional operator takeover.
        if situation_class is SituationClass.BLACK:
            raise_to(EscalationLevel.TAKEOVER, "SITUATION_BLACK")

        # R2 — Z+/Z pinned in RED beyond the sustain window.
        if (
            situation_class is SituationClass.RED
            and vvip_class in cfg.high_tiers
            and side.red_sustained_sec >= cfg.red_sustain_takeover_sec
        ):
            raise_to(EscalationLevel.TAKEOVER, "RED_SUSTAINED_HIGH_TIER")

        # R3 — decision stack fully degraded while situation ≥ RED:
        # rules-only control cannot be trusted for critical movements.
        if (
            degradation_tier is DegradationTier.RULES_ONLY
            and situation_class.rank >= SituationClass.RED.rank
        ):
            raise_to(EscalationLevel.TAKEOVER, "MODEL_OUT_IN_RED")

        # R4 — decider degraded (Gemma-only fallback) while ≥ RED.
        if (
            degradation_tier is DegradationTier.TACTICAL_FALLBACK
            and situation_class.rank >= SituationClass.RED.rank
        ):
            raise_to(EscalationLevel.NOTIFY, "DECIDER_DEGRADED_IN_RED")

        # R5 — a deviation was rejected by the twin shadow-eval.
        if side.shadow_eval_failed:
            raise_to(EscalationLevel.NOTIFY, "SHADOW_EVAL_FAILED")

        # R6 — telemetry staleness while at or above AMBER.
        if (
            side.last_gps_age_sec > cfg.gps_stale_sec
            and situation_class.rank >= cfg.notify_min_band.rank
        ):
            raise_to(EscalationLevel.TAKEOVER, "GPS_STALE")

        # R7 — projected ambulance delay beyond protocol windows.
        if side.ambulance_delay_projection_sec >= cfg.ambulance_delay_takeover_sec:
            raise_to(EscalationLevel.TAKEOVER, "AMBULANCE_DELAY_SEVERE")
        elif side.ambulance_delay_projection_sec >= cfg.ambulance_delay_notify_sec:
            raise_to(EscalationLevel.NOTIFY, "AMBULANCE_DELAY_ELEVATED")

        # R8 — extreme crowds around a top-tier movement: advisory even
        # when the numeric band has not yet caught up.
        if (
            vvip_class is SecurityTier.Z_PLUS
            and situation_class in (SituationClass.AMBER, SituationClass.RED)
            and side.crowd_extreme
        ):
            raise_to(EscalationLevel.NOTIFY, "CROWD_EXTREME_HIGH_TIER")

        escalate = level is not EscalationLevel.NONE
        return EscalationDecision(
            escalate=escalate,
            level=level,
            reasons=tuple(reasons),
            handoff="operator" if escalate else "none",
        )


__all__ = [
    "EscalationConfig",
    "EscalationDecision",
    "EscalationEngine",
]
