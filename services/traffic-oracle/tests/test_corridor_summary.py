"""Corridor summary honesty + API hardening on the oracle's HTTP surface.

Coverage:
    - compute_corridor_summary: ``no_data`` (never green) with data_age_sec
      / newest_observation_t when the table is empty or stale; latest-row-
      per-segment aggregation, segment (not row) counts, anomalous rows
      excluded and reported; green/amber/red classification unchanged.
    - GET /api/v1/corridor/summary: 10 s cache TTL, cache outages degrade
      to computing (never a 500), DB failures answer a generic 500 detail
      (no driver/DB text leaks).
    - GET /api/v1/anomalies/recent decodes asyncpg's jsonb TEXT (the old
      dict(str) 500) and bounds hours 1..168 / limit 1..1000; traffic
      /historical hours 1..720; k-shortest-paths k 1..10 → 422 outside.
    - POST /api/v1/predict/eta accepts convoy-brain's day_of_week /
      road_class_score spellings.
    - Prometheus endpoint labels are route TEMPLATES (bounded cardinality).
    - /health always carries an ``ingest`` block.
    - DB tier (read-only SELECTs, VVIP_DATABASE_URL): the summary SQL runs
      against the real schema.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from traffic_oracle.__main__ import (  # noqa: E402
    SUMMARY_CACHE_TTL_SEC,
    SUMMARY_WINDOW_SEC,
    compute_corridor_summary,
    create_app,
)


# ─── Fakes ────────────────────────────────────────────────────────────────────


def stats_row(**over) -> dict:
    row = {
        "total_segments": 0,
        "emulated_segments": 0,
        "avg_speed": None,
        "avg_congestion": None,
        "congested_count": 0,
        "critical_count": 0,
        "newest": None,
        "real_rows": 0,
        "synthetic_rows": 0,
        "anomalous_rows": 0,
    }
    row.update(over)
    return row


class SummaryConn:
    def __init__(self, stats: dict, newest_any=None, fail: bool = False) -> None:
        self.stats = stats
        self.newest_any = newest_any
        self.fail = fail
        self.queries: list[str] = []

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if self.fail:
            raise RuntimeError('relation "traffic.observations" secret-internal-detail')
        return self.stats

    async def fetchval(self, query, *args):
        self.queries.append(query)
        return self.newest_any

    async def fetch(self, query, *args):
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("secret-internal-detail")
        return self.rows if hasattr(self, "rows") else []


class OneConnPool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


class FakeSummaryCache:
    def __init__(self, fail_read: bool = False) -> None:
        self.stored: dict = {}
        self.ttls: dict = {}
        self.fail_read = fail_read

    async def get_corridor_summary(self, corridor_id):
        if self.fail_read:
            raise ConnectionError("valkey down")
        return self.stored.get(corridor_id)

    async def set_corridor_summary(self, corridor_id, summary, ttl_sec=10):
        self.stored[corridor_id] = summary
        self.ttls[corridor_id] = ttl_sec


def app_with(pool=None, cache=None):
    app = create_app()
    app.state.pool = pool
    app.state.cache = cache or FakeSummaryCache()
    return app


async def get(app, path: str, **kw):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.get(path, **kw)


async def post(app, path: str, body: dict):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.post(path, json=body)


# ─── compute_corridor_summary ─────────────────────────────────────────────────


class TestComputeCorridorSummary:
    async def test_empty_table_is_no_data_never_green(self):
        summary = await compute_corridor_summary(OneConnPool(SummaryConn(stats_row())))
        assert summary["status"] == "no_data"
        assert summary["total_segments"] == 0
        assert summary["data_age_sec"] is None
        assert summary["newest_observation_t"] is None
        # frozen fields keep their types (numbers, not nulls)
        assert summary["avg_speed_kmh"] == 0.0 and summary["avg_congestion_idx"] == 0.0

    async def test_stale_data_reports_how_dark_the_corridor_is(self):
        newest = datetime.now(timezone.utc) - timedelta(days=9)
        conn = SummaryConn(stats_row(anomalous_rows=4), newest_any=newest)
        summary = await compute_corridor_summary(OneConnPool(conn))
        assert summary["status"] == "no_data"
        assert summary["data_age_sec"] == pytest.approx(9 * 86400, abs=5)
        assert summary["newest_observation_t"].endswith("Z")
        assert summary["quality_mix"]["anomalous_excluded"] == 4
        assert len(conn.queries) == 2  # window query + per-segment newest probe

    async def test_fresh_data_classifies_on_latest_rows_per_segment(self):
        newest = datetime.now(timezone.utc) - timedelta(seconds=8)
        conn = SummaryConn(
            stats_row(
                total_segments=5, avg_speed=18.46, avg_congestion=0.6834,
                congested_count=4, critical_count=1, newest=newest,
                real_rows=16, synthetic_rows=3, anomalous_rows=133,
            )
        )
        summary = await compute_corridor_summary(OneConnPool(conn))
        assert summary["status"] == "amber"
        assert summary["total_segments"] == 5
        assert summary["avg_speed_kmh"] == 18.5
        assert summary["avg_congestion_idx"] == 0.683
        assert (summary["congested_segments"], summary["critical_segments"]) == (4, 1)
        assert summary["data_age_sec"] == pytest.approx(8, abs=2)
        assert summary["quality_mix"] == {"real": 16, "synthetic": 3, "anomalous_excluded": 133}
        assert summary["provenance"] == "live" and summary["emulated_segments"] == 0
        assert summary["window_sec"] == SUMMARY_WINDOW_SEC
        assert len(conn.queries) == 1  # no stale probe needed
        sql = conn.queries[0]
        assert "DISTINCT ON (segment_id)" in sql
        assert "IS DISTINCT FROM 'anomalous'" in sql

    @pytest.mark.parametrize(
        ("total", "emulated", "provenance"),
        [(0, 0, "none"), (4, 0, "live"), (4, 4, "emulated"), (4, 1, "mixed")],
    )
    async def test_provenance_says_what_the_status_stands_on(self, total, emulated, provenance):
        conn = SummaryConn(
            stats_row(
                total_segments=total, emulated_segments=emulated, avg_speed=12.0,
                avg_congestion=0.9 if total else None,
                newest=datetime.now(timezone.utc) if total else None,
            )
        )
        summary = await compute_corridor_summary(OneConnPool(conn))
        assert summary["provenance"] == provenance
        assert summary["emulated_segments"] == emulated
        # an emulator-driven RED is still RED — and now labelled as such
        assert summary["status"] == ("red" if total else "no_data")
        assert "WHERE data_quality = 'synthetic'" in conn.queries[0]

    @pytest.mark.parametrize(
        ("avg_congestion", "status"), [(0.2, "green"), (0.41, "amber"), (0.71, "red")]
    )
    async def test_status_thresholds_unchanged(self, avg_congestion, status):
        conn = SummaryConn(
            stats_row(
                total_segments=3, avg_speed=30.0, avg_congestion=avg_congestion,
                newest=datetime.now(timezone.utc),
            )
        )
        assert (await compute_corridor_summary(OneConnPool(conn)))["status"] == status


# ─── /api/v1/corridor/summary ─────────────────────────────────────────────────


class TestCorridorSummaryEndpoint:
    async def test_no_data_is_served_and_cached_for_ten_seconds(self):
        cache = FakeSummaryCache()
        app = app_with(OneConnPool(SummaryConn(stats_row())), cache)
        res = await get(app, "/api/v1/corridor/summary")
        assert res.status_code == 200
        assert res.json()["status"] == "no_data"
        assert cache.ttls["default"] == SUMMARY_CACHE_TTL_SEC == 10

    async def test_cache_outage_degrades_to_computing(self):
        app = app_with(OneConnPool(SummaryConn(stats_row())), FakeSummaryCache(fail_read=True))
        res = await get(app, "/api/v1/corridor/summary")
        assert res.status_code == 200 and res.json()["status"] == "no_data"

    async def test_db_failure_is_a_generic_500_without_leaking_internals(self):
        app = app_with(OneConnPool(SummaryConn(stats_row(), fail=True)))
        res = await get(app, "/api/v1/corridor/summary")
        assert res.status_code == 500
        assert res.json()["detail"] == "corridor_summary.failed: internal error"
        assert "secret" not in res.text


# ─── /api/v1/anomalies/recent and query bounds ────────────────────────────────


class TestAnomaliesAndBounds:
    async def test_jsonb_details_text_is_decoded(self):
        conn = SummaryConn(stats_row())
        conn.rows = [
            {
                "anomaly_id": 7,
                "segment_id": 1013,
                "timestamp_utc": datetime(2026, 10, 22, 13, 36, tzinfo=timezone.utc),
                "anomaly_type": "speed_jump",
                "severity": "medium",
                "details": json.dumps({"anomaly_flags": ["speed_jump"], "speed_kmh": 9.0}),
                "flagged_at": datetime(2026, 10, 22, 13, 37, 2, tzinfo=timezone.utc),
            },
            {
                "anomaly_id": 8,
                "segment_id": 1013,
                "timestamp_utc": datetime(2026, 10, 22, 13, 36, tzinfo=timezone.utc),
                "anomaly_type": "stale_data",
                "severity": "low",
                "details": "{}",  # the column default — truthy text
                "flagged_at": datetime(2026, 10, 22, 13, 37, 2, tzinfo=timezone.utc),
            },
        ]
        res = await get(app_with(OneConnPool(conn)), "/api/v1/anomalies/recent?hours=2&limit=10")
        assert res.status_code == 200
        first, second = res.json()["anomalies"]
        assert first["details"] == {"anomaly_flags": ["speed_jump"], "speed_kmh": 9.0}
        assert second["details"] == {}
        assert first["flagged_at"] == "2026-10-22T13:37:02.000Z"
        # recency is detection time: an old stale_data observation flagged
        # just now must surface
        sql = conn.queries[-1]
        assert "WHERE COALESCE(flagged_at, timestamp_utc) >= $1" in sql

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/anomalies/recent?limit=-1",
            "/api/v1/anomalies/recent?limit=0",
            "/api/v1/anomalies/recent?limit=1001",
            "/api/v1/anomalies/recent?hours=0",
            "/api/v1/anomalies/recent?hours=169",
            "/api/v1/traffic/historical?segment_id=1001&hours=99999999",
            "/api/v1/traffic/historical?segment_id=1001&hours=0",
            "/api/v1/graph/k-shortest-paths?source=1001&target=1005&k=0",
            "/api/v1/graph/k-shortest-paths?source=1001&target=1005&k=11",
        ],
    )
    async def test_out_of_range_params_are_422_not_500(self, path):
        res = await get(app_with(OneConnPool(SummaryConn(stats_row()))), path)
        assert res.status_code == 422

    async def test_snapshot_and_history_label_provenance(self):
        conn = SummaryConn(stats_row())
        conn.rows = [
            {
                "segment_id": 1001,
                "speed_kmh": 22.0,
                "congestion_idx": 0.6,
                "data_quality": "synthetic",  # an emulated showcase row
                "last_updated": 1792676165140.0,
                "timestamp_utc": datetime(2026, 10, 22, 13, 36, tzinfo=timezone.utc),
                "source": "fleet_gps",
            }
        ]
        app = app_with(OneConnPool(conn))
        snap = await get(app, "/api/traffic/snapshot?min_lon=72&min_lat=22&max_lon=73&max_lat=24")
        assert snap.json() == [
            {
                "segmentId": 1001,
                "speedKmh": 22.0,
                "congestionIdx": 0.6,
                "lastUpdated": 1792676165140,
                "dataQuality": "synthetic",
            }
        ]
        hist = await get(app, "/api/v1/traffic/history/1001")
        assert hist.json()["observations"][0]["data_quality"] == "synthetic"

    async def test_handler_errors_never_echo_exception_text(self):
        conn = SummaryConn(stats_row(), fail=True)
        res = await get(app_with(OneConnPool(conn)), "/api/v1/anomalies/recent")
        assert res.status_code == 500
        assert "secret-internal-detail" not in res.text
        assert res.json()["detail"] == "anomalies_recent.failed: internal error"


# ─── predict/eta aliases ──────────────────────────────────────────────────────


class TestEtaAliases:
    BODY = {
        "route_length_m": 6000.0,
        "num_segments": 4,
        "avg_predicted_speed": 36.0,
        "avg_predicted_congestion": 0.4,
        "hour": 9,
        "num_signals": 3,
    }

    @pytest.mark.parametrize(
        "spelling",
        [
            {"day_of_week": 3, "road_class_score": 70.0},  # convoy-brain
            {"dow": 3, "weighted_road_class_score": 70.0},  # oracle native
        ],
    )
    async def test_both_spellings_validate(self, spelling):
        from traffic_oracle.predict.eta_model import ETAPredictor

        app = app_with()
        app.state.eta_predictor = ETAPredictor()  # unfitted → distance/speed
        res = await post(app, "/api/v1/predict/eta", {**self.BODY, **spelling})
        assert res.status_code == 200, res.text
        assert res.json() == {"eta_seconds": 600.0, "model": "fallback"}

    async def test_missing_day_is_still_rejected(self):
        res = await post(app_with(), "/api/v1/predict/eta", self.BODY)
        assert res.status_code == 422


# ─── Metrics labels + health ──────────────────────────────────────────────────


class TestMetricsAndHealth:
    async def test_endpoint_label_is_the_route_template(self):
        from traffic_oracle.metrics import API_REQUESTS_TOTAL, UNMATCHED_ROUTE_LABEL

        app = app_with(OneConnPool(SummaryConn(stats_row(), fail=True)))
        for sid in (424242, 535353):
            await get(app, f"/api/v1/spatial/segments/{sid}")
        await get(app, "/definitely/not/a/route/9f1c")
        labels = {
            sample.labels["endpoint"]
            for metric in API_REQUESTS_TOTAL.collect()
            for sample in metric.samples
        }
        assert "/api/v1/spatial/segments/{segment_id}" in labels
        assert UNMATCHED_ROUTE_LABEL in labels
        assert not any("424242" in label or "9f1c" in label for label in labels)

    async def test_health_always_carries_an_ingest_block(self):
        app = app_with()
        app.state.ingest = None  # lifespan not run / ingest disabled
        res = await get(app, "/health")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ok"
        assert body["ingest"]["enabled"] is False
        assert body["ingest"]["connected"] is False


# ─── DB tier (read-only) ──────────────────────────────────────────────────────


_DSN = os.environ.get("VVIP_DATABASE_URL")


@pytest.mark.skipif(not _DSN, reason="VVIP_DATABASE_URL not set (read-only schema tier)")
class TestSummaryOnRealSchema:
    async def test_summary_sql_runs_against_the_real_schema(self):
        asyncpg = pytest.importorskip("asyncpg")
        try:
            pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=1, timeout=5)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"database not reachable: {type(exc).__name__}")
        try:
            summary = await compute_corridor_summary(pool)
        finally:
            await pool.close()
        assert summary["status"] in {"green", "amber", "red", "no_data"}
        if summary["total_segments"] == 0:
            assert summary["status"] == "no_data"
