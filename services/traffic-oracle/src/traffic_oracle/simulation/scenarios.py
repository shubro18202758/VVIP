"""Named scenario library for the VVIP corridor twin.

Fourteen seedable, reproducible scenarios on the repo's own Ahmedabad seed
network (same segment IDs the DB and the frozen impact harness
``scripts/impact_run.py`` parse from ``sample_corridor.sql``). Each scenario
fixes: route, departure clock, VVIP class, closure policy, horizon, weather,
event-rate overrides, and any schedule-forced events (rallies, planted
breakdowns). Every run of a scenario with the same seed reproduces
bit-identical metrics.

The three mandated anchors are present:
    - ``monsoon_evening_peak``          (monsoon evening)
    - ``multi_event_collision_kalupur`` (multi-event collision)
    - ``zplus_airport_morning_peak``    (Z+ at peak)

Consumption contract for the impact harness: ``export_library()`` /
``python -m traffic_oracle.simulation export`` emits
``VVIP/scripts/scenario_library.json`` whose ``network`` block carries the
same segment/lane/speed facts the harness parses from the seed and whose
``scenarios[].closure_policy`` / demand parameters map onto
``impact_run.commuter_delay_min(hold_min, lambda_per_min, n_junctions)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from traffic_oracle.simulation.events import EventConfig, EventType, ScheduledEvent
from traffic_oracle.simulation.network import default_network
from traffic_oracle.simulation.tod import WeatherState


@dataclass(frozen=True)
class ScenarioSpec:
    """One named, reproducible scenario of the corridor twin."""

    scenario_id: str
    name: str
    description: str
    category: str                     # baseline | weather | event | multi_event | ops
    route_segment_ids: tuple[int, ...]
    departure_time: str               # ISO-8601 (local, IST offset in the stamp)
    vvip_class: str                   # 'Z+' | 'Z' | 'Y' | 'X' | 'none'
    closure_policy: dict
    horizon_min: int
    n_replications: int
    seed: int
    convoy_speed_kmh: float = 60.0
    weather: dict = field(default_factory=dict)
    event_overrides: dict = field(default_factory=dict)
    forced_events: tuple[dict, ...] = ()
    expected_features: tuple[str, ...] = ()
    # Event-agnostic extension (additive; defaults keep every pre-existing
    # scenario byte-identical in behavior):
    event_kind: str = "protected_movement"   # one of the 8 archetypes
    crowd_class: str = "LOW"                 # LOW | MEDIUM | HIGH | EXTREME
    duration_class: str = "minutes"          # minutes | hours | multi_day | multi_week

    # ── derived views ─────────────────────────────────────────────────
    def departure_dt(self) -> datetime:
        return datetime.fromisoformat(self.departure_time)

    def weather_state(self) -> WeatherState:
        return WeatherState(
            monsoon=bool(self.weather.get("monsoon", False)),
            rain_intensity_mm_h=float(self.weather.get("rain_intensity_mm_h", 0.0)),
            fog=bool(self.weather.get("fog", False)),
        )

    def event_config(self) -> EventConfig:
        return EventConfig(
            breakdown_rate_multiplier=float(self.event_overrides.get("breakdown", 1.0)),
            waterlogging_rate_multiplier=float(self.event_overrides.get("waterlogging", 1.0)),
            signal_fault_rate_multiplier=float(self.event_overrides.get("signal_fault", 1.0)),
            market_rate_multiplier=float(self.event_overrides.get("market", 1.0)),
            rally_rate_multiplier=float(self.event_overrides.get("rally", 0.0)),
            # Sixth override (007 / contract C4): ambulance Poisson knob.
            ambulance_rate_multiplier=float(self.event_overrides.get("ambulance", 1.0)),
        )

    def scheduled_events(self) -> list[ScheduledEvent]:
        out: list[ScheduledEvent] = []
        for fe in self.forced_events:
            out.append(
                ScheduledEvent(
                    event_type=EventType(fe["type"]),
                    segment_id=int(fe["segment_id"]),
                    start_sec=float(fe["start_sec"]),
                    duration_sec=float(fe["duration_sec"]),
                    severity=float(fe.get("severity", 0.7)),
                    label=str(fe.get("label", "")),
                    extra_demand_veh_h=float(fe.get("extra_demand_veh_h", 0.0)) or 0.0,
                )
            )
        return out

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "route_segment_ids": list(self.route_segment_ids),
            "departure_time": self.departure_time,
            "vvip_class": self.vvip_class,
            "closure_policy": dict(self.closure_policy),
            "horizon_min": self.horizon_min,
            "n_replications": self.n_replications,
            "seed": self.seed,
            "convoy_speed_kmh": self.convoy_speed_kmh,
            "weather": dict(self.weather),
            "event_overrides": dict(self.event_overrides),
            "forced_events": [dict(e) for e in self.forced_events],
            "expected_features": list(self.expected_features),
            "event_kind": self.event_kind,
            "crowd_class": self.crowd_class,
            "duration_class": self.duration_class,
        }


# ── The library ────────────────────────────────────────────────────────────

SCENARIO_LIBRARY: dict[str, ScenarioSpec] = {}


def _register(s: ScenarioSpec) -> ScenarioSpec:
    if s.scenario_id in SCENARIO_LIBRARY:
        raise ValueError(f"duplicate scenario_id {s.scenario_id!r}")
    SCENARIO_LIBRARY[s.scenario_id] = s
    return s


# Ashram Road spine used by several scenarios (J2 → J1 → J7 northbound).
_ASHRAM_SPINE = (1001, 1002, 1009)

_register(ScenarioSpec(
    scenario_id="baseline_weekday_midday",
    name="Baseline Weekday Midday",
    description=(
        "Z-class movement up the Ashram Road spine on a quiet Tuesday early "
        "afternoon — the reference case: default event rates, no weather, "
        "standard 120 s rolling closure."
    ),
    category="baseline",
    route_segment_ids=_ASHRAM_SPINE,
    departure_time="2026-10-13T13:40:00+05:30",
    vvip_class="Z",
    closure_policy={"advance_closure_sec": 120, "closure_type": "full_closure"},
    horizon_min=45,
    n_replications=8,
    seed=20261013,
    expected_features=("moderate_demand", "rolling_closure", "reference_case"),
))

_register(ScenarioSpec(
    scenario_id="monsoon_evening_peak",
    name="Monsoon Evening Peak",
    description=(
        "MANDATED ANCHOR — monsoon evening. Z+ airport arrival into the city "
        "at 18:30 in active monsoon (18 mm/h rain): Airport Road into "
        "Shahibaug, Dr. Ambedkar Road and Relief Road to the Kalupur station "
        "approach (1014) — its "
        "scheduled waterlogging plus evening-peak demand and weather-damped "
        "free-flow make this the worst single-weather case."
    ),
    category="weather",
    route_segment_ids=(1028, 1027, 1043, 1005, 1014),
    departure_time="2026-07-21T18:30:00+05:30",
    vvip_class="Z+",
    closure_policy={"advance_closure_sec": 180, "closure_type": "full_closure"},
    horizon_min=60,
    n_replications=10,
    seed=20260721,
    convoy_speed_kmh=50.0,
    weather={"monsoon": True, "rain_intensity_mm_h": 18.0},
    event_overrides={"waterlogging": 2.5, "breakdown": 1.4},
    forced_events=(
        {"type": "waterlogging", "segment_id": 1014, "start_sec": 300.0,
         "duration_sec": 2700.0, "severity": 0.7,
         "label": "Kalupur station approach waterlogged — 45 min pump-out"},
    ),
    expected_features=("monsoon", "evening_peak", "waterlogging_prone", "low_visibility_speed"),
))

_register(ScenarioSpec(
    scenario_id="multi_event_collision_kalupur",
    name="Multi-Event Collision at Kalupur",
    description=(
        "MANDATED ANCHOR — multi-event collision. A Z movement through the "
        "Kalupur junction complex (J8/J15) at 17:50 Friday while a double-lane "
        "breakdown blocks Station Road, a signal fault collapses the J8 "
        "greens, and a monsoon shower waterlogs the Relief Road "
        "approach to Kalupur — three simultaneous capacity hits on one "
        "junction complex, all on the convoy's path."
    ),
    category="multi_event",
    route_segment_ids=(1030, 1014, 1005),
    departure_time="2026-08-14T17:50:00+05:30",
    vvip_class="Z",
    closure_policy={"advance_closure_sec": 150, "closure_type": "full_closure"},
    horizon_min=60,
    n_replications=12,
    seed=20260814,
    weather={"monsoon": True, "rain_intensity_mm_h": 9.0},
    event_overrides={"waterlogging": 1.5, "signal_fault": 1.2},
    forced_events=(
        {"type": "vehicle_breakdown", "segment_id": 1014, "start_sec": 240.0,
         "duration_sec": 2100.0, "severity": 0.85,
         "label": "truck breakdown blocking 2 lanes at Kalupur approach"},
        {"type": "signal_fault", "segment_id": 1014, "start_sec": 480.0,
         "duration_sec": 1500.0, "severity": 0.8,
         "label": "J8 signal controller fault — flashing amber"},
        {"type": "waterlogging", "segment_id": 1005, "start_sec": 900.0,
         "duration_sec": 2400.0, "severity": 0.65,
         "label": "waterlogged Relief Road approach to Kalupur"},
    ),
    expected_features=("multi_event", "simultaneous_capacity_loss", "friday_peak",
                       "long_recovery"),
))

_register(ScenarioSpec(
    scenario_id="zplus_airport_morning_peak",
    name="Z+ Airport Run at Morning Peak",
    description=(
        "MANDATED ANCHOR — Z+ at peak. Head-of-state class run city → airport "
        "at 08:45 Monday: maximum protocol closure (180 s advance), full "
        "closure type, morning-peak demand on the Relief Road - Dr. Ambedkar "
        "Road - Airport Road corridor, plus the Monday commute peak in full swing."
    ),
    category="baseline",
    route_segment_ids=(1014, 1005, 1043, 1027, 1029),
    departure_time="2026-10-12T08:45:00+05:30",
    vvip_class="Z+",
    closure_policy={"advance_closure_sec": 180, "closure_type": "full_closure"},
    horizon_min=45,
    n_replications=10,
    seed=20261012,
    expected_features=("zplus_protocol", "morning_peak", "full_closure"),
))

_register(ScenarioSpec(
    scenario_id="sunday_market_spillover",
    name="Sunday Market Spillover (Old City)",
    description=(
        "Y-class run through the Lal Darwaja bazaar cluster at 11:00 Sunday: "
        "market spillover demand saturates Relief/Gandhi/Swami Vivekananda "
        "Roads while "
        "Sunday shoppers queue at the J3/J5 signals."
    ),
    category="event",
    route_segment_ids=(1015, 1005, 1049, 1013),
    departure_time="2026-10-18T11:00:00+05:30",
    vvip_class="Y",
    closure_policy={"advance_closure_sec": 90, "closure_type": "partial_closure"},
    horizon_min=45,
    n_replications=8,
    seed=20261018,
    event_overrides={"market": 2.2},
    expected_features=("market_spillover", "sunday_demand", "narrow_roads"),
))

_register(ScenarioSpec(
    scenario_id="rally_saturday_evening",
    name="Political Rally Spillover, Saturday Evening",
    description=(
        "A scheduled rally on the Ashram Road riverfront at 17:00 Saturday "
        "pours 900 veh/h of extra arrivals onto the corridor while a Z-class "
        "movement runs the spine in the opposite shoulder of the evening peak."
    ),
    category="event",
    route_segment_ids=(1009, 1002, 1001),
    departure_time="2026-10-17T17:00:00+05:30",
    vvip_class="Z",
    closure_policy={"advance_closure_sec": 120, "closure_type": "full_closure"},
    horizon_min=60,
    n_replications=10,
    seed=20261017,
    forced_events=(
        {"type": "rally_spillover", "segment_id": 1006, "start_sec": 0.0,
         "duration_sec": 5400.0, "severity": 0.8,
         "extra_demand_veh_h": 900.0,
         "label": "riverfront rally — 15k attendees, parking spillover"},
        {"type": "rally_spillover", "segment_id": 1001, "start_sec": 600.0,
         "duration_sec": 4800.0, "severity": 0.6,
         "extra_demand_veh_h": 500.0,
         "label": "rally dispersal onto Ashram Road southbound"},
    ),
    expected_features=("scheduled_rally", "saturday_evening", "demand_surge"),
))

_register(ScenarioSpec(
    scenario_id="signal_fault_friday_peak",
    name="Signal Faults at Friday Peak",
    description=(
        "Signal-controller faults at J1 and J8 during the Friday 18:00 peak "
        "while an X-class medical convoy crosses the city — tests green-ratio "
        "collapse under saturated demand without any physical blockage."
    ),
    category="ops",
    route_segment_ids=(1003, 1004, 1002, 1001),
    departure_time="2026-10-16T18:00:00+05:30",
    vvip_class="X",
    closure_policy={"advance_closure_sec": 60, "closure_type": "signal_hold"},
    horizon_min=45,
    n_replications=8,
    seed=20261016,
    event_overrides={"signal_fault": 3.0},
    forced_events=(
        {"type": "signal_fault", "segment_id": 1001, "start_sec": 300.0,
         "duration_sec": 1800.0, "severity": 0.75,
         "label": "J1 controller fault — flashing red"},
        {"type": "signal_fault", "segment_id": 1014, "start_sec": 900.0,
         "duration_sec": 1500.0, "severity": 0.6,
         "label": "J8 detector fault — stuck green one side"},
    ),
    expected_features=("signal_fault", "friday_peak", "capacity_loss_without_blockage"),
))

_register(ScenarioSpec(
    scenario_id="night_owl_low_demand",
    name="Night Owl — Low Demand",
    description=(
        "X-class run at 02:30 Wednesday night: near-empty roads, minimal "
        "disruption, quick recovery — the floor case that calibrates how much "
        "of peak-hour delay is demand-driven rather than closure-driven."
    ),
    category="baseline",
    route_segment_ids=_ASHRAM_SPINE,
    departure_time="2026-10-14T02:30:00+05:30",
    vvip_class="X",
    closure_policy={"advance_closure_sec": 60, "closure_type": "full_closure"},
    horizon_min=30,
    n_replications=6,
    seed=20261014,
    expected_features=("low_demand", "fast_recovery", "floor_case"),
))

_register(ScenarioSpec(
    scenario_id="monsoon_morning_commute",
    name="Monsoon Morning Commute (SG Highway)",
    description=(
        "Y-class run west on SG Highway at 09:00 in moderate monsoon rain "
        "(8 mm/h) — morning-peak demand on the motorway with weather-damped "
        "free-flow and elevated breakdown rates."
    ),
    category="weather",
    route_segment_ids=(1018, 1017, 1016),
    departure_time="2026-08-11T09:00:00+05:30",
    vvip_class="Y",
    closure_policy={"advance_closure_sec": 90, "closure_type": "partial_closure"},
    horizon_min=45,
    n_replications=8,
    seed=20260811,
    weather={"monsoon": True, "rain_intensity_mm_h": 8.0},
    event_overrides={"breakdown": 1.6, "waterlogging": 1.2},
    expected_features=("monsoon", "morning_peak", "motorway_demand"),
))

_register(ScenarioSpec(
    scenario_id="breakdown_ring_road",
    name="Multi-Lane Breakdown on SP Ring Road",
    description=(
        "Z-class run on the eastern ring road when a container truck loses "
        "two of eight lanes for 35 minutes mid-route — tests partial-capacity "
        "closures interacting with a rolling convoy window."
    ),
    category="event",
    route_segment_ids=(1022, 1008),
    departure_time="2026-10-15T16:30:00+05:30",
    vvip_class="Z",
    closure_policy={"advance_closure_sec": 120, "closure_type": "full_closure"},
    horizon_min=45,
    n_replications=8,
    seed=20261015,
    forced_events=(
        {"type": "vehicle_breakdown", "segment_id": 1008, "start_sec": 420.0,
         "duration_sec": 2100.0, "severity": 0.9,
         "label": "container truck jackknifed — 2 lanes blocked"},
    ),
    expected_features=("forced_breakdown", "partial_capacity", "ring_road_speed"),
))

_register(ScenarioSpec(
    scenario_id="stadium_match_evening",
    name="Match Night at Motera",
    description=(
        "T20 match dispersal at Motera stadium: scheduled spillover of 1100 "
        "veh/h onto the stadium road and its neighbours at 20:15 while a "
        "Z-class movement brings the dignitary out through the same corridor."
    ),
    category="event",
    route_segment_ids=(1009, 1041, 1032),
    departure_time="2026-10-19T20:15:00+05:30",
    vvip_class="Z",
    closure_policy={"advance_closure_sec": 120, "closure_type": "full_closure"},
    horizon_min=60,
    n_replications=10,
    seed=20261019,
    forced_events=(
        {"type": "rally_spillover", "segment_id": 1032, "start_sec": 0.0,
         "duration_sec": 3600.0, "severity": 0.85,
         "extra_demand_veh_h": 1100.0,
         "label": "post-match dispersal surge"},
    ),
    expected_features=("scheduled_surge", "stadium_adjacency", "night_peak"),
))

_register(ScenarioSpec(
    scenario_id="fog_winter_morning",
    name="Winter Fog Morning",
    description=(
        "X-class run at 07:30 in late-December fog: visibility caps urban "
        "free-flow at ~55% and shifts the morning peak later; tests weather "
        "speed caps without rain or events."
    ),
    category="weather",
    route_segment_ids=(1003, 1004),
    departure_time="2026-12-22T07:30:00+05:30",
    vvip_class="X",
    closure_policy={"advance_closure_sec": 60, "closure_type": "partial_closure"},
    horizon_min=45,
    n_replications=8,
    seed=20261222,
    weather={"monsoon": False, "fog": True},
    expected_features=("fog_speed_cap", "winter_morning", "weather_only"),
))

_register(ScenarioSpec(
    scenario_id="friday_market_evening",
    name="Friday Market Evening (CG Road)",
    description=(
        "Friday 19:00 run along CG Road: late-shopping market spillover on "
        "the adjacent Relief Road cluster combines with the extended Friday "
        "evening peak; the signal_hold policy variant shows the lighter-touch "
        "closure option under sustained demand."
    ),
    category="event",
    route_segment_ids=(1003, 1004),
    departure_time="2026-10-16T19:00:00+05:30",
    vvip_class="Y",
    closure_policy={"advance_closure_sec": 90, "closure_type": "signal_hold"},
    horizon_min=45,
    n_replications=8,
    seed=20261016,
    event_overrides={"market": 1.8},
    forced_events=(
        {"type": "market_spillover", "segment_id": 1005, "start_sec": 0.0,
         "duration_sec": 3000.0, "severity": 0.6,
         "label": "Friday bazaar spillover from Relief Road"},
    ),
    expected_features=("friday_late_peak", "market_spillover", "signal_hold_policy"),
))

_register(ScenarioSpec(
    scenario_id="republic_day_parade_morning",
    name="Republic Day Parade Morning",
    description=(
        "Jan 26 morning: a scheduled parade on the Gandhinagar Highway strip "
        "(staged, 09:00–10:30) plus holiday-morning demand shape; a Z-class "
        "movement runs the airport corridor through the tail of the parade "
        "window."
    ),
    category="event",
    route_segment_ids=(1029, 1028, 1048, 1024, 1025),
    departure_time="2027-01-26T09:15:00+05:30",
    vvip_class="Z",
    closure_policy={"advance_closure_sec": 150, "closure_type": "full_closure"},
    horizon_min=60,
    n_replications=10,
    seed=20270126,
    forced_events=(
        {"type": "rally_spillover", "segment_id": 1025, "start_sec": 0.0,
         "duration_sec": 4500.0, "severity": 0.75,
         "extra_demand_veh_h": 700.0,
         "label": "Republic Day parade — spectators and parking"},
    ),
    expected_features=("holiday_demand", "scheduled_parade", "morning_peak_tail"),
))


# ── The eight use-case profiles (core stream; wired verbatim from the
# shared use-case library configs — the per-use-case scenario wiring the
# event-agnostic re-engineering consumes).  The VVIP base case is the
# reference every other use case generalizes; the other seven exercise
# the public-event archetypes on the same corridor network.
# ────────────────────────────────────────────────────────────────────────────

_register(ScenarioSpec(
    scenario_id="vvip_convoy_base",
    name="VVIP Convoy — Base Case (Z+ state-capital run)",
    description=(
        "BASE CASE. Z+ protected movement from Gandhinagar into the city "
        "core on a Monday mid-morning: down the Gandhinagar-Ahmedabad "
        "Highway (1026-1025-1024) through Koba and Bhat Circles, over the "
        "Indira Bridge (1048), along Airport Road (1028-1027) to Shahibaug "
        "and in on Dr. Ambedkar Road (1043) to Lal Darwaja. Maximum-"
        "protocol closure (180 s advance, full closure), default event "
        "rates, no weather — the reference movement every other use case is "
        "a generalization of."
    ),
    category="baseline",
    route_segment_ids=(1026, 1025, 1024, 1048, 1028, 1027, 1043),
    departure_time="2026-11-02T10:10:00+05:30",
    vvip_class="Z+",
    closure_policy={"advance_closure_sec": 180, "closure_type": "full_closure"},
    horizon_min=45,
    n_replications=10,
    seed=20261102,
    convoy_speed_kmh=60.0,
    expected_features=("zplus_protocol", "capital_to_city", "reference_case"),
    event_kind="protected_movement",
    crowd_class="LOW",
    duration_class="minutes",
))

_register(ScenarioSpec(
    scenario_id="rath_yatra_ahmedabad",
    name="Jagannath Rath Yatra — Old-City Procession",
    description=(
        "148th Ahmedabad Rath Yatra (27 Jun 2025): three chariots step off "
        "at 07:00 from the ~400-year-old Jamalpur temple and crawl the "
        "old-city bazaar belt of India's second-largest Rath Yatra (~14 km "
        "in all). Modeled on the walled city's real streets: east along "
        "one-way Gandhi Road to Kalupur (1012), south past Sarangpur "
        "Darwaja (1049) and west along one-way Swami Vivekananda Road "
        "toward Jamalpur (1013), with Relief Road (1005) shut alongside. "
        "Modeled as an X-class managed movement (the procession "
        "itself, not a protected person) at chariot pace (8 km/h, the "
        "engine floor) with full street closure rolling ahead of it and "
        "lakhs-strong crowd load on every segment."
    ),
    category="event",
    route_segment_ids=(1012, 1049, 1013),
    departure_time="2025-06-27T07:00:00+05:30",
    vvip_class="X",
    closure_policy={"advance_closure_sec": 120, "closure_type": "full_closure"},
    horizon_min=60,
    n_replications=12,
    seed=20250627,
    convoy_speed_kmh=8.0,
    event_overrides={"market": 2.5},
    forced_events=(
        {"type": "rally_spillover", "segment_id": 1012, "start_sec": 0.0,
         "duration_sec": 14400.0, "severity": 0.9,
         "extra_demand_veh_h": 950.0,
         "label": "three chariots + lakhs of devotees on Gandhi Road — "
                  "yatra out-leg"},
        {"type": "rally_spillover", "segment_id": 1005, "start_sec": 1200.0,
         "duration_sec": 13200.0, "severity": 0.85,
         "extra_demand_veh_h": 850.0,
         "label": "chariot crawl on Relief Road — old-city bazaar belt "
                  "closed"},
        {"type": "market_spillover", "segment_id": 1013, "start_sec": 0.0,
         "duration_sec": 10800.0, "severity": 0.75,
         "label": "Swami Vivekananda Road bazaar crowd pinned behind barricades"},
    ),
    expected_features=("public_procession", "extreme_crowd",
                       "narrow_old_city_roads", "slow_rolling_closure"),
    event_kind="procession",
    crowd_class="EXTREME",
    duration_class="hours",
))

# FLAGSHIP multi-event composition — one deterministic config driving three
# event families the engine never hard-coded: a tier-'none' ceremonial
# procession (EXTREME crowd, day-long, moving corridor), a monsoon weather
# event (18 mm/h burst + scheduled waterlogging on the low-lying Nehru
# Bridge approach), and an emergency-passage request (the G4 gap) whose
# serving entity is a 108 ambulance — not a VVIP.  The engine's answer is
# signal_hold at 45 s advance: 'regulated, not stopped' (Blue Book text,
# PROBLEM_STATEMENT_GUJARAT.md:62-69) — the policy knob adapting by event
# class (full_closure 180 s for Z+ -> signal_hold 45 s for a green
# corridor) proves the engine optimizes corridor occupancy, not protocol
# rank.
_register(ScenarioSpec(
    scenario_id="rath_yatra_monsoon_ambulance",
    name="Rath Yatra Morning, Monsoon Burst, One Siren",
    description=(
        "FLAGSHIP multi-event showcase. 09:20 on the real 148th Rath "
        "Yatra morning of 27 Jun 2025 (procession stepped off 07:00 from "
        "Jamalpur): three chariots crawl Gandhi Road and Relief Road with "
        "lakhs on the street, a monsoon burst at 18 mm/h waterlogs the "
        "Nehru Bridge approach, bazaar crowds stand behind Swami "
        "Vivekananda Road barricades — and a 108 ambulance carrying a "
        "cardiac patient requests a green corridor across the river: "
        "Relief Road -> Nehru Bridge -> Ashram Road south -> Riverfront "
        "West (1005->1015->1001->1006). The engine's answer: "
        "closure_policy = signal_hold at 45 s "
        "advance, threading a medical convoy between a public festival "
        "and the weather."
    ),
    category="multi_event",
    route_segment_ids=(1005, 1015, 1001, 1006),
    departure_time="2025-06-27T09:20:00+05:30",
    vvip_class="X",
    closure_policy={"advance_closure_sec": 45, "closure_type": "signal_hold"},
    horizon_min=60,
    n_replications=12,
    seed=202506271,
    convoy_speed_kmh=42.0,
    weather={"monsoon": True, "rain_intensity_mm_h": 18.0},
    event_overrides={"market": 2.0, "ambulance": 1.0},
    forced_events=(
        {"type": "rally_spillover", "segment_id": 1012, "start_sec": 0.0,
         "duration_sec": 3600.0, "severity": 0.95,
         "extra_demand_veh_h": 950.0,
         "label": "three chariots + lakhs on Gandhi Road — yatra in its "
                  "third hour"},
        {"type": "rally_spillover", "segment_id": 1005, "start_sec": 1200.0,
         "duration_sec": 2400.0, "severity": 0.9,
         "extra_demand_veh_h": 850.0,
         "label": "chariot crawl turns onto Relief Road — bazaar belt "
                  "regulated, not stopped"},
        {"type": "waterlogging", "segment_id": 1015, "start_sec": 600.0,
         "duration_sec": 1800.0, "severity": 0.7,
         "label": "monsoon burst at 18 mm/h waterlogs the Nehru Bridge "
                  "approach (scheduled waterlogging)"},
        {"type": "market_spillover", "segment_id": 1013, "start_sec": 0.0,
         "duration_sec": 3600.0, "severity": 0.75,
         "label": "Swami Vivekananda Road bazaar crowd behind barricades"},
        {"type": "emergency_vehicle", "segment_id": 1005, "start_sec": 600.0,
         "duration_sec": 300.0, "severity": 0.9,
         "label": "108 cardiac green-corridor window — Relief Road "
                  "stretch"},
        {"type": "emergency_vehicle", "segment_id": 1015, "start_sec": 700.0,
         "duration_sec": 300.0, "severity": 0.9,
         "label": "108 cardiac green-corridor window — Nehru Bridge "
                  "crossing"},
        {"type": "emergency_vehicle", "segment_id": 1006, "start_sec": 800.0,
         "duration_sec": 300.0, "severity": 0.9,
         "label": "108 cardiac green-corridor window — Riverfront West"},
        {"type": "emergency_vehicle", "segment_id": 1001, "start_sec": 900.0,
         "duration_sec": 300.0, "severity": 0.9,
         "label": "108 cardiac green-corridor window — Ashram South "
                  "handover"},
    ),
    expected_features=("multi_event_composition", "public_procession",
                       "monsoon_burst", "green_corridor_request",
                       "signal_hold_regulated_passage"),
    event_kind="procession",
    crowd_class="EXTREME",
    duration_class="hours",
))

_register(ScenarioSpec(
    scenario_id="kumbh_scale_gathering",
    name="Kumbh-Class Mass Gathering — Perimeter Zone",
    description=(
        "A Kumbh-class bathing-day window (the Maha Kumbh 2025 class: "
        "~660 million visits over the mela, largest gathering on earth). "
        "The corridor problem inverts: the zone interior is car-free and "
        "frozen; what the engine manages is the perimeter ring and the "
        "ingress surges. Modeled on the corridor network's riverfront "
        "cluster — Ashram Road south (1001) onto Riverfront West (1006) and "
        "up Ashram Road through Usmanpura (1009) — with an X-class "
        "supply/medical shuttle "
        "entering the zone at 04:30 pre-dawn under signal_hold (signals "
        "keep cycling: staged ingress, not blanket freeze)."
    ),
    category="event",
    route_segment_ids=(1001, 1006, 1009),
    departure_time="2025-01-29T04:30:00+05:30",
    vvip_class="X",
    closure_policy={"advance_closure_sec": 90, "closure_type": "signal_hold"},
    horizon_min=60,
    n_replications=12,
    seed=20250129,
    convoy_speed_kmh=40.0,
    forced_events=(
        {"type": "rally_spillover", "segment_id": 1006, "start_sec": 0.0,
         "duration_sec": 21600.0, "severity": 0.95,
         "extra_demand_veh_h": 1400.0,
         "label": "riverfront zone core — bathing-day ingress surge "
                  "(Kumbh-class footfall)"},
        {"type": "rally_spillover", "segment_id": 1007, "start_sec": 3600.0,
         "duration_sec": 18000.0, "severity": 0.9,
         "extra_demand_veh_h": 1100.0,
         "label": "east-bank queueing corridors at capacity"},
        {"type": "rally_spillover", "segment_id": 1001, "start_sec": 0.0,
         "duration_sec": 21600.0, "severity": 0.8,
         "extra_demand_veh_h": 900.0,
         "label": "Ashram Road approach saturated by pilgrim arrivals"},
    ),
    expected_features=("mass_gathering", "zone_perimeter", "staged_ingress",
                       "extreme_crowd"),
    event_kind="mass_gathering",
    crowd_class="EXTREME",
    duration_class="multi_day",
))

_register(ScenarioSpec(
    scenario_id="riverfront_marathon",
    name="City Marathon — Sabarmati Riverfront Race Corridor",
    description=(
        "Adani Ahmedabad Marathon class (9th edition: 24,000+ runners on "
        "the Sabarmati Riverfront, 24 Nov 2024; World Athletics calendar). "
        "A 05:45 gun start means the race corridor (Riverfront East 1007 - "
        "Nehru Bridge 1015 - Ashram South 1001 - Riverfront West 1006) is "
        "closed to vehicles in the dark and reopens in stages behind the "
        "sweep. Modeled as an X-class pace/sweep convoy at 12 km/h under "
        "full closure with spectator spillover forced on the two approach "
        "segments."
    ),
    category="event",
    route_segment_ids=(1007, 1015, 1001, 1006),
    departure_time="2024-11-24T05:45:00+05:30",
    vvip_class="X",
    closure_policy={"advance_closure_sec": 60, "closure_type": "full_closure"},
    horizon_min=60,
    n_replications=10,
    seed=20241124,
    convoy_speed_kmh=12.0,
    forced_events=(
        {"type": "rally_spillover", "segment_id": 1006, "start_sec": 0.0,
         "duration_sec": 14400.0, "severity": 0.85,
         "extra_demand_veh_h": 800.0,
         "label": "24,000+ runners + spectator vehicles pressing the "
                  "riverfront road"},
        {"type": "rally_spillover", "segment_id": 1001, "start_sec": 3600.0,
         "duration_sec": 10800.0, "severity": 0.7,
         "extra_demand_veh_h": 600.0,
         "label": "spectator spillover onto Ashram Road south"},
    ),
    expected_features=("road_race", "dawn_start", "rolling_reopen",
                       "spectator_spillover"),
    event_kind="race",
    crowd_class="HIGH",
    duration_class="hours",
))

_register(ScenarioSpec(
    scenario_id="national_day_parade",
    name="Republic Day / Independence Day — Ceremonial Parade Corridor",
    description=(
        "The two fixed ceremonial days every year. At the 76th Republic "
        "Day (26 Jan 2025) the Governor hoisted the flag and took the "
        "parade salute with the CM present (Gujarat CMO) — a Z-class "
        "protected movement to the venue up the GN Highway strip. Modeled: "
        "dignitary runs from SVPI airport (1029-1028) over the Indira "
        "Bridge (1048) onto the GN Highway parade strip at Bhat "
        "(1024-1025) at 08:35, arriving before the 09:00 flag, while "
        "spectator and parking load saturates the strip itself. "
        "Independence Day (15 Aug) is the calendar mirror."
    ),
    category="event",
    route_segment_ids=(1029, 1028, 1048, 1024, 1025),
    departure_time="2027-01-26T08:35:00+05:30",
    vvip_class="Z",
    closure_policy={"advance_closure_sec": 150, "closure_type": "full_closure"},
    horizon_min=60,
    n_replications=10,
    seed=20270126,
    convoy_speed_kmh=60.0,
    forced_events=(
        {"type": "rally_spillover", "segment_id": 1025, "start_sec": 0.0,
         "duration_sec": 5400.0, "severity": 0.75,
         "extra_demand_veh_h": 700.0,
         "label": "Republic Day parade — grandstand spectators and parking "
                  "on the GN Highway strip"},
        {"type": "rally_spillover", "segment_id": 1024, "start_sec": 0.0,
         "duration_sec": 5400.0, "severity": 0.6,
         "extra_demand_veh_h": 500.0,
         "label": "spectator approach traffic on GN Highway south"},
    ),
    expected_features=("national_day", "ceremonial_strip", "morning_protocol",
                       "holiday_demand"),
    event_kind="ceremonial",
    crowd_class="HIGH",
    duration_class="minutes",
))

_register(ScenarioSpec(
    scenario_id="motera_stadium_egress",
    name="Stadium Egress — Narendra Modi Stadium Match Night",
    description=(
        "Narendra Modi Stadium (Motera): ~132,000 seats, the world's "
        "largest cricket ground; ~130,000 in the house for the IPL 2026 "
        "final and ~3,000 police + ~1,000 home guards with published "
        "traffic advisories and diversions for the T20 World Cup 2026 "
        "final. The corridor event is the 90-120 min egress surge — "
        "modeled at 20:05, just after a final ball: an X-class ambulance "
        "with a green-corridor request fights the dispersal wave from "
        "Motera Stadium Road (1032) over the Chimanbhai Patel Flyover "
        "(1041) down the Ashram spine (1009-1002-1001) "
        "under signal_hold, with the surge and one stalled car forced on "
        "the egress segments."
    ),
    category="event",
    route_segment_ids=(1032, 1041, 1009, 1002, 1001),
    departure_time="2026-03-08T20:05:00+05:30",
    vvip_class="X",
    closure_policy={"advance_closure_sec": 60, "closure_type": "signal_hold"},
    horizon_min=60,
    n_replications=12,
    seed=20260308,
    convoy_speed_kmh=50.0,
    forced_events=(
        {"type": "rally_spillover", "segment_id": 1032, "start_sec": 0.0,
         "duration_sec": 5400.0, "severity": 0.95,
         "extra_demand_veh_h": 1500.0,
         "label": "~130k spectators dispersing from Narendra Modi Stadium"},
        {"type": "rally_spillover", "segment_id": 1002, "start_sec": 600.0,
         "duration_sec": 4800.0, "severity": 0.8,
         "extra_demand_veh_h": 1000.0,
         "label": "egress wave onto Ashram Road north"},
        {"type": "vehicle_breakdown", "segment_id": 1002, "start_sec": 1200.0,
         "duration_sec": 1500.0, "severity": 0.7,
         "label": "stalled car blocking one dispersal lane"},
        # The ambulance-with-green-corridor-request from the use-case
        # description, as a deterministic scheduled emergency vehicle:
        {"type": "emergency_vehicle", "segment_id": 1032, "start_sec": 300.0,
         "duration_sec": 900.0, "severity": 0.9,
         "label": "green-corridor ambulance crossing the dispersal wave"},
    ),
    expected_features=("stadium_egress", "night_surge", "mass_dispersal",
                       "ambulance_through_crowd"),
    event_kind="egress_surge",
    crowd_class="EXTREME",
    duration_class="hours",
))

_register(ScenarioSpec(
    scenario_id="flood_evacuation_corridor",
    name="Monsoon Flood — Evacuation and Relief Corridor",
    description=(
        "Gujarat floods, 27 Aug 2024 class: ~280 mm of rain in 24 h, 28+ "
        "dead, 24,000+ relocated statewide, NDRF and Army columns running "
        "boat rescues as low-lying city cores went under. Modeled on the "
        "corridor network: the low-lying river spine (1001/1006/1015) "
        "waterlogs while an X-class relief column runs the reverse-flow "
        "evacuation spine — Sarkhej (1040) onto SP Ring Road "
        "(1021-1022-1008), the high-ground detour — at 11:20 in 22 mm/h "
        "rain under signal_hold, with evacuee traffic surging onto the "
        "ring road behind it."
    ),
    category="weather",
    route_segment_ids=(1040, 1021, 1022, 1008),
    departure_time="2024-08-27T11:20:00+05:30",
    vvip_class="X",
    closure_policy={"advance_closure_sec": 60, "closure_type": "signal_hold"},
    horizon_min=60,
    n_replications=10,
    seed=20240827,
    convoy_speed_kmh=40.0,
    weather={"monsoon": True, "rain_intensity_mm_h": 22},
    event_overrides={"breakdown": 1.8, "waterlogging": 3},
    forced_events=(
        {"type": "waterlogging", "segment_id": 1001, "start_sec": 0.0,
         "duration_sec": 10800.0, "severity": 0.8,
         "label": "Ashram Road south under water — low-lying spine "
                  "flooded"},
        {"type": "waterlogging", "segment_id": 1006, "start_sec": 1200.0,
         "duration_sec": 9000.0, "severity": 0.75,
         "label": "riverfront west carriageway submerged"},
        {"type": "waterlogging", "segment_id": 1015, "start_sec": 600.0,
         "duration_sec": 9600.0, "severity": 0.7,
         "label": "Nehru Bridge approach pumping"},
        {"type": "rally_spillover", "segment_id": 1021, "start_sec": 0.0,
         "duration_sec": 10800.0, "severity": 0.85,
         "extra_demand_veh_h": 1200.0,
         "label": "evacuee traffic surging onto SP Ring Road"},
        {"type": "vehicle_breakdown", "segment_id": 1022, "start_sec": 3000.0,
         "duration_sec": 1800.0, "severity": 0.7,
         "label": "abandoned vehicle on the flooded ring-road shoulder"},
    ),
    expected_features=("flood_response", "reverse_flow_spine",
                       "low_lying_closures", "relief_priority"),
    event_kind="emergency_evacuation",
    crowd_class="MEDIUM",
    duration_class="hours",
))

_register(ScenarioSpec(
    scenario_id="metro_work_zone",
    name="Planned Infrastructure Work Zone — Metro/Bullet-Train Works",
    description=(
        "The most predictable corridor event: months-long lane loss "
        "published in advance. Ahmedabad Metro Phase 2 (Motera to "
        "Gandhinagar's Mahatma Mandir, built since Feb 2021; 77.63 km "
        "network approved) put Kankaria Road under a published 3 Jul - 30 "
        "Sep closure with diversions, and ~100 m at Kalupur Bridge shut "
        "for bullet-train pillar work. Modeled: an X-class inspection "
        "convoy at 09:15 runs Kasturba Gandhi Road (1031) through the "
        "Kalupur pinch (1014), south past Sarangpur Darwaja (1049) and onto "
        "Kankaria Road (1038) while both work zones hold two barricaded "
        "lanes (long-duration forced lane-loss) and diverted bazaar traffic "
        "squeezes Kankaria Road itself."
    ),
    category="ops",
    route_segment_ids=(1031, 1014, 1049, 1038),
    departure_time="2025-08-14T09:15:00+05:30",
    vvip_class="X",
    closure_policy={"advance_closure_sec": 60, "closure_type": "partial_closure"},
    horizon_min=60,
    n_replications=8,
    seed=20250814,
    convoy_speed_kmh=40.0,
    event_overrides={"market": 1.5},
    forced_events=(
        {"type": "vehicle_breakdown", "segment_id": 1038, "start_sec": 0.0,
         "duration_sec": 39600.0, "severity": 0.9,
         "label": "Kankaria Road metro works — two lanes barricaded "
                  "(published 3 Jul-30 Sep closure)"},
        {"type": "vehicle_breakdown", "segment_id": 1014, "start_sec": 0.0,
         "duration_sec": 39600.0, "severity": 0.85,
         "label": "Kalupur Bridge bullet-train pillar works — standing "
                  "lane loss on Station Road"},
        {"type": "market_spillover", "segment_id": 1038, "start_sec": 0.0,
         "duration_sec": 10800.0, "severity": 0.7,
         "label": "diverted bazaar traffic squeezing Kankaria Road"},
    ),
    expected_features=("planned_work_zone", "months_long_lane_loss",
                       "published_diversion", "peak_hour_pinch"),
    event_kind="work_zone",
    crowd_class="LOW",
    duration_class="multi_week",
))


# ── Library access API ─────────────────────────────────────────────────────

def list_scenarios() -> list[str]:
    """Scenario IDs in registration order."""
    return list(SCENARIO_LIBRARY.keys())


def get_scenario(scenario_id: str) -> ScenarioSpec:
    try:
        return SCENARIO_LIBRARY[scenario_id]
    except KeyError:
        raise KeyError(
            f"unknown scenario {scenario_id!r}; available: {list_scenarios()}"
        ) from None


def library_summary() -> list[dict]:
    return [
        {
            "scenario_id": s.scenario_id,
            "name": s.name,
            "category": s.category,
            "vvip_class": s.vvip_class,
            "departure_time": s.departure_time,
            "route_segment_ids": list(s.route_segment_ids),
            "seed": s.seed,
            "expected_features": list(s.expected_features),
        }
        for s in SCENARIO_LIBRARY.values()
    ]


def export_library(*, include_network: bool = True) -> dict:
    """Serializable form of the whole library (+ network facts for consumers).

    The JSON emitted from this dict is the artifact ``scripts/impact_run.py``
    consumes: segment facts match its own seed parse, and closure/demand
    parameters map onto ``commuter_delay_min(hold_min, lam, n_junctions)``.
    """
    network = default_network()
    return {
        "format": "veer/scenario-library/1",
        "generator": "traffic_oracle.simulation.scenarios.export_library",
        "attribution": "network facts from the seed's roads on OpenStreetMap — "
                       "(c) OpenStreetMap contributors, ODbL 1.0",
        "n_scenarios": len(SCENARIO_LIBRARY),
        "scenarios": [s.to_dict() for s in SCENARIO_LIBRARY.values()],
        **({"network": network.to_dict()} if include_network else {}),
    }
