"""Twin calibration scheduler — prime at startup, replay hourly on field data.

Driven by the oracle lifespan (``__main__.py``) in two steps:

  * :meth:`CalibrationScheduler.prime` is AWAITED before the situation
    engine starts.  It restores the newest persisted calibration
    (``simulation.calibrations``, at most
    :data:`~traffic_oracle.simulation.calibration.CALIBRATION_MAX_AGE_DAYS`
    old) and runs one coverage check (a replay only with >=
    :data:`MIN_FIELD_HOURS` field hours), so the loop's FIRST decision
    already quotes the real calibration reason.  Without it the first
    publish after every restart quoted the placeholder ("not yet checked
    for field observations") or a reloaded ledger reason, and that stale
    text stuck on the audit trail (``adaptive.param_overrides``, the
    ``/city/feed`` AMA items) until the band changed.  Bounded by
    :data:`PRIME_TIMEOUT_SEC` and never fatal: on timeout or failure the
    lifespan proceeds and the background task checks again at once.
  * :meth:`start` runs the background task: every ``interval_sec``
    (hourly) it checks the field coverage of ``traffic.hourly_aggregates``
    — rolled up from ``data_quality = 'real'`` observations only — and:

      * with >= :data:`MIN_FIELD_HOURS` field hours in the window, runs
        the replay calibration over that window and ledgers the result;
      * otherwise leaves the twin as it is and records WHY no replay ran
        (``no field observations — simulated feed only`` while only the
        simulated feed is flowing) in the shared calibration state, which
        the adaptive shadow gate and ``/adaptive/status`` quote verbatim.

    A failed check is retried after :data:`RETRY_DELAY_SEC`, not an hour.

Every failure is caught and surfaced through :meth:`health` — the
scheduler never takes the oracle down and never marks the twin calibrated
on anything but a passing field replay.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from traffic_oracle.simulation.calibration import (
    CALIBRATION,
    CALIBRATION_MAX_AGE_DAYS,
    MIN_FIELD_HOURS,
    field_coverage,
    persist_calibration,
    reload_calibration,
    run_replay,
)

logger = structlog.get_logger(__name__)

DEFAULT_INTERVAL_SEC = 3600.0
#: Upper bound on the startup prime (three short queries normally; a full
#: field replay — ~0.2 s of fitting for a 7-day window — when >= 24 field
#: hours exist).  The oracle never waits longer than this to start.
PRIME_TIMEOUT_SEC = 30.0
#: A failed check (database blip) is retried this soon, not an hour later.
RETRY_DELAY_SEC = 60.0


class CalibrationScheduler:
    """Startup prime, then a background task: hourly coverage check + replay."""

    def __init__(
        self,
        pool: Any | None,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
        window_days: int = CALIBRATION_MAX_AGE_DAYS,
        min_field_hours: int = MIN_FIELD_HOURS,
        prime_timeout_sec: float = PRIME_TIMEOUT_SEC,
        retry_delay_sec: float = RETRY_DELAY_SEC,
    ) -> None:
        self._pool = pool
        self._interval_sec = interval_sec
        self._window_days = window_days
        self._min_field_hours = min_field_hours
        self._prime_timeout_sec = prime_timeout_sec
        self._retry_delay_sec = retry_delay_sec
        self._task: asyncio.Task | None = None
        self._prime_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.reloaded: bool | None = None
        self.primed = False
        self.prime_status: str | None = None
        self.prime_ms: float | None = None
        self.last_check_at: str | None = None
        self.last_run_at: str | None = None
        self.last_status: str | None = None
        self.last_error: str | None = None
        self.last_coverage: dict | None = None
        self.checks = 0
        self.runs = 0
        self._next_check_monotonic: float | None = None

    # -- startup prime ---------------------------------------------------------

    async def prime(self) -> dict:
        """Make the calibration state final BEFORE the first adaptive decision.

        Reload the newest ledger row, then one coverage check (a replay only
        with >= ``min_field_hours`` field hours).  Awaited by the lifespan
        ahead of the situation engine; bounded by ``prime_timeout_sec`` and
        never raises (a cancelled lifespan still propagates).  On timeout or
        failure ``primed`` stays False and :meth:`start` checks at once.

        The bound abandons an overdue prime instead of awaiting it:
        cancelling an in-flight asyncpg query talks to the same server
        (cancel request, connection reset), so on a hung database
        ``asyncio.wait_for`` would hold the oracle's startup until the
        database answered again.
        """
        started = time.monotonic()
        task = asyncio.create_task(self._prime_steps(), name="twin-calibration-prime")
        self._prime_task = task
        try:
            done, _ = await asyncio.wait({task}, timeout=self._prime_timeout_sec)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if not done:
            task.cancel()
            task.add_done_callback(_consume_outcome)
            self.prime_status = "prime_timeout"
            self.last_error = (
                f"prime: no answer within {self._prime_timeout_sec:g} s"
            )
        elif task.cancelled():
            self.prime_status = "prime_failed"
            self.last_error = "prime: cancelled"
        elif (exc := task.exception()) is not None:
            self.prime_status = "prime_failed"
            self.last_error = f"prime: {type(exc).__name__}: {exc}"
        else:
            self.primed = True
            self.prime_status = self.last_status
        self.prime_ms = round((time.monotonic() - started) * 1000.0, 1)
        logger.info(
            "calibration.primed",
            primed=self.primed,
            status=self.prime_status,
            ms=self.prime_ms,
            calibrated=CALIBRATION.calibrated,
            reason=CALIBRATION.reason,
            error=None if self.primed else self.last_error,
        )
        return {
            "primed": self.primed,
            "status": self.prime_status,
            "ms": self.prime_ms,
            "calibrated": CALIBRATION.calibrated,
            "reason": CALIBRATION.reason,
        }

    async def _prime_steps(self) -> None:
        await self._reload()
        await self.check_and_run()

    async def _reload(self) -> None:
        try:
            self.reloaded = await reload_calibration(self._pool)
        except Exception as exc:  # noqa: BLE001 - surfaced, never fatal
            self.last_error = f"reload: {type(exc).__name__}: {exc}"

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="twin-calibration")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        prime = self._prime_task
        if prime is not None and not prime.done():
            prime.cancel()
            # bounded: an abandoned prime on a hung database may never unwind
            await asyncio.wait({prime}, timeout=1.0)

    async def _sleep(self, seconds: float) -> None:
        self._next_check_monotonic = time.monotonic() + seconds
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _run(self) -> None:
        prime = self._prime_task
        if prime is not None and not prime.done():
            # An abandoned prime is still unwinding against a database that
            # stopped answering: let it finish first, so two checks (and
            # two replays) never run at once.
            await asyncio.wait({prime})
        if self.primed:
            # The prime just checked: the next check is a full interval away.
            await self._sleep(self._interval_sec)
        elif self.reloaded is None:
            # Never primed (or the prime died before its reload): reload,
            # then check at once — no fixed first delay.
            await self._reload()
        while not self._stop.is_set():
            ok = True
            try:
                await self.check_and_run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced, never fatal
                ok = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("calibration.scheduler_failed", error=self.last_error)
            await self._sleep(
                self._interval_sec
                if ok
                else min(self._interval_sec, self._retry_delay_sec)
            )

    # -- one cycle -------------------------------------------------------------

    async def check_and_run(self) -> dict:
        """One coverage check; replays when the field window is long enough."""
        if self._pool is None:
            self.last_status = "no_database"
            return {"status": "no_database"}
        self.checks += 1
        now = datetime.now(UTC)
        self.last_check_at = now.isoformat(timespec="seconds")
        coverage = await field_coverage(self._pool, self._window_days)
        self.last_coverage = coverage
        CALIBRATION.field_hours = coverage["field_hours"]
        CALIBRATION.feed_quality = coverage.get("feed_quality")
        CALIBRATION.checked_at = self.last_check_at
        if coverage["field_hours"] < self._min_field_hours:
            self.last_status = "waiting_for_field_data"
            if not CALIBRATION.calibrated and coverage["reason"]:
                CALIBRATION.mark_uncalibrated(coverage["reason"])
            logger.info(
                "calibration.replay_deferred",
                field_hours=coverage["field_hours"],
                reason=coverage["reason"],
            )
            return {"status": self.last_status, **coverage}
        window_start = now - timedelta(days=self._window_days)
        report = await run_replay(self._pool, window_start, now)
        report["trigger"] = "scheduler"
        await persist_calibration(self._pool, report)
        self.runs += 1
        self.last_run_at = self.last_check_at
        self.last_status = f"replay_{report.get('status')}"
        logger.info(
            "calibration.replay_scheduled_run",
            status=report.get("status"),
            calibrated=report.get("calibrated"),
            n=report.get("n_observations"),
        )
        return {"status": self.last_status, "report": report, **coverage}

    def health(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "interval_sec": self._interval_sec,
            "min_field_hours": self._min_field_hours,
            "window_days": self._window_days,
            "primed": self.primed,
            "prime_status": self.prime_status,
            "prime_ms": self.prime_ms,
            "reloaded_calibrated": self.reloaded,
            "checks": self.checks,
            "runs": self.runs,
            "last_check_at": self.last_check_at,
            "last_run_at": self.last_run_at,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "coverage": self.last_coverage,
            "next_check_in_sec": (
                round(max(0.0, self._next_check_monotonic - time.monotonic()), 1)
                if self._next_check_monotonic is not None
                else None
            ),
        }


def _consume_outcome(task: asyncio.Task) -> None:
    """Retrieve an abandoned prime's outcome (no 'never retrieved' noise)."""
    if not task.cancelled():
        task.exception()


__all__ = [
    "CalibrationScheduler",
    "DEFAULT_INTERVAL_SEC",
    "PRIME_TIMEOUT_SEC",
    "RETRY_DELAY_SEC",
]
