"""Use-case emulation profiles — the 8 archetypes (emulatorContract).

A profile is pure cause-side interpretation of a UseCaseLibrary config: the
event_kind archetype, the crowd script, the weather onset ramp, any
profile-added scheduled causes (EMERGENCY_VEHICLE green-corridor requests,
the base case's planted breakdown), and the ambulance-delay projection the
escalation engine reads.  Profiles NEVER select code paths in the real stack
and never carry outcomes — only exogenous cause shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["UseCaseProfile", "PROFILES", "EVENT_KINDS"]

# The archetypes (domain-model axis 3); they select closure-policy and
# dossier TEMPLATES downstream, never emulator code paths.  crime_incident
# and vehicle_pursuit are the watchlist-anpr additions (contract C3
# lockstep: migration 010's CHECK relaxation + traffic_oracle
# events.EVENT_KINDS mirror this tuple exactly).
EVENT_KINDS = (
    "protected_movement",
    "procession",
    "mass_gathering",
    "race",
    "ceremonial",
    "egress_surge",
    "emergency_evacuation",
    "work_zone",
    # watchlist-anpr additions (010, C3):
    "crime_incident",
    "vehicle_pursuit",
)


@dataclass(frozen=True)
class UseCaseProfile:
    """Cause-side interpretation of one use case."""

    event_kind: str
    crowd_class: str                       # LOW | MEDIUM | HIGH | EXTREME
    crowd_waypoints: tuple[tuple[float, float], ...]   # (t_sec, score 0..1)
    weather_onset_sec: float = 0.0         # ramp-up time to steady severity
    weather_steady_severity: float = 0.0
    weather_code: str | None = None        # explicit code key (C3), or None
    # profile-added scheduled causes beyond the config's forced_events
    extra_events: tuple[dict, ...] = field(default_factory=tuple)
    # ambulance delay projection (escalation side-input) while an
    # EMERGENCY_VEHICLE window is active: decays base → cleared
    ambulance_base_sec: float = 0.0
    ambulance_cleared_sec: float = 0.0
    # ── watchlist knobs (watchlist-anpr stream, contract C2) ────────────
    # Drive the tape's anpr_read / agency_unit generators: how many ANPR
    # cameras watch the route, how much background traffic they read, and
    # whether the scenario plants a stolen-plate hit and/or an
    # earmarked-plate hit among the reads.  Causes only — hits label
    # generator-known plates, they never fabricate outcomes.
    anpr_cameras: int = 0
    anpr_background_vehicles: int = 0
    anpr_stolen_plate_hit: bool = False
    anpr_earmarked_plate_hit: bool = False
    agency_units: int = 0


_BASE_EV = {
    "type": "emergency_vehicle",
    "severity": 0.9,
}

PROFILES: dict[str, UseCaseProfile] = {
    # BASE CASE — Z+ run, LOW crowd, one planted breakdown (contract).
    "vvip_convoy_base": UseCaseProfile(
        event_kind="protected_movement",
        crowd_class="LOW",
        crowd_waypoints=((0, 0.08), (900, 0.15), (1800, 0.12), (2700, 0.10)),
        extra_events=(
            {
                "type": "vehicle_breakdown",
                "segment_id": 1024,
                "start_sec": 600,
                "duration_sec": 900,
                "severity": 0.6,
                "label": "planted breakdown — tanker stalled on GN Highway (base-case cause)",
            },
        ),
    ),
    # Rath Yatra — procession + EXTREME crowd script + market overrides ride
    # in the config itself (event_overrides.market = 2.5).
    "rath_yatra_ahmedabad": UseCaseProfile(
        event_kind="procession",
        crowd_class="EXTREME",
        crowd_waypoints=((0, 0.55), (1200, 0.95), (2400, 0.92), (3600, 0.88)),
    ),
    # FLAGSHIP multi-event composition — the Rath Yatra morning (procession,
    # EXTREME crowd building through the window) + a monsoon burst (18 mm/h
    # -> severity 0.6 with a 300 s onset ramp, RAIN code) + one siren: the
    # 108 cardiac ambulance's green-corridor windows ride the config's
    # forced_events (emergency_vehicle on all four route segments); the
    # ambulance-delay projection decays base -> cleared while a window runs
    # (the escalation channel the engine reads).
    "rath_yatra_monsoon_ambulance": UseCaseProfile(
        event_kind="procession",
        crowd_class="EXTREME",
        crowd_waypoints=((0, 0.55), (1500, 0.92), (2400, 0.90), (3600, 0.86)),
        weather_onset_sec=300.0,
        weather_steady_severity=0.6,      # 18 mm/h -> min(1, 18/30)
        weather_code="RAIN",
        ambulance_base_sec=120.0,
        ambulance_cleared_sec=15.0,
    ),
    # Kumbh — perimeter phases, staged ingress, EXTREME pre-dawn crowd.
    "kumbh_scale_gathering": UseCaseProfile(
        event_kind="mass_gathering",
        crowd_class="EXTREME",
        crowd_waypoints=((0, 0.85), (1800, 0.90), (3600, 0.85)),
    ),
    # Marathon — staged crossings, rolling reopen behind the sweep.
    "riverfront_marathon": UseCaseProfile(
        event_kind="race",
        crowd_class="HIGH",
        crowd_waypoints=((0, 0.60), (600, 0.72), (2400, 0.50), (3600, 0.30)),
    ),
    # Republic Day — Z tier ceremonial strip, spectator saturation on the strip.
    "national_day_parade": UseCaseProfile(
        event_kind="ceremonial",
        crowd_class="HIGH",
        crowd_waypoints=((0, 0.50), (900, 0.65), (1800, 0.55), (3600, 0.40)),
    ),
    # Motera — night egress surge + ambulance green-corridor request fighting
    # the dispersal wave (the showcase's emergency channel).
    "motera_stadium_egress": UseCaseProfile(
        event_kind="egress_surge",
        crowd_class="EXTREME",
        crowd_waypoints=((0, 0.75), (600, 0.95), (2400, 0.62), (3600, 0.35)),
        extra_events=(
            {
                **_BASE_EV,
                "segment_id": 1032,
                "start_sec": 300,
                "duration_sec": 1200,
                "label": "green-corridor request — ambulance through the dispersal wave",
            },
            {
                **_BASE_EV,
                "segment_id": 1002,
                "start_sec": 1500,
                "duration_sec": 900,
                "severity": 0.8,
                "label": "second green-corridor request — medical escort down Ashram spine",
            },
        ),
        ambulance_base_sec=120.0,
        ambulance_cleared_sec=15.0,
    ),
    # Flood — weather ramp + relief column EMERGENCY_VEHICLE causes +
    # ambulance_delay_projection inputs (flood relief per C4: priority
    # sub-event rows + scheduled EMERGENCY_VEHICLE + projection inputs).
    "flood_evacuation_corridor": UseCaseProfile(
        event_kind="emergency_evacuation",
        crowd_class="MEDIUM",
        crowd_waypoints=((0, 0.35), (1800, 0.50), (3600, 0.45)),
        weather_onset_sec=900.0,
        weather_steady_severity=0.73,      # 22 mm/h → min(1, 22/30)
        weather_code="RAIN",
        extra_events=(
            {
                **_BASE_EV,
                "segment_id": 1040,
                "start_sec": 0,
                "duration_sec": 3600,
                "severity": 0.95,
                "label": "NDRF relief column — reverse-flow evacuation spine",
            },
            {
                **_BASE_EV,
                "segment_id": 1021,
                "start_sec": 1200,
                "duration_sec": 1800,
                "severity": 0.8,
                "label": "boat-rescue ambulance extraction onto SP Ring Road",
            },
        ),
        ambulance_base_sec=120.0,
        ambulance_cleared_sec=20.0,
    ),
    # Work zone — published multi-week lane loss; the run window sees the
    # standing pinch plus diverted bazaar traffic (both in forced_events).
    "metro_work_zone": UseCaseProfile(
        event_kind="work_zone",
        crowd_class="LOW",
        crowd_waypoints=((0, 0.18), (1800, 0.22), (3600, 0.20)),
    ),
    # WATCHLIST (watchlist-anpr stream) — a stolen-vehicle FIR drives an
    # emulated ANPR camera sweep of the old-city belt.  The corridor event
    # this animates is the CRIME INCIDENT itself (public/civic operation,
    # protection_class 'none'); the camera reads and agency-unit patrols
    # ride the tape's anpr_read / agency_unit frames (contract C2) with a
    # stolen-plate hit and an earmarked-plate hit included.
    "watchlist_anpr_sweep": UseCaseProfile(
        event_kind="crime_incident",
        crowd_class="LOW",
        crowd_waypoints=((0, 0.15), (1200, 0.28), (2700, 0.35), (3600, 0.25)),
        extra_events=(
            {
                "type": "rally_spillover",
                "segment_id": 1012,
                "start_sec": 900,
                "duration_sec": 1200,
                "extra_demand_veh_h": 420,
                "severity": 0.5,
                "label": "bazaar spill onto Jamalpur approach during the sweep",
            },
        ),
        # watchlist knobs (consumed by the tape's anpr_read generator)
        anpr_cameras=4,
        anpr_background_vehicles=6,
        anpr_stolen_plate_hit=True,
        anpr_earmarked_plate_hit=True,
        agency_units=2,
    ),
}


def profile_for(use_case_id: str) -> UseCaseProfile:
    try:
        return PROFILES[use_case_id]
    except KeyError as exc:
        raise KeyError(
            f"no emulation profile for use case {use_case_id!r} — "
            f"known: {sorted(PROFILES)}"
        ) from exc


# Event kinds that are PUBLIC operations (no protectee) → protection_class
# 'none' in events.corridor_events (007 semantics: 'none' = public event);
# protected_movement / ceremonial keep the config's vvip_class (Z+ / Z).
# The watchlist kinds (010) are government/civic operations with no
# protectee — 'none' as well.
_PUBLIC_EVENT_KINDS = frozenset(
    {"procession", "mass_gathering", "race", "egress_surge",
     "emergency_evacuation", "work_zone",
     "crime_incident", "vehicle_pursuit"}
)


def protection_class_for(profile: UseCaseProfile, vvip_class: str) -> str:
    if profile.event_kind in _PUBLIC_EVENT_KINDS:
        return "none"
    return vvip_class
