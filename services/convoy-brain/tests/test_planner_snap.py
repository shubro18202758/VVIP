"""Route planner snapping, candidate hygiene and narration merge (WP-04).

Root causes pinned here (api-e2e audit):
    - the planner took the FIRST bbox row, and the oracle orders bbox rows by
      segment_id — so it snapped to the lowest id, not the nearest road
      (Sarkhej–Ashram South, Lal Darwaja and Sabarmati Riverfront all
      snapped to 1001; 8 POI pairs snapped both ends to one segment);
    - MIP and KSP returned identical segment lists as two "alternatives";
    - Gemma's narration replaced the solver candidates, dropping
      estimated_time_sec / distance / security (the HUD showed ETA 0 min).
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from convoy_brain.agents.route_planner import (
    MAX_ROUTE_ATTEMPTS,
    SNAP_BOX_DEG,
    SNAP_BOX_WIDE_DEG,
    RoutePlannerAgent,
    _box_half_width_m,
    _haversine_m,
    _nearest_row_m,
    _point_to_lines_m,
    _snap_candidates,
)
from convoy_brain.config import AdaptiveParams
from conftest import (
    DESTINATION,
    NETWORK,
    ORIGIN,
    FakeExecutor,
    bbox_handler,
    details_handler,
    seg,
)
from test_agents import StubBridge


def route(rid, segments, *, eta=600, dist=6000.0, disruption=20.0, security=80.0,
          composite=10.0):
    return {
        "route_id": rid, "segment_ids": list(segments), "total_distance_m": dist,
        "estimated_time_sec": eta, "disruption_score": disruption,
        "security_score": security, "composite_score": composite,
    }


def executor_for(network, routes_by_pair=None, *, path_costs=None, bbox_calls=None):
    """FakeExecutor over a synthetic network.

    routes_by_pair: {(o, d): [route, ...]}; missing pairs return no routes.
    path_costs: {(o, d): seconds | None}; None = no path; missing pair =
    probe tool unavailable (raises).
    """
    routes_by_pair = routes_by_pair or {}

    def find(args):
        return {"routes": routes_by_pair.get(
            (args["origin_segment"], args["destination_segment"]), []
        )}

    handlers = {
        "query_segments_in_bbox": bbox_handler(network, bbox_calls),
        "query_segment_details": details_handler(network),
        "find_convoy_routes": find,
        "predict_traffic_flow": {"predictions": {}},
    }
    if path_costs is not None:
        def shortest(args):
            key = (args["source_segment"], args["target_segment"])
            if key not in path_costs:
                raise RuntimeError("probe unavailable")
            cost = path_costs[key]
            if cost is None:
                return {"path": []}
            return {"path": [
                {"seq": 1, "node": key[0], "edge": 1, "cost": cost, "agg_cost": 0.0},
                {"seq": 2, "node": key[1], "edge": -1, "cost": 0.0, "agg_cost": cost},
            ]}
        handlers["query_shortest_path"] = shortest
    return FakeExecutor(routes=handlers)


class TestGeometry:
    def test_point_to_polyline_distance_is_metric(self):
        line = [[(72.55, 23.02), (72.56, 23.02)]]
        # A point ~0.001° north of the line ≈ 110 m.
        assert 100 < _point_to_lines_m((72.555, 23.021), line) < 120
        assert _point_to_lines_m((72.55, 23.02), line) < 0.01


class TestNearestSnap:
    async def test_nearest_segment_wins_over_lowest_id(self):
        # Both rows fall in the ±0.002° box; the oracle lists 1001 first
        # (ORDER BY segment_id) but the point lies ON 1002.
        network = {
            1001: seg(1001, (72.5000, 23.0018), (72.5100, 23.0018)),  # ~200 m north
            1002: seg(1002, (72.5000, 23.0000), (72.5100, 23.0000)),
            1003: seg(1003, (72.5300, 23.0300), (72.5400, 23.0300)),
        }
        planner = RoutePlannerAgent(StubBridge(), executor_for(network))
        snap = await planner.resolve_endpoints((72.505, 23.0000), (72.535, 23.0300), [])
        assert snap["error"] is None
        assert snap["origin"]["segment_id"] == 1002
        assert snap["origin_candidates"] == [1002]  # 1001 is ~200 m away: not a tie
        assert snap["pairs"][0] == (1002, 1003)

    async def test_empty_box_widens_to_0_004(self):
        calls: list = []
        network = {
            1001: seg(1001, (72.5000, 23.0035), (72.5100, 23.0035)),  # 390 m north
            1002: seg(1002, (72.5300, 23.0300), (72.5400, 23.0300)),
        }
        planner = RoutePlannerAgent(StubBridge(), executor_for(network, bbox_calls=calls))
        snap = await planner.resolve_endpoints((72.505, 23.0000), (72.535, 23.0300), [])
        assert snap["origin"]["segment_id"] == 1001
        pads = sorted({round(c["max_lon"] - c["min_lon"], 4) for c in calls})
        assert pads == [round(2 * SNAP_BOX_DEG, 4), round(2 * SNAP_BOX_WIDE_DEG, 4)]

    async def test_a_far_nearest_widens_the_box_and_the_nearer_road_wins(self):
        # The oracle matches ENVELOPES: the small box returns only 7001, an
        # L-shaped road whose bounding box covers the point although the road
        # itself runs ~2.2 km away. 7002 lies ~300 m south — beyond the
        # small box's ~205 m half-width, so its envelope misses the box —
        # and is found only by the wider read, which then wins the snap.
        calls: list = []
        network = {
            7001: seg(7001, (72.480, 22.980), (72.480, 23.020)),
            7002: seg(7002, (72.500, 22.9973), (72.510, 22.9973)),
            7003: seg(7003, (72.540, 23.030), (72.550, 23.030)),
        }
        network[7001]["geometry"]["coordinates"].append([72.530, 23.020])  # the L's foot
        planner = RoutePlannerAgent(StubBridge(), executor_for(network, bbox_calls=calls))
        point = (72.505, 23.000)
        small = await planner._bbox_rows(point, SNAP_BOX_DEG)
        assert [r["segment_id"] for r in small] == [7001]
        assert _nearest_row_m(point, small) > 2_000 > _box_half_width_m(point, SNAP_BOX_DEG)
        calls.clear()
        snap = await planner.resolve_endpoints(point, (72.545, 23.030), [])
        assert snap["error"] is None
        assert snap["origin"]["segment_id"] == 7002
        assert 280 < snap["origin"]["distance_m"] < 320
        assert snap["origin_candidates"] == [7002]       # 7001 is no tie at 2.2 km
        pads = sorted(round(c["max_lon"] - c["min_lon"], 4) for c in calls)
        # origin: small then wide; destination (on its road): small only
        assert pads == [round(2 * SNAP_BOX_DEG, 4)] * 2 + [round(2 * SNAP_BOX_WIDE_DEG, 4)]

    async def test_a_road_within_the_half_width_keeps_the_small_box(self):
        # ~150 m from 8001: any nearer road would have to cross the small
        # box, so the small box's answer is final — one read per end.
        calls: list = []
        network = {
            8001: seg(8001, (72.500, 23.00135), (72.510, 23.00135)),
            8002: seg(8002, (72.540, 23.030), (72.550, 23.030)),
        }
        planner = RoutePlannerAgent(StubBridge(), executor_for(network, bbox_calls=calls))
        rows = await planner._snap_rows((72.505, 23.000))
        assert [r["segment_id"] for r in rows] == [8001]
        assert 140 < _nearest_row_m((72.505, 23.000), rows) < 160
        pads = [round(c["max_lon"] - c["min_lon"], 4) for c in calls]
        assert pads == [round(2 * SNAP_BOX_DEG, 4)]

    async def test_rows_without_geometry_never_widen(self):
        # A legacy payload (bare ids, no geometry) cannot be measured: the
        # oracle's own order stands, as before.
        calls: list = []

        def bare(args):
            calls.append(dict(args))
            return {"segments": [1002, 1001]}

        planner = RoutePlannerAgent(
            StubBridge(), FakeExecutor(routes={"query_segments_in_bbox": bare}))
        rows = await planner._snap_rows((72.505, 23.000))
        assert [r["segment_id"] for r in rows] == [1002, 1001]
        assert len(calls) == 1

    def test_the_half_width_is_the_boxs_inscribed_radius(self):
        # ±0.002° at Ahmedabad: ~205 m east-west (the narrower side), ~221 m
        # north-south — a road nearer than the narrower side crosses the box.
        half = _box_half_width_m((72.58, 23.03), SNAP_BOX_DEG)
        assert 200 < half < 210
        assert _box_half_width_m((72.58, 23.03), SNAP_BOX_WIDE_DEG) == pytest.approx(2 * half)

    async def test_unresolvable_point_is_a_clear_reason(self):
        planner = RoutePlannerAgent(StubBridge(), executor_for(NETWORK))
        snap = await planner.resolve_endpoints((73.5, 24.5), DESTINATION, [])
        assert "Could not resolve the origin" in snap["error"]

    async def test_pair_never_reuses_one_segment(self):
        # The direct trip lies on ONE segment (2001 spans O->D); every other
        # candidate touches only one end. The chosen pair must differ, and
        # the network probe decides which neighbour to add.
        network = {
            2001: seg(2001, (72.50, 23.00), (72.51, 23.00)),
            2002: seg(2002, (72.50, 23.00), (72.50, 23.01)),  # leaves O northward
            2003: seg(2003, (72.51, 23.00), (72.52, 23.00)),  # continues past D
        }
        costs = {(2001, 2003): 90.0, (2002, 2001): 140.0, (2002, 2003): 220.0}
        planner = RoutePlannerAgent(StubBridge(), executor_for(network, path_costs=costs))
        snap = await planner.resolve_endpoints((72.50, 23.00), (72.51, 23.00), [])
        assert snap["error"] is None
        assert all(o != d for o, d in snap["pairs"])
        assert snap["pairs"][0] == (2001, 2003)  # cheapest network cost

    async def test_single_shared_segment_is_rejected(self):
        network = {3001: seg(3001, (72.50, 23.00), (72.52, 23.00))}
        planner = RoutePlannerAgent(StubBridge(), executor_for(network))
        result = await planner.analyze_routes("m", (72.505, 23.00), (72.515, 23.00), "Z")
        assert result["primary_route"] is None
        assert "both resolve only to segment 3001" in result["overall_reasoning"]

    async def test_origin_equal_destination_is_rejected(self):
        planner = RoutePlannerAgent(StubBridge(), executor_for(NETWORK))
        result = await planner.analyze_routes("m", ORIGIN, ORIGIN, "Z")
        assert result["primary_route"] is None
        assert "apart" in result["overall_reasoning"]
        assert result["confidence"] == "low"

    async def test_unreachable_pair_falls_through_to_next(self):
        # 1001 and 1005 both touch O; the router has nothing for 1005 but
        # routes 1001 — the planner must try the next pair, not give up.
        costs = {(1005, 1003): 50.0, (1005, 1004): 60.0,
                 (1001, 1003): 300.0, (1001, 1004): 320.0}
        routes = {(1001, 1003): [route("mip_a", [1001, 1002, 1003])]}
        planner = RoutePlannerAgent(StubBridge(fail=True),
                                    executor_for(NETWORK, routes, path_costs=costs))
        result = await planner.analyze_routes("m", ORIGIN, DESTINATION, "Y")
        assert result["primary_route"]["route_id"] == "mip_a"
        attempts = result["snap"]["route_attempts"]
        assert attempts[0]["routes"] == 0 and attempts[-1]["routes"] == 1
        assert len(attempts) <= MAX_ROUTE_ATTEMPTS

    async def test_disconnected_network_is_named(self):
        costs = {(o, d): None for o in (1001, 1005) for d in (1003, 1004)}
        planner = RoutePlannerAgent(StubBridge(), executor_for(NETWORK, {}, path_costs=costs))
        result = await planner.analyze_routes("m", ORIGIN, DESTINATION, "Z")
        assert result["primary_route"] is None
        assert "no path at all" in result["overall_reasoning"]

    async def test_avoided_snap_candidate_is_skipped(self):
        planner = RoutePlannerAgent(StubBridge(), executor_for(NETWORK))
        snap = await planner.resolve_endpoints(ORIGIN, DESTINATION, [1003])
        assert 1003 not in snap["destination_candidates"]
        assert snap["destination_candidates"] == [1004]



def traced(row: dict, *drawn: tuple[float, float]) -> dict:
    """A bbox row as the C-K spatial router serves an OSM-traced segment: the
    DRAWN line as ``geometry`` (same endpoints), the routing line beside it."""
    first, last = row["geometry"]["coordinates"][0], row["geometry"]["coordinates"][-1]
    return {
        **row,
        "geometry": {"type": "LineString",
                     "coordinates": [first, *[list(p) for p in drawn], last]},
        "geometry_basis": "osm_display",
        "schematic_geometry": row["geometry"],
    }


class TestRoutingGeometrySnap:
    """Snapping routes, so it measures the schematic line (contract C-K)."""

    def test_the_drawn_line_never_wins_the_snap(self):
        # 4001's DRAWN road bends right through the point, but the line the
        # network routes on runs ~330 m south of it; 4002's routing line is
        # ~55 m away. The routing line decides.
        bent = traced(seg(4001, (72.500, 22.997), (72.510, 22.997)),
                      (72.505, 23.000))
        near = seg(4002, (72.500, 23.0005), (72.510, 23.0005))
        cands = _snap_candidates((72.505, 23.000), [bent, near], [])
        assert [c.segment_id for c in cands] == [4002]
        # measured on the routing line, the drawn one is ~0 m off the point
        only = _snap_candidates((72.505, 23.000), [bent], [])[0]
        assert 300 < only.distance_m < 360

    def test_length_and_far_end_come_from_the_routing_line(self):
        straight = seg(4003, (72.500, 23.000), (72.510, 23.000))
        detour = traced(straight, (72.503, 23.010), (72.507, 23.010))
        (cand,) = _snap_candidates((72.500, 23.000), [detour], [])
        schematic_m = _haversine_m((72.500, 23.000), (72.510, 23.000))
        assert cand.length_m == pytest.approx(schematic_m, rel=1e-6)  # not the ~3 km drawn
        assert cand.far_end == (72.510, 23.000)

    async def test_destination_traversal_cost_uses_the_routing_length(self):
        # The pair cost is the probe's agg_cost plus the destination segment's
        # traversal at its limit — on the schematic length, never the drawn one.
        network = {
            5001: seg(5001, (72.500, 23.000), (72.510, 23.000), speed=36.0),
            5002: traced(seg(5002, (72.520, 23.000), (72.530, 23.000), speed=36.0),
                         (72.523, 23.020), (72.527, 23.020)),
        }
        planner = RoutePlannerAgent(
            StubBridge(), executor_for(network, path_costs={(5001, 5002): 100.0})
        )
        snap = await planner.resolve_endpoints((72.500, 23.000), (72.530, 23.000), [])
        schematic_m = _haversine_m((72.520, 23.000), (72.530, 23.000))
        assert snap["pairs"][0] == (5001, 5002)
        assert snap["probe_costs_sec"]["5001->5002"] == pytest.approx(
            round(100.0 + schematic_m / 10.0, 1)  # 36 km/h = 10 m/s
        )


REPO = Path(__file__).resolve().parents[3]


def _osm_network_rows() -> dict[int, dict]:
    """The committed OpenStreetMap network (WP-20 evidence, 49 traced roads)
    as the oracle's spatial rows: routing line = the real road (basis 'osm')."""
    doc = json.loads((REPO / "scripts" / "data" / "osm_network.json").read_text(encoding="utf-8"))
    return {
        s["segment_id"]: {
            "segment_id": s["segment_id"], "road_name": s["road_name"],
            "road_class": s["road_class"], "lanes": s["lanes"],
            "speed_limit_kmh": s["speed_limit_kmh"], "oneway": s["oneway"],
            "length_m": s["length_m"],
            "geometry": {"type": "LineString", "coordinates": s["line"]},
        }
        for s in doc["segments"]
    }


def _poi_matrix_script():
    spec = importlib.util.spec_from_file_location(
        "poi_route_matrix", REPO / "scripts" / "poi_route_matrix.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestRealNetworkPoiSnaps:
    """The planner's own snap over the real network (the oracle's bbox
    semantics: envelope overlap, ORDER BY segment_id) resolves every
    acceptance-matrix / planner-rail POI to a road within the matrix's
    MAX_SNAP_M.  A point only a long road's bounding box reaches in the
    small box — the old Kankaria Lake point, which "snapped" to SP Ring Road
    1022 6.8 km away and turned 26 matrix pairs into 36-42 km detours — now
    widens to the larger box and lands on the road beside it."""

    async def test_every_matrix_poi_snaps_to_a_road_beside_it(self):
        matrix = _poi_matrix_script()
        planner = RoutePlannerAgent(StubBridge(), executor_for(_osm_network_rows()))
        nearest = {}
        for name, point in matrix.POIS:
            cands = _snap_candidates(point, await planner._snap_rows(point), [])
            assert cands, name
            assert cands[0].distance_m <= matrix.MAX_SNAP_M, (
                name, cands[0].segment_id, round(cands[0].distance_m))
            nearest[name] = cands[0].segment_id
        # the acceptance journey's ends and the lake on the roads beside them
        assert nearest["SVPI Airport"] == 1029           # Domestic Airport Road
        assert nearest["Narendra Modi Stadium"] == 1032  # Motera Stadium Road
        assert nearest["Kankaria Lake"] == 1038          # Kankaria Road, lake gate

    @pytest.mark.parametrize("point, small_only, road, lo, hi", [
        # the schematic-era Kankaria point: the small box holds only SP Ring
        # Road 1022 (its envelope), 6.8 km away; Kankaria Road is 272 m off
        ((72.6, 23.007), 1022, 1038, 250, 300),
        # the schematic-era SVPI point: only Naroda Road 1030 (1.9 km); the
        # airport spur, Domestic Airport Road 1029, is 376 m off
        ((72.6266, 23.0733), 1030, 1029, 350, 400),
    ])
    async def test_a_point_only_a_long_roads_envelope_reached_snaps_beside_it(
        self, point, small_only, road, lo, hi
    ):
        planner = RoutePlannerAgent(StubBridge(), executor_for(_osm_network_rows()))
        small = await planner._bbox_rows(point, SNAP_BOX_DEG)
        assert [r["segment_id"] for r in small] == [small_only]
        assert _nearest_row_m(point, small) > 1_500
        cands = _snap_candidates(point, await planner._snap_rows(point), [])
        assert cands[0].segment_id == road
        assert lo < cands[0].distance_m < hi

    async def test_the_stadium_end_keeps_its_road(self):
        # Narendra Modi Stadium's point lies ~553 m from Motera Stadium Road,
        # across the stadium's grounds: the wider read finds no nearer road.
        planner = RoutePlannerAgent(StubBridge(), executor_for(_osm_network_rows()))
        point = (72.5957, 23.0916)
        cands = _snap_candidates(point, await planner._snap_rows(point), [])
        assert [c.segment_id for c in cands] == [1032]
        assert 540 < cands[0].distance_m < 570

    def test_the_planner_rail_offers_the_matrix_pois(self):
        # The deck's planner rail (PlannerRail.svelte POIS) and the matrix
        # are one list: same names, same points, same order — so every pair
        # an operator can pick is a pair the matrix measured.
        rail = (REPO / "services" / "command-deck" / "src" / "lib" / "ops" / "components"
                / "template" / "PlannerRail.svelte").read_text(encoding="utf-8")
        block = rail[rail.index("const POIS"):]
        block = block[:block.index("];")]
        offered = [
            (name, (float(lon), float(lat)))
            for name, lon, lat in re.findall(
                r"\{\s*name:\s*'([^']+)',\s*coord:\s*\[\s*([\d.]+),\s*([\d.]+)\s*\]\s*\}", block)
        ]
        assert offered == list(_poi_matrix_script().POIS)


class TestCandidates:
    async def test_identical_mip_and_ksp_lists_are_deduplicated(self):
        routes = {(1001, 1003): [
            route("mip_x", [1001, 1002, 1003], composite=9.0),
            route("ksp_y", [1001, 1002, 1003], composite=9.0),
            route("ksp_z", [1001, 1002, 1004], composite=11.0),
        ]}
        planner = RoutePlannerAgent(StubBridge(fail=True), executor_for(NETWORK, routes))
        result = await planner.analyze_routes("m", ORIGIN, DESTINATION, "Y")
        ids = [result["primary_route"]["route_id"]] + [
            r["route_id"] for r in result["alternate_routes"]
        ]
        assert ids == ["mip_x", "ksp_z"]
        assert result["primary_route"]["duplicate_route_ids"] == ["ksp_y"]
        segment_lists = [tuple(r["segment_ids"]) for r in [result["primary_route"],
                                                           *result["alternate_routes"]]]
        assert len(set(segment_lists)) == len(segment_lists)

    async def test_narration_merges_onto_solver_metrics(self):
        routes = {(1001, 1003): [
            route("mip_x", [1001, 1002, 1004], eta=720, dist=7100.0, security=90.0),
            route("ksp_z", [1001, 1002, 1003], eta=650, dist=6500.0, security=70.0),
        ]}
        narration = json.dumps({
            # Gemma echoes ids but no metrics (and a wrong segment list).
            "primary_route": {"route_id": "mip_x", "segment_ids": [9], "score": 0.9,
                              "reason": "widest corridor: 6 lanes throughout"},
            "alternate_routes": [{"route_id": "ghost_1", "segment_ids": [1],
                                  "reason": "hallucinated"}],
            "rejected_routes": [],
            "overall_reasoning": "Bypass keeps the 6-lane floor.",
            "confidence": "high",
        })
        planner = RoutePlannerAgent(StubBridge(replies=[narration]),
                                    executor_for(NETWORK, routes))
        result = await planner.analyze_routes("m", ORIGIN, DESTINATION, "Z")
        primary = result["primary_route"]
        assert result["_tier"] == "gemma"
        assert primary["route_id"] == "mip_x"
        assert primary["segment_ids"] == [1001, 1002, 1004]  # solver's, not Gemma's
        assert primary["estimated_time_sec"] == 720
        assert primary["distance_m"] == 7100.0
        assert primary["security_score"] == 0.9  # 0-100 solver scale -> 0..1
        assert primary["reason"] == "widest corridor: 6 lanes throughout"
        assert result["overall_reasoning"] == "Bypass keeps the 6-lane floor."
        assert all(r["route_id"] != "ghost_1" for r in result["alternate_routes"])

    async def test_eta_is_never_zero(self):
        routes = {(1001, 1003): [route("mip_x", [1001, 1002, 1003], eta=0, dist=3000.0)]}
        planner = RoutePlannerAgent(StubBridge(fail=True), executor_for(NETWORK, routes))
        result = await planner.analyze_routes("m", ORIGIN, DESTINATION, "Y")
        primary = result["primary_route"]
        assert primary["estimated_time_sec"] > 0
        assert primary["eta_source"].startswith("assumed_")

    async def test_protocol_floor_rejects_while_a_compliant_route_exists(self):
        routes = {(1001, 1003): [
            route("ksp_narrow", [1001, 1002, 1003], composite=5.0),  # 1003 has 4 lanes
            route("mip_bypass", [1001, 1002, 1004], composite=9.0),
        ]}
        planner = RoutePlannerAgent(StubBridge(fail=True), executor_for(NETWORK, routes))
        result = await planner.analyze_routes("m", ORIGIN, DESTINATION, "Z+")
        assert result["primary_route"]["route_id"] == "mip_bypass"
        assert result["primary_route"]["route_tier"] == "compliant"
        assert [r["route_id"] for r in result["rejected_routes"]] == ["ksp_narrow"]
        assert "6-lane floor" in result["rejected_routes"][0]["reason"]

    async def test_all_conditional_candidates_are_kept_and_labelled(self):
        routes = {(1001, 1003): [route("mip_narrow", [1001, 1002, 1003])]}
        planner = RoutePlannerAgent(StubBridge(fail=True), executor_for(NETWORK, routes))
        result = await planner.analyze_routes("m", ORIGIN, DESTINATION, "Z+")
        primary = result["primary_route"]
        assert primary["route_tier"] == "conditional"
        assert [v["segment_id"] for v in primary["lane_violations"]] == [1003]
        assert "No fully compliant route" in result["overall_reasoning"]

    async def test_adaptive_weights_change_the_ranking(self):
        fast_disruptive = route("fast", [1001, 1002, 1004], eta=300, disruption=80.0)
        slow_quiet = route("quiet", [1005, 1001, 1002, 1004], eta=900, disruption=5.0)
        routes = {(1001, 1004): [fast_disruptive, slow_quiet],
                  (1001, 1003): [fast_disruptive, slow_quiet]}

        async def rank(weights):
            planner = RoutePlannerAgent(
                StubBridge(fail=True), executor_for(NETWORK, routes),
                adaptive=AdaptiveParams(scoring_weights=weights),
            )
            result = await planner.analyze_routes("m", ORIGIN, DESTINATION, "Y")
            return result["primary_route"]["route_id"]

        assert await rank({"time": 0.9, "disruption": 0.05, "security": 0.05,
                           "complexity": 0.0}) == "fast"
        assert await rank({"time": 0.05, "disruption": 0.9, "security": 0.05,
                           "complexity": 0.0}) == "quiet"
