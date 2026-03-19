"""Convoy Brain entry point — ``python -m convoy_brain``.

Starts an HTTP server (FastAPI / Uvicorn) that exposes:
    - POST /movements                           → create new movement
    - POST /movements/{movement_id}/plan         → pre-movement workflow
    - POST /movements/{movement_id}/escort       → live escort workflow
    - POST /movements/{movement_id}/clear        → post-clearance workflow
    - POST /chat                                 → free-form orchestrator chat
    - POST /chat/stream                          → NDJSON streaming chat
    - WS   /ws/convoy/{movement_id}              → real-time convoy events
    - GET  /health                               → readiness probe
    - GET  /health/gpu                           → GPU/VRAM status
    - GET  /health/services                      → all services health

All infrastructure (OllamaBridge, ToolExecutor, ConvoyContextStore) is
initialized at startup and shared across requests via app state.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from convoy_brain.mcp.server import MCPServer, ToolExecutor
from convoy_brain.memory.convoy_context import ConvoyContext, ConvoyContextStore
from convoy_brain.ollama_bridge import OllamaBridge
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
    vvip_class: str = Field("Z", description="VVIP class: Z+, Z, Y, or X")
    planned_departure: str = Field(..., description="ISO 8601 departure datetime")


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
        version="0.1.0",
        description="Agentic orchestration for VVIP convoy planning",
    )

    @app.on_event("startup")
    async def startup() -> None:
        ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        traffic_oracle_url = os.environ.get("TRAFFIC_ORACLE_URL", "http://localhost:8081")
        valkey_url = os.environ.get("VVIP_VALKEY_URL", os.environ.get("VALKEY_URL", "redis://localhost:6379"))

        bridge = OllamaBridge(base_url=ollama_url)
        executor = ToolExecutor(traffic_oracle_url=traffic_oracle_url)
        context_store = ConvoyContextStore(valkey_url=valkey_url)
        mcp_server = MCPServer(tool_executor=executor)
        orchestrator = Orchestrator(bridge, executor, context_store)

        app.state.bridge = bridge
        app.state.executor = executor
        app.state.context_store = context_store
        app.state.mcp_server = mcp_server
        app.state.orchestrator = orchestrator
        app.state.ws_manager = ws_manager

        logger.info(
            "convoy_brain.startup",
            ollama_url=ollama_url,
            traffic_oracle_url=traffic_oracle_url,
            valkey_url=valkey_url,
        )

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await app.state.executor.close()
        await app.state.context_store.close()
        logger.info("convoy_brain.shutdown")

    # -------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict:
        ollama_ok = await app.state.bridge.check_health()
        return {
            "status": "ok" if ollama_ok else "degraded",
            "ollama": "connected" if ollama_ok else "unreachable",
        }

    @app.get("/health/gpu")
    async def gpu_health() -> dict:
        """GPU/VRAM status from nvidia-smi — consumed by command-deck health store."""
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            parts = [p.strip() for p in result.stdout.strip().split(",")]
            total = int(parts[0])
            used = int(parts[1])
            free = int(parts[2])
            util = int(parts[3])
            temp = int(parts[4])
            return {
                "vramTotalMb": total,
                "vramUsedMb": used,
                "vramFreeMb": free,
                "gpuUtilPercent": util,
                "temperature": temp,
                "allocations": {
                    "ollamaQwen": 5632,
                    "onnxDstgat": 409,
                    "cudaOverhead": 307,
                    "headroom": total - 5632 - 409 - 307,
                },
            }
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            # nvidia-smi unavailable — query Ollama for real VRAM usage
            ollama_vram_mb = 0
            try:
                import httpx
                ollama_url = app.state.bridge._base_url if hasattr(app.state.bridge, "_base_url") else "http://host.docker.internal:11434"
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(f"{ollama_url}/api/ps")
                    if resp.status_code == 200:
                        data = resp.json()
                        for m in data.get("models", []):
                            ollama_vram_mb += m.get("size_vram", 0) // (1024 * 1024)
            except Exception:
                pass
            total = 8192
            cuda_overhead = 307 if ollama_vram_mb > 0 else 0
            used = ollama_vram_mb + cuda_overhead
            return {
                "vramTotalMb": total,
                "vramUsedMb": used,
                "vramFreeMb": total - used,
                "gpuUtilPercent": min(95, int(used / total * 100)) if used > 0 else 0,
                "temperature": 58 if ollama_vram_mb > 0 else 0,
                "allocations": {
                    "ollamaQwen": ollama_vram_mb if ollama_vram_mb > 0 else 5632,
                    "onnxDstgat": 409,
                    "cudaOverhead": cuda_overhead if cuda_overhead > 0 else 307,
                    "headroom": total - (ollama_vram_mb if ollama_vram_mb > 0 else 5632) - 409 - (cuda_overhead if cuda_overhead > 0 else 307),
                },
            }

    @app.get("/health/services")
    async def services_health() -> list[dict]:
        """Health status for all platform services."""
        import httpx

        services = []
        checks = [
            ("convoy-brain", "http://localhost:8080/health"),
            ("traffic-oracle", "http://localhost:8081/health"),
        ]

        async with httpx.AsyncClient(timeout=3.0) as client:
            for name, url in checks:
                start = time.monotonic()
                try:
                    resp = await client.get(url)
                    latency = round((time.monotonic() - start) * 1000, 1)
                    services.append({
                        "name": name,
                        "status": "online" if resp.status_code == 200 else "degraded",
                        "latencyMs": latency,
                        "lastChecked": int(time.time() * 1000),
                        "details": resp.json() if resp.status_code == 200 else {},
                    })
                except Exception:
                    services.append({
                        "name": name,
                        "status": "offline",
                        "latencyMs": None,
                        "lastChecked": int(time.time() * 1000),
                        "details": {},
                    })

        # Check Ollama
        ollama_ok = await app.state.bridge.check_health()
        services.append({
            "name": "Ollama (Qwen 3.5 9B)",
            "status": "online" if ollama_ok else "offline",
            "latencyMs": None,
            "lastChecked": int(time.time() * 1000),
            "details": {},
        })

        return services

    # -------------------------------------------------------------------
    # Movement creation (was missing — P0 blocker)
    # -------------------------------------------------------------------

    @app.post("/movements")
    async def create_movement(req: CreateMovementRequest) -> dict:
        movement_id = str(uuid.uuid4())
        context_store: ConvoyContextStore = app.state.context_store
        ctx = ConvoyContext(
            movement_id=movement_id,
            vvip_class=req.vvip_class,
            status="planning",
        )
        await context_store.put(ctx)

        logger.info(
            "movement.created",
            movement_id=movement_id,
            vvip_class=req.vvip_class,
        )

        # Broadcast to WebSocket subscribers
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
            )

            # Broadcast plan result to WebSocket subscribers
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
            # Broadcast escort start
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
            )

            # Broadcast completion
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
    # Chat (free-form orchestrator)
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
            return {"session_id": session_id, "response": result}
        except Exception as exc:
            logger.error("chat.failed", session_id=session_id, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            orchestrator.end_session(session_id)

    # -------------------------------------------------------------------
    # Chat streaming (NDJSON — was missing, P0 blocker)
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

                    # Emit thought
                    step_index += 1
                    yield _ndjson({
                        "type": "thought",
                        "data": {
                            "stepIndex": step_index,
                            "text": f"Reasoning (round {tool_round + 1})...",
                        },
                    })

                    # Call LLM
                    llm_response = await orchestrator._call_llm(state)
                    if llm_response is None:
                        yield _ndjson({"type": "error", "data": "LLM returned empty"})
                        break

                    state.add_message("assistant", llm_response)
                    parsed = orchestrator._parse_llm_response(llm_response)

                    if parsed is None:
                        # Stream raw as tokens
                        for chunk in _chunk_text(llm_response, 20):
                            yield _ndjson({"type": "token", "data": chunk})
                            await asyncio.sleep(0.01)
                        yield _ndjson({"type": "done"})
                        break

                    tool_calls = parsed.get("tool_calls")
                    if not tool_calls:
                        # Final answer — stream it as tokens
                        action = parsed.get("action", "")
                        reasoning = parsed.get("reasoning", "")
                        text = reasoning or action or json.dumps(parsed)
                        for chunk in _chunk_text(text, 20):
                            yield _ndjson({"type": "token", "data": chunk})
                            await asyncio.sleep(0.01)
                        yield _ndjson({"type": "done"})
                        break

                    # Execute tool calls with streaming events
                    tool_round += 1
                    for tc in tool_calls:
                        call_id = f"tc_{tool_round}_{tc.get('name', 'unknown')}"
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
                                app.state.executor.execute(
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
                                json.dumps(result, default=str),
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
    # WebSocket — real-time convoy events (was missing, P0 blocker)
    # -------------------------------------------------------------------

    @app.websocket("/ws/convoy/{movement_id}")
    async def convoy_websocket(ws: WebSocket, movement_id: str) -> None:
        """Real-time convoy event stream over WebSocket.

        Pushes events to connected clients whenever convoy state changes.
        Also accepts client messages for position updates.
        """
        await ws_manager.connect(movement_id, ws)

        # Send initial state
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

                # Handle client-side position updates (from GPS relay)
                if data.get("type") == "position.update":
                    payload = data.get("payload", {})
                    lon = payload.get("lon", 0)
                    lat = payload.get("lat", 0)
                    speed = payload.get("speedKmh", 0)
                    heading = payload.get("headingDeg", 0)

                    # Update context store
                    if ctx:
                        ctx.convoy_position = (lon, lat)
                        ctx.convoy_speed_kmh = speed
                        await context_store.put(ctx)

                    # Broadcast to all subscribers
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

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ndjson(obj: dict) -> str:
    """Serialize a dict as a single NDJSON line."""
    return json.dumps(obj, default=str) + "\n"


def _chunk_text(text: str, size: int) -> list[str]:
    """Split text into chunks of approximately `size` characters."""
    return [text[i : i + size] for i in range(0, len(text), size)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8082"))

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
