"""Diversion planning wiring + the optimiser guards live data switches on.

Coverage:
    - The lifespan's planner (``build_diversion_planner``) carries its
      heuristic: POST /api/v1/optimize/diversions returns a NON-EMPTY plan
      for a seed corridor route (it used to log ``no_heuristic`` and answer
      []), on the seed topology served through a fake pool — the graph a
      store holds after migration 012 + the seed + the network step
      (reanchor_osm_network.sql: the real OpenStreetMap network, every
      movement at the junction where its segments meet, directed road_graph).
    - The heuristic reads corridor.road_graph cost plus each movement's
      junction (the old SQL selected a non-existent
      segment_adjacency.travel_cost_sec), never routes a detour THROUGH its
      own closed segment (the old up-weighting did for 6 of 8 segments of
      the acceptance route), and every detour it reports really joins two
      junctions of the closed segment — replayed against the store's own
      movement list, one-way direction included.  Two neighbours at the
      SAME junction never pose as a detour (the Nehru Bridge 1015 used to
      get two roads that merely meet at its end); bridges say ``no_path``,
      stubs ``dead_end``; one-ways are driven their own way.
    - A planner without heuristic / without a loadable graph answers a
      labelled 503 instead of a silent [].
    - Practicality (WP-16): a detour longer than max(5 km, 6 × the closed
      segment's length) is never offered — the cheapest walk WITHIN the
      limit is (resource-constrained search, cross-checked against brute
      force), else the closure is ``impractical`` (no alt segments, the
      cheapest walk's length, a labelled reason) — on the real network the
      Nehru Bridge (1.1 km; 7.4 km round over the Gandhi Bridge) and
      Airport Road (South) (3.0 km; 32.6 km round the ring).  One-way Gandhi
      Road 1012 has a local way round: Relief Road 1005, which joins the
      same two junctions.  Each detour end says whether the route pinned it
      (the first segment's start is chosen when it has a third junction).
    - Rolling closures (WP-17, #47): a detour never runs over a route
      segment whose closure window overlaps the closed segment's (Airport
      Road S and N used to be "diverted" over each other while both were
      shut — on the real network the parallel riverside pair Dr. Ambedkar
      Road 1043 / Riverfront Road (East) 1007); a route segment open
      meanwhile is penalised, not banned, and listed in
      ``uses_route_segment_ids`` (C-R).
    - MIP solver: strictly positive edge costs (no disjoint subtours under
      free-flow forecasts) and reconstruction that rejects paths that do
      not reach the destination.  The convoy router plans on the directed
      corridor.road_graph, never up one-way Gandhi Road or Swami
      Vivekananda Road the wrong way.
    - DB tier (read-only SELECTs): VEER_TEST_PG_DSN (a seeded store) — the
      fake IS that store's graph, row for row, and the store plans the
      acceptance route exactly as the fake does; VVIP_DATABASE_URL (any
      corridor store) — the real graph plans the acceptance route.
"""

from __future__ import annotations

import functools
import itertools
import math
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from structlog.testing import capture_logs

pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from traffic_oracle.__main__ import build_diversion_planner, create_app  # noqa: E402
from traffic_oracle.data.graph_queries import CorridorGraphDB  # noqa: E402
from traffic_oracle.optimize.constructive_heuristic import (  # noqa: E402
    _SQL_SEGMENT_ATTRS,
    _SQL_TURNS,
    DETOUR_MAX_FACTOR,
    DETOUR_MAX_MIN_M,
    ROUTE_SEGMENT_PENALTY_SEC,
    DiversionHeuristic,
    closure_windows_overlap,
    detour_limit_m,
)
from traffic_oracle.optimize.corridor_router import CorridorRouter  # noqa: E402
from traffic_oracle.optimize.diversion_planner import (  # noqa: E402
    DiversionPlanner,
    DiversionUnavailableError,
    coerce_departure_time,
)
from traffic_oracle.optimize.mip_solver import ConvoyMIPSolver  # noqa: E402
from traffic_oracle.simulation.network import default_network  # noqa: E402

#: The acceptance route on the real network (OpenStreetMap, WP-20): Paldi to
#: SVPI airport — Ashram Road (Paldi) 1036, Ashram Road (South) 1001, the
#: Nehru Bridge Road 1015, Dr. Ambedkar Road 1043, Airport Road (South) 1027
#: and the Domestic Airport Road 1029 to the terminal.
ACCEPTANCE_ROUTE = [1036, 1001, 1015, 1043, 1027, 1029]

#: Its detours: segment → (detour_status, alt_segment_ids,
#: (detour_from_junction_id, detour_to_junction_id)).  Rolling closures:
#: every route segment's window overlaps its neighbours' (the 120 s advance
#: closure), so a detour may use neither — and none of these needs one.
ACCEPTANCE_DETOURS: dict[int, tuple[str, list[int], tuple[int | None, int | None]]] = {
    # Ashram Road (Paldi): CG Road south to north to Income Tax Circle, then
    # the west riverfront road back down to Ellisbridge.
    1036: ("found", [1003, 1004, 1006], (4, 33)),
    # Ashram Road (South): the west riverfront to Income Tax Circle, then
    # Ashram Road (North) back to the Nehru Bridge landing.
    1001: ("found", [1006, 1002], (33, 1)),
    # Nehru Bridge Road (1.1 km): round over the Gandhi Bridge and Kalupur is
    # 7.4 km, beyond max(5 km, 6 x 1.1 km) — impractical, nothing offered.
    1015: ("impractical", [], (1, 3)),
    # Dr. Ambedkar Road: the east riverfront road joins Lal Darwaja and
    # Shahibaug as well.
    1043: ("found", [1007], (3, 20)),
    # Airport Road (South) (3.0 km): 32.6 km round the ring and the Indira
    # Bridge against a 17.8 km limit.
    1027: ("impractical", [], (20, 14)),
    # Domestic Airport Road: the terminal spur — no through traffic.
    1029: ("dead_end", [], (None, None)),
}

#: One-way Gandhi Road (1012, J3 → J15)'s way round: Relief Road, two-way
#: between the same two junctions.
GANDHI_LOCAL = [1005]

#: …and with Relief Road shut as well: the loop over the Nehru Bridge,
#: Ashram Road (North), the Gandhi Bridge (Kasturba Gandhi Road) and Station
#: Road — 6.6 km, a real walk J3 → J15.
GANDHI_LOOP = [1015, 1002, 1031, 1014]

#: Which detour ends the acceptance route pins: every two-junction segment's
#: both ends (the displaced movement's); the terminal spur has none.
ACCEPTANCE_PINS = {sid: (True, True) for sid in ACCEPTANCE_DETOURS} | {1029: (None, None)}


# ─── Fake corridor graph (repaired seed topology, 012 road_graph) ─────────────

#: The seed a corridor store is loaded from and the network step every store
#: applies after it (veer_up.sh: sample_corridor.sql, then
#: reanchor_osm_network.sql — both generated by scripts/gen_osm_network.py
#: from OpenStreetMap).  Migration 012's view over them IS the live graph:
#: the VEER_TEST_PG_DSN tier checks this derivation against such a store.
SEED_DIR = Path(__file__).resolve().parents[2] / "corridor-store" / "seed"
SEED_SQL = SEED_DIR / "sample_corridor.sql"
NETWORK_SQL = SEED_DIR / "reanchor_osm_network.sql"
_SEED_SEGMENT_RE = re.compile(
    r"\((\d{4}),\s*'[^']+',\s*'\w+',\s*\d+,\s*\d+,\s*(TRUE|FALSE),\s*"
    r"ST_GeomFromText\('LINESTRING\(([^)]+)\)'"
)
_SEED_MOVEMENT_RE = re.compile(r"\((\d{4}),\s*(\d{4}),\s*(\d+),\s*(\d+)\)")
#: the network step's junction rows: (junction id, type, signal, POINT(lon lat), ...)
_NETWORK_JUNCTION_RE = re.compile(
    r"\((\d+),\s*'\w+',\s*(?:TRUE|FALSE),\s*ST_GeomFromText\('POINT\(([^ ]+) ([^)]+)\)'"
)
#: A junction lies ON a segment within 25 m — the rule post-seed C1/C3
#: re-point adjacency rows by and the 012 view judges one-ways with.
_ON_SEGMENT_M = 25.0

#: (from segment, to segment, via junction, turn cost s)
Movement = tuple[int, int, int, float]
_Point = tuple[float, float]


def _insert_block(sql: str, head: str) -> str:
    """The first ``INSERT INTO <head>`` statement.  ``--`` comments go
    first: a comment may carry a semicolon, and slicing at the first one
    would cut the statement short."""
    sql = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    start = sql.index(f"INSERT INTO {head}")
    return sql[start:sql.index(";", start)]


def _xy_m(point: _Point) -> _Point:
    """Equirectangular metres at Ahmedabad's latitude (city scale)."""
    k = math.radians(1.0) * 6_371_000.0
    return point[0] * k * math.cos(math.radians(23.03)), point[1] * k


def _gap_m(point: _Point, line: list[_Point]) -> float:
    """Distance (m) from ``point`` to the polyline ``line``."""
    px, py = _xy_m(point)
    gap = math.inf
    for (ax, ay), (bx, by) in itertools.pairwise(map(_xy_m, line)):
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ((dx * dx + dy * dy) or 1.0)))
        gap = min(gap, math.hypot(px - ax - t * dx, py - ay - t * dy))
    return gap


@dataclass(frozen=True)
class SeedTopology:
    #: every segment_adjacency row of a store (one-way rows too)
    movements: list[Movement]
    #: corridor.road_graph (012): (from, to) → cheapest LEGAL turn cost
    edges: dict[tuple[int, int], float]
    junction_xy: dict[int, _Point]
    start_xy: dict[int, _Point]
    oneway: frozenset[int]
    #: one-way segment → (junction at its start, junction at its end)
    oneway_ends: dict[int, tuple[int | None, int | None]]


@functools.cache
def seed_topology() -> SeedTopology:
    """The corridor graph of a store after migration 012, the seed and the
    network step — derived from those files:

    * segments from the seed; junctions 1-34 from the network step (it
      writes every junction at its OpenStreetMap node, by explicit id);
    * every movement from the network step (segment_adjacency becomes
      exactly that set) — the seed lists the same (from, to) pairs with the
      same turn costs, citing the nearest seeded junction as a placeholder
      for a junction 16+ (asserted); every movement lies at a junction ON
      both of its segments (asserted), and every pair of touching segments
      is listed both ways (asserted);
    * 012's directed road_graph: no edge enters a one-way segment away from
      its start or leaves it away from its end (judged at the movement's
      junction); each (from, to) edge keeps its cheapest legal turn.
    """
    seed = SEED_SQL.read_text(encoding="utf-8")
    network = NETWORK_SQL.read_text(encoding="utf-8")
    lines: dict[int, list[_Point]] = {}
    oneway: set[int] = set()
    for sid, is_oneway, wkt in _SEED_SEGMENT_RE.findall(
        _insert_block(seed, "corridor.road_segments")
    ):
        lines[int(sid)] = [(float(p.split()[0]), float(p.split()[1])) for p in wkt.split(",")]
        if is_oneway == "TRUE":
            oneway.add(int(sid))
    junction_xy = {
        int(jid): (float(lon), float(lat))
        for jid, lon, lat in _NETWORK_JUNCTION_RE.findall(
            _insert_block(network, "corridor.junctions AS j")
        )
    }

    def on(j: int, sid: int) -> bool:
        return _gap_m(junction_xy[j], lines[sid]) <= _ON_SEGMENT_M

    movements: list[Movement] = []
    for a, b, via, turn in _SEED_MOVEMENT_RE.findall(
        _insert_block(network, "_veer_osm_moves")
    ):
        a, b, via = int(a), int(b), int(via)
        assert on(via, a) and on(via, b), f"{a}→{b} listed at J{via}, off one of them"
        movements.append((a, b, via, float(turn)))
    seed_pairs = {
        (int(a), int(b), float(t))
        for a, b, _via, t in _SEED_MOVEMENT_RE.findall(
            _insert_block(seed, "corridor.segment_adjacency")
        )
    }
    assert seed_pairs == {(a, b, t) for a, b, _v, t in movements}, "seed ≠ network step"

    pairs = {(a, b) for a, b, *_ in movements}
    for a, b in itertools.permutations(lines, 2):
        if any(
            math.dist(_xy_m(p), _xy_m(q)) <= 1.0
            for p in (lines[a][0], lines[a][-1])
            for q in (lines[b][0], lines[b][-1])
        ):
            assert (a, b) in pairs, f"touching {a}→{b} is missing (C2 would add it)"

    def gap(point: _Point, j: int) -> float:
        return math.dist(_xy_m(point), _xy_m(junction_xy[j]))

    def at_junction(point: _Point) -> int | None:
        near = [j for j in sorted(junction_xy) if gap(point, j) <= _ON_SEGMENT_M]
        return min(near, key=lambda j: gap(point, j)) if near else None

    oneway_ends = {sid: (at_junction(lines[sid][0]), at_junction(lines[sid][-1])) for sid in oneway}
    edges: dict[tuple[int, int], float] = {}
    for a, b, via, turn in movements:
        if b in oneway and gap(lines[b][0], via) > _ON_SEGMENT_M:
            continue  # would enter one-way b away from its start
        if a in oneway and gap(lines[a][-1], via) > _ON_SEGMENT_M:
            continue  # would leave one-way a away from its end
        edges[(a, b)] = min(turn, edges.get((a, b), math.inf))
    return SeedTopology(
        movements=movements,
        edges=edges,
        junction_xy=junction_xy,
        start_xy={sid: line[0] for sid, line in lines.items()},
        oneway=frozenset(oneway),
        oneway_ends=oneway_ends,
    )


class GraphConn:
    def __init__(self, turns: list[dict], segments: list[dict], fail: bool = False) -> None:
        self.turns = turns
        self.segments = segments
        self.fail = fail
        self.queries: list[str] = []

    async def fetch(self, query: str, *args):
        self.queries.append(query)
        if self.fail:
            raise ConnectionError("db down")
        if "corridor.road_graph" in query:
            return self.turns
        if "corridor.road_segments" in query:
            return self.segments
        raise AssertionError(f"unexpected query: {query}")


class GraphPool:
    def __init__(self, conn: GraphConn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def seed_graph_pool() -> GraphPool:
    """What a repaired store answers the heuristic's two queries with: the
    corridor.road_graph ⋈ segment_adjacency rows — every movement of a
    road_graph edge with its junction, at the edge's cost (012: cheapest
    legal turn + length(source) / (speed_limit(source) / 3.6)) — plus the
    segment rows (length, speed, one-way flag, geometry start point)."""
    net = default_network()
    topo = seed_topology()
    turns = []
    for a, b, via, turn in topo.movements:
        if (a, b) not in topo.edges:
            continue
        seg = net.segments[a]
        lon, lat = topo.junction_xy[via]
        turns.append({
            "from_segment_id": a,
            "to_segment_id": b,
            "via_junction_id": via,
            "turn_cost_sec": turn,
            "travel_cost_sec": topo.edges[(a, b)] + seg.length_m / (seg.speed_limit_kmh / 3.6),
            "via_lon": lon,
            "via_lat": lat,
        })
    segments = [
        {
            "segment_id": s.segment_id,
            "length_m": s.length_m,
            "speed_limit_kmh": s.speed_limit_kmh,
            "oneway": s.segment_id in topo.oneway,
            "start_lon": topo.start_xy[s.segment_id][0],
            "start_lat": topo.start_xy[s.segment_id][1],
        }
        for s in net.segments.values()
    ]
    return GraphPool(GraphConn(turns, segments))


def two_way(a: int, b: int, via: int, cost: float | None = 60.0) -> list[tuple]:
    """Both movements between segments ``a`` and ``b`` at junction ``via``."""
    return [(a, b, via, cost), (b, a, via, cost)]


def toy_pool(
    movements: list[tuple],
    *,
    speed: float | None = 50.0,
    oneway_start: dict[int, tuple[float, float]] | None = None,
    junction_xy: dict[int, tuple[float, float]] | None = None,
) -> GraphPool:
    """Toy turn graph over 1 km segments: ``(from, to, via junction,
    road_graph cost)`` movements; ``oneway_start`` maps a one-way segment to
    its geometry start point (matched against ``junction_xy``)."""
    oneway_start = oneway_start or {}
    junction_xy = junction_xy or {}
    nodes = {n for m in movements for n in m[:2]}
    turns = [
        {
            "from_segment_id": a,
            "to_segment_id": b,
            "via_junction_id": j,
            "turn_cost_sec": 0.0,
            "travel_cost_sec": c,
            "via_lon": junction_xy.get(j, (None, None))[0],
            "via_lat": junction_xy.get(j, (None, None))[1],
        }
        for a, b, j, c in movements
    ]
    segments = [
        {
            "segment_id": n,
            "length_m": 1000.0,
            "speed_limit_kmh": speed,
            "oneway": n in oneway_start,
            "start_lon": oneway_start.get(n, (None, None))[0],
            "start_lat": oneway_start.get(n, (None, None))[1],
        }
        for n in nodes
    ]
    return GraphPool(GraphConn(turns, segments))


def replay_walk(
    movements: list[tuple],
    closed: int,
    walk: list[int],
    from_junction: int,
    to_junction: int,
    turnarounds: list[int],
    oneway_ends: dict[int, tuple[int | None, int | None]],
) -> None:
    """Assert a reported detour REALLY goes around ``closed``, independently
    of the heuristic's own graph: it leaves one junction of the closed
    segment and arrives at another (a closed one-way: its start, then its
    end), every segment is driven from the junction it is entered at to
    another of its junctions (single-junction ones are declared
    turnarounds), a one-way only from its start junction to its end one,
    and consecutive segments are joined by a listed movement at the
    junction reached.  ``movements`` is the RAW adjacency (one-way rows
    too) and ``oneway_ends`` comes from geometry, so a walk up a one-way
    the wrong way fails here even where the movement list allows it.

    A segment crossed mid-way has three junctions (Ashram Road S at the CG
    Road (East) crossing): a walk entering it may leave at either other
    one, so the replay tracks every junction the walk can stand at and
    passes when SOME consistent sequence ends at ``to_junction``; over
    two-junction segments that set is a single junction throughout."""
    sides: dict[int, set[int]] = {}
    vias: dict[tuple[int, int], set[int]] = {}
    for a, b, j, *_ in movements:
        sides.setdefault(a, set()).add(j)
        sides.setdefault(b, set()).add(j)
        vias.setdefault((a, b), set()).add(j)
    assert walk, "a found detour has segments"
    assert closed not in walk
    assert {from_junction, to_junction} <= sides[closed]
    assert from_junction != to_junction
    if closed in oneway_ends:
        assert (from_junction, to_junction) == oneway_ends[closed], (
            f"closed one-way {closed} planned against its direction"
        )
    at = {from_junction}
    for i, seg in enumerate(walk):
        if i:
            at &= vias.get((walk[i - 1], seg), set())
            assert at, f"no movement {walk[i - 1]}→{seg} at a junction the walk reached"
        entries = at & sides[seg]
        assert entries, f"segment {seg} touches none of junctions {sorted(at)}"
        if seg in oneway_ends:
            start, end = oneway_ends[seg]
            assert start in entries and end is not None, (
                f"one-way {seg} ({start} → {end}) entered at {sorted(entries)}"
            )
            at = {end}
        elif len(sides[seg]) == 1:
            assert seg in turnarounds, f"undeclared turnaround on {seg}"
            at = entries
        else:
            at = {j for e in entries for j in sides[seg] - {e}}
    assert to_junction in at, f"walk ends at junction(s) {sorted(at)}, not {to_junction}"
    assert {s for s in walk if len(sides[s]) == 1} == set(turnarounds), (
        f"declared turnarounds {turnarounds} are not the walk's single-junction segments"
    )


async def post(app, body: dict):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.post("/api/v1/optimize/diversions", json=body)


def app_with_planner(planner) -> object:
    app = create_app()
    app.state.diversion_planner = planner
    return app


# ─── Lifespan wiring → non-empty plans ────────────────────────────────────────


class TestDiversionWiring:
    def test_lifespan_planner_carries_its_heuristic(self):
        planner = build_diversion_planner(CorridorGraphDB(pool=seed_graph_pool()), None)
        assert isinstance(planner, DiversionPlanner)
        assert planner.available
        assert isinstance(planner._heuristic, DiversionHeuristic)

    async def test_endpoint_returns_a_non_empty_plan_for_a_corridor_route(self):
        pool = seed_graph_pool()
        planner = build_diversion_planner(CorridorGraphDB(pool=pool), None)
        with capture_logs() as logs:
            res = await post(
                app_with_planner(planner),
                {
                    "route_segment_ids": ACCEPTANCE_ROUTE,
                    "advance_closure_sec": 120,
                    "departure_time": "2026-10-23T09:00:00+05:30",
                },
            )
        assert res.status_code == 200, res.text
        body = res.json()
        diversions = body["diversions"]
        assert len(diversions) == len(ACCEPTANCE_ROUTE)  # every known segment planned
        assert body["unplanned_segment_ids"] == []
        assert body["segment_lengths_source"] == "corridor.road_segments"
        topo = seed_topology()
        net = default_network()
        for d in diversions:
            assert d["segment_id"] not in d["alt_segment_ids"]  # never through itself
            assert d["detour_found"] is (d["detour_status"] == "found")
            assert d["detour_found"] is bool(d["alt_segment_ids"])
            assert d["activate_at"] < d["deactivate_at"]
            assert d["diversion_type"] in {"full_closure", "partial_closure", "speed_restriction"}
            limit = detour_limit_m(net.segments[d["segment_id"]].length_m)
            if d["detour_found"]:
                replay_walk(
                    topo.movements, d["segment_id"], d["alt_segment_ids"],
                    d["detour_from_junction_id"], d["detour_to_junction_id"],
                    d["turnaround_segment_ids"], topo.oneway_ends,
                )
                assert 0 < d["detour_distance_m"] <= limit  # never an impractical offer
            elif d["detour_status"] == "impractical":
                # reported, not offered: the cheapest way round, beyond the limit
                assert d["detour_distance_m"] > max(limit, 5000.0)
                assert d["turnaround_segment_ids"] == []
            else:
                assert d["detour_distance_m"] is None
        ambedkar = next(d for d in diversions if d["segment_id"] == 1043)
        assert ambedkar["detour_distance_m"] == pytest.approx(
            net.segments[1007].length_m, abs=0.2
        )
        assert sum(d["detour_status"] == "found" for d in diversions) >= 1
        # Ashram Road (Paldi / South): round by CG Road and the riverfront;
        # Dr. Ambedkar Road by the east riverfront; the Nehru Bridge and
        # Airport Road (South) only far beyond the practicality limit.
        assert {
            d["segment_id"]: (
                d["detour_status"], d["alt_segment_ids"],
                (d["detour_from_junction_id"], d["detour_to_junction_id"]),
            )
            for d in diversions
        } == ACCEPTANCE_DETOURS
        assert all(d["turnaround_segment_ids"] == [] for d in diversions)
        assert not any(e["event"] == "diversion_planner.no_heuristic" for e in logs)
        # road_graph cost + the movement's junction, never the non-existent
        # segment_adjacency.travel_cost_sec
        assert all("travel_cost_sec FROM corridor.segment_adjacency" not in q
                   for q in pool.conn.queries)
        graph_sql = next(q for q in pool.conn.queries if "FROM corridor.road_graph" in q)
        assert "sa.via_junction_id" in graph_sql and "rg.cost" in graph_sql

    async def test_planner_carries_pins_limit_and_reason(self):
        """The plan objects the endpoint serializes carry the WP-16 fields:
        the pins, every planned closure knows the limit it was held to, and a
        closure without a way round says why."""
        planner = build_diversion_planner(CorridorGraphDB(pool=seed_graph_pool()), None)
        with capture_logs() as logs:
            result = await planner.plan(
                ACCEPTANCE_ROUTE, advance_closure_sec=120,
                departure_time=coerce_departure_time("2026-10-23T09:00:00+05:30"),
            )
        by_seg = {p.segment_id: p for p in result.plans}
        net = default_network()
        assert {sid: (p.entry_pinned, p.exit_pinned) for sid, p in by_seg.items()} == (
            ACCEPTANCE_PINS
        )
        for sid, p in by_seg.items():
            if p.detour_status == "dead_end":
                assert p.detour_reason  # the terminal spur: nothing to divert
                continue
            assert p.detour_limit_m == pytest.approx(
                max(DETOUR_MAX_MIN_M, DETOUR_MAX_FACTOR * net.segments[sid].length_m), abs=0.2
            )
            if p.detour_status == "found":
                assert p.detour_reason is None  # the cheapest walk fits: nothing to explain
            else:
                assert p.detour_reason
        ambedkar = by_seg[1043]
        assert (ambedkar.detour_status, ambedkar.detour_found, ambedkar.alt_segment_ids) == (
            "found", True, [1007]
        )
        assert ambedkar.detour_distance_m == pytest.approx(
            net.segments[1007].length_m, abs=0.2
        )                                                  # the east riverfront, 6.1 km
        assert ambedkar.detour_distance_m < ambedkar.detour_limit_m
        assert ambedkar.diversion_type == "full_closure"  # the closure itself still stands
        assert by_seg[1001].detour_status == "found" and by_seg[1001].entry_pinned is True
        # a way round far beyond the practicality limit is reported, never offered
        for sid in (1015, 1027):
            assert by_seg[sid].detour_status == "impractical"
            assert by_seg[sid].alt_segment_ids == []
            assert "practicality limit" in by_seg[sid].detour_reason
        # C-R: no acceptance detour leans on a route segment at all
        assert all(p.uses_route_segment_ids == [] for p in result.plans)
        plan_log = next(e for e in logs if e["event"] == "diversion_planner.plan")
        assert sorted(plan_log["impractical"]) == [1015, 1027]

    async def test_activation_times_follow_real_segment_lengths(self):
        planner = build_diversion_planner(CorridorGraphDB(pool=seed_graph_pool()), None)
        result = await planner.plan(
            [1002, 1009], convoy_speed_kmh=60.0, advance_closure_sec=0,
            departure_time=coerce_departure_time("2026-10-23T09:00:00+05:30"),
        )
        first, second = sorted(result.plans, key=lambda p: p.activate_at)
        length_1002 = default_network().segments[1002].length_m  # ~1.56 km, not 500 m
        expected = length_1002 / (60 / 3.6)
        assert (second.activate_at - first.activate_at).total_seconds() == pytest.approx(
            expected, rel=1e-6
        )
        assert result.speeds_source == "convoy_speed"


# ─── Detour correctness ───────────────────────────────────────────────────────


async def built(pool: GraphPool) -> DiversionHeuristic:
    heuristic = DiversionHeuristic(CorridorGraphDB(pool=pool))
    await heuristic.build_graph()
    return heuristic


class TestSeedDetours:
    """Closures on the seed graph (a seeded store's, row for row)."""

    def test_the_fake_is_the_repaired_graph(self):
        topo = seed_topology()
        assert len(topo.movements) == 198 and len(topo.edges) == 186
        assert len(topo.start_xy) == 49 and len(topo.junction_xy) == 34
        # Every movement sits at the junction where its two segments meet:
        # Ashram Road S and N at the Nehru Bridge landing (J1), Airport Road
        # N onto the Indira Bridge at its south end (J13), the bridge onto
        # the Bhat-Motera road and the highway at its north end (J30).
        vias = {(a, b): j for a, b, j, _ in topo.movements}
        assert vias[(1001, 1002)] == vias[(1002, 1001)] == 1
        assert vias[(1028, 1048)] == 13
        assert vias[(1048, 1010)] == vias[(1048, 1024)] == 30
        # 012: Swami Vivekananda Road (one-way J5 → J6) is neither left at J5
        # nor entered at J6, Gandhi Road (one-way J3 → J15) neither left at
        # J3 nor entered at J15 — those rows are adjacency, not road_graph.
        assert topo.oneway_ends == {1012: (3, 15), 1013: (5, 6)}
        not_edges = {(1013, 1049), (1013, 1038), (1039, 1013), (1012, 1015), (1014, 1012)}
        assert not_edges.isdisjoint(topo.edges)
        assert {(1013, 1049, 5), (1039, 1013, 6), (1014, 1012, 15)} <= {
            m[:3] for m in topo.movements}
        # Relief Road 1005 meets Gandhi Road at BOTH ends: each pair is an
        # edge only at the junction one-way Gandhi Road allows — entered at
        # J3 (3 s, straight on), left at J15 (10 s).
        assert {j for a, b, j, _ in topo.movements if (a, b) == (1005, 1012)} == {3, 15}
        assert topo.edges[(1005, 1012)] == 3.0 and topo.edges[(1012, 1005)] == 10.0

    async def test_every_reported_seed_detour_really_goes_around(self):
        heuristic = await built(seed_graph_pool())
        topo = seed_topology()
        segments = sorted({m[0] for m in topo.movements} | {m[1] for m in topo.movements})
        status: dict[str, set[int]] = {}
        for sid in segments:
            detour = heuristic._find_detour(sid, {sid})
            if detour.found:
                replay_walk(
                    topo.movements, sid, list(detour.segment_ids), detour.from_junction_id,
                    detour.to_junction_id, list(detour.turnaround_segment_ids),
                    topo.oneway_ends,
                )
            else:
                assert detour.segment_ids == ()
            status.setdefault(detour.status, set()).add(sid)
        # The real network is a mesh of loops (the Sardar Patel Ring Road
        # closes the city; the two banks are joined by five crossings): every
        # two-junction segment has a way round, unbounded…
        stubs = {1020, 1026, 1029, 1038}
        assert status["found"] == set(segments) - stubs - {1025}
        # …except the stubs (one junction: Prahlad Nagar Road's dead end, the
        # Gandhinagar Highway north to Gandhinagar, the SVPI terminal spur,
        # Kankaria Road) — no through movement to divert —
        assert status["dead_end"] == stubs
        # …and the Gandhinagar Highway between Bhat and Koba Circles: Koba's
        # only other road is that north stub, so nothing joins its two ends.
        assert status["no_path"] == {1025}

    async def test_bridge_closure_never_pairs_neighbours_at_one_junction(self):
        topo = seed_topology()
        # The old pairwise search answered two roads that merely meet at the
        # closed bridge's end: Ashram Road S and N both sit at the Nehru
        # Bridge's J1 and are adjacent there.
        assert (1001, 1002, 1) in {(a, b, j) for a, b, j, _ in topo.movements}
        heuristic = await built(seed_graph_pool())
        # The way round is a real walk from J1 to J3 — Ashram Road N to
        # Income Tax Circle, the Gandhi Bridge, Station Road, Relief Road —
        # never the two neighbours at J1.
        detour = heuristic._find_detour(1015, set(ACCEPTANCE_ROUTE), 1001, 1043)
        assert detour.found and (detour.from_junction_id, detour.to_junction_id) == (1, 3)
        assert tuple(detour.segment_ids) == (1002, 1031, 1014, 1005)
        replay_walk(
            topo.movements, 1015, list(detour.segment_ids), 1, 3, [], topo.oneway_ends
        )
        # …and the other way round, along the same roads, never down one-way
        # Gandhi Road against its direction.
        back = heuristic._find_detour(1015, set(ACCEPTANCE_ROUTE), 1043, 1001)
        assert back.found and (back.from_junction_id, back.to_junction_id) == (3, 1)
        assert tuple(back.segment_ids) == (1005, 1014, 1031, 1002)
        assert 1012 not in back.segment_ids
        replay_walk(
            topo.movements, 1015, list(back.segment_ids), 3, 1, [], topo.oneway_ends
        )
        # The convoy's own neighbouring closures are not on that walk: it
        # stands while they are shut.
        shut = heuristic._find_detour(
            1015, set(ACCEPTANCE_ROUTE), 1001, 1043, blocked_now=frozenset({1001, 1043}),
        )
        assert shut.found and shut.segment_ids == detour.segment_ids

    async def test_acceptance_route_detours_are_specific(self):
        heuristic = await built(seed_graph_pool())
        routes = await heuristic.compute_diversions(ACCEPTANCE_ROUTE, predicted_speeds={})
        by_seg = {r.blocked_segment_id: r for r in routes}
        assert {
            sid: (r.detour_status, r.alt_segment_ids,
                  (r.detour_from_junction_id, r.detour_to_junction_id))
            for sid, r in by_seg.items()
        } == ACCEPTANCE_DETOURS
        topo = seed_topology()
        net = default_network()
        for sid, r in by_seg.items():
            assert r.turnaround_segment_ids == []
            assert r.detour_found is (r.detour_status == "found")
            assert (r.entry_pinned, r.exit_pinned) == ACCEPTANCE_PINS[sid]
            if r.detour_found:
                replay_walk(
                    topo.movements, sid, r.alt_segment_ids, r.detour_from_junction_id,
                    r.detour_to_junction_id, r.turnaround_segment_ids, topo.oneway_ends,
                )
                assert r.detour_distance_m == pytest.approx(
                    sum(net.segments[s].length_m for s in r.alt_segment_ids), abs=0.2
                )
                assert r.detour_distance_m <= r.detour_limit_m
            elif r.detour_status == "impractical":
                assert r.alt_segment_ids == []
                assert r.detour_distance_m > r.detour_limit_m
            else:
                assert r.detour_status in {"no_path", "dead_end"}
                assert r.alt_segment_ids == [] and r.detour_distance_m is None
        # every acceptance segment meets exactly two junctions (none is
        # crossed mid-way on the real network) — the terminal spur one
        assert heuristic._sides[1001] == (1, 33)
        assert heuristic._sides[1029] == (14,)

    async def test_gandhi_road_goes_round_locally_and_an_impractical_walk_is_never_offered(self):
        heuristic = await built(seed_graph_pool())
        topo = seed_topology()
        net = default_network()
        limit = detour_limit_m(net.segments[1012].length_m)
        assert limit == pytest.approx(6 * net.segments[1012].length_m)
        # Relief Road joins one-way Gandhi Road's two ends: the way round is
        # local, driven J3 → J15 the one-way's own way.
        local = heuristic._find_detour(1012, {1012}, max_length_m=limit)
        assert local.found and list(local.segment_ids) == GANDHI_LOCAL
        assert local.reason is None and local.length_m < 2_500
        replay_walk(topo.movements, 1012, GANDHI_LOCAL, 3, 15, [], topo.oneway_ends)
        # With Relief Road shut as well, the search finds the loop over the
        # Nehru Bridge and the Gandhi Bridge — a real walk, inside the limit.
        loop = heuristic._find_detour(
            1012, {1012}, max_length_m=limit, blocked_now=frozenset({1005})
        )
        assert loop.found and list(loop.segment_ids) == GANDHI_LOOP
        assert loop.length_m == pytest.approx(
            sum(net.segments[s].length_m for s in GANDHI_LOOP)) and loop.length_m < limit
        replay_walk(topo.movements, 1012, GANDHI_LOOP, 3, 15, [], topo.oneway_ends)
        # The Nehru Bridge (1.1 km): its cheapest way round (7.4 km) exceeds
        # the practicality limit — impractical, nothing offered, the walk's
        # length reported, its ends kept.
        bridge_limit = detour_limit_m(net.segments[1015].length_m)
        round_m = sum(net.segments[s].length_m for s in (1002, 1031, 1014, 1005))
        held = heuristic._find_detour(
            1015, set(ACCEPTANCE_ROUTE), 1001, 1043, max_length_m=bridge_limit
        )
        assert held.status == "impractical" and not held.found
        assert held.segment_ids == () and held.turnaround_segment_ids == ()
        assert (held.from_junction_id, held.to_junction_id) == (1, 3)
        assert held.length_m == pytest.approx(round_m) and held.length_m > bridge_limit
        assert held.limit_m == bridge_limit
        assert (held.entry_pinned, held.exit_pinned) == (True, True)
        assert "hold or meter traffic" in held.reason
        assert f"{round_m / 1000:.1f} km" in held.reason
        assert f"{bridge_limit / 1000:.1f} km" in held.reason
        # A limit the walk fits is no limit at all: found, unlabelled.
        roomy = heuristic._find_detour(
            1015, set(ACCEPTANCE_ROUTE), 1001, 1043, max_length_m=round_m + 1.0
        )
        assert roomy.found and list(roomy.segment_ids) == [1002, 1031, 1014, 1005]
        assert roomy.reason is None and roomy.length_m == pytest.approx(round_m)

    async def test_detour_ends_say_whether_the_route_pinned_them(self):
        heuristic = await built(seed_graph_pool())
        # First route segment with two junctions: the exit (J33, towards
        # Ashram Road S) fixes the entry too — both pinned.
        first = heuristic._find_detour(1036, {1036, 1001}, None, 1001)
        assert (first.from_junction_id, first.to_junction_id) == (4, 33)
        assert (first.entry_pinned, first.exit_pinned) == (True, True)
        # Mid-route, both ends come from the displaced through movement.
        mid = heuristic._find_detour(1001, {1036, 1001, 1015}, 1036, 1015)
        assert (mid.from_junction_id, mid.to_junction_id) == (33, 1)
        assert (mid.entry_pinned, mid.exit_pinned) == (True, True)
        # A one-segment route pins neither end (both directions searched).
        alone = heuristic._find_detour(1001, {1001})
        assert (alone.entry_pinned, alone.exit_pinned) == (False, False)
        # A stub has no detour to pin.
        stub = heuristic._find_detour(1029, {1029})
        assert stub.status == "dead_end"
        assert (stub.entry_pinned, stub.exit_pinned) == (None, None) and stub.reason

    async def test_a_walk_can_leave_a_road_at_its_mid_way_crossing(self):
        # A road met by a third road half-way along has three junctions (the
        # schematic seed's Ashram Road S at the CG Road (East) crossing; none
        # on the real network, every segment ends at its junctions): closed
        # 12 joins 4 and 33; 13 runs 4 → 2; road 11 meets 2, 33 (mid-way)
        # and 1.  The way round leaves 11 at the crossing.
        movements = [
            *two_way(13, 12, 4), *two_way(11, 12, 33), *two_way(13, 11, 2),
            *two_way(11, 14, 1),
        ]
        heuristic = await built(toy_pool(movements))
        assert heuristic._sides[11] == (1, 2, 33)
        detour = heuristic._find_detour(12, {12})
        assert detour.segment_ids == (13, 11)
        assert (detour.from_junction_id, detour.to_junction_id) == (4, 33)
        replay_walk(movements, 12, [13, 11], 4, 33, [], {})
        # starting a route on such a road: only its exit is pinned, the start
        # is CHOSEN among its other junctions (partial)
        first = heuristic._find_detour(11, {11, 12}, None, 12)
        assert first.exit_pinned is True and first.entry_pinned is False

    async def test_one_way_gandhi_road_is_planned_its_own_way(self):
        heuristic = await built(seed_graph_pool())
        # Gandhi Road (1012) is one-way from junction 3 to junction 15 and
        # Swami Vivekananda Road (1013) from 5 to 6: a closure is planned for
        # that direction even against the route order.
        assert heuristic._oneway_entry == {1012: 3, 1013: 5}
        detour = heuristic._find_detour(1012, {1012}, 1014, 1015)
        assert (detour.from_junction_id, detour.to_junction_id) == (3, 15)
        assert detour.found and list(detour.segment_ids) == GANDHI_LOCAL
        # No movement the heuristic plans enters it at J15 or leaves it at J3
        # — Relief Road 1005, meeting it at both ends, included.
        assert 1012 not in dict(heuristic._moves.get((1014, 15), ()))
        assert 1012 not in dict(heuristic._moves.get((1005, 15), ()))
        assert (1012, 3) not in heuristic._moves
        assert 1012 in dict(heuristic._moves[(1005, 3)])
        assert 1005 in dict(heuristic._moves[(1012, 15)])

    def test_replay_rejects_a_one_way_driven_backwards(self):
        topo = seed_topology()
        # Kasturba Gandhi Road (J2 - J8) "diverted" from Kalupur back to
        # Income Tax Circle down one-way Gandhi Road the wrong way: every
        # movement is in the raw adjacency (Station Road → Gandhi Road at
        # J15 among them) — only Gandhi Road's direction rules the walk out.
        wrong_way = [1014, 1012, 1015, 1002]
        replay_walk(topo.movements, 1031, wrong_way, 8, 2, [], {})
        with pytest.raises(AssertionError, match="one-way 1012"):
            replay_walk(topo.movements, 1031, wrong_way, 8, 2, [], topo.oneway_ends)


class TestDetourSearch:
    async def test_through_movement_detour_crosses_between_the_ends(self):
        # Route 1 → 2 → 3; closed 2 joins junctions 10 and 20.  Stubs 5 and
        # 6 meet 2 at junction 10 and are adjacent to each other there (the
        # cheapest "pair" the old search would have returned); the real way
        # round is side road 8 (10 → 30) then 9 (30 → 20).
        movements = [
            *two_way(1, 2, 10), *two_way(2, 3, 20),
            *two_way(5, 6, 10, 5.0), *two_way(5, 2, 10), *two_way(6, 2, 10),
            *two_way(1, 5, 10), *two_way(1, 6, 10),
            *two_way(1, 8, 10, 90.0), *two_way(8, 9, 30, 90.0), *two_way(9, 3, 20, 90.0),
            *two_way(8, 2, 10), *two_way(9, 2, 20),
        ]
        heuristic = DiversionHeuristic(CorridorGraphDB(pool=toy_pool(movements)))
        routes = await heuristic.compute_diversions([1, 2, 3], predicted_speeds={})
        closed = next(r for r in routes if r.blocked_segment_id == 2)
        assert closed.alt_segment_ids == [8, 9]
        assert (closed.detour_from_junction_id, closed.detour_to_junction_id) == (10, 20)
        assert closed.detour_found and closed.detour_status == "found"
        assert closed.detour_distance_m == pytest.approx(2000.0)  # both 1 km segments
        replay_walk(movements, 2, closed.alt_segment_ids, 10, 20, [], {})

    async def test_neighbours_at_the_same_junction_are_not_a_detour(self):
        # 5 and 6 run in parallel from junction 10 to 50 and meet 2 at
        # junction 10; nothing but 2 reaches junction 20 → no path.
        movements = [
            *two_way(1, 2, 10), *two_way(2, 3, 20),
            *two_way(5, 6, 10), *two_way(5, 6, 50),
            *two_way(1, 5, 10), *two_way(1, 6, 10), *two_way(2, 5, 10), *two_way(2, 6, 10),
        ]
        heuristic = DiversionHeuristic(CorridorGraphDB(pool=toy_pool(movements)))
        routes = await heuristic.compute_diversions([1, 2, 3], predicted_speeds={})
        closed = next(r for r in routes if r.blocked_segment_id == 2)
        assert closed.alt_segment_ids == [] and closed.detour_found is False
        assert closed.detour_status == "no_path"
        assert (closed.detour_from_junction_id, closed.detour_to_junction_id) == (10, 20)
        assert closed.detour_distance_m is None

    async def test_stub_closure_is_a_dead_end(self):
        heuristic = await built(toy_pool([*two_way(1, 2, 10), *two_way(1, 7, 10)]))
        detour = heuristic._find_detour(7, {7})
        assert detour.status == "dead_end" and detour.segment_ids == ()

    async def test_turnaround_on_a_stub_is_reported(self):
        # 8 (10 → 30) and 9 (30 → 20) are not joined at junction 30 except
        # through the stub 7: the walk turns around on it, and says so.
        movements = [
            *two_way(1, 2, 10), *two_way(2, 3, 20), *two_way(1, 8, 10),
            *two_way(8, 7, 30), *two_way(7, 9, 30), *two_way(9, 3, 20),
        ]
        heuristic = await built(toy_pool(movements))
        detour = heuristic._find_detour(2, {1, 2, 3}, 1, 3)
        assert detour.segment_ids == (8, 7, 9)
        assert detour.turnaround_segment_ids == (7,)
        replay_walk(movements, 2, [8, 7, 9], 10, 20, [7], {})

    async def test_one_way_segments_are_never_driven_against_their_direction(self):
        xy = {10: (72.50, 23.0), 30: (72.51, 23.0), 20: (72.52, 23.0)}
        movements = [
            *two_way(1, 2, 10), *two_way(2, 3, 20), *two_way(1, 8, 10),
            *two_way(8, 9, 30), *two_way(9, 3, 20),
        ]
        # 8 digitised from junction 30 to 10: driving it 10 → 30 is wrong-way.
        against = await built(toy_pool(movements, oneway_start={8: xy[30]}, junction_xy=xy))
        assert against._find_detour(2, {1, 2, 3}, 1, 3).status == "no_path"
        # Digitised 10 → 30: the same walk is legal.
        along = await built(toy_pool(movements, oneway_start={8: xy[10]}, junction_xy=xy))
        assert along._find_detour(2, {1, 2, 3}, 1, 3).segment_ids == (8, 9)
        # A one-way stub can be neither entered backwards nor turned on.
        stub = [
            *two_way(1, 2, 10), *two_way(2, 3, 20), *two_way(1, 8, 10),
            *two_way(8, 7, 30), *two_way(7, 9, 30), *two_way(9, 3, 20),
        ]
        one_way_stub = await built(toy_pool(stub, oneway_start={7: xy[30]}, junction_xy=xy))
        assert one_way_stub._find_detour(2, {1, 2, 3}, 1, 3).status == "no_path"

    async def test_a_closed_one_way_is_planned_in_its_own_direction(self):
        xy = {10: (72.50, 23.0), 30: (72.51, 23.0), 20: (72.52, 23.0)}
        movements = [
            *two_way(1, 2, 10), *two_way(2, 3, 20), *two_way(1, 8, 10),
            *two_way(8, 9, 30), *two_way(9, 3, 20),
        ]
        # Closed 2 is one-way 20 → 10; the convoy drives it 10 → 20
        # (escorted contraflow), but displaced public traffic goes 20 → 10.
        heuristic = await built(toy_pool(movements, oneway_start={2: xy[20]}, junction_xy=xy))
        detour = heuristic._find_detour(2, {1, 2, 3}, 1, 3)
        assert (detour.from_junction_id, detour.to_junction_id) == (20, 10)
        assert detour.segment_ids == (9, 8)

    async def test_other_route_segments_are_penalised_not_banned(self):
        # Closing 2 (10 → 20): parallel 4 joins the same junctions directly;
        # 5 → 6 → 7 goes the long way round over junctions 50 and 60.
        movements = [
            *two_way(1, 2, 10, 10.0), *two_way(2, 3, 20, 10.0),
            *two_way(1, 4, 10, 10.0), *two_way(4, 3, 20, 10.0),
            *two_way(1, 5, 10, 10.0), *two_way(5, 6, 50, 10.0),
            *two_way(6, 7, 60, 10.0), *two_way(7, 3, 20, 10.0),
        ]
        heuristic = await built(toy_pool(movements))
        # 4 is on the convoy route → the longer non-route side road wins…
        assert heuristic._find_detour(2, {1, 2, 3, 4}, 1, 3).segment_ids == (5, 6, 7)
        # …unless the side road is penalised too.
        assert heuristic._find_detour(2, {1, 2, 3, 4, 5}, 1, 3).segment_ids == (4,)
        assert ROUTE_SEGMENT_PENALTY_SEC > 0

    async def test_null_road_graph_cost_falls_back_to_length_over_speed(self):
        heuristic = await built(toy_pool([(1, 2, 10, None), (2, 3, 20, 30.0)], speed=None))
        cost_12 = dict(heuristic._moves[(1, 10)])[2]
        assert cost_12 == pytest.approx(1000.0 / (50.0 / 3.6))  # 50 km/h fallback


# ─── Practicality (WP-16) ─────────────────────────────────────────────────────


def _chain(segments: list[int], first_junction: int = 50, cost: float = 60.0) -> list[tuple]:
    """Two-way movements chaining ``segments`` end to end through fresh
    junctions ``first_junction``, +1, …"""
    out: list[tuple] = []
    for i, (a, b) in enumerate(itertools.pairwise(segments)):
        out += two_way(a, b, first_junction + i, cost)
    return out


def brute_force_cheapest(heuristic: DiversionHeuristic, blocked: int, ja: int, jb: int,
                         route_set: set[int], max_m: float, length_of) -> float:
    """Every walk from ``ja`` to ``jb`` within ``max_m``, enumerated by DFS
    over the heuristic's own moves (the graph semantics are replayed
    separately); the cheapest one's cost, or inf."""
    best = math.inf

    def penalty(segment: int) -> float:
        return ROUTE_SEGMENT_PENALTY_SEC if segment in route_set else 0.0

    def dfs(state, cost: float, length: float) -> None:
        nonlocal best
        segment, at = state
        if at == jb:
            best = min(best, cost + heuristic._traverse_sec.get(segment, 0.0))
            return
        for nxt, move_cost in heuristic._moves.get(state, ()):
            if nxt == blocked:
                continue
            for end in heuristic._ends(nxt, at):
                if length + length_of(nxt) <= max_m:
                    dfs((nxt, end), cost + move_cost + penalty(nxt), length + length_of(nxt))

    for first in heuristic._at_junction.get(ja, ()):
        if first == blocked:
            continue
        for end in heuristic._ends(first, ja):
            if length_of(first) <= max_m:
                dfs((first, end), penalty(first), length_of(first))
    return best


class TestPracticality:
    def test_limit_is_the_larger_of_5_km_and_six_closures(self):
        assert (DETOUR_MAX_MIN_M, DETOUR_MAX_FACTOR) == (5000.0, 6.0)
        assert detour_limit_m(400.0) == 5000.0           # short closures: 5 km
        assert detour_limit_m(1540.8) == pytest.approx(9244.8)
        assert detour_limit_m(1363.6) == pytest.approx(8181.6)

    async def test_the_cheapest_walk_within_the_limit_is_offered(self):
        # Closed 2 joins 10 → 20.  Route segment 4 joins them directly
        # (1 km, penalised); the side roads 5…12 go the long way round
        # (7 km, cheaper because none of them is closed by the convoy).
        movements = [
            *two_way(1, 2, 10), *two_way(2, 3, 20),
            *two_way(1, 4, 10), *two_way(4, 3, 20),
            *two_way(1, 5, 10), *_chain([5, 6, 7, 8, 9, 11, 12]),
            *two_way(12, 3, 20),
        ]
        heuristic = await built(toy_pool(movements))
        route = {1, 2, 3, 4}
        cheapest = heuristic._find_detour(2, route, 1, 3)
        assert cheapest.segment_ids == (5, 6, 7, 8, 9, 11, 12)  # 7 km
        held = heuristic._find_detour(2, route, 1, 3, max_length_m=detour_limit_m(1000.0))
        assert held.found and held.segment_ids == (4,)
        assert held.length_m == 1000.0 and held.limit_m == 6000.0
        assert held.cost_sec > cheapest.cost_sec  # costlier, but practical
        assert "7.0 km" in held.reason and "within the limit" in held.reason
        replay_walk(movements, 2, list(held.segment_ids), 10, 20, [], {})

    async def test_nothing_is_offered_when_every_way_round_is_too_long(self):
        movements = [
            *two_way(1, 2, 10), *two_way(2, 3, 20),
            *two_way(1, 5, 10), *_chain([5, 6, 7, 8, 9, 11, 12]),
            *two_way(12, 3, 20),
        ]
        heuristic = DiversionHeuristic(CorridorGraphDB(pool=toy_pool(movements)))
        routes = await heuristic.compute_diversions([1, 2, 3], predicted_speeds={})
        closed = next(r for r in routes if r.blocked_segment_id == 2)
        assert closed.detour_status == "impractical" and closed.detour_found is False
        assert closed.alt_segment_ids == [] and closed.turnaround_segment_ids == []
        assert closed.detour_distance_m == 7000.0 and closed.detour_limit_m == 6000.0
        assert (closed.detour_from_junction_id, closed.detour_to_junction_id) == (10, 20)
        assert closed.detour_reason.startswith("impractical:")
        # One segment shorter and the same chain is a plain, unlabelled offer.
        short = [m for m in movements if 12 not in m[:2]] + two_way(11, 3, 20)
        heuristic = DiversionHeuristic(CorridorGraphDB(pool=toy_pool(short)))
        routes = await heuristic.compute_diversions([1, 2, 3], predicted_speeds={})
        closed = next(r for r in routes if r.blocked_segment_id == 2)
        assert closed.detour_status == "found" and closed.alt_segment_ids == [5, 6, 7, 8, 9, 11]
        assert closed.detour_distance_m == 6000.0 and closed.detour_reason is None

    async def test_practical_walk_matches_brute_force_on_random_graphs(self):
        import random

        checked = 0
        for seed in range(40):
            rng = random.Random(seed)
            junctions = list(range(10, 10 + rng.randint(3, 6)))
            segments = list(range(1, rng.randint(5, 9) + 1))
            ends = {s: rng.sample(junctions, 2) for s in segments}
            movements = [
                (a, b, j, float(rng.randint(10, 120)))
                for a in segments for b in segments if a != b
                for j in sorted(set(ends[a]) & set(ends[b])) if rng.random() < 0.85
            ]
            if not movements:
                continue
            heuristic = await built(toy_pool(movements))
            route = set(rng.sample(segments, 2))
            for blocked, sides in sorted(heuristic._sides.items()):
                for ja, jb in itertools.permutations(sides, 2):
                    for max_m in (1000.0, 2000.0, 3000.0, 4000.0):
                        expect = brute_force_cheapest(
                            heuristic, blocked, ja, jb, route, max_m, heuristic._length
                        )
                        got = heuristic._practical_walk(
                            blocked, ja, jb, route, max_m, heuristic._length
                        )
                        checked += 1
                        if math.isinf(expect):
                            assert got is None, (seed, blocked, ja, jb, max_m)
                            continue
                        assert got is not None, (seed, blocked, ja, jb, max_m)
                        assert got.cost_sec == pytest.approx(expect, abs=0.06)
                        assert len(got.segment_ids) * 1000.0 <= max_m
                        replay_walk(movements, blocked, list(got.segment_ids), ja, jb,
                                    list(got.turnaround_segment_ids), {})
        assert checked > 500

    async def test_unbounded_practical_walk_is_the_cheapest_walk_on_the_seed(self):
        heuristic = await built(seed_graph_pool())
        topo = seed_topology()
        compared = 0
        for sid, sides in sorted(heuristic._sides.items()):
            if len(sides) < 2:
                continue
            # the orientations the planner searches (one-ways their own way)
            for ja, jb in heuristic._orientations(sid, sides, None, None):
                cheapest = heuristic._junction_walk(sid, ja, jb, {sid})
                practical = heuristic._practical_walk(
                    sid, ja, jb, {sid}, math.inf, heuristic._length
                )
                assert (cheapest is None) is (practical is None), (sid, ja, jb)
                if cheapest is None:
                    continue
                compared += 1
                assert practical.cost_sec == cheapest.cost_sec, (sid, ja, jb)
                replay_walk(topo.movements, sid, list(practical.segment_ids), ja, jb,
                            list(practical.turnaround_segment_ids), topo.oneway_ends)
        assert compared >= 30


# ─── Rolling closures (WP-17, #47) ────────────────────────────────────────────


class TestRollingClosures:
    """A detour never runs over a route segment closed at the same time."""

    def test_windows_overlap_only_when_they_share_time(self):
        t0 = coerce_departure_time("2026-10-23T09:00:00+05:30")
        sec = lambda s: t0 + timedelta(seconds=s)  # noqa: E731
        assert closure_windows_overlap((sec(0), sec(100)), (sec(50), sec(150)))
        assert closure_windows_overlap((sec(50), sec(150)), (sec(0), sec(100)))
        assert closure_windows_overlap((sec(0), sec(300)), (sec(100), sec(200)))
        # back to back: one reopens as the other closes — no shared time
        assert not closure_windows_overlap((sec(0), sec(100)), (sec(100), sec(200)))
        assert not closure_windows_overlap((sec(0), sec(100)), (sec(400), sec(500)))

    async def test_no_found_detour_uses_a_route_segment_closed_meanwhile(self):
        heuristic = await built(seed_graph_pool())
        routes = await heuristic.compute_diversions(
            ACCEPTANCE_ROUTE, predicted_speeds={}, advance_closure_sec=120,
        )
        windows = {r.blocked_segment_id: (r.activate_at, r.deactivate_at) for r in routes}
        found = [r for r in routes if r.detour_found]
        assert {r.blocked_segment_id for r in found} == {1001, 1036, 1043}
        for r in found:
            closed = windows[r.blocked_segment_id]
            for sid in r.alt_segment_ids:
                if sid in windows:
                    assert not closure_windows_overlap(windows[sid], closed), (
                        r.blocked_segment_id, sid,
                    )
            assert r.uses_route_segment_ids == [
                s for s in dict.fromkeys(r.alt_segment_ids) if s in windows
            ]
        # every pair of consecutive route segments overlaps (advance closure)
        for a, b in itertools.pairwise(ACCEPTANCE_ROUTE):
            assert closure_windows_overlap(windows[a], windows[b])

    async def test_a_33_km_way_round_a_3_km_closure_is_never_offered(self):
        heuristic = await built(seed_graph_pool())
        routes = await heuristic.compute_diversions(ACCEPTANCE_ROUTE, predicted_speeds={})
        airport = next(r for r in routes if r.blocked_segment_id == 1027)
        net = default_network()
        assert net.segments[1027].length_m == pytest.approx(2972, abs=2)
        assert airport.detour_status == "impractical" and airport.alt_segment_ids == []
        assert airport.detour_distance_m > 30_000 > airport.detour_limit_m
        # whatever IS offered fits its practicality limit
        assert all(r.detour_distance_m <= r.detour_limit_m for r in routes if r.detour_found)

    async def test_the_riverside_pair_was_diverted_over_itself(self, monkeypatch):
        import traffic_oracle.optimize.constructive_heuristic as ch

        # Dr. Ambedkar Road 1043 and Riverfront Road (East) 1007 both join
        # Lal Darwaja (J3) to Shahibaug (J20): a route over one and back over
        # the other has them shut together.
        pair = [1043, 1007]
        heuristic = await built(seed_graph_pool())
        fixed = {r.blocked_segment_id: r for r in await heuristic.compute_diversions(
            pair, predicted_speeds={})}
        # the defect, reproduced by ignoring the windows: each "detoured"
        # over the other while both were shut
        monkeypatch.setattr(ch, "closure_windows_overlap", lambda a, b: False)
        old = {r.blocked_segment_id: r for r in await heuristic.compute_diversions(
            pair, predicted_speeds={})}
        assert old[1043].alt_segment_ids == [1007] and old[1007].alt_segment_ids == [1043]
        assert old[1043].uses_route_segment_ids == [1007]
        for sid, other in ((1043, 1007), (1007, 1043)):
            assert fixed[sid].detour_status == "found"
            assert other not in fixed[sid].alt_segment_ids
            assert fixed[sid].uses_route_segment_ids == []

    async def test_a_route_segment_open_meanwhile_is_penalised_and_reported(self):
        # Closed 2 (junction 10 → 20); route segment 4 also joins 10 and 20.
        movements = [
            *two_way(1, 2, 10, 10.0), *two_way(1, 4, 10, 10.0),
            *two_way(2, 3, 20, 10.0), *two_way(4, 3, 20, 10.0),
            *two_way(3, 6, 30, 10.0), *two_way(6, 7, 40, 10.0),
            *two_way(7, 8, 50, 10.0),
        ]
        heuristic = await built(toy_pool(movements))
        speeds = {s: 40.0 for s in (1, 2, 3, 4, 6, 7, 8)}   # 90 s per 1 km segment
        # 4 driven long after 2: its window starts once 2 has reopened
        late = await heuristic.compute_diversions(
            [1, 2, 3, 6, 7, 8, 4], predicted_speeds=speeds, advance_closure_sec=0,
        )
        closed = next(r for r in late if r.blocked_segment_id == 2)
        later = next(r for r in late if r.blocked_segment_id == 4)
        assert not closure_windows_overlap(
            (closed.activate_at, closed.deactivate_at), (later.activate_at, later.deactivate_at)
        )
        assert closed.detour_status == "found" and closed.alt_segment_ids == [4]
        assert closed.uses_route_segment_ids == [4]
        # …while right after 2 they are shut together: no way round
        near = await heuristic.compute_diversions(
            [1, 2, 4, 3], predicted_speeds=speeds, advance_closure_sec=120,
        )
        closed = next(r for r in near if r.blocked_segment_id == 2)
        assert closed.detour_status == "no_path" and closed.uses_route_segment_ids == []
        planner = DiversionPlanner(heuristic=heuristic)
        plans = await planner.plan_diversions([1, 2, 3, 6, 7, 8, 4], advance_closure_sec=0)
        assert next(p for p in plans if p.segment_id == 2).uses_route_segment_ids == [4]


# ─── Labelled unavailability ─────────────────────────────────────────────────


class TestLabelledUnavailability:
    async def test_no_heuristic_is_a_labelled_503(self):
        with pytest.raises(DiversionUnavailableError) as err:
            await DiversionPlanner().plan([1001])
        assert err.value.reason == "no_heuristic"
        res = await post(app_with_planner(DiversionPlanner()), {"route_segment_ids": [1001]})
        assert res.status_code == 503
        assert res.json()["detail"]["reason"] == "no_heuristic"

    async def test_graph_load_failure_is_a_labelled_503(self):
        pool = seed_graph_pool()
        pool.conn.fail = True
        planner = build_diversion_planner(CorridorGraphDB(pool=pool), None)
        res = await post(app_with_planner(planner), {"route_segment_ids": [1001]})
        assert res.status_code == 503
        assert res.json()["detail"]["reason"] == "graph_unavailable"

    async def test_bad_departure_time_is_422(self):
        planner = build_diversion_planner(CorridorGraphDB(pool=seed_graph_pool()), None)
        res = await post(
            app_with_planner(planner),
            {"route_segment_ids": [1001], "departure_time": "tomorrow-ish"},
        )
        assert res.status_code == 422

    async def test_unknown_segments_are_listed_not_hidden(self):
        planner = build_diversion_planner(CorridorGraphDB(pool=seed_graph_pool()), None)
        res = await post(app_with_planner(planner), {"route_segment_ids": [1001, 999999]})
        assert res.status_code == 200
        assert res.json()["unplanned_segment_ids"] == [999999]

    def test_naive_departure_is_ahmedabad_time(self):
        dt = coerce_departure_time("2026-10-23T09:00:00")
        assert dt.utcoffset().total_seconds() == 5.5 * 3600
        assert coerce_departure_time("2026-10-23T03:30:00Z").isoformat() == (
            "2026-10-23T03:30:00+00:00"
        )
        assert coerce_departure_time(None) is None


# ─── MIP solver guards ────────────────────────────────────────────────────────


WEIGHTS = {"time": 0.3, "disruption": 0.4, "security": 0.2, "complexity": 0.1}


def motorway(*ids: int, length: float = 100.0) -> dict[int, dict]:
    return {
        s: {"length_m": length, "road_class": "motorway", "lanes": 3} for s in ids
    }


class TestMipGuards:
    def test_free_flow_secure_cycles_no_longer_pay_for_themselves(self):
        # Path 1 → 2 → 3 plus a short motorway 2-cycle 4 ↔ 5 disjoint from
        # it: under the old −security reward every edge here was negative
        # cost and the cycle was activated (a disjoint subtour).
        attrs = motorway(1, 2, 3, 4, 5)
        edges = [(1, 2), (2, 3), (4, 5), (5, 4)]
        speeds = {s: 100.0 for s in attrs}  # free flow
        cong = {s: 0.0 for s in attrs}
        with capture_logs() as logs:
            result = ConvoyMIPSolver(WEIGHTS).solve(edges, attrs, 1, 3, speeds, cong)
        assert result is not None
        assert result.segment_ids == [1, 2, 3]
        assert result.solve_status == "optimal"
        assert result.objective_value > 0
        assert not any(e["event"] == "mip_solver.subtours_discarded" for e in logs)

    def test_reconstruction_follows_cycles_without_truncating(self):
        # Active edges: path 1→2→3 plus a stray cycle through path node 2.
        path = ConvoyMIPSolver._reconstruct_path([(1, 2), (2, 5), (5, 2), (2, 3)], 1, 3)
        assert path == [1, 2, 3]

    def test_reconstruction_reports_a_path_that_misses_the_destination(self):
        path = ConvoyMIPSolver._reconstruct_path([(1, 2), (2, 4)], 1, 3)
        assert path[-1] != 3

    def test_solve_rejects_a_path_that_does_not_reach_the_destination(self, monkeypatch):
        attrs = motorway(1, 2, 3)
        monkeypatch.setattr(
            ConvoyMIPSolver, "_reconstruct_path", staticmethod(lambda e, o, d: [o, 2])
        )
        with capture_logs() as logs:
            result = ConvoyMIPSolver(WEIGHTS).solve(
                [(1, 2), (2, 3)], attrs, 1, 3, {s: 50.0 for s in attrs}, {s: 0.2 for s in attrs}
            )
        assert result is None
        assert any(e["event"] == "mip_solver.path_rejected" for e in logs)

    def test_security_still_steers_the_route_as_a_penalty(self):
        # Two equal-length branches; the residential one is less secure.
        attrs = {
            1: {"length_m": 100.0, "road_class": "motorway", "lanes": 3},
            2: {"length_m": 100.0, "road_class": "residential", "lanes": 3},
            3: {"length_m": 100.0, "road_class": "motorway", "lanes": 3},
            4: {"length_m": 100.0, "road_class": "motorway", "lanes": 3},
        }
        edges = [(1, 2), (2, 4), (1, 3), (3, 4)]
        speeds = {s: 50.0 for s in attrs}
        cong = {s: 0.0 for s in attrs}
        result = ConvoyMIPSolver(WEIGHTS).solve(edges, attrs, 1, 4, speeds, cong)
        assert result.segment_ids == [1, 3, 4]


# ─── Convoy router: the directed road_graph ───────────────────────────────────


class RouterConn:
    """corridor_router's queries on the repaired seed: the segment rows,
    the directed road_graph edges, the RAW segment_adjacency pairs (which
    still list the movements 012 excludes) — and no pgRouting (no KSP)."""

    def __init__(self) -> None:
        topo = seed_topology()
        self.segments = [
            {"segment_id": s.segment_id, "road_class": s.road_class, "lanes": s.lanes,
             "speed_limit_kmh": s.speed_limit_kmh, "length_m": s.length_m}
            for s in default_network().segments.values()
        ]
        self.edges = [{"from_segment_id": a, "to_segment_id": b} for a, b in topo.edges]
        self.adjacency = [
            {"from_segment_id": a, "to_segment_id": b} for a, b, *_ in topo.movements
        ]

    async def fetch(self, query: str, *args):
        if "pgr_" in query:
            return []
        if "corridor.road_graph" in query:
            return self.edges
        if "corridor.segment_adjacency" in query:
            return self.adjacency
        if "corridor.road_segments" in query:
            return self.segments
        raise AssertionError(f"unexpected query: {query}")


class TestConvoyRouterDirectedGraph:
    async def test_mip_never_drives_a_one_way_backwards(self):
        # Station Road (1014) → Nehru Bridge Road (1015) is two hops in the
        # raw adjacency up one-way Gandhi Road (1012, J3 → J15) from its end.
        # On the directed graph the convoy takes Relief Road (1005), the
        # two-way road between the same junctions.
        router = CorridorRouter(graph_db=CorridorGraphDB(pool=GraphPool(RouterConn())))
        candidates = await router.find_routes(1014, 1015)
        assert len(candidates) == 1  # the MIP route (no pgRouting here)
        path = candidates[0].segment_ids
        assert path == [1014, 1005, 1015]
        assert 1012 not in path
        assert set(itertools.pairwise(path)) <= seed_topology().edges.keys()
        # Ellis Bridge (1039) → Kankaria Road (1038): never back along
        # one-way Swami Vivekananda Road (1013, J5 → J6) from its end — over
        # the river, Lal Darwaja and Sarangpur instead.
        path = (await router.find_routes(1039, 1038))[0].segment_ids
        assert path == [1039, 1001, 1015, 1005, 1049, 1038]
        assert 1013 not in path

    async def test_the_airport_reaches_the_stadium_over_the_indira_bridge(self):
        # SVPI airport (Airport Road N 1028) → Motera Stadium Road (1032):
        # over the Indira Bridge (1048) and the Bhat - Motera road (1010).
        router = CorridorRouter(graph_db=CorridorGraphDB(pool=GraphPool(RouterConn())))
        path = (await router.find_routes(1028, 1032))[0].segment_ids
        assert path == [1028, 1048, 1010, 1032]
        net = default_network()
        assert sum(net.segments[s].length_m for s in path) < 12_000


# ─── DB tier (read-only) ──────────────────────────────────────────────────────

#: One-way segments with the junction at their geometry's start and end
#: (25 m — how the 012 view judges them).
_SQL_ONEWAY_ENDS = """
SELECT s.segment_id,
       (SELECT j.junction_id FROM corridor.junctions j
         WHERE ST_DWithin(j.geom::geography, ST_StartPoint(s.geom)::geography, 25)
         ORDER BY j.geom <-> ST_StartPoint(s.geom) LIMIT 1) AS start_junction_id,
       (SELECT j.junction_id FROM corridor.junctions j
         WHERE ST_DWithin(j.geom::geography, ST_EndPoint(s.geom)::geography, 25)
         ORDER BY j.geom <-> ST_EndPoint(s.geom) LIMIT 1) AS end_junction_id
FROM corridor.road_segments s
WHERE s.oneway
"""
_SQL_ADJACENCY = (
    "SELECT from_segment_id, to_segment_id, via_junction_id, turn_cost_sec "
    "FROM corridor.segment_adjacency"
)


async def open_pool(dsn: str):
    asyncpg = pytest.importorskip("asyncpg")
    try:
        return await asyncpg.create_pool(dsn, min_size=1, max_size=1, timeout=5)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database not reachable: {type(exc).__name__}")


async def store_movements(conn) -> tuple[list[Movement], dict[int, tuple[int | None, int | None]]]:
    """A store's RAW movement list and its one-way segments' end junctions:
    what ``replay_walk`` checks a detour against."""
    movements = [
        (r["from_segment_id"], r["to_segment_id"], r["via_junction_id"],
         float(r["turn_cost_sec"]))
        for r in await conn.fetch(_SQL_ADJACENCY)
    ]
    ends = {
        r["segment_id"]: (r["start_junction_id"], r["end_junction_id"])
        for r in await conn.fetch(_SQL_ONEWAY_ENDS)
    }
    return movements, ends


_DSN = os.environ.get("VVIP_DATABASE_URL")


@pytest.mark.skipif(not _DSN, reason="VVIP_DATABASE_URL not set (read-only graph tier)")
class TestPlannerOnRealGraph:
    async def test_acceptance_route_plans_on_the_real_corridor_graph(self):
        pool = await open_pool(_DSN)
        try:
            planner = build_diversion_planner(CorridorGraphDB(pool=pool), None)
            result = await planner.plan(
                ACCEPTANCE_ROUTE,
                advance_closure_sec=120,
                departure_time=coerce_departure_time("2026-10-23T09:00:00+05:30"),
            )
            async with pool.acquire() as conn:
                movements, oneway_ends = await store_movements(conn)
        finally:
            await pool.close()
        assert len(result.plans) >= 1
        for plan in result.plans:
            assert plan.segment_id not in plan.alt_segment_ids
            assert plan.detour_status in {"found", "no_path", "dead_end", "impractical"}
            assert plan.detour_found is (plan.detour_status == "found")
            if plan.detour_found:
                # Replayed against the DB's own movement list: a real crossing.
                replay_walk(
                    movements, plan.segment_id, plan.alt_segment_ids,
                    plan.detour_from_junction_id, plan.detour_to_junction_id,
                    plan.turnaround_segment_ids, oneway_ends,
                )
                assert plan.detour_distance_m <= plan.detour_limit_m
            elif plan.detour_status == "impractical":
                assert plan.alt_segment_ids == []
                assert plan.detour_distance_m > plan.detour_limit_m >= DETOUR_MAX_MIN_M
            else:
                assert plan.alt_segment_ids == [] and plan.detour_distance_m is None


_REPAIRED_DSN = os.environ.get("VEER_TEST_PG_DSN")


@pytest.mark.skipif(
    not _REPAIRED_DSN,
    reason="set VEER_TEST_PG_DSN to a store with 001-020 + the seed files "
           "(read-only checks)",
)
class TestRepairedStore:
    """The fake above against a seeded store: the same graph, row for
    row, and the same plans."""

    async def test_the_fake_is_the_store_graph(self):
        pool = await open_pool(_REPAIRED_DSN)
        try:
            async with pool.acquire() as conn:
                turns = await conn.fetch(_SQL_TURNS)
                segments = await conn.fetch(_SQL_SEGMENT_ATTRS)
                junctions = await conn.fetch(
                    "SELECT junction_id, ST_X(geom) AS lon, ST_Y(geom) AS lat "
                    "FROM corridor.junctions"
                )
                movements, oneway_ends = await store_movements(conn)
        finally:
            await pool.close()
        topo = seed_topology()
        fake = seed_graph_pool().conn

        def by_movement(rows) -> dict:
            return {(r["from_segment_id"], r["to_segment_id"], r["via_junction_id"]): r
                    for r in rows}

        store_turns, fake_turns = by_movement(turns), by_movement(fake.turns)
        assert len(store_turns) == len(turns)
        assert store_turns.keys() == fake_turns.keys()
        for key, row in store_turns.items():
            assert float(row["turn_cost_sec"]) == fake_turns[key]["turn_cost_sec"]
            # the twin's haversine lengths vs the spheroid: < 0.5 % apart
            assert float(row["travel_cost_sec"]) == pytest.approx(
                fake_turns[key]["travel_cost_sec"], rel=5e-3
            )
        assert sorted(movements) == sorted(topo.movements)
        assert oneway_ends == topo.oneway_ends
        assert {r["junction_id"] for r in junctions} == topo.junction_xy.keys()
        for r in junctions:
            assert (r["lon"], r["lat"]) == pytest.approx(
                topo.junction_xy[r["junction_id"]], abs=1e-9
            )
        fake_segments = {s["segment_id"]: s for s in fake.segments}
        assert {r["segment_id"] for r in segments} == fake_segments.keys()
        for r in segments:
            seg = fake_segments[r["segment_id"]]
            assert bool(r["oneway"]) is seg["oneway"]
            assert r["speed_limit_kmh"] == seg["speed_limit_kmh"]
            assert float(r["length_m"]) == pytest.approx(seg["length_m"], rel=5e-3)
            assert (r["start_lon"], r["start_lat"]) == pytest.approx(
                (seg["start_lon"], seg["start_lat"]), abs=1e-9
            )

    async def test_the_store_plans_the_acceptance_route_like_the_fake(self):
        pool = await open_pool(_REPAIRED_DSN)
        try:
            heuristic = await built(pool)
            routes = await heuristic.compute_diversions(ACCEPTANCE_ROUTE, predicted_speeds={})
            async with pool.acquire() as conn:
                movements, oneway_ends = await store_movements(conn)
        finally:
            await pool.close()
        assert {
            r.blocked_segment_id: (r.detour_status, r.alt_segment_ids,
                                   (r.detour_from_junction_id, r.detour_to_junction_id))
            for r in routes
        } == ACCEPTANCE_DETOURS
        assert {
            r.blocked_segment_id: (r.entry_pinned, r.exit_pinned) for r in routes
        } == ACCEPTANCE_PINS
        for r in routes:
            assert r.turnaround_segment_ids == []
            if r.detour_found:
                replay_walk(
                    movements, r.blocked_segment_id, r.alt_segment_ids,
                    r.detour_from_junction_id, r.detour_to_junction_id,
                    r.turnaround_segment_ids, oneway_ends,
                )
        # Gandhi Road's local way round (Relief Road) on the store's own
        # movements, and the loop that replaces it still a real walk there.
        replay_walk(movements, 1012, GANDHI_LOCAL, 3, 15, [], oneway_ends)
        replay_walk(movements, 1012, GANDHI_LOOP, 3, 15, [], oneway_ends)

    async def test_the_router_never_drives_gandhi_road_backwards(self):
        pool = await open_pool(_REPAIRED_DSN)
        try:
            router = CorridorRouter(graph_db=CorridorGraphDB(pool=pool))
            candidates = await router.find_routes(1014, 1015)
            async with pool.acquire() as conn:
                edges = {
                    (r["source"], r["target"])
                    for r in await conn.fetch("SELECT source, target FROM corridor.road_graph")
                }
        finally:
            await pool.close()
        assert candidates  # the MIP route + pgr_KSP alternatives
        for c in candidates:
            assert c.segment_ids[0] == 1014 and c.segment_ids[-1] == 1015
            assert (1014, 1012) not in set(itertools.pairwise(c.segment_ids))
            assert set(itertools.pairwise(c.segment_ids)) <= edges
