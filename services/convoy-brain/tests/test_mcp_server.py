"""MCP server tests — tool surface, dispatch, simulate_twin proxy, prompts."""

from __future__ import annotations

import json

import pytest

from convoy_brain.mcp.server import MCPServer, TOOL_DEFINITIONS
from conftest import FakeExecutor


EXPECTED_11 = [
    "predict_traffic_flow",
    "find_convoy_routes",
    "plan_diversions",
    "evaluate_scenarios",
    "predict_eta",
    "query_shortest_path",
    "query_k_shortest_paths",
    "query_segments_in_bbox",
    "query_segment_details",
    "get_live_traffic",
    "get_historical_pattern",
]


class TestToolSurface:
    def test_eleven_original_tools_survive(self):
        names = [t["name"] for t in TOOL_DEFINITIONS]
        for tool in EXPECTED_11:
            assert tool in names

    def test_simulate_twin_added(self):
        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert "simulate_twin" in names
        assert len(names) == 12
        twin = next(t for t in TOOL_DEFINITIONS if t["name"] == "simulate_twin")
        assert twin["inputSchema"]["required"] == ["route_segment_ids"]

    def test_every_tool_has_input_schema(self):
        for tool in TOOL_DEFINITIONS:
            assert tool["inputSchema"]["type"] == "object"
            assert "properties" in tool["inputSchema"]

    def test_no_qwen_vocabulary_anywhere(self):
        blob = json.dumps(TOOL_DEFINITIONS).lower()
        assert "qwen" not in blob
        assert "ollama" not in blob
        assert "no_think" not in blob


class TestJsonRpcDispatch:
    async def test_tools_list(self):
        server = MCPServer(tool_executor=FakeExecutor())
        response = await server.handle_message(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        }))
        payload = json.loads(response)
        assert len(payload["result"]["tools"]) == 12

    async def test_tools_call_dispatches(self):
        executor = FakeExecutor(routes={"get_live_traffic": {"segments": []}})
        server = MCPServer(tool_executor=executor)
        response = await server.handle_message(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_live_traffic", "arguments": {"segment_ids": [1]}},
        }))
        payload = json.loads(response)
        assert payload["result"]["content"][0]["type"] == "text"
        assert executor.calls == [("get_live_traffic", {"segment_ids": [1]})]

    async def test_unknown_method_error(self):
        server = MCPServer(tool_executor=FakeExecutor())
        response = await server.handle_message(json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "bogus/method",
        }))
        payload = json.loads(response)
        assert payload["error"]["code"] == -32601

    async def test_parse_error(self):
        server = MCPServer(tool_executor=FakeExecutor())
        response = await server.handle_message("not json{")
        assert json.loads(response)["error"]["code"] == -32700


class TestPrompts:
    def test_security_protocol_z_plus(self):
        executor = FakeExecutor()
        messages = executor.get_prompt("vvip_security_protocol", {"vvip_class": "Z+"})
        text = messages[0]["content"]["text"]
        assert "180 seconds" in text
        assert "6 lanes" in text

    def test_security_protocol_unknown_class_defaults_y(self):
        executor = FakeExecutor()
        messages = executor.get_prompt("vvip_security_protocol", {"vvip_class": "???"})
        assert "Y (" in messages[0]["content"]["text"]

    def test_route_analysis_brief(self):
        executor = FakeExecutor()
        messages = executor.get_prompt("route_analysis_brief", {
            "origin_name": "A", "destination_name": "B", "vvip_class": "Z",
        })
        assert "A → B" in messages[0]["content"]["text"]


class TestSimulateTwinProxy:
    """The simulate_twin MCP tool must proxy POST /api/v1/simulate/twin (C5)."""

    async def test_handler_posts_to_simulate_endpoint(self):
        import httpx

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["json"] = json.loads(request.content)
            return httpx.Response(200, json={
                "runs": [{"replication": 1, "total_disruption_vehicle_hours": 4.2}],
                "calibrated": False,
            })

        from convoy_brain.mcp.server import ToolExecutor

        executor = ToolExecutor(
            traffic_oracle_url="http://localhost:8081",
            client=httpx.AsyncClient(
                base_url="http://localhost:8081",
                transport=httpx.MockTransport(handler),
            ),
        )
        result = await executor.dispatch("simulate_twin", {
            "route_segment_ids": [1, 2, 3],
            "n_replications": 8,
            "closure_policy": {"advance_closure_sec": 120, "closure_type": "full"},
        })
        assert seen["path"] == "/api/v1/simulate/twin"
        assert seen["json"]["route_segment_ids"] == [1, 2, 3]
        assert seen["json"]["closure_policy"]["advance_closure_sec"] == 120
        assert result["calibrated"] is False
        await executor.close()

    async def test_unknown_tool_raises(self):
        from convoy_brain.mcp.server import ToolExecutionError, ToolExecutor

        executor = ToolExecutor(traffic_oracle_url="http://localhost:8081")
        with pytest.raises(ToolExecutionError, match="Unknown tool"):
            await executor.dispatch("traffic_fetch", {})  # phantom legacy name
        await executor.close()

    async def test_flatten_args_unwraps_nested(self):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            seen["json"] = json.loads(request.content)
            return httpx.Response(200, json={})

        seen: dict = {}
        from convoy_brain.mcp.server import ToolExecutor

        executor = ToolExecutor(
            traffic_oracle_url="http://localhost:8081",
            client=httpx.AsyncClient(
                base_url="http://localhost:8081",
                transport=httpx.MockTransport(handler),
            ),
        )
        await executor.dispatch("get_live_traffic", {
            "arguments": {"segment_ids": [9]},  # classic LLM nesting
        })
        assert seen["json"] == {"segment_ids": [9]}
        await executor.close()
