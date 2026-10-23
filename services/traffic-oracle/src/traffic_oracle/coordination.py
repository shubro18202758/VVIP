"""C0 coordination — the ONE shared registration site for stream routers.

Contract C0: exactly two shared files carry registrations
(``traffic_oracle/__main__.py`` and ``convoy_brain/agents/__init__.py``).
This module holds the oracle half so the __main__.py edit stays two lines.

Registration rules:
    * a stream's real router mounts when its module imports (each stream
      owns exactly its own module files);
    * when a module is absent (stream not landed), a STUB mounts in its
      place answering 503 {"detail": "not deployed"} on its documented
      paths — the tree always boots, parallel streams never collide;
    * a stub is skipped entirely when a real route with the same path
      already exists (an owning stream that registers directly never gets
      shadowed).

Routes registered here (contract C1):
    /api/v1/vehicles...          watchlist-anpr (real, this stream)
    /api/v1/enforcement...       watchlist-anpr (real, this stream)
    /api/v1/events/.../interception-plan  watchlist-anpr (real, this stream)
    /api/v1/signals/state        signalling stream (stub until landed)
    /api/v1/units                signalling/canvas stream (stub until landed)
    /api/v1/adaptive/situation_log  adaptive stream (stub until landed)

City fusion spine (contract C-C, WP-02 — real router, 503 stub only if
its package fails to import):
    /api/v1/cameras              camera layer (locations + read counters)
    /api/v1/city/entities        fused snapshot of the city
    /api/v1/city/entity/{kind}/{id}  dossier
    /api/v1/city/feed            city narrative
    /api/v1/events/{id}/audit    the narrative scoped to one event
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)

__all__ = ["CITY_PATHS", "register_coordination_routers"]

_STUB_DETAIL = "not deployed"

#: Every documented city fusion path (C-C): if the city package fails to
#: import, ALL of them answer 503 not-deployed — none silently 404s.
CITY_PATHS: tuple[str, ...] = (
    "/api/v1/cameras",
    "/api/v1/city/entities",
    "/api/v1/city/entity/{kind}/{entity_id}",
    "/api/v1/city/feed",
    "/api/v1/events/{event_id}/audit",
)


def _stub_router(paths: tuple[str, ...]) -> APIRouter:
    """A router answering 503 not-deployed on every documented path."""
    r = APIRouter()

    for path in paths:

        @r.get(path, include_in_schema=False)
        async def _not_deployed() -> JSONResponse:
            return JSONResponse(status_code=503, content={"detail": _STUB_DETAIL})

    return r


def _route_exists(app: FastAPI, path: str) -> bool:
    """True when ``path`` is already registered.

    FastAPI >= 0.143 wraps included routers in a lazy ``_IncludedRouter``
    carrying the original router — walk both layers so a stream's real
    route is found no matter which representation it sits in.  The
    include-time prefix (``include_router(..., prefix="/api/v1")``) lives
    on the wrapper's ``include_context``, not on the inner routes.
    """
    for route in app.router.routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            prefix = getattr(context, "prefix", "") or ""
            for sub in inner.routes:
                sub_path = getattr(sub, "path", None)
                if sub_path is not None and path in (sub_path, prefix + sub_path):
                    return True
        if getattr(route, "path", None) == path:
            return True
    return False


def _mount(app: FastAPI, label: str, router: Any) -> None:
    if router is not None:
        app.include_router(router)
        logger.info("coordination.router_mounted", stream=label)


def register_coordination_routers(app: FastAPI) -> None:
    """Mount every stream router with ImportError tolerance (C0)."""

    # ── watchlist-anpr (this stream) — real routers ─────────────────────
    try:
        from traffic_oracle.vehicles import router as vehicles_router

        _mount(app, "vehicles", vehicles_router)
    except ImportError as exc:  # pragma: no cover - tree-safety stub
        logger.warning("coordination.import_failed", stream="vehicles", error=str(exc))
        app.include_router(_stub_router(("/api/v1/vehicles",)))

    try:
        from traffic_oracle.enforcement import router as enforcement_router

        _mount(app, "enforcement", enforcement_router)
    except ImportError as exc:  # pragma: no cover
        logger.warning(
            "coordination.import_failed", stream="enforcement", error=str(exc)
        )
        app.include_router(
            _stub_router(("/api/v1/enforcement/recommendations",))
        )

    try:
        from traffic_oracle.vehicles.pursuit import router as pursuit_router

        _mount(app, "pursuit", pursuit_router)
    except ImportError as exc:  # pragma: no cover
        logger.warning("coordination.import_failed", stream="pursuit", error=str(exc))

    # ── city fusion spine (C-C) — cameras, entities, dossier, feed, audit ──
    try:
        from traffic_oracle.city.router import router as city_router

        _mount(app, "city", city_router)
    except ImportError as exc:  # pragma: no cover - tree-safety stub
        logger.warning("coordination.import_failed", stream="city", error=str(exc))
        app.include_router(_stub_router(CITY_PATHS))

    # ── signalling / canvas / adaptive streams — 503 stubs until landed ──
    stubbed: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("signals", ("/api/v1/signals/state",)),
        ("units", ("/api/v1/units",)),
        ("situation_log", ("/api/v1/adaptive/situation_log",)),
    )
    for label, paths in stubbed:
        if any(_route_exists(app, p) for p in paths):
            logger.info("coordination.stub_skipped", stream=label)
            continue
        app.include_router(_stub_router(paths))
        logger.info("coordination.stub_mounted", stream=label)
