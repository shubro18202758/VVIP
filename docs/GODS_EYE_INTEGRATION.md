# The God's-Eye Canvas — Integration, Emulation Split, and Ethics Rails

> The shared picture five hierarchies never had. The God's-eye canvas is
> the city-scale surface where every Veer stream becomes one live
> operational picture — corridor events, the live congestion network,
> earmarked vehicle traces, enforcement, the signal network, agency units,
> dispatches, the convoy, cameras and anomalies — on **real 3D Ahmedabad**
> for a signed-in operator and on a keyless OSM globe for everyone else.
> It is the surface the "God's Eye Morning" flagship showcase renders end
> to end. This document is the integration contract: the canvas
> architecture, the measured layer table, the emulated/real split
> **extended from corridor causes to ANPR reads and agency units**, and the
> ethics rails that make the enforcement family governable (vehicles only,
> human-approved penalties, no fine for a stolen vehicle's owner, retention
> limits).

**Status of this document:** describes shipped code — every claim names
the file it lives in. Measured figures were re-probed for wave 4
(2026-10-23) on private stacks built from this tree (migrations 001–020,
the real-road network) — a fresh install, and for the layer table a
converted copy of the live store; the 3D path is WP-22's — merged into this tree,
canvas component included, and verified on a keyed, signed-in preview of
it (§1) and, after the deploy, on the live stack itself (2026-10-24: the
live acceptance's 3D smoke and the recaptured hero shots, §1 and §5).

---

## 1. What the canvas is

One component (`services/command-deck/src/lib/gods-eye/`), three places:

| Surface | Route | Shape |
|---|---|---|
| Convoy Command | `/` | The cockpit's full-bleed canvas under the planner and escort rails; the escort HUD shows the AI's proposals for the officer to approve |
| Ops wall | `/ops` | The centerpiece panel of the situation wall, beside the AMA module card and the Decision Queue |
| Cinematic fullscreen | `/gods-eye` | Fullscreen canvas + HUD — City Vitals, the layer chips, the AMA band chip, sensor modes, the fusion rail, and a **share link** that restores the exact camera, sensor style and layer set |

**Rendering — two honest modes.** CesiumJS 1.146 is served from the
deck's own `/cesium/` path (vendored, no CDN).

- **Keyless (the default, and every public page).** OSM raster imagery
  (dark re-grade, credited on the globe) + ellipsoid terrain + Veer's own
  entities — zero calls to cesium.com or Google. Chip: `OSM · DARK`.
- **Photoreal 3D (a signed-in operator on an ion-keyed deck — this
  box).** `GET /gods-eye/mode` (`routes/(deck)/gods-eye/mode/+server.ts`)
  reads `VEER_CESIUM_ION_TOKEN` on the server and hands it **only to a
  request that carries a valid operator session** while the gate is on;
  the answer is `cache-control: no-store`, `vary: cookie`. The canvas then
  streams **Google Photorealistic 3D Tiles** (ion asset 2275207) over
  **Cesium World Terrain** (asset 1) with **Bing aerial imagery** (asset 2)
  (`layers/basemap.ts`, `layers/photoreal-3d.ts`). The imagery chip
  names what is on screen (`basemap.ts` `imageryChip()`, EN/HI/GU):
  **`PHOTOREAL 3D · GOOGLE`** once the 3D tiles cover the view
  (`PHOTOREAL 3D · LOADING` while they stream), and the snapshot's
  `imageryMode` reports `ion-photoreal` — also over an imagery fallback,
  because Google's mesh is what is on screen (the canvas's note names
  the fallback). A 3D-tile load that fails for a transient
  reason (no answer, a dropped TLS handshake, a 5xx / 429) is retried at
  5 / 20 / 65 / 130 s; a refusal (401/403/404) fails at once — the 3D
  layer reports itself off and the globe carries the view. If the ion
  IMAGERY fails or does not answer and no 3D tiles cover the view, the
  canvas falls back to the keyless OSM raster and says so
  (`OSM · DARK · FALLBACK`, with the reason). The
  operator pages' CSP admits `http://*.virtualearth.net` because Cesium
  asks Bing for tiles in the page's scheme and the demo deck is plain
  `http://127.0.0.1:5273`; public pages keep the strict policy and never
  receive the token. Google, Cesium ion and OpenStreetMap credits are on
  screen (`showCreditsOnScreen`), on a glass plate lifted into the region
  the host's panels leave clear — on City Command above its fusion rail,
  fully legible.
- **The judge view (City Command on the photoreal city):** the live
  action framed obliquely at −42° (range capped at 4.4 km, so the camera
  stays at most ≈ 2.94 km up; measured 2.3–2.7 km on the merged tree, and
  −42.0° with the camera 2945 m up on the live stack) or, with
  nothing in action, an oblique over the old city and the Sabarmati
  (−40°, ≈ 2 km up) — `GodsEyeCanvas.svelte` `ACTION_VIEW_3D` /
  `HOME_VIEW_3D`; the wall views and the keyless canvas keep their
  framing. While the tiles stand the camera keeps ≥ 80 m from the
  surface (tile collision is off, below).
- **Verified on the merged tree** (a keyed, signed-in preview): the chip
  `PHOTOREAL 3D · GOOGLE`, `imageryMode` `ion-photoreal`, every overlay
  layer 0 floating (enforcement and pursuit included), the credits
  legible above the fusion rail, **0 CSP violations**, and the ion token
  in no anonymous or public answer. **And on the live stack** (acceptance
  step 17, 2026-10-24 00:08 IST, `tests/gods-eye-3d-smoke.mjs`, signed
  in): the same chip and mode; the drape audit draped / clamped /
  floating — events 32 / 11 / 0, flow 49 / 0 / 0, traces 1 / 9 / 0,
  enforcement 0 / 11 / 0, signals 0 / 11 / 0, cameras 0 / 28 / 0,
  anomalies 0 / 30 / 0, the pursuit overlay 5 / 6 / 0; 0 CSP violations
  and 0 page errors on `/gods-eye`, `/` and `/ops`; the Google, Cesium ion
  and OpenStreetMap credits on screen (on `/` the whole credit bar clear
  of the tool dock); no frame over 3 s under SwiftShader (median 567 ms).

**The overlays stand on the city** (`layers/draping.ts`, WP-22). Lines —
the congestion ramp, event spines (RECOMMENDED glow, dashed alternates,
superseded routes), vehicle and pursuit traces, the pursuit fan and arc,
dispatch arrows and rings, the convoy track and detours — are DRAPED
(`clampToGround`, classification type BOTH: on the terrain and on the 3D
tiles), their draw order set by `zIndex`, readable from 1.5–3 km. Markers,
dots and labels — signal rings, units, cameras, anomalies, the convoy
marker, the pursuit's sighting and chokes, the trace hops, the
e-challan pins (fanned out per camera site) — are clamped to the terrain
at street level with the depth test off, so no building hides them;
labels get a small eye offset. The selection halo is a draped glow ring
(95 m, re-laid when its subject has moved 20 m), the AI's agent notes are
clamped labels, and the REPLAY of a recorded emulator run (`replay.ts`,
`hud/replay.svelte.ts`) drapes its camera rings and clamps its tracks.
Every layer reports a drape audit (`drapeAudit()`: draped / clamped /
floating) in `layerStats`, and on the 3D city every overlay layer reads
**0 floating**. Tile collision is off (WP-22's measurement under
SwiftShader: clamping to the tiles cost 134 height queries in 30 s,
12.3 s of work and 4.9 s frames; off, zero tile queries and no frame
over 400 ms); the tiles render at
screen-space error 24 over the globe kept beneath them at 8. The same code
draws the keyless canvas — the ground decides, not the mode.

**Sensor modes.** One FLIR-class thermal fragment shader (`shaders/flir.ts`
— Ironbow palette, coherent temporal noise, hot-spot bloom, lens vignette,
sensor pixelation) cycles **White-Hot → Black-Hot → raw** from the HUD
chip — a documented source-level port from `gods-eye-view` (MIT ©
bilawalsidhu); the port ledger is `lib/gods-eye/NOTICE.md`.

**Share links.** `share-link.ts` encodes camera + sensor + layer state into
the URL hash with non-finite guards on parse — a Jan Rakshak field unit or
a police control room opens the exact view the sender saw.

## 2. The layer architecture and the measured layer table

Every data source on the canvas is a **layer** registered in one array
(`layers/index.ts`) and validated by the registry's integrity rules
(`registry.ts`: id slug grammar, duplicate ids, pick-prefix overlap — the
`gods-eye-view` provider pattern). Every layer maps its picks to C-C URNs,
so a click selects the same entity the fusion rail and the dossier
address; the data client (`data.ts`, `city-api.ts`) is the security
contract — every URL a fixed same-origin constant or built from validated
ids, never assembled from external input. A 404 renders an honest "not
deployed" chip, never a silent blank.

**Eleven layers, chip order, measured** — `window.__GODS_EYE__.layerStats`
read on `/gods-eye` of a gate-off, keyless preview of this tree's merged
deck at 2026-10-23T17:41Z, over a converted copy of the live store (the
private clone stack: the stolen plate's sweep, its pending sightings and
the old pursuit are on it). The network-bound counts (flow, signals,
cameras) are fixed by the network; the others move with the city. The
same read's drape audit shows every overlay layer at **0 floating** on
the keyless canvas too:

| # | Layer (`id`, file) | Draws | Feed | Measured | Provenance on screen |
|---|---|---|---|---|---|
| 1 | Corridor events (`events`, `corridor-events.ts`) | every live corridor event's spines — RECOMMENDED glow, alternates, superseded routes, zones — and its marker, ON its rank-1 route (the route point nearest the route's centroid, `position_basis: spine_point_nearest_centroid`; a zone centroid only for an event with no route) | the ops board / `GET /api/v1/events/active` | 1 event (the open pursuit), 4 spine lines draped, its marker clamped | EMULATED on emulator / test rows |
| 2 | Traffic flow (`flow`, `traffic-flow.ts`) | the congestion ramp on every road segment | `POST /api/v1/traffic/live` over the 49 segments | **49 of 49 coloured, all 49 `osm` (0 schematic)** | **SIM** — all 49 on the synthetic ingress feed; EMULATED for the emulator's plane |
| 3 | Vehicle traces (`traces`, `vehicle-traces.ts`) | earmarked vehicles' timed traces with numbered camera hops | `GET /api/v1/vehicles` + `/{id}/trace` (lawful reads only) | 1 earmarked vehicle, 8 camera hops | EMULATED reads (`emulated_anpr`); plates masked |
| 4 | Enforcement (`enforcement`, `enforcement.ts`) | recommendation pins at their reads — e-challans and stolen-vehicle sightings (`STOLEN · NO FINE`; a pending penal row of a stolen vehicle `WITHHELD · STOLEN`; one approved before the stolen-vehicle rule `SUPERSEDED · STOLEN`) | city fusion API | 10 pins — 6 pending stolen-vehicle sightings, 3 penal rows WITHHELD · STOLEN, 1 legacy approved e-challan (read `SUPERSEDED · STOLEN` by today's canvas) | e-challan withheld on stolen plates; plates masked |
| 5 | Signal network (`signals`, `signal-network.ts`) | each programmed junction's ring — green arc by green split, red in lost time | `GET /api/v1/signals/state` | **11 programs** (7 green · 1 amber · 3 red at capture) | `osm_assumed` programs, labelled |
| 6 | Agency units (`units`, `agency-units.ts`) | Jan Rakshak / police / traffic / 108 / fire units with STATUS · ETA | `GET /api/v1/units` | 0 | EMULATED (`emulated_dispatch_projection`, `emulated_patrol`) |
| 7 | Dispatch (`dispatch`, `dispatch.ts`) | dispatch arrows from post to waypoint, rings at the target — a dispatch whose officer named no waypoint at its event's marker (`position_basis: event_marker`) | city fusion API | 0 | EMULATED projection |
| 8 | Convoy (`convoy`, `convoy.ts`) | the escorted convoy's marker, track and detour previews | `convoy:position:{id}` via the fusion API | 0 | `emulated_gps` labelled |
| 9 | Cameras (`cameras`, `cameras.ts`) | ANPR / CCTV camera SITES (location + identity only), pulsing on a hit | `GET /api/v1/cameras` | **28 sites** (none reporting in the last 15 min) | EMULATED sites |
| 10 | Anomalies (`anomalies`, `anomalies.ts`) | Tier-1 anomaly pulses on their segments | city fusion API | 24 | **SIM** — every one raised on the simulated feed (`data_quality: synthetic`) |
| 11 | Photoreal 3D (`photoreal-3d`, `photoreal-3d.ts`) | Google Photorealistic 3D Tiles — scenery, never a pick | ion (keyed only) | 0 (keyless preview: unconfigured) | Google / Cesium ion credits |

At that moment the fusion registry held 106 city entities (34 junctions,
28 cameras, 24 anomalies, 10 recommendations, 8 sightings, 1 vehicle,
1 event). On the live stack the same read is `layerStats` on a signed-in
`/gods-eye`, where layer 11 reports the photoreal tiles loaded.

**The AMA chip.** The canvas carries the Active Movement Adaptation band
chip — the current band, its age, and the source rung (`situation_log`
primary → `adaptive/status` fallback) with SIMULATED FEED when the band
stands on the synthetic feed — and City Vitals shows the city posture,
pending human decisions (e-challans, sightings, dispatches, 108 requests,
escort proposals), units out and the ethics rails. The canvas *surfaces*
AMA; it re-implements none of it (`lib/gods-eye/AMA.md`).

## 3. The emulated/real split, extended to ANPR and units

The platform's founding honesty rule — the emulator injects **causes
only**, the real stack produces every outcome — covers three feed
families, each with the same shape: a frozen contract, one labelled
emulated implementation, and a documented (not shipped) live path.

### 3.1 Corridor causes (the original split)

The event emulator injects corridor events through the C-F lifecycle,
adaptive inputs docs, twin configs and a NATS showcase plane that is never
`.live` — the oracle caches it in its own key space, so emulated traffic
can never drive a real event's band (`TECHNICAL_DOCS.md` §2.9). Every row
on that plane is labelled `data_quality` SYNTHETIC (2) — ANOMALOUS (1)
only as the congestion-outlier marker, never REAL (0) — and sits on its
segment (the midpoint along the line); every emulated movement starts and
ends at its route's ends on the road.

### 3.2 ANPR camera reads

- **The seam (contract C-E)**: `POST /api/v1/vehicles/sightings` takes one
  read `{read_id, plate, camera_id, t, lon, lat, speed_kmh, heading,
  vehicle_class, source}`; plates are normalized server-side, the call is
  idempotent on `read_id`, and the ORACLE computes the hit from its
  active earmarks and runs the rules — the emulator asserts neither.
- **The port** (`traffic_oracle/vehicles/sources.py`): an `AnprSource`
  yields `AnprRead` records; `source` is exactly `emulated_anpr` or
  `live_anpr` (the migration-010 CHECK).
- **What ships**: emulated reads only. The emulator's tapes and the living
  city generate them deterministically (uuid5 identity per run, so a
  replay upserts instead of forking); **every row is born
  `source='emulated_anpr'`**, and every synthetic plate carries the EM
  series marker. A `live_anpr` read is refused unless
  `VVIP_ACCEPT_LIVE_ANPR=1`.
- **The documented live path (not shipped)**: a real ANPR vendor feed —
  the state's ICCC / CCTV / ITMS cameras, registered as
  `corridor.cameras` sites with `source = registered` — → an adapter
  normalizing to the identical read schema → `source='live_anpr'` →
  identical downstream: sightings, deterministic rules, human approval.
- The emulated sweep is a first-class use case: `watchlist_anpr_sweep`
  (category `enforcement`) — a stolen-vehicle FIR driving camera reads
  around the walled city. Before its first read it makes its premise
  explicit: a `stolen_vehicle_fir` earmark on GJ27EM9001 labelled
  `event-emulator (system) — scenario precondition` / EMULATED, so its
  reads become evidence-only stolen-vehicle sightings, never fines
  (`services/event-emulator/src/event_emulator/runner.py`). Its second
  plate, GJ27EM4321, is an operator exercise: the emulator never earmarks
  it, so its reads hit only after a duty officer earmarks it — on a fresh
  install they are ordinary sightings (the library text and the stored
  profile say so).
- **Reads stay on the roads.** When the WP-20 network step moved a
  store's segments onto OpenStreetMap it carried each EMULATED read
  (`source='emulated_anpr'`) to the same fraction along its segment on
  the new line — in `corridor.vehicle_sightings` and in the run's
  recorded tape, which a REPLAY draws — positions only, never a decision
  or an audit trail; a `live_anpr` read is never moved
  (`seed/reanchor_osm_network.sql` section 0).

### 3.3 Agency units

Two emulated sources, both badged at the row level (`units/router.py`
`GET /api/v1/units`): **`emulated_dispatch_projection`** — a dispatched
unit's position is a pure function of `(dispatch_id, approved_at, now)`
(`units/projection.py`: a seeded speed inside a 32–48 km/h response band,
projected along the graph path to the waypoint junction itself, so a unit
is never "arrived" at t=0) — and **`emulated_patrol`** — emulator patrol
frames on NATS `corridor.units.emulated` → Valkey `units:emulated:{id}`
(contract C-A, EX 30). Only units of a still-live event project; a closed
event releases them. The documented live seam: real AVL frames take
precedence and the projection becomes the interpolation between them.

**Why this matters for the demo numbers:** because ANPR reads, unit
positions and the traffic feed are labelled at the row level, every
number quoted in the showcase is traceable to rows that say what they are.

## 4. The ethics rails (contracts C6 + C-N, enforced in schema and code)

The enforcement family exists to **strengthen the state's capability** —
its ICCC / CCTV programmes, its police and its emergency services — and
is bounded by rails that live in the migration CHECKs and the request
validators, not in convention:

1. **Vehicles only — via ANPR plates, never faces or persons.** A read
   carries a plate and kinematics; no face, person name, or
   person-directed field exists on any path (`vehicles/sources.py`
   ETHICS header). Camera SITES carry location and identity only — no
   frame, image or stream column (migration 012). The agency registry is
   agencies and unit ids — "never persons" (`units/agencies.py`); the
   interception planner keys on the vehicle's sightings and the road graph
   (`vehicles/pursuit.py`).
2. **Penalties are recommendations until a human approves.** Rows are
   created `pending` **only** — "there is no code path that creates an
   approved row"; the single transition is `POST
   /api/v1/enforcement/recommendations/{id}/decision`, which requires a
   named officer (the deck's verified `x-veer-officer` when signed in) and
   appends an append-only `audit_tail` entry (`enforcement/__init__.py`).
   The rules are deterministic (`enforcement/rules.py` — `R_SPD_SEGMENT`,
   `R_SIGNAL_VIOLATION`, `R_CLOSED_CORRIDOR_ENTRY`): the machine proposes,
   a duty officer disposes.
3. **No fine, ever, for a reported-stolen vehicle's owner (C-N).** Under an
   active `stolen_vehicle_fir` earmark a read files ONE evidence-only
   `R_STOLEN_SIGHTING` (the penal rules it would have fired recorded as
   withheld); approving any penal rule on that vehicle answers **409
   "e-challan withheld — vehicle reported stolen"**; approving the
   sighting is the authorised basis of a pursuit. On the canvas a
   sighting's pin reads `STOLEN · NO FINE` (with its state), a pending
   penal row of that vehicle `WITHHELD · STOLEN`, and an e-challan
   approved before the stolen-vehicle rule `SUPERSEDED · STOLEN` — a dim
   pin, kept as audit history (decisions are final), never a pursuit
   basis (`layers/enforcement.ts`; the hero pursuit shot of §5 shows both
   kinds).
4. **Retention limits, enforced on read.** An earmark requires an
   enumerated `lawful_basis` (MV Act 1988 s.177/s.183/s.207, BNSS 2023
   s.94, `stolen_vehicle_fir`) and a `retention_days` capped at **90**;
   `retention_until` is computed server-side. Sightings without an active
   earmark are kept 30 days and reaped by a throttled, parameter-bound
   DELETE in the read path; the reaper's health is public at `GET
   /api/v1/vehicles/retention` and on City Vitals.
5. **One lawful-read rule (C6 parity).** A single camera read may be shown
   only when its vehicle holds an active earmark or the read is evidence
   for a recommendation — one predicate (`city/common.py`
   `SQL_LAWFUL_READ`) composed by `/sightings`, `/trace`, the feed, the
   camera hit list, the dossier and the interception plan; anything else
   is a count-free 404.
6. **Plates are masked until a named officer is signed in** — `GJ27EM•••1`
   on every canvas label, dossier, feed row and board title
   (`lib/gods-eye/plates.ts`, mirrored by the oracle's own masking).
7. **Pursuits are human-initiated.** The system never opens a pursuit: an
   operator converts a human-approved recommendation + an active earmark
   into a `vehicle_pursuit` event whose metadata carries `vehicle_id` and
   `recommendation_id` — for a reported-stolen car that recommendation is
   its approved `R_STOLEN_SIGHTING`, **never an e-challan** (the oracle
   answers 422 to a pursuit opened from one, and the `/ops` pursuit card
   follows the same rule). **One officer approval authorises one
   pursuit**: a second pursuit from an approval that already authorised
   one answers 422; every recommendation row the oracle lists carries the
   `pursuit` it authorised, and `/ops` never offers an approval spent on a
   closed pursuit as the next basis (`events/__init__.py`
   `_validate_pursuit`, `enforcement/__init__.py`, `ops/client.ts`
   `approvalSpent`). The
   interception plan "assists the operator, it never dispatches on its
   own", and a unit moves only after a named officer approves its
   dispatch.
8. **The emulated badge is never optional.** Every sighting, unit, camera
   site and feed row carries its source; the canvas renders EMULATED /
   SIMULATED FEED wherever it applies.

## 5. Where the canvas is proven

`scripts/showcase_run.sh` — the **"God's Eye Morning"** flagship — proves
the canvas against the live stack and leaves pixel-level evidence:
headless SwiftShader WebGL screenshots of the city (mid-chaos, live
layers), the pursuit-trace view and the citizen advisory, each proven
non-blank by a saved-PNG reload decode — the two canvas shots also by a
live-canvas distinct-colour readback (≥8 distinct colours;
`showcase/showcase_results.json` `screenshot_proofs`); the `/ops` wall is
the same camera's separate `--ops-only` shot. **It ran live on the deployed stack on the night of
2026-10-23/24 — SHOWCASE PASS three times** (the artifacts on disk are the
repetition gate's last round, generated 2026-10-23T18:31Z = 00:01 IST:
6 emulated reads → 6 evidence-only sightings, an e-challan approval
refused 409, the sighting approved by `Scripted demo officer (showcase
script)`, 10 choke junctions ranked, a 12-position trace — its 24 h
window held these 6 reads and the 23:51 run's 6 — no fine anywhere;
`TECHNICAL_DOCS.md` §13). The four hero screenshots on disk were
recaptured on 2026-10-24 at 01:25–01:26 IST on the live stack in its
clean, staged judging state — the three operator shots on the photoreal
3D city (`PHOTOREAL 3D · GOOGLE`): the city at rest with the flow network
draped, the pursuit trace (its pins `STOLEN · NO FINE · APPROVED` and the
pre-rule e-challan's `SUPERSEDED · STOLEN`) and the `/ops` wall; and the
public citizen advisory on its keyless map (no pursuit, no plate)
(`scripts/showcase_canvas_shots.mjs`; `--ops-only` takes the wall alone
and merges it into `shots_manifest.json`). The pursuit chain the canvas
renders is: earmark (lawful basis + 30-day retention) → emulated
ANPR sightings → evidence-only `R_STOLEN_SIGHTING` (PENDING; an e-challan
on the plate refused, 409) → **a named officer approves the sighting** →
operator-opened pursuit (one approval, one pursuit; an e-challan basis
refused, 422) → pgRouting interception plan (choke junctions ranked off
the latest sighting) → a dispatch to choke #1 that a named officer
approves → the live trace on the vehicle-traces layer.
`scripts/journeys.mjs` J2 walks the same chain through the deck's own
proxy (23 checks — among them the 409 on the e-challan and the 422 when
a pursuit is asked for on that dismissed e-challan; 23/23 on the live
deck, 2026-10-24).

**REPLAY** draws a recorded emulator run on the canvas clock from its
stored tape (it never re-delivers anything to the stack). A run recorded
before the WP-20 conversion is history: its tape's ANPR reads were
carried onto the real roads by the network step, but its patrol-unit
frames (an older tape format) were not, and the emulator refuses to
re-animate such a run server-side (422 `route_not_on_current_network` —
its recorded route is no longer a legal path). Replay a run recorded on
the current network for the judges (`CC_REPLAY_RUN` in
`tests/city-command-smoke.mjs` names the one its smoke replays).

## 6. Honest gaps, stated

- **All ANPR reads, camera sites and agency units are emulated** in this
  build; nothing writes `live_anpr` (that is the point of the split — the
  live adapter has a frozen contract to land in).
- **The traffic feed is synthetic.** signal-ingress runs edge-synthetic
  collectors (every row `data_quality: synthetic`); the flow and anomaly
  layers say SIM accordingly.
- **The signal-policy writer refuses to commit** while the twin is not
  field-calibrated (≥ 24 real field hours and replay MAE ≤ 5 km/h; a fresh
  install has none) — the canvas shows the refusal, not a pretend write.
- **The 3D path needs the ion token and a signed-in operator.** On this
  host one `api.cesium.com` CDN edge refuses TLS, so a browser session
  that resolves to it loses the ion imagery (honest FALLBACK note) while
  the 3D tiles retry and usually land.
- Our DSTGAT forecaster still ships as architecture only (no trained
  weights; `TECHNICAL_DOCS.md` §6.1) — forecasts come from the third-party
  t0-alpha foundation rung or the heuristic rung (§6.1b), and the flow
  layer's congestion is the live (synthetic) feed itself, labelled SIM.

## 7. File map

| Concern | Where |
|---|---|
| Canvas component + HUD | `services/command-deck/src/lib/gods-eye/GodsEyeCanvas.svelte`, `hud/` |
| Fullscreen route | `services/command-deck/src/routes/(deck)/gods-eye/+page.svelte` |
| Imagery mode endpoint (keyless / ion, session-gated) | `services/command-deck/src/routes/(deck)/gods-eye/mode/+server.ts` |
| CSP and the operator gate | `services/command-deck/src/hooks.server.ts`, `src/lib/shell/operator-session.server.ts` |
| Layer registry + integrity rules | `services/command-deck/src/lib/gods-eye/registry.ts` |
| The eleven layers + basemap + draping | `services/command-deck/src/lib/gods-eye/layers/` (`index.ts`, `basemap.ts`, `photoreal-3d.ts`, `draping.ts`, …) |
| Data clients (fixed endpoints) | `services/command-deck/src/lib/gods-eye/data.ts`, `city-api.ts` |
| Plate masking | `services/command-deck/src/lib/gods-eye/plates.ts` |
| FLIR shader / share links / notices | `shaders/flir.ts`, `share-link.ts`, `NOTICE.md` |
| AMA surfacing note | `services/command-deck/src/lib/gods-eye/AMA.md` |
| ANPR port + emulated source | `services/traffic-oracle/src/traffic_oracle/vehicles/sources.py` |
| Watchlist API + the C-E ingest seam | `services/traffic-oracle/src/traffic_oracle/vehicles/__init__.py` |
| Enforcement API (rules → pending → approval; C-N) | `services/traffic-oracle/src/traffic_oracle/enforcement/__init__.py`, `rules.py`, `engine.py` |
| Interception plan (pgRouting) | `services/traffic-oracle/src/traffic_oracle/vehicles/pursuit.py` |
| City fusion + the lawful-read rule | `services/traffic-oracle/src/traffic_oracle/city/` |
| Agency registry + unit projection | `services/traffic-oracle/src/traffic_oracle/units/agencies.py`, `projection.py`, `router.py` |
| Schema rails (migration CHECKs) | `services/corridor-store/migrations/010_vehicles_watchlist.sql`, `011_city_coordination.sql`, `012_city_assets_graph_integrity.sql`, `017_enforcement_stolen_sighting.sql` |
| The road network the canvas draws | `services/corridor-store/seed/` (generated by `scripts/gen_osm_network.py`; © OpenStreetMap contributors, ODbL 1.0) |
| Emulated sweep use case | `services/event-emulator/src/event_emulator/use_case_library.json` (`watchlist_anpr_sweep`) |
