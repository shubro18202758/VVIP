"""Builds segment adjacency and computes turn costs."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import structlog

from corridor_importer.schema_mapper import MappedSegment

logger = structlog.get_logger()

# Turn cost estimates in seconds
TURN_COSTS: dict[str, float] = {
    "straight": 0.0,
    "right": 5.0,
    "left": 10.0,
    "u_turn": 20.0,
    "roundabout": 15.0,
}


@dataclass
class AdjacencyEdge:
    from_segment_id: int  # osm_way_id of origin segment
    to_segment_id: int    # osm_way_id of destination segment
    via_node: int         # junction node connecting them
    turn_cost_sec: float


class TopologyBuilder:
    """Extracts segment adjacency from the road network."""

    def build_adjacency(self, segments: list[MappedSegment]) -> list[AdjacencyEdge]:
        """Build adjacency list from mapped segments.

        Two segments are adjacent if one's to_node equals the other's from_node.
        """
        # Index: node → list of segments starting from that node
        starts_at: dict[int, list[MappedSegment]] = defaultdict(list)
        for seg in segments:
            starts_at[seg.from_node].append(seg)
            if not seg.oneway:
                # Bidirectional: also reachable from to_node
                starts_at[seg.to_node].append(seg)

        adjacency = []
        for seg in segments:
            # Segments reachable from this segment's to_node
            for neighbor in starts_at.get(seg.to_node, []):
                if neighbor.osm_way_id == seg.osm_way_id:
                    continue  # skip self-loops
                cost = self._estimate_turn_cost(seg, neighbor)
                adjacency.append(AdjacencyEdge(
                    from_segment_id=seg.osm_way_id,
                    to_segment_id=neighbor.osm_way_id,
                    via_node=seg.to_node,
                    turn_cost_sec=cost,
                ))

        return adjacency

    @staticmethod
    def _estimate_turn_cost(from_seg: MappedSegment, to_seg: MappedSegment) -> float:
        """Estimate turn cost based on road class changes.

        Full geometry-based angle calculation deferred to future phase.
        """
        # U-turn heuristic: going back the way we came
        if from_seg.from_node == to_seg.to_node:
            return TURN_COSTS["u_turn"]

        # Same road class = likely straight through
        if from_seg.road_class == to_seg.road_class:
            return TURN_COSTS["straight"]

        # Different road class = likely a turn
        return TURN_COSTS["right"]
