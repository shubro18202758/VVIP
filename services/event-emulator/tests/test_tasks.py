"""Task registry + reaper tests (fake seams).

The reaper is the startup / periodic safety net: a 'running' row left by
an earlier process at this host:port (a restart) is interrupted on the
first pass, as is a row whose owner stopped heart-beating, and its corridor
event is closed through C-F; rows this process owns, and rows a live peer
elsewhere still heart-beats, are never touched; an event still live after
its run ended is closed too.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from event_emulator.library import load_library
from event_emulator.runner import RunnerDeps
from event_emulator.spec import EmulationSpec
from event_emulator.tasks import Reaper, RunConflict, TaskRegistry
from fakes import FakeNats, FakeOracle, FakeStore, FakeTwin, FakeValkey, NoSleep


def _deps(store: FakeStore) -> RunnerDeps:
    return RunnerDeps(store=store, valkey=FakeValkey(), twin=FakeTwin(), nats=FakeNats(),
                      oracle=FakeOracle(store=store), sleep=NoSleep())


async def _orphan(store: FakeStore, event_status: str = "active", age_sec: float = 300.0,
                  use_case: str = "rath_yatra_monsoon_ambulance") -> tuple[int, str]:
    """A run row + its event as a dead process left them."""
    rid = await store.insert_run(
        use_case_id=use_case, spec={"mode": "use_case"}, spec_hash="h", seed=1,
        t0_anchor=datetime.now(timezone.utc), clock_scale=10.0, tape_json="{}",
        is_replay=False, replay_of_run_id=None)
    stale = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
    store.runs[rid]["spec"]["heartbeat_at"] = stale.isoformat()
    store.runs[rid]["spec"]["progress"] = {"frame": 40, "total": 734}
    eid = f"00000000-0000-0000-0000-{rid:012d}"
    await store.upsert_corridor_event(
        eid, eid, "procession", "none", "EXTREME", "orphan", stale, stale + timedelta(hours=1),
        {"source": "event-emulator", "mode": "use_case", "run_id": rid})
    store.events[eid]["status"] = event_status
    return rid, eid


class TestReaper:
    async def test_interrupts_stale_rows_and_completes_their_events(self):
        store = FakeStore()
        rid, eid = await _orphan(store)
        reaper = Reaper(_deps(store), owned=lambda: set())
        report = await reaper.run_once()
        assert report["interrupted"] == [rid]
        assert store.runs[rid]["spec"]["final_status"] == "interrupted"
        assert store.runs[rid]["status"] == "stopped"     # 009 CHECK fallback
        assert store.runs[rid]["spec"]["progress"]["frame"] == 40
        assert store.events[eid]["status"] == "completed"
        assert eid in report["events_closed"]

    async def test_planning_events_are_cancelled_not_completed(self):
        store = FakeStore()
        _rid, eid = await _orphan(store, event_status="planning")
        await Reaper(_deps(store), owned=lambda: set()).run_once()
        assert store.events[eid]["status"] == "cancelled"

    async def test_owned_and_fresh_rows_are_left_alone(self):
        store = FakeStore()
        owned, owned_eid = await _orphan(store)
        fresh, fresh_eid = await _orphan(store, age_sec=5.0)
        report = await Reaper(_deps(store), owned=lambda: {owned}).run_once()
        assert report["interrupted"] == []
        assert store.runs[owned]["status"] == "running"
        assert store.runs[fresh]["status"] == "running"
        assert store.events[owned_eid]["status"] == "active"
        assert store.events[fresh_eid]["status"] == "active"

    async def test_restart_at_this_address_is_reaped_at_once(self):
        store = FakeStore()
        me = {"host": "test-host", "port": 8090, "pid": 1, "boot": "boot-now"}
        # a run the PREVIOUS process at this host:port left behind, its
        # heartbeat still fresh (killed seconds ago)
        prev, prev_eid = await _orphan(store, age_sec=3.0)
        store.runs[prev]["spec"]["runner"] = dict(me, boot="boot-before", pid=7)
        # a live peer on another port of the same host: heartbeat rules
        peer, peer_eid = await _orphan(store, age_sec=3.0)
        store.runs[peer]["spec"]["runner"] = dict(me, port=8913, boot="boot-peer")
        # a row with no port in its identity (older build): heartbeat rules
        old, old_eid = await _orphan(store, age_sec=3.0)
        store.runs[old]["spec"]["runner"] = {"host": "test-host", "pid": 7}
        report = await Reaper(_deps(store), owned=lambda: set(), identity=me).run_once()
        assert report["interrupted"] == [prev]
        assert store.runs[prev]["spec"]["final_status"] == "interrupted"
        assert "restarted" in store.runs[prev]["spec"]["reason"]
        assert store.events[prev_eid]["status"] == "completed"
        for rid, eid in ((peer, peer_eid), (old, old_eid)):
            assert store.runs[rid]["status"] == "running"
            assert store.events[eid]["status"] == "active"

    async def test_own_boot_is_never_a_restart(self):
        store = FakeStore()
        me = {"host": "test-host", "port": 8090, "pid": 1, "boot": "boot-now"}
        rid, eid = await _orphan(store, age_sec=3.0)
        store.runs[rid]["spec"]["runner"] = dict(me)
        report = await Reaper(_deps(store), owned=lambda: set(), identity=me).run_once()
        assert report["interrupted"] == []
        assert store.runs[rid]["status"] == "running"

    async def test_events_of_finished_runs_are_closed(self):
        store = FakeStore()
        rid, eid = await _orphan(store, age_sec=1.0)
        await store.finish_run(rid, "completed", {})
        report = await Reaper(_deps(store), owned=lambda: set()).run_once()
        assert eid in report["events_closed"]
        assert store.events[eid]["status"] == "completed"

    async def test_pre_ledger_events_get_a_grace_window(self):
        store = FakeStore()
        now = datetime.now(timezone.utc)
        for eid, end in (("10000000-0000-0000-0000-000000000001", now - timedelta(hours=2)),
                         ("10000000-0000-0000-0000-000000000002", now - timedelta(minutes=2))):
            await store.upsert_corridor_event(
                eid, None, "procession", "none", "LOW", "legacy", end - timedelta(hours=1),
                end, {"source": "event-emulator"})
            store.events[eid]["status"] = "active"
        await Reaper(_deps(store), owned=lambda: set()).run_once()
        assert store.events["10000000-0000-0000-0000-000000000001"]["status"] == "completed"
        assert store.events["10000000-0000-0000-0000-000000000002"]["status"] == "active"


class TestRegistry:
    async def test_lock_stop_and_shutdown(self):
        store = FakeStore()
        deps = _deps(store)
        deps.sleep = asyncio.sleep                     # real time: the run stays up
        registry = TaskRegistry(deps, load_library())
        spec = EmulationSpec(use_case_id="riverfront_marathon", clock_scale=60.0)
        task = await registry.start_run(spec)
        assert task.running and registry.owned_run_ids() == {task.run_id}
        assert registry.reserved_routes()[task.run_id][1] == frozenset({1007, 1015, 1001, 1006})
        try:
            await registry.start_run(spec)
            raise AssertionError("second launch must conflict")
        except RunConflict as exc:
            assert exc.run_id == task.run_id
        await asyncio.sleep(0.2)
        other = await registry.start_run(EmulationSpec(use_case_id="metro_work_zone",
                                                       clock_scale=60.0))
        await registry.stop(task.run_id)
        assert task.status == "stopped"
        assert store.runs[task.run_id]["status"] == "stopped"
        await registry.shutdown(timeout=5.0)
        assert other.status == "interrupted"
        assert store.runs[other.run_id]["spec"]["final_status"] == "interrupted"
        assert registry.running() == []
        # every event either run opened is closed
        live = [e for e in store.events.values() if e["status"] in ("planning", "active")]
        assert live == []

    async def test_start_hook_runs_in_the_background(self):
        # the hook (the living city yielding the run's roads) waits on the
        # city's tick lock and the oracle: start_run — the 202 — never does
        store = FakeStore()
        deps = _deps(store)
        deps.sleep = asyncio.sleep
        release = asyncio.Event()
        seen: list[int] = []

        async def slow_hook(task):
            await release.wait()
            seen.append(task.run_id)

        registry = TaskRegistry(deps, load_library(), on_start=slow_hook)
        loop = asyncio.get_running_loop()
        started = loop.time()
        task = await registry.start_run(EmulationSpec(use_case_id="metro_work_zone",
                                                      clock_scale=60.0))
        assert loop.time() - started < 0.5 and task.running and seen == []
        release.set()
        await asyncio.sleep(0.05)
        assert seen == [task.run_id]
        # a hook still pending at shutdown is cancelled, never awaited
        release.clear()
        other = await registry.start_run(EmulationSpec(use_case_id="riverfront_marathon",
                                                       clock_scale=60.0))
        await registry.shutdown(timeout=5.0)
        assert other.status == "interrupted" and seen == [task.run_id]
        await asyncio.sleep(0)
        assert registry._hooks == set()

    async def test_reaped_progress_keeps_the_tape_total(self):
        # the run row learns its total as soon as the tape is built, so a
        # process killed before its first heartbeat is reaped as 0 of N
        store = FakeStore()
        deps = _deps(store)
        deps.sleep = asyncio.sleep
        registry = TaskRegistry(deps, load_library())
        task = await registry.start_run(EmulationSpec(use_case_id="metro_work_zone",
                                                      clock_scale=1.0))
        await asyncio.sleep(0.1)
        progress = store.runs[task.run_id]["spec"]["progress"]
        assert progress["total"] > 0 and progress["total"] == task.control.progress.total
        await registry.shutdown(timeout=5.0)
