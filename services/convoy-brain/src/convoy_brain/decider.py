"""Strategic tier — the decision model as brain/orchestrator.

The decider (pplx-decider-v1-27b behind model-host) owns strategic choice and
TOOL SELECTION via typed choice/noul/score questions. It is NOT a chat model.

Event-driven cadence (red-team F8 correction — never per-GPS-tick):
    checkpoints fire only on
        1. incident_detected
        2. congestion-threshold crossing
        3. situation_class change (Valkey key ``situation:changed``)
        4. phase transitions (plan / escort / clear)
        5. operator chat
    with >= DECIDER_MIN_CHECKPOINT_INTERVAL_SEC between checkpoints per
    movement. The live-escort GPS loop itself is LLM-free.

Degradation: when /decider/health reports p95 > DECIDER_P95_DEGRADE_MS (or the
host is unreachable), the brain drops to Gemma-only tactical mode and raises
an operator alert — every transition is audit-logged via the decisions_log.

Confidence gating: any answer with confidence < CONFIDENCE_FLOOR triggers a
Gemma draft plus an operator alert instead of being acted on directly.
Answers whose type carries no confidence (noul) are EXCLUDED from the
aggregation — None means "not applicable", never 0.0 (calibrated
2026-10-19: the previous None→0.0 coercion made every noul-bearing
question set permanently low_confidence). CONFIDENCE_FLOOR defaults to 0.45
(env VVIP_CONFIDENCE_FLOOR), calibrated against the live 27B's measured
pre-movement confidences (0.5033 / 0.4696) — see config.py.

Novelty latch (the complement of the confidence gate): a CONFIDENT answer
whose situation has no similar memory (embedding /search best maxsim below
NOVELTY_SIMILARITY_FLOOR, or zero hits) is withheld — the checkpoint returns
tier "novelty_latched", which the orchestrator treats like any non-decider
tier (tactical Gemma takes over), and an operator alert fires. An
unreachable memory service disables the latch honestly per checkpoint
(alert + audit) WITHOUT benching the strategic tier. MaxSim and
dense_fallback scores are never compared: an all-dense-fallback result is
"insufficient_evidence", never novel. Low-confidence answers never
double-escalate — the confidence gate already covers them.

Tier-0 gate (routine-checkpoint cache): BEFORE the 27B /decide call, an
eligible trigger (operator_chat excluded in v1 — the orchestrator trusts
only tier "decider" for tool selection) whose situation is memory-covered
(best maxsim above TIER0_ROUTINE_SIMILARITY_FLOOR — strictly tighter than
the novelty floor, reusing the Wave-1 novelty machinery above) consults a
small TTL-bounded cache keyed per movement by (trigger, canonical
questions, best_doc_id); movement scoping is load-bearing because the
embedding service ranks by movement but does not filter on it. A hit
serves the cached recent CONFIDENT answer as tier "tier0_cache_hit" with
ZERO calls to the model host and consumes cadence like a real decide;
novelty, below-routine similarity, or an absent/expired entry forces a
miss and today's flow runs unchanged. The similarity consult is fail-OPEN
— memory outages never bench the tier — and the write side mirrors the
read side: low-confidence, latched, and novel answers never enter the
cache. For a TIER0_OWN_DOC_TRIGGERS trigger (pre_movement) a stored
answer is ALSO filed for the movement's own decision docs: it is served
when the best memory is any decision doc this movement's checkpoints of
that trigger recorded (the search reports the doc's ref_id, which names
movement and trigger) and the question set is identical. Its best-doc key
alone carries the doc seen BEFORE the feeder upserted the situation's own
doc, which no repeat matches again — so a pre-movement question set, which
names the plan's candidates and varies with the tactical tier's trimming,
could need three occurrences to hit, and alternating sets never did.

Decision-memory feeder (the corpus half the gate reads): after a SERVED
27B checkpoint (tier "decider" — low-confidence answers included, they
are still served with a Gemma draft), the checkpoint's state document is
upserted into the embedding corpus as kind=decision (ref_id mirrors the
tier-0 key: movement/trigger/questions signature, so a re-decided
situation refreshes one doc instead of duplicating rows). Without the
feeder the corpus the gate searches never gains a decision doc and every
live checkpoint is miss_novel forever, no matter how routine. The feeder
runs AFTER the checkpoint's own /search — a situation's first occurrence
can still be novel (Wave-1 latch semantics preserved); repeats become
"on record". It records SITUATIONS, never answers: which answers may be
served from the tier-0 cache stays gated exactly as above. Fail-open and
fire-and-log like the rest of the memory machinery: a feeder outage
counts a metric and never breaks a checkpoint.

The feeder has an off switch: VVIP_MEMORY_RECORD=0 (config
decision_memory_record_enabled; default on) leaves the corpus untouched —
checkpoints still SEARCH it, nothing is upserted, and every skipped record
counts as outcome ``skipped_disabled``. Sandbox brains run with it off.

A WITHHELD (novelty_latched) checkpoint records its situation too, with
``metadata.withheld=true``: the tactical tier and the operator handled the
first occurrence, so the situation is now on record. Before this, only
served answers were recorded — a confidently-answered novel situation was
withheld, never recorded, and therefore latched forever (the self-lock).
The latch's memo for that exact (movement, state) is dropped once the
record lands, so an immediate repeat re-searches and reads "covered".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from convoy_brain.config import (
    CONFIDENCE_FLOOR,
    DECIDER_MIN_CHECKPOINT_INTERVAL_SEC,
    DECIDER_P95_DEGRADE_MS,
    DECIDER_PROBE_TIMEOUT_SEC,
    DECISION_MEMORY_RECORD_TIMEOUT_SEC,
    NOVELTY_MEMO_TTL_SEC,
    NOVELTY_SEARCH_TIMEOUT_SEC,
    NOVELTY_SIMILARITY_FLOOR,
    STATE_TOKENS_HARD_CAP,
    STATE_TOKENS_PER_EVENT,
    STATE_TOKENS_PRE_MOVEMENT,
    TIER0_CACHE_MAX_ENTRIES,
    TIER0_CACHE_TTL_SEC,
    TIER0_ELIGIBLE_TRIGGERS,
    TIER0_OWN_DOC_TRIGGERS,
    TIER0_ROUTINE_SIMILARITY_FLOOR,
    decision_memory_record_enabled,
)
from convoy_brain.llm.decider_client import DeciderAnswer, DeciderClient, DeciderUnavailable
from convoy_brain.memory.memory_client import MemoryClient, MemorySearchResult
from convoy_brain.metrics import MEMORY_RECORD_EVENTS, TIER0_CACHE_EVENTS, TIER0_LATENCY_SAVED_MS

logger = structlog.get_logger(__name__)

#: Trigger kinds that legitimately start a decider checkpoint.
CHECKPOINT_TRIGGERS = (
    "incident_detected",
    "congestion_threshold_crossed",
    "situation_changed",
    "phase_transition",
    "operator_chat",
    "pre_movement",
)


#: Rough token estimate — 4 characters per token is the standard approximation
#: for state documents; the model-host enforces the hard cap server-side too.
def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _questions_signature(questions: dict[str, dict]) -> str:
    """Canonical signature of a question set (cache-key component).

    ``json.dumps(sort_keys=True)`` makes dict ordering irrelevant; the raw
    state document is deliberately NOT part of the key (it embeds per-tick
    values, so exact-doc keys would never hit twice).
    """
    return hashlib.sha256(
        json.dumps(questions, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


#: Hex digits of the questions signature at the end of a decision doc's ref_id.
DECISION_REF_SIGNATURE_HEX = 16


def _decision_ref_prefix(movement_id: str, trigger: str) -> str:
    """``decision:{movement_id}:{trigger}:`` — the feeder's ref_id up to the
    questions signature (:meth:`DeciderBrain._record_decision_memory`)."""
    return f"decision:{movement_id}:{trigger}:"


def _is_own_decision_ref(ref_id: Any, movement_id: str, trigger: str) -> bool:
    """True when ``ref_id`` names a decision doc THIS movement's checkpoints of
    this trigger recorded: the exact prefix, then only the signature's hex
    digits (so a movement id that itself contains ':' can never pass for
    another movement's)."""
    if not isinstance(ref_id, str):
        return False
    prefix = _decision_ref_prefix(movement_id, trigger)
    tail = ref_id[len(prefix):]
    return (
        ref_id.startswith(prefix)
        and len(tail) == DECISION_REF_SIGNATURE_HEX
        and all(c in "0123456789abcdef" for c in tail)
    )


def _round_conf(confidence: float | None) -> float | None:
    """Audit-safe rounding that preserves absent confidence as None.

    Noul answers carry no confidence (the readout head exposes none for
    none-of-the-above likelihood); the audit row labels that honestly as
    null instead of fabricating 0.0.
    """
    return round(confidence, 4) if confidence is not None else None


@dataclass
class CheckpointResult:
    """Outcome of one decider checkpoint, including the audit record."""

    tier: str  # "decider" | "novelty_latched" (confident answer withheld) | "tier0_cache_hit" (routine answer served from cache, no /decide) | "gemma" (fallback) | "rate_limited" | "unavailable"
    answers: dict[str, DeciderAnswer] = field(default_factory=dict)
    degraded_reason: str | None = None
    operator_alerts: list[dict] = field(default_factory=list)
    audit: dict = field(default_factory=dict)


class DeciderBrain:
    """Wraps the DeciderClient with cadence, budget, and degradation policy."""

    def __init__(
        self,
        client: DeciderClient,
        min_interval_sec: float = DECIDER_MIN_CHECKPOINT_INTERVAL_SEC,
        confidence_floor: float = CONFIDENCE_FLOOR,
        *,
        memory_client: MemoryClient | None = None,
        novelty_floor: float = NOVELTY_SIMILARITY_FLOOR,
        novelty_timeout_sec: float = NOVELTY_SEARCH_TIMEOUT_SEC,
        memo_ttl_sec: float = NOVELTY_MEMO_TTL_SEC,
        clock=time.monotonic,
        tier0_enabled: bool = True,
        tier0_ttl_sec: float = TIER0_CACHE_TTL_SEC,
        tier0_floor: float = TIER0_ROUTINE_SIMILARITY_FLOOR,
        tier0_max_entries: int = TIER0_CACHE_MAX_ENTRIES,
        tier0_triggers: frozenset[str] = TIER0_ELIGIBLE_TRIGGERS,
        memory_record_enabled: bool | None = None,
        tier0_own_doc_triggers: frozenset[str] = TIER0_OWN_DOC_TRIGGERS,
    ) -> None:
        self._client = client
        # The feeder's write side (VVIP_MEMORY_RECORD, default on).
        self._memory_record_enabled = (
            decision_memory_record_enabled() if memory_record_enabled is None
            else bool(memory_record_enabled)
        )
        self._min_interval = min_interval_sec
        self._confidence_floor = confidence_floor
        self._memory_client = memory_client
        self._novelty_floor = novelty_floor
        self._novelty_timeout = novelty_timeout_sec
        self._memo_ttl_sec = memo_ttl_sec
        self._clock = clock
        self._novelty_memo: dict[tuple[str, str], tuple[float, MemorySearchResult]] = {}
        self._tier0_enabled = tier0_enabled
        self._tier0_ttl_sec = tier0_ttl_sec
        self._tier0_floor = tier0_floor
        self._tier0_max_entries = tier0_max_entries
        self._tier0_triggers = frozenset(tier0_triggers)
        self._tier0_own_doc_triggers = frozenset(tier0_own_doc_triggers)
        # Keyed (movement_id, trigger, questions_signature, best_doc_id):
        # MOVEMENT-SCOPED so one movement can never be served another
        # movement's cached decision — the embedding service treats
        # movement_id as a soft rank preference, not a filter, so a
        # different movement's situation can legitimately surface the same
        # best_doc_id; without the movement component its first-ever
        # checkpoint would hit and record another movement's answer. The raw
        # state_doc is NEVER a key (docs embed per-tick values — exact-doc
        # keys would never hit twice); best_doc_id churn on re-index degrades
        # to a safe miss, and the TTL is the staleness bound. FIFO-bounded
        # below.
        self._tier0_cache: dict[tuple[str, str, str, str], dict] = {}
        self._last_checkpoint_at: dict[str, float] = {}
        self._degraded: bool = False
        self._degraded_reason: str | None = None
        logger.info(
            "decider_brain.init",
            min_interval_sec=min_interval_sec,
            novelty_latch_enabled=memory_client is not None,
            tier0_enabled=tier0_enabled,
        )

    # -- state documents -------------------------------------------------------

    @staticmethod
    def cap_state_doc(text: str, budget_tokens: int) -> str:
        """Trim a state document to its token budget (hard cap applies)."""
        budget = min(budget_tokens, STATE_TOKENS_HARD_CAP)
        if estimate_tokens(text) <= budget:
            return text
        return text[: budget * 4]

    @classmethod
    def build_state_doc(
        cls,
        *,
        trigger: str,
        summary: str,
        context: dict[str, Any] | None = None,
        pre_movement: bool = False,
    ) -> str:
        """Compose the compact state document for one checkpoint.

        Budgets: <=1500 tokens pre-movement, <=400 tokens per event
        (contract T5), hard cap 8192.
        """
        budget = STATE_TOKENS_PRE_MOVEMENT if pre_movement else STATE_TOKENS_PER_EVENT
        lines = [f"TRIGGER: {trigger}", summary.strip()]
        if context:
            for key, value in context.items():
                lines.append(f"{key}: {value}")
        return cls.cap_state_doc("\n".join(lines), budget)

    # -- cadence ----------------------------------------------------------------

    def checkpoint_due(self, movement_id: str) -> bool:
        """True when the per-movement rate limit allows a new checkpoint."""
        last = self._last_checkpoint_at.get(movement_id)
        return last is None or (time.monotonic() - last) >= self._min_interval

    def _mark_checkpoint(self, movement_id: str) -> None:
        self._last_checkpoint_at[movement_id] = time.monotonic()

    # -- degradation --------------------------------------------------------------

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    @property
    def novelty_latch_enabled(self) -> bool:
        """True when the memory client is wired (the latch is armed)."""
        return self._memory_client is not None

    @property
    def tier0_enabled(self) -> bool:
        """True when the tier-0 routine-checkpoint cache gate is on."""
        return self._tier0_enabled

    @property
    def memory_record_enabled(self) -> bool:
        """True when served checkpoints are upserted into the memory corpus."""
        return self._memory_record_enabled

    async def refresh_health(self) -> dict:
        """Poll /decider/health; flip degradation state on p95 threshold.

        The server-side p95 is a rolling window over past /decide calls.
        Once the brain degrades, no decides flow, so the window never
        refreshes and a single bad stretch benched the strategic tier
        forever (the sticky-latch bug: a 7.2 s p95 from cold-start decides
        kept the tier degraded while direct probes answered in ~0.9 s).
        Before acting on a p95 breach — whether to degrade or to stay
        degraded — we verify with ONE real probe decide that must beat the
        p95 line. A fast probe also appends a fresh latency sample
        server-side, so the window recovers on its own.
        """
        health = await self._client.health()
        loaded = bool(health.get("loaded"))
        p95 = self._client.p95_decide_latency_ms

        if not health or not loaded:
            self._set_degradation("model-host unreachable or model not loaded")
        elif p95 is not None and p95 > DECIDER_P95_DEGRADE_MS:
            if await self._probe_decide():
                # Fresh evidence contradicts the stale window: the strategic
                # tier is answering fast right now. Keep/restore it.
                if self._degraded:
                    logger.info(
                        "decider_brain.probe_recovered",
                        stale_p95_ms=round(p95, 1),
                    )
                    self._clear_degradation()
            else:
                self._set_degradation(
                    f"/decider/health p95 {p95:.0f} ms exceeds {DECIDER_P95_DEGRADE_MS} ms "
                    "(probe decide failed or exceeded the p95 line)"
                )
        elif self._degraded:
            self._clear_degradation()

        return health

    async def _probe_decide(self) -> bool:
        """One minimal /decide probe that must complete under the p95 line.

        Rate-limited by construction: called only from :meth:`refresh_health`
        (every ``DECIDER_HEALTH_POLL_SEC``) and never from the hot path. Any
        exception, malformed answer, or slow completion counts as unhealthy.
        """
        started = time.monotonic()
        try:
            await asyncio.wait_for(
                self._client.decide(
                    state="TRIGGER: health_probe\nstrategic-tier latency probe",
                    questions={
                        "probe": {
                            "type": "noul",
                            "instructions": (
                                "Health probe (none-of-the-above likelihood): "
                                "is an unscheduled strategic checkpoint needed now?"
                            ),
                        }
                    },
                ),
                timeout=DECIDER_PROBE_TIMEOUT_SEC,
            )
        except Exception as exc:
            logger.info("decider_brain.probe_failed", error=str(exc))
            return False
        elapsed_ms = (time.monotonic() - started) * 1000
        return elapsed_ms <= DECIDER_P95_DEGRADE_MS

    def _set_degradation(self, reason: str) -> None:
        if not self._degraded:
            logger.warning("decider_brain.degraded_gemma_only", reason=reason)
        self._degraded = True
        self._degraded_reason = reason

    def _clear_degradation(self) -> None:
        if self._degraded:
            logger.info("decider_brain.recovered", reason=self._degraded_reason)
        self._degraded = False
        self._degraded_reason = None

    # -- checkpoints ---------------------------------------------------------------

    async def checkpoint(
        self,
        movement_id: str,
        trigger: str,
        state_doc: str,
        questions: dict[str, dict],
        *,
        force: bool = False,
    ) -> CheckpointResult:
        """Run one typed-question checkpoint, honoring cadence + degradation.

        ``force`` bypasses the rate limit for single-shot events that cannot
        queue-hop (phase transitions, pre-movement planning) and for chained
        tool-selection checkpoints inside one operator turn — those are one
        operator_chat cadence event, still human-paced, never per-tick.
        """
        if trigger not in CHECKPOINT_TRIGGERS:
            raise ValueError(
                f"Unknown checkpoint trigger {trigger!r}; expected one of {CHECKPOINT_TRIGGERS}"
            )

        alerts: list[dict] = []

        if self._degraded:
            return self._fallback_result(
                movement_id, trigger, questions,
                reason=f"decider degraded: {self._degraded_reason}",
                alerts=alerts,
            )

        if not force and not self.checkpoint_due(movement_id):
            return self._fallback_result(
                movement_id, trigger, questions,
                reason=(
                    f"rate-limited: last checkpoint <{self._min_interval}s ago "
                    f"for movement {movement_id}"
                ),
                alerts=alerts,
                tier="rate_limited",
            )

        # Tier-0 gate — routine situations with a recent confident cached
        # answer skip the 27B /decide entirely. Placement is load-bearing:
        # below the degraded and rate-limited short-circuits (those results
        # stay byte-identical and never touch memory), above the model call.
        # The consult never raises and its memory await rides the Wave-1
        # wait_for-bounded _novelty_check.
        tier0_started = time.monotonic()
        try:
            tier0_row, tier0_novelty, tier0_entry = await self._tier0_consult(
                movement_id, trigger, state_doc, questions
            )
        except Exception as exc:  # the gate must never break a checkpoint
            logger.warning("decider_brain.tier0_gate_error", error=str(exc)[:200])
            TIER0_CACHE_EVENTS.labels(outcome="miss_no_similarity_evidence").inc()
            tier0_row, tier0_novelty, tier0_entry = self._tier0_row(
                "miss_no_similarity_evidence", error=str(exc)[:200]
            ), None, None

        if tier0_entry is not None:
            # HIT — serve the cached routine answer. A hit is not an
            # escalation: no operator alert, no Gemma draft. Cadence is
            # consumed exactly like a real decide so tier0 cannot bypass the
            # per-movement rate limit.
            self._mark_checkpoint(movement_id)
            answers = {
                name: DeciderAnswer(
                    name=name,
                    type=stored["type"],
                    value=stored["value"],
                    probability=stored["probability"],
                    confidence=stored["confidence"],
                )
                for name, stored in tier0_entry["answers"].items()
            }
            TIER0_LATENCY_SAVED_MS.inc(tier0_entry["origin_latency_ms"])
            audit = {
                "phase": "decider_checkpoint",
                "trigger": trigger,
                "movement_id": movement_id,
                "tier": "tier0_cache_hit",
                "questions": {
                    name: {"type": spec["type"], "options": spec.get("options")}
                    for name, spec in questions.items()
                },
                "answers": {
                    name: {
                        "value": stored["value"],
                        "probability": stored["probability"],
                        "confidence": stored["confidence"],
                    }
                    for name, stored in tier0_entry["answers"].items()
                },
                "needs_gemma_draft": False,
                "novelty_latch": tier0_novelty,
                "tier0_cache": tier0_row,
                "latency_ms": int((time.monotonic() - tier0_started) * 1000),
                "timestamp": time.time(),
            }
            return CheckpointResult(
                tier="tier0_cache_hit",
                answers=answers,
                operator_alerts=alerts,
                audit=audit,
            )

        started = time.monotonic()
        try:
            answers = await self._client.decide(state=state_doc, questions=questions)
        except DeciderUnavailable as exc:
            self._set_degradation(str(exc))
            return self._fallback_result(
                movement_id, trigger, questions, reason=str(exc), alerts=alerts
            )

        self._mark_checkpoint(movement_id)
        duration_ms = int((time.monotonic() - started) * 1000)

        # Confidence gating — low-confidence answers get a Gemma draft + alert.
        # Answers whose type carries no confidence (noul: None) are EXCLUDED
        # from the aggregation entirely — "not applicable" is neither a pass
        # nor a fail (fixed 2026-10-19; previously None coerced to 0.0 made
        # every noul-bearing question set permanently low_confidence).
        low_confidence = {
            name: ans for name, ans in answers.items()
            if ans.confidence is not None and ans.confidence < self._confidence_floor
        }
        needs_gemma_draft = bool(low_confidence)
        if needs_gemma_draft:
            alerts.append({
                "type": "low_confidence_decision",
                "movement_id": movement_id,
                "trigger": trigger,
                "floor": self._confidence_floor,
                "questions": {n: a.confidence for n, a in low_confidence.items()},
                "action": "gemma_draft_required",
                "timestamp": time.time(),
            })

        # Novelty latch — the complement of the gate above: a CONFIDENT
        # answer to a situation with no similar memory is withheld instead
        # of trusted off-distribution. Never double-escalates: low-confidence
        # answers are already covered by the gate and only get an audit row.
        max_confidence = max(
            (ans.confidence for ans in answers.values()
             if ans.confidence is not None),
            default=0.0,
        )
        if self._memory_client is None:
            novelty = self._novelty_row("disabled_not_configured")
        elif max_confidence < self._confidence_floor:
            novelty = self._novelty_row("skipped_low_confidence")
        else:
            novelty = await self._novelty_check(movement_id, state_doc)

        tier = "decider"
        withheld_reason: str | None = None
        if novelty["escalate"]:
            tier = "novelty_latched"
            needs_gemma_draft = True
            if novelty["hits"] == 0:
                withheld_reason = (
                    "novel situation: confident answer withheld "
                    "(no similar memory on record)"
                )
            else:
                withheld_reason = (
                    f"novel situation: confident answer withheld "
                    f"(best maxsim {novelty['best_score']:.4f} "
                    f"< floor {self._novelty_floor})"
                )
            alerts.append({
                "type": "novel_situation_confident_answer",
                "movement_id": movement_id,
                "trigger": trigger,
                "best_score": novelty["best_score"],
                "similarity_floor": self._novelty_floor,
                "max_confidence": round(max_confidence, 4),
                "hits": novelty["hits"],
                "action": "escalated_to_tactical_and_operator",
                "timestamp": time.time(),
            })
            logger.warning(
                "decider_brain.novelty_latched",
                movement_id=movement_id,
                trigger=trigger,
                best_score=novelty["best_score"],
                floor=self._novelty_floor,
                hits=novelty["hits"],
            )
        elif novelty["status"] == "disabled_unreachable":
            # Memory outage disables the latch HONESTLY per checkpoint but
            # must NOT bench the strategic tier (no _set_degradation): the
            # confidence floor still gates every answer.
            alerts.append({
                "type": "novelty_check_unavailable",
                "movement_id": movement_id,
                "trigger": trigger,
                "error": novelty["error"],
                "action": "novelty latch disabled (honest)",
                "timestamp": time.time(),
            })
            logger.info(
                "decider_brain.novelty_latch_disabled",
                movement_id=movement_id,
                error=novelty["error"],
            )

        # Tier-0 write side — mirrors the read side exactly: only a
        # confident (no low-confidence answers), memory-covered, non-latched
        # answer for an eligible trigger enters the routine cache. A latched
        # or novel answer never stores ("novelty forces a miss" holds
        # write-side too, else the cache launders off-distribution answers
        # the Wave-1 latch withheld); unavailable memory never stores.
        if (
            self._tier0_enabled
            and trigger in self._tier0_triggers
            and not low_confidence
            and novelty["status"] == "covered"
            and novelty["best_score"] is not None
            and novelty["best_score"] >= self._tier0_floor
        ):
            key = (
                movement_id,
                trigger,
                _questions_signature(questions),
                novelty["best_doc_id"],
            )
            entry = {
                "answers": {
                    name: {
                        "type": ans.type,
                        "value": ans.value,
                        "probability": round(ans.probability, 4),
                        "confidence": _round_conf(ans.confidence),
                    }
                    for name, ans in answers.items()
                },
                "stored_at": self._clock(),
                "origin_latency_ms": duration_ms,
                "origin_timestamp": time.time(),
                "state_doc_sha256": hashlib.sha256(
                    state_doc.encode("utf-8")
                ).hexdigest(),
                "best_doc_id": novelty["best_doc_id"],
            }
            self._tier0_put(key, entry)
            # An own-doc trigger also files it for the movement's own
            # decision docs (read only when one of them is the best match —
            # see _tier0_consult); one answer, so "stored" counts it once.
            own_key = self._tier0_own_key(movement_id, trigger, key[2])
            if own_key is not None:
                self._tier0_put(own_key, entry)
            TIER0_CACHE_EVENTS.labels(outcome="stored").inc()
            logger.info(
                "decider_brain.tier0_stored",
                movement_id=movement_id,
                trigger=trigger,
                best_doc_id=novelty["best_doc_id"],
                origin_latency_ms=duration_ms,
            )

        # Decision-memory feeder — upsert the situation into the corpus
        # (kind=decision) so the gate/latch read side has something to
        # recall on repeat traffic. tier "decider" means the 27B's answer
        # was actually served (low-confidence ones too — they ship with a
        # Gemma draft). A withheld (novelty_latched) checkpoint records its
        # SITUATION as well (metadata.withheld=true) — otherwise a novel
        # situation can never become "on record" and latches forever.
        # Fallback results (no decide happened) never record. Runs AFTER
        # this checkpoint's own /search — the first occurrence of a
        # situation must still be able to read as novel. Fail-open: never
        # raises, never benches.
        if tier == "decider":
            await self._record_decision_memory(
                movement_id, trigger, state_doc, questions, answers,
                needs_gemma_draft=needs_gemma_draft,
            )
        elif tier == "novelty_latched":
            recorded = await self._record_decision_memory(
                movement_id, trigger, state_doc, questions, answers,
                needs_gemma_draft=needs_gemma_draft,
                withheld=True,
            )
            if recorded:
                # The memo still holds the pre-record "novel" search for this
                # exact (movement, state); drop it so a repeat re-searches.
                self._novelty_memo.pop(
                    (movement_id, hashlib.sha256(state_doc.encode("utf-8")).hexdigest()),
                    None,
                )

        audit = {
            "phase": "decider_checkpoint",
            "trigger": trigger,
            "movement_id": movement_id,
            "tier": tier,
            "questions": {
                name: {"type": spec["type"], "options": spec.get("options")}
                for name, spec in questions.items()
            },
            "answers": {
                name: {
                    "value": ans.value,
                    "probability": round(ans.probability, 4),
                    "confidence": _round_conf(ans.confidence),
                }
                for name, ans in answers.items()
            },
            "needs_gemma_draft": needs_gemma_draft,
            "novelty_latch": novelty,
            "tier0_cache": tier0_row,
            "latency_ms": duration_ms,
            "timestamp": time.time(),
        }
        return CheckpointResult(
            tier=tier,
            answers=answers,
            degraded_reason=withheld_reason,
            operator_alerts=alerts,
            audit=audit,
        )

    def _fallback_result(
        self,
        movement_id: str,
        trigger: str,
        questions: dict[str, dict],
        *,
        reason: str,
        alerts: list[dict],
        tier: str = "gemma",
    ) -> CheckpointResult:
        alerts.append({
            "type": "decider_unavailable",
            "movement_id": movement_id,
            "trigger": trigger,
            "reason": reason,
            "action": f"tactical fallback to {tier}",
            "timestamp": time.time(),
        })
        audit = {
            "phase": "decider_checkpoint",
            "trigger": trigger,
            "movement_id": movement_id,
            "tier": tier,
            "reason": reason,
            "timestamp": time.time(),
        }
        return CheckpointResult(tier=tier, degraded_reason=reason, operator_alerts=alerts, audit=audit)

    # -- novelty latch -------------------------------------------------------------

    def _novelty_row(
        self,
        status: str,
        *,
        escalate: bool = False,
        result: MemorySearchResult | None = None,
        memo: bool = False,
        error: str | None = None,
    ) -> dict:
        """Uniform JSON-safe audit row for every latch status.

        Keys are identical across statuses (None where not applicable) so the
        decisions_log schema stays stable across
        novel/covered/insufficient_evidence/disabled_unreachable/
        disabled_not_configured/skipped_low_confidence.
        """
        return {
            "status": status,
            "escalate": escalate,
            "best_score": (
                round(result.best_score, 4)
                if result is not None and result.best_score is not None
                else None
            ),
            "floor": self._novelty_floor,
            "hits": result.hits if result is not None else 0,
            "strategy": result.best_strategy if result is not None else None,
            "best_doc_id": result.best_doc_id if result is not None else None,
            "best_kind": result.best_kind if result is not None else None,
            # Which situation matched (a decision doc's ref names its
            # movement/trigger/question set); read by the tier-0 own-doc filing.
            "best_ref_id": getattr(result, "best_ref_id", None) if result is not None else None,
            "memo": memo,
            "error": error,
        }

    async def _novelty_check(self, movement_id: str, state_doc: str) -> dict:
        """One memory-similarity check for a confident checkpoint.

        Memoized per (movement_id, state_doc) for ``memo_ttl_sec`` — only
        successful (status 'ok') searches are memoized; failures always
        re-search next time. Bounded by ``wait_for``; any exception (timeout
        included) is the disabled-honestly degradation, never a raise.
        """
        key = (movement_id, hashlib.sha256(state_doc.encode("utf-8")).hexdigest())
        now = self._clock()
        memoized = False
        memo_hit = self._novelty_memo.get(key)
        if memo_hit is not None and (now - memo_hit[0]) < self._memo_ttl_sec:
            result = memo_hit[1]
            memoized = True
        else:
            try:
                result = await asyncio.wait_for(
                    self._memory_client.search(state_doc, movement_id=movement_id),
                    timeout=self._novelty_timeout,
                )
            except Exception as exc:
                return self._novelty_row(
                    "disabled_unreachable", error=str(exc)[:200]
                )
            if result.status != "ok":
                # 'unavailable' results are NEVER memoized — the next
                # confident checkpoint retries the memory service.
                return self._novelty_row(
                    "disabled_unreachable", error=result.error
                )
            self._novelty_memo[key] = (now, result)

        if result.hits == 0:
            # Empty memory resembles nothing — that IS novel.
            return self._novelty_row("novel", escalate=True, result=result, memo=memoized)
        if not result.maxsim_present:
            # Every hit fell back to dense scores; maxsim and dense_fallback
            # scales are NOT comparable — no similarity evidence, no
            # novelty claim (limitation recorded, escalation suppressed).
            return self._novelty_row(
                "insufficient_evidence", result=result, memo=memoized
            )
        if result.best_score is not None and result.best_score < self._novelty_floor:
            return self._novelty_row("novel", escalate=True, result=result, memo=memoized)
        return self._novelty_row("covered", result=result, memo=memoized)

    # -- decision-memory feeder -----------------------------------------------------

    async def _record_decision_memory(
        self,
        movement_id: str,
        trigger: str,
        state_doc: str,
        questions: dict[str, dict],
        answers: dict[str, DeciderAnswer],
        *,
        needs_gemma_draft: bool,
        withheld: bool = False,
    ) -> bool:
        """Upsert one checkpoint's situation into the memory corpus.

        The ref_id mirrors the tier-0 cache-key contract (movement, trigger,
        canonical questions signature — deliberately NOT the raw state doc,
        which embeds per-tick values): the embedding service upserts on
        ``(kind, ref_id)``, so a re-decided situation refreshes one stable
        doc (and its doc_id) instead of duplicating rows, keeping the
        best_doc_id component of tier-0 keys stable across repeats.
        ``withheld`` marks a novelty-latched checkpoint: its situation is
        recorded (metadata.withheld=true, tier novelty_latched) so repeats
        stop latching, while its answers stay out of every serving path.
        Never raises; every outcome lands in the MEMORY_RECORD_EVENTS metric
        so a dead feeder is visible without reading logs. Returns True when
        the record landed.
        """
        if not self._memory_record_enabled:
            # VVIP_MEMORY_RECORD=0: the corpus is read, never written.
            MEMORY_RECORD_EVENTS.labels(outcome="skipped_disabled").inc()
            return False
        if self._memory_client is None:
            MEMORY_RECORD_EVENTS.labels(outcome="skipped_no_client").inc()
            return False
        record = getattr(self._memory_client, "record_decision", None)
        if record is None:  # duck-typed fakes without a write side keep working
            MEMORY_RECORD_EVENTS.labels(outcome="skipped_unconfigured").inc()
            return False
        ref_id = (
            _decision_ref_prefix(movement_id, trigger)
            + _questions_signature(questions)[:DECISION_REF_SIGNATURE_HEX]
        )
        metadata = {
            "trigger": trigger,
            "tier": "novelty_latched" if withheld else "decider",
            "withheld": withheld,
            "needs_gemma_draft": needs_gemma_draft,
            "min_confidence": _round_conf(min(
                (a.confidence for a in answers.values()
                 if a.confidence is not None),
                default=None,
            )),
            "answers": {
                name: {
                    "type": a.type,
                    "value": a.value,
                    "probability": round(a.probability, 4),
                    "confidence": _round_conf(a.confidence),
                }
                for name, a in answers.items()
            },
            "state_doc_sha256": hashlib.sha256(
                state_doc.encode("utf-8")
            ).hexdigest(),
        }
        try:
            recorded = await asyncio.wait_for(
                record(
                    state_doc,
                    movement_id=movement_id,
                    ref_id=ref_id,
                    title=(
                        f"decider {trigger} checkpoint"
                        + (" (withheld: novel situation)" if withheld else "")
                    ),
                    metadata=metadata,
                ),
                timeout=DECISION_MEMORY_RECORD_TIMEOUT_SEC,
            )
        except Exception as exc:  # the feeder must never break a checkpoint
            MEMORY_RECORD_EVENTS.labels(outcome="error").inc()
            logger.warning(
                "decider_brain.memory_record_error", error=str(exc)[:200]
            )
            return False
        if recorded:
            MEMORY_RECORD_EVENTS.labels(outcome="recorded").inc()
            return True
        MEMORY_RECORD_EVENTS.labels(outcome="error").inc()
        return False

    # -- tier-0 gate ----------------------------------------------------------------

    def _tier0_row(
        self,
        status: str,
        *,
        novelty: dict | None = None,
        age_sec: float | None = None,
        latency_saved_ms: float | None = None,
        origin_state_doc_sha256: str | None = None,
        origin_timestamp: float | None = None,
        error: str | None = None,
    ) -> dict:
        """Uniform JSON-safe audit row for every tier-0 gate status.

        Keys are identical across statuses (None where not applicable) so the
        decisions_log schema stays stable across
        hit/miss_no_entry/miss_ttl_expired/miss_novel/
        miss_below_routine_floor/miss_no_similarity_evidence/
        miss_ineligible_trigger/disabled_kill_switch — same doctrine as
        :meth:`_novelty_row`. Similarity fields are copied from the reused
        novelty row (already rounded, memo flag included); on a hit the
        origin_* provenance identifies which stored decide produced the
        served answers (an auditor can diff the serving state_doc against
        origin_state_doc_sha256 to see how much the situation moved).
        """
        return {
            "status": status,
            "best_score": novelty["best_score"] if novelty is not None else None,
            "floor": self._tier0_floor,
            "hits": novelty["hits"] if novelty is not None else 0,
            "best_doc_id": novelty["best_doc_id"] if novelty is not None else None,
            "best_kind": novelty["best_kind"] if novelty is not None else None,
            "age_sec": round(age_sec, 4) if age_sec is not None else None,
            "latency_saved_ms": latency_saved_ms,
            "origin_state_doc_sha256": origin_state_doc_sha256,
            "origin_timestamp": origin_timestamp,
            "memo": bool(novelty["memo"]) if novelty is not None else False,
            "error": error if error is not None else (
                novelty.get("error") if novelty is not None else None
            ),
        }

    def _tier0_put(self, key: tuple[str, str, str, str], entry: dict) -> None:
        """Insert one cache entry, then enforce the FIFO bound."""
        self._tier0_cache[key] = entry
        while len(self._tier0_cache) > self._tier0_max_entries:
            oldest_key = next(iter(self._tier0_cache))  # FIFO: dicts keep insert order
            del self._tier0_cache[oldest_key]

    def _tier0_own_key(
        self, movement_id: str, trigger: str, signature: str,
    ) -> tuple[str, str, str, str] | None:
        """The movement-level filing of a TIER0_OWN_DOC_TRIGGERS trigger.

        Its last slot is the movement's decision-doc ref prefix instead of
        one doc id: the answer stored for this question set is served when
        the best memory is ANY decision doc this movement's checkpoints of
        this trigger recorded. Without it the first answer per question set
        sat under the best doc seen before the feeder upserted the
        situation's own doc, which no repeat matches again; and which own
        doc a repeat matches is a near tie when one candidate set's state
        doc is nearly a subset of another's (MaxSim scores a query the same
        against any doc holding all its tokens). Still one movement, one
        trigger, one exact question set, and the routine floor on the match.
        None for every other trigger (their filing is unchanged).
        """
        if trigger not in self._tier0_own_doc_triggers:
            return None
        return (movement_id, trigger, signature, _decision_ref_prefix(movement_id, trigger))

    async def _tier0_consult(
        self,
        movement_id: str,
        trigger: str,
        state_doc: str,
        questions: dict[str, dict],
    ) -> tuple[dict, dict | None, dict | None]:
        """Consult the tier-0 routine cache; NEVER raises, never benches the tier.

        Returns ``(row, novelty, entry)``: ``row`` is the uniform audit row
        (every status); ``novelty`` is the underlying similarity row when the
        consult ran one (None otherwise — carried into the hit audit so
        decider_checkpoint rows keep ONE shape in decisions_log); ``entry``
        is the cached answer dict on a HIT, None on every miss. The
        similarity leg reuses :meth:`_novelty_check` — already memoized per
        (movement_id, state_doc) and wait_for-bounded — so the gate costs one
        embedding /search per distinct state document, regardless of hit or
        miss. The cache key is MOVEMENT-SCOPED: the embedding service ranks
        by movement but does not filter on it, so without the movement
        component a different movement's first-ever checkpoint could hit and
        be served (and audited) another movement's decision.
        """
        if not self._tier0_enabled:
            TIER0_CACHE_EVENTS.labels(outcome="disabled_kill_switch").inc()
            return self._tier0_row("disabled_kill_switch"), None, None
        if trigger not in self._tier0_triggers:
            TIER0_CACHE_EVENTS.labels(outcome="miss_ineligible_trigger").inc()
            return self._tier0_row("miss_ineligible_trigger"), None, None
        if self._memory_client is None:
            # No similarity machinery wired — fail OPEN to the 27B with an
            # honest audit row (mirrors the latch's disabled_not_configured).
            TIER0_CACHE_EVENTS.labels(outcome="miss_no_similarity_evidence").inc()
            return self._tier0_row("miss_no_similarity_evidence"), None, None

        novelty = await self._novelty_check(movement_id, state_doc)
        if novelty["escalate"]:
            # Novel (best maxsim < the latch floor or zero hits): novelty
            # forces a miss.
            TIER0_CACHE_EVENTS.labels(outcome="miss_novel").inc()
            return self._tier0_row("miss_novel", novelty=novelty), novelty, None
        if novelty["status"] != "covered":
            # disabled_unreachable / insufficient_evidence — fail OPEN to the
            # 27B: honest audit, no operator alert, never _set_degradation
            # (the latch's memory-outage discipline).
            TIER0_CACHE_EVENTS.labels(outcome="miss_no_similarity_evidence").inc()
            return (
                self._tier0_row("miss_no_similarity_evidence", novelty=novelty),
                novelty,
                None,
            )
        if novelty["best_score"] is not None and novelty["best_score"] < self._tier0_floor:
            # Covered by the latch's (looser) floor but below the ROUTINE bar
            # — a distinct outcome from "novel": the situation is known, just
            # not similar enough to serve a cached answer for.
            TIER0_CACHE_EVENTS.labels(outcome="miss_below_routine_floor").inc()
            return (
                self._tier0_row("miss_below_routine_floor", novelty=novelty),
                novelty,
                None,
            )

        signature = _questions_signature(questions)
        key = (movement_id, trigger, signature, novelty["best_doc_id"])
        own_key = self._tier0_own_key(movement_id, trigger, signature)
        if (
            own_key is not None
            and own_key in self._tier0_cache
            and novelty["best_kind"] == "decision"
            and _is_own_decision_ref(novelty.get("best_ref_id"), movement_id, trigger)
        ):
            # The best memory is one of this movement's own decided
            # situations: the latest answer stored for this exact question
            # set serves it, whichever of those situations matched.
            key = own_key
        entry = self._tier0_cache.get(key)
        if entry is None:
            TIER0_CACHE_EVENTS.labels(outcome="miss_no_entry").inc()
            return self._tier0_row("miss_no_entry", novelty=novelty), novelty, None
        now = self._clock()
        age_sec = now - entry["stored_at"]
        if age_sec >= self._tier0_ttl_sec:
            del self._tier0_cache[key]  # expired — evict, never serve stale
            TIER0_CACHE_EVENTS.labels(outcome="miss_ttl_expired").inc()
            return (
                self._tier0_row("miss_ttl_expired", novelty=novelty, age_sec=age_sec),
                novelty,
                None,
            )

        TIER0_CACHE_EVENTS.labels(outcome="hit").inc()
        row = self._tier0_row(
            "hit",
            novelty=novelty,
            age_sec=age_sec,
            latency_saved_ms=entry["origin_latency_ms"],
            origin_state_doc_sha256=entry.get("state_doc_sha256"),
            origin_timestamp=entry.get("origin_timestamp"),
        )
        logger.info(
            "decider_brain.tier0_cache_hit",
            movement_id=movement_id,
            trigger=trigger,
            best_doc_id=novelty["best_doc_id"],
            filed_under="own_decision_docs" if key == own_key else "best_doc",
            age_sec=row["age_sec"],
            latency_saved_ms=row["latency_saved_ms"],
        )
        return row, novelty, entry

    # -- question builders -----------------------------------------------------------

    @staticmethod
    def tool_selection_question(
        tool_names: list[str],
        *,
        name: str = "next_tool",
        allow_final: bool = True,
    ) -> dict[str, dict]:
        """Build the typed question set for strategic tool selection.

        One choice question over the tool menu (<=249 options is far below the
        readout head's budget) plus a score question for decision confidence.
        """
        options = list(tool_names)
        if allow_final and "final_answer" not in options:
            options.append("final_answer")
        return {
            name: {
                "type": "choice",
                "instructions": (
                    "You are orchestrating a VVIP convoy command center. Given the "
                    "state document, choose the single next tool to invoke from the "
                    "menu, or 'final_answer' when enough data has been gathered to "
                    "answer the operator without another tool call."
                ),
                "options": options,
            },
            "decision_confidence": {
                "type": "score",
                "instructions": (
                    "How confident are you that the choice above is the right next "
                    "step? 0.0 = guessing, 1.0 = certain."
                ),
                "scale": [0.0, 1.0],
            },
        }

    @staticmethod
    def pre_movement_questions(route_options: list[str]) -> dict[str, dict]:
        """Typed question set for the pre-movement strategic checkpoint."""
        options = route_options[:MAX_ROUTE_OPTIONS] if route_options else ["primary", "alternate"]
        return {
            "route_strategy": {
                "type": "choice",
                "instructions": (
                    "Pre-movement planning for a VVIP convoy. Weigh security "
                    "protocol compliance for the VVIP class against public "
                    "disruption. Which candidate strategy should lead?"
                ),
                "options": options,
            },
            "urgency": {
                "type": "noul",
                "instructions": (
                    "How urgent is it to lock this plan now rather than waiting "
                    "for better traffic conditions? (noul = none-of-the-above "
                    "likelihood, i.e. probability the situation defies the "
                    "listed strategy options)"
                ),
            },
            "disruption_tolerance": {
                "type": "score",
                "instructions": (
                    "Acceptable level of public-traffic disruption for this "
                    "movement, 0.0 (minimize at all costs) to 1.0 (security "
                    "dominates completely)."
                ),
                "scale": [0.0, 1.0],
            },
        }


MAX_ROUTE_OPTIONS = 249
