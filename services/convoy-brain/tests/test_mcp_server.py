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
        # 12 compute/read tools + get_active_corridor_events + 9 read-only
        # city tools + 6 canvas verbs (wave 3, C-D2)
        assert len(names) == 28
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
        assert len(payload["result"]["tools"]) == 28

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


def executor_with(handler):
    import httpx

    from convoy_brain.mcp.server import ToolExecutor

    return ToolExecutor(
        traffic_oracle_url="http://localhost:8081",
        client=httpx.AsyncClient(
            base_url="http://localhost:8081", transport=httpx.MockTransport(handler)
        ),
    )


class TestOracleShapeFixes:
    async def test_predict_eta_sends_dow_and_weighted_score(self):
        import httpx

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["json"] = json.loads(request.content)
            return httpx.Response(200, json={"eta_seconds": 610.0, "model": "fallback"})

        executor = executor_with(handler)
        result = await executor.dispatch("predict_eta", {
            "route_length_m": 8000, "num_segments": 6, "avg_predicted_speed": 40,
            "avg_predicted_congestion": 0.3, "hour": 9, "day_of_week": 3,
            "road_class_score": 80,
        })
        assert seen["path"] == "/api/v1/predict/eta"
        body = seen["json"]
        assert body["dow"] == 3 and "day_of_week" not in body
        assert body["weighted_road_class_score"] == 80 and "road_class_score" not in body
        assert "num_signals" not in body  # oracle default 0 applies
        assert result["eta_seconds"] == 610.0
        await executor.close()

    async def test_predict_eta_missing_keys_fail_fast(self):
        from convoy_brain.mcp.server import ToolExecutionError

        executor = executor_with(lambda r: (_ for _ in ()).throw(AssertionError("no call")))
        with pytest.raises(ToolExecutionError) as err:
            await executor.dispatch("predict_eta", {"route_length_m": 1})
        assert err.value.retryable is False
        assert "day_of_week" in str(err.value)
        await executor.close()

    async def test_active_corridor_events_reads_the_events_board(self):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path == "/api/v1/events/active"
            return httpx.Response(200, json={"events": [
                {"event_id": "e1", "event_kind": "protected_movement", "title": "Z run",
                 "status": "planning", "protection_class": "Z", "movement_id": "m1",
                 "routes": [{"label": "primary", "segment_ids": [1, 2]}],
                 "phases": [], "metadata": {"source": "convoy-brain"},
                 "zone_geojson": {"type": "Polygon", "coordinates": []}},
                {"event_id": "e2", "event_kind": "vehicle_pursuit", "title": "pursuit",
                 "status": "active", "routes": [], "phases": [], "metadata": {}},
            ]})

        executor = executor_with(handler)
        everything = await executor.dispatch("get_active_corridor_events", {})
        assert everything["count"] == 2
        assert "zone_geojson" not in everything["events"][0]
        assert everything["events"][0]["routes"][0]["label"] == "primary"
        only = await executor.dispatch("get_active_corridor_events", {"movement_id": "m1"})
        assert [e["event_id"] for e in only["events"]] == ["e1"]
        # Drafted wildcards / unknown kinds must never hide the board.
        for wildcard in ("all", "ANY", "*", ""):
            loose = await executor.dispatch("get_active_corridor_events",
                                            {"event_kind": wildcard})
            assert loose["count"] == 2
        unknown = await executor.dispatch("get_active_corridor_events",
                                          {"event_kind": "convoys"})
        assert unknown["count"] == 2
        assert unknown["ignored_event_kind_filter"] == "convoys"
        pursuits = await executor.dispatch("get_active_corridor_events",
                                           {"event_kind": "vehicle_pursuit"})
        assert [e["event_id"] for e in pursuits["events"]] == ["e2"]
        assert pursuits["total_active"] == 2
        await executor.close()

    async def test_key_and_type_errors_are_typed_and_not_retryable(self):
        from convoy_brain.mcp.server import ToolExecutionError

        executor = executor_with(lambda r: (_ for _ in ()).throw(AssertionError("no call")))
        with pytest.raises(ToolExecutionError) as err:
            await executor.dispatch("get_live_traffic", {})
        assert err.value.retryable is False
        assert "segment_ids" in str(err.value)
        await executor.close()

    async def test_http_4xx_is_not_retryable_but_5xx_is(self):
        import httpx

        from convoy_brain.mcp.server import ToolExecutionError

        for code, retryable in ((422, False), (404, False), (429, True), (503, True)):
            executor = executor_with(lambda r, c=code: httpx.Response(c, text="nope"))
            with pytest.raises(ToolExecutionError) as err:
                await executor.dispatch("get_live_traffic", {"segment_ids": [1]})
            assert err.value.status_code == code
            assert err.value.retryable is retryable
            await executor.close()

    async def test_segment_history_resource_uses_the_segment_id(self):
        import httpx

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            return httpx.Response(200, json={"history": []})

        executor = executor_with(handler)
        await executor.read_resource("traffic://segments/101/history")
        assert seen["path"] == "/api/v1/traffic/history/101"
        await executor.close()

    def test_stdio_transport_has_its_asyncio_import(self):
        import convoy_brain.mcp.server as srv

        assert hasattr(srv, "asyncio")  # run_stdio_transport NameError'd before
