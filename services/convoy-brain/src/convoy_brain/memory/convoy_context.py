"""Convoy Context Memory — maintains stateful context for active convoy movements.

Stores the evolving state of each convoy movement in Valkey for fast access
by LLM agents. This enables agents to maintain awareness across multiple
interaction turns without reloading from PostgreSQL.

Context includes:
    - Current movement status and phase
    - Selected route and active diversions
    - Real-time convoy position and speed
    - Decisions made and their reasoning (audit trail)
    - Alerts and anomalies detected

Writes are field-scoped and atomic. Every partial update (a GPS fix, an
audit row, a protocol-state merge, the diversion bookkeeping, a status
change) goes through :meth:`ConvoyContextStore.mutate`, which re-reads the
document inside WATCH/MULTI and retries when another writer committed in
between — so a position update can no longer write back a stale snapshot
and wipe the status, the route or the decisions_log, and two writers that
touch different fields never lose each other's change. Writers inside one
brain process also queue on a per-movement lock, so a burst of concurrent
updates is serialized instead of thrashing the optimistic retry; WATCH
still guards against writers in other processes. ``put`` keeps the
whole-document write for creation and the pre-movement finalize step.

Every write sets a 7-day TTL (``CONTEXT_TTL_SEC``) and caps decisions_log at
``MAX_DECISIONS_LOG`` rows; the newest plan (``pre_movement``) and
``city_event`` rows are always kept, because the chat grounding and the
escort read the primary route and the corridor event from them.

Two small side keys sit next to the context document:
    convoy:position:{movement_id}  the C-B GPS frame (EX 30 s) the city
                                   picture renders as the convoy entity
    convoy:escort:{movement_id}    the escort status document behind
                                   GET /movements/{id}/escort (contract C-J)
"""

from __future__ import annotations

import asyncio
import json
import random
import weakref
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

CONTEXT_KEY_PREFIX = "convoy:context:"
POSITION_KEY_PREFIX = "convoy:position:"
ESCORT_KEY_PREFIX = "convoy:escort:"
CONTEXT_TTL_SEC = 7 * 86400  # 7 days on EVERY write — contexts never live forever
POSITION_TTL_SEC = 30        # contract C-B: a frame older than this has expired
ESCORT_STATUS_TTL_SEC = CONTEXT_TTL_SEC
MAX_DECISIONS_LOG = 500
MAX_ALERTS = 200
#: decisions_log rows that survive the cap (newest of each phase).
ANCHOR_PHASES = ("pre_movement", "city_event")
#: Optimistic-transaction attempts before a write is reported as a conflict.
MUTATE_MAX_ATTEMPTS = 25
#: Movement statuses that take no more GPS fixes (the movement is over).
CLOSED_STATUSES = ("completed", "cancelled")


class ContextWriteConflictError(RuntimeError):
    """A field update lost the WATCH race MUTATE_MAX_ATTEMPTS times in a row."""


@dataclass
class ConvoyContext:
    """In-memory representation of an active corridor event's state.

    The id may be a legacy movement id or a corridor-event id — both are
    corridor subjects; ``event_kind``/``crowd_class`` carry the public-
    event profile (defaults keep the protected-movement base case
    byte-identical through serialization).
    """

    movement_id: str
    vvip_class: str
    status: str = "planning"  # planning | approved | active | completed
    selected_route_id: str | None = None
    # Event-agnostic profile (core re-engineering, additive).
    event_kind: str = "protected_movement"
    crowd_class: str = "LOW"
    # Adaptive tier's situation classification for this movement (baseline
    # until the SituationEngine publishes otherwise).
    situation_class: str = "baseline"
    # The registered corridor event (events.corridor_events uuid) — written
    # by the pre-movement finalize step; escort start/clear move it through
    # the C-F lifecycle and C-B frames link to it.
    event_id: str | None = None
    # Operator label for the movement (e.g. 'ACCEPTANCE TEST escort'): it
    # prefixes the corridor event's title and rides as metadata.label, so a
    # test or rehearsal movement never reads as a real protected movement.
    label: str | None = None
    convoy_position: tuple[float, float] | None = None  # (lon, lat)
    convoy_speed_kmh: float = 0.0
    convoy_heading: float | None = None   # degrees clockwise from north
    position_t: str | None = None         # ISO-8601 UTC time of the latest fix
    position_source: str | None = None    # "gps" | "emulated_gps"
    active_diversions: list[str] = field(default_factory=list)
    decisions_log: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    # Blue Book protocol state — persisted to Valkey alongside movement
    protocol_state: dict = field(default_factory=lambda: {
        "asl_checklist": {
            "asl_meeting": False,
            "route_finalised": False,
            "route_survey": False,
            "antisab_sweep": False,
            "vehicle_checks": False,
            "driver_vetting": False,
            "comm_channels": False,
            "hospital_mapping": False,
            "safe_houses": False,
            "helipad_locations": False,
            "counter_surveillance": False,
            "rehearsal_completed": False,
        },
        "protocol_compliance": {
            "r1_no_last_minute_changes": True,
            "r2_police_arrangements": False,
            "r3_dedicated_road": True,
            "r4_dgp_chief_sec": False,
            "r5_contingency_rehearsed": False,
            "r6_same_make_vehicles": False,
            "r7_spg_director_clearance": False,
            "r8_realtime_updates": False,
            "r9_security_faces_crowd": True,
            "r10_incidents_logged": True,
        },
        "anti_sabotage": {
            "physical_search": False,
            "technical_gadgets": False,
            "sniffer_dogs": False,
        },
        "transit_status": {
            "ecm_active": False,
            "spg_clearance": False,
            "route_sanitised": False,
            "formation_intact": True,
        },
        "plan_b": {
            "active": False,
            "alt_route_sanitised": False,
            "alt_route_rehearsed": False,
            "contingency_motorcade_ready": False,
            "transport_fallback": False,
            "nearest_hospital": None,
            "nearest_safe_house": None,
            "activated_at": None,
            "reason": None,
        },
        "threat_level": "nominal",
        "dossier_generated_at": None,
    })

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dictionary."""
        d = asdict(self)
        # tuples become lists in asdict — preserve as-is for JSON
        return d

    @classmethod
    def from_dict(cls, data: dict) -> ConvoyContext:
        """Deserialize from a dictionary."""
        pos = data.get("convoy_position")
        if pos and isinstance(pos, list):
            data["convoy_position"] = tuple(pos)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def cap_decisions_log(rows: list[dict], limit: int = MAX_DECISIONS_LOG) -> list[dict]:
    """Keep the newest ``limit`` rows plus the newest row of each anchor phase.

    Order is preserved. An anchor older than the window replaces the oldest
    row inside it, so the result never exceeds ``limit`` rows.
    """
    if len(rows) <= limit:
        return rows
    keep = set(range(len(rows) - limit, len(rows)))
    anchors: list[int] = []
    for phase in ANCHOR_PHASES:
        for index in range(len(rows) - 1, -1, -1):
            row = rows[index]
            if isinstance(row, dict) and row.get("phase") == phase:
                anchors.append(index)
                break
    window = sorted(keep)
    for anchor in sorted(anchors):
        if anchor in keep:
            continue
        # Evict the oldest non-anchor row of the window for the anchor.
        for victim in window:
            if victim in keep and victim not in anchors:
                keep.discard(victim)
                break
        keep.add(anchor)
    return [rows[i] for i in sorted(keep)]


def _iso_to_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def apply_position(
    ctx: ConvoyContext,
    lon: float,
    lat: float,
    *,
    speed_kmh: float | None = None,
    heading: float | None = None,
    t: str | None = None,
    source: str = "gps",
) -> bool:
    """Write one fix into ``ctx``; False (untouched) when ``t`` is older than
    the stored fix — two producers racing can never move the convoy back."""
    new_epoch = _iso_to_epoch(t)
    old_epoch = _iso_to_epoch(ctx.position_t)
    if new_epoch is not None and old_epoch is not None and new_epoch < old_epoch:
        return False
    ctx.convoy_position = (float(lon), float(lat))
    ctx.convoy_speed_kmh = float(speed_kmh) if speed_kmh is not None else 0.0
    ctx.convoy_heading = float(heading) if heading is not None else None
    ctx.position_t = t
    ctx.position_source = source
    return True


class ConvoyContextStore:
    """Valkey-backed convoy context store for multi-agent workflows.

    Each convoy movement gets a dedicated key in Valkey with a 7-day TTL
    refreshed on every write.
    """

    def __init__(self, valkey_url: str = "redis://localhost:6379", client: Any = None) -> None:
        self._valkey_url = valkey_url
        self._client = client  # Valkey async client (lazy init; injectable for tests)
        # One lock per movement while a write is in flight (entries vanish
        # with their last user).
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        logger.info("convoy_context_store.init", valkey_url=valkey_url)

    def _lock_for(self, movement_id: str) -> asyncio.Lock:
        lock = self._locks.get(movement_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[movement_id] = lock
        return lock

    async def _ensure_client(self) -> None:
        """Lazy-initialize the Valkey client."""
        if self._client is None:
            import valkey.asyncio as valkey_async
            self._client = valkey_async.from_url(
                self._valkey_url, decode_responses=True
            )
            logger.info("convoy_context_store.connected")

    @staticmethod
    def _decode(movement_id: str, raw: str | None) -> ConvoyContext | None:
        if raw is None:
            return None
        try:
            return ConvoyContext.from_dict(json.loads(raw))
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning("convoy_context.get.corrupt", movement_id=movement_id, error=str(exc))
            return None

    @staticmethod
    def _encode(ctx: ConvoyContext) -> str:
        ctx.decisions_log = cap_decisions_log(ctx.decisions_log)
        if len(ctx.alerts) > MAX_ALERTS:
            ctx.alerts = ctx.alerts[-MAX_ALERTS:]
        return json.dumps(ctx.to_dict(), default=str)

    async def get(self, movement_id: str) -> ConvoyContext | None:
        """Retrieve convoy context from Valkey."""
        await self._ensure_client()
        key = f"{CONTEXT_KEY_PREFIX}{movement_id}"
        ctx = self._decode(movement_id, await self._client.get(key))
        if ctx is None:
            logger.debug("convoy_context.get.miss", movement_id=movement_id)
            return None
        logger.debug("convoy_context.get.hit", movement_id=movement_id, status=ctx.status)
        return ctx

    async def put(self, ctx: ConvoyContext) -> None:
        """Persist a whole context document (creation / plan finalize).

        Field updates of an existing context go through :meth:`mutate` and
        its named helpers instead — a whole-document write from a snapshot
        read earlier would discard whatever was written in between.
        """
        await self._ensure_client()
        key = f"{CONTEXT_KEY_PREFIX}{ctx.movement_id}"
        await self._client.set(key, self._encode(ctx), ex=CONTEXT_TTL_SEC)
        logger.debug("convoy_context.put", movement_id=ctx.movement_id, status=ctx.status)

    async def mutate(
        self,
        movement_id: str,
        fn: Callable[[ConvoyContext], bool | None],
        *,
        create: Callable[[], ConvoyContext] | None = None,
    ) -> ConvoyContext | None:
        """Apply ``fn`` to the freshest document atomically (WATCH/MULTI).

        ``fn`` edits the context in place and may return False to skip the
        write. It can run more than once (a lost race re-reads and re-applies),
        so it must not have side effects outside the context. A missing
        context is created with ``create()`` when given, else None is
        returned and nothing is written.
        """
        await self._ensure_client()
        from valkey.exceptions import WatchError

        key = f"{CONTEXT_KEY_PREFIX}{movement_id}"
        async with self._lock_for(movement_id):
            for attempt in range(MUTATE_MAX_ATTEMPTS):
                async with self._client.pipeline(transaction=True) as pipe:
                    try:
                        await pipe.watch(key)
                        ctx = self._decode(movement_id, await pipe.get(key))
                        if ctx is None:
                            if create is None:
                                return None
                            ctx = create()
                        if fn(ctx) is False:
                            return ctx
                        raw = self._encode(ctx)
                        pipe.multi()
                        pipe.set(key, raw, ex=CONTEXT_TTL_SEC)
                        await pipe.execute()
                        return ctx
                    except WatchError:
                        logger.debug("convoy_context.mutate.retry", movement_id=movement_id,
                                     attempt=attempt + 1)
                # Lost to another process: back off a little before re-reading.
                await asyncio.sleep(random.uniform(0.0, 0.002 * (attempt + 1)))
        raise ContextWriteConflictError(
            f"context {movement_id}: write lost the WATCH race "
            f"{MUTATE_MAX_ATTEMPTS} times"
        )

    async def update_fields(self, movement_id: str, **fields: Any) -> ConvoyContext | None:
        """Set top-level fields of an existing context (None when absent)."""
        unknown = [name for name in fields if name not in ConvoyContext.__dataclass_fields__]
        if unknown:
            raise ValueError(f"unknown ConvoyContext field(s): {unknown}")

        def apply(ctx: ConvoyContext) -> None:
            for name, value in fields.items():
                setattr(ctx, name, value)

        return await self.mutate(movement_id, apply)

    async def update_position(
        self,
        movement_id: str,
        lon: float,
        lat: float,
        *,
        speed_kmh: float | None = None,
        heading: float | None = None,
        t: str | None = None,
        source: str = "gps",
    ) -> tuple[ConvoyContext | None, bool]:
        """Record one GPS fix without touching any other field.

        Returns ``(ctx, applied)``: ctx is None for an unknown movement;
        ``applied`` is False when the fix is older than the stored one, or
        when the movement is closed (completed / cancelled — a fix after
        /clear would put the convoy back on the city picture).
        """
        outcome = {"applied": False}

        def apply(ctx: ConvoyContext) -> bool:
            if ctx.status in CLOSED_STATUSES:
                outcome["applied"] = False
                return False
            outcome["applied"] = apply_position(
                ctx, lon, lat, speed_kmh=speed_kmh, heading=heading, t=t, source=source
            )
            return outcome["applied"]

        ctx = await self.mutate(movement_id, apply)
        return ctx, outcome["applied"]

    async def append_decision(self, movement_id: str, decision: dict) -> None:
        """Append a decision record to the convoy's audit trail."""
        def apply(ctx: ConvoyContext) -> None:
            ctx.decisions_log.append(decision)

        def minimal() -> ConvoyContext:
            logger.warning("convoy_context.decision.no_context", movement_id=movement_id)
            return ConvoyContext(movement_id=movement_id, vvip_class="unknown")

        ctx = await self.mutate(movement_id, apply, create=minimal)
        logger.info(
            "convoy_context.decision",
            movement_id=movement_id,
            total_decisions=len(ctx.decisions_log) if ctx else 0,
        )

    async def list_active(self) -> list[ConvoyContext]:
        """List all active convoy movement contexts."""
        await self._ensure_client()
        keys = []
        async for key in self._client.scan_iter(match=f"{CONTEXT_KEY_PREFIX}*", count=100):
            keys.append(key)
        results = []
        for key in keys:
            raw = await self._client.get(key)
            if raw:
                try:
                    data = json.loads(raw)
                    ctx = ConvoyContext.from_dict(data)
                    if ctx.status in ("planning", "approved", "active"):
                        results.append(ctx)
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue
        return results

    async def update_protocol_state(
        self, movement_id: str, protocol_update: dict,
    ) -> ConvoyContext | None:
        """Merge a partial protocol state update into the convoy context."""
        def apply(ctx: ConvoyContext) -> None:
            # Deep-merge the protocol_state dict
            for key, value in protocol_update.items():
                current = ctx.protocol_state.get(key)
                if isinstance(current, dict) and isinstance(value, dict):
                    current.update(value)
                else:
                    ctx.protocol_state[key] = value

        ctx = await self.mutate(movement_id, apply)
        if ctx is None:
            logger.warning("convoy_context.protocol.no_context", movement_id=movement_id)
            return None
        logger.info("convoy_context.protocol_updated", movement_id=movement_id)
        return ctx

    # -- side keys -------------------------------------------------------------

    async def write_position_frame(self, movement_id: str, frame: dict) -> None:
        """Mirror one fix to ``convoy:position:{movement_id}`` (contract C-B)."""
        await self._ensure_client()
        await self._client.set(
            f"{POSITION_KEY_PREFIX}{movement_id}",
            json.dumps(frame, default=str),
            ex=POSITION_TTL_SEC,
        )

    async def read_position_frame(self, movement_id: str) -> dict | None:
        await self._ensure_client()
        return _loads_dict(await self._client.get(f"{POSITION_KEY_PREFIX}{movement_id}"))

    async def write_escort_status(self, movement_id: str, doc: dict) -> None:
        """Persist the escort status document (survives a brain restart)."""
        await self._ensure_client()
        await self._client.set(
            f"{ESCORT_KEY_PREFIX}{movement_id}",
            json.dumps(doc, default=str),
            ex=ESCORT_STATUS_TTL_SEC,
        )

    async def read_escort_status(self, movement_id: str) -> dict | None:
        await self._ensure_client()
        return _loads_dict(await self._client.get(f"{ESCORT_KEY_PREFIX}{movement_id}"))

    async def close(self) -> None:
        """Close the Valkey connection."""
        if self._client:
            await self._client.aclose()
            self._client = None


def _loads_dict(raw: str | None) -> dict | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


async def mutate_context(
    store: Any,
    movement_id: str,
    fn: Callable[[ConvoyContext], bool | None],
    *,
    create: Callable[[], ConvoyContext] | None = None,
) -> ConvoyContext | None:
    """Atomic field update through any context store.

    The Valkey store runs ``fn`` inside WATCH/MULTI (:meth:`ConvoyContextStore.mutate`).
    A minimal store exposing only ``get``/``put`` (the in-process test double)
    gets the plain read-modify-write, which is atomic there: nothing awaits
    between its read and its write.
    """
    mutate = getattr(store, "mutate", None)
    if mutate is not None:
        return await mutate(movement_id, fn, create=create)
    ctx = await store.get(movement_id)
    if ctx is None:
        if create is None:
            return None
        ctx = create()
    if fn(ctx) is False:
        return ctx
    await store.put(ctx)
    return ctx
