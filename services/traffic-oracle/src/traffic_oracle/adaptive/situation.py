"""SituationEngine — deterministic situation classifier (security tier ×
traffic regime × weather × crowd density → situation code + class band).

Pure function of the inputs: same inputs always yield the same code,
severity score, and class band.  No model calls, no I/O.

Scoring model (integer points, additive):

    dimension      mildest → most severe
    -------------- --------------------------------------
    security       none=0  X=0  Y=2  Z=4  Z+=6
    traffic        FREE_FLOW=0  HEAVY=2  CONGESTED=4  GRIDLOCK=6
    weather        CLEAR=0  RAIN=1  FOG=2  STORM=3
    crowd          LOW=0  MEDIUM=1  HIGH=2  EXTREME=3
    anomaly flag   +2 when the corridor anomaly feed is active
    event flag     +1 when the event calendar has an active event

Score range 0..21.  Bands:

    GREEN   0..4    AMBER 5..9    RED 10..14    BLACK 15..21

Because every dimension contributes a non-negative amount that is
monotone in its severity rank, the band is monotone in every input —
worsening any single dimension can never soften the band.  The Z+ tier
scores ≥ 6 on security alone, so a Z+ movement is never classified
below AMBER.  Public events (protection class 'none') score 0 on
security, exactly like X — their weight arrives through the crowd and
event dimensions instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from traffic_oracle.adaptive.enums import (
    CrowdDensity,
    SecurityTier,
    SituationClass,
    TrafficRegime,
    WeatherCondition,
)
from traffic_oracle.adaptive.inputs import SituationInputs
from traffic_oracle.adaptive.protocol_params_gen import SECURITY_POINTS as _GEN_SECURITY_POINTS

# ---------------------------------------------------------------------------
# Point tables (exhaustive over enum members)
# ---------------------------------------------------------------------------

#: Severity points per protection class, sourced from the canonical
#: protocol-params JSON via the generated module (contract C1).  'none'
#: (public/civic corridor event) scores 0, same as X.
SECURITY_POINTS: dict[SecurityTier, int] = {
    SecurityTier.from_code(code): int(points)
    for code, points in _GEN_SECURITY_POINTS.items()
}
TRAFFIC_POINTS: dict[TrafficRegime, int] = {
    TrafficRegime.FREE_FLOW: 0,
    TrafficRegime.HEAVY: 2,
    TrafficRegime.CONGESTED: 4,
    TrafficRegime.GRIDLOCK: 6,
}
WEATHER_POINTS: dict[WeatherCondition, int] = {
    WeatherCondition.CLEAR: 0,
    WeatherCondition.RAIN: 1,
    WeatherCondition.FOG: 2,
    WeatherCondition.STORM: 3,
}
CROWD_POINTS: dict[CrowdDensity, int] = {
    CrowdDensity.LOW: 0,
    CrowdDensity.MEDIUM: 1,
    CrowdDensity.HIGH: 2,
    CrowdDensity.EXTREME: 3,
}

ANOMALY_POINTS = 2
EVENT_POINTS = 1

# Band thresholds on the total score: GREEN [0,5), AMBER [5,10),
# RED [10,15), BLACK [15, ∞).
BAND_LOWER_BOUNDS: tuple[tuple[int, SituationClass], ...] = (
    (0, SituationClass.GREEN),
    (5, SituationClass.AMBER),
    (10, SituationClass.RED),
    (15, SituationClass.BLACK),
)

MAX_SEVERITY_SCORE = (
    max(SECURITY_POINTS.values())
    + max(TRAFFIC_POINTS.values())
    + max(WEATHER_POINTS.values())
    + max(CROWD_POINTS.values())
    + ANOMALY_POINTS
    + EVENT_POINTS
)


def band_from_score(score: int) -> SituationClass:
    """Map an integer severity score to its band (monotone step function)."""
    if score < 0:
        raise ValueError(f"severity score must be >= 0, got {score}")
    result = BAND_LOWER_BOUNDS[0][1]
    for lower, band in BAND_LOWER_BOUNDS:
        if score >= lower:
            result = band
    return result


@dataclass(frozen=True)
class Situation:
    """Immutable classification result."""

    code: str                 # e.g. "Z+:GRIDLOCK:STORM:EXTREME"
    situation_class: SituationClass
    severity_score: int
    inputs: SituationInputs

    def to_dict(self) -> dict:
        return {
            "situation_code": self.code,
            "situation_class": self.situation_class.code,
            "severity_score": self.severity_score,
            "inputs": self.inputs.to_dict(),
        }


class SituationEngine:
    """Stateless classifier: ``SituationInputs`` → ``Situation``.

    Kept as a class (rather than a bare function) so the startup hook can
    instantiate and expose one engine, and so future input validation can
    be attached without touching call sites.
    """

    def classify(self, inputs: SituationInputs) -> Situation:
        score = (
            SECURITY_POINTS[inputs.vvip_class]
            + TRAFFIC_POINTS[inputs.traffic_regime]
            + WEATHER_POINTS[inputs.weather]
            + CROWD_POINTS[inputs.crowd]
            + (ANOMALY_POINTS if inputs.anomaly_active else 0)
            + (EVENT_POINTS if inputs.event_active else 0)
        )
        code = ":".join(
            (
                inputs.vvip_class.code,
                inputs.traffic_regime.code,
                inputs.weather.code,
                inputs.crowd.code,
            )
        )
        return Situation(
            code=code,
            situation_class=band_from_score(score),
            severity_score=score,
            inputs=inputs,
        )

    def classify_scores(self, **kwargs: object) -> Situation:
        """Convenience wrapper accepting raw scores (see SituationInputs.from_scores)."""
        return self.classify(SituationInputs.from_scores(**kwargs))  # type: ignore[arg-type]


def all_input_combinations() -> list[SituationInputs]:
    """Enumerate the full input product space (5·4·4·4·2·2 = 1280).

    Used by the exhaustive unit tests; exposed as a module function so
    other streams can sanity-check coverage without re-deriving it.
    """
    combos: list[SituationInputs] = []
    for sec in SecurityTier:
        for traffic in TrafficRegime:
            for weather in WeatherCondition:
                for crowd in CrowdDensity:
                    for anomaly in (False, True):
                        for event in (False, True):
                            combos.append(
                                SituationInputs(
                                    vvip_class=sec,
                                    traffic_regime=traffic,
                                    weather=weather,
                                    crowd=crowd,
                                    anomaly_active=anomaly,
                                    event_active=event,
                                )
                            )
    return combos


__all__ = [
    "ANOMALY_POINTS",
    "BAND_LOWER_BOUNDS",
    "CROWD_POINTS",
    "EVENT_POINTS",
    "MAX_SEVERITY_SCORE",
    "SECURITY_POINTS",
    "Situation",
    "SituationEngine",
    "SituationInputs",
    "TRAFFIC_POINTS",
    "WEATHER_POINTS",
    "all_input_combinations",
    "band_from_score",
]
