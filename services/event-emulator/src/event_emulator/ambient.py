"""Living City — the always-on, deterministic ambient city.

Runs when ``EMULATOR_LIVING_CITY=1`` (or when an operator starts it from
the deck).  Ahmedabad is never empty at rest: at any instant two to three
emulated causes hold a stretch of road, two Jan Rakshak beats ride their
patrol routes, the registered ANPR cameras read passing traffic, and every
~25 minutes a 108 ambulance asks for a green corridor — which only a named
duty officer can approve.

DETERMINISM.  The day program is a pure function f(city_seed, IST date):
~40 slots on three lanes (two always on, one intermittent — hence 2–3
overlapping causes), the 108 requests and the rare deliberate violators.
The same date and seed give the identical program on any host.  The
background trickle is a pure function of (day seed, tick index); the only
runtime input it consults is the signal phase the oracle itself would
evaluate (the same pure clock, tape.movement_states_at), so law-abiding
traffic crosses stop lines on green.

CAUSES ONLY.  A slot becomes a corridor event (metadata source
'event-emulator', mode 'living_city', slot, run_id) opened 'active' and
closed 'completed' through the oracle's lifecycle seam (C-F).  Reads go
through the oracle's ANPR seam (C-E): the oracle computes hits from its
earmarks and e-challan rules from its limits and programs — the living
city never asserts either; deliberate violators are causes (a vehicle that
speeds, one that runs a red), and the outcome is checked against the plan:
an UNPLANNED rule hit is counted and its camera leaves the compliant
rotation.  108 requests land as dispatch rows in 'requested' — never
approved by the emulator.  Patrols ride corridor.units.emulated (C-A); a
patrol an officer has dispatched is handed to the oracle's dispatch
projection.  Slots write no adaptive:inputs docs: the situation engine
reads live traffic for them.

PRE-EMPTION.  A manual or flagship emulator run holds its route: open slots
on those roads close at once ('pre-empted by run N'), none opens there
until the run ends, and a pre-empted slot whose window is still open then
resumes as a fresh event.

108 REQUESTS OUTLIVE THEIR SLOT.  Closing an event makes the oracle cancel
its open dispatches (its C-F auto-close).  A 108 request still waiting for
an officer when its slot closes — pre-emption, the window's end, the IST
day rollover — is re-filed on the next tick onto a slot that is open, with
a note naming the request it replaces, for up to 20 minutes after 108
first asked; the duty officer's queue never silently loses a pending
request.  (An emulator restart is the exception: the closing process
cannot re-file, so those requests end cancelled with their audit row.)

SELF-HEALING.  An oracle or database outage never leaves the city wrong
for longer than it lasts: an event that could not be activated, or closed,
is retried every ~30 s, and a session that could not be ledgered at start
is ledgered once the store answers.  An event an officer cancelled before
the city could activate it is left closed.

NETWORK CHECK.  The sites and patrol beats name the seed network's own
segments and junctions; at the first session the city checks them against
the live network, and a mismatch (a reseeded network) fails loudly — status
detail, /health reason, error log — while slots on unknown roads never
open.

Ethics (C6): vehicles only (plates, no person, face or frame); every plate
carries the EM series marker; every event, unit and read is labelled
emulated.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any

import httpx
import structlog

from event_emulator.nats_seam import UNITS_EMULATED_SUBJECT
from event_emulator.rng import SeededRng, derive_seed
from event_emulator.runner import (
    RunnerDeps,
    _detail,
    activate_event,
    close_event,
    load_camera_context,
)
from event_emulator.safe_http import UnsafeUrlError
from event_emulator.store import utcnow
from event_emulator.tape import (
    chain_polyline,
    compliant_speed_kmh,
    movement_states_at,
    pingpong_point,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "IST",
    "TICK_SEC",
    "CITY_SEED_DEFAULT",
    "SLOT_KINDS",
    "SITES",
    "Slot",
    "AmbulanceRequest",
    "Violation",
    "DayProgram",
    "day_seed_for",
    "build_day_program",
    "trickle_read",
    "LivingCity",
]

#: India Standard Time — fixed offset, no DST (no tz database needed).
IST = timezone(timedelta(hours=5, minutes=30))

#: Supervisor tick (seconds) at clock 1.
TICK_SEC = 5.0

#: Default city seed: Ahmedabad's founding year.
CITY_SEED_DEFAULT = 1411

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "vvip-event-emulator/living-city")


# ── slot kinds and sites (the seed network's own roads and junctions) ───────


@dataclass(frozen=True)
class SlotKind:
    event_kind: str               # events.corridor_events archetype (007 CHECK)
    closure_type: str             # phase closure policy
    crowd_class: str
    duration_min: tuple[float, float]
    sentence: str                 # what the cause is, for the description


SLOT_KINDS: dict[str, SlotKind] = {
    "market_spillover": SlotKind(
        "mass_gathering", "partial_closure", "HIGH", (100.0, 180.0),
        "bazaar crowds and hawkers spill onto the carriageway"),
    "signal_fault": SlotKind(
        "work_zone", "signal_hold", "LOW", (60.0, 110.0),
        "a junction signal is dark; traffic police hold it on manual control"),
    "breakdown": SlotKind(
        "work_zone", "partial_closure", "LOW", (45.0, 85.0),
        "a broken-down vehicle blocks a lane until the tow clears it"),
    "monsoon_rain": SlotKind(
        "work_zone", "partial_closure", "LOW", (75.0, 140.0),
        "a rain cell waterlogs a low stretch; one lane is barricaded"),
    "school_egress": SlotKind(
        "egress_surge", "signal_hold", "MEDIUM", (50.0, 80.0),
        "school dismissal: buses, autos and parents crowd the approach"),
}

#: A night market winds down by ~02:00, whatever its draw.
_NIGHT_MARKET_MIN = (60.0, 110.0)


@dataclass(frozen=True)
class Site:
    site_id: str
    kind: str
    name: str                     # signage name (road or junction)
    segments: tuple[int, ...]
    junctions: tuple[int, ...]    # adjacent junctions (segment_adjacency)
    detail: str = ""


#: Junctions with a signal program (corridor.signal_programs).
_SIGNALISED = frozenset({1, 3, 5, 6, 7, 8, 9, 10, 11, 13, 14})

SITES: tuple[Site, ...] = (
    # breakdowns on the arterials (lane loss until the tow)
    Site("BD-1017", "breakdown", "SG Highway (Bodakdev)", (1017,), (16, 17),
         "Container truck breakdown"),
    Site("BD-1021", "breakdown", "SP Ring Road (South)", (1021,), (10, 19), "Truck breakdown"),
    Site("BD-1002", "breakdown", "Ashram Road (North)", (1002,), (1, 2), "AMTS bus breakdown"),
    Site("BD-1030", "breakdown", "Naroda Road", (1030,), (8, 31), "Tanker breakdown"),
    Site("BD-1040", "breakdown", "SG Highway (Sarkhej)", (1040,), (10, 28),
         "Goods tempo breakdown"),
    Site("BD-1025", "breakdown", "Gandhinagar Highway (Central)", (1025,), (32, 34),
         "Truck breakdown"),
    Site("BD-1011", "breakdown", "Drive-In Road", (1011,), (17, 27), "Stalled car"),
    Site("BD-1019", "breakdown", "Satellite Road", (1019,), (9, 18), "Stalled car"),
    Site("BD-1031", "breakdown", "Kasturba Gandhi Road", (1031,), (2, 8), "Truck breakdown"),
    Site("BD-1027", "breakdown", "Airport Road (South)", (1027,), (14, 20), "Cab breakdown"),
    Site("BD-1032", "breakdown", "Motera Stadium Road", (1032,), (24, 25), "AMTS bus breakdown"),
    Site("BD-1022", "breakdown", "SP Ring Road (Southeast)", (1022,), (11, 19),
         "Container truck breakdown"),
    Site("BD-1034", "breakdown", "SP Ring Road (North)", (1034,), (12, 32), "Truck breakdown"),
    # signal faults at programmed junctions (the junction's legs)
    Site("SF-J1", "signal_fault", "Nehru Bridge - Ashram Road Junction", (1001, 1002, 1015), (1,)),
    Site("SF-J3", "signal_fault", "Lal Darwaja", (1005, 1007, 1012, 1015, 1043), (3,)),
    Site("SF-J5", "signal_fault", "Astodia Darwaja", (1013, 1038, 1049), (5,)),
    Site("SF-J6", "signal_fault", "Ellis Bridge East (Swami Vivekananda Road)",
         (1013, 1039), (6,)),
    Site("SF-J7", "signal_fault", "Gandhi Ashram Junction", (1009, 1041), (7,)),
    Site("SF-J8", "signal_fault", "Kalupur Station Junction", (1014, 1030, 1031), (8,)),
    Site("SF-J9", "signal_fault", "Iskcon Cross Roads (SG Highway)",
         (1016, 1019, 1023, 1045), (9,)),
    Site("SF-J10", "signal_fault", "Sarkhej - SP Ring Road Junction (NH147)",
         (1021, 1040, 1047), (10,)),
    Site("SF-J11", "signal_fault", "Odhav Ring Road Circle (SP Ring Road)", (1008, 1022), (11,)),
    Site("SF-J13", "signal_fault", "Indira Bridge Circle (Airport Road - Gandhinagar Highway)",
         (1028, 1048), (13,)),
    Site("SF-J14", "signal_fault", "Airport Circle (Hansol)", (1027, 1028, 1029), (14,)),
    # bazaar spillover (old city + commercial strips)
    Site("MK-1005", "market_spillover", "Relief Road", (1005,), (3, 15)),
    Site("MK-1013", "market_spillover", "Swami Vivekananda Road", (1013,), (5, 6)),
    Site("MK-1012", "market_spillover", "Gandhi Road", (1012,), (3, 15)),
    Site("MK-1038", "market_spillover", "Kankaria Road", (1038,), (5,)),
    Site("MK-1004", "market_spillover", "CG Road (North)", (1004,), (2, 22)),
    Site("MK-1014", "market_spillover", "Station Road (Kalupur)", (1014,), (8, 15)),
    Site("MK-1039", "market_spillover", "Swami Vivekananda Bridge (Ellis Bridge)", (1039,),
         (6, 33)),
    # school dismissal approaches
    Site("SE-1044", "school_egress", "132 Ft. Ring Road (IIM)", (1044,), (18, 27)),
    Site("SE-1019", "school_egress", "Satellite Road", (1019,), (9, 18)),
    Site("SE-1036", "school_egress", "Ashram Road (Paldi)", (1036,), (4, 33)),
    Site("SE-1010", "school_egress", "SH71 (Bhat–Motera Road)", (1010,), (24, 30)),
    Site("SE-1045", "school_egress", "SG Highway (Prahlad Nagar)", (1045,), (9, 28)),
    Site("SE-1009", "school_egress", "Ashram Road (Sabarmati Ashram)", (1009,), (2, 7)),
    Site("SE-1004", "school_egress", "CG Road (North)", (1004,), (2, 22)),
    # low stretches that waterlog in a rain cell
    Site("RN-1036", "monsoon_rain", "Ashram Road (Paldi)", (1036,), (4, 33)),
    Site("RN-1043", "monsoon_rain", "Dr. Ambedkar Road (Lal Darwaja–Shahibaug)", (1043,), (3, 20)),
    Site("RN-1038", "monsoon_rain", "Kankaria Road", (1038,), (5,)),
    Site("RN-1015", "monsoon_rain", "Nehru Bridge Road", (1015,), (1, 3)),
    Site("RN-1040", "monsoon_rain", "SG Highway (Sarkhej)", (1040,), (10, 28)),
    Site("RN-1020", "monsoon_rain", "Prahlad Nagar Road", (1020,), (28,)),
    Site("RN-1039", "monsoon_rain", "Swami Vivekananda Bridge (Ellis Bridge)", (1039,), (6, 33)),
)

_SITES_BY_KIND: dict[str, tuple[Site, ...]] = {
    k: tuple(s for s in SITES if s.kind == k) for k in SLOT_KINDS
}

#: The two Jan Rakshak beats (connected chains of the corridor network, each
#: one-way driven its own way: scripts/data/osm_network.json).  Unit
#: ids carry the EM marker like every emulated plate and unit (LC: living
#: city — never the JR-EM-0n ids a use-case tape publishes).
PATROLS: tuple[tuple[str, str, tuple[int, ...]], ...] = (
    ("JR-EM-LC1", "Old City belt", (1015, 1012, 1049, 1013)),
    ("JR-EM-LC2", "Riverfront & Ashram Road", (1006, 1001, 1015, 1007)),
)
_PATROL_SPEED_KMH = 18.0

#: 108 units of the emulated ambulance pool (deck /driver unit format).
_AMBULANCES = tuple(f"108-EM{i:02d}" for i in range(1, 7))
_URGENCIES = ("cardiac", "trauma", "maternity", "stroke", "respiratory")

#: Cadences (minutes).
_AMBULANCE_EVERY_MIN = 24.0       # + 0..3 min jitter: always < 30 min apart
_VIOLATOR_EVERY_MIN = 15.0        # + 0..4 min jitter

#: Retry cadence (seconds) for lifecycle calls / a ledger row an outage
#: left undone.
_HEAL_SEC = 30.0

#: A 108 request closed under an officer's queue is re-filed for this long
#: after 108 first asked (an ambulance call does not wait for ever).
_REFILE_WINDOW_MIN = 20.0

#: Requests remembered (by dispatch id) for re-filing; older ones are
#: decided or moot.
_REQUEST_MEMORY = timedelta(hours=3)


# ── program ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Slot:
    slot_id: str
    lane: int
    kind: str
    site_id: str
    title: str
    event_kind: str
    crowd_class: str
    closure_type: str
    route_segment_ids: tuple[int, ...]
    junction_ids: tuple[int, ...]
    starts_at: datetime
    ends_at: datetime
    severity: float
    weather: dict | None = None

    def active_at(self, t: datetime) -> bool:
        return self.starts_at <= t < self.ends_at

    def to_dict(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "slot_id": self.slot_id, "lane": self.lane, "kind": self.kind,
            "site_id": self.site_id, "title": self.title,
            "event_kind": self.event_kind, "crowd_class": self.crowd_class,
            "closure_type": self.closure_type,
            "route_segment_ids": list(self.route_segment_ids),
            "junction_ids": list(self.junction_ids),
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "severity": self.severity,
        }
        if self.weather is not None:
            doc["weather"] = self.weather
        return doc


@dataclass(frozen=True)
class AmbulanceRequest:
    at: datetime
    unit_id: str
    urgency: str
    slot_id: str | None
    waypoint_junction: int | None

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "unit_id": self.unit_id, "urgency": self.urgency,
                "slot_id": self.slot_id, "waypoint_junction": self.waypoint_junction}


@dataclass(frozen=True)
class Violation:
    at: datetime
    kind: str                     # 'speed' | 'red_light'
    pick: int                     # deterministic camera / plate pick

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "kind": self.kind, "pick": self.pick}


@dataclass(frozen=True)
class DayProgram:
    date_ist: date
    city_seed: int
    day_seed: int
    slots: tuple[Slot, ...]
    ambulance_requests: tuple[AmbulanceRequest, ...]
    violations: tuple[Violation, ...]

    @property
    def day_start(self) -> datetime:
        return datetime.combine(self.date_ist, dtime(0), IST).astimezone(timezone.utc)

    def active_at(self, t: datetime) -> list[Slot]:
        return [s for s in self.slots if s.active_at(t)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "living_city",
            "date_ist": self.date_ist.isoformat(),
            "city_seed": self.city_seed,
            "day_seed": self.day_seed,
            "slots": [s.to_dict() for s in self.slots],
            "ambulance_requests": [a.to_dict() for a in self.ambulance_requests],
            "violations": [v.to_dict() for v in self.violations],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


#: Day seeds stay below 2**53, so a browser reads the JSON number exactly.
_JS_SAFE_INT = (1 << 53) - 1


def day_seed_for(city_seed: int, date_ist: date) -> int:
    return derive_seed("living-city", int(city_seed), date_ist.isoformat()) & _JS_SAFE_INT


def _monsoon(d: date) -> bool:
    return 6 <= d.month <= 9


def _kind_weights(hour: float, d: date) -> dict[str, float]:
    """Kind weights at local hour ``hour`` of IST date ``d`` — the rhythm of
    an Ahmedabad day: dawn vegetable markets, the school runs, the evening
    bazaar and night food markets, trucks on the ring road at night, and
    rain cells only in their season."""
    night = hour >= 22 or hour < 5
    w = {
        "breakdown": 3.0 if night else (2.5 if 7 <= hour < 10 else 2.0),
        "signal_fault": 0.8 if night else 1.0,
    }
    if 5 <= hour < 9:
        market = 2.0                          # dawn vegetable markets
    elif 9 <= hour < 17:
        market = 1.8
    elif 17 <= hour < 22:
        market = 3.2                          # evening bazaar
    elif hour >= 22 or hour < 2:
        market = 2.0                          # night food markets
    else:
        market = 0.0
    w["market_spillover"] = market
    school_day = d.weekday() < 6            # Monday-Saturday
    school_run = 7 <= hour < 8.5 or 12 <= hour < 14 or 16 <= hour < 18
    w["school_egress"] = (3.0 if hour < 9 else 4.0) if school_day and school_run else 0.0
    if _monsoon(d):
        rain = 3.5 if 14 <= hour < 21 else 2.5
    elif d.month == 10 and d.day <= 15:
        rain = 0.8                           # retreating monsoon
    elif d.month in (11, 12, 1, 2):
        rain = 0.25                          # unseasonal shower
    else:
        rain = 0.0                           # pre-monsoon heat
    w["monsoon_rain"] = rain
    return w


def _weighted_pick(rng: SeededRng, weights: dict[str, float]) -> str:
    items = sorted((k, v) for k, v in weights.items() if v > 0)
    total = sum(v for _k, v in items)
    x = rng.uniform(0.0, total)
    acc = 0.0
    for k, v in items:
        acc += v
        if x < acc:
            return k
    return items[-1][0]


def _shuffled(rng: SeededRng, seq: tuple[Site, ...]) -> list[Site]:
    out = list(seq)
    for i in range(len(out) - 1, 0, -1):
        j = int(rng.uniform(0.0, i + 1))
        out[i], out[j] = out[j], out[i]
    return out


def _title(kind: str, site: Site, start_local_hour: float, d: date) -> str:
    if kind == "breakdown":
        return f"{site.detail} · {site.name}"
    if kind == "signal_fault":
        return f"Signal fault · {site.name} (manual control)"
    if kind == "market_spillover":
        h = start_local_hour
        label = ("Night market spill" if h >= 21 or h < 2
                 else "Morning vegetable market" if 5 <= h < 9
                 else "Evening bazaar rush" if h >= 17
                 else "Market spillover")
        return f"{label} · {site.name}"
    if kind == "school_egress":
        label = "School drop-off rush" if start_local_hour < 10 else "School dismissal surge"
        return f"{label} · {site.name}"
    if kind == "monsoon_rain":
        cell = "Monsoon burst" if _monsoon(d) or d.month == 10 else "Unseasonal shower"
        return f"{cell} · waterlogging on {site.name}"
    return site.name


def build_day_program(city_seed: int, date_ist: date) -> DayProgram:
    """The day's ambient program — pure: f(city_seed, IST date)."""
    seed = day_seed_for(city_seed, date_ist)
    rng = SeededRng(seed)
    day_start = datetime.combine(date_ist, dtime(0), IST).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    slots: list[Slot] = []
    used: dict[str, int] = {}

    def overlaps(start: datetime, end: datetime, segs: tuple[int, ...]) -> bool:
        return any(s.starts_at < end and start < s.ends_at
                   and set(s.route_segment_ids) & set(segs) for s in slots)

    for lane in range(3):
        cursor = day_start
        if lane == 2:
            cursor += timedelta(minutes=rng.uniform(20.0, 60.0))
        prev_kind: str | None = None
        prev_site: str | None = None
        index = 0
        while cursor < day_end - timedelta(minutes=10):
            local = cursor.astimezone(IST)
            hour = local.hour + local.minute / 60.0
            weights = _kind_weights(hour, date_ist)
            if prev_kind in weights:
                weights[prev_kind] *= 0.3          # vary the lane's story
            chosen: tuple[str, Site, datetime] | None = None
            for _attempt in range(6):
                kind = _weighted_pick(rng, weights)
                lo, hi = SLOT_KINDS[kind].duration_min
                if kind == "market_spillover" and (hour >= 22 or hour < 2):
                    lo, hi = _NIGHT_MARKET_MIN
                minutes = rng.uniform(20.0, 45.0) if (lane == 1 and index == 0) \
                    else rng.uniform(lo, hi)
                end = min(cursor + timedelta(minutes=minutes), day_end)
                ranked = sorted(_shuffled(rng, _SITES_BY_KIND[kind]),
                                key=lambda st: used.get(st.site_id, 0))
                for site in ranked:     # least-used first: the city varies
                    if site.site_id != prev_site and not overlaps(cursor, end, site.segments):
                        chosen = (kind, site, end)
                        break
                if chosen:
                    break
                weights[kind] = 0.0
                if not any(v > 0 for v in weights.values()):
                    break
            if chosen is None:              # every site busy: idle 10 min
                if lane < 2 and slots and slots[-1].lane == lane:
                    # an always-on lane never goes dark: hold its cause on
                    slots[-1] = replace(slots[-1], ends_at=min(
                        slots[-1].ends_at + timedelta(minutes=10), day_end))
                    cursor = slots[-1].ends_at
                else:
                    cursor += timedelta(minutes=10)
                continue
            kind, site, end = chosen
            if lane < 2 and day_end - end < timedelta(minutes=15):
                end = day_end                # an always-on lane runs to midnight
            sk = SLOT_KINDS[kind]
            severity = round(rng.uniform(0.4, 0.9), 2)
            weather = None
            if kind == "monsoon_rain":
                mm = round(rng.uniform(8.0, 35.0), 1)
                weather = {"code": "RAIN", "rain_intensity_mm_h": mm,
                           "severity": round(min(1.0, mm / 30.0), 3)}
            crowd = sk.crowd_class
            if kind == "market_spillover" and not (17 <= hour < 23):
                crowd = "MEDIUM"
            slots.append(Slot(
                slot_id=f"{date_ist:%Y%m%d}-L{lane}-{index:02d}",
                lane=lane, kind=kind, site_id=site.site_id,
                title=_title(kind, site, hour, date_ist),
                event_kind=sk.event_kind, crowd_class=crowd,
                closure_type=sk.closure_type,
                route_segment_ids=site.segments, junction_ids=site.junctions,
                starts_at=cursor, ends_at=end, severity=severity, weather=weather,
            ))
            index += 1
            used[site.site_id] = used.get(site.site_id, 0) + 1
            prev_kind, prev_site = kind, site.site_id
            cursor = end
            if lane == 2:
                cursor += timedelta(minutes=rng.uniform(30.0, 80.0))

    slots.sort(key=lambda s: (s.starts_at, s.lane))

    # 108 requests: ~every 25 min, aimed at a slot open at that moment
    ambulances: list[AmbulanceRequest] = []
    t = day_start + timedelta(minutes=7.0)
    k = 0
    while t < day_end:
        at = t + timedelta(minutes=rng.uniform(0.0, 3.0))
        open_slots = [s for s in slots if s.active_at(at)]
        target = open_slots[int(rng.uniform(0.0, len(open_slots)))] if open_slots else None
        waypoint = None
        if target is not None and target.junction_ids:
            signalised = [j for j in target.junction_ids if j in _SIGNALISED]
            waypoint = (signalised or list(target.junction_ids))[0]
        ambulances.append(AmbulanceRequest(
            at=at, unit_id=_AMBULANCES[k % len(_AMBULANCES)],
            urgency=_URGENCIES[int(rng.uniform(0.0, len(_URGENCIES)))],
            slot_id=target.slot_id if target else None, waypoint_junction=waypoint,
        ))
        k += 1
        t += timedelta(minutes=_AMBULANCE_EVERY_MIN)

    # rare deliberate violators: one every ~15-19 minutes
    violations: list[Violation] = []
    t = day_start + timedelta(minutes=4.0)
    while t < day_end:
        at = t + timedelta(minutes=rng.uniform(0.0, 4.0))
        kind = "red_light" if rng.uniform() < 0.5 else "speed"
        violations.append(Violation(at=at, kind=kind, pick=int(rng.uniform(0.0, 1e9))))
        t += timedelta(minutes=_VIOLATOR_EVERY_MIN)

    return DayProgram(
        date_ist=date_ist, city_seed=int(city_seed), day_seed=seed,
        slots=tuple(slots), ambulance_requests=tuple(ambulances),
        violations=tuple(violations),
    )


# ── background trickle (pure per tick) ──────────────────────────────────────

_VEHICLE_CLASSES = (("car", 55), ("two_wheeler", 25), ("auto_rickshaw", 10),
                    ("truck", 6), ("bus", 4))
_RESERVED_PLATES = frozenset({"GJ27EM9001", "GJ27EM4321"})  # watchlist demo plates


def _h(*parts: object) -> int:
    return int.from_bytes(
        hashlib.blake2b("|".join(str(p) for p in parts).encode("utf-8"), digest_size=8).digest(),
        "big",
    )


def _plate(city_seed: int, n: int) -> tuple[str, str]:
    """Vehicle n of the city's emulated fleet: (plate, vehicle class)."""
    for salt in range(8):
        u = _h("living-plate", city_seed, n, salt)
        rto = ("01", "01", "01", "27", "18")[u % 5]
        plate = f"GJ{rto}EM{1000 + (u >> 8) % 9000}"
        if plate not in _RESERVED_PLATES:
            break
    pick = (u >> 32) % 100
    acc = 0
    vclass = "car"
    for name, share in _VEHICLE_CLASSES:
        acc += share
        if pick < acc:
            vclass = name
            break
    return plate, vclass


_FLEET = 480


@dataclass(frozen=True)
class TrickleRead:
    plate: str
    vehicle_class: str
    camera_rank: int              # start of the deterministic camera rotation
    speed_u: int                  # compliant-speed draw
    direction_u: int              # two-way roads: which way


def trickle_read(city_seed: int, day_seed: int, tick: int) -> TrickleRead:
    """The background read of one supervisor tick (pure)."""
    u = _h("living-trickle", day_seed, tick)
    plate, vclass = _plate(city_seed, u % _FLEET)
    return TrickleRead(plate=plate, vehicle_class=vclass, camera_rank=(u >> 12) % 997,
                       speed_u=(u >> 24) % 100_000, direction_u=(u >> 44) % 2)


def _site_state(site: dict, epoch_s: float) -> str:
    """green | amber | red | none — the approach movement's state at a
    stop-line site (none: no program, or no decision possible)."""
    sig = site.get("signal")
    if not sig or not sig.get("movement"):
        return "none"
    states = movement_states_at(sig["cycle_sec"], sig.get("offset_sec", 0),
                                sig["phases"], epoch_s)
    return states.get(str(sig["movement"]), "none")


def _site_heading(site: dict, direction_u: int) -> float | None:
    if site.get("heading_deg") is not None:
        return round(float(site["heading_deg"]), 1)
    bearing = site.get("bearing_deg")
    if bearing is None:
        return None
    if not site.get("oneway") and direction_u:
        bearing = (float(bearing) + 180.0) % 360.0
    return round(float(bearing), 1)


# ── the supervisor ──────────────────────────────────────────────────────────


@dataclass
class _OpenSlot:
    slot: Slot
    event_id: str
    opened_at: datetime
    active: bool = False          # the C-F 'active' transition landed


@dataclass
class _Request:
    """One 108 request the city filed — what a re-filing needs."""

    unit_id: str
    urgency: str
    first_at: datetime            # when 108 first asked (the program time)
    filed_at: datetime
    event_id: str
    filings: int = 1
    closed_by: str | None = None  # why its event closed under it
    dispatch_id: str | None = None


@dataclass
class _Session:
    run_id: int | None
    date_ist: date
    program: DayProgram
    since: datetime
    last_tick: datetime
    opened: dict[str, _OpenSlot] = field(default_factory=dict)
    closed: set[str] = field(default_factory=set)        # window over (never reopens)
    preempted: dict[str, int] = field(default_factory=dict)   # slot -> last pre-empting run
    yielded: set[str] = field(default_factory=set)       # pre-empted, may resume
    resumes: dict[str, int] = field(default_factory=dict)     # slot -> times pre-empted
    unclosed: dict[str, str] = field(default_factory=dict)    # event_id -> close note
    pending_red: list[Violation] = field(default_factory=list)


class LivingCity:
    """The ambient-city supervisor (one per process)."""

    def __init__(
        self,
        deps: RunnerDeps,
        *,
        city_seed: int = CITY_SEED_DEFAULT,
        enabled: bool = False,
        reserved_routes: Callable[[], dict[int, tuple[str, frozenset[int]]]] | None = None,
        tick_sec: float = TICK_SEC,
        now: Callable[[], datetime] = utcnow,
        violators: bool = True,
    ) -> None:
        self._deps = deps
        self.city_seed = int(city_seed)
        self.enabled = bool(enabled)
        # drive the program's deliberate violators (each one a PENDING
        # e-challan recommendation an officer must review); off: the
        # program is unchanged, its violators are counted, not driven
        self.violators = bool(violators)
        self._reserved = reserved_routes or (lambda: {})
        self._tick_sec = tick_sec
        self._now = now
        self._task: asyncio.Task | None = None
        self._session: _Session | None = None
        self._lock = asyncio.Lock()
        self._programs: dict[date, DayProgram] = {}
        self._cameras: list[dict] = []
        self._cameras_loaded: float = 0.0
        self._excluded_cameras: set[str] = set()
        self._paths: dict[str, list[tuple[float, float]]] = {}
        # the store's network key the patrol lines were chained under
        self._paths_key: Any = None
        self._dispatched: set[str] = set()
        self._dispatched_checked: float = 0.0
        self._last_heartbeat: float = 0.0
        self._healed_at: datetime | None = None
        # 108 requests by dispatch id, and those to re-file (they outlive a
        # session: the IST rollover re-files onto the next day's slots)
        self._requests: dict[str, _Request] = {}
        self._refile: list[_Request] = []
        self._unknown_segments: frozenset[int] = frozenset()
        self._network_checked = False
        self._network_problem: str | None = None
        self.counters: Counter = Counter()
        self.last_error: str | None = None
        self.detail: str | None = None

    # -- program -------------------------------------------------------------

    def program_for(self, d: date) -> DayProgram:
        if d not in self._programs:
            self._programs = {k: v for k, v in self._programs.items() if k >= d - timedelta(days=1)}
            self._programs[d] = build_day_program(self.city_seed, d)
        return self._programs[d]

    @staticmethod
    def ist_date(t: datetime) -> date:
        return t.astimezone(IST).date()

    # -- status --------------------------------------------------------------

    @property
    def running(self) -> bool:
        """A session is open (start() opens it with its tick loop; stop()
        and shutdown close both together)."""
        return self._session is not None

    @property
    def run_id(self) -> int | None:
        return self._session.run_id if self._session else None

    def owned_run_ids(self) -> set[int]:
        rid = self.run_id
        return {rid} if rid is not None and self.running else set()

    def status_doc(self) -> dict[str, Any]:
        now = self._now()
        sess = self._session if self.running else None
        program = sess.program if sess else self.program_for(self.ist_date(now))
        active = []
        if sess:
            for o in sorted(sess.opened.values(), key=lambda o: o.slot.starts_at):
                active.append({
                    "slot_id": o.slot.slot_id, "kind": o.slot.kind, "title": o.slot.title,
                    "event_id": o.event_id, "starts_at": o.slot.starts_at.isoformat(),
                    "ends_at": o.slot.ends_at.isoformat(),
                    "route_segment_ids": list(o.slot.route_segment_ids),
                    "event_status": "active" if o.active else "planning",
                })
        upcoming: list[dict[str, Any]] = []
        if sess:
            horizon = now + timedelta(minutes=90)
            programs = [program]
            if horizon > program.day_start + timedelta(days=1):
                # the look-ahead crosses IST midnight: tomorrow's program too
                programs.append(self.program_for(program.date_ist + timedelta(days=1)))
            for prog in programs:
                for s in prog.slots:
                    if now <= s.starts_at < horizon and s.slot_id not in sess.closed:
                        upcoming.append({"at": s.starts_at.isoformat(), "kind": s.kind,
                                         "title": s.title})
                for a in prog.ambulance_requests:
                    if now <= a.at < horizon:
                        upcoming.append({
                            "at": a.at.isoformat(), "kind": "ambulance_request",
                            "title": f"108 green-corridor request ({a.urgency}) · {a.unit_id}"})
            upcoming.sort(key=lambda c: c["at"])
        return {
            "enabled": self.enabled,
            "status": "running" if sess else "stopped",
            "date_ist": program.date_ist.isoformat(),
            "day_seed": program.day_seed,
            "city_seed": self.city_seed,
            "run_id": sess.run_id if sess else None,
            "active_slots": active,
            "next_causes": upcoming[:8],
            "units": [p[0] for p in PATROLS],
            "violators": self.violators,
            "since": sess.since.isoformat() if sess else None,
            "program": {"slots": len(program.slots),
                        "ambulance_requests": len(program.ambulance_requests),
                        "violations": len(program.violations)},
            "preempted": dict(sess.preempted) if sess else {},
            "yielded": sorted(sess.yielded) if sess else [],
            "refiling": [{"unit_id": r.unit_id, "urgency": r.urgency,
                          "first_at": r.first_at.isoformat(), "closed_by": r.closed_by,
                          "replaces": r.dispatch_id} for r in self._refile],
            "counters": dict(self.counters),
            "excluded_cameras": sorted(self._excluded_cameras),
            "detail": self.detail,
            "last_error": self.last_error,
        }

    def health_doc(self) -> dict[str, Any]:
        return {"enabled": self.enabled,
                "status": "running" if self.running else "stopped",
                "active_slots": len(self._session.opened) if self._session and self.running else 0,
                "detail": self.detail}

    def delivered(self) -> int:
        c = self.counters
        return (c["events_opened"] + c["reads_delivered"] + c["units_published"]
                + c["dispatch_requested"] + c["dispatch_refiled"])

    def attempted(self) -> int:
        c = self.counters
        return (c["events_opened"] + c["events_open_failed"] + c["reads_delivered"]
                + c["reads_failed"] + c["units_published"] + c["units_failed"]
                + c["dispatch_requested"] + c["dispatch_failed"]
                + c["dispatch_refiled"] + c["dispatch_refile_failed"])

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        async with self._lock:
            self.enabled = True
            if self.running:
                return self.status_doc()
            await self._open_session(self._now())
            self._task = asyncio.create_task(self._loop(), name="living-city")
        return self.status_doc()

    async def stop(self, reason: str = "stopped") -> dict[str, Any]:
        async with self._lock:
            if reason == "stopped":
                self.enabled = False
            task, self._task = self._task, None
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if self._session is not None:
                note = {"stopped": "living city stopped by operator",
                        "interrupted": "living city interrupted (emulator shut down)"}.get(
                            reason, f"living city {reason}")
                await self._close_session(reason, note)
            # the city is off: nothing re-files (its requests end cancelled,
            # each with the oracle's audit row)
            self._refile.clear()
            self._requests.clear()
        return self.status_doc()

    async def on_run_started(self, run_task: Any) -> None:
        """A manual / flagship run took a route: yield it at once."""
        if self.running and self._session is not None:
            async with self._lock:
                await self._preempt(self._session)

    async def _ledger_session(self, d: date, program: DayProgram, since: datetime) -> int | None:
        """The session's ledger row (its tape is the day program); None
        when the store cannot take it now (the tick retries)."""
        store = self._deps.store
        if store is None:
            return None
        try:
            return await store.insert_run(
                use_case_id="living_city",
                spec={"mode": "living_city", "city_seed": self.city_seed,
                      "date_ist": d.isoformat(), "day_seed": program.day_seed,
                      "tick_sec": self._tick_sec, "clock_scale": 1.0,
                      "counters": {}},
                spec_hash=hashlib.blake2b(
                    f"living_city|{self.city_seed}|{d.isoformat()}".encode("utf-8"),
                    digest_size=16).hexdigest(),
                seed=program.day_seed,
                t0_anchor=since,
                clock_scale=1.0,
                tape_json=program.to_json(),
                is_replay=False,
                replay_of_run_id=None,
            )
        except Exception as exc:  # noqa: BLE001 - the city runs unledgered for now
            self.last_error = f"ledger: {exc}"
            logger.error("living_city.ledger_failed", error=str(exc))
            return None

    async def _check_network(self) -> None:
        """The sites' and beats' segment / junction ids against the live
        seed network (once per process; retried by the heal pass while the
        store cannot answer).  A mismatch fails loudly; slots on unknown
        roads never open."""
        store = self._deps.store
        if self._network_checked or store is None:
            return
        segs = sorted({s for site in SITES for s in site.segments}
                      | {s for _u, _b, route in PATROLS for s in route})
        try:
            adjacency = await store.segment_junction_ids()
        except Exception as exc:  # noqa: BLE001 - checked again by the heal pass
            self.last_error = f"network check: {exc}"
            return
        self._network_checked = True
        missing = [s for s in segs if s not in adjacency]
        self._unknown_segments = frozenset(missing)
        problems: list[str] = []
        if missing:
            problems.append(f"segments {missing} are not in the road graph (segment_adjacency)")
        stray = [f"{site.site_id}:J{j}" for site in SITES for j in site.junctions
                 if not any(j in adjacency.get(s, ()) for s in site.segments)]
        if stray:
            problems.append(f"junctions not adjacent to their site's roads: {stray}")
        if problems:
            self._network_problem = ("seed network mismatch — " + "; ".join(problems)
                                     + " (slots on unknown roads are skipped)")
            logger.error("living_city.network_mismatch", problems=problems)
        else:
            self._network_problem = None

    async def _open_session(self, now: datetime) -> None:
        d = self.ist_date(now)
        program = self.program_for(d)
        self.counters = Counter()
        await self._check_network()
        self.detail = self._network_problem
        run_id = await self._ledger_session(d, program, now)
        self._session = _Session(run_id=run_id, date_ist=d, program=program,
                                 since=now, last_tick=now)
        self._healed_at = now
        await self._close_orphans(run_id)
        logger.info("living_city.session_open", run_id=run_id, date_ist=d.isoformat(),
                    slots=len(program.slots), day_seed=program.day_seed)

    async def _close_orphans(self, run_id: int | None) -> None:
        """Living-city events of an earlier session (a restart): close them
        now rather than leave a doubled city until the reaper catches up."""
        store = self._deps.store
        if store is None:
            return
        try:
            events = await store.live_emulator_events()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"orphan scan: {exc}"
            return
        for ev in events:
            if ev.get("mode") != "living_city":
                continue
            if run_id is not None and str(ev.get("run_id")) == str(run_id):
                continue
            entry = await close_event(self._deps.oracle, str(ev["event_id"]),
                                      "living-city event of an earlier session — closed at restart")
            self.counters["orphans_closed" if entry["result"] == "delivered"
                          else "orphans_close_failed"] += 1

    async def _close_session(self, status: str, note: str) -> None:
        sess = self._session
        if sess is None:
            return
        for slot_id in list(sess.opened):
            await self._close_slot(sess, slot_id, note)
        for event_id, why in list(sess.unclosed.items()):
            # one last try; what still fails is the reaper's (the run ends)
            entry = await close_event(self._deps.oracle, event_id, why)
            if entry["result"] == "delivered":
                del sess.unclosed[event_id]
                self.counters["events_closed"] += 1
                self._note_released(entry, why)
        if self._deps.store is not None and sess.run_id is not None:
            try:
                await self._deps.store.finish_run(sess.run_id, status, {
                    "counters": dict(self.counters),
                    "delivery": {"delivered": self.delivered(),
                                 "attempted": self.attempted()},
                    "preempted": dict(sess.preempted),
                    "excluded_cameras": sorted(self._excluded_cameras),
                    "reason": note,
                })
            except Exception as exc:  # noqa: BLE001
                logger.error("living_city.finish_failed", error=str(exc))
        self._session = None

    # -- the loop ------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                async with self._lock:
                    await self.tick(self._now())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the city never dies on a tick
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.error("living_city.tick_failed", error=self.last_error)
            await asyncio.sleep(max(0.2, self._tick_sec - (time.monotonic() - started)))

    async def tick(self, now: datetime) -> None:
        """One supervisor step at wall time ``now`` (also driven by tests)."""
        sess = self._session
        if sess is None:
            return
        if self.ist_date(now) != sess.date_ist:      # IST midnight: next day
            await self._close_session("completed", "IST day ended — next day's program begins")
            await self._open_session(now)
            sess = self._session
            assert sess is not None
        await self._heal(sess, now)
        await self._preempt(sess)
        await self._slots(sess, now)
        await self._patrols(now)
        await self._trickle(sess, now)
        await self._violators(sess, now)
        await self._ambulances(sess, now)
        sess.last_tick = now
        await self._heartbeat(sess)

    # -- slots ---------------------------------------------------------------

    def _blocked_by(self, slot: Slot) -> int | None:
        for rid, (_uc, segs) in self._reserved().items():
            if segs & set(slot.route_segment_ids):
                return rid
        return None

    async def _heal(self, sess: _Session, now: datetime) -> None:
        """Retry what an outage left undone (every _HEAL_SEC): the session's
        ledger row, 'active' for opened slots, closes that failed."""
        if self._healed_at is not None and (now - self._healed_at).total_seconds() < _HEAL_SEC:
            return
        self._healed_at = now
        if not self._network_checked:
            await self._check_network()
            if self._network_problem:
                self.detail = self._network_problem
        if sess.run_id is None and self._deps.store is not None:
            sess.run_id = await self._ledger_session(sess.date_ist, sess.program, sess.since)
            if sess.run_id is not None:
                self.counters["session_ledgered_late"] += 1
        for o in list(sess.opened.values()):
            if o.active:
                continue
            entry = await activate_event(
                self._deps.oracle, o.event_id, f"living-city slot {o.slot.slot_id} window open")
            if entry["result"] == "delivered":
                o.active = True
                self.counters["events_activated_late"] += 1
            elif entry.get("terminal"):
                # an officer closed it before the city could activate it:
                # the slot is over (never re-opened, nothing to close)
                sess.opened.pop(o.slot.slot_id, None)
                sess.closed.add(o.slot.slot_id)
                self.counters["events_closed_by_officer"] += 1
        for event_id, note in list(sess.unclosed.items()):
            entry = await close_event(self._deps.oracle, event_id, note)
            if entry["result"] == "delivered":
                del sess.unclosed[event_id]
                self.counters["events_closed"] += 1
                self._note_released(entry, note)

    async def _preempt(self, sess: _Session) -> None:
        for slot_id, open_slot in list(sess.opened.items()):
            rid = self._blocked_by(open_slot.slot)
            if rid is None:
                continue
            uc = self._reserved().get(rid, ("?", frozenset()))[0]
            sess.preempted[slot_id] = rid
            sess.resumes[slot_id] = sess.resumes.get(slot_id, 0) + 1
            await self._close_slot(
                sess, slot_id,
                f"pre-empted by emulator run {rid} ({uc}); resumes when the run ends",
                final=False)
            self.counters["slots_preempted"] += 1

    async def _slots(self, sess: _Session, now: datetime) -> None:
        for slot_id in list(sess.opened):
            if not sess.opened[slot_id].slot.active_at(now):
                await self._close_slot(sess, slot_id, "living-city slot window ended")
        if sess.yielded:
            # a pre-empted slot whose window closed meanwhile never resumes
            ended = {s.slot_id for s in sess.program.slots
                     if s.slot_id in sess.yielded and not s.active_at(now)}
            sess.yielded -= ended
            sess.closed |= ended
        for slot in sess.program.active_at(now):
            if slot.slot_id in sess.opened or slot.slot_id in sess.closed:
                continue
            if self._unknown_segments & set(slot.route_segment_ids):
                # a road the live network does not have (the network check
                # said so loudly): never an event on it
                sess.closed.add(slot.slot_id)
                self.counters["slots_unknown_roads"] += 1
                continue
            if self._blocked_by(slot) is not None:
                continue
            await self._open_slot(sess, slot, now)

    def _event_id(self, sess: _Session, slot: Slot) -> str:
        """One event per (slot, session) — and a fresh one per resumption
        after a pre-emption (the pre-empted event is completed)."""
        key = f"{sess.date_ist.isoformat()}|{slot.slot_id}|run:{sess.run_id}"
        resumed = sess.resumes.get(slot.slot_id, 0)
        if resumed:
            key += f"|resume:{resumed}"
        return str(uuid.uuid5(_NS, key))

    async def _open_slot(self, sess: _Session, slot: Slot, now: datetime) -> None:
        store = self._deps.store
        if store is None:
            self.detail = "store not configured — slots cannot open"
            return
        event_id = self._event_id(sess, slot)
        sk = SLOT_KINDS[slot.kind]
        resumed = sess.resumes.get(slot.slot_id, 0)
        meta: dict[str, Any] = {
            "source": "event-emulator", "mode": "living_city", "slot": slot.slot_id,
            "run_id": sess.run_id, "lane": slot.lane, "cause": slot.kind,
            "site": slot.site_id, "severity": slot.severity,
            "city_seed": self.city_seed, "day_seed": sess.program.day_seed,
            "date_ist": sess.date_ist.isoformat(),
        }
        if slot.weather is not None:
            meta["weather"] = slot.weather
        if resumed:
            meta["resumed_after_run"] = sess.preempted.get(slot.slot_id)
        note = (f"living-city slot {slot.slot_id} resumed after emulator run "
                f"{sess.preempted.get(slot.slot_id)} (session run {sess.run_id})" if resumed
                else f"living-city slot {slot.slot_id} opened (session run {sess.run_id})")
        try:
            await store.upsert_corridor_event(
                event_id=event_id, movement_id=None, event_kind=slot.event_kind,
                protection_class="none", crowd_class=slot.crowd_class, title=slot.title,
                description=(f"EMULATED living-city cause — {sk.sentence}. Deterministic day "
                             f"program (city seed {self.city_seed}, IST {sess.date_ist}, "
                             f"slot {slot.slot_id})."),
                planned_start=slot.starts_at, planned_end=slot.ends_at,
                metadata_doc=meta,
                note=note,
            )
            await store.replace_event_route(event_id, list(slot.route_segment_ids))
            await store.replace_event_phase(
                event_id, label=slot.kind, window_start=slot.starts_at,
                window_end=slot.ends_at,
                closure_policy={"closure_type": slot.closure_type, "advance_closure_sec": 0},
            )
        except Exception as exc:  # noqa: BLE001
            self.counters["events_open_failed"] += 1
            self.last_error = f"open {slot.slot_id}: {exc}"
            return
        opened = _OpenSlot(slot=slot, event_id=event_id, opened_at=now)
        sess.opened[slot.slot_id] = opened
        sess.yielded.discard(slot.slot_id)
        entry = await activate_event(
            self._deps.oracle, event_id, f"living-city slot {slot.slot_id} window open")
        self.counters["events_opened"] += 1
        if resumed:
            self.counters["slots_resumed"] += 1
        if entry["result"] == "delivered":
            opened.active = True
        else:
            # stays 'planning' (still live, still labelled); _heal retries
            self.counters["events_activate_failed"] += 1
            self.last_error = f"activate {slot.slot_id}: {entry.get('reason')}"

    async def _close_slot(self, sess: _Session, slot_id: str, note: str,
                          final: bool = True) -> None:
        """Close a slot's event (C-F).  ``final`` False: pre-empted — the
        slot may reopen as a fresh event once its roads are free."""
        open_slot = sess.opened.pop(slot_id, None)
        if open_slot is None:
            return
        if final:
            sess.closed.add(slot_id)
        else:
            sess.yielded.add(slot_id)
        entry = await close_event(self._deps.oracle, open_slot.event_id, note)
        if entry["result"] == "delivered":
            self.counters["events_closed"] += 1
            self._note_released(entry, note)
        else:
            # retried by _heal; the reaper catches whatever outlives the run
            self.counters["events_close_failed"] += 1
            sess.unclosed[open_slot.event_id] = note
            self.last_error = f"close {slot_id}: {entry.get('reason')}"

    # -- patrols -------------------------------------------------------------

    async def _patrol_line(self, unit_id: str, route: tuple[int, ...]) -> list[tuple[float, float]]:
        """The beat's chained road line, re-chained when the store's
        network key changes (a network converted under a running city)."""
        store = self._deps.store
        if store is None:
            return self._paths.get(unit_id, [])
        network_key = getattr(store, "network_key", None)
        key = await network_key() if network_key is not None else None
        if key != self._paths_key:
            self._paths.clear()
            self._paths_key = key
        if unit_id not in self._paths:
            paths = await store.segment_paths(list(route))
            self._paths[unit_id] = chain_polyline(route, paths)
        return self._paths.get(unit_id, [])

    async def _patrols(self, now: datetime) -> None:
        nats = self._deps.nats
        if nats is None:
            return
        mono = time.monotonic()
        store = self._deps.store
        if store is not None and mono - self._dispatched_checked >= 15.0:
            self._dispatched_checked = mono
            try:
                self._dispatched = await store.dispatched_units([p[0] for p in PATROLS])
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"dispatch check: {exc}"
        day_start = datetime.combine(self.ist_date(now), dtime(0), IST)
        elapsed = (now - day_start).total_seconds()
        for i, (unit_id, beat, route) in enumerate(PATROLS):
            if unit_id in self._dispatched:
                self.counters["units_handed_over"] += 1
                continue
            try:
                line = await self._patrol_line(unit_id, route)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"patrol geometry: {exc}"
                continue
            if len(line) < 2:
                continue
            lon, lat, heading = pingpong_point(
                line, _PATROL_SPEED_KMH / 3.6 * elapsed + 1500.0 * i)
            doc = {
                "unit_id": unit_id, "agency": "jan_rakshak",
                "label": f"Jan Rakshak {unit_id} · {beat} beat",
                "position": [lon, lat], "heading": heading,
                "speed_kmh": _PATROL_SPEED_KMH, "status": "patrol",
                "t": now.isoformat(), "run_id": self.run_id, "source": "emulated",
            }
            ok = await nats.publish_json(UNITS_EMULATED_SUBJECT, doc)
            self.counters["units_published" if ok else "units_failed"] += 1

    # -- ANPR trickle + violators -------------------------------------------

    async def _camera_sites(self) -> list[dict]:
        store = self._deps.store
        mono = time.monotonic()
        if store is not None and (not self._cameras or mono - self._cameras_loaded > 60.0):
            # programs may change under the city signal writer: refresh
            try:
                self._cameras = await load_camera_context(store)
                self._cameras_loaded = mono
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"camera sites: {exc}"
        return [c for c in self._cameras if c["camera_id"] not in self._excluded_cameras]

    def _tick_index(self, sess: _Session, now: datetime) -> int:
        return int((now - sess.program.day_start).total_seconds() // self._tick_sec)

    async def _post_read(self, sess: _Session, site: dict, plate: str, vclass: str,
                         speed: float, heading: float | None, now: datetime,
                         read_key: str, planned: str | None) -> None:
        oracle = self._deps.oracle
        if oracle is None:
            return
        body: dict[str, Any] = {
            "read_id": str(uuid.uuid5(_NS, f"read|{read_key}|run:{sess.run_id}")),
            "plate": plate, "camera_id": site["camera_id"], "t": now.isoformat(),
            "lon": round(float(site["lon"]), 7), "lat": round(float(site["lat"]), 7),
            "speed_kmh": round(float(speed), 1), "heading": heading,
            "vehicle_class": vclass, "source": "emulated_anpr",
        }
        if sess.run_id is not None:
            body["run_id"] = sess.run_id
        try:
            resp = await oracle.post_sighting(body)
        except (httpx.HTTPError, UnsafeUrlError) as exc:
            self.counters["reads_failed"] += 1
            self.last_error = f"read: {type(exc).__name__}: {exc}"
            return
        if resp.status_code != 200:
            self.counters["reads_failed"] += 1
            self.last_error = f"read {resp.status_code}: {_detail(resp)}"
            return
        self.counters["reads_delivered"] += 1
        try:
            out = resp.json()
        except ValueError:
            out = {}
        if out.get("hit"):
            self.counters["reads_hit"] += 1
        fired = set(out.get("rules_fired") or [])
        # C-N: a vehicle reported stolen gets ONE R_STOLEN_SIGHTING per read
        # and its penal rules come back WITHHELD (no fine to the owner) — the
        # oracle still caught them, so the plan is checked against both
        stolen = out.get("hit") == "stolen_vehicle"
        withheld = set(out.get("withheld_rules") or [])
        caught = fired | withheld
        expected = {"speed": {"R_SPD_SEGMENT"}, "red_light": {"R_SIGNAL_VIOLATION"}}.get(
            planned or "", set())
        if planned:
            self.counters["violations_planned"] += 1
            if expected <= caught:
                self.counters["violations_flagged"] += 1
                if not expected <= fired:
                    self.counters["violations_withheld"] += 1
        if stolen and "R_STOLEN_SIGHTING" in fired:
            self.counters["stolen_sightings"] += 1
        unplanned = caught - expected - {"R_CLOSED_CORRIDOR_ENTRY"}
        if stolen:
            unplanned.discard("R_STOLEN_SIGHTING")  # expected for a stolen-earmarked plate
        for rule in sorted(unplanned):
            self.counters[f"unplanned_{rule}"] += 1
            if rule == "R_SIGNAL_VIOLATION":
                # the replica disagreed with the oracle at this site: keep
                # law-abiding traffic away from it for the rest of the run
                self._excluded_cameras.add(site["camera_id"])
                logger.warning("living_city.unplanned_signal_violation",
                               camera_id=site["camera_id"])
        if "R_CLOSED_CORRIDOR_ENTRY" in caught:
            self.counters["earmark_closure_entries"] += 1

    async def _trickle(self, sess: _Session, now: datetime) -> None:
        sites = await self._camera_sites()
        if not sites:
            return
        tick = self._tick_index(sess, now)
        r = trickle_read(self.city_seed, sess.program.day_seed, tick)
        epoch = now.timestamp()
        n = len(sites)
        for k in range(n):
            site = sites[(r.camera_rank + k) % n]
            # a law-abiding vehicle crosses a stop line only on green
            if _site_state(site, epoch) in ("none", "green"):
                break
        else:
            return
        speed = compliant_speed_kmh(site.get("speed_limit_kmh"), r.speed_u)
        await self._post_read(sess, site, r.plate, r.vehicle_class, speed,
                              _site_heading(site, r.direction_u), now,
                              f"{sess.date_ist}|tick:{tick}", None)

    async def _violators(self, sess: _Session, now: datetime) -> None:
        due = [v for v in sess.program.violations if sess.last_tick < v.at <= now]
        due += [v for v in sess.pending_red if v not in due]
        sess.pending_red = []
        if not due:
            return
        if not self.violators:
            self.counters["violations_suppressed"] += len(due)
            return
        sites = await self._camera_sites()
        epoch = now.timestamp()
        for v in due:
            plate, vclass = _plate(self.city_seed, v.pick % _FLEET)
            if v.kind == "red_light":
                reds = [s for s in sites if _site_state(s, epoch) == "red"]
                if not reds:
                    if now - v.at < timedelta(seconds=120):
                        sess.pending_red.append(v)     # wait for a red
                        continue
                else:
                    site = reds[v.pick % len(reds)]
                    speed = compliant_speed_kmh(site.get("speed_limit_kmh"), v.pick)
                    await self._post_read(sess, site, plate, vclass, speed,
                                          _site_heading(site, 0), now,
                                          f"{sess.date_ist}|violation:{v.at.isoformat()}",
                                          "red_light")
                    continue
            # speeding (or a red-light violator that found no red in 2 min)
            free = [s for s in sites if _site_state(s, epoch) == "none"
                    and s.get("speed_limit_kmh")]
            if not free:
                continue
            site = free[v.pick % len(free)]
            speed = float(site["speed_limit_kmh"]) + 14.0 + float(v.pick % 13)
            await self._post_read(sess, site, plate, vclass, speed,
                                  _site_heading(site, v.pick % 2), now,
                                  f"{sess.date_ist}|violation:{v.at.isoformat()}", "speed")

    # -- 108 requests --------------------------------------------------------

    @staticmethod
    def _waypoint(slot: Slot) -> int | None:
        """The green-corridor waypoint: the slot's first signalised junction."""
        if not slot.junction_ids:
            return None
        signalised = [j for j in slot.junction_ids if j in _SIGNALISED]
        return (signalised or list(slot.junction_ids))[0]

    def _note_released(self, entry: dict, why: str) -> None:
        """Requests the oracle cancelled with a closing event while they still
        waited for an officer (its C-F auto-close): queue them for re-filing.
        Decided requests (approved, en route …) are never re-filed."""
        for d in entry.get("closed_dispatches") or []:
            if d.get("from_status") != "requested":
                continue
            req = self._requests.pop(str(d.get("dispatch_id")), None)
            if req is None:
                continue
            req.closed_by = why
            self._refile.append(req)
            self.counters["dispatch_released"] += 1

    async def _file_request(self, target: _OpenSlot, unit_id: str, waypoint: int | None,
                            note: str) -> tuple[bool, str | None]:
        """POST one 108 request — a 'requested' row only a named duty officer
        can approve.  (filed?, its dispatch id)."""
        oracle = self._deps.oracle
        assert oracle is not None
        body: dict[str, Any] = {
            "agency": "ambulance", "unit_id": unit_id,
            "requested_by": "108 control (emulated)", "note": note[:500],
        }
        if waypoint is not None:
            body["waypoint_junction"] = waypoint
        try:
            resp = await oracle.request_dispatch(target.event_id, body)
        except (httpx.HTTPError, UnsafeUrlError) as exc:
            self.last_error = f"108: {type(exc).__name__}: {exc}"
            return False, None
        if resp.status_code != 200:
            self.last_error = f"108 {resp.status_code}: {_detail(resp)}"
            return False, None
        try:
            out = resp.json()
        except ValueError:
            out = {}
        did = out.get("dispatch_id") if isinstance(out, dict) else None
        return True, (str(did) if did else None)

    async def _refile_released(self, sess: _Session, now: datetime) -> None:
        """Re-file each released request onto an open slot (oldest slot id
        first), within _REFILE_WINDOW_MIN of 108's first ask; a request with
        no open slot to ride waits for the next tick."""
        if not self._refile:
            return
        keep: list[_Request] = []
        for req in self._refile:
            if now - req.first_at > timedelta(minutes=_REFILE_WINDOW_MIN):
                self.counters["dispatch_refile_expired"] += 1
                continue
            targets = sorted((o for o in sess.opened.values() if o.event_id != req.event_id),
                             key=lambda o: o.slot.slot_id)
            if not targets or self._deps.oracle is None:
                keep.append(req)
                continue
            target = targets[0]
            note = (f"green corridor · {req.urgency} · via {target.slot.title} — re-filed "
                    f"(filing {req.filings + 1}): request {req.dispatch_id} was cancelled with "
                    f"its event ({req.closed_by}) — EMULATED living-city 108 request")
            filed, did = await self._file_request(target, req.unit_id,
                                                  self._waypoint(target.slot), note)
            if not filed:
                self.counters["dispatch_refile_failed"] += 1
                keep.append(req)
                continue
            self.counters["dispatch_refiled"] += 1
            if did:
                self._requests[did] = replace(
                    req, filed_at=now, event_id=target.event_id, filings=req.filings + 1,
                    closed_by=None, dispatch_id=did)
        self._refile = keep

    async def _ambulances(self, sess: _Session, now: datetime) -> None:
        await self._refile_released(sess, now)
        due = [a for a in sess.program.ambulance_requests if sess.last_tick < a.at <= now]
        oracle = self._deps.oracle
        for a in due:
            target = sess.opened.get(a.slot_id) if a.slot_id else None
            if target is None and sess.opened:
                target = sorted(sess.opened.values(), key=lambda o: o.slot.slot_id)[0]
            if target is None or oracle is None:
                self.counters["dispatch_skipped"] += 1
                continue
            waypoint = a.waypoint_junction if target.slot.slot_id == a.slot_id else None
            if waypoint is None:
                waypoint = self._waypoint(target.slot)
            filed, did = await self._file_request(
                target, a.unit_id, waypoint,
                f"green corridor · {a.urgency} · via {target.slot.title} — "
                "EMULATED living-city 108 request")
            if not filed:
                self.counters["dispatch_failed"] += 1
                continue
            self.counters["dispatch_requested"] += 1
            if did:
                self._requests[did] = _Request(
                    unit_id=a.unit_id, urgency=a.urgency, first_at=a.at, filed_at=now,
                    event_id=target.event_id, dispatch_id=did)
        # requests long since decided (or moot) are forgotten
        cutoff = now - _REQUEST_MEMORY
        for did in [d for d, r in self._requests.items() if r.filed_at < cutoff]:
            del self._requests[did]

    # -- ledger heartbeat ----------------------------------------------------

    async def _heartbeat(self, sess: _Session) -> None:
        store = self._deps.store
        mono = time.monotonic()
        if store is None or sess.run_id is None or mono - self._last_heartbeat < 10.0:
            return
        self._last_heartbeat = mono
        try:
            await store.heartbeat_run(sess.run_id, {
                "counters": dict(self.counters),
                "active_slots": sorted(sess.opened),
                "progress": {"frame": self._tick_index(sess, sess.last_tick),
                             "total": int(86400 // self._tick_sec),
                             "delivered": self.delivered(),
                             "failed": self.attempted() - self.delivered()},
            })
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"heartbeat: {exc}"
