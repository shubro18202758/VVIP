# VVIP Convoy Orchestration Platform — Copilot Instructions

> **This file instructs AI coding assistants (GitHub Copilot, Claude, etc.) on the
> strict hardware constraints, design patterns, architectural rules, and the
> INVIOLABLE Backend-First Integration Law for this project.
> Read this file completely before generating any code.**

---

## 0. THE BACKEND-FIRST LAW — ABSOLUTE ARCHITECTURAL MANDATE

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                                                                            ║
║   THE BACKEND IS THE SOLE SOURCE OF TRUTH.                                 ║
║                                                                            ║
║   Every data model, API response schema, enum value, field name,           ║
║   validation rule, and domain concept is DEFINED by the backend.           ║
║                                                                            ║
║   The frontend (command-deck) MUST consume backend structures exactly      ║
║   as they are. Under NO circumstances shall backend schemas, API           ║
║   contracts, database models, or service interfaces be modified,           ║
║   weakened, or restructured to accommodate frontend convenience.           ║
║                                                                            ║
║   When a conflict exists between backend data shape and UI needs,          ║
║   the UI MUST be overridden or extended — never the backend.               ║
║                                                                            ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### 0.1 Backend-First Integration Rules

1. **Schema Direction**: Data flows from backend → frontend. TypeScript types in
   `command-deck/src/lib/types/` are MIRRORS of backend Pydantic models and Rust
   structs. If a backend model changes, the TypeScript type MUST be updated to
   match — not the reverse.

2. **Field Names**: Backend uses `snake_case` (Python/Rust). Frontend uses
   `camelCase` (TypeScript). The API client layer
   (`command-deck/src/lib/api/client.ts`) handles serialization: outbound
   requests send `snake_case` JSON keys; inbound responses are mapped to
   `camelCase` TypeScript interfaces. The mapping is purely cosmetic and MUST NOT
   alter semantics, add fields, remove fields, or change types.

3. **Enum Values**: All enum string values (`VvipClass`, `MovementStatus`,
   `DiversionType`, `DiversionStatus`, `FeedSource`, `DataQuality`,
   `anomaly_type`, `severity`, `road_class`, `junction_type`, etc.) are defined
   by backend code or database CHECK constraints. Frontend MUST use the exact
   same string literals. Never introduce frontend-only enum values.

4. **Nullable Fields**: If a backend field is `None`/`null` (e.g.,
   `convoy_position`, `selected_route_id`, `eta_seconds`), the frontend MUST
   handle null gracefully. Do NOT default these to empty strings or zeros unless
   the backend specifies a default.

5. **No Frontend-Invented Endpoints**: The frontend shall not assume or call API
   endpoints that do not exist on the backend. If the UI needs data that no
   current endpoint provides, the backend MUST be extended first, then the
   frontend can consume the new endpoint.

6. **No Client-Side Domain Logic**: Route scoring, security validation, VRAM
   budget calculations, anomaly classification, and all ML-driven decisions are
   computed exclusively by the backend. The frontend MAY display derived values
   (e.g., color-coding based on `congestion_idx` thresholds) but MUST NOT
   re-implement or override backend business rules.

7. **WebSocket Event Fidelity**: All `WsEvent<T>` payloads are backend-defined.
   The frontend MUST type the payload generically against the backend schema and
   MUST NOT add client-side event types that masquerade as backend events.

8. **Streaming Protocol**: The NDJSON chat streaming protocol (`token`,
   `thought`, `tool_call`, `tool_result`, `done`, `error`) is defined by
   `convoy-brain`. Frontend parsing MUST handle exactly these event types with
   no client-side extensions to the event schema.

### 0.2 When Extending the Frontend

When building new UI features:

1. **Read the backend code first.** Identify which endpoints, data models, and
   tool outputs the feature will consume.
2. **Mirror backend types exactly** in `src/lib/types/index.ts`. Include all
   fields, even if the current UI does not display them.
3. **Build adapter functions** in `src/lib/api/client.ts` that map backend
   `snake_case` responses to frontend `camelCase` types. These adapters MUST be
   1:1 field mappings with no data transformation beyond casing.
4. **If the UI needs data aggregation**, request it from the backend (e.g., via
   MCP resources like `traffic://corridor/summary`) instead of computing it
   client-side from raw data.
5. **If the UI needs a data shape the backend doesn't provide**, propose and
   implement a new backend endpoint or MCP resource first.

### 0.3 Forbidden Patterns

- Adding optional fields to TypeScript interfaces that don't exist on the backend
- Wrapping backend responses in frontend "enrichment" layers that add computed fields
- Creating client-side stores that merge data from multiple endpoints into novel shapes
- Using `as any` or `as unknown` to bypass type mismatches between backend and frontend
- Hardcoding magic numbers that are defined by backend constants (e.g., `MAX_TOOL_ROUNDS=6`, VRAM budgets, congestion thresholds)
- Implementing client-side route scoring, security classification, or ETA prediction

---

## 1. Hardware Constraints — ABSOLUTE LIMITS

This platform deploys on a **single edge device** (Lenovo Legion 7i laptop):

| Resource | Specification | Status |
|---|---|---|
| CPU | Intel i9-13900HX (8P+16E = 24T) | **Core-Pinned** |
| GPU | NVIDIA RTX 4070 Laptop 8GB GDDR6X | **8192 MB VRAM** |
| System RAM | 32 GB DDR5 | Adequate |
| Storage | 1 TB NVMe Gen4 SSD | Adequate |

**The 8GB VRAM is the immovable bottleneck.** Every line of code you write must
respect this constraint. The CPU is core-pinned per service — respect the
allocation map in Section 10.

---

## 2. VRAM Budget — ENFORCED HARD LIMITS

```
┌─────────────────────────────────────┬───────────┬──────────┬─────────────────┐
│ Component                           │ Budget MB │ Priority │ Fallback        │
├─────────────────────────────────────┼───────────┼──────────┼─────────────────┤
│ Qwen 3.5 9B (Q4_K_M via Ollama)    │ 5632      │ 1 (KING) │ None — required │
│ CUDA Runtime Overhead               │  307      │ 0 (sys)  │ N/A             │
│ ONNX Flow Forecaster Inference      │  409      │ 2 (med)  │ CPU EP          │
│ Safety Headroom / KV Cache Growth   │ 1844      │ 3 (low)  │ Reduce num_ctx  │
├─────────────────────────────────────┼───────────┼──────────┼─────────────────┤
│ TOTAL                               │ 8192      │          │                 │
└─────────────────────────────────────┴───────────┴──────────┴─────────────────┘
```

### Rules:
1. **Ollama/Qwen is KING.** It always gets its VRAM allocation. All other GPU consumers must yield.
2. **ONNX inference checks VRAM before loading.** Use `GpuArbiter.can_allocate_onnx()` before creating a CUDA session.
3. **PyTorch training NEVER runs concurrently with Ollama.** Training windows are explicit offline phases where Ollama is unloaded.
4. **Never call `torch.cuda.empty_cache()` in production code** — it's a sign of poor memory management.
5. **Never use `model.to('cuda')` without first checking `GpuArbiter`.** Always gate GPU transfers.
6. **PostGIS, pgRouting, pgvector are CPU-ONLY.** Zero VRAM consumption.
7. **WebGPU simulation (command-deck) prefers integrated GPU** to avoid VRAM competition.

---

## 3. Design Patterns for VRAM-Constrained Edge ML

### 3.1 Aggressive RAM Offloading
- All training data, intermediate activations, and non-inference tensors MUST reside in **system RAM**, never VRAM.
- Use `pin_memory=True` with PyTorch DataLoaders for efficient CPU→GPU transfers.
- Database query results (PostGIS, DuckDB) stay in system RAM or Arrow buffers.
- Valkey cache uses system RAM — never GPU memory.

### 3.2 Lazy GPU Loading
- ONNX sessions are **lazily initialized** — no VRAM allocated until first inference request.
- LLM model stays loaded in Ollama's managed VRAM space — do NOT load Qwen in your Python process.
- Use ONNX Runtime SessionOptions to control memory arena:
  ```python
  opts = ort.SessionOptions()
  opts.enable_mem_pattern = True
  opts.enable_cpu_mem_arena = True  # offload to CPU arena
  ```

### 3.3 Serialized LLM Access
- All LLM calls go through `OllamaBridge` which uses `asyncio.Lock` to serialize requests.
- NEVER make concurrent Ollama API calls — each call allocates KV cache in VRAM.
- Keep `num_ctx` <= 8192 tokens. Larger context = larger KV cache = VRAM overflow risk.

### 3.4 CPU-First Computation
- Route optimization (OR-Tools CP-SAT): **Always CPU.** Zero VRAM.
- Scenario simulation (Monte Carlo): **Always CPU.**
- Geospatial queries (PostGIS, Shapely): **Always CPU.**
- Graph algorithms (igraph, NetworkX): **Always CPU.**
- Anomaly detection (Isolation Forest): **Always CPU.**
- ETA prediction (HistGBT): **Always CPU.**
- Only DSTGAT flow forecaster inference uses GPU, and only when `GpuArbiter` permits.

### 3.5 Graceful GPU→CPU Fallback
Every GPU code path MUST have a CPU fallback:
```python
providers = arbiter.get_onnx_providers()
# Returns ["CUDAExecutionProvider", "CPUExecutionProvider"]
# or just ["CPUExecutionProvider"] if VRAM is exhausted
session = ort.InferenceSession(model_path, providers=providers)
```

---

## 4. Architecture Overview

```
                    ┌──────────────────┐
                    │  command-deck    │  SvelteKit + MapLibre GL + WebGPU
                    │  (Presentation)  │  Port 5173
                    └────────┬─────────┘
                             │ HTTP / WebSocket / NDJSON Stream
                    ┌────────▼─────────┐
                    │  convoy-brain    │  Python + LangGraph Workflows
                    │ (Orchestration)  │  Port 8080
                    └──┬─────┬─────┬──┘
                       │     │     │
              ┌────────▼─┐ ┌▼────┐ ┌▼────────────┐
              │ Ollama   │ │NATS │ │traffic-oracle│  Python + ONNX Runtime
              │Qwen 3.5  │ │     │ │ (ML/Optim)   │  Port 8081
              │ (GPU)    │ │     │ └──────┬───────┘
              └──────────┘ └──┬──┘        │
                              │     ┌─────▼──────┐
                    ┌─────────▼──┐  │ PostGIS    │
                    │signal-     │  │ + Valkey   │
                    │ingress     │  │ + DuckDB   │
                    │ (Rust)     │  └────────────┘
                    └────────────┘
                         │
            ┌────────────┼────────────┐
            ▼            ▼            ▼
       Gov Traffic    Fleet GPS   Crowdsource
         Feeds       Telemetry     Reports
```

### Service Registry

| Service | Language | Build Tool | Port | VRAM | Purpose |
|---|---|---|---|---|---|
| signal-ingress | Rust 2024, Edition 2024 | Cargo | — | 0 | Arrow IPC traffic feed ingestion |
| corridor-store | SQL/PostGIS | Docker | 5432 | 0 | Spatial DB + pgRouting + pgvector |
| traffic-oracle | Python 3.13 | uv | 8081 | 409* | ML prediction + route optimization |
| convoy-brain | Python 3.13 | uv | 8080 | 0** | LLM agentic orchestration + MCP |
| command-deck | TypeScript/Svelte | Bun | 5173 | 0*** | SvelteKit dashboard + WebGPU sim |
| Ollama | Go (external) | — | 11434 | 5632 | Qwen 3.5 9B LLM runtime (HOST) |

\* ONNX inference only; falls back to CPU if VRAM exhausted.
\** convoy-brain accesses GPU indirectly through Ollama HTTP API.
\*** WebGPU simulation runs on integrated GPU, not discrete.

---

## 5. Complete Backend API Contract — Source of Truth

### 5.1 convoy-brain Endpoints (Port 8080)

#### `GET /health`
```json
{
  "status": "ok | degraded",
  "ollama": "connected | unreachable"
}
```

#### `POST /movements/{movement_id}/plan`
**Request** (`PlanRequest`):
```json
{
  "origin": [77.209, 28.614],
  "destination": [77.202, 28.628],
  "vvip_class": "Z",
  "planned_departure": "2026-03-17T10:00:00+05:30"
}
```
**Response** — Full Pre-Movement Workflow result:
```json
{
  "movement_id": "string",
  "vvip_class": "Z+ | Z | Y | X",
  "planned_departure": "ISO-8601",
  "corridor_status": "green | amber | red",
  "primary_route": {
    "route_id": "string",
    "segment_ids": [1, 2, 3],
    "score": 0.85,
    "reason": "string"
  },
  "alternate_routes": [{"route_id": "...", "segment_ids": [...], "score": 0.72, "reason": "..."}],
  "security_compliant": true,
  "security_violations": [{"segment_id": 5, "rule": "min_lanes", "detail": "2 lanes < 4 required", "severity": "critical"}],
  "security_warnings": [{"segment_id": 7, "concern": "crowd risk near market"}],
  "security_score": 0.92,
  "diversion_directives": [{"segment_id": 1, "action": "activate | hold | deactivate", "agency": "traffic_police | transport | security", "timing_sec": 120, "detail": "..."}],
  "scenario_comparison": {},
  "status": "approved | conditional",
  "confidence": "high | medium | low"
}
```

#### `POST /movements/{movement_id}/escort`
**Request** (`EscortRequest`):
```json
{ "destination": [77.202, 28.628] }
```
**Response**:
```json
{
  "movement_id": "string",
  "escort_complete": true,
  "total_iterations": 150,
  "final_status": "string"
}
```

#### `POST /movements/{movement_id}/clear`
**Request**: None (path parameter only).
**Response** — Post-Clearance Report:
```json
{
  "movement_id": "string",
  "vvip_class": "string",
  "selected_route_id": "string | null",
  "total_affected_segments": 12,
  "segments_recovered": 10,
  "segments_still_congested": 2,
  "recovery_iterations": 15,
  "recovery_time_sec": 75.0,
  "diversions_deactivated": 8,
  "alerts_during_escort": 3,
  "total_decisions": 24,
  "planning_to_completion_sec": 1800.0,
  "escort_duration_sec": 900.0,
  "historical_baseline": {},
  "generated_at": 1710654321.0
}
```

#### `POST /chat`
**Request** (`ChatRequest`):
```json
{
  "message": "Plan a Z+ movement from Rashtrapati Bhavan to IGI Airport",
  "movement_id": "optional-uuid",
  "vvip_class": "Z+"
}
```
**Response**:
```json
{
  "session_id": "uuid-string",
  "response": {
    "action": "string",
    "reasoning": "string",
    "tool_calls_made": ["predict_traffic_flow", "find_convoy_routes"],
    "confidence": "high | medium | low",
    "data": {}
  }
}
```

#### `POST /chat/stream` (NDJSON Streaming)
Same request as `/chat`. Response is newline-delimited JSON events:
```
{"type":"token","data":"The traffic on"}
{"type":"thought","data":{"stepIndex":1,"text":"Analyzing corridor congestion..."}}
{"type":"tool_call","data":{"callId":"tc_1","toolName":"predict_traffic_flow","arguments":{"segment_ids":[1,2,3]},"state":"running"}}
{"type":"tool_result","data":{"callId":"tc_1","state":"success","result":{...},"durationMs":245}}
{"type":"done"}
```

### 5.2 MCP Tools — 11 Backend-Defined Tools

All MCP tools are defined in `convoy-brain/src/convoy_brain/mcp/server.py` and
proxy to traffic-oracle HTTP endpoints. The frontend MUST NOT call traffic-oracle
directly for MCP-proxied operations — it goes through convoy-brain's orchestrator.

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

### 5.3 MCP Resources — 4 Backend-Defined Resources

| URI | Description | Backend Endpoint |
|---|---|---|
| `traffic://anomalies/recent` | Anomalies detected in last hour | `GET /api/v1/anomalies/recent` |
| `traffic://corridor/summary` | Aggregated corridor conditions | `GET /api/v1/corridor/summary` |
| `traffic://segments/{id}/history` | 24h history for segment | `GET /api/v1/traffic/history/{id}` |
| `convoy://movements/active` | Active convoy movements | `GET /api/v1/movements/active` |

### 5.4 MCP Prompts — 3 Backend-Defined Prompt Templates

| Prompt Name | Purpose | Key Arguments |
|---|---|---|
| `vvip_security_protocol` | Security classification + constraints | `vvip_class` |
| `route_analysis_brief` | Route scoring weights + criteria | `origin_name`, `destination_name`, `vvip_class` |
| `diversion_coordination_brief` | Multi-agency coordination template | `movement_id` |

### 5.5 VVIP Security Classification — Backend Constants

| Class | Min Lanes | Closure Type | Advance Time | Max Queue |
|---|---|---|---|---|
| Z+ | 6 | Full closure | 180s | 2000m |
| Z | 4 | Partial closure | 120s | 1000m |
| Y | 2 | Speed restriction + signal priority | 60s | 500m |
| X | None | Signal priority only | 0s | 0m (no diversions) |

These are defined in `convoy-brain/src/convoy_brain/mcp/server.py`
(`ToolExecutor.get_prompt`, `vvip_security_protocol`). Frontend MUST display
these values as received — never redefine them client-side.

---

## 6. Complete Database Schema — Source of Truth

### Schema: `corridor`

**`corridor.road_segments`** — Road network edges
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
| `created_at`, `updated_at` | TIMESTAMPTZ | |

**`corridor.junctions`** — Intersection nodes
| Column | Type | Notes |
|---|---|---|
| `junction_id` | BIGSERIAL PK | |
| `junction_type` | TEXT DEFAULT 'intersection' | CHECK: intersection, roundabout, flyover, underpass, toll_plaza, signal |
| `signal_control` | BOOLEAN DEFAULT FALSE | |
| `geom` | GEOMETRY(Point, 4326) | GIST indexed |

**`corridor.segment_adjacency`** — Graph edges (adjacency list)
| Column | Type | Notes |
|---|---|---|
| `from_segment_id` | BIGINT FK | |
| `to_segment_id` | BIGINT FK | |
| `via_junction_id` | BIGINT FK | |
| `turn_cost_sec` | REAL DEFAULT 0 | |
PK: (`from_segment_id`, `to_segment_id`, `via_junction_id`)

**`corridor.road_graph`** — pgRouting-compatible VIEW
```sql
cost = turn_cost_sec + ST_Length(geom::geography) / (speed_limit_kmh * 1000/3600)
reverse_cost = CASE WHEN oneway THEN -1.0 ELSE cost END
```

### Schema: `traffic`

**`traffic.observations`** — Live data (RANGE-partitioned by `timestamp_utc`)
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
PK: (`observation_id`, `timestamp_utc`)

**`traffic.hourly_aggregates`** — ML training features
| Column | Type |
|---|---|
| `segment_id` | BIGINT |
| `hour_utc` | TIMESTAMPTZ |
| `avg_speed_kmh` | REAL |
| `p50_speed_kmh` | REAL |
| `p95_congestion` | REAL |
| `observation_cnt` | INTEGER |
PK: (`segment_id`, `hour_utc`)

**`traffic.segment_embeddings`** — pgvector HNSW-indexed
| Column | Type | Notes |
|---|---|---|
| `segment_id` | BIGINT FK | |
| `pattern_type` | TEXT NOT NULL | CHECK: daily_profile, weekly_profile, event_response, incident_response |
| `embedding` | vector(24) | HNSW index (m=16, ef_construction=64, vector_l2_ops) |
| `computed_at` | TIMESTAMPTZ | |
PK: (`segment_id`, `pattern_type`)

**`traffic.anomaly_log`** — Flagged anomalies
| Column | Type | Notes |
|---|---|---|
| `anomaly_id` | BIGSERIAL PK | |
| `segment_id` | BIGINT NOT NULL | |
| `timestamp_utc` | TIMESTAMPTZ NOT NULL | |
| `anomaly_type` | TEXT NOT NULL | CHECK: future_timestamp, stale_data, speed_jump, phantom_reading, speed_zscore, congestion_outlier, road_class_mismatch, spatial_mismatch, isolation_forest |
| `severity` | TEXT NOT NULL | CHECK: low, medium, high |
| `details` | JSONB DEFAULT '{}' | |

**`traffic.synthetic_observations`** — Gap-filled data
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

**`convoy.movements`** — VVIP movement records
| Column | Type | Notes |
|---|---|---|
| `movement_id` | UUID PK | DEFAULT gen_random_uuid() |
| `vvip_class` | TEXT NOT NULL | CHECK: Z+, Z, Y, X |
| `status` | TEXT NOT NULL DEFAULT 'planning' | CHECK: planning, approved, active, completed, cancelled |
| `origin_geom` | GEOMETRY(Point, 4326) NOT NULL | |
| `destination_geom` | GEOMETRY(Point, 4326) NOT NULL | |
| `planned_start`, `actual_start`, `actual_end` | TIMESTAMPTZ | |

**`convoy.planned_routes`** — Ranked route candidates
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

**`convoy.diversions`** — Planned diversions per movement
| Column | Type | Notes |
|---|---|---|
| `diversion_id` | UUID PK | |
| `movement_id` | UUID FK | |
| `segment_id` | BIGINT FK | |
| `diversion_type` | TEXT NOT NULL | CHECK: full_closure, partial_closure, speed_restriction, signal_override |
| `activate_at`, `deactivate_at` | TIMESTAMPTZ NOT NULL | |
| `alt_route_geom` | GEOMETRY(LineString, 4326) | |

---

## 7. ML Model Inventory — Backend Source of Truth

### 7.1 DSTGAT Flow Forecaster (ONNX, GPU)
- **Architecture**: CausalTCN → TemporalAttentionPooling → CongestionGate → 3x DynamicGATLayer → 4x MultiHorizonHead
- **Parameters**: ~83,574 (FP32: ~327KB, INT8: ~82KB)
- **Input**: `(B, 200, 12, 8)` — batch, max_nodes, lookback_steps, features
- **Output**: `(B, 200, 4, 2)` — speed_kmh + congestion_idx at T+5/10/15/30 min
- **Features**: speed, congestion, lanes, road_class, hour_sin, hour_cos, dow_sin, dow_cos
- **VRAM Budget**: 409 MB max, CPU fallback via GpuArbiter
- **Training Loss**: `speed_MSE + 0.5 * congestion_MSE + 0.1 * regime_mismatch`

### 7.2 ETA Predictor (scikit-learn HistGBT, CPU-only)
- **Architecture**: HistGradientBoostingRegressor (max_iter=200, max_depth=6)
- **Features** (10): route_length_m, num_segments, avg_predicted_speed, avg_predicted_congestion, hour_sin, hour_cos, dow_sin, dow_cos, num_signals, weighted_road_class_score
- **Output**: predicted travel time in seconds

### 7.3 Isolation Forest Anomaly Detector (scikit-learn, CPU-only)
- **Architecture**: IsolationForest (n_estimators=100, contamination=0.05)
- **Features** (8): speed_normalized, congestion_idx, hour_sin, hour_cos, dow_sin, dow_cos, speed_delta, neighbor_speed_ratio
- **Output**: anomaly labels (-1=anomalous, 1=normal) + scores

### 7.4 Factored DQN Signal Controller (RL, per-signal ONNX, CPU-only)
- **Architecture**: Per-signal Q-network `Linear(6→32→3)` (~291 params each)
- **Obs per signal**: [phase, time_in_phase, queue_length, approach_rate, convoy_distance, convoy_eta]
- **Actions**: {hold, extend_green_5s, skip_to_next_phase}
- **Reward range**: [-100, +20] with heavy convoy delay penalty

### 7.5 Tier-1 Anomaly Detector (Rust, inline, CPU-only)
- 8-check inline detection at ~500ns/observation in signal-ingress
- Checks: future_timestamp, stale_data, speed_jump, phantom_reading, speed_zscore, congestion_outlier, road_class_mismatch, spatial_mismatch

---

## 8. Orchestrator Architecture — Backend Source of Truth

### 8.1 ReAct Loop Constants
| Constant | Value | Description |
|---|---|---|
| `MAX_TOOL_ROUNDS` | 6 | Max LLM→tool→LLM cycles per turn |
| `MAX_RETRIES_PER_TOOL` | 2 | Per-tool failure retries |
| `TOOL_TIMEOUT_SEC` | 30.0 | Per-tool execution timeout |
| `TOTAL_TURN_TIMEOUT_SEC` | 120.0 | Max time for complete turn |

### 8.2 LLM Configuration
| Setting | Value |
|---|---|
| Model | `qwen3.5:9b-q4_K_M` |
| Max Context | 8192 tokens |
| Max Output | 2048 tokens |
| Temperature | 0.3 |
| Output Format | JSON mode |
| Concurrency | Serialized (`asyncio.Lock`) |

### 8.3 Four LLM Agents (convoy-brain/agents/)
| Agent | Output Schema | Key Fields |
|---|---|---|
| TrafficAnalystAgent | Corridor assessment | `overall_status`, `congestion_summary`, `risk_segments`, `trend`, `recommendation`, `confidence` |
| RoutePlannerAgent | Route ranking | `primary_route`, `alternate_routes`, `rejected_routes`, `overall_reasoning`, `confidence` |
| SecurityLiaisonAgent | Security validation | `compliant`, `violations`, `warnings`, `recommendations`, `security_score`, `confidence` |
| DiversionCoordinatorAgent | Diversion directives | `directives`, `queue_alerts`, `overall_status`, `confidence` |

### 8.4 Three LangGraph Workflows (convoy-brain/workflows/)
| Workflow | Nodes | Graph Shape |
|---|---|---|
| Pre-Movement | 7 nodes | Linear with conditional reroute loop (max 2 retries) |
| Live Escort | 6 nodes | Cyclic monitor→act loop (max 1800 iterations / 30 min) |
| Post-Clearance | 5 nodes | Recovery poll loop (max 120 iterations / 10 min) |

---

## 9. IPC and Data Flow Patterns

### 9.1 Zero-Copy Arrow IPC
- signal-ingress serializes `TrafficObservation` into Arrow RecordBatches (9 columns).
- Published over NATS JetStream to traffic-oracle for zero-copy consumption.
- Arrow Schema: `timestamp_ms(i64)`, `lon(f64)`, `lat(f64)`, `segment_id(utf8)`, `speed_kmh(f32)`, `congestion_index(f32)`, `source(u8)`, `data_quality(u8)`, `confidence(f32)`

### 9.2 NATS Subjects
| Subject | Format | Publisher | Consumer |
|---|---|---|---|
| `corridor.traffic.live` | Arrow IPC bytes | signal-ingress | traffic-oracle |
| `corridor.traffic.anomaly` | JSON (TrafficObservation) | signal-ingress | traffic-oracle |

### 9.3 Valkey Cache Keys
| Key Pattern | TTL | Purpose |
|---|---|---|
| `traffic:latest:{segment_id}` | 300s | Latest observation per segment |
| `corridor:summary:{corridor_id}` | 60s | Aggregated corridor conditions |
| `traffic:forecast:{segment_id}:{horizon}` | 60s | Cached DSTGAT predictions |
| `convoy:context:{movement_id}` | 24h (completed) / none (active) | Convoy state |

### 9.4 Inter-Service HTTP
| From | To | Protocol | Purpose |
|---|---|---|---|
| convoy-brain | traffic-oracle | HTTP (httpx, 30s timeout) | All 11 MCP tool proxies |
| convoy-brain | Ollama | HTTP (ollama client) | LLM inference |
| convoy-brain | Valkey | Redis protocol | ConvoyContext persistence |
| command-deck | convoy-brain | HTTP/WS/NDJSON | All frontend API calls |
| command-deck | traffic-oracle | HTTP (via Vite proxy) | Traffic snapshot/segments |

---

## 10. CPU Core Allocation Map — Production Pinning

```
┌────────────────────────────────────────────────────────────────┐
│ i9-13900HX: 8 Performance Cores + 16 Efficiency Cores (24T)   │
├──────────┬──────────────────────────┬──────────────────────────┤
│ Cores    │ Service                  │ Rationale                │
├──────────┼──────────────────────────┼──────────────────────────┤
│  0 —  3  │ signal-ingress (Rust)    │ P-cores: latency-        │
│          │                          │ sensitive stream I/O      │
│  4 —  7  │ traffic-oracle (Python)  │ P-cores: ONNX, sklearn,  │
│          │                          │ OR-Tools ML inference     │
│  8 — 11  │ PostGIS/pgRouting        │ P-cores: CPU-bound        │
│          │                          │ spatial + graph queries   │
│ 12 — 15  │ convoy-brain + Ollama    │ Mixed: agentic orch +    │
│          │ CPU fallback             │ LLM CPU fallback path    │
│ 16 — 23  │ OS, NATS, Valkey,        │ E-cores: background      │
│          │ command-deck, monitoring  │ services, low priority   │
└──────────┴──────────────────────────┴──────────────────────────┘
```

---

## 11. Code Quality Rules

### General
- **No `.env` files in git.** Use Docker environment variables and secrets.
- **Structured logging only.** JSON logs via `tracing` (Rust) and `structlog` (Python).
- **No print statements.** Ever. Use loggers.
- **Type everything.** Rust: strict mode. Python: mypy strict. TypeScript: strict.

### Rust (signal-ingress)
- Use `tokio` async runtime exclusively.
- Prefer `Arc<T>` over `Rc<T>` — all code must be `Send + Sync`.
- Zero `unwrap()` in production code — use `?` operator with `anyhow::Result`.
- Use `#[instrument]` from `tracing` on all public functions.
- Bounded channels only: `feed_tx` (10,000), `batch_tx` (100).

### Python (traffic-oracle, convoy-brain)
- Python 3.13+ minimum. Use `|` union syntax, not `Union`.
- Package management: `uv` only. Never `pip install` directly.
- Async-first: `asyncpg` for PostgreSQL, `httpx` for HTTP, `nats-py` for NATS.
- Never use `import *`.
- All data classes use `pydantic.BaseModel` or `@dataclass`, not plain dicts.
- `BoundedMLThreadPool` (4 threads) for all ML inference via `asyncio.run_in_executor`.
- `memory_guard(max_rss_mb=3500)` context manager for memory-critical operations.

### TypeScript (command-deck)
- SvelteKit 2.x with Svelte 5 runes syntax (`$props`, `$state`, `$derived`).
- Package management: `bun` only.
- Strict TypeScript — no `any` except unavoidable library gaps.
- MapLibre GL JS for all map rendering — NOT Google Maps or Mapbox.
- WebGPU simulation prefers integrated GPU adapter (`powerPreference: 'low-power'`).
- All types in `src/lib/types/index.ts` mirror backend models — see Section 0.

---

## 12. Database Patterns

### PostGIS
- All spatial queries use **spatial indexes** (`GIST`).
- Use `ST_DWithin` for proximity queries (uses index), NOT `ST_Distance < X` (full scan).
- Partition `traffic.observations` by day for efficient retention and pruning.
- Road network graph traversal uses `corridor.segment_adjacency` + `corridor.road_graph` VIEW.

### pgRouting
- `pgr_dijkstra` for shortest path (single pair).
- `pgr_KSP` for K alternative shortest paths.
- `pgr_drivingDistance` for isochrone computation.
- Recursive CTE on `corridor.segment_adjacency` for multi-hop neighbor discovery.
- Road graph cost = `turn_cost_sec + segment_length_m / speed_limit_m_per_sec`.

### pgvector
- 24-dimensional segment traffic pattern embeddings.
- HNSW index (m=16, ef_construction=64) with L2 distance operator (`<->`).
- Pattern types: `daily_profile`, `weekly_profile`, `event_response`, `incident_response`.
- Embeddings extracted from DSTGAT GAT layer activations.

### DuckDB (Embedded Analytics)
- Used inside traffic-oracle for historical analytics on `traffic.observations`.
- Connects to PostgreSQL via `postgres_scanner` extension.
- Runs in-process — no separate server.
- Used for training data windowing: 5-minute binning, sliding windows, forward-fill.

### Valkey (Real-time Cache)
- Max memory: 512 MB (system RAM, not VRAM).
- Eviction policy: `allkeys-lfu` — least frequently used.
- Key TTLs: latest traffic (300s), corridor summary (60s), forecasts (60s).

---

## 13. Security and Access Control

- VVIP class information is **SENSITIVE**. Never log movement destinations or VVIP identities.
- All inter-service communication uses internal Docker network — no external exposure.
- The command-deck is the ONLY service with external port exposure (5173), proxied via Vite.
- Ollama runs on HOST (not Docker) for native GPU access — bound to localhost:11434 only.

---

## 14. Testing Strategy

- **Unit tests**: Each service has its own test suite.
  - Rust: `cargo test` (anomaly_tests.rs, pipeline_tests.rs — ~50 tests)
  - Python: `pytest` with `pytest-asyncio` (~117 tests across 13 files)
  - TypeScript: `vitest` (via SvelteKit)
- **Integration tests**: Docker Compose with test fixtures.
- **VRAM compliance tests**: Run after any model change.
- **Load tests**: Simulate 1000+ concurrent traffic observations/sec through signal-ingress.
- **Adversarial tests**: `python -m tests.adversarial` — automated agentic testing with 14 routing scenarios + 2 stress scenarios.

---

## 15. Frontend → Backend Type Mapping Reference

This table maps every TypeScript type in `command-deck/src/lib/types/index.ts`
to its backend source of truth:

| TypeScript Type | Backend Source | Backend Location |
|---|---|---|
| `VvipClass` | DB CHECK + MCP prompt | `convoy.movements.vvip_class`, `mcp/server.py` |
| `MovementStatus` | DB CHECK constraint | `convoy.movements.status` |
| `DiversionType` | DB CHECK constraint | `convoy.diversions.diversion_type` |
| `DiversionStatus` | Convoy context state | `convoy_context.py` active_diversions |
| `ConvoyMovement` | `ConvoyContext` dataclass | `convoy_brain/memory/convoy_context.py` |
| `RouteCandidate` | `RouteCandidate` dataclass | `traffic_oracle/optimize/corridor_router.py` |
| `DiversionEntry` | `DiversionPlan` dataclass | `traffic_oracle/optimize/diversion_planner.py` |
| `SegmentTraffic` | Valkey cache entry | `traffic:latest:{segment_id}` |
| `RoadSegment` | `RoadSegment` Pydantic model | `traffic_oracle/data/models.py` |
| `TrafficPrediction` | DSTGAT output tensor | `traffic_oracle/predict/flow_forecaster.py` |
| `TrafficAnomaly` | `AnomalyRecord` model | `traffic_oracle/data/models.py` |
| `ChatMessage` | Orchestrator turn response | `convoy_brain/orchestrator.py` |
| `ThoughtStep` | NDJSON stream event | `convoy_brain/__main__.py` stream handler |
| `ToolCallStatus` | Orchestrator tool execution | `convoy_brain/orchestrator.py` |
| `SimulationConfig` | Frontend-only (WebGPU) | N/A — simulation is client-side |
| `SimulationState` | Frontend-only (WebGPU) | N/A — simulation is client-side |
| `VehicleGPULayout` | Frontend-only (WebGPU) | N/A — GPU buffer layout |
| `ServiceHealth` | Health endpoint response | `convoy_brain/__main__.py /health` |
| `GpuStatus` | nvidia-smi + GpuArbiter | `traffic_oracle/runtime/gpu_arbiter.py` |
| `WsEvent` | Backend WebSocket events | `convoy_brain` (planned) |

**Note**: `SimulationConfig`, `SimulationState`, and `VehicleGPULayout` are the
ONLY types that are frontend-only (WebGPU microscopic simulation). All other
types MUST mirror their backend source exactly.

---

## 16. Observability Stack

### Prometheus Metrics (Backend-defined)

**convoy-brain metrics** (`convoy_brain/metrics.py`):
- `vvip_llm_tokens_generated_total` (Counter, labels: model)
- `vvip_llm_inference_duration_seconds` (Histogram, labels: model)
- `vvip_ollama_health_up` (Gauge)
- `vvip_llm_tokens_per_second` (Gauge, labels: model)
- `vvip_mcp_tool_calls_total` (Counter, labels: tool_name)
- `vvip_mcp_tool_duration_seconds` (Histogram, labels: tool_name)
- `vvip_mcp_tool_errors_total` (Counter, labels: tool_name, error)
- `vvip_api_request_duration_seconds` (Histogram, labels: service, method, endpoint, status)
- `vvip_orchestrator_tool_rounds_total` (Gauge, labels: movement_id)
- `vvip_orchestrator_nondeterministic_errors_total` (Counter)

**traffic-oracle metrics** (`traffic_oracle/metrics.py`):
- `vvip_onnx_inference_duration_seconds` (Histogram, labels: model_name, provider)
- `vvip_pgrouting_query_duration_seconds` (Histogram, labels: query_type)
- `vvip_pgvector_search_duration_seconds` (Histogram)
- `vvip_db_pool_total_connections` (Gauge)
- `vvip_anomaly_detections_total` (Counter, labels: anomaly_type, severity)
- `vvip_observations_processed_total` (Counter)
- `vvip_memory_rss_mb` (Gauge)

### Alert Rules (18 rules across 8 groups)
- GPU/VRAM: >92% critical, >80% warning, >85C temperature
- LLM: <5 tok/sec, p99>30s latency, Ollama unreachable
- MCP: tool timeouts, >20% error rate
- Reasoning: >5 tool rounds (loop), non-deterministic failures
- API: p95>5s (convoy-brain), p95>2s (traffic-oracle), >5% 5xx
- Spatial DB: pgRouting p95>1s, pgvector p95>500ms, pool exhaustion
- Stream: >80% channel utilization, >30% anomaly spike
- Containers: >85% memory, >2 restarts in 15m

---

## 17. Quick Reference — Backend Constants the Frontend Must Never Redefine

```python
# Orchestrator limits (convoy-brain/orchestrator.py)
MAX_TOOL_ROUNDS = 6
MAX_RETRIES_PER_TOOL = 2
TOOL_TIMEOUT_SEC = 30.0
TOTAL_TURN_TIMEOUT_SEC = 120.0

# LLM config (convoy-brain/ollama_bridge.py)
MODEL = "qwen3.5:9b-q4_K_M"
MAX_CONTEXT_TOKENS = 8192
MAX_OUTPUT_TOKENS = 2048
TEMPERATURE = 0.3

# VRAM budgets (traffic-oracle/runtime/gpu_arbiter.py)
OLLAMA_VRAM_MB = 5632
ONNX_INFERENCE_MB = 409
CUDA_OVERHEAD_MB = 307
HEADROOM_MB = 1844

# Route scoring weights (traffic-oracle/optimize/corridor_router.py)
WEIGHT_TIME = 0.3
WEIGHT_DISRUPTION = 0.4
WEIGHT_SECURITY = 0.2
WEIGHT_COMPLEXITY = 0.1

# Road class security scores
ROAD_CLASS_SCORES = {
    "motorway": 100, "trunk": 85, "primary": 70,
    "secondary": 50, "tertiary": 30, "residential": 10
}

# Signal-ingress pipeline (signal-ingress/src/pipeline/channels.rs)
FEED_CHANNEL_CAPACITY = 10_000
BATCH_CHANNEL_CAPACITY = 100
BATCH_FLUSH_COUNT = 100
BATCH_FLUSH_TIMEOUT_MS = 100

# Live Escort workflow (convoy-brain/workflows/live_escort.py)
GPS_POLL_INTERVAL_SEC = 1.0
ACTIVATION_LOOKAHEAD_SEC = 120
CONGESTION_SPIKE_THRESHOLD = 0.8
MAX_MONITOR_ITERATIONS = 1800

# Post-Clearance workflow (convoy-brain/workflows/post_clearance.py)
RECOVERY_CONGESTION_THRESHOLD = 0.3
RECOVERY_POLL_INTERVAL_SEC = 5.0
MAX_RECOVERY_ITERATIONS = 120

# Memory guard (traffic-oracle/runtime/memory_profiler.py)
MEMORY_GUARD_MB = 3500
```

---

**END OF COPILOT INSTRUCTIONS**

*Last updated: 2026-03-17. This file is autogenerated from backend source analysis.*
*Any modifications to backend schemas, APIs, or constants MUST be reflected here.*
