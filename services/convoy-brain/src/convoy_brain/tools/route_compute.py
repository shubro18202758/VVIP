"""Route Compute Tool — triggers route optimization on traffic-oracle via MCP ToolExecutor."""

from __future__ import annotations

import structlog

from convoy_brain.mcp.server import ToolExecutor

logger = structlog.get_logger(__name__)

_executor: ToolExecutor | None = None


def configure(executor: ToolExecutor) -> None:
    """Bind the module to a ToolExecutor instance."""
    global _executor
    _executor = executor
    logger.info("route_compute.configured")


def _get_executor() -> ToolExecutor:
    if _executor is None:
        raise RuntimeError("route_compute not configured — call configure(executor) first")
    return _executor


async def compute_routes(
    origin_lon: float,
    origin_lat: float,
    dest_lon: float,
    dest_lat: float,
    vvip_class: str = "Y",
    max_candidates: int = 5,
    avoid_segments: list[int] | None = None,
) -> list[dict]:
    """Request route computation from the traffic-oracle corridor_router.

    First resolves lon/lat to nearest segment IDs, then calls the MIP solver.

    Args:
        origin_lon, origin_lat: Convoy origin in WGS84
        dest_lon, dest_lat: Convoy destination in WGS84
        vvip_class: Security classification for road width constraints
        max_candidates: Number of route options to generate
        avoid_segments: Segments to exclude from routing

    Returns:
        List of route candidates with scores and geometries
    """
    executor = _get_executor()
    logger.info(
        "route_compute.request",
        origin=(origin_lon, origin_lat),
        dest=(dest_lon, dest_lat),
        vvip_class=vvip_class,
        max_k=max_candidates,
    )

    # Step 1: Resolve coordinates to nearest segments
    origin_seg = await executor.execute("query_segments_in_bbox", {
        "min_lon": origin_lon - 0.002,
        "min_lat": origin_lat - 0.002,
        "max_lon": origin_lon + 0.002,
        "max_lat": origin_lat + 0.002,
    })
    dest_seg = await executor.execute("query_segments_in_bbox", {
        "min_lon": dest_lon - 0.002,
        "min_lat": dest_lat - 0.002,
        "max_lon": dest_lon + 0.002,
        "max_lat": dest_lat + 0.002,
    })

    origin_id = _extract_nearest_segment(origin_seg)
    dest_id = _extract_nearest_segment(dest_seg)

    if origin_id is None or dest_id is None:
        logger.warning("route_compute.no_segments_found", origin_id=origin_id, dest_id=dest_id)
        return []

    # Step 2: Call the MIP-based route optimizer
    result = await executor.execute("find_convoy_routes", {
        "origin_segment": origin_id,
        "destination_segment": dest_id,
        "max_candidates": max_candidates,
        "avoid_segments": avoid_segments or [],
    })

    if isinstance(result, dict) and "routes" in result:
        return result["routes"]
    if isinstance(result, list):
        return result
    return [result] if result else []


def _extract_nearest_segment(result: dict | list) -> int | None:
    """Extract the first segment ID from a spatial query result."""
    segments = result if isinstance(result, list) else result.get("segments", [])
    if segments and isinstance(segments[0], dict):
        return segments[0].get("segment_id")
    if segments and isinstance(segments[0], int):
        return segments[0]
    return None
