"""Live Management Workflow — real-time corridor-event tracking and adaptation (R9).

Active while the corridor event is live (protected movement escort,
procession crawl, race sweep, egress window — the loop is event-agnostic).
It runs as one background task per movement (single-flight, see
:class:`EscortRegistry`): POST /movements/{id}/escort answers 202 at once and
GET /movements/{id}/escort reads the status this loop publishes (contract C-J).

    1. Tracks the convoy along its primary route. Every fix — real GPS
       (POST /movements/{id}/position) or the EMULATED walker in gps_sim
       (VVIP_ESCORT_SIM_GPS=1) — is projected onto the route geometry for
       progress, ETA and the segments ahead. A missing or stale fix is
       ``waiting_for_gps`` with a timeout that FAILS the escort; a missing
       fix never completes it. Arrival is the end of the route.
    2. Departure: the first live fix is the planned -> live phase
       transition. ONE decider checkpoint (trigger ``phase_transition``)
       sees the whole route's live congestion and orders the departure
       posture — proceed, hold the departure until the corridor ahead
       clears, or switch to the pre-sanitised Plan B spine.
    3. Monitors traffic on the route segments ahead (numeric, LLM-free per
       tick) with the ADAPTIVE congestion threshold (fail-closed to
       BASE_CONGESTION_SPIKE_THRESHOLD). Every tool call is isolated: a
       failure reads as "no data" and is audited once per failure streak.
       The status document says which feed the numbers came from
       (``traffic_feed.data_quality``: real / synthetic), so a simulated
       feed is labelled as such wherever the escort is shown.
    4. Incidents: the RISING EDGE of a threshold crossing (hysteresis band,
       cooldown) raises ONE decider checkpoint per crossing, trigger
       ``incident_detected``; the crossing itself is logged as an observation
       row. A crossing inside the cooldown is deferred, never dropped: if the
       corridor is still above the threshold when the cooldown ends, it is
       raised then as the next incident. A crossing clears only after the
       corridor has read below threshold - hysteresis on every reporting tick
       for ESCORT_CLEAR_CONFIRM_SEC (one low sample is not a clear). The
       checkpoint waits for the decider's per-movement cadence instead of
       being rate-limited, and a rate-limited outcome never raises a
       decider_unavailable alert. Silence (no observations) neither raises
       nor clears an incident.
    5. ESCORT AUTHORITY — "officer approves every switch" (user decision,
       2026-10-23). The decider only PROPOSES: an answer that would change
       the plan — a hold (or a held departure), Plan B (splice the
       pre-sanitised alternate spine), an emergency re-route (a
       deterministic solver call that avoids the hot segments and passes
       the class lane floor) and the closures a new route brings — becomes
       a PROPOSAL whatever its confidence; only "continue monitoring" /
       "proceed" (no change) is carried out on its own. A proposal is
       computed in full when it is raised (the exact route, the closures it
       adds and lifts, km left before and after, the decider tier and
       confidence) and carries ``expires_at``: ONE open proposal per escort
       (a newer one supersedes the older, audited), persisted as the
       decisions_log row ``operator_approval_required`` and the C-J field
       ``pending_approval``. A named officer approves or rejects it (POST
       /movements/{id}/escort/approvals/{approval_id}; the deck-verified
       x-veer-officer is the actor): an approval applies exactly what the
       officer saw — the route switch, its C-O spine update and closures —
       or, when the convoy has rolled past the route's branch point, is
       re-proposed instead of improvised; a rejection is recorded. With no
       answer in VVIP_ESCORT_APPROVAL_TIMEOUT_SEC the proposal EXPIRES
       ("no officer response — current plan held"): the convoy keeps
       executing its current approved route and closures — it does not
       stop and nothing switches. A later crossing may propose again.
       Low-confidence, withheld (novel) and fallback answers raise the same
       proposal with their reason. An approved hold waits on ITS OWN
       segments (watched even beyond the lookahead) until they clear, the
       convoy passes them or a route switch leaves them; it is bounded by
       ESCORT_HOLD_MAX_SEC, and one that expires on segments still above the
       threshold is re-decided as a new incident. The emulated convoy halts
       while held; a real one is advised by radio. Every checkpoint audit is
       persisted to decisions_log, and every row measured on the traffic
       feed carries its ``data_quality`` (``simulated`` when synthetic).
    6. Closures: just-in-time windows are recomputed from the ETA every tick
       (activate_at / deactivate_at in the status document) and a closure is
       lifted once the convoy has passed its segment, by route order. The ETA
       is the remaining route priced segment by segment at each segment's
       expected speed (its limit; capped at the emulated cruise for the
       walker) — never the whole distance at the instantaneous speed.
    7. The corridor event stays authoritative: an escort whose event is
       already cancelled / completed when it starts ends 'failed', and the
       loop re-reads the event about every ESCORT_EVENT_CHECK_SEC — an
       officer who cancels or completes it on /ops stops the escort, lifts
       its closures and closes the movement (audited
       'event_closed_externally', naming the officer). A re-route or Plan B
       publishes the new spine as the event's primary (C-O), best effort.

The ACTIVATION_LOOKAHEAD_SEC constant that lived here was dead code (sole
reference in the repo) and is intentionally deleted; the real application
points are the adaptive parameters threaded into the plan_diversions MCP call
and this loop's poll interval / spike threshold.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from convoy_brain.adaptive_params import AdaptiveParamsClient
from convoy_brain.agents.diversion_coordinator import DiversionCoordinatorAgent
from convoy_brain.agents.route_planner import RoutePlannerAgent
from convoy_brain.agents.security_liaison import fetch_segment_facts, lane_audit
from convoy_brain.city_link import (
    CITY_LINK_TIMEOUT_SEC,
    CLOSURE_TYPE,
    SYSTEM_ACTOR,
    CityLink,
    current_event_id,
)
from convoy_brain.config import (
    BASE_CONGESTION_SPIKE_THRESHOLD,
    BASE_GPS_POLL_INTERVAL_SEC,
    ESCORT_ARRIVAL_RADIUS_M,
    ESCORT_CLEAR_CONFIRM_SEC,
    ESCORT_CONGESTION_HYSTERESIS,
    ESCORT_ETA_FALLBACK_KMH,
    ESCORT_EVENT_CHECK_SEC,
    ESCORT_GPS_STALE_SEC,
    ESCORT_GPS_TIMEOUT_SEC,
    ESCORT_HOLD_MAX_SEC,
    ESCORT_INCIDENT_COOLDOWN_SEC,
    ESCORT_LOOKAHEAD_M,
    ESCORT_MAX_ITERATIONS,
    ESCORT_OFF_ROUTE_M,
    ESCORT_REROUTE_MAX_DETOUR,
    baseline_params,
    escort_approval_timeout_sec,
    escort_sim_speed_kmh,
)
from convoy_brain.decider import CheckpointResult, DeciderBrain
from convoy_brain.gps_sim import (
    PositionFix,
    RouteGeometry,
    RouteLocation,
    fetch_segment_details,
    geometry_paces,
    geometry_parts,
    load_route_geometry,
)
from convoy_brain.http_guard import assert_platform_service_url
from convoy_brain.llm.heuristics import (
    dedupe_route_candidates,
    heuristic_route_ranking,
    heuristic_traffic_assessment,
    live_observation_rows,
)
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.mcp.server import ToolExecutor
from convoy_brain.memory.convoy_context import (
    ConvoyContext,
    ConvoyContextStore,
    mutate_context,
)
from convoy_brain.metrics import (
    ESCORT_DEPARTURES,
    ESCORT_INCIDENTS,
    ESCORT_TASKS_RUNNING,
    ESCORT_TICKS,
    track_decider_checkpoint,
    track_mcp_tool,
)

logger = structlog.get_logger(__name__)

# GPS update interval (seconds) — fail-closed default; the adaptive tier may
# publish a per-movement override (>=1s protocol floor).
GPS_POLL_INTERVAL_SEC = BASE_GPS_POLL_INTERVAL_SEC

# Congestion spike threshold for triggering re-route assessment — fail-closed
# default; adaptive may override per movement.
CONGESTION_SPIKE_THRESHOLD = BASE_CONGESTION_SPIKE_THRESHOLD

# Maximum iterations before forcing exit (safety valve: ~4 h at 1s ticks)
MAX_MONITOR_ITERATIONS = ESCORT_MAX_ITERATIONS

#: The decider's incident menu (one /decide per crossing; <= 6 questions).
INCIDENT_ACTIONS = (
    "continue_monitoring",
    "hold_position",
    "activate_plan_b",
    "emergency_reroute",
    "escalate_operator",
)
#: The decider's departure menu (one phase_transition checkpoint per escort).
DEPARTURE_ACTIONS = ("proceed", "hold_departure", "activate_plan_b")
#: Departure answers as the escort actions that carry them out.
DEPARTURE_EXECUTES = {
    "proceed": "continue_monitoring",
    "hold_departure": "hold_position",
    "activate_plan_b": "activate_plan_b",
}
#: Checkpoint tiers whose answer counts as the decider's own (a confident
#: answer from them is still only a PROPOSAL when it changes the plan).
TRUSTED_TIERS = ("decider", "tier0_cache_hit")
#: The escort's authority rule, published on the C-J status document.
ESCORT_AUTHORITY = "officer_approves_every_switch"
#: Escort actions that change the approved plan (route, closures, a hold):
#: never carried out without a named officer's approval.
PLAN_CHANGING_ACTIONS = ("hold_position", "activate_plan_b", "emergency_reroute")
#: Proposal kind per recommended action (the deck's label).
PROPOSAL_KINDS = {
    "activate_plan_b": "plan_b",
    "emergency_reroute": "reroute",
    "hold_position": "hold",
    "continue_monitoring": "continue",
}
#: What an expired proposal means for the convoy (audit + C-J outcome).
APPROVAL_TIMEOUT_REASON = "no officer response — current plan held"
#: Decided / expired / superseded proposals the status document keeps.
MAX_APPROVAL_OUTCOMES = 5
#: Proposal statuses (pending is the only open one).
APPROVAL_CLOSED = ("approved", "rejected", "expired", "superseded")
#: C-J terminal escort statuses.
TERMINAL_STATUSES = ("completed", "stopped", "failed")
#: Route geometry is retried this often when it could not be loaded.
ROUTE_RETRY_SEC = 30.0
#: Finished escort runs the registry keeps in memory; older ones are dropped
#: (GET /movements/{id}/escort then serves the persisted status document).
MAX_FINISHED_RUNS = 64
#: Corridor-event statuses after which no escort may run on the event.
CLOSED_EVENT_STATUSES = ("cancelled", "completed")


def utc_iso(epoch: float | None = None) -> str:
    """ISO-8601 UTC with milliseconds and a Z suffix (contract time format)."""
    moment = datetime.fromtimestamp(epoch, timezone.utc) if epoch is not None \
        else datetime.now(timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# ---------------------------------------------------------------------------
# Plan / context readers
# ---------------------------------------------------------------------------


def plan_row(ctx: ConvoyContext | None) -> dict | None:
    """The newest pre-movement plan row of the audit trail."""
    for row in reversed(getattr(ctx, "decisions_log", None) or []):
        if isinstance(row, dict) and row.get("phase") == "pre_movement":
            return row
    return None


def plan_route_segments(ctx: ConvoyContext | None) -> list[int]:
    """The planned primary route (segment ids in travel order), or []."""
    row = plan_row(ctx)
    ids = row.get("primary_segment_ids") if row else None
    out: list[int] = []
    for sid in ids or []:
        try:
            out.append(int(sid))
        except (TypeError, ValueError):
            continue
    return out


def resumable_route(
    persisted: dict | None, ctx: ConvoyContext | None,
) -> tuple[list[int], str] | None:
    """The route an earlier run of this escort switched to (re-route / Plan B).

    A resumed escort (after a stop or a brain restart) continues on it
    instead of snapping back to the planned primary — but only when that
    run started after the movement's newest plan; a re-plan wins.
    Returns ``(segment_ids, source)`` or None.
    """
    if not isinstance(persisted, dict):
        return None
    route = persisted.get("route")
    if not isinstance(route, dict) or route.get("source") in (None, "plan"):
        return None
    ids: list[int] = []
    for sid in route.get("segment_ids") or []:
        try:
            ids.append(int(sid))
        except (TypeError, ValueError):
            return None
    started = _epoch(persisted.get("started_at"))
    row = plan_row(ctx)
    planned_at = row.get("timestamp") if row else None
    if not ids or started is None:
        return None
    if isinstance(planned_at, (int, float)) and started < planned_at:
        return None
    return ids, str(route["source"])


def feed_label(live: dict | None, rows: list[dict], watched: list[int]) -> dict:
    """Which feed the escort's congestion numbers came from.

    The oracle tags every observation with its ``data_quality`` (real /
    anomalous / synthetic); a synthetic row means the numbers come from a
    SIMULATED feed and the status document says so.
    """
    observations = (live or {}).get("observations") if isinstance(live, dict) else None
    qualities = sorted({
        str(o["data_quality"]) for o in observations or []
        if isinstance(o, dict) and o.get("data_quality")
    })
    return {
        "segments_watched": len(watched),
        "segments_reporting": len({r["segment_id"] for r in rows}),
        "data_quality": qualities,
        "simulated": "synthetic" in qualities,
    }


def segment_qualities(live: dict | None) -> dict[int, str]:
    """``{segment_id: data_quality}`` of a live-traffic response (labelled rows)."""
    out: dict[int, str] = {}
    observations = live.get("observations") if isinstance(live, dict) else None
    for obs in observations if isinstance(observations, list) else []:
        if not isinstance(obs, dict) or not obs.get("data_quality"):
            continue
        try:
            out[int(obs.get("segment_id"))] = str(obs["data_quality"])
        except (TypeError, ValueError):
            continue
    return out


def feed_fields(qualities: dict[int, str], segment_ids: Iterable[int]) -> dict:
    """The feed label of the rows behind ``segment_ids``, for an audit row.

    Every escort row measured on the traffic feed (a crossing, a checkpoint,
    a hold, an approval) carries ``data_quality`` and ``simulated``, so a
    decision taken on the synthetic feed is never shown as live data.
    """
    labels = sorted({qualities[s] for s in segment_ids if s in qualities})
    return {"data_quality": labels, "simulated": "synthetic" in labels}


def fix_of(ctx: ConvoyContext | None) -> PositionFix | None:
    """The context's latest fix (None before the first one)."""
    if ctx is None or not ctx.convoy_position:
        return None
    lon, lat = ctx.convoy_position[0], ctx.convoy_position[1]
    return PositionFix(
        lon=float(lon),
        lat=float(lat),
        speed_kmh=ctx.convoy_speed_kmh,
        heading=getattr(ctx, "convoy_heading", None),
        t=getattr(ctx, "position_t", None) or "",
        source=getattr(ctx, "position_source", None) or "gps",
    )


# ---------------------------------------------------------------------------
# Escort status (contract C-J) and the run handle
# ---------------------------------------------------------------------------


@dataclass
class EscortState:
    """The C-J status document (plus additive detail fields)."""

    movement_id: str
    event_id: str | None = None
    status: str = "idle"  # idle | waiting_for_gps | running | completed | stopped | failed
    iteration: int = 0
    position: dict | None = None
    progress: float | None = None
    eta_sec: float | None = None
    active_diversions: list[dict] = field(default_factory=list)
    last_decision: dict | None = None
    started_at: str | None = None
    updated_at: str | None = None
    detail: str | None = None
    # additive
    task_id: str | None = None
    officer: str | None = None
    ended_at: str | None = None
    route: dict | None = None
    current_segment_id: int | None = None
    remaining_m: float | None = None
    offset_m: float | None = None
    gps: dict = field(default_factory=dict)
    hold: dict | None = None
    pending_approvals: list[dict] = field(default_factory=list)
    # The ONE open proposal (route switch / Plan B / hold / closures) a named
    # officer must approve before it runs; None when nothing awaits one.
    pending_approval: dict | None = None
    # The newest decided / expired / superseded proposals (newest last).
    approval_outcomes: list[dict] = field(default_factory=list)
    approval_timeout_sec: float | None = None
    escort_authority: str = ESCORT_AUTHORITY
    peak_congestion_ahead: float | None = None
    spike_threshold: float | None = None
    traffic_feed: dict | None = None
    incidents: int = 0
    plan_status: str | None = None

    def to_doc(self) -> dict:
        doc = asdict(self)
        for key in ("progress",):
            if doc[key] is not None:
                doc[key] = round(min(max(doc[key], 0.0), 1.0), 4)
        for key in ("eta_sec", "remaining_m", "offset_m"):
            if doc[key] is not None:
                doc[key] = round(doc[key], 1)
        return doc


class EscortRun:
    """One escort's runtime handle: status, route, controls, tasks."""

    def __init__(
        self,
        movement_id: str,
        *,
        task_id: str | None = None,
        officer: str | None = None,
        route_segment_ids: list[int] | None = None,
        route_source: str = "plan",
        destination: tuple[float, float] | None = None,
        sim_gps: bool = False,
        claimed_officer: str | None = None,
    ) -> None:
        self.movement_id = movement_id
        self.task_id = task_id or uuid.uuid4().hex
        self.officer = officer
        # A body officer that differs from the deck-verified header officer
        # (kept for the audit; the header officer is the actor).
        self.claimed_officer = claimed_officer
        self.state = EscortState(movement_id=movement_id, task_id=self.task_id, officer=officer)
        self.route: RouteGeometry | None = None
        self.route_segment_ids = [int(s) for s in (route_segment_ids or [])]
        # "plan" for the planned primary; "reroute" / "plan_b" when a resumed
        # run continues on the route an earlier run switched to.
        self.route_source = route_source
        self.alternates: list[list[int]] = []
        self.destination = destination
        self.sim_gps = sim_gps
        self.real_gps_seen = False
        self.hold: dict | None = None
        self.last_location: RouteLocation | None = None
        self.details_cache: dict[int, dict] = {}
        self.approvals: dict[str, dict] = {}
        self.operator_actions: list[dict] = []
        # Monotonic seconds for proposal expiry; the loop rebinds it to its
        # own (injectable) clock so an approval and the loop agree on time.
        self.clock: Callable[[], float] = time.monotonic
        self.task: asyncio.Task | None = None
        self.sim_task: asyncio.Task | None = None
        self.stop_reason: str | None = None
        self.stop_officer: str | None = None
        self._stop = asyncio.Event()

    # -- controls ----------------------------------------------------------------

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    @property
    def holding(self) -> bool:
        return self.hold is not None

    @property
    def finished(self) -> bool:
        return self.state.status in TERMINAL_STATUSES

    @property
    def alive(self) -> bool:
        return self.task is not None and not self.task.done()

    def request_stop(self, reason: str, officer: str | None = None) -> None:
        if not self._stop.is_set():
            self.stop_reason = reason
            self.stop_officer = officer
            self._stop.set()

    async def wait_stop(self, timeout: float) -> None:
        """Sleep up to ``timeout`` seconds, waking early on a stop request."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, timeout))

    def note_fix(self, fix: PositionFix) -> None:
        """A real fix retires the emulated walker for good."""
        if fix.source == "gps":
            self.real_gps_seen = True

    def locate(self, lon: float, lat: float) -> RouteLocation | None:
        if self.route is None:
            return None
        hint = self.last_location.distance_m if self.last_location else None
        return self.route.locate(lon, lat, hint_m=hint)

    def decide(
        self, approval_id: str, decision: str, officer: str,
        claimed_officer: str | None = None,
    ) -> tuple[dict | None, str]:
        """Record a named officer's answer to a proposal.

        Returns ``(row, outcome)``: ``decided`` (approved / rejected now),
        ``expired`` (it ran out before this answer — the caller audits the
        expiry), ``closed`` (already approved, rejected, expired or
        superseded — never decided twice) or ``unknown`` (row None).
        An approval queues the proposal; the escort loop carries it out on
        its next tick (route changes stay inside the loop task).
        """
        row = self.approvals.get(approval_id)
        if row is None:
            return None, "unknown"
        if row["status"] != "pending":
            return row, "closed"
        if self._overdue(row, self.clock()):
            self._close(row, "expired", reason=APPROVAL_TIMEOUT_REASON)
            self._refresh_pending()
            return row, "expired"
        approved = decision == "approve"
        row["status"] = "approved" if approved else "rejected"
        row["decided_by"] = officer
        row["decided_at"] = utc_iso()
        if claimed_officer:
            row["claimed_officer"] = claimed_officer
        if approved:
            self.operator_actions.append(dict(row))
        self._refresh_pending()
        return row, "decided"

    def approve(self, approval_id: str, decision: str, officer: str) -> dict | None:
        """:meth:`decide` without the outcome (None for an unknown id)."""
        return self.decide(approval_id, decision, officer)[0]

    @staticmethod
    def _overdue(row: dict, now: float) -> bool:
        due = row.get("expires_mono")
        return isinstance(due, (int, float)) and now >= due

    def _close(self, row: dict, status: str, *, reason: str, by: str = "system") -> None:
        row["status"] = status
        row["decided_by"] = by
        row["decided_at"] = utc_iso()
        row["expiry_reason"] = reason

    def expire_overdue(self, now: float) -> list[dict]:
        """Expire every proposal past its ``expires_at`` (no officer answered):
        the convoy holds its current approved plan."""
        expired = [
            row for row in self.approvals.values()
            if row["status"] == "pending" and self._overdue(row, now)
        ]
        for row in expired:
            self._close(row, "expired", reason=APPROVAL_TIMEOUT_REASON)
        if expired:
            self._refresh_pending()
        return expired

    def expire_pending(self, reason: str) -> list[dict]:
        """Retire every still-pending approval (its situation is gone)."""
        expired = []
        for row in self.approvals.values():
            if row["status"] == "pending":
                self._close(row, "expired", reason=reason)
                expired.append(row)
        if expired:
            self._refresh_pending()
        return expired

    def supersede_pending(self, by_id: str, reason: str) -> list[dict]:
        """One open proposal per escort: a newer one retires the older."""
        superseded = []
        for row in self.approvals.values():
            if row["status"] == "pending" and row["approval_id"] != by_id:
                self._close(row, "superseded", reason=reason)
                row["superseded_by"] = by_id
                superseded.append(row)
        if superseded:
            self._refresh_pending()
        return superseded

    def _refresh_pending(self) -> None:
        pending = [_approval_view(a) for a in self.approvals.values() if a["status"] == "pending"]
        self.state.pending_approvals = pending[-5:]
        self.state.pending_approval = pending[-1] if pending else None
        closed = [a for a in self.approvals.values() if a["status"] in APPROVAL_CLOSED]
        closed.sort(key=lambda a: a.get("decided_at") or "")
        self.state.approval_outcomes = [_outcome_view(a) for a in closed[-MAX_APPROVAL_OUTCOMES:]]


def _approval_view(row: dict) -> dict:
    """A pending proposal as the status document shows it (no traffic rows)."""
    view = {
        "approval_id": row["approval_id"],
        "action": row["action"],
        "kind": row.get("kind") or PROPOSAL_KINDS.get(row["action"], "action"),
        "summary": row.get("summary"),
        "reason": row.get("why") or row["reason"],
        "raised_at": row["raised_at"],
        "created_at": row.get("created_at") or row["raised_at"],
        "expires_at": row.get("expires_at"),
        "timeout_sec": row.get("timeout_sec"),
        "status": row["status"],
        "trigger": row.get("trigger"),
        "tier": row.get("tier"),
        "confidence": row.get("confidence"),
        "segment_ids": list(row.get("segment_ids") or []),
    }
    # What approving it would do (route, closures added / lifted, km), the
    # detour drawn as a preview, and the feed its numbers came from
    # (``simulated`` -> the deck labels it SIMULATED FEED).
    for key in ("proposed", "peak_congestion", "detour_m", "remaining_m", "detour_segment_ids",
                "data_quality", "simulated", "checkpoint_tier"):
        if row.get(key) is not None:
            view[key] = row[key]
    return view


def _outcome_view(row: dict) -> dict:
    """A decided / expired / superseded proposal (the HUD's outcome line)."""
    view = {
        "approval_id": row["approval_id"],
        "action": row["action"],
        "kind": row.get("kind") or PROPOSAL_KINDS.get(row["action"], "action"),
        "summary": row.get("summary"),
        "status": row["status"],
        "decided_by": row.get("decided_by"),
        "decided_at": row.get("decided_at"),
    }
    for key in ("claimed_officer", "expiry_reason", "superseded_by", "applied", "applied_detail"):
        if row.get(key) is not None:
            view[key] = row[key]
    return view


class EscortAlreadyRunningError(RuntimeError):
    """Single-flight guard: an escort task is already running for the movement."""

    def __init__(self, run: EscortRun) -> None:
        super().__init__(f"escort already running for {run.movement_id}")
        self.run = run


class EscortRegistry:
    """Single-flight registry of escort tasks, keyed by movement id."""

    def __init__(self) -> None:
        self._runs: dict[str, EscortRun] = {}

    def get(self, movement_id: str) -> EscortRun | None:
        return self._runs.get(movement_id)

    def active(self, movement_id: str) -> EscortRun | None:
        run = self._runs.get(movement_id)
        return run if run is not None and run.alive else None

    def running_count(self) -> int:
        return sum(1 for run in self._runs.values() if run.alive)

    def runs(self) -> list[EscortRun]:
        """Every run the registry holds (running and recently finished)."""
        return list(self._runs.values())

    def start(
        self,
        run: EscortRun,
        main: Callable[[EscortRun], Awaitable[Any]],
    ) -> EscortRun:
        """Register and launch ``main(run)``; raises when one is already alive.

        Check and insert happen with no await in between, so two concurrent
        POSTs can never both start a loop for the same movement.
        """
        current = self.active(run.movement_id)
        if current is not None:
            raise EscortAlreadyRunningError(current)
        # Re-insert so the dict order stays "least recently started first".
        self._runs.pop(run.movement_id, None)
        self._runs[run.movement_id] = run
        run.task = asyncio.create_task(main(run), name=f"escort:{run.movement_id}")
        run.task.add_done_callback(lambda _t: ESCORT_TASKS_RUNNING.set(self.running_count()))
        ESCORT_TASKS_RUNNING.set(self.running_count())
        self.prune()
        return run

    def prune(self, keep: int = MAX_FINISHED_RUNS) -> None:
        """Drop all but the ``keep`` most recently started finished runs, so
        a long-lived brain does not hold every escort it ever ran."""
        finished = [mid for mid, run in self._runs.items() if not run.alive]
        for mid in finished[: max(0, len(finished) - keep)]:
            del self._runs[mid]

    async def stop(
        self, movement_id: str, reason: str, officer: str | None = None, timeout: float = 5.0,
    ) -> EscortRun | None:
        """Ask a running escort to stop and wait (bounded) for it to settle."""
        run = self.active(movement_id)
        if run is None:
            return self._runs.get(movement_id)
        run.request_stop(reason, officer)
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(run.task), timeout=timeout)
        return run

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Stop every escort (brain shutdown); stragglers are cancelled."""
        alive = [run for run in self._runs.values() if run.alive]
        for run in alive:
            run.request_stop("brain shutting down", SYSTEM_ACTOR)
        if alive:
            _done, pending = await asyncio.wait([r.task for r in alive], timeout=timeout)
            for task in pending:
                task.cancel()
        ESCORT_TASKS_RUNNING.set(0)


# ---------------------------------------------------------------------------
# Corridor-event lifecycle (contract C-F) for escort start / clear
# ---------------------------------------------------------------------------


class EventLifecycle:
    """Thin async client for POST /api/v1/events/{id}/status and the
    event's route spines. Nothing here raises."""

    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        timeout_sec: float = CITY_LINK_TIMEOUT_SEC,
    ) -> None:
        self._base_url = assert_platform_service_url(base_url.rstrip("/"))
        self._timeout = timeout_sec
        self._owns_client = client is None
        self._client = client

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def transition(
        self, event_id: str | None, to_status: str, officer: str, note: str,
    ) -> dict:
        """Move the event; ``outcome`` is transitioned | already | conflict |
        not_found | rejected | failed | skipped (no event)."""
        if not event_id:
            return {"outcome": "skipped", "event_id": None, "to_status": to_status,
                    "reason": "movement has no registered corridor event"}
        body = {"to_status": to_status, "officer": officer, "note": note[:500]}
        try:
            client = await self._ensure_client()
            resp = await client.post(
                f"{self._base_url}/api/v1/events/{event_id}/status",
                json=body, timeout=self._timeout,
            )
        except Exception as exc:
            logger.warning("escort.lifecycle_failed", event_id=event_id, error=str(exc)[:200])
            return {"outcome": "failed", "event_id": event_id, "to_status": to_status,
                    "reason": f"transport: {str(exc)[:200]}"}
        try:
            detail = resp.json()
        except ValueError:
            detail = {"detail": resp.text[:300]}
        if resp.status_code == 200:
            outcome = "transitioned"
        elif resp.status_code == 409:
            outcome = "already" if isinstance(detail, dict) and \
                detail.get("from_status") == to_status else "conflict"
        elif resp.status_code == 404:
            outcome = "not_found"
        elif 400 <= resp.status_code < 500:
            outcome = "rejected"
        else:
            outcome = "failed"
        logger.info("escort.lifecycle", event_id=event_id, to_status=to_status,
                    outcome=outcome, http_status=resp.status_code)
        return {
            "outcome": outcome,
            "event_id": event_id,
            "to_status": to_status,
            "http_status": resp.status_code,
            "from_status": detail.get("from_status") if isinstance(detail, dict) else None,
            "allowed": detail.get("allowed") if isinstance(detail, dict) else None,
        }

    async def status(self, event_id: str | None, since: str | None = None) -> dict:
        """The event's current status: ``{status, found, source}``. Never raises.

        The active board first (planning / approved / active); an event no
        longer on it is looked up in the history (GET /api/v1/events?since=,
        newest first). ``status`` None means it could not be read.
        """
        out: dict = {"status": None, "found": False, "source": None}
        if not event_id:
            return out
        try:
            client = await self._ensure_client()
            resp = await client.get(f"{self._base_url}/api/v1/events/active", timeout=self._timeout)
            resp.raise_for_status()
            for event in resp.json().get("events") or []:
                if isinstance(event, dict) and str(event.get("event_id")) == str(event_id):
                    return {"status": event.get("status"), "found": True, "source": "active"}
            params: dict = {"limit": 200}
            if since:
                params["since"] = since
            resp = await client.get(
                f"{self._base_url}/api/v1/events", params=params, timeout=self._timeout
            )
            resp.raise_for_status()
            for event in resp.json().get("events") or []:
                if isinstance(event, dict) and str(event.get("event_id")) == str(event_id):
                    return {"status": event.get("status"), "found": True, "source": "history"}
        except Exception as exc:
            logger.info("escort.event_status_unavailable", event_id=event_id, error=str(exc)[:200])
            return out
        return dict(out, source="absent")

    async def closed_by(self, event_id: str, to_status: str) -> str | None:
        """Who moved the event to ``to_status`` (its audit trail), or None."""
        try:
            client = await self._ensure_client()
            resp = await client.get(
                f"{self._base_url}/api/v1/events/{event_id}/audit",
                params={"limit": 50}, timeout=self._timeout,
            )
            resp.raise_for_status()
            items = resp.json().get("items") or []
        except Exception as exc:
            logger.info("escort.event_audit_unavailable", event_id=event_id, error=str(exc)[:200])
            return None
        for item in items:  # newest first
            props = item.get("props") if isinstance(item, dict) else None
            if (isinstance(props, dict) and props.get("to_status") == to_status
                    and item.get("actor")):
                return str(item["actor"])[:64]
        return None

    async def spines(self, event_id: str | None) -> dict[str, list[int]]:
        """``{label: segment_ids}`` of the event's route spines ({} on failure)."""
        if not event_id:
            return {}
        try:
            client = await self._ensure_client()
            resp = await client.get(f"{self._base_url}/api/v1/events/active", timeout=self._timeout)
            resp.raise_for_status()
            events = resp.json().get("events") or []
        except Exception as exc:
            logger.info("escort.spines_unavailable", event_id=event_id, error=str(exc)[:200])
            return {}
        for event in events:
            if isinstance(event, dict) and str(event.get("event_id")) == str(event_id):
                return {
                    str(r.get("label")): [int(s) for s in r.get("segment_ids") or []]
                    for r in event.get("routes") or []
                    if isinstance(r, dict) and r.get("label")
                }
        return {}


# ---------------------------------------------------------------------------
# Workflow entry point
# ---------------------------------------------------------------------------


async def run_live_escort_workflow(
    movement_id: str,
    *,
    destination: tuple[float, float] | None = None,
    bridge: LlamaBridge | None = None,
    executor: ToolExecutor | None = None,
    context_store: ConvoyContextStore | None = None,
    decider_brain: DeciderBrain | None = None,
    adaptive_client: AdaptiveParamsClient | None = None,
    max_iterations: int | None = None,
    run: EscortRun | None = None,
    lifecycle: EventLifecycle | None = None,
    publish: Callable[[EscortRun], Awaitable[None]] | None = None,
    poll_interval_sec: float | None = None,
    gps_timeout_sec: float | None = None,
    incident_cooldown_sec: float | None = None,
    hold_max_sec: float | None = None,
    clear_confirm_sec: float | None = None,
    clock: Callable[[], float] | None = None,
    city_link: CityLink | None = None,
    event_check_sec: float | None = None,
    approval_timeout_sec: float | None = None,
) -> dict:
    """Execute the live escort monitoring loop.

    Runs until the convoy reaches the end of its route (``completed``), the
    movement is stopped or cleared (``stopped``), or GPS never arrives /
    the context disappears (``failed``). GPS ticks are LLM-free; the decider
    runs once per congestion crossing, and the tactical agents fire only on
    the actions it orders.

    Args:
        movement_id: UUID of the active movement
        destination: (lon, lat) corridor destination, used to re-route a
            movement that has no planned route geometry
        bridge: LlamaBridge instance (required)
        executor: ToolExecutor instance (required)
        context_store: ConvoyContextStore instance (required)
        decider_brain: strategic tier (optional)
        adaptive_client: adaptive-parameter client (optional; fail-closed)
        max_iterations: test/integration override for the safety valve
        run: the registry's run handle (status, controls); a private one
            is made when absent
        lifecycle: corridor-event client — the escort start moves the
            movement's event to ``active`` (C-F) and reads its Plan B spines
        publish: called with the run after every tick (status persistence)
        poll_interval_sec / gps_timeout_sec / incident_cooldown_sec /
            hold_max_sec / clear_confirm_sec: test/integration overrides of
            the loop timings
        clock: monotonic-seconds source for those timings (tests)
        city_link: corridor-event writer — a re-route / Plan B publishes the
            new spine as the event's primary (C-O, best effort)
        event_check_sec: how often the loop re-reads the corridor event
            (an external cancel / completion stops the escort)
        approval_timeout_sec: how long a proposal waits for a named officer
            before it expires and the convoy holds its current plan
            (default VVIP_ESCORT_APPROVAL_TIMEOUT_SEC)

    Returns:
        Escort report. ``status`` is the C-J terminal status and ``arrived``
        says whether the convoy reached its destination; ``escort_complete``
        (legacy) is True when the run ended in order — arrival, a stop, or
        the safety valve — and False when it failed.
    """
    if bridge is None or executor is None or context_store is None:
        raise ValueError(
            "bridge, executor, and context_store are required. "
            "Pass them as keyword arguments."
        )
    run = run or EscortRun(movement_id, destination=destination)
    if run.destination is None and destination is not None:
        run.destination = destination
    loop = _EscortLoop(
        run=run,
        bridge=bridge,
        executor=executor,
        store=context_store,
        decider=decider_brain,
        adaptive_client=adaptive_client,
        lifecycle=lifecycle,
        publish=publish,
        poll_interval_sec=poll_interval_sec,
        gps_timeout_sec=gps_timeout_sec,
        incident_cooldown_sec=incident_cooldown_sec,
        hold_max_sec=hold_max_sec,
        clear_confirm_sec=clear_confirm_sec,
        clock=clock,
        city_link=city_link,
        event_check_sec=event_check_sec,
        approval_timeout_sec=approval_timeout_sec,
    )
    return await loop.execute(max_iterations)


@dataclass
class _Incident:
    """One congestion crossing awaiting (or past) its decider checkpoint."""

    peak: float
    hot: list[int]
    rows: list[dict]
    at: float  # monotonic
    t: str
    folded: int = 0
    attempts: int = 0
    trigger: str = "incident_detected"  # phase_transition for the departure
    # crossing | deferred_crossing (raised when the cooldown ended) |
    # hold_expired (a hold ran out on segments still congested) | departure
    origin: str = "crossing"
    detail: str | None = None
    deferred_from: str | None = None  # when the deferred crossing happened
    quality: dict = field(default_factory=dict)  # feed_fields() of the rows


@dataclass
class _RouteCandidate:
    """A route change computed for a proposal — nothing has switched yet."""

    route: RouteGeometry
    source: str  # plan_b | reroute
    route_id: str | None
    tail: list[int]  # from the convoy's segment on
    avoided: list[int]
    old_left_m: float  # routing metres left on the current route
    new_left_m: float  # ... and on the proposed one
    note: str | None = None  # e.g. why Plan B gave way to a re-route

    @property
    def long_detour(self) -> bool:
        """Longer than ESCORT_REROUTE_MAX_DETOUR times the distance left —
        still only a proposal; its figures say so."""
        return self.old_left_m > 0 and self.new_left_m > self.old_left_m * ESCORT_REROUTE_MAX_DETOUR


def _confidence_text(confidence: float | None) -> str:
    return f"confidence {confidence:.2f}" if isinstance(confidence, float) else "no confidence"


def closed_approval_row(row: dict, action: str) -> dict:
    """The audit row of a proposal that closed without being decided:
    ``approval_expired`` (no officer answered in time — the convoy held its
    current plan — or the situation it answered ended) or
    ``approval_superseded`` (a newer proposal replaced it)."""
    key = "superseded_action" if action == "approval_superseded" else "expired_action"
    out = {
        "phase": "live_escort",
        "action": action,
        "approval_id": row["approval_id"],
        key: row["action"],
        "kind": row.get("kind") or PROPOSAL_KINDS.get(row["action"], "action"),
        "summary": row.get("summary"),
        "reason": row.get("expiry_reason"),
    }
    if row.get("superseded_by"):
        out["superseded_by"] = row["superseded_by"]
    if row.get("expiry_reason") == APPROVAL_TIMEOUT_REASON:
        out["timeout_sec"] = row.get("timeout_sec")
    return out


class _EscortLoop:
    """The monitoring loop of one escort (see the module docstring)."""

    def __init__(
        self,
        *,
        run: EscortRun,
        bridge: LlamaBridge,
        executor: ToolExecutor,
        store: ConvoyContextStore,
        decider: DeciderBrain | None,
        adaptive_client: AdaptiveParamsClient | None,
        lifecycle: EventLifecycle | None,
        publish: Callable[[EscortRun], Awaitable[None]] | None,
        poll_interval_sec: float | None,
        gps_timeout_sec: float | None,
        incident_cooldown_sec: float | None,
        hold_max_sec: float | None,
        clear_confirm_sec: float | None = None,
        clock: Callable[[], float] | None = None,
        city_link: CityLink | None = None,
        event_check_sec: float | None = None,
        approval_timeout_sec: float | None = None,
    ) -> None:
        # Monotonic seconds for every loop timing (cooldown, debounce, hold,
        # GPS wait, proposal expiry); injectable so the timing tests need no
        # real sleeps. The run answers approvals on the same clock.
        self._clock = clock or time.monotonic
        run.clock = self._clock
        self.city_link = city_link
        self.event_check_sec = (
            event_check_sec if event_check_sec is not None else ESCORT_EVENT_CHECK_SEC
        )
        self.next_event_check = 0.0
        self.approval_timeout = (
            approval_timeout_sec if approval_timeout_sec is not None
            else escort_approval_timeout_sec()
        )
        run.state.approval_timeout_sec = self.approval_timeout
        self.run = run
        self.mid = run.movement_id
        self.bridge = bridge
        self.executor = executor
        self.store = store
        self.decider = decider
        self.adaptive_client = adaptive_client
        self.lifecycle = lifecycle
        self._publish_cb = publish
        self.poll_override = poll_interval_sec
        self.gps_timeout = (
            gps_timeout_sec if gps_timeout_sec is not None else ESCORT_GPS_TIMEOUT_SEC
        )
        self.cooldown = (
            incident_cooldown_sec if incident_cooldown_sec is not None
            else ESCORT_INCIDENT_COOLDOWN_SEC
        )
        self.hold_max = hold_max_sec if hold_max_sec is not None else ESCORT_HOLD_MAX_SEC
        self.clear_confirm = (
            clear_confirm_sec if clear_confirm_sec is not None else ESCORT_CLEAR_CONFIRM_SEC
        )
        self.vvip_class = "Z"
        self.event_kind = "protected_movement"
        self.cruise_kmh = escort_sim_speed_kmh()
        self.spike_threshold = CONGESTION_SPIKE_THRESHOLD
        self.poll = GPS_POLL_INTERVAL_SEC
        self.advance_closure_sec = 0
        self.adaptive = None
        self.coordinator: DiversionCoordinatorAgent | None = None
        self.iterations = 0
        self.started_mono = self._clock()
        self.waiting_since: float | None = None
        self.departed = False
        self.was_high = False
        # Start of the current run of clear readings (falling-edge debounce).
        self.clear_since: float | None = None
        self.last_incident_at: float | None = None
        self.pending: _Incident | None = None
        # A crossing the cooldown deferred ({"at", "t"}) and the crossings
        # folded into the next incident since the last one was raised.
        self.deferred: dict | None = None
        self.folded_crossings = 0
        self.off_route = False
        self.tool_failures: dict[str, int] = {}
        self.store_failures = 0
        self.route_retry_at = 0.0
        self.side_tasks: set[asyncio.Task] = set()
        self.reroutes = 0

    # -- plumbing ----------------------------------------------------------------

    async def _append(self, row: dict) -> None:
        """Audit row (isolated: a store hiccup never breaks the loop)."""
        row.setdefault("timestamp", time.time())
        try:
            await self.store.append_decision(self.mid, row)
        except Exception as exc:
            logger.warning("live_escort.audit_write_failed", movement_id=self.mid,
                           error=str(exc)[:200])

    async def _publish(self) -> None:
        self.run.state.updated_at = utc_iso()
        if self._publish_cb is None:
            return
        try:
            await self._publish_cb(self.run)
        except Exception as exc:
            logger.warning("live_escort.publish_failed", movement_id=self.mid,
                           error=str(exc)[:200])

    async def _tool(self, name: str, args: dict) -> dict | None:
        """One oracle call, isolated: a failure reads as no data and is
        audited once per failure streak (not per tick)."""
        try:
            async with track_mcp_tool(name):
                result = await self.executor.execute(name, args)
        except Exception as exc:
            streak = self.tool_failures.get(name, 0) + 1
            self.tool_failures[name] = streak
            if streak == 1:
                await self._append({
                    "phase": "live_escort",
                    "action": "tool_degraded",
                    "tool": name,
                    "error": str(exc)[:200],
                })
            logger.warning("live_escort.tool_failed", movement_id=self.mid, tool=name,
                           streak=streak, error=str(exc)[:200])
            return None
        if self.tool_failures.pop(name, 0):
            await self._append({"phase": "live_escort", "action": "tool_recovered", "tool": name})
        return result if isinstance(result, dict) else None

    # -- lifecycle -----------------------------------------------------------------

    async def execute(self, max_iterations: int | None) -> dict:
        st = self.run.state
        st.started_at = st.started_at or utc_iso()
        st.status = "waiting_for_gps"
        st.detail = "escort starting"

        ctx = await self.store.get(self.mid)
        if ctx is None:
            return await self._finish("failed", "movement context not found")
        self.vvip_class = ctx.vvip_class or "Z"
        self.event_kind = ctx.event_kind or "protected_movement"
        st.event_id = getattr(ctx, "event_id", None) or current_event_id(ctx)
        row = plan_row(ctx)
        st.plan_status = row.get("recommendation") if row else None

        # Adaptive parameters for this movement (fail-closed). The spike
        # threshold and GPS poll interval are applied HERE, at their real use.
        if self.adaptive_client is not None:
            self.adaptive = await self.adaptive_client.fetch(
                movement_id=self.mid, vvip_class=self.vvip_class
            )
        if self.adaptive is not None:
            self.spike_threshold = self.adaptive.congestion_spike_threshold
            self.poll = self.adaptive.gps_poll_interval_sec
        if self.poll_override is not None:
            self.poll = self.poll_override
        st.spike_threshold = self.spike_threshold
        self.advance_closure_sec = (self.adaptive or baseline_params()).floored_advance_closure_sec(
            self.vvip_class
        )
        self.coordinator = DiversionCoordinatorAgent(
            self.bridge, self.executor, self.store, adaptive=self.adaptive
        )

        await self._load_route(ctx)
        refused = await self._activate_event()
        if refused is not None:
            return await self._finish("failed", refused)
        self.next_event_check = self._clock() + self.event_check_sec

        def activate(c: ConvoyContext) -> bool:
            if c.status in ("completed", "cancelled", "active"):
                return False
            c.status = "active"
            return True

        try:
            await mutate_context(self.store, self.mid, activate)
        except Exception as exc:
            logger.warning("live_escort.status_write_failed", error=str(exc)[:200])
        await self._append({
            "phase": "live_escort",
            "action": "escort_started",
            "officer": self.run.officer or SYSTEM_ACTOR,
            **({"claimed_officer": self.run.claimed_officer} if self.run.claimed_officer else {}),
            "task_id": self.run.task_id,
            "route_segment_ids": list(self.run.route_segment_ids),
            "route_source": self.run.route_source,
            "route_length_m": (
                round(self.run.route.routing_length_m, 1) if self.run.route else None
            ),
            "gps_mode": "emulated" if self.run.sim_gps else "live",
            "plan_status": st.plan_status,
            "spike_threshold": self.spike_threshold,
            "advance_closure_sec": self.advance_closure_sec,
        })
        logger.info("live_escort_workflow.start", movement_id=self.mid,
                    route=self.run.route_segment_ids, sim_gps=self.run.sim_gps)
        await self._publish()

        cap = max_iterations if max_iterations is not None else MAX_MONITOR_ITERATIONS
        while True:
            if self.run.stop_requested:
                return await self._finish("stopped", self.run.stop_reason or "stopped")
            self.iterations += 1
            st.iteration = self.iterations
            outcome = await self._tick()
            if outcome is not None:
                return await self._finish(*outcome)
            await self._publish()
            if self.iterations >= cap:
                return await self._finish(
                    "stopped", f"safety valve: {cap} monitoring iterations (last state {st.status})"
                )
            await self.run.wait_stop(self.poll)

    async def _load_route(self, ctx: ConvoyContext) -> None:
        st = self.run.state
        if not self.run.route_segment_ids:
            self.run.route_segment_ids = plan_route_segments(ctx)
        if self.run.route is None and self.run.route_segment_ids:
            self.run.route = await load_route_geometry(
                self.executor, self.run.route_segment_ids, source=self.run.route_source,
                cache=self.run.details_cache,
            )
            self.route_retry_at = self._clock() + ROUTE_RETRY_SEC
        if self.run.route is not None:
            st.route = self.run.route.summary()
        elif self.run.route_segment_ids:
            st.route = {"segment_ids": list(self.run.route_segment_ids), "length_m": None,
                        "source": self.run.route_source,
                        "missing_geometry": list(self.run.route_segment_ids)}

    async def _activate_event(self) -> str | None:
        """C-F: the escort start moves the movement's corridor event to
        ``active`` (officer from the request, else the system actor), and
        reads the event's alternate spines for Plan B.

        Returns the failure detail when the event is already cancelled or
        completed (409 from that status): no escort runs on a closed event.
        """
        if self.lifecycle is None:
            return None
        event_id = self.run.state.event_id
        officer = self.run.officer or SYSTEM_ACTOR
        claimed = (
            f" [claimed officer: {self.run.claimed_officer}]" if self.run.claimed_officer else ""
        )
        transition = await self.lifecycle.transition(
            event_id, "active", officer,
            note=f"live escort started (movement {self.mid}, task {self.run.task_id}){claimed}",
        )
        if event_id:
            await self._append({
                "phase": "city_event",
                "action": f"event_{transition['outcome']}",
                "event_id": event_id,
                "to_status": "active",
                "officer": officer,
                "http_status": transition.get("http_status"),
                "from_status": transition.get("from_status"),
            })
            if (transition["outcome"] == "conflict"
                    and transition.get("from_status") in CLOSED_EVENT_STATUSES):
                return (
                    f"corridor event {event_id} is already {transition['from_status']} — "
                    "the escort did not start; plan a new movement"
                )
            if not self.run.alternates:
                spines = await self.lifecycle.spines(event_id)
                self.run.alternates = [
                    ids for label, ids in sorted(spines.items())
                    if label.startswith("alt") and ids
                ]
        else:
            await self._append({"phase": "live_escort", "action": "no_corridor_event",
                                "detail": transition.get("reason")})
        return None

    async def _check_event(self) -> tuple[str, str] | None:
        """Every event_check_sec: the corridor event closed elsewhere stops the escort.

        An officer who cancels or completes the event on /ops has decided:
        the escort stops, its closures are lifted, the movement takes the
        event's status (via mutate_context) and the audit row
        'event_closed_externally' names who closed it. The published C-J
        document agrees with the lift — no closure schedule and no hold
        outlive the close (the ops HUD reads its active_diversions as
        'N diversions active'). A failed context write lifts nothing, and
        the detail says so instead of claiming the lift.
        """
        event_id = self.run.state.event_id
        status_of = getattr(self.lifecycle, "status", None)
        if status_of is None or not event_id or self._clock() < self.next_event_check:
            return None
        self.next_event_check = self._clock() + self.event_check_sec
        info = await status_of(event_id, since=self.run.state.started_at)
        to_status = info.get("status") if isinstance(info, dict) else None
        if to_status not in CLOSED_EVENT_STATUSES:
            return None
        closed_by = getattr(self.lifecycle, "closed_by", None)
        actor = await closed_by(event_id, to_status) if closed_by is not None else None
        lifted: list[str] = []

        def close(c: ConvoyContext) -> None:
            lifted[:] = list(c.active_diversions)
            c.active_diversions = []
            if c.status not in CLOSED_EVENT_STATUSES:
                c.status = to_status

        write_error: str | None = None
        try:
            await mutate_context(self.store, self.mid, close)
        except Exception as exc:
            write_error = str(exc)[:200]
            lifted.clear()  # nothing was persisted: no closure was lifted
            logger.warning("live_escort.close_write_failed", error=write_error)
        st = self.run.state
        if write_error is None:
            st.active_diversions = []
        self.run.hold = None
        st.hold = None
        row = {
            "phase": "live_escort",
            "action": "event_closed_externally",
            "event_id": event_id,
            "to_status": to_status,
            "officer": actor,
            "closures_lifted": [int(s) for s in lifted if str(s).isdigit()],
        }
        if write_error is not None:
            row["closure_lift_failed"] = write_error
        await self._append(row)
        who = actor or "an officer on the ops wall"
        lift_text = (
            f"{len(lifted)} closure(s) lifted" if write_error is None
            else "closures NOT lifted (context store write failed) — clear them on /ops"
        )
        return "stopped", f"corridor event {to_status} by {who} — escort stopped, {lift_text}"

    async def _finish(self, status: str, detail: str) -> dict:
        st = self.run.state
        st.status = status
        st.detail = detail
        st.ended_at = utc_iso()
        if status == "completed":
            st.progress = 1.0 if self.run.route is not None else st.progress
            st.eta_sec = 0.0
        self.run.hold = None
        st.hold = None
        for task in list(self.side_tasks):
            task.cancel()
        if self.side_tasks:
            await asyncio.gather(*self.side_tasks, return_exceptions=True)
        # An approval that landed after the last tick never ran: said so.
        while self.run.operator_actions:
            await self._not_applied(self.run.operator_actions.pop(0),
                                    f"the escort {status} before the approved action ran")
        await self._expire_approvals(f"the escort {status} before an officer decided")
        action = {"completed": "escort_complete", "stopped": "escort_stopped",
                  "failed": "escort_failed"}[status]
        row = {
            "phase": "live_escort",
            "action": action,
            "arrived": status == "completed",
            "iterations": self.iterations,
            "detail": detail,
            "incidents": st.incidents,
            "reroutes": self.reroutes,
            "distance_m": (
                round(self.run.last_location.distance_m, 1) if self.run.last_location else None
            ),
            "duration_sec": round(self._clock() - self.started_mono, 1),
        }
        if status == "stopped" and self.run.stop_officer:
            row["officer"] = self.run.stop_officer
        await self._append(row)
        await self._publish()
        final_status = None
        with contextlib.suppress(Exception):
            ctx = await self.store.get(self.mid)
            final_status = ctx.status if ctx else None
        logger.info("live_escort_workflow.complete", movement_id=self.mid, status=status,
                    iterations=self.iterations, detail=detail)
        return {
            "movement_id": self.mid,
            "escort_complete": status in ("completed", "stopped"),
            "arrived": status == "completed",
            "status": status,
            "detail": detail,
            "total_iterations": self.iterations,
            "final_status": final_status,
            "route": st.route,
        }

    # -- one tick ------------------------------------------------------------------

    async def _tick(self) -> tuple[str, str] | None:
        st = self.run.state
        try:
            ctx = await self.store.get(self.mid)
        except Exception as exc:
            self.store_failures += 1
            st.detail = "context store unreachable — retrying"
            logger.warning("live_escort.store_failed", movement_id=self.mid,
                           streak=self.store_failures, error=str(exc)[:200])
            if self.store_failures >= 10:
                return "failed", f"context store unreachable ({str(exc)[:120]})"
            return None
        self.store_failures = 0
        if ctx is None:
            return "failed", "movement context disappeared"
        if ctx.status in ("completed", "cancelled"):
            return "stopped", f"movement {ctx.status}"
        closed = await self._check_event()
        if closed is not None:
            return closed

        if self.run.route is None and self.run.route_segment_ids \
                and self._clock() >= self.route_retry_at:
            await self._load_route(ctx)

        await self._run_operator_actions()
        await self._expire_overdue()

        fix = fix_of(ctx)
        fix_epoch = _epoch(fix.t) if fix else None
        age = (time.time() - fix_epoch) if fix_epoch is not None else None
        if fix is None or (age is not None and age > ESCORT_GPS_STALE_SEC):
            return await self._wait_for_gps(fix, age)
        if self.waiting_since is not None:
            await self._append({
                "phase": "live_escort",
                "action": "gps_acquired",
                "source": fix.source,
                "waited_sec": round(self._clock() - self.waiting_since, 1),
            })
            self.waiting_since = None

        st.status = "running"
        ESCORT_TICKS.labels(status="running").inc()
        st.position = {
            "lon": fix.lon, "lat": fix.lat, "speed_kmh": fix.speed_kmh,
            "heading": fix.heading, "t": fix.t or None, "source": fix.source,
        }
        st.gps = {
            "source": fix.source,
            "age_sec": round(age, 1) if age is not None else None,
            "mode": "emulated" if fix.source == "emulated_gps" else "live",
        }
        speed = fix.speed_kmh if fix.speed_kmh and fix.speed_kmh >= 5.0 else self.cruise_kmh

        loc = self.run.locate(fix.lon, fix.lat)
        if loc is not None:
            self.run.last_location = loc
            st.progress = loc.progress
            st.remaining_m = loc.remaining_m
            st.current_segment_id = loc.segment_id
            st.offset_m = loc.offset_m
            st.eta_sec = self._eta(loc, fix)
            await self._check_off_route(loc)
            if loc.remaining_m <= ESCORT_ARRIVAL_RADIUS_M:
                st.remaining_m = 0.0
                return "completed", "arrived at the end of the route"
            ahead = self.run.route.segments_ahead(loc.index, loc.distance_m, ESCORT_LOOKAHEAD_M)
            road = self.run.route.road_name(loc.segment_id)
            st.detail = (
                f"on segment {loc.segment_id}" + (f" ({road})" if road else "")
                + f", {loc.remaining_m / 1000:.1f} km to go"
                + (f" — HOLDING ({self.run.hold['ordered_by']})" if self.run.holding else "")
            )
        else:
            ahead = await self._segments_near(fix)
            st.detail = "tracking without route geometry (segments around the convoy)"

        if not self.departed and self.run.route_segment_ids:
            await self._departure(ctx, loc, fix, speed, ahead)

        # ONE live-traffic call per tick: the lookahead plus, while a hold is
        # in force, the segments the hold waits on (a departure hold's
        # congestion may lie beyond the lookahead).
        watched = list(ahead)
        if self.run.hold is not None:
            watched += [s for s in self.run.hold["segment_ids"] if s not in watched]
        observed: dict[int, dict] = {}
        qualities: dict[int, str] = {}
        if watched:
            live = await self._tool("get_live_traffic", {"segment_ids": watched})
            all_rows = live_observation_rows(live) if live is not None else []
            st.traffic_feed = feed_label(live, all_rows, watched)
            qualities = segment_qualities(live)
            for r in all_rows:
                seen = observed.get(r["segment_id"])
                if seen is None or r["congestion_idx"] > seen["congestion_idx"]:
                    observed[r["segment_id"]] = r
        in_lookahead = set(ahead)
        rows = [r for r in observed.values() if r["segment_id"] in in_lookahead]
        peak = max((r["congestion_idx"] for r in rows), default=None)
        st.peak_congestion_ahead = round(peak, 3) if peak is not None else None
        # The hold first: one that expires on still-congested segments
        # raises the incident this tick's checkpoint then decides.
        await self._maybe_release_hold(observed, rows, peak, loc, qualities)
        await self._congestion(peak, rows, ctx, loc, fix, ahead, qualities)

        # Closure bookkeeping: lift what the convoy has passed (route order).
        remaining = list(ctx.active_diversions)
        try:
            if self.run.route is not None and loc is not None:
                upcoming = self.run.route.segments_ahead(loc.index, loc.distance_m, float("inf"))
                result = await self.coordinator.tick_update(
                    self.mid, (fix.lon, fix.lat), upcoming,
                    route_segment_ids=self.run.route.segment_ids,
                )
            else:
                result = await self.coordinator.tick_update(self.mid, (fix.lon, fix.lat), ahead)
            remaining = list(result.get("remaining_active", remaining))
            if result.get("deactivated"):
                await self._append({
                    "phase": "live_escort",
                    "action": "closures_lifted",
                    "segment_ids": [int(s) for s in result["deactivated"]],
                    "reason": "convoy passed",
                })
        except Exception as exc:
            logger.warning("live_escort.tick_update_failed", error=str(exc)[:200])
        st.active_diversions = self._closure_schedule(remaining, loc, speed)
        return None

    def _eta(self, loc: RouteLocation, fix: PositionFix) -> float:
        """Remaining route time, segment by segment at its expected speed.

        Expected speed is the segment's limit (ESCORT_ETA_FALLBACK_KMH when
        unknown), capped at the emulated cruise for the walker's own fixes —
        the speed the emulated convoy will actually drive there.
        """
        emulated = fix.source == "emulated_gps"

        def expected(limit: float | None) -> float:
            speed = limit or ESCORT_ETA_FALLBACK_KMH
            return min(speed, self.cruise_kmh) if emulated else speed

        if self.run.route is None:
            return loc.remaining_m / (ESCORT_ETA_FALLBACK_KMH / 3.6)
        return self.run.route.eta_sec(loc.distance_m, expected)

    async def _wait_for_gps(
        self, fix: PositionFix | None, age: float | None,
    ) -> tuple[str, str] | None:
        st = self.run.state
        st.status = "waiting_for_gps"
        ESCORT_TICKS.labels(status="waiting_for_gps").inc()
        # Refreshed every waiting tick, so the HUD's fix age matches the detail.
        emulated = self.run.sim_gps and not self.run.real_gps_seen
        st.gps = {
            "source": fix.source if fix is not None else None,
            "age_sec": round(age, 1) if age is not None else None,
            "mode": (
                ("emulated" if fix.source == "emulated_gps" else "live") if fix is not None
                else ("emulated" if emulated else "live")
            ),
        }
        now = self._clock()
        reason = "no GPS fix yet" if fix is None else f"last GPS fix is {age:.0f} s old"
        if self.waiting_since is None:
            self.waiting_since = now
            await self._append({"phase": "live_escort", "action": "waiting_for_gps",
                                "detail": reason})
        waited = now - self.waiting_since
        st.detail = f"waiting for GPS ({reason}; {waited:.0f}/{self.gps_timeout:.0f} s)"
        if waited >= self.gps_timeout:
            await self._append({
                "phase": "live_escort_alert",
                "type": "gps_lost",
                "action": "escort_failed",
                "reason": f"no usable GPS fix for {waited:.0f} s",
                "movement_id": self.mid,
            })
            return "failed", (
                f"no usable GPS fix for {waited:.0f} s — the escort did NOT complete; "
                "restart it with POST /movements/{id}/escort once a fix arrives"
            ).replace("{id}", self.mid)
        return None

    async def _segments_near(self, fix: PositionFix) -> list[int]:
        """Legacy monitor (no route geometry): segments in a box around the convoy."""
        result = await self._tool("query_segments_in_bbox", {
            "min_lon": fix.lon - 0.005,
            "min_lat": fix.lat - 0.005,
            "max_lon": fix.lon + 0.015,
            "max_lat": fix.lat + 0.015,
        })
        seg_list = (result or {}).get("segments", [])
        out: list[int] = []
        for s in (seg_list if isinstance(seg_list, list) else [])[:50]:
            sid = s.get("segment_id") if isinstance(s, dict) else s
            if isinstance(sid, int):
                out.append(sid)
        return out

    async def _check_off_route(self, loc: RouteLocation) -> None:
        if loc.offset_m > ESCORT_OFF_ROUTE_M and not self.off_route:
            self.off_route = True
            await self._append({
                "phase": "live_escort_alert",
                "type": "convoy_off_route",
                "action": "verify_route",
                "reason": f"convoy {loc.offset_m:.0f} m from the planned route",
                "movement_id": self.mid,
            })
        elif loc.offset_m < ESCORT_OFF_ROUTE_M / 2 and self.off_route:
            self.off_route = False
            await self._append({"phase": "live_escort", "action": "back_on_route",
                                "offset_m": round(loc.offset_m, 1)})

    def _closure_schedule(
        self, remaining: list[str], loc: RouteLocation | None, speed_kmh: float,
    ) -> list[dict]:
        """C-J active_diversions: each closure with its just-in-time window,
        in route order (closures off the route last)."""
        closure_type = CLOSURE_TYPE.get(self.vvip_class, "managed")
        now = time.time()
        v = max(speed_kmh, 5.0) / 3.6
        route = self.run.route
        out: list[tuple[float, dict]] = []
        for raw in remaining:
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                continue
            activate_at = deactivate_at = None
            state = "active"
            order = float("inf")
            span = (
                route.segment_range(sid, after_index=loc.index)
                if route is not None and loc is not None else None
            )
            if span is not None:
                start, end = span
                order = start
                # time to the segment's ends on the ROUTING lengths the plan
                # priced (the walker keeps that timetable on traced segments)
                arrive = now + route.routing_m(loc.distance_m, start) / v
                activate = arrive - self.advance_closure_sec
                activate_at = utc_iso(activate)
                deactivate_at = utc_iso(now + route.routing_m(loc.distance_m, end) / v)
                state = "active" if activate <= now else "scheduled"
            out.append((order, {
                "segment_id": sid,
                "type": closure_type,
                "activate_at": activate_at,
                "deactivate_at": deactivate_at,
                "state": state,
            }))
        return [row for _order, row in sorted(out, key=lambda pair: pair[0])]

    # -- departure -------------------------------------------------------------------

    async def _departure(
        self, ctx: ConvoyContext, loc: RouteLocation | None, fix: PositionFix, speed_kmh: float,
        ahead: list[int],
    ) -> None:
        """The planned -> live phase transition: ONE decider checkpoint on the
        first live fix, over the live congestion of the whole route.

        The officer already launched the escort: the convoy proceeds on that
        launch (the current approved plan). A held departure or Plan B — a
        confident answer or not — is a PROPOSAL a named officer approves; a
        trusted "proceed" changes nothing and is simply recorded.

        Congestion already above the threshold inside the lookahead is the
        crossing this checkpoint decided: the incident path treats it as
        decided (rising edge consumed, cooldown started) instead of asking
        the decider about the same congestion again seconds later.
        """
        self.departed = True
        st = self.run.state
        thr = self.spike_threshold
        # The route still ahead: all of it at a fresh departure, the rest of
        # it when a resumed run picks the convoy up mid-route.
        if self.run.route is not None and loc is not None:
            route_ids = self.run.route.segments_ahead(loc.index, loc.distance_m, float("inf"))
        else:
            route_ids = list(self.run.route_segment_ids)
        live = await self._tool("get_live_traffic", {"segment_ids": route_ids})
        rows = live_observation_rows(live) if live is not None else []
        peak = max((r["congestion_idx"] for r in rows), default=None)
        hot = [r["segment_id"] for r in sorted(rows, key=lambda r: -r["congestion_idx"])
               if r["congestion_idx"] > thr]
        snapshot = _Incident(
            peak=peak if peak is not None else 0.0, hot=hot, rows=rows,
            at=self._clock(), t=utc_iso(), trigger="phase_transition", origin="departure",
            # The posture is decided on the whole route's numbers.
            quality=feed_fields(segment_qualities(live), [r["segment_id"] for r in rows]),
        )
        feed = feed_label(live, rows, route_ids)
        if set(hot) & set(ahead):
            self.was_high = True
            self.clear_since = None
            self.last_incident_at = self._clock()
        heuristic = "hold_departure" if hot else "proceed"
        if self.decider is None:
            st.last_decision = {"phase": "departure", "tier": "none", "action": None,
                                "confidence": None, "t": utc_iso()}
            await self._depart(heuristic, snapshot, trusted=False, tier="none", confidence=None,
                               reason="no strategic tier wired")
            return

        if loc is not None and loc.progress > 0.02:
            where = (
                f"resuming at {loc.progress:.0%} of the route, "
                f"{loc.remaining_m / 1000:.1f} km to go"
            )
        elif loc is not None:
            where = f"{loc.remaining_m / 1000:.1f} km to go"
        else:
            where = "route geometry unavailable"
        eta_min = (loc.remaining_m / (speed_kmh / 3.6) / 60) if loc is not None else None
        questions = {
            "departure": {
                "type": "choice",
                "instructions": (
                    "The convoy is rolling onto its planned primary route. Choose the "
                    "departure posture: proceed now, hold the departure until the "
                    "corridor ahead clears, or switch to the pre-sanitised Plan B route."
                ),
                "options": list(DEPARTURE_ACTIONS),
            },
        }
        state_doc = DeciderBrain.build_state_doc(
            trigger="phase_transition",
            summary=(
                f"Live escort start for the {self.vvip_class} convoy ({self.event_kind}) on "
                f"its {len(route_ids)}-segment primary route, {where}"
                + (f", about {eta_min:.0f} min at {speed_kmh:.0f} km/h" if eta_min else "")
                + ". Live congestion on the route "
                + (f"peaks at {peak:.2f}" if peak is not None else "is not reporting")
                + f" (spike threshold {thr:.2f})"
                + (f"; above it: {hot[:6]}" if hot else "") + "."
            ),
            context={
                "movement_id": self.mid,
                "vvip_class": self.vvip_class,
                "event_kind": self.event_kind,
                "plan_status": st.plan_status,
                "route_segments": route_ids[:12],
                "segments_reporting": f"{feed['segments_reporting']}/{len(route_ids)}",
                "closures_active": len(ctx.active_diversions),
                "plan_b_spine": "available" if self.run.alternates else "none",
                "gps": fix.source,
            },
        )
        async with track_decider_checkpoint([q["type"] for q in questions.values()]) as tracker:
            checkpoint = await self.decider.checkpoint(
                movement_id=self.mid,
                trigger="phase_transition",
                state_doc=state_doc,
                questions=questions,
                force=True,  # single-shot phase transition, never per tick
            )
            tracker["confidences"] = [a.confidence for a in checkpoint.answers.values()]
        await self._append(dict(checkpoint.audit, escort_phase="departure", **snapshot.quality))
        for alert in checkpoint.operator_alerts:
            await self._append(dict(alert, phase="live_escort_alert"))

        answer = checkpoint.answers.get("departure")
        choice = answer.value if answer is not None and answer.value in DEPARTURE_ACTIONS else None
        confidence = answer.confidence if answer is not None else None
        st.last_decision = {
            "phase": "departure",
            "tier": checkpoint.tier,
            "action": choice,
            "confidence": round(confidence, 4) if isinstance(confidence, float) else confidence,
            "t": utc_iso(),
        }
        trusted = (
            checkpoint.tier in TRUSTED_TIERS
            and choice is not None
            and not checkpoint.audit.get("needs_gemma_draft")
        )
        if trusted:
            reason = None
        elif checkpoint.tier == "novelty_latched":
            reason = "novel situation: the decider's confident answer was withheld"
        elif checkpoint.tier in TRUSTED_TIERS and choice is None:
            reason = (
                f"the decider's answer {getattr(answer, 'value', None)!r} "
                "is not a departure posture"
            )
        elif checkpoint.tier in TRUSTED_TIERS:
            reason = (
                f"low confidence ({confidence:.2f}) — below the floor"
                if isinstance(confidence, float) else "low confidence"
            )
        else:
            reason = f"strategic tier unavailable ({checkpoint.degraded_reason or checkpoint.tier})"
        # no usable answer: the deterministic rule stands in (labelled so)
        await self._depart(choice or heuristic, snapshot, trusted=trusted,
                           tier=checkpoint.tier if choice else "deterministic",
                           confidence=confidence if choice else None, reason=reason,
                           checkpoint_tier=checkpoint.tier)

    async def _depart(
        self, posture: str, snapshot: _Incident, *, trusted: bool, tier: str,
        confidence: float | None, reason: str | None, checkpoint_tier: str | None = None,
    ) -> None:
        """Record one departure posture: "proceed" is the plan the officer
        launched; a held departure or Plan B is proposed to a named officer
        (the convoy proceeds on the launch meanwhile)."""
        if trusted and posture == "proceed":
            ESCORT_DEPARTURES.labels(posture=posture).inc()
            await self._append({"phase": "live_escort", "action": "departure_proceed",
                                "ordered_by": f"decider ({tier})",
                                "segment_ids": list(snapshot.hot),
                                "peak_congestion": round(snapshot.peak, 3),
                                **snapshot.quality})
            return
        launcher = f"officer launch ({self.run.officer or SYSTEM_ACTOR})"
        if trusted:
            reason = (
                f"decider ({tier}) at {_confidence_text(confidence)} — "
                "an officer approves every switch"
            )
        await self._append({"phase": "live_escort", "action": "departure_proceed",
                            "ordered_by": launcher, "decider_tier": checkpoint_tier or tier,
                            "note": reason,
                            **({"decider_proposes": posture} if posture != "proceed" else {}),
                            "segment_ids": list(snapshot.hot),
                            "peak_congestion": round(snapshot.peak, 3),
                            **snapshot.quality})
        if posture == "proceed":
            ESCORT_DEPARTURES.labels(posture="proceed_on_launch").inc()
            return
        ESCORT_DEPARTURES.labels(posture="operator_approval").inc()
        await self._propose(
            DEPARTURE_EXECUTES[posture], snapshot, why=f"departure: {reason}", tier=tier,
            confidence=confidence, proposer=f"decider ({tier})" if trusted else None,
            checkpoint_tier=checkpoint_tier,
        )

    # -- incidents -------------------------------------------------------------------

    async def _congestion(
        self,
        peak: float | None,
        rows: list[dict],
        ctx: ConvoyContext,
        loc: RouteLocation | None,
        fix: PositionFix,
        ahead: list[int],
        qualities: dict[int, str],
    ) -> None:
        """Rising-edge incident detection over the lookahead.

        A rising edge raises ONE incident. One inside the cooldown is
        deferred: the first tick after the cooldown ends that still reads
        above the threshold raises it (a jam that outlasts the cooldown is
        always decided). The falling edge needs clear readings for
        ``clear_confirm`` seconds; any congested reading restarts that
        window, and silence neither raises nor clears.
        """
        thr = self.spike_threshold
        now = self._clock()
        if peak is not None:
            if peak < thr - ESCORT_CONGESTION_HYSTERESIS:
                if self.was_high:
                    if self.clear_since is None:
                        self.clear_since = now
                    if now - self.clear_since >= self.clear_confirm:
                        # The crossing has cleared; a deferred one cleared
                        # before its turn and needs no decision.
                        self.was_high = False
                        self.clear_since = None
                        self.deferred = None
                        await self._expire_approvals(
                            "the congestion ahead cleared before an officer decided"
                        )
            else:
                self.clear_since = None
                if peak > thr:
                    hot = [r["segment_id"] for r in sorted(rows, key=lambda r: -r["congestion_idx"])
                           if r["congestion_idx"] > thr]
                    quality = feed_fields(qualities, hot)
                    if not self.was_high:
                        self.was_high = True
                        await self._crossing(peak, hot, rows, loc, fix, now, quality)
                    elif (self.deferred is not None and self.pending is None
                          and not self._in_cooldown(now)):
                        await self._raise_deferred(peak, hot, rows, now, quality)
        if self.pending is not None:
            await self._incident_checkpoint(ctx, loc, fix, ahead)

    def _in_cooldown(self, now: float) -> bool:
        return self.last_incident_at is not None and now - self.last_incident_at < self.cooldown

    async def _crossing(
        self, peak: float, hot: list[int], rows: list[dict], loc: RouteLocation | None,
        fix: PositionFix, now: float, quality: dict,
    ) -> None:
        """One rising edge, logged as an observation row: raised as an
        incident, folded into the one still awaiting its checkpoint, or
        deferred while the cooldown runs."""
        row = {
            "phase": "live_escort",
            "action": "congestion_crossing",
            "trigger": "congestion_threshold_crossed",
            "peak_congestion": round(peak, 3),
            "threshold": round(self.spike_threshold, 3),
            "segment_ids": hot,
            "segment_id": loc.segment_id if loc else None,
            "progress": round(loc.progress, 4) if loc else None,
            "position": [fix.lon, fix.lat],
            **quality,
        }
        if self.pending is not None:
            self.pending.folded += 1
            row["folded_into_pending"] = True
        elif self._in_cooldown(now):
            self.folded_crossings += 1
            if self.deferred is None:
                self.deferred = {"at": now, "t": utc_iso()}
            row["deferred"] = True
            row["decision_due_in_sec"] = round(self.cooldown - (now - self.last_incident_at), 1)
            logger.info("live_escort.crossing_deferred", movement_id=self.mid,
                        peak=round(peak, 3))
        else:
            self._open_incident(peak, hot, rows, now, quality)
        await self._append(row)

    async def _raise_deferred(
        self, peak: float, hot: list[int], rows: list[dict], now: float, quality: dict,
    ) -> None:
        """The cooldown ended with its deferred crossing still above the
        threshold: that crossing is the next incident (one checkpoint)."""
        crossed_at = self.deferred["t"] if self.deferred else None
        self._open_incident(peak, hot, rows, now, quality, origin="deferred_crossing",
                            detail=f"crossed at {crossed_at}, inside the incident cooldown")
        await self._append({
            "phase": "live_escort",
            "action": "deferred_incident_raised",
            "trigger": "congestion_threshold_crossed",
            "reason": "the corridor was still above the threshold when the incident cooldown ended",
            "crossed_at": crossed_at,
            "peak_congestion": round(peak, 3),
            "threshold": round(self.spike_threshold, 3),
            "segment_ids": hot,
            **quality,
        })

    def _open_incident(
        self, peak: float, hot: list[int], rows: list[dict], now: float, quality: dict,
        *, origin: str = "crossing", detail: str | None = None,
    ) -> None:
        """Raise the next incident (its checkpoint runs at the decider's
        cadence); the crossings deferred since the last one fold into it."""
        if self.pending is not None:
            self.pending.folded += 1
            return
        self.last_incident_at = now
        self.pending = _Incident(
            peak=peak, hot=hot, rows=rows, at=now, t=utc_iso(),
            folded=self.folded_crossings, origin=origin, detail=detail,
            deferred_from=self.deferred["t"] if self.deferred else None, quality=quality,
        )
        self.folded_crossings = 0
        self.deferred = None

    async def _incident_checkpoint(
        self, ctx: ConvoyContext, loc: RouteLocation | None, fix: PositionFix, ahead: list[int],
    ) -> None:
        inc = self.pending
        st = self.run.state
        if self.decider is None:
            self.pending = None
            st.incidents += 1
            await self._propose(
                self._heuristic_action(inc), inc,
                why="no strategic tier wired — operator decision required",
                tier="none", confidence=None,
            )
            return
        if not self.decider.degraded and not self.decider.checkpoint_due(self.mid):
            return  # wait for the cadence slot instead of being rate-limited

        self.pending = None
        inc.attempts += 1
        # ONE question set for every incident origin (it keys the tier-0
        # cache); the summary says why this incident was raised.
        questions = {
            "action": {
                "type": "choice",
                "instructions": (
                    "Congestion on the convoy's route is above the spike threshold "
                    "(see the summary). Choose the immediate action: keep monitoring, "
                    "hold the convoy at its position, activate the pre-sanitised Plan B "
                    "route, re-route around the congested segments, or escalate to the "
                    "operator."
                ),
                "options": list(INCIDENT_ACTIONS),
            },
            "urgency": {
                "type": "noul",
                "instructions": (
                    "Probability that none of the listed actions fit "
                    "this incident (escalate to operator)."
                ),
            },
        }
        hot_text = ", ".join(
            f"{r['segment_id']}={r['congestion_idx']:.2f}" for r in inc.rows
            if r["segment_id"] in inc.hot
        )[:200]
        where = (
            f"at {loc.progress:.0%} of the route, {loc.remaining_m / 1000:.1f} km to go"
            if loc else f"near ({fix.lon:.4f}, {fix.lat:.4f})"
        )
        thr = self.spike_threshold
        if inc.origin == "hold_expired":
            lead = (
                f"A hold ran out ({inc.detail}) with congestion {inc.peak:.2f} still above "
                f"the spike threshold {thr:.2f} on the held segment(s) of the "
                f"{self.vvip_class} convoy"
            )
        elif inc.origin == "deferred_crossing":
            lead = (
                f"Congestion {inc.peak:.2f} crossed the spike threshold {thr:.2f} during the "
                f"incident cooldown and is still above it on segment(s) ahead of the "
                f"{self.vvip_class} convoy"
            )
        else:
            lead = (
                f"Congestion {inc.peak:.2f} crossed the spike threshold {thr:.2f} on "
                f"segment(s) ahead of the {self.vvip_class} convoy"
            )
        state_doc = DeciderBrain.build_state_doc(
            trigger="incident_detected",
            summary=f"{lead}, {where}.",
            context={
                "movement_id": self.mid,
                "vvip_class": self.vvip_class,
                "event_kind": self.event_kind,
                "incident_origin": inc.origin,
                "current_segment": loc.segment_id if loc else None,
                "segments_ahead": ahead[:8],
                "hot_segments": hot_text,
                "convoy_speed_kmh": fix.speed_kmh,
                "convoy_holding": "yes" if self.run.holding else "no",
                "plan_b_spine": "available" if self.run.alternates else "none",
                "closures_active": len(ctx.active_diversions),
                "folded_crossings": inc.folded,
                "gps": fix.source,
            },
        )
        async with track_decider_checkpoint([q["type"] for q in questions.values()]) as tracker:
            checkpoint = await self.decider.checkpoint(
                movement_id=self.mid,
                trigger="incident_detected",
                state_doc=state_doc,
                questions=questions,
            )
            tracker["confidences"] = [a.confidence for a in checkpoint.answers.values()]
        await self._append(dict(
            checkpoint.audit, escort_phase="incident", incident_origin=inc.origin,
            **({"deferred_from": inc.deferred_from} if inc.deferred_from else {}),
            **inc.quality,
        ))
        for alert in checkpoint.operator_alerts:
            if checkpoint.tier == "rate_limited" and alert.get("type") == "decider_unavailable":
                continue  # cadence, not an outage — never a false outage alert
            await self._append(dict(alert, phase="live_escort_alert"))

        if checkpoint.tier == "rate_limited" and inc.attempts < 3:
            self.pending = inc  # lost a cadence race (e.g. an operator chat turn)
            return
        st.incidents += 1
        await self._act(inc, checkpoint)

    async def _act(self, inc: _Incident, checkpoint: CheckpointResult) -> None:
        st = self.run.state
        answer = checkpoint.answers.get("action")
        choice = answer.value if answer is not None and answer.value in INCIDENT_ACTIONS else None
        confidence = answer.confidence if answer is not None else None
        st.last_decision = {
            "phase": "incident_detected",
            "tier": checkpoint.tier,
            "action": choice,
            "confidence": round(confidence, 4) if isinstance(confidence, float) else confidence,
            "t": utc_iso(),
        }
        trusted = (
            checkpoint.tier in TRUSTED_TIERS
            and choice is not None
            and not checkpoint.audit.get("needs_gemma_draft")
        )
        if trusted and choice == "continue_monitoring":
            # No change to the approved plan: recorded, nothing to approve.
            ESCORT_INCIDENTS.labels(outcome=choice).inc()
            await self._execute(choice, inc, actor=f"decider ({checkpoint.tier})")
            return
        if trusted and choice in PLAN_CHANGING_ACTIONS:
            # Officer approves every switch: a confident answer is a proposal.
            st.last_decision["proposed"] = True
            await self._propose(
                choice, inc, tier=checkpoint.tier, confidence=confidence,
                proposer=f"decider ({checkpoint.tier})",
                why=(f"decider ({checkpoint.tier}) at {_confidence_text(confidence)} — "
                     "an officer approves every switch"),
            )
            return

        if checkpoint.tier == "novelty_latched":
            reason = "novel situation: the decider's confident answer was withheld"
        elif checkpoint.tier in TRUSTED_TIERS and choice == "escalate_operator":
            reason = "the decider escalated this incident to the operator"
        elif checkpoint.tier in TRUSTED_TIERS and choice is None:
            reason = (
                f"the decider's answer {getattr(answer, 'value', None)!r} "
                "is not an escort action"
            )
        elif checkpoint.tier in TRUSTED_TIERS:
            reason = (
                f"low confidence ({confidence:.2f}) — below the floor"
                if isinstance(confidence, float) else "low confidence"
            )
        else:
            reason = f"strategic tier unavailable ({checkpoint.degraded_reason or checkpoint.tier})"
        model_answer = choice not in (None, "escalate_operator")
        recommended = choice if model_answer else self._heuristic_action(inc)
        st.last_decision["proposed"] = True
        # Who proposed it: the model's own (low-confidence / withheld)
        # answer, or — when it gave none — the deterministic rule that stood
        # in, never labelled as a model's (the checkpoint's tier rides along)
        await self._propose(recommended, inc, why=reason,
                            tier=checkpoint.tier if model_answer else "deterministic",
                            confidence=confidence if model_answer else None,
                            checkpoint_tier=checkpoint.tier)

    def _heuristic_action(self, inc: _Incident) -> str:
        """Deterministic recommendation from the observed rows (no model)."""
        status = heuristic_traffic_assessment({"observations": inc.rows})["overall_status"]
        return {"red": "emergency_reroute", "amber": "hold_position"}.get(
            status, "continue_monitoring"
        )

    # -- proposals (officer approves every switch) -------------------------------------

    async def _propose(
        self, action: str, inc: _Incident, *, why: str, tier: str, confidence: float | None,
        proposer: str | None = None, checkpoint_tier: str | None = None,
    ) -> None:
        """Turn a recommended action into ONE concrete proposal for a named
        officer — never executed here, whatever the confidence.

        A route change is computed in full now (Plan B's splice, else the
        solver's protocol-compliant detour from the convoy's segment), so
        the officer approves the exact route, the closures it adds and
        lifts and the km it costs. A route change that cannot be carried
        out becomes a proposal to hold short of the jam — or nothing, when
        no hold would avoid it (the convoy is already in it). ``proposer``
        names who proposed it in the referral texts; ``checkpoint_tier`` is
        the decider checkpoint's own tier when a deterministic rule stood in
        for an answer it did not give.
        """
        who = proposer or (
            "the deterministic heuristic" if tier in ("none", "deterministic")
            else f"the {tier} tier"
        )
        # a rule that replaces what was proposed keeps where it came from
        source_tier = checkpoint_tier or tier
        if action in ("hold_position", "continue_monitoring"):
            await self._raise_approval(inc, recommended=action, reason=why, tier=tier,
                                       confidence=confidence, checkpoint_tier=checkpoint_tier)
            return
        note: str | None = None
        candidate: _RouteCandidate | None = None
        if action == "activate_plan_b":
            candidate, why_not = await self._plan_b_candidate(inc)
            if candidate is None:
                note = f"{why_not} — re-routing instead"
        if candidate is None:
            if self.run.route is None or self.run.last_location is None:
                await self._propose_replan(inc, why=why, tier=tier, confidence=confidence,
                                           checkpoint_tier=checkpoint_tier)
                return
            candidate, outcome, detail = await self._reroute_candidate(inc, who)
            if candidate is None:
                if outcome == "skipped":
                    await self._propose_unavoidable(inc, action, why, checkpoint_tier=source_tier)
                    return
                await self._raise_approval(
                    inc, recommended="hold_position",
                    reason=(f"{why}; the {action.replace('_', ' ')} it recommends cannot be "
                            "carried out — " + (f"{note}; " if note else "") + detail
                            + " — approve to hold the convoy until it clears, reject to continue"),
                    tier="deterministic", confidence=None, checkpoint_tier=source_tier,
                )
                return
            candidate.note = note
        await self._raise_approval(
            inc, recommended=action, reason=why, tier=tier, confidence=confidence,
            candidate=candidate, checkpoint_tier=checkpoint_tier,
        )

    async def _propose_replan(
        self, inc: _Incident, *, why: str, tier: str, confidence: float | None,
        checkpoint_tier: str | None = None,
    ) -> None:
        """No route geometry: the re-route is a re-plan from the convoy's fix
        to the destination, computed when an officer approves it."""
        if not self.run.destination or not self.run.state.position:
            await self._raise_approval(
                inc, recommended="hold_position",
                reason="no planned route geometry to re-route from — operator decision required",
                tier="deterministic", confidence=None, checkpoint_tier=checkpoint_tier or tier,
            )
            return
        await self._raise_approval(
            inc, recommended="emergency_reroute", reason=why, tier=tier, confidence=confidence,
            summary=(f"re-plan from the convoy's position around {self._roads(inc.hot)} "
                     "(no route geometry — the route is computed on approval)"),
            proposed={"replan": True, "avoids": sorted(set(inc.hot))},
            checkpoint_tier=checkpoint_tier,
        )

    async def _propose_unavoidable(
        self, inc: _Incident, action: str, why: str, *, checkpoint_tier: str | None = None,
    ) -> None:
        """A proposed route change that cannot be carried out leaves the
        convoy heading into the congestion. When hot segments still lie
        ahead of it (beyond its own segment), an officer decides whether it
        holds short of them — the proposal is never dropped silently. A
        convoy already in the congestion, or on its final segment, has
        nothing a hold would avoid."""
        route, loc = self.run.route, self.run.last_location
        if route is None or loc is None:
            return
        beyond = set(route.segments_ahead(loc.index, loc.distance_m, float("inf"))[1:])
        unavoidable = [s for s in inc.hot if s in beyond and s != loc.segment_id]
        if not unavoidable:
            return
        await self._raise_approval(
            _Incident(peak=inc.peak, hot=unavoidable, rows=inc.rows, at=inc.at, t=inc.t,
                      trigger=inc.trigger, origin=inc.origin, quality=inc.quality),
            recommended="hold_position",
            reason=(
                f"{why}; the {action.replace('_', ' ')} it recommends cannot be carried out: no "
                f"route avoids {unavoidable} ahead of the convoy — approve to hold the convoy "
                "until it clears, reject to continue"
            ),
            tier="deterministic", confidence=None, checkpoint_tier=checkpoint_tier,
        )

    async def _raise_approval(
        self, inc: _Incident, *, recommended: str, reason: str, tier: str,
        confidence: float | None, extra: dict | None = None,
        candidate: _RouteCandidate | None = None, summary: str | None = None,
        proposed: dict | None = None, checkpoint_tier: str | None = None,
    ) -> str:
        """Record ONE proposal for a named officer: the decisions_log row
        ``operator_approval_required`` and the C-J ``pending_approval``.

        It supersedes any older open proposal (audited) and expires after
        the approval timeout; nothing is executed here. ``reason`` says why
        an officer is asked; the row's own ``reason`` reads "AI proposes
        <summary> — <why>" (the city feed narrates it) — "Rule proposes"
        when no model answered and the deterministic rule stood in (``tier``
        deterministic / none), never an AI label on a rule. Returns its id.
        """
        approval_id = uuid.uuid4().hex[:12]
        kind = PROPOSAL_KINDS.get(recommended, "action")
        if candidate is not None:
            kind = candidate.source
            proposed = await self._route_proposal(candidate)
            summary = summary or self._route_summary(candidate, inc)
        elif recommended == "hold_position":
            proposed = proposed or {"hold_segment_ids": list(dict.fromkeys(inc.hot)),
                                    "max_hold_sec": self.hold_max,
                                    "hold_reason": self._hold_reason(inc)}
        summary = summary or self._summary_for(recommended, inc)
        proposer = "Rule proposes" if tier in ("deterministic", "none") else "AI proposes"
        now = time.time()
        row = {
            **(extra or {}),
            "phase": "live_escort_alert",
            "type": "operator_approval_required",
            "action": recommended,
            "kind": kind,
            "summary": summary,
            "reason": f"{proposer} {summary} — {reason}",
            "why": reason,
            "approval_id": approval_id,
            "status": "pending",
            "trigger": inc.trigger,
            "tier": tier,
            "confidence": round(confidence, 4) if isinstance(confidence, float) else None,
            "segment_ids": list(inc.hot),
            "peak_congestion": round(inc.peak, 3),
            "movement_id": self.mid,
            "created_at": utc_iso(now),
            "expires_at": utc_iso(now + self.approval_timeout),
            "timeout_sec": self.approval_timeout,
            "authority": ESCORT_AUTHORITY,
            **inc.quality,
        }
        if checkpoint_tier and checkpoint_tier != tier:
            row["checkpoint_tier"] = checkpoint_tier  # the checkpoint gave no usable answer
        if proposed:
            row["proposed"] = proposed
            if proposed.get("segment_ids"):
                # the route the officer would approve, drawn dashed as a preview
                row["detour_segment_ids"] = list(proposed["segment_ids"])
                row["detour_m"] = proposed.get("new_remaining_m")
                row["remaining_m"] = proposed.get("remaining_m")
                row["route_id"] = proposed.get("route_id")
        await self._append(dict(row))
        self.run.approvals[approval_id] = {
            **row,
            "raised_at": row["created_at"],
            "expires_mono": self._clock() + self.approval_timeout,
            "incident": {"peak": inc.peak, "hot": list(inc.hot), "rows": list(inc.rows),
                         "quality": dict(inc.quality)},
        }
        for older in self.run.supersede_pending(
            approval_id, "a newer proposal replaces it — one open proposal per escort",
        ):
            await self._audit_closed(older, "approval_superseded")
        self.run._refresh_pending()
        ESCORT_INCIDENTS.labels(outcome="operator_approval").inc()
        logger.warning("live_escort.operator_approval_required", movement_id=self.mid,
                       action=recommended, kind=kind, summary=summary, reason=reason)
        return approval_id

    async def _audit_closed(self, row: dict, action: str) -> None:
        """Append-only trail of a proposal that closed without being decided."""
        await self._append(closed_approval_row(row, action))

    async def _expire_approvals(self, reason: str) -> None:
        """Audit the approvals whose situation is gone (append-only trail)."""
        for row in self.run.expire_pending(reason):
            await self._audit_closed(row, "approval_expired")

    async def _expire_overdue(self) -> None:
        """Proposals no officer answered within the timeout expire: the
        convoy keeps executing its current approved plan."""
        for row in self.run.expire_overdue(self._clock()):
            logger.info("live_escort.approval_expired", movement_id=self.mid,
                        approval_id=row["approval_id"], action=row["action"])
            await self._audit_closed(row, "approval_expired")

    def _incident_of(self, approved: dict) -> _Incident:
        snap = approved.get("incident") or {}
        return _Incident(
            peak=float(snap.get("peak") or 0.0),
            hot=list(snap.get("hot") or []),
            rows=list(snap.get("rows") or []),
            at=self._clock(),
            t=utc_iso(),
            trigger=str(approved.get("trigger") or "incident_detected"),
            quality=dict(snap.get("quality") or {}),
        )

    async def _run_operator_actions(self) -> None:
        """Carry out what a named officer approved since the last tick —
        exactly the proposal the officer saw."""
        while self.run.operator_actions:
            approved = self.run.operator_actions.pop(0)
            inc = self._incident_of(approved)
            officer = approved.get("decided_by")
            claimed = approved.get("claimed_officer")
            await self._append({
                "phase": "live_escort",
                "action": "operator_approved",
                "approved_action": approved["action"],
                "kind": approved.get("kind"),
                "approval_id": approved["approval_id"],
                "officer": officer,
                **({"claimed_officer": claimed} if claimed else {}),
                "summary": approved.get("summary"),
                "proposed_by_tier": approved.get("tier"),
                "proposed_confidence": approved.get("confidence"),
            })
            self.run.state.last_decision = {
                "phase": "operator_approval",
                "tier": "operator",
                "action": approved["action"],
                "confidence": None,
                "t": utc_iso(),
            }
            ESCORT_INCIDENTS.labels(outcome=f"approved_{approved['action']}").inc()
            applied, detail = await self._apply_approved(approved, inc, f"officer {officer}")
            self._mark_applied(approved["approval_id"], applied, detail)

    async def _apply_approved(
        self, approved: dict, inc: _Incident, actor: str,
    ) -> tuple[bool, str | None]:
        """One approved proposal: the hold or the exact route the officer saw.

        A route the convoy can no longer reach (it rolled past the branch
        point) is NOT replaced by a fresher one on the officer's name: the
        current plan holds and a new proposal is raised for a new click.
        """
        action = approved["action"]
        proposed = approved.get("proposed") or {}
        claimed = approved.get("claimed_officer")
        if action == "hold_position":
            await self._hold(inc, actor, reason=proposed.get("hold_reason"))
            return True, None
        if action in ("activate_plan_b", "emergency_reroute") and proposed.get("segment_ids"):
            source = proposed.get("route_source") or (
                "plan_b" if action == "activate_plan_b" else "reroute"
            )
            switched = await self._take_approved_route(
                {"segment_ids": proposed["segment_ids"], "route_id": proposed.get("route_id")},
                inc, actor, source=source, claimed_officer=claimed, note=proposed.get("note"),
            )
            if switched:
                for stale in self.run.supersede_pending(
                    approved["approval_id"],
                    "the approved route switch replaced the plan it was computed on",
                ):
                    await self._audit_closed(stale, "approval_superseded")
                return True, None
            loc = self.run.last_location
            detail = (
                "the convoy is no longer on the approved route"
                + (f" (segment {loc.segment_id} — it rolled past the branch point)" if loc else "")
                + " — current plan held; a fresh proposal follows"
            )
            await self._not_applied(approved, detail)
            await self._propose(
                action, inc, tier="deterministic", confidence=None, proposer=actor,
                why=f"re-proposed: {actor} approved a route the convoy can no longer take",
            )
            return False, detail
        # continue monitoring, or an approved re-plan without route geometry
        # (computed now — the officer approved re-planning around the jam)
        await self._execute(action, inc, actor=actor)
        return True, None

    async def _not_applied(self, approved: dict, detail: str) -> None:
        await self._append({
            "phase": "live_escort",
            "action": "approval_not_applied",
            "approval_id": approved["approval_id"],
            "approved_action": approved["action"],
            "officer": approved.get("decided_by"),
            "reason": detail,
        })
        self._mark_applied(approved["approval_id"], False, detail)

    def _mark_applied(self, approval_id: str, applied: bool, detail: str | None) -> None:
        row = self.run.approvals.get(approval_id)
        if row is None:
            return
        row["applied"] = applied
        if detail:
            row["applied_detail"] = detail
        self.run._refresh_pending()

    # -- proposal texts ------------------------------------------------------------------

    def _road_of(self, sid: int, *routes: RouteGeometry | None) -> str:
        for route in (*routes, self.run.route):
            name = route.road_name(sid) if route is not None else None
            if name:
                return name
        cached = self.run.details_cache.get(int(sid)) or {}
        return str(cached.get("road_name") or f"segment {sid}")

    def _roads(self, ids: Iterable[int], *routes: RouteGeometry | None) -> str:
        names: list[str] = []
        for sid in ids:
            name = self._road_of(sid, *routes)
            if name not in names:
                names.append(name)
        if not names:
            return "the corridor ahead"
        return ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")

    def _hold_reason(self, inc: _Incident) -> str:
        what = f"congestion {inc.peak:.2f} on {self._roads(inc.hot)}"
        if inc.trigger == "phase_transition":
            return f"departure held until {what} clears"
        return f"held short of {what}"

    def _summary_for(self, action: str, inc: _Incident) -> str:
        roads, peak = self._roads(inc.hot), f"{inc.peak:.2f}"
        if action == "hold_position":
            if inc.trigger == "phase_transition":
                return (f"hold the departure until {roads} clears (congestion {peak}), "
                        f"up to {self.hold_max:.0f} s")
            return (f"hold the convoy short of {roads} (congestion {peak}) until it clears, "
                    f"up to {self.hold_max:.0f} s")
        if action == "continue_monitoring":
            return (f"keep monitoring — no change to the approved plan "
                    f"(congestion {peak} on {roads})")
        return f"{action.replace('_', ' ')} (congestion {peak} on {roads})"

    def _route_summary(self, cand: _RouteCandidate, inc: _Incident) -> str:
        old_ids = set(self.run.route.segment_ids) if self.run.route else set()
        new_ids = [s for s in cand.tail if s not in old_ids] or cand.tail
        via = self._roads(new_ids, cand.route)
        left = f"{cand.new_left_m / 1000:.1f} km left (now {cand.old_left_m / 1000:.1f} km"
        if cand.old_left_m > 0 and cand.new_left_m > cand.old_left_m * 1.05:
            left += f", {cand.new_left_m / cand.old_left_m:.1f}x"
            if cand.long_detour:
                left += " — a long detour"
        left += ")"
        what = "Plan B" if cand.source == "plan_b" else "re-route"
        avoid = (f", avoiding {self._roads(cand.avoided)} (congestion {inc.peak:.2f})"
                 if cand.avoided else "")
        return f"{what} via {via} — {left}{avoid}"

    async def _route_proposal(self, cand: _RouteCandidate) -> dict:
        """What approving a route change would do: the exact route, the
        closures the new route adds and the ones it lifts, the km."""
        loc = self.run.last_location
        start = cand.route.index_at(loc.distance_m) if loc is not None else 0
        ahead = list(dict.fromkeys(cand.route.segment_ids[start:]))
        new_ids = set(cand.route.segment_ids)
        old_ids = list(self.run.route.segment_ids) if self.run.route else []
        active: list[str] = []
        with contextlib.suppress(Exception):
            ctx = await self.store.get(self.mid)
            active = list(ctx.active_diversions) if ctx else []
        closure_type = CLOSURE_TYPE.get(self.vvip_class, "managed")
        # what is NEW on it (the roads the convoy would take instead), in order
        via = [s for s in ahead if s not in old_ids] or ahead
        return {
            "route_source": cand.source,
            "segment_ids": list(cand.route.segment_ids),
            "tail_segment_ids": list(cand.tail),
            "route_id": cand.route_id,
            "road_names": [self._road_of(s, cand.route) for s in ahead],
            "via_segment_ids": via,
            "via_road_names": list(dict.fromkeys(self._road_of(s, cand.route) for s in via)),
            "remaining_m": round(cand.old_left_m, 1),
            "new_remaining_m": round(cand.new_left_m, 1),
            "avoids": list(cand.avoided),
            "long_detour": cand.long_detour,
            "closure_type": closure_type,
            # the tactical tier drafts these on approval (X class: signal priority only)
            "closures_new": [s for s in ahead if str(s) not in active],
            "closures_lifted": sorted(
                int(s) for s in active
                if str(s).isdigit() and int(s) in old_ids and int(s) not in new_ids
            ),
            **({"note": cand.note} if cand.note else {}),
        }

    # -- route candidates (computed for a proposal; nothing switches) ------------------

    def _candidate(self, new_route: RouteGeometry, source: str, route_id: str | None,
                   inc: _Incident, tail: list[int]) -> _RouteCandidate:
        route, loc = self.run.route, self.run.last_location
        # routing metres (the plan's lengths, C-K), never the drawn line's
        old_left = route.routing_m(loc.distance_m, route.length_m) if route and loc else 0.0
        new_left = new_route.routing_m(loc.distance_m, new_route.length_m) if loc else 0.0
        return _RouteCandidate(
            route=new_route, source=source, route_id=route_id, tail=list(tail),
            avoided=sorted({s for s in inc.hot if s not in new_route.segment_ids}),
            old_left_m=old_left, new_left_m=new_left,
        )

    async def _plan_b_candidate(self, inc: _Incident) -> tuple[_RouteCandidate | None, str]:
        """The first pre-sanitised alternate spine the convoy is on whose
        remaining path avoids the congested segments: ``(candidate, why_not)``."""
        route, loc = self.run.route, self.run.last_location
        if route is None or loc is None:
            return None, "no route geometry to splice Plan B onto"
        if not self.run.alternates:
            return None, "the plan has no alternate (Plan B) spine"
        hot = set(inc.hot) - {loc.segment_id}
        reachable = False
        for alternate in self.run.alternates:
            if loc.segment_id not in alternate:
                continue
            reachable = True
            tail = alternate[alternate.index(loc.segment_id):]
            if hot & set(tail):
                continue  # this spine runs into the same congestion
            details = await fetch_segment_details(self.executor, tail, self.run.details_cache)
            lines, limits, names = geometry_parts(details)
            new_route = route.splice(loc.index, tail, source="plan_b", tail_lines=lines,
                                     tail_limits=limits, tail_names=names,
                                     tail_paces=geometry_paces(details))
            if not new_route.usable or new_route.segment_ids == route.segment_ids:
                continue  # no geometry, or Plan B is already the route
            return self._candidate(new_route, "plan_b", None, inc, tail), ""
        if reachable:
            return None, f"every Plan B spine runs through the congested segment(s) {sorted(hot)}"
        return None, f"no Plan B spine passes the convoy's segment {loc.segment_id}"

    async def _reroute_candidate(
        self, inc: _Incident, who: str,
    ) -> tuple[_RouteCandidate | None, str, str]:
        """Deterministic emergency re-route around the hot segments:
        ``(candidate, outcome, detail)`` with outcome ``found``, ``skipped``
        (nothing a re-route could change — audited) or ``no_path``.

        The solver's candidates are ranked with the adaptive weights and the
        first one passing the class lane floor is spliced at the convoy's
        segment. A detour longer than ESCORT_REROUTE_MAX_DETOUR times the
        distance left is still only a proposal — its figures say so.
        """
        route, loc = self.run.route, self.run.last_location
        assert route is not None and loc is not None
        current, target = loc.segment_id, route.segment_ids[-1]

        async def skipped(why: str) -> tuple[None, str, str]:
            await self._append({"phase": "live_escort", "action": "reroute_skipped",
                                "proposed_by": who, "reason": why})
            return None, "skipped", why

        if current == target:
            return await skipped("convoy already on the final segment")
        avoid = sorted({s for s in inc.hot if s not in (current, target)})
        if not avoid:
            # The convoy's own segment and the destination cannot be routed
            # around; a solver call without anything to avoid would only
            # re-draw the current route.
            return await skipped("no congested segment ahead that a re-route could avoid")
        result = await self._tool("find_convoy_routes", {
            "origin_segment": current,
            "destination_segment": target,
            "max_candidates": 5,
            "avoid_segments": avoid,
        })
        rows = [r for r in (result or {}).get("routes", [])
                if isinstance(r, dict) and r.get("segment_ids")]
        weights = (self.adaptive.scoring_weights if self.adaptive else None) or {}
        ranked = heuristic_route_ranking(dedupe_route_candidates(rows), weights) if rows else []
        pick = None
        if ranked:
            ids = [s for r in ranked for s in r.get("segment_ids", [])]
            facts = await fetch_segment_facts(self.executor, ids, {})
            for candidate in ranked:
                if lane_audit(facts, candidate["segment_ids"], self.vvip_class)["compliant"]:
                    pick = candidate
                    break
        if pick is None:
            return None, "no_path", (
                "emergency re-route found no "
                + ("protocol-compliant " if ranked else "")
                + f"path from segment {current} to {target} avoiding {avoid}"
            )
        tail = [int(s) for s in pick["segment_ids"]]
        details = await fetch_segment_details(self.executor, tail, self.run.details_cache)
        lines, limits, names = geometry_parts(details)
        new_route = route.splice(loc.index, tail, source="reroute", tail_lines=lines,
                                 tail_limits=limits, tail_names=names,
                                 tail_paces=geometry_paces(details))
        if not new_route.usable:
            return await skipped("no geometry for the re-route's segments")
        if new_route.segment_ids == route.segment_ids:
            return await skipped("the best protocol-compliant path is the current route")
        return self._candidate(new_route, "reroute", pick.get("route_id"), inc, tail), "found", ""

    # -- actions -----------------------------------------------------------------------

    async def _execute(
        self, action: str, inc: _Incident, *, actor: str, reason: str | None = None,
    ) -> None:
        """Carry out one escort action — only ever "continue monitoring" on
        the decider's word; a hold or a route change only on an officer's
        approval (``actor`` "officer <name>")."""
        if action == "continue_monitoring":
            await self._append({"phase": "live_escort", "action": "continue_monitoring",
                                "ordered_by": actor, "segment_ids": inc.hot})
        elif action == "hold_position":
            await self._hold(inc, actor, reason=reason)
        elif action in ("activate_plan_b", "emergency_reroute"):
            if action == "activate_plan_b":
                candidate, why = await self._plan_b_candidate(inc)
                if candidate is not None:
                    await self._switch_to(candidate, actor,
                                          reason="Plan B (pre-sanitised alternate spine)")
                    return
                outcome = await self._reroute(inc, actor, note=f"{why} — re-routing instead")
            else:
                outcome = await self._reroute(inc, actor)
            if outcome == "skipped":
                await self._propose_unavoidable(inc, action, f"approved by {actor}")
        else:
            await self._append({"phase": "live_escort", "action": "no_action",
                                "requested": action, "ordered_by": actor})

    async def _hold(self, inc: _Incident, actor: str, *, reason: str | None = None) -> None:
        self.run.hold = {
            "since": utc_iso(),
            "since_mono": self._clock(),
            "reason": reason or f"congestion {inc.peak:.2f} ahead on {inc.hot}",
            "ordered_by": actor,
            # The segments this hold waits on (watched every tick, even
            # beyond the lookahead, until they clear).
            "segment_ids": list(dict.fromkeys(inc.hot)),
            "max_sec": self.hold_max,
            "peak_congestion": round(inc.peak, 3),
            "clear_since": None,
        }
        self._show_hold()
        await self._append({
            "phase": "live_escort",
            "action": "hold_position",
            "ordered_by": actor,
            "reason": self.run.hold["reason"],
            "segment_ids": list(inc.hot),
            "max_hold_sec": self.hold_max,
            # A real convoy holds on the officers' radio order; only the
            # emulated walker is halted by the brain itself.
            "advisory": not self.run.sim_gps or self.run.real_gps_seen,
            **inc.quality,
        })

    def _show_hold(self) -> None:
        """Mirror the hold into the status document (without loop internals)."""
        hold = self.run.hold
        self.run.state.hold = None if hold is None else {
            k: v for k, v in hold.items() if k not in ("since_mono", "clear_since")
        }

    async def _release_hold(self, reason: str, held: float, **extra: Any) -> None:
        self.run.hold = None
        self.run.state.hold = None
        await self._append({
            "phase": "live_escort",
            "action": "hold_released",
            "reason": reason,
            "held_sec": round(held, 1),
            **extra,
        })

    async def _maybe_release_hold(
        self,
        observed: dict[int, dict],
        rows_ahead: list[dict],
        peak_ahead: float | None,
        loc: RouteLocation | None,
        qualities: dict[int, str],
    ) -> None:
        """Lift a hold once ITS segments clear, the convoy has passed them,
        or it expires.

        A hold is judged on the segments it waits on, never on the
        lookahead alone (a departure hold's congestion may lie beyond it).
        Every held segment must report below threshold - hysteresis for
        ``clear_confirm`` seconds; a congested reading restarts that window
        and silence keeps the hold. A hold without named segments waits on
        the corridor ahead. ``hold_max`` bounds every hold: one that expires
        on segments still above the threshold is released AND re-decided as
        a new incident, so the convoy never rolls into the jam undecided.
        """
        hold = self.run.hold
        if hold is None:
            return
        now = self._clock()
        held = now - hold["since_mono"]
        thr = self.spike_threshold
        ids = list(hold["segment_ids"])
        if ids and self.run.route is not None and loc is not None:
            route_left = self.run.route.segments_ahead(loc.index, loc.distance_m, float("inf"))
            if not set(ids) & set(route_left):
                await self._release_hold("the convoy is past the held segment(s)", held,
                                         segment_ids=ids)
                return
        if ids:
            readings = [observed[s] for s in ids if s in observed]
            everyone_reports = len(readings) == len(ids)
            what = f"held segment(s) {ids}"
        else:
            readings = list(rows_ahead)
            everyone_reports = peak_ahead is not None
            what = "corridor ahead"
        hold_peak = max((r["congestion_idx"] for r in readings), default=None)
        if hold_peak is not None:
            hold["peak_congestion"] = round(hold_peak, 3)
        if hold_peak is not None and hold_peak >= thr - ESCORT_CONGESTION_HYSTERESIS:
            hold["clear_since"] = None  # a congested reading restarts the clear window
        elif everyone_reports and readings:
            if hold["clear_since"] is None:
                hold["clear_since"] = now
            if now - hold["clear_since"] >= self.clear_confirm:
                await self._release_hold(f"{what} cleared", held, segment_ids=ids,
                                         peak_congestion=round(hold_peak, 3))
                return
        if held >= self.hold_max:
            hot_rows = sorted((r for r in readings if r["congestion_idx"] > thr),
                              key=lambda r: -r["congestion_idx"])
            hot = [r["segment_id"] for r in hot_rows]
            await self._release_hold(
                f"hold expired after {held:.0f} s"
                + (f"; {hot} still above the threshold — re-deciding" if hot else ""),
                held, segment_ids=ids, still_congested=hot,
            )
            if hot:
                self._open_incident(
                    hot_rows[0]["congestion_idx"], hot, hot_rows, now,
                    feed_fields(qualities, hot), origin="hold_expired",
                    detail=f"held {held:.0f} s of a {self.hold_max:.0f} s maximum",
                )
            return
        self._show_hold()

    async def _switch_to(self, cand: _RouteCandidate, actor: str, *, reason: str,
                         claimed_officer: str | None = None) -> None:
        """Switch to a computed candidate (only ever on an officer's approval)."""
        await self._switch_route(cand.route, actor, reason=reason, route_id=cand.route_id,
                                 claimed_officer=claimed_officer)
        if cand.source == "plan_b":
            await self._mark_plan_b(actor, f"congestion on {cand.avoided}")

    async def _mark_plan_b(self, actor: str, why: str) -> None:
        with contextlib.suppress(Exception):
            await self.store.update_protocol_state(self.mid, {"plan_b": {
                "active": True,
                "activated_at": time.time(),
                "reason": f"{actor}: {why}",
            }})

    async def _reroute(self, inc: _Incident, actor: str, note: str | None = None) -> str:
        """Re-route on an officer's approval when no route was computed with
        the proposal. Returns ``switched``, ``referred`` (a hold proposal was
        raised instead) or ``skipped`` (nothing a re-route could change)."""
        route, loc = self.run.route, self.run.last_location
        if route is None or loc is None:
            return await self._reroute_without_route(inc, actor)
        candidate, outcome, detail = await self._reroute_candidate(inc, actor)
        if candidate is None:
            if outcome == "skipped":
                return "skipped"
            await self._raise_approval(
                inc, recommended="hold_position",
                reason=(f"{note} (approved by {actor}); " if note
                        else f"re-route approved by {actor}; ") + detail,
                tier="deterministic", confidence=None,
            )
            return "referred"
        await self._switch_to(candidate, actor,
                              reason=note or f"emergency re-route avoiding {candidate.avoided}")
        return "switched"

    async def _take_approved_route(
        self, approved: dict, inc: _Incident, actor: str, *, source: str = "reroute",
        claimed_officer: str | None = None, note: str | None = None,
    ) -> bool:
        """Splice the exact route an officer approved, from the convoy's
        current segment on. False when the convoy is no longer on it."""
        route, loc = self.run.route, self.run.last_location
        try:
            wanted = [int(s) for s in approved.get("segment_ids") or []]
        except (TypeError, ValueError):
            return False
        if route is None or loc is None or loc.segment_id not in wanted:
            return False
        tail = wanted[wanted.index(loc.segment_id):]
        details = await fetch_segment_details(self.executor, tail, self.run.details_cache)
        lines, limits, names = geometry_parts(details)
        new_route = route.splice(loc.index, tail, source=source, tail_lines=lines,
                                 tail_limits=limits, tail_names=names,
                                 tail_paces=geometry_paces(details))
        if not new_route.usable or new_route.segment_ids == route.segment_ids:
            return False
        avoided = sorted({s for s in inc.hot if s not in new_route.segment_ids})
        reason = (
            "Plan B (pre-sanitised alternate spine), officer-approved" if source == "plan_b"
            else f"officer-approved detour avoiding {avoided}"
        )
        if note:
            reason = f"{note}; {reason}"
        await self._switch_route(new_route, actor, reason=reason,
                                 route_id=approved.get("route_id"),
                                 claimed_officer=claimed_officer)
        if source == "plan_b":
            await self._mark_plan_b(actor, f"congestion {inc.peak:.2f} on {inc.hot}")
        return True

    async def _reroute_without_route(self, inc: _Incident, actor: str) -> str:
        """No route geometry: re-plan from the fix to the destination (if any)."""
        fix = self.run.state.position
        if not self.run.destination or not fix:
            await self._raise_approval(
                inc, recommended="hold_position",
                reason="no planned route geometry to re-route from — operator decision required",
                tier="deterministic", confidence=None,
            )
            return "referred"
        planner = RoutePlannerAgent(self.bridge, self.executor, adaptive=self.adaptive)
        result = await planner.analyze_routes(
            movement_id=self.mid,
            origin=(fix["lon"], fix["lat"]),
            destination=tuple(self.run.destination),
            vvip_class=self.vvip_class,
            avoid_segments=list(inc.hot),
        )
        primary = result.get("primary_route") or {}
        ids = [int(s) for s in primary.get("segment_ids") or []]
        new_route = (
            await load_route_geometry(self.executor, ids, source="reroute",
                                      cache=self.run.details_cache) if ids else None
        )
        if new_route is None:
            await self._raise_approval(
                inc, recommended="hold_position",
                reason=(f"re-route approved by {actor}: re-planning from the convoy's fix found "
                        f"no usable route avoiding {sorted(set(inc.hot))} — operator decision "
                        "required"),
                tier="deterministic", confidence=None,
            )
            return "referred"
        await self._switch_route(new_route, actor, reason="emergency re-route (re-planned)",
                                 route_id=primary.get("route_id"))
        return "switched"

    async def _switch_route(
        self, new_route: RouteGeometry, actor: str, *, reason: str, route_id: str | None = None,
        claimed_officer: str | None = None,
    ) -> None:
        """Switch the escort onto ``new_route`` — reached only through an
        officer's approval (``actor`` "officer <name>"): closures on the
        abandoned segments lift, the new spine is published (C-O) and the
        tactical tier drafts the new route's closures."""
        old_ids = (
            list(self.run.route.segment_ids) if self.run.route
            else list(self.run.route_segment_ids)
        )
        self.run.route = new_route
        self.run.route_segment_ids = list(new_route.segment_ids)
        self.run.state.route = new_route.summary()
        self.reroutes += 1
        new_ids = set(new_route.segment_ids)
        abandoned = {str(s) for s in old_ids if s not in new_ids}
        selected = route_id or f"{new_route.source}-{self.reroutes}"
        lifted: list[str] = []

        def apply(c: ConvoyContext) -> None:
            lifted.clear()
            lifted.extend(s for s in c.active_diversions if s in abandoned)
            c.active_diversions = [s for s in c.active_diversions if s not in abandoned]
            c.selected_route_id = selected
            c.alerts.append({
                "type": ("emergency_reroute" if new_route.source == "reroute"
                         else "plan_b_activated"),
                "timestamp": time.time(),
                "reason": reason,
                "new_route": selected,
            })

        try:
            await mutate_context(self.store, self.mid, apply)
        except Exception as exc:
            logger.warning("live_escort.route_switch_write_failed", error=str(exc)[:200])
        await self._append({
            "phase": "live_escort",
            "action": "route_switched",
            "source": new_route.source,
            "ordered_by": actor,
            "reason": reason,
            "route_id": selected,
            "from_segment_ids": old_ids,
            "segment_ids": list(new_route.segment_ids),
            "length_m": round(new_route.length_m, 1),
            "closures_lifted": [int(s) for s in lifted],
            **({"claimed_officer": claimed_officer} if claimed_officer else {}),
        })
        logger.warning("live_escort.route_switched", movement_id=self.mid, source=new_route.source,
                       segments=new_route.segment_ids, ordered_by=actor)
        # The city picture follows the convoy's real route (C-O): the new
        # spine becomes the event's primary — best effort, never blocking.
        publish = asyncio.create_task(
            self._publish_route(new_route, actor, reason, claimed_officer=claimed_officer)
        )
        self.side_tasks.add(publish)
        publish.add_done_callback(self.side_tasks.discard)
        hold = self.run.hold
        if hold is not None and hold["segment_ids"] and not set(hold["segment_ids"]) & new_ids:
            await self._release_hold("the new route avoids the held segment(s)",
                                     self._clock() - hold["since_mono"],
                                     segment_ids=list(hold["segment_ids"]))
        # The tactical tier drafts the new closures in the background; the
        # tick loop keeps running meanwhile.
        loc = self.run.last_location
        start_index = new_route.index_at(loc.distance_m) if loc else 0
        ahead = [s for s in new_route.segment_ids[start_index:]]
        task = asyncio.create_task(self._activate_diversions(ahead))
        self.side_tasks.add(task)
        task.add_done_callback(self.side_tasks.discard)

    async def _publish_route(
        self, new_route: RouteGeometry, actor: str, reason: str,
        claimed_officer: str | None = None,
    ) -> None:
        """POST the switched route as the corridor event's primary spine (C-O),
        under the approving officer's name.

        Audited either way: 'route_update_updated', 'route_update_not_deployed'
        (404 — the endpoint or the event is absent), 'route_update_conflict'
        (409) or 'route_update_rejected' (422).
        """
        event_id = self.run.state.event_id
        if self.city_link is None or not event_id:
            return
        variant = "plan_b" if new_route.source == "plan_b" else f"reroute-{self.reroutes}"
        officer = actor[len("officer "):] if actor.startswith("officer ") else SYSTEM_ACTOR
        claim = f" [claimed officer: {claimed_officer}]" if claimed_officer else ""
        try:
            outcome = await self.city_link.update_route(
                event_id, variant, list(new_route.segment_ids), make_primary=True,
                officer=officer, note=f"{reason} — ordered by {actor}{claim}",
            )
        except Exception as exc:  # update_route never raises; belt and braces
            outcome = {"status": "failed", "detail": str(exc)[:200]}
        row = {
            "phase": "city_event",
            "action": f"route_update_{outcome.get('status')}",
            "event_id": event_id,
            "variant": variant,
            "segment_ids": list(new_route.segment_ids),
            "officer": officer,
            "http_status": outcome.get("http_status"),
        }
        if outcome.get("detail") is not None:
            row["detail"] = str(outcome["detail"])[:300]
        await self._append(row)

    async def _activate_diversions(self, segment_ids: list[int]) -> None:
        try:
            result = await self.coordinator.activate_diversions(
                self.mid, segment_ids, vvip_class=self.vvip_class
            )
        except Exception as exc:
            logger.warning("live_escort.diversions_failed", error=str(exc)[:200])
            await self._append({"phase": "live_escort", "action": "diversions_failed",
                                "error": str(exc)[:200]})
            return
        directives = result.get("directives") or []
        await self._append({
            "phase": "live_escort",
            "action": "diversions_activated",
            "tier": result.get("_tier"),
            "directives": len(directives),
            "closures": sorted({int(d["segment_id"]) for d in directives
                                if isinstance(d, dict) and d.get("action") == "activate"}),
            "signal_priority": sorted({
                int(d["segment_id"]) for d in directives
                if isinstance(d, dict) and d.get("action") == "signal_priority"
            }),
        })
