"""Run task registry + reaper — asynchronous runs (contract C-G).

``POST /api/v1/emulator/runs`` ledgers a run and returns 202 at once; the
run itself is a background task held here:

* one run per use case at a time (a second launch is a :class:`RunConflict`
  → HTTP 409 naming the running run);
* live progress {frame, total, delivered, failed} straight from the run's
  :class:`~event_emulator.runner.RunControl`;
* ``stop`` sets the run's stop event — the frame loop ends between frames,
  the corridor event is closed through C-F and the row finishes 'stopped';
* ``shutdown`` (process exit) interrupts every run the same way
  ('interrupted'), within a bounded wait, then cancels any straggler;
* the start hook (the living city yielding the run's roads) runs in the
  background: it waits on the city's tick and on oracle calls, and the 202
  never does.

The :class:`Reaper` is the safety net for what a dead process could not
finish: a 'running' row left by an earlier process at THIS host:port (a
restart — two live emulators can never share an address) is marked
'interrupted' on the first pass after boot, as is any row whose owner
stopped heart-beating (every 10 s); their corridor events are closed
through C-F, and any emulator event still live after its run left
'running' is closed too.  Rows owned by THIS process are never touched,
and a live peer elsewhere is judged by its heartbeat alone, so a second
emulator process sharing the ledger is never reaped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from event_emulator.library import UseCase
from event_emulator.runner import (
    RunControl,
    RunnerDeps,
    RunOutcome,
    close_event,
    execute_replay,
    execute_run,
    ledger_run,
)
from event_emulator.spec import EmulationSpec, ResolvedRun
from event_emulator.store import utcnow
from event_emulator.tape import CauseTape

logger = structlog.get_logger(__name__)

__all__ = [
    "RunConflict",
    "RunTask",
    "TaskRegistry",
    "Reaper",
    "STALE_HEARTBEAT_SEC",
]

#: A running row whose heartbeat is older than this has no live owner.
STALE_HEARTBEAT_SEC = 45.0

#: How long ``stop`` waits for a run to close its event and its row.
STOP_WAIT_SEC = 15.0

#: Finished runs kept in memory (their progress outlives the task briefly).
_KEEP_FINISHED = 64


class RunConflict(Exception):
    """The use case already has a running run."""

    def __init__(self, use_case_id: str, run_id: int) -> None:
        super().__init__(f"use case {use_case_id!r} is already running as run {run_id}")
        self.use_case_id = use_case_id
        self.run_id = run_id


@dataclass
class RunTask:
    run_id: int
    use_case_id: str
    route_segment_ids: tuple[int, ...]
    control: RunControl
    kind: str = "use_case"            # 'use_case' | 'replay'
    replay_of: int | None = None
    task: asyncio.Task | None = None
    outcome: RunOutcome | None = None
    error: str | None = None
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def status(self) -> str:
        if self.running:
            return "running"
        if self.outcome is not None:
            return self.outcome.status
        return self.control.reason or "failed"

    @property
    def reason(self) -> str | None:
        """Why the run ended as it did (None while running / completed)."""
        if self.running:
            return None
        if self.outcome is not None:
            return self.outcome.reason
        return self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "use_case_id": self.use_case_id,
            "kind": self.kind,
            "status": self.status,
            "reason": self.reason,
            "progress": self.control.progress.to_dict(),
            "event_id": self.control.event_id,
            "event_status": self.control.event_status,
            "replay_of_run_id": self.replay_of,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class TaskRegistry:
    """Every run this process is executing (and the last few it finished)."""

    def __init__(
        self,
        deps: RunnerDeps,
        library: dict[str, UseCase],
        *,
        on_start: Callable[[RunTask], Awaitable[None]] | None = None,
    ) -> None:
        self._deps = deps
        self._library = library
        self._tasks: dict[int, RunTask] = {}
        self._by_use_case: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._hooks: set[asyncio.Task] = set()
        self.on_start = on_start
        self.last_finished: RunTask | None = None

    # -- views ---------------------------------------------------------------

    def get(self, run_id: int) -> RunTask | None:
        return self._tasks.get(run_id)

    def running(self) -> list[RunTask]:
        return [t for t in self._tasks.values() if t.running]

    def owned_run_ids(self) -> set[int]:
        return {t.run_id for t in self.running()}

    def reserved_routes(self) -> dict[int, tuple[str, frozenset[int]]]:
        """run_id → (use case, route segments) of every running run — the
        roads a manual / flagship run holds (the living city yields them)."""
        return {t.run_id: (t.use_case_id, frozenset(t.route_segment_ids))
                for t in self.running()}

    # -- launch --------------------------------------------------------------

    def _check_free(self, use_case_id: str) -> None:
        rid = self._by_use_case.get(use_case_id)
        if rid is not None:
            task = self._tasks.get(rid)
            if task is not None and task.running:
                raise RunConflict(use_case_id, rid)

    async def start_run(self, spec: EmulationSpec) -> RunTask:
        """Ledger + spawn a live run; returns as soon as the row exists."""
        use_case = self._library[spec.use_case_id]
        async with self._lock:
            self._check_free(spec.use_case_id)
            resolved = ResolvedRun.resolve(spec, use_case)
            spec = spec.model_copy(update={"t0_anchor": resolved.t0_anchor})
            run_id = await ledger_run(
                self._deps, spec, use_case, t0=resolved.t0_anchor, seed=resolved.seed,
                is_replay=False, replay_of=None,
            )
            if run_id is None:
                raise RuntimeError("runs ledger unavailable (no database)")
            control = RunControl()
            control.run_id = run_id
            task = RunTask(
                run_id=run_id, use_case_id=spec.use_case_id,
                route_segment_ids=tuple(use_case.config["route_segment_ids"]),
                control=control,
            )
            self._register(task, execute_run(
                self._deps, spec, library=self._library, run_id=run_id, control=control,
            ))
        self._started(task)
        return task

    async def start_replay(
        self, *, tape: CauseTape, replay_of: int, clock_scale: float,
        t0: datetime | None = None,
    ) -> RunTask:
        """Ledger + spawn a verbatim replay of a stored tape."""
        use_case = self._library[tape.use_case_id]
        anchor = t0 or utcnow()
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        spec = EmulationSpec(use_case_id=tape.use_case_id, seed=tape.seed,
                             clock_scale=clock_scale)
        async with self._lock:
            self._check_free(tape.use_case_id)
            run_id = await ledger_run(
                self._deps, spec, use_case, t0=anchor, seed=tape.seed,
                is_replay=True, replay_of=replay_of,
            )
            if run_id is None:
                raise RuntimeError("runs ledger unavailable (no database)")
            control = RunControl()
            control.run_id = run_id
            task = RunTask(
                run_id=run_id, use_case_id=tape.use_case_id,
                route_segment_ids=tuple(use_case.config["route_segment_ids"]),
                control=control, kind="replay", replay_of=replay_of,
            )
            self._register(task, execute_replay(
                self._deps, tape=tape, clock_scale=clock_scale, replay_of=replay_of,
                t0=anchor, library=self._library, run_id=run_id, control=control,
            ))
        self._started(task)
        return task

    def _register(self, task: RunTask, coro: Awaitable[RunOutcome]) -> None:
        self._tasks[task.run_id] = task
        self._by_use_case[task.use_case_id] = task.run_id
        task.task = asyncio.create_task(self._drive(task, coro), name=f"emulator-run-{task.run_id}")

    def _started(self, task: RunTask) -> None:
        """Fire the start hook in the background (held until it ends)."""
        if self.on_start is None:
            return
        hook = asyncio.create_task(self._run_hook(task), name=f"emulator-run-{task.run_id}-hook")
        self._hooks.add(hook)
        hook.add_done_callback(self._hooks.discard)

    async def _run_hook(self, task: RunTask) -> None:
        assert self.on_start is not None
        try:
            await self.on_start(task)
        except Exception as exc:  # noqa: BLE001 - a hook never sinks a run
            logger.warning("tasks.on_start_failed", run_id=task.run_id, error=str(exc))

    async def _drive(self, task: RunTask, coro: Awaitable[RunOutcome]) -> None:
        try:
            task.outcome = await coro
        except asyncio.CancelledError:
            task.error = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, the row is closed
            task.error = f"{type(exc).__name__}: {exc}"
            logger.error("tasks.run_crashed", run_id=task.run_id, error=task.error)
            if self._deps.store is not None:
                try:
                    await self._deps.store.finish_run(
                        task.run_id, "failed", {"reason": task.error}
                    )
                except Exception as exc2:  # noqa: BLE001
                    logger.error("tasks.finish_failed", run_id=task.run_id, error=str(exc2))
        finally:
            task.finished_at = utcnow()
            if self._by_use_case.get(task.use_case_id) == task.run_id:
                del self._by_use_case[task.use_case_id]
            self.last_finished = task
            self._prune()

    def _prune(self) -> None:
        done = sorted((t for t in self._tasks.values() if not t.running),
                      key=lambda t: t.run_id)
        for t in done[:-_KEEP_FINISHED]:
            self._tasks.pop(t.run_id, None)

    # -- stop / shutdown -----------------------------------------------------

    async def stop(self, run_id: int, reason: str = "stopped",
                   timeout: float = STOP_WAIT_SEC) -> RunTask | None:
        """Stop a running run and wait for it to close its event + row."""
        task = self._tasks.get(run_id)
        if task is None or not task.running:
            return task
        task.control.request_stop(reason)
        assert task.task is not None
        try:
            await asyncio.wait_for(asyncio.shield(task.task), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("tasks.stop_timeout", run_id=run_id)
            task.task.cancel()
        except Exception:  # noqa: BLE001 - _drive already recorded it
            pass
        return task

    async def shutdown(self, timeout: float = 6.0) -> None:
        """Interrupt every running run (process exit), bounded."""
        for hook in list(self._hooks):
            hook.cancel()
        running = self.running()
        if not running:
            return
        for t in running:
            t.control.request_stop("interrupted")
        pending = [t.task for t in running if t.task is not None]
        done, still = await asyncio.wait(pending, timeout=timeout)
        for t in still:
            t.cancel()
        if still:
            await asyncio.wait(still, timeout=2.0)


def _restarted_here(runner: Any, identity: dict[str, Any] | None) -> bool:
    """Was the row's owner an EARLIER process at our host:port?"""
    if not identity or not isinstance(runner, dict):
        return False
    if runner.get("port") is None or identity.get("port") is None:
        return False
    return (str(runner.get("host")) == str(identity.get("host"))
            and int(runner["port"]) == int(identity["port"])
            and runner.get("boot") != identity.get("boot"))


def _age_sec(stamp: str | None, fallback: datetime | None, now: datetime) -> float:
    ts: datetime | None = None
    if stamp:
        try:
            ts = datetime.fromisoformat(stamp)
        except ValueError:
            ts = None
    if ts is None:
        ts = fallback
    if ts is None:
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds()


class Reaper:
    """Interrupts orphaned runs and closes events their runs left open."""

    def __init__(
        self,
        deps: RunnerDeps,
        owned: Callable[[], set[int]],
        *,
        identity: dict[str, Any] | None = None,
        stale_after_sec: float = STALE_HEARTBEAT_SEC,
        interval_sec: float = 20.0,
        legacy_grace: timedelta = timedelta(minutes=10),
    ) -> None:
        self._deps = deps
        self._owned = owned
        self._identity = identity
        self._stale = stale_after_sec
        self._interval = interval_sec
        self._legacy_grace = legacy_grace
        self.last_pass: dict[str, Any] | None = None

    async def run_once(self) -> dict[str, Any]:
        store = self._deps.store
        report: dict[str, Any] = {"interrupted": [], "events_closed": [], "errors": []}
        if store is None:
            return report
        now = utcnow()
        owned = self._owned()
        try:
            rows = await store.running_runs()
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"running_runs: {exc}")
            rows = []
        orphans: list[int] = []
        for row in rows:
            rid = int(row["run_id"])
            if rid in owned:
                continue
            restarted = _restarted_here(row.get("runner"), self._identity)
            if (not restarted and
                    _age_sec(row.get("heartbeat_at"), row.get("created_at"), now) < self._stale):
                continue
            orphans.append(rid)
            doc: dict[str, Any] = {
                "reason": ("the emulator restarted while the run was in flight" if restarted
                           else "owner stopped heart-beating (emulator restarted or died)"),
                "reaped_at": now.isoformat(),
            }
            if row.get("progress") is not None:
                doc["progress"] = row["progress"]
            try:
                await store.finish_run(rid, "interrupted", doc)
                report["interrupted"].append(rid)
            except Exception as exc:  # noqa: BLE001
                report["errors"].append(f"finish {rid}: {exc}")

        # events still live although their run is over (or the run is one
        # we just reaped) — close them through C-F
        try:
            events = await store.live_emulator_events()
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"live_emulator_events: {exc}")
            events = []
        run_ids = sorted({int(e["run_id"]) for e in events
                          if str(e.get("run_id") or "").isdigit()})
        try:
            finished = await store.finished_run_ids([r for r in run_ids if r not in owned])
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"finished_run_ids: {exc}")
            finished = set()
        for ev in events:
            raw = str(ev.get("run_id") or "")
            if raw.isdigit():
                rid = int(raw)
                if rid in owned or rid not in finished:
                    continue
                note = f"emulator run {rid} is over — event closed by the emulator reaper"
            else:
                end = ev.get("planned_end")
                if end is None or (end.replace(tzinfo=end.tzinfo or timezone.utc)
                                   > now - self._legacy_grace):
                    continue
                note = "pre-ledger emulator event past its window — closed by the emulator reaper"
            entry = await close_event(self._deps.oracle, str(ev["event_id"]), note)
            if entry["result"] == "delivered":
                report["events_closed"].append(str(ev["event_id"]))
            else:
                report["errors"].append(f"close {ev['event_id']}: {entry.get('reason')}")
        if report["interrupted"] or report["events_closed"]:
            logger.info("reaper.pass", **{k: v for k, v in report.items() if v})
        report["at"] = now.isoformat()
        self.last_pass = report
        return report

    async def loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - the reaper never dies
                logger.error("reaper.failed", error=str(exc))
            await asyncio.sleep(self._interval)
