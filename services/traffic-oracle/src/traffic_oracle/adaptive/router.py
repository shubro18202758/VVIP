"""HTTP surface of the adaptive layer (contract C4 + status/observe).

Endpoints (mounted under ``/api/v1`` by ``__main__``):

    GET  /adaptive/params?movement_id=&vvip_class=
         → C4 response: {situation_class, params, source, shadow_eval,
                          evidence}   (params mirrored to Valkey TTL 30 s)

    GET  /adaptive/status
         → degradation tier + audit trail + per-movement situations

    POST /adaptive/observe
         → feed one observation for a movement (used by the situation
           engine loop and by tests); body carries raw feed scores and
           optional escalation side inputs; returns the effective
           situation and the current C4 param response.

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
    movement_id: str = Query(default="default", max_length=128),
    vvip_class: str = Query(default="Z"),
) -> dict:
    """C4 parameter endpoint (contract shape; see module docstring)."""
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
    return await service.params_for(movement_id, tier)


@router.get("/status")
async def adaptive_status(request: Request) -> dict:
    """Degradation + per-movement situation status."""
    service = getattr(request.app.state, "adaptive", None)
    if service is None:
        return {"service": "not_configured"}
    status = service.status()
    status["service"] = "ok"
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
