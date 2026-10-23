# IMPACT REPORT — rolling corridor holds vs blanket freeze

*Generated 2026-10-23T14:39:29+00:00 by `scripts/impact_run.py` (deterministic;
re-run to regenerate). Model, not field measurement — see the honesty contract
in the script header and the assumptions table below.*

## What is compared

- **Baseline (today):** blanket freeze at every corridor junction for the full
  passage window — documented practice is **15–45 minutes**; the model's
  central case uses **25 min**.
- **Platform (this repo):** predictive rolling holds — closures activated
  60–180 s ahead of the convoy, released as it passes — **~2.5 min**
  per junction.

## Network under test (Veer's road network, on real OpenStreetMap roads)

49 segments, **34 junctions**, 192.0 km
(great-circle length of the served geometry), mean 4.7 lanes —
read from the committed generator output (`scripts/data/osm_network.json`) — the network a deployed stack serves; re-run without `--offline` to read a running stack. Every segment is drawn along the real road of its name between
real intersections (migration 020, `scripts/gen_osm_network.py`); road
geometry © OpenStreetMap contributors, ODbL 1.0. Arrival rate modelled at
**71 vehicles/min/junction** (30 veh/km/lane ×
30 km/h peak approach speed — the repo simulator's own
density default — × the mean lane count).

*Scale note: the model freezes every junction of the network under test for
one movement — the whole 49-segment city network, an upper-bound
framing kept from the original model. A single movement crosses a subset of
it; what transfers to any corridor is the per-junction figure (central case
≈ 33,291 → ≈ 333 commuter-minutes per frozen junction) and
the reduction ratio, which does not depend on the junction count.*

| Segment | Road | Class | Lanes | Speed | km |
|---|---|---|---|---|---|
| 1001 | Ashram Road (South) | primary | 4 | 60 | 0.5 |
| 1002 | Ashram Road (North) | primary | 4 | 60 | 1.6 |
| 1003 | CG Road (South) | primary | 4 | 50 | 3.2 |
| 1004 | CG Road (North) | primary | 4 | 50 | 1.0 |
| 1005 | Relief Road | secondary | 4 | 40 | 2.0 |
| 1006 | Riverfront Road (West) | tertiary | 4 | 40 | 3.1 |
| 1007 | Riverfront Road (East) | secondary | 4 | 50 | 6.1 |
| 1008 | SP Ring Road (East) | trunk | 4 | 80 | 7.6 |
| 1009 | Ashram Road (Sabarmati Ashram) | primary | 4 | 40 | 2.7 |
| 1010 | SH71 (Bhat–Motera Road) | tertiary | 4 | 40 | 5.0 |
| 1011 | Drive-In Road | secondary | 4 | 50 | 2.6 |
| 1012 | Gandhi Road | tertiary | 4 | 40 | 2.4 |
| 1013 | Swami Vivekananda Road | secondary | 2 | 50 | 1.6 |
| 1014 | Station Road (Kalupur) | primary | 4 | 50 | 0.4 |
| 1015 | Nehru Bridge Road | secondary | 4 | 50 | 1.1 |
| 1016 | SG Highway (Vastrapur) | primary | 6 | 60 | 1.4 |
| 1017 | SG Highway (Bodakdev) | trunk | 8 | 80 | 2.2 |
| 1018 | SG Highway (Thaltej) | trunk | 8 | 80 | 3.1 |
| 1019 | Satellite Road | secondary | 4 | 40 | 2.5 |
| 1020 | Prahlad Nagar Road | tertiary | 4 | 40 | 3.2 |
| 1021 | SP Ring Road (South) | trunk | 4 | 80 | 13.8 |
| 1022 | SP Ring Road (Southeast) | trunk | 4 | 80 | 15.1 |
| 1023 | Ambli Bopal Road | tertiary | 4 | 40 | 3.2 |
| 1024 | Gandhinagar Highway (South) | primary | 6 | 60 | 1.3 |
| 1025 | Gandhinagar Highway (Central) | primary | 6 | 60 | 1.9 |
| 1026 | Gandhinagar Highway (North) | primary | 6 | 60 | 6.4 |
| 1027 | Airport Road (South) | primary | 6 | 60 | 3.0 |
| 1028 | Airport Road (North) | primary | 6 | 60 | 1.6 |
| 1029 | Domestic Airport Road (Hansol) | secondary | 4 | 50 | 1.4 |
| 1030 | Naroda Road | primary | 6 | 60 | 11.4 |
| 1031 | Kasturba Gandhi Road | primary | 4 | 50 | 3.5 |
| 1032 | Motera Stadium Road | primary | 6 | 60 | 3.0 |
| 1033 | SP Ring Road (West) | trunk | 4 | 80 | 6.9 |
| 1034 | SP Ring Road (North) | trunk | 4 | 70 | 9.6 |
| 1035 | SP Ring Road (Northeast) | trunk | 4 | 70 | 7.7 |
| 1036 | Ashram Road (Paldi) | primary | 4 | 40 | 1.6 |
| 1037 | Science City Road | secondary | 4 | 40 | 3.2 |
| 1038 | Kankaria Road | secondary | 4 | 40 | 1.4 |
| 1039 | Swami Vivekananda Bridge (Ellis Bridge) | secondary | 6 | 40 | 0.6 |
| 1040 | SG Highway (Sarkhej) | trunk | 8 | 80 | 5.6 |
| 1041 | Chimanbhai Patel Flyover (Ashram Road–Motera) | primary | 4 | 60 | 3.0 |
| 1042 | Commerce College Road (Drive-In Road–CG Road) | secondary | 4 | 50 | 2.3 |
| 1043 | Dr. Ambedkar Road (Lal Darwaja–Shahibaug) | primary | 6 | 40 | 4.8 |
| 1044 | 132 Ft. Ring Road (IIM) | primary | 4 | 50 | 2.7 |
| 1045 | SG Highway (Prahlad Nagar) | trunk | 8 | 80 | 3.4 |
| 1046 | SP Ring Road (Northwest) | trunk | 4 | 80 | 8.6 |
| 1047 | SP Ring Road (Southwest) | trunk | 4 | 70 | 7.9 |
| 1048 | Indira Bridge (Airport–Motera) | primary | 6 | 60 | 2.1 |
| 1049 | Sarangpur Road (Kalupur–Astodia) | primary | 4 | 40 | 2.1 |

## Commuter impact per movement (central case)

| Metric | Baseline | Platform |
|---|---|---|
| Hold per junction | 25 min | 2.5 min |
| Commuter-minutes of delay | **1,131,888** | **11,319** |
| Vehicles caught in holds | 60,367 | 6,037 |
| Fuel idled (litres) | 10,187 | 102 |
| CO₂ (kg) | 25,468 | 255 |

**Reduction in junction-freeze commuter delay: 100×**
(conservative case 15→3 min with
−40% arrivals: **25×**; optimistic 45→2 min:
**506×**). The mechanism is structural — delay scales with
hold duration **squared** — which is why even the conservative bound clears the
platform's original "6×" aspiration. *We no longer lead with "6×"; we lead with
the mechanism and this range, modelled.*

## Emergency patients (the acute case)

- 12 ambulance trips/day are modelled as crossing
  the corridor during a movement window (assumption — basis below).
- Per intersecting trip the platform returns **11 minutes**
  (documented holds 5–20 min, midpoint 12; platform ≤ 1 min
  with an approved window).
- **≈ 132 golden-hour minutes per movement-day ·
  ≈ 33,000 per year** at 250 movement-days/yr.

## Annualised (central case)

- **4,669,037 commuter-hours
  returned to the city per year** (labelled movement-day assumption below).
- 2,521,275 litres
  of fuel and 6,303.2 tonnes
  of CO₂ avoided.

## Assumptions ledger (every number, sourced or labelled)

| Constant | Value | Source |
|---|---|---|
| Traffic density | 30 veh/km/lane | repo simulator default (`scenario_sim.py`) |
| Peak approach speed | 30 km/h | **assumption** (urban peak) |
| Baseline blanket hold | 25 min (15–45) | documented practice (PROBLEM_STATEMENT citations) |
| Platform rolling hold | 2.5 min | platform design (60–180 s activation) |
| Discharge factor | 1.5 | **assumption** (textbook oversaturation shape) |
| Junctions frozen per movement | 34 | **assumption** (every junction of the network under test — see the scale note) |
| Ambulance trips crossing/movement-day | 12 | **assumption** (≈0.4% of Gujarat's ~2,925 EMRI trips/day; falsify with dispatch data) |
| Ambulance baseline delay | 12 min (5–20) | documented incidents (Mumbai 2022, Kanchipuram 2018, Agartala 2026) |
| Movement-days/year | 250 | **assumption** (RTI-able; Ahmedabad–Gandhinagar heavy schedule) |
| Idle consumption / CO₂ | 0.9 L/h · 2.5 kg/L | standard factors |

**To falsify or improve this report:** replace any assumption with measured
data and re-run `python3 scripts/impact_run.py` (a running stack) or
`python3 scripts/impact_run.py --offline` (the committed network) — the report
and `scripts/impact_results.json` regenerate deterministically.

*Road network: (c) OpenStreetMap contributors, ODbL 1.0 — https://www.openstreetmap.org/copyright.*
