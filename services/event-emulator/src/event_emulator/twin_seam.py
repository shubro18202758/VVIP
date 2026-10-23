"""Twin + oracle seams, and the guarded outbound HTTP client.

Every outbound request funnels through :class:`SafeHttpClient`:

* URLs that are one of the configured FIXED INTERNAL ENDPOINTS (config
  constants — the traffic-oracle base) pass the internal-allowlist
  assertion (http/https, loopback / host.docker.internal host);
* any other URL — i.e. anything originating from request bodies, CLI flags,
  tape files, or env-provided values — MUST pass
  :func:`event_emulator.safe_http.validate_external_url`, which rejects
  non-http(s) schemes, embedded credentials, and every loopback / private /
  reserved / link-local / multicast / unspecified address BEFORE the request.

The twin POST carries the use-case causes: route, departure, closure
policy, horizon, replications, seed — and ``scenario_id`` when the oracle's
own scenario library holds the use case, in which case the server resolves
the full cause set (weather, event config, scheduled events) from its copy.
A library miss (HTTP 404) is remembered and the same POST is re-sent
without it; the run ledger records which level was actually delivered.
The additive cause fields (weather / event_overrides / forced_events) ride
in-body for the record; the server's TwinSimRequest ignores them today.

The oracle seam is how the emulator drives the oracle's own write APIs
instead of writing behind its back: corridor-event lifecycle (contract
C-F), ANPR reads (contract C-E) and 108 / agency dispatch requests.

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

__all__ = [
    "SafeHttpClient",
    "TwinSeam",
    "OracleSeam",
    "REPLAY_HEADER",
    "DELIVERY_LEVELS",
]

REPLAY_HEADER = "X-Emulation-Replay"

#: What a twin POST actually delivered (recorded per run):
#:   twin_scenario_library  the oracle resolved the full cause set (weather,
#:                          event config, scheduled events) from its own
#:                          library copy of the use case
#:   twin_route_only        route/departure/policy/horizon/reps/seed ran;
#:                          the in-body cause fields were not consumed
#:   twin_rejected          non-200 answer (status recorded)
#:   twin_unreachable       transport error / policy refusal
DELIVERY_LEVELS = (
    "twin_scenario_library",
    "twin_route_only",
    "twin_rejected",
    "twin_unreachable",
)

#: Timeouts: the twin computes replications (seconds); the write seams
#: answer in milliseconds and must never stall a 5 s living-city tick.
_TWIN_TIMEOUT = httpx.Timeout(60.0, connect=3.0)
_WRITE_TIMEOUT = httpx.Timeout(8.0, connect=2.0)
_PROBE_TIMEOUT = httpx.Timeout(3.0, connect=1.0)


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
        timeout: httpx.Timeout | float | None = None,
    ) -> httpx.Response:
        target = self._guard(url)
        headers: dict[str, str] = {}
        if replay_run_id is not None:
            headers[REPLAY_HEADER] = str(replay_run_id)
        kwargs: dict[str, Any] = {"json": json_body, "headers": headers}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return await self._client.post(str(target), **kwargs)

    async def get(
        self,
        url: str,
        *,
        replay_run_id: int | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> httpx.Response:
        target = self._guard(url)
        headers: dict[str, str] = {}
        if replay_run_id is not None:
            headers[REPLAY_HEADER] = str(replay_run_id)
        kwargs: dict[str, Any] = {"headers": headers}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return await self._client.get(str(target), **kwargs)


def _twin_summary(resp: httpx.Response) -> dict[str, Any]:
    """Compact, outcome-free record of what the twin computed (ledger only;
    the emulator never acts on it)."""
    try:
        body = resp.json()
    except ValueError:
        return {}
    runs = [r for r in (body.get("runs") or []) if isinstance(r, dict)]

    def _mean(key: str) -> float | None:
        vals = [float(r[key]) for r in runs if isinstance(r.get(key), (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else None

    def _max(key: str) -> float | None:
        vals = [float(r[key]) for r in runs if isinstance(r.get(key), (int, float))]
        return round(max(vals), 2) if vals else None

    return {
        "replications": len(runs),
        "calibrated": body.get("calibrated"),
        "forecast_coupling": body.get("forecast_coupling"),
        "seed": body.get("seed"),
        "mean_total_disruption_vehicle_hours": _mean("total_disruption_vehicle_hours"),
        "max_queue_length_m": _max("max_queue_length_m"),
        "mean_convoy_transit_time_sec": _mean("convoy_transit_time_sec"),
        "mean_ambulance_delay_sec": _mean("ambulance_delay_sec"),
        "mean_recovery_sec": _mean("recovery_sec"),
    }


class TwinSeam:
    """Posts the deterministic cause set to the real simulation twin."""

    def __init__(self, client: SafeHttpClient, oracle_base_url: str) -> None:
        self._client = client
        self._base = oracle_base_url.rstrip("/")
        #: scenario ids the oracle's library answered 404 for (process-wide)
        self.library_misses: set[str] = set()
        #: why the last probe failed (/health names it), None once it answers
        self.last_error: str | None = None

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

        ``scenario_id`` (additive): when the use case is also registered in
        the server's own scenario library, the server resolves the full
        cause set from its copy of the same config.  Contract C2 governs:
        the explicit scalar fields above are posted too and always win, so
        the two sources can never disagree silently.
        """
        body: dict[str, Any] = {
            # TwinSimRequest (simulation/router.py) — consumed
            "route_segment_ids": route_segment_ids,
            "departure_time": departure_time,
            "closure_policy": closure_policy,
            "horizon_min": horizon_min,
            "n_replications": n_replications,
            "seed": seed,
            # additive cause fields (not consumed by today's TwinSimRequest;
            # the ledger's delivery_level says so)
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
            timeout=_TWIN_TIMEOUT,
        )

    async def deliver(
        self, body: dict[str, Any], replay_run_id: int | None = None
    ) -> dict[str, Any]:
        """POST the cause set; on a scenario-library miss re-POST without
        ``scenario_id``.  Returns the ledger entry (never raises)."""
        scenario_id = body.get("scenario_id")
        entry: dict[str, Any] = {"scenario_id_sent": None, "library_miss": False}
        send = dict(body)
        if scenario_id is not None and scenario_id in self.library_misses:
            send.pop("scenario_id", None)
            entry["library_miss"] = True
        try:
            resp = await self.post_twin(send, replay_run_id=replay_run_id)
            if resp.status_code == 404 and "scenario_id" in send:
                self.library_misses.add(str(scenario_id))
                entry["library_miss"] = True
                send.pop("scenario_id", None)
                resp = await self.post_twin(send, replay_run_id=replay_run_id)
        except (httpx.HTTPError, UnsafeUrlError) as exc:
            entry.update(result="failed", delivery_level="twin_unreachable",
                         reason=f"{type(exc).__name__}: {exc}")
            return entry
        entry["http_status"] = resp.status_code
        entry["scenario_id_sent"] = send.get("scenario_id")
        if resp.status_code == 200:
            entry["result"] = "delivered"
            entry["delivery_level"] = (
                "twin_scenario_library" if "scenario_id" in send else "twin_route_only"
            )
            entry["summary"] = _twin_summary(resp)
        else:
            entry["result"] = "failed"
            entry["delivery_level"] = "twin_rejected"
            entry["reason"] = resp.text[:200]
        return entry

    async def probe(self) -> bool:
        """Is the oracle reachable? (degradation signal, never fatal here)"""
        try:
            resp = await self._client.get(f"{self._base}/health", timeout=_PROBE_TIMEOUT)
        except (httpx.HTTPError, UnsafeUrlError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("twin_seam.probe_failed", error=self.last_error)
            return False
        if resp.status_code != 200:
            self.last_error = f"/health answered HTTP {resp.status_code}"
            return False
        self.last_error = None
        return True


class OracleSeam:
    """The oracle's write APIs the emulator drives (C-F, C-E, dispatch)."""

    def __init__(self, client: SafeHttpClient, oracle_base_url: str, officer: str) -> None:
        self._client = client
        self._base = oracle_base_url.rstrip("/")
        self.officer = officer

    async def event_status(
        self, event_id: str, to_status: str, note: str | None = None,
        replay_run_id: int | None = None,
    ) -> httpx.Response:
        """POST /api/v1/events/{id}/status (contract C-F)."""
        body: dict[str, Any] = {"to_status": to_status, "officer": self.officer}
        if note:
            body["note"] = note[:500]
        return await self._client.post(
            f"{self._base}/api/v1/events/{event_id}/status",
            json_body=body, replay_run_id=replay_run_id, timeout=_WRITE_TIMEOUT,
        )

    async def post_sighting(
        self, read: dict[str, Any], replay_run_id: int | None = None
    ) -> httpx.Response:
        """POST /api/v1/vehicles/sightings (contract C-E)."""
        return await self._client.post(
            f"{self._base}/api/v1/vehicles/sightings",
            json_body=read, replay_run_id=replay_run_id, timeout=_WRITE_TIMEOUT,
        )

    async def request_dispatch(
        self, event_id: str, body: dict[str, Any]
    ) -> httpx.Response:
        """POST /api/v1/events/{id}/dispatch — a 'requested' row that waits
        for a named duty officer's decision (never auto-approved)."""
        return await self._client.post(
            f"{self._base}/api/v1/events/{event_id}/dispatch",
            json_body=body, timeout=_WRITE_TIMEOUT,
        )
