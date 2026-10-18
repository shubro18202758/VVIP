"""Deterministic seeding utilities for the simulation twin.

Every random draw in the twin goes through :class:`SeededRng`, which wraps
``random.Random`` but only ever consumes ``random.Random.random()`` — the one
generator CPython guarantees reproducible across versions.  All named
distributions (exponential, lognormal, Poisson, weighted choice, triangular)
are implemented from that single uniform stream via explicit inverse-CDF
transforms, so a given seed produces bit-identical event schedules and metrics
on any Python 3.12+ host and across process restarts.

Seed derivation uses ``hashlib.blake2b`` (not Python's hash randomization,
which is process-randomized and therefore NOT reproducible).
"""

from __future__ import annotations

import hashlib
import math
import random

_MAX_U = 1.0 - 2.0 ** -53  # largest uniform draw strictly below 1.0


def derive_seed(*parts: int | str) -> int:
    """Stable 63-bit seed from arbitrary parts.

    ``derive_seed("scenario", 7, 3)`` is identical on every machine and every
    run; unlike ``hash()`` it is not affected by PYTHONHASHSEED.
    """
    payload = "|".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


class SeededRng:
    """Version-stable random source.

    Only ``random.Random.random()`` is consumed from the stdlib generator;
    every distribution below is an explicit deterministic transform of that
    uniform stream (inverse-CDF / Knuth), so sequences cannot drift with
    Python point releases the way ``.gauss``/``.expovariate`` internals could.
    """

    __slots__ = ("_rng",)

    def __init__(self, seed: int | str) -> None:
        if isinstance(seed, str):
            seed = derive_seed(seed)
        self._rng = random.Random(seed)

    def uniform(self, lo: float = 0.0, hi: float = 1.0) -> float:
        """Uniform on [lo, hi)."""
        return lo + (hi - lo) * self._rng.random()

    def expovariate(self, lam: float) -> float:
        """Exponential draw with rate ``lam`` (mean 1/lam)."""
        if lam <= 0:
            raise ValueError("expovariate rate must be > 0")
        u = self._rng.random() * _MAX_U
        return -math.log(max(u, 1e-300)) / lam

    def lognormal(self, mu: float, sigma: float) -> float:
        """Lognormal draw: ln(X) ~ N(mu, sigma^2) (Box–Muller, single value)."""
        u1 = max(self._rng.random(), 1e-18)
        u2 = self._rng.random()
        z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
        return math.exp(mu + sigma * z)

    def normal(self, mu: float, sigma: float) -> float:
        """Normal draw via Box–Muller (deterministic single-value form)."""
        return mu + sigma * self._standard_normal()

    def _standard_normal(self) -> float:
        u1 = max(self._rng.random(), 1e-18)
        u2 = self._rng.random()
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

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

    def poisson(self, lam: float) -> int:
        """Poisson draw (Knuth multiplication method, stable for lam < ~30)."""
        if lam <= 0:
            return 0
        if lam > 25.0:
            # Split large lambda into chunks to keep Knuth's loop bounded.
            k = lam // 25.0
            remainder = lam - k * 25.0
            total = sum(self.poisson(25.0) for _ in range(int(k)))
            return total + self.poisson(remainder)
        threshold = math.exp(-lam)
        n, p = 0, 1.0
        while True:
            p *= self._rng.random()
            if p <= threshold:
                return n
            n += 1

    def choice_index(self, weights: list[float]) -> int:
        """Weighted index choice; weights need not sum to 1."""
        total = sum(weights)
        if total <= 0:
            raise ValueError("weights must sum > 0")
        u = self._rng.random() * total
        acc = 0.0
        for i, w in enumerate(weights):
            acc += w
            if u < acc:
                return i
        return len(weights) - 1
