#!/usr/bin/env python3
"""Impact run — quantifies commuter and emergency-patient benefit of rolling
corridor holds vs today's blanket freeze, on Veer's own Ahmedabad road
network (49 segments on real OpenStreetMap roads, WP-20).

Honesty contract (read before quoting any number from the report):
- This is a DETERMINISTIC ANALYTICAL MODEL on the platform's road network,
  not a field measurement. Every constant is either sourced (column
  "source") or an explicit labelled assumption.
- The mechanism it models is real and conservative: delay scales with the
  SQUARE of hold duration (deterministic queueing), so shrinking a 25-minute
  blanket hold to a 2.5-minute rolling hold removes ~99% of junction delay
  even before spillback effects.
- To falsify or improve: replace the assumption constants with RTI'd
  movement counts and junction arrival rates and re-run
  (`python3 scripts/impact_run.py`). The report regenerates deterministically
  from the same network.

The network under test is read from a RUNNING stack — the traffic-oracle's
own reads, the ones every planner and the God's-eye canvas use:
    GET {oracle}/api/v1/spatial/segments?min_lon&min_lat&max_lon&max_lat
        the 49 segments (id, name, class, lanes, speed limit, geometry)
    GET {oracle}/api/v1/city/entities?kinds=junction
        the junctions
{oracle} is --oracle, else $ORACLE_URL, else http://127.0.0.1:${ORACLE_PORT:-8091}
(the live stack's loopback oracle; point it at a sandbox to re-run there).
--offline reads the committed generator output instead
(scripts/data/osm_network.json, written by scripts/gen_osm_network.py) —
the same network, with no stack needed. Segment lengths are great-circle
(haversine) sums over the served geometry in both modes.

Road geometry (c) OpenStreetMap contributors, ODbL 1.0.

Stdlib only. Writes IMPACT_REPORT.md and scripts/impact_results.json.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SEED = REPO / "services" / "corridor-store" / "seed" / "sample_corridor.sql"
OFFLINE_NETWORK = REPO / "scripts" / "data" / "osm_network.json"
ATTRIBUTION = "(c) OpenStreetMap contributors, ODbL 1.0 — https://www.openstreetmap.org/copyright"
#: The city's road-network extent (the brain's CITY_BBOX / the deck's NETWORK_BBOX).
CITY_BBOX = (72.40, 22.90, 72.80, 23.20)
HTTP_TIMEOUT_SEC = 15

# ── Model constants ────────────────────────────────────────────────────────
# Sourced from this repo's own simulator (evaluate/scenario_sim.py defaults):
DENSITY_VEH_PER_KM_PER_LANE = 30.0     # source: scenario_sim.py default
# Effective peak approach speed used to turn density into arrivals
# (km/h, capped by each junction's approach speed limits):
EFFECTIVE_APPROACH_KMH = 30.0           # assumption: urban peak congestion
# Hold durations (minutes):
BASELINE_HOLD_MIN = 25.0                # source: documented 15–45 min blanket holds
BASELINE_HOLD_LOW, BASELINE_HOLD_HIGH = 15.0, 45.0   # sensitivity band
PLATFORM_HOLD_MIN = 2.5                 # source: PROBLEM_STATEMENT (rolling 2–3 min)
PLATFORM_HOLD_CONSERVATIVE = 3.0        # longer rolling hold = platform's worse case
PLATFORM_HOLD_OPTIMISTIC = 2.0          # shorter rolling hold = platform's better case
# Discharge factor: queue takes roughly half the hold again to clear:
DISCHARGE_FACTOR = 1.5                  # assumption (textbook over-saturation shape)
# Ambulance exposure per movement-day (assumption; see report for basis):
AMB_TRIPS_CROSSING_PER_MOVEMENT = 12.0
AMB_BASELINE_DELAY_MIN = 12.0           # source: documented 5–20 min holds (midpoint)
AMB_PLATFORM_DELAY_MIN = 1.0
# Annualization (assumption; RTI-able per state/city):
MOVEMENT_DAYS_PER_YEAR = 250
# Fuel/CO2 (source: standard idle-consumption and emission factors):
IDLE_LITERS_PER_HOUR = 0.9              # mixed-fleet idle consumption
IDLING_SHARE_OF_DELAY = 0.6             # assumption: fraction of delay spent idling
CO2_KG_PER_LITER = 2.5                  # mixed petrol/diesel


def haversine_m(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def line_length_m(coords) -> float:
    pts = [(float(p[0]), float(p[1])) for p in coords]
    return sum(haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def parse_seed(path: Path):
    """The seed SQL's segments and its junction-row count — the frozen harness
    parse the twin's own seed parse is tested against (traffic-oracle
    tests/test_simulation.py TestNetwork).  The report itself reads the network
    from a running stack or the committed generator output (below): the seed
    file holds the network's first junction rows only, the rest arrive with
    reanchor_osm_network.sql."""
    sql = path.read_text(encoding="utf-8")
    seg_rows = re.findall(
        r"\((\d{4}),\s*'([^']+)',\s*'(\w+)',\s*(\d+),\s*(\d+),\s*(?:TRUE|FALSE),\s*"
        r"ST_GeomFromText\('LINESTRING\(([^)]+)\)'", sql)
    segments = []
    for sid, name, rclass, lanes, speed, wkt in seg_rows:
        pts = [tuple(map(float, p.split())) for p in wkt.split(",")]
        length = sum(haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        segments.append({"id": int(sid), "name": name, "class": rclass,
                         "lanes": int(lanes), "speed_kmh": int(speed),
                         "length_m": round(length, 1)})
    n_junctions = len(re.findall(r"ST_GeomFromText\('POINT\(", sql))
    return segments, n_junctions


def default_oracle() -> str:
    url = os.environ.get("ORACLE_URL", "").strip()
    if url:
        return url.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('ORACLE_PORT', '8091').strip() or '8091'}"


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SEC) as resp:  # noqa: S310 - fixed loopback/config URL
        return json.loads(resp.read().decode("utf-8"))


def network_from_stack(oracle: str):
    """The segments and junctions the running oracle serves."""
    lo_lon, lo_lat, hi_lon, hi_lat = CITY_BBOX
    seg_doc = _get_json(f"{oracle}/api/v1/spatial/segments?min_lon={lo_lon}&min_lat={lo_lat}"
                        f"&max_lon={hi_lon}&max_lat={hi_lat}")
    rows = seg_doc.get("segments", seg_doc) if isinstance(seg_doc, dict) else seg_doc
    segments = []
    for r in rows:
        geom = (r.get("geometry") or {}).get("coordinates") or []
        segments.append({"id": int(r["segment_id"]), "name": r["road_name"], "class": r["road_class"],
                         "lanes": int(r["lanes"]), "speed_kmh": int(r["speed_limit_kmh"]),
                         "length_m": round(line_length_m(geom), 1)})
    jdoc = _get_json(f"{oracle}/api/v1/city/entities?kinds=junction")
    n_junctions = len([e for e in jdoc.get("entities", []) if e.get("kind") == "junction"])
    return sorted(segments, key=lambda s: s["id"]), n_junctions


def network_offline(path: Path):
    """The committed generator output (scripts/gen_osm_network.py)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    segments = [{"id": int(s["segment_id"]), "name": s["road_name"], "class": s["road_class"],
                 "lanes": int(s["lanes"]), "speed_kmh": int(s["speed_limit_kmh"]),
                 "length_m": round(float(s["length_m"]), 1)}
                for s in doc["segments"]]
    return sorted(segments, key=lambda s: s["id"]), len(doc["junctions"])


def commuter_delay_min(hold_min, lambda_per_min, n_junctions):
    """Deterministic-queue commuter delay: arrivals λ during hold H queue up;
    average delay per arrival ≈ H/2 (plus linear discharge), total = λH²/2."""
    return DISCHARGE_FACTOR * lambda_per_min * n_junctions * (hold_min ** 2) / 2.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oracle", default=default_oracle(),
                        help="traffic-oracle base URL (default $ORACLE_URL, else :${ORACLE_PORT:-8091})")
    parser.add_argument("--offline", action="store_true",
                        help=f"read the committed network ({OFFLINE_NETWORK.relative_to(REPO)}) instead of a stack")
    args = parser.parse_args()

    if args.offline:
        segments, n_junctions = network_offline(OFFLINE_NETWORK)
        source = {"mode": "offline", "file": str(OFFLINE_NETWORK.relative_to(REPO))}
    else:
        try:
            segments, n_junctions = network_from_stack(args.oracle)
        except OSError as exc:  # unreachable, refused, timed out, HTTP error
            raise SystemExit(f"cannot read the network from {args.oracle} ({exc}) — "
                             "start the stack, point --oracle / ORACLE_URL at one, or pass --offline")
        except (ValueError, KeyError, TypeError) as exc:  # not JSON, or not the shape read above
            raise SystemExit(f"unexpected answer from {args.oracle} ({type(exc).__name__}: {exc}) — "
                             "is it a traffic-oracle? pass --offline to use the committed network")
        source = {"mode": "stack", "oracle": args.oracle,
                  "reads": ["GET /api/v1/spatial/segments (city bbox)",
                            "GET /api/v1/city/entities?kinds=junction"]}
    if not segments or not n_junctions:
        raise SystemExit("network read failed — no segments/junctions found")

    total_km = sum(s["length_m"] for s in segments) / 1000
    mean_lanes = sum(s["lanes"] for s in segments) / len(segments)
    # Arrivals per minute at a representative junction (density × speed → flow):
    lam = DENSITY_VEH_PER_KM_PER_LANE * EFFECTIVE_APPROACH_KMH / 60.0 * mean_lanes

    def scenario(hb, hp, lam_=None):
        l = lam_ if lam_ is not None else lam
        base = commuter_delay_min(hb, l, n_junctions)
        plat = commuter_delay_min(hp, l, n_junctions)
        veh_base = n_junctions * l * hb
        veh_plat = n_junctions * l * hp
        idle_h_base = base / 60 * IDLING_SHARE_OF_DELAY
        idle_h_plat = plat / 60 * IDLING_SHARE_OF_DELAY
        return {
            "baseline_hold_min": hb, "platform_hold_min": hp,
            "commuter_minutes_baseline": round(base, 1),
            "commuter_minutes_platform": round(plat, 1),
            "reduction_factor": round(base / plat, 1),
            "vehicles_delayed_baseline": round(veh_base),
            "vehicles_delayed_platform": round(veh_plat),
            "idle_liters_baseline": round(idle_h_base * IDLE_LITERS_PER_HOUR, 1),
            "idle_liters_platform": round(idle_h_plat * IDLE_LITERS_PER_HOUR, 1),
            "co2_kg_baseline": round(idle_h_base * IDLE_LITERS_PER_HOUR * CO2_KG_PER_LITER, 1),
            "co2_kg_platform": round(idle_h_plat * IDLE_LITERS_PER_HOUR * CO2_KG_PER_LITER, 1),
        }

    central = scenario(BASELINE_HOLD_MIN, PLATFORM_HOLD_MIN)
    conservative = scenario(BASELINE_HOLD_LOW, PLATFORM_HOLD_CONSERVATIVE, lam * 0.6)
    optimistic = scenario(BASELINE_HOLD_HIGH, PLATFORM_HOLD_OPTIMISTIC)
    per_junction_base = commuter_delay_min(BASELINE_HOLD_MIN, lam, 1)
    per_junction_plat = commuter_delay_min(PLATFORM_HOLD_MIN, lam, 1)

    amb_per_movement = AMB_TRIPS_CROSSING_PER_MOVEMENT * (AMB_BASELINE_DELAY_MIN - AMB_PLATFORM_DELAY_MIN)
    amb_annual = amb_per_movement * MOVEMENT_DAYS_PER_YEAR

    results = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "network": {
            "segments": len(segments), "junctions": n_junctions,
            "total_km": round(total_km, 1), "mean_lanes": round(mean_lanes, 1),
            "arrival_rate_veh_per_min_per_junction": round(lam, 1),
            "source": source,
            "attribution": ATTRIBUTION,
        },
        "scenarios": {"central": central, "conservative": conservative,
                      "optimistic": optimistic},
        "per_junction_central": {
            "commuter_minutes_baseline": round(per_junction_base, 1),
            "commuter_minutes_platform": round(per_junction_plat, 1),
        },
        "ambulance": {
            "trips_crossing_per_movement_day": AMB_TRIPS_CROSSING_PER_MOVEMENT,
            "minutes_returned_per_movement_day": round(amb_per_movement),
            "minutes_returned_per_year": round(amb_annual),
        },
        "annualization": {
            "movement_days_per_year": MOVEMENT_DAYS_PER_YEAR,
            "commuter_hours_returned_per_year_central": round(
                (central["commuter_minutes_baseline"] - central["commuter_minutes_platform"])
                * MOVEMENT_DAYS_PER_YEAR / 60),
        },
    }

    out_json = Path(__file__).resolve().parent / "impact_results.json"
    out_json.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    seg_lines = "\n".join(
        f"| {s['id']} | {s['name']} | {s['class']} | {s['lanes']} | "
        f"{s['speed_kmh']} | {s['length_m']/1000:.1f} |" for s in segments)
    if source["mode"] == "stack":
        read_from = (f"read from the running stack's traffic-oracle (`{source['oracle']}`: "
                     "`GET /api/v1/spatial/segments` + `GET /api/v1/city/entities?kinds=junction`); "
                     f"`--offline` rebuilds the same network, and identical figures, from the "
                     f"committed `{OFFLINE_NETWORK.relative_to(REPO)}`")
    else:
        read_from = (f"read from the committed generator output (`{source['file']}`) — the network "
                     "a deployed stack serves; re-run without `--offline` to read a running stack")

    c, k, o = central, conservative, optimistic
    report = f"""# IMPACT REPORT — rolling corridor holds vs blanket freeze

*Generated {results['generated_at_utc']} by `scripts/impact_run.py` (deterministic;
re-run to regenerate). Model, not field measurement — see the honesty contract
in the script header and the assumptions table below.*

## What is compared

- **Baseline (today):** blanket freeze at every corridor junction for the full
  passage window — documented practice is **15–45 minutes**; the model's
  central case uses **{BASELINE_HOLD_MIN:.0f} min**.
- **Platform (this repo):** predictive rolling holds — closures activated
  60–180 s ahead of the convoy, released as it passes — **~{PLATFORM_HOLD_MIN} min**
  per junction.

## Network under test (Veer's road network, on real OpenStreetMap roads)

{len(segments)} segments, **{n_junctions} junctions**, {total_km:.1f} km
(great-circle length of the served geometry), mean {mean_lanes:.1f} lanes —
{read_from}. Every segment is drawn along the real road of its name between
real intersections (migration 020, `scripts/gen_osm_network.py`); road
geometry © OpenStreetMap contributors, ODbL 1.0. Arrival rate modelled at
**{lam:.0f} vehicles/min/junction** ({DENSITY_VEH_PER_KM_PER_LANE:.0f} veh/km/lane ×
{EFFECTIVE_APPROACH_KMH:.0f} km/h peak approach speed — the repo simulator's own
density default — × the mean lane count).

*Scale note: the model freezes every junction of the network under test for
one movement — the whole {len(segments)}-segment city network, an upper-bound
framing kept from the original model. A single movement crosses a subset of
it; what transfers to any corridor is the per-junction figure (central case
≈ {per_junction_base:,.0f} → ≈ {per_junction_plat:,.0f} commuter-minutes per frozen junction) and
the reduction ratio, which does not depend on the junction count.*

| Segment | Road | Class | Lanes | Speed | km |
|---|---|---|---|---|---|
{seg_lines}

## Commuter impact per movement (central case)

| Metric | Baseline | Platform |
|---|---|---|
| Hold per junction | {c['baseline_hold_min']:.0f} min | {c['platform_hold_min']} min |
| Commuter-minutes of delay | **{c['commuter_minutes_baseline']:,.0f}** | **{c['commuter_minutes_platform']:,.0f}** |
| Vehicles caught in holds | {c['vehicles_delayed_baseline']:,} | {c['vehicles_delayed_platform']:,} |
| Fuel idled (litres) | {c['idle_liters_baseline']:,.0f} | {c['idle_liters_platform']:,.0f} |
| CO₂ (kg) | {c['co2_kg_baseline']:,.0f} | {c['co2_kg_platform']:,.0f} |

**Reduction in junction-freeze commuter delay: {c['reduction_factor']:,.0f}×**
(conservative case {k['baseline_hold_min']:.0f}→{k['platform_hold_min']:.0f} min with
−40% arrivals: **{k['reduction_factor']:,.0f}×**; optimistic {o['baseline_hold_min']:.0f}→{o['platform_hold_min']:.0f} min:
**{o['reduction_factor']:,.0f}×**). The mechanism is structural — delay scales with
hold duration **squared** — which is why even the conservative bound clears the
platform's original "6×" aspiration. *We no longer lead with "6×"; we lead with
the mechanism and this range, modelled.*

## Emergency patients (the acute case)

- {AMB_TRIPS_CROSSING_PER_MOVEMENT:.0f} ambulance trips/day are modelled as crossing
  the corridor during a movement window (assumption — basis below).
- Per intersecting trip the platform returns **{AMB_BASELINE_DELAY_MIN - AMB_PLATFORM_DELAY_MIN:.0f} minutes**
  (documented holds 5–20 min, midpoint {AMB_BASELINE_DELAY_MIN:.0f}; platform ≤ {AMB_PLATFORM_DELAY_MIN:.0f} min
  with an approved window).
- **≈ {amb_per_movement:,.0f} golden-hour minutes per movement-day ·
  ≈ {amb_annual:,.0f} per year** at {MOVEMENT_DAYS_PER_YEAR} movement-days/yr.

## Annualised (central case)

- **{results['annualization']['commuter_hours_returned_per_year_central']:,} commuter-hours
  returned to the city per year** (labelled movement-day assumption below).
- {(c['idle_liters_baseline'] - c['idle_liters_platform']) * MOVEMENT_DAYS_PER_YEAR:,.0f} litres
  of fuel and {(c['co2_kg_baseline'] - c['co2_kg_platform']) * MOVEMENT_DAYS_PER_YEAR / 1000:,.1f} tonnes
  of CO₂ avoided.

## Assumptions ledger (every number, sourced or labelled)

| Constant | Value | Source |
|---|---|---|
| Traffic density | {DENSITY_VEH_PER_KM_PER_LANE:.0f} veh/km/lane | repo simulator default (`scenario_sim.py`) |
| Peak approach speed | {EFFECTIVE_APPROACH_KMH:.0f} km/h | **assumption** (urban peak) |
| Baseline blanket hold | {BASELINE_HOLD_MIN:.0f} min (15–45) | documented practice (PROBLEM_STATEMENT citations) |
| Platform rolling hold | {PLATFORM_HOLD_MIN} min | platform design (60–180 s activation) |
| Discharge factor | {DISCHARGE_FACTOR} | **assumption** (textbook oversaturation shape) |
| Junctions frozen per movement | {n_junctions} | **assumption** (every junction of the network under test — see the scale note) |
| Ambulance trips crossing/movement-day | {AMB_TRIPS_CROSSING_PER_MOVEMENT:.0f} | **assumption** (≈0.4% of Gujarat's ~2,925 EMRI trips/day; falsify with dispatch data) |
| Ambulance baseline delay | {AMB_BASELINE_DELAY_MIN:.0f} min (5–20) | documented incidents (Mumbai 2022, Kanchipuram 2018, Agartala 2026) |
| Movement-days/year | {MOVEMENT_DAYS_PER_YEAR} | **assumption** (RTI-able; Ahmedabad–Gandhinagar heavy schedule) |
| Idle consumption / CO₂ | {IDLE_LITERS_PER_HOUR} L/h · {CO2_KG_PER_LITER} kg/L | standard factors |

**To falsify or improve this report:** replace any assumption with measured
data and re-run `python3 scripts/impact_run.py` (a running stack) or
`python3 scripts/impact_run.py --offline` (the committed network) — the report
and `scripts/impact_results.json` regenerate deterministically.

*Road network: {ATTRIBUTION}.*
"""
    (REPO / "IMPACT_REPORT.md").write_text(report, encoding="utf-8")
    print(f"wrote IMPACT_REPORT.md and {out_json}")
    print(f"source={source['mode']} junctions={n_junctions} segments={len(segments)} km={total_km:.1f} "
          f"reduction_central={c['reduction_factor']}x conservative={k['reduction_factor']}x "
          f"optimistic={o['reduction_factor']}x amb_minutes_year={amb_annual:,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
