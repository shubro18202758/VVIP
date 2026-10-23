"""Defensive JSON extraction from model output.

Both tiers emit JSON, but models wrap it in prose or markdown fences often
enough that every consumer funnels through :func:`extract_json`.
"""

from __future__ import annotations

import json
from typing import Any


def extract_json(text: str) -> Any | None:
    """Extract the first plausible JSON value from raw model output.

    Handles, in order:
        1. bare JSON documents
        2. ```json / ``` fenced blocks (leading or trailing)
        3. the outermost ``{ ... }`` or ``[ ... ]`` span in mixed prose
    """
    if not text:
        return None

    cleaned = text.strip()

    # Strip markdown fences (leading ```json or ```, trailing ```)
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1 :]
        else:
            cleaned = cleaned.lstrip("`").lstrip("json")
    if cleaned.rstrip().endswith("```"):
        cleaned = cleaned.rstrip().rstrip("`")
    cleaned = cleaned.strip()

    # 1. direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 2. outermost brace span
    brace_start = cleaned.find("{")
    brace_end = cleaned.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        try:
            return json.loads(cleaned[brace_start : brace_end + 1])
        except json.JSONDecodeError:
            pass

    # 3. outermost bracket span
    bracket_start = cleaned.find("[")
    bracket_end = cleaned.rfind("]")
    if bracket_start != -1 and bracket_end > bracket_start:
        try:
            return json.loads(cleaned[bracket_start : bracket_end + 1])
        except json.JSONDecodeError:
            pass

    return None


def rescue_string_field(text: str | None, key: str) -> str | None:
    """The first COMPLETE string value of ``key`` in a (possibly cut) JSON reply.

    A reply truncated at the token cap fails :func:`extract_json` although
    the field the operator reads (``"reasoning"``, written second by the
    schema) is often already closed. This scans for ``"key": "`` and reads
    the string escape-aware up to its closing quote; the escapes are then
    decoded by the JSON parser itself. None when the key is absent or its
    string never closed (a half sentence is never passed off as an answer).
    """
    if not text:
        return None
    needle = f'"{key}"'
    start = 0
    while True:
        at = text.find(needle, start)
        if at < 0:
            return None
        i = at + len(needle)
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i < len(text) and text[i] == ":":
            break
        start = at + 1  # the key's name appeared inside another string
    i += 1
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    if i >= len(text) or text[i] != '"':
        return None
    j = i + 1
    while j < len(text):
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == '"':
            try:
                value = json.loads(text[i : j + 1])
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, str) else None
        j += 1
    return None


def truncate_for_context(text: str, max_bytes: int = 4096) -> str:
    """UTF-8-safe truncation for tool results fed back into model context."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "…[truncated]"
