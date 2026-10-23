"""Parameter-bound persistence for the adaptive audit tables.

Tables live in migration ``corridor-store/migrations/005_adaptive.sql``:
``adaptive.situation_log`` and ``adaptive.param_overrides``.

Both statements are 100% parameter-bound (asyncpg ``$n`` placeholders,
values passed positionally — never interpolated into the SQL text; each
query is a static literal defined at its call site).  A ``None`` pool
disables persistence: the service runs fine without a DB, the drop is
logged once, and serving (the C4 HTTP response) is never blocked by
audit-path failures.
"""

from __future__ import annotations

import json

import structlog

logger = structlog.get_logger(__name__)


class AsyncpgPersister:
    """``PersisterPort`` backed by an asyncpg pool; errors never propagate."""

    def __init__(self, pool: object | None) -> None:
        self._pool = pool
        self._warned = pool is None
        self.written_situations = 0
        self.written_params = 0

    async def log_situation(self, record: dict) -> None:
        """Append one row to adaptive.situation_log (all values bound).

        ``protection_class`` (migration 007) is written alongside the
        legacy ``vvip_class`` column with the same value — the generalized
        name of the tier axis ('none' = public/civic event).
        """
        if self._pool is None:
            if not self._warned:
                logger.warning("adaptive.persistence_disabled", table="situation_log")
                self._warned = True
            return
        query = "INSERT INTO adaptive.situation_log (movement_id, vvip_class, situation_code, situation_class, severity_score, inputs, observed_class, transitioned, protection_class, observed_at) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, NOW())"  # noqa: E501
        movement_id = str(record["movement_id"])
        vvip_class = str(record["vvip_class"])
        situation_code = str(record["situation_code"])
        situation_class = str(record["situation_class"])
        severity_score = int(record["severity_score"])
        inputs_json = json.dumps(record["inputs"])
        observed_class = str(record["observed_class"])
        transitioned = bool(record["transitioned"])
        try:
            async with self._pool.acquire() as conn:  # type: ignore[attr-defined]
                await conn.execute(  # type: ignore[attr-defined]
                    query,
                    movement_id,
                    vvip_class,
                    situation_code,
                    situation_class,
                    severity_score,
                    inputs_json,
                    observed_class,
                    transitioned,
                    vvip_class,  # protection_class (007)
                )
            self.written_situations += 1
        except Exception as exc:  # noqa: BLE001 - audit must not break serving
            logger.warning(
                "adaptive.situation_log_failed",
                movement_id=record.get("movement_id"),
                error=str(exc),
            )

    async def log_params(self, record: dict) -> bool | None:
        """Append one row to adaptive.param_overrides (all values bound).

        Returns True when the row landed, False when it did not (the
        service then re-attempts it on its next decision — rows are written
        only on change, so a dropped one would leave a decision change
        unaudited), None when persistence is disabled (no pool).
        """
        if self._pool is None:
            return None
        query = "INSERT INTO adaptive.param_overrides (movement_id, vvip_class, situation_class, params, source, shadow_eval, evidence, protection_class, published_at) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7::jsonb, $8, NOW())"  # noqa: E501
        movement_id = str(record["movement_id"])
        vvip_class = str(record["vvip_class"])
        situation_class = str(record["situation_class"])
        params_json = json.dumps(record["params"])
        source = str(record["source"])
        shadow_eval = str(record["shadow_eval"])
        evidence_json = json.dumps(record["evidence"])
        try:
            async with self._pool.acquire() as conn:  # type: ignore[attr-defined]
                await conn.execute(  # type: ignore[attr-defined]
                    query,
                    movement_id,
                    vvip_class,
                    situation_class,
                    params_json,
                    source,
                    shadow_eval,
                    evidence_json,
                    vvip_class,  # protection_class (007)
                )
            self.written_params += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "adaptive.param_overrides_log_failed",
                movement_id=record.get("movement_id"),
                error=str(exc),
            )
            return False
        return True


class NullPersister:
    """Explicit no-op persister."""

    async def log_situation(self, record: dict) -> None:
        _ = record

    async def log_params(self, record: dict) -> bool | None:
        _ = record
        return None


__all__ = ["AsyncpgPersister", "NullPersister"]
