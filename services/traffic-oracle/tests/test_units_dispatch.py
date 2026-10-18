"""Pure-python tests — agency dispatch channel + emulated units.

Covers (signalling-janrakshak stream):
    * agencies registry — Jan Rakshak first-class, closed set.
    * units/projection  — deterministic-from-dispatch_id kinematics.
    * units/router      — the green-corridor-mirroring dispatch flow
      over a fake pool through the REAL FastAPI app: request → human
      approve/defer → lifecycle, with the append-only audit tail; the
      emulated units projection and its EMULATED badge; transition
      legality (409 on illegal walks).
    * adaptive/surfacing — situation_log response shape (the ops board's
      primary band rung) + the AMA module descriptor (R9).

No database, no network: httpx ASGITransport against a bare app with a
fake pool, exactly like the existing oracle suites' style.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from traffic_oracle.__main__ import create_app  # noqa: E402
from traffic_oracle.units import AGENCIES, JAN_RAKSHAK  # noqa: E402
from traffic_oracle.units.projection import (  # noqa: E402
    dispatch_seed,
    offset_position,
    seeded_bearing_offset_deg,
    seeded_speed_kmh,
    travel_state,
)
from traffic_oracle.units.router import DISPATCH_TRANSITIONS, LIVE_UNIT_STATUSES  # noqa: E402


# ─── Fake pool for the dispatch/units surface ────────────────────────────────


class DispatchFakePool:
    """Canned-table fake: events, dispatches, junctions, spines, adjacency."""

    def __init__(self) -> None:
        self.event_id = "33333333-3333-3333-3333-333333333333"
        self.dispatches: dict[str, dict] = {}
        self.audit: list[dict] = []
        self.junction_geoms = {
            1: (72.57, 23.03),
            2: (72.58, 23.04),
            3: (72.59, 23.05),
        }
        self.segment_points = {
            101: (72.571, 23.031),
            102: (72.581, 23.041),
        }
        self.junction_adjacent_segment = {2: 102, 3: 103}
        self.spine_segment_ids = [101]
        self.calls: list[tuple[str, tuple]] = []

    # -- routing of canned queries --------------------------------------
    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        self.calls.append((sql, args))
        if "FROM events.corridor_events WHERE event_id" in sql:
            if str(args[0]) != self.event_id:
                return []
            return [
                {
                    "event_id": uuid.UUID(self.event_id),
                    "status": "active",
                }
            ]
        if "FROM events.event_routes" in sql:
            return [{"segment_ids": self.spine_segment_ids}]
        if "WHERE sa.via_junction_id = $1" in sql:
            return [{"segment_id": self.junction_adjacent_segment[args[0]]}]
        if "FROM corridor.road_segments rs WHERE rs.segment_id = ANY" in sql:
            out = []
            for sid in args[0]:
                lon, lat = self.segment_points[sid]
                out.append({"segment_id": sid, "lon": lon, "lat": lat})
            return out
        if "FROM corridor.junctions j WHERE j.junction_id = ANY" in sql:
            wanted = args[0]
            out = []
            for jid in wanted:
                lon, lat = self.junction_geoms[jid]
                out.append({"junction_id": jid, "lon": lon, "lat": lat})
            return out
        if "d.status = ANY($1::text[])" in sql:
            live = [
                d
                for d in self.dispatches.values()
                if d["status"] in LIVE_UNIT_STATUSES
            ]
            return live[: args[1]]
        if "ORDER BY d.created_at DESC LIMIT $4" in sql:
            out = list(self.dispatches.values())
            if args[0]:
                out = [d for d in out if str(d["event_id"]) == str(args[0])]
            if args[1]:
                out = [d for d in out if d["agency"] == args[1]]
            if args[2]:
                out = [d for d in out if d["status"] == args[2]]
            return out[: args[3]]
        if "FROM corridor.dispatches d WHERE d.dispatch_id = $1::uuid" in sql:
            d = self.dispatches.get(str(args[0]))
            return [d] if d else []
        if "FROM corridor.dispatch_events e WHERE e.dispatch_id" in sql:
            did = str(args[0])
            return [
                a for a in reversed(self.audit) if a["dispatch_id"] == did
            ][: args[1]]
        raise AssertionError("unexpected SQL: " + sql[:70])

    async def fetch(self, sql, *args):
        return self._rows(sql, args)

    async def fetchrow(self, sql, *args):
        if "INSERT INTO corridor.dispatches" in sql:
            # INSERT ... RETURNING through fetchrow (router style)
            marker = await self.execute(sql, *args)
            did = marker.split()[-1]
            return {
                "dispatch_id": uuid.UUID(did),
                "created_at": self.dispatches[did]["created_at"],
            }
        if "UPDATE corridor.dispatches" in sql and "RETURNING" in sql:
            marker = await self.execute(sql, *args)
            if marker == "UPDATE 0":
                return None
            did = next(
                a for a in args
                if isinstance(a, str) and len(a) == 36 and a.count("-") == 4
            )
            return {"dispatch_id": uuid.UUID(did)}
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    class _Acq:
        def __init__(self, pool: "DispatchFakePool") -> None:
            self.pool = pool

        async def __aenter__(self):
            return self.pool

        async def __aexit__(self, *exc):
            return False

    def acquire(self):
        return DispatchFakePool._Acq(self)

    # -- execute (INSERT/UPDATE) ----------------------------------------
    async def execute(self, sql: str, *args):
        self.calls.append((sql, args))
        if "INSERT INTO corridor.dispatches" in sql:
            did = str(uuid.uuid4())
            self.dispatches[did] = {
                "dispatch_id": uuid.UUID(did),
                "event_id": uuid.UUID(args[0]),
                "agency": args[1],
                "unit_id": args[2],
                "waypoint_junction": args[3],
                "status": "requested",
                "requested_by": args[4],
                "decided_by": None,
                "decision_note": None,
                "approved_at": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            return f"INSERT 0 1 {did}"
        if "status = $1, decided_by = $2" in sql:
            to_status, officer, note, did = args[0], args[1], args[2], str(args[3])
            d = self.dispatches.get(did)
            if d is None or d["status"] != "requested":
                return "UPDATE 0"
            d["status"] = to_status
            d["decided_by"] = officer
            d["decision_note"] = note
            if to_status == "approved":
                d["approved_at"] = datetime.now(timezone.utc)
            return "UPDATE 1"
        if "status = $1, updated_at = NOW()" in sql:
            to_status, did, from_status = args[0], str(args[1]), args[2]
            d = self.dispatches.get(did)
            if d is None or d["status"] != from_status:
                return "UPDATE 0"
            d["status"] = to_status
            return "UPDATE 1"
        if "INSERT INTO corridor.dispatch_events" in sql:
            self.audit.append(
                {
                    "dispatch_id": str(args[0]),
                    "from_status": args[1],
                    "to_status": args[2],
                    "actor": args[3],
                    "note": args[4],
                    "at": datetime.now(timezone.utc),
                }
            )
            return "INSERT 0 1"
        raise AssertionError("unexpected execute SQL: " + sql[:70])

    def audit_for(self, did: str) -> list[dict]:
        return [a for a in self.audit if a["dispatch_id"] == did]


class FakeGraphDB:
    """pgr-style rows over the segment id space (road_graph nodes)."""

    def __init__(self, edges: dict[tuple[int, int], float]) -> None:
        self.edges = edges
        self.calls: list[tuple[int, int]] = []

    async def shortest_path(self, source: int, target: int) -> list[dict]:
        self.calls.append((source, target))
        if (source, target) in self.edges:
            cost = self.edges[(source, target)]
            return [
                {"seq": 1, "node": source, "cost": 0.0},
                {"seq": 2, "node": target, "cost": cost},
            ]
        return []


def _make_app(pool: DispatchFakePool):
    app = create_app()
    app.state.pool = pool
    app.state.graph_db = FakeGraphDB({(101, 102): 45.0})
    return app


@pytest.fixture
def client():
    pool = DispatchFakePool()
    app = _make_app(pool)
    transport = ASGITransport(app=app)

    class Ctx:
        def __init__(self) -> None:
            self.pool = pool

    ctx = Ctx()

    async def enter():
        client_ = AsyncClient(transport=transport, base_url="http://t")
        return client_, ctx

    # pytest-asyncio anyio style: expose a synchronous helper instead
    class SyncClient:
        def request(self, method: str, path: str, **kw):
            async def run():
                async with AsyncClient(
                    transport=transport, base_url="http://t"
                ) as c:
                    return await c.request(method, path, **kw)

            return asyncio.run(run())

    SyncClient.pool = pool
    return SyncClient()


# ─── agencies ────────────────────────────────────────────────────────────────


def test_jan_rakshak_is_first_class_agency():
    assert JAN_RAKSHAK.code == "jan_rakshak"
    assert "jan_rakshak" in AGENCIES
    assert AGENCIES["jan_rakshak"].label == "Jan Rakshak"
    assert set(AGENCIES) == {
        "jan_rakshak", "police", "traffic", "ambulance", "fire"
    }


# ─── projection determinism ─────────────────────────────────────────────────


def test_dispatch_seed_is_stable_and_bounded():
    a = dispatch_seed("44444444-4444-4444-4444-444444444444")
    b = dispatch_seed("44444444-4444-4444-4444-444444444444")
    assert a == b and 0 <= a < 2**31
    assert dispatch_seed("55555555-5555-5555-5555-555555555555") != a


def test_seeded_kinematics_are_deterministic():
    seed = dispatch_seed("44444444-4444-4444-4444-444444444444")
    assert seeded_speed_kmh(seed) == seeded_speed_kmh(seed)
    assert 32.0 <= seeded_speed_kmh(seed) <= 48.0
    assert -12.0 <= seeded_bearing_offset_deg(seed) <= 12.0


def test_travel_state_interpolates_and_arrives():
    pts = [(0.0, 0.0), (0.01, 0.0), (0.02, 0.0)]
    costs = [60.0, 60.0]  # seconds per leg
    st = travel_state(pts, costs, elapsed_sec=30, speed_kmh=40)
    assert st is not None
    assert st.arrived is False
    assert 0.0 < st.progress < 0.5
    assert st.remaining_sec == pytest.approx(90.0, abs=1.0)
    done = travel_state(pts, costs, elapsed_sec=1000, speed_kmh=40)
    assert done.arrived is True
    assert done.position == (0.02, 0.0)
    single = travel_state([(1.0, 1.0)], [], elapsed_sec=5, speed_kmh=40)
    assert single.arrived is True


def test_travel_state_same_inputs_same_output():
    pts = [(0.0, 0.0), (0.02, 0.0)]
    a = travel_state(pts, [120.0], 60, 40)
    b = travel_state(pts, [120.0], 60, 40)
    assert a == b


def test_offset_position_is_small_and_pure():
    p = (72.57, 23.03)
    q = offset_position(p, 10.0)
    assert abs(q[0] - p[0]) < 0.001 and abs(q[1] - p[1]) < 0.001
    assert offset_position(p, 10.0) == q


# ─── dispatch flow through the real app ──────────────────────────────────────


def test_dispatch_request_approve_lifecycle_audited(client):
    pool = client.pool
    # 1. REQUEST (the operator half of the green-corridor pattern)
    r = client.request(
        "POST",
        "/api/v1/events/" + pool.event_id + "/dispatch",
        json={
            "agency": "jan_rakshak",
            "unit_id": "JR-7",
            "waypoint_junction": 2,
            "requested_by": "ops-desk",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "requested"
    assert body["agency_label"] == "Jan Rakshak"
    did = body["dispatch_id"]
    assert pool.audit_for(did)[0]["to_status"] == "requested"

    # 2. DECISION — the HUMAN half: approve
    r = client.request(
        "POST", "/api/v1/dispatches/" + did + "/decision",
        json={"action": "approve", "officer": "dcp-control", "note": "go"},
    )
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["status"] == "approved"
    assert approved["decided_by"] == "dcp-control"
    assert approved["approved_at"] is not None
    statuses = [a["to_status"] for a in pool.audit_for(did)]
    assert statuses == ["requested", "approved"]

    # 3. LIFECYCLE: en_route → on_scene → completed
    for status in ("en_route", "on_scene", "completed"):
        r = client.request(
            "POST", "/api/v1/dispatches/" + did + "/status",
            json={"status": status, "officer": "jr-field"},
        )
        assert r.status_code == 200, (status, r.text)
    statuses = [a["to_status"] for a in pool.audit_for(did)]
    assert statuses == ["requested", "approved", "en_route", "on_scene", "completed"]

    # 4. LIST with audit tail
    r = client.request("GET", "/api/v1/dispatches?agency=jan_rakshak")
    assert r.status_code == 200
    listing = r.json()
    assert listing["count"] == 1
    assert listing["dispatches"][0]["audit"][0]["to_status"] == "completed"


def test_dispatch_defer_is_terminal_and_audited(client):
    pool = client.pool
    r = client.request(
        "POST", "/api/v1/events/" + pool.event_id + "/dispatch",
        json={"agency": "police", "unit_id": "PC-1", "waypoint_junction": 3},
    )
    did = r.json()["dispatch_id"]
    r = client.request(
        "POST", "/api/v1/dispatches/" + did + "/decision",
        json={"action": "defer", "officer": "dcp-control"},
    )
    assert r.json()["status"] == "deferred"
    # deferred is terminal: no lifecycle move allowed
    r = client.request(
        "POST", "/api/v1/dispatches/" + did + "/status",
        json={"status": "en_route", "officer": "x"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["allowed"] == []


def test_decision_rejected_after_decision(client):
    pool = client.pool
    r = client.request(
        "POST", "/api/v1/events/" + pool.event_id + "/dispatch",
        json={"agency": "traffic", "unit_id": "TP-9"},
    )
    did = r.json()["dispatch_id"]
    client.request(
        "POST", "/api/v1/dispatches/" + did + "/decision",
        json={"action": "approve", "officer": "dcp"},
    )
    again = client.request(
        "POST", "/api/v1/dispatches/" + did + "/decision",
        json={"action": "defer", "officer": "dcp"},
    )
    assert again.status_code == 409


def test_dispatch_validation(client):
    # unknown agency → 422
    r = client.request(
        "POST", "/api/v1/events/" + client.pool.event_id + "/dispatch",
        json={"agency": "unknown_agency", "unit_id": "X"},
    )
    assert r.status_code == 422
    # bad event uuid → 400
    r = client.request(
        "POST", "/api/v1/events/not-a-uuid/dispatch",
        json={"agency": "police", "unit_id": "X"},
    )
    assert r.status_code == 400
    # unknown event → 404 (fake pool knows exactly one event id)
    r = client.request(
        "POST", "/api/v1/events/" + str(uuid.uuid4()) + "/dispatch",
        json={"agency": "police", "unit_id": "X"},
    )
    assert r.status_code == 404


def test_units_projection_badged_emulated(client):
    pool = client.pool
    r = client.request(
        "POST", "/api/v1/events/" + pool.event_id + "/dispatch",
        json={"agency": "jan_rakshak", "unit_id": "JR-1", "waypoint_junction": 2},
    )
    did = r.json()["dispatch_id"]
    client.request(
        "POST", "/api/v1/dispatches/" + did + "/decision",
        json={"action": "approve", "officer": "dcp"},
    )
    r = client.request("GET", "/api/v1/units")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "emulated_dispatch_projection"
    unit = next(u for u in body["units"] if u["unit_id"] == "JR-1")
    assert unit["emulated"] is True
    assert unit["agency"] == "jan_rakshak"
    assert "Jan Rakshak" in unit["label"]
    assert unit["assigned_event_id"] == pool.event_id
    assert isinstance(unit["position"], list) and len(unit["position"]) == 2


def test_transition_table_shape():
    assert DISPATCH_TRANSITIONS["requested"] == ("approved", "deferred", "cancelled")
    assert DISPATCH_TRANSITIONS["deferred"] == ()
    assert DISPATCH_TRANSITIONS["completed"] == ()
    for dead in ("deferred", "completed", "cancelled"):
        assert DISPATCH_TRANSITIONS[dead] == ()


# ─── adaptive surfacing (situation_log + AMA module) ────────────────────────


class SurfacingFakePool:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def fetch(self, sql, *args):
        if "FROM adaptive.situation_log" in sql:
            return self.rows
        raise AssertionError("unexpected SQL: " + sql[:70])


def test_situation_log_shape_matches_board_parser():
    pool = SurfacingFakePool(
        [
            {
                "movement_id": "evt-1",
                "situation_class": "AMBER",
                "situation_code": "SIT-AMBER-3",
                "severity_score": 9,
                "transitioned": True,
                "protection_class": "Z",
                "observed_at": datetime(2026, 10, 13, 1, 2, 3, tzinfo=timezone.utc),
            }
        ]
    )
    app = create_app()
    app.state.pool = pool

    async def run():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/api/v1/adaptive/situation_log?event_id=evt-1")

    r = asyncio.run(run())
    assert r.status_code == 200
    body = r.json()
    # client.ts fetchSituationLog reads resp.entries → situation_class /
    # timestamp_utc / severity_score (command-deck client.ts:220-247)
    assert body["entries"][0]["situation_class"] == "AMBER"
    assert body["entries"][0]["timestamp_utc"].startswith("2026-10-13T01:02:03")
    assert body["entries"][0]["severity_score"] == 9
    assert body["subject"] == "evt-1"


def test_ama_module_descriptor_r9():
    app = create_app()

    async def run():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/api/v1/adaptive/module")

    r = asyncio.run(run())
    assert r.status_code == 200
    card = r.json()
    assert card["module"] == "Active Movement Adaptation"
    assert card["requirement"] == "R9"
    assert card["fail_closed"] is True
    names = [s["name"] for s in card["stages"]]
    assert names[:5] == [
        "Situation", "Hysteresis", "Protocol floors",
        "Twin shadow-eval", "Audited persistence",
    ]
    assert card["protocol_floors_advance_closure_sec"]["Z+"] == 180


def test_signals_state_contract_shape():
    from traffic_oracle.signals.router import _SQL_STATE  # noqa: F401 (exists)

    class StatePool:
        async def fetch(self, sql, *args):
            return [
                {
                    "junction_id": 1,
                    "cycle_sec": 110,
                    "phases": json.dumps(
                        [{"green_sec": 50, "movements": ["through"]},
                         {"green_sec": 42, "movements": ["cross"]}]
                    ),
                    "offset_sec": 0,
                    "source": "osm_assumed",
                    "lon": 72.57,
                    "lat": 23.03,
                }
            ]

    app = create_app()
    app.state.pool = StatePool()

    async def run():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/api/v1/signals/state")

    r = asyncio.run(run())
    assert r.status_code == 200
    j = r.json()["junctions"][0]
    for key in ("junction_id", "lon", "lat", "cycle_sec", "active_phase",
                "green_ratio", "source"):
        assert key in j
    assert r.json()["writer_enabled"] is False
