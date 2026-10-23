"""MCP Server — exposes traffic-oracle capabilities as standard MCP primitives.

Implements a lightweight Model Context Protocol server with:
- Resources: read-only data access (live traffic, anomalies, corridor summaries)
- Tools: executable functions (route optimizer, flow forecaster, ETA predictor,
  diversion planner, scenario evaluator, spatial queries), the READ-ONLY
  city tools over the fused picture (contract C-C: entities, dossiers, the
  city feed, signals, units, dispatches, the interception plan, the
  situation log, e-challan recommendations) and the CANVAS verbs
- Prompts: reusable system prompt templates for VVIP security protocols

Transport: STDIO (for same-host Docker deployment), or JSON-RPC 2.0 over
HTTP (POST /mcp on the brain, contract C-D2).

Read-only rail (R2): no tool approves, decides, dispatches, earmarks,
withdraws or transitions anything — every human decision stays a human
action on the ops wall. The executor's POSTs go to a fixed allow-list of
compute endpoints (predict / optimize / evaluate / traffic-live /
simulate); every city tool is a GET. Ids interpolated into a path are
validated against the oracle's own grammar first, and a 404 answers
``{ok: false, not_found | not_deployed}`` instead of failing the turn.

Canvas verbs (canvas_fly_to, canvas_track_entity, canvas_highlight,
canvas_annotate, canvas_set_sensor, canvas_toggle_layer) never call HTTP:
they validate their arguments and return a DIRECTIVE — a proposal the
orchestrator streams to the deck, where a human applies or dismisses it.
The AI never moves the operator's camera on its own.

Error typing (consumed by the orchestrator's retry policy): every dispatch
failure surfaces as :class:`ToolExecutionError`. Deterministic failures —
missing/malformed arguments (KeyError/TypeError/ValueError) and HTTP 4xx from
the oracle — carry ``retryable=False`` so the caller never repeats them;
transport errors, timeouts, 429 and 5xx stay retryable.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tool / Resource / Prompt schema definitions
# ---------------------------------------------------------------------------

#: Kinds GET /api/v1/city/entities serves (traffic-oracle city/common.py).
_CITY_ENTITY_KINDS_DEF = (
    "event", "vehicle", "sighting", "camera", "junction", "unit", "convoy",
    "dispatch", "recommendation", "anomaly",
)
#: FeedItem.kind values (contract C-C).
_CITY_FEED_KINDS_DEF = ("event", "enforcement", "anpr", "dispatch", "signals", "ama", "brain")
#: Canvas layer ids (deck gods-eye/layers/index.ts, plus WP-17's 3D tiles).
_CANVAS_LAYERS_DEF = (
    "events", "flow", "traces", "enforcement", "signals", "units", "dispatch",
    "convoy", "cameras", "anomalies", "photoreal-3d",
)

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "predict_traffic_flow",
        "description": (
            "Predict traffic speed and congestion for road segments at future time "
            "horizons (T+5, T+10, T+15, T+30 minutes). Uses a DSTGAT spatio-temporal "
            "graph attention network via ONNX inference."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "segment_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Road segment IDs to forecast.",
                },
                "horizons_min": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Prediction horizons in minutes. Default: [5, 10, 15, 30].",
                },
            },
            "required": ["segment_ids"],
        },
    },
    {
        "name": "find_convoy_routes",
        "description": (
            "Find optimal corridor routes/spines between origin and destination "
            "segments for any corridor event (protected movement, procession, "
            "race, ceremonial, egress surge, evacuation, work zone). "
            "Alias: find_corridor_routes. "
            "Uses multi-objective optimization (OR-Tools CP-SAT MIP solver) balancing "
            "transit time, public disruption, security score, and route complexity."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "origin_segment": {
                    "type": "integer",
                    "description": "Origin road segment ID.",
                },
                "destination_segment": {
                    "type": "integer",
                    "description": "Destination road segment ID.",
                },
                "max_candidates": {
                    "type": "integer",
                    "description": "Maximum number of route candidates to return. Default: 5.",
                },
                "avoid_segments": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Segment IDs to exclude from routing.",
                },
            },
            "required": ["origin_segment", "destination_segment"],
        },
    },
    {
        "name": "plan_diversions",
        "description": (
            "Generate per-segment traffic diversion plans for a corridor-event "
            "route/spine. Alias: plan_event_diversions. "
            "Computes activation/deactivation timing, alternative routes, "
            "queue length estimates, and closure types (full/partial/speed restriction)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "route_segment_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Ordered list of segment IDs along the event route.",
                },
                "convoy_speed_kmh": {
                    "type": "number",
                    "description": "Expected managed-movement speed in km/h. Default: 60.",
                },
                "advance_closure_sec": {
                    "type": "integer",
                    "description": (
                        "Seconds before event arrival to activate closure. Default: 120; "
                        "protocol floors apply per protection class (Z+ 180, Z 120, Y 60, X/none 0)."
                    ),
                },
                "departure_time": {
                    "type": "string",
                    "description": "ISO 8601 departure time. Default: now.",
                },
            },
            "required": ["route_segment_ids"],
        },
    },
    {
        "name": "evaluate_scenarios",
        "description": (
            "Compare multiple convoy movement scenarios using Monte Carlo simulation. "
            "Evaluates total public disruption (vehicle-hours), queue lengths, "
            "closure durations, and complaint risk for each scenario."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scenarios": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "scenario_id": {"type": "string"},
                            "route_segment_ids": {
                                "type": "array",
                                "items": {"type": "integer"},
                            },
                            "departure_time": {"type": "string"},
                            "convoy_speed_kmh": {"type": "number"},
                            "route_name": {"type": "string"},
                        },
                        "required": ["scenario_id", "route_segment_ids"],
                    },
                    "description": "List of scenarios to evaluate.",
                },
            },
            "required": ["scenarios"],
        },
    },
    {
        "name": "predict_eta",
        "description": (
            "Predict estimated travel time for a convoy route using gradient-boosted "
            "regression. Returns ETA in seconds."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "route_length_m": {"type": "number", "description": "Total route length in meters."},
                "num_segments": {"type": "integer", "description": "Number of road segments on route."},
                "avg_predicted_speed": {"type": "number", "description": "Average predicted speed (km/h)."},
                "avg_predicted_congestion": {"type": "number", "description": "Average predicted congestion index (0-1)."},
                "hour": {"type": "integer", "description": "Departure hour (0-23)."},
                "day_of_week": {
                    "type": "integer",
                    "description": "Day of week (0=Monday). Sent to the oracle as dow.",
                },
                "num_signals": {
                    "type": "integer", "description": "Traffic signals on route. Default: 0.",
                },
                "road_class_score": {
                    "type": "number",
                    "description": (
                        "Weighted road class score (10-100). Sent to the oracle as "
                        "weighted_road_class_score; default 50."
                    ),
                },
            },
            "required": [
                "route_length_m", "num_segments", "avg_predicted_speed",
                "avg_predicted_congestion", "hour", "day_of_week",
            ],
        },
    },
    {
        "name": "query_shortest_path",
        "description": (
            "Find shortest path between two road segments using pgRouting Dijkstra. "
            "Returns ordered list of nodes with cumulative travel cost in seconds."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_segment": {"type": "integer", "description": "Source segment ID."},
                "target_segment": {"type": "integer", "description": "Target segment ID."},
            },
            "required": ["source_segment", "target_segment"],
        },
    },
    {
        "name": "query_k_shortest_paths",
        "description": (
            "Find K alternative shortest paths using pgRouting KSP algorithm. "
            "Returns multiple route alternatives ranked by travel cost."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_segment": {"type": "integer", "description": "Source segment ID."},
                "target_segment": {"type": "integer", "description": "Target segment ID."},
                "k": {"type": "integer", "description": "Number of alternatives. Default: 5."},
            },
            "required": ["source_segment", "target_segment"],
        },
    },
    {
        "name": "query_segments_in_bbox",
        "description": (
            "Find all road segments within a geographic bounding box. "
            "Returns segment IDs, names, road classes, and geometries."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_lon": {"type": "number"},
                "min_lat": {"type": "number"},
                "max_lon": {"type": "number"},
                "max_lat": {"type": "number"},
            },
            "required": ["min_lon", "min_lat", "max_lon", "max_lat"],
        },
    },
    {
        "name": "query_segment_details",
        "description": (
            "Get detailed attributes for a specific road segment including "
            "road name, class, lanes, speed limit, oneway status, and geometry."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "segment_id": {"type": "integer", "description": "Road segment ID."},
            },
            "required": ["segment_id"],
        },
    },
    {
        "name": "get_live_traffic",
        "description": (
            "Fetch real-time traffic conditions for specified road segments from "
            "Valkey cache. Returns speed, congestion index, and last update time."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "segment_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Segment IDs to query.",
                },
            },
            "required": ["segment_ids"],
        },
    },
    {
        "name": "get_historical_pattern",
        "description": (
            "Historical baseline for one road segment at the same IST weekday "
            "and hour, from the oracle's REAL hourly aggregates (the last 7 "
            "days). Average speed, p50 speed and p95 congestion per hour. A "
            "synthetic rollup of the simulated feed, when the oracle serves "
            "one, is returned separately and labelled — it is never a "
            "calibration input and never presented as real history."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "segment_id": {"type": "integer", "description": "Road segment ID."},
                "day_of_week": {
                    "type": "integer", "description": "IST weekday, 0=Monday, 6=Sunday.",
                },
                "hour": {"type": "integer", "description": "IST hour of day (0-23)."},
            },
            "required": ["segment_id", "day_of_week", "hour"],
        },
    },
    {
        "name": "simulate_twin",
        "description": (
            "Run the deterministic digital-twin simulation for a convoy scenario: "
            "n_replications seeded Monte-Carlo runs over the corridor twin, "
            "reporting total public disruption (vehicle-hours), max queue length, "
            "closure durations, convoy transit time, recovery and ambulance delay. "
            "The `calibrated` flag in the result tells whether the twin has been "
            "calibrated against observed traffic via /simulate/replay."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "route_segment_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Ordered segment IDs of the route to simulate.",
                },
                "departure_time": {
                    "type": "string",
                    "description": "ISO 8601 departure time. Default: now.",
                },
                "closure_policy": {
                    "type": "object",
                    "properties": {
                        "advance_closure_sec": {
                            "type": "integer",
                            "description": "Closure activation lead time in seconds.",
                        },
                        "closure_type": {
                            "type": "string",
                            "description": "full | partial | speed_restriction.",
                        },
                    },
                },
                "horizon_min": {
                    "type": "integer",
                    "description": "Simulation horizon in minutes. Max 60.",
                },
                "n_replications": {
                    "type": "integer",
                    "description": "Number of seeded replications. Max 20.",
                },
                "seed": {"type": "integer", "description": "RNG seed for determinism."},
            },
            "required": ["route_segment_ids"],
        },
    },
    {
        "name": "get_active_corridor_events",
        "description": (
            "List the corridor events registered in the unified city picture "
            "(status planning / approved / active): protected movements, "
            "processions, gatherings, pursuits and other operations, with "
            "kind, title, protection class, status, linked movement id, the "
            "labelled route spines (segment lists) and closure phases. "
            "Read-only; served by traffic-oracle GET /api/v1/events/active."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": (
                        "Optional: only this corridor event (its uuid). An event "
                        "no longer active is looked up in the event history."
                    ),
                },
                "movement_id": {
                    "type": "string",
                    "description": "Optional: only the event linked to this movement.",
                },
                "event_kind": {
                    "type": "string",
                    "description": (
                        "Optional filter: protected_movement, procession, "
                        "mass_gathering, race, ceremonial, egress_surge, "
                        "emergency_evacuation, work_zone, crime_incident or "
                        "vehicle_pursuit. Omit it for every active event."
                    ),
                },
            },
            "required": [],
        },
    },
    # -- read-only city tools (contract C-C; R2: nothing here writes) --------
    {
        "name": "city_query_entities",
        "description": (
            "Query the fused city picture (GET /api/v1/city/entities): corridor "
            "events, earmarked vehicles (plates masked), ANPR sightings and "
            "cameras, junction signals, emulated agency units, convoys, "
            "dispatches, e-challan recommendations and anomalies — each with "
            "its URN, label, state, source (live / EMULATED) and position. "
            "Use it to resolve what the operator means ('the stolen vehicle', "
            "'units near Relief Road') before talking about or showing it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kinds": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "string", "enum": list(_CITY_ENTITY_KINDS_DEF)},
                    "description": "Entity kinds to return. Omit for every kind.",
                },
                "bbox": {
                    "type": "array",
                    "minItems": 4,
                    "maxItems": 4,
                    "items": {"type": "number"},
                    "description": (
                        "Optional [min_lon, min_lat, max_lon, max_lat] inside Ahmedabad."
                    ),
                },
                "match": {
                    "type": "string",
                    "maxLength": 64,
                    "description": (
                        "Optional text an entity's label, plate, road or id must "
                        "contain (case-insensitive) — e.g. a plate or a road name."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Rows returned (1-50, default 25).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "city_get_dossier",
        "description": (
            "One entity's dossier by URN (GET /api/v1/city/entity/{kind}/{id}): "
            "the entity, its 1-hop related entities (vehicle → sightings, "
            "cameras, recommendations, pursuit; event → spines, dispatches) and "
            "its audit trail with the human or machine actor of every row."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "urn": {
                    "type": "string",
                    "description": "Canonical '<kind>:<id>', e.g. 'vehicle:<uuid>', 'junction:3'.",
                },
            },
            "required": ["urn"],
        },
    },
    {
        "name": "city_feed",
        "description": (
            "The city narrative, newest first (GET /api/v1/city/feed): event "
            "transitions, e-challan decisions, ANPR hits, dispatches, signal "
            "plans, situation-band changes and brain checkpoints, each with "
            "its actor and source."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string", "description": "ISO-8601 instant: only items after it.",
                },
                "kinds": {
                    "type": "array",
                    "maxItems": 7,
                    "items": {"type": "string", "enum": list(_CITY_FEED_KINDS_DEF)},
                    "description": "Feed kinds to return. Omit for all.",
                },
                "limit": {"type": "integer", "description": "Items (1-50, default 20)."},
            },
            "required": [],
        },
    },
    {
        "name": "get_signal_state",
        "description": (
            "City signal network now (GET /api/v1/signals/state): every "
            "programmed junction's cycle, active phase and green share, plus "
            "the city band. Program-clock phases, not controller telemetry."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_units",
        "description": (
            "Agency units on the ground (GET /api/v1/units): Jan Rakshak, "
            "police, traffic, ambulance and fire units with status and "
            "position. Every unit is EMULATED (dispatch projections and "
            "patrol frames) and labelled so."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agency": {
                    "type": "string",
                    "description": "Optional: jan_rakshak, police, traffic, ambulance or fire.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_dispatches",
        "description": (
            "Dispatch records (GET /api/v1/dispatches): which agency unit was "
            "requested for which event, its status, who requested it and the "
            "named officer who decided it. Read-only — dispatching stays an "
            "officer action on the ops wall."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Optional: one event's dispatches."},
                "status": {"type": "string", "description": "Optional status filter."},
                "limit": {"type": "integer", "description": "Rows (1-50, default 20)."},
            },
            "required": [],
        },
    },
    {
        "name": "get_interception_plan",
        "description": (
            "A vehicle pursuit's interception plan (GET "
            "/api/v1/events/{id}/interception-plan): the latest lawful ANPR "
            "sighting and its age, the direction of travel and the ranked "
            "choke junctions. Advisory for the officers who run the pursuit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The vehicle_pursuit event's uuid."},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "get_situation_log",
        "description": (
            "Situation-band audit rows, newest first (GET "
            "/api/v1/adaptive/situation_log): the band, its code, severity and "
            "whether it transitioned, for one event or the city."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string", "description": "Optional: one event (or movement) id.",
                },
                "limit": {"type": "integer", "description": "Rows (1-50, default 12)."},
            },
            "required": [],
        },
    },
    {
        "name": "list_enforcement_recommendations",
        "description": (
            "E-challan RECOMMENDATIONS by status (GET "
            "/api/v1/enforcement/recommendations): pending ones await a named "
            "officer; approved / rejected ones carry the deciding officer. "
            "Machine-proposed only — no penalty is ever automated."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "approved", "rejected"],
                    "description": "Default pending.",
                },
                "limit": {"type": "integer", "description": "Rows (1-50, default 20)."},
            },
            "required": [],
        },
    },
    # -- canvas verbs: directives a human applies (never HTTP, never auto) ----
    {
        "name": "canvas_fly_to",
        "description": (
            "PROPOSE flying the operator's God's-eye camera to an entity (urn) "
            "or a point (lon, lat). Returns a directive the operator applies "
            "or dismisses; nothing moves on its own."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "urn": {"type": "string", "description": "Entity URN '<kind>:<id>'."},
                "lon": {"type": "number", "description": "Longitude (with lat, instead of urn)."},
                "lat": {"type": "number", "description": "Latitude (with lon, instead of urn)."},
                "range_m": {"type": "number", "description": "Camera range in metres (100-60000)."},
            },
            "required": [],
        },
    },
    {
        "name": "canvas_track_entity",
        "description": (
            "PROPOSE selecting an entity on the God's-eye canvas and keeping "
            "the camera on it (vehicle, unit, convoy, event …). A directive "
            "the operator applies or dismisses."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"urn": {"type": "string", "description": "Entity URN '<kind>:<id>'."}},
            "required": ["urn"],
        },
    },
    {
        "name": "canvas_highlight",
        "description": "PROPOSE highlighting 1-20 entities on the God's-eye canvas.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "urns": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {"type": "string"},
                    "description": "Entity URNs.",
                },
            },
            "required": ["urns"],
        },
    },
    {
        "name": "canvas_annotate",
        "description": (
            "PROPOSE a short-lived note pinned to an entity on the canvas "
            "(<= 140 characters, severity info / warn / critical)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "urn": {"type": "string", "description": "Entity URN '<kind>:<id>'."},
                "text": {"type": "string", "maxLength": 140, "description": "The note."},
                "severity": {"type": "string", "enum": ["info", "warn", "critical"]},
            },
            "required": ["urn", "text"],
        },
    },
    {
        "name": "canvas_set_sensor",
        "description": (
            "PROPOSE a canvas sensor mode: raw (COMMAND colours), flir-whot, "
            "flir-bhot or flir-iron."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["raw", "flir-whot", "flir-bhot", "flir-iron"]},
            },
            "required": ["mode"],
        },
    },
    {
        "name": "canvas_toggle_layer",
        "description": (
            "PROPOSE switching a canvas layer on or off: events, flow, traces, "
            "enforcement, signals, units, dispatch, convoy, cameras, anomalies "
            "or photoreal-3d."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer_id": {"type": "string", "enum": list(_CANVAS_LAYERS_DEF)},
                "on": {"type": "boolean"},
            },
            "required": ["layer_id", "on"],
        },
    },
]

RESOURCE_DEFINITIONS: list[dict[str, Any]] = [
    {
        "uri": "traffic://anomalies/recent",
        "name": "Recent Traffic Anomalies",
        "description": (
            "List of traffic anomalies detected in the last hour across the corridor. "
            "Includes anomaly type, severity, segment ID, and timestamp."
        ),
        "mimeType": "application/json",
    },
    {
        "uri": "traffic://corridor/summary",
        "name": "Corridor Traffic Summary",
        "description": (
            "Aggregated traffic conditions across the monitored corridor. "
            "Includes average speed, congestion distribution, and active incident count."
        ),
        "mimeType": "application/json",
    },
    {
        "uri": "traffic://segments/{segment_id}/history",
        "name": "Segment Traffic History",
        "description": (
            "Historical traffic data for a specific road segment. "
            "Returns observations from the last 24 hours."
        ),
        "mimeType": "application/json",
    },
    {
        "uri": "convoy://movements/active",
        "name": "Active Convoy Movements",
        "description": (
            "List of currently active convoy movements with their status, "
            "position, selected route, and active diversions."
        ),
        "mimeType": "application/json",
    },
]

PROMPT_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "vvip_security_protocol",
        "description": (
            "Inject VVIP security classification rules and route compliance "
            "requirements into the agent's context."
        ),
        "arguments": [
            {
                "name": "vvip_class",
                "description": "Security classification: Z+, Z, Y, or X.",
                "required": True,
            },
        ],
    },
    {
        "name": "route_analysis_brief",
        "description": (
            "Structured prompt for analyzing route candidates. Injects corridor "
            "context and scoring criteria for multi-objective route evaluation."
        ),
        "arguments": [
            {
                "name": "origin_name",
                "description": "Human-readable origin location name.",
                "required": True,
            },
            {
                "name": "destination_name",
                "description": "Human-readable destination location name.",
                "required": True,
            },
            {
                "name": "vvip_class",
                "description": "Security classification for compliance filtering.",
                "required": True,
            },
        ],
    },
    {
        "name": "diversion_coordination_brief",
        "description": (
            "Structured prompt for coordinating traffic diversions across "
            "agencies. Injects timing rules and public disruption constraints."
        ),
        "arguments": [
            {
                "name": "movement_id",
                "description": "Active convoy movement identifier.",
                "required": True,
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# MCP Server implementation
# ---------------------------------------------------------------------------

class MCPServer:
    """Lightweight MCP Server exposing traffic-oracle as standard MCP primitives.

    This server implements the Model Context Protocol 2025-03-26 specification
    with support for:
    - tools/list, tools/call
    - resources/list, resources/read
    - prompts/list, prompts/get
    - initialize, ping

    Transport: STDIO (newline-delimited JSON-RPC 2.0).
    """

    PROTOCOL_VERSION = "2025-03-26"
    SERVER_NAME = "vvip-convoy-mcp"
    SERVER_VERSION = "0.2.0"

    def __init__(self, tool_executor: ToolExecutor) -> None:
        self._executor = tool_executor
        self._initialized = False
        self._handlers: dict[str, Any] = {
            "initialize": self._handle_initialize,
            "initialized": self._handle_initialized,
            "ping": self._handle_ping,
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
            "resources/list": self._handle_resources_list,
            "resources/read": self._handle_resources_read,
            "prompts/list": self._handle_prompts_list,
            "prompts/get": self._handle_prompts_get,
        }
        logger.info("mcp_server.init")

    async def handle_message(self, raw: str) -> str | None:
        """Process a single JSON-RPC 2.0 message and return a response (or None for notifications)."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return _jsonrpc_error(None, -32700, "Parse error")

        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        handler = self._handlers.get(method)
        if handler is None:
            if msg_id is not None:
                return _jsonrpc_error(msg_id, -32601, f"Method not found: {method}")
            return None  # unknown notification — ignore

        try:
            result = await handler(params)
        except ToolExecutionError as exc:
            logger.warning("mcp_server.tool_error", method=method, error=str(exc))
            return _jsonrpc_error(msg_id, -32000, str(exc))
        except Exception as exc:
            logger.exception("mcp_server.internal_error", method=method)
            return _jsonrpc_error(msg_id, -32603, f"Internal error: {type(exc).__name__}")

        # Notifications (no id) don't get a response
        if msg_id is None:
            return None

        return json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result})

    # -- Protocol lifecycle ------------------------------------------------

    async def _handle_initialize(self, params: dict) -> dict:
        self._initialized = True
        logger.info(
            "mcp_server.initialize",
            client_name=params.get("clientInfo", {}).get("name"),
            protocol_version=params.get("protocolVersion"),
        )
        return {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {
                "name": self.SERVER_NAME,
                "version": self.SERVER_VERSION,
            },
        }

    async def _handle_initialized(self, _params: dict) -> dict:
        return {}

    async def _handle_ping(self, _params: dict) -> dict:
        return {}

    # -- Tools -------------------------------------------------------------

    async def _handle_tools_list(self, _params: dict) -> dict:
        return {"tools": TOOL_DEFINITIONS}

    async def _handle_tools_call(self, params: dict) -> dict:
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        logger.info("mcp_server.tools_call", tool=name, args_keys=list(arguments.keys()))

        result = await self._executor.execute(name, arguments)
        return {
            "content": [
                {"type": "text", "text": json.dumps(result, default=str)},
            ],
        }

    # -- Resources ---------------------------------------------------------

    async def _handle_resources_list(self, _params: dict) -> dict:
        return {"resources": RESOURCE_DEFINITIONS}

    async def _handle_resources_read(self, params: dict) -> dict:
        uri = params.get("uri", "")
        logger.info("mcp_server.resources_read", uri=uri)
        content = await self._executor.read_resource(uri)
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(content, default=str),
                },
            ],
        }

    # -- Prompts -----------------------------------------------------------

    async def _handle_prompts_list(self, _params: dict) -> dict:
        return {"prompts": PROMPT_DEFINITIONS}

    async def _handle_prompts_get(self, params: dict) -> dict:
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        logger.info("mcp_server.prompts_get", prompt=name)
        messages = self._executor.get_prompt(name, arguments)
        return {"messages": messages}


# ---------------------------------------------------------------------------
# Tool executor — bridges MCP tool calls to traffic-oracle service
# ---------------------------------------------------------------------------

class ToolExecutionError(Exception):
    """Raised when a tool call fails in a recoverable way.

    ``retryable`` is False for deterministic failures (bad/missing arguments,
    HTTP 4xx other than 429) — repeating the identical call cannot succeed,
    so the orchestrator returns the error straight into the transcript.
    ``status_code`` carries the oracle's HTTP status when there was one.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


#: Per-tool schema properties — used to keep object-typed parameters intact
#: during argument flattening.
_TOOL_PROPERTY_MAP: dict[str, dict] = {
    t["name"]: t.get("inputSchema", {}).get("properties", {})
    for t in TOOL_DEFINITIONS
}

#: Event-agnostic alias names (contract C2): generalized names ship as
#: advertised aliases of the canonical tools — legacy names keep working,
#: new callers may use the corridor-event vocabulary.
#: ``get_active_corridor_events`` is NOT an alias: it was mis-mapped onto
#: get_live_traffic (a KeyError on every call) and is now a real tool backed
#: by GET /api/v1/events/active.
TOOL_ALIASES: dict[str, str] = {
    "find_corridor_routes": "find_convoy_routes",
    "plan_event_diversions": "plan_diversions",
    "evaluate_event_scenarios": "evaluate_scenarios",
}
_TOOL_ALIASES = TOOL_ALIASES  # name the routing pin test reads

#: Corridor-event kinds the oracle board serves (events/__init__.py EVENT_KINDS).
_EVENT_KINDS = frozenset({
    "protected_movement", "procession", "mass_gathering", "race", "ceremonial",
    "egress_surge", "emergency_evacuation", "work_zone", "crime_incident",
    "vehicle_pursuit",
})
#: Drafted filter values that mean "no filter".
_WILDCARDS = frozenset({"", "all", "any", "*", "none", "null"})

#: predict_eta keys the oracle's ETAPredictRequest requires (plus ``dow``,
#: which the tool advertises as ``day_of_week``).
_ETA_REQUIRED = (
    "route_length_m", "num_segments", "avg_predicted_speed",
    "avg_predicted_congestion", "hour",
)

#: R2 read-only rail: the ONLY oracle paths the executor POSTs to — compute
#: endpoints that read and calculate (forecast, optimise, evaluate, the live
#: cache read, the twin). Every city tool is a GET; no write surface (event
#: lifecycle, e-challan decisions, dispatch, earmarks) is reachable from a
#: model-chosen tool.
POST_ALLOWLIST = frozenset({
    "/api/v1/predict/flow",
    "/api/v1/predict/eta",
    "/api/v1/optimize/routes",
    "/api/v1/optimize/diversions",
    "/api/v1/evaluate/scenarios",
    "/api/v1/traffic/live",
    "/api/v1/simulate/twin",
})
#: No tool name may make the AI an actor (R2): pinned by the test suite.
FORBIDDEN_TOOL_VERBS = re.compile(
    r"approve|decide|earmark|dispatch_request|withdraw|transition", re.IGNORECASE
)

#: The canvas verbs — directive-returning tools that never call HTTP.
CANVAS_VERBS = (
    "canvas_fly_to",
    "canvas_track_entity",
    "canvas_highlight",
    "canvas_annotate",
    "canvas_set_sensor",
    "canvas_toggle_layer",
)
CANVAS_LAYERS = frozenset(_CANVAS_LAYERS_DEF)
SENSOR_MODES = ("raw", "flir-whot", "flir-bhot", "flir-iron")
ANNOTATION_SEVERITIES = ("info", "warn", "critical")
ANNOTATION_MAX_CHARS = 140
HIGHLIGHT_MAX = 20
#: Canvas coordinates must lie inside the city's road-network extent (contract
#: C-J's city bbox — the deck's NETWORK_BBOX, which its directive validator
#: applies too, so a proposal valid here is never dropped as 'bbox' there).
CANVAS_BBOX = (72.40, 22.90, 72.80, 23.20)
FLY_RANGE_M = (100.0, 60_000.0)

#: URN kinds and id grammar — a mirror of traffic-oracle city/common.py
#: parse_entity_id, so a malformed id never reaches a path.
URN_KINDS = _CITY_ENTITY_KINDS_DEF + ("segment",)
_UUID_KINDS = frozenset({"event", "vehicle", "sighting", "dispatch", "recommendation"})
_INT_KINDS = frozenset({"junction", "segment", "anomaly"})
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$")
_STATUS_RE = re.compile(r"^[a-z_]{1,32}$")
UNIT_AGENCIES = ("jan_rakshak", "police", "traffic", "ambulance", "fire")

#: Indian registration marks inside free text (mirror of the deck's
#: plates.ts / the oracle's masking): only the 4-digit number is masked.
_PLATE_IN_TEXT_RE = re.compile(r"\b([A-Z]{2}[ -]?\d{1,2}[ -]?(?:[A-Z]{1,3}[ -]?)?)(\d{4})\b")


def mask_plates(text: Any) -> str:
    """Mask every registration mark in ``text`` (GJ27EM9001 -> GJ27EM•••1)."""
    if not isinstance(text, str) or not text:
        return "" if text is None else str(text)
    return _PLATE_IN_TEXT_RE.sub(lambda m: f"{m.group(1)}•••{m.group(2)[-1]}", text)


def plate_label(text: Any) -> str | None:
    """'vehicle GJ27EM•••1' from the first registration mark in ``text``; None without one."""
    match = _PLATE_IN_TEXT_RE.search(text) if isinstance(text, str) else None
    return f"vehicle {match.group(1)}•••{match.group(2)[-1]}" if match else None


def parse_urn(urn: Any) -> tuple[str, str] | None:
    """``'<kind>:<id>'`` -> ``(kind, canonical id)``; None when malformed.

    Split at the FIRST colon (slug ids may contain colons); uuid kinds are
    lower-cased canonical, int kinds positive, slug kinds bounded.
    """
    if not isinstance(urn, str) or ":" not in urn or len(urn) > 160:
        return None
    kind, _, ident = urn.strip().partition(":")
    if kind not in URN_KINDS or not ident:
        return None
    if kind in _UUID_KINDS:
        try:
            return kind, str(uuid.UUID(ident))
        except ValueError:
            return None
    if kind in _INT_KINDS:
        if not ident.isdigit() or not 0 < int(ident) <= 2**62:
            return None
        return kind, str(int(ident))
    return (kind, ident) if _SLUG_RE.fullmatch(ident) else None


def canonical_urn(urn: Any) -> str | None:
    parsed = parse_urn(urn)
    return f"{parsed[0]}:{parsed[1]}" if parsed else None


def _bad_args(tool: str, why: str) -> ToolExecutionError:
    return ToolExecutionError(f"Tool '{tool}' got malformed arguments: {why}", retryable=False)


def _uuid_arg(tool: str, value: Any, name: str = "event_id") -> str:
    """A uuid argument headed for a path or query (non-retryable error otherwise)."""
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError):
        raise _bad_args(tool, f"{name} must be a uuid") from None


def _int_arg(value: Any, lo: int, hi: int, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _in_city(lon: float, lat: float) -> bool:
    min_lon, min_lat, max_lon, max_lat = CANVAS_BBOX
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


def _norm(text: Any) -> str:
    """Lower-case, spaces / dashes / dots removed (plates match however typed)."""
    return re.sub(r"[\s\-.]", "", str(text or "")).lower()


#: Entity props worth model context per kind (no raw plate, no raw title:
#: labels are already masked by the oracle; free text is masked here).
_ENTITY_PROPS = {
    "event": ("event_kind", "status", "protection_class", "crowd_class", "movement_id"),
    "vehicle": (
        "earmark_state", "lawful_basis", "earmark_reason", "last_camera_id", "vehicle_class",
    ),
    "sighting": ("camera_id", "speed_kmh", "heading", "matched_plate_hit", "read_source"),
    "camera": (
        "camera_kind", "camera_source", "junction_id", "segment_id", "reads_15m", "hits_15m",
    ),
    "junction": ("name", "junction_type", "signal_control", "cycle_sec", "green_ratio"),
    "unit": ("agency", "agency_label", "status", "eta_sec", "unit_source"),
    "convoy": ("movement_id", "speed_kmh", "progress", "segment_id", "position_source"),
    "dispatch": ("agency", "unit_id", "status", "requested_by", "decided_by", "event_title"),
    "recommendation": ("rule_id", "status", "decided_by", "camera_id"),
    "anomaly": ("anomaly_type", "severity", "road_name", "segment_id", "count_15m"),
    "segment": ("road_name", "road_class", "lanes", "speed_limit_kmh", "oneway", "length_m"),
}


def _seen_ist(stamp: Any) -> str | None:
    """'22:26 IST, 22 Oct' for an entity's own ISO-8601 time; None when absent.

    The entity's OWN observation time, so the tactical tier never reads the
    query's time as a sighting time (seen live: "last seen at
    2026-10-22T22:59:26Z" for a sighting six hours older).
    """
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    text = stamp.strip()
    try:
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(_IST).strftime("%H:%M IST, %d %b")


def compact_entity(entity: Any) -> dict | None:
    """One CityEntity as model context: URN, masked label, state, source,
    position, its own time ('seen_ist') and the kind's useful props (never a
    raw plate)."""
    if not isinstance(entity, dict) or not isinstance(entity.get("urn"), str):
        return None
    kind = str(entity.get("kind") or entity["urn"].split(":", 1)[0])
    props = entity.get("props") if isinstance(entity.get("props"), dict) else {}
    row: dict[str, Any] = {
        "urn": entity["urn"],
        "kind": kind,
        "label": mask_plates(entity.get("label") or entity["urn"]),
        "state": entity.get("state"),
        "source": entity.get("source"),
    }
    lon, lat = _finite(entity.get("lon")), _finite(entity.get("lat"))
    if lon is not None and lat is not None:
        row["lon"], row["lat"] = round(lon, 6), round(lat, 6)
    if entity.get("band"):
        row["band"] = entity["band"]
    seen = _seen_ist(entity.get("t"))
    if seen:
        row["seen_ist"] = seen
    if _finite(entity.get("age_sec")) is not None:
        row["age_sec"] = round(float(entity["age_sec"]))
    kept = {}
    for key in _ENTITY_PROPS.get(kind, ()):
        value = props.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        kept[key] = mask_plates(value) if isinstance(value, str) else value
    if kind == "event":
        spines = props.get("route_spines") if isinstance(props.get("route_spines"), list) else []
        kept["spines"] = [
            {"label": sp.get("label"), "segment_ids": list(sp.get("segment_ids") or [])[:24]}
            for sp in spines[:4] if isinstance(sp, dict)
        ]
        meta = props.get("metadata") if isinstance(props.get("metadata"), dict) else {}
        vehicle = meta.get("vehicle_id")
        if isinstance(vehicle, str) and parse_urn(f"vehicle:{vehicle}"):
            kept["vehicle_urn"] = f"vehicle:{vehicle.lower()}"
        if meta.get("fir"):
            kept["fir"] = mask_plates(meta["fir"])
    if kept:
        row["props"] = kept
    return row


def _haystack(entity: dict) -> str:
    props = entity.get("props") if isinstance(entity.get("props"), dict) else {}
    parts = [entity.get("urn"), entity.get("label"), entity.get("id")]
    for key in ("plate", "plate_masked", "title", "road_name", "name", "camera_id", "unit_id",
                "agency_label"):
        parts.append(props.get(key))
    return _norm(" ".join(str(p) for p in parts if p))


def _not_found(resp: httpx.Response, subject: str | None) -> dict:
    """A 404 as a tool answer: an unknown subject, or a route not deployed."""
    try:
        body = resp.json()
    except ValueError:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    if detail in (None, "Not Found"):
        try:
            path = resp.request.url.path
        except RuntimeError:  # a response built without its request
            path = None
        return {
            "ok": False,
            "not_deployed": True,
            "detail": "the oracle does not serve this route (404) — not deployed",
            "path": path,
        }
    return {"ok": False, "not_found": True, "subject": subject, "detail": str(detail)[:200]}


def _as_tool_error(tool_name: str, exc: Exception) -> ToolExecutionError:
    """Classify a raw dispatch failure into a typed :class:`ToolExecutionError`."""
    if isinstance(exc, ToolExecutionError):
        return exc
    if isinstance(exc, KeyError):
        return ToolExecutionError(
            f"Tool '{tool_name}' missing required argument {exc}",
            retryable=False,
        )
    if isinstance(exc, (TypeError, ValueError)):
        return ToolExecutionError(
            f"Tool '{tool_name}' got malformed arguments: {exc}",
            retryable=False,
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        detail = exc.response.text[:200]
        return ToolExecutionError(
            f"Tool '{tool_name}' failed: oracle HTTP {status}: {detail}",
            status_code=status,
            retryable=status >= 500 or status == 429,
        )
    return ToolExecutionError(f"Tool '{tool_name}' failed: {exc}", retryable=True)


class ToolExecutor:
    """Dispatches MCP tool calls to traffic-oracle HTTP API endpoints.

    Acts as an adapter between the MCP protocol and the traffic-oracle
    service running at TRAFFIC_ORACLE_URL. All calls use httpx async HTTP.
    """

    def __init__(
        self,
        traffic_oracle_url: str = "http://localhost:8081",
        client: Any | None = None,
    ) -> None:
        self._oracle_url = traffic_oracle_url.rstrip("/")
        self._client = client  # injectable for tests; lazy-created otherwise
        self._owns_client = client is None
        logger.info("tool_executor.init", oracle_url=self._oracle_url)

    async def _ensure_client(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._oracle_url,
                timeout=httpx.Timeout(30.0, connect=5.0),
            )

    async def close(self):
        if self._client and self._owns_client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _flatten_args(args: dict, schema_properties: dict | None = None) -> dict:
        """Flatten wrapper dicts that LLMs sometimes produce.

        E.g. ``{"bbox": {"min_lon": 1}}`` → ``{"min_lon": 1}`` — but ONLY for
        keys that are not real parameters of the tool. Object-typed parameters
        (like simulate_twin's ``closure_policy``) are passed through intact.
        """
        properties = schema_properties or {}
        flat: dict = {}
        for key, val in args.items():
            if isinstance(val, dict) and key not in properties:
                flat.update(val)
            else:
                flat[key] = val
        return flat

    async def dispatch(self, tool_name: str, arguments: dict) -> dict:
        """Route an MCP tool call to the corresponding traffic-oracle endpoint.

        This is an HTTP dispatch to the traffic-oracle REST API — no SQL is
        built or run here. ``execute`` is a legacy alias kept for callers
        that pass literal tool names.
        """
        await self._ensure_client()

        router = {
            "predict_traffic_flow": self._predict_traffic_flow,
            "find_convoy_routes": self._find_convoy_routes,
            "plan_diversions": self._plan_diversions,
            "evaluate_scenarios": self._evaluate_scenarios,
            "predict_eta": self._predict_eta,
            "query_shortest_path": self._query_shortest_path,
            "query_k_shortest_paths": self._query_k_shortest_paths,
            "query_segments_in_bbox": self._query_segments_in_bbox,
            "query_segment_details": self._query_segment_details,
            "get_live_traffic": self._get_live_traffic,
            "get_historical_pattern": self._get_historical_pattern,
            "simulate_twin": self._simulate_twin,
            "get_active_corridor_events": self._get_active_events,
            "city_query_entities": self._city_query_entities,
            "city_get_dossier": self._city_get_dossier,
            "city_feed": self._city_feed,
            "get_signal_state": self._get_signal_state,
            "list_units": self._list_units,
            "list_dispatches": self._list_dispatches,
            "get_interception_plan": self._get_interception_plan,
            "get_situation_log": self._get_situation_log,
            "list_enforcement_recommendations": self._list_recommendations,
            **{verb: self._canvas_handler(verb) for verb in CANVAS_VERBS},
        }

        canonical_name = TOOL_ALIASES.get(tool_name, tool_name)
        handler = router.get(canonical_name)
        if handler is None:
            raise ToolExecutionError("Unknown tool: " + str(tool_name), retryable=False)
        if not isinstance(arguments, dict):
            raise ToolExecutionError(
                f"Tool '{tool_name}' arguments must be an object", retryable=False
            )

        # Always try flattened args first (handles LLM nesting like {bbox:{...}});
        # object-typed parameters survive flattening via the schema properties.
        schema = _TOOL_PROPERTY_MAP.get(canonical_name, {})
        flat = self._flatten_args(arguments, schema)

        try:
            return await handler(flat)
        except (KeyError, TypeError) as exc:
            # Flattened version failed — try original if different
            if flat != arguments:
                try:
                    return await handler(arguments)
                except Exception as exc2:
                    error = _as_tool_error(str(tool_name), exc2)
                    logger.error("tool_executor.failed", tool=tool_name, error=str(error))
                    raise error from exc2
            error = _as_tool_error(str(tool_name), exc)
            logger.warning("tool_executor.bad_arguments", tool=tool_name, error=str(error))
            raise error from exc
        except ToolExecutionError:
            raise
        except Exception as exc:
            error = _as_tool_error(str(tool_name), exc)
            logger.error(
                "tool_executor.failed",
                tool=tool_name,
                status=error.status_code,
                retryable=error.retryable,
                error=str(error),
            )
            raise error from exc

    async def execute(self, tool_name: str, arguments: dict) -> dict:
        """Legacy alias for :meth:`dispatch`."""
        return await self.dispatch(tool_name, arguments)

    # -- transport (R2: POSTs only to the compute allow-list) --------------

    async def _post(self, path: str, payload: dict) -> dict:
        """POST to an allow-listed compute endpoint (anything else refused)."""
        if path not in POST_ALLOWLIST:
            raise ToolExecutionError(
                f"refused: POST {path} is not a read/compute endpoint (R2 read-only rail)",
                retryable=False,
            )
        resp = await self._client.post(path, json=payload)
        resp.raise_for_status()
        return resp.json()

    async def _read(
        self, path: str, params: dict | None = None, *, subject: str | None = None
    ) -> dict:
        """GET one oracle read; a 404 answers ``{ok: false, not_found | not_deployed}``."""
        query = {k: v for k, v in (params or {}).items() if v is not None}
        resp = await self._client.get(path, params=query)
        if resp.status_code == 404:
            return _not_found(resp, subject)
        resp.raise_for_status()
        body = resp.json()
        return body if isinstance(body, dict) else {"rows": body}

    # -- Traffic prediction ------------------------------------------------

    async def _predict_traffic_flow(self, args: dict) -> dict:
        return await self._post("/api/v1/predict/flow", {
            "segment_ids": args["segment_ids"],
            "horizons_min": args.get("horizons_min", [5, 10, 15, 30]),
        })

    # -- Route optimization ------------------------------------------------

    async def _find_convoy_routes(self, args: dict) -> dict:
        return await self._post("/api/v1/optimize/routes", {
            "origin_segment": args["origin_segment"],
            "destination_segment": args["destination_segment"],
            "max_candidates": args.get("max_candidates", 5),
            "avoid_segments": args.get("avoid_segments", []),
        })

    async def _plan_diversions(self, args: dict) -> dict:
        payload: dict[str, Any] = {
            "route_segment_ids": args["route_segment_ids"],
            "convoy_speed_kmh": args.get("convoy_speed_kmh", 60.0),
            "advance_closure_sec": args.get("advance_closure_sec", 120),
        }
        if args.get("departure_time"):
            payload["departure_time"] = args["departure_time"]
        return await self._post("/api/v1/optimize/diversions", payload)

    # -- Scenario evaluation -----------------------------------------------

    async def _evaluate_scenarios(self, args: dict) -> dict:
        return await self._post("/api/v1/evaluate/scenarios", {
            "scenarios": args["scenarios"],
        })

    # -- ETA prediction ----------------------------------------------------

    async def _predict_eta(self, args: dict) -> dict:
        """POST /api/v1/predict/eta, mapping the tool vocabulary onto the
        oracle's ETAPredictRequest.

        The tool advertises ``day_of_week`` / ``road_class_score`` (the
        names the models draft); the oracle requires ``dow`` and names the
        class score ``weighted_road_class_score`` — posting the tool
        arguments verbatim answered 422 on every call. Both spellings are
        accepted here; missing required keys fail fast (not retryable).
        """
        dow = args.get("dow", args.get("day_of_week"))
        missing = [key for key in _ETA_REQUIRED if args.get(key) is None]
        if dow is None:
            missing.append("day_of_week")
        if missing:
            raise ToolExecutionError(
                "Tool 'predict_eta' missing required argument(s): " + ", ".join(missing),
                retryable=False,
            )
        payload: dict[str, Any] = {key: args[key] for key in _ETA_REQUIRED}
        payload["dow"] = dow
        if args.get("num_signals") is not None:
            payload["num_signals"] = args["num_signals"]
        class_score = args.get("weighted_road_class_score", args.get("road_class_score"))
        if class_score is not None:
            payload["weighted_road_class_score"] = class_score
        for optional in ("segment_ids", "horizon_min"):
            if args.get(optional) is not None:
                payload[optional] = args[optional]
        return await self._post("/api/v1/predict/eta", payload)

    # -- Spatial queries ---------------------------------------------------

    async def _query_shortest_path(self, args: dict) -> dict:
        resp = await self._client.get("/api/v1/graph/shortest-path", params={
            "source": args["source_segment"],
            "target": args["target_segment"],
        })
        resp.raise_for_status()
        return resp.json()

    async def _query_k_shortest_paths(self, args: dict) -> dict:
        resp = await self._client.get("/api/v1/graph/k-shortest-paths", params={
            "source": args["source_segment"],
            "target": args["target_segment"],
            "k": args.get("k", 5),
        })
        resp.raise_for_status()
        return resp.json()

    async def _query_segments_in_bbox(self, args: dict) -> dict:
        resp = await self._client.get("/api/v1/spatial/segments", params={
            "min_lon": args["min_lon"],
            "min_lat": args["min_lat"],
            "max_lon": args["max_lon"],
            "max_lat": args["max_lat"],
        })
        resp.raise_for_status()
        return resp.json()

    async def _query_segment_details(self, args: dict) -> dict:
        resp = await self._client.get(f"/api/v1/spatial/segments/{args['segment_id']}")
        resp.raise_for_status()
        return resp.json()

    # -- Live traffic and history ------------------------------------------

    async def _get_live_traffic(self, args: dict) -> dict:
        return await self._post("/api/v1/traffic/live", {
            "segment_ids": args["segment_ids"],
        })

    async def _get_historical_pattern(self, args: dict) -> dict:
        """Same IST weekday/hour baseline over the last 7 days (contract C-P).

        Sends ``hours=168`` plus ``day_of_week`` / ``hour``. An oracle with
        the C-P basis filters itself; an older one (no ``basis``) returns
        every hour of the window, which is filtered here to the UTC buckets
        that overlap the asked IST hour (IST is +05:30, so an IST hour spans
        two UTC buckets). ``aggregates`` stay the REAL rollup;
        ``synthetic_aggregates`` (simulated feed) ride separately, labelled,
        and ``provenance`` says which one there is (real | synthetic | none).
        """
        dow = _int_arg(args["day_of_week"], 0, 6, 0)
        hour = _int_arg(args["hour"], 0, 23, 0)
        body = await self._read("/api/v1/traffic/historical", {
            "segment_id": int(args["segment_id"]),
            "hours": HISTORICAL_LOOKBACK_HOURS,
            "day_of_week": dow,
            "hour": hour,
        }, subject=f"segment:{args['segment_id']}")
        if body.get("ok") is False:
            return body
        real = [r for r in body.get("aggregates") or [] if isinstance(r, dict)]
        synthetic = [r for r in body.get("synthetic_aggregates") or [] if isinstance(r, dict)]
        filtered_by = "oracle"
        if not isinstance(body.get("basis"), dict):
            filtered_by = "brain"
            real = [r for r in real if _ist_bucket_matches(r.get("hour_utc"), dow, hour)]
            synthetic = [r for r in synthetic if _ist_bucket_matches(r.get("hour_utc"), dow, hour)]
        for row in synthetic:
            row.setdefault("data_quality", "synthetic")
        provenance = body.get("provenance")
        if provenance not in ("real", "synthetic", "none"):
            provenance = "real" if real else ("synthetic" if synthetic else "none")
        return {
            "segment_id": body.get("segment_id", args["segment_id"]),
            "aggregates": real,
            "synthetic_aggregates": synthetic,
            "provenance": provenance,
            "basis": body.get("basis") if isinstance(body.get("basis"), dict) else {
                "kind": "same_ist_weekday_hour",
                "day_of_week": dow,
                "hour": hour,
                "lookback_hours": HISTORICAL_LOOKBACK_HOURS,
                "ist_bucket_offset_min": 30,
            },
            "filtered_by": filtered_by,
            "note": (
                "synthetic rollup of the SIMULATED feed — labelled, never a calibration input"
                if provenance == "synthetic" else None
            ),
        }

    # -- Digital-twin simulation (contract C5 — proxied for the decider) ----

    async def _simulate_twin(self, args: dict) -> dict:
        payload: dict[str, Any] = {"route_segment_ids": args["route_segment_ids"]}
        for optional in ("departure_time", "closure_policy", "horizon_min", "n_replications", "seed"):
            if args.get(optional) is not None:
                payload[optional] = args[optional]
        return await self._post("/api/v1/simulate/twin", payload)

    # -- Corridor events (unified city picture, read-only) ------------------

    async def _get_active_events(self, args: dict) -> dict:
        """GET /api/v1/events/active, compacted for model context.

        Zone polygons and bulky metadata are dropped; route spines keep their
        labels and segment lists (the canvas draws exactly these). Filters:
        ``event_id`` (one event — looked up in the history when it is no
        longer active), ``movement_id`` (a movement id that IS an event id
        is treated as the event filter: pursuits carry no movement) and
        ``event_kind``. A filter that matches nothing while events are
        active returns the whole board with a note — never an empty answer
        that reads as 'no data'.
        """
        resp = await self._client.get("/api/v1/events/active")
        resp.raise_for_status()
        payload = resp.json()
        events = payload.get("events", []) if isinstance(payload, dict) else []
        events = [e for e in events if isinstance(e, dict)]

        def clean(value: Any) -> str | None:
            if not isinstance(value, str) or value.strip().lower() in _WILDCARDS:
                return None
            return value.strip()

        event_filter = clean(args.get("event_id"))
        movement_filter = clean(args.get("movement_id"))
        if movement_filter and any(str(e.get("event_id")) == movement_filter for e in events):
            # The deck's selected pursuit travels as movement_id on older
            # clients: the id names an event, not a movement.
            event_filter, movement_filter = event_filter or movement_filter, None
        kind_filter = args.get("event_kind")
        ignored_filter = None
        if not isinstance(kind_filter, str) or kind_filter.strip().lower() in _WILDCARDS:
            kind_filter = None
        elif kind_filter not in _EVENT_KINDS:
            # A drafted kind the board does not know would silently hide every
            # event; ignore it and say so.
            ignored_filter, kind_filter = kind_filter, None
        notes: list[str] = []
        if event_filter:
            try:
                event_filter = str(uuid.UUID(event_filter))
            except ValueError:
                notes.append(
                    f"event_id {event_filter[:40]!r} is not an event uuid — filter ignored")
                event_filter = None

        def keep(event: dict) -> bool:
            if event_filter and str(event.get("event_id")) != event_filter:
                return False
            if movement_filter and str(event.get("movement_id")) != movement_filter:
                return False
            return not (kind_filter and event.get("event_kind") != kind_filter)

        matched = [e for e in events if keep(e)]
        if event_filter and not matched:
            past = await self._event_from_history(event_filter)
            if past is not None:
                matched = [past]
                notes.append(
                    f"event {event_filter} is no longer active — status {past.get('status')}"
                )
        if not matched and events and (event_filter or movement_filter or kind_filter):
            notes.append(
                "no active event matches the filter "
                + ", ".join(
                    f"{k}={v}" for k, v in (
                        ("event_id", event_filter), ("movement_id", movement_filter),
                        ("event_kind", kind_filter),
                    ) if v
                )
                + f" — showing all {len(events)} active event(s) instead"
            )
            matched = events
        rows = [_event_row(e) for e in matched]
        result: dict[str, Any] = {
            "events": rows,
            "count": len(rows),
            "total_active": len(events),
        }
        if ignored_filter is not None:
            result["ignored_event_kind_filter"] = ignored_filter
        if notes:
            result["note"] = "; ".join(notes)
        return result

    async def _event_from_history(self, event_id: str) -> dict | None:
        """One event from GET /api/v1/events (completed / cancelled ones too)."""
        try:
            resp = await self._client.get("/api/v1/events", params={"limit": 200})
            if resp.status_code != 200:
                return None
            rows = (resp.json() or {}).get("events") or []
        except Exception as exc:  # history is a best-effort widening
            logger.info("tool_executor.event_history_unavailable", error=str(exc)[:200])
            return None
        return next(
            (r for r in rows if isinstance(r, dict) and str(r.get("event_id")) == event_id),
            None,
        )

    # -- City picture (C-C, read-only) --------------------------------------

    async def _city_query_entities(self, args: dict) -> dict:
        kinds = args.get("kinds")
        if isinstance(kinds, str):
            kinds = [k.strip() for k in kinds.split(",")]
        wanted = [k for k in (kinds or []) if k in _CITY_ENTITY_KINDS_DEF]
        unknown = [k for k in (kinds or []) if k and k not in _CITY_ENTITY_KINDS_DEF]
        params: dict[str, Any] = {"kinds": ",".join(dict.fromkeys(wanted)) or None}
        bbox = _bbox_arg(args.get("bbox"))
        if bbox is not None:
            params["bbox"] = ",".join(f"{v:.6f}" for v in bbox)
        body = await self._read("/api/v1/city/entities", params)
        if body.get("ok") is False:
            return body
        match = args.get("match")
        needle = _norm(match)[:64] if isinstance(match, str) and match.strip() else ""
        entities = [
            e for e in body.get("entities") or []
            if isinstance(e, dict) and (not wanted or e.get("kind") in wanted)
        ]
        if needle:
            entities = [e for e in entities if needle in _haystack(e)]
            # An exact plate / id match first.
            entities.sort(key=lambda e: 0 if needle in (
                _norm((e.get("props") or {}).get("plate")), _norm(e.get("id")),
            ) else 1)
        limit = _int_arg(args.get("limit"), 1, 50, 25)
        rows = [r for r in (compact_entity(e) for e in entities[:limit]) if r]
        feeds = body.get("feeds") if isinstance(body.get("feeds"), dict) else {}
        result: dict[str, Any] = {
            # the moment this query ran — NOT when anything was seen (each
            # entity carries its own seen_ist / age_sec)
            "queried_at": body.get("as_of"),
            "count": len(rows),
            "total": len(entities),
            "entities": rows,
            "feeds": {
                k: {"count": v.get("count"), "status": v.get("status", "ok")}
                for k, v in feeds.items() if isinstance(v, dict)
            },
        }
        if needle:
            result["match"] = match.strip()[:64]
        if unknown:
            result["ignored_kinds"] = unknown
        return result

    async def _city_get_dossier(self, args: dict) -> dict:
        parsed = parse_urn(args.get("urn"))
        if parsed is None:
            raise _bad_args(
                "city_get_dossier",
                f"urn must be '<kind>:<id>' with kind in {list(URN_KINDS)}",
            )
        kind, ident = parsed
        body = await self._read(
            f"/api/v1/city/entity/{kind}/{quote(ident, safe='')}",
            {"audit_limit": 10},
            subject=f"{kind}:{ident}",
        )
        if body.get("ok") is False:
            return body
        related = []
        for row in body.get("related") or []:
            if isinstance(row, dict):
                entity = compact_entity(row.get("entity"))
                if entity:
                    related.append({"relation": row.get("relation"), **entity})
        audit = [
            {
                "t": a.get("t"),
                "kind": a.get("kind"),
                "actor": a.get("actor"),
                "actor_kind": a.get("actor_kind"),
                "text": mask_plates(a.get("text")),
                "source": a.get("source"),
            }
            for a in (body.get("audit") or [])[:8] if isinstance(a, dict)
        ]
        # Pending rows first: what waits on a named officer must reach the
        # model even when the related list is long.
        related.sort(
            key=lambda r: 0 if r.get("state") in ("pending", "requested", "flagged") else 1)
        return {
            "entity": compact_entity(body.get("entity")),
            "related": related[:16],
            "related_total": len(related),
            "audit": audit,
        }

    async def _city_feed(self, args: dict) -> dict:
        params: dict[str, Any] = {"limit": _int_arg(args.get("limit"), 1, 50, 20)}
        since = args.get("since")
        if isinstance(since, str) and since.strip():
            try:
                datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
            except ValueError:
                raise _bad_args("city_feed", "since must be an ISO-8601 instant") from None
            params["since"] = since.strip()
        kinds = args.get("kinds")
        if isinstance(kinds, str):
            kinds = [k.strip() for k in kinds.split(",")]
        wanted = [k for k in (kinds or []) if k in _CITY_FEED_KINDS_DEF]
        if wanted:
            params["kinds"] = ",".join(dict.fromkeys(wanted))
        body = await self._read("/api/v1/city/feed", params)
        if body.get("ok") is False:
            return body
        items = [
            {
                "t": i.get("t"),
                "t_ist": _seen_ist(i.get("t")),
                "kind": i.get("kind"),
                "actor": i.get("actor"),
                "actor_kind": i.get("actor_kind"),
                "text": mask_plates(i.get("text")),
                "source": i.get("source"),
                "severity": i.get("severity"),
                "urns": [u for u in (i.get("urns") or []) if isinstance(u, str)][:4],
            }
            for i in body.get("items") or [] if isinstance(i, dict)
        ]
        return {"queried_at": body.get("as_of"), "count": len(items), "items": items}

    async def _get_signal_state(self, args: dict) -> dict:
        body = await self._read("/api/v1/signals/state")
        if body.get("ok") is False:
            return body
        junctions = []
        for j in body.get("junctions") or []:
            if not isinstance(j, dict) or j.get("junction_id") is None:
                continue
            phase = j.get("active_phase") if isinstance(j.get("active_phase"), dict) else {}
            junctions.append({
                "urn": f"junction:{j['junction_id']}",
                "junction_id": j["junction_id"],
                "lon": j.get("lon"),
                "lat": j.get("lat"),
                "cycle_sec": j.get("cycle_sec"),
                "green_ratio": j.get("green_ratio"),
                "phase": {
                    "index": phase.get("index"),
                    "all_red": bool(phase.get("lost")),
                    "remaining_sec": phase.get("remaining_sec"),
                    "movement_states": phase.get("movement_states"),
                },
                "source": j.get("source"),
            })
        return {
            "count": body.get("count", len(junctions)),
            "city_band": body.get("city_band"),
            "junctions": junctions[:30],
            "phase_basis": "program clock (not controller telemetry)",
        }

    async def _list_units(self, args: dict) -> dict:
        agency = args.get("agency")
        if isinstance(agency, str) and agency.strip().lower() in _WILDCARDS:
            agency = None
        if agency is not None and agency not in UNIT_AGENCIES:
            raise _bad_args("list_units", f"agency must be one of {list(UNIT_AGENCIES)}")
        body = await self._read("/api/v1/units")
        if body.get("ok") is False:
            return body
        units = [
            {
                "urn": f"unit:{u.get('unit_id')}",
                "unit_id": u.get("unit_id"),
                "agency": u.get("agency"),
                "label": mask_plates(u.get("label") or u.get("unit_id")),
                "status": u.get("status"),
                "lon": u.get("lon"),
                "lat": u.get("lat"),
                "source": u.get("source") or "emulated",
            }
            for u in body.get("units") or []
            if isinstance(u, dict) and u.get("unit_id")
            and (agency is None or u.get("agency") == agency)
        ]
        return {
            "count": len(units),
            "units": units[:30],
            "source": body.get("source", "emulated"),
            "note": body.get("note") or (
                "no units fielded right now" if not units else None
            ),
        }

    async def _list_dispatches(self, args: dict) -> dict:
        params: dict[str, Any] = {"limit": _int_arg(args.get("limit"), 1, 50, 20)}
        event_id = args.get("event_id")
        if isinstance(event_id, str) and event_id.strip().lower() not in _WILDCARDS:
            params["event_id"] = _uuid_arg("list_dispatches", event_id)
        status = args.get("status")
        if isinstance(status, str) and status.strip().lower() not in _WILDCARDS:
            if not _STATUS_RE.fullmatch(status.strip()):
                raise _bad_args("list_dispatches", "status must be a status word")
            params["status"] = status.strip()
        body = await self._read("/api/v1/dispatches", params)
        if body.get("ok") is False:
            return body
        rows = [
            {
                "urn": f"dispatch:{d.get('dispatch_id')}",
                "event_urn": f"event:{d.get('event_id')}" if d.get("event_id") else None,
                "agency": d.get("agency"),
                "unit_id": d.get("unit_id"),
                "status": d.get("status"),
                "requested_by": d.get("requested_by"),
                "decided_by": d.get("decided_by"),
                "created_at": d.get("created_at"),
            }
            for d in body.get("dispatches") or [] if isinstance(d, dict)
        ]
        return {"count": len(rows), "dispatches": rows}

    async def _get_interception_plan(self, args: dict) -> dict:
        event_id = _uuid_arg("get_interception_plan", args.get("event_id"))
        body = await self._read(
            f"/api/v1/events/{event_id}/interception-plan", subject=f"event:{event_id}"
        )
        if body.get("ok") is False:
            return body
        sighting = body.get("latest_sighting")
        sighting = sighting if isinstance(sighting, dict) else None
        direction = body.get("direction_of_travel")
        direction = direction if isinstance(direction, dict) else {}
        chokes = [
            {
                "urn": f"junction:{c.get('junction_id')}",
                "name": c.get("name"),
                "junction_type": c.get("junction_type"),
                "graph_cost_sec": c.get("graph_cost_sec"),
                "likely_passed": c.get("likely_passed"),
            }
            for c in body.get("interception_junctions") or [] if isinstance(c, dict)
        ]
        vehicle = body.get("vehicle_id")
        return {
            "event_urn": f"event:{event_id}",
            "vehicle_urn": f"vehicle:{vehicle}" if vehicle else None,
            "latest_sighting": {
                "urn": (
                    f"sighting:{sighting.get('sighting_id')}"
                    if sighting.get("sighting_id") else None
                ),
                "camera_id": sighting.get("camera_id"),
                "t": sighting.get("t"),
                "position": sighting.get("position"),
                "speed_kmh": sighting.get("speed_kmh"),
                "heading": sighting.get("heading"),
                "source": sighting.get("source"),
            } if sighting else None,
            "sighting_age_sec": body.get("sighting_age_sec"),
            "stale": body.get("stale"),
            "direction_of_travel": {
                "basis": direction.get("basis"),
                "towards_junction_ids": direction.get("towards_junction_ids"),
                "against_oneway": direction.get("against_oneway"),
                "onward_segment_ids": direction.get("onward_segment_ids"),
            },
            "chokes": chokes[:6],
            "method": body.get("method"),
            "note": mask_plates(body.get("note")) if body.get("note") else None,
        }

    async def _get_situation_log(self, args: dict) -> dict:
        params: dict[str, Any] = {"limit": _int_arg(args.get("limit"), 1, 50, 12)}
        subject = args.get("event_id")
        if isinstance(subject, str) and subject.strip().lower() not in _WILDCARDS:
            if not _SLUG_RE.fullmatch(subject.strip()):
                raise _bad_args("get_situation_log", "event_id must be an event or movement id")
            params["event_id"] = subject.strip()
        body = await self._read("/api/v1/adaptive/situation_log", params)
        if body.get("ok") is False:
            return body
        entries = [
            {
                "subject": e.get("movement_id") or e.get("event_id"),
                "band": e.get("situation_class"),
                "observed": e.get("observed_class"),
                "code": e.get("situation_code"),
                "severity_score": e.get("severity_score"),
                "transitioned": e.get("transitioned"),
                "t": e.get("timestamp_utc"),
                "emulated": e.get("emulated"),
                **({"simulated_feed": e["simulated_feed"]} if "simulated_feed" in e else {}),
            }
            for e in body.get("entries") or [] if isinstance(e, dict)
        ]
        return {"count": len(entries), "entries": entries, "subject": body.get("subject")}

    async def _list_recommendations(self, args: dict) -> dict:
        status = args.get("status") or "pending"
        if status not in ("pending", "approved", "rejected"):
            raise _bad_args("list_enforcement_recommendations",
                            "status must be pending, approved or rejected")
        body = await self._read("/api/v1/enforcement/recommendations", {
            "status": status, "limit": _int_arg(args.get("limit"), 1, 50, 20),
        })
        if body.get("ok") is False:
            return body
        rows = []
        for r in body.get("recommendations") or []:
            if not isinstance(r, dict):
                continue
            evidence = r.get("evidence") if isinstance(r.get("evidence"), dict) else {}
            rows.append({
                "urn": f"recommendation:{r.get('recommendation_id')}",
                "vehicle_urn": f"vehicle:{r.get('vehicle_id')}" if r.get("vehicle_id") else None,
                "rule_id": r.get("rule_id"),
                "status": r.get("status"),
                "recommendation": mask_plates(r.get("recommendation")),
                "evidence": {
                    k: evidence.get(k) for k in (
                        "speed_kmh", "speed_limit_kmh", "road_name", "camera_id", "t", "source",
                    ) if evidence.get(k) is not None
                },
                "decided_by": r.get("decided_by"),
                "decided_at": r.get("decided_at"),
                "created_at": r.get("created_at"),
            })
        return {
            "status": status,
            "count": len(rows),
            "recommendations": rows,
            "rail": "machine-proposed only — a named officer approves or rejects every one",
        }

    # -- Canvas verbs (directives; never HTTP) --------------------------------

    def _canvas_handler(self, verb: str):
        async def handler(args: dict) -> dict:
            return canvas_directive(verb, args)
        return handler

    # -- Resource reading --------------------------------------------------

    async def read_resource(self, uri: str) -> dict:
        """Fetch resource content by URI."""
        await self._ensure_client()

        if uri == "traffic://anomalies/recent":
            resp = await self._client.get("/api/v1/anomalies/recent")
            resp.raise_for_status()
            return resp.json()

        if uri == "traffic://corridor/summary":
            resp = await self._client.get("/api/v1/corridor/summary")
            resp.raise_for_status()
            return resp.json()

        if uri.startswith("traffic://segments/") and uri.endswith("/history"):
            # traffic://segments/{id}/history -> ['traffic:', '', 'segments', id, 'history']
            segment_id = uri.split("/")[3]
            # POST /mcp serves this over HTTP: the id is an integer before it
            # becomes part of an oracle path ('1012/../../x' is no segment).
            if (len(uri.split("/")) != 5 or not segment_id.isascii()
                    or not segment_id.isdigit() or not 0 < int(segment_id) <= 2**62):
                raise ToolExecutionError(
                    f"Malformed segment resource URI: {uri[:120]}", retryable=False)
            resp = await self._client.get(f"/api/v1/traffic/history/{int(segment_id)}")
            resp.raise_for_status()
            return resp.json()

        if uri == "convoy://movements/active":
            resp = await self._client.get("/api/v1/movements/active")
            resp.raise_for_status()
            return resp.json()

        raise ToolExecutionError(f"Unknown resource URI: {uri}")

    # -- Prompt templates --------------------------------------------------

    def get_prompt(self, name: str, arguments: dict) -> list[dict]:
        """Generate prompt messages for a named template."""
        if name == "vvip_security_protocol":
            return self._prompt_security_protocol(arguments)
        if name == "route_analysis_brief":
            return self._prompt_route_analysis(arguments)
        if name == "diversion_coordination_brief":
            return self._prompt_diversion_coordination(arguments)
        raise ToolExecutionError(f"Unknown prompt: {name}")

    def _prompt_security_protocol(self, args: dict) -> list[dict]:
        vvip_class = args.get("vvip_class", "Y")
        protocols = {
            "Z+": {
                "classification": "Z+ (Highest Threat — PM, President, visiting Head of State)",
                "min_road_width": "6 lanes minimum",
                "closure_type": "Full road closure mandatory",
                "route_constraints": (
                    "Flyovers and elevated corridors strongly preferred. "
                    "NO markets, NO dense residential zones, NO narrow lanes. "
                    "All intersecting roads closed 3 minutes before convoy arrival. "
                    "Counter-assault teams at every major junction."
                ),
                "diversion_advance_sec": 180,
                "max_queue_tolerance_m": 2000,
            },
            "Z": {
                "classification": "Z (High Threat — Cabinet Ministers, Senior Judiciary)",
                "min_road_width": "4 lanes minimum",
                "closure_type": "Partial closure permitted (opposite carriageway open)",
                "route_constraints": (
                    "Standard arterial roads acceptable. Avoid single-lane sections. "
                    "Intersecting roads held for 90 seconds before convoy passage."
                ),
                "diversion_advance_sec": 120,
                "max_queue_tolerance_m": 1000,
            },
            "Y": {
                "classification": "Y (Medium Threat — State Ministers, Service Chiefs)",
                "min_road_width": "2 lanes minimum",
                "closure_type": "Speed restriction with signal priority",
                "route_constraints": (
                    "All standard roads acceptable. Avoid known congestion "
                    "hotspots during peak hours. Signal pre-emption 60 seconds ahead."
                ),
                "diversion_advance_sec": 60,
                "max_queue_tolerance_m": 500,
            },
            "X": {
                "classification": "X (Standard VIP — MPs, Senior Bureaucrats)",
                "min_road_width": "No minimum",
                "closure_type": "Signal priority only, no road closure",
                "route_constraints": (
                    "Green-wave signal coordination along primary route. "
                    "No road closures or diversions. Motorcycle escort for gap creation."
                ),
                "diversion_advance_sec": 0,
                "max_queue_tolerance_m": 0,
            },
            # Event-agnostic extension (core stream): 'none' is a public /
            # civic corridor event — no protectee, crowd-safety doctrine.
            "none": {
                "classification": (
                    "none (Public/Civic Event — procession, mass gathering, "
                    "race, ceremonial, egress surge, evacuation, work zone)"
                ),
                "min_road_width": "No protectee minimum; crowd capacity governs",
                "closure_type": "Managed per event plan (staged, signal_hold common)",
                "route_constraints": (
                    "Crowd class drives the plan: hardened perimeters for "
                    "EXTREME crowds, staged ingress/egress instead of blanket "
                    "freeze, emergency-vehicle lanes kept clear at all times. "
                    "Published diversion and phase windows for multi-day events."
                ),
                "diversion_advance_sec": 0,
                "max_queue_tolerance_m": 1500,
            },
        }
        protocol = protocols.get(vvip_class, protocols["Y"])
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"## VVIP Security Protocol — {protocol['classification']}\n\n"
                        f"**Minimum Road Width**: {protocol['min_road_width']}\n"
                        f"**Closure Type**: {protocol['closure_type']}\n"
                        f"**Route Constraints**: {protocol['route_constraints']}\n"
                        f"**Diversion Advance Time**: {protocol['diversion_advance_sec']} seconds\n"
                        f"**Maximum Queue Tolerance**: {protocol['max_queue_tolerance_m']} meters\n\n"
                        "You MUST enforce these constraints when evaluating routes and diversions. "
                        "Any route violating the minimum road width or closure type requirements "
                        "MUST be rejected with an explicit compliance failure reason."
                    ),
                },
            },
        ]

    def _prompt_route_analysis(self, args: dict) -> list[dict]:
        origin = args.get("origin_name", "Origin")
        destination = args.get("destination_name", "Destination")
        vvip_class = args.get("vvip_class", "Y")
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"## Route Analysis Brief: {origin} → {destination}\n"
                        f"**VVIP Classification**: {vvip_class}\n\n"
                        "Analyze the route candidates using the following scoring criteria:\n\n"
                        "1. **Transit Time** (weight 0.3): Total travel time including signal delays.\n"
                        "   Call `predict_traffic_flow` for real-time speed predictions.\n\n"
                        "2. **Public Disruption** (weight 0.4): Total vehicle-hours of delay caused\n"
                        "   to civilian traffic. Call `plan_diversions` to estimate queue impact.\n\n"
                        "3. **Security Score** (weight 0.2): Road width, chokepoints, alternative\n"
                        "   exits, crowd exposure. Use `query_segment_details` per segment.\n\n"
                        "4. **Route Complexity** (weight 0.1): Number of turns, signal intersections,\n"
                        "   and diversion points. Simpler routes reduce coordination failure risk.\n\n"
                        "First call `find_convoy_routes` to get candidates, then evaluate each.\n"
                        "Respond with a ranked JSON comparison table."
                    ),
                },
            },
        ]

    def _prompt_diversion_coordination(self, args: dict) -> list[dict]:
        movement_id = args.get("movement_id", "UNKNOWN")
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"## Diversion Coordination Brief — Movement {movement_id}\n\n"
                        "You are coordinating traffic diversions between three agencies:\n"
                        "- **Traffic Police**: Signal control and physical barriers\n"
                        "- **Transport Department**: Public transport rerouting\n"
                        "- **Security Agencies**: Counter-assault positioning\n\n"
                        "**Critical Rule**: Closure on any segment must NOT exceed the minimum\n"
                        "necessary duration. Target: activate no more than 120 seconds before\n"
                        "convoy arrival, deactivate immediately after passage.\n\n"
                        "Steps:\n"
                        "1. Call `plan_diversions` with the approved route segments\n"
                        "2. For each diversion, verify alternative route capacity using\n"
                        "   `predict_traffic_flow` on the diversion segments\n"
                        "3. If any alternative route shows congestion_idx > 0.7, flag it\n"
                        "   and consider timing adjustment\n"
                        "4. Produce a per-segment activation timeline in JSON format"
                    ),
                },
            },
        ]


# ---------------------------------------------------------------------------
# Pure helpers (events, history buckets, canvas directives)
# ---------------------------------------------------------------------------

#: get_historical_pattern lookback: one week of the same weekday/hour.
HISTORICAL_LOOKBACK_HOURS = 168
_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_bucket_matches(hour_utc: Any, day_of_week: int, hour: int) -> bool:
    """True when a UTC hourly bucket overlaps IST ``hour`` on IST ``day_of_week``.

    IST is +05:30: the IST hour H:00–H+1:00 spans the UTC buckets starting
    at IST (H-1):30 and H:30 — both count.
    """
    if not isinstance(hour_utc, str):
        return False
    try:
        start = datetime.fromisoformat(hour_utc.replace("Z", "+00:00"))
    except ValueError:
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    ist_start = start.astimezone(_IST)
    ist_end = ist_start + timedelta(hours=1)
    target_start = ist_start.replace(hour=hour, minute=0, second=0, microsecond=0)
    for day_shift in (-1, 0, 1):
        window = target_start + timedelta(days=day_shift)
        if window.weekday() != day_of_week:
            continue
        if ist_start < window + timedelta(hours=1) and ist_end > window:
            return True
    return False


def _event_row(event: dict) -> dict:
    """One corridor event as model context (spines and phases, no zones)."""
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    row: dict[str, Any] = {
        "urn": f"event:{event.get('event_id')}",
        "event_id": event.get("event_id"),
        "event_kind": event.get("event_kind"),
        "title": mask_plates(event.get("title")),
        "status": event.get("status"),
        "protection_class": event.get("protection_class"),
        "crowd_class": event.get("crowd_class"),
        "movement_id": event.get("movement_id"),
        "planned_start": event.get("planned_start"),
        "planned_end": event.get("planned_end"),
        "routes": [
            {"label": r.get("label"), "segment_ids": r.get("segment_ids", [])}
            for r in event.get("routes", []) if isinstance(r, dict)
        ],
        "phases": [
            {
                "label": p.get("label"),
                "window_start": p.get("window_start"),
                "window_end": p.get("window_end"),
            }
            for p in event.get("phases", []) if isinstance(p, dict)
        ],
        "source": metadata.get("source"),
    }
    vehicle = metadata.get("vehicle_id")
    if isinstance(vehicle, str) and parse_urn(f"vehicle:{vehicle}"):
        row["vehicle_urn"] = f"vehicle:{vehicle.lower()}"
        vehicle_label = plate_label(event.get("title"))
        if vehicle_label:
            row["vehicle_label"] = vehicle_label
    if metadata.get("label"):
        row["label"] = mask_plates(metadata["label"])
    return row


def _bbox_arg(value: Any) -> tuple[float, float, float, float] | None:
    """[min_lon, min_lat, max_lon, max_lat] (list, object or CSV) inside the city."""
    if isinstance(value, dict):
        value = [value.get(k) for k in ("min_lon", "min_lat", "max_lon", "max_lat")]
    elif isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        box = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in box) or box[0] >= box[2] or box[1] >= box[3]:
        return None
    lo_lon, lo_lat, hi_lon, hi_lat = CANVAS_BBOX
    clipped = (max(box[0], lo_lon), max(box[1], lo_lat), min(box[2], hi_lon), min(box[3], hi_lat))
    return clipped if clipped[0] < clipped[2] and clipped[1] < clipped[3] else None


def _urn_arg(verb: str, value: Any) -> str:
    urn = canonical_urn(value)
    if urn is None:
        raise _bad_args(verb, f"urn must be '<kind>:<id>' with kind in {list(URN_KINDS)}")
    return urn


def canvas_directive(verb: str, args: dict) -> dict:
    """Validate one canvas verb's arguments into a directive (pure, no HTTP).

    The directive is a PROPOSAL: the orchestrator streams it to the deck,
    where the operator applies or dismisses it. Malformed arguments are a
    non-retryable tool error (an unknown URN, coordinates outside the city,
    a note over 140 characters, an unknown sensor or layer).
    """
    if verb == "canvas_fly_to":
        clean: dict[str, Any] = {}
        if args.get("urn") not in (None, ""):
            clean["urn"] = _urn_arg(verb, args.get("urn"))
            label = f"fly to {clean['urn']}"
        else:
            lon, lat = _finite(args.get("lon")), _finite(args.get("lat"))
            if lon is None or lat is None:
                raise _bad_args(verb, "give an entity urn, or lon and lat")
            if not _in_city(lon, lat):
                raise _bad_args(
                    verb, f"({lon}, {lat}) is outside the city operating area {CANVAS_BBOX}")
            clean["lon"], clean["lat"] = round(lon, 6), round(lat, 6)
            label = f"fly to {clean['lon']:.4f}, {clean['lat']:.4f}"
        rng = _finite(args.get("range_m"))
        if rng is not None:
            clean["range_m"] = round(min(FLY_RANGE_M[1], max(FLY_RANGE_M[0], rng)))
    elif verb == "canvas_track_entity":
        clean = {"urn": _urn_arg(verb, args.get("urn"))}
        label = f"track {clean['urn']}"
    elif verb == "canvas_highlight":
        raw = args.get("urns")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not 1 <= len(raw) <= HIGHLIGHT_MAX:
            raise _bad_args(verb, f"urns must list 1..{HIGHLIGHT_MAX} entity URNs")
        clean = {"urns": list(dict.fromkeys(_urn_arg(verb, u) for u in raw))}
        label = f"highlight {len(clean['urns'])} entit{'y' if len(clean['urns']) == 1 else 'ies'}"
    elif verb == "canvas_annotate":
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _bad_args(verb, "text is required")
        text = " ".join(text.split())
        if len(text) > ANNOTATION_MAX_CHARS:
            raise _bad_args(verb, f"text must be <= {ANNOTATION_MAX_CHARS} characters")
        severity = args.get("severity") or "info"
        if severity not in ANNOTATION_SEVERITIES:
            raise _bad_args(verb, f"severity must be one of {list(ANNOTATION_SEVERITIES)}")
        clean = {
            "urn": _urn_arg(verb, args.get("urn")), "text": mask_plates(text),
            "severity": severity,
        }
        label = f"note on {clean['urn']}: {clean['text']}"
    elif verb == "canvas_set_sensor":
        mode = args.get("mode")
        if mode not in SENSOR_MODES:
            raise _bad_args(verb, f"mode must be one of {list(SENSOR_MODES)}")
        clean = {"mode": mode}
        label = f"sensor {mode}"
    elif verb == "canvas_toggle_layer":
        layer = args.get("layer_id")
        if layer not in CANVAS_LAYERS:
            raise _bad_args(verb, f"layer_id must be one of {sorted(CANVAS_LAYERS)}")
        on = args.get("on")
        if not isinstance(on, bool):
            raise _bad_args(verb, "on must be true or false")
        clean = {"layer_id": layer, "on": on}
        label = f"layer {layer} {'on' if on else 'off'}"
    else:  # pragma: no cover - the router only maps CANVAS_VERBS here
        raise ToolExecutionError(f"Unknown canvas verb: {verb}", retryable=False)
    return {
        "ok": True,
        "directive": {"verb": verb, "args": clean, "label": label},
        "note": "proposal only — the operator applies or dismisses it on the God's-eye canvas",
    }


# ---------------------------------------------------------------------------
# STDIO transport
# ---------------------------------------------------------------------------

async def run_stdio_transport(server: MCPServer) -> None:
    """Run the MCP server over STDIO (newline-delimited JSON-RPC 2.0).

    Reads JSON-RPC messages from stdin (one per line), processes them,
    and writes responses to stdout (one per line).
    """
    import sys

    logger.info("mcp_server.stdio.start")
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)

    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

    w_transport, w_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(w_transport, w_protocol, reader, loop)

    while True:
        line = await reader.readline()
        if not line:
            break
        raw = line.decode("utf-8").strip()
        if not raw:
            continue

        response = await server.handle_message(raw)
        if response is not None:
            writer.write((response + "\n").encode("utf-8"))
            await writer.drain()

    logger.info("mcp_server.stdio.shutdown")


def _jsonrpc_error(msg_id: int | str | None, code: int, message: str) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    })
