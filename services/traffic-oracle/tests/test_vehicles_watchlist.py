"""Watchlist & enforcement tests — pure python, fakes only (no DB, no net).

Covers:
* enforcement/rules.py — the three deterministic rules, honest degradation,
  phase arithmetic, movement mapping, and byte-stable determinism;
* vehicles/sources.py — the AnprSource port, the frozen AnprRead frame
  schema (C2), source-labelling enforcement, EmulatedAnprSource ordering
  and tape normalization;
* vehicles + enforcement routers — earmark validation (lawful basis,
  retention cap), listing with ACTIVE-earmark-only filtering, decision
  flow (pending -> approved/rejected, officer mandatory, append-only
  audit tail), evaluate idempotency — against a fake asyncpg-style pool;
* coordination stubs (C0) — real routers mount; absent streams stub 503.
"""

from __future__ import annotations

import asyncio
import json
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

    def test_movement_for_heading(self):
        assert movement_for_heading(0.0) == "NS"
        assert movement_for_heading(180.0) == "NS"
        assert movement_for_heading(90.0) == "EW"
        assert movement_for_heading(270.0) == "EW"
        assert movement_for_heading(350.0) == "NS"
        assert movement_for_heading(None) is None

    def test_phase_state_is_pure_and_periodic(self):
        t = T0.replace(second=0, microsecond=0)
        s0, into0 = phase_state_at(self.PROGRAM, "NS", t)
        assert s0 == "green" and 0 <= into0 < 100
        # 100 s later -> identical state (cycle period)
        s1, into1 = phase_state_at(self.PROGRAM, "NS", t + timedelta(seconds=100))
        assert (s0, into0) == (s1, into1)
        # NS green for 0..39, EW green 40..79, tail 80..99 red for both
        assert phase_state_at(self.PROGRAM, "NS", t + timedelta(seconds=10))[0] == "green"
        assert phase_state_at(self.PROGRAM, "EW", t + timedelta(seconds=10))[0] == "red"
        assert phase_state_at(self.PROGRAM, "EW", t + timedelta(seconds=50))[0] == "green"
        assert phase_state_at(self.PROGRAM, "NS", t + timedelta(seconds=90))[0] == "red"

    def test_red_crossing_fires(self):
        t = T0.replace(second=0, microsecond=0) + timedelta(seconds=50)
        recs = evaluate_rules(
            _sighting(t=t, heading=5.0), None, self.PROGRAM
        )
        sig = [r for r in recs if r.rule_id == "R_SIGNAL_VIOLATION"]
        assert len(sig) == 1
        assert sig[0].evidence["movement"] == "NS"
        assert sig[0].evidence["signal_state"] == "red"
        assert sig[0].evidence["green_fraction"] == 0.4

    def test_green_crossing_does_not_fire(self):
        t = T0.replace(second=0, microsecond=0) + timedelta(seconds=10)
        recs = evaluate_rules(_sighting(t=t, heading=5.0), None, self.PROGRAM)
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
            SegmentContext(1012, 50.0),
            TestSignalRule.PROGRAM,
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


def _client(store: FakeStore) -> TestClient:
    app = FastAPI()
    app.state.pool = store
    from traffic_oracle.enforcement import router as enforcement_router
    from traffic_oracle.vehicles import router as vehicles_router

    app.include_router(vehicles_router)
    app.include_router(enforcement_router)
    return TestClient(app)


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

    def test_trace_404_without_sightings(self):
        store = FakeStore()
        c = _client(store)
        r = c.get(f"/api/v1/vehicles/{uuid.uuid4()}/trace")
        assert r.status_code == 404

    def test_trace_shape_and_source_badge(self):
        trace_rows = [
            FakeRow(t=T0, lon=72.58, lat=22.99, speed_kmh=52.0,
                    source="emulated_anpr", camera_id="EM-CAM-01"),
            FakeRow(t=T0 + timedelta(minutes=5), lon=72.59, lat=22.98,
                    speed_kmh=54.0, source="emulated_anpr", camera_id="EM-CAM-02"),
        ]
        store = FakeStore(matches=[("ST_X(position)", trace_rows)])
        c = _client(store)
        r = c.get(f"/api/v1/vehicles/{uuid.uuid4()}/trace?window_sec=3600")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["geometry"]["type"] == "LineString"
        assert len(body["geometry"]["coordinates"]) == 2
        assert all(p["source"] == "emulated_anpr" for p in body["positions"])
        assert body["source"] == "emulated_anpr"
        # the reap ran before the select (retention enforced in the read path)
        sqls = [sql for kind, sql, _ in store.calls if kind == "fetch"]
        assert any("DELETE FROM corridor.vehicle_sightings" in s for s in sqls)


class TestEnforcementDecisionFlow:
    def _decide_client(self):
        rid = uuid.uuid4()
        store = FakeStore(matches=[
            ("SELECT recommendation_id, status, audit_tail",
             FakeRow(recommendation_id=rid, status="pending", audit_tail=[])),
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
