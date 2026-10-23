"""Traffic Oracle entry point — ``python -m traffic_oracle``.

Starts an HTTP server (FastAPI / Uvicorn) that exposes the traffic API
expected by convoy-brain's ToolExecutor and direct frontend calls via
the Vite proxy, plus the adaptive (T2) and simulation (C5) routers.

Endpoints:
    POST /api/v1/predict/flow           → flow forecaster (foundation/DSTGAT/heuristic
                                          ladder) + input provenance
    POST /api/v1/predict/eta            → ETA point estimate + p10/p50/p90 band
                                          whenever segment_ids are given
    POST /api/v1/optimize/routes        → OR-Tools corridor router
    POST /api/v1/optimize/diversions    → Diversion planner
    POST /api/v1/evaluate/scenarios     → Monte Carlo scenario sim
    GET  /api/v1/graph/shortest-path    → pgRouting Dijkstra
    GET  /api/v1/graph/k-shortest-paths → pgRouting KSP
    GET  /api/v1/spatial/segments       → Bbox segment query (spatial.router
                                          when WP-17 lands: display geometry)
    GET  /api/v1/spatial/segments/{id}  → Single segment detail
    POST /api/v1/traffic/live           → Valkey cache read (+ road_name; the
                                          emulator's rows beside, labelled)
    GET  /api/v1/traffic/historical     → hourly aggregates: real (calibration
                                          baseline) + synthetic rollup beside,
                                          provenance + IST weekday/hour basis
    GET  /api/v1/anomalies/recent       → Recent anomaly log
    GET  /api/v1/corridor/summary       → Corridor conditions
    GET  /api/v1/traffic/history/{id}   → 24h segment history
    GET  /api/v1/movements/active       → Active convoy movements
    GET  /api/v1/adaptive/params        → C4 adaptive parameters (read-only)
    GET  /api/v1/adaptive/status        → degradation + subjects + city band +
                                          engine-loop health
    POST /api/v1/adaptive/observe       → feed one situation observation
    POST /api/v1/simulate/twin          → C5 twin (simulation router)
    POST /api/v1/simulate/replay        → C5 calibration (simulation router)
    GET  /api/v1/ops/degradation-taxonomy → decisions_log failure ledger
    GET  /health                        → Readiness probe (+ live ingest block)
    GET  /metrics                       → Prometheus metrics

Live data plane: the lifespan starts the supervised ingest service
(ingest/service.py — NATS corridor.traffic.live / .emulated / .anomaly /
corridor.units.emulated → Valkey hot cache + traffic.observations) once
the pool and caches exist, and drains it before the situation engine stops.
Alongside it: the forecast history feed (predict/history_feed.py — the
foundation rung's 5-min grid from traffic.observations), the twin
calibration scheduler (simulation/scheduler.py — primed before the
situation engine starts, hourly field replay) and the situation engine
(adaptive/engine_loop.py — live bands for every active corridor event).
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
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import AliasChoices, BaseModel, Field

from traffic_oracle.adaptive.engine_loop import start_situation_engine
from traffic_oracle.adaptive.persistence import AsyncpgPersister
from traffic_oracle.adaptive.router import router as adaptive_router
from traffic_oracle.adaptive.service import AdaptiveService
from traffic_oracle.adaptive.twin import build_twin_port
from traffic_oracle.adaptive.valkey_mirror import LoggingValkeySink
from traffic_oracle.data.cache import TrafficCache
from traffic_oracle.data.config import Settings
from traffic_oracle.data.graph_queries import CorridorGraphDB
from traffic_oracle.data.models import DataQuality, TrafficObservation
from traffic_oracle.data.vector_store import TrafficVectorStore
from traffic_oracle.evaluate.scenario_sim import ScenarioInputError, ScenarioSimulator
from traffic_oracle.ingest.cache_writer import emulated_latest_key
from traffic_oracle.ingest.service import IngestService, disabled_health
from traffic_oracle.metrics import (
    get_metrics_response,
    record_api_request,
    route_template_label,
    update_pool_metrics,
)
from traffic_oracle.optimize.constructive_heuristic import DiversionHeuristic
from traffic_oracle.optimize.corridor_router import (
    CorridorRouter,
    _segment_quantiles_from_row,
)
from traffic_oracle.optimize.diversion_planner import (
    DiversionPlanner,
    DiversionUnavailableError,
    coerce_departure_time,
)
from traffic_oracle.optimize.quantile_objective import (
    SegmentQuantiles,
    eta_band_seconds,
)
from traffic_oracle.predict.eta_model import ETAPredictor
from traffic_oracle.predict.flow_forecaster import FlowForecaster
from traffic_oracle.predict.forecast_quantiles import validated_bands
from traffic_oracle.predict.history_feed import HistoryFeed
from traffic_oracle.runtime.gpu_arbiter import GpuArbiter
from traffic_oracle.simulation.calibration import CALIBRATION
from traffic_oracle.simulation.scheduler import CalibrationScheduler

logger = structlog.get_logger(__name__)

# C7 ordering contract: the simulation stream lands simulation/router.py
# and simulation/twin.py (stub-safe).  Import defensively so this service
# still boots if the ordering is violated in a working tree.
try:  # pragma: no cover - depends on parallel stream landing order
    from traffic_oracle.simulation.router import router as simulation_router
except ImportError:  # pragma: no cover
    simulation_router = None
    logger.warning(
        "traffic_oracle.startup",
        msg="simulation.router not importable yet (C7 ordering) — "
        "twin endpoints unavailable, adaptive shadow-eval fails closed",
    )

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
    # convoy-brain's predict_eta tool (mcp/server.py) sends day_of_week /
    # road_class_score; both spellings validate onto the same field.
    dow: int = Field(validation_alias=AliasChoices("dow", "day_of_week"))
    num_signals: int = 0
    weighted_road_class_score: float = Field(
        default=50.0,
        validation_alias=AliasChoices("weighted_road_class_score", "road_class_score"),
    )
    # Optional ETA-band inputs (additive): when segment_ids is given the
    # endpoint additionally computes an honest p10/p50/p90 band from the
    # quantile forecast stream (lengths from the graph DB, quantiles from
    # the app.state provider).  Aggregates above stay the point payload.
    segment_ids: list[int] | None = None
    horizon_min: int = 5


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
    # Each scenario: scenario_id|route_id, route_segment_ids|segment_ids,
    # departure_time, ... (evaluate/scenario_sim.py normalises both
    # spellings).  Bounded: every scenario runs the seeded twin.
    scenarios: list[dict] = Field(max_length=20)


class LiveTrafficRequest(BaseModel):
    segment_ids: list[int]


#: /traffic/historical same-slot look-back (4 weeks of one weekday/hour).
HISTORICAL_SLOT_LOOKBACK_H = 672

# Hourly aggregates of one segment, optionally one IST weekday / hour.  A
# UTC hour bucket straddles two IST half-hours; it is placed at its IST
# MIDPOINT (hour_utc + 30 min is a whole IST hour — the twin calibration's
# convention, simulation/calibration.py bucket_midpoint), so hour=9 is the
# bucket 08:30-09:30 IST.  $3 day_of_week (0 = Monday), $4 hour; NULL = any.
_SQL_HISTORICAL_REAL = "SELECT segment_id, hour_utc, avg_speed_kmh, p50_speed_kmh, p95_congestion, observation_cnt FROM traffic.hourly_aggregates WHERE segment_id = $1 AND hour_utc >= $2 AND ($3::int IS NULL OR EXTRACT(ISODOW FROM (hour_utc + interval '30 minutes') AT TIME ZONE 'Asia/Kolkata')::int - 1 = $3::int) AND ($4::int IS NULL OR EXTRACT(HOUR FROM (hour_utc + interval '30 minutes') AT TIME ZONE 'Asia/Kolkata')::int = $4::int) ORDER BY hour_utc"  # noqa: E501

#: The simulated feed's rollup (migration 019) — same filters, never merged.
_SQL_HISTORICAL_SYNTHETIC = "SELECT segment_id, hour_utc, avg_speed_kmh, p50_speed_kmh, p95_congestion, observation_cnt FROM traffic.hourly_aggregates_synthetic WHERE segment_id = $1 AND hour_utc >= $2 AND ($3::int IS NULL OR EXTRACT(ISODOW FROM (hour_utc + interval '30 minutes') AT TIME ZONE 'Asia/Kolkata')::int - 1 = $3::int) AND ($4::int IS NULL OR EXTRACT(HOUR FROM (hour_utc + interval '30 minutes') AT TIME ZONE 'Asia/Kolkata')::int = $4::int) ORDER BY hour_utc"  # noqa: E501

_SQL_SYNTHETIC_TABLE_PRESENT = "SELECT to_regclass('traffic.hourly_aggregates_synthetic') IS NOT NULL"  # noqa: E501

#: Segment names / limits for /traffic/live rows (refreshed periodically).
_SQL_SEGMENT_NAMES = "SELECT segment_id, road_name, speed_limit_kmh FROM corridor.road_segments"  # noqa: E501
SEGMENT_NAMES_TTL_SEC = 600.0


def _aggregate_out(r) -> dict:
    """One hourly aggregate row on the wire (the pre-C-P shape, unchanged)."""
    return {
        "hour_utc": r["hour_utc"].isoformat(),
        "avg_speed_kmh": float(r["avg_speed_kmh"]) if r["avg_speed_kmh"] else None,
        "p50_speed_kmh": float(r["p50_speed_kmh"]) if r["p50_speed_kmh"] else None,
        "p95_congestion": float(r["p95_congestion"]) if r["p95_congestion"] else None,
        "observation_cnt": r["observation_cnt"],
    }


#: Corridor summary freshness window and cache TTL (seconds).
SUMMARY_WINDOW_SEC = 600
SUMMARY_CACHE_TTL_SEC = 10

# Latest usable (non-anomalous, not future-dated) observation per segment
# inside the freshness window, plus the quality mix of every fresh row.
_SQL_CORRIDOR_SUMMARY = """
WITH fresh AS (
    SELECT segment_id, timestamp_utc, speed_kmh, congestion_idx, data_quality
    FROM traffic.observations
    WHERE timestamp_utc >= NOW() - make_interval(secs => $1)
      AND timestamp_utc <= NOW() + INTERVAL '1 minute'
),
latest AS (
    SELECT DISTINCT ON (segment_id)
           segment_id, timestamp_utc, speed_kmh, congestion_idx, data_quality
    FROM fresh
    WHERE data_quality IS DISTINCT FROM 'anomalous'
    ORDER BY segment_id, timestamp_utc DESC
)
SELECT (SELECT COUNT(*) FROM latest) AS total_segments,
       (SELECT COUNT(*) FROM latest WHERE data_quality = 'synthetic')
           AS emulated_segments,
       (SELECT AVG(speed_kmh) FROM latest) AS avg_speed,
       (SELECT AVG(congestion_idx) FROM latest) AS avg_congestion,
       (SELECT COUNT(*) FROM latest WHERE congestion_idx > 0.7) AS congested_count,
       (SELECT COUNT(*) FROM latest WHERE congestion_idx > 0.9) AS critical_count,
       (SELECT MAX(timestamp_utc) FROM latest) AS newest,
       (SELECT COUNT(*) FROM fresh WHERE data_quality IS DISTINCT FROM 'anomalous'
                                     AND data_quality IS DISTINCT FROM 'synthetic')
           AS real_rows,
       (SELECT COUNT(*) FROM fresh WHERE data_quality = 'synthetic') AS synthetic_rows,
       (SELECT COUNT(*) FROM fresh WHERE data_quality = 'anomalous') AS anomalous_rows
"""

# Newest usable observation anywhere (stale path only): one index probe per
# segment on (segment_id, timestamp_utc DESC) instead of a full-table max().
_SQL_NEWEST_USABLE_OBSERVATION = """
SELECT MAX(t) AS newest FROM (
    SELECT (SELECT o.timestamp_utc
            FROM traffic.observations o
            WHERE o.segment_id = s.segment_id
              AND o.timestamp_utc <= NOW() + INTERVAL '1 minute'
              AND o.data_quality IS DISTINCT FROM 'anomalous'
            ORDER BY o.timestamp_utc DESC
            LIMIT 1) AS t
    FROM corridor.road_segments s
) per_segment
"""


def _iso_z(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


async def compute_corridor_summary(pool: asyncpg.Pool) -> dict:
    """Honest corridor conditions from traffic.observations.

    * Aggregates the LATEST usable observation per segment in the last
      ``SUMMARY_WINDOW_SEC`` (segments weigh equally; congested/critical
      count SEGMENTS, not rows).  Tier-1-flagged ``anomalous`` rows and
      future-dated rows are excluded and reported in ``quality_mix``.
    * Status ``no_data`` whenever no usable fresh observation exists — the
      corridor is never reported green on zero data — with
      ``data_age_sec`` / ``newest_observation_t`` saying how dark it is
      (both null when the table has never held a usable row).
    * ``provenance`` says what the status stands on: ``live`` (no emulated
      segment), ``emulated`` (only emulated segments), ``mixed``, or
      ``none`` (no segment) — a consumer that reads only ``status`` can
      still tell an emulator-driven RED; ``emulated_segments`` counts them.
    * Frozen fields keep their types; everything else is additive.
    """
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        stats = await conn.fetchrow(_SQL_CORRIDOR_SUMMARY, float(SUMMARY_WINDOW_SEC))
        newest = stats["newest"]
        if newest is None:
            newest = await conn.fetchval(_SQL_NEWEST_USABLE_OBSERVATION)

    total = int(stats["total_segments"] or 0)
    emulated = int(stats["emulated_segments"] or 0)
    if total == 0:
        provenance = "none"
    elif emulated == 0:
        provenance = "live"
    else:
        provenance = "emulated" if emulated == total else "mixed"
    data_age_sec = (
        round(max(0.0, (now - newest).total_seconds()), 1) if newest is not None else None
    )
    summary = {
        "total_segments": total,
        "avg_speed_kmh": round(float(stats["avg_speed"] or 0), 1),
        "avg_congestion_idx": round(float(stats["avg_congestion"] or 0), 3),
        "congested_segments": int(stats["congested_count"] or 0),
        "critical_segments": int(stats["critical_count"] or 0),
        "status": "green",
        # additive honesty fields:
        "provenance": provenance,
        "emulated_segments": emulated,
        "data_age_sec": data_age_sec,
        "newest_observation_t": _iso_z(newest),
        "window_sec": SUMMARY_WINDOW_SEC,
        "quality_mix": {
            "real": int(stats["real_rows"] or 0),
            "synthetic": int(stats["synthetic_rows"] or 0),
            "anomalous_excluded": int(stats["anomalous_rows"] or 0),
        },
        "as_of": _iso_z(now),
    }

    if total == 0 or data_age_sec is None or data_age_sec > SUMMARY_WINDOW_SEC:
        summary["status"] = "no_data"
        return summary

    # Classify corridor status
    avg_cong = summary["avg_congestion_idx"]
    if avg_cong > 0.7:
        summary["status"] = "red"
    elif avg_cong > 0.4:
        summary["status"] = "amber"
    return summary


def _jsonb_object(raw: object) -> dict:
    """asyncpg returns jsonb as TEXT (no codec registered) — decode it.

    ``dict(r["details"])`` on that text raised ValueError on the first
    anomaly row; non-object or corrupt payloads degrade to ``{}``.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {"value": value}
    return {}


async def _segment_names(app: FastAPI) -> dict[int, tuple[str | None, float | None]]:
    """segment_id → (road_name, speed_limit_kmh), cached on app.state for
    :data:`SEGMENT_NAMES_TTL_SEC`; a failed refresh keeps the last map (or
    answers {} — rows then carry nulls, never a 500)."""
    cached = getattr(app.state, "segment_names", None)
    now = time.monotonic()
    if cached is not None and cached[0] > now:
        return cached[1]
    pool = getattr(app.state, "pool", None)
    if pool is None:
        return cached[1] if cached else {}
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SQL_SEGMENT_NAMES)
    except Exception as exc:  # noqa: BLE001 — names are garnish on /traffic/live
        logger.warning("live_traffic.segment_names_unavailable", error=repr(exc))
        return cached[1] if cached else {}
    names = {
        int(r["segment_id"]): (
            r["road_name"],
            float(r["speed_limit_kmh"]) if r["speed_limit_kmh"] is not None else None,
        )
        for r in rows
    }
    app.state.segment_names = (now + SEGMENT_NAMES_TTL_SEC, names)
    return names


def _named_row(
    row: dict, names: dict[int, tuple[str | None, float | None]], origin: str
) -> dict:
    """A /traffic/live row + its road name, speed limit and origin."""
    road, limit = names.get(int(row["segment_id"]), (None, None))
    return {**row, "road_name": road, "speed_limit_kmh": limit, "origin": origin}


async def _emulated_rows(
    valkey: object | None,
    segment_ids: list[int],
    names: dict[int, tuple[str | None, float | None]],
) -> list[dict]:
    """The emulator's latest rows for ``segment_ids`` (their own key space,
    ingest/cache_writer.py), labelled — [] when none or Valkey is absent."""
    if valkey is None or not segment_ids:
        return []
    try:
        raws = await valkey.mget([emulated_latest_key(s) for s in segment_ids])  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — the emulated overlay is optional
        logger.warning("live_traffic.emulated_unavailable", error=repr(exc))
        return []
    out: list[dict] = []
    for raw in raws:
        if raw is None:
            continue
        try:
            obs = TrafficObservation.model_validate_json(raw)
        except ValueError:
            continue
        row = _named_row(obs.model_dump(), names, "emulated")
        row["data_quality"] = DataQuality.SYNTHETIC  # an emulated row is never real
        row["emulated"] = True
        out.append(row)
    return out


def _internal_error(event: str) -> HTTPException:
    """Log the active exception (with traceback) and build a generic 500.

    Handlers used to answer ``detail=str(exc)``, leaking DB/driver error
    text to any caller; the detail now names only the failing operation.
    """
    logger.exception(event)
    return HTTPException(status_code=500, detail=f"{event}: internal error")


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
        support_bounds_path=settings.FORECAST_SUPPORT_BOUNDS_PATH,
        foundation_model_path=settings.FOUNDATION_MODEL_PATH,
    )

    eta_predictor = ETAPredictor()
    router = CorridorRouter(
        graph_db=graph_db,
        forecaster=forecaster,
        eta_predictor=eta_predictor,
    )
    diversion_planner = build_diversion_planner(graph_db, forecaster)
    scenario_sim = ScenarioSimulator(
        forecaster=forecaster,
        router=router,
        diversion_planner=diversion_planner,
        eta_predictor=eta_predictor,
        pool=pool,
    )

    # --- adaptive (T2) wiring: service + situation-engine startup hook (C7)
    import valkey.asyncio as valkey_async

    adaptive_valkey = valkey_async.from_url(
        settings.VALKEY_URL, decode_responses=True
    )
    adaptive_service = AdaptiveService(
        clock=time.monotonic,
        sink=LoggingValkeySink(adaptive_valkey),
        persister=AsyncpgPersister(pool),
        twin_port=build_twin_port(app),
        # An uncalibrated twin can never pass the shadow gate: the verdict
        # quotes the calibration reason instead of running a doomed pair.
        twin_readiness=CALIBRATION.readiness,
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
    app.state.adaptive = adaptive_service
    app.state.adaptive_valkey = adaptive_valkey
    # Quantile stream for the ETA band: produced from the live forecaster
    # through the shared mapping layer (never test-only wiring — absent
    # only when the lifespan itself did not run, e.g. bare TestClients).
    app.state.forecast_quantiles = build_forecast_quantiles_provider(forecaster)

    logger.info(
        "traffic_oracle.startup",
        db=settings.DATABASE_URL.split("@")[-1],
        valkey=settings.VALKEY_URL,
    )

    # Live data plane: one supervised NATS connection feeding the Valkey
    # hot cache (traffic:latest:*, units:emulated:*) and PostGIS
    # (traffic.observations, traffic.anomaly_log).  Partition maintenance
    # runs inside the service before its first subscription.  Background
    # task — NATS may come up after the oracle; /health shows the state.
    ingest_service: IngestService | None = None
    if settings.INGEST_ENABLED:
        ingest_service = IngestService(
            nats_url=settings.NATS_URL,
            pool=pool,
            cache=cache,
            valkey_client=adaptive_valkey,
            flush_interval_sec=settings.INGEST_FLUSH_INTERVAL_SEC,
            flush_rows=settings.INGEST_FLUSH_ROWS,
            retention_days=settings.OBSERVATION_RETENTION_DAYS,
            emulated=settings.INGEST_EMULATED,
        )
        ingest_service.start()
    else:
        logger.warning("traffic_oracle.startup", msg="live ingest disabled (VVIP_INGEST_ENABLED)")
    app.state.ingest = ingest_service

    # Forecast history: the foundation rung's 5-min grid, from ingest.
    history_feed = HistoryFeed(pool, forecaster)
    history_feed.start()
    app.state.history_feed = history_feed

    # Twin calibration: reload the persisted fit and check field coverage
    # BEFORE the situation engine starts (bounded, never fatal), so the
    # loop's first decision already quotes the real reason — e.g. "no field
    # observations — simulated feed only" — instead of a placeholder that
    # would stick on the audit trail.  Then replay hourly once >= 24 field
    # hours exist (simulated feeds never count).
    calibration_scheduler = CalibrationScheduler(pool)
    await calibration_scheduler.prime()
    calibration_scheduler.start()
    app.state.calibration_scheduler = calibration_scheduler

    # Situation-engine startup hook: classify active subjects from their
    # live inputs, publish situation:changed transitions, decide + mirror
    # adaptive params (persisted only on change).
    engine_loop = start_situation_engine(
        app, adaptive_service, valkey_client=adaptive_valkey
    )

    yield

    # Drain ingest first: in-flight NATS callbacks finish and the buffered
    # observations/anomalies are flushed while the pool is still open.
    if ingest_service is not None:
        await ingest_service.stop()
    await engine_loop.stop()
    await calibration_scheduler.stop()
    await history_feed.stop()
    forecaster.close()
    await adaptive_valkey.aclose()
    await cache.close()
    await pool.close()
    logger.info("traffic_oracle.shutdown")


def build_diversion_planner(
    graph_db: CorridorGraphDB, forecaster: FlowForecaster | None
) -> DiversionPlanner:
    """Production diversion planner: heuristic over the corridor graph.

    The planner used to be built WITHOUT its heuristic, so every
    /optimize/diversions call logged ``diversion_planner.no_heuristic`` and
    answered [] — the brain's plan_diversions tool silently got nothing.
    """
    return DiversionPlanner(
        heuristic=DiversionHeuristic(graph_db=graph_db),
        forecaster=forecaster,
    )


async def predict_with_meta(
    forecaster: object, segment_ids: list[int], horizons_min: list[int] | None
) -> tuple[dict, object | None]:
    """``forecaster.predict_with_meta`` when it has one (FlowForecaster),
    else plain ``predict`` with no meta (duck-typed forecasters)."""
    with_meta = getattr(forecaster, "predict_with_meta", None)
    if with_meta is not None:
        return await with_meta(segment_ids, horizons_min)
    return await forecaster.predict(segment_ids, horizons_min), None  # type: ignore[attr-defined]


def build_forecast_quantiles_provider(forecaster: FlowForecaster):
    """Production quantile provider for the ETA band (app.state hook).

    Wraps the forecaster's point-shaped ``predict`` and maps each row's
    optional ``quantiles`` object (the shared forecast-quantiles
    contract) into the producer-agnostic ``SegmentQuantiles`` values the
    leaf consumes — via the SAME thin mapping layer the corridor router
    uses (``_segment_quantiles_from_row``), so a producer shape change
    adapts one layer for both consumers.  Rows without the object are
    absent from the mapping; ``eta_band_seconds`` then degrades with the
    labelled reason (point-only producers never fabricate a spread).
    """

    async def provider(
        segment_ids: list[int], horizon_min: int
    ) -> dict[int, SegmentQuantiles]:
        forecasts, _meta = await predict_with_meta(
            forecaster, list(segment_ids), [horizon_min]
        )
        stream: dict[int, SegmentQuantiles] = {}
        for sid, preds in forecasts.items():
            row = preds.get(horizon_min) if isinstance(preds, dict) else None
            if row is None:
                continue
            quantiles = _segment_quantiles_from_row(sid, horizon_min, row)
            if quantiles is not None:
                stream[sid] = quantiles
        return stream

    return provider


async def _compute_eta_band(
    app: FastAPI, segment_ids: list[int], horizon_min: int
) -> tuple[dict | None, str | None]:
    """Optional ETA band from the quantile forecast stream.

    Returns ``(band_payload, None)`` when an honest p10/p50/p90 band was
    computed (all requested segments covered by a monotone quantile
    stream; lengths from the graph DB), else ``(None, reason)`` with the
    precise labelled degradation reason.  The band rides additive-only
    response fields — the two-key point payload is untouched.  The
    provider is wired by the service lifespan from the live forecaster
    (``build_forecast_quantiles_provider``); in test contexts without a
    lifespan it is absent and degrades to ``no_quantile_stream``, never a
    fabricated spread.
    """
    provider = getattr(app.state, "forecast_quantiles", None)
    if provider is None:
        return None, "no_quantile_stream"
    pool = getattr(app.state, "pool", None)
    if pool is None:
        return None, "no_quantile_stream"
    try:
        quantiles = await provider(segment_ids, horizon_min)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT segment_id, ST_Length(geom::geography) AS length_m
                FROM corridor.road_segments
                WHERE segment_id = ANY($1)
                """,
                list(segment_ids),
            )
    except Exception as exc:  # provider/DB failure degrades, never 500s the ETA
        logger.warning("predict_eta.band_unavailable", error=str(exc))
        return None, "no_quantile_stream"
    if not quantiles:
        return None, "no_quantile_stream"
    lengths = {r["segment_id"]: float(r["length_m"]) for r in rows}
    outcome = eta_band_seconds(lengths, quantiles, list(segment_ids))
    if outcome.band is None:
        logger.info("predict_eta.band_degraded", reason=outcome.reason)
        return None, outcome.reason or "no_quantile_stream"
    band = outcome.band
    return (
        {
            "p10_sec": round(band.p10_sec, 1),
            "p50_sec": round(band.p50_sec, 1),
            "p90_sec": round(band.p90_sec, 1),
            "source": band.source,
        },
        None,
    )


def create_app() -> FastAPI:
    """Build the FastAPI application with all traffic-oracle endpoints."""
    app = FastAPI(
        title="Traffic Oracle",
        version="0.2.0",
        description="Predictive traffic modeling API for VVIP corridor planning",
        lifespan=lifespan,
    )

    # --- adaptive (T2) router: C4 params + status + observe ---------------
    app.include_router(adaptive_router, prefix="/api/v1")

    # --- events (007) router: event-agnostic corridor events --------------
    # POST /api/v1/events + GET /api/v1/events/active; rows land in
    # events.corridor_events which the situation-engine loop polls.
    from traffic_oracle.events import router as events_router

    app.include_router(events_router)

    # --- simulation (C5) router: twin + replay -----------------------------
    # C7 ordering contract: mounted when the simulation stream has landed
    # its router (it carries its own "/api/v1/simulate" prefix); until
    # then the twin endpoints are absent and adaptive shadow-eval fails
    # closed to baseline params.
    # C7 writer-gate (signalling stream): every twin request holds the
    # shared half of the signal-policy gate for its full lifecycle, so a
    # city-plan writer commit can never interleave with an in-flight twin
    # read of corridor.signal_programs (signals/gate.py).
    _twin_gate_deps = []
    try:
        from traffic_oracle.signals.gate import twin_in_flight as _twin_in_flight

        _twin_gate_deps = [Depends(_twin_in_flight)]
    except ImportError:  # pragma: no cover - tree-safety
        pass
    if simulation_router is not None:
        app.include_router(simulation_router, dependencies=_twin_gate_deps)

    # --- signalling-janrakshak stream (011): city signals + agency units --
    # Real routers mount here so the C0 coordination stubs for
    # /signals/state, /units and /adaptive/situation_log self-skip (they
    # only mount when the path does not already exist).  Guarded imports
    # keep the tree bootable regardless of landing order.
    try:
        from traffic_oracle.signals.router import router as signals_router

        app.include_router(signals_router)
    except ImportError as exc:  # pragma: no cover - tree-safety
        logger.warning(
            "traffic_oracle.startup",
            msg="signals.router not importable (signalling stream)",
            error=str(exc),
        )
    try:
        from traffic_oracle.units.router import router as units_router

        app.include_router(units_router)
    except ImportError as exc:  # pragma: no cover - tree-safety
        logger.warning(
            "traffic_oracle.startup",
            msg="units.router not importable (signalling stream)",
            error=str(exc),
        )
    try:
        from traffic_oracle.adaptive.surfacing import (
            router as adaptive_surfacing_router,
        )

        app.include_router(adaptive_surfacing_router, prefix="/api/v1")
    except ImportError as exc:  # pragma: no cover - tree-safety
        logger.warning(
            "traffic_oracle.startup",
            msg="adaptive.surfacing not importable (signalling stream)",
            error=str(exc),
        )

    # --- ops ledger: degradation taxonomy over convoy-brain's
    # decisions_log (read from the SHARED Valkey both services wire to).
    # Guarded import keeps the tree bootable regardless of landing order.
    try:
        from traffic_oracle.ops.router import router as ops_router

        app.include_router(ops_router, prefix="/api/v1")
    except ImportError as exc:  # pragma: no cover - tree-safety
        logger.warning(
            "traffic_oracle.startup",
            msg="ops.router not importable (degradation ledger)",
            error=str(exc),
        )

    # --- C-K / C-S (WP-17): display-geometry spatial reads ----------------
    # /api/v1/spatial/segments(/{id}) and /api/traffic/segments answered
    # from COALESCE(display_geom, geom) with a geometry_basis label once the
    # spatial package lands.  Included HERE — before the inline Spatial
    # section below registers the same paths — so its routes match first;
    # without the package the inline (schematic) handlers keep serving.
    try:
        from traffic_oracle.spatial.router import router as spatial_router

        app.include_router(spatial_router)
    except ImportError as exc:
        # Absent package: expected until WP-17 lands (info).  A package that
        # is present but fails to import is a fault (warning) — either way
        # the inline handlers below still serve.
        absent = exc.name in ("traffic_oracle.spatial", "traffic_oracle.spatial.router")
        (logger.info if absent else logger.warning)(
            "traffic_oracle.startup",
            msg="spatial.router not deployed — inline schematic segment handlers serve",
            error=str(exc),
        )

    # --- C0 coordination: one shared registration site for stream routers
    # (watchlist-anpr's vehicles/enforcement/pursuit real; signals / units /
    # situation_log degrade to 503 'not deployed' stubs until their owning
    # streams land — the tree always boots, streams never collide here).
    from traffic_oracle.coordination import register_coordination_routers

    register_coordination_routers(app)

    # -------------------------------------------------------------------
    # Middleware — request metrics
    # -------------------------------------------------------------------

    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        start = time.monotonic()
        response = await call_next(request)
        duration = time.monotonic() - start
        # Label by the matched route TEMPLATE (set on the shared scope by
        # the router), never the raw path: ids in paths would otherwise
        # mint one Prometheus series per id.
        record_api_request(
            method=request.method,
            endpoint=route_template_label(request.scope),
            status=response.status_code,
            duration=duration,
        )
        return response

    # -------------------------------------------------------------------
    # Health + Metrics
    # -------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict:
        ingest_service: IngestService | None = getattr(app.state, "ingest", None)
        ingest = await ingest_service.health() if ingest_service else disabled_health()
        body: dict = {"status": "ok", "service": "traffic-oracle", "ingest": ingest}
        feed = getattr(app.state, "history_feed", None)
        if feed is not None:
            body["forecast_history"] = feed.health()
        forecaster = getattr(app.state, "forecaster", None)
        oos_log = getattr(forecaster, "out_of_support_log_health", None)
        if callable(oos_log):
            # the rate-limited flow_forecaster.out_of_support warning:
            # emitted vs suppressed (one per rung and segment per window)
            body["forecaster"] = {"out_of_support_log": oos_log()}
        scheduler = getattr(app.state, "calibration_scheduler", None)
        if scheduler is not None:
            body["twin_calibration"] = {
                "calibrated": CALIBRATION.calibrated,
                "reason": CALIBRATION.reason,
                # additive: what the calibration input actually is
                "data_quality": CALIBRATION.input_quality(),
                "scheduler": scheduler.health(),
            }
        loop = getattr(app.state, "adaptive_engine_loop", None)
        if loop is not None:
            snap = loop.health.snapshot()
            body["situation_engine"] = {
                k: snap[k]
                for k in (
                    "running", "last_tick_age_sec", "ticks", "subjects_active",
                    "subjects_observed", "subjects_skipped", "last_error",
                )
            }
        return body

    @app.get("/metrics")
    async def metrics() -> Response:
        pool = getattr(app.state, "pool", None)
        if isinstance(pool, asyncpg.Pool):
            size, idle = pool.get_size(), pool.get_idle_size()
            update_pool_metrics(
                total=size,
                available=idle + max(0, pool.get_max_size() - size),
                idle=idle,
            )
        body, content_type = get_metrics_response()
        return Response(content=body, media_type=content_type)

    # -------------------------------------------------------------------
    # Predict
    # -------------------------------------------------------------------

    @app.post("/api/v1/predict/flow")
    async def predict_flow(req: FlowPredictRequest) -> dict:
        try:
            forecaster: FlowForecaster = app.state.forecaster
            result, meta = await predict_with_meta(
                forecaster, req.segment_ids, req.horizons_min
            )
            # additive provenance: which rung answered, why it degraded, and
            # what the inputs were (synthetic = the simulated feed)
            body: dict = {"predictions": result}
            if meta is not None:
                body["provenance"] = meta.provenance()
            return body
        except Exception as exc:
            raise _internal_error("predict_flow.failed") from exc

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
            body: dict
            if not predictor._is_fitted:
                # Fallback: simple distance / speed estimate (labelled).
                speed_ms = max(req.avg_predicted_speed * 1000 / 3600, 0.1)
                eta_sec = req.route_length_m / speed_ms
                body = {"eta_seconds": round(eta_sec, 1), "model": "fallback"}
            else:
                eta = predictor.predict(features)
                body = {"eta_seconds": round(float(eta[0]), 1), "model": "histgbt"}
            # Optional band (additive-only), INDEPENDENT of the point model:
            # it needs only the quantile stream and segment lengths, so it is
            # computed whenever segment_ids are given.  Present only when
            # honest (full monotone coverage); otherwise the labelled reason.
            if req.segment_ids is not None:
                band, band_reason = await _compute_eta_band(
                    app, req.segment_ids, req.horizon_min
                )
                if band is not None:
                    body["eta_band"] = band
                    body["eta_band_seconds"] = band
                    # Point and band stand on different inputs (the point:
                    # the request's length/speed features; the band: the
                    # live per-segment forecast over segment_ids).  Label
                    # the basis and whether they agree, so a caller never
                    # shows a point outside its band unexplained.
                    body["eta_band_basis"] = "live_segment_forecast"
                    body["eta_point_in_band"] = (
                        band["p10_sec"] <= body["eta_seconds"] <= band["p90_sec"]
                    )
                else:
                    body["eta_band_reason"] = band_reason
            return body
        except Exception as exc:
            raise _internal_error("predict_eta.failed") from exc

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
            routes: list[dict] = []
            for c in candidates:
                route = {
                    "route_id": c.route_id,
                    "segment_ids": c.segment_ids,
                    "total_distance_m": c.total_distance_m,
                    "estimated_time_sec": c.estimated_time_sec,
                    "disruption_score": c.disruption_score,
                    "security_score": c.security_score,
                    "composite_score": c.composite_score,
                }
                # Additive robust overlays: present only when the quantile
                # stream drove the objective; today's keys (and their
                # meaning) are untouched.
                if c.estimated_time_p90_sec is not None:
                    route["estimated_time_p90_sec"] = c.estimated_time_p90_sec
                if c.disruption_p90 is not None:
                    route["disruption_p90"] = c.disruption_p90
                routes.append(route)
            return {"routes": routes}
        except Exception as exc:
            raise _internal_error("optimize_routes.failed") from exc

    @app.post("/api/v1/optimize/diversions")
    async def optimize_diversions(req: DiversionPlanRequest) -> dict:
        try:
            departure = coerce_departure_time(req.departure_time)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="departure_time must be an ISO-8601 timestamp",
            ) from exc
        try:
            planner: DiversionPlanner = app.state.diversion_planner
            result = await planner.plan(
                route_segment_ids=req.route_segment_ids,
                convoy_speed_kmh=req.convoy_speed_kmh,
                advance_closure_sec=req.advance_closure_sec,
                departure_time=departure,
            )
        except DiversionUnavailableError as exc:
            # Labelled 503 — never a silent [] that reads as "no diversions".
            logger.warning("optimize_diversions.unavailable", reason=exc.reason)
            raise HTTPException(
                status_code=503,
                detail={"reason": exc.reason, "message": str(exc)},
            ) from exc
        except Exception as exc:
            raise _internal_error("optimize_diversions.failed") from exc
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
                    # additive: the detour joins the closed segment's two
                    # ends (from → to junction); no_path / dead_end say why
                    # there is none instead of inventing one.
                    "detour_found": p.detour_found,
                    "detour_status": p.detour_status,
                    "detour_distance_m": p.detour_distance_m,
                    "detour_from_junction_id": p.detour_from_junction_id,
                    "detour_to_junction_id": p.detour_to_junction_id,
                    "turnaround_segment_ids": p.turnaround_segment_ids,
                    # additive (WP-16): whether each detour end was FIXED by
                    # the route (False => the detour covers only part of the
                    # closed segment: say 'partial'), the practicality limit
                    # it was held to (max(5 km, 6 x the closure)) and the
                    # labelled reason (impractical / no_path / dead_end, or a
                    # practical detour offered instead of the cheapest).
                    "entry_pinned": p.entry_pinned,
                    "exit_pinned": p.exit_pinned,
                    "detour_limit_m": p.detour_limit_m,
                    "detour_reason": p.detour_reason,
                    # additive (C-R, WP-17): route segments the detour runs
                    # over ([] when none, or on a planner without the field)
                    "uses_route_segment_ids": list(
                        getattr(p, "uses_route_segment_ids", None) or []
                    ),
                }
                for p in result.plans
            ],
            # additive honesty side channel:
            "unplanned_segment_ids": result.unplanned_segment_ids,
            "segment_lengths_source": result.lengths_source,
            "speeds_source": result.speeds_source,
        }

    # -------------------------------------------------------------------
    # Evaluate
    # -------------------------------------------------------------------

    @app.post("/api/v1/evaluate/scenarios")
    async def evaluate_scenarios(req: ScenarioEvalRequest) -> dict:
        try:
            sim: ScenarioSimulator = app.state.scenario_sim
            results = await sim.evaluate(req.scenarios)
        except ScenarioInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise _internal_error("evaluate_scenarios.failed") from exc
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
                    # additive twin provenance / audit fields:
                    **r.audit_fields(),
                }
                for r in results
            ]
        }

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
            raise _internal_error("shortest_path.failed") from exc

    @app.get("/api/v1/graph/k-shortest-paths")
    async def k_shortest_paths(
        source: int = Query(...),
        target: int = Query(...),
        k: int = Query(default=5, ge=1, le=10),
    ) -> dict:
        try:
            graph_db: CorridorGraphDB = app.state.graph_db
            paths = await graph_db.k_shortest_paths(source, target, k)
            return {"paths": paths}
        except Exception as exc:
            raise _internal_error("k_shortest_paths.failed") from exc

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
            raise _internal_error("spatial_segments.failed") from exc

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
            raise _internal_error("segment_detail.failed") from exc

    # -------------------------------------------------------------------
    # Traffic
    # -------------------------------------------------------------------

    @app.post("/api/v1/traffic/live")
    async def live_traffic(req: LiveTrafficRequest) -> dict:
        """Latest row per requested segment from the LIVE hot cache.

        Additive (C-P): every row names its road (``road_name``,
        ``speed_limit_kmh`` from a cached segment map) and its ``origin``.
        The event-emulator's showcase rows live in their own key space and
        are served BESIDE the live plane as ``emulated_observations`` —
        each ``origin: 'emulated'``, ``data_quality: 'synthetic'`` — never
        in ``observations``.
        """
        try:
            cache: TrafficCache = app.state.cache
            names = await _segment_names(app)
            results = []
            for sid in req.segment_ids:
                obs = await cache.get_latest(sid)
                if obs:
                    results.append(_named_row(obs.model_dump(), names, "live"))
            emulated = await _emulated_rows(
                getattr(app.state, "adaptive_valkey", None), req.segment_ids, names
            )
            return {"observations": results, "emulated_observations": emulated}
        except Exception as exc:
            raise _internal_error("live_traffic.failed") from exc

    @app.get("/api/v1/traffic/historical")
    async def traffic_historical(
        segment_id: int = Query(...),
        hours: int | None = Query(
            default=None,
            ge=1,
            le=720,
            description=(
                "look-back window; default 24 h, or 672 h (4 weeks) when a "
                "day_of_week / hour filter asks for a same-slot baseline"
            ),
        ),
        day_of_week: int | None = Query(
            default=None, ge=0, le=6, description="IST weekday, 0 = Monday"
        ),
        hour: int | None = Query(default=None, ge=0, le=23, description="IST hour"),
        include_synthetic: bool = Query(
            default=True,
            description="also return the simulated feed's rollup, labelled",
        ),
    ) -> dict:
        """Hourly aggregates of one segment (contract C-P).

        ``aggregates`` is the REAL-only baseline (traffic.hourly_aggregates
        — the twin's calibration input) and keeps its shape.  The simulated
        feed's rollup rides beside it as ``synthetic_aggregates`` (every row
        ``data_quality: 'synthetic'``, migration 019) — never merged — and
        ``provenance`` says what the answer stands on: real | synthetic |
        none.  ``day_of_week`` / ``hour`` filter to the same IST slot;
        ``basis`` says how (UTC hour buckets straddle IST half-hours:
        ``ist_bucket_offset_min`` 30).
        """
        slot = day_of_week is not None or hour is not None
        lookback = hours if hours is not None else (HISTORICAL_SLOT_LOOKBACK_H if slot else 24)
        try:
            pool: asyncpg.Pool = app.state.pool
            cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback)
            synthetic_rows: list = []
            synthetic_available: bool | None = None  # None: not asked for
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    _SQL_HISTORICAL_REAL, segment_id, cutoff, day_of_week, hour
                )
                if include_synthetic:
                    synthetic_available = bool(
                        await conn.fetchval(_SQL_SYNTHETIC_TABLE_PRESENT)
                    )
                    if synthetic_available:
                        synthetic_rows = await conn.fetch(
                            _SQL_HISTORICAL_SYNTHETIC, segment_id, cutoff, day_of_week, hour
                        )
            aggregates = [_aggregate_out(r) for r in rows]
            synthetic = [
                {**_aggregate_out(r), "data_quality": "synthetic"} for r in synthetic_rows
            ]
            body = {
                "segment_id": segment_id,
                "aggregates": aggregates,
                # additive (C-P)
                "synthetic_aggregates": synthetic,
                "synthetic_available": synthetic_available,
                "provenance": "real" if aggregates else ("synthetic" if synthetic else "none"),
                "basis": {
                    "kind": "same_weekday_hour_ist" if slot else "last_n_hours",
                    "day_of_week": day_of_week,
                    "hour": hour,
                    "lookback_hours": lookback,
                    "ist_bucket_offset_min": 30,
                },
            }
            if not aggregates:
                body["note"] = (
                    "no real hourly aggregates in the window — the simulated "
                    "feed's rollup is labelled synthetic and is never a "
                    "calibration input"
                    if synthetic
                    else "no hourly aggregates in the window"
                )
            return body
        except Exception as exc:
            raise _internal_error("traffic_historical.failed") from exc

    @app.get("/api/v1/traffic/history/{segment_id}")
    async def traffic_segment_history(segment_id: int) -> dict:
        """24h history for a single segment — MCP resource endpoint."""
        try:
            pool: asyncpg.Pool = app.state.pool
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT timestamp_utc, speed_kmh, congestion_idx, source,
                           data_quality
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
                        # additive provenance: real | anomalous | synthetic
                        # (emulated showcase rows are 'synthetic')
                        "data_quality": r["data_quality"],
                    }
                    for r in rows
                ],
            }
        except Exception as exc:
            raise _internal_error("traffic_segment_history.failed") from exc

    # -------------------------------------------------------------------
    # Anomalies
    # -------------------------------------------------------------------

    @app.get("/api/v1/anomalies/recent")
    async def anomalies_recent(
        hours: int = Query(default=1, ge=1, le=168),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict:
        try:
            pool: asyncpg.Pool = app.state.pool
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            async with pool.acquire() as conn:
                # Recency is DETECTION time (flagged_at): a stale_data
                # anomaly carries an old observation timestamp and would
                # never surface under a timestamp_utc filter.
                rows = await conn.fetch(
                    """
                    SELECT anomaly_id, segment_id, timestamp_utc,
                           anomaly_type, severity, details,
                           COALESCE(flagged_at, timestamp_utc) AS flagged_at
                    FROM traffic.anomaly_log
                    WHERE COALESCE(flagged_at, timestamp_utc) >= $1
                    ORDER BY COALESCE(flagged_at, timestamp_utc) DESC, anomaly_id DESC
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
                        "details": _jsonb_object(r["details"]),
                        # additive: when the anomaly was detected
                        "flagged_at": _iso_z(r["flagged_at"]),
                    }
                    for r in rows
                ]
            }
        except Exception as exc:
            raise _internal_error("anomalies_recent.failed") from exc

    # -------------------------------------------------------------------
    # Corridor summary
    # -------------------------------------------------------------------

    @app.get("/api/v1/corridor/summary")
    async def corridor_summary() -> dict:
        """Corridor conditions — ``no_data`` (never green) when dark.

        Cached 10 s in Valkey; a Valkey outage degrades to computing every
        call, never to a 500.
        """
        cache: TrafficCache = app.state.cache
        try:
            cached = await cache.get_corridor_summary("default")
        except Exception as exc:  # noqa: BLE001 — cache is an optimisation
            logger.warning("corridor_summary.cache_read_failed", error=str(exc))
            cached = None
        if cached:
            return cached
        try:
            summary = await compute_corridor_summary(app.state.pool)
        except Exception as exc:
            raise _internal_error("corridor_summary.failed") from exc
        try:
            await cache.set_corridor_summary(
                "default", summary, ttl_sec=SUMMARY_CACHE_TTL_SEC
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("corridor_summary.cache_write_failed", error=str(exc))
        return summary

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
            raise _internal_error("active_movements.failed") from exc

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
                           o.data_quality,
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
                    # additive provenance so emulated ('synthetic') and
                    # Tier-1-flagged ('anomalous') rows are never shown as
                    # plain live readings
                    "dataQuality": r["data_quality"],
                }
                for r in rows
            ]
        except Exception as exc:
            raise _internal_error("traffic_snapshot.failed") from exc

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
            raise _internal_error("traffic_segments.failed") from exc

    @app.post("/api/traffic/predict")
    async def traffic_predict(req: FlowPredictRequest) -> list[dict]:
        """Traffic predictions — consumed by command-deck."""
        try:
            forecaster: FlowForecaster = app.state.forecaster
            result, meta = await predict_with_meta(
                forecaster, req.segment_ids, req.horizons_min
            )
            quality = getattr(meta, "input_quality", {}) or {}
            flat: list[dict] = []
            for sid, horizons in result.items():
                for horizon_min, vals in horizons.items():
                    # Confidence from the forecast's own p10..p90 speed band
                    # (narrow band → high); null for point-only rows — never
                    # a hardcoded constant.
                    bands, _reason = validated_bands(vals)
                    confidence = None
                    if bands is not None:
                        speed = bands["speed_kmh"]
                        spread = (speed["p90"] - speed["p10"]) / max(speed["p50"], 1.0)
                        confidence = round(max(0.0, min(1.0, 1.0 - spread)), 3)
                    flat.append({
                        "segmentId": sid,
                        "horizonMin": horizon_min,
                        "predictedSpeedKmh": round(vals["speed_kmh"], 1),
                        "predictedCongestionIdx": round(vals["congestion_idx"], 3),
                        "confidence": confidence,
                        "rung": getattr(meta, "rung", None),
                        "inputQuality": quality.get(sid),
                    })
            return flat
        except Exception as exc:
            raise _internal_error("traffic_predict.failed") from exc

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
