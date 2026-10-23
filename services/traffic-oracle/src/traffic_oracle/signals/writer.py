"""Gated city signal-plan writer (contract C7) + snapshot/restore.

Flow of ONE ``apply`` (every step is auditable and reversible):

  1. FLAG     — the writer is default-OFF.  ``VVIP_SIGNAL_POLICY_WRITER``
                unset/0 ⇒ the report says ``writer_disabled`` and NOTHING
                is written (showcase never sets it, so the twin's
                bit-identical rerun gate is safe by default).
  2. GATE     — acquire the in-process writer-exclusive gate
                (``signals.gate``): no commit may interleave with any
                in-flight twin / shadow-eval read of signal_programs.
  3. PLAN     — load current programs, apply the PURE band→plan table
                (``signals.plans``) — deterministic, documented.
  4. SHADOW   — twin shadow-evaluation of the plan BEFORE any write:
                paired SimulationEngine runs (baseline programs vs
                candidate programs, identical route/seed/policy) gated by
                the SAME ``ShadowEvaluator`` tolerance rule the adaptive
                layer uses.  Any failure ⇒ fail-closed, nothing written.
  5. COMMIT   — single transaction: the FIRST calibrated write per
                junction snapshots the prior program into
                ``corridor.signal_program_history`` (kind='snapshot'),
                then upserts the calibrated program, then appends ONE
                ``corridor.signal_plan_log`` row for the apply (013).
  6. REPORT   — the full decision (band, junctions, shadow outcome) is
                returned AND kept in-process for GET /signals/plans.

``restore`` rebuilds ``osm_assumed`` rows from the earliest snapshot per
junction and appends kind='restore' audit rows — demo state pollution is
reversible by construction.

Attribution (migration 013): every signal write is a row carrying
``actor``, the named officer who triggered it (NULL when none was given
and VVIP_REQUIRE_OFFICER is unset; never a guessed name) — each committed
apply appends one ``signal_plan_log`` row (band, junctions, snapshots,
shadow verdict), its first-write snapshot rows carry the same actor, and
so does every restore row.  A re-apply over already-snapshotted junctions
is therefore attributed too, not only the first write.

SQL hygiene (house style, cf. adaptive/persistence.py and
events/__init__.py): every WRITE statement is a LOCAL variable assigned a
single-line string literal at its call site, with $n placeholders only —
values are passed positionally, never interpolated, never composed from
request data.  Read statements are module-level single-line constants
with the same parameter discipline.

Security: this module makes no outbound HTTP calls.  The shadow-eval
engine runs in-process (pure CPU), exactly like the twin router.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import structlog

from traffic_oracle.adaptive.enums import ShadowEvalStatus
from traffic_oracle.adaptive.shadow import ShadowEvaluator, TwinRun
from traffic_oracle.signals.gate import signal_policy_gate
from traffic_oracle.signals.plans import (
    JunctionProgram,
    plan_city,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Flag (default OFF)
# ---------------------------------------------------------------------------

WRITER_FLAG_ENV = "VVIP_SIGNAL_POLICY_WRITER"
_TRUTHY = {"1", "true", "yes", "on"}

# Fixed audit-reason literals (constants — audit text is never composed
# from request data).
_REASON_FIRST_WRITE = "first calibrated write (city signal plan)"
_REASON_RESTORE = "restore to pre-calibration snapshot"


def writer_enabled() -> bool:
    """VVIP_SIGNAL_POLICY_WRITER unset/0 ⇒ the writer refuses to write."""
    return os.environ.get(WRITER_FLAG_ENV, "").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Static, parameter-bound READ SQL — one-line constants.
# ---------------------------------------------------------------------------

_SQL_LOAD_PROGRAMS = "SELECT sp.junction_id, sp.cycle_sec, sp.phases, sp.offset_sec, sp.source FROM corridor.signal_programs sp ORDER BY sp.junction_id"  # noqa: E501

_SQL_LOAD_JUNCTION_GEOMS = "SELECT j.junction_id, ST_X(j.geom) AS lon, ST_Y(j.geom) AS lat FROM corridor.junctions j WHERE j.junction_id = ANY($1::bigint[])"  # noqa: E501

_SQL_LOAD_ADJACENCY = "SELECT DISTINCT sa.from_segment_id AS segment_id, sa.via_junction_id AS junction_id FROM corridor.segment_adjacency sa"  # noqa: E501

_SQL_HISTORY_JUNCTIONS = "SELECT DISTINCT junction_id FROM corridor.signal_program_history WHERE junction_id = ANY($1::bigint[])"  # noqa: E501

_SQL_RESTORE_TARGETS_ALL = "SELECT DISTINCT ON (h.junction_id) h.junction_id, h.cycle_sec, h.phases, h.offset_sec, h.source, h.event_id FROM corridor.signal_program_history h WHERE h.kind = 'snapshot' ORDER BY h.junction_id, h.captured_at ASC"  # noqa: E501

_SQL_RESTORE_TARGETS_EVENT = "SELECT DISTINCT ON (h.junction_id) h.junction_id, h.cycle_sec, h.phases, h.offset_sec, h.source, h.event_id FROM corridor.signal_program_history h WHERE h.kind = 'snapshot' AND h.event_id = $1::uuid ORDER BY h.junction_id, h.captured_at ASC"  # noqa: E501

_SQL_HISTORY_LIST = "SELECT h.history_id, h.junction_id, h.event_id, h.kind, h.cycle_sec, h.phases, h.offset_sec, h.source, h.reason, h.actor, h.captured_at FROM corridor.signal_program_history h ORDER BY h.captured_at DESC LIMIT $1"  # noqa: E501


# ---------------------------------------------------------------------------
# Program loading / overrides math
# ---------------------------------------------------------------------------


def _parse_phases(raw: Any) -> list[dict]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            return []
    if isinstance(raw, list):
        return [dict(p) for p in raw if isinstance(p, dict)]
    return []


#: Public name for the program-phases parser (the city fusion snapshot
#: renders junction phases exactly like GET /signals/state does).
parse_phases = _parse_phases


async def load_programs(pool: Any) -> list[JunctionProgram]:
    rows = await pool.fetch(_SQL_LOAD_PROGRAMS)
    return [
        JunctionProgram(
            junction_id=int(r["junction_id"]),
            cycle_sec=int(r["cycle_sec"]),
            phases=_parse_phases(r["phases"]),
            offset_sec=int(r["offset_sec"] or 0),
            source=str(r["source"]),
        )
        for r in rows
    ]


def overrides_from_programs(
    programs: Sequence[JunctionProgram], adjacency_rows: Iterable[Any]
) -> dict[int, dict]:
    """Per-segment green-ratio overrides (calibration-parity math, pure).

    Mirrors ``simulation/calibration.load_signal_overrides`` — Σgreen/cycle
    clamped to [0.05, 0.95], mapped onto segments through
    ``segment_adjacency.via_junction_id`` — but computed from IN-MEMORY
    programs so the shadow gate can evaluate a NOT-YET-WRITTEN candidate.
    """
    green_by_junction: dict[int, tuple[float, int]] = {}
    for prog in programs:
        if prog.cycle_sec <= 0:
            continue
        green = sum(float(p.get("green_sec", 0) or 0) for p in prog.phases)
        green_by_junction[prog.junction_id] = (
            max(0.05, min(0.95, green / prog.cycle_sec)),
            prog.cycle_sec,
        )
    overrides: dict[int, dict] = {}
    for row in adjacency_rows:
        jid = int(row["junction_id"])
        if jid in green_by_junction:
            ratio, cycle = green_by_junction[jid]
            overrides[int(row["segment_id"])] = {
                "green_ratio": ratio,
                "cycle_sec": cycle,
                "source": "shadow",
            }
    return overrides


# ---------------------------------------------------------------------------
# Shadow evaluation (paired in-process twin runs, fail-closed)
# ---------------------------------------------------------------------------

_SHADOW_SEED = 20260101
_SHADOW_REPLICATIONS = 5
_SHADOW_HORIZON_MIN = 30


@dataclass(frozen=True)
class ShadowReport:
    status: str          # 'passed' | 'unavailable'
    detail: str
    baseline_vh: float | None = None
    candidate_vh: float | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "detail": self.detail,
            "baseline_disruption_vh": self.baseline_vh,
            "candidate_disruption_vh": self.candidate_vh,
        }


def _run_arm(network: Any, route_segment_ids: list[int], departure: datetime, policy: Any) -> TwinRun:  # noqa: E501
    """One twin arm over a prepared network (in-process, pure CPU).

    ``calibrated`` carries the twin's REAL process-wide calibration state
    (simulation.calibration.CALIBRATION, set by POST /simulate/replay) so
    the C4 shadow gate applies its warmup fail-closed posture verbatim:
    an uncalibrated twin refuses every city-plan publish.
    """
    from traffic_oracle.simulation.calibration import CALIBRATION
    from traffic_oracle.simulation.engine import SimulationEngine

    engine = SimulationEngine(network=network)
    results = engine.run(
        route_segment_ids=route_segment_ids,
        departure_time=departure,
        closure_policy=policy,
        horizon_min=_SHADOW_HORIZON_MIN,
        n_replications=_SHADOW_REPLICATIONS,
        seed=_SHADOW_SEED,
    )
    n = max(len(results), 1)
    return TwinRun(
        total_disruption_vehicle_hours=sum(
            r.total_disruption_vehicle_hours for r in results
        )
        / n,
        calibrated=bool(CALIBRATION.calibrated),
        is_stub=False,
        ambulance_delay_sec=sum(r.ambulance_delay_sec for r in results) / n,
        convoy_transit_time_sec=sum(
            r.convoy_transit_time_sec for r in results
        )
        / n,
    )


def shadow_evaluate_plan(
    baseline_programs: Sequence[JunctionProgram],
    candidate_programs: Sequence[JunctionProgram],
    adjacency_rows: Sequence[Any],
    route_segment_ids: Sequence[int],
) -> tuple[bool, ShadowReport]:
    """Fail-closed shadow gate for a city plan; True ⇔ plan may commit.

    Deterministic twin discipline: identical route, departure, policy,
    seed and replication count for both arms; only the signal programs
    differ.  Any exception ⇒ ``unavailable`` (fail-closed, no write).
    """
    if not route_segment_ids:
        return False, ShadowReport(
            "unavailable", "no route segments to shadow-evaluate"
        )
    try:
        from traffic_oracle.simulation.calibration import (
            apply_signal_overrides,
        )
        from traffic_oracle.simulation.engine import ClosurePolicy
        from traffic_oracle.simulation.network import default_network

        merged: dict[int, JunctionProgram] = {
            p.junction_id: p for p in baseline_programs
        }
        for p in candidate_programs:
            merged[p.junction_id] = p

        base_net = default_network()
        cand_net = default_network()
        apply_signal_overrides(
            base_net, overrides_from_programs(baseline_programs, adjacency_rows)
        )
        apply_signal_overrides(
            cand_net, overrides_from_programs(list(merged.values()), adjacency_rows)
        )

        departure = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=1)  # noqa: E501
        policy = ClosurePolicy(advance_closure_sec=120, closure_type="signal_hold")
        route = [int(s) for s in route_segment_ids]
        baseline_run = _run_arm(base_net, route, departure, policy)
        candidate_run = _run_arm(cand_net, route, departure, policy)
    except Exception as exc:  # noqa: BLE001 — any failure fails closed
        return False, ShadowReport("unavailable", repr(exc))

    evaluator = ShadowEvaluator()
    outcome = evaluator.evaluate(
        candidate_params=None,   # type: ignore[arg-type] — plan gate, not param gate
        baseline_run=baseline_run,
        candidate_run=candidate_run,
    )
    base_vh = round(baseline_run.total_disruption_vehicle_hours, 4)
    cand_vh = round(candidate_run.total_disruption_vehicle_hours, 4)
    if outcome.status is ShadowEvalStatus.PASSED:
        return True, ShadowReport("passed", outcome.detail, base_vh, cand_vh)
    return False, ShadowReport("unavailable", outcome.detail, base_vh, cand_vh)


# ---------------------------------------------------------------------------
# The writer (apply) — flag → gate → plan → shadow → commit
# ---------------------------------------------------------------------------

# In-process last-plan report (surfaced by GET /api/v1/signals/plans).
_LAST_PLAN: dict[str, Any] | None = None


def last_plan_report() -> dict[str, Any] | None:
    return _LAST_PLAN


def _default_route(
    adjacency_rows: Sequence[Any], junction_ids: Sequence[int]
) -> list[int]:
    """Deterministic shadow route: sorted adjacent segments of the planned
    junctions (capped at 16 for twin cost)."""
    wanted = set(int(j) for j in junction_ids)
    segs = sorted(
        {
            int(r["segment_id"])
            for r in adjacency_rows
            if int(r["junction_id"]) in wanted
        }
    )
    return segs[:16]


async def apply_city_plan(
    pool: Any,
    *,
    band: str,
    event_id: str | None = None,
    junction_ids: Sequence[int] | None = None,
    route_segment_ids: Sequence[int] | None = None,
    writer_flag_value: bool | None = None,
    gate: Any = None,
    shadow_fn: Any = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Apply one band→city-plan.  See the module docstring for the gate
    chain.  ``shadow_fn``/``gate`` injectable for tests; production uses
    the module defaults.  ``actor`` (the named officer) is recorded on
    this apply's ``signal_plan_log`` row and on the snapshot rows it
    writes.

    Returns the plan report (also stored as the last-plan report).  When
    the writer is disabled or the shadow gate fails, NOTHING is written
    and the report says so — callers surface it verbatim.
    """
    global _LAST_PLAN

    enabled = writer_enabled() if writer_flag_value is None else writer_flag_value
    if not enabled:
        report = {
            "applied": False,
            "reason": "writer_disabled",
            "band": band,
            "actor": actor,
            "flag_env": WRITER_FLAG_ENV,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        _LAST_PLAN = report
        return report

    use_gate = gate or signal_policy_gate
    async with use_gate.writer_exclusive():
        programs = await load_programs(pool)
        if junction_ids:
            wanted = {int(j) for j in junction_ids}
            programs = [p for p in programs if p.junction_id in wanted]
        adjacency_rows = await pool.fetch(_SQL_LOAD_ADJACENCY)

        planned = plan_city(programs, band)
        report: dict[str, Any] = {
            "applied": False,
            "band": band,
            "event_id": event_id,
            "actor": actor,
            "planned_junctions": [p.junction_id for p in planned],
            "at": datetime.now(timezone.utc).isoformat(),
        }
        if not planned:
            report["reason"] = "no_junctions_changed"
            _LAST_PLAN = report
            return report

        if route_segment_ids:
            route = [int(s) for s in route_segment_ids][:16]
        else:
            route = _default_route(
                adjacency_rows, [p.junction_id for p in planned]
            )
        shadow_fn = shadow_fn or shadow_evaluate_plan
        # Shadow arms: baseline = current programs; candidate = current with
        # planned junctions replaced (deterministic merge, same order).
        merged = {p.junction_id: p for p in programs}
        for p in planned:
            merged[p.junction_id] = p
        passed, shadow = shadow_fn(
            list(programs), list(merged.values()), adjacency_rows, route
        )
        report["shadow_eval"] = shadow.to_dict()
        report["shadow_route_segment_ids"] = route
        if not passed:
            report["reason"] = "shadow_eval_failed_closed"
            logger.warning(
                "signals.city_plan_fail_closed",
                band=band,
                detail=shadow.detail,
            )
            _LAST_PLAN = report
            return report

        # Write statements: LOCAL single-line literals, $n params only
        # (house style — see module docstring).
        snapshot_insert = "INSERT INTO corridor.signal_program_history (junction_id, event_id, kind, cycle_sec, phases, offset_sec, source, reason, actor) VALUES ($1, $2, 'snapshot', $3, $4::jsonb, $5, $6, $7, $8)"  # noqa: E501
        program_upsert = "INSERT INTO corridor.signal_programs (junction_id, cycle_sec, phases, offset_sec, source) VALUES ($1, $2, $3::jsonb, $4, $5) ON CONFLICT (junction_id) DO UPDATE SET cycle_sec = EXCLUDED.cycle_sec, phases = EXCLUDED.phases, offset_sec = EXCLUDED.offset_sec, source = EXCLUDED.source, updated_at = NOW()"  # noqa: E501
        plan_log_insert = "INSERT INTO corridor.signal_plan_log (event_id, band, junction_ids, snapshots_written, shadow_eval, actor) VALUES ($1, $2, $3::bigint[], $4, $5, $6)"  # noqa: E501

        planned_ids = [p.junction_id for p in planned]
        current_by_junction = {p.junction_id: p for p in programs}
        snapshots_written = 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing_rows = await conn.fetch(
                    _SQL_HISTORY_JUNCTIONS, planned_ids
                )
                existing = {int(r["junction_id"]) for r in existing_rows}
                for prog in planned:
                    prior = current_by_junction.get(prog.junction_id)
                    new_phases_json = json.dumps(prog.phases)
                    if prog.junction_id not in existing and prior is not None:
                        prior_phases_json = json.dumps(prior.phases)
                        await conn.execute(
                            snapshot_insert,
                            prog.junction_id,
                            event_id,
                            prior.cycle_sec,
                            prior_phases_json,
                            prior.offset_sec,
                            prior.source,
                            _REASON_FIRST_WRITE,
                            actor,
                        )
                        snapshots_written += 1
                    await conn.execute(
                        program_upsert,
                        prog.junction_id,
                        prog.cycle_sec,
                        new_phases_json,
                        prog.offset_sec,
                        "calibrated",
                    )
                # The apply's own ledger row — in the SAME transaction, so a
                # program write never exists without its attributed row.
                await conn.execute(
                    plan_log_insert,
                    event_id,
                    band,
                    planned_ids,
                    snapshots_written,
                    shadow.status,
                    actor,
                )

        report["applied"] = True
        report["reason"] = "committed"
        report["snapshots_written"] = snapshots_written
        report["gate"] = use_gate.snapshot()
        logger.info(
            "signals.city_plan_committed",
            band=band,
            junctions=len(planned),
            snapshots=snapshots_written,
            shadow=shadow.status,
            actor=actor,
        )
        _LAST_PLAN = report
        return report


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


async def restore_programs(
    pool: Any,
    *,
    event_id: str | None = None,
    restore_all: bool = False,
    actor: str | None = None,
) -> dict[str, Any]:
    """Rebuild ``osm_assumed`` programs from the earliest snapshot per
    junction (``event_id`` scopes to that event's first-write snapshots;
    ``restore_all`` to every snapshotted junction) and append
    kind='restore' audit rows attributed to ``actor``.  Returns
    per-junction outcomes.
    """
    if not restore_all and not event_id:
        return {"error": "specify event_id or all=true"}

    program_upsert = "INSERT INTO corridor.signal_programs (junction_id, cycle_sec, phases, offset_sec, source) VALUES ($1, $2, $3::jsonb, $4, $5) ON CONFLICT (junction_id) DO UPDATE SET cycle_sec = EXCLUDED.cycle_sec, phases = EXCLUDED.phases, offset_sec = EXCLUDED.offset_sec, source = EXCLUDED.source, updated_at = NOW()"  # noqa: E501
    restore_marker = "INSERT INTO corridor.signal_program_history (junction_id, event_id, kind, cycle_sec, phases, offset_sec, source, reason, actor) VALUES ($1, $2, 'restore', $3, $4::jsonb, $5, $6, $7, $8)"  # noqa: E501

    async with signal_policy_gate.writer_exclusive():
        async with pool.acquire() as conn:
            if restore_all:
                rows = await conn.fetch(_SQL_RESTORE_TARGETS_ALL)
            else:
                rows = await conn.fetch(_SQL_RESTORE_TARGETS_EVENT, event_id)
            restored: list[dict] = []
            async with conn.transaction():
                for r in rows:
                    phases = _parse_phases(r["phases"])
                    phases_json = json.dumps(phases)
                    junction_id = int(r["junction_id"])
                    cycle_sec = int(r["cycle_sec"])
                    offset_sec = int(r["offset_sec"] or 0)
                    await conn.execute(
                        program_upsert,
                        junction_id,
                        cycle_sec,
                        phases_json,
                        offset_sec,
                        "osm_assumed",
                    )
                    await conn.execute(
                        restore_marker,
                        junction_id,
                        r["event_id"],
                        cycle_sec,
                        phases_json,
                        offset_sec,
                        "osm_assumed",
                        _REASON_RESTORE,
                        actor,
                    )
                    restored.append(
                        {
                            "junction_id": junction_id,
                            "cycle_sec": cycle_sec,
                            "source": "osm_assumed",
                        }
                    )
    logger.info(
        "signals.restored", junctions=len(restored), event_id=event_id, actor=actor
    )
    return {
        "restored": len(restored),
        "junctions": restored,
        "event_id": event_id,
        "all": restore_all,
        "actor": actor,
    }


async def list_history(pool: Any, limit: int = 50) -> list[dict]:
    rows = await pool.fetch(_SQL_HISTORY_LIST, max(1, min(int(limit), 200)))
    out: list[dict] = []
    for r in rows:
        phases = _parse_phases(r["phases"])
        out.append(
            {
                "history_id": int(r["history_id"]),
                "junction_id": int(r["junction_id"]),
                "event_id": str(r["event_id"]) if r["event_id"] else None,
                "kind": r["kind"],
                "cycle_sec": int(r["cycle_sec"]),
                "phases": phases,
                "offset_sec": int(r["offset_sec"] or 0),
                "source": r["source"],
                "reason": r["reason"],
                "actor": r["actor"],
                "captured_at": r["captured_at"].isoformat(),
            }
        )
    return out


__all__ = [
    "WRITER_FLAG_ENV",
    "apply_city_plan",
    "last_plan_report",
    "list_history",
    "load_programs",
    "overrides_from_programs",
    "parse_phases",
    "restore_programs",
    "shadow_evaluate_plan",
    "writer_enabled",
]
