"""Prometheus metrics instrumentation for traffic-oracle.

Exports metrics for:
- ONNX inference latency and throughput
- pgRouting query latency by type (dijkstra, KSP, isochrone, multi-hop)
- pgvector similarity search latency
- Database connection pool statistics
- Anomaly detection rates
- Stream processor channel utilization
- Memory profiling gauges
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

# ─── ONNX Inference Metrics ──────────────────────────────────────

ONNX_INFERENCE_DURATION = Histogram(
    "vvip_onnx_inference_duration_seconds",
    "ONNX Runtime inference latency",
    ["model_name", "provider"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1],
)

ONNX_INFERENCE_TOTAL = Counter(
    "vvip_onnx_inference_total",
    "Total ONNX inference calls",
    ["model_name", "provider"],
)

# ─── pgRouting Query Metrics ─────────────────────────────────────

PGROUTING_QUERY_DURATION = Histogram(
    "vvip_pgrouting_query_duration_seconds",
    "pgRouting query latency by type",
    ["query_type"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5],
)

PGROUTING_QUERY_TOTAL = Counter(
    "vvip_pgrouting_query_total",
    "Total pgRouting queries",
    ["query_type"],
)

# ─── pgvector Metrics ────────────────────────────────────────────

PGVECTOR_SEARCH_DURATION = Histogram(
    "vvip_pgvector_search_duration_seconds",
    "pgvector HNSW similarity search latency",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
)

PGVECTOR_SEARCH_TOTAL = Counter(
    "vvip_pgvector_search_total",
    "Total pgvector similarity searches",
)

# ─── Database Pool Metrics ────────────────────────────────────────

DB_POOL_TOTAL = Gauge(
    "vvip_db_pool_total_connections",
    "Total connections in asyncpg pool",
)

DB_POOL_AVAILABLE = Gauge(
    "vvip_db_pool_available_connections",
    "Available connections in asyncpg pool",
)

DB_POOL_IDLE = Gauge(
    "vvip_db_pool_idle_connections",
    "Idle connections in asyncpg pool",
)

# ─── Anomaly Detection Metrics ───────────────────────────────────

ANOMALY_DETECTIONS = Counter(
    "vvip_anomaly_detections_total",
    "Anomaly detections by type",
    ["anomaly_type", "severity"],
)

OBSERVATIONS_PROCESSED = Counter(
    "vvip_observations_processed_total",
    "Total traffic observations processed",
)

# ─── Stream Processor Channel Metrics ─────────────────────────────

INGRESS_CHANNEL_UTILIZATION = Gauge(
    "vvip_ingress_channel_utilization",
    "feed_rx channel utilization (0.0-1.0)",
)

INGRESS_BATCH_CHANNEL_UTILIZATION = Gauge(
    "vvip_ingress_batch_channel_utilization",
    "batch_rx channel utilization (0.0-1.0)",
)

# ─── Memory Metrics ───────────────────────────────────────────────

MEMORY_RSS_MB = Gauge(
    "vvip_memory_rss_mb",
    "Current RSS of traffic-oracle process in MB",
)

MEMORY_PEAK_ALLOC_MB = Gauge(
    "vvip_memory_peak_alloc_mb",
    "Peak memory allocation of last profiled function in MB",
)

# ─── API Metrics ──────────────────────────────────────────────────

API_REQUEST_DURATION = Histogram(
    "vvip_api_request_duration_seconds",
    "API request duration",
    ["service", "method", "endpoint", "status"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10],
)

API_REQUESTS_TOTAL = Counter(
    "vvip_api_requests_total",
    "Total API requests",
    ["service", "method", "endpoint", "status"],
)

# ─── Build Info ───────────────────────────────────────────────────

BUILD_INFO = Info(
    "vvip_traffic_oracle",
    "Build information for traffic-oracle service",
)
BUILD_INFO.info({
    "version": "1.0.0",
    "service": "traffic-oracle",
})


# ─── Instrumentation Helpers ──────────────────────────────────────

@asynccontextmanager
async def track_pgrouting(query_type: str) -> AsyncIterator[None]:
    """Track pgRouting query duration."""
    PGROUTING_QUERY_TOTAL.labels(query_type=query_type).inc()
    start = time.monotonic()
    try:
        yield
    finally:
        PGROUTING_QUERY_DURATION.labels(query_type=query_type).observe(
            time.monotonic() - start
        )


@asynccontextmanager
async def track_pgvector_search() -> AsyncIterator[None]:
    """Track pgvector similarity search duration."""
    PGVECTOR_SEARCH_TOTAL.inc()
    start = time.monotonic()
    try:
        yield
    finally:
        PGVECTOR_SEARCH_DURATION.observe(time.monotonic() - start)


@contextmanager
def track_onnx_inference(model_name: str, provider: str) -> Iterator[None]:
    """Track ONNX inference duration."""
    ONNX_INFERENCE_TOTAL.labels(model_name=model_name, provider=provider).inc()
    start = time.monotonic()
    try:
        yield
    finally:
        ONNX_INFERENCE_DURATION.labels(
            model_name=model_name, provider=provider
        ).observe(time.monotonic() - start)


def record_api_request(
    method: str, endpoint: str, status: int, duration: float
) -> None:
    """Record traffic-oracle API request metrics."""
    labels = {
        "service": "traffic-oracle",
        "method": method,
        "endpoint": endpoint,
        "status": str(status),
    }
    API_REQUEST_DURATION.labels(**labels).observe(duration)
    API_REQUESTS_TOTAL.labels(**labels).inc()


def update_pool_metrics(total: int, available: int, idle: int) -> None:
    """Update asyncpg connection pool gauges."""
    DB_POOL_TOTAL.set(total)
    DB_POOL_AVAILABLE.set(available)
    DB_POOL_IDLE.set(idle)


def get_metrics_response() -> tuple[bytes, str]:
    """Generate Prometheus metrics response."""
    return generate_latest(), CONTENT_TYPE_LATEST
