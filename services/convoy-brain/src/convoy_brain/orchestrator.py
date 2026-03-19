"""Agentic Host Orchestrator — routes requests between Ollama and MCP servers.

This is the central control loop that:
1. Manages multi-turn conversation state
2. Sends user/system prompts to Qwen via OllamaBridge
3. Parses tool-call requests from LLM responses
4. Dispatches tool calls to the MCP server's ToolExecutor
5. Feeds tool results back to the LLM for integration
6. Implements guardrails: JSON validation, retry logic, fallback strategies
7. Maintains convoy context across interaction turns via ConvoyContextStore
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from convoy_brain.mcp.prompts import TOOL_CALLING_INSTRUCTION, VVIP_AGENT_SYSTEM_PROMPT
from convoy_brain.mcp.server import MCPServer, ToolExecutionError, ToolExecutor
from convoy_brain.memory.convoy_context import ConvoyContext, ConvoyContextStore
from convoy_brain.ollama_bridge import OllamaBridge

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_TOOL_ROUNDS = 6          # Max LLM→tool→LLM loops before forced termination
MAX_RETRIES_PER_TOOL = 2     # Max retries for a single failed tool call
TOOL_TIMEOUT_SEC = 30.0      # Per-tool execution timeout
TOTAL_TURN_TIMEOUT_SEC = 120.0  # Max time for a complete orchestration turn


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------

@dataclass
class Message:
    """A single message in the conversation history."""
    role: str           # "system" | "user" | "assistant" | "tool"
    content: str        # text content
    tool_name: str | None = None       # for role="tool", which tool produced this
    tool_call_id: str | None = None    # correlation ID for tool call → result


@dataclass
class ConversationState:
    """Multi-turn conversation state for a single orchestration session."""
    session_id: str
    movement_id: str | None = None
    vvip_class: str | None = None
    messages: list[Message] = field(default_factory=list)
    tool_calls_total: int = 0
    errors: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        self.messages.append(Message(role=role, content=content, **kwargs))

    def build_ollama_messages(self) -> list[dict[str, str]]:
        """Convert conversation to Ollama chat format."""
        result = []
        for msg in self.messages:
            if msg.role == "tool":
                # Tool results are injected as user messages with tool context
                result.append({
                    "role": "user",
                    "content": f"[Tool Result: {msg.tool_name}]\n{msg.content}",
                })
            else:
                result.append({"role": msg.role, "content": msg.content})
        return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """Agentic host binding Ollama LLM to MCP tool ecosystem.

    Implements the ReAct (Reasoning + Acting) pattern:
    1. LLM reasons about the request
    2. LLM emits tool_calls if it needs data
    3. Orchestrator executes tools via MCP ToolExecutor
    4. Tool results fed back to LLM
    5. Repeat until LLM produces final answer or max rounds exceeded
    """

    def __init__(
        self,
        bridge: OllamaBridge,
        tool_executor: ToolExecutor,
        context_store: ConvoyContextStore,
    ) -> None:
        self._bridge = bridge
        self._executor = tool_executor
        self._context_store = context_store
        self._active_sessions: dict[str, ConversationState] = {}
        logger.info("orchestrator.init")

    async def create_session(
        self,
        session_id: str,
        movement_id: str | None = None,
        vvip_class: str | None = None,
    ) -> ConversationState:
        """Initialize a new orchestration session."""
        state = ConversationState(
            session_id=session_id,
            movement_id=movement_id,
            vvip_class=vvip_class,
        )

        # Inject system prompt
        system_prompt = VVIP_AGENT_SYSTEM_PROMPT + "\n\n" + TOOL_CALLING_INSTRUCTION

        # If we have a movement context, inject it
        if movement_id:
            ctx = await self._context_store.get(movement_id)
            if ctx:
                system_prompt += (
                    f"\n\n## Active Movement Context\n"
                    f"Movement ID: {ctx.movement_id}\n"
                    f"VVIP Class: {ctx.vvip_class}\n"
                    f"Status: {ctx.status}\n"
                    f"Selected Route: {ctx.selected_route_id or 'None'}\n"
                    f"Convoy Position: {ctx.convoy_position or 'Not deployed'}\n"
                    f"Active Diversions: {len(ctx.active_diversions)}\n"
                )

        # Inject VVIP security protocol if class specified
        if vvip_class:
            protocol_messages = self._executor.get_prompt(
                "vvip_security_protocol", {"vvip_class": vvip_class}
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

    async def process_turn(
        self,
        session_id: str,
        user_input: str,
    ) -> dict:
        """Process a single user turn through the full ReAct loop.

        Returns the final structured response from the LLM after all
        tool interactions are complete.
        """
        state = self._active_sessions.get(session_id)
        if state is None:
            raise ValueError(f"No active session: {session_id}")

        state.add_message("user", user_input)

        turn_start = time.time()
        tool_round = 0
        final_response: dict | None = None

        logger.info(
            "orchestrator.turn.start",
            session_id=session_id,
            user_input_len=len(user_input),
        )

        while tool_round < MAX_TOOL_ROUNDS:
            # Check total turn timeout
            elapsed = time.time() - turn_start
            if elapsed > TOTAL_TURN_TIMEOUT_SEC:
                logger.warning("orchestrator.turn.timeout", elapsed_sec=elapsed)
                final_response = {
                    "action": "timeout",
                    "reasoning": (
                        f"Processing exceeded {TOTAL_TURN_TIMEOUT_SEC}s timeout after "
                        f"{tool_round} tool rounds. Returning partial results."
                    ),
                    "confidence": "low",
                    "tool_calls_made": [],
                    "data": {},
                }
                break

            # Call LLM
            llm_response = await self._call_llm(state)
            if llm_response is None:
                final_response = self._error_response("LLM returned empty response")
                break

            state.add_message("assistant", llm_response)

            # Parse response — does it contain tool calls?
            parsed = self._parse_llm_response(llm_response)

            if parsed is None:
                # Unparseable response — attempt recovery
                recovery_response = await self._recover_from_bad_json(state, llm_response)
                if recovery_response is not None:
                    final_response = recovery_response
                else:
                    final_response = {
                        "action": "parse_error",
                        "reasoning": "LLM produced invalid JSON that could not be recovered.",
                        "confidence": "low",
                        "raw_response": llm_response[:500],
                        "data": {},
                    }
                break

            tool_calls = parsed.get("tool_calls")
            if not tool_calls:
                # No tool calls — this is the final answer
                final_response = parsed
                break

            # Execute tool calls
            tool_round += 1
            logger.info(
                "orchestrator.tool_round",
                round=tool_round,
                num_tools=len(tool_calls),
            )

            tool_results = await self._execute_tool_calls(tool_calls)
            state.tool_calls_total += len(tool_calls)

            # Feed results back to conversation
            for result in tool_results:
                state.add_message(
                    "tool",
                    json.dumps(result["result"], default=str),
                    tool_name=result["name"],
                    tool_call_id=result.get("call_id"),
                )

        if final_response is None:
            final_response = {
                "action": "max_rounds_exceeded",
                "reasoning": f"Reached maximum {MAX_TOOL_ROUNDS} tool interaction rounds.",
                "confidence": "low",
                "data": {},
            }

        # Persist decision to convoy context if movement is active
        if state.movement_id:
            await self._context_store.append_decision(
                state.movement_id,
                {
                    "session_id": session_id,
                    "action": final_response.get("action"),
                    "confidence": final_response.get("confidence"),
                    "tool_calls_total": state.tool_calls_total,
                    "turn_duration_sec": round(time.time() - turn_start, 2),
                },
            )

        logger.info(
            "orchestrator.turn.complete",
            session_id=session_id,
            tool_rounds=tool_round,
            tool_calls_total=state.tool_calls_total,
            duration_sec=round(time.time() - turn_start, 2),
        )

        return final_response

    # -- LLM interaction ---------------------------------------------------

    async def _call_llm(self, state: ConversationState) -> str | None:
        """Call Ollama via the bridge with the full conversation history."""
        messages = state.build_ollama_messages()

        # Separate system message from the rest
        system_prompt = ""
        user_messages_text = ""
        for msg in messages:
            if msg["role"] == "system":
                system_prompt = msg["content"]
            else:
                user_messages_text += f"[{msg['role'].upper()}]\n{msg['content']}\n\n"

        try:
            response = await self._bridge.generate(
                system_prompt=system_prompt,
                user_prompt=user_messages_text.strip(),
                json_mode=True,
            )
            return response if response else None
        except Exception as exc:
            logger.error("orchestrator.llm_call_failed", error=str(exc))
            state.errors.append({"type": "llm_error", "error": str(exc)})
            return None

    # -- Response parsing --------------------------------------------------

    def _parse_llm_response(self, raw: str) -> dict | None:
        """Parse the LLM's JSON response, handling common malformations."""
        # Strip markdown code fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Attempt to extract JSON from mixed text
        brace_start = cleaned.find("{")
        brace_end = cleaned.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            try:
                return json.loads(cleaned[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                pass

        logger.warning("orchestrator.parse_failed", raw_len=len(raw), raw_prefix=raw[:100])
        return None

    async def _recover_from_bad_json(
        self, state: ConversationState, bad_response: str,
    ) -> dict | None:
        """Attempt to get the LLM to fix a malformed JSON response."""
        recovery_prompt = (
            "Your previous response was not valid JSON. "
            "Please reformat your response as a single valid JSON object. "
            "Do NOT include markdown code fences or explanatory text."
        )
        state.add_message("user", recovery_prompt)

        retry_response = await self._call_llm(state)
        if retry_response:
            state.add_message("assistant", retry_response)
            parsed = self._parse_llm_response(retry_response)
            if parsed is not None:
                return parsed

        return None

    # -- Tool execution ----------------------------------------------------

    async def _execute_tool_calls(self, tool_calls: list[dict]) -> list[dict]:
        """Execute a batch of tool calls with timeout, retry, and error handling."""
        results = []

        # Run independent tool calls concurrently
        tasks = []
        for i, tc in enumerate(tool_calls):
            call_id = f"call_{i}_{tc.get('name', 'unknown')}"
            tasks.append(self._execute_single_tool(tc, call_id))

        completed = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(completed):
            tc = tool_calls[i]
            name = tc.get("name", "unknown")
            call_id = f"call_{i}_{name}"

            if isinstance(result, Exception):
                logger.error("orchestrator.tool_exception", tool=name, error=str(result))
                results.append({
                    "name": name,
                    "call_id": call_id,
                    "result": {
                        "error": True,
                        "message": f"Tool '{name}' failed: {result}",
                    },
                })
            else:
                results.append(result)

        return results

    async def _execute_single_tool(self, tool_call: dict, call_id: str) -> dict:
        """Execute a single tool call with retry logic."""
        name = tool_call.get("name", "")
        arguments = tool_call.get("arguments", {})

        for attempt in range(MAX_RETRIES_PER_TOOL):
            try:
                result = await asyncio.wait_for(
                    self._executor.execute(name, arguments),
                    timeout=TOOL_TIMEOUT_SEC,
                )
                return {"name": name, "call_id": call_id, "result": result}

            except asyncio.TimeoutError:
                logger.warning(
                    "orchestrator.tool_timeout",
                    tool=name,
                    attempt=attempt + 1,
                    timeout_sec=TOOL_TIMEOUT_SEC,
                )
                if attempt < MAX_RETRIES_PER_TOOL - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))  # backoff
                    continue
                return {
                    "name": name,
                    "call_id": call_id,
                    "result": {
                        "error": True,
                        "message": f"Tool '{name}' timed out after {TOOL_TIMEOUT_SEC}s "
                                   f"({attempt + 1} attempts)",
                    },
                }

            except ToolExecutionError as exc:
                logger.warning(
                    "orchestrator.tool_error",
                    tool=name,
                    attempt=attempt + 1,
                    error=str(exc),
                )
                if attempt < MAX_RETRIES_PER_TOOL - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                return {
                    "name": name,
                    "call_id": call_id,
                    "result": {
                        "error": True,
                        "message": str(exc),
                    },
                }

        # Should not reach here, but safeguard
        return {
            "name": name,
            "call_id": call_id,
            "result": {"error": True, "message": "Max retries exhausted"},
        }

    # -- Session management ------------------------------------------------

    def get_session(self, session_id: str) -> ConversationState | None:
        return self._active_sessions.get(session_id)

    def end_session(self, session_id: str) -> None:
        self._active_sessions.pop(session_id, None)
        logger.info("orchestrator.session_ended", session_id=session_id)

    # -- Helpers -----------------------------------------------------------

    @staticmethod
    def _error_response(message: str) -> dict:
        return {
            "action": "error",
            "reasoning": message,
            "confidence": "low",
            "tool_calls_made": [],
            "data": {},
        }
