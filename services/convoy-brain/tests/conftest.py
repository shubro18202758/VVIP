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


# ---------------------------------------------------------------------------
# Synthetic road network (oracle doubles with real geometry)
# ---------------------------------------------------------------------------


def seg(sid, a, b, *, lanes=6, road_class="trunk", name=None, oneway=False,
        speed=60.0, length_m=1000.0):
    """One road segment row as the oracle's spatial endpoints return it."""
    return {
        "segment_id": sid,
        "road_name": name or f"Road {sid}",
        "road_class": road_class,
        "lanes": lanes,
        "speed_limit_kmh": speed,
        "oneway": oneway,
        "length_m": length_m,
        "geometry": {"type": "LineString", "coordinates": [list(a), list(b)]},
    }


def bbox_handler(segments: dict, calls: list | None = None):
    """query_segments_in_bbox double: rows whose geometry bbox overlaps the
    query box, ORDER BY segment_id — exactly the oracle's ordering."""
    def handler(args):
        if calls is not None:
            calls.append(dict(args))
        rows = []
        for sid in sorted(segments):
            coords = segments[sid]["geometry"]["coordinates"]
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            if (min(lons) <= args["max_lon"] and max(lons) >= args["min_lon"]
                    and min(lats) <= args["max_lat"] and max(lats) >= args["min_lat"]):
                rows.append(segments[sid])
        return {"segments": rows}
    return handler


def details_handler(segments: dict):
    """query_segment_details double (geometry kept, like the oracle)."""
    def handler(args):
        sid = args["segment_id"]
        if sid not in segments:
            raise ToolExecutionError("oracle HTTP 404: Segment not found", status_code=404,
                                     retryable=False)
        return dict(segments[sid])
    return handler


#: A small corridor: origin O=(72.55, 23.02) -> destination D=(72.58, 23.02).
#: 1001-1002-1003 run east along lat 23.02 (1003 has only 4 lanes); 1004 is
#: a 6-lane bypass north of 1003 ending at D; 1005 touches O and leads away.
NETWORK = {
    1001: seg(1001, (72.550, 23.020), (72.560, 23.020), lanes=6, name="West Arterial"),
    1002: seg(1002, (72.560, 23.020), (72.570, 23.020), lanes=6, name="Central Arterial"),
    1003: seg(1003, (72.570, 23.020), (72.580, 23.020), lanes=4, road_class="primary",
              name="East Narrows"),
    1004: seg(1004, (72.570, 23.020), (72.580, 23.020 + 0.0001), lanes=6,
              road_class="motorway", name="Bypass"),
    1005: seg(1005, (72.550, 23.020), (72.540, 23.030), lanes=6, name="Old Road"),
}
ORIGIN = (72.550, 23.020)
DESTINATION = (72.580, 23.020)
