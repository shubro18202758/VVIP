# IMPACT REPORT — rolling corridor holds vs blanket freeze

*Generated 2026-10-10T23:40:28+00:00 by `scripts/impact_run.py` (deterministic;
re-run to regenerate). Model, not field measurement — see the honesty contract
in the script header and the assumptions table below.*

## What is compared

- **Baseline (today):** blanket freeze at every corridor junction for the full
  passage window — documented practice is **15–45 minutes**; the model's
  central case uses **25 min**.
- **Platform (this repo):** predictive rolling holds — closures activated
  60–180 s ahead of the convoy, released as it passes — **~2.5 min**
  per junction.

## Network under test (parsed live from the seed)

40 segments, **14 junctions**, 90.1 km, mean
5.8 lanes. Arrival rate modelled at **87 vehicles/min/junction**
(30 veh/km/lane × 30 km/h
peak approach speed — the repo simulator's own density default).

| Segment | Road | Class | Lanes | Speed | km |
|---|---|---|---|---|---|
| 1001 | Ashram Road (South) | trunk | 6 | 60 | 1.5 |
| 1002 | Ashram Road (North) | trunk | 6 | 60 | 2.9 |
| 1003 | CG Road (West) | primary | 4 | 50 | 1.2 |
| 1004 | CG Road (East) | primary | 4 | 50 | 1.6 |
| 1005 | Relief Road | primary | 4 | 40 | 1.4 |
| 1006 | Riverfront Road (West) | trunk | 6 | 60 | 1.5 |
| 1007 | Riverfront Road (East) | trunk | 6 | 60 | 1.5 |
| 1008 | SP Ring Road (East) | motorway | 8 | 80 | 4.9 |
| 1009 | Sabarmati Ashram Road | secondary | 4 | 40 | 1.1 |
| 1010 | Usmanpura Overbridge Road | secondary | 4 | 40 | 1.3 |
| 1011 | Drive-In Road | primary | 4 | 50 | 1.4 |
| 1012 | Gandhi Road | trunk | 6 | 40 | 0.7 |
| 1013 | Tilak Road | primary | 4 | 50 | 1.4 |
| 1014 | Station Road (Kalupur) | primary | 4 | 50 | 1.1 |
| 1015 | Nehru Bridge Road | trunk | 6 | 60 | 1.0 |
| 1016 | SG Highway (Vastrapur) | motorway | 8 | 80 | 1.6 |
| 1017 | SG Highway (Bodakdev) | motorway | 8 | 80 | 1.6 |
| 1018 | SG Highway (Thaltej) | motorway | 8 | 80 | 1.2 |
| 1019 | Satellite Road | secondary | 4 | 40 | 1.2 |
| 1020 | Prahlad Nagar Road | secondary | 4 | 40 | 1.1 |
| 1021 | SP Ring Road (South) | motorway | 8 | 80 | 6.2 |
| 1022 | SP Ring Road (Southeast) | motorway | 8 | 80 | 6.2 |
| 1023 | Bopal Road | primary | 4 | 50 | 2.0 |
| 1024 | Gandhinagar Highway (South) | motorway | 8 | 100 | 2.4 |
| 1025 | Gandhinagar Highway (Central) | motorway | 8 | 100 | 2.4 |
| 1026 | Gandhinagar Highway (North) | motorway | 8 | 100 | 2.4 |
| 1027 | Airport Road (South) | motorway | 8 | 80 | 2.3 |
| 1028 | Airport Road (North) | motorway | 8 | 80 | 2.9 |
| 1029 | Hansol-Airport Connector | motorway | 6 | 80 | 1.4 |
| 1030 | Naroda Road | trunk | 6 | 60 | 3.1 |
| 1031 | CTM Road | primary | 4 | 50 | 2.3 |
| 1032 | Motera Stadium Road | trunk | 6 | 60 | 1.4 |
| 1033 | SP Ring Road (West) | motorway | 8 | 80 | 2.4 |
| 1034 | SP Ring Road (North) | motorway | 8 | 70 | 4.2 |
| 1035 | SP Ring Road (Northeast) | motorway | 6 | 70 | 4.2 |
| 1036 | Paldi Road | secondary | 4 | 40 | 1.3 |
| 1037 | Science City Road | secondary | 4 | 40 | 3.3 |
| 1038 | Kankaria Road | secondary | 4 | 40 | 1.6 |
| 1039 | Ellis Bridge Road | secondary | 4 | 40 | 1.3 |
| 1040 | Sarkhej Road | primary | 4 | 50 | 5.2 |

## Commuter impact per movement (central case)

| Metric | Baseline | Platform | 
|---|---|---|
| Hold per junction | 25 min | 2.5 min |
| Commuter-minutes of delay | **570,938** | **5,709** |
| Vehicles caught in holds | 30,450 | 3,045 |
| Fuel idled (litres) | 5,138 | 51 |
| CO₂ (kg) | 12,846 | 128 |

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

- **2,355,117 commuter-hours
  returned to the city per year** (labelled movement-day assumption below).
- 1,271,750 litres
  of fuel and 3,179.4 tonnes
  of CO₂ avoided.

## Assumptions ledger (every number, sourced or labelled)

| Constant | Value | Source |
|---|---|---|
| Traffic density | 30 veh/km/lane | repo simulator default (`scenario_sim.py`) |
| Peak approach speed | 30 km/h | **assumption** (urban peak) |
| Baseline blanket hold | 25 min (15–45) | documented practice (PROBLEM_STATEMENT citations) |
| Platform rolling hold | 2.5 min | platform design (60–180 s activation) |
| Discharge factor | 1.5 | **assumption** (textbook oversaturation shape) |
| Ambulance trips crossing/movement-day | 12 | **assumption** (≈0.4% of Gujarat's ~2,925 EMRI trips/day; falsify with dispatch data) |
| Ambulance baseline delay | 12 min (5–20) | documented incidents (Mumbai 2022, Kanchipuram 2018, Agartala 2026) |
| Movement-days/year | 250 | **assumption** (RTI-able; Ahmedabad–Gandhinagar heavy schedule) |
| Idle consumption / CO₂ | 0.9 L/h · 2.5 kg/L | standard factors |

**To falsify or improve this report:** replace any assumption with measured
data and re-run `python3 scripts/impact_run.py` — the report and
`scripts/impact_results.json` regenerate deterministically.
