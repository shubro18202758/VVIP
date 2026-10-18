"""Adaptive-parameter client (contract C4) — always fail-closed.

Fetches ``GET /api/v1/adaptive/params`` from traffic-oracle (where the
adaptive router is mounted) and returns an :class:`AdaptiveParams`. Any
failure — endpoint absent, transport error, malformed payload, non-passed
shadow-eval — collapses to the baseline constants. Deviations from baseline
are only honored when the twin shadow-eval passed.
"""

from __future__ import annotations

from dataclasses import asdict

import httpx
import structlog

from convoy_brain.config import (
    AdaptiveParams,
    BASE_ADVANCE_CLOSURE_SEC,
    BASE_CONGESTION_SPIKE_THRESHOLD,
    BASE_GPS_POLL_INTERVAL_SEC,
    BASE_SCORING_WEIGHTS,
)
from convoy_brain.http_guard import assert_platform_service_url

logger = structlog.get_logger(__name__)

ADAPTIVE_PARAMS_TIMEOUT_SEC = 2.5


class AdaptiveParamsClient:
    """Thin async client for the adaptive tier's parameter endpoint."""

    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = assert_platform_service_url(base_url.rstrip("/"))
        self._owns_client = client is None
        self._client = client
        logger.info("adaptive_params_client.init", base_url=self._base_url)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=ADAPTIVE_PARAMS_TIMEOUT_SEC)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(
        self,
        movement_id: str | None = None,
        vvip_class: str | None = None,
    ) -> AdaptiveParams:
        """Fetch movement-scoped parameters; fail closed to baseline.

        Fail-closed rule (contract C4/F12): a payload may only move the
        parameters off baseline when ``shadow_eval == "passed"``. Otherwise
        the baseline constants are returned with the payload's evidence
        attached for the audit trail.
        """
        try:
            client = await self._ensure_client()
            resp = await client.get(
                f"{self._base_url}/api/v1/adaptive/params",
                params={
                    k: v
                    for k, v in (("movement_id", movement_id), ("vvip_class", vvip_class))
                    if v
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.info(
                "adaptive_params.unavailable_fail_closed",
                movement_id=movement_id,
                error=str(exc),
            )
            return AdaptiveParams(source="baseline", shadow_eval="unavailable")

        if not isinstance(payload, dict):
            logger.warning("adaptive_params.malformed_payload_fail_closed")
            return AdaptiveParams(source="baseline", shadow_eval="unavailable")

        params_raw = payload.get("params") or {}
        shadow_eval = str(payload.get("shadow_eval") or "unavailable")
        source = str(payload.get("source") or "baseline")

        if source == "baseline" or shadow_eval != "passed":
            # Warmup period or unvalidated deviation — baseline by construction.
            return AdaptiveParams(
                situation_class=str(payload.get("situation_class") or "baseline"),
                source=source,
                shadow_eval=shadow_eval,
                evidence=_evidence(payload),
            )

        weights = dict(BASE_SCORING_WEIGHTS)
        raw_weights = params_raw.get("scoring_weights")
        if isinstance(raw_weights, dict):
            for key in weights:
                value = raw_weights.get(key)
                if isinstance(value, (int, float)):
                    weights[key] = float(value)

        try:
            advance = int(params_raw.get("advance_closure_sec", BASE_ADVANCE_CLOSURE_SEC))
            threshold = float(
                params_raw.get("congestion_spike_threshold", BASE_CONGESTION_SPIKE_THRESHOLD)
            )
            poll = float(params_raw.get("gps_poll_interval_sec", BASE_GPS_POLL_INTERVAL_SEC))
        except (TypeError, ValueError):
            logger.warning("adaptive_params.bad_numeric_values_fail_closed", payload=params_raw)
            return AdaptiveParams(source="baseline", shadow_eval=shadow_eval)

        # gps_poll has a hard protocol floor of one update per second.
        poll = max(poll, 1.0)
        # Congestion threshold must stay inside (0, 1].
        threshold = min(max(threshold, 0.05), 1.0)

        return AdaptiveParams(
            situation_class=str(payload.get("situation_class") or "baseline"),
            advance_closure_sec=advance,
            congestion_spike_threshold=threshold,
            gps_poll_interval_sec=poll,
            scoring_weights=weights,
            source=source,
            shadow_eval=shadow_eval,
            evidence=_evidence(payload),
        )


def _evidence(payload: dict) -> dict:
    evidence = payload.get("evidence")
    return evidence if isinstance(evidence, dict) else {}


def params_to_log(params: AdaptiveParams) -> dict:
    """JSON-safe audit view of the resolved parameter set."""
    return asdict(params)
