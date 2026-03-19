# VVIP Convoy Orchestration Platform — Technical Documentation

> **Version**: 1.0  
> **Stack**: Python 3.13 · Rust 2024 · TypeScript 5.7 · PostgreSQL 17 + PostGIS · Valkey · NATS  
> **AI Runtime**: Ollama + Qwen 3.5 9B (Q4_K_M) · ONNX Runtime 1.20 · PyTorch 2.6  
> **Infrastructure**: Docker Compose · Prometheus · Grafana

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Service Reference](#2-service-reference)
   - 2.1 [signal-ingress (Rust)](#21-signal-ingress)
   - 2.2 [traffic-oracle (Python)](#22-traffic-oracle)
   - 2.3 [convoy-brain (Python)](#23-convoy-brain)
   - 2.4 [corridor-store (PostgreSQL)](#24-corridor-store)
   - 2.5 [command-deck (SvelteKit)](#25-command-deck)
   - 2.6 [Frontend Dashboard (React)](#26-frontend-dashboard)
3. [API Reference](#3-api-reference)
4. [Database Schema](#4-database-schema)
5. [ML Model Inventory](#5-ml-model-inventory)
6. [MCP Tools & Resources](#6-mcp-tools--resources)
7. [AI Agent Architecture](#7-ai-agent-architecture)
8. [IPC & Data Flow](#8-ipc--data-flow)
9. [Hardware Constraints & VRAM Budget](#9-hardware-constraints--vram-budget)
10. [Infrastructure & Deployment](#10-infrastructure--deployment)
11. [Observability & Monitoring](#11-observability--monitoring)
12. [Testing Strategy](#12-testing-strategy)
13. [Security Model](#13-security-model)
14. [Configuration Reference](#14-configuration-reference)

---

## 1. Architecture Overview

```
                          ┌─────────────────────┐
                          │   command-deck       │  SvelteKit + MapLibre GL + WebGPU
                          │   (Presentation)     │  Port 5173
                          └──────────┬──────────┘
                                     │ HTTP / WebSocket / NDJSON Stream
                          ┌──────────▼──────────┐
                          │    convoy-brain      │  Python + LangGraph Workflows
                          │  (Orchestration)     │  Port 8080
                          └──┬──────┬───────┬───┘
                             │      │       │
                    ┌────────▼┐  ┌──▼──┐  ┌─▼───────────┐
                    │ Ollama  │  │NATS │  │traffic-oracle│  Python + ONNX Runtime
                    │Qwen 3.5 │  │     │  │ (ML/Optim)   │  Port 8081
                    │ (GPU)   │  │     │  └──────┬───────┘
                    └─────────┘  └──┬──┘         │
                                    │      ┌─────▼───────┐
                          ┌─────────▼───┐  │ PostGIS     │
                          │signal-      │  │ + Valkey    │
                          │ingress      │  │ + DuckDB    │
                          │ (Rust)      │  └─────────────┘
                          └─────────────┘
                               │
                  ┌────────────┼────────────┐
                  ▼            ▼            ▼
             Gov Traffic   Fleet GPS   Crowdsource
               Feeds      Telemetry     Reports
```

### Service Registry

| Service | Language | Build Tool | Port | VRAM | Purpose |
|---|---|---|---|---|---|
| signal-ingress | Rust 2024 | Cargo | — | 0 | Arrow IPC traffic feed ingestion |
| corridor-store | SQL/PostGIS | Docker | 5432 | 0 | Spatial DB + pgRouting + pgvector |
| traffic-oracle | Python 3.13 | uv | 8081 | 409 MB* | ML prediction + route optimization |
| convoy-brain | Python 3.13 | uv | 8080 | 0** | LLM agentic orchestration + MCP |
| command-deck | TypeScript/Svelte | Bun | 5173 | 0*** | SvelteKit dashboard + WebGPU sim |
| Ollama | Go (external) | — | 11434 | 5632 MB | Qwen 3.5 9B LLM runtime |

\* ONNX inference only; falls back to CPU if VRAM exhausted  
\** Accesses GPU indirectly through Ollama HTTP API  
\*** WebGPU simulation runs on integrated GPU

---

## 2. Service Reference

### 2.1 signal-ingress

**Language**: Rust 2024 Edition  
**Build**: `cargo build --release`  
**Dependencies**: tokio 1.0, reqwest 0.12, arrow 54, async-nats 0.38, tracing 0.1, chrono 0.4, serde 1.0

#### Module Structure

| Module | Files | Purpose |
|---|---|---|
| `feeds/` | traffic_gov.rs, mapping_api.rs, fleet_gps.rs, crowdsource.rs | 4 traffic data source adapters + feed trait dispatcher |
| `pipeline/` | channels.rs, batch.rs | Bounded channels (feed: 10,000 / batch: 100), batching with flush logic |
| `normalize/` | schema_align.rs | API response → `TrafficObservation` struct mapping |
| `anomaly/` | detector.rs, flags.rs, config.rs, stats.rs | Tier-1 inline anomaly detection (~500ns per observation) |
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

---

### 2.2 traffic-oracle

**Language**: Python 3.13  
**Build**: `uv sync && uv run python -m traffic_oracle`  
**Key Dependencies**: torch 2.6, onnxruntime 1.20, ortools 9.11, networkx 3.4, igraph 0.11, polars 1.17, geopandas 1.0, shapely 2.0, duckdb 1.2, asyncpg 0.31, valkey 6.1, nats-py 2.9

#### Module Structure

| Module | Key Files | Purpose |
|---|---|---|
| `data/` | db.py, models.py, graph_queries.py, cache.py, vector_store.py | Database access, Pydantic models, pgRouting queries, Valkey cache, pgvector search |
| `predict/` | flow_forecaster.py, eta_model.py, data_loader.py, training_pipeline.py, congestion_heatmap.py | DSTGAT forecasting, HistGBT ETA, training data preparation |
| `nn/` | dstgat.py, graph_attention.py, temporal_conv.py, congestion_gate.py, quantize.py | Neural network architecture (CausalTCN → TemporalAttention → CongestionGate → DynamicGAT → MultiHorizon) |
| `optimize/` | corridor_router.py, diversion_planner.py, constructive_heuristic.py, mip_solver.py | OR-Tools CP-SAT routing, MIP diversion optimization |
| `optimize/rl/` | agent.py, environment.py, reward.py, trainer.py | Factored DQN signal controller (per-signal Q-network) |
| `ingest/` | nats_consumer.py, arrow_decoder.py, db_writer.py, cache_writer.py | NATS → Arrow decode → DB bulk insert + cache write |
| `anomaly/` | stream_detector.py, ml_detector.py, feature_builder.py | Real-time detection + Isolation Forest ML flagging |
| `synthetic/` | generator.py, gap_detector.py, pattern_matcher.py, spatial_interpolator.py, confidence_scorer.py | Gap-filling synthetic data generation |
| `evaluate/` | scenario_sim.py | Monte Carlo scenario comparison |
| `runtime/` | gpu_arbiter.py, onnx_serve.py, memory_profiler.py, thread_pool.py | VRAM allocation, ONNX session management, memory guards |

#### HTTP Endpoints (Port 8081)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/predict/flow` | DSTGAT T+5/10/15/30 traffic forecast |
| `POST` | `/api/v1/predict/eta` | HistGBT travel time prediction |
| `POST` | `/api/v1/optimize/routes` | OR-Tools CP-SAT multi-objective routing |
| `POST` | `/api/v1/optimize/diversions` | Per-segment diversion plans with timing |
| `POST` | `/api/v1/evaluate/scenarios` | Monte Carlo scenario comparison |
| `POST` | `/api/v1/traffic/live` | Real-time Valkey cache read |
| `GET` | `/api/v1/traffic/historical` | DuckDB hourly aggregates |
| `GET` | `/api/v1/traffic/history/{id}` | 24h history for segment |
| `GET` | `/api/v1/graph/shortest-path` | pgRouting Dijkstra |
| `GET` | `/api/v1/graph/k-shortest-paths` | pgRouting KSP alternatives |
| `GET` | `/api/v1/spatial/segments` | Spatial bbox segment query |
| `GET` | `/api/v1/spatial/segments/{id}` | Single segment attributes |
| `GET` | `/api/v1/anomalies/recent` | Anomalies in last hour |
| `GET` | `/api/v1/corridor/summary` | Aggregated corridor conditions |
| `GET` | `/api/v1/movements/active` | Active convoy movements |

---

### 2.3 convoy-brain

**Language**: Python 3.13  
**Build**: `uv sync && uv run python -m convoy_brain`  
**Key Dependencies**: langgraph 0.3, ollama 0.4, mcp 1.6, httpx 0.28, structlog, valkey, prometheus-client, pydantic 2.10

#### Module Structure

| Module | Key Files | Purpose |
|---|---|---|
| Root | `__main__.py`, `orchestrator.py`, `ollama_bridge.py` | FastAPI app (port 8080), ReAct reasoning loop, Ollama client |
| `agents/` | traffic_analyst.py, route_planner.py, security_liaison.py, diversion_coordinator.py | 4 specialized LLM agents with structured output schemas |
| `workflows/` | pre_movement.py, live_escort.py, post_clearance.py | 3 LangGraph state machine workflows |
| `mcp/` | server.py, tools.py, resources.py, prompts.py | MCP server: 11 tools, 4 resources, 3 prompt templates |
| `memory/` | convoy_context.py, store.py | Convoy state management backed by Valkey |
| `metrics/` | metrics.py | Prometheus metric definitions |

#### Orchestrator Configuration

| Setting | Value |
|---|---|
| Model | `qwen3.5:9b-q4_K_M` |
| Max Context | 8192 tokens |
| Max Output | 2048 tokens |
| Temperature | 0.3 |
| Output Format | JSON mode |
| Concurrency | Serialized (`asyncio.Lock`) |
| Max Tool Rounds | 6 |
| Tool Timeout | 30s |
| Turn Timeout | 120s |
| Max Retries/Tool | 2 |

---

### 2.4 corridor-store

**Engine**: PostgreSQL 17 + PostGIS 3.5 + pgRouting 3.7 + pgvector 0.8  
**Extensions**: `postgis`, `pgrouting`, `vector`, `pg_trgm`

#### Schemas

- `corridor` — Road network: segments, junctions, adjacency graph, routing views
- `traffic` — Observations (range-partitioned), hourly aggregates, anomaly log, synthetic data, segment embeddings (pgvector)
- `convoy` — Movement records, planned routes, diversions

#### OSM Importer

Python CLI tool (`corridor-importer`) that extracts road network data from OpenStreetMap via OSMnx, maps it to the `corridor` schema, builds the segment adjacency graph, and bulk-loads to PostgreSQL.

---

### 2.5 command-deck

**Framework**: SvelteKit 2.15 + Svelte 5.15 (runes syntax)  
**Build**: `bun install && bun run dev`  
**Key Dependencies**: MapLibre GL 5.0, deck.gl 9.1, D3 7.9, Turf.js 7.2, Flatbuffers 24.12

#### Component Architecture

| Component | Purpose |
|---|---|
| ConvoyMap.svelte | MapLibre GL map with route overlays, markers |
| RouteLayer.svelte | Route visualization with scoring overlays |
| TrafficHeatmap.svelte | Congestion heatmap rendering |
| ConvoyTracker.svelte | Live convoy position tracking |
| DiversionPanel.svelte | Diversion plan display and status |
| SecurityBadge.svelte | VVIP classification display |
| ToolCallViewer.svelte | MCP tool execution visualization |
| ThoughtStream.svelte | Chain-of-thought reasoning display |
| MetricsBar.svelte | System performance metrics |
| ChatInterface.svelte | AI assistant chat with NDJSON streaming |
| SimulationView.svelte | WebGPU microscopic traffic simulation |
| TimelineView.svelte | Temporal event history |
| AlertPanel.svelte | Alert management |
| StatusGrid.svelte | Service health monitoring |

#### Stores

| Store | Purpose |
|---|---|
| convoy.ts | Active convoy state management |
| traffic.ts | Traffic data subscriptions |
| simulation.ts | WebGPU simulation state |
| chat.ts | Chat message history and streaming |
| alerts.ts | Alert state management |
| settings.ts | User preferences |

#### WebGPU Simulation

Two WGSL compute shaders for microscopic traffic simulation:
- `vehicle_physics.wgsl` — Vehicle movement and interaction
- `reduce_stats.wgsl` — Statistics aggregation

---

### 2.6 Frontend Dashboard

**Framework**: React 19 + Vite 6  
**Key Dependencies**: react-leaflet 5.0.0, recharts 3.8.0, lucide-react

#### Components

| Component | Lines | Purpose |
|---|---|---|
| VVIPDashboard.jsx | ~400 | Root component — toolbar, panel layout management |
| ConvoyMap.jsx | ~350 | Leaflet map with convoy route overlays |
| LeftPanel.jsx | — | Convoy planning — VVIP profile, vehicle assignment, route preview |
| RightPanel.jsx | ~1900+ | Operations monitor — LIVE, CORRIDOR, INTEL, PREDICT, COMMS tabs |
| AIReasoningPanel.jsx | — | Chain-of-thought AI reasoning display |
| NotificationBell.jsx | — | Emergency clearance notification system |
| SearchBar.jsx | — | Global search for convoys and locations |
| ConvoyContext.jsx | — | React context for global convoy state |
| useLiveData.js | — | Custom hook for real-time data subscriptions |

#### PREDICT Tab Features

- Live notification engine with gradient severity alerts
- Synthetic ground data feature computation
- Present-state recommendations with chain-of-thought reasoning
- Multi-horizon forecast curves (T+5, T+10, T+15, T+30)
- Future predictions with chain-of-thought reasoning
- Model status display (Qwen 3.5 9B integration status)

---

## 3. API Reference

### convoy-brain Endpoints (Port 8080)

#### `GET /health`

```json
{
  "status": "ok | degraded",
  "ollama": "connected | unreachable"
}
```

#### `POST /movements/{movement_id}/plan`

Pre-movement planning workflow. Accepts origin/destination coordinates, VVIP class, and planned departure time. Returns scored routes, security validation, diversion directives, and approval status.

**Request** (`PlanRequest`):
```json
{
  "origin": [77.209, 28.614],
  "destination": [77.202, 28.628],
  "vvip_class": "Z",
  "planned_departure": "2026-03-17T10:00:00+05:30"
}
```

**Response**:
```json
{
  "movement_id": "uuid",
  "vvip_class": "Z+ | Z | Y | X",
  "planned_departure": "ISO-8601",
  "corridor_status": "green | amber | red",
  "primary_route": {
    "route_id": "uuid",
    "segment_ids": [1, 2, 3],
    "score": 0.85,
    "reason": "string"
  },
  "alternate_routes": [...],
  "security_compliant": true,
  "security_violations": [...],
  "security_warnings": [...],
  "security_score": 0.92,
  "diversion_directives": [...],
  "scenario_comparison": {},
  "status": "approved | conditional",
  "confidence": "high | medium | low"
}
```

#### `POST /movements/{movement_id}/escort`

Initiates the live escort workflow. Monitors convoy in real-time, activates diversions per segment, handles congestion spikes and rerouting.

**Request**: `{ "destination": [77.202, 28.628] }`  
**Response**: `{ "movement_id": "uuid", "escort_complete": true, "total_iterations": 150, "final_status": "string" }`

#### `POST /movements/{movement_id}/clear`

Post-clearance recovery workflow. Deactivates diversions, monitors traffic recovery, and generates an after-action report.

**Response fields**: segments recovered, diversions deactivated, recovery time, escort duration, total decisions made.

#### `POST /chat`

Single-turn chat with the AI orchestrator. Returns the AI's response with action, reasoning, tool calls made, and confidence level.

```json
{
  "message": "Plan a Z+ movement from Rashtrapati Bhavan to IGI Airport",
  "movement_id": "optional-uuid",
  "vvip_class": "Z+"
}
```

#### `POST /chat/stream` (NDJSON Streaming)

Same request as `/chat`. Response is newline-delimited JSON:

```
{"type":"token","data":"The traffic on"}
{"type":"thought","data":{"stepIndex":1,"text":"Analyzing corridor congestion..."}}
{"type":"tool_call","data":{"callId":"tc_1","toolName":"predict_traffic_flow","arguments":{...},"state":"running"}}
{"type":"tool_result","data":{"callId":"tc_1","state":"success","result":{...},"durationMs":245}}
{"type":"done"}
```

Event types: `token`, `thought`, `tool_call`, `tool_result`, `done`, `error`

---

## 4. Database Schema

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
| `geom` | GEOMETRY(LineString, 4326) | GIST indexed |

#### `corridor.junctions`

| Column | Type | Notes |
|---|---|---|
| `junction_id` | BIGSERIAL PK | |
| `junction_type` | TEXT DEFAULT 'intersection' | CHECK: intersection, roundabout, flyover, underpass, toll_plaza, signal |
| `signal_control` | BOOLEAN DEFAULT FALSE | |
| `geom` | GEOMETRY(Point, 4326) | GIST indexed |

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

| Column | Type |
|---|---|
| `segment_id` | BIGINT |
| `hour_utc` | TIMESTAMPTZ |
| `avg_speed_kmh` | REAL |
| `p50_speed_kmh` | REAL |
| `p95_congestion` | REAL |
| `observation_cnt` | INTEGER |

PK: (`segment_id`, `hour_utc`)

#### `traffic.segment_embeddings`

| Column | Type | Notes |
|---|---|---|
| `segment_id` | BIGINT FK | |
| `pattern_type` | TEXT NOT NULL | CHECK: daily_profile, weekly_profile, event_response, incident_response |
| `embedding` | vector(24) | HNSW index (m=16, ef_construction=64, vector_l2_ops) |
| `computed_at` | TIMESTAMPTZ | |

#### `traffic.anomaly_log`

| Column | Type | Notes |
|---|---|---|
| `anomaly_id` | BIGSERIAL PK | |
| `segment_id` | BIGINT NOT NULL | |
| `timestamp_utc` | TIMESTAMPTZ NOT NULL | |
| `anomaly_type` | TEXT NOT NULL | CHECK: future_timestamp, stale_data, speed_jump, phantom_reading, speed_zscore, congestion_outlier, road_class_mismatch, spatial_mismatch, isolation_forest |
| `severity` | TEXT NOT NULL | CHECK: low, medium, high |
| `details` | JSONB DEFAULT '{}' | |

#### `traffic.synthetic_observations`

| Column | Type | Notes |
|---|---|---|
| `synthetic_id` | BIGSERIAL PK | |
| `segment_id` | BIGINT NOT NULL | |
| `timestamp_utc` | TIMESTAMPTZ NOT NULL | |
| `speed_kmh` | REAL NOT NULL | |
| `congestion_idx` | REAL NOT NULL | CHECK: 0.0 to 1.0 |
| `generation_method` | TEXT NOT NULL | CHECK: historical_match, spatial_interpolation, temporal_pattern, default_fallback |
| `confidence` | REAL NOT NULL | CHECK: 0.0 to 1.0 |
| `source_segments` | BIGINT[] | |

### Schema: `convoy`

#### `convoy.movements`

| Column | Type | Notes |
|---|---|---|
| `movement_id` | UUID PK | DEFAULT gen_random_uuid() |
| `vvip_class` | TEXT NOT NULL | CHECK: Z+, Z, Y, X |
| `status` | TEXT NOT NULL DEFAULT 'planning' | CHECK: planning, approved, active, completed, cancelled |
| `origin_geom` | GEOMETRY(Point, 4326) NOT NULL | |
| `destination_geom` | GEOMETRY(Point, 4326) NOT NULL | |
| `planned_start`, `actual_start`, `actual_end` | TIMESTAMPTZ | |

#### `convoy.planned_routes`

| Column | Type | Notes |
|---|---|---|
| `route_id` | UUID PK | |
| `movement_id` | UUID FK | |
| `route_rank` | SMALLINT NOT NULL | 1=primary, 2+=alternates |
| `total_distance_m` | REAL | |
| `estimated_time_sec` | INTEGER | |
| `disruption_score` | REAL | Predicted traffic disruption [0-100] |
| `route_geom` | GEOMETRY(LineString, 4326) NOT NULL | |
| `segment_ids` | BIGINT[] NOT NULL | Ordered segment list |

#### `convoy.diversions`

| Column | Type | Notes |
|---|---|---|
| `diversion_id` | UUID PK | |
| `movement_id` | UUID FK | |
| `segment_id` | BIGINT FK | |
| `diversion_type` | TEXT NOT NULL | CHECK: full_closure, partial_closure, speed_restriction, signal_override |
| `activate_at`, `deactivate_at` | TIMESTAMPTZ NOT NULL | |
| `alt_route_geom` | GEOMETRY(LineString, 4326) | |

---

## 5. ML Model Inventory

### 5.1 DSTGAT Flow Forecaster (ONNX, GPU)

- **Architecture**: CausalTCN → TemporalAttentionPooling → CongestionGate → 3× DynamicGATLayer → 4× MultiHorizonHead
- **Parameters**: ~83,574 (FP32: ~327 KB, INT8: ~82 KB)
- **Input Shape**: `(B, 200, 12, 8)` — batch, max_nodes, lookback_steps, features
- **Output Shape**: `(B, 200, 4, 2)` — speed_kmh + congestion_idx at T+5/10/15/30 min
- **Features** (8): speed, congestion, lanes, road_class, hour_sin, hour_cos, dow_sin, dow_cos
- **VRAM Budget**: 409 MB max, CPU fallback via GpuArbiter
- **Training Loss**: `speed_MSE + 0.5 × congestion_MSE + 0.1 × regime_mismatch`

### 5.2 ETA Predictor (scikit-learn HistGBT, CPU-only)

- **Architecture**: HistGradientBoostingRegressor (max_iter=200, max_depth=6)
- **Features** (10): route_length_m, num_segments, avg_predicted_speed, avg_predicted_congestion, hour_sin, hour_cos, dow_sin, dow_cos, num_signals, weighted_road_class_score
- **Output**: Predicted travel time in seconds

### 5.3 Isolation Forest Anomaly Detector (scikit-learn, CPU-only)

- **Architecture**: IsolationForest (n_estimators=100, contamination=0.05)
- **Features** (8): speed_normalized, congestion_idx, hour_sin, hour_cos, dow_sin, dow_cos, speed_delta, neighbor_speed_ratio
- **Output**: Anomaly labels (-1 = anomalous, 1 = normal) + anomaly scores

### 5.4 Factored DQN Signal Controller (RL, per-signal ONNX, CPU-only)

- **Architecture**: Per-signal Q-network `Linear(6→32→3)` (~291 params each)
- **Observation** (6): phase, time_in_phase, queue_length, approach_rate, convoy_distance, convoy_eta
- **Actions**: {hold, extend_green_5s, skip_to_next_phase}
- **Reward range**: [-100, +20] with heavy convoy delay penalty

### 5.5 Qwen 3.5 9B (Ollama, GPU)

- **Quantization**: Q4_K_M (~5.6 GB VRAM)
- **Context Window**: 8192 tokens
- **Max Output**: 2048 tokens
- **Temperature**: 0.3
- **Model File**: `models/Modelfile.qwen_vvip`
- **Fine-tuning**: QLoRA pipeline in `models/finetune/`

---

## 6. MCP Tools & Resources

### MCP Tools (11 tools)

All tools are defined in `convoy-brain/src/convoy_brain/mcp/server.py` and proxy to traffic-oracle HTTP endpoints.

| # | Tool Name | Purpose | Backend Endpoint |
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
| 10 | `get_live_traffic` | Real-time Valkey cache read | `POST /api/v1/traffic/live` |
| 11 | `get_historical_pattern` | DuckDB hourly aggregates | `GET /api/v1/traffic/historical` |

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

## 7. AI Agent Architecture

### Agent Definitions

| Agent | Output Schema | Key Fields |
|---|---|---|
| TrafficAnalystAgent | Corridor assessment | `overall_status`, `congestion_summary`, `risk_segments`, `trend`, `recommendation`, `confidence` |
| RoutePlannerAgent | Route ranking | `primary_route`, `alternate_routes`, `rejected_routes`, `overall_reasoning`, `confidence` |
| SecurityLiaisonAgent | Security validation | `compliant`, `violations`, `warnings`, `recommendations`, `security_score`, `confidence` |
| DiversionCoordinatorAgent | Diversion directives | `directives`, `queue_alerts`, `overall_status`, `confidence` |

### LangGraph Workflows

| Workflow | Nodes | Graph Shape | Constraints |
|---|---|---|---|
| Pre-Movement | 7 nodes | Linear with conditional reroute loop | Max 2 reroute retries |
| Live Escort | 6 nodes | Cyclic monitor→act loop | Max 1800 iterations (30 min) |
| Post-Clearance | 5 nodes | Recovery poll loop | Max 120 iterations (10 min) |

### Live Escort Constants

```python
GPS_POLL_INTERVAL_SEC         = 1.0
ACTIVATION_LOOKAHEAD_SEC      = 120
CONGESTION_SPIKE_THRESHOLD    = 0.8
MAX_MONITOR_ITERATIONS        = 1800
```

### Post-Clearance Constants

```python
RECOVERY_CONGESTION_THRESHOLD = 0.3
RECOVERY_POLL_INTERVAL_SEC    = 5.0
MAX_RECOVERY_ITERATIONS       = 120
```

---

## 8. IPC & Data Flow

### Zero-Copy Arrow IPC

signal-ingress serializes `TrafficObservation` into Arrow RecordBatches (9 columns) and publishes over NATS JetStream for zero-copy consumption by traffic-oracle.

**Arrow Schema**:

| Column | Type |
|---|---|
| `timestamp_ms` | Int64 |
| `lon` | Float64 |
| `lat` | Float64 |
| `segment_id` | Utf8 |
| `speed_kmh` | Float32 |
| `congestion_index` | Float32 |
| `source` | UInt8 |
| `data_quality` | UInt8 |
| `confidence` | Float32 |

### NATS Subjects

| Subject | Format | Publisher | Consumer |
|---|---|---|---|
| `corridor.traffic.live` | Arrow IPC bytes | signal-ingress | traffic-oracle |
| `corridor.traffic.anomaly` | JSON | signal-ingress | traffic-oracle |

### Valkey Cache Keys

| Key Pattern | TTL | Purpose |
|---|---|---|
| `traffic:latest:{segment_id}` | 300s | Latest observation per segment |
| `corridor:summary:{corridor_id}` | 60s | Aggregated corridor conditions |
| `traffic:forecast:{segment_id}:{horizon}` | 60s | Cached DSTGAT predictions |
| `convoy:context:{movement_id}` | 24h (completed) / none (active) | Convoy state |

### Inter-Service HTTP

| From | To | Protocol | Purpose |
|---|---|---|---|
| convoy-brain | traffic-oracle | HTTP (httpx, 30s timeout) | All 11 MCP tool proxies |
| convoy-brain | Ollama | HTTP (ollama client) | LLM inference |
| convoy-brain | Valkey | Redis protocol | ConvoyContext persistence |
| command-deck | convoy-brain | HTTP/WS/NDJSON | All frontend API calls |
| command-deck | traffic-oracle | HTTP (via Vite proxy) | Traffic snapshot/segments |

---

## 9. Hardware Constraints & VRAM Budget

### Target Hardware

| Resource | Specification |
|---|---|
| CPU | Intel i9-13900HX (8P + 16E = 24 threads) |
| GPU | NVIDIA RTX 4070 Laptop 8 GB GDDR6X |
| RAM | 32 GB DDR5 |
| Storage | 1 TB NVMe Gen4 SSD |

### VRAM Allocation

| Component | Budget MB | Priority | Fallback |
|---|---|---|---|
| Qwen 3.5 9B (Q4_K_M via Ollama) | 5632 | 1 (KING) | None — required |
| CUDA Runtime Overhead | 307 | 0 (system) | N/A |
| ONNX Flow Forecaster Inference | 409 | 2 (medium) | CPU ExecutionProvider |
| Safety Headroom / KV Cache Growth | 1844 | 3 (low) | Reduce num_ctx |
| **TOTAL** | **8192** | | |

### CPU Core Pinning

| Cores | Service | Rationale |
|---|---|---|
| 0–3 | signal-ingress | P-cores: latency-sensitive I/O |
| 4–7 | traffic-oracle | P-cores: ML inference, OR-Tools |
| 8–11 | PostGIS/pgRouting | P-cores: spatial + graph queries |
| 12–15 | convoy-brain + Ollama | Mixed: orchestration + LLM |
| 16–23 | OS, NATS, Valkey, frontend | E-cores: background services |

### VRAM Rules

1. **Ollama/Qwen is KING** — always gets its VRAM allocation
2. ONNX checks VRAM via `GpuArbiter.can_allocate_onnx()` before loading
3. PyTorch training NEVER runs concurrently with Ollama
4. PostGIS, pgRouting, pgvector are CPU-ONLY (zero VRAM)
5. WebGPU simulation uses integrated GPU (`powerPreference: 'low-power'`)

---

## 10. Infrastructure & Deployment

### Docker Compose Services

Defined in `infra/compose.yml`:

| Container | Image | Ports | Notes |
|---|---|---|---|
| postgis | Custom (PostGIS + pgRouting + pgvector) | 5432 | Volumes for data persistence |
| valkey | valkey/valkey:latest | 6379 | Max memory: 512 MB, allkeys-lfu |
| nats | nats:latest | 4222 | JetStream enabled |
| signal-ingress | Custom Rust binary | — | Core-pinned: 0–3 |
| traffic-oracle | Custom Python | 8081 | Core-pinned: 4–7 |
| convoy-brain | Custom Python | 8080 | Core-pinned: 12–15 |

**Ollama runs on HOST** — not in Docker — for native GPU access, bound to `localhost:11434`.

### Dockerfiles

| File | Service | Base Image |
|---|---|---|
| Dockerfile.postgis | corridor-store | postgres:17 |
| Dockerfile.signal-ingress | signal-ingress | rust:1.82 (build) → debian:bookworm-slim |
| Dockerfile.traffic-oracle | traffic-oracle | python:3.13-slim |
| Dockerfile.convoy-brain | convoy-brain | python:3.13-slim |
| Dockerfile.frontend | frontend | node:22-slim |
| Dockerfile.command-deck | command-deck | node:22-slim |

### Deployment Scripts

| Script | Purpose |
|---|---|
| `infra/scripts/bootstrap.sh` | Initial system setup, dependency installation |
| `infra/scripts/deploy.sh` | Production deployment automation |
| `infra/scripts/os_tuning.sh` | OS-level performance tuning (CPU governor, I/O scheduler) |
| `infra/scripts/vram_audit.sh` | VRAM usage monitoring and reporting |
| `infra/scripts/refresh_seed.ps1` | PowerShell database seeding |

---

## 11. Observability & Monitoring

### Prometheus Metrics

**convoy-brain metrics**:

| Metric | Type | Labels |
|---|---|---|
| `vvip_llm_tokens_generated_total` | Counter | model |
| `vvip_llm_inference_duration_seconds` | Histogram | model |
| `vvip_ollama_health_up` | Gauge | — |
| `vvip_llm_tokens_per_second` | Gauge | model |
| `vvip_mcp_tool_calls_total` | Counter | tool_name |
| `vvip_mcp_tool_duration_seconds` | Histogram | tool_name |
| `vvip_mcp_tool_errors_total` | Counter | tool_name, error |
| `vvip_api_request_duration_seconds` | Histogram | service, method, endpoint, status |
| `vvip_orchestrator_tool_rounds_total` | Gauge | movement_id |
| `vvip_orchestrator_nondeterministic_errors_total` | Counter | — |

**traffic-oracle metrics**:

| Metric | Type | Labels |
|---|---|---|
| `vvip_onnx_inference_duration_seconds` | Histogram | model_name, provider |
| `vvip_pgrouting_query_duration_seconds` | Histogram | query_type |
| `vvip_pgvector_search_duration_seconds` | Histogram | — |
| `vvip_db_pool_total_connections` | Gauge | — |
| `vvip_anomaly_detections_total` | Counter | anomaly_type, severity |
| `vvip_observations_processed_total` | Counter | — |
| `vvip_memory_rss_mb` | Gauge | — |

### Alert Rules (18 rules, 8 groups)

| Group | Alert Examples |
|---|---|
| GPU/VRAM | >92% critical, >80% warning, >85°C temperature |
| LLM | <5 tok/s, p99 >30s, Ollama unreachable |
| MCP | Tool timeouts, >20% error rate |
| Reasoning | >5 tool rounds (loop detection), non-deterministic failures |
| API | p95 >5s (convoy-brain), p95 >2s (traffic-oracle), >5% 5xx |
| Spatial DB | pgRouting p95 >1s, pgvector p95 >500ms, pool exhaustion |
| Streaming | >80% channel utilization, >30% anomaly spike |
| Containers | >85% memory, >2 restarts in 15 min |

---

## 12. Testing Strategy

### Unit Tests

| Service | Framework | Test Count |
|---|---|---|
| signal-ingress | `cargo test` | ~50 tests (anomaly_tests.rs, pipeline_tests.rs) |
| traffic-oracle | `pytest` + `pytest-asyncio` | ~117 tests across 13 files |
| command-deck | `vitest` | SvelteKit component tests |

### Adversarial Testing

Located in `tests/adversarial/`:

- **16 routing scenarios** testing agent reasoning quality
- **2 stress scenarios** testing system stability under load
- Entry point: `python -m tests.adversarial`
- Components: config.py (scenarios), runner.py (execution), evaluator.py (reasoning assessment), report.py (report generation), stability.py (determinism checks)

### Integration Tests

Docker Compose with test fixtures — full stack end-to-end validation.

---

## 13. Security Model

### VVIP Security Classification

| Class | Min Lanes | Closure Type | Advance Time | Max Queue |
|---|---|---|---|---|
| Z+ | 6 | Full closure | 180s | 2000m |
| Z | 4 | Partial closure | 120s | 1000m |
| Y | 2 | Speed restriction + signal priority | 60s | 500m |
| X | None | Signal priority only | 0s | 0m (no diversions) |

### Road Class Security Scores

```python
ROAD_CLASS_SCORES = {
    "motorway": 100, "trunk": 85, "primary": 70,
    "secondary": 50, "tertiary": 30, "residential": 10
}
```

### Access Control

- VVIP class information is **SENSITIVE** — movement destinations and VVIP identities are never logged
- All inter-service communication over internal Docker network — no external exposure
- command-deck is the ONLY externally-exposed service (port 5173), proxied via Vite
- Ollama bound to `localhost:11434` only

---

## 14. Configuration Reference

### Route Scoring Weights

```python
WEIGHT_TIME       = 0.3
WEIGHT_DISRUPTION = 0.4
WEIGHT_SECURITY   = 0.2
WEIGHT_COMPLEXITY = 0.1
```

### Valkey Configuration

```
Max Memory:      512 MB (system RAM)
Eviction Policy: allkeys-lfu
Key TTLs:        latest traffic (300s), corridor summary (60s), forecasts (60s)
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

---

*This document reflects the architecture as of March 2026. All schemas, APIs, and constants are authoritative — the backend is the sole source of truth.*
