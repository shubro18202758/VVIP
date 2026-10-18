"""SafeHttpClient choke-point tests: internal constants pass, everything
else goes through validate_external_url BEFORE any request is attempted."""

from __future__ import annotations

import httpx
import pytest

from event_emulator.safe_http import UnsafeUrlError
from event_emulator.twin_seam import REPLAY_HEADER, SafeHttpClient

INTERNAL = "http://localhost:8081"


def _transport(seen: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


class TestChokePoint:
    async def test_internal_constant_reaches_transport(self):
        seen: list = []
        client = SafeHttpClient((INTERNAL,), transport=_transport(seen))
        resp = await client.post(f"{INTERNAL}/api/v1/simulate/twin", json_body={"a": 1})
        assert resp.status_code == 200
        assert len(seen) == 1
        await client.close()

    async def test_external_loopback_rejected_before_request(self):
        seen: list = []
        client = SafeHttpClient((INTERNAL,), transport=_transport(seen))
        with pytest.raises(UnsafeUrlError, match="loopback"):
            await client.post("http://127.0.0.1:8081/api", json_body={})
        assert seen == []  # nothing was sent
        with pytest.raises(UnsafeUrlError):
            await client.get("http://localhost:9999/x")  # not an internal constant
        assert seen == []
        await client.close()

    async def test_external_private_rejected_before_request(self):
        seen: list = []
        client = SafeHttpClient((INTERNAL,), transport=_transport(seen))
        with pytest.raises(UnsafeUrlError, match="private"):
            await client.post("http://10.0.0.7/api", json_body={})
        assert seen == []
        await client.close()

    async def test_replay_header_is_the_single_tell(self):
        seen: list = []
        client = SafeHttpClient((INTERNAL,), transport=_transport(seen))
        await client.post(f"{INTERNAL}/x", json_body={}, replay_run_id=7)
        assert seen[0].headers.get(REPLAY_HEADER) == "7"
        await client.post(f"{INTERNAL}/x", json_body={})  # live: NO header
        assert seen[1].headers.get(REPLAY_HEADER) is None
        await client.close()
