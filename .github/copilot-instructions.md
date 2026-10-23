# Veer — Instructions for AI Coding Assistants

> For GitHub Copilot, Claude and any other assistant generating code in this
> repository. Read it before writing code. It states what the system is now,
> the rules that are not negotiable (ethics, human authority, honesty), and
> where the source of truth for every constant lives. When this file and the
> code disagree, **the code wins** — fix this file in the same change.
> Detailed reference: `TECHNICAL_DOCS.md`; the God's-eye canvas:
> `docs/GODS_EYE_INTEGRATION.md`; the use-case library: `docs/USE_CASES.md`.

---

## 1. What Veer is

Veer (the repo keeps its birth name, VVIP) is a **city-scale, multi-agency
corridor-and-enforcement operating system for Ahmedabad**, running entirely
on one GB10 edge box. One backbone schedules, predicts and releases road
occupancy for corridor events (protected movements, processions, mass
gatherings, races, ceremonials, stadium egress, flood evacuation, work
zones), carries the stolen-vehicle chain (earmark → ANPR sightings →
human-approved recommendation → pursuit → interception plan), dispatches
five agencies (Jan Rakshak, police, traffic, 108 ambulance, fire) and puts
all of it on one God's-eye picture. The VVIP convoy is the base case, not
the boundary. Veer is built to **strengthen the state's ICCC / CCTV / ITMS
programmes** — it is decision support for government officers, never a
replacement for them.

The road network is the **real OpenStreetMap network**: 49 segments
(ids 1001–1049) between 34 real junctions, every routing line drawn along
the OSM road between its two junction nodes (`scripts/gen_osm_network.py`,
migration 020 `geom_source = 'osm'`; © OpenStreetMap contributors,
ODbL 1.0 — keep the attribution wherever geometry is shown or shipped).

## 2. The backend-first law

**The backend is the sole source of truth.** Every data model, response
schema, enum value, field name, validation rule and domain constant is
defined by the backend (Pydantic models, Rust structs, SQL CHECKs, the
generated protocol tables). The command-deck consumes them as they are.

1. **Schema direction:** backend → frontend. Deck types (`src/lib/types/`,
   `src/lib/ops/types.ts`) mirror backend shapes; when a backend model
   changes, the mirror changes — never the reverse.
2. **Names and enums:** backend `snake_case` and enum literals are used
   verbatim (`road_class`, `data_quality`, `geometry_basis`, band names
   GREEN/AMBER/RED/BLACK, rule ids, statuses). No frontend-only enum values.
3. **Nullable means nullable:** render `null` honestly ("unavailable right
   now", "—"), never as 0, "none" or a guessed default.
4. **No invented endpoints:** if the UI needs data no endpoint serves,
   extend the backend first. Every deck URL is a fixed constant
   (`src/lib/ops/endpoints.ts`, `src/lib/gods-eye/data.ts`) or built from
   validated ids; a 404 is "not deployed", shown as such.
5. **No client-side domain logic:** route scoring, security validation,
   band classification, e-challan rules, ETA and anomaly decisions are
   backend computations. The deck may colour and format; it never
   re-implements or overrides a rule, and never re-derives a backend
   constant (floors, thresholds, timeouts) — read it from the API.
6. **Protocol shared values** live in `shared/schemas/protocol_params.json`
   and are generated into both services (`protocol_params_gen.py`);
   `scripts/export_protocol_params.py --check` fails on drift. Edit the JSON
   and regenerate — never edit a generated file by hand.
7. **Additive contracts:** response changes are additive; consumers tolerate
   missing optional fields and unknown extra ones, and treat a 404 from a
   not-yet-deployed route as "feature not deployed", never as a crash.

## 3. Ethics rails — non-negotiable, enforced in schema and code

- **Vehicles only, via ANPR plates — never faces or persons.** No person,
  face, frame, image or stream-URL field exists on any path (migrations
  010/012, `vehicles/sources.py`, `units/agencies.py`). Do not add one.
- **Every penalty is human-approved.** E-challan recommendations are created
  `pending` only; the single transition is
  `POST /api/v1/enforcement/recommendations/{id}/decision` with a named
  officer and an append-only `audit_tail` (`enforcement/__init__.py`).
- **No fine for a reported-stolen vehicle's owner.** Under an active
  `stolen_vehicle_fir` earmark the rules file one evidence-only
  `R_STOLEN_SIGHTING` per read (migration 017); approving a penal
  recommendation of that vehicle returns **409** `e-challan withheld —
  vehicle reported stolen` (`reason: stolen_vehicle_fir`). Approving the
  R_STOLEN_SIGHTING is the authorised basis of a pursuit, never a fine —
  a pursuit of a stolen vehicle based on an e-challan answers **422**, and
  **one officer approval authorises one pursuit**: an approval that
  already authorised a pursuit answers **422** on reuse
  (`events/__init__.py` `_validate_pursuit`). Every recommendation row
  carries `pursuit` `{event_id, status}`; the deck skips an approval spent
  on a closed pursuit (`ops/client.ts` `approvalSpent`) — keep both sides
  in step.
- **Lawful basis and retention:** an earmark needs an enumerated
  `lawful_basis` and a retention capped at 90 days (server-computed);
  sightings past retention are reaped. A sighting trail is disclosed only
  for an earmarked vehicle or as evidence of a recommendation
  (`city/common.SQL_LAWFUL_READ`, one predicate everywhere — C6 parity).
- **Plates are masked** (`GJ27EM•••1`) on every canvas, feed and dossier
  unless a named duty officer is signed in (`src/lib/gods-eye/plates.ts`,
  mirrored by the oracle's `city/common.py`). New text sinks must mask too.
- **Emulated and simulated data say so.** Emulated rows carry their label
  (`source = 'emulated_anpr'`, `emulated_patrol`,
  `emulated_dispatch_projection`, `metadata.source = 'event-emulator'`,
  `EM` plates); the ingress feeds are `data_quality = synthetic`, and the
  emulator's wire rows are SYNTHETIC (2) — ANOMALOUS (1) only as the
  outlier marker, never REAL (0). The deck labels them EMULATED /
  SIMULATED FEED. Never strip or soften a label. Emulated positions sit ON
  the road (`ST_LineInterpolatePoint`, never `ST_Centroid`; event markers
  on their route).
- **Nothing anti-government.** Copy, comments and docs describe Veer as
  strengthening the state's own programmes and officers.

## 4. Human authority — the AI proposes, a named officer decides

- **Escort authority — "officer approves every switch".** During a live
  escort the decider only PROPOSES a route switch, Plan B, a hold or new
  closures. One open proposal per escort (a newer one supersedes it,
  audited); a named officer answers
  `POST /movements/{id}/escort/approvals/{approval_id}` `{decision:
  approve|reject, officer?}` (200; 404 unknown; 409 expired, superseded or
  already decided; 422 bad body). With no answer within
  `VVIP_ESCORT_APPROVAL_TIMEOUT_SEC` (default 90 s, clamped 15–600) the
  proposal EXPIRES and the convoy holds its current approved plan. The
  decider never switches a route on its own (`workflows/live_escort.py`,
  `GET /escorts` lists the open proposals).
- **Dispatches** need a duty officer's approve/defer
  (`POST /api/v1/dispatches/{id}/decision`); **pursuits** are opened by an
  operator, never by the system; the **signal-plan writer** is default OFF
  (`VVIP_SIGNAL_POLICY_WRITER`), operator-triggered and twin
  shadow-evaluated (fail-closed).
- **MCP read-only rail (R2):** no MCP tool approves, decides, dispatches,
  earmarks or transitions anything (`mcp/server.py` `POST_ALLOWLIST`,
  `FORBIDDEN_TOOL_VERBS`, pinned by tests). The canvas verbs
  (`canvas_fly_to`, `canvas_track_entity`, `canvas_highlight`,
  `canvas_annotate`, `canvas_set_sensor`, `canvas_toggle_layer`) return a
  DIRECTIVE a human applies or dismisses — the AI never moves the
  operator's camera itself.
- **Officer identity:** the deck forwards the signed-in officer as the
  percent-encoded `x-veer-officer` header and strips any client copy; the
  oracle and brain trust it only on a loopback connection. A differing body
  `officer` is kept as `claimed_officer`. `VVIP_REQUIRE_OFFICER=1` makes a
  named officer mandatory where it is not already (escort start / stop /
  clear on the brain; signal-plan applies and watchlist writes on the
  oracle); decisions, approvals and lifecycle transitions always need one.
- Scripts that must approve something unattended record the honest label
  `Scripted demo officer (showcase script)`; in the judged demo a signed-in
  human approves on `/ops`.

## 5. The stack (argus, the live system)

| Component | Where | Port (loopback unless noted) |
|---|---|---|
| command-deck (SvelteKit, adapter-node) | `vvip-argus-command-deck` | **5273 on 0.0.0.0** — the only LAN-facing port, behind the operator PIN gate |
| traffic-oracle (Python) | `vvip-argus-traffic-oracle` | 8091 |
| convoy-brain (Python) | `vvip-argus-convoy-brain` | 8092 |
| event-emulator (Python) | `vvip-argus-event-emulator` | 8090 |
| PostgreSQL 16 + PostGIS 3.6 + pgRouting + pgvector (arm64 image) | `vvip-argus-postgis` | 56434 |
| Valkey | `vvip-argus-valkey` | 6380 |
| NATS (JetStream) | `vvip-argus-nats` | 4223 (monitoring 8223) |
| signal-ingress (Rust) | `vvip-argus-signal-ingress` | — (publishes to NATS) |
| decider pplx-decider-v1-27b | systemd `veer-decider.service` | 8083 |
| Gemma 4 12B (llama-server) | systemd `veer-llama.service` | 8084 |
| embeddings (pplx-embed-v2-late + bge-m3) | systemd `veer-embed.service` | 8085 |

Ports, container names and the compose invocation come from
`scripts/veer_env.sh` and `infra/compose.argus-override.yml` — scripts take
every port/URL/container from env with these defaults, so the same script
runs against a private sandbox and against the live stack. The retired
original stack's deck / brain / oracle ports are never a default: every
default is the table above (deck 5273, brain 8092, oracle 8091); only the
vite dev server keeps vite's own default port under `bun run dev`
(`services/command-deck/vite.config.ts`).

**Bring-up:** `bash scripts/veer_up.sh` (idempotent: preflight → model
units → compose up → migrate → seed / OSM network step if needed →
converge → readiness table, READY 11/11); `--status` is read-only;
`--install-units` links `infra/systemd/*` and enables `veer.target`, which
brings the stack back after every reboot (`veer-up.service` +
`veer-models.target`). Settings and secrets live in
`~/.config/veer/veer.env` (mode 600; `scripts/veer_env.sh --set KEY -`
reads a secret from stdin).

**Operator gate:** with `VEER_OPERATOR_PIN` set, `POST /api/session
{officer, pin}` issues a signed 12 h cookie; every proxied API prefix is
DEFAULT-DENIED (403) without it, except the public road geometry, the
liveness probes and the 108 driver's ambulance request (6 per client per
10 min). PIN failures are throttled (8 per 5 min) — scripts sign in ONCE
through the shared helpers `scripts/deck_session.sh` /
`scripts/lib/deck_session.mjs` (never a retry loop). Never print, log or
commit `VEER_OPERATOR_PIN`, `VEER_SESSION_SECRET` or
`VEER_CESIUM_ION_TOKEN`.

**3D canvas:** CesiumJS 1.146 is vendored (`static/cesium`). Keyless by
default (OSM raster + ellipsoid terrain, zero cesium.com/Google calls);
with `VEER_CESIUM_ION_TOKEN` set server-side the operator canvas gets
Cesium World Terrain, Bing aerial imagery and Google Photorealistic 3D
Tiles — and while the PIN gate is on, only a SIGNED-IN operator receives
the token (`routes/(deck)/gods-eye/mode/+server.ts`); anonymous clients and
the public pages stay keyless. Keep the Google / Cesium / OSM credits
visible.

## 6. Data model essentials

- Schemas: `corridor` (network, junctions, adjacency, cameras, stations,
  signal programs, vehicles/earmarks/sightings, dispatches), `traffic`
  (observations — partitioned daily, hourly aggregates, anomaly log,
  memory tables), `events` (corridor events, spines, phases, status log,
  enforcement recommendations), `adaptive` (situation log, parameter
  overrides), `convoy` (legacy movements), `emulator` (runs ledger).
- **Migrations** `services/corridor-store/migrations/001…020`, applied by
  `scripts/migrate.py` in lexical order with a sha256 ledger
  (`corridor.schema_migrations`): an applied file is immutable (checksum
  drift is a hard error) — change the schema with a NEW numbered file,
  additive and idempotent (`IF NOT EXISTS`, guarded CHECK swaps), schema
  only (seed rows belong in `services/corridor-store/seed/`), and keep the
  splitter grammar (no `;` inside string literals, no dollar-quoted bodies,
  no block comments). 020 adds `road_segments.geom_source` /
  `geom_meta` and `junctions.osm_node_id`.
- **Geometry (contract C-K):** ROUTE, cost, attribute and calibrate on
  `geom`; DRAW and animate with `COALESCE(display_geom, geom)`.
  `geometry_basis` says how a drawn line was made: `osm` (the routing geom
  is the OSM road — all 49 today), `osm_display`, or `schematic` (the only
  basis drawn dashed).
- **Real vs synthetic history:** `traffic.hourly_aggregates` is REAL-only —
  the twin's calibration input. Synthetic (simulated-feed) hours roll up
  into `traffic.hourly_aggregates_synthetic` (migration 019), served
  labelled and never read by anything that calibrates, trains or fits.
- SQL is always parameter-bound (asyncpg `$n` / psycopg `%s`); never
  f-string, format or concatenate SQL. Spatial predicates use indexed forms
  (`&&`, `ST_DWithin`), never `ST_Distance < x` scans.

## 7. Contract vocabulary you will meet in docstrings

| Id | Meaning |
|---|---|
| C-A | emulated agency units: NATS `corridor.units.emulated` → Valkey `units:emulated:{unit_id}` (EX 30) |
| C-B | convoy GPS → Valkey `convoy:position:{movement_id}` (EX 30) |
| C-C | city fusion API: `/api/v1/city/entities`, `/city/entity/{kind}/{id}`, `/city/feed`, `/events/{id}/audit`, `/cameras` |
| C-D / C-D2 | brain chat stream (NDJSON `thought`, `tool_call`, `tool_result`, `directive`, `token`, `error`, `done`), directive acks, `POST /mcp` |
| C-E | ANPR sightings ingest `POST /api/v1/vehicles/sightings` |
| C-F | event lifecycle `POST /api/v1/events/{id}/status` (planning → approved/active/cancelled …) |
| C-G | emulator async runs (202 / 409 / progress / stop) + living city |
| C-H | ingress anomaly documents (`anomaly_flags`, `severity`, `feed_quality`) |
| C-J | escort control + status (incl. `pending_approval`) |
| C-K | display geometry + `geometry_basis` |
| C-L | photoreal-3d layer (tile picks never open cards) |
| C-M | `veer_lang` cookie (EN/HI/GU) + emulated labelling on public events |
| C-N | stolen-vehicle suppression (`R_STOLEN_SIGHTING`, the 409) |
| C-O | route updates `POST /api/v1/events/{id}/routes` (superseded spines kept as history) |
| C-P | `/traffic/historical` synthetic aggregates + provenance + basis |
| C-Q | provenance fields (`city_band_since`, `simulated_feed`, `data_quality` …) |
| C-R | diversion fields (`uses_route_segment_ids`, `entry_pinned`/`exit_pinned`, `detour_limit_m`, `detour_reason`) |
| C-S | the spatial router mounted ahead of the inline segment handlers |

## 8. The two-tier brain (convoy-brain)

- **Strategic tier:** the 27B decider on :8083, asked typed questions
  (`choice` / `noul` / `score`) — never chat. Checkpoints are event-driven
  (incident, congestion crossing, `situation:changed`, phase transition,
  operator chat), ≥ `DECIDER_MIN_CHECKPOINT_INTERVAL_SEC` (5 s) apart per
  movement; p95 above `DECIDER_P95_DEGRADE_MS` (3000) degrades to the
  tactical tier with an operator alert. `CONFIDENCE_FLOOR` is 0.45
  (`VVIP_CONFIDENCE_FLOOR`, calibrated against the live decider — do not
  lower it without new measurements). A confident-but-novel answer is
  withheld (novelty latch); routine checkpoints may be served by the tier-0
  cache (`DECIDER_TIER0_DISABLE=1` is the kill switch). The cache key holds
  the checkpoint's question set, so decider options must be stable content:
  pre-movement route options are labels derived from each route's own
  segment sequence (`workflows/pre_movement.py` `route_option_label`) —
  never the oracle's per-request route ids (that broke the live tier-0
  gate, 2026-10-23) and never ranks; `TIER0_OWN_DOC_TRIGGERS` files a
  pre-movement answer for the movement's own decision docs too.
- **Tactical tier:** Gemma 4 12B on :8084 drafts tool arguments against each
  tool's `inputSchema` (grammar-constrained, one repair round) and composes
  the brain's human-facing text — dossiers, trilingual (EN/HI/GU)
  advisories, `/chat` narration.
- **Floor:** deterministic heuristics (`llm/heuristics.py`). The fallback
  chain decider → Gemma → heuristics is total, and every drop is audited
  into the movement's `decisions_log`.
- Turn limits (`orchestrator.py`): `MAX_TOOL_ROUNDS = 6`,
  `TOTAL_TURN_TIMEOUT_SEC = 120`; tool results truncated at 4096 bytes.
- MCP surface on the brain: 28 tools (incl. the 6 canvas verbs),
  4 resources, 3 prompts (`POST /mcp tools/list`).
- Protocol floors (`protocol_params_gen.py`): advance closure Z+ 180 /
  Z 120 / Y 60 s (X and `none`: none); minimum lanes Z+ 6 / Z 4 / Y 2 / X 1.

## 9. IPC

| Channel | Name | Notes |
|---|---|---|
| NATS | `corridor.traffic.live` | Arrow IPC from signal-ingress (data_quality synthetic today) |
| NATS | `corridor.traffic.anomaly` | Tier-1 anomaly JSON (C-H) |
| NATS | `corridor.traffic.emulated` | the emulator's showcase plane — NEVER `.live` |
| NATS | `corridor.units.emulated` | emulated agency units (C-A) |
| Valkey | `traffic:latest:{sid}` (TTL 300 s) | live plane's newest row per segment |
| Valkey | `traffic:emulated:latest:{sid}` | emulated plane — read only by EMULATED events |
| Valkey | `adaptive:inputs:{id}`, `adaptive:params:{id}` (TTL 30 s), `situation:changed` (TTL 60 s) | AMA inputs / mirror / decider trigger |
| Valkey | `convoy:context:{movement_id}` | brain context + `decisions_log` audit |

## 10. Code-quality rules

**General.** No secrets or `.env` files in git (`~/.config/veer/veer.env`
is outside the tree). Structured logging (`structlog` in Python, `tracing`
in Rust); no `print` in service code. Every outbound URL is a fixed
platform constant (loopback / `host.docker.internal` allow-list) or passes
the external-URL validator — never fetch a user-supplied URL. Match the
surrounding style and comment density; make the smallest correct change;
keep the docstring headers (they are the module contracts) true.

**Python** (traffic-oracle, convoy-brain, event-emulator, embedding
service). `requires-python >= 3.12` (the service images run
python:3.12-slim); ruff with line length 100 (oracle and brain select E, F,
I, N, UP, B, SIM, TCH); mypy strict in oracle and brain; async-first
(asyncpg, httpx, nats-py); pydantic models or dataclasses at boundaries;
pytest with per-test `importorskip` for optional stacks — never skip a
whole suite.

**Rust** (signal-ingress). Edition 2024, tokio; `thiserror` / `anyhow`
errors with `?` on production paths; bounded channels; rustls (no OpenSSL);
`cargo test --locked`. The network table `src/feeds/segments_ahmedabad.rs`
is GENERATED (`scripts/gen_ingress_segments.py`) — never hand-edit it.

**TypeScript / Svelte** (command-deck). SvelteKit 2 + Svelte 5 runes
(`$state`, `$derived`, `$props`), strict TypeScript, no `any` outside
unavoidable library gaps; bun is the package manager (`bun.lock`);
`svelte-check` must report 0 errors; `eslint .` (`eslint.config.js`)
carries the import-boundary rules, and `tests/design-tokens.mjs` enforces
the same boundaries textually plus token parity and the hex sweep.
Components consume semantic design tokens (`src/lib/design/tokens.css`);
canvas code reads colours through `tokens.ts` (`bandColor()`, `cssVar()`).
Every user-visible string goes through the EN/HI/GU string packs
(`src/lib/i18n/`).

## 11. Gates and tests

- `bash scripts/verify_all.sh` — the acceptance ladder (every pytest
  suite, the deck's type/unit/UI tiers, cargo, generator `--verify` /
  `--check` tiers, the submission-consistency oracle, the wave gates);
  the first failing tier aborts. Its header lists the tiers.
- `node scripts/submission_consistency_check.mjs` (and `--selftest`) — the
  docs/deck figures must equal the artifacts they cite.
- Deck smokes in `services/command-deck/tests/` (design, screens, gods-eye,
  city-command, canvas-ux, ops-wall, citizen; `contract/run.mjs` and the
  `*.unit.mjs` files) — gated smokes sign in through
  `services/command-deck/tests/lib/operator-session.mjs`; no smoke POSTs to
  a writer.
- `bash scripts/argus_full_smoke.sh`, `bash scripts/repetition_gate.sh
  --fast` and `bash scripts/showcase_run.sh` exercise the integrated stack.
  Live writes (showcase rounds, demo resets) need the operator's go-ahead,
  in this order: the live smokes → `showcase_run.sh` →
  `repetition_gate.sh` (with `/ops` idle) → `journeys.mjs --live` →
  `demo_reset.sh --apply --officer '<name>' --cancel-debris` (≥ 30 min
  after the showcase and the journeys).
- Test against a private sandbox stack or a scratch clone — never write to
  the live store (:56434) from a test.

## 12. Honesty rules

- Never invent a figure. Every number in code comments, docs, the deck and
  the submission comes from a command, an artifact or the code, and says
  which; a figure that could not be re-measured is marked as history.
- Never present emulated or simulated data as a measurement, a model's
  output as a field observation, or a fallback answer as the AI's.
- Say "not deployed", "unavailable" or "unknown" when that is the truth —
  a stale band is UNKNOWN, never GREEN; an empty table is an honest empty
  state, never padded with fixtures.
- Report a refusal as designed safety when it is one (e.g. the signal
  writer refusing an uncalibrated twin) — never work around it.

## 13. Where to look

| Concern | Path |
|---|---|
| Oracle HTTP surface | `services/traffic-oracle/src/traffic_oracle/__main__.py` + package routers (`events/`, `city/`, `vehicles/`, `enforcement/`, `units/`, `signals/`, `adaptive/`, `spatial/`, `ops/`, `simulation/`) |
| Brain HTTP + escort authority | `services/convoy-brain/src/convoy_brain/__main__.py`, `workflows/live_escort.py`, `config.py` |
| MCP server | `services/convoy-brain/src/convoy_brain/mcp/server.py` |
| Emulator | `services/event-emulator/README.md` |
| Ingress | `services/signal-ingress/README.md` |
| Deck proxy, CSP and gate | `services/command-deck/src/hooks.server.ts`, `src/lib/shell/operator-session.server.ts` |
| God's-eye canvas + layers | `services/command-deck/src/lib/gods-eye/` |
| Network generator + evidence | `scripts/gen_osm_network.py`, `scripts/data/osm_network*.json`, `services/corridor-store/seed/NOTICE.md` |
| Bring-up | `scripts/veer_up.sh`, `scripts/veer_env.sh`, `infra/systemd/` |
