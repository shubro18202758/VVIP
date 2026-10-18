"""FastAPI wiring for the C3 endpoints.

POST /embed/documents   — encode + upsert memory docs (late + dense)
POST /search            — hybrid: dense ANN recall_k=100 → MaxSim top-20
POST /incidents         — record a live incident OR a simulation scenario run
GET  /incidents/recent  — recent incident/pattern memory
GET  /health            — service heartbeat

FastAPI/pydantic are imported lazily; the handlers themselves live in
``api.py`` and are transport-neutral.
"""

from __future__ import annotations

from typing import Any

from .api import ApiError, EmbeddingApi
from .db import create_pool
from .encoders import BgeM3DenseEncoder, PplxLateInteractionEncoder
from .incidents import IncidentIngestor
from .retrieval import HybridRetriever
from .store import MemoryStore


def build_api(
    store: MemoryStore,
    late_encoder: Any,
    dense_encoder: Any,
) -> EmbeddingApi:
    """Assemble handlers over injected store + encoders (tests use fakes)."""
    retriever = HybridRetriever(store, late_encoder, dense_encoder)
    ingestor = IncidentIngestor(store, late_encoder, dense_encoder)
    return EmbeddingApi(store=store, retriever=retriever, ingestor=ingestor)


def create_app(api: EmbeddingApi | None = None):
    """Create the FastAPI application (lazy import keeps core testable).

    Pass ``api`` (tests inject fakes); the production factory is
    ``create_app_from_env`` below.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    if api is None:
        raise RuntimeError(
            "create_app() requires an EmbeddingApi; use create_app_from_env()"
        )

    app = FastAPI(title="veer-embedding-service", version="1.0.0")

    @app.get("/health")
    async def health() -> dict:
        return await api.health()

    @app.post("/embed/documents")
    async def embed_documents(payload: dict) -> dict:
        return await _guard(api.embed_documents(payload))

    @app.post("/search")
    async def search(payload: dict) -> dict:
        return await _guard(api.search(payload))

    @app.post("/incidents")
    async def record_incident(payload: dict) -> dict:
        return await _guard(api.record_incident(payload))

    @app.get("/incidents/recent")
    async def recent_incidents(limit: str = "20", kind: str | None = None) -> dict:
        # NOTE(core fix): this handler previously took ``request: Request``.
        # With ``from __future__ import annotations`` the annotation is a
        # string FastAPI resolves against module globals — and Request was
        # imported INSIDE create_app's closure, so resolution failed and
        # FastAPI fell back to treating it as a required query param
        # (live 422: "field required: query.request").  Explicit query
        # parameters keep the contract typed and the closure bug
        # structurally impossible; ``dict(request.query_params)`` semantics
        # are preserved (unknown params were ignored by the API layer).
        params: dict[str, Any] = {"limit": limit}
        if kind is not None:
            params["kind"] = kind
        return await _guard(api.recent_incidents(params))

    async def _guard(coro):
        try:
            return await coro
        except ApiError as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    return app


def main() -> None:
    """Run the service with uvicorn (models venv / serve_models.sh)."""
    import uvicorn

    from . import config

    uvicorn.run(
        "embedding_service.service:create_app_from_env",
        factory=True,
        host=config.LISTEN_HOST,
        port=config.LISTEN_PORT,
    )


async def create_app_from_env():
    """Async uvicorn factory: real pool + real (lazily loaded) encoders."""
    pool = await create_pool()
    api = build_api(
        MemoryStore(pool),
        PplxLateInteractionEncoder(),
        BgeM3DenseEncoder(),
    )
    return create_app(api)


if __name__ == "__main__":
    main()
