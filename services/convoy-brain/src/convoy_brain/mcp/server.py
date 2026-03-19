"""MCP Server — exposes traffic-oracle capabilities as standard MCP primitives.

Implements a lightweight Model Context Protocol server with:
- Resources: read-only data access (live traffic, anomalies, corridor summaries)
- Tools: executable functions (route optimizer, flow forecaster, ETA predictor,
  diversion planner, scenario evaluator, spatial queries)
- Prompts: reusable system prompt templates for VVIP security protocols

Transport: STDIO (for same-host Docker deployment) or Streamable HTTP.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tool / Resource / Prompt schema definitions
# ---------------------------------------------------------------------------

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
            "Find optimal convoy routes between origin and destination segments. "
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
            "Generate per-segment traffic diversion plans for a convoy route. "
            "Computes activation/deactivation timing, alternative routes, "
            "queue length estimates, and closure types (full/partial/speed restriction)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "route_segment_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Ordered list of segment IDs along the convoy route.",
                },
                "convoy_speed_kmh": {
                    "type": "number",
                    "description": "Expected convoy speed in km/h. Default: 60.",
                },
                "advance_closure_sec": {
                    "type": "integer",
                    "description": "Seconds before convoy arrival to activate closure. Default: 120.",
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
                "day_of_week": {"type": "integer", "description": "Day of week (0=Monday)."},
                "num_signals": {"type": "integer", "description": "Traffic signals on route."},
                "road_class_score": {"type": "number", "description": "Weighted road class score (10-100)."},
            },
            "required": [
                "route_length_m", "num_segments", "avg_predicted_speed",
                "avg_predicted_congestion", "hour", "day_of_week",
                "num_signals", "road_class_score",
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
            "Retrieve historical traffic patterns for a segment at a specific "
            "day-of-week and hour. Returns average speed, p50/p95 statistics "
            "from DuckDB hourly aggregates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "segment_id": {"type": "integer"},
                "day_of_week": {"type": "integer", "description": "0=Monday, 6=Sunday."},
                "hour": {"type": "integer", "description": "Hour of day (0-23)."},
            },
            "required": ["segment_id", "day_of_week", "hour"],
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
    SERVER_VERSION = "0.1.0"

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
    """Raised when a tool call fails in a recoverable way."""


class ToolExecutor:
    """Dispatches MCP tool calls to traffic-oracle HTTP API endpoints.

    Acts as an adapter between the MCP protocol and the traffic-oracle
    service running at TRAFFIC_ORACLE_URL. All calls use httpx async HTTP.
    """

    def __init__(self, traffic_oracle_url: str = "http://localhost:8081") -> None:
        self._oracle_url = traffic_oracle_url.rstrip("/")
        self._client = None
        logger.info("tool_executor.init", oracle_url=self._oracle_url)

    async def _ensure_client(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                base_url=self._oracle_url,
                timeout=httpx.Timeout(30.0, connect=5.0),
            )

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _flatten_args(args: dict) -> dict:
        """Flatten single-level nested dicts that LLMs sometimes produce.

        E.g. ``{"bbox": {"min_lon": 1}}`` → ``{"min_lon": 1}`` when the only
        value is a dict.  Also merges when an outer key wraps the real params.
        """
        flat = {}
        for key, val in args.items():
            if isinstance(val, dict):
                flat.update(val)
            else:
                flat[key] = val
        return flat

    async def execute(self, tool_name: str, arguments: dict) -> dict:
        """Route a tool call to the corresponding traffic-oracle endpoint."""
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
        }

        handler = router.get(tool_name)
        if handler is None:
            raise ToolExecutionError(f"Unknown tool: {tool_name}")

        # Always try flattened args first (handles LLM nesting like {bbox:{...}})
        flat = self._flatten_args(arguments)

        try:
            return await handler(flat)
        except (KeyError, TypeError):
            # Flattened version failed — try original if different
            if flat != arguments:
                try:
                    return await handler(arguments)
                except Exception as exc2:
                    logger.error("tool_executor.failed", tool=tool_name, error=str(exc2))
                    raise ToolExecutionError(f"Tool '{tool_name}' failed: {exc2}") from exc2
            raise
        except ToolExecutionError:
            raise
        except Exception as exc:
            logger.error("tool_executor.failed", tool=tool_name, error=str(exc))
            raise ToolExecutionError(f"Tool '{tool_name}' failed: {exc}") from exc

    # -- Traffic prediction ------------------------------------------------

    async def _predict_traffic_flow(self, args: dict) -> dict:
        resp = await self._client.post("/api/v1/predict/flow", json={
            "segment_ids": args["segment_ids"],
            "horizons_min": args.get("horizons_min", [5, 10, 15, 30]),
        })
        resp.raise_for_status()
        return resp.json()

    # -- Route optimization ------------------------------------------------

    async def _find_convoy_routes(self, args: dict) -> dict:
        resp = await self._client.post("/api/v1/optimize/routes", json={
            "origin_segment": args["origin_segment"],
            "destination_segment": args["destination_segment"],
            "max_candidates": args.get("max_candidates", 5),
            "avoid_segments": args.get("avoid_segments", []),
        })
        resp.raise_for_status()
        return resp.json()

    async def _plan_diversions(self, args: dict) -> dict:
        payload: dict[str, Any] = {
            "route_segment_ids": args["route_segment_ids"],
            "convoy_speed_kmh": args.get("convoy_speed_kmh", 60.0),
            "advance_closure_sec": args.get("advance_closure_sec", 120),
        }
        if args.get("departure_time"):
            payload["departure_time"] = args["departure_time"]
        resp = await self._client.post("/api/v1/optimize/diversions", json=payload)
        resp.raise_for_status()
        return resp.json()

    # -- Scenario evaluation -----------------------------------------------

    async def _evaluate_scenarios(self, args: dict) -> dict:
        resp = await self._client.post("/api/v1/evaluate/scenarios", json={
            "scenarios": args["scenarios"],
        })
        resp.raise_for_status()
        return resp.json()

    # -- ETA prediction ----------------------------------------------------

    async def _predict_eta(self, args: dict) -> dict:
        resp = await self._client.post("/api/v1/predict/eta", json=args)
        resp.raise_for_status()
        return resp.json()

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
        resp = await self._client.post("/api/v1/traffic/live", json={
            "segment_ids": args["segment_ids"],
        })
        resp.raise_for_status()
        return resp.json()

    async def _get_historical_pattern(self, args: dict) -> dict:
        resp = await self._client.get("/api/v1/traffic/historical", params={
            "segment_id": args["segment_id"],
            "day_of_week": args["day_of_week"],
            "hour": args["hour"],
        })
        resp.raise_for_status()
        return resp.json()

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
            segment_id = uri.split("/")[2]
            resp = await self._client.get(f"/api/v1/traffic/history/{segment_id}")
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
