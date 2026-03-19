"""Constructive heuristics for public traffic diversion planning.

Uses igraph (C backend, ~10x faster than networkx for Dijkstra) to compute
alternative routes for public traffic displaced by convoy road closures.
Calculates activation/deactivation times and queue length estimates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog

from traffic_oracle.data.graph_queries import CorridorGraphDB
from traffic_oracle.runtime.memory_profiler import profile_memory

logger = structlog.get_logger(__name__)


@dataclass
class DiversionRoute:
    """A computed diversion for a specific blocked segment."""

    blocked_segment_id: int
    alt_segment_ids: list[int]
    activate_at: datetime
    deactivate_at: datetime
    estimated_queue_m: float
    dissipation_time_sec: int
    detour_distance_m: float


class DiversionHeuristic:
    """Greedy diversion path computation using igraph."""

    def __init__(self, graph_db: CorridorGraphDB) -> None:
        self._graph_db = graph_db
        self._graph = None  # igraph.Graph, lazy loaded

    @profile_memory
    async def build_graph(self) -> None:
        """Load corridor edges into igraph for fast Dijkstra."""
        import igraph as ig

        # Fetch all edges from the corridor graph
        pool = self._graph_db._pool
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT from_segment_id, to_segment_id, travel_cost_sec
                FROM corridor.segment_adjacency
            """)

        edge_list = []
        weights = []
        vertices = set()
        for r in rows:
            src, tgt = r["from_segment_id"], r["to_segment_id"]
            vertices.add(src)
            vertices.add(tgt)
            edge_list.append((src, tgt))
            weights.append(r["travel_cost_sec"] or 10.0)

        vertex_list = sorted(vertices)
        v_map = {v: i for i, v in enumerate(vertex_list)}

        self._graph = ig.Graph(
            n=len(vertex_list),
            edges=[(v_map[s], v_map[t]) for s, t in edge_list],
            directed=True,
        )
        self._graph.vs["segment_id"] = vertex_list
        self._graph.es["weight"] = weights
        self._v_map = v_map
        self._v_list = vertex_list

        logger.info(
            "diversion_heuristic.graph_built",
            vertices=len(vertex_list),
            edges=len(edge_list),
        )

    @profile_memory
    async def compute_diversions(
        self,
        convoy_route: list[int],
        predicted_speeds: dict[int, float],
        segment_lengths: dict[int, float],
        departure_time: datetime | None = None,
        advance_closure_sec: int = 120,
    ) -> list[DiversionRoute]:
        """Compute diversion routes for each blocked segment along the convoy route.

        Args:
            convoy_route: Ordered segment IDs of convoy path
            predicted_speeds: segment_id → predicted speed (km/h)
            segment_lengths: segment_id → length in meters
            departure_time: Convoy departure time (default: now)
            advance_closure_sec: Seconds before convoy arrival to activate closure

        Returns:
            List of DiversionRoute, one per blocked segment
        """
        if self._graph is None:
            await self.build_graph()

        if departure_time is None:
            departure_time = datetime.now(timezone.utc)

        blocked_set = set(convoy_route)
        diversions = []

        # Compute cumulative convoy arrival time at each segment
        cumulative_sec = 0.0
        segment_etas: dict[int, float] = {}
        for sid in convoy_route:
            segment_etas[sid] = cumulative_sec
            length = segment_lengths.get(sid, 500.0)
            speed = predicted_speeds.get(sid, 40.0)
            travel_sec = length / max(speed * 1000 / 3600, 0.1)
            cumulative_sec += travel_sec

        for sid in convoy_route:
            if sid not in self._v_map:
                continue

            eta_sec = segment_etas[sid]
            activate = departure_time + timedelta(
                seconds=max(0, eta_sec - advance_closure_sec)
            )
            length = segment_lengths.get(sid, 500.0)
            speed = predicted_speeds.get(sid, 40.0)
            traverse_sec = length / max(speed * 1000 / 3600, 0.1)
            deactivate = departure_time + timedelta(
                seconds=eta_sec + traverse_sec + 30  # 30s buffer
            )

            # Find alternative path avoiding blocked segments
            alt_path = self._find_detour(sid, blocked_set)

            # Queue estimate: inflow_rate × closure_duration × spacing
            closure_duration = (deactivate - activate).total_seconds()
            congestion_factor = 1.0  # simplified
            inflow_rate = 0.3  # vehicles/second default
            avg_spacing = 8.0  # meters per queued vehicle
            queue_m = inflow_rate * closure_duration * avg_spacing * congestion_factor

            # Queue dissipation: ~2 seconds per queued vehicle
            dissipation_sec = int(queue_m / avg_spacing * 2)

            detour_dist = sum(
                segment_lengths.get(s, 500.0) for s in alt_path
            )

            diversions.append(DiversionRoute(
                blocked_segment_id=sid,
                alt_segment_ids=alt_path,
                activate_at=activate,
                deactivate_at=deactivate,
                estimated_queue_m=round(queue_m, 1),
                dissipation_time_sec=dissipation_sec,
                detour_distance_m=round(detour_dist, 1),
            ))

        diversions.sort(key=lambda d: d.activate_at)

        logger.info(
            "diversion_heuristic.computed",
            diversions=len(diversions),
            route_segments=len(convoy_route),
        )
        return diversions

    def _find_detour(
        self, blocked_segment: int, blocked_set: set[int]
    ) -> list[int]:
        """Find shortest detour around a blocked segment using igraph."""
        if self._graph is None:
            return []

        g = self._graph
        v_map = self._v_map

        # Temporarily increase weights on blocked segments
        blocked_indices = {v_map[s] for s in blocked_set if s in v_map}
        original_weights = list(g.es["weight"])

        for e in g.es:
            if e.source in blocked_indices or e.target in blocked_indices:
                e["weight"] = 1e9  # effective block

        # Find neighbors of blocked segment as entry/exit points
        idx = v_map.get(blocked_segment)
        if idx is None:
            g.es["weight"] = original_weights
            return []

        predecessors = [e.source for e in g.es if e.target == idx]
        successors = [e.target for e in g.es if e.source == idx]

        best_path = []
        best_cost = float("inf")

        for entry in predecessors[:3]:  # limit search
            for exit_node in successors[:3]:
                if entry == exit_node:
                    continue
                try:
                    path = g.get_shortest_paths(
                        entry, exit_node, weights="weight", output="vpath"
                    )[0]
                    if path:
                        cost = sum(
                            g.es[g.get_eid(path[i], path[i + 1])]["weight"]
                            for i in range(len(path) - 1)
                        )
                        if cost < best_cost:
                            best_cost = cost
                            best_path = [self._v_list[v] for v in path]
                except Exception:
                    continue

        # Restore weights
        g.es["weight"] = original_weights
        return best_path
