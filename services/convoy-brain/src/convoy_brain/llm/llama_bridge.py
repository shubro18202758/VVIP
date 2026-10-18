"""Tactical-tier client — Gemma 4 12B (aditi-ps18-v7h) on llama-server.

Speaks llama-server's OpenAI-compatible endpoint::

    POST {LLAMA_BASE_URL}/v1/chat/completions
    GET  {LLAMA_BASE_URL}/health

The GGUF embeds Gemma 4's canonical chat template, which llama-server applies
server-side — the bridge sends standard ``system``/``user``/``assistant``
messages and never hand-renders the template.

Schema-constrained decoding ladder (contract C2; json_schema support in
llama-server build 69bf643 is smoke-tested by VVIP/scripts/smoke_models.sh):

    1. ``response_format: {"type": "json_schema", json_schema: {...}}``
       (llama-server compiles this to a GBNF grammar internally)
    2. ``response_format: {"type": "json_object"}``
    3. prompt-embedded schema + strict post-hoc validation

``suppress_thinking`` is accepted and deliberately IGNORED: it is leftover
vocabulary from the retired stack (``/no_think`` + ``think=False``) with no
llama-server meaning. We log that once for operators.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from convoy_brain.config import DEFAULT_LLAMA_URL, GEMMA_MODEL_ID, LLAMA_CALL_TIMEOUT_SEC
from convoy_brain.http_guard import assert_platform_service_url

logger = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_SEC = LLAMA_CALL_TIMEOUT_SEC  # per-call wall bound (was 120 s)
DEFAULT_MAX_TOKENS = 512
DEFAULT_TEMPERATURE = 0.2  # low temperature for deterministic tactical output


class LlamaUnavailable(Exception):
    """llama-server is unreachable or persistently erroring."""


class LlamaBridge:
    """Async interface to the Gemma 4 12B tactical model."""

    def __init__(
        self,
        base_url: str = DEFAULT_LLAMA_URL,
        model: str = GEMMA_MODEL_ID,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = assert_platform_service_url(base_url.rstrip("/"))
        self._model = model
        self._timeout = timeout_sec
        self._max_tokens = max_tokens
        self._owns_client = client is None
        self._client = client
        self._suppress_thinking_notice_logged = False
        logger.info(
            "llama_bridge.init",
            base_url=self._base_url,
            model=model,
            max_tokens=max_tokens,
        )

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def check_health(self) -> bool:
        """True when llama-server /health responds 200."""
        try:
            client = await self._ensure_client()
            resp = await client.get(f"{self._base_url}/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    # -- generation ------------------------------------------------------------

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        json_mode: bool = True,
        suppress_thinking: bool = True,
        *,
        schema: dict[str, Any] | None = None,
        schema_name: str = "response",
        temperature: float = DEFAULT_TEMPERATURE,
        extra_messages: list[dict[str, str]] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Generate one completion from the tactical model.

        Args:
            system_prompt: agent system instructions (kept Gemma-native —
                no legacy ``/no_think`` tokens; ``suppress_thinking`` is
                accepted for call-site compatibility and ignored).
            user_prompt: the task payload.
            json_mode: request JSON-shaped output.
            schema: optional JSON Schema for constrained decoding.
            schema_name: name for the constrained request.
            extra_messages: additional prior turns (role/content dicts)
                appended after the system prompt, before the user prompt.
            max_tokens: per-call output budget override. Call sites with a
                known small envelope (tool selection, argument drafting)
                pass a tight cap so a rambling generation cannot blow the
                turn budget; the grammar-constrained stop usually fires far
                earlier than this cap.
        """
        if suppress_thinking and not self._suppress_thinking_notice_logged:
            logger.info(
                "llama_bridge.suppress_thinking_ignored",
                note="suppress_thinking is retired-stack vocabulary; llama-server has no such switch",
            )
            self._suppress_thinking_notice_logged = True

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for msg in extra_messages or []:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_prompt})

        client = await self._ensure_client()
        call_max_tokens = max_tokens if max_tokens is not None else self._max_tokens

        def build_payload(response_format: dict | None, prompt: str) -> dict:
            payload: dict[str, Any] = {
                "model": self._model,
                "messages": (
                    messages if response_format is not None
                    else [
                        *messages[:-1],
                        {"role": "user", "content": prompt},
                    ]
                ),
                "temperature": temperature,
                "max_tokens": call_max_tokens,
            }
            if response_format is not None:
                payload["response_format"] = response_format
            return payload

        # Ladder step 1: json_schema constrained decoding
        if json_mode and schema is not None:
            response_format: dict | None = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                },
            }
            try:
                content = await self._post_completion(
                    client, build_payload(response_format, user_prompt)
                )
                if content is not None:
                    return content
            except _SchemaUnsupported:
                logger.warning("llama_bridge.json_schema_unsupported_fallback")

        # Ladder step 2: generic JSON object mode
        if json_mode:
            try:
                content = await self._post_completion(
                    client, build_payload({"type": "json_object"}, user_prompt)
                )
                if content is not None:
                    return content
            except _SchemaUnsupported:
                pass

        # Ladder step 3: prompt-embedded schema, unconstrained decoding
        prompt = user_prompt
        if json_mode and schema is not None:
            prompt = (
                f"{user_prompt}\n\nRespond with a single valid JSON object that "
                f"conforms exactly to this JSON Schema:\n{json.dumps(schema)}"
            )
        elif json_mode:
            prompt = (
                f"{user_prompt}\n\nRespond with a single valid JSON object and "
                f"nothing else — no markdown fences, no commentary."
            )
        content = await self._post_completion(client, build_payload(None, prompt), strict=True)
        if content is None:
            raise LlamaUnavailable("tactical model returned an empty completion")
        return content

    async def _post_completion(
        self, client: httpx.AsyncClient, payload: dict, strict: bool = False
    ) -> str | None:
        """POST /v1/chat/completions once. Returns content or None (retryable)."""
        try:
            resp = await client.post(
                f"{self._base_url}/v1/chat/completions",
                json=payload,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise LlamaUnavailable(f"chat/completions transport error: {exc}") from exc

        if resp.status_code in (400, 422) and "response_format" in resp.text:
            # Server rejects structured output — climb the ladder.
            raise _SchemaUnsupported(resp.text[:200])

        if resp.status_code == 404:
            raise LlamaUnavailable("chat/completions endpoint not found (404)")

        if resp.status_code >= 400:
            raise LlamaUnavailable(
                f"chat/completions returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        try:
            body = resp.json()
        except ValueError as exc:
            if strict:
                raise LlamaUnavailable("chat/completions returned non-JSON body") from exc
            return None

        try:
            content: str = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            if strict:
                raise LlamaUnavailable("chat/completions response missing choices/content") from exc
            return None

        content = content.strip()
        if strict and not content:
            raise LlamaUnavailable("chat/completions returned empty content")
        return content or None


class _SchemaUnsupported(Exception):
    """llama-server rejected a response_format payload — fall to next rung."""
