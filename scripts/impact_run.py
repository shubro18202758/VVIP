#!/usr/bin/env python3
"""Impact run — quantifies commuter and emergency-patient benefit of rolling
corridor holds vs today's blanket freeze, on the repo's own Ahmedabad seed
network.

Honesty contract (read before quoting any number from the report):
- This is a DETERMINISTIC ANALYTICAL MODEL on the synthetic seed network
  (services/corridor-store/seed/sample_corridor.sql), not a field
  measurement. Every constant is either sourced (column "source") or an
  explicit labelled assumption.
- The mechanism it models is real and conservative: delay scales with the
  SQUARE of hold duration (deterministic queueing), so shrinking a 25-minute
  blanket hold to a 2.5-minute rolling hold removes ~99% of junction delay
  even before spillback effects.
- To falsify or improve: replace the assumption constants with RTI'd
  movement counts and junction arrival rates and re-run
  (`python3 scripts/impact_run.py`). The report regenerates deterministically.

Stdlib only. Writes IMPACT_REPORT.md and scripts/impact_results.json.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SEED = REPO / "services" / "corridor-store" / "seed" / "sample_corridor.sql"

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


def parse_seed(path: Path):
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


def commuter_delay_min(hold_min, lambda_per_min, n_junctions):
    """Deterministic-queue commuter delay: arrivals λ during hold H queue up;
    average delay per arrival ≈ H/2 (plus linear discharge), total = λH²/2."""
    return DISCHARGE_FACTOR * lambda_per_min * n_junctions * (hold_min ** 2) / 2.0


def main() -> int:
    segments, n_junctions = parse_seed(SEED)
    if not segments or not n_junctions:
        raise SystemExit("seed parse failed — no segments/junctions found")

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

    amb_per_movement = AMB_TRIPS_CROSSING_PER_MOVEMENT * (AMB_BASELINE_DELAY_MIN - AMB_PLATFORM_DELAY_MIN)
    amb_annual = amb_per_movement * MOVEMENT_DAYS_PER_YEAR

    results = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "network": {
            "segments": len(segments), "junctions": n_junctions,
            "total_km": round(total_km, 1), "mean_lanes": round(mean_lanes, 1),
            "arrival_rate_veh_per_min_per_junction": round(lam, 1),
        },
        "scenarios": {"central": central, "conservative": conservative,
                      "optimistic": optimistic},
        "ambulance": {
            "trips_crossing_per_movement_day": AMB_TRIPS_CROSSING_PER_MOVEMENT,
            "minutes_returned_per_movement_day": round(amb_per_movement),
            "minutes_returned_per_year": round(amb_annual),
        },
        "annualization": {
            "movement_days_per_year": MOVEMENT_DAYS_PER_YEAR,
            "commuter_hours_returned_per_year_central": round(
                central["commuter_minutes_baseline"] - central["commuter_minutes_platform"], 1)
            / 60 * MOVEMENT_DAYS_PER_YEAR,
        },
    }
    results["annualization"]["commuter_hours_returned_per_year_central"] = round(
        (central["commuter_minutes_baseline"] - central["commuter_minutes_platform"])
        * MOVEMENT_DAYS_PER_YEAR / 60)

    out_json = Path(__file__).resolve().parent / "impact_results.json"
    out_json.write_text(json.dumps(results, indent=2), encoding="utf-8")

    seg_lines = "\n".join(
        f"| {s['id']} | {s['name']} | {s['class']} | {s['lanes']} | "
        f"{s['speed_kmh']} | {s['length_m']/1000:.1f} |" for s in segments)

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

## Network under test (parsed live from the seed)

{len(segments)} segments, **{n_junctions} junctions**, {total_km:.1f} km, mean
{mean_lanes:.1f} lanes. Arrival rate modelled at **{lam:.0f} vehicles/min/junction**
({DENSITY_VEH_PER_KM_PER_LANE:.0f} veh/km/lane × {EFFECTIVE_APPROACH_KMH:.0f} km/h
peak approach speed — the repo simulator's own density default).

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
| Ambulance trips crossing/movement-day | {AMB_TRIPS_CROSSING_PER_MOVEMENT:.0f} | **assumption** (≈0.4% of Gujarat's ~2,925 EMRI trips/day; falsify with dispatch data) |
| Ambulance baseline delay | {AMB_BASELINE_DELAY_MIN:.0f} min (5–20) | documented incidents (Mumbai 2022, Kanchipuram 2018, Agartala 2026) |
| Movement-days/year | {MOVEMENT_DAYS_PER_YEAR} | **assumption** (RTI-able; Ahmedabad–Gandhinagar heavy schedule) |
| Idle consumption / CO₂ | {IDLE_LITERS_PER_HOUR} L/h · {CO2_KG_PER_LITER} kg/L | standard factors |

**To falsify or improve this report:** replace any assumption with measured
data and re-run `python3 scripts/impact_run.py` — the report and
`scripts/impact_results.json` regenerate deterministically.
"""
    (REPO / "IMPACT_REPORT.md").write_text(report, encoding="utf-8")
    print(f"wrote IMPACT_REPORT.md and {out_json}")
    print(f"junctions={n_junctions} segments={len(segments)} km={total_km:.1f} "
          f"reduction_central={c['reduction_factor']}x "
          f"conservative={k['reduction_factor']}x "
          f"amb_minutes_year={amb_annual:,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
