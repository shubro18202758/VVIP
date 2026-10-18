"""Twin seam + the guarded outbound HTTP client.

Every outbound request funnels through :class:`SafeHttpClient`:

* URLs that are one of the configured FIXED INTERNAL ENDPOINTS (config
  constants — the traffic-oracle twin endpoint) pass the internal-allowlist
  assertion (http/https, loopback / host.docker.internal host);
* any other URL — i.e. anything originating from request bodies, CLI flags,
  tape files, or env-provided values — MUST pass
  :func:`event_emulator.safe_http.validate_external_url`, which rejects
  non-http(s) schemes, embedded credentials, and every loopback / private /
  reserved / link-local / multicast / unspecified address BEFORE the request.

The twin POST carries the use-case causes verbatim: route, departure,
closure policy, horizon, replications, seed, plus the additive cause fields
(weather / event_overrides with ambulance_rate_multiplier / forced_events
incl. emergency_vehicle) in the request body.  Today the server's
TwinSimRequest ignores unknown fields (pydantic default), so the scalar
fields drive the run and the additive cause fields ride along until the
server widens its request model — the run log records the delivery level
honestly.

Replay marker: live injections carry NO emulator header (indistinguishable
from a real feed at the API boundary); replays carry
``X-Emulation-Replay: <run_id>`` — the single tell, by contract.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from event_emulator.safe_http import (
    UnsafeUrlError,
    assert_internal_endpoint,
    validate_external_url,
)

logger = structlog.get_logger(__name__)

__all__ = ["SafeHttpClient", "TwinSeam", "REPLAY_HEADER"]

REPLAY_HEADER = "X-Emulation-Replay"


class SafeHttpClient:
    """httpx wrapper enforcing the outbound URL policy at one choke point."""

    def __init__(
        self, internal_endpoints: tuple[str, ...], transport: Any = None
    ) -> None:
        self._internal = tuple(internal_endpoints)
        client_kwargs: dict[str, Any] = {"timeout": 30.0}
        if transport is not None:  # test injection
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    async def close(self) -> None:
        await self._client.aclose()

    def _guard(self, url: str) -> httpx.URL:
        for base in self._internal:
            if url == base or url.startswith(base.rstrip("/") + "/"):
                assert_internal_endpoint(base)
                return httpx.URL(url)
        validate_external_url(url)  # raises UnsafeUrlError on policy failure
        return httpx.URL(url)

    async def post(
        self,
        url: str,
        *,
        json_body: dict[str, Any],
        replay_run_id: int | None = None,
    ) -> httpx.Response:
        target = self._guard(url)
        headers: dict[str, str] = {}
        if replay_run_id is not None:
            headers[REPLAY_HEADER] = str(replay_run_id)
        return await self._client.post(str(target), json=json_body, headers=headers)

    async def get(self, url: str, *, replay_run_id: int | None = None) -> httpx.Response:
        target = self._guard(url)
        headers: dict[str, str] = {}
        if replay_run_id is not None:
            headers[REPLAY_HEADER] = str(replay_run_id)
        return await self._client.get(str(target), headers=headers)


class TwinSeam:
    """Posts the deterministic cause set to the real simulation twin."""

    def __init__(self, client: SafeHttpClient, oracle_base_url: str) -> None:
        self._client = client
        self._base = oracle_base_url.rstrip("/")

    def build_body(
        self,
        *,
        route_segment_ids: list[int],
        departure_time: str,
        closure_policy: dict,
        horizon_min: int,
        n_replications: int,
        seed: int,
        weather: dict,
        event_overrides: dict,
        forced_events: list[dict],
        scenario_id: str | None = None,
    ) -> dict[str, Any]:
        """TwinSimRequest fields + additive cause fields riding in-body.

        ``scenario_id`` (additive, flagship showcase): when the use case is
        also registered in the server's own scenario library, the server
        resolves the full cause set (weather / event_config / scheduled
        events / convoy speed) from its copy of the same config.  Contract
        C2 governs: the explicit scalar fields above are posted too and
        always win, so the two sources can never disagree silently.
        """
        body: dict[str, Any] = {
            # TwinSimRequest (simulation/router.py:72-79) — accepted today
            "route_segment_ids": route_segment_ids,
            "departure_time": departure_time,
            "closure_policy": closure_policy,
            "horizon_min": horizon_min,
            "n_replications": n_replications,
            "seed": seed,
            # additive cause fields (consumed once TwinSimRequest widens;
            # pydantic ignores them until then — delivery level is logged)
            "weather": weather,
            "event_overrides": event_overrides,
            "forced_events": forced_events,
        }
        if scenario_id is not None:
            body["scenario_id"] = scenario_id
        return body

    async def post_twin(
        self, body: dict[str, Any], replay_run_id: int | None = None
    ) -> httpx.Response:
        return await self._client.post(
            f"{self._base}/api/v1/simulate/twin",
            json_body=body,
            replay_run_id=replay_run_id,
        )

    async def probe(self) -> bool:
        """Is the oracle reachable? (degradation signal, never fatal here)"""
        try:
            resp = await self._client.get(f"{self._base}/health")
            return resp.status_code == 200
        except (httpx.HTTPError, UnsafeUrlError) as exc:
            logger.warning("twin_seam.probe_failed", error=str(exc))
            return False
