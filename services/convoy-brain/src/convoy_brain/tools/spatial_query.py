"""Spatial Query Tool — PostGIS-backed geospatial queries via MCP ToolExecutor.

Provides structured access to road network topology, segment attributes,
and spatial relationships. LLM agents call this tool to ground their
reasoning in actual geographic data.
"""

from __future__ import annotations

import structlog

from convoy_brain.mcp.server import ToolExecutor

logger = structlog.get_logger(__name__)

_executor: ToolExecutor | None = None


def configure(executor: ToolExecutor) -> None:
    """Bind the module to a ToolExecutor instance."""
    global _executor
    _executor = executor
    logger.info("spatial_query.configured")


def _get_executor() -> ToolExecutor:
    if _executor is None:
        raise RuntimeError("spatial_query not configured — call configure(executor) first")
    return _executor


async def query_segments_in_bbox(
    bbox: tuple[float, float, float, float],
) -> list[dict]:
    """Find all road segments within a bounding box.

    Args:
        bbox: (min_lon, min_lat, max_lon, max_lat) in WGS84

    Returns:
        List of segment dicts with id, name, class, lanes, geometry
    """
    # Ahmedabad Strict Bounds
    AHM_MIN_LON, AHM_MIN_LAT = 72.45, 22.90
    AHM_MAX_LON, AHM_MAX_LAT = 72.70, 23.15
    
    clamped_min_lon = max(bbox[0], AHM_MIN_LON)
    clamped_min_lat = max(bbox[1], AHM_MIN_LAT)
    clamped_max_lon = min(bbox[2], AHM_MAX_LON)
    clamped_max_lat = min(bbox[3], AHM_MAX_LAT)
    
    if clamped_min_lon >= clamped_max_lon or clamped_min_lat >= clamped_max_lat:
        logger.warning("spatial_query.out_of_bounds", bbox=bbox)
        return []

    executor = _get_executor()
    logger.info("spatial_query.segments_in_bbox", original_bbox=bbox)
    result = await executor.execute("query_segments_in_bbox", {
        "min_lon": clamped_min_lon,
        "min_lat": clamped_min_lat,
        "max_lon": clamped_max_lon,
        "max_lat": clamped_max_lat,
    })
    if isinstance(result, dict) and "segments" in result:
        return result["segments"]
    if isinstance(result, list):
        return result
    return []


async def query_segment_attributes(segment_id: int) -> dict | None:
    """Get detailed attributes for a specific road segment."""
    executor = _get_executor()
    logger.info("spatial_query.segment_attrs", segment_id=segment_id)
    result = await executor.execute("query_segment_details", {"segment_id": segment_id})
    if isinstance(result, dict) and result.get("error"):
        return None
    return result if result else None


async def find_adjacent_segments(segment_id: int) -> list[dict]:
    """Find all segments connected to the given segment via junctions.

    Uses the multi-hop neighbor query with hops=1 for direct adjacency.
    """
    executor = _get_executor()
    logger.info("spatial_query.adjacent", segment_id=segment_id)
    # Use the shortest path tool to find neighbors via a self-query
    # or use a bbox around the segment — k_shortest_paths with same source/target
    # is not useful, so we use segments_in_bbox with a tight radius
    details = await query_segment_attributes(segment_id)
    if not details:
        return []

    # Get the segment's centroid and query nearby segments
    geom = details.get("geom_wkt", "")
    if not geom:
        return []

    # Parse first coordinate from WKT as centroid approximation
    lon, lat = _extract_centroid(geom)
    if lon is None:
        return []

    return await query_segments_in_bbox((lon - 0.001, lat - 0.001, lon + 0.001, lat + 0.001))


async def find_nearest_segment(lon: float, lat: float) -> dict | None:
    """Find the nearest road segment to a WGS84 coordinate."""
    executor = _get_executor()
    logger.info("spatial_query.nearest", lon=lon, lat=lat)
    # Use a small bbox centered on the point
    segments = await query_segments_in_bbox((lon - 0.001, lat - 0.001, lon + 0.001, lat + 0.001))
    if segments:
        return segments[0]  # Closest by default sort order from PostGIS
    # Expand search radius
    segments = await query_segments_in_bbox((lon - 0.005, lat - 0.005, lon + 0.005, lat + 0.005))
    return segments[0] if segments else None


def _extract_centroid(geom_wkt: str) -> tuple[float | None, float | None]:
    """Extract approximate centroid from WKT geometry string."""
    # Handle LINESTRING(lon1 lat1, lon2 lat2, ...) or POINT(lon lat)
    try:
        inner = geom_wkt.split("(", 1)[1].rstrip(")")
        coords = inner.split(",")
        mid = coords[len(coords) // 2].strip()
        parts = mid.split()
        return float(parts[0]), float(parts[1])
    except (IndexError, ValueError):
        return None, None
