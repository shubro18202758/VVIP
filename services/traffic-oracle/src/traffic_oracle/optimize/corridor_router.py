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
from traffic_oracle.optimize.quantile_objective import (
    RobustCostResult,
    SegmentQuantiles,
    resolve_robust_knob,
    robust_edge_costs,
)
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
    # Robust (P90) overlays — additive optional fields; None whenever the
    # quantile stream was absent/disabled/non-monotonic.  Today's keys
    # (estimated_time_sec &c.) keep their point/p50 meaning — the deck's
    # plannedEtaMin and the MCP passthrough consume them unchanged.
    estimated_time_p90_sec: int | None = None
    disruption_p90: float | None = None


def _segment_quantiles_from_row(
    segment_id: int, horizon: int, row: dict
) -> SegmentQuantiles | None:
    """Map a forecast row's optional ``quantiles`` object (the contract
    shape: {speed_kmh: {p10,p50,p90}, congestion_idx: {p10,p50,p90}},
    knots non-decreasing per field — the forecast_quantiles producer
    convention) onto the producer-agnostic SegmentQuantiles leaf input.

    Thin mapping layer ONLY.  Knot values pass through UNCOERCED so the
    leaf's strict validation is the single authority: bools, strings and
    NaN label the row ``invalid_quantiles:<sid>`` instead of silently
    coercing, and a present-but-shape-broken object maps to empty knot
    dicts (labelled ``invalid_quantiles`` by the leaf) rather than
    degrading to "absent".  A genuinely absent object (point-only row)
    returns None.  If the producer side changes stream shape, this
    function alone adapts.
    """
    if not isinstance(row, dict) or "quantiles" not in row:
        return None
    raw = row["quantiles"]
    speeds = raw.get("speed_kmh") if isinstance(raw, dict) else None
    cong = raw.get("congestion_idx") if isinstance(raw, dict) else None
    return SegmentQuantiles(
        segment_id=segment_id,
        horizon_min=horizon,
        speed_kmh_by_q=dict(speeds) if isinstance(speeds, dict) else {},
        congestion_idx_by_q=dict(cong) if isinstance(cong, dict) else {},
    )


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
        robust_routing: dict[str, float | bool] | None = None,
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
        # Robust-routing knob (P90 objective): canonical baseline from the
        # generated protocol module; quantile clamped to
        # [ROBUST_QUANTILE_MIN, ROBUST_QUANTILE_MAX]; enabled=false is the
        # auditable one-key revert to today's point objective.
        self._robust = resolve_robust_knob(robust_routing)
        #: Labelled reason for the last find_routes objective run:
        #: "quantile" when the robust tables drove the MIP objective,
        #: otherwise the precise point-degradation label
        #: ("no_quantile_stream" | "partial_coverage:n/m" |
        #: "non_monotonic:<sid>" | "invalid_quantiles:<sid>" |
        #: "robust_routing_disabled").
        self.last_objective_reason: str | None = None
        logger.info(
            "corridor_router.init", weights=self._weights, robust=self._robust
        )

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
        # Optional quantile stream rides a SEPARATE structure: the
        # ForecastProvider payload contract stays exactly point-shaped
        # ({segment: {horizon: {speed_kmh, congestion_idx}}}).
        quantile_stream: dict[int, SegmentQuantiles] = {}

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
                    quantiles = _segment_quantiles_from_row(sid, 5, preds[5])
                    if quantiles is not None:
                        quantile_stream[sid] = quantiles

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

                # Directed edges: corridor.road_graph, the graph the KSP
                # alternatives below route on (012: every edge is stored
                # forwards, reverse_cost = -1).  The raw segment_adjacency
                # table still lists the physical movements the view
                # excludes — e.g. Station Road 1014 -> Gandhi Road 1012 at
                # J15, the END of one-way Gandhi Road — and the MIP planned
                # on it drove the convoy up one-way roads the wrong way.
                adj_rows = await conn.fetch("""
                    SELECT source AS from_segment_id, target AS to_segment_id
                    FROM corridor.road_graph
                """)
                edges = [(r["from_segment_id"], r["to_segment_id"]) for r in adj_rows]

        # Robust (P90) objective: compute quantile-aware cost tables from
        # the optional stream.  Absent/disabled/non-monotonic streams leave
        # the MIP call byte-identical to today's point path; the labelled
        # reason rides this attribute + the log, never the forecast payload.
        robust: RobustCostResult | None = None
        robust_time_costs = None
        robust_disruption_costs = None
        robust_weights = None
        if self._robust.get("enabled", True):
            robust = robust_edge_costs(
                segment_attrs=segment_attrs,
                point_speeds=predicted_speeds,
                point_congestion=predicted_congestion,
                quantiles=quantile_stream or None,
                quantile_level=float(self._robust["quantile"]),
            )
            if robust.source == "quantile":
                robust_time_costs = robust.time_q_scaled
                robust_disruption_costs = robust.disruption_q_scaled
                robust_weights = {
                    "time": float(self._robust["time_weight"]),
                    "disruption": float(self._robust["disruption_weight"]),
                }
                self.last_objective_reason = robust.reason or "quantile"
            else:
                self.last_objective_reason = robust.reason or "no_quantile_stream"
        else:
            self.last_objective_reason = "robust_routing_disabled"
        logger.info(
            "corridor_router.robust_objective",
            source=robust.source if robust is not None else "point",
            reason=self.last_objective_reason,
            quantile_level=float(self._robust["quantile"]),
        )

        # 1. MIP optimal route
        mip_result = self._mip_solver.solve(
            edges=edges,
            segment_attrs=segment_attrs,
            origin=origin_segment,
            destination=destination_segment,
            predicted_speeds=predicted_speeds,
            predicted_congestion=predicted_congestion,
            avoid_segments=avoid_set,
            robust_time_costs=robust_time_costs,
            robust_disruption_costs=robust_disruption_costs,
            robust_weights=robust_weights,
        )

        if mip_result:
            candidates.append(self._make_candidate(
                mip_result.segment_ids,
                segment_attrs,
                predicted_speeds,
                predicted_congestion,
                source="mip",
                robust=robust,
            ))

        # 2. K-shortest path alternatives via pgRouting (k = max_candidates:
        #    KSP #1 usually repeats the MIP optimum and is de-duplicated
        #    below, so this still leaves max_candidates distinct routes)
        if self._graph_db:
            k_paths = await self._graph_db.k_shortest_paths(
                origin_segment, destination_segment, k=max_candidates
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
                        robust=robust,
                    ))

        # De-duplicate identical paths (the MIP optimum is usually also KSP
        # #1): the first occurrence wins — MIP before KSP, KSP in rank
        # order — so a "choice" of N candidates is N distinct routes.
        unique: list[RouteCandidate] = []
        seen_paths: set[tuple[int, ...]] = set()
        for candidate in candidates:
            path = tuple(candidate.segment_ids)
            if path in seen_paths:
                continue
            seen_paths.add(path)
            unique.append(candidate)
        dropped = len(candidates) - len(unique)
        candidates = unique

        # Score and sort
        candidates.sort(key=lambda c: c.composite_score)

        logger.info(
            "corridor_router.find_routes",
            origin=origin_segment,
            dest=destination_segment,
            candidates=len(candidates),
            duplicates_dropped=dropped,
        )
        return candidates[:max_candidates]

    def _make_candidate(
        self,
        segment_ids: list[int],
        segment_attrs: dict[int, dict],
        predicted_speeds: dict[int, float],
        predicted_congestion: dict[int, float],
        source: str,
        robust: RobustCostResult | None = None,
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

        # Robust (P90) overlays: per-segment q costs from the quantile
        # stream; segments outside the robust result (KSP paths reaching
        # beyond the loaded attrs) fall back to the same point defaults
        # the estimate above uses (500 m @ 40 km/h, secondary volume).
        estimated_time_p90_sec: int | None = None
        disruption_p90: float | None = None
        if robust is not None and robust.source == "quantile":
            t90 = 0.0
            d90 = 0.0
            for sid in segment_ids:
                cost = robust.per_segment.get(sid)
                if cost is not None:
                    t90 += cost.time_q_sec
                    d90 += cost.disruption_q
                    continue
                length = segment_attrs.get(sid, {}).get("length_m", 500.0)
                t = length / max(40.0 * 1000 / 3600, 0.1)
                t90 += t
                d90 += 0.3 * 2.0 * t  # default congestion × secondary volume proxy
            estimated_time_p90_sec = int(t90)
            disruption_p90 = round(d90, 1)

        return RouteCandidate(
            route_id=f"{source}_{uuid.uuid4().hex[:8]}",
            segment_ids=segment_ids,
            total_distance_m=round(total_dist, 1),
            estimated_time_sec=int(total_time),
            disruption_score=round(disruption, 1),
            security_score=round(avg_security, 1),
            composite_score=round(composite, 3),
            estimated_time_p90_sec=estimated_time_p90_sec,
            disruption_p90=disruption_p90,
        )
