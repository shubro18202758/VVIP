<p align="center">
  <img src="https://img.shields.io/badge/VVIP-Convoy%20Command-ea580c?style=for-the-badge&logoColor=white&labelColor=1e293b" alt="VVIP Convoy Command" />
</p>

<h1 align="center">
  🚨 VVIP Convoy Orchestration Platform
</h1>

<p align="center">
  <strong>AI-Powered Intelligent Mobility & Corridor Optimization for High-Security Convoy Movements</strong>
</p>

<p align="center">
  <em>Reducing public traffic disruption by up to 6× while maintaining absolute security protocols</em>
</p>

<br/>

<p align="center">
  <img src="https://img.shields.io/badge/Status-Prototype%20Ready-16a34a?style=flat-square" />
  <img src="https://img.shields.io/badge/AI%20Engine-Qwen%203.5%209B-7c3aed?style=flat-square" />
  <img src="https://img.shields.io/badge/Edge%20Deployment-RTX%204070%208GB-76b900?style=flat-square" />
  <img src="https://img.shields.io/badge/Architecture-5%20Microservices-2563eb?style=flat-square" />
  <img src="https://img.shields.io/badge/Language-Python%20|%20Rust%20|%20TypeScript-f97316?style=flat-square" />
  <img src="https://img.shields.io/badge/License-MIT-64748b?style=flat-square" />
</p>

<br/>

<p align="center">
  <a href="#-the-problem">The Problem</a> •
  <a href="#-the-solution">The Solution</a> •
  <a href="#-how-it-works">How It Works</a> •
  <a href="#-key-capabilities">Key Capabilities</a> •
  <a href="#-system-architecture">Architecture</a> •
  <a href="#-project-structure">Project Structure</a> •
  <a href="#-getting-started">Getting Started</a> •
  <a href="#-technical-documentation">Tech Docs</a>
</p>

---

<br/>

<img width="3199" height="1788" alt="Screenshot 2026-03-19 233116" src="https://github.com/user-attachments/assets/0195417e-fe92-40b1-a7cc-9f06bec067b1" />

## 🔴 The Problem

> *"Every day across India's cities, thousands of commuters are stranded — sometimes for 30+ minutes — as roads are locked down far in advance for VVIP convoy passages."*

When a high-security convoy moves through a city corridor, the current protocol is blunt and manual:

| Current Reality | Impact |
|---|---|
| 🚫 Roads blocked **15–30 minutes** before convoy arrival | Massive traffic queues stretching kilometers |
| 📞 Coordination happens over **phone calls & radio** | Delays, miscommunication, missed signals |
| 🗺️ Routes chosen by **experience, not data** | Sub-optimal paths through congested zones |
| 🚑 Emergency vehicles **cannot request corridor access** | Ambulances stuck behind security blockades |
| 👮 **Five agencies** coordinate without shared visibility | Traffic police, transport, security, municipal, emergency — all disconnected |

The cost is enormous: **gridlocked cities, delayed emergency response, frustrated commuters, and exhausted security personnel** — all because convoy planning relies on decades-old manual coordination methods.

<br/>

## 💡 The Solution

**VVIP Convoy Command** is an intelligent edge-deployed platform that transforms convoy operations from reactive road-blocking into **predictive, AI-optimized corridor management**.

Instead of shutting down roads 30 minutes early, the platform:

- 🧠 **Predicts** traffic conditions 5–30 minutes into the future using neural networks trained on real traffic patterns
- 🛣️ **Optimizes** convoy routes in real-time, scoring every possible path for speed, disruption, and security
- ⏱️ **Activates diversions just 60–180 seconds before** the convoy reaches each segment — not minutes
- 🤖 **Deploys an AI command agent** (powered by Qwen 3.5) that reasons through complex multi-agency decisions like a seasoned operations officer
- 📡 **Provides a unified command dashboard** where every agency sees the same live picture

The result? **Traffic disruption reduced by up to 6×**, convoy security maintained, and emergency vehicles can dynamically request corridor clearance.

<br/>

---

<br/>
<img width="580" height="1499" alt="Screenshot 2026-03-19 232948" src="https://github.com/user-attachments/assets/176bb2f1-3cfe-4900-a492-2f09a61731a8" />

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

The AI agent evaluates every candidate route against security classification rules — a Z+ convoy (highest security) requires 6-lane roads with full closure capability, while a Y-class movement only needs signal priority. The system automatically rejects routes that violate these constraints and explains why.

<br/>

### Phase 2 — Live Escort 🚔

> *Real-time convoy tracking and dynamic response*

As the convoy moves:

- **GPS tracking** updates convoy position every second
- **Traffic predictions** run continuously at T+5, T+10, T+15, and T+30 minutes
- **Diversions activate** precisely as the convoy approaches each road segment
- **Congestion spikes** are detected instantly — if a planned route suddenly jams, the system recommends live rerouting
- **Signal controllers** optimize traffic lights in the convoy's path using reinforcement learning
- **Emergency requests** from ambulances are processed and corridor windows are opened when safe

The AI agent monitors 1,800+ data points per 30-minute escort, making autonomous decisions on diversion timing, signal overrides, and contingency routing.

<br/>

### Phase 3 — Post-Clearance Recovery 🔄

> *After the convoy passes — bringing the city back to normal*

Most convoy systems stop at escort completion. This platform goes further:

- **Diversions deactivate** segment-by-segment as the convoy clears each zone
- **Recovery monitoring** tracks how quickly traffic returns to normal flow
- **Congestion analysis** identifies which segments are still jammed and why
- **After-action reporting** generates a complete movement report — total disruption time, decisions made, alerts handled, recovery statistics

<br/>

---

<br/>
<img width="591" height="1451" alt="Screenshot 2026-03-19 233020" src="https://github.com/user-attachments/assets/9bff891d-6037-488e-a0b3-c53466fbfda4" />  <img width="595" height="1691" alt="Screenshot 2026-03-19 233049" src="https://github.com/user-attachments/assets/9346d54b-5a1b-46c3-83d6-17641a170448" />


## 🏗️ Key Capabilities

### 🧠 AI-Powered Decision Making

The platform's brain is a **Qwen 3.5 9B language model** running locally on the edge device — no cloud dependency, no internet requirement, complete operational security.

Four specialized AI agents collaborate through structured workflows:

| Agent | Role | Think of it as... |
|---|---|---|
| 🔍 **Traffic Analyst** | Reads the road — congestion patterns, anomalies, trends | *"The intelligence officer scanning the terrain"* |
| 🗺️ **Route Planner** | Finds the best path — scores, ranks, and recommends routes | *"The navigation expert with a chess player's foresight"* |
| 🔒 **Security Liaison** | Validates compliance — lane widths, closure types, crowd risks | *"The security chief ensuring protocol compliance"* |
| 📡 **Diversion Coordinator** | Orchestrates ground ops — agency assignments, timing, sequences | *"The field commander coordinating five agencies simultaneously"* |

These agents don't just follow scripts — they **reason**. Each decision includes a chain-of-thought explanation that operators can review, building trust and enabling oversight.

<br/>

### 🛣️ Intelligent Route Optimization

Routes aren't picked randomly or by shortest distance. The optimizer evaluates every candidate on four weighted dimensions:

| Factor | Weight | What It Measures |
|---|---|---|
| **Travel Time** | 30% | Predicted travel duration using ML forecasts |
| **Traffic Disruption** | 40% | How many commuters will be affected |
| **Security Compliance** | 20% | Road class, lane count, known risk zones |
| **Route Complexity** | 10% | Number of turns, signals, junctions |

The result: routes that are fast for the convoy AND minimize suffering for the public.

<br/>

### 📊 Neural Traffic Forecasting

The **DSTGAT** (Dynamic Spatio-Temporal Graph Attention Network) predicts traffic conditions across the entire road network:

- **4 forecast horizons**: 5, 10, 15, and 30 minutes ahead
- **Graph-aware**: Understands how congestion on one road affects neighboring roads
- **Temporal patterns**: Learns daily and weekly rhythms — morning rush, evening rush, weekend patterns
- **Anomaly detection**: Flags unusual conditions (accidents, events, sensor failures) in real-time

<br/>

### 🖥️ Unified Command Dashboard

A single-screen command interface designed for **24/7 control room operations**:

- **Left Panel** — Convoy planning: VVIP profile, route selection, vehicle assignment
- **Center Map** — Live geospatial view with route overlays, checkpoint markers, congestion heatmaps
- **Right Panel** — Operations monitor with 5 tabs:
  - 📡 **LIVE** — Real-time convoy tracking and active diversions
  - 🛣️ **CORRIDOR** — Road network health, segment analytics
  - 🔍 **INTEL** — Anomaly reports, historical patterns
  - 🔮 **PREDICT** — AI forecasts with chain-of-thought reasoning
  - 📻 **COMMS** — Inter-agency coordination channel

<br/>

### 🚑 Emergency Corridor Access

A dedicated clearance system allows emergency vehicles to request corridor access during active convoy movements:

- Ambulances and fire trucks submit requests through the notification system
- The AI evaluates the request against convoy position and timing
- Duty officers can **approve or defer** with one click
- Approved requests create temporary corridor windows without compromising security

<br/>

### 🔐 VVIP Security Classification

Four security tiers with distinct operational requirements:

| Class | Description | Road Requirement | Closure Type |
|---|---|---|---|
| **Z+** | Highest security (Head of State) | 6+ lane roads | Full road closure |
| **Z** | High security (Cabinet level) | 4+ lane roads | Partial closure |
| **Y** | Medium security (State officials) | 2+ lane roads | Speed restriction + signal priority |
| **X** | Standard escort | No restriction | Signal priority only |

<br/>

### ⚡ Edge Deployment

The entire platform runs on a **single laptop** — no cloud servers, no data center, no internet connection required:

- **NVIDIA RTX 4070** (8 GB) powers the AI inference
- **Intel i9-13900HX** (24 threads) handles all computation
- **32 GB DDR5** stores the road network, traffic history, and active state
- Deployed anywhere a laptop can go — motorcade command vehicles, mobile control rooms, field ops centers

This isn't a demo that needs a server farm. It's a **field-deployable system** that works at the edge.

<br/>

---

<br/>
<img width="736" height="1795" alt="Screenshot 2026-03-19 233122" src="https://github.com/user-attachments/assets/5b701079-1464-49f8-bebd-41e1e3e8b5e1" />

## 🔧 System Architecture

The platform is composed of **five specialized microservices**, each handling a distinct operational domain:

```
                          ┌─────────────────────┐
                          │   Command Dashboard  │   React + SvelteKit
                          │   (Presentation)     │   Live maps, controls, AI chat
                          └──────────┬──────────┘
                                     │
                          ┌──────────▼──────────┐
                          │    Convoy Brain      │   Python · LangGraph · Ollama
                          │  (AI Orchestration)  │   4 agents, 3 workflows, MCP tools
                          └──┬──────┬───────┬───┘
                             │      │       │
                    ┌────────▼┐  ┌──▼──┐  ┌─▼───────────┐
                    │  Qwen   │  │NATS │  │Traffic Oracle│   Python · ONNX · OR-Tools
                    │ 3.5 9B  │  │     │  │  (ML Engine) │   Neural forecasting + routing
                    │  (GPU)  │  │     │  └──────┬───────┘
                    └─────────┘  └──┬──┘         │
                                    │      ┌─────▼───────┐
                          ┌─────────▼───┐  │  PostGIS    │   Spatial database
                          │   Signal    │  │  + Valkey   │   pgRouting + pgvector
                          │  Ingress    │  │  + DuckDB   │   Real-time cache
                          │   (Rust)    │  └─────────────┘
                          └──────┬──────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              Gov Traffic    Fleet GPS   Crowdsource
                Feeds       Telemetry     Reports
```

| Service | Language | What It Does |
|---|---|---|
| **Signal Ingress** | Rust | Ingests live traffic from 4 data sources at sub-millisecond latency |
| **Traffic Oracle** | Python | Runs ML predictions, route optimization, anomaly detection |
| **Convoy Brain** | Python | AI agent orchestration — the "thinking" layer |
| **Corridor Store** | PostgreSQL | Spatial database with road network, traffic history, graph routing |
| **Command Dashboard** | TypeScript | Visual command interface for human operators |

<br/>

---

<br/>

## 📁 Project Structure

```
VVIP/
│
├── 📄 README.md                          ← You are here
├── 📄 TECHNICAL_DOCS.md                  ← Detailed technical documentation
├── 📄 VVIP.md                            ← Original project proposal
├── 📄 LICENSE                            ← MIT License
│
├── 🎨 frontend/                          ← React 19 Command Dashboard
│   ├── src/
│   │   ├── components/                   ← 10 UI components
│   │   │   ├── VVIPDashboard.jsx         ← Root dashboard — toolbar, panels, layout
│   │   │   ├── ConvoyMap.jsx             ← Interactive map with route overlays
│   │   │   ├── LeftPanel.jsx             ← Convoy planning & VVIP profile
│   │   │   ├── RightPanel.jsx            ← Operations monitor (5 tabs)
│   │   │   ├── AIReasoningPanel.jsx       ← Chain-of-thought AI display
│   │   │   ├── NotificationBell.jsx      ← Emergency clearance system
│   │   │   ├── SearchBar.jsx             ← Global convoy/location search
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
│   │       ├── orchestrator.py           ← ReAct reasoning loop
│   │       ├── ollama_bridge.py          ← Qwen 3.5 LLM interface
│   │       ├── agents/                   ← 4 specialized AI agents
│   │       ├── workflows/                ← 3 LangGraph state machines
│   │       ├── mcp/                      ← 11 MCP tools + 4 resources
│   │       └── memory/                   ← Convoy state persistence
│   │
│   ├── traffic-oracle/                   ← ML Prediction & Optimization
│   │   └── src/traffic_oracle/
│   │       ├── predict/                  ← DSTGAT forecaster + ETA model
│   │       ├── nn/                       ← Neural network architecture
│   │       ├── optimize/                 ← Route optimizer + diversion planner
│   │       ├── anomaly/                  ← Real-time anomaly detection
│   │       ├── ingest/                   ← NATS → database pipeline
│   │       ├── synthetic/                ← Gap-filling data generation
│   │       └── runtime/                  ← GPU memory management
│   │
│   ├── signal-ingress/                   ← Rust Real-Time Data Ingestion
│   │   └── src/
│   │       ├── feeds/                    ← 5 traffic data source adapters
│   │       ├── pipeline/                 ← Bounded channel batching
│   │       ├── anomaly/                  ← Tier-1 inline detection (~500ns)
│   │       ├── normalize/                ← Schema alignment
│   │       └── publish/                  ← Arrow IPC → NATS streaming
│   │
│   ├── corridor-store/                   ← Spatial Database
│   │   ├── migrations/                   ← PostGIS + pgRouting + pgvector
│   │   ├── seed/                         ← Sample corridor data
│   │   └── importer/                     ← OpenStreetMap → PostGIS loader
│   │
│   └── command-deck/                     ← SvelteKit Advanced Dashboard
│       └── src/
│           ├── lib/components/           ← 14 Svelte 5 components
│           ├── lib/stores/               ← 6 reactive state stores
│           └── lib/simulation/           ← WebGPU traffic simulation
│
├── 🏗️ infra/                             ← Infrastructure & Deployment
│   ├── compose.yml                       ← Docker Compose full stack
│   ├── docker/                           ← 6 Dockerfiles
│   ├── scripts/                          ← Bootstrap, deploy, OS tuning
│   └── observability/                    ← Prometheus + Grafana (18 alert rules)
│
├── 🤖 models/                            ← ML Model Definitions
│   ├── Modelfile.qwen_vvip              ← Ollama model configuration
│   ├── configs/                          ← Hyperparameters (DSTGAT, anomaly)
│   ├── finetune/                         ← QLoRA fine-tuning pipeline
│   └── onnx/                             ← Compiled ONNX model exports
│
├── 📡 shared/                            ← Cross-Service Definitions
│   ├── proto/                            ← Protocol Buffer schemas
│   └── schemas/arrow/                    ← Arrow IPC data formats
│
└── 🧪 tests/                            ← Testing Framework
    └── adversarial/                      ← 16 automated routing scenarios
```

<br/>

---

<br/>

<img width="3199" height="1785" alt="Screenshot 2026-03-19 232907" src="https://github.com/user-attachments/assets/b26f7c21-45cf-46b4-a886-8d71dccc3007" />

## 🚀 Getting Started

### Prerequisites

| Requirement | Minimum |
|---|---|
| **GPU** | NVIDIA GPU with 8+ GB VRAM (RTX 4070 recommended) |
| **CPU** | 8+ cores (i9-14900HX recommended) |
| **RAM** | 32 GB DDR5 |
| **OS** | Windows 11 / Ubuntu 22.04+ |
| **Docker** | Docker Desktop with Compose v2 |
| **Ollama** | v0.5+ installed on host |

### Quick Start

**1. Clone the repository**
```bash
git clone https://github.com/shubro18202758/VVIP.git
cd VVIP
```

**2. Set up the AI model**
```bash
ollama pull qwen3.5:9b-q4_K_M
```

**3. Launch the platform**
```bash
cd infra
docker compose up -d
```

**4. Start the frontend**
```bash
cd frontend
npm install
npm run dev
```

**5. Open the dashboard**

Navigate to `http://localhost:5173` — the VVIP Convoy Command dashboard is ready.

<br/>

---

<br/>

## 📚 Technical Documentation

For detailed technical documentation including API contracts, database schemas, ML model architectures, service configurations, and code-level documentation, see:

**➡️ [TECHNICAL_DOCS.md](TECHNICAL_DOCS.md)**

<br/>

---

<br/>

## 🏛️ Context & Relevance

This platform is designed in the context of India's expanding road infrastructure under:

- **Bharatmala Pariyojana** — National highway development program
- **PM Gati Shakti** — Integrated infrastructure planning
- **Ministry of Road Transport & Highways (MoRTH)** Intelligent Transport Systems initiatives
- **Smart Cities Mission** — Urban mobility modernization

The system can be pilot-deployed on selected city corridors (e.g., Ahmedabad–Gandhinagar) and expanded nationally based on results.

<br/>

## 🎯 Expected Outcomes

| Metric | Target |
|---|---|
| 🕐 Traffic disruption duration | **Reduced by up to 6×** |
| 🚗 Commuter queue length | **Minimized through predictive diversions** |
| 🚑 Emergency vehicle access | **Dynamic corridor windows during convoy ops** |
| 👮 Agency coordination time | **Near-instant through unified dashboard** |
| 🔒 Security compliance | **100% automated validation** |
| 🧠 Decision transparency | **Full chain-of-thought AI reasoning** |

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
