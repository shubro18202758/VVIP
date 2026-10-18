"""In-process twin/writer exclusion gate (contract C7).

Why: the twin and the shadow-evaluator both read
``corridor.signal_programs`` (``simulation/calibration.py:
load_signal_overrides``) to build their networks.  A city-plan writer
commit landing *between* the baseline arm and the candidate arm of a
shadow-eval — or between two twin replications that must reproduce
bit-identically — would silently invalidate the comparison.  The twin runs
inside this same traffic-oracle process (``AsgiTwinPort`` →
``/api/v1/simulate/twin``), so an in-process gate is the correct scope.

Protocol (many-readers / one-writer):

* Every twin simulation / shadow-eval request acquires the gate **shared**
  (``twin_in_flight`` — a FastAPI dependency attached to the simulation
  router at include time in ``__main__``).  Shared holders never block
  each other; twin throughput is unchanged.
* The city-plan writer acquires the gate **exclusive**
  (``SignalPolicyGate.writer_exclusive``).  Exclusive waits for all
  in-flight shared holders to drain, blocks new shared acquisitions while
  held, and spans the writer's own shadow-evaluation *and* its DB commit —
  so no writer commit can interleave with an in-flight twin read.

Honest scope: the gate is per-process (exactly what C7 prescribes — the
twin is in-process).  A second oracle instance pointed at the same
database is outside the contract; the demo fleet runs exactly one oracle.

Pure asyncio, no I/O — unit-testable without a database.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class SignalPolicyGate:
    """Many-readers / one-writer gate over twin vs writer contention.

    ``shared()`` — async context manager for twin/shadow-eval requests.
    ``writer_exclusive()`` — async context manager for the plan writer;
    spans shadow-evaluation + commit.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._shared_active = 0
        self._writer_active = False
        self._writer_waiters = 0

    # -- shared (twin / shadow-eval) ------------------------------------

    @asynccontextmanager
    async def shared(self) -> AsyncIterator[None]:
        if self._writer_active or self._writer_waiters > 0:
            # A writer holds or is waiting for the gate: twin requests queue
            # behind it via the lock so the writer cannot starve.
            async with self._lock:
                self._shared_active += 1
                try:
                    yield
                finally:
                    self._shared_active -= 1
            return
        self._shared_active += 1
        try:
            yield
        finally:
            self._shared_active -= 1

    # -- exclusive (writer) ----------------------------------------------

    @property
    def writer_active(self) -> bool:
        return self._writer_active

    @property
    def shared_active(self) -> int:
        return self._shared_active

    @asynccontextmanager
    async def writer_exclusive(self) -> AsyncIterator[None]:
        self._writer_waiters += 1
        try:
            async with self._lock:
                self._writer_active = True
                try:
                    yield
                finally:
                    self._writer_active = False
        finally:
            self._writer_waiters -= 1

    def snapshot(self) -> dict:
        """Observability (surfaced via GET /api/v1/signals/plans)."""
        return {
            "writer_active": self._writer_active,
            "writer_waiters": self._writer_waiters,
            "twin_in_flight": self._shared_active,
        }


# Module-level singleton — ONE gate per oracle process.
signal_policy_gate = SignalPolicyGate()


async def twin_in_flight() -> AsyncIterator[None]:
    """FastAPI dependency marking a request as a twin/shadow-eval in flight.

    Attached to the simulation router include in ``__main__``:

        app.include_router(
            simulation_router,
            dependencies=[Depends(twin_in_flight)],
        )

    Generator dependencies hold the shared gate for the full request
    lifecycle (FastAPI runs the teardown after the response is produced).
    """
    async with signal_policy_gate.shared():
        yield


__all__ = ["SignalPolicyGate", "signal_policy_gate", "twin_in_flight"]
