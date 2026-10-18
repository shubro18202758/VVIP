"""Convoy Brain entry point — ``python -m convoy_brain``.

Starts an HTTP server (FastAPI / Uvicorn) on PORT (default 8080 — the port
the frozen nginx.conf, the adversarial suite, and TECHNICAL_DOCS §1 agree on):

    - POST /movements                           → create new movement
    - POST /movements/{movement_id}/plan         → pre-movement workflow
    - POST /movements/{movement_id}/escort       → live escort workflow
    - POST /movements/{movement_id}/clear        → post-clearance workflow
    - POST /chat                                 → free-form orchestrator chat
    - POST /chat/stream                          → NDJSON streaming chat
    - WS   /ws/convoy/{movement_id}              → real-time convoy events
    - GET  /health                               → readiness probe
    - GET  /health/gpu                           → unified-memory/RSS status
    - GET  /health/services                      → all services health
    - GET  /metrics                              → Prometheus exposition

Two-tier wiring: the decider (model-host :8083) is the strategic brain and
llama-server (:8084) serves Gemma 4 12B as the tactical tier; both come from
config.load_service_urls() (env > veer_models.yaml > compile-time defaults)
and are validated as platform service URLs (http/https on loopback hosts).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid

import httpx
import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from convoy_brain.adaptive_params import AdaptiveParamsClient
from convoy_brain.config import DECIDER_HEALTH_POLL_SEC, load_service_urls, tier0_enabled
from convoy_brain.decider import DeciderBrain
from convoy_brain.llm.decider_client import DeciderClient
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.mcp.server import MCPServer, ToolExecutor
from convoy_brain.memory.convoy_context import ConvoyContext, ConvoyContextStore
from convoy_brain.memory.memory_client import MemoryClient
from convoy_brain.metrics import NOVELTY_LATCH_DISABLED, TIER0_ENABLED
from convoy_brain.orchestrator import Orchestrator
from convoy_brain.workflows.live_escort import run_live_escort_workflow
from convoy_brain.workflows.post_clearance import run_post_clearance_workflow
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


class PlanRequest(BaseModel):
    origin: tuple[float, float] = Field(..., description="(lon, lat) origin")
    destination: tuple[float, float] = Field(..., description="(lon, lat) destination")
    vvip_class: str = Field("Z", description="VVIP class: Z+, Z, Y, or X")
    planned_departure: str = Field(..., description="ISO 8601 departure datetime")


class EscortRequest(BaseModel):
    destination: tuple[float, float] | None = Field(
        None, description="(lon, lat) destination for emergency re-routing"
    )


class ChatRequest(BaseModel):
    message: str
    movement_id: str | None = None
    vvip_class: str | None = None


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


def create_app() -> FastAPI:
    """Build the FastAPI application with lifespan-managed infrastructure."""
    app = FastAPI(
        title="Convoy Brain",
        version="0.2.0",
        description="Two-tier agentic orchestration for VVIP convoy planning",
    )

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
        app.state.executor = executor
        app.state.context_store = context_store
        app.state.mcp_server = mcp_server
        app.state.orchestrator = orchestrator
        app.state.ws_manager = ws_manager

        # Warm the decider in the background so the first checkpoint is fast.
        asyncio.create_task(_background_health_loop(app))

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
        await app.state.executor.close()
        await app.state.decider_client.aclose()
        await app.state.bridge.aclose()
        await app.state.adaptive_client.aclose()
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
        decider_ok = bool(decider_health and decider_health.get("loaded"))
        degraded = app.state.decider_brain.degraded
        return {
            "status": "ok" if (decider_ok and llama_ok) else "degraded",
            "strategic_tier": {
                "model": "pplx-decider-v1-27b",
                "model_host": "connected" if decider_ok else "unreachable",
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
        )
        await context_store.put(ctx)

        logger.info(
            "movement.created",
            movement_id=movement_id,
            vvip_class=req.vvip_class,
            event_kind=req.event_kind,
            crowd_class=req.crowd_class,
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

        return {"movement_id": movement_id}

    # -------------------------------------------------------------------
    # Workflow endpoints
    # -------------------------------------------------------------------

    @app.post("/movements/{movement_id}/plan")
    async def plan_movement(movement_id: str, req: PlanRequest) -> dict:
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
            )

            await ws_manager.broadcast(movement_id, {
                "type": "convoy.status",
                "timestamp": int(time.time() * 1000),
                "payload": {
                    "movementId": movement_id,
                    "status": "approved" if result.get("status") == "approved" else "planning",
                },
            })

            return result
        except Exception as exc:
            logger.error("plan_movement.failed", movement_id=movement_id, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/movements/{movement_id}/escort")
    async def start_escort(movement_id: str, req: EscortRequest) -> dict:
        try:
            await ws_manager.broadcast(movement_id, {
                "type": "convoy.status",
                "timestamp": int(time.time() * 1000),
                "payload": {"movementId": movement_id, "status": "active"},
            })

            result = await run_live_escort_workflow(
                movement_id=movement_id,
                destination=req.destination,
                bridge=app.state.bridge,
                executor=app.state.executor,
                context_store=app.state.context_store,
                decider_brain=app.state.decider_brain,
                adaptive_client=app.state.adaptive_client,
            )
            return result
        except Exception as exc:
            logger.error("start_escort.failed", movement_id=movement_id, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/movements/{movement_id}/clear")
    async def clear_movement(movement_id: str) -> dict:
        try:
            result = await run_post_clearance_workflow(
                movement_id=movement_id,
                bridge=app.state.bridge,
                executor=app.state.executor,
                context_store=app.state.context_store,
                decider_brain=app.state.decider_brain,
            )

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
        session_id = str(uuid.uuid4())

        await orchestrator.create_session(
            session_id=session_id,
            movement_id=req.movement_id,
            vvip_class=req.vvip_class,
        )

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
        finally:
            orchestrator.end_session(session_id)

    # -------------------------------------------------------------------
    # Chat streaming (NDJSON)
    # -------------------------------------------------------------------

    @app.post("/chat/stream")
    async def chat_stream(req: ChatRequest) -> StreamingResponse:
        """Stream chat response as NDJSON events.

        Event types:
            {"type":"token","data":"..."}
            {"type":"thought","data":{"stepIndex":N,"text":"..."}}
            {"type":"tool_call","data":{...}}
            {"type":"tool_result","data":{...}}
            {"type":"done"}
            {"type":"error","data":"..."}
        """

        async def generate():  # type: ignore[no-untyped-def]
            orchestrator: Orchestrator = app.state.orchestrator
            session_id = str(uuid.uuid4())

            try:
                state = await orchestrator.create_session(
                    session_id=session_id,
                    movement_id=req.movement_id,
                    vvip_class=req.vvip_class,
                )
                state.add_message("user", req.message)

                tool_round = 0
                turn_start = time.time()
                step_index = 0

                while tool_round < 6:
                    elapsed = time.time() - turn_start
                    if elapsed > 120.0:
                        yield _ndjson({"type": "error", "data": "Turn timeout exceeded"})
                        break

                    step_index += 1
                    yield _ndjson({
                        "type": "thought",
                        "data": {
                            "stepIndex": step_index,
                            "text": "Reasoning (round " + str(tool_round + 1) + ")...",
                        },
                    })

                    # Tactical call with history (None -> tier down)
                    llm_response = await orchestrator._safe_generate(
                        state,
                        "Continue the conversation: either call a tool or produce "
                        "the final answer JSON.",
                    )
                    if llm_response is None:
                        yield _ndjson({"type": "error", "data": "LLM returned empty"})
                        break

                    state.add_message("assistant", llm_response)
                    parsed = _parse_tool_calls(llm_response)

                    if parsed is None:
                        for chunk in _chunk_text(llm_response, 20):
                            yield _ndjson({"type": "token", "data": chunk})
                            await asyncio.sleep(0.01)
                        yield _ndjson({"type": "done"})
                        break

                    tool_calls = parsed
                    if not tool_calls:
                        text = llm_response
                        for chunk in _chunk_text(text, 20):
                            yield _ndjson({"type": "token", "data": chunk})
                            await asyncio.sleep(0.01)
                        yield _ndjson({"type": "done"})
                        break

                    tool_round += 1
                    for tc in tool_calls:
                        call_id = "tc_" + str(tool_round) + "_" + str(tc.get("name", "unknown"))
                        yield _ndjson({
                            "type": "tool_call",
                            "data": {
                                "callId": call_id,
                                "toolName": tc.get("name", "unknown"),
                                "arguments": tc.get("arguments", {}),
                                "state": "running",
                            },
                        })

                        start_tool = time.monotonic()
                        try:
                            result = await asyncio.wait_for(
                                app.state.executor.dispatch(
                                    tc.get("name", ""), tc.get("arguments", {})
                                ),
                                timeout=30.0,
                            )
                            duration_ms = int((time.monotonic() - start_tool) * 1000)
                            yield _ndjson({
                                "type": "tool_result",
                                "data": {
                                    "callId": call_id,
                                    "state": "success",
                                    "result": result,
                                    "durationMs": duration_ms,
                                },
                            })

                            state.add_message(
                                "tool",
                                json.dumps(result, default=str)[:4096],
                                tool_name=tc.get("name"),
                                tool_call_id=call_id,
                            )
                        except Exception as exc:
                            duration_ms = int((time.monotonic() - start_tool) * 1000)
                            yield _ndjson({
                                "type": "tool_result",
                                "data": {
                                    "callId": call_id,
                                    "state": "error",
                                    "result": {"error": str(exc)},
                                    "durationMs": duration_ms,
                                },
                            })
                            state.add_message(
                                "tool",
                                json.dumps({"error": str(exc)}),
                                tool_name=tc.get("name"),
                                tool_call_id=call_id,
                            )

                else:
                    yield _ndjson({"type": "error", "data": "Max tool rounds exceeded"})

            except Exception as exc:
                logger.error("chat_stream.failed", error=str(exc))
                yield _ndjson({"type": "error", "data": str(exc)})
            finally:
                orchestrator.end_session(session_id)

        return StreamingResponse(generate(), media_type="application/x-ndjson")

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
                    payload = data.get("payload", {})
                    lon = payload.get("lon", 0)
                    lat = payload.get("lat", 0)
                    speed = payload.get("speedKmh", 0)
                    heading = payload.get("headingDeg", 0)

                    if ctx:
                        ctx.convoy_position = (lon, lat)
                        ctx.convoy_speed_kmh = speed
                        await context_store.put(ctx)

                    await ws_manager.broadcast(movement_id, {
                        "type": "convoy.position",
                        "timestamp": int(time.time() * 1000),
                        "payload": {
                            "movementId": movement_id,
                            "position": [lon, lat],
                            "speedKmh": speed,
                            "headingDeg": heading,
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
    """Poll /decider/health periodically; flip the brain's degradation state."""
    from convoy_brain.metrics import DECIDER_DEGRADED, MODEL_HOST_HEALTH

    first = True
    while True:
        try:
            health = await app.state.decider_brain.refresh_health()
            up = bool(health and health.get("loaded"))
            MODEL_HOST_HEALTH.set(1 if up else 0)
            DECIDER_DEGRADED.set(1 if app.state.decider_brain.degraded else 0)
            if first and up:
                await app.state.decider_client.warmup()
                first = False
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("health_loop.error", error=str(exc))
        await asyncio.sleep(DECIDER_HEALTH_POLL_SEC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ndjson(obj: dict) -> str:
    """Serialize a dict as a single NDJSON line."""
    return json.dumps(obj, default=str) + "\n"


def _chunk_text(text: str, size: int) -> list[str]:
    """Split text into chunks of approximately `size` characters."""
    return [text[i : i + size] for i in range(0, len(text), size)]


def _parse_tool_calls(raw: str) -> list[dict] | None:
    """Extract a tool_calls list from raw model output (None = final answer)."""
    from convoy_brain.llm.parsing import extract_json

    parsed = extract_json(raw)
    if isinstance(parsed, dict):
        calls = parsed.get("tool_calls")
        if isinstance(calls, list):
            return [c for c in calls if isinstance(c, dict)]
    return None


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
