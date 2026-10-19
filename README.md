<p align="center">
  <img src="https://img.shields.io/badge/Veer-city--scale%20multi--agency%20platform-ea580c?style=for-the-badge&logoColor=white&labelColor=1e293b" alt="Veer" />
</p>

<h1 align="center">
  🚨 Veer — the City-Scale Corridor-and-Enforcement Operating System
</h1>

<p align="center">
  <strong>Born as the VVIP Convoy Orchestration Platform — and elevated by the paradigm
  shift our mentor pushed us to take seriously: VVIP routing is <em>one component among
  many</em>. One engine now schedules, predicts and releases road-network occupancy for
  eight corridor-event classes (protected movements, processions, mass gatherings, races,
  ceremonials, stadium egress, flood evacuations, work zones), carries a stolen-vehicle
  report from FIR to ranked interception plan on the same rails, and puts every agency on
  one live God's-eye picture. The VVIP run is the base case, not the boundary
  (<a href="docs/USE_CASES.md">the use-case library</a> ·
  <a href="docs/GODS_EYE_INTEGRATION.md">the God's-eye canvas</a>).</strong>
</p>

<p align="center">
  <em>Modelled 25×–506× (central 100×) reduction in junction-freeze commuter delay — deterministic impact model over the repo's own seed network, every assumption falsifiable in <code>IMPACT_REPORT.md</code></em>
</p>

<br/>

<p align="center">
  <img src="https://img.shields.io/badge/Status-Prototype%20Ready-16a34a?style=flat-square" />
  <img src="https://img.shields.io/badge/Strategic%20Tier-pplx--decider--v1--27b-7c3aed?style=flat-square" />
  <img src="https://img.shields.io/badge/Tactical%20Tier-Gemma%204%2012B-2563eb?style=flat-square" />
  <img src="https://img.shields.io/badge/Edge%20Deployment-GB10%20Unified%20Memory%20121%20GiB-76b900?style=flat-square" />
  <img src="https://img.shields.io/badge/Architecture-6%20Microservices%20%2B%203%20Model%20Hosts-2563eb?style=flat-square" />
  <img src="https://img.shields.io/badge/Language-Python%20|%20Rust%20|%20TypeScript-f97316?style=flat-square" />
  <img src="https://img.shields.io/badge/License-MIT-64748b?style=flat-square" />
</p>

<br/>

<p align="center">
  <a href="#-the-problem">The Problem</a> •
  <a href="#-the-solution">The Solution</a> •
  <a href="#-the-use-case-library--two-mission-families">Use-Case Library</a> •
  <a href="#-the-intelligence-stack">Intelligence Stack</a> •
  <a href="#-how-it-works">How It Works</a> •
  <a href="#-key-capabilities">Key Capabilities</a> •
  <a href="#-system-architecture">Architecture</a> •
  <a href="#-project-structure">Project Structure</a> •
  <a href="#-getting-started">Getting Started</a> •
  <a href="#-technical-documentation">Tech Docs</a>
</p>

---

<br/>

<p align="center">
  <img width="840" alt="The /ops situation wall on the live command-deck — band eased to GREEN after the flagship morning, plate GJ27EM9001 live in the Decision Queue" src="showcase/showcase_ops_wall.png" />
</p>
<p align="center">
  <img width="840" alt="The God's-eye 3D city mid-chaos — all five layers live (events, traces, signals, units, cameras), 38 entities" src="showcase/showcase_gods_eye_city.png" />
</p>

## ⚡ The Interface — one command-deck, every audience

**The canonical interface is the SvelteKit command-deck served at
`http://localhost:5173`** (container `vvip-command-deck`; `/` is the
**Convoy Command cockpit** — the restored VVIP template home — with `/ops`
the situation wall beside it; asserted by the deck's design-smoke contract:
GET `/` = 200 carrying `data-template="convoy-command"` — the route tree
serves `/` directly). Every audience lands on the same deck:

| Surface | Route | Theme | What it is |
|---|---|---|---|
| **Situation wall** | `/ops` | `command` (graphite) | The operator's wall (the cockpit `/` links here): band masthead (city state RED/AMBER/GREEN/BLACK), the Corridor Operations Board, Pursuit & Watchlist console, Decision Queue (human approvals), Jan Rakshak Dispatch, City Signal Plans, the AMA module card, the Decision-Audit Stream — and the wall God's-eye canvas (`.ge-canvas`) as centerpiece |
| **God's-eye cinematic** | `/gods-eye` | `command` | The fullscreen CesiumJS 3D city — all five integrity-validated layers, FLIR-class sensor modes, share links |
| **Citizen Advisory** | `/citizen` | `daylight` (paper) | Plain-language band strip + live event cards: which stretch, when, how long, the alternate |
| **108 Green Corridor** | `/driver` | `daylight` | The driver flow driven end-to-end: request → evaluating → proposal (duty-officer approve) → open window with live countdown and the golden-hour counter |
| **Comms desk** | `/comms` | `command` | Trilingual advisory columns — EN / हिंदी / ગુજરાતી carrying the same facts verbatim |
| **Chat console** | `/chat` | `command` | Operator takeover chat with context header and stream |

Per-surface acceptance is a camera, not a claim:
`services/command-deck/tests/screens-verify.mjs` photographs **every revamped
surface under SwiftShader software-WebGL and proves each page's key elements
plus a non-blank PNG** — **40/40 checks green** (re-executed against the live
`:5173` container for this revision, 2026-10-13 IST, exit 0; manifest:
`tests/artifacts/screens/screens_manifest.json`, shots
`tests/artifacts/screens/screen_{ops,gods-eye,citizen,driver,comms,chat}.png`).
This pass's run saw the `/ops` band **GREEN with 10 panels** (the same
manifest read **RED** earlier the same day — the masthead moves with the
city, and the deck's `/ops` had 23 live event cards at capture).

> **Demo honesty note:** the deck is the backend-wired canonical surface —
> every screenshot and check above was taken against the running
> 7-container stack. The React `frontend/` is retained **as the offline
> fallback demo**: `cd frontend && npm install && npm run dev` boots a
> scripted full-escort simulation (rolling congestion, diversions, AI
> reasoning panels) that needs no GPU and no backends, and its headless
> journey test (`frontend/tests/e2e/citizen-green-corridor.e2e.mjs`, 11
> checks) still gates `scripts/verify_all.sh`. When the stack is up, use the
> deck.

### 🎥 The flagship: "God's Eye Morning" (live stack)

`scripts/showcase_run.sh` (no arguments) injects the flagship composite
scenario through the **event emulator's real HTTP API into the running
stack**: 09:20 on the real 148th Rath Yatra morning (27 Jun 2025) — chariots
crawling the old city, an 18 mm/h monsoon burst waterlogging the Nehru Bridge
approach, bazaar crowds behind barricades — and a 108 cardiac ambulance
requesting a green corridor across the river. Into that morning a **stolen
vehicle is reported** (FIR 4401/2026, Kalupur PS, plate GJ27EM9001) and the
full enforcement chain runs through the same stack: plate **earmarked**
(lawful basis + 30-day retention) → **emulated ANPR sightings** (4 cameras,
every row born `source='emulated_anpr'`) → deterministic **e-challan
recommendation** filed PENDING → **duty-officer approval** (the only
pending→approved transition in the system) → **operator-initiated pursuit
event** → **pgRouting interception plan** (choke junctions ranked off the
latest sighting) → **live trace on the God's-eye canvas** — while a **Jan
Rakshak unit** is dispatched to the spillover and the **city signal-policy
guard** refuses an uncalibrated commit, audited. Re-run for this revision
(2026-10-18, bench artifacts 2026-10-13 13:24 UTC, showcase one run —
summary + pursuit chain 15:01 UTC / 20:23–20:31 IST, **exit 0,
`passed: true`**): 736/736 + 600 emulator frames delivered; **36 audit
decisions this run** (30 situation + 6 parameter); 3 band transitions; 1
ambulance-delay escalation; **4 green-corridor windows**; 12-replication
twin ambulance delay mean **7.63 s** (model output); **4 pixel-proven
screenshots** — the God's-eye 3D city mid-chaos (38 entities, all five
layers), the pursuit-trace camera parked on the stolen vehicle's real trace
centroid (4 timed positions, `source=emulated_anpr`), the rebuilt Daylight
citizen advisory (1/1 live-source event cards), and the `/ops` situation wall
in the flagship aftermath (band eased to GREEN, plate GJ27EM9001 live in the
Decision Queue). Evidence lands in `showcase/` (results JSON, pursuit chain,
earmark, twin audit, screenshots + shot manifests). The emulated/real
split is stated plainly in [`docs/USE_CASES.md`](docs/USE_CASES.md) and
[`docs/GODS_EYE_INTEGRATION.md`](docs/GODS_EYE_INTEGRATION.md): the emulator
injects **causes only** (schedules, weather, incidents, requests, ANPR
reads) through real APIs; every decision, escalation and approval quoted
above was made by the real stack or a named human officer.

---

## 🔴 The Problem

> *"Every day across India's cities, thousands of commuters are stranded — sometimes for 30–45 minutes — as roads are locked down far in advance for VVIP convoy passages."*

When a high-security convoy moves through a city corridor, the current protocol is blunt and manual:

| Current Reality | Impact |
|---|---|
| 🚫 Roads blocked **15–45 minutes** before convoy arrival | Massive traffic queues stretching kilometers |
| 📞 Coordination happens over **phone calls & radio** | Delays, miscommunication, missed signals |
| 🗺️ Routes chosen by **experience, not data** | Sub-optimal paths through congested zones |
| 🚑 Emergency vehicles **cannot request corridor access** | Ambulances stuck behind security blockades |
| 👮 **Five agencies** coordinate without shared visibility | Traffic police, transport, security, municipal, emergency — all disconnected |

The cost is enormous: **gridlocked cities, delayed emergency response, frustrated commuters, and exhausted security personnel** — all because convoy planning relies on decades-old manual coordination methods.

<br/>

## 💡 The Solution

**Veer** (the repo keeps its birth name, VVIP) is an intelligent
edge-deployed, city-scale multi-agency platform that transforms corridor
operations — for protected movements **and public events alike** — from
reactive road-blocking into **predictive, AI-optimized corridor
management**, and carries everyday enforcement work (stolen-vehicle
watchlists, ANPR reads, e-challans, pursuits) on the same auditable rails
with a human in every penalty loop.

Instead of shutting down roads 30 minutes early, the platform:

- 🧠 **Forecasts** traffic conditions 5–30 minutes ahead — the DSTGAT neural architecture is implemented in real PyTorch, but **no trained weights ship in this repo yet** (`models/onnx/` is empty; the runtime serves heuristic/cache-based forecasts until a model is exported there — see the honesty note under Neural Traffic Forecasting)
- 🛣️ **Optimizes** convoy routes in real-time, scoring every possible path for speed, disruption, and security
- ⏱️ **Activates diversions just 60–180 seconds before** the convoy reaches each segment — not minutes
- 🤖 **Runs a two-tier AI command mind** — a 27B strategic **decider** that owns strategy and tool selection, and a Gemma 4 12B **tactical** tier that drafts tool arguments and composes every human-facing word — both resident on the edge box, no cloud
- 🛰️ **Renders one God's-eye city canvas** — a keyless CesiumJS 3D picture (corridor events, earmarked vehicle traces, signals, agency units, cameras) that every agency shares, fullscreen or embedded in `/ops`
- 🚨 **Carries the enforcement family** — earmark → ANPR sightings → deterministic e-challan recommendations → human approval → operator-initiated pursuit → pgRouting interception plan — vehicles only, lawful basis recorded, retention capped
- 📡 **Provides a unified command dashboard** where every agency sees the same live picture

The result on the repo's own modelled corridor: **junction-freeze commuter delay falls 25×–506× (central case 100×)** under the falsifiable assumptions ledger in `IMPACT_REPORT.md` — the mechanism is structural (delay ∝ hold²), which is why even the conservative bound clears the original "6×" aspiration we no longer lead with because it was unmeasured.

<br/>

## 🗺️ The Use-Case Library — two mission families

The VVIP convoy is the **base case**, not the boundary. Rows 0–7 are the
same failure mode in eight costumes — a corridor whose occupancy must be
scheduled, predicted and released — and the engine expresses them through
one generalized data model (`events.corridor_events`, migration
`services/corridor-store/migrations/007_corridor_events.sql`: five
orthogonal axes — protection class, crowd class, event kind, footprint,
phases — where the kind selects **closure-policy and dossier templates,
never code paths**). Row 8 is the **second mission family**: everyday
enforcement riding the same city backbone.

| # | Corridor event (documented class) | In the engine |
|---|---|---|
| 0 | **VVIP convoy base case** — Z+ state-capital run; 15–45 min blanket holds today | `vvip_convoy_base` · Z+ · full closure 180 s |
| 1 | **Jagannath Rath Yatra** — 148 annual editions, ~14 km old-city procession, lakhs on the street, 35-drone/GPS monitored by 2026 | `rath_yatra_ahmedabad` · procession · 8 km/h speed floor · EXTREME crowd |
| 2 | **Kumbh-class mass gathering** — perimeter ingress/egress around a standing sea of people (240→660 M visits, 2019→2025) | `kumbh_scale_gathering` · pre-dawn medical shuttle · signal hold |
| 3 | **Sabarmati riverfront marathon** — 24,000+ runners, pre-dawn closure → staged crossings → rolling reopen | `riverfront_marathon` · race · 12 km/h pace/sweep |
| 4 | **Republic Day / Independence Day** — Z-class run terminating inside a standing public ceremony | `national_day_parade` · ceremonial · Z · full closure 150 s |
| 5 | **Narendra Modi Stadium egress** — world's largest cricket ground (~132,000 seats); ambulance vs the dispersal wave | `motera_stadium_egress` · egress_surge · seed segment 1032 `stadium_adjacent` |
| 6 | **Monsoon flood evacuation** (Gujarat 2024 class: 28+ dead, 24,000+ relocated) — low-lying arterials vanish, relief columns run the high-ground detour | `flood_evacuation_corridor` · 7 low-lying seed segments waterlogged |
| 7 | **Metro / bullet-train work zones** — months-long published lane loss (Kankaria Road, Kalupur Bridge) | `metro_work_zone` · standing forced lane-loss · partial closure |
| 8 | **Enforcement: stolen vehicle → ANPR sweep → pursuit** — the second mission family on the same backbone | `watchlist_anpr_sweep` · `crime_incident` + `vehicle_pursuit` event kinds · earmark → reads → challan → approval → pursuit → interception plan |

Each is a runnable scenario today. Two files carry them, by design: the
digital twin exports the full 22-scenario library to
`scripts/scenario_library.json` (via `python -m traffic_oracle.simulation
export`), while the event emulator loads its own 10-entry use-case copy —
the 8 corridor use cases + the flagship composite + the emulated ANPR
sweep, `scenarioConfig` only; the `event_kind`/`crowd_class` axes come from
the emulator's hardcoded profiles
(`services/event-emulator/src/event_emulator/use_case_library.json` +
`profiles.py`), which match the twin's values. Each case's documented
reality, Viksit Bharat relevance and engine expression are written up in
**[`docs/USE_CASES.md`](docs/USE_CASES.md)** — including the flagship
composite ("God's Eye Morning": Rath Yatra + monsoon + one ambulance + the
full stolen-vehicle chain) that `scripts/showcase_run.sh` proves end-to-end
on the live stack, and the **God's-eye canvas**
(**[`docs/GODS_EYE_INTEGRATION.md`](docs/GODS_EYE_INTEGRATION.md)**) that
renders both families on one shared 3D picture.

<br/>

---

<br/>
<p align="center">
  <img width="840" alt="Pursuit trace — the God's-eye camera parked on the stolen vehicle's real trace centroid" src="showcase/showcase_pursuit_trace.png" />
</p>
<p align="center">
  <img width="360" alt="Citizen Advisory — the rebuilt Daylight public surface, live-source event cards" src="showcase/showcase_citizen_advisory.png" />
</p>

## 🧠 The Intelligence Stack

Five tiers, decider at the top. Liveness re-probed for this revision
(2026-10-13, IST): **all service health endpoints return HTTP 200** — brain
`:8080`, traffic-oracle `:8081`, decider `:8083`, Gemma `:8084`, embeddings
`:8085`, the event emulator `:8090` (plus command-deck `:5173`; the seven
`vvip-*` compose containers up, PostGIS healthy on host `:55434` with
migrations through **011** — `010_vehicles_watchlist`,
`011_city_coordination` — and the Ahmedabad seed). Traffic-oracle had been down
in the pre-repair probe; it runs under the GB10 host-network compose override
(`infra/compose.gb10-override.yml` — host port 55434 for PostGIS, host
networking for oracle/brain so the loopback model hosts are reachable):

| Tier | What | Where | Owns |
|---|---|---|---|
| **T5 · Strategic** | `pplx-decider-v1-27b` (BF16, 52,170,802,640 B across 11 shards) | model-host `:8083` | Strategic decisions + **tool selection**, asked as typed `choice`/`noul`/`score` questions — never chat |
| **T4 · Tactical** | `aditi-ps18-v7h` **Gemma 4 12B** (Q4_K_M GGUF, 7,381,382,880 B) | llama-server `:8084` (build 69bf643) | Tool-argument drafting against MCP inputSchemas (1 repair round), dossiers, trilingual advisories, `/chat` narration |
| **T3 · Numeric** | DSTGAT ONNX · HistGBT ETA · iForest · OR-Tools CP-SAT/MIP | traffic-oracle `:8081` | The deterministic tool surface the decider chooses among |
| **T2 · Adaptive** | SituationEngine (deterministic classifier) + hysteresis/escalation/shadow-eval | traffic-oracle `:8081` | Situation class (GREEN/AMBER/RED/BLACK) → protocol-floored movement parameters, published via Valkey |
| **T1 · Embedding spine** | `pplx-embed-v2-late-0.6b` (128-d multivector, MaxSim) + `bge-m3` (1024-d dense) | embedding-service `:8085` | "Similar past situations" memory over incidents and scenario runs |

### How a decision is made — and audited

1. **The decider never runs per GPS tick.** Checkpoints are **event-driven**:
   they fire on (1) incident detection, (2) congestion-threshold crossing,
   (3) an adaptive `situation:changed` notification, (4) phase transitions
   (plan / escort / clear), (5) operator chat — with a ≥5 s per-movement
   rate limit between checkpoints. The live-escort GPS loop itself is
   LLM-free (verified in `workflows/live_escort.py`).
2. **Every decider answer is a distribution.** Each typed answer returns
   `{value, probability, confidence, distribution}` and is appended to the
   movement's `decisions_log` (`memory/convoy_context.py`) — the audit trail
   operators and after-action reports read.
3. **Confidence is gated.** Any answer below the 0.6 confidence floor
   triggers a Gemma draft plus an operator alert instead of being acted on
   directly.
4. **Degradation is explicit.** If `/decider/health` p95 exceeds 3 s, the
   brain drops to Gemma-only tactical mode and raises an operator alert —
   every transition audit-logged.
5. **The fallback chain is total:** decider → Gemma → deterministic
   heuristics. No tier failure stops the convoy.

### Measured (bench re-executed for the interface revision, 2026-10-13 18:54 IST, `scripts/bench_results.json`) — gaps listed, not smoothed

*Timezone note: dates labelled IST are local (UTC+5:30); artifact
timestamps quoted alongside are UTC — the same instants.*

Decider **455.78 tok/s** (p99 **1106.8 ms**) · Gemma **20.09 tok/s** ·
embeddings **92.7 ms** · **62.73 GiB** resident across the stack on the
121.44 GiB GB10 unified-memory pool — **19/19 adversarial scenarios
passed (score 100)**, avg **29.19 s** / max **57.38 s** per scenario, every
one under the 60 s scored line (`scripts/bench_adversarial_report.json`,
2026-10-13T13:24:26Z — the run that produced the quoted
`bench_results.json`). Run history in the same harness, no number rounded
in this repo's favour: round 1 (2026-10-11) measured 496.63 tok/s /
958.8 ms / 21.2 tok/s / 92.1 ms / 62.61 GiB with **0/14** scenarios (9×
HTTP 504 at the 120 s cap; 5 evaluator crashes); the post-fix round 2
(2026-10-12, `bench_adversarial_report.r1fix.json`) measured 492.77 tok/s /
968.2 ms / 21.36 tok/s / 84.4 ms / 62.66 GiB with **14/14** (avg 29.27 s /
max 52.03 s); a 2026-10-12T19:50Z re-run went 14/14 at 539.76 tok/s /
888.6 ms / 22.69 tok/s / 91.7 ms (avg 29.32 s / max 56.02 s); the suite
then **grew 14 → 19 scenarios** with the five watchlist/enforcement
scenarios (`tests/adversarial/config.py`: 14 routing + 5 watchlist in the
headless standard run; 2 stress scenarios defined for the stress mode) —
the 2026-10-13T04:09Z run passed **19/19** at 548.59 tok/s / 861.3 ms /
24.04 tok/s / 82.4 ms (avg 25.73 s / max 69.99 s — one scenario over the
scored 60 s line, recorded because it is true; *its report file was later
overwritten, so those figures are doc-only history*); the 2026-10-13T04:33Z
docs-pass run went 19/19 at 546.42 tok/s / 866.0 ms / 23.29 tok/s /
85.9 ms (avg 25.98 s / max 47.6 s); and the interface-revision re-run
quoted above went 19/19 again with every scenario under the line — slower
tok/s and higher p99 than the morning runs, reported exactly as measured.
*(The `bench_results.json` `"round"` label is the script's hard-coded
counter, not a run count.)* The round-1 → round-2 fix landed at the
source (grammar-constrained generation, per-call token budgets and wall
clocks, a decider health-probe latch fix, and the bench client's own
`str` coercion) with **no evaluation threshold touched**.

**Gates — green for this revision:** the workflow's gate sessions drove
all three green after **one fix round** — `scripts/verify_all.sh` (exit 0,
ALL TIERS PASSED: frontend bundle + headless e2e journey + the two
gods-eye tiers (svelte-check + SwiftShader canvas smoke) + every
pure-python pytest suite, as collected on the current tree — convoy-brain
262, corridor-store importer 22 + store 12, embedding-service 113,
event-emulator 94, traffic-oracle 633, plus the command-deck's own 2
proxy-guard tests — + live models smoke);
`scripts/showcase_run.sh` (exit 0, SHOWCASE PASS — "God's Eye Morning",
numbers and artifacts in the flagship section above, `passed: true` in
`showcase/showcase_results.json`, 2026-10-18T15:01Z); `scripts/bench_stack.sh`
(exit 0, BENCH OK — the run quoted above, `bench_results.json` +
`bench_adversarial_report.json` at 2026-10-13T13:24Z). The interface
acceptance camera `services/command-deck/tests/screens-verify.mjs` was
**re-executed by this documentation pass itself** against the live
`:5173` container: **40/40 checks, exit 0** (per-surface evidence in
`tests/artifacts/screens/screens_manifest.json`). `verify_all.sh` and
`fleet_smoke.sh` were **not** re-executed by this pass; the live fleet was
health-checked by the showcase run and screens-verify against the same
containers. *(The earlier 127 exits of the gate scripts in the
2026-10-12 repair session are history, kept visible in
`TECHNICAL_DOCS.md` §3/§13's provenance notes.)*

**Waves 1–3 (2026-10-17/18) — what the ladder grew:** the acceptance gate
(`scripts/verify_all.sh`) now runs fourteen tiers in order —
frontend-bundle, frontend-e2e, gods-eye-check, gods-eye-canvas, the
pure-python pytest suites, submission-consistency, wave1-hatches,
wave2-forecaster, wave2-routing, wave2-tier0, wave3-repetition,
wave3-ledger, models, model-drift — aborting at the first failure.
Wave-1 added the forecaster out-of-support
hatch (`predict/support_bounds.py`: no model ever sees an
out-of-support input), the decider novelty latch (confident-but-novel
checkpoints abstain with an audit row, memory outages fail to
latch-disabled rather than benching the strategic tier), and the
model-drift panel (four hosted surfaces vs a justified baseline). Wave-2
added the t0-alpha ONNX INT8 foundation rung behind the hatch
(`models-local/t0`, quantile contract p10/p50/p90 per field), P90-robust
routing (`optimize/quantile_objective.py`; knob `routing.robust_routing`
registered in `shared/schemas/protocol_params.json`, drift-guarded into
both services) with banded ETAs (`eta_band`/`eta_band_reason`), and the
decider tier-0 routine-checkpoint cache (routine hits served with zero
27B calls; `DECIDER_TIER0_DISABLE` kill switch). Wave-3 added the
**degradation ledger** (oracle
`GET /api/v1/ops/degradation-taxonomy` mining convoy-brain's
`decisions_log` into a recurring-cause taxonomy + the deck's
DegradationLedger panel on `/ops`) and the **repetition gate**
(`scripts/repetition_gate.sh`: identical showcase terminal DB state
across restored rounds — `--fast` offline selftest in the ladder, the
full live gate on demand); `scripts/argus_full_smoke.sh` is the argus
clone's full-system live smoke with a read-only guard over the original
stack. Full detail:
`TECHNICAL_DOCS.md` §3 (latch + tier-0 cache), §6.1b (forecast ladder),
§13 (wave gates). *Re-executed standalone by the 2026-10-18 repair pass:
`smoke_models.sh` exit 0 and `model_drift_panel.sh` `--selftest` + live
panel exit 0, after restoring the host model stack via
`infra/scripts/serve_models.sh start` (workspace-scoped runtime dirs);
in the same day's verify_all run every tier through wave2-tier0 had
passed and only the models tier failed — on the then-down model hosts.*

**Still open, published as-is:** tool_validity **0.684** (up from 0.643;
the decider often final-answers after one tool where a scenario expects
two) and keyword_coverage **0.561** (it moves run to run — 0.595, then
0.579, then this) — both pass the gate but are tracked quality gaps, not
hidden; reasoning_depth 0.30–0.41 across runs.

<br/>

---

<br/>

## 🎯 How It Works

The platform operates in three distinct phases — each fully automated and AI-supervised:

<br/>

### Phase 1 — Pre-Movement Planning 📋

> *Minutes to hours before the convoy departs*

```
  📍 Origin & Destination Defined
         │
         ▼
  🛰️ Traffic Conditions Analyzed
         │  ← Live feeds + historical patterns + anomaly detection
         ▼
  🗺️ Multiple Routes Scored
         │  ← Time (30%) + Disruption (40%) + Security (20%) + Complexity (10%)
         ▼
  🔒 Security Validation
         │  ← Lane requirements, closure types, crowd risk assessment
         ▼
  📋 Diversion Plan Generated
         │  ← Per-segment timing, agency assignments, activation schedules
         ▼
  ✅ Plan Approved — Ready for Execution
```

The decider takes a strategic checkpoint here (≤1500-token state document)
and answers typed questions — route strategy as a `choice`, urgency as a
`noul` probability, disruption tolerance as a `score` — before any route is
locked. The tactical tier validates every candidate route against security
classification rules: a Z+ convoy (highest security) requires 6-lane roads
with full closure capability, while a Y-class movement only needs signal
priority. Routes that violate constraints are rejected **with the reason
attached**.

<br/>

### Phase 2 — Live Escort 🚔

> *Real-time convoy tracking and dynamic response*

As the convoy moves:

- **GPS tracking** updates convoy position every second — the tick loop is pure numeric code, **no model calls**
- **Traffic predictions** run continuously at T+5, T+10, T+15, and T+30 minutes
- **Diversions activate** precisely as the convoy approaches each road segment
- **Congestion spikes** crossing the adaptive threshold trigger a decider checkpoint — only then — and tactical agents fire **only on incidents**
- **Signal controllers** optimize traffic lights in the convoy's path
- **Emergency requests** from ambulances are processed and corridor windows are opened when safe

<br/>

### Phase 3 — Post-Clearance Recovery 🔄

> *After the convoy passes — bringing the city back to normal*

Most convoy systems stop at escort completion. This platform goes further:

- **Diversions deactivate** segment-by-segment as the convoy clears each zone
- **Recovery monitoring** tracks how quickly traffic returns to normal flow
- **Congestion analysis** identifies which segments are still jammed and why
- **After-action reporting** generates a complete movement report — total disruption time, every decision with its distribution and confidence, alerts handled, recovery statistics

<br/>

---

<br/>

## 🏗️ Key Capabilities

### 🧠 Two-Tier AI Decision Making

The platform's mind is a **strategic decider (27B) on top of a tactical
composer (Gemma 4 12B)** — both running locally on the edge device, no cloud
dependency, no internet requirement, complete operational security.

Four specialized tactical agents run on Gemma under decider supervision:

| Agent | Role | Think of it as... |
|---|---|---|
| 🔍 **Traffic Analyst** | Reads the road — congestion patterns, anomalies, trends | *"The intelligence officer scanning the terrain"* |
| 🗺️ **Route Planner** | Finds the best path — scores, ranks, and recommends routes | *"The navigation expert with a chess player's foresight"* |
| 🔒 **Security Liaison** | Validates compliance — lane widths, closure types, crowd risks | *"The security chief ensuring protocol compliance"* |
| 📡 **Diversion Coordinator** | Orchestrates ground ops — agency assignments, timing, sequences | *"The field commander coordinating five agencies simultaneously"* |

The decider picks which tool each agent calls next; Gemma drafts the
arguments against the tool's declared inputSchema (with one repair round);
the numeric tier computes. Every answer carries a probability distribution
and confidence that lands in the movement's audit log.

<br/>

### 🛣️ Intelligent Route Optimization

Routes aren't picked randomly or by shortest distance. The optimizer evaluates every candidate on four weighted dimensions:

| Factor | Weight | What It Measures |
|---|---|---|
| **Travel Time** | 30% | Predicted travel duration using ML forecasts |
| **Traffic Disruption** | 40% | How many commuters will be affected |
| **Security Compliance** | 20% | Road class, lane count, known risk zones |
| **Route Complexity** | 10% | Number of turns, signals, junctions |

The adaptive tier may reweight these per movement from the live situation
class — always protocol-floored, always fail-closed to these baselines.

<br/>

### 📊 Neural Traffic Forecasting (architecture real; weights not yet shipped)

The **DSTGAT** (Dynamic Spatio-Temporal Graph Attention Network) is the
forecasting architecture:

- **4 forecast horizons**: 5, 10, 15, and 30 minutes ahead
- **Graph-aware**: Understands how congestion on one road affects neighboring roads
- **Temporal patterns**: Learns daily and weekly rhythms — morning rush, evening rush, weekend patterns
- **Anomaly detection**: Flags unusual conditions (accidents, events, sensor failures) in real-time

> **Honesty note (verified in-repo):** the DSTGAT PyTorch/ONNX *code path* is
> real, but **no trained model artifact ships in this repository** —
> `models/onnx/` contains only `.gitkeep`, and the runtime default
> `ONNX_MODEL_PATH` points at a not-yet-present
> `models/onnx/flow_forecaster.onnx`. Until an exported model is placed
> there, `flow_forecaster.py` logs `no_model` and serves its
> heuristic/cache fallback. The same is true of the ETA and anomaly models
> (scikit-learn): no trained artifacts ship — they are trained/persisted at
> runtime via joblib when data flows. All corridor data in this repo is the
> synthetic Ahmedabad seed
> (`services/corridor-store/seed/sample_corridor.sql`) — no real traffic
> records are bundled.

<br/>

### 🧭 Active Movement Adaptation (the named AMA module, R9)

A deterministic SituationEngine classifies every movement's world —
security tier × traffic regime × weather × crowd density, plus anomaly and
event flags — into GREEN / AMBER / RED / BLACK. Because the scoring is
monotone in every input, worsening any single dimension can never soften
the band, and a Z+ movement is never classified below AMBER. Class
transitions publish `situation:changed`, which is one of the five events
that legitimately wake the decider. The module is **named and surfaced as
the Active Movement Adaptation (AMA) module** — six stages (situation →
hysteresis → protocol floors → twin shadow-eval → audited persistence →
city signal plans), rendered as a module card on `/ops`
(`AMAModuleCard.svelte`), as a band chip on the God's-eye canvas, and as
machine-readable data at `GET /api/v1/adaptive/module`:
**[`docs/ACTIVE_MOVEMENT_ADAPTATION.md`](docs/ACTIVE_MOVEMENT_ADAPTATION.md)**.

<br/>

### 🛰️ The God's-Eye City Canvas

The shared picture five hierarchies never had: a **keyless CesiumJS 3D
city** (OSM raster + Veer's own 3D entities; photorealistic 3D-tiles only
if an env key is present) embedded as the centerpiece of the `/ops` board
and as a fullscreen `/gods-eye` cinematic route — sensor modes (FLIR
White-Hot / Black-Hot / raw), share links that restore the exact view, and
five integrity-validated layer providers: corridor events, **vehicle
traces** (earmarked vehicles with watch/flagged colours and EMULATED
badges), the signal network, agency units and cameras (the cameras
backend is not deployed yet and honestly says so on the chip).
Architecture, the emulated/real split extended to ANPR, and the ethics
rails: **[`docs/GODS_EYE_INTEGRATION.md`](docs/GODS_EYE_INTEGRATION.md)**.

<br/>

### 🚨 Watchlist & Enforcement (vehicles only, humans in the loop)

The second mission family on the same backbone: a stolen-vehicle FIR
becomes a **lawful-basis earmark** (retention capped at 90 days,
server-computed) → emulated ANPR camera reads (every row born
`source='emulated_anpr'`; the `live_anpr` label is reserved for a real
vendor feed behind the `AnprSource` port) → **deterministic e-challan
recommendations** filed PENDING → **duty-officer approval** with an
append-only audit tail (the only pending→approved transition in the
system) → **operator-initiated pursuit** → **pgRouting interception
plan** ranking choke junctions off the latest sighting. Vehicles only —
no person-directed field exists on any path — and Jan Rakshak, police,
traffic, 108 and fire are dispatchable agencies through the same
human-approved, audited dispatch channel.

<br/>

### 🖥️ The `/ops` Situation Wall — the operator's wall (the cockpit `/` links here)

The canonical operator surface (`/ops` on the command-deck at `:5173`) is a
**situation wall** designed for 24/7 control-room operations — ten panels
around the wall God's-eye canvas, verified live by the screens camera
(`screens_manifest.json`, this revision's run):

- **Band masthead** — the city's situation band (RED/AMBER/GREEN/BLACK) with
  pending-decisions and units-out counts; it moves with the city (the
  manifest has recorded RED and GREEN on the same masthead across runs)
- **Corridor Operations Board** — every live corridor event as a card
  (23 at this pass's capture), each with its freeze window
- **Pursuit & Watchlist** — earmarked plates, emulated ANPR sightings, the
  live pursuit console
- **Decision Queue** — the enforcement queue where e-challan
  recommendations wait **PENDING** for a named duty officer
- **Jan Rakshak Dispatch** — the civilian-force dispatch panel
- **City Signal Plans** — the gated signal-plan writer's view
- **Active Movement Adaptation** — the named AMA module card (live
  `/adaptive/module` descriptor)
- **Decision-Audit Stream** — the append-only audit rows as they land
- **Band history + Twin Shadow-Eval Comparison** — the hysteresis trail and
  the fail-closed twin gate every non-baseline plan must pass
- **The wall canvas** — the God's-eye 3D city embedded as the centerpiece
  (`.ge-canvas`), with the fullscreen `/gods-eye` cinematic one click away

The retired React dashboard (left planning panel / center map / five-tab
right monitor) lives on only inside `frontend/` as the **offline fallback
demo** — its scripted simulation still runs with no backends, and its e2e
journey still gates `verify_all.sh`.

<br/>

### 🚑 Emergency Corridor Access

A dedicated clearance system allows emergency vehicles to request corridor access during active convoy movements:

- Ambulances and fire trucks submit requests through the notification system
- The platform evaluates the request against convoy position and timing
- Duty officers can **approve or defer** with one click
- Approved requests create temporary corridor windows without compromising security

<br/>

### 🔐 VVIP Security Classification

Four security tiers with distinct operational requirements:

| Class | Description | Road Requirement | Closure Type | Advance-Closure Floor |
|---|---|---|---|---|
| **Z+** | Highest security (Head of State) | 6+ lane roads | Full road closure | 180 s |
| **Z** | High security (Cabinet level) | 4+ lane roads | Partial closure | 120 s |
| **Y** | Medium security (State officials) | 2+ lane roads | Speed restriction + signal priority | 60 s |
| **X** | Standard escort | No restriction | Signal priority only | none (no closure) |

> **Event-agnostic generalization:** the engine's generalized event model
> adds a fifth protection class — **`none`, a public event with no
> protectee** (a procession, a marathon, an egress surge, an evacuation
> column, a work zone) — alongside Z+/Z/Y/X. The base-case machinery
> (protocol floors, adaptive bands, audit trail) applies unchanged; a
> `none`-class Rath Yatra is managed by the same forecaster, router and
> situation engine as a Z+ run, with closure policies chosen per event kind
> (`docs/USE_CASES.md`).

<br/>

### ⚡ Edge Deployment

The entire platform runs on **one GB10-class edge box** (Grace-Blackwell
unified memory, 121.44 GiB pool) — no cloud servers, no data center, no
internet connection required:

- The 27B decider resident in BF16 (48.58 GiB of weights), Gemma 4 12B Q4_K_M, and both embedders co-resident — the planned budget is ≈82 GiB with ≈39 GiB headroom (`models/MEMORY_BUDGET.md`)
- Nothing is ever evicted; every model host is supervised with restart-on-exit
- Deployed anywhere the box can go — motorcade command vehicles, mobile control rooms, field ops centers

The browser demo (`frontend/`) needs none of this — it runs on any laptop.

<br/>

---

<br/>

## 🔧 System Architecture

Six specialized microservices plus three resident model hosts (the
event-emulator is the sixth service — the emulated half, §"Key
Capabilities" and `TECHNICAL_DOCS.md` §2.9):

```
                          ┌─────────────────────┐
                          │   Command Deck       │   SvelteKit :5173 — canonical
                          │   (Presentation)     │   /ops wall · God's-eye · public
                          └──────────┬──────────┘
                                     │
                          ┌──────────▼──────────┐
                          │    Convoy Brain      │   Python · decider + Gemma
                          │  (AI Orchestration)  │   4 agents, 3 workflows, MCP tools
                          └──┬───────┬───────┬───┘
                             │       │       │
              ┌──────────────▼─┐  ┌──▼───┐  ┌▼──────────────┐
              │ model-host     │  │NATS  │  │Traffic Oracle │  Python · ONNX · OR-Tools
              │ :8083 decider  │  │      │  │ (ML Engine)   │  Neural forecasting + routing
              │ 27B strategic  │  │      │  │ + Situation-  │  + adaptive layer
              ├────────────────┤  │      │  └──────┬────────┘
              │ llama-server   │  │      │         │
              │ :8084 Gemma 4  │  │      │  ┌──────▼───────┐
              │ 12B tactical   │  │      │  │  PostGIS     │   Spatial database
              ├────────────────┤  │      │  │  + Valkey    │   pgRouting + pgvector
              │ embedding-svc  │  │      │  │  + DuckDB    │   Real-time cache
              │ :8085 memory   │  │      │  └──────────────┘
              └────────────────┘  │      │
                          ┌───────▼──────┐
                          │ Signal       │   Rust · Arrow IPC
                          │ Ingress      │   sub-ms feed ingestion
                          └──────┬───────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              Gov Traffic    Fleet GPS   Crowdsource
                Feeds       Telemetry     Reports

   ┌──────────────────────┐   causes only (never outcomes): event rows,
   │ event-emulator :8090 │   adaptive inputs docs, one twin POST, NATS
   │ (the emulated half)  │   corridor.traffic.emulated — never .live
   └──────────────────────┘
```

| Service | Language | What It Does |
|---|---|---|
| **Signal Ingress** | Rust | Ingests live traffic from 4 data sources at sub-millisecond latency |
| **Traffic Oracle** | Python | ML predictions, route optimization, anomaly detection, adaptive situation layer — plus the watchlist/enforcement routers (earmarks, sightings, e-challans, interception plans), the agency dispatch channel and the gated city signal-plan writer |
| **Convoy Brain** | Python | Decider-supervised agent orchestration — the "thinking" layer |
| **Corridor Store** | PostgreSQL | Spatial database with road network, traffic history, graph routing, memory embeddings, generalized `events.*` schema, vehicle watchlist + city coordination tables (migrations 001–011) |
| **command-deck (canonical UI)** | TypeScript/SvelteKit | The interface for every audience at `:5173` — `/ops` situation wall (with the embedded God's-eye canvas), `/gods-eye` cinematic, `/citizen` + `/driver` public surfaces, `/comms` trilingual desk, `/chat`. The React `frontend/` is the offline fallback demo, not a service |
| **event-emulator** | Python | The emulated half — injects the use-case library's causes through the real APIs, including emulated ANPR camera reads (`:8090`; `TECHNICAL_DOCS.md` §2.9) |
| **model-host** | Python | Serves the 27B strategic decider as typed questions (`:8083`) |
| **llama-server** | llama.cpp | Serves Gemma 4 12B tactical completions (`:8084`) |
| **embedding-service** | Python | Multivector + dense memory retrieval (`:8085`) |

<br/>

---

<br>

## 📁 Project Structure

```
VVIP/
│
├── 📄 README.md                          ← You are here
├── 📄 TECHNICAL_DOCS.md                  ← Detailed technical documentation
├── 📄 IMPACT_REPORT.md                   ← Measured-model impact (deterministic)
├── 📄 VVIP.md                            ← Original project proposal
├── 📄 LICENSE                            ← MIT License
│
├── 📚 docs/
│   ├── USE_CASES.md                      ← The use-case library (8 corridor classes + the
│   │                                        enforcement family, each with documented reality,
│   │                                        Viksit Bharat relevance, and its runnable scenario)
│   ├── GODS_EYE_INTEGRATION.md           ← The God's-eye canvas: architecture, the emulated/
│   │                                        real split extended to ANPR, the ethics rails
│   └── ACTIVE_MOVEMENT_ADAPTATION.md     ← The named AMA module (R9): six stages, HTTP surface
│
├── 🎨 frontend/                          ← LEGACY React dashboard — the OFFLINE
│   │                                        FALLBACK demo (scripted simulation,
│   │                                        no backends; e2e still gates verify_all)
│   ├── src/
│   │   ├── components/                   ← 12 UI components
│   │   │   ├── VVIPDashboard.jsx         ← Root dashboard — toolbar, panels, layout
│   │   │   ├── ConvoyMap.jsx             ← Interactive map with route overlays
│   │   │   ├── LeftPanel.jsx             ← Convoy planning & VVIP profile
│   │   │   ├── RightPanel.jsx            ← Operations monitor (5 tabs)
│   │   │   ├── AIReasoningPanel.jsx       ← Chain-of-thought AI display
│   │   │   ├── NotificationBell.jsx      ← Emergency clearance system
│   │   │   └── ...
│   │   ├── context/ConvoyContext.jsx      ← Global state management
│   │   ├── hooks/useLiveData.js          ← Real-time data subscriptions
│   │   ├── services/api.js              ← Backend API client
│   │   └── views/                        ← Route planning & comms views
│   ├── package.json
│   └── vite.config.js
│
├── 🧠 services/
│   │
│   ├── convoy-brain/                     ← AI Orchestration Engine
│   │   └── src/convoy_brain/
│   │       ├── orchestrator.py           ← Two-tier turn loop + fallback chain
│   │       ├── decider.py                ← Strategic tier: cadence, budgets, degradation
│   │       ├── adaptive_params.py        ← Fail-closed adaptive-parameter client
│   │       ├── http_guard.py             ← Outbound URL safety (SSRF guard)
│   │       ├── llm/
│   │       │   ├── decider_client.py     ← pplx-decider-v1-27b typed-question client
│   │       │   ├── llama_bridge.py       ← Gemma 4 12B tactical bridge (schema ladder)
│   │       │   └── heuristics.py         ← Deterministic floor of the fallback chain
│   │       ├── agents/                   ← 4 specialized tactical agents
│   │       ├── workflows/                ← 3 state-machine workflows
│   │       ├── mcp/                      ← 12 MCP tools + 4 resources (defined in server.py; prompts in prompts.py)
│   │       └── memory/                   ← Convoy state + decisions audit log
│   │
│   ├── model-host/                       ← T5 strategic tier (:8083)
│   │   └── src/model_host/               ← Loads the 27B decider snapshot
│   │
│   ├── embedding-service/                ← T1 memory (:8085)
│   │   └── src/                          ← multivector MaxSim + dense ANN
│   │
│   ├── traffic-oracle/                   ← ML Prediction & Optimization
│   │   └── src/traffic_oracle/
│   │       ├── predict/                  ← DSTGAT forecaster + ETA model (+ nn/ architecture)
│   │       ├── simulation/               ← Digital-twin engine (twin, scenarios, calibration)
│   │       ├── optimize/                 ← Route optimizer + diversion planner
│   │       ├── adaptive/                 ← SituationEngine + params + escalation
│   │       ├── anomaly/                  ← Real-time anomaly detection
│   │       ├── ingest/                   ← NATS → database pipeline
│   │       └── runtime/                  ← Unified-memory management
│   │
│   ├── signal-ingress/                   ← Rust Real-Time Data Ingestion
│   │   └── src/
│   │       ├── feeds/                    ← traffic data source adapters
│   │       ├── pipeline/                 ← Bounded channel batching
│   │       ├── anomaly/                  ← Tier-1 inline detection (~500ns)
│   │       └── publish/                  ← Arrow IPC → NATS streaming
│   │
│   ├── corridor-store/                   ← Spatial Database
│   │   ├── migrations/                   ← PostGIS + pgRouting + pgvector + memory
│   │   ├── seed/                         ← Sample corridor data
│   │   └── importer/                     ← OpenStreetMap → PostGIS loader
│   │
│   └── command-deck/                     ← ★ CANONICAL INTERFACE (SvelteKit, :5173):
│       └── src/                          │   /ops wall · /gods-eye · /citizen ·
│           ├── lib/components/           ← Svelte 5 components              /driver ·
│           ├── lib/design/               ← tokens.css/tokens.ts — the one    /comms ·
│           │                               design-token contract (command     /chat +
│           └── lib/ops/components/template/ ← the restored VVIP cockpit parts
│
├── 🎭 services/event-emulator/           ← The emulated half of the corridor stack
│   └── src/event_emulator/               ← Injects CAUSES ONLY (procession schedules,
│       │                                 │   weather onset, incidents, green-corridor
│       │                                 │   requests) through the real stack's real
│       │                                 │   APIs — never outcomes. Serves :8090,
│       │                                 │   consumes the use-case library verbatim
│       └── use_case_library.json         ← The 8 use cases + the flagship composite
│
├── 🏗️ infra/                             ← Infrastructure & Deployment
│   ├── compose.yml                       ← Docker Compose stack
│   ├── compose.gb10-override.yml         ← GB10 single-host layout (host networking, PostGIS :55434)
│   ├── scripts/serve_models.sh           ← Boots :8083/:8084/:8085 model hosts
│   ├── docker/                           ← Dockerfiles
│   └── observability/                    ← Prometheus + Grafana (23 alert rules)
│
├── 🤖 models/                            ← Model Stack (owned by the models stream)
│   ├── configs/veer_models.yaml          ← C0: single source of model-stack truth
│   ├── Modelfile.gemma_vvip              ← Ollama debug side-load of the same GGUF
│   ├── MEMORY_BUDGET.md                  ← GB10 unified-memory budget (planned+measured)
│   ├── embedding_launcher.py             ← Models-owned embedding service launcher
│   ├── finetune/                         ← QLoRA pipeline (Qwen-era: train_qwen_vvip.py predates the Gemma switch; kept for provenance)
│   └── onnx/                             ← ONNX export target — EMPTY today (.gitkeep only; see Neural Forecasting honesty note)
│
├── 📡 shared/                            ← Cross-Service Definitions
│   ├── proto/                            ← Protocol Buffer schemas
│   └── schemas/arrow/                    ← Arrow IPC data formats
│
└── 🧪 tests/
    └── adversarial/                      ← 21-scenario suite (14 routing + 5 watchlist +
                                             2 stress); the bench's headless standard run
                                             gates on the 19 non-stress scenarios
```

<br/>

---

<br/>

## 🚀 Getting Started

### Prerequisites

| Requirement | Minimum |
|---|---|
| **GPU** | Full stack: GB10-class unified memory (121 GiB pool). Browser demo: any laptop |
| **CPU** | 8+ cores |
| **RAM** | 32 GB (demo) · 121 GiB unified (full stack) |
| **OS** | Ubuntu 22.04+ / Windows 11 |
| **Docker** | Docker with Compose v2 (platform services) |

### Quick Start

**1. Clone the repository**
```bash
git clone https://github.com/shubro18202758/VVIP.git
cd VVIP
```

**2. Start the resident model hosts** (decider :8083, Gemma :8084, embeddings :8085)
```bash
./infra/scripts/serve_models.sh start     # builds venvs if missing; ~5 min cold decider load
```

**3. Launch the platform services**
```bash
cd infra
docker compose up -d
```

**4. Open the command-deck (the canonical interface)**

The `vvip-command-deck` container started in step 3 already serves the deck
— navigate to `http://localhost:5173`: `/` is **the Convoy Command
cockpit** (the restored template home), `/ops` is the situation wall, and
`/gods-eye`, `/citizen`, `/driver`, `/comms` and
`/chat` hang off the same origin (surface map above). For deck development
outside Docker: `cd services/command-deck && bun install && bun run dev`.

**5. Offline fallback (optional, no backends needed)**

Without the stack (no GPU, no containers), the legacy React frontend still
runs the scripted demo: `cd frontend && npm install && npm run dev` — the
offline fallback surface, not the canonical one.

> The model stack is configured by `models/configs/veer_models.yaml` (the
> `services:` block pins :8083/:8084/:8085; each is overridable via
> `MODEL_HOST_URL` / `LLAMA_BASE_URL` / `EMBEDDING_SERVICE_URL`). No
> credentials live in that file; database credentials stay env-only.

<br/>

---

<br/>

## 📚 Technical Documentation

For detailed technical documentation including the five-tier intelligence
stack, API contracts, database schemas, ML model architectures, service
configurations, and code-level documentation, see:

**➡️ [TECHNICAL_DOCS.md](TECHNICAL_DOCS.md)**

<br/>

---

<br/>

## 🏛️ Context & Relevance — Viksit Bharat

This platform is designed in the context of India's expanding road infrastructure under:

- **Bharatmala Pariyojana** — National highway development program
- **PM Gati Shakti** — Integrated infrastructure planning
- **Ministry of Road Transport & Highways (MoRTH)** Intelligent Transport Systems initiatives
- **Smart Cities Mission** — Urban mobility modernization

The corridor-OS generalization is the Viksit Bharat argument: the same
governance failure — a corridor frozen because nobody could schedule, predict
and release its occupancy — recurs across **eight documented event classes**
on the national calendar ([`docs/USE_CASES.md`](docs/USE_CASES.md)): the
capital-region protected runs of a state that hosts Vibrant Gujarat; the
148-year-old Rath Yatra through Ahmedabad's old city; Kumbh-class gatherings
measured in hundreds of millions of visits; World Athletics-calendar
marathons; two immovable national ceremonial days; the world's largest
cricket stadium emptying ~130,000 people onto city roads; the recurring
monsoon flood evacuation; and the metro/bullet-train work zones that
haircut arterial capacity for a season. The enforcement family
(stolen-vehicle watchlist → ANPR sweep → pursuit) extends the same argument
to everyday policing — one auditable backbone, five dispatchable agencies
(police, traffic, 108, fire, Jan Rakshak), one shared God's-eye picture,
humans in every penalty loop. A platform that can only manage VVIP runs
would be a protocol tool; one that expresses all of this is **municipal
infrastructure** — pilot-deployable on one corridor
(Ahmedabad–Gandhinagar) and forkable by any state (MIT).

<br/>

## 🎯 Expected Outcomes

| Metric | Target |
|---|---|
| 🕐 Junction-freeze commuter delay | **Modelled 25×–506× reduction (central 100×)** — see `IMPACT_REPORT.md` |
| 🚗 Commuter queue length | **Minimized through predictive diversions** |
| 🚑 Emergency vehicle access | **Dynamic corridor windows during convoy ops** |
| 👮 Agency coordination time | **Near-instant through unified dashboard** |
| 🔒 Security compliance | **100% automated validation** |
| 🧠 Decision transparency | **Every answer logged with distribution + confidence** |

<br/>

## 👥 Who Benefits

| Stakeholder | How |
|---|---|
| **Commuters** | Shorter wait times, smoother diversions, less frustration |
| **Emergency Services** | Corridor access requests processed in seconds, not ignored |
| **Traffic Police** | Clear diversion instructions with timing, not vague radio calls |
| **Security Agencies** | Automated compliance checks, no security protocol gaps |
| **Transport Departments** | Data-driven decisions, after-action analytics |
| **City Administration** | Reduced public grievances, measurable impact metrics |

<br/>

---

<br/>

<p align="center">
  <strong>Built for the roads of India 🇮🇳</strong>
</p>

<p align="center">
  <em>Where security meets intelligence, and every minute of public disruption matters.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Made%20with-Purpose-ea580c?style=for-the-badge" />
</p>
