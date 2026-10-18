"""PURE deterministic band → city signal-plan mapping (contract C7).

This module is I/O-free and side-effect-free: every function is a total
function of its inputs, which is what makes the city-plan writer
auditable — the same (junction program, band) pair always yields the same
candidate program, and the table below is the single documented source.

Scope ruling (mandate): this extends the adaptive layer's POLICY
APPLICATION POINTS from corridor-scope to city-scope.  The per-junction
reinforcement learner (``optimize/rl``) is untouched — it stays
corridor-scoped; this module never reads or writes its state.

────────────────────────────────────────────────────────────────────────
BAND → PLAN TABLE (the deterministic mapping, documented per C7)
────────────────────────────────────────────────────────────────────────

For each programmed junction, the plan keeps ``cycle_sec`` and
``offset_sec`` UNCHANGED (so the 006 CHECKs and progression offsets are
trivially preserved) and adjusts only the green SPLIT:

  band   | priority_share (fraction   | cross_green_floor_sec
         | of cycle transferred to    |
         | the priority phase)        |
  -------+----------------------------+----------------------
  GREEN  | 0.00  (no plan is written) |    —
  AMBER  | 0.06                       |   18
  RED    | 0.12                       |   14
  BLACK  | 0.18                       |   10

Mechanics per junction (deterministic given (program, band)):

1. The priority phase is the FIRST phase of the program (the seed's
   main/through movement — 006 always seeds it first).
2. ``transfer_sec = round(cycle_sec * priority_share)``.
3. ``transfer_sec`` is taken from the non-priority phases, each bounded
   below by ``cross_green_floor_sec``; phases already at/below the floor
   contribute nothing (the plan never removes a phase's minimum green).
4. Whatever was actually collected is added to the priority phase.
5. Junctions whose program cannot change (single-phase, or every
   non-priority phase already at its floor) are SKIPPED — the writer
   only commits junctions whose plan actually differs.

Cross-traffic hold (pursuit / BLACK): the same mechanism at the maximum
share with the minimum floor — a BLACK plan drives cross phases to their
10 s floor at interception junctions, which IS the "hold cross-traffic"
posture, expressed as a bounded, reversible split change rather than an
unsafe infinite red.

``active_phase`` / ``phase_timeline``: deterministic phase-in-effect
function of (epoch seconds, cycle, offset, phases) — the twin's
clock discipline, reused by GET /api/v1/signals/state so the canvas
renders the same phase the simulator would.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from traffic_oracle.adaptive.enums import SituationClass

# ---------------------------------------------------------------------------
# The documented band → plan table (single source of truth for C7).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BandPlanSpec:
    """One row of the band→plan table."""

    band: str
    priority_share: float          # fraction of cycle moved to priority phase
    cross_green_floor_sec: int     # minimum green any non-priority phase keeps
    writes: bool                   # GREEN plans nothing (writer no-ops)

    def to_dict(self) -> dict:
        return {
            "band": self.band,
            "priority_share": self.priority_share,
            "cross_green_floor_sec": self.cross_green_floor_sec,
            "writes": self.writes,
        }


BAND_PLAN_TABLE: dict[str, BandPlanSpec] = {
    "GREEN": BandPlanSpec("GREEN", 0.00, 30, False),
    "AMBER": BandPlanSpec("AMBER", 0.06, 18, True),
    "RED": BandPlanSpec("RED", 0.12, 14, True),
    "BLACK": BandPlanSpec("BLACK", 0.18, 10, True),
}

# ---------------------------------------------------------------------------
# City band aggregation
# ---------------------------------------------------------------------------


def city_band_from_status(movements: Mapping[str, Mapping]) -> str:
    """City situation band = the MOST SEVERE band across active subjects.

    Pure: takes the ``movements`` dict of ``GET /api/v1/adaptive/status``
    (``{id: {effective_class|situation_code|vvip_class: ...}}``) and
    returns the max-band code.  Unknown/missing entries are ignored —
    never fabricated.  No subjects → 'GREEN' (the city is at rest).
    """
    best: SituationClass | None = None
    for state in movements.values():
        if not isinstance(state, Mapping):
            continue
        code = state.get("effective_class") or state.get("situation_code")
        if not isinstance(code, str):
            continue
        try:
            band = SituationClass.from_code(code)
        except ValueError:
            continue
        if best is None or band.rank > best.rank:  # type: ignore[attr-defined]
            best = band  # type: ignore[assignment]
    return (best or SituationClass.GREEN).code


# ---------------------------------------------------------------------------
# Program math (pure)
# ---------------------------------------------------------------------------


def _phases(raw: object) -> list[dict]:
    """Coerce a program's ``phases`` JSONB (list | JSON str) to list[dict]."""
    if isinstance(raw, (str, bytes)):
        import json

        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            return []
    if not isinstance(raw, Sequence):
        return []
    out: list[dict] = []
    for p in raw:
        if isinstance(p, dict):
            out.append(dict(p))
    return out


def green_ratio(phases: object, cycle_sec: int) -> float:
    """Σgreen_sec / cycle_sec, clamped to [0.05, 0.95] (calibration parity)."""
    greens = sum(
        float(p.get("green_sec", 0) or 0) for p in _phases(phases)
    )
    if cycle_sec <= 0:
        return 0.0
    return max(0.05, min(0.95, greens / cycle_sec))


def phase_timeline(phases: object, cycle_sec: int, offset_sec: int) -> list[dict]:
    """Expand phases onto [0, cycle) with start/end offsets applied.

    Deterministic: position k in the list starts at
    (offset + Σ green[0..k)) mod cycle and runs green_sec.  The remainder
    of the cycle after the last phase is unallocated lost time (amber/
    all-red), matching the 006 seed discipline (e.g. cycle 110 = 50+42
    greens + 18 s lost time).
    """
    ps = _phases(phases)
    timeline: list[dict] = []
    cursor = int(offset_sec) % int(cycle_sec) if cycle_sec > 0 else 0
    for idx, p in enumerate(ps):
        green = max(0, int(p.get("green_sec", 0) or 0))
        timeline.append(
            {
                "index": idx,
                "movements": list(p.get("movements", []) or []),
                "start_sec": cursor,
                "end_sec": cursor + green,
                "green_sec": green,
            }
        )
        cursor += green
    return timeline


def active_phase(
    phases: object, cycle_sec: int, offset_sec: int, epoch_sec: float
) -> dict:
    """Phase in effect at ``epoch_sec`` — the twin's clock discipline.

    The cycle position is ``floor(epoch) mod cycle``; the phase whose
    [start, end) window (from ``phase_timeline``) contains it wins; a
    position in lost time returns the LAST phase with green_sec > 0
    (amber/all-red trails the preceding green), or a synthetic empty
    phase when the program has no greens at all.
    """
    if cycle_sec <= 0:
        return {"index": None, "movements": [], "green_sec": 0, "lost": True}
    pos = int(epoch_sec) % int(cycle_sec)
    timeline = phase_timeline(phases, cycle_sec, offset_sec)
    greens = [t for t in timeline if t["green_sec"] > 0]
    if not greens:
        return {"index": None, "movements": [], "green_sec": 0, "lost": True}
    for t in timeline:
        if t["green_sec"] > 0 and t["start_sec"] <= pos < t["end_sec"]:
            return {
                "index": t["index"],
                "movements": t["movements"],
                "green_sec": t["green_sec"],
                "lost": False,
            }
    last = greens[-1]
    return {
        "index": last["index"],
        "movements": last["movements"],
        "green_sec": last["green_sec"],
        "lost": True,
    }


# ---------------------------------------------------------------------------
# Band → candidate program (the plan)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JunctionProgram:
    """One corridor.signal_programs row (values only, no DB types)."""

    junction_id: int
    cycle_sec: int
    phases: list[dict]
    offset_sec: int
    source: str

    def clone_with(self, *, phases: list[dict], source: str) -> "JunctionProgram":
        return JunctionProgram(
            junction_id=self.junction_id,
            cycle_sec=self.cycle_sec,
            phases=phases,
            offset_sec=self.offset_sec,
            source=source,
        )


def plan_for_junction(
    program: JunctionProgram, band: str
) -> JunctionProgram | None:
    """Apply the band→plan table to ONE junction; None = no change.

    Deterministic: depends only on (program, band).  Never violates the
    006 CHECKs (cycle unchanged; phases stays a non-empty array) and
    never pushes a non-priority phase below the band's cross floor.
    """
    spec = BAND_PLAN_TABLE.get(band)
    if spec is None or not spec.writes:
        return None
    phases = _phases(program.phases)
    if len(phases) < 2:
        # Single-phase (or empty) programs have no split to shift.
        return None

    transfer_total = int(round(program.cycle_sec * spec.priority_share))
    if transfer_total <= 0:
        return None

    new_phases = [dict(p) for p in phases]
    collected = 0
    # Non-priority phases surrender green down to (never below) the floor,
    # in reverse order so the LAST (cross) phase yields first — the
    # "take from cross traffic" posture, deterministic by phase index.
    for idx in range(len(new_phases) - 1, 0, -1):
        if collected >= transfer_total:
            break
        green = int(new_phases[idx].get("green_sec", 0) or 0)
        giveable = green - spec.cross_green_floor_sec
        if giveable <= 0:
            continue
        give = min(giveable, transfer_total - collected)
        new_phases[idx]["green_sec"] = green - give
        collected += give
    if collected <= 0:
        return None

    priority_green = int(new_phases[0].get("green_sec", 0) or 0)
    new_phases[0]["green_sec"] = priority_green + collected

    if new_phases == phases:
        return None
    return program.clone_with(phases=new_phases, source="calibrated")


def plan_city(
    programs: Iterable[JunctionProgram], band: str
) -> list[JunctionProgram]:
    """Plan every junction for a band; only CHANGING junctions returned."""
    planned = (plan_for_junction(p, band) for p in programs)
    return [p for p in planned if p is not None]


def band_plan_table_doc() -> list[dict]:
    """The documented table as JSON (surfaced by GET /api/v1/signals/plans)."""
    return [spec.to_dict() for spec in BAND_PLAN_TABLE.values()]


__all__ = [
    "BAND_PLAN_TABLE",
    "BandPlanSpec",
    "JunctionProgram",
    "active_phase",
    "band_plan_table_doc",
    "city_band_from_status",
    "green_ratio",
    "phase_timeline",
    "plan_city",
    "plan_for_junction",
]
