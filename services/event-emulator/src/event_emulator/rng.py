"""Deterministic seeding for the emulator (blake2b pattern).

Same discipline as traffic_oracle/simulation/rng.py: every random draw in a
cause tape goes through :class:`SeededRng`, which only consumes
``random.Random.random()`` — the one generator CPython guarantees
reproducible across versions — and derives every named distribution from
that single uniform stream.  Seed derivation uses ``hashlib.blake2b`` (not
process-randomized ``hash()``), so ``derive_seed("tape", use_case, seed)``
is identical on every machine, every run, every restart.
"""

from __future__ import annotations

import hashlib
import math
import random

__all__ = ["derive_seed", "SeededRng"]


def derive_seed(*parts: int | str) -> int:
    """Stable 63-bit seed from arbitrary parts (blake2b, PYTHONHASHSEED-free)."""
    payload = "|".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


class SeededRng:
    """Version-stable random source (uniform-stream transforms only)."""

    __slots__ = ("_rng",)

    def __init__(self, seed: int | str) -> None:
        if isinstance(seed, str):
            seed = derive_seed(seed)
        self._rng = random.Random(seed)

    def uniform(self, lo: float = 0.0, hi: float = 1.0) -> float:
        """Uniform on [lo, hi)."""
        return lo + (hi - lo) * self._rng.random()

    def triangular(self, lo: float, mode: float, hi: float) -> float:
        """Triangular draw on [lo, hi] with peak at ``mode``."""
        u = self._rng.random()
        span = hi - lo
        if span <= 0:
            return lo
        f = (mode - lo) / span
        if u < f:
            return lo + math.sqrt(u * f) * span
        return hi - math.sqrt((1.0 - u) * (1.0 - f)) * span
