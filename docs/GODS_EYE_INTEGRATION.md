# The God's-Eye Canvas — Integration, Emulation Split, and Ethics Rails

> The shared picture five hierarchies never had. The God's-eye canvas is
> the city-scale 3D surface where every Veer stream becomes one live
> operational picture — corridor events, earmarked vehicle traces, the
> signal network, agency units and cameras on one keyless CesiumJS globe —
> and it is the surface the "God's Eye Morning" flagship showcase renders
> end to end. This document is the integration contract: the canvas
> architecture, the emulated/real split **extended from corridor causes to
> ANPR reads and agency dispatches**, and the ethics rails that make the
> enforcement family governable (vehicles only, human-approved penalties,
> retention limits).

**Status of this document:** describes shipped code — every claim names
the file it lives in. Live endpoint states quoted were re-probed against
the running fleet during this revision (2026-10-13, IST).

---

## 1. What the canvas is

Two surfaces, one component
(`services/command-deck/src/lib/gods-eye/`):

| Surface | Route | Shape |
|---|---|---|
| Ops board embed | `/ops` | The canvas is the centerpiece panel of the operations board alongside the AMA module card (`AMAModuleCard.svelte`) and the situation-band panel |
| Cinematic fullscreen | `/gods-eye` | Fullscreen canvas + HUD — sensor-mode cycle, layer chips, the AMA band chip, and a **share link** that restores the exact camera, sensor style and layer set (`routes/gods-eye/+page.svelte`) |

**Rendering.** CesiumJS loaded from the deck's own `/cesium/` asset path
(no CDN). **Keyless by default**: OSM raster imagery (ODbL, credited in
the HUD) + ellipsoid terrain + Veer's own 3D entities — extruded corridor
polygons, event markers, vehicle traces, signal dots, unit markers. An
automatic photorealistic upgrade exists but stays off unless
`VEER_CESIUM_ION_TOKEN` is set server-side
(`routes/gods-eye/mode/+server.ts`: the token is read from the env on the
server and never appears in source; without it, zero cesium.com calls).

**Sensor modes.** One FLIR-class thermal fragment shader
(`shaders/flir.ts` — Ironbow palette, coherent temporal noise, hot-spot
bloom, lens vignette, sensor pixelation) cycles **White-Hot → Black-Hot →
raw** from the HUD chip. The shader is a documented source-level port
from `gods-eye-view` (MIT © bilawalsidhu) — the full port ledger,
including what was trimmed, is `lib/gods-eye/NOTICE.md`.

**Share links.** `share-link.ts` encodes camera + sensor + layer state
into the URL hash (`#v=2&lat=..&style=flir&layers=…`) with non-finite
guards on parse — a Jan Rakshak field unit or a police control room opens
the exact view the sender saw.

## 2. The layer architecture (the extension contract)

Every data source on the canvas is a **layer provider** registered in one
array (`layers/index.ts`), validated by the registry's integrity rules
(`registry.ts`: id slug grammar, duplicate ids, pick-prefix overlap — the
`gods-eye-view` provider pattern, ported and documented). Five layers are
registered today, in HUD chip order:

| Layer | File | Feed (fixed same-origin endpoints) | State today |
|---|---|---|---|
| Corridor events | `layers/corridor-events.ts` | events + segments APIs | live |
| **Vehicle traces** | `layers/vehicle-traces.ts` | `GET /api/v1/vehicles` + per-vehicle `GET /api/v1/vehicles/{id}/trace?window_sec=900` (max 12 traces per refresh, bounded on purpose) | live |
| Signal network | `layers/signal-network.ts` | `GET /api/v1/signals/state` | live |
| Agency units | `layers/agency-units.ts` | `GET /api/v1/units` | live |
| Cameras | `layers/cameras.ts` | `GET /api/v1/cameras` | **not deployed** — the endpoint 404s and the chip honestly says so |

The data client (`data.ts`) is the security contract: every URL is a
fixed constant from `src/lib/ops/endpoints.ts` or built from
`safePathId`-validated ids — layer code never assembles URLs from
external input. A 404 raises `NotDeployed` so a layer renders the honest
"not deployed" chip state rather than an error; the cameras layer is
exactly that case **in this build** (re-probed live this revision:
`GET /api/v1/cameras` → 404).

**The AMA chip.** The canvas carries the Active Movement Adaptation band
chip — current city band, last-transition age, and the source rung
(`situation_log` primary → `adaptive/status` fallback), fed by the same
ops-store ladder the `/ops` board uses. The canvas *surfaces* AMA; it
re-implements none of it (`lib/gods-eye/AMA.md`).

## 3. The emulated/real split, extended to ANPR

The platform's founding honesty rule — the emulator injects **causes
only**, the real stack produces every outcome — now covers three feed
families, each with the same shape: a frozen port, one labelled emulated
implementation, and a documented (not shipped) live path.

### 3.1 Corridor causes (the original split)

The event emulator injects event rows, adaptive inputs docs, twin configs
and a NATS showcase plane that is never `.live`
(`TECHNICAL_DOCS.md` §2.9). Unchanged.

### 3.2 ANPR camera reads (the extension)

- **The port** (`traffic_oracle/vehicles/sources.py`): an `AnprSource` is
  anything that yields `AnprRead` records — one camera read of one plate.
  The persistence layer consumes reads **through this port only**; it
  never talks to a vendor SDK, camera, or file format.
- **The frozen frame schema (contract C2)**: `AnprRead` carries
  `read_id / plate / camera_id / t / lon / lat / source / speed_kmh /
  heading / vehicle_class / run_id / matched_plate_hit` — and validates
  `source` against exactly two values (the migration-010 CHECK):
  `emulated_anpr` or `live_anpr`.
- **What ships**: `EmulatedAnprSource` is the only implementation. It
  replays the `anpr_read` frames of a cause tape generated by the event
  emulator (deterministic uuid5 identity over use-case+seed+camera+plate,
  so replays upsert sightings idempotently instead of forking rows), and
  **every row is born labelled `source='emulated_anpr'`** — the badge
  cannot be dropped by any view, because every sightings/trace response
  carries the source field (`vehicles/__init__.py`).
- **The documented live path (not shipped)**: a real ANPR/VMS vendor feed
  → adapter normalizing to the identical `AnprRead` schema →
  `source='live_anpr'` on every row → **identical downstream**: sightings,
  deterministic rules, human approval. Until a real feed exists, nothing
  in this codebase writes `live_anpr` (`vehicles/sources.py`: "nothing in
  this codebase writes `source='live_anpr'`").
- The emulated sweep itself is a first-class use case:
  `watchlist_anpr_sweep` (category `enforcement`) in the emulator's
  library — a stolen-vehicle FIR driving camera reads along the old-city
  belt, every synthetic plate carrying an EM segment, every sighting
  landing `source=emulated_anpr`
  (`services/event-emulator/src/event_emulator/use_case_library.json`).

### 3.3 Agency dispatches (the same split, for units)

A dispatched unit's rendered position is a **pure function of**
`(dispatch_id, approved_at, now)` — `units/projection.py`: blake2b-seeded
speed inside a 32–48 km/h response band and a small lateral bearing
offset, projected along the graph path. Every surfaced unit carries
`emulated: true` + `source='emulated_dispatch_projection'`
(`units/router.py` `GET /api/v1/units`). The documented live seam: when
live `agency_unit` frame ingest lands, observed frame positions take
precedence and the projection becomes the interpolation between frames.

**Why this matters for the demo numbers:** because the ANPR reads and the
unit positions are labelled at the row level, every pursuit-chain number
quoted in the showcase is traceable to rows that say what they are. A
reviewer can open `showcase/pursuit_chain.json` and see
`source: emulated_anpr` on every sighting feeding the interception plan.

## 4. The ethics rails (contract C6, enforced in schema and code)

The enforcement family exists to **strengthen government capability** —
and is bounded by rails that live in the migration CHECKs and the request
validators, not in convention:

1. **Vehicles only — never persons.** An `AnprRead` carries a plate and
   kinematics; no face, person name, or person-directed field exists on
   any path (`vehicles/sources.py` ETHICS header). The watchlist tables
   key on `corridor.vehicles` (plate, vehicle_class); the agency registry
   is agencies and unit ids — "agencies and units — never persons. No
   name/face fields" (`units/agencies.py`). The interception planner
   states it again: "everything keys on the vehicle's sightings and the
   road graph; no person-directed data exists on this path"
   (`vehicles/pursuit.py`).
2. **Penalties are recommendations until a human approves.** Enforcement
   rows are created `pending` **only** — "there is no code path that
   creates an approved row"; the single transition is
   `POST /api/v1/enforcement/recommendations/{id}/decision`, which
   requires a human `officer` identity (min 2 chars, validated) and
   appends an append-only JSONB `audit_tail` entry (creation, decision,
   actor, timestamp) (`enforcement/__init__.py` APPROVAL MODEL). The
   rules themselves are deterministic (`enforcement/rules.py` —
   `R_SPD_SEGMENT`, `R_SIGNAL_VIOLATION`), so the machine proposes and a
   duty officer disposes.
3. **Retention limits, enforced on read.** An earmark requires an
   enumerated `lawful_basis` (NOT NULL; the accepted codes are framed on
   Indian enforcement law — MV Act 1988 s.177/s.183/s.207, BNSS 2023
   s.94, `stolen_vehicle_fir`) and a `retention_days` capped at **90**
   (mirrored by the migration-010 CHECK); `retention_until` is computed
   **server-side** — a client cannot stretch it. Sightings past retention
   are reaped by parameter-bound DELETE in the read path, never promised
   by a cron; default sighting horizon 30 days (`vehicles/__init__.py`,
   `vehicles/store.py` `REAP_*_SQL`). Expired earmarks are filtered in
   the query path.
4. **Pursuits are human-initiated.** The system never opens a pursuit
   itself: an operator converts an approved recommendation + earmark into
   a `vehicle_pursuit` corridor event whose metadata carries `vehicle_id`
   and `recommendation_id` (`vehicles/pursuit.py` header, C6). The
   interception plan "assists the operator, it never dispatches on its
   own" — the API response says so in band.
5. **The emulated badge is never optional.** Every sighting response
   carries its `source`; the canvas vehicle-traces layer renders an
   EMULATED badge on synthetic reads (`layers/vehicle-traces.ts`); unit
   markers carry `emulated: true`.

## 5. Where the canvas is proven (the flagship)

`scripts/showcase_run.sh` — the **"God's Eye Morning"** flagship — proves
the canvas against the live stack and leaves pixel-level evidence:
headless SwiftShader WebGL screenshots of the 3D city (mid-chaos, live
layers) and the pursuit-trace view, each proven non-blank by
distinct-colour readback plus saved-PNG reload decode (≥8 distinct
colours; method recorded in `showcase/showcase_results.json`
`screenshot_proofs`). The pursuit chain the canvas renders, end to end:

earmark (lawful basis + 30-day retention) → emulated ANPR sightings →
deterministic e-challan recommendation (PENDING) → **duty-officer
approval** (the only pending→approved transition in the system) →
operator-opened pursuit event → pgRouting interception plan (choke-junction
ranking off the latest sighting) → live trace on the canvas vehicle-traces
layer. Numbers and artifact pointers: `docs/USE_CASES.md` (the flagship
section) and `showcase/showcase_results.json`. Artifact fidelity: the
run's `showcase/pursuit_chain.json` carries the pursuit event's
**persisted** metadata (vehicle_id + recommendation_id + FIR) merged from
`events.corridor_events` — the creation POST's response echoes metadata as
`{}`, so the showcase script merges the authoritative row into the
artifact (`scripts/showcase_run.sh`, phase 6).

## 6. Honest gaps, stated

- **`GET /api/v1/cameras` is not deployed** (404; re-probed live this
  revision). The cameras layer therefore renders its honest "not deployed"
  chip — the registry pattern makes a missing backend a visible state,
  never a silent blank.
- **The signal-policy writer refuses to commit** while the twin is
  uncalibrated (C7 fail-closed; the showcase records the guard refusing
  with `twin_not_calibrated` and the honest replay MAE). This is the
  designed posture, quoted as such — the canvas shows the refusal, not a
  pretend write.
- **All ANPR reads are emulated** in this build; nothing writes
  `live_anpr` yet (that is the point of the split — the live adapter has
  a frozen contract to land in).
- The neural forecaster still ships as architecture only (no trained
  weights; see `TECHNICAL_DOCS.md` §6.1) — canvas congestion states come
  from the live heuristic stack, labelled as such.

## 7. File map

| Concern | Where |
|---|---|
| Canvas component + HUD | `services/command-deck/src/lib/gods-eye/GodsEyeCanvas.svelte` |
| Fullscreen route | `services/command-deck/src/routes/gods-eye/+page.svelte` |
| Imagery mode endpoint (keyless/ion) | `services/command-deck/src/routes/gods-eye/mode/+server.ts` |
| Layer registry + integrity rules | `services/command-deck/src/lib/gods-eye/registry.ts` |
| Registered layers | `services/command-deck/src/lib/gods-eye/layers/` (index + 5 layers) |
| Data client (fixed endpoints, NotDeployed) | `services/command-deck/src/lib/gods-eye/data.ts` |
| FLIR shader / share links / notices | `shaders/flir.ts`, `share-link.ts`, `NOTICE.md` |
| AMA surfacing note | `services/command-deck/src/lib/gods-eye/AMA.md` |
| ANPR port + emulated source | `services/traffic-oracle/src/traffic_oracle/vehicles/sources.py` |
| Watchlist API (earmarks/sightings/traces) | `services/traffic-oracle/src/traffic_oracle/vehicles/__init__.py` |
| Enforcement API (rules → pending → approval) | `services/traffic-oracle/src/traffic_oracle/enforcement/__init__.py`, `rules.py` |
| Interception plan (pgRouting) | `services/traffic-oracle/src/traffic_oracle/vehicles/pursuit.py` |
| Agency registry + dispatch projection | `services/traffic-oracle/src/traffic_oracle/units/agencies.py`, `projection.py`, `router.py` |
| Schema rails (migration CHECKs) | `services/corridor-store/migrations/010_vehicles_watchlist.sql`, `011_city_coordination.sql` |
| Emulated sweep use case | `services/event-emulator/src/event_emulator/use_case_library.json` (`watchlist_anpr_sweep`) |
