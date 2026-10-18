"""Loader for the hand-authored agent JSON schemas.

The schemas live at ``agents/schemas/*.json`` and are derived from the JSON
examples embedded in the agent prompt strings — no machine-readable schema
existed before these files. They are used for llama-server schema-constrained
decoding (``response_format: json_schema``) and for post-hoc validation.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "agents" / "schemas"

_KNOWN = (
    "traffic_analyst",
    "route_planner",
    "security_liaison",
    "diversion_coordinator",
    "orchestrator_final",
    "tool_arguments",
)


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict:
    """Load a schema by name (without .json suffix). Raises KeyError if absent."""
    path = SCHEMA_DIR / f"{name}.json"
    if not path.is_file():
        raise KeyError(
            f"Unknown agent schema {name!r}; expected one of {_KNOWN} in {SCHEMA_DIR}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def known_schemas() -> tuple[str, ...]:
    return _KNOWN
