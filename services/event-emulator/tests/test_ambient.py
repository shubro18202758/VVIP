"""Living City tests: the deterministic day program and the supervisor.

Program (pure): the same IST date and city seed give the identical program;
two to three causes overlap at every minute of the day on disjoint roads;
about forty slots; 108 requests never more than 30 minutes apart; the
seasons and the school week shape what happens.

Supervisor (fake seams, driven tick by tick): slots open as corridor
events through C-F with living-city provenance; two Jan Rakshak beats
publish C-A docs; the ANPR trickle crosses stop lines only on green at a
compliant speed; deliberate violators are planned causes and an UNPLANNED
rule hit retires its camera; 108 requests land as 'requested' dispatches
and a request still waiting for an officer when its slot closes is
re-filed on an open slot; a manual run pre-empts overlapping slots; stop /
day rollover close everything through C-F; a seed-network mismatch fails
loudly.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import pytest

from event_emulator.ambient import (
    _FLEET,
    _NS,
    IST,
    PATROLS,
    SITES,
    LivingCity,
    build_day_program,
    _plate,
    day_seed_for,
    trickle_read,
)
from event_emulator.nats_seam import UNITS_EMULATED_SUBJECT
from event_emulator.runner import RunnerDeps
from event_emulator.tape import movement_states_at
from fakes import FakeNats, FakeOracle, FakeStore, FakeTwin, FakeValkey, NoSleep

SEED = 1411
DAY = date(2026, 10, 22)

PROGRAM = {"cycle_sec": 100, "offset_sec": 0,
           "phases": [{"green_sec": 47, "movements": ["through"]},
                      {"green_sec": 38, "movements": ["cross"]}]}
CAMERAS = [
    {"camera_id": "EM-CAM-01", "lon": 72.5812, "lat": 23.0283, "junction_id": None,
     "heading_deg": None, "segment_id": 1012, "speed_limit_kmh": 40.0, "oneway": False,
     "bearing_deg": 70.0, "signal_junction_id": None},
    {"camera_id": "EM-J03", "lon": 72.57956, "lat": 23.02538, "junction_id": 3,
     "heading_deg": 84.86, "segment_id": 1015, "speed_limit_kmh": 60.0, "oneway": False,
     "bearing_deg": 110.0, "signal_junction_id": 3},
]
LEGS = {3: [
    {"segment_id": 1005, "road_class": "primary", "bearing_deg": 138.0},
    {"segment_id": 1012, "road_class": "trunk", "bearing_deg": 60.0},
    {"segment_id": 1015, "road_class": "trunk", "bearing_deg": 265.0},
]}


# ── the day program (pure) ──────────────────────────────────────────────────


class TestDayProgram:
    def test_same_date_and_seed_identical(self):
        assert build_day_program(SEED, DAY).to_json() == build_day_program(SEED, DAY).to_json()

    def test_date_and_seed_both_matter(self):
        base = build_day_program(SEED, DAY).to_json()
        assert build_day_program(SEED, DAY + timedelta(days=1)).to_json() != base
        assert build_day_program(SEED + 1, DAY).to_json() != base
        assert day_seed_for(SEED, DAY) != day_seed_for(SEED, DAY + timedelta(days=1))

    @pytest.mark.parametrize("offset", range(0, 365, 23))
    def test_two_to_three_causes_on_disjoint_roads(self, offset):
        d = date(2026, 1, 1) + timedelta(days=offset)
        p = build_day_program(SEED, d)
        assert 36 <= len(p.slots) <= 52
        t = p.day_start
        while t < p.day_start + timedelta(days=1):
            active = p.active_at(t)
            assert 2 <= len(active) <= 3, (d, t)
            segs = [s for slot in active for s in slot.route_segment_ids]
            assert len(segs) == len(set(segs)), (d, t)
            t += timedelta(minutes=5)

    def test_ambulance_requests_every_25_minutes(self):
        p = build_day_program(SEED, DAY)
        gaps = [(b.at - a.at).total_seconds() / 60.0
                for a, b in zip(p.ambulance_requests, p.ambulance_requests[1:])]
        assert max(gaps) < 30.0 and 20.0 < sum(gaps) / len(gaps) < 26.0
        for a in p.ambulance_requests:
            target = next(s for s in p.slots if s.slot_id == a.slot_id)
            assert target.active_at(a.at)
            assert a.unit_id.startswith("108-EM") and a.waypoint_junction is not None

    def test_violators_are_rare(self):
        p = build_day_program(SEED, DAY)
        assert 70 <= len(p.violations) <= 100      # ~1 per 15-19 min
        assert {v.kind for v in p.violations} == {"speed", "red_light"}

    def test_seasons_and_school_week(self):
        july = build_day_program(SEED, date(2026, 7, 14))
        april = build_day_program(SEED, date(2026, 4, 14))
        assert any(s.kind == "monsoon_rain" for s in july.slots)
        assert not any(s.kind == "monsoon_rain" for s in april.slots)
        rain = next(s for s in july.slots if s.kind == "monsoon_rain")
        assert rain.weather["code"] == "RAIN" and rain.title.startswith("Monsoon burst")
        sunday = build_day_program(SEED, date(2026, 10, 25))
        assert not any(s.kind == "school_egress" for s in sunday.slots)
        for s in build_day_program(SEED, DAY).slots:
            if s.kind == "school_egress":
                h = s.starts_at.astimezone(IST)
                assert 7 <= h.hour < 18

    def test_slot_shape(self):
        p = build_day_program(SEED, DAY)
        kinds = Counter(s.kind for s in p.slots)
        assert set(kinds) <= {"market_spillover", "signal_fault", "breakdown",
                              "monsoon_rain", "school_egress"}
        for s in p.slots:
            assert s.slot_id.startswith("20261022-L")
            assert s.ends_at > s.starts_at
            assert s.event_kind in ("mass_gathering", "work_zone", "egress_surge")

    def test_day_seed_is_exact_in_a_browser(self):
        # JSON numbers above 2**53 lose digits in JavaScript (the deck)
        for offset in range(0, 400, 7):
            assert 0 <= day_seed_for(SEED, DAY + timedelta(days=offset)) < 2**53

    def test_trickle_is_pure_and_em_marked(self):
        seed = day_seed_for(SEED, DAY)
        reads = [trickle_read(SEED, seed, t) for t in range(500)]
        assert reads == [trickle_read(SEED, seed, t) for t in range(500)]
        assert all(r.plate[4:6] == "EM" for r in reads)
        assert {"GJ27EM9001", "GJ27EM4321"}.isdisjoint({r.plate for r in reads})
        assert len({r.plate for r in reads}) > 100


# ── the supervisor ──────────────────────────────────────────────────────────


def _city(store=None, reserved=None, rules=None):
    store = store or FakeStore(cameras=[dict(c) for c in CAMERAS], programs={3: PROGRAM},
                               legs=LEGS)
    oracle = FakeOracle(store=store, rules=rules)
    nats = FakeNats()
    deps = RunnerDeps(store=store, valkey=FakeValkey(), twin=FakeTwin(), nats=nats,
                      oracle=oracle, sleep=NoSleep())
    city = LivingCity(deps, city_seed=SEED, reserved_routes=reserved or (lambda: {}))
    return city, store, oracle, nats


def _at(hh: int, mm: int = 0, ss: int = 0, d: date = DAY) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=IST).astimezone(timezone.utc)


def _trickle_ids(city: LivingCity, start: datetime, ticks: int) -> set[str]:
    """The read ids of the background trickle over a tick range (the
    supervisor keys them by IST date + tick index + session run)."""
    import uuid

    day_start = city.program_for(DAY).day_start
    first = int((start - day_start).total_seconds() // 5)
    return {str(uuid.uuid5(_NS, f"read|{DAY}|tick:{first + k}|run:{city.run_id}"))
            for k in range(ticks + 1)}


async def _run(city, start: datetime, ticks: int, step: float = 5.0) -> datetime:
    await city._open_session(start)
    t = start
    for _ in range(ticks):
        await city.tick(t)
        t += timedelta(seconds=step)
    return t


class TestSupervisor:
    async def test_slots_open_as_living_city_events(self):
        city, store, oracle, nats = _city()
        await _run(city, _at(18, 30), 1)
        sess = city._session
        assert sess is not None and sess.run_id is not None
        assert store.runs[sess.run_id]["spec"]["mode"] == "living_city"
        active = city.status_doc()["active_slots"]
        assert 2 <= len(active) <= 3
        for slot in active:
            ev = store.events[slot["event_id"]]
            meta = ev["metadata"]
            assert meta["source"] == "event-emulator" and meta["mode"] == "living_city"
            assert meta["slot"] == slot["slot_id"] and meta["run_id"] == sess.run_id
            assert ev["status"] == "active" and ev["protection_class"] == "none"
            assert ev["movement_id"] is None
        assert all(t[3] == "event-emulator (system)" for t in oracle.transitions)
        assert not store.movements                     # slots write no movements

    async def test_patrols_publish_c_a_docs(self):
        city, store, oracle, nats = _city()
        await _run(city, _at(9, 0), 3)
        docs = [d for s, d, _r in nats.json_docs if s == UNITS_EMULATED_SUBJECT]
        assert len(docs) == 6
        assert {d["unit_id"] for d in docs} == {p[0] for p in PATROLS}
        for d in docs:
            assert d["agency"] == "jan_rakshak" and d["source"] == "emulated"
            assert d["status"] == "patrol" and d["run_id"] == city.run_id
        # they move
        first = [d["position"] for d in docs if d["unit_id"] == "JR-EM-LC1"]
        assert first[0] != first[-1]

    async def test_patrols_ride_the_network_after_it_changes(self):
        # the store's network is converted under the running city (WP-20):
        # the beats re-chain their lines on the new key, without a restart
        city, store, oracle, nats = _city()
        await _run(city, _at(9, 0), 1)
        beat = PATROLS[0]
        before = await city._patrol_line(beat[0], beat[2])
        moved = {s: [(lon + 0.01, lat), (lon + 0.02, lat)]
                 for s, (lon, lat) in store.anchors.items()}
        store.paths.update(moved)
        assert await city._patrol_line(beat[0], beat[2]) == before   # same key: cached
        store.network = "osm"
        after = await city._patrol_line(beat[0], beat[2])
        assert after != before and after[0] in [p for s in beat[2] for p in moved[s]]

    async def test_dispatched_patrol_is_handed_over(self):
        store = FakeStore(cameras=[dict(c) for c in CAMERAS], programs={3: PROGRAM},
                          legs=LEGS, dispatched={"JR-EM-LC2"})
        city, store, oracle, nats = _city(store=store)
        await _run(city, _at(9, 0), 2)
        units = {d["unit_id"] for s, d, _r in nats.json_docs if s == UNITS_EMULATED_SUBJECT}
        assert units == {"JR-EM-LC1"}

    async def test_trickle_obeys_the_light_and_the_limit(self):
        city, store, oracle, nats = _city()
        start = _at(11, 0)
        await _run(city, start, 240)                  # 20 minutes of ticks
        ids = _trickle_ids(city, start, 240)
        reads = [b for b, _r in oracle.sighting_bodies]
        trickle = [b for b in reads if b["read_id"] in ids]
        assert len(trickle) >= 200
        at_stopline = [b for b in trickle if b["camera_id"] == "EM-J03"]
        assert at_stopline, "the stop-line camera reads on its greens"
        for b in at_stopline:
            epoch = datetime.fromisoformat(b["t"]).timestamp()
            assert movement_states_at(100, 0, PROGRAM["phases"], epoch)["through"] == "green"
            assert b["heading"] == round(84.86, 1)
        limits = {c["camera_id"]: c["speed_limit_kmh"] for c in CAMERAS}
        for b in trickle:
            assert b["speed_kmh"] <= limits[b["camera_id"]] * 0.9 + 0.05
            assert b["source"] == "emulated_anpr" and b["run_id"] == city.run_id
            assert b["plate"][4:6] == "EM"
            assert datetime.fromisoformat(b["t"]) <= start + timedelta(seconds=5 * 240)

    async def test_violators_are_planned_causes(self):
        city, store, oracle, nats = _city()
        start = _at(11, 0)
        await _run(city, start, 240)
        ids = _trickle_ids(city, start, 240)
        violators = [b for b, _r in oracle.sighting_bodies if b["read_id"] not in ids]
        due = [v for v in city.program_for(DAY).violations
               if start < v.at <= start + timedelta(seconds=5 * 239)]
        assert violators and len(violators) == len(due)
        assert city.counters["violations_planned"] == len(due)
        for b in violators:
            epoch = datetime.fromisoformat(b["t"]).timestamp()
            if b["camera_id"] == "EM-J03":            # ran the red
                assert movement_states_at(100, 0, PROGRAM["phases"], epoch)["through"] == "red"
            else:                                      # sped past a mid-block camera
                assert b["speed_kmh"] > 40.0 + 10.0

    async def test_violators_can_be_switched_off(self):
        city, store, oracle, nats = _city()
        city.violators = False
        start = _at(11, 0)
        await _run(city, start, 240)
        ids = _trickle_ids(city, start, 240)
        assert all(b["read_id"] in ids for b, _r in oracle.sighting_bodies)   # trickle only
        due = [v for v in city.program_for(DAY).violations
               if start < v.at <= start + timedelta(seconds=5 * 239)]
        assert due and city.counters["violations_suppressed"] == len(due)
        assert city.counters["violations_planned"] == 0
        assert city.status_doc()["violators"] is False

    async def test_unplanned_rule_hit_retires_the_camera(self):
        def rules(read):
            # an oracle that disagrees with the replica: every stop-line
            # read is a red-light violation
            return ["R_SIGNAL_VIOLATION"] if read["camera_id"] == "EM-J03" else []

        city, store, oracle, nats = _city(rules=rules)
        start = _at(11, 0)
        await _run(city, start, 240)
        ids = _trickle_ids(city, start, 240)
        trickle = [b for b, _r in oracle.sighting_bodies if b["read_id"] in ids]
        first = next(i for i, b in enumerate(trickle) if b["camera_id"] == "EM-J03")
        assert all(b["camera_id"] != "EM-J03" for b in trickle[first + 1:])
        assert "EM-J03" in city.status_doc()["excluded_cameras"]
        assert city.counters["unplanned_R_SIGNAL_VIOLATION"] == 1

    async def test_a_stolen_violator_is_a_sighting_never_an_unplanned_hit(self):
        def rules(read):
            # the oracle's penal rules: the violator sped, or ran the red
            if read["camera_id"] == "EM-J03":
                epoch = datetime.fromisoformat(read["t"]).timestamp()
                red = movement_states_at(100, 0, PROGRAM["phases"], epoch)["through"] == "red"
                return ["R_SIGNAL_VIOLATION"] if red else []
            return ["R_SPD_SEGMENT"] if read["speed_kmh"] > 40.0 + 10.0 else []

        city, store, oracle, nats = _city(rules=rules)
        start = _at(11, 0)
        due = [v for v in city.program_for(DAY).violations
               if start < v.at <= start + timedelta(seconds=5 * 239)]
        assert due
        # an officer's FIR earmark on every one of today's violators: the
        # oracle answers R_STOLEN_SIGHTING and WITHHOLDS the penal rule (C-N)
        oracle.earmarked.update(
            {_plate(SEED, v.pick % _FLEET)[0]: "stolen_vehicle" for v in due})
        await _run(city, start, 240)
        c = city.counters
        assert c["violations_planned"] == len(due)
        # caught all the same — the e-challan withheld, never a miss
        assert c["violations_flagged"] == c["violations_withheld"] == len(due)
        assert c["stolen_sightings"] >= len(due)
        assert not [k for k in c if k.startswith("unplanned_")], dict(c)
        assert city.status_doc()["excluded_cameras"] == []

    async def test_ambulance_requests_wait_for_an_officer(self):
        city, store, oracle, nats = _city()
        program = city.program_for(DAY)
        req = program.ambulance_requests[40]
        start = req.at - timedelta(seconds=20)
        await _run(city, start, 10)
        assert len(oracle.dispatches) == 1
        event_id, body = oracle.dispatches[0]
        assert body["agency"] == "ambulance" and body["unit_id"] == req.unit_id
        assert body["requested_by"] == "108 control (emulated)"
        assert "EMULATED" in body["note"]
        assert store.events[event_id]["metadata"]["mode"] == "living_city"
        assert city.counters["dispatch_requested"] == 1

    async def test_manual_run_preempts_overlapping_slots(self):
        reserved: dict = {}
        city, store, oracle, nats = _city(reserved=lambda: reserved)
        t = await _run(city, _at(18, 30), 1)
        active = city.status_doc()["active_slots"]
        victim = active[0]
        reserved[99] = ("rath_yatra_monsoon_ambulance", frozenset(victim["route_segment_ids"]))
        await city.tick(t)
        assert store.events[victim["event_id"]]["status"] == "completed"
        assert city.status_doc()["preempted"] == {victim["slot_id"]: 99}
        note = [tr for tr in oracle.transitions if tr[0] == victim["event_id"]][-1][4]
        assert "pre-empted by emulator run 99" in note
        # it does not come back while the run holds the road
        await city.tick(t + timedelta(seconds=5))
        doc = city.status_doc()
        assert victim["slot_id"] not in {s["slot_id"] for s in doc["active_slots"]}
        assert doc["yielded"] == [victim["slot_id"]]
        # the run ends: the slot (window still open) resumes as a FRESH event
        reserved.clear()
        await city.tick(t + timedelta(seconds=10))
        resumed = next(s for s in city.status_doc()["active_slots"]
                       if s["slot_id"] == victim["slot_id"])
        assert resumed["event_id"] != victim["event_id"]
        ev = store.events[resumed["event_id"]]
        assert ev["status"] == "active" and ev["metadata"]["resumed_after_run"] == 99
        assert store.events[victim["event_id"]]["status"] == "completed"
        assert city.status_doc()["yielded"] == []
        assert city.counters["slots_resumed"] == 1

    async def test_an_oracle_outage_heals(self):
        city, store, oracle, nats = _city()
        oracle.down = True
        t = await _run(city, _at(18, 30), 1)
        doc = city.status_doc()
        assert len(doc["active_slots"]) >= 2      # opened (store), not yet active
        assert all(s["event_status"] == "planning" for s in doc["active_slots"])
        assert all(store.events[s["event_id"]]["status"] == "planning"
                   for s in doc["active_slots"])
        assert city.counters["events_activate_failed"] == len(doc["active_slots"])
        oracle.down = False
        t = t + timedelta(seconds=31)              # the next heal pass
        await city.tick(t)
        assert all(store.events[s["event_id"]]["status"] == "active"
                   for s in city.status_doc()["active_slots"])
        assert city.counters["events_activated_late"] == len(doc["active_slots"])
        # a close that fails while the oracle is down is retried, not lost
        victim = city.status_doc()["active_slots"][0]
        oracle.down = True
        await city.stop()
        assert store.events[victim["event_id"]]["status"] == "active"
        assert city.counters["events_close_failed"] >= 1

    async def test_close_retried_by_the_running_session(self):
        reserved: dict = {}
        city, store, oracle, nats = _city(reserved=lambda: reserved)
        t = await _run(city, _at(18, 30), 1)
        victim = city.status_doc()["active_slots"][0]
        oracle.down = True
        reserved[7] = ("metro_work_zone", frozenset(victim["route_segment_ids"]))
        await city.tick(t)                         # pre-empt: the close fails
        assert store.events[victim["event_id"]]["status"] == "active"
        oracle.down = False
        await city.tick(t + timedelta(seconds=31))
        assert store.events[victim["event_id"]]["status"] == "completed"
        assert city._session.unclosed == {}

    async def test_session_ledgered_once_the_store_answers(self):
        city, store, oracle, nats = _city()
        real_insert = store.insert_run

        async def broken(**kw):
            raise ConnectionError("store down")

        store.insert_run = broken
        t = await _run(city, _at(9, 0), 1)
        assert city.run_id is None and "ledger" in (city.last_error or "")
        store.insert_run = real_insert
        await city.tick(t + timedelta(seconds=31))
        assert city.run_id is not None
        assert store.runs[city.run_id]["spec"]["mode"] == "living_city"

    async def test_day_rollover_finishes_the_session(self):
        city, store, oracle, nats = _city()
        await _run(city, _at(23, 59, 50), 1)
        first = city.run_id
        await city.tick(_at(0, 0, 5, d=DAY + timedelta(days=1)))
        assert city.run_id != first
        assert store.runs[first]["status"] == "completed"
        assert city._session.date_ist == DAY + timedelta(days=1)

    async def test_next_causes_look_past_ist_midnight(self):
        city, store, oracle, nats = _city()
        now = _at(23, 30)
        city._now = lambda: now
        await _run(city, now, 1)
        midnight = _at(0, 0, d=DAY + timedelta(days=1))
        causes = city.status_doc()["next_causes"]
        assert causes and all(now <= datetime.fromisoformat(c["at"]) < now + timedelta(minutes=90)
                              for c in causes)
        assert any(datetime.fromisoformat(c["at"]) >= midnight for c in causes)

    async def test_an_officer_cancelled_event_is_left_closed(self):
        city, store, oracle, nats = _city()
        oracle.down = True
        t = await _run(city, _at(18, 30), 1)
        victim = city.status_doc()["active_slots"][0]
        oracle.down = False
        # a duty officer cancels the still-planning event from the ops wall
        await oracle.event_status(victim["event_id"], "cancelled", "duty officer")
        await city.tick(t + timedelta(seconds=31))           # the heal pass
        doc = city.status_doc()
        assert victim["slot_id"] not in {s["slot_id"] for s in doc["active_slots"]}
        assert all(s["event_status"] == "active" for s in doc["active_slots"])
        assert city.counters["events_closed_by_officer"] == 1
        await city.tick(t + timedelta(seconds=36))
        assert victim["slot_id"] not in {s["slot_id"] for s in city.status_doc()["active_slots"]}
        assert store.events[victim["event_id"]]["status"] == "cancelled"

    async def test_start_stop_and_orphans(self):
        store = FakeStore(cameras=[dict(c) for c in CAMERAS], programs={3: PROGRAM}, legs=LEGS)
        # an earlier session's slot left open by a crash
        await store.upsert_corridor_event(
            "20000000-0000-0000-0000-000000000001", None, "work_zone", "none", "LOW",
            "orphan slot", _at(10), _at(12),
            {"source": "event-emulator", "mode": "living_city", "run_id": 1234})
        store.events["20000000-0000-0000-0000-000000000001"]["status"] = "active"
        city, store, oracle, nats = _city(store=store)
        await city.start()
        await asyncio.sleep(0.1)
        assert store.events["20000000-0000-0000-0000-000000000001"]["status"] == "completed"
        doc = city.status_doc()
        assert doc["status"] == "running" and doc["active_slots"]
        opened = [s["event_id"] for s in doc["active_slots"]]
        stopped = await city.stop()
        assert stopped["status"] == "stopped" and stopped["active_slots"] == []
        assert all(store.events[e]["status"] == "completed" for e in opened)
        rid = doc["run_id"]
        assert store.runs[rid]["status"] == "stopped"


# ── 108 requests outlive their slot ─────────────────────────────────────────


def _filed(oracle: FakeOracle) -> list[tuple[str, dict]]:
    """(dispatch id, row) in filing order."""
    return list(oracle.dispatch_rows.items())


async def _one_request(city: LivingCity, oracle: FakeOracle, index: int = 40):
    """Run the city up to program request ``index`` (filed, 'requested')."""
    req = city.program_for(DAY).ambulance_requests[index]
    t = await _run(city, req.at - timedelta(seconds=20), 10)
    assert len(oracle.dispatches) == 1
    did, row = _filed(oracle)[0]
    return req, t, did, row


class TestRequestsOutliveTheirSlot:
    async def test_a_preempted_slot_refiles_its_pending_request(self):
        reserved: dict = {}
        city, store, oracle, nats = _city(reserved=lambda: reserved)
        req, t, first_id, first = await _one_request(city, oracle)
        slot = next(s for s in city.status_doc()["active_slots"]
                    if s["event_id"] == first["event_id"])
        reserved[99] = ("vvip_convoy_base", frozenset(slot["route_segment_ids"]))
        await city.tick(t)
        # the oracle's auto-close cancelled it with the pre-empted event …
        assert oracle.dispatch_rows[first_id]["status"] == "cancelled"
        # … and 108 re-filed it at once on a slot that is still open
        assert len(oracle.dispatches) == 2
        new_id, new = _filed(oracle)[1]
        assert new["event_id"] != first["event_id"]
        assert store.events[new["event_id"]]["status"] == "active"
        assert store.events[new["event_id"]]["metadata"]["mode"] == "living_city"
        body = new["body"]
        assert body["unit_id"] == req.unit_id and body["agency"] == "ambulance"
        assert body["requested_by"] == "108 control (emulated)"
        assert "re-filed (filing 2)" in body["note"] and first_id in body["note"]
        assert "pre-empted by emulator run 99" in body["note"] and "EMULATED" in body["note"]
        assert len(body["note"]) <= 500 and body.get("waypoint_junction")
        assert new["status"] == "requested"            # an officer still decides
        assert city.counters["dispatch_released"] == 1
        assert city.counters["dispatch_refiled"] == 1
        # once filed, it is not filed again on later ticks
        await city.tick(t + timedelta(seconds=5))
        assert len(oracle.dispatches) == 2 and city.status_doc()["refiling"] == []

    async def test_a_decided_request_is_never_refiled(self):
        reserved: dict = {}
        city, store, oracle, nats = _city(reserved=lambda: reserved)
        req, t, first_id, first = await _one_request(city, oracle)
        oracle.decide(first_id, "approved")           # the duty officer approved it
        slot = next(s for s in city.status_doc()["active_slots"]
                    if s["event_id"] == first["event_id"])
        reserved[99] = ("vvip_convoy_base", frozenset(slot["route_segment_ids"]))
        await city.tick(t)
        assert oracle.dispatch_rows[first_id]["status"] == "cancelled"
        assert len(oracle.dispatches) == 1
        assert city.counters["dispatch_refiled"] == 0 and city.counters["dispatch_released"] == 0

    async def test_a_request_with_no_open_slot_waits_then_expires(self):
        reserved: dict = {}
        city, store, oracle, nats = _city(reserved=lambda: reserved)
        req, t, first_id, first = await _one_request(city, oracle)
        # a run holds EVERY living-city road: nothing can carry the request
        reserved[99] = ("vvip_convoy_base",
                        frozenset(s for site in SITES for s in site.segments))
        await city.tick(t)
        assert city.status_doc()["active_slots"] == []
        assert [r["replaces"] for r in city.status_doc()["refiling"]] == [first_id]
        for k in range(1, 23):                        # 22 minutes, a tick a minute
            await city.tick(t + timedelta(minutes=k))
        assert len(oracle.dispatches) == 1
        assert city.counters["dispatch_refile_expired"] == 1
        assert city.status_doc()["refiling"] == []

    async def test_the_ist_rollover_refiles_onto_the_next_days_slots(self):
        # a day whose last 108 request is late enough to still be waiting
        # at midnight (re-filed within 20 minutes of the first ask)
        for offset in range(0, 30):
            d = DAY + timedelta(days=offset)
            late = [a for a in build_day_program(SEED, d).ambulance_requests
                    if a.at.astimezone(IST).hour == 23 and a.at.astimezone(IST).minute >= 42]
            if late:
                break
        else:
            raise AssertionError("no late 108 request in 30 days")
        req = late[-1]
        city, store, oracle, nats = _city()
        t = await _run(city, req.at - timedelta(seconds=10), 4)
        assert len(oracle.dispatches) == 1
        first_id = _filed(oracle)[0][0]
        midnight = datetime.combine(d + timedelta(days=1), datetime.min.time(), IST)
        while t <= midnight + timedelta(seconds=5):
            await city.tick(t)
            t += timedelta(seconds=5)
        assert oracle.dispatch_rows[first_id]["status"] == "cancelled"
        refiled = [(did, row) for did, row in _filed(oracle)[1:]
                   if "IST day ended" in row["body"]["note"]]
        assert refiled
        did, row = refiled[-1]
        meta = store.events[row["event_id"]]["metadata"]
        assert meta["date_ist"] == (d + timedelta(days=1)).isoformat()
        assert meta["run_id"] == city.run_id and row["status"] == "requested"

    async def test_stop_clears_what_was_waiting(self):
        reserved: dict = {}
        city, store, oracle, nats = _city(reserved=lambda: reserved)
        req, t, first_id, first = await _one_request(city, oracle)
        await city.stop()
        assert oracle.dispatch_rows[first_id]["status"] == "cancelled"
        assert city.status_doc()["refiling"] == []
        assert len(oracle.dispatches) == 1


# ── the seed network check ──────────────────────────────────────────────────


class TestNetworkCheck:
    async def test_the_seed_network_passes(self):
        city, store, oracle, nats = _city()
        await _run(city, _at(9, 0), 1)
        assert city.detail is None and city.health_doc()["detail"] is None

    async def test_a_mismatch_fails_loudly_and_skips_unknown_roads(self):
        adjacency = await FakeStore().segment_junction_ids()
        del adjacency[1030]                           # Naroda Road reseeded away
        for s in (1001, 1002, 1015):                  # junction 1 renumbered
            adjacency[s].discard(1)
        store = FakeStore(cameras=[dict(c) for c in CAMERAS], programs={3: PROGRAM},
                          legs=LEGS, adjacency=adjacency)
        city, store, oracle, nats = _city(store=store)
        program = city.program_for(DAY)
        slot = next(s for s in program.slots if 1030 in s.route_segment_ids)
        await _run(city, slot.starts_at + timedelta(minutes=1), 1)
        detail = city.status_doc()["detail"]
        assert detail.startswith("seed network mismatch")
        assert "[1030]" in detail and "SF-J1:J1" in detail
        assert city.health_doc()["detail"] == detail
        opened = {s["slot_id"] for s in city.status_doc()["active_slots"]}
        assert slot.slot_id not in opened
        assert city.counters["slots_unknown_roads"] >= 1
        # no event was ever opened on the unknown road
        assert store.event_routes and all(1030 not in r for r in store.event_routes.values())
