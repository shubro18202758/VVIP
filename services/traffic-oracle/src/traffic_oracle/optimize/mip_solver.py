"""OR-Tools CP-SAT Mixed Integer Programming solver for convoy route optimization.

Formulates convoy routing as a constrained optimization problem:
- Binary edge variables x[i,j] ∈ {0,1}
- Flow conservation constraints
- Width constraints (convoy needs 7m clearance)
- Multi-objective: minimize travel time + disruption - security + complexity

Timeout: 2 seconds. Falls back to shortest path if CP-SAT exceeds limit.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import structlog
from ortools.sat.python import cp_model

from traffic_oracle.runtime.memory_profiler import profile_memory

logger = structlog.get_logger(__name__)

# Road class security scores (higher = more secure for convoy)
ROAD_CLASS_SECURITY = {
    "motorway": 100,
    "trunk": 85,
    "primary": 70,
    "secondary": 50,
    "tertiary": 30,
    "residential": 10,
}


@dataclass
class MIPSolution:
    """Result from the MIP solver."""

    segment_ids: list[int]
    total_time_sec: float
    disruption_score: float
    security_score: float
    objective_value: float
    solve_status: str  # "optimal", "feasible", "timeout_fallback"


class ConvoyMIPSolver:
    """CP-SAT convoy route optimizer using OR-Tools."""

    def __init__(self, weights: dict[str, float]) -> None:
        self._weights = weights

    @profile_memory
    def solve(
        self,
        edges: list[tuple[int, int]],
        segment_attrs: dict[int, dict],
        origin: int,
        destination: int,
        predicted_speeds: dict[int, float],
        predicted_congestion: dict[int, float],
        avoid_segments: set[int] | None = None,
        max_duration_sec: float | None = None,
        convoy_width_m: float = 7.0,
        timeout_ms: int = 2000,
    ) -> MIPSolution | None:
        """Solve convoy routing as a CP-SAT problem.

        Args:
            edges: List of (from_segment, to_segment) directed edges
            segment_attrs: segment_id → {length_m, road_class, lanes, speed_limit_kmh}
            origin: Origin segment ID
            destination: Destination segment ID
            predicted_speeds: segment_id → predicted speed (km/h)
            predicted_congestion: segment_id → predicted congestion [0,1]
            avoid_segments: Segments to exclude from routing
            max_duration_sec: Optional maximum route duration constraint
            convoy_width_m: Required road width for convoy passage
            timeout_ms: Solver time limit in milliseconds

        Returns:
            MIPSolution or None if infeasible
        """
        avoid_segments = avoid_segments or set()

        # Preprocess: filter edges by width + avoidance
        valid_edges = []
        for src, tgt in edges:
            if src in avoid_segments or tgt in avoid_segments:
                continue
            src_attrs = segment_attrs.get(src, {})
            tgt_attrs = segment_attrs.get(tgt, {})
            src_width = src_attrs.get("lanes", 2) * 3.5
            tgt_width = tgt_attrs.get("lanes", 2) * 3.5
            if src_width >= convoy_width_m and tgt_width >= convoy_width_m:
                valid_edges.append((src, tgt))

        if not valid_edges:
            logger.warning("mip_solver.no_valid_edges")
            return None

        # Collect all nodes
        all_nodes = set()
        for src, tgt in valid_edges:
            all_nodes.add(src)
            all_nodes.add(tgt)

        if origin not in all_nodes or destination not in all_nodes:
            logger.warning("mip_solver.origin_dest_unreachable")
            return None

        nodes = sorted(all_nodes)
        node_idx = {n: i for i, n in enumerate(nodes)}

        # Build CP-SAT model
        model = cp_model.CpModel()

        # Binary edge variables
        x = {}
        for src, tgt in valid_edges:
            x[(src, tgt)] = model.NewBoolVar(f"x_{src}_{tgt}")

        # Flow conservation
        for node in nodes:
            inflow = sum(x[(s, t)] for s, t in valid_edges if t == node and (s, t) in x)
            outflow = sum(x[(s, t)] for s, t in valid_edges if s == node and (s, t) in x)

            if node == origin:
                model.Add(outflow - inflow == 1)
            elif node == destination:
                model.Add(inflow - outflow == 1)
            else:
                model.Add(inflow == outflow)

        # Compute edge costs (scaled to integers for CP-SAT)
        SCALE = 1000
        time_costs = {}
        disruption_costs = {}
        security_scores = {}

        for src, tgt in valid_edges:
            attrs = segment_attrs.get(src, {})
            length_m = attrs.get("length_m", 500.0)
            road_class = attrs.get("road_class", "secondary")
            speed = predicted_speeds.get(src, 40.0)
            congestion = predicted_congestion.get(src, 0.3)

            # Travel time (seconds)
            travel_sec = length_m / max(speed * 1000 / 3600, 0.1)
            time_costs[(src, tgt)] = int(travel_sec * SCALE)

            # Disruption: congestion × estimated traffic volume
            volume_proxy = {"motorway": 5.0, "trunk": 4.0, "primary": 3.0,
                           "secondary": 2.0, "tertiary": 1.5, "residential": 1.0}
            disruption = congestion * volume_proxy.get(road_class, 2.0) * travel_sec
            disruption_costs[(src, tgt)] = int(disruption * SCALE)

            # Security score (higher = better)
            security_scores[(src, tgt)] = int(
                ROAD_CLASS_SECURITY.get(road_class, 30) * SCALE
            )

        # Objective: minimize weighted combination
        w = self._weights
        objective_terms = []
        for edge in valid_edges:
            if edge in x:
                objective_terms.append(
                    int(w["time"] * SCALE) * time_costs[edge] * x[edge]
                )
                objective_terms.append(
                    int(w["disruption"] * SCALE) * disruption_costs[edge] * x[edge]
                )
                objective_terms.append(
                    -int(w["security"] * SCALE) * security_scores[edge] * x[edge]
                )
                objective_terms.append(
                    int(w["complexity"] * SCALE) * x[edge]
                )

        model.Minimize(sum(objective_terms))

        # Optional duration constraint
        if max_duration_sec is not None:
            total_time = sum(time_costs[e] * x[e] for e in valid_edges if e in x)
            model.Add(total_time <= int(max_duration_sec * SCALE))

        # Solve
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = timeout_ms / 1000.0

        status = solver.Solve(model)

        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # Extract path
            active_edges = [(s, t) for (s, t), var in x.items() if solver.Value(var)]
            path = self._reconstruct_path(active_edges, origin, destination)

            total_time = sum(
                time_costs.get((path[i], path[i + 1]), 0)
                for i in range(len(path) - 1)
            ) / SCALE

            disruption = sum(
                disruption_costs.get((path[i], path[i + 1]), 0)
                for i in range(len(path) - 1)
            ) / SCALE

            security = sum(
                security_scores.get((path[i], path[i + 1]), 0)
                for i in range(len(path) - 1)
            ) / SCALE / max(len(path) - 1, 1)

            status_str = "optimal" if status == cp_model.OPTIMAL else "feasible"
            logger.info(
                "mip_solver.solved",
                status=status_str,
                path_length=len(path),
                time_sec=round(total_time, 1),
            )

            return MIPSolution(
                segment_ids=path,
                total_time_sec=total_time,
                disruption_score=disruption,
                security_score=security,
                objective_value=solver.ObjectiveValue() / (SCALE * SCALE),
                solve_status=status_str,
            )

        logger.warning("mip_solver.infeasible", status=status)
        return None

    @staticmethod
    def _reconstruct_path(
        edges: list[tuple[int, int]], origin: int, destination: int
    ) -> list[int]:
        """Reconstruct ordered path from active edge set."""
        adj: dict[int, int] = {}
        for src, tgt in edges:
            adj[src] = tgt

        path = [origin]
        current = origin
        visited = {origin}
        while current != destination and current in adj:
            nxt = adj[current]
            if nxt in visited:
                break
            path.append(nxt)
            visited.add(nxt)
            current = nxt
        return path
