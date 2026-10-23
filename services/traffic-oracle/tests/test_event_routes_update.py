"""Corridor-event route updates — contract C-O (WP-19, migration 018).

    POST /api/v1/events/{event_id}/routes
         {variant 'reroute-N' | 'plan_b', segment_ids 1..64, make_primary,
          officer?, note?}

Through the REAL FastAPI app over a stateful fake store (transactions roll
back on error, like test_event_lifecycle.py):

    * one transaction: the event locked, 409 unless planning/approved/
      active, the current primary SUPERSEDED (never deleted), the new
      spine the 'primary' at rank 1 with its variant and author, one
      event_status_log row (from = to = the status) narrating it;
    * the response carries the current spines and the route_history;
    * 422 for a bad variant / segment list / unknown segment ids, 404 for
      an unknown event, 503 before migration 018;
    * the actor is header-first (x-veer-officer on a loopback connection),
      a differing body officer kept as the claim in the ledger note;
    * an event created without a 'primary' label has its de-facto primary
      (the lowest-ranked spine) superseded; make_primary=false adds an
      alternate and supersedes nothing;
    * EVERY reader of event_routes filters superseded spines behind the
      column probe (engine loop, city snapshot + dossier, e-challan
      closure test, unit staging, the events read shape), and the engine
      loop's next inputs run on the new primary;
    * the city feed narrates the ledger row as 'Route updated'.

An optional DB tier (VEER_TEST_PG_DSN = a SANDBOX store with 018; never the
live :56434 store) runs the endpoint end to end inside a transaction that
is rolled back.
"""

from __future__ import annotations

import asyncio
import copy
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from traffic_oracle import events as events_mod  # noqa: E402
from traffic_oracle.__main__ import create_app  # noqa: E402
from traffic_oracle.adaptive import engine_loop as loop_mod  # noqa: E402
from traffic_oracle.city import common  # noqa: E402
from traffic_oracle.city import dossier as dos  # noqa: E402
from traffic_oracle.city import entities as ent  # noqa: E402
from traffic_oracle.city.feed import event_log_item  # noqa: E402
from traffic_oracle.enforcement import engine as enf_engine  # noqa: E402
from traffic_oracle.units import router as units_router  # noqa: E402

EVENT = uuid.UUID("9a350da3-2212-4fac-9431-98a2fd1dff28")
SEGMENTS = {1005, 1012, 1013, 1014, 1015, 1016, 1020}


# ─── stateful fake store ─────────────────────────────────────────────────────


class RoutesDB:
    def __init__(self, *, status: str = "active", primary_label: str = "primary") -> None:
        now = datetime.now(timezone.utc)
        self.events = {
            EVENT: {"event_id": EVENT, "status": status, "event_kind": "protected_movement",
                    "title": "Z convoy · Relief Road", "updated_at": now - timedelta(hours=1)},
        }
        self.routes: list[dict] = [
            self._route(1, primary_label, [1005, 1012, 1015], now - timedelta(hours=1)),
            self._route(2, "alt-1", [1005, 1013], now - timedelta(hours=1)),
        ]
        self.log: list[dict] = []
        self.columns = {("events.event_routes", "superseded_at")}
        self.relations = {"events.event_status_log"}
        self.seq = 0
        self.fail_on: str | None = None

    @staticmethod
    def _route(rank: int, label: str, segs: list[int], at: datetime, **kw: Any) -> dict:
        return {"route_id": uuid.uuid4(), "event_id": EVENT, "route_rank": rank,
                "label": label, "segment_ids": list(segs), "variant": None,
                "created_by": None, "created_at": at, "superseded_at": None, **kw}

    def current(self) -> list[dict]:
        return sorted(
            (r for r in self.routes if r["superseded_at"] is None),
            key=lambda r: (r["route_rank"], r["created_at"]),
        )

    def snapshot(self) -> tuple:
        return copy.deepcopy((self.events, self.routes, self.log, self.seq))

    def restore(self, snap: tuple) -> None:
        self.events, self.routes, self.log, self.seq = snap


class RoutesConn:
    def __init__(self, db: RoutesDB) -> None:
        self.db = db

    class _Tx:
        def __init__(self, conn: RoutesConn) -> None:
            self.conn = conn

        async def __aenter__(self):
            self.snap = self.conn.db.snapshot()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if exc_type is not None:
                self.conn.db.restore(self.snap)
            return False

    def transaction(self):
        return RoutesConn._Tx(self)

    async def fetchval(self, sql: str, *args: Any):
        if sql == common._SQL_COLUMN:
            return (args[0], args[1]) in self.db.columns
        if sql == common._SQL_REGCLASS:
            return args[0] in self.db.relations
        raise AssertionError("unexpected fetchval: " + sql[:80])

    async def fetch(self, sql: str, *args: Any):
        db = self.db
        if sql == events_mod._SQL_KNOWN_SEGMENTS:
            return [{"segment_id": s} for s in args[0] if s in SEGMENTS]
        if sql == events_mod._SQL_CURRENT_ROUTES_FOR_UPDATE:
            return [dict(r) for r in db.current() if r["event_id"] == args[0]]
        if sql.startswith("UPDATE events.event_routes SET superseded_at = NOW()"):
            if db.fail_on == "supersede":
                raise RuntimeError("injected failure")
            out = []
            for r in db.routes:
                if r["route_id"] in args[0] and r["superseded_at"] is None:
                    r["superseded_at"] = datetime.now(timezone.utc)
                    out.append(dict(r))
            return out
        if sql == events_mod._SQL_EVENT_ROUTES_CURRENT:
            ids = {str(i) for i in args[0]}
            return [dict(r) for r in db.current() if str(r["event_id"]) in ids]
        if sql == events_mod._SQL_EVENT_ROUTE_HISTORY:
            ids = {str(i) for i in args[0]}
            return sorted(
                (dict(r) for r in db.routes
                 if str(r["event_id"]) in ids and r["superseded_at"] is not None),
                key=lambda r: (r["superseded_at"], r["route_rank"]),
            )
        if sql == events_mod._SQL_EVENT_ROUTES:
            ids = {str(i) for i in args[0]}
            return [dict(r) for r in sorted(db.routes, key=lambda r: r["route_rank"])
                    if str(r["event_id"]) in ids]
        raise AssertionError("unexpected fetch: " + sql[:80])

    async def fetchrow(self, sql: str, *args: Any):
        db = self.db
        if sql == events_mod._SQL_EVENT_FOR_UPDATE:
            ev = db.events.get(args[0])
            return dict(ev) if ev else None
        if sql.startswith("INSERT INTO events.event_routes"):
            eid, rank, label, segs, variant, created_by = args
            row = db._route(rank, label, segs, datetime.now(timezone.utc),
                            variant=variant, created_by=created_by)
            db.routes.append(row)
            return {"route_id": row["route_id"], "created_at": row["created_at"]}
        if sql.startswith("INSERT INTO events.event_status_log"):
            if db.fail_on == "log":
                raise RuntimeError("injected failure")
            db.seq += 1
            row = {"seq": db.seq, "event_id": args[0], "from_status": args[1],
                   "to_status": args[1], "actor": args[2], "note": args[3],
                   "at": datetime.now(timezone.utc)}
            db.log.append(row)
            return {"seq": row["seq"], "at": row["at"]}
        raise AssertionError("unexpected fetchrow: " + sql[:80])

    async def execute(self, sql: str, *args: Any):
        if sql.startswith("UPDATE events.corridor_events SET updated_at = NOW()"):
            self.db.events[args[0]]["updated_at"] = datetime.now(timezone.utc)
            return "UPDATE 1"
        raise AssertionError("unexpected execute: " + sql[:80])


class RoutesPool:
    def __init__(self, db: RoutesDB | None = None) -> None:
        self.db = db or RoutesDB()
        self.conn = RoutesConn(self.db)

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acq()


def _app(pool: RoutesPool):
    common.reset_relation_cache()
    app = create_app()
    app.state.pool = pool
    return app


async def _apost(app, body: dict, *, event: uuid.UUID | str = EVENT, base: str = "http://t",
                 headers: dict | None = None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=base) as c:
        return await c.post(f"/api/v1/events/{event}/routes", json=body, headers=headers)


def _post(app, body: dict, **kw: Any):
    return asyncio.run(_apost(app, body, **kw))


REROUTE = {"variant": "reroute-1", "segment_ids": [1005, 1013, 1014, 1016],
           "make_primary": True, "officer": "Duty Officer Test",
           "note": "Relief Road blocked at Kalupur"}


# ─── the endpoint ────────────────────────────────────────────────────────────


class TestRouteUpdate:
    def test_the_new_primary_supersedes_the_old_one_in_one_transaction(self):
        pool = RoutesPool()
        r = _post(_app(pool), REROUTE)
        assert r.status_code == 200, r.text
        body = r.json()
        db = pool.db
        old = next(x for x in db.routes if x["segment_ids"] == [1005, 1012, 1015])
        assert old["superseded_at"] is not None  # superseded, never deleted
        assert len(db.routes) == 3
        new = next(x for x in db.current() if x["variant"] == "reroute-1")
        assert (new["route_rank"], new["label"], new["created_by"]) == (
            1, "primary", "Duty Officer Test"
        )
        # the response: current spines + route_history
        assert [(s["label"], s["variant"], s["rank"]) for s in body["routes"]] == [
            ("primary", "reroute-1", 1), ("alt-1", None, 2)
        ]
        assert body["routes"][0]["segment_ids"] == [1005, 1013, 1014, 1016]
        (hist,) = body["route_history"]
        assert hist["segment_ids"] == [1005, 1012, 1015] and hist["superseded_at"]
        (sup,) = body["superseded"]
        assert sup["label"] == "primary" and sup["variant"] is None
        assert (body["actor"], body["actor_source"], body["claimed_officer"]) == (
            "Duty Officer Test", "body", None
        )
        assert body["status"] == "active" and body["seq"] == 1
        # ONE ledger row narrating the change (from = to = the status)
        (log,) = db.log
        assert (log["from_status"], log["to_status"], log["actor"]) == (
            "active", "active", "Duty Officer Test"
        )
        assert log["note"] == (
            "route update: reroute-1 is now the primary spine (4 segments, 1005→1016); "
            "supersedes primary — Relief Road blocked at Kalupur"
        )

    def test_a_second_reroute_supersedes_the_first(self):
        pool = RoutesPool()
        app = _app(pool)
        assert _post(app, REROUTE).status_code == 200
        r = _post(app, {**REROUTE, "variant": "reroute-2", "segment_ids": [1005, 1020]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["routes"][0]["variant"] == "reroute-2"
        assert [h["variant"] for h in body["route_history"]] == [None, "reroute-1"]
        assert [x["variant"] for x in pool.db.current()] == ["reroute-2", None]

    def test_an_event_without_a_primary_label_supersedes_its_lowest_ranked_spine(self):
        pool = RoutesPool(RoutesDB(primary_label="spine"))
        r = _post(_app(pool), REROUTE)
        assert r.status_code == 200, r.text
        assert [s["label"] for s in r.json()["superseded"]] == ["spine"]
        assert [(x["label"], x["route_rank"]) for x in pool.db.current()] == [
            ("primary", 1), ("alt-1", 2)
        ]

    def test_make_primary_false_adds_an_alternate_and_supersedes_nothing(self):
        pool = RoutesPool()
        r = _post(_app(pool), {**REROUTE, "variant": "plan_b", "make_primary": False})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["superseded"] == [] and body["route_history"] == []
        assert [(s["label"], s["variant"], s["rank"]) for s in body["routes"]] == [
            ("primary", None, 1), ("alt-1", None, 2), ("plan_b", "plan_b", 3)
        ]
        assert "added as an alternate spine" in pool.db.log[0]["note"]

    @pytest.mark.parametrize("status", ["completed", "cancelled"])
    def test_a_closed_event_is_409_and_nothing_changes(self, status):
        pool = RoutesPool(RoutesDB(status=status))
        before = copy.deepcopy(pool.db.routes)
        r = _post(_app(pool), REROUTE)
        assert r.status_code == 409, r.text
        assert r.json()["status"] == status
        assert "only a planning, approved or active event" in r.json()["detail"]
        assert pool.db.routes == before and pool.db.log == []

    @pytest.mark.parametrize("status", ["planning", "approved", "active"])
    def test_every_live_status_can_be_rerouted(self, status):
        pool = RoutesPool(RoutesDB(status=status))
        assert _post(_app(pool), REROUTE).status_code == 200

    def test_unknown_segments_are_422_and_nothing_changes(self):
        pool = RoutesPool()
        r = _post(_app(pool), {**REROUTE, "segment_ids": [1005, 999999, 888888]})
        assert r.status_code == 422, r.text
        assert "[999999, 888888]" in r.json()["detail"]
        assert len(pool.db.routes) == 2 and pool.db.log == []

    @pytest.mark.parametrize(
        "patch",
        [
            {"variant": "detour"},
            {"variant": "reroute-0"},
            {"variant": "reroute-1000"},
            {"variant": "plan_c"},
            {"segment_ids": []},
            {"segment_ids": list(range(1001, 1066))},  # 65 > 64
            {"segment_ids": [0, 1005]},
            {"segment_ids": [-1005]},
            {"officer": "Duty\x00Officer"},
            {"officer": "Duty ‮Officer"},
            {"note": "a\x00b"},
            {"note": "x" * 501},
        ],
    )
    def test_invalid_bodies_are_422(self, patch):
        pool = RoutesPool()
        r = _post(_app(pool), {**REROUTE, **patch})
        assert r.status_code == 422, r.text
        assert len(pool.db.routes) == 2

    def test_no_officer_anywhere_is_422(self):
        body = {k: v for k, v in REROUTE.items() if k != "officer"}
        assert _post(_app(RoutesPool()), body).status_code == 422

    def test_unknown_event_is_404_and_a_malformed_id_422(self):
        assert _post(_app(RoutesPool()), REROUTE, event=uuid.uuid4()).status_code == 404
        assert _post(_app(RoutesPool()), REROUTE, event="not-a-uuid").status_code == 422

    def test_before_migration_018_it_is_a_labelled_503(self):
        db = RoutesDB()
        db.columns.clear()
        r = _post(_app(RoutesPool(db)), REROUTE)
        assert r.status_code == 503
        assert "apply migration 018" in r.json()["detail"]
        assert len(db.routes) == 2

    def test_a_failure_mid_transaction_rolls_everything_back(self):
        db = RoutesDB()
        db.fail_on = "log"
        before = copy.deepcopy(db.routes)
        with pytest.raises(RuntimeError):
            _post(_app(RoutesPool(db)), REROUTE)
        assert db.routes == before and db.log == []

    def test_the_verified_header_is_the_actor_and_a_differing_body_a_claim(self):
        pool = RoutesPool()
        r = _post(_app(pool), {**REROUTE, "officer": "B"}, base="http://127.0.0.1",
                  headers={"x-veer-officer": "Duty%20Officer%20A"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["actor"], body["actor_source"], body["claimed_officer"]) == (
            "Duty Officer A", "x-veer-officer", "B"
        )
        new = pool.db.current()[0]
        assert new["created_by"] == "Duty Officer A"
        assert pool.db.log[0]["actor"] == "Duty Officer A"
        assert pool.db.log[0]["note"].endswith(
            "[x-veer-officer: Duty Officer A; claimed officer: B]"
        )


# ─── every reader skips superseded spines ────────────────────────────────────


#: (pre-018 statement, 018 statement) per reader of events.event_routes.
READERS = {
    "engine_loop": (loop_mod._EVENT_SPINES_SQL, loop_mod._EVENT_SPINES_CURRENT_SQL),
    "city entities": (ent._SQL_EVENT_SPINES, ent._SQL_EVENT_SPINES_CURRENT),
    "events read shape": (events_mod._SQL_EVENT_ROUTES, events_mod._SQL_EVENT_ROUTES_CURRENT),
    "dossier spine_of": (dos._SQL_SEGMENT_EVENTS, dos._SQL_SEGMENT_EVENTS_CURRENT),
    "e-challan closures": (
        enf_engine._SQL_COVERING_CLOSURES,
        enf_engine._SQL_COVERING_CLOSURES_CURRENT,
    ),
    "unit staging": (
        units_router._SQL_EVENT_SPINE_START,
        units_router._SQL_EVENT_SPINE_START_CURRENT,
    ),
}


class TestReaders:
    @pytest.mark.parametrize("name", sorted(READERS))
    def test_the_018_statement_filters_superseded_spines(self, name):
        pre, current = READERS[name]
        assert "superseded_at" not in pre  # still runs on a pre-018 store
        assert "r.superseded_at IS NULL" in current
        # the 018 statement is the pre-018 one plus the filter (same shape)
        assert current.startswith(pre.split(" WHERE ")[0].split(" ORDER BY ")[0][:40])

    def test_the_readers_switch_on_the_column_probe(self):
        assert common.ROUTES_TABLE == "events.event_routes"
        assert common.ROUTES_SUPERSEDE_COLUMN == "superseded_at"

    async def test_the_engine_loop_reads_the_new_primary_on_its_next_tick(self):
        db = RoutesDB()
        pool = RoutesPool(db)
        app = _app(pool)
        assert (await _apost(app, REROUTE)).status_code == 200

        class LoopConn:
            async def fetch(self, sql, *args):
                if "FROM events.corridor_events" in sql:
                    return [{"event_id": EVENT, "protection_class": "Z", "crowd_class": "LOW",
                             "status": "active", "event_kind": "protected_movement",
                             "planned_start": None, "movement_id": None, "metadata": {}}]
                if sql == loop_mod._EVENT_SPINES_CURRENT_SQL:
                    return [{"event_id": r["event_id"], "label": r["label"],
                             "segment_ids": r["segment_ids"]} for r in db.current()]
                if sql == loop_mod._EVENT_SPINES_SQL:
                    raise AssertionError("the pre-018 statement on a 018 store")
                if "FROM convoy.movements" in sql:
                    return []
                raise AssertionError(sql[:60])

            async def fetchval(self, sql, *args):
                assert sql == common._SQL_COLUMN
                return True

        class LoopPool:
            def acquire(self):
                conn = LoopConn()

                class _A:
                    async def __aenter__(self):
                        return conn

                    async def __aexit__(self, *e):
                        return False

                return _A()

        common.reset_relation_cache()
        (subject,) = await loop_mod._load_active_subjects(LoopPool())
        assert subject.spine_segments() == ("primary", [1005, 1013, 1014, 1016])

    async def test_the_events_read_shape_carries_variant_and_route_history(self):
        db = RoutesDB()
        app = _app(RoutesPool(db))
        assert (await _apost(app, REROUTE)).status_code == 200
        common.reset_relation_cache()
        current, history = await events_mod.load_spines(RoutesConn(db), [EVENT])
        (cur,) = [c for c in current[str(EVENT)] if c["label"] == "primary"]
        assert (cur["variant"], cur["rank"], cur["created_by"]) == (
            "reroute-1", 1, "Duty Officer Test"
        )
        (hist,) = history[str(EVENT)]
        assert hist["segment_ids"] == [1005, 1012, 1015] and hist["superseded_at"]

    async def test_a_pre_018_store_reads_every_spine_and_no_history(self):
        db = RoutesDB()
        db.columns.clear()
        common.reset_relation_cache()
        current, history = await events_mod.load_spines(RoutesConn(db), [EVENT])
        assert [c["label"] for c in current[str(EVENT)]] == ["primary", "alt-1"]
        assert "variant" not in current[str(EVENT)][0] and history == {}


class TestFeedNarration:
    def test_a_route_update_row_reads_route_updated(self):
        item = event_log_item({
            "seq": 7, "event_id": EVENT, "from_status": "active", "to_status": "active",
            "actor": "Duty Officer A", "at": datetime.now(timezone.utc),
            "note": "route update: reroute-1 is now the primary spine (4 segments, 1005→1016)",
            "event_kind": "protected_movement", "title": "Z convoy", "metadata": {},
        })
        assert item["text"].startswith("Route updated: Z convoy by Duty Officer A — route update:")
        assert item["props"]["action"] == "route_update" and item["severity"] == "warn"

    def test_a_lifecycle_row_is_unchanged(self):
        item = event_log_item({
            "seq": 8, "event_id": EVENT, "from_status": "planning", "to_status": "active",
            "actor": "Duty Officer A", "at": datetime.now(timezone.utc), "note": None,
            "event_kind": "protected_movement", "title": "Z convoy", "metadata": {},
        })
        assert item["text"] == "Event activated: Z convoy (planning → active) by Duty Officer A"
        assert item["props"]["action"] is None


# ─── DB tier (sandbox store with 018; everything rolled back) ───────────────

_DSN = os.environ.get("VEER_TEST_PG_DSN", "")
_LIVE_PORT = ":56434"
db_tier = pytest.mark.skipif(
    not _DSN or _LIVE_PORT in _DSN,
    reason="set VEER_TEST_PG_DSN to a SANDBOX store (001-018 + seed); never the live :56434 store",
)


class _OneConnPool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *e):
                return False

        return _A()


@db_tier
def test_db_route_update_end_to_end_rolled_back():
    import asyncpg

    async def run():
        conn = await asyncpg.connect(_DSN)
        tx = conn.transaction()
        await tx.start()
        try:
            common.reset_relation_cache()
            if not await common.routes_supersede_supported(conn):
                pytest.skip("migration 018 not applied on this store")
            eid = await conn.fetchval(
                "INSERT INTO events.corridor_events (event_kind, protection_class, crowd_class, "
                "title, status) VALUES ('protected_movement', 'Z', 'LOW', 'WP-19 DB test', "
                "'active') RETURNING event_id"
            )
            await conn.execute(
                "INSERT INTO events.event_routes (event_id, route_rank, label, segment_ids) "
                "VALUES ($1, 1, 'primary', $2), ($1, 2, 'alt-1', $3)",
                eid, [1005, 1012, 1015], [1005, 1013],
            )
            app = create_app()
            app.state.pool = _OneConnPool(conn)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                r = await c.post(f"/api/v1/events/{eid}/routes", json=REROUTE)
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["routes"][0]["variant"] == "reroute-1"
                assert body["route_history"][0]["segment_ids"] == [1005, 1012, 1015]
                active = (await c.get("/api/v1/events/active")).json()["events"]
                mine = next(e for e in active if e["event_id"] == str(eid))
                assert mine["routes"][0]["segment_ids"] == [1005, 1013, 1014, 1016]
                assert len(mine["route_history"]) == 1
                bad = await c.post(f"/api/v1/events/{eid}/routes",
                                   json={**REROUTE, "segment_ids": [999999]})
                assert bad.status_code == 422
            rows = await conn.fetch(
                "SELECT label, variant, superseded_at IS NOT NULL AS gone FROM "
                "events.event_routes WHERE event_id = $1", eid
            )
            # (one test transaction: every created_at is the same NOW())
            assert sorted((r["label"], r["variant"] or "", r["gone"]) for r in rows) == [
                ("alt-1", "", False), ("primary", "", True), ("primary", "reroute-1", False)
            ]
            spines = await conn.fetch(loop_mod._EVENT_SPINES_CURRENT_SQL, [eid])
            assert [list(s["segment_ids"]) for s in spines][0] == [1005, 1013, 1014, 1016]
        finally:
            await tx.rollback()
            await conn.close()

    asyncio.run(run())
