"""Corridor Router — multi-objective route optimization for VVIP convoy paths.

Uses OR-Tools CP-SAT MIP solver for optimal convoy routing on the corridor
road graph, with K-shortest-path alternatives via pgRouting, and ETA
estimation via gradient-boosted trees. All computation is CPU-only (zero VRAM).

Optimization objectives (weighted):
    1. Minimize total convoy transit time (0.3)
    2. Minimize public traffic disruption (0.4)
    3. Maximize security compliance (0.2)
    4. Minimize diversion complexity (0.1)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog

from traffic_oracle.data.graph_queries import CorridorGraphDB
from traffic_oracle.optimize.mip_solver import ConvoyMIPSolver
from traffic_oracle.predict.eta_model import ETAPredictor
from traffic_oracle.predict.flow_forecaster import FlowForecaster
from traffic_oracle.runtime.memory_profiler import profile_memory

logger = structlog.get_logger(__name__)

ROAD_CLASS_SCORES = {
    "motorway": 100, "trunk": 85, "primary": 70,
    "secondary": 50, "tertiary": 30, "residential": 10,
}


@dataclass
class RouteCandidate:
    """A scored route option for convoy movement."""

    route_id: str
    segment_ids: list[int]
    total_distance_m: float
    estimated_time_sec: int
    disruption_score: float  # 0-100, lower is better
    security_score: float    # 0-100, higher is better
    composite_score: float   # Weighted combination


class CorridorRouter:
    """Multi-objective convoy route optimizer using OR-Tools on the road graph.

    Runs entirely on CPU — zero VRAM consumption. Safe to run concurrently
    with LLM inference without memory contention.
    """

    def __init__(
        self,
        graph_db: CorridorGraphDB | None = None,
        forecaster: FlowForecaster | None = None,
        eta_predictor: ETAPredictor | None = None,
        disruption_weight: float = 0.4,
        time_weight: float = 0.3,
        security_weight: float = 0.2,
        complexity_weight: float = 0.1,
    ) -> None:
        self._graph_db = graph_db
        self._forecaster = forecaster
        self._eta_predictor = eta_predictor
        self._weights = {
            "disruption": disruption_weight,
            "time": time_weight,
            "security": security_weight,
            "complexity": complexity_weight,
        }
        self._mip_solver = ConvoyMIPSolver(self._weights)
        logger.info("corridor_router.init", weights=self._weights)

    @profile_memory
    async def find_routes(
        self,
        origin_segment: int,
        destination_segment: int,
        max_candidates: int = 5,
        avoid_segments: list[int] | None = None,
    ) -> list[RouteCandidate]:
        """Find top-K convoy routes between origin and destination.

        Args:
            origin_segment: Starting road segment ID
            destination_segment: Ending road segment ID
            max_candidates: Maximum number of route options to return
            avoid_segments: Segments to exclude (e.g., known construction)

        Returns:
            Scored and ranked route candidates
        """
        avoid_set = set(avoid_segments or [])
        candidates: list[RouteCandidate] = []

        # Get predicted conditions for routing area
        predicted_speeds: dict[int, float] = {}
        predicted_congestion: dict[int, float] = {}

        if self._forecaster and self._graph_db:
            # Fetch segments in routing area
            neighbors = await self._graph_db.multi_hop_neighbors(origin_segment, hops=5)
            neighbors.extend(
                await self._graph_db.multi_hop_neighbors(destination_segment, hops=5)
            )
            all_segments = list(set(neighbors + [origin_segment, destination_segment]))

            forecasts = await self._forecaster.predict(all_segments, [5])
            for sid, preds in forecasts.items():
                if 5 in preds:
                    predicted_speeds[sid] = preds[5]["speed_kmh"]
                    predicted_congestion[sid] = preds[5]["congestion_idx"]

        # Load segment attributes for MIP solver
        segment_attrs: dict[int, dict] = {}
        edges: list[tuple[int, int]] = []

        if self._graph_db:
            pool = self._graph_db._pool
            async with pool.acquire() as conn:
                seg_rows = await conn.fetch("""
                    SELECT segment_id, road_class, lanes, speed_limit_kmh,
                           ST_Length(geom::geography) AS length_m
                    FROM corridor.road_segments
                """)
                for r in seg_rows:
                    segment_attrs[r["segment_id"]] = {
                        "road_class": r["road_class"],
                        "lanes": r["lanes"],
                        "speed_limit_kmh": r["speed_limit_kmh"],
                        "length_m": r["length_m"],
                    }

                adj_rows = await conn.fetch("""
                    SELECT from_segment_id, to_segment_id
                    FROM corridor.segment_adjacency
                """)
                edges = [(r["from_segment_id"], r["to_segment_id"]) for r in adj_rows]

        # 1. MIP optimal route
        mip_result = self._mip_solver.solve(
            edges=edges,
            segment_attrs=segment_attrs,
            origin=origin_segment,
            destination=destination_segment,
            predicted_speeds=predicted_speeds,
            predicted_congestion=predicted_congestion,
            avoid_segments=avoid_set,
        )

        if mip_result:
            candidates.append(self._make_candidate(
                mip_result.segment_ids,
                segment_attrs,
                predicted_speeds,
                predicted_congestion,
                source="mip",
            ))

        # 2. K-shortest path alternatives via pgRouting
        if self._graph_db:
            k_paths = await self._graph_db.k_shortest_paths(
                origin_segment, destination_segment, k=max_candidates - 1
            )
            for path_rows in k_paths:
                path_nodes = [r["node"] for r in path_rows if r["node"] > 0]
                if path_nodes and not avoid_set.intersection(path_nodes):
                    candidates.append(self._make_candidate(
                        path_nodes,
                        segment_attrs,
                        predicted_speeds,
                        predicted_congestion,
                        source="ksp",
                    ))

        # Score and sort
        candidates.sort(key=lambda c: c.composite_score)

        logger.info(
            "corridor_router.find_routes",
            origin=origin_segment,
            dest=destination_segment,
            candidates=len(candidates),
        )
        return candidates[:max_candidates]

    def _make_candidate(
        self,
        segment_ids: list[int],
        segment_attrs: dict[int, dict],
        predicted_speeds: dict[int, float],
        predicted_congestion: dict[int, float],
        source: str,
    ) -> RouteCandidate:
        """Build a scored RouteCandidate from a path."""
        total_dist = sum(
            segment_attrs.get(s, {}).get("length_m", 500.0) for s in segment_ids
        )

        # Estimated time from predicted speeds
        total_time = 0.0
        for sid in segment_ids:
            length = segment_attrs.get(sid, {}).get("length_m", 500.0)
            speed = predicted_speeds.get(sid, 40.0)
            total_time += length / max(speed * 1000 / 3600, 0.1)

        # Disruption: avg congestion × segment count
        avg_cong = sum(
            predicted_congestion.get(s, 0.3) for s in segment_ids
        ) / max(len(segment_ids), 1)
        disruption = avg_cong * len(segment_ids) * 10  # scale to 0-100

        # Security: avg road class score
        avg_security = sum(
            ROAD_CLASS_SCORES.get(
                segment_attrs.get(s, {}).get("road_class", "secondary"), 50
            )
            for s in segment_ids
        ) / max(len(segment_ids), 1)

        # Composite score (lower is better)
        w = self._weights
        composite = (
            w["time"] * (total_time / 60)
            + w["disruption"] * disruption
            - w["security"] * (avg_security / 100)
            + w["complexity"] * len(segment_ids)
        )

        return RouteCandidate(
            route_id=f"{source}_{uuid.uuid4().hex[:8]}",
            segment_ids=segment_ids,
            total_distance_m=round(total_dist, 1),
            estimated_time_sec=int(total_time),
            disruption_score=round(disruption, 1),
            security_score=round(avg_security, 1),
            composite_score=round(composite, 3),
        )
