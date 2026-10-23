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

``phase_at``: THE shared phase clock — one pure function of (program,
Unix epoch seconds).  ``pos = (floor(epoch) - offset) mod cycle``, then
the phases are walked back-to-back from position 0; whatever the greens
leave over at the end of the cycle is lost time (amber/all-red).  Both
consumers call it: GET /api/v1/signals/state (the canvas ring, through
``active_phase``) and the e-challan engine (``enforcement/rules.py``
``phase_state_at``), so the ring an operator sees and the signal state a
recommendation cites can never disagree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

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
    """Coerce a program's ``phases`` to list[dict].

    Accepts the 006 JSONB shape (list of ``{green_sec, movements}`` or its
    JSON text) and the rules engine's frozen ``((green_sec, movements),)``
    tuples, so every caller feeds the same clock.
    """
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
        elif (
            isinstance(p, Sequence)
            and not isinstance(p, (str, bytes))
            and len(p) == 2
        ):
            green, movements = p
            out.append({"green_sec": green, "movements": list(movements or ())})
    return out


def green_ratio(phases: object, cycle_sec: int) -> float:
    """Σgreen_sec / cycle_sec, clamped to [0.05, 0.95] (calibration parity)."""
    greens = sum(
        float(p.get("green_sec", 0) or 0) for p in _phases(phases)
    )
    if cycle_sec <= 0:
        return 0.0
    return max(0.05, min(0.95, greens / cycle_sec))


#: Amber (yellow) interval after every green, seconds — the IRC:93 urban
#: signal practice.  The seeded programs carry only greens plus a lump of
#: lost time, so the amber is modelled as the first AMBER_SEC after a
#: movement's green ends: neither green nor red.  The e-challan engine
#: never recommends a red-light challan inside it (benefit of the doubt).
AMBER_SEC = 3


@dataclass(frozen=True)
class PhaseState:
    """What a junction's program shows at one instant (``phase_at``).

    ``index``/``movements``/``green_sec`` name the phase in effect; inside
    the trailing lost time they name the phase whose green just ended
    (amber/all-red trails the preceding green) and ``lost`` is True.
    ``movement_states`` maps EVERY movement label the program defines to
    green | amber | red; a label the program does not define has no state
    at all (``state_of`` -> 'none') — never an implied red.
    """

    index: int | None
    movements: tuple[str, ...]
    green_sec: int
    lost: bool
    sec_into_cycle: int
    remaining_sec: int
    cycle_sec: int
    movement_states: Mapping[str, str] = field(default_factory=dict)

    def state_of(self, movement: str) -> str:
        """green | amber | red for a defined movement, 'none' otherwise."""
        return self.movement_states.get(movement, "none")

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "movements": list(self.movements),
            "green_sec": self.green_sec,
            "lost": self.lost,
            "sec_into_cycle": self.sec_into_cycle,
            "remaining_sec": self.remaining_sec,
            "movement_states": dict(self.movement_states),
        }


def _program_fields(program: object) -> tuple[int, int, list[dict]]:
    """(cycle_sec, offset_sec, phases) from a mapping or an attribute object
    (JunctionProgram, the rules engine's SignalContext, a DB row dict)."""
    if isinstance(program, Mapping):
        cycle = program.get("cycle_sec", 0)
        offset = program.get("offset_sec", 0)
        phases: Any = program.get("phases", [])
    else:
        cycle = getattr(program, "cycle_sec", 0)
        offset = getattr(program, "offset_sec", 0)
        phases = getattr(program, "phases", [])
    return int(cycle or 0), int(offset or 0), _phases(phases)


def phase_timeline(phases: object, cycle_sec: int) -> list[dict]:
    """The program's green windows in CYCLE-POSITION time.

    Position 0 is the start of phase 0; phase k runs
    [Σ green[0..k), Σ green[0..k] ) and the remainder of the cycle after
    the last green is unallocated lost time (amber/all-red), matching the
    006 seed discipline (e.g. cycle 110 = 50 + 42 greens + 18 s lost).
    The junction offset is NOT in these windows: it only shifts the
    epoch -> position mapping (``phase_at``), so no window ever wraps.
    """
    timeline: list[dict] = []
    cursor = 0
    for idx, p in enumerate(_phases(phases)):
        green = max(0, int(p.get("green_sec", 0) or 0))
        timeline.append(
            {
                "index": idx,
                "movements": [str(m) for m in (p.get("movements", []) or [])],
                "start_sec": cursor,
                "end_sec": cursor + green,
                "green_sec": green,
            }
        )
        cursor += green
    if cycle_sec > 0:
        # an over-allocated program (Σgreen > cycle) is clipped to the cycle
        for t in timeline:
            t["start_sec"] = min(t["start_sec"], cycle_sec)
            t["end_sec"] = min(t["end_sec"], cycle_sec)
            t["green_sec"] = t["end_sec"] - t["start_sec"]
    return timeline


def phase_at(program: object, epoch_s: float) -> PhaseState:
    """THE phase clock: the program's state at Unix time ``epoch_s`` (pure).

    ``pos = (floor(epoch_s) - offset_sec) mod cycle_sec``; the phases are
    walked back-to-back from position 0 and the phase whose green window
    holds ``pos`` is in effect.  Past the last green the junction is in
    lost time.  Per movement: green while a phase listing it is in effect,
    amber for AMBER_SEC after one of its greens ends, red otherwise.

    The SAME function feeds the canvas ring (GET /signals/state through
    ``active_phase``) and the e-challan engine (``phase_state_at``), so
    the display and the evidence cannot disagree.  A degenerate program
    (cycle <= 0 or no green at all) defines no movement state — callers
    then make no signal decision.
    """
    cycle, offset, phases = _program_fields(program)
    timeline = phase_timeline(phases, cycle)
    greens = [t for t in timeline if t["green_sec"] > 0]
    if cycle <= 0 or not greens:
        return PhaseState(
            index=None, movements=(), green_sec=0, lost=True,
            sec_into_cycle=0, remaining_sec=0, cycle_sec=max(cycle, 0),
        )
    pos = (math.floor(epoch_s) - offset) % cycle

    active = None
    for t in greens:
        if t["start_sec"] <= pos < t["end_sec"]:
            active = t
            break

    labels: list[str] = []
    for t in timeline:
        for m in t["movements"]:
            if m not in labels:
                labels.append(m)
    states: dict[str, str] = {}
    for m in labels:
        if active is not None and m in active["movements"]:
            states[m] = "green"
            continue
        in_amber = any(
            (pos - t["end_sec"]) % cycle < AMBER_SEC
            for t in greens
            if m in t["movements"]
        )
        states[m] = "amber" if in_amber else "red"

    if active is not None:
        return PhaseState(
            index=active["index"],
            movements=tuple(active["movements"]),
            green_sec=active["green_sec"],
            lost=False,
            sec_into_cycle=pos,
            remaining_sec=active["end_sec"] - pos,
            cycle_sec=cycle,
            movement_states=states,
        )
    last = greens[-1]
    return PhaseState(
        index=last["index"],
        movements=tuple(last["movements"]),
        green_sec=last["green_sec"],
        lost=True,
        sec_into_cycle=pos,
        remaining_sec=cycle - pos,
        cycle_sec=cycle,
        movement_states=states,
    )


def active_phase(
    phases: object, cycle_sec: int, offset_sec: int, epoch_sec: float
) -> dict:
    """Phase in effect at ``epoch_sec`` for GET /signals/state (C1 shape).

    A thin view over ``phase_at`` — the keys index / movements / green_sec
    / lost keep their contract meaning (lost time names the phase whose
    green just ended); sec_into_cycle, remaining_sec and movement_states
    are additive so the canvas can draw the same green/amber/red the
    e-challan engine evaluates.
    """
    return phase_at(
        {"cycle_sec": cycle_sec, "offset_sec": offset_sec, "phases": phases},
        epoch_sec,
    ).to_dict()


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
    "AMBER_SEC",
    "BAND_PLAN_TABLE",
    "BandPlanSpec",
    "JunctionProgram",
    "PhaseState",
    "active_phase",
    "band_plan_table_doc",
    "city_band_from_status",
    "green_ratio",
    "phase_at",
    "phase_timeline",
    "plan_city",
    "plan_for_junction",
]
