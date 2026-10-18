"""Deterministic e-challan recommendation rules (watchlist-anpr stream).

PURE functions: (sighting context, segment speed limit, junction signal
program, active closure windows) -> zero or more :class:`Recommendation`
records.  Same inputs ALWAYS produce the same recommendations — no ML, no
model tier, no clock reads (``now`` is an input), no discretion.  The
determinism is what makes a human officer's approval meaningful: the
evidence JSONB is reproducible byte-for-byte from the sighting row.

Rules (rule ids are CHECK-constrained in migration 010):
    R_SPD_SEGMENT          speed above the segment's speed limit by more
                           than SPEED_TOLERANCE_KMH
    R_SIGNAL_VIOLATION     camera crossing during the red interval of the
                           approach movement's phase (per the junction's
                           signal program, 006 semantics)
    R_CLOSED_CORRIDOR_ENTRY
                           an EARMARKED vehicle read inside an active
                           closure window covering the read position

HONEST DEGRADATION: a rule whose input context is missing (no known speed
limit, no signal program, no closures passed) produces NO recommendation —
never a guess.  The caller (traffic_oracle/enforcement/__init__.py)
assembles the context from the live tables and persists what fires.

ETHICS (C6): recommendations are PROPOSALS only.  Nothing here approves,
serves, or mails anything — only a human officer decision flips status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

__all__ = [
    "RULE_IDS",
    "SPEED_TOLERANCE_KMH",
    "SightingContext",
    "SegmentContext",
    "SignalContext",
    "ClosureContext",
    "Recommendation",
    "phase_state_at",
    "movement_for_heading",
    "evaluate_rules",
]

#: The three rule ids (migration 010 CHECK mirrors this tuple).
RULE_IDS = ("R_SPD_SEGMENT", "R_SIGNAL_VIOLATION", "R_CLOSED_CORRIDOR_ENTRY")

#: Speed tolerance before a segment-speed recommendation fires (km/h) —
#: the fixed enforcement margin, identical for every segment and vehicle.
SPEED_TOLERANCE_KMH = 10.0

#: Approach-movement labels the signal rules understand (006 phases carry
#: ``movements`` arrays with these codes).
_MOVEMENTS = ("NS", "EW")


@dataclass(frozen=True)
class SightingContext:
    """What the rules know about one camera read (all values from the row)."""

    sighting_id: str
    plate: str
    camera_id: str
    t: datetime
    lon: float
    lat: float
    source: str                      # 'emulated_anpr' this build
    speed_kmh: float | None = None
    heading: float | None = None     # degrees clockwise from north
    matched_plate_hit: str | None = None
    earmark_state: str | None = None  # active earmark state, if any


@dataclass(frozen=True)
class SegmentContext:
    """The road segment the read position falls on (caller-resolved)."""

    segment_id: int
    speed_limit_kmh: float | None    # None = unknown -> no speed rule
    road_name: str | None = None


@dataclass(frozen=True)
class SignalContext:
    """The signal program governing the read's junction (006 semantics)."""

    junction_id: int
    cycle_sec: int
    phases: tuple[tuple[int, tuple[str, ...]], ...]  # ((green_sec, movements),)
    offset_sec: int = 0

    def green_fraction(self, movement: str) -> float:
        """Share of the cycle during which ``movement`` has green (pure)."""
        green = sum(
            g for g, movements in self.phases if movement in movements
        )
        return green / float(self.cycle_sec) if self.cycle_sec else 0.0


@dataclass(frozen=True)
class ClosureContext:
    """One active closure window covering the read (caller pre-filtered)."""

    event_id: str
    title: str
    event_kind: str
    window_start: datetime
    window_end: datetime
    covers_position: bool            # caller's point-in-zone test result


@dataclass(frozen=True)
class Recommendation:
    """One deterministic rule outcome — persisted as a pending row."""

    rule_id: str
    recommendation: str
    evidence: dict[str, Any] = field(default_factory=dict)


def movement_for_heading(heading: float | None) -> str | None:
    """Map a camera heading to the signal movement group it feeds.

    0 deg = north.  headings within 45 deg of a cardinal axis map to that
    axis's movement: N/S -> 'NS', E/W -> 'EW'.  None or non-finite -> None
    (no signal rule possible).  Pure arithmetic, identical everywhere.
    """
    if heading is None:
        return None
    h = float(heading) % 360.0
    if 315.0 <= h or h < 45.0 or 135.0 <= h < 225.0:
        return "NS"
    return "EW"


def phase_state_at(
    program: SignalContext, movement: str, t: datetime
) -> tuple[str, int]:
    """Which state (green/red) ``movement`` is in at time ``t`` (pure).

    Deterministic fixed-time model (006 semantics): the cycle anchors at a
    program epoch; within each cycle, phases run back-to-back in array
    order; a movement is green exactly while a phase listing it is active.
    The program epoch is anchored on the UTC minute boundary of
    2026-01-01T00:00:00Z plus the junction offset, so any host computes the
    same answer for the same (program, t).  Returns (state, sec_into_cycle).
    """
    epoch = datetime(2026, 1, 1, tzinfo=t.tzinfo)
    elapsed = int((t - epoch).total_seconds()) - int(program.offset_sec)
    into = elapsed % int(program.cycle_sec) if program.cycle_sec else 0
    cursor = 0
    for green_sec, movements in program.phases:
        cursor += int(green_sec)
        if into < cursor:
            return ("green" if movement in movements else "red", into)
    # amber/lost-time tail beyond the listed green durations counts as red
    return ("red", into)


def evaluate_rules(
    sighting: SightingContext,
    segment: SegmentContext | None,
    signal: SignalContext | None,
    closures: tuple[ClosureContext, ...] = (),
) -> tuple[Recommendation, ...]:
    """Run all three rules over one sighting.  Pure and total.

    Missing context means the corresponding rule does not fire — the
    honest-degradation contract: a recommendation never rests on a guess.
    """
    out: list[Recommendation] = []

    # ── R_SPD_SEGMENT ────────────────────────────────────────────────────
    if (
        segment is not None
        and segment.speed_limit_kmh is not None
        and sighting.speed_kmh is not None
    ):
        limit = float(segment.speed_limit_kmh)
        speed = float(sighting.speed_kmh)
        if speed > limit + SPEED_TOLERANCE_KMH:
            out.append(
                Recommendation(
                    rule_id="R_SPD_SEGMENT",
                    recommendation=(
                        f"e-challan recommended: {sighting.plate} read at "
                        f"{speed:.0f} km/h in a {limit:.0f} km/h segment"
                    ),
                    evidence={
                        "sighting_id": sighting.sighting_id,
                        "plate": sighting.plate,
                        "camera_id": sighting.camera_id,
                        "t": sighting.t.isoformat(),
                        "speed_kmh": round(speed, 2),
                        "speed_limit_kmh": limit,
                        "excess_kmh": round(speed - limit, 2),
                        "tolerance_kmh": SPEED_TOLERANCE_KMH,
                        "segment_id": segment.segment_id,
                        "road_name": segment.road_name,
                        "source": sighting.source,
                    },
                )
            )

    # ── R_SIGNAL_VIOLATION ───────────────────────────────────────────────
    if signal is not None and signal.cycle_sec > 0 and signal.phases:
        movement = movement_for_heading(sighting.heading)
        if movement is not None:
            state, into = phase_state_at(signal, movement, sighting.t)
            if state == "red":
                out.append(
                    Recommendation(
                        rule_id="R_SIGNAL_VIOLATION",
                        recommendation=(
                            f"e-challan recommended: {sighting.plate} crossed "
                            f"junction {signal.junction_id} on red "
                            f"(movement {movement})"
                        ),
                        evidence={
                            "sighting_id": sighting.sighting_id,
                            "plate": sighting.plate,
                            "camera_id": sighting.camera_id,
                            "t": sighting.t.isoformat(),
                            "junction_id": signal.junction_id,
                            "movement": movement,
                            "signal_state": "red",
                            "sec_into_cycle": into,
                            "cycle_sec": signal.cycle_sec,
                            "green_fraction": round(
                                signal.green_fraction(movement), 4
                            ),
                            "source": sighting.source,
                        },
                    )
                )

    # ── R_CLOSED_CORRIDOR_ENTRY ──────────────────────────────────────────
    if sighting.earmark_state is not None:
        for closure in closures:
            covers_time = closure.window_start <= sighting.t <= closure.window_end
            if covers_time and closure.covers_position:
                out.append(
                    Recommendation(
                        rule_id="R_CLOSED_CORRIDOR_ENTRY",
                        recommendation=(
                            f"earmarked vehicle {sighting.plate} "
                            f"(state {sighting.earmark_state}) read inside the "
                            f"active closure window of {closure.title!r}"
                        ),
                        evidence={
                            "sighting_id": sighting.sighting_id,
                            "plate": sighting.plate,
                            "camera_id": sighting.camera_id,
                            "t": sighting.t.isoformat(),
                            "earmark_state": sighting.earmark_state,
                            "matched_plate_hit": sighting.matched_plate_hit,
                            "event_id": closure.event_id,
                            "event_kind": closure.event_kind,
                            "window_start": closure.window_start.isoformat(),
                            "window_end": closure.window_end.isoformat(),
                            "source": sighting.source,
                        },
                    )
                )
                break  # one closure recommendation per sighting (deterministic)

    return tuple(out)
