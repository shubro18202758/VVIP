"""Corridor network model for the twin — parsed from the frozen seed SQL.

The twin must run anywhere (tests, CLI export, host-mode service) without a
database, so the Ahmedabad seed network
(``services/corridor-store/seed/sample_corridor.sql``) is parsed directly with
the same regex approach the frozen impact harness uses
(``scripts/impact_run.py:68-78``).  Segment IDs therefore match exactly what
the seed, the DB, and ``impact_run.py`` agree on.

When the service runs with PostGIS available, :func:`from_rows` builds the
same structure from live ``corridor.road_segments`` rows.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

# Default location of the seed network relative to this package:
# src/traffic_oracle/simulation/ -> parents[5]=VVIP -> services/corridor-store/seed/
_SEED_RELATIVE = (
    Path(__file__).resolve().parents[5]
    / "services" / "corridor-store" / "seed" / "sample_corridor.sql"
)

_SEG_ROW_RE = re.compile(
    r"\((\d{4}),\s*'([^']+)',\s*'(\w+)',\s*(\d+),\s*(\d+),\s*(?:TRUE|FALSE),\s*"
    r"ST_GeomFromText\('LINESTRING\(([^)]+)\)'"
)
_JUNCTION_ROW_RE = re.compile(
    r"\('(intersection|roundabout|flyover|underpass|toll_plaza|signal)',\s*"
    r"(TRUE|FALSE),\s*ST_GeomFromText\('POINT\(([^)]+)\)'"
)
_ADJ_ROW_RE = re.compile(r"\((\d{4}),\s*(\d{4}),\s*(\d+),\s*(\d+)\)")

# Assumed peak-direction green split for signalized approaches, by road class.
# Road-class signal assumptions (labelled): cycle and split by dominant class.
_SIGNAL_ASSUMPTIONS: dict[str, dict] = {
    # cycle_sec, major green share, source: osm_assumed defaults in migration 006
    "motorway": {"cycle_sec": 90, "green_ratio": 0.48},
    "trunk": {"cycle_sec": 100, "green_ratio": 0.45},
    "primary": {"cycle_sec": 110, "green_ratio": 0.42},
    "secondary": {"cycle_sec": 120, "green_ratio": 0.40},
    "residential": {"cycle_sec": 120, "green_ratio": 0.36},
    "service": {"cycle_sec": 90, "green_ratio": 0.30},
}

# Base peak-hour demand per lane (veh/h/lane) by road class — calibrated so a
# class-TYPICAL signalized approach (class green ratio below) sits at v/c ≈
# 0.82 at the TOD peak (tod multiplier 1.0). Unsignalized/roundabout
# approaches of the same class then run below capacity, which is why they are
# unsignalized in the seed. Replay calibration (/api/v1/simulate/replay) fits
# a per-class scale against traffic.hourly_aggregates and marks the twin
# calibrated when MAE passes.
_BASE_DEMAND_VEH_PER_H_PER_LANE: dict[str, float] = {
    "motorway": 710.0,    # 1800 × 0.48 green × 0.82
    "trunk": 670.0,       # 1800 × 0.45 × 0.82
    "primary": 620.0,     # 1800 × 0.42 × 0.82
    "secondary": 590.0,   # 1800 × 0.40 × 0.82
    "residential": 430.0,
    "service": 300.0,
}

# Saturation flow per lane (veh/h/lane) — capacity side of v/c.
_SATURATION_FLOW_VEH_PER_H_PER_LANE = 1800.0

# Market-adjacent segments (old-city bazaar corridors in the seed): Relief,
# Gandhi, Swami Vivekananda, Kankaria.
_MARKET_SEGMENTS = frozenset({1005, 1012, 1013, 1038})
# Low-lying / underpass-prone segments: river-crossing corridor plus the
# Kalupur station approach (Station Road 1014) — labelled assumption:
# monsoon waterlogging concentrates here.
_LOW_LYING_SEGMENTS = frozenset({1001, 1002, 1006, 1007, 1015, 1014, 1039})
# Stadium-adjacent segment for match spillover.
_STADIUM_SEGMENTS = frozenset({1032})


@dataclass
class SegmentSpec:
    """One directed road segment of the corridor."""

    segment_id: int
    road_name: str
    road_class: str
    lanes: int
    speed_limit_kmh: int
    oneway: bool
    length_m: float
    # Populated by the network loader:
    signalized: bool = False
    green_ratio: float = 1.0
    cycle_sec: int = 0

    @property
    def base_peak_demand_veh_h(self) -> float:
        return _BASE_DEMAND_VEH_PER_H_PER_LANE.get(
            self.road_class, 500.0
        ) * self.lanes

    @property
    def capacity_veh_h(self) -> float:
        """Saturation capacity; signalized approaches lose their red time."""
        sat = _SATURATION_FLOW_VEH_PER_H_PER_LANE * self.lanes
        return sat * (self.green_ratio if self.signalized else 1.0)

    @property
    def free_flow_kmh(self) -> float:
        return float(self.speed_limit_kmh)

    @property
    def is_market_adjacent(self) -> bool:
        return self.segment_id in _MARKET_SEGMENTS

    @property
    def is_low_lying(self) -> bool:
        return self.segment_id in _LOW_LYING_SEGMENTS

    @property
    def is_stadium_adjacent(self) -> bool:
        return self.segment_id in _STADIUM_SEGMENTS

    def to_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "road_name": self.road_name,
            "road_class": self.road_class,
            "lanes": self.lanes,
            "speed_limit_kmh": self.speed_limit_kmh,
            "oneway": self.oneway,
            "length_m": round(self.length_m, 1),
            "signalized": self.signalized,
            "green_ratio": self.green_ratio,
            "cycle_sec": self.cycle_sec,
            "base_peak_demand_veh_h": round(self.base_peak_demand_veh_h, 1),
            "capacity_veh_h": round(self.capacity_veh_h, 1),
            "market_adjacent": self.is_market_adjacent,
            "low_lying": self.is_low_lying,
            "stadium_adjacent": self.is_stadium_adjacent,
        }


@dataclass
class SimNetwork:
    """The corridor network the twin simulates over."""

    segments: dict[int, SegmentSpec] = field(default_factory=dict)
    n_signalized_junctions: int = 0
    adjacency: dict[int, set[int]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.segments)

    def get(self, segment_id: int) -> SegmentSpec | None:
        return self.segments.get(segment_id)

    def require(self, segment_id: int) -> SegmentSpec:
        seg = self.segments.get(segment_id)
        if seg is None:
            raise KeyError(f"segment {segment_id} not in network")
        return seg

    def neighbors(self, segment_id: int) -> set[int]:
        return self.adjacency.get(segment_id, set())

    def subset(self, segment_ids: list[int]) -> SimNetwork:
        return SimNetwork(
            segments={sid: self.segments[sid] for sid in segment_ids if sid in self.segments},
            n_signalized_junctions=self.n_signalized_junctions,
            adjacency={
                k: (v & set(segment_ids))
                for k, v in self.adjacency.items()
                if k in segment_ids
            },
        )

    def to_dict(self) -> dict:
        return {
            "n_segments": len(self.segments),
            "n_signalized_junctions": self.n_signalized_junctions,
            "total_km": round(sum(s.length_m for s in self.segments.values()) / 1000.0, 2),
            "segments": [
                s.to_dict() for s in sorted(self.segments.values(), key=lambda x: x.segment_id)
            ],
        }


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def from_seed_sql(path: Path | str | None = None) -> SimNetwork:
    """Parse the frozen seed SQL into a :class:`SimNetwork`.

    Signalization is derived from the seed's junction table by matching each
    segment endpoint to a signal_control=TRUE junction point, then applying
    the road-class green-split assumptions (identical defaults to migration
    006's ``corridor.signal_programs`` seed).
    """
    seed_path = Path(path) if path else _SEED_RELATIVE
    sql = seed_path.read_text(encoding="utf-8")

    signal_points: list[tuple[float, float]] = []
    for _jtype, sig, pt in _JUNCTION_ROW_RE.findall(sql):
        if sig == "TRUE":
            lon, lat = map(float, pt.split())
            signal_points.append((lon, lat))

    network = SimNetwork(n_signalized_junctions=len(signal_points))
    for sid, name, rclass, lanes, speed, wkt in _SEG_ROW_RE.findall(sql):
        pts = [tuple(map(float, p.split())) for p in wkt.split(",")]
        length = sum(_haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        # Signalized if either endpoint snaps to a signal junction (≤25 m).
        hit = any(
            _haversine_m(endpoint, sp) <= 25.0
            for endpoint in (pts[0], pts[-1])
            for sp in signal_points
        )
        assume = _SIGNAL_ASSUMPTIONS.get(rclass, _SIGNAL_ASSUMPTIONS["secondary"])
        network.segments[int(sid)] = SegmentSpec(
            segment_id=int(sid),
            road_name=name,
            road_class=rclass,
            lanes=int(lanes),
            speed_limit_kmh=int(speed),
            oneway=False,  # regex above does not capture it; conservative default
            length_m=round(length, 1),
            signalized=hit,
            green_ratio=assume["green_ratio"] if hit else 1.0,
            cycle_sec=assume["cycle_sec"] if hit else 0,
        )

    # Undirected adjacency from the segment_adjacency INSERT block (4-int rows).
    valid_ids = set(network.segments)
    for a, b, _j, _cost in _ADJ_ROW_RE.findall(sql):
        fa, fb = int(a), int(b)
        if fa in valid_ids and fb in valid_ids:
            network.adjacency.setdefault(fa, set()).add(fb)
            network.adjacency.setdefault(fb, set()).add(fa)
    return network


def from_rows(rows: list[dict]) -> SimNetwork:
    """Build a network from live DB rows (``corridor.road_segments`` shape:
    segment_id, road_name, road_class, lanes, speed_limit_kmh, oneway,
    length_m, signalized?, green_ratio?, cycle_sec?)."""
    network = SimNetwork()
    for r in rows:
        rclass = str(r["road_class"])
        assume = _SIGNAL_ASSUMPTIONS.get(rclass, _SIGNAL_ASSUMPTIONS["secondary"])
        signalized = bool(r.get("signalized", False))
        network.segments[int(r["segment_id"])] = SegmentSpec(
            segment_id=int(r["segment_id"]),
            road_name=str(r.get("road_name") or ""),
            road_class=rclass,
            lanes=int(r.get("lanes") or 2),
            speed_limit_kmh=int(r.get("speed_limit_kmh") or 50),
            oneway=bool(r.get("oneway", False)),
            length_m=float(r.get("length_m") or 500.0),
            signalized=signalized,
            green_ratio=float(
                r.get("green_ratio") or (assume["green_ratio"] if signalized else 1.0)
            ),
            cycle_sec=int(
                r.get("cycle_sec") or (assume["cycle_sec"] if signalized else 0)
            ),
        )
        if signalized:
            network.n_signalized_junctions += 1
    return network


def default_network() -> SimNetwork:
    """Seed network, or a minimal fallback if the seed file is absent."""
    if _SEED_RELATIVE.exists():
        return from_seed_sql(_SEED_RELATIVE)
    # The seed's own values for the Ashram Road spine (OpenStreetMap, WP-20).
    return from_rows([
        {"segment_id": 1001, "road_name": "Ashram Road (South)", "road_class": "primary",
         "lanes": 4, "speed_limit_kmh": 60, "length_m": 462.0, "signalized": True},
        {"segment_id": 1002, "road_name": "Ashram Road (North)", "road_class": "primary",
         "lanes": 4, "speed_limit_kmh": 60, "length_m": 1565.0, "signalized": True},
        {"segment_id": 1009, "road_name": "Ashram Road (Sabarmati Ashram)", "road_class": "primary",
         "lanes": 4, "speed_limit_kmh": 40, "length_m": 2660.0, "signalized": True},
    ])
