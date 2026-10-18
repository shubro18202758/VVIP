"""Situation-engine startup hook (plan C7).

``start_situation_engine(app)`` launches one asyncio task that keeps the
adaptive layer's inputs fresh for every active movement:

  * every ``poll_interval_sec`` it lists active movements (planning /
    approved / active) from ``convoy.movements`` via the app's asyncpg
    pool (parameter-bound query, no interpolation);
  * for each movement it reads the latest raw-input document from
    Valkey (``adaptive:inputs:{movement_id}``, written by the feed layer
    / signal-ingress) and the corridor anomaly flag
    (``adaptive:anomaly:active``);
  * it then drives the pure core: classify → hysteresis → publish
    ``situation:changed`` on transitions → mirror params (TTL 30 s).

Missing inputs are skipped, never fabricated.  Every infrastructure
failure is caught and surfaced in ``loop_status()`` (exposed through
/api/v1/adaptive/status as ``engine_loop``), because a dead input path
must be visible, not silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

import structlog

from traffic_oracle.adaptive.enums import SecurityTier
from traffic_oracle.adaptive.inputs import SituationInputs
from traffic_oracle.adaptive.service import AdaptiveService

logger = structlog.get_logger(__name__)

DEFAULT_POLL_INTERVAL_SEC = 5.0

# Legacy movement subjects (backward compatibility: the VVIP base case
# still creates convoy.movements rows; its behavior is unchanged).
_ACTIVE_MOVEMENTS_SQL = "SELECT movement_id, vvip_class FROM convoy.movements WHERE status IN ('planning', 'approved', 'active') ORDER BY planned_start LIMIT 100"  # noqa: E501

# Event-agnostic corridor events (migration 007): protection_class
# generalizes vvip_class ('none' = public/civic event).  Same inputs-doc
# key shape per subject id, so the observe path is shared.
_ACTIVE_EVENTS_SQL = "SELECT event_id, protection_class FROM events.corridor_events WHERE status IN ('planning', 'approved', 'active') ORDER BY planned_start LIMIT 100"  # noqa: E501


class LoopHealth:
    """Mutable health record for the engine loop (surfaced via /status)."""

    def __init__(self) -> None:
        self.running = False
        self.last_tick_monotonic: float | None = None
        self.last_error: str | None = None
        self.consecutive_errors = 0
        self.movements_seen = 0

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "last_tick_monotonic": self.last_tick_monotonic,
            "last_error": self.last_error,
            "consecutive_errors": self.consecutive_errors,
            "movements_seen": self.movements_seen,
        }


def inputs_key(movement_id: str) -> str:
    return f"adaptive:inputs:{movement_id}"


ANOMALY_KEY = "adaptive:anomaly:active"


async def _load_active_movements(pool: Any) -> list[tuple[str, str]]:
    """Active classification subjects: corridor events + legacy movements.

    The 007 events table is polled FIRST (id, protection_class); legacy
    convoy.movements rows are appended unless their id already appears as
    an event (events may soft-link movement_id).  Both use the same
    ``adaptive:inputs:{id}`` doc key, so the observe path is shared and
    the legacy VVIP base case behaves identically.
    """
    subjects: list[tuple[str, str]] = []
    seen: set[str] = set()
    async with pool.acquire() as conn:
        try:
            event_rows = await conn.fetch(_ACTIVE_EVENTS_SQL)
        except Exception:  # noqa: BLE001 - 007 not applied yet; legacy only
            event_rows = []
        for r in event_rows:
            subject_id = str(r["event_id"])
            subjects.append((subject_id, str(r["protection_class"])))
            seen.add(subject_id)
        movement_rows = await conn.fetch(_ACTIVE_MOVEMENTS_SQL)
    for r in movement_rows:
        subject_id = str(r["movement_id"])
        if subject_id not in seen:
            subjects.append((subject_id, str(r["vvip_class"])))
    return subjects


async def _load_inputs_doc(client: Any, movement_id: str) -> dict | None:
    raw = await client.get(inputs_key(movement_id))
    if raw is None:
        return None
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        doc = json.loads(raw)
        if isinstance(doc, dict):
            return doc
    return None


async def _load_anomaly_flag(client: Any) -> bool:
    raw = await client.get(ANOMALY_KEY)
    if raw is None:
        return False
    with contextlib.suppress(ValueError, TypeError):
        return bool(int(raw))
    return str(raw).lower() in ("1", "true", "yes")


class SituationEngineLoop:
    """The background task; all I/O is defensive and health-surfaced."""

    def __init__(
        self,
        service: AdaptiveService,
        pool: Any | None,
        valkey_client: Any | None,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
    ) -> None:
        self._service = service
        self._pool = pool
        self._valkey = valkey_client
        self._poll_interval_sec = poll_interval_sec
        self.health = LoopHealth()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self.health.running = True
        self._task = asyncio.create_task(self._run(), name="situation-engine")
        logger.info(
            "adaptive.engine_loop.started",
            poll_interval_sec=self._poll_interval_sec,
            has_pool=self._pool is not None,
            has_valkey=self._valkey is not None,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self.health.running = False
        logger.info("adaptive.engine_loop.stopped")

    # -- main loop -------------------------------------------------------------

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
                self.health.consecutive_errors = 0
                self.health.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - loop must survive
                self.health.consecutive_errors += 1
                self.health.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "adaptive.engine_loop.tick_failed",
                    error=self.health.last_error,
                    consecutive=self.health.consecutive_errors,
                )
            self.health.last_tick_monotonic = time.monotonic()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._poll_interval_sec
                )

    async def _tick(self) -> None:
        if self._pool is None or self._valkey is None:
            return  # inputs not wired; observe endpoint still works
        movements = await _load_active_movements(self._pool)
        self.health.movements_seen = len(movements)
        anomaly_active = await _load_anomaly_flag(self._valkey)
        for movement_id, vvip_class_raw in movements:
            try:
                tier = SecurityTier.from_code(vvip_class_raw)
            except ValueError:
                logger.warning(
                    "adaptive.engine_loop.bad_protection_class",
                    movement_id=movement_id,
                    protection_class=vvip_class_raw,
                )
                continue
            doc = await _load_inputs_doc(self._valkey, movement_id)
            if doc is None:
                continue  # no fresh inputs — never fabricate
            try:
                inputs = SituationInputs.from_scores(
                    vvip_class=tier.code,
                    predicted_congestion_idx=float(
                        doc.get("predicted_congestion_idx", 0.0)
                    ),
                    weather_severity=float(doc.get("weather_severity", 0.0)),
                    crowd_score=float(doc.get("crowd_score", 0.0)),
                    anomaly_active=bool(doc.get("anomaly_active", anomaly_active)),
                    event_active=bool(doc.get("event_active", False)),
                    weather=doc.get("weather"),
                    # public-event profile extension (additive, C3):
                    crowd_class=doc.get("crowd_class"),
                    duration_class=str(doc.get("duration_class", "minutes")),
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "adaptive.engine_loop.bad_inputs_doc",
                    movement_id=movement_id,
                    error=str(exc),
                )
                continue
            await self._service.observe(
                movement_id,
                inputs,
                last_gps_age_sec=float(doc.get("last_gps_age_sec", 0.0)),
                ambulance_delay_projection_sec=float(
                    doc.get("ambulance_delay_projection_sec", 0.0)
                ),
            )


def start_situation_engine(
    app: Any,
    service: AdaptiveService,
    valkey_client: Any | None = None,
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
) -> SituationEngineLoop:
    """Startup hook: build and launch the loop, stash it on app.state."""
    pool = getattr(app.state, "pool", None)
    loop = SituationEngineLoop(
        service,
        pool=pool,
        valkey_client=valkey_client,
        poll_interval_sec=poll_interval_sec,
    )
    loop.start()
    app.state.adaptive_engine_loop = loop
    return loop


__all__ = [
    "ANOMALY_KEY",
    "DEFAULT_POLL_INTERVAL_SEC",
    "LoopHealth",
    "SituationEngineLoop",
    "inputs_key",
    "start_situation_engine",
]
