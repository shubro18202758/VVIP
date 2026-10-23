"""Route Planner Agent — Gemma 4 12B tactical tier with brain-side scoring.

This agent reasons over route candidates produced by the CorridorRouter
(traffic-oracle OR-Tools MIP + pgRouting KSP). Deterministic work comes
first; the model only narrates:

    1. SNAP     Both endpoints resolve to road segments by true
                point-to-polyline distance over the oracle's bbox rows
                (±0.002°, widened to ±0.004° when that box is empty or its
                nearest road lies beyond the box's half-width: the oracle
                matches ENVELOPES, so a small box can return only a long
                road whose bounding box covers the point while a nearer
                road just outside the box is missed). The oracle returns
                those rows ORDER BY segment_id, so "first row" used to mean
                "lowest id", not "nearest road" (three POIs snapped to 1001).
                Segments within SNAP_TIE_TOLERANCE_M of the nearest are
                equivalent snaps (a junction point touches several); the
                origin/destination PAIR is chosen by the network's own
                shortest-path cost (query_shortest_path), and a pair never
                uses one segment for both ends. A request whose ends can only
                resolve to one segment is rejected with that reason.
    2. ROUTE    The best pairs go through find_convoy_routes until one returns
                candidates — an isolated or avoided snap falls through to the
                next pair instead of failing the plan.
    3. RANK     Identical segment lists are de-duplicated (MIP and KSP often
                return the same path); 0..1 scores are derived from the
                solver metrics and the adaptive ``scoring_weights`` are
                applied brain-side (contract C4 — no traffic-oracle
                optimize/ edit needed). Every candidate is audited against
                the protection class's lane floor; compliant candidates rank
                first, and non-compliant ones are rejected while a compliant
                one exists.
    4. NARRATE  Gemma explains the ranking. Its narration is merged back onto
                the solver candidates by route_id — ETA, distance and
                security always come from the solver, never from the model,
                and the deterministic ranking is the floor when the tactical
                tier is unavailable. The reply is bounded by its schema
                (route_id + a ≤160-character reason per route, ≤3 alternates,
                ≤4 rejections — the solver returns at most 5 candidates —
                and ≤600 characters overall; no echoed segment lists or
                scores, no extra keys) and its budget is that bounded
                maximum, so the first reply is never cut at the cap.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from typing import Any

import structlog

from convoy_brain.agents.base import (
    AgentSpec,
    grounded_text,
    structured_agent_call,
    whole_sentences,
)
from convoy_brain.agents.security_liaison import (
    fetch_segment_facts,
    hard_violation_summary,
    lane_audit,
    min_lanes_for,
)
from convoy_brain.config import AdaptiveParams
from convoy_brain.llm.heuristics import (
    _normalize_weights,
    dedupe_route_candidates,
    forecast_rows,
    heuristic_route_ranking,
)
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.mcp.server import ToolExecutor

logger = structlog.get_logger(__name__)

ROUTE_PLANNER_SYSTEM_PROMPT = """\
You are the Route Planner agent for the Corridor Event Orchestration Platform.
Your role is to analyze route/spine candidates and recommend the optimal
corridor for the event — a protected movement, procession, race, ceremonial,
egress surge, evacuation run or work zone.

You have access to the following tools:
- find_convoy_routes: Multi-objective route search (OR-Tools MIP solver)
- predict_traffic_flow: Forecast speed/congestion at T+5/10/15/30 min
- predict_eta: Estimate travel time along the corridor
- query_segment_details: Road attributes (width, class, speed limit, lanes)
- plan_diversions: Per-segment closure plans with timing and queue estimates
- evaluate_scenarios: Compare routes by total disruption

When recommending routes, always consider:
1. PROTECTION: Wider roads with fewer chokepoints are preferred for higher protection classes
2. CROWD: For public events, prefer spines with hardened perimeters and staged ingress capacity
3. DISRUPTION: Minimize the number of affected public traffic segments
4. TIMING: Consider time-of-day traffic patterns and event phase windows
5. BACKUP: Always recommend at least one alternate route

The candidates arrive pre-ranked by a weighted score (time / disruption /
security / complexity), compliant candidates first. Each carries its
deterministic lane audit (route_tier, lane_violations): a candidate's
lane_violations are segments OF THAT candidate below the class's lane
floor — name them, never place them elsewhere. Respect that
ordering; explain the trade-offs in plain language with the numbers given
— name the roads, the lane floor, the forecast congestion and the ETA;
never write generic prose. Use only route IDs that appear in the
candidates, and do NOT repeat segment lists or scores (the solver keeps
them).

Respond with ONE compact single-line JSON object — no line breaks, no
indentation. Each reason is at most 25 words (160 characters) and names
the figures that decide it (ETA minutes, km, the roads below the lane
floor, forecast congestion); overall_reasoning is at most 3 sentences
(600 characters) with figures; at most 3 alternates and 4 rejections:
{"primary_route":{"route_id":str,"reason":str},"alternate_routes":[{"route_id":str,"reason":str}],"rejected_routes":[{"route_id":str,"reason":str}],"overall_reasoning":str,"confidence":"high|medium|low"}
"""

SPEC = AgentSpec(
    name="route_planner",
    system_prompt=ROUTE_PLANNER_SYSTEM_PROMPT,
    schema_name="route_planner",
)

#: Snap search boxes (degrees): the first query, and the widened one read
#: when the first box holds no segment at all — or when the nearest road it
#: holds lies beyond the box's half-width (:func:`_box_half_width_m`, about
#: 205 m at Ahmedabad's latitude): only a road within that distance is sure
#: to touch the box, so a nearer road may sit just outside it (the old
#: Kankaria Lake point's small box held only SP Ring Road 1022, 6.8 km off
#: by geometry, while Kankaria Road 1038 lay 272 m away). The nearest road
#: of both boxes wins; a nearest-neighbour query would be the general fix.
SNAP_BOX_DEG = 0.002
SNAP_BOX_WIDE_DEG = 0.004
#: Segments this much farther than the nearest one are still "at the point"
#: (a junction POI touches every segment meeting there at ~0 m).
SNAP_TIE_TOLERANCE_M = 30.0
#: Origin and destination closer than this are the same place.
MIN_OD_SEPARATION_M = 50.0
#: Shortest-path probes per plan (cheap pgRouting calls, run concurrently).
MAX_PATH_PROBES = 12
#: find_convoy_routes attempts over the best-ranked snap pairs.
MAX_ROUTE_ATTEMPTS = 3
#: Assumed access speed for the snap distance (the walk from the point onto
#: the road), and the fallback traversal speed when a limit is unknown.
_ACCESS_SPEED_MPS = 30 / 3.6
_FALLBACK_SPEED_KMH = 40.0

_M_PER_DEG_LAT = 110_574.0


@dataclass
class SnapCandidate:
    """One road segment a trip endpoint can resolve to."""

    segment_id: int
    distance_m: float
    length_m: float
    road_name: str | None
    road_class: str | None
    lanes: int | None
    speed_limit_kmh: float | None
    far_end: tuple[float, float] | None  # endpoint away from the snap point

    def as_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "road_name": self.road_name,
            "distance_m": round(self.distance_m, 1),
        }


class RoutePlannerAgent:
    """Tactical agent specialized in convoy route analysis and recommendation."""

    def __init__(
        self,
        bridge: LlamaBridge,
        executor: ToolExecutor,
        adaptive: AdaptiveParams | None = None,
        *,
        segment_cache: dict[int, dict] | None = None,
    ) -> None:
        self._bridge = bridge
        self._executor = executor
        self._adaptive = adaptive
        self._segment_cache = segment_cache if segment_cache is not None else {}
        logger.info("route_planner_agent.init")

    async def analyze_routes(
        self,
        movement_id: str,
        origin: tuple[float, float],
        destination: tuple[float, float],
        vvip_class: str,
        avoid_segments: list[int] | None = None,
    ) -> dict:
        """Analyze and recommend convoy routes for a planned movement."""
        logger.info(
            "route_planner_agent.analyze",
            movement_id=movement_id,
            vvip_class=vvip_class,
            avoid=len(avoid_segments or []),
        )
        avoid = sorted({int(s) for s in (avoid_segments or []) if s is not None})

        # 1. Resolve origin/destination to the nearest road segments
        snap = await self.resolve_endpoints(origin, destination, avoid)
        if snap.get("error"):
            return _no_route(snap["error"], snap=snap)

        # 2. Route over the best snap pairs (fall through isolated/avoided snaps)
        candidates: list[dict] = []
        attempts: list[dict] = []
        used_pair: tuple[int, int] | None = None
        for origin_id, dest_id in snap["pairs"][:MAX_ROUTE_ATTEMPTS]:
            try:
                routes_result = await self._executor.execute("find_convoy_routes", {
                    "origin_segment": origin_id,
                    "destination_segment": dest_id,
                    "max_candidates": 5,
                    "avoid_segments": avoid,
                })
            except Exception as exc:
                attempts.append({
                    "origin_segment": origin_id, "destination_segment": dest_id,
                    "routes": 0, "error": str(exc)[:200],
                })
                continue
            rows = (
                routes_result.get("routes", [])
                if isinstance(routes_result, dict) else
                routes_result if isinstance(routes_result, list) else []
            )
            rows = [r for r in rows if isinstance(r, dict) and r.get("segment_ids")]
            attempts.append({
                "origin_segment": origin_id, "destination_segment": dest_id, "routes": len(rows),
            })
            if rows:
                candidates, used_pair = rows, (origin_id, dest_id)
                break
        snap["route_attempts"] = attempts
        if not candidates:
            tried = ", ".join(
                f"{a['origin_segment']}→{a['destination_segment']}" for a in attempts
            ) or "none"
            reason = (
                "The MIP/KSP router returned no route candidates for the snapped "
                f"segment pairs ({tried})"
                + (f" while avoiding segments {avoid}" if avoid else "")
                + "."
            )
            disconnected = snap.get("network_disconnected")
            if disconnected:
                reason += (
                    " The corridor road graph has no path at all between origin "
                    f"segment(s) {disconnected['origin']} and destination segment(s) "
                    f"{disconnected['destination']}: one end has no links in the "
                    "network adjacency data, so no corridor can join them."
                )
            return _no_route(reason, snap=snap)
        snap["origin_segment"], snap["destination_segment"] = used_pair

        # 3. De-duplicate, derive scores, rank with the adaptive weights (C4)
        weights = (self._adaptive.scoring_weights if self._adaptive else None) or {}
        unique = dedupe_route_candidates(candidates)
        ranked = heuristic_route_ranking(unique, weights)

        # 4. Deterministic lane audit per candidate; compliant first
        all_ids = [s for route in ranked for s in route.get("segment_ids", [])]
        facts = await fetch_segment_facts(self._executor, all_ids, self._segment_cache)
        shaped = [_shape_candidate(route, facts, vvip_class) for route in ranked]
        shaped.sort(key=lambda r: 0 if r["route_tier"] == "compliant" else 1)  # stable
        rejected: list[dict] = []
        if any(r["route_tier"] == "compliant" for r in shaped):
            kept = []
            for route in shaped:
                if route["route_tier"] == "compliant":
                    kept.append(route)
                else:
                    rejected.append({
                        "route_id": route["route_id"],
                        "reason": "protocol: " + hard_violation_summary(
                            route["lane_violations"], vvip_class
                        ),
                    })
            shaped = kept

        # 5. Traffic forecast for the narration (failure is non-fatal)
        unique_seg_ids = list(dict.fromkeys(s for r in shaped for s in r["segment_ids"]))[:200]
        try:
            forecast = await self._executor.execute("predict_traffic_flow", {
                "segment_ids": unique_seg_ids,
                "horizons_min": [5, 15, 30],
            })
        except Exception as exc:
            logger.warning("route_planner.forecast_failed", error=str(exc))
            forecast = {"error": str(exc)[:200]}
        _attach_forecast(shaped, forecast)

        # 6. Gemma narrates; the deterministic ranking is the floor
        norm_weights = _normalize_weights(weights)

        def heuristic() -> dict:
            return {
                "primary_route": None,
                "alternate_routes": [],
                "rejected_routes": [],
                "overall_reasoning": _deterministic_reasoning(
                    shaped, rejected, norm_weights, vvip_class, snap
                ),
                "confidence": "medium",
            }

        user_prompt = json.dumps({
            "task": "analyze_and_rank_routes",
            "movement_id": movement_id,
            "vvip_class": vvip_class,
            "min_lanes_required": min_lanes_for(vvip_class),
            "origin_snap": snap["origin"],
            "destination_snap": snap["destination"],
            "scoring_weights": norm_weights,
            "pre_ranked_candidates": [_prompt_view(r) for r in shaped],
            "protocol_rejected": rejected,
        }, default=str)

        narration, tier = await structured_agent_call(
            self._bridge,
            SPEC,
            user_prompt,
            heuristic,
            repair_hint="keys: primary_route, alternate_routes[], rejected_routes[], overall_reasoning, confidence",
        )
        result = _merge_narration(narration, tier, shaped, rejected)
        if not result["overall_reasoning"] or not _DIGIT_RE.search(result["overall_reasoning"]):
            # No narration, or a generic one without a single figure ("the
            # primary has the highest score…"): the operator reads the
            # deterministic ranking instead — the per-route reasons the model
            # wrote with figures stay on their routes (figure-free ones were
            # already replaced by each route's own line in the merge).
            if result["overall_reasoning"]:
                result["narration_replaced"] = (
                    "figure-free narration replaced by the deterministic ranking"
                )
            result["overall_reasoning"] = _deterministic_reasoning(
                shaped, rejected, norm_weights, vvip_class, snap
            )
        result["_tier"] = tier
        fallback_reason = narration.get("_fallback_reason") if isinstance(narration, dict) else None
        if tier == "heuristic" and fallback_reason:
            result["_fallback_reason"] = fallback_reason
        result["_ranked_candidates"] = shaped
        result["snap"] = snap
        return result

    # -- snapping ------------------------------------------------------------

    async def resolve_endpoints(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        avoid: list[int] | None = None,
    ) -> dict:
        """Resolve both endpoints to ranked (origin_segment, destination_segment) pairs.

        Returns ``{pairs, origin, destination, error}``; ``error`` is a plain
        operator-facing reason when no usable pair exists.
        """
        separation = _haversine_m(origin, destination)
        origin_rows, dest_rows = await asyncio.gather(
            self._snap_rows(origin), self._snap_rows(destination)
        )
        origin_cands = _snap_candidates(origin, origin_rows, avoid or [])
        dest_cands = _snap_candidates(destination, dest_rows, avoid or [])
        snap: dict[str, Any] = {
            "origin": origin_cands[0].as_dict() if origin_cands else None,
            "destination": dest_cands[0].as_dict() if dest_cands else None,
            "origin_candidates": [c.segment_id for c in origin_cands],
            "destination_candidates": [c.segment_id for c in dest_cands],
            "pairs": [],
            "error": None,
        }
        if not origin_cands or not dest_cands:
            missing = "origin" if not origin_cands else "destination"
            snap["error"] = (
                f"Could not resolve the {missing} to a road segment: no corridor "
                f"segment within ±{SNAP_BOX_WIDE_DEG}° of the point."
            )
            return snap
        if separation < MIN_OD_SEPARATION_M:
            snap["error"] = (
                f"Origin and destination are only {separation:.0f} m apart — they "
                "resolve to the same place; choose two distinct endpoints."
            )
            return snap

        pairs = [
            (o, d) for o in origin_cands for d in dest_cands if o.segment_id != d.segment_id
        ]
        if not pairs:
            only = origin_cands[0]
            snap["error"] = (
                f"Origin and destination both resolve only to segment {only.segment_id}"
                f" ({only.road_name or 'unnamed road'}) — a corridor needs two distinct "
                "road segments; move one endpoint onto a different road."
            )
            return snap

        pairs.sort(key=lambda p: (
            _pair_prior(p, origin, destination), p[0].segment_id, p[1].segment_id
        ))
        probed = pairs[:MAX_PATH_PROBES]
        costs = await asyncio.gather(*(self._path_cost(o, d) for o, d in probed))
        cost_by_pair = {
            (o.segment_id, d.segment_id): cost for (o, d), cost in zip(probed, costs)
        }
        probes_ok = any(cost is not None for cost in costs)

        def order_key(pair: tuple[SnapCandidate, SnapCandidate]) -> tuple:
            o, d = pair
            cost = cost_by_pair.get((o.segment_id, d.segment_id))
            access = (o.distance_m + d.distance_m) / _ACCESS_SPEED_MPS
            if not probes_ok or cost is None:
                network = math.inf if probes_ok else 0.0
            else:
                network = cost + access
            return (network, _pair_prior(pair, origin, destination), o.segment_id, d.segment_id)

        pairs.sort(key=order_key)
        snap["pairs"] = [(o.segment_id, d.segment_id) for o, d in pairs]
        snap["probe_costs_sec"] = {
            f"{o}->{d}": (round(c, 1) if c is not None and math.isfinite(c) else None)
            for (o, d), c in cost_by_pair.items()
        }
        if probes_ok and len(cost_by_pair) == len(pairs):
            # Every pair was probed and none connects: report both candidate
            # sets so the reason can say the network data links neither end
            # (e.g. a segment with no adjacency rows at all).
            reachable = {
                key for key, cost in cost_by_pair.items()
                if cost is not None and math.isfinite(cost)
            }
            if not reachable:
                snap["network_disconnected"] = {
                    "origin": sorted({o for o, _ in cost_by_pair}),
                    "destination": sorted({d for _, d in cost_by_pair}),
                }
        return snap

    async def _snap_rows(self, point: tuple[float, float]) -> list[dict]:
        """bbox rows around a point: the ±SNAP_BOX_DEG box, widened to
        ±SNAP_BOX_WIDE_DEG when it holds no segment, or when the nearest
        road it holds lies beyond the box's half-width (a nearer road may
        then sit just outside it — the oracle matches envelopes). The wide
        box's rows join the small box's, so :func:`_snap_candidates` takes
        the nearest of all. Rows without geometry cannot be measured: they
        never trigger the widening (the oracle's order stands)."""
        rows = await self._bbox_rows(point, SNAP_BOX_DEG)
        nearest = _nearest_row_m(point, rows) if rows else None
        if rows and (nearest is None or nearest <= _box_half_width_m(point, SNAP_BOX_DEG)):
            return rows
        wide = await self._bbox_rows(point, SNAP_BOX_WIDE_DEG)
        if not rows:
            return wide or []
        if wide:
            logger.info(
                "route_planner.snap_box_widened", point=point,
                nearest_m=round(nearest, 1) if nearest is not None else None,
                rows=len(rows), wide_rows=len(wide),
            )
        merged = {int(r["segment_id"]): r for r in rows if r.get("segment_id") is not None}
        for r in wide or []:
            sid = r.get("segment_id")
            if sid is not None:
                merged.setdefault(int(sid), r)
        return [merged[sid] for sid in sorted(merged)]

    async def _bbox_rows(self, point: tuple[float, float], pad: float) -> list[dict] | None:
        """One ±pad bbox read (rows as dicts); None when the query failed."""
        try:
            result = await self._executor.execute("query_segments_in_bbox", {
                "min_lon": point[0] - pad,
                "min_lat": point[1] - pad,
                "max_lon": point[0] + pad,
                "max_lat": point[1] + pad,
            })
        except Exception as exc:
            logger.warning("route_planner.snap_query_failed", pad=pad, error=str(exc))
            return None
        rows = result.get("segments", []) if isinstance(result, dict) else result
        rows = [r for r in rows or [] if isinstance(r, (dict, int))]
        return [r if isinstance(r, dict) else {"segment_id": r} for r in rows]

    async def _path_cost(self, origin: SnapCandidate, dest: SnapCandidate) -> float | None:
        """Network travel cost (s) of the pair: pgRouting path + destination traversal.

        ``None`` when the probe itself failed (no evidence); ``inf`` when the
        network has no path between the two segments.
        """
        try:
            result = await self._executor.execute("query_shortest_path", {
                "source_segment": origin.segment_id,
                "target_segment": dest.segment_id,
            })
        except Exception as exc:
            logger.info("route_planner.path_probe_failed", error=str(exc)[:200])
            return None
        path = result.get("path", []) if isinstance(result, dict) else []
        rows = [r for r in path if isinstance(r, dict) and (r.get("node") or 0) > 0]
        if not rows or rows[-1].get("node") != dest.segment_id:
            return math.inf
        agg = rows[-1].get("agg_cost")
        if not isinstance(agg, (int, float)):
            return math.inf
        speed = dest.speed_limit_kmh or _FALLBACK_SPEED_KMH
        return float(agg) + dest.length_m / max(speed / 3.6, 0.1)


# ---------------------------------------------------------------------------
# Snapping geometry (pure)
# ---------------------------------------------------------------------------


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6_371_000.0 * math.asin(min(1.0, math.sqrt(h)))


def _geometry_lines(geometry: Any) -> list[list[tuple[float, float]]]:
    """LineString / MultiLineString GeoJSON -> list of (lon, lat) point lists."""
    if not isinstance(geometry, dict):
        return []
    coords = geometry.get("coordinates")
    if geometry.get("type") == "LineString":
        raw_lines = [coords]
    elif geometry.get("type") == "MultiLineString":
        raw_lines = coords
    else:
        return []
    lines = []
    for raw in raw_lines or []:
        points = [
            (float(p[0]), float(p[1]))
            for p in raw or []
            if isinstance(p, (list, tuple)) and len(p) >= 2
        ]
        if points:
            lines.append(points)
    return lines


def _point_to_lines_m(point: tuple[float, float], lines: list[list[tuple[float, float]]]) -> float:
    """Exact point-to-polyline distance in a local equirectangular frame (metres)."""
    lon0, lat0 = point
    kx = 111_320.0 * math.cos(math.radians(lat0))
    best = math.inf
    for points in lines:
        xy = [((lon - lon0) * kx, (lat - lat0) * _M_PER_DEG_LAT) for lon, lat in points]
        if len(xy) == 1:
            best = min(best, math.hypot(*xy[0]))
            continue
        for (x1, y1), (x2, y2) in zip(xy, xy[1:]):
            dx, dy = x2 - x1, y2 - y1
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / seg2))
            best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    return best


def _lines_length_m(lines: list[list[tuple[float, float]]]) -> float:
    return sum(
        _haversine_m(a, b) for points in lines for a, b in zip(points, points[1:])
    )


def _row_lines(row: dict) -> list[list[tuple[float, float]]]:
    """The ROUTING line of a bbox row (C-K: ``schematic_geometry`` when the
    row is served on a display line, else ``geometry``)."""
    return _geometry_lines(row.get("schematic_geometry") or row.get("geometry"))


def _nearest_row_m(point: tuple[float, float], rows: list[dict]) -> float | None:
    """Distance (m) from the point to the nearest row's routing line; None
    when no row carries geometry (nothing can be measured)."""
    best: float | None = None
    for row in rows:
        lines = _row_lines(row)
        if lines:
            d = _point_to_lines_m(point, lines)
            best = d if best is None else min(best, d)
    return best


def _box_half_width_m(point: tuple[float, float], pad_deg: float) -> float:
    """The ±pad_deg box's inscribed half-width in metres at the point: any
    road closer than this crosses the box, so its envelope is returned."""
    kx = 111_320.0 * math.cos(math.radians(point[1]))
    return pad_deg * min(kx, _M_PER_DEG_LAT)


def _snap_candidates(
    point: tuple[float, float],
    rows: list[dict],
    avoid: list[int],
) -> list[SnapCandidate]:
    """Segments at the point: nearest by geometry, plus near-ties.

    Snapping ROUTES, so it measures the routing geometry (contract C-K): a
    row the spatial bbox serves on its OSM-traced display line carries the
    schematic line routing uses as ``schematic_geometry`` — distance, length
    (the destination traversal cost) and far end are taken from that, never
    from the drawn line, which can sit 0.5-1.6 km off the routing chord.
    Rows without geometry (legacy/fake payloads) keep the oracle order with
    an unknown distance — they are only used when no row carries geometry.
    Avoided segments are dropped whenever another candidate exists.
    """
    measured: list[SnapCandidate] = []
    unmeasured: list[SnapCandidate] = []
    for row in rows:
        sid = row.get("segment_id")
        if sid is None:
            continue
        lines = _row_lines(row)
        lanes = row.get("lanes")
        cand = SnapCandidate(
            segment_id=int(sid),
            distance_m=_point_to_lines_m(point, lines) if lines else math.inf,
            length_m=_lines_length_m(lines) if lines else 500.0,
            road_name=row.get("road_name"),
            road_class=row.get("road_class"),
            lanes=int(lanes) if isinstance(lanes, (int, float)) else None,
            speed_limit_kmh=(
                float(row["speed_limit_kmh"])
                if isinstance(row.get("speed_limit_kmh"), (int, float)) else None
            ),
            far_end=_far_end(point, lines),
        )
        (measured if lines else unmeasured).append(cand)

    if measured:
        measured.sort(key=lambda c: (c.distance_m, c.segment_id))
        nearest = measured[0].distance_m
        pool = [c for c in measured if c.distance_m <= nearest + SNAP_TIE_TOLERANCE_M]
    else:
        pool = unmeasured  # oracle order, distance unknown
    preferred = [c for c in pool if c.segment_id not in set(avoid)]
    return preferred or pool


def _far_end(
    point: tuple[float, float], lines: list[list[tuple[float, float]]]
) -> tuple[float, float] | None:
    if not lines:
        return None
    first, last = lines[0][0], lines[-1][-1]
    return first if _haversine_m(point, first) > _haversine_m(point, last) else last


def _pair_prior(
    pair: tuple[SnapCandidate, SnapCandidate],
    origin: tuple[float, float],
    destination: tuple[float, float],
) -> float:
    """Geometric prior for a snap pair (metres; lower is better).

    Snap distances plus each segment's wasted length: the part of the
    segment that does not make progress toward the other trip end (a
    segment leading away from the destination is closure without benefit).
    Orders which pairs get a shortest-path probe and breaks probe ties.
    """
    o, d = pair
    direct = _haversine_m(origin, destination)

    def waste(cand: SnapCandidate, other: tuple[float, float]) -> float:
        if cand.far_end is None or not math.isfinite(cand.distance_m):
            return 0.0
        progress = direct - _haversine_m(cand.far_end, other)
        return max(0.0, cand.length_m - progress)

    distance = sum(c.distance_m for c in pair if math.isfinite(c.distance_m))
    return distance + waste(o, destination) + waste(d, origin)


# ---------------------------------------------------------------------------
# Candidate shaping / narration merge (pure)
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _shape_candidate(route: dict, facts: dict[int, dict], vvip_class: str) -> dict:
    """Normalize one solver candidate into the plan-document route shape (C-I).

    ``security_score`` / ``disruption_score`` are rescaled from the solver's
    0-100 to 0..1 (the plan document's scale; raw values kept under
    ``solver_scores``); ``estimated_time_sec`` is always > 0; ``route_tier``
    is the deterministic lane-floor verdict.
    """
    segment_ids = [int(s) for s in route.get("segment_ids", [])]
    audit = lane_audit(facts, segment_ids, vvip_class)
    distance = _num(route.get("total_distance_m"))
    if distance is None or distance <= 0:
        lengths = [_num(facts.get(s, {}).get("length_m")) for s in segment_ids]
        distance = sum(x for x in lengths if x) or None
    eta = _num(route.get("estimated_time_sec"))
    eta_source = "solver"
    if eta is None or eta < 1:
        # Solver gave no usable ETA: distance at the fallback corridor speed.
        eta = (distance or 500.0 * max(len(segment_ids), 1)) / (_FALLBACK_SPEED_KMH / 3.6)
        eta_source = f"assumed_{_FALLBACK_SPEED_KMH:.0f}kmh"
    raw_security = _num(route.get("security_score"))
    raw_disruption = _num(route.get("disruption_score"))
    route_id = str(route.get("route_id"))
    shaped = {
        "route_id": route_id,
        "segment_ids": segment_ids,
        "source": route_id.split("_", 1)[0] if "_" in route_id else "solver",
        "road_names": [facts.get(s, {}).get("road_name") for s in segment_ids],
        "estimated_time_sec": max(1, int(round(eta))),
        "eta_source": eta_source,
        "distance_m": round(distance, 1) if distance else None,
        "total_distance_m": round(distance, 1) if distance else None,
        "security_score": (
            round(min(max(raw_security / 100.0, 0.0), 1.0), 3)
            if raw_security is not None else audit["security_score"]
        ),
        "disruption_score": (
            round(min(max(raw_disruption / 100.0, 0.0), 1.0), 3)
            if raw_disruption is not None else None
        ),
        "composite_score": _num(route.get("composite_score")),
        "solver_scores": {
            "security_score": raw_security,
            "disruption_score": raw_disruption,
            "composite_score": _num(route.get("composite_score")),
        },
        "scores": route.get("scores"),
        "scores_source": route.get("scores_source"),
        "weighted_score": route.get("weighted_score"),
        "score": route.get("weighted_score"),
        "route_tier": "compliant" if audit["compliant"] else "conditional",
        "lane_violations": audit["violations"],
        "protocol_security_score": audit["security_score"],
        "reason": "",
    }
    for optional in ("estimated_time_p90_sec", "disruption_p90", "duplicate_route_ids"):
        if route.get(optional) is not None:
            shaped[optional] = route[optional]
    return shaped


def _attach_forecast(routes: list[dict], forecast: Any) -> None:
    """Per-route mean forecast at T+15 (or the nearest horizon) — compact."""
    rows = forecast_rows(forecast)
    if not rows:
        for route in routes:
            route["forecast"] = None
        return
    by_segment: dict[int, dict] = {}
    for row in rows:
        best = by_segment.get(row["segment_id"])
        if best is None or abs(row["horizon_min"] - 15) < abs(best["horizon_min"] - 15):
            by_segment[row["segment_id"]] = row
    for route in routes:
        hits = [by_segment[s] for s in route["segment_ids"] if s in by_segment]
        if not hits:
            route["forecast"] = None
            continue
        speeds = [h["speed_kmh"] for h in hits if h.get("speed_kmh") is not None]
        route["forecast"] = {
            "horizon_min": hits[0]["horizon_min"],
            "segments": len(hits),
            "mean_congestion": round(sum(h["congestion_idx"] for h in hits) / len(hits), 3),
            "mean_speed_kmh": round(sum(speeds) / len(speeds), 1) if speeds else None,
        }


def _prompt_view(route: dict) -> dict:
    """Compact candidate view for the tactical prompt (no geometry, no raw blobs)."""
    return {
        "route_id": route["route_id"],
        "segment_ids": route["segment_ids"],
        "roads": route["road_names"],
        "distance_km": round((route["distance_m"] or 0.0) / 1000.0, 2),
        "eta_min": round(route["estimated_time_sec"] / 60.0, 1),
        "weighted_score": route["weighted_score"],
        "security_score": route["security_score"],
        "disruption_score": route["disruption_score"],
        "route_tier": route["route_tier"],
        "lane_violations": [
            {"segment_id": v.get("segment_id"), "detail": v.get("detail")}
            for v in route["lane_violations"]
        ],
        "forecast": route.get("forecast"),
    }


def _merge_narration(
    narration: dict,
    tier: str,
    shaped: list[dict],
    protocol_rejected: list[dict],
) -> dict:
    """Merge the tactical narration back onto the solver candidates by route_id.

    The deterministic order stands (compliant-first, weighted score); the
    narration contributes per-route reasons, the overall reasoning and its
    rejections of ALTERNATES (with reasons). It can never invent a route,
    replace a segment list or drop the primary. A reason the grammar clipped
    at its maxLength ends at its last full sentence; a reason without a
    single figure, or one that contradicts the route's own lane audit, is
    not kept — the route's deterministic line (km, ETA, weighted score,
    violations) is what the operator reads instead.
    """
    routes = [dict(r) for r in shaped]
    by_id = {r["route_id"]: r for r in routes}
    rejected = list(protocol_rejected)
    rejected_ids = {r["route_id"] for r in rejected}
    reasoning = ""
    confidence = "medium"

    if tier == "gemma" and isinstance(narration, dict):
        entries = [narration.get("primary_route"), *(narration.get("alternate_routes") or [])]
        for entry in entries:
            if isinstance(entry, dict):
                route = by_id.get(str(entry.get("route_id")))
                reason = grounded_text(entry.get("reason"), REASON_MAX_CHARS)
                if route is not None and reason and _consistent_with_audit(reason, route):
                    route["reason"] = reason
        primary_id = routes[0]["route_id"] if routes else None
        for entry in narration.get("rejected_routes") or []:
            if not isinstance(entry, dict):
                continue
            rid = str(entry.get("route_id"))
            if rid in by_id and rid != primary_id and rid not in rejected_ids:
                reason = grounded_text(entry.get("reason"), REASON_MAX_CHARS)
                if not reason or not _consistent_with_audit(reason, by_id[rid]):
                    reason = "rejected by the tactical review — " + _route_line(by_id[rid])
                rejected.append({"route_id": rid, "reason": reason})
                rejected_ids.add(rid)
        reasoning = whole_sentences(narration.get("overall_reasoning"), OVERALL_MAX_CHARS)
        if narration.get("confidence") in ("high", "medium", "low"):
            confidence = narration["confidence"]
    elif isinstance(narration, dict):
        reasoning = str(narration.get("overall_reasoning") or "").strip()
        confidence = narration.get("confidence") or "medium"

    for route in routes:
        if not route["reason"]:
            route["reason"] = _route_line(route)
    kept = [r for r in routes if r["route_id"] not in rejected_ids or r is routes[0]]
    return {
        "primary_route": kept[0] if kept else None,
        "alternate_routes": kept[1:],
        "rejected_routes": rejected,
        "overall_reasoning": reasoning,
        "confidence": confidence,
    }


def _route_line(route: dict) -> str:
    distance = (route.get("distance_m") or 0.0) / 1000.0
    tier = (
        "meets the lane floor" if route["route_tier"] == "compliant"
        else f"{len(route['lane_violations'])} protocol violation(s)"
    )
    return (
        f"{len(route['segment_ids'])} segments, {distance:.1f} km, ETA "
        f"{route['estimated_time_sec'] / 60:.0f} min, weighted score "
        f"{(route.get('weighted_score') or 0.0):.2f}; {tier}."
    )


_DIGIT_RE = re.compile(r"\d")

#: The narration schema's string bounds (route_planner.json maxLength).
REASON_MAX_CHARS = 160
OVERALL_MAX_CHARS = 600

#: A reason claiming the route is clear of the lane floor ...
_CLEAR_CLAIM_RE = re.compile(
    r"\b(?:no|zero) (?:lane[- ]|protocol )?violations?\b|\bmeets? the (?:\d+-)?lane floor\b"
    r"|(?<!not )\bfully compliant\b",
    re.IGNORECASE,
)
#: ... or counting its violations ("3 lane violations", "3 lane-violations").
_VIOLATION_COUNT_RE = re.compile(
    r"\b(\d+) (?:lane[- ]|protocol )?violations?\b", re.IGNORECASE)


def _consistent_with_audit(reason: str, route: dict) -> bool:
    """False when a narrated reason contradicts the route's own lane audit.

    Seen live: "… 0.241 disruption score. No lane violations." on a Z+
    primary with three segments below the 6-lane floor. A reason that calls
    a violating route clear, or counts its violations wrong, is not shown.
    """
    violations = len(route.get("lane_violations") or [])
    if violations and _CLEAR_CLAIM_RE.search(reason):
        return False
    counts = {int(n) for n in _VIOLATION_COUNT_RE.findall(reason)}
    return not counts or counts == {violations}

#: The sentence that opens a conditional plan's verdict. The deterministic
#: reasoning and the pre-movement re-route note both state it; the plan's
#: overall reasoning must carry it exactly once.
NO_COMPLIANT_LEAD = "No fully compliant route:"


def _deterministic_reasoning(
    routes: list[dict],
    rejected: list[dict],
    weights: dict[str, float],
    vvip_class: str,
    snap: dict,
    *,
    include_lead: bool = True,
) -> str:
    """Operator-facing explanation of the deterministic ranking.

    ``include_lead=False`` leaves out the 'No fully compliant route: …'
    verdict for a caller that states it itself (pre_movement's re-route note).
    """
    if not routes:
        return "No route candidates survived ranking."
    primary = routes[0]
    weight_text = " / ".join(f"{k} {v:.2f}" for k, v in weights.items())
    origin = snap.get("origin") or {}
    destination = snap.get("destination") or {}
    text = (
        f"Ranked {len(routes) + len(rejected)} distinct candidate(s) by weighted "
        f"score ({weight_text}). Primary {primary['route_id']}: "
        + _route_line(primary)
    )
    if primary["route_tier"] != "compliant" and include_lead:
        text += (
            f" {NO_COMPLIANT_LEAD} "
            + hard_violation_summary(primary["lane_violations"], vvip_class) + "."
        )
    if rejected:
        text += f" {len(rejected)} candidate(s) rejected on protocol."
    if origin.get("segment_id") is not None and destination.get("segment_id") is not None:
        text += (
            f" Origin snapped to {origin['segment_id']} ({origin.get('road_name')}), "
            f"destination to {destination['segment_id']} ({destination.get('road_name')})."
        )
    return text


def _no_route(reason: str, *, snap: dict | None = None) -> dict:
    return {
        "primary_route": None,
        "alternate_routes": [],
        "rejected_routes": [],
        "overall_reasoning": reason,
        "confidence": "low",
        "_tier": "deterministic",
        "_ranked_candidates": [],
        "snap": snap or {},
    }
