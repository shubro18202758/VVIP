"""Run orchestration — build, ledger, execute, replay.

Execution model: each frame fires at ``t0 + t_sec / clock_scale`` (late
frames fire immediately, flagged late).  Delivery is per-frame and honest:
every frame ends ``delivered`` / ``failed(reason)`` / ``skipped(reason)``,
and the run row records the summary.  The emulator NEVER consumes or
produces outcomes — twin responses are logged as status codes only.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from event_emulator.library import UseCase, load_library
from event_emulator.nats_seam import (
    ANPR_EMULATED_SUBJECT,
    NatsSeam,
    UNITS_EMULATED_SUBJECT,
)
from event_emulator.profiles import UseCaseProfile, profile_for, protection_class_for
from event_emulator.spec import EmulationSpec, ResolvedRun
from event_emulator.store import CorridorStore, utcnow
from event_emulator.tape import CauseTape, build_tape
from event_emulator.twin_seam import SafeHttpClient, TwinSeam
from event_emulator.valkey_seam import ValkeySeam

logger = structlog.get_logger(__name__)

__all__ = ["RunnerDeps", "RunOutcome", "execute_run", "execute_replay"]


@dataclass
class RunnerDeps:
    """Real-stack clients; any may be None → its frames degrade honestly."""

    store: CorridorStore | None = None
    valkey: ValkeySeam | None = None
    twin: TwinSeam | None = None
    nats: NatsSeam | None = None
    sleep: Any = asyncio.sleep  # injectable clock for tests


@dataclass
class RunOutcome:
    run_id: int | None
    status: str
    tape: CauseTape
    movement_id: str
    delivery: dict = field(default_factory=dict)
    frame_log: list[dict] = field(default_factory=list)


def _summarize(frame_log: list[dict]) -> dict:
    by: dict[str, int] = {}
    for e in frame_log:
        by[e["result"]] = by.get(e["result"], 0) + 1
    return {
        "frames": len(frame_log),
        "delivered": by.get("delivered", 0),
        "failed": by.get("failed", 0),
        "skipped": by.get("skipped", 0),
        "late": sum(1 for e in frame_log if e.get("late")),
    }


def _has_watchlist_knobs(profile: UseCaseProfile) -> bool:
    return bool(
        getattr(profile, "anpr_cameras", 0)
        or getattr(profile, "agency_units", 0)
    )


async def _deliver_anpr_read(
    deps: RunnerDeps,
    payload: dict,
    t_sec: float,
    t0_iso: str,
    run_id: int | None,
    is_replay: bool,
) -> dict:
    """Persist one anpr_read as a sighting + publish the emulated JSON.

    Deterministic read ids make this idempotent — a replay upserts nothing
    (ON CONFLICT DO NOTHING) and re-publishes only.  Honest per-frame
    result: delivered / skipped / failed with reason.
    """
    read_id = str(payload.get("read_id") or "")
    if deps.store is None:
        return {"result": "skipped", "reason": "store not configured"}
    try:
        from datetime import datetime as _dt

        t = _dt.fromisoformat(t0_iso) + timedelta(seconds=float(t_sec))
        vehicle_id = await deps.store.upsert_vehicle(
            str(payload["plate"]),
            str(payload.get("vehicle_class", "car")),
            t,
        )
        inserted = await deps.store.insert_sighting(
            sighting_id=read_id,
            vehicle_id=vehicle_id,
            plate=str(payload["plate"]),
            camera_id=str(payload["camera_id"]),
            t=t,
            lon=float(payload["position"][0]),
            lat=float(payload["position"][1]),
            speed_kmh=payload.get("speed_kmh"),
            heading=payload.get("heading"),
            vehicle_class=payload.get("vehicle_class"),
            run_id=run_id,
            matched_plate_hit=payload.get("matched_plate_hit"),
        )
    except Exception as exc:  # noqa: BLE001 - per-frame isolation
        return {"result": "failed", "reason": f"{type(exc).__name__}: {exc}"}

    entry: dict = {"result": "delivered", "sighting_new": inserted}
    if deps.nats is not None:
        doc = dict(payload)
        doc["t_sec"] = float(t_sec)
        doc["source"] = "emulated_anpr"
        ok = await deps.nats.publish_json(
            ANPR_EMULATED_SUBJECT, doc, replay_run_id=run_id if is_replay else None
        )
        entry["nats"] = "delivered" if ok else "failed"
    return entry


async def _deliver_agency_unit(
    deps: RunnerDeps,
    payload: dict,
    run_id: int | None,
    is_replay: bool,
) -> dict:
    """Publish one agency_unit position (clearly labelled emulated)."""
    if deps.nats is None:
        return {"result": "skipped", "reason": "nats not configured"}
    doc = dict(payload)
    doc["source"] = "emulated"
    ok = await deps.nats.publish_json(
        UNITS_EMULATED_SUBJECT, doc, replay_run_id=run_id if is_replay else None
    )
    return {"result": "delivered" if ok else "failed"}


async def _deliver_twin_causes(
    deps: RunnerDeps,
    use_case: UseCase,
    profile: UseCaseProfile,
    tape: CauseTape,
    run_id: int | None,
    replay: bool,
    frame_log: list[dict],
) -> None:
    """One POST of the full deterministic cause set to the real twin."""
    if deps.twin is None:
        frame_log.append({"frame": None, "kind": "twin_post", "result": "skipped",
                          "reason": "twin seam not configured"})
        return
    cfg = use_case.config
    forced = [dict(f.payload) for f in tape.frames if f.kind == "scheduled_event"]
    ec = next(f.payload for f in tape.frames if f.kind == "event_config")
    overrides = dict(ec["event_overrides"])
    overrides["ambulance_rate_multiplier"] = ec["ambulance_rate_multiplier"]
    body = deps.twin.build_body(
        route_segment_ids=list(cfg["route_segment_ids"]),
        departure_time=cfg["departure_time"],
        closure_policy=dict(cfg["closure_policy"]),
        horizon_min=int(cfg["horizon_min"]),
        n_replications=int(cfg["n_replications"]),
        seed=tape.seed,
        weather=dict(cfg["weather"]),
        event_overrides=overrides,
        forced_events=forced,
        scenario_id=str(cfg["scenario_id"]),
    )
    try:
        resp = await deps.twin.post_twin(
            body, replay_run_id=run_id if replay else None
        )
        result = "delivered" if resp.status_code == 200 else "failed"
        entry: dict[str, Any] = {
            "frame": None, "kind": "twin_post", "result": result,
            "http_status": resp.status_code,
            "causes": len(forced),
            "delivery_level": (
                "twin_accepted_fields"
                if resp.status_code == 200 else "twin_rejected"
            ),
        }
        frame_log.append(entry)
    except Exception as exc:  # noqa: BLE001 - degradation, not fatal
        frame_log.append({"frame": None, "kind": "twin_post", "result": "failed",
                          "reason": f"{type(exc).__name__}: {exc}"})


async def execute_run(
    deps: RunnerDeps,
    spec: EmulationSpec,
    *,
    library: dict[str, UseCase] | None = None,
    replay_of: int | None = None,
    replay_tape: CauseTape | None = None,
    replay_t0: datetime | None = None,
) -> RunOutcome:
    """Inject (or replay) one use case through the real stack's seams."""
    lib = library if library is not None else load_library()
    if spec.use_case_id not in lib:
        raise KeyError(f"unknown use case {spec.use_case_id!r}")
    use_case = lib[spec.use_case_id]
    profile = profile_for(spec.use_case_id)
    is_replay = replay_tape is not None

    if is_replay and replay_tape is not None:
        tape = replay_tape
        if replay_t0 is not None:
            tape = CauseTape(
                use_case_id=tape.use_case_id,
                seed=tape.seed,
                t0_anchor_iso=replay_t0.astimezone(timezone.utc).isoformat(),
                clock_scale=spec.clock_scale,
                movement_id=tape.movement_id,
                frames=tape.frames,  # re-posted VERBATIM — replay contract
            )
    else:
        run = ResolvedRun.resolve(spec, use_case)
        # watchlist knobs (C2) need real route geometry to anchor camera
        # reads / agency units: fetch centroids BEFORE the pure tape build
        # (the same seam event_row delivery uses).  Without a store the
        # generators fall back to the documented belt anchor — still pure.
        centroids_for_tape = None
        if _has_watchlist_knobs(profile) and deps.store is not None:
            try:
                centroids_for_tape = await deps.store.segment_centroids(
                    list(use_case.config["route_segment_ids"])
                )
            except Exception as exc:  # noqa: BLE001 - anchor degrades, run lives
                logger.warning("runner.centroids_failed", error=str(exc))
        tape = build_tape(run, use_case, profile, segment_centroids=centroids_for_tape)

    # ── ledger ────────────────────────────────────────────────────────────
    run_id: int | None = None
    if deps.store is not None:
        try:
            run_id = await deps.store.insert_run(
                use_case_id=spec.use_case_id,
                spec=json.loads(spec.model_dump_json()) if not is_replay else {
                    "replay_of_run_id": replay_of,
                    "clock_scale": spec.clock_scale,
                },
                spec_hash=ResolvedRun.resolve(spec, use_case).spec_hash,
                seed=tape.seed,
                t0_anchor=datetime.fromisoformat(tape.t0_anchor_iso),
                clock_scale=spec.clock_scale,
                tape_json=tape.to_json(),
                is_replay=is_replay,
                replay_of_run_id=replay_of,
            )
        except Exception as exc:  # noqa: BLE001 - ledger down ≠ run down
            logger.error("runner.ledger_insert_failed", error=str(exc))

    frame_log: list[dict] = []
    t0 = datetime.fromisoformat(tape.t0_anchor_iso).timestamp()
    movement_created = False

    # ── the one twin POST carrying the deterministic cause set ────────────
    if spec.deliver_twin:
        await _deliver_twin_causes(
            deps, use_case, profile, tape, run_id, is_replay, frame_log
        )
    else:
        frame_log.append({"frame": None, "kind": "twin_post", "result": "skipped",
                          "reason": "deliver_twin=False"})

    # ── timed frames ──────────────────────────────────────────────────────
    last_doc: dict[str, Any] | None = None
    last_rows: list[tuple[int, float, float, float, float]] = []
    centroids: dict[int, tuple[float, float]] = {}
    start_monotonic = time.monotonic()

    for f in tape.frames:
        fire_at = t0 + f.t_sec / spec.clock_scale
        delay = fire_at - (t0 + (time.monotonic() - start_monotonic))
        if delay > 0:
            await deps.sleep(delay)
        late = delay < -1.0

        entry: dict[str, Any] = {"frame": f.frame, "kind": f.kind, "t_sec": f.t_sec}

        try:
            if f.kind == "event_row":
                p = f.payload
                if p["action"] == "create":
                    if deps.store is None:
                        entry.update(result="skipped", reason="store not configured")
                    else:
                        route = list(p["route_segment_ids"])
                        centroids = await deps.store.segment_centroids(route)
                        if len(centroids) < len(route):
                            missing = [s for s in route if s not in centroids]
                            entry.update(result="failed",
                                         reason=f"unknown segments {missing}")
                        else:
                            pts = [centroids[s] for s in route]
                            t0_dt = datetime.fromisoformat(tape.t0_anchor_iso)
                            horizon_sec = max(
                                (fr.t_sec for fr in tape.frames
                                 if fr.kind == "event_row"), default=0.0
                            )
                            # legacy row — what today's running engine loop
                            # polls (engine_loop.py:40 reads convoy.movements)
                            await deps.store.upsert_movement(
                                p["movement_id"], p["vvip_class"],
                                t0_dt, pts[0], pts[-1],
                            )
                            await deps.store.replace_route(
                                p["movement_id"], route, pts
                            )
                            # generalized row — core's 007 events schema,
                            # soft-linked to the movement (no FK by design)
                            cfg = use_case.config
                            await deps.store.upsert_corridor_event(
                                event_id=p["movement_id"],
                                movement_id=p["movement_id"],
                                event_kind=p["event_kind"],
                                protection_class=protection_class_for(
                                    profile, p["vvip_class"]
                                ),
                                crowd_class=profile.crowd_class,
                                title=str(cfg["name"]),
                                planned_start=t0_dt,
                                planned_end=t0_dt + timedelta(seconds=horizon_sec),
                                metadata_doc={
                                    "source": "event-emulator",
                                    "use_case_id": spec.use_case_id,
                                    "scenario_id": cfg["scenario_id"],
                                    "seed": tape.seed,
                                    "closure_policy": cfg["closure_policy"],
                                    "is_replay": is_replay,
                                },
                            )
                            await deps.store.replace_event_route(
                                p["movement_id"], route
                            )
                            await deps.store.replace_event_phase(
                                p["movement_id"],
                                label=p["event_kind"],
                                window_start=t0_dt,
                                window_end=t0_dt + timedelta(seconds=horizon_sec),
                                closure_policy=dict(cfg["closure_policy"]),
                            )
                            movement_created = True
                            entry.update(result="delivered",
                                         movement_id=p["movement_id"])
                elif p["action"] == "complete":
                    if deps.store is None or not movement_created:
                        entry.update(result="skipped", reason="no live movement row")
                    else:
                        await deps.store.set_movement_status(p["movement_id"], "completed")
                        await deps.store.set_event_status(p["movement_id"], "completed")
                        entry.update(result="delivered")

            elif f.kind == "inputs_doc":
                if not movement_created or deps.valkey is None:
                    entry.update(result="skipped",
                                 reason="no movement row" if not movement_created
                                 else "valkey not configured")
                else:
                    await deps.valkey.write_inputs_doc(tape.movement_id, f.payload)
                    if run_id is not None:
                        await deps.valkey.write_source(
                            tape.movement_id, run_id, spec.use_case_id, is_replay
                        )
                    last_doc = f.payload
                    entry.update(result="delivered")

            elif f.kind in ("scheduled_event", "event_config",
                            "weather_ramp", "crowd_script"):
                # carried by the twin POST (causes) and, for the doc-shaped
                # ones, by the inputs_doc series — recorded as informational
                entry.update(result="delivered", delivered_by="twin_post"
                             if f.kind in ("scheduled_event", "event_config")
                             else "inputs_docs")

            elif f.kind == "anpr_read":
                # watchlist delivery (C2): sighting row + emulated NATS JSON
                outcome = await _deliver_anpr_read(
                    deps, f.payload, f.t_sec, tape.t0_anchor_iso, run_id, is_replay
                )
                entry.update(outcome)

            elif f.kind == "agency_unit":
                outcome = await _deliver_agency_unit(
                    deps, f.payload, run_id, is_replay
                )
                entry.update(outcome)

            else:  # pragma: no cover - tape kinds are closed
                entry.update(result="skipped", reason=f"unknown kind {f.kind}")

        except Exception as exc:  # noqa: BLE001 - per-frame isolation
            entry.update(result="failed", reason=f"{type(exc).__name__}: {exc}")

        if late:
            entry["late"] = True
        frame_log.append(entry)

        # showcase plane: project the freshest cause state onto .emulated
        if (f.kind == "inputs_doc" and last_doc is not None
                and deps.nats is not None and spec.deliver_nats):
            rows = _project_rows(last_doc, centroids)
            if rows:
                ok = await deps.nats.publish(rows, replay_run_id=run_id if is_replay else None)
                frame_log.append({
                    "frame": f.frame, "kind": "nats_emulated_batch",
                    "result": "delivered" if ok else "failed",
                    "segments": len(rows),
                })

    delivery = _summarize(frame_log)
    failed_share = (
        delivery["failed"] / delivery["frames"] if delivery["frames"] else 1.0
    )
    status = "failed" if (not movement_created and deps.store is not None) or failed_share > 0.5 \
        else "completed"

    if deps.store is not None and run_id is not None:
        try:
            await deps.store.finish_run(run_id, status, delivery)
        except Exception as exc:  # noqa: BLE001
            logger.error("runner.ledger_finish_failed", error=str(exc))

    return RunOutcome(
        run_id=run_id, status=status, tape=tape,
        movement_id=tape.movement_id, delivery=delivery, frame_log=frame_log,
    )


def _project_rows(
    doc: dict[str, Any], centroids: dict[int, tuple[float, float]]
) -> list[tuple[int, float, float, float, float]]:
    """Project one inputs doc onto per-segment synthetic observations."""
    cong = float(doc.get("predicted_congestion_idx", 0.0))
    rows: list[tuple[int, float, float, float, float]] = []
    for sid, (lon, lat) in sorted(centroids.items()):
        speed = max(5.0, 50.0 * (1.0 - 0.85 * cong))
        rows.append((sid, lon, lat, round(speed, 2), round(cong, 4)))
    return rows


async def execute_replay(
    deps: RunnerDeps,
    *,
    tape: CauseTape,
    clock_scale: float,
    deliver_nats: bool = True,
    deliver_twin: bool = True,
    replay_of: int | None = None,
    t0: datetime | None = None,
    library: dict[str, UseCase] | None = None,
) -> RunOutcome:
    """Re-post a stored tape verbatim with a re-anchored t0 (replay mode)."""
    lib = library if library is not None else load_library()
    spec = EmulationSpec(
        use_case_id=tape.use_case_id,
        seed=tape.seed,
        t0_anchor=None,
        clock_scale=clock_scale,
        deliver_nats=deliver_nats,
        deliver_twin=deliver_twin,
    )
    anchor = t0 or utcnow()
    return await execute_run(
        deps, spec, library=lib, replay_of=replay_of,
        replay_tape=tape, replay_t0=anchor,
    )
