"""pgvector text codecs.

pgvector values cross the asyncpg wire as text with an explicit cast
parameter (``$n::vector``, ``$n::vector(128)[]``) — exactly the pattern the
legacy store uses (data/vector_store.py:21-29). These helpers serialise
Python floats to pgvector literal text and parse ``::text`` output back.

This module contains no SQL and no database access.
"""

from __future__ import annotations

import re
from typing import Sequence

from .encoders import TokenVecs

_ARRAY_ITEM_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def vector_to_text(vec: Sequence[float]) -> str:
    """Serialise one vector to pgvector literal text '[a,b,c]'."""
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


def vector_array_to_text(token_vecs: TokenVecs | None) -> str | None:
    """Serialise a token-vector set to a Postgres array literal.

    ``[[a,b],[c,d]]`` → ``{"[a,b]","[c,d]"}`` (matching vector(128)[]::text).
    Each element MUST be double-quoted: vector literals contain commas, and
    Postgres array input treats unquoted commas as element separators.
    Vector text never contains quotes/backslashes, so no escaping is needed.
    """
    if token_vecs is None:
        return None
    return "{" + ",".join('"' + vector_to_text(v) + '"' for v in token_vecs) + "}"


def vector_array_to_param(token_vecs: TokenVecs | None) -> list[str] | None:
    """Token-vector set in the form asyncpg ACCEPTS for $n::vector(128)[].

    Verified against pgvector 0.8.6: asyncpg introspects the parameter type
    of ``$n::vector(128)[]`` and REJECTS a single string ("a sized iterable
    container expected"), while a list of per-vector text literals binds
    cleanly and the server casts each element. This is the binding form the
    store uses; ``vector_array_to_text`` above remains the reference for
    the ``::text`` wire format.
    """
    if token_vecs is None:
        return None
    return [vector_to_text(v) for v in token_vecs]


def parse_vector_text(text: str) -> list[float]:
    """Parse '[a,b,c]' → [a, b, c]."""
    inner = text.strip().lstrip("[").rstrip("]")
    if not inner:
        return []
    return [float(part) for part in inner.split(",")]


def parse_vector_array_text(text: str | None) -> TokenVecs | None:
    """Parse vector(128)[]::text back to a token-vector set.

    ``{"[1,2]","[3,4]"}`` → [[1.0, 2.0], [3.0, 4.0]].
    """
    if text is None:
        return None
    stripped = text.strip()
    if stripped == "NULL":
        return None
    if stripped in ("", "{}"):
        return []
    items = _ARRAY_ITEM_RE.findall(stripped)
    if not items:
        return []
    return [parse_vector_text(item.replace('\\"', '"')) for item in items]


__all__ = [
    "parse_vector_array_text",
    "parse_vector_text",
    "vector_array_to_param",
    "vector_array_to_text",
    "vector_to_text",
]
