"""Shared fixtures for the convoy-brain test suite.

Pure python: httpx's MockTransport fakes the model tiers; fakes replace the
Valkey context store and MCP executor. No torch/onnx, no network, no sockets.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from convoy_brain.memory.convoy_context import ConvoyContext  # noqa: E402
from convoy_brain.mcp.server import ToolExecutionError  # noqa: E402


class FakeContextStore:
    """In-memory ConvoyContextStore drop-in (no Valkey)."""

    def __init__(self) -> None:
        self.contexts: dict[str, ConvoyContext] = {}

    async def get(self, movement_id: str) -> ConvoyContext | None:
        return self.contexts.get(movement_id)

    async def put(self, ctx: ConvoyContext) -> None:
        self.contexts[ctx.movement_id] = ctx

    async def append_decision(self, movement_id: str, decision: dict) -> None:
        ctx = self.contexts.get(movement_id)
        if ctx is None:
            ctx = ConvoyContext(movement_id=movement_id, vvip_class="unknown")
            self.contexts[movement_id] = ctx
        ctx.decisions_log.append(decision)

    async def update_protocol_state(self, movement_id: str, update: dict):
        ctx = self.contexts.get(movement_id)
        if ctx is None:
            return None
        for key, value in update.items():
            if key in ctx.protocol_state and isinstance(ctx.protocol_state[key], dict) and isinstance(value, dict):
                ctx.protocol_state[key].update(value)
            else:
                ctx.protocol_state[key] = value
        return ctx

    async def close(self) -> None:
        pass


class FakeExecutor:
    """Scriptable MCP tool executor."""

    def __init__(self, routes: dict[str, object] | None = None) -> None:
        # routes: tool_name -> dict result, or Exception subclass/instance
        self.routes = routes or {}
        self.calls: list[tuple[str, dict]] = []

    async def dispatch(self, tool_name: str, arguments: dict):
        self.calls.append((tool_name, arguments))
        handler = self.routes.get(tool_name)
        if handler is None:
            raise ToolExecutionError("Unknown tool: " + tool_name)
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(arguments)
        return handler

    async def execute(self, tool_name: str, arguments: dict):
        return await self.dispatch(tool_name, arguments)

    def get_prompt(self, name: str, arguments: dict):
        from convoy_brain.mcp.server import ToolExecutor

        real = ToolExecutor.__new__(ToolExecutor)
        return real.get_prompt(name, arguments)

    async def close(self) -> None:
        pass


@pytest.fixture
def fake_store() -> FakeContextStore:
    return FakeContextStore()


@pytest.fixture
def fake_executor() -> FakeExecutor:
    return FakeExecutor()


def json_transport(handler) -> httpx.AsyncClient:
    """Build an httpx client whose transport is handler(request) -> Response."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
