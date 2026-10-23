"""History feed — the foundation rung's 5-minute grid, filled from ingest.

The t0 foundation artifact reads a speed series on a 300 s grid
(HORIZON_TO_STEP: T+5 = one step).  That grid is built here from what the
live data plane already persists — ``traffic.observations`` (written by
ingest/service.py) — aggregated in SQL into 300 s bins keyed by
OBSERVATION time:

  * at start, a backfill of the last ``backfill_hours`` (the window depth),
    so a restarted oracle forecasts as soon as an hour of history exists;
  * every ``interval_sec``, a refresh of the last ``refresh_bins`` bins
    (the open bin keeps maturing until it closes).

Rows flagged ``anomalous`` (Tier-1) never enter a bin; ``synthetic``
(simulated-feed) rows do, with their share recorded per bin so every
forecast can say what it stands on.  Future-stamped rows are ignored.  The
same pass refreshes each segment's free-flow speed (its speed limit),
which the rung uses to derive congestion.  Failures are caught and
surfaced through :meth:`health` — a dark history feed is visible, and the
forecaster simply stays on its next rung.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_INTERVAL_SEC = 60.0
DEFAULT_BACKFILL_HOURS = 8.0
DEFAULT_REFRESH_BINS = 3

# 300 s bins of non-anomalous, non-future observations, with provenance.
_SQL_BINS = "SELECT o.segment_id, floor(extract(epoch FROM o.timestamp_utc) / $2)::bigint AS bin, avg(o.speed_kmh) AS speed, count(*) AS n, count(*) FILTER (WHERE o.data_quality = 'real') AS n_real, count(*) FILTER (WHERE o.data_quality = 'synthetic') AS n_synthetic FROM traffic.observations o WHERE o.timestamp_utc >= $1 AND o.timestamp_utc <= NOW() + INTERVAL '1 minute' AND o.data_quality IS DISTINCT FROM 'anomalous' GROUP BY o.segment_id, bin"  # noqa: E501
_SQL_FREE_FLOW = "SELECT segment_id, speed_limit_kmh FROM corridor.road_segments"  # noqa: E501


class HistoryFeed:
    """Background task feeding a forecaster's time-binned history window."""

    def __init__(
        self,
        pool: Any | None,
        forecaster: Any,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
        backfill_hours: float = DEFAULT_BACKFILL_HOURS,
        refresh_bins: int = DEFAULT_REFRESH_BINS,
    ) -> None:
        self._pool = pool
        self._forecaster = forecaster
        self._window = forecaster.history_window
        self._interval_sec = interval_sec
        self._backfill_hours = backfill_hours
        self._refresh_bins = refresh_bins
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.backfilled = False
        self.bins_loaded = 0
        self.refreshes = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_refresh_monotonic: float | None = None
        self.last_quality: dict[str, int] = {}

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._pool is None or (self._task is not None and not self._task.done()):
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="forecast-history-feed")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.backfilled:
                    await self.load_free_flow()
                    await self.refresh(hours=self._backfill_hours)
                    self.backfilled = True
                else:
                    await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced, never fatal
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("history_feed.refresh_failed", error=self.last_error)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_sec)

    # -- one pass --------------------------------------------------------------

    async def load_free_flow(self) -> int:
        """Per-segment free-flow speeds (speed limits) → the forecaster."""
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            rows = await conn.fetch(_SQL_FREE_FLOW)
        self._forecaster.set_free_flow(
            {
                int(r["segment_id"]): float(r["speed_limit_kmh"])
                for r in rows
                if r["speed_limit_kmh"] is not None
            }
        )
        return len(rows)

    async def refresh(self, hours: float | None = None) -> int:
        """Upsert the bins of the last ``hours`` (default: refresh_bins)."""
        bin_sec = self._window.bin_ms // 1000
        if hours is None:
            span = timedelta(seconds=bin_sec * self._refresh_bins)
        else:
            span = timedelta(hours=hours)
        since = datetime.now(UTC) - span
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            rows = await conn.fetch(_SQL_BINS, since, float(bin_sec))
        quality = {"real": 0, "synthetic": 0}
        for r in rows:
            self._window.set_bin(
                int(r["segment_id"]),
                int(r["bin"]),
                float(r["speed"]),
                int(r["n"]),
                real=int(r["n_real"] or 0),
                synthetic=int(r["n_synthetic"] or 0),
            )
            quality["real"] += int(r["n_real"] or 0)
            quality["synthetic"] += int(r["n_synthetic"] or 0)
        self.bins_loaded += len(rows)
        self.refreshes += 1
        self.last_refresh_monotonic = time.monotonic()
        self.last_quality = quality
        self.last_error = None
        return len(rows)

    def health(self) -> dict:
        now_ms = int(time.time() * 1000)
        return {
            "running": self._task is not None and not self._task.done(),
            "foundation_active": bool(
                getattr(self._forecaster, "foundation_active", False)
            ),
            "backfilled": self.backfilled,
            "interval_sec": self._interval_sec,
            "refreshes": self.refreshes,
            "bins_loaded": self.bins_loaded,
            "last_refresh_age_sec": (
                round(time.monotonic() - self.last_refresh_monotonic, 1)
                if self.last_refresh_monotonic is not None
                else None
            ),
            "last_refresh_rows_quality": dict(self.last_quality),
            "simulated_feed": self.last_quality.get("synthetic", 0) > 0,
            "errors": self.errors,
            "last_error": self.last_error,
            "window": self._window.health(now_ms),
        }


__all__ = ["DEFAULT_INTERVAL_SEC", "HistoryFeed"]
