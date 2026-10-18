"""Degradation behavior — model slow/unavailable → cheaper tier, with
explicit status surfacing and anti-flap hysteresis.

The decision stack has three tiers (see ``DegradationTier``):

    FULL              decider (pplx-27B) + Gemma tactical
    TACTICAL_FALLBACK decider slow/unavailable → Gemma-only tactical
                      control + operator alert (plan: /decider/health
                      p95 > 3 s degrades to exactly this)
    RULES_ONLY        both tiers out → pure rules/baseline posture

Health snapshots are *fed in* (``observe_health``) — this module makes no
model calls and no HTTP requests.  Transitions require consecutive
evidence in one direction (degrade: ``degrade_confirm``; recover:
``recover_confirm`` at a stricter latency threshold, so a model hovering
at the boundary cannot flap), and every transition is appended to an
in-memory audit trail that status endpoints surface verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass

from traffic_oracle.adaptive.enums import DegradationTier


@dataclass(frozen=True)
class DegradationConfig:
    """Thresholds and confirmation counts; defaults are shipping values."""

    decider_slow_ms: float = 3000.0      # plan: p95 > 3 s → tactical fallback
    decider_recover_ms: float = 2000.0   # stricter recovery threshold (anti-flap)
    degrade_confirm: int = 2
    recover_confirm: int = 5
    min_dwell_sec: float = 30.0


@dataclass(frozen=True)
class ModelHealthSnapshot:
    """One health observation for one model role."""

    role: str                  # "decider" | "gemma"
    ok: bool
    latency_ms: float = 0.0
    ts_monotonic: float = 0.0

    def is_slow(self, slow_ms: float) -> bool:
        return self.latency_ms > slow_ms


@dataclass(frozen=True)
class AuditEntry:
    """One degradation transition, surfaced verbatim by /status."""

    ts_monotonic: float
    from_tier: str
    to_tier: str
    cause: str


@dataclass
class _RoleTracker:
    """Per-role consecutive-evidence counters."""

    slow_streak: int = 0
    healthy_streak: int = 0
    last_ok: bool | None = None
    last_latency_ms: float = 0.0
    was_out: bool = False


class DegradationManager:
    """Tracks model health and owns the degradation ladder.

    ``role_outage`` maps a role to whether it is currently considered
    unavailable; the decision tier is the worst of the roles it needs:

        decider out           → TACTICAL_FALLBACK (Gemma carries tactics)
        decider + gemma out   → RULES_ONLY
    """

    DECIDER = "decider"
    GEMMA = "gemma"

    def __init__(
        self,
        config: DegradationConfig | None = None,
        clock: object | None = None,
    ) -> None:
        self.config = config or DegradationConfig()
        self._clock = clock
        self.tier: DegradationTier = DegradationTier.FULL
        self.audit_trail: list[AuditEntry] = []
        self._trackers: dict[str, _RoleTracker] = {
            self.DECIDER: _RoleTracker(),
            self.GEMMA: _RoleTracker(),
        }
        self._last_transition_ts: float = 0.0

    # -- time ----------------------------------------------------------------

    def _now(self) -> float:
        if self._clock is not None:
            return float(self._clock())
        import time

        return time.monotonic()

    # -- health ingestion ------------------------------------------------------

    def observe_health(self, snapshot: ModelHealthSnapshot) -> DegradationTier:
        """Fold one health snapshot in; returns the (possibly new) tier."""
        cfg = self.config
        tracker = self._trackers.setdefault(snapshot.role, _RoleTracker())
        tracker.last_ok = snapshot.ok
        tracker.last_latency_ms = snapshot.latency_ms

        unhealthy = (not snapshot.ok) or snapshot.is_slow(cfg.decider_slow_ms)
        healthy = (
            snapshot.ok
            and snapshot.latency_ms <= cfg.decider_recover_ms
        )

        if unhealthy:
            tracker.slow_streak += 1
            tracker.healthy_streak = 0
        elif healthy:
            tracker.healthy_streak += 1
            tracker.slow_streak = 0
        else:
            # Between thresholds: neither evidence direction advances.
            tracker.slow_streak = 0
            tracker.healthy_streak = 0

        self._recompute(cause_role=snapshot.role)
        return self.tier

    def role_outage(self, role: str) -> bool:
        """True when ``role`` has enough evidence to be treated as out."""
        cfg = self.config
        tracker = self._trackers.get(role)
        if tracker is None:
            return False
        return tracker.slow_streak >= cfg.degrade_confirm

    # -- ladder ----------------------------------------------------------------

    def _recompute(self, cause_role: str) -> None:
        decider_out = self.role_outage(self.DECIDER)
        gemma_out = self.role_outage(self.GEMMA)

        for role, is_out in ((self.DECIDER, decider_out), (self.GEMMA, gemma_out)):
            if is_out:
                self._trackers[role].was_out = True

        if decider_out and gemma_out:
            target = DegradationTier.RULES_ONLY
        elif decider_out:
            target = DegradationTier.TACTICAL_FALLBACK
        else:
            target = DegradationTier.FULL

        # Recovery additionally requires the min dwell plus sustained
        # healthy evidence from every role that had dropped out, so a
        # flapping model cannot bounce the stack back up immediately.
        now = self._now()
        if target.rank < self.tier.rank:
            dwell_elapsed = (now - self._last_transition_ts) >= self.config.min_dwell_sec
            recovered = all(
                t.healthy_streak >= self.config.recover_confirm
                for t in self._trackers.values()
                if t.was_out
            )
            if not (dwell_elapsed and recovered):
                return

        if target is not self.tier:
            self.audit_trail.append(
                AuditEntry(
                    ts_monotonic=now,
                    from_tier=self.tier.code,
                    to_tier=target.code,
                    cause=f"health_evidence:{cause_role}",
                )
            )
            self.tier = target
            self._last_transition_ts = now

    # -- status surfacing --------------------------------------------------------

    def status(self) -> dict:
        """Explicit status block for /api/v1/adaptive/status."""
        cfg = self.config
        roles = {
            role: {
                "last_ok": t.last_ok,
                "last_latency_ms": t.last_latency_ms,
                "slow_streak": t.slow_streak,
                "healthy_streak": t.healthy_streak,
                "outage": self.role_outage(role),
            }
            for role, t in sorted(self._trackers.items())
        }
        return {
            "degradation_tier": self.tier.code,
            "decider_checkpoints_paused": self.tier is not DegradationTier.FULL,
            "adaptive_params_forced_baseline": (
                self.tier is DegradationTier.RULES_ONLY
            ),
            "thresholds": {
                "decider_slow_ms": cfg.decider_slow_ms,
                "decider_recover_ms": cfg.decider_recover_ms,
                "degrade_confirm": cfg.degrade_confirm,
                "recover_confirm": cfg.recover_confirm,
                "min_dwell_sec": cfg.min_dwell_sec,
            },
            "roles": roles,
            "audit_trail": [
                {
                    "ts_monotonic": e.ts_monotonic,
                    "from": e.from_tier,
                    "to": e.to_tier,
                    "cause": e.cause,
                }
                for e in self.audit_trail
            ],
        }

    # -- policy integration --------------------------------------------------------

    def forces_baseline(self) -> bool:
        """RULES_ONLY pins every published param set to baseline."""
        return self.tier is DegradationTier.RULES_ONLY


__all__ = [
    "AuditEntry",
    "DegradationConfig",
    "DegradationManager",
    "DegradationTier",
    "ModelHealthSnapshot",
]
