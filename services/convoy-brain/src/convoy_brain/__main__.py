"""Convoy Brain entry point — ``python -m convoy_brain``.

Starts an HTTP server (FastAPI / Uvicorn) on PORT (default 8080 — the port
the frozen nginx.conf, the adversarial suite, and TECHNICAL_DOCS §1 agree on):

    - POST /movements                           → create new movement
    - POST /movements/{movement_id}/plan         → pre-movement workflow (409 while
                                                   a live escort runs — stop it first)
    - POST /movements/{movement_id}/escort       → start the live escort as a
                                                   background task (202; contract C-J)
    - GET  /movements/{movement_id}/escort       → escort status (C-J)
    - POST /movements/{movement_id}/stop         → stop the escort (idempotent)
    - POST /movements/{movement_id}/position     → one convoy GPS fix (REST — the deck
                                                   proxy cannot pass WebSockets);
                                                   mirrored to convoy:position:{id} (C-B);
                                                   409 once the movement is closed
    - POST /movements/{movement_id}/escort/approvals/{approval_id}
                                                 → a named officer approves / rejects
                                                   the escort's ONE open proposal (route
                                                   switch, Plan B, hold, closures): 404
                                                   unknown, 409 expired / superseded /
                                                   already decided, 422 bad body
    - POST /movements/{movement_id}/escort/approve → the same answer, approval_id in the
                                                   body (kept for older decks)
    - GET  /escorts                              → running (and just-ended) escorts with
                                                   their open proposal and its outcomes —
                                                   one read for every console's queue
    - POST /movements/{movement_id}/clear        → post-clearance workflow; the
                                                   corridor event is completed (C-F),
                                                   or cancelled if it never went live
    - POST /chat                                 → free-form orchestrator chat
    - POST /chat/stream                          → NDJSON streaming chat (contract C-D:
                                                   the same two-tier turn as /chat,
                                                   streamed as thought / tool_call /
                                                   tool_result / directive / token /
                                                   done; C-D2: event_id / context_urn
                                                   ground the turn, done.citations)
    - POST /chat/directives/ack                  → the operator applied / dismissed a
                                                   canvas directive (audited, C-D2)
    - POST /mcp                                  → MCP JSON-RPC 2.0 (initialize,
                                                   tools/list|call, resources/list|read,
                                                   prompts/list|get) — read-only tools
                                                   and canvas verbs (C-D2)
    - WS   /ws/convoy/{movement_id}              → real-time convoy events
    - GET  /health                               → readiness probe
    - GET  /health/gpu                           → unified-memory/RSS status
    - GET  /health/services                      → all services health
    - GET  /metrics                              → Prometheus exposition

Two-tier wiring: the decider (model-host :8083) is the strategic brain and
llama-server (:8084) serves Gemma 4 12B as the tactical tier; both come from
config.load_service_urls() (env > veer_models.yaml > compile-time defaults)
and are validated as platform service URLs (http/https on loopback hosts).

The background health loop feeds both tiers' health to the oracle's
degradation ladder (POST /api/v1/adaptive/health) on every poll, so the
city picture's degradation tier reflects the brain's real state.

Escort authority ("officer approves every switch", 2026-10-23): the escort's
decider only PROPOSES a route switch, Plan B, a hold or new closures; each
needs a named officer's approval before it runs, and one nobody answers in
VVIP_ESCORT_APPROVAL_TIMEOUT_SEC expires — the convoy holds its current
approved plan (workflows/live_escort.py).

Officer attribution: the deck forwards the verified session's officer as
``x-veer-officer`` (percent-encoded) on every proxied call and strips any
client copy. The brain binds loopback, so on a loopback connection that
header is the actor of escort start / stop / clear / approval and of a
directive ack; a differing body ``officer`` is kept as ``claimed_officer``
in the audit row and the C-F note (mirror of traffic-oracle
units/router.py authenticated_officer).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import math
import os
import re
import subprocess
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import unquote

import httpx
import structlog
import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from convoy_brain.adaptive_params import AdaptiveParamsClient
from convoy_brain.city_link import SYSTEM_ACTOR, CityLink, clean_label, current_event_id
from convoy_brain.config import (
    ADAPTIVE_HEALTH_TIMEOUT_SEC,
    CITY_BBOX,
    DECIDER_HEALTH_POLL_SEC,
    escort_gps_timeout_sec,
    escort_sim_gps_enabled,
    escort_sim_speed_kmh,
    load_service_urls,
    officer_required,
    tier0_enabled,
)
from convoy_brain.decider import DeciderBrain
from convoy_brain.gps_sim import GpsSimulator, PositionFix, utc_now_iso
from convoy_brain.http_guard import assert_platform_service_url
from convoy_brain.llm.decider_client import DeciderClient
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.mcp.server import MCPServer, ToolExecutor
from convoy_brain.memory.convoy_context import (
    CLOSED_STATUSES,
    ConvoyContext,
    ConvoyContextStore,
)
from convoy_brain.memory.memory_client import MemoryClient
from convoy_brain.metrics import (
    ADAPTIVE_HEALTH_REPORTS,
    ESCORT_POSITIONS,
    LLAMA_HEALTH,
    NOVELTY_LATCH_DISABLED,
    TIER0_ENABLED,
    record_api_request,
)
from convoy_brain.orchestrator import Orchestrator, alert_text, chunk_prose, final_prose
from convoy_brain.workflows.live_escort import (
    CLOSED_EVENT_STATUSES,
    ESCORT_AUTHORITY,
    TERMINAL_STATUSES,
    EscortAlreadyRunningError,
    EscortRegistry,
    EscortRun,
    EscortState,
    EventLifecycle,
    closed_approval_row,
    fix_of,
    plan_route_segments,
    resumable_route,
    run_live_escort_workflow,
    utc_iso,
)
from convoy_brain.workflows.post_clearance import (
    RECOVERY_POLL_INTERVAL_SEC,
    run_post_clearance_workflow,
)
from convoy_brain.workflows.pre_movement import run_pre_movement_workflow

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class CreateMovementRequest(BaseModel):
    origin: tuple[float, float] = Field(..., description="(lon, lat) origin")
    destination: tuple[float, float] = Field(..., description="(lon, lat) destination")
    vvip_class: str = Field(
        "Z",
        description="Protection class of the corridor event: Z+, Z, Y, X, "
        "or none (public/civic event — procession, gathering, race, "
        "egress surge, evacuation, work zone)",
    )
    planned_departure: str = Field(..., description="ISO 8601 departure datetime")
    event_kind: str = Field(
        "protected_movement",
        description="Corridor-event archetype: protected_movement, "
        "procession, mass_gathering, race, ceremonial, egress_surge, "
        "emergency_evacuation, work_zone",
    )
    crowd_class: str = Field(
        "LOW", description="Expected crowd load: LOW, MEDIUM, HIGH, EXTREME"
    )
    title: str | None = Field(
        None, max_length=120,
        description="Optional operator label (e.g. 'ACCEPTANCE TEST escort'): it "
        "prefixes the corridor event's title and rides as metadata.label, so a "
        "test or rehearsal never reads as a real movement",
    )


class PlanRequest(BaseModel):
    origin: tuple[float, float] = Field(..., description="(lon, lat) origin")
    destination: tuple[float, float] = Field(..., description="(lon, lat) destination")
    vvip_class: str = Field("Z", description="VVIP class: Z+, Z, Y, or X")
    planned_departure: str = Field(..., description="ISO 8601 departure datetime")


class EscortRequest(BaseModel):
    destination: tuple[float, float] | None = Field(
        None, description="(lon, lat) destination for emergency re-routing"
    )
    officer: str | None = Field(
        None, min_length=2, max_length=64,
        description="Named duty officer launching the escort (required when "
        "VVIP_REQUIRE_OFFICER=1); recorded on the corridor event's activation",
    )


class OfficerRequest(BaseModel):
    officer: str | None = Field(None, min_length=2, max_length=64)


class ClearRequest(BaseModel):
    officer: str | None = Field(
        None, min_length=2, max_length=64,
        description="Officer recorded on the corridor event's completion",
    )
    max_recovery_sec: float = Field(
        120.0, ge=0.0, le=600.0,
        description="How long to watch queue dissipation before reporting "
        "(bounded so the request fits the deck proxy's budget)",
    )


class PositionRequest(BaseModel):
    lon: float
    lat: float
    speed_kmh: float | None = None
    heading: float | None = None
    t: str | None = Field(None, description="ISO-8601 fix time (default: now)")
    source: Literal["gps", "emulated_gps"] = "gps"


class ApprovalRequest(BaseModel):
    approval_id: str = Field(..., min_length=1, max_length=64)
    decision: Literal["approve", "reject"] = "approve"
    officer: str = Field(..., min_length=2, max_length=64)


class ApprovalDecision(BaseModel):
    """POST /movements/{id}/escort/approvals/{approval_id}: the officer's
    answer. ``officer`` may be omitted when the deck-verified
    x-veer-officer names the officer (a differing body name is kept as
    claimed_officer); with neither, the answer is refused (422)."""

    decision: Literal["approve", "reject"]
    officer: str | None = Field(None, min_length=2, max_length=64)


#: Officer strings the oracle refuses (C-F: control and bidi characters).
_OFFICER_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f‎‏‪-‮⁦-⁩]")

#: The deck-verified officer header (deck hooks.server.ts OFFICER_HEADER).
OFFICER_HEADER = "x-veer-officer"
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
#: POST /mcp body cap (a JSON-RPC message is small; anything larger is refused).
MCP_MAX_BODY_BYTES = 256 * 1024


def _loopback_transport(request: Request) -> bool:
    """True when the request arrived on a loopback interface (scope 'server',
    which no forwarded-for header can rewrite)."""
    server = request.scope.get("server")
    host = server[0] if isinstance(server, (tuple, list)) and server else None
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    return addr.is_loopback or bool(mapped is not None and mapped.is_loopback)


def verified_officer(request: Request) -> str | None:
    """The deck-verified officer named by ``x-veer-officer`` (or None).

    Mirrors traffic-oracle units/router.py authenticated_officer: trusted
    only because the brain binds loopback and the deck — the one LAN-facing
    surface — sets it from a verified session after stripping any client
    copy, so it is honoured only on a loopback connection. One header,
    strict percent-decoding (UTF-8), whitespace collapsed, 2..64
    characters, no control (Cc) or bidi characters; anything else is a 422,
    never silently replaced by the body's officer.
    """
    values = request.headers.getlist(OFFICER_HEADER)
    if not values:
        return None
    if not _loopback_transport(request):
        logger.warning("officer_header.ignored", path=request.url.path,
                       reason="request did not arrive on a loopback interface")
        return None
    if len(values) > 1:
        raise HTTPException(status_code=422, detail=f"{OFFICER_HEADER} must be sent exactly once")
    raw = values[0]
    if not raw.isascii() or _BAD_PERCENT_ESCAPE.search(raw):
        raise HTTPException(
            status_code=422,
            detail=f"{OFFICER_HEADER} must be percent-encoded (encodeURIComponent)",
        )
    try:
        decoded = unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=422, detail=f"{OFFICER_HEADER} is not percent-encoded UTF-8"
        ) from exc
    if any(unicodedata.category(c) == "Cc" for c in decoded) or _OFFICER_FORBIDDEN.search(decoded):
        raise HTTPException(
            status_code=422,
            detail=f"{OFFICER_HEADER} must not contain control or bidi characters",
        )
    name = " ".join(decoded.split())
    if not 2 <= len(name) <= 64:
        raise HTTPException(
            status_code=422, detail=f"{OFFICER_HEADER} must be 2..64 characters (a named officer)"
        )
    return name


def _attributed(
    request: Request, body_officer: str | None, *, required: bool | None = None,
) -> tuple[str | None, str | None]:
    """``(actor, claimed_officer)`` for one officer action.

    The verified header wins; a differing body officer is the claim kept
    for the audit. Without the header the validated body officer is the
    actor (required when VVIP_REQUIRE_OFFICER=1 or ``required``).
    """
    header = verified_officer(request)
    if header is None:
        return _officer(body_officer, required=required), None
    typed = " ".join((body_officer or "").split())
    return header, (typed[:64] if typed and typed != header else None)


def _officer(value: str | None, *, required: bool | None = None) -> str | None:
    """Validated officer name (422 when malformed, or missing when required)."""
    required = officer_required() if required is None else required
    if value is None or not value.strip():
        if required:
            raise HTTPException(
                status_code=422,
                detail="officer is required (VVIP_REQUIRE_OFFICER=1): name the duty officer",
            )
        return None
    name = value.strip()
    if not 2 <= len(name) <= 64 or _OFFICER_FORBIDDEN.search(name):
        raise HTTPException(
            status_code=422,
            detail="officer must be 2..64 characters without control or bidi characters",
        )
    return name


def _finite(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise HTTPException(status_code=422, detail=f"{name} must be a finite number")
    return float(value)


def _fix_from_request(lon: object, lat: object, speed_kmh: object, heading: object,
                      t: object, source: str) -> PositionFix:
    """Validate one fix (finite, inside CITY_BBOX, sane speed/time) or raise 422."""
    min_lon, min_lat, max_lon, max_lat = CITY_BBOX
    lon_v, lat_v = _finite(lon, "lon"), _finite(lat, "lat")
    if lon_v is None or lat_v is None:
        raise HTTPException(status_code=422, detail="lon/lat must be finite numbers")
    if not (min_lon <= lon_v <= max_lon and min_lat <= lat_v <= max_lat):
        raise HTTPException(
            status_code=422,
            detail=(
                f"position ({lon_v}, {lat_v}) is outside the city operating area "
                f"(lon {min_lon}–{max_lon}, lat {min_lat}–{max_lat})"
            ),
        )
    speed = _finite(speed_kmh, "speed_kmh")
    if speed is not None and not 0 <= speed <= 250:
        raise HTTPException(status_code=422, detail="speed_kmh must be within 0..250")
    bearing = _finite(heading, "heading")
    if bearing is not None:
        bearing = bearing % 360.0
    if t is None:
        stamp = utc_now_iso()
    elif not isinstance(t, str):
        raise HTTPException(status_code=422, detail="t must be an ISO-8601 time")
    else:
        text = t.strip()
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="t must be an ISO-8601 time") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.timestamp() > time.time() + 30:
            raise HTTPException(status_code=422, detail="t is in the future")
        stamp = parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
    return PositionFix(
        lon=lon_v, lat=lat_v, speed_kmh=speed, heading=bearing, t=stamp, source=source,
    )


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = Field(
        None,
        description="Continue a session (kept 15 min after its last turn); "
        "the response / done event names the session actually used",
    )
    movement_id: str | None = None
    vvip_class: str | None = None
    event_id: str | None = Field(
        None, max_length=64,
        description="The operator's selected corridor event (C-D2): resolved once "
        "and stated in the session context; event tools are scoped to it",
    )
    context_urn: str | None = Field(
        None, max_length=160,
        description="The entity the operator asks about ('<kind>:<id>', C-D2) — "
        "a dossier's 'Explain this'; resolved through city_get_dossier",
    )


class DirectiveAck(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    directive_id: str = Field(..., min_length=1, max_length=64)
    decision: Literal["applied", "dismissed"]
    officer: str | None = Field(None, min_length=2, max_length=64)


class ProtocolStateUpdate(BaseModel):
    asl_checklist: dict | None = None
    protocol_compliance: dict | None = None
    anti_sabotage: dict | None = None
    transit_status: dict | None = None
    plan_b: dict | None = None
    threat_level: str | None = None


class DossierRequest(BaseModel):
    vvip_class: str = Field("Z", description="VVIP class for dossier generation")
    origin_name: str | None = Field(None, description="Human-readable origin name")
    destination_name: str | None = Field(None, description="Human-readable destination name")
    include_sections: list[str] | None = Field(
        None, description="Specific dossier sections to generate, or all if None"
    )


class AnalyticsReasoningRequest(BaseModel):
    metric_name: str = Field(..., description="Name of the analytics metric to reason about")
    metric_value: float | str = Field(..., description="Current value of the metric")
    metric_context: dict = Field(default_factory=dict, description="Additional context — related metrics, thresholds, history")
    vvip_class: str = Field("Z", description="Active VVIP class")


class RecommendationReasoningRequest(BaseModel):
    statement: str = Field(..., description="The recommendation statement to explain")
    category: str = Field("general", description="Category: speed, congestion, security, routing, incident, environmental")
    ground_data: dict = Field(default_factory=dict, description="Current ground sensor data snapshot")
    vvip_class: str = Field("Z", description="Active VVIP class")


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------


class ConvoyConnectionManager:
    """Manages WebSocket connections per movement for real-time event push."""

    def __init__(self) -> None:
        self._connections: dict[str, list[WebSocket]] = {}

    async def connect(self, movement_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.setdefault(movement_id, []).append(ws)
        logger.info("ws.connect", movement_id=movement_id)

    def disconnect(self, movement_id: str, ws: WebSocket) -> None:
        conns = self._connections.get(movement_id, [])
        if ws in conns:
            conns.remove(ws)
        if not conns:
            self._connections.pop(movement_id, None)
        logger.info("ws.disconnect", movement_id=movement_id)

    async def broadcast(self, movement_id: str, event: dict) -> None:
        """Send an event to all clients subscribed to a movement."""
        conns = self._connections.get(movement_id, [])
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(movement_id, ws)


ws_manager = ConvoyConnectionManager()


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


class _ApiMetricsMiddleware:
    """Pure-ASGI request metrics (never buffers a streamed body).

    Labels use the matched route template (``/movements/{movement_id}/plan``),
    never the raw path, so label cardinality stays bounded.
    """

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = {"code": 500}

        async def send_wrapper(message) -> None:  # type: ignore[no-untyped-def]
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            record_api_request(
                str(scope.get("method", "?")),
                getattr(route, "path", None) or "unmatched",
                status["code"],
                time.monotonic() - started,
            )


def create_app() -> FastAPI:
    """Build the FastAPI application with lifespan-managed infrastructure."""
    app = FastAPI(
        title="Convoy Brain",
        version="0.2.0",
        description="Two-tier agentic orchestration for VVIP convoy planning",
    )
    app.add_middleware(_ApiMetricsMiddleware)

    @app.on_event("startup")
    async def startup() -> None:
        urls = load_service_urls()
        valkey_url = urls.valkey

        llama_bridge = LlamaBridge(base_url=urls.llama)
        decider_client = DeciderClient(base_url=urls.model_host)
        memory_client = MemoryClient(base_url=urls.embedding)
        decider_brain = DeciderBrain(
            decider_client,
            memory_client=memory_client,
            tier0_enabled=tier0_enabled(),  # env kill switch: DECIDER_TIER0_DISABLE
        )
        NOVELTY_LATCH_DISABLED.set(0)
        TIER0_ENABLED.set(1 if decider_brain.tier0_enabled else 0)
        executor = ToolExecutor(traffic_oracle_url=urls.traffic_oracle)
        adaptive_client = AdaptiveParamsClient(base_url=urls.traffic_oracle)
        city_link = CityLink(base_url=urls.traffic_oracle)
        context_store = ConvoyContextStore(valkey_url=valkey_url)
        mcp_server = MCPServer(tool_executor=executor)
        orchestrator = Orchestrator(
            llama_bridge,
            executor,
            context_store,
            decider_client=decider_client,
            decider_brain=decider_brain,
        )

        app.state.urls = urls
        app.state.bridge = llama_bridge
        app.state.decider_client = decider_client
        app.state.decider_brain = decider_brain
        app.state.memory_client = memory_client
        app.state.adaptive_client = adaptive_client
        app.state.city_link = city_link
        app.state.executor = executor
        app.state.context_store = context_store
        app.state.mcp_server = mcp_server
        app.state.orchestrator = orchestrator
        app.state.ws_manager = ws_manager
        app.state.lifecycle = EventLifecycle(base_url=urls.traffic_oracle)
        app.state.escorts = EscortRegistry()
        app.state.health_client = httpx.AsyncClient(timeout=ADAPTIVE_HEALTH_TIMEOUT_SEC)
        app.state.oracle_url = assert_platform_service_url(urls.traffic_oracle.rstrip("/"))

        # Warm the decider in the background so the first checkpoint is fast;
        # the task is kept so shutdown can cancel it.
        app.state.health_task = asyncio.create_task(
            _background_health_loop(app), name="convoy-brain:health"
        )

        logger.info(
            "convoy_brain.startup",
            model_host=urls.model_host,
            llama=urls.llama,
            embedding=urls.embedding,
            traffic_oracle=urls.traffic_oracle,
            config_source=urls.source,
        )

    @app.on_event("shutdown")
    async def shutdown() -> None:
        task = getattr(app.state, "health_task", None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await app.state.escorts.shutdown()
        await app.state.executor.close()
        await app.state.decider_client.aclose()
        await app.state.bridge.aclose()
        await app.state.adaptive_client.aclose()
        await app.state.city_link.aclose()
        await app.state.lifecycle.aclose()
        await app.state.health_client.aclose()
        await app.state.memory_client.aclose()
        await app.state.context_store.close()
        logger.info("convoy_brain.shutdown")

    # -------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict:
        decider_health = await app.state.decider_client.health()
        llama_ok = await app.state.bridge.check_health()
        LLAMA_HEALTH.set(1 if llama_ok else 0)
        decider_ok = bool(decider_health and decider_health.get("loaded"))
        degraded = app.state.decider_brain.degraded
        return {
            "status": "ok" if (decider_ok and llama_ok) else "degraded",
            "strategic_tier": {
                "model": "pplx-decider-v1-27b",
                # A reachable host that is still loading weights is "loading"
                # (amber), never "unreachable" (down).
                "model_host": _model_host_state(decider_health),
                "load_error": (decider_health or {}).get("load_error"),
                "degraded": degraded,
                "degraded_reason": app.state.decider_brain.degraded_reason,
                "p95_decide_latency_ms": app.state.decider_client.p95_decide_latency_ms,
                "novelty_latch": (
                    "armed" if app.state.decider_brain.novelty_latch_enabled
                    else "disabled"
                ),
                "tier0_cache": (
                    "enabled" if app.state.decider_brain.tier0_enabled
                    else "disabled"
                ),
                # VVIP_MEMORY_RECORD: whether served checkpoints are upserted
                # into the memory corpus (sandbox brains run it disabled).
                "memory_record": (
                    "enabled" if app.state.decider_brain.memory_record_enabled
                    else "disabled"
                ),
            },
            "tactical_tier": {
                "model": "aditi-ps18-v7h-gemma4-12b",
                "llama_server": "connected" if llama_ok else "unreachable",
            },
        }

    @app.get("/health/gpu")
    async def gpu_health() -> dict:
        """Memory status — GB10 unified pool; consumed by command-deck.

        nvidia-smi reports memory.total as N/A on GB10, so /proc/meminfo is
        the primary source and model-host RSS (from /decider/health) is the
        model-tier allocation view.
        """
        total_mib = used_mib = None
        try:
            with open("/proc/meminfo", encoding="ascii") as fh:
                info = {}
                for line in fh:
                    key, _, value = line.partition(":")
                    info[key.strip()] = int(value.strip().split()[0]) // 1024
            total_mib = info.get("MemTotal")
            used_mib = info.get("MemTotal", 0) - info.get("MemAvailable", 0)
        except (OSError, ValueError):
            pass

        if total_mib is None:
            # Last resort — legacy discrete-GPU path (not this hardware).
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.total,memory.used",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, check=True, timeout=5,
                )
                total_mib, used_mib = (int(p) for p in result.stdout.strip().split(","))
            except (subprocess.SubprocessError, FileNotFoundError, ValueError):
                total_mib, used_mib = 0, 0

        decider_health = await app.state.decider_client.health()
        llama_ok = await app.state.bridge.check_health()
        decider_rss_mib = int(float(decider_health.get("resident_rss_gib") or 0) * 1024)

        # Planned budgets from the architecture's memory plan (GiB -> MiB):
        # llama-server ~9.5, embedding-service ~5.0, traffic-oracle ~3.5.
        llama_budget_mib = 9728 if llama_ok else 0
        embedding_budget_mib = 5120
        oracle_budget_mib = 3584

        allocated = decider_rss_mib + llama_budget_mib + embedding_budget_mib + oracle_budget_mib
        return {
            "vramTotalMb": total_mib,
            "vramUsedMb": used_mib,
            "vramFreeMb": (total_mib - used_mib) if total_mib else 0,
            "gpuUtilPercent": None,  # not reported by nvidia-smi on GB10
            "allocations": {
                "deciderRssMiB": decider_rss_mib,
                "llamaServerBudgetMiB": llama_budget_mib,
                "embeddingServiceBudgetMiB": embedding_budget_mib,
                "trafficOracleBudgetMiB": oracle_budget_mib,
                "headroom": (total_mib - allocated) if total_mib else None,
            },
            "pool": "unified",
        }

    @app.get("/health/services")
    async def services_health() -> list[dict]:
        """Health status for all platform services (fixed internal endpoints)."""
        urls = app.state.urls

        async with httpx.AsyncClient(timeout=3.0) as client:
            async def probe(name: str, url: str, path: str) -> dict:
                start = time.monotonic()
                try:
                    resp = await client.get(url + path)
                    latency = round((time.monotonic() - start) * 1000, 1)
                    try:
                        details: dict = resp.json() if resp.status_code == 200 else {}
                    except ValueError:
                        details = {}
                    return {
                        "name": name,
                        "status": "online" if resp.status_code == 200 else "degraded",
                        "latencyMs": latency,
                        "lastChecked": int(time.time() * 1000),
                        "details": details,
                    }
                except Exception:
                    return {
                        "name": name,
                        "status": "offline",
                        "latencyMs": None,
                        "lastChecked": int(time.time() * 1000),
                        "details": {},
                    }

            services = [
                await probe("traffic-oracle", urls.traffic_oracle, "/health"),
                await probe("model-host (decider)", urls.model_host, "/decider/health"),
                await probe("llama-server (Gemma)", urls.llama, "/health"),
                await probe("embedding-service", urls.embedding, "/health"),
            ]

        # Self (convoy-brain) — measured in-process to avoid a self-call.
        services.append({
            "name": "convoy-brain",
            "status": "online",
            "latencyMs": 0.1,
            "lastChecked": int(time.time() * 1000),
            "details": {"degraded": app.state.decider_brain.degraded},
        })
        return services

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        from convoy_brain.metrics import get_metrics_response
        payload, content_type = get_metrics_response()
        return Response(content=payload, media_type=content_type)

    # -------------------------------------------------------------------
    # Movement creation
    # -------------------------------------------------------------------

    @app.post("/movements")
    async def create_movement(req: CreateMovementRequest) -> dict:
        movement_id = str(uuid.uuid4())
        context_store: ConvoyContextStore = app.state.context_store
        ctx = ConvoyContext(
            movement_id=movement_id,
            vvip_class=req.vvip_class,
            status="planning",
            event_kind=req.event_kind,
            crowd_class=req.crowd_class,
            label=clean_label(req.title),
        )
        await context_store.put(ctx)

        logger.info(
            "movement.created",
            movement_id=movement_id,
            vvip_class=req.vvip_class,
            event_kind=req.event_kind,
            crowd_class=req.crowd_class,
            label=ctx.label,
        )

        await ws_manager.broadcast(movement_id, {
            "type": "convoy.status",
            "timestamp": int(time.time() * 1000),
            "payload": {
                "movementId": movement_id,
                "status": "planning",
                "vvipClass": req.vvip_class,
            },
        })

        return {"movement_id": movement_id, "label": ctx.label}

    # -------------------------------------------------------------------
    # Workflow endpoints
    # -------------------------------------------------------------------

    @app.post("/movements/{movement_id}/plan")
    async def plan_movement(movement_id: str, req: PlanRequest) -> dict:
        # A re-plan under a running escort would supersede the ACTIVE corridor
        # event and rewrite the context the escort is driving on.
        live = app.state.escorts.active(movement_id)
        if live is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"a live escort is running for this movement (task {live.task_id}, "
                    f"{live.state.status}) — POST /movements/{movement_id}/stop before "
                    "re-planning"
                ),
            )
        try:
            result = await run_pre_movement_workflow(
                movement_id=movement_id,
                origin=req.origin,
                destination=req.destination,
                vvip_class=req.vvip_class,
                planned_departure=req.planned_departure,
                bridge=app.state.bridge,
                executor=app.state.executor,
                context_store=app.state.context_store,
                decider_brain=app.state.decider_brain,
                adaptive_client=app.state.adaptive_client,
                city_link=app.state.city_link,
            )

            await ws_manager.broadcast(movement_id, {
                "type": "convoy.status",
                "timestamp": int(time.time() * 1000),
                "payload": {
                    "movementId": movement_id,
                    "status": "approved" if result.get("status") == "approved" else "planning",
                    "planStatus": result.get("status"),
                    "eventId": result.get("event_id"),
                },
            })

            return result
        except Exception as exc:
            logger.error("plan_movement.failed", movement_id=movement_id, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    # -------------------------------------------------------------------
    # Live escort (contract C-J): background task, status, stop, GPS
    # -------------------------------------------------------------------

    async def accept_position(
        movement_id: str, fix: PositionFix,
    ) -> tuple[ConvoyContext | None, bool]:
        """The single GPS accept path (REST, WebSocket and the emulated walker).

        Field-scoped context update (never a stale snapshot), the C-B mirror
        ``convoy:position:{movement_id}`` (EX 30 s) and a WS broadcast.
        Returns ``(ctx, applied)``; ctx is None for an unknown movement.
        """
        store: ConvoyContextStore = app.state.context_store
        ctx, applied = await store.update_position(
            movement_id, fix.lon, fix.lat,
            speed_kmh=fix.speed_kmh, heading=fix.heading, t=fix.t, source=fix.source,
        )
        if ctx is None:
            return None, False
        if ctx.status in CLOSED_STATUSES:
            return ctx, False  # the movement is over: no fix, no C-B frame
        run = app.state.escorts.get(movement_id)
        if run is not None:
            run.note_fix(fix)
        if not applied:
            return ctx, False
        loc = run.locate(fix.lon, fix.lat) if run is not None and run.alive else None
        frame = {
            "movement_id": movement_id,
            "event_id": ctx.event_id or current_event_id(ctx),
            "lon": fix.lon,
            "lat": fix.lat,
            "speed_kmh": fix.speed_kmh,
            "heading": fix.heading,
            "t": fix.t,
            "source": fix.source,
            "progress": round(loc.progress, 4) if loc is not None else None,
            "segment_id": loc.segment_id if loc is not None else None,
        }
        try:
            await store.write_position_frame(movement_id, frame)
        except Exception as exc:
            logger.warning("position.mirror_failed", movement_id=movement_id, error=str(exc)[:200])
        ESCORT_POSITIONS.labels(source=fix.source).inc()
        await ws_manager.broadcast(movement_id, {
            "type": "convoy.position",
            "timestamp": int(time.time() * 1000),
            "payload": {
                "movementId": movement_id,
                "position": [fix.lon, fix.lat],
                "speedKmh": fix.speed_kmh,
                "headingDeg": fix.heading,
                "source": fix.source,
                "progress": frame["progress"],
            },
        })
        return ctx, True

    async def persist_escort(run: EscortRun) -> None:
        doc = run.state.to_doc()
        await app.state.context_store.write_escort_status(run.movement_id, doc)
        await ws_manager.broadcast(run.movement_id, {
            "type": "convoy.escort",
            "timestamp": int(time.time() * 1000),
            "payload": doc,
        })

    async def simulate_gps(run: EscortRun, start: tuple[float, float] | None) -> None:
        """EMULATED GPS walker for one escort (VVIP_ESCORT_SIM_GPS=1)."""
        sim = GpsSimulator(
            movement_id=run.movement_id,
            get_route=lambda: run.route,
            publish=lambda fix: accept_position(run.movement_id, fix),
            cruise_kmh=escort_sim_speed_kmh(),
            is_holding=lambda: run.holding,
            should_stop=lambda: run.finished or run.stop_requested or run.real_gps_seen,
            start=start,
            # The loop retries route geometry every ROUTE_RETRY_SEC: wait for
            # it as long as the escort itself waits for a fix.
            route_wait_sec=escort_gps_timeout_sec(),
        )
        outcome = await sim.run()
        if run.real_gps_seen and outcome == "stopped":
            outcome = "real_gps"
        with contextlib.suppress(Exception):
            await app.state.context_store.append_decision(run.movement_id, {
                "phase": "live_escort",
                "action": "emulated_gps_ended",
                "reason": outcome,
                "fixes_published": sim.published,
                "timestamp": time.time(),
            })

    async def escort_main(run: EscortRun) -> dict | None:
        store: ConvoyContextStore = app.state.context_store
        try:
            if run.sim_gps:
                ctx = await store.get(run.movement_id)
                start = tuple(ctx.convoy_position) if ctx and ctx.convoy_position else None
                run.sim_task = asyncio.create_task(
                    simulate_gps(run, start), name=f"escort-gps:{run.movement_id}"
                )
            return await run_live_escort_workflow(
                movement_id=run.movement_id,
                destination=run.destination,
                bridge=app.state.bridge,
                executor=app.state.executor,
                context_store=store,
                decider_brain=app.state.decider_brain,
                adaptive_client=app.state.adaptive_client,
                run=run,
                lifecycle=app.state.lifecycle,
                publish=persist_escort,
                gps_timeout_sec=escort_gps_timeout_sec(),
                city_link=app.state.city_link,
            )
        except asyncio.CancelledError:
            run.state.status = "stopped"
            run.state.detail = "escort task cancelled (brain shutting down)"
            with contextlib.suppress(Exception):
                await persist_escort(run)
            raise
        except Exception as exc:
            logger.error("escort.task_failed", movement_id=run.movement_id, error=str(exc))
            run.state.status = "failed"
            run.state.detail = f"escort task error: {str(exc)[:200]}"
            with contextlib.suppress(Exception):
                await persist_escort(run)
                await store.append_decision(run.movement_id, {
                    "phase": "live_escort", "action": "escort_failed",
                    "detail": run.state.detail, "timestamp": time.time(),
                })
            return None
        finally:
            if run.sim_task is not None and not run.sim_task.done():
                run.sim_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await run.sim_task

    @app.post("/movements/{movement_id}/escort", status_code=202)
    async def start_escort(
        movement_id: str, request: Request,
        req: Annotated[EscortRequest | None, Body()] = None,
    ) -> JSONResponse:
        """Start the live escort as a background task (contract C-J).

        202 at once; 409 while one is already running, or when the movement
        or its corridor event is closed (cancelled / completed on /ops); 404
        unknown movement; 422 without a planned primary route. The deck's
        verified ``x-veer-officer`` is the recorded officer.
        """
        req = req or EscortRequest()
        officer, claimed = _attributed(request, req.officer)
        store: ConvoyContextStore = app.state.context_store
        ctx = await store.get(movement_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail="Movement not found")
        if ctx.status in ("completed", "cancelled"):
            return JSONResponse(status_code=409, content={
                "detail": f"movement is {ctx.status}; plan a new movement to escort again",
                "status": ctx.status,
            })
        event_id = ctx.event_id or current_event_id(ctx)
        status_of = getattr(app.state.lifecycle, "status", None)
        if event_id and status_of is not None and app.state.escorts.active(movement_id) is None:
            event = await status_of(event_id)
            if isinstance(event, dict) and event.get("status") in CLOSED_EVENT_STATUSES:
                return JSONResponse(status_code=409, content={
                    "detail": (
                        f"corridor event {event_id} is {event['status']} — an officer closed it; "
                        "plan a new movement to escort again"
                    ),
                    "status": event["status"],
                    "event_id": event_id,
                })
        route = plan_route_segments(ctx)
        if not route:
            raise HTTPException(
                status_code=422,
                detail="movement has no plan with a primary route — "
                       f"POST /movements/{movement_id}/plan first",
            )
        registry: EscortRegistry = app.state.escorts
        # A resumed escort continues on the route an earlier run switched to
        # (re-route / Plan B) instead of snapping back to the plan.
        persisted = None
        if registry.active(movement_id) is None:
            with contextlib.suppress(Exception):
                persisted = await store.read_escort_status(movement_id)
        resumed = resumable_route(persisted, ctx)
        run = EscortRun(
            movement_id,
            officer=officer,
            route_segment_ids=resumed[0] if resumed else route,
            route_source=resumed[1] if resumed else "plan",
            destination=tuple(req.destination) if req.destination else None,
            sim_gps=escort_sim_gps_enabled(),
            claimed_officer=claimed,
        )
        try:
            registry.start(run, escort_main)
        except EscortAlreadyRunningError as exc:
            return JSONResponse(status_code=409, content={
                "detail": "an escort task is already running for this movement",
                "status": exc.run.state.status,
                "task_id": exc.run.task_id,
            })
        logger.info("escort.started", movement_id=movement_id, task_id=run.task_id,
                    officer=officer, sim_gps=run.sim_gps, route=run.route_segment_ids,
                    route_source=run.route_source)
        await ws_manager.broadcast(movement_id, {
            "type": "convoy.status",
            "timestamp": int(time.time() * 1000),
            "payload": {"movementId": movement_id, "status": "active"},
        })
        return JSONResponse(status_code=202, content={
            "movement_id": movement_id,
            "status": "running",
            "task_id": run.task_id,
        })

    @app.get("/movements/{movement_id}/escort")
    async def escort_status(movement_id: str) -> dict:
        """Escort status document (contract C-J)."""
        store: ConvoyContextStore = app.state.context_store
        ctx = await store.get(movement_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail="Movement not found")
        run = app.state.escorts.get(movement_id)
        if run is not None:
            doc = run.state.to_doc()
            if not run.alive and doc["status"] not in TERMINAL_STATUSES:
                doc["status"] = "failed"
                doc["detail"] = doc.get("detail") or "escort task ended unexpectedly"
        else:
            persisted = None
            with contextlib.suppress(Exception):
                persisted = await store.read_escort_status(movement_id)
            if persisted:
                doc = persisted
                if doc.get("status") not in (*TERMINAL_STATUSES, "idle"):
                    doc["status"] = "stopped"
                    doc["detail"] = (
                        "escort task is not running in this brain process (restarted?) — "
                        f"POST /movements/{movement_id}/escort to resume"
                    )
            else:
                doc = EscortState(movement_id=movement_id).to_doc()
        if run is None or not run.alive:
            # A proposal is answerable only while its escort runs.
            doc["pending_approval"] = None
            doc["pending_approvals"] = []
        fix = fix_of(ctx)
        if fix is not None:
            doc["position"] = {
                "lon": fix.lon, "lat": fix.lat, "speed_kmh": fix.speed_kmh,
                "heading": fix.heading, "t": fix.t or None, "source": fix.source,
            }
        doc["event_id"] = doc.get("event_id") or ctx.event_id or current_event_id(ctx)
        return doc

    @app.post("/movements/{movement_id}/stop")
    async def stop_escort(
        movement_id: str, request: Request,
        req: Annotated[OfficerRequest | None, Body()] = None,
    ) -> dict:
        """Stop the escort task (idempotent). The corridor event stays as it
        is — /clear completes it once traffic is restored."""
        req = req or OfficerRequest()
        officer, claimed = _attributed(request, req.officer)
        store: ConvoyContextStore = app.state.context_store
        if await store.get(movement_id) is None:
            raise HTTPException(status_code=404, detail="Movement not found")
        reason = f"stopped by {officer}" if officer else "stopped by operator request"
        if claimed:
            await store.append_decision(movement_id, {
                "phase": "live_escort", "action": "stop_requested", "officer": officer,
                "claimed_officer": claimed, "officer_source": OFFICER_HEADER,
                "timestamp": time.time(),
            })
        run = await app.state.escorts.stop(movement_id, reason, officer=officer or SYSTEM_ACTOR)
        return {
            "movement_id": movement_id,
            "status": "stopped",
            "task_id": run.task_id if run else None,
        }

    @app.post("/movements/{movement_id}/position", status_code=202)
    async def post_position(movement_id: str, req: PositionRequest) -> JSONResponse:
        """One convoy GPS fix (REST twin of the WS position.update)."""
        fix = _fix_from_request(req.lon, req.lat, req.speed_kmh, req.heading, req.t, req.source)
        ctx, applied = await accept_position(movement_id, fix)
        if ctx is None:
            raise HTTPException(status_code=404, detail="Movement not found")
        if ctx.status in CLOSED_STATUSES:
            return JSONResponse(status_code=409, content={
                "detail": f"movement is {ctx.status}; position fixes are no longer accepted",
                "status": ctx.status,
            })
        return JSONResponse(status_code=202, content={
            "accepted": True,
            "applied": applied,
            "t": fix.t,
            **({} if applied else {"detail": "older than the stored fix; not applied"}),
        })

    async def decide_proposal(
        movement_id: str, approval_id: str, decision: str, officer: str,
        claimed: str | None, *, alive_only: bool = False,
    ) -> JSONResponse:
        """A named officer answers the escort's open proposal.

        approve → queued; the escort loop applies exactly what was proposed
        on its next tick (the route switch with its C-O spine update and
        closures, or the hold) under the officer's name. reject → recorded;
        the convoy keeps its current plan. 404 unknown movement / escort /
        approval; 409 when the proposal expired (no officer answered in
        time — the convoy held its current plan — or its escort is no longer
        running, so nothing would apply it), was superseded by a newer one
        or was already decided.
        """
        store: ConvoyContextStore = app.state.context_store
        if alive_only:
            run = app.state.escorts.active(movement_id)
            if run is None:
                raise HTTPException(status_code=404, detail="no escort running for this movement")
        else:
            if await store.get(movement_id) is None:
                raise HTTPException(status_code=404, detail="Movement not found")
            run = app.state.escorts.get(movement_id)
            if run is None:
                raise HTTPException(
                    status_code=404,
                    detail="no escort for this movement in this brain process — nothing to approve",
                )
        if not run.alive:
            # The escort ended (or its task died) with a proposal still open:
            # nothing would ever apply an approval, so it expires instead.
            for stale in run.expire_pending("the escort is not running — nothing to apply"):
                await store.append_decision(movement_id, dict(
                    closed_approval_row(stale, "approval_expired"), timestamp=time.time(),
                ))
        row, outcome = run.decide(approval_id, decision, officer, claimed)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown approval_id")
        if outcome == "expired":
            await store.append_decision(movement_id, dict(
                closed_approval_row(row, "approval_expired"), timestamp=time.time(),
                answered_late_by=officer,
            ))
        if outcome in ("expired", "closed"):
            status = row["status"]
            why = row.get("expiry_reason") or "no officer response"
            detail = {
                "expired": f"proposal expired — {why}",
                "superseded": "proposal superseded by a newer one — answer the open proposal",
            }.get(status, f"proposal already {status}"
                  + (f" by {row['decided_by']}" if row.get("decided_by") else ""))
            return JSONResponse(status_code=409, content={
                "detail": detail, "status": status, "approval_id": approval_id,
                "decided_by": row.get("decided_by"),
                "superseded_by": row.get("superseded_by"),
            })
        if row["status"] == "rejected":
            await store.append_decision(movement_id, {
                "phase": "live_escort",
                "action": "operator_rejected",
                "rejected_action": row["action"],
                "kind": row.get("kind"),
                "summary": row.get("summary"),
                "approval_id": approval_id,
                "officer": officer,
                **({"claimed_officer": claimed} if claimed else {}),
                "reason": "rejected by a named officer — the convoy keeps its current plan",
                "timestamp": time.time(),
            })
        logger.info("escort.proposal_decided", movement_id=movement_id, approval_id=approval_id,
                    status=row["status"], officer=officer, claimed_officer=claimed)
        return JSONResponse(status_code=200, content={
            "movement_id": movement_id,
            "approval_id": approval_id,
            "status": row["status"],
            "action": row["action"],
            "kind": row.get("kind"),
            "summary": row.get("summary"),
            "decided_by": row.get("decided_by"),
            "decided_at": row.get("decided_at"),
            **({"claimed_officer": claimed} if claimed else {}),
        })

    @app.post("/movements/{movement_id}/escort/approvals/{approval_id}")
    async def decide_escort_proposal(
        movement_id: str, approval_id: str, req: ApprovalDecision, request: Request,
    ) -> JSONResponse:
        """A named officer approves or rejects the escort's open proposal.
        The deck's verified ``x-veer-officer`` is the deciding officer; a
        differing body ``officer`` is kept as ``claimed_officer``."""
        officer, claimed = _attributed(request, req.officer, required=True)
        return await decide_proposal(movement_id, approval_id, req.decision, officer, claimed)

    @app.post("/movements/{movement_id}/escort/approve")
    async def approve_escort_action(
        movement_id: str, req: ApprovalRequest, request: Request,
    ) -> JSONResponse:
        """The same answer with ``approval_id`` in the body (older decks)."""
        officer, claimed = _attributed(request, req.officer, required=True)
        return await decide_proposal(movement_id, req.approval_id, req.decision, officer,
                                     claimed, alive_only=True)

    @app.get("/escorts")
    async def list_escorts(recent_sec: float = 900.0) -> dict:
        """Running escorts — and ones that ended in the last ``recent_sec`` —
        with their open proposal and recent outcomes: one read feeds every
        console's escort lane (HUD, decision queue, city vitals)."""
        now = time.time()
        out: list[dict] = []
        for run in app.state.escorts.runs():
            mid = run.movement_id
            doc = run.state.to_doc()
            if not run.alive:
                ended = doc.get("ended_at") or doc.get("updated_at")
                try:
                    ended_at = datetime.fromisoformat(str(ended).replace("Z", "+00:00"))
                except ValueError:
                    continue
                age = now - ended_at.timestamp()
                if age > max(0.0, min(recent_sec, 3600.0)):
                    continue
            out.append({
                "movement_id": mid,
                "event_id": doc.get("event_id"),
                "status": doc.get("status") if run.alive or doc.get("status") in TERMINAL_STATUSES
                else "failed",
                "alive": run.alive,
                "officer": doc.get("officer"),
                "started_at": doc.get("started_at"),
                "updated_at": doc.get("updated_at"),
                "progress": doc.get("progress"),
                "gps_mode": (doc.get("gps") or {}).get("mode"),
                "traffic_simulated": (doc.get("traffic_feed") or {}).get("simulated"),
                # a proposal is answerable only while its escort runs
                "pending_approval": doc.get("pending_approval") if run.alive else None,
                "approval_outcomes": doc.get("approval_outcomes") or [],
                "approval_timeout_sec": doc.get("approval_timeout_sec"),
            })
        return {
            "escorts": out,
            "count": len(out),
            "pending": sum(1 for e in out if e["pending_approval"]),
            "authority": ESCORT_AUTHORITY,
            "t": utc_iso(now),
        }

    @app.post("/movements/{movement_id}/clear")
    async def clear_movement(
        movement_id: str, request: Request,
        req: Annotated[ClearRequest | None, Body()] = None,
    ) -> dict:
        req = req or ClearRequest()
        officer, claimed = _attributed(request, req.officer)
        claim_note = f" [claimed officer: {claimed}]" if claimed else ""
        if await app.state.context_store.get(movement_id) is None:
            raise HTTPException(status_code=404, detail="Movement not found")
        try:
            # The convoy has passed: retire the escort loop first.
            await app.state.escorts.stop(
                movement_id, "cleared — post-clearance restoration started",
                officer=officer or SYSTEM_ACTOR,
            )
            result = await run_post_clearance_workflow(
                movement_id=movement_id,
                bridge=app.state.bridge,
                executor=app.state.executor,
                context_store=app.state.context_store,
                decider_brain=app.state.decider_brain,
                max_recovery_iterations=max(
                    1, math.ceil(req.max_recovery_sec / RECOVERY_POLL_INTERVAL_SEC)
                ),
            )

            # C-F: the corridor event is completed. One that never went live
            # (cleared before any escort: planning / approved, from which
            # "completed" is illegal) is cancelled instead — never left
            # dangling on the active list, never a fabricated completion.
            ctx = await app.state.context_store.get(movement_id)
            event_id = (ctx.event_id or current_event_id(ctx)) if ctx else None
            actor = officer or SYSTEM_ACTOR
            transitions = [await app.state.lifecycle.transition(
                event_id, "completed", actor,
                note=f"post-clearance complete (movement {movement_id}){claim_note}",
            )]
            if (transitions[0]["outcome"] == "conflict"
                    and transitions[0].get("from_status") in ("planning", "approved")):
                transitions.append(await app.state.lifecycle.transition(
                    event_id, "cancelled", actor,
                    note=(f"movement {movement_id} cleared before any live escort — "
                          f"the event never went live{claim_note}"),
                ))
            if event_id:
                for transition in transitions:
                    await app.state.context_store.append_decision(movement_id, {
                        "phase": "city_event",
                        "action": f"event_{transition['outcome']}",
                        "event_id": event_id,
                        "to_status": transition["to_status"],
                        "officer": actor,
                        **({"claimed_officer": claimed} if claimed else {}),
                        "http_status": transition.get("http_status"),
                        "from_status": transition.get("from_status"),
                        "timestamp": time.time(),
                    })
            result["event_transition"] = transitions[-1]
            result["event_transitions"] = transitions

            await ws_manager.broadcast(movement_id, {
                "type": "convoy.status",
                "timestamp": int(time.time() * 1000),
                "payload": {"movementId": movement_id, "status": "completed"},
            })

            return result
        except Exception as exc:
            logger.error("clear_movement.failed", movement_id=movement_id, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    # -------------------------------------------------------------------
    # Protocol state management (Blue Book integration)
    # -------------------------------------------------------------------

    @app.get("/movements/{movement_id}/protocol")
    async def get_protocol_state(movement_id: str) -> dict:
        """Retrieve the current Blue Book protocol state for a movement."""
        context_store: ConvoyContextStore = app.state.context_store
        ctx = await context_store.get(movement_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail="Movement not found")
        return {
            "movement_id": movement_id,
            "protocol_state": ctx.protocol_state,
        }

    @app.post("/movements/{movement_id}/protocol")
    async def update_protocol_state(movement_id: str, req: ProtocolStateUpdate) -> dict:
        """Update Blue Book protocol state and persist to Valkey."""
        context_store: ConvoyContextStore = app.state.context_store
        update_data = {k: v for k, v in req.model_dump().items() if v is not None}
        if not update_data:
            raise HTTPException(status_code=400, detail="No fields to update")

        ctx = await context_store.update_protocol_state(movement_id, update_data)
        if ctx is None:
            raise HTTPException(status_code=404, detail="Movement not found")

        logger.info("protocol.updated", movement_id=movement_id, fields=list(update_data.keys()))
        return {
            "movement_id": movement_id,
            "protocol_state": ctx.protocol_state,
            "updated_fields": list(update_data.keys()),
        }

    @app.post("/movements/{movement_id}/protocol/assess")
    async def assess_protocol(movement_id: str) -> dict:
        """Two-tier Blue Book protocol compliance assessment.

        The decider takes a strategic checkpoint (compliance verdict posture)
        and Gemma performs the item-by-item assessment with tool support.
        """
        context_store: ConvoyContextStore = app.state.context_store
        ctx = await context_store.get(movement_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail="Movement not found")

        orchestrator: Orchestrator = app.state.orchestrator
        session_id = str(uuid.uuid4())

        assessment_prompt = (
            "Perform a Blue Book protocol compliance assessment for movement "
            + movement_id
            + ".\n\nVVIP Class: " + ctx.vvip_class
            + "\nMovement Status: " + ctx.status
            + "\nProtocol State:\n" + json.dumps(ctx.protocol_state, indent=2)
            + "\n\nAnalyze the following:\n"
            "1. ASL checklist completion — identify missing critical items\n"
            "2. 10-Rule protocol compliance — flag any violations\n"
            "3. Anti-sabotage sweep status — assess readiness\n"
            "4. Plan B contingency readiness — evaluate preparedness\n"
            "5. Overall deployment readiness verdict\n\n"
            "Use available tools to check live traffic conditions and corridor "
            "status to enrich your assessment. Return your protocol_assessment "
            "in the standard output format."
        )

        try:
            await orchestrator.create_session(
                session_id=session_id,
                movement_id=movement_id,
                vvip_class=ctx.vvip_class,
            )
            result = await orchestrator.process_turn(session_id, assessment_prompt)

            await context_store.update_protocol_state(movement_id, {
                "last_assessment": {
                    "timestamp": time.time(),
                    "confidence": result.get("confidence", "unknown"),
                    "action": result.get("action", "assessment"),
                },
            })

            return {
                "movement_id": movement_id,
                "assessment": result,
                "protocol_state": ctx.protocol_state,
            }
        except Exception as exc:
            logger.error("protocol.assess.failed", movement_id=movement_id, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            orchestrator.end_session(session_id)

    @app.post("/movements/{movement_id}/dossier")
    async def generate_dossier(movement_id: str, req: DossierRequest) -> dict:
        """Generate a comprehensive security dossier (Gemma tactical tier)."""
        context_store: ConvoyContextStore = app.state.context_store
        ctx = await context_store.get(movement_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail="Movement not found")

        orchestrator: Orchestrator = app.state.orchestrator
        session_id = str(uuid.uuid4())

        sections_instruction = ""
        if req.include_sections:
            sections_instruction = "\nFocus on these sections: " + ", ".join(req.include_sections)

        dossier_prompt = (
            "Generate a comprehensive VVIP Security Dossier for movement "
            + movement_id + ".\n\nVVIP Class: " + req.vvip_class
            + "\nOrigin: " + (req.origin_name or "Not specified")
            + "\nDestination: " + (req.destination_name or "Not specified")
            + "\nMovement Status: " + ctx.status
            + "\nProtocol State:\n" + json.dumps(ctx.protocol_state, indent=2)
            + sections_instruction
            + "\n\nUse available tools to gather:\n"
            "- Live traffic conditions along the corridor\n"
            "- Recent anomalies in the area\n"
            "- Traffic flow predictions for key segments\n"
            "- Corridor summary for overall situational awareness\n\n"
            "Produce a structured security dossier with these sections:\n"
            "1. Movement Overview & Classification\n"
            "2. Route Security Assessment\n"
            "3. Threat Environment Analysis (based on anomalies + traffic)\n"
            "4. ASL Compliance Status\n"
            "5. Anti-Sabotage Readiness\n"
            "6. Diversion & Traffic Management Plan\n"
            "7. Communication Protocols (command hierarchy)\n"
            "8. Plan B Contingency Assessment\n"
            "9. Risk Matrix (segment-level risk scoring)\n"
            "10. Recommendations & Action Items\n\n"
            "Return the dossier as a JSON object with section keys and content values."
        )

        try:
            await orchestrator.create_session(
                session_id=session_id,
                movement_id=movement_id,
                vvip_class=req.vvip_class,
            )
            result = await orchestrator.process_turn(session_id, dossier_prompt)

            await context_store.update_protocol_state(movement_id, {
                "dossier_generated_at": time.time(),
            })

            return {
                "movement_id": movement_id,
                "vvip_class": req.vvip_class,
                "dossier": result,
                "generated_at": time.time(),
            }
        except Exception as exc:
            logger.error("dossier.generate.failed", movement_id=movement_id, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            orchestrator.end_session(session_id)

    @app.post("/movements/{movement_id}/threat-assessment")
    async def threat_assessment(movement_id: str) -> dict:
        """Real-time threat assessment: live tools + Gemma analysis."""
        context_store: ConvoyContextStore = app.state.context_store
        ctx = await context_store.get(movement_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail="Movement not found")

        orchestrator: Orchestrator = app.state.orchestrator
        session_id = str(uuid.uuid4())

        threat_prompt = (
            "Perform a real-time threat assessment for active movement "
            + movement_id + ".\n\nVVIP Class: " + ctx.vvip_class
            + "\nStatus: " + ctx.status
            + "\nConvoy Position: " + str(ctx.convoy_position or "Not deployed")
            + "\nSpeed: " + str(ctx.convoy_speed_kmh) + " km/h"
            + "\nActive Diversions: " + str(len(ctx.active_diversions))
            + "\nCurrent Threat Level: " + str(ctx.protocol_state.get("threat_level", "nominal"))
            + "\n\nUse these tools to gather real-time intelligence:\n"
            "1. get_live_traffic — check current conditions on route segments\n"
            "2. get_historical_pattern — check for deviations from the norm\n"
            "3. predict_traffic_flow — forecast near-term conditions\n\n"
            "Assess and return:\n"
            "- threat_level: nominal | elevated | high | critical\n"
            "- threat_factors: list of identified risks with severity\n"
            "- anomaly_summary: count and types of active anomalies\n"
            "- corridor_status: green | amber | red\n"
            "- protocol_violations: any Blue Book rule violations detected\n"
            "- recommended_actions: immediate actions if threat is elevated\n"
            "- plan_b_trigger: whether Plan B activation is recommended (true/false)"
        )

        try:
            await orchestrator.create_session(
                session_id=session_id,
                movement_id=movement_id,
                vvip_class=ctx.vvip_class,
            )
            result = await orchestrator.process_turn(session_id, threat_prompt)

            assessed_threat = result.get("data", {}).get("threat_level") or result.get("threat_level")
            if assessed_threat in ("nominal", "elevated", "high", "critical"):
                await context_store.update_protocol_state(movement_id, {
                    "threat_level": assessed_threat,
                })

            return {
                "movement_id": movement_id,
                "threat": result,
                "current_protocol_state": ctx.protocol_state,
                "assessed_at": time.time(),
            }
        except Exception as exc:
            logger.error("threat.assess.failed", movement_id=movement_id, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            orchestrator.end_session(session_id)

    # -------------------------------------------------------------------
    # Deep-dive reasoning (Gemma tactical tier)
    # -------------------------------------------------------------------

    @app.post("/analytics/reasoning")
    async def analytics_reasoning(req: AnalyticsReasoningRequest) -> dict:
        """Generate mathematical reasoning behind an analytics metric (Gemma)."""
        bridge: LlamaBridge = app.state.bridge

        system_prompt = (
            "You are an elite traffic analytics mathematician for a VVIP convoy "
            "protection unit. Your task is to produce a rigorous, multi-layered "
            "mathematical reasoning breakdown for a given metric. Use transport "
            "engineering formulas (BPR delay function, Greenshields speed-density, "
            "Webster signal delay, Little's law, queueing theory) where applicable. "
            "Then produce the final structured JSON analysis."
        )

        user_prompt = (
            "METRIC: " + str(req.metric_name)
            + "\nCURRENT VALUE: " + str(req.metric_value)
            + "\nVVIP CLASS: " + req.vvip_class
            + "\nCONTEXT: " + json.dumps(req.metric_context, default=str)
            + "\n\nReturn a JSON object with exactly these fields:\n"
            "{\n"
            '  "metric_name": "...",\n'
            '  "formula": "The primary mathematical formula",\n'
            '  "secondary_formulas": ["Additional relevant formulas used"],\n'
            '  "inputs": [{"name": "...", "value": 0, "unit": "...", "source": "..."}],\n'
            '  "computation_steps": [\n'
            '    {"step": 1, "operation": "...", "result": 0, "explanation": "detailed explanation", "formula_ref": "..."}\n'
            "  ],\n"
            '  "result": {"value": 0, "unit": "...", "precision": "...", "z_score": 0, "percentile": 0},\n'
            '  "sensitivity": [{"factor": "...", "elasticity": 0, "direction": "positive|negative"}],\n'
            '  "interpretation": "What this value means operationally for convoy safety",\n'
            '  "thresholds": {"green": "...", "amber": "...", "red": "...", "critical": "..."},\n'
            '  "trend_analysis": "Rising/falling/stable with quantified rate of change",\n'
            '  "regime": "free_flow | synchronized | forced_flow | gridlock",\n'
            '  "actionable_insight": "Specific tactical action for the convoy commander",\n'
            '  "confidence": "high | medium | low",\n'
            '  "data_quality_note": "Assessment of input data reliability"\n'
            "}"
        )

        try:
            raw = await bridge.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                json_mode=True,
            )
            parsed = json.loads(raw) if raw.strip() else {}
            return {
                "metric_name": req.metric_name,
                "metric_value": req.metric_value,
                "reasoning": {
                    "action": "analytics_reasoning",
                    "reasoning": parsed.get("interpretation", raw[:500]),
                    "confidence": parsed.get("confidence", "medium"),
                    "tool_calls_made": [],
                    "data": parsed,
                },
                "generated_at": time.time(),
            }
        except json.JSONDecodeError:
            return {
                "metric_name": req.metric_name,
                "metric_value": req.metric_value,
                "reasoning": {
                    "action": "analytics_reasoning",
                    "reasoning": raw[:1000] if raw else "Analysis could not be parsed.",
                    "confidence": "low",
                    "tool_calls_made": [],
                    "data": {},
                },
                "generated_at": time.time(),
            }
        except Exception as exc:
            logger.error("analytics.reasoning.failed", metric=req.metric_name, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/recommendations/reasoning")
    async def recommendation_reasoning(req: RecommendationReasoningRequest) -> dict:
        """Generate chain-of-thought reasoning behind a recommendation (Gemma)."""
        bridge: LlamaBridge = app.state.bridge

        system_prompt = (
            "You are the chief VVIP convoy security advisor with expertise in convoy "
            "protection, traffic engineering, and real-time threat assessment. Your task "
            "is to produce a comprehensive, multi-phase chain-of-thought reasoning "
            "breakdown for a given recommendation. Use domain-specific reasoning: threat "
            "classification (Z+/Z/Y/X protocols), BPR congestion modeling, corridor "
            "resilience metrics, and risk quantification. Reason thoroughly "
            "before producing your final structured JSON analysis."
        )

        user_prompt = (
            "RECOMMENDATION: " + req.statement
            + "\nCATEGORY: " + req.category
            + "\nVVIP CLASS: " + req.vvip_class
            + "\nGROUND DATA: " + json.dumps(req.ground_data, default=str)
            + "\n\nProduce a detailed chain-of-thought analysis. Each reasoning step must "
            "contain at least 2-3 sentences with specific values, calculations, or "
            "domain references. Return a JSON object with exactly these fields:\n"
            "{\n"
            '  "statement": "The recommendation being explained",\n'
            '  "chain_of_thought": [\n'
            '    {"step": 1, "phase": "Observation", "reasoning": "Detailed raw data observed with specific values and measurements"},\n'
            '    {"step": 2, "phase": "Hypothesis", "reasoning": "Pattern identified, causal model with engineering basis"},\n'
            '    {"step": 3, "phase": "Evidence", "reasoning": "Supporting data points, statistical backing, and formula references"},\n'
            '    {"step": 4, "phase": "Risk Assessment", "reasoning": "Quantified threat level with probability and impact numbers"},\n'
            '    {"step": 5, "phase": "Decision", "reasoning": "Why this specific action was chosen over alternatives with trade-off analysis"}\n'
            "  ],\n"
            '  "data_sources": ["list of data sources consulted"],\n'
            '  "risk_factors": [{"factor": "...", "severity": "low|medium|high", "mitigation": "...", "probability": 0.0}],\n'
            '  "alternative_actions": [{"action": "...", "pros": "...", "cons": "...", "risk_delta": "..."}],\n'
            '  "impact_assessment": {"speed_delta_pct": 0, "eta_delta_sec": 0, "risk_score": 0, "disruption_radius_m": 0},\n'
            '  "historical_precedent": "Similar situation reference if applicable",\n'
            '  "confidence": "high | medium | low",\n'
            '  "urgency": "immediate | short-term | advisory",\n'
            '  "vvip_protocol_reference": "Relevant Z+/Z/Y/X protocol constraint"\n'
            "}"
        )

        try:
            raw = await bridge.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                json_mode=True,
            )
            parsed = json.loads(raw) if raw.strip() else {}
            return {
                "statement": req.statement,
                "category": req.category,
                "reasoning": {
                    "action": "recommendation_reasoning",
                    "reasoning": parsed.get("statement", raw[:500]),
                    "confidence": parsed.get("confidence", "medium"),
                    "tool_calls_made": [],
                    "data": parsed,
                },
                "generated_at": time.time(),
            }
        except json.JSONDecodeError:
            return {
                "statement": req.statement,
                "category": req.category,
                "reasoning": {
                    "action": "recommendation_reasoning",
                    "reasoning": raw[:1000] if raw else "Reasoning could not be parsed.",
                    "confidence": "low",
                    "tool_calls_made": [],
                    "data": {},
                },
                "generated_at": time.time(),
            }
        except Exception as exc:
            logger.error(
                "recommendation.reasoning.failed",
                statement=req.statement[:50],
                error=str(exc),
            )
            raise HTTPException(status_code=500, detail=str(exc))

    # -------------------------------------------------------------------
    # Chat (free-form orchestrator, two-tier)
    # -------------------------------------------------------------------

    @app.post("/chat")
    async def chat(req: ChatRequest) -> dict:
        orchestrator: Orchestrator = app.state.orchestrator
        state = await orchestrator.open_session(
            session_id=req.session_id,
            movement_id=req.movement_id,
            vvip_class=req.vvip_class,
            event_id=req.event_id,
            context_urn=req.context_urn,
        )
        session_id = state.session_id

        try:
            result = await orchestrator.process_turn(session_id, req.message)
            return {
                "session_id": session_id,
                "response": result,
                # Top-level tool_calls so the adversarial gate (runner.py) scores
                # the real MCP invocations of the turn (contract C9a).
                "tool_calls": result.get("tool_calls", []),
            }
        except Exception as exc:
            logger.error("chat.failed", session_id=session_id, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    # -------------------------------------------------------------------
    # Chat streaming (NDJSON, contract C-D)
    # -------------------------------------------------------------------

    @app.post("/chat/stream")
    async def chat_stream(req: ChatRequest) -> StreamingResponse:
        """Stream one two-tier orchestrator turn as NDJSON (contract C-D).

        The turn is the SAME code path as /chat (Orchestrator.process_turn):
        decider tool selection with the confidence gate, Gemma argument
        drafting with validation, the decisions_log audit. Event lines:

            {"type":"thought","data":{"stepIndex":N,"text":"...","tier":"decider|gemma|heuristic"}}
            {"type":"tool_call","data":{"id","name","args", + legacy callId/toolName/
                                         arguments/state}}
            {"type":"tool_result","data":{"id","name","ok","summary","result"?,
                                           + legacy callId/state/durationMs}}
            {"type":"directive","data":{"id","verb","args","label",
                                        "provenance":{"session_id","tier","confidence",
                                                      "tool_call_id"}}}   (C-D2)
            {"type":"token","data":"..."}          (the answer's prose — never raw JSON)
            {"type":"done","data":{"session_id","tiers_used","operator_alerts","degraded",
                                   "citations":[{"urn","label"}]}}
            {"type":"error","data":"..."}

        ``event_id`` / ``context_urn`` ground the session in the operator's
        selection (C-D2). The turn runs as its own task: a client that
        disconnects mid-turn does not cancel it, so its audit row still lands.
        """
        orchestrator: Orchestrator = app.state.orchestrator
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        async def on_event(kind: str, data: object) -> None:
            await queue.put({"type": kind, "data": data})

        async def run_turn() -> None:
            try:
                state = await orchestrator.open_session(
                    session_id=req.session_id,
                    movement_id=req.movement_id,
                    vvip_class=req.vvip_class,
                    event_id=req.event_id,
                    context_urn=req.context_urn,
                )
                result = await orchestrator.process_turn(
                    state.session_id, req.message, on_event=on_event
                )
                for chunk in chunk_prose(final_prose(result)):
                    await queue.put({"type": "token", "data": chunk})
                await queue.put({
                    "type": "done",
                    "data": {
                        "session_id": state.session_id,
                        "tiers_used": result.get("tiers") or ["heuristic"],
                        "operator_alerts": [
                            alert_text(a) for a in result.get("operator_alerts", [])
                            if isinstance(a, dict)
                        ],
                        "degraded": bool(result.get("degraded")),
                        "citations": result.get("citations") or [],
                    },
                })
            except Exception as exc:
                logger.error("chat_stream.failed", error=str(exc))
                await queue.put({"type": "error", "data": str(exc)})
            finally:
                await queue.put(None)

        task = asyncio.create_task(run_turn())
        app.state.chat_tasks = getattr(app.state, "chat_tasks", set())
        app.state.chat_tasks.add(task)
        task.add_done_callback(app.state.chat_tasks.discard)

        async def generate():  # type: ignore[no-untyped-def]
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield _ndjson(item)

        return StreamingResponse(generate(), media_type="application/x-ndjson")

    @app.post("/chat/directives/ack")
    async def ack_directive(req: DirectiveAck, request: Request) -> dict:
        """The operator applied or dismissed a canvas directive (C-D2).

        The AI only proposes: this records the human's answer — the deck's
        verified officer (else the body's), the decision, the directive and
        its provenance — in the bound movement's decisions_log when the
        session has one, and in the brain log always. 404 unknown or expired
        directive; 409 already acknowledged; 422 another session's directive.
        """
        officer, claimed = _attributed(request, req.officer, required=False)
        orchestrator: Orchestrator = app.state.orchestrator
        try:
            return await orchestrator.ack_directive(
                req.session_id, req.directive_id, req.decision,
                officer=officer, claimed_officer=claimed,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown or expired directive") from exc
        except LookupError as exc:
            return JSONResponse(status_code=409, content={
                "detail": f"directive already {exc.args[0]}", "decision": exc.args[0],
            })
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # -------------------------------------------------------------------
    # MCP over HTTP (JSON-RPC 2.0, contract C-D2)
    # -------------------------------------------------------------------

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> Response:
        """One JSON-RPC 2.0 message to the MCP server (initialize, tools/list,
        tools/call, resources/list|read, prompts/list|get, ping).

        The body is capped at MCP_MAX_BODY_BYTES; malformed input answers a
        JSON-RPC error object (-32700 parse error, -32600 invalid request),
        a notification (no id) answers 204. Batches are not accepted. The
        tools are the read-only city tools and the canvas verbs — nothing
        reachable here approves, decides, dispatches or earmarks (R2).
        """
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MCP_MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content=_rpc_error(
                None, -32600, f"request body over {MCP_MAX_BODY_BYTES} bytes"))
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MCP_MAX_BODY_BYTES:
                return JSONResponse(status_code=413, content=_rpc_error(
                    None, -32600, f"request body over {MCP_MAX_BODY_BYTES} bytes"))
        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse(content=_rpc_error(None, -32700, "Parse error"))
        if not isinstance(message, dict):
            return JSONResponse(content=_rpc_error(
                None, -32600, "Invalid Request: one JSON-RPC object per call (no batches)"))
        msg_id = message.get("id")
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str) \
                or not isinstance(msg_id, (str, int, type(None))) or isinstance(msg_id, bool):
            return JSONResponse(content=_rpc_error(
                msg_id if isinstance(msg_id, (str, int)) and not isinstance(msg_id, bool) else None,
                -32600, "Invalid Request: jsonrpc '2.0', a string method and a string/int id"))
        params = message.get("params", {})
        if params is not None and not isinstance(params, dict):
            return JSONResponse(content=_rpc_error(msg_id, -32602, "Invalid params: an object"))
        mcp_server: MCPServer = app.state.mcp_server
        reply = await mcp_server.handle_message(json.dumps(message))
        if reply is None:
            return Response(status_code=204)
        return Response(content=reply, media_type="application/json")

    # -------------------------------------------------------------------
    # WebSocket — real-time convoy events
    # -------------------------------------------------------------------

    @app.websocket("/ws/convoy/{movement_id}")
    async def convoy_websocket(ws: WebSocket, movement_id: str) -> None:
        """Real-time convoy event stream over WebSocket."""
        await ws_manager.connect(movement_id, ws)

        context_store: ConvoyContextStore = app.state.context_store
        ctx = await context_store.get(movement_id)
        if ctx:
            await ws.send_json({
                "type": "convoy.status",
                "timestamp": int(time.time() * 1000),
                "payload": {
                    "movementId": ctx.movement_id,
                    "status": ctx.status,
                    "vvipClass": ctx.vvip_class,
                    "selectedRouteId": ctx.selected_route_id,
                    "position": list(ctx.convoy_position) if ctx.convoy_position else None,
                    "speedKmh": ctx.convoy_speed_kmh,
                    "activeDiversions": ctx.active_diversions,
                },
            })

        try:
            while True:
                data = await ws.receive_json()

                if data.get("type") == "position.update":
                    payload = data.get("payload") or {}
                    try:
                        fix = _fix_from_request(
                            payload.get("lon"), payload.get("lat"),
                            payload.get("speedKmh"), payload.get("headingDeg"),
                            payload.get("t"),
                            "emulated_gps" if payload.get("source") == "emulated_gps" else "gps",
                        )
                    except HTTPException as exc:
                        await ws.send_json({
                            "type": "error",
                            "timestamp": int(time.time() * 1000),
                            "payload": {"movementId": movement_id, "detail": exc.detail},
                        })
                        continue
                    # Field-scoped write of the FRESH document (the snapshot
                    # read at connect is never written back), C-B mirror and
                    # broadcast — the same path as POST /position.
                    fresh, _applied = await accept_position(movement_id, fix)
                    if fresh is not None and fresh.status in CLOSED_STATUSES:
                        await ws.send_json({
                            "type": "error",
                            "timestamp": int(time.time() * 1000),
                            "payload": {
                                "movementId": movement_id,
                                "detail": f"movement is {fresh.status}; position "
                                          "fixes are no longer accepted",
                            },
                        })

        except WebSocketDisconnect:
            ws_manager.disconnect(movement_id, ws)
        except Exception as exc:
            logger.error("ws.error", movement_id=movement_id, error=str(exc))
            ws_manager.disconnect(movement_id, ws)

    @app.websocket("/ws/event/{event_id}")
    async def event_websocket(ws: WebSocket, event_id: str) -> None:
        """Event-agnostic twin of /ws/convoy/{movement_id} (showcase C6).

        Same manager, same payload shapes — an event id and a movement id
        are both corridor-subject ids here; contexts created through either
        vocabulary are reachable from both endpoints.
        """
        await convoy_websocket(ws, event_id)

    return app


# ---------------------------------------------------------------------------
# Background health loop — drives decider degradation + warmup
# ---------------------------------------------------------------------------


async def _background_health_loop(app: FastAPI) -> None:
    """Poll both tiers periodically; flip the brain's degradation state and
    feed every observation to the oracle's degradation ladder.

    Each poll POSTs ``{role, ok, latency_ms}`` for the decider (latency: the
    host's p95 decide latency, else the health round trip) and for Gemma
    (the /health round trip) to /api/v1/adaptive/health, so
    /api/v1/adaptive/status reflects the brain's real tiers instead of
    FULL with ``last_ok: null``.
    """
    from convoy_brain.metrics import DECIDER_DEGRADED, MODEL_HOST_HEALTH

    first = True
    while True:
        try:
            started = time.monotonic()
            health = await app.state.decider_brain.refresh_health()
            health_ms = (time.monotonic() - started) * 1000
            up = bool(health and health.get("loaded"))
            degraded = app.state.decider_brain.degraded
            MODEL_HOST_HEALTH.set(1 if up else 0)
            DECIDER_DEGRADED.set(1 if degraded else 0)
            p95 = app.state.decider_client.p95_decide_latency_ms
            await _report_role_health(
                app, "decider", ok=up and not degraded,
                latency_ms=p95 if (up and p95 is not None) else health_ms,
            )

            started = time.monotonic()
            llama_ok = await app.state.bridge.check_health()
            LLAMA_HEALTH.set(1 if llama_ok else 0)
            await _report_role_health(
                app, "gemma", ok=llama_ok, latency_ms=(time.monotonic() - started) * 1000,
            )

            if first and up:
                await app.state.decider_client.warmup()
                first = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("health_loop.error", error=str(exc))
        await asyncio.sleep(DECIDER_HEALTH_POLL_SEC)


async def _report_role_health(app: FastAPI, role: str, *, ok: bool, latency_ms: float) -> None:
    """POST one health snapshot to the oracle (best effort; never raises)."""
    try:
        resp = await app.state.health_client.post(
            f"{app.state.oracle_url}/api/v1/adaptive/health",
            json={"role": role, "ok": bool(ok), "latency_ms": round(max(0.0, latency_ms), 1)},
        )
        resp.raise_for_status()
        ADAPTIVE_HEALTH_REPORTS.labels(role=role, outcome="ok").inc()
    except Exception as exc:
        ADAPTIVE_HEALTH_REPORTS.labels(role=role, outcome="error").inc()
        logger.info("health_loop.report_failed", role=role, error=str(exc)[:200])


def _model_host_state(decider_health: dict | None) -> str:
    """connected | loading | load_error | unreachable for /health."""
    if not decider_health:
        return "unreachable"
    if decider_health.get("loaded"):
        return "connected"
    if decider_health.get("load_error"):
        return "load_error"
    return "loading"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rpc_error(msg_id: object, code: int, message: str) -> dict:
    """A JSON-RPC 2.0 error object."""
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _ndjson(obj: dict) -> str:
    """Serialize a dict as a single compact NDJSON line (``{"type":"token",...}``
    — no separator whitespace, so line-level greps match the wire form)."""
    return json.dumps(obj, default=str, separators=(",", ":")) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
    )

    logger.info("convoy_brain.starting", host=host, port=port)

    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
