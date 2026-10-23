#!/usr/bin/env python3
"""gen_osm_network.py — Veer's Ahmedabad road network, re-anchored on OpenStreetMap.

The corridor store's 49 road segments (ids 1001-1049) and their junctions
(ids 1-33, plus Koba Circle 34) used to be SCHEMATIC: hand-placed 2-5 vertex
lines whose road names sat a median ~1 km from the real roads of those names.
This script rebuilds the network on the real roads:

  * every junction is a REAL intersection — one OpenStreetMap node, chosen in
    the reviewable mapping table ``scripts/data/osm_network_map.json`` (node id,
    EN + Gujarati name, evidence, confidence);
  * every segment is the path along the named OSM way(s) between its two
    junction nodes (or to a declared dead end), traced by a shortest-path
    search that prefers the ways whose name matches the segment's road (the
    table's ``follow`` pattern), keeps to the main carriageway (``min_class``:
    a service road beside it costs more) and honours OSM one-ways: a one-way
    segment is traced its own legal direction only;
  * ids are kept; a seed name that is not a real road takes the real road's
    name, and the table records the change and why (``rename_reason``);
  * road class, lanes and speed limit come from the OSM tags along the path,
    the seed values as fallback (the table's ``fallback`` where a renamed
    segment is now another road), clamped to what the OSM class admits.

From the traced network it derives, deterministically, everything the store
holds about it: the movements (``segment_adjacency``: every ordered pair of
segments meeting at a junction, turn costs by the post-seed C2 formula), the
signal programs of the signalised junctions (the 006 derivation over the
junction's real approaches), the emulated camera sites (stop lines 25 m back
on an approach, bridge heads, the watchlist sweep cameras), the agency
stations, and writes:

  services/corridor-store/seed/sample_corridor.sql      the fresh-install seed
  services/corridor-store/seed/reanchor_osm_network.sql the idempotent network
        step: converts an EXISTING store in place (every id preserved) and
        completes a fresh one (junctions 16+, names, cameras, programs); it
        keeps the EMULATED camera reads of a segment it moves at their place
        along that segment — in corridor.vehicle_sightings and in the runs'
        recorded tapes (emulator.emulation_runs) alike — moves an
        event-profile spine still at its seeded (schematic) value onto the
        use-case library's route (each checked to be a legal path, one-ways
        included), and corrects event-profile text still at a seeded value
        the emulator does not live up to (the watchlist sweep's promised
        earmarked-plate hit)
  services/corridor-store/seed/display_geometry_osm.sql superseded: the
        routing geom IS the road now, so it only clears migration 015 lines
  scripts/data/osm_network.json                         the evidence table:
        per junction its OSM node; per segment its OSM way ids, the OSM names
        along it, the stored line, the raw OSM path and the metrics

Network etiquette: two bulk Overpass extracts (the city's motorway..
unclassified roads with their signal / named nodes, and the walled city's
residential lanes Gandhi Road runs on), at most one request per second, a
descriptive User-Agent, EVERY response cached under ``--cache``: a run from
the cache is offline and byte-identical.  The two extracts the committed
files were built from are committed too, as the cache file's own JSON in
``scripts/data/osm/<label>.overpass.json.gz``: without a cache a clone
regenerates from that snapshot, and writing the outputs rewrites it, so the
snapshot and the files never drift apart.  Without ``--fetch`` a miss in
both is an error, never a silent network call.

``--verify`` is the gates' check.  From the committed evidence table alone it
re-measures, per segment, the OSM way ids, the name match, the Hausdorff
distance to the OSM centreline (<= 15 m), the endpoints (exactly the
junction points), the length against the OSM path (within 2 %); it checks
the network (one strongly connected directed component, every one-way
legal, cameras on their own segment), that the seed and the network step
carry the same network, that the step's event-profile spines are the
library's routes, each a legal path on the evidence network, and that its
event-profile text corrections start from the values the migrations seed.
With the extracts (the cache, else the committed snapshot) it also measures each
junction against its OSM node (<= 25 m), regenerates every output in memory
and requires byte-identity; without them it says those two checks were NOT
run.

Data: (c) OpenStreetMap contributors, ODbL 1.0 — every generated file says so.

Usage (stdlib only; paths resolve from the repo root):
    python3 scripts/gen_osm_network.py --fetch --cache DIR   # a newer extract
    python3 scripts/gen_osm_network.py [--cache DIR]         # from cache / snapshot
    python3 scripts/gen_osm_network.py --verify [--cache DIR]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import heapq
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_DIR = REPO_ROOT / "services" / "corridor-store" / "seed"
MAP_JSON = REPO_ROOT / "scripts" / "data" / "osm_network_map.json"
NETWORK_JSON = REPO_ROOT / "scripts" / "data" / "osm_network.json"
SEED_SQL = SEED_DIR / "sample_corridor.sql"
REANCHOR_SQL = SEED_DIR / "reanchor_osm_network.sql"
DISPLAY_SQL = SEED_DIR / "display_geometry_osm.sql"
#: The migrations that seed events.event_profiles (ON CONFLICT DO NOTHING;
#: checksum-ledgered, never edited): a profile still carrying their spine
#: has not been changed by an operator.
MIGRATIONS_DIR = REPO_ROOT / "services" / "corridor-store" / "migrations"
PROFILE_SEED_MIGRATIONS = (MIGRATIONS_DIR / "008_event_profiles.sql",
                           MIGRATIONS_DIR / "010_vehicles_watchlist.sql")
#: The use-case library the emulator runs (each use case's route_segment_ids
#: is the spine it instantiates events on) and the twin's scenario library,
#: which must agree with it: the source of truth for a profile's spine.
USE_CASE_LIBRARY = (REPO_ROOT / "services" / "event-emulator" / "src" / "event_emulator"
                    / "use_case_library.json")
SCENARIO_LIBRARY = REPO_ROOT / "scripts" / "scenario_library.json"
DEFAULT_CACHE = Path(
    os.environ.get("VEER_OSM_CACHE", Path.home() / ".cache" / "veer" / "osm-network")
)
#: The committed snapshot of the extracts the committed files were built from.
SNAPSHOT_DIR = REPO_ROOT / "scripts" / "data" / "osm"

USER_AGENT = (
    "Veer-osm-network/1.0 (corridor seed re-anchored on OpenStreetMap roads; "
    "one bulk extract, responses cached; +https://github.com/shubro18202758/VVIP)"
)
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
#: Never more than one request per second.
MIN_REQUEST_INTERVAL_SEC = 1.1

#: The two cached extracts — the cache key is the query text, byte for byte.
#: city: Ahmedabad's motorway..unclassified roads (+ links) with every tag
#: (names, oneway, lanes, maxspeed, bridge, junction) and node ids, plus the
#: signal / named / junction nodes on them; walled_city: the residential
#: lanes of the old city (Gandhi Road's west end is one).
EXTRACTS = (
    ("city",
     '[out:json][timeout:300][bbox:22.900,72.450,23.200,72.750];\n'
     'way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified)(_link)?$"]'
     '->.roads;\n'
     '.roads out body geom;\n'
     '(node(w.roads)["highway"="traffic_signals"];node(w.roads)["name"];'
     'node(w.roads)["junction"];);\n'
     'out body;\n'),
    ("walled_city",
     '[out:json][timeout:180][bbox:23.005,72.570,23.045,72.612];\n'
     'way["highway"~"^(residential|living_street|unclassified|road)$"];\n'
     'out body geom;\n'),
)

ATTRIBUTION = "(c) OpenStreetMap contributors, ODbL 1.0 — https://www.openstreetmap.org/copyright"

# ── tracing ──────────────────────────────────────────────────────────────────
#: Edge cost = metres x factor.  The factor starts at 1 and adds
#: NAME_MISMATCH on a way that is not the segment's road (UNNAMED_CARRIAGEWAY
#: instead when the way is unnamed but of the segment's ``min_class`` or
#: better: a slip, a flyover ramp or an untagged piece of the carriageway),
#: MINOR_ROAD on residential / unclassified lanes, BELOW_CLASS on a way below
#: ``min_class`` (the service road beside a highway); it is then multiplied
#: by (1 + WRONG_WAY) against a one-way carriageway — two-way segments only:
#: a dual carriageway is drawn along one of its halves, and a one-way
#: segment never enters a way against its direction.
NAME_MISMATCH = 4.0
UNNAMED_CARRIAGEWAY = 0.5
MINOR_ROAD = 3.0
BELOW_CLASS = 2.0
WRONG_WAY = 0.3
MINOR_CLASSES = frozenset({"residential", "living_street", "road", "unclassified", "service"})

# ── geometry output ──────────────────────────────────────────────────────────
#: Douglas-Peucker tolerance of the stored line (metres) — the line stays
#: within this of the OSM centreline.
DP_TOLERANCE_M = 2.0
#: A junction leg's bearing is read off its first vertex pair (the e-challan
#: engine's _junction_legs, the turn-cost formula): when that pair is shorter
#: than the leg length, the point that far along the OSM path becomes vertex
#: 2 — the longest of LEG_TRY_M whose chord keeps the OSM path within
#: LEG_DEVIATION_MAX_M (a junction node a few metres off the carriageway
#: must not turn every leg's bearing into the stub's).
LEG_TRY_M = (40.0, 32.0, 25.0, 20.0)
LEG_DEVIATION_MAX_M = 6.0
LEG_MIN_M = LEG_TRY_M[-1]
COORD_DP = 7

# ── validation (the acceptance limits) ──────────────────────────────────────
HAUSDORFF_MAX_M = 15.0
LENGTH_TOL = 0.02
JUNCTION_NODE_MAX_M = 25.0
#: Junction points closer than this would let the store's 25 m geometry rules
#: (post-seed C1-C3, the 012 road_graph view) confuse two junctions.
JUNCTION_SPACING_MIN_M = 60.0
NAME_OVERLAP_MIN = 0.5
#: Segments 1041-1049 are the seed's connectors: they may run along several
#: real roads (each recorded) instead of one road of their name.
CONNECTOR_IDS = frozenset(range(1041, 1050))
#: Two segments may share at most this much road (a slip lane at a junction).
OVERLAP_MAX_M = 160.0
#: Every segment must be drivable the way it is open: the shortest LEGAL drive
#: start -> end (and end -> start when two-way) is at most this multiple of
#: its length plus the slack ...
REVERSE_RATIO_MAX = 1.5
REVERSE_SLACK_M = 300.0
#: ... measured between the carriageways: the drive may start / end at any
#: road node this close to the junction node.
CARRIAGEWAY_NEAR_M = 50.0

#: The fresh-install seed creates junctions 1..15 in serial order (the impact
#: harness counts its POINT literals); reanchor_osm_network.sql the rest.
SEED_JUNCTIONS = 15

#: Stop-line camera sites sit this far back along their approach.
STOP_LINE_BACK_M = 25.0
#: A camera site keeps at least this far from every segment but its own ...
CAMERA_CLEARANCE_M = 3.0
#: ... sliding back along its approach for it, an ANPR stop line no further
#: than the e-challan engine's 40 m junction radius, a CCTV site further.
STOP_LINE_MAX_BACK_M = 38.0
CCTV_MAX_BACK_M = 150.0
#: A bridge-head camera sits this fraction into the bridge span.
BRIDGE_HEAD_FRAC = 0.15

_last_request_at = 0.0

# ─────────────────────────────────────────────────────────────────────────────
# geometry
# ─────────────────────────────────────────────────────────────────────────────

_R_EARTH_M = 6_371_008.8
_LAT0 = 23.03
_KX = math.radians(1.0) * _R_EARTH_M * math.cos(math.radians(_LAT0))
_KY = math.radians(1.0) * _R_EARTH_M

Point = tuple[float, float]  # (lon, lat)


def dist_m(a: Point, b: Point) -> float:
    """Great-circle distance (haversine) in metres."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * _R_EARTH_M * math.asin(min(1.0, math.sqrt(h)))


def length_m(line: list[Point]) -> float:
    return sum(dist_m(a, b) for a, b in zip(line, line[1:]))


def bearing_deg(a: Point, b: Point) -> float:
    """Initial great-circle bearing a -> b, degrees clockwise from north."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return math.degrees(math.atan2(y, x)) % 360.0


def _xy(p: Point) -> tuple[float, float]:
    return p[0] * _KX, p[1] * _KY


def point_segment_m(p: Point, a: Point, b: Point) -> float:
    px, py = _xy(p)
    ax, ay = _xy(a)
    bx, by = _xy(b)
    dx, dy = bx - ax, by - ay
    den = dx * dx + dy * dy
    t = 0.0 if den == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / den))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def point_line_m(p: Point, line: list[Point]) -> float:
    return min(point_segment_m(p, a, b) for a, b in zip(line, line[1:]))


def point_line_deg(p: Point, line: list[Point]) -> float:
    """PLANAR distance in degrees — what PostGIS ``geom <-> geom`` orders by."""
    best = math.inf
    for a, b in zip(line, line[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        den = dx * dx + dy * dy
        t = 0.0 if den == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / den))
        best = min(best, math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy))
    return best


def densify(line: list[Point], step_m: float = 5.0) -> list[Point]:
    out = [line[0]]
    for a, b in zip(line, line[1:]):
        n = max(1, int(math.ceil(dist_m(a, b) / step_m)))
        for i in range(1, n + 1):
            out.append((a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n))
    return out


def hausdorff_m(a: list[Point], b: list[Point]) -> float:
    """Discrete Hausdorff distance, both lines densified to 5 m."""
    da, db = densify(a), densify(b)
    return max(max(point_line_m(p, b) for p in da), max(point_line_m(p, a) for p in db))


def douglas_peucker(line: list[Point], tol_m: float) -> list[Point]:
    if len(line) < 3:
        return list(line)
    keep = [False] * len(line)
    keep[0] = keep[-1] = True
    stack = [(0, len(line) - 1)]
    while stack:
        i, j = stack.pop()
        best, idx = -1.0, -1
        for k in range(i + 1, j):
            d = point_segment_m(line[k], line[i], line[j])
            if d > best:
                best, idx = d, k
        if idx >= 0 and best > tol_m:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(line, keep) if k]


def point_along(line: list[Point], at_m: float) -> Point:
    """The point ``at_m`` metres along ``line`` (clamped to its end)."""
    walked = 0.0
    for a, b in zip(line, line[1:]):
        d = dist_m(a, b)
        if d > 0 and walked + d >= at_m:
            t = (at_m - walked) / d
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        walked += d
    return line[-1]


def rounded(p: Point) -> Point:
    return (round(p[0], COORD_DP), round(p[1], COORD_DP))


def leg_length_m(path: list[Point]) -> float:
    """How far along ``path`` (from its junction end) vertex 2 goes: the
    longest LEG_TRY_M whose chord keeps the path within LEG_DEVIATION_MAX_M."""
    for leg in LEG_TRY_M:
        end = point_along(path, leg)
        walked, inner = 0.0, []
        for a, b in zip(path, path[1:]):
            walked += dist_m(a, b)
            if walked >= leg:
                break
            inner.append(b)
        if all(point_segment_m(p, path[0], end) <= LEG_DEVIATION_MAX_M for p in inner):
            return leg
    return LEG_TRY_M[-1]


def stored_line(raw: list[Point]) -> list[Point]:
    """``_stored_line`` with the longest legs that keep the line's length
    within 1.5 % of the OSM path's (a short bridge cut at both ends falls
    back to the shortest leg)."""
    total = length_m(raw)
    line = _stored_line(raw, adaptive=True)
    if total > 0 and abs(length_m(line) - total) / total > 0.015:
        line = _stored_line(raw, adaptive=False)
    return line


def _stored_line(raw: list[Point], adaptive: bool) -> list[Point]:
    """The line the store keeps: Douglas-Peucker at DP_TOLERANCE_M, 7-decimal
    coordinates, and — when the first (last) vertex pair is shorter than the
    leg (``leg_length_m``) — the point that far along the OSM path as vertex
    2 (n-1), so a junction leg's bearing is the road's, not a kerb kink's or
    a junction stub's.  Every vertex lies on the OSM path; the endpoints are
    its endpoints exactly."""
    line = douglas_peucker(raw, DP_TOLERANCE_M)
    total = length_m(raw)
    if len(line) >= 2 and total > 3 * LEG_TRY_M[0]:
        head_m = leg_length_m(raw) if adaptive else LEG_MIN_M
        if dist_m(line[0], line[1]) < head_m:
            head = point_along(raw, head_m)
            line = [line[0], head] + [p for p in line[1:]
                                      if dist_m(line[0], p) > head_m + 0.5 or p == line[-1]]
        back = list(reversed(raw))
        tail_m = leg_length_m(back) if adaptive else LEG_MIN_M
        if dist_m(line[-1], line[-2]) < tail_m:
            tail = point_along(back, tail_m)
            body = [p for p in line[:-1] if dist_m(raw[-1], p) > tail_m + 0.5 or p == line[0]]
            line = [*body, tail, raw[-1]]
    out: list[Point] = []
    for p in line:
        q = rounded(p)
        if not out or out[-1] != q:
            out.append(q)
    return out


def planar_centroid(line: list[Point]) -> Point:
    """ST_Centroid of a LINESTRING: the length-weighted mean of its pieces'
    midpoints, planar in degrees (what the emulator anchors a sweep camera on)."""
    sx = sy = total = 0.0
    for a, b in zip(line, line[1:]):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        sx += d * (a[0] + b[0]) / 2
        sy += d * (a[1] + b[1]) / 2
        total += d
    return (sx / total, sy / total) if total else line[0]


def closest_point_deg(p: Point, line: list[Point]) -> Point:
    """ST_ClosestPoint(line, p), planar in degrees."""
    best, out = math.inf, line[0]
    for a, b in zip(line, line[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        den = dx * dx + dy * dy
        t = 0.0 if den == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / den))
        q = (a[0] + t * dx, a[1] + t * dy)
        d = math.hypot(p[0] - q[0], p[1] - q[1])
        if d < best:
            best, out = d, q
    return out


# ─────────────────────────────────────────────────────────────────────────────
# network: cached, rate-limited, never implicit
# ─────────────────────────────────────────────────────────────────────────────


class CacheMiss(RuntimeError):
    pass


def _cache_path(cache: Path, kind: str, key: str) -> Path:
    return cache / f"{kind}-{hashlib.sha256(key.encode()).hexdigest()[:20]}.json"


def _http(url: str, data: bytes | None, timeout: float) -> bytes:
    global _last_request_at
    wait = MIN_REQUEST_INTERVAL_SEC - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https hosts
            return resp.read()
    finally:
        _last_request_at = time.monotonic()


def snapshot_path(label: str) -> Path:
    return SNAPSHOT_DIR / f"{label}.overpass.json.gz"


def read_snapshot(label: str, query: str) -> dict | None:
    """The committed snapshot's response for ``query`` (None when there is no
    snapshot of ``label`` or it was taken with another query text)."""
    path = snapshot_path(label)
    if not path.is_file():
        return None
    doc = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    return doc["response"] if doc.get("request") == query else None


def write_snapshot(label: str, query: str, response: dict) -> bool:
    """Commit-ready snapshot of one extract (the cache file's JSON, gzip with a
    zero mtime); rewritten only when its content changed.  True when written."""
    if read_snapshot(label, query) == response:
        return False
    raw = json.dumps({"request": query, "response": response}, sort_keys=True).encode("utf-8")
    path = snapshot_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
    return True


def extract_source(cache: Path | None, label: str, query: str) -> str:
    """Where ``fetch_overpass`` reads the extract from without --fetch."""
    if cache is not None and _cache_path(cache, "overpass", query).is_file():
        return f"cache {cache}"
    if read_snapshot(label, query) is not None:
        return f"snapshot {snapshot_path(label).relative_to(REPO_ROOT)}"
    return "missing"


def fetch_overpass(cache: Path, query: str, fetch: bool, label: str | None = None) -> dict:
    """The Overpass response for ``query``: the cache, else (without --fetch)
    the committed snapshot of ``label``; requested only with --fetch."""
    path = _cache_path(cache, "overpass", query)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))["response"]
    if not fetch:
        snap = read_snapshot(label, query) if label else None
        if snap is not None:
            return snap
        raise CacheMiss(f"overpass response not cached ({path.name}) and no committed "
                        "snapshot of that query — re-run with --fetch")
    body = urllib.parse.urlencode({"data": query}).encode()
    last: Exception | None = None
    for attempt in range(6):
        endpoint = OVERPASS_ENDPOINTS[attempt % len(OVERPASS_ENDPOINTS)]
        try:
            print(f"[overpass] POST {endpoint} (attempt {attempt + 1})", file=sys.stderr)
            doc = json.loads(_http(endpoint, body, timeout=330))
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            backoff = min(60.0, 5.0 * 2 ** attempt)
            print(f"[overpass] {type(exc).__name__}: {exc} — retry in {backoff:.0f} s",
                  file=sys.stderr)
            time.sleep(backoff)
    else:
        raise RuntimeError(f"Overpass unavailable after retries: {last}")
    cache.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"request": query, "response": doc}, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(path)
    return doc


# ─────────────────────────────────────────────────────────────────────────────
# the OSM way graph
# ─────────────────────────────────────────────────────────────────────────────


def _oneway(tags: dict) -> int:
    """1 forward only, -1 reverse only, 0 both (OSM oneway semantics)."""
    v = str(tags.get("oneway", "")).lower()
    if v in ("yes", "true", "1"):
        return 1
    if v == "-1":
        return -1
    if v in ("no", "false", "0"):
        return 0
    if tags.get("junction") in ("roundabout", "circular") or tags.get("highway") in (
        "motorway", "motorway_link"
    ):
        return 1
    return 0


def clean_text(text: str) -> str:
    """Names in SQL literals: no ';', no apostrophes, no '--', single spaces."""
    text = re.sub(r"-{2,}", "-", str(text).replace(";", ",").replace("'", ""))
    return " ".join(text.split())


@dataclass
class Way:
    way_id: int
    tags: dict
    nodes: list[int]
    oneway: int

    @property
    def highway(self) -> str:
        return str(self.tags.get("highway", ""))

    @property
    def label(self) -> str:
        """The printable OSM name (its ref when it has none, '' when neither)."""
        return clean_text(self.tags.get("name") or self.tags.get("ref") or "")

    @property
    def search_text(self) -> str:
        return " ".join(str(self.tags.get(k, "")) for k in ("name", "name:en", "alt_name", "ref"))


@dataclass
class OsmGraph:
    osm_base: dict[str, str]
    ways: dict[int, Way]
    xy: dict[int, Point]
    #: node -> [(next node, way id, metres, with the way's direction?)]
    adj: dict[int, list[tuple[int, int, float, bool]]]
    #: node -> its tags (the city extract's signal / named / junction nodes)
    node_tags: dict[int, dict]
    #: node -> ids of the ways through it
    node_ways: dict[int, list[int]] = field(default_factory=dict)


def build_graph(docs: list[tuple[str, dict]]) -> OsmGraph:
    ways: dict[int, Way] = {}
    xy: dict[int, Point] = {}
    node_tags: dict[int, dict] = {}
    osm_base: dict[str, str] = {}
    for label, doc in docs:
        osm_base[label] = str((doc.get("osm3s") or {}).get("timestamp_osm_base", "unknown"))
        for el in sorted(doc.get("elements", []), key=lambda e: (e.get("type", ""), e.get("id", 0))):
            if el.get("type") == "node" and el.get("tags"):
                node_tags.setdefault(int(el["id"]), dict(el["tags"]))
                continue
            if el.get("type") != "way" or not el.get("nodes") or not el.get("geometry"):
                continue
            wid = int(el["id"])
            if wid in ways:
                continue
            nodes = [int(n) for n in el["nodes"]]
            for n, p in zip(nodes, el["geometry"]):
                xy[n] = (float(p["lon"]), float(p["lat"]))
            tags = dict(el.get("tags", {}))
            ways[wid] = Way(wid, tags, nodes, _oneway(tags))
    adj: dict[int, list[tuple[int, int, float, bool]]] = {}
    node_ways: dict[int, set[int]] = {}
    for w in sorted(ways.values(), key=lambda w: w.way_id):
        for n in w.nodes:
            node_ways.setdefault(n, set()).add(w.way_id)
        for a, b in zip(w.nodes, w.nodes[1:]):
            if a == b:
                continue
            d = dist_m(xy[a], xy[b])
            adj.setdefault(a, []).append((b, w.way_id, d, w.oneway >= 0))
            adj.setdefault(b, []).append((a, w.way_id, d, w.oneway <= 0))
    for n in adj:
        adj[n].sort()
    return OsmGraph(osm_base, ways, xy, adj, node_tags,
                    {n: sorted(ws) for n, ws in node_ways.items()})


def load_extracts(cache: Path, fetch: bool) -> list[tuple[str, str, dict]]:
    """(label, query, response) per extract — cache, snapshot or --fetch."""
    return [(label, q, fetch_overpass(cache, q, fetch, label)) for label, q in EXTRACTS]


def load_graph(cache: Path, fetch: bool) -> OsmGraph:
    return build_graph([(label, doc) for label, _q, doc in load_extracts(cache, fetch)])


# ─────────────────────────────────────────────────────────────────────────────
# road classes
# ─────────────────────────────────────────────────────────────────────────────

#: road class rank (the store's classes; *_link counts as its road)
CLASS_RANK = {"motorway": 0, "trunk": 1, "primary": 2, "secondary": 3, "tertiary": 4,
              "residential": 5, "service": 6}
#: what an OSM class admits when the tags carry no lanes / maxspeed:
#: (min lanes, max lanes, max speed km/h) — the seed value is clamped to it
CLASS_LIMITS = {
    "motorway": (4, 8, 100), "trunk": (4, 8, 80), "primary": (2, 6, 60),
    "secondary": (2, 4, 50), "tertiary": (2, 4, 40), "residential": (2, 2, 30),
    "service": (1, 2, 20),
}


def osm_class(highway: str) -> str:
    """The store class (001 CHECK) of an OSM highway value."""
    base = highway.removesuffix("_link")
    if base in ("unclassified", "road"):
        return "tertiary"
    if base == "living_street":
        return "residential"
    return base if base in CLASS_RANK else "tertiary"


def _parse_int(value) -> int | None:
    m = re.match(r"\s*(\d+)", str(value or ""))
    return int(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────────────────────────
# tracing
# ─────────────────────────────────────────────────────────────────────────────


class TraceError(RuntimeError):
    pass


def edge_factor(w: Way, follow: re.Pattern | None, min_rank: int | None) -> float:
    """The cost factor of one metre along ``w`` (see NAME_MISMATCH)."""
    f = 1.0
    rank = CLASS_RANK[osm_class(w.highway)]
    if follow is not None and not follow.search(w.search_text):
        if min_rank is not None and not w.label and rank <= min_rank:
            f += UNNAMED_CARRIAGEWAY
        else:
            f += NAME_MISMATCH
    if w.highway in MINOR_CLASSES:
        f += MINOR_ROAD
    if min_rank is not None and rank > min_rank:
        f += BELOW_CLASS
    return f


def _dijkstra(graph: OsmGraph, a: int, b: int, follow: re.Pattern | None,
              min_rank: int | None, oneway: bool,
              avoid: frozenset[int]) -> tuple[list[int], list[int]]:
    best = {a: 0.0}
    parent: dict[int, tuple[int, int] | None] = {a: None}
    heap = [(0.0, a)]
    factors: dict[int, float] = {}
    while heap:
        c, n = heapq.heappop(heap)
        if c > best.get(n, math.inf):
            continue
        if n == b:
            break
        for m, wid, d, forward in graph.adj.get(n, ()):
            if (not forward and oneway) or wid in avoid:
                continue
            f = factors.get(wid)
            if f is None:
                f = factors[wid] = edge_factor(graph.ways[wid], follow, min_rank)
            nc = c + d * (f if forward else f * (1.0 + WRONG_WAY))
            if nc < best.get(m, math.inf):
                best[m] = nc
                parent[m] = (n, wid)
                heapq.heappush(heap, (nc, m))
    if b not in parent:
        raise TraceError(f"no {'legal one-way ' if oneway else ''}path {a} -> {b}")
    nodes, edge_ways = [b], []
    while parent[nodes[-1]] is not None:
        prev, wid = parent[nodes[-1]]  # type: ignore[misc]
        nodes.append(prev)
        edge_ways.append(wid)
    nodes.reverse()
    edge_ways.reverse()
    return nodes, edge_ways


def trace(graph: OsmGraph, a: int, b: int, follow: str | None, min_class: str | None,
          oneway: bool, via: list[int] = (), avoid: frozenset[int] = frozenset()
          ) -> tuple[list[int], list[int]]:
    """Node sequence and per-edge way ids from ``a`` to ``b`` (through ``via``)."""
    rx = re.compile(follow, re.I) if follow else None
    min_rank = CLASS_RANK[min_class] if min_class else None
    stops = [a, *via, b]
    nodes, ways = [a], []
    for s, t in zip(stops, stops[1:]):
        n, w = _dijkstra(graph, s, t, rx, min_rank, oneway, avoid)
        nodes += n[1:]
        ways += w
    return nodes, ways


def _nodes_near(graph: OsmGraph, node: int, radius_m: float) -> set[int]:
    p = graph.xy[node]
    d_lon = radius_m / (_KX if _KX else 1.0)
    d_lat = radius_m / _KY
    return {n for n, q in graph.xy.items()
            if abs(q[0] - p[0]) <= d_lon and abs(q[1] - p[1]) <= d_lat
            and n in graph.adj and dist_m(p, q) <= radius_m}


def shortest_legal_m(graph: OsmGraph, a: int, b: int, cap_m: float = 60_000.0,
                     near_m: float = 0.0) -> float | None:
    """Length of the shortest LEGAL drive a -> b (any road), None if none.

    With ``near_m`` the drive may start at any node within that radius of
    ``a`` and end at any node within it of ``b``: a junction node sits on ONE
    carriageway of a dual road, and the other direction runs on the other."""
    starts = _nodes_near(graph, a, near_m) | {a} if near_m else {a}
    goals = _nodes_near(graph, b, near_m) | {b} if near_m else {b}
    best = {s: dist_m(graph.xy[a], graph.xy[s]) for s in starts}
    heap = sorted((c, s) for s, c in best.items())
    while heap:
        c, n = heapq.heappop(heap)
        if c > best.get(n, math.inf):
            continue
        if n in goals:
            return c + dist_m(graph.xy[n], graph.xy[b])
        if c > cap_m:
            break
        for m, _wid, d, forward in graph.adj.get(n, ()):
            if not forward:
                continue
            nc = c + d
            if nc < best.get(m, math.inf):
                best[m] = nc
                heapq.heappush(heap, (nc, m))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# road names
# ─────────────────────────────────────────────────────────────────────────────

#: Words that say what KIND of road, not WHICH road.
_GENERIC = frozenset({
    "road", "rd", "marg", "path", "highway", "hwy", "street", "st", "lane", "the",
    "link", "connector", "flyover", "bridge", "overbridge", "underpass", "underbridge",
    "approach", "service", "ft", "feet", "new", "old", "main", "cross", "crossroads",
    "circle", "dr", "domestic", "and",
})
#: Ahmedabad's initialisms and their spelled-out names (OSM uses either).
_ALIASES = {
    "sg": ("sarkhej", "gandhinagar"),
    "nh147": ("sarkhej", "gandhinagar"),
    "sp": ("sardar", "patel"),
    "cg": ("chimanlal", "girdharlal"),
}
_FOLD = {"river": "riverfront", "front": "riverfront", "drivein": "drive", "in": ""}


def name_tokens(text: str) -> frozenset[str]:
    """Distinguishing tokens of a road name (aliases expanded, kinds and the
    parenthesised locality qualifier dropped)."""
    text = re.sub(r"\([^)]*\)", " ", text.lower())
    text = text.replace("s.g.", "sg").replace("s.p.", "sp").replace("c.g.", "cg")
    out: set[str] = set()
    for tok in re.split(r"[^a-z0-9]+", text):
        tok = _FOLD.get(tok, tok)
        if not tok or tok in _GENERIC:
            continue
        out.update(_ALIASES.get(tok, (tok,)))
    return frozenset(out)


def way_tokens(w: Way) -> frozenset[str]:
    out: set[str] = set()
    for key in ("name", "name:en", "alt_name", "official_name", "ref"):
        for part in str(w.tags.get(key, "")).split(";"):
            out |= name_tokens(part)
    return frozenset(out)


def names_match(seg_tokens: frozenset[str], tokens: frozenset[str]) -> bool:
    """At least half of the segment's distinguishing tokens (and one) appear."""
    if not seg_tokens or not tokens:
        return False
    hit = len(seg_tokens & tokens)
    return hit >= 1 and hit * 2 >= len(seg_tokens)


#: name-match vocabulary of the evidence table
NAME_EXACT = "exact"            # the OSM ways carry the segment's road name
NAME_PARTIAL = "partial"        # most of the path does; the rest is unnamed / a slip
NAME_CONNECTOR = "connector"    # a 1041-1049 connector along several named roads
NAME_UNNAMED = "osm_unnamed"    # OSM names little of it; the table says why


def name_match_kind(seg_id: int, overlap: float, named_share: float) -> str:
    if overlap >= 0.9:
        return NAME_EXACT
    if overlap >= NAME_OVERLAP_MIN:
        return NAME_PARTIAL
    if seg_id in CONNECTOR_IDS:
        return NAME_CONNECTOR
    return NAME_UNNAMED


# ─────────────────────────────────────────────────────────────────────────────
# the mapping table and the traced network
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Junction:
    junction_id: int
    osm_node_id: int
    lon: float
    lat: float
    name: str
    name_gu: str
    junction_type: str
    signal_control: bool
    evidence: str
    confidence: str
    schematic_name: str | None = None
    #: the OSM names of the ways at the node
    osm_names: list[str] = field(default_factory=list)
    #: metres to the nearest OSM traffic_signals node (None: none within 150 m)
    osm_signal_m: float | None = None

    @property
    def point(self) -> Point:
        return (self.lon, self.lat)


@dataclass
class Segment:
    segment_id: int
    road_name: str
    from_junction: int
    to_junction: int | None
    oneway: bool
    road_class: str
    lanes: int
    speed_limit_kmh: int
    line: list[Point]               # what the store keeps (geom)
    osm_path: list[Point]           # the raw OSM path (evidence)
    osm_node_ids: list[int]
    edge_way_ids: list[int]         # the way of each consecutive node pair
    osm_way_ids: list[int]          # distinct, in path order
    osm_way_id: int                 # the way carrying the most of its length
    osm_names: list[tuple[str, float]]  # (name, metres) in path order, merged
    name_overlap: float
    named_share: float
    name_match: str
    seed: dict
    rename_reason: str | None
    name_note: str | None
    attr_source: dict               # where class / lanes / speed came from
    drive_m: float | None           # the shortest legal drive start -> end
    reverse_drive_m: float | None   # two-way: the shortest legal drive back
    bridge_spans: list[tuple[float, float]] = field(default_factory=list)

    @property
    def length_m(self) -> float:
        return length_m(self.line)


def load_mapping(path: Path = MAP_JSON) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    jids = [j["junction_id"] for j in doc["junctions"]]
    sids = [s["segment_id"] for s in doc["segments"]]
    if len(set(jids)) != len(jids) or len(set(sids)) != len(sids):
        raise SystemExit("ERROR: duplicate ids in the mapping table")
    if sorted(sids) != list(range(1001, 1050)):
        raise SystemExit("ERROR: the mapping table must carry segments 1001-1049")
    if sorted(jids)[:33] != list(range(1, 34)):
        raise SystemExit("ERROR: the mapping table must keep junction ids 1-33")
    cams = [c["camera_id"] for c in doc.get("cameras", [])]
    if len(set(cams)) != len(cams):
        raise SystemExit("ERROR: duplicate camera ids in the mapping table")
    return doc


def build_junctions(graph: OsmGraph, mapping: dict) -> dict[int, Junction]:
    signals = [graph.xy[n] for n, t in sorted(graph.node_tags.items())
               if t.get("highway") == "traffic_signals" and n in graph.xy]
    out: dict[int, Junction] = {}
    for j in mapping["junctions"]:
        node = int(j["osm_node_id"])
        if node not in graph.xy:
            raise SystemExit(f"ERROR: J{j['junction_id']}: OSM node {node} not in the extract")
        lon, lat = rounded(graph.xy[node])
        names = sorted({graph.ways[w].label or f"(unnamed {graph.ways[w].highway})"
                        for w in graph.node_ways.get(node, [])})
        near = [dist_m((lon, lat), p) for p in signals
                if abs(p[0] - lon) < 0.002 and abs(p[1] - lat) < 0.002]
        sig_m = round(min(near), 1) if near and min(near) <= 150.0 else None
        out[int(j["junction_id"])] = Junction(
            int(j["junction_id"]), node, lon, lat, clean_text(j["name"]), j["name_gu"],
            j["junction_type"], bool(j["signal_control"]), j["evidence"], j["confidence"],
            j.get("schematic_name"), names, sig_m)
    return out


def bridge_spans(graph: OsmGraph, nodes: list[int], edge_ways: list[int]) -> list[tuple[float, float]]:
    """(start m, end m) along the path of each run of bridge=yes ways."""
    out: list[tuple[float, float]] = []
    walked = 0.0
    for n1, n2, wid in zip(nodes, nodes[1:], edge_ways):
        d = dist_m(graph.xy[n1], graph.xy[n2])
        if str(graph.ways[wid].tags.get("bridge", "")) not in ("", "no"):
            if out and abs(out[-1][1] - walked) < 1e-6:
                out[-1] = (out[-1][0], walked + d)
            else:
                out.append((walked, walked + d))
        walked += d
    return [(round(a, 1), round(b, 1)) for a, b in out]


def build_segment(graph: OsmGraph, row: dict, junctions: dict[int, Junction]) -> Segment:
    sid = int(row["segment_id"])
    a = junctions[row["from_junction"]].osm_node_id
    if row.get("to_junction"):
        b = junctions[row["to_junction"]].osm_node_id
    elif row.get("end_osm_node_id"):
        b = int(row["end_osm_node_id"])
    else:
        raise SystemExit(f"ERROR: {sid}: no to_junction and no end_osm_node_id")
    oneway = bool(row["oneway"])
    try:
        nodes, ways = trace(graph, a, b, row.get("follow"), row.get("min_class"), oneway,
                            [int(v) for v in row.get("via_osm_node_ids", [])],
                            frozenset(int(v) for v in row.get("avoid_way_ids", [])))
    except TraceError as exc:
        raise SystemExit(f"ERROR: {sid} {row['road_name']}: {exc}") from exc
    raw = [graph.xy[n] for n in nodes]
    # the way ids in path order, and per-way metres
    way_m: dict[int, float] = {}
    order: list[int] = []
    for n1, n2, wid in zip(nodes, nodes[1:], ways):
        if wid not in way_m:
            order.append(wid)
            way_m[wid] = 0.0
        way_m[wid] += dist_m(graph.xy[n1], graph.xy[n2])
    total = sum(way_m.values())
    # the OSM names along the path, consecutive runs merged
    runs: list[list] = []
    for n1, n2, wid in zip(nodes, nodes[1:], ways):
        label = graph.ways[wid].label or "(unnamed)"
        d = dist_m(graph.xy[n1], graph.xy[n2])
        if runs and runs[-1][0] == label:
            runs[-1][1] += d
        else:
            runs.append([label, d])
    seg_tok = name_tokens(row["road_name"])
    matched = sum(m for wid, m in way_m.items() if names_match(seg_tok, way_tokens(graph.ways[wid])))
    named = sum(m for wid, m in way_m.items() if graph.ways[wid].label)
    # attributes: class by length, lanes / maxspeed from the tags (seed fallback)
    by_class: dict[str, float] = {}
    for wid, m in way_m.items():
        cls = osm_class(graph.ways[wid].highway)
        by_class[cls] = by_class.get(cls, 0.0) + m
    road_class = min(by_class, key=lambda c: (-round(by_class[c], 3), CLASS_RANK[c]))
    seed = row["seed"]
    # the fallback when OSM tags none: the table's (a renamed segment is now
    # another road than the seed row described), else the seed's
    fallback, fb_src = (row["fallback"], "table") if row.get("fallback") else (seed, "seed")
    min_l, max_l, max_v = CLASS_LIMITS[road_class]
    lane_m: dict[int, float] = {}
    speed_m: dict[int, float] = {}
    for wid, m in way_m.items():
        w = graph.ways[wid]
        lanes = _parse_int(w.tags.get("lanes"))
        if lanes:
            if w.oneway != 0 and not oneway:
                lanes *= 2  # one carriageway of a dual road: both directions
            lane_m[lanes] = lane_m.get(lanes, 0.0) + m
        speed = _parse_int(w.tags.get("maxspeed"))
        if speed:
            speed_m[speed] = speed_m.get(speed, 0.0) + m
    attr_source = {"road_class": "osm"}
    if sum(lane_m.values()) >= 0.3 * total:
        lanes = max(lane_m, key=lambda k: (lane_m[k], k))
        attr_source["lanes"] = "osm"
    else:
        lanes = max(min_l, min(max_l, int(fallback["lanes"])))
        attr_source["lanes"] = (fb_src if lanes == int(fallback["lanes"])
                                else f"{fb_src}_clamped_to_class")
    if sum(speed_m.values()) >= 0.3 * total:
        speed = max(speed_m, key=lambda k: (speed_m[k], k))
        attr_source["speed_limit_kmh"] = "osm"
    else:
        speed = min(max_v, int(fallback["speed_limit_kmh"]))
        attr_source["speed_limit_kmh"] = (fb_src if speed == int(fallback["speed_limit_kmh"])
                                          else f"{fb_src}_clamped_to_class")
    if oneway:
        attr_source["oneway"] = "osm"
    elif any(graph.ways[w].oneway == 0 for w in way_m):
        attr_source["oneway"] = "osm"
    else:
        attr_source["oneway"] = "osm (dual carriageway)"
    drive = shortest_legal_m(graph, a, b, near_m=CARRIAGEWAY_NEAR_M)
    reverse = None
    if not oneway:
        back = shortest_legal_m(graph, b, a, near_m=CARRIAGEWAY_NEAR_M)
        reverse = round(back, 1) if back is not None else None
    overlap = round(matched / total, 3) if total else 0.0
    named_share = round(named / total, 3) if total else 0.0
    return Segment(
        segment_id=sid,
        road_name=clean_text(row["road_name"]),
        from_junction=int(row["from_junction"]),
        to_junction=int(row["to_junction"]) if row.get("to_junction") else None,
        oneway=oneway,
        road_class=road_class,
        lanes=int(lanes),
        speed_limit_kmh=int(speed),
        line=stored_line(raw),
        osm_path=[rounded(p) for p in raw],
        osm_node_ids=nodes,
        edge_way_ids=ways,
        osm_way_ids=order,
        osm_way_id=max(order, key=lambda w: (round(way_m[w], 3), -w)),
        osm_names=[(name, round(m, 1)) for name, m in runs],
        name_overlap=overlap,
        named_share=named_share,
        name_match=name_match_kind(sid, overlap, named_share),
        seed=seed,
        rename_reason=row.get("rename_reason"),
        name_note=row.get("name_note"),
        attr_source=attr_source,
        drive_m=round(drive, 1) if drive is not None else None,
        reverse_drive_m=reverse,
        bridge_spans=bridge_spans(graph, nodes, ways),
    )


# ─────────────────────────────────────────────────────────────────────────────
# derivations: movements, signal programs, cameras
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Movement:
    from_id: int
    to_id: int
    via: int
    turn_cost_sec: int


def segment_ends(seg: Segment) -> list[tuple[int, bool]]:
    """(junction id, at the segment's START?) for each junction end."""
    out = [(seg.from_junction, True)]
    if seg.to_junction is not None:
        out.append((seg.to_junction, False))
    return out


def movements(segments: dict[int, Segment], junctions: dict[int, Junction]) -> list[Movement]:
    """Every ordered pair of segments meeting at a junction, at that junction —
    the post-seed C2 rule and its turn-cost formula: roundabout 15 s, straight
    on (deflection <= 30 deg) 3 s, signalised turn 12 s, other turn 10 s.  The
    bearings are the stored line's end vertex pairs (C2 reads ST_PointN 2 /
    n-1).  One-way segments keep their physical movements: the directed
    corridor.road_graph view (012) enters them only at their start and leaves
    them only at their end."""
    legs: dict[int, list[tuple[int, Point, Point]]] = {}
    for seg in segments.values():
        for jid, at_start in segment_ends(seg):
            pt = seg.line[0] if at_start else seg.line[-1]
            inner = seg.line[1] if at_start else seg.line[-2]
            legs.setdefault(jid, []).append((seg.segment_id, pt, inner))
    out: list[Movement] = []
    for jid in sorted(legs):
        j = junctions[jid]
        for a_id, a_pt, a_inner in sorted(legs[jid]):
            for b_id, b_pt, b_inner in sorted(legs[jid]):
                if a_id == b_id:
                    continue
                arrive = bearing_deg(a_inner, a_pt)
                depart = bearing_deg(b_pt, b_inner)
                deflection = abs(((depart - arrive + 540.0) % 360.0) - 180.0)
                if j.junction_type == "roundabout":
                    cost = 15
                elif deflection <= 30.0:
                    cost = 3
                elif j.signal_control:
                    cost = 12
                else:
                    cost = 10
                out.append(Movement(a_id, b_id, jid, cost))
    return out


#: migration 006 / post-seed D templates by the junction's dominant class
SIGNAL_TEMPLATES = (
    ("motorway", {"cycle_sec": 90, "phases": [{"green_sec": 44, "movements": ["through"]},
                                              {"green_sec": 34, "movements": ["ramp", "cross"]}]}),
    ("trunk", {"cycle_sec": 100, "phases": [{"green_sec": 47, "movements": ["through"]},
                                            {"green_sec": 38, "movements": ["cross"]}]}),
    ("primary", {"cycle_sec": 110, "phases": [{"green_sec": 50, "movements": ["through"]},
                                              {"green_sec": 42, "movements": ["cross"]}]}),
    ("secondary", {"cycle_sec": 120, "phases": [{"green_sec": 54, "movements": ["through"]},
                                                {"green_sec": 46, "movements": ["cross"]}]}),
)
SIGNAL_OTHER = {"cycle_sec": 120, "phases": [{"green_sec": 40, "movements": ["local"]},
                                             {"green_sec": 34, "movements": ["cross"]}]}


def signal_programs(segments: dict[int, Segment], junctions: dict[int, Junction],
                    moves: list[Movement]) -> dict[int, dict]:
    """Program per signalised junction: the 006 / post-seed D derivation — the
    template of the highest class among the segments whose movements leave
    from it (the junction's real approaches), offset (id x 17) mod cycle."""
    out: dict[int, dict] = {}
    for jid in sorted(junctions):
        j = junctions[jid]
        if not j.signal_control:
            continue
        classes = {segments[m.from_id].road_class for m in moves if m.via == jid}
        if not classes:
            continue
        prog = next((t for cls, t in SIGNAL_TEMPLATES if cls in classes), SIGNAL_OTHER)
        out[jid] = {"cycle_sec": prog["cycle_sec"], "phases": prog["phases"],
                    "offset_sec": (jid * 17) % prog["cycle_sec"]}
    return out


@dataclass(frozen=True)
class CameraSite:
    camera_id: str
    kind: str
    label: str
    junction_id: int | None
    segment_id: int
    lon: float
    lat: float
    heading_deg: float | None

    @property
    def point(self) -> Point:
        return (self.lon, self.lat)


def _approach_site(seg: Segment, jid: int, others: list[Segment], max_back_m: float
                   ) -> tuple[Point, float]:
    """STOP_LINE_BACK_M back from the junction along the segment, reading
    traffic heading INTO the junction (at most half the segment) — slid
    further back, a metre at a time up to ``max_back_m``, while the site
    still sits on a stretch another segment shares (a slip at the junction
    node): a read there must attribute to this segment alone."""
    line = seg.line if seg.from_junction == jid else list(reversed(seg.line))
    back = min(STOP_LINE_BACK_M, seg.length_m / 2)
    while True:
        p = rounded(point_along(line, back))
        clear = all(point_line_m(p, o.line) >= CAMERA_CLEARANCE_M for o in others)
        if clear or back + 1.0 > min(max_back_m, seg.length_m / 2):
            return p, round(bearing_deg(p, line[0]), 1)
        back += 1.0


def _bridge_site(seg: Segment, head: str) -> tuple[Point, float]:
    """BRIDGE_HEAD_FRAC into the bridge span from the ``head`` landing,
    reading traffic heading onto the bridge from that landing."""
    total = length_m(seg.osm_path)
    if seg.bridge_spans:
        lo = min(s for s, _e in seg.bridge_spans)
        hi = max(e for _s, e in seg.bridge_spans)
    else:
        lo, hi = 0.0, total
    at = lo + BRIDGE_HEAD_FRAC * (hi - lo) if head == "start" else hi - BRIDGE_HEAD_FRAC * (hi - lo)
    # placed ON the stored line (within DP_TOLERANCE_M of the OSM path)
    p = rounded(point_along(seg.line, at / total * seg.length_m))
    if head == "start":
        return p, round(bearing_deg(seg.line[0], p), 1)
    return p, round(bearing_deg(seg.line[-1], p), 1)


def camera_sites(mapping: dict, segments: dict[int, Segment],
                 junctions: dict[int, Junction]) -> list[CameraSite]:
    out: list[CameraSite] = []
    for c in mapping.get("cameras", []):
        seg = segments[c["segment_id"]]
        mode = c["mode"]
        jid = c.get("junction_id")
        if mode == "approach":
            if jid not in (seg.from_junction, seg.to_junction):
                raise SystemExit(f"ERROR: camera {c['camera_id']}: segment {seg.segment_id} "
                                 f"does not end at J{jid}")
            if seg.oneway and jid != seg.to_junction:
                raise SystemExit(f"ERROR: camera {c['camera_id']}: one-way {seg.segment_id} "
                                 "only APPROACHES its end junction")
            others = [o for o in segments.values() if o.segment_id != seg.segment_id]
            max_back = STOP_LINE_MAX_BACK_M if c["kind"] == "anpr" else CCTV_MAX_BACK_M
            pt, heading = _approach_site(seg, jid, others, max_back)
            label = c.get("label") or f"{junctions[jid].name}: {seg.road_name} approach"
        elif mode == "bridge":
            pt, heading = _bridge_site(seg, c["head"])
            jid = None
            label = c["label"]
        elif mode == "sweep":
            pt = rounded(closest_point_deg(planar_centroid(seg.line), seg.line))
            heading, jid = None, None
            label = c.get("label") or f"Emulated ANPR sweep camera: {seg.road_name}"
        else:
            raise SystemExit(f"ERROR: camera {c['camera_id']}: unknown mode {mode!r}")
        out.append(CameraSite(c["camera_id"], c["kind"], clean_text(label), jid,
                              seg.segment_id, pt[0], pt[1], heading))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# network checks
# ─────────────────────────────────────────────────────────────────────────────


def road_graph_edges(segments: dict[int, Segment], moves: list[Movement]) -> dict[tuple[int, int], int]:
    """corridor.road_graph (012): a one-way segment is entered only at its
    start and left only at its end (judged at the movement's junction)."""
    edges: dict[tuple[int, int], int] = {}
    for m in moves:
        a, b = segments[m.from_id], segments[m.to_id]
        if b.oneway and b.from_junction != m.via:
            continue
        if a.oneway and a.to_junction != m.via:
            continue
        edges[(m.from_id, m.to_id)] = min(m.turn_cost_sec, edges.get((m.from_id, m.to_id), 10**9))
    return edges


def strongly_connected(nodes: list[int], edges: dict[tuple[int, int], int]) -> list[list[int]]:
    """Strongly connected components (Kosaraju, iterative), sorted."""
    succ: dict[int, list[int]] = {n: [] for n in nodes}
    pred: dict[int, list[int]] = {n: [] for n in nodes}
    for a, b in sorted(edges):
        succ[a].append(b)
        pred[b].append(a)
    order: list[int] = []
    seen: set[int] = set()
    for root in nodes:
        if root in seen:
            continue
        seen.add(root)
        stack = [(root, iter(succ[root]))]
        while stack:
            v, it = stack[-1]
            nxt = next((w for w in it if w not in seen), None)
            if nxt is None:
                stack.pop()
                order.append(v)
            else:
                seen.add(nxt)
                stack.append((nxt, iter(succ[nxt])))
    comps: list[list[int]] = []
    assigned: set[int] = set()
    for root in reversed(order):
        if root in assigned:
            continue
        comp, stack = [], [root]
        assigned.add(root)
        while stack:
            v = stack.pop()
            comp.append(v)
            for w in pred[v]:
                if w not in assigned:
                    assigned.add(w)
                    stack.append(w)
        comps.append(sorted(comp))
    return sorted(comps)


def shared_road_m(graph: OsmGraph, a: Segment, b: Segment) -> float:
    """Metres of OSM edges both segments' paths run along."""
    ea = {frozenset(p) for p in zip(a.osm_node_ids, a.osm_node_ids[1:])}
    total = 0.0
    for n1, n2 in zip(b.osm_node_ids, b.osm_node_ids[1:]):
        if frozenset((n1, n2)) in ea:
            total += dist_m(graph.xy[n1], graph.xy[n2])
    return total


@dataclass(frozen=True)
class ProfileSpine:
    """An events.event_profiles spine the network step rewrites: from the
    value its migration seeded to the use-case library's route."""
    profile_id: str
    seeded: tuple[int, ...]
    spine: tuple[int, ...]


@dataclass(frozen=True)
class ProfileText:
    """An events.event_profiles text column the network step corrects:
    from the value its migration seeded to the honest one (a template is
    its JSON text)."""
    profile_id: str
    column: str          # 'description' | 'template'
    seeded: str
    corrected: str


@dataclass
class Network:
    osm_base: dict[str, str]
    junctions: dict[int, Junction]
    segments: dict[int, Segment]
    moves: list[Movement]
    programs: dict[int, dict]
    cameras: list[CameraSite]
    stations: list[dict]
    shared: dict[tuple[int, int], float]
    components: list[list[int]]
    problems: list[str]
    #: the profile spines that differ from their seeded value (H)
    spines: list[ProfileSpine] = field(default_factory=list)
    #: the profile text corrections (I)
    texts: list[ProfileText] = field(default_factory=list)


def check_network(net: Network) -> list[str]:
    """Every network-level rule the store and its consumers rely on."""
    problems: list[str] = []
    segs, js = net.segments, net.junctions
    used = {jid for s in segs.values() for jid, _ in segment_ends(s)}
    for jid in sorted(set(js) - used):
        problems.append(f"J{jid} {js[jid].name}: no segment ends there")
    if len(net.components) != 1:
        problems.append("road_graph is not one strongly connected component: "
                        + " | ".join(",".join(map(str, c)) for c in net.components))
    for (a_id, b_id), shared in sorted(net.shared.items()):
        if shared > OVERLAP_MAX_M:
            problems.append(f"{a_id} and {b_id} share {shared:.0f} m of road (> {OVERLAP_MAX_M:.0f} m)")
    ids = sorted(js)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            d = dist_m(js[a].point, js[b].point)
            if d < JUNCTION_SPACING_MIN_M:
                problems.append(f"J{a} and J{b} are {d:.0f} m apart (< {JUNCTION_SPACING_MIN_M:.0f} m)")
    for s in segs.values():
        # a segment end that is not a junction must not sit by one (C2 / 012 25 m rules)
        if s.to_junction is None:
            for jid, j in js.items():
                if dist_m(j.point, s.line[-1]) <= 30.0:
                    problems.append(f"{s.segment_id}: dead end within 30 m of J{jid}")
        # the store's 25 m rules must not see a foreign junction at an end
        for jid, at_start in segment_ends(s):
            pt = s.line[0] if at_start else s.line[-1]
            for kid, k in js.items():
                if kid != jid and dist_m(k.point, pt) <= 25.0:
                    problems.append(f"{s.segment_id}: J{kid} within 25 m of its J{jid} end")
        limit = REVERSE_RATIO_MAX * s.length_m + REVERSE_SLACK_M
        if s.drive_m is None or s.drive_m > limit:
            problems.append(f"{s.segment_id} {s.road_name}: the legal drive start -> end is "
                            f"{s.drive_m} m (> {limit:.0f} m) — traced against a one-way?")
        if not s.oneway and (s.reverse_drive_m is None or s.reverse_drive_m > limit):
            problems.append(f"{s.segment_id} {s.road_name}: two-way but the legal drive back is "
                            f"{s.reverse_drive_m} m (> {limit:.0f} m) — one-way in OSM?")
        if s.name_match == NAME_UNNAMED and not s.name_note and not s.rename_reason:
            problems.append(f"{s.segment_id} {s.road_name}: OSM names {s.name_overlap:.0%} of it "
                            "— record a name_note")
    for c in net.cameras:
        # a read at the site must attribute to the camera's own segment (the
        # store orders by geom <-> geom): ON its segment, clear of every other
        # one — never on a stretch two segments share, where the float
        # rounding of two lines decides
        if point_line_m(c.point, segs[c.segment_id].line) > 0.5:
            problems.append(f"camera {c.camera_id}: off its segment")
        for sid, other in segs.items():
            if sid != c.segment_id and point_line_m(c.point, other.line) < CAMERA_CLEARANCE_M:
                problems.append(f"camera {c.camera_id}: within {CAMERA_CLEARANCE_M:.0f} m of "
                                f"segment {sid} as well as its own {c.segment_id}")
    return problems


def build_network(graph: OsmGraph, mapping: dict) -> Network:
    junctions = build_junctions(graph, mapping)
    segments: dict[int, Segment] = {}
    for row in sorted(mapping["segments"], key=lambda r: r["segment_id"]):
        seg = build_segment(graph, row, junctions)
        segments[seg.segment_id] = seg
    moves = movements(segments, junctions)
    programs = signal_programs(segments, junctions, moves)
    cameras = camera_sites(mapping, segments, junctions)
    edges = road_graph_edges(segments, moves)
    comps = strongly_connected(sorted(segments), edges)
    ids = sorted(segments)
    shared = {}
    for i, a_id in enumerate(ids):
        for b_id in ids[i + 1:]:
            m = shared_road_m(graph, segments[a_id], segments[b_id])
            if m > 0:
                shared[(a_id, b_id)] = round(m, 1)
    stations = [dict(s) for s in mapping.get("stations", [])]
    net = Network(graph.osm_base, junctions, segments, moves, programs, cameras, stations,
                  shared, comps, [])
    net.problems = check_network(net)
    net.spines, spine_issues = profile_spines(set(segments), edges)
    net.problems += spine_issues
    net.texts, text_issues = profile_texts()
    net.problems += text_issues
    return net


# ─────────────────────────────────────────────────────────────────────────────
# event-profile spines (events.event_profiles, migrations 008 / 010)
# ─────────────────────────────────────────────────────────────────────────────

#: one VALUES row of the profiles INSERT: its profile_id, then (after the
#: kind, class, crowd class and the texts) its route_segment_ids ARRAY
_PROFILE_ROW_RE = re.compile(
    r"\(\s*'([a-z0-9_]+)',\s*'[a-z_]+',\s*'[^']*',\s*'[A-Z]+',.*?ARRAY\[([0-9,\s]+)\]", re.S)


def seeded_profile_spines() -> dict[str, tuple[int, ...]]:
    """profile_id -> the route_segment_ids its migration seeded."""
    out: dict[str, tuple[int, ...]] = {}
    for path in PROFILE_SEED_MIGRATIONS:
        text = "\n".join(line.split("--", 1)[0]
                         for line in path.read_text(encoding="utf-8").splitlines())
        start = text.index("INSERT INTO events.event_profiles")
        for pid, ids in _PROFILE_ROW_RE.findall(text[start:text.index(";", start)]):
            out[pid] = tuple(int(x) for x in ids.split(","))
    return out


def library_routes() -> tuple[dict[str, tuple[int, ...]], dict[str, tuple[int, ...]]]:
    """(use case id -> route_segment_ids of the emulator's library,
    scenario id -> route_segment_ids of the twin's scenario library)."""
    use_cases = {str(u["id"]): tuple(int(s) for s in u["scenarioConfig"]["route_segment_ids"])
                 for u in json.loads(USE_CASE_LIBRARY.read_text(encoding="utf-8"))}
    scenarios: dict[str, tuple[int, ...]] = {}
    if SCENARIO_LIBRARY.is_file():
        for sc in json.loads(SCENARIO_LIBRARY.read_text(encoding="utf-8")).get("scenarios", []):
            scenarios[str(sc["scenario_id"])] = tuple(int(s) for s in sc["route_segment_ids"])
    return use_cases, scenarios


def spine_problems(spine: tuple[int, ...], segment_ids: set[int],
                   edges: dict[tuple[int, int], int] | set[tuple[int, int]]) -> list[str]:
    """Why ``spine`` is not a legal directed path (corridor.road_graph, the
    one-ways included); [] when it is one."""
    out: list[str] = []
    if not spine:
        return ["an empty spine"]
    unknown = [s for s in spine if s not in segment_ids]
    if unknown:
        out.append(f"unknown segments {unknown}")
    if len(set(spine)) != len(spine):
        out.append("a segment twice")
    out += [f"{a} -> {b} is not a legal move" for a, b in zip(spine, spine[1:])
            if (a, b) not in edges]
    return out


def profile_spines(segment_ids: set[int], edges: dict[tuple[int, int], int] | set[tuple[int, int]]
                   ) -> tuple[list[ProfileSpine], list[str]]:
    """Every seeded profile's spine on this network — the use-case library's
    route of the same id, which the twin's scenario library (where it has
    the id) must carry too, a legal path.  Returns the spines that differ
    from the seeded value (what H rewrites) and the problems."""
    seeded = seeded_profile_spines()
    use_cases, scenarios = library_routes()
    out: list[ProfileSpine] = []
    problems: list[str] = []
    for pid in sorted(seeded):
        spine = use_cases.get(pid)
        if spine is None:
            problems.append(f"profile {pid}: no use case of that id in {USE_CASE_LIBRARY.name}")
            continue
        if pid in scenarios and scenarios[pid] != spine:
            problems.append(f"profile {pid}: {USE_CASE_LIBRARY.name} runs {list(spine)} but "
                            f"{SCENARIO_LIBRARY.name} carries {list(scenarios[pid])}")
        problems += [f"profile {pid} spine {list(spine)}: {p}"
                     for p in spine_problems(spine, segment_ids, edges)]
        if spine != seeded[pid]:
            out.append(ProfileSpine(pid, seeded[pid], spine))
    return out, problems


# ── event-profile text (I) ──────────────────────────────────────────────────

#: Event-profile text the network step corrects where a store still carries
#: the value its migration seeded.  Migration 010's watchlist sweep promised
#: "an earmarked plate" hit (template earmarked_plate_hit true) that the
#: emulator never produces: GJ27EM4321 is an OPERATOR EXERCISE — its reads hit
#: only after a duty officer earmarks it — the honest wording of the use-case
#: library (use_case_library.json, expected feature operator_earmark_exercise).
#: Plain text: no ';', no '--', no quote (the step's splitter-safe grammar).
PROFILE_TEXT_FIXES: dict[str, dict] = {
    "watchlist_anpr_sweep": {
        "description": (
            "EMULATED watchlist scenario (clearly labelled, never a live feed). A "
            "stolen-vehicle FIR drives an ANPR camera sweep around the walled city. "
            "Deterministic camera reads cover background traffic, the FIR-named stolen "
            "plate GJ27EM9001 and the earmark-exercise plate GJ27EM4321, plus patrolling "
            "Jan Rakshak units. The FIR is the premise of the scenario, stated at t0 as a "
            "labelled EMULATED precondition (a stolen_vehicle_fir earmark on GJ27EM9001), "
            "so the reads of the stolen car hit as stolen_vehicle and file evidence-only "
            "R_STOLEN_SIGHTING recommendations, never a fine. GJ27EM4321 is an OPERATOR "
            "EXERCISE: the emulator never earmarks it, so its reads hit as earmarked_plate "
            "only after a duty officer has earmarked it with a lawful basis. Sightings land "
            "in corridor.vehicle_sightings with source emulated_anpr, the oracle computes "
            "every hit and rule, and only a named human officer acts on any recommendation."
        ),
        "template": {"earmarked_plate_hit": False, "operator_earmark_exercise": True},
    },
}

_SQL_LITERAL_RE = re.compile(r"'((?:[^']|'')*)'")


def _sql_row(text: str, start: int) -> tuple[str, int]:
    """The inside of the parenthesised SQL row opening at ``text[start]``
    (string literals respected) and the index just past its close."""
    assert text[start] == "("
    depth, i = 0, start
    while i < len(text):
        ch = text[i]
        if ch == "'":
            i += 1
            while text[i] != "'" or text[i + 1:i + 2] == "'":
                i += 2 if text[i] == "'" else 1
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
        i += 1
    raise ValueError("unbalanced SQL row")


def _sql_values(row: str) -> list[str]:
    """The top-level comma-separated values of a row's inside (commas in
    literals, brackets and parentheses do not split)."""
    out: list[str] = []
    depth, cur, i = 0, [], 0
    while i < len(row):
        ch = row[i]
        if ch == "'":
            j = i + 1
            while row[j] != "'" or row[j + 1:j + 2] == "'":
                j += 2 if row[j] == "'" else 1
            cur.append(row[i:j + 1])
            i = j + 1
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur).strip())
    return out


def _sql_text(value: str) -> str | None:
    """A value written as adjacent string literals (a ::cast after them
    allowed): the text SQL concatenates them into — else None."""
    body = re.sub(r"::\w+\s*$", "", value.strip())
    pieces = _SQL_LITERAL_RE.findall(body)
    if not pieces or _SQL_LITERAL_RE.sub("", body).strip():
        return None
    return "".join(p.replace("''", "'") for p in pieces)


def seeded_profile_rows() -> dict[str, dict[str, str]]:
    """profile_id -> {column: value} of every events.event_profiles row the
    seeding migrations (008 / 010) insert: a literal's text (adjacent
    literals concatenated, the ::cast dropped), any other value verbatim."""
    out: dict[str, dict[str, str]] = {}
    for path in PROFILE_SEED_MIGRATIONS:
        text = "\n".join(line.split("--", 1)[0]
                         for line in path.read_text(encoding="utf-8").splitlines())
        start = text.index("INSERT INTO events.event_profiles")
        stmt = text[start:text.index(";", start)]
        cols_at = stmt.index("(")
        cols = [c.strip() for c in stmt[cols_at + 1:stmt.index(")", cols_at)].split(",")]
        rows = stmt[stmt.index("VALUES") + len("VALUES"):stmt.index("ON CONFLICT")]
        at = rows.find("(")
        while at >= 0:
            inner, end = _sql_row(rows, at)
            values = _sql_values(inner)
            row = {c: (t if (t := _sql_text(v)) is not None else v)
                   for c, v in zip(cols, values)}
            out[row["profile_id"]] = row
            at = rows.find("(", end)
    return out


def _template_json(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, separators=(", ", ": "))


def profile_texts() -> tuple[list[ProfileText], list[str]]:
    """The profile text corrections (PROFILE_TEXT_FIXES) as the step's
    section I writes them — each from the value today's migrations seed —
    and the problems (a profile no migration seeds, text the step's grammar
    cannot carry)."""
    rows = seeded_profile_rows()
    out: list[ProfileText] = []
    problems: list[str] = []
    for pid in sorted(PROFILE_TEXT_FIXES):
        fix = PROFILE_TEXT_FIXES[pid]
        row = rows.get(pid)
        if row is None:
            problems.append(f"profile {pid}: no migration seeds it")
            continue
        if "description" in fix and fix["description"] != row["description"]:
            out.append(ProfileText(pid, "description", row["description"], fix["description"]))
        if "template" in fix:
            seeded = json.loads(row["template"])
            corrected = {**seeded, **fix["template"]}
            if corrected != seeded:
                out.append(ProfileText(pid, "template", _template_json(seeded),
                                       _template_json(corrected)))
    for t in out:
        if ";" in t.corrected or "--" in t.corrected or "'" in t.corrected:
            problems.append(f"profile {t.profile_id} {t.column}: a ';', '--' or quote the "
                            "network step's splitter-safe grammar cannot carry")
    return out, problems


# ─────────────────────────────────────────────────────────────────────────────
# the evidence table
# ─────────────────────────────────────────────────────────────────────────────


def osm_base_line(net: Network) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(net.osm_base.items()))


def segment_meta(s: Segment, osm_base: str) -> dict:
    """corridor.road_segments.geom_meta (migration 020): geom's provenance."""
    return {
        "source": "osm_way_path",
        "osm_base": osm_base,
        "osm_way_ids": s.osm_way_ids,
        "osm_names": sorted({n for n, _m in s.osm_names if n != "(unnamed)"}),
        "name_match": s.name_match,
        "name_overlap": s.name_overlap,
        "length_m": round(s.length_m, 1),
        "osm_length_m": round(length_m(s.osm_path), 1),
        "hausdorff_m": round(hausdorff_m(s.line, s.osm_path), 2),
        "generator": "scripts/gen_osm_network.py",
    }


def evidence_doc(net: Network) -> dict:
    base = osm_base_line(net)
    edges = road_graph_edges(net.segments, net.moves)
    return {
        "about": ("Veer's Ahmedabad road network on OpenStreetMap — the evidence behind "
                  "services/corridor-store/seed/{sample_corridor,reanchor_osm_network}.sql. "
                  "GENERATED by scripts/gen_osm_network.py from scripts/data/osm_network_map.json; "
                  "do not edit by hand."),
        "attribution": ATTRIBUTION,
        "osm_base": net.osm_base,
        "junction_control": (
            "junction_type and signal_control are the seed's, kept for continuity with the "
            "signal programs and the history recorded against them, not read from "
            "OpenStreetMap; osm_traffic_signals_m is the OSM evidence beside them (metres to "
            "the nearest traffic_signals node, null when none lies within 150 m)"),
        "limits": {"hausdorff_max_m": HAUSDORFF_MAX_M, "length_tol": LENGTH_TOL,
                   "junction_node_max_m": JUNCTION_NODE_MAX_M,
                   "dp_tolerance_m": DP_TOLERANCE_M},
        "network": {
            "segments": len(net.segments), "junctions": len(net.junctions),
            "movements": len(net.moves), "road_graph_edges": len(edges),
            "strong_components": len(net.components),
            "total_km": round(sum(s.length_m for s in net.segments.values()) / 1000.0, 2),
            "problems": net.problems,
        },
        "junctions": [
            {
                "junction_id": j.junction_id, "name": j.name, "name_gu": j.name_gu,
                "osm_node_id": j.osm_node_id, "lon": j.lon, "lat": j.lat,
                "junction_type": j.junction_type, "signal_control": j.signal_control,
                "osm_ways_at_node": j.osm_names, "osm_traffic_signals_m": j.osm_signal_m,
                "evidence": j.evidence, "confidence": j.confidence,
                "schematic_name": j.schematic_name,
            }
            for j in (net.junctions[k] for k in sorted(net.junctions))
        ],
        "segments": [
            {
                "segment_id": s.segment_id, "road_name": s.road_name,
                "from_junction": s.from_junction, "to_junction": s.to_junction,
                "oneway": s.oneway, "road_class": s.road_class, "lanes": s.lanes,
                "speed_limit_kmh": s.speed_limit_kmh, "attr_source": s.attr_source,
                "seed": s.seed, "rename_reason": s.rename_reason, "name_note": s.name_note,
                "osm_way_id": s.osm_way_id, "osm_way_ids": s.osm_way_ids,
                "osm_names": [[n, m] for n, m in s.osm_names],
                "name_match": s.name_match, "name_overlap": s.name_overlap,
                "named_share": s.named_share,
                "length_m": round(s.length_m, 1),
                "osm_length_m": round(length_m(s.osm_path), 1),
                "hausdorff_m": round(hausdorff_m(s.line, s.osm_path), 2),
                "drive_m": s.drive_m, "reverse_drive_m": s.reverse_drive_m,
                "bridge_spans_m": [list(b) for b in s.bridge_spans],
                "line": [list(p) for p in s.line],
                "osm_node_ids": s.osm_node_ids,
                "osm_path": [list(p) for p in s.osm_path],
            }
            for s in (net.segments[k] for k in sorted(net.segments))
        ],
        "shared_road_m": [[a, b, m] for (a, b), m in sorted(net.shared.items())],
        "signal_programs": {str(k): v for k, v in sorted(net.programs.items())},
        "cameras": [
            {"camera_id": c.camera_id, "kind": c.kind, "label": c.label,
             "junction_id": c.junction_id, "segment_id": c.segment_id,
             "lon": c.lon, "lat": c.lat, "heading_deg": c.heading_deg}
            for c in net.cameras
        ],
        "stations": net.stations,
        "segment_meta_osm_base": base,
    }


def render_evidence(net: Network) -> str:
    return json.dumps(evidence_doc(net), indent=1, ensure_ascii=False, sort_keys=False) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# SQL writers
# ─────────────────────────────────────────────────────────────────────────────


def _sql_str(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _wkt_line(line: list[Point]) -> str:
    return "LINESTRING(" + ", ".join(f"{lon:.{COORD_DP}f} {lat:.{COORD_DP}f}"
                                    for lon, lat in line) + ")"


def _wkt_point(p: Point) -> str:
    return f"POINT({p[0]:.{COORD_DP}f} {p[1]:.{COORD_DP}f})"


def _fmt(v: float) -> str:
    return f"{v:.{COORD_DP}f}"


def _bool(v: bool) -> str:
    return "TRUE" if v else "FALSE"


def _json_lit(doc) -> str:
    return _sql_str(json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _sql_lines(text: str, indent: str, width: int = 76) -> str:
    """``text`` as adjacent string literals of about ``width`` characters, one
    per line — SQL concatenates literals that only a newline separates."""
    chunks: list[str] = []
    cur = ""
    for token in re.findall(r"\s+|\S+\s*", text):
        if cur and len(cur) + len(token) > width:
            chunks.append(cur)
            cur = ""
        cur += token
    chunks.append(cur)
    assert "".join(chunks) == text
    return ("\n" + indent).join(_sql_str(c) for c in chunks)


def placeholder_via(net: Network, jid: int) -> int:
    """A junction 16+ does not exist yet when the seed loads: its movements
    cite the NEAREST seeded junction (1-15) as a placeholder, which
    reanchor_osm_network.sql replaces with the junction itself."""
    if jid <= SEED_JUNCTIONS:
        return jid
    p = net.junctions[jid].point
    return min(range(1, SEED_JUNCTIONS + 1), key=lambda k: (dist_m(p, net.junctions[k].point), k))


def _segment_comment(net: Network, s: Segment) -> str:
    names = ", ".join(dict.fromkeys(n for n, _m in s.osm_names if n != "(unnamed)"))
    return (f"    -- {s.segment_id}: J{s.from_junction} -> "
            + (f"J{s.to_junction}" if s.to_junction else "dead end")
            + f" · {s.length_m:.0f} m · along {clean_text(names) or 'unnamed ways'}")


def render_seed(net: Network) -> str:
    out: list[str] = []
    w = out.append
    w("-- sample_corridor.sql — GENERATED by scripts/gen_osm_network.py; do not edit by hand.")
    w("--")
    w("-- Ahmedabad corridor seed: the 49 road segments (ids 1001-1049) of Veer's city")
    w("-- network, every one drawn along the REAL road of its name between real")
    w("-- intersections (OpenStreetMap), plus synthetic traffic observations.")
    w(f"-- Road geometry: {ATTRIBUTION}")
    w(f"--   Overpass extracts: {osm_base_line(net)} (osm_base).")
    w("--   The junction / segment mapping, every rename and the OSM evidence are")
    w("--   scripts/data/osm_network_map.json and scripts/data/osm_network.json.")
    w("-- Every traffic observation below is labelled data_quality = synthetic: they")
    w("-- are random numbers, not a feed.  Nothing here writes traffic.hourly_aggregates:")
    w("-- that table is REAL-only (migration 019) — the oracle rolls the synthetic")
    w("-- observations up into traffic.hourly_aggregates_synthetic, labelled.")
    w("--")
    w("-- Apply AFTER scripts/migrate.py, then reanchor_osm_network.sql (it completes")
    w("-- the network: junctions 16+, every movement at its own junction, names,")
    w("-- cameras, signal programs, stations), post_seed_derivations.sql and")
    w("-- display_geometry_osm.sql — scripts/veer_up.sh seed() does all four, in that")
    w("-- order.  The seed alone is still ONE connected network (the twin parses it:")
    w("-- traffic_oracle/simulation/network.py; so does scripts/impact_run.py).")
    w("")
    w("-- Junction ids 1-15 are the serial order of the INSERT below: start the")
    w("-- serial at 1 on an empty table (a rolled-back earlier attempt advances it).")
    w("SELECT setval(pg_get_serial_sequence('corridor.junctions', 'junction_id'), 1, false)")
    w("WHERE NOT EXISTS (SELECT 1 FROM corridor.junctions);")
    w("")
    w("-- ═══════════════════════════════════════════════════════════════════")
    w(f"-- ROAD SEGMENTS ({len(net.segments)} segments on OpenStreetMap roads)")
    w("-- ═══════════════════════════════════════════════════════════════════")
    w("")
    w("INSERT INTO corridor.road_segments (segment_id, road_name, road_class, lanes, "
      "speed_limit_kmh, oneway, geom, osm_way_id)")
    w("VALUES")
    ids = sorted(net.segments)
    for i, sid in enumerate(ids):
        s = net.segments[sid]
        w(_segment_comment(net, s))
        w(f"    ({sid}, {_sql_str(s.road_name)}, {_sql_str(s.road_class)}, {s.lanes}, "
          f"{s.speed_limit_kmh}, {_bool(s.oneway)},\n"
          f"     ST_GeomFromText({_sql_str(_wkt_line(s.line))}, 4326), {s.osm_way_id})"
          + ("," if i < len(ids) - 1 else ";"))
    w("")
    w("")
    w("-- ═══════════════════════════════════════════════════════════════════")
    w(f"-- JUNCTIONS 1-{SEED_JUNCTIONS} (serial order = junction_id on a fresh install;")
    w("-- reanchor_osm_network.sql names them and adds the rest)")
    w("-- ═══════════════════════════════════════════════════════════════════")
    w("")
    w("INSERT INTO corridor.junctions (junction_type, signal_control, geom)")
    w("VALUES")
    for jid in range(1, SEED_JUNCTIONS + 1):
        j = net.junctions[jid]
        w(f"    -- J{jid}: {j.name} (OSM node {j.osm_node_id})")
        w(f"    ({_sql_str(j.junction_type)}, {_bool(j.signal_control)}, "
          f"ST_GeomFromText('{_wkt_point(j.point)}', 4326))"
          + ("," if jid < SEED_JUNCTIONS else ";"))
    w("")
    w("")
    w("-- ═══════════════════════════════════════════════════════════════════")
    w("-- SEGMENT ADJACENCY (the line graph pgRouting routes on)")
    w("-- Every ordered pair of segments meeting at a junction, both directions,")
    w("-- turn cost by the post-seed C2 formula: roundabout 15 s, straight on")
    w("-- (deflection <= 30 deg) 3 s, signalised turn 12 s, other turn 10 s.  A")
    w("-- movement at a junction 16+ cites the NEAREST seeded junction as a")
    w("-- placeholder (junctions 16+ do not exist yet when this file loads):")
    w("-- reanchor_osm_network.sql writes it at its own junction.  One-way segments")
    w("-- are listed with their physical movements: the directed corridor.road_graph")
    w("-- view (012) enters them only at their start and leaves only at their end.")
    w("-- ═══════════════════════════════════════════════════════════════════")
    w("")
    w("INSERT INTO corridor.segment_adjacency (from_segment_id, to_segment_id, "
      "via_junction_id, turn_cost_sec)")
    w("VALUES")
    by_j: dict[int, list[Movement]] = {}
    for m in net.moves:
        by_j.setdefault(m.via, []).append(m)
    lines: list[str] = []
    for jid in sorted(by_j):
        lines.append(f"    -- via J{jid} {net.junctions[jid].name}"
                     + ("" if jid <= SEED_JUNCTIONS
                        else f" (placeholder J{placeholder_via(net, jid)})"))
        for m in by_j[jid]:
            lines.append(f"    ({m.from_id}, {m.to_id}, {placeholder_via(net, jid)}, "
                         f"{m.turn_cost_sec}),")
    last = max(i for i, ln in enumerate(lines) if not ln.lstrip().startswith("--"))
    lines[last] = lines[last][:-1] + ";"
    out.extend(lines)
    out.append(SEED_OBSERVATIONS)
    return "\n".join(out).rstrip("\n") + "\n"


SEED_OBSERVATIONS = """

-- ═══════════════════════════════════════════════════════════════════
-- TRAFFIC OBSERVATIONS (SYNTHETIC — random numbers, labelled as such)
-- Multiple observations per segment with varied congestion levels,
-- timestamped within the last 25 minutes of the seed load.  data_quality
-- is 'synthetic' and confidence <= 0.6: nothing here is a real feed (the
-- source column only admits feed names, so it carries a nominal one).
-- ═══════════════════════════════════════════════════════════════════

-- Create observations partition for current date if not exists
DO $$
DECLARE
    part_name TEXT;
    start_date DATE;
    end_date DATE;
BEGIN
    start_date := CURRENT_DATE;
    end_date := CURRENT_DATE + 1;
    part_name := 'observations_' || to_char(start_date, 'YYYY_MM_DD');
    IF NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'traffic' AND c.relname = part_name
    ) THEN
        EXECUTE format(
            'CREATE TABLE traffic.%I PARTITION OF traffic.observations FOR VALUES FROM (%L) TO (%L)',
            part_name, start_date, end_date
        );
    END IF;
END $$;

-- Insert synthetic traffic observations for every seeded segment
-- Each segment gets 5 observations spread over last 25 minutes
INSERT INTO traffic.observations (segment_id, timestamp_utc, speed_kmh, congestion_idx, source, geom, data_quality, confidence)
SELECT
    s.segment_id,
    NOW() - (interval '5 minutes' * gen.i),
    -- Speed varies by road class and time offset (simulates fluctuation)
    CASE s.road_class
        WHEN 'motorway' THEN 55 + (random() * 30)::real - (gen.i * 2)::real
        WHEN 'trunk' THEN 40 + (random() * 25)::real - (gen.i * 3)::real
        WHEN 'primary' THEN 30 + (random() * 20)::real - (gen.i * 2)::real
        WHEN 'secondary' THEN 25 + (random() * 15)::real - (gen.i * 1.5)::real
        ELSE 20 + (random() * 10)::real
    END,
    -- Congestion index: higher for older observations (simulates building congestion)
    LEAST(1.0, GREATEST(0.0,
        CASE s.road_class
            WHEN 'motorway' THEN 0.15 + (random() * 0.25)::real + (gen.i * 0.05)::real
            WHEN 'trunk' THEN 0.25 + (random() * 0.30)::real + (gen.i * 0.06)::real
            WHEN 'primary' THEN 0.35 + (random() * 0.30)::real + (gen.i * 0.04)::real
            WHEN 'secondary' THEN 0.30 + (random() * 0.25)::real + (gen.i * 0.03)::real
            ELSE 0.20 + (random() * 0.20)::real
        END
    )),
    (ARRAY['government', 'mapping_api', 'fleet_gps', 'crowdsource'])[1 + (random() * 3)::int],
    ST_LineInterpolatePoint(s.geom, random()),
    'synthetic',
    0.40 + (random() * 0.20)::real
FROM corridor.road_segments s
CROSS JOIN generate_series(0, 4) AS gen(i);

-- Insert additional peak-hour congestion observations for central Ahmedabad segments
INSERT INTO traffic.observations (segment_id, timestamp_utc, speed_kmh, congestion_idx, source, geom, data_quality, confidence)
SELECT
    s.segment_id,
    NOW() - (interval '2 minutes' * gen.i),
    -- Peak hour: slower speeds
    CASE s.road_class
        WHEN 'motorway' THEN 40 + (random() * 20)::real
        WHEN 'trunk' THEN 25 + (random() * 15)::real
        WHEN 'primary' THEN 15 + (random() * 12)::real
        WHEN 'secondary' THEN 12 + (random() * 10)::real
        ELSE 10 + (random() * 8)::real
    END,
    -- Higher congestion during peak
    LEAST(1.0, GREATEST(0.0,
        CASE s.road_class
            WHEN 'motorway' THEN 0.40 + (random() * 0.30)::real
            WHEN 'trunk' THEN 0.50 + (random() * 0.35)::real
            WHEN 'primary' THEN 0.55 + (random() * 0.35)::real
            WHEN 'secondary' THEN 0.45 + (random() * 0.30)::real
            ELSE 0.35 + (random() * 0.25)::real
        END
    )),
    (ARRAY['government', 'mapping_api', 'fleet_gps', 'crowdsource'])[1 + (random() * 3)::int],
    ST_LineInterpolatePoint(s.geom, random()),
    'synthetic',
    0.45 + (random() * 0.15)::real
FROM corridor.road_segments s
CROSS JOIN generate_series(0, 2) AS gen(i)
WHERE s.segment_id IN (1001, 1002, 1003, 1004, 1005, 1006, 1011, 1012, 1014, 1016, 1018, 1019);
"""


def render_reanchor(net: Network) -> str:
    """The live data migration: the store's network made exactly this one."""
    base = osm_base_line(net)
    out: list[str] = []
    w = out.append
    w("-- reanchor_osm_network.sql — GENERATED by scripts/gen_osm_network.py; do not edit by hand.")
    w("--")
    w("-- Veer's road network ON OpenStreetMap (WP-20): the idempotent step that makes")
    w("-- a store's network exactly the real one of sample_corridor.sql — the same")
    w("-- 49 segment ids and 34 junction ids, every segment along the real road of its")
    w("-- name between real intersections, every movement at its own junction.")
    w(f"-- Road geometry: {ATTRIBUTION}")
    w(f"--   Overpass extracts: {base} (osm_base).  Mapping and evidence:")
    w("--   scripts/data/osm_network_map.json, scripts/data/osm_network.json.")
    w("--")
    w("-- WHEN (scripts/veer_up.sh seed(), sha-stamped, AFTER scripts/migrate.py — it")
    w("-- needs migrations 012, 015 and 020 — and BEFORE post_seed_derivations.sql):")
    w("--   * fresh install: after sample_corridor.sql — completes the network")
    w("--     (junctions 16-34, names, the movements at junctions 16+, cameras,")
    w("--     signal programs, stations);")
    w("--   * an EXISTING store (the schematic network of waves 1-3): converts it in")
    w("--     place.  Every id is preserved — segment, junction, camera, station —")
    w("--     so events, routes, dispatches, sightings, recommendations and earmarks")
    w("--     are untouched and keep pointing at the same ids.  History keeps what")
    w("--     it recorded — observations, planned and event routes, decisions and")
    w("--     their audit trails — with one exception: an EMULATED camera read on a")
    w("--     segment this step moves keeps its place along that segment, on the")
    w("--     new line, and so does the same read in its run's recorded tape (0).")
    w("--     A live_anpr read is never moved.")
    w("--   * whenever the store is not on this network — veer_up.sh probes it")
    w("--     (read-only) against the line below, so a restored pre-conversion")
    w("--     backup is converted again whatever the run-dir stamp says:")
    w(f"-- network-probe: geom_source=osm segments={len(net.segments)} "
      f"junctions={len(net.junctions)}")
    w("-- Running it again is a no-op: every write is an upsert, update or delete")
    w("-- guarded by IS DISTINCT FROM (or by a line that differs), so a second apply")
    w("-- changes no row (not even updated_at).")
    w("-- ONE transaction: a failure leaves the store exactly as it was.")
    w("--")
    w("-- WHAT")
    w("--   0. the EMULATED camera reads (vehicle_sightings source 'emulated_anpr')")
    w("--      of a segment whose line changes: the same fraction along the")
    w("--      segment, on its new line — their pins, and the recommendations and")
    w("--      last-known vehicle positions drawn at them, sit on the real road —")
    w("--      and the same reads (by read id) in the runs' recorded tapes")
    w("--      (emulator.emulation_runs.tape, what a REPLAY draws): position only")
    w("--   A. segments 1001-1049: name, class, lanes, limit, one-way, geom (the OSM")
    w("--      path), osm_way_id, geom_source 'osm' + geom_meta (020); migration 015's")
    w("--      display line is cleared — the routing line IS the road now (C-K basis")
    w("--      'osm', served by the oracle's spatial router)")
    w("--   B. junctions 1-34 at their OSM nodes, named (EN + Gujarati), osm_node_id")
    w("--   C. segment_adjacency = exactly the generated movements")
    w("--   D. signal programs of the signalised junctions — the 006 derivation over")
    w("--      the real approaches; only 'osm_assumed' rows are rewritten (a")
    w("--      calibrated program is kept), an 'osm_assumed' program at a junction")
    w("--      that is not signalised is removed (none with history)")
    w("--   E. emulated camera SITES (EM-*): stop lines 25 m back on an approach,")
    w("--      bridge heads, the watchlist sweep cameras — other cameras untouched")
    w("--   F. agency stations: OpenStreetMap POIs and the labelled emulated posts")
    w("--   G. provenance (C-P): delete the hourly aggregates the original seed")
    w("--      FABRICATED into the REAL-only traffic.hourly_aggregates (019) — the")
    w("--      rows of the seed-load hour, and only where no data_quality = 'real'")
    w("--      observation of that segment exists in that hour")
    w("--   H. event-profile spines (events.event_profiles, migrations 008 / 010)")
    w("--      still at their seeded schematic value: the use-case library's route")
    w("--      on this network, a legal path (an operator's spine is kept)")
    w("--   I. event-profile text still at a seeded value the emulator does not")
    w("--      live up to: the watchlist sweep's description and template (010)")
    w("--      promised an earmarked-plate hit — GJ27EM4321 is an operator exercise")
    w("--      (an operator's text is kept)")
    w("--")
    w("-- Grammar: plain statements — no ';' inside string literals, no dollar")
    w("-- quoting, no block comments.")
    w("")
    w("BEGIN;")
    w("")
    w("-- =============================================================================")
    w("-- 0. THE NEW LINES, and the EMULATED camera reads kept on them — before A")
    w("-- moves the segments.  A read (corridor.vehicle_sightings) with source")
    w("-- 'emulated_anpr' attributed to a segment whose line changes keeps its place")
    w("-- ALONG that segment: ST_LineLocatePoint(old line, position) re-interpolated")
    w("-- on the new line.  Its segment, in this order:")
    w("--   1. the e-challan engine's attribution, the segment_id in its")
    w("--      recommendation's evidence (what an officer decides on)")
    w("--   2. the segment line the read already lies on (within 0.5 m — an")
    w("--      emulator read at a camera site on the network)")
    w("--   3. its camera's segment as registered BEFORE E re-sites the cameras")
    w("-- A read none of these attributes to a segment of this network stays where")
    w("-- it was recorded.  Only the position changes: ids, plate, time, speed,")
    w("-- heading and hit are kept, and no recommendation, decision or audit trail")
    w("-- is touched.  A live_anpr read is never moved.  A segment whose line is")
    w("-- already this one moves no read, so a second apply changes nothing.")
    w("-- =============================================================================")
    w("")
    w("CREATE TEMP TABLE _veer_osm_segments (")
    w("    segment_id BIGINT PRIMARY KEY, road_name TEXT, road_class TEXT, lanes SMALLINT,")
    w("    speed_limit_kmh SMALLINT, oneway BOOLEAN, geom geometry(LineString, 4326),")
    w("    osm_way_id BIGINT, geom_meta JSONB")
    w(") ON COMMIT DROP;")
    w("")
    w("INSERT INTO _veer_osm_segments (segment_id, road_name, road_class, lanes,")
    w("    speed_limit_kmh, oneway, geom, osm_way_id, geom_meta)")
    w("VALUES")
    ids = sorted(net.segments)
    for i, sid in enumerate(ids):
        s = net.segments[sid]
        w(_segment_comment(net, s))
        w(f"    ({sid}, {_sql_str(s.road_name)}, {_sql_str(s.road_class)}, {s.lanes}, "
          f"{s.speed_limit_kmh}, {_bool(s.oneway)},\n"
          f"     ST_GeomFromText({_sql_str(_wkt_line(s.line))}, 4326), {s.osm_way_id},\n"
          f"     {_json_lit(segment_meta(s, base))}::jsonb)"
          + ("," if i < len(ids) - 1 else ";"))
    w("")
    w("UPDATE corridor.vehicle_sightings AS v")
    w("SET position = ST_LineInterpolatePoint(n.geom, ST_LineLocatePoint(s.geom, v.position))")
    w("FROM (")
    w("    SELECT x.sighting_id, COALESCE(")
    w("        (SELECT ne.segment_id")
    w("         FROM events.enforcement_recommendations AS r")
    w("         JOIN _veer_osm_segments AS ne ON ne.segment_id::TEXT = r.evidence->>'segment_id'")
    w("         WHERE r.sighting_id = x.sighting_id")
    w("         ORDER BY r.created_at, r.recommendation_id")
    w("         LIMIT 1),")
    w("        (SELECT g.segment_id")
    w("         FROM corridor.road_segments AS g")
    w("         JOIN _veer_osm_segments AS ng ON ng.segment_id = g.segment_id")
    w("         WHERE g.geom && ST_Expand(x.position, 0.0001)")
    w("           AND ST_DWithin(g.geom::geography, x.position::geography, 0.5)")
    w("         ORDER BY g.segment_id IS DISTINCT FROM c.segment_id,")
    w("                  ST_Distance(g.geom::geography, x.position::geography), g.segment_id")
    w("         LIMIT 1),")
    w("        c.segment_id) AS segment_id")
    w("    FROM corridor.vehicle_sightings AS x")
    w("    LEFT JOIN corridor.cameras AS c ON c.camera_id = x.camera_id")
    w("    WHERE x.source = 'emulated_anpr'")
    w(") AS a")
    w("JOIN corridor.road_segments AS s ON s.segment_id = a.segment_id")
    w("JOIN _veer_osm_segments AS n ON n.segment_id = a.segment_id")
    w("WHERE v.sighting_id = a.sighting_id")
    w("  AND v.source = 'emulated_anpr'")
    w("  AND NOT ST_OrderingEquals(s.geom, n.geom);")
    w("")
    w("-- ... and the same reads in the runs' recorded tapes (emulator.emulation_runs:")
    w("-- the God's-eye REPLAY draws a tape's reads, a server-side replay re-posts")
    w("-- them as duplicates of the stored ones).  An anpr_read frame whose read_id")
    w("-- is the sighting id of an EMULATED read of the same camera takes that read's")
    w("-- position (7 decimals, ~1 cm).  Only payload.position changes — every other")
    w("-- frame, field and column is kept — and a frame already within 5 cm of its")
    w("-- read is left alone, so a second apply (or a store whose tapes already")
    w("-- match its reads) changes nothing.  A tape without reads is not rewritten.")
    w("UPDATE emulator.emulation_runs AS r")
    w("SET tape = jsonb_set(r.tape, '{frames}', m.frames)")
    w("FROM (")
    w("    SELECT t.run_id,")
    w("           jsonb_agg(CASE WHEN s.sighting_id IS NULL THEN f.frame")
    w("                          ELSE jsonb_set(f.frame, '{payload,position}',")
    w("                                         jsonb_build_array(round(ST_X(s.position)::NUMERIC, 7),")
    w("                                                           round(ST_Y(s.position)::NUMERIC, 7)))")
    w("                     END ORDER BY f.ord) AS frames,")
    w("           count(s.sighting_id) AS moved")
    w("    FROM emulator.emulation_runs AS t")
    w("    CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(t.tape->'frames') = 'array'")
    w("                                                 THEN t.tape->'frames' END)")
    w("                       WITH ORDINALITY AS f (frame, ord)")
    w("    CROSS JOIN LATERAL (")
    w("        SELECT CASE WHEN f.frame->>'kind' = 'anpr_read'")
    w("                     AND jsonb_typeof(f.frame->'payload'->'position'->0) = 'number'")
    w("                     AND jsonb_typeof(f.frame->'payload'->'position'->1) = 'number'")
    w("                    THEN ST_SetSRID(ST_MakePoint((f.frame->'payload'->'position'->>0)::FLOAT8,")
    w("                                                 (f.frame->'payload'->'position'->>1)::FLOAT8), 4326)")
    w("               END AS position")
    w("    ) AS tp")
    w("    LEFT JOIN corridor.vehicle_sightings AS s")
    w("      ON tp.position IS NOT NULL")
    w("     AND s.sighting_id::TEXT = f.frame->'payload'->>'read_id'")
    w("     AND s.camera_id = f.frame->'payload'->>'camera_id'")
    w("     AND s.source = 'emulated_anpr'")
    w("     AND NOT ST_DWithin(s.position::geography, tp.position::geography, 0.05)")
    w("    WHERE t.tape @? '$.frames[*] ? (@.kind == \"anpr_read\")'")
    w("    GROUP BY t.run_id")
    w(") AS m")
    w("WHERE r.run_id = m.run_id")
    w("  AND m.moved > 0;")
    w("")
    w("-- =============================================================================")
    w("-- A. SEGMENTS")
    w("-- =============================================================================")
    w("")
    w("INSERT INTO corridor.road_segments AS s (segment_id, road_name, road_class, lanes,")
    w("    speed_limit_kmh, oneway, geom, osm_way_id, geom_source, geom_meta)")
    w("SELECT segment_id, road_name, road_class, lanes, speed_limit_kmh, oneway, geom,")
    w("    osm_way_id, 'osm', geom_meta")
    w("FROM _veer_osm_segments")
    w("ON CONFLICT (segment_id) DO UPDATE SET")
    w("    road_name = EXCLUDED.road_name, road_class = EXCLUDED.road_class,")
    w("    lanes = EXCLUDED.lanes, speed_limit_kmh = EXCLUDED.speed_limit_kmh,")
    w("    oneway = EXCLUDED.oneway, geom = EXCLUDED.geom, osm_way_id = EXCLUDED.osm_way_id,")
    w("    geom_source = EXCLUDED.geom_source, geom_meta = EXCLUDED.geom_meta,")
    w("    display_geom = NULL, display_geom_source = NULL, display_geom_meta = NULL,")
    w("    updated_at = NOW()")
    w("WHERE s.road_name IS DISTINCT FROM EXCLUDED.road_name")
    w("   OR s.road_class IS DISTINCT FROM EXCLUDED.road_class")
    w("   OR s.lanes IS DISTINCT FROM EXCLUDED.lanes")
    w("   OR s.speed_limit_kmh IS DISTINCT FROM EXCLUDED.speed_limit_kmh")
    w("   OR s.oneway IS DISTINCT FROM EXCLUDED.oneway")
    w("   OR NOT ST_OrderingEquals(s.geom, EXCLUDED.geom)")
    w("   OR s.osm_way_id IS DISTINCT FROM EXCLUDED.osm_way_id")
    w("   OR s.geom_source IS DISTINCT FROM EXCLUDED.geom_source")
    w("   OR s.geom_meta IS DISTINCT FROM EXCLUDED.geom_meta")
    w("   OR s.display_geom IS NOT NULL OR s.display_geom_source IS NOT NULL")
    w("   OR s.display_geom_meta IS NOT NULL;")
    w("")
    w("-- =============================================================================")
    w("-- B. JUNCTIONS (explicit ids; the serial continues after the highest)")
    w("-- =============================================================================")
    w("")
    w("INSERT INTO corridor.junctions AS j (junction_id, junction_type, signal_control, geom,")
    w("    name, name_gu, osm_node_id)")
    w("VALUES")
    jids = sorted(net.junctions)
    for i, jid in enumerate(jids):
        j = net.junctions[jid]
        w(f"    ({jid}, {_sql_str(j.junction_type)}, {_bool(j.signal_control)}, "
          f"ST_GeomFromText('{_wkt_point(j.point)}', 4326),\n"
          f"     {_sql_str(j.name)}, {_sql_str(j.name_gu)}, {j.osm_node_id})"
          + ("," if i < len(jids) - 1 else ""))
    w("ON CONFLICT (junction_id) DO UPDATE SET")
    w("    junction_type = EXCLUDED.junction_type, signal_control = EXCLUDED.signal_control,")
    w("    geom = EXCLUDED.geom, name = EXCLUDED.name, name_gu = EXCLUDED.name_gu,")
    w("    osm_node_id = EXCLUDED.osm_node_id")
    w("WHERE j.junction_type IS DISTINCT FROM EXCLUDED.junction_type")
    w("   OR j.signal_control IS DISTINCT FROM EXCLUDED.signal_control")
    w("   OR NOT ST_OrderingEquals(j.geom, EXCLUDED.geom)")
    w("   OR j.name IS DISTINCT FROM EXCLUDED.name")
    w("   OR j.name_gu IS DISTINCT FROM EXCLUDED.name_gu")
    w("   OR j.osm_node_id IS DISTINCT FROM EXCLUDED.osm_node_id;")
    w("")
    w("SELECT setval(pg_get_serial_sequence('corridor.junctions', 'junction_id'),")
    w("              GREATEST((SELECT max(junction_id) FROM corridor.junctions), 1));")
    w("")
    w("-- =============================================================================")
    w("-- C. MOVEMENTS — segment_adjacency is exactly this set (turn costs: the")
    w("-- post-seed C2 formula over the real legs)")
    w("-- =============================================================================")
    w("")
    w("CREATE TEMP TABLE _veer_osm_moves (")
    w("    from_segment_id BIGINT, to_segment_id BIGINT, via_junction_id BIGINT,")
    w("    turn_cost_sec REAL")
    w(") ON COMMIT DROP;")
    w("")
    w("INSERT INTO _veer_osm_moves (from_segment_id, to_segment_id, via_junction_id, turn_cost_sec)")
    w("VALUES")
    rows = [f"    ({m.from_id}, {m.to_id}, {m.via}, {m.turn_cost_sec})" for m in net.moves]
    w(",\n".join(rows) + ";")
    w("")
    w("DELETE FROM corridor.segment_adjacency sa")
    w("WHERE NOT EXISTS (")
    w("    SELECT 1 FROM _veer_osm_moves m")
    w("    WHERE m.from_segment_id = sa.from_segment_id")
    w("      AND m.to_segment_id = sa.to_segment_id")
    w("      AND m.via_junction_id = sa.via_junction_id")
    w(");")
    w("")
    w("INSERT INTO corridor.segment_adjacency AS sa (from_segment_id, to_segment_id,")
    w("    via_junction_id, turn_cost_sec)")
    w("SELECT from_segment_id, to_segment_id, via_junction_id, turn_cost_sec FROM _veer_osm_moves")
    w("ON CONFLICT (from_segment_id, to_segment_id, via_junction_id) DO UPDATE SET")
    w("    turn_cost_sec = EXCLUDED.turn_cost_sec")
    w("WHERE sa.turn_cost_sec IS DISTINCT FROM EXCLUDED.turn_cost_sec;")
    w("")
    w("-- =============================================================================")
    w("-- D. SIGNAL PROGRAMS (source osm_assumed only — a calibrated row is kept)")
    w("-- =============================================================================")
    w("")
    w("INSERT INTO corridor.signal_programs AS p (junction_id, cycle_sec, phases, offset_sec, source)")
    w("VALUES")
    prows = []
    for jid in sorted(net.programs):
        prog = net.programs[jid]
        prows.append(f"    ({jid}, {prog['cycle_sec']}, "
                     f"{_sql_str(json.dumps(prog['phases'], separators=(', ', ': ')))}::jsonb, "
                     f"{prog['offset_sec']}, 'osm_assumed')")
    w(",\n".join(prows))
    w("ON CONFLICT (junction_id) DO UPDATE SET")
    w("    cycle_sec = EXCLUDED.cycle_sec, phases = EXCLUDED.phases,")
    w("    offset_sec = EXCLUDED.offset_sec, updated_at = NOW()")
    w("WHERE p.source = 'osm_assumed'")
    w("  AND (p.cycle_sec IS DISTINCT FROM EXCLUDED.cycle_sec")
    w("       OR p.phases IS DISTINCT FROM EXCLUDED.phases")
    w("       OR p.offset_sec IS DISTINCT FROM EXCLUDED.offset_sec);")
    w("")
    w("DELETE FROM corridor.signal_programs p")
    w("USING corridor.junctions j")
    w("WHERE j.junction_id = p.junction_id")
    w("  AND p.source = 'osm_assumed'")
    w("  AND NOT COALESCE(j.signal_control, FALSE)")
    w("  AND NOT EXISTS (SELECT 1 FROM corridor.signal_program_history h")
    w("                  WHERE h.junction_id = p.junction_id);")
    w("")
    w("-- =============================================================================")
    w("-- E. CAMERA SITES — source emulated (EMULATED badge); locations only, no")
    w("-- frames, no images (C6).  A stop-line site sits 25 m back along its")
    w("-- approach (further where a slip another segment shares runs there: up to")
    w("-- 38 m for ANPR, inside the e-challan engine's 40 m junction radius) and reads")
    w("-- traffic heading INTO the junction; a bridge-head site sits 15 % into the")
    w("-- bridge span from its landing, reading traffic heading onto the bridge; a")
    w("-- watchlist sweep camera (EM-CAM-0n) is the point ON its segment nearest the")
    w("-- segment's centroid (the emulator's anchor for it).  An operator's status")
    w("-- (offline, maintenance) is kept.")
    w("-- =============================================================================")
    w("")
    w("INSERT INTO corridor.cameras AS c (camera_id, kind, source, label, geom, junction_id,")
    w("    segment_id, heading_deg, status)")
    w("VALUES")
    crows = []
    for c in net.cameras:
        heading = "NULL" if c.heading_deg is None else f"{c.heading_deg:.1f}"
        crows.append(f"    ({_sql_str(c.camera_id)}, {_sql_str(c.kind)}, 'emulated', "
                     f"{_sql_str(c.label)},\n"
                     f"     ST_GeomFromText('{_wkt_point(c.point)}', 4326), "
                     f"{'NULL' if c.junction_id is None else c.junction_id}, {c.segment_id}, "
                     f"{heading}, 'active')")
    w(",\n".join(crows))
    w("ON CONFLICT (camera_id) DO UPDATE SET")
    w("    kind = EXCLUDED.kind, source = EXCLUDED.source, label = EXCLUDED.label,")
    w("    geom = EXCLUDED.geom, junction_id = EXCLUDED.junction_id,")
    w("    segment_id = EXCLUDED.segment_id, heading_deg = EXCLUDED.heading_deg,")
    w("    updated_at = NOW()")
    w("WHERE c.kind IS DISTINCT FROM EXCLUDED.kind")
    w("   OR c.source IS DISTINCT FROM EXCLUDED.source")
    w("   OR c.label IS DISTINCT FROM EXCLUDED.label")
    w("   OR NOT ST_OrderingEquals(c.geom, EXCLUDED.geom)")
    w("   OR c.junction_id IS DISTINCT FROM EXCLUDED.junction_id")
    w("   OR c.segment_id IS DISTINCT FROM EXCLUDED.segment_id")
    w("   OR c.heading_deg IS DISTINCT FROM EXCLUDED.heading_deg;")
    w("")
    w("-- =============================================================================")
    w("-- F. AGENCY STATIONS — source osm: positions from OpenStreetMap, osm_ref names")
    w("-- the object (the two hospital points tagged blood_bank are the blood-bank")
    w("-- nodes INSIDE the V. S. and L. G. hospital campuses, which carry no node of")
    w("-- their own); source emulated: Jan Rakshak / 108 / traffic posts placed by")
    w("-- this demo beside the real junction they serve, labelled (emulated) in both")
    w("-- languages.")
    w("-- =============================================================================")
    w("")
    w("INSERT INTO corridor.agency_stations AS st (station_id, agency, kind, name, name_gu, geom,")
    w("    source, osm_ref)")
    w("VALUES")
    srows = []
    for s in net.stations:
        ref = "NULL" if not s.get("osm_ref") else _sql_str(s["osm_ref"])
        srows.append(f"    ({_sql_str(s['station_id'])}, {_sql_str(s['agency'])}, "
                     f"{_sql_str(s['kind'])}, {_sql_str(s['name'])}, {_sql_str(s['name_gu'])},\n"
                     f"     ST_GeomFromText('{_wkt_point((s['lon'], s['lat']))}', 4326), "
                     f"{_sql_str(s['source'])}, {ref})")
    w(",\n".join(srows))
    w("ON CONFLICT (station_id) DO UPDATE SET")
    w("    agency = EXCLUDED.agency, kind = EXCLUDED.kind, name = EXCLUDED.name,")
    w("    name_gu = EXCLUDED.name_gu, geom = EXCLUDED.geom, source = EXCLUDED.source,")
    w("    osm_ref = EXCLUDED.osm_ref, updated_at = NOW()")
    w("WHERE st.agency IS DISTINCT FROM EXCLUDED.agency")
    w("   OR st.kind IS DISTINCT FROM EXCLUDED.kind")
    w("   OR st.name IS DISTINCT FROM EXCLUDED.name")
    w("   OR st.name_gu IS DISTINCT FROM EXCLUDED.name_gu")
    w("   OR NOT ST_OrderingEquals(st.geom, EXCLUDED.geom)")
    w("   OR st.source IS DISTINCT FROM EXCLUDED.source")
    w("   OR st.osm_ref IS DISTINCT FROM EXCLUDED.osm_ref;")
    w("")
    w("-- =============================================================================")
    w("-- G. PROVENANCE — the original seed wrote random numbers into the REAL-only")
    w("-- traffic.hourly_aggregates (migration 019), stamped date_trunc('hour', NOW())")
    w("-- of the seed load, which is also every seeded segment's created_at.  Only")
    w("-- that hour bucket is touched (the bucket starting within the hour before the")
    w("-- seed load, whatever the seed session's time zone), and a row survives when")
    w("-- a data_quality = 'real' observation of its segment exists in its hour.")
    w("-- =============================================================================")
    w("")
    w("DELETE FROM traffic.hourly_aggregates h")
    w("WHERE h.hour_utc <= (SELECT min(created_at) FROM corridor.road_segments)")
    w("  AND h.hour_utc > (SELECT min(created_at) FROM corridor.road_segments) - interval '1 hour'")
    w("  AND NOT EXISTS (")
    w("      SELECT 1 FROM traffic.observations o")
    w("      WHERE o.segment_id = h.segment_id")
    w("        AND o.data_quality = 'real'")
    w("        AND o.timestamp_utc >= h.hour_utc")
    w("        AND o.timestamp_utc < h.hour_utc + interval '1 hour'")
    w("  );")
    w("")
    w("-- =============================================================================")
    w("-- H. EVENT-PROFILE SPINES — events.event_profiles holds each use case's")
    w("-- representative route (migrations 008 / 010), drawn on the schematic")
    w("-- network: most are no longer paths on the real one.  A profile whose spine")
    w("-- is STILL the seeded value takes the route the use-case library runs on this")
    w("-- network (services/event-emulator/src/event_emulator/use_case_library.json,")
    w("-- which scripts/scenario_library.json agrees with) — every one a legal")
    w("-- directed path, the one-ways included (the generator checks).  A spine an")
    w("-- operator changed is kept.  Events already instantiated from a profile")
    w("-- (events.event_routes) are recorded history and are not rewritten.")
    if net.spines:
        w("-- Profiles already on their library route are not listed.")
        w("-- =============================================================================")
        w("")
        w("UPDATE events.event_profiles AS p")
        w("SET route_segment_ids = v.spine")
        w("FROM (VALUES")
        rows = [f"    ({_sql_str(ps.profile_id)}, "
                f"ARRAY[{', '.join(map(str, ps.seeded))}]::BIGINT[], "
                f"ARRAY[{', '.join(map(str, ps.spine))}]::BIGINT[])" for ps in net.spines]
        w(",\n".join(rows))
        w(") AS v (profile_id, seeded, spine)")
        w("WHERE p.profile_id = v.profile_id")
        w("  AND p.route_segment_ids = v.seeded")
        w("  AND p.route_segment_ids IS DISTINCT FROM v.spine;")
    else:
        w("-- Every seeded profile is already on its library route: nothing to do.")
        w("-- =============================================================================")
    w("")
    w("-- =============================================================================")
    w("-- I. EVENT-PROFILE TEXT — a profile column still at the value its migration")
    w("-- seeded, where that value promises what the emulator does not do, takes the")
    w("-- honest text: the watchlist sweep (010) promised an EARMARKED-plate hit, but")
    w("-- GJ27EM4321 is an operator exercise — the emulator never earmarks it, its reads")
    w("-- hit only after a duty officer has (the use-case library's wording, expected")
    w("-- feature operator_earmark_exercise).  Data hygiene: nothing reads these")
    w("-- columns at run time.  A column an operator changed is kept, and a second")
    w("-- apply changes nothing.")
    if net.texts:
        w("-- =============================================================================")
        for column in ("description", "template"):
            rows = [t for t in net.texts if t.column == column]
            if not rows:
                continue
            cast = "::jsonb" if column == "template" else ""
            w("")
            w("UPDATE events.event_profiles AS p")
            w(f"SET {column} = v.corrected")
            w("FROM (VALUES")
            w(",\n".join(f"    ({_sql_str(t.profile_id)},\n"
                         f"     {_sql_lines(t.seeded, '     ')}{cast},\n"
                         f"     {_sql_lines(t.corrected, '     ')}{cast})" for t in rows))
            w(") AS v (profile_id, seeded, corrected)")
            w("WHERE p.profile_id = v.profile_id")
            w(f"  AND p.{column} = v.seeded")
            w(f"  AND p.{column} IS DISTINCT FROM v.corrected;")
    else:
        w("-- Every corrected text already equals its seeded value: nothing to do.")
        w("-- =============================================================================")
    w("")
    w("COMMIT;")
    return "\n".join(out) + "\n"


def render_display(net: Network) -> str:
    """Migration 015's display file, superseded: nothing left to draw apart."""
    out: list[str] = []
    w = out.append
    w("-- display_geometry_osm.sql — GENERATED by scripts/gen_osm_network.py; do not edit by hand.")
    w("--")
    w("-- SUPERSEDED (WP-20).  Migration 015's display_geom drew a schematic segment")
    w("-- along OpenStreetMap while routing stayed on the hand-placed line.  The")
    w("-- routing geom IS the OpenStreetMap road now (reanchor_osm_network.sql, basis")
    w("-- 'osm', contract C-K), so no segment has a separate display line: this file")
    w("-- only clears one a store still carries (COALESCE(display_geom, geom) then")
    w("-- serves the real line).  The 015 columns stay — a future segment drawn")
    w("-- schematically could carry one again.")
    w(f"-- Road geometry: {ATTRIBUTION}")
    w(f"--   Overpass extracts: {osm_base_line(net)} (osm_base).")
    w("--")
    w("-- Apply AFTER reanchor_osm_network.sql and post_seed_derivations.sql, only when")
    w("-- the display_geom column exists (scripts/veer_up.sh seed() checks, applies,")
    w("-- sha-stamps).  Idempotent and guarded: a re-apply changes no row.  One")
    w("-- transaction.  Grammar: plain statements, no ';' inside string literals, no")
    w("-- dollar quoting.")
    w("")
    w("BEGIN;")
    w("")
    w("UPDATE corridor.road_segments")
    w("SET display_geom = NULL,")
    w("    display_geom_source = NULL,")
    w("    display_geom_meta = NULL")
    w("WHERE segment_id BETWEEN 1001 AND 1049")
    w("  AND (display_geom IS NOT NULL OR display_geom_source IS NOT NULL")
    w("       OR display_geom_meta IS NOT NULL);")
    w("")
    w("COMMIT;")
    return "\n".join(out) + "\n"


def render_all(net: Network) -> dict[Path, str]:
    return {
        SEED_SQL: render_seed(net),
        REANCHOR_SQL: render_reanchor(net),
        DISPLAY_SQL: render_display(net),
        NETWORK_JSON: render_evidence(net),
    }


# ─────────────────────────────────────────────────────────────────────────────
# --verify
# ─────────────────────────────────────────────────────────────────────────────

_SEG_ROW_RE = re.compile(
    r"\((\d{4}),\s*'([^']+)',\s*'(\w+)',\s*(\d+),\s*(\d+),\s*(TRUE|FALSE),\s*"
    r"ST_GeomFromText\('LINESTRING\(([^)]+)\)'"
)


def _sql_segments(text: str) -> dict[int, tuple]:
    out = {}
    for sid, name, cls, lanes, speed, ow, wkt in _SEG_ROW_RE.findall(text):
        line = [tuple(float(v) for v in p.split()) for p in wkt.split(",")]
        out[int(sid)] = (name, cls, int(lanes), int(speed), ow == "TRUE",
                         [(round(x, COORD_DP), round(y, COORD_DP)) for x, y in line])
    return out


#: the network step's header line scripts/veer_up.sh probes a store against
PROBE_RE = re.compile(r"^-- network-probe: geom_source=osm segments=(\d+) junctions=(\d+)$",
                      re.M)
_SPINE_ROW_RE = re.compile(
    r"\('([a-z0-9_]+)', ARRAY\[([0-9, ]+)\]::BIGINT\[\], ARRAY\[([0-9, ]+)\]::BIGINT\[\]\)")


_SPINE_UPDATE = "UPDATE events.event_profiles AS p\nSET route_segment_ids = v.spine"


def sql_profile_spines(text: str) -> list[ProfileSpine]:
    """The event-profile spines a network step (H) rewrites, in file order."""
    if _SPINE_UPDATE not in text:
        return []
    start = text.index(_SPINE_UPDATE)
    return [ProfileSpine(pid, tuple(int(x) for x in seeded.split(",")),
                         tuple(int(x) for x in spine.split(",")))
            for pid, seeded, spine in _SPINE_ROW_RE.findall(text[start:text.index(";", start)])]


def sql_profile_texts(text: str) -> list[ProfileText]:
    """The event-profile text corrections a network step (I) writes, in file
    order (literals concatenated, casts dropped)."""
    out: list[ProfileText] = []
    for column in ("description", "template"):
        head = f"UPDATE events.event_profiles AS p\nSET {column} = v.corrected"
        if head not in text:
            continue
        start = text.index(head)
        stmt = _no_sql_comments(text[start:text.index(";", start)])
        rows = stmt[stmt.index("FROM (VALUES") + len("FROM (VALUES"):stmt.index(") AS v (")]
        at = rows.find("(")
        while at >= 0:
            inner, end = _sql_row(rows, at)
            pid, seeded, corrected = (_sql_text(v) for v in _sql_values(inner))
            out.append(ProfileText(str(pid), column, str(seeded), str(corrected)))
            at = rows.find("(", end)
    return out


def _no_sql_comments(text: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def _text_key(t: ProfileText) -> tuple:
    """A correction compared by meaning: a template as parsed JSON."""
    if t.column == "template":
        return (t.profile_id, t.column, json.dumps(json.loads(t.seeded), sort_keys=True),
                json.dumps(json.loads(t.corrected), sort_keys=True))
    return (t.profile_id, t.column, t.seeded, t.corrected)


def evidence_edges(doc: dict) -> set[tuple[int, int]]:
    """corridor.road_graph from the evidence table alone: the movements (every
    ordered pair of segments ending at a common junction) under the 012 one-way
    rule of road_graph_edges."""
    segs = {s["segment_id"]: SimpleNamespace(segment_id=s["segment_id"], oneway=s["oneway"],
                                             from_junction=s["from_junction"],
                                             to_junction=s["to_junction"])
            for s in doc["segments"]}
    at: dict[int, list[int]] = {}
    for seg in segs.values():
        for jid, _start in segment_ends(seg):
            at.setdefault(jid, []).append(seg.segment_id)
    moves = [Movement(a, b, jid, 0) for jid, sids in sorted(at.items())
             for a in sids for b in sids if a != b]
    return set(road_graph_edges(segs, moves))


def verify(cache: Path | None) -> int:
    """Re-measure the committed evidence, cross-check the generated files and —
    with the cache — regenerate everything in memory (byte-identity)."""
    failures: list[str] = []
    doc = json.loads(NETWORK_JSON.read_text(encoding="utf-8"))
    junctions = {j["junction_id"]: j for j in doc["junctions"]}
    graph = None
    if cache is not None:
        for label, query in EXTRACTS:
            print(f"verify: {label} extract from {extract_source(cache, label, query)}",
                  file=sys.stderr)
        try:
            graph = load_graph(cache, fetch=False)
        except CacheMiss as exc:
            print(f"verify: {exc} — the junction-node and regeneration checks are NOT run",
                  file=sys.stderr)
    print(f"{'seg':>4} {'name':<44} {'ways':>4} {'name':<11} {'haus':>6} {'ends':>5} "
          f"{'len_m':>8} {'osm_m':>8} {'d%':>6}  first OSM way")
    for s in doc["segments"]:
        line = [tuple(p) for p in s["line"]]
        osm = [tuple(p) for p in s["osm_path"]]
        haus = hausdorff_m(line, osm)
        lm, om = length_m(line), length_m(osm)
        dlen = abs(lm - om) / om
        a = junctions[s["from_junction"]]
        ends_ok = tuple(line[0]) == (a["lon"], a["lat"]) and tuple(osm[0]) == (a["lon"], a["lat"])
        if s["to_junction"] is not None:
            b = junctions[s["to_junction"]]
            ends_ok = ends_ok and tuple(line[-1]) == (b["lon"], b["lat"])
        ends_ok = ends_ok and tuple(line[-1]) == tuple(osm[-1])
        print(f"{s['segment_id']:>4} {s['road_name'][:44]:<44} {len(s['osm_way_ids']):>4} "
              f"{s['name_match']:<11} {haus:6.2f} {'ok' if ends_ok else 'BAD':>5} "
              f"{lm:8.1f} {om:8.1f} {100 * dlen:6.2f}  way/{s['osm_way_ids'][0]}")
        if haus > HAUSDORFF_MAX_M:
            failures.append(f"{s['segment_id']}: Hausdorff {haus:.1f} m > {HAUSDORFF_MAX_M} m")
        if not ends_ok:
            failures.append(f"{s['segment_id']}: endpoints are not exactly its junction points")
        if dlen > LENGTH_TOL:
            failures.append(f"{s['segment_id']}: length {lm:.0f} m vs OSM {om:.0f} m (> 2 %)")
        if not s["osm_way_ids"] or s["osm_way_id"] not in s["osm_way_ids"]:
            failures.append(f"{s['segment_id']}: no OSM way ids")
        if s["name_match"] == NAME_UNNAMED and not (s["name_note"] or s["rename_reason"]):
            failures.append(f"{s['segment_id']}: name not on OSM and no note")
    print()
    print(f"{'J':>3} {'name':<52} {'osm node':>13} {'node_m':>7}  signals_m")
    for j in doc["junctions"]:
        node_m: float | None = None  # not measured without the extracts
        if graph is not None:
            node = graph.xy.get(j["osm_node_id"])
            node_m = dist_m(node, (j["lon"], j["lat"])) if node else math.inf
        shown = f"{node_m:7.2f}" if node_m is not None else f"{'n/a':>7}"
        print(f"{j['junction_id']:>3} {j['name'][:52]:<52} {j['osm_node_id']:>13} {shown}  "
              f"{j['osm_traffic_signals_m']}")
        if node_m is not None and node_m > JUNCTION_NODE_MAX_M:
            failures.append(f"J{j['junction_id']}: {node_m:.1f} m from OSM node {j['osm_node_id']}")
        if not any(j["junction_id"] in (s["from_junction"], s["to_junction"])
                   for s in doc["segments"]):
            failures.append(f"J{j['junction_id']}: no segment ends there")
    net_doc = doc["network"]
    print()
    print(f"network: {net_doc['segments']} segments, {net_doc['junctions']} junctions, "
          f"{net_doc['movements']} movements, {net_doc['road_graph_edges']} road_graph edges, "
          f"{net_doc['strong_components']} strongly connected component(s), "
          f"{net_doc['total_km']} km")
    if net_doc["strong_components"] != 1:
        failures.append(f"{net_doc['strong_components']} strongly connected components")
    failures += [f"network: {p}" for p in net_doc["problems"]]
    # the seed and the network step carry the evidence's network
    want = {s["segment_id"]: (s["road_name"], s["road_class"], s["lanes"], s["speed_limit_kmh"],
                              s["oneway"], [tuple(p) for p in s["line"]])
            for s in doc["segments"]}
    for path in (SEED_SQL, REANCHOR_SQL):
        got = _sql_segments(path.read_text(encoding="utf-8"))
        if got != want:
            bad = sorted(k for k in set(want) | set(got) if want.get(k) != got.get(k))
            failures.append(f"{path.name}: segments differ from the evidence table: {bad[:8]}")
    if "INSERT INTO traffic.hourly_aggregates" in SEED_SQL.read_text(encoding="utf-8"):
        failures.append("sample_corridor.sql writes the REAL-only traffic.hourly_aggregates")
    for path in (SEED_SQL, REANCHOR_SQL, DISPLAY_SQL):
        if "OpenStreetMap contributors, ODbL 1.0" not in path.read_text(encoding="utf-8")[:2000]:
            failures.append(f"{path.name}: no ODbL attribution in the header")
    # the line scripts/veer_up.sh probes a store against
    reanchor = REANCHOR_SQL.read_text(encoding="utf-8")
    probe = PROBE_RE.search(reanchor)
    if probe is None or (int(probe.group(1)), int(probe.group(2))) != (
            len(doc["segments"]), len(doc["junctions"])):
        failures.append(f"{REANCHOR_SQL.name}: no network-probe line stating the evidence's "
                        f"{len(doc['segments'])} segments and {len(doc['junctions'])} junctions")
    # the event-profile spines (H): the library's routes, legal on this network
    edges = evidence_edges(doc)
    if len(edges) != net_doc["road_graph_edges"]:
        failures.append(f"evidence road_graph: {len(edges)} edges derived, "
                        f"{net_doc['road_graph_edges']} recorded")
    spines, spine_issues = profile_spines({s["segment_id"] for s in doc["segments"]}, edges)
    failures += [f"profile spines: {p}" for p in spine_issues]
    if sql_profile_spines(reanchor) != spines:
        failures.append(f"{REANCHOR_SQL.name}: its event-profile spines (H) are not the seeded "
                        "values and library routes of today's migrations and library — regenerate")
    print(f"profile spines: {len(spines)} seeded spine(s) move to the library route — "
          + ("; ".join(f"{p.profile_id} {list(p.seeded)} -> {list(p.spine)}" for p in spines)
             or "none"))
    # the event-profile text corrections (I): from the values today's migrations seed
    texts, text_issues = profile_texts()
    failures += [f"profile texts: {p}" for p in text_issues]
    if [_text_key(t) for t in sql_profile_texts(reanchor)] != [_text_key(t) for t in texts]:
        failures.append(f"{REANCHOR_SQL.name}: its event-profile text corrections (I) do not start "
                        "from the values today's migrations seed — regenerate")
    print(f"profile texts: {len(texts)} seeded value(s) corrected — "
          + ("; ".join(f"{t.profile_id}.{t.column}" for t in texts) or "none"))
    if graph is not None:
        net = build_network(graph, load_mapping())
        for path, text in render_all(net).items():
            if path.read_text(encoding="utf-8") != text:
                failures.append(f"{path.relative_to(REPO_ROOT)}: differs from a regeneration "
                                "from the cache (stale or hand-edited)")
    print()
    if failures:
        print(f"FAIL ({len(failures)})")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: every segment on its OSM road (Hausdorff <= 15 m, ends at its junctions, "
          "length within 2 %), one strongly connected network, every profile spine a legal "
          "path, "
          + ("every junction on its OSM node, outputs byte-identical to a regeneration"
             if graph else "junction nodes and byte-identity NOT checked (no OSM extracts "
                           "loaded)"))
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE,
                    help=f"Overpass response cache (default {DEFAULT_CACHE}; the committed "
                         f"snapshot {SNAPSHOT_DIR.relative_to(REPO_ROOT)} when it misses)")
    ap.add_argument("--fetch", action="store_true",
                    help="request extracts missing from the cache (rate-limited)")
    ap.add_argument("--verify", action="store_true",
                    help="re-measure the committed evidence and outputs; exit 1 on a failure")
    ap.add_argument("--report", action="store_true",
                    help="print the per-segment trace report without writing files")
    args = ap.parse_args(argv)
    if args.verify:
        return verify(args.cache)
    extracts = load_extracts(args.cache, args.fetch)
    graph = build_graph([(label, doc) for label, _q, doc in extracts])
    net = build_network(graph, load_mapping())
    for s in (net.segments[k] for k in sorted(net.segments)):
        names = " > ".join(f"{n} {m:.0f}" for n, m in s.osm_names)
        print(f"{s.segment_id} {s.road_name} J{s.from_junction}->"
              f"{'J' + str(s.to_junction) if s.to_junction else 'end'} "
              f"{s.length_m:.0f} m {s.road_class}/{s.lanes}/{s.speed_limit_kmh}"
              f"{' oneway' if s.oneway else ''} {s.name_match} {s.name_overlap:.2f}: {names}",
              file=sys.stderr)
    for p in net.problems:
        print(f"PROBLEM: {p}", file=sys.stderr)
    if args.report:
        return 1 if net.problems else 0
    if net.problems:
        print(f"ERROR: {len(net.problems)} network problem(s) — nothing written", file=sys.stderr)
        return 1
    for path, text in render_all(net).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)}", file=sys.stderr)
    # the extracts these files were built from travel with them
    for label, query, doc in extracts:
        if write_snapshot(label, query, doc):
            print(f"wrote {snapshot_path(label).relative_to(REPO_ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
