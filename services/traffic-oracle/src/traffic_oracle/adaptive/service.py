"""AdaptiveService — orchestrates the T2 adaptive layer per movement.

Pure-core orchestration: every I/O surface is an injected *port*
(clock, Valkey sink, DB persister, twin port).  The service is
deterministic given the port behavior and the sequence of observations,
which is what lets the unit tests run the full pipeline with fakes.

Responsibilities:
    * per-subject situation state (classifier + hysteresis + dwell), for
      exactly the subjects the situation-engine loop reports active —
      ``retain_only`` evicts everything else every tick, so a completed
      event can never hold the city band
    * C4 param decisions (protocol floors, twin shadow-eval, fail-closed),
      with the shadow verdict and its escalation flag tracked PER SUBJECT
    * escalation evaluation with sustained-RED tracking
    * degradation integration (RULES_ONLY forces baseline)
    * Valkey mirroring (``adaptive:params:{movement_id}`` TTL 30 s,
      ``situation:changed`` on class transitions — the decider cadence
      trigger #3)
    * the city band (most severe tracked subject) and the wall-clock time
      it last changed (``city_band_since``)

Read/write split (the audit-noise fix):
    * ``current_params`` is READ-ONLY — what ``GET /adaptive/params``
      serves: the subject's last published decision, or the tier's
      floor-clamped baseline for an untracked subject.  It never creates
      state, never writes ``adaptive.param_overrides`` and never touches
      the mirror.
    * ``params_for`` is the decision path (situation-engine loop and
      ``POST /adaptive/observe``): it decides, persists one audit row ONLY
      when the decision changed (class, params, source, shadow verdict,
      escalation, degradation — labelled in ``evidence.publish_reason``),
      and keeps the 30 s mirror alive with the same payload in between.
      A calibration refusal counts with its full reason (a corrected
      reason is one ``verdict_change`` row); other shadow details count by
      token only.  A row that fails to land is re-attempted on the next
      decision, and one subject's decisions never interleave.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NamedTuple, Protocol

import structlog

from traffic_oracle.adaptive.degradation import (
    DegradationManager,
    ModelHealthSnapshot,
)
from traffic_oracle.adaptive.enums import (
    DegradationTier,
    ParamSource,
    SecurityTier,
    ShadowEvalStatus,
    SituationClass,
)
from traffic_oracle.adaptive.escalation import (
    EscalationDecision,
    EscalationEngine,
    EscalationInputs,
)
from traffic_oracle.adaptive.hysteresis import (
    HysteresisConfig,
    HysteresisController,
)
from traffic_oracle.adaptive.inputs import SituationInputs
from traffic_oracle.adaptive.policy import (
    BASELINE_ADVANCE_CLOSURE_SEC,
    ParameterAdapter,
    PolicyDecision,
    clamp_advance_closure,
)
from traffic_oracle.adaptive.shadow import ShadowEvaluator
from traffic_oracle.adaptive.situation import Situation, SituationEngine
from traffic_oracle.adaptive.twin import (
    ClosurePolicy,
    ShadowContext,
    TwinPort,
    UnavailableTwinPort,
)

logger = structlog.get_logger(__name__)

PARAMS_MIRROR_TTL_SEC = 30          # C4: TTL 30 s
#: Unchanged decisions re-SET the mirror (same payload) this often, so the
#: 30 s key stays alive while the loop runs and lapses when it dies.
PARAMS_MIRROR_REFRESH_SEC = 10.0
SITUATION_CHANGED_TTL_SEC = 60      # decider cadence trigger window
#: A cached twin shadow verdict is re-evaluated after this long (traffic
#: demand moves; the verdict for the same candidate can change).
SHADOW_REEVAL_SEC = 300.0

#: ``() -> (calibrated, reason)`` — the twin's calibration state.  When it
#: reports uncalibrated the paired twin run is not even attempted: an
#: uncalibrated twin can never pass the gate (fail-closed by contract), so
#: the verdict is UNAVAILABLE with the calibration reason verbatim.
TwinReadiness = Callable[[], tuple[bool, str | None]]


def _audit_safe(value: Any) -> bool:
    """True when a value can persist into the audit evidence JSONB as-is.

    Scalars always; dicts/lists recurse (bounded by the small, fixed
    evidence payloads built here) so structured decisions — e.g. the
    escalation rule table's ``{"escalate", "level", "reasons", "handoff"}``
    — survive into ``adaptive.param_overrides.evidence`` instead of being
    silently dropped by the old scalars-only filter.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return True
    if isinstance(value, dict):
        return all(_audit_safe(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_audit_safe(v) for v in value)
    return False


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _detail_token(detail: str) -> str:
    """Machine token of a shadow detail (``"no_regression:…"`` →
    ``"no_regression"``) — per-run numbers in the tail never mint audit rows."""
    return detail.split(":", 1)[0].strip()


#: Shadow details whose tail is operator-facing STATE rather than a per-run
#: measurement.  ``twin_not_calibrated: <reason>`` quotes the twin's
#: calibration reason, which changes only when the calibration does
#: (startup prime, hourly coverage check, replay) — so its full text is in
#: the publish signature and a corrected reason writes exactly one
#: ``verdict_change`` row.  Reduced to its token, a restart's first publish
#: kept quoting the pre-check placeholder on the audit trail and the AMA
#: feed until the band changed.
_STATEFUL_DETAIL_TOKENS = frozenset({"twin_not_calibrated"})


def _detail_signature(detail: str) -> str:
    """The part of a shadow detail that makes a decision different."""
    token = _detail_token(detail)
    return detail.strip() if token in _STATEFUL_DETAIL_TOKENS else token


class _Signature(NamedTuple):
    """What makes one C4 decision different from the last (one audit row)."""

    band: str
    params: str
    source: str
    shadow_eval: str
    shadow_detail: str
    fail_closed_reason: str | None
    escalation_level: str
    escalation_reasons: tuple[str, ...]
    degradation_tier: str


def _publish_reason(previous: _Signature | None, current: _Signature) -> str:
    """Why a decision is published (``evidence.publish_reason``)."""
    if previous is None:
        return "first_publish"
    if current.band != previous.band:
        return "class_change"
    if (current.params, current.source) != (previous.params, previous.source):
        return "param_change"
    if (current.shadow_eval, current.shadow_detail, current.fail_closed_reason) != (
        previous.shadow_eval,
        previous.shadow_detail,
        previous.fail_closed_reason,
    ):
        return "verdict_change"
    if (current.escalation_level, current.escalation_reasons) != (
        previous.escalation_level,
        previous.escalation_reasons,
    ):
        return "escalation_change"
    return "degradation_change"


# ---------------------------------------------------------------------------
# Ports (all I/O behind Protocols)
# ---------------------------------------------------------------------------


class SinkPort(Protocol):
    """Where mirrors and change notifications go (Valkey in production)."""

    async def publish_params(self, movement_id: str, payload_json: str) -> None:
        ...  # pragma: no cover

    async def publish_situation_change(
        self, movement_id: str, from_code: str, to_code: str, payload_json: str
    ) -> None:
        ...  # pragma: no cover


class PersisterPort(Protocol):
    """Audit persistence (situation_log / param_overrides).

    ``log_params`` returns False when the row did NOT land (database
    blip); the service then keeps the previous publish signature so the
    next decision re-attempts the row.  None (or True) means written — or
    persistence deliberately disabled, which is never retried.
    """

    async def log_situation(self, record: dict) -> None:
        ...  # pragma: no cover

    async def log_params(self, record: dict) -> bool | None:
        ...  # pragma: no cover


class NullSink:
    """No-op sink (tests / Valkey-less operation)."""

    async def publish_params(self, movement_id: str, payload_json: str) -> None:
        _ = movement_id, payload_json

    async def publish_situation_change(
        self, movement_id: str, from_code: str, to_code: str, payload_json: str
    ) -> None:
        _ = movement_id, from_code, to_code, payload_json


class NullPersister:
    """No-op persister (DB-less operation)."""

    async def log_situation(self, record: dict) -> None:
        _ = record

    async def log_params(self, record: dict) -> bool | None:
        _ = record
        return None


# ---------------------------------------------------------------------------
# Per-movement state
# ---------------------------------------------------------------------------


@dataclass
class MovementAdaptiveState:
    """Everything the service remembers about one movement."""

    movement_id: str
    vvip_class: SecurityTier
    hysteresis: HysteresisController = field(default_factory=HysteresisController)
    last_situation: Situation | None = None
    effective_class: SituationClass | None = None
    band_enter_monotonic: float = 0.0
    last_transition_monotonic: float = 0.0
    last_gps_age_sec: float = 0.0
    ambulance_delay_projection_sec: float = 0.0
    crowd_extreme: bool = False
    observation_count: int = 0
    # wall-clock band age + the provenance of the last observation
    band_since: datetime | None = None
    last_observed_at: datetime | None = None
    last_inputs: dict | None = None
    # twin shadow verdict — PER SUBJECT (one movement's evaluation never
    # sets another's SHADOW_EVAL_FAILED escalation input)
    shadow_status: ShadowEvalStatus = ShadowEvalStatus.UNAVAILABLE
    shadow_detail: str = "no_shadow_eval_yet"
    shadow_failed: bool = False
    shadow_key: tuple | None = None
    shadow_checked_monotonic: float | None = None
    # last published decision (what the read-only GET serves)
    published: dict | None = None
    published_signature: _Signature | None = None
    published_at: datetime | None = None
    publish_count: int = 0
    mirror_refreshed_monotonic: float | None = None
    # audit rows that did not land (each is re-attempted on the next decision)
    audit_failures: int = 0
    # one decision at a time per subject (loop tick vs POST /adaptive/observe)
    decision_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
    )


@dataclass(frozen=True)
class ObservationResult:
    """Returned by ``observe`` for callers that want the full picture."""

    situation: Situation
    effective_class: SituationClass
    transitioned: bool
    previous_class: SituationClass | None


class AdaptiveService:
    """Per-movement adaptive orchestration (see module docstring)."""

    def __init__(
        self,
        clock: Callable[[], float],
        sink: SinkPort | None = None,
        persister: PersisterPort | None = None,
        twin_port: TwinPort | None = None,
        hysteresis_config: HysteresisConfig | None = None,
        shadow_evaluator: ShadowEvaluator | None = None,
        escalation_engine: EscalationEngine | None = None,
        degradation: DegradationManager | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        twin_readiness: TwinReadiness | None = None,
    ) -> None:
        self._clock = clock
        self._wall = wall_clock or (lambda: datetime.now(UTC))
        self._sink = sink or NullSink()
        self._persister = persister or NullPersister()
        self._twin_port = twin_port or UnavailableTwinPort()
        self._twin_readiness = twin_readiness
        self._engine = SituationEngine()
        self._adapter = ParameterAdapter()
        self._shadow = shadow_evaluator or ShadowEvaluator()
        self._escalation = escalation_engine or EscalationEngine()
        self._degradation = degradation or DegradationManager(clock=clock)
        self._hysteresis_config = hysteresis_config or HysteresisConfig()
        self._movements: dict[str, MovementAdaptiveState] = {}
        # movement refs (brain slugs / legacy ids) → the event subject that
        # carries them, so a reader asking by movement id finds its event
        self._aliases: dict[str, str] = {}
        # most recent shadow evaluation across subjects (status display only)
        self._last_shadow: dict = {
            "status": ShadowEvalStatus.UNAVAILABLE.code,
            "detail": "no_shadow_eval_yet",
            "movement_id": None,
            "at": None,
        }
        self._evicted_total = 0
        # City band = most severe tracked subject; ``since`` is the wall
        # time it last changed (service start while the city is at rest).
        self._city_band: SituationClass = SituationClass.GREEN
        self._city_band_since: datetime = self._wall()
        self._city_band_driver: str | None = None
        # Situation-engine poll health: while the loop cannot load its
        # subjects, the city band (GREEN once their stale bands are evicted)
        # is UNKNOWN and says so — an outage never reads as "at rest".
        self._poll_failing_since: datetime | None = None
        self._poll_failures = 0

    # -- state access ---------------------------------------------------------

    def movement_state(self, movement_id: str) -> MovementAdaptiveState | None:
        return self._movements.get(movement_id)

    def tracked_ids(self) -> list[str]:
        return sorted(self._movements)

    def now_wall(self) -> datetime:
        """The service's wall clock (injected in tests)."""
        return self._wall()

    def set_aliases(self, aliases: dict[str, str]) -> None:
        """Replace the movement-ref → subject map (rebuilt by the loop)."""
        self._aliases = {str(k): str(v) for k, v in aliases.items() if k and v}

    def resolve(self, movement_id: str | None) -> str | None:
        """The tracked subject a reader's id denotes (itself or an alias)."""
        if not movement_id:
            return None
        if movement_id in self._movements:
            return movement_id
        target = self._aliases.get(movement_id)
        return target if target in self._movements else None

    def _ensure_state(
        self, movement_id: str, vvip_class: SecurityTier
    ) -> MovementAdaptiveState:
        state = self._movements.get(movement_id)
        if state is None:
            state = MovementAdaptiveState(
                movement_id=movement_id,
                vvip_class=vvip_class,
                hysteresis=HysteresisController(
                    config=self._hysteresis_config,
                ),
            )
            self._movements[movement_id] = state
        # A movement's tier can change between planning cycles; trust the
        # latest authoritative value.
        state.vvip_class = vvip_class
        return state

    # -- subject hygiene --------------------------------------------------------

    def retain_only(
        self, active_ids: Iterable[str], reason: str = "not_active"
    ) -> list[str]:
        """Evict every tracked subject that is not in ``active_ids``.

        Called by the situation-engine loop each tick with the subjects it
        loaded as active (planning/approved/active), so completed or
        cancelled events and one-off ids stop counting toward the city
        band immediately.  Returns the evicted ids.
        """
        keep = set(active_ids)
        evicted = [mid for mid in self._movements if mid not in keep]
        for mid in evicted:
            self.evict(mid, reason=reason)
        return evicted

    def evict(self, movement_id: str, reason: str = "evicted") -> bool:
        """Forget one subject's state (band, hysteresis, last decision)."""
        state = self._movements.pop(movement_id, None)
        if state is None:
            return False
        self._evicted_total += 1
        logger.info(
            "adaptive.subject_evicted",
            movement_id=movement_id,
            reason=reason,
            band=state.effective_class.code if state.effective_class else None,
        )
        self._refresh_city_band()
        return True

    # -- city band ---------------------------------------------------------------

    def _refresh_city_band(self) -> None:
        best: SituationClass = SituationClass.GREEN
        driver: str | None = None
        for mid, state in sorted(self._movements.items()):
            band = state.effective_class
            if band is not None and (driver is None or band.rank > best.rank):
                best, driver = band, mid
        if best is not self._city_band:
            logger.info(
                "adaptive.city_band_changed",
                city_band_from=self._city_band.code,
                city_band_to=best.code,
                driver=driver,
            )
            self._city_band = best
            self._city_band_since = self._wall()
        self._city_band_driver = driver

    def note_poll(self, ok: bool) -> None:
        """Record one situation-engine poll outcome (the loop calls it every
        tick); a failing poll marks the city band stale until one succeeds."""
        if ok:
            if self._poll_failing_since is not None:
                logger.info(
                    "adaptive.city_band_fresh_again",
                    failed_polls=self._poll_failures,
                )
            self._poll_failing_since = None
            self._poll_failures = 0
            return
        if self._poll_failing_since is None:
            self._poll_failing_since = self._wall()
        self._poll_failures += 1

    def city_band(self) -> dict:
        """The city band, the wall time it last changed and its subjects.

        ``simulated_feed`` is the driving subject's input provenance (True
        when its band stands on the simulated feed or emulated inputs), so
        a surface can label the city band honestly.  ``stale`` is True
        while the situation engine cannot poll its subjects: the band is
        then unknown (``stale_since`` = first failed poll), not at rest.
        """
        driver = self._movements.get(self._city_band_driver or "")
        stale = self._poll_failing_since is not None
        return {
            "band": self._city_band.code,
            "since": _iso(self._city_band_since),
            "subjects": len(self._movements),
            "driver": self._city_band_driver,
            "simulated_feed": (
                (driver.last_inputs or {}).get("simulated_feed")
                if driver is not None
                else None
            ),
            "stale": stale,
            "stale_reason": (
                f"situation engine poll failing ({self._poll_failures} in a row)"
                if stale
                else None
            ),
            "stale_since": _iso(self._poll_failing_since),
        }

    # -- health / degradation ---------------------------------------------------

    def feed_health(self, snapshot: ModelHealthSnapshot) -> DegradationTier:
        return self._degradation.observe_health(snapshot)

    # -- observation pipeline -----------------------------------------------------

    async def observe(
        self,
        movement_id: str,
        inputs: SituationInputs,
        last_gps_age_sec: float = 0.0,
        ambulance_delay_projection_sec: float = 0.0,
        provenance: dict | None = None,
    ) -> ObservationResult:
        """Classify one observation and log it to adaptive.situation_log.

        ``provenance`` (additive) records WHERE the inputs came from — the
        engine loop labels ``source`` ('live_cache' | 'emulator' |
        'inputs_doc'), the congestion basis and coverage, the weather and
        crowd sources and the feed's data quality — next to the classified
        inputs in the audit row's ``inputs`` JSON.
        """
        now = self._clock()
        wall = self._wall()
        state = self._ensure_state(movement_id, inputs.vvip_class)

        situation = self._engine.classify(inputs)
        result = state.hysteresis.evaluate(situation.situation_class, now)
        previous = state.effective_class

        if state.effective_class is None:
            # First observation initializes the band clock without
            # publishing a change notification.
            state.band_enter_monotonic = now
            state.last_transition_monotonic = now
            state.band_since = wall
        elif result.transitioned:
            state.band_enter_monotonic = now
            state.last_transition_monotonic = now
            state.band_since = wall
            await self._sink.publish_situation_change(
                movement_id,
                from_code=previous.code if previous else "NONE",
                to_code=result.effective.code,
                payload_json=json.dumps(
                    {
                        "movement_id": movement_id,
                        "from": previous.code if previous else "NONE",
                        "to": result.effective.code,
                        "situation_code": situation.code,
                        "severity_score": situation.severity_score,
                        "monotonic": now,
                        "at": _iso(wall),
                    }
                ),
            )

        state.last_situation = situation
        state.effective_class = result.effective
        state.last_gps_age_sec = last_gps_age_sec
        state.ambulance_delay_projection_sec = ambulance_delay_projection_sec
        state.crowd_extreme = inputs.crowd.code == "EXTREME"
        state.observation_count += 1
        state.last_observed_at = wall
        state.last_inputs = dict(provenance) if provenance else None
        self._refresh_city_band()

        logged_inputs = situation.inputs.to_dict()
        if provenance:
            logged_inputs.update(
                {k: v for k, v in provenance.items() if _audit_safe(v)}
            )
        await self._persister.log_situation(
            {
                "movement_id": movement_id,
                "vvip_class": inputs.vvip_class.code,
                "situation_code": situation.code,
                "situation_class": result.effective.code,
                "severity_score": situation.severity_score,
                "inputs": logged_inputs,
                "observed_class": situation.situation_class.code,
                "transitioned": result.transitioned,
                "monotonic": now,
            }
        )

        return ObservationResult(
            situation=situation,
            effective_class=result.effective,
            transitioned=result.transitioned,
            previous_class=previous,
        )

    # -- sustained-RED -----------------------------------------------------------

    def _red_sustained_sec(self, state: MovementAdaptiveState) -> float:
        if state.effective_class is not SituationClass.RED:
            return 0.0
        return max(0.0, self._clock() - state.band_enter_monotonic)

    # -- C4 param pipeline ----------------------------------------------------------

    def current_params(self, movement_id: str | None, vvip_class: SecurityTier) -> dict:
        """READ-ONLY C4 answer (``GET /adaptive/params``).

        A tracked subject answers its last published decision (or, before
        the loop's first publish, the decision its current band implies,
        computed without a twin run and without persisting).  An unknown
        or missing ``movement_id`` answers the tier's floor-clamped
        baseline WITHOUT creating state.  Nothing here writes the audit
        table, the mirror or the subject map.
        """
        subject = self.resolve(movement_id)
        state = self._movements.get(subject) if subject else None
        if state is None:
            decision = self._adapter.decide(
                situation_class=SituationClass.GREEN,
                vvip_class=vvip_class,
                shadow_status=ShadowEvalStatus.UNAVAILABLE,
                shadow_detail="subject_not_tracked",
                force_baseline=self._degradation.forces_baseline(),
            )
            return decision.to_response(
                {
                    "movement_id": movement_id,
                    "vvip_class": vvip_class.code,
                    "subject_tracked": False,
                    "observation_count": 0,
                    "degradation_tier": self._degradation.tier.code,
                    "read_only": True,
                }
            )
        if state.published is not None:
            response = json.loads(json.dumps(state.published))
            response["evidence"].update(
                {
                    "subject_id": subject,
                    "subject_tracked": True,
                    "published": True,
                    "published_at": _iso(state.published_at),
                    "read_only": True,
                }
            )
            return response
        band = state.effective_class or SituationClass.GREEN
        decision = self._adapter.decide(
            situation_class=band,
            vvip_class=state.vvip_class,
            shadow_status=state.shadow_status,
            shadow_detail=state.shadow_detail,
            force_baseline=self._degradation.forces_baseline(),
        )
        return decision.to_response(
            {
                "movement_id": movement_id,
                "subject_id": subject,
                "vvip_class": state.vvip_class.code,
                "subject_tracked": True,
                "published": False,
                "observation_count": state.observation_count,
                "degradation_tier": self._degradation.tier.code,
                "read_only": True,
            }
        )

    async def params_for(
        self,
        movement_id: str,
        vvip_class: SecurityTier,
        shadow_context: ShadowContext | None = None,
        requested_source: ParamSource = ParamSource.RETRIEVED,
    ) -> dict:
        """Decide the subject's C4 params; persist + publish ONLY on change.

        The decision path (situation-engine loop, ``POST /adaptive/observe``).
        For an untracked subject this degrades to the read-only answer —
        it never mints state for an arbitrary id.  Decisions for one subject
        are serialised: a ``POST /adaptive/observe`` racing a loop tick would
        otherwise both compare against the old signature across the twin and
        audit awaits and write the same change twice.
        """
        state = self._movements.get(movement_id)
        if state is None:
            return self.current_params(movement_id, vvip_class)
        async with state.decision_lock:
            if self._movements.get(movement_id) is not state:
                # evicted while waiting: a forgotten subject is never audited
                return self.current_params(movement_id, vvip_class)
            return await self._decide(
                state, movement_id, shadow_context, requested_source
            )

    async def _decide(
        self,
        state: MovementAdaptiveState,
        movement_id: str,
        shadow_context: ShadowContext | None,
        requested_source: ParamSource,
    ) -> dict:
        tier = state.vvip_class
        band = state.effective_class or SituationClass.GREEN

        candidate = self._adapter.candidate_params(band, tier)
        needs_shadow = not self._adapter.candidate_is_baseline_shaped(
            candidate, band, tier
        )
        if needs_shadow:
            shadow_status, shadow_detail = await self._shadow_verdict(
                state, band, tier, candidate.advance_closure_sec, shadow_context
            )
        else:
            shadow_status = ShadowEvalStatus.UNAVAILABLE
            shadow_detail = "shadow_eval_not_required"
            state.shadow_key = None
        state.shadow_status = shadow_status
        state.shadow_detail = shadow_detail
        state.shadow_failed = shadow_status is ShadowEvalStatus.FAILED

        decision: PolicyDecision = self._adapter.decide(
            situation_class=band,
            vvip_class=tier,
            shadow_status=shadow_status,
            shadow_detail=shadow_detail,
            requested_source=requested_source,
            force_baseline=self._degradation.forces_baseline(),
        )

        escalation = self._evaluate_escalation(state, band, tier)

        evidence_extra = {
            "movement_id": movement_id,
            "vvip_class": tier.code,
            "observation_count": state.observation_count,
            "situation_code": (
                state.last_situation.code if state.last_situation else None
            ),
            "shadow_eval_required": needs_shadow,
            "degradation_tier": self._degradation.tier.code,
            "escalation": escalation.to_dict(),
            "inputs_source": (state.last_inputs or {}).get("source"),
        }
        response = decision.to_response(evidence_extra)
        signature = _Signature(
            band=response["situation_class"],
            params=json.dumps(response["params"], sort_keys=True),
            source=response["source"],
            shadow_eval=response["shadow_eval"],
            shadow_detail=_detail_signature(shadow_detail),
            fail_closed_reason=decision.fail_closed_reason,
            escalation_level=escalation.level.code,
            escalation_reasons=tuple(escalation.reasons),
            degradation_tier=self._degradation.tier.code,
        )
        now = self._clock()
        payload_json = json.dumps(response["params"])

        if signature != state.published_signature:
            reason = _publish_reason(state.published_signature, signature)
            wall = self._wall()
            await self._sink.publish_params(movement_id, payload_json)
            audited = await self._persister.log_params(
                {
                    "movement_id": movement_id,
                    "vvip_class": tier.code,
                    "situation_class": band.code,
                    "params": response["params"],
                    "source": response["source"],
                    "shadow_eval": response["shadow_eval"],
                    "evidence": {
                        **{
                            k: v for k, v in response["evidence"].items()
                            if _audit_safe(v)
                        },
                        "publish_reason": reason,
                    },
                    "monotonic": now,
                }
            )
            # The decision is live either way (mirror above, GET below).
            state.published = response
            state.published_at = wall
            state.mirror_refreshed_monotonic = now
            if audited is False:
                # The audit row did not land (database blip): keep the old
                # signature, so the next decision re-attempts it — a change
                # is never left unaudited.
                state.audit_failures += 1
                logger.warning(
                    "adaptive.params_audit_retry",
                    movement_id=movement_id,
                    band=band.code,
                    reason=reason,
                    failures=state.audit_failures,
                )
                return response
            state.published_signature = signature
            state.publish_count += 1
            logger.info(
                "adaptive.params_published",
                movement_id=movement_id,
                band=band.code,
                source=response["source"],
                shadow_eval=response["shadow_eval"],
                reason=reason,
            )
        else:
            # Unchanged decision: no audit row; keep the TTL mirror alive.
            state.published = response
            last = state.mirror_refreshed_monotonic
            if last is None or now - last >= PARAMS_MIRROR_REFRESH_SEC:
                await self._sink.publish_params(movement_id, payload_json)
                state.mirror_refreshed_monotonic = now
        return response

    async def _shadow_verdict(
        self,
        state: MovementAdaptiveState,
        band: SituationClass,
        tier: SecurityTier,
        candidate_advance_sec: int,
        ctx: ShadowContext | None,
    ) -> tuple[ShadowEvalStatus, str]:
        """Twin shadow-eval of the band's candidate for ONE subject.

        Fail-closed in every degraded case; a passing/failing verdict is
        cached per (band, tier, candidate, route, departure) and re-run
        after ``SHADOW_REEVAL_SEC``.
        """
        if ctx is None:
            return ShadowEvalStatus.UNAVAILABLE, "shadow_context_missing"
        if self._twin_readiness is not None:
            calibrated, reason = self._twin_readiness()
            if not calibrated:
                detail = "twin_not_calibrated"
                if reason:
                    detail = f"{detail}: {reason}"
                self._note_shadow(state.movement_id, ShadowEvalStatus.UNAVAILABLE, detail)
                state.shadow_key = None
                return ShadowEvalStatus.UNAVAILABLE, detail
        key = (
            band.code,
            tier.code,
            candidate_advance_sec,
            ctx.route_segment_ids,
            ctx.departure_time,
        )
        now = self._clock()
        if (
            state.shadow_key == key
            and state.shadow_checked_monotonic is not None
            and now - state.shadow_checked_monotonic < SHADOW_REEVAL_SEC
        ):
            return state.shadow_status, state.shadow_detail
        # The baseline arm runs what baseline would PUBLISH for this tier
        # (the protocol floor applies to baseline too: Z+ = 180 s).
        baseline_run, candidate_run, error = await self._run_twin_pair(
            ClosurePolicy(
                advance_closure_sec=clamp_advance_closure(
                    BASELINE_ADVANCE_CLOSURE_SEC, tier
                )
            ),
            ClosurePolicy(advance_closure_sec=candidate_advance_sec),
            ctx,
        )
        outcome = self._shadow.evaluate(
            self._adapter.candidate_params(band, tier),
            baseline_run,
            candidate_run,
            twin_error=error,
        )
        state.shadow_key = key
        state.shadow_checked_monotonic = now
        self._note_shadow(state.movement_id, outcome.status, outcome.detail)
        return outcome.status, outcome.detail

    def _note_shadow(
        self, movement_id: str, status: ShadowEvalStatus, detail: str
    ) -> None:
        self._last_shadow = {
            "status": status.code,
            "detail": detail,
            "movement_id": movement_id,
            "at": _iso(self._wall()),
        }

    def _evaluate_escalation(
        self,
        state: MovementAdaptiveState,
        band: SituationClass,
        vvip_class: SecurityTier,
    ) -> EscalationDecision:
        side = EscalationInputs(
            last_gps_age_sec=state.last_gps_age_sec,
            ambulance_delay_projection_sec=state.ambulance_delay_projection_sec,
            red_sustained_sec=self._red_sustained_sec(state),
            shadow_eval_failed=state.shadow_failed,
            decider_checkpoints_paused=(
                self._degradation.tier is not DegradationTier.FULL
            ),
            crowd_extreme=state.crowd_extreme,
        )
        return self._escalation.evaluate(
            band, vvip_class, self._degradation.tier, side
        )

    async def _run_twin_pair(
        self,
        baseline: ClosurePolicy,
        candidate: ClosurePolicy,
        ctx: ShadowContext,
    ) -> tuple[Any, Any, str | None]:
        port = self._twin_port
        run_pair_async = getattr(port, "run_pair_async", None)
        if run_pair_async is not None:
            pair = await run_pair_async(baseline, candidate, ctx)
        else:
            pair = port.run_pair(baseline, candidate, ctx)
        return pair.baseline, pair.candidate, pair.error

    # -- status ---------------------------------------------------------------

    def status(self) -> dict:
        """Degradation, every TRACKED subject, the city band and shadow state.

        ``movements`` holds exactly the loop-active subjects with a band
        (the city band and ``/signals/plans`` band_subjects read it), each
        with its band age (``band_since``), the pending hysteresis streak,
        its own shadow verdict and its last publish.
        """
        now = self._clock()
        movements: dict[str, dict] = {}
        for mid, s in sorted(self._movements.items()):
            hyst = s.hysteresis.state
            published = s.published or {}
            movements[mid] = {
                "vvip_class": s.vvip_class.code,
                "effective_class": (
                    s.effective_class.code if s.effective_class else None
                ),
                "situation_code": (
                    s.last_situation.code if s.last_situation else None
                ),
                "severity_score": (
                    s.last_situation.severity_score
                    if s.last_situation
                    else None
                ),
                "observation_count": s.observation_count,
                "red_sustained_sec": round(self._red_sustained_sec(s), 1),
                "observed_class": (
                    s.last_situation.situation_class.code
                    if s.last_situation
                    else None
                ),
                "band_since": _iso(s.band_since),
                "band_age_sec": round(max(0.0, now - s.band_enter_monotonic), 1),
                "pending_class": hyst.candidate.code if hyst.candidate else None,
                "pending_count": hyst.candidate_count,
                "last_observed_at": _iso(s.last_observed_at),
                "inputs_source": (s.last_inputs or {}).get("source"),
                # what the band stands on: data quality of the live-cache
                # rows ('real' | 'synthetic' | 'mixed') and whether any
                # input was simulated or emulated (surfaces label it so);
                # None when the inputs did not say (explicit feed docs)
                "inputs_data_quality": (s.last_inputs or {}).get("data_quality"),
                "simulated_feed": (s.last_inputs or {}).get("simulated_feed"),
                "shadow_eval": {
                    "status": s.shadow_status.code,
                    "detail": s.shadow_detail,
                },
                "published": (
                    {
                        "situation_class": published.get("situation_class"),
                        "source": published.get("source"),
                        "shadow_eval": published.get("shadow_eval"),
                        "advance_closure_sec": (
                            published.get("params") or {}
                        ).get("advance_closure_sec"),
                        "published_at": _iso(s.published_at),
                        "publish_count": s.publish_count,
                        "audit_failures": s.audit_failures,
                    }
                    if s.published is not None
                    else None
                ),
            }
        return {
            "degradation": self._degradation.status(),
            "movements": movements,
            "city_band": self.city_band(),
            "city_band_since": _iso(self._city_band_since),
            "evicted_total": self._evicted_total,
            "last_shadow_eval": dict(self._last_shadow),
        }


__all__ = [
    "AdaptiveService",
    "MovementAdaptiveState",
    "NullPersister",
    "NullSink",
    "ObservationResult",
    "PARAMS_MIRROR_REFRESH_SEC",
    "PARAMS_MIRROR_TTL_SEC",
    "SHADOW_REEVAL_SEC",
    "SITUATION_CHANGED_TTL_SEC",
    "TwinReadiness",
]
