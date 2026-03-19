"""Maps OSM highway tags to corridor schema road classes."""

from __future__ import annotations

from dataclasses import dataclass

import geopandas as gpd
import structlog

logger = structlog.get_logger()

# OSM highway tag → (road_class, default_speed_limit_kmh)
HIGHWAY_MAP: dict[str, tuple[str, float]] = {
    "motorway": ("motorway", 120.0),
    "motorway_link": ("motorway_link", 80.0),
    "trunk": ("trunk", 100.0),
    "trunk_link": ("trunk_link", 60.0),
    "primary": ("primary", 80.0),
    "primary_link": ("primary_link", 50.0),
    "secondary": ("secondary", 60.0),
    "secondary_link": ("secondary_link", 40.0),
    "tertiary": ("tertiary", 50.0),
    "tertiary_link": ("tertiary_link", 30.0),
    "residential": ("residential", 30.0),
    "living_street": ("living_street", 20.0),
    "service": ("service", 20.0),
    "unclassified": ("unclassified", 40.0),
}


@dataclass
class MappedSegment:
    osm_way_id: int
    from_node: int
    to_node: int
    road_class: str
    lanes: int
    speed_limit_kmh: float
    oneway: bool
    geom_wkt: str


@dataclass
class MappedJunction:
    osm_node_id: int
    lon: float
    lat: float
    junction_type: str


class SchemaMapper:
    """Converts OSM attributes to corridor schema."""

    def map_edges(self, edges: gpd.GeoDataFrame) -> list[MappedSegment]:
        """Map OSM edge GeoDataFrame to MappedSegment list."""
        segments = []
        for idx, row in edges.iterrows():
            u, v, _ = idx if isinstance(idx, tuple) else (idx, 0, 0)
            highway = row.get("highway", "unclassified")
            if isinstance(highway, list):
                highway = highway[0]
            road_class, default_speed = HIGHWAY_MAP.get(highway, ("unclassified", 40.0))

            maxspeed = row.get("maxspeed")
            if maxspeed and isinstance(maxspeed, str) and maxspeed.isdigit():
                speed = float(maxspeed)
            else:
                speed = default_speed

            lanes_raw = row.get("lanes", 2)
            if isinstance(lanes_raw, str) and lanes_raw.isdigit():
                lanes = int(lanes_raw)
            elif isinstance(lanes_raw, (int, float)):
                lanes = int(lanes_raw)
            else:
                lanes = 2

            oneway_raw = row.get("oneway", False)
            oneway = oneway_raw is True or oneway_raw == "yes"

            geom = row.get("geometry")
            geom_wkt = geom.wkt if geom is not None else ""

            segments.append(MappedSegment(
                osm_way_id=int(row.get("osmid", 0)),
                from_node=int(u),
                to_node=int(v),
                road_class=road_class,
                lanes=lanes,
                speed_limit_kmh=speed,
                oneway=oneway,
                geom_wkt=geom_wkt,
            ))

        return segments

    def map_nodes(self, nodes: gpd.GeoDataFrame) -> list[MappedJunction]:
        """Map OSM node GeoDataFrame to MappedJunction list."""
        junctions = []
        for node_id, row in nodes.iterrows():
            highway = row.get("highway", "")
            if highway == "traffic_signals":
                jtype = "signal"
            elif highway == "mini_roundabout":
                jtype = "roundabout"
            else:
                jtype = "intersection"

            junctions.append(MappedJunction(
                osm_node_id=int(node_id),
                lon=float(row.get("x", 0)),
                lat=float(row.get("y", 0)),
                junction_type=jtype,
            ))

        return junctions
