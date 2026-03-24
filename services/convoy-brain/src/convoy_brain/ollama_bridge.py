"""Ollama Bridge — interface to the local Qwen 3.5 9B LLM via Ollama.

This is the single point of contact between the agentic layer and the LLM.
All agent invocations route through this bridge, which:
    - Manages Ollama connection lifecycle
    - Enforces token limits to stay within VRAM KV-cache budget
    - Implements structured output parsing (JSON mode)
    - Provides request queuing to prevent concurrent LLM calls
      (which would cause VRAM contention)
"""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger(__name__)

# Model configuration — tuned for 8GB VRAM with Q4_K_M quantization
OLLAMA_MODEL = "qwen3.5:9b-q4_K_M"
OLLAMA_BASE_URL = "http://localhost:11434"
MAX_CONTEXT_TOKENS = 8192   # Conservative to limit KV-cache VRAM growth
MAX_OUTPUT_TOKENS = 2048
TEMPERATURE = 0.3           # Low temperature for deterministic planning decisions


class OllamaBridge:
    """Async interface to Qwen 3.5 9B running on local Ollama instance.

    Serializes all LLM calls through a single asyncio.Lock to prevent
    concurrent inference requests that would spike VRAM beyond budget.
    """

    def __init__(
        self,
        model: str = OLLAMA_MODEL,
        base_url: str = OLLAMA_BASE_URL,
    ) -> None:
        self._model = model
        self._base_url = base_url
        self._client = None  # Ollama async client (lazy init)
        self._lock = asyncio.Lock()  # Serialize LLM calls
        logger.info(
            "ollama_bridge.init",
            model=model,
            base_url=base_url,
            max_ctx=MAX_CONTEXT_TOKENS,
        )

    async def _ensure_client(self) -> None:
        """Lazy-initialize the Ollama client."""
        if self._client is None:
            import ollama
            self._client = ollama.AsyncClient(host=self._base_url)
            logger.info("ollama_bridge.connected", base_url=self._base_url)

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        json_mode: bool = True,
        suppress_thinking: bool = True,
    ) -> str:
        """Generate a response from Qwen via Ollama.

        Uses asyncio.Lock to ensure only one inference runs at a time,
        preventing VRAM contention from concurrent KV-cache allocations.

        Args:
            system_prompt: Agent's system instructions
            user_prompt: The specific query/task
            json_mode: If True, request JSON-formatted output
            suppress_thinking: If True, append /no_think and set think=False.
                Set to False for reasoning endpoints that need deep CoT output.

        Returns:
            Raw response text from the LLM
        """
        await self._ensure_client()

        async with self._lock:
            logger.debug(
                "ollama_bridge.generate.start",
                model=self._model,
                system_len=len(system_prompt),
                user_len=len(user_prompt),
                suppress_thinking=suppress_thinking,
            )

            if suppress_thinking:
                # Append /no_think token to enforce no-think mode at content level
                # (belt-and-suspenders with the think=False API parameter)
                enforced_prompt = user_prompt.rstrip() + " /no_think"
            else:
                # Skip /no_think token so model produces detailed content output.
                # Still keep think=False for VRAM safety (no thinking block allocation).
                enforced_prompt = user_prompt

            response = await self._client.chat(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": enforced_prompt},
                ],
                options={
                    "num_ctx": MAX_CONTEXT_TOKENS,
                    "num_predict": MAX_OUTPUT_TOKENS,
                    "temperature": TEMPERATURE,
                },
                format="json" if json_mode else "",
                think=False,  # Always False — VRAM cannot support thinking blocks
            )

            content = response["message"]["content"]
            # Fallback: if content is empty but thinking field has output, use thinking
            if not content.strip():
                thinking = response.get("message", {}).get("thinking", "")
                if thinking:
                    logger.warning("ollama_bridge.thinking_fallback", thinking_len=len(thinking))
                    content = thinking
            logger.debug(
                "ollama_bridge.generate.complete",
                response_len=len(content),
            )
            return content

    async def check_health(self) -> bool:
        """Verify Ollama is running and the model is loaded."""
        try:
            await self._ensure_client()
            models = await self._client.list()
            loaded = any(m.model == self._model for m in models.models)
            logger.info("ollama_bridge.health", loaded=loaded, model=self._model)
            return loaded
        except Exception:
            logger.error("ollama_bridge.health_failed")
            return False
