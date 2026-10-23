"""Signal-writer attribution tests — named officer on apply/restore (WP-02).

Covers (signals/router.py + signals/writer.py, migration 013):
    * PlanApplyRequest / RestoreRequest: ``officer`` optional (2..64
      chars, trimmed) and REQUIRED only when VVIP_REQUIRE_OFFICER=1, so
      older decks keep working until they send it;
    * ``event_id`` must be a corridor-event uuid — a preview-fixture slug
      is a 422 instead of a 500 from the ``$1::uuid`` cast;
    * the officer reaches the ledger: snapshot rows of an apply and every
      restore row bind it as ``signal_program_history.actor``, the report
      echoes it, GET /signals/history returns it;
    * EVERY committed apply appends one attributed ``signal_plan_log`` row
      in its own transaction — including a re-apply over junctions that
      were already snapshotted (the escalation that used to leave no
      attributed row); fail-closed outcomes write none;
    * officers with control / bidi characters are 422 (a NUL was a 500);
    * a DB without migration 013 (no actor column, no plan ledger) is a
      clear 503 — the write transaction rolled back, nothing half-written;
    * LIVE (VVIP_TEST_DATABASE_URL = a SCRATCH clone; one transaction,
      rolled back): a real apply (shadow gate injected as passing) and a
      real restore write the officer into the history rows.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any

import pytest

pytest.importorskip("httpx")

from asyncpg.exceptions import UndefinedColumnError, UndefinedTableError  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from traffic_oracle.__main__ import create_app  # noqa: E402
from traffic_oracle.signals import router as signals_router  # noqa: E402
from traffic_oracle.signals import writer as W  # noqa: E402
from traffic_oracle.signals.router import (  # noqa: E402
    REQUIRE_OFFICER_ENV,
    PlanApplyRequest,
    RestoreRequest,
    officer_required,
)

EVENT = "4508d86d-566b-438f-8414-b3d67fd7e6e4"


# ─── fake pool (records every write with its bound args) ────────────────────


class WriterFakePool:
    def __init__(
        self,
        *,
        snapshot_junctions: set[int] | None = None,
        fail_writes: bool = False,
        fail_plan_log: bool = False,
    ) -> None:
        self.programs = [
            {
                "junction_id": 1,
                "cycle_sec": 110,
                "offset_sec": 17,
                "source": "osm_assumed",
                "phases": json.dumps(
                    [
                        {"green_sec": 50, "movements": ["through"]},
                        {"green_sec": 42, "movements": ["cross"]},
                    ]
                ),
            },
            {
                "junction_id": 2,
                "cycle_sec": 100,
                "offset_sec": 0,
                "source": "osm_assumed",
                "phases": json.dumps(
                    [
                        {"green_sec": 47, "movements": ["through"]},
                        {"green_sec": 38, "movements": ["cross"]},
                    ]
                ),
            },
        ]
        self.snapshot_junctions = snapshot_junctions or set()
        self.fail_writes = fail_writes
        self.fail_plan_log = fail_plan_log
        self.writes: list[tuple[str, tuple]] = []

    def _rows(self, sql: str, args: tuple) -> list[dict]:
        if sql == W._SQL_LOAD_PROGRAMS:
            return self.programs
        if sql == W._SQL_LOAD_ADJACENCY:
            return [{"segment_id": 101, "junction_id": 1}, {"segment_id": 102, "junction_id": 2}]
        if sql == W._SQL_HISTORY_JUNCTIONS:
            return [{"junction_id": j} for j in self.snapshot_junctions]
        if sql in (W._SQL_RESTORE_TARGETS_ALL, W._SQL_RESTORE_TARGETS_EVENT):
            return [
                {
                    "junction_id": p["junction_id"],
                    "cycle_sec": p["cycle_sec"],
                    "phases": p["phases"],
                    "offset_sec": p["offset_sec"],
                    "source": "osm_assumed",
                    "event_id": None,
                }
                for p in self.programs
                if p["junction_id"] in self.snapshot_junctions
            ]
        if sql == W._SQL_HISTORY_LIST:
            return [
                {
                    "history_id": 9,
                    "junction_id": 1,
                    "event_id": None,
                    "kind": "restore",
                    "cycle_sec": 110,
                    "phases": self.programs[0]["phases"],
                    "offset_sec": 17,
                    "source": "osm_assumed",
                    "reason": "restore to pre-calibration snapshot",
                    "actor": "Duty Officer A",
                    "captured_at": datetime.now(timezone.utc),
                }
            ]
        raise AssertionError("unexpected SQL: " + sql[:80])

    async def fetch(self, sql: str, *args: Any):
        return self._rows(sql, args)

    async def execute(self, sql: str, *args: Any):
        if self.fail_writes and "signal_program_history" in sql:
            raise UndefinedColumnError(
                'column "actor" of relation "signal_program_history" does not exist'
            )
        if self.fail_plan_log and "signal_plan_log" in sql:
            raise UndefinedTableError('relation "corridor.signal_plan_log" does not exist')
        self.writes.append((sql, args))
        return "INSERT 0 1"

    class _Tx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    def transaction(self):
        return WriterFakePool._Tx()

    class _Acq:
        def __init__(self, pool: "WriterFakePool") -> None:
            self.pool = pool

        async def __aenter__(self):
            return self.pool

        async def __aexit__(self, *exc):
            return False

    def acquire(self):
        return WriterFakePool._Acq(self)

    def history_writes(self, kind: str) -> list[tuple]:
        return [
            args
            for sql, args in self.writes
            if "signal_program_history" in sql and f"'{kind}'" in sql
        ]

    def plan_writes(self) -> list[tuple]:
        return [args for sql, args in self.writes if "signal_plan_log" in sql]


def _passing_shadow(*_a: Any, **_k: Any):
    return True, W.ShadowReport("passed", "ok (test)", 10.0, 9.8)


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


@pytest.fixture
def writer_on(monkeypatch):
    monkeypatch.setenv(W.WRITER_FLAG_ENV, "1")
    monkeypatch.setattr(W, "shadow_evaluate_plan", _passing_shadow)
    monkeypatch.delenv(REQUIRE_OFFICER_ENV, raising=False)


def _app(pool: WriterFakePool):
    app = create_app()
    app.state.pool = pool
    return app


# ─── request models ─────────────────────────────────────────────────────────


class TestRequestModels:
    def test_event_id_is_a_uuid_or_422(self):
        assert PlanApplyRequest(event_id=EVENT.upper()).event_id == EVENT
        assert RestoreRequest(event_id=EVENT).event_id == EVENT
        for model in (PlanApplyRequest, RestoreRequest):
            with pytest.raises(ValueError):
                model(event_id="rath_yatra_ahmedabad")

    def test_officer_optional_trimmed_and_bounded(self):
        assert PlanApplyRequest().officer is None
        assert PlanApplyRequest(officer="  Duty   Officer A ").officer == "Duty Officer A"
        for bad in ("x", " ", "y" * 65, "Duty\x00Officer", "Duty \u202eOfficer"):
            with pytest.raises(ValueError):
                RestoreRequest(all=True, officer=bad)
            with pytest.raises(ValueError):
                PlanApplyRequest(officer=bad)

    def test_require_officer_flag(self, monkeypatch):
        monkeypatch.delenv(REQUIRE_OFFICER_ENV, raising=False)
        assert officer_required() is False
        for value, expected in (
            ("1", True),
            ("true", True),
            ("0", False),
            ("", False),
            ("yes", True),
        ):
            monkeypatch.setenv(REQUIRE_OFFICER_ENV, value)
            assert officer_required() is expected


# ─── routes ─────────────────────────────────────────────────────────────────


class TestApplyRestoreRoutes:
    def test_apply_binds_the_officer_on_snapshot_rows(self, writer_on):
        pool = WriterFakePool()
        r = _post(
            _app(pool),
            "/api/v1/signals/plans/apply",
            {"band": "AMBER", "event_id": EVENT, "officer": "Duty Officer A"},
        )
        assert r.status_code == 200, r.text
        report = r.json()
        assert report["applied"] is True and report["actor"] == "Duty Officer A"
        snaps = pool.history_writes("snapshot")
        assert len(snaps) == 2
        assert all(args[-1] == "Duty Officer A" for args in snaps)
        assert all(args[1] == EVENT for args in snaps)

    def test_every_apply_appends_one_attributed_plan_row(self, writer_on):
        pool = WriterFakePool()
        app = _app(pool)
        r = _post(app, "/api/v1/signals/plans/apply",
                  {"band": "AMBER", "event_id": EVENT, "officer": "Duty Officer A"})
        assert r.status_code == 200, r.text
        plans = pool.plan_writes()
        assert len(plans) == 1
        event_id, band, junctions, snapshots, shadow, actor = plans[0]
        assert (event_id, band, actor, shadow) == (EVENT, "AMBER", "Duty Officer A", "passed")
        assert sorted(junctions) == [1, 2] and snapshots == 2
        # the escalation: every planned junction already snapshotted, so the
        # C7 history ledger takes NO row — the plan ledger still attributes it
        pool.snapshot_junctions = {1, 2}
        pool.writes.clear()
        r = _post(app, "/api/v1/signals/plans/apply", {"band": "RED", "officer": "Duty Officer B"})
        assert r.status_code == 200, r.text
        assert pool.history_writes("snapshot") == []
        plans = pool.plan_writes()
        assert len(plans) == 1 and plans[0][1] == "RED" and plans[0][-1] == "Duty Officer B"
        assert plans[0][3] == 0  # no snapshots this time

    def test_fail_closed_apply_writes_no_plan_row(self, monkeypatch):
        monkeypatch.setenv(W.WRITER_FLAG_ENV, "1")
        monkeypatch.setattr(
            W, "shadow_evaluate_plan",
            lambda *a, **k: (False, W.ShadowReport("unavailable", "twin not calibrated")),
        )
        pool = WriterFakePool()
        r = _post(_app(pool), "/api/v1/signals/plans/apply",
                  {"band": "AMBER", "officer": "Duty Officer A"})
        assert r.status_code == 409
        assert pool.writes == []

    def test_apply_without_officer_still_works_when_not_required(self, writer_on):
        pool = WriterFakePool()
        r = _post(_app(pool), "/api/v1/signals/plans/apply", {"band": "AMBER"})
        assert r.status_code == 200, r.text
        assert all(args[-1] is None for args in pool.history_writes("snapshot"))

    def test_officer_required_flag_is_enforced_before_any_write(self, writer_on, monkeypatch):
        monkeypatch.setenv(REQUIRE_OFFICER_ENV, "1")
        pool = WriterFakePool(snapshot_junctions={1, 2})
        app = _app(pool)
        r = _post(app, "/api/v1/signals/plans/apply", {"band": "AMBER"})
        assert r.status_code == 422 and REQUIRE_OFFICER_ENV in r.json()["detail"]
        r = _post(app, "/api/v1/signals/restore", {"all": True})
        assert r.status_code == 422
        assert pool.writes == []
        r = _post(app, "/api/v1/signals/restore", {"all": True, "officer": "Duty Officer A"})
        assert r.status_code == 200, r.text

    def test_restore_binds_the_officer_on_restore_rows(self, writer_on):
        pool = WriterFakePool(snapshot_junctions={1})
        r = _post(
            _app(pool), "/api/v1/signals/restore", {"event_id": EVENT, "officer": "Duty Officer A"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["actor"] == "Duty Officer A" and r.json()["restored"] == 1
        marks = pool.history_writes("restore")
        assert len(marks) == 1 and marks[0][-1] == "Duty Officer A"

    def test_slug_event_id_is_422_not_500(self, writer_on):
        pool = WriterFakePool()
        app = _app(pool)
        for path, body in (
            ("/api/v1/signals/plans/apply", {"band": "AMBER", "event_id": "rath_yatra_ahmedabad"}),
            ("/api/v1/signals/restore", {"event_id": "rath_yatra_ahmedabad"}),
        ):
            r = _post(app, path, body)
            assert r.status_code == 422, (path, r.text)
        assert pool.writes == []

    def test_missing_013_column_is_a_clear_503(self, writer_on):
        pool = WriterFakePool(fail_writes=True)
        r = _post(
            _app(pool),
            "/api/v1/signals/plans/apply",
            {"band": "AMBER", "officer": "Duty Officer A"},
        )
        assert r.status_code == 503 and "013" in r.json()["detail"]
        pool = WriterFakePool(snapshot_junctions={1}, fail_writes=True)
        r = _post(_app(pool), "/api/v1/signals/restore", {"all": True, "officer": "Duty Officer A"})
        assert r.status_code == 503
        # a re-apply touches no history row — the missing PLAN ledger alone
        # must still refuse (nothing committed without its attributed row)
        pool = WriterFakePool(snapshot_junctions={1, 2}, fail_plan_log=True)
        r = _post(_app(pool), "/api/v1/signals/plans/apply",
                  {"band": "RED", "officer": "Duty Officer A"})
        assert r.status_code == 503 and "013" in r.json()["detail"]

    def test_writer_disabled_report_carries_the_actor(self, monkeypatch):
        monkeypatch.delenv(W.WRITER_FLAG_ENV, raising=False)
        pool = WriterFakePool()
        r = _post(
            _app(pool),
            "/api/v1/signals/plans/apply",
            {"band": "AMBER", "officer": "Duty Officer A"},
        )
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "writer_disabled"
        assert r.json()["detail"]["actor"] == "Duty Officer A"
        assert pool.writes == []

    def test_history_returns_the_actor(self):
        body = _get(_app(WriterFakePool()), "/api/v1/signals/history?limit=5").json()
        assert body["history"][0]["actor"] == "Duty Officer A"

    def test_router_documents_the_env_flag(self):
        assert signals_router.REQUIRE_OFFICER_ENV == "VVIP_REQUIRE_OFFICER"


# ─── LIVE (scratch clone; one transaction, rolled back) ─────────────────────

LIVE_DSN = os.environ.get("VVIP_TEST_DATABASE_URL")


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


@pytest.mark.skipif(not LIVE_DSN, reason="VVIP_TEST_DATABASE_URL (scratch clone) not set")
def test_live_apply_and_restore_record_the_officer_rolled_back():
    asyncpg = pytest.importorskip("asyncpg")

    async def run():
        conn = await asyncpg.connect(LIVE_DSN)
        tx = conn.transaction()
        await tx.start()
        try:
            has_actor = await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'corridor' "
                "AND table_name = 'signal_program_history' AND column_name = 'actor'"
            )
            if not has_actor:
                pytest.skip("migration 013 not applied on the scratch DB")
            pool = _OneConnPool(conn)
            report = await W.apply_city_plan(
                pool,
                band="RED",
                writer_flag_value=True,
                shadow_fn=_passing_shadow,
                actor="Duty Officer Live",
            )
            assert report["applied"] is True, report
            rows = await conn.fetch(
                "SELECT kind, actor FROM corridor.signal_program_history "
                "WHERE actor = 'Duty Officer Live'"
            )
            snapshots = [r for r in rows if r["kind"] == "snapshot"]
            assert len(snapshots) == report["snapshots_written"] >= 1
            if await conn.fetchval("SELECT to_regclass('corridor.signal_plan_log') IS NOT NULL"):
                plan = await conn.fetchrow(
                    "SELECT band, actor, snapshots_written, shadow_eval, cardinality(junction_ids) "
                    "AS n FROM corridor.signal_plan_log WHERE actor = 'Duty Officer Live'"
                )
                assert plan is not None and plan["band"] == "RED"
                assert plan["snapshots_written"] == report["snapshots_written"]
                assert plan["n"] == len(report["planned_junctions"])
                # same transaction clock as its snapshot rows (feed dedupe key)
                assert await conn.fetchval(
                    "SELECT count(*) FROM corridor.signal_program_history h JOIN "
                    "corridor.signal_plan_log p ON p.applied_at = h.captured_at "
                    "WHERE p.actor = 'Duty Officer Live' AND h.kind = 'snapshot'"
                ) == report["snapshots_written"]
            restored = await W.restore_programs(pool, restore_all=True, actor="Duty Officer Live")
            assert restored["restored"] >= 1
            rows = await conn.fetch(
                "SELECT kind FROM corridor.signal_program_history "
                "WHERE actor = 'Duty Officer Live' AND kind = 'restore'"
            )
            assert len(rows) == restored["restored"]
            history = await W.list_history(pool, 5)
            assert history[0]["actor"] == "Duty Officer Live"
        finally:
            await tx.rollback()
            await conn.close()

    asyncio.run(run())
