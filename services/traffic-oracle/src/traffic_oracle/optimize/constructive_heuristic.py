"""Constructive heuristics for public traffic diversion planning.

Computes alternative routes for public traffic displaced by convoy road
closures, plus the activation/deactivation times and queue length
estimates that go with each closure.

Graph: the corridor TURN graph.  One movement per
``corridor.segment_adjacency`` row — ``from → to`` made AT junction
``via_junction_id`` — whose pair is a ``corridor.road_graph`` edge (directed
since migration 012), costed with the cost pgRouting routes on
(``turn_cost_sec + length_m / (speed_limit_kmh / 3.6)`` of the FROM
segment).  A segment's junctions (its "sides") are the junctions its
movements are made at: its two ends, plus any junction where a road
crosses it mid-way (a walk may then enter or leave it at any of them; the
real OpenStreetMap network has none — every segment runs junction to
junction — and the toy tests keep the case).  A one-way segment is
driven from the junction nearest its geometry's start point to its other
side.

Detour (per closed segment ``b`` with sides ``ja → jb``): the cheapest
walk that LEAVES junction ``ja`` on another segment and ARRIVES at junction
``jb`` without using ``b``.  The search runs over (segment, junction
reached) states, so every segment of the walk is entered at one of its
junctions and left at another — two neighbours of ``b`` at the SAME
junction can never pose as a way around it.  (The previous pairwise search
did exactly that: closing the Nehru Bridge 1015 "diverted" traffic over
[1006, 1001], both at the bridge's western junction, although the corridor
graph did not connect its two banks at all.)

* Orientation: the displaced through movement fixes ``ja`` (junction shared
  with the previous route segment) and ``jb`` (shared with the next one); a
  one-way ``b`` is only ever driven its own way.  An end the route does
  not pin (first / last route segment) may be any other junction of ``b``
  — the cheapest detour found wins — and only when neither end is pinned
  are both directions tried.  No other movement is substituted when the
  through movement has no path.
* No path between the two sides (a bridge) → ``detour_status='no_path'``;
  a segment touching a single junction (a stub — no through movement to
  divert) → ``'dead_end'``.  Both report an empty ``alt_segment_ids`` and
  ``detour_found=False`` instead of inventing a detour.
* A single-junction segment inside a walk is a TURNAROUND (enter, turn
  back, leave at the same junction) — the routing graph allows it and the
  seed's turn lists need it at some junctions — and is reported in
  ``turnaround_segment_ids`` so the operator sees the U-turn.  One-way
  segments are never driven against their direction nor used to turn back.
* Rolling closures: every route segment has its own closure window
  ``[activate_at, deactivate_at]``.  A route segment whose window OVERLAPS
  the closed segment's is closed at the same time, so a detour never enters
  it (exactly like the closed segment itself) — the detour around Airport
  Road S used to run over Airport Road N while both were shut.  A route
  segment whose window does not overlap is open while this detour is in use:
  it costs ``ROUTE_SEGMENT_PENALTY_SEC`` extra (a poor, not impossible,
  alternative) and is reported in ``uses_route_segment_ids`` so the operator
  sees the detour leans on the convoy's road before or after its closure.
* PRACTICALITY: a detour longer than :func:`detour_limit_m` — max(5 km,
  6 × the closed segment's length) — is not offered as a diversion.  When
  the cheapest walk is longer, the cheapest walk WITHIN the limit is
  searched (resource-constrained, :meth:`DiversionHeuristic._practical_walk`)
  and offered with a labelled reason; when there is none the closure is
  ``detour_status='impractical'``: no alt segments, ``detour_distance_m``
  = the cheapest walk's length, and a reason telling the operator to hold
  or meter traffic at the closure instead (the real network's case:
  Airport Road (South), 3.0 km, whose cheapest way round — the ring and
  the Indira Bridge — is 32.6 km against a 17.8 km limit).
* ``entry_pinned`` / ``exit_pinned``: whether the detour's from / to
  junction was FIXED — by the displaced through movement, a one-way's
  direction, or being the only junction left — rather than chosen among
  several.  An unpinned end means the detour may cover only part of the
  closed segment (a first / last route segment crossed mid-way: its detour
  may leave from the crossing, not the segment's end — the toy tests keep
  the case; on the real network every end is pinned) — consumers say
  'partial'.

``alt_segment_ids`` is the detour itself, ``[first segment leaving ja, ...,
last segment arriving at jb]``; ``detour_distance_m`` is its length (each
segment once — the routing cost's convention).  The graph is small (the
city segment graph, thousands of nodes at most) and rebuilt every
``graph_ttl_sec`` so geometry fixes reach the planner without a restart.
"""

from __future__ import annotations

import heapq
import itertools
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import structlog

from traffic_oracle.data.graph_queries import CorridorGraphDB
from traffic_oracle.runtime.memory_profiler import profile_memory

logger = structlog.get_logger(__name__)

#: Extra cost (s) for routing a detour over another convoy-route segment
#: whose closure window does not overlap the closed segment's (one that
#: does is never entered).
ROUTE_SEGMENT_PENALTY_SEC = 900.0
#: Fallback traversal speed when a segment has no usable speed limit.
_FALLBACK_SPEED_KMH = 50.0
#: Fallback segment length when the graph has no geometry for a segment.
DEFAULT_SEGMENT_LENGTH_M = 500.0

#: ``detour_status`` vocabulary.
DETOUR_FOUND = "found"
DETOUR_NO_PATH = "no_path"
DETOUR_DEAD_END = "dead_end"
DETOUR_IMPRACTICAL = "impractical"

#: Practicality: a detour longer than max(DETOUR_MAX_MIN_M,
#: DETOUR_MAX_FACTOR × the closed segment's length) is not offered.
DETOUR_MAX_MIN_M = 5000.0
DETOUR_MAX_FACTOR = 6.0


def detour_limit_m(closed_length_m: float) -> float:
    """The longest detour (m) worth offering around a closure this long."""
    return max(DETOUR_MAX_MIN_M, DETOUR_MAX_FACTOR * float(closed_length_m))


def _km(metres: float) -> str:
    return f"{metres / 1000.0:.1f} km"


# Movements with their junction.  road_graph carries the routing cost but
# drops via_junction_id, so it is joined back to segment_adjacency on
# (source, target).  The 012 view admits no pair whose only movement
# enters a one-way segment away from its start or leaves it away from its
# end (Station Road 1014 -> Gandhi Road 1012 at J15, the END of one-way
# Gandhi Road); _ends judges one-way direction itself as well.  A pair that
# meets at two junctions — Relief Road 1005 and Gandhi Road 1012 both run
# J3-J15 — is ONE road_graph edge (012 keeps its cheapest turn, so
# pgRouting's edge id stays unique) and comes back once per junction, at
# that edge's cost.
_SQL_TURNS = """
SELECT rg.source AS from_segment_id, rg.target AS to_segment_id,
       sa.via_junction_id, sa.turn_cost_sec, rg.cost AS travel_cost_sec,
       ST_X(j.geom) AS via_lon, ST_Y(j.geom) AS via_lat
FROM corridor.road_graph rg
JOIN corridor.segment_adjacency sa
  ON sa.from_segment_id = rg.source AND sa.to_segment_id = rg.target
JOIN corridor.junctions j ON j.junction_id = sa.via_junction_id
"""

_SQL_SEGMENT_ATTRS = """
SELECT segment_id, ST_Length(geom::geography) AS length_m, speed_limit_kmh,
       oneway, ST_X(ST_StartPoint(geom)) AS start_lon,
       ST_Y(ST_StartPoint(geom)) AS start_lat
FROM corridor.road_segments
"""

_State = tuple[int, int]  # (segment, junction reached on it)
_SINK: _State = (-1, -1)


@dataclass(frozen=True)
class Detour:
    """Outcome of one detour search around a closed segment.

    ``length_m`` is set when the search ran with a practicality limit: the
    offered walk's length, or for ``impractical`` the cheapest walk's
    (which is then NOT in ``segment_ids`` — nothing is offered).
    """

    status: str
    segment_ids: tuple[int, ...] = ()
    from_junction_id: int | None = None
    to_junction_id: int | None = None
    turnaround_segment_ids: tuple[int, ...] = ()
    cost_sec: float = 0.0
    entry_pinned: bool | None = None
    exit_pinned: bool | None = None
    length_m: float | None = None
    limit_m: float | None = None
    reason: str | None = None

    @property
    def found(self) -> bool:
        return self.status == DETOUR_FOUND


@dataclass
class DiversionRoute:
    """A computed diversion for a specific blocked segment."""

    blocked_segment_id: int
    alt_segment_ids: list[int]
    activate_at: datetime
    deactivate_at: datetime
    estimated_queue_m: float
    dissipation_time_sec: int
    detour_distance_m: float | None
    detour_found: bool = True
    detour_status: str = DETOUR_FOUND
    detour_from_junction_id: int | None = None
    detour_to_junction_id: int | None = None
    turnaround_segment_ids: list[int] = field(default_factory=list)
    # additive (WP-16): whether each end of the detour was fixed rather
    # than chosen (None for a dead end — there is no detour to pin), the
    # practicality limit it was held to, and a labelled reason whenever the
    # status needs one (impractical, no_path, dead_end, or a practical
    # detour offered instead of the cheapest).
    entry_pinned: bool | None = None
    exit_pinned: bool | None = None
    detour_limit_m: float | None = None
    detour_reason: str | None = None
    # additive (WP-17, C-R): convoy-route segments the detour uses — each
    # one open while this closure is (its own window does not overlap);
    # empty when the detour stays off the convoy's road
    uses_route_segment_ids: list[int] = field(default_factory=list)


def closure_windows_overlap(
    a: tuple[datetime, datetime], b: tuple[datetime, datetime]
) -> bool:
    """Whether two closure windows ``(activate_at, deactivate_at)`` share time."""
    return a[0] < b[1] and b[0] < a[1]


def _planar_sq_dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Squared equirectangular distance (ranking only, city scale)."""
    k = math.cos(math.radians((a[1] + b[1]) / 2.0))
    return ((a[0] - b[0]) * k) ** 2 + (a[1] - b[1]) ** 2


class DiversionHeuristic:
    """Junction-aware diversion search over the corridor turn graph."""

    def __init__(self, graph_db: CorridorGraphDB, graph_ttl_sec: float = 600.0) -> None:
        self._graph_db = graph_db
        self._graph_ttl_sec = graph_ttl_sec
        #: segment → its junctions (sorted); None until the graph is built.
        self._sides: dict[int, tuple[int, ...]] | None = None
        #: (segment, junction) → [(next segment, movement cost s)], sorted.
        self._moves: dict[_State, list[tuple[int, float]]] = {}
        #: junction → segments touching it (sorted).
        self._at_junction: dict[int, tuple[int, ...]] = {}
        #: (from, to) → junctions the movement is made at.
        self._movement_vias: dict[tuple[int, int], frozenset[int]] = {}
        #: one-way segment → the junction it is entered at.
        self._oneway_entry: dict[int, int] = {}
        self._lengths: dict[int, float] = {}
        self._traverse_sec: dict[int, float] = {}
        self._built_at: float | None = None

    @property
    def is_built(self) -> bool:
        return self._sides is not None

    @profile_memory
    async def build_graph(self) -> None:
        """Load corridor movements (road_graph cost + junction) and segments."""
        pool = self._graph_db._pool
        async with pool.acquire() as conn:
            turn_rows = await conn.fetch(_SQL_TURNS)
            seg_rows = await conn.fetch(_SQL_SEGMENT_ATTRS)

        lengths: dict[int, float] = {}
        speeds: dict[int, float] = {}
        oneway_start: dict[int, tuple[float, float]] = {}
        for r in seg_rows:
            sid = int(r["segment_id"])
            if r["length_m"] is not None:
                lengths[sid] = float(r["length_m"])
            if r["speed_limit_kmh"]:
                speeds[sid] = float(r["speed_limit_kmh"])
            if r["oneway"] and r["start_lon"] is not None and r["start_lat"] is not None:
                oneway_start[sid] = (float(r["start_lon"]), float(r["start_lat"]))

        def traverse_sec(sid: int) -> float:
            speed = speeds.get(sid) or _FALLBACK_SPEED_KMH
            return lengths.get(sid, DEFAULT_SEGMENT_LENGTH_M) / (speed / 3.6)

        sides: dict[int, set[int]] = {}
        junction_xy: dict[int, tuple[float, float]] = {}
        rows: list[tuple[int, int, int, float]] = []
        for r in turn_rows:
            src, tgt = int(r["from_segment_id"]), int(r["to_segment_id"])
            via = int(r["via_junction_id"])
            if src == tgt:
                continue
            cost = r["travel_cost_sec"]
            if cost is None or not math.isfinite(float(cost)) or float(cost) <= 0:
                # road_graph cost is NULL when speed_limit_kmh is 0/NULL:
                # same formula with the fallback speed (never a free edge).
                turn = r["turn_cost_sec"]
                turn = float(turn) if turn is not None and math.isfinite(float(turn)) else 0.0
                cost = max(turn, 0.0) + traverse_sec(src)
            rows.append((src, tgt, via, float(cost)))
            sides.setdefault(src, set()).add(via)
            sides.setdefault(tgt, set()).add(via)
            if r["via_lon"] is not None and r["via_lat"] is not None:
                junction_xy[via] = (float(r["via_lon"]), float(r["via_lat"]))

        oneway_entry: dict[int, int] = {}
        for sid, start in oneway_start.items():
            candidates = [j for j in sorted(sides.get(sid, ())) if j in junction_xy]
            if candidates:
                oneway_entry[sid] = min(
                    candidates, key=lambda j: _planar_sq_dist(junction_xy[j], start)
                )

        # A pair that meets at two junctions is one road_graph edge, and the
        # join brings back the movement at EACH junction — for a one-way
        # segment one of them is not a movement at all (Relief Road 1005
        # meets one-way Gandhi Road 1012 at both ends: 1005 -> 1012 is made at
        # J3, never at J15, and 1012 -> 1005 at J15, never at J3).  Keep 012's
        # rule here too: a one-way is entered at its entry junction only and
        # never left there.
        moves: dict[_State, dict[int, float]] = {}
        vias: dict[tuple[int, int], set[int]] = {}
        for src, tgt, via, cost in rows:
            if tgt in oneway_entry and via != oneway_entry[tgt]:
                continue
            if src in oneway_entry and via == oneway_entry[src] and len(sides[src]) > 1:
                continue
            bucket = moves.setdefault((src, via), {})
            if cost < bucket.get(tgt, math.inf):
                bucket[tgt] = cost
            vias.setdefault((src, tgt), set()).add(via)

        at_junction: dict[int, set[int]] = {}
        for sid, js in sides.items():
            for j in js:
                at_junction.setdefault(j, set()).add(sid)

        self._moves = {k: sorted(v.items()) for k, v in moves.items()}
        self._sides = {sid: tuple(sorted(js)) for sid, js in sides.items()}
        self._at_junction = {j: tuple(sorted(s)) for j, s in at_junction.items()}
        self._movement_vias = {k: frozenset(v) for k, v in vias.items()}
        self._oneway_entry = oneway_entry
        self._lengths = lengths
        self._traverse_sec = {sid: traverse_sec(sid) for sid in sides}
        self._built_at = time.monotonic()
        logger.info(
            "diversion_heuristic.graph_built",
            segments=len(sides),
            movements=len(turn_rows),
            junctions=len(at_junction),
            oneway_segments=len(oneway_entry),
        )

    async def _ensure_graph(self) -> None:
        stale = (
            self._built_at is None
            or time.monotonic() - self._built_at > self._graph_ttl_sec
        )
        if self._sides is None or stale:
            await self.build_graph()

    async def segment_lengths(self, segment_ids: list[int]) -> dict[int, float]:
        """Real lengths (m) from corridor.road_segments for known segments."""
        await self._ensure_graph()
        return {sid: self._lengths[sid] for sid in segment_ids if sid in self._lengths}

    def knows(self, segment_id: int) -> bool:
        return self._sides is not None and segment_id in self._sides

    @profile_memory
    async def compute_diversions(
        self,
        convoy_route: list[int],
        predicted_speeds: dict[int, float],
        segment_lengths: dict[int, float] | None = None,
        departure_time: datetime | None = None,
        advance_closure_sec: int = 120,
    ) -> list[DiversionRoute]:
        """Compute diversion routes for each blocked segment along the convoy route.

        Args:
            convoy_route: Ordered segment IDs of convoy path
            predicted_speeds: segment_id → predicted speed (km/h)
            segment_lengths: segment_id → length in meters (None → the
                real lengths loaded from corridor.road_segments)
            departure_time: Convoy departure time (default: now)
            advance_closure_sec: Seconds before convoy arrival to activate closure

        Returns:
            List of DiversionRoute, one per blocked segment known to the graph
        """
        await self._ensure_graph()

        if departure_time is None:
            departure_time = datetime.now(timezone.utc)

        def length_of(sid: int) -> float:
            if segment_lengths is not None and sid in segment_lengths:
                return float(segment_lengths[sid])
            return self._lengths.get(sid, DEFAULT_SEGMENT_LENGTH_M)

        route_set = set(convoy_route)
        diversions = []

        # Compute cumulative convoy arrival time at each segment
        cumulative_sec = 0.0
        segment_etas: dict[int, float] = {}
        for sid in convoy_route:
            segment_etas[sid] = cumulative_sec
            speed = predicted_speeds.get(sid, 40.0)
            travel_sec = length_of(sid) / max(speed * 1000 / 3600, 0.1)
            cumulative_sec += travel_sec

        # Every route segment's closure window, BEFORE any detour is
        # searched: a detour may not run over a route segment that is shut
        # at the same time (rolling closures overlap their neighbours).
        windows: dict[int, tuple[datetime, datetime]] = {}
        for sid in convoy_route:
            eta_sec = segment_etas[sid]
            speed = predicted_speeds.get(sid, 40.0)
            traverse_sec = length_of(sid) / max(speed * 1000 / 3600, 0.1)
            windows[sid] = (
                departure_time + timedelta(seconds=max(0, eta_sec - advance_closure_sec)),
                departure_time + timedelta(seconds=eta_sec + traverse_sec + 30),  # 30s buffer
            )

        for pos, sid in enumerate(convoy_route):
            if not self.knows(sid):
                continue

            activate, deactivate = windows[sid]
            # Route segments closed while this one is: never entered.
            blocked_now = frozenset(
                other for other in route_set
                if other != sid and closure_windows_overlap(windows[other], windows[sid])
            )

            # Path around the closed segment, from one of its ends to the
            # other — no longer than the practicality limit for its length.
            prev_sid = convoy_route[pos - 1] if pos > 0 else None
            next_sid = convoy_route[pos + 1] if pos + 1 < len(convoy_route) else None
            limit_m = detour_limit_m(length_of(sid))
            detour = self._find_detour(
                sid, route_set, prev_sid, next_sid, max_length_m=limit_m, length_of=length_of,
                blocked_now=blocked_now,
            )

            # Queue estimate: inflow_rate × closure_duration × spacing
            closure_duration = (deactivate - activate).total_seconds()
            congestion_factor = 1.0  # simplified
            inflow_rate = 0.3  # vehicles/second default
            avg_spacing = 8.0  # meters per queued vehicle
            queue_m = inflow_rate * closure_duration * avg_spacing * congestion_factor

            # Queue dissipation: ~2 seconds per queued vehicle
            dissipation_sec = int(queue_m / avg_spacing * 2)

            # Distance driven INSTEAD of the closed segment: the whole detour
            # walk from one end of the closure to the other — for an
            # impractical closure the cheapest walk's length (not offered).
            detour_dist = round(detour.length_m, 1) if detour.length_m is not None else None

            diversions.append(DiversionRoute(
                blocked_segment_id=sid,
                alt_segment_ids=list(detour.segment_ids),
                activate_at=activate,
                deactivate_at=deactivate,
                estimated_queue_m=round(queue_m, 1),
                dissipation_time_sec=dissipation_sec,
                detour_distance_m=detour_dist,
                detour_found=detour.found,
                detour_status=detour.status,
                detour_from_junction_id=detour.from_junction_id,
                detour_to_junction_id=detour.to_junction_id,
                turnaround_segment_ids=list(detour.turnaround_segment_ids),
                entry_pinned=detour.entry_pinned,
                exit_pinned=detour.exit_pinned,
                detour_limit_m=(
                    round(detour.limit_m, 1) if detour.limit_m is not None else None
                ),
                detour_reason=detour.reason,
                uses_route_segment_ids=list(
                    dict.fromkeys(s for s in detour.segment_ids if s in route_set)
                ),
            ))

        diversions.sort(key=lambda d: d.activate_at)

        logger.info(
            "diversion_heuristic.computed",
            diversions=len(diversions),
            route_segments=len(convoy_route),
            without_detour=sum(1 for d in diversions if not d.detour_found),
            no_path=sum(1 for d in diversions if d.detour_status == DETOUR_NO_PATH),
            dead_end=sum(1 for d in diversions if d.detour_status == DETOUR_DEAD_END),
            impractical=sum(1 for d in diversions if d.detour_status == DETOUR_IMPRACTICAL),
            over_route_segments=sum(1 for d in diversions if d.uses_route_segment_ids),
        )
        return diversions

    # -- detour search ------------------------------------------------------------

    def _length(self, segment: int) -> float:
        return self._lengths.get(segment, DEFAULT_SEGMENT_LENGTH_M)

    def _find_detour(
        self,
        blocked_segment: int,
        route_set: set[int],
        prev_sid: int | None = None,
        next_sid: int | None = None,
        *,
        max_length_m: float | None = None,
        length_of: Callable[[int], float] | None = None,
        blocked_now: frozenset[int] = frozenset(),
    ) -> Detour:
        """Cheapest walk from one end of a closed segment to the other.

        With ``max_length_m`` the walk is also held to the practicality
        limit: a cheapest walk within it is returned as found (labelled
        when it is not the cheapest overall), and when no walk fits the
        result is ``impractical`` — nothing offered, the cheapest walk's
        length reported.  ``length_of`` measures segments (default: the
        graph's own lengths).  ``blocked_now`` — the route segments closed
        while this one is (overlapping windows) — is never entered, like
        the closed segment itself; the rest of ``route_set`` is penalised.
        """
        sides = self._sides.get(blocked_segment, ()) if self._sides is not None else ()
        if len(sides) < 2:
            return Detour(
                status=DETOUR_DEAD_END,
                reason="dead end: the closed segment meets a single junction — "
                "no through traffic to divert",
            )

        orientations = self._orientations(blocked_segment, sides, prev_sid, next_sid)
        # An end is pinned when the search had ONE candidate junction for it
        # (displaced movement, one-way direction, or the only junction left).
        pins = {
            "entry_pinned": len({a for a, _ in orientations}) == 1,
            "exit_pinned": len({b for _, b in orientations}) == 1,
        }
        best: Detour | None = None
        for ja, jb in orientations:
            found = self._junction_walk(blocked_segment, ja, jb, route_set, blocked_now)
            if found is not None and (best is None or found.cost_sec < best.cost_sec):
                best = found
        if best is None:
            ja, jb = orientations[0]
            also_shut = (
                f" or the route segments closed at the same time "
                f"({', '.join(str(s) for s in sorted(blocked_now))})"
                if blocked_now else ""
            )
            return Detour(
                status=DETOUR_NO_PATH,
                from_junction_id=ja,
                to_junction_id=jb,
                limit_m=max_length_m,
                reason=f"no way round: nothing joins junction {ja} to junction {jb} "
                f"without the closed segment{also_shut} (one-way rules included)",
                **pins,
            )
        if max_length_m is None:
            return replace(best, **pins)

        measure = length_of or self._length
        best_m = sum(measure(s) for s in best.segment_ids)
        if best_m <= max_length_m:
            return replace(best, length_m=best_m, limit_m=max_length_m, **pins)
        practical: Detour | None = None
        for ja, jb in orientations:
            found = self._practical_walk(
                blocked_segment, ja, jb, route_set, max_length_m, measure, blocked_now
            )
            if found is not None and (practical is None or found.cost_sec < practical.cost_sec):
                practical = found
        if practical is not None:
            return replace(
                practical,
                length_m=sum(measure(s) for s in practical.segment_ids),
                limit_m=max_length_m,
                reason=f"the cheapest detour ({_km(best_m)}) is longer than the "
                f"{_km(max_length_m)} practicality limit — the cheapest detour "
                "within the limit is offered instead",
                **pins,
            )
        closed_m = measure(blocked_segment)
        return Detour(
            status=DETOUR_IMPRACTICAL,
            from_junction_id=best.from_junction_id,
            to_junction_id=best.to_junction_id,
            cost_sec=best.cost_sec,
            length_m=best_m,
            limit_m=max_length_m,
            reason=f"impractical: the cheapest way round is {_km(best_m)}, beyond the "
            f"{_km(max_length_m)} practicality limit (the larger of "
            f"{_km(DETOUR_MAX_MIN_M)} and {DETOUR_MAX_FACTOR:g} × the {_km(closed_m)} "
            "closure) and no way round fits it — no diversion is offered; hold or "
            "meter traffic at the closure instead",
            **pins,
        )

    def _shared_junctions(self, a: int | None, b: int | None) -> frozenset[int]:
        """Junctions where a movement between ``a`` and ``b`` is made."""
        if a is None or b is None:
            return frozenset()
        return self._movement_vias.get((a, b), frozenset()) | self._movement_vias.get(
            (b, a), frozenset()
        )

    def _orientations(
        self,
        blocked: int,
        sides: tuple[int, ...],
        prev_sid: int | None,
        next_sid: int | None,
    ) -> list[tuple[int, int]]:
        """``(ja, jb)`` pairs to search, the displaced movement's first."""
        entry = self._oneway_entry.get(blocked)
        if entry is not None and entry in sides:
            # Public traffic only ever drives a one-way its own way, whatever
            # direction the (escorted) convoy takes.
            return [(entry, j) for j in sides if j != entry]
        from_route = self._shared_junctions(prev_sid, blocked)
        to_route = self._shared_junctions(blocked, next_sid)
        pairs = [
            (a, b)
            for a in sides
            for b in sides
            if a != b
            and (not from_route or a in from_route)
            and (not to_route or b in to_route)
        ]
        # A route that does not pin the orientation (first/only segment, or
        # neighbours that do not touch b) → both directions are candidates.
        return pairs or [(a, b) for a in sides for b in sides if a != b]

    def _ends(self, segment: int, entered_at: int) -> tuple[int, ...]:
        """Junctions reachable by driving ``segment`` from ``entered_at``."""
        entry = self._oneway_entry.get(segment)
        if entry is not None and entered_at != entry:
            return ()  # never against a one-way
        sides = self._sides.get(segment, ()) if self._sides is not None else ()
        others = tuple(j for j in sides if j != entered_at)
        if others:
            return others
        # Single-junction segment: a turnaround (never on a one-way).
        return () if entry is not None else (entered_at,)

    def _junction_walk(
        self,
        blocked: int,
        ja: int,
        jb: int,
        route_set: set[int],
        blocked_now: frozenset[int] = frozenset(),
    ) -> Detour | None:
        """Dijkstra over (segment, junction reached) states from ``ja`` to ``jb``.

        Cost: the road_graph cost of every movement (traversal of the FROM
        segment + its turn), the traversal of the last segment, and
        ``ROUTE_SEGMENT_PENALTY_SEC`` per convoy-route segment entered.
        ``blocked`` and ``blocked_now`` (route segments closed at the same
        time) are never entered.
        """
        assert self._sides is not None
        dist: dict[_State, float] = {}
        parent: dict[_State, _State | None] = {}
        heap: list[tuple[float, int, _State]] = []
        order = itertools.count()

        def relax(state: _State, cost: float, came_from: _State | None) -> None:
            if cost < dist.get(state, math.inf):
                dist[state] = cost
                parent[state] = came_from
                heapq.heappush(heap, (cost, next(order), state))

        def penalty(segment: int) -> float:
            return ROUTE_SEGMENT_PENALTY_SEC if segment in route_set else 0.0

        for first in self._at_junction.get(ja, ()):
            if first == blocked or first in blocked_now:
                continue
            for end in self._ends(first, ja):
                relax((first, end), penalty(first), None)

        while heap:
            cost, _, state = heapq.heappop(heap)
            if cost > dist.get(state, math.inf):
                continue
            if state == _SINK:
                walk: list[_State] = []
                node = parent[_SINK]
                while node is not None:
                    walk.append(node)
                    node = parent[node]
                walk.reverse()
                segments = tuple(s for s, _ in walk)
                return Detour(
                    status=DETOUR_FOUND,
                    segment_ids=segments,
                    from_junction_id=ja,
                    to_junction_id=jb,
                    turnaround_segment_ids=tuple(
                        dict.fromkeys(s for s in segments if len(self._sides.get(s, ())) < 2)
                    ),
                    cost_sec=round(cost, 1),
                )
            segment, at = state
            if at == jb:
                # Arrived at the far end: close the walk with the last
                # segment's own traversal (no further movement is costed).
                relax(_SINK, cost + self._traverse_sec.get(segment, 0.0), state)
                continue
            for nxt, move_cost in self._moves.get(state, ()):
                if nxt == blocked or nxt in blocked_now:
                    continue
                for end in self._ends(nxt, at):
                    relax((nxt, end), cost + move_cost + penalty(nxt), state)
        return None

    def _practical_walk(
        self,
        blocked: int,
        ja: int,
        jb: int,
        route_set: set[int],
        max_length_m: float,
        length_of: Callable[[int], float],
        blocked_now: frozenset[int] = frozenset(),
    ) -> Detour | None:
        """Cheapest walk from ``ja`` to ``jb`` no longer than ``max_length_m``.

        The same moves, costs, penalties and one-way rules as
        :meth:`_junction_walk` (``blocked`` and ``blocked_now`` are never
        entered); a walk's
        length counts each of its segments once (``detour_distance_m``'s
        convention).  Resource-constrained label setting: every
        (segment, junction) state keeps the labels no other label there
        beats on BOTH cost and length — on the city segment graph these
        fronts stay tiny — and labels pop in cost order, so the first one
        to reach the sink is the cheapest walk within the limit.  Costs and
        lengths are strictly positive, so a label that loops back to a
        state is dominated there and the search terminates.  None when
        every way round is longer than the limit.
        """
        assert self._sides is not None
        labels: list[tuple[_State, int | None]] = []  # label → (state, parent label)
        alive: list[bool] = []
        fronts: dict[_State, list[tuple[float, float, int]]] = {}
        heap: list[tuple[float, float, int]] = []

        def penalty(segment: int) -> float:
            return ROUTE_SEGMENT_PENALTY_SEC if segment in route_set else 0.0

        def push(state: _State, cost: float, length: float, parent: int | None) -> None:
            if length > max_length_m:
                return
            front = fronts.get(state, [])
            if any(c <= cost and m <= length for c, m, _ in front):
                return  # an existing label is at least as cheap AND as short
            kept = []
            for c, m, other in front:
                if cost <= c and length <= m:
                    alive[other] = False  # the new label beats it on both
                else:
                    kept.append((c, m, other))
            label = len(labels)
            labels.append((state, parent))
            alive.append(True)
            kept.append((cost, length, label))
            fronts[state] = kept
            heapq.heappush(heap, (cost, length, label))

        for first in self._at_junction.get(ja, ()):
            if first == blocked or first in blocked_now:
                continue
            for end in self._ends(first, ja):
                push((first, end), penalty(first), length_of(first), None)

        while heap:
            cost, length, label = heapq.heappop(heap)
            if not alive[label]:
                continue
            state, parent = labels[label]
            if state == _SINK:
                walk: list[_State] = []
                node = parent
                while node is not None:
                    walked, node = labels[node]
                    walk.append(walked)
                walk.reverse()
                segments = tuple(s for s, _ in walk)
                return Detour(
                    status=DETOUR_FOUND,
                    segment_ids=segments,
                    from_junction_id=ja,
                    to_junction_id=jb,
                    turnaround_segment_ids=tuple(
                        dict.fromkeys(s for s in segments if len(self._sides.get(s, ())) < 2)
                    ),
                    cost_sec=round(cost, 1),
                )
            segment, at = state
            if at == jb:
                push(_SINK, cost + self._traverse_sec.get(segment, 0.0), length, label)
                continue
            for nxt, move_cost in self._moves.get(state, ()):
                if nxt == blocked or nxt in blocked_now:
                    continue
                for end in self._ends(nxt, at):
                    push(
                        (nxt, end), cost + move_cost + penalty(nxt), length + length_of(nxt), label
                    )
        return None
