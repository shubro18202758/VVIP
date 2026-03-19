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
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

import structlog

logger = structlog.get_logger(__name__)

CONTEXT_KEY_PREFIX = "convoy:context:"
CONTEXT_TTL_SEC = 86400  # 24 hours — auto-expire after movement completion


@dataclass
class ConvoyContext:
    """In-memory representation of an active convoy movement's state."""

    movement_id: str
    vvip_class: str
    status: str = "planning"  # planning | approved | active | completed
    selected_route_id: str | None = None
    convoy_position: tuple[float, float] | None = None  # (lon, lat)
    convoy_speed_kmh: float = 0.0
    active_diversions: list[str] = field(default_factory=list)
    decisions_log: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)

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


class ConvoyContextStore:
    """Valkey-backed convoy context store for multi-agent workflows.

    Each convoy movement gets a dedicated key in Valkey with TTL-based
    expiry after movement completion.
    """

    def __init__(self, valkey_url: str = "redis://localhost:6379") -> None:
        self._valkey_url = valkey_url
        self._client = None  # Valkey async client (lazy init)
        logger.info("convoy_context_store.init", valkey_url=valkey_url)

    async def _ensure_client(self) -> None:
        """Lazy-initialize the Valkey client."""
        if self._client is None:
            import valkey.asyncio as valkey_async
            self._client = valkey_async.from_url(
                self._valkey_url, decode_responses=True
            )
            logger.info("convoy_context_store.connected")

    async def get(self, movement_id: str) -> ConvoyContext | None:
        """Retrieve convoy context from Valkey."""
        await self._ensure_client()
        key = f"{CONTEXT_KEY_PREFIX}{movement_id}"
        raw = await self._client.get(key)
        if raw is None:
            logger.debug("convoy_context.get.miss", movement_id=movement_id)
            return None
        try:
            data = json.loads(raw)
            ctx = ConvoyContext.from_dict(data)
            logger.debug("convoy_context.get.hit", movement_id=movement_id, status=ctx.status)
            return ctx
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning("convoy_context.get.corrupt", movement_id=movement_id, error=str(exc))
            return None

    async def put(self, ctx: ConvoyContext) -> None:
        """Persist convoy context to Valkey."""
        await self._ensure_client()
        key = f"{CONTEXT_KEY_PREFIX}{ctx.movement_id}"
        raw = json.dumps(ctx.to_dict())
        ttl = CONTEXT_TTL_SEC if ctx.status == "completed" else 0
        if ttl:
            await self._client.setex(key, ttl, raw)
        else:
            await self._client.set(key, raw)
        logger.debug("convoy_context.put", movement_id=ctx.movement_id, status=ctx.status)

    async def append_decision(self, movement_id: str, decision: dict) -> None:
        """Append a decision record to the convoy's audit trail."""
        await self._ensure_client()
        ctx = await self.get(movement_id)
        if ctx is None:
            logger.warning("convoy_context.decision.no_context", movement_id=movement_id)
            # Create a minimal context to persist the decision
            ctx = ConvoyContext(movement_id=movement_id, vvip_class="unknown")
        ctx.decisions_log.append(decision)
        await self.put(ctx)
        logger.info(
            "convoy_context.decision",
            movement_id=movement_id,
            total_decisions=len(ctx.decisions_log),
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

    async def close(self) -> None:
        """Close the Valkey connection."""
        if self._client:
            await self._client.aclose()
            self._client = None
