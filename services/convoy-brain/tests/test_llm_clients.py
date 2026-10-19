"""Decider client + llama bridge + parsing + schemas tests (MockTransport)."""

from __future__ import annotations

import json

import httpx
import pytest

from convoy_brain.llm.decider_client import (
    DeciderClient,
    DeciderUnavailable,
    validate_questions,
)
from convoy_brain.llm.llama_bridge import LlamaBridge, LlamaUnavailable
from convoy_brain.llm.parsing import extract_json, truncate_for_context
from convoy_brain.llm.schemas import known_schemas, load_schema


# ---------------------------------------------------------------------------
# Decider client
# ---------------------------------------------------------------------------


class TestQuestionValidation:
    def test_valid_question_set(self):
        questions = {
            "next_tool": {
                "type": "choice",
                "instructions": "pick",
                "options": ["a", "b"],
            },
            "urgency": {"type": "noul", "instructions": "how urgent"},
            "score_q": {"type": "score", "instructions": "confidence", "scale": [0.0, 1.0]},
        }
        assert validate_questions(questions) is questions

    def test_rejects_more_than_six_questions(self):
        questions = {
            f"q{i}": {"type": "noul", "instructions": "x"} for i in range(7)
        }
        with pytest.raises(ValueError, match="6 questions"):
            validate_questions(questions)

    def test_rejects_bad_type(self):
        with pytest.raises(ValueError, match="type"):
            validate_questions({"q": {"type": "essay", "instructions": "x"}})

    def test_rejects_choice_without_options(self):
        with pytest.raises(ValueError, match="options"):
            validate_questions({"q": {"type": "choice", "instructions": "x"}})

    def test_rejects_too_many_options(self):
        with pytest.raises(ValueError, match="249"):
            validate_questions({
                "q": {"type": "choice", "instructions": "x", "options": [str(i) for i in range(250)]}
            })

    def test_rejects_missing_instructions(self):
        with pytest.raises(ValueError, match="instructions"):
            validate_questions({"q": {"type": "noul"}})

    def test_exactly_249_options_ok(self):
        questions = {
            "q": {"type": "choice", "instructions": "x", "options": [str(i) for i in range(249)]}
        }
        validate_questions(questions)  # must not raise


def decider_handler(payloads: list):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/decide":
            body = json.loads(request.content)
            payloads.append(body)
            return httpx.Response(200, json={
                "answers": {
                    name: {
                        "value": "find_convoy_routes",
                        "probability": 0.91,
                        "confidence": 0.88,
                        "distribution": [{"value": "find_convoy_routes", "probability": 0.91}],
                    }
                    for name in body["questions"]
                },
                "state_tokens": 12,
                "latency_ms": 402,
            })
        return httpx.Response(404)
    return handler


class TestDeciderClient:
    async def test_noul_confidence_none_flows_through(self):
        # Wire contract (model_host/decider_runtime.py): "autojev noul
        # answers carry no confidence". The client must preserve None —
        # the old None→0.0 coercion made every noul-bearing question set
        # permanently low_confidence below the floor (fixed 2026-10-19).
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "answers": {
                    name: (
                        {"value": 0.46, "probability": 0.46,
                         "confidence": None,
                         "distribution": {"false": 0.54, "true": 0.46}}
                        if body["questions"][name]["type"] == "noul"
                        else {"value": "primary", "probability": 0.67,
                              "confidence": 0.5033,
                              "distribution": {"primary": 0.67}}
                    )
                    for name in body["questions"]
                },
                "state_tokens": 30,
                "latency_ms": 120,
            })

        client = DeciderClient(
            base_url="http://127.0.0.1:8083",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        answers = await client.decide(
            state="TRIGGER: pre_movement",
            questions={
                "route_strategy": {
                    "type": "choice", "instructions": "pick",
                    "options": ["primary", "alternate"],
                },
                "urgency": {"type": "noul", "instructions": "how urgent"},
            },
        )
        assert answers["route_strategy"].confidence == 0.5033
        assert answers["urgency"].confidence is None  # NOT 0.0

    async def test_decide_round_trip(self):
        payloads: list = []
        client = DeciderClient(
            base_url="http://127.0.0.1:8083",
            client=httpx.AsyncClient(transport=httpx.MockTransport(decider_handler(payloads))),
        )
        answers = await client.decide(
            state="TRIGGER: operator_chat\nrequest",
            questions={
                "next_tool": {
                    "type": "choice",
                    "instructions": "pick",
                    "options": ["find_convoy_routes", "final_answer"],
                },
            },
        )
        assert answers["next_tool"].value == "find_convoy_routes"
        assert answers["next_tool"].confidence == 0.88
        assert answers["next_tool"].distribution
        # payload shape honored the C1 contract
        sent = payloads[0]
        assert sent["request_id"]
        assert set(sent["questions"]) == {"next_tool"}

    async def test_decide_429_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "5"})
        client = DeciderClient(
            base_url="http://127.0.0.1:8083",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(DeciderUnavailable, match="429"):
            await client.decide("state", {"q": {"type": "noul", "instructions": "x"}})

    async def test_decide_missing_answer_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"answers": {}})
        client = DeciderClient(
            base_url="http://127.0.0.1:8083",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(DeciderUnavailable, match="missing answer"):
            await client.decide("state", {"q": {"type": "noul", "instructions": "x"}})

    async def test_transport_error_raises_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")
        client = DeciderClient(
            base_url="http://127.0.0.1:8083",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(DeciderUnavailable):
            await client.decide("state", {"q": {"type": "noul", "instructions": "x"}})

    async def test_health_populates_p95(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/decider/health"
            return httpx.Response(200, json={
                "loaded": True,
                "resident_rss_gib": 48.6,
                "p95_decide_latency_ms": 1840.0,
                "queue_depth": 2,
            })
        client = DeciderClient(
            base_url="http://127.0.0.1:8083",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        health = await client.health()
        assert health["loaded"] is True
        assert client.p95_decide_latency_ms == 1840.0

    async def test_health_failure_returns_empty(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")
        client = DeciderClient(
            base_url="http://127.0.0.1:8083",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        assert await client.health() == {}


# ---------------------------------------------------------------------------
# Llama bridge
# ---------------------------------------------------------------------------


def completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}}]
    })


class TestLlamaBridge:
    async def test_json_schema_request_shape(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return completion('{"overall_status": "green"}')

        bridge = LlamaBridge(
            base_url="http://127.0.0.1:8084",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        schema = load_schema("traffic_analyst")
        out = await bridge.generate(
            system_prompt="sys",
            user_prompt="do",
            json_mode=True,
            schema=schema,
            schema_name="traffic_analyst",
        )
        assert out == '{"overall_status": "green"}'
        request = seen[0]
        assert request["model"] == "aditi-ps18-v7h"
        assert request["response_format"]["type"] == "json_schema"
        assert request["response_format"]["json_schema"]["schema"] == schema
        assert request["messages"][0]["role"] == "system"
        assert request["messages"][-1]["role"] == "user"

    async def test_suppress_thinking_is_ignored(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            # The bridge must never inject the retired /no_think token.
            assert "/no_think" not in json.dumps(body)
            return completion("{}")

        bridge = LlamaBridge(
            base_url="http://127.0.0.1:8084",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        await bridge.generate("s", "u", json_mode=False, suppress_thinking=True)
        await bridge.generate("s", "u", json_mode=False, suppress_thinking=False)
        # accepted and ignored; no exception, prompt unchanged

    async def test_falls_to_json_object_then_prompt_schema(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            rf = body.get("response_format")
            if rf and rf.get("type") == "json_schema":
                calls.append("json_schema")
                return httpx.Response(400, text="response_format json_schema not supported")
            if rf and rf.get("type") == "json_object":
                calls.append("json_object")
                return httpx.Response(400, text="response_format rejected")
            calls.append("prompt")
            assert "JSON Schema" in body["messages"][-1]["content"]
            return completion('{"ok": 1}')

        bridge = LlamaBridge(
            base_url="http://127.0.0.1:8084",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        out = await bridge.generate(
            "sys", "u", json_mode=True,
            schema=load_schema("tool_arguments"), schema_name="tool_arguments",
        )
        assert calls == ["json_schema", "json_object", "prompt"]
        assert out == '{"ok": 1}'

    async def test_down_server_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no llama")
        bridge = LlamaBridge(
            base_url="http://127.0.0.1:8084",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(LlamaUnavailable):
            await bridge.generate("s", "u", json_mode=False)

    async def test_health_check(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/health"
            return httpx.Response(200, json={"status": "ok"})
        bridge = LlamaBridge(
            base_url="http://127.0.0.1:8084",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        assert await bridge.check_health() is True


# ---------------------------------------------------------------------------
# Parsing + schemas
# ---------------------------------------------------------------------------


class TestParsing:
    def test_plain_json(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
        assert extract_json('```\n{"a": 2}\n```') == {"a": 2}

    def test_json_in_prose(self):
        assert extract_json('Sure! Here: {"a": 3} hope that helps') == {"a": 3}

    def test_garbage_returns_none(self):
        assert extract_json("no json at all") is None
        assert extract_json("") is None

    def test_truncate_is_utf8_safe(self):
        text = "é" * 5000
        out = truncate_for_context(text, 100)
        assert len(out.encode("utf-8")) <= 100 + 20
        assert out.endswith("[truncated]")


class TestSchemas:
    def test_all_known_schemas_load(self):
        for name in known_schemas():
            schema = load_schema(name)
            assert schema["type"] == "object"
            assert "required" in schema

    def test_unknown_schema_raises(self):
        import pytest

        with pytest.raises(KeyError):
            load_schema("nope")
