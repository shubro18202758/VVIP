# Veer (VVIP) — Technical Documentation

> **Version**: 2.6 (wave 4 — Veer is a **city-scale multi-agency
> corridor-and-enforcement operating system**; VVIP routing is one
> component among many. On top of the corridor-OS generalization
> (`events.*` schema, migrations 007–009; the cause-only event-emulator,
> §2.9; the 8-use-case corridor library, `docs/USE_CASES.md`), the
> **enforcement family** (watchlist/ANPR/e-challan/pursuit, migrations
> 010–011), the **God's-eye canvas** (`docs/GODS_EYE_INTEGRATION.md`), the
> named **Active Movement Adaptation module** (R9,
> `docs/ACTIVE_MOVEMENT_ADAPTATION.md`) and the multi-agency dispatch
> channel with Jan Rakshak first-class, this revision documents waves 3–4:
> the road network rebuilt on **real OpenStreetMap roads** (49 segments,
> 34 junctions, ≈192 km — migration 020, §16.1), **escort authority —
> the officer approves every switch** (§16.2), the stolen-vehicle rule
> (§16.3), the God's-eye in **3D** for signed-in operators (§16.9), the
> operator PIN gate (§16.11), boot auto-start (§11) and migrations
> **001–020** (§5). Figures quoted for this revision come from code and
> from private stacks built from this tree — a fresh install, or a
> converted copy of the live store where the figure says so — each with
> the file or the read that produced it)
> **Stack**: Python 3.12 · Rust 2024 · TypeScript 5.7 · PostgreSQL 16 + PostGIS 3.6 + pgRouting + pgvector (the deployed arm64 image) · Valkey · NATS
> **Intelligence stack**: model-host **pplx-decider-v1-27b** (:8083) · llama-server **aditi-ps18-v7h Gemma 4 12B** (:8084) · embedding-service **pplx-embed-v2-late-0.6b + bge-m3** (:8085) · ONNX Runtime ≥1.20 · PyTorch ≥2.6 (oracle) / torch 2.14.0 (embedding venv pins)
> **Infrastructure**: Docker Compose (platform services) + supervised host model stack · Prometheus · Grafana

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Service Reference](#2-service-reference)
   - 2.1 [signal-ingress (Rust)](#21-signal-ingress)
   - 2.2 [traffic-oracle (Python)](#22-traffic-oracle)
   - 2.3 [convoy-brain (Python)](#23-convoy-brain)
   - 2.4 [model-host (T5 strategic tier)](#24-model-host-t5-strategic-tier)
   - 2.5 [embedding-service (T1 memory)](#25-embedding-service-t1-memory)
   - 2.6 [corridor-store (PostgreSQL)](#26-corridor-store)
   - 2.7 [command-deck (SvelteKit — the CANONICAL interface, :5273)](#27-command-deck--the-canonical-interface-5273)
   - 2.8 [Frontend Dashboard (React — LEGACY, offline fallback)](#28-frontend-dashboard--legacy-offline-fallback)
   - 2.9 [event-emulator (the emulated half)](#29-event-emulator-the-emulated-half)
3. [The Intelligence Stack (5 tiers, decider on top)](#3-the-intelligence-stack-5-tiers-decider-on-top)
4. [API Reference](#4-api-reference)
5. [Database Schema](#5-database-schema)
6. [ML Model Inventory](#6-ml-model-inventory)
7. [MCP Tools, Resources & Prompts](#7-mcp-tools-resources--prompts)
8. [AI Agent Architecture](#8-ai-agent-architecture)
9. [IPC & Data Flow](#9-ipc--data-flow)
10. [Hardware Constraints & Unified-Memory Budget](#10-hardware-constraints--unified-memory-budget)
11. [Infrastructure & Deployment](#11-infrastructure--deployment)
12. [Observability & Monitoring](#12-observability---monitoring)
13. [Testing Strategy](#13-testing-strategy)
14. [Security Model](#14-security-model)
15. [Configuration Reference](#15-configuration-reference)
16. [Waves 3–4: real roads, human authority and the contracts behind them](#16-waves-34-real-roads-human-authority-and-the-contracts-behind-them)

---

## 1. Architecture Overview

The platform is a **city-scale multi-agency operating system for corridors
and enforcement**: the same forecasting, routing, adaptive-situation and
audit machinery manages any corridor event expressible in the generalized
`events.corridor_events` model — eight documented use-case classes plus
the enforcement family's `crime_incident`/`vehicle_pursuit` kinds
(`docs/USE_CASES.md`) — of which the Z+ VVIP run is the base case, and the
same backbone carries the stolen-vehicle watchlist chain (earmark → ANPR
reads → evidence-only sighting, never a fine on the owner → a named
officer's approval → pursuit → interception plan) with five
dispatchable agencies (Jan Rakshak first-class). The **event-emulator**
(§2.9) is the emulated half of the stack: it injects exogenous *causes*
(procession schedules, weather onset, incidents, green-corridor requests,
emulated ANPR camera reads, patrolling unit positions) through the real
services' real APIs; it never produces outcomes. The **God's-eye canvas**
renders both families on one shared 3D picture
(`docs/GODS_EYE_INTEGRATION.md`).

```
                          ┌─────────────────────┐
                          │   command-deck       │  SvelteKit + CesiumJS (God's-eye)
                          │   (Presentation)     │  Port 5273 · operator PIN gate
                          └──────────┬──────────┘
                                     │ same-origin proxy: /api/v1 · /api/convoy · /api/emulator
                          ┌──────────▼──────────┐
              ┌ ─ ─ ─ ─ ─│    convoy-brain      │  Python · decider + Gemma + heuristics
              │  causes   │  (Orchestration)     │  Port 8092
              │  only     └──┬───────┬───────┬──┘
              │              │       │       │
              │  ┌──────────────▼─┐  ┌──▼───┐  ┌▼──────────────┐
              │  │ model-host     │  │NATS  │  │traffic-oracle │  Python + ONNX Runtime
              │  │ :8083          │  │:4223 │  │ (ML/Optim/    │  Port 8091
              │  │ pplx-decider-  │  │      │  │  Adaptive)    │
              │  │ v1-27b (T5)    │  │      │  └──────┬────────┘
              │  ├────────────────┤  │      │         │
              │  │ llama-server   │  │      │  ┌──────▼───────┐
              │  │ :8084 Gemma 4  │  │      │  │ PostGIS     │
              │  │ 12B Q4_K_M(T4) │  │      │  │ :56434      │
              │  ├────────────────┤  │      │  │ + Valkey    │
              │  │ embedding-svc  │  │      │  │ :6380       │
              │  │ :8085 (T1)     │  │      │  └─────────────┘
              │  └────────────────┘  │      │
              │          ┌───────▼──────┐
              │          │signal-ingress│  Rust · Arrow IPC · the 49-segment
              │          │              │  OSM network compiled in
              │          └──────┬───────┘
              │                 │
              │     ┌───────────┼────────────┐
              │     ▼           ▼            ▼
              │  Gov Traffic  Fleet GPS  Crowdsource   (edge-synthetic today:
              │    Feeds     Telemetry     Reports      every row data_quality
              │                                         'synthetic', §2.1)
   ┌──────────┴───────────┐        causes = corridor events via the C-F lifecycle,
   │ event-emulator :8090 │        Valkey adaptive:inputs docs in the exact reader
   │ (container           │        shape, ANPR reads via POST /vehicles/sightings,
   │ vvip-argus-event-    │        unit frames on corridor.units.emulated, one
   │ emulator)            │        twin POST, NATS corridor.traffic.emulated
   └──────────────────────┘        (never .live)
```

Ports are the deployed argus stack's (`scripts/veer_env.sh`,
`infra/compose.argus-override.yml`): every service except the deck binds
loopback; the deck (`0.0.0.0:5273`) is the one LAN-facing surface and sits
behind the operator PIN gate (§16.11). The services' own process defaults
are overridden by that deployment (`PORT` env); the deployed ports are the
ones quoted throughout this document.

### Service Registry

| Service | Language | Build Tool | Port | Purpose |
|---|---|---|---|---|
| signal-ingress | Rust 2024 | Cargo | — (NATS `:4223`) | Arrow IPC traffic feed ingestion over the 49-segment network |
| corridor-store | SQL/PostGIS | Docker | 56434 | Spatial DB + pgRouting + pgvector + memory embeddings + generalized `events.*` schema; migrations 001–020 |
| traffic-oracle | Python 3.12+ | uv | 8091 | ML prediction, route optimization, adaptive situation layer, city fusion, enforcement, dispatch, signals |
| convoy-brain | Python 3.12+ | uv | 8092 | Two-tier orchestration (decider strategic + Gemma tactical), live escort, MCP |
| model-host | Python | uv (persistent venv) | 8083 | T5 strategic decider — typed questions, single forward pass |
| llama-server | llama.cpp (build 69bf643) | cmake | 8084 | T4 tactical Gemma 4 12B, OpenAI-compatible + Prometheus |
| embedding-service | Python | uv | 8085 | T1 memory: multivector MaxSim + dense ANN |
| command-deck | TypeScript/Svelte | Bun/node | 5273 | The SvelteKit deck (adapter-node) + same-origin proxy + operator gate |
| **event-emulator** | Python 3.12 | uv | **8090** | Cause-only event injection: the use-case library driven through the real stack's real APIs (§2.9) |

Model hosts run **outside Docker** (native access to the GB10 unified-memory
pool) as systemd `--user` units — `veer-decider.service`,
`veer-llama.service`, `veer-embed.service` under `veer-models.target`, all
under `veer.target` (§11). The externally-active ollama daemon (`:11434`)
is **not part of the serving path** — it is untouched and used only as an
operator/debug side-load (`gemma-vvip:12b`, same GGUF;
`models-config/Modelfile.gemma_vvip`). The event-emulator runs as the
compose container `vvip-argus-event-emulator` on loopback `:8090`.

---

## 2. Service Reference

### 2.1 signal-ingress

**Language**: Rust 2024 Edition
**Build**: `cargo build --release`
**Dependencies**: tokio 1.0, reqwest 0.12, arrow 54, async-nats 0.38, tracing 0.1, chrono 0.4, serde 1.0

#### Module Structure

| Module | Files | Purpose |
|---|---|---|
| `feeds/` | traffic_gov.rs, mapping_api.rs, fleet_gps.rs, crowdsource.rs | traffic data source adapters + feed trait dispatcher |
| `pipeline/` | channels.rs, batch.rs | Bounded channels, batching with flush logic |
| `normalize/` | schema_align.rs | API response → `TrafficObservation` struct mapping |
| `anomaly/` | detector.rs, flags.rs, config.rs, stats.rs | Tier-1 inline anomaly detection (~500 ns per observation is the detector's design target, `detector.rs`; not benchmarked) |
| `publish/` | arrow_ipc.rs | Arrow IPC serialization → NATS JetStream publish |

#### Anomaly Detection (Tier-1, Inline)

8 checks performed at ingestion time:

| Check | Description |
|---|---|
| `future_timestamp` | Observation timestamp ahead of current time |
| `stale_data` | Observation older than expected freshness window |
| `speed_jump` | Speed delta exceeds physical plausibility threshold |
| `phantom_reading` | Speed/congestion values for inactive/nonexistent segments |
| `speed_zscore` | Speed deviates >3σ from rolling window mean |
| `congestion_outlier` | Congestion index outside expected band for road class |
| `road_class_mismatch` | Speed inconsistent with road class (e.g., 120 km/h on residential) |
| `spatial_mismatch` | Observation coordinates outside segment geometry buffer |

#### Pipeline Constants

```
FEED_CHANNEL_CAPACITY    = 10,000
BATCH_CHANNEL_CAPACITY   = 100
BATCH_FLUSH_COUNT        = 100
BATCH_FLUSH_TIMEOUT_MS   = 100
```

#### The network it observes, and what its rows are

No live ITMS / fleet-AVL / crowd connector is wired yet, so every collector
runs **edge-synthetic** and every row is published with
`data_quality = synthetic` — never presented as a measurement
(`services/signal-ingress/README.md`). The four collectors observe one
shared congestion field over the city network compiled into the binary:
`src/feeds/segments_ahmedabad.rs`, GENERATED by
`scripts/gen_ingress_segments.py` from `corridor.road_segments` — the 49
OpenStreetMap segments of §16.1 (ids 1001–1049, 37 distinct endpoint
nodes; © OpenStreetMap contributors, ODbL 1.0). `gen_ingress_segments.py
--check` fails when the store and the compiled table drift (a
`verify_all.sh` tier). The C-H anomaly document carries an additive
`feed_quality` — the reading's quality BEFORE the detector relabels it
`Anomalous` (`Synthetic` on these feeds) — so a flag raised on simulated
data is never mistaken for a field anomaly
(`src/normalize/schema_align.rs`); the oracle reads it as
`props.data_quality` on anomaly entities (`city/entities.py`
`anomaly_quality`). Every row lands on its own road: the crowdsource
feed's phone-GPS error (12 m σ) is truncated at the **30 m on-road bar**
(`crowdsource.rs` `ON_ROAD_BAR_M`), and the feed tests hold all four feeds
to that bar against the real segment geometry. `cargo test --locked`:
**120 passed** (49 lib + 7 + 45 + 19, run in `rust:alpine`, 2026-10-23).

---

### 2.2 traffic-oracle

**Language**: Python 3.12+
**Build**: `uv sync && uv run python -m traffic_oracle`
**Key Dependencies** (`services/traffic-oracle/pyproject.toml`): torch ≥2.6, onnxruntime ≥1.20, ortools ≥9.11, networkx ≥3.4, igraph ≥0.11, polars ≥1.15, geopandas ≥1.0, shapely ≥2.0, duckdb ≥1.1, asyncpg ≥0.30, valkey ≥6.0, nats-py ≥2.9, scikit-learn ≥1.5, **httpx ≥0.27** (plan C10 — twin/ASGI transport; outbound hosts are config-fixed), **gymnasium ≥1.0** (adaptive stream's TwinEnv wrapper, C5/C10), pyyaml ≥6.0 (unified-memory budget reader)

#### Module Structure

| Module | Key Files | Purpose |
|---|---|---|
| `data/` | db.py, models.py, graph_queries.py, cache.py, vector_store.py | Database access, Pydantic models, pgRouting queries, Valkey cache, pgvector search |
| `predict/` | flow_forecaster.py, eta_model.py, data_loader.py, training_pipeline.py, congestion_heatmap.py | DSTGAT forecasting, HistGBT ETA, training data preparation |
| `predict/nn/` | dstgat.py, graph_attention.py, temporal_conv.py, congestion_gate.py, quantize.py | Neural network architecture |
| `optimize/` | corridor_router.py, diversion_planner.py, constructive_heuristic.py, mip_solver.py | OR-Tools CP-SAT routing, MIP diversion optimization |
| `optimize/rl/` | agent.py, environment.py, reward.py, trainer.py | Factored DQN signal controller (+ Gymnasium TwinEnv wrapper, C5) |
| `adaptive/` | situation.py, service.py, engine_loop.py, valkey_mirror.py, hysteresis.py, escalation.py, degradation.py, shadow.py, twin.py, router.py | **T2 adaptive layer** — see below |
| `vehicles/` | `__init__.py` (API), store.py, sources.py, pursuit.py | **Watchlist & enforcement** (010): earmarks/sightings/traces API, the C-E ANPR ingest seam (`POST /api/v1/vehicles/sightings` — plates normalized server-side, idempotent on `read_id`, hits computed from ACTIVE earmarks), parameter-bound SQL incl. read-path retention reaps (health at `GET /api/v1/vehicles/retention`), the `AnprSource` port + `EmulatedAnprSource`, and the pgRouting interception plan (C3/C6) |
| `enforcement/` | `__init__.py` (API), rules.py, engine.py | Deterministic e-challan rules (`R_SPD_SEGMENT`, `R_SIGNAL_VIOLATION`, `R_CLOSED_CORRIDOR_ENTRY`) → PENDING recommendations; under a stolen-vehicle earmark ONE evidence-only `R_STOLEN_SIGHTING` instead (C-N, §16.3); the human-approval decision endpoint with append-only audit tail (C6) |
| `units/` | router.py, projection.py, agencies.py | Agency dispatch channel (011): closed agency registry (Jan Rakshak first-class + police/traffic/108/fire), `GET /api/v1/dispatches/{id}`, deterministic emulated-unit projection (`emulated_dispatch_projection`) + emulator patrol frames (`emulated_patrol`, C-A) |
| `signals/` | router.py, plans.py, writer.py, gate.py | City signal-plan writer (C7): band→plan table, default-OFF flag, twin shadow-eval fail-closed, snapshot/restore ledger; city-band provenance (`city_band_since` / `_stale` / `_simulated_feed`, C-Q) and the 409 `city band unknown — pass band explicitly` |
| `city/` | router.py, entities.py, dossier.py, common.py | **City fusion (C-C)**: `GET /api/v1/city/entities`, `/city/entity/{kind}/{id}`, `/city/feed`, `/cameras`, `/events/{id}/audit`; the ONE lawful-read predicate `SQL_LAWFUL_READ` every sighting surface composes (C6 parity, §16.12); plate masking of every label and feed text |
| `events/` | `__init__.py` | Corridor events (007/013/018): create, `/active`, history, the C-F lifecycle `POST /{id}/status`, and route updates `POST /{id}/routes` (C-O, §16.4) |
| `spatial/` | router.py | **Spatial router (C-S)**: `GET /api/v1/spatial/segments(/{id})` and `/api/traffic/segments` with `geometry_basis` `osm` \| `osm_display` \| `schematic` (C-K, §16.8) |
| `simulation/` | engine.py, twin.py, scenarios.py, calibration.py, network.py, events.py, rng.py, tod.py, router.py | **Digital-twin simulation engine** (the twin behind adaptive shadow-eval and `simulate_twin`) |
| `ingest/` | nats_consumer.py, arrow_decoder.py, db_writer.py, cache_writer.py | NATS → Arrow decode → DB bulk insert + cache write |
| `anomaly/` | stream_detector.py, ml_detector.py, feature_builder.py | The Tier-1 anomaly stream consumer (`stream_detector.py`: the ingress's flags → `traffic.anomaly_log`); `ml_detector.py`'s Isolation Forest is code only, not wired into the live path (§6.3) |
| `synthetic/` | generator.py, gap_detector.py, pattern_matcher.py, spatial_interpolator.py, confidence_scorer.py | Gap-filling synthetic data generation |
| `evaluate/` | scenario_sim.py | Monte Carlo scenario comparison |
| `runtime/` | gpu_arbiter.py, onnx_serve.py, memory_profiler.py, thread_pool.py | Unified-memory allocation, ONNX session management, memory guards |
| `ops/` | `__init__.py` (API), router.py, degradation_taxonomy.py | **Wave-3 degradation ledger** — read-only failure surfaces over shared fleet state: mines convoy-brain's `decisions_log` into a recurring-cause taxonomy (`GET /api/v1/ops/degradation-taxonomy`, §13) |

#### The Adaptive Layer (T2)

- **SituationEngine** (`adaptive/situation.py`) — a *deterministic* classifier:
  security tier (X=0/Y=2/Z=4/Z+=6) × traffic regime (FREE_FLOW=0…GRIDLOCK=6)
  × weather (CLEAR=0…STORM=3) × crowd density (LOW=0…EXTREME=3), +2 anomaly
  flag, +1 event flag → score 0–21 → bands **GREEN 0–4, AMBER 5–9, RED 10–14,
  BLACK 15–21**. Monotone in every input: worsening any single dimension can
  never soften the band; a Z+ movement is never below AMBER.
- **AdaptiveService** (`adaptive/service.py`) — hysteresis + dwell on class
  transitions, escalation with sustained-RED tracking, degradation manager,
  shadow evaluation against the simulation twin, and C4 parameter publishing
  (protocol-floored, fail-closed).
- **Engine loop** (`adaptive/engine_loop.py`) — one asyncio task, 5 s poll:
  lists active movements from `convoy.movements` (parameter-bound SQL), reads
  `adaptive:inputs:{movement_id}` and `adaptive:anomaly:active` from Valkey,
  classifies, publishes `situation:changed` on transitions and mirrors
  parameters. Missing inputs are skipped, never fabricated; a dead input path
  is surfaced via `/api/v1/adaptive/status`.
- **Valkey mirroring** (`adaptive/valkey_mirror.py`) —
  `adaptive:params:{movement_id}` (TTL 30 s) and `situation:changed`
  (TTL 60 s — the decider's cadence trigger #3).
- **Read-only params (the audit-noise fix).** `GET /api/v1/adaptive/params`
  serves the subject's last PUBLISHED decision, or the tier's
  floor-clamped baseline for an untracked subject (`evidence.read_only:
  true`) — no audit row, no mirror write, no subject state is ever created
  by a reader (`adaptive/router.py`, `service.current_params`). Only the
  engine loop and `POST /adaptive/observe` decide (`params_for`).
- **Publish only on change, with a reason.** A decision writes ONE
  `adaptive.param_overrides` row when it changed, labelled
  `evidence.publish_reason` = `first_publish` | `class_change` |
  `param_change` | `verdict_change` | `escalation_change` |
  `degradation_change` (`service.py` `_publish_reason`); between changes
  the loop re-SETs the same mirror payload every 10 s so the 30 s key
  lapses only when the loop dies. A failed audit write is retried, and one
  subject's decisions never interleave.
- **Calibration prime and the field gate.** At startup the lifespan awaits
  `CalibrationScheduler.prime()` (bounded, never fatal; an overdue prime is
  abandoned, not awaited) before the engine loop's first decision, so the
  first publish already quotes the true refusal reason
  (`simulation/scheduler.py`). The twin counts as field-calibrated only
  with ≥ **24 distinct field hours** of REAL hourly aggregates
  (`MIN_FIELD_HOURS`) AND a replay MAE ≤ **5.0 km/h**
  (`_MAE_TOLERANCE_KMH`, `simulation/calibration.py`); synthetic rows never
  count. A fresh install has 0 real field hours, so every non-baseline
  plan — and the city signal writer — fails closed with
  `twin_not_calibrated: no field observations — simulated feed only`
  (a fresh install's `/health` reads `twin_calibration` …
  `waiting_for_field_data`).
- **Tick abandon and the stale band.** A tick that hangs (a database that
  stops answering instead of refusing) is abandoned after
  `TICK_TIMEOUT_SEC = 30` and counts as a failed poll; while polls fail the
  city band is marked **stale** — unknown, not at rest
  (`city_band.stale`, `city_band_stale_since`) — and a subject whose inputs
  are missing for `STALE_INPUTS_SEC` (300 s) is evicted, so a database
  outage never freezes a stale band in place (`adaptive/engine_loop.py`).
  A completed or cancelled event is evicted on the next tick
  (`retain_only`), so it can never hold the city band.
- **Same-provenance inputs.** Emulated traffic lives in its own key space
  (`traffic:emulated:latest:{segment_id}`, §9): a LIVE event's band reads
  `traffic:latest:*` only — emulated traffic can never drive a real
  event's band; an EMULATED event prefers its scenario's emulated rows. The
  audit row says which (`inputs_origin` live | emulated | mixed,
  `simulated_feed`).

#### HTTP Endpoints (Port 8091 on the argus stack)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Service heartbeat |
| `GET` | `/metrics` | Prometheus exposition |
| `POST` | `/api/v1/predict/flow` | DSTGAT T+5/10/15/30 traffic forecast |
| `POST` | `/api/v1/predict/eta` | HistGBT travel time prediction |
| `POST` | `/api/v1/optimize/routes` | OR-Tools CP-SAT multi-objective routing |
| `POST` | `/api/v1/optimize/diversions` | Per-segment diversion plans with timing |
| `POST` | `/api/v1/evaluate/scenarios` | Monte Carlo scenario comparison |
| `GET` | `/api/v1/graph/shortest-path` | pgRouting Dijkstra |
| `GET` | `/api/v1/graph/k-shortest-paths` | pgRouting KSP alternatives |
| `GET` | `/api/v1/spatial/segments?min_lon&min_lat&max_lon&max_lat` | Bbox segment read with `geometry_basis` (`osm` for all 49 on the real network, C-K/C-S) |
| `GET` | `/api/v1/spatial/segments/{id}` | One segment; `length_m` is the routing geodesic length |
| `POST` | `/api/v1/traffic/live` | Live-cache read (Valkey, newest row per segment): `observations[]` (each with `road_name`, `origin`) + `emulated_observations[]` from the emulator's own plane, labelled, never merged |
| `GET` | `/api/v1/traffic/historical?segment_id&hours&day_of_week&hour` | REAL hourly aggregates + `synthetic_aggregates` (the simulated feed's rollup, labelled `data_quality: synthetic`), `provenance`, `basis` (last-n-hours or a same-slot IST baseline over 672 h) — C-P, §16.5 |
| `GET` | `/api/v1/traffic/history/{id}` | 24h history for segment |
| `GET` | `/api/v1/anomalies/recent` | Anomalies in last hour |
| `GET` | `/api/v1/corridor/summary` | Aggregated corridor conditions |
| `GET` | `/api/v1/movements/active` | Active convoy movements |
| `GET` | `/api/v1/adaptive/params` | C4 adaptive parameters |
| `GET` | `/api/v1/adaptive/status` | Degradation + situation status (+ engine-loop health) |
| `POST` | `/api/v1/adaptive/observe` | Feed one situation observation |
| `GET` | `/api/v1/adaptive/situation_log` | Band audit history (board primary rung; adaptive surfacing router) |
| `GET` | `/api/v1/adaptive/module` | AMA module card as data (R9) |
| `GET` | `/api/v1/ops/degradation-taxonomy` | **Wave-3 degradation ledger** — recurring degradation/failure causes mined from convoy-brain's `decisions_log` (latch firings, tier-0 misses, tactical fallbacks, operator alerts, failed turns; honest-empty taxonomy when the log is clean, every entry sample-referenced — §13) |
| `POST` | `/api/v1/vehicles/{plate}/earmark` · `POST .../earmark/clear` | Watchlist earmark (lawful basis + capped retention) / clear |
| `POST` | `/api/v1/vehicles/sightings` | **C-E ANPR ingest**: `{read_id, plate, camera_id, t, lon, lat, …, source}` → `{sighting_id, vehicle_id, duplicate, hit, recommendations[]}`; 409 for a read > 30 s in the future; `live_anpr` refused unless `VVIP_ACCEPT_LIVE_ANPR=1` |
| `GET` | `/api/v1/vehicles` · `/{id}/sightings` · `/{id}/trace` · `/retention` | Earmarked vehicles / lawful camera reads (count-free 404 without a lawful basis) / timed trace (all source-labelled) / the retention policy + reaper health |
| `POST` | `/api/v1/enforcement/evaluate` | Run deterministic rules for one sighting → PENDING recommendations |
| `GET`/`POST` | `/api/v1/enforcement/recommendations[/{id}/decision]` | List recommendations — each row carries `pursuit` `{event_id, status}` (the pursuit its approval authorised) or null; the human approve/reject transition (officer identity mandatory, append-only audit tail); approving a PENAL rule of a reported-stolen vehicle → **409** `e-challan withheld — vehicle reported stolen` (C-N) |
| `POST`/`GET` | `/api/v1/events` · `/api/v1/events/active` | Create a corridor event (spines + phases, one transaction) / the live board. A `vehicle_pursuit` needs an actively earmarked vehicle and a human-APPROVED recommendation of it, else **422**: for a vehicle reported stolen only its `R_STOLEN_SIGHTING` (an e-challan basis → 422, C-N), and one approval authorises one pursuit (an approval that already authorised a pursuit → 422) — §16.3 |
| `POST` | `/api/v1/events/{id}/status` | C-F lifecycle transition (named officer; 409 illegal transition) |
| `POST` | `/api/v1/events/{id}/routes` | C-O route update: the new spine becomes primary, the old one SUPERSEDED (never deleted) — §16.4 |
| `GET` | `/api/v1/events/{event_id}/interception-plan` | pgRouting choke-junction ranking for a `vehicle_pursuit` (C3) |
| `GET` | `/api/v1/events/{event_id}/audit` | The event's audit feed (C-C) |
| `POST` | `/api/v1/events/{event_id}/dispatch` · `POST /api/v1/dispatches/{id}/decision` · `POST /api/v1/dispatches/{id}/status` | Request an agency unit / duty-officer approve-defer / lifecycle walk (011) |
| `GET` | `/api/v1/dispatches` · `/api/v1/dispatches/{id}` | Dispatches with their append-only audit tails; one dispatch (404 unknown, 422 malformed uuid) |
| `GET` | `/api/v1/units` | Emulated agency units (deterministic projection or emulator patrol frames; `emulated: true`) |
| `GET` | `/api/v1/city/entities` · `/city/entity/{kind}/{id}` · `/city/feed` · `/cameras` | City fusion (C-C): the unified picture, an entity dossier, the audit feed, the camera registry (28 emulated sites on a fresh install). An event's marker sits ON its rank-1 route — the route point nearest the route's centroid, `props.position_basis: spine_point_nearest_centroid` (`zone_centroid` only for an event with no route); a dispatch with no named waypoint is drawn at its event's marker (`event_marker`) — §16.12 |
| `GET` | `/api/v1/signals/state` · `/signals/plans` · `POST /signals/plans/apply` · `POST /signals/restore` · `GET /signals/history` | City signal surface (C7; writer default OFF, shadow-eval fail-closed, snapshot/restore; an apply with no band while the city band is stale → **409** `city band unknown — pass band explicitly`) |
| `POST` | `/api/v1/simulate/twin` · `/twin/compare` · `/replay` | The digital twin (shadow-eval, `simulate_twin`, calibration replay) |
| `GET` | `/api/traffic/snapshot` | Frontend-facing traffic snapshot |
| `GET` | `/api/traffic/segments` | Frontend-facing segment query |
| `POST` | `/api/traffic/predict` | Frontend-facing prediction |

(The simulation router — twin endpoints — is included when the optional
simulation stack is importable; without it, twin endpoints are absent and
adaptive shadow-eval fails closed.)

---

### 2.3 convoy-brain

**Language**: Python 3.12 (`requires-python >=3.12`; the image is `python:3.12-slim`)
**Build**: `uv sync && uv run python -m convoy_brain`
**Key Dependencies**: fastapi, httpx, structlog, valkey, prometheus-client, pydantic 2.10

#### Module Structure

| Module | Key Files | Purpose |
|---|---|---|
| Root | `__main__.py`, `orchestrator.py`, `decider.py`, `adaptive_params.py`, `http_guard.py`, `config.py`, `metrics.py`, `city_link.py`, `gps_sim.py` | FastAPI app (`PORT`; 8092 on the argus stack), two-tier turn loop + fallback chain, strategic-tier policy (cadence/budgets/degradation), fail-closed adaptive client, outbound URL guard, service-URL resolution |
| `llm/` | `decider_client.py`, `llama_bridge.py`, `heuristics.py`, `parsing.py`, `schemas.py` | T5 typed-question client, T4 Gemma bridge (schema-constrained ladder), deterministic heuristic floor, JSON extraction, agent output schemas |
| `agents/` | traffic_analyst.py, route_planner.py, security_liaison.py, diversion_coordinator.py | 4 tactical agents on Gemma (structured output schemas); `community_liaison.py` (a directive drafter for approved recommendations) is registered but not wired into any workflow |
| `workflows/` | pre_movement.py, live_escort.py, post_clearance.py | 3 state-machine workflows |
| `mcp/` | server.py, prompts.py | MCP server: 28 tools + 4 resources (both defined inline in `server.py`), 3 prompt templates (§7) |
| `memory/` | convoy_context.py, store.py | Convoy state + **decisions audit log**, backed by Valkey |

> **Retired**: `ollama_bridge.py` is gone (the retired Qwen/Ollama serving
> path). Its replacement surface is `llm/decider_client.py` +
> `llm/llama_bridge.py`. The compose file keeps `OLLAMA_BASE_URL` only as a
> documented legacy transition variable.

#### Orchestrator Configuration (`orchestrator.py`)

| Setting | Value | Notes |
|---|---|---|
| Tool selection | **decider** (`choice` over the tool menu) | one question per round, ≤249 options; the Gemma fallback selector is enum-constrained (`orchestrator.py` `_tool_selection_schema`) |
| Argument drafting | **Gemma** against the tool's `inputSchema`, inside a `json_schema` response envelope | llama-server compiles the envelope (tool schema embedded under `arguments`) to GBNF, so generation stops at the closing brace — the round-1 110 s ramble is structurally impossible (`orchestrator.py` `_tool_args_schema`); 1 repair round on validation failure |
| Fallback floor | deterministic heuristics | keyword tool selection + default args (`llm/heuristics.py`) |
| Max Context | 8192 tokens per Gemma slot | 2 slots |
| Per-call output budgets | 48 / 192 / 512 tokens (select / args / final) | `config.py:68-70` — `TACTICAL_{SELECT,ARGS,FINAL}_MAX_TOKENS` (final raised 384 → 512: a 1200-char reasoning string was cut mid-string at 384); serving `num_predict` stays 2048, the bridge overrides per call |
| Per-call HTTP wall | **max(30 s, 5 s + max_tokens ÷ 16 tok/s)** (was a flat 120 s, then a flat 30 s) | `config.py` — `LLAMA_CALL_TIMEOUT_SEC` floor stretched to cover the call's own budget (`LLAMA_PREFILL_ALLOWANCE_SEC`, `LLAMA_DECODE_FLOOR_TOK_PER_SEC`); the plan and turn deadlines still bound every call from outside |
| Turn budget enforcement | `TOTAL_TURN_TIMEOUT_SEC` 120 s drives a deadline enforced via `asyncio.wait_for` on every LLM call, with per-tool timeouts shrinking to the deadline | `orchestrator.py` `_run_turn` deadline + `_deadline_timeout` — no single generation can exceed the client budget |
| Concurrency | serialized decider (`asyncio.Lock` in client) + 2 llama slots | the v1 whole-stack VRAM lock is retired |
| Max Tool Rounds | 6 | |
| Tool Timeout | 30 s | |
| Turn Timeout | 120 s | |
| Max Retries/Tool | 2 | |
| Tool-result truncation | 4096 B | `config.TOOL_RESULT_TRUNCATE_BYTES` |

#### Service-URL resolution (contract C0)

`config.load_service_urls()` resolves each model endpoint in this order:
(1) env `MODEL_HOST_URL` / `LLAMA_BASE_URL` / `EMBEDDING_SERVICE_URL`,
(2) the `services:` block of `models-config/configs/veer_models.yaml`,
(3) compile-time defaults `http://127.0.0.1:{8083,8084,8085}`. Docker
containers override to `host.docker.internal` (see §11).

#### Live escort and escort authority (contract C-J, WP-12 + WP-21)

`POST /movements/{id}/escort` starts the live escort as a background task
(202; 409 while one runs); `GET /movements/{id}/escort` is its C-J status
document; positions arrive by `POST /movements/{id}/position` (or, on a
demo stack with `VVIP_ESCORT_SIM_GPS=1`, an EMULATED GPS walker labelled
`gps.mode: emulated`) and are mirrored to `convoy:position:{movement_id}`
(C-B). The GPS loop is LLM-free. **The decider never switches a route on
its own:** a hold, Plan B, an emergency re-route and the closures a new
route brings are raised as ONE open PROPOSAL per escort that a named
officer approves or rejects; a proposal nobody answers within
`VVIP_ESCORT_APPROVAL_TIMEOUT_SEC` (default **90 s**, clamped **15–600**)
expires and the convoy **holds its current approved plan** — it neither
stops nor switches. The full contract — proposal fields,
`POST /movements/{id}/escort/approvals/{approval_id}` (200/404/409/422),
`GET /escorts` — is §16.2.

---

### 2.4 model-host (T5 strategic tier)

**Language**: Python (persistent venv `.venvs/model-host` under `$VEER_VENV_DIR`; requirements `models-config/model-host-requirements.txt`)
**Run**: the systemd `--user` unit `veer-decider.service` (`infra/scripts/serve_models.sh --foreground decider`, `Restart=always`) — binds `127.0.0.1:8083`

Serves **pplx-decider-v1-27b** as typed questions. It is **not** a chat
model — the jinja chat template in the snapshot is never used.

**Model on disk (verified this revision):** HF snapshot
`models--perplexity-ai--pplx-decider-v1-27b/snapshots/b01a5cbaca5391f73bd55103d4f27e8982cd5e60`
— 11 BF16 safetensors shards totalling exactly **52,170,802,640 B
(48.58 GiB)**, plus a separately-measured decision readout head
**`readout.safetensors` = 2,611,288 B** (a double symlink into blobs, like
every snapshot file). **Load-path rule (C1):** load via the snapshot
*directory path* — every file is a double symlink into `../../blobs`; a
naive `cp -r` produces dangling stubs. Never copy the tree. Cold load ≈5 min;
the model stays resident forever (no eviction path).

**Decision configuration** (baked into the snapshot's `decision_config.json`,
never overridden): temperature **2.207568…**, a readout alphabet of 255
letter-codes (`codes`/`token_ids`), of which **249 single-token codes** are
the enforced option budget for `choice` questions
(`model_host/decider_runtime.py:27`, `convoy_brain/llm/decider_client.py:39`).

#### API (contract C1)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/decider/health` | `{loaded, loading, load_error, resident_rss_gib, cuda_reserved_gib, p95_decide_latency_ms, queue_depth, decides_total, decides_failed, model}` |
| `POST` | `/warmup` | Wait for load + one warm decision (idempotent) |
| `POST` | `/decide` | `{request_id?, state, questions}` → answers; **all questions in one request are batched into a single forward pass** |
| `GET` | `/metrics` | Prometheus (`model_host_*`) |

Question types: `choice` (options ≤249, list or label→description dict),
`noul` (P(true); model emits no confidence for noul), `score` (scale = ≥2
level descriptions; value = expected level). ≤6 questions per call (422
beyond). Every answer carries `{value, probability, confidence, distribution}`.

Errors: `429` + `Retry-After: 5` when 8 requests already queued (serialized
compute — one forward at a time); `503` while loading; `413` when the encoded
state exceeds the **8192-token hard cap** (no truncation, by design); `422`
on malformed questions.

**Degradation contract:** convoy-brain polls `/decider/health` every 15 s;
when p95 stays above **3000 ms** (or the host is unreachable) the brain drops
to Gemma-only tactical mode and raises an operator alert — every transition
audit-logged brain-side. *Latch fix (bench-repair pass):* once degraded, no
decides flow, so the server's p95 window goes stale and the tier used to
stay degraded forever while direct probes answered in ~0.9 s. Before acting
on any p95 breach — degrading **or** staying degraded — `refresh_health`
now runs one minimal probe `/decide` that must itself beat the 3000 ms line
(`decider.py` `refresh_health`; `DECIDER_P95_DEGRADE_MS` unchanged).

---

### 2.5 embedding-service (T1 memory)

**Language**: Python (persistent venv; `models-config/embedding_launcher.py` is the models-owned launcher)
**Run**: the systemd `--user` unit `veer-embed.service` (`infra/scripts/serve_models.sh --foreground embed`) — binds `127.0.0.1:8085`

Late-interaction memory: **pplx-embed-v2-late-0.6b** multi-vector embeddings
(128-d per-token vectors, MaxSim rerank; 1.11 GiB weights) with **bge-m3**
dense ANN recall (1024-d unit-norm; 1.06 GiB weights). An optional 9B aux
embedder exists in config but is **disabled by default**.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/embed/documents` | Encode + upsert memory docs (late + dense) |
| `POST` | `/search` | Hybrid: dense ANN `recall_k=100` → MaxSim rerank `top_k=20` |
| `POST` | `/incidents` | Record a live incident **or** a simulation scenario run |
| `GET` | `/incidents/recent` | Recent incident/pattern memory |
| `GET` | `/health` | Heartbeat |

Storage: `traffic.memory_docs`, `traffic.incidents`,
`traffic.memory_segment_embeddings` (corridor-store migration
`004_memory_embeddings.sql`). The brain queries this whenever it needs
"similar past situations" — the retrieval spine that grounds decider
checkpoints and tactical drafts in corridor history.

---

### 2.6 corridor-store

**Engine**: PostgreSQL 16 + PostGIS 3.6 + pgRouting 4.0 + pgvector 0.8 — the deployed arm64 image (`infra/docker/Dockerfile.postgis.arm64`: `pgvector/pgvector:pg16` + Debian's PostGIS / pgRouting packages; the live container reports PostgreSQL 16.15). The x86 `Dockerfile.postgis` (PostgreSQL 17 + PostGIS 3.5) publishes no arm64 image; the migrations use no version-specific syntax.
**Extensions**: `postgis`, `pgrouting`, `vector`, `pg_trgm`

#### Schemas

- `corridor` — Road network (segments on OpenStreetMap roads, junctions on OSM nodes, adjacency graph, routing views), camera sites, agency stations, signal programs, the vehicle watchlist, dispatches, signal-plan ledgers
- `traffic` — Observations (range-partitioned), REAL hourly aggregates and the separate synthetic rollup, anomaly log, synthetic data, segment embeddings (pgvector), **memory docs / incidents / memory embeddings (T1)**
- `convoy` — Movement records, planned routes, diversions
- `events` / `emulator` / `adaptive` / `simulation` — corridor events + their status ledger, e-challan recommendations, the emulator's runs ledger, the adaptive audit tables, twin runs (§5)

#### Migrations and the seed (a fresh install = 20 migrations + 4 data steps)

`scripts/migrate.py` applies `services/corridor-store/migrations/001…020`
in order and ledgers each in `corridor.schema_migrations` (name, checksum,
applied_at); 001–006 are byte-immutable (checksum ledger), 007–020 are
additive, idempotent and schema-only (no migration references a seed
row). The data then lands in the order `scripts/veer_up.sh` `seed()` runs
it: `sample_corridor.sql` (only into an empty store) →
`reanchor_osm_network.sql` (completes and, on an existing store, converts
the network to the OpenStreetMap roads in place — ids preserved, the
EMULATED camera reads and the use-case profiles' spines and text carried
onto the real roads, a second apply is a no-op; applied whenever the
store is not on the generated network, §11 / §16.1) →
`post_seed_derivations.sql` (camera sites, stations, junction names,
signal programs) → `display_geometry_osm.sql`. A fresh
install built that way (this revision's private stack) ledgers **20**
migrations and holds 49 segments, 34 junctions, 28 camera sites, 18
agency stations and 11 signal programs. The per-migration list is §5.

#### OSM Importer and the network generator

`corridor-importer` (Python CLI) extracts OpenStreetMap data via OSMnx into
the `corridor` schema. The **network itself** is produced by
`scripts/gen_osm_network.py` — deterministic, from committed Overpass
extracts — which writes `sample_corridor.sql`, `reanchor_osm_network.sql`
and the evidence file `scripts/data/osm_network.json` (§16.1).

---

### 2.7 command-deck — the CANONICAL interface (:5273)

**Framework**: SvelteKit 2.15 + Svelte 5.15 (runes syntax), adapter-node
**Build**: `bun install && bun run build` (deployed as the compose service `command-deck`, image `vvip-argus-command-deck`, host-networked on `0.0.0.0:5273`; `bun run dev` for development)
**Key Dependencies** (`package.json`): Cesium 1.146 (vendored under `static/cesium`), MapLibre GL 5.0, deck.gl 9.1

**Status (wave 4):** the command-deck is the **canonical interface for
every audience** — operator, agency, citizen, 108 driver, comms officer —
reached at `http://127.0.0.1:5273` on the GB10 itself (the demo access
the team chose). The React `frontend/` (§2.8) is the **offline fallback
demo** only. The deck is the one LAN-facing service, so it carries the
**operator PIN gate** (`VEER_OPERATOR_PIN`, §16.11): operator surfaces and
every proxied API need a signed-in, named officer; `/citizen` and
`/driver` stay public. Its server-side proxy pins every upstream to
loopback (`/api/v1` and `/api/traffic` → oracle `ORACLE_PORT` 8091,
`/api/convoy` → brain `BRAIN_PORT` 8092, `/api/emulator` → emulator
`EMULATOR_PORT` 8090 — `src/hooks.server.ts`) and forwards the signed-in
officer as `x-veer-officer`.

#### Surface map (route groups under `src/routes/`)

| Group | Route | Theme | Surface |
|---|---|---|---|
| `(deck)` | `/ops` | `command` | **The situation wall** — the operator's wall; `/` is the Convoy Command cockpit (see the `/` row below), served directly with no route rewrite (asserted by design-smoke: `/` = 200 + `data-template="convoy-command"`). Band masthead (`data-prim=band-masthead`), Corridor Operations Board, Pursuit & Watchlist, Decision Queue, Jan Rakshak Dispatch, City Signal Plans, AMA module card, Decision-Audit Stream, band history, twin shadow-eval comparison, Event Emulator panel, and the wall God's-eye canvas (`.ge-canvas`) |
| `(deck)` | `/gods-eye` | `command` | Fullscreen CesiumJS cinematic — eleven integrity-validated layers (`layers/index.ts`), FLIR sensor modes, share links; Google Photorealistic 3D Tiles for a signed-in operator on an ion-keyed deck, the keyless OSM globe otherwise (§16.9); viewer readiness at `window.__GODS_EYE__` |
| `(deck)` | `/comms` | `command` | Trilingual advisory desk — EN/हिंदी/ગુજરાતી columns, same facts verbatim |
| `(deck)` | `/chat` | `command` | Takeover chat console (context header, stream, degradation chip) |
| `(deck)` | `/design` | both | The living style sheet — every primitive mounts (`[data-prim]`), both theme previews |
| `(public)` | `/citizen` | `daylight` | Citizen Advisory — plain-language band strip, event cards from the deck's own server-side projection (`/api/public/events`); an emulated or test event is badged SIMULATED FEED · EMULATED, never LIVE (`emulated_labelling: true`, C-M); EN/HI/GU via the `veer_lang` cookie |
| `(public)` | `/driver` | `daylight` | 108 Green-Corridor flow: idle → awaiting the duty officer (`requested`) → approved by a named officer on `/ops` → open window with live countdown + golden-hour counter; with no live event up, a labelled scripted fallback (`data-mode="scripted"`) walks idle → evaluating → proposal → open instead |
| `(deck)` | `/` | `command` | **Convoy Command cockpit** — the restored VVIP template on the live stack (planner/escort/ladder/trace; 2026-10-14 merge, see `../../TEMPLATE_RESTORATION.md`) — replaced the retired `(legacy)` group |

The deck's own server routes: `POST/GET/DELETE /api/session` (operator
sign-in, §16.11), `GET /gods-eye/mode` (keyless vs ion-keyed basemap,
§16.9), `GET /gods-eye/feeds`, and the public projections
`/api/public/events` + `/api/public/dispatches/{id}` that feed `/citizen`
and `/driver` without exposing any operator API.

#### Design system — one token contract, two themes

`src/lib/design/tokens.css` + `tokens.ts` are **the single token contract**:

- **Two themes, one variable list**: `command` (operator graphite — the map
  is the hero, chrome is furniture; matte surfaces lifted by 1px hairlines,
  never drop shadows) and `daylight` (citizen/driver — warm paper, one
  orange accent, calm band strips with plain-language consequences).
  `:root` carries command values so a no-JS first paint defaults to the
  operator theme; `(public)` layouts flip `data-theme="daylight"` on
  `<html>`.
- **Components consume semantic tokens only** (`var(--v-*)`); canvas code
  (Cesium/deck.gl/SVG) cannot read CSS vars and calls `bandColor()` /
  `cssVar()` from `tokens.ts` instead — `tests/design-tokens.mjs` asserts
  key parity between the two files.
- **Colour discipline**: the human-act orange (`--v-act`) is the single
  "a human must act" colour — never fills large areas, never paints nav;
  amber is reserved exclusively for the city band ladder
  (GREEN/AMBER/RED/BLACK); the BLACK chip is an inverted chip (hairline +
  translucent fill, never an opaque void).
- **Boundaries (R4)**: the design-shell never imports ops/gods-eye;
  public/comms never import the legacy stores; `tokens.css` is imported
  once — enforced textually by `tests/design-tokens.mjs` (parity, hex
  sweep, boundaries: exit 1 on any violation; zero browser).

#### Interface acceptance cameras (all exit-0 gated)

- `tests/design-smoke.mjs` — `/` IS the **Convoy Command cockpit** (200 +
  `data-template="convoy-command"`); the **live URLs** (`/`, `/ops`,
  `/gods-eye`, `/comms`, `/chat`, `/citizen`, `/driver`, `/design`)
  return 200; `/design` primitives mount; token parity in BOTH themes via
  `getComputedStyle`; BLACK inverted-chip rule; modal focus-trap + Escape;
  toasts (max-3); reduced-motion calming; command-bar focus and the honest
  hollow `band —` chip.
- `tests/screens-verify.mjs` — the per-surface camera: photographs
  `/ops`, `/gods-eye`, `/citizen`, `/driver`, `/comms`, `/chat` under
  SwiftShader software-WebGL, asserts each surface's key elements and a
  non-blank PNG. **40/40 checks green on the live deck, 2026-10-24
  00:21 IST** (live acceptance step 21 on the deployed stack, signed in,
  exit 0) — manifest and PNGs in `tests/artifacts/screens/`
  (`screens_manifest.json`, `screen_{ops,gods-eye,citizen,driver,comms,chat}.png`);
  that run recorded the `/ops` masthead band=GREEN over 12 panels and
  `/driver` in its scripted fallback (`screens.driver.mode: scripted`).
  History, kept: the 2026-10-13 interface-revision docs pass re-executed
  it against the then-live deck (40/40, exit 0), recording the `/ops`
  masthead **band=GREEN, 10 panels, 23 event cards, 2170 PNG colours,
  100% opaque**; the same manifest had recorded **band=RED** earlier that
  day — the masthead tracks the city.
  `/driver` is driven end to end (request → evaluating → proposal → the
  open window's countdown) **only in its labelled scripted fallback**
  (`data-mode="scripted"`: nothing is sent, the approval is scripted); on
  a live deck the camera asserts the idle request card — the request is
  held until a 108 unit number is typed — and never files a request. The
  manifest records which mode it saw (`screens.driver.mode`).
- `tests/citizen-smoke.mjs` — public/comms render gate (Daylight advisory,
  Gujarati flip, driver pipeline with scripted-fallback badge + ticking
  golden-hour counter, chat degradation chip, trilingual same-facts).
- `tests/ops-live-smoke.mjs` — the `/ops` fallback ladder probed against
  a running fleet (oracle, brain and emulator bases as arguments), so client
  assumptions cannot drift from the stack silently.
- `tests/gods-eye-smoke.mjs` — canvas build + non-blank SwiftShader render
  (a `verify_all.sh` tier, §13).

**The God's-eye canvas** (`src/lib/gods-eye/` — CesiumJS 3D city, eleven
registry-validated layers, FLIR sensor modes, share links; Google
Photorealistic 3D Tiles + Cesium World Terrain + Bing aerial imagery for
signed-in operators on an ion-keyed deck, the keyless OSM globe for
everyone else) is the centerpiece of `/`, `/ops` and the fullscreen
`/gods-eye` route: architecture, the measured layer table and the ethics
rails in `docs/GODS_EYE_INTEGRATION.md`; the 3D path in §16.9.

---

### 2.8 Frontend Dashboard — LEGACY, offline fallback

**Status**: retained as the **offline fallback demo** — the scripted
simulation runs with no GPU and no backends, and it ships with its own
headless journey test (`frontend/tests/e2e/citizen-green-corridor.e2e.mjs`,
11 checks). Its tiers were retired from the acceptance ladder in wave 4
(§13). It is **not** the canonical interface; the command-deck (§2.7) is.

**Framework**: React 19 + Vite 8
**Key Dependencies**: react-leaflet 5.0.0, recharts 3.8.0, lucide-react

Components: `VVIPDashboard.jsx` (root), `ConvoyMap.jsx`, `LeftPanel.jsx`
(planning), `RightPanel.jsx` (operations monitor — LIVE, CORRIDOR, INTEL,
PREDICT, COMMS tabs), `AIReasoningPanel.jsx`, `NotificationBell.jsx`
(emergency clearance), `SearchBar.jsx`, `ConvoyContext.jsx`, `useLiveData.js`.

The PREDICT tab renders multi-horizon forecast curves (T+5/10/15/30) with
reasoning display; the citizen advisory and 108 driver pages hang off the
same shared simulation (see `frontend/tests/e2e/` for the headless journey
test — 11 journey checks, runs with no backend; it is no longer a
`verify_all.sh` tier).

> ⚠️ **Known stale content adjacent to the docs (recorded, not hidden):**
> (a) a few frontend status strings still carry the previous stack's name —
> `frontend/src/components/RightPanel.jsx` has 7 Qwen-era mentions — the
> serving stack behind them is the decider/Gemma pair; (b)
> `models/finetune/train_qwen_vvip.py` is the Qwen-era fine-tuning
> pipeline, kept for provenance. Neither is on the serving path.
> (`.github/copilot-instructions.md`, previously listed here, was
> rewritten for wave 4 and carries no Qwen-era text or retired constant.)

---

### 2.9 event-emulator (the emulated half)

**Language**: Python 3.12
**Run**: the compose service `event-emulator` (container `vvip-argus-event-emulator`, image built from this tree) — binds loopback `:8090`; the deck reaches it through its `/api/emulator` proxy
**Key Dependencies** (`services/event-emulator/pyproject.toml`): fastapi ≥0.115, uvicorn, httpx ≥0.27, psycopg[pool] ≥3.2, valkey ≥6.0, nats-py ≥2.9, pyarrow ≥18, pydantic ≥2.10

The corridor-OS generalization's emulated half. It **emulates only causes** —
procession schedules, marathon waves, weather onset, vehicle incidents,
emergency green-corridor requests, convoy departures, and (since the
enforcement family) **emulated ANPR camera reads and patrolling agency
units** — consuming the use-case library verbatim
(`src/event_emulator/use_case_library.json`: 10 entries — the 8 corridor
use cases + the flagship composite + the emulated `watchlist_anpr_sweep`,
every route a legal chain on the real network of §16.1) and driving the
**real stack through its real APIs**. The emulator never produces outcomes
(no decisions, params, situation classes, hits or recommendations): the
real stack plans, classifies, escalates and degrades on its own. This is
the emulated/real split, stated plainly — and it is what makes every
showcase number a measurement of the real decision stack responding to
injected causes. The ANPR extension of the split (frozen `AnprRead` frame
schema, `source='emulated_anpr'` on every row, `live_anpr` reserved for a
real vendor adapter) is written up in `docs/GODS_EYE_INTEGRATION.md` §3;
the full service reference is `services/event-emulator/README.md`.

#### Injection seams

| Seam | Vehicle | Where the real stack consumes it |
|---|---|---|
| Corridor events (C-F) | the event + legacy movement rows (store upsert with a creation ledger row), then `active` at t0 and `completed` at the horizon through `POST /api/v1/events/{id}/status` as `event-emulator (system)`; a failed activation is retried every 5 s of wall time and a run whose event never went live ends `failed`. The movement's route line is its segments' routing lines chained in the direction of travel (oriented by the junctions between consecutive segments and the one-way rule), and its origin / destination are where that line starts and ends — on the road | situation engine loop, the ops board, dispatch auto-close |
| Valkey — inputs docs | `adaptive:inputs:{id}` in the **exact reader shape** (`predicted_congestion_idx / weather_severity / crowd_score / anomaly_active / event_active` + optional weather code + `last_gps_age_sec / ambulance_delay_projection_sec`), with the provenance sidecar `emulator:source:{id}` the engine loop checks to label the inputs `source='emulator'` | situation classify → hysteresis → `situation:changed` |
| ANPR reads (C-E) | `POST /api/v1/vehicles/sightings` — the ORACLE computes the hit and the e-challan rules; a tape driving a FIR-named stolen plate first ensures its premise, a `stolen_vehicle_fir` earmark labelled `event-emulator (system) — scenario precondition` / EMULATED, so its reads become evidence-only `R_STOLEN_SIGHTING` (never a fine) | watchlist, enforcement queue, interception plan |
| Agency units (C-A) | NATS `corridor.units.emulated` → Valkey `units:emulated:{unit_id}` (EX 30); a unit an officer has dispatched is left to the oracle's dispatch projection | `GET /api/v1/units`, the canvas |
| Simulation twin | one `POST /api/v1/simulate/twin` per run carrying the config verbatim + the additive cause fields | seeded Monte-Carlo twin (the answer is recorded, never acted on) |
| NATS — showcase plane | synthetic Arrow IPC batches on `corridor.traffic.emulated` (**never** `.live`); every row is labelled `data_quality` 2 SYNTHETIC — 1 ANOMALOUS only as the congestion-outlier marker (congestion > 0.85), **never 0 REAL** — and sits ON its segment, at the midpoint along the line (`ST_LineInterpolatePoint(geom, 0.5)`, the anchor signal-ingress uses) | the oracle caches them in their own key space `traffic:emulated:latest:{segment_id}` (§9): only EMULATED events' bands read them |

#### API (loopback :8090, contract C-G)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Real probes of store / Valkey / NATS / oracle (`degraded` with `degraded_reasons` when a seam is down or the last run failed) + the living city |
| `GET` | `/api/v1/emulator/use-cases` | The use-case library (verbatim; 10 entries) |
| `POST` | `/api/v1/emulator/runs` | **202** `{run_id, status: 'running', use_case_id}` right after the ledger insert (the run continues in the background); **409** `{detail, run_id}` while that use case runs; `tick_sec` 1–60 (default 5) |
| `GET` | `/api/v1/emulator/runs` · `/runs/{id}` | Runs ledger; a run's `status` (running \| completed \| stopped \| interrupted \| failed), `progress {frame, total, delivered, failed}`, `event_id`, `movement_id`, `event_status`; `?include_tape=false` is the light progress poll |
| `POST` | `/api/v1/emulator/runs/{id}/stop` · `/replay` | Stop between frames (its event closed through C-F) / replay the stored tape verbatim (202; the movement's origin and destination re-drawn at the ends of its route on today's roads). A tape whose recorded route is not a legal path on the CURRENT road network (`corridor.road_graph`) — a run recorded before the WP-20 conversion — is refused before anything is written: **422** `{detail, reason: 'route_not_on_current_network', run_id, use_case_id, route_segment_ids, problems}` (start a new run of the use case instead); **503** when the route cannot be checked; 409 keeps its one meaning, "that use case is running". `python -m event_emulator.cli replay` refuses the same tape with exit 2 |
| `GET`/`POST` | `/api/v1/emulator/living-city` | The ambient living city — **off by default** (`EMULATOR_LIVING_CITY` unset/0; an operator can start it from the deck) |

#### Determinism & provenance (contracts C3/C4)

- The cause tape is a pure function of `(spec, seed, t0)` — blake2b-derived
  seed + uniform-stream RNG; two runs with the same inputs are
  byte-identical (verified across processes by sha256).
- **Run-scoped identity**: a ledgered run's movement / corridor-event id is
  `uuid5(use_case | seed | run:{run_id})` (`tape.movement_uuid`) — a fresh
  subject per run, so repeated showcase rounds never collide; camera reads
  are `uuid5` over (use case, seed, camera, plate, run). A replay re-posts
  the STORED tape, original ids included, so it upserts instead of forking.
  Runs ledger in `emulator.emulation_runs` (migration 009, plus the
  `interrupted` status of migration **016**); `simulation.runs` (006) is
  never written by the emulator.
- Live injections **ride the same APIs as a real feed** — no emulator
  header on them; the provenance rides in the rows themselves (and the
  showcase plane on its own subject, every row labelled SYNTHETIC).
  Replays carry one more tell: `X-Emulation-Replay: <run_id>` on HTTP
  requests and NATS message headers.
  Every corridor event the emulator opens carries metadata source
  `event-emulator`, so every surface — the citizen feed included — labels
  it EMULATED; every generated plate carries the EM series marker.
- **On the road** (`store.py`, `runner.py`): every position the emulator
  attributes or anchors lies ON its road — a segment's anchor is the point
  halfway along its routing line (`ST_LineInterpolatePoint(geom, 0.5)`,
  never `ST_Centroid`, which on a curved OpenStreetMap road can sit a
  kilometre off it); patrol paths follow the drawn line; a movement starts
  and ends at its route's ends. The cached anchors and paths are keyed by a
  fingerprint of the network re-probed every 60 s, so a network converted
  under a running emulator is re-read within a minute (a restart reads it
  afresh). Verified on a fresh install of this tree: 855 wire rows ≤ 0.006 m
  from their segment, patrol units ≤ 0.066 m, sightings ≤ 0.055 m, movement
  ends 0.0 m from their route's ends, and no row labelled REAL.
- The cause vocabulary is closed (contract C4): `event_row`, `inputs_doc`,
  `scheduled_event` (six EventTypes — the five existing + additive
  `emergency_vehicle`), `event_config` (five config multipliers +
  `ambulance_rate_multiplier`), `weather_ramp`, `crowd_script` — plus the
  two watchlist additions `anpr_read` and `agency_unit`.
  `emergency_request` is deliberately **not** a kind: flood relief is
  priority sub-event rows + scheduled `emergency_vehicle` +
  `ambulance_delay_projection_sec` inputs (the green-corridor channel).
- **The living city** (`ambient.py`): a deterministic day program
  f(`EMULATOR_CITY_SEED` default 1411, IST date) — 2–3 overlapping
  emulated causes, two Jan Rakshak beats, registered cameras reading
  passing traffic, a 108 green-corridor request about every 25 minutes
  that only a named duty officer can approve (re-filed onto an open slot
  for up to 20 minutes if its slot closes first, so the queue never loses
  it), and — unless `EMULATOR_LIVING_VIOLATORS=0` — rare deliberate
  violators whose reads become PENDING recommendations for a human.
  `scripts/veer_up.sh --demo` refuses to switch it on unless the deck's
  public feed answers `emulated_labelling: true`.
- Outbound HTTP/SQL safety mirrors the platform rules (§14): scheme
  http/https only; URLs from external input are host-validated
  (`safe_http.py`); the oracle URL is a loopback/`host.docker.internal`
  config constant; SQL is %-bound.

**Tests**: `services/event-emulator/tests/` — **328** tests collected on
this tree by the gate venv (the pytest tier of `verify_all.sh`; the
PostgreSQL-backed ones skip unless `EMULATOR_TEST_PG_DSN` names a scratch
clone).

---

## 3. The Intelligence Stack (5 tiers, decider on top)

```
┌────────────────────────────────────────────────────────────────────┐
│ T5 STRATEGIC — pplx-decider-v1-27b (model-host :8083)              │
│   Typed choice/noul/score questions. Owns strategy + TOOL SELECTION│
│   Event-driven cadence · ≥5 s spacing · p95>3 s → tactical fallback │
├────────────────────────────────────────────────────────────────────┤
│ T4 TACTICAL — aditi-ps18-v7h Gemma 4 12B (llama-server :8084)      │
│   4 agents · tool-argument drafting vs inputSchema (1 repair) ·    │
│   dossiers · trilingual advisories (EN/HI/GU) · /chat narration    │
├────────────────────────────────────────────────────────────────────┤
│ T3 NUMERIC — forecast ladder (t0-alpha · DSTGAT) · ETA · OR-Tools  │
│   The deterministic tool surface the decider chooses among         │
├────────────────────────────────────────────────────────────────────┤
│ T2 ADAPTIVE — SituationEngine + hysteresis/escalation/shadow-eval  │
│   GREEN/AMBER/RED/BLACK → protocol-floored params (fail-closed)    │
│   publishes situation:changed (decider cadence trigger #3)         │
├────────────────────────────────────────────────────────────────────┤
│ T1 EMBEDDING SPINE — pplx-embed-v2-late-0.6b + bge-m3 (:8085)      │
│   "Similar past situations" memory over incidents + scenario runs  │
└────────────────────────────────────────────────────────────────────┘
```

*T3 honesty note: our DSTGAT forecaster ships as architecture only — no
trained DSTGAT weights are bundled. The forecasts served today come from
the ladder's foundation rung, t0-alpha (a third-party zero-shot ONNX INT8
model the oracle image bakes in, §6.1b), with the heuristic cache as the
last rung; the HistGBT ETA model is untrained on the live path (its
endpoint answers `model: fallback`) and the Isolation Forest is not wired
in (§6.1–§6.3).*

### Decision flow per turn (`orchestrator.py`)

1. **STRATEGIC** — the decider picks the next **tool** via a typed `choice`
   question over the tool menu (or `final_answer`), plus a confidence
   `score`. State documents are budgeted: ≤1500 tokens pre-movement,
   ≤400 per event, hard cap 8192 (the model rejects larger with 413).
2. **TACTICAL** — Gemma drafts the tool **arguments** against the chosen
   tool's `inputSchema`; on validation failure exactly one repair round
   names the missing fields; then Gemma composes the final structured
   answer.
3. **FLOOR** — deterministic heuristics (keyword tool selection, default
   arguments, degraded final answer).

**Fallback chain: decider → Gemma → heuristics.** Whenever a tier is slow or
unavailable the turn falls through, and the drop is audit-logged into the
movement's `decisions_log` together with the decider's
`{value, probability, confidence}` distribution when it ran.

### Event-driven decider cadence (red-team F8 correction)

The decider is **never called per GPS tick**. Checkpoints fire only on
(`decider.py` `CHECKPOINT_TRIGGERS`; the pre-movement planning checkpoint
is the plan phase's own trigger, `pre_movement`):

1. `incident_detected`
2. congestion-threshold crossing (adaptive per movement; fail-closed 0.8)
3. `situation:changed` — the adaptive tier's class transition (Valkey key)
4. phase transitions (plan / escort / clear)
5. operator chat

with `DECIDER_MIN_CHECKPOINT_INTERVAL_SEC = 5` between checkpoints per
movement (phase transitions and pre-movement planning may `force` past the
rate limit — they are single-shot events that cannot queue-hop). The
live-escort GPS loop itself is LLM-free (`workflows/live_escort.py` — the
per-tick path is numeric-only diversion bookkeeping; tactical agents fire
only on incidents).

### Decider novelty latch — confident-but-novel abstention (Wave-1, 2026-10-17)

A checkpoint that is BOTH novel and confident is withheld, not acted on.
`DeciderBrain` (`services/convoy-brain/src/convoy_brain/decider.py`)
first queries the memory tier for the best comparable (`MemoryClient` →
embedding-service search, best `MaxSim` row, bounded by
`NOVELTY_SEARCH_TIMEOUT_SEC`): a covered situation proceeds, while a
confident answer with no comparable above `NOVELTY_SIMILARITY_FLOOR`
returns tier `novelty_latched` with an audit row and an operator alert.
Honesty rules pinned by `tests/test_novelty_latch.py`: an unreachable
memory disables the latch without benching the strategic tier;
unconfigured memory is silent; degraded/rate-limited short-circuits never
call memory; cadence still applies to latched checkpoints; the search
memo (bounded by `NOVELTY_MEMO_TTL_SEC`) suppresses chained searches and
never memorizes failures; and the orchestrator falls through a latched
checkpoint to the tactical tier — a `novelty_latched` tier is never
trusted as an answer.

### Decider tier-0 cache — routine checkpoints with zero 27B calls (Wave-2, 2026-10-18)

Built on the same memory machinery, a routine checkpoint — eligible
triggers only (`TIER0_ELIGIBLE_TRIGGERS`, `config.py`: congestion
threshold, incident, situation change, phase transition, pre-movement;
operator chat never qualifies) — whose best comparable clears the strictly tighter
`TIER0_ROUTINE_SIMILARITY_FLOOR = 0.75` (`config.py`, maxsim scale,
uncalibrated) is answered from the tier-0 cache with ZERO additional
`/decide` calls (`tests/test_tier0_gate.py` asserts the scripted host's
`decide_calls` stays at the priming count). Entries carry
`TIER0_CACHE_TTL_SEC = 300` freshness and a 128-entry FIFO bound
(`TIER0_CACHE_MAX_ENTRIES`, `config.py`); cross-movement isolation, novelty,
below-routine similarity and TTL expiry (injected clock, no wall-clock
dependence) all force a miss. Memory outage or all-`dense_fallback`
results fail OPEN — the decider is called, never degraded, with an
honest audit row. `DECIDER_TIER0_DISABLE=1` (`config.py` `tier0_enabled()`) is the kill switch: it preserves pre-Wave behavior
exactly and flips the `tier0_cache` field `/health` reports
(`__main__.py`, mirrored by the `TIER0_ENABLED` gauge).

**The pre-movement route checkpoint keys on content, never on request
ids (the live fix, 2026-10-24).** The cache key carries the checkpoint's
canonical question set, and the pre-movement `route_strategy` question
used to offer the oracle's candidate route ids as its options — ids the
oracle mints per request (`{source}_{uuid4().hex[:8]}`), so identical
plans never shared a key and the live tier-0 gate found **no hit on three
identical plans** (live acceptance step 8, 2026-10-23 23:49 IST, §13).
Each candidate is now offered under a **label derived from its own path**
(`workflows/pre_movement.py` `route_option_label` / `_route_options`):
`route_` + the first 12 hex digits of a SHA-256 over its ORDERED segment
sequence (ids normalised to integers, the reverse order another route) —
never the route id, its source, rank, ETA or scores. A candidate whose
path repeats a higher-ranked one is folded into that option (never
offered twice); two different paths whose short digests collide each take
a longer prefix of their own digest; tactical rank order is kept, at most
`MAX_ROUTE_OPTIONS − 1` routes with `delay_departure` last. The answered
label maps back to its candidate, so `decider_pick.route_id` is that
path's id IN THE CURRENT REQUEST (`choice` keeps the label), and the
`decider_checkpoint` audit row gains `route_labels` {label → oracle route
id}. A cached answer can only be served to the identical labels in the
identical order — so it always selects the same physical route; a
re-ranked or trimmed candidate set misses and the 27B decides again.
The decision-memory doc of a pre-movement checkpoint is likewise one per
movement and candidate set, no longer one per request.

**Own-doc filing for pre-movement.** The failed gate's own plans (read
back GET/SELECT-only) showed why labels alone would not do: the tactical
tier trims the tail alternates in some plans and not others (3 / 5 / 3
candidates for three identical bodies), and plan 1's answer was filed
under the best memory doc seen BEFORE the feeder upserted the movement's
own doc — which no repeat matches again (an offline replay of that
3 / 5 / 3 sequence: labels alone 0 hits in 3; with the filing, plan 3
hits).
So for `TIER0_OWN_DOC_TRIGGERS = {"pre_movement"}` (`config.py`) a stored
answer is ALSO filed under the movement's own decision docs, and that
filing is read only when the best memory row is `kind=decision` with a
`ref_id` of exactly `decision:{movement}:pre_movement:` + 16 hex digits
(`decider.py` `_is_own_decision_ref`; `MemorySearchResult.best_ref_id` is
parsed from the `ref_id` `/search` already returns). Movement scoping, the
exact question set, the routine floor, TTL and FIFO are unchanged; the
escort triggers ask fixed question sets and keep their filing; the hit
log names `filed_under`. Offline: the convoy-brain suite went 584 → **634
passed / 1 skipped** (`tests/test_pre_movement_route_labels.py`,
`test_tier0_gate.py`, mutation-checked), `check_tier0_gate.sh` PASS.
**Live: redeployed at 01:20 IST and re-run green** —
`check_tier0_live.sh` (a) 3 identical plans: plan 2 a **hit** (1 hit in
3, hit counter +1, `latency_saved_ms` **+1825**); (b) a different-POI plan
on a fresh movement: `miss_no_entry` (best 0.8239) — no stale serving;
wiring: the feeder's `kind=decision` docs recalled by `:8085/search`,
`memory_record_events_total{recorded}` 0 → 3; (d) the drift panel 4/4
green.

### Auditable reasoning

Every decider answer `{value, probability, confidence, distribution}` and
every fallback transition is appended to
`ConvoyContext.decisions_log` (`services/convoy-brain/src/convoy_brain/memory/convoy_context.py`,
appended via `append_decision`) — the audit trail after-action
reports read. Confidence below **0.45** (`CONFIDENCE_FLOOR`, env
`VVIP_CONFIDENCE_FLOOR`; calibrated 2026-10-19 against the live 27B, whose
pre-movement checkpoint confidences measured 0.50 and 0.47 — the earlier
0.6 floor labelled every live checkpoint low-confidence, `config.py` `CONFIDENCE_FLOOR`)
triggers a Gemma draft + operator alert instead of direct action. During a
live escort no answer acts on the route at all: it becomes an officer's
proposal (§16.2).

### Measured (bench re-executed by this documentation pass, 2026-10-13, IST — `scripts/bench_results.json`; full run history kept)

*Timezone note: dates labelled IST in this document are local (UTC+5:30);
artifact timestamps quoted alongside are UTC — the same instants.*

*Bench context (2026-10-13, IST): the bench ran against the original
`vvip` compose fleet of that day — since retired (2026-10-19) in favour of
the argus stack this document describes — with every service and model
host answering HTTP 200, migrations through `011_city_coordination` and
the schematic seed network of the time. The model hosts measured below are
the same ones the argus stack uses today; the bench has not been re-run
since.*

| Metric | Interface-revision run (current, 2026-10-13T13:24Z) | Docs-pass run (2026-10-13T04:33Z) | 2026-10-13T04:09Z run (doc-only†) | Post-fix round 2 (2026-10-12) | Round 1 (2026-10-11) |
|---|---|---|---|---|---|
| Decider throughput | **455.78 tok/s** | 546.42 tok/s | 548.59 tok/s | 492.77 tok/s | 496.63 tok/s (12 decide calls) |
| Decider latency p99 | **1106.8 ms** | 866.0 ms | 861.3 ms | 968.2 ms | 958.8 ms |
| Gemma throughput | **20.09 tok/s** | 23.29 tok/s | 24.04 tok/s | 21.36 tok/s | 21.2 tok/s (3 calls) |
| Embedding round-trip | **92.7 ms** | 85.9 ms | 82.4 ms | 84.4 ms | 92.1 ms (9 calls) |
| Stack resident memory | **62.73 GiB** of the 121.6 GiB pool | 62.73 GiB | 62.73 GiB | 62.66 GiB | 62.61 GiB |
| Adversarial scenarios | **19/19 (score 100)** — avg 29.19 s / max 57.38 s | 19/19 — avg 25.98 s / max 47.6 s | 19/19 — avg 25.73 s / max 69.99 s | 14/14 — avg 29.27 s / max 52.03 s | 0/14 (9× HTTP 504; 5 evaluator crashes) |

The suite **grew 14 → 19 scenarios** with the enforcement family (five
watchlist scenarios in `tests/adversarial/config.py`; two stress scenarios
are defined for the stress mode and are not part of the headless standard
run). Latency honesty, kept: the 60 s line is a **scored dimension, not a
hard gate** — the 04:09Z run passed 19/19 with one scenario at 69.99 s
(recorded here because it is true); the interface-revision run kept every
scenario under it (max 57.38 s) at a slower tok/s and higher p99 than the
same-day morning runs — reported exactly as measured, no number rounded
in this repo's favour. *Artifact-honesty note: the 04:09Z run's report was
overwritten by later re-runs (`bench_stack.sh` writes a fixed filename),
so the 04:09Z column above and the 69.99 s figure are **doc-only
history** — read from the artifact at the time, no longer re-derivable
from disk. The surviving artifacts (`bench_results.json`,
`bench_adversarial_report.json`, 2026-10-13T13:24:26Z) are the
interface-revision run's.* *(The `bench_results.json` `"round"` label is
the script's hard-coded counter, not a run count.)* No evaluation
threshold was touched by any fix work — the round-1 → round-2 repair
landed at the source:

1. *Grammar + budgets:* `_draft_arguments` sends a `json_schema` envelope
   embedding each tool's `inputSchema` (llama-server compiles to GBNF and
   stops at the closing brace — the 110.6 s cap-hitting ramble is
   structurally impossible, `orchestrator.py` `_tool_args_schema`); the
   Gemma tool selector is enum-constrained (`_tool_selection_schema`); per-call token
   budgets 48/192/384 at the time (the final budget is 512 today, §2.3);
   per-call HTTP wall 120 s → 30 s (since stretched to cover each call's
   own budget, §2.3).
2. *Turn budget between calls:* `TOTAL_TURN_TIMEOUT_SEC` (120 s, unchanged)
   drives a deadline enforced via `asyncio.wait_for` on every LLM call
   with shrinking per-tool timeouts (`orchestrator.py` `_deadline_timeout`).
3. *Decider latch un-stuck:* `refresh_health` runs one minimal probe
   `/decide` that must beat the 3000 ms line before any p95-based
   degrade/stay-degraded decision (`decider.py` `refresh_health`;
   `DECIDER_P95_DEGRADE_MS` untouched).
4. *traffic-oracle brought up* via the GB10 host-network compose
   override (`infra/compose.gb10-override.yml`; the repo-tree-mirroring
   image fixes the flat-`/app` import crash), convoy-brain rebuilt.
5. *Bench client crash fixed:* `/chat`'s dict `response` coerced to `str`
   in `runner.py:191-195` and `evaluator.py:56-60` (the five round-1
   `slice(None, 2000, None)` exceptions).

**Adversarial suite, current (2026-10-13T13:24:26Z):** 19/19 passed, 100%
pass rate, avg **29.19 s** / max **57.38 s** per scenario, zero 504s
(`scripts/bench_adversarial_report.json`).

**Open quality gaps (pass the gate, tracked, not hidden):**
tool_validity **0.684** in the docs-pass run (0.643 in the prior; the
decider often final-answers after one tool where a scenario expects two —
e.g. only `find_convoy_routes` called where `predict_traffic_flow` was also
expected) and keyword_coverage **0.561** (0.579 and 0.595 in the two prior
runs — it moves run to run); reasoning_depth is 0.30–0.41 across runs.

**Gates — green for this revision (the interface revision's gate sessions
drove all three green after one fix round; the bench artifacts on disk are
the 2026-10-13T13:24Z run, and the showcase artifacts on disk are now the
live run of 2026-10-23T18:31Z (00:01 IST on 2026-10-24, on the
deployed stack — `showcase_results.json` + `pursuit_chain.json`, §13);
`verify_all.sh` was not
re-executed by the docs pass itself; the React tiers named here were
retired from the ladder in wave 4):**
`scripts/verify_all.sh` → **exit 0, ALL TIERS PASSED** — frontend bundle +
headless e2e journey + the two gods-eye tiers (svelte-check; SwiftShader
canvas smoke) + every pure-python pytest suite as collected on THAT tree
(convoy-brain 262, corridor-store importer 22 + store 12,
embedding-service 113, event-emulator 94, traffic-oracle 633, plus the
command-deck's own 2 proxy-guard tests — the wave-4 tree collects far
more: §13) + live models smoke; `scripts/showcase_run.sh` → **exit 0, SHOWCASE PASS**
("God's Eye Morning", §13); `scripts/bench_stack.sh` → **exit 0, BENCH OK**
(the run quoted above). The interface camera
`services/command-deck/tests/screens-verify.mjs` was additionally
**re-executed by the docs pass against the then-live deck container:
40/40 checks, exit 0** (§2.7). `scripts/fleet_smoke.sh` was not re-executed
by this pass (last recorded run exit 0, ALL PASS; the fleet it probes was
health-checked end-to-end by the showcase phase-1 gate and the screens
camera). *Provenance:* the verify/fleet/bench scripts had exited 127
(command-not-found class) in the 2026-10-12 repair session, cause never
diagnosed — kept here as history, not hidden.

---

## 4. API Reference

### convoy-brain Endpoints (Port 8092 on the argus stack; reached by the deck as `/api/convoy/*`)

#### `GET /health`

```json
{
  "status": "ok | degraded",
  "strategic_tier": {
    "model": "pplx-decider-v1-27b",
    "model_host": "connected | unreachable",
    "degraded": false,
    "degraded_reason": null,
    "p95_decide_latency_ms": 718.0
  },
  "tactical_tier": {
    "model": "aditi-ps18-v7h-gemma4-12b",
    "llama_server": "connected | unreachable"
  }
}
```

#### `GET /health/gpu`

Unified-memory status for the GB10 pool (`/proc/meminfo` is the source of
truth — nvidia-smi reports `memory.total` as N/A on GB10): totals, free
memory, and per-tier allocations (decider RSS from `/decider/health`, llama
9.5 GiB budget, embedding 5.0 GiB budget, oracle 3.5 GiB budget).

#### `GET /health/services`

Reachability of every fixed platform endpoint.

#### `POST /movements` · `POST /movements/{id}/plan`

Pre-movement planning workflow. The decider takes its strategic checkpoint
(route strategy `choice`, urgency `noul`, disruption tolerance `score`)
against a ≤1500-token state document; tactical tiers validate and draft.
Returns scored routes, security validation, diversion directives, and
approval status (schema as v1: `corridor_status`, `primary_route`,
`alternate_routes`, `security_*`, `diversion_directives`,
`scenario_comparison`, `status`, `confidence`).

#### `POST /movements/{id}/escort` · `GET /movements/{id}/escort` · `POST /movements/{id}/stop` · `POST /movements/{id}/position`

Live escort (contract C-J). `POST …/escort {officer?}` → **202**
`{movement_id, status: "running", task_id}` (409 while one runs) — the
escort runs as a background task; `GET …/escort` is the status document
`{movement_id, event_id, status: idle|waiting_for_gps|running|completed|stopped|failed,
iteration, position, progress, eta_sec, active_diversions[], last_decision,
pending_approval, approval_outcomes[], approval_timeout_sec, started_at,
updated_at, detail}`; `POST …/stop` is idempotent; `POST …/position
{lon, lat, speed_kmh?, heading?, t?, source?}` → 202 (422 outside the city
bbox, 409 once the movement is closed), mirrored to
`convoy:position:{movement_id}` (C-B). The GPS loop is LLM-free; decider
checkpoints are event-driven (§3) and never change the plan on their own
(§16.2).

#### `POST /movements/{id}/escort/approvals/{approval_id}` · `GET /escorts`

The officer's answer to the escort's ONE open proposal
`{decision: approve|reject, officer?}` — the deck's verified
`x-veer-officer` is the deciding officer, a differing body name is kept as
`claimed_officer`: **200** decided `{movement_id, approval_id, status,
action, kind, summary, decided_by, decided_at}` · **404** unknown movement
or `approval_id` · **409** expired / superseded / already decided (the body
names who and why) · **422** a malformed body or no officer at all.
`POST /movements/{id}/escort/approve` takes the same answer with
`approval_id` in the body (older decks). `GET /escorts?recent_sec=` lists
running (and just-ended) escorts with their open proposal and recent
outcomes — one read for every console's escort lane — and publishes
`"authority": "officer_approves_every_switch"`. Full contract: §16.2.

#### `POST /movements/{id}/clear`

Post-clearance recovery workflow: segment-by-segment deactivation, recovery
polling, an after-action report (escort duration, segments affected and
recovered, recovery time, diversions lifted, and the counts of alerts and
audited decisions — the decisions themselves stay in `decisions_log`).

#### `POST /movements/{id}/dossier` · `POST /movements/{id}/threat-assessment`

Security dossier and threat assessment — composed by the **Gemma tactical
tier** as structured JSON sections.

#### `GET/POST /movements/{id}/protocol` · `POST /movements/{id}/protocol/assess`

Blue Book protocol state (ASL checklist, protocol compliance rules,
anti-sabotage, transit status, plan B) — persisted with the movement context.

#### `POST /analytics/reasoning` · `POST /recommendations/reasoning`

Reasoning-backed analytics and recommendation endpoints for the PREDICT tab.

#### `POST /chat` and `POST /chat/stream` (NDJSON, contracts C-D / C-D2)

Single-turn and streaming chat with the two-tier orchestrator — the same
turn, decider + argument validation + `decisions_log` audit, for both.
Stream event types: `thought`, `tool_call`, `tool_result`, `directive`,
`token` (prose only, never raw JSON), `done` (`session_id`, `tiers_used`,
`operator_alerts`, `degraded`, `citations`), `error`; `event_id` /
`context_urn` on the request ground the turn in one corridor event or city
entity. Sessions continue for 15 minutes.

#### `POST /chat/directives/ack` · `POST /mcp`

A canvas **directive** (fly to, track, highlight, annotate, sensor, layer —
the six `canvas_*` verbs) is a proposal the operator applies or dismisses
on the canvas; the ack records that human answer (the verified officer)
in the bound movement's `decisions_log` — 404 unknown/expired directive,
409 already acknowledged, 422 another session's directive. `POST /mcp`
is MCP over JSON-RPC 2.0 (initialize, tools/list|call, resources/list|read,
prompts/list|get): **28 tools / 4 resources / 3 prompts** on this tree
(§7) — read-only city tools, compute tools and the canvas verbs; nothing
reachable there approves, decides, dispatches or earmarks (R2).

---

## 5. Database Schema

### Schema: `corridor`

#### `corridor.road_segments`

| Column | Type | Notes |
|---|---|---|
| `segment_id` | BIGINT PK | |
| `osm_way_id` | BIGINT | Nullable |
| `road_name` | TEXT | GIN trigram index for fuzzy search |
| `road_class` | TEXT NOT NULL | CHECK: motorway, trunk, primary, secondary, tertiary, residential, service |
| `lanes` | SMALLINT DEFAULT 2 | |
| `speed_limit_kmh` | SMALLINT DEFAULT 50 | |
| `oneway` | BOOLEAN DEFAULT FALSE | |
| `geom` | GEOMETRY(LineString, 4326) | GIST indexed — the ROUTING line every planner routes, costs and attributes on; on this revision the path along the OpenStreetMap way(s) between the segment's two junction nodes |
| `display_geom` / `display_geom_source` / `display_geom_meta` | GEOMETRY / TEXT / JSONB | migration 015 (C-K): a draw-only OSM trace for a segment whose routing line is hand-placed; NULL on all 49 segments today (their routing line IS the road) |
| `geom_source` / `geom_meta` | TEXT / JSONB | migration 020: `osm` (all 49 today) \| `schematic`; NULL = not recorded, treated as schematic. `geom_meta` = OSM way ids in path order, the `osm_base` stamp, the OSM names along it, the validation metrics |

#### `corridor.junctions`

| Column | Type | Notes |
|---|---|---|
| `junction_id` | BIGSERIAL PK | |
| `junction_type` | TEXT DEFAULT 'intersection' | CHECK: intersection, roundabout, flyover, underpass, toll_plaza, signal |
| `signal_control` | BOOLEAN DEFAULT FALSE | 11 of the 34 junctions are signal-controlled (11 programs in `corridor.signal_programs`) |
| `geom` | GEOMETRY(Point, 4326) | GIST indexed — on the junction's OSM node |
| `name` / `name_gu` | TEXT | migration 012: EN + Gujarati labels |
| `osm_node_id` | BIGINT | migration 020: the OpenStreetMap node (34/34 set) |

#### `corridor.segment_adjacency`

| Column | Type | Notes |
|---|---|---|
| `from_segment_id` | BIGINT FK | |
| `to_segment_id` | BIGINT FK | |
| `via_junction_id` | BIGINT FK | |
| `turn_cost_sec` | REAL DEFAULT 0 | |

PK: (`from_segment_id`, `to_segment_id`, `via_junction_id`)

#### `corridor.road_graph` (VIEW)

pgRouting-compatible view computing edge costs:
```sql
cost = turn_cost_sec + ST_Length(geom::geography) / (speed_limit_kmh * 1000/3600)
reverse_cost = CASE WHEN oneway THEN -1.0 ELSE cost END
```

### Schema: `traffic`

#### `traffic.observations` (RANGE-partitioned by `timestamp_utc`)

| Column | Type | Notes |
|---|---|---|
| `observation_id` | BIGSERIAL | |
| `segment_id` | BIGINT NOT NULL | |
| `timestamp_utc` | TIMESTAMPTZ NOT NULL | |
| `speed_kmh` | REAL NOT NULL | |
| `congestion_idx` | REAL NOT NULL | CHECK: 0.0 to 1.0 |
| `source` | TEXT NOT NULL | CHECK: government, mapping_api, fleet_gps, crowdsource |
| `geom` | GEOMETRY(Point, 4326) | |
| `data_quality` | TEXT DEFAULT 'real' | CHECK: real, anomalous, synthetic |
| `confidence` | REAL DEFAULT 1.0 | CHECK: 0.0 to 1.0 |

#### `traffic.hourly_aggregates`

PK (`segment_id`, `hour_utc`); columns: `avg_speed_kmh`, `p50_speed_kmh`,
`p95_congestion`, `observation_cnt`.

#### `traffic.segment_embeddings`

| Column | Type | Notes |
|---|---|---|
| `segment_id` | BIGINT FK | |
| `pattern_type` | TEXT NOT NULL | CHECK: daily_profile, weekly_profile, event_response, incident_response |
| `embedding` | vector(24) | HNSW index (m=16, ef_construction=64, vector_l2_ops) |
| `computed_at` | TIMESTAMPTZ | |

#### `traffic.anomaly_log`

`anomaly_id` PK, `segment_id`, `timestamp_utc`, `anomaly_type` (CHECK:
future_timestamp, stale_data, speed_jump, phantom_reading, speed_zscore,
congestion_outlier, road_class_mismatch, spatial_mismatch,
isolation_forest), `severity` (low/medium/high), `details` JSONB.

#### `traffic.synthetic_observations`

Synthetic gap-filling rows with `generation_method` (historical_match,
spatial_interpolation, temporal_pattern, default_fallback), `confidence`
(0–1), `source_segments` BIGINT[].

#### T1 memory tables (migration `004_memory_embeddings.sql`)

`traffic.memory_docs`, `traffic.incidents`, and
`traffic.memory_segment_embeddings` — the embedding spine's storage (dense
1024-d vectors + multivector payloads).

### Schema: `convoy`

- `convoy.movements` — `movement_id` UUID PK, `vvip_class` (Z+/Z/Y/X CHECK),
  `status` (planning/approved/active/completed/cancelled CHECK), origin/
  destination geometry, planned/actual timestamps.
- `convoy.planned_routes` — ranked routes with distance, time,
  `disruption_score`, geometry, ordered `segment_ids`.
- `convoy.diversions` — per-segment directives (`diversion_type` CHECK:
  full_closure, partial_closure, speed_restriction, signal_override),
  activate/deactivate timestamps, alternate geometry.

### Schema: `events` + `emulator` — the event-agnostic generalization (migrations 007–009)

The corridor-OS data model. A corridor event is *a planned operation that
takes control of part of the road network for a window* — the
generalization of `convoy.movements` covering the corridor use-case
library (`docs/USE_CASES.md`; migration 010 later widens the kind CHECK to
the enforcement family's `crime_incident`/`vehicle_pursuit`). Five
orthogonal axes; **the event kind selects closure-policy/dossier
templates, never code paths**:

- `events.corridor_events` — `event_kind` CHECK over the eight archetypes
  (`protected_movement`, `procession`, `mass_gathering`, `race`,
  `ceremonial`, `egress_surge`, `emergency_evacuation`, `work_zone`) ·
  `protection_class` CHECK (`Z+`/`Z`/`Y`/`X`/**`none`** — none = public
  event, no protectee; the migration is additive and backfills from
  `vvip_class`, never rewrites it) · `crowd_class` (`LOW`…`EXTREME`,
  first-class) · footprint (`event_routes` spines as segment-id arrays +
  optional `zone_geom` polygon) · phases (`event_phases`: label, window,
  closure_policy tuples) · soft `movement_id` link back to the legacy
  convoy row.
- `events.event_profiles` (migration 008) — the eight canonical use-case
  profiles seeded from the scenario library as **templates** (archetype +
  representative route spine, departure, closure policy, simulation knobs),
  never live event rows; idempotent `ON CONFLICT DO NOTHING` so operator
  edits survive re-seeding.
- `emulator.emulation_runs` (migration 009) — the event-emulator's runs
  ledger: `spec_hash`, `spec`, `seed`, `t0_anchor`, `clock_scale`,
  `status`, `tape` JSONB. `simulation.runs` (006) is never written by the
  emulator.
- Adjacent additive changes (007): `corridor.protocol_params` (auditable DB
  mirror of `shared/schemas/protocol_params.json`, which remains the single
  human-edited source); the `adaptive` audit tables gain `protection_class`
  (005 CHECKs relaxed idempotently and backfilled); `traffic.incidents`
  gains `event_id` + event-domain incident kinds alongside `vvip_hold`;
  `traffic.memory_docs` gains `event_id`.

### Schema: `corridor` watchlist + city coordination (migrations 010–011)

The enforcement family and the multi-agency layer, additive-only:

- **`010_vehicles_watchlist.sql`** — `corridor.vehicles` (plate-keyed,
  vehicle classes only), `corridor.vehicle_earmarks` (`state`
  watch|flagged, `lawful_basis` **NOT NULL** with an enumerated CHECK —
  MV Act 1988 s.177/s.183/s.207, BNSS 2023 s.94, `stolen_vehicle_fir`;
  `retention_days` capped at **90**, `retention_until` strictly after
  `earmarked_at`), `corridor.vehicle_sightings` (`source` CHECK:
  `emulated_anpr` | `live_anpr` — nothing writes the latter in this
  build), and `events.enforcement_recommendations` (rule id, evidence
  JSONB, `status` pending|approved|rejected, append-only `audit_tail`
  JSONB). Also relaxes the event-kind CHECKs (C3 lockstep) to add
  `crime_incident` + `vehicle_pursuit`.
- **`011_city_coordination.sql`** — `corridor.dispatches` +
  `corridor.dispatch_events` (the agency dispatch channel: request → human
  approve/defer → en_route → on_scene → completed, every transition one
  append-only audit row; agency CHECK over the closed registry — Jan
  Rakshak, police, traffic, 108 ambulance, fire) and
  `corridor.signal_program_history` (the C7 snapshot/restore ledger for
  the city signal-plan writer). No person fields exist in either
  migration (C6).

### Migrations 012–020 (waves 1–3, all schema-only, additive, idempotent)

| Migration | Adds | Contract / package |
|---|---|---|
| `012_city_assets_graph_integrity.sql` | `corridor.cameras` (camera SITES: location + identity only — no frame, image, stream URL or person column; `source` emulated \| osm \| registered, the last being an integration of the state's ICCC / CCTV / ITMS inventory this platform is built to strengthen), `corridor.agency_stations`, junction `name` / `name_gu`, `vehicle_earmarks.cleared_by`, the normalized-plate CHECK, recommendation evidence cascade | WP-03 (C6) |
| `013_event_lifecycle_audit.sql` | `events.event_status_log` (every corridor-event status, creation included, with its named actor), `corridor.signal_program_history.actor`, `corridor.signal_plan_log` (one row per committed signal-plan apply) | WP-02 (C-F) |
| `014_anomaly_log_hygiene.sql` | the detection-time index on `traffic.anomaly_log` (`COALESCE(flagged_at, timestamp_utc)`, serving `/anomalies/recent` and the bounded anomaly purge), `corridor.dispatch_events.detail` (actor provenance: `actor_source`, `claimed_officer`) | WP-16 |
| `015_road_display_geometry.sql` | `road_segments.display_geom` / `_source` / `_meta` (draw-only OSM traces, C-K) | WP-17 |
| `016_emulation_runs_interrupted.sql` | `emulator.emulation_runs.status` gains `interrupted` (a run whose process stopped mid-tape) | WP-19 (C-G) |
| `017_enforcement_stolen_sighting.sql` | `events.enforcement_recommendations.rule_id` gains `R_STOLEN_SIGHTING` (evidence-only, §16.3) | WP-19 (C-N) |
| `018_event_routes_supersede.sql` | `events.event_routes.superseded_at` / `variant` / `created_by` + the current-spine index (route updates, §16.4) | WP-19 (C-O) |
| `019_hourly_aggregates_synthetic.sql` | `traffic.hourly_aggregates_synthetic` — the simulated feed's rollup, kept apart from the REAL-only `traffic.hourly_aggregates` that calibration reads (§16.5) | WP-19 (C-P) |
| `020_road_network_osm.sql` | `road_segments.geom_source` / `geom_meta`, `junctions.osm_node_id` — the provenance of the routing geometry; the network data itself is `reanchor_osm_network.sql` (§16.1) | WP-20 (C-K basis `osm`) |

*Correction (this revision):* the `interrupted` status of
`emulator.emulation_runs` is widened by migration **016** (WP-19) — not
014, as an earlier note had it; 014 touches only the anomaly log and the
dispatch audit.

Migrations 001–006 are byte-immutable (checksum ledger); 007–020 are
additive-only, idempotent and schema-only. A fresh install of this tree
ledgers **20/20** in `corridor.schema_migrations` (verified on the
private fresh-install stack); the live store ledgered 19 at the
2026-10-23 reboots and applied 020 with the wave-4 deploy at 23:33 IST
that night (`veer_up.sh --build` ran `migrate.py` — `apply
020_road_network_osm.sql (82 ms)`, 20 in the ledger — then the
idempotent network step); its readiness row reads `network osm 49/49 ·
34 junctions · 20 migrations ledgered`.

---

## 6. ML Model Inventory

### 6.1 DSTGAT Flow Forecaster (ONNX) — architecture shipped, weights not yet

- **Architecture**: CausalTCN → TemporalAttentionPooling → CongestionGate → 3× DynamicGATLayer → 4× MultiHorizonHead
- **Config** (`models-config/configs/flow_forecaster.yaml`): hidden_dim 64, 4 heads, 3 layers, 8 input features, 2 output features, dropout 0.1; horizons 5/10/15/30 min; lookback 60 min; ONNX opset 18
- **VRAM budget**: 409 MB (`target_vram_mb` — a *budget*, not a shipped model's footprint), CUDA→CPU provider fallback via GpuArbiter
- **Training Loss**: `speed_MSE + 0.5 × congestion_MSE + 0.1 × regime_mismatch`

> **Artifact status (verified in-repo this revision):** no trained DSTGAT
> model ships — git tracks no `*.onnx` / `*.pt` / `*.pth` / `*.ckpt`, and
> the only such file on the box outside the venvs is the git-ignored
> third-party t0-alpha artifact of §6.1b (`models-local/t0/`);
> `models/onnx/` holds only `.gitkeep`; the runtime default
> `ONNX_MODEL_PATH = 'models/onnx/flow_forecaster.onnx'`
> (`traffic_oracle/data/config.py:12`) does not exist, so the DSTGAT rung
> logs `flow_forecaster.no_model` (`predict/flow_forecaster.py`) and stays
> dormant. Our neural path **cannot engage today**; it activates when an
> exported model is placed at that path. What answers instead is the
> rung ahead of it, t0-alpha (§6.1b), then the heuristic cache. The ETA
> (§6.2) and IsolationForest (§6.3) models likewise ship no trained
> artifacts (no `*.joblib`/`*.pkl` in-repo); their joblib training code
> exists, but nothing trains them on the live path. The road network is real (OpenStreetMap, §16.1), but every
> traffic observation the repo carries or the ingress publishes is
> synthetic (`data_quality = synthetic`) — there is no field traffic data
> to train on yet.

### 6.1b The wired forecast ladder — support hatch + t0-alpha foundation rung (Waves 1–2)

The DSTGAT rung above sits inside an explicit ladder in `FlowForecaster`
(`predict/flow_forecaster.py`), ordered **support gate → foundation rung
→ DSTGAT rung → heuristic fallback**:

- **Support hatch (Wave-1, `predict/support_bounds.py`).** Recorded
  per-feature support bounds (`FORECAST_SUPPORT_BOUNDS_PATH`,
  `data/config.py:38`) gate EVERY model rung: an out-of-support input
  never reaches a model — it falls back to the heuristic with the exact
  drift math, a labelled reason and log events. Bounds fail closed (NaN
  input is a violation, not a pass); a missing bounds file leaves the
  gate off loudly; a corrupt file disables the gate loudly.
  `tests/test_support_hatch.py` pins the leaf (schema, inclusive edges,
  violation fields, `record_from_observations` round-trip), the config
  surface and the forecaster integration — all pure-python, gate venv.
- **Foundation rung (Wave-2, `predict/nn/foundation_adapter.py`).**
  `FOUNDATION_MODEL_PATH` (`data/config.py:32`, env
  `VVIP_FOUNDATION_MODEL_PATH`; `None` keeps the rung dormant) points at
  the clone-local `models-local/t0/t0-alpha-grouped-int8.onnx` — the
  oracle image bakes that artifact in and sets the env
  (`infra/docker/Dockerfile.traffic-oracle.gb10`), so on the deployed
  stack the rung serves (`/health` `forecast_history.foundation_active:
  true` on a fresh install of this tree) — t0-alpha (~101.6M
  params), ONNX opset-20 INT8, sha256-pinned with full provenance and
  budget in `models-local/t0/NOTICE` (Apache-2.0, within the
  models-local policy). The rung needs ≥12 warm-up samples (shorter
  histories degrade loudly to the next rung with a labelled reason), and
  a corrupt artifact disables the rung without disabling the forecaster
  (fail-open, the support-bounds philosophy). The adapter emits the
  quantile contract of `predict/forecast_quantiles.py`: point fields
  `{speed_kmh, congestion_idx}` plus the optional
  `quantiles: {speed_kmh, congestion_idx} × {p10, p50, p90}`, monotone
  per field (speed p10 = the SLOW tail); consumers MUST validate
  (`validated_bands`) and degrade to point-only on violation — never
  trust the object blindly.

Wiring and contract tests run in the pure gate venv
(`tests/test_foundation_wiring.py`, `tests/test_foundation_adapter.py`);
the REAL-artifact tier — the real loader inside `FlowForecaster`
(`tests/test_foundation_real_artifact.py`) plus the integrity, contract,
determinism and latency probe (`scripts/probe_foundation_t0.py`) — runs
under the onnxruntime wave2 venv via `scripts/check_t0_forecaster.sh`
(verify_all tier `wave2-forecaster`). Measured on this host
(2026-10-18): 200 segments × 32 horizons ≈ 494 ms per forecast, VmHWM
1536 MiB — over the 0.4 GiB artifact budget, load-bound and documented
in `models-local/t0/NOTICE`.

### 6.2 ETA Predictor (scikit-learn HistGBT, CPU-only)

HistGradientBoostingRegressor (max_iter=200, max_depth=6) over 10 route
features → predicted travel time in seconds. Nothing trains it on the
live path today, so `POST /api/v1/predict/eta` answers with its labelled
fallback (`model: fallback`).

### 6.3 Isolation Forest Anomaly Detector (scikit-learn, CPU-only)

IsolationForest (n_estimators=100, contamination=0.05) over 8 features →
anomaly labels + scores — code only: the live anomaly path is the
ingress's Tier-1 checks (§2.1), which the oracle logs; this detector is
not wired into it.

### 6.4 Factored DQN Signal Controller (RL, per-signal, CPU-only)

Per-signal Q-network `Linear(6→32→3)` (323 parameters each); actions
{hold, extend_green_5s, skip_to_next_phase}; reward range [-100, +20] with
heavy convoy delay penalty.

### 6.5 pplx-decider-v1-27b (T5 strategic, model-host :8083)

| Property | Value |
|---|---|
| Precision | BF16 |
| Weights | **52,170,802,640 B (48.58 GiB)** across 11 shards + `readout.safetensors` **2,611,288 B** — both measured on disk |
| Readout | typed letter-code head; 255-code alphabet, **249 enforced option cap** |
| Temperature | 2.207568… baked in `decision_config.json`, never overridden |
| Question types | choice / noul / score; ≤6 per request, single forward pass |
| State budgets | ≤1500 tokens pre-movement · ≤400 per event · hard cap 8192 (413 beyond) |
| Queue | 8 serialized computes; 429 + Retry-After past this |
| Cold load | ≈5 min; resident forever, supervised restart-on-exit |

### 6.6 aditi-ps18-v7h Gemma 4 12B (T4 tactical, llama-server :8084)

| Property | Value |
|---|---|
| Quantization | Q4_K_M GGUF — `q4-stack/v7h-merged-Q4_K_M.gguf`, **7,381,382,880 B (≈6.87 GiB)** measured |
| Server | llama.cpp llama-server, **build 69bf643** (pinned in `models-config/configs/veer_models.yaml`; `/health` returns `{"status":"ok"}` when up — the running build string was recorded when the pin was made, not re-checked this pass) |
| Slots | 2 parallel × 8192-token contexts (`-c 16384`) |
| Chat template | the GGUF-embedded Gemma 4 canonical template (`--jinja`) — never hand-rendered |
| Constrained decoding | `response_format: json_schema` → `json_object` → prompt-embedded schema (3-rung ladder, `llm/llama_bridge.py`) |
| Tactical sampling | temperature 0.2 default in the bridge (deterministic JSON); serving defaults in `veer_models.yaml`: temp 0.6, top_p 0.85, top_k 35, repeat_penalty 1.15, num_predict 2048 |
| Metrics | Prometheus `/metrics` on :8084 |

### 6.7 Embedding models (T1, embedding-service :8085)

| Model | Role | Dim | Weights |
|---|---|---|---|
| pplx-embed-v2-late-0.6b | multi-vector late interaction, MaxSim rerank | 128 per-token | 1.11 GiB |
| BAAI/bge-m3 | dense ANN recall (unit-norm) | 1024 | 1.06 GiB |
| pplx-embed-v2-late-9b | aux slot — **disabled by default** | — | (+15.6 GiB if enabled) |

Embedding venv pins: torch 2.14.0, torchvision 0.29.0, transformers 5.17.0,
sentence-transformers 6.1.0, accelerate 1.15.0, pillow 12.3.0,
safetensors 0.8.0.

---

## 7. MCP Tools, Resources & Prompts

### MCP Tools (28 tools — `convoy-brain/src/convoy_brain/mcp/server.py`; `tools/list` on the wave-4 brain)

The original twelve compute/read tools:

| # | Tool Name | Purpose | Backend |
|---|---|---|---|
| 1 | `predict_traffic_flow` | DSTGAT T+5/10/15/30 forecast | `POST /api/v1/predict/flow` |
| 2 | `find_convoy_routes` | OR-Tools CP-SAT multi-objective routing | `POST /api/v1/optimize/routes` |
| 3 | `plan_diversions` | Per-segment diversion plans with timing | `POST /api/v1/optimize/diversions` |
| 4 | `evaluate_scenarios` | Monte Carlo scenario comparison | `POST /api/v1/evaluate/scenarios` |
| 5 | `predict_eta` | HistGBT travel time prediction | `POST /api/v1/predict/eta` |
| 6 | `query_shortest_path` | pgRouting Dijkstra | `GET /api/v1/graph/shortest-path` |
| 7 | `query_k_shortest_paths` | pgRouting KSP alternatives | `GET /api/v1/graph/k-shortest-paths` |
| 8 | `query_segments_in_bbox` | Spatial bbox segment query | `GET /api/v1/spatial/segments` |
| 9 | `query_segment_details` | Single segment attributes | `GET /api/v1/spatial/segments/{id}` |
| 10 | `get_live_traffic` | Live-cache read (Valkey) | `POST /api/v1/traffic/live` |
| 11 | `get_historical_pattern` | DuckDB hourly aggregates | `GET /api/v1/traffic/historical` |
| 12 | `simulate_twin` | Simulation-twin scenario run (adaptive shadow-eval / C5) | simulation router |

Plus sixteen added for the unified city picture (C-C / C-D2): ten
read-only city tools — `get_active_corridor_events`, `city_query_entities`,
`city_get_dossier`, `city_feed`, `get_signal_state`, `list_units`,
`list_dispatches`, `get_interception_plan`, `get_situation_log`,
`list_enforcement_recommendations` (every one a GET) — and the six canvas
verbs `canvas_fly_to`, `canvas_track_entity`, `canvas_highlight`,
`canvas_annotate`, `canvas_set_sensor`, `canvas_toggle_layer`, which call
no HTTP at all: they return a **directive** the operator applies or
dismisses on the canvas (`/chat/directives/ack`). **The read-only rail
(R2):** the executor POSTs only to seven compute endpoints
(`POST_ALLOWLIST`: predict flow / ETA, optimize routes / diversions,
evaluate scenarios, the live-cache read, the twin), and no tool name may
make the AI an actor (`FORBIDDEN_TOOL_VERBS`: approve, decide, earmark,
dispatch_request, withdraw, transition — pinned by the test suite).

### MCP Resources (4 resources)

| URI | Description |
|---|---|
| `traffic://anomalies/recent` | Anomalies detected in last hour |
| `traffic://corridor/summary` | Aggregated corridor conditions |
| `traffic://segments/{id}/history` | 24h history for segment |
| `convoy://movements/active` | Active convoy movements |

### MCP Prompt Templates (3 prompts)

| Prompt Name | Purpose | Key Arguments |
|---|---|---|
| `vvip_security_protocol` | Security classification + constraints | `vvip_class` |
| `route_analysis_brief` | Route scoring weights + criteria | `origin_name`, `destination_name`, `vvip_class` |
| `diversion_coordination_brief` | Multi-agency coordination template | `movement_id` |

---

## 8. AI Agent Architecture

### Agent Definitions (tactical tier, on Gemma)

| Agent | Output Schema | Key Fields |
|---|---|---|
| TrafficAnalystAgent | Corridor assessment | `overall_status`, `congestion_summary`, `risk_segments`, `trend`, `recommendation`, `confidence` |
| RoutePlannerAgent | Route ranking | `primary_route`, `alternate_routes`, `rejected_routes`, `overall_reasoning`, `confidence` |
| SecurityLiaisonAgent | Security validation | `compliant`, `violations`, `warnings`, `recommendations`, `security_score`, `confidence` |
| DiversionCoordinatorAgent | Diversion directives | `directives`, `queue_alerts`, `overall_status`, `confidence` |

### Workflows

| Workflow | Shape | Constraints |
|---|---|---|
| Pre-Movement | linear with security re-validation loop | `MAX_SECURITY_RETRIES = 2` (`pre_movement.py`) |
| Live Escort | background task (C-J): GPS ticks LLM-free; incidents → decider checkpoint → an officer's PROPOSAL (never a self-executed switch) | `ESCORT_MAX_ITERATIONS = 14400` (~4 h at the 1 s tick, `config.py`); adaptive spike threshold (fail-closed 0.8) with 0.05 hysteresis and a 10 s clear-confirm; adaptive GPS poll (fail-closed 1.0 s); lookahead 3 km; approval timeout 90 s (15–600); hold max 180 s |
| Post-Clearance | recovery poll loop | `RECOVERY_CONGESTION_THRESHOLD = 0.3`, `RECOVERY_POLL_INTERVAL_SEC = 5.0`, `MAX_RECOVERY_ITERATIONS = 120` |

The v1 `ACTIVATION_LOOKAHEAD_SEC` constant in live-escort was **dead code**
(sole reference in the repo) and is intentionally deleted; the real
application points are the adaptive parameters threaded into the
`plan_diversions` MCP call (protocol-floored per VVIP class before the call)
and this loop's poll interval / spike threshold.

### Decider checkpoint triggers and state budgets

| Trigger | Fires when | State budget |
|---|---|---|
| `pre_movement` | planning phase opens | ≤1500 tokens |
| `incident_detected` | incident-level congestion ahead | ≤400 tokens |
| `congestion_threshold_crossed` | adaptive threshold crossing (edge-triggered) | ≤400 tokens |
| `situation_changed` | adaptive class transition (Valkey `situation:changed`) | ≤400 tokens |
| `phase_transition` | plan → escort → clear | ≤400 tokens |
| `operator_chat` | operator sends a chat turn | ≤400 tokens |

Rate limit ≥5 s between checkpoints per movement; confidence floor 0.45;
p95 >3000 ms → Gemma-only fallback + operator alert (audit-logged).

---

## 9. IPC & Data Flow

### Zero-Copy Arrow IPC

signal-ingress serializes `TrafficObservation` into Arrow RecordBatches and publishes over NATS JetStream for zero-copy consumption by traffic-oracle.

**Arrow Schema**: `timestamp_ms` Int64 · `lon`/`lat` Float64 · `segment_id` Utf8 · `speed_kmh`/`congestion_index` Float32 · `source`/`data_quality` UInt8 · `confidence` Float32.

### NATS Subjects

| Subject | Format | Publisher | Consumer |
|---|---|---|---|
| `corridor.traffic.live` | Arrow IPC bytes | signal-ingress | traffic-oracle |
| `corridor.traffic.anomaly` | JSON | signal-ingress | traffic-oracle |
| `corridor.traffic.emulated` | Arrow IPC bytes (live-plane schema) | event-emulator | traffic-oracle caches it in its OWN key space `traffic:emulated:latest:{segment_id}` — only EMULATED events' bands read it, never a live event's; replays headered `X-Emulation-Replay` |
| `corridor.units.emulated` | JSON (C-A) | event-emulator | traffic-oracle → Valkey `units:emulated:{unit_id}` (EX 30) → `GET /api/v1/units` (`source: emulated_patrol`) |

### Valkey Cache Keys

| Key Pattern | TTL | Purpose |
|---|---|---|
| `traffic:latest:{segment_id}` | 300 s | Latest LIVE-plane observation per segment (the ingress feed — synthetic today, labelled) |
| `traffic:emulated:latest:{segment_id}` | 300 s | The event-emulator's latest row per segment — same JSON + `"origin": "emulated"`; never overwrites the live plane (`ingest/cache_writer.py`) |
| `units:emulated:{unit_id}` | 30 s | Emulator patrol frame (C-A) |
| `convoy:position:{movement_id}` | 30 s | Latest convoy fix, `source: gps \| emulated_gps` (C-B) |
| `corridor:summary:{corridor_id}` | 60 s | Aggregated corridor conditions |
| `traffic:forecast:{segment_id}:{horizon}` | 60 s | Cached DSTGAT predictions |
| `convoy:context:{movement_id}` | 24 h (completed) / none (active) | Convoy state **+ decisions audit log** |
| `adaptive:inputs:{movement_id}` | — | situation inputs doc (the emulator writes it for its events, beside the provenance sidecar `emulator:source:{id}`; without one the engine reads the live cache) |
| `adaptive:anomaly:active` | — | corridor anomaly flag for the classifier |
| `adaptive:params:{movement_id}` | 30 s | C4 adaptive parameters mirror |
| `situation:changed` | 60 s | situation-class transition notification (decider trigger #3) |

### Inter-Service HTTP

| From | To | Protocol | Purpose |
|---|---|---|---|
| convoy-brain | model-host :8083 | HTTP (httpx, 20 s decide timeout) | typed decider questions |
| convoy-brain | llama-server :8084 | HTTP (httpx; per-call wall max(30 s, 5 s + tokens ÷ 16 tok/s)) | Gemma tactical completions |
| convoy-brain | traffic-oracle :8091 | HTTP (30 s) | MCP tool proxies, corridor-event lifecycle (C-F), route updates (C-O) |
| command-deck | oracle :8091 / brain :8092 / emulator :8090 | HTTP via the deck's loopback proxy | every browser call; the signed-in officer forwarded as `x-veer-officer` |
| event-emulator | traffic-oracle :8091 | HTTP | lifecycle, ANPR ingest, dispatch, twin seams |
| convoy-brain | embedding-service :8085 | HTTP | similar-past-situation retrieval |
| convoy-brain | Valkey | Redis protocol | ConvoyContext persistence |
| traffic-oracle | embedding-service :8085 | HTTP (config-fixed host) | memory writes from incidents/scenario runs |

All outbound URLs are **fixed platform constants** (config-resolved, see
§14/§15) — no user-supplied URL is ever fetched (§14, SSRF guard).

---

## 10. Hardware Constraints & Unified-Memory Budget

### Target Hardware

NVIDIA GB10 (Grace-Blackwell DGX-Spark class) **unified memory**.
`/proc/meminfo` MemTotal = 127,533,980 kB (KiB) = **121.6 GiB** is the source
of truth (nvidia-smi reports `memory.total` as N/A on GB10). *Correction:
`models-config/MEMORY_BUDGET.md` and `veer_models.yaml` quote the same
MemTotal as "≈ 121.44 GiB" — an arithmetic slip (127,533,976 KiB ÷ 2²⁰ =
121.63 GiB); the budget below is re-stated on 121.6 GiB.* Disk: 2.0 TiB
free of 3.7 TiB (`df`, 2026-10-23).

### Resident budget (`models-config/MEMORY_BUDGET.md` — planned + measured)

| Resident process | Weights | Planned budget | Measured |
|---|---|---|---|
| model-host: decider 27B BF16 | 48.58 GiB | 55 GiB | cuda_reserved **48.99 GiB**; VmRSS 1.66 / VmHWM 5.92 GiB (weights migrate to the GPU side); warm decide 460–477 ms @ 138-token state |
| llama-server: Gemma 4 12B Q4_K_M (2×8192 ctx) | 6.87 GiB | 9.5 GiB | VmHWM **7.25 GiB** peak after first completion. *KiB-nit: `MEMORY_BUDGET.md:22` and the `gguf:` line of `veer_models.yaml` carry a stale "7,208,388 KiB" comment; the byte-exact file is 7,381,382,880 B = 7,208,382 KiB (6 KiB off) — flagged to the models stream, docs use the byte figure* |
| embedding-service (both encoders) | 2.17 GiB | 5.0 GiB | co-resident; cold start ≈36 s |
| traffic-oracle: forecaster model | — | 0.4 GiB (the DSTGAT budget) | CPU fallback kept; the live rung is t0-alpha, over that budget (probe VmHWM 1536 MiB, §6.1b) — a whole oracle with it loaded uses ≈1.6 GiB (`docker stats` on the private stacks, 2026-10-23), inside its 3.5 GiB |
| **Model subtotal** | | **≈69.9 GiB (≈57% of 121.6)** | bench stack resident: **62.61 GiB** (round 1) / **62.66 GiB** (round 2) / **62.73 GiB** (re-run 2026-10-13, IST) |
| Platform services (PostGIS 2.0 + Valkey 0.5 + NATS 0.5 + oracle 3.5 + brain 1.5 + dev 4) | | ≈12 GiB | |
| **Total planned** | | **≈82 GiB → ≈40 GiB headroom** | |

### Residency & serialization rules

1. **Nothing is ever evicted** — every model host is resident for the life of
   the process, supervised by its systemd `--user` unit (`Restart=always`,
   running `serve_models.sh --foreground`).
2. The v1 asyncio.Lock whole-stack VRAM-serialization regime is **retired**;
   compute-only serialization replaces it (decider compute lock + queue-8 in
   model-host; llama-server 2-slot scheduling; embedder batch lock).
3. Decider context budget: ≤400/event, ≤1500 pre-movement, hard cap 8192
   (413 on overflow — bidirectional attention means no KV/prefix reuse;
   prefix caching is useless and not attempted).
4. Gemma context budget: 8192/slot × 2 = 16,384 tokens KV; tool results
   truncated at 4 KB.
5. PostGIS, pgRouting, pgvector are CPU-only. No client-side simulation
   runs in the browser (the deck's former WebGPU simulation was retired with
   the `(legacy)` routes); the browser renders the Cesium canvas only.
6. ollama `:11434` runs externally (not ours, never restarted by us) — if its
   daemon loads `qwen3.5:9b` or the `gemma-vvip:12b` debug side-load,
   subtract that runner RSS from headroom.

---

## 11. Infrastructure & Deployment

### Bring-up — one command, and it survives a reboot

`bash scripts/veer_up.sh` (as the user that owns the stack, never root) is
the one idempotent bring-up: preflight → the model units → `docker
compose -p vvip-argus … up -d` → PostGIS healthy → `migrate.py` → seed if
empty (+ the OpenStreetMap network step — `reanchor_osm_network.sql`,
applied whenever the store is not on the generated network or the step
changed — and the post-seed repairs and the display file when they
change; after a network step the services that cache the network are
restarted once) → converge → a readiness table of **11
rows** (postgis — which names the network basis, e.g. `network osm 49/49 ·
34 junctions` — valkey, nats, signal-ingress, oracle, brain, deck,
emulator, decider, gemma, embeddings) ending `READY 11/11` (exit 0 only
then). `--status` prints the same table read-only (with the deck's bind
scope and its operator gate on a `security` row — a LAN-exposed deck with
the gate OFF reads DEGRADED); `--install-units` writes
`~/.config/veer/veer.env` (mode 600; secrets go in with
`scripts/veer_env.sh --set KEY -`, value on stdin) and links + enables
the systemd `--user` units of `infra/systemd/`; `--demo` switches the city
signal-plan writer and the living city on (refused while the citizen feed
does not label emulated events); `--build` rebuilds the `-argus` images
(they bake the source, so changed service code reaches the stack only
through it). One bring-up runs at a time (a lock).

**The road-network step** (`seed()`, `seed/reanchor_osm_network.sql`,
generated by `scripts/gen_osm_network.py`): one transaction, idempotent (a
second apply changes no row). It runs on a fresh store, when the file
differs from its run-dir stamp, **and whenever a read-only probe finds the
store off the generated network** — a segment whose `geom_source` is not
`osm`, or a segment / junction count other than the file's
`network-probe` line — so a pre-conversion backup restored later (a
rollback, a reboot onto one) is converted again whatever the stamp says.
Besides the network itself it carries the history that must stay on the
roads: each EMULATED camera read (`vehicle_sightings`, source
`emulated_anpr`) on a segment whose line changes keeps its fraction along
that segment on the new line, and so does the same read inside its run's
recorded tape (what a God's-eye REPLAY draws) — positions only; a
`live_anpr` read is never moved and no decision or audit trail is
touched; the use-case profiles' spines still at their seeded schematic
value become the library's legal routes, and the watchlist sweep
profile's seeded text is corrected (an operator's edit is kept). The
postgis readiness row names the basis — `network osm 49/49 · 34
junctions` — and reads DEGRADED while the store is off the generated
network. A bring-up that applied the step restarts the traffic-oracle,
the event-emulator and the command-deck **once** right after the seed
(they cache the network); one that fails before that restart leaves a
marker so the next bring-up restarts them, and nothing is restarted when
the network did not change — no manual restart or stamp removal belongs
in any runbook. An open event whose current route is no longer a legal
path is reported with a WARN (recorded routes are history and are never
rewritten: re-plan the event or close it). Each seed file runs as one
quiet transaction — no psql transaction warnings.

**Boot auto-start.** `veer.target` (WantedBy `default.target`) wants
`veer-models.target` — `veer-decider.service` (:8083),
`veer-llama.service` (:8084), `veer-embed.service` (:8085), each running
`infra/scripts/serve_models.sh --foreground <host>` with `Restart=always`
— and `veer-up.service` (oneshot, `scripts/veer_up.sh --boot`: wait for
Docker, bring up, converge; `Restart=on-failure`). All read
`%h/.config/veer/veer.env`, so a reboot, a unit restart and a manual run
see the same settings — the operator PIN and the ion token included.
**Verified by real reboots on 2026-10-23** (`journalctl --user -u
veer-up.service`): the unit started at 17:54:19 IST and printed
`READY 11/11` at 17:58:44 — **4 min 25 s, no manual command**; a second
reboot repeated it (18:06:48 → 18:11:15, 4 min 27 s). The ollama daemon
(`:11434`) is never started or stopped by any of this.

### Docker Compose services (project `vvip-argus`: `infra/compose.yml` + `infra/compose.gb10-override.yml` + `infra/compose.argus-override.yml`)

Every image is built from this tree under an `-argus` tag; every
container is `restart: unless-stopped`; only the deck is reachable from
the LAN.

| Container | Image | Bind | Notes |
|---|---|---|---|
| `vvip-argus-postgis` | `vvip-argus-postgis:arm64` (PostGIS + pgRouting + pgvector) | 127.0.0.1:56434 | Own volume; migrations 001–020 + the seed via `veer_up.sh` |
| `vvip-argus-valkey` | valkey | 127.0.0.1:6380 | Max memory 512 MB, allkeys-lfu |
| `vvip-argus-nats` | nats:2-alpine (JetStream) | 127.0.0.1:4223 (monitoring :8223) | |
| `vvip-argus-signal-ingress` | `vvip-argus-signal-ingress:latest` | — (NATS client) | the 49-segment network compiled in (§2.1) |
| `vvip-argus-traffic-oracle` | `vvip-argus-traffic-oracle:latest` | 127.0.0.1:8091 (host network) | `PORT` env; host networking so the loopback model hosts are reachable |
| `vvip-argus-convoy-brain` | `vvip-argus-convoy-brain:latest` | 127.0.0.1:8092 (host network) | model URLs loopback `{8083,8084,8085}`, `TRAFFIC_ORACLE_URL` → loopback 8091; `VVIP_REQUIRE_OFFICER`, `VVIP_ESCORT_SIM_GPS` from veer.env |
| `vvip-argus-command-deck` | `vvip-argus-command-deck:latest` | 0.0.0.0:5273 (host network) | proxy targets `ORACLE_PORT` / `BRAIN_PORT` / `EMULATOR_PORT`; `VEER_OPERATOR_PIN`, `VEER_SESSION_SECRET`, `VEER_CESIUM_ION_TOKEN` from veer.env |
| `vvip-argus-event-emulator` | `vvip-argus-event-emulator:latest` | 127.0.0.1:8090 (host network) | the only writer of `corridor.traffic.emulated` and of emulator runs-ledger rows; `EMULATOR_LIVING_CITY` off unless `--demo` |

### Dockerfiles

What the argus project builds (`infra/compose.argus-override.yml`):
`Dockerfile.postgis.arm64` (`pgvector/pgvector:pg16` + PostGIS +
pgRouting), `Dockerfile.signal-ingress` (`rust:alpine` → `alpine:3.21`),
`Dockerfile.traffic-oracle.gb10` and `Dockerfile.convoy-brain.gb10`
(`python:3.12-slim`, the repo-tree-mirroring images), `Dockerfile.event-emulator`
(`python:3.12-slim`), `Dockerfile.command-deck` (`oven/bun:1` build →
`oven/bun:1-slim`, the adapter-node build run by bun). Also in the tree:
`Dockerfile.postgis` (x86, `postgis/postgis:17-3.5`), the plain
`Dockerfile.traffic-oracle` / `Dockerfile.convoy-brain`, and
`Dockerfile.frontend` (the offline fallback, `node:20-alpine` → nginx).

### Deployment and gate scripts

| Script | Purpose |
|---|---|
| `scripts/veer_up.sh` | The bring-up above (`--status`, `--install-units`, `--demo`, `--boot`) |
| `scripts/veer_env.sh` | The host environment in one place (`~/.config/veer/veer.env`); `--set KEY -` reads a secret from stdin |
| `infra/scripts/serve_models.sh` | The three model hosts (run by the systemd units) |
| `scripts/migrate.py` | Ledgered migrations 001–020 |
| `scripts/gen_osm_network.py` · `gen_ingress_segments.py` · `gen_display_geometry.py` | The real-road network, the ingress table, the display file — each with a verify/check mode |
| `scripts/smoke_models.sh` | Live model-stack smoke (decider choice+noul+score; Gemma agent call; both embedders) |
| `scripts/verify_all.sh` | The acceptance ladder (§13): offline by default, `--live` signs in to the deck through `scripts/deck_session.sh` |
| `scripts/deck_session.sh` · `scripts/lib/deck_session.mjs` | The ONE operator sign-in helper every gated tool uses (PIN read at run time from `VEER_OPERATOR_PIN`, `VEER_OPERATOR_PIN_FILE` or veer.env — never printed; one attempt per run) |
| `scripts/argus_full_smoke.sh` · `scripts/fleet_smoke.sh` | Full-system live smoke of the stack |
| `scripts/showcase_run.sh` · `scripts/repetition_gate.sh` | The flagship "God's Eye Morning" (§13) and the consistency-under-repetition gate |
| `scripts/journeys.mjs` | The judge's journeys J1–J7 through the deck's own proxy (a private stack by default; `--live` only with the go-ahead) |
| `scripts/demo_reset.sh` | Pre-judging hygiene: dry-run (GET-only) by default; `--apply --officer '<name>'` stops at PENDING — a signed-in human approves live on `/ops` |
| `scripts/impact_run.py` | Deterministic impact model → `IMPACT_REPORT.md` (reads a running stack's network, or `--offline` the committed one) |

---

## 12. Observability & Monitoring

### Prometheus metrics

**model-host**: `model_host_*` (decides, failures, p95 latency, queue depth,
RSS) — plus llama-server's own `/metrics` on :8084.

**convoy-brain**: LLM token/latency metrics per tier, MCP tool
calls/duration/errors per tool, API request duration per endpoint,
orchestrator tool-round gauges, non-deterministic failure counters.

**traffic-oracle**: ONNX inference duration (model, provider), pgRouting
query duration, pgvector search duration, DB pool gauges, anomaly
detections by type/severity, observations processed, RSS.

### Alert Rules (23 rules, 8 groups — `infra/observability/alerts.yml`)

| Group | Covers |
|---|---|
| gpu_vram | unified-pool pressure, temperature |
| llm_performance | Gemma tok/s (flags bandwidth contention with the 27B), model-host unreachable/unloaded, llama-server unreachable, embedding-service unreachable, decider latency |
| mcp_tools | tool timeouts, error rate |
| reasoning_anomalies | tool-round loops, non-deterministic failures |
| api_performance | p95 per service, 5xx rates |
| spatial_db | pgRouting/pgvector latency, pool exhaustion |
| stream_processor | channel utilization, anomaly spikes |
| container_health | memory, restarts |

---

## 13. Testing Strategy

### Unit / integration tests

Every pytest suite under `VVIP/` runs **whole** in the acceptance gate
(`scripts/verify_all.sh`, one tier per suite): a test that needs torch /
onnx or a database skips itself (`importorskip` / `skipif`, the count
printed) — a suite is never skipped wholesale. signal-ingress runs
`cargo test`; the command-deck runs its own unit, contract and smoke
scripts (`tests/*.unit.mjs`, `tests/contract/run.mjs`, the SwiftShader
smokes).

*Collected on this tree (wave 4, as deployed) by the gate venv —
`VVIP/.venvs/gate/bin/python -m pytest --collect-only` per suite, the same
discovery `submission_consistency_check.mjs` performs, re-run on
2026-10-24 after the tier-0 fix:* convoy-brain
**635** (634 passed + 1 skipped), corridor-store importer **22** + store **107**,
embedding-service **113**, event-emulator **328**, traffic-oracle
**1500**, plus the command-deck's own **4** proxy-guard tests — **2709**
in all (the tier-0 fix added 50 convoy-brain tests to the 585 of
2026-10-23). Run whole on that tree (2026-10-24 01:19 IST, the gate
venv): 2624 passed and 85 skipped — convoy-brain 1, corridor-store 29,
event-emulator 13, traffic-oracle 42, each a test whose optional
dependency or scratch store (a database, a Valkey) is absent — 0 failed. `cargo test --locked` (signal-ingress): **120 passed** —
49 lib + 7 + 45 + 19 (§2.1; re-run 2026-10-24 in `rust:alpine` on a clean
in-container target). A cargo target volume shared with other checkouts
can report a stale build: the live acceptance ladder's offline run
(2026-10-23 23:34 IST) read 118 from the shared `veer-cargo-target`
volume — the stale-build caveat, not this tree's count.
*(History, kept: the 2026-10-13 interface revision's gate session
collected 182 / 22+12 / 113 / 93+1skip / 453+1skip; the 2026-10-18 run
262 / 22+12 / 113 / 94 / 633; the merged tree of 2026-10-23 2659, with
convoy-brain 585.)*

### The acceptance ladder (`scripts/verify_all.sh`)

`bash scripts/verify_all.sh` is the OFFLINE ladder — no operator
sign-in, no model call, nothing written to any stack; the deck tiers
serve the built deck on a PRIVATE preview (`:5199`, gate off and
keyless). It runs, in order and failing fast: **scripts-syntax** (`bash
-n`, `node --check`, a Python AST parse and a JSON load over the gate and
release scripts), **gods-eye-check** (svelte-check), **gods-eye-canvas**
(SwiftShader canvas smoke),
**design-smoke** (the cockpit-topology tripwire: `/` IS the Convoy
Command cockpit), **deck-ui** (the hermetic ops-wall smoke incl. the C6
'no officer' masking group, the recorded-payload contract tests, every
`*.unit.mjs`, the edge-basemap entry, the GET-only ops-write probe),
**`pytest-<suite>`** (every suite, whole), **cargo**,
**display-geometry** (`gen_display_geometry.py --verify`),
**ingress-segments** (`gen_ingress_segments.py --check`),
**submission-consistency** (the docs-vs-tree oracle and its
`--selftest`), then the wave tiers **wave1-hatches**,
**wave2-forecaster**, **wave2-routing**, **wave2-tier0**,
**wave3-repetition** (`repetition_gate.sh --fast`) and **wave3-ledger**.
`bash scripts/verify_all.sh --live` runs the LIVE tiers against the deck
(`http://127.0.0.1:5273` by default): **live** — ONE operator sign-in
through `scripts/deck_session.sh` (GET-only reads through the deck's
proxy; the gate must refuse the anonymous operator reads; the ion token
must never reach an anonymous client); **demo-reset** —
`scripts/demo_reset.sh` dry-run (GET + read-only SELECT); **live-smokes**
— `ops-live-smoke.mjs` then `integration-agent.mjs` (gate-aware, one
sign-in through the shared helper); then **models** (`smoke_models.sh`)
and **model-drift** (`VERIFY_LIVE_MODELS=0` skips the two model tiers, and
says so, on a private rehearsal whose models are the live ones). The
TEST-titled smokes (`argus_full_smoke.sh`, `fleet_smoke.sh`,
`check_tier0_live.sh`) are not part of `--live`: they run on demand. The
tier inventory and its order live in the script
header; the legacy React `frontend/` (the offline fallback demo) was
retired from the ladder. The ladder's earlier shape, kept as history: the
2026-10-17/18 waves ran frontend-bundle, frontend-e2e, the two gods-eye
tiers, the pytest suites, submission-consistency, the six wave tiers,
models and model-drift in one offline run.

The wave tiers, as introduced:

- **wave1-hatches** — `check_forecaster_hatch.sh` + `check_brain_latch.sh`
  (the forecast support hatch and the decider novelty latch, §3/§6.1b).
- **wave2-forecaster** — `check_t0_forecaster.sh`: gate-venv wiring and
  contract tests, then the real ONNX artifact through the wave2 venv.
- **wave2-routing** — `check_routing_quantiles.sh`: the P90-robust
  routing objective plus the protocol-params drift guard.
- **wave2-tier0** — `check_tier0_gate.sh`: the full convoy-brain suite
  including the tier-0 gate.
- **wave3-repetition** — `repetition_gate.sh --fast`: the offline selftest
  of the consistency-under-repetition gate (comparator, normalizer and
  FAIL path proven on fixtures; the FULL live gate is on-demand,
  `bash scripts/repetition_gate.sh`, tens of minutes).
- **wave3-ledger** — `check_degradation_ledger.sh`: the
  degradation-ledger taxonomy tests over `decisions_log` (gate venv).

The wave check scripts resolve every path from their own location, take
no arguments, and run the gate venv bootstrapped by the pytest tier.

- **P90-robust routing (Wave-2).** `optimize/quantile_objective.py` is a
  pure leaf (torch-free, mirrors the hatch leaf pattern) pricing edge
  costs on the ADVERSE quantile tail of the forecast rows: level q on
  congestion, 1−q on speed — q = 0.90 prices speed p10 + congestion p90.
  The knob `routing.robust_routing` is registered in
  `shared/schemas/protocol_params.json` (clamped to the registered
  0.5–0.99 bounds) and materialized into BOTH services' generated
  `protocol_params_gen.py`; `scripts/export_protocol_params.py --check`
  — run by the tier — fails on any drift. Absent or non-monotone
  streams degrade per-segment (or whole-stream under strict) to today's
  point path bit-for-bit. `tests/test_quantile_objective.py` (44 tests
  at the 2026-10-18 run) pins the leaf and its thin integrations
  (`corridor_router`, `mip_solver`, the ETA provider), including
  cross-checks that rows built by the REAL producer
  (`predict.forecast_quantiles`) are accepted end-to-end.
- **Banded ETAs.** The traffic-oracle ETA endpoint attaches
  `eta_band` / `eta_band_reason` derived from the same validated
  quantile stream (`__main__.py` `_compute_eta_band`): present when
  the producer supplies monotone bands, honestly labelled whenever it
  degrades.
- **Model-drift panel.** `scripts/model_drift_panel.sh` probes the four
  hosted surfaces (decider :8083 typed checkpoint with cross-field
  consistency, Gemma :8084 JSON chat parse, embedders :8085 marker
  retrieval, oracle :8091 heuristic summary against its own congestion
  thresholds) and compares against `scripts/drift_baseline.json`. The
  drift surface is schema + required fields + sane ranges + semantic
  anchors + latency bounds — never byte comparison (free generation is
  sampling-dependent) — and each anchor's evidence is recorded in the
  baseline under `justifications`.

### Wave-3 — the degradation ledger + the repetition gate (2026-10-18)

**Degradation ledger (the self-criticism surface).** The oracle's `ops/`
module (§2.2 — read-only router + the pure `degradation_taxonomy.py`)
serves `GET /api/v1/ops/degradation-taxonomy`: it scans the
`convoy:context:*` Valkey keys convoy-brain persists and groups the
degradation/failure rows of each `decisions_log` into a recurring-cause
taxonomy — `checkpoint_fallback:<tier>` (a checkpoint answered by a
fallback tier, e.g. `gemma`), `checkpoint_rate_limited` (cadence
refusal), `novelty_latch:novel` (the Wave-1 latch firing, §3),
`tier0:<miss status>` (an ARMED tier-0 gate that failed to serve, §3),
`alert:<type>` (operator alerts — classified by SHAPE, any non-checkpoint
row carrying a `type` field, so future alert phases classify without
touching the module) and `orchestrator:<action>` for failed turns. It
fabricates nothing: an empty log yields an honest empty taxonomy, every
entry carries a `sample_ref` naming one real audit row, and corrupt
contexts are counted, never silently skipped. The taxonomy is pinned by
`services/traffic-oracle/tests/test_degradation_taxonomy.py` (the
wave3-ledger tier, `check_degradation_ledger.sh`). The **deck panel**
(`services/command-deck/src/lib/ops/components/DegradationLedger.svelte`,
fed by the ops store's 10 s poll of the same endpoint) renders the ledger
on `/ops` — click a row for the full audit entry, a down endpoint degrades
to a labelled note over the last-good ledger, and a clean log renders the
honest EmptyState, never invented entries.

**Repetition gate — `scripts/repetition_gate.sh`.** The
consistency-under-repetition proof for the flagship showcase: restore an
identical baseline, run `showcase_run.sh` N times (default 2) against the
live argus fleet, and the terminal database state the showcase's phase-8
assertions are judged over (`adaptive.situation_log`,
`adaptive.param_overrides`, the `events.*` tables, plus the
pursuit/dispatch/earmark terminal tables) is IDENTICAL across rounds —
after normalizing exactly the volatility classes that are provably not
system output (wall clock, the harness's per-run sweep seed,
`gen_random_uuid`/BIGSERIAL identity, one in-process counter, and
engine-loop row multiplicity/sampling — the engine loop polls a doc the
flagship tape overwrites many times per poll period), every
normalization justified in a manifest printed on every run; anything else
that differs FAILS with the differing rows. Each round resets the ARGUS
traffic-oracle process (its in-memory adaptive state cannot be restored
by any DB snapshot) and the database is restored to baseline and left as
found. Because emulator runs are run-scoped (§2.9), each round's
flagship subject is a fresh id that the comparison must map across
rounds. **The restore is ownership-scoped**: it deletes only rows the
round provably wrote (its emulator runs and their events, the scripted
officer's pursuit, those events' children, the reads carrying the
round's run ids and their recommendations, the showcase's earmark of the
stolen plate, vehicles wholly the round's, the round subjects' movements
and audit rows), and its only UPDATE puts back a baseline vehicle whose
change is exactly the round's own read/earmark widening. It never
re-inserts anything (a read the retention reaper deleted stays deleted)
and never deletes, reverts or re-inserts anyone else's row: an officer's
approval, earmark or status change, or a living-city run, during the gate
is listed and FAILS it — "the store was not quiescent" — untouched, and a
round entangled with an officer's work (say, an officer decided one of
its recommendations) restores nothing at all. Every foreign key into the
scoped tables is read from `pg_catalog` and checked before any DELETE, so
no CASCADE / SET NULL can reach a row that is not the round's; each pass
reads one REPEATABLE READ snapshot. Run the live gate with `/ops` idle.
`showcase_run.sh` and `repetition_gate.sh` switch the C7 signal-policy
writer on **only for their own run** (the oracle recreated with
`VVIP_SIGNAL_POLICY_WRITER=1`) and put it back to its pre-run value
afterwards — checked, and also on die, Ctrl-C or TERM through an exit
trap (`ORACLE_WRITER_CMD` recreates a private stack's oracle).
`--fast` is the offline selftest (comparator/normalizer/FAIL path and
three restore-classifier checks on fixtures — the ladder tier above); the
full live gate is on demand and takes tens of minutes.
**Run live on the deployed stack (`repetition_gate.sh 2`, live acceptance
step 13, 2026-10-23 23:55 → 2026-10-24 00:02 IST): REPETITION GATE
PASS** — two rounds of God's Eye Morning (flagship runs 25 and 27, the
sweep seed pinned to 20261105) left IDENTICAL canonical terminal-state
projections (65 canonical lines, the 13-class normalization manifest
printed with them) from identical baselines (20 tables, 17,720 rows).
After each round the restore deleted only that round's own rows (34
sightings, 6 recommendations, 3 events, 1 dispatch, … per round),
reverted the 8 baseline vehicles the round had widened, kept the
situation engine's audit rows of every other subject, and verified "no
round row left; every baseline row present and unchanged"; the guarded
containers were untouched and the oracle's signal-plan writer went back
to its pre-gate value. So no round's rows stay in the store: the showcase
artifacts the gate leaves in `showcase/` (§13's flagship table) are round
2's run, whose rows the restore removed by design.

**`scripts/argus_full_smoke.sh` — the stack's full-system live smoke.**
Proves the tree runs LIVE end to end on the argus stack (compose project
`vvip-argus`), one line per leg, after a bounded converge-wait that
absorbs exactly the redeploy window: the decider, Gemma and embeddings
(:8083/:8084/:8085); the emulator (judged on connectivity — a historical
`degraded` prints its `degraded_reasons`); the oracle and its corridor
summary; **the road network** — 49 segments over the city, every one
`geometry_basis: osm`, none schematic; **`traffic:latest` freshness** (the
newest key ≤ 60 s old); the brain with both model tiers connected; the
`-argus` image tags; PostGIS (the ledger count equals the migration files
on disk), Valkey, NATS; **signal-ingress working** (a bounded subscription
sees ≥ 1 message) and **the oracle consumer** consuming in the same window
(its ingest counters rise, cross-checked on the NATS monitor's `/connz`);
the deck's seven pages, the operator gate (an anonymous `/api/v1` read
refused 403) and ONE sign-in through `scripts/deck_session.sh`; a real
movement plan (Lal Darwaja → SVPI Airport on the re-anchored POIs, class
Z) with its ETA band and **a non-empty diversion plan for that Z route**;
the **city picture** (`/city/entities` kind counts, `/cameras`, the
event audit of this run's own event, `/traffic/historical` provenance,
`/signals/plans` `city_band_since`); the **MCP rail** (`tools/list` ≥ 20
tools, none an actor); and **chat → canvas directive** ("show me the
stolen vehicle on the map": a tool call, prose and — when the city holds
an earmarked vehicle — a directive aimed at it); then the drift panel. The
movements smokes create are titled so they can never read as real ones —
`ARGUS SMOKE TEST`, `FLEET SMOKE TEST` (`fleet_smoke.sh`), `TIER-0 GATE
TEST` (`check_tier0_live.sh`) — and wear the deck's TEST pill. A private
sandbox runs the same script with its ports and containers in env plus
`EMBED_PROBE=read` (nothing written to the shared embedding store),
`DRIFT_PANEL=0`, `NATS_MON_URL=` and `DECK_CONTAINER=` (each prints SKIP).
**Negative control** (live only with the user's go-ahead — it darkens the
feed ~7 minutes): with `vvip-argus-signal-ingress` stopped for 6 minutes
(past the 300 s `traffic:latest` TTL) the smoke must exit non-zero on the
data-plane legs (traffic:latest, signal-ingress, oracle consumer) while
everything else stays green; after a restart it exits 0 again. Its
read-only guard over the original `vvip` stack (retired 2026-10-19)
reports SKIP when no original container exists.

**Image ownership.** Every image the argus project runs is built from
this tree under an `-argus` tag (`infra/compose.argus-override.yml`,
§11) — since the original fleet's retirement the postgis and
signal-ingress images are argus-owned too, so a cleanup of the retired
project's images can never strand the live stack.

**Submission-consistency oracle — `scripts/submission_consistency_check.mjs`.**
The submission-material registry as code (~20 checks, each with a
failure path proven by `--selftest`): deck/script/maker path existence,
figure-vs-artifact agreement (bench, showcase, pursuit, pixel proofs),
PPTX/PDF freshness, pytest-count agreement between the deck and adjacent
docs, and gate-script contract checks. It is itself the
submission-consistency tier of the ladder above; the submission deck was
regenerated against it (2026-10-18 evening, again for wave 4, and again
from the live run's artifacts on 2026-10-24). Hero shots taken apart from
the run they illustrate must be disclosed with their own trace figure
(`showcase-artifacts-single-run`), and every colour, entity and card
figure quoted for them must equal the shot logs.

### The judge's journeys — `scripts/journeys.mjs` (J1–J7)

Every journey runs end to end through ONE deck origin (`DECK_URL`) — the
operator gate, the verified `x-veer-officer` header and the loopback
pinning all in the path — signed in ONCE through the shared helper
(`scripts/lib/deck_session.mjs`) as the officer `Journey check
(automated)`:

| Journey | What it proves |
|---|---|
| J1 | the city at rest — honest labels, no false ⚠: every fusion feed answers, cameras / units / sightings say EMULATED, the canvas says SIMULATED FEED exactly when the oracle says the traffic is synthetic, the roads are the OSM-traced network |
| J2 | the stolen vehicle: an EMULATED ANPR read → hit → PENDING `R_STOLEN_SIGHTING` (the penal rules withheld) → an e-challan on that plate refused (409) and dismissed → the named officer approves the sighting → no pursuit opens from the e-challan (422) → pursuit opened from the approved sighting → interception plan → dispatch to choke #1 → approval → the unit moves → pursuit closed → the unit released |
| J3 | the escort: a Z-class plan for the escort pair of `scripts/data/route_acceptance_pairs.json` → its spine on the board → the escort on EMULATED GPS → the marker moves → an AI proposal approved by the officer, or none and the approved plan holds → clear (C-F closed) |
| J4 | the 108 corridor: the anonymous `/driver` request (ambulance only) → `/ops` approval by the named officer → the unit on the canvas and on the driver's own view |
| J5 | chat: "show me the stolen vehicle on the map" → tool call, prose and a canvas directive → the AI-suggests chip (never auto-applied) → Apply → one ack |
| J6 | the citizen pages in EN / HI / GU: emulated rows badged SIMULATED FEED · EMULATED, a plan beyond the 2-hour horizon under "Later" with its IST date, no plate, no pursuit |
| J7 | the reboot checklist (manual), with its recorded evidence: `scripts/data/journeys/reboot_evidence.json` from `journalctl --user -u veer-up` — READY 11/11 in 265.3 s (17:54:19 → 17:58:44 IST) and 267.2 s (18:06:48 → 18:11:15) after two real reboots on 2026-10-23, no manual command |

Every row a journey creates is labelled `JOURNEY CHECK (automated)` (so
the citizen projection badges it EMULATED, never LIVE), every plate it
invents carries EM, and a cleanup that always runs closes every event,
dispatch, escort and earmark it opened; it approves only its own rows.
It refuses the live deck `:5273` unless `--live` is passed (the user's
go-ahead); `--only J2,J5` picks journeys, `--no-browser` keeps every
check at API level; artifacts go to `$VEER_LOG_DIR/journeys-<stamp>/`.
J3 (plan) and J5 (chat) call the shared models one call at a time.
A cleanup that cannot close its row fails the run, and SIGINT / SIGTERM
close the rows before exiting. Exit: 0 when J1–J6 pass (J7 is a recorded
manual check) · 1 a journey failed, or a cleanup left a row open · 2
usage / refusal · 130 / 143 interrupted (the rows closed first).
**Result on the live deck (`journeys.mjs --live`, live acceptance step
14, 2026-10-24 00:02–00:06 IST, gate ON, the deployed tree): J1–J6
PASS — 17/17, 23/23, 13/13, 11/11, 10/10 and 18/18, 92/92 checks — and
the J7 checklist recorded; every row it opened closed again**
(artifacts `$VEER_LOG_DIR/journeys-20261023T183200/`). J2's 422 is the
refusal of a pursuit asked for on the e-challan the officer had just
dismissed; J1 found every entity on the city picture (101 of 101)
carrying its source, 28 cameras and 8 sightings EMULATED and the roads
`{"osm":49}`; J3's escort saw no AI proposal in its window, so it proved
the hold branch (the approved plan held — the approve / reject / expiry
path is `tests/test_escort_authority.py`'s and the hermetic ops-wall
smoke's); J6 found no pursuit
and no plate on the public feed in EN, HI and GU. The same 92/92 was
recorded earlier on the WP-15 package's private fresh-install stack
(2026-10-23 20:20 IST, before the merge).

### Pre-judging hygiene — `scripts/demo_reset.sh`

Puts the stack into the judged starting state **without deciding
anything a human must decide** — it never approves a sighting, a dispatch
or an e-challan; a signed-in officer authorises the pursuit live on
`/ops`.

- **Dry-run by default — GET-only by construction**: it prints the
  inventory and the plan (rehearsed on a live-store clone: a fingerprint
  of every row of the touched tables, the runs ledger and Valkey was
  identical before and after — zero writes).
- **`--apply --officer '<name>'`** (with the user's go-ahead) signs in
  through `scripts/deck_session.sh` as that officer, so every write is
  recorded under that name. It verifies the stack (READY 11/11,
  migrations 015–020 ledgered, the network served as `osm`, the operator
  gate on, the ion mode when keyed); stops stale emulator runs; applies
  the stolen-vehicle remediation — cancels the old fine-based pursuit
  `4508d86d-…` through the audited C-F lifecycle and has the officer
  REJECT the pending penal recommendations on the stolen plate (they are
  unapprovable, 409 `e-challan withheld — vehicle reported stolen`; the
  approved e-challan behind that old pursuit stays as superseded audit
  history — decisions are final); stages `watchlist_anpr_sweep`, which
  ensures the FIR earmark first, and waits until a PENDING
  `R_STOLEN_SIGHTING` and that run's JR-EM patrol units exist; then
  verifies the judged state GET-only and prints the judge checklist.
  Rehearsed on the clone: the first apply made 5 writes (the pursuit
  cancelled, 3 penal recommendations rejected, the sweep staged); a second
  apply was a no-op.
- **It verifies what `/ops` will offer**, derived with the deck's own
  rules (`ops_offer`, a read-only mirror of `DecisionQueue.svelte`
  `stolenGroups` and `store.svelte.ts` `pursuitCandidates`): ONE
  stolen-vehicle card with AUTHORISE PURSUIT + DISMISS (no sighting
  approved yet), and a pursuit card with no basis until that human
  approval — never an e-challan. Like the deck, it skips an approval
  already spent on a closed pursuit (one approval authorises one pursuit,
  §16.3), so the showcase's scripted approval — its pursuit closed by the
  showcase itself — no longer pre-empts the judged AUTHORISE. An approved
  sighting of the stolen plate that is NOT yet spent (no closed pursuit
  behind it) would: decisions are final, so the dry-run WARNs and the
  `--apply` verification FAILS and names it.
- **Debris** (acceptance and rehearsal rows): the smokes' TEST-labelled
  events (`ARGUS SMOKE TEST`, `FLEET SMOKE TEST`, `TIER-0 GATE TEST`), the
  journeys' `JOURNEY CHECK (automated)` rows, the showcase's per-run
  residue (rows attributed to `Scripted demo officer (showcase script)` /
  "God's Eye Morning showcase", events of earlier flagship or sweep runs)
  and the acceptance rows. By default they are left as they are and the
  verification FAILS truthfully while any is visible on `/ops`;
  **`--cancel-debris`** (with `--apply`) cancels each LIVE debris event
  through C-F as the named officer (its open dispatches close with it),
  clears a leftover journey exercise plate (its pending sightings
  dismissed, its earmark cleared) and dismisses the stolen plate's stale
  EMULATED sightings from earlier runs, so its card counts only the
  staging sweep's. It cancels every live event that carries a
  `metadata.label` — read the dry-run's plan lines first. Finished
  dispatch tiles fold into `/ops`'s history (n) 30 minutes after they
  end; the verification prints that fold time, so the reset runs **at
  least 30 minutes after the showcase and the journeys** (or is re-run, a
  no-op, after the printed time). Rehearsed on a rebuilt clone with
  injected smoke debris: `--apply` made 5 writes and FAILed honestly,
  `--apply --cancel-debris` 2 writes and exit 0, a further `--apply` 0.
- The staged EMULATED sweep keeps running for 45 minutes — the JR-EM
  patrol units exist only while a run publishes them (C-A, Valkey EX 30) —
  so re-run it within about 20 minutes of judging; a second `--apply` is
  a no-op while the sweep is fresh, `--restage` forces a safe re-stage.
- **The live order on release day** (each write with the user's
  go-ahead): the live smokes → `showcase_run.sh` → `repetition_gate.sh`
  (with `/ops` idle) → `journeys.mjs --live` → `demo_reset.sh --apply
  --officer '<name>' --cancel-debris`, the last ≥ 30 minutes after the
  showcase and the journeys and ≤ 20 minutes before judging.
- **Audit rows are never deleted**: `--purge-acceptance` only lists the
  rows a purge would delete unless `VEER_PURGE_ACCEPTANCE_OK=yes`
  (rehearsed on a scratch copy of the database only). Exit: 0 done · 1 a
  step or a check failed · 2 usage / preflight refusal · 3 purge listed
  but not confirmed. Logs: `$VEER_LOG_DIR/demo_reset-*`.
- **Run on the live stack (2026-10-24 IST, as the named officer
  `Scripted demo officer (demo reset)`):** 00:28 the dry-run listed the
  old pursuit `4508d86d` still planning, 3 pending penal rows and 5 stale
  sightings on the stolen plate, 5 TEST-labelled smoke events and 2
  finished journey dispatch tiles; `--apply --cancel-debris` made **15
  writes** — `4508d86d` cancelled through C-F, the 3 penal rows rejected,
  the 5 TEST events cancelled, the 5 stale sightings dismissed, sweep run
  29 staged (a PENDING `R_STOLEN_SIGHTING` and 2 JR-EM units after 70 s)
  — and FAILED one check as designed: the 2 journey tiles stay visible
  until they fold into history at 00:32 / 00:34; a second `--apply`
  (00:29) made 0 writes and failed the same check, the tiles not yet
  folded. After the tier-0 gate's re-run (2 more TEST events) the
  01:23 `--apply` made **9 writes** (those 2 events cancelled, 6 stale
  sightings dismissed, sweep run 30 staged — PENDING sighting + 2 JR-EM
  units after 85 s) and **every check passed, exit 0**: 1 PENDING
  `R_STOLEN_SIGHTING` and 0 pending penal rows on GJ27EM9001, `4508d86d`
  cancelled, its e-challan `e91d4591` still APPROVED as superseded audit
  history, `/ops` offering AUTHORISE PURSUIT + DISMISS on the
  stolen-vehicle card and a pursuit card with no basis, no acceptance
  debris visible on `/ops` (the city-scope audit stream still lists 34
  dated acceptance/showcase rows among its newest 100 — the ledger is
  never edited). The staging sweep completes on its own at 02:08 IST.

### Adversarial testing (`tests/adversarial/` + `scripts/scenario_library.json`)

- **21-scenario suite** (14 routing — route, edge, traffic, diversion,
  security, multi-turn, adversarial-injection, adversarial-overload
  categories — plus **5 watchlist/enforcement scenarios** added with the
  enforcement family: earmark, challan-approval, stolen-sweep,
  pursuit-initiation, emulated-badge; plus 2 stress scenarios for the
  stress mode). The bench's headless standard run gates on the **19**
  non-stress scenarios (`runner.py` batches `ROUTING_SCENARIOS` +
  `WATCHLIST_SCENARIOS`). Two libraries exist, by design:
  `scripts/scenario_library.json` (23 scenarios, `veer/scenario-library/1`
  format) is the **twin's** exported library (`simulation/scenarios.py`,
  written by `python -m traffic_oracle.simulation export`), and it
  includes the eight use-case scenarios; the **emulator loads its own
  10-entry copy** (`src/event_emulator/use_case_library.json`,
  `library.py:17` — 8 corridor use cases + the flagship composite + the
  emulated `watchlist_anpr_sweep`) whose entries carry `scenarioConfig`
  only, with the `event_kind`/`crowd_class` axes supplied by its hardcoded
  `profiles.py` (values matching the twin's).
- Components: `config.py` (scenarios), `runner.py` (execution),
  `evaluator.py` (reasoning assessment), `report.py`,
  `stability.py` (determinism checks)
- **Re-measured this revision (all runs headless standard, 19 scenarios
  since the watchlist five landed): 2026-10-13T13:24:26Z (the
  interface-revision re-run) 19/19 passed** — avg **29.19 s** / max
  **57.38 s**, zero 504s, zero evaluator crashes
  (`scripts/bench_adversarial_report.json`, the run that produced the
  current `bench_results.json`: decider 455.78 tok/s, p99 1106.8 ms).
  Earlier same-day runs: 2026-10-13T04:09Z 19/19 (avg 25.73 s / max
  69.99 s — one scenario over the scored 60 s line, recorded because it
  is true; its report was later overwritten, so it is doc-only history —
  see §3's artifact-honesty note); 2026-10-13T04:33Z (the docs-pass
  re-execution) 19/19 — avg 25.98 s / max 47.6 s.
  Earlier history at 14 scenarios: 2026-10-12 post-fix 14/14 (avg 29.27 s
  / max 52.03 s, `bench_adversarial_report.r1fix.json`), a 19:50Z re-run
  14/14 (avg 29.32 s / max 56.02 s), and a 19:23Z artifact 14/14 with one
  scenario at 68.59 s. Latency honesty: the 60 s line is a scored
  dimension, not a hard gate. **No evaluation threshold was touched** by
  any fix work; what changed is listed in §3.
  *Provenance caveat:* the round-1 0/14 report file was overwritten by
  later re-runs, so the 0/14 detail now survives only in
  `scripts/bench_run.log` (git-ignored, on the box — round-1 window,
  adversarial tier "exited rc=1") and the round-1 column of the §3 table.
- **Open quality gaps (pass the gate, tracked, not hidden):**
  tool_validity **0.684** docs-pass run (0.643 prior; the decider often
  final-answers after one tool where the scenario expects two) and
  keyword_coverage **0.561** docs-pass run (0.579 / 0.595 in the two
  priors; it moves run to run); reasoning_depth 0.30–0.41 across runs.
  These do not gate the suite but are the next quality targets.

### Live acceptance on the deployed stack (2026-10-23/24 IST)

**The deploy.** At 23:33 IST on 2026-10-23 the release tree went live:
`COMPOSE_PARALLEL_LIMIT=2 bash scripts/veer_up.sh --build --timeout 900`
→ exit 0, **READY 11/11** — migration 020 applied (82 ms, 20 in the
ledger), the network step applied (`reanchor_osm_network.sql`, the
post-seed derivations, the display file), the oracle, emulator and deck
restarted once after it, and one WARN: the old pursuit `4508d86d`'s
route is no longer a legal path on the real-road network (the pre-judging
reset cancels it). A `corridor_db` dump, the seed stamps and
`:pre-wave3c` rollback tags on every `vvip-argus-*` image were taken
first.

**The ladder** — the documented live gates in order, one log per step
(23:33:54 → 00:22:00 IST): **19 of 22 passed on the first run.**

| # | Step | Result |
|---|---|---|
| 1–2 | `veer_up.sh --status`; API probes | READY 11/11; 49 segments, all `osm`; 34 junctions; 191.76 km; 20 migrations; escort authority `officer_approves_every_switch`; 28 MCP tools |
| 3 | `verify_all.sh` (the offline ladder, on the tree as deployed) | ALL TIERS PASSED (its pytest tier still before the tier-0 fix; its cargo tier read 118 from the shared target volume — the stale-build caveat of the test inventory above) |
| 4 | `argus_full_smoke.sh` | ALL PASS |
| 5–6 | negative control: signal-ingress stopped for 6 minutes, then started again | exit 1 with exactly the four data-plane legs failing (`traffic:latest`, signal-ingress, the oracle consumer twice), everything else green; ALL PASS again after the restart |
| 7 | `fleet_smoke.sh` | ALL PASS |
| 8 | `check_tier0_live.sh` | **FAIL** — (a) no tier-0 hit on three identical plans; fixed and re-run green (below) |
| 9 | the model drift panel | 4/4 probes green |
| 10 | `verify_all.sh --live` | ALL TIERS PASSED (live) |
| 11 | `repetition_gate.sh --fast` | PASS |
| 12 | `showcase_run.sh` | SHOWCASE PASS (flagship run 23, sweep run 24) |
| 13 | `repetition_gate.sh 2` | REPETITION GATE PASS — two rounds identical (§13 Wave-3) |
| 14 | `journeys.mjs --live` | J1–J6 92/92, J7 recorded |
| 15–17 | the design, gods-eye and gods-eye-3d smokes | all assertions passed — chip `PHOTOREAL 3D · GOOGLE`, every overlay layer 0 floating, 0 CSP violations, no frame over 3 s, City Command at −42.0° with the camera 2945 m up |
| 18 | canvas-ux smoke | **FAIL** — 2 assertions (picking an event spine; the focus LOCKED chip) |
| 19 | city-command smoke | **FAIL** — 1 assertion (the stolen vehicle's dossier marker not on the canvas) |
| 20 | ops live smoke + the integration agent | all checks passed; 10 passed, 0 failed |
| 21 | `screens-verify.mjs` | 40/40 |
| 22 | leak check over every log | clean — no session secret, ion token or operator PIN anywhere |

**What failed, why, and what fixed it.**
- **Step 8 — the tier-0 gate.** The decider's pre-movement route options
  were the oracle's per-request route ids, so identical plans never
  shared a cache key (§3). Fixed in the brain — options labelled by their
  own segment sequence, plus the pre-movement own-doc filing; the
  convoy-brain suite went 584 → 634 passed — redeployed at 01:20 IST
  (`veer_up.sh --build`, READY 11/11, no migration to apply) and **re-run
  green at 01:21–01:23**: 1 hit in 3 identical plans (+1825 ms saved),
  the different-POI plan a miss, the wiring and the drift panel green.
- **Steps 18–19 — acceptance debris.** Both failures traced to five
  TEST-labelled smoke events still in planning and the old pre-rule
  pursuit `4508d86d` shifting City Command's action frame: the canvas-ux
  smoke had picked that old pursuit as its target and found no spine of
  it on screen, and the city-command smoke missed the stolen vehicle's
  dossier marker. Once the pre-judging reset had cancelled them
  (00:28, `demo_reset.sh` above) both **re-ran green**: the canvas-ux
  pick and focus group at 00:30 (it picked the staged sweep's spine, its
  card, the hover and the LOCKED chip) and the city-command smoke at
  00:32 (the marker opened the stolen vehicle's dossier: 12 sightings at
  6 cameras, the recommendations, the linked pursuit, 39 audit rows).
  Those re-runs used the 23:33 build; the 01:20 redeploy added the tier-0
  fix and the canvas's `SUPERSEDED · STOLEN` pin (§16.3).

After the ladder: the pre-judging reset (00:28, and the final run at
01:23 that passed every check and verified the judged state;
`demo_reset.sh` above) and
the hero shots recaptured on that staged state at 01:25–01:26 (the
flagship table below). Logs of every step are kept with the orchestration
record; the journeys' and the reset's own artifacts are in
`$VEER_LOG_DIR`.

### Flagship showcase gate (`scripts/showcase_run.sh`)

The city-platform end-to-end proof — **"God's Eye Morning"**: the flagship
composite `rath_yatra_monsoon_ambulance` (Rath Yatra + monsoon burst + one
108 ambulance, `docs/USE_CASES.md`) **plus the emulated stolen-vehicle
ANPR sweep**, injected through the emulator's real HTTP API into the
**running** stack. **Run live on the deployed stack three times on the
night of 2026-10-23/24 — exit 0, SHOWCASE PASS each time**: alone at
23:51–23:55 IST (acceptance step 12: flagship run 23, sweep run 24, its
scripted pursuit `33ed536b`), then twice under the repetition gate (step
13: runs 25/26 and 27/28), whose two rounds left identical canonical end
states. **The artifacts on disk are the gate's last round — one run**:
`showcase/showcase_results.json` generated **2026-10-23T18:31:51Z**
(00:01 IST on 2026-10-24), `passed: true`, no failed assertion;
`pursuit_chain.json` is the same run (the sighting's approval audit row
18:31:27Z). The gate restored the store after each round, so that run's
own rows (flagship event `c7073ae6`, pursuit `38423ac5`) are no longer in
the live store, while the step-12 run's are (flagship `dd43b362`, sweep
`29e3030d`, its scripted pursuit `33ed536b` closed at run end). Every
approval in these runs was made unattended by the script and is recorded
as **`Scripted demo officer (showcase script)`**; in the judged demo a
signed-in human approves on `/ops`.

| Assertion | Result (the artifacts' run, 2026-10-24 00:00 IST) |
|---|---|
| Fleet + models + emulator health | phase 1: the deck serving the cockpit, the emulator wired, the 8 containers, the models `:8083/:8084/:8085` — all green; the operator gate on, the script signed in as `Showcase run (automated check)` |
| Injections | flagship run 27 (`rath_yatra_monsoon_ambulance`, seed 202506271, clock ×30): **761/761 frames**, 0 failed / 0 skipped / 0 late; event row `procession`, `protection_class=none`, 1 phase · ANPR sweep run 28 (`watchlist_anpr_sweep`, seed pinned 20261105, clock ×60): **583/583 frames**, its precondition the FIR earmark (already active) |
| Audit trail | **31 decision rows this run** (25 situation + 6 parameter overrides); 2 `AMBULANCE_DELAY` parameter decisions — the 108 escalation decided mid-chaos (TAKEOVER, reason `AMBULANCE_DELAY_SEVERE`, handoff operator) |
| Adaptive response | 1 band transition this run (AMBER → RED at situation row n=8, `none:CONGESTED:RAIN:HIGH`, `band_easing.json`); band ledger AMBER 7 / RED 18; once the flagship event completed the engine released it and the city band read GREEN (`showcase_results.json` `band_transitions`) |
| Engine's closure answer | `signal_hold`, 45 s advance — "regulated, not stopped" |
| Green corridor | **4 `emergency_vehicle` windows** in the audited twin schedule (Relief Road stretch → Nehru Bridge crossing → Riverfront West → Ashram South handover) — the scenario's scripted 108 crossings (forced events of `rath_yatra_monsoon_ambulance`, `simulation/scenarios.py`), i.e. causes the twin schedules and charges closure/queue delay, not windows the engine granted; 12-replication twin (seed 202506271) ambulance-delay mean **231.92 s** (model output, `twin_audit.json`) |
| Pursuit chain (enforcement family) | GJ27EM9001 earmarked (`lawful_basis=stolen_vehicle_fir`, FIR 4401/2026 Kalupur PS, retention 30 d) → **6 emulated ANPR reads this run** (EM-CAM-01..04, EM-J01B, EM-J05 at 95–115 km/h, `source=emulated_anpr`, `matched_plate_hit=stolen_vehicle`; 17 reads of the plate in all) → **`R_STOLEN_SIGHTING` ×6, evidence only** — 0 penal recommendations; the penal rules the reads would have fired are withheld, none filed (the script's re-evaluation of the 6 reads: `R_SPD_SEGMENT` ×6, `R_SIGNAL_VIOLATION` ×2 — the red-phase rule is a wall-clock lottery the chain never asserts; the chosen row's own evidence, written as the read landed, lists `R_SPD_SEGMENT` and `R_CLOSED_CORRIDOR_ENTRY`, `pursuit_chain.json` `chosen.withheld`) → the newest sighting ("stolen vehicle (FIR 4401/2026) sighted at EM-CAM-03 — e-challan withheld, vehicle reported stolen") **approved by `Scripted demo officer (showcase script)`**: "pursuit authorised on a reported-stolen vehicle (evidence, no fine)" (append-only audit tail) → the C-N probe: approving a pending `R_SPD_SEGMENT` of the plate (filed on 2026-10-18 under that day's FIR earmark, before the stolen-vehicle rule existed) answered **409 "e-challan withheld — vehicle reported stolen"**, the row still pending, nothing written → operator-opened `vehicle_pursuit` event `38423ac5` (metadata vehicle_id + the approved sighting's recommendation_id + FIR + opened_by, merged into `pursuit_chain.json` from `events.corridor_events` — the POST response echoes metadata as `{}`) → **10 choke junctions ranked** by pgRouting off the latest sighting (top: junction 15 "Kalupur (Relief Road - Station Road)", unsignalised, score 0.008683, graph cost 79.6 s) → a 12-position trace on the vehicle-traces layer (`source=emulated_anpr`; its 24 h window, 23:53:58 → 00:00:55 IST, held this run's 6 reads and the 6 of the step-12 run's sweep 24 — the restore removed round 1's) → phase 8c closed the scripted pursuit (planning → cancelled, C-F): its approval is spent — one approval, one pursuit — and never stands in for the judged AUTHORISE. No fine anywhere in the chain |
| Jan Rakshak dispatch | JR-EM-01 to the market spillover behind the Swami Vivekananda Road barricades (segment 1013, waypoint junction 5): requested → approved → en_route → on_scene, every transition audited (each step taken by the scripted officer); the unit projected deterministically (`emulated_dispatch_projection`) — the script walks the statuses in seconds, so at `on_scene` its projected unit had not yet left its post (progress ≈ 0, ETA 227.8 s, `arrived: false`, `jan_rakshak.json`): the status is the officer's report, the position the projection's (journey J2 shows a unit moving) |
| Signal-policy guard (C7) | band AMBER → a plan for 11 junctions proposed → the operator apply returned **409 `shadow_eval_failed_closed`** (`twin_not_calibrated`): the twin's calibration replay is **`no_data`** — 0 field hourly aggregates in the last 72 h (`traffic.hourly_aggregates` holds REAL field observations only, and the store has none: the feed is synthetic) — fail-closed by design; 0 committed writes; the refusal is the oracle's last-plan answer in `GET /api/v1/signals/plans` (held in the process, so the run kept it in `signal_policy.json` `last_plan_auditable` — only a committed apply writes `corridor.signal_plan_log`, and the oracle was recreated after the run); the writer was switched on for the gate's rounds only and put back to its pre-gate value afterwards (verified) |
| Citizen advisory | the active-movements feed reflected the public event (no protectee); the public `/citizen` page showed no pursuit and no plate |
| Interface evidence | each run proves its own shots non-blank (SwiftShader WebGL; every saved PNG reload-decoded, ≥ 8 colours; the two `/gods-eye` shots also by a live-canvas readback). **The four hero shots on disk were recaptured at 01:25–01:26 IST on 2026-10-24**, on the clean, staged state the final pre-judging reset left, at 1440×900 — the three operator shots on the photoreal 3D city (`imagery=photoreal`, first attempt), the citizen page on its keyless Daylight map: the 3D city at rest — city band GREEN, the flow network draped in the simulated feed's colours, 110 entities (PNG 10,515 colours, `shots_city.log`); the pursuit trace — the camera on the stolen vehicle's trace centroid as the trace stood at capture (a 13-point trace, `shots_manifest.json`: the 23:51 run's 6 reads and the staging sweeps' 7), its pins reading `STOLEN · NO FINE · APPROVED` (the 23:51 run's scripted approval `c3431600`) and, on the e-challan a rehearsal script approved on 2026-10-18 under a cast officer name before the stolen-vehicle rule, `SUPERSEDED · STOLEN` (PNG 10,031 colours); the public citizen advisory — the deck's public projection, the same for every visitor: no pursuit, no plate, 0 event cards (the one live event, the staged sweep, is operator-only; PNG 2,060 colours); the `/ops` wall — band GREEN, 1 pending decision, 2 Decision Queue cards (the stolen-vehicle card with AUTHORISE PURSUIT and its pursuit card, no basis yet; PNG 7,268 colours, `shots_ops.log`). On the city and pursuit shots the fusion rail's newest human-decision rows are the 01:23 reset's, under its label `Scripted demo officer (demo reset)`. Per-surface screen checks: §2.7's screens camera, **40/40** on the live deck |

**The hero-shot camera** (`scripts/showcase_canvas_shots.mjs` — a camera,
not a test: it signs in once through the shared helper, clicks nothing
and writes nothing). On an ion-keyed deck every `/gods-eye` and `/ops`
shot waits for the photoreal tiles to load (`initialTilesLoaded`), tells
a keyed page that fell back to keyless imagery from a keyless deck,
retries in a fresh page up to three times (one `api.cesium.com` edge
refuses TLS from this host) and lets the draped lines settle 20 s before
the frame; the city shot turns the `photoreal-3d` layer on in its share
hash and includes the flow layer, the pursuit shot frames the trace
without it, and the citizen shot opens `/citizen` on a page that sets no
session cookie. That page still shares the browser's cookie jar with the
signed-in pages, so the camera's operator cookie was present (a
cookie-less capture needs a fresh browser context — the script's own
header claims "WITHOUT the session"); `/citizen` reads only the deck's
public projection (`GET /api/public/events`) and the public road
geometry, so the picture is the one every visitor gets. `--city-only` and `--rest-only` split the shots across the
showcase's phases; **`--ops-only`** takes just the signed-in `/ops` wall
and merges its record into `shots_manifest.json` instead of replacing the
city and pursuit evidence. The shot taken apart from the run frames the
trace as it stands then, so the docs quote both: the run's 12 positions
and the hero shot's 13 points.

Every decision, band transition and escalation above was produced by the
real stack responding to emulator-injected causes (the green-corridor
windows are such causes — the scenario's scripted crossings as the twin
schedules them); every
pending→approved transition was the showcase script's, under the honest
label `Scripted demo officer (showcase script)` (§2.9,
`docs/GODS_EYE_INTEGRATION.md` §3) — the emulated/real split stated
plainly, covering ANPR reads and dispatch projections too. In the judged
demo a signed-in human approves live on `/ops`, and `demo_reset.sh`
stops at PENDING. **History, kept:** the artifacts before this run were a
2026-10-18 run on the schematic network of the time and before the
stolen-vehicle rule — its stolen-plate reads filed a penal e-challan that
the script approved under a cast officer name ("Duty Officer R. Mehta
(God's Eye Morning)"), the fine C-N now refuses. That approval
(`e91d4591`) stays in the store as superseded audit history — decisions
are final — and on the canvas its pin reads `SUPERSEDED · STOLEN`
(§16.3); its pursuit `4508d86d` was cancelled by the pre-judging reset.
That run's twin-replay figure came from seed hourly aggregates since
found fabricated and deleted (C-P, §16.5); the replay is an honest
`no_data` today.

---

## 14. Security Model

### VVIP Security Classification

| Class | Min Lanes | Closure Type | Advance-Closure Floor | Max Queue |
|---|---|---|---|---|
| Z+ | 6 | Full closure | 180 s | 2000 m |
| Z | 4 | Partial closure | 120 s | 1000 m |
| Y | 2 | Speed restriction + signal priority | 60 s | 500 m |
| X | none | Signal priority only | none — no closure (0 s) | 0 m |

`ADVANCE_CLOSURE_FLOOR_SEC = {"Z+": 180, "Z": 120, "Y": 60, "X": None}`
(`protocol_params_gen.py`, generated from `shared/schemas/protocol_params.json` into both services); the adaptive tier may raise closure windows
per situation but never below the floor, and X-class movements keep the
closure window at exactly zero.

### Road Class Security Scores

```python
ROAD_CLASS_SCORES = {
    "motorway": 100, "trunk": 85, "primary": 70,
    "secondary": 50, "tertiary": 30, "residential": 10
}
```

### Outbound HTTP safety (SSRF guard — `convoy_brain/http_guard.py`)

Two policies, both enforced before any request is made:

1. **Platform service endpoints** (:8083/:8084/:8085 and the oracle :8091) are fixed
   configuration constants resolved from env / `veer_models.yaml` /
   defaults. `assert_platform_service_url` accepts plain http(s) on
   loopback or `host.docker.internal` only — nothing else can sneak in via
   environment overrides.
2. **Externally supplied URLs** (from user input, tool output, or another
   service's payload) are **never fetched** by this stack today; any future
   outbound client must pass them through `validate_external_url`, which
   rejects non-http(s) schemes and every loopback / private / reserved /
   link-local / multicast address before the request is made.

model-host makes **no outbound HTTP calls** at all.

### Access Control

- VVIP class information is **SENSITIVE** — movement destinations and VVIP identities are never logged; a protected movement's route is withheld from the public feed until it is inside the 2-hour horizon
- Every service except the deck binds **loopback** (oracle :8091, brain :8092, emulator :8090, PostGIS :56434, Valkey :6380, NATS :4223, the model hosts); the deck (`0.0.0.0:5273`) is the only LAN-facing service and reaches the others through its loopback-pinned proxy (authority pinning refuses a protocol-relative path that would turn it into an open relay)
- **Operator PIN gate** on the deck (`VEER_OPERATOR_PIN`, §16.11): without a signed-in officer every proxied API is default-denied (403); only the road geometry, three liveness probes and the 108 driver's ambulance request pass anonymously (that request capped per client, 429 beyond)
- **Officer attribution**: the deck forwards the signed-in officer as `x-veer-officer` and strips any client copy; the oracle and the brain honour it only on a loopback connection (a repeated header is a 422) — it is the actor of every lifecycle, approval, dispatch and route write
- **No credentials in source**: `veer_models.yaml` carries none; the operator PIN, the session secret and the Cesium ion token live only in `~/.config/veer/veer.env` (mode 600) and are read at run time — never printed or logged

### Enforcement rails (contract C6 + C-N), enforced in schema and code

1. **Vehicles only — via ANPR plates, never faces or persons.** No person-directed field exists on any path: camera SITES carry location and identity only (no frame, image or stream column, migration 012); sightings are plate + kinematics; agencies dispatch units, never people.
2. **Every penalty waits for a named officer's approval.** Recommendations are born PENDING — no code path creates an approved row; the only transition is a named officer's decision, with an append-only audit tail. In operation that officer is a signed-in human; the live store's one approved e-challan (`e91d4591`, 2026-10-18) was a rehearsal script's under a cast officer name — superseded, never a pursuit basis (§16.3).
3. **No fine, ever, for a reported-stolen vehicle's owner (C-N).** Under an active `stolen_vehicle_fir` earmark a read files ONE evidence-only `R_STOLEN_SIGHTING` (the penal rules it would have fired recorded as withheld); approving any penal rule on that vehicle answers **409** `e-challan withheld — vehicle reported stolen` (rejecting stays allowed); approving the sighting is the ONLY authorised basis of a pursuit — opening one from an approved e-challan of that vehicle answers **422** (§16.3) — and one officer approval authorises ONE pursuit: a second pursuit from the same approval answers **422** too.
4. **Lawful basis and retention.** An earmark needs an enumerated lawful basis (MV Act 1988 s.177/s.183/s.207, BNSS 2023 s.94, `stolen_vehicle_fir`) and a server-computed retention capped at 90 days; sightings without an active earmark are kept 30 days and reaped in the read path (`GET /api/v1/vehicles/retention`).
5. **One lawful-read rule (C6 parity).** A single camera read is shown only when its vehicle holds an active earmark or the read is evidence for a recommendation (`city/common.py` `SQL_LAWFUL_READ`, composed by `/sightings`, `/trace`, the feed, the camera hit list, the dossier and the interception plan); anything else is a count-free 404.
6. **Plates masked unless a named officer is signed in.** Every surface shows `GJ27EM•••1` until a named duty officer is on console — the oracle masks every entity label and feed text it sends (the raw plate rides only in data fields — an entity's `props.plate`, the enforcement listing's `plate`, an e-challan's own recommendation text — which the deck's proxy serves only to a signed-in officer while the operator gate is on, as it is on the argus stack), and the deck masks every plate it renders, from the API or from its own data, unless a named officer is signed in (`lib/gods-eye/plates.ts`).
7. **Emulated and simulated sources are labelled** at the row level (`emulated_anpr`, `emulated_dispatch_projection`, `emulated_patrol`, `emulated_gps`, `data_quality: synthetic`) and on every surface (EMULATED / SIMULATED FEED); nothing writes `live_anpr` in this build.
8. **Veer strengthens the state's programmes.** Camera sites are designed to integrate the state's existing ICCC / CCTV / ITMS inventory (`corridor.cameras.source = registered`), and every authority — penalties, pursuits, escort route switches, dispatches — stays with a named human officer.

---

## 15. Configuration Reference

### Single source of model-stack truth — `models-config/configs/veer_models.yaml` (C0)

Owned by the models stream; read by every stream. Structure:

```yaml
services:                      # fixed endpoints; env-overridable
  model_host: http://127.0.0.1:8083     # env MODEL_HOST_URL
  llama:      http://127.0.0.1:8084    # env LLAMA_BASE_URL
  embedding:  http://127.0.0.1:8085    # env EMBEDDING_SERVICE_URL
decider:                      # snapshot path, weights bytes, offline mode,
                              # ≤6 questions/request, state caps 1500/400/8192,
                              # queue 8, ≥5 s checkpoints, p95 3000 ms degrade
llama:                        # server bin (build 69bf643), gguf path, 2×8192
                              # slots, jinja template, metrics, sampling defaults
ollama:                       # debug side-load only (:11434 external, untouched)
embeddings:                   # multivector 128-d + dense 1024-d, aux disabled,
                              # venv pins (torch 2.14.0 …)
memory_budget_gib:            # planned GB10 budget (see §10)
```

### Deployment environment (`~/.config/veer/veer.env`, read by the compose interpolation and the systemd units)

| Variable | Default on the argus stack | Effect |
|---|---|---|
| `VEER_OPERATOR_PIN` / `VEER_SESSION_SECRET` | set (gate ON) | The deck's operator gate and its session signing key (§16.11); `VEER_SESSION_SECURE=1` only behind TLS |
| `VEER_CESIUM_ION_TOKEN` | set on this box | Signed-in operators get the ion upgrade — Cesium World Terrain + Bing aerial + Google Photorealistic 3D Tiles (§16.9); empty = keyless everywhere |
| `VVIP_ESCORT_APPROVAL_TIMEOUT_SEC` | 90 (clamped 15–600) | How long an escort proposal waits for a named officer before it expires and the current plan is held (§16.2) |
| `VVIP_REQUIRE_OFFICER` | 1 on the brain | Escort start / stop / clear must name the duty officer (422 otherwise; a route-switch approval always must). The oracle reads the same flag for earmarks, earmark clears and signal-plan applies / restores, but its compose service does not set it — on the gated deck the verified `x-veer-officer` names the actor anyway |
| `VVIP_ESCORT_SIM_GPS` | 1 | No GPS hardware feeds the deployment: an escort walks its approved route on EMULATED GPS (`emulated_gps`), and stops the moment real fixes arrive |
| `VVIP_CONFIDENCE_FLOOR` | 0.45 | The decider confidence gate (§3) |
| `VVIP_SIGNAL_POLICY_WRITER` | empty (OFF) | The city signal-plan writer; `veer_up.sh --demo` sets 1 — every apply still needs a calibrated twin |
| `EMULATOR_LIVING_CITY` / `EMULATOR_LIVING_VIOLATORS` / `EMULATOR_CITY_SEED` | 0 / 1 / 1411 | The ambient living city (off), its deliberate violators (each a PENDING recommendation), its day-program seed |
| `VVIP_ACCEPT_LIVE_ANPR` | unset | `live_anpr` reads are refused until a real vendor adapter exists |
| `DECIDER_TIER0_DISABLE` | unset | Kill switch of the decider tier-0 cache (§3) |
| `VEER_DEMO` | 0 | Persisted demo mode (`veer_up.sh --demo` / `--no-demo`) |

### Route Scoring Weights (baseline; adaptive may reweight per movement)

```python
# protocol_params_gen.py (generated from shared/schemas/protocol_params.json)
BASELINE_SCORING_WEIGHTS = {"time": 0.3, "disruption": 0.4,
                            "security": 0.2, "complexity": 0.1}
```

### Traffic flow (from `models-config/configs/flow_forecaster.yaml`)

Horizons [5, 10, 15, 30] min · lookback 60 min · ONNX opset 18 ·
target VRAM 409 MB · CUDA→CPU provider fallback.

### Valkey Configuration

```
Max Memory:      512 MB (system RAM)
Eviction Policy: allkeys-lfu
Key TTLs:        latest traffic 300s, corridor summary 60s, forecasts 60s,
                 adaptive params 30s, situation:changed 60s
```

### Memory Guard

```python
MEMORY_GUARD_MB = 3500  # Max RSS for traffic-oracle process
```

### Shared Protocol Definitions

| File | Format | Purpose |
|---|---|---|
| `shared/proto/corridor.proto` | Protobuf | Road network messages |
| `shared/proto/traffic_signal.proto` | Protobuf | Signal timing messages |
| `shared/proto/convoy_event.proto` | Protobuf | Event stream messages |
| `shared/schemas/arrow/traffic_snapshot.fbs` | FlatBuffers | Arrow IPC traffic snapshot |

## 16. Waves 3–4: real roads, human authority and the contracts behind them

The wave-3 contracts (C-K … C-S, frozen and additive) and the two
user-decided packages that close the gap between the demo and the city:
the road network on real OpenStreetMap roads (WP-20) and escort authority
(WP-21), plus the God's-eye in 3D (WP-22). Every figure below was measured
on a fresh install of this tree (migrations 001–020 and the bring-up's
seed order) or read from the code it names; the few measured on a
converted copy of the live store, or on a keyed, signed-in preview of
the merged deck, say so.

### 16.1 The road network on real OpenStreetMap roads (WP-20, migration 020)

**What it is.** The 49 segments (ids **1001–1049**) of the city network
each run along the **real road of its name between two real
intersections**, and each of the **34 junctions** sits on its
OpenStreetMap node (`junctions.osm_node_id`, 34/34). The network has
**198** turning movements (`corridor.segment_adjacency`), **186**
`road_graph` edges and is **one strongly connected component**; the
store measures **191.76 km** (≈192 km: the sum of PostGIS
`ST_Length(geom::geography)` — the spheroid — over
`corridor.road_segments` on a fresh install; the generator's spherical
haversine sum, `scripts/data/osm_network.json` `total_km`, reads 192.04 km,
the same as `ST_Length(geom::geography, false)`). Classes: primary 21, trunk 12,
secondary 11, tertiary 5; two one-ways (1012 Gandhi Road, 1013 Swami
Vivekananda Road). On it: 11 signal-controlled junctions with 11 signal
programs, 28 EMULATED camera sites (24 ANPR + 4 CCTV), 18 agency stations
(police 8 · ambulance 4 · Jan Rakshak 3 · fire 2 · traffic 1).

**How it is made — deterministic and re-checkable.**
`scripts/gen_osm_network.py` reads the committed Overpass extracts
`scripts/data/osm/{city,walled_city}.overpass.json.gz` (osm_base
2026-10-23T08:08:01Z and 08:34:36Z) and the reviewable junction/segment
mapping `scripts/data/osm_network_map.json`, traces each segment along the
named OSM ways between its two junction nodes and writes
`services/corridor-store/seed/sample_corridor.sql` (fresh installs),
`reanchor_osm_network.sql` (converts an existing store in place — ids
preserved, idempotent, a second apply is a no-op — and keeps its EMULATED
history on the real roads: the camera reads and their copies in the runs'
recorded tapes, the use-case profiles' seeded spines and text; applied by
the bring-up whenever the store is off this network, §11) and the
evidence file `scripts/data/osm_network.json`. On a converted copy of the
live store every emulated read sits ≤ 0.001 m from its segment (up to
435 m before) and a recorded sweep tape's 32 reads ≤ 0.006 m, with every
decision and audit table unchanged. `gen_osm_network.py --verify` re-measures
everything from the committed snapshot with no network access: every
segment within **15 m** Hausdorff of its OSM centreline (worst measured
**7.76 m**), ending exactly at its junctions (49/49), length within
**2 %** (worst **0.96 %**), one strongly connected network, every
junction on its node, outputs byte-identical to a regeneration — exit 0
on this tree. Downstream data were regenerated from the same network: the
ingress table (`segments_ahmedabad.rs`, 37 endpoint nodes, §2.1), camera
sites, stations, junction names and signal programs
(`post_seed_derivations.sql`), the emulator library's route chains, the
twin's scenario library (`scripts/scenario_library.json`: 23 scenarios,
49 segments, 192.04 km on the generator's spherical basis) and the POI route matrix
(`scripts/data/route_acceptance_pairs.json`: **182/182** pairs route, no
far snap; every Z+ plan on this network is honestly `conditional` — no
route meets the Z+ lane floor end to end). The seed's fabricated
"real" hourly aggregates were removed (C-P, §16.5).

**Migration 020** records the provenance of the ROUTING geometry:
`road_segments.geom_source` (`osm` on all 49; `schematic` = a hand-placed
line; NULL = not recorded, treated as schematic), `geom_meta` (the OSM way
ids in path order, the `osm_base` stamp, the OSM names along the path and
the validation metrics) and `junctions.osm_node_id`. The oracle serves it
as `geometry_basis: osm` (§16.8) — the deck draws such a line SOLID.

**Real names.** Every segment now carries the name of the road it is
drawn on — among them **1013 Swami Vivekananda Road**,
**1031 Kasturba Gandhi Road**, **1010 SH71 (Bhat–Motera Road)**,
1003/1004 CG Road (South/North), 1009 Ashram Road (Sabarmati Ashram),
1023 Ambli Bopal Road, 1029 Domestic Airport Road (Hansol),
1036 Ashram Road (Paldi), 1039 Swami Vivekananda Bridge (Ellis Bridge),
1040 SG Highway (Sarkhej) — and the former connectors 1041–1049 are now
the real roads and bridges they stood for (e.g. 1041 Chimanbhai Patel
Flyover, 1048 Indira Bridge, 1049 Sarangpur Road). 1010's four lanes are the seed's own
value (OSM tags none); 1043 and 1049 carry naming notes in the evidence
file.

**Attribution.** Road geometry © OpenStreetMap contributors, ODbL 1.0 —
in the seed file headers, `services/corridor-store/seed/NOTICE.md`, the
God's-eye `NOTICE.md`, the ingress table header and `IMPACT_REPORT.md`.

### 16.2 Escort authority — "officer approves every switch" (WP-21, the human-veto guarantee)

User decision, 2026-10-23: during a live escort the AI decider **only
proposes**. Every answer that would change the plan — a hold or a held
departure, Plan B (the pre-sanitised alternate spine), an emergency
re-route (a deterministic solver call that avoids the hot segments and
passes the class lane floor) and the closures a new route brings —
becomes a **proposal**, whatever its confidence. Only "continue
monitoring" / "proceed" (no change) is carried out on its own
(`workflows/live_escort.py`, `PLAN_CHANGING_ACTIONS`). **The decider never
switches a route by itself.**

- **One open proposal per escort**, computed in full when raised — the
  exact route, the closures it adds and lifts, km left before and after,
  the decider tier and confidence. A newer proposal supersedes the older
  (audited). Fields (`_approval_view`): `approval_id`, `action`, `kind`
  (`plan_b` | `reroute` | `hold`), `summary` (naming the roads new on the
  route), `reason`, `raised_at` / `created_at`, `expires_at`,
  `timeout_sec`, `status`, `trigger`, `tier`, `confidence`, `segment_ids`,
  plus `proposed`, `peak_congestion`, `detour_m`, `remaining_m`,
  `detour_segment_ids`, `data_quality` / `simulated` (SIMULATED FEED when
  the numbers came from the synthetic feed) and `checkpoint_tier`. A
  proposal raised by the fixed rule because no model answered is labelled
  a rule (`tier deterministic`, "RULE PROPOSES"), never AI. It is persisted
  as the `decisions_log` row `operator_approval_required` and published on
  the C-J status document as `pending_approval`.
- **The officer answers**: `POST /movements/{id}/escort/approvals/{approval_id}`
  `{decision: approve|reject, officer?}` — the deck's verified
  `x-veer-officer` is the deciding officer. **200** decided · **404**
  unknown movement or `approval_id` · **409** expired, superseded or
  already decided · **422** a malformed body or no officer. An approval
  applies exactly what the officer saw (the route switch, its C-O spine
  update, the closures); if the convoy has rolled past the route's branch
  point it is re-proposed instead of improvised. A rejection is recorded
  (`operator_rejected`), the convoy keeps its plan.
- **Timeout holds the plan.** With no answer in
  `VVIP_ESCORT_APPROVAL_TIMEOUT_SEC` (default **90 s**, clamped
  **15–600**, `config.py`) the proposal **expires** — "no officer
  response — current plan held": the convoy keeps executing its current
  approved route and closures; it does not stop and nothing switches. A
  proposal whose escort is no longer running expires (409) rather than
  being approved with nothing to carry it out.
- **Every console sees it**: `GET /escorts` lists running and just-ended
  escorts with their open proposal and recent outcomes and publishes
  `"authority": "officer_approves_every_switch"`; the deck shows it in the
  escort HUD ("AI PROPOSES … — APPROVE / REJECT · expires in mm:ss"), in the
  `/ops` Decision Queue's escort lane and in the pending-decisions count,
  in EN/HI/GU.

### 16.3 Stolen-vehicle suppression (C-N, WP-19 + WP-21, migration 017)

A vehicle whose ACTIVE earmark carries the lawful basis
`stolen_vehicle_fir` is the **victim's** car: a camera read of it is
evidence for the investigating officer, **never an e-challan to its
registered owner**. Under such an earmark the deterministic rules persist
ONE `R_STOLEN_SIGHTING` per read — the read plus the penal rules it would
have fired, recorded as withheld, and the FIR reference only — instead of
`R_SPD_SEGMENT` / `R_SIGNAL_VIOLATION` / `R_CLOSED_CORRIDOR_ENTRY`.
Approving any PENAL recommendation of that vehicle — including one filed
before the earmark — answers **409** `{detail: 'e-challan withheld —
vehicle reported stolen', reason: 'stolen_vehicle_fir'}`; rejecting stays
allowed. A named officer still decides every `R_STOLEN_SIGHTING` (approve
= the authorised basis of a pursuit, reject = dismissed) — nothing is
auto-approved (a signed-in human in the judged demo; an unattended
script's decisions carry its own label, `Scripted demo officer (showcase
script)` / `(demo reset)`). A `vehicle_pursuit` event is accepted only when its
metadata names a vehicle with an active earmark and a recommendation for
it approved by a named officer — and **for a reported-stolen vehicle that
recommendation is its approved `R_STOLEN_SIGHTING`, never an e-challan**:
the oracle refuses (**422**) to open a pursuit of a vehicle currently
reported stolen from an approved PENAL recommendation — even one approved
before the theft was reported — "a pursuit of a vehicle reported stolen is
authorised only by its R_STOLEN_SIGHTING, approved by a human officer
(C-N)" (`events/__init__.py` `_validate_pursuit`), and the `/ops` pursuit
card offers only the sighting as the basis. (The WP-15 rehearsal on a
live-store clone found an old approved e-challan still offered as a
pursuit basis; the rule above closed that gap, and the pre-judging reset
cancels the old fine-based pursuit.)

**One officer approval authorises ONE pursuit.** An approval that already
authorised a pursuit — open, completed or cancelled — is spent: a second
pursuit opened from it answers **422** "recommendation X already
authorised pursuit Y — one officer approval authorises one pursuit;
approve a newer sighting to open another (C6)". `GET
/api/v1/enforcement/recommendations` carries the link on every row —
`pursuit: {event_id, status}` (the newest pursuit opened from it) or
`null` — so a reader can tell a spent approval from a live one (verified
on the live-store clone: the old approved e-challan `e91d4591` lists its
pursuit `4508d86d`, `planning`). `/ops` follows the same rule: the
stolen-vehicle card and the pursuit card never stand an approval spent on
a closed pursuit in for the officer's next authorisation (`client.ts`
`approvalSpent`, used by `DecisionQueue.svelte` and `store.svelte.ts`
`pursuitCandidates`; an approval behind a still-open pursuit keeps
counting, and that pursuit is on the board). So the showcase's scripted
approval, whose pursuit the showcase closes, never pre-empts the human's
AUTHORISE PURSUIT in the judged demo; `scripts/demo_reset.sh` mirrors the
rule when it verifies the `/ops` offer (§13). The
emulator's stolen-vehicle sweep ensures the
FIR earmark as an explicit, labelled scenario precondition before its
reads, so its reads are evidence-only too. The city entity and feed say
"e-challan withheld"; a penal row approved before the rule existed is
flagged "vehicle reported stolen", never relabelled.

**On the canvas** (`layers/enforcement.ts`) the stolen-vehicle rows read
as what they are: a sighting's pin says `STOLEN · NO FINE` with its state
(`PENDING`, `APPROVED`); a PENDING penal row of a vehicle reported stolen
says `WITHHELD · STOLEN`; and an e-challan **APPROVED before the
stolen-vehicle rule** on a vehicle now reported stolen says
**`SUPERSEDED · STOLEN`** — a dim pin whose pick card says it was
approved by its officer before the rule, is superseded, is kept as audit
history (decisions are final), is never a pursuit basis, and that no fine
is approved on a reported-stolen vehicle (409); the layer's coverage hint
and detail count superseded pins apart. **On the live stack, after the
reset (2026-10-24):** the stolen plate holds two approvals — the
2026-10-18 e-challan `e91d4591` (shown `SUPERSEDED · STOLEN`; its pursuit
`4508d86d` cancelled through C-F) and the first live showcase run's
sighting `c3431600` (`STOLEN · NO FINE · APPROVED`; its scripted pursuit
`33ed536b` cancelled at run end, so the approval is spent) — and `/ops`
offers AUTHORISE PURSUIT on the staged sweep's PENDING sighting; the
hero pursuit shot (§13) shows both pins.

### 16.4 Route updates (C-O, migration 018)

`POST /api/v1/events/{id}/routes` `{variant 'reroute-N' | 'plan_b',
segment_ids (1..64 corridor segment ids), make_primary (default true),
officer?, note?}` runs in ONE transaction: the event row is locked
(**409** unless planning / approved / active), the current primary is
**superseded** (`superseded_at`) — never deleted — the new spine becomes
the primary with its `variant` and `created_by`, and one
`event_status_log` row narrates the change ("Route updated" in the feed).
**422** for an unknown segment id or a malformed event id, **404** for an
unknown event, **503** before migration 018. Every reader of
`event_routes` (situation engine, city snapshot, dossier, e-challan
closure test, unit projection) reads CURRENT spines only, and the event
read model returns the rest as `route_history`. An approved escort
re-route lands here (§16.2).

### 16.5 Synthetic history kept apart (C-P, migration 019)

`traffic.hourly_aggregates` is **REAL-only**: it is the twin's field
calibration input, the pattern matcher's and the vector store's history.
The oracle's hourly maintenance rolls the simulated feed's
`data_quality = 'synthetic'` observations into their own table,
`traffic.hourly_aggregates_synthetic`, which `GET
/api/v1/traffic/historical` serves as `synthetic_aggregates` — every row
labelled `data_quality: synthetic` — NEXT TO (never merged into) the real
aggregates, with `provenance` and `basis` (`last_n_hours`, or a same-slot
baseline by IST day-of-week and hour over a 672 h look-back). Nothing that
calibrates, trains or fits may read the synthetic table (the oracle's
test suite fails if any calibration, twin, pattern or vector SQL names
it). A fresh install therefore holds **0** real aggregate rows (the
seed's fabricated ones were removed) and a growing synthetic rollup.

### 16.6 Provenance on every surface (C-Q)

Additive provenance fields so no surface can present simulated data as
field data: the city band's `city_band_since`, `city_band_simulated_feed`,
`city_band_stale`, `city_band_stale_since` on `/signals/state` and
`/signals/plans` (and an apply with no band while the band is unknown is
a 409); `simulated_feed` / `inputs_data_quality` on the situation log and
`/adaptive/status`; `data_quality` / `feed_source` on anomaly entities
(from the ingress `feed_quality`, §2.1); provenance on feed items, city
entities and the dossier (`geometry_basis` included); the C-H
`feed_quality` field itself. The deck badges each accordingly (EMULATED,
SIMULATED FEED, "provenance unverified").

### 16.7 Diversion fields (C-R)

`POST /api/v1/optimize/diversions` serializes, per closed segment, beside
the timing and queue estimates: `alt_segment_ids`, `detour_distance_m`,
`detour_found`, `detour_status` (`found` | `no_path` — a bridge with no
way round | `dead_end` | `impractical`), `detour_from_junction_id` /
`detour_to_junction_id`, `turnaround_segment_ids` (a U-turn on a
single-junction stub, shown), `entry_pinned` / `exit_pinned` (whether each
end of the detour was fixed by the route — False means it may cover only
part of the closed segment), `detour_limit_m` (max(5 km, 6 × the closed
length)), `detour_reason` (an impractical closure says to hold or meter
traffic instead) and `uses_route_segment_ids` (route segments the detour
runs over that are OPEN during this closure — a route segment closed at
the same time is never part of a detour). Detours are Dijkstra over
(segment, junction reached) states on the turn graph: one-ways are never
driven backwards and two neighbours at the same junction never pose as a
detour.

### 16.8 Display geometry and the spatial router (C-K, C-S, migration 015)

`GET /api/v1/spatial/segments(/{id})` (`spatial/router.py`, mounted ahead
of the inline handlers) answers each segment's drawn line as
`COALESCE(display_geom, geom)` with `geometry_basis`: **`osm`** — the
routing geom itself is the OSM road (migration 020) and no display line
overrides it; **`osm_display`** — a migration-015 trace follows the road
while routing stays on a hand-placed line; **`schematic`** — the
hand-placed line, drawn as is. On this tree all 49 segments answer `osm`
(no display overrides remain). The rule for every consumer: route, cost,
attribute and calibrate on `geom`; draw and animate on
`COALESCE(display_geom, geom)`. `length_m` stays the routing geodesic
length. The column probes are cached 300 s, so a pre-015 / pre-020 store
answers `schematic` instead of failing.

### 16.9 The God's-eye in 3D (C-L, WP-22)

**Signed-in operators** on a deck that carries `VEER_CESIUM_ION_TOKEN`
(this box) see real 3D Ahmedabad: **Google Photorealistic 3D Tiles**
(Cesium ion asset 2275207) over **Cesium World Terrain** with **Bing
aerial imagery** (ion asset 2). The token is read server-side by `GET
/gods-eye/mode` and handed only to a request carrying a valid operator
session while the gate is on — an anonymous LAN client, and every public
page, gets the **keyless** basemap: OSM raster + ellipsoid terrain + Veer's
own entities, zero cesium.com and zero Google calls (the answer is
`cache-control: no-store`, `vary: cookie`).

What WP-22 adds on top — merged into this tree and verified on a keyed,
signed-in preview of it (chip `PHOTOREAL 3D · GOOGLE`, `imageryMode`
`ion-photoreal`, every overlay layer 0 floating, the credits legible above
City Command's fusion rail, **0 CSP violations**, the ion token served
only to the signed-in operator) and **again on the live stack**
(acceptance step 17, 2026-10-24 00:08 IST, `gods-eye-3d-smoke.mjs`: the
same chip and mode; events, flow, traces, enforcement, signals, cameras,
anomalies and the pursuit overlay all 0 floating; 0 CSP violations and 0
page errors on `/gods-eye`, `/` and `/ops`; the Google, Cesium ion and
OpenStreetMap credits on screen — on `/` the whole credit bar clear of the
tool dock; no frame over 3 s under SwiftShader, median 567 ms):
- **The overlays stand on the city.** Lines — the congestion network,
  event spines (RECOMMENDED glow, dashed alternates), traces, dispatch
  arrows, the convoy track, the pursuit trace/fan — are draped
  (`clampToGround`, classification type BOTH: on terrain AND on the 3D
  tiles), ordered by `zIndex`; the markers, dots and labels of those
  layers and of the signal, unit, camera, anomaly and enforcement layers
  clamp to the terrain at street level with the depth test off, so no
  building hides them (`layers/draping.ts`). Every overlay layer reports a
  drape audit (draped / clamped / floating) in `layerStats`, and on the 3D
  city every one reads **0 floating** — the e-challan pins (clamped, fanned
  out per camera site) and the pursuit's trace, fan and chokes included.
  The selection halo is a draped glow ring (95 m, re-laid only when its
  subject has moved 20 m), the AI's agent notes are clamped labels, and
  the God's-eye REPLAY of a recorded run drapes its camera rings and
  clamps its tracks — nothing stands at a fixed height. Tile collision is
  off: WP-22 measured clamping to the tiles at 134 height queries in 30 s
  costing 12.3 s with 4.9 s frames under SwiftShader; with it off, zero
  tile queries and no frame over 400 ms — and, while the tiles stand, the
  camera keeps ≥ 80 m from the surface (`PHOTOREAL_MIN_ZOOM_M`), so a
  wheel zoom never enters the mesh. The tiles render at screen-space
  error 24 over the globe kept beneath them at 8 (the markers' ground); a
  10-minute orbit soak showed a stable heap with tiles plateauing at
  119 MB.
- **Honest imagery, an honest chip.** Operator pages admit
  `http://*.virtualearth.net` (img-src + connect-src) because Cesium asks
  Bing for tiles in the page's scheme and the demo deck is plain
  `http://127.0.0.1:5273`; public pages keep the strict CSP and never
  receive the token. The chip names what is on screen
  (`basemap.ts` `imageryChip()`, EN/HI/GU): **`PHOTOREAL 3D · GOOGLE`** once
  the 3D tiles cover the view (`PHOTOREAL 3D · LOADING` while they stream,
  `ION AERIAL · 3D TERRAIN` on the key without the 3D layer), and
  `imageryMode` reports `ion-photoreal` — also when the imagery under the
  mesh has fallen back, because Google's mesh is what is on screen (the
  canvas's note still names the fallback); the offline-schematic mode
  describes a flat dark globe and is never claimed while the 3D city
  renders. A 3D-tile load that fails for a transient reason (no answer, a
  dropped TLS handshake, a 5xx / 429) is retried at 5 / 20 / 65 / 130 s; a
  refusal (401/403/404) fails at once — the 3D layer reports itself off
  and the globe carries the view. When the ion IMAGERY fails or does not
  answer and no 3D tiles cover the view, the canvas falls back to the
  keyless OSM raster with the `OSM · DARK · FALLBACK` chip and the reason.
- **A judge-grade City Command.** On the ion key with the 3D layer on,
  City Command frames the live action obliquely at **−42°** (range capped
  at 4.4 km, so the camera stays at most ≈ 2.94 km up; measured on the
  merged tree at 2.3–2.7 km, and on the live stack at −42.0° with the
  camera 2945 m up) and, with nothing in action, flies to an oblique over the
  old city and the Sabarmati (`HOME_VIEW_3D`: −40°, the camera ≈ 2 km up).
  The wall views and the keyless canvas keep their framing.
- **Attribution** — Google, Cesium ion and OpenStreetMap credits on screen
  (`showCreditsOnScreen`, Google's terms), on a glass plate lifted into the
  region the host's panels leave clear: on City Command, above its fusion
  rail, fully legible. Tile features never open a card.
- **Geometry basis on the canvas**: `osm` and `osm_display` render solid;
  only `schematic` renders dashed — after WP-20 no dashed flow line remains.
- Known environment fault, stated: one `api.cesium.com` CDN edge refuses
  TLS from this host, so a browser session that resolves to it loses the
  ion imagery (honest FALLBACK note) while the 3D tiles retry and usually
  land.

### 16.10 Chat grounding, MCP and canvas directives (C-D2)

`/chat/stream` runs the same two-tier turn as `/chat` and streams
`thought` / `tool_call` / `tool_result` / `directive` / `token` / `done`;
`event_id` / `context_urn` ground a turn, `done.citations` names what it
read, and a map request is grounded first by one deterministic, read-only
`city_query_entities` call. A **directive** ("fly to the stolen vehicle")
is a proposal: the deck shows it as an "AI suggests" chip that is never
auto-applied; the operator's Apply or Dismiss is acknowledged once through
`POST /chat/directives/ack` and audited. `POST /mcp` exposes the 28 tools
of §7 — read-only city tools, compute tools and canvas verbs; no tool can
approve, decide, dispatch or earmark (R2).

### 16.11 The operator gate, officer attribution and plate masking

- **The PIN gate** (`src/lib/shell/operator-session.server.ts`, opt-in by
  `VEER_OPERATOR_PIN`, ON on the argus stack): `POST /api/session
  {officer, pin}` issues an HttpOnly, SameSite=Strict `veer_operator`
  cookie (HMAC-signed with `VEER_SESSION_SECRET`, 12 h);
  `DELETE /api/session` signs out and revokes the token server-side;
  `GET /api/session` says `{gate, officer, expires_at}`. Failed PINs are
  throttled at 8 per 5 minutes per client; comparisons are constant-time;
  officer names refuse control and bidi characters. Without the cookie,
  every proxied request (`/api/v1`, `/api/traffic`, `/api/convoy`,
  `/api/emulator`) is default-denied (403) on its canonical, once-decoded
  path, except the public road geometry, three liveness probes and the
  108 driver's ambulance request (anonymous, ambulance-only, capped per
  client). The public pages read the deck's own server-side projection
  (`/api/public/*`), so no raw operator feed is ever anonymous.
- **Officer attribution.** The deck forwards the signed-in officer as the
  percent-encoded `x-veer-officer` header on every proxied call and strips
  any client copy; the oracle and the brain honour it only on a loopback
  connection (sent exactly once — a repeated header is a 422). It is the
  actor of every lifecycle transition, e-challan or sighting decision,
  dispatch, earmark, route update and escort approval; a differing body
  `officer` is kept as `claimed_officer` (`corridor.dispatch_events.detail`,
  migration 014; the audit tails; the C-F note).
- **Plates masked unless a named officer is signed in** (C6): every
  canvas, dossier, feed and board shows `GJ27EM•••1` until a named duty
  officer is on console (§14).
- **Every gated tool signs in through ONE helper** —
  `scripts/deck_session.sh` (bash) / `scripts/lib/deck_session.mjs`
  (node): the PIN is read at run time from `VEER_OPERATOR_PIN`, the file
  named by `VEER_OPERATOR_PIN_FILE`, or `VEER_OPERATOR_PIN=` in veer.env,
  sent once per run on stdin, never printed; a gate that is off is
  detected and skipped.

### 16.12 Lifecycle, dispatch, C6 parity and data hygiene

- **Event lifecycle (C-F, migration 013)** — `POST
  /api/v1/events/{id}/status {to_status, officer, note?}`: planning →
  approved | active | cancelled; approved → active | cancelled; active →
  completed | cancelled; 409 on an illegal transition. Every status, the
  creation included, is one `events.event_status_log` row with its named
  actor; a closing event walks its open dispatches to completed /
  cancelled in the same transaction, so no unit stays on scene.
- **`GET /api/v1/dispatches/{id}`** — one dispatch with its append-only
  audit tail (404 unknown, 422 malformed uuid); the `/driver` page follows
  its own ambulance request through the deck's public projection of it. A
  unit dispatched to an interception choke travels to THAT junction and is
  never "arrived" at t=0.
- **C6 parity** — one lawful-read predicate (`SQL_LAWFUL_READ`) for every
  sighting surface (§14 rail 5).
- **Anomaly purge** — the hourly partition maintenance purges
  `traffic.anomaly_log` rows detected before the observation retention
  cutoff, oldest first, in bounded batches under an advisory lock
  (migration 014's detection-time index), so a backlog drains over a few
  passes instead of one long-locking DELETE.
- **Emulator runs (C-G, migration 016)** — async 202 runs, 409 while the
  same use case runs, `interrupted` for a run whose process stopped
  mid-tape, run-scoped ids; a replay of a tape whose recorded route is no
  longer a legal path on today's roads is refused, 422
  `route_not_on_current_network` (503 when the route cannot be checked) —
  after the WP-20 conversion the runs recorded on the schematic network
  are history, never re-animated (§2.9).
- **Event markers on their route** (`city/entities.py`) — an event's
  point on the city picture and in its dossier is the point OF its rank-1
  route nearest the route's centroid (`ST_ClosestPoint(spine,
  centroid)`, `position_basis: spine_point_nearest_centroid`), so it sits
  on a road — the centroid alone lands off the curved OSM roads, inside a
  loop route by up to kilometres; an event with no route keeps its zone's
  centroid (`zone_centroid`). A dispatch whose officer named no waypoint
  junction is drawn at its event's marker (`position_basis:
  event_marker`). The API shape is unchanged; verified on both private
  stacks: every marker ≤ 0.03 m from its route.

---

*Revision v2.4 — city-platform pass (2026-10-13, IST). The platform is
restated as Veer, a **city-scale multi-agency corridor-and-enforcement
operating system** — VVIP routing one component among many. Added this
pass on top of v2.3's corridor-OS generalization: the **enforcement
family** (migrations 010–011; all eleven of that day applied live — vehicle watchlist,
earmarks with lawful-basis + capped retention, source-labelled sightings,
pending-only e-challans with human approval and append-only audit tails,
`crime_incident`/`vehicle_pursuit` event kinds, the agency dispatch
channel with Jan Rakshak first-class, the C7 city signal-plan writer with
snapshot/restore), the **God's-eye canvas**
(`docs/GODS_EYE_INTEGRATION.md`), and the named **AMA module** (R9).
Measured this revision, first-hand — **all three gates re-executed by
this documentation pass**: `verify_all.sh` **ALL TIERS PASSED, exit 0**
(pytest tiers: 182 / 22+12 / 113 / 93+1skip / 453+1skip, plus the two
gods-eye tiers); `showcase_run.sh` ("God's Eye Morning") **exit 0,
SHOWCASE PASS** (§13's showcase table); `bench_stack.sh` **exit 0, BENCH
OK** — decider 546.42 tok/s / p99 866.0 ms, Gemma 23.29 tok/s,
embeddings 85.9 ms, 62.73 GiB resident, adversarial **19/19** (score 100)
at avg 25.98 s / max 47.6 s. `fleet_smoke.sh` not re-run this pass (last
recorded ALL PASS; fleet health covered transitively).
Liveness probed: all health endpoints 200 incl. the emulator `:8090`.
**Kept visible:** open quality gaps tool_validity 0.684 /
keyword_coverage 0.561 (§3, §13); the 04:09Z bench run with one
69.99 s scenario (§3 — report overwritten by the re-run, doc-only
history); the emulator's `X-Emulation-Replay` provenance
rule and the ANPR `emulated_anpr`/`live_anpr` split (§2.9,
`docs/GODS_EYE_INTEGRATION.md`). The backend is the sole source of truth.*

*Revision v2.5 — Wave-3 pass (2026-10-18, IST), documentation added
2026-10-19: the **degradation ledger** (oracle `ops/` module +
`GET /api/v1/ops/degradation-taxonomy` + the deck's DegradationLedger
panel on `/ops`), the **consistency-under-repetition gate**
(`scripts/repetition_gate.sh` — full live gate vs `--fast` selftest), the
clone's **full-system live smoke** (`scripts/argus_full_smoke.sh` with the
original-stack read-only guard), and the **`-argus` image ownership
split** (`infra/compose.argus-override.yml`), with the wave3-repetition /
wave3-ledger tiers added to the §13 ladder. The Wave-1/2 material (novelty
latch §3, tier-0 cache §3, support hatch + t0 foundation rung §6.1b,
P90-robust routing + banded ETAs + model-drift panel §13) was already in
place. No measurement figure elsewhere in this document was changed by
this pass.*

*Revision v2.6 — wave 4 (WP-15, 2026-10-23, IST): documents the road
network rebuilt on real OpenStreetMap roads (WP-20 — 49 segments, 34
junctions, ≈192 km (191.76 km measured in the store), migration 020 `geom_source`, ODbL attribution,
§16.1), escort authority — the decider only proposes and a named officer
approves every switch, a timeout holds the current plan (WP-21, §16.2),
the stolen-vehicle rule and its 409 (§16.3), route updates (§16.4), the
synthetic history kept apart (§16.5), provenance (§16.6), the diversion
fields (§16.7), display geometry and the spatial router (§16.8), the
God's-eye in 3D for signed-in operators with the keyless fallback (WP-22,
§16.9), chat grounding / MCP / directives (§16.10), the operator PIN gate
and officer attribution (§16.11), the WP-11 adaptive behaviours (§2.2),
migrations 012–020 with the 016-not-014 correction (§5), bring-up and the
boot auto-start verified by real reboots (§11), the current test
collection and acceptance ladder (§13) and the deployed ports throughout.
Every figure added by this revision was measured on a fresh install of
this tree (or read from the code it names);
the bench (§3) and the flagship showcase artifacts (§13) are kept as the
dated runs they are and are re-run on the deployed wave-4 stack (the
showcase was, on 2026-10-24 — v2.7).*

*Revision v2.6.1 — the integrated tree (2026-10-23, IST): WP-22's 3D
draping, chip and judge view merged and verified (§16.9 states them
as shipped); the stolen-vehicle pursuit basis (an e-challan
basis → 422) and **one officer approval → one pursuit** (reuse → 422, the
`pursuit` link on every recommendation row, `/ops` and `demo_reset.sh`
skipping spent approvals — §16.3); the emulator's positions on the road
line, its SYNTHETIC wire labels, movements ending at their route's ends
and the 422 refusal of a pre-conversion replay (§2.9); the network step's
re-anchoring of EMULATED reads, tapes and profiles, its stamp-proof probe
and its one restart (§11, §16.1); event markers on their route (§16.12);
the repetition gate's ownership-scoped restore, the journeys' 92 checks
and `demo_reset.sh --cancel-debris` (§13); and the test collection
recounted on the merged tree (§13). The flagship showcase artifacts stay
the dated 2026-10-18 run until the live showcase re-run (done — v2.7).*

*Revision v2.7 — the live pass (2026-10-24, IST): the release deployed to
the live stack (2026-10-23 23:33, READY 11/11, migration 020 applied) and
its live acceptance recorded as it ran — 19 of 22 steps green first time,
the three failures, their causes, the fixes and the green re-runs, with
times (§13); the tier-0 fix — pre-movement route options labelled by
their own segments and the pre-movement own-doc filing — redeployed at
01:20 and its live gate green (§3); the flagship showcase table, the
journeys, the repetition gate and the pre-judging reset regenerated from
the LIVE runs (§13) — the showcase artifacts are now the live run of
2026-10-24 00:01 IST, its stolen-vehicle chain without a fine; the hero
shots recaptured on the staged judging state (the operator views in
photoreal 3D), with the `--ops-only` capture (§13); the canvas's `SUPERSEDED · STOLEN` pin
(§16.3); the live 3D verification (§16.9); the test collection recounted
after the fix — 2709 pytest, convoy-brain 635, cargo 120 (§13). The
replay MAE once quoted for the 2026-10-18 run stood on seed hourly
aggregates since found fabricated and deleted; no such figure is quoted
any more.*
