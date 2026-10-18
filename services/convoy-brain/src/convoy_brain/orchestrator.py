"""Two-tier Orchestrator — decider (strategic) + Gemma (tactical) + heuristics.

Hierarchy of every turn:

    STRATEGIC  pplx-decider-v1-27b picks the next TOOL via a typed choice
               question (or 'final_answer'), with a confidence score.
    TACTICAL   Gemma 4 12B drafts the tool ARGUMENTS against the tool's
               inputSchema (one repair round on validation failure) and later
               composes the final structured answer.
    FLOOR      deterministic heuristics — keyword tool selection, default
               arguments, and a degraded final answer.

Fallback chain (the ask): decider -> Gemma -> deterministic heuristics.
Whenever a tier is slow or unavailable the turn falls through to the next and
the drop is audit-logged into the movement's decisions_log together with the
decider's {value, probability, confidence} distribution when it ran.

This module performs no database access of any kind; ToolExecutor dispatch
routes MCP tool calls to the traffic-oracle service over HTTP.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from convoy_brain.config import (
    TACTICAL_ARGS_MAX_TOKENS,
    TACTICAL_FINAL_MAX_TOKENS,
    TACTICAL_SELECT_MAX_TOKENS,
    TOOL_RESULT_TRUNCATE_BYTES,
)
from convoy_brain.decider import DeciderBrain
from convoy_brain.llm.decider_client import DeciderClient
from convoy_brain.llm.heuristics import (
    heuristic_default_arguments,
    heuristic_final_answer,
    heuristic_select_tool,
)
from convoy_brain.llm.llama_bridge import LlamaBridge, LlamaUnavailable
from convoy_brain.llm.parsing import extract_json, truncate_for_context
from convoy_brain.llm.schemas import load_schema
from convoy_brain.mcp.prompts import TOOL_CALLING_INSTRUCTION, VVIP_AGENT_SYSTEM_PROMPT
from convoy_brain.mcp.server import (
    TOOL_DEFINITIONS,
    ToolExecutionError,
    ToolExecutor,
)
from convoy_brain.memory.convoy_context import ConvoyContextStore

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_TOOL_ROUNDS = 6          # Max tier loops before forced termination
MAX_RETRIES_PER_TOOL = 2     # Max retries for a single failed tool call
TOOL_TIMEOUT_SEC = 30.0      # Per-tool execution timeout
TOTAL_TURN_TIMEOUT_SEC = 120.0  # Max time for a complete orchestration turn

_TOOL_MENU = [t["name"] for t in TOOL_DEFINITIONS]
_TOOL_SCHEMAS = {t["name"]: t.get("inputSchema", {}) for t in TOOL_DEFINITIONS}

# Event-agnostic alias names (contract C2): generalized names ship as
# advertised aliases of the canonical tools.  Any model tier (or the
# heuristic floor) may answer with an alias; normalization maps it onto
# the canonical wire name before dispatch.
_TOOL_ALIASES: dict[str, str] = {
    "find_corridor_routes": "find_convoy_routes",
    "plan_event_diversions": "plan_diversions",
    "evaluate_event_scenarios": "evaluate_scenarios",
    "get_active_corridor_events": "get_live_traffic",
}

FINAL_ANSWER = "final_answer"


def normalize_tool_name(name: str | None) -> str | None:
    """Map an alias tool name onto its canonical wire name (identity otherwise)."""
    if not isinstance(name, str):
        return name
    return _TOOL_ALIASES.get(name, name)


def _tool_selection_schema() -> dict:
    """Grammar for tactical tool selection: one call, name constrained to the
    menu (+ final_answer). llama-server compiles this to GBNF, so the reply
    stops at the closing brace of a valid envelope instead of rambling."""
    return {
        "type": "object",
        "properties": {
            "tool_calls": {
                "type": "array",
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": _TOOL_MENU + [FINAL_ANSWER]}
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tool_calls"],
        "additionalProperties": False,
    }


def _tool_args_schema(tool_name: str) -> dict:
    """Grammar for argument drafting: ``{"arguments": <tool inputSchema>}``.

    Embedding the tool's own inputSchema under ``arguments`` makes the GBNF
    grammar enforce the tool's required keys and types directly, which is
    what makes the 110 s cap-hitting ramble structurally impossible: the
    generation stops as soon as the envelope closes.
    """
    input_schema = _TOOL_SCHEMAS.get(tool_name) or {"type": "object"}
    return {
        "type": "object",
        "properties": {"arguments": input_schema},
        "required": ["arguments"],
        "additionalProperties": False,
    }

# Default Ahmedabad corridor bbox handed to the heuristic-argument floor.
_DEFAULT_BBOX = (72.48, 22.95, 72.68, 23.12)


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------

@dataclass
class Message:
    """A single message in the conversation history."""

    role: str           # "system" | "user" | "assistant" | "tool"
    content: str
    tool_name: str | None = None
    tool_call_id: str | None = None


@dataclass
class ConversationState:
    """Multi-turn conversation state for a single orchestration session."""

    session_id: str
    movement_id: str | None = None
    vvip_class: str | None = None
    messages: list[Message] = field(default_factory=list)
    tool_calls_total: int = 0
    tool_calls: list[dict] = field(default_factory=list)  # audit of {name, arguments}
    errors: list[dict] = field(default_factory=list)
    operator_alerts: list[dict] = field(default_factory=list)
    tiers_used: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        self.messages.append(Message(role=role, content=content, **kwargs))

    def build_chat_messages(self) -> list[dict[str, str]]:
        """Convert conversation to OpenAI chat format for llama-server.

        Gemma's canonical chat template (applied server-side) supports the
        system role; tool results are injected as user turns with an explicit
        tool header so the tactical model can ground on them.
        """
        result = []
        for msg in self.messages:
            if msg.role == "tool":
                result.append({
                    "role": "user",
                    "content": "[Tool Result: " + str(msg.tool_name) + "]\n" + msg.content,
                })
            else:
                result.append({"role": msg.role, "content": msg.content})
        return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """Two-tier agentic host binding the model tiers to the MCP tool ecosystem."""

    def __init__(
        self,
        bridge: LlamaBridge,
        tool_executor: ToolExecutor,
        context_store: ConvoyContextStore,
        decider_client: DeciderClient | None = None,
        decider_brain: DeciderBrain | None = None,
    ) -> None:
        self._bridge = bridge
        self._executor = tool_executor
        self._context_store = context_store
        self._decider = decider_brain or (
            DeciderBrain(decider_client) if decider_client is not None else None
        )
        self._active_sessions: dict[str, ConversationState] = {}
        logger.info(
            "orchestrator.init",
            tactical_model="gemma",
            strategic_model="decider" if self._decider else "disabled",
        )

    # -- sessions ---------------------------------------------------------------

    async def create_session(
        self,
        session_id: str,
        movement_id: str | None = None,
        vvip_class: str | None = None,
    ) -> ConversationState:
        """Initialize a new orchestration session with the system prompt."""
        state = ConversationState(
            session_id=session_id,
            movement_id=movement_id,
            vvip_class=vvip_class,
        )

        system_prompt = VVIP_AGENT_SYSTEM_PROMPT + "\n\n" + TOOL_CALLING_INSTRUCTION

        if movement_id:
            ctx = await self._context_store.get(movement_id)
            if ctx:
                system_prompt += (
                    "\n\n## Active Movement Context\n"
                    "Movement ID: " + str(ctx.movement_id) + "\n"
                    "VVIP Class: " + str(ctx.vvip_class) + "\n"
                    "Status: " + str(ctx.status) + "\n"
                    "Selected Route: " + str(ctx.selected_route_id or "None") + "\n"
                    "Convoy Position: " + str(ctx.convoy_position or "Not deployed") + "\n"
                    "Active Diversions: " + str(len(ctx.active_diversions)) + "\n"
                )
                state.vvip_class = state.vvip_class or ctx.vvip_class

        if state.vvip_class:
            protocol_messages = self._executor.get_prompt(
                "vvip_security_protocol", {"vvip_class": state.vvip_class}
            )
            for pm in protocol_messages:
                content = pm.get("content", {})
                if isinstance(content, dict):
                    system_prompt += "\n\n" + content.get("text", "")
                elif isinstance(content, str):
                    system_prompt += "\n\n" + content

        state.add_message("system", system_prompt)
        self._active_sessions[session_id] = state

        logger.info(
            "orchestrator.session_created",
            session_id=session_id,
            movement_id=movement_id,
            vvip_class=vvip_class,
        )
        return state

    def get_session(self, session_id: str) -> ConversationState | None:
        return self._active_sessions.get(session_id)

    def end_session(self, session_id: str) -> None:
        self._active_sessions.pop(session_id, None)
        logger.info("orchestrator.session_ended", session_id=session_id)

    # -- main loop ------------------------------------------------------------

    async def process_turn(self, session_id: str, user_input: str) -> dict:
        """Process one operator turn through the two-tier loop."""
        state = self._active_sessions.get(session_id)
        if state is None:
            raise ValueError("No active session: " + session_id)

        state.add_message("user", user_input)

        turn_start = time.time()
        turn_deadline = turn_start + TOTAL_TURN_TIMEOUT_SEC
        tool_round = 0
        final_response: dict | None = None
        executed_signatures: set[tuple] = set()

        logger.info(
            "orchestrator.turn.start",
            session_id=session_id,
            user_input_len=len(user_input),
        )

        while tool_round < MAX_TOOL_ROUNDS:
            if time.time() >= turn_deadline:
                elapsed = round(time.time() - turn_start, 1)
                logger.warning("orchestrator.turn.timeout", elapsed_sec=elapsed)
                timeout_msg = (
                    "Processing exceeded " + str(TOTAL_TURN_TIMEOUT_SEC) + "s timeout after "
                    + str(tool_round) + " tool rounds. Returning partial results."
                )
                final_response = self._error_response(timeout_msg, action="timeout")
                break

            # ---- STRATEGIC tier: decider picks the next tool ----------------
            next_tool, decider_audit = await self._select_tool(
                state, user_input, deadline=turn_deadline
            )
            if decider_audit:
                state.operator_alerts.extend(decider_audit.get("alerts", []))
                state.tiers_used.append("decider:" + str(decider_audit.get("tier", "unknown")))

            if next_tool == FINAL_ANSWER or next_tool is None:
                final_response = await self._final_answer(
                    state, user_input, deadline=turn_deadline
                )
                break

            # ---- TACTICAL tier: Gemma drafts the arguments --------------------
            arguments, arg_tier = await self._draft_arguments(
                state, next_tool, user_input, deadline=turn_deadline
            )
            state.tiers_used.append("gemma_args:" + arg_tier)

            signature = (next_tool, json.dumps(arguments, sort_keys=True, default=str))
            if signature in executed_signatures:
                # Identical (tool, arguments) already ran this turn — its
                # result is already in the transcript. Re-dispatching would
                # be a duplicate round (reasoning-loop signal to the gate);
                # compose the final answer from what we have instead.
                logger.info("orchestrator.duplicate_tool_call_skipped", tool=next_tool)
                final_response = await self._final_answer(
                    state, user_input, deadline=turn_deadline
                )
                break
            executed_signatures.add(signature)

            # ---- Dispatch the tool call via MCP (HTTP) -------------------------
            tool_round += 1
            logger.info(
                "orchestrator.tool_round",
                round=tool_round,
                tool=next_tool,
                arg_tier=arg_tier,
            )

            state.tool_calls.append({"name": next_tool, "arguments": arguments})
            state.tool_calls_total += 1

            results = await self._dispatch_tool_calls(
                [{"name": next_tool, "arguments": arguments}],
                deadline=turn_deadline,
            )
            for result in results:
                rendered = json.dumps(result["result"], default=str)
                state.add_message(
                    "tool",
                    truncate_for_context(rendered, TOOL_RESULT_TRUNCATE_BYTES),
                    tool_name=result["name"],
                    tool_call_id=result.get("call_id"),
                )

        if final_response is None:
            max_rounds_msg = (
                "Reached maximum " + str(MAX_TOOL_ROUNDS) + " tool interaction rounds."
            )
            final_response = self._error_response(max_rounds_msg, action="max_rounds_exceeded")

        # Audit trail: decisions_log entry for this turn.
        final_response["tool_calls_made"] = [
            tc["name"] for tc in state.tool_calls
        ]
        final_response["tool_calls"] = list(state.tool_calls)
        final_response["tiers_used"] = list(dict.fromkeys(state.tiers_used)) or ["heuristic"]
        if state.operator_alerts:
            final_response["operator_alerts"] = state.operator_alerts

        if state.movement_id:
            await self._context_store.append_decision(
                state.movement_id,
                {
                    "session_id": session_id,
                    "phase": "orchestrator_turn",
                    "action": final_response.get("action"),
                    "confidence": final_response.get("confidence"),
                    "tiers_used": final_response.get("tiers_used"),
                    "tool_calls_total": state.tool_calls_total,
                    "operator_alerts": len(state.operator_alerts),
                    "turn_duration_sec": round(time.time() - turn_start, 2),
                    "timestamp": time.time(),
                },
            )
            for alert in state.operator_alerts:
                await self._context_store.append_decision(
                    state.movement_id, dict(alert, phase="operator_alert")
                )

        logger.info(
            "orchestrator.turn.complete",
            session_id=session_id,
            tool_rounds=tool_round,
            tool_calls_total=state.tool_calls_total,
            duration_sec=round(time.time() - turn_start, 2),
        )

        return final_response

    # -- strategic tier ----------------------------------------------------------

    async def _select_tool(
        self, state: ConversationState, user_input: str, *, deadline: float | None = None
    ) -> tuple[str | None, dict | None]:
        """Ask the decider which tool to invoke next.

        Returns (tool_name_or_FINAL_ANSWER, audit_record_or_None). Falls back
        to the tactical tier (Gemma classic tool-calling prompt) and then to
        keyword heuristics when the decider tier is unavailable. The
        deterministic floor never repeats a tool it already called this turn
        (keyword matching is stateless — repeating would loop to max rounds).
        """
        def heuristic_floor() -> str:
            already_called = [tc["name"] for tc in state.tool_calls]
            pick = heuristic_select_tool(user_input, _TOOL_MENU)
            if pick is None or pick in already_called:
                return FINAL_ANSWER
            return pick

        if self._decider is None:
            gemma_choice = await self._gemma_select_tool(state, deadline=deadline)
            if gemma_choice is not None:
                return gemma_choice, None
            return heuristic_floor(), None

        state_doc = DeciderBrain.build_state_doc(
            trigger="operator_chat",
            summary="Operator request: " + user_input[:600],
            context={
                "tools_already_called": [tc["name"] for tc in state.tool_calls],
                "vvip_class": state.vvip_class or "unspecified",
            },
        )
        questions = DeciderBrain.tool_selection_question(_TOOL_MENU)

        result = await self._decider.checkpoint(
            movement_id=state.movement_id or state.session_id,
            trigger="operator_chat",
            state_doc=state_doc,
            questions=questions,
            # Chained tool-selection checkpoints inside ONE operator turn are
            # a single operator_chat cadence event (contract F8 targets
            # per-GPS-tick spam, not mid-turn strategy): bypass the 5 s gap
            # once tools have already fired this turn, so the strategic tier
            # — not a fallback Gemma call — decides when enough data is in.
            force=bool(state.tool_calls),
        )

        audit = {
            "tier": result.tier,
            "record": result.audit,
            "alerts": result.operator_alerts,
        }

        if result.tier == "decider":
            answer = result.answers.get("next_tool")
            value = answer.value if answer else None
            value = normalize_tool_name(value)
            if value in _TOOL_MENU or value == FINAL_ANSWER:
                return value, audit
            # Decider chose something off-menu — fall through to tactical tier.
            logger.warning("orchestrator.decider_off_menu", value=value)

        # Decider unavailable/degraded/rate-limited: Gemma classic selection.
        gemma_choice = await self._gemma_select_tool(state, deadline=deadline)
        if gemma_choice is not None:
            return gemma_choice, audit

        return heuristic_floor(), audit

    async def _gemma_select_tool(
        self, state: ConversationState, *, deadline: float | None = None
    ) -> str | None:
        """Tactical-tier tool selection (grammar-constrained JSON menu pick)."""
        prompt = (
            "Given the conversation so far, decide the single next MCP tool to "
            "call for this request. Reply with ONLY a JSON object: "
            '{"tool_calls": [{"name": "<tool>"}]} — or {"tool_calls": []} '
            "when you have enough data for a final answer. "
            "Available tools: " + ", ".join(_TOOL_MENU) + "."
        )
        raw = await self._safe_generate(
            state,
            prompt,
            schema=_tool_selection_schema(),
            schema_name="tool_selection",
            max_tokens=TACTICAL_SELECT_MAX_TOKENS,
            deadline=deadline,
        )
        if raw is None:
            return None
        parsed = extract_json(raw)
        if isinstance(parsed, dict):
            calls = parsed.get("tool_calls") or []
            if isinstance(calls, list) and calls and isinstance(calls[0], dict):
                name = normalize_tool_name(calls[0].get("name"))
                if name in _TOOL_MENU:
                    return name
                if name == FINAL_ANSWER:
                    return FINAL_ANSWER
        return None

    # -- tactical tier: argument drafting ------------------------------------------

    async def _draft_arguments(
        self,
        state: ConversationState,
        tool_name: str,
        user_input: str,
        *,
        deadline: float | None = None,
    ) -> tuple[dict, str]:
        """Gemma drafts arguments for the decider-chosen tool against its
        inputSchema, with one repair round. The generation is
        schema-constrained (llama-server compiles the envelope + inputSchema
        to a GBNF grammar) and capped at TACTICAL_ARGS_MAX_TOKENS, so the
        call cannot ramble past the turn budget. Falls back to deterministic
        defaults."""
        input_schema = _TOOL_SCHEMAS.get(tool_name, {})
        prompt = (
            "Draft the arguments for the MCP tool '" + tool_name + "' to serve the "
            "operator request below. The tool's inputSchema is:\n"
            + json.dumps(input_schema)
            + "\n\nReply with ONLY a JSON object of the shape "
            '{"arguments": {...}} where the inner object satisfies the '
            "schema's required fields exactly. Use only values implied by the "
            "conversation; never invent segment IDs.\n\n"
            "Operator request: " + user_input[:800]
        )

        async def attempt(p: str) -> dict | None:
            raw = await self._safe_generate(
                state,
                p,
                schema=_tool_args_schema(tool_name),
                schema_name="tool_arguments",
                max_tokens=TACTICAL_ARGS_MAX_TOKENS,
                deadline=deadline,
            )
            if raw is None:
                return None
            parsed = extract_json(raw)
            arguments = _coerce_arguments(parsed)
            if _arguments_valid(arguments, input_schema):
                return arguments
            return None

        arguments = await attempt(prompt)
        if arguments is not None:
            return arguments, "gemma"

        # One repair round with the specific failure named. Truncated output
        # (cap hit mid-object) is the most common failure, so the repair also
        # demands compact single-line JSON to cut token count roughly in half.
        missing = _missing_required(arguments or {}, input_schema)
        what = ", ".join(missing) if missing else "schema mismatch"
        repair = (
            prompt + "\n\nYour previous arguments were invalid (missing or "
            "malformed: " + what + "). Reply again with corrected arguments "
            "only, as COMPACT single-line JSON with no whitespace beyond "
            "what the schema requires."
        )
        arguments = await attempt(repair)
        if arguments is not None:
            return arguments, "gemma_repaired"

        logger.warning("orchestrator.heuristic_arguments", tool=tool_name)
        defaults = heuristic_default_arguments(
            tool_name, {"corridor_bbox": _DEFAULT_BBOX}
        )
        return defaults, "heuristic"

    # -- final answer ----------------------------------------------------------------

    async def _final_answer(
        self, state: ConversationState, user_input: str, *, deadline: float | None = None
    ) -> dict:
        """Gemma composes the final structured answer (heuristic floor)."""
        schema = load_schema("orchestrator_final")
        transcript = "\n\n".join(
            "[" + m.role.upper() + ((" " + m.tool_name) if m.tool_name else "") + "]\n" + m.content
            for m in state.messages[-12:]
        )
        prompt = (
            "Produce the final answer to the operator's original request using "
            "the tool results above. Reply with ONLY the required JSON object.\n\n"
            "Original request: " + user_input[:800]
        )

        try:
            raw = await self._safe_generate(
                state,
                prompt,
                schema=schema,
                schema_name="orchestrator_final",
                max_tokens=TACTICAL_FINAL_MAX_TOKENS,
                deadline=deadline,
                plain_history=False,
                extra_messages=[{"role": "user", "content": transcript}],
            )
            if raw is not None:
                parsed = extract_json(raw)
                if isinstance(parsed, dict) and "action" in parsed:
                    state.tiers_used.append("gemma_final")
                    return parsed
        except LlamaUnavailable as exc:
            logger.warning("orchestrator.final_answer_llm_unavailable", error=str(exc))

        state.tiers_used.append("heuristic_final")
        return heuristic_final_answer(
            user_input,
            [tc["name"] for tc in state.tool_calls],
        )

    # -- helpers ----------------------------------------------------------------------

    @staticmethod
    def _deadline_timeout(deadline: float | None) -> float | None:
        """Seconds left before the turn ``deadline`` (None when unbounded).

        Deadlines are wall-clock (``time.time()``) to match the
        ``TOTAL_TURN_TIMEOUT_SEC`` turn budget they are derived from.
        """
        if deadline is None:
            return None
        return deadline - time.time()

    async def _safe_generate(
        self,
        state: ConversationState,
        prompt: str,
        *,
        schema: dict | None = None,
        schema_name: str = "response",
        max_tokens: int | None = None,
        deadline: float | None = None,
        plain_history: bool = True,
        extra_messages: list[dict[str, str]] | None = None,
    ) -> str | None:
        """One tactical call with conversation history; None on any failure.

        The call is bounded by the remaining turn budget (``deadline``): a
        generation that would sail past the budget is cancelled and the tier
        falls through, instead of the old behavior where one in-flight call
        could exceed both the 60 s gate and the client's 120 s timeout.
        """
        history = [
            m for m in state.build_chat_messages() if m["role"] != "system"
        ][-10:] if plain_history else []

        timeout = self._deadline_timeout(deadline)
        if timeout is not None and timeout <= 0:
            logger.warning("orchestrator.llm_turn_budget_exhausted")
            state.errors.append({"type": "llm_error", "error": "turn budget exhausted"})
            return None

        async def _call() -> str:
            if plain_history:
                return await self._bridge.generate(
                    system_prompt=VVIP_AGENT_SYSTEM_PROMPT,
                    user_prompt=prompt,
                    json_mode=True,
                    schema=schema,
                    schema_name=schema_name,
                    max_tokens=max_tokens,
                    extra_messages=history,
                )
            return await self._bridge.generate(
                system_prompt=VVIP_AGENT_SYSTEM_PROMPT,
                user_prompt=prompt,
                json_mode=True,
                schema=schema,
                schema_name=schema_name,
                max_tokens=max_tokens,
                extra_messages=extra_messages,
            )

        try:
            if timeout is None:
                return await _call()
            return await asyncio.wait_for(_call(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "orchestrator.llm_turn_deadline", timeout_sec=round(timeout, 1)
            )
            state.errors.append({
                "type": "llm_error",
                "error": f"tactical call exceeded remaining turn budget ({timeout:.1f}s)",
            })
            return None
        except LlamaUnavailable as exc:
            logger.warning("orchestrator.llm_unavailable", error=str(exc))
            state.errors.append({"type": "llm_error", "error": str(exc)})
            return None

    async def _dispatch_tool_calls(
        self, tool_calls: list[dict], *, deadline: float | None = None
    ) -> list[dict]:
        """Dispatch tool calls with timeout, retry, and error handling."""
        tasks = []
        for i, tc in enumerate(tool_calls):
            call_id = "call_" + str(i) + "_" + str(tc.get("name", "unknown"))
            tasks.append(self._dispatch_single_tool(tc, call_id, deadline=deadline))

        completed = await asyncio.gather(*tasks, return_exceptions=True)

        results = []
        for i, result in enumerate(completed):
            tc = tool_calls[i]
            name = tc.get("name", "unknown")
            call_id = "call_" + str(i) + "_" + name
            if isinstance(result, Exception):
                logger.error("orchestrator.tool_exception", tool=name, error=str(result))
                fail_msg = "Tool '" + name + "' failed: " + str(result)
                results.append({
                    "name": name,
                    "call_id": call_id,
                    "result": {"error": True, "message": fail_msg},
                })
            else:
                results.append(result)
        return results

    async def _dispatch_single_tool(
        self, tool_call: dict, call_id: str, *, deadline: float | None = None
    ) -> dict:
        """Run one MCP tool call — HTTP dispatch to traffic-oracle.

        The per-tool timeout shrinks to the remaining turn budget when that
        is smaller, and retries are skipped entirely once the budget is gone:
        a dead oracle must not push the turn past its deadline.
        """
        name = tool_call.get("name", "")
        arguments = tool_call.get("arguments", {})

        for attempt in range(MAX_RETRIES_PER_TOOL):
            remaining = self._deadline_timeout(deadline)
            if remaining is not None and remaining <= 0:
                break
            call_timeout = (
                min(TOOL_TIMEOUT_SEC, remaining) if remaining is not None else TOOL_TIMEOUT_SEC
            )
            try:
                result = await asyncio.wait_for(
                    self._executor.dispatch(name, arguments),
                    timeout=call_timeout,
                )
                return {"name": name, "call_id": call_id, "result": result}
            except asyncio.TimeoutError:
                if attempt < MAX_RETRIES_PER_TOOL - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                timeout_msg = (
                    "Tool '" + name + "' timed out after "
                    + str(TOOL_TIMEOUT_SEC) + "s"
                )
                return {
                    "name": name,
                    "call_id": call_id,
                    "result": {"error": True, "message": timeout_msg},
                }
            except ToolExecutionError as exc:
                logger.warning(
                    "orchestrator.tool_error", tool=name, attempt=attempt + 1, error=str(exc)
                )
                if attempt < MAX_RETRIES_PER_TOOL - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                return {
                    "name": name,
                    "call_id": call_id,
                    "result": {"error": True, "message": str(exc)},
                }

        return {
            "name": name,
            "call_id": call_id,
            "result": {"error": True, "message": "Max retries exhausted"},
        }

    @staticmethod
    def _error_response(message: str, action: str = "error") -> dict:
        return {
            "action": action,
            "reasoning": message,
            "confidence": "low",
            "tool_calls_made": [],
            "tool_calls": [],
            "data": {},
        }


# ---------------------------------------------------------------------------
# Argument coercion / validation (no jsonschema dependency)
# ---------------------------------------------------------------------------


def _coerce_arguments(parsed: Any) -> dict:
    if isinstance(parsed, dict):
        if "arguments" in parsed and isinstance(parsed["arguments"], dict):
            return parsed["arguments"]
        if "name" in parsed and "arguments" not in parsed:
            # model echoed the whole call envelope; treat rest as args minus name
            return {k: v for k, v in parsed.items() if k != "name"}
        return parsed
    return {}


def _missing_required(arguments: dict, input_schema: dict) -> list[str]:
    required = input_schema.get("required", []) if isinstance(input_schema, dict) else []
    return [key for key in required if key not in arguments]


def _arguments_valid(arguments: dict, input_schema: dict) -> bool:
    """Required-keys-present + loose type check against the tool inputSchema."""
    if not isinstance(arguments, dict):
        return False
    if not isinstance(input_schema, dict):
        return bool(arguments)
    if _missing_required(arguments, input_schema):
        return False

    properties = input_schema.get("properties", {})
    type_map = {
        "integer": int,
        "number": (int, float),
        "string": str,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    for key, value in arguments.items():
        spec = properties.get(key)
        if spec is None:
            continue
        expected = spec.get("type")
        expected_type = type_map.get(expected)
        if expected_type is not None and not isinstance(value, expected_type):
            return False
    return True
