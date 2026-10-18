"""Raw numeric inputs → ranked enums (deterministic threshold mapping).

The adaptive layer consumes scores produced by T3 numeric tools (DSTGAT
congestion forecasts, NATS ``corridor.traffic.anomaly`` flags, weather
and crowd feeds) — never text.  This module owns every threshold used to
turn those scores into the enum levels the situation classifier consumes.

Threshold semantics: lower bounds are inclusive at each boundary
(``value < bound`` falls in the previous bucket, ``value == bound``
falls in the bucket starting at ``bound``).
"""

from __future__ import annotations

from dataclasses import dataclass

from traffic_oracle.adaptive.enums import (
    CrowdDensity,
    SecurityTier,
    TrafficRegime,
    WeatherCondition,
)

# ---------------------------------------------------------------------------
# Thresholds (module constants so tests can pin them)
# ---------------------------------------------------------------------------

# Predicted congestion index → traffic regime.  An *active* anomaly flag
# forces at least CONGESTED regardless of the numeric forecast.
TRAFFIC_REGIME_BOUNDS: tuple[tuple[float, TrafficRegime], ...] = (
    (0.35, TrafficRegime.FREE_FLOW),
    (0.60, TrafficRegime.HEAVY),
    (0.85, TrafficRegime.CONGESTED),
    (float("inf"), TrafficRegime.GRIDLOCK),
)

# Weather severity score [0, 1] → condition.
WEATHER_BOUNDS: tuple[tuple[float, WeatherCondition], ...] = (
    (0.25, WeatherCondition.CLEAR),
    (0.50, WeatherCondition.RAIN),
    (0.75, WeatherCondition.FOG),
    (float("inf"), WeatherCondition.STORM),
)

# Crowd density score [0, 1] → level.
CROWD_BOUNDS: tuple[tuple[float, CrowdDensity], ...] = (
    (0.25, CrowdDensity.LOW),
    (0.50, CrowdDensity.MEDIUM),
    (0.75, CrowdDensity.HIGH),
    (float("inf"), CrowdDensity.EXTREME),
)

# Public-event duration classes (carried metadata for event profiles —
# procession/multi-week work zones vs a minutes-scale protected movement).
# Deliberately NOT part of the severity score: the VVIP base case must
# classify identically with or without these fields set.
DURATION_CLASSES: tuple[str, ...] = ("minutes", "hours", "multi_day", "multi_week")


def _bucket(value: float, bounds: tuple[tuple[float, object], ...]) -> object:
    """Return the first bucket whose upper bound (exclusive) covers value."""
    if value < 0.0:
        value = 0.0
    for upper, member in bounds:
        if value < upper:
            return member
    return bounds[-1][1]  # pragma: no cover - inf bounds make this unreachable


def traffic_regime_from_congestion(
    congestion_idx: float, anomaly_active: bool = False
) -> TrafficRegime:
    """Map a predicted congestion index [0,1] to a TrafficRegime.

    An active corridor anomaly (NATS ``corridor.traffic.anomaly``) floors
    the regime at CONGESTED: anomalies matter even when the point forecast
    has not caught up.
    """
    if congestion_idx < 0.0:
        congestion_idx = 0.0
    elif congestion_idx > 1.0:
        congestion_idx = 1.0
    regime = _bucket(congestion_idx, TRAFFIC_REGIME_BOUNDS)
    assert isinstance(regime, TrafficRegime)
    if anomaly_active and regime.rank < TrafficRegime.CONGESTED.rank:
        return TrafficRegime.CONGESTED
    return regime


def weather_from_severity(severity: float) -> WeatherCondition:
    """Map a weather severity score [0,1] to a WeatherCondition."""
    return _bucket(severity, WEATHER_BOUNDS)  # type: ignore[return-value]


def crowd_from_score(score: float) -> CrowdDensity:
    """Map a crowd density score [0,1] to a CrowdDensity level."""
    return _bucket(score, CROWD_BOUNDS)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Situation inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SituationInputs:
    """Fully-resolved inputs to the situation classifier.

    ``to_dict``/``from_dict`` use only scalars and enum codes so the
    structure can cross the Valkey/observe-endpoint boundary losslessly.

    Public-event profile fields (event-agnostic extension, additive):
      * ``crowd_class`` — explicit CrowdDensity carried by public-event
        profiles (a Rath-Yatra-class feed knows it is EXTREME without a
        numeric score).  When set it OVERRIDES the score-derived crowd
        level; when None the legacy score derivation applies unchanged.
      * ``duration_class`` — 'minutes' | 'hours' | 'multi_day' |
        'multi_week' (see DURATION_CLASSES).  Metadata only: neither field
        contributes to the severity score, so the VVIP base case
        (crowd_class=None, duration_class='minutes') classifies identically
        to the pre-generalization behavior.
    """

    vvip_class: SecurityTier
    traffic_regime: TrafficRegime
    weather: WeatherCondition
    crowd: CrowdDensity
    anomaly_active: bool = False
    event_active: bool = False
    crowd_class: CrowdDensity | None = None
    duration_class: str = "minutes"

    def to_dict(self) -> dict:
        return {
            "vvip_class": self.vvip_class.code,
            "traffic_regime": self.traffic_regime.code,
            "weather": self.weather.code,
            "crowd": self.crowd.code,
            "anomaly_active": self.anomaly_active,
            "event_active": self.event_active,
            "crowd_class": self.crowd_class.code if self.crowd_class else None,
            "duration_class": self.duration_class,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> SituationInputs:
        crowd_class_raw = raw.get("crowd_class")
        return cls(
            vvip_class=SecurityTier.from_code(raw["vvip_class"]),
            traffic_regime=TrafficRegime.from_code(raw["traffic_regime"]),
            weather=WeatherCondition.from_code(raw["weather"]),
            crowd=CrowdDensity.from_code(raw["crowd"]),
            anomaly_active=bool(raw.get("anomaly_active", False)),
            event_active=bool(raw.get("event_active", False)),
            crowd_class=(
                CrowdDensity.from_code(crowd_class_raw)
                if crowd_class_raw
                else None
            ),
            duration_class=str(raw.get("duration_class", "minutes")),
        )

    @classmethod
    def from_scores(
        cls,
        vvip_class: str,
        predicted_congestion_idx: float,
        weather_severity: float = 0.0,
        crowd_score: float = 0.0,
        anomaly_active: bool = False,
        event_active: bool = False,
        weather: str | WeatherCondition | None = None,
        crowd_class: str | CrowdDensity | None = None,
        duration_class: str = "minutes",
    ) -> SituationInputs:
        """Convenience constructor from raw numeric feed scores.

        ``weather`` may be passed as an explicit code ("STORM"...) for
        feeds that already classify; otherwise it is derived from
        ``weather_severity``.  ``crowd_class`` may be passed as an explicit
        code by public-event profile feeds; when given it overrides the
        score-derived crowd level (the score stays the fallback).
        """
        weather_value: WeatherCondition
        if weather is not None:
            weather_value = (
                weather
                if isinstance(weather, WeatherCondition)
                else WeatherCondition.from_code(weather)
            )
        else:
            weather_value = weather_from_severity(weather_severity)
        crowd_value: CrowdDensity
        if crowd_class is not None:
            crowd_value = (
                crowd_class
                if isinstance(crowd_class, CrowdDensity)
                else CrowdDensity.from_code(crowd_class)
            )
        else:
            crowd_value = crowd_from_score(crowd_score)
        return cls(
            vvip_class=SecurityTier.from_code(vvip_class),
            traffic_regime=traffic_regime_from_congestion(
                predicted_congestion_idx, anomaly_active
            ),
            weather=weather_value,
            crowd=crowd_value,
            anomaly_active=anomaly_active,
            event_active=event_active,
            crowd_class=crowd_value if crowd_class is not None else None,
            duration_class=duration_class,
        )


@dataclass(frozen=True)
class EscalationInputs:
    """Side inputs for the escalation engine (not part of classification)."""

    last_gps_age_sec: float = 0.0
    ambulance_delay_projection_sec: float = 0.0
    red_sustained_sec: float = 0.0
    shadow_eval_failed: bool = False
    decider_checkpoints_paused: bool = False
    crowd_extreme: bool = False

    def to_dict(self) -> dict:
        return {
            "last_gps_age_sec": self.last_gps_age_sec,
            "ambulance_delay_projection_sec": self.ambulance_delay_projection_sec,
            "red_sustained_sec": self.red_sustained_sec,
            "shadow_eval_failed": self.shadow_eval_failed,
            "decider_checkpoints_paused": self.decider_checkpoints_paused,
            "crowd_extreme": self.crowd_extreme,
        }


def round_trip(inputs: SituationInputs) -> SituationInputs:
    """Lossless dict round-trip helper (used by tests and the observe API)."""
    return SituationInputs.from_dict(inputs.to_dict())


__all__ = [
    "CROWD_BOUNDS",
    "DURATION_CLASSES",
    "EscalationInputs",
    "SituationInputs",
    "TRAFFIC_REGIME_BOUNDS",
    "WEATHER_BOUNDS",
    "crowd_from_score",
    "round_trip",
    "traffic_regime_from_congestion",
    "weather_from_severity",
]
