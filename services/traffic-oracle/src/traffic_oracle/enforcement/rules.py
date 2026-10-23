"""Deterministic e-challan recommendation rules (watchlist-anpr stream).

PURE functions: (sighting context, segment speed limit, junction signal
program, active closure windows) -> zero or more :class:`Recommendation`
records.  Same inputs ALWAYS produce the same recommendations — no ML, no
model tier, no clock reads (``now`` is an input), no discretion.  The
determinism is what makes a human officer's approval meaningful: the
evidence JSONB is reproducible byte-for-byte from the sighting row.

Rules (rule ids are CHECK-constrained in migration 010, widened by 017):
    R_SPD_SEGMENT          speed above the segment's speed limit by more
                           than SPEED_TOLERANCE_KMH
    R_SIGNAL_VIOLATION     a read ON an approach leg, heading into the
                           junction, during the RED interval of that
                           approach movement's phase (per the junction's
                           signal program, 006 semantics) — never during
                           its green or its amber
    R_CLOSED_CORRIDOR_ENTRY
                           an EARMARKED vehicle read inside an active
                           closure window covering the read position
    R_STOLEN_SIGHTING      (evidence, not penal — migration 017) a read of
                           a vehicle whose ACTIVE earmark rests on a
                           stolen-vehicle FIR

STOLEN VEHICLES (C-N, ethics): the registered owner of a car reported
stolen is the victim, never the payer of its thief's violations.  When the
caller passes a :class:`StolenVehicleContext`, :func:`evaluate_rules`
still evaluates the three penal rules — purely, as always — but returns
ONE ``R_STOLEN_SIGHTING`` instead: the read plus every penal rule it
would have fired, recorded as WITHHELD.  No e-challan is ever proposed
for a stolen vehicle; a human officer decides the sighting (approve =
the authorised basis of a pursuit, reject = dismissed).

HONEST DEGRADATION: a rule whose input context is missing (no known speed
limit, no signal program, no closures passed) produces NO recommendation —
never a guess.  The caller (traffic_oracle/enforcement/engine.py)
assembles the context from the live tables and persists what fires.

SIGNAL VOCABULARY: 006 seeds programs in road-hierarchy terms — phase 0
serves the junction's major ``through`` road (``local`` on a residential
junction), a later phase serves ``cross`` (with ``ramp`` on motorway
junctions).  A read's heading is mapped onto THAT vocabulary through the
junction's approach geometry: the leg whose inbound direction matches the
heading is the approach, the major road's legs carry phase 0's movement
and every other leg carries the cross movement.  Compass-labelled
programs (NS/EW) map by compass axis.  A heading that matches no approach
(or approaches with different movements) and a movement the program
does not define are NO DECISION — never an implied red.

ON THE APPROACH: a decision also needs the read to sit ON that approach
leg — the read's own segment (caller-resolved) must be the leg its heading
points in from (:func:`read_approach`).  A read past the stop line, on a
departure leg, cannot say which approach the vehicle came from (a
right-turner from the cross road reads exactly like a through vehicle),
and a read heading away from the junction on an approach leg is leaving
it: both are NO DECISION.  Without approach geometry there is no decision
either, whatever the program's vocabulary.

PHASE CLOCK: ``phase_state_at`` reads ``traffic_oracle.signals.plans.
phase_at`` — the same pure function that drives the canvas ring
(GET /api/v1/signals/state) — at the read's Unix time, so the evidence a
recommendation cites is exactly what the signal layer displayed.

ETHICS (C6): recommendations are PROPOSALS only.  Nothing here approves,
serves, or mails anything — only a human officer decision flips status.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from traffic_oracle.signals.plans import AMBER_SEC, phase_at

__all__ = [
    "RULE_IDS",
    "EVIDENCE_RULE_IDS",
    "ALL_RULE_IDS",
    "STOLEN_VEHICLE_BASIS",
    "CHALLAN_WITHHELD_TEXT",
    "SPEED_TOLERANCE_KMH",
    "APPROACH_TOL_DEG",
    "APPROACH_AMBIGUITY_DEG",
    "THROUGH_MAX_DEFLECTION_DEG",
    "SightingContext",
    "SegmentContext",
    "JunctionLeg",
    "SignalContext",
    "ClosureContext",
    "StolenVehicleContext",
    "Recommendation",
    "ApproachMovement",
    "through_legs",
    "approach_movement",
    "read_approach",
    "movement_for_heading",
    "phase_state_at",
    "fir_reference",
    "is_penal",
    "stolen_sighting",
    "evaluate_rules",
]

#: The three PENAL rule ids — e-challan proposals (migration 010 CHECK).
RULE_IDS = ("R_SPD_SEGMENT", "R_SIGNAL_VIOLATION", "R_CLOSED_CORRIDOR_ENTRY")

#: Non-penal EVIDENCE rule ids (migration 017 widens the CHECK with them):
#: a read recorded for an officer's decision, never an e-challan.
EVIDENCE_RULE_IDS = ("R_STOLEN_SIGHTING",)

#: Every rule id the migration-017 CHECK accepts.
ALL_RULE_IDS = RULE_IDS + EVIDENCE_RULE_IDS

#: The earmark lawful basis that marks a vehicle reported stolen (010).
STOLEN_VEHICLE_BASIS = "stolen_vehicle_fir"

#: The one wording for a withheld challan (the decision endpoint's 409,
#: the city entity and the feed all say exactly this).
CHALLAN_WITHHELD_TEXT = "e-challan withheld — vehicle reported stolen"

#: A FIR reference inside an earmark reason ('FIR 4401/2026, Kalupur PS'
#: -> '4401/2026').  Only the reference leaves the reason — never the rest
#: of the officer's free text — and a reference carries at least one digit
#: ('FIR registered at ...' names none).
_FIR_REF_RE = re.compile(
    r"\bFIR\b[\s:#.\-]*(?:No\b\.?\s*)?([A-Za-z0-9][A-Za-z0-9/\-]{0,31})", re.IGNORECASE
)

#: Speed tolerance before a segment-speed recommendation fires (km/h) —
#: the fixed enforcement margin, identical for every segment and vehicle.
SPEED_TOLERANCE_KMH = 10.0

#: A read's heading must lie within this many degrees of a leg's inbound
#: direction for that leg to be its approach; otherwise no signal decision.
APPROACH_TOL_DEG = 45.0

#: Any approach whose inbound direction matches the heading within this
#: many degrees of the best match, but carries a DIFFERENT movement, makes
#: the approach ambiguous — no signal decision.
APPROACH_AMBIGUITY_DEG = 10.0

#: Two legs form the major road's through pair only when the path between
#: them deflects at most this much from straight.
THROUGH_MAX_DEFLECTION_DEG = 75.0

#: Road-class rank for picking the major (through) road at a junction —
#: the same hierarchy 006 uses to choose a junction's program.
_CLASS_RANK = {
    "motorway": 6,
    "trunk": 5,
    "primary": 4,
    "secondary": 3,
    "tertiary": 2,
    "residential": 1,
    "service": 0,
}

#: Compass vocabulary (006 header example).  Programs labelled only with
#: these map by compass axis instead of road hierarchy.
_COMPASS_MOVEMENTS = frozenset({"NS", "EW"})


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
class JunctionLeg:
    """One road leg meeting a junction (caller-resolved from geometry).

    ``bearing_deg`` points AWAY from the junction along the leg (degrees
    clockwise from north), so traffic approaching on this leg travels
    with heading ``bearing_deg + 180``.
    """

    segment_id: int
    road_class: str
    bearing_deg: float
    road_name: str | None = None


@dataclass(frozen=True)
class SignalContext:
    """The signal program governing the read's junction (006 semantics)."""

    junction_id: int
    cycle_sec: int
    phases: tuple[tuple[int, tuple[str, ...]], ...]  # ((green_sec, movements),)
    offset_sec: int = 0
    legs: tuple[JunctionLeg, ...] = ()               # approach geometry

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
class StolenVehicleContext:
    """The read's vehicle holds an ACTIVE stolen-vehicle earmark (caller-
    resolved: lawful basis ``stolen_vehicle_fir``, not cleared, retention
    not lapsed).  Its presence is the whole switch — see evaluate_rules."""

    fir_ref: str | None = None       # 'FIR 4401/2026' -> '4401/2026'
    earmark_state: str | None = None
    lawful_basis: str = STOLEN_VEHICLE_BASIS


@dataclass(frozen=True)
class Recommendation:
    """One deterministic rule outcome — persisted as a pending row."""

    rule_id: str
    recommendation: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApproachMovement:
    """The movement a read's heading feeds at a junction (pure result)."""

    movement: str
    approach_segment_id: int | None   # None: compass mapping without legs
                                      # (never enough for a decision)
    approach_delta_deg: float | None  # heading vs the leg's inbound direction
    through: bool


def _angle_diff(a: float, b: float) -> float:
    """Smallest absolute difference between two bearings, in [0, 180]."""
    d = (float(a) - float(b)) % 360.0
    return min(d, 360.0 - d)


def _program_vocabulary(signal: SignalContext) -> tuple[str | None, str | None]:
    """(major movement, cross movement) as the program itself names them.

    Phase 0 serves the major road (006 always seeds it first); the cross
    movement is 'cross' when any later phase lists it, else the first
    other label of phase 1.  Missing labels are None — never invented.
    """
    phases = [movements for _green, movements in signal.phases if movements]
    if not phases:
        return None, None
    major = phases[0][0]
    for movements in phases[1:]:
        if "cross" in movements and "cross" != major:
            return major, "cross"
    if len(phases) > 1:
        others = [m for m in phases[1] if m != major]
        return major, (others[0] if others else None)
    return major, None


def through_legs(legs: tuple[JunctionLeg, ...]) -> frozenset[int]:
    """Segment ids of the junction's major-road (through) legs.  Pure.

    The through pair is the pair of legs with the best combined road-class
    rank, discounted by how far the path between them bends from straight
    (one rank per 45 deg), among pairs within THROUGH_MAX_DEFLECTION_DEG of
    straight.  With no such pair, the single highest-ranked leg.  Ties
    break on segment id, so the answer is deterministic.
    """
    if not legs:
        return frozenset()
    ordered = sorted(legs, key=lambda leg: leg.segment_id)
    best_score: float | None = None
    best_pair: tuple[int, int] | None = None
    # pairs are visited in ascending (id, id) order and only a STRICTLY
    # better score replaces the incumbent: ties keep the lowest ids
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            deflection = 180.0 - _angle_diff(a.bearing_deg, b.bearing_deg)
            if deflection > THROUGH_MAX_DEFLECTION_DEG:
                continue
            score = (
                _CLASS_RANK.get(a.road_class, 0)
                + _CLASS_RANK.get(b.road_class, 0)
                - deflection / 45.0
            )
            if best_score is None or score > best_score + 1e-9:
                best_score, best_pair = score, (a.segment_id, b.segment_id)
    if best_pair is not None:
        return frozenset(best_pair)
    top = max(ordered, key=lambda leg: (_CLASS_RANK.get(leg.road_class, 0),
                                        -leg.segment_id))
    return frozenset({top.segment_id})


def _compass_movement(bearing: float) -> str:
    h = float(bearing) % 360.0
    if 315.0 <= h or h < 45.0 or 135.0 <= h < 225.0:
        return "NS"
    return "EW"


def approach_movement(
    heading: float | None, signal: SignalContext
) -> ApproachMovement | None:
    """Map a read's heading onto the movement the program actually defines.

    Pure.  Road-hierarchy programs (through/cross/ramp/local) need the
    junction legs: the approach is the leg whose inbound direction
    (bearing + 180) is closest to the heading, within APPROACH_TOL_DEG; a
    through leg feeds phase 0's movement, any other leg the cross
    movement.  Compass programs (NS/EW) map the approach's inbound
    direction — or the raw heading when no legs are known — to its axis.
    None (no decision) when the heading is missing or non-finite, matches
    no approach, is ambiguous (ANY approach within APPROACH_AMBIGUITY_DEG
    of the best match carries a different movement), or the program does
    not define the resulting movement.  This maps a heading; whether the
    read is ON that approach is :func:`read_approach`'s question.
    """
    if heading is None:
        return None
    try:
        h = float(heading)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(h):
        return None
    h %= 360.0

    labels = {m for _, movements in signal.phases for m in movements}
    compass = bool(labels) and labels <= _COMPASS_MOVEMENTS

    ranked = sorted(
        (
            (_angle_diff(h, leg.bearing_deg + 180.0), leg.segment_id, leg)
            for leg in signal.legs
        ),
        key=lambda x: (x[0], x[1]),
    )

    if compass:
        if not ranked:
            movement = _compass_movement(h)
            if movement not in labels:
                return None
            return ApproachMovement(movement, None, None, False)
        delta, _, leg = ranked[0]
        if delta > APPROACH_TOL_DEG:
            return None
        movement = _compass_movement(leg.bearing_deg + 180.0)
        for delta2, _, leg2 in ranked[1:]:  # ascending: stop past the band
            if delta2 - delta >= APPROACH_AMBIGUITY_DEG:
                break
            if _compass_movement(leg2.bearing_deg + 180.0) != movement:
                return None
        if movement not in labels:
            return None
        return ApproachMovement(movement, leg.segment_id, round(delta, 1), False)

    major, cross = _program_vocabulary(signal)
    if not ranked or major is None:
        return None
    through = through_legs(signal.legs)

    def _movement_of(leg: JunctionLeg) -> str | None:
        return major if leg.segment_id in through else cross

    delta, _, leg = ranked[0]
    if delta > APPROACH_TOL_DEG:
        return None
    movement = _movement_of(leg)
    for delta2, _, leg2 in ranked[1:]:  # ascending: stop past the band
        if delta2 - delta >= APPROACH_AMBIGUITY_DEG:
            break
        if _movement_of(leg2) != movement:
            return None
    if movement is None or movement not in labels:
        return None
    return ApproachMovement(
        movement, leg.segment_id, round(delta, 1), leg.segment_id in through
    )


def read_approach(
    heading: float | None, segment_id: int | None, signal: SignalContext
) -> ApproachMovement | None:
    """The approach a read is ON — the only basis for a signal decision.

    Pure.  POSITION picks the leg (the read's own segment must be one of
    the junction's legs) and the HEADING must say the vehicle is driving
    in on it: the leg's inbound direction within APPROACH_TOL_DEG of the
    heading and inside the ambiguity band of the best-matching approach
    (:func:`approach_movement`, which already refused a band holding two
    movements — so the read's leg carries the mapped movement).  A read
    on a departure leg (a turning vehicle, or one already through), a read
    heading away from the junction, a read off every leg and a program
    without leg geometry are all None — no decision.
    """
    approach = approach_movement(heading, signal)
    if approach is None or approach.approach_segment_id is None:
        return None
    if segment_id is None:
        return None
    own = next(
        (leg for leg in signal.legs if leg.segment_id == int(segment_id)), None
    )
    if own is None:
        return None  # the read is not on any leg of this junction
    if own.segment_id == approach.approach_segment_id:
        return approach
    h = float(heading) % 360.0
    best = min(_angle_diff(h, leg.bearing_deg + 180.0) for leg in signal.legs)
    delta = _angle_diff(h, own.bearing_deg + 180.0)
    if delta > APPROACH_TOL_DEG or delta - best >= APPROACH_AMBIGUITY_DEG:
        return None  # its own leg is not the way this heading drives in
    # inside the band every leg carries approach.movement (and so the same
    # through/cross role), so the read's own leg is the approach
    return ApproachMovement(
        approach.movement, own.segment_id, round(delta, 1), approach.through
    )


def movement_for_heading(
    heading: float | None, signal: SignalContext
) -> str | None:
    """The program-vocabulary movement a heading feeds (None = no decision)."""
    approach = approach_movement(heading, signal)
    return approach.movement if approach is not None else None


def phase_state_at(
    program: SignalContext, movement: str, t: datetime
) -> tuple[str, int]:
    """State of ``movement`` at time ``t``: (green|amber|red|none, sec_into_cycle).

    Pure.  Delegates to the shared phase clock
    (``traffic_oracle.signals.plans.phase_at``) at ``t``'s Unix time —
    pos = (epoch - offset) mod cycle with the phases walked from 0 — so
    this is, by construction, the state the canvas ring displays.  A
    movement the program does not define is 'none' (no decision), never
    red; amber is the AMBER_SEC after one of the movement's greens.
    """
    state = phase_at(program, t.timestamp())
    return state.state_of(movement), state.sec_into_cycle


def fir_reference(reason: str | None) -> str | None:
    """The FIR reference inside an earmark reason, or None.  Pure.

    'STOLEN VEHICLE reported — FIR 4401/2026, Kalupur PS' -> '4401/2026'.
    Only the reference is extracted (letters, digits, '/' and '-', at most
    32 characters): the rest of the officer's free text never reaches a
    recommendation.
    """
    if not reason:
        return None
    m = _FIR_REF_RE.search(str(reason))
    if m is None:
        return None
    ref = m.group(1).rstrip("/-")
    return ref if any(c.isdigit() for c in ref) else None


def is_penal(rule_id: str | None) -> bool:
    """True for an e-challan rule (the ones a stolen vehicle never gets)."""
    return rule_id in RULE_IDS


def stolen_sighting(
    sighting: SightingContext,
    segment: SegmentContext | None,
    withheld: tuple[Recommendation, ...],
    stolen: StolenVehicleContext,
) -> Recommendation:
    """The ONE recommendation a stolen vehicle's read produces.  Pure.

    Evidence = the read (time, camera, position, speed, heading, source)
    plus every penal rule it would have fired — kept as WITHHELD, with
    their own deterministic evidence, so the officer sees what the camera
    recorded without anyone proposing a fine to the victim.
    """
    fir = f"FIR {stolen.fir_ref}" if stolen.fir_ref else "FIR"
    return Recommendation(
        rule_id="R_STOLEN_SIGHTING",
        recommendation=(
            f"stolen vehicle ({fir}) sighted at {sighting.camera_id} — "
            "e-challan withheld, vehicle reported stolen"
        ),
        evidence={
            "sighting_id": sighting.sighting_id,
            "plate": sighting.plate,
            "camera_id": sighting.camera_id,
            "t": sighting.t.isoformat(),
            "lon": round(float(sighting.lon), 6),
            "lat": round(float(sighting.lat), 6),
            "speed_kmh": (
                round(float(sighting.speed_kmh), 2)
                if sighting.speed_kmh is not None
                else None
            ),
            "heading": (
                round(float(sighting.heading), 1)
                if sighting.heading is not None
                else None
            ),
            "segment_id": segment.segment_id if segment is not None else None,
            "road_name": segment.road_name if segment is not None else None,
            "matched_plate_hit": sighting.matched_plate_hit,
            "earmark_state": stolen.earmark_state or sighting.earmark_state,
            "lawful_basis": stolen.lawful_basis,
            "fir_ref": stolen.fir_ref,
            "challan_withheld": True,
            "withheld_reason": "vehicle reported stolen",
            # what the penal rules WOULD have proposed — never persisted as
            # recommendations of their own
            "withheld_rules": [
                {"rule_id": rec.rule_id, "evidence": rec.evidence} for rec in withheld
            ],
            "source": sighting.source,
        },
    )


def evaluate_rules(
    sighting: SightingContext,
    segment: SegmentContext | None,
    signal: SignalContext | None,
    closures: tuple[ClosureContext, ...] = (),
    *,
    stolen: StolenVehicleContext | None = None,
) -> tuple[Recommendation, ...]:
    """Run all three rules over one sighting.  Pure and total.

    Missing context means the corresponding rule does not fire — the
    honest-degradation contract: a recommendation never rests on a guess.

    ``stolen`` (the vehicle's active stolen-vehicle earmark) switches the
    outcome, never the evaluation: the penal rules run exactly as without
    it, and their results are folded into ONE ``R_STOLEN_SIGHTING`` (see
    :func:`stolen_sighting`) — no penal recommendation is returned.
    """
    penal = _penal_rules(sighting, segment, signal, closures)
    if stolen is None:
        return penal
    return (stolen_sighting(sighting, segment, penal, stolen),)


def _penal_rules(
    sighting: SightingContext,
    segment: SegmentContext | None,
    signal: SignalContext | None,
    closures: tuple[ClosureContext, ...],
) -> tuple[Recommendation, ...]:
    """The three penal rules over one sighting (pure; evaluate_rules)."""
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
        approach = read_approach(
            sighting.heading,
            segment.segment_id if segment is not None else None,
            signal,
        )
        if approach is not None:
            movement = approach.movement
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
                            "heading": round(float(sighting.heading), 1),
                            "approach_segment_id": approach.approach_segment_id,
                            "approach_delta_deg": approach.approach_delta_deg,
                            "signal_state": "red",
                            "sec_into_cycle": into,
                            "cycle_sec": signal.cycle_sec,
                            "offset_sec": signal.offset_sec,
                            "amber_sec": AMBER_SEC,
                            "phase_clock": "(unix_epoch - offset_sec) mod cycle_sec",
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
