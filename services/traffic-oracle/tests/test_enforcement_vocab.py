"""R_SIGNAL_VIOLATION speaks the vocabulary the signal programs define.

Pure python, no DB.  The programs are the REAL 006 phase JSON (parsed out
of corridor-store/migrations/006_simulation_runs.sql) and the junction
approach geometry is the REAL seed network (parsed out of
corridor-store/seed/sample_corridor.sql), so these tests break the moment
either drifts from what the rules assume.

The bug this pins: the rules mapped headings onto compass codes NS/EW while
every seeded program lists through/cross/ramp/local, and a movement absent
from every phase was evaluated as red — R_SIGNAL_VIOLATION fired on 200 of
200 reads of a full-cycle sweep at junction 3.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from traffic_oracle import enforcement
from traffic_oracle.enforcement import engine, rules
from traffic_oracle.enforcement.rules import (
    JunctionLeg,
    SegmentContext,
    SightingContext,
    SignalContext,
    approach_movement,
    evaluate_rules,
    phase_state_at,
    read_approach,
    through_legs,
)
from traffic_oracle.signals.plans import AMBER_SEC

REPO = Path(__file__).resolve().parents[3]
M006 = REPO / "services" / "corridor-store" / "migrations" / "006_simulation_runs.sql"
SEED = REPO / "services" / "corridor-store" / "seed" / "sample_corridor.sql"

# The same row regexes the twin parser uses (simulation/network.py), with
# the oneway flag captured.
_SEG_RE = re.compile(
    r"\((\d{4}),\s*'([^']+)',\s*'(\w+)',\s*(\d+),\s*(\d+),\s*(TRUE|FALSE),\s*"
    r"ST_GeomFromText\('LINESTRING\(([^)]+)\)'"
)
_JUNCTION_RE = re.compile(
    r"\('(intersection|roundabout|flyover|underpass|toll_plaza|signal)',\s*"
    r"(TRUE|FALSE),\s*ST_GeomFromText\('POINT\(([^)]+)\)'"
)

_CLASS_ORDER = ("motorway", "trunk", "primary", "secondary")


# ── fixtures: real programs + real geometry ────────────────────────────────


def _templates() -> dict[str, dict]:
    """road class -> 006 program template (cycle + phases)."""
    text = M006.read_text()
    out = {
        cls: json.loads(js)
        for cls, js in re.findall(
            r"WHEN bool_or\(rs\.road_class = '(\w+)'\) THEN\s+'(\{.*?\})'::jsonb",
            text,
            re.S,
        )
    }
    other = re.search(r"ELSE\s+'(\{.*?\})'::jsonb", text, re.S)
    out["other"] = json.loads(other.group(1))
    return out


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Initial great-circle bearing a -> b, degrees (PostGIS geography
    ST_Azimuth agrees to well under 0.1 deg at this scale)."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return math.degrees(math.atan2(y, x)) % 360.0


def _dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _seed_network():
    sql = SEED.read_text()
    segments = {}
    for sid, name, rclass, _lanes, _speed, oneway, wkt in _SEG_RE.findall(sql):
        pts = [tuple(map(float, p.split())) for p in wkt.split(",")]
        segments[int(sid)] = (name, rclass, oneway == "TRUE", pts)
    junctions = {}
    for idx, (jtype, sig, pt) in enumerate(_JUNCTION_RE.findall(sql), start=1):
        lon, lat = map(float, pt.split())
        junctions[idx] = (jtype, sig == "TRUE", (lon, lat))
    return segments, junctions


SEGMENTS, JUNCTIONS = _seed_network()


def _legs(junction_id: int) -> tuple[JunctionLeg, ...]:
    """Every segment with an endpoint within 25 m of the junction, bearing
    pointing AWAY from it — the same rule engine._junction_legs applies."""
    _jtype, _sig, jpt = JUNCTIONS[junction_id]
    legs = []
    for sid, (name, rclass, _oneway, pts) in sorted(SEGMENTS.items()):
        if _dist_m(pts[0], jpt) <= 25.0:
            legs.append(JunctionLeg(sid, rclass, round(_bearing(pts[0], pts[1]), 2), name))
        elif _dist_m(pts[-1], jpt) <= 25.0:
            legs.append(JunctionLeg(sid, rclass, round(_bearing(pts[-1], pts[-2]), 2), name))
    return tuple(legs)


def _program_for(junction_id: int) -> SignalContext:
    """006's program choice (dominant leg class), 006's offset formula."""
    legs = _legs(junction_id)
    classes = {leg.road_class for leg in legs}
    cls = next((c for c in _CLASS_ORDER if c in classes), "other")
    tpl = _templates()[cls]
    cycle = int(tpl["cycle_sec"])
    return SignalContext(
        junction_id=junction_id,
        cycle_sec=cycle,
        phases=engine.parse_phases(tpl["phases"]),
        offset_sec=(junction_id * 17) % cycle,
        legs=legs,
    )


def _sighting(t: datetime, heading: float | None, **kw) -> SightingContext:
    base = dict(
        sighting_id=str(uuid.uuid4()), plate="GJ27EM9001", camera_id="EM-J03",
        t=t, lon=72.5810, lat=23.0264, source="emulated_anpr",
        speed_kmh=None, heading=heading,
    )
    base.update(kw)
    return SightingContext(**base)


# A 100 s boundary of the Unix epoch (2026-11-05T05:00:00Z).
BASE = datetime(2026, 11, 5, 5, 0, 0, tzinfo=timezone.utc)


# ── the vocabulary ──────────────────────────────────────────────────────────


def test_006_programs_speak_through_cross_ramp_local():
    templates = _templates()
    assert set(templates) == {"motorway", "trunk", "primary", "secondary", "other"}
    labels = {
        m for tpl in templates.values() for ph in tpl["phases"] for m in ph["movements"]
    }
    assert labels == {"through", "cross", "ramp", "local"}
    # phase 0 is the major road's movement in every template
    assert {tpl["phases"][0]["movements"][0] for tpl in templates.values()} == {
        "through", "local",
    }


def test_compass_codes_on_a_006_program_are_no_decision_not_red():
    """The original bug: a movement absent from every phase was 'red'."""
    sig = _program_for(3)
    for k in range(sig.cycle_sec):
        t = BASE + timedelta(seconds=k)
        assert phase_state_at(sig, "NS", t)[0] == "none"
        assert phase_state_at(sig, "EW", t)[0] == "none"


def test_without_approach_geometry_a_006_program_never_fires():
    sig = _program_for(3)
    bare = SignalContext(sig.junction_id, sig.cycle_sec, sig.phases, sig.offset_sec)
    fired = [
        r
        for k in range(bare.cycle_sec)
        for r in evaluate_rules(_sighting(BASE + timedelta(seconds=k), 78.0), None, bare)
        if r.rule_id == "R_SIGNAL_VIOLATION"
    ]
    assert fired == []


# ── approach geometry on the real seed junctions ───────────────────────────


# The seed is the REAL network (OpenStreetMap, WP-20): each junction sits on
# the OSM node of its intersection and every leg's bearing is the road's.
@pytest.mark.parametrize(
    "junction_id, expected",
    [
        (1, {1001, 1002}),   # Ashram Road S/N through, Nehru Bridge Road crosses
        (3, {1005, 1007}),   # Lal Darwaja: Relief Rd + Riverfront Rd (East) through;
                             # Nehru Bridge Rd / Dr. Ambedkar Rd (north) and Gandhi Rd cross
        (5, {1013, 1049}),   # Astodia Darwaja: Swami Vivekananda Rd + Sarangpur Rd,
                             # Kankaria Rd crosses
        (8, {1014, 1031}),   # Kalupur Station: Station Rd + Kasturba Gandhi Rd,
                             # Naroda Rd crosses
        (13, {1028, 1048}),  # Airport Road (North) runs on over the Indira Bridge
        (14, {1027, 1028}),  # Airport Circle: Airport Road S/N, Domestic Airport Rd crosses
    ],
)
def test_through_legs_on_seed_junctions(junction_id, expected):
    assert through_legs(_legs(junction_id)) == frozenset(expected)


def test_heading_maps_to_the_program_movement_at_lal_darwaja():
    sig = _program_for(3)
    a = approach_movement(268.0, sig)           # westbound on Relief Road into Lal Darwaja
    assert a is not None and a.movement == "through" and a.approach_segment_id == 1005
    b = approach_movement(92.0, sig)            # eastbound off the riverfront
    assert b is not None and b.movement == "through" and b.approach_segment_id == 1007
    c = approach_movement(193.0, sig)           # southbound from Nehru Bridge Road
    assert c is not None and c.movement == "cross" and c.approach_segment_id == 1015
    # Nehru Bridge Road 1015 and Dr. Ambedkar Road 1043 leave Lal Darwaja
    # along one 76 m stub: one movement (cross), and the read's POSITION says
    # which road it is on
    assert read_approach(193.0, 1043, sig).approach_segment_id == 1043
    assert read_approach(193.0, 1015, sig).approach_segment_id == 1015
    # a heading that matches no approach within 45 deg: no decision
    assert approach_movement(336.0, sig) is None
    assert approach_movement(None, sig) is None


def test_ambiguous_approach_is_no_decision():
    legs = (
        JunctionLeg(1, "trunk", 0.0),
        JunctionLeg(2, "trunk", 180.0),
        JunctionLeg(3, "secondary", 260.0),
        JunctionLeg(4, "secondary", 280.0),
    )
    sig = SignalContext(9, 100, ((47, ("through",)), (38, ("cross",))), 0, legs)
    # heading 90 matches leg 3 (inbound 80) and leg 4 (inbound 100) equally
    # well — both carry cross: the movement is unambiguous
    assert approach_movement(90.0, sig).movement == "cross"
    # ... and the read's POSITION says which of the two it is on
    assert read_approach(90.0, 3, sig).approach_segment_id == 3
    on4 = read_approach(90.0, 4, sig)
    assert on4.approach_segment_id == 4 and on4.movement == "cross"
    assert on4.approach_delta_deg == 10.0
    # a read on a through leg with that heading is not driving in on it
    assert read_approach(90.0, 1, sig) is None
    assert read_approach(90.0, 2, sig) is None
    # heading 300 is 60+ deg from every inbound direction: no approach
    assert approach_movement(300.0, sig) is None
    mixed = SignalContext(9, 100, sig.phases, 0, (
        JunctionLeg(1, "trunk", 0.0),
        JunctionLeg(2, "trunk", 180.0),
        JunctionLeg(3, "secondary", 15.0),  # inbound 195
    ))
    # heading 188: leg 1 inbound 180 (8 deg, through) vs leg 3 inbound 195
    # (7 deg, cross) — different movements within 10 deg: ambiguous
    assert approach_movement(188.0, mixed) is None
    # the band is checked against EVERY approach, not only the runner-up:
    # heading 84 -> leg 6 (1 deg, cross), leg 5 (4 deg, cross), and leg 7
    # (5 deg, THROUGH) all inside 10 deg of the best match: ambiguous
    crowded = SignalContext(9, 100, sig.phases, 0, (
        JunctionLeg(5, "secondary", 260.0),  # inbound 80
        JunctionLeg(6, "secondary", 265.0),  # inbound 85
        JunctionLeg(7, "trunk", 269.0),      # inbound 89, through with 8
        JunctionLeg(8, "trunk", 89.0),       # inbound 269
    ))
    assert through_legs(crowded.legs) == frozenset({7, 8})
    assert approach_movement(84.0, crowded) is None
    # away from the crowded band the mapping stands
    assert approach_movement(269.0, crowded).movement == "through"


def test_every_signalized_seed_junction_maps_every_approach():
    """Each approach of each signalized seed junction maps onto a movement
    its own 006 program defines (no orphan approaches); a read ON a leg
    driving in is a decision there, a read on a leg driving OUT never is.
    Two legs that leave a junction along one stub (a junction node a few
    metres off the carriageway on the real network, e.g. Nehru Bridge Road
    and Dr. Ambedkar Road at Lal Darwaja) share their inbound heading: the
    heading alone then names one movement for both, and the read's position
    names the leg."""
    for jid, (_jtype, signalized, _pt) in JUNCTIONS.items():
        if not signalized:
            continue
        sig = _program_for(jid)
        labels = {m for _g, mv in sig.phases for m in mv}
        for leg in sig.legs:
            inbound = leg.bearing_deg + 180.0
            twins = [o for o in sig.legs if o.segment_id != leg.segment_id
                     and abs(((o.bearing_deg - leg.bearing_deg + 540.0) % 360.0) - 180.0) < 10.0]
            a = approach_movement(inbound, sig)
            assert a is not None, (jid, leg)
            assert a.movement in labels
            if twins:
                assert a.approach_segment_id in {leg.segment_id, *(o.segment_id for o in twins)}
            else:
                assert a.approach_segment_id == leg.segment_id, (jid, leg)
            on = read_approach(inbound, leg.segment_id, sig)
            assert on is not None and on.approach_segment_id == leg.segment_id
            assert read_approach(leg.bearing_deg, leg.segment_id, sig) is None, (jid, leg)


# ── the acceptance sweep ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "heading, movement, approach", [(268.0, "through", 1005), (193.0, "cross", 1043)]
)
def test_junction3_full_cycle_sweep_fires_on_red_only(heading, movement, approach):
    """Two reads a second over one full cycle at junction 3 (the audit's
    sweep), each ON its approach leg: R_SIGNAL_VIOLATION fires exactly on
    the red share of the cycle — never on green, never inside the amber —
    instead of on every read."""
    sig = _program_for(3)
    # Lal Darwaja's approaches include primary Dr. Ambedkar Road 1043: 006's
    # primary template (110 s cycle), offset (3 x 17) mod 110
    assert sig.cycle_sec == 110 and sig.offset_sec == 51  # the live program
    on_leg = SegmentContext(approach, None)
    fired = 0
    reads = 2 * sig.cycle_sec
    for k in range(reads):
        t = BASE + timedelta(seconds=k * sig.cycle_sec / reads)
        recs = evaluate_rules(_sighting(t, heading), on_leg, sig)
        hit = [r for r in recs if r.rule_id == "R_SIGNAL_VIOLATION"]
        if hit:
            fired += 1
            ev = hit[0].evidence
            assert ev["movement"] == movement and ev["signal_state"] == "red"
            assert phase_state_at(sig, movement, t)[0] == "red"
    green = sum(g for g, mv in sig.phases if movement in mv)
    red_fraction = 1.0 - (green + AMBER_SEC) / sig.cycle_sec
    assert fired == round(reads * red_fraction)
    assert fired < reads
    if movement == "through":
        assert fired == 114  # 57 of 110 s red (50 green + 3 amber): about half,
        # as the audit expected


def test_attribution_radius_is_a_stop_line():
    assert engine.JUNCTION_ATTRIBUTION_TOL_M == 40.0
    assert enforcement.JUNCTION_ATTRIBUTION_TOL_M == 40.0
    assert engine.SEGMENT_ATTRIBUTION_TOL_M == 300.0


def test_violation_evidence_names_approach_and_clock():
    sig = _program_for(3)
    # through is red from position 53 (50 green + 3 amber) to the cycle end;
    # BASE sits on a 100 s epoch boundary, the cycle is 110 s
    into_cycle_at_base = int(BASE.timestamp()) % sig.cycle_sec
    t = BASE + timedelta(seconds=(sig.offset_sec + 60 - into_cycle_at_base) % sig.cycle_sec)
    recs = evaluate_rules(_sighting(t, 268.0), SegmentContext(1005, 40.0), sig)
    ev = [r for r in recs if r.rule_id == "R_SIGNAL_VIOLATION"][0].evidence
    assert ev["approach_segment_id"] == 1005
    assert ev["movement"] == "through" and ev["sec_into_cycle"] == 60
    assert ev["offset_sec"] == 51 and ev["amber_sec"] == AMBER_SEC
    assert "unix_epoch" in ev["phase_clock"]
    assert rules.RULE_IDS == ("R_SPD_SEGMENT", "R_SIGNAL_VIOLATION", "R_CLOSED_CORRIDOR_ENTRY")


def test_a_turner_read_past_the_stop_line_is_no_decision():
    """Kalupur Station Junction (J8): Station Road 1014 (from the south) and
    Kasturba Gandhi Road 1031 (north-west) are the through pair, Naroda
    Road 1030 crosses.  A vehicle that came off Naroda Road on the CROSS
    green and turned onto Kasturba Gandhi Road, read on that leg past the
    junction, carries exactly the heading of a through vehicle arriving
    from Station Road.  Through is red at that moment — only the read's
    position says the vehicle is not on that approach."""
    sig = _program_for(8)
    assert through_legs(sig.legs) == frozenset({1014, 1031})
    central = next(leg for leg in sig.legs if leg.segment_id == 1031)
    departing = central.bearing_deg          # away from J8 along 1031
    # the heading alone maps onto the Station Road (through) approach ...
    assert approach_movement(departing, sig).approach_segment_id == 1014
    # ... at a second when cross is green and through is red
    t = next(
        BASE + timedelta(seconds=k)
        for k in range(sig.cycle_sec)
        if phase_state_at(sig, "cross", BASE + timedelta(seconds=k))[0] == "green"
    )
    assert phase_state_at(sig, "through", t)[0] == "red"

    def fired(segment_id):
        recs = evaluate_rules(_sighting(t, departing), SegmentContext(segment_id, None), sig)
        return [r for r in recs if r.rule_id == "R_SIGNAL_VIOLATION"]

    assert fired(1031) == []                 # past the stop line: no decision
    assert read_approach(departing, 1031, sig) is None
    assert len(fired(1014)) == 1             # on the approach leg: red, decided
    assert read_approach(departing, 1014, sig).movement == "through"
