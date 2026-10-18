"""T2 adaptive layer — situation-aware parameter adaptation for VVIP
convoy movements.

Pure, deterministic core (no model calls, no I/O):

    enums        ranked levels (security/traffic/weather/crowd/bands)
    inputs       raw feed scores → ranked enums (threshold mapping)
    situation    SituationEngine: inputs → situation code + class band
    hysteresis   anti-oscillation filter for class transitions
    policy       ParameterAdapter: protocol floors, C4 fail-closed rule
    escalation   rule table for handing control to humans
    degradation  model slow/unavailable → cheaper tier, status surfaced
    shadow       twin shadow-evaluation gate
    twin         twin ports (in-process ASGI; fails closed when absent)

Orchestration + I/O adapters:

    service      AdaptiveService (ports injected; deterministic core)
    router       FastAPI endpoints (C4 /api/v1/adaptive/params &c.)
    engine_loop  startup hook driving classification for active movements
    persistence  parameter-bound audit writes (situation_log/param_overrides)
    valkey_mirror  params TTL 30 s + situation:changed notifications
"""

from traffic_oracle.adaptive.degradation import (
    DegradationConfig,
    DegradationManager,
    DegradationTier,
    ModelHealthSnapshot,
)
from traffic_oracle.adaptive.enums import (
    CrowdDensity,
    EscalationLevel,
    ParamSource,
    SecurityTier,
    ShadowEvalStatus,
    SituationClass,
    TrafficRegime,
    WeatherCondition,
)
from traffic_oracle.adaptive.escalation import (
    EscalationConfig,
    EscalationDecision,
    EscalationEngine,
)
from traffic_oracle.adaptive.hysteresis import (
    HysteresisConfig,
    HysteresisController,
    HysteresisResult,
)
from traffic_oracle.adaptive.inputs import (
    EscalationInputs,
    SituationInputs,
    crowd_from_score,
    traffic_regime_from_congestion,
    weather_from_severity,
)
from traffic_oracle.adaptive.policy import (
    ADVANCE_CLOSURE_FLOOR_SEC,
    BASELINE_ADVANCE_CLOSURE_SEC,
    BASELINE_CONGESTION_SPIKE_THRESHOLD,
    BASELINE_GPS_POLL_INTERVAL_SEC,
    BASELINE_SCORING_WEIGHTS,
    AdaptiveParams,
    ParameterAdapter,
    PolicyDecision,
    clamp_advance_closure,
)
from traffic_oracle.adaptive.service import (
    AdaptiveService,
    NullPersister,
    NullSink,
)
from traffic_oracle.adaptive.shadow import (
    ShadowEvalOutcome,
    ShadowEvaluator,
    TwinRun,
)
from traffic_oracle.adaptive.situation import (
    Situation,
    SituationEngine,
    all_input_combinations,
    band_from_score,
)

__all__ = [
    "ADVANCE_CLOSURE_FLOOR_SEC",
    "BASELINE_ADVANCE_CLOSURE_SEC",
    "BASELINE_CONGESTION_SPIKE_THRESHOLD",
    "BASELINE_GPS_POLL_INTERVAL_SEC",
    "BASELINE_SCORING_WEIGHTS",
    "AdaptiveParams",
    "AdaptiveService",
    "CrowdDensity",
    "DegradationConfig",
    "DegradationManager",
    "DegradationTier",
    "EscalationConfig",
    "EscalationDecision",
    "EscalationEngine",
    "EscalationInputs",
    "EscalationLevel",
    "HysteresisConfig",
    "HysteresisController",
    "HysteresisResult",
    "ModelHealthSnapshot",
    "NullPersister",
    "NullSink",
    "ParamSource",
    "ParameterAdapter",
    "PolicyDecision",
    "SecurityTier",
    "ShadowEvalOutcome",
    "ShadowEvalStatus",
    "ShadowEvaluator",
    "Situation",
    "SituationClass",
    "SituationEngine",
    "SituationInputs",
    "TrafficRegime",
    "TwinRun",
    "WeatherCondition",
    "all_input_combinations",
    "band_from_score",
    "clamp_advance_closure",
    "crowd_from_score",
    "traffic_regime_from_congestion",
    "weather_from_severity",
]
