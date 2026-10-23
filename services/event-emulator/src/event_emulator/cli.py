"""Inject CLI — ``python -m event_emulator.cli <command>``.

    list                          — the UseCaseLibrary ids
    inject USE_CASE_ID [opts]     — build + ledger + inject a run
    replay RUN_ID | --tape PATH   — re-post a stored tape, re-anchored (refused, exit 2,
                                    when its route is no longer a path on the network)
    show RUN_ID                   — ledger row for a run

``inject --dry-run`` builds and (unless --no-ledger) ledgers the tape but
delivers nothing — the deterministic preview.  ``--tape-out`` writes the
tape file; two dry-runs of the same spec+seed+t0 produce byte-identical
files (the determinism self-check).
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import click
import structlog
import valkey.asyncio as valkey_async
from psycopg_pool import AsyncConnectionPool

from event_emulator.config import get_settings
from event_emulator.library import load_library
from event_emulator.nats_seam import NatsSeam
from event_emulator.profiles import profile_for
from event_emulator.runner import RunnerDeps, execute_replay, execute_run, replay_refusal
from event_emulator.spec import EmulationSpec
from event_emulator.store import SYSTEM_ACTOR, CorridorStore
from event_emulator.tape import CauseTape
from event_emulator.twin_seam import OracleSeam, SafeHttpClient, TwinSeam
from event_emulator.valkey_seam import ValkeySeam

logger = structlog.get_logger(__name__)


async def _build_deps(dry_run: bool) -> tuple[RunnerDeps, object | None]:
    settings = get_settings()
    store: CorridorStore | None = None
    pool = None
    if settings.database_url and not dry_run:
        pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=4)
        await pool.open()
        store = CorridorStore(pool)
    valkey_seam: ValkeySeam | None = None
    nats_seam: NatsSeam | None = None
    twin: TwinSeam | None = None
    oracle: OracleSeam | None = None
    if not dry_run:
        try:
            vclient = valkey_async.from_url(settings.valkey_url)
            await vclient.ping()
            valkey_seam = ValkeySeam(vclient, ttl_sec=settings.inputs_doc_ttl_sec)
        except Exception as exc:  # noqa: BLE001
            click.echo(f"warn: valkey unavailable ({exc}); inputs docs will skip",
                       err=True)
        http = SafeHttpClient(internal_endpoints=(settings.traffic_oracle_url,))
        twin = TwinSeam(http, settings.traffic_oracle_url)
        oracle = OracleSeam(http, settings.traffic_oracle_url, SYSTEM_ACTOR)
        nats_seam = NatsSeam(settings.nats_url, settings.nats_emulated_subject)
    return RunnerDeps(store=store, valkey=valkey_seam, twin=twin, nats=nats_seam,
                      oracle=oracle), pool


def _cleanup(deps: RunnerDeps, pool: object | None) -> None:
    asyncio.get_event_loop().run_until_complete(_aclose(deps, pool))


async def _aclose(deps: RunnerDeps, pool: object | None) -> None:
    if deps.valkey is not None:
        await deps.valkey.close()
    if deps.nats is not None:
        await deps.nats.close()
    if pool is not None:
        await pool.close()


@click.group()
def cli() -> None:
    """Corridor event emulator — causes only, never outcomes."""


@cli.command("list")
def list_use_cases() -> None:
    lib = load_library()
    for uc in lib.values():
        c = uc.config
        click.echo(
            f"{uc.id:32s} {c['vvip_class']:3s} {c['category']:10s} "
            f"horizon={c['horizon_min']}m reps={c['n_replications']} "
            f"seed={c['seed']} route={c['route_segment_ids']}"
        )


@cli.command()
@click.argument("use_case_id")
@click.option("--seed", type=int, default=None, help="override the config seed")
@click.option("--clock-scale", type=float, default=1.0, help="wall-clock compression")
@click.option("--tick-sec", type=float, default=5.0, help="inputs doc cadence")
@click.option("--t0", "t0_iso", default=None, help="ISO anchor (default: now)")
@click.option("--dry-run", is_flag=True, help="build + ledger, deliver nothing")
@click.option("--no-ledger", is_flag=True, help="do not write the runs ledger")
@click.option("--no-nats", is_flag=True, help="skip corridor.traffic.emulated")
@click.option("--no-twin", is_flag=True, help="skip the twin POST")
@click.option("--tape-out", type=click.Path(), default=None, help="write tape JSON here")
def inject(
    use_case_id: str, seed: int, clock_scale: float, tick_sec: float, t0_iso: str,
    dry_run: bool, no_ledger: bool, no_nats: bool, no_twin: bool, tape_out: str,
) -> None:
    lib = load_library()
    if use_case_id not in lib:
        click.echo(f"unknown use case {use_case_id!r}; see `list`", err=True)
        raise SystemExit(2)

    async def _run() -> None:
        deps, pool = await _build_deps(dry_run)
        if no_ledger:
            deps.store = None
        try:
            spec = EmulationSpec(
                use_case_id=use_case_id, seed=seed,
                t0_anchor=datetime.fromisoformat(t0_iso) if t0_iso else None,
                clock_scale=clock_scale, tick_sec=tick_sec,
                deliver_nats=not no_nats, deliver_twin=not no_twin and not dry_run,
            )
            if dry_run:
                # build-only: same deterministic tape, zero delivery, zero wait
                from event_emulator.spec import ResolvedRun
                from event_emulator.tape import build_tape

                run = ResolvedRun.resolve(spec, lib[use_case_id])
                tape = build_tape(run, lib[use_case_id], profile_for(use_case_id))
                from event_emulator.runner import RunOutcome, _summarize

                outcome = RunOutcome(
                    run_id=None, status="dry-run", tape=tape,
                    movement_id=tape.movement_id,
                    delivery=_summarize([{"result": "skipped", "kind": "dry-run"}]),
                )
            else:
                outcome = await execute_run(deps, spec, library=lib)
            if tape_out and outcome.tape is not None:
                Path(tape_out).write_text(outcome.tape.to_json(), encoding="utf-8")
            click.echo(
                f"run_id={outcome.run_id} status={outcome.status} "
                f"movement={outcome.movement_id} delivery={outcome.delivery}"
            )
        finally:
            await _aclose(deps, pool)

    asyncio.run(_run())


@cli.command()
@click.argument("run_id", required=False, type=int)
@click.option("--tape", "tape_path", type=click.Path(exists=True), default=None)
@click.option("--t0", "t0_iso", default=None, help="ISO anchor (default: now)")
@click.option("--clock-scale", type=float, default=1.0)
def replay(run_id: int, tape_path: str, t0_iso: str, clock_scale: float) -> None:
    if (run_id is None) == (tape_path is None):
        click.echo("give exactly one of RUN_ID or --tape PATH", err=True)
        raise SystemExit(2)

    async def _run() -> None:
        deps, pool = await _build_deps(dry_run=False)
        replay_of: int | None = None
        try:
            if tape_path is not None:
                tape = CauseTape.from_dict(
                    json.loads(Path(tape_path).read_text(encoding="utf-8"))
                )
            else:
                assert run_id is not None and deps.store is not None
                row = await deps.store.fetch_run(run_id)
                if row is None:
                    click.echo(f"run {run_id} not found", err=True)
                    raise SystemExit(2)
                raw = row["tape"]
                tape = CauseTape.from_dict(raw if isinstance(raw, dict) else json.loads(raw))
                replay_of = run_id
            # the API's rule: a route that is not a path on today's roads
            # would re-animate the event off them — refused, nothing ledgered
            refusal = await replay_refusal(deps.store, tape, replay_of)
            if refusal is not None:
                click.echo(refusal["detail"], err=True)
                raise SystemExit(2)
            outcome = await execute_replay(
                deps, tape=tape, clock_scale=clock_scale, replay_of=replay_of,
                t0=datetime.fromisoformat(t0_iso) if t0_iso else None, library=load_library(),
            )
            click.echo(
                f"run_id={outcome.run_id} (replay of {replay_of}) "
                f"status={outcome.status} movement={outcome.movement_id} "
                f"delivery={outcome.delivery}"
            )
        finally:
            await _aclose(deps, pool)

    asyncio.run(_run())


@cli.command()
@click.argument("run_id", type=int)
def show(run_id: int) -> None:
    settings = get_settings()
    if not settings.database_url:
        click.echo("VVIP_DATABASE_URL is not set — ledger unavailable", err=True)
        raise SystemExit(2)

    async def _run() -> None:
        pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=2)
        await pool.open()
        try:
            store = CorridorStore(pool)
            row = await store.fetch_run(run_id)
            if row is None:
                click.echo(f"run {run_id} not found", err=True)
                raise SystemExit(2)
            for k, v in row.items():
                if isinstance(v, datetime):
                    v = v.isoformat()
                elif isinstance(v, (dict, list)):
                    v = json.dumps(v)[:200]
                elif isinstance(v, str) and len(v) > 200:
                    v = v[:200] + "…"
                click.echo(f"{k:18s} {v}")
        finally:
            await pool.close()

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
