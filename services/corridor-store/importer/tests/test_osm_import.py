"""Tests for the GIS network importer: SchemaMapper, TopologyBuilder."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

from corridor_importer.schema_mapper import (
    HIGHWAY_MAP,
    MappedJunction,
    MappedSegment,
    SchemaMapper,
)
from corridor_importer.topology_builder import (
    TURN_COSTS,
    AdjacencyEdge,
    TopologyBuilder,
)


# ─── SchemaMapper Tests ──────────────────────────────────────────────────────


class TestSchemaMapper:
    def _make_edges_gdf(self, rows: list[dict]) -> gpd.GeoDataFrame:
        """Build a mock edges GeoDataFrame with multi-index (u, v, key)."""
        index = pd.MultiIndex.from_tuples(
            [(r["u"], r["v"], 0) for r in rows], names=["u", "v", "key"]
        )
        data = {
            "highway": [r.get("highway", "primary") for r in rows],
            "osmid": [r.get("osmid", 1000 + i) for i, r in enumerate(rows)],
            "lanes": [r.get("lanes", 2) for r in rows],
            "maxspeed": [r.get("maxspeed", None) for r in rows],
            "oneway": [r.get("oneway", False) for r in rows],
            "geometry": [
                r.get("geometry", LineString([(77.2, 28.6), (77.21, 28.61)])) for r in rows
            ],
        }
        return gpd.GeoDataFrame(data, index=index)

    def _make_nodes_gdf(self, rows: list[dict]) -> gpd.GeoDataFrame:
        """Build a mock nodes GeoDataFrame."""
        index = pd.Index([r["node_id"] for r in rows])
        data = {
            "x": [r.get("x", 77.2) for r in rows],
            "y": [r.get("y", 28.6) for r in rows],
            "highway": [r.get("highway", "") for r in rows],
        }
        return gpd.GeoDataFrame(data, index=index)

    def test_map_edges_basic(self):
        gdf = self._make_edges_gdf([
            {"u": 1, "v": 2, "highway": "primary", "osmid": 5001},
        ])
        mapper = SchemaMapper()
        segments = mapper.map_edges(gdf)

        assert len(segments) == 1
        seg = segments[0]
        assert seg.osm_way_id == 5001
        assert seg.from_node == 1
        assert seg.to_node == 2
        assert seg.road_class == "primary"
        assert seg.speed_limit_kmh == 80.0  # HIGHWAY_MAP default for primary

    def test_map_edges_uses_maxspeed(self):
        gdf = self._make_edges_gdf([
            {"u": 1, "v": 2, "highway": "motorway", "maxspeed": "100"},
        ])
        mapper = SchemaMapper()
        segments = mapper.map_edges(gdf)

        assert segments[0].speed_limit_kmh == 100.0

    def test_map_edges_list_highway_tag(self):
        """Some OSM ways have list-valued highway tags."""
        gdf = self._make_edges_gdf([
            {"u": 1, "v": 2, "highway": ["tertiary", "residential"]},
        ])
        mapper = SchemaMapper()
        segments = mapper.map_edges(gdf)

        assert segments[0].road_class == "tertiary"

    def test_map_edges_unknown_highway(self):
        gdf = self._make_edges_gdf([
            {"u": 1, "v": 2, "highway": "bridleway"},
        ])
        mapper = SchemaMapper()
        segments = mapper.map_edges(gdf)

        assert segments[0].road_class == "unclassified"
        assert segments[0].speed_limit_kmh == 40.0

    def test_map_edges_oneway(self):
        gdf = self._make_edges_gdf([
            {"u": 1, "v": 2, "oneway": True},
        ])
        mapper = SchemaMapper()
        segments = mapper.map_edges(gdf)

        assert segments[0].oneway is True

    def test_map_edges_oneway_string(self):
        gdf = self._make_edges_gdf([
            {"u": 1, "v": 2, "oneway": "yes"},
        ])
        mapper = SchemaMapper()
        segments = mapper.map_edges(gdf)

        assert segments[0].oneway is True

    def test_map_edges_string_lanes(self):
        gdf = self._make_edges_gdf([
            {"u": 1, "v": 2, "lanes": "4"},
        ])
        mapper = SchemaMapper()
        segments = mapper.map_edges(gdf)

        assert segments[0].lanes == 4

    def test_map_edges_geometry_wkt(self):
        line = LineString([(77.0, 28.0), (77.1, 28.1), (77.2, 28.2)])
        gdf = self._make_edges_gdf([
            {"u": 1, "v": 2, "geometry": line},
        ])
        mapper = SchemaMapper()
        segments = mapper.map_edges(gdf)

        assert "LINESTRING" in segments[0].geom_wkt

    def test_map_nodes_signal(self):
        gdf = self._make_nodes_gdf([
            {"node_id": 10, "x": 77.1, "y": 28.5, "highway": "traffic_signals"},
        ])
        mapper = SchemaMapper()
        junctions = mapper.map_nodes(gdf)

        assert len(junctions) == 1
        assert junctions[0].junction_type == "signal"

    def test_map_nodes_roundabout(self):
        gdf = self._make_nodes_gdf([
            {"node_id": 20, "highway": "mini_roundabout"},
        ])
        mapper = SchemaMapper()
        junctions = mapper.map_nodes(gdf)

        assert junctions[0].junction_type == "roundabout"

    def test_map_nodes_default_intersection(self):
        gdf = self._make_nodes_gdf([
            {"node_id": 30, "highway": ""},
        ])
        mapper = SchemaMapper()
        junctions = mapper.map_nodes(gdf)

        assert junctions[0].junction_type == "intersection"

    def test_all_highway_map_entries_have_class_and_speed(self):
        for key, (road_class, speed) in HIGHWAY_MAP.items():
            assert isinstance(road_class, str)
            assert speed > 0


# ─── TopologyBuilder Tests ────────────────────────────────────────────────────


class TestTopologyBuilder:
    def test_build_adjacency_connected(self, sample_segments):
        builder = TopologyBuilder()
        adjacency = builder.build_adjacency(sample_segments)

        assert len(adjacency) > 0
        assert all(isinstance(e, AdjacencyEdge) for e in adjacency)

    def test_adjacency_excludes_self_loops(self, sample_segments):
        builder = TopologyBuilder()
        adjacency = builder.build_adjacency(sample_segments)

        for edge in adjacency:
            assert edge.from_segment_id != edge.to_segment_id

    def test_adjacency_connects_sequential(self, sample_segments):
        """Segment 1001 (to_node=2) should be adjacent to 1002 (from_node=2)."""
        builder = TopologyBuilder()
        adjacency = builder.build_adjacency(sample_segments)

        pairs = {(e.from_segment_id, e.to_segment_id) for e in adjacency}
        assert (1001, 1002) in pairs

    def test_adjacency_connects_to_branch(self, sample_segments):
        """Segment 1001 (to_node=2) should connect to 1003 (from_node=2)."""
        builder = TopologyBuilder()
        adjacency = builder.build_adjacency(sample_segments)

        pairs = {(e.from_segment_id, e.to_segment_id) for e in adjacency}
        assert (1001, 1003) in pairs

    def test_bidirectional_links_for_non_oneway(self, sample_segments):
        """Non-oneway segments should appear in reverse adjacency too."""
        builder = TopologyBuilder()
        adjacency = builder.build_adjacency(sample_segments)

        pairs = {(e.from_segment_id, e.to_segment_id) for e in adjacency}
        # 1002 (to_node=3, not oneway) → 1004 connects to node 3
        # So 1004 should be adjacent from 1002 via node 3
        assert (1004, 1002) in pairs or (1002, 1004) in pairs

    def test_turn_cost_same_class(self, sample_segments):
        """Same road class should have straight (0.0) turn cost."""
        builder = TopologyBuilder()
        adjacency = builder.build_adjacency(sample_segments)

        # 1001 → 1002: both primary
        for e in adjacency:
            if e.from_segment_id == 1001 and e.to_segment_id == 1002:
                assert e.turn_cost_sec == TURN_COSTS["straight"]
                break

    def test_turn_cost_different_class(self, sample_segments):
        """Different road class should get right-turn cost."""
        builder = TopologyBuilder()
        adjacency = builder.build_adjacency(sample_segments)

        # 1001 (primary) → 1003 (secondary)
        for e in adjacency:
            if e.from_segment_id == 1001 and e.to_segment_id == 1003:
                assert e.turn_cost_sec == TURN_COSTS["right"]
                break

    def test_empty_segments(self):
        builder = TopologyBuilder()
        adjacency = builder.build_adjacency([])
        assert adjacency == []

    def test_single_segment_no_adjacency(self):
        seg = MappedSegment(
            osm_way_id=9999,
            from_node=1,
            to_node=2,
            road_class="tertiary",
            lanes=1,
            speed_limit_kmh=30.0,
            oneway=True,
            geom_wkt="LINESTRING(77.0 28.0, 77.1 28.1)",
        )
        builder = TopologyBuilder()
        adjacency = builder.build_adjacency([seg])
        assert adjacency == []

    def test_turn_costs_defined(self):
        assert "straight" in TURN_COSTS
        assert "right" in TURN_COSTS
        assert "left" in TURN_COSTS
        assert "u_turn" in TURN_COSTS
        assert "roundabout" in TURN_COSTS
        assert TURN_COSTS["u_turn"] > TURN_COSTS["left"] > TURN_COSTS["right"]
