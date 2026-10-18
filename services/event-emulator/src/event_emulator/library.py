"""UseCaseLibrary loader — the 8 scenario configs, consumed verbatim.

The JSON file next to this module is the UseCaseLibrary handoff, embedded
byte-for-byte.  Nothing here rewrites, renames, or reinterprets a config
value; the loader only VALIDATES shape so a bad handoff fails closed at
import time instead of silently injecting a malformed cause.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

__all__ = ["UseCase", "load_library", "LIBRARY_PATH", "KNOWN_EVENT_TYPES"]

LIBRARY_PATH = Path(__file__).with_name("use_case_library.json")

# The five EventTypes the twin defines today (simulation/events.py:33-38)
# plus the additive EMERGENCY_VEHICLE cause (the deterministic counterpart of
# the replication-random ambulance Poisson, engine.py:431-433).  The tape may
# carry all six; 'emergency_request' is NOT a kind (deleted primitive, C4).
KNOWN_EVENT_TYPES = (
    "vehicle_breakdown",
    "waterlogging",
    "rally_spillover",
    "market_spillover",
    "signal_fault",
    "emergency_vehicle",
)

_KNOWN_CLOSURE_TYPES = ("full_closure", "partial_closure", "signal_hold")
_KNOWN_VVIP_CLASSES = ("Z+", "Z", "Y", "X")


@dataclass(frozen=True)
class UseCase:
    """One UseCaseLibrary entry: an id plus its verbatim scenarioConfig."""

    id: str
    config: dict

    @property
    def horizon_sec(self) -> float:
        return float(self.config["horizon_min"]) * 60.0


def _validate(entry: dict, path: Path) -> None:
    if "id" not in entry or "scenarioConfig" not in entry:
        raise ValueError(f"{path}: library entry missing id/scenarioConfig")
    cfg = entry["scenarioConfig"]
    if cfg.get("scenario_id") != entry["id"]:
        raise ValueError(f"{path}: scenario_id {cfg.get('scenario_id')!r} != id {entry['id']!r}")
    required = (
        "name", "category", "route_segment_ids", "departure_time", "vvip_class",
        "closure_policy", "horizon_min", "n_replications", "seed",
        "forced_events", "event_overrides", "weather",
    )
    for key in required:
        if key not in cfg:
            raise ValueError(f"{path}: {entry['id']}: missing scenarioConfig key {key!r}")
    if not cfg["route_segment_ids"] or not all(isinstance(s, int) for s in cfg["route_segment_ids"]):
        raise ValueError(f"{path}: {entry['id']}: route_segment_ids must be a non-empty int list")
    if cfg["vvip_class"] not in _KNOWN_VVIP_CLASSES:
        raise ValueError(f"{path}: {entry['id']}: unknown vvip_class {cfg['vvip_class']!r}")
    if cfg["closure_policy"].get("closure_type") not in _KNOWN_CLOSURE_TYPES:
        raise ValueError(f"{path}: {entry['id']}: unknown closure_type")
    if not (1 <= int(cfg["horizon_min"]) <= 60):
        raise ValueError(f"{path}: {entry['id']}: horizon_min outside 1..60 (twin wall)")
    if not (1 <= int(cfg["n_replications"]) <= 20):
        raise ValueError(f"{path}: {entry['id']}: n_replications outside 1..20 (twin wall)")
    for fe in cfg["forced_events"]:
        if fe.get("type") not in KNOWN_EVENT_TYPES:
            raise ValueError(f"{path}: {entry['id']}: unknown forced_events type {fe.get('type')!r}")


def load_library(path: Path = LIBRARY_PATH) -> dict[str, UseCase]:
    """Parse + validate the UseCaseLibrary.  Fails closed on any drift."""
    if not path.is_file():
        raise FileNotFoundError(
            f"UseCaseLibrary file missing: {path} — the emulator fails closed "
            "rather than run on a half-present library"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"{path}: expected a non-empty JSON array of use cases")
    out: dict[str, UseCase] = {}
    for entry in data:
        _validate(entry, path)
        uc = UseCase(id=entry["id"], config=entry["scenarioConfig"])
        if uc.id in out:
            raise ValueError(f"{path}: duplicate use-case id {uc.id!r}")
        out[uc.id] = uc
    return out
