"""HTTP surface of the adaptive layer (contract C4 + status/observe).

Endpoints (mounted under ``/api/v1`` by ``__main__``):

    GET  /adaptive/params?movement_id=&vvip_class=
         → C4 response: {situation_class, params, source, shadow_eval,
                          evidence}.  READ-ONLY: the subject's last
           published decision (the situation-engine loop decides,
           persists and mirrors); an unknown or missing movement_id gets
           the tier's floor-clamped baseline.  No audit row, no mirror
           write, no subject state is ever created by a reader.

    GET  /adaptive/status
         → degradation tier + audit trail + the loop-tracked subjects
           (``movements``) + the city band and when it last changed
           (``city_band.since`` / ``city_band_since``) + engine-loop
           health with per-subject ``last_inputs_age_sec`` and
           ``skipped_no_inputs`` (``engine_loop``) + what each band stands
           on (``simulated_feed`` / ``inputs_data_quality``, also on the
           city band) + twin calibration state + the forecaster's history
           feed

    POST /adaptive/observe
         → feed one observation for a movement (tests / explicit feeds);
           body carries raw feed scores and optional escalation side
           inputs; returns the effective situation and the C4 decision
           (persisted only when it changed).

The router never instantiates I/O itself: it looks up the
``AdaptiveService`` wired into ``app.state.adaptive`` by the startup
hook.  When the service is missing (e.g. bare-app tests) the params
endpoint still answers schema-conformant baseline values.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from traffic_oracle.adaptive.enums import SecurityTier, SituationClass
from traffic_oracle.adaptive.inputs import SituationInputs
from traffic_oracle.adaptive.policy import ParameterAdapter
from traffic_oracle.adaptive.twin import ShadowContext

router = APIRouter(prefix="/adaptive", tags=["adaptive"])

_VIP_CLASSES = ("Z+", "Z", "Y", "X", "none")  # 'none' = public/civic event


class ObserveRequest(BaseModel):
    """One observation from the feed layer for one movement."""

    movement_id: str = Field(min_length=1, max_length=128)
    vvip_class: str
    predicted_congestion_idx: float = Field(ge=0.0, le=1.0, default=0.0)
    weather_severity: float = Field(ge=0.0, le=1.0, default=0.0)
    weather: str | None = None
    crowd_score: float = Field(ge=0.0, le=1.0, default=0.0)
    anomaly_active: bool = False
    event_active: bool = False
    # Public-event profile extension (additive, C3): explicit crowd class
    # overrides the score-derived level; duration class is metadata.
    crowd_class: str | None = Field(default=None)
    duration_class: str = Field(default="minutes")
    last_gps_age_sec: float = Field(ge=0.0, default=0.0)
    ambulance_delay_projection_sec: float = Field(ge=0.0, default=0.0)
    route_segment_ids: list[int] = Field(default_factory=list)
    departure_time: str | None = None


class HealthRequest(BaseModel):
    """One model-health snapshot fed to the degradation manager."""

    role: str
    ok: bool
    latency_ms: float = Field(ge=0.0, default=0.0)


def _fallback_params_response(vvip_class: str) -> dict:
    """Schema-conformant baseline answer when no service is wired."""
    tier = SecurityTier.from_code(vvip_class)
    adapter = ParameterAdapter()
    decision = adapter.decide(
        situation_class=SituationClass.GREEN,
        vvip_class=tier,
    )
    return decision.to_response({"movement_id": None, "fallback": True})


@router.get("/params")
async def adaptive_params(
    request: Request,
    movement_id: str | None = Query(default=None, max_length=128),
    vvip_class: str = Query(default="Z"),
) -> dict:
    """C4 parameter endpoint — read-only (see module docstring)."""
    service = getattr(request.app.state, "adaptive", None)
    if service is None:
        try:
            return _fallback_params_response(vvip_class)
        except ValueError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        tier = SecurityTier.from_code(vvip_class)
    except ValueError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service.current_params(movement_id, tier)


def _twin_calibration() -> dict | None:
    """The twin's calibration state (shadow-eval prerequisite), if mounted."""
    try:
        from traffic_oracle.simulation.calibration import CALIBRATION
    except ImportError:  # pragma: no cover - simulation stream absent
        return None
    return CALIBRATION.to_dict()


@router.get("/status")
async def adaptive_status(request: Request) -> dict:
    """Degradation, tracked subjects, city band, loop + feed health."""
    service = getattr(request.app.state, "adaptive", None)
    if service is None:
        return {"service": "not_configured"}
    status = service.status()
    status["service"] = "ok"
    # Model-role health at the top level too (the degradation block keeps
    # its own copy): .roles.decider.last_ok is what operators look for.
    status["roles"] = status["degradation"].get("roles", {})
    loop = getattr(request.app.state, "adaptive_engine_loop", None)
    loop_health = loop.health.snapshot() if loop is not None else None
    status["engine_loop"] = (
        loop_health
        if loop_health is not None
        else {"running": False, "reason": "situation engine not started"}
    )
    # One row per loop-active subject: its input health and (when it has
    # one) its band — a skipped subject shows WHY it has no band.
    subjects: dict[str, dict] = {}
    for sid, health in (loop_health or {}).get("subjects", {}).items():
        band = status["movements"].get(sid)
        subjects[sid] = {
            **health,
            "band": band.get("effective_class") if band else None,
            "band_since": band.get("band_since") if band else None,
            "simulated_feed": band.get("simulated_feed") if band else None,
        }
    status["subjects"] = subjects
    status["twin_calibration"] = _twin_calibration()
    feed = getattr(request.app.state, "history_feed", None)
    if feed is not None:
        status["forecast_history"] = feed.health()
    return status


@router.post("/observe")
async def adaptive_observe(req: ObserveRequest, request: Request) -> dict:
    """Feed one observation; returns effective situation + params."""
    service = getattr(request.app.state, "adaptive", None)
    if service is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="adaptive service not configured")
    try:
        inputs = SituationInputs.from_scores(
            vvip_class=req.vvip_class,
            predicted_congestion_idx=req.predicted_congestion_idx,
            weather_severity=req.weather_severity,
            crowd_score=req.crowd_score,
            anomaly_active=req.anomaly_active,
            event_active=req.event_active,
            weather=req.weather,
            crowd_class=req.crowd_class,
            duration_class=req.duration_class,
        )
    except ValueError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = await service.observe(
        req.movement_id,
        inputs,
        last_gps_age_sec=req.last_gps_age_sec,
        ambulance_delay_projection_sec=req.ambulance_delay_projection_sec,
    )

    shadow_ctx: ShadowContext | None = None
    if req.route_segment_ids and req.departure_time:
        shadow_ctx = ShadowContext(
            route_segment_ids=tuple(req.route_segment_ids),
            departure_time=req.departure_time,
        )

    params_response = await service.params_for(
        req.movement_id, inputs.vvip_class, shadow_context=shadow_ctx
    )
    return {
        "situation": result.situation.to_dict(),
        "effective_class": result.effective_class.code,
        "transitioned": result.transitioned,
        "previous_class": result.previous_class.code if result.previous_class else None,
        "params_response": params_response,
    }


@router.post("/health")
async def adaptive_model_health(req: HealthRequest, request: Request) -> dict:
    """Feed one model-health snapshot to the degradation manager."""
    service = getattr(request.app.state, "adaptive", None)
    if service is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="adaptive service not configured")
    from traffic_oracle.adaptive.degradation import ModelHealthSnapshot

    tier = service.feed_health(
        ModelHealthSnapshot(
            role=req.role, ok=req.ok, latency_ms=req.latency_ms
        )
    )
    return {"degradation_tier": tier.code}


__all__ = ["router"]
