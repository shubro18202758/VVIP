"""Shared machinery for the four tactical agents (Gemma 4 12B tier).

Every tactical agent follows the same pattern:
    1. gather data via MCP ToolExecutor (deterministic, no model)
    2. one schema-constrained Gemma call to synthesize the assessment
    3. one repair round if the output fails schema validation
    4. deterministic heuristic fallback if the model tier is unavailable

This module implements steps 2-4 once so each agent only declares its prompt,
schema, budget and heuristic.

Budgets and truncation: an agent's first call is budgeted at its schema's
BOUNDED maximum (:func:`agent_budget`): the live route plan hit a flat
512-token cap on both the primary call and the identical repair, and a
per-agent budget below what the schema allows (500 / 384 tokens on the Z+
plan) still cut the first reply and paid a second Gemma call every time.
``max_tokens`` only caps generation — a short reply costs nothing extra —
so the cap now sits above the longest reply the grammar can produce. A
reply still cut at the cap (``finish_reason == "length"``: whitespace the
grammar allows) is never repeated as-is: the one repair round asks for
COMPACT single-line JSON with short reasons, under a larger cap. The repair
is skipped when the plan deadline (a ``remaining()`` on the bridge —
pre_movement's _DeadlineBridge) leaves no room for it, and the fallback
always logs WHY the heuristic floor ran.
"""

from __future__ import annotations

import functools
import json
import math
import re
from dataclasses import dataclass
from typing import Any

import structlog

from convoy_brain.config import (
    AGENT_BUDGET_MARGIN,
    AGENT_CHARS_PER_TOKEN,
    AGENT_DEFAULT_MAX_TOKENS,
    AGENT_MAX_TOKENS_CEILING,
    AGENT_REPAIR_MIN_SEC,
    AGENT_RETRY_MAX_TOKENS,
)
from convoy_brain.llm.llama_bridge import FINISH_LENGTH, LlamaBridge, LlamaUnavailable
from convoy_brain.llm.parsing import extract_json
from convoy_brain.llm.schemas import load_schema

logger = structlog.get_logger(__name__)

#: Keys whose presence + rough type is checked post-hoc (strict validation
#: without pulling a JSON-Schema library into the hot path).
REQUIRED_SENTINEL = object()

#: The compact-output demand of the repair round (the usual failure is a
#: reply cut at the token cap mid-object).
COMPACT_REPAIR = (
    "Reply again with ONE compact single-line JSON object only: no "
    "whitespace beyond what JSON needs, every reason or detail 20 words or "
    "fewer, and only the keys the schema requires."
)


@dataclass
class AgentSpec:
    """Declarative description of one tactical agent."""

    name: str
    system_prompt: str
    schema_name: str


# ---------------------------------------------------------------------------
# Output budgets: the schema's bounded maximum
# ---------------------------------------------------------------------------

#: The widest serialization the grammar's unbounded scalars take in
#: practice: Gemma echoes a float at full precision ("0.1585751026868820"),
#: and segment ids / second counts stay within 8 digits.
_WIDEST_NUMBER = 0.1234567890123456
_WIDEST_INTEGER = 12345678


class UnboundedSchemaError(ValueError):
    """A schema with no derivable maximum (a string without maxLength, an
    array without maxItems, an object open to additional properties)."""


def schema_max_instance(schema: Any) -> Any:
    """The longest value a bounded JSON schema admits (compact serialization).

    Strings fill their maxLength, arrays their maxItems, objects carry every
    property (optional ones included); an enum gives its longest member and
    a union its longest branch. Raises :class:`UnboundedSchemaError` when the
    grammar llama-server compiles from the schema has no ceiling.
    """
    if not isinstance(schema, dict):
        raise UnboundedSchemaError(f"not a schema: {schema!r}")
    if "enum" in schema:
        return max(schema["enum"], key=lambda v: len(_compact(v)))
    branches = schema.get("anyOf") or schema.get("oneOf")
    if branches:
        return max((schema_max_instance(b) for b in branches), key=lambda v: len(_compact(v)))
    kind = schema.get("type")
    if isinstance(kind, list):
        return max(
            (schema_max_instance({**schema, "type": k}) for k in kind),
            key=lambda v: len(_compact(v)),
        )
    if kind == "string":
        limit = schema.get("maxLength")
        if not isinstance(limit, int):
            raise UnboundedSchemaError("string without maxLength")
        return "x" * limit
    if kind == "integer":
        return _WIDEST_INTEGER
    if kind == "number":
        return _WIDEST_NUMBER
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    if kind == "array":
        limit = schema.get("maxItems")
        if not isinstance(limit, int):
            raise UnboundedSchemaError("array without maxItems")
        return [schema_max_instance(schema.get("items"))] * limit
    if kind == "object":
        if schema.get("additionalProperties", True) is not False:
            raise UnboundedSchemaError("object open to additional properties")
        return {key: schema_max_instance(sub)
                for key, sub in (schema.get("properties") or {}).items()}
    raise UnboundedSchemaError(f"unsupported schema type {kind!r}")


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def estimate_tokens(text: str) -> float:
    """Gemma tokens of a JSON reply, estimated from above.

    Every digit is its own token on Gemma's tokenizer (a 16-decimal float
    is 18 tokens); everything else runs at AGENT_CHARS_PER_TOKEN — the floor
    of the ratios measured on the live agents' replies.
    """
    digits = sum(ch.isdigit() for ch in text)
    return digits + (len(text) - digits) / AGENT_CHARS_PER_TOKEN


@functools.cache
def agent_budget(schema_name: str) -> int:
    """First-call output budget of an agent: its schema's bounded maximum.

    The longest compact reply the schema admits, in estimated tokens, plus
    AGENT_BUDGET_MARGIN; capped at AGENT_MAX_TOKENS_CEILING (the plan
    agents' schemas are tested to fit under it). A schema with no bounded
    maximum gets AGENT_DEFAULT_MAX_TOKENS.
    """
    try:
        widest = _compact(schema_max_instance(load_schema(schema_name)))
    except UnboundedSchemaError:
        return AGENT_DEFAULT_MAX_TOKENS
    return min(
        AGENT_MAX_TOKENS_CEILING,
        math.ceil(estimate_tokens(widest) * AGENT_BUDGET_MARGIN),
    )


# ---------------------------------------------------------------------------
# Narration hygiene: what an operator reads
# ---------------------------------------------------------------------------

_FIGURE_RE = re.compile(r"\d")


def whole_sentences(text: Any, limit: int | None = None) -> str:
    """Model text cut back to its last complete sentence when it was clipped.

    The grammar closes a string at its schema ``maxLength`` mid-sentence
    ("… is not suitable for the Z+ class. Therefore, the"), and the model,
    nearing the bound, trails off itself ("… The route has 3 lane ..."):
    such a string ends at its last full stop, or at a word boundary with an
    ellipsis when it has none. Text that ends where the model ended it is
    untouched.
    """
    out = str(text or "").strip()
    trailed = out.endswith(("...", "…"))
    if trailed:
        out = out.rstrip(".… ")
    elif limit is None or len(out) < limit - 1 or not out or out[-1] in ".!?)":
        return out
    cut = max(out.rfind(mark) for mark in (". ", "! ", "? "))
    if cut > 0:
        return out[:cut + 1]
    if trailed or " " not in out:
        return out + " …"  # the model stopped after a whole word
    return out.rsplit(" ", 1)[0].rstrip(",;:—- ") + " …"  # the grammar cut one in half


def grounded_text(text: Any, limit: int | None = None) -> str | None:
    """Model text fit to show an operator, or None when it cites no figure.

    A reason without a single figure ("the route has a higher ETA than the
    primary") is no reason: the caller keeps its deterministic line instead.
    """
    out = whole_sentences(text, limit)
    return out if _FIGURE_RE.search(out) else None


async def structured_agent_call(
    bridge: LlamaBridge,
    spec: AgentSpec,
    user_prompt: str,
    heuristic,
    *,
    repair_hint: str = "",
    max_tokens: int | None = None,
    compact_repair: bool = True,
) -> tuple[dict, str]:
    """Run the shared generate -> validate -> repair -> fallback cycle.

    Args:
        bridge: the tactical-tier bridge (Gemma).
        spec: agent prompt + schema identity.
        user_prompt: serialized task payload.
        heuristic: zero-arg callable returning the deterministic fallback dict.
        repair_hint: the keys the repair prompt names.
        max_tokens: an explicit output budget; default :func:`agent_budget`
            (the schema's bounded maximum — the first reply is never cut).
        compact_repair: the repair demands single-line JSON with short reasons.

    Returns:
        (result, tier) where tier is "gemma" or "heuristic". A heuristic
        result carries ``_fallback_reason`` (why the model tier was not used).
    """
    schema = load_schema(spec.schema_name)
    budget = int(max_tokens or agent_budget(spec.schema_name))

    # -- primary, schema-constrained call --------------------------------
    raw, finish = await _generate(bridge, spec, user_prompt, schema, budget)
    if raw is not None:
        parsed = extract_json(raw)
        if isinstance(parsed, dict):
            return parsed, "gemma"

    if raw is None:
        reason = "tactical tier unavailable"
    elif finish == FINISH_LENGTH:
        reason = f"reply cut at the {budget}-token cap"
    else:
        reason = "reply did not parse as the schema's JSON object"

    # -- one repair round --------------------------------------------------
    if raw is not None:
        remaining = _remaining(bridge)
        if remaining is not None and remaining < AGENT_REPAIR_MIN_SEC:
            reason += f"; repair skipped ({remaining:.0f}s of plan budget left)"
        else:
            cut = finish == FINISH_LENGTH
            repair_prompt = (
                f"{user_prompt}\n\nYour previous reply "
                + ("was cut off at the length limit" if cut else "was not valid JSON")
                + f" matching the required shape{(' — ' + repair_hint) if repair_hint else ''}. "
                + (COMPACT_REPAIR if (compact_repair or cut)
                   else "Reply again with a single JSON object only.")
            )
            repair_budget = (
                max(budget, min(AGENT_RETRY_MAX_TOKENS, budget * 2)) if cut else budget
            )
            raw2, finish2 = await _generate(bridge, spec, repair_prompt, schema, repair_budget)
            if raw2 is not None:
                parsed = extract_json(raw2)
                if isinstance(parsed, dict):
                    logger.info(
                        "tactical_agent.repaired",
                        agent=spec.name,
                        first_failure=reason,
                        repair_max_tokens=repair_budget,
                    )
                    return parsed, "gemma"
                reason += (
                    f"; repair cut at the {repair_budget}-token cap" if finish2 == FINISH_LENGTH
                    else "; repair did not parse either"
                )
            else:
                reason += "; repair call failed"

    # -- deterministic floor -------------------------------------------------
    logger.warning("tactical_agent.heuristic_fallback", agent=spec.name, reason=reason)
    result = heuristic()
    if isinstance(result, dict):
        result.setdefault("_fallback_reason", reason)
    return result, "heuristic"


def _remaining(bridge: Any) -> float | None:
    """Seconds of plan budget the bridge has left (None: unbounded bridge)."""
    remaining = getattr(type(bridge), "remaining", None)
    if not callable(remaining):
        return None
    try:
        value = bridge.remaining()
    except Exception:  # a duck-typed bridge without a usable clock
        return None
    return float(value) if isinstance(value, (int, float)) else None


async def _generate(
    bridge: LlamaBridge,
    spec: AgentSpec,
    user_prompt: str,
    schema: dict[str, Any],
    max_tokens: int,
) -> tuple[str | None, str | None]:
    """One schema-constrained call: ``(content, finish_reason)``; None on failure.

    Bridges that only offer ``generate`` (test doubles, older bridges) are
    called through it; their finish reason is unknown (None).
    """
    kwargs = {
        "system_prompt": spec.system_prompt,
        "user_prompt": user_prompt,
        "json_mode": True,
        "schema": schema,
        "schema_name": spec.schema_name,
        "max_tokens": max_tokens,
    }
    try:
        with_meta = getattr(bridge, "generate_with_meta", None)
        if with_meta is not None and callable(with_meta):
            content, finish = await with_meta(**kwargs)
            return content, finish
        return await bridge.generate(**kwargs), None
    except LlamaUnavailable as exc:
        logger.warning("tactical_agent.llm_unavailable", agent=spec.name, error=str(exc))
        return None, None
