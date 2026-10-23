"""Watchlist & enforcement tests — pure python, fakes only (no net).

Everything runs without a database except the interception-plan graph
check at the end, which runs the real endpoint against a sandbox store
inside a READ ONLY transaction when VEER_TEST_PG_DSN is set.

Covers:
* enforcement/rules.py — the three deterministic rules, honest degradation,
  phase arithmetic (the shared Unix-epoch clock), compass movement mapping
  for NS/EW-labelled programs, and byte-stable determinism (the 006
  through/cross vocabulary lives in test_enforcement_vocab.py);
* vehicles/sources.py — the AnprSource port, the frozen AnprRead frame
  schema (C2), source-labelling enforcement, EmulatedAnprSource ordering
  and tape normalization;
* vehicles + enforcement routers — earmark validation (lawful basis,
  retention cap), listing with ACTIVE-earmark-only filtering, decision
  flow (pending -> approved/rejected, officer mandatory, append-only
  audit tail, no decision before the violation), the throttled retention
  reap, plate normalization and the earmark_state key — against a fake
  asyncpg-style pool;
* vehicles/pursuit.py — heading-chosen downstream seeding, ONE ranking
  statement, age/stale flags, and one-way legality matching the 012
  road_graph (fake pool; plus the sandbox-store check);
* coordination stubs (C0) — real routers mount; absent streams stub 503.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from traffic_oracle.coordination import register_coordination_routers
from traffic_oracle.enforcement import rules
from traffic_oracle.enforcement.rules import (
    ClosureContext,
    JunctionLeg,
    Recommendation,
    SegmentContext,
    SignalContext,
    SightingContext,
    evaluate_rules,
    movement_for_heading,
    phase_state_at,
)
from traffic_oracle.vehicles import LAWFUL_BASIS_CODES, EarmarkRequest
from traffic_oracle.vehicles.sources import (
    SOURCE_EMULATED,
    SOURCE_LIVE,
    AnprRead,
    EmulatedAnprSource,
    anpr_read_id,
    read_from_tape_payload,
)

T0 = datetime(2026, 11, 5, 5, 0, 0, tzinfo=timezone.utc)


# ── rules ───────────────────────────────────────────────────────────────────


def _sighting(**kw) -> SightingContext:
    base = dict(
        sighting_id=str(uuid.uuid4()),
        plate="GJ01EM1234",
        camera_id="EM-CAM-01",
        t=T0,
        lon=72.58,
        lat=22.99,
        source=SOURCE_EMULATED,
        speed_kmh=50.0,
        heading=10.0,
    )
    base.update(kw)
    return SightingContext(**base)


class TestSpeedRule:
    def test_fires_above_limit_plus_tolerance(self):
        recs = evaluate_rules(
            _sighting(speed_kmh=75.0),
            SegmentContext(segment_id=1005, speed_limit_kmh=40.0, road_name="Relief Rd"),
            None,
        )
        spd = [r for r in recs if r.rule_id == "R_SPD_SEGMENT"]
        assert len(spd) == 1
        ev = spd[0].evidence
        assert ev["speed_limit_kmh"] == 40.0 and ev["excess_kmh"] == 35.0
        assert ev["source"] == SOURCE_EMULATED

    def test_within_tolerance_does_not_fire(self):
        recs = evaluate_rules(
            _sighting(speed_kmh=49.0),
            SegmentContext(segment_id=1005, speed_limit_kmh=40.0),
            None,
        )
        assert [r.rule_id for r in recs if r.rule_id == "R_SPD_SEGMENT"] == []

    def test_missing_limit_or_speed_is_honest_none(self):
        assert evaluate_rules(_sighting(speed_kmh=None),
                              SegmentContext(1005, None), None) == ()
        assert evaluate_rules(_sighting(speed_kmh=99.0), None, None) == ()


class TestSignalRule:
    PROGRAM = SignalContext(
        junction_id=7,
        cycle_sec=100,
        phases=((40, ("NS",)), (40, ("EW",))),
        offset_sec=0,
    )
    # the same program with its four legs (bearings AWAY from the junction):
    # a northbound read (heading ~0) approaches on the SOUTH leg, 2002
    LEGGED = SignalContext(
        junction_id=7,
        cycle_sec=100,
        phases=((40, ("NS",)), (40, ("EW",))),
        offset_sec=0,
        legs=(
            JunctionLeg(2001, "primary", 0.0),     # north leg
            JunctionLeg(2002, "primary", 180.0),   # south leg
            JunctionLeg(2003, "secondary", 90.0),  # east leg
            JunctionLeg(2004, "secondary", 270.0), # west leg
        ),
    )
    ON_SOUTH_LEG = SegmentContext(2002, None)

    def test_movement_for_heading_compass_program(self):
        # an NS/EW-labelled program with no leg geometry maps by compass
        assert movement_for_heading(0.0, self.PROGRAM) == "NS"
        assert movement_for_heading(180.0, self.PROGRAM) == "NS"
        assert movement_for_heading(90.0, self.PROGRAM) == "EW"
        assert movement_for_heading(270.0, self.PROGRAM) == "EW"
        assert movement_for_heading(350.0, self.PROGRAM) == "NS"
        assert movement_for_heading(None, self.PROGRAM) is None
        assert movement_for_heading(float("nan"), self.PROGRAM) is None

    def test_phase_state_is_pure_and_periodic(self):
        # T0 sits on a 100 s boundary of the UNIX epoch (the shared clock:
        # pos = (epoch - offset) mod cycle), so position == seconds after T0
        assert T0.timestamp() % 100 == 0
        t = T0.replace(second=0, microsecond=0)
        s0, into0 = phase_state_at(self.PROGRAM, "NS", t)
        assert s0 == "green" and into0 == 0
        # 100 s later -> identical state (cycle period)
        s1, into1 = phase_state_at(self.PROGRAM, "NS", t + timedelta(seconds=100))
        assert (s0, into0) == (s1, into1)
        # NS green 0..39, EW green 40..79, tail 80..99 lost time
        assert phase_state_at(self.PROGRAM, "NS", t + timedelta(seconds=10))[0] == "green"
        assert phase_state_at(self.PROGRAM, "EW", t + timedelta(seconds=10))[0] == "red"
        assert phase_state_at(self.PROGRAM, "EW", t + timedelta(seconds=50))[0] == "green"
        assert phase_state_at(self.PROGRAM, "NS", t + timedelta(seconds=90))[0] == "red"
        # the AMBER_SEC after a green is amber — neither green nor red
        assert phase_state_at(self.PROGRAM, "NS", t + timedelta(seconds=41))[0] == "amber"
        assert phase_state_at(self.PROGRAM, "NS", t + timedelta(seconds=43))[0] == "red"
        assert phase_state_at(self.PROGRAM, "EW", t + timedelta(seconds=82))[0] == "amber"
        # a movement the program does not define has NO state (never red)
        assert phase_state_at(self.PROGRAM, "through", t + timedelta(seconds=90))[0] == "none"

    def test_offset_shifts_the_clock(self):
        shifted = SignalContext(
            junction_id=7, cycle_sec=100,
            phases=((40, ("NS",)), (40, ("EW",))), offset_sec=30,
        )
        t = T0.replace(second=0, microsecond=0)
        # epoch position 30 == program position 0 with a 30 s offset
        assert phase_state_at(shifted, "NS", t + timedelta(seconds=30)) == ("green", 0)
        # epoch position 10 == program position 80 (lost time, wrapped)
        assert phase_state_at(shifted, "NS", t + timedelta(seconds=10)) == ("red", 80)

    def test_amber_crossing_does_not_fire(self):
        t = T0.replace(second=0, microsecond=0) + timedelta(seconds=41)
        recs = evaluate_rules(_sighting(t=t, heading=5.0), self.ON_SOUTH_LEG, self.LEGGED)
        assert [r for r in recs if r.rule_id == "R_SIGNAL_VIOLATION"] == []

    def test_red_crossing_fires(self):
        t = T0.replace(second=0, microsecond=0) + timedelta(seconds=50)
        recs = evaluate_rules(
            _sighting(t=t, heading=5.0), self.ON_SOUTH_LEG, self.LEGGED
        )
        sig = [r for r in recs if r.rule_id == "R_SIGNAL_VIOLATION"]
        assert len(sig) == 1
        assert sig[0].evidence["movement"] == "NS"
        assert sig[0].evidence["signal_state"] == "red"
        assert sig[0].evidence["approach_segment_id"] == 2002
        assert sig[0].evidence["green_fraction"] == 0.4

    def test_green_crossing_does_not_fire(self):
        t = T0.replace(second=0, microsecond=0) + timedelta(seconds=10)
        recs = evaluate_rules(_sighting(t=t, heading=5.0), self.ON_SOUTH_LEG, self.LEGGED)
        assert [r for r in recs if r.rule_id == "R_SIGNAL_VIOLATION"] == []

    def test_a_read_must_be_on_its_approach_leg(self):
        """NS is red at 50 s.  Only the northbound read ON the south leg
        (before the stop line, driving in) is a decision."""
        t = T0.replace(second=0, microsecond=0) + timedelta(seconds=50)

        def fired(heading, segment):
            recs = evaluate_rules(_sighting(t=t, heading=heading), segment, self.LEGGED)
            return [r for r in recs if r.rule_id == "R_SIGNAL_VIOLATION"]

        assert len(fired(5.0, self.ON_SOUTH_LEG)) == 1
        # past the stop line on the north (departure) leg: it may be a
        # right-turner from the EW road on its green — no decision
        assert fired(5.0, SegmentContext(2001, None)) == []
        # a turner read on the east leg, heading east: no decision
        assert fired(95.0, SegmentContext(2003, None)) == []
        # on the south leg but heading south, i.e. leaving: no decision
        assert fired(185.0, self.ON_SOUTH_LEG) == []
        # read off every leg, or no segment at all: no decision
        assert fired(5.0, SegmentContext(9999, None)) == []
        assert fired(5.0, None) == []

    def test_without_leg_geometry_there_is_no_decision(self):
        # the compass mapping alone cannot say the read is on an approach
        t = T0.replace(second=0, microsecond=0) + timedelta(seconds=50)
        recs = evaluate_rules(_sighting(t=t, heading=5.0), self.ON_SOUTH_LEG, self.PROGRAM)
        assert [r for r in recs if r.rule_id == "R_SIGNAL_VIOLATION"] == []

    def test_no_program_or_no_heading_no_rule(self):
        assert evaluate_rules(_sighting(heading=None), None, self.PROGRAM) == ()


class TestClosureRule:
    def _closure(self, covers: bool = True) -> ClosureContext:
        return ClosureContext(
            event_id=str(uuid.uuid4()),
            title="Rath Yatra",
            event_kind="procession",
            window_start=T0 - timedelta(minutes=30),
            window_end=T0 + timedelta(minutes=30),
            covers_position=covers,
        )

    def test_earmarked_inside_closure_fires(self):
        recs = evaluate_rules(
            _sighting(earmark_state="flagged", matched_plate_hit="earmarked_plate"),
            None,
            None,
            (self._closure(),),
        )
        closed = [r for r in recs if r.rule_id == "R_CLOSED_CORRIDOR_ENTRY"]
        assert len(closed) == 1
        assert closed[0].evidence["earmark_state"] == "flagged"

    def test_unearmarked_never_fires(self):
        recs = evaluate_rules(_sighting(), None, None, (self._closure(),))
        assert [r for r in recs if r.rule_id == "R_CLOSED_CORRIDOR_ENTRY"] == []

    def test_outside_window_or_position_does_not_fire(self):
        later = _sighting(
            t=T0 + timedelta(hours=2), earmark_state="watch"
        )
        assert evaluate_rules(later, None, None, (self._closure(),)) == ()
        away = _sighting(earmark_state="watch")
        assert evaluate_rules(away, None, None, (self._closure(covers=False),)) == ()

    def test_one_closure_recommendation_even_with_many(self):
        recs = evaluate_rules(
            _sighting(earmark_state="watch"),
            None, None,
            (self._closure(), self._closure()),
        )
        assert len([r for r in recs if r.rule_id == "R_CLOSED_CORRIDOR_ENTRY"]) == 1


class TestDeterminism:
    def test_same_inputs_same_outputs_byte_for_byte(self):
        args = (
            _sighting(speed_kmh=95.0, earmark_state="flagged"),
            SegmentContext(1012, 50.0, "Gandhi Rd"),
            TestSignalRule.PROGRAM,
            (ClosureContext(
                str(uuid.uuid4()), "Sweep", "crime_incident",
                T0 - timedelta(minutes=5), T0 + timedelta(minutes=5), True,
            ),),
        )
        a = evaluate_rules(*args)
        b = evaluate_rules(*args)
        assert a == b
        assert json.dumps([r.evidence for r in a], sort_keys=True) == json.dumps(
            [r.evidence for r in b], sort_keys=True
        )

    def test_all_three_can_fire_together(self):
        t = T0.replace(second=0, microsecond=0) + timedelta(seconds=50)
        recs = evaluate_rules(
            _sighting(speed_kmh=95.0, heading=5.0, earmark_state="flagged", t=t),
            SegmentContext(2002, 50.0),
            TestSignalRule.LEGGED,
            (ClosureContext(
                str(uuid.uuid4()), "Sweep", "crime_incident",
                t - timedelta(minutes=5), t + timedelta(minutes=5), True,
            ),),
        )
        assert set(r.rule_id for r in recs) == set(rules.RULE_IDS)


# ── sources port ────────────────────────────────────────────────────────────


class TestAnprSource:
    def _tape_frames(self, n=3):
        frames = []
        for i in range(n):
            frames.append({
                "frame": 0,
                "t_sec": float(30 - i * 10),  # deliberately unsorted input
                "kind": "anpr_read",
                "payload": {
                    "camera_id": f"EM-CAM-{i:02d}",
                    "plate": f"GJ01EM{i:04d}",
                    "position": [72.58 + i * 0.001, 22.99 - i * 0.001],
                    "speed_kmh": 40.0 + i,
                    "heading": 90.0 * i,
                    "vehicle_class": "car",
                    "read_id": anpr_read_id(
                        "watchlist_anpr_sweep", 7, f"EM-CAM-{i:02d}", f"GJ01EM{i:04d}"
                    ),
                },
            })
        return frames

    def test_read_id_derivation_is_uuid5_and_stable(self):
        a = anpr_read_id("uc", 1, "cam", "plate")
        b = anpr_read_id("uc", 1, "cam", "plate")
        assert a == b and uuid.UUID(a).version == 5
        assert anpr_read_id("uc", 2, "cam", "plate") != a

    def test_source_label_enforced_on_read(self):
        with pytest.raises(ValueError):
            AnprRead(
                read_id=anpr_read_id("uc", 1, "c", "p"), plate="GJ01EM1",
                camera_id="c", t=T0, lon=72.5, lat=23.0, source="live_feed",
            )
        with pytest.raises(ValueError):
            AnprRead(
                read_id=anpr_read_id("uc", 1, "c", "p"), plate="GJ01EM1",
                camera_id="c", t=T0, lon=72.5, lat=23.0,
                source=SOURCE_EMULATED, vehicle_class="zeppelin",
            )
        ok = AnprRead(
            read_id=anpr_read_id("uc", 1, "c", "p"), plate="GJ01EM1",
            camera_id="c", t=T0, lon=72.5, lat=23.0, source=SOURCE_LIVE,
        )
        assert ok.source == SOURCE_LIVE  # the port accepts the reserved label

    def test_tape_normalization_and_ordering(self):
        src = EmulatedAnprSource.from_tape_frames(
            self._tape_frames(), t0=T0, use_case_id="watchlist_anpr_sweep", seed=7
        )
        assert src.label == SOURCE_EMULATED and src.n_reads == 3

        async def _collect():
            return [r async for r in src.reads()]

        reads = asyncio.run(_collect())
        assert [r.camera_id for r in reads] == ["EM-CAM-02", "EM-CAM-01", "EM-CAM-00"]
        first = reads[0]
        assert first.t == T0 + timedelta(seconds=10)
        assert first.read_id == anpr_read_id(
            "watchlist_anpr_sweep", 7, "EM-CAM-02", "GJ01EM0002"
        )

    def test_normalization_rejects_malformed_frames(self):
        with pytest.raises(KeyError):
            read_from_tape_payload(
                {"camera_id": "c"}, t0=T0, use_case_id="uc", seed=1
            )

    def test_source_matches_runtime_protocol(self):
        src = EmulatedAnprSource.from_tape_frames(
            self._tape_frames(1), t0=T0, use_case_id="uc", seed=1
        )
        from traffic_oracle.vehicles.sources import AnprSource

        assert isinstance(src, AnprSource)


# ── fake pool + routers ─────────────────────────────────────────────────────


@dataclass
class FakeRow(dict):
    """A dict row (asyncpg Record-like: r['col'])."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)

    def __getitem__(self, k):
        return dict.__getitem__(self, k)


@dataclass
class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@dataclass
class FakeConn:
    store: "FakeStore"

    async def fetchrow(self, sql, *args):
        return self.store.fetchrow(sql, args)

    async def fetch(self, sql, *args):
        return self.store.fetch(sql, args)

    def transaction(self):
        return FakeTransaction()


@dataclass
class FakeAcquire:
    conn: FakeConn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


@dataclass
class FakeStore:
    """Fake asyncpg pool: routes by SQL substring to canned rows.

    ``matches``: list of (substring, rows_or_row) tried in order per call.
    """

    matches: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def _rows_for(self, sql):
        for needle, rows in self.matches:
            if needle in sql:
                return rows if isinstance(rows, list) else [rows]
        return []

    def fetchrow(self, sql, args):
        self.calls.append(("fetchrow", sql, args))
        rows = self._rows_for(sql)
        return rows[0] if rows else None

    def fetch(self, sql, args):
        self.calls.append(("fetch", sql, args))
        return self._rows_for(sql)

    def acquire(self):
        return FakeAcquire(FakeConn(self))


def _client(store: FakeStore, base_url: str = "http://testserver") -> TestClient:
    """``base_url`` sets the server address the request 'arrived' on: the
    x-veer-officer header is honoured only on a loopback one."""
    app = FastAPI()
    app.state.pool = store
    from traffic_oracle.enforcement import router as enforcement_router
    from traffic_oracle.vehicles import router as vehicles_router

    app.include_router(vehicles_router)
    app.include_router(enforcement_router)
    return TestClient(app, base_url=base_url)


class TestEarmarkValidation:
    def test_requires_enumerated_lawful_basis(self):
        with pytest.raises(Exception):
            EarmarkRequest(
                state="watch", reason="stolen vehicle FIR",
                lawful_basis="because_i_said_so",
            )

    def test_accepts_known_basis_and_caps_retention(self):
        req = EarmarkRequest(
            state="flagged", reason="FIR-linked plate",
            lawful_basis=LAWFUL_BASIS_CODES[0], retention_days=90,
        )
        assert req.retention_days == 90
        with pytest.raises(Exception):
            EarmarkRequest(
                state="watch", reason="x", lawful_basis=LAWFUL_BASIS_CODES[0],
                retention_days=91,
            )

    def test_state_must_be_watch_or_flagged(self):
        with pytest.raises(Exception):
            EarmarkRequest(
                state="arrest", reason="no — vehicles only, states are "
                "watch|flagged", lawful_basis=LAWFUL_BASIS_CODES[0],
            )


class TestVehiclesRouter:
    def test_earmark_persists_and_returns_horizon(self):
        vid = uuid.uuid4()
        store = FakeStore(matches=[
            ("INSERT INTO corridor.vehicles",
             FakeRow(vehicle_id=vid, plate="GJ01EM1")),
            ("INSERT INTO corridor.vehicle_earmarks",
             FakeRow(earmark_id=uuid.uuid4(), earmarked_at=T0,
                     retention_until=T0 + timedelta(days=30))),
        ])
        c = _client(store)
        r = c.post(
            "/api/v1/vehicles/GJ01EM1/earmark",
            json={
                "state": "flagged", "reason": "FIR-linked stolen plate",
                "lawful_basis": "stolen_vehicle_fir", "retention_days": 30,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["lawful_basis"] == "stolen_vehicle_fir"
        assert body["retention_days"] == 30

    def test_listing_requires_valid_filter(self):
        c = _client(FakeStore())
        assert c.get("/api/v1/vehicles?earmark=bogus").status_code == 422

    def test_trace_404_for_unknown_vehicle(self):
        """An unknown id answers exactly like a vehicle without a lawful
        basis (C6): the read paths never confirm a vehicle was seen."""
        store = FakeStore()
        c = _client(store)
        r = c.get(f"/api/v1/vehicles/{uuid.uuid4()}/trace")
        assert r.status_code == 404
        assert r.json() == {
            "detail": "no lawful basis to disclose individual reads for this vehicle"
        }

    def test_trace_quiet_window_is_200_with_latest_t(self):
        """An EARMARKED vehicle with no sighting in the window is 'quiet',
        not 'not deployed': 200, null geometry (no LineString without two
        positions), ``status: quiet``, latest_t of its newest (lawful) read."""
        vid = uuid.uuid4()
        store = FakeStore(matches=[
            ("AS lawful_reads", FakeRow(earmarked=True, lawful_reads=True)),
            ("max(s.t) AS latest_t", FakeRow(latest_t=T0)),
        ])
        c = _client(store)
        r = c.get(f"/api/v1/vehicles/{vid}/trace?window_sec=900")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["geometry"] is None
        assert body["positions"] == [] and body["count"] == 0
        assert body["latest_t"] == T0.isoformat()
        assert body["status"] == "quiet" and body["disclosure"] == "active_earmark"
        # earmarked vehicle that was never sighted: still 200, latest_t null
        store2 = FakeStore(matches=[
            ("AS lawful_reads", FakeRow(earmarked=True, lawful_reads=False)),
        ])
        r2 = _client(store2).get(f"/api/v1/vehicles/{vid}/trace")
        assert r2.status_code == 200 and r2.json()["latest_t"] is None

    def test_trace_shape_and_source_badge(self):
        trace_rows = [
            FakeRow(t=T0, lon=72.58, lat=22.99, speed_kmh=52.0,
                    source="emulated_anpr", camera_id="EM-CAM-01"),
            FakeRow(t=T0 + timedelta(minutes=5), lon=72.59, lat=22.98,
                    speed_kmh=54.0, source="emulated_anpr", camera_id="EM-CAM-02"),
        ]
        store = FakeStore(matches=[
            ("AS lawful_reads", FakeRow(earmarked=True, lawful_reads=True)),
            ("ST_X(s.position)", trace_rows),
        ])
        c = _client(store)
        r = c.get(f"/api/v1/vehicles/{uuid.uuid4()}/trace?window_sec=3600")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["geometry"]["type"] == "LineString"
        assert len(body["geometry"]["coordinates"]) == 2
        assert all(p["source"] == "emulated_anpr" for p in body["positions"])
        assert body["source"] == "emulated_anpr"
        assert body["status"] == "tracking"
        assert body["latest_t"] == (T0 + timedelta(minutes=5)).isoformat()
        # every read path filters future-stamped rows
        sqls = [sql for kind, sql, _ in store.calls if kind == "fetch"]
        trace_sql = [s for s in sqls if "ST_X(s.position)" in s][0]
        assert "s.t <= NOW()" in trace_sql

    def test_reap_is_throttled_not_per_request(self):
        """The retention reap runs at most once per REAP_MIN_INTERVAL_SEC —
        never on every GET — under a transaction-scoped advisory lock."""
        from traffic_oracle.vehicles import store as vstore

        vstore.REAPER.reset()
        try:
            store = FakeStore(matches=[
                ("pg_try_advisory_xact_lock", FakeRow(got=True)),
            ])
            c = _client(store)
            vid = uuid.uuid4()
            for _ in range(3):
                c.get(f"/api/v1/vehicles/{vid}/sightings")
            deletes = [
                sql for kind, sql, _ in store.calls
                if kind == "fetch" and sql.lstrip().upper().startswith("DELETE")
            ]
            # one reap = the three retention DELETEs, once, not per GET
            assert len(deletes) == 3
            assert vstore.REAPER.due() is False
        finally:
            vstore.REAPER.reset()

    def test_reap_sql_spares_decided_recommendation_evidence(self):
        from traffic_oracle.vehicles import store as vstore

        for sql in (vstore.REAP_DEFAULT_SQL, vstore.REAP_EARMARKED_SQL):
            assert "events.enforcement_recommendations" in sql
            assert "r.status <> 'pending'" in sql
            assert "RETURNING" in sql
        # the earmarked reap uses the earmark's own horizon (it used to
        # require created_at > retention_until while the earmark is still
        # active — a predicate that can never hold)
        assert "created_at > e.retention_until" not in vstore.REAP_EARMARKED_SQL
        # ... and an earmark only ever EXTENDS retention past the default
        assert ("make_interval(days => GREATEST(e.retention_days, $1::int))"
                in vstore.REAP_EARMARKED_SQL)

    def test_earmark_normalizes_plate_and_returns_earmark_state(self):
        vid = uuid.uuid4()
        store = FakeStore(matches=[
            ("INSERT INTO corridor.vehicles",
             FakeRow(vehicle_id=vid)),
            ("INSERT INTO corridor.vehicle_earmarks",
             FakeRow(earmark_id=uuid.uuid4(), earmarked_at=T0,
                     retention_until=T0 + timedelta(days=30))),
        ])
        c = _client(store)
        r = c.post(
            "/api/v1/vehicles/gj 27 em-9001/earmark",
            json={
                "state": "flagged", "reason": "FIR-linked stolen plate",
                "lawful_basis": "stolen_vehicle_fir",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["plate"] == "GJ27EM9001"
        assert body["earmark_state"] == "flagged"
        upsert = [a for k, sql, a in store.calls if "INSERT INTO corridor.vehicles" in sql]
        assert upsert and upsert[0][0] == "GJ27EM9001"
        assert c.post(
            "/api/v1/vehicles/NOT-A-PLATE!!/earmark",
            json={"state": "watch", "reason": "x" * 5,
                  "lawful_basis": "mva_1988_s177"},
        ).status_code == 422

    def test_earmark_clear_records_officer(self, monkeypatch):
        vid = uuid.uuid4()
        eid = uuid.uuid4()
        store = FakeStore(matches=[
            ("UPDATE corridor.vehicle_earmarks",
             [FakeRow(earmark_id=eid, cleared_at=T0, cleared_by="Inspector R. Patel")]),
        ])
        c = _client(store)
        r = c.post(f"/api/v1/vehicles/{vid}/earmark/clear",
                   json={"officer": "Inspector R. Patel"})
        assert r.status_code == 200, r.text
        assert r.json()["cleared_by"] == "Inspector R. Patel"
        assert r.json()["officer_recorded"] is True
        upd = [a for k, sql, a in store.calls if "UPDATE corridor.vehicle_earmarks" in sql]
        assert upd[0][1] == "Inspector R. Patel"
        # the deck's legacy empty body is still accepted (unattributed) ...
        store2 = FakeStore(matches=[
            ("UPDATE corridor.vehicle_earmarks",
             [FakeRow(earmark_id=eid, cleared_at=T0, cleared_by=None)]),
        ])
        r2 = _client(store2).post(f"/api/v1/vehicles/{vid}/earmark/clear", json={})
        assert r2.status_code == 200 and r2.json()["officer_recorded"] is False
        # ... unless the deployment requires the officer
        monkeypatch.setenv("VVIP_REQUIRE_OFFICER", "1")
        r3 = _client(store2).post(f"/api/v1/vehicles/{vid}/earmark/clear", json={})
        assert r3.status_code == 422


class TestEnforcementDecisionFlow:
    def _decide_client(self, sighting_t=None, rule_id="R_SPD_SEGMENT", stolen=False):
        if sighting_t is None:  # a read well in the past (T0 is a demo date)
            sighting_t = datetime.now(timezone.utc) - timedelta(minutes=5)
        rid = uuid.uuid4()
        store = FakeStore(matches=[
            ("SELECT r.recommendation_id, r.status, r.audit_tail",
             FakeRow(recommendation_id=rid, status="pending", audit_tail=[],
                     sighting_t=sighting_t, rule_id=rule_id,
                     vehicle_id=uuid.uuid4(), stolen_vehicle=stolen)),
            ("UPDATE events.enforcement_recommendations",
             FakeRow(recommendation_id=rid, status="approved",
                     decided_by="Inspector R. Patel", decided_at=T0)),
        ])
        return _client(store), store, rid

    def test_decision_requires_officer(self):
        c, store, rid = self._decide_client()
        r = c.post(
            f"/api/v1/enforcement/recommendations/{rid}/decision",
            json={"decision": "approve", "officer": "x"},  # too short
        )
        assert r.status_code == 422

    def test_decision_appends_audit_and_approves(self):
        c, store, rid = self._decide_client()
        r = c.post(
            f"/api/v1/enforcement/recommendations/{rid}/decision",
            json={"decision": "approve", "officer": "Inspector R. Patel"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "approved"
        assert body["audit_tail"][-1]["actor"] == "Inspector R. Patel"
        assert body["audit_tail"][-1]["action"] == "decision:approved"

    def test_no_decision_before_the_violation(self):
        """A recommendation whose sighting still lies in the future cannot
        be decided (the audit trail may never show approval first)."""
        future = datetime.now(timezone.utc) + timedelta(minutes=6)
        c, store, rid = self._decide_client(sighting_t=future)
        r = c.post(
            f"/api/v1/enforcement/recommendations/{rid}/decision",
            json={"decision": "approve", "officer": "Inspector R. Patel"},
        )
        assert r.status_code == 409, r.text
        assert not any("UPDATE events.enforcement_recommendations" in sql
                       for _, sql, _ in store.calls)

    def test_no_decision_inside_the_ingest_skew_allowance_either(self):
        """Ingest tolerates 30 s of camera clock skew; a decision tolerates
        none — even 10 s ahead, the approval would predate the violation."""
        ahead = datetime.now(timezone.utc) + timedelta(seconds=10)
        c, store, rid = self._decide_client(sighting_t=ahead)
        r = c.post(
            f"/api/v1/enforcement/recommendations/{rid}/decision",
            json={"decision": "approve", "officer": "Inspector R. Patel"},
        )
        assert r.status_code == 409, r.text
        assert "retry after" in r.json()["detail"]
        assert not any("UPDATE events.enforcement_recommendations" in sql
                       for _, sql, _ in store.calls)

    # ── WP-16: the deck-verified officer header ──────────────────────────

    def _update_args(self, store) -> tuple:
        (args,) = [a for _k, sql, a in store.calls
                   if "UPDATE events.enforcement_recommendations" in sql]
        return args

    def test_officer_header_decides_and_a_differing_claim_is_kept(self):
        c, store, rid = self._decide_client()
        c = _client(store, base_url="http://127.0.0.1")
        r = c.post(
            f"/api/v1/enforcement/recommendations/{rid}/decision",
            headers={"x-veer-officer": "Duty%20Officer%20A"},
            json={"decision": "approve", "officer": "Inspector R. Patel"},
        )
        assert r.status_code == 200, r.text
        status, decided_by, _at, tail_json, _rid = self._update_args(store)
        assert (status, decided_by) == ("approved", "Duty Officer A")
        entry = json.loads(tail_json)[-1]
        assert entry["actor"] == "Duty Officer A"
        assert entry["actor_source"] == "x-veer-officer"
        assert entry["claimed_officer"] == "Inspector R. Patel"
        assert r.json()["audit_tail"][-1] == entry

    def test_officer_header_alone_is_enough(self):
        c, store, rid = self._decide_client()
        c = _client(store, base_url="http://127.0.0.1")
        r = c.post(
            f"/api/v1/enforcement/recommendations/{rid}/decision",
            headers={"x-veer-officer": "Duty%20Officer%20A"},
            json={"decision": "reject", "note": "plate misread"},
        )
        assert r.status_code == 200, r.text
        entry = json.loads(self._update_args(store)[3])[-1]
        assert entry["actor"] == "Duty Officer A" and "claimed_officer" not in entry
        assert entry["note"] == "plate misread"

    def test_no_officer_anywhere_is_422_and_nothing_is_decided(self):
        c, store, rid = self._decide_client()
        r = c.post(f"/api/v1/enforcement/recommendations/{rid}/decision",
                   json={"decision": "approve"})
        assert r.status_code == 422
        assert not any("UPDATE events.enforcement_recommendations" in sql
                       for _, sql, _ in store.calls)

    def test_header_off_loopback_is_ignored_and_the_body_officer_decides(self):
        c, store, rid = self._decide_client()  # http://testserver: not loopback
        r = c.post(
            f"/api/v1/enforcement/recommendations/{rid}/decision",
            headers={"x-veer-officer": "Duty%20Officer%20A"},
            json={"decision": "approve", "officer": "Inspector R. Patel"},
        )
        assert r.status_code == 200, r.text
        entry = json.loads(self._update_args(store)[3])[-1]
        assert entry == {k: entry[k] for k in ("actor", "action", "at", "note")}
        assert entry["actor"] == "Inspector R. Patel"

    def test_malformed_header_is_422(self):
        c, store, rid = self._decide_client()
        c = _client(store, base_url="http://127.0.0.1")
        r = c.post(
            f"/api/v1/enforcement/recommendations/{rid}/decision",
            headers={"x-veer-officer": "Duty%00Officer"},
            json={"decision": "approve", "officer": "Inspector R. Patel"},
        )
        assert r.status_code == 422
        assert not any("UPDATE events.enforcement_recommendations" in sql
                       for _, sql, _ in store.calls)

    def test_body_officer_with_control_characters_is_422_not_500(self):
        c, store, rid = self._decide_client()
        r = c.post(
            f"/api/v1/enforcement/recommendations/{rid}/decision",
            json={"decision": "approve", "officer": "Inspector\u0000Patel"},
        )
        assert r.status_code == 422

    def test_decision_value_validated(self):
        c, store, rid = self._decide_client()
        r = c.post(
            f"/api/v1/enforcement/recommendations/{rid}/decision",
            json={"decision": "execute", "officer": "Inspector R. Patel"},
        )
        assert r.status_code == 422

    def test_listing_status_validated(self):
        c = _client(FakeStore())
        assert c.get(
            "/api/v1/enforcement/recommendations?status=served"
        ).status_code == 422


class TestInterceptionPlan:
    """vehicles/pursuit.py — heading picks the downstream end, the ranking
    is ONE statement (no per-junction queries), age/stale are reported."""

    def test_direction_of_travel(self):
        from traffic_oracle.vehicles.pursuit import direction_of_travel

        assert direction_of_travel(78.0, 53.1) == "end"      # with the segment
        assert direction_of_travel(250.0, 53.1) == "start"   # against it
        assert direction_of_travel(140.0, 53.1) == "unknown"  # across it
        assert direction_of_travel(None, 53.1) == "unknown"
        assert direction_of_travel(78.0, None) == "unknown"

    def _plan_store(self, sighting_t, heading=78.0, oneway=False, roots=None):
        eid, vid = uuid.uuid4(), uuid.uuid4()
        if roots is None:  # the legal movements leaving J3 (the SQL filters)
            roots = [
                FakeRow(via_junction_id=3, to_segment_id=1005, turn_cost_sec=12.0),
                FakeRow(via_junction_id=3, to_segment_id=1012, turn_cost_sec=12.0),
            ]
        rows = [
            ("FROM events.corridor_events", FakeRow(
                event_id=eid, event_kind="vehicle_pursuit", status="planning",
                title="pursuit", metadata=json.dumps({"vehicle_id": str(vid)}))),
            ("FROM corridor.vehicle_sightings", FakeRow(
                sighting_id=uuid.uuid4(), plate="GJ27EM9001", camera_id="EM-CAM-02",
                t=sighting_t, lon=72.5733, lat=23.0240, speed_kmh=96.0,
                heading=heading, source="emulated_anpr")),
            ("WITH pt AS", FakeRow(
                segment_id=1015, dist_m=3.0, frac=0.25, len_m=970.0,
                speed_limit_kmh=60, bearing_deg=53.1, oneway=oneway,
                start_junction_id=2, end_junction_id=3)),
            ("SELECT sa.via_junction_id, sa.to_segment_id, sa.turn_cost_sec", roots),
            ("WITH roots AS", [
                FakeRow(junction_id=3, junction_type="signal", signal_control=True,
                        name="Lal Darwaja", lon=72.5798, lat=23.0254, cost_sec=43.6),
                FakeRow(junction_id=5, junction_type="intersection", signal_control=True,
                        name=None, lon=72.59, lat=23.018, cost_sec=177.0),
                FakeRow(junction_id=2, junction_type="roundabout", signal_control=False,
                        name=None, lon=72.5714, lat=23.0225, cost_sec=187.3),
            ]),
        ]
        return FakeStore(matches=rows), eid

    def test_plan_ranks_downstream_and_reports_age(self):
        now = datetime.now(timezone.utc)
        store, eid = self._plan_store(now - timedelta(seconds=60))
        c = _client_with_pursuit(store)
        r = c.get(f"/api/v1/events/{eid}/interception-plan")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["direction_of_travel"]["basis"] == "heading"
        assert body["direction_of_travel"]["towards_junction_ids"] == [3]
        assert body["stale"] is False and 55 <= body["sighting_age_sec"] <= 120
        ranked = [j["junction_id"] for j in body["interception_junctions"]]
        assert ranked[0] == 3 and body["interception_junctions"][0]["downstream"]
        # 43.6 s from the sighting, 60 s ago: the vehicle is likely past it
        assert body["interception_junctions"][0]["likely_passed"] is True
        assert all(j["graph_cost_sec"] > 0 for j in body["interception_junctions"])
        # the ranking is ONE statement whatever the junction count (no N+1)
        assert len(store.calls) == 5
        (rank_args,) = [a for _k, sql, a in store.calls if "WITH roots AS" in sql]
        roots, bases, horizon, down_ids, down_cost, snap_m = rank_args
        assert roots == [1005, 1012] and down_ids == [3] and snap_m == 25.0
        assert body["direction_of_travel"]["onward_segment_ids"] == [1005, 1012]
        assert body["direction_of_travel"]["against_oneway"] is False
        remaining = 0.75 * 970.0 / (60 / 3.6)
        assert bases == [pytest.approx(remaining + 12.0)] * 2
        assert down_cost == [pytest.approx(remaining)]

    def test_plan_flags_a_stale_sighting(self):
        store, eid = self._plan_store(
            datetime.now(timezone.utc) - timedelta(hours=4)
        )
        body = _client_with_pursuit(store).get(
            f"/api/v1/events/{eid}/interception-plan"
        ).json()
        assert body["stale"] is True and "STALE" in body["note"]

    def test_plan_without_heading_considers_both_ends(self):
        store, eid = self._plan_store(datetime.now(timezone.utc), heading=None)
        body = _client_with_pursuit(store).get(
            f"/api/v1/events/{eid}/interception-plan"
        ).json()
        assert body["direction_of_travel"]["basis"] == "unknown"
        assert body["direction_of_travel"]["towards_junction_ids"] == [3, 2]

    def test_latest_sighting_excludes_future_rows(self):
        store, eid = self._plan_store(datetime.now(timezone.utc))
        _client_with_pursuit(store).get(f"/api/v1/events/{eid}/interception-plan")
        (sql,) = [q for _k, q, _a in store.calls if "FROM corridor.vehicle_sightings" in q]
        assert "t <= NOW()" in sql

    def test_one_way_seed_segment_without_heading_takes_its_legal_direction(self):
        store, eid = self._plan_store(
            datetime.now(timezone.utc), heading=None, oneway=True
        )
        body = _client_with_pursuit(store).get(
            f"/api/v1/events/{eid}/interception-plan"
        ).json()
        assert body["direction_of_travel"]["basis"] == "oneway"
        assert body["direction_of_travel"]["towards_junction_ids"] == [3]  # its end
        assert body["direction_of_travel"]["against_oneway"] is False

    def test_read_against_a_one_way_segment_is_projected_as_observed_and_flagged(self):
        store, eid = self._plan_store(
            datetime.now(timezone.utc), heading=233.1, oneway=True,
            roots=[FakeRow(via_junction_id=2, to_segment_id=1001, turn_cost_sec=15.0)],
        )
        body = _client_with_pursuit(store).get(
            f"/api/v1/events/{eid}/interception-plan"
        ).json()
        assert body["direction_of_travel"]["towards_junction_ids"] == [2]  # its start
        assert body["direction_of_travel"]["against_oneway"] is True
        assert "AGAINST one-way" in body["note"]

    def test_plan_sql_carries_the_road_graph_one_way_rules(self):
        """Every raw segment_adjacency read applies the 012 view's rule:
        roots ENTER a one-way segment only at its start, and a one-way
        segment reaches only its END junction (the raw table still lists
        the movements the directed view excludes)."""
        from traffic_oracle.vehicles import pursuit

        roots = " ".join(pursuit._SQL_ROOTS.split())
        assert "ST_StartPoint(rt.geom)" in roots and "rt.oneway" in roots
        rank = " ".join(pursuit._SQL_RANK.split())
        assert "ST_EndPoint(rs.geom)" in rank and "rs.oneway" in rank
        assert "FROM corridor.road_graph" in rank and "directed := true" in rank


def _client_with_pursuit(store: FakeStore) -> TestClient:
    app = FastAPI()
    app.state.pool = store
    from traffic_oracle.vehicles.pursuit import router as pursuit_router

    app.include_router(pursuit_router)
    return TestClient(app)


def _api_paths(app) -> set:
    """Materialize every registered path (FastAPI >= 0.143 lazy includes)."""
    out = set()
    for route in app.router.routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            for sub in inner.routes:
                p = getattr(sub, "path", None)
                if p:
                    out.add(p)
        else:
            p = getattr(route, "path", None)
            if p:
                out.add(p)
    return out


class TestCoordinationStubs:
    def test_real_watchlist_routers_mount_and_stub_absent_streams(self):
        app = FastAPI()
        register_coordination_routers(app)
        paths = _api_paths(app)
        assert "/api/v1/vehicles" in paths
        assert "/api/v1/enforcement/recommendations" in paths
        assert "/api/v1/enforcement/recommendations/{recommendation_id}/decision" in paths
        # standalone: sibling streams absent -> their paths carry 503 stubs
        with TestClient(app) as client:
            r = client.get("/api/v1/signals/state")
            assert r.status_code == 503 and r.json() == {"detail": "not deployed"}
            r = client.get("/api/v1/units")
            assert r.status_code == 503
            r = client.get("/api/v1/adaptive/situation_log")
            assert r.status_code == 503
            # and the REAL vehicles router answers (503 pool, not 'not deployed')
            r = client.get("/api/v1/vehicles")
            assert r.status_code == 503
            assert r.json() == {"detail": "database pool unavailable"}

    def test_stub_self_skips_when_route_exists(self):
        app = FastAPI()

        @app.get("/api/v1/signals/state")
        async def _real():  # pragma: no cover - sibling stream's router
            return {"ok": True}

        register_coordination_routers(app)
        with TestClient(app) as client:
            assert client.get("/api/v1/signals/state").json() == {"ok": True}


# ── interception plan on the real graph (READ ONLY store check) ───────────
#
# The fake-pool tests above pin the SQL shape; this runs the REAL endpoint
# against VEER_TEST_PG_DSN (001-020 + the seed files: the real OpenStreetMap
# network) so the one-way legality is checked on the actual Gandhi Road
# (1012, one-way J3 -> J15, beside two-way Relief Road 1005).  Only the
# pursuit event and the latest sighting are stubbed
# (the two rows a test would otherwise have to write); the transaction is
# READ ONLY, so the check is safe against any store.

_DSN = os.environ.get("VEER_TEST_PG_DSN", "")

needs_store = pytest.mark.skipif(
    not _DSN,
    reason="set VEER_TEST_PG_DSN to a store with 001-020 + the seed files — "
           "the 49-segment network on OpenStreetMap (read-only check)",
)


class _StubbedConn:
    """The real asyncpg connection, minus the event + latest sighting."""

    def __init__(self, real, event: dict, sighting: dict) -> None:
        self._real = real
        self._event = event
        self._sighting = sighting

    async def fetchrow(self, sql, *args):
        if "FROM events.corridor_events" in sql:
            return self._event
        if "FROM corridor.vehicle_sightings" in sql:
            return self._sighting
        return await self._real.fetchrow(sql, *args)

    async def fetch(self, sql, *args):
        return await self._real.fetch(sql, *args)


def _plan_on_store(lon: float, lat: float, heading: float | None) -> dict:
    from types import SimpleNamespace

    import asyncpg

    from traffic_oracle.vehicles.pursuit import interception_plan

    async def run():
        real = await asyncpg.connect(_DSN)
        tx = real.transaction(readonly=True)
        await tx.start()
        try:
            eid, vid = uuid.uuid4(), uuid.uuid4()
            conn = _StubbedConn(
                real,
                {"event_id": eid, "event_kind": "vehicle_pursuit",
                 "status": "active", "title": "pytest pursuit",
                 "metadata": json.dumps({"vehicle_id": str(vid)})},
                {"sighting_id": uuid.uuid4(), "plate": "GJ99ZZ0003",
                 "camera_id": "PYTEST", "source": "emulated_anpr",
                 "t": datetime.now(timezone.utc) - timedelta(seconds=5),
                 "lon": lon, "lat": lat, "speed_kmh": 40.0, "heading": heading},
            )
            pool = SimpleNamespace(acquire=lambda: FakeAcquire(conn))
            request = SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(pool=pool))
            )
            return await interception_plan(str(eid), request, max_cost_sec=600.0)
        finally:
            await tx.rollback()
            await real.close()

    return asyncio.run(run())


def _ranked(plan: dict) -> dict[int, float]:
    return {j["junction_id"]: j["graph_cost_sec"] for j in plan["interception_junctions"]}


@needs_store
class TestInterceptionPlanOnTheStore:
    def test_southbound_station_road_never_runs_gandhi_road_backwards(self):
        # Station Road (1014) heading south towards J15 — the END of one-way
        # Gandhi Road (1012, J3 -> J15).  The raw adjacency lists 1014 -> 1012
        # via J15; driving it would reach J3 against the one-way.  J3 is
        # reached LEGALLY only over two-way Relief Road (1005, J15 -> J3):
        # onward [1005, 1049] — never up Gandhi Road.
        plan = _plan_on_store(72.59954, 23.02958, 156.5)
        dot = plan["direction_of_travel"]
        assert plan["seed_segment_id"] == 1014
        assert dot["towards_junction_ids"] == [15]
        assert 1012 not in dot["onward_segment_ids"]
        assert 1005 in dot["onward_segment_ids"]
        assert _ranked(plan)[3] > _ranked(plan)[15]
        assert 15 in _ranked(plan)

    def test_gandhi_road_is_left_only_at_its_end(self):
        # on Gandhi Road (1012) driving its legal direction (east): J3 (its
        # start) is behind the vehicle — reached only by leaving at J15 and
        # coming back over Relief Road 1005, never by reversing up the one-way
        plan = _plan_on_store(72.59314, 23.02491, 90.0)
        assert plan["seed_segment_id"] == 1012
        assert plan["direction_of_travel"]["towards_junction_ids"] == [15]
        assert plan["direction_of_travel"]["against_oneway"] is False
        assert 1005 in plan["direction_of_travel"]["onward_segment_ids"]
        assert _ranked(plan)[3] > _ranked(plan)[15]
        # no heading: the one-way segment's legal direction decides
        quiet = _plan_on_store(72.59314, 23.02491, None)
        assert quiet["direction_of_travel"]["basis"] == "oneway"
        assert quiet["direction_of_travel"]["towards_junction_ids"] == [15]

    def test_relief_road_enters_gandhi_road_at_its_start_and_reaches_its_end(self):
        plan = _plan_on_store(72.59059, 23.02823, 255.5)  # Relief Road -> J3
        ranked = _ranked(plan)
        assert plan["direction_of_travel"]["towards_junction_ids"] == [3]
        assert 1012 in plan["direction_of_travel"]["onward_segment_ids"]
        # J15 (behind the vehicle) is reached over the whole of Gandhi Road,
        # after J3
        assert 15 in ranked and ranked[15] > ranked[3] + 90.0

    def test_wrong_way_read_is_projected_as_observed_and_flagged(self):
        plan = _plan_on_store(72.59314, 23.02491, 270.0)  # against Gandhi Road
        assert plan["direction_of_travel"]["against_oneway"] is True
        assert plan["direction_of_travel"]["towards_junction_ids"] == [3]
        assert "AGAINST one-way" in plan["note"]
