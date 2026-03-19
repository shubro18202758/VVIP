"""Extracts road networks from OpenStreetMap via osmnx."""

from __future__ import annotations

import geopandas as gpd
import osmnx as ox
import structlog

logger = structlog.get_logger()


class OSMExtractor:
    """Downloads OSM road network data and returns GeoDataFrames."""

    def __init__(self, network_type: str = "drive") -> None:
        self._network_type = network_type

    def extract_by_bbox(
        self, south: float, north: float, west: float, east: float
    ) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
        """Extract road network within a bounding box.

        Returns (nodes_gdf, edges_gdf).
        """
        G = ox.graph_from_bbox(
            bbox=(north, south, east, west),
            network_type=self._network_type,
        )
        nodes, edges = ox.graph_to_gdfs(G)
        logger.info(
            "OSM extraction complete (bbox)",
            nodes=len(nodes), edges=len(edges),
        )
        return nodes, edges

    def extract_by_place(self, place: str) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
        """Extract road network by geocoded place name."""
        G = ox.graph_from_place(place, network_type=self._network_type)
        nodes, edges = ox.graph_to_gdfs(G)
        logger.info(
            "OSM extraction complete (place)",
            place=place, nodes=len(nodes), edges=len(edges),
        )
        return nodes, edges
