"""Emulation spec — a run is {emulation_spec, seed, t0_anchor, clock_scale}.

The spec hash (blake2b over the canonical, t0-free spec JSON) is what the
runs ledger records; t0_anchor is stored separately so a replay of the same
spec at a later anchor is recognizably the same cause program re-posted.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from event_emulator.library import UseCase

__all__ = ["EmulationSpec", "ResolvedRun"]


class EmulationSpec(BaseModel):
    """What the operator asks for; everything has a safe default."""

    use_case_id: str
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)
    t0_anchor: datetime | None = None     # None → resolve to now at run start
    clock_scale: float = Field(default=1.0, gt=0.0, le=3600.0)
    tick_sec: float = Field(default=5.0, ge=1.0, le=60.0)   # ≥ 1 s: bounded tapes
    deliver_nats: bool = True
    deliver_twin: bool = True

    def resolved_seed(self, use_case: UseCase) -> int:
        return self.seed if self.seed is not None else int(use_case.config["seed"])

    def canonical(self, use_case: UseCase) -> str:
        """Canonical JSON of the spec WITHOUT t0_anchor (t0 is run metadata)."""
        payload: dict[str, Any] = {
            "use_case_id": self.use_case_id,
            "seed": self.resolved_seed(use_case),
            "clock_scale": self.clock_scale,
            "tick_sec": self.tick_sec,
            "deliver_nats": self.deliver_nats,
            "deliver_twin": self.deliver_twin,
            "library_scenario": {
                "scenario_id": use_case.config["scenario_id"],
                "seed": use_case.config["seed"],
                "route_segment_ids": use_case.config["route_segment_ids"],
                "departure_time": use_case.config["departure_time"],
                "closure_policy": use_case.config["closure_policy"],
                "horizon_min": use_case.config["horizon_min"],
                "n_replications": use_case.config["n_replications"],
                "vvip_class": use_case.config["vvip_class"],
            },
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def spec_hash(self, use_case: UseCase) -> str:
        return hashlib.blake2b(
            self.canonical(use_case).encode("utf-8"), digest_size=16
        ).hexdigest()


class ResolvedRun(BaseModel):
    """A spec resolved against the library with its anchor fixed."""

    spec: EmulationSpec
    seed: int
    t0_anchor: datetime
    spec_hash: str

    @staticmethod
    def resolve(spec: EmulationSpec, use_case: UseCase) -> ResolvedRun:
        t0 = spec.t0_anchor or datetime.now(timezone.utc)
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=timezone.utc)
        return ResolvedRun(
            spec=spec,
            seed=spec.resolved_seed(use_case),
            t0_anchor=t0,
            spec_hash=spec.spec_hash(use_case),
        )
