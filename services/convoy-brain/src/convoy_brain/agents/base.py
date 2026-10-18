"""Shared machinery for the four tactical agents (Gemma 4 12B tier).

Every tactical agent follows the same pattern:
    1. gather data via MCP ToolExecutor (deterministic, no model)
    2. one schema-constrained Gemma call to synthesize the assessment
    3. one repair round if the output fails schema validation
    4. deterministic heuristic fallback if the model tier is unavailable

This module implements steps 2-4 once so each agent only declares its prompt,
schema, and heuristic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from convoy_brain.llm.llama_bridge import LlamaBridge, LlamaUnavailable
from convoy_brain.llm.parsing import extract_json
from convoy_brain.llm.schemas import load_schema

logger = structlog.get_logger(__name__)

#: Keys whose presence + rough type is checked post-hoc (strict validation
#: without pulling a JSON-Schema library into the hot path).
REQUIRED_SENTINEL = object()


@dataclass
class AgentSpec:
    """Declarative description of one tactical agent."""

    name: str
    system_prompt: str
    schema_name: str


async def structured_agent_call(
    bridge: LlamaBridge,
    spec: AgentSpec,
    user_prompt: str,
    heuristic,
    *,
    repair_hint: str = "",
) -> tuple[dict, str]:
    """Run the shared generate -> validate -> repair -> fallback cycle.

    Args:
        bridge: the tactical-tier bridge (Gemma).
        spec: agent prompt + schema identity.
        user_prompt: serialized task payload.
        heuristic: zero-arg callable returning the deterministic fallback dict.

    Returns:
        (result, tier) where tier is "gemma" or "heuristic".
    """
    schema = load_schema(spec.schema_name)

    # -- primary, schema-constrained call --------------------------------
    raw = await _generate(bridge, spec, user_prompt, schema)
    if raw is not None:
        parsed = extract_json(raw)
        if isinstance(parsed, dict):
            return parsed, "gemma"

    # -- one repair round --------------------------------------------------
    if raw is not None:
        repair_prompt = (
            f"{user_prompt}\n\nYour previous reply was not valid JSON matching the "
            f"required shape{(' — ' + repair_hint) if repair_hint else ''}. "
            f"Reply again with a single JSON object only."
        )
        raw = await _generate(bridge, spec, repair_prompt, schema)
        if raw is not None:
            parsed = extract_json(raw)
            if isinstance(parsed, dict):
                return parsed, "gemma"

    # -- deterministic floor -------------------------------------------------
    logger.warning("tactical_agent.heuristic_fallback", agent=spec.name)
    return heuristic(), "heuristic"


async def _generate(
    bridge: LlamaBridge,
    spec: AgentSpec,
    user_prompt: str,
    schema: dict[str, Any],
) -> str | None:
    try:
        return await bridge.generate(
            system_prompt=spec.system_prompt,
            user_prompt=user_prompt,
            json_mode=True,
            schema=schema,
            schema_name=spec.schema_name,
        )
    except LlamaUnavailable as exc:
        logger.warning("tactical_agent.llm_unavailable", agent=spec.name, error=str(exc))
        return None
