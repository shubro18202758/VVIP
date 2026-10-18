"""Twin ports — how the adaptive layer talks to the digital twin.

The C5 contract puts the twin behind ``POST /api/v1/simulate/twin``
(owned by the simulation stream and mounted in *this* service via
``simulation.router``).  The adaptive layer therefore reaches the twin
**in-process** through the ASGI app itself (``httpx.ASGITransport``):
no sockets, no hostnames, no outbound URLs.  If the simulation router
is not mounted yet (parallel build ordering) the port returns
"unavailable" and the policy engine fails closed to baseline — exactly
the C4 fail-closed mode.

The pure core (``shadow.ShadowEvaluator``) never imports this module's
I/O; it consumes ``TwinRun`` dataclasses produced here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from traffic_oracle.adaptive.shadow import TwinRun

# C5 request path inside this service.
SIMULATE_TWIN_PATH = "/api/v1/simulate/twin"
# C5 limits (contract): horizon_min ≤ 60, n_replications ≤ 20.
MAX_HORIZON_MIN = 60
MAX_REPLICATIONS = 20


@dataclass(frozen=True)
class ClosurePolicy:
    """C5 ``closure_policy`` object.

    ``closure_type`` uses the simulation router's vocabulary
    (full_closure | partial_closure | signal_hold).
    """

    advance_closure_sec: int
    closure_type: str = "full_closure"


@dataclass(frozen=True)
class ShadowContext:
    """Everything the twin needs to reproduce a comparable pair of runs."""

    route_segment_ids: tuple[int, ...]
    departure_time: str          # ISO 8601
    horizon_min: int = 30
    n_replications: int = 5
    seed: int = 0

    def __post_init__(self) -> None:
        if not self.route_segment_ids:
            raise ValueError("route_segment_ids must be non-empty")
        if not (1 <= self.horizon_min <= MAX_HORIZON_MIN):
            raise ValueError(f"horizon_min must be in [1, {MAX_HORIZON_MIN}]")
        if not (1 <= self.n_replications <= MAX_REPLICATIONS):
            raise ValueError(f"n_replications must be in [1, {MAX_REPLICATIONS}]")

    def to_request(self, policy: ClosurePolicy) -> dict:
        return {
            "route_segment_ids": list(self.route_segment_ids),
            "departure_time": self.departure_time,
            "closure_policy": {
                "advance_closure_sec": policy.advance_closure_sec,
                "closure_type": policy.closure_type,
            },
            "horizon_min": self.horizon_min,
            "n_replications": self.n_replications,
            "seed": self.seed,
        }


@dataclass(frozen=True)
class TwinPair:
    """Outcome of a paired (baseline, candidate) twin evaluation."""

    baseline: TwinRun | None
    candidate: TwinRun | None
    error: str | None


class TwinPort(Protocol):
    """Interface the policy engine needs from the twin."""

    def run_pair(
        self, baseline: ClosurePolicy, candidate: ClosurePolicy, ctx: ShadowContext
    ) -> TwinPair:
        ...  # pragma: no cover - protocol


class UnavailableTwinPort:
    """Default port: the twin is not reachable; everything fails closed."""

    def run_pair(
        self, baseline: ClosurePolicy, candidate: ClosurePolicy, ctx: ShadowContext
    ) -> TwinPair:
        return TwinPair(None, None, "twin_port_not_configured")

    def describe(self) -> str:
        return "unavailable"


class AsgiTwinPort:
    """Calls the in-app simulation router via the ASGI app (no sockets).

    ``transport`` is an ``httpx.AsyncBaseTransport``; in production the
    app wires ``httpx.ASGITransport(app=<traffic-oracle app>)`` so the
    request never leaves the process.  A separate ``client`` may be
    injected directly (tests).
    """

    def __init__(self, app: Any, client: Any | None = None) -> None:
        self._app = app
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self._app),
                base_url="http://traffic-oracle.internal",
            )
        return self._client

    async def _post(self, payload: dict) -> dict:
        client = self._get_client()
        resp = await client.post(SIMULATE_TWIN_PATH, json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"twin_http_{resp.status_code}")
        return resp.json()

    async def run_pair_async(
        self, baseline: ClosurePolicy, candidate: ClosurePolicy, ctx: ShadowContext
    ) -> TwinPair:
        try:
            baseline_payload = await self._post(ctx.to_request(baseline))
            candidate_payload = await self._post(ctx.to_request(candidate))
        except Exception as exc:  # noqa: BLE001 - any failure fails closed
            return TwinPair(None, None, type(exc).__name__)
        try:
            return TwinPair(
                TwinRun.from_payload(baseline_payload),
                TwinRun.from_payload(candidate_payload),
                None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return TwinPair(None, None, f"twin_payload_invalid:{exc}")

    def run_pair(
        self, baseline: ClosurePolicy, candidate: ClosurePolicy, ctx: ShadowContext
    ) -> TwinPair:
        """Sync facade (spins a loop) — prefer ``run_pair_async`` in the app."""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            raise RuntimeError(
                "AsgiTwinPort.run_pair must not be called from a running "
                "event loop; use run_pair_async"
            )
        return asyncio.run(self.run_pair_async(baseline, candidate, ctx))

    def describe(self) -> str:
        return "asgi-inprocess"


def build_twin_port(app: Any | None) -> TwinPort:
    """Startup factory: ASGI port when the app is available, else closed."""
    if app is None:
        return UnavailableTwinPort()
    return AsgiTwinPort(app)


__all__ = [
    "AsgiTwinPort",
    "ClosurePolicy",
    "MAX_HORIZON_MIN",
    "MAX_REPLICATIONS",
    "ShadowContext",
    "SIMULATE_TWIN_PATH",
    "TwinPair",
    "TwinPort",
    "UnavailableTwinPort",
    "build_twin_port",
]
