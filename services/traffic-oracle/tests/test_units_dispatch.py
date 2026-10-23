"""Pure-python tests — agency dispatch channel + emulated units.

Covers (signalling-janrakshak stream + WP-02 dispatch truth):
    * agencies registry — Jan Rakshak first-class, closed set.
    * units/projection  — deterministic-from-dispatch_id kinematics.
    * units/router      — the green-corridor-mirroring dispatch flow
      over a fake pool through the REAL FastAPI app: request → human
      approve/defer → lifecycle, with the append-only audit tail; the
      emulated units projection and its EMULATED badge; transition
      legality (409 on illegal walks).
    * WP-02: every mutation and its audit row commit in ONE transaction
      (an audit failure rolls the status change back); /units releases
      the units of completed/cancelled events; emulator patrol frames
      from Valkey ``units:emulated:*`` (contract C-A) are merged and
      validated; the dispatch list fetches every audit tail in ONE query;
      projected unit paths are cached between polls and searched on the
      request's HELD connection (never a nested pool acquire); a unit
      approved on several live dispatches stays visible as a conflict
      (``double_booked`` + ``conflicts``); NUL / control characters in
      officers, notes and unit ids are 422s, not 500s.
    * C-N last step (WP-21): a unit sent to a waypoint junction travels to
      the junction ITSELF and is never 'arrived' short of it at t=0; a
      pursuit's unit starts at its agency's nearest post; the graph is
      searched to each of the junction's approaches (the cheapest wins); the
      dispatch REQUEST is attributed to the verified header officer.
    * WP-16: the deck-verified ``x-veer-officer`` header is the recorded
      actor of decisions and lifecycle moves (decoded, 2..64, control /
      bidi refused, honoured only on a loopback connection); a differing
      body officer survives as ``claimed_officer`` in the audit row's
      ``detail`` (migration 014) — or, on a pre-014 store, in its note.
    * adaptive/surfacing — situation_log response shape (the ops board's
      primary band rung) + the AMA module descriptor (R9).

No database, no network: httpx ASGITransport against a bare app with a
fake pool, exactly like the existing oracle suites' style.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest

pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from traffic_oracle.__main__ import create_app  # noqa: E402
from traffic_oracle.units import AGENCIES, JAN_RAKSHAK  # noqa: E402
from traffic_oracle.units import router as units_router  # noqa: E402
from traffic_oracle.units.projection import (  # noqa: E402
    dispatch_seed,
    offset_position,
    seeded_bearing_offset_deg,
    seeded_speed_kmh,
    travel_state,
)
from traffic_oracle.units.router import (  # noqa: E402
    DISPATCH_TRANSITIONS,
    LIVE_UNIT_STATUSES,
    patrol_unit,
)


# ─── Fake pool for the dispatch/units surface ────────────────────────────────


class DispatchFakePool:
    """Canned-table fake: events, dispatches, junctions, spines, adjacency."""

    def __init__(self) -> None:
        self.event_id = "33333333-3333-3333-3333-333333333333"
        self.event_status = "active"
        self.dispatches: dict[str, dict] = {}
        self.audit: list[dict] = []
        self.fail_audit = False   # inject an audit-insert failure (atomicity)
        self.commits = 0
        self.rollbacks = 0
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
        # every segment entering a junction (C-N: the unit path ends on the
        # nearest approach, then the junction point); defaults to the one above
        self.junction_approaches: dict[int, list[int]] = {}
        self.spine_segment_ids = [101]
        self.event_kind = "protected_movement"
        # corridor.agency_stations (012): agency -> the post nearest any waypoint
        self.stations: dict[str, dict] = {}
        self.calls: list[tuple[str, tuple]] = []
        self.graph: FakeGraphDB | None = None  # routing graph searched on THIS conn
        self.detail_column = True  # migration 014: corridor.dispatch_events.detail

    # -- routing of canned queries --------------------------------------
    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        self.calls.append((sql, args))
        if "FROM events.corridor_events WHERE event_id" in sql:
            if str(args[0]) != self.event_id:
                return []
            return [
                {
                    "event_id": uuid.UUID(self.event_id),
                    "status": self.event_status,
                }
            ]
        if "FROM events.event_routes" in sql:
            return [{"segment_ids": self.spine_segment_ids, "event_kind": self.event_kind}]
        if "WHERE sa.via_junction_id = $1" in sql:
            ids = self.junction_approaches.get(args[0]) or [self.junction_adjacent_segment[args[0]]]
            return [{"segment_id": s} for s in ids]
        if sql == units_router._SQL_NEAREST_STATION:
            station = self.stations.get(args[0])
            return [station] if station else []
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
            # The projection joins the EVENT: closed events release units.
            assert "e.status IN ('planning', 'approved', 'active')" in sql
            if self.event_status not in ("planning", "approved", "active"):
                return []
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
        if sql == units_router._SQL_SHORTEST_PATH:
            # pgr_dijkstra on the HELD connection (never a nested acquire)
            return self.graph.search(int(args[0]), int(args[1])) if self.graph else []
        if sql == units_router._SQL_AUDIT_TAILS:
            wanted = {str(d) for d in args[0]}
            out = []
            for did in sorted(wanted):
                tail = [a for a in reversed(self.audit) if a["dispatch_id"] == did][: args[1]]
                out.extend(tail)
            return out
        raise AssertionError("unexpected SQL: " + sql[:70])

    async def fetch(self, sql, *args):
        return self._rows(sql, args)

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if sql == units_router._SQL_AUDIT_DETAIL_COLUMN:
            return self.detail_column
        raise AssertionError("unexpected fetchval SQL: " + sql[:70])

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

    class _Tx:
        """asyncpg-style transaction: rolls the canned tables back on error."""

        def __init__(self, pool: "DispatchFakePool") -> None:
            self.pool = pool

        async def __aenter__(self):
            import copy

            self.snapshot = (copy.deepcopy(self.pool.dispatches), list(self.pool.audit))
            return self

        async def __aexit__(self, exc_type, *exc):
            if exc_type is not None:
                self.pool.dispatches, self.pool.audit = self.snapshot
                self.pool.rollbacks += 1
            else:
                self.pool.commits += 1
            return False

    def transaction(self):
        return DispatchFakePool._Tx(self)

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
            if self.fail_audit:
                raise RuntimeError("audit insert failed (injected)")
            if len(args) > 5:
                assert self.detail_column, "detail written to a pre-014 store"
            self.audit.append(
                {
                    "dispatch_id": str(args[0]),
                    "from_status": args[1],
                    "to_status": args[2],
                    "actor": args[3],
                    "note": args[4],
                    "detail": args[5] if len(args) > 5 else None,  # jsonb text
                    "at": datetime.now(timezone.utc),
                }
            )
            return "INSERT 0 1"
        raise AssertionError("unexpected execute SQL: " + sql[:70])

    def audit_for(self, did: str) -> list[dict]:
        return [a for a in self.audit if a["dispatch_id"] == did]


class FakeGraphDB:
    """pgr-style rows over the segment id space (road_graph nodes).

    The pool answers ``_SQL_SHORTEST_PATH`` from these edges on the
    request's held connection; the graph's own pool-acquiring
    ``shortest_path`` must never run inside a request (nested acquire).
    """

    def __init__(self, edges: dict[tuple[int, int], float]) -> None:
        self.edges = edges
        self.calls: list[tuple[int, int]] = []

    def search(self, source: int, target: int) -> list[dict]:
        self.calls.append((source, target))
        if (source, target) in self.edges:
            cost = self.edges[(source, target)]
            return [
                {"seq": 1, "node": source, "edge": 7, "cost": 0.0, "agg_cost": 0.0},
                {"seq": 2, "node": target, "edge": -1, "cost": cost, "agg_cost": cost},
            ]
        return []

    async def shortest_path(self, source: int, target: int) -> list[dict]:
        raise AssertionError("nested pool acquire: graph_db.shortest_path called")


def _make_app(pool: DispatchFakePool, valkey=None, graph_db=None):
    units_router.reset_path_cache()
    units_router.reset_audit_detail_probe()
    graph = graph_db or FakeGraphDB({(101, 102): 45.0})
    pool.graph = graph
    app = create_app()
    app.state.pool = pool
    app.state.graph_db = graph
    app.state.adaptive_valkey = valkey
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
    assert body["source"] == "emulated"
    assert body["sources"]["emulated_dispatch_projection"] == 1
    unit = next(u for u in body["units"] if u["unit_id"] == "JR-1")
    assert unit["source"] == "emulated_dispatch_projection"
    assert unit["emulated"] is True
    assert unit["agency"] == "jan_rakshak"
    assert "Jan Rakshak" in unit["label"]
    assert unit["assigned_event_id"] == pool.event_id
    assert isinstance(unit["position"], list) and len(unit["position"]) == 2


# ─── WP-02: dispatch truth ───────────────────────────────────────────────────


class FakePatrolValkey:
    """scan_iter + mget over a dict (the C-A keyspace)."""

    def __init__(self, data: dict[str, str]) -> None:
        self.data = data
        self.mget_calls = 0

    def scan_iter(self, match: str = "*", count: int = 10):
        prefix = match.rstrip("*")
        keys = sorted(k for k in self.data if k.startswith(prefix))

        async def gen():
            for k in keys:
                yield k

        return gen()

    async def mget(self, keys):
        self.mget_calls += 1
        return [self.data.get(k) for k in keys]


def _run(app, method: str, path: str, **kw):
    async def go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.request(method, path, **kw)

    return asyncio.run(go())


def _fielded(app, pool: DispatchFakePool, unit_id: str, waypoint: int = 2) -> str:
    r = _run(app, "POST", "/api/v1/events/" + pool.event_id + "/dispatch",
             json={"agency": "jan_rakshak", "unit_id": unit_id, "waypoint_junction": waypoint})
    did = r.json()["dispatch_id"]
    _run(app, "POST", "/api/v1/dispatches/" + did + "/decision",
         json={"action": "approve", "officer": "dcp"})
    return did


def test_mutation_and_audit_commit_together_or_not_at_all():
    pool = DispatchFakePool()
    app = _make_app(pool)
    r = _run(app, "POST", "/api/v1/events/" + pool.event_id + "/dispatch",
             json={"agency": "police", "unit_id": "PC-1", "waypoint_junction": 2})
    did = r.json()["dispatch_id"]
    assert pool.commits == 1   # insert + its audit row: one transaction

    pool.fail_audit = True

    async def decide():
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                               base_url="http://t") as c:
            return await c.post("/api/v1/dispatches/" + did + "/decision",
                                json={"action": "approve", "officer": "dcp"})

    r = asyncio.run(decide())
    assert r.status_code == 500
    assert pool.dispatches[did]["status"] == "requested"     # no status change without its row
    assert [a["to_status"] for a in pool.audit_for(did)] == ["requested"]
    assert pool.rollbacks == 1

    pool.fail_audit = False
    _run(app, "POST", "/api/v1/dispatches/" + did + "/decision",
         json={"action": "approve", "officer": "dcp"})
    pool.fail_audit = True

    async def advance():
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                               base_url="http://t") as c:
            return await c.post("/api/v1/dispatches/" + did + "/status",
                                json={"status": "en_route", "officer": "field"})

    assert asyncio.run(advance()).status_code == 500
    assert pool.dispatches[did]["status"] == "approved"
    assert pool.rollbacks == 2


def test_create_dispatch_refused_for_a_closed_event():
    pool = DispatchFakePool()
    pool.event_status = "completed"
    r = _run(_make_app(pool), "POST", "/api/v1/events/" + pool.event_id + "/dispatch",
             json={"agency": "police", "unit_id": "PC-1"})
    assert r.status_code == 409
    assert pool.dispatches == {} and pool.audit == []


def test_units_released_when_their_event_closes():
    pool = DispatchFakePool()
    app = _make_app(pool)
    _fielded(app, pool, "JR-EM-01")
    assert [u["unit_id"] for u in _run(app, "GET", "/api/v1/units").json()["units"]] == ["JR-EM-01"]
    pool.event_status = "completed"   # e.g. closed by the emulator straight in SQL
    body = _run(app, "GET", "/api/v1/units").json()
    assert body["units"] == [] and body["count"] == 0


def test_patrol_frames_merge_with_projection_and_are_validated():
    pool = DispatchFakePool()
    valkey = FakePatrolValkey(
        {
            "units:emulated:T1": json.dumps({"unit_id": "T1", "agency": "jan_rakshak",
                                             "lon": 72.58, "lat": 23.02, "status": "patrol"}),
            "units:emulated:JR-1": json.dumps({"unit_id": "JR-1", "agency": "jan_rakshak",
                                               "lon": 72.582, "lat": 23.042, "status": "en_route",
                                               "t": datetime.now(timezone.utc).isoformat(),
                                               "heading": 90, "speed_kmh": 30, "run_id": "20"}),
            "units:emulated:BAD-AGENCY": json.dumps({"unit_id": "BAD-AGENCY", "agency": "army",
                                                     "lon": 72.5, "lat": 23.0}),
            "units:emulated:NO-POS": json.dumps({"unit_id": "NO-POS", "agency": "police"}),
            "units:emulated:JUNK": "not json",
        }
    )
    app = _make_app(pool, valkey=valkey)
    _fielded(app, pool, "JR-1")
    body = _run(app, "GET", "/api/v1/units").json()
    by_id = {u["unit_id"]: u for u in body["units"]}
    assert set(by_id) == {"JR-1", "T1"}
    t1 = by_id["T1"]
    assert t1["emulated"] is True and t1["source"] == "emulated_patrol"
    assert t1["position"] == [72.58, 23.02] and t1["label"] == "Jan Rakshak · T1"
    assert t1["assigned_event_id"] is None and t1["t"] is None
    jr1 = by_id["JR-1"]
    assert jr1["position"] == [72.582, 23.042]            # the frame wins the position
    assert jr1["position_source"] == "emulated_patrol"
    assert jr1["assignment_source"] == "dispatch"
    assert jr1["assigned_event_id"] == pool.event_id and jr1["eta_sec"] is None
    assert body["sources"] == {"emulated_dispatch_projection": 0, "emulated_patrol": 2}
    assert body["patrol_feed"]["rejected"] == 3 and body["patrol_feed"]["status"] == "ok"
    assert valkey.mget_calls == 1                          # one batched read, not N GETs


def test_patrol_unit_validation_is_pure():
    now = datetime.now(timezone.utc)
    ok = patrol_unit("units:emulated:T9", {"agency": "ambulance", "lon": 72.6, "lat": 23.0,
                                           "status": "teleporting"}, now)
    assert ok["unit_id"] == "T9" and ok["status"] == "unknown"
    assert ok["agency_label"] == "108 Ambulance"
    assert patrol_unit("units:emulated:x y", {"agency": "police", "lon": 1, "lat": 1}, now) is None
    assert patrol_unit("units:emulated:P", {"agency": "police", "lon": 200, "lat": 1}, now) is None
    nan_doc = {"agency": "police", "lon": float("nan"), "lat": 1}
    assert patrol_unit("units:emulated:P", nan_doc, now) is None


def test_valkey_down_degrades_to_projection_only():
    class DeadValkey:
        def scan_iter(self, match="*", count=10):
            async def gen():
                raise ConnectionError("valkey down")
                yield  # pragma: no cover

            return gen()

    pool = DispatchFakePool()
    app = _make_app(pool, valkey=DeadValkey())
    _fielded(app, pool, "JR-1")
    body = _run(app, "GET", "/api/v1/units").json()
    assert [u["unit_id"] for u in body["units"]] == ["JR-1"]
    assert body["patrol_feed"]["status"] == "error"


def test_dispatch_list_reads_all_audit_tails_in_one_query():
    pool = DispatchFakePool()
    app = _make_app(pool)
    for n in range(4):
        _fielded(app, pool, f"JR-{n}")
    pool.calls.clear()
    body = _run(app, "GET", "/api/v1/dispatches").json()
    assert body["count"] == 4
    for d in body["dispatches"]:
        assert [a["to_status"] for a in d["audit"]] == ["approved", "requested"]
    tails = [sql for sql, _ in pool.calls if sql == units_router._SQL_AUDIT_TAILS]
    singles = [sql for sql, _ in pool.calls if sql == units_router._SQL_AUDIT_TAIL]
    assert len(tails) == 1 and singles == []


def test_projected_paths_are_cached_between_polls():
    pool = DispatchFakePool()
    graph = FakeGraphDB({(101, 102): 45.0})
    app = _make_app(pool, graph_db=graph)
    _fielded(app, pool, "JR-1")
    first = _run(app, "GET", "/api/v1/units").json()["units"][0]
    second = _run(app, "GET", "/api/v1/units").json()["units"][0]
    assert graph.calls == [(101, 102)]          # pgr_dijkstra once, not once per poll
    searches = [sql for sql, _ in pool.calls if sql == units_router._SQL_SHORTEST_PATH]
    assert len(searches) == 1                   # ...and on the HELD connection
    assert first["position"] is not None and second["eta_sec"] <= first["eta_sec"]
    assert isinstance(first["eta_sec"], float)  # a real graph path (staging has no ETA)


def test_double_booked_unit_stays_visible():
    pool = DispatchFakePool()
    app = _make_app(pool)
    first = _fielded(app, pool, "JR-1")
    second = _fielded(app, pool, "JR-1", waypoint=3)   # the same unit, approved again
    body = _run(app, "GET", "/api/v1/units").json()
    jr1 = [u for u in body["units"] if u["unit_id"] == "JR-1"]
    assert len(jr1) == 1                                # one row per unit...
    unit = jr1[0]
    assert unit["dispatch_id"] == second                # ...for its latest approval
    assert unit["double_booked"] is True                # ...and the conflict is visible
    assert [d["dispatch_id"] for d in unit["other_dispatches"]] == [first]
    assert body["conflicts"] == [
        {"unit_id": "JR-1", "dispatch_ids": [first, second], "event_ids": [pool.event_id]}
    ]
    _fielded(app, pool, "JR-2")
    body = _run(app, "GET", "/api/v1/units").json()
    jr2 = next(u for u in body["units"] if u["unit_id"] == "JR-2")
    assert jr2["double_booked"] is False and "other_dispatches" not in jr2


# ─── C-N last step: a unit drives to the choke junction ITSELF ───────────────

#: The gate's live case: a pursuit anchored on Tilak Road (1013, J5 -> J15),
#: Jan Rakshak sent to choke #1 = J15, the anchor's own end junction (whose
#: approaches are 1013, 1014 and 1049).  JR-KALUPUR's post sits on 1014.
J15 = (72.6006, 23.0251)
ANCHOR_1013 = (72.5953, 23.0216)   # the suspect's last sighting (segment point)


def _pursuit_pool(*, kind="vehicle_pursuit", station=True) -> DispatchFakePool:
    pool = DispatchFakePool()
    pool.event_kind = kind
    pool.spine_segment_ids = [1013]
    pool.junction_geoms[15] = J15
    pool.junction_approaches[15] = [1013, 1014, 1049]
    pool.segment_points.update({
        1013: ANCHOR_1013, 1014: (72.6053, 23.0276), 1049: (72.5953, 23.0216),
        1012: (72.5800, 23.0200),
    })
    if station:
        pool.stations["jan_rakshak"] = {
            "station_id": "JR-KALUPUR", "name": "Jan Rakshak Post - Kalupur (emulated)",
            "lon": 72.6093, "lat": 23.0306, "segment_id": 1014,
        }
    return pool


def _units_at(pool: DispatchFakePool, graph, at) -> dict[str, dict]:
    units, _meta = asyncio.run(
        units_router.collect_units(pool, graph_db=graph, valkey=None, now=at)
    )
    return {u["unit_id"]: u for u in units}


def _metres(a, b) -> float:
    from traffic_oracle.units.projection import _haversine_m

    return _haversine_m((a[0], a[1]), (b[0], b[1]))


def test_a_unit_sent_to_its_anchors_own_junction_drives_there_and_arrives_only_there():
    """The gate's C-N defect: J15 resolved to the anchor 1013 itself and the
    unit was 'arrived' at t=0 ~700 m short of the junction."""
    from datetime import timedelta

    pool = _pursuit_pool(kind="protected_movement")  # spine staging (the anchor)
    graph = FakeGraphDB({})
    app = _make_app(pool, graph_db=graph)
    did = _fielded(app, pool, "JR-INT-01", waypoint=15)
    t0 = pool.dispatches[did]["approved_at"]
    start = _units_at(pool, graph, t0)["JR-INT-01"]
    assert start["arrived"] is False and start["progress"] == 0.0
    assert start["eta_sec"] > 0 and start["staged_at"] == "spine_start"
    assert _metres(start["position"], ANCHOR_1013) < 30      # (de-stacking offset)
    later = _units_at(pool, graph, t0 + timedelta(seconds=5))["JR-INT-01"]
    assert later["arrived"] is False
    assert _metres(later["position"], J15) < _metres(start["position"], J15) - 30  # toward J15
    done = _units_at(pool, graph, t0 + timedelta(hours=1))["JR-INT-01"]
    assert done["arrived"] is True and done["progress"] == 1.0 and done["eta_sec"] == 0.0
    assert done["position"] == [J15[0], J15[1]]                 # ON the junction, never short


def test_a_pursuit_unit_comes_from_its_agencys_post_not_the_suspects_sighting():
    from datetime import timedelta

    pool = _pursuit_pool()
    graph = FakeGraphDB({})
    app = _make_app(pool, graph_db=graph)
    did = _fielded(app, pool, "JR-INT-01", waypoint=15)
    t0 = pool.dispatches[did]["approved_at"]
    unit = _units_at(pool, graph, t0)["JR-INT-01"]
    assert unit["staged_at"] == "station:JR-KALUPUR"
    assert unit["staged_at_name"] == "Jan Rakshak Post - Kalupur (emulated)"
    assert _metres(unit["position"], (72.6093, 23.0306)) < 30
    assert _metres(unit["position"], ANCHOR_1013) > 1000          # not on the suspect
    assert unit["arrived"] is False
    t_arrive = t0 + timedelta(seconds=unit["eta_sec"] + 1)
    assert _units_at(pool, graph, t_arrive)["JR-INT-01"]["position"] == [J15[0], J15[1]]


def test_a_pursuit_unit_routes_over_the_graph_to_the_cheapest_approach():
    pool = _pursuit_pool()
    pool.stations["jan_rakshak"]["segment_id"] = 1012          # a post off the approaches
    graph = FakeGraphDB({(1012, 1013): 90.0, (1012, 1049): 40.0})
    app = _make_app(pool, graph_db=graph)
    did = _fielded(app, pool, "JR-INT-02", waypoint=15)
    _units_at(pool, graph, pool.dispatches[did]["approved_at"])
    assert graph.calls == [(1012, 1013), (1012, 1014), (1012, 1049)]  # each approach once
    path = units_router._PATH_CACHE[(did, 15)][1]
    assert path.path_pts[0] == (72.6093, 23.0306)               # the post
    assert path.path_pts[-1] == J15                              # ...to the junction
    assert (72.5800, 23.0200) in path.path_pts                  # via 1012 -> 1049 (cheaper)


def test_cheapest_path_is_pure():
    paths = {
        9: [{"node": 1, "agg_cost": 0.0}, {"node": 9, "agg_cost": 50.0}],
        7: [{"node": 1, "agg_cost": 0.0}, {"node": 7, "agg_cost": 20.0}],
        5: [],  # no route to this approach: never the winner
    }
    assert units_router.cheapest_path(paths) == [1, 7]
    assert units_router.cheapest_path({}) == [] and units_router.cheapest_path({5: []}) == []


def test_without_a_graph_path_the_unit_waits_at_its_start_never_arrived():
    pool = _pursuit_pool(station=False)
    pool.spine_segment_ids = [101]                              # off the approaches
    graph = FakeGraphDB({})                                     # no path anywhere
    app = _make_app(pool, graph_db=graph)
    did = _fielded(app, pool, "JR-INT-04", waypoint=15)
    unit = _units_at(pool, graph, pool.dispatches[did]["approved_at"])["JR-INT-04"]
    assert unit["position"] == [72.571, 23.031] and unit["arrived"] is False
    assert unit["eta_sec"] is None and unit["staged_at"] == "spine_start"


# ─── C-N: the dispatch REQUEST is attributed to the verified officer ────────


def test_the_request_is_attributed_to_the_header_officer_and_the_body_is_a_claim():
    pool = DispatchFakePool()
    app = _make_app(pool)
    r = _run_at(app, LOCAL, "POST", "/api/v1/events/" + pool.event_id + "/dispatch",
                headers={"x-veer-officer": "Integration%20Gate%20Officer"},
                json={"agency": "jan_rakshak", "unit_id": "JR-INT-01", "waypoint_junction": 2,
                      "requested_by": "Somebody Else (claimed)"})
    assert r.status_code == 200, r.text
    assert r.json()["requested_by"] == "Integration Gate Officer"
    did = r.json()["dispatch_id"]
    assert pool.dispatches[did]["requested_by"] == "Integration Gate Officer"
    row = _last_audit(pool, did)
    assert row["to_status"] == "requested" and row["actor"] == "Integration Gate Officer"
    assert json.loads(row["detail"]) == {
        "actor": "Integration Gate Officer", "actor_source": "x-veer-officer",
        "claimed_officer": "Somebody Else (claimed)",
    }
    # the header alone: no claim; a matching body: no claim either
    for body_name in (None, "Integration  Gate Officer"):
        body = {"agency": "police", "unit_id": "PC-9"}
        if body_name:
            body["requested_by"] = body_name
        r = _run_at(app, LOCAL, "POST", "/api/v1/events/" + pool.event_id + "/dispatch",
                    headers={"x-veer-officer": "Integration%20Gate%20Officer"}, json=body)
        assert json.loads(_last_audit(pool, r.json()["dispatch_id"])["detail"]) == {
            "actor": "Integration Gate Officer", "actor_source": "x-veer-officer"}


def test_without_the_header_the_request_names_the_body_or_operator():
    pool = DispatchFakePool()
    app = _make_app(pool)
    named = _run(app, "POST", "/api/v1/events/" + pool.event_id + "/dispatch",
                 json={"agency": "police", "unit_id": "PC-1", "requested_by": "108-driver"})
    assert named.json()["requested_by"] == "108-driver"
    assert _last_audit(pool, named.json()["dispatch_id"])["detail"] is None
    unnamed = _run(app, "POST", "/api/v1/events/" + pool.event_id + "/dispatch",
                   json={"agency": "police", "unit_id": "PC-2"})
    assert unnamed.json()["requested_by"] == "operator"
    assert _last_audit(pool, unnamed.json()["dispatch_id"])["actor"] == "operator"


@pytest.mark.parametrize(
    "path_kind, body",
    [
        ("dispatch", {"agency": "police", "unit_id": "PC\x001"}),
        ("dispatch", {"agency": "police", "unit_id": "PC-1", "note": "a\x00b"}),
        ("dispatch", {"agency": "police", "unit_id": "PC-1", "requested_by": "op\x00"}),
        ("decision", {"action": "approve", "officer": "Duty\x00Officer"}),
        ("decision", {"action": "approve", "officer": "   "}),
        ("decision", {"action": "approve", "officer": "dcp", "note": "x\x00"}),
        ("status", {"status": "en_route", "officer": "Duty \u202eOfficer"}),
    ],
)
def test_control_characters_are_422_not_500(path_kind, body):
    pool = DispatchFakePool()
    app = _make_app(pool)
    did = _fielded(app, pool, "JR-9")
    path = {
        "dispatch": "/api/v1/events/" + pool.event_id + "/dispatch",
        "decision": "/api/v1/dispatches/" + did + "/decision",
        "status": "/api/v1/dispatches/" + did + "/status",
    }[path_kind]
    before = (len(pool.dispatches), len(pool.audit))
    r = _run(app, "POST", path, json=body)
    assert r.status_code == 422, r.text
    assert (len(pool.dispatches), len(pool.audit)) == before


# ─── WP-16: the authenticated actor (x-veer-officer) ────────────────────────


def _run_at(app, base_url: str, method: str, path: str, **kw):
    """Request with an explicit server address: the officer header is
    honoured only on a loopback connection (scope["server"])."""
    async def go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url=base_url) as c:
            return await c.request(method, path, **kw)

    return asyncio.run(go())


LOCAL = "http://127.0.0.1"


def _requested(app, pool: DispatchFakePool, unit_id: str = "JR-H1") -> str:
    r = _run(app, "POST", "/api/v1/events/" + pool.event_id + "/dispatch",
             json={"agency": "jan_rakshak", "unit_id": unit_id, "waypoint_junction": 2})
    return r.json()["dispatch_id"]


def _last_audit(pool: DispatchFakePool, did: str) -> dict:
    return pool.audit_for(did)[-1]


def test_officer_header_is_the_recorded_actor_and_a_differing_claim_is_kept():
    pool = DispatchFakePool()
    app = _make_app(pool)
    did = _requested(app, pool)
    r = _run_at(app, LOCAL, "POST", f"/api/v1/dispatches/{did}/decision",
                headers={"x-veer-officer": "Duty%20Officer%20A"},
                json={"action": "approve", "officer": "dcp", "note": "go"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decided_by"] == "Duty Officer A"
    row = _last_audit(pool, did)
    assert row["actor"] == "Duty Officer A" and row["note"] == "go"
    assert json.loads(row["detail"]) == {
        "actor": "Duty Officer A", "actor_source": "x-veer-officer", "claimed_officer": "dcp",
    }
    # the audit tail served back carries the provenance (additive key)
    assert body["audit"][0]["actor"] == "Duty Officer A"
    assert body["audit"][0]["detail"]["claimed_officer"] == "dcp"
    assert body["audit"][1]["detail"] is None  # the request row: body-attributed


def test_matching_body_officer_is_not_a_claim():
    pool = DispatchFakePool()
    app = _make_app(pool)
    did = _requested(app, pool)
    r = _run_at(app, LOCAL, "POST", f"/api/v1/dispatches/{did}/decision",
                headers={"x-veer-officer": "Duty%20Officer%20A"},
                json={"action": "approve", "officer": "Duty  Officer A"})  # same, spaced
    assert r.status_code == 200, r.text
    assert json.loads(_last_audit(pool, did)["detail"]) == {
        "actor": "Duty Officer A", "actor_source": "x-veer-officer",
    }


def test_header_alone_names_the_actor_of_every_human_move():
    pool = DispatchFakePool()
    app = _make_app(pool)
    did = _requested(app, pool)
    hdr = {"x-veer-officer": "Duty%20Officer%20A"}
    assert _run_at(app, LOCAL, "POST", f"/api/v1/dispatches/{did}/decision",
                   headers=hdr, json={"action": "approve"}).status_code == 200
    for status in ("en_route", "on_scene"):
        r = _run_at(app, LOCAL, "POST", f"/api/v1/dispatches/{did}/status",
                    headers=hdr, json={"status": status})
        assert r.status_code == 200, r.text
    assert [a["actor"] for a in pool.audit_for(did)] == [
        "operator", "Duty Officer A", "Duty Officer A", "Duty Officer A",
    ]
    r = _run_at(app, LOCAL, "POST", f"/api/v1/dispatches/{did}/status",
                headers={"x-veer-officer": "Inspector%20R.%20Patel"},
                json={"status": "completed", "officer": "field-unit"})
    assert r.status_code == 200
    assert json.loads(_last_audit(pool, did)["detail"])["claimed_officer"] == "field-unit"


def test_without_the_header_nothing_changes():
    pool = DispatchFakePool()
    app = _make_app(pool)
    did = _requested(app, pool)
    r = _run_at(app, LOCAL, "POST", f"/api/v1/dispatches/{did}/decision",
                json={"action": "approve", "officer": "dcp"})
    assert r.status_code == 200 and r.json()["decided_by"] == "dcp"
    assert _last_audit(pool, did)["detail"] is None
    # the pre-014 statement exactly (no detail column, no column probe)
    inserts = [sql for sql, _ in pool.calls if "INSERT INTO corridor.dispatch_events" in sql]
    assert all("detail" not in sql for sql in inserts)
    assert not any(sql == units_router._SQL_AUDIT_DETAIL_COLUMN for sql, _ in pool.calls)


def test_neither_header_nor_officer_is_a_422_and_changes_nothing():
    pool = DispatchFakePool()
    app = _make_app(pool)
    did = _requested(app, pool)
    before = (pool.dispatches[did]["status"], len(pool.audit))
    for path, body in ((f"/api/v1/dispatches/{did}/decision", {"action": "approve"}),
                       (f"/api/v1/dispatches/{did}/status", {"status": "cancelled"})):
        r = _run_at(app, LOCAL, "POST", path, json=body)
        assert r.status_code == 422, r.text
        assert "officer is required" in r.json()["detail"]
    assert (pool.dispatches[did]["status"], len(pool.audit)) == before


@pytest.mark.parametrize(
    "raw, why",
    [
        ("Duty%ZZOfficer", "bad escape"),
        ("Duty%E0%A4Officer", "truncated UTF-8"),
        ("A", "one character"),
        ("%20%20A%20", "one character once trimmed"),
        ("x" * 65, "65 characters"),
        ("Duty%00Officer", "NUL"),
        ("Duty%0AOfficer", "line break"),
        ("Duty%E2%80%AEOfficer", "bidi override"),
    ],
)
def test_malformed_officer_header_is_a_422_never_ignored(raw, why):
    pool = DispatchFakePool()
    app = _make_app(pool)
    did = _requested(app, pool)
    r = _run_at(app, LOCAL, "POST", f"/api/v1/dispatches/{did}/decision",
                headers={"x-veer-officer": raw}, json={"action": "approve", "officer": "dcp"})
    assert r.status_code == 422, (why, r.text)
    assert "x-veer-officer" in r.json()["detail"]
    assert pool.dispatches[did]["status"] == "requested"


def test_a_repeated_officer_header_names_no_single_officer():
    pool = DispatchFakePool()
    app = _make_app(pool)
    did = _requested(app, pool)
    r = _run_at(app, LOCAL, "POST", f"/api/v1/dispatches/{did}/decision",
                headers=[("x-veer-officer", "Duty%20Officer%20A"),
                         ("x-veer-officer", "Duty%20Officer%20B")],
                json={"action": "approve", "officer": "dcp"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "x-veer-officer must be sent exactly once"
    assert pool.dispatches[did]["status"] == "requested"


def test_indic_names_decode_from_the_percent_encoded_header():
    from urllib.parse import quote

    pool = DispatchFakePool()
    app = _make_app(pool)
    did = _requested(app, pool)
    name = "निरीक्षक पटेल"  # Devanagari — the deck encodes with encodeURIComponent
    r = _run_at(app, LOCAL, "POST", f"/api/v1/dispatches/{did}/decision",
                headers={"x-veer-officer": quote(name, safe="")},
                json={"action": "approve"})
    assert r.status_code == 200, r.text
    assert r.json()["decided_by"] == name and _last_audit(pool, did)["actor"] == name


@pytest.mark.parametrize("base_url", ["http://t", "http://10.20.30.40", "http://veer.example"])
def test_header_is_ignored_unless_the_connection_is_loopback(base_url):
    """The header is trusted only because the oracle binds loopback: on a
    request that arrived elsewhere it is ignored and the body officer
    stands, exactly as without it."""
    pool = DispatchFakePool()
    app = _make_app(pool)
    did = _requested(app, pool)
    r = _run_at(app, base_url, "POST", f"/api/v1/dispatches/{did}/decision",
                headers={"x-veer-officer": "Duty%20Officer%20A"},
                json={"action": "approve", "officer": "dcp"})
    assert r.status_code == 200 and r.json()["decided_by"] == "dcp"
    assert _last_audit(pool, did)["detail"] is None
    # …and never authenticates a body-less write
    did2 = _requested(app, pool, "JR-H2")
    r = _run_at(app, base_url, "POST", f"/api/v1/dispatches/{did2}/decision",
                headers={"x-veer-officer": "Duty%20Officer%20A"}, json={"action": "approve"})
    assert r.status_code == 422


def test_forwarded_for_cannot_switch_the_loopback_check():
    """scope["server"] (where the connection arrived), not the forwarded
    client address, decides — a proxied X-Forwarded-For changes nothing."""
    pool = DispatchFakePool()
    app = _make_app(pool)
    did = _requested(app, pool)
    r = _run_at(app, LOCAL, "POST", f"/api/v1/dispatches/{did}/decision",
                headers={"x-veer-officer": "Duty%20Officer%20A",
                         "x-forwarded-for": "203.0.113.9"},
                json={"action": "approve", "officer": "dcp"})
    assert r.json()["decided_by"] == "Duty Officer A"


@pytest.mark.parametrize(
    "server, trusted",
    [
        (("127.0.0.1", 8091), True), (("127.0.0.53", 8091), True), (("::1", 8091, 0, 0), True),
        (("::ffff:127.0.0.1", 8091), True), (("localhost", 8091), True),
        (("10.0.0.5", 8091), False), (("0.0.0.0", 8091), False), (("t", None), False),
        (None, False), ((), False),
    ],
)
def test_loopback_transport_check(server, trusted):
    from starlette.requests import Request as StarletteRequest

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "server": server}
    assert units_router._loopback_transport(StarletteRequest(scope)) is trusted


def test_pre_014_store_keeps_the_claim_in_the_note():
    pool = DispatchFakePool()
    pool.detail_column = False  # migration 014 not applied yet
    app = _make_app(pool)
    did = _requested(app, pool)
    r = _run_at(app, LOCAL, "POST", f"/api/v1/dispatches/{did}/decision",
                headers={"x-veer-officer": "Duty%20Officer%20A"},
                json={"action": "approve", "officer": "dcp", "note": "go"})
    assert r.status_code == 200, r.text
    row = _last_audit(pool, did)
    assert row["actor"] == "Duty Officer A" and row["detail"] is None
    assert row["note"] == "go [x-veer-officer: Duty Officer A; claimed officer: dcp]"
    # the column probe is cached: the next attributed write does not re-ask
    probes = sum(sql == units_router._SQL_AUDIT_DETAIL_COLUMN for sql, _ in pool.calls)
    _run_at(app, LOCAL, "POST", f"/api/v1/dispatches/{did}/status",
            headers={"x-veer-officer": "Duty%20Officer%20A"}, json={"status": "en_route"})
    assert sum(sql == units_router._SQL_AUDIT_DETAIL_COLUMN for sql, _ in pool.calls) == probes == 1
    assert _last_audit(pool, did)["note"] is None  # no claim → note untouched


def test_attributed_actor_is_pure():
    assert units_router.attributed_actor(None, "dcp") == ("dcp", None)
    assert units_router.attributed_actor("Duty Officer A", None) == (
        "Duty Officer A", {"actor": "Duty Officer A", "actor_source": "x-veer-officer"},
    )
    actor, detail = units_router.attributed_actor("Duty Officer A", "Officer B")
    assert actor == "Duty Officer A" and detail["claimed_officer"] == "Officer B"
    with pytest.raises(Exception) as err:
        units_router.attributed_actor(None, None)
    assert getattr(err.value, "status_code", None) == 422


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
    # additive provenance: 'evt-1' denotes no corridor event → not emulated
    assert body["entries"][0]["emulated"] is False


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
