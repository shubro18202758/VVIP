"""AdaptiveService — orchestrates the T2 adaptive layer per movement.

Pure-core orchestration: every I/O surface is an injected *port*
(clock, Valkey sink, DB persister, twin port).  The service is
deterministic given the port behavior and the sequence of observations,
which is what lets the unit tests run the full pipeline with fakes.

Responsibilities:
    * per-movement situation state (classifier + hysteresis + dwell)
    * C4 param publishing (protocol floors, twin shadow-eval, fail-closed)
    * escalation evaluation with sustained-RED tracking
    * degradation integration (RULES_ONLY forces baseline)
    * Valkey mirroring (``adaptive:params:{movement_id}`` TTL 30 s,
      ``situation:changed`` on class transitions — the decider cadence
      trigger #3)
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from traffic_oracle.adaptive.degradation import (
    DegradationManager,
    ModelHealthSnapshot,
)
from traffic_oracle.adaptive.escalation import (
    EscalationDecision,
    EscalationEngine,
    EscalationInputs,
)
from traffic_oracle.adaptive.enums import (
    DegradationTier,
    ParamSource,
    SecurityTier,
    ShadowEvalStatus,
    SituationClass,
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
)
from traffic_oracle.adaptive.shadow import ShadowEvaluator
from traffic_oracle.adaptive.situation import Situation, SituationEngine
from traffic_oracle.adaptive.twin import (
    ClosurePolicy,
    ShadowContext,
    TwinPort,
    UnavailableTwinPort,
)

PARAMS_MIRROR_TTL_SEC = 30          # C4: TTL 30 s
SITUATION_CHANGED_TTL_SEC = 60      # decider cadence trigger window


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
    """Audit persistence (situation_log / param_overrides)."""

    async def log_situation(self, record: dict) -> None:
        ...  # pragma: no cover

    async def log_params(self, record: dict) -> None:
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

    async def log_params(self, record: dict) -> None:
        _ = record


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
    ) -> None:
        self._clock = clock
        self._sink = sink or NullSink()
        self._persister = persister or NullPersister()
        self._twin_port = twin_port or UnavailableTwinPort()
        self._engine = SituationEngine()
        self._adapter = ParameterAdapter()
        self._shadow = shadow_evaluator or ShadowEvaluator()
        self._escalation = escalation_engine or EscalationEngine()
        self._degradation = degradation or DegradationManager(clock=clock)
        self._hysteresis_config = hysteresis_config or HysteresisConfig()
        self._movements: dict[str, MovementAdaptiveState] = {}
        self._last_shadow_status: ShadowEvalStatus = ShadowEvalStatus.UNAVAILABLE
        self._last_shadow_detail: str = "no_shadow_eval_yet"
        self._shadow_failed_flag: bool = False

    # -- state access ---------------------------------------------------------

    def movement_state(self, movement_id: str) -> MovementAdaptiveState | None:
        return self._movements.get(movement_id)

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
    ) -> ObservationResult:
        now = self._clock()
        state = self._ensure_state(movement_id, inputs.vvip_class)

        situation = self._engine.classify(inputs)
        result = state.hysteresis.evaluate(situation.situation_class, now)
        previous = state.effective_class

        if state.effective_class is None:
            # First observation initializes the band clock without
            # publishing a change notification.
            state.band_enter_monotonic = now
            state.last_transition_monotonic = now
        elif result.transitioned:
            state.band_enter_monotonic = now
            state.last_transition_monotonic = now
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
                    }
                ),
            )

        state.last_situation = situation
        state.effective_class = result.effective
        state.last_gps_age_sec = last_gps_age_sec
        state.ambulance_delay_projection_sec = ambulance_delay_projection_sec
        state.crowd_extreme = inputs.crowd.code == "EXTREME"
        state.observation_count += 1

        await self._persister.log_situation(
            {
                "movement_id": movement_id,
                "vvip_class": inputs.vvip_class.code,
                "situation_code": situation.code,
                "situation_class": result.effective.code,
                "severity_score": situation.severity_score,
                "inputs": situation.inputs.to_dict(),
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

    async def params_for(
        self,
        movement_id: str,
        vvip_class: SecurityTier,
        shadow_context: ShadowContext | None = None,
        requested_source: ParamSource = ParamSource.RETRIEVED,
    ) -> dict:
        state = self._ensure_state(movement_id, vvip_class)
        band = state.effective_class or SituationClass.GREEN

        candidate = self._adapter.candidate_params(band, vvip_class)
        baseline_closure = ClosurePolicy(
            advance_closure_sec=BASELINE_ADVANCE_CLOSURE_SEC
        )

        shadow_status = ShadowEvalStatus.UNAVAILABLE
        shadow_detail = "shadow_eval_not_required"
        needs_shadow = not self._adapter.candidate_is_baseline_shaped(
            candidate, band, vvip_class
        )

        if needs_shadow:
            if shadow_context is None:
                shadow_status = ShadowEvalStatus.UNAVAILABLE
                shadow_detail = "shadow_context_missing"
            else:
                baseline_run, candidate_run, error = await self._run_twin_pair(
                    baseline_closure,
                    ClosurePolicy(advance_closure_sec=candidate.advance_closure_sec),
                    shadow_context,
                )
                outcome = self._shadow.evaluate(
                    candidate, baseline_run, candidate_run, twin_error=error
                )
                shadow_status = outcome.status
                shadow_detail = outcome.detail

        self._last_shadow_status = shadow_status
        self._last_shadow_detail = shadow_detail
        self._shadow_failed_flag = shadow_status is ShadowEvalStatus.FAILED

        decision: PolicyDecision = self._adapter.decide(
            situation_class=band,
            vvip_class=vvip_class,
            shadow_status=shadow_status,
            shadow_detail=shadow_detail,
            requested_source=requested_source,
            force_baseline=self._degradation.forces_baseline(),
        )

        escalation = self._evaluate_escalation(state, band, vvip_class)

        evidence_extra = {
            "movement_id": movement_id,
            "vvip_class": vvip_class.code,
            "observation_count": state.observation_count,
            "situation_code": (
                state.last_situation.code if state.last_situation else None
            ),
            "shadow_eval_required": needs_shadow,
            "degradation_tier": self._degradation.tier.code,
            "escalation": escalation.to_dict(),
        }
        response = decision.to_response(evidence_extra)

        await self._sink.publish_params(
            movement_id, json.dumps(response["params"])
        )
        await self._persister.log_params(
            {
                "movement_id": movement_id,
                "vvip_class": vvip_class.code,
                "situation_class": band.code,
                "params": response["params"],
                "source": response["source"],
                "shadow_eval": response["shadow_eval"],
                "evidence": {
                    k: v for k, v in response["evidence"].items()
                    if _audit_safe(v)
                },
                "monotonic": self._clock(),
            }
        )
        return response

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
            shadow_eval_failed=self._shadow_failed_flag,
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
        return {
            "degradation": self._degradation.status(),
            "movements": {
                mid: {
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
                }
                for mid, s in sorted(self._movements.items())
            },
            "last_shadow_eval": {
                "status": self._last_shadow_status.code,
                "detail": self._last_shadow_detail,
            },
        }


__all__ = [
    "AdaptiveService",
    "MovementAdaptiveState",
    "NullPersister",
    "NullSink",
    "ObservationResult",
    "PARAMS_MIRROR_TTL_SEC",
    "SITUATION_CHANGED_TTL_SEC",
]
