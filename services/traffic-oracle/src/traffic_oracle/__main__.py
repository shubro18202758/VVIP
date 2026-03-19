"""Traffic Oracle entry point — ``python -m traffic_oracle``.

Starts an HTTP server (FastAPI / Uvicorn) that exposes all 15 API
endpoints expected by convoy-brain's ToolExecutor and direct frontend
calls via Vite proxy.

Endpoints:
    POST /api/v1/predict/flow           → DSTGAT flow forecaster
    POST /api/v1/predict/eta            → HistGBT ETA prediction
    POST /api/v1/optimize/routes        → OR-Tools corridor router
    POST /api/v1/optimize/diversions    → Diversion planner
    POST /api/v1/evaluate/scenarios     → Monte Carlo scenario sim
    GET  /api/v1/graph/shortest-path    → pgRouting Dijkstra
    GET  /api/v1/graph/k-shortest-paths → pgRouting KSP
    GET  /api/v1/spatial/segments       → Bbox segment query
    GET  /api/v1/spatial/segments/{id}  → Single segment detail
    POST /api/v1/traffic/live           → Valkey cache read
    GET  /api/v1/traffic/historical     → DuckDB hourly aggregates
    GET  /api/v1/anomalies/recent       → Recent anomaly log
    GET  /api/v1/corridor/summary       → Corridor conditions
    GET  /api/v1/traffic/history/{id}   → 24h segment history
    GET  /api/v1/movements/active       → Active convoy movements
    GET  /health                        → Readiness probe
    GET  /metrics                       → Prometheus metrics
"""

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import asyncpg
import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from traffic_oracle.data.cache import TrafficCache
from traffic_oracle.data.config import Settings
from traffic_oracle.data.graph_queries import CorridorGraphDB
from traffic_oracle.data.models import AnomalyRecord, RoadSegment
from traffic_oracle.data.vector_store import TrafficVectorStore
from traffic_oracle.evaluate.scenario_sim import ScenarioSimulator
from traffic_oracle.metrics import (
    get_metrics_response,
    record_api_request,
)
from traffic_oracle.optimize.corridor_router import CorridorRouter
from traffic_oracle.optimize.diversion_planner import DiversionPlanner
from traffic_oracle.predict.eta_model import ETAPredictor
from traffic_oracle.predict.flow_forecaster import FlowForecaster
from traffic_oracle.runtime.gpu_arbiter import GpuArbiter

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class FlowPredictRequest(BaseModel):
    segment_ids: list[int]
    horizons_min: list[int] = Field(default=[5, 10, 15, 30])


class ETAPredictRequest(BaseModel):
    route_length_m: float
    num_segments: int
    avg_predicted_speed: float
    avg_predicted_congestion: float
    hour: int
    dow: int
    num_signals: int = 0
    weighted_road_class_score: float = 50.0


class RouteOptimizeRequest(BaseModel):
    origin_segment: int
    destination_segment: int
    max_candidates: int = 5
    avoid_segments: list[int] = Field(default_factory=list)


class DiversionPlanRequest(BaseModel):
    route_segment_ids: list[int]
    convoy_speed_kmh: float = 60.0
    advance_closure_sec: int = 120
    departure_time: str | None = None


class ScenarioEvalRequest(BaseModel):
    scenarios: list[dict]


class LiveTrafficRequest(BaseModel):
    segment_ids: list[int]


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize shared infrastructure on startup; teardown on shutdown."""
    settings = Settings()

    pool = await asyncpg.create_pool(
        dsn=settings.DATABASE_URL, min_size=2, max_size=10
    )

    cache = TrafficCache(url=settings.VALKEY_URL)
    graph_db = CorridorGraphDB(pool=pool)
    vector_store = TrafficVectorStore(pool=pool)
    arbiter = GpuArbiter()

    forecaster = FlowForecaster(
        onnx_model_path=settings.ONNX_MODEL_PATH,
        cache=cache,
        graph_db=graph_db,
        arbiter=arbiter,
    )

    eta_predictor = ETAPredictor()
    router = CorridorRouter(
        graph_db=graph_db,
        forecaster=forecaster,
        eta_predictor=eta_predictor,
    )
    diversion_planner = DiversionPlanner(forecaster=forecaster)
    scenario_sim = ScenarioSimulator(
        forecaster=forecaster,
        router=router,
        diversion_planner=diversion_planner,
        eta_predictor=eta_predictor,
    )

    app.state.pool = pool
    app.state.cache = cache
    app.state.graph_db = graph_db
    app.state.vector_store = vector_store
    app.state.arbiter = arbiter
    app.state.forecaster = forecaster
    app.state.eta_predictor = eta_predictor
    app.state.router = router
    app.state.diversion_planner = diversion_planner
    app.state.scenario_sim = scenario_sim

    logger.info(
        "traffic_oracle.startup",
        db=settings.DATABASE_URL.split("@")[-1],
        valkey=settings.VALKEY_URL,
    )

    yield

    await cache.close()
    await pool.close()
    logger.info("traffic_oracle.shutdown")


def create_app() -> FastAPI:
    """Build the FastAPI application with all traffic-oracle endpoints."""
    app = FastAPI(
        title="Traffic Oracle",
        version="0.1.0",
        description="Predictive traffic modeling API for VVIP corridor planning",
        lifespan=lifespan,
    )

    # -------------------------------------------------------------------
    # Middleware — request metrics
    # -------------------------------------------------------------------

    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        start = time.monotonic()
        response = await call_next(request)
        duration = time.monotonic() - start
        record_api_request(
            method=request.method,
            endpoint=request.url.path,
            status=response.status_code,
            duration=duration,
        )
        return response

    # -------------------------------------------------------------------
    # Health + Metrics
    # -------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "traffic-oracle"}

    @app.get("/metrics")
    async def metrics() -> Response:
        body, content_type = get_metrics_response()
        return Response(content=body, media_type=content_type)

    # -------------------------------------------------------------------
    # Predict
    # -------------------------------------------------------------------

    @app.post("/api/v1/predict/flow")
    async def predict_flow(req: FlowPredictRequest) -> dict:
        try:
            forecaster: FlowForecaster = app.state.forecaster
            result = await forecaster.predict(req.segment_ids, req.horizons_min)
            return {"predictions": result}
        except Exception as exc:
            logger.error("predict_flow.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/v1/predict/eta")
    async def predict_eta(req: ETAPredictRequest) -> dict:
        try:
            predictor: ETAPredictor = app.state.eta_predictor
            features = ETAPredictor.build_features(
                route_length_m=req.route_length_m,
                num_segments=req.num_segments,
                avg_speed=req.avg_predicted_speed,
                avg_congestion=req.avg_predicted_congestion,
                hour=req.hour,
                dow=req.dow,
                num_signals=req.num_signals,
                road_class_score=req.weighted_road_class_score,
            )
            if not predictor._is_fitted:
                # Fallback: simple distance / speed estimate
                speed_ms = max(req.avg_predicted_speed * 1000 / 3600, 0.1)
                eta_sec = req.route_length_m / speed_ms
                return {"eta_seconds": round(eta_sec, 1), "model": "fallback"}
            eta = predictor.predict(features)
            return {"eta_seconds": round(float(eta[0]), 1), "model": "histgbt"}
        except Exception as exc:
            logger.error("predict_eta.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    # -------------------------------------------------------------------
    # Optimize
    # -------------------------------------------------------------------

    @app.post("/api/v1/optimize/routes")
    async def optimize_routes(req: RouteOptimizeRequest) -> dict:
        try:
            router: CorridorRouter = app.state.router
            candidates = await router.find_routes(
                origin_segment=req.origin_segment,
                destination_segment=req.destination_segment,
                max_candidates=req.max_candidates,
                avoid_segments=req.avoid_segments or None,
            )
            return {
                "routes": [
                    {
                        "route_id": c.route_id,
                        "segment_ids": c.segment_ids,
                        "total_distance_m": c.total_distance_m,
                        "estimated_time_sec": c.estimated_time_sec,
                        "disruption_score": c.disruption_score,
                        "security_score": c.security_score,
                        "composite_score": c.composite_score,
                    }
                    for c in candidates
                ]
            }
        except Exception as exc:
            logger.error("optimize_routes.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/v1/optimize/diversions")
    async def optimize_diversions(req: DiversionPlanRequest) -> dict:
        try:
            planner: DiversionPlanner = app.state.diversion_planner
            departure = None
            if req.departure_time:
                departure = datetime.fromisoformat(req.departure_time)
            plans = await planner.plan_diversions(
                route_segment_ids=req.route_segment_ids,
                convoy_speed_kmh=req.convoy_speed_kmh,
                advance_closure_sec=req.advance_closure_sec,
                departure_time=departure,
            )
            return {
                "diversions": [
                    {
                        "segment_id": p.segment_id,
                        "diversion_type": p.diversion_type,
                        "activate_at": p.activate_at.isoformat(),
                        "deactivate_at": p.deactivate_at.isoformat(),
                        "alt_segment_ids": p.alt_segment_ids,
                        "estimated_queue_m": p.estimated_queue_m,
                        "dissipation_time_sec": p.dissipation_time_sec,
                    }
                    for p in plans
                ]
            }
        except Exception as exc:
            logger.error("optimize_diversions.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    # -------------------------------------------------------------------
    # Evaluate
    # -------------------------------------------------------------------

    @app.post("/api/v1/evaluate/scenarios")
    async def evaluate_scenarios(req: ScenarioEvalRequest) -> dict:
        try:
            sim: ScenarioSimulator = app.state.scenario_sim
            results = await sim.evaluate(req.scenarios)
            return {
                "results": [
                    {
                        "scenario_id": r.scenario_id,
                        "route_name": r.route_name,
                        "total_disruption_vehicle_hours": r.total_disruption_vehicle_hours,
                        "max_queue_length_m": r.max_queue_length_m,
                        "avg_closure_duration_sec": r.avg_closure_duration_sec,
                        "segments_affected": r.segments_affected,
                        "estimated_complaints_risk": r.estimated_complaints_risk,
                        "convoy_transit_time_sec": r.convoy_transit_time_sec,
                    }
                    for r in results
                ]
            }
        except Exception as exc:
            logger.error("evaluate_scenarios.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    # -------------------------------------------------------------------
    # Graph (pgRouting)
    # -------------------------------------------------------------------

    @app.get("/api/v1/graph/shortest-path")
    async def shortest_path(
        source: int = Query(...), target: int = Query(...)
    ) -> dict:
        try:
            graph_db: CorridorGraphDB = app.state.graph_db
            path = await graph_db.shortest_path(source, target)
            return {"path": path}
        except Exception as exc:
            logger.error("shortest_path.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/v1/graph/k-shortest-paths")
    async def k_shortest_paths(
        source: int = Query(...),
        target: int = Query(...),
        k: int = Query(default=5),
    ) -> dict:
        try:
            graph_db: CorridorGraphDB = app.state.graph_db
            paths = await graph_db.k_shortest_paths(source, target, k)
            return {"paths": paths}
        except Exception as exc:
            logger.error("k_shortest_paths.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    # -------------------------------------------------------------------
    # Spatial
    # -------------------------------------------------------------------

    @app.get("/api/v1/spatial/segments")
    async def spatial_segments(
        min_lon: float = Query(...),
        min_lat: float = Query(...),
        max_lon: float = Query(...),
        max_lat: float = Query(...),
    ) -> dict:
        try:
            pool: asyncpg.Pool = app.state.pool
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT segment_id, osm_way_id, road_name, road_class,
                           lanes, speed_limit_kmh, oneway,
                           ST_AsGeoJSON(geom)::json AS geojson
                    FROM corridor.road_segments
                    WHERE geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)
                    ORDER BY segment_id
                    LIMIT 2000;
                    """,
                    min_lon, min_lat, max_lon, max_lat,
                )
            return {
                "segments": [
                    {
                        "segment_id": r["segment_id"],
                        "osm_way_id": r["osm_way_id"],
                        "road_name": r["road_name"],
                        "road_class": r["road_class"],
                        "lanes": r["lanes"],
                        "speed_limit_kmh": r["speed_limit_kmh"],
                        "oneway": r["oneway"],
                        "geometry": (
                            json.loads(r["geojson"]) if isinstance(r["geojson"], str) else r["geojson"]
                        ) if r["geojson"] else None,
                    }
                    for r in rows
                ],
            }
        except Exception as exc:
            logger.error("spatial_segments.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/v1/spatial/segments/{segment_id}")
    async def segment_detail(segment_id: int) -> dict:
        try:
            pool: asyncpg.Pool = app.state.pool
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT segment_id, osm_way_id, road_name, road_class,
                           lanes, speed_limit_kmh, oneway,
                           ST_AsGeoJSON(geom)::json AS geojson,
                           ST_Length(geom::geography) AS length_m
                    FROM corridor.road_segments
                    WHERE segment_id = $1;
                    """,
                    segment_id,
                )
            if not row:
                raise HTTPException(status_code=404, detail="Segment not found")
            return {
                "segment_id": row["segment_id"],
                "osm_way_id": row["osm_way_id"],
                "road_name": row["road_name"],
                "road_class": row["road_class"],
                "lanes": row["lanes"],
                "speed_limit_kmh": row["speed_limit_kmh"],
                "oneway": row["oneway"],
                "geometry": (
                    json.loads(row["geojson"]) if isinstance(row["geojson"], str) else row["geojson"]
                ) if row["geojson"] else None,
                "length_m": round(row["length_m"], 1),
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("segment_detail.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    # -------------------------------------------------------------------
    # Traffic
    # -------------------------------------------------------------------

    @app.post("/api/v1/traffic/live")
    async def live_traffic(req: LiveTrafficRequest) -> dict:
        try:
            cache: TrafficCache = app.state.cache
            results = []
            for sid in req.segment_ids:
                obs = await cache.get_latest(sid)
                if obs:
                    results.append(obs.model_dump())
            return {"observations": results}
        except Exception as exc:
            logger.error("live_traffic.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/v1/traffic/historical")
    async def traffic_historical(
        segment_id: int = Query(...),
        hours: int = Query(default=24),
    ) -> dict:
        try:
            pool: asyncpg.Pool = app.state.pool
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT segment_id, hour_utc,
                           avg_speed_kmh, p50_speed_kmh,
                           p95_congestion, observation_cnt
                    FROM traffic.hourly_aggregates
                    WHERE segment_id = $1 AND hour_utc >= $2
                    ORDER BY hour_utc;
                    """,
                    segment_id, cutoff,
                )
            return {
                "segment_id": segment_id,
                "aggregates": [
                    {
                        "hour_utc": r["hour_utc"].isoformat(),
                        "avg_speed_kmh": float(r["avg_speed_kmh"]) if r["avg_speed_kmh"] else None,
                        "p50_speed_kmh": float(r["p50_speed_kmh"]) if r["p50_speed_kmh"] else None,
                        "p95_congestion": float(r["p95_congestion"]) if r["p95_congestion"] else None,
                        "observation_cnt": r["observation_cnt"],
                    }
                    for r in rows
                ],
            }
        except Exception as exc:
            logger.error("traffic_historical.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/v1/traffic/history/{segment_id}")
    async def traffic_segment_history(segment_id: int) -> dict:
        """24h history for a single segment — MCP resource endpoint."""
        try:
            pool: asyncpg.Pool = app.state.pool
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT timestamp_utc, speed_kmh, congestion_idx, source
                    FROM traffic.observations
                    WHERE segment_id = $1 AND timestamp_utc >= $2
                    ORDER BY timestamp_utc DESC
                    LIMIT 500;
                    """,
                    segment_id, cutoff,
                )
            return {
                "segment_id": segment_id,
                "observations": [
                    {
                        "timestamp_utc": r["timestamp_utc"].isoformat(),
                        "speed_kmh": float(r["speed_kmh"]),
                        "congestion_idx": float(r["congestion_idx"]),
                        "source": r["source"],
                    }
                    for r in rows
                ],
            }
        except Exception as exc:
            logger.error("traffic_segment_history.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    # -------------------------------------------------------------------
    # Anomalies
    # -------------------------------------------------------------------

    @app.get("/api/v1/anomalies/recent")
    async def anomalies_recent(
        hours: int = Query(default=1),
        limit: int = Query(default=100),
    ) -> dict:
        try:
            pool: asyncpg.Pool = app.state.pool
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT anomaly_id, segment_id, timestamp_utc,
                           anomaly_type, severity, details
                    FROM traffic.anomaly_log
                    WHERE timestamp_utc >= $1
                    ORDER BY timestamp_utc DESC
                    LIMIT $2;
                    """,
                    cutoff, limit,
                )
            return {
                "anomalies": [
                    {
                        "anomaly_id": r["anomaly_id"],
                        "segment_id": r["segment_id"],
                        "timestamp_utc": r["timestamp_utc"].isoformat(),
                        "anomaly_type": r["anomaly_type"],
                        "severity": r["severity"],
                        "details": dict(r["details"]) if r["details"] else {},
                    }
                    for r in rows
                ]
            }
        except Exception as exc:
            logger.error("anomalies_recent.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    # -------------------------------------------------------------------
    # Corridor summary
    # -------------------------------------------------------------------

    @app.get("/api/v1/corridor/summary")
    async def corridor_summary() -> dict:
        try:
            cache: TrafficCache = app.state.cache
            pool: asyncpg.Pool = app.state.pool

            # Try cache first
            cached = await cache.get_corridor_summary("default")
            if cached:
                return cached

            # Compute from live data
            async with pool.acquire() as conn:
                stats = await conn.fetchrow(
                    """
                    SELECT COUNT(DISTINCT segment_id) AS total_segments,
                           AVG(speed_kmh) AS avg_speed,
                           AVG(congestion_idx) AS avg_congestion,
                           COUNT(*) FILTER (WHERE congestion_idx > 0.7) AS congested_count,
                           COUNT(*) FILTER (WHERE congestion_idx > 0.9) AS critical_count
                    FROM traffic.observations
                    WHERE timestamp_utc >= NOW() - INTERVAL '10 minutes';
                    """
                )

            summary = {
                "total_segments": stats["total_segments"] or 0,
                "avg_speed_kmh": round(float(stats["avg_speed"] or 0), 1),
                "avg_congestion_idx": round(float(stats["avg_congestion"] or 0), 3),
                "congested_segments": stats["congested_count"] or 0,
                "critical_segments": stats["critical_count"] or 0,
                "status": "green",
            }

            # Classify corridor status
            avg_cong = summary["avg_congestion_idx"]
            if avg_cong > 0.7:
                summary["status"] = "red"
            elif avg_cong > 0.4:
                summary["status"] = "amber"

            await cache.set_corridor_summary("default", summary, ttl_sec=60)
            return summary
        except Exception as exc:
            logger.error("corridor_summary.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    # -------------------------------------------------------------------
    # Active movements (convoy context from Valkey)
    # -------------------------------------------------------------------

    @app.get("/api/v1/movements/active")
    async def active_movements() -> dict:
        try:
            pool: asyncpg.Pool = app.state.pool
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT movement_id, vvip_class, status,
                           ST_AsGeoJSON(origin_geom)::json AS origin,
                           ST_AsGeoJSON(destination_geom)::json AS destination,
                           planned_start
                    FROM convoy.movements
                    WHERE status IN ('planning', 'approved', 'active')
                    ORDER BY planned_start;
                    """
                )
            return {
                "movements": [
                    {
                        "movement_id": str(r["movement_id"]),
                        "vvip_class": r["vvip_class"],
                        "status": r["status"],
                        "origin": (
                            json.loads(r["origin"]) if isinstance(r["origin"], str) else r["origin"]
                        ) if r["origin"] else None,
                        "destination": (
                            json.loads(r["destination"]) if isinstance(r["destination"], str) else r["destination"]
                        ) if r["destination"] else None,
                        "planned_start": r["planned_start"].isoformat() if r["planned_start"] else None,
                    }
                    for r in rows
                ]
            }
        except Exception as exc:
            logger.error("active_movements.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    # -------------------------------------------------------------------
    # Traffic snapshot (frontend-facing, Vite proxy at /api/traffic)
    # -------------------------------------------------------------------

    @app.get("/api/traffic/snapshot")
    async def traffic_snapshot(
        min_lon: float = Query(...),
        min_lat: float = Query(...),
        max_lon: float = Query(...),
        max_lat: float = Query(...),
    ) -> list[dict]:
        """Live traffic snapshot for map viewport — consumed by command-deck."""
        try:
            pool: asyncpg.Pool = app.state.pool
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT ON (o.segment_id)
                           o.segment_id, o.speed_kmh, o.congestion_idx,
                           EXTRACT(EPOCH FROM o.timestamp_utc) * 1000 AS last_updated
                    FROM traffic.observations o
                    JOIN corridor.road_segments s ON o.segment_id = s.segment_id
                    WHERE s.geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)
                      AND o.timestamp_utc >= NOW() - INTERVAL '10 minutes'
                    ORDER BY o.segment_id, o.timestamp_utc DESC;
                    """,
                    min_lon, min_lat, max_lon, max_lat,
                )
            return [
                {
                    "segmentId": r["segment_id"],
                    "speedKmh": float(r["speed_kmh"]),
                    "congestionIdx": float(r["congestion_idx"]),
                    "lastUpdated": int(r["last_updated"]) if r["last_updated"] else 0,
                }
                for r in rows
            ]
        except Exception as exc:
            logger.error("traffic_snapshot.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/traffic/segments")
    async def traffic_segments(
        min_lon: float = Query(...),
        min_lat: float = Query(...),
        max_lon: float = Query(...),
        max_lat: float = Query(...),
    ) -> list[dict]:
        """Road segments in bbox — consumed by command-deck CorridorMap."""
        try:
            pool: asyncpg.Pool = app.state.pool
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT segment_id, osm_way_id, road_name, road_class,
                           lanes, speed_limit_kmh, oneway,
                           ST_AsGeoJSON(geom)::json AS geojson
                    FROM corridor.road_segments
                    WHERE geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)
                    ORDER BY segment_id
                    LIMIT 2000;
                    """,
                    min_lon, min_lat, max_lon, max_lat,
                )
            return [
                {
                    "segmentId": r["segment_id"],
                    "osmWayId": r["osm_way_id"],
                    "roadName": r["road_name"],
                    "roadClass": r["road_class"],
                    "lanes": r["lanes"],
                    "speedLimitKmh": r["speed_limit_kmh"],
                    "oneway": r["oneway"],
                    "geometry": (
                        (json.loads(r["geojson"]) if isinstance(r["geojson"], str) else r["geojson"]).get("coordinates", [])
                        if r["geojson"]
                        else []
                    ),
                }
                for r in rows
            ]
        except Exception as exc:
            logger.error("traffic_segments.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/traffic/predict")
    async def traffic_predict(req: FlowPredictRequest) -> list[dict]:
        """Traffic predictions — consumed by command-deck."""
        try:
            forecaster: FlowForecaster = app.state.forecaster
            result = await forecaster.predict(req.segment_ids, req.horizons_min)
            flat: list[dict] = []
            for sid, horizons in result.items():
                for horizon_min, vals in horizons.items():
                    flat.append({
                        "segmentId": sid,
                        "horizonMin": horizon_min,
                        "predictedSpeedKmh": round(vals["speed_kmh"], 1),
                        "predictedCongestionIdx": round(vals["congestion_idx"], 3),
                        "confidence": 0.8,
                    })
            return flat
        except Exception as exc:
            logger.error("traffic_predict.failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    return app


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8081"))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),
    )

    logger.info("traffic_oracle.starting", host=host, port=port)

    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
