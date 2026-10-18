"""Enumerations for the adaptive (T2) situational-awareness layer.

Everything in this package is pure, deterministic Python: no model calls,
no I/O, no clock reads.  The layer consumes *scores* (congestion indices,
weather severity, crowd density) — never raw text — and maps them onto a
small, totally-ordered set of levels that downstream policy code can
reason about exhaustively.

Ordering contract: every enum below implements a severity *rank*
(``rank`` property, 0 = mildest).  Tests assert that the situation
classifier is monotone in each rank.
"""

from __future__ import annotations

from enum import Enum


class _RankedEnum(Enum):
    """Base class providing a total severity order plus stable codes."""

    @property
    def rank(self) -> int:
        """Severity rank; 0 is mildest, higher is more severe."""
        return type(self)._RANKS[self]  # type: ignore[attr-defined]

    @property
    def code(self) -> str:
        """Short stable string used inside situation codes and logs."""
        return self.name

    @classmethod
    def from_code(cls, value: str) -> _RankedEnum:
        """Parse a code or name (case-insensitive); raises ValueError."""
        normalized = str(value).strip().upper()
        # Accept both the enum name and any registered alias.
        aliases: dict[str, _RankedEnum] = getattr(cls, "_ALIASES", {})  # type: ignore[assignment]
        if normalized in aliases:
            return aliases[normalized]  # type: ignore[return-value]
        for member in cls:
            if member.name == normalized:
                return member
        valid = sorted({m.name for m in cls} | set(aliases))
        raise ValueError(f"invalid {cls.__name__}: {value!r} (valid: {valid})")


class SecurityTier(_RankedEnum):
    """Corridor protection class.  Order: none < X < Y < Z < Z+.

    The legacy name is kept as the shared type every layer already imports;
    ``ProtectionClass`` is the event-agnostic alias.  ``NONE`` (code
    ``'none'``) is a public/civic corridor event with no protectee — a
    procession, gathering, race or work zone — scoring 0 security points,
    same as X, with no protocol floor.
    """

    NONE = "none"
    X = "X"
    Y = "Y"
    Z = "Z"
    Z_PLUS = "Z+"

    @property
    def code(self) -> str:
        if self is SecurityTier.Z_PLUS:
            return "Z+"
        if self is SecurityTier.NONE:
            return "none"
        return self.name

    @classmethod
    def from_code(cls, value: str) -> SecurityTier:
        if not isinstance(value, str):
            # None / numbers must stay invalid — str(None) == "NONE" would
            # otherwise silently classify a missing value as a public event.
            raise ValueError(
                f"invalid SecurityTier: {value!r} (valid: none, X, Y, Z, Z+)"
            )
        normalized = value.strip().upper()
        if normalized in ("Z+", "ZPLUS", "Z_PLUS", "ZP"):
            return cls.Z_PLUS
        if normalized in ("NONE", "PUBLIC", "NO_PROTECTEE"):
            return cls.NONE
        for member in cls:
            if member.name == normalized:
                return member
        raise ValueError(
            f"invalid SecurityTier: {value!r} (valid: none, X, Y, Z, Z+)"
        )


#: Event-agnostic alias — a corridor event's protection class.
ProtectionClass = SecurityTier


class TrafficRegime(_RankedEnum):
    """Traffic regime derived from predicted congestion index."""

    FREE_FLOW = "FREE_FLOW"
    HEAVY = "HEAVY"
    CONGESTED = "CONGESTED"
    GRIDLOCK = "GRIDLOCK"


class WeatherCondition(_RankedEnum):
    """Weather condition.  Order: CLEAR < RAIN < FOG < STORM."""

    CLEAR = "CLEAR"
    RAIN = "RAIN"
    FOG = "FOG"
    STORM = "STORM"


class CrowdDensity(_RankedEnum):
    """Crowd density along the corridor.  Order: LOW < MEDIUM < HIGH < EXTREME."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


class SituationClass(_RankedEnum):
    """Coarse situation band (the published ``situation_class``).

    GREEN   — routine; today's constants apply.
    AMBER   — elevated; earlier spike detection, security-leaning weights.
    RED     — critical; longer advance closure, faster polling, escalation.
    BLACK   — contingency; operator takeover, maximal protocol posture.
    """

    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"
    BLACK = "BLACK"


class ParamSource(_RankedEnum):
    """Provenance of published parameters (C4 contract values)."""

    BASELINE = "baseline"
    RETRIEVED = "retrieved"
    LEARNED = "learned"

    @property
    def code(self) -> str:
        return self.value


class ShadowEvalStatus(_RankedEnum):
    """Outcome of the twin shadow-evaluation gate (C4 fail-closed rule).

    The C4 response schema exposes only ``passed`` and ``unavailable``.
    A twin that *ran* and rejected a deviation is reported as
    ``unavailable`` in the response (fail-closed to baseline) with the
    precise outcome carried in ``evidence.shadow_eval_detail``.
    """

    PASSED = "passed"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"

    @property
    def code(self) -> str:
        return self.value

    @property
    def published(self) -> str:
        """Value allowed in the C4 response ``shadow_eval`` field."""
        return "passed" if self is ShadowEvalStatus.PASSED else "unavailable"


class EscalationLevel(_RankedEnum):
    """How hard the system waves at humans."""

    NONE = "NONE"
    NOTIFY = "NOTIFY"
    TAKEOVER = "TAKEOVER"


class DegradationTier(_RankedEnum):
    """Decision-stack tier under model degradation.

    FULL             — decider (27B) + Gemma tactical both healthy.
    TACTICAL_FALLBACK — decider slow/unavailable; Gemma-only tactical
                        control, operator alert (plan: /decider/health
                        p95 > 3 s triggers exactly this).
    RULES_ONLY       — both model tiers unavailable; pure rules/baseline
                        posture, all adaptive params forced to baseline.
    """

    FULL = "FULL"
    TACTICAL_FALLBACK = "TACTICAL_FALLBACK"
    RULES_ONLY = "RULES_ONLY"


# Rank tables (indices mirror declaration order, mildest → most severe).
# NONE ranks at -1 (below X, per the plan) so every existing rank assertion
# on X/Y/Z/Z+ keeps its value; 'none' is a public event, never a protectee.
SecurityTier._RANKS = {  # type: ignore[attr-defined]
    SecurityTier.NONE: -1,
    SecurityTier.X: 0,
    SecurityTier.Y: 1,
    SecurityTier.Z: 2,
    SecurityTier.Z_PLUS: 3,
}
TrafficRegime._RANKS = {  # type: ignore[attr-defined]
    TrafficRegime.FREE_FLOW: 0,
    TrafficRegime.HEAVY: 1,
    TrafficRegime.CONGESTED: 2,
    TrafficRegime.GRIDLOCK: 3,
}
WeatherCondition._RANKS = {  # type: ignore[attr-defined]
    WeatherCondition.CLEAR: 0,
    WeatherCondition.RAIN: 1,
    WeatherCondition.FOG: 2,
    WeatherCondition.STORM: 3,
}
CrowdDensity._RANKS = {  # type: ignore[attr-defined]
    CrowdDensity.LOW: 0,
    CrowdDensity.MEDIUM: 1,
    CrowdDensity.HIGH: 2,
    CrowdDensity.EXTREME: 3,
}
SituationClass._RANKS = {  # type: ignore[attr-defined]
    SituationClass.GREEN: 0,
    SituationClass.AMBER: 1,
    SituationClass.RED: 2,
    SituationClass.BLACK: 3,
}
ParamSource._RANKS = {  # type: ignore[attr-defined]
    ParamSource.BASELINE: 0,
    ParamSource.RETRIEVED: 1,
    ParamSource.LEARNED: 2,
}
ShadowEvalStatus._RANKS = {  # type: ignore[attr-defined]
    ShadowEvalStatus.UNAVAILABLE: 0,
    ShadowEvalStatus.FAILED: 1,
    ShadowEvalStatus.PASSED: 2,
}
EscalationLevel._RANKS = {  # type: ignore[attr-defined]
    EscalationLevel.NONE: 0,
    EscalationLevel.NOTIFY: 1,
    EscalationLevel.TAKEOVER: 2,
}
DegradationTier._RANKS = {  # type: ignore[attr-defined]
    DegradationTier.FULL: 0,
    DegradationTier.TACTICAL_FALLBACK: 1,
    DegradationTier.RULES_ONLY: 2,
}
