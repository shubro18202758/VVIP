#!/usr/bin/env python3
"""poi_route_matrix.py — the 182-pair POI routing matrix, deterministically.

Fourteen Ahmedabad points of interest, every ordered pair (14 x 13 = 182),
through exactly what convoy-brain's route planner does BEFORE any model is
involved — and nothing after:

  1. SNAP   RoutePlannerAgent.resolve_endpoints: true point-to-polyline
            snapping over the oracle's bbox rows, snap pairs ranked by the
            network's own shortest-path cost, never one segment for both ends;
            a pair whose end lands more than MAX_SNAP_M from its point is
            NOT ok ("far_snap"): a route from a road kilometres away is no
            route from the place, however well it routes;
  2. ROUTE  find_convoy_routes over the best pairs (<= MAX_ROUTE_ATTEMPTS),
            falling through an isolated snap like the planner does;
  3. TIER   the planner's deterministic ranking (dedupe, heuristic weights)
            and its lane-floor verdict per candidate (route_tier compliant |
            conditional for the protection class, default Z+).

No model call is made (the planner's Gemma narration is never invoked —
RoutePlannerAgent is built without a bridge).  The brain's source is
imported READ-ONLY (``--brain-src``, default this checkout's
services/convoy-brain/src); the oracle is only queried (GET and the
read-only planning POSTs the planner itself makes).

It prints each POI's snap (the road it resolves to and how far it is), the
ok / same-segment / rejected / router-empty / far-snap counts, the
SVPI Airport -> Narendra Modi Stadium primary length, and writes
scripts/data/route_acceptance_pairs.json for the journey and showcase gates:

    compliant     the protected-movement acceptance pair (Z+ primary meets
                  the lane floor) — or null with the reason when none exists
    conditional   a Z+ pair whose best route cannot meet the lane floor (the
                  planner says "No fully compliant route:"), re-picked on this
                  network — or null with the reason when none exists
    escort_demo   the pair the live escort demo drives

Usage:
    python3 scripts/poi_route_matrix.py --oracle http://127.0.0.1:8117
        [--brain-src DIR] [--vvip-class Z+] [--out scripts/data/route_acceptance_pairs.json]
        [--no-write]
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BRAIN_SRC = REPO_ROOT / "services" / "convoy-brain" / "src"
DEFAULT_OUT = REPO_ROOT / "scripts" / "data" / "route_acceptance_pairs.json"

#: (name, (lon, lat)) — the deck planner rail's points of interest (ops
#: PlannerRail.svelte POIS), at their REAL places since the network runs on
#: OpenStreetMap (WP-20): most schematic-era points sat where the
#: schematic network drew them, kilometres from the place (Science City in
#: the east, "CTM Crossroads" at Kalupur — the real CTM Cross Roads lies
#: off the corridor network, so the station it stood beside is named).
#: Kankaria Lake sits at the lake-gate end of Kankaria Road (1038): the old
#: point lay 272 m off it, outside the planner's first snap box, where only
#: the SP Ring Road's bounding box reached — a 6.8 km "snap".
POIS: tuple[tuple[str, tuple[float, float]], ...] = (
    ("Sarkhej–Ashram South", (72.5714, 23.0225)),
    ("Lal Darwaja", (72.5813, 23.0264)),
    ("Ellis Bridge / CG Road", (72.562, 23.03)),
    ("Sabarmati Riverfront", (72.578, 23.035)),
    ("Sabarmati Ashram", (72.5802, 23.0607)),
    ("Narendra Modi Stadium", (72.5957, 23.0916)),
    ("Drive-In Road", (72.5293, 23.0464)),
    ("Prahlad Nagar", (72.512, 23.0065)),
    ("Satellite", (72.5191, 23.0279)),
    ("Kalupur Railway Station", (72.6005, 23.027)),
    ("Naroda", (72.6428, 23.0649)),
    ("Kankaria Lake", (72.5994, 23.0094)),
    ("SVPI Airport", (72.6221, 23.072)),
    ("Science City", (72.4978, 23.0813)),
)

#: The protected-movement acceptance journey (WP-14 / WP-15 gates).
ACCEPTANCE_PAIR = ("SVPI Airport", "Narendra Modi Stadium")

#: A trip end that resolves to a road farther than this from its point is no
#: snap at all (the planner's bbox can hand back only a long road whose
#: bounding box covers the point).  The widest legitimate one is Narendra
#: Modi Stadium's: its point lies ~550 m from Motera Stadium Road, across
#: the stadium's own grounds.
MAX_SNAP_M = 600.0


def import_brain(brain_src: Path):
    sys.path.insert(0, str(brain_src))
    from convoy_brain.agents.route_planner import (  # noqa: E402
        MAX_ROUTE_ATTEMPTS,
        RoutePlannerAgent,
        _shape_candidate,
        _snap_candidates,
    )
    from convoy_brain.agents.security_liaison import fetch_segment_facts  # noqa: E402
    from convoy_brain.llm.heuristics import (  # noqa: E402
        dedupe_route_candidates,
        heuristic_route_ranking,
    )
    from convoy_brain.mcp.server import ToolExecutor  # noqa: E402

    return {
        "MAX_ROUTE_ATTEMPTS": MAX_ROUTE_ATTEMPTS,
        "RoutePlannerAgent": RoutePlannerAgent,
        "_shape_candidate": _shape_candidate,
        "_snap_candidates": _snap_candidates,
        "fetch_segment_facts": fetch_segment_facts,
        "dedupe_route_candidates": dedupe_route_candidates,
        "heuristic_route_ranking": heuristic_route_ranking,
        "ToolExecutor": ToolExecutor,
    }


async def snap_pois(brain: dict, planner) -> dict[str, dict]:
    """Each POI's snap as the planner measures it: its nearest road, and the
    distance (m) of every equivalent candidate (the near-tie pool a pair's
    end is chosen from)."""
    out: dict[str, dict] = {}
    for name, point in POIS:
        cands = brain["_snap_candidates"](point, await planner._snap_rows(point), [])
        best = cands[0] if cands else None
        out[name] = {
            "segment_id": best.segment_id if best else None,
            "road_name": best.road_name if best else None,
            "snap_m": (round(best.distance_m, 1)
                       if best and math.isfinite(best.distance_m) else None),
            "candidates_m": {c.segment_id: c.distance_m for c in cands},
        }
    return out


def _far_ends(snaps: dict[str, dict], origin: str, dest: str, o: int | None, d: int | None) -> list[str]:
    """The pair's ends that land beyond MAX_SNAP_M (or at an unmeasured distance)."""
    far = []
    for role, name, seg in (("origin", origin, o), ("destination", dest, d)):
        snap = snaps[name]
        dist = snap["candidates_m"].get(seg) if seg is not None else snap["snap_m"]
        if dist is None or not math.isfinite(dist) or dist > MAX_SNAP_M:
            shown = f"{dist:.0f} m" if dist is not None and math.isfinite(dist) else "an unmeasured distance"
            far.append(f"{role} {name} resolves to segment {seg if seg is not None else snap['segment_id']} "
                       f"at {shown} (> {MAX_SNAP_M:.0f} m)")
    return far


async def plan_pair(brain: dict, planner, ex, facts_cache: dict, snaps: dict[str, dict],
                    origin_name: str, dest_name: str, vvip_class: str) -> dict:
    """SNAP -> ROUTE -> TIER for one ordered pair (the planner's deterministic half)."""
    lonlat = dict(POIS)
    far = _far_ends(snaps, origin_name, dest_name, None, None)
    if far:
        return {"status": "far_snap", "reason": "; ".join(far)}
    snap = await planner.resolve_endpoints(lonlat[origin_name], lonlat[dest_name], [])
    if snap.get("error"):
        return {"status": "rejected", "reason": str(snap["error"])[:160]}
    pairs = snap.get("pairs") or []
    if not pairs:
        return {"status": "rejected", "reason": "no snap pair"}
    if pairs[0][0] == pairs[0][1]:
        return {"status": "same", "reason": f"same segment {pairs[0][0]}"}
    tried = []
    for o, d in pairs[: brain["MAX_ROUTE_ATTEMPTS"]]:
        res = await ex.execute("find_convoy_routes", {
            "origin_segment": o, "destination_segment": d,
            "max_candidates": 5, "avoid_segments": [],
        })
        rows = res.get("routes", []) if isinstance(res, dict) else []
        rows = [r for r in rows if isinstance(r, dict) and r.get("segment_ids")]
        tried.append([o, d])
        if not rows:
            continue
        ranked = brain["heuristic_route_ranking"](brain["dedupe_route_candidates"](rows), {})
        ids = [s for r in ranked for s in r.get("segment_ids", [])]
        facts = await brain["fetch_segment_facts"](ex, ids, facts_cache)
        shaped = [brain["_shape_candidate"](r, facts, vvip_class) for r in ranked]
        shaped.sort(key=lambda r: 0 if r["route_tier"] == "compliant" else 1)  # the planner's order
        primary = shaped[0]
        far = _far_ends(snaps, origin_name, dest_name, o, d)
        if far:
            return {"status": "far_snap", "reason": "; ".join(far)}
        return {
            "status": "ok",
            "origin_segment": o,
            "destination_segment": d,
            "origin_snap_m": round(snaps[origin_name]["candidates_m"][o], 1),
            "destination_snap_m": round(snaps[dest_name]["candidates_m"][d], 1),
            "segment_ids": primary["segment_ids"],
            "road_names": primary["road_names"],
            "distance_m": primary["distance_m"],
            "route_tier": primary["route_tier"],
            "lane_violations": len(primary["lane_violations"]),
            "candidates": len(shaped),
        }
    return {"status": "empty", "reason": f"no route over pairs {tried}"}


def _pair_doc(origin: str, dest: str, row: dict) -> dict:
    lonlat = dict(POIS)
    return {
        "origin": {"name": origin, "lon": lonlat[origin][0], "lat": lonlat[origin][1],
                   "snap_m": row["origin_snap_m"]},
        "destination": {"name": dest, "lon": lonlat[dest][0], "lat": lonlat[dest][1],
                        "snap_m": row["destination_snap_m"]},
        "origin_segment": row["origin_segment"],
        "destination_segment": row["destination_segment"],
        "segment_ids": row["segment_ids"],
        "road_names": row["road_names"],
        "distance_m": row["distance_m"],
        "route_tier": row["route_tier"],
    }


def pick_pairs(matrix: dict[tuple[str, str], dict], vvip_class: str) -> dict:
    ok = {k: v for k, v in matrix.items() if v["status"] == "ok"}
    order = list(itertools.permutations([p for p, _ in POIS], 2))

    def by_length(keys):
        return sorted(keys, key=lambda k: (ok[k]["distance_m"] or 0.0, order.index(k)))

    acc = ok.get(ACCEPTANCE_PAIR)
    compliant_keys = [k for k in ok if ok[k]["route_tier"] == "compliant"]
    conditional_keys = [k for k in ok if ok[k]["route_tier"] == "conditional"]
    if acc and acc["route_tier"] == "compliant":
        compliant = _pair_doc(*ACCEPTANCE_PAIR, acc)
        compliant["why"] = "the protected-movement acceptance journey; its Z+ primary meets the lane floor"
    elif compliant_keys:
        k = by_length(compliant_keys)[0]
        compliant = _pair_doc(*k, ok[k])
        compliant["why"] = "shortest pair whose primary meets the lane floor"
        compliant_reason = None
    else:
        compliant = None
        compliant_reason = (
            f"no routed pair has a {vvip_class} primary that meets the lane floor on this "
            "network: every best route crosses a road whose lanes (OpenStreetMap, or the seed's "
            "value where OSM tags none) are below it — each plan is honestly conditional"
        )
    if conditional_keys:
        k = by_length(conditional_keys)[0]
        conditional = _pair_doc(*k, ok[k])
        conditional["why"] = (
            f"shortest pair whose best {vvip_class} route cannot meet the lane floor — the "
            "planner keeps it as conditional and says 'No fully compliant route:'"
        )
        conditional_reason = None
    else:
        conditional = None
        conditional_reason = f"every routed pair has a lane-floor-compliant {vvip_class} primary"
    escort = None
    if acc and acc["status"] == "ok":
        escort = _pair_doc(*ACCEPTANCE_PAIR, acc)
        escort["why"] = "the airport-to-stadium journey: short, one river crossing (Indira Bridge Road)"
    return {
        "compliant": compliant,
        **({"compliant_reason": compliant_reason} if compliant is None else {}),
        "conditional": conditional,
        **({"conditional_reason": conditional_reason} if conditional is None else {}),
        "escort_demo": escort,
    }


async def run(oracle: str, brain_src: Path, vvip_class: str) -> tuple[dict, dict, dict]:
    brain = import_brain(brain_src)
    ex = brain["ToolExecutor"](traffic_oracle_url=oracle)
    planner = brain["RoutePlannerAgent"](bridge=None, executor=ex)
    facts_cache: dict = {}
    matrix: dict[tuple[str, str], dict] = {}
    try:
        snaps = await snap_pois(brain, planner)
        for (a, _pa), (b, _pb) in itertools.permutations(POIS, 2):
            matrix[(a, b)] = await plan_pair(brain, planner, ex, facts_cache, snaps, a, b,
                                             vvip_class)
    finally:
        await ex.close()
    return matrix, pick_pairs(matrix, vvip_class), snaps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oracle", default=os.environ.get("ORACLE", "http://127.0.0.1:8091"))
    parser.add_argument("--brain-src", type=Path, default=DEFAULT_BRAIN_SRC)
    parser.add_argument("--vvip-class", default="Z+")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--no-write", action="store_true", help="print only")
    args = parser.parse_args()

    matrix, picks, snaps = asyncio.run(run(args.oracle, args.brain_src, args.vvip_class))
    print(f"POI snaps (the planner's nearest road; a pair end beyond {MAX_SNAP_M:.0f} m is not ok):")
    for name, _point in POIS:
        snap = snaps[name]
        shown = f"{snap['snap_m']:.0f} m" if snap["snap_m"] is not None else "unmeasured"
        flag = "" if snap["snap_m"] is not None and snap["snap_m"] <= MAX_SNAP_M else "  FAR"
        print(f"  {name:<26} -> {snap['segment_id']} {snap['road_name'] or '?'} at {shown}{flag}")
    counts = {s: sum(1 for v in matrix.values() if v["status"] == s)
              for s in ("ok", "same", "rejected", "empty", "far_snap")}
    tiers = {t: sum(1 for v in matrix.values() if v.get("route_tier") == t)
             for t in ("compliant", "conditional")}
    for (a, b), v in matrix.items():
        if v["status"] != "ok":
            print(f"  DEAD: {a} -> {b}: {v['status']} — {v.get('reason', '')}")
    acc = matrix.get(ACCEPTANCE_PAIR, {})
    print(f"pairs={len(matrix)} ok={counts['ok']} same_segment={counts['same']} "
          f"rejected={counts['rejected']} router_empty={counts['empty']} "
          f"far_snap={counts['far_snap']} "
          f"({args.vvip_class}: compliant={tiers['compliant']} conditional={tiers['conditional']})")
    if acc.get("status") == "ok":
        print(f"SVPI Airport -> Narendra Modi Stadium: {acc['distance_m'] / 1000:.2f} km over "
              f"{len(acc['segment_ids'])} segments {acc['segment_ids']} ({acc['route_tier']})")
    else:
        print(f"SVPI Airport -> Narendra Modi Stadium: {acc.get('status')} — {acc.get('reason')}")
    if not args.no_write:
        doc = {
            "generated_by": "scripts/poi_route_matrix.py",
            "attribution": "routes over Veer's road network on OpenStreetMap — "
                           "(c) OpenStreetMap contributors, ODbL 1.0",
            "vvip_class": args.vvip_class,
            "max_snap_m": MAX_SNAP_M,
            "matrix": {"pairs": len(matrix), **counts, **tiers},
            "poi_snaps": [
                {"name": name, "lon": point[0], "lat": point[1],
                 "segment_id": snaps[name]["segment_id"], "road_name": snaps[name]["road_name"],
                 "snap_m": snaps[name]["snap_m"]}
                for name, point in POIS
            ],
            **picks,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
