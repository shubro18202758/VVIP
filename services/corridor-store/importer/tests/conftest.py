"""Shared fixtures for corridor-importer tests."""

from __future__ import annotations

import pytest
from shapely.geometry import LineString, Point

from corridor_importer.schema_mapper import MappedJunction, MappedSegment


@pytest.fixture
def sample_segments() -> list[MappedSegment]:
    """Small graph: 4 segments forming a diamond shape."""
    return [
        MappedSegment(
            osm_way_id=1001,
            from_node=1,
            to_node=2,
            road_class="primary",
            lanes=2,
            speed_limit_kmh=60.0,
            oneway=False,
            geom_wkt="LINESTRING(77.2 28.6, 77.21 28.61)",
        ),
        MappedSegment(
            osm_way_id=1002,
            from_node=2,
            to_node=3,
            road_class="primary",
            lanes=2,
            speed_limit_kmh=60.0,
            oneway=False,
            geom_wkt="LINESTRING(77.21 28.61, 77.22 28.62)",
        ),
        MappedSegment(
            osm_way_id=1003,
            from_node=2,
            to_node=4,
            road_class="secondary",
            lanes=1,
            speed_limit_kmh=40.0,
            oneway=True,
            geom_wkt="LINESTRING(77.21 28.61, 77.215 28.605)",
        ),
        MappedSegment(
            osm_way_id=1004,
            from_node=4,
            to_node=3,
            road_class="secondary",
            lanes=1,
            speed_limit_kmh=40.0,
            oneway=True,
            geom_wkt="LINESTRING(77.215 28.605, 77.22 28.62)",
        ),
    ]


@pytest.fixture
def sample_junctions() -> list[MappedJunction]:
    return [
        MappedJunction(osm_node_id=1, lon=77.2, lat=28.6, junction_type="intersection"),
        MappedJunction(osm_node_id=2, lon=77.21, lat=28.61, junction_type="signal"),
        MappedJunction(osm_node_id=3, lon=77.22, lat=28.62, junction_type="intersection"),
        MappedJunction(osm_node_id=4, lon=77.215, lat=28.605, junction_type="roundabout"),
    ]
