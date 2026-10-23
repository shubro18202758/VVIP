"""Run orchestration — ledger, build, execute, stop, replay.

Execution model: a run is ledgered FIRST (its run id is part of a live
run's identity, tape.movement_uuid), then its tape is built and every frame
fires at WALL time ``t0 + t_sec / clock_scale`` (late frames fire at once,
flagged late).  Anything a frame stamps — a camera read's ``t``, a unit
position's ``t``, the event's window — is wall time on that same clock, and
the scenario offset (``t_sec``) rides along in the frame log, never in a
timestamp: nothing the emulator writes can sit in the future.

Seams per frame kind (contracts in CONTRACTS.md):
    event_row    corridor event + legacy movement rows (store, direct upsert
                 with its creation ledger row), then 'active' at t0 and
                 'completed' at the horizon through POST /events/{id}/status
                 (C-F, officer 'event-emulator (system)') — so the status
                 ledger and the dispatch auto-close run
    inputs_doc   Valkey adaptive:inputs:{id} (+ the .emulated Arrow plane)
    anpr_read    POST /api/v1/vehicles/sightings (C-E): the ORACLE computes
                 the hit and the e-challan rules; the emulator asserts none.
                 A tape that drives a FIR-named stolen plate (role
                 stolen_fir_plate) first makes its PREMISE explicit at t0:
                 POST /api/v1/vehicles/{plate}/earmark (lawful basis
                 stolen_vehicle_fir, the use case's FIR, earmarked_by
                 "event-emulator (system) — scenario precondition", reason
                 EMULATED) unless an active stolen-FIR earmark exists — so
                 its reads become evidence-only R_STOLEN_SIGHTING (C-N),
                 never a fine.  When the premise cannot be ensured, that
                 plate's reads are skipped (said so), never filed as penal
    agency_unit  NATS corridor.units.emulated (C-A); a unit an officer has
                 dispatched is left to the oracle's dispatch projection
    twin         one POST /api/v1/simulate/twin per run, concurrent with the
                 frames, delivery level recorded honestly

Delivery is per-frame and honest: every frame ends ``delivered`` /
``failed(reason)`` / ``skipped(reason)``; a run that delivered nothing is
'failed', never 'completed'.  The corridor event is the run's purpose, so
its create frame counts as delivered only once the event is LIVE: when the
oracle cannot take the t0 'active' transition (an outage), the frame
counts as failed and the activation is retried every ACTIVATE_RETRY_SEC of
wall time until it lands (the frame is then re-counted delivered); a run
whose event never became active ends 'failed', never 'completed'.  A
:class:`RunControl` stops a run between frames ('stopped' by an operator,
'interrupted' by shutdown): its corridor event is closed through C-F and
the run row finished with the reason.  The emulator NEVER consumes
outcomes — the twin's answer is recorded compactly, never acted on.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
import structlog

from event_emulator.library import UseCase, load_library
from event_emulator.nats_seam import NatsSeam, UNITS_EMULATED_SUBJECT
from event_emulator.profiles import UseCaseProfile, profile_for, protection_class_for
from event_emulator.safe_http import UnsafeUrlError
from event_emulator.spec import EmulationSpec, ResolvedRun
from event_emulator.store import CorridorStore, utcnow
from event_emulator.tape import (
    CAUSE_KINDS,
    CauseTape,
    approach_movement,
    build_tape,
    orient_route,
    recorded_route,
    route_polyline,
)
from event_emulator.twin_seam import OracleSeam, TwinSeam
from event_emulator.valkey_seam import ValkeySeam

logger = structlog.get_logger(__name__)

__all__ = [
    "ACTIVATE_RETRY_SEC",
    "FRAME_LOG_CAP",
    "RunControl",
    "RunProgress",
    "RunnerDeps",
    "RunOutcome",
    "activate_event",
    "attach_signal_context",
    "cap_frame_log",
    "close_event",
    "execute_replay",
    "execute_run",
    "ledger_run",
    "replay_refusal",
]

#: Frame-log entries persisted per run (the full log can run to thousands).
FRAME_LOG_CAP = 300

#: Seconds between progress / liveness writes to a running ledger row.
HEARTBEAT_SEC = 10.0

#: How long a stopping run waits for an in-flight twin POST.
_TWIN_GRACE_SEC = 5.0

#: Re-check of which tape units an officer has dispatched (seconds).
_DISPATCH_CHECK_SEC = 15.0

#: Wall seconds between retries of a corridor event's 'active' transition
#: (C-F) that the oracle could not take at t0.
ACTIVATE_RETRY_SEC = 5.0

#: Placeholder tape while a ledgered run builds its real one.
_PENDING_TAPE = json.dumps({"tape_version": 2, "frames": [], "pending": True})

#: The scenario-premise earmark (C-N): who states it and how long it holds.
#: It is the scenario's own precondition — labelled, EMULATED — never an
#: operational decision: the sweep's FIR names an EMULATED plate (EM).
SCENARIO_EARMARKER = "event-emulator (system) — scenario precondition"
SCENARIO_EARMARK_RETENTION_DAYS = 7
STOLEN_FIR_BASIS = "stolen_vehicle_fir"
#: The tape role of a FIR-named stolen plate (tape._build_anpr_frames).
STOLEN_FIR_ROLE = "stolen_fir_plate"
_EARMARK_TIMEOUT = httpx.Timeout(8.0, connect=2.0)


@dataclass
class RunnerDeps:
    """Real-stack clients; any may be None → its frames degrade honestly."""

    store: CorridorStore | None = None
    valkey: ValkeySeam | None = None
    twin: TwinSeam | None = None
    nats: NatsSeam | None = None
    oracle: OracleSeam | None = None
    sleep: Any = asyncio.sleep  # injectable clock for tests
    clock: Callable[[], float] = time.time


@dataclass
class RunProgress:
    """Tape frames processed so far (contract C-G progress).

    A count can move once after the fact: the create frame of an event the
    oracle activated LATE leaves ``failed`` for ``delivered`` when the
    retried 'active' lands (:meth:`reclassify`).
    """

    frame: int = 0
    total: int = 0
    delivered: int = 0
    failed: int = 0
    skipped: int = 0
    late: int = 0

    @staticmethod
    def _bucket(result: Any) -> str:
        return result if result in ("delivered", "failed") else "skipped"

    def record(self, entry: dict) -> None:
        self.frame += 1
        bucket = self._bucket(entry.get("result"))
        setattr(self, bucket, getattr(self, bucket) + 1)
        if entry.get("late"):
            self.late += 1

    def reclassify(self, was: str, now: str) -> None:
        """Move one already-recorded frame from result ``was`` to ``now``."""
        old, new = self._bucket(was), self._bucket(now)
        if old != new and getattr(self, old) > 0:
            setattr(self, old, getattr(self, old) - 1)
            setattr(self, new, getattr(self, new) + 1)

    def to_dict(self) -> dict[str, int]:
        return {
            "frame": self.frame, "total": self.total,
            "delivered": self.delivered, "failed": self.failed,
            "skipped": self.skipped, "late": self.late,
        }


class RunControl:
    """Cooperative stop + live progress for one run.

    The frame loop sleeps on the stop event, so a stop takes effect at once
    (between frames — a seam call in flight completes first, nothing is
    left half-written).  ``reason`` is the run's final status:
    'stopped' (operator) or 'interrupted' (process shutdown).
    ``event_status`` is the run's corridor event as the lifecycle seam last
    answered (planning → active → completed | cancelled; None before it
    exists) — 'planning' mid-run means its activation is still pending.
    """

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self.reason: str | None = None
        self.progress = RunProgress()
        self.run_id: int | None = None
        self.event_id: str | None = None
        self.event_status: str | None = None
        self.started_at: datetime = utcnow()

    def request_stop(self, reason: str = "stopped") -> None:
        if self.reason is None:
            self.reason = reason
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    async def wait(self, delay: float, sleep: Any = asyncio.sleep) -> bool:
        """Sleep ``delay`` seconds or until stopped; True when stopped."""
        if self._stop.is_set():
            return True
        if sleep is not asyncio.sleep:   # injected test clock
            await sleep(delay)
            return self._stop.is_set()
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        return self._stop.is_set()


@dataclass
class RunOutcome:
    run_id: int | None
    status: str
    tape: CauseTape | None
    movement_id: str | None
    delivery: dict = field(default_factory=dict)
    frame_log: list[dict] = field(default_factory=list)
    twin: dict | None = None
    reason: str | None = None


def _summarize(frame_log: list[dict]) -> dict:
    by: dict[str, int] = {}
    for e in frame_log:
        by[e["result"]] = by.get(e["result"], 0) + 1
    return {
        "frames": len(frame_log),
        "delivered": by.get("delivered", 0),
        "failed": by.get("failed", 0),
        "skipped": by.get("skipped", 0),
        "late": sum(1 for e in frame_log if e.get("late")),
    }


def cap_frame_log(frame_log: list[dict], cap: int = FRAME_LOG_CAP) -> tuple[list[dict], int]:
    """(entries kept, entries dropped) — every failure / skip and lifecycle
    entry first (up to half the cap), the newest of the rest after."""
    if len(frame_log) <= cap:
        return list(frame_log), 0
    notable = [
        i for i, e in enumerate(frame_log)
        if e.get("result") != "delivered"
        or e.get("kind") in ("twin_post", "event_row", "event_lifecycle", "run",
                             "scenario_precondition")
    ][: cap // 2]
    keep = set(notable)
    rest = [i for i in range(len(frame_log)) if i not in keep]
    keep.update(rest[-(cap - len(keep)):])
    kept = [frame_log[i] for i in sorted(keep)]
    return kept, len(frame_log) - len(kept)


def _iso(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat()


# ── corridor-event lifecycle through the oracle (contract C-F) ──────────────


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("detail", body))[:200]
    return str(body)[:200]


def _closed_dispatches(resp: httpx.Response) -> list[dict]:
    """The dispatches the oracle released with an event close (C-F
    additive ``closed_dispatches``: {dispatch_id, unit_id, agency,
    from_status, to_status}); [] when the answer carries none."""
    try:
        body = resp.json()
    except ValueError:
        return []
    rows = body.get("closed_dispatches") if isinstance(body, dict) else None
    return [dict(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


async def activate_event(
    oracle: OracleSeam | None, event_id: str, note: str,
    replay_run_id: int | None = None,
) -> dict:
    """planning → active (C-F).  Never raises; returns the ledger entry.

    ``terminal`` True: the event was closed under the caller (a 409 from
    'completed' / 'cancelled') — no retry can ever activate it."""
    if oracle is None:
        return {"result": "skipped", "reason": "oracle seam not configured"}
    try:
        resp = await oracle.event_status(event_id, "active", note, replay_run_id)
    except (httpx.HTTPError, UnsafeUrlError) as exc:
        return {"result": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    if resp.status_code == 200:
        return {"result": "delivered", "to_status": "active", "http_status": 200}
    if resp.status_code == 409:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        from_status = body.get("from_status")
        if from_status == "active":
            return {"result": "delivered", "to_status": "active", "http_status": 409,
                    "note": "already active"}
        if from_status in ("completed", "cancelled"):
            return {"result": "failed", "http_status": 409, "from_status": from_status,
                    "terminal": True,
                    "reason": f"the event was {from_status} before it became active"}
    return {"result": "failed", "http_status": resp.status_code, "reason": _detail(resp)}


async def close_event(
    oracle: OracleSeam | None, event_id: str, note: str,
    replay_run_id: int | None = None,
) -> dict:
    """Close an emulator event through C-F: 'completed' when it ran,
    'cancelled' when it never left planning/approved, nothing when it is
    already terminal.  Never raises; returns the ledger entry, with the
    dispatches the oracle released along with the event
    (``closed_dispatches``, its auto-close) when it moved."""
    if oracle is None:
        return {"result": "skipped", "reason": "oracle seam not configured"}
    try:
        resp = await oracle.event_status(event_id, "completed", note, replay_run_id)
        if resp.status_code == 200:
            return {"result": "delivered", "to_status": "completed", "http_status": 200,
                    "closed_dispatches": _closed_dispatches(resp)}
        if resp.status_code == 404:
            return {"result": "skipped", "reason": "event not found", "http_status": 404}
        if resp.status_code == 409:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            allowed = body.get("allowed") or []
            from_status = body.get("from_status")
            if "cancelled" in allowed:
                resp2 = await oracle.event_status(event_id, "cancelled", note, replay_run_id)
                if resp2.status_code == 200:
                    return {"result": "delivered", "to_status": "cancelled",
                            "from_status": from_status, "http_status": 200,
                            "closed_dispatches": _closed_dispatches(resp2)}
                return {"result": "failed", "http_status": resp2.status_code,
                        "reason": _detail(resp2)}
            if not allowed:  # terminal already: nothing left to close
                return {"result": "delivered", "to_status": from_status,
                        "http_status": 409, "note": "already terminal"}
        return {"result": "failed", "http_status": resp.status_code, "reason": _detail(resp)}
    except (httpx.HTTPError, UnsafeUrlError) as exc:
        return {"result": "failed", "reason": f"{type(exc).__name__}: {exc}"}


# ── signal context for camera sites (pure over store rows) ─────────────────


def attach_signal_context(
    sites: list[dict],
    programs: dict[int, dict],
    legs_by_junction: dict[int, list[dict]],
) -> list[dict]:
    """Give each camera site the program + movement that governs a read
    placed on it with its registered heading (None when no program does,
    or when the oracle could make no signal decision for such a read)."""
    out: list[dict] = []
    for s in sites:
        site = dict(s)
        jid = site.get("signal_junction_id")
        prog = programs.get(int(jid)) if jid is not None else None
        site["signal"] = None
        if prog is not None:
            heading = site.get("heading_deg")
            movement = None
            if heading is not None:
                movement = approach_movement(
                    round(float(heading), 1), legs_by_junction.get(int(jid), []),
                    prog["phases"], segment_id=site.get("segment_id"),
                )
            site["signal"] = {
                "junction_id": int(jid),
                "cycle_sec": int(prog["cycle_sec"]),
                "offset_sec": int(prog.get("offset_sec", 0)),
                "phases": prog["phases"],
                "movement": movement,
            }
        out.append(site)
    return out


async def load_camera_context(store: CorridorStore) -> list[dict]:
    """Registered ANPR sites + their governing signal context."""
    sites = await store.camera_sites()
    if not sites:
        return []
    junctions = sorted({int(s["signal_junction_id"]) for s in sites
                        if s.get("signal_junction_id") is not None})
    programs = await store.signal_programs() if junctions else {}
    legs = await store.junction_legs(junctions) if junctions else {}
    return attach_signal_context(sites, programs, legs)


# ── ledger ──────────────────────────────────────────────────────────────────


async def ledger_run(
    deps: RunnerDeps,
    spec: EmulationSpec,
    use_case: UseCase,
    *,
    t0: datetime,
    seed: int,
    is_replay: bool,
    replay_of: int | None,
) -> int | None:
    """Insert the 'running' ledger row (placeholder tape); None without a
    store.  Raises when the store is configured but the insert fails."""
    if deps.store is None:
        return None
    resolved = ResolvedRun.resolve(spec, use_case)
    doc = json.loads(spec.model_dump_json()) if not is_replay else {
        "replay_of_run_id": replay_of,
        "clock_scale": spec.clock_scale,
        "deliver_nats": spec.deliver_nats,
        "deliver_twin": spec.deliver_twin,
    }
    doc["mode"] = "use_case"
    doc["progress"] = RunProgress().to_dict()
    return await deps.store.insert_run(
        use_case_id=spec.use_case_id,
        spec=doc,
        spec_hash=resolved.spec_hash,
        seed=seed,
        t0_anchor=t0,
        clock_scale=spec.clock_scale,
        tape_json=_PENDING_TAPE,
        is_replay=is_replay,
        replay_of_run_id=replay_of,
    )


def _has_watchlist_knobs(profile: UseCaseProfile) -> bool:
    return bool(
        getattr(profile, "anpr_cameras", 0)
        or getattr(profile, "anpr_stopline_cameras", 0)
        or getattr(profile, "agency_units", 0)
    )


def _unit_track_segment_ids(use_case: UseCase) -> list[int]:
    """The segments a use case's scripted unit tracks ride (config
    ``unit_tracks.units[*].segment_ids``), in first-seen order."""
    spec = use_case.config.get("unit_tracks") or {}
    out: list[int] = []
    for unit in spec.get("units") or []:
        for sid in unit.get("segment_ids") or []:
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                continue
            if sid not in out:
                out.append(sid)
    return out


async def _build_live_tape(
    deps: RunnerDeps,
    resolved: ResolvedRun,
    use_case: UseCase,
    profile: UseCaseProfile,
    run_id: int | None,
) -> CauseTape:
    """Fetch the real geometry the watchlist generators anchor to, then
    build the pure tape.  Geometry failures degrade the anchors (logged),
    never the run.

    The scripted unit tracks (the flagship's Jan Rakshak beats and its 108
    run) ride the store's DRAWN segment paths too — COALESCE(display_geom,
    geom), contract C-K — so a recorded unit follows the OSM-traced road
    once its segments are traced; a segment the store cannot answer keeps
    the library's embedded ``route_geometry`` (tape._build_track_frames)."""
    route = list(use_case.config["route_segment_ids"])
    anchors = paths = sites = None
    if _has_watchlist_knobs(profile) and deps.store is not None:
        try:
            anchors = await deps.store.segment_midpoints(route)
            if getattr(profile, "agency_units", 0):
                paths = await deps.store.segment_paths(route)
            if getattr(profile, "anpr_cameras", 0) or getattr(profile, "anpr_stopline_cameras", 0):
                sites = await load_camera_context(deps.store)
        except Exception as exc:  # noqa: BLE001 - anchors degrade, run lives
            logger.warning("runner.geometry_failed", error=str(exc))
    tracks = [s for s in _unit_track_segment_ids(use_case) if s not in (paths or {})]
    if tracks and deps.store is not None:
        try:
            track_paths = await deps.store.segment_paths(tracks)
        except Exception as exc:  # noqa: BLE001 - the library geometry stands in
            logger.warning("runner.track_geometry_failed", error=str(exc))
        else:
            paths = {**(paths or {}), **track_paths}
    return build_tape(
        resolved, use_case, profile, anchors,
        run_id=run_id, segment_paths=paths, camera_sites=sites,
    )


# ── scenario premise (C-N): the FIR-named stolen plate IS reported stolen ──


def _plate_key(plate: Any) -> str:
    return "".join(str(plate or "").split()).upper()


def stolen_fir_plates(tape: CauseTape) -> list[str]:
    """The FIR-named stolen plates a tape drives (its stolen_fir_plate reads)."""
    return sorted({
        str(f.payload["plate"]) for f in tape.frames
        if f.kind == "anpr_read" and f.payload.get("role") == STOLEN_FIR_ROLE
        and f.payload.get("plate")
    })


def stolen_fir_ref(use_case: UseCase, profile: UseCaseProfile) -> str:
    """The FIR the scenario's premise names: the use-case config's, else the
    profile's (an FIR reference the oracle can extract — 'FIR <ref>')."""
    ref = use_case.config.get("stolen_fir_ref") or getattr(profile, "anpr_stolen_fir_ref", None)
    return str(ref).strip() if ref else "FIR (scenario)"


async def ensure_stolen_earmark(
    oracle: Any, plate: str, *, fir_ref: str, use_case_id: str, run_id: int | None,
    replay_run_id: int | None = None,
) -> dict:
    """Make the scenario premise explicit: ``plate`` holds an ACTIVE
    stolen_vehicle_fir earmark (C-N) — created through the oracle's own
    earmark API when it has none, labelled an EMULATED scenario precondition
    under ``SCENARIO_EARMARKER``; idempotent when one is already active.

    Returns the ledger entry (never raises): ``result`` delivered with
    ``earmark`` 'already_active' or 'created'; failed with the reason when
    the oracle could not confirm or take it; skipped for a seam that cannot
    read or write earmarks at all (a test double). The calls ride the
    oracle seam's guarded client (its fixed internal endpoint) — the same
    allowlist as every other oracle write.
    """
    entry: dict[str, Any] = {"plate": plate, "lawful_basis": STOLEN_FIR_BASIS,
                             "fir_ref": fir_ref}
    if oracle is None:
        return {**entry, "result": "failed", "reason": "oracle seam not configured"}
    client = getattr(oracle, "_client", None)
    base = getattr(oracle, "_base", None)
    if client is None or not base:
        return {**entry, "result": "skipped",
                "reason": "this oracle seam cannot read or write earmarks — premise not checked"}
    try:
        listed = await client.get(f"{base}/api/v1/vehicles", replay_run_id=replay_run_id,
                                  timeout=_EARMARK_TIMEOUT)
    except (httpx.HTTPError, UnsafeUrlError) as exc:
        return {**entry, "result": "failed", "reason": f"{type(exc).__name__}: {exc}"[:200]}
    if listed.status_code == 200:
        try:
            vehicles = listed.json().get("vehicles") or []
        except (ValueError, AttributeError):
            vehicles = []
        for v in vehicles:
            if (isinstance(v, dict) and _plate_key(v.get("plate")) == _plate_key(plate)
                    and v.get("lawful_basis") == STOLEN_FIR_BASIS):
                return {**entry, "result": "delivered", "earmark": "already_active",
                        "earmark_reason": str(v.get("earmark_reason") or "")[:200]}
    run = f", run {run_id}" if run_id is not None else ""
    body = {
        "state": "flagged",
        "lawful_basis": STOLEN_FIR_BASIS,
        "retention_days": SCENARIO_EARMARK_RETENTION_DAYS,
        "earmarked_by": SCENARIO_EARMARKER,
        "reason": (f"EMULATED scenario precondition — stolen-vehicle {fir_ref} names {plate} "
                   f"(event-emulator use case '{use_case_id}'{run}); its reads are "
                   "evidence-only sightings, never a fine"),
    }
    try:
        resp = await client.post(f"{base}/api/v1/vehicles/{plate}/earmark", json_body=body,
                                 replay_run_id=replay_run_id, timeout=_EARMARK_TIMEOUT)
    except (httpx.HTTPError, UnsafeUrlError) as exc:
        return {**entry, "result": "failed", "reason": f"{type(exc).__name__}: {exc}"[:200]}
    if resp.status_code != 200:
        return {**entry, "result": "failed", "http_status": resp.status_code,
                "reason": _detail(resp)}
    try:
        out = resp.json()
    except ValueError:
        out = {}
    return {**entry, "result": "delivered", "earmark": "created",
            "earmark_id": out.get("earmark_id"), "earmarked_by": out.get("earmarked_by"),
            "retention_until": out.get("retention_until")}


async def _ensure_scenario_premise(deps: RunnerDeps, st: _RunState, frame_log: list[dict]) -> None:
    """Before the first read: every FIR-named stolen plate the tape drives
    holds its stolen-FIR earmark, or its reads will be skipped."""
    plates = stolen_fir_plates(st.tape)
    if not plates:
        return
    fir_ref = stolen_fir_ref(st.use_case, st.profile)
    for plate in plates:
        entry = await ensure_stolen_earmark(
            deps.oracle, plate, fir_ref=fir_ref, use_case_id=st.tape.use_case_id,
            run_id=st.run_id, replay_run_id=st.replay_header,
        )
        if entry["result"] == "failed":
            st.unearmarked_stolen.add(_plate_key(plate))
            logger.warning("runner.scenario_premise_unmet", plate=plate,
                           reason=entry.get("reason"))
        st.preconditions.append(dict(entry))
        frame_log.append({"frame": None, "kind": "scenario_precondition", **entry})
    # The running row says so at once (merged into its spec like a
    # heartbeat), so the deck's run list shows the premise mid-run, not only
    # once the run has finished; the finish doc carries it as before.
    if deps.store is not None and st.run_id is not None:
        try:
            await deps.store.heartbeat_run(
                st.run_id, {"scenario_precondition": list(st.preconditions)})
        except Exception as exc:  # noqa: BLE001 - the finish doc still carries it
            logger.warning("runner.scenario_premise_ledger_failed", run_id=st.run_id,
                           error=str(exc))


# ── execution ───────────────────────────────────────────────────────────────


@dataclass
class _RunState:
    """Mutable per-run bookkeeping shared by the frame handlers."""

    tape: CauseTape
    run_id: int | None
    is_replay: bool
    replay_of: int | None
    clock_scale: float
    wall0: float
    use_case: UseCase
    profile: UseCaseProfile
    movement_created: bool = False
    event_open: bool = False
    event_active: bool = False          # the C-F 'active' transition landed
    create_entry: dict | None = None    # the create frame's log entry
    activate_attempts: int = 0
    activate_retry_at: float = 0.0      # wall time of the next late attempt
    activate_error: str | None = None   # why the last attempt failed
    activate_gave_up: bool = False      # closed under us: nothing to activate
    anchors: dict = field(default_factory=dict)   # segment -> on-road midpoint
    last_doc: dict | None = None
    dispatched: set = field(default_factory=set)
    dispatched_checked: float = 0.0
    # C-N scenario premise: the FIR-named stolen plates whose earmark could
    # NOT be ensured (their reads are skipped, never filed as penal) and the
    # precondition entries recorded in the run ledger
    unearmarked_stolen: set = field(default_factory=set)
    preconditions: list = field(default_factory=list)

    @property
    def replay_header(self) -> int | None:
        return self.run_id if self.is_replay else None

    @property
    def awaiting_activation(self) -> bool:
        return self.event_open and not self.event_active and not self.activate_gave_up


def _note_activation(
    deps: RunnerDeps, st: _RunState, control: RunControl, entry: dict
) -> bool:
    """Record one 'active' attempt's answer; True once the event is live."""
    if entry["result"] == "delivered":
        st.event_active = True
        st.activate_error = None
        control.event_status = "active"
        return True
    st.activate_error = str(entry.get("reason") or f"HTTP {entry.get('http_status')}")[:200]
    st.activate_retry_at = deps.clock() + ACTIVATE_RETRY_SEC
    if entry.get("terminal"):
        # someone closed it first (an officer's cancel): nothing to retry,
        # nothing left for this run to close
        st.activate_gave_up = True
        st.event_open = False
        control.event_status = entry.get("from_status")
    elif entry["result"] == "skipped":
        # no lifecycle seam at all: no retry can ever activate it
        st.activate_gave_up = True
    return False


async def _retry_activation(
    deps: RunnerDeps, st: _RunState, control: RunControl, frame_log: list[dict],
    frame_no: int | None,
) -> None:
    """Late 'active' (C-F) for an event the oracle could not take at t0 —
    at most one attempt per ACTIVATE_RETRY_SEC of wall time.  Only the
    outcome is logged (a long outage would otherwise flood the frame log);
    the create frame carries the attempt count."""
    if not st.awaiting_activation or deps.clock() < st.activate_retry_at:
        return
    st.activate_attempts += 1
    entry = await activate_event(
        deps.oracle, st.tape.movement_id,
        f"emulator run {st.run_id}: late activation (attempt {st.activate_attempts}) — "
        "the oracle could not take it at t0", st.replay_header,
    )
    landed = _note_activation(deps, st, control, entry)
    created = st.create_entry
    if landed:
        frame_log.append({"frame": frame_no, "kind": "event_lifecycle", "to": "active",
                          "late": True, "attempt": st.activate_attempts, **entry})
        if created is not None and created.get("result") == "failed":
            created.pop("reason", None)
            created.update(result="delivered", activated_late=True,
                           activation_attempts=st.activate_attempts)
            control.progress.reclassify("failed", "delivered")
        if deps.store is not None:
            try:
                await deps.store.set_movement_status(st.tape.movement_id, "active")
            except Exception as exc:  # noqa: BLE001 - the legacy row lags, run lives
                logger.warning("runner.movement_status_failed", error=str(exc))
    elif st.activate_gave_up:
        frame_log.append({"frame": frame_no, "kind": "event_lifecycle", "to": "active",
                          "attempt": st.activate_attempts, **entry})
    if created is not None and not landed:
        created["activation_attempts"] = st.activate_attempts
        created["reason"] = _not_active_reason(st)


def _not_active_reason(st: _RunState) -> str:
    if st.activate_gave_up:
        return f"corridor event never became active (C-F): {st.activate_error}"
    return (f"corridor event created but not active (C-F: {st.activate_error}) — "
            f"retried every {ACTIVATE_RETRY_SEC:g} s")


async def _deliver_event_create(
    deps: RunnerDeps, st: _RunState, control: RunControl, frame: Any,
    frame_log: list[dict],
) -> dict:
    """Create (or re-animate) the corridor event, then 'active' at t0.

    The frame is delivered only when the event is LIVE: an activation the
    oracle could not take leaves it 'failed' (with the reason) while
    :func:`_retry_activation` keeps trying; the rest of the tape plays on
    against the created event meanwhile."""
    p = frame.payload
    if deps.store is None:
        return {"result": "skipped", "reason": "store not configured"}
    route = list(p["route_segment_ids"])
    st.anchors = await deps.store.segment_midpoints(route)
    if len(st.anchors) < len(route):
        missing = [s for s in route if s not in st.anchors]
        return {"result": "failed", "reason": f"unknown segments {missing}"}
    line = await _route_line(deps.store, route, st.anchors)
    t0_dt = datetime.fromtimestamp(st.wall0, tz=timezone.utc)
    horizon_sec = max(
        (fr.t_sec for fr in st.tape.frames if fr.kind == "event_row"), default=0.0
    )
    # the event's window is WALL time: the scenario's horizon compressed by
    # the clock scale (a 60-min scenario at x10 holds the road 6 minutes)
    window_end = t0_dt + timedelta(seconds=horizon_sec / st.clock_scale)
    mid = str(p["movement_id"])
    cfg = st.use_case.config
    # legacy row — what today's running engine loop polls (engine_loop.py):
    # origin / destination are where the route starts and ends on its roads
    await deps.store.upsert_movement(mid, p["vvip_class"], t0_dt, line[0], line[-1])
    await deps.store.replace_route(mid, route, line)
    # generalized row — core's 007 events schema, soft-linked to the movement
    meta = {
        "source": "event-emulator",
        "mode": "use_case",
        "run_id": st.run_id,
        "use_case_id": st.tape.use_case_id,
        "scenario_id": cfg["scenario_id"],
        "seed": st.tape.seed,
        "closure_policy": cfg["closure_policy"],
        "clock_scale": st.clock_scale,
        "scenario_horizon_sec": horizon_sec,
        "is_replay": st.is_replay,
        "replay_of_run_id": st.replay_of,
    }
    note = (f"emulator run {st.run_id} (replay of run {st.replay_of}) re-animates "
            f"the event" if st.is_replay else f"created by emulator run {st.run_id}")
    prior = await deps.store.upsert_corridor_event(
        event_id=mid,
        movement_id=mid,
        event_kind=p["event_kind"],
        protection_class=protection_class_for(st.profile, p["vvip_class"]),
        crowd_class=st.profile.crowd_class,
        title=str(cfg["name"]),
        description=(f"EMULATED use case '{st.tape.use_case_id}' (seed {st.tape.seed}, "
                     f"x{st.clock_scale:g}) — event-emulator run {st.run_id}"),
        planned_start=t0_dt,
        planned_end=window_end,
        metadata_doc=meta,
        note=note,
    )
    await deps.store.replace_event_route(mid, route)
    await deps.store.replace_event_phase(
        mid, label=p["event_kind"], window_start=t0_dt, window_end=window_end,
        closure_policy=dict(cfg["closure_policy"]),
    )
    st.movement_created = True
    st.event_open = True
    control.event_status = "planning"
    # 'active' at t0 through the oracle's lifecycle seam (C-F)
    st.activate_attempts = 1
    lifecycle = await activate_event(
        deps.oracle, mid, f"emulator run {st.run_id}: scenario t0", st.replay_header
    )
    frame_log.append({"frame": frame.frame, "kind": "event_lifecycle",
                      "to": "active", "attempt": 1, **lifecycle})
    out: dict[str, Any] = {"movement_id": mid}
    if prior is not None:
        out["reanimated_from"] = prior
    if _note_activation(deps, st, control, lifecycle):
        await deps.store.set_movement_status(mid, "active")
        out["result"] = "delivered"
    else:
        out.update(result="failed", reason=_not_active_reason(st), activation_attempts=1)
    return out


async def _route_line(
    store: Any, route: list[int], anchors: dict[int, tuple[float, float]]
) -> list[tuple[float, float]]:
    """The route as ONE LineString in its direction of travel: the
    segments' routing lines chained in route order, each oriented by the
    junctions segment_adjacency records between consecutive segments and
    by its one-way direction (tape.orient_route).  Its first point is
    where the first segment is entered, its last where the last one is
    left.  A store that answers no line keeps the on-road midpoints."""
    geometry = await store.route_geometry(route)
    line = route_polyline(orient_route(
        route, geometry.get("lines") or {},
        oneway=geometry.get("oneway"), vias=geometry.get("vias"),
    ))
    if len(line) < 2:
        line = [anchors[s] for s in route]
    return line


async def _close_run_event(
    deps: RunnerDeps, st: _RunState, control: RunControl, note: str,
    frame_log: list[dict], frame_no: int | None,
) -> dict:
    mid = st.tape.movement_id
    entry = await close_event(deps.oracle, mid, note, st.replay_header)
    frame_log.append({"frame": frame_no, "kind": "event_lifecycle",
                      "to": entry.get("to_status", "completed"), **entry})
    if entry["result"] == "delivered":
        st.event_open = False
        if entry.get("to_status"):
            control.event_status = entry["to_status"]
        if deps.store is not None:
            final = entry.get("to_status")
            await deps.store.set_movement_status(
                mid, final if final in ("completed", "cancelled") else "completed"
            )
    return entry


async def _deliver_anpr_read(
    deps: RunnerDeps, st: _RunState, payload: dict, t_sec: float, fire_wall: float
) -> dict:
    """One camera read through the oracle's ingest seam (contract C-E).

    Stamped at its wall fire time (never later than now); the oracle
    computes the hit and every rule.  Deterministic read ids make a replay
    idempotent: the seam answers duplicate=true and forks nothing.
    """
    if deps.oracle is None:
        return {"result": "skipped", "reason": "oracle seam not configured"}
    if _plate_key(payload.get("plate")) in st.unearmarked_stolen:
        return {"result": "skipped",
                "reason": ("scenario precondition not met — the FIR-named stolen plate has no "
                           "stolen_vehicle_fir earmark; its read would file a penal e-challan "
                           "against the theft victim's car")}
    t_wall = min(fire_wall, deps.clock())
    body: dict[str, Any] = {
        "read_id": str(payload["read_id"]),
        "plate": str(payload["plate"]),
        "camera_id": str(payload["camera_id"]),
        "t": _iso(t_wall),
        "lon": float(payload["position"][0]),
        "lat": float(payload["position"][1]),
        "speed_kmh": payload.get("speed_kmh"),
        "heading": payload.get("heading"),
        "vehicle_class": payload.get("vehicle_class"),
        "source": "emulated_anpr",
    }
    if st.run_id is not None:
        body["run_id"] = st.run_id
    try:
        resp = await deps.oracle.post_sighting(body, replay_run_id=st.replay_header)
    except (httpx.HTTPError, UnsafeUrlError) as exc:
        return {"result": "failed", "reason": f"{type(exc).__name__}: {exc}", "t": body["t"]}
    if resp.status_code != 200:
        return {"result": "failed", "http_status": resp.status_code,
                "reason": _detail(resp), "t": body["t"]}
    try:
        out = resp.json()
    except ValueError:
        out = {}
    return {
        "result": "delivered",
        "t": body["t"],
        "sighting_id": out.get("sighting_id"),
        "duplicate": bool(out.get("duplicate")),
        "hit": out.get("hit"),
        "recommendations": len(out.get("recommendations") or []),
        "rules_fired": list(out.get("rules_fired") or []),
    }


async def _deliver_agency_unit(
    deps: RunnerDeps, st: _RunState, payload: dict, fire_wall: float
) -> dict:
    """Publish one agency_unit position (clearly labelled emulated, C-A)."""
    if deps.nats is None:
        return {"result": "skipped", "reason": "nats not configured"}
    unit_id = str(payload.get("unit_id"))
    now = deps.clock()
    if deps.store is not None and now - st.dispatched_checked >= _DISPATCH_CHECK_SEC:
        st.dispatched_checked = now
        units = sorted({str(f.payload.get("unit_id")) for f in st.tape.frames
                        if f.kind == "agency_unit"})
        try:
            st.dispatched = await deps.store.dispatched_units(units)
        except Exception as exc:  # noqa: BLE001 - handover check degrades
            logger.warning("runner.dispatch_check_failed", error=str(exc))
    if unit_id in st.dispatched:
        return {"result": "skipped",
                "reason": "unit dispatched by an officer — the dispatch projection drives it"}
    doc = dict(payload)
    doc["source"] = "emulated"
    doc["t"] = _iso(min(fire_wall, now))
    if st.run_id is not None:
        doc["run_id"] = st.run_id
    ok = await deps.nats.publish_json(
        UNITS_EMULATED_SUBJECT, doc, replay_run_id=st.replay_header
    )
    return {"result": "delivered" if ok else "failed",
            **({} if ok else {"reason": "nats disconnected"})}


async def _deliver_twin(
    deps: RunnerDeps, st: _RunState
) -> dict:
    """The one POST of the full deterministic cause set to the real twin."""
    cfg = st.use_case.config
    forced = [dict(f.payload) for f in st.tape.frames if f.kind == "scheduled_event"]
    ec = next((f.payload for f in st.tape.frames if f.kind == "event_config"), None)
    overrides = dict(ec["event_overrides"]) if ec else {}
    if ec:
        overrides["ambulance_rate_multiplier"] = ec["ambulance_rate_multiplier"]
    assert deps.twin is not None
    body = deps.twin.build_body(
        route_segment_ids=list(cfg["route_segment_ids"]),
        departure_time=cfg["departure_time"],
        closure_policy=dict(cfg["closure_policy"]),
        horizon_min=int(cfg["horizon_min"]),
        n_replications=int(cfg["n_replications"]),
        seed=st.tape.seed,
        weather=dict(cfg["weather"]),
        event_overrides=overrides,
        forced_events=forced,
        scenario_id=str(cfg["scenario_id"]),
    )
    entry = await deps.twin.deliver(body, replay_run_id=st.replay_header)
    entry.update(frame=None, kind="twin_post", causes=len(forced))
    return entry


async def _wait_for_frame(
    deps: RunnerDeps, st: _RunState, control: RunControl, fire_wall: float,
    frame_log: list[dict], frame_no: int,
) -> bool:
    """Sleep until wall time ``fire_wall`` or until stopped (True when
    stopped).  While the run's event awaits its late activation the sleep
    is cut into ACTIVATE_RETRY_SEC steps, so the retry cadence does not
    depend on how far apart the frames are."""
    delay = fire_wall - deps.clock()
    while delay > 0:
        step = min(delay, ACTIVATE_RETRY_SEC) if st.awaiting_activation else delay
        if await control.wait(step, deps.sleep):
            return True
        await _retry_activation(deps, st, control, frame_log, frame_no)
        # the planned remainder, cut short by the wall clock when a slow
        # retry (a hanging oracle) ate into it — an injected test sleep
        # need not move the clock at all
        delay = min(delay - step, fire_wall - deps.clock())
    return False


async def _play(
    deps: RunnerDeps,
    spec: EmulationSpec,
    st: _RunState,
    control: RunControl,
    frame_log: list[dict],
) -> None:
    """Fire every tape frame at its wall time, until done or stopped."""
    twin_carries = spec.deliver_twin and deps.twin is not None
    for f in st.tape.frames:
        if control.stopping:
            break
        fire_wall = st.wall0 + f.t_sec / st.clock_scale
        delay = fire_wall - deps.clock()
        if await _wait_for_frame(deps, st, control, fire_wall, frame_log, f.frame):
            break
        late = delay < -1.0
        await _retry_activation(deps, st, control, frame_log, f.frame)

        entry: dict[str, Any] = {"frame": f.frame, "kind": f.kind, "t_sec": f.t_sec}
        try:
            if f.kind == "event_row":
                if f.payload["action"] == "create":
                    st.create_entry = entry
                    entry.update(await _deliver_event_create(deps, st, control, f, frame_log))
                elif f.payload["action"] == "complete":
                    if not st.movement_created:
                        entry.update(result="skipped", reason="no live movement row")
                    elif not st.event_open:
                        entry.update(result="skipped",
                                     reason="the event was closed before the horizon")
                    else:
                        closed = await _close_run_event(
                            deps, st, control,
                            f"emulator run {st.run_id}: tape horizon reached",
                            frame_log, f.frame,
                        )
                        entry.update(result=closed["result"],
                                     **({"reason": closed["reason"]} if "reason" in closed else {}))

            elif f.kind == "inputs_doc":
                if not st.movement_created or deps.valkey is None:
                    entry.update(result="skipped",
                                 reason="no movement row" if not st.movement_created
                                 else "valkey not configured")
                else:
                    await deps.valkey.write_inputs_doc(st.tape.movement_id, f.payload)
                    if st.run_id is not None:
                        await deps.valkey.write_source(
                            st.tape.movement_id, st.run_id, spec.use_case_id, st.is_replay
                        )
                    st.last_doc = f.payload
                    entry.update(result="delivered")

            elif f.kind in ("scheduled_event", "event_config"):
                # carried by the run's one twin POST (its own entry records
                # what the twin actually accepted)
                if twin_carries:
                    entry.update(result="delivered", delivered_by="twin_post")
                else:
                    entry.update(result="skipped", reason="carried by the twin POST, which is off")

            elif f.kind in ("weather_ramp", "crowd_script"):
                # carried by the inputs_doc series
                if st.movement_created and deps.valkey is not None:
                    entry.update(result="delivered", delivered_by="inputs_docs")
                else:
                    entry.update(result="skipped", reason="no inputs-doc carrier")

            elif f.kind == "anpr_read":
                entry.update(await _deliver_anpr_read(deps, st, f.payload, f.t_sec, fire_wall))

            elif f.kind == "agency_unit":
                entry.update(await _deliver_agency_unit(deps, st, f.payload, fire_wall))

            else:  # pragma: no cover - tape kinds are closed
                entry.update(result="skipped", reason=f"unknown kind {f.kind}")

        except Exception as exc:  # noqa: BLE001 - per-frame isolation
            entry.update(result="failed", reason=f"{type(exc).__name__}: {exc}")

        if late:
            entry["late"] = True
        frame_log.append(entry)
        if f.kind in CAUSE_KINDS:
            control.progress.record(entry)

        # showcase plane: project the freshest cause state onto .emulated
        if (f.kind == "inputs_doc" and st.last_doc is not None
                and entry.get("result") == "delivered"
                and deps.nats is not None and spec.deliver_nats):
            rows = _project_rows(st.last_doc, st.anchors)
            if rows:
                ok = await deps.nats.publish(rows, replay_run_id=st.replay_header)
                frame_log.append({
                    "frame": f.frame, "kind": "nats_emulated_batch",
                    "result": "delivered" if ok else "failed",
                    "segments": len(rows),
                    **({} if ok else {"reason": "nats disconnected"}),
                })



async def _heartbeat(deps: RunnerDeps, run_id: int, control: RunControl) -> None:
    """Liveness + progress into the running row every HEARTBEAT_SEC — the
    reaper only ever interrupts a row whose heartbeat went stale."""
    assert deps.store is not None
    while True:
        await asyncio.sleep(HEARTBEAT_SEC)
        await _beat(deps, run_id, control)


async def _beat(deps: RunnerDeps, run_id: int, control: RunControl) -> None:
    assert deps.store is not None
    try:
        await deps.store.heartbeat_run(run_id, {"progress": control.progress.to_dict(),
                                                "event_status": control.event_status})
    except Exception as exc:  # noqa: BLE001 - liveness degrades, run lives
        logger.warning("runner.heartbeat_failed", run_id=run_id, error=str(exc))


async def _await_twin(task: asyncio.Task, control: RunControl) -> dict:
    """The twin POST's ledger entry; a stopping run waits _TWIN_GRACE_SEC."""
    try:
        return await asyncio.wait_for(
            asyncio.shield(task),
            timeout=_TWIN_GRACE_SEC if control.stopping else None,
        )
    except asyncio.TimeoutError:
        task.cancel()
        return {"frame": None, "kind": "twin_post", "result": "skipped",
                "reason": "run ended before the twin answered"}
    except asyncio.CancelledError:
        task.cancel()
        control.request_stop("interrupted")
        raise


def _final_status(control: RunControl, st: _RunState | None, deps: RunnerDeps,
                  frame_log: list[dict], prepared: bool) -> tuple[str, str | None]:
    """(C-G status, reason)."""
    if control.reason == "stopped":
        return "stopped", "stopped by an operator"
    if control.reason == "interrupted":
        return "interrupted", "the emulator shut down while the run was in flight"
    if not prepared or st is None:
        return "failed", "the run could not be prepared"
    progress = control.progress
    if deps.store is not None and not st.movement_created:
        return "failed", "the corridor event was never created"
    if st.movement_created and not st.event_active:
        # the run's purpose is a LIVE corridor event: frames delivered
        # around one that stayed 'planning' are not a completed run
        why = "the corridor event never became active (C-F)"
        if st.activate_error:
            why += (f": {st.activate_error} (after {st.activate_attempts} "
                    f"attempt{'s' if st.activate_attempts != 1 else ''})")
        return "failed", why
    if progress.delivered == 0:
        return "failed", "no frame was delivered"
    if progress.frame and progress.failed / progress.frame > 0.5:
        return "failed", "more than half of the frames failed"
    return "completed", None


async def execute_run(
    deps: RunnerDeps,
    spec: EmulationSpec,
    *,
    library: dict[str, UseCase] | None = None,
    replay_of: int | None = None,
    replay_tape: CauseTape | None = None,
    replay_t0: datetime | None = None,
    run_id: int | None = None,
    control: RunControl | None = None,
) -> RunOutcome:
    """Inject (or replay) one use case through the real stack's seams.

    ``run_id`` given: the ledger row was inserted by the caller (the async
    API); otherwise it is inserted here.  ``control`` stops the run early.
    """
    lib = library if library is not None else load_library()
    if spec.use_case_id not in lib:
        raise KeyError(f"unknown use case {spec.use_case_id!r}")
    use_case = lib[spec.use_case_id]
    profile = profile_for(spec.use_case_id)
    is_replay = replay_tape is not None
    control = control or RunControl()

    resolved = ResolvedRun.resolve(spec, use_case)
    t0 = (replay_t0 or utcnow()) if is_replay else resolved.t0_anchor
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    seed = replay_tape.seed if replay_tape is not None else resolved.seed

    if run_id is None and deps.store is not None:
        try:
            run_id = await ledger_run(deps, spec, use_case, t0=t0, seed=seed,
                                      is_replay=is_replay, replay_of=replay_of)
        except Exception as exc:  # noqa: BLE001 - ledger down ≠ run down
            logger.error("runner.ledger_insert_failed", error=str(exc))
    control.run_id = run_id

    frame_log: list[dict] = []
    st: _RunState | None = None
    twin_entry: dict | None = None
    prepared = False
    cancelled = False
    try:
        if replay_tape is not None:
            tape = CauseTape(
                use_case_id=replay_tape.use_case_id,
                seed=replay_tape.seed,
                t0_anchor_iso=t0.astimezone(timezone.utc).isoformat(),
                clock_scale=spec.clock_scale,
                movement_id=replay_tape.movement_id,
                frames=replay_tape.frames,  # re-posted VERBATIM — replay contract
                run_id=replay_tape.run_id,
            )
        else:
            tape = await _build_live_tape(
                deps, ResolvedRun(spec=spec, seed=seed, t0_anchor=t0,
                                  spec_hash=resolved.spec_hash),
                use_case, profile, run_id,
            )
        st = _RunState(
            tape=tape, run_id=run_id, is_replay=is_replay, replay_of=replay_of,
            clock_scale=spec.clock_scale, wall0=t0.timestamp(),
            use_case=use_case, profile=profile,
        )
        control.event_id = tape.movement_id
        control.progress.total = len(tape.frames)
        if deps.store is not None and run_id is not None:
            await deps.store.update_run_tape(run_id, tape.to_json())
            # the total in the row at once: a run killed before its first
            # heartbeat is reaped with {frame: 0, total: N}, not 0 of 0
            await _beat(deps, run_id, control)
        # C-N: the scenario's premise (its FIR-named stolen plate IS
        # reported stolen) holds before the first camera read lands
        await _ensure_scenario_premise(deps, st, frame_log)
        prepared = True

        twin_task: asyncio.Task | None = None
        if spec.deliver_twin and deps.twin is not None:
            twin_task = asyncio.create_task(_deliver_twin(deps, st))
        else:
            frame_log.append({"frame": None, "kind": "twin_post", "result": "skipped",
                              "reason": "deliver_twin=False" if not spec.deliver_twin
                              else "twin seam not configured"})
        heartbeat = (
            asyncio.create_task(_heartbeat(deps, run_id, control))
            if deps.store is not None and run_id is not None else None
        )
        try:
            await _play(deps, spec, st, control, frame_log)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
            if twin_task is not None:
                twin_entry = await _await_twin(twin_task, control)
                frame_log.append(twin_entry)
    except asyncio.CancelledError:
        # hard cancel (a shutdown that could not wait): finalize, re-raise
        control.request_stop("interrupted")
        cancelled = True
    except Exception as exc:  # noqa: BLE001 - the run fails, the row closes
        logger.error("runner.run_failed", run_id=run_id, error=str(exc))
        frame_log.append({"frame": None, "kind": "run", "result": "failed",
                          "reason": f"{type(exc).__name__}: {exc}"})

    status, reason = _final_status(control, st, deps, frame_log, prepared)
    if st is not None and st.event_open:
        why = {
            "stopped": f"emulator run {run_id} stopped by operator",
            "interrupted": f"emulator run {run_id} interrupted (emulator shut down)",
        }.get(status, f"emulator run {run_id} ended ({status})")
        await _close_run_event(deps, st, control, why, frame_log, None)

    delivery = _summarize(frame_log)
    delivery["progress"] = control.progress.to_dict()
    twin_doc = None
    if twin_entry is not None:
        twin_doc = {k: v for k, v in twin_entry.items() if k not in ("frame", "kind")}
    if deps.store is not None and run_id is not None:
        kept, dropped = cap_frame_log(frame_log)
        doc: dict[str, Any] = {
            "delivery": delivery,
            "progress": control.progress.to_dict(),
            "frame_log": kept,
            "frame_log_dropped": dropped,
            "event_id": st.tape.movement_id if st else None,
            "event_status": control.event_status,
        }
        if st is not None and st.movement_created:
            # how the event went live: at t0, late (after an outage), never
            doc["event_activation"] = {
                "active": st.event_active, "attempts": st.activate_attempts,
                "late": st.event_active and st.activate_attempts > 1,
                "error": st.activate_error,
            }
        if twin_doc is not None:
            doc["twin"] = twin_doc
        if st is not None and st.preconditions:
            doc["scenario_precondition"] = list(st.preconditions)
        if reason:
            doc["reason"] = reason
        try:
            await deps.store.finish_run(run_id, status, doc)
        except Exception as exc:  # noqa: BLE001
            logger.error("runner.ledger_finish_failed", error=str(exc))
    if cancelled:
        raise asyncio.CancelledError()

    return RunOutcome(
        run_id=run_id, status=status, tape=st.tape if st else None,
        movement_id=st.tape.movement_id if st else None,
        delivery=delivery, frame_log=frame_log, twin=twin_doc, reason=reason,
    )


def _project_rows(
    doc: dict[str, Any], anchors: dict[int, tuple[float, float]]
) -> list[tuple[int, float, float, float, float]]:
    """Project one inputs doc onto per-segment synthetic observations, each
    at its segment's on-road anchor (the midpoint along the line)."""
    cong = float(doc.get("predicted_congestion_idx", 0.0))
    rows: list[tuple[int, float, float, float, float]] = []
    for sid, (lon, lat) in sorted(anchors.items()):
        speed = max(5.0, 50.0 * (1.0 - 0.85 * cong))
        rows.append((sid, lon, lat, round(speed, 2), round(cong, 4)))
    return rows


#: The machine-readable reason a replay is refused (API body ``reason``).
REPLAY_OFF_NETWORK = "route_not_on_current_network"


async def replay_refusal(
    store: Any, tape: CauseTape, run_id: int | None = None
) -> dict[str, Any] | None:
    """Why a stored tape must NOT be replayed on this store — None when it
    may be.

    A replay re-posts its tape VERBATIM: the event is re-created, live
    again, on the route the tape recorded, its movement re-drawn on
    today's roads.  A recorded route that is not a legal path on the
    CURRENT network — a run recorded before the WP-20 conversion put the
    roads on OpenStreetMap — would come back to life off the roads: a
    route line bridging the gaps with straight jumps.  The answer is to
    refuse, not to re-route: a re-routed tape is a new run in disguise
    (its camera reads were timed for roads it would no longer take), and a
    new run of the use case is built on the current network.  A tape that
    creates no event re-animates nothing and is never refused here."""
    route = recorded_route(tape)
    if route is None or store is None:
        return None
    problems = await store.route_problems(route)
    if not problems:
        return None
    which = f"run {run_id}" if run_id is not None else "this tape"
    return {
        "detail": (
            f"{which} cannot be replayed: its recorded route {route} is not a legal path on "
            f"the current road network ({'; '.join(problems)}) — a replay would re-animate "
            f"its event off the roads. Start a new run of {tape.use_case_id} instead: it is "
            f"built on the current network."),
        "reason": REPLAY_OFF_NETWORK,
        "run_id": run_id,
        "use_case_id": tape.use_case_id,
        "route_segment_ids": route,
        "problems": problems,
    }


async def execute_replay(
    deps: RunnerDeps,
    *,
    tape: CauseTape,
    clock_scale: float,
    deliver_nats: bool = True,
    deliver_twin: bool = True,
    replay_of: int | None = None,
    t0: datetime | None = None,
    library: dict[str, UseCase] | None = None,
    run_id: int | None = None,
    control: RunControl | None = None,
) -> RunOutcome:
    """Re-post a stored tape verbatim with a re-anchored t0 (replay mode)."""
    lib = library if library is not None else load_library()
    spec = EmulationSpec(
        use_case_id=tape.use_case_id,
        seed=tape.seed,
        t0_anchor=None,
        clock_scale=clock_scale,
        deliver_nats=deliver_nats,
        deliver_twin=deliver_twin,
    )
    anchor = t0 or utcnow()
    return await execute_run(
        deps, spec, library=lib, replay_of=replay_of,
        replay_tape=tape, replay_t0=anchor, run_id=run_id, control=control,
    )
