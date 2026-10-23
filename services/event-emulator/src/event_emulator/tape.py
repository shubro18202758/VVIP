"""The cause tape — a pure, deterministic function of (spec, seed, t0, run).

Cause kinds are the six of contract C4 plus the two watchlist additions of
contract C2 (watchlist-anpr stream):
    event_row, inputs_doc, scheduled_event, event_config,
    weather_ramp, crowd_script,
    anpr_read, agency_unit

'anpr_read' and 'agency_unit' fire only for profiles carrying watchlist
knobs (anpr_cameras / agency_units > 0).  Every generated plate carries an
'EM' segment (self-labelling synthetic) and read identity is uuid5 over
(use_case, seed, camera, plate, run).  A read is a CAUSE only — a vehicle
passing a camera; whether it is a hit, and whether any e-challan rule
fires, is computed by the oracle's ANPR seam (contract C-E) from its own
earmarks, limits and signal programs.  The tape never asserts a hit.

('emergency_request' is not a kind — flood relief is priority sub-event rows
+ scheduled emergency_vehicle + ambulance_delay_projection inputs.)

Every frame carries t_sec (seconds after t0_anchor, SCENARIO time); a frame
fires — and anything it stamps is stamped — at wall time
``t0 + t_sec / clock_scale``.  Replay re-posts the stored frames verbatim
with only the wall-clock anchor shifted.  The same (spec, seed, t0, run_id,
geometry) builds a byte-identical tape on any host.

Run identity: a ledgered live run's movement/event id is uuid5 over
(use_case, seed, run_id), so every run is a fresh subject for the
situation engine (no hysteresis carried over from an earlier run); a replay
re-posts the stored tape and therefore re-animates the ORIGINAL id.

This module also holds the pure helpers the emulated drivers share with the
living city (ambient.py): road geometry walking and the signal-phase clock
— a faithful replica of the oracle's phase clock and approach mapping
(traffic_oracle/signals/plans.py ``phase_at`` and enforcement/rules.py
``through_legs`` / ``approach_movement``), so an emulated vehicle that is
meant to obey a light crosses its stop line on the very green the
e-challan engine evaluates.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from event_emulator.library import UseCase
from event_emulator.profiles import UseCaseProfile
from event_emulator.rng import SeededRng, derive_seed
from event_emulator.spec import ResolvedRun

__all__ = [
    "CAUSE_KINDS",
    "TAPE_VERSION",
    "AGENCY_CADENCE_SEC",
    "SATURATION_IDX",
    "CauseFrame",
    "CauseTape",
    "movement_uuid",
    "anpr_read_uuid",
    "build_tape",
    "recorded_route",
    "wall_time",
    "congestion_at",
    "chain_polyline",
    "orient_route",
    "route_polyline",
    "polyline_length_m",
    "point_along",
    "pingpong_point",
    "compliant_speed_kmh",
    "AMBER_SEC",
    "movement_states_at",
    "approach_movement",
    "is_emulated_plate",
]

CAUSE_KINDS = (
    "event_row",
    "inputs_doc",
    "scheduled_event",
    "event_config",
    "weather_ramp",
    "crowd_script",
    # watchlist additions (contract C2):
    "anpr_read",
    "agency_unit",
)

#: 2 = run-scoped identities, camera-site reads, no asserted hits.
TAPE_VERSION = 2

#: Tape-time cadence of agency_unit frames.  The oracle keeps each unit
#: key for 30 s (contract C-A, EX 30), so a frame at most every 25 s of tape
#: time keeps a patrol continuously visible at any clock_scale >= 1.
AGENCY_CADENCE_SEC = 20.0

#: predicted_congestion_idx at or above which a tick counts as saturated.
SATURATION_IDX = 0.95

_EMULATOR_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "vvip-event-emulator")


def movement_uuid(use_case_id: str, seed: int, run_id: int | None = None) -> str:
    """Movement / corridor-event identity of a run.

    A ledgered run: uuid5 over (use_case, seed, run_id) — a fresh subject
    per run.  ``run_id`` None (an unledgered dry run or ``--no-ledger``
    injection) keeps the run-less uuid5(use_case, seed).  Replays never
    call this: they re-post the stored tape, original id included.
    """
    if run_id is None:
        return str(uuid.uuid5(_EMULATOR_NAMESPACE, f"{use_case_id}|{seed}"))
    return str(uuid.uuid5(_EMULATOR_NAMESPACE, f"{use_case_id}|{seed}|run:{run_id}"))


def anpr_read_uuid(
    use_case_id: str, seed: int, camera_id: str, plate: str, run_id: int | None = None
) -> str:
    """Deterministic camera-read identity (contracts C2 / C-E).

    uuid5 over (use_case, seed, camera, plate[, run]) — one read per
    (camera, plate) pair per run.  The oracle's ingest seam is idempotent on
    the read id, so a REPLAY (which re-posts the original tape) lands as
    duplicates and forks nothing, while a new live run reads afresh.
    """
    key = f"{use_case_id}|{seed}|{camera_id}|{plate}"
    if run_id is not None:
        key += f"|run:{run_id}"
    return str(uuid.uuid5(_EMULATOR_NAMESPACE, key))


# ── watchlist generators (contract C2) ──────────────────────────────────────

#: Synthetic-plate alphabet — every generated plate embeds 'EM' so an
#: emulated plate is self-labelling wherever it surfaces (C6 badge).
_PLATE_RTO_PREFIX = "GJ01"   # Gujarat RTO 01 (Ahmedabad)
_PLATE_MARKER = "EM"

#: The demo plate a stolen-vehicle FIR names (its stolen-FIR earmark is the
#: scenario's premise — an operator's, else the runner's labelled EMULATED
#: precondition; the oracle — not the tape — turns its reads into
#: stolen_vehicle hits).
_STOLEN_LABEL = "GJ27EM9001"
#: The demo plate an operator earmark exercise names: its reads hit
#: (earmarked_plate) only once a duty officer has earmarked it — the
#: emulator never does.
_EARMARKED_LABEL = "GJ27EM4321"

#: Fallback old-city belt anchor (lon, lat) used when the caller passes no
#: geometry at all — deterministic on every host either way.
_FALLBACK_ANCHOR = (72.58, 22.99)

#: Metres per degree on this latitude belt: 111 320 m per degree of
#: latitude; longitude shrinks by cos(23.03 deg) = 0.9203 (a fixed factor,
#: so every host computes identical positions).
_M_PER_DEG_LAT = 111320.0
_M_PER_DEG_LON = 111320.0 * 0.9203

#: Speed a background vehicle holds relative to the limit of the segment
#: under the camera: well inside the e-challan tolerance (limit + 10).
_COMPLIANT_FACTOR = (0.55, 0.9)
#: Limit assumed when no segment limit is known (the old-city minimum).
_DEFAULT_LIMIT_KMH = 40.0

#: Patrol speed (km/h) — a two-wheeler Jan Rakshak beat.
_PATROL_SPEED_KMH = 18.0


def _uuid_int(*parts: object) -> int:
    return uuid.uuid5(_EMULATOR_NAMESPACE, "|".join(str(p) for p in parts)).int


def _plate_for(index: int) -> str:
    digits = 1000 + (_uuid_int("plate", index) % 9000)
    return f"{_PLATE_RTO_PREFIX}{_PLATE_MARKER}{digits}"


def is_emulated_plate(plate: str) -> bool:
    """Every emulator plate carries the EM series marker after the RTO code."""
    return len(plate) > 6 and plate[4:6] == _PLATE_MARKER


def compliant_speed_kmh(limit_kmh: float | None, u: int) -> float:
    """A law-abiding speed for a camera read (deterministic in ``u``)."""
    limit = float(limit_kmh) if limit_kmh else _DEFAULT_LIMIT_KMH
    lo, hi = _COMPLIANT_FACTOR
    frac = (u % 1000) / 999.0
    return round(limit * (lo + (hi - lo) * frac), 1)


def _offset_m(anchor: tuple[float, float], dx_m: float, dy_m: float) -> list[float]:
    """Anchor + (dx_m east, dy_m north) in degrees, rounded for stability."""
    return [
        round(anchor[0] + dx_m / _M_PER_DEG_LON, 6),
        round(anchor[1] + dy_m / _M_PER_DEG_LAT, 6),
    ]


# ── road geometry (pure; shared with the living city) ──────────────────────


def _dist_m(a: Sequence[float], b: Sequence[float]) -> float:
    dx = (float(b[0]) - float(a[0])) * _M_PER_DEG_LON
    dy = (float(b[1]) - float(a[1])) * _M_PER_DEG_LAT
    return math.hypot(dx, dy)


def _bearing_deg(a: Sequence[float], b: Sequence[float]) -> float:
    dx = (float(b[0]) - float(a[0])) * _M_PER_DEG_LON
    dy = (float(b[1]) - float(a[1])) * _M_PER_DEG_LAT
    return math.degrees(math.atan2(dx, dy)) % 360.0


def chain_polyline(
    segment_ids: Sequence[int],
    paths: Mapping[int, Sequence[Sequence[float]]],
) -> list[tuple[float, float]]:
    """Join the segments' polylines into one path, in route order.

    Each segment is oriented so it starts at the end nearest the path so
    far (the seed draws segments in arbitrary directions).  Segments
    without geometry are skipped.
    """
    out: list[tuple[float, float]] = []
    for sid in segment_ids:
        pts = [(float(p[0]), float(p[1])) for p in paths.get(int(sid), ())]
        if len(pts) < 2:
            continue
        if out:
            tail = out[-1]
            if _dist_m(tail, pts[-1]) < _dist_m(tail, pts[0]):
                pts.reverse()
        elif len(segment_ids) > 1:
            nxt = paths.get(int(segment_ids[1]), ())
            if len(nxt) >= 2:
                ends = (nxt[0], nxt[-1])
                if min(_dist_m(pts[0], e) for e in ends) < min(_dist_m(pts[-1], e) for e in ends):
                    pts.reverse()
        if out and _dist_m(out[-1], pts[0]) < 1.0:
            pts = pts[1:]
        out.extend(pts)
    return out


#: orient_route: metres of mismatch charged for reading a ONE-WAY segment
#: against its geometry (corridor.road_graph: a one-way is entered at its
#: start and left at its end).  It settles a tie — two roads that meet at
#: both ends, e.g. one-way Gandhi Road (1012) beside Relief Road (1005)
#: between Lal Darwaja and Kalupur — and never outweighs a real gap
#: between consecutive segments (the road_graph's own 25 m junction snap).
_ONEWAY_AGAINST_M = 25.0
#: ... and a two-way segment read against its stored direction: with
#: nothing else to go on, the stored direction stands.
_STORED_DIRECTION_EPS_M = 1e-6


def orient_route(
    segment_ids: Sequence[int],
    lines: Mapping[int, Sequence[Sequence[float]]],
    *,
    oneway: Mapping[int, bool] | None = None,
    vias: Sequence[Sequence[Sequence[float]]] | None = None,
) -> list[list[tuple[float, float]]]:
    """Each route segment's polyline in the DIRECTION OF TRAVEL.

    ``vias[i]`` holds the junction point(s) segment_adjacency records for
    the movement route[i] -> route[i+1] (empty: none recorded).  Every
    segment's orientation is chosen jointly — a Viterbi pass over
    forward / reversed — to minimise metres of mismatch: the exit of one
    segment meets the entry of the next, at a recorded via junction when
    the pair has one, and a one-way segment runs from its geometry's
    start to its end.  So a pair of roads meeting at both ends is settled
    by the one-way rule or by the neighbours, and a stored route that is
    not a legal path still gets its most continuous reading.  A segment
    without geometry yields an empty list in its place.
    """
    ids = [int(s) for s in segment_ids]
    pts = [[(float(p[0]), float(p[1])) for p in lines.get(s, ())] for s in ids]
    valid = [i for i, line in enumerate(pts) if len(line) >= 2]
    one = oneway or {}
    via = vias or ()
    out: list[list[tuple[float, float]]] = [[] for _ in ids]
    if not valid:
        return out

    def ends(i: int, rev: int) -> tuple[tuple[float, float], tuple[float, float]]:
        return (pts[i][-1], pts[i][0]) if rev else (pts[i][0], pts[i][-1])

    def own(i: int, rev: int) -> float:
        if not rev:
            return 0.0
        return _ONEWAY_AGAINST_M if one.get(ids[i]) else _STORED_DIRECTION_EPS_M

    def joint(i: int, ri: int, j: int, rj: int) -> float:
        leave, enter = ends(i, ri)[1], ends(j, rj)[0]
        juncs = via[i] if j == i + 1 and i < len(via) else ()
        if juncs:
            return min(_dist_m(leave, v) + _dist_m(enter, v) for v in juncs)
        return _dist_m(leave, enter)

    cost = [own(valid[0], 0), own(valid[0], 1)]
    back: list[tuple[int, int]] = []
    for prev, cur in zip(valid, valid[1:]):
        step: list[float] = []
        came: list[int] = []
        for rev in (0, 1):
            options = [cost[q] + joint(prev, q, cur, rev) for q in (0, 1)]
            q = 0 if options[0] <= options[1] else 1
            step.append(options[q] + own(cur, rev))
            came.append(q)
        cost = step
        back.append((came[0], came[1]))
    rev = 0 if cost[0] <= cost[1] else 1
    picks = [rev]
    for came_from in reversed(back):
        rev = came_from[rev]
        picks.append(rev)
    picks.reverse()
    for i, rev in zip(valid, picks):
        out[i] = pts[i][::-1] if rev else list(pts[i])
    return out


def route_polyline(oriented: Iterable[Sequence[Sequence[float]]]) -> list[tuple[float, float]]:
    """Oriented segment lines (:func:`orient_route`) as ONE LineString in
    travel order — the shared junction vertex once; a gap a stored route
    leaves between two segments is bridged by its straight join."""
    out: list[tuple[float, float]] = []
    for line in oriented:
        pts = [(float(p[0]), float(p[1])) for p in line]
        if out and pts and _dist_m(out[-1], pts[0]) < 1.0:
            pts = pts[1:]
        out.extend(pts)
    return out


def polyline_length_m(line: Sequence[Sequence[float]]) -> float:
    return sum(_dist_m(a, b) for a, b in zip(line, line[1:]))


def point_along(
    line: Sequence[Sequence[float]], dist_m: float
) -> tuple[float, float, float]:
    """(lon, lat, bearing) ``dist_m`` metres along the polyline (clamped)."""
    if not line:
        return (_FALLBACK_ANCHOR[0], _FALLBACK_ANCHOR[1], 0.0)
    if len(line) == 1:
        return (float(line[0][0]), float(line[0][1]), 0.0)
    remaining = max(0.0, float(dist_m))
    for a, b in zip(line, line[1:]):
        seg = _dist_m(a, b)
        if remaining <= seg or b is line[-1]:
            frac = 0.0 if seg <= 0 else min(1.0, remaining / seg)
            lon = float(a[0]) + (float(b[0]) - float(a[0])) * frac
            lat = float(a[1]) + (float(b[1]) - float(a[1])) * frac
            return (round(lon, 6), round(lat, 6), round(_bearing_deg(a, b), 1))
        remaining -= seg
    last, prev = line[-1], line[-2]
    return (float(last[0]), float(last[1]), round(_bearing_deg(prev, last), 1))


def pingpong_point(
    line: Sequence[Sequence[float]], travelled_m: float
) -> tuple[float, float, float]:
    """Position after ``travelled_m`` of a back-and-forth beat on ``line``."""
    length = polyline_length_m(line)
    if length <= 0:
        return point_along(line, 0.0)
    s = float(travelled_m) % (2.0 * length)
    if s <= length:
        return point_along(line, s)
    lon, lat, bearing = point_along(line, 2.0 * length - s)
    return (lon, lat, round((bearing + 180.0) % 360.0, 1))


# ── signal clock replica (oracle phase_at + approach mapping) ──────────────

#: Amber after each green (signals/plans.py AMBER_SEC) — never a red.
AMBER_SEC = 3

#: Approach matching (enforcement/rules.py).
_APPROACH_TOL_DEG = 45.0
_APPROACH_AMBIGUITY_DEG = 10.0
_THROUGH_MAX_DEFLECTION_DEG = 75.0
_CLASS_RANK = {
    "motorway": 6, "trunk": 5, "primary": 4, "secondary": 3,
    "tertiary": 2, "residential": 1, "service": 0,
}
_COMPASS = frozenset({"NS", "EW"})


def _norm_phases(phases: Any) -> list[tuple[int, tuple[str, ...]]]:
    """006 JSONB phases (list of {green_sec, movements}) or (green, movements) pairs."""
    raw = json.loads(phases) if isinstance(phases, str) else (phases or [])
    out: list[tuple[int, tuple[str, ...]]] = []
    for p in raw:
        if isinstance(p, Mapping):
            out.append((int(p.get("green_sec", 0) or 0),
                        tuple(str(m) for m in (p.get("movements") or []))))
        else:
            out.append((int(p[0]), tuple(str(m) for m in p[1])))
    return out


def movement_states_at(
    cycle_sec: int, offset_sec: int, phases: Any, epoch_s: float
) -> dict[str, str]:
    """green | amber | red per movement at Unix time ``epoch_s``.

    pos = (floor(epoch) - offset) mod cycle; phases are walked back-to-back
    from 0 (over-allocated greens clipped to the cycle); a movement is green
    while a phase listing it is in effect, amber for AMBER_SEC after one of
    its greens ends, red otherwise.  A degenerate program defines nothing.
    """
    cycle = int(cycle_sec or 0)
    plist = _norm_phases(phases)
    if cycle <= 0:
        return {}
    timeline: list[tuple[int, int, tuple[str, ...]]] = []
    cursor = 0
    for green, movements in plist:
        g = max(0, int(green))
        start, end = min(cursor, cycle), min(cursor + g, cycle)
        timeline.append((start, end, movements))
        cursor += g
    greens = [t for t in timeline if t[1] > t[0]]
    if not greens:
        return {}
    pos = (math.floor(epoch_s) - int(offset_sec or 0)) % cycle
    active = next((t for t in greens if t[0] <= pos < t[1]), None)
    labels: list[str] = []
    for _s, _e, movements in timeline:
        for m in movements:
            if m not in labels:
                labels.append(m)
    states: dict[str, str] = {}
    for m in labels:
        if active is not None and m in active[2]:
            states[m] = "green"
            continue
        in_amber = any((pos - t[1]) % cycle < AMBER_SEC for t in greens if m in t[2])
        states[m] = "amber" if in_amber else "red"
    return states


def _angle_diff(a: float, b: float) -> float:
    d = (float(a) - float(b)) % 360.0
    return min(d, 360.0 - d)


def _through_legs(legs: Sequence[Mapping[str, Any]]) -> frozenset[int]:
    ordered = sorted(legs, key=lambda leg: int(leg["segment_id"]))
    best_score: float | None = None
    best_pair: tuple[int, int] | None = None
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            deflection = 180.0 - _angle_diff(a["bearing_deg"], b["bearing_deg"])
            if deflection > _THROUGH_MAX_DEFLECTION_DEG:
                continue
            score = (_CLASS_RANK.get(str(a["road_class"]), 0)
                     + _CLASS_RANK.get(str(b["road_class"]), 0)
                     - deflection / 45.0)
            if best_score is None or score > best_score + 1e-9:
                best_score = score
                best_pair = (int(a["segment_id"]), int(b["segment_id"]))
    if best_pair is not None:
        return frozenset(best_pair)
    if not ordered:
        return frozenset()
    top = max(ordered, key=lambda leg: (_CLASS_RANK.get(str(leg["road_class"]), 0),
                                        -int(leg["segment_id"])))
    return frozenset({int(top["segment_id"])})


def _compass_movement(bearing: float) -> str:
    h = float(bearing) % 360.0
    return "NS" if (315.0 <= h or h < 45.0 or 135.0 <= h < 225.0) else "EW"


def approach_movement(
    heading: float | None,
    legs: Sequence[Mapping[str, Any]],
    phases: Any,
    segment_id: int | None = None,
) -> str | None:
    """The program movement a read with ``heading`` on ``segment_id`` feeds.

    ``legs``: [{segment_id, road_class, bearing_deg (away from the
    junction)}].  None means the oracle could make NO signal decision for
    such a read (no heading, no matching or an ambiguous approach, a read
    off the approach leg, or a movement the program does not define) — the
    read can then never be a red-light violation.
    """
    if heading is None or not math.isfinite(float(heading)):
        return None
    h = float(heading) % 360.0
    plist = _norm_phases(phases)
    labels = {m for _g, movements in plist for m in movements}
    ranked = sorted(((_angle_diff(h, float(leg["bearing_deg"]) + 180.0),
                      int(leg["segment_id"]), leg) for leg in legs),
                    key=lambda x: (x[0], x[1]))
    if not ranked:
        return None
    compass = bool(labels) and labels <= _COMPASS
    delta, _sid, leg = ranked[0]
    if delta > _APPROACH_TOL_DEG:
        return None
    if compass:
        def movement_of(lg: Mapping[str, Any]) -> str | None:
            return _compass_movement(float(lg["bearing_deg"]) + 180.0)
    else:
        named = [movements for _g, movements in plist if movements]
        if not named:
            return None
        major = named[0][0]
        cross: str | None = None
        for movements in named[1:]:
            if "cross" in movements and major != "cross":
                cross = "cross"
                break
        if cross is None and len(named) > 1:
            others = [m for m in named[1] if m != major]
            cross = others[0] if others else None
        through = _through_legs(legs)

        def movement_of(lg: Mapping[str, Any]) -> str | None:
            return major if int(lg["segment_id"]) in through else cross

    movement = movement_of(leg)
    for delta2, _s2, leg2 in ranked[1:]:
        if delta2 - delta >= _APPROACH_AMBIGUITY_DEG:
            break
        if movement_of(leg2) != movement:
            return None
    if movement is None or movement not in labels:
        return None
    if segment_id is not None and int(segment_id) != int(leg["segment_id"]):
        own = next((lg for lg in legs if int(lg["segment_id"]) == int(segment_id)), None)
        if own is None:
            return None
        own_delta = _angle_diff(h, float(own["bearing_deg"]) + 180.0)
        if own_delta > _APPROACH_TOL_DEG or own_delta - delta >= _APPROACH_AMBIGUITY_DEG:
            return None
    return movement


def _signal_state(signal: Mapping[str, Any] | None, epoch_s: float) -> str:
    """green | amber | red | none for a camera site's approach at ``epoch_s``."""
    if not signal or not signal.get("movement"):
        return "none"
    states = movement_states_at(
        signal["cycle_sec"], signal.get("offset_sec", 0), signal["phases"], epoch_s
    )
    return states.get(str(signal["movement"]), "none")


@dataclass(frozen=True)
class CauseFrame:
    frame: int
    t_sec: float
    kind: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame": self.frame,
            "t_sec": round(self.t_sec, 3),
            "kind": self.kind,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class CauseTape:
    use_case_id: str
    seed: int
    t0_anchor_iso: str
    clock_scale: float
    movement_id: str
    frames: tuple[CauseFrame, ...]
    run_id: int | None = None

    def to_json(self) -> str:
        """Canonical, byte-stable serialization (tape file + JSONB ledger)."""
        doc: dict[str, Any] = {
            "tape_version": TAPE_VERSION,
            "use_case_id": self.use_case_id,
            "seed": self.seed,
            "t0_anchor": self.t0_anchor_iso,
            "clock_scale": self.clock_scale,
            "movement_id": self.movement_id,
            "frames": [f.to_dict() for f in self.frames],
        }
        if self.run_id is not None:
            doc["run_id"] = self.run_id
        return json.dumps(doc, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def from_dict(raw: dict) -> CauseTape:
        frames = tuple(
            CauseFrame(
                frame=int(f["frame"]),
                t_sec=float(f["t_sec"]),
                kind=str(f["kind"]),
                payload=f["payload"],
            )
            for f in raw["frames"]
        )
        run_raw = raw.get("run_id")
        return CauseTape(
            use_case_id=raw["use_case_id"],
            seed=int(raw["seed"]),
            t0_anchor_iso=raw["t0_anchor"],
            clock_scale=float(raw["clock_scale"]),
            movement_id=raw["movement_id"],
            frames=frames,
            run_id=int(run_raw) if run_raw is not None else None,
        )


# ── deterministic cause models ─────────────────────────────────────────────

# Time-of-day congestion base by local hour (IST) — the exogenous demand
# curve every scenario rides; linear interpolation between anchors.
_TOD_CURVE: tuple[tuple[int, float], ...] = (
    (0, 0.15), (5, 0.18), (7, 0.45), (9, 0.55), (12, 0.50),
    (16, 0.52), (18, 0.70), (21, 0.45), (24, 0.20),
)


def _tod_base(local_hour: float) -> float:
    h = local_hour % 24.0
    for (h0, v0), (h1, v1) in zip(_TOD_CURVE, _TOD_CURVE[1:]):
        if h0 <= h <= h1:
            frac = 0.0 if h1 == h0 else (h - h0) / (h1 - h0)
            return v0 + (v1 - v0) * frac
    return _TOD_CURVE[-1][1]


def _departure_local_hour(departure_time: str) -> float:
    """Local (as written, IST) clock hour of the use case's departure."""
    dep = datetime.fromisoformat(str(departure_time))
    return dep.hour + dep.minute / 60.0 + dep.second / 3600.0


def _piecewise(waypoints: tuple[tuple[float, float], ...], t: float) -> float:
    """Piecewise-linear interpolation over (t, value) waypoints."""
    if t <= waypoints[0][0]:
        return waypoints[0][1]
    if t >= waypoints[-1][0]:
        return waypoints[-1][1]
    for (t0, v0), (t1, v1) in zip(waypoints, waypoints[1:]):
        if t0 <= t <= t1:
            frac = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return v0 + (v1 - v0) * frac
    return waypoints[-1][1]


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _active(fe: dict, t: float) -> bool:
    return float(fe["start_sec"]) <= t < float(fe["start_sec"]) + float(fe["duration_sec"])


def _contribution(fe: dict) -> float:
    """One active cause's share of the remaining capacity (0..1).

    Spillovers load the road by their extra demand (veh/h over a ~3000
    veh/h arterial, capped at 0.35) or, without a demand figure, by
    severity; lane-loss incidents take 0.12; a green-corridor window 0.05.
    """
    et = fe["type"]
    if et in ("rally_spillover", "market_spillover"):
        demand = fe.get("extra_demand_veh_h")
        if demand is not None:
            return min(0.35, max(0.0, float(demand)) / 3000.0)
        return 0.3 * _clamp01(float(fe.get("severity", 0.0)))
    if et in ("vehicle_breakdown", "waterlogging", "signal_fault"):
        return 0.12
    if et == "emergency_vehicle":
        return 0.05
    return 0.0


def congestion_at(
    tod: float, scheduled: Iterable[dict], route: set[int], t: float
) -> float:
    """Noise-free congestion on the route at scenario second ``t``.

    Only causes ON the route contribute, and they combine softly:
    1 - (1 - tod) * prod(1 - c_i) — each cause takes its share of the
    capacity the others left, so stacked causes approach saturation
    without piling past it.
    """
    free = 1.0 - _clamp01(tod)
    for fe in scheduled:
        if int(fe["segment_id"]) in route and _active(fe, t):
            free *= 1.0 - _contribution(fe)
    return 1.0 - free


def _weather_severity_at(profile: UseCaseProfile, t: float) -> float:
    if profile.weather_steady_severity <= 0.0:
        return 0.02  # clear-sky baseline noise floor
    onset = max(profile.weather_onset_sec, 1.0)
    return _clamp01(profile.weather_steady_severity * min(1.0, t / onset))


def _ambulance_projection_at(
    profile: UseCaseProfile, events: list[dict], t: float
) -> float:
    """Ambulance-delay projection for the escalation channel.

    0 whenever no emergency_vehicle window is open (no ambulance, no
    projected delay — nothing to take over for); inside a window it decays
    from the profile's base toward its cleared value as the corridor
    clears in front of the ambulance.
    """
    if profile.ambulance_base_sec <= 0.0:
        return 0.0
    ev = [e for e in events if e.get("type") == "emergency_vehicle" and _active(e, t)]
    if not ev:
        return 0.0
    e0 = min(ev, key=lambda e: float(e["start_sec"]))
    frac = (t - float(e0["start_sec"])) / max(float(e0["duration_sec"]), 1.0)
    return profile.ambulance_base_sec + (
        profile.ambulance_cleared_sec - profile.ambulance_base_sec
    ) * min(1.0, max(0.0, frac))


def _route_anchor(
    route: list[int],
    index: int,
    segment_anchors: Mapping[int, tuple[float, float]] | None,
) -> tuple[float, float]:
    """Deterministic anchor N on the route (a stride over the segments'
    on-road anchors) or the belt."""
    if segment_anchors and route:
        positions = [segment_anchors[s] for s in route if s in segment_anchors]
        if positions:
            pos = positions[index % len(positions)]
            return (float(pos[0]), float(pos[1]))
    drift = index * 0.004
    return (_FALLBACK_ANCHOR[0] + drift, _FALLBACK_ANCHOR[1] - drift / 2.0)


def _anpr_sites(
    profile: UseCaseProfile,
    route: list[int],
    segment_anchors: Mapping[int, tuple[float, float]] | None,
    camera_sites: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict], list[dict]]:
    """(mid-block sweep sites, stop-line sites) the reads are placed at.

    With the registry (corridor.cameras, passed by the caller) reads sit ON
    the registered sites of the route: the EM-CAM sweep cameras (mid-block,
    out of every junction's 40 m stop-line radius) first, then stop-line
    approach sites (heading_deg given).  Without a registry the sweep
    cameras fall back to the route anchors (each segment's on-road midpoint)
    — no drift either way.
    """
    n_mid = int(getattr(profile, "anpr_cameras", 0) or 0)
    n_stop = int(getattr(profile, "anpr_stopline_cameras", 0) or 0)
    on_route = [dict(s) for s in (camera_sites or ())
                if s.get("segment_id") is not None and int(s["segment_id"]) in route]
    mid = sorted((s for s in on_route if s.get("junction_id") is None
                  and s.get("heading_deg") is None),
                 key=lambda s: str(s["camera_id"]))
    stop = sorted((s for s in on_route if s.get("junction_id") is not None
                   and s.get("heading_deg") is not None),
                  key=lambda s: str(s["camera_id"]))
    if not mid and n_mid > 0:
        for i in range(n_mid):
            lon, lat = _route_anchor(route, i, segment_anchors)
            mid.append({
                "camera_id": f"EM-CAM-{i + 1:02d}", "lon": lon, "lat": lat,
                "segment_id": route[i % len(route)] if route else None,
                "heading_deg": None, "speed_limit_kmh": None, "signal": None,
            })
    return mid[:n_mid], stop[:n_stop]


def _red_light_t_sec(
    site: Mapping[str, Any], t_sec: float, t0_epoch: float, clock_scale: float,
    latest: float, earliest: float | None = None,
) -> float:
    """The scenario second nearest ``t_sec`` — first at/after it (up to
    ``latest``), else before it (down to ``earliest``) — whose wall stamp
    falls in the RED of the site's approach; one cycle searched each way.
    Unchanged when no such second exists (a fast clock can compress the
    whole window into less than a cycle) or the site's signal is unknown —
    the oracle then decides on its own."""
    signal = site.get("signal")
    if not signal:
        return t_sec
    step = max(clock_scale, 1e-6)          # one wall second of scenario time
    cycle = int(signal.get("cycle_sec", 0) or 0)

    def red(candidate: float) -> bool:
        wall = t0_epoch + round(candidate, 3) / clock_scale
        return _signal_state(signal, wall) == "red"

    candidate = t_sec
    for _ in range(cycle + 1):
        if candidate > latest:
            break
        if red(candidate):
            return round(candidate, 3)
        candidate += step
    if earliest is not None:
        candidate = t_sec - step
        for _ in range(cycle + 1):
            if candidate < earliest:
                break
            if red(candidate):
                return round(candidate, 3)
            candidate -= step
    return t_sec


#: Road distance between two camera sites ≈ straight line × this (the old
#: city never runs straight).
_DETOUR_FACTOR = 1.35

#: The fleeing vehicle's hops spread over this share of the sweep window
#: (it goes to ground between cameras); traffic just drives through.
_CHASE_SHARE = 0.45


def _site_xy(site: Mapping[str, Any]) -> tuple[float, float]:
    return (float(site["lon"]), float(site["lat"]))


def _tour(sites: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The order a vehicle physically passes ``sites``: a nearest-next walk
    from the westmost site (ties by camera id) — deterministic."""
    remaining = sorted(sites, key=lambda s: (float(s["lon"]), str(s["camera_id"])))
    if not remaining:
        return []
    path = [remaining.pop(0)]
    while remaining:
        here = _site_xy(path[-1])
        nxt = min(remaining, key=lambda s: (_dist_m(here, _site_xy(s)), str(s["camera_id"])))
        remaining.remove(nxt)
        path.append(nxt)
    return path


def _travel_heading(site: Mapping[str, Any], toward: float | None) -> float | None:
    """The road's bearing under a mid-block camera, oriented with the
    vehicle's travel (``toward``: bearing to/from the neighbouring hop) —
    the legal direction on a one-way road."""
    bearing = site.get("bearing_deg")
    if bearing is None:
        return None if toward is None else round(toward % 360.0, 1)
    b = float(bearing) % 360.0
    if not site.get("oneway") and toward is not None:
        d = (toward - b) % 360.0
        if 90.0 < d < 270.0:
            b = (b + 180.0) % 360.0
    return round(b, 1)


def _build_anpr_frames(
    use_case_id: str,
    seed: int,
    run_id: int | None,
    profile: UseCaseProfile,
    route: list[int],
    horizon: float,
    segment_anchors: Mapping[int, tuple[float, float]] | None,
    camera_sites: Sequence[Mapping[str, Any]] | None,
    t0_epoch: float,
    clock_scale: float,
) -> list[CauseFrame]:
    """Deterministic camera reads (kind 'anpr_read', contracts C2 / C-E).

    Every vehicle drives ONE physically possible journey: it passes its
    cameras in path order (a nearest-next walk over the sites), and the
    time between two reads is never shorter than the road distance at its
    speed — so the numbered camera hops of a trace follow a route an
    operator can retrace, and no plate is ever in two places at once.

    Background traffic and the earmark-exercise plate drive through the
    mid-block sweep cameras at law-abiding speeds (either direction along
    the belt).  The FIR-named stolen vehicle flees: it reads above every
    limit, criss-crosses the sweep and stop-line cameras over a good part
    of the window, and runs the red at the stop-line sites (timed into the
    red of that approach when the caller passed its signal program).  The
    tape states only these causes — the oracle computes hits (from its
    earmarks) and rule outcomes.
    """
    mid, stop = _anpr_sites(profile, route, segment_anchors, camera_sites)
    if not mid and not stop:
        return []

    n_bg = int(getattr(profile, "anpr_background_vehicles", 0) or 0)
    vehicles: list[tuple[str, str, str]] = [
        (_plate_for(i), "background", "car") for i in range(n_bg)
    ]
    if getattr(profile, "anpr_stolen_plate_hit", False):
        vehicles.append((_STOLEN_LABEL, "stolen_fir_plate", "car"))
    if getattr(profile, "anpr_earmarked_plate_hit", False):
        vehicles.append((_EARMARKED_LABEL, "earmark_exercise_plate", "auto_rickshaw"))

    span = max(int(horizon) - 30, 60)
    window_end = float(horizon) - 15.0       # every read lands before the horizon
    stop_ids = {str(s["camera_id"]) for s in stop}
    belt = _tour(mid)
    flight = _tour(list(mid) + list(stop))
    frames: list[CauseFrame] = []

    for plate, role, vclass in vehicles:
        fleeing = role == "stolen_fir_plate"
        u = _uuid_int(use_case_id, seed, "journey", plate)
        if fleeing:
            path = flight
            t = 30.0 + float(u % max(span // 10, 1))      # the chase opens early
            pace = _CHASE_SHARE * span / max(len(path), 1)
        else:
            path = belt if (u >> 40) % 2 == 0 else list(reversed(belt))
            t = 15.0 + float(u % max(span // 2, 1))       # traffic enters all along
            pace = 0.0
        for k, site in enumerate(path):
            camera_id = str(site["camera_id"])
            ur = _uuid_int(use_case_id, seed, camera_id, plate)
            if fleeing:
                speed = 92.0 + float((ur >> 8) % 24)      # above every limit
            else:
                speed = compliant_speed_kmh(site.get("speed_limit_kmh"), ur >> 8)
            earliest = 15.0
            if k > 0:
                gap_m = _DETOUR_FACTOR * _dist_m(_site_xy(path[k - 1]), _site_xy(site))
                travel = gap_m / (speed / 3.6)
                earliest = t + travel
                # loiter between hops, but leave every remaining hop its
                # share of the window (travel time is never compressed)
                share = (window_end - t) / (len(path) - k)
                loiter = min(pace * (0.7 + 0.6 * ((ur >> 16) % 1000) / 999.0), share)
                t += max(travel, loiter)
            stop_line = camera_id in stop_ids
            if stop_line and fleeing:
                t = _red_light_t_sec(site, t, t0_epoch, clock_scale, window_end, earliest)
            if t > window_end:      # out of window: the vehicle left the cordon
                break
            if stop_line:
                heading = round(float(site["heading_deg"]), 1)
            else:
                toward = None
                if k > 0:
                    toward = _bearing_deg(_site_xy(path[k - 1]), _site_xy(site))
                elif len(path) > 1:
                    toward = _bearing_deg(_site_xy(site), _site_xy(path[1]))
                heading = _travel_heading(site, toward)
                if heading is None:
                    heading = float((ur >> 40) % 360)
            frames.append(CauseFrame(
                frame=0,
                t_sec=round(t, 3),
                kind="anpr_read",
                payload={
                    "camera_id": camera_id,
                    "plate": plate,
                    "position": [round(float(site["lon"]), 6), round(float(site["lat"]), 6)],
                    "speed_kmh": round(speed, 1),
                    "heading": round(heading, 1),
                    "vehicle_class": vclass,
                    "read_id": anpr_read_uuid(use_case_id, seed, camera_id, plate, run_id),
                    "role": role,
                },
            ))
    return frames


def _build_agency_frames(
    use_case_id: str,
    seed: int,
    profile: UseCaseProfile,
    route: list[int],
    horizon: float,
    segment_anchors: Mapping[int, tuple[float, float]] | None,
    segment_paths: Mapping[int, Sequence[Sequence[float]]] | None,
) -> list[CauseFrame]:
    """Deterministic agency-unit patrol frames (contracts C2 / C-A).

    Jan Rakshak two-wheeler beats riding the route's real geometry back and
    forth (one unit per half of the beat, opposite directions), a frame
    every AGENCY_CADENCE_SEC of tape time.  Every unit id is self-labelled
    EM (emulated).
    """
    n_units = int(getattr(profile, "agency_units", 0) or 0)
    if n_units <= 0:
        return []
    line: list[tuple[float, float]] = []
    if segment_paths:
        line = chain_polyline(route, segment_paths)
    if len(line) < 2 and segment_anchors:
        line = [segment_anchors[s] for s in route if s in segment_anchors]
    if len(line) < 2:
        a = _route_anchor(route, 0, None)
        line = [a, tuple(_offset_m(a, 600.0, 400.0))]
    length = polyline_length_m(line)
    speed_ms = _PATROL_SPEED_KMH / 3.6
    frames: list[CauseFrame] = []
    for u_i in range(n_units):
        unit_id = f"JR-EM-{u_i + 1:02d}"
        start = (2.0 * length) * u_i / max(n_units, 1)
        t = 0.0
        while t <= horizon + 1e-9:
            lon, lat, heading = pingpong_point(line, start + speed_ms * t)
            frames.append(CauseFrame(
                frame=0,
                t_sec=round(t, 3),
                kind="agency_unit",
                payload={
                    "unit_id": unit_id,
                    "agency": "jan_rakshak",
                    "label": f"Jan Rakshak {unit_id} · sweep patrol",
                    "position": [lon, lat],
                    "heading": heading,
                    "speed_kmh": _PATROL_SPEED_KMH,
                    "status": "patrol",
                },
            ))
            t += AGENCY_CADENCE_SEC
    return frames


def _build_track_frames(
    cfg: Mapping[str, Any],
    horizon: float,
    segment_paths: Mapping[int, Sequence[Sequence[float]]] | None,
) -> list[CauseFrame]:
    """Recorded agency-unit tracks a use case scripts (config ``unit_tracks``).

    Each unit rides its own ``segment_ids`` — chained in order — either as a
    ``patrol`` (back and forth over the whole horizon, ``offset`` a fraction
    of the beat) or a ``run`` (once, from ``start_sec``, at ``speed_kmh``,
    then held where it arrived): a frame every AGENCY_CADENCE_SEC of tape
    time, plus the arrival frame of a run — stationary, with the unit's
    ``arrival_status`` (a C-A spelling: 'arrived' reads on_scene live), so a
    REPLAY holding the last frame never shows an arrived unit as en route
    (a run the horizon cuts short keeps its moving status).  Geometry: the
    store's drawn segment paths when the run has them (COALESCE(display_geom,
    geom), C-K), else the config's ``route_geometry`` (the same segments as
    seeded), so the tape is identical on every host and the REPLAY animates
    them.
    EMULATED units only — every id carries EM; no person is modelled.
    """
    spec = cfg.get("unit_tracks") or {}
    library_lines = {
        int(sid): [(float(p[0]), float(p[1])) for p in pts]
        for sid, pts in (spec.get("route_geometry") or {}).items()
    }
    frames: list[CauseFrame] = []
    for unit in spec.get("units") or []:
        seg_ids = [int(s) for s in unit["segment_ids"]]
        lines = {
            s: (segment_paths or {}).get(s) or library_lines.get(s) for s in seg_ids
        }
        line = chain_polyline(seg_ids, {s: ln for s, ln in lines.items() if ln})
        if len(line) < 2:
            continue
        length = polyline_length_m(line)
        speed_kmh = float(unit["speed_kmh"])
        speed_ms = speed_kmh / 3.6
        start = float(unit.get("start_sec", 0.0))

        def frame(t: float, at: tuple[float, float, float], status: str,
                  speed: float = speed_kmh) -> CauseFrame:
            lon, lat, heading = at
            return CauseFrame(
                frame=0,
                t_sec=round(t, 3),
                kind="agency_unit",
                payload={
                    "unit_id": unit["unit_id"],
                    "agency": unit["agency"],
                    "label": unit["label"],
                    "position": [lon, lat],
                    "heading": heading,
                    "speed_kmh": speed,
                    "status": status,
                },
            )

        status = str(unit.get("status", "patrol"))
        if unit.get("mode") == "run":
            arrive = min(horizon, start + length / speed_ms)
            arrived = start + length / speed_ms <= horizon
            t = start
            while t < arrive - 1e-9:
                frames.append(frame(t, point_along(line, speed_ms * (t - start)), status))
                t += AGENCY_CADENCE_SEC
            at = point_along(line, speed_ms * (arrive - start))
            if arrived:
                frames.append(frame(arrive, at, str(unit.get("arrival_status", status)), 0.0))
            else:
                frames.append(frame(arrive, at, status))
        else:
            phase = float(unit.get("offset", 0.0)) * 2.0 * length
            t = start
            while t <= horizon + 1e-9:
                at = pingpong_point(line, phase + speed_ms * (t - start))
                frames.append(frame(t, at, status))
                t += AGENCY_CADENCE_SEC
    return frames


def build_tape(
    run: ResolvedRun,
    use_case: UseCase,
    profile: UseCaseProfile,
    segment_anchors: Mapping[int, tuple[float, float]] | None = None,
    *,
    run_id: int | None = None,
    segment_paths: Mapping[int, Sequence[Sequence[float]]] | None = None,
    camera_sites: Sequence[Mapping[str, Any]] | None = None,
) -> CauseTape:
    """Build the ordered cause tape.  Pure: same inputs → identical bytes.

    ``segment_anchors`` (each segment's on-road midpoint) /
    ``segment_paths`` / ``camera_sites`` (optional, from the store seam)
    anchor the watchlist generators to the REAL route geometry and the
    registered camera sites; without them a fixed
    fallback anchor is used — deterministic on every host either way, and
    replays re-post the ledgered tape verbatim regardless.
    """
    cfg = use_case.config
    horizon = use_case.horizon_sec
    tick = run.spec.tick_sec
    seed = run.seed
    rng = SeededRng(derive_seed("emulator:tape", run.spec.use_case_id, seed))

    route = list(cfg["route_segment_ids"])
    route_set = set(route)
    movement_id = movement_uuid(run.spec.use_case_id, seed, run_id)

    # ── scheduled_event causes ────────────────────────────────────────────
    scheduled: list[dict] = []
    for fe in cfg["forced_events"]:
        scheduled.append({**fe, "source": "use_case_config"})
    for fe in profile.extra_events:
        scheduled.append({**fe, "source": "profile"})
    scheduled.sort(key=lambda e: (float(e["start_sec"]), int(e["segment_id"])))

    # the scenario's OWN clock: departure_time's local hour + t, whatever
    # wall time the run is injected at
    departure_hour = _departure_local_hour(cfg["departure_time"])

    def _local_hour(t: float) -> float:
        return (departure_hour + t / 3600.0) % 24.0

    frames: list[CauseFrame] = []

    # ── event_config (t=0): the config's multipliers VERBATIM + the
    # additive ambulance_rate_multiplier (sixth override knob) ─────────────
    frames.append(CauseFrame(
        frame=0, t_sec=0.0, kind="event_config",
        payload={
            "event_overrides": dict(cfg["event_overrides"]),
            "ambulance_rate_multiplier": (
                1.0 + len([e for e in scheduled if e["type"] == "emergency_vehicle"])
            ),
            "note": (
                "event_overrides verbatim from UseCaseLibrary; "
                "ambulance_rate_multiplier is the additive sixth knob "
                "(deterministic counterpart of the random ambulance Poisson)"
            ),
        },
    ))

    # ── scheduled_event frames (verbatim causes) ──────────────────────────
    for e in scheduled:
        frames.append(CauseFrame(
            frame=0, t_sec=float(e["start_sec"]), kind="scheduled_event",
            payload=dict(e),
        ))

    # ── weather_ramp (one envelope frame) ─────────────────────────────────
    weather_cfg = dict(cfg["weather"])
    if profile.weather_steady_severity > 0.0:
        waypoints = (
            (0.0, 0.02),
            (profile.weather_onset_sec, profile.weather_steady_severity),
            (horizon, profile.weather_steady_severity),
        )
    else:
        waypoints = ((0.0, 0.02), (horizon, 0.03))
    frames.append(CauseFrame(
        frame=0, t_sec=0.0, kind="weather_ramp",
        payload={
            "monsoon": bool(weather_cfg.get("monsoon", False)),
            "rain_intensity_mm_h": float(weather_cfg.get("rain_intensity_mm_h", 0.0)),
            "weather_code": profile.weather_code,
            "severity_waypoints": [[round(t, 1), round(v, 3)] for t, v in waypoints],
        },
    ))

    # ── crowd_script (one envelope frame) ─────────────────────────────────
    frames.append(CauseFrame(
        frame=0, t_sec=0.0, kind="crowd_script",
        payload={
            "crowd_class": profile.crowd_class,
            "score_waypoints": [
                [round(t, 1), round(v, 3)] for t, v in profile.crowd_waypoints
            ],
        },
    ))

    # ── event_row frames (movement lifecycle in corridor-store) ───────────
    frames.append(CauseFrame(
        frame=0, t_sec=0.0, kind="event_row",
        payload={
            "action": "create",
            "movement_id": movement_id,
            "vvip_class": cfg["vvip_class"],
            "event_kind": profile.event_kind,
            "route_segment_ids": route,
            "planned_start_offset_sec": 0.0,
        },
    ))
    frames.append(CauseFrame(
        frame=0, t_sec=horizon, kind="event_row",
        payload={"action": "complete", "movement_id": movement_id},
    ))

    # ── anpr_read frames (watchlist generators, contracts C2 / C-E) ───────
    frames.extend(_build_anpr_frames(
        run.spec.use_case_id, seed, run_id, profile, route, horizon,
        segment_anchors, camera_sites,
        run.t0_anchor.timestamp(), run.spec.clock_scale,
    ))

    # ── agency_unit frames (patrolling units, contracts C2 / C-A) ─────────
    frames.extend(_build_agency_frames(
        run.spec.use_case_id, seed, profile, route, horizon,
        segment_anchors, segment_paths,
    ))
    # …and the units a use case scripts itself (the flagship's Jan Rakshak
    # beats and its 108 ambulance run), so a REPLAY shows them moving
    frames.extend(_build_track_frames(cfg, horizon, segment_paths))

    # ── inputs_doc frames (the exact engine_loop reader shape) ────────────
    t = 0.0
    while t <= horizon + 1e-9:
        cong = congestion_at(_tod_base(_local_hour(t)), scheduled, route_set, t)
        cong += rng.uniform(-0.02, 0.02)

        wsv = _weather_severity_at(profile, t) + rng.uniform(-0.01, 0.01)
        crowd = _piecewise(profile.crowd_waypoints, t) + rng.uniform(-0.02, 0.02)

        anomaly = any(
            _active(e, t) and e["type"] in
            ("vehicle_breakdown", "waterlogging", "signal_fault")
            and int(e["segment_id"]) in route_set
            for e in scheduled
        )
        doc: dict[str, Any] = {
            "predicted_congestion_idx": round(_clamp01(cong), 4),
            "weather_severity": round(_clamp01(wsv), 4),
            "crowd_score": round(_clamp01(crowd), 4),
            "anomaly_active": anomaly,
            "event_active": t < horizon,
            "last_gps_age_sec": round(
                rng.uniform(10.0, 25.0) if wsv > 0.5 else rng.uniform(0.0, 8.0), 2
            ),
            "ambulance_delay_projection_sec": round(
                _ambulance_projection_at(profile, scheduled, t), 1
            ),
        }
        if profile.weather_code is not None and _clamp01(wsv) > 0.3:
            doc["weather"] = profile.weather_code

        frames.append(CauseFrame(frame=0, t_sec=round(t, 3), kind="inputs_doc", payload=doc))
        t += tick

    # ── order + number ────────────────────────────────────────────────────
    kind_order = {k: i for i, k in enumerate(CAUSE_KINDS)}
    frames.sort(key=lambda f: (f.t_sec, kind_order.get(f.kind, 99)))
    frames = [CauseFrame(i, f.t_sec, f.kind, f.payload) for i, f in enumerate(frames)]

    return CauseTape(
        use_case_id=run.spec.use_case_id,
        seed=seed,
        t0_anchor_iso=run.t0_anchor.isoformat(),
        clock_scale=run.spec.clock_scale,
        movement_id=movement_id,
        frames=tuple(frames),
        run_id=run_id,
    )


def recorded_route(tape: CauseTape) -> list[int] | None:
    """The route segments a tape's event is created on (its event_row
    'create' frame) — what a replay re-animates; None when the tape
    creates no event."""
    for f in tape.frames:
        if f.kind == "event_row" and f.payload.get("action") == "create":
            return [int(s) for s in f.payload.get("route_segment_ids") or []]
    return None


def wall_time(t0_iso: str, t_sec: float, clock_scale: float) -> float:
    """Absolute unix time at which a frame fires: t0 + t/clock_scale."""
    t0 = datetime.fromisoformat(t0_iso)
    return t0.timestamp() + t_sec / clock_scale
