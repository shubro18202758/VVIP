"""models-owned launcher for the embedding-service app (TEMPORARY adapter).

Why this exists: the embeddings stream's shipped entrypoint
(``uvicorn.run("embedding_service.service:create_app_from_env",
factory=True)``) is broken on uvicorn 0.54 — the factory is an ``async def``
and uvicorn never awaits it, so every request hits a raw coroutine
("TypeError: 'coroutine' object is not callable" → HTTP 500). Verified twice
this session: via their ``python -m embedding_service`` AND by awaiting the
factory manually (that variant then broke differently: ``asyncio.run`` closed
the loop the asyncpg pool was bound to → "Event loop is closed").

This adapter builds the exact same app (``create_app``) but initializes the
pool and both encoders INSIDE the serving loop via a FastAPI startup event —
which is what their factory intended. It performs no outbound HTTP calls.

RETIRE ME: delete this file and point serve_models.sh back at
``python -m embedding_service`` once the embeddings stream fixes their
entrypoint (either a sync factory that wires an async startup, or an awaited
factory).

Tracked home: VVIP/models-config/ (one level below the repo root, like the
VVIP/models/ dir it came from, so the ``services/embedding-service/src``
path below resolves the same way).
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "services", "embedding-service", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from embedding_service.db import create_pool  # noqa: E402
from embedding_service.encoders import BgeM3DenseEncoder, PplxLateInteractionEncoder  # noqa: E402
from embedding_service.service import build_api, create_app  # noqa: E402
from embedding_service.store import MemoryStore  # noqa: E402


class _LazyApi:
    """Delegates to the real EmbeddingApi once the startup event built it."""

    def __init__(self) -> None:
        self._real = None

    def __getattr__(self, name: str):
        if self._real is None:
            raise RuntimeError("embedding api not initialized yet")
        return getattr(self._real, name)


def build_lazy_app():
    lazy = _LazyApi()
    app = create_app(lazy)

    @app.on_event("startup")
    async def _init_on_serving_loop() -> None:
        pool = await create_pool()
        lazy._real = build_api(
            MemoryStore(pool),
            PplxLateInteractionEncoder(),
            BgeM3DenseEncoder(),
        )

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        build_lazy_app(),
        host=os.environ.get("EMBEDDING_LISTEN_HOST", "127.0.0.1"),
        port=int(os.environ.get("EMBEDDING_LISTEN_PORT", "8085")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
