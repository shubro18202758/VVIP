"""model-host HTTP API (C1 contract) for pplx-decider-v1-27b.

Endpoints:
  GET  /decider/health → {loaded, resident_rss_gib, p95_decide_latency_ms, queue_depth, ...}
  POST /warmup         → wait for load, run a warm decision (idempotent)
  POST /decide         → {request_id?, state, questions:{name:{type,...}}} ≤6 questions
                        → {request_id?, answers:{name:{value,probability,confidence,
                           distribution?}}, state_tokens, latency_ms}
  GET  /metrics        → Prometheus text exposition

Behavior:
  - /decide is serialized (one forward pass at a time); when ≥ queue_depth
    requests are running+waiting → 429 with Retry-After: 5.
  - This service makes NO outbound HTTP calls.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from .config import get_settings
from .decider_runtime import ModelNotReady, QueueFull, Runtime

settings = get_settings()
runtime = Runtime(settings)

app = FastAPI(title="vvip-model-host", version="1.0.0")


class DecideRequest(BaseModel):
    request_id: str | None = None
    state: str = Field(min_length=1)
    questions: dict[str, dict[str, Any]] = Field(min_length=1)


@app.on_event("startup")
async def _startup() -> None:
    runtime.start_load()


@app.get("/decider/health")
async def decider_health() -> dict[str, Any]:
    return runtime.health()


@app.get("/health")
async def health() -> dict[str, Any]:
    """Generic liveness (used by serve_models.sh / smoke_models.sh)."""
    h = runtime.health()
    return {
        "ok": h["loaded"] and h["load_error"] is None,
        "service": "model-host",
        "decider": h,
    }


@app.post("/warmup")
async def warmup() -> dict[str, Any]:
    try:
        result = await runtime.warmup()
    except ModelNotReady as exc:
        return JSONResponse(status_code=503, content={"error": str(exc)})
    return {"status": "warm", "warm_decision": result}


@app.post("/decide")
async def decide(req: DecideRequest) -> Response:
    if len(req.questions) > settings.max_questions:
        return JSONResponse(
            status_code=422,
            content={
                "error": f"at most {settings.max_questions} questions per request"
            },
        )
    try:
        result = await runtime.decide(req.state, req.questions)
    except QueueFull:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "5"},
            content={"error": "decider queue full, retry shortly"},
        )
    except ModelNotReady as exc:
        return JSONResponse(status_code=503, content={"error": str(exc)})
    except ValueError as exc:
        msg = str(exc)
        status = 413 if "token" in msg else 422
        return JSONResponse(status_code=status, content={"error": msg})
    body: dict[str, Any] = dict(result)
    if req.request_id is not None:
        body["request_id"] = req.request_id
    return JSONResponse(content=body)


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    h = runtime.health()
    lines = [
        "# HELP model_host_loaded 1 when the decider weights are resident",
        "# TYPE model_host_loaded gauge",
        f"model_host_loaded {1 if h['loaded'] else 0}",
        "# HELP model_host_resident_rss_gib process RSS in GiB",
        "# TYPE model_host_resident_rss_gib gauge",
        f"model_host_resident_rss_gib {h['resident_rss_gib']}",
        "# HELP model_host_queue_depth running+waiting decide requests",
        "# TYPE model_host_queue_depth gauge",
        f"model_host_queue_depth {h['queue_depth']}",
        "# HELP model_host_decides_total total decide forwards",
        "# TYPE model_host_decides_total counter",
        f"model_host_decides_total {h['decides_total']}",
        "# HELP model_host_decides_failed_total failed decide forwards",
        "# TYPE model_host_decides_failed_total counter",
        f"model_host_decides_failed_total {h['decides_failed']}",
    ]
    p95 = h.get("p95_decide_latency_ms")
    if p95 is not None:
        lines += [
            "# HELP model_host_decide_latency_p95_ms p95 decide latency",
            "# TYPE model_host_decide_latency_p95_ms gauge",
            f"model_host_decide_latency_p95_ms {p95}",
        ]
    return PlainTextResponse("\n".join(lines) + "\n")


def main() -> None:
    """Run under uvicorn. Bind defaults to 127.0.0.1:8083 (fixed internal
    endpoint of the platform; not user-supplied)."""
    import uvicorn

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=os.environ.get("MODEL_HOST_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
