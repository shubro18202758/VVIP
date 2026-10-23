"""Atomic convoy-context writes — field-scoped updates under WATCH/MULTI.

The audit repro this guards: the WebSocket handler read the context once at
connect and wrote that snapshot back on every position.update, reverting the
status (approved -> planning), the route, the threat level and wiping the
decisions_log. Every field update now re-reads the document inside an
optimistic transaction and retries when another writer committed in between.

``FakeValkey`` implements the WATCH/MULTI/EXEC semantics the store relies on
(a watched key changed by anyone else fails EXEC with WatchError). An
optional class runs the same races against a real Valkey when
VVIP_TEST_VALKEY_URL points at a scratch instance.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import pytest
from valkey.exceptions import WatchError

from convoy_brain.memory.convoy_context import (
    CONTEXT_KEY_PREFIX,
    CONTEXT_TTL_SEC,
    MAX_DECISIONS_LOG,
    MUTATE_MAX_ATTEMPTS,
    POSITION_KEY_PREFIX,
    POSITION_TTL_SEC,
    ContextWriteConflictError,
    ConvoyContext,
    ConvoyContextStore,
    cap_decisions_log,
)


class FakeValkey:
    """In-memory async Valkey with versioned keys and optimistic transactions."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}
        self.version: dict[str, int] = {}
        self.on_watch = None  # hook: async fn(key) run right after WATCH (race injection)
        self.exec_calls = 0

    def _bump(self, key: str) -> None:
        self.version[key] = self.version.get(key, 0) + 1

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.data[key] = value
        self.ttl[key] = ex
        self._bump(key)
        return True

    async def setex(self, key: str, ttl: int, value: str):
        return await self.set(key, value, ex=ttl)

    async def scan_iter(self, match: str = "*", count: int = 100):
        prefix = match.rstrip("*")
        for key in list(self.data):
            if key.startswith(prefix):
                yield key

    async def aclose(self) -> None:
        pass

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, server: FakeValkey) -> None:
        self.server = server
        self.watched: dict[str, int] = {}
        self.buffer: list[tuple[str, str, int | None]] = []
        self.in_multi = False

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.reset()

    async def reset(self) -> None:
        self.watched.clear()
        self.buffer.clear()
        self.in_multi = False

    async def watch(self, *keys: str) -> None:
        for key in keys:
            self.watched[key] = self.server.version.get(key, 0)
        if self.server.on_watch is not None:
            await self.server.on_watch(keys[0])

    async def get(self, key: str):
        return self.server.data.get(key)

    def multi(self) -> None:
        self.in_multi = True

    def set(self, key: str, value: str, ex: int | None = None) -> FakePipeline:
        self.buffer.append((key, value, ex))
        return self

    async def execute(self):
        self.server.exec_calls += 1
        try:
            for key, version in self.watched.items():
                if self.server.version.get(key, 0) != version:
                    raise WatchError("Watched variable changed.")
            for key, value, ex in self.buffer:
                await self.server.set(key, value, ex=ex)
            return [True] * len(self.buffer)
        finally:
            await self.reset()


def _doc(server: FakeValkey, movement_id: str) -> dict:
    return json.loads(server.data[f"{CONTEXT_KEY_PREFIX}{movement_id}"])


def _seed(status="approved") -> ConvoyContext:
    ctx = ConvoyContext(movement_id="mv-1", vvip_class="Z", status=status,
                        selected_route_id="R-7")
    ctx.protocol_state["threat_level"] = "high"
    ctx.decisions_log.append({"phase": "pre_movement", "primary_segment_ids": [1001, 1015],
                              "timestamp": 1.0})
    return ctx


@pytest.fixture
def server() -> FakeValkey:
    return FakeValkey()


@pytest.fixture
def store(server) -> ConvoyContextStore:
    return ConvoyContextStore("redis://unused", client=server)


class TestFieldScopedWrites:
    async def test_position_update_preserves_every_other_field(self, store, server):
        await store.put(_seed())
        ctx, applied = await store.update_position(
            "mv-1", 72.57, 23.02, speed_kmh=41.5, heading=90.0,
            t="2026-10-22T10:00:00.000Z", source="emulated_gps",
        )
        assert applied is True
        doc = _doc(server, "mv-1")
        assert doc["status"] == "approved"
        assert doc["selected_route_id"] == "R-7"
        assert doc["protocol_state"]["threat_level"] == "high"
        assert len(doc["decisions_log"]) == 1
        assert doc["convoy_position"] == [72.57, 23.02]
        assert doc["convoy_speed_kmh"] == 41.5
        assert doc["convoy_heading"] == 90.0
        assert doc["position_source"] == "emulated_gps"
        assert ctx.position_t == "2026-10-22T10:00:00.000Z"

    async def test_unknown_movement_is_not_created_by_a_position(self, store, server):
        ctx, applied = await store.update_position("nope", 72.5, 23.0, t="2026-10-22T10:00:00Z")
        assert ctx is None and applied is False
        assert not server.data

    @pytest.mark.parametrize("status", ["completed", "cancelled"])
    async def test_a_closed_movement_takes_no_more_fixes(self, store, server, status):
        await store.put(_seed(status=status))
        before = server.data["convoy:context:mv-1"]
        ctx, applied = await store.update_position("mv-1", 72.57, 23.02,
                                                   t="2026-10-22T10:00:00Z")
        assert applied is False and ctx.status == status
        assert server.data["convoy:context:mv-1"] == before  # nothing written

    async def test_an_older_fix_never_moves_the_convoy_back(self, store, server):
        await store.put(_seed())
        await store.update_position("mv-1", 72.58, 23.02, t="2026-10-22T10:00:05Z")
        _ctx, applied = await store.update_position("mv-1", 72.55, 23.02,
                                                    t="2026-10-22T10:00:01Z")
        assert applied is False
        assert _doc(server, "mv-1")["convoy_position"] == [72.58, 23.02]

    async def test_concurrent_writer_between_watch_and_exec_is_retried_not_lost(
        self, store, server
    ):
        await store.put(_seed())
        injected = {"done": False}

        async def race(key: str) -> None:
            # Another writer commits an audit row right after our WATCH.
            if not injected["done"]:
                injected["done"] = True
                doc = json.loads(server.data[key])
                doc["decisions_log"].append({"phase": "decider_checkpoint", "timestamp": 2.0})
                await server.set(key, json.dumps(doc), ex=CONTEXT_TTL_SEC)

        server.on_watch = race
        await store.update_position("mv-1", 72.57, 23.02, t="2026-10-22T10:00:00Z")
        doc = _doc(server, "mv-1")
        # Both writes survived: the retry re-read the document.
        assert [r["phase"] for r in doc["decisions_log"]] == ["pre_movement", "decider_checkpoint"]
        assert doc["convoy_position"] == [72.57, 23.02]
        assert server.exec_calls == 2  # first EXEC lost the race, second won

    async def test_interleaved_writers_keep_all_changes(self, store, server):
        await store.put(_seed())

        async def positions():
            for i in range(30):
                await store.update_position(
                    "mv-1", 72.55 + i * 0.001, 23.02,
                    t=f"2026-10-22T10:00:{i:02d}.000Z",
                )
                await asyncio.sleep(0)

        async def decisions():
            for i in range(30):
                await store.append_decision("mv-1", {"phase": "live_escort", "n": i})
                await asyncio.sleep(0)

        async def protocol():
            for _ in range(10):
                await store.update_protocol_state("mv-1", {"transit_status": {"ecm_active": True}})
                await asyncio.sleep(0)

        await asyncio.gather(positions(), decisions(), protocol())
        doc = _doc(server, "mv-1")
        assert [r.get("n") for r in doc["decisions_log"] if "n" in r] == list(range(30))
        assert doc["convoy_position"] == [pytest.approx(72.55 + 29 * 0.001), 23.02]
        assert doc["protocol_state"]["transit_status"]["ecm_active"] is True
        assert doc["status"] == "approved"

    async def test_a_hopeless_race_is_reported_not_silently_dropped(self, store, server):
        await store.put(_seed())

        async def always(key: str) -> None:
            server._bump(key)

        server.on_watch = always
        with pytest.raises(ContextWriteConflictError):
            await store.append_decision("mv-1", {"phase": "x"})
        assert server.exec_calls == MUTATE_MAX_ATTEMPTS

    async def test_update_fields_rejects_unknown_names(self, store):
        await store.put(_seed())
        with pytest.raises(ValueError):
            await store.update_fields("mv-1", not_a_field=1)
        ctx = await store.update_fields("mv-1", status="active")
        assert ctx.status == "active"

    async def test_append_decision_on_missing_context_creates_minimal(self, store, server):
        await store.append_decision("fresh", {"phase": "decider_checkpoint", "timestamp": 1.0})
        doc = _doc(server, "fresh")
        assert doc["vvip_class"] == "unknown"
        assert doc["decisions_log"][0]["phase"] == "decider_checkpoint"


class TestRetention:
    async def test_every_write_sets_the_seven_day_ttl(self, store, server):
        key = f"{CONTEXT_KEY_PREFIX}mv-1"
        await store.put(_seed(status="planning"))  # not completed: still a TTL
        assert server.ttl[key] == CONTEXT_TTL_SEC == 7 * 86400
        for write in (
            store.append_decision("mv-1", {"phase": "x"}),
            store.update_protocol_state("mv-1", {"threat_level": "elevated"}),
            store.update_position("mv-1", 72.5, 23.0, t="2026-10-22T10:00:00Z"),
        ):
            server.ttl[key] = None
            await write
            assert server.ttl[key] == CONTEXT_TTL_SEC

    async def test_decisions_log_is_capped_and_keeps_the_plan_anchor(self, store, server):
        ctx = _seed()
        ctx.decisions_log.append({"phase": "city_event", "event_id": "e-1", "timestamp": 1.5})
        await store.put(ctx)
        for i in range(MAX_DECISIONS_LOG + 40):
            await store.append_decision("mv-1", {"phase": "live_escort", "n": i})
        log = _doc(server, "mv-1")["decisions_log"]
        assert len(log) == MAX_DECISIONS_LOG
        phases = [r["phase"] for r in log]
        assert "pre_movement" in phases and "city_event" in phases
        assert log[-1]["n"] == MAX_DECISIONS_LOG + 39  # newest kept

    def test_cap_keeps_order_and_size(self):
        rows = [{"phase": "pre_movement", "i": 0}] + [{"phase": "x", "i": i} for i in range(1, 20)]
        capped = cap_decisions_log(rows, limit=5)
        assert len(capped) == 5
        assert capped[0]["phase"] == "pre_movement"
        assert [r["i"] for r in capped] == [0, 16, 17, 18, 19]

    async def test_position_frame_side_key_expires_in_30s(self, store, server):
        await store.write_position_frame("mv-1", {"movement_id": "mv-1", "lon": 72.5, "lat": 23.0})
        key = f"{POSITION_KEY_PREFIX}mv-1"
        assert server.ttl[key] == POSITION_TTL_SEC == 30
        assert (await store.read_position_frame("mv-1"))["lon"] == 72.5


class TestWebSocketHandler:
    """The audit repro, through the real FastAPI WebSocket route."""

    def test_ws_position_update_never_writes_back_a_stale_snapshot(self, monkeypatch):
        from starlette.testclient import TestClient

        from convoy_brain import __main__ as brain_main

        server = FakeValkey()
        monkeypatch.setenv("MODEL_HOST_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("LLAMA_BASE_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("TRAFFIC_ORACLE_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("EMBEDDING_SERVICE_URL", "http://127.0.0.1:1")
        monkeypatch.setattr(
            brain_main, "ConvoyContextStore",
            lambda valkey_url: ConvoyContextStore(valkey_url, client=server),
        )
        app = brain_main.create_app()
        mid = str(uuid.uuid4())
        with TestClient(app) as client:
            store = app.state.context_store
            seed = ConvoyContext(movement_id=mid, vvip_class="Z", status="planning")
            asyncio.run(_put(store, seed))
            with client.websocket_connect(f"/ws/convoy/{mid}") as ws:
                hello = ws.receive_json()
                assert hello["payload"]["status"] == "planning"
                # Other writers move the movement on AFTER the socket's snapshot.
                asyncio.run(_advance(store, mid))
                ws.send_json({"type": "position.update", "payload": {
                    "lon": 72.571, "lat": 23.021, "speedKmh": 38.0, "headingDeg": 92.0,
                }})
                frame = ws.receive_json()
                assert frame["type"] == "convoy.position"
                ws.send_json({"type": "position.update", "payload": {"lon": 77.2, "lat": 28.6}})
                refused = ws.receive_json()
                assert refused["type"] == "error"
        doc = json.loads(server.data[f"{CONTEXT_KEY_PREFIX}{mid}"])
        assert doc["status"] == "approved"
        assert doc["selected_route_id"] == "R-7"
        assert doc["protocol_state"]["threat_level"] == "high"
        assert len(doc["decisions_log"]) == 1
        assert doc["convoy_position"] == [72.571, 23.021]
        frame_doc = json.loads(server.data[f"{POSITION_KEY_PREFIX}{mid}"])
        assert frame_doc["source"] == "gps" and frame_doc["movement_id"] == mid


async def _put(store, ctx):
    await store.put(ctx)


async def _advance(store, mid):
    await store.update_fields(mid, status="approved", selected_route_id="R-7")
    await store.update_protocol_state(mid, {"threat_level": "high"})
    await store.append_decision(mid, {"phase": "pre_movement", "timestamp": time.time()})


@pytest.mark.skipif(
    not os.environ.get("VVIP_TEST_VALKEY_URL"),
    reason="set VVIP_TEST_VALKEY_URL to a scratch Valkey to run the live WATCH/MULTI race",
)
class TestLiveValkey:
    async def test_real_watch_multi_keeps_every_concurrent_write(self):
        # Two stores = two clients with their own lock maps: the only thing
        # keeping their writes apart is WATCH/MULTI on the server.
        url = os.environ["VVIP_TEST_VALKEY_URL"]
        gps, audit = ConvoyContextStore(url), ConvoyContextStore(url)
        mid = f"wp12-atomic-{uuid.uuid4().hex[:8]}"
        try:
            await gps.put(ConvoyContext(movement_id=mid, vvip_class="Z", status="approved"))

            async def fix(i):
                await gps.update_position(mid, 72.55 + i * 1e-4, 23.02,
                                          t=f"2026-10-22T10:{i // 60:02d}:{i % 60:02d}.000Z")

            async def row(i):
                await audit.append_decision(mid, {"phase": "live_escort", "n": i})

            async def status():
                await audit.update_fields(mid, status="active")

            await asyncio.gather(*(fix(i) for i in range(60)), *(row(i) for i in range(60)),
                                 status())
            ctx = await gps.get(mid)
            assert sorted(r["n"] for r in ctx.decisions_log) == list(range(60))
            assert ctx.status == "active"
            assert ctx.convoy_position[0] == pytest.approx(72.55 + 59 * 1e-4)
            ttl = await gps._client.ttl(f"{CONTEXT_KEY_PREFIX}{mid}")
            assert 0 < ttl <= CONTEXT_TTL_SEC
        finally:
            await gps._client.delete(f"{CONTEXT_KEY_PREFIX}{mid}")
            await gps.close()
            await audit.close()
