"""Pure-python tests — city signal plans, gate, and the gated writer.

Covers (signalling-janrakshak stream):
    * signals/plans.py  — the deterministic band→plan table, split
      transfer with cross floors, cycle/offset invariance, green-ratio
      and active-phase math, city-band aggregation.
    * signals/gate.py   — shared/exclusive exclusion semantics.
    * signals/writer.py — flag default-OFF, fail-closed shadow gate,
      snapshot-once + calibrated upsert + restore with a fake pool.

No database, no network — the pool is a fake; the shadow function is
injectable.  HTTP behaviour is exercised through the app in
test_units_dispatch.py (same app factory).
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

import pytest

from traffic_oracle.signals import plans as P
from traffic_oracle.signals import writer as W
from traffic_oracle.signals.gate import SignalPolicyGate


# ─── Fake asyncpg pool ───────────────────────────────────────────────────────


class FakeConn:
    def __init__(self, store: "FakePool") -> None:
        self.store = store

    async def fetch(self, sql, *args):
        return self.store._rows(sql, args)

    async def fetchrow(self, sql, *args):
        rows = self.store.fetch(sql, args)
        return rows[0] if rows else None

    async def execute(self, sql, *args):
        self.store.executed.append((sql, args))
        return "OK"

    class _Tx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    def transaction(self):
        return FakeConn._Tx()


class FakeAcquire:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    """Minimal asyncpg.Pool double backed by canned rows + a call log."""

    def __init__(self, programs: list[P.JunctionProgram]) -> None:
        self.programs = {p.junction_id: p for p in programs}
        self.history_junctions: set[int] = set()
        self.executed: list[tuple[str, tuple]] = []
        self.adjacency: list[dict] = [
            {"segment_id": 101, "junction_id": 1},
            {"segment_id": 102, "junction_id": 2},
        ]

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        if "FROM corridor.signal_programs sp ORDER BY" in sql:
            return [
                {
                    "junction_id": p.junction_id,
                    "cycle_sec": p.cycle_sec,
                    "phases": json.dumps(p.phases),
                    "offset_sec": p.offset_sec,
                    "source": p.source,
                }
                for p in self.programs.values()
            ]
        if "FROM corridor.segment_adjacency" in sql:
            return self.adjacency
        if "FROM corridor.signal_program_history WHERE junction_id" in sql:
            return [{"junction_id": j} for j in self.history_junctions]
        if "kind = 'snapshot'" in sql:
            return [
                {
                    "junction_id": j,
                    "cycle_sec": p.cycle_sec,
                    "phases": json.dumps(p.phases),
                    "offset_sec": p.offset_sec,
                    "source": "osm_assumed",
                    "event_id": None,
                }
                for j, p in self.programs.items()
                if j in self.history_junctions
            ]
        if "ORDER BY h.captured_at DESC LIMIT" in sql:
            return []
        raise AssertionError("unexpected SQL in test: " + sql[:80])

    async def fetch(self, sql, *args):
        return self._rows(sql, args)

    async def fetchrow(self, sql, *args):
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def acquire(self):
        return FakeAcquire(FakeConn(self))


def seed_program(junction_id: int = 1, cycle: int = 110) -> P.JunctionProgram:
    """The 006-style two-phase primary-road program."""
    return P.JunctionProgram(
        junction_id=junction_id,
        cycle_sec=cycle,
        phases=[
            {"green_sec": 50, "movements": ["through"]},
            {"green_sec": 42, "movements": ["cross"]},
        ],
        offset_sec=17,
        source="osm_assumed",
    )


# ─── plans: the band→plan table ──────────────────────────────────────────────


def test_band_plan_table_is_the_documented_one():
    assert P.BAND_PLAN_TABLE["GREEN"].writes is False
    assert P.BAND_PLAN_TABLE["AMBER"].priority_share == 0.06
    assert P.BAND_PLAN_TABLE["AMBER"].cross_green_floor_sec == 18
    assert P.BAND_PLAN_TABLE["RED"].priority_share == 0.12
    assert P.BAND_PLAN_TABLE["RED"].cross_green_floor_sec == 14
    assert P.BAND_PLAN_TABLE["BLACK"].priority_share == 0.18
    assert P.BAND_PLAN_TABLE["BLACK"].cross_green_floor_sec == 10


def test_green_band_plans_nothing():
    assert P.plan_for_junction(seed_program(), "GREEN") is None


def test_amber_plan_moves_split_deterministically():
    planned = P.plan_for_junction(seed_program(), "AMBER")
    assert planned is not None
    # transfer = round(110 * 0.06) = 7 s from cross to through
    assert planned.phases[0]["green_sec"] == 57
    assert planned.phases[1]["green_sec"] == 35
    assert planned.source == "calibrated"
    # Determinism: same inputs → identical plan
    again = P.plan_for_junction(seed_program(), "AMBER")
    assert again == planned


def test_black_plan_respects_cross_floor():
    planned = P.plan_for_junction(seed_program(), "BLACK")
    assert planned is not None
    # transfer = round(110 * 0.18) = 20, but cross can only give
    # 42 - 10 = 32 → full 20 moves; cross keeps the 10 s floor.
    assert planned.phases[1]["green_sec"] == 22
    assert planned.phases[0]["green_sec"] == 70
    assert planned.cycle_sec == 110
    assert planned.offset_sec == 17  # offsets never move


def test_floor_limited_transfer_never_goes_below_cross_floor():
    prog = P.JunctionProgram(
        junction_id=9,
        cycle_sec=90,
        phases=[
            {"green_sec": 30, "movements": ["through"]},
            {"green_sec": 12, "movements": ["cross"]},  # below any floor headroom
        ],
        offset_sec=0,
        source="osm_assumed",
    )
    planned = P.plan_for_junction(prog, "BLACK")
    # cross can only give 12-10=2s
    assert planned.phases[1]["green_sec"] == 10
    assert planned.phases[0]["green_sec"] == 32


def test_single_phase_program_is_skipped():
    prog = P.JunctionProgram(
        junction_id=3,
        cycle_sec=60,
        phases=[{"green_sec": 40, "movements": ["through"]}],
        offset_sec=0,
        source="osm_assumed",
    )
    assert P.plan_for_junction(prog, "RED") is None


def test_plan_city_returns_only_changed_junctions():
    progs = [seed_program(1), seed_program(2)]
    planned = P.plan_city(progs, "AMBER")
    assert [p.junction_id for p in planned] == [1, 2]
    assert P.plan_city(progs, "GREEN") == []


def test_cycle_stays_within_006_check_bounds():
    for band in ("AMBER", "RED", "BLACK"):
        planned = P.plan_for_junction(seed_program(cycle=240), band)
        assert 40 <= planned.cycle_sec <= 240
        greens = sum(p["green_sec"] for p in planned.phases)
        assert greens <= planned.cycle_sec  # never over-allocate the cycle


# ─── plans: phase math ───────────────────────────────────────────────────────


def test_green_ratio_matches_calibration_math():
    phases = [{"green_sec": 50}, {"green_sec": 42}]
    assert P.green_ratio(phases, 110) == pytest.approx(92 / 110)
    assert P.green_ratio(phases, 0) == 0.0


def test_active_phase_is_deterministic_function_of_epoch():
    phases = [{"green_sec": 50, "movements": ["through"]}, {"green_sec": 42, "movements": ["cross"]}]
    a = P.active_phase(phases, 110, 0, 1_000_000_010)
    b = P.active_phase(phases, 110, 0, 1_000_000_010 + 110)  # one cycle later
    assert a["index"] == b["index"]
    assert a["movements"] == b["movements"]
    # offset 0: t=10 is inside the first (through) phase
    assert a["movements"] == ["through"]
    cross = P.active_phase(phases, 110, 0, 1_000_000_060)
    assert cross["movements"] == ["cross"]
    # lost time (92..110) trails the last green phase
    # 999,999,990 is a cycle boundary +95 s → position 95 (lost time)
    lost = P.active_phase(phases, 110, 0, 1_000_000_085)
    assert lost["lost"] is True and lost["movements"] == ["cross"]


def test_phase_at_wraps_modulo_cycle_for_every_offset():
    """The old timeline started at the offset and never wrapped, so a
    junction with offset 85 of 110 showed 'lost' for 85 s of every cycle.
    The shared clock shows lost time for exactly cycle - sum(greens)."""
    phases = [{"green_sec": 50, "movements": ["through"]},
              {"green_sec": 42, "movements": ["cross"]}]
    for offset in (0, 17, 51, 85, 109):
        program = {"cycle_sec": 110, "offset_sec": offset, "phases": phases}
        lost = sum(
            1 for k in range(110)
            if P.phase_at(program, 1_000_000_000 + k).lost
        )
        assert lost == 110 - 92, (offset, lost)
        # and the green windows are exactly as long as programmed
        through = sum(
            1 for k in range(110)
            if P.phase_at(program, 1_000_000_000 + k).state_of("through") == "green"
        )
        assert through == 50


def test_phase_at_position_is_epoch_minus_offset_mod_cycle():
    phases = [{"green_sec": 47, "movements": ["through"]},
              {"green_sec": 38, "movements": ["cross"]}]
    program = {"cycle_sec": 100, "offset_sec": 51, "phases": phases}
    epoch = 1_793_854_800 + 51  # a 100 s boundary + the offset -> position 0
    st = P.phase_at(program, epoch)
    assert st.sec_into_cycle == 0 and st.index == 0 and not st.lost
    assert st.remaining_sec == 47
    st = P.phase_at(program, epoch + 48)
    assert st.index == 1 and st.movement_states == {"through": "amber", "cross": "green"}
    st = P.phase_at(program, epoch + 86)  # lost time after cross
    assert st.lost and st.movements == ("cross",)
    assert st.movement_states == {"through": "red", "cross": "amber"}
    assert st.remaining_sec == 100 - 86
    # sub-second precision floors, never rounds into the next second
    assert P.phase_at(program, epoch + 46.999).index == 0


def test_phase_at_accepts_every_program_shape():
    tuples = ((40, ("NS",)), (40, ("EW",)))
    dicts = [{"green_sec": 40, "movements": ["NS"]},
             {"green_sec": 40, "movements": ["EW"]}]
    jp = P.JunctionProgram(junction_id=1, cycle_sec=100, phases=dicts,
                           offset_sec=5, source="osm_assumed")
    for epoch in (1_000_000_000, 1_000_000_047, 1_000_000_093):
        a = P.phase_at({"cycle_sec": 100, "offset_sec": 5, "phases": tuples}, epoch)
        b = P.phase_at(jp, epoch)
        c = P.phase_at({"cycle_sec": 100, "offset_sec": 5,
                        "phases": json.dumps(dicts)}, epoch)
        assert a == b == c


def test_phase_at_degenerate_program_defines_no_state():
    for program in (
        {"cycle_sec": 0, "phases": [{"green_sec": 40, "movements": ["through"]}]},
        {"cycle_sec": 100, "phases": [{"green_sec": 0, "movements": ["through"]}]},
        {"cycle_sec": 100, "phases": []},
    ):
        st = P.phase_at(program, 1_000_000_000)
        assert st.lost and st.index is None
        assert st.state_of("through") == "none"  # no decision, never red


def test_active_phase_keeps_the_c1_keys():
    phases = [{"green_sec": 50, "movements": ["through"]},
              {"green_sec": 42, "movements": ["cross"]}]
    out = P.active_phase(phases, 110, 85, 1_000_000_000)
    for key in ("index", "movements", "green_sec", "lost"):
        assert key in out
    assert isinstance(out["movements"], list)
    assert set(out["movement_states"]) == {"through", "cross"}


# ─── plans: city band aggregation ────────────────────────────────────────────


def test_city_band_is_most_severe_subject():
    movements = {
        "a": {"effective_class": "GREEN", "situation_code": None},
        "b": {"effective_class": "AMBER"},
        "c": {"effective_class": "RED"},
        "d": {"situation_code": "BLACK"},
    }
    assert P.city_band_from_status(movements) == "BLACK"
    assert P.city_band_from_status({"a": {"effective_class": "AMBER"}}) == "AMBER"


def test_city_band_ignores_garbage_and_empty():
    assert P.city_band_from_status({}) == "GREEN"
    assert P.city_band_from_status({"x": {"effective_class": "TEAL"}}) == "GREEN"
    assert P.city_band_from_status({"x": None}) == "GREEN"


# ─── gate: shared/exclusive semantics ────────────────────────────────────────


def test_gate_writer_excludes_concurrent_shared():
    gate = SignalPolicyGate()
    order: list[str] = []

    async def scenario() -> None:
        # A twin request holds shared; the writer must wait for it.
        async with gate.shared():
            order.append("twin-start")
            await asyncio.sleep(0.05)
            order.append("twin-end")
        async with gate.writer_exclusive():
            order.append("writer")

    asyncio.run(scenario())
    assert order == ["twin-start", "twin-end", "writer"]


def test_gate_shared_arriving_during_writer_waits_for_release():
    gate = SignalPolicyGate()
    order: list[str] = []

    async def scenario() -> None:
        async def shared_task() -> None:
            async with gate.shared():
                order.append("shared-entered")

        async with gate.writer_exclusive():
            order.append("writer-hold")
            task = asyncio.create_task(shared_task())
            await asyncio.sleep(0.05)  # let the shared task queue on the lock
            assert "shared-entered" not in order  # still excluded
            order.append("writer-body-done")
        await task  # proceeds only after the writer released
        order.append("done")

    asyncio.run(scenario())
    assert order == ["writer-hold", "writer-body-done", "shared-entered", "done"]
    assert gate.snapshot()["twin_in_flight"] == 0
    assert gate.snapshot()["writer_active"] is False


def test_gate_many_shared_concurrent():
    gate = SignalPolicyGate()

    async def one() -> None:
        async with gate.shared():
            await asyncio.sleep(0.02)

    async def scenario() -> None:
        await asyncio.gather(*(one() for _ in range(5)))

    asyncio.run(scenario())
    assert gate.snapshot()["twin_in_flight"] == 0


# ─── writer: flag + fail-closed ──────────────────────────────────────────────


def test_writer_disabled_by_default(monkeypatch):
    monkeypatch.delenv(W.WRITER_FLAG_ENV, raising=False)
    assert W.writer_enabled() is False
    pool = FakePool([seed_program()])
    report = asyncio.run(
        W.apply_city_plan(pool, band="RED", writer_flag_value=None)
    )
    assert report["applied"] is False
    assert report["reason"] == "writer_disabled"
    assert pool.executed == []  # nothing written, no snapshot


def test_writer_flag_env(monkeypatch):
    monkeypatch.setenv(W.WRITER_FLAG_ENV, "1")
    assert W.writer_enabled() is True
    monkeypatch.setenv(W.WRITER_FLAG_ENV, "0")
    assert W.writer_enabled() is False
    monkeypatch.setenv(W.WRITER_FLAG_ENV, "yes")
    assert W.writer_enabled() is True


def test_apply_fail_closed_when_shadow_fails():
    pool = FakePool([seed_program()])

    def failing_shadow(*args, **kwargs):
        return False, W.ShadowReport("unavailable", "twin_down")

    report = asyncio.run(
        W.apply_city_plan(
            pool,
            band="RED",
            writer_flag_value=True,
            shadow_fn=failing_shadow,
        )
    )
    assert report["applied"] is False
    assert report["reason"] == "shadow_eval_failed_closed"
    assert report["shadow_eval"]["status"] == "unavailable"
    assert pool.executed == []  # fail-closed: zero writes


def test_apply_commits_with_snapshot_once_then_skips_snapshot():
    pool = FakePool([seed_program(1), seed_program(2)])

    def passing_shadow(*args, **kwargs):
        return True, W.ShadowReport("passed", "ok", 10.0, 10.2)

    report = asyncio.run(
        W.apply_city_plan(
            pool,
            band="AMBER",
            event_id="11111111-1111-1111-1111-111111111111",
            writer_flag_value=True,
            shadow_fn=passing_shadow,
        )
    )
    assert report["applied"] is True
    assert report["snapshots_written"] == 2
    inserts = [sql for sql, _ in pool.executed if "signal_program_history" in sql]
    upserts = [sql for sql, _ in pool.executed if "ON CONFLICT (junction_id)" in sql]
    assert len(inserts) == 2
    assert len(upserts) == 2
    assert "'snapshot'" in inserts[0]

    # Second apply: history junctions now exist → no new snapshots.
    pool.history_junctions = {1, 2}
    pool.executed.clear()
    report2 = asyncio.run(
        W.apply_city_plan(
            pool,
            band="RED",
            writer_flag_value=True,
            shadow_fn=passing_shadow,
        )
    )
    assert report2["applied"] is True
    assert report2["snapshots_written"] == 0
    inserts2 = [sql for sql, _ in pool.executed if "signal_program_history" in sql]
    assert inserts2 == []


def test_apply_green_writes_nothing():
    pool = FakePool([seed_program()])
    report = asyncio.run(
        W.apply_city_plan(pool, band="GREEN", writer_flag_value=True)
    )
    assert report["applied"] is False
    assert report["reason"] == "no_junctions_changed"
    assert pool.executed == []


def test_restore_rebuilds_osm_assumed_and_appends_marker():
    pool = FakePool([seed_program(1)])
    pool.history_junctions = {1}
    result = asyncio.run(W.restore_programs(pool, restore_all=True))
    assert result["restored"] >= 1
    marker = [sql for sql, _ in pool.executed if "'restore'" in sql]
    assert len(marker) >= 1
    upserts = [args for sql, args in pool.executed if "ON CONFLICT (junction_id)" in sql]
    assert any(args[-1] == "osm_assumed" for args in upserts)


def test_restore_requires_scope():
    pool = FakePool([seed_program(1)])
    result = asyncio.run(W.restore_programs(pool))
    assert "error" in result


def test_overrides_from_programs_matches_calibration_parity():
    progs = [seed_program(1)]
    ov = W.overrides_from_programs(
        progs, [{"segment_id": 101, "junction_id": 1}]
    )
    assert ov[101]["green_ratio"] == pytest.approx(92 / 110)
    assert ov[101]["cycle_sec"] == 110


def test_shadow_evaluate_plan_is_deterministic_and_fails_closed():
    prog = seed_program()
    adjacency = [{"segment_id": 101, "junction_id": 1}]
    candidate = P.plan_for_junction(prog, "AMBER")
    assert candidate is not None
    r1 = W.shadow_evaluate_plan([prog], [candidate], adjacency, [101])
    r2 = W.shadow_evaluate_plan([prog], [candidate], adjacency, [101])
    assert r1[0] == r2[0]
    assert r1[1].status == r2[1].status
    # empty route → fail closed without running
    ok, rep = W.shadow_evaluate_plan([prog], [candidate], adjacency, [])
    assert ok is False and rep.status == "unavailable"


def test_last_plan_report_tracks_latest_decision():
    pool = FakePool([seed_program()])
    asyncio.run(
        W.apply_city_plan(pool, band="AMBER", writer_flag_value=False)
    )
    assert W.last_plan_report()["reason"] == "writer_disabled"


def test_writer_reads_no_env_literals_in_sql():
    """Every executed statement is one of the static local literals."""
    pool = FakePool([seed_program(1)])
    asyncio.run(
        W.apply_city_plan(
            pool,
            band="AMBER",
            event_id="22222222-2222-2222-2222-222222222222",
            writer_flag_value=True,
            shadow_fn=lambda *a, **k: (True, W.ShadowReport("passed", "ok")),
        )
    )
    for sql, args in pool.executed:
        assert "$" in sql  # parameter-bound placeholders present
        assert "{" not in sql and "}" not in sql  # no format interpolation
