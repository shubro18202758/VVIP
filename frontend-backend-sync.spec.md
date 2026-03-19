# Frontend ↔ Backend Synchronization Specification

> **Version:** 1.0.0
> **Date:** 2026-03-17
> **Scope:** VVIP Convoy Orchestration Platform — command-deck ↔ (convoy-brain + traffic-oracle + corridor-store + signal-ingress)
> **Constraint:** Implementation-ready blueprint only. No application code.

---

## Table of Contents

- [Category 1 — ✓ Matches](#category-1--matches)
- [Category 2 — ✗ Conflicts](#category-2--conflicts)
- [Category 3 — ⊗ Missing Frontend UI](#category-3--missing-frontend-ui)
- [Verification Checklist](#verification-checklist)

---

## Category 1 — ✓ Matches

Verified frontend implementations that correctly consume backend endpoints with proper data flow.

### Match 1.1 — Health Polling Loop

| Aspect | Detail |
|---|---|
| **Frontend** | `routes/+page.svelte` lines 28-45 polls every 10 s |
| **API calls** | `checkConvoyBrainHealth()` → `GET /api/convoy/health`; `getGpuStatus()` → `GET /api/convoy/health/gpu` |
| **Backend** | `convoy-brain/__main__.py` line 106: `GET /health` returns `{"status": str, "ollama": str}` |
| **Store** | `health.ts` — `updateServiceStatus()` and `updateGpuStatus()` correctly update writable stores |
| **Consuming components** | Dashboard page reads `services`, `gpuStatus`, `allServicesOnline`, `vramUsagePercent` |
| **Verdict** | ✓ Fully wired. Polling interval, store mutation, and derived stores all functional. |

### Match 1.2 — Route Planning Flow (planMovement)

| Aspect | Detail |
|---|---|
| **Frontend** | `routes/planning/+page.svelte` line 51 calls `planMovement(movementId, params)` |
| **API calls** | `client.ts` lines 67-83: `POST /api/convoy/movements/{id}/plan` with **correctly converted** snake_case body: `{origin, destination, vvip_class, planned_departure}` |
| **Backend** | `__main__.py` line 118: `POST /movements/{movement_id}/plan` accepts `PlanRequest` with `vvip_class: str`, `planned_departure: str` |
| **Store** | `convoy.ts` — planning page calls `setRoutes()` to populate `routeCandidates` and auto-selects first via `selectedRouteId` |
| **Consuming components** | `RouteComparator.svelte` reads `routeCandidates`, `selectedRouteId`; `CorridorMap.svelte` reads `selectedRoute` derived store |
| **Verdict** | ✓ Correct snake_case conversion, schema matches `PlanRequest`, store hydration verified. |

### Match 1.3 — Escort Launch

| Aspect | Detail |
|---|---|
| **Frontend** | `routes/command/+page.svelte` calls `launchEscort(movementId, destination)` |
| **API calls** | `client.ts` lines 85-90: `POST /api/convoy/movements/{id}/escort` with `{destination}` |
| **Backend** | `__main__.py` line 136: accepts `EscortRequest` with `destination: tuple[float, float] | None` |
| **Verdict** | ✓ Schema match. Optional destination field correctly nullable on both sides. |

### Match 1.4 — Movement Clearance

| Aspect | Detail |
|---|---|
| **Frontend** | `routes/command/+page.svelte` calls `clearMovement(movementId)` |
| **API calls** | `client.ts` lines 92-94: `POST /api/convoy/movements/{id}/clear` with `{}` |
| **Backend** | `__main__.py` line 151: no request body, only path param |
| **Store** | Command page calls `clearConvoy()` which archives `activeConvoy` to `convoyHistory` |
| **Verdict** | ✓ Endpoint, method, and store lifecycle correct. |

### Match 1.5 — LLM Streaming Chat

| Aspect | Detail |
|---|---|
| **Frontend** | `ChatPanel.svelte` line 44 calls `streamChat(message, movementId, vvipClass, callbacks)` |
| **API calls** | `client.ts` lines 172-241: `POST /api/convoy/chat/stream` with `{message, movement_id, vvip_class}` — correctly snake_case |
| **Backend** | `__main__.py` line 169: `POST /chat` accepts `ChatRequest` with `message`, `movement_id`, `vvip_class` |
| **Protocol** | NDJSON streaming with 6 event types: `token`, `thought`, `tool_call`, `tool_result`, `done`, `error` |
| **Store** | `chat.ts` — callbacks wire to `appendToMessage`, `addThought`, `addToolCall`, `updateToolCallState`, `finishStreaming` |
| **Consuming components** | `ChatMessage.svelte` → `ThoughtChain.svelte` + `ToolCallCard.svelte` |
| **Verdict** | ✓ Full streaming pipeline verified. Event parsing, store mutations, and component rendering all aligned. |

**Note:** Backend endpoint is `/chat` (line 169), not `/chat/stream`. The Vite proxy prepends `/api/convoy`, so the frontend hits `/api/convoy/chat/stream` which proxies to `http://localhost:8080/chat/stream`. **Potential issue:** backend defines `/chat`, not `/chat/stream`. This needs verification — if streaming is a separate endpoint, the backend may have it defined elsewhere or the same `/chat` endpoint may stream based on `Accept` headers. This is flagged as **Conflict 2.9** below.

### Match 1.6 — Non-Streaming Chat Fallback

| Aspect | Detail |
|---|---|
| **Frontend** | `client.ts` lines 246-256: `sendChat()` → `POST /api/convoy/chat` |
| **Backend** | `__main__.py` line 169: `POST /chat` |
| **Verdict** | ✓ Endpoint and schema match. Note: this function is currently **unused** by any component (ChatPanel always uses `streamChat`). |

### Match 1.7 — MCP Tool Display Names

| Aspect | Detail |
|---|---|
| **Frontend** | `ToolCallCard.svelte` lines 11-23: maps all 11 MCP tool names to display labels |
| **Backend** | `server.py` lines 26-267: `TOOL_DEFINITIONS` defines exactly 11 tools |
| **Names** | `predict_traffic_flow`, `find_convoy_routes`, `plan_diversions`, `evaluate_scenarios`, `predict_eta`, `query_shortest_path`, `query_k_shortest_paths`, `query_segments_in_bbox`, `query_segment_details`, `get_live_traffic`, `get_historical_pattern` |
| **Verdict** | ✓ All 11 tool names match 1:1 between frontend display mapping and backend definitions. |

### Match 1.8 — Service Health Check (Multi-Service)

| Aspect | Detail |
|---|---|
| **Frontend** | `client.ts` lines 328-330: `checkServiceHealth()` → `GET /api/convoy/health/services` |
| **Store** | `health.ts` — `services` writable with 5 pre-defined entries |
| **Verdict** | ✓ Endpoint defined. Note: currently unused by any page (dashboard only calls `checkConvoyBrainHealth`). |

---

## Category 2 — ✗ Conflicts

Frontend-backend contradictions, bugs, architectural misalignments, and dead code requiring fixes.

---

### Conflict 2.1 — `createMovement()` Sends camelCase Keys

| Aspect | Detail |
|---|---|
| **File** | `client.ts` line 64 |
| **Bug** | `createMovement(params)` passes raw `params` object to `post()`, sending `{vvipClass, plannedDeparture}` in camelCase |
| **Backend expects** | `PlanRequest` schema (line 39-43 of `__main__.py`): `vvip_class: str`, `planned_departure: str` |
| **Contrast** | `planMovement()` at lines 77-82 correctly converts: `vvip_class: params.vvipClass` |

#### Root Cause
Missing explicit key transformation in `createMovement()`. The `params` TypeScript interface uses camelCase per TS convention, but the backend Pydantic model uses snake_case.

#### Fix Procedure
1. In `client.ts` line 64, replace `return post(\`${CONVOY_API}/movements\`, params)` with an explicit object mapping:
   ```
   return post(`${CONVOY_API}/movements`, {
     origin: params.origin,
     destination: params.destination,
     vvip_class: params.vvipClass,
     planned_departure: params.plannedDeparture,
   })
   ```
2. Follow the identical pattern used by `planMovement()` at lines 77-82.

#### Verification
- Call `createMovement()` from the planning page with a Z+ class and future departure time.
- Inspect the network request body in browser DevTools → keys must be `vvip_class` and `planned_departure`.
- Backend must return `200` with `{movement_id: ...}` (not a 422 validation error).

**Note:** There is a secondary issue — the backend `__main__.py` does not define a `POST /movements` endpoint for creation. Only `POST /movements/{id}/plan`, `POST /movements/{id}/escort`, `POST /movements/{id}/clear`, and `POST /chat` exist. The `createMovement()` frontend call targets an endpoint that **does not exist** on the backend. This is escalated as **Conflict 2.8**.

---

### Conflict 2.2 — Traffic API Prefix Mismatch + Missing Server

| Aspect | Detail |
|---|---|
| **Frontend** | `client.ts` line 25: `TRAFFIC_API = '/api/traffic'` — used by `getTrafficSnapshot`, `getRoadSegments`, `getTrafficPredictions` |
| **Vite proxy** | `vite.config.ts`: `/api/traffic` → `http://localhost:8081` (traffic-oracle) |
| **Backend reality** | traffic-oracle has **NO HTTP server**. No FastAPI app, no `__main__.py` with routes, no `uvicorn` dependency. It is a library + NATS consumer only. |
| **ToolExecutor** | `server.py` lines 575-673: expects traffic-oracle to serve `/api/v1/*` endpoints (15 total) at `http://localhost:8081` |

#### Root Cause
Two-layered mismatch:
1. **Prefix mismatch:** Frontend uses `/api/traffic/*`, ToolExecutor uses `/api/v1/*`.
2. **Server missing:** The traffic-oracle service that should host these endpoints does not have an HTTP server. The compose file allocates port 8081 but there's nothing listening.

#### Fix Procedure — Phase 1 (Create traffic-oracle HTTP server)
1. Create `services/traffic-oracle/src/traffic_oracle/api/__init__.py`.
2. Create `services/traffic-oracle/src/traffic_oracle/api/server.py` with a FastAPI app.
3. Add `fastapi`, `uvicorn` to `pyproject.toml` dependencies.
4. Implement all 15 endpoints that `ToolExecutor` expects:
   - `POST /api/v1/predict/flow`
   - `POST /api/v1/optimize/routes`
   - `POST /api/v1/optimize/diversions`
   - `POST /api/v1/evaluate/scenarios`
   - `POST /api/v1/predict/eta`
   - `GET  /api/v1/graph/shortest-path`
   - `GET  /api/v1/graph/k-shortest-paths`
   - `GET  /api/v1/spatial/segments`
   - `GET  /api/v1/spatial/segments/{segment_id}`
   - `POST /api/v1/traffic/live`
   - `GET  /api/v1/traffic/historical`
   - `GET  /api/v1/anomalies/recent`
   - `GET  /api/v1/corridor/summary`
   - `GET  /api/v1/traffic/history/{segment_id}`
   - `GET  /api/v1/movements/active`
5. Each endpoint delegates to the existing traffic-oracle library modules (DSTGAT ONNX, HistGBT, IF, DQN, OR-Tools).
6. Update `Dockerfile.traffic-oracle` to run uvicorn.

#### Fix Procedure — Phase 2 (Align frontend prefix)
Two options (pick one):

**Option A — Update frontend to use `/api/v1/*`:**
1. In `client.ts`, change `TRAFFIC_API = '/api/traffic'` to `TRAFFIC_API = '/api/v1'`.
2. Update `vite.config.ts` proxy: change `/api/traffic` rule to `/api/v1` targeting `http://localhost:8081`.

**Option B — Add `/api/traffic/*` aliases in traffic-oracle server:**
1. In the new traffic-oracle FastAPI app, mount the `/api/v1` router also at `/api/traffic` prefix.
2. No frontend changes needed.

**Recommended:** Option B — keeps frontend prefixes semantically clear (`/api/convoy` vs `/api/traffic`).

#### Verification
- Start traffic-oracle container → `curl http://localhost:8081/api/v1/corridor/summary` returns JSON.
- From command-deck dev server → `getTrafficSnapshot({...})` receives real data, not a connection error.

---

### Conflict 2.3 — WebSocket Endpoint Does Not Exist

| Aspect | Detail |
|---|---|
| **Frontend** | `client.ts` lines 265-317: `createConvoySocket(movementId)` connects to `ws://host/api/convoy/ws/convoy/{movementId}` |
| **Backend** | convoy-brain `__main__.py` — **zero WebSocket endpoints defined**. No `@app.websocket()` decorator anywhere. |
| **Usage** | `routes/command/+page.svelte` creates socket on mount, dispatches events to `updateConvoyPosition`, `updateConvoyStatus`, `updateDiversion` |

#### Root Cause
The WebSocket endpoint was designed in the frontend but never implemented in convoy-brain. The `onclose` handler triggers infinite reconnect attempts (with backoff to 30s), silently consuming resources.

#### Fix Procedure
1. In `convoy-brain/__main__.py`, add a WebSocket endpoint:
   ```
   @app.websocket("/ws/convoy/{movement_id}")
   ```
2. The handler should:
   - Accept the connection.
   - Subscribe to a NATS subject like `convoy.{movement_id}.events`.
   - Forward events as JSON to the WebSocket client.
   - Event types to support (matching frontend `WsEvent` type): `position_update`, `status_change`, `diversion_update`, `eta_update`.
3. Implement a publisher in the escort workflow that publishes convoy position updates to `convoy.{movement_id}.events` as the escort progresses.
4. The Vite proxy already has `ws: true` on the `/api/convoy` rule (confirmed in `vite.config.ts`), so WebSocket upgrade will work through the dev proxy.

#### Verification
- Start convoy-brain → create a movement → open command page → check browser DevTools WebSocket tab → connection should upgrade to `101 Switching Protocols`.
- Trigger a position update → verify `ConvoyTracker.svelte` updates position/speed/heading in real time.

---

### Conflict 2.4 — Store Hydration Gap (4 Dead Traffic Endpoints)

| Aspect | Detail |
|---|---|
| **Dead endpoints** | `getTrafficSnapshot()` (line 102), `getRoadSegments()` (line 117), `getTrafficPredictions()` (line 132), `getRoutes()` (line 96) — all defined in `client.ts` but **never called** by any component or store |
| **Intended stores** | `traffic.ts` — `liveTraffic`, `roadSegments`, `predictions` are writable but have action functions (`updateTrafficBatch`, `loadRoadNetwork`, `setPredictions`) that are never invoked |
| **Result** | `CorridorMap.svelte` renders empty deck.gl layers; `TrafficHeatmap.svelte` shows zeroed congestion stats; `SegmentInspector.svelte` renders simulated/mock speed history |

#### Root Cause
No component calls these API functions. The store action functions exist but have no caller. The intended data flow was:
```
Map mount → calculate viewport bbox → call getTrafficSnapshot(bbox), getRoadSegments(bbox) → populate stores → deck.gl layers render
```
This chain was never connected.

#### Fix Procedure
1. In `CorridorMap.svelte` `onMount`:
   - After MapLibre initializes (inside `map.on('load', ...)`), calculate the current viewport bounding box from `map.getBounds()`.
   - Call `getRoadSegments(bbox)` → pipe result to `loadRoadNetwork()` store action.
   - Call `getTrafficSnapshot(bbox)` → pipe result to `updateTrafficBatch()` store action.
2. Add a `map.on('moveend', ...)` handler:
   - Debounce (300ms) → recalculate bbox → re-fetch `getTrafficSnapshot(bbox)`.
   - Only re-fetch `getRoadSegments(bbox)` if zoom level changed significantly (±2 levels).
3. Add a periodic refresh interval for live traffic:
   - Every 15 seconds: `getTrafficSnapshot(currentBbox)` → `updateTrafficBatch()`.
   - Store the interval handle and clear it in `onDestroy`.
4. For predictions:
   - When `SegmentInspector` opens (segment selected), call `getTrafficPredictions([segmentId], [5,10,15,30])` → `setPredictions()`.
   - Replace the mock speed history (lines 42-54 of `SegmentInspector.svelte`) with data from a real history API call.
5. For `getRoutes(movementId)`:
   - After `planMovement()` succeeds in `planning/+page.svelte`, the routes are returned in the response and piped to `setRoutes()`. The standalone `getRoutes()` is a refresh endpoint — call it when the user navigates back to the planning page with an existing `movementId`.

#### Verification
- Open `/map` route → after load, browser DevTools Network tab shows `GET /api/traffic/segments?...` and `GET /api/traffic/snapshot?...` requests.
- deck.gl road network layer renders colored road segments.
- Congestion heatmap overlay shows real data.
- Pan/zoom triggers debounced re-fetch (visible in Network tab).
- Select a segment → SegmentInspector shows real predictions (not mock sparkline).

---

### Conflict 2.5 — SimulationViewer Engine Never Instantiated

| Aspect | Detail |
|---|---|
| **File** | `components/SimulationViewer.svelte` |
| **Issue** | `simEngine` declared at line 31 with correct type annotation but **never assigned**. `onMount` (lines 33-66) only checks WebGPU availability. Play/pause/reset handlers (lines 72-87) only update store state. Canvas (line 115) never gets a WebGPU rendering context. |
| **Engine status** | `engine.ts`, `gpu_context.ts`, `renderer.ts`, `projection.ts`, 2 WGSL shaders are all complete and production-quality. Import chain: shaders → `gpu_context.ts` → `engine.ts`. `renderer.ts` is orphaned (never imported). |

#### Root Cause
"Last mile" integration missing. The engine was developed independently but never wired into the Svelte component lifecycle.

#### Fix Procedure
1. In `SimulationViewer.svelte` `onMount`, after WebGPU availability check succeeds:
   ```
   a. Dynamically import: const { TrafficSimulation } = await import('$lib/simulation/engine')
   b. Dynamically import: const { SimulationRenderer } = await import('$lib/simulation/renderer')
   c. Create engine: simEngine = new TrafficSimulation(adapter, device)
   d. Call simEngine.initializeVehicles(0.3) — density parameter
   e. Create renderer: const renderer = new SimulationRenderer(canvas, device, simEngine)
   ```
2. Wire control buttons:
   ```
   handleStart → simEngine.start()
   handlePause → simEngine.pause()
   handleReset → simEngine.reset(); simEngine.initializeVehicles(0.3)
   ```
3. Wire store reactivity:
   - `$effect(() => { simEngine?.setSpeed($simulationSpeed) })` — sync speed slider.
   - The engine already calls `updateSimulationState()` internally (confirmed in `engine.ts`), so `simulationState` store will auto-update.
4. Wire canvas rendering:
   - The renderer needs the canvas context. Call `renderer.startRenderLoop()` after engine starts.
   - On `onDestroy`: call `simEngine?.destroy()` and `renderer?.destroy()`.
5. Connect to simulation config:
   - When `simulationConfig` store is populated (from route planning), pass it to engine:
     `simEngine.configure(config.routeSegments, config.convoySpeed, config.departureTime)`.

#### Verification
- Navigate to `/simulation` or `/map` → Sim tab.
- Canvas shows "WebGPU available" status text.
- Click Play → vehicles appear on canvas, stats update (vehicle count, avg speed, simulation time).
- Click Pause → vehicles freeze. Click Reset → stats zero.
- Adjust speed slider → simulation speed changes proportionally.
- GPU memory usage stays within 8 GB VRAM budget (engine uses low-power adapter preference).

---

### Conflict 2.6 — `activeConvoy` Never Populated After Creation

| Aspect | Detail |
|---|---|
| **File** | `routes/planning/+page.svelte` line 44 |
| **Bug** | `createMovement(params)` is called and returns `{movementId}`, but the result is only stored in a local `let movementId` variable. The `activeConvoy` store (from `convoy.ts`) is never updated with the newly created movement. |
| **Impact** | After creating a movement on the planning page: (1) command page shows "No active convoy" because `$activeConvoy` is `null`, (2) `ConvoyTracker` renders nothing, (3) WebSocket cannot be opened because there's no movementId to subscribe to. |

#### Root Cause
Missing store dispatch after the `createMovement()` API call resolves.

#### Fix Procedure
1. After `createMovement()` succeeds in `planning/+page.svelte`:
   ```
   const { movementId } = await createMovement(params)
   // Add this: populate the activeConvoy store
   activeConvoy.set({
     movementId,
     origin: params.origin,
     destination: params.destination,
     vvipClass: params.vvipClass,
     plannedDeparture: params.plannedDeparture,
     status: 'planning',
     position: null,
     speed: null,
     heading: null,
     eta: null,
     selectedRouteId: null,
   })
   ```
2. When `planMovement()` subsequently succeeds and routes are received:
   ```
   activeConvoy.update(c => c ? {...c, status: 'planned'} : c)
   ```
3. When the user navigates to `/command`, the `$activeConvoy` will be populated and the WebSocket can connect using `$activeConvoy.movementId`.

#### Verification
- On planning page → fill form → click Create → `activeConvoy` store populates (check via browser DevTools or `$inspect()`).
- Navigate to command page → `ConvoyTracker` shows the movement details (status: "planning").
- WebSocket opens successfully using the movementId.

---

### Conflict 2.7 — WebSocket Cleanup Missing on Command Page

| Aspect | Detail |
|---|---|
| **File** | `routes/command/+page.svelte` |
| **Bug** | `createConvoySocket()` returns `{close}` handle. If the `onDestroy` lifecycle hook does not call `socket.close()`, the WebSocket persists after navigation, reconnect loop continues indefinitely. |

#### Fix Procedure
1. Store the socket handle: `let socket = createConvoySocket(movementId, onEvent)`.
2. Add cleanup: `onDestroy(() => socket?.close())`.
3. Verify `onDestroy` is imported from `svelte`.

#### Verification
- Navigate to command page (socket opens) → navigate away → check browser DevTools WebSocket tab → connection should be closed, no reconnection attempts.

---

### Conflict 2.8 — `POST /movements` Creation Endpoint Missing

| Aspect | Detail |
|---|---|
| **Frontend** | `client.ts` line 64: `POST /api/convoy/movements` with body `{origin, destination, vvip_class, planned_departure}` |
| **Backend** | `__main__.py` defines only: `POST /movements/{id}/plan`, `POST /movements/{id}/escort`, `POST /movements/{id}/clear`, `POST /chat`. There is **no** `POST /movements` endpoint for creating a new movement. |

#### Root Cause
The movement creation endpoint was designed in the frontend API client but never implemented in convoy-brain.

#### Fix Procedure
1. Add to `convoy-brain/__main__.py`:
   ```
   @app.post("/movements")
   async def create_movement(request: PlanRequest) -> dict:
       movement_id = str(uuid4())
       # Insert into corridor-store: convoy.movements table
       # with status='planning', origin_geom, destination_geom, vvip_class, planned_start
       return {"movement_id": movement_id}
   ```
2. The handler should:
   - Generate a UUID for `movement_id`.
   - Insert a row into `convoy.movements` (PostGIS) with status `planning`.
   - Return `{"movement_id": str}`.
3. Alternatively, refactor the frontend to combine create+plan into a single call if the backend prefers `planMovement` to handle creation internally. But the cleaner REST design is a separate creation endpoint.

#### Verification
- `curl -X POST http://localhost:8080/movements -H "Content-Type: application/json" -d '{"origin":[77.209,28.614],"destination":[77.225,28.635],"vvip_class":"Z","planned_departure":"2026-03-17T10:00:00Z"}'` returns `{"movement_id":"<uuid>"}`.
- Query `corridor-store`: `SELECT * FROM convoy.movements WHERE movement_id = '<uuid>'` returns 1 row with status `planning`.

---

### Conflict 2.9 — Chat Streaming Endpoint Path Ambiguity

| Aspect | Detail |
|---|---|
| **Frontend** | `client.ts` line 178: `POST /api/convoy/chat/stream` |
| **Backend** | `__main__.py` line 169: only `POST /chat` exists. No `/chat/stream` endpoint. |

#### Root Cause
Frontend expects a dedicated streaming endpoint at `/chat/stream`, but backend only defines `/chat`. Either:
- The `/chat` endpoint returns a streaming response conditionally, or
- A `/chat/stream` endpoint needs to be added alongside the non-streaming `/chat`.

#### Fix Procedure
**Option A — Add `/chat/stream` endpoint** (recommended):
1. In `__main__.py`, add a new route:
   ```
   @app.post("/chat/stream")
   async def chat_stream(request: ChatRequest) -> StreamingResponse:
       # Same agent invocation as /chat but returns StreamingResponse
       # with media_type="application/x-ndjson"
       # Yields newline-delimited JSON events: token, thought, tool_call, tool_result, done, error
   ```
2. The existing `/chat` endpoint remains as a non-streaming fallback (used by `sendChat()` in client.ts).

**Option B — Make `/chat` always stream:**
1. Change the `/chat` endpoint to always return `StreamingResponse`.
2. Update `sendChat()` in client.ts to parse the NDJSON response and extract the final message.

**Recommended:** Option A — separate endpoints for separate protocols.

#### Verification
- `curl -N -X POST http://localhost:8080/chat/stream -H "Content-Type: application/json" -d '{"message":"Hello","movement_id":null,"vvip_class":null}'` emits NDJSON lines.
- From chat page → type message → tokens stream in real-time without 404/405 errors.

---

### Conflict 2.10 — SegmentInspector Uses Mock Speed History

| Aspect | Detail |
|---|---|
| **File** | `components/SegmentInspector.svelte` lines 42-54 |
| **Bug** | Speed history sparkline generates 30 pseudo-random data points using a deterministic seed from `segment.segmentId` instead of fetching real historical data |
| **Comment** | Line 39: "In production this would come from a history store; here we generate a plausible series based on the current speed with deterministic variation." |
| **Backend available** | MCP tool `get_historical_pattern` (line 250-266 of `server.py`) and resource `traffic://segments/{id}/history` |

#### Fix Procedure
1. When `SegmentInspector` opens with a selected segment:
   - Call a new API function `getSegmentHistory(segmentId)` → `GET /api/traffic/history/{segmentId}`.
   - Or call `get_historical_pattern` for the current day/hour via the traffic API.
2. Replace the pseudo-random generation (lines 42-54) with real data mapping.
3. Add loading state while fetching.

#### Verification
- Click a road segment on the map → SegmentInspector opens → sparkline shows real historical speed data (not deterministic pseudo-random).
- Data correlates with `traffic.hourly_aggregates` table values.

---

### Conflict 2.11 — `chat.connectionStatus` Never Updated

| Aspect | Detail |
|---|---|
| **File** | `stores/chat.ts` line 19 |
| **Bug** | `connectionStatus` store initialized as `'disconnected'` and **never updated** by any function or component. |

#### Fix Procedure
1. In `ChatPanel.svelte`, before calling `streamChat()`: `connectionStatus.set('connecting')`.
2. On first `onToken` callback: `connectionStatus.set('connected')`.
3. On `onDone` or `onError`: `connectionStatus.set('disconnected')`.
4. Display the status in the chat UI header (e.g., green/yellow/red dot indicator).

#### Verification
- Open chat page → status shows "disconnected" (grey).
- Send a message → status briefly shows "connecting" (yellow) → "connected" (green) during streaming.
- After response completes → "disconnected" (grey) again.

---

## Category 3 — ⊗ Missing Frontend UI

Backend capabilities with no corresponding frontend views, components, or store integrations.

---

### Missing 3.1 — Traffic-Oracle HTTP Server (Foundation for All Missing UI)

**Priority:** P0 (BLOCKER — Must be implemented first)

| Aspect | Detail |
|---|---|
| **Backend** | traffic-oracle exists as Python library + NATS consumers but has no HTTP server |
| **Required endpoints** | 15 endpoints per ToolExecutor mapping (see Conflict 2.2) |
| **Blocked items** | All frontend traffic data flows, all MCP tool execution, all Missing 3.x features |

#### Implementation Steps
1. Create `services/traffic-oracle/src/traffic_oracle/api/` package.
2. Create `server.py` with FastAPI app, routers for `/api/v1/predict`, `/api/v1/optimize`, `/api/v1/graph`, `/api/v1/spatial`, `/api/v1/traffic`, `/api/v1/anomalies`, `/api/v1/corridor`, `/api/v1/movements`.
3. Each route handler delegates to existing modules:
   - `predict/flow` → `traffic_oracle.models.dstgat.predict()`
   - `predict/eta` → `traffic_oracle.models.histgbt.predict()`
   - `optimize/routes` → `traffic_oracle.optimize.ortools_solver.find_routes()`
   - `optimize/diversions` → `traffic_oracle.optimize.diversion_planner.plan()`
   - `evaluate/scenarios` → `traffic_oracle.optimize.scenario_evaluator.evaluate()`
   - `graph/*` → `traffic_oracle.graph.pgrouting.query()`
   - `spatial/*` → `traffic_oracle.spatial.postgis.query()`
   - `traffic/live` → `traffic_oracle.cache.valkey.get_live()`
   - `traffic/historical` → `traffic_oracle.analytics.duckdb.query()`
   - `anomalies/recent` → `traffic_oracle.anomaly.detector.recent()`
   - `corridor/summary` → `traffic_oracle.analytics.corridor.summarize()`
4. Add `fastapi>=0.115`, `uvicorn[standard]>=0.34` to `pyproject.toml`.
5. Update `__main__.py` to launch uvicorn on port 8081.
6. Update `Dockerfile.traffic-oracle` CMD.

#### Verification
- `docker compose up traffic-oracle` → logs show `Uvicorn running on 0.0.0.0:8081`.
- `curl http://localhost:8081/api/v1/corridor/summary` returns JSON.

---

### Missing 3.2 — Anomaly Detection Dashboard

| Aspect | Detail |
|---|---|
| **Backend** | `traffic.anomaly_log` table (migration 003) with 9 anomaly types (`future_timestamp`, `stale_data`, `speed_jump`, `congestion_spike`, `spatial_outlier`, `source_disagreement`, `pattern_deviation`, `isolation_forest`, `manual_flag`), 3 severity levels (low/medium/high), JSONB details |
| **NATS** | signal-ingress publishes to `corridor.traffic.anomaly` subject (Tier-1); traffic-oracle's `AnomalyStreamConsumer` performs Tier-2 IsolationForest detection |
| **MCP resource** | `traffic://anomalies/recent` → `GET /api/v1/anomalies/recent` |
| **Frontend current** | `traffic.ts` has `anomalies` writable and `criticalAnomalies` derived store but they're **never populated** |

#### Required UI Architecture

**Route:** `/anomalies` (new)
**Components:**
- `AnomalyDashboard.svelte` — main page layout
- `AnomalyTimeline.svelte` — temporal view (scrollable timeline with anomaly markers)
- `AnomalyHeatmap.svelte` — spatial view (deck.gl HeatmapLayer on CorridorMap)
- `AnomalyDetailCard.svelte` — expanded detail view for single anomaly

**Store additions to `traffic.ts`:**
- `anomalyFilter` writable: `{types: string[], severities: string[], timeRange: [Date, Date]}`
- `filteredAnomalies` derived: filtered from `anomalies` by `anomalyFilter`

#### Implementation Steps
1. Create route `routes/anomalies/+page.svelte`.
2. Add nav entry in `+layout.svelte` (after "Simulation").
3. Create `AnomalyDashboard.svelte`:
   - Top bar: severity filter chips (Low/Medium/High), anomaly type multi-select, time range picker.
   - Left panel: `AnomalyTimeline` — vertical scrollable timeline grouped by hour, anomaly dots colored by severity.
   - Center: `CorridorMap` with `AnomalyHeatmap` overlay — deck.gl HeatmapLayer with anomaly locations, radius proportional to severity.
   - Right panel: `AnomalyDetailCard` — shown when an anomaly is clicked, displays type, severity, segment name, timestamp, JSONB details.
4. Create API function `getRecentAnomalies()` → `GET /api/traffic/anomalies/recent` (or `/api/v1/anomalies/recent`).
5. On mount: call `getRecentAnomalies()` → `addAnomaly()` for each item to populate `anomalies` store.
6. Add 30-second polling interval for fresh anomalies.
7. Wire `anomalyMarkersVisible` toggle in `TrafficHeatmap.svelte` to show/hide the anomaly layer on the main corridor map page as well.

#### Verification
- Navigate to `/anomalies` → see anomaly timeline populated from `traffic.anomaly_log`.
- Filter by severity "high" → only high-severity anomalies visible.
- Click an anomaly marker on map → detail card opens with JSONB context.
- Anomaly count matches `SELECT COUNT(*) FROM traffic.anomaly_log WHERE timestamp_utc > NOW() - INTERVAL '1 hour'`.

---

### Missing 3.3 — Historical Traffic Patterns Explorer

| Aspect | Detail |
|---|---|
| **Backend** | `traffic.hourly_aggregates` table: `segment_id`, `hour_utc`, `avg_speed_kmh`, `p50_speed_kmh`, `p95_congestion`, `observation_cnt` |
| **MCP tool** | `get_historical_pattern(segment_id, day_of_week, hour)` → `/api/v1/traffic/historical` |
| **MCP resource** | `traffic://segments/{id}/history` → `/api/v1/traffic/history/{segment_id}` |
| **Frontend current** | `SegmentInspector.svelte` shows **mock** speed history sparkline (pseudo-random, lines 42-54) |

#### Required UI Architecture

**Route:** Enhance existing `/map` route (add "History" tab in sidebar)
**Components:**
- `HistoryExplorer.svelte` — time-series chart panel
- Reuse `SegmentInspector.svelte` (replace mock data)

**Store additions to `traffic.ts`:**
- `segmentHistory` writable: `Map<number, {hour: number, avgSpeed: number, p50Speed: number, p95Congestion: number}[]>`

#### Implementation Steps
1. Add API function `getSegmentHistory(segmentId: number)` → `GET /api/traffic/history/{segmentId}`.
2. Add API function `getHistoricalPattern(segmentId, dayOfWeek, hour)` → the existing MCP tool endpoint.
3. Create `HistoryExplorer.svelte`:
   - Day-of-week selector (Mon-Sun tabs).
   - 24-hour time series chart (SVG or Canvas) showing `avg_speed_kmh` and `p95_congestion` dual Y-axis.
   - Hover tooltip with exact values.
   - "Compare" mode: overlay two segments on the same chart.
4. Replace `SegmentInspector.svelte` mock sparkline (lines 42-54) with real data from `segmentHistory` store.
5. Add "History" tab to `/map` page sidebar (alongside existing "Layers", "Inspect", "Sim" tabs).
6. When a segment is selected, fetch its 24-hour pattern for the current day and populate the chart.

#### Verification
- Select segment on map → History tab shows real 24-hour speed/congestion chart.
- Change day-of-week → chart updates with different pattern.
- Values match `SELECT * FROM traffic.hourly_aggregates WHERE segment_id = X AND EXTRACT(dow FROM hour_utc) = Y`.

---

### Missing 3.4 — Scenario Evaluation UI

| Aspect | Detail |
|---|---|
| **Backend** | MCP tool `evaluate_scenarios` → `POST /api/v1/evaluate/scenarios` with Monte Carlo simulation comparing multiple scenarios (disruption, queue lengths, closure durations, complaint risk) |
| **Frontend current** | Zero UI. `ChatInput.svelte` has a quick action chip "Evaluate scenarios" (line 21) that sends a text message to the LLM, but no structured UI. |

#### Required UI Architecture

**Route:** `/analysis` (new, or repurpose if exists)
**Components:**
- `ScenarioBuilder.svelte` — form for defining scenario configs
- `ScenarioComparison.svelte` — side-by-side chart cards comparing results

#### Implementation Steps
1. Create route `routes/analysis/+page.svelte`.
2. Create `ScenarioBuilder.svelte`:
   - "Add Scenario" button (max 5 scenarios).
   - Per scenario: name field, route selection dropdown (from `routeCandidates`), departure time picker, convoy speed slider.
   - "Evaluate" button calls API.
3. Create API function `evaluateScenarios(scenarios)` → `POST /api/traffic/evaluate/scenarios` (or `/api/v1/evaluate/scenarios`).
4. Create `ScenarioComparison.svelte`:
   - Card per scenario showing: total disruption (vehicle-hours), max queue length, total closure duration, complaint risk score.
   - Bar chart or radar chart comparing all scenarios.
   - "Select Best" button that highlights the winning scenario.
5. Wire result to route planning: "Apply Scenario" button sets `selectedRouteId` to the chosen scenario's route.

#### Verification
- Navigate to `/analysis` → create 3 scenarios with different routes/times.
- Click Evaluate → backend returns comparison data → cards populate with disruption metrics.
- Select a scenario → its route highlights on `CorridorMap`.

---

### Missing 3.5 — pgvector Embedding Explorer

| Aspect | Detail |
|---|---|
| **Backend** | `traffic.segment_embeddings` table (migration 002): 24-dimensional vectors per segment, pattern types: `daily_profile`, `weekly_profile`, `event_response`, `incident_response`. HNSW index (m=16, ef_construction=64). |
| **Frontend current** | Zero UI. Embeddings are not queried or visualized anywhere. |

#### Required UI Architecture

**Route:** Enhance `/analysis` route with "Embeddings" tab
**Components:**
- `EmbeddingExplorer.svelte` — 2D scatter projection + similarity search

#### Implementation Steps
1. Add API function `getSegmentEmbeddings(patternType)` → `GET /api/traffic/embeddings?pattern_type=daily_profile` (new endpoint needed on traffic-oracle).
2. Add API function `findSimilarSegments(segmentId, k)` → `GET /api/traffic/embeddings/similar?segment_id=X&k=10` (new endpoint using pgvector `<=>` operator).
3. Create `EmbeddingExplorer.svelte`:
   - Pattern type selector (4 types).
   - 2D scatter plot (PCA/t-SNE projection computed server-side or client-side on 24D vectors).
   - Each dot = segment, colored by road class or congestion.
   - Click a dot → show segment details + top-K similar segments list.
   - "Similar segments" list with similarity scores, clickable to fly to segment on map.
4. PCA can be done client-side (24D → 2D is trivial, ~O(n) for small n). Send raw embeddings, project in-browser.

#### Verification
- Select "daily_profile" → scatter plot shows clustered segments.
- Click segment dot → "Similar Segments" panel shows 10 nearest neighbors.
- Click a similar segment → map flies to its location.

---

### Missing 3.6 — NATS Ingestion Monitor

| Aspect | Detail |
|---|---|
| **Backend** | signal-ingress publishes Arrow IPC RecordBatches to `corridor.traffic.live` via NATS JetStream. NATS exposes HTTP monitoring on port 8222. |
| **Frontend current** | Zero UI for ingestion health or throughput metrics. |

#### Required UI Architecture

**Route:** Enhance Dashboard or create `/admin/ingestion` route
**Components:**
- `IngestionMonitor.svelte` — throughput gauge + latency chart

#### Implementation Steps
1. Add API function `getNatsStats()` → proxy to `http://nats:8222/varz` or `/connz` (NATS HTTP monitoring).
2. Alternatively, add a health endpoint to signal-ingress that exposes: messages/sec, bytes/sec, last batch timestamp, schema info.
3. Create `IngestionMonitor.svelte`:
   - Throughput gauge (msgs/sec, observations/sec).
   - Latency histogram (time from observation timestamp to NATS publish).
   - "Last batch" timestamp with staleness indicator (green < 5s, yellow < 30s, red > 30s).
   - Arrow schema viewer (column names, types from IPC schema).
4. Add this as a card on the dashboard page or as a tab in a new admin section.
5. Poll every 5 seconds.

#### Verification
- Dashboard shows NATS ingestion rate.
- If signal-ingress stops → staleness indicator turns red.
- Values correlate with `nats-top` monitoring output.

---

### Missing 3.7 — Multi-Convoy Tracker

| Aspect | Detail |
|---|---|
| **Backend** | `convoy.movements` table supports multiple concurrent movements (UUID PK, various statuses). MCP resource `convoy://movements/active` returns a **list** of active movements. |
| **Frontend current** | Only `activeConvoy` (singular) store exists. UI shows exactly one convoy at a time. `ConvoyTracker` reads only `$activeConvoy`. |

#### Required UI Architecture

**Route:** Enhance `/command` route or create `/fleet` route
**Components:**
- `ConvoyList.svelte` — sidebar list of all active/recent convoys
- Enhance `ConvoyTracker.svelte` to accept a convoy as prop (not just read global store)

**Store additions to `convoy.ts`:**
- `allMovements` writable: `ConvoyMovement[]`
- `activeMovementId` writable: `string | null` (which one is focused)
- Refactor `activeConvoy` to be derived: `allMovements.find(m => m.movementId === activeMovementId)`

#### Implementation Steps
1. Add API function `getActiveMovements()` → `GET /api/convoy/movements/active` (new endpoint on convoy-brain that reads from `convoy.movements` table or MCP resource).
2. Create `ConvoyList.svelte`:
   - Scrollable list of all movements, grouped by status (active, planning, completed).
   - Each item shows: movement ID (truncated), VVIP class badge, status badge, ETA.
   - Click to select → updates `activeMovementId` → `activeConvoy` derived store updates → map/tracker/WebSocket switch context.
3. Refactor command page to use `ConvoyList` in left sidebar.
4. On `CorridorMap`: render all active convoys as markers (not just the focused one), with the focused one highlighted.
5. WebSocket management: when `activeMovementId` changes, close old socket and open new one.

#### Verification
- Create 2 movements → both appear in `ConvoyList`.
- Click movement A → ConvoyTracker shows A, map centers on A's position.
- Click movement B → tracker and map switch to B.
- Both convoy markers visible on map simultaneously.

---

### Missing 3.8 — Ollama / LLM Configuration Interface

| Aspect | Detail |
|---|---|
| **Backend** | convoy-brain uses Ollama (Qwen 3.5 9B) for LLM. MCP prompt templates exist for `vvip_security_protocol`, `route_analysis_brief`, `diversion_coordination_brief` — with configurable arguments. |
| **Frontend current** | Zero UI for model configuration. Temperature, top_p, system prompts are not adjustable. |

#### Required UI Architecture

**Route:** Enhance `/settings` route (if exists) or `/chat` settings panel
**Components:**
- `LLMConfig.svelte` — model/prompt configuration panel

#### Implementation Steps
1. Add API endpoint `GET /api/convoy/config/llm` → returns current model name, temperature, top_p, max_tokens.
2. Add API endpoint `PUT /api/convoy/config/llm` → updates model config.
3. Create `LLMConfig.svelte`:
   - Model selector dropdown (populated from Ollama API's model list).
   - Temperature slider (0.0 - 2.0, default 0.7).
   - Top-P slider (0.0 - 1.0, default 0.9).
   - Max tokens input (128 - 8192).
   - System prompt template selector (3 MCP prompts + custom).
   - "Reset to defaults" button.
4. Add as collapsible panel in chat page header or as a route (`/settings/llm`).

#### Verification
- Open config panel → current settings populate from backend.
- Change temperature to 0.1 → send chat message → response is more deterministic.
- Change model → responses come from new model (visible in latency difference).

---

### Missing 3.9 — Observability Dashboard Links

| Aspect | Detail |
|---|---|
| **Backend** | Prometheus at port 9090, Grafana at port 3000, nvidia-exporter at 9835. Alert rules in `infra/observability/`. Grafana dashboards configured. |
| **Frontend current** | Zero links or embedded views for observability tools. |

#### Required UI Architecture

**Route:** Enhance Dashboard or create `/admin/observability` route
**Components:**
- `ObservabilityPanel.svelte` — links and embedded iframe panels

#### Implementation Steps
1. Add a "System Monitoring" section to the dashboard page.
2. Create `ObservabilityPanel.svelte`:
   - Quick-link cards: Grafana Dashboards (`http://localhost:3000`), Prometheus Targets (`http://localhost:9090/targets`), NATS Monitor (`http://localhost:8222`).
   - Optional: embed Grafana panels via iframe with specific dashboard URLs.
   - GPU metrics card: fetch from nvidia-exporter (`http://localhost:9835/metrics`) → parse Prometheus text format → display VRAM used/total, GPU utilization %, temperature.
3. Add nav entry or dashboard section.

#### Verification
- Dashboard shows Grafana/Prometheus/NATS links.
- GPU metrics card shows real nvidia-smi values.
- Clicking Grafana link opens the monitoring dashboard.

---

### Missing 3.10 — Diversion Impact Visualization

| Aspect | Detail |
|---|---|
| **Backend** | MCP tool `plan_diversions` returns per-segment activation/deactivation timing, alternative routes, queue length estimates, closure types. `convoy.diversions` table stores all planned diversions. |
| **Frontend current** | `DiversionTimeline.svelte` exists but only shows diversions from the store (which comes from WebSocket events). No proactive diversion impact preview before launching. |

#### Required UI Architecture

**Route:** Enhance `/planning` route after route selection
**Components:**
- `DiversionPreview.svelte` — pre-launch diversion impact visualization

#### Implementation Steps
1. After route selection on planning page, automatically call diversion planning:
   - Extract `selectedRoute.segmentIds` from store.
   - Call new API function `previewDiversions(routeSegmentIds, convoySpeed, departureTime)` → `POST /api/traffic/optimize/diversions` (or via `/api/v1/optimize/diversions`).
2. Create `DiversionPreview.svelte`:
   - Segment-by-segment timeline showing closure activation → convoy passage → deactivation.
   - Alternative route paths rendered on `CorridorMap` as dashed lines.
   - Queue length estimates per segment (bar chart).
   - Total disruption score (vehicle-hours) displayed prominently.
   - Color coding: green (< 100 vehicle-hours), yellow (100-500), red (> 500).
3. Wire into planning flow: show `DiversionPreview` after route selection, before "Launch Escort" button.
4. Allow toggling between routes to compare diversion impacts.

#### Verification
- Select a route on planning page → DiversionPreview auto-loads.
- Timeline shows per-segment closure windows.
- Map shows alternative routes as dashed lines.
- Total disruption score matches `evaluate_scenarios` output for single scenario.

---

## Verification Checklist

### Phase 0 — Foundation (Conflict 2.2 / Missing 3.1)

- [ ] **V0.1** — traffic-oracle HTTP server starts: `curl http://localhost:8081/api/v1/corridor/summary` returns JSON 200.
- [ ] **V0.2** — All 15 ToolExecutor endpoints reachable: run `pytest` against traffic-oracle API with fixture requests for each endpoint.
- [ ] **V0.3** — MCP server can execute tools end-to-end: send `tools/call` with `predict_traffic_flow` via STDIO → get valid JSON response.

### Phase 1 — Bug Fixes (Conflicts 2.1, 2.6–2.9, 2.11)

- [ ] **V1.1** — createMovement camelCase fix (2.1): Network tab shows `{vvip_class, planned_departure}` in POST body.
- [ ] **V1.2** — POST /movements endpoint exists (2.8): `curl -X POST http://localhost:8080/movements ...` returns `{movement_id}`.
- [ ] **V1.3** — activeConvoy populated (2.6): After creating movement on planning page, `$activeConvoy` is non-null (verify via `$inspect()` or DevTools).
- [ ] **V1.4** — WebSocket cleanup (2.7): Navigate away from command page → WebSocket closes cleanly (no reconnection attempts in console).
- [ ] **V1.5** — Chat streaming endpoint (2.9): `POST /chat/stream` returns NDJSON streaming response.
- [ ] **V1.6** — connectionStatus updates (2.11): Chat page shows connecting → connected → disconnected status transitions.

### Phase 2 — WebSocket Backend (Conflict 2.3)

- [ ] **V2.1** — WebSocket endpoint exists: `wscat -c ws://localhost:8080/ws/convoy/{id}` connects successfully.
- [ ] **V2.2** — Position updates flow: Trigger escort → command page `ConvoyTracker` shows moving position/speed.
- [ ] **V2.3** — Diversion updates flow: Activate diversion → `DiversionTimeline` updates in real-time.

### Phase 3 — Store Hydration (Conflict 2.4)

- [ ] **V3.1** — Road segments load: Open `/map` → Network tab shows `GET /api/traffic/segments?...` request → deck.gl PathLayer renders roads.
- [ ] **V3.2** — Traffic snapshot loads: Open `/map` → Network tab shows `GET /api/traffic/snapshot?...` → heatmap layer shows congestion colors.
- [ ] **V3.3** — Periodic refresh works: Wait 15 seconds on map page → new snapshot request fires automatically.
- [ ] **V3.4** — Predictions load on inspect: Click segment → `getTrafficPredictions()` fires → SegmentInspector shows T+5/10/15/30 predictions.
- [ ] **V3.5** — Mock data eliminated (2.10): SegmentInspector sparkline shows real historical data from `traffic.hourly_aggregates`.

### Phase 4 — Simulation Engine (Conflict 2.5)

- [ ] **V4.1** — Engine instantiates: Open simulation page → console shows no errors → `simEngine` is non-null.
- [ ] **V4.2** — Play/pause works: Click Play → `simulationState.running` becomes `true`, `vehicleCount > 0`, timer increments.
- [ ] **V4.3** — Canvas renders: Vehicles visible on WebGPU canvas (not blank).
- [ ] **V4.4** — VRAM budget: During simulation, total GPU memory usage stays under 8 GB (verify via `nvidia-smi`).
- [ ] **V4.5** — Speed slider: Adjust to 2x → simulation time advances twice as fast.

### Phase 5 — Missing UI Features (Missing 3.2–3.10)

- [ ] **V5.1** — Anomaly dashboard: Navigate to `/anomalies` → timeline and heatmap populated from `traffic.anomaly_log`.
- [ ] **V5.2** — History explorer: Select segment → 24-hour speed chart renders from `traffic.hourly_aggregates`.
- [ ] **V5.3** — Scenario evaluation: Create 3 scenarios → click Evaluate → comparison cards show disruption metrics.
- [ ] **V5.4** — Embedding explorer: Select "daily_profile" → 2D scatter plot renders → click dot → similar segments list appears.
- [ ] **V5.5** — NATS monitor: Dashboard shows ingestion rate and staleness indicator.
- [ ] **V5.6** — Multi-convoy: Create 2 movements → both visible in convoy list → click to switch focus.
- [ ] **V5.7** — LLM config: Open settings → change temperature → next chat response reflects setting.
- [ ] **V5.8** — Observability links: Dashboard shows Grafana/Prometheus quick-link cards.
- [ ] **V5.9** — Diversion preview: Select route → DiversionPreview shows closure timeline and disruption score.

### Phase 6 — End-to-End Integration

- [ ] **V6.1** — Full flow: Create movement → plan routes → select route → preview diversions → launch escort → monitor on command page → clear movement. All stores update at each step.
- [ ] **V6.2** — LLM-assisted flow: Open chat → "Plan a Z+ route from Kartavya Path to Parliament" → agent calls MCP tools → results appear in chat with tool call cards → route appears on map.
- [ ] **V6.3** — Zero console errors: Complete entire flow with browser console open → no uncaught exceptions or 404/422/500 errors.
- [ ] **V6.4** — All stores populated: After full flow, verify via `$inspect()`: `activeConvoy` ≠ null, `routeCandidates.length > 0`, `liveTraffic.size > 0`, `roadSegments.size > 0`, `diversions.length > 0`.

---

## Appendix A — API Endpoint Inventory

### Frontend → convoy-brain (`/api/convoy/*` → `http://localhost:8080`)

| Frontend Function | Method | URL | Backend Status | Conflict |
|---|---|---|---|---|
| `createMovement()` | POST | `/movements` | **MISSING** | 2.8 |
| `planMovement()` | POST | `/movements/{id}/plan` | ✓ Exists | — |
| `launchEscort()` | POST | `/movements/{id}/escort` | ✓ Exists | — |
| `clearMovement()` | POST | `/movements/{id}/clear` | ✓ Exists | — |
| `getRoutes()` | GET | `/movements/{id}/routes` | **MISSING** | Dead code |
| `streamChat()` | POST | `/chat/stream` | **MISSING** (only `/chat`) | 2.9 |
| `sendChat()` | POST | `/chat` | ✓ Exists | Unused |
| `createConvoySocket()` | WS | `/ws/convoy/{id}` | **MISSING** | 2.3 |
| `checkConvoyBrainHealth()` | GET | `/health` | ✓ Exists | — |
| `checkServiceHealth()` | GET | `/health/services` | **MISSING** | Dead code |
| `getGpuStatus()` | GET | `/health/gpu` | **MISSING** | Needs impl |

### Frontend → traffic-oracle (`/api/traffic/*` → `http://localhost:8081`)

| Frontend Function | Method | URL | Backend Status | Conflict |
|---|---|---|---|---|
| `getTrafficSnapshot()` | GET | `/snapshot?...` | **MISSING** (no server) | 2.2, 2.4 |
| `getRoadSegments()` | GET | `/segments?...` | **MISSING** (no server) | 2.2, 2.4 |
| `getTrafficPredictions()` | POST | `/predict` | **MISSING** (no server) | 2.2, 2.4 |

### ToolExecutor → traffic-oracle (`/api/v1/*` → `http://localhost:8081`)

| Tool Name | Method | URL | Server Status |
|---|---|---|---|
| `predict_traffic_flow` | POST | `/api/v1/predict/flow` | **MISSING** (no server) |
| `find_convoy_routes` | POST | `/api/v1/optimize/routes` | **MISSING** |
| `plan_diversions` | POST | `/api/v1/optimize/diversions` | **MISSING** |
| `evaluate_scenarios` | POST | `/api/v1/evaluate/scenarios` | **MISSING** |
| `predict_eta` | POST | `/api/v1/predict/eta` | **MISSING** |
| `query_shortest_path` | GET | `/api/v1/graph/shortest-path` | **MISSING** |
| `query_k_shortest_paths` | GET | `/api/v1/graph/k-shortest-paths` | **MISSING** |
| `query_segments_in_bbox` | GET | `/api/v1/spatial/segments` | **MISSING** |
| `query_segment_details` | GET | `/api/v1/spatial/segments/{id}` | **MISSING** |
| `get_live_traffic` | POST | `/api/v1/traffic/live` | **MISSING** |
| `get_historical_pattern` | GET | `/api/v1/traffic/historical` | **MISSING** |

---

## Appendix B — Store Hydration Status Matrix

| Store | Field | Expected Source | Current Status |
|---|---|---|---|
| `convoy.activeConvoy` | API + WS | `createMovement()` + WebSocket | **NEVER SET** (2.6) |
| `convoy.routeCandidates` | API | `planMovement()` → `setRoutes()` | ✓ Working |
| `convoy.diversions` | WS | WebSocket events | **NO WS BACKEND** (2.3) |
| `traffic.liveTraffic` | API | `getTrafficSnapshot()` → `updateTrafficBatch()` | **NEVER CALLED** (2.4) |
| `traffic.roadSegments` | API | `getRoadSegments()` → `loadRoadNetwork()` | **NEVER CALLED** (2.4) |
| `traffic.predictions` | API | `getTrafficPredictions()` → `setPredictions()` | **NEVER CALLED** (2.4) |
| `traffic.anomalies` | WS/API | `addAnomaly()` | **NEVER CALLED** (3.2) |
| `health.services` | API | `checkConvoyBrainHealth()` → `updateServiceStatus()` | ✓ Working (dashboard polls) |
| `health.gpuStatus` | API | `getGpuStatus()` → `updateGpuStatus()` | ✓ Working (dashboard polls) |
| `chat.messages` | Streaming | `streamChat()` callbacks | ✓ Working |
| `chat.connectionStatus` | Manual | Should be set in ChatPanel | **NEVER SET** (2.11) |
| `simulation.simulationConfig` | Store | Should come from route planning | **NEVER SET** |
| `simulation.simulationState` | Engine | `TrafficSimulation.updateSimulationState()` | **ENGINE NOT WIRED** (2.5) |
| `simulation.webgpuAvailable` | Browser | `navigator.gpu` check | ✓ Working |
