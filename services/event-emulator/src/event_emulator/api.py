"""Emulator HTTP surface — :8090, CORS open to the command-deck origin.

The showcase's emulator control panel talks to http://localhost:8090
directly (absolute loopback URL in dev and prod — contract C6), so CORS is
enabled for the deck origins.  No URL-valued fields are accepted anywhere
on this surface; the outbound policy in safe_http.py stays the only path
out of the process.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator

import structlog
import uvicorn
import valkey.asyncio as valkey_async
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from event_emulator.config import get_settings
from event_emulator.library import load_library
from event_emulator.nats_seam import NatsSeam
from event_emulator.runner import RunOutcome, RunnerDeps, execute_replay, execute_run
from event_emulator.spec import EmulationSpec
from event_emulator.store import CorridorStore
from event_emulator.tape import CauseTape
from event_emulator.twin_seam import SafeHttpClient, TwinSeam
from event_emulator.valkey_seam import ValkeySeam

logger = structlog.get_logger(__name__)


class CreateRunRequest(BaseModel):
    use_case_id: str
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)
    t0_anchor: datetime | None = None
    clock_scale: float = Field(default=1.0, gt=0.0, le=3600.0)
    tick_sec: float = Field(default=5.0, gt=0.0, le=60.0)
    deliver_nats: bool = True
    deliver_twin: bool = True


class ReplayRequest(BaseModel):
    t0: datetime | None = None
    clock_scale: float = Field(default=1.0, gt=0.0, le=3600.0)


def create_app() -> FastAPI:
    settings = get_settings()
    library = load_library()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store: CorridorStore | None = None
        valkey_seam: ValkeySeam | None = None
        pool = None
        if settings.database_url:
            try:
                pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=4)
                await pool.open()
                store = CorridorStore(pool)
            except Exception as exc:  # noqa: BLE001 - degrade, stay up
                logger.error("api.pool_init_failed", error=str(exc))
                store = None
        try:
            vclient = valkey_async.from_url(settings.valkey_url)
            await vclient.ping()
            valkey_seam = ValkeySeam(vclient, ttl_sec=settings.inputs_doc_ttl_sec)
        except Exception as exc:  # noqa: BLE001
            logger.error("api.valkey_init_failed", error=str(exc))
            valkey_seam = None
        http = SafeHttpClient(internal_endpoints=(settings.traffic_oracle_url,))
        twin = TwinSeam(http, settings.traffic_oracle_url)
        nats_seam = NatsSeam(settings.nats_url, settings.nats_emulated_subject)
        app.state.deps = RunnerDeps(store=store, valkey=valkey_seam, twin=twin, nats=nats_seam)
        app.state.store = store
        app.state.library = library
        app.state.settings = settings
        yield
        await http.close()
        if valkey_seam is not None:
            await valkey_seam.close()
        if nats_seam is not None:
            await nats_seam.close()
        if pool is not None:
            await pool.close()

    app = FastAPI(title="corridor event emulator", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.deck_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _deps() -> RunnerDeps:
        return app.state.deps

    @app.get("/")
    async def root() -> dict:
        return {
            "service": "event-emulator",
            "principle": "causes only — no outcomes are ever produced",
            "seams": {
                "db": "convoy.movements + convoy.planned_routes (corridor-store)",
                "valkey": "adaptive:inputs:{movement_id} (engine_loop reader shape)",
                "twin": "POST /api/v1/simulate/twin (deterministic cause set)",
                "nats": "corridor.traffic.emulated (showcase-only Arrow IPC)",
            },
            "use_cases": len(app.state.library),
        }

    @app.get("/health")
    async def health() -> dict:
        twin_up = await _deps().twin.probe() if _deps().twin else False
        return {
            "status": "ok",
            "library": len(app.state.library),
            "stack": {
                "store": _deps().store is not None,
                "valkey": _deps().valkey is not None,
                "twin": twin_up,
                "nats": _deps().nats is not None,
            },
        }

    @app.get("/api/v1/emulator/use-cases")
    async def use_cases() -> list[dict]:
        return [
            {"id": uc.id, "name": uc.config["name"], "category": uc.config["category"],
             "vvip_class": uc.config["vvip_class"],
             "route_segment_ids": uc.config["route_segment_ids"],
             "horizon_min": uc.config["horizon_min"],
             "seed": uc.config["seed"]}
            for uc in app.state.library.values()
        ]

    @app.post("/api/v1/emulator/runs")
    async def create_run(req: CreateRunRequest) -> dict:
        if req.use_case_id not in app.state.library:
            raise HTTPException(404, f"unknown use_case_id {req.use_case_id!r}")
        spec = EmulationSpec(
            use_case_id=req.use_case_id, seed=req.seed, t0_anchor=req.t0_anchor,
            clock_scale=req.clock_scale, tick_sec=req.tick_sec,
            deliver_nats=req.deliver_nats, deliver_twin=req.deliver_twin,
        )
        try:
            outcome = await execute_run(_deps(), spec, library=app.state.library)
        except KeyError as exc:
            # library entry without an emulation profile yet — the library
            # is a growing handoff; profiles are the emulator's own file
            raise HTTPException(422, str(exc)) from exc
        return _outcome_dict(outcome, include_tape=False)

    @app.get("/api/v1/emulator/runs")
    async def list_runs(limit: int = 50) -> dict:
        store: CorridorStore | None = app.state.store
        if store is None:
            raise HTTPException(503, "runs ledger unavailable (no database)")
        rows = await store.fetch_runs(limit=min(limit, 500))
        for r in rows:
            _jsonify_datetimes(r)
        return {"runs": rows}

    @app.get("/api/v1/emulator/runs/{run_id}")
    async def get_run(run_id: int) -> dict:
        store: CorridorStore | None = app.state.store
        if store is None:
            raise HTTPException(503, "runs ledger unavailable (no database)")
        row = await store.fetch_run(run_id)
        if row is None:
            raise HTTPException(404, f"run {run_id} not found")
        _jsonify_datetimes(row)
        if isinstance(row.get("tape"), str):
            row["tape"] = json.loads(row["tape"])
        return row

    @app.post("/api/v1/emulator/runs/{run_id}/replay")
    async def replay_run(run_id: int, req: ReplayRequest) -> dict:
        store: CorridorStore | None = app.state.store
        if store is None:
            raise HTTPException(503, "runs ledger unavailable (no database)")
        row = await store.fetch_run(run_id)
        if row is None:
            raise HTTPException(404, f"run {run_id} not found")
        raw = row["tape"]
        tape = CauseTape.from_dict(raw if isinstance(raw, dict) else json.loads(raw))
        outcome = await execute_replay(
            _deps(), tape=tape, clock_scale=req.clock_scale,
            replay_of=run_id, t0=req.t0, library=app.state.library,
        )
        return _outcome_dict(outcome, include_tape=False)

    def _outcome_dict(outcome: RunOutcome, include_tape: bool) -> dict:
        payload: dict[str, Any] = {
            "run_id": outcome.run_id,
            "status": outcome.status,
            "movement_id": outcome.movement_id,
            "use_case_id": outcome.tape.use_case_id,
            "seed": outcome.tape.seed,
            "t0_anchor": outcome.tape.t0_anchor_iso,
            "clock_scale": outcome.tape.clock_scale,
            "delivery": outcome.delivery,
        }
        if include_tape:
            payload["tape"] = json.loads(outcome.tape.to_json())
        return payload

    return app


def _jsonify_datetimes(row: dict) -> None:
    for k, v in list(row.items()):
        if isinstance(v, datetime):
            row[k] = v.isoformat()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "event_emulator.api:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
