# The Use-Case Library — One Failure Mode, Two Mission Families

> The VVIP convoy is the **base case**, not the boundary. This platform is a
> **city-scale, multi-agency corridor-and-enforcement operating system**: one
> engine that schedules, predicts and releases road-network occupancy for any
> protected or public movement — and, since the enforcement family landed, one
> watchlist/ANPR backbone that carries a stolen-vehicle report from FIR to
> interception plan on the same rails. This document is the library of event
> families the engine must express — each entry carrying the documented reality
> it models, why it recurs in a Viksit Bharat governance frame, and exactly how
> it is expressed in the running system today. The God's-eye canvas
> (`docs/GODS_EYE_INTEGRATION.md`) is the shared picture both families
> render onto.

**The thesis, in one sentence.** When a protected person moves by road,
traffic along the corridor is frozen 15–45 minutes at each major junction
though the passage itself occupies it for seconds; coordination runs by
phone, radio and WhatsApp across five hierarchies with no shared picture
(`PROBLEM_STATEMENT_GUJARAT.md:11-29`) — and **every other use case below is
this same failure mode in a different costume: a corridor whose occupancy
must be scheduled, predicted and released.**

The Blue Book itself demands that traffic be *"regulated, not stopped"*
(`PROBLEM_STATEMENT_GUJARAT.md:62-69`). The gap has never been policy. It is
that no tooling exists to execute the policy at the precision it demands —
for a Z+ state-capital run, a 14 km procession, a marathon, a stadium emptying
itself, or a flood evacuation column.

---

## How to read this library

The library spans **two families**. The **corridor family** (entries 0–7)
is one failure mode in eight costumes: a corridor whose occupancy must be
scheduled, predicted and released. The **enforcement family** (entry 8)
rides the same city backbone in the other direction: a crime report
becomes a lawful watchlist, watchlist hits become human-approved
recommendations, and an approved recommendation becomes a human-initiated
pursuit with a ranked interception plan. Each entry has three parts:

- **Reality** — the documented, citable event class in the world (sources
  named inline; where a figure could not be verified, the entry says so).
- **Why it recurs (Viksit Bharat relevance)** — the governance case: how often
  this corridor class occurs and why a national traffic platform must be able
  to express it.
- **In the engine** — how the case is expressed *today* as a runnable
  scenario: `scenario_id`, `event_kind`, `protection_class`, route (seed
  segment IDs), crowd class and closure policy, from
  `scripts/scenario_library.json` (verified against this file) and the
  emulator's own copy at
  `services/event-emulator/src/event_emulator/use_case_library.json`.

All the corridor entries are **not mockups**: each is a scenario the event
emulator injects through the real stack's real APIs, and the digital twin
runs in seeded 12-replication Monte Carlo. The generalized data model
behind them (`events.corridor_events`, migration
`services/corridor-store/migrations/007_corridor_events.sql`, extended by
`010_vehicles_watchlist.sql`) carries five orthogonal axes —
`protection_class` (`Z+`…`X`, plus **`none`** for public events),
`crowd_class` (`LOW`…`EXTREME`), `event_kind` (the ten archetypes below),
footprint and phases — and the kind selects **closure-policy and dossier
templates, never code paths**: the same forecasting, routing, situation
and audit machinery runs for all of them.

| # | Use case | `event_kind` | `protection_class` | Crowd | Closure policy (seeded) |
|---|---|---|---|---|---|
| 0 | VVIP convoy base case | `protected_movement` | **Z+** | LOW | full closure, 180 s advance |
| 1 | Jagannath Rath Yatra, Ahmedabad | `procession` | none (X) | EXTREME | full closure, 120 s |
| 2 | Kumbh-class mass gathering | `mass_gathering` | none (X) | EXTREME | signal hold, 90 s |
| 3 | Sabarmati riverfront marathon | `race` | none (X) | HIGH | full closure, 60 s |
| 4 | Republic Day / Independence Day | `ceremonial` | **Z** | HIGH | full closure, 150 s |
| 5 | Narendra Modi Stadium egress | `egress_surge` | none (X) | EXTREME | signal hold, 60 s |
| 6 | Monsoon flood evacuation | `emergency_evacuation` | none (X) | MEDIUM | signal hold, 60 s |
| 7 | Metro / bullet-train work zone | `work_zone` | none (X) | LOW | partial closure, 60 s |
| 8 | **Enforcement: stolen vehicle → ANPR sweep → pursuit** | `crime_incident` + `vehicle_pursuit` | none | LOW | none (watchlist, not closure) |

*"none (X)"* = generalized `protection_class: none` (a public event, no
protectee), seeded in the legacy scenario config as an X-class managed
movement. *(The flagship composite below — "God's Eye Morning" — layers
the Rath Yatra, the monsoon, the ambulance and the enforcement family on
one morning.)*

---

## 0. VVIP Convoy — Base Case (Z+ state-capital run, Ahmedabad–Gandhinagar)

**Reality.** The problem the system was born from: when a protected person
moves by road, traffic along the corridor is frozen 15–45 minutes at each
major junction though the passage itself occupies it for seconds
(`PROBLEM_STATEMENT_GUJARAT.md:13-15`); coordination runs by phone, radio and
WhatsApp across five hierarchies — traffic police, district administration,
SPG/state security, municipal signal teams, 108 dispatch — with no shared
picture, on the ~25 km Ahmedabad–Gandhinagar corridor that leadership travels
continuously and that events like the Vibrant Gujarat summit multiply
(`PROBLEM_STATEMENT_GUJARAT.md:11-29`). The Blue Book itself demands traffic
be *"regulated, not stopped"* — the gap is tooling, not policy
(`PROBLEM_STATEMENT_GUJARAT.md:62-69`).

**Why it recurs.** This is the base case the problem statement quantifies for
Viksit Bharat governance: 15–45 min blanket holds per junction against a
Gujarat 108 fleet of ~585 ambulances averaging ~5 emergency trips each per
day (`PROBLEM_STATEMENT_GUJARAT.md:56-60`) — every blanket hold is a lottery
ticket against one of those trips.

**In the engine** — `scenario_id: vvip_convoy_base` (category `baseline`):
Z+ movement, Monday 10:10, down the Gandhinagar Highway spine
(1026→1025→1024) through Kalupur (1014) onto Naroda Road (1030); maximum
protocol — full closure, 180 s advance (the Z+ floor); default event rates,
no weather. This is the reference movement every other use case is a
generalization of.

## 1. Jagannath Rath Yatra — Old-City Procession, Ahmedabad

**Reality.** Every Ashadha Shukla Dwitiya (June–July), three chariots step
off at 07:00 from the ~400-year-old Jagannath Temple in Jamalpur and walk a
~14 km loop through the old city — Jamalpur, Kalupur, Delhi Chakla,
Dariapur, Shahpur — halting at Saraspur before returning by night. It is
India's second-largest Rath Yatra after Puri: the 148th edition ran
27 June 2025 (NDTV, 26 Jun 2025; utsav.gov.in), and by the 2026 edition the
three chariots were live-monitored by 35 drones and GPS (Gujarat Samachar,
Jul 2026). The corridor problem is inverted from the VVIP case: the
"convoy" is the crowd's own sacred object, moving at walking pace for hours
through bazaar streets never designed for lakhs of pedestrians plus
elephants, trucks and floats — the entire old-city road grid is functionally
closed, and the management challenge is barricade timing, diversion of the
city's traffic around a frozen quarter, and emergency access through it.

**Why it recurs.** 148 consecutive annual editions — a calendar-fixed,
city-scale corridor freeze that civic infrastructure must schedule around,
with lakhs of participants and 35-drone/GPS monitoring by 2026 (sources
above).

**In the engine** — `scenario_id: rath_yatra_ahmedabad` (category `event`):
a public procession at **8 km/h — the engine's speed floor** — classified
`procession`, `protection_class` X, crowd **EXTREME**, full closure rolling
ahead of it (120 s advance), seeded to the real 148th edition's date
(2025-06-27T07:00, seed 20250627). Route: Gandhi Road (1012) → Nehru Bridge
(1015) → Relief Road (1005) → Tilak Road (1013); forced causes: rally
spillover on the yatra segments (950 + 850 veh/h) and a bazaar crowd pinned
behind Tilak Road barricades.

## 2. Kumbh-Class Mass Gathering — Perimeter Zone Management

**Reality.** The Maha Kumbh at Prayagraj drew ~660 million visits (66 crore)
across its ~45-day run in 2025, up from 240 million in 2019 — the largest
human gathering on record — and the mela cycle recurs every few years across
four sites (Wikipedia, citing UP government figures). At that scale there is
no "route" to clear: the zone interior is a car-free pedestrian city, and
the entire corridor problem moves to the perimeter — ingress corridors that
must absorb bathing-day surges (the pre-dawn Mauni Amavasya window is the
peak), egress corridors that must release them without crush, and the thin
supply/medical shuttle lanes that must thread both. This is the
generalization the engine must survive: from *"clear a line for a moving
principal"* to *"schedule the flux across a ring around a standing sea of
people."*

**Why it recurs.** Kumbh-class gatherings recur every 3–4 years across four
sites at 240–660 million visits (2019 → 2025, Wikipedia/UP government) —
the extreme end of crowd-scale corridor management any national traffic
platform must be able to express.

**In the engine** — `scenario_id: kumbh_scale_gathering` (category `event`):
`mass_gathering`, X-class **supply/medical shuttle** entering the zone
pre-dawn (04:30, seeded to the 2025 Mauni Amavasya class, seed 20250129)
under **signal hold** — signals keep cycling: staged ingress, not a blanket
freeze. Route on the seed network's riverfront cluster: Ashram Road south
(1001) → Riverfront West (1006) → Usmanpura (1010); forced causes:
bathing-day ingress surges on the zone core, east-bank queueing corridors
at capacity, and a saturated Ashram Road approach.

## 3. City Marathon — Sabarmati Riverfront Race Corridor, Ahmedabad

**Reality.** The Adani Ahmedabad Marathon's 9th edition brought 24,000+
runners — a record — to the Sabarmati Riverfront on 24 November 2024, and
the event sits on World Athletics' global calendar (ahmedabadmarathon.com;
Olympics.com preview; ThePrint). City races are a recurring corridor class
of their own: riverfront running events already force Ahmedabad police to
restrict the riverfront road and divert vehicles from Hariharanand Circle
(Times of India, 5 May 2024). The disruption signature is distinctive — a
pre-dawn full closure that must convert to staged crossings as the field
spreads, then a rolling reopen behind the sweep vehicle — and it stresses
exactly the machinery the VVIP case built: the "convoy" is a pace/sweep
vehicle moving at runner speed, and the closure is measured in
kilometre-hours of arterial capacity donated to a public event.

**Why it recurs.** 24,000+ runners at the 2024 Adani Ahmedabad Marathon on
the World Athletics calendar, with documented police diversions for
riverfront runs (TOI, 5 May 2024) — the recurring race-day closure class
every Smart City must stage.

**In the engine** — `scenario_id: riverfront_marathon` (category `event`):
`race`, X-class pace/sweep convoy at **12 km/h**, full closure converting to
staged reopen (60 s advance), 05:45 gun start, seeded to the real 2024
edition (seed 20241124). Route: Riverfront East (1007) → Ashram South
(1001) → Riverfront West (1006) → Usmanpura (1010); forced causes:
spectator spillover on both approach segments.

## 4. Republic Day / Independence Day — Ceremonial Parade Corridor

**Reality.** Two fixed calendar days a year, at national scale. At Gujarat's
76th Republic Day (26 January 2025) the Governor hoisted the flag and took
the parade salute with the Chief Minister present, at the state-level
function held that year in Tapi district while Gandhinagar hosted its own
ceremonies (Gujarat CMO, 25–26 Jan 2025) — every one of these is a
protected-person movement window plus a spectator-and-parking surge on the
parade strip and its approaches. *(Delhi's Kartavya Path spectator and
closure figures did not surface in the research for this library and are
not cited here.)* The corridor shape is a hybrid the engine must express: a
Z-class dignitary movement that terminates inside a standing public event —
closure policy has to serve both the protocol run and the ceremonial crowd
that remains after the principal has arrived.

**Why it recurs.** Two immovable national calendar days per year, each a
Z-class movement + public-ceremony composite — at the 76th RD (2025) the
Governor took the salute with the CM present (Gujarat CMO) on the same
capital-region corridor the engine seeds.

**In the engine** — `scenario_id: national_day_parade` (category `event`):
`ceremonial`, **Z-class** dignitary run from the airport (1028→1027) onto
the GN Highway parade strip (1024→1025) at 08:35, arriving before the 09:00
flag hoisting (seed 20270126); full closure at 150 s advance; forced
causes: grandstand spectators and parking saturating the strip itself plus
spectator approach traffic. Independence Day is the calendar mirror.

## 5. Stadium Egress — Narendra Modi Stadium Match Night, Ahmedabad

**Reality.** Narendra Modi Stadium at Motera is the world's largest cricket
ground at ~132,000 seats (Wikipedia); the IPL 2026 final put roughly 130,000
people in the house (Ayen News), and the T20 World Cup 2026 final brought
~3,000 police personnel plus ~1,000 home guards with anti-drone cover,
staggered entry protocols and published traffic advisories — diversions,
movement restrictions and special parking plans around Motera (India Today,
8 Mar 2026; Yahoo Sports; Telegraph India). Egress is the hardest 90 minutes
of stadium operations: the entire crowd exits onto a road network sized for
a normal evening, while the platform's own use case — an ambulance fighting
the dispersal wave — is the G4 emergency-passage problem at crowd scale.

**Why it recurs.** A ~132,000-seat venue with ~130,000 at the IPL 2026
final and 3,000+1,000 police/home-guard deployments with published diversion
advisories (sources above) — a near-monthly, citable-at-scale egress event.

**In the engine** — `scenario_id: motera_stadium_egress` (category `event`):
`egress_surge` at 20:05, just after a final ball (seed 20260308), crowd
**EXTREME**. The seed network carries the venue precisely: **segment 1032
"Motera Stadium Road" with `stadium_adjacent: true`**
(`scripts/scenario_library.json`, network section). An X-class ambulance
with a green-corridor request fights the dispersal wave from 1032 down the
Ashram spine (1002→1001) under **signal hold** (60 s); forced causes: the
~130k dispersal, the egress wave onto Ashram Road north, one stalled car
blocking a dispersal lane, and the `emergency_vehicle` window itself.

## 6. Monsoon Flood — Evacuation and Relief Corridor (Gujarat 2024 class)

**Reality.** In late August 2024 Gujarat took ~280 mm of rain in 24 hours at
Vadodara; the Vishwamitri crossed its 25-ft danger mark, low-lying city
cores went 10–12 ft under water, and the statewide toll reached 28+ dead
with more than 24,000 people relocated, as NDRF and Army columns ran boat
rescues (BBC, 29 Aug 2024; NDTV; Free Press Journal; ReliefWeb sitrep
27–28 Aug 2024). Flood response inverts every corridor assumption: the
low-lying arterials are the ones that vanish, evacuation traffic flows the
"wrong" way at saturation, and the convoy the engine must protect is a
relief column whose speed is set by water, not protocol. This is also the
use case where the ambulance-passage machinery (G4) stops being a
convenience and starts being the whole point.

**Why it recurs.** The August 2024 Gujarat floods — 28+ dead, 24,000+
relocated, NDRF and Army columns in the streets (BBC, 29 Aug 2024) — are the
recurring monsoon case where corridor minutes are measured in lives rather
than delay.

**In the engine** — `scenario_id: flood_evacuation_corridor` (category
`weather`): `emergency_evacuation`, 11:20 in 22 mm/h rain (seed 20240827,
the real flood week). The seed network marks the flood surface explicitly:
**7 low-lying segments — 1001, 1002, 1006, 1007, 1015, 1024, 1039**
(`low_lying: true` in `scripts/scenario_library.json`) — and the scenario
waterlogs the low-lying river spine (1001/1006/1015) while an X-class
relief column runs the reverse-flow, high-ground detour: Sarkhej (1040) onto
SP Ring Road (1021→1022→1008), under signal hold, with evacuee traffic
surging onto the ring road behind it and an abandoned vehicle on the
flooded shoulder.

## 7. Planned Infrastructure Work Zone — Metro / Bullet-Train Works, Ahmedabad

**Reality.** The most predictable corridor event of all: capacity loss that
is scheduled months ahead. Ahmedabad Metro Phase 2 — Motera Stadium north to
Gandhinagar's Mahatma Mandir, under construction since February 2021, with
the Cabinet approving the 77.63 km Phase 2(A) network (Wikipedia/Ahmedabad;
PIB) — has kept work zones live along the exact corridor the engine seeds;
Kankaria Road spent a published 3 July–30 September closure under metro
works with ~500 m diversions (Indian Express — "three traffic diversions
announced" for the underground stretch; India Live Ahmedabad), and ~100 m at
Kalupur Bridge was shut for bullet-train pillar installation. Unlike every
other use case, here the "event" is a standing capacity haircut: the
management problem is diversion publishing, night-time logistics convoys,
and holding commute corridors below gridlock for a season — which is
precisely the G3 citizen-advisory and G5 measurement loop of the problem
statement (`PROBLEM_STATEMENT_GUJARAT.md:77-79`).

**Why it recurs.** Months-long published lane losses are already routine on
the seeded corridor — Kankaria Road's 3 Jul–30 Sep metro closure with
diversions (Indian Express) and Kalupur Bridge's bullet-train pillar works —
making planned work zones the highest-certainty ROI for corridor tooling
(PIB: 77.63 km approved network).

**In the engine** — `scenario_id: metro_work_zone` (category `ops`):
`work_zone`, an X-class inspection convoy at 09:15 running CTM Road (1031)
through the Kalupur pinch (1014) onto GN Highway south (1024) (seed
20250814) under partial closure, while both work zones hold two barricaded
lanes as **standing, long-duration forced lane-loss** (Kankaria Road 1038 —
a `market_adjacent` segment — and Kalupur Bridge 1014) and diverted bazaar
traffic squeezes Kankaria Road.

---

## 8. The Enforcement Family — Stolen Vehicle → ANPR Sweep → Pursuit

The second mission family, and the reason this repository stopped being
only a corridor tool: the same city backbone (road graph, situation
engine, audit discipline, God's-eye canvas) also carries day-to-day
policing work where the "corridor" is not a scheduled event but a
**vehicle of interest** moving through the city.

**Reality.** A stolen-vehicle FIR is the routine opening of an urban
enforcement morning anywhere in India: a report is registered at a police
station, the plate goes on a watchlist, and recovery depends on the plate
being recognised in traffic — which today means luck, a sharp constable,
or a camera network whose alerts nobody is watching on a shared picture.
Camera-based enforcement (ANPR reads feeding e-challans) is a rolling
national deployment; what is missing is the connective tissue — the
watchlist, the reads, the recommendation and the pursuit sitting on
**one auditable platform with a human in the penalty loop**. *(No
national or state stolen-vehicle count and no specific city deployment
figure is quoted here: none was verified for this library, and the entry
does not need one to make its case.)*

**Why it recurs.** Vehicle theft is a daily, calendar-independent
enforcement workload — and it is the cleanest demonstration of the
platform's governance posture: lawful basis recorded per watchlist entry,
every camera read labelled emulated or live, penalties proposed by
deterministic rules and consummated **only** by a named duty officer, and
pursuits opened by operators, never by the system.

**In the engine** — `scenario_id: watchlist_anpr_sweep` (category
**`enforcement`**, the emulator library's tenth entry,
`services/event-emulator/src/event_emulator/use_case_library.json`):
a stolen-vehicle FIR drives an emulated ANPR camera sweep along the
old-city belt — Jamalpur (1012) up Gandhi Road (1015) to Relief Road
(1005) and Tilak Road (1013) — with the stolen plate (`matched_plate_hit:
stolen_vehicle`) and an earmarked plate among background traffic. Two new
event kinds extend the axis (migration `010_vehicles_watchlist.sql`:
`crime_incident`, `vehicle_pursuit`). The full chain, every step a real
API on the running stack:

1. **Earmark** — `POST /api/v1/vehicles/{plate}/earmark` flags the plate
   with an enumerated `lawful_basis` (`stolen_vehicle_fir`), a reason
   naming the FIR, and a server-computed retention horizon (30 days in
   the flagship; hard cap 90).
2. **ANPR sightings** — emulated camera reads land through the
   `AnprSource` port as `corridor.vehicle_sightings` rows, every one
   born `source='emulated_anpr'` (`traffic_oracle/vehicles/sources.py`;
   the `live_anpr` label is reserved for a real vendor feed — nothing
   writes it in this build).
3. **e-challan recommendation** — `POST /api/v1/enforcement/evaluate`
   runs deterministic rules (`R_SPD_SEGMENT`, `R_SIGNAL_VIOLATION`)
   over each persisted sighting and files **PENDING** recommendations
   only; there is no code path that creates an approved row.
4. **Human approval** — `POST /api/v1/enforcement/recommendations/{id}/decision`
   with a mandatory officer identity flips pending→approved and appends
   to the append-only audit tail. This is the **only** such transition
   in the system.
5. **Pursuit (operator-initiated, C6)** — `POST /api/v1/events` opens a
   `vehicle_pursuit` corridor event whose metadata carries `vehicle_id`
   + `recommendation_id` + the FIR reference; the system never opens a
   pursuit itself.
6. **Interception plan** — `GET /api/v1/events/{id}/interception-plan`
   ranks choke junctions ahead of the vehicle's **latest sighting**:
   one-to-many pgRouting `pgr_drivingDistance` over the live
   `corridor.road_graph` from the sighting's nearest segment, junctions
   scored `choke_weight / (1 + graph_cost_sec)` with fixed weights and
   a junction-id tie-break (`traffic_oracle/vehicles/pursuit.py`).
7. **Live trace** — `GET /api/v1/vehicles/{id}/trace` feeds the
   God's-eye canvas vehicle-traces layer (the pursuit view of the
   flagship screenshots).

**Ethics rails (contract C6), enforced in schema and validators — not
convention:** vehicles only (no person-directed field exists on any
path); every earmark carries a lawful basis and a capped retention
horizon, with sightings reaped past retention **in the read path**; the
emulated badge is carried on every row and rendered on the canvas; and
agencies are dispatchable actors through the same audited, human-approved
channel (migration `011_city_coordination.sql`; Jan Rakshak is
first-class — `traffic_oracle/units/agencies.py`). The full rails are
written up in **[`docs/GODS_EYE_INTEGRATION.md`](GODS_EYE_INTEGRATION.md)**.

---

## The God's-eye canvas — one picture across both families

The mentor's paradigm shift, rendered: if the city's traffic police,
police control, 108 dispatch, fire and Jan Rakshak all act on the same
live picture, the picture itself becomes the platform. The God's-eye
canvas (`services/command-deck/src/lib/gods-eye/`) is a keyless CesiumJS
3D city — OSM raster basemap plus Veer's own 3D entities — embedded as
the centerpiece of the `/ops` board and as a fullscreen `/gods-eye`
cinematic route with sensor modes (FLIR White-Hot / Black-Hot / raw) and
a share link that restores the exact view. Five layer providers are
registered under a ported, integrity-validated registry
(`layers/index.ts`): corridor events, **vehicle traces** (earmarked
vehicles, watch/flagged colours, EMULATED badges), the signal network,
agency units and cameras. Every layer fetches through fixed same-origin
endpoints; an undeployed backend (the cameras API today) renders an
honest "not deployed" chip, never a silent blank. The AMA band chip
surfaces the Active Movement Adaptation module's live city band.
Architecture, the emulated/real split extended to ANPR and dispatch
projection, and the ethics rails: **[`docs/GODS_EYE_INTEGRATION.md`](GODS_EYE_INTEGRATION.md)**.

---

## The flagship: "God's Eye Morning" — the whole platform on one morning

The submission's flagship demonstration (`scripts/showcase_run.sh`, no
arguments) is a **composite of the library** that now spans both mission
families: 09:20 on the real 148th Rath Yatra morning of 27 June 2025 —
three chariots crawling Gandhi Road and Relief Road with lakhs on the
street, a monsoon burst at 18 mm/h waterlogging the Nehru Bridge
approach, bazaar crowds behind Tilak Road barricades — and a 108
ambulance carrying a cardiac patient requesting a green corridor across
the river (Relief Road → Nehru Bridge → Riverfront West → Ashram South,
1005→1015→1006→1001). Into that morning, a **stolen vehicle is reported**
(FIR 4401/2026, Kalupur PS, plate GJ27EM9001) and the full enforcement
chain of entry 8 runs through the same live stack. The engine's answer
for the **public** procession (`protection_class: none`): **signal hold
at 45 s advance — "regulated, not stopped"** — threading a medical
convoy between a festival and the weather, while the watchlist chain
runs alongside.

Re-run end-to-end for this revision by the documentation pass itself
(exit 0; artifacts regenerated 2026-10-13T05:00Z / 10:30 IST:
`showcase/showcase_results.json`, `showcase/pursuit_chain.json`,
`showcase/chain_earmark.json`, `showcase/twin_audit.json`):

| Assertion | Measured (this run) |
|---|---|
| Emulator delivery | flagship 736/736 frames, 0 failed / 0 late (run 41) + ANPR sweep 600 frames (run 42) |
| Decisions logged this run | **36 audit rows** — 30 situation + 6 parameter overrides |
| Band transitions observed | 3 this run (38 total across the ledger; bands AMBER 52 / GREEN 28 / RED 402) |
| Ambulance-delay escalations | 1 `AMBULANCE_DELAY` decision in the escalation channel |
| Green-corridor windows | **4** `emergency_vehicle` windows in the audited twin schedule (Relief Road → Nehru Bridge → Riverfront West → Ashram South) |
| Twin outcome (12-replication seeded model) | ambulance delay mean **7.63 s** — a model output, labelled as such |
| Pursuit chain | earmark `lawful_basis=stolen_vehicle_fir`, retention 30 d → 4 emulated ANPR reads on EM-CAM-01..04 at 107/108/112/108 km/h, `R_SPD_SEGMENT` firing 4× (`matched_plate_hit=stolen_vehicle`) → e-challan "GJ27EM9001 read at 108 km/h in a 50 km/h segment" filed PENDING → **approved by Duty Officer R. Mehta** (append-only audit tail) → operator-opened `vehicle_pursuit` event (metadata carries vehicle_id + recommendation_id + FIR, merged into the artifact from `events.corridor_events` — the POST response echoes metadata as `{}`) → interception plan: **9 choke junctions ranked** off the latest sighting (top: junction 3, signal-controlled, score 0.9) → 56-position live trace, `source=emulated_anpr` |
| Jan Rakshak dispatch | JR-EM-01 to the Tilak Road spillover: requested → approved → en_route → on_scene, every transition audited |
| Signal-policy guard | city band plan proposed for 11 junctions; the C7 shadow-eval guard **refused the commit** (twin_not_calibrated, replay MAE 13.23 km/h vs the 5.0 gate) — fail-closed by design, `signal_plan_shifts_committed: 0`, the refusal itself audited in `GET /api/v1/signals/plans` |
| Screenshots | God's-eye 3D city (mid-chaos, live layers), pursuit trace, citizen advisory — each proven non-blank at the pixel level (SwiftShader WebGL, ≥8 distinct colours) |

---

## What this library is — and is not (the honesty contract)

1. **The engine's expression of each case is a model, not a reconstruction.**
   Routes, demand figures and forced causes are the scenario library's
   seeded, synthetic representation of the documented event class on the
   repo's 40-segment synthetic Ahmedabad seed network
   (`scripts/scenario_library.json`; seed SQL in
   `services/corridor-store/seed/sample_corridor.sql`). No real traffic
   records from any Rath Yatra, marathon, match night, flood — or any
   real FIR, plate or ANPR read — are bundled. The flagship's stolen
   vehicle, its plate and its FIR number are synthetic cast members of
   the scripted morning.
2. **The emulated/real split is plain — and now covers ANPR and dispatches.**
   The event emulator (`services/event-emulator/`) injects **causes
   only** — procession schedules, marathon waves, weather onset, vehicle
   incidents, green-corridor requests — through the real stack's real
   APIs (DB event rows, Valkey inputs docs in the exact engine-loop
   reader shape, one twin POST, and a NATS `corridor.traffic.emulated`
   showcase plane that is **never** `.live`). Since the enforcement
   family landed, the same discipline extends to camera reads (every
   sighting row is born `source='emulated_anpr'`; the `AnprSource` port
   reserves `live_anpr` for a real vendor adapter that does not exist in
   this build) and to agency units (positions are a deterministic
   `emulated_dispatch_projection`, `emulated: true` on every unit). The
   emulator never produces outcomes: no decisions, parameters, situation
   classes — and no approved penalty. Every decision, band transition,
   escalation, green-corridor window and **every pending→approved
   transition** quoted above was produced by the real stack or by a
   named human officer. Live injections are indistinguishable from a
   real feed at the API boundary; replays carry a single
   `X-Emulation-Replay` header (`services/event-emulator/README.md`,
   seams verified live 2026-10-12).
3. **Twin outputs are simulation outputs and are labelled as such.** The
   12-replication Monte-Carlo mean above (ambulance delay 7.63 s) is a
   property of the seeded model under its stated cause set — never
   presented as a field measurement.
4. **External figures are only as good as their sources**, which are named
   per entry (news reports, government portals, utsav.gov.in, PIB, ReliefWeb,
   Wikipedia-citing-UP-government). Where a figure could not be verified —
   Delhi's Kartavya Path closure numbers, any stolen-vehicle statistic —
   the entry says so instead of estimating.
5. **The VVIP base case keeps its own honesty ledger.** The junction-freeze
   impact figures (25×–506×, central 100×) live in `IMPACT_REPORT.md` with
   every assumption falsifiable; the neural forecaster ships as
   architecture only (heuristic forecasts until trained weights are
   exported); and the security veto stays human. See `README.md` and
   `TECHNICAL_DOCS.md` §3/§6.1/§13.
6. **The enforcement family's rails are stated where they are enforced.**
   Vehicles-only, lawful basis + capped retention, read-path reaping,
   pending-only penalties, operator-initiated pursuits: the schema
   CHECKs and validators are quoted in
   [`GODS_EYE_INTEGRATION.md`](GODS_EYE_INTEGRATION.md) §4 — and the
   one backend the canvas waits on (`GET /api/v1/cameras`) honestly
   renders as "not deployed" rather than blank.

---

## Sources

**Problem base**: `PROBLEM_STATEMENT_GUJARAT.md` §1–§2 (with its §6 source
list: ToI Ahmedabad 2009; Vibes of India 2022; Free Press Journal Sept 2022;
Kanchipuram 2018; Agartala Feb 2026; GVK EMRI fleet figures; MHA Blue Book
protocol summary). **Rath Yatra**: NDTV 26 Jun 2025; utsav.gov.in; Gujarat
Samachar Jul 2026. **Kumbh**: Wikipedia / UP government figures (2019: 240 M;
2025: ~660 M visits). **Marathon**: ahmedabadmarathon.com; Olympics.com;
ThePrint; Times of India 5 May 2024. **Republic Day**: Gujarat CMO 25–26 Jan
2025. **Motera**: Wikipedia (capacity); Ayen News (IPL 2026 final
attendance); India Today 8 Mar 2026, Yahoo Sports, Telegraph India (T20 WC
2026 final deployment and advisories). **Floods**: BBC 29 Aug 2024; NDTV;
Free Press Journal; ReliefWeb sitrep 27–28 Aug 2024. **Work zones**:
Wikipedia/Ahmedabad Metro; PIB (77.63 km Phase 2A approval); Indian Express
and India Live Ahmedabad (Kankaria Road closure/diversions); bullet-train
pillar works at Kalupur Bridge.
