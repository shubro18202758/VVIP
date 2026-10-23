"""Two-tier Orchestrator — decider (strategic) + Gemma (tactical) + heuristics.

Hierarchy of every turn:

    STRATEGIC  pplx-decider-v1-27b picks the next TOOL via a typed choice
               question (or 'final_answer'), with a confidence score. A pick
               below the confidence floor is acted on only when the tactical
               tier independently agrees (disagreement prefers Gemma and is
               audited as an operator alert).
    TACTICAL   Gemma 4 12B drafts the tool ARGUMENTS against the tool's
               inputSchema (one repair round on validation failure) and later
               composes the final structured answer.
    FLOOR      deterministic heuristics — keyword tool selection, default
               arguments, and a degraded final answer built from the tool
               results' deterministic summaries.

Fallback chain (the ask): decider -> Gemma -> deterministic heuristics.
Whenever a tier is slow or unavailable the turn falls through to the next and
the drop is audit-logged into the movement's decisions_log together with the
decider's {value, probability, confidence} distribution when it ran.

Streaming (contract C-D): :meth:`Orchestrator.process_turn` takes an optional
``on_event`` async callback and reports the turn as it happens — ``thought``
events for every tier decision (decider / Gemma / heuristic), ``tool_call`` /
``tool_result`` around each dispatch. The caller streams the final answer's
PROSE (:func:`final_prose`), never the raw JSON envelope. /chat/stream is a
thin NDJSON adapter over this one code path, so the deck's chat gets the
strategic tier, argument validation and the decisions_log audit.

Sessions persist for SESSION_TTL_SEC after their last turn (a follow-up
question keeps its context); turns on one session are serialized. A session
may be GROUNDED in the operator's selection (contract C-D2): ``event_id``
(the selected corridor event — resolved once through GET /events/active and
stated in the session context with its kind, status, title and spines) and
``context_urn`` (the entity a dossier's "Explain this" is about — resolved
through city_get_dossier). Both are part of the session-reuse key.

Agentic God's-eye (C-D2): a canvas_* tool the tiers choose returns a
DIRECTIVE, streamed as ``{type:'directive', data:{id, verb, args, label,
provenance:{session_id, tier, confidence, tool_call_id}}}``. A deterministic
post-step makes the map answer independent of the decider's whim: a
show / where / locate / track / fly / highlight request whose tool results
resolved entity URNs gets the matching directive (tier 'heuristic'); a map
request that names a kind ('the stolen vehicle', 'units', a plate) is
grounded first with one read-only city_query_entities call. Every directive
is a proposal — the operator applies or dismisses it on the canvas, and the
acknowledgement is audited (POST /chat/directives/ack). ``done.citations``
lists the URNs the turn's tool results named, labels plate-masked.

Prompt budget: llama-server runs 2 slots over a 16384-token context, i.e.
8192 tokens per call. Every tactical prompt is assembled under
PROMPT_TOKEN_BUDGET estimated tokens (system prompt + budgeted history +
task), and the final answer's transcript is the current request plus this
turn's tool results only — never the system prompt a second time (that
duplication overflowed the slot and produced false "both tiers unavailable"
answers).

This module performs no database access of any kind; ToolExecutor dispatch
routes MCP tool calls to the traffic-oracle service over HTTP.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from convoy_brain.config import (
    CONFIDENCE_FLOOR,
    DECIDER_MODEL_ID,
    GEMMA_MODEL_ID,
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
    summarize_tool_result,
)
from convoy_brain.llm.llama_bridge import LlamaBridge, LlamaUnavailable
from convoy_brain.llm.parsing import extract_json, rescue_string_field, truncate_for_context
from convoy_brain.llm.schemas import load_schema
from convoy_brain.mcp.prompts import TOOL_CALLING_INSTRUCTION, VVIP_AGENT_SYSTEM_PROMPT
from convoy_brain.mcp.server import (
    CANVAS_VERBS,
    TOOL_ALIASES,
    TOOL_DEFINITIONS,
    ToolExecutionError,
    ToolExecutor,
    canonical_urn,
    mask_plates,
    parse_urn,
)
from convoy_brain.memory.convoy_context import ConvoyContextStore
from convoy_brain.protocol_params_gen import ADVANCE_CLOSURE_FLOOR_SEC, MIN_LANES

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_TOOL_ROUNDS = 6          # Max tier loops before forced termination
MAX_RETRIES_PER_TOOL = 2     # Max attempts for a single retryable tool failure
TOOL_TIMEOUT_SEC = 30.0      # Per-tool execution timeout
TOTAL_TURN_TIMEOUT_SEC = 120.0  # Max time for a complete orchestration turn

SESSION_TTL_SEC = 900.0      # Sessions live 15 min after their last turn
MAX_SESSIONS = 256           # LRU bound on concurrently held sessions

#: Estimated-token ceiling for one tactical prompt (system + history + task).
#: A llama-server slot holds 8192 tokens (16384 context / 2 slots); the
#: estimate is conservative, so ~6k estimated leaves room for the output cap.
PROMPT_TOKEN_BUDGET = 6000
#: tool_result events carry the raw result only below this many characters.
TOOL_EVENT_RESULT_MAX_CHARS = 8000

_TOOL_MENU = [t["name"] for t in TOOL_DEFINITIONS]
_TOOL_SCHEMAS = {t["name"]: t.get("inputSchema", {}) for t in TOOL_DEFINITIONS}

# Event-agnostic alias names (contract C2): generalized names ship as
# advertised aliases of the canonical tools. Any model tier (or the
# heuristic floor) may answer with an alias; normalization maps it onto
# the canonical wire name before dispatch. Single source: mcp/server.py.
_TOOL_ALIASES: dict[str, str] = dict(TOOL_ALIASES)

FINAL_ANSWER = "final_answer"

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

#: Directives issued and not yet acknowledged are kept this long (the deck
#: expires its own queue after 10 min) and at most this many.
DIRECTIVE_TTL_SEC = 1800.0
MAX_TRACKED_DIRECTIVES = 1024
DIRECTIVE_DECISIONS = ("applied", "dismissed")
#: done.citations entries per turn.
MAX_CITATIONS = 8

#: A map request: the operator wants something SHOWN on the God's-eye canvas.
_MAP_INTENT_RE = re.compile(
    r"\b(show|where|locate|track|follow|fly|highlight|point out|zoom|on the map|on the canvas)\b",
    re.IGNORECASE,
)
_TRACK_RE = re.compile(r"\b(track|follow)\b", re.IGNORECASE)
_HIGHLIGHT_RE = re.compile(r"\b(highlight|all|every)\b", re.IGNORECASE)
#: "this / it / here" — the session's context entity is the subject.
_DEICTIC_RE = re.compile(r"\b(this|it|here|that one)\b", re.IGNORECASE)
_PLURAL_EVENTS_RE = re.compile(r"\b(all|every|which|list|how many|other)\b", re.IGNORECASE)
_STOLEN_RE = re.compile(r"\bstolen\b", re.IGNORECASE)
#: An Indian registration mark typed in a message (any case, any spacing).
_PLATE_RE = re.compile(r"\b([A-Za-z]{2}[ -]?\d{1,2}[ -]?(?:[A-Za-z]{1,3}[ -]?)?\d{4})\b")
#: Entity kinds a message names, in the order a map request is grounded.
_KIND_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("vehicle", re.compile(
        r"\b(vehicles?|cars?|plates?|stolen|earmark\w*|suspect|watchlist)\b", re.I)),
    ("convoy", re.compile(r"\bconvoys?\b", re.I)),
    ("unit", re.compile(r"\b(units?|patrols?|jan rakshak|ambulances?|fire tenders?)\b", re.I)),
    ("camera", re.compile(r"\b(cameras?|anpr|cctv)\b", re.I)),
    ("event", re.compile(r"\b(pursuits?|events?|processions?|gatherings?|movements?)\b", re.I)),
    ("junction", re.compile(r"\b(junctions?|intersections?|chokes?)\b", re.I)),
    ("dispatch", re.compile(r"\bdispatch\w*\b", re.I)),
    ("recommendation", re.compile(r"\b(e-?challans?|recommendations?|fines?)\b", re.I)),
    ("anomaly", re.compile(r"\banomal\w*\b", re.I)),
)
#: Entities that move: a map request about one follows it on the canvas.
_TRACKABLE_KINDS = frozenset({"vehicle", "unit", "convoy"})
#: Tools whose ``event_id`` the session's selected event scopes when the
#: drafted arguments name none.
_EVENT_SCOPED_TOOLS = frozenset({
    "get_active_corridor_events", "get_interception_plan", "list_dispatches", "get_situation_log",
})

#: Canonical protocol floors (contract C1 single source) placed next to the
#: facts in every final-answer transcript: the tactical model misread the
#: prose table in the system prompt ("4 lanes meets Z+"), and these numbers
#: decide protection questions.
_PROTOCOL_FLOORS = (
    "[PROTOCOL FLOORS — authoritative]\nminimum lanes: "
    + ", ".join(f"{cls} {lanes}" for cls, lanes in MIN_LANES.items())
    + "; advance closure (s): "
    + ", ".join(
        f"{cls} {floor if floor is not None else 0}"
        for cls, floor in ADVANCE_CLOSURE_FLOOR_SEC.items()
    )
)

EventSink = Callable[[str, Any], Awaitable[None]]


def normalize_tool_name(name: str | None) -> str | None:
    """Map an alias tool name onto its canonical wire name (identity otherwise)."""
    if not isinstance(name, str):
        return name
    return _TOOL_ALIASES.get(name, name)


def estimate_prompt_tokens(text: str) -> int:
    """Conservative Gemma token estimate for budgeting prompts.

    Calibrated 2026-10-22 against llama-server /tokenize on this stack's own
    text: English prose ≈ 4.1 chars/token, tool-calling markdown ≈ 3.3, JSON
    tool results ≈ 1.9 (digits and punctuation tokenize ~1:1). Letters/4 +
    digits-and-punctuation/1 + whitespace/6 over-estimated every sample by
    6-55 % and never under-estimated — the safe side for an 8192-token slot.
    """
    letters = spaces = other = 0
    for ch in text:
        if ch.isalpha():
            letters += 1
        elif ch.isspace():
            spaces += 1
        else:
            other += 1
    return int(letters / 4 + other + spaces / 6) + 1


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

#: Keys stripped from tool results before they enter model context: bulky
#: geometry the models cannot use, which tokenizes at ~2 chars/token.
_CONTEXT_DROP_KEYS = frozenset({"geometry", "geojson", "zone_geojson", "coordinates"})
_CONTEXT_MAX_LIST = 30


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
    """Multi-turn conversation state for a single orchestration session.

    ``tool_calls`` / ``tiers_used`` / ``operator_alerts`` / ``errors`` /
    ``tiers`` / ``degraded`` / ``tool_results`` describe the CURRENT turn
    (reset when a turn starts); ``messages`` is the session history.
    """

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
    # Session-level prompt material (see Orchestrator.create_session).
    system_prompt: str = ""          # VVIP prompt + tool-calling block + context
    answer_system_prompt: str = ""   # VVIP prompt + context (final answers)
    requested_movement_id: str | None = None
    last_used: float = field(default_factory=time.monotonic)
    turns: int = 0
    # Per-turn contract view (C-D done.data).
    tiers: list[str] = field(default_factory=list)
    degraded: bool = False
    tool_results: list[dict] = field(default_factory=list)
    turn_start_index: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # Operator grounding (C-D2): the selected corridor event and the entity
    # a dossier asked about, as requested and as resolved.
    event_id: str | None = None
    context_urn: str | None = None
    requested_event_id: str | None = None
    requested_context_urn: str | None = None
    event_brief: str | None = None
    context_brief: str | None = None
    #: Entities the session context resolved (urn -> facts), for directives.
    session_entities: dict[str, dict] = field(default_factory=dict)
    # Per-turn agentic view: entities the tool results named, directives
    # streamed, the tier/confidence of the newest pick, and auxiliary
    # low-confidence readouts kept out of the operator alerts.
    turn_entities: dict[str, dict] = field(default_factory=dict)
    directives: list[dict] = field(default_factory=list)
    last_pick: dict = field(default_factory=dict)
    aux_low_confidence: list[dict] = field(default_factory=list)

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


class _TurnEvents:
    """Per-turn emitter for the optional streaming callback (contract C-D).

    The stream is an observer: a failing sink is logged and dropped, never
    allowed to break the turn or its audit row.
    """

    def __init__(self, sink: EventSink | None) -> None:
        self._sink = sink
        self._step = 0

    async def emit(self, kind: str, data: Any) -> None:
        if self._sink is None:
            return
        try:
            await self._sink(kind, data)
        except Exception as exc:
            logger.warning("orchestrator.event_sink_failed", error=str(exc)[:200])
            self._sink = None

    async def thought(self, text: str, tier: str) -> None:
        self._step += 1
        await self.emit("thought", {"stepIndex": self._step, "text": text, "tier": tier})

    async def tool_call(self, call_id: str, name: str, args: dict) -> None:
        # Contract keys (id/name/args) plus the deck parser's legacy keys
        # (callId/toolName/arguments/state) — one event, both readers.
        await self.emit("tool_call", {
            "id": call_id, "name": name, "args": args,
            "callId": call_id, "toolName": name, "arguments": args, "state": "running",
        })

    async def directive(self, directive: dict) -> None:
        await self.emit("directive", directive)

    async def tool_result(
        self,
        call_id: str,
        name: str,
        ok: bool,
        summary: str,
        result: Any,
        duration_ms: int,
    ) -> None:
        data: dict[str, Any] = {
            "id": call_id, "name": name, "ok": ok, "summary": summary,
            "callId": call_id, "state": "success" if ok else "error",
            "durationMs": duration_ms,
        }
        if len(json.dumps(result, default=str)) <= TOOL_EVENT_RESULT_MAX_CHARS:
            data["result"] = result
        else:
            data["result_truncated"] = True
        await self.emit("tool_result", data)


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
        #: Directives issued (id -> record) awaiting the operator's ack.
        self._directives: OrderedDict[str, dict] = OrderedDict()
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
        *,
        event_id: str | None = None,
        context_urn: str | None = None,
    ) -> ConversationState:
        """Initialize a new orchestration session with the system prompt.

        A movement id binds the session only when that movement exists: the
        system prompt then carries its context and every turn is audited into
        its decisions_log. An unknown id is not bound (auditing into it would
        mint an empty "unknown" context in the store).

        ``event_id`` (the operator's selected corridor event) is resolved once
        through get_active_corridor_events and stated in the context —
        'Selected corridor event: <id> (<kind>, <status>, <title>), spines
        <label: ids>'. ``context_urn`` is resolved through city_get_dossier.
        Either failing leaves an honest line instead of a guess.
        """
        self._evict_expired()
        state = ConversationState(
            session_id=session_id,
            movement_id=None,
            vvip_class=vvip_class,
            requested_movement_id=movement_id,
            requested_event_id=event_id,
            requested_context_urn=context_urn,
        )

        context_prompt = ""
        if movement_id:
            ctx = await self._context_store.get(movement_id)
            if ctx:
                state.movement_id = movement_id
                context_prompt += (
                    "\n\n## Active Movement Context\n"
                    "Movement ID: " + str(ctx.movement_id) + "\n"
                    "VVIP Class: " + str(ctx.vvip_class) + "\n"
                    "Status: " + str(ctx.status) + "\n"
                    "Selected Route: " + str(ctx.selected_route_id or "None") + "\n"
                    "Convoy Position: " + str(ctx.convoy_position or "Not deployed") + "\n"
                    "Active Diversions: " + str(len(ctx.active_diversions)) + "\n"
                )
                plan_row = next(
                    (row for row in reversed(ctx.decisions_log)
                     if isinstance(row, dict) and row.get("phase") == "pre_movement"),
                    None,
                )
                if plan_row and plan_row.get("primary_segment_ids"):
                    # The plan's own segment list: follow-up questions about
                    # "this route" use real ids instead of guessing.
                    context_prompt += (
                        "Selected Route Segments: "
                        + json.dumps(plan_row["primary_segment_ids"]) + "\n"
                        + (
                            "Selected Route Roads: "
                            + " → ".join(plan_row.get("primary_road_names") or []) + "\n"
                            if plan_row.get("primary_road_names") else ""
                        )
                        + "Plan Status: " + str(plan_row.get("recommendation")) + "\n"
                        + "Registered Corridor Event: " + str(plan_row.get("event_id")) + "\n"
                    )
                state.vvip_class = state.vvip_class or ctx.vvip_class
            else:
                logger.warning(
                    "orchestrator.unknown_movement_not_bound", movement_id=movement_id
                )

        context_prompt += await self._ground_event(state, event_id)
        context_prompt += await self._ground_context_urn(state, context_urn)

        if state.vvip_class:
            protocol_messages = self._executor.get_prompt(
                "vvip_security_protocol", {"vvip_class": state.vvip_class}
            )
            for pm in protocol_messages:
                content = pm.get("content", {})
                if isinstance(content, dict):
                    context_prompt += "\n\n" + content.get("text", "")
                elif isinstance(content, str):
                    context_prompt += "\n\n" + content

        state.system_prompt = (
            VVIP_AGENT_SYSTEM_PROMPT + "\n\n" + TOOL_CALLING_INSTRUCTION + context_prompt
        )
        state.answer_system_prompt = VVIP_AGENT_SYSTEM_PROMPT + context_prompt
        state.add_message("system", state.system_prompt)
        self._active_sessions[session_id] = state
        while len(self._active_sessions) > MAX_SESSIONS:
            oldest = min(self._active_sessions.values(), key=lambda s: s.last_used)
            self._active_sessions.pop(oldest.session_id, None)

        logger.info(
            "orchestrator.session_created",
            session_id=session_id,
            movement_id=state.movement_id,
            vvip_class=vvip_class,
            event_id=state.event_id,
            context_urn=state.context_urn,
        )
        return state

    async def _ground_event(self, state: ConversationState, event_id: str | None) -> str:
        """Resolve the selected corridor event once; its context line ('' when none)."""
        if not event_id:
            return ""
        try:
            event_id = str(uuid.UUID(str(event_id)))
        except ValueError:
            return (
                f"\n\nSelected corridor event: {str(event_id)[:64]} "
                "(not an event id — ignored)\n"
            )
        state.event_id = event_id
        try:
            payload = await self._executor.dispatch(
                "get_active_corridor_events", {"event_id": event_id})
        except Exception as exc:  # grounding is best effort: say so, never guess
            logger.warning("orchestrator.event_grounding_failed",
                           event_id=event_id, error=str(exc)[:200])
            state.event_brief = f"{event_id} (could not be resolved: the board did not answer)"
            return f"\n\nSelected corridor event: {state.event_brief}\n"
        rows = payload.get("events") if isinstance(payload, dict) else None
        event = next(
            (e for e in rows or [] if isinstance(e, dict) and str(e.get("event_id")) == event_id),
            None,
        )
        if event is None:
            state.event_brief = f"{event_id} (not on the board — completed, cancelled or unknown)"
            return f"\n\nSelected corridor event: {state.event_brief}\n"
        spines = "; ".join(
            f"{r.get('label')}: {', '.join(str(i) for i in (r.get('segment_ids') or [])[:24])}"
            for r in event.get("routes") or [] if isinstance(r, dict)
        ) or "none"
        state.event_brief = (
            f"{event_id} ({event.get('event_kind')}, {event.get('status')}, "
            f"{mask_plates(event.get('title'))}), spines {spines}"
        )
        urn = f"event:{event_id}"
        state.session_entities[urn] = {
            "urn": urn, "kind": "event", "label": mask_plates(event.get("title")) or urn,
            "state": event.get("status"), "call_id": None,
        }
        if event.get("vehicle_urn"):
            state.session_entities[event["vehicle_urn"]] = {
                "urn": event["vehicle_urn"], "kind": "vehicle",
                "label": event.get("vehicle_label") or event["vehicle_urn"],
                "call_id": None, "linked_to": urn,
            }
        return (
            f"\n\nSelected corridor event: {state.event_brief}\n"
            "Questions about 'the corridor', 'this event' or 'the pursuit' are about "
            "this event unless the operator names another.\n"
        )

    async def _ground_context_urn(self, state: ConversationState, context_urn: str | None) -> str:
        """Resolve the dossier entity the operator asked about ('' when none)."""
        if not context_urn:
            return ""
        urn = canonical_urn(context_urn)
        if urn is None:
            return f"\n\nContext entity: {str(context_urn)[:80]} (not a valid URN — ignored)\n"
        state.context_urn = urn
        try:
            payload = await self._executor.dispatch("city_get_dossier", {"urn": urn})
        except Exception as exc:
            logger.warning("orchestrator.context_grounding_failed", urn=urn, error=str(exc)[:200])
            state.context_brief = f"{urn} (the dossier could not be read)"
            return f"\n\nContext entity: {state.context_brief}\n"
        entity = payload.get("entity") if isinstance(payload, dict) else None
        if not isinstance(entity, dict):
            detail = (payload or {}).get("detail") or "not in the city picture"
            state.context_brief = f"{urn} ({detail})"
            return f"\n\nContext entity: {state.context_brief}\n"
        facts = ", ".join(
            f"{k} {v}" for k, v in (entity.get("props") or {}).items()
            if not isinstance(v, (list, dict))
        )
        related = _related_summary(payload.get("related") or [], payload.get("related_total"))
        audit = "; ".join(
            f"{a.get('actor') or a.get('actor_kind')}: {a.get('text')}"
            for a in (payload.get("audit") or [])[:4]
        )
        state.context_brief = (
            f"{urn} — {entity.get('label')} ({entity.get('kind')}, {entity.get('state')}, "
            f"source {entity.get('source')})"
        )
        state.session_entities[urn] = dict(entity, call_id=None)
        for row in payload.get("related") or []:
            if isinstance(row, dict) and parse_urn(row.get("urn")):
                state.session_entities.setdefault(row["urn"], dict(row, call_id=None))
        return (
            f"\n\nContext entity (the operator is asking about it): {state.context_brief}\n"
            + (f"Facts: {facts}\n" if facts else "")
            + (f"Related: {related}\n" if related else "")
            + (f"Recent audit: {audit}\n" if audit else "")
        )

    async def open_session(
        self,
        session_id: str | None = None,
        movement_id: str | None = None,
        vvip_class: str | None = None,
        *,
        event_id: str | None = None,
        context_urn: str | None = None,
    ) -> ConversationState:
        """Resume a live session or start one (contract C-D session continuity).

        The session is reused when it exists, has not expired, and the request
        does not switch it to a different movement, selected event or context
        entity; otherwise a new session starts — under the caller's id when it
        is a well-formed one, else a fresh uuid (``done.data.session_id`` tells
        the client which).
        """
        self._evict_expired()
        if session_id:
            state = self._active_sessions.get(session_id)
            if state is not None and (
                (not movement_id or movement_id == state.requested_movement_id)
                and (not event_id or event_id == state.requested_event_id)
                and (not context_urn or context_urn == state.requested_context_urn)
            ):
                state.last_used = time.monotonic()
                if vvip_class and not state.vvip_class:
                    state.vvip_class = vvip_class
                return state
        new_id = (
            session_id
            if session_id and _SESSION_ID_RE.match(session_id)
            and session_id not in self._active_sessions
            else str(uuid.uuid4())
        )
        return await self.create_session(
            new_id, movement_id=movement_id, vvip_class=vvip_class,
            event_id=event_id, context_urn=context_urn,
        )

    def get_session(self, session_id: str) -> ConversationState | None:
        return self._active_sessions.get(session_id)

    def end_session(self, session_id: str) -> None:
        self._active_sessions.pop(session_id, None)
        logger.info("orchestrator.session_ended", session_id=session_id)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [
            sid for sid, state in self._active_sessions.items()
            if now - state.last_used > SESSION_TTL_SEC and not state.lock.locked()
        ]
        for sid in expired:
            self._active_sessions.pop(sid, None)
        if expired:
            logger.info("orchestrator.sessions_expired", count=len(expired))

    # -- main loop ------------------------------------------------------------

    async def process_turn(
        self,
        session_id: str,
        user_input: str,
        *,
        on_event: EventSink | None = None,
    ) -> dict:
        """Process one operator turn through the two-tier loop.

        ``on_event(kind, data)`` (optional) receives ``thought`` /
        ``tool_call`` / ``tool_result`` events as the turn runs (C-D).
        """
        state = self._active_sessions.get(session_id)
        if state is None:
            raise ValueError("No active session: " + session_id)
        async with state.lock:
            try:
                return await self._run_turn(state, user_input, _TurnEvents(on_event))
            finally:
                state.last_used = time.monotonic()

    async def _run_turn(
        self, state: ConversationState, user_input: str, events: _TurnEvents
    ) -> dict:
        # Per-turn fields start clean; the message history carries over.
        state.tool_calls = []
        state.tool_calls_total = 0
        state.errors = []
        state.operator_alerts = []
        state.tiers_used = []
        state.tiers = []
        state.degraded = False
        state.tool_results = []
        state.turn_entities = {}
        state.directives = []
        state.last_pick = {}
        state.aux_low_confidence = []
        state.turns += 1
        state.add_message("user", user_input)
        state.turn_start_index = len(state.messages)

        turn_start = time.time()
        turn_deadline = turn_start + TOTAL_TURN_TIMEOUT_SEC
        tool_round = 0
        final_response: dict | None = None
        executed_signatures: set[tuple] = set()

        logger.info(
            "orchestrator.turn.start",
            session_id=state.session_id,
            user_input_len=len(user_input),
            turn=state.turns,
        )

        if state.requested_movement_id and not state.movement_id:
            await events.thought(
                f"Movement {state.requested_movement_id} is not known to the brain — "
                "answering without movement context (nothing is audited against it).",
                "heuristic",
            )

        # Deterministic grounding of a map request: one read-only lookup of
        # the kinds the operator named, so the canvas directive never
        # depends on which tool the strategic tier happens to pick first.
        grounding = _map_grounding_args(user_input, state)
        if grounding is not None:
            tool_round += 1
            await events.thought(
                "Map request — resolving "
                + (", ".join(grounding.get("kinds") or []) or "the entity")
                + (f" matching '{grounding['match']}'" if grounding.get("match") else "")
                + " in the city picture first (deterministic, read-only)",
                "heuristic",
            )
            executed_signatures.add(
                ("city_query_entities", json.dumps(grounding, sort_keys=True, default=str))
            )
            await self._run_tool(
                state, "city_query_entities", grounding, tool_round, events,
                deadline=turn_deadline, tier="heuristic",
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
                state.degraded = True
                break

            # ---- STRATEGIC tier: decider picks the next tool ----------------
            next_tool, decider_audit = await self._select_tool(
                state, user_input, deadline=turn_deadline, events=events
            )
            if decider_audit:
                state.operator_alerts.extend(decider_audit.get("alerts", []))
                state.aux_low_confidence.extend(decider_audit.get("aux_low_confidence", []))
                state.tiers_used.append("decider:" + str(decider_audit.get("tier", "unknown")))

            if next_tool == FINAL_ANSWER or next_tool is None:
                final_response = await self._final_answer(
                    state, user_input, deadline=turn_deadline, events=events
                )
                break

            # ---- TACTICAL tier: Gemma drafts the arguments --------------------
            arguments, arg_tier = await self._draft_arguments(
                state, next_tool, user_input, deadline=turn_deadline, events=events
            )
            state.tiers_used.append("gemma_args:" + arg_tier)
            arguments = await self._scope_to_selection(
                state, next_tool, arguments, user_input, events)
            arguments = await self._ground_canvas_urns(
                state, next_tool, arguments, user_input, events)

            signature = (next_tool, json.dumps(arguments, sort_keys=True, default=str))
            if signature in executed_signatures:
                # Identical (tool, arguments) already ran this turn — its
                # result is already in the transcript. Re-dispatching would
                # be a duplicate round (reasoning-loop signal to the gate);
                # compose the final answer from what we have instead.
                logger.info("orchestrator.duplicate_tool_call_skipped", tool=next_tool)
                await events.thought(
                    f"{next_tool} already ran with these arguments — composing the answer.",
                    "heuristic",
                )
                final_response = await self._final_answer(
                    state, user_input, deadline=turn_deadline, events=events
                )
                break
            executed_signatures.add(signature)

            # ---- Dispatch the tool call via MCP (HTTP) -------------------------
            tool_round += 1
            await self._run_tool(
                state, next_tool, arguments, tool_round, events, deadline=turn_deadline,
                tier=state.last_pick.get("tier"), confidence=state.last_pick.get("confidence"),
            )

        if final_response is None:
            max_rounds_msg = (
                "Reached maximum " + str(MAX_TOOL_ROUNDS) + " tool interaction rounds."
            )
            final_response = self._error_response(max_rounds_msg, action="max_rounds_exceeded")
            state.degraded = True

        # Agentic God's-eye: the map request's directive, independent of the
        # tier that answered (proposals only — a human applies them).
        await self._map_post_step(state, user_input, events)

        # Audit trail: decisions_log entry for this turn.
        final_response["tool_calls_made"] = [
            tc["name"] for tc in state.tool_calls
        ]
        final_response["tool_calls"] = list(state.tool_calls)
        final_response["tiers_used"] = list(dict.fromkeys(state.tiers_used)) or ["heuristic"]
        final_response["tiers"] = list(state.tiers) or ["heuristic"]
        final_response["degraded"] = state.degraded
        final_response["directives"] = list(state.directives)
        final_response["citations"] = self._citations(state, final_response)
        if state.operator_alerts:
            final_response["operator_alerts"] = state.operator_alerts
        if state.aux_low_confidence:
            final_response["aux_low_confidence"] = list(state.aux_low_confidence)

        prose = final_prose(final_response)
        state.add_message("assistant", prose)

        if state.movement_id:
            await self._context_store.append_decision(
                state.movement_id,
                {
                    "session_id": state.session_id,
                    "phase": "orchestrator_turn",
                    "action": final_response.get("action"),
                    "confidence": final_response.get("confidence"),
                    "tiers_used": final_response.get("tiers_used"),
                    "tiers": final_response.get("tiers"),
                    "degraded": state.degraded,
                    "tool_calls": final_response.get("tool_calls_made"),
                    "tool_calls_total": state.tool_calls_total,
                    "operator_alerts": len(state.operator_alerts),
                    # Auxiliary score readouts below the floor while the pick
                    # itself cleared it: audited here, never an operator alert.
                    "aux_low_confidence": list(state.aux_low_confidence),
                    "directives": [
                        {"id": d["id"], "verb": d["verb"], "tier": d["provenance"]["tier"]}
                        for d in state.directives
                    ],
                    "event_id": state.event_id,
                    "context_urn": state.context_urn,
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
            session_id=state.session_id,
            tool_rounds=tool_round,
            tool_calls_total=state.tool_calls_total,
            tiers=final_response["tiers"],
            degraded=state.degraded,
            directives=len(state.directives),
            duration_sec=round(time.time() - turn_start, 2),
        )

        return final_response

    async def _run_tool(
        self,
        state: ConversationState,
        name: str,
        arguments: dict,
        tool_round: int,
        events: _TurnEvents,
        *,
        deadline: float | None,
        tier: str | None = None,
        confidence: float | None = None,
    ) -> None:
        """Dispatch one tool, stream it, keep its result for the answer, and
        harvest the entities it names (a canvas verb becomes a directive)."""
        call_id = "call_" + str(tool_round) + "_" + str(name)
        logger.info("orchestrator.tool_round", round=tool_round, tool=name)
        state.tool_calls.append({"name": name, "arguments": arguments})
        state.tool_calls_total += 1

        await events.tool_call(call_id, name, arguments)
        started = time.monotonic()
        results = await self._dispatch_tool_calls(
            [{"name": name, "arguments": arguments, "call_id": call_id}],
            deadline=deadline,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        for result in results:
            payload = result["result"]
            ok = not (
                isinstance(payload, dict)
                and (payload.get("error") is True or payload.get("ok") is False)
            )
            if (result["name"] in CANVAS_VERBS and isinstance(payload, dict)
                    and isinstance(payload.get("directive"), dict)):
                # the tool card names the target as the chip will ('track
                # GJ27EM•••1'), not by its raw URN
                directive = payload["directive"]
                payload = {**payload, "directive": {
                    **directive, "label": _directive_label(directive, state)}}
            summary = summarize_tool_result(result["name"], arguments, payload)
            state.tool_results.append({
                "name": result["name"],
                "arguments": arguments,
                "ok": ok,
                "summary": summary,
                "call_id": call_id,
                "simulated": ok and _simulated_source(payload),
            })
            await events.tool_result(
                result.get("call_id") or call_id, result["name"], ok, summary,
                payload, duration_ms,
            )
            rendered = json.dumps(_compact_for_context(payload), default=str)
            state.add_message(
                "tool",
                truncate_for_context(rendered, TOOL_RESULT_TRUNCATE_BYTES),
                tool_name=result["name"],
                tool_call_id=result.get("call_id"),
            )
            if ok:
                _harvest_entities(state.turn_entities, payload, call_id)
                if result["name"] in CANVAS_VERBS and isinstance(payload, dict):
                    await self._issue_directive(
                        state, payload.get("directive"), events,
                        tier=tier or "heuristic", confidence=confidence, tool_call_id=call_id,
                    )

    async def _scope_to_selection(
        self,
        state: ConversationState,
        tool: str,
        arguments: dict,
        user_input: str,
        events: _TurnEvents,
    ) -> dict:
        """The selected corridor event scopes event tools that name none.

        A drafted kind filter that would hide the selection is dropped with
        it. A question about many events ('which events…', 'all…') keeps
        the whole board.
        """
        if not state.event_id or tool not in _EVENT_SCOPED_TOOLS or not isinstance(arguments, dict):
            return arguments
        if arguments.get("event_id") or arguments.get("movement_id"):
            return arguments
        if tool == "get_active_corridor_events" and _PLURAL_EVENTS_RE.search(user_input):
            return arguments
        scoped = {k: v for k, v in arguments.items() if k != "event_kind"}
        scoped["event_id"] = state.event_id
        await events.thought(
            f"{tool} scoped to the selected corridor event {state.event_id} (operator context)",
            "heuristic",
        )
        return scoped

    async def _ground_canvas_urns(
        self,
        state: ConversationState,
        tool: str,
        arguments: dict,
        user_input: str,
        events: _TurnEvents,
    ) -> dict:
        """A canvas verb's mis-copied URN is snapped to the entity it meant.

        The tactical tier copies 36-character uuids by hand and drops a
        character now and then (seen live: 'vehicle:1b94e92-…'). Only a
        NEAR MISS is corrected: a drafted URN within a few edits of exactly
        one resolved entity of its kind — malformed, or a uuid that a changed
        character left well-formed. A well-formed URN of an entity this turn
        did not list (a unit outside the top 20) is kept as drafted, never
        swapped for a different unit or vehicle; the deck resolves it against
        its own registry or drops it. A malformed URN with no near miss stays
        malformed, the verb fails validation, and the map post-step proposes
        the ranked entity instead (tier 'heuristic'). Every snap is said.
        """
        if tool not in CANVAS_VERBS or not isinstance(arguments, dict):
            return arguments
        known = {**state.session_entities, **state.turn_entities}
        if not known:
            return arguments

        def snap(value: Any) -> str | None:
            urn = canonical_urn(value)
            if urn and urn in known:
                return urn
            kind = str(value).split(":", 1)[0] if isinstance(value, str) else ""
            if urn is not None and kind not in _UUID_URN_KINDS:
                return urn  # a well-formed slug / integer id: another real entity
            same = [u for u, e in known.items() if str(e.get("kind")) == kind]
            return _near_miss(str(value).strip().lower(), same) or urn

        fixed = dict(arguments)
        if isinstance(fixed.get("urn"), str):
            urn = snap(fixed["urn"])
            if urn and urn != canonical_urn(fixed["urn"]):
                await events.thought(
                    f"{tool}: drafted urn {fixed['urn'][:60]} names no resolved entity — "
                    f"using {urn}, resolved this turn ({_edit_distance(fixed['urn'].lower(), urn)} "
                    "character(s) apart)",
                    "heuristic",
                )
            if urn:
                fixed["urn"] = urn
            if urn and tool == "canvas_fly_to":
                fixed.pop("lon", None)
                fixed.pop("lat", None)
        if isinstance(fixed.get("urns"), list):
            snapped = [snap(u) for u in fixed["urns"]]
            fixed["urns"] = list(dict.fromkeys(u for u in snapped if u)) or fixed["urns"]
        return fixed

    # -- directives (agentic God's-eye, C-D2) ----------------------------------------

    async def _issue_directive(
        self,
        state: ConversationState,
        directive: Any,
        events: _TurnEvents,
        *,
        tier: str,
        confidence: float | None,
        tool_call_id: str | None,
    ) -> dict | None:
        """Stream one canvas directive (a proposal) and track it for its ack."""
        if not isinstance(directive, dict) or directive.get("verb") not in CANVAS_VERBS:
            return None
        args = directive.get("args") if isinstance(directive.get("args"), dict) else {}
        key = (directive["verb"], json.dumps(args, sort_keys=True))
        if any((d["verb"], json.dumps(d["args"], sort_keys=True)) == key for d in state.directives):
            return None
        out = {
            "id": "dir_" + uuid.uuid4().hex[:16],
            "verb": directive["verb"],
            "args": args,
            "label": _directive_label(directive, state),
            "provenance": {
                "session_id": state.session_id,
                "tier": tier,
                "confidence": (
                    round(confidence, 4) if isinstance(confidence, (int, float)) else None
                ),
                "tool_call_id": tool_call_id,
            },
        }
        state.directives.append(out)
        self._directives[out["id"]] = {
            "directive": out,
            "session_id": state.session_id,
            "movement_id": state.movement_id,
            "issued_at": time.time(),
            "ack": None,
        }
        self._prune_directives()
        await events.directive(out)
        logger.info("orchestrator.directive", id=out["id"], verb=out["verb"], tier=tier)
        return out

    async def _map_post_step(
        self, state: ConversationState, user_input: str, events: _TurnEvents
    ) -> None:
        """A map request whose tool results resolved entities gets its directive.

        Deterministic (tier 'heuristic'): the entity the request names is
        ranked over this turn's tool results (a typed plate first, then the
        kind named, 'stolen' against a stolen-vehicle earmark, a live state,
        a position); a 'this / it' request falls back to the session's
        context entity. One moving entity is tracked, a place is flown to,
        and 'highlight / all …' highlights up to 20 of the kind named.
        """
        if not _MAP_INTENT_RE.search(user_input):
            return
        if any(d["verb"] in ("canvas_fly_to", "canvas_track_entity", "canvas_highlight")
               for d in state.directives):
            return  # a tier already proposed the camera move
        prefer = (f"event:{state.event_id}" if state.event_id else None, state.context_urn)
        ranked = _rank_entities(user_input, state.turn_entities, prefer)
        if not ranked and state.context_urn and state.context_urn in state.session_entities:
            ranked = [state.session_entities[state.context_urn]]
        if not ranked and _DEICTIC_RE.search(user_input) and state.event_id:
            event_urn = f"event:{state.event_id}"
            ranked = [state.session_entities.get(event_urn, {"urn": event_urn, "kind": "event"})]
        if not ranked:
            return
        top = ranked[0]
        kind = str(top.get("kind") or top["urn"].split(":", 1)[0])
        if _HIGHLIGHT_RE.search(user_input):
            same = [e["urn"] for e in ranked if str(e.get("kind")) == kind][:20]
            if len(same) > 1:
                directive = {"verb": "canvas_highlight", "args": {"urns": same},
                             "label": f"highlight {len(same)} {kind}(s)"}
                await self._issue_directive(state, directive, events, tier="heuristic",
                                            confidence=None, tool_call_id=top.get("call_id"))
                return
        verb = "canvas_track_entity" if (_TRACK_RE.search(user_input) or kind in _TRACKABLE_KINDS) \
            else "canvas_fly_to"
        directive = {"verb": verb, "args": {"urn": top["urn"]}, "label": ""}
        await self._issue_directive(state, directive, events, tier="heuristic",
                                    confidence=None, tool_call_id=top.get("call_id"))

    def _citations(self, state: ConversationState, answer: dict) -> list[dict]:
        """``[{urn, label}]`` the turn's tool results named (plate-masked labels).

        Directive targets first, then entities the answer's prose mentions,
        then the rest in the order the tools returned them.
        """
        prose = _answer_prose(answer) or ""
        prose_norm = prose.lower()
        targets: list[str] = []
        for d in state.directives:
            args = d.get("args") or {}
            targets += [args["urn"]] if args.get("urn") else list(args.get("urns") or [])
        pool = dict(state.turn_entities)
        if state.context_urn and state.context_urn in state.session_entities:
            pool.setdefault(state.context_urn, state.session_entities[state.context_urn])

        def mentioned(entity: dict) -> bool:
            label = str(entity.get("label") or "").lower()
            ident = entity["urn"].split(":", 1)[1].lower()
            return (len(label) > 3 and label in prose_norm) or ident in prose_norm

        order = (
            [u for u in targets if u in pool]
            + [u for u, e in pool.items() if u not in targets and mentioned(e)]
            + [u for u in pool if u not in targets]
        )
        out: list[dict] = []
        for urn in dict.fromkeys(order):
            entity = pool.get(urn) or {}
            out.append({"urn": urn, "label": mask_plates(entity.get("label") or urn)})
            if len(out) >= MAX_CITATIONS:
                break
        return out

    def _prune_directives(self) -> None:
        cutoff = time.time() - DIRECTIVE_TTL_SEC
        while self._directives:
            oldest_id, oldest = next(iter(self._directives.items()))
            if len(self._directives) > MAX_TRACKED_DIRECTIVES or oldest["issued_at"] < cutoff:
                self._directives.pop(oldest_id, None)
                continue
            break

    async def ack_directive(
        self,
        session_id: str,
        directive_id: str,
        decision: str,
        *,
        officer: str | None = None,
        claimed_officer: str | None = None,
    ) -> dict:
        """Record the operator's answer to a directive (applied / dismissed).

        Raises KeyError for an unknown (or expired) directive, ValueError for
        a session mismatch or a bad decision, and LookupError when it was
        already acknowledged. The ack is audited into the bound movement's
        decisions_log when the session has one; it is always logged.
        """
        if decision not in DIRECTIVE_DECISIONS:
            raise ValueError(f"decision must be one of {list(DIRECTIVE_DECISIONS)}")
        self._prune_directives()
        record = self._directives.get(directive_id)
        if record is None:
            raise KeyError(directive_id)
        if record["session_id"] != session_id:
            raise ValueError("the directive belongs to another session")
        if record["ack"] is not None:
            raise LookupError(record["ack"]["decision"])
        directive = record["directive"]
        ack = {
            "phase": "canvas_directive",
            "action": f"directive_{decision}",
            "directive_id": directive_id,
            "verb": directive["verb"],
            "args": directive["args"],
            "label": directive["label"],
            "tier": directive["provenance"]["tier"],
            "confidence": directive["provenance"]["confidence"],
            "session_id": session_id,
            "officer": officer,
            "decision": decision,
            "timestamp": time.time(),
        }
        if claimed_officer and claimed_officer != officer:
            ack["claimed_officer"] = claimed_officer
        record["ack"] = ack
        recorded_in = "brain log"
        if record.get("movement_id"):
            await self._context_store.append_decision(record["movement_id"], ack)
            recorded_in = "decisions_log"
        # The log line carries the whole ack (a session with no bound
        # movement — most chat, palette and 'Explain this' sessions — is
        # recorded nowhere else, so a differing typed name is kept here too).
        logger.info(
            "orchestrator.directive_ack",
            directive_id=directive_id, verb=directive["verb"], decision=decision,
            officer=officer, claimed_officer=ack.get("claimed_officer"),
            session_id=session_id, label=directive["label"], tier=ack["tier"],
            recorded_in=recorded_in,
        )
        return {
            "directive_id": directive_id,
            "decision": decision,
            "verb": directive["verb"],
            "officer": officer,
            "recorded_in": recorded_in,
        }

    # -- tier bookkeeping ----------------------------------------------------------

    @staticmethod
    def _note_tier(state: ConversationState, tier: str) -> None:
        """Record a contributing tier in the contract shape (C-D tiers_used)."""
        label = {
            "decider": "decider:" + DECIDER_MODEL_ID,
            "gemma": "gemma:" + GEMMA_MODEL_ID,
        }.get(tier, "heuristic")
        if label not in state.tiers:
            state.tiers.append(label)
        if tier == "heuristic":
            state.degraded = True

    # -- strategic tier ----------------------------------------------------------

    async def _select_tool(
        self,
        state: ConversationState,
        user_input: str,
        *,
        deadline: float | None = None,
        events: _TurnEvents | None = None,
    ) -> tuple[str | None, dict | None]:
        """Ask the decider which tool to invoke next.

        Returns (tool_name_or_FINAL_ANSWER, audit_record_or_None). Falls back
        to the tactical tier (Gemma grammar-constrained menu pick) and then to
        keyword heuristics when the decider tier is unavailable, withheld or
        rate-limited. A decider pick whose own confidence is below the floor
        is acted on only when Gemma independently agrees (low confidence on
        the auxiliary score question alone is surfaced, not gated). The
        deterministic
        floor never repeats a tool it already called this turn (keyword
        matching is stateless — repeating would loop to max rounds).
        """
        events = events or _TurnEvents(None)

        def heuristic_floor() -> str:
            already_called = [tc["name"] for tc in state.tool_calls]
            pick = heuristic_select_tool(user_input, _TOOL_MENU)
            if pick is None or pick in already_called:
                return FINAL_ANSWER
            return pick

        async def tactical_or_floor(lead: str | None) -> str:
            gemma_choice = await self._gemma_select_tool(state, deadline=deadline)
            if gemma_choice is not None:
                self._note_tier(state, "gemma")
                state.last_pick = {"tier": "gemma", "confidence": None}
                await events.thought(
                    (lead + " — " if lead else "")
                    + f"tactical tier (Gemma) selected {gemma_choice}",
                    "gemma",
                )
                return gemma_choice
            pick = heuristic_floor()
            self._note_tier(state, "heuristic")
            state.last_pick = {"tier": "heuristic", "confidence": None}
            await events.thought(
                (lead + " — " if lead else "")
                + f"tactical tier unavailable; deterministic keyword floor selected {pick}",
                "heuristic",
            )
            return pick

        if self._decider is None:
            return await tactical_or_floor("Strategic tier not configured"), None

        context: dict[str, Any] = {
            "tools_already_called": [tc["name"] for tc in state.tool_calls],
            "vvip_class": state.vvip_class or "unspecified",
        }
        if state.event_brief:
            context["selected_event"] = state.event_brief[:300]
        if state.context_brief:
            context["context_entity"] = state.context_brief[:300]
        earlier = _previous_exchange(state)
        if earlier:
            # Follow-ups ("is that enough lanes?") are only decidable with
            # the previous answer in view.
            context["earlier_in_session"] = earlier
        state_doc = DeciderBrain.build_state_doc(
            trigger="operator_chat",
            summary="Operator request: " + user_input[:600],
            context=context,
        )
        questions = DeciderBrain.tool_selection_question(_TOOL_MENU)

        result = await self._decider.checkpoint(
            movement_id=state.movement_id or state.session_id,
            trigger="operator_chat",
            state_doc=state_doc,
            questions=questions,
            # Every operator turn is one human-paced operator_chat event and
            # its chained tool-selection checkpoints belong to it (contract
            # F8 targets per-GPS-tick spam, not operator strategy). With
            # session continuity the cadence key is the session, so an
            # unforced first checkpoint would rate-limit a follow-up asked
            # within 5 s and hand the turn to the fallback tier.
            force=True,
        )

        audit = {
            "tier": result.tier,
            "record": result.audit,
            "alerts": [dict(a) for a in result.operator_alerts],
        }
        answer = result.answers.get("next_tool")
        value = normalize_tool_name(answer.value if answer else None)
        confidence = answer.confidence if answer else None
        conf_text = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "n/a"

        if result.tier == "decider":
            self._note_tier(state, "decider")
            if value in _TOOL_MENU or value == FINAL_ANSWER:
                # Confidence gate on the PICK (the decision-bearing next_tool
                # answer). The decider's aggregate needs_gemma_draft also
                # trips on the auxiliary decision_confidence score, whose
                # readout confidence sits at 0.06-0.42 live even when the pick
                # itself is 0.6-0.97 — gating on it let the weaker tier
                # overrule correct strategic picks round after round.
                pick_low = (
                    isinstance(confidence, (int, float)) and confidence < CONFIDENCE_FLOOR
                )
                low = ", ".join(
                    f"{name} {row['confidence']:.2f}"
                    for name, row in (result.audit.get("answers") or {}).items()
                    if isinstance(row, dict)
                    and isinstance(row.get("confidence"), (int, float))
                    and row["confidence"] < CONFIDENCE_FLOOR
                )
                if not pick_low:
                    # The pick cleared the floor: a low_confidence alert raised
                    # ONLY by the auxiliary score is audit material (the turn
                    # row's aux_low_confidence), not an operator alert — no
                    # confirmation runs and nothing waits on a human.
                    kept, aux = [], []
                    for alert in audit["alerts"]:
                        questions = alert.get("questions") or {}
                        if (alert.get("type") == "low_confidence_decision"
                                and "next_tool" not in questions):
                            aux.append({
                                "questions": questions,
                                "floor": alert.get("floor"),
                                "pick": value,
                                "pick_confidence": confidence,
                                "timestamp": alert.get("timestamp"),
                            })
                        else:
                            kept.append(alert)
                    audit["alerts"], audit["aux_low_confidence"] = kept, aux
                    state.last_pick = {"tier": "decider", "confidence": confidence}
                    await events.thought(
                        f"Strategic tier ({DECIDER_MODEL_ID}) chose {value} "
                        f"(confidence {conf_text})"
                        + (
                            f"; auxiliary {low} below the {CONFIDENCE_FLOOR:.2f} floor is "
                            "recorded in the audit (the pick itself cleared it)"
                            if low else ""
                        ),
                        "decider",
                    )
                    return value, audit
                # Below the floor the pick needs tactical agreement before it
                # is acted on.
                for alert in audit["alerts"]:
                    if alert.get("type") == "low_confidence_decision":
                        alert["confirmation_ran"] = True
                await events.thought(
                    f"Strategic tier ({DECIDER_MODEL_ID}) leans {value} but its pick "
                    f"confidence {conf_text} is below the {CONFIDENCE_FLOOR:.2f} floor — "
                    "asking the tactical tier to confirm",
                    "decider",
                )
                gemma_choice = await self._gemma_select_tool(state, deadline=deadline)
                if gemma_choice is not None:
                    self._note_tier(state, "gemma")
                audit["confirmation"] = {"decider": value, "gemma": gemma_choice}
                if gemma_choice == value:
                    audit["confirmation"]["agreed"] = True
                    state.tiers_used.append("decider:confirmed_by_gemma")
                    state.last_pick = {"tier": "decider", "confidence": confidence}
                    await events.thought(
                        f"Tactical tier (Gemma) agrees: {value}", "gemma"
                    )
                    return value, audit
                audit["confirmation"]["agreed"] = False
                state.operator_alerts.append({
                    "type": "decider_gemma_disagreement",
                    "movement_id": state.movement_id or state.session_id,
                    "trigger": "operator_chat",
                    "decider_pick": value,
                    "decider_confidence": confidence,
                    "gemma_pick": gemma_choice,
                    "action": (
                        "acted on the tactical pick" if gemma_choice is not None
                        else "tactical tier unavailable; deterministic floor"
                    ),
                    "timestamp": time.time(),
                })
                if gemma_choice is not None:
                    state.last_pick = {"tier": "gemma", "confidence": None}
                    await events.thought(
                        f"Tactical tier (Gemma) chose {gemma_choice} instead — acting on "
                        "the tactical pick; the disagreement is audited",
                        "gemma",
                    )
                    return gemma_choice, audit
                pick = heuristic_floor()
                self._note_tier(state, "heuristic")
                state.last_pick = {"tier": "heuristic", "confidence": None}
                await events.thought(
                    f"Tactical tier unavailable to confirm — deterministic keyword floor "
                    f"selected {pick}",
                    "heuristic",
                )
                return pick, audit
            # Decider chose something off-menu — fall through to tactical tier.
            logger.warning("orchestrator.decider_off_menu", value=value)
            lead = f"Strategic tier answered off-menu ({value})"
        elif result.tier == "novelty_latched":
            self._note_tier(state, "decider")
            lead = (
                f"Strategic tier's confident pick ({value}, confidence {conf_text}) withheld: "
                f"{result.degraded_reason or 'novel situation'}"
            )
        elif result.tier == "rate_limited":
            lead = "Strategic tier rate-limited (checkpoint cadence)"
        else:
            state.degraded = True
            lead = f"Strategic tier unavailable ({result.degraded_reason or result.tier})"

        # Decider unavailable/degraded/withheld/rate-limited: Gemma selection.
        return await tactical_or_floor(lead), audit

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
            calls = parsed.get("tool_calls")
            if isinstance(calls, list) and not calls:
                return FINAL_ANSWER  # explicit "enough data" signal
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
        events: _TurnEvents | None = None,
    ) -> tuple[dict, str]:
        """Gemma drafts arguments for the decider-chosen tool against its
        inputSchema, with one repair round. The generation is
        schema-constrained (llama-server compiles the envelope + inputSchema
        to a GBNF grammar) and capped at TACTICAL_ARGS_MAX_TOKENS, so the
        call cannot ramble past the turn budget. Falls back to deterministic
        defaults (segment ids named in the request are honoured)."""
        events = events or _TurnEvents(None)
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
            self._note_tier(state, "gemma")
            await events.thought(
                f"Tactical tier (Gemma) drafted the {tool_name} arguments", "gemma"
            )
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
            self._note_tier(state, "gemma")
            await events.thought(
                f"Tactical tier (Gemma) drafted the {tool_name} arguments after one repair round",
                "gemma",
            )
            return arguments, "gemma_repaired"

        logger.warning("orchestrator.heuristic_arguments", tool=tool_name)
        ranked = _rank_entities(user_input, state.turn_entities)
        grounding = _map_grounding_args(user_input, state, force=True) or {}
        defaults = heuristic_default_arguments(
            tool_name,
            {
                "corridor_bbox": _DEFAULT_BBOX,
                "segment_ids": _segment_ids_in(user_input),
                "event_id": state.event_id,
                "context_urn": state.context_urn,
                "kinds": grounding.get("kinds"),
                "match": grounding.get("match"),
                "top_urn": ranked[0]["urn"] if ranked else state.context_urn,
                "top_urns": [e["urn"] for e in ranked[:20]],
            },
        )
        self._note_tier(state, "heuristic")
        await events.thought(
            f"Tactical tier could not draft valid {tool_name} arguments — deterministic "
            "defaults used",
            "heuristic",
        )
        return defaults, "heuristic"

    # -- final answer ----------------------------------------------------------------

    async def _final_answer(
        self,
        state: ConversationState,
        user_input: str,
        *,
        deadline: float | None = None,
        events: _TurnEvents | None = None,
    ) -> dict:
        """Gemma composes the final structured answer (heuristic floor).

        The transcript is the operator request plus THIS turn's tool results,
        budgeted to PROMPT_TOKEN_BUDGET together with the answer system
        prompt — the session system prompt is never duplicated into it.
        """
        events = events or _TurnEvents(None)
        schema = load_schema("orchestrator_final")
        prompt = (
            "Compose the final answer to the operator's request from the tool "
            "results above. Reply with ONLY the required JSON object. The "
            "'reasoning' field is shown to the operator verbatim as your answer: "
            "write 2-5 plain sentences (under 1000 characters) that answer the "
            "question with the concrete facts from the tool results — names, "
            "statuses, segment ids, congestion values, lane counts, closure "
            "windows, times — never generic reassurance. State only what the "
            "tool results contain: never add a detail they do not report (a "
            "vehicle's make, model or colour, a place, a time). Refer to vehicles, "
            "cameras, junctions and events by their labels — never quote a uuid, "
            "a URN or raw coordinates (name the road, junction or segment "
            "instead). A vehicle or sighting was seen at its own seen_ist / "
            "age_sec — 'queried_at' is only when the question was asked. A "
            "police station named in an FIR or earmark reason is where the case "
            "was reported, not where the vehicle is. Name every event you "
            "mention with its status word exactly as the tools report it "
            "(planning / approved / active / completed / cancelled). Use a "
            "result's 'note' when it has one, and say plainly when a tool failed. Data marked "
            "EMULATED or synthetic is a simulated feed: say so — and never say "
            "data is real, verified or not emulated. Never put JSON "
            "inside 'reasoning'.\n\n"
            "Original request: " + user_input[:800]
        )
        system_prompt = state.answer_system_prompt or VVIP_AGENT_SYSTEM_PROMPT
        transcript = self._final_transcript(state, user_input, system_prompt, prompt)
        error: str | None = None

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
                system_prompt=system_prompt,
            )
            if raw is not None:
                parsed = extract_json(raw)
                if isinstance(parsed, dict) and "action" in parsed:
                    prose = _answer_prose(parsed)
                    if prose:
                        parsed["reasoning"] = _grounded_prose(prose, state)
                        parsed.setdefault("data", {})
                        state.tiers_used.append("gemma_final")
                        self._note_tier(state, "gemma")
                        await events.thought(
                            "Tactical tier (Gemma) composed the answer from "
                            f"{len(state.tool_results)} tool result(s)",
                            "gemma",
                        )
                        return parsed
                    error = "the composed answer carried no prose"
                else:
                    # A reply cut at the token cap: its 'reasoning' (the
                    # schema's second field) is usually complete already.
                    rescued = rescue_string_field(raw, "reasoning")
                    if rescued and rescued.strip() and not _looks_structured(rescued):
                        state.tiers_used.append("gemma_final_rescued")
                        self._note_tier(state, "gemma")
                        if "gemma_final_rescued" not in state.tiers:
                            state.tiers.append("gemma_final_rescued")
                        await events.thought(
                            "Tactical tier (Gemma) answer was cut before its JSON closed — "
                            "its complete reasoning was recovered",
                            "gemma",
                        )
                        return {
                            "action": rescue_string_field(raw, "action") or "answered",
                            "reasoning": _grounded_prose(rescued.strip(), state),
                            "tool_calls_made": [tc["name"] for tc in state.tool_calls],
                            "confidence": "medium",
                            "data": {"tier": "gemma_final_rescued"},
                            "protocol_assessment": {
                                "asl_status": "not_assessed",
                                "protocol_violations": [],
                                "threat_level": "nominal",
                                "plan_b_ready": False,
                            },
                        }
                    error = "the composed answer did not match the answer schema"
            else:
                error = (state.errors[-1]["error"] if state.errors else "tactical tier unavailable")
        except LlamaUnavailable as exc:
            logger.warning("orchestrator.final_answer_llm_unavailable", error=str(exc))
            error = str(exc)

        state.tiers_used.append("heuristic_final")
        self._note_tier(state, "heuristic")
        await events.thought(
            "Tactical tier could not compose the answer — deterministic summary of the "
            "tool results instead",
            "heuristic",
        )
        floor = heuristic_final_answer(
            user_input,
            [tc["name"] for tc in state.tool_calls],
            error=(error or "")[:160] or None,
            tool_summaries=[r["summary"] for r in state.tool_results],
        )
        if isinstance(floor.get("reasoning"), str):
            floor["reasoning"] = _label_simulated(floor["reasoning"], state)
        return floor

    def _final_transcript(
        self,
        state: ConversationState,
        user_input: str,
        system_prompt: str,
        prompt: str,
    ) -> str:
        """Request + this turn's tool results, shrunk to the prompt budget."""
        budget = (
            PROMPT_TOKEN_BUDGET
            - estimate_prompt_tokens(system_prompt)
            - estimate_prompt_tokens(prompt)
            - 64  # chat-template framing
        )
        header = "[OPERATOR REQUEST]\n" + user_input[:800] + "\n\n" + _PROTOCOL_FLOORS
        if state.tool_results:
            # Deterministic one-liners first: cheap tokens that anchor the
            # prose on what each tool actually returned (or did not).
            header += "\n\n[TOOL SUMMARIES]\n" + "\n".join(
                "- " + r["summary"] for r in state.tool_results
            )
        # [label, full body, byte limit] — bodies are re-cut from the original
        # each round so truncation markers never stack.
        results = [
            ["[TOOL RESULT " + str(m.tool_name) + "]\n", m.content, len(m.content.encode("utf-8"))]
            for m in state.messages[state.turn_start_index:]
            if m.role == "tool"
        ]
        if not results:
            text = "No tools were called this turn."
            results = [["[TOOL RESULTS]\n", text, len(text)]]

        def render() -> str:
            return "\n\n".join(
                [header]
                + [label + truncate_for_context(body, limit) for label, body, limit in results]
            )

        # The previous exchange is small and is what makes follow-ups
        # ("is that enough lanes?") answerable: its room is reserved first,
        # and tool results shrink into what is left.
        earlier = _previous_exchange(state)
        if earlier:
            earlier = "[EARLIER IN THIS SESSION]\n" + earlier
            budget -= estimate_prompt_tokens(earlier) + 2

        text = render()
        for _ in range(24):
            if estimate_prompt_tokens(text) <= budget:
                break
            longest = max(results, key=lambda r: r[2])
            if longest[2] < 200:
                break
            longest[2] = int(longest[2] * 0.7)
            text = render()
        return earlier + "\n\n" + text if earlier else text

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

    @staticmethod
    def _budgeted_history(
        state: ConversationState, system_prompt: str, prompt: str
    ) -> list[dict[str, str]]:
        """Most recent non-system messages that fit the prompt budget (<= 10)."""
        budget = (
            PROMPT_TOKEN_BUDGET
            - estimate_prompt_tokens(system_prompt)
            - estimate_prompt_tokens(prompt)
            - 64
        )
        chosen: list[dict[str, str]] = []
        for msg in reversed([
            m for m in state.build_chat_messages() if m["role"] != "system"
        ][-10:]):
            cost = estimate_prompt_tokens(msg["content"])
            if cost <= budget:
                chosen.append(msg)
                budget -= cost
                continue
            if budget > 150:
                # Keep the newest overflowing message, shortened to fit.
                ratio = budget / cost
                shortened = truncate_for_context(
                    msg["content"], max(200, int(len(msg["content"].encode("utf-8")) * ratio))
                )
                chosen.append({"role": msg["role"], "content": shortened})
            break
        return list(reversed(chosen))

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
        system_prompt: str | None = None,
    ) -> str | None:
        """One tactical call with conversation history; None on any failure.

        The system message is the SESSION's own (VVIP prompt, tool-calling
        block, movement context, protocol) — the previous version dropped it
        for the bare VVIP prompt, so the model never saw the tool-calling
        instructions. History is budgeted to PROMPT_TOKEN_BUDGET.

        The call is bounded by the remaining turn budget (``deadline``): a
        generation that would sail past the budget is cancelled and the tier
        falls through, instead of the old behavior where one in-flight call
        could exceed both the 60 s gate and the client's 120 s timeout.
        """
        system = system_prompt or state.system_prompt or VVIP_AGENT_SYSTEM_PROMPT
        history = (
            self._budgeted_history(state, system, prompt) if plain_history else []
        )

        timeout = self._deadline_timeout(deadline)
        if timeout is not None and timeout <= 0:
            logger.warning("orchestrator.llm_turn_budget_exhausted")
            state.errors.append({"type": "llm_error", "error": "turn budget exhausted"})
            return None

        async def _call() -> str:
            return await self._bridge.generate(
                system_prompt=system,
                user_prompt=prompt,
                json_mode=True,
                schema=schema,
                schema_name=schema_name,
                max_tokens=max_tokens,
                extra_messages=history if plain_history else extra_messages,
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
        call_ids = []
        for i, tc in enumerate(tool_calls):
            call_id = tc.get("call_id") or ("call_" + str(i) + "_" + str(tc.get("name", "unknown")))
            call_ids.append(call_id)
            tasks.append(self._dispatch_single_tool(tc, call_id, deadline=deadline))

        completed = await asyncio.gather(*tasks, return_exceptions=True)

        results = []
        for i, result in enumerate(completed):
            tc = tool_calls[i]
            name = tc.get("name", "unknown")
            if isinstance(result, Exception):
                logger.error("orchestrator.tool_exception", tool=name, error=str(result))
                fail_msg = "Tool '" + name + "' failed: " + str(result)
                results.append({
                    "name": name,
                    "call_id": call_ids[i],
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
        a dead oracle must not push the turn past its deadline. Deterministic
        failures (bad arguments, HTTP 4xx) are never retried.
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
                retryable = getattr(exc, "retryable", True)
                logger.warning(
                    "orchestrator.tool_error",
                    tool=name,
                    attempt=attempt + 1,
                    retryable=retryable,
                    status=getattr(exc, "status_code", None),
                    error=str(exc),
                )
                if retryable and attempt < MAX_RETRIES_PER_TOOL - 1:
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
# Prose + alert rendering (the stream's operator-facing text)
# ---------------------------------------------------------------------------


def _looks_structured(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith(("{", "[", "```"))


def _answer_prose(answer: dict) -> str:
    """The operator-facing prose of a final-answer object ('' when none).

    Prefers ``reasoning`` (the schema's prose field), then ``answer`` /
    ``summary``; a value that is itself a JSON document is unwrapped once,
    never passed through raw.
    """
    for key in ("reasoning", "answer", "summary"):
        value = answer.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if not _looks_structured(value):
            return value.strip()
        inner = extract_json(value)
        if isinstance(inner, dict):
            for inner_key in ("reasoning", "answer", "summary", "text"):
                nested = inner.get(inner_key)
                if isinstance(nested, str) and nested.strip() and not _looks_structured(nested):
                    return nested.strip()
    return ""


def final_prose(result: dict) -> str:
    """Prose to stream as ``token`` events for a turn result (contract C-D).

    Never a raw JSON envelope: falls back to a plain sentence naming the
    action when the answer object carries no usable prose.
    """
    prose = _answer_prose(result) if isinstance(result, dict) else ""
    if prose:
        return prose
    action = result.get("action") if isinstance(result, dict) else None
    tools = result.get("tool_calls_made") if isinstance(result, dict) else None
    return (
        f"No narrative answer was produced (action: {action or 'unknown'}; tools: "
        f"{', '.join(tools or []) or 'none'})."
    )


def chunk_prose(text: str, size: int = 24) -> list[str]:
    """Split prose into ~``size``-character chunks on word boundaries."""
    chunks: list[str] = []
    current = ""
    for word in re.findall(r"\S+\s*", text):
        if current and len(current) + len(word) > size:
            chunks.append(current)
            current = ""
        current += word
    if current:
        chunks.append(current)
    return chunks


def alert_text(alert: dict) -> str:
    """One-line operator alert for the stream's ``done.data.operator_alerts``."""
    kind = str(alert.get("type", "alert"))
    if kind == "low_confidence_decision":
        questions = alert.get("questions") or {}
        detail = ", ".join(
            f"{name} {value:.2f}" if isinstance(value, (int, float)) else str(name)
            for name, value in questions.items()
        )
        # 'tactical confirmation required' only when a confirmation actually ran.
        tail = (
            "tactical confirmation required" if alert.get("confirmation_ran")
            else "recorded for audit (no tactical confirmation ran)"
        )
        return f"low_confidence_decision: {detail} below the {alert.get('floor')} floor — {tail}"
    if kind == "novel_situation_confident_answer":
        return (
            "novel_situation_confident_answer: confident strategic pick withheld (no "
            "similar situation on record) — tactical tier decided, operator informed"
        )
    if kind == "decider_unavailable":
        return f"decider_unavailable: {alert.get('reason')} — {alert.get('action')}"
    if kind == "novelty_check_unavailable":
        return (
            f"novelty_check_unavailable: {alert.get('error')} — latch disabled for "
            "this checkpoint"
        )
    if kind == "decider_gemma_disagreement":
        return (
            f"decider_gemma_disagreement: decider {alert.get('decider_pick')} vs Gemma "
            f"{alert.get('gemma_pick')} — {alert.get('action')}"
        )
    action = alert.get("action")
    return kind + (f": {action}" if action else "")


# ---------------------------------------------------------------------------
# Argument coercion / validation (no jsonschema dependency)
# ---------------------------------------------------------------------------


def _previous_exchange(state: ConversationState, limit: int = 600) -> str:
    """The session's previous question and answer, compacted ('' on turn one)."""
    history = state.messages[: max(0, state.turn_start_index - 1)]
    answer = next((m.content for m in reversed(history) if m.role == "assistant"), "")
    if not answer:
        return ""
    question = next((m.content for m in reversed(history) if m.role == "user"), "")
    text = f"Q: {question[:200]} A: {answer}"
    return text[:limit]


# ---------------------------------------------------------------------------
# Agentic God's-eye helpers (map grounding, entities, directives)
# ---------------------------------------------------------------------------


def _kinds_named(text: str) -> list[str]:
    """Entity kinds a message names, in grounding order (a plate implies vehicle)."""
    kinds = [kind for kind, pattern in _KIND_WORDS if pattern.search(text)]
    if _PLATE_RE.search(text) and "vehicle" not in kinds:
        kinds.insert(0, "vehicle")
    return kinds


def _map_grounding_args(
    text: str, state: ConversationState, *, force: bool = False
) -> dict | None:
    """city_query_entities arguments for a map request that names a kind.

    None when the message is not a map request (unless ``force``), names no
    kind or plate, or is about the session's own context entity ('show this').
    """
    if not force and not _MAP_INTENT_RE.search(text):
        return None
    if not force and state.context_urn and _DEICTIC_RE.search(text) and not _PLATE_RE.search(text):
        return None
    kinds = _kinds_named(text)
    plate = _PLATE_RE.search(text)
    if not kinds:
        return None
    args: dict[str, Any] = {"kinds": kinds[:4], "limit": 20}
    if plate:
        args["match"] = re.sub(r"[\s-]", "", plate.group(1)).upper()
    return args


def _harvest_entities(into: dict[str, dict], payload: Any, call_id: str, depth: int = 0) -> None:
    """Collect the entities a tool result names: ``{urn: facts}`` (bounded walk).

    A row with a ``urn`` brings its label, kind, state, position and props;
    a ``vehicle_urn`` / ``event_urn`` link brings the URN alone (labelled
    later from the row that names it, if any).
    """
    if depth > 4 or len(into) >= 200:
        return
    if isinstance(payload, list):
        for item in payload[:60]:
            _harvest_entities(into, item, call_id, depth + 1)
        return
    if not isinstance(payload, dict):
        return
    urn = canonical_urn(payload.get("urn")) if isinstance(payload.get("urn"), str) else None
    if urn:
        current = into.get(urn) or {}
        facts = {
            "urn": urn,
            "kind": payload.get("kind") or urn.split(":", 1)[0],
            "label": payload.get("label") or payload.get("title") or payload.get("name")
            or current.get("label") or urn,
            "state": payload.get("state", payload.get("status", current.get("state"))),
            "source": payload.get("source", current.get("source")),
            "props": payload.get("props") if isinstance(payload.get("props"), dict)
            else current.get("props") or {},
            "call_id": current.get("call_id") or call_id,
            "matched": bool(current.get("matched")),
        }
        lon, lat = payload.get("lon"), payload.get("lat")
        if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
            facts["lon"], facts["lat"] = float(lon), float(lat)
        elif current.get("lon") is not None:
            facts["lon"], facts["lat"] = current["lon"], current["lat"]
        into[urn] = facts
    for key in ("vehicle_urn", "event_urn"):
        linked = canonical_urn(payload.get(key)) if isinstance(payload.get(key), str) else None
        if linked and linked not in into:
            link_label = payload.get("vehicle_label") if key == "vehicle_urn" else None
            into[linked] = {
                "urn": linked, "kind": linked.split(":", 1)[0],
                "label": link_label if isinstance(link_label, str) and link_label else linked,
                "props": {}, "call_id": call_id, "matched": False,
            }
    if isinstance(payload.get("match"), str) and isinstance(payload.get("entities"), list):
        for row in payload["entities"]:
            matched = canonical_urn(row.get("urn")) if isinstance(row, dict) else None
            if matched:
                _harvest_entities(into, row, call_id, depth + 1)
                into[matched]["matched"] = True
    for key, value in payload.items():
        if key in ("props", "urns") or not isinstance(value, (dict, list)):
            continue
        _harvest_entities(into, value, call_id, depth + 1)


def _rank_entities(
    text: str, entities: dict[str, dict], prefer: tuple[str | None, ...] = ()
) -> list[dict]:
    """The turn's entities most likely meant by ``text``, best first.

    A plate the operator typed (the grounding query matched it) wins; then
    the operator's own selection ('this event' — the session's selected
    event or context entity, ``prefer``) when the message points at it; the
    kind the message names (its first kind highest); 'stolen' against a
    stolen-vehicle earmark; a live state; a known position. Ties keep the
    order the tools returned them.
    """
    if not entities:
        return []
    deictic = bool(_DEICTIC_RE.search(text))
    preferred = {p for p in prefer if p}
    kinds = _kinds_named(text)
    stolen = bool(_STOLEN_RE.search(text))
    scored = []
    for order, entity in enumerate(entities.values()):
        kind = str(entity.get("kind"))
        props = entity.get("props") or {}
        score = 0.0
        if entity.get("matched"):
            score += 100
        if deictic and entity.get("urn") in preferred:
            score += 60
        if kind in kinds:
            score += 25 - 3 * kinds.index(kind)
        if stolen and kind == "vehicle":
            blob = " ".join(str(v) for v in (
                props.get("lawful_basis"), props.get("earmark_reason"), entity.get("label"),
            ) if v).lower()
            if "stolen" in blob:
                score += 40
        if str(entity.get("state")) in (
            "flagged", "active", "en_route", "on_scene", "pending", "running",
        ):
            score += 5
        if entity.get("lon") is not None:
            score += 3
        if kinds and kind not in kinds:
            score -= 30
        scored.append((score, order, entity))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [entity for score, _order, entity in scored if score > 0]


def _directive_label(directive: dict, state: ConversationState) -> str:
    """The operator-facing label of a directive (plate-masked)."""
    verb = directive.get("verb")
    args = directive.get("args") if isinstance(directive.get("args"), dict) else {}
    known = {**state.session_entities, **state.turn_entities}

    def name(urn: str) -> str:
        entity = known.get(urn) or {}
        label = entity.get("label")
        return mask_plates(label) if label and label != urn else urn

    if verb == "canvas_track_entity" and args.get("urn"):
        return f"track {name(args['urn'])}"
    if verb == "canvas_fly_to" and args.get("urn"):
        return f"fly to {name(args['urn'])}"
    if verb == "canvas_annotate" and args.get("urn"):
        return f"note on {name(args['urn'])}: {args.get('text', '')}"
    if verb == "canvas_highlight" and args.get("urns"):
        names = [name(u) for u in args["urns"][:3]]
        more = len(args["urns"]) - len(names)
        return "highlight " + ", ".join(names) + (f" +{more}" if more > 0 else "")
    return mask_plates(directive.get("label") or str(verb))


def _related_summary(rows: list[dict], total: Any = None) -> str:
    """'4 recommendation (3 pending), 1 pursuit (planning: PURSUIT — …)' —
    every relation counted by state, so 'what waits on an officer' is a fact
    in the context, not a guess from the first few rows."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if isinstance(row, dict):
            groups.setdefault(str(row.get("relation") or row.get("kind")), []).append(row)
    parts = []
    for relation, items in groups.items():
        states: dict[str, int] = {}
        for item in items:
            states[str(item.get("state") or "—")] = states.get(str(item.get("state") or "—"), 0) + 1
        state_text = ", ".join(f"{n} {st}" for st, n in sorted(states.items()))
        sample = items[0].get("label") if len(items) == 1 else None
        parts.append(
            f"{len(items)} {relation} ({state_text})"
            + (f": {mask_plates(sample)}" if sample else "")
        )
    shown = sum(len(v) for v in groups.values())
    more = (
        f"; {int(total) - shown} more not listed"
        if isinstance(total, int) and total > shown else ""
    )
    return "; ".join(parts) + more


_DIGIT_RE = re.compile(r"\d")
_SIMULATED_WORDS_RE = re.compile(r"simulat|emulat|synthetic", re.IGNORECASE)
_SIMULATED_SOURCES = frozenset({"emulated", "synthetic", "simulated"})
SIMULATED_FEED_NOTE = "(SIMULATED FEED: part of this answer comes from emulated or synthetic data.)"


def _simulated_source(payload: Any, depth: int = 0) -> bool:
    """True when a tool result carries an emulated / synthetic marker:
    simulated_feed or emulated true, data_quality 'synthetic', source
    'emulated', provenance 'synthetic' or input_quality.synthetic > 0."""
    if depth > 5:
        return False
    if isinstance(payload, list):
        return any(_simulated_source(item, depth + 1) for item in payload[:60])
    if not isinstance(payload, dict):
        return False
    quality = payload.get("input_quality")
    synthetic_inputs = quality.get("synthetic") if isinstance(quality, dict) else None
    if (
        payload.get("simulated_feed") is True
        or payload.get("emulated") is True
        or str(payload.get("data_quality") or "").lower() == "synthetic"
        or str(payload.get("source") or "").lower() in _SIMULATED_SOURCES
        or payload.get("provenance") == "synthetic"
        or (isinstance(synthetic_inputs, (int, float)) and synthetic_inputs > 0)
    ):
        return True
    return any(
        _simulated_source(value, depth + 1)
        for key, value in payload.items()
        if isinstance(value, (dict, list)) and key not in _CONTEXT_DROP_KEYS
    )


def _label_simulated(prose: str, state: ConversationState) -> str:
    """An answer drawn from an emulated / synthetic tool result says so."""
    simulated = any(r.get("ok") and r.get("simulated") for r in state.tool_results)
    if not simulated or _SIMULATED_WORDS_RE.search(prose):
        return prose
    return prose.rstrip() + " " + SIMULATED_FEED_NOTE


#: A city coordinate pair copied out of a tool result ("23.02503, 72.575356",
#: "(72.5754, 23.0250)"): the operator reads the map, not numbers. Ahmedabad
#: latitudes / longitudes only, 3+ decimals each, so congestion values and
#: speeds never match.
_COORD_PAIR_RE = re.compile(
    r"(?:\b(?:the\s+)?(?:gps\s+)?coordinates\s*(?:of\s*)?)?\(?"
    r"\b(?:2[23]|7[23])\.\d{3,}\s*,\s*(?:2[23]|7[23])\.\d{3,}\b\)?",
    re.IGNORECASE,
)
#: A claim that the data is NOT emulated / simulated: the brain cannot verify
#: it (a band computed on the synthetic feed is not marked yet — seen live:
#: "The data is not emulated." on the sandbox's synthetic feed), and the
#: claim would also suppress the SIMULATED FEED label.
_NOT_SIMULATED_CLAIM_RE = re.compile(
    r"\b(?:is|are|was|were)\s+not\s+(?:emulated|simulated|synthetic)\b"
    r"|\b(?:isn't|aren't|wasn't|weren't)\s+(?:emulated|simulated|synthetic)\b"
    r"|\bnon-?(?:emulated|simulated|synthetic)\b"
    r"|\bnot\s+(?:an?\s+)?(?:emulated|simulated|synthetic)\s+(?:data|feed|source|reading)s?\b",
    re.IGNORECASE,
)


def _scrub_unverifiable(prose: str) -> str:
    """Raw coordinates read as the map position; a 'not emulated' claim is
    dropped with its sentence (never shown, never suppressing the label)."""
    prose = _COORD_PAIR_RE.sub("the position shown on the map", prose)
    if not _NOT_SIMULATED_CLAIM_RE.search(prose):
        return prose
    sentences = re.split(r"(?<=[.!?])\s+", prose.strip())
    return " ".join(x for x in sentences if not _NOT_SIMULATED_CLAIM_RE.search(x)).strip()


#: "an active pursuit" / "the active event": an activity claim about an
#: event. Negated forms ("no active events") are true when none is active
#: and are left alone.
_ACTIVE_EVENT_RE = re.compile(
    r"(?<!\bno )(?<!\bnot )\b(?P<article>an?\s+|the\s+)?(?:currently\s+)?active\s+"
    r"(?P<noun>(?:vehicle\s+)?pursuit|corridor\s+event|event|movement|escort|procession)"
    r"(?P<plural>s?)\b",
    re.IGNORECASE,
)


def _true_event_status(prose: str, state: ConversationState) -> str:
    """'an active pursuit' reads the status the tools reported when NO event
    the session named is active (seen live, twice: 'there is an active
    pursuit' in an 'Explain this' answer for a pursuit in planning)."""
    known = {**state.session_entities, **state.turn_entities}.values()
    statuses = {str(e["state"]) for e in known
                if str(e.get("kind")) == "event" and isinstance(e.get("state"), str)}
    if not statuses or "active" in statuses:
        return prose
    word = next(iter(statuses)) if len(statuses) == 1 else "not active"

    def fix(match: re.Match) -> str:
        article = match.group("article") or ""
        if article.strip().lower() == "an":
            article = article[0] + " "
        return f"{article}{match.group('noun')}{match.group('plural')} (status {word})"

    return _ACTIVE_EVENT_RE.sub(fix, prose)


def _grounded_prose(prose: str, state: ConversationState) -> str:
    """Prose that cites the data: a generic answer (no figure at all) while
    the tools returned figures gets their deterministic summaries appended;
    an answer drawn from a simulated source is labelled SIMULATED FEED. Any
    registration mark the model wrote (even an invented one) is masked, raw
    coordinates read as the map position, a claim that the data is not
    emulated is dropped, and an event called 'active' that the tools report
    otherwise carries its reported status."""
    prose = _true_event_status(_scrub_unverifiable(mask_plates(prose)), state)
    if _DIGIT_RE.search(prose):
        return _label_simulated(prose, state)
    facts = [
        r["summary"] for r in state.tool_results
        if r.get("ok") and _DIGIT_RE.search(r.get("summary") or "")
        and not str(r.get("name", "")).startswith("canvas_")
    ]
    if not facts:
        return _label_simulated(prose, state)
    data = "; ".join(_clip_words(f, 240) for f in facts[:3])
    if not prose:
        return _label_simulated(f"Data: {data}.", state)
    return _label_simulated(prose.rstrip() + f" (Data: {data}.)", state)


#: URN kinds whose ids are uuids (a hand-copied uuid is where near misses occur).
_UUID_URN_KINDS = frozenset({"event", "vehicle", "sighting", "dispatch", "recommendation"})


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (two short URNs; O(len(a) * len(b)))."""
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _near_miss(value: str, candidates: list[str]) -> str | None:
    """The one candidate a mis-copied URN meant: within 3 edits, unambiguous."""
    scored = sorted((_edit_distance(value, c.lower()), c) for c in candidates)
    if not scored or scored[0][0] > 3:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None  # two entities equally near: not a typo of either
    return scored[0][1]


def _clip_words(text: str, limit: int) -> str:
    """``text`` cut at a word boundary with an ellipsis ('segments 1012, …',
    never a figure cut in half such as 'segments 1')."""
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0].rstrip(",;:—- ")
    return head + " …"


def _segment_ids_in(text: str) -> list[int]:
    """Segment ids named in an operator request ("segments 1012, 1015 and 1005")."""
    if "segment" not in text.lower():
        return []
    return [int(n) for n in re.findall(r"\b\d{3,6}\b", text)][:200]


def _compact_for_context(value: Any, depth: int = 0) -> Any:
    """Drop geometry blobs and cap long lists before a result enters model context."""
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {
            k: _compact_for_context(v, depth + 1)
            for k, v in value.items() if k not in _CONTEXT_DROP_KEYS
        }
    if isinstance(value, list):
        head = [_compact_for_context(v, depth + 1) for v in value[:_CONTEXT_MAX_LIST]]
        if len(value) > _CONTEXT_MAX_LIST:
            head.append(f"... {len(value) - _CONTEXT_MAX_LIST} more")
        return head
    return value


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
