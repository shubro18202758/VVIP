#!/usr/bin/env python3
"""gen_ingress_segments.py — signal-ingress Ahmedabad network codegen.

signal-ingress (Rust, NATS publisher) has no database connection: it runs on
the compose bridge network and must keep publishing when PostGIS is still
starting. Its edge-synthetic feeds therefore carry the seeded Ahmedabad road
network as a BUILD-TIME constant generated from corridor.road_segments:

    services/signal-ingress/src/feeds/segments_ahmedabad.rs

For every seeded segment the module records the identity and road attributes
(name, class, lanes, speed limit, one-way), the geodesic length, the midpoint
(ST_LineInterpolatePoint(geom, 0.5)), the full LINESTRING (so readings land
on the actual roadway) and the endpoint node ids (endpoints snapped to 1e-6
deg; a shared endpoint is one node). The generated text carries a content
fingerprint and no timestamp, so regeneration is byte-reproducible.

The database is only ever READ: the psql session sets
default_transaction_read_only and the psycopg path opens a read-only
connection — the script cannot write even if the SQL were wrong.

Usage (from anywhere; paths resolve from the repo root):
    python3 scripts/gen_ingress_segments.py            # via docker exec psql
    python3 scripts/gen_ingress_segments.py --dsn "$VVIP_DATABASE_URL"
    python3 scripts/gen_ingress_segments.py --check    # exit 1 on drift

``--dsn`` needs psycopg: an interpreter without it re-runs the script under
the repo's migrate venv (the one scripts/migrate.py uses) when one exists.

Re-run (and rebuild signal-ingress) whenever the corridor network changes,
e.g. after scripts/gen_osm_network.py regenerates the seed and the network
step (services/corridor-store/seed/reanchor_osm_network.sql) and that step is
applied.  The table carries geom — the routing geometry every attribution
uses, since WP-20 the OpenStreetMap road itself ((c) OpenStreetMap
contributors, ODbL 1.0, stated in the generated header) — never a 015
display_geom a map may draw (contract C-K).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = (REPO_ROOT / "services" / "signal-ingress" / "src" / "feeds"
          / "segments_ahmedabad.rs")

# Repo constants (infra/compose.argus-override.yml) — identity, not secrets.
DEFAULT_CONTAINER = os.environ.get("POSTGIS_CONTAINER", "vvip-argus-postgis")
PG_USER = "corridor_admin"
PG_DB = "corridor_db"

# Single read-only query; the whole result is one JSON document so no text
# parsing of psql output is needed.
QUERY = """
SELECT json_build_object(
  'segments', (
    SELECT json_agg(json_build_object(
        'id', segment_id,
        'name', coalesce(road_name, ''),
        'road_class', road_class,
        'lanes', coalesce(lanes, 2),
        'speed_limit_kmh', coalesce(speed_limit_kmh, 50),
        'oneway', coalesce(oneway, false),
        'length_m', round(ST_Length(geom::geography)::numeric, 1),
        'mid', json_build_array(
            round(ST_X(ST_LineInterpolatePoint(geom, 0.5))::numeric, 7),
            round(ST_Y(ST_LineInterpolatePoint(geom, 0.5))::numeric, 7)),
        'path', (ST_AsGeoJSON(geom, 7)::json) -> 'coordinates'
      ) ORDER BY segment_id)
    FROM corridor.road_segments),
  'extent', (
    SELECT json_build_array(ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e))
    FROM (SELECT ST_Extent(geom) AS e FROM corridor.road_segments) x)
)
"""

ROAD_CLASSES = {
    "motorway": "Motorway",
    "trunk": "Trunk",
    "primary": "Primary",
    "secondary": "Secondary",
    "tertiary": "Tertiary",
    "residential": "Residential",
    "service": "Service",
}

NODE_SNAP_DP = 6  # 1e-6 deg ≈ 0.1 m: endpoints closer than this are one node


def fetch_via_container(container: str) -> dict:
    cmd = [
        "docker", "exec", "-i", container,
        "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
        "-U", PG_USER, "-d", PG_DB,
        "-c", "SET default_transaction_read_only = on",
        "-c", QUERY,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"ERROR: psql via {container} failed (exit {proc.returncode}):\n"
                         f"{proc.stderr.strip()}")
    return json.loads(proc.stdout.strip())


#: Interpreters with psycopg, in the order scripts/veer_up.sh looks for them.
MIGRATE_PYTHONS = (
    os.environ.get("VEER_MIGRATE_PY", ""),
    os.path.join(os.environ.get("VEER_VENV_DIR", ""), "migrate", "bin", "python")
    if os.environ.get("VEER_VENV_DIR") else "",
    str(REPO_ROOT / ".venvs" / "migrate" / "bin" / "python"),
    str(REPO_ROOT / "models" / ".venvs" / "migrate" / "bin" / "python"),
)


def _reexec_with_psycopg() -> None:
    """Re-run this script under a venv that has psycopg (never loops)."""
    if os.environ.get("VEER_GEN_INGRESS_REEXEC"):
        return
    for py in MIGRATE_PYTHONS:
        if py and os.path.isfile(py) and os.access(py, os.X_OK):
            os.environ["VEER_GEN_INGRESS_REEXEC"] = "1"
            os.execv(py, [py, os.path.abspath(__file__), *sys.argv[1:]])


def fetch_via_dsn(dsn: str) -> dict:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - operational guard
        _reexec_with_psycopg()
        raise SystemExit("ERROR: --dsn needs psycopg (use the docker exec path instead)") from exc
    with psycopg.connect(dsn) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(QUERY)
            row = cur.fetchone()
    doc = row[0]
    return doc if isinstance(doc, dict) else json.loads(doc)


def rust_f64(value: float) -> str:
    text = f"{float(value):.7f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def rust_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def canonical(doc: dict) -> list[dict]:
    """Validated, normalised rows (raises on anything the feeds cannot use)."""
    rows = doc.get("segments") or []
    if not rows:
        raise SystemExit("ERROR: corridor.road_segments is empty — seed the corridor first")
    out = []
    for r in rows:
        path = [(round(float(x), 7), round(float(y), 7)) for x, y in r["path"]]
        if len(path) < 2:
            raise SystemExit(f"ERROR: segment {r['id']} has a degenerate geometry")
        if r["road_class"] not in ROAD_CLASSES:
            raise SystemExit(f"ERROR: segment {r['id']} has unknown road_class {r['road_class']!r}")
        out.append({
            "id": int(r["id"]),
            "name": str(r["name"]),
            "road_class": str(r["road_class"]),
            "lanes": int(r["lanes"]),
            "speed_limit_kmh": int(r["speed_limit_kmh"]),
            "oneway": bool(r["oneway"]),
            "length_m": float(r["length_m"]),
            "mid": (round(float(r["mid"][0]), 7), round(float(r["mid"][1]), 7)),
            "path": path,
        })
    return out


def assign_nodes(rows: list[dict]) -> tuple[list[tuple[int, int]], int]:
    nodes: dict[tuple[float, float], int] = {}

    def node_of(pt: tuple[float, float]) -> int:
        key = (round(pt[0], NODE_SNAP_DP), round(pt[1], NODE_SNAP_DP))
        if key not in nodes:
            nodes[key] = len(nodes)
        return nodes[key]

    ends = [(node_of(r["path"][0]), node_of(r["path"][-1])) for r in rows]
    return ends, len(nodes)


def render(doc: dict) -> str:
    rows = canonical(doc)
    ends, node_count = assign_nodes(rows)
    fingerprint = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in doc["extent"])
    ids = [r["id"] for r in rows]

    out: list[str] = []
    w = out.append
    w("//! Ahmedabad corridor road network — GENERATED, DO NOT EDIT BY HAND.")
    w("//!")
    w(f"//! Source: corridor.road_segments ({len(rows)} rows, ids {ids[0]}-{ids[-1]}) of the")
    w("//! corridor-store seed, read-only. Regenerate with")
    w("//!     python3 scripts/gen_ingress_segments.py")
    w("//! and check for drift against the live store with `--check`.")
    w("//!")
    w("//! `mid` is ST_LineInterpolatePoint(geom, 0.5), `length_m` is the geodesic")
    w("//! (geography) length, `path` is the full LINESTRING in WGS84 lon/lat, and")
    w("//! `start_node`/`end_node` are the endpoints snapped to 1e-6 deg (a shared")
    w("//! endpoint is one node).")
    w("//!")
    w("//! Road geometry: (c) OpenStreetMap contributors, ODbL 1.0 —")
    w("//! https://www.openstreetmap.org/copyright (the network is traced on")
    w("//! OpenStreetMap by scripts/gen_osm_network.py).")
    w("")
    w("use super::city::{RoadClass, Segment};")
    w("")
    w("/// sha256 prefix of the canonical JSON of the rows below.")
    w(f'pub const SOURCE_FINGERPRINT: &str = "{fingerprint}";')
    w("")
    w("/// Network envelope (min_lon, min_lat, max_lon, max_lat) = ST_Extent(geom).")
    w(f"pub const NETWORK_BBOX: (f64, f64, f64, f64) = ({rust_f64(min_lon)}, {rust_f64(min_lat)}, "
      f"{rust_f64(max_lon)}, {rust_f64(max_lat)});")
    w("")
    w("/// Number of distinct endpoint nodes referenced by `start_node`/`end_node`.")
    w(f"pub const NODE_COUNT: usize = {node_count};")
    w("")
    w("pub static SEGMENTS: &[Segment] = &[")
    for r, (start, end) in zip(rows, ends):
        path = ", ".join(f"({rust_f64(x)}, {rust_f64(y)})" for x, y in r["path"])
        w("    Segment {")
        w(f"        id: {r['id']},")
        w(f"        name: {rust_str(r['name'])},")
        w(f"        road_class: RoadClass::{ROAD_CLASSES[r['road_class']]},")
        w(f"        lanes: {r['lanes']},")
        w(f"        speed_limit_kmh: {rust_f64(r['speed_limit_kmh'])},")
        w(f"        oneway: {'true' if r['oneway'] else 'false'},")
        w(f"        length_m: {rust_f64(r['length_m'])},")
        w(f"        mid: ({rust_f64(r['mid'][0])}, {rust_f64(r['mid'][1])}),")
        w(f"        path: &[{path}],")
        w(f"        start_node: {start},")
        w(f"        end_node: {end},")
        w("    },")
    w("];")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--container", default=DEFAULT_CONTAINER,
                        help=f"PostGIS container for docker exec psql (default {DEFAULT_CONTAINER})")
    parser.add_argument("--dsn", default=None,
                        help="read via psycopg instead of docker exec (read-only connection)")
    parser.add_argument("--out", default=str(TARGET), help="output .rs path")
    parser.add_argument("--check", action="store_true",
                        help="regenerate in memory; exit 1 if the checked-in file differs")
    args = parser.parse_args()

    doc = fetch_via_dsn(args.dsn) if args.dsn else fetch_via_container(args.container)
    text = render(doc)
    out = Path(args.out)
    if args.check:
        current = out.read_text() if out.is_file() else ""
        if current != text:
            print(f"DRIFT: {out} does not match corridor.road_segments — regenerate it",
                  file=sys.stderr)
            return 1
        print(f"OK: {out} matches corridor.road_segments")
        return 0
    out.write_text(text)
    n = text.count("    Segment {")
    print(f"wrote {out} ({n} segments)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
