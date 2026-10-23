"""Route geometry and the emulated convoy GPS producer (live escort, R9).

Two pieces live here:

``RouteGeometry``
    The escort's primary route as one travel-ordered polyline, built from
    each segment's geometry (GET /api/v1/spatial/segments/{id} through the
    ``query_segment_details`` tool). Segments are oriented so consecutive
    segments join end to start. The escort projects every fix onto it for
    progress, ETA and the segments ahead; the simulator walks it.

    The line is the DRAWN one (COALESCE(display_geom, geom), contract C-K),
    but time and distance are ROUTING quantities: each segment's pace —
    drawn metres per routing metre, ``display_length_m / length_m`` from the
    same read — converts between the two, so the remaining distance, the
    ETA and the just-in-time closure windows are priced on the schematic
    lengths the plan used, and the walker reaches every junction when the
    plan says (on a traced segment it covers more drawn metres per second).
    A schematic segment, or an oracle without ``display_length_m``, paces 1.

``GpsSimulator``
    With VVIP_ESCORT_SIM_GPS=1 the escort starts this walker: it advances
    along the route at the convoy's cruise speed (capped by each segment's
    speed limit), and publishes one fix per tick through the same accept
    path as POST /movements/{id}/position — labelled ``source:
    "emulated_gps"``, so the city picture badges the convoy EMULATED. It
    pauses while a decider-ordered hold is in force, follows a re-route
    from the spot it has reached, and stops for good as soon as a real
    (``"gps"``) fix is accepted for the movement.

Pure computation plus one tool call per segment; no model calls.
"""

from __future__ import annotations

import asyncio
import bisect
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog

from convoy_brain.config import ESCORT_SIM_TICK_SEC

logger = structlog.get_logger(__name__)

EARTH_RADIUS_M = 6_371_008.8
_M_PER_DEG = math.pi * EARTH_RADIUS_M / 180.0

#: A segment's pace (drawn / routing length) is trusted inside these bounds;
#: the display generator admits 0.8-1.8 (015 + gen_display_geometry), so
#: anything outside is a bad read, clamped rather than believed.
PACE_MIN = 0.25
PACE_MAX = 4.0

Point = tuple[float, float]  # (lon, lat)


def utc_now_iso() -> str:
    """ISO-8601 UTC with milliseconds and a Z suffix (contract time format)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def haversine_m(a: Point, b: Point) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(a: Point, b: Point) -> float:
    """Initial bearing a -> b in degrees clockwise from north (0..360)."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


@dataclass(frozen=True)
class PositionFix:
    """One convoy fix as the brain records and mirrors it (C-B / C-J)."""

    lon: float
    lat: float
    speed_kmh: float | None
    heading: float | None
    t: str
    source: str  # "gps" | "emulated_gps"


@dataclass(frozen=True)
class RouteLocation:
    """A fix projected onto the route."""

    distance_m: float  # along the (drawn) route line from its start
    offset_m: float    # cross-track distance of the fix from the route line
    index: int         # position in segment_ids of the segment at distance_m
    segment_id: int
    remaining_m: float  # ROUTING metres to the end (the plan's lengths, C-K)
    progress: float    # 0..1 along the drawn line


def segment_line(geometry: Any) -> list[Point]:
    """GeoJSON LineString / MultiLineString -> one point list (parts chained)."""
    if not isinstance(geometry, dict):
        return []
    coords = geometry.get("coordinates")
    if geometry.get("type") == "LineString":
        parts = [coords]
    elif geometry.get("type") == "MultiLineString":
        parts = coords
    else:
        return []
    line: list[Point] = []
    for part in parts or []:
        pts = [
            (float(c[0]), float(c[1]))
            for c in (part or [])
            if isinstance(c, (list, tuple)) and len(c) >= 2
        ]
        if not pts:
            continue
        if line and haversine_m(line[-1], pts[-1]) < haversine_m(line[-1], pts[0]):
            pts.reverse()
        line.extend(pts)
    return line


class RouteGeometry:
    """A route's segments as one travel-ordered polyline with distances.

    ``paces`` (segment -> drawn metres per routing metre, see
    :func:`segment_pace`) default to 1: the drawn line IS the routing line.
    """

    def __init__(
        self,
        segment_ids: list[int],
        lines: dict[int, list[Point]],
        *,
        speed_limits: dict[int, float | None] | None = None,
        names: dict[int, str | None] | None = None,
        paces: dict[int, float] | None = None,
        source: str = "plan",
    ) -> None:
        self.segment_ids = [int(s) for s in segment_ids]
        self.source = source
        self._lines = {int(k): list(v) for k, v in lines.items() if v}
        self._limits = dict(speed_limits or {})
        self._names = dict(names or {})
        self._paces = {
            int(k): float(v) for k, v in (paces or {}).items()
            if isinstance(v, (int, float)) and math.isfinite(v) and v > 0
        }
        self.missing = [s for s in self.segment_ids if s not in self._lines]

        oriented: list[list[Point]] = []
        for i, sid in enumerate(self.segment_ids):
            line = list(self._lines.get(sid) or [])
            if not line:
                oriented.append([])
                continue
            previous = next((o for o in reversed(oriented) if o), None)
            if previous is None:
                following = next(
                    (self._lines[s] for s in self.segment_ids[i + 1:] if self._lines.get(s)),
                    None,
                )
                if following:
                    ends = (following[0], following[-1])
                    if (min(haversine_m(line[0], e) for e in ends)
                            < min(haversine_m(line[-1], e) for e in ends)):
                        line.reverse()
            elif haversine_m(line[-1], previous[-1]) < haversine_m(line[0], previous[-1]):
                line.reverse()
            oriented.append(line)

        self.points: list[Point] = []
        self.cum: list[float] = []
        self.ranges: list[tuple[int, float, float]] = []
        total = 0.0
        for sid, line in zip(self.segment_ids, oriented, strict=True):
            start = total
            for p in line:
                if self.points:
                    step = haversine_m(self.points[-1], p)
                    if step < 0.5:
                        continue  # the shared junction vertex
                    total += step
                self.points.append(p)
                self.cum.append(total)
            self.ranges.append((sid, start, total))
        self.length_m = total
        self._range_starts = [r[1] for r in self.ranges]
        lat0 = self.points[0][1] if self.points else 23.0
        self._kx = math.cos(math.radians(lat0)) * _M_PER_DEG
        self._ky = _M_PER_DEG

    # -- queries -----------------------------------------------------------------

    @property
    def usable(self) -> bool:
        return len(self.points) >= 2 and self.length_m > 0

    @property
    def start(self) -> Point | None:
        return self.points[0] if self.points else None

    @property
    def end(self) -> Point | None:
        return self.points[-1] if self.points else None

    def index_at(self, distance_m: float) -> int:
        """Index (into segment_ids) of the segment covering ``distance_m``."""
        if not self.ranges:
            return 0
        # The last segment starting at/before the distance: a geometry-less
        # (zero-length) segment sharing that start is passed over.
        i = bisect.bisect_right(self._range_starts, distance_m) - 1
        return max(0, min(i, len(self.ranges) - 1))

    def locate(self, lon: float, lat: float, hint_m: float | None = None) -> RouteLocation:
        """Project a fix onto the route (nearest edge; backward jumps of more
        than 50 m behind ``hint_m`` are penalised so a route that passes
        near itself cannot snap the convoy back to an earlier pass)."""
        if not self.points:
            first = self.segment_ids[0] if self.segment_ids else 0
            return RouteLocation(0.0, float("inf"), 0, first, 0.0, 0.0)
        ox, oy = self.points[0]
        px, py = (lon - ox) * self._kx, (lat - oy) * self._ky
        best: tuple[float, float, float] | None = None  # (score, along, offset)
        if len(self.points) == 1:
            best = (0.0, 0.0, haversine_m((lon, lat), self.points[0]))
        for k in range(len(self.points) - 1):
            ax, ay = (self.points[k][0] - ox) * self._kx, (self.points[k][1] - oy) * self._ky
            nxt = self.points[k + 1]
            bx, by = (nxt[0] - ox) * self._kx, (nxt[1] - oy) * self._ky
            dx, dy = bx - ax, by - ay
            length2 = dx * dx + dy * dy
            t = 0.0 if length2 == 0 else max(
                0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2)
            )
            offset = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            along = self.cum[k] + t * (self.cum[k + 1] - self.cum[k])
            score = offset
            if hint_m is not None and along < hint_m - 50.0:
                score += (hint_m - 50.0 - along) * 0.25
            if best is None or score < best[0]:
                best = (score, along, offset)
        _, along, offset = best
        index = self.index_at(along)
        remaining = self.routing_m(along, self.length_m)
        return RouteLocation(
            distance_m=along,
            offset_m=offset,
            index=index,
            segment_id=self.segment_ids[index] if self.segment_ids else 0,
            remaining_m=remaining,
            progress=(along / self.length_m) if self.length_m > 0 else 0.0,
        )

    def point_at(self, distance_m: float) -> tuple[float, float, float | None]:
        """(lon, lat, heading) at ``distance_m`` along the route."""
        if not self.points:
            raise ValueError("route has no geometry")
        if len(self.points) == 1:
            return self.points[0][0], self.points[0][1], None
        d = max(0.0, min(distance_m, self.length_m))
        k = bisect.bisect_right(self.cum, d) - 1
        k = max(0, min(k, len(self.points) - 2))
        span = self.cum[k + 1] - self.cum[k]
        t = 0.0 if span <= 0 else (d - self.cum[k]) / span
        a, b = self.points[k], self.points[k + 1]
        return (
            a[0] + t * (b[0] - a[0]),
            a[1] + t * (b[1] - a[1]),
            round(bearing_deg(a, b), 1),
        )

    def segments_ahead(self, index: int, distance_m: float, lookahead_m: float) -> list[int]:
        """The segment at ``index`` and every later one starting within
        ``lookahead_m`` of ``distance_m`` (route order, no repeats)."""
        out: list[int] = []
        for sid, start, _end in self.ranges[max(0, index):]:
            if start > distance_m + lookahead_m and out:
                break
            if sid not in out:
                out.append(sid)
        return out

    def segment_range(self, segment_id: int, after_index: int = 0) -> tuple[float, float] | None:
        """(start_m, end_m) of the first occurrence at/after ``after_index``."""
        for sid, start, end in self.ranges[max(0, after_index):]:
            if sid == int(segment_id):
                return start, end
        return None

    def pace(self, segment_id: int) -> float:
        """Drawn metres per routing metre on ``segment_id`` (1.0 unless traced)."""
        return self._paces.get(int(segment_id), 1.0)

    def routing_m(self, from_m: float, to_m: float) -> float:
        """Routing metres between two distances along the drawn line (0 when
        ``to_m`` is not ahead of ``from_m``)."""
        if to_m <= from_m:
            return 0.0
        total = 0.0
        for sid, start, end in self.ranges[self.index_at(from_m):]:
            if start >= to_m:
                break
            part = min(end, to_m) - max(start, from_m)
            if part > 0:
                total += part / self.pace(sid)
        return total

    @property
    def routing_length_m(self) -> float:
        """The whole route on the plan's (schematic) lengths."""
        return self.routing_m(0.0, self.length_m)

    def advance(self, distance_m: float, routing_m: float) -> float:
        """The drawn distance reached after ``routing_m`` routing metres from
        ``distance_m`` — each segment's own pace, across segment ends; capped
        at the end of the route."""
        d = max(0.0, min(distance_m, self.length_m))
        left = max(0.0, routing_m)
        for sid, _start, end in self.ranges[self.index_at(d):]:
            if left <= 0:
                break
            if end <= d:
                continue
            pace = self.pace(sid)
            room = (end - d) / pace  # routing metres to this segment's end
            if left < room:
                return d + left * pace
            left -= room
            d = end
        return min(d, self.length_m)

    def speed_limit_at(self, distance_m: float) -> float | None:
        if not self.segment_ids:
            return None
        value = self._limits.get(self.segment_ids[self.index_at(distance_m)])
        return float(value) if isinstance(value, (int, float)) and value > 0 else None

    def road_name(self, segment_id: int) -> str | None:
        return self._names.get(int(segment_id))

    def eta_sec(self, distance_m: float, speed_kmh: Callable[[float | None], float]) -> float:
        """Seconds to the end of the route from ``distance_m`` along it.

        Each remaining segment's ROUTING length (the plan's, C-K) is divided
        by ``speed_kmh(limit)`` (the speed expected on THAT segment from its
        limit), so a slow fix on a 40 km/h street does not price the 100 km/h
        highway ahead at 40, and a traced segment's longer drawn line does
        not price it slower than planned.
        """
        total = 0.0
        for sid, start, end in self.ranges[self.index_at(distance_m):]:
            part = end - max(start, distance_m)
            if part <= 0:
                continue
            limit = self._limits.get(sid)
            limit = float(limit) if isinstance(limit, (int, float)) and limit > 0 else None
            total += part / self.pace(sid) / (max(speed_kmh(limit), 1.0) / 3.6)
        return total

    def splice(self, index: int, tail: list[int], *, source: str,
               tail_lines: dict[int, list[Point]] | None = None,
               tail_limits: dict[int, float | None] | None = None,
               tail_names: dict[int, str | None] | None = None,
               tail_paces: dict[int, float] | None = None) -> RouteGeometry:
        """This route up to and including ``segment_ids[index]``, then ``tail``
        (a leading repeat of the current segment is dropped)."""
        prefix = self.segment_ids[: index + 1]
        tail = [int(s) for s in tail]
        if tail and prefix and tail[0] == prefix[-1]:
            tail = tail[1:]
        lines = dict(self._lines)
        lines.update(tail_lines or {})
        limits = dict(self._limits)
        limits.update(tail_limits or {})
        names = dict(self._names)
        names.update(tail_names or {})
        paces = dict(self._paces)
        paces.update(tail_paces or {})
        return RouteGeometry(prefix + tail, lines, speed_limits=limits, names=names,
                             paces=paces, source=source)

    def summary(self) -> dict:
        return {
            "segment_ids": list(self.segment_ids),
            "length_m": round(self.length_m, 1),
            # additive (C-K): the plan's (schematic) length of the same route
            "routing_length_m": round(self.routing_length_m, 1),
            "source": self.source,
            "missing_geometry": list(self.missing),
        }


async def fetch_segment_details(
    executor: Any,
    segment_ids: list[int],
    cache: dict[int, dict] | None = None,
) -> dict[int, dict]:
    """GET /api/v1/spatial/segments/{id} per segment (concurrent, cached).

    A failed lookup is logged and left out; the caller sees it as missing
    geometry instead of an aborted escort.
    """
    store = cache if cache is not None else {}
    wanted = [int(s) for s in dict.fromkeys(segment_ids) if s is not None]

    async def one(sid: int) -> tuple[int, dict | None]:
        try:
            details = await executor.execute("query_segment_details", {"segment_id": sid})
        except Exception as exc:  # one bad segment never aborts the escort
            logger.warning("gps_sim.segment_lookup_failed", segment_id=sid, error=str(exc)[:200])
            return sid, None
        return sid, details if isinstance(details, dict) else None

    missing = [sid for sid in wanted if sid not in store]
    if missing:
        for sid, details in await asyncio.gather(*(one(s) for s in missing)):
            if details is not None:
                store[sid] = details
    return {sid: store[sid] for sid in wanted if sid in store}


def geometry_parts(details: dict[int, dict]) -> tuple[
    dict[int, list[Point]], dict[int, float | None], dict[int, str | None]
]:
    """Segment details -> (lines, speed limits, road names)."""
    lines = {sid: segment_line(d.get("geometry")) for sid, d in details.items()}
    limits = {sid: d.get("speed_limit_kmh") for sid, d in details.items()}
    names = {sid: d.get("road_name") for sid, d in details.items()}
    return {s: ln for s, ln in lines.items() if ln}, limits, names


def _positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def segment_pace(details: dict) -> float:
    """Drawn metres per routing metre of one segment (contract C-K).

    GET /api/v1/spatial/segments/{id} answers ``length_m`` (the schematic
    routing length every ETA and closure window is priced on) and
    ``display_length_m`` (the line drawn).  1.0 when either is missing (an
    oracle before the spatial package) or the segment is schematic; clamped
    to [PACE_MIN, PACE_MAX].
    """
    routing = _positive(details.get("length_m"))
    drawn = _positive(details.get("display_length_m"))
    if routing is None or drawn is None:
        return 1.0
    return min(PACE_MAX, max(PACE_MIN, drawn / routing))


def geometry_paces(details: dict[int, dict]) -> dict[int, float]:
    """Segment details -> {segment: pace} for the segments that are not 1."""
    paces = {sid: segment_pace(d) for sid, d in details.items()}
    return {sid: pace for sid, pace in paces.items() if pace != 1.0}


async def load_route_geometry(
    executor: Any,
    segment_ids: list[int],
    *,
    source: str = "plan",
    cache: dict[int, dict] | None = None,
) -> RouteGeometry | None:
    """Build the travel-ordered route polyline; None when no segment has
    usable geometry."""
    if not segment_ids:
        return None
    details = await fetch_segment_details(executor, segment_ids, cache)
    lines, limits, names = geometry_parts(details)
    route = RouteGeometry(segment_ids, lines, speed_limits=limits, names=names,
                          paces=geometry_paces(details), source=source)
    if not route.usable:
        return None
    if route.missing:
        logger.warning("gps_sim.route_geometry_partial", missing=route.missing)
    return route


class GpsSimulator:
    """Walks the escort's current route and publishes EMULATED fixes.

    The cruise speed is a ROUTING speed: each tick covers ``speed x dt``
    routing metres, converted to the drawn line by the segment's pace
    (RouteGeometry.advance), so the emulated convoy keeps the plan's
    timetable on OSM-traced segments too.
    """

    def __init__(
        self,
        *,
        movement_id: str,
        get_route: Callable[[], RouteGeometry | None],
        publish: Callable[[PositionFix], Awaitable[Any]],
        cruise_kmh: float,
        tick_sec: float = ESCORT_SIM_TICK_SEC,
        is_holding: Callable[[], bool] = lambda: False,
        should_stop: Callable[[], bool] = lambda: False,
        start: Point | None = None,
        route_wait_sec: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.movement_id = movement_id
        self._get_route = get_route
        self._publish = publish
        self._cruise_kmh = float(cruise_kmh)
        self._tick = max(0.05, float(tick_sec))
        self._is_holding = is_holding
        self._should_stop = should_stop
        self._start = start
        self._route_wait = route_wait_sec
        self._clock = clock
        self._sleep = sleep
        self.published = 0
        self.distance_m = 0.0

    async def run(self) -> str:
        """Walk until arrival or a stop; returns why it ended
        (``arrived`` | ``stopped`` | ``no_route``)."""
        waited = 0.0
        route = self._get_route()
        while route is None or not route.usable:
            if self._should_stop():
                return "stopped"
            if waited >= self._route_wait:
                logger.warning("gps_sim.no_route", movement_id=self.movement_id)
                return "no_route"
            await self._sleep(self._tick)
            waited += self._tick
            route = self._get_route()

        # Resume from the last known fix when it lies on the route.
        if self._start is not None:
            loc = route.locate(*self._start)
            if loc.offset_m <= 200.0:
                self.distance_m = loc.distance_m
        logger.info(
            "gps_sim.start",
            movement_id=self.movement_id,
            route_m=round(route.length_m, 1),
            from_m=round(self.distance_m, 1),
            cruise_kmh=self._cruise_kmh,
        )

        last = self._clock()
        while True:
            if self._should_stop():
                return "stopped"
            current = self._get_route()
            if current is not None and current is not route and current.usable:
                # Re-route / plan B: carry on from the spot already reached.
                lon, lat, _ = route.point_at(self.distance_m)
                self.distance_m = current.locate(lon, lat, hint_m=self.distance_m).distance_m
                route = current
            now = self._clock()
            dt = min(max(now - last, 0.0), self._tick * 3)
            last = now
            holding = self._is_holding()
            speed = 0.0
            if not holding:
                limit = route.speed_limit_at(self.distance_m)
                speed = min(self._cruise_kmh, limit) if limit else self._cruise_kmh
                self.distance_m = route.advance(self.distance_m, speed / 3.6 * dt)
            arrived = self.distance_m >= route.length_m - 0.5
            lon, lat, heading = route.point_at(self.distance_m)
            fix = PositionFix(
                lon=round(lon, 7),
                lat=round(lat, 7),
                speed_kmh=0.0 if (holding or arrived) else round(speed, 1),
                heading=heading,
                t=utc_now_iso(),
                source="emulated_gps",
            )
            try:
                await self._publish(fix)
                self.published += 1
            except Exception as exc:  # a failed publish never kills the walker
                logger.warning("gps_sim.publish_failed", movement_id=self.movement_id,
                               error=str(exc)[:200])
            if arrived:
                logger.info("gps_sim.arrived", movement_id=self.movement_id,
                            published=self.published)
                return "arrived"
            await self._sleep(self._tick)
