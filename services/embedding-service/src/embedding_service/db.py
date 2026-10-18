"""Database pool creation for the embedding service (asyncpg).

The DSN comes from the environment only (VEER_DATABASE_DSN /
VVIP_DATABASE_URL / DATABASE_URL — same family the rest of the stack uses,
see infra/compose.yml). No credentials exist in source (C12).
"""

from __future__ import annotations

from urllib.parse import urlparse

from . import config

_ALLOWED_DSN_SCHEMES = ("postgres", "postgresql")


def resolve_dsn() -> str:
    """Return the configured DSN or raise with an actionable message."""
    dsn = config.database_dsn()
    if not dsn:
        raise RuntimeError(
            "no database DSN configured; set VEER_DATABASE_DSN"
        )
    scheme = urlparse(dsn).scheme.lower()
    if scheme not in _ALLOWED_DSN_SCHEMES:
        raise ValueError(f"DSN scheme {scheme!r} not allowed")
    return dsn


async def create_pool():
    """Create the real asyncpg pool (the only asyncpg import point)."""
    import asyncpg  # lazy: tests run without it

    return await asyncpg.create_pool(resolve_dsn(), min_size=1, max_size=4)


__all__ = ["create_pool", "resolve_dsn"]
