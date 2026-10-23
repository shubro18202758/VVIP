"""Situation-engine startup hook (plan C7).

``start_situation_engine(app)`` launches one asyncio task that keeps the
adaptive layer's inputs fresh for every active subject:

  * every ``poll_interval_sec`` it lists the active subjects — corridor
    events (planning / approved / active) with their CURRENT route spines
    (``events.event_routes``; a spine superseded by a route update,
    migration 018, is skipped — the next tick reads the new primary), plus
    legacy ``convoy.movements`` with their ranked ``convoy.planned_routes``
    — via the app's asyncpg pool (parameter-bound queries, no
    interpolation);
  * it EVICTS every adaptive state that is no longer in that set, so a
    completed or cancelled event stops holding the city band at once;
  * per subject it resolves inputs, in this order:
      1. ``adaptive:inputs:{id}`` — a feed / emulator document (labelled
         ``source='emulator'`` when the emulator's provenance sidecar
         ``emulator:source:{id}`` exists, else ``'inputs_doc'``);
      2. otherwise the LIVE CACHE (``source='live_cache'``): the
         length-weighted congestion of the latest ``traffic:latest:{sid}``
         observation over the subject's route spine (the ``primary`` spine
         when one is labelled so, else every spine), using the
         length-weighted p90 under the robust flag; weather and crowd come
         from the event row (``metadata.weather`` / ``weather_severity``,
         ``crowd_class``) and are labelled ``none`` when the row carries
         none.  Only fresh (<= 300 s), non-anomalous, non-future rows
         count; ``synthetic`` (simulated-feed) rows are usable and their
         provenance rides the audit row.  No fresh row on the spine —
         the subject is SKIPPED, never fabricated.
         SAME-PROVENANCE INPUTS: the event-emulator's observations live
         in their own key space (``traffic:emulated:latest:{sid}``,
         ingest/cache_writer.py).  A LIVE event reads ``traffic:latest``
         only — emulated traffic can never drive a real event's band; an
         EMULATED event (metadata source event-emulator / living city)
         prefers its scenario's emulated row per segment and falls back to
         the live one.  The audit row says which (``inputs_origin`` live |
         emulated | mixed, ``emulated_segments``) and ``simulated_feed``
         is true whenever an emulated or synthetic row stood behind it.
  * it then drives the pure core: classify → hysteresis → publish
    ``situation:changed`` on transitions → decide C4 params with a twin
    ShadowContext built from the spine and ``planned_start`` → persist and
    mirror only when the decision changed (adaptive/service.py).

A subject whose inputs have been missing for ``STALE_INPUTS_SEC`` is
evicted too (its band would be old news) — also while the subject poll
itself is failing, so a database outage never freezes a stale band (and
the city band it drives) in place.  A tick that HANGS (a database that
stops answering instead of refusing) is abandoned after
``TICK_TIMEOUT_SEC`` and counts as a failed poll; while polls fail the
city band is marked stale (unknown, not at rest).  Every infrastructure
failure is caught and surfaced in ``LoopHealth.snapshot()`` (exposed
through /api/v1/adaptive/status as ``engine_loop``) together with per-subject
``last_inputs_age_sec`` and ``skipped_no_inputs`` — a dead input path must
be visible, not silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from traffic_oracle.adaptive.enums import SecurityTier, WeatherCondition
from traffic_oracle.adaptive.inputs import SituationInputs
from traffic_oracle.adaptive.protocol_params_gen import BASELINE_ROBUST_ROUTING
from traffic_oracle.adaptive.service import AdaptiveService
from traffic_oracle.adaptive.twin import ShadowContext
from traffic_oracle.city.common import event_source, routes_supersede_supported
from traffic_oracle.ingest.cache_writer import emulated_latest_key

logger = structlog.get_logger(__name__)

DEFAULT_POLL_INTERVAL_SEC = 5.0

#: traffic:latest rows older than this never drive a band (the cache TTL).
LIVE_MAX_AGE_SEC = 300.0
#: Rows stamped further than this in the future are refused (clock skew).
LIVE_MAX_FUTURE_SEC = 60.0
#: A tracked subject with no usable inputs for this long is evicted.
STALE_INPUTS_SEC = 300.0
#: A tick still running after this long is abandoned and counted as a
#: failed poll — a hung database (paused host, silent network partition)
#: must not freeze the loop and the bands it holds.  Normal ticks take
#: milliseconds; twin shadow-evals are ~0.2 s per subject.
TICK_TIMEOUT_SEC = 30.0
#: Segment-length table refresh (corridor.road_segments rarely changes).
LENGTHS_REFRESH_SEC = 600.0
#: Live departures (the event is already under way) are floored to this
#: bucket so the twin shadow verdict's cache key stays stable.
DEPARTURE_BUCKET_MIN = 15

#: ``VVIP_ADAPTIVE_ROBUST_INPUTS`` = 1/0 overrides the robust flag; unset
#: follows the canonical robust knob (protocol params, ``enabled``).
ROBUST_INPUTS_ENV = "VVIP_ADAPTIVE_ROBUST_INPUTS"
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

# Corridor events (migration 007) — protection_class generalizes
# vvip_class ('none' = public/civic event); crowd_class + metadata feed the
# live-cache inputs.
_ACTIVE_EVENTS_SQL = "SELECT e.event_id, e.protection_class, e.crowd_class, e.status, e.event_kind, e.planned_start, e.movement_id, e.metadata FROM events.corridor_events e WHERE e.status IN ('planning', 'approved', 'active') ORDER BY e.planned_start NULLS LAST LIMIT 100"  # noqa: E501
_EVENT_SPINES_SQL = "SELECT r.event_id, r.label, r.segment_ids FROM events.event_routes r WHERE r.event_id = ANY($1::uuid[]) ORDER BY r.event_id, r.route_rank"  # noqa: E501
#: Migration 018 (probed): current spines only — a re-routed event's next
#: tick reads its new primary, never the superseded one.
_EVENT_SPINES_CURRENT_SQL = "SELECT r.event_id, r.label, r.segment_ids FROM events.event_routes r WHERE r.event_id = ANY($1::uuid[]) AND r.superseded_at IS NULL ORDER BY r.event_id, r.route_rank, r.created_at DESC"  # noqa: E501

# Legacy movement subjects (backward compatibility: the VVIP base case
# still creates convoy.movements rows; its behavior is unchanged).
_ACTIVE_MOVEMENTS_SQL = "SELECT movement_id, vvip_class, status, planned_start FROM convoy.movements WHERE status IN ('planning', 'approved', 'active') ORDER BY planned_start LIMIT 100"  # noqa: E501
_MOVEMENT_ROUTES_SQL = "SELECT r.movement_id, r.route_rank, r.segment_ids FROM convoy.planned_routes r WHERE r.movement_id = ANY($1::uuid[]) ORDER BY r.movement_id, r.route_rank"  # noqa: E501

_SEGMENT_LENGTHS_SQL = "SELECT segment_id, ST_Length(geom::geography) AS length_m FROM corridor.road_segments"  # noqa: E501


def robust_inputs_enabled(env: dict[str, str] | None = None) -> bool:
    """Robust (length-weighted p90) congestion aggregation on/off."""
    raw = (env if env is not None else os.environ).get(ROBUST_INPUTS_ENV, "")
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return bool(BASELINE_ROBUST_ROUTING.get("enabled", True))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@dataclass
class SubjectHealth:
    """Per-subject input health (surfaced via /adaptive/status)."""

    subject_id: str
    kind: str
    protection_class: str
    status: str | None = None
    inputs_source: str | None = None
    last_inputs_monotonic: float | None = None
    skipped_no_inputs: int = 0
    consecutive_skips: int = 0
    last_skip_reason: str | None = None
    obs_age_max_sec: float | None = None
    coverage: float | None = None
    last_error: str | None = None

    def snapshot(self, now: float) -> dict:
        return {
            "kind": self.kind,
            "protection_class": self.protection_class,
            "status": self.status,
            "inputs_source": self.inputs_source,
            "last_inputs_age_sec": (
                round(now - self.last_inputs_monotonic, 1)
                if self.last_inputs_monotonic is not None
                else None
            ),
            "skipped_no_inputs": self.skipped_no_inputs,
            "consecutive_skips": self.consecutive_skips,
            "last_skip_reason": self.last_skip_reason,
            "obs_age_max_sec": self.obs_age_max_sec,
            "coverage": self.coverage,
            "last_error": self.last_error,
        }


class LoopHealth:
    """Mutable health record for the engine loop (surfaced via /status)."""

    def __init__(self, poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC) -> None:
        self.running = False
        self.last_tick_monotonic: float | None = None
        self.last_error: str | None = None
        self.consecutive_errors = 0
        self.movements_seen = 0
        self.poll_interval_sec = poll_interval_sec
        self.ticks = 0
        self.last_tick_duration_ms: float | None = None
        self.subjects_observed = 0
        self.subjects_skipped = 0
        self.evicted_last_tick: list[str] = []
        self.evicted_total = 0
        self.robust_inputs = robust_inputs_enabled()
        self.subjects: dict[str, SubjectHealth] = {}

    def snapshot(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        return {
            "running": self.running,
            "last_tick_monotonic": self.last_tick_monotonic,
            "last_tick_age_sec": (
                round(now - self.last_tick_monotonic, 1)
                if self.last_tick_monotonic is not None
                else None
            ),
            "last_tick_duration_ms": self.last_tick_duration_ms,
            "poll_interval_sec": self.poll_interval_sec,
            "ticks": self.ticks,
            "last_error": self.last_error,
            "consecutive_errors": self.consecutive_errors,
            "movements_seen": self.movements_seen,
            "subjects_active": len(self.subjects),
            "subjects_observed": self.subjects_observed,
            "subjects_skipped": self.subjects_skipped,
            "evicted_last_tick": list(self.evicted_last_tick),
            "evicted_total": self.evicted_total,
            "robust_inputs": self.robust_inputs,
            "live_max_age_sec": LIVE_MAX_AGE_SEC,
            "stale_inputs_sec": STALE_INPUTS_SEC,
            "subjects": {
                sid: h.snapshot(now) for sid, h in sorted(self.subjects.items())
            },
        }


def inputs_key(movement_id: str) -> str:
    return f"adaptive:inputs:{movement_id}"


def emulator_source_key(movement_id: str) -> str:
    """The emulator's provenance sidecar (event_emulator/valkey_seam.py)."""
    return f"emulator:source:{movement_id}"


def latest_key(segment_id: int) -> str:
    return f"traffic:latest:{segment_id}"


ANOMALY_KEY = "adaptive:anomaly:active"


# ---------------------------------------------------------------------------
# Subjects
# ---------------------------------------------------------------------------


@dataclass
class Subject:
    """One active classification subject (event or legacy movement)."""

    subject_id: str
    protection_class: str
    kind: str = "event"                     # 'event' | 'movement'
    status: str | None = None
    event_kind: str | None = None
    crowd_class: str | None = None
    planned_start: datetime | None = None
    metadata: dict = field(default_factory=dict)
    movement_ref: str | None = None
    spines: list[tuple[str, list[int]]] = field(default_factory=list)

    @property
    def emulated(self) -> bool:
        """An emulator-created event (C-C provenance of its metadata) — the
        only kind of subject emulated observations may drive."""
        return self.kind == "event" and event_source(self.metadata) == "emulated"

    def spine_segments(self) -> tuple[str, list[int]]:
        """(basis, segment ids): the primary spine when one is labelled so,
        else the de-duplicated union of every spine in rank order."""
        for label, segs in self.spines:
            if label == "primary" and segs:
                return "primary", list(dict.fromkeys(segs))
        union: list[int] = []
        for _label, segs in self.spines:
            union.extend(segs)
        return "all_spines", list(dict.fromkeys(union))


def _as_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        with contextlib.suppress(ValueError, TypeError):
            value = json.loads(raw)
            if isinstance(value, dict):
                return value
    return {}


def _int_list(raw: Any) -> list[int]:
    out: list[int] = []
    for v in raw or ():
        with contextlib.suppress(TypeError, ValueError):
            out.append(int(v))
    return out


async def _load_active_subjects(pool: Any) -> list[Subject]:
    """Active classification subjects: corridor events + legacy movements.

    The 007 events table is polled FIRST (with route spines); legacy
    convoy.movements rows are appended unless their id already appears as
    an event or an event soft-links them.  Both use the same
    ``adaptive:inputs:{id}`` doc key, so the observe path is shared and
    the legacy VVIP base case behaves identically.
    """
    subjects: list[Subject] = []
    seen: set[str] = set()
    async with pool.acquire() as conn:
        try:
            event_rows = await conn.fetch(_ACTIVE_EVENTS_SQL)
        except Exception as exc:  # noqa: BLE001 - 007 not applied yet; legacy only
            logger.debug("adaptive.engine_loop.events_unavailable", error=repr(exc))
            event_rows = []
        by_event: dict[str, Subject] = {}
        for r in event_rows:
            sid = str(r["event_id"])
            metadata = _as_dict(r.get("metadata"))
            movement_ref = metadata.get("movement_ref") or (
                str(r["movement_id"]) if r.get("movement_id") else None
            )
            subject = Subject(
                subject_id=sid,
                protection_class=str(r["protection_class"]),
                kind="event",
                status=r.get("status"),
                event_kind=r.get("event_kind"),
                crowd_class=r.get("crowd_class"),
                planned_start=r.get("planned_start"),
                metadata=metadata,
                movement_ref=str(movement_ref) if movement_ref else None,
            )
            subjects.append(subject)
            by_event[sid] = subject
            seen.add(sid)
            if r.get("movement_id"):
                seen.add(str(r["movement_id"]))
        if by_event:
            try:
                spines_sql = (
                    _EVENT_SPINES_CURRENT_SQL
                    if await routes_supersede_supported(conn)
                    else _EVENT_SPINES_SQL
                )
                spine_rows = await conn.fetch(
                    spines_sql, [uuid.UUID(k) for k in by_event]
                )
            except Exception as exc:  # noqa: BLE001 - spines are best-effort
                logger.warning("adaptive.engine_loop.spines_unavailable", error=repr(exc))
                spine_rows = []
            for r in spine_rows:
                subject = by_event.get(str(r["event_id"]))
                if subject is not None:
                    subject.spines.append(
                        (str(r["label"] or "spine"), _int_list(r["segment_ids"]))
                    )

        movement_rows = await conn.fetch(_ACTIVE_MOVEMENTS_SQL)
        legacy: dict[str, Subject] = {}
        for r in movement_rows:
            sid = str(r["movement_id"])
            if sid in seen:
                continue
            subject = Subject(
                subject_id=sid,
                protection_class=str(r["vvip_class"]),
                kind="movement",
                status=r.get("status"),
                planned_start=r.get("planned_start"),
                movement_ref=sid,
            )
            subjects.append(subject)
            legacy[sid] = subject
        if legacy:
            ids: list[uuid.UUID] = []
            for k in legacy:
                with contextlib.suppress(ValueError):
                    ids.append(uuid.UUID(k))
            if ids:
                try:
                    route_rows = await conn.fetch(_MOVEMENT_ROUTES_SQL, ids)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "adaptive.engine_loop.planned_routes_unavailable",
                        error=repr(exc),
                    )
                    route_rows = []
                for r in route_rows:
                    subject = legacy.get(str(r["movement_id"]))
                    if subject is not None:
                        label = "primary" if int(r["route_rank"] or 1) == 1 else (
                            f"alt-{int(r['route_rank']) - 1}"
                        )
                        subject.spines.append((label, _int_list(r["segment_ids"])))
    return subjects


async def _load_active_movements(pool: Any) -> list[tuple[str, str]]:
    """(id, protection class) pairs — the pre-spine view, kept for callers."""
    return [(s.subject_id, s.protection_class) for s in await _load_active_subjects(pool)]


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


async def _mget(client: Any, keys: list[str]) -> list[Any]:
    if not keys:
        return []
    mget = getattr(client, "mget", None)
    if mget is not None:
        return list(await mget(keys))
    return [await client.get(k) for k in keys]


# ---------------------------------------------------------------------------
# Live-cache aggregation (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveCongestion:
    """Spine congestion derived from the live cache (pure result)."""

    mean: float
    p90: float
    segments_used: int
    segments_total: int
    coverage: float
    obs_age_max_sec: float
    obs_age_min_sec: float
    quality: dict[str, int]
    anomalous_excluded: int
    stale_excluded: int


def weighted_quantile(values: list[tuple[float, float]], q: float) -> float:
    """Length-weighted quantile of (value, weight) pairs (lower interpolation).

    The smallest value whose cumulative weight reaches ``q`` of the total —
    for q=0.9 over a spine: the congestion that at least 90 % of the spine
    length is at or below.
    """
    ordered = sorted(values)
    total = sum(w for _v, w in ordered)
    if total <= 0:
        return ordered[-1][0]
    target = q * total
    cum = 0.0
    for value, weight in ordered:
        cum += weight
        if cum >= target - 1e-9:
            return value
    return ordered[-1][0]


def aggregate_live_congestion(
    segment_ids: list[int],
    raw_rows: list[Any],
    lengths: dict[int, float],
    now_ms: int,
    max_age_sec: float = LIVE_MAX_AGE_SEC,
    max_future_sec: float = LIVE_MAX_FUTURE_SEC,
) -> LiveCongestion | None:
    """Length-weighted spine congestion from ``traffic:latest`` rows.

    ``raw_rows[i]`` is the JSON (or None) cached for ``segment_ids[i]``.
    Usable rows: parseable, not ``anomalous``, timestamp within
    ``max_age_sec`` and at most ``max_future_sec`` ahead, finite
    congestion in [0, 1].  Returns None when no row is usable — the
    caller skips the subject (never a fabricated band).
    """
    weighted: list[tuple[float, float]] = []
    quality: Counter[str] = Counter()
    ages: list[float] = []
    anomalous = stale = 0
    total_len = 0.0
    used_len = 0.0
    for sid, raw in zip(segment_ids, raw_rows, strict=False):
        length = float(lengths.get(sid) or 0.0)
        if length <= 0:
            length = 500.0  # unknown length: neutral weight (labelled by coverage)
        total_len += length
        if raw is None:
            continue
        try:
            obs = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else dict(raw)
            ts = int(obs["timestamp_ms"])
            cong = float(obs["congestion_idx"])
            dq = str(obs.get("data_quality") or "real")
        except (KeyError, TypeError, ValueError):
            continue
        if dq == "anomalous":
            anomalous += 1
            continue
        age = (now_ms - ts) / 1000.0
        if age > max_age_sec or age < -max_future_sec:
            stale += 1
            continue
        if not math.isfinite(cong) or cong < 0.0 or cong > 1.0:
            continue
        weighted.append((cong, length))
        used_len += length
        quality[dq] += 1
        ages.append(max(0.0, age))
    if not weighted:
        return None
    mean = sum(c * w for c, w in weighted) / sum(w for _c, w in weighted)
    return LiveCongestion(
        mean=mean,
        p90=weighted_quantile(weighted, 0.9),
        segments_used=len(weighted),
        segments_total=len(segment_ids),
        coverage=(used_len / total_len) if total_len > 0 else 0.0,
        obs_age_max_sec=max(ages),
        obs_age_min_sec=min(ages),
        quality=dict(quality),
        anomalous_excluded=anomalous,
        stale_excluded=stale,
    )


def _fresh_row(raw: Any, now_ms: int) -> bool:
    """A cached row whose timestamp is inside the live window (the same
    bounds aggregate_live_congestion applies); False when absent/corrupt."""
    if raw is None:
        return False
    try:
        obs = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else dict(raw)
        age = (now_ms - int(obs["timestamp_ms"])) / 1000.0
    except (KeyError, TypeError, ValueError):
        return False
    return -LIVE_MAX_FUTURE_SEC <= age <= LIVE_MAX_AGE_SEC


def _quality_label(quality: dict[str, int]) -> str:
    kinds = {k for k, n in quality.items() if n > 0}
    if not kinds:
        return "none"
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed"


def _weather_from_metadata(metadata: dict) -> tuple[str | None, float, str]:
    """(weather code, severity, source) from an event row's metadata."""
    code = metadata.get("weather")
    if isinstance(code, str):
        with contextlib.suppress(ValueError):
            return WeatherCondition.from_code(code).code, 0.0, "event_metadata"
    severity = metadata.get("weather_severity")
    if isinstance(severity, (int, float)) and not isinstance(severity, bool):
        sev = float(severity)
        if math.isfinite(sev):
            return None, min(1.0, max(0.0, sev)), "event_metadata"
    return None, 0.0, "none"


def _departure_for(subject: Subject, now: datetime) -> str:
    """Twin departure: ``planned_start`` while it is ahead, else now
    (floored to the bucket — the movement is under way)."""
    start = subject.planned_start
    if isinstance(start, datetime):
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if start > now:
            return start.isoformat()
    floored = now.replace(second=0, microsecond=0)
    floored -= timedelta(minutes=floored.minute % DEPARTURE_BUCKET_MIN)
    return floored.isoformat()


class SituationEngineLoop:
    """The background task; all I/O is defensive and health-surfaced."""

    def __init__(
        self,
        service: AdaptiveService,
        pool: Any | None,
        valkey_client: Any | None,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        robust_inputs: bool | None = None,
        tick_timeout_sec: float = TICK_TIMEOUT_SEC,
    ) -> None:
        self._service = service
        self._pool = pool
        self._valkey = valkey_client
        self._poll_interval_sec = poll_interval_sec
        self._tick_timeout_sec = tick_timeout_sec
        self.health = LoopHealth(poll_interval_sec)
        if robust_inputs is not None:
            self.health.robust_inputs = robust_inputs
        self._task: asyncio.Task | None = None
        # the tick in flight (its own task, so an overdue one can be
        # abandoned without awaiting a cleanup a hung database never ends)
        self._tick_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lengths: dict[int, float] = {}
        self._lengths_loaded_monotonic: float | None = None

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
            robust_inputs=self.health.robust_inputs,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        tick = self._tick_task
        if tick is not None and not tick.done():
            tick.cancel()
            # bounded: an abandoned tick on a hung database may never unwind
            await asyncio.wait({tick}, timeout=1.0)
        self.health.running = False
        logger.info("adaptive.engine_loop.stopped")

    # -- main loop -------------------------------------------------------------

    async def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            error = await self._tick_bounded()
            if error is None:
                self.health.consecutive_errors = 0
                self.health.last_error = None
                self._service.note_poll(ok=True)
            else:
                self.health.consecutive_errors += 1
                self.health.last_error = error
                logger.warning(
                    "adaptive.engine_loop.tick_failed",
                    error=self.health.last_error,
                    consecutive=self.health.consecutive_errors,
                )
                # The city band is unknown while the poll fails (it reads
                # stale, never "at rest" once the bands below are retired).
                self._service.note_poll(ok=False)
                # No fresh inputs this tick either: retire bands gone stale.
                stale = self._sweep_stale()
                self.health.evicted_last_tick = stale
                self.health.evicted_total += len(stale)
            self.health.ticks += 1
            self.health.last_tick_monotonic = time.monotonic()
            self.health.last_tick_duration_ms = round(
                (self.health.last_tick_monotonic - started) * 1000.0, 1
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._poll_interval_sec
                )

    async def _tick_bounded(self) -> str | None:
        """Run one tick as its own task, bounded by ``tick_timeout_sec``.

        Returns None on success, else the failure text.  An overdue tick is
        cancelled but NOT awaited: cancelling an in-flight asyncpg query
        talks to the same server (cancel request, connection reset), so on
        a hung database ``asyncio.wait_for`` would block until the database
        came back — the very freeze the bound exists to prevent.  While the
        abandoned tick is still unwinding no new tick starts (each interval
        counts as another failed poll), so ticks never overlap.
        """
        previous = self._tick_task
        if previous is not None and not previous.done():
            return (
                "TimeoutError: previous tick still unwinding "
                "(database not answering)"
            )
        tick = asyncio.create_task(self._tick(), name="situation-engine-tick")
        tick.add_done_callback(_consume_outcome)
        self._tick_task = tick
        done, _ = await asyncio.wait({tick}, timeout=self._tick_timeout_sec)
        if not done:
            tick.cancel()
            return f"TimeoutError: tick abandoned after {self._tick_timeout_sec:g} s"
        if tick.cancelled():
            return "CancelledError: tick cancelled"
        exc = tick.exception()
        if exc is not None:
            return f"{type(exc).__name__}: {exc}"
        return None

    async def _segment_lengths(self) -> dict[int, float]:
        now = time.monotonic()
        if (
            self._lengths_loaded_monotonic is not None
            and now - self._lengths_loaded_monotonic < LENGTHS_REFRESH_SEC
        ):
            return self._lengths
        try:
            async with self._pool.acquire() as conn:  # type: ignore[union-attr]
                rows = await conn.fetch(_SEGMENT_LENGTHS_SQL)
            self._lengths = {
                int(r["segment_id"]): float(r["length_m"] or 0.0) for r in rows
            }
            self._lengths_loaded_monotonic = now
        except Exception as exc:  # noqa: BLE001 - lengths only weight the mean
            logger.warning("adaptive.engine_loop.lengths_unavailable", error=repr(exc))
        return self._lengths

    async def _tick(self) -> None:
        if self._pool is None or self._valkey is None:
            return  # inputs not wired; observe endpoint still works
        subjects = await _load_active_subjects(self._pool)
        self.health.movements_seen = len(subjects)
        active_ids = {s.subject_id for s in subjects}

        # Subject hygiene: nothing outside the active set keeps a band.
        evicted = self._service.retain_only(active_ids, reason="not_active")
        self.health.subjects = {
            sid: h for sid, h in self.health.subjects.items() if sid in active_ids
        }
        # A reader asking by movement id (brain slug / soft link) finds the
        # event subject that carries it.
        self._service.set_aliases(
            {
                s.movement_ref: s.subject_id
                for s in subjects
                if s.movement_ref and s.movement_ref != s.subject_id
            }
        )

        anomaly_active = await _load_anomaly_flag(self._valkey)
        lengths = await self._segment_lengths()
        observed = skipped = 0
        for subject in subjects:
            health = self.health.subjects.get(subject.subject_id)
            if health is None:
                health = SubjectHealth(
                    subject_id=subject.subject_id,
                    kind=subject.kind,
                    protection_class=subject.protection_class,
                )
                self.health.subjects[subject.subject_id] = health
            health.status = subject.status
            try:
                done = await self._observe_subject(
                    subject, health, anomaly_active, lengths
                )
                health.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one subject never stops the tick
                done = False
                health.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "adaptive.engine_loop.subject_failed",
                    subject=subject.subject_id,
                    error=health.last_error,
                )
            if done:
                observed += 1
            else:
                skipped += 1
        # A band whose inputs vanished goes stale, then is evicted.
        evicted.extend(self._sweep_stale())
        self.health.subjects_observed = observed
        self.health.subjects_skipped = skipped
        self.health.evicted_last_tick = evicted
        self.health.evicted_total += len(evicted)

    def _sweep_stale(self) -> list[str]:
        """Evict every tracked subject whose last inputs are older than
        ``STALE_INPUTS_SEC``.

        Runs at the end of every tick AND after a failed one: a dead subject
        poll (database outage) must not freeze an old band — and the city
        band it drives — in place.
        """
        now = self._service.now_wall()
        evicted: list[str] = []
        for sid in self._service.tracked_ids():
            state = self._service.movement_state(sid)
            seen = state.last_observed_at if state is not None else None
            if (
                seen is not None
                and (now - seen).total_seconds() >= STALE_INPUTS_SEC
                and self._service.evict(sid, reason="inputs_stale")
            ):
                evicted.append(sid)
        return evicted

    async def _observe_subject(
        self,
        subject: Subject,
        health: SubjectHealth,
        anomaly_active: bool,
        lengths: dict[int, float],
    ) -> bool:
        """Resolve inputs, observe, decide params.  False = skipped."""
        try:
            tier = SecurityTier.from_code(subject.protection_class)
        except ValueError:
            logger.warning(
                "adaptive.engine_loop.bad_protection_class",
                movement_id=subject.subject_id,
                protection_class=subject.protection_class,
            )
            self._skip(health, "bad_protection_class")
            return False

        doc = await _load_inputs_doc(self._valkey, subject.subject_id)
        if doc is not None:
            built = await self._inputs_from_doc(subject, tier, doc, anomaly_active)
        else:
            built = await self._inputs_from_live_cache(
                subject, tier, anomaly_active, lengths
            )
        if isinstance(built, str):
            self._skip(health, built)
            return False
        inputs, provenance, gps_age, ambulance = built

        await self._service.observe(
            subject.subject_id,
            inputs,
            last_gps_age_sec=gps_age,
            ambulance_delay_projection_sec=ambulance,
            provenance=provenance,
        )
        health.inputs_source = str(provenance.get("source"))
        health.last_inputs_monotonic = time.monotonic()
        health.consecutive_skips = 0
        health.obs_age_max_sec = provenance.get("obs_age_max_sec")
        health.coverage = provenance.get("coverage")

        _basis, route = subject.spine_segments()
        ctx: ShadowContext | None = None
        if route:
            ctx = ShadowContext(
                route_segment_ids=tuple(route[:64]),
                departure_time=_departure_for(subject, self._service.now_wall()),
            )
        await self._service.params_for(subject.subject_id, tier, shadow_context=ctx)
        return True

    def _skip(self, health: SubjectHealth, reason: str) -> None:
        health.skipped_no_inputs += 1
        health.consecutive_skips += 1
        health.last_skip_reason = reason

    async def _inputs_from_doc(
        self,
        subject: Subject,
        tier: SecurityTier,
        doc: dict,
        anomaly_active: bool,
    ) -> tuple[SituationInputs, dict, float, float] | str:
        try:
            inputs = SituationInputs.from_scores(
                vvip_class=tier.code,
                predicted_congestion_idx=float(doc.get("predicted_congestion_idx", 0.0)),
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
                movement_id=subject.subject_id,
                error=str(exc),
            )
            return "bad_inputs_doc"
        provenance: dict = {"source": "inputs_doc"}
        with contextlib.suppress(Exception):
            sidecar = await self._valkey.get(  # type: ignore[union-attr]
                emulator_source_key(subject.subject_id)
            )
            side = _as_dict(sidecar)
            if side:
                provenance = {
                    "source": "emulator",
                    "emulator_run_id": side.get("run_id"),
                    "emulator_use_case": side.get("use_case_id"),
                    "emulator_replay": bool(side.get("replay", False)),
                    # emulated inputs are never field data (EMULATED label)
                    "simulated_feed": True,
                    "data_quality": "synthetic",
                    "inputs_origin": "emulated",
                }
        provenance["predicted_congestion_idx"] = round(
            float(doc.get("predicted_congestion_idx", 0.0)), 4
        )
        return (
            inputs,
            provenance,
            float(doc.get("last_gps_age_sec", 0.0)),
            float(doc.get("ambulance_delay_projection_sec", 0.0)),
        )

    async def _inputs_from_live_cache(
        self,
        subject: Subject,
        tier: SecurityTier,
        anomaly_active: bool,
        lengths: dict[int, float],
    ) -> tuple[SituationInputs, dict, float, float] | str:
        basis, route = subject.spine_segments()
        if not route:
            return "no_route_spine"
        raw, emulated_rows = await self._spine_rows(subject, route)
        now_ms = int(time.time() * 1000)
        live = aggregate_live_congestion(route, raw, lengths, now_ms)
        if live is None:
            return "no_fresh_observations"

        robust = self.health.robust_inputs
        congestion = live.p90 if robust else live.mean
        weather_code, weather_severity, weather_source = _weather_from_metadata(
            subject.metadata
        )
        crowd_class = subject.crowd_class if subject.kind == "event" else None
        duration = subject.metadata.get("duration_class")
        try:
            inputs = SituationInputs.from_scores(
                vvip_class=tier.code,
                predicted_congestion_idx=congestion,
                weather_severity=weather_severity,
                crowd_score=0.0,
                anomaly_active=anomaly_active,
                event_active=subject.status == "active",
                weather=weather_code,
                crowd_class=crowd_class,
                duration_class=(
                    str(duration)
                    if duration in ("minutes", "hours", "multi_day", "multi_week")
                    else "minutes"
                ),
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "adaptive.engine_loop.bad_live_inputs",
                movement_id=subject.subject_id,
                error=str(exc),
            )
            return "bad_live_inputs"

        gps_age, gps_source = await self._gps_age(subject)
        quality = _quality_label(live.quality)
        provenance = {
            "source": "live_cache",
            "congestion_basis": "traffic_latest",
            "congestion_aggregate": (
                "length_weighted_p90" if robust else "length_weighted_mean"
            ),
            "predicted_congestion_idx": round(congestion, 4),
            "congestion_mean": round(live.mean, 4),
            "congestion_p90": round(live.p90, 4),
            "spine": basis,
            "segments_used": live.segments_used,
            "segments_total": live.segments_total,
            "coverage": round(live.coverage, 3),
            "obs_age_max_sec": round(live.obs_age_max_sec, 1),
            "obs_age_min_sec": round(live.obs_age_min_sec, 1),
            "data_quality": quality,
            # emulator rows are synthetic too; either kind = simulated feed
            "simulated_feed": live.quality.get("synthetic", 0) > 0 or emulated_rows > 0,
            # same-provenance inputs: a live event never reads the
            # emulator's key space; an emulated one prefers it per segment
            "inputs_origin": (
                "live"
                if emulated_rows == 0
                else "emulated" if emulated_rows == len(route) else "mixed"
            ),
            "emulated_segments": emulated_rows,
            "anomalous_excluded": live.anomalous_excluded,
            "stale_excluded": live.stale_excluded,
            "weather_source": weather_source,
            "crowd_source": "event_row" if crowd_class else "none",
            "event_active_basis": "status",
            "gps_source": gps_source,
        }
        return inputs, provenance, gps_age, 0.0

    async def _spine_rows(
        self, subject: Subject, route: list[int]
    ) -> tuple[list[Any], int]:
        """The cached rows behind one subject's spine, same provenance only.

        A live subject reads ``traffic:latest:{sid}`` and nothing else.  An
        emulated subject reads its scenario's ``traffic:emulated:latest:
        {sid}`` row where one exists and the live row otherwise.  Returns
        ``(rows aligned with route, emulated rows chosen)``.
        """
        live_rows = await _mget(self._valkey, [latest_key(sid) for sid in route])
        if not subject.emulated:
            return live_rows, 0
        emu_rows = await _mget(self._valkey, [emulated_latest_key(sid) for sid in route])
        now_ms = int(time.time() * 1000)
        rows: list[Any] = []
        emulated = 0
        for live_raw, emu_raw in zip(live_rows, emu_rows, strict=False):
            if _fresh_row(emu_raw, now_ms):
                rows.append(emu_raw)
                emulated += 1
            else:
                rows.append(live_raw)
        return rows, emulated

    async def _gps_age(self, subject: Subject) -> tuple[float, str]:
        """Age of the subject's convoy GPS mirror (contract C-B), if any."""
        if not subject.movement_ref:
            return 0.0, "none"
        with contextlib.suppress(Exception):
            raw = await self._valkey.get(  # type: ignore[union-attr]
                f"convoy:position:{subject.movement_ref}"
            )
            pos = _as_dict(raw)
            t = pos.get("t")
            if isinstance(t, str):
                at = datetime.fromisoformat(t.replace("Z", "+00:00"))
                if at.tzinfo is None:
                    at = at.replace(tzinfo=UTC)
                age = (datetime.now(UTC) - at).total_seconds()
                return round(max(0.0, age), 1), str(pos.get("source") or "gps")
        return 0.0, "none"


def _consume_outcome(task: asyncio.Task) -> None:
    """Retrieve an abandoned tick's outcome (no 'never retrieved' noise)."""
    if not task.cancelled():
        task.exception()


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
    "LIVE_MAX_AGE_SEC",
    "LiveCongestion",
    "LoopHealth",
    "STALE_INPUTS_SEC",
    "SituationEngineLoop",
    "TICK_TIMEOUT_SEC",
    "Subject",
    "SubjectHealth",
    "aggregate_live_congestion",
    "emulator_source_key",
    "inputs_key",
    "latest_key",
    "robust_inputs_enabled",
    "start_situation_engine",
    "weighted_quantile",
]
