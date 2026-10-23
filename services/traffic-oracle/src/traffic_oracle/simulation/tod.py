"""Time-of-day, day-of-week, and seasonal realism for the twin.

Pure functions of a :class:`datetime.datetime` (plus scenario weather) →
demand/speed multipliers, so they are unit-testable and identical inside the
engine, the scenario library, and the replay calibrator.

Empirical shape (labelled assumptions, Ahmedabad urban-corridor flavoured):
    - Two superposed commuter peaks: morning ~09:00, evening ~18:30.
    - Evening peak heavier and wider than morning (office + commercial + market
      return trips); lunch shoulder ~13:30.
    - Weekday > Saturday > Sunday; Sunday peaks shift later and flatten.
    - Friday evening extends later (market / social night traffic).
    - Monsoon (Jun 15 – Sep 15): free-flow speed damped, waterlogging-prone.
    - Winter fog (mid-Dec – mid-Jan, 06:30–09:00): visibility speed cap.

Time zone: every curve here is an Ahmedabad LOCAL-time shape, so every
instant is normalised with :func:`to_ist` (``astimezone(Asia/Kolkata)``;
naive datetimes are taken as IST) before its hour, weekday or date is
read.  The same instant written as ``…T03:30Z`` or ``…T09:00+05:30``
therefore yields identical demand, weather and twin metrics.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

#: Asia/Kolkata — a fixed +05:30 offset (no DST), so no tz database needed.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def to_ist(dt: datetime) -> datetime:
    """The same instant in Asia/Kolkata local time (naive = already IST)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)

# Monday == 0 ... Sunday == 6 (Python convention).

# Peak shapes: (centre_hour, sigma_hours, amplitude above base)
_WEEKDAY_PEAKS = (
    (9.0, 1.7, 0.72),    # morning commuter peak
    (13.4, 1.1, 0.16),   # lunch shoulder
    (18.5, 2.1, 0.85),   # evening peak — heavier, wider
)
_SUNDAY_PEAKS = (
    (11.2, 1.9, 0.42),   # late-brunch church/temple outings
    (19.3, 2.3, 0.55),   # evening family outing
)
_SATURDAY_PEAKS = (
    (10.6, 1.8, 0.48),
    (19.0, 2.4, 0.68),
)

_BASE_DEMAND = 0.30  # 03:00 trough relative to peak capacity-driven demand
_WEEKEND_SCALE = {5: 0.86, 6: 0.68}  # Saturday, Sunday vs weekday (0-based dow)
_FRIDAY_LATE_BOOST_HOURS = (21, 22, 23)  # Friday evening lingers

# Monsoon window (day-of-year bounds, inclusive)
_MONSOON_START = date(2001, 6, 15).timetuple().tm_yday   # 166
_MONSOON_END = date(2001, 9, 15).timetuple().tm_yday     # 258
# Fog window: mid-Dec to mid-Jan (wraps year end)
_FOG_START = date(2001, 12, 10).timetuple().tm_yday      # 344
_FOG_END = date(2001, 1, 15).timetuple().tm_yday         # 15


@dataclass(frozen=True)
class WeatherState:
    """Scenario weather; monsoon/fog derive from the date when not forced."""

    monsoon: bool = False
    rain_intensity_mm_h: float = 0.0   # 0 (dry) .. 60 (cloudburst)
    fog: bool = False                  # visibility-driven speed cap


def is_monsoon_day(d: date) -> bool:
    """Monsoon season flag (Jun 15 – Sep 15, labelled assumption)."""
    doy = d.timetuple().tm_yday
    return _MONSOON_START <= doy <= _MONSOON_END


def is_fog_day(d: date) -> bool:
    """Winter fog-prone window (Dec 10 – Jan 15, wraps the year end)."""
    doy = d.timetuple().tm_yday
    return doy >= _FOG_START or doy <= _FOG_END


def infer_weather(d: date, hour: int) -> WeatherState:
    """Default weather for an IST date/hour when the scenario forces nothing.

    Fog only applies in the early-morning window; monsoon is all-day.
    Callers holding an instant use :func:`infer_weather_at`.
    """
    fog = is_fog_day(d) and 6 <= hour <= 9
    return WeatherState(monsoon=is_monsoon_day(d), fog=fog)


def infer_weather_at(dt: datetime) -> WeatherState:
    """Default weather at an instant (normalised to IST first)."""
    local = to_ist(dt)
    return infer_weather(local.date(), local.hour)


def hour_demand_multiplier(hour: int, dow: int) -> float:
    """Relative demand multiplier for an hour-of-week.

    Returns a value in roughly [0.10, 1.0] where 1.0 is the busiest hour of
    the busiest weekday. Pure function; same inputs → same output always.
    """
    hour_f = float(hour)
    if dow == 6:
        peaks, scale = _SUNDAY_PEAKS, _WEEKEND_SCALE[6]
    elif dow == 5:
        peaks, scale = _SATURDAY_PEAKS, _WEEKEND_SCALE[5]
    else:
        peaks, scale = _WEEKDAY_PEAKS, 1.0

    m = _BASE_DEMAND
    for centre, sigma, amp in peaks:
        m += amp * math.exp(-((hour_f - centre) ** 2) / (2.0 * sigma ** 2))
    # Overnight decay: after 23:00 demand slides to the trough.
    if hour >= 23:
        m *= 0.75
    if hour <= 4:
        m *= 0.55
    # Friday night extension.
    if dow == 4 and hour in _FRIDAY_LATE_BOOST_HOURS:
        m *= 1.18
    return round(min(m * scale, 1.0), 4)


def time_of_day_demand(dt: datetime) -> float:
    """Demand multiplier at an instant, linearly interpolated between hours.

    The instant is read in IST (:func:`to_ist`) — the curve is local time.
    """
    dt = to_ist(dt)
    h0 = dt.hour
    frac = dt.minute / 60.0 + dt.second / 3600.0
    m0 = hour_demand_multiplier(h0, dt.weekday())
    h1 = (h0 + 1) % 24
    m1 = hour_demand_multiplier(h1, dt.weekday() if h0 != 23 else (dt.weekday() + 1) % 7)
    return m0 + (m1 - m0) * frac


def weather_speed_factor(weather: WeatherState, rain_mm_h: float | None = None) -> float:
    """Free-flow speed multiplier under weather (1.0 = clear).

    Rain effect saturates: 5 mm/h ≈ −12%, 30 mm/h ≈ −32%, cloudburst caps at
    −45%. Monsoon base dampening applies even between showers (surface spray,
    potholes). Fog caps urban free-flow at ~55% until it burns off.
    """
    rain = weather.rain_intensity_mm_h if rain_mm_h is None else rain_mm_h
    f = 1.0
    if weather.monsoon:
        f -= 0.08
    if rain > 0:
        f -= 0.12 * (1.0 - math.exp(-rain / 12.0)) * 2.0  # saturating at ~-24% extra
    if weather.fog:
        f = min(f, 0.55)
    return max(f, 0.35)


def waterlogging_rate_multiplier(weather: WeatherState) -> float:
    """Event-rate multiplier for waterlogging events.

    Dry season: waterlogging essentially never happens (0.02×). Active monsoon
    with rain scales up to ~8× the base underpass rate.
    """
    if not weather.monsoon:
        return 0.02
    rain = weather.rain_intensity_mm_h
    return 2.0 + 6.0 * (1.0 - math.exp(-rain / 15.0))


def daylight_speed_relief(dt: datetime) -> float:
    """Minor free-flow relief in the dead of night (less friction, no parked
    cars in the curb lane): +8% between 00:00 and 05:00 IST."""
    return 1.08 if to_ist(dt).hour < 5 else 1.0
