"""Emulator HTTP surface — loopback :8090; the deck reaches it through its
same-origin /api/emulator proxy (CORS stays open to the configured deck
origins for the dev server).

Routes (contract C-G):
    GET  /health                            real probes + living_city
    GET  /api/v1/emulator/use-cases
    POST /api/v1/emulator/runs              202 {run_id, status:'running', use_case_id}
                                            right after the ledger insert — the run
                                            continues as a background task (tasks.py);
                                            409 {detail, run_id} while that use case
                                            runs; 404 unknown use case
    GET  /api/v1/emulator/runs?limit=1..500
    GET  /api/v1/emulator/runs/{id}         + status, progress, event_id, movement_id,
                                            event_status (additive: 'planning' mid-run =
                                            its activation is still pending)
                                            (?include_tape=false: a light progress poll);
                                            spec.scenario_precondition (C-N, the sweep's
                                            EMULATED stolen-FIR earmark) from the moment
                                            it is settled — mid-run, not only at the end
    POST /api/v1/emulator/runs/{id}/stop    200 {run_id, status:'stopped'}
    POST /api/v1/emulator/runs/{id}/replay  202 — a verbatim replay, async like a run;
                                            422 {detail, reason:'route_not_on_current_network',
                                            run_id, use_case_id, route_segment_ids, problems}
                                            when the tape's recorded route is not a legal path
                                            on the CURRENT road network (a run recorded before
                                            the network changed would re-animate its event off
                                            the roads — runner.replay_refusal); 409 stays C-G's
                                            "that use case is running"
    GET  /api/v1/emulator/living-city       ambient-city status
    POST /api/v1/emulator/living-city       {action: 'start' | 'stop'}

No URL-valued fields are accepted anywhere on this surface; the outbound
policy in safe_http.py stays the only path out of the process.

Health is honest: every seam is PROBED (SELECT 1, Valkey PING, NATS
PING/PONG, oracle /health) rather than reported by object existence, the
Valkey client is kept even when the boot-time PING lost a race (it heals on
its own), a dead broker cannot hang the boot (the NATS connect is bounded),
and the status is 'degraded' — with reasons — whenever a seam is down (the
oracle included: it carries the lifecycle, ANPR, dispatch and twin seams)
or the last run that ended on its own failed (its corridor event never
went live, say) or delivered no frame.  Shutdown
interrupts every run and the living city together within Docker's stop
window; a run a killed process could not close is reaped on the next boot.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable, Literal

import structlog
import uvicorn
import valkey.asyncio as valkey_async
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from event_emulator.ambient import LivingCity
from event_emulator.config import Settings, get_settings
from event_emulator.library import UseCase, load_library
from event_emulator.nats_seam import NatsSeam
from event_emulator.profiles import profile_for
from event_emulator.runner import RunnerDeps, close_event, replay_refusal
from event_emulator.spec import EmulationSpec
from event_emulator.store import SYSTEM_ACTOR, CorridorStore, runner_identity, utcnow
from event_emulator.tape import CauseTape
from event_emulator.tasks import STALE_HEARTBEAT_SEC, Reaper, RunConflict, TaskRegistry
from event_emulator.twin_seam import OracleSeam, SafeHttpClient, TwinSeam
from event_emulator.valkey_seam import ValkeySeam

logger = structlog.get_logger(__name__)

#: A probe that does not answer within this budget counts as down.
_PROBE_TIMEOUT_SEC = 2.5

#: A running living city with nothing delivered after this long is degraded.
_LIVING_GRACE_SEC = 60.0

#: Shutdown budget: runs and the living city close their events and rows
#: concurrently, inside Docker's default 10 s stop window.
_SHUTDOWN_SEC = 7.0


class CreateRunRequest(BaseModel):
    use_case_id: str
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)
    t0_anchor: datetime | None = None
    clock_scale: float = Field(default=1.0, gt=0.0, le=3600.0)
    # ≥ 1 s: the tape is built on the event loop (a 1 ms tick would stall
    # the living city and /health while millions of frames build)
    tick_sec: float = Field(default=5.0, ge=1.0, le=60.0)
    deliver_nats: bool = True
    deliver_twin: bool = True


class ReplayRequest(BaseModel):
    t0: datetime | None = None
    clock_scale: float = Field(default=1.0, gt=0.0, le=3600.0)


class LivingCityRequest(BaseModel):
    action: Literal["start", "stop"]


async def _build_deps(settings: Settings) -> tuple[RunnerDeps, list[Callable[[], Awaitable[Any]]]]:
    """Real-stack seams + their closers.  Nothing here gates on a first
    successful round trip: every seam reconnects on its own and /health
    probes the real state."""
    closers: list[Callable[[], Awaitable[Any]]] = []
    store: CorridorStore | None = None
    if settings.database_url:
        try:
            pool = AsyncConnectionPool(
                settings.database_url, min_size=1, max_size=6, open=False,
                timeout=5.0, check=AsyncConnectionPool.check_connection,
            )
            await pool.open(wait=False)
            store = CorridorStore(pool, runner=runner_identity(settings.port))
            closers.append(pool.close)
        except Exception as exc:  # noqa: BLE001 - degrade, stay up
            logger.error("api.pool_init_failed", error=str(exc))
    vclient = valkey_async.from_url(
        settings.valkey_url, socket_connect_timeout=2, socket_timeout=3,
    )
    valkey_seam = ValkeySeam(vclient, ttl_sec=settings.inputs_doc_ttl_sec)
    closers.append(valkey_seam.close)
    http = SafeHttpClient(internal_endpoints=(settings.traffic_oracle_url,))
    closers.append(http.close)
    nats_seam = NatsSeam(settings.nats_url, settings.nats_emulated_subject)
    await nats_seam.connect(force=True)
    closers.append(nats_seam.close)
    deps = RunnerDeps(
        store=store,
        valkey=valkey_seam,
        twin=TwinSeam(http, settings.traffic_oracle_url),
        nats=nats_seam,
        oracle=OracleSeam(http, settings.traffic_oracle_url, SYSTEM_ACTOR),
    )
    return deps, closers


def create_app(
    deps: RunnerDeps | None = None,
    *,
    settings: Settings | None = None,
    library: dict[str, UseCase] | None = None,
    background: bool = True,
) -> FastAPI:
    """App factory.  ``deps`` injects seams (tests); ``background`` False
    skips the reaper loop and the living-city autostart."""
    settings = settings or get_settings()
    library = library if library is not None else load_library()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        closers: list[Callable[[], Awaitable[Any]]] = []
        if deps is None:
            run_deps, closers = await _build_deps(settings)
        else:
            run_deps = deps
        registry = TaskRegistry(run_deps, library)
        living = LivingCity(
            run_deps, city_seed=settings.city_seed, enabled=settings.living_city,
            reserved_routes=registry.reserved_routes,
            violators=settings.living_violators,
        )
        registry.on_start = living.on_run_started
        reaper = Reaper(
            run_deps, lambda: registry.owned_run_ids() | living.owned_run_ids(),
            identity=getattr(run_deps.store, "runner", None),
        )
        app.state.deps = run_deps
        app.state.registry = registry
        app.state.living = living
        app.state.reaper = reaper
        app.state.library = library
        app.state.settings = settings
        background_tasks: list[asyncio.Task] = []
        if background:
            background_tasks.append(asyncio.create_task(reaper.loop(), name="emulator-reaper"))
            if settings.living_city and run_deps.store is not None:
                try:
                    await living.start()
                except Exception as exc:  # noqa: BLE001 - the API stays up
                    logger.error("api.living_city_start_failed", error=str(exc))
        try:
            yield
        finally:
            # interrupt (not abandon): close events, finish rows — runs and
            # the living city together, bounded inside Docker's stop window
            for t in background_tasks:
                t.cancel()
            closing = [registry.shutdown(timeout=_SHUTDOWN_SEC - 2.0)]
            if living.running:
                closing.append(living.stop("interrupted"))
            with suppress(Exception):
                await asyncio.wait_for(
                    asyncio.gather(*closing, return_exceptions=True), timeout=_SHUTDOWN_SEC)
            for close in reversed(closers):
                with suppress(Exception):
                    await close()

    app = FastAPI(title="corridor event emulator", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.deck_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _deps() -> RunnerDeps:
        return app.state.deps

    def _registry() -> TaskRegistry:
        return app.state.registry

    def _living() -> LivingCity:
        return app.state.living

    def _store() -> CorridorStore:
        store = _deps().store
        if store is None:
            raise HTTPException(503, "runs ledger unavailable (no database)")
        return store

    @app.get("/")
    async def root() -> dict:
        return {
            "service": "event-emulator",
            "principle": "causes only — no outcomes are ever produced",
            "seams": {
                "db": "corridor events + legacy movements (creation upsert + its ledger row)",
                "lifecycle": "POST /api/v1/events/{id}/status (C-F, event-emulator (system))",
                "anpr": "POST /api/v1/vehicles/sightings (C-E; the oracle computes hits/rules)",
                "valkey": "adaptive:inputs:{movement_id} (engine_loop reader shape)",
                "twin": "POST /api/v1/simulate/twin (deterministic cause set)",
                "nats": "corridor.traffic.emulated (Arrow) + corridor.units.emulated (C-A)",
            },
            "use_cases": len(app.state.library),
        }

    async def _probe(name: str, fn: Callable[[], Awaitable[Any]] | None) -> dict[str, Any]:
        if fn is None:
            return {"ok": False, "error": "not configured"}
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            ok = bool(await asyncio.wait_for(fn(), timeout=_PROBE_TIMEOUT_SEC))
            return {"ok": ok, "ms": round((loop.time() - started) * 1000.0, 1)}
        except Exception as exc:  # noqa: BLE001 - a failed probe is data
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    @app.get("/health")
    async def health() -> dict:
        d = _deps()
        names = ("store", "valkey", "nats", "twin")
        results = await asyncio.gather(
            _probe("store", d.store.probe if d.store else None),
            _probe("valkey", d.valkey.probe if d.valkey else None),
            _probe("nats", d.nats.probe if d.nats else None),
            _probe("twin", d.twin.probe if d.twin else None),
        )
        probes = dict(zip(names, results))
        for name, seam in (("nats", d.nats), ("twin", d.twin)):
            if not probes[name]["ok"] and "error" not in probes[name]:
                # the seam answers False rather than raising: name its last error
                last_error = getattr(seam, "last_error", None)
                if last_error:
                    probes[name]["error"] = str(last_error)[:200]
        reasons = [f"{n}: {probes[n].get('error') or 'probe failed'}"
                   for n in ("store", "valkey", "nats") if not probes[n]["ok"]]
        if not probes["twin"]["ok"]:
            # the oracle carries the lifecycle (C-F), ANPR (C-E), dispatch
            # and twin seams: without it no event opens or closes
            reasons.append(f"oracle: {probes['twin'].get('error') or 'unreachable'}")
        registry = _registry()
        last = registry.last_finished
        last_doc = None
        if last is not None:
            last_doc = {"run_id": last.run_id, "use_case_id": last.use_case_id,
                        "status": last.status, "reason": last.reason,
                        "event_status": last.control.event_status,
                        "progress": last.control.progress.to_dict()}
            p = last.control.progress
            # a run that ended on its own and failed (its corridor event
            # never went live, say) or delivered nothing — an operator's
            # early stop is not a fault
            if last.status == "failed" or (
                    p.delivered == 0 and last.status not in ("stopped", "interrupted")):
                msg = f"last run {last.run_id} ({last.status})"
                if p.delivered == 0:
                    msg += f" delivered 0 of {p.total or p.frame} frames"
                if last.reason:
                    msg += f": {last.reason}"
                reasons.append(msg)
        living = _living()
        if (living.running and living.attempted() > 0 and living.delivered() == 0
                and living.run_id is not None):
            since = living.status_doc().get("since")
            age = (utcnow() - datetime.fromisoformat(since)).total_seconds() if since else 0.0
            if age > _LIVING_GRACE_SEC:
                reasons.append("living city delivered nothing in its first minute")
        if living.running and living.detail:
            # e.g. its sites name roads the live network does not have
            reasons.append(f"living city: {living.detail}")
        return {
            "status": "degraded" if reasons else "ok",
            "degraded_reasons": reasons,
            "library": len(app.state.library),
            "stack": {n: probes[n]["ok"] for n in names},
            "probes": probes,
            "runs": {"running": [t.to_dict() for t in registry.running()]},
            "last_run": last_doc,
            "living_city": living.health_doc(),
            "reaper": app.state.reaper.last_pass,
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

    @app.post("/api/v1/emulator/runs", status_code=202)
    async def create_run(req: CreateRunRequest) -> Any:
        if req.use_case_id not in app.state.library:
            raise HTTPException(404, f"unknown use_case_id {req.use_case_id!r}")
        try:
            profile_for(req.use_case_id)
        except KeyError as exc:
            # library entry without an emulation profile yet — the library
            # is a growing handoff; profiles are the emulator's own file
            raise HTTPException(422, str(exc)) from exc
        _store()
        spec = EmulationSpec(
            use_case_id=req.use_case_id, seed=req.seed, t0_anchor=req.t0_anchor,
            clock_scale=req.clock_scale, tick_sec=req.tick_sec,
            deliver_nats=req.deliver_nats, deliver_twin=req.deliver_twin,
        )
        try:
            task = await _registry().start_run(spec)
        except RunConflict as exc:
            return JSONResponse(status_code=409,
                                content={"detail": str(exc), "run_id": exc.run_id})
        except Exception as exc:  # noqa: BLE001 - no ledger row, no run
            logger.error("api.run_start_failed", error=str(exc))
            raise HTTPException(503, f"run could not be ledgered: {exc}") from exc
        return {"run_id": task.run_id, "status": "running", "use_case_id": task.use_case_id,
                "clock_scale": req.clock_scale, "started_at": task.started_at.isoformat()}

    @app.get("/api/v1/emulator/runs")
    async def list_runs(limit: int = Query(default=50, ge=1, le=500)) -> dict:
        rows = await _store().fetch_runs(limit=limit)
        registry = _registry()
        for r in rows:
            _jsonify_datetimes(r)
            spec = r.get("spec") if isinstance(r.get("spec"), dict) else {}
            task = registry.get(int(r["run_id"]))
            r["ledger_status"] = r["status"]
            r["status"] = _run_status(r, spec, task)
            r["progress"] = (task.control.progress.to_dict() if task is not None
                             else spec.get("progress"))
        return {"runs": rows}

    @app.get("/api/v1/emulator/runs/{run_id}")
    async def get_run(
        run_id: int,
        include_tape: bool = Query(default=True, description="false: omit the (large) tape"),
    ) -> dict:
        row = await _store().fetch_run(run_id)
        if row is None:
            raise HTTPException(404, f"run {run_id} not found")
        _jsonify_datetimes(row)
        if isinstance(row.get("tape"), str):
            row["tape"] = json.loads(row["tape"])
        spec = row.get("spec") if isinstance(row.get("spec"), dict) else {}
        tape = row.get("tape") if isinstance(row.get("tape"), dict) else {}
        if not include_tape:
            # a progress poll needs the identity, never the frames
            row["tape"] = {k: v for k, v in tape.items()
                           if k not in ("frames", "slots", "ambulance_requests", "violations")}
        task = _registry().get(run_id)
        row["ledger_status"] = row["status"]
        row["status"] = _run_status(row, spec, task)
        row["progress"] = (task.control.progress.to_dict() if task is not None
                           else spec.get("progress")
                           or (spec.get("delivery") or {}).get("progress"))
        row["use_case_id"] = spec.get("use_case_id")
        row["mode"] = spec.get("mode", "use_case")
        movement_id = tape.get("movement_id") or (task.control.event_id if task else None)
        row["movement_id"] = movement_id
        row["event_id"] = spec.get("event_id") or movement_id
        row["event_status"] = spec.get("event_status")
        if task is not None:
            if task.control.event_id:
                row["event_id"] = task.control.event_id
            row["event_status"] = task.control.event_status
        return row

    @app.post("/api/v1/emulator/runs/{run_id}/stop")
    async def stop_run(run_id: int) -> Any:
        registry = _registry()
        task = registry.get(run_id)
        if task is not None and task.running:
            await registry.stop(run_id, "stopped")
            return {"run_id": run_id, "status": task.status}
        living = _living()
        if living.running and living.run_id == run_id:
            await living.stop("stopped")
            return {"run_id": run_id, "status": "stopped"}
        store = _store()
        row = await store.fetch_run(run_id)
        if row is None:
            raise HTTPException(404, f"run {run_id} not found")
        spec = row.get("spec") if isinstance(row.get("spec"), dict) else {}
        status = _run_status(row, spec, task)
        if status != "running":
            return {"run_id": run_id, "status": status, "detail": "run already finished"}
        # a 'running' row no task here owns: a live peer (fresh heartbeat)
        # keeps it; an orphan of a dead process is stopped on the spot
        beat = spec.get("heartbeat_at")
        if beat:
            age = (utcnow() - datetime.fromisoformat(beat)).total_seconds()
            if age < STALE_HEARTBEAT_SEC:
                return JSONResponse(status_code=409, content={
                    "detail": "run is owned by another emulator process", "run_id": run_id})
        await store.finish_run(run_id, "stopped", {
            "reason": "stopped by operator (orphaned run of a previous process)"})
        for ev in await store.live_emulator_events():
            if str(ev.get("run_id")) == str(run_id):
                await close_event(_deps().oracle, str(ev["event_id"]),
                                  f"emulator run {run_id} stopped by operator")
        return {"run_id": run_id, "status": "stopped"}

    @app.post("/api/v1/emulator/runs/{run_id}/replay", status_code=202)
    async def replay_run(run_id: int, req: ReplayRequest) -> Any:
        row = await _store().fetch_run(run_id)
        if row is None:
            raise HTTPException(404, f"run {run_id} not found")
        spec = row.get("spec") if isinstance(row.get("spec"), dict) else {}
        if spec.get("mode") == "living_city":
            raise HTTPException(
                422, "a living-city session is a day program, not a replayable tape")
        raw = row["tape"]
        doc = raw if isinstance(raw, dict) else json.loads(raw)
        if not doc.get("frames"):
            raise HTTPException(422, f"run {run_id} has no tape frames to replay")
        tape = CauseTape.from_dict(doc)
        if tape.use_case_id not in app.state.library:
            raise HTTPException(422, f"tape use case {tape.use_case_id!r} is not in the library")
        # a tape whose route is no longer a path on today's roads would
        # re-animate its event off them: refused before anything is ledgered
        try:
            refusal = await replay_refusal(_deps().store, tape, run_id)
        except Exception as exc:  # noqa: BLE001 - unchecked is not replayed
            logger.error("api.replay_route_check_failed", run_id=run_id, error=str(exc))
            raise HTTPException(
                503, f"run {run_id}'s recorded route could not be checked against the "
                     f"current road network: {exc}") from exc
        if refusal is not None:
            logger.info("api.replay_refused", run_id=run_id, problems=refusal["problems"])
            return JSONResponse(status_code=422, content=refusal)
        try:
            task = await _registry().start_replay(
                tape=tape, replay_of=run_id, clock_scale=req.clock_scale, t0=req.t0)
        except RunConflict as exc:
            return JSONResponse(status_code=409,
                                content={"detail": str(exc), "run_id": exc.run_id})
        except Exception as exc:  # noqa: BLE001
            logger.error("api.replay_start_failed", error=str(exc))
            raise HTTPException(503, f"replay could not be ledgered: {exc}") from exc
        return {"run_id": task.run_id, "status": "running", "use_case_id": task.use_case_id,
                "replay_of_run_id": run_id, "clock_scale": req.clock_scale,
                "started_at": task.started_at.isoformat()}

    @app.get("/api/v1/emulator/living-city")
    async def living_city_status() -> dict:
        return _living().status_doc()

    @app.post("/api/v1/emulator/living-city")
    async def living_city_control(req: LivingCityRequest) -> dict:
        living = _living()
        if req.action == "start":
            _store()
            return await living.start()
        return await living.stop("stopped")

    return app


def _run_status(row: dict, spec: dict, task: Any) -> str:
    """The C-G status: live from this process when it runs the run, else
    the ledger's final_status (which may be finer than the column)."""
    if task is not None and task.running:
        return "running"
    final = spec.get("final_status")
    if row.get("status") != "running" and isinstance(final, str):
        return final
    return str(row.get("status"))


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
