"""Adaptive-parameter client tests — C4 fail-closed semantics."""

from __future__ import annotations

import httpx
import pytest

from convoy_brain.adaptive_params import AdaptiveParamsClient
from convoy_brain.config import AdaptiveParams


def client_with(handler) -> AdaptiveParamsClient:
    return AdaptiveParamsClient(
        base_url="http://127.0.0.1:8081",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class TestFailClosed:
    async def test_transport_error_returns_baseline(self):
        def handler(request):
            raise httpx.ConnectError("no oracle")
        params = await client_with(handler).fetch("m1", "Z")
        assert params.source == "baseline"
        assert params.shadow_eval == "unavailable"
        assert params.advance_closure_sec == 120
        assert params.scoring_weights == {"time": 0.3, "disruption": 0.4, "security": 0.2, "complexity": 0.1}

    async def test_http_error_returns_baseline(self):
        params = await client_with(
            lambda request: httpx.Response(503)
        ).fetch("m1", "Z")
        assert params.source == "baseline"

    async def test_malformed_payload_returns_baseline(self):
        params = await client_with(
            lambda request: httpx.Response(200, text="not json")
        ).fetch("m1", "Z")
        assert params.source == "baseline"

    async def test_endpoint_absent_returns_baseline(self):
        # adaptive router not mounted yet -> 404
        params = await client_with(
            lambda request: httpx.Response(404)
        ).fetch("m1", "Z")
        assert params.source == "baseline"


class TestShadowEvalGate:
    async def test_deviations_require_passed_shadow_eval(self):
        def handler(request):
            return httpx.Response(200, json={
                "situation_class": "congestion_spike",
                "params": {
                    "advance_closure_sec": 60,
                    "congestion_spike_threshold": 0.6,
                    "gps_poll_interval_sec": 0.5,
                    "scoring_weights": {"time": 0.1, "disruption": 0.7, "security": 0.1, "complexity": 0.1},
                },
                "source": "retrieved",
                "shadow_eval": "unavailable",  # twin never validated
                "evidence": {"reason": "twin is stub"},
            })
        params = await client_with(handler).fetch("m1", "Z")
        # fail-closed: baseline values, situation class still recorded
        assert params.source == "retrieved"
        assert params.advance_closure_sec == 120
        assert params.congestion_spike_threshold == 0.8

    async def test_passed_shadow_eval_unlocks_params(self):
        def handler(request):
            return httpx.Response(200, json={
                "situation_class": "congestion_spike",
                "params": {
                    "advance_closure_sec": 150,
                    "congestion_spike_threshold": 0.65,
                    "gps_poll_interval_sec": 2.0,
                    "scoring_weights": {"time": 0.1, "disruption": 0.7, "security": 0.1, "complexity": 0.1},
                },
                "source": "learned",
                "shadow_eval": "passed",
                "evidence": {"twin_score": 0.92},
            })
        params = await client_with(handler).fetch("m1", "Z")
        assert params.source == "learned"
        assert params.advance_closure_sec == 150
        assert params.congestion_spike_threshold == 0.65
        assert params.gps_poll_interval_sec == 2.0
        assert params.scoring_weights["disruption"] == 0.7
        assert params.evidence == {"twin_score": 0.92}

    async def test_gps_poll_has_protocol_floor_of_one_second(self):
        def handler(request):
            return httpx.Response(200, json={
                "params": {"gps_poll_interval_sec": 0.2},
                "source": "learned", "shadow_eval": "passed",
            })
        params = await client_with(handler).fetch("m1", "Z")
        assert params.gps_poll_interval_sec == 1.0

    async def test_bad_numeric_values_fail_closed(self):
        def handler(request):
            return httpx.Response(200, json={
                "params": {"advance_closure_sec": "soon"},
                "source": "learned", "shadow_eval": "passed",
            })
        params = await client_with(handler).fetch("m1", "Z")
        assert params.advance_closure_sec == 120


class TestRequestShape:
    async def test_query_params_sent(self):
        seen: dict = {}

        def handler(request):
            seen["params"] = str(request.url.params)
            return httpx.Response(200, json={
                "params": {}, "source": "baseline", "shadow_eval": "unavailable",
            })
        await client_with(handler).fetch("mv-9", "Z+")
        assert "movement_id=mv-9" in seen["params"]
        assert "vvip_class=Z%2B" in seen["params"]

    async def test_platform_url_policy_enforced(self):
        with pytest.raises(Exception):
            AdaptiveParamsClient(base_url="http://example.com:8081")
