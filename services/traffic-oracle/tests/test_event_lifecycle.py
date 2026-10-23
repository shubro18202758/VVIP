"""Corridor-event lifecycle tests — contract C-F (WP-02, migration 013).

Covers:
    * the pure transition table (planning → approved|active|cancelled,
      approved → active|cancelled, active → completed|cancelled) and the
      dispatch closure rule — every auto-close move is a LEGAL dispatch
      transition;
    * POST /api/v1/events/{id}/status through the REAL app over a
      stateful fake pool whose transactions roll back on error: 200 shape,
      ledger row, actual_start/actual_end, 409 {detail, from_status,
      allowed}, 404, 422 (officer, to_status, uuid), 503 without the 013
      ledger, the open dispatches walked shut with actor
      'system:event_closed' IN THE SAME transaction (a failing ledger
      insert leaves the event untouched);
    * POST /api/v1/events — the C6 pursuit chain (uuid vehicle with an
      active earmark + APPROVED recommendation for that same vehicle → else
      422) and the creation ledger row with its actor;
    * GET /api/v1/events — status/since filters and validation;
    * input hygiene: a NUL / control / bidi character in the officer, a
      NUL in the note or in any text the create body persists, and a
      non-uuid movement_id are 422s that write nothing (never a 500 from
      Postgres);
    * migration 013 grammar (naive ';'-splitter safe, idempotent, schema
      only, status CHECK and the signal plan ledger in lockstep with the
      code);
    * LIVE (VVIP_TEST_DATABASE_URL = a SCRATCH clone; everything inside
      one transaction that is rolled back): create → activate → dispatch
      → complete against the real schema, plus the pursuit 422 against
      real recommendation rows.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from traffic_oracle import events as events_mod  # noqa: E402
from traffic_oracle.__main__ import create_app  # noqa: E402
from traffic_oracle.city import common  # noqa: E402
from traffic_oracle.events import (  # noqa: E402
    EVENT_STATUSES,
    EVENT_TRANSITIONS,
    LIVE_EVENT_STATUSES,
)
from traffic_oracle.units import router as units_router  # noqa: E402
from traffic_oracle.units.router import (  # noqa: E402
    DISPATCH_TRANSITIONS,
    EVENT_CLOSED_ACTOR,
    OPEN_DISPATCH_STATUSES,
    closure_status,
)

REPO = Path(__file__).resolve().parents[3]
M013 = REPO / "services" / "corridor-store" / "migrations" / "013_event_lifecycle_audit.sql"

EVENT = uuid.UUID("4508d86d-566b-438f-8414-b3d67fd7e6e4")
VEHICLE = uuid.UUID("1b94e9d2-7e11-4174-a735-7dd3be3189cf")
OTHER_VEHICLE = uuid.UUID("29aa1caa-2a7a-402a-803b-1e9d84a20af8")
REC_APPROVED = uuid.UUID("e91d4591-390a-4c55-bcb0-ffd10c769c20")
REC_PENDING = uuid.UUID("c20da1e8-40a4-4099-b54c-e7bcc3ee154c")
REC_OTHER = uuid.UUID("66666666-6666-6666-6666-666666666666")


# ─── pure tables ─────────────────────────────────────────────────────────────


def test_transition_table_is_the_contract():
    assert EVENT_TRANSITIONS == {
        "planning": ("approved", "active", "cancelled"),
        "approved": ("active", "cancelled"),
        "active": ("completed", "cancelled"),
        "completed": (),
        "cancelled": (),
    }
    assert set(EVENT_TRANSITIONS) == set(EVENT_STATUSES)
    assert LIVE_EVENT_STATUSES == ("planning", "approved", "active")
    for targets in EVENT_TRANSITIONS.values():
        assert "planning" not in targets  # nothing ever goes back to planning


@pytest.mark.parametrize("dispatch_status", OPEN_DISPATCH_STATUSES)
@pytest.mark.parametrize("event_to", ("completed", "cancelled"))
def test_closure_moves_are_legal_dispatch_transitions(dispatch_status, event_to):
    target = closure_status(dispatch_status, event_to)
    assert target in DISPATCH_TRANSITIONS[dispatch_status]
    if event_to == "completed" and dispatch_status == "on_scene":
        assert target == "completed"  # the unit finished its assignment
    else:
        assert target == "cancelled"  # recalled / never arrived / event called off


# ─── stateful fake pool (transactions roll back) ─────────────────────────────


class LifecycleDB:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.events = {
            EVENT: {
                "event_id": EVENT,
                "status": "planning",
                "event_kind": "vehicle_pursuit",
                "title": "PURSUIT — stolen GJ27EM9001",
                "actual_start": None,
                "actual_end": None,
                "updated_at": now - timedelta(hours=1),
            },
        }
        self.log: list[dict] = []
        self.dispatches: dict[uuid.UUID, dict] = {}
        self.dispatch_events: list[dict] = []
        self.recs = {
            REC_APPROVED: {
                "recommendation_id": REC_APPROVED,
                "vehicle_id": VEHICLE,
                "status": "approved",
                "decided_by": "Duty Officer R. Mehta",
            },
            REC_PENDING: {
                "recommendation_id": REC_PENDING,
                "vehicle_id": VEHICLE,
                "status": "pending",
                "decided_by": None,
            },
            REC_OTHER: {
                "recommendation_id": REC_OTHER,
                "vehicle_id": OTHER_VEHICLE,
                "status": "approved",
                "decided_by": "Duty Officer",
            },
        }
        self.earmarked = {VEHICLE}
        self.relations = {"events.event_status_log"}
        self.fail_on: str | None = None  # SQL prefix that raises (atomicity tests)
        self.seq = 0

    def add_dispatch(self, status: str, unit: str) -> uuid.UUID:
        did = uuid.uuid4()
        self.dispatches[did] = {
            "dispatch_id": did,
            "event_id": EVENT,
            "unit_id": unit,
            "agency": "jan_rakshak",
            "status": status,
            "created_at": datetime.now(timezone.utc),
        }
        return did

    def state(self) -> tuple:
        return (
            copy.deepcopy(self.events),
            copy.deepcopy(self.log),
            copy.deepcopy(self.dispatches),
            copy.deepcopy(self.dispatch_events),
        )

    def restore(self, snap: tuple) -> None:
        self.events, self.log, self.dispatches, self.dispatch_events = snap


class LifecycleConn:
    def __init__(self, db: LifecycleDB) -> None:
        self.db = db
        self.committed = 0
        self.rolled_back = 0

    class _Tx:
        def __init__(self, conn: "LifecycleConn") -> None:
            self.conn = conn

        async def __aenter__(self):
            self.snap = self.conn.db.state()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if exc_type is not None:
                self.conn.db.restore(self.snap)
                self.conn.rolled_back += 1
            else:
                self.conn.committed += 1
            return False

    def transaction(self):
        return LifecycleConn._Tx(self)

    def _check_fail(self, sql: str) -> None:
        if self.db.fail_on and sql.startswith(self.db.fail_on):
            raise RuntimeError("injected failure: " + sql[:40])

    async def fetchval(self, sql: str, *args: Any):
        if sql == common._SQL_REGCLASS:
            return args[0] in self.db.relations
        if sql == events_mod._SQL_ACTIVE_EARMARKS:
            return 1 if args[0] in self.db.earmarked else 0
        if sql == events_mod._SQL_PURSUIT_OF_RECOMMENDATION:
            for ev in self.db.events.values():
                md = ev.get("metadata") or {}
                md = json.loads(md) if isinstance(md, str) else md
                if ev.get("event_kind") == "vehicle_pursuit" and md.get("recommendation_id") == args[0]:
                    return str(ev["event_id"])
            return None
        raise AssertionError("unexpected fetchval: " + sql[:80])

    async def fetchrow(self, sql: str, *args: Any):
        self._check_fail(sql)
        db = self.db
        if sql == events_mod._SQL_EVENT_FOR_UPDATE:
            ev = db.events.get(args[0])
            return dict(ev) if ev else None
        if sql.startswith("UPDATE events.corridor_events SET status = $2"):
            eid, to_status, from_status = args
            ev = db.events.get(eid)
            if ev is None or ev["status"] != from_status:
                return None
            now = datetime.now(timezone.utc)
            ev["status"] = to_status
            if to_status == "active" and ev["actual_start"] is None:
                ev["actual_start"] = now
            if to_status in ("completed", "cancelled"):
                ev["actual_end"] = now
            return {"actual_start": ev["actual_start"], "actual_end": ev["actual_end"]}
        if sql.startswith("INSERT INTO events.event_status_log") and "RETURNING seq" in sql:
            db.seq += 1
            row = {
                "seq": db.seq,
                "event_id": args[0],
                "from_status": args[1],
                "to_status": args[2],
                "actor": args[3],
                "note": args[4],
                "at": datetime.now(timezone.utc),
            }
            db.log.append(row)
            return {"seq": row["seq"], "at": row["at"]}
        if sql.startswith(
            "UPDATE corridor.dispatches SET status = $1, updated_at = NOW() WHERE dispatch_id = $2"
        ):
            to_status, did, from_status = args
            d = db.dispatches.get(uuid.UUID(str(did)))
            if d is None or d["status"] != from_status:
                return None
            d["status"] = to_status
            return {"dispatch_id": d["dispatch_id"]}
        if sql == events_mod._SQL_PURSUIT_RECOMMENDATION:
            rec = db.recs.get(args[0])
            return dict(rec) if rec else None
        if sql.startswith("INSERT INTO events.corridor_events"):
            eid = uuid.uuid4()
            db.events[eid] = {
                "event_id": eid,
                "status": "planning",
                "event_kind": args[0],
                "title": args[3],
                "movement_id": args[5],
                "metadata": args[9],
                "actual_start": None,
                "actual_end": None,
                "updated_at": datetime.now(timezone.utc),
            }
            return {"event_id": eid, "created_at": datetime.now(timezone.utc)}
        raise AssertionError("unexpected fetchrow: " + sql[:90])

    async def fetch(self, sql: str, *args: Any):
        self._check_fail(sql)
        db = self.db
        if sql == units_router._SQL_OPEN_EVENT_DISPATCHES:
            return [
                dict(d)
                for d in db.dispatches.values()
                if str(d["event_id"]) == str(args[0]) and d["status"] in args[1]
            ]
        if sql == events_mod._SQL_EVENTS_HISTORY:
            statuses, since, limit = args
            rows = [
                e
                for e in db.events.values()
                if (not statuses or e["status"] in statuses)
                and (since is None or e["updated_at"] >= since)
            ]
            return [self._event_row(e) for e in rows][:limit]
        if sql in (events_mod._SQL_EVENT_ROUTES, events_mod._SQL_EVENT_PHASES):
            return []
        raise AssertionError("unexpected fetch: " + sql[:90])

    async def execute(self, sql: str, *args: Any):
        self._check_fail(sql)
        db = self.db
        if sql.startswith("INSERT INTO corridor.dispatch_events"):
            db.dispatch_events.append(
                {
                    "dispatch_id": str(args[0]),
                    "from_status": args[1],
                    "to_status": args[2],
                    "actor": args[3],
                    "note": args[4],
                }
            )
            return "INSERT 0 1"
        if sql.startswith("INSERT INTO events.event_status_log"):
            db.seq += 1
            db.log.append(
                {
                    "seq": db.seq,
                    "event_id": args[0],
                    "from_status": None,
                    "to_status": "planning",
                    "actor": args[1],
                    "note": args[2],
                    "at": datetime.now(timezone.utc),
                }
            )
            return "INSERT 0 1"
        if sql.startswith(("INSERT INTO events.event_routes", "INSERT INTO events.event_phases")):
            return "INSERT 0 1"
        raise AssertionError("unexpected execute: " + sql[:90])

    @staticmethod
    def _event_row(e: dict) -> dict:
        return {
            "event_id": e["event_id"],
            "event_kind": e["event_kind"],
            "protection_class": "none",
            "crowd_class": "LOW",
            "title": e["title"],
            "description": "",
            "status": e["status"],
            "movement_id": None,
            "planned_start": None,
            "planned_end": None,
            "actual_start": e["actual_start"],
            "actual_end": e["actual_end"],
            "metadata": json.dumps({}),
            "created_at": e["updated_at"],
            "updated_at": e["updated_at"],
            "zone_geojson": None,
        }


class LifecyclePool:
    def __init__(self, db: LifecycleDB | None = None) -> None:
        self.db = db or LifecycleDB()
        self.conn = LifecycleConn(self.db)

    class _Acq:
        def __init__(self, conn: LifecycleConn) -> None:
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *exc):
            return False

    def acquire(self):
        return LifecyclePool._Acq(self.conn)


def _app(pool: LifecyclePool):
    common.reset_relation_cache()
    app = create_app()
    app.state.pool = pool
    return app


def _post(app, path: str, body: dict):
    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post(path, json=body)

    return asyncio.run(run())


def _get(app, path: str):
    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.get(path)

    return asyncio.run(run())


STATUS = f"/api/v1/events/{EVENT}/status"


# ─── POST /events/{id}/status ────────────────────────────────────────────────


class TestTransitionRoute:
    def test_activate_writes_ledger_row_and_actual_start(self):
        pool = LifecyclePool()
        r = _post(
            _app(pool),
            STATUS,
            {"to_status": "active", "officer": "Duty Officer A", "note": "pursuit live"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["event_id"] == str(EVENT)
        assert (body["from_status"], body["to_status"], body["actor"]) == (
            "planning",
            "active",
            "Duty Officer A",
        )
        assert common.parse_ts(body["at"]) is not None and body["at"].endswith("Z")
        assert body["actual_start"] is not None and body["actual_end"] is None
        assert pool.db.events[EVENT]["status"] == "active"
        assert [(x["from_status"], x["to_status"], x["actor"], x["note"]) for x in pool.db.log] == [
            ("planning", "active", "Duty Officer A", "pursuit live")
        ]
        assert body["closed_dispatches"] == []

    def test_full_walk_and_terminal_409(self):
        pool = LifecyclePool()
        app = _app(pool)
        for to in ("approved", "active", "completed"):
            r = _post(app, STATUS, {"to_status": to, "officer": "Duty Officer A"})
            assert r.status_code == 200, (to, r.text)
        r = _post(app, STATUS, {"to_status": "active", "officer": "Duty Officer A"})
        assert r.status_code == 409
        body = r.json()
        assert body["from_status"] == "completed" and body["allowed"] == []
        assert "detail" in body
        assert [x["to_status"] for x in pool.db.log] == ["approved", "active", "completed"]

    def test_illegal_skip_names_the_allowed_moves(self):
        pool = LifecyclePool()
        r = _post(_app(pool), STATUS, {"to_status": "completed", "officer": "Duty Officer A"})
        assert r.status_code == 409
        assert r.json()["allowed"] == ["approved", "active", "cancelled"]
        assert pool.db.log == [] and pool.db.events[EVENT]["status"] == "planning"

    @pytest.mark.parametrize(
        "body",
        [
            {"to_status": "active", "officer": "x"},  # 1 char
            {"to_status": "active", "officer": "   "},  # blank
            {"to_status": "active", "officer": "y" * 65},  # too long
            {"to_status": "active"},  # missing
            {"to_status": "planning", "officer": "Duty Officer A"},
            {"to_status": "exploded", "officer": "Duty Officer A"},
            {"to_status": "active", "officer": "Duty\x00Officer"},  # NUL: was a 500
            {"to_status": "active", "officer": "Duty \u202eOfficer"},  # bidi override
            {"to_status": "active", "officer": "Duty Officer A", "note": "a\x00b"},
        ],
    )
    def test_invalid_bodies_are_422_and_write_nothing(self, body):
        pool = LifecyclePool()
        r = _post(_app(pool), STATUS, body)
        assert r.status_code == 422, r.text
        assert pool.db.log == []

    def test_unknown_404_and_malformed_uuid_422(self):
        app = _app(LifecyclePool())
        r = _post(
            app,
            f"/api/v1/events/{uuid.uuid4()}/status",
            {"to_status": "active", "officer": "Duty Officer A"},
        )
        assert r.status_code == 404
        r = _post(
            app,
            "/api/v1/events/rath_yatra/status",
            {"to_status": "active", "officer": "Duty Officer A"},
        )
        assert r.status_code == 422

    def test_missing_ledger_is_503_not_a_silent_write(self):
        pool = LifecyclePool()
        pool.db.relations.clear()
        r = _post(_app(pool), STATUS, {"to_status": "active", "officer": "Duty Officer A"})
        assert r.status_code == 503 and "013" in r.json()["detail"]
        assert pool.db.events[EVENT]["status"] == "planning"

    def test_completion_closes_open_dispatches_atomically(self):
        pool = LifecyclePool()
        pool.db.events[EVENT]["status"] = "active"
        on_scene = pool.db.add_dispatch("on_scene", "JR-1")
        en_route = pool.db.add_dispatch("en_route", "JR-2")
        requested = pool.db.add_dispatch("requested", "PCR-3")
        done = pool.db.add_dispatch("completed", "JR-0")
        r = _post(_app(pool), STATUS, {"to_status": "completed", "officer": "Duty Officer A"})
        assert r.status_code == 200, r.text
        closed = {
            c["unit_id"]: (c["from_status"], c["to_status"]) for c in r.json()["closed_dispatches"]
        }
        assert closed == {
            "JR-1": ("on_scene", "completed"),
            "JR-2": ("en_route", "cancelled"),
            "PCR-3": ("requested", "cancelled"),
        }
        d = pool.db.dispatches
        assert (d[on_scene]["status"], d[en_route]["status"], d[requested]["status"]) == (
            "completed",
            "cancelled",
            "cancelled",
        )
        assert d[done]["status"] == "completed"  # terminal rows untouched
        audit = pool.db.dispatch_events
        assert len(audit) == 3 and all(a["actor"] == EVENT_CLOSED_ACTOR for a in audit)
        assert all("event completed by Duty Officer A" == a["note"] for a in audit)
        assert pool.conn.committed == 1 and pool.conn.rolled_back == 0

    def test_cancel_cancels_even_on_scene_units(self):
        pool = LifecyclePool()
        pool.db.events[EVENT]["status"] = "active"
        on_scene = pool.db.add_dispatch("on_scene", "JR-1")
        r = _post(_app(pool), STATUS, {"to_status": "cancelled", "officer": "Duty Officer A"})
        assert r.status_code == 200
        assert pool.db.dispatches[on_scene]["status"] == "cancelled"

    def test_failure_mid_transaction_rolls_everything_back(self):
        pool = LifecyclePool()
        pool.db.events[EVENT]["status"] = "active"
        on_scene = pool.db.add_dispatch("on_scene", "JR-1")
        pool.db.fail_on = "INSERT INTO corridor.dispatch_events"  # the close audit fails
        app = _app(pool)

        async def run():
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
            ) as c:
                return await c.post(
                    STATUS, json={"to_status": "completed", "officer": "Duty Officer A"}
                )

        r = asyncio.run(run())
        assert r.status_code == 500
        assert pool.db.events[EVENT]["status"] == "active"  # status change rolled back
        assert pool.db.log == []  # ledger row rolled back
        assert pool.db.dispatches[on_scene]["status"] == "on_scene"  # dispatch untouched
        assert pool.conn.rolled_back == 1


# ─── POST /events — pursuit chain (C6) + creation ledger row ────────────────


def _pursuit(meta: dict, **extra: Any) -> dict:
    return {
        "event_kind": "vehicle_pursuit",
        "title": "PURSUIT — stolen GJ27EM9001",
        "route_spines": [{"label": "pursuit anchor", "segment_ids": [1015]}],
        "metadata": meta,
        **extra,
    }


class TestPursuitValidation:
    @pytest.mark.parametrize(
        "meta, fragment",
        [
            ({}, "metadata.vehicle_id"),
            (
                {"vehicle_id": "GJ27EM9001", "recommendation_id": str(REC_APPROVED)},
                "metadata.vehicle_id",
            ),
            ({"vehicle_id": str(VEHICLE)}, "metadata.recommendation_id"),
            (
                {"vehicle_id": str(VEHICLE), "recommendation_id": "not-a-uuid"},
                "metadata.recommendation_id",
            ),
            (
                {"vehicle_id": str(OTHER_VEHICLE), "recommendation_id": str(REC_OTHER)},
                "no active earmark",
            ),
            ({"vehicle_id": str(VEHICLE), "recommendation_id": str(REC_PENDING)}, "is pending"),
            ({"vehicle_id": str(VEHICLE), "recommendation_id": str(uuid.uuid4())}, "not found"),
            (
                {"vehicle_id": str(VEHICLE), "recommendation_id": str(REC_OTHER)},
                "different vehicle",
            ),
        ],
    )
    def test_broken_chain_is_422_and_creates_nothing(self, meta, fragment):
        pool = LifecyclePool()
        before = set(pool.db.events)
        r = _post(_app(pool), "/api/v1/events", _pursuit(meta))
        assert r.status_code == 422, r.text
        assert fragment in r.json()["detail"]
        assert set(pool.db.events) == before and pool.db.log == []

    def test_approved_chain_opens_with_a_creation_row(self):
        pool = LifecyclePool()
        r = _post(
            _app(pool),
            "/api/v1/events",
            _pursuit(
                {"vehicle_id": str(VEHICLE).upper(), "recommendation_id": str(REC_APPROVED)},
                officer="Duty Officer B",
            ),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "planning" and body["actor"] == "Duty Officer B"
        new_id = uuid.UUID(body["event_id"])
        stored = json.loads(pool.db.events[new_id]["metadata"])
        assert stored["vehicle_id"] == str(VEHICLE)  # canonicalized
        assert pool.db.log[-1]["from_status"] is None
        assert (pool.db.log[-1]["to_status"], pool.db.log[-1]["actor"]) == (
            "planning",
            "Duty Officer B",
        )

    def test_non_pursuit_events_need_no_chain_and_fall_back_to_metadata_actor(self):
        pool = LifecyclePool()
        r = _post(
            _app(pool),
            "/api/v1/events",
            {
                "event_kind": "protected_movement",
                "protection_class": "Z",
                "title": "Convoy",
                "route_spines": [{"segment_ids": [1001]}],
                "metadata": {"source": "convoy-brain"},
            },
        )
        assert r.status_code == 200, r.text
        assert pool.db.log[-1]["actor"] == "convoy-brain"
        r = _post(
            _app(pool),
            "/api/v1/events",
            {
                "event_kind": "work_zone",
                "title": "Metro works",
                "route_spines": [{"segment_ids": [1001]}],
            },
        )
        assert pool.db.log[-1]["actor"] == "operator"

    def test_creation_without_013_still_creates_the_event(self):
        pool = LifecyclePool()
        pool.db.relations.clear()
        r = _post(
            _app(pool),
            "/api/v1/events",
            {
                "event_kind": "work_zone",
                "title": "Metro works",
                "route_spines": [{"segment_ids": [1001]}],
            },
        )
        assert r.status_code == 200
        assert pool.db.log == []  # no ledger to write to — never a failed create

    def test_officer_field_is_validated(self):
        r = _post(
            _app(LifecyclePool()),
            "/api/v1/events",
            {
                "event_kind": "work_zone",
                "title": "x",
                "route_spines": [{"segment_ids": [1]}],
                "officer": "y",
            },
        )
        assert r.status_code == 422


class TestCreateHygiene:
    _BASE = {"event_kind": "work_zone", "title": "Metro works",
             "route_spines": [{"segment_ids": [1001]}]}

    @pytest.mark.parametrize(
        "patch, fragment",
        [
            ({"title": "Metro\x00works"}, "title"),
            ({"description": "a\x00b"}, "description"),
            ({"route_spines": [{"label": "sp\x00ine", "segment_ids": [1001]}]}, "route_spines"),
            ({"metadata": {"fir": {"no": "44\x0001"}}}, "metadata.fir.no"),
            ({"officer": "Duty\x00Officer"}, "control characters"),
            ({"movement_id": "argus-smoke-1"}, "movement_id must be a uuid"),
        ],
    )
    def test_unstorable_text_is_422_and_creates_nothing(self, patch, fragment):
        pool = LifecyclePool()
        before = set(pool.db.events)
        r = _post(_app(pool), "/api/v1/events", {**self._BASE, **patch})
        assert r.status_code == 422, r.text
        assert fragment in json.dumps(r.json(), ensure_ascii=False)
        assert set(pool.db.events) == before and pool.db.log == []

    def test_movement_id_is_canonicalized_and_indic_names_are_legal(self):
        pool = LifecyclePool()
        mid = uuid.uuid4()
        r = _post(
            _app(pool),
            "/api/v1/events",
            {**self._BASE, "movement_id": str(mid).upper(), "officer": "ઇન્\u200dસ્પેક્ટર Patel"},
        )
        assert r.status_code == 200, r.text
        new_id = uuid.UUID(r.json()["event_id"])
        assert pool.db.events[new_id]["movement_id"] == str(mid)
        assert pool.db.log[-1]["actor"] == "ઇન્\u200dસ્પેક્ટર Patel"  # ZWJ kept


# ─── GET /events (history) ──────────────────────────────────────────────────


class TestHistory:
    def test_status_and_since_filters(self):
        pool = LifecyclePool()
        app = _app(pool)
        _post(app, STATUS, {"to_status": "cancelled", "officer": "Duty Officer A"})
        pool.db.events[EVENT]["updated_at"] = datetime.now(timezone.utc)
        body = _get(app, "/api/v1/events?status=cancelled,completed").json()
        assert [e["status"] for e in body["events"]] == ["cancelled"]
        assert body["events"][0]["actual_end"] is not None
        assert _get(app, "/api/v1/events?status=active").json()["events"] == []
        future = (
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        )
        assert _get(app, f"/api/v1/events?since={future}").json()["events"] == []
        assert _get(app, "/api/v1/events?status=exploded").status_code == 422
        assert _get(app, "/api/v1/events?limit=0").status_code == 422


# ─── migration 013 grammar ──────────────────────────────────────────────────


def _statements(text: str) -> list[str]:
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


class TestMigration013:
    def test_exists_and_matches_runner_pattern(self):
        assert M013.exists()
        assert re.fullmatch(r"\d{3}_[A-Za-z0-9_]+\.sql", M013.name)

    def test_naive_splitter_safe(self):
        text = M013.read_text()
        body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
        in_literal = False
        for ch in body:
            if ch == "'":
                in_literal = not in_literal
            elif ch == ";" and in_literal:
                pytest.fail("';' inside a string literal breaks naive splitters")
        assert "$$" not in text and "/*" not in text

    def test_idempotent_additive_schema_only(self):
        stmts = _statements(M013.read_text())
        assert len(stmts) >= 5
        for stmt in stmts:
            head = " ".join(stmt.split()).upper()
            assert head.startswith(
                ("CREATE TABLE IF NOT EXISTS", "CREATE INDEX IF NOT EXISTS", "ALTER TABLE")
            ), head[:60]
            if head.startswith("ALTER TABLE"):
                assert "ADD COLUMN IF NOT EXISTS" in head and "DROP" not in head
            for verb in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE "):
                assert not head.startswith(verb)

    def test_ledger_shape_in_lockstep_with_code(self):
        text = M013.read_text()
        assert "CREATE TABLE IF NOT EXISTS events.event_status_log" in text
        for status in EVENT_STATUSES:
            assert f"'{status}'" in text
        for column in ("seq", "event_id", "from_status", "to_status", "actor", "note", "at"):
            assert re.search(rf"^\s+{column}\s", text, re.M), column
        assert "ADD COLUMN IF NOT EXISTS actor TEXT" in text

    def test_signal_plan_ledger_in_lockstep_with_the_writer(self):
        from traffic_oracle.signals import writer

        text = M013.read_text()
        assert "CREATE TABLE IF NOT EXISTS corridor.signal_plan_log" in text
        src = inspect.getsource(writer.apply_city_plan)
        insert = re.search(r"INSERT INTO corridor\.signal_plan_log \(([^)]*)\)", src)
        assert insert is not None
        for column in [c.strip() for c in insert.group(1).split(",")] + ["plan_id", "applied_at"]:
            assert re.search(rf"^\s+{column}\s", text, re.M), column
        for band in ("GREEN", "AMBER", "RED", "BLACK"):
            assert f"'{band}'" in text


# ─── LIVE (scratch clone; one transaction, rolled back) ─────────────────────

LIVE_DSN = os.environ.get("VVIP_TEST_DATABASE_URL")
live = pytest.mark.skipif(not LIVE_DSN, reason="VVIP_TEST_DATABASE_URL (scratch clone) not set")


class _OneConnPool:
    def __init__(self, conn) -> None:
        self.conn = conn

    class _Acq:
        def __init__(self, conn) -> None:
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *exc):
            return False

    def acquire(self):
        return _OneConnPool._Acq(self.conn)

    async def fetch(self, *args):
        return await self.conn.fetch(*args)


@live
def test_live_lifecycle_with_dispatch_auto_close_rolled_back():
    asyncpg = pytest.importorskip("asyncpg")

    async def run():
        conn = await asyncpg.connect(LIVE_DSN)
        tx = conn.transaction()
        await tx.start()
        try:
            if not await conn.fetchval("SELECT to_regclass('events.event_status_log') IS NOT NULL"):
                pytest.skip("migration 013 not applied on the scratch DB")
            common.reset_relation_cache()
            app = create_app()
            app.state.pool = _OneConnPool(conn)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                seg = await conn.fetchval(
                    "SELECT segment_id FROM corridor.road_segments ORDER BY segment_id LIMIT 1"
                )
                jid = await conn.fetchval(
                    "SELECT junction_id FROM corridor.junctions ORDER BY junction_id LIMIT 1"
                )
                r = await c.post(
                    "/api/v1/events",
                    json={
                        "event_kind": "work_zone",
                        "title": "WP-02 lifecycle probe (rolled back)",
                        "route_spines": [{"segment_ids": [seg]}],
                        "officer": "Duty Officer Test",
                    },
                )
                assert r.status_code == 200, r.text
                eid = r.json()["event_id"]
                path = f"/api/v1/events/{eid}/status"
                assert (
                    await c.post(path, json={"to_status": "active", "officer": "Duty Officer Test"})
                ).status_code == 200
                r = await c.post(
                    f"/api/v1/events/{eid}/dispatch",
                    json={
                        "agency": "jan_rakshak",
                        "unit_id": "JR-PROBE",
                        "waypoint_junction": jid,
                        "requested_by": "Duty Officer Test",
                    },
                )
                assert r.status_code == 200, r.text
                did = r.json()["dispatch_id"]
                for step in (
                    ("decision", {"action": "approve", "officer": "Duty Officer Test"}),
                    ("status", {"status": "en_route", "officer": "JR lead"}),
                    ("status", {"status": "on_scene", "officer": "JR lead"}),
                ):
                    rr = await c.post(f"/api/v1/dispatches/{did}/{step[0]}", json=step[1])
                    assert rr.status_code == 200, rr.text
                units = (await c.get("/api/v1/units")).json()["units"]
                assert any(u["unit_id"] == "JR-PROBE" for u in units)
                r = await c.post(
                    path, json={"to_status": "completed", "officer": "Duty Officer Test"}
                )
                assert r.status_code == 200, r.text
                assert r.json()["closed_dispatches"][0]["to_status"] == "completed"
                r = await c.post(path, json={"to_status": "active", "officer": "Duty Officer Test"})
                assert r.status_code == 409 and r.json()["from_status"] == "completed"
                units = (await c.get("/api/v1/units")).json()["units"]
                assert not any(u["unit_id"] == "JR-PROBE" for u in units)
                audit = (await c.get(f"/api/v1/events/{eid}/audit")).json()["items"]
                texts = " | ".join(i["text"] for i in audit)
                assert "Event opened" in texts and "activated" in texts and "completed" in texts
                assert any(i["actor"] == EVENT_CLOSED_ACTOR for i in audit)
                rows = await conn.fetch(
                    "SELECT from_status, to_status, actor FROM events.event_status_log "
                    "WHERE event_id = $1 ORDER BY seq",
                    uuid.UUID(eid),
                )
                assert [(r["from_status"], r["to_status"]) for r in rows] == [
                    (None, "planning"),
                    ("planning", "active"),
                    ("active", "completed"),
                ]
                # the C6 pursuit chain against REAL recommendation rows
                pending = await conn.fetchrow(
                    "SELECT recommendation_id, vehicle_id FROM events.enforcement_recommendations "
                    "WHERE status = 'pending' LIMIT 1"
                )
                if pending is not None:
                    r = await c.post(
                        "/api/v1/events",
                        json=_pursuit(
                            {
                                "vehicle_id": str(pending["vehicle_id"]),
                                "recommendation_id": str(pending["recommendation_id"]),
                            }
                        ),
                    )
                    assert r.status_code == 422, r.text
        finally:
            await tx.rollback()
            await conn.close()

    asyncio.run(run())
