"""Async PostgreSQL connection pool management."""

from __future__ import annotations

import asyncpg
import structlog

logger = structlog.get_logger()


class DatabasePool:
    """Manages an asyncpg connection pool to PostGIS."""

    def __init__(self, dsn: str, min_size: int = 2, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def init(self) -> None:
        """Create the connection pool."""
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
        )
        logger.info("database pool initialized", dsn=self._dsn, max_size=self._max_size)

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("database pool closed")

    async def health_check(self) -> bool:
        """Verify database connectivity."""
        pool = self.get_pool()
        try:
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            logger.exception("database health check failed")
            return False

    def get_pool(self) -> asyncpg.Pool:
        """Return the active pool. Raises if not initialized."""
        if self._pool is None:
            raise RuntimeError("DatabasePool not initialized — call init() first")
        return self._pool
