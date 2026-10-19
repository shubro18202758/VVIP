"""Strategic-tier client — pplx-decider-v1-27b behind the model-host API.

Contract C1 surface:

    GET  /decider/health  -> {loaded, resident_rss_gib, p95_decide_latency_ms, queue_depth}
    POST /warmup
    POST /decide          -> {request_id, state, questions:{name:{type,instructions,options?,scale?}}}
                          <- {answers:{name:{value,probability,confidence,distribution?}},
                              state_tokens, latency_ms}

Rules enforced client-side:
    - typed questions only: choice | noul | score
    - at most 6 questions per /decide call
    - at most 249 options per choice question (the readout head's letter-code budget)
    - serialized per-process (an asyncio.Lock): the model-host also queues,
      but we avoid piling onto a full queue

The decider is NOT a chat model — the jinja chat template is never used here.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from convoy_brain.config import DECIDER_HEALTH_TIMEOUT_SEC, DECIDE_TIMEOUT_SEC, DEFAULT_MODEL_HOST_URL
from convoy_brain.http_guard import assert_platform_service_url

logger = structlog.get_logger(__name__)

MAX_QUESTIONS_PER_DECIDE = 6
MAX_OPTIONS_PER_CHOICE = 249
VALID_QUESTION_TYPES = frozenset({"choice", "noul", "score"})


class DeciderUnavailable(Exception):
    """The model-host /decider endpoints are unreachable, slow, or malformed."""


@dataclass
class DeciderAnswer:
    """One typed answer with its probability and confidence.

    ``confidence`` is None when the answer type carries none (noul — the
    readout head exposes no confidence for none-of-the-above likelihood);
    such answers are EXCLUDED from confidence-floor aggregation in the
    decider, never treated as 0.0 ("failing") or as passing.
    """

    name: str
    type: str
    value: Any
    probability: float = 0.0
    confidence: float | None = 0.0
    distribution: list[dict[str, Any]] = field(default_factory=list)


def validate_questions(questions: dict[str, dict]) -> dict[str, dict]:
    """Structural validation of a question set before it is sent."""
    if not questions:
        raise ValueError("At least one question is required")
    if len(questions) > MAX_QUESTIONS_PER_DECIDE:
        raise ValueError(
            f"At most {MAX_QUESTIONS_PER_DECIDE} questions per /decide call, got {len(questions)}"
        )
    for name, spec in questions.items():
        qtype = spec.get("type")
        if qtype not in VALID_QUESTION_TYPES:
            raise ValueError(
                f"Question {name!r}: type must be one of {sorted(VALID_QUESTION_TYPES)}, got {qtype!r}"
            )
        if not spec.get("instructions"):
            raise ValueError(f"Question {name!r}: instructions are required")
        if qtype == "choice":
            options = spec.get("options")
            if not options:
                raise ValueError(f"Choice question {name!r} requires options")
            if len(options) > MAX_OPTIONS_PER_CHOICE:
                raise ValueError(
                    f"Choice question {name!r} has {len(options)} options; "
                    f"the readout head supports at most {MAX_OPTIONS_PER_CHOICE}"
                )
        if qtype == "score":
            scale = spec.get("scale")
            if scale is not None and not (
                isinstance(scale, (list, tuple)) and len(scale) == 2
            ):
                raise ValueError(f"Score question {name!r}: scale must be [min, max]")
    return questions


class DeciderClient:
    """Async client for the strategic tier (model-host)."""

    def __init__(
        self,
        base_url: str = DEFAULT_MODEL_HOST_URL,
        timeout_sec: float = DECIDE_TIMEOUT_SEC,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = assert_platform_service_url(base_url.rstrip("/"))
        self._timeout = timeout_sec
        self._owns_client = client is None
        self._client = client
        self._lock = asyncio.Lock()  # serialize decides; model-host queue is 8 deep
        self._last_health: dict[str, Any] = {}
        self._last_health_at: float = 0.0
        logger.info("decider_client.init", base_url=self._base_url, timeout_sec=timeout_sec)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- health / warmup -----------------------------------------------------

    async def health(self, timeout_sec: float = DECIDER_HEALTH_TIMEOUT_SEC) -> dict[str, Any]:
        """GET /decider/health. Returns {} (never raises) when unreachable."""
        try:
            client = await self._ensure_client()
            resp = await client.get(
                f"{self._base_url}/decider/health", timeout=timeout_sec
            )
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError("health payload is not an object")
            self._last_health = payload
            self._last_health_at = time.monotonic()
            return payload
        except Exception as exc:
            logger.warning("decider_client.health_failed", error=str(exc))
            return {}

    async def warmup(self) -> bool:
        """POST /warmup — best effort."""
        try:
            client = await self._ensure_client()
            resp = await client.post(f"{self._base_url}/warmup")
            return resp.status_code in (200, 202)
        except Exception as exc:
            logger.warning("decider_client.warmup_failed", error=str(exc))
            return False

    @property
    def p95_decide_latency_ms(self) -> float | None:
        """Last observed p95 decide latency from /decider/health, if reported."""
        value = self._last_health.get("p95_decide_latency_ms")
        if isinstance(value, (int, float)):
            return float(value)
        return None

    # -- decide ---------------------------------------------------------------

    async def decide(
        self,
        state: str,
        questions: dict[str, dict],
        request_id: str | None = None,
    ) -> dict[str, DeciderAnswer]:
        """Ask up to 6 typed questions against one state document.

        Returns answers keyed by question name. Raises
        :class:`DeciderUnavailable` on transport errors, timeouts, queue-full
        (429) exhaustions, or malformed responses — callers fall back to the
        tactical tier.
        """
        validate_questions(questions)
        payload = {
            "request_id": request_id or str(uuid.uuid4()),
            "state": state,
            "questions": questions,
        }

        async with self._lock:
            try:
                client = await self._ensure_client()
                resp = await client.post(
                    f"{self._base_url}/decide", json=payload, timeout=self._timeout
                )
            except httpx.HTTPError as exc:
                raise DeciderUnavailable(f"/decide transport error: {exc}") from exc

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "5")
                raise DeciderUnavailable(
                    f"/decide queue full (429), Retry-After={retry_after}"
                )
            if resp.status_code >= 400:
                raise DeciderUnavailable(
                    f"/decide returned HTTP {resp.status_code}: {resp.text[:200]}"
                )

            try:
                body = resp.json()
            except ValueError as exc:
                raise DeciderUnavailable("/decide returned non-JSON body") from exc

        answers_raw = body.get("answers")
        if not isinstance(answers_raw, dict) or not answers_raw:
            raise DeciderUnavailable("/decide response missing answers object")

        answers: dict[str, DeciderAnswer] = {}
        for name, spec in questions.items():
            raw = answers_raw.get(name)
            if not isinstance(raw, dict):
                raise DeciderUnavailable(f"/decide response missing answer for {name!r}")
            # Confidence coercion bug (fixed 2026-10-19): the model-host
            # returns confidence=None for noul answers ("autojev noul answers
            # carry no confidence" — model_host/decider_runtime.py). Squashing
            # None to 0.0 made every noul-bearing question set permanently
            # "low_confidence" below the floor, mislabeling every live
            # pre-movement checkpoint and blocking the tier-0 write side.
            # None now means "not applicable" and flows through; the decider
            # EXCLUDES such answers from confidence aggregation rather than
            # treating unknown as either passing or failing.
            raw_confidence = raw.get("confidence")
            answers[name] = DeciderAnswer(
                name=name,
                type=spec["type"],
                value=raw.get("value"),
                probability=float(raw.get("probability") or 0.0),
                confidence=(
                    float(raw_confidence) if raw_confidence is not None else None
                ),
                distribution=raw.get("distribution") or [],
            )

        gated = [a.confidence for a in answers.values() if a.confidence is not None]
        logger.info(
            "decider_client.decide",
            questions=list(questions),
            latency_ms=body.get("latency_ms"),
            min_confidence=min(gated) if gated else None,
        )
        return answers
