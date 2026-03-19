"""Traffic Fetch Tool — retrieves real-time and forecasted traffic data for LLM agents.

Delegates to the MCP ToolExecutor which routes requests to the traffic-oracle service.
"""

from __future__ import annotations

import structlog

from convoy_brain.mcp.server import ToolExecutor

logger = structlog.get_logger(__name__)

# Module-level executor reference — set by the application bootstrap
_executor: ToolExecutor | None = None


def configure(executor: ToolExecutor) -> None:
    """Bind the module to a ToolExecutor instance."""
    global _executor
    _executor = executor
    logger.info("traffic_fetch.configured")


def _get_executor() -> ToolExecutor:
    if _executor is None:
        raise RuntimeError("traffic_fetch not configured — call configure(executor) first")
    return _executor


async def get_live_traffic(segment_ids: list[int]) -> list[dict]:
    """Fetch current traffic conditions from Valkey cache via traffic-oracle.

    Returns per-segment: speed_kmh, congestion_idx, last_updated.
    """
    executor = _get_executor()
    logger.info("traffic_fetch.live", segments=len(segment_ids))
    result = await executor.execute("get_live_traffic", {"segment_ids": segment_ids})
    if isinstance(result, dict) and "observations" in result:
        return result["observations"]
    if isinstance(result, list):
        return result
    return [result] if result else []


async def get_forecast(
    segment_ids: list[int],
    horizons_min: list[int] | None = None,
) -> list[dict]:
    """Fetch traffic forecast from the DSTGAT model in traffic-oracle.

    Returns per-segment per-horizon: predicted_speed_kmh, predicted_congestion_idx.
    """
    if horizons_min is None:
        horizons_min = [5, 10, 15, 30]
    executor = _get_executor()
    logger.info("traffic_fetch.forecast", segments=len(segment_ids), horizons=horizons_min)
    result = await executor.execute("predict_traffic_flow", {
        "segment_ids": segment_ids,
        "horizons_min": horizons_min,
    })
    if isinstance(result, dict) and "predictions" in result:
        return result["predictions"]
    if isinstance(result, list):
        return result
    return [result] if result else []


async def get_historical_pattern(
    segment_id: int,
    day_of_week: int,
    hour: int,
) -> dict | None:
    """Fetch historical traffic pattern from DuckDB aggregates via traffic-oracle.

    Returns: avg_speed_kmh, p50_speed_kmh, p95_congestion for the given time slot.
    """
    executor = _get_executor()
    logger.info(
        "traffic_fetch.historical",
        segment_id=segment_id,
        dow=day_of_week,
        hour=hour,
    )
    result = await executor.execute("get_historical_pattern", {
        "segment_id": segment_id,
        "day_of_week": day_of_week,
        "hour": hour,
    })
    if isinstance(result, dict) and result.get("error"):
        return None
    return result if result else None
