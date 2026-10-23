"""Simulation HTTP surface — contract C5.

Endpoints (APIRouter, included by traffic-oracle's app owner):
    POST /api/v1/simulate/twin    — seeded multi-replication corridor twin
    POST /api/v1/simulate/replay  — calibration report vs traffic observations

Request/response shapes are fixed by C5:
    twin request:  {route_segment_ids, departure_time,
                    closure_policy{advance_closure_sec, closure_type},
                    horizon_min<=60, n_replications<=20, seed}
                   (+ optional scenario_id — additive)
    twin response: {runs: [{replication, total_disruption_vehicle_hours,
                    max_queue_length_m, avg_closure_duration_sec,
                    convoy_transit_time_sec, recovery_sec,
                    ambulance_delay_sec}], calibrated: bool}
                   (+ additive audit fields)
    replay req:    {window_start, window_end}
    replay resp:   calibration report (speed MAE vs traffic.observations;
                   sets the calibrated flag)

The router never requires a database or forecaster: it reads
``request.app.state.pool`` / ``.forecaster`` when present (DB segments the
seed lacks + per-approach DB signal programs per C6, forecast coupling) and
degrades to the deterministic seed-network + internal demand model
otherwise. FastAPI sync-less design: the engine is pure-CPU (~ms per
replication) and runs inline.

Honesty fields (additive): ``forecast_coupling`` names the rung that
actually shaped demand — ``internal`` (the twin's own TOD model) whenever
the forecaster contributed no shape for the route — with
``forecast_coverage`` {covered, route_segments}; ``route_coverage`` says
how many requested segments the twin network models; every run carries
``censored`` (recovery not reached inside the horizon, measured against a
paired no-closure run).  Departure instants are read in IST however they
are written.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from traffic_oracle.simulation.calibration import (
    CALIBRATION,
    build_twin_network,
    persist_calibration,
    run_replay,
)
from traffic_oracle.simulation.engine import (
    ClosurePolicy,
    SimulationConfig,
    SimulationEngine,
    shapes_from_forecaster,
)
from traffic_oracle.simulation.scenarios import get_scenario
from traffic_oracle.simulation.tod import to_ist

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/simulate", tags=["simulation"])


class ClosurePolicyModel(BaseModel):
    advance_closure_sec: int = Field(default=120, ge=0, le=1800)
    closure_type: str = Field(
        default="full_closure",
        description="full_closure | partial_closure | signal_hold",
    )

    @field_validator("closure_type")
    @classmethod
    def _valid_closure_type(cls, v: str) -> str:
        allowed = ("full_closure", "partial_closure", "signal_hold")
        if v not in allowed:
            raise ValueError(f"closure_type must be one of {allowed}")
        return v


class TwinSimRequest(BaseModel):
    # Contract C2: scenario_id resolves ONLY fields the request left unset.
    # Route/departure are therefore optional — a scenario-only POST works
    # with no dummy fields, and an explicit field always wins over the
    # scenario's value (the pre-fix behavior unconditionally clobbered
    # route/departure/policy from the scenario).
    route_segment_ids: list[int] | None = Field(
        default=None, min_length=1, max_length=64
    )
    departure_time: datetime | None = None
    closure_policy: ClosurePolicyModel | None = None
    horizon_min: int | None = Field(default=None, ge=1, le=60)
    n_replications: int | None = Field(default=None, ge=1, le=20)
    seed: int | None = Field(default=None, ge=0)
    scenario_id: str | None = None  # additive: run a named library scenario


class TwinCompareRequest(BaseModel):
    """Contract C2: twin shadow-eval comparison (both arms + deltas).

    The ONLY mechanism the showcase's comparison panel may use — a live
    POST /api/v1/simulate/twin with a scenario_id used to clobber explicit
    policies (router bug, red-team C2), so comparisons go through this
    endpoint where baseline and candidate policies are applied verbatim.
    """

    scenario_id: str
    baseline_policy: ClosurePolicyModel | None = None
    candidate_policy: ClosurePolicyModel
    n_replications: int | None = Field(default=None, ge=1, le=20)
    seed: int | None = Field(default=None, ge=0)


class ReplayRequest(BaseModel):
    window_start: datetime
    window_end: datetime


def _coerce_h(dt: datetime) -> datetime:
    """The instant in IST (Asia/Kolkata); naive datetimes are taken as IST.

    Equivalent instants (``…T03:30Z`` vs ``…T09:00+05:30``) therefore drive
    the identical twin run.
    """
    return to_ist(dt)


async def _build_engine(request: Request) -> tuple[SimulationEngine, dict]:
    """Engine over the live twin network (C6) with the calibrated scales.

    Capacity scales apply only while the twin is calibrated: an
    unvalidated fit never moves the twin.
    """
    pool = getattr(request.app.state, "pool", None)
    network, extras = await build_twin_network(pool)
    if CALIBRATION.calibrated:
        cfg = SimulationConfig(
            capacity_scale=CALIBRATION.capacity_scale,
            class_capacity_scale=dict(CALIBRATION.per_road_class_scale),
        )
    else:
        cfg = SimulationConfig()
    return SimulationEngine(network=network, config=cfg), extras


async def _forecast_shapes(
    request: Request, segment_ids: list[int]
) -> tuple[dict, str, dict]:
    """Forecaster horizon shapes when a forecaster is wired (C5 coupling).

    The twin consumes the POINT fields only (shapes_from_forecaster), so
    the foundation rung's optional ``quantiles`` objects degrade to point
    values here by design. The source label is the rung that actually
    produced the forecast (foundation | dstgat | heuristic) — and
    ``internal`` whenever no shape covers the route (an empty forecast
    used to be labelled with the rung name anyway).  The third value is
    the coverage {covered, route_segments}.
    """
    route = list(dict.fromkeys(int(s) for s in segment_ids))
    coverage = {"covered": 0, "route_segments": len(route)}
    forecaster = getattr(request.app.state, "forecaster", None)
    if forecaster is None:
        return {}, "internal", coverage
    try:
        predict_with_meta = getattr(forecaster, "predict_with_meta", None)
        if predict_with_meta is not None:
            forecast, meta = await predict_with_meta(route, [5, 10, 15, 30])
            rung = meta.rung
        else:
            forecast = await forecaster.predict(route, [5, 10, 15, 30])
            rung = getattr(forecaster, "last_forecast_rung", None)
        shapes = shapes_from_forecaster(forecast)
    except Exception as exc:
        logger.warning("simulate.twin_forecast_unavailable", error=str(exc))
        return {}, "internal", coverage
    coverage["covered"] = sum(1 for sid in route if sid in shapes)
    if not coverage["covered"]:
        return {}, "internal", coverage
    return shapes, rung or "dstgat", coverage


def _route_coverage(results: list, requested: list[int]) -> dict:
    modelled = results[0].route_segments_modelled if results else 0
    return {"modelled": modelled, "requested": len(requested)}


@router.post("/twin")
async def simulate_twin(req: TwinSimRequest, request: Request) -> dict:
    """Run the seeded corridor twin for a corridor event (C5, C2)."""
    route = list(req.route_segment_ids) if req.route_segment_ids else None
    departure = _coerce_h(req.departure_time) if req.departure_time else None
    policy: ClosurePolicy | None = None
    if req.closure_policy is not None:
        policy = ClosurePolicy(
            advance_closure_sec=req.closure_policy.advance_closure_sec,
            closure_type=req.closure_policy.closure_type,
        )
    horizon_min = req.horizon_min
    n_replications = req.n_replications
    seed = req.seed

    # Scenario library membership resolves ONLY the fields the request left
    # unset (contract C2): an explicit route/departure/policy always wins,
    # and a scenario-only POST needs no dummy required fields.
    scenario = None
    if req.scenario_id is not None:
        try:
            scenario = get_scenario(req.scenario_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if route is None:
            route = list(scenario.route_segment_ids)
        if departure is None:
            departure = _coerce_h(scenario.departure_dt())
        if policy is None:
            policy = ClosurePolicy(**scenario.closure_policy)
        if horizon_min is None:
            horizon_min = scenario.horizon_min
        if n_replications is None:
            n_replications = scenario.n_replications
        if seed is None:
            seed = scenario.seed

    if route is None:
        raise HTTPException(
            status_code=422,
            detail="route_segment_ids is required when scenario_id is absent",
        )
    if departure is None:
        raise HTTPException(
            status_code=422,
            detail="departure_time is required when scenario_id is absent",
        )
    if policy is None:
        policy = ClosurePolicy()

    horizon_min = 45 if horizon_min is None else horizon_min
    n_replications = 5 if n_replications is None else n_replications
    seed = 20260101 if seed is None else seed

    try:
        engine, extras = await _build_engine(request)
        shapes, forecast_source, forecast_coverage = await _forecast_shapes(
            request, route
        )

        weather = None
        event_config = None
        scheduled_events = None
        if scenario is not None:
            weather = scenario.weather_state()
            event_config = scenario.event_config()
            scheduled_events = scenario.scheduled_events()

        results = engine.run(
            route_segment_ids=route,
            departure_time=departure,
            closure_policy=policy,
            horizon_min=horizon_min,
            n_replications=n_replications,
            seed=seed,
            weather=weather,
            event_config=event_config,
            scheduled_events=scheduled_events,
            forecast_shapes=shapes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"unknown segment in route: {exc}",
        ) from exc

    response = {
        "runs": [r.to_dict() for r in results],
        "calibrated": CALIBRATION.calibrated,
        # additive audit fields:
        "seed": seed,
        "scenario_id": req.scenario_id,
        "route_segment_ids": route,
        "departure_time": departure.isoformat(),
        "closure_policy": {
            "advance_closure_sec": policy.advance_closure_sec,
            "closure_type": policy.closure_type,
        },
        "horizon_min": horizon_min,
        "n_replications": n_replications,
        "forecast_coupling": forecast_source,
        "forecast_coverage": forecast_coverage,
        "route_coverage": _route_coverage(results, route),
        "censored_runs": sum(1 for r in results if r.censored),
        "calibration": CALIBRATION.to_dict(),
        **extras,
    }
    logger.info(
        "simulate.twin",
        segments=len(route),
        replications=n_replications,
        seed=seed,
        forecast=forecast_source,
        calibrated=CALIBRATION.calibrated,
    )
    return response


@router.post("/replay")
async def simulate_replay(req: ReplayRequest, request: Request) -> dict:
    """Calibrate the twin against observed traffic (C5)."""
    window_start = _coerce_h(req.window_start)
    window_end = _coerce_h(req.window_end)
    if window_end <= window_start:
        raise HTTPException(
            status_code=422, detail="window_end must be after window_start"
        )
    pool = getattr(request.app.state, "pool", None)
    report = await run_replay(pool, window_start, window_end)
    report["trigger"] = "api"
    calibration_id = await persist_calibration(pool, report)
    if calibration_id is not None:
        report["calibration_id"] = calibration_id
    logger.info(
        "simulate.replay",
        status=report.get("status"),
        calibrated=report.get("calibrated"),
        n=report.get("n_observations", 0),
        calibration_id=calibration_id,
    )
    return report


# ── C2: twin shadow-eval comparison (both arms + deltas) ────────────────────


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


_METRIC_KEYS = (
    "total_disruption_vehicle_hours",
    "max_queue_length_m",
    "avg_closure_duration_sec",
    "convoy_transit_time_sec",
    "recovery_sec",
    "ambulance_delay_sec",
)


def _arm_summary(runs: list[dict]) -> dict:
    summary = {
        key: round(_mean([r.get(key, 0.0) for r in runs]), 3) for key in _METRIC_KEYS
    }
    # additive: how many runs never recovered inside the horizon (their
    # recovery_sec is a lower bound, so the mean is one too)
    summary["recovery_censored_runs"] = sum(1 for r in runs if r.get("censored"))
    return summary


@router.post("/twin/compare")
async def simulate_twin_compare(req: TwinCompareRequest, request: Request) -> dict:
    """Run baseline and candidate closure policies on one twin scenario.

    Both arms share the scenario's route/departure/seed/replications and
    differ ONLY in closure policy; the response carries each arm's runs,
    per-metric means, and candidate-minus-baseline deltas.  This is the
    only sanctioned comparison path (a plain /twin POST with scenario_id
    used to clobber explicit policies — red-team C2, now fixed there too).
    """
    try:
        scenario = get_scenario(req.scenario_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    n_replications = req.n_replications or scenario.n_replications
    seed = req.seed if req.seed is not None else scenario.seed
    route = list(scenario.route_segment_ids)
    departure = _coerce_h(scenario.departure_dt())
    horizon_min = scenario.horizon_min

    baseline_policy_model = req.baseline_policy or ClosurePolicyModel(
        **scenario.closure_policy
    )
    arms: dict[str, ClosurePolicy] = {
        "baseline": ClosurePolicy(
            advance_closure_sec=baseline_policy_model.advance_closure_sec,
            closure_type=baseline_policy_model.closure_type,
        ),
        "candidate": ClosurePolicy(
            advance_closure_sec=req.candidate_policy.advance_closure_sec,
            closure_type=req.candidate_policy.closure_type,
        ),
    }

    try:
        engine, extras = await _build_engine(request)
        shapes, forecast_source, forecast_coverage = await _forecast_shapes(
            request, route
        )
        weather = scenario.weather_state()
        event_config = scenario.event_config()
        scheduled_events = scenario.scheduled_events()

        summaries: dict[str, dict] = {}
        run_rows: dict[str, list[dict]] = {}
        for arm_name, arm_policy in arms.items():
            results = engine.run(
                route_segment_ids=route,
                departure_time=departure,
                closure_policy=arm_policy,
                horizon_min=horizon_min,
                n_replications=n_replications,
                seed=seed,
                weather=weather,
                event_config=event_config,
                scheduled_events=scheduled_events,
                forecast_shapes=shapes,
            )
            run_rows[arm_name] = [r.to_dict() for r in results]
            summaries[arm_name] = _arm_summary(run_rows[arm_name])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=422, detail=f"unknown segment in route: {exc}"
        ) from exc

    deltas = {
        key: round(summaries["candidate"][key] - summaries["baseline"][key], 3)
        for key in _METRIC_KEYS
    }

    logger.info(
        "simulate.twin_compare",
        scenario_id=req.scenario_id,
        replications=n_replications,
        seed=seed,
        forecast=forecast_source,
    )
    return {
        "scenario_id": req.scenario_id,
        "seed": seed,
        "n_replications": n_replications,
        "policies": {
            arm: {
                "advance_closure_sec": policy.advance_closure_sec,
                "closure_type": policy.closure_type,
            }
            for arm, policy in arms.items()
        },
        "baseline": {"summary": summaries["baseline"], "runs": run_rows["baseline"]},
        "candidate": {"summary": summaries["candidate"], "runs": run_rows["candidate"]},
        "deltas": deltas,
        "forecast_coupling": forecast_source,
        "forecast_coverage": forecast_coverage,
        "calibrated": CALIBRATION.calibrated,
        "calibration_reason": CALIBRATION.reason,
        **extras,
    }
