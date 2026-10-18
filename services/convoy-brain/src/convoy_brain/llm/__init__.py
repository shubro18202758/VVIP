"""LLM sub-package — the two model tiers of the convoy-brain hierarchy.

- ``decider_client`` — strategic tier: typed choice/noul/score questions to
  the pplx-decider-v1-27b model-host (POST /decide, GET /decider/health).
- ``llama_bridge`` — tactical tier: Gemma 4 12B (aditi-ps18-v7h) via
  llama-server's OpenAI-compatible /v1/chat/completions endpoint.
- ``parsing`` — shared defensive JSON extraction for model output.
- ``schemas`` — hand-authored JSON schemas for schema-constrained decoding.
"""

from convoy_brain.llm.decider_client import (
    DeciderAnswer,
    DeciderClient,
    DeciderUnavailable,
)
from convoy_brain.llm.llama_bridge import LlamaBridge, LlamaUnavailable
from convoy_brain.llm.parsing import extract_json

__all__ = [
    "DeciderAnswer",
    "DeciderClient",
    "DeciderUnavailable",
    "LlamaBridge",
    "LlamaUnavailable",
    "extract_json",
]
