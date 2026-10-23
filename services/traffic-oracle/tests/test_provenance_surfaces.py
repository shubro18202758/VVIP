"""Provenance on every surface — contracts C-Q, C-K, C-P, C-R, C-S (WP-19).

What a band, a row or an anchor STANDS ON is said beside it, never inferred:

    * /signals/state and /signals/plans: city_band_since, _simulated_feed,
      _stale, _stale_since (null without AMA); an apply with no band while
      the band is stale or absent is 409 'city band unknown — pass band
      explicitly' — never a silent GREEN;
    * /adaptive/situation_log entries: inputs_source, simulated_feed (a
      guarded boolean cast), data_quality, inputs_origin;
    * /city/feed: AMA and brain rows carry props.simulated_feed /
      data_quality and end ' · simulated feed'; dispatch rows carry the
      actor's provenance (actor_source, claimed_officer);
    * anomaly entities: the reading's own quality (feed_quality first) and
      its feed; a vehicle whose reads are withheld is badged from an
      aggregate, never a default 'live';
    * the segment dossier: geometry_basis schematic | osm_display | osm (C-K;
      'osm' from migration 020's geom_source) and
      its latest live / emulated hot-cache rows;
    * the C-K display anchors (anomaly, segment, unit) use
      COALESCE(display_geom, geom); routing lengths keep geom;
    * the hot cache keeps the emulator's rows in their own key space, and
      /traffic/live serves them beside the live rows, labelled, with road
      names (C-P);
    * the twin calibration reports its ACTUAL input quality;
    * the forecaster's out_of_support warning is rate-limited per (rung,
      segment) with a suppressed counter, and the DSTGAT gate normalises
      speed by the segment's free flow;
    * C-S: a spatial.router package serves ahead of the inline handlers
      when present; without it the inline handlers serve (no ImportError);
      C-R: diversion rows carry uses_route_segment_ids ([] when absent).
"""

from __future__ import annotations

import json
import sys
import time
import types
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from traffic_oracle import __main__ as main_mod
from traffic_oracle.adaptive import surfacing
from traffic_oracle.city import common
from traffic_oracle.city import dossier as dos
from traffic_oracle.city import entities as ent
from traffic_oracle.city import feed as feed_mod
from traffic_oracle.ingest.cache_writer import (
    CacheWriter,
    emulated_latest_key,
    live_latest_key,
)
from traffic_oracle.signals import router as signals_router_mod
from traffic_oracle.units import router as units_router

NOW = datetime.now(timezone.utc)


# ─── signals: the city band's provenance ─────────────────────────────────────


class FakeAdaptive:
    def __init__(self, band: str = "AMBER", stale: bool = False, simulated: bool | None = True):
        self.doc = {
            "band": band, "since": "2026-10-22T21:45:50.871Z", "subjects": 3,
            "driver": "e1", "simulated_feed": simulated, "stale": stale,
            "stale_reason": "situation engine poll failing (2 in a row)" if stale else None,
            "stale_since": "2026-10-22T21:48:31.271Z" if stale else None,
        }

    def city_band(self) -> dict:
        return dict(self.doc)

    def status(self) -> dict:
        return {"movements": {"e1": {"effective_class": self.doc["band"]}}}


class StateConn:
    async def fetch(self, sql, *args):
        return [{"junction_id": 1, "cycle_sec": 90, "offset_sec": 0, "source": "osm_assumed",
                 "lon": 72.58, "lat": 23.02,
                 "phases": json.dumps([{"green_sec": 40, "movements": ["through"]},
                                       {"green_sec": 40, "movements": ["cross"]}])}]


def _signals_app(adaptive: Any = None, pool: Any = None) -> TestClient:
    app = FastAPI()
    app.include_router(signals_router_mod.router)
    app.state.pool = pool if pool is not None else StateConn()
    if adaptive is not None:
        app.state.adaptive = adaptive
    return TestClient(app)


BAND_KEYS = ("city_band_since", "city_band_simulated_feed", "city_band_stale",
             "city_band_stale_since")


class TestCityBandProvenance:
    @pytest.mark.parametrize("path", ["/api/v1/signals/state", "/api/v1/signals/plans"])
    def test_the_band_says_since_simulated_and_stale(self, path):
        body = _signals_app(FakeAdaptive()).get(path).json()
        assert body["city_band"] == "AMBER" and body["band_subjects"] == 3
        assert {k: body[k] for k in BAND_KEYS} == {
            "city_band_since": "2026-10-22T21:45:50.871Z",
            "city_band_simulated_feed": True,
            "city_band_stale": False,
            "city_band_stale_since": None,
        }

    @pytest.mark.parametrize("path", ["/api/v1/signals/state", "/api/v1/signals/plans"])
    def test_without_ama_every_provenance_key_is_null(self, path):
        body = _signals_app().get(path).json()
        assert body["city_band"] == "GREEN" and body["band_subjects"] == 0
        assert all(body[k] is None for k in BAND_KEYS)

    def test_a_service_without_city_band_falls_back_to_status(self):
        class Legacy:
            def status(self):
                return {"movements": {"a": {"effective_class": "RED"}}}

        doc = signals_router_mod._city_band_doc(
            types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(
                adaptive=Legacy())))
        )
        assert (doc["band"], doc["subjects"], doc["known"]) == ("RED", 1, True)
        assert all(doc[k] is None for k in BAND_KEYS)

    @pytest.mark.parametrize("adaptive", [FakeAdaptive(stale=True), None])
    def test_an_apply_without_a_band_while_unknown_is_409(self, adaptive, monkeypatch):
        called = []

        async def never(*a, **kw):
            called.append(kw)
            return {"applied": True}

        monkeypatch.setattr(signals_router_mod.signal_writer, "apply_city_plan", never)
        r = _signals_app(adaptive).post("/api/v1/signals/plans/apply", json={})
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == "city band unknown — pass band explicitly"
        assert r.json()["adaptive_wired"] is (adaptive is not None)
        assert called == []  # nothing planned, nothing written

    def test_an_explicit_band_still_applies_while_stale(self, monkeypatch):
        seen = []

        async def fake_apply(pool, **kw):
            seen.append(kw)
            return {"applied": True, "band": kw["band"], "actor": kw["actor"]}

        monkeypatch.setattr(signals_router_mod.signal_writer, "apply_city_plan", fake_apply)
        r = _signals_app(FakeAdaptive(stale=True)).post(
            "/api/v1/signals/plans/apply", json={"band": "RED", "officer": "Duty Officer A"}
        )
        assert r.status_code == 200, r.text
        assert seen[0]["band"] == "RED"

    def test_a_known_band_drives_an_apply_without_one(self, monkeypatch):
        seen = []

        async def fake_apply(pool, **kw):
            seen.append(kw)
            return {"applied": True, "band": kw["band"], "actor": kw["actor"]}

        monkeypatch.setattr(signals_router_mod.signal_writer, "apply_city_plan", fake_apply)
        r = _signals_app(FakeAdaptive(band="RED")).post("/api/v1/signals/plans/apply", json={})
        assert r.status_code == 200, r.text
        assert seen[0]["band"] == "RED"


# ─── situation_log entries ───────────────────────────────────────────────────


class TestSituationLogProvenance:
    def test_the_sql_guards_the_boolean_cast(self):
        flat = " ".join(surfacing._SQL_SITUATION_LOG.split())
        assert "inputs->>'source' AS inputs_source" in flat
        assert ("CASE WHEN jsonb_typeof(inputs->'simulated_feed') = 'boolean' "
                "THEN (inputs->>'simulated_feed')::boolean END AS simulated_feed") in flat
        assert "inputs->>'data_quality' AS data_quality" in flat
        assert "inputs->>'inputs_origin' AS inputs_origin" in flat

    def test_entries_carry_what_the_band_stood_on(self):
        class Pool:
            async def fetch(self, sql, *args):
                if sql == surfacing._SQL_SITUATION_LOG:
                    return [
                        {"log_id": 2, "movement_id": "e1", "situation_class": "AMBER",
                         "observed_class": "AMBER", "situation_code": "Z:FREE_FLOW",
                         "severity_score": 5, "transitioned": True, "protection_class": "Z",
                         "observed_at": NOW, "inputs_source": "live_cache",
                         "simulated_feed": True, "data_quality": "synthetic",
                         "inputs_origin": "live"},
                        {"log_id": 1, "movement_id": "e1", "situation_class": "GREEN",
                         "observed_class": "GREEN", "situation_code": "Z:FREE_FLOW",
                         "severity_score": 2, "transitioned": False, "protection_class": "Z",
                         "observed_at": NOW - timedelta(minutes=1)},  # an older row
                    ]
                return []  # resolve_subjects

        app = FastAPI()
        app.include_router(surfacing.router, prefix="/api/v1")
        app.state.pool = Pool()
        newer, older = TestClient(app).get("/api/v1/adaptive/situation_log").json()["entries"]
        assert {k: newer[k] for k in ("inputs_source", "simulated_feed", "data_quality",
                                      "inputs_origin")} == {
            "inputs_source": "live_cache", "simulated_feed": True,
            "data_quality": "synthetic", "inputs_origin": "live",
        }
        assert all(older[k] is None for k in ("inputs_source", "simulated_feed",
                                              "data_quality", "inputs_origin"))


# ─── the city feed ───────────────────────────────────────────────────────────


class TestFeedProvenance:
    def _situation(self, **kw) -> dict:
        return {"log_id": 9, "movement_id": "m1", "situation_class": "AMBER",
                "prev_class": "GREEN", "situation_code": "Z:CONGESTED", "severity_score": 6,
                "protection_class": "Z", "observed_at": NOW, **kw}

    def test_an_ama_band_on_the_simulated_feed_says_so(self):
        item = feed_mod.situation_item(
            self._situation(simulated_feed=True, data_quality="synthetic",
                            inputs_source="live_cache", inputs_origin="live"),
            common.ResolvedSubjects(),
        )
        assert item["text"].endswith(" · simulated feed")
        assert item["props"]["simulated_feed"] is True
        assert item["props"]["data_quality"] == "synthetic"
        assert item["props"]["inputs_origin"] == "live"

    def test_an_older_row_says_nothing_rather_than_live(self):
        item = feed_mod.situation_item(self._situation(), common.ResolvedSubjects())
        assert not item["text"].endswith("simulated feed")
        assert item["props"]["simulated_feed"] is None

    @pytest.mark.parametrize(
        "row, expected",
        [
            ({"simulated": True}, True),
            ({"data_quality": ["synthetic"]}, True),
            ({"data_quality": ["real", "synthetic"]}, True),
            ({"data_quality": "synthetic"}, True),
            ({"data_quality": ["real"], "simulated": False}, False),
            ({"simulated": False}, False),
            ({"data_quality": []}, None),
            ({"phase": "decider_checkpoint"}, None),
        ],
    )
    def test_brain_row_simulated(self, row, expected):
        assert feed_mod.brain_row_simulated(row) is expected

    def test_brain_rows_carry_the_feed_they_stood_on(self):
        rows = [
            {"timestamp": NOW.isoformat(), "phase": "live_escort", "action": "crossing 1013",
             "data_quality": ["synthetic"], "simulated": True},
            {"timestamp": NOW.isoformat(), "phase": "decider_checkpoint", "tier": "decider",
             "trigger": "interval"},
        ]
        simulated, checkpoint = feed_mod.brain_items(
            "m1", rows, common.ResolvedSubjects(), common.EPOCH
        )
        assert simulated["text"] == "Live escort: crossing 1013 · simulated feed"
        assert simulated["props"]["simulated_feed"] is True
        assert simulated["props"]["data_quality"] == ["synthetic"]
        assert simulated["source"] == "live"  # C-C badge = the event's provenance
        assert checkpoint["props"]["simulated_feed"] is None
        assert not checkpoint["text"].endswith("simulated feed")

    def _dispatch(self, detail: Any) -> dict:
        return {"seq": 5, "dispatch_id": uuid.uuid4(), "from_status": "requested",
                "to_status": "approved", "actor": "Duty Officer A", "note": "go",
                "at": NOW, "detail": detail, "event_id": uuid.uuid4(),
                "agency": "jan_rakshak", "unit_id": "JR-1", "waypoint_junction": 2,
                "event_title": "convoy", "event_metadata": {}}

    @pytest.mark.parametrize(
        "detail, source, claimed",
        [
            (json.dumps({"actor": "Duty Officer A", "actor_source": "x-veer-officer",
                         "claimed_officer": "dcp"}), "x-veer-officer", "dcp"),
            ({"actor": "Duty Officer A", "actor_source": "x-veer-officer"},
             "x-veer-officer", None),
            ("null", None, None),  # the JSON null of a body-attributed row
            (None, None, None),  # a pre-014 store
        ],
    )
    def test_dispatch_rows_carry_the_actor_provenance(self, detail, source, claimed):
        item = feed_mod.dispatch_item(self._dispatch(detail))
        assert item["props"]["actor_source"] == source
        assert item["props"]["claimed_officer"] == claimed

    def test_the_dispatch_read_selects_the_014_detail_safely(self):
        assert "to_jsonb(de) -> 'detail' AS detail" in feed_mod._SQL_FEED_DISPATCH


# ─── entities + dossier ──────────────────────────────────────────────────────


def _anomaly(details: dict) -> dict:
    return {"anomaly_id": 837, "segment_id": 1013, "road_name": "Tilak Road",
            "lon": 72.59, "lat": 23.02, "timestamp_utc": NOW, "anomaly_type": "speed_zscore",
            "severity": "medium", "details": json.dumps(details), "flagged_at": NOW,
            "n_15m": 2}


class TestEntityProvenance:
    @pytest.mark.parametrize(
        "details, quality",
        [
            ({"source": "FleetGps", "data_quality": "Anomalous", "feed_quality": "Synthetic"},
             "synthetic"),
            ({"source": "GovernmentTraffic", "data_quality": "Anomalous"}, "anomalous"),
            ({"source": "FleetGps"}, None),
        ],
    )
    def test_an_anomaly_carries_its_reading_quality_and_feed(self, details, quality):
        e = ent.anomaly_entity(_anomaly(details), NOW)
        assert e["props"]["data_quality"] == quality
        assert e["props"]["feed_source"] == details["source"]
        assert e["source"] == "live"  # the C-C badge keeps its meaning

    def test_a_vehicle_without_lawful_reads_is_badged_from_the_aggregate(self):
        row = {"vehicle_id": uuid.uuid4(), "plate": "GJ27EM4321", "vehicle_class": "car",
               "state": None}
        assert ent.vehicle_entity(row, None, NOW, earmarked=False,
                                  reads_source="emulated")["source"] == "emulated"
        assert ent.vehicle_entity(row, None, NOW, earmarked=False)["source"] == "live"

    async def test_the_dossier_aggregates_withheld_reads_without_disclosing_one(self):
        vid = uuid.uuid4()

        class Conn:
            calls: list[str] = []

            async def fetch(self, sql, *args):
                self.calls.append(sql)
                if sql == dos._SQL_DOSSIER_VEHICLES:
                    return [{"vehicle_id": vid, "plate": "GJ27EM4321", "vehicle_class": "car",
                             "first_seen": NOW, "last_seen": NOW, "state": None,
                             "reason": None, "lawful_basis": None, "retention_until": None,
                             "earmarked_at": None, "earmarked_by": None}]
                if sql == dos._SQL_VEHICLE_READS_PROVENANCE:
                    assert args == ([vid],)
                    return [{"vehicle_id": vid, "all_emulated": True}]
                raise AssertionError(sql[:60])

        conn = Conn()
        (row, entity, trail) = (await dos._Dossier(conn, now=NOW, valkey=None,
                                                   graph_db=None).vehicles([str(vid)]))[0]
        assert entity["source"] == "emulated" and entity["state"] == "not_earmarked"
        assert trail == []
        assert dos._SQL_RECENT_SIGHTINGS not in conn.calls  # no read was fetched
        agg = dos._SQL_VEHICLE_READS_PROVENANCE
        assert "bool_and(s.source LIKE 'emulated%')" in agg and "GROUP BY s.vehicle_id" in agg
        for leak in ("s.t", "s.camera_id", "s.position", "s.sighting_id"):
            assert leak not in agg

    def _segment_row(self, **kw) -> dict:
        return {"segment_id": 1001, "road_name": "Ashram Road (South)", "road_class": "trunk",
                "lanes": 6, "speed_limit_kmh": 60.0, "oneway": False, "lon": 72.57,
                "lat": 23.03, "length_m": 1540.8, **kw}

    def test_a_segment_says_how_it_is_drawn(self):
        assert dos.segment_entity(self._segment_row(), NOW)["props"]["geometry_basis"] == \
            "schematic"
        osm = dos.segment_entity(self._segment_row(has_display_geom=True), NOW)
        assert osm["props"]["geometry_basis"] == "osm_display"
        assert osm["props"]["length_m"] == 1540.8  # the routing length, unchanged

    def test_a_real_road_is_never_called_schematic(self):
        # WP-20 (migration 020): the routing geom IS the OpenStreetMap road —
        # 'osm', the spatial router's word for it; a 015 display line still
        # outranks it (the router's own order), and anything else on a store
        # without 020 (geom_source NULL) stays 'schematic'.
        def basis(**kw):
            return dos.segment_entity(self._segment_row(**kw), NOW)["props"]["geometry_basis"]

        assert basis(has_display_geom=False, geom_source="osm") == "osm"
        assert basis(has_display_geom=True, geom_source="osm") == "osm_display"
        assert basis(has_display_geom=False, geom_source=None) == "schematic"
        assert basis(has_display_geom=False, geom_source="seed") == "schematic"
        # the 015 variant reads it without naming the column (safe before 020)
        sql = dos._SQL_DOSSIER_SEGMENTS_DISPLAY
        assert "(to_jsonb(rs) ->> 'geom_source') AS geom_source" in sql
        assert "rs.geom_source" not in sql

    def test_latest_observation_parsing(self):
        raw = json.dumps({"timestamp_ms": int((time.time() - 4) * 1000), "segment_id": 1001,
                          "speed_kmh": 52.9, "congestion_idx": 0.17, "source": "fleet_gps",
                          "data_quality": "synthetic", "lon": 72.5, "lat": 23.0})
        obs = dos.latest_observation(raw, datetime.now(timezone.utc))
        assert obs["speed_kmh"] == 52.9 and obs["data_quality"] == "synthetic"
        assert 3.0 <= obs["age_sec"] <= 30.0 and obs["t"]
        assert dos.latest_observation(None, NOW) is None
        assert dos.latest_observation("not json", NOW) is None

    async def test_the_segment_dossier_reads_both_planes_apart(self):
        class Valkey:
            async def mget(self, keys):
                assert keys == [live_latest_key(1001), emulated_latest_key(1001)]
                now_ms = int(time.time() * 1000)
                return [
                    json.dumps({"timestamp_ms": now_ms, "speed_kmh": 50.0,
                                "congestion_idx": 0.2, "data_quality": "synthetic",
                                "source": "fleet_gps"}),
                    json.dumps({"timestamp_ms": now_ms, "speed_kmh": 6.0,
                                "congestion_idx": 0.95, "data_quality": "synthetic",
                                "source": "fleet_gps", "origin": "emulated"}),
                ]

        d = dos._Dossier(None, now=datetime.now(timezone.utc), valkey=Valkey(), graph_db=None)
        live, emulated = (await d.latest_observations([1001]))[1001]
        assert (live["speed_kmh"], live["origin"]) == (50.0, "live")
        assert (emulated["speed_kmh"], emulated["origin"], emulated["data_quality"]) == (
            6.0, "emulated", "synthetic"
        )


class TestDisplayAnchors:
    """C-K: anything drawn sits on COALESCE(display_geom, geom); anything
    routed, costed or attributed keeps geom."""

    DISPLAY = "ST_PointOnSurface(COALESCE(rs.display_geom, rs.geom))"

    @pytest.mark.parametrize(
        "pre, display",
        [
            (ent._SQL_RECENT_ANOMALIES, ent._SQL_RECENT_ANOMALIES_DISPLAY),
            (dos._SQL_DOSSIER_SEGMENTS, dos._SQL_DOSSIER_SEGMENTS_DISPLAY),
            (dos._SQL_DOSSIER_ANOMALY, dos._SQL_DOSSIER_ANOMALY_DISPLAY),
            (dos._SQL_SEGMENT_ANOMALIES, dos._SQL_SEGMENT_ANOMALIES_DISPLAY),
            (units_router._SQL_SEGMENT_POINTS, units_router._SQL_SEGMENT_POINTS_DISPLAY),
        ],
    )
    def test_the_display_variant_anchors_on_the_drawn_road(self, pre, display):
        assert "display_geom" not in pre  # still runs on a pre-015 store
        assert self.DISPLAY in display
        assert "ST_PointOnSurface(rs.geom)" not in display

    def test_lengths_and_attribution_keep_the_routing_geometry(self):
        assert "ST_Length(rs.geom::geography) AS length_m" in dos._SQL_DOSSIER_SEGMENTS_DISPLAY
        assert "(rs.display_geom IS NOT NULL) AS has_display_geom" in \
            dos._SQL_DOSSIER_SEGMENTS_DISPLAY
        from traffic_oracle.enforcement import engine

        assert "display_geom" not in engine._SQL_COVERING_CLOSURES_CURRENT

    def test_the_probe_names_015s_column(self):
        assert (common.SEGMENTS_TABLE, common.DISPLAY_GEOM_COLUMN) == (
            "corridor.road_segments", "display_geom"
        )

    async def test_a_failed_probe_is_false_and_not_cached(self):
        common.reset_relation_cache()

        class Down:
            calls = 0

            async def fetchval(self, sql, *args):
                Down.calls += 1
                raise ConnectionError("down")

        assert await common.display_geom_supported(Down()) is False
        assert await common.display_geom_supported(Down()) is False
        assert Down.calls == 2  # retried, never cached as absent


# ─── the hot cache: two planes, never last-writer-wins ───────────────────────


class PipeValkey:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def pipeline(self, transaction: bool = True):
        outer = self

        class _P:
            def __init__(self):
                self.ops = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False

            def set(self, key, value, ex=None):
                self.ops.append((key, value, ex))

            async def execute(self):
                for k, v, ex in self.ops:
                    outer.store[k] = v
                    outer.ttls[k] = ex

        return _P()

    async def get(self, key):
        return self.store.get(key)

    async def mget(self, keys):
        return [self.store.get(k) for k in keys]


def _cache(valkey: PipeValkey):
    from traffic_oracle.data.cache import TrafficCache

    cache = TrafficCache.__new__(TrafficCache)
    cache._client = valkey
    return cache


def _canonical(rows: list[tuple]) -> pl.DataFrame:
    """rows: (ts_ms, segment_id, speed, congestion, quality, origin)."""
    return pl.DataFrame(
        {
            "timestamp_ms": [r[0] for r in rows], "lon": [72.58] * len(rows),
            "lat": [23.02] * len(rows), "segment_id": [r[1] for r in rows],
            "speed_kmh": [r[2] for r in rows], "congestion_idx": [r[3] for r in rows],
            "source": ["fleet_gps"] * len(rows), "data_quality": [r[4] for r in rows],
            "confidence": [0.9] * len(rows), "origin": [r[5] for r in rows],
        },
        schema={"timestamp_ms": pl.Int64, "lon": pl.Float64, "lat": pl.Float64,
                "segment_id": pl.Int64, "speed_kmh": pl.Float64,
                "congestion_idx": pl.Float64, "source": pl.String,
                "data_quality": pl.String, "confidence": pl.Float64, "origin": pl.String},
    )


class TestCachePlanes:
    async def test_each_plane_keeps_its_own_latest_row(self):
        valkey = PipeValkey()
        writer = CacheWriter(_cache(valkey))
        ts = int(time.time() * 1000)
        n = await writer.update_from_batch(_canonical([
            (ts, 1001, 41.0, 0.35, "synthetic", "live"),
            (ts + 500, 1001, 6.0, 0.95, "synthetic", "emulated"),  # later: used to win
            (ts, 1002, 30.0, 0.5, "synthetic", "emulated"),
        ]))
        assert n == 3
        assert set(valkey.store) == {"traffic:latest:1001", "traffic:emulated:latest:1001",
                                     "traffic:emulated:latest:1002"}
        assert json.loads(valkey.store["traffic:latest:1001"])["speed_kmh"] == 41.0
        emu = json.loads(valkey.store["traffic:emulated:latest:1001"])
        assert (emu["speed_kmh"], emu["origin"], emu["data_quality"]) == (6.0, "emulated",
                                                                          "synthetic")
        assert valkey.ttls["traffic:emulated:latest:1001"] == 300
        assert writer.written == {"live": 1, "emulated": 2}

    def test_the_emulated_key_space_is_outside_the_live_scan(self):
        assert emulated_latest_key(1001) == "traffic:emulated:latest:1001"
        assert not emulated_latest_key(1001).startswith("traffic:latest:")
        assert live_latest_key(1001) == "traffic:latest:1001"

    def test_a_forced_quality_marks_the_emulated_origin(self):
        from traffic_oracle.data.models import DataQuality
        from traffic_oracle.ingest.normalize import normalize_batch

        df = pl.DataFrame({
            "timestamp_ms": [int(time.time() * 1000)], "lon": [72.58], "lat": [23.02],
            "segment_id": [1001], "speed_kmh": [40.0], "congestion_index": [0.3],
            "source": [2], "data_quality": [0], "confidence": [0.9],
        })
        emulated = normalize_batch(df, force_quality=DataQuality.SYNTHETIC).frame
        assert emulated["origin"].to_list() == ["emulated"]
        assert emulated["data_quality"].to_list() == ["synthetic"]  # never 'real'
        assert normalize_batch(df).frame["origin"].to_list() == ["live"]
        explicit = normalize_batch(df, origin="emulated").frame
        assert explicit["origin"].to_list() == ["emulated"]
        with pytest.raises(ValueError):
            normalize_batch(df, origin="shadow")


# ─── /traffic/live: road names + the emulated plane beside it ────────────────


class NamesConn:
    async def fetch(self, sql, *args):
        assert sql == main_mod._SQL_SEGMENT_NAMES
        return [{"segment_id": 1001, "road_name": "Ashram Road (South)", "speed_limit_kmh": 60}]


class NamesPool:
    def acquire(self):
        class _A:
            async def __aenter__(self):
                return NamesConn()

            async def __aexit__(self, *e):
                return False

        return _A()


class TestLiveTraffic:
    def _app(self, valkey: PipeValkey) -> TestClient:
        app = main_mod.create_app()
        app.state.pool = NamesPool()
        app.state.cache = _cache(valkey)
        app.state.adaptive_valkey = valkey
        return TestClient(app)

    def test_rows_carry_road_names_and_the_emulated_rows_ride_beside(self):
        valkey = PipeValkey()
        now_ms = int(time.time() * 1000)
        doc = {"timestamp_ms": now_ms, "lon": 72.5, "lat": 23.0, "segment_id": 1001,
               "speed_kmh": 50.0, "congestion_idx": 0.2, "source": "fleet_gps",
               "data_quality": "synthetic", "confidence": 1.0}
        valkey.store["traffic:latest:1001"] = json.dumps(doc)
        valkey.store["traffic:emulated:latest:1001"] = json.dumps(
            {**doc, "speed_kmh": 6.0, "congestion_idx": 0.95, "origin": "emulated"}
        )
        body = self._app(valkey).post("/api/v1/traffic/live",
                                      json={"segment_ids": [1001, 4242]}).json()
        (live,) = body["observations"]
        assert (live["road_name"], live["speed_limit_kmh"], live["origin"]) == (
            "Ashram Road (South)", 60.0, "live"
        )
        assert live["speed_kmh"] == 50.0  # the emulated row never replaces it
        (emu,) = body["emulated_observations"]
        assert (emu["speed_kmh"], emu["origin"], emu["data_quality"], emu["emulated"]) == (
            6.0, "emulated", "synthetic", True
        )
        assert emu["road_name"] == "Ashram Road (South)"

    def test_unknown_segments_have_null_names_and_no_valkey_no_overlay(self):
        valkey = PipeValkey()
        valkey.store["traffic:latest:4242"] = json.dumps(
            {"timestamp_ms": 1, "lon": 72.5, "lat": 23.0, "segment_id": 4242, "speed_kmh": 5.0,
             "congestion_idx": 0.5, "source": "fleet_gps", "data_quality": "synthetic"}
        )
        client = self._app(valkey)
        client.app.state.adaptive_valkey = None
        body = client.post("/api/v1/traffic/live", json={"segment_ids": [4242]}).json()
        assert body["observations"][0]["road_name"] is None
        assert body["emulated_observations"] == []


# ─── the twin calibration's input quality ────────────────────────────────────


class TestTwinCalibrationQuality:
    def _state(self, **kw):
        from traffic_oracle.simulation.calibration import CalibrationState

        state = CalibrationState()
        for k, v in kw.items():
            setattr(state, k, v)
        return state

    def test_never_a_constant_real_beside_a_simulated_only_refusal(self):
        from traffic_oracle.simulation.calibration import REASON_SIMULATED_ONLY

        state = self._state(field_hours=0, feed_quality="synthetic", reason=REASON_SIMULATED_ONLY)
        doc = state.to_dict()
        assert doc["data_quality"] == "synthetic" and doc["feed_quality"] == "synthetic"
        assert doc["reason"] == REASON_SIMULATED_ONLY

    @pytest.mark.parametrize(
        "kw, quality",
        [
            ({"field_hours": 30, "feed_quality": "mixed"}, "real"),
            ({"calibrated": True, "field_hours": 0}, "real"),
            ({"field_hours": 0, "feed_quality": "none"}, "none"),
            ({"field_hours": 0, "feed_quality": None}, "none"),
            ({}, "unknown"),  # not yet checked
        ],
    )
    def test_input_quality(self, kw, quality):
        assert self._state(**kw).input_quality() == quality

    async def test_the_coverage_check_reports_the_feed_quality(self):
        from traffic_oracle.simulation.calibration import field_coverage

        class Pool:
            def __init__(self, real: bool, synth: bool) -> None:
                self.real, self.synth = real, synth

            async def fetchrow(self, sql, *args):
                if "count(DISTINCT hour_utc)" in sql:
                    return {"hours": 0, "first_hour": None, "last_hour": None}
                return {"has_real": self.real, "has_synthetic": self.synth}

        for (real, synth), quality in {(False, True): "synthetic", (True, True): "mixed",
                                       (True, False): "real", (False, False): "none"}.items():
            assert (await field_coverage(Pool(real, synth)))["feed_quality"] == quality


# ─── the forecaster's out-of-support warning ─────────────────────────────────


class _Obs:
    def __init__(self, speed: float, congestion: float = 0.2) -> None:
        self.speed_kmh = speed
        self.congestion_idx = congestion
        self.data_quality = "synthetic"


class TestForecasterGate:
    def _forecaster(self):
        from traffic_oracle.predict.flow_forecaster import FlowForecaster
        from traffic_oracle.predict.support_bounds import SupportBounds

        bounds = SupportBounds.from_json(json.dumps(
            {"schema": 1, "features": {"speed_norm": [0.05, 1.25]}}
        ))
        f = FlowForecaster(support_bounds=bounds)
        clock = {"t": 1000.0}
        f._monotonic = lambda: clock["t"]
        return f, clock

    def test_the_warning_fires_once_per_rung_and_segment_per_window(self, monkeypatch):
        from traffic_oracle.predict import flow_forecaster as ff

        f, clock = self._forecaster()
        logged = []
        monkeypatch.setattr(ff.logger, "warning", lambda event, **kw: logged.append(kw))
        observed = {1: _Obs(200.0), 2: _Obs(30.0)}
        for _ in range(3):  # three predicts inside the window
            assert f._gate("dstgat", [1, 2], observed, []) == [1]  # abstains EVERY time
        assert len(logged) == 1 and logged[0]["segments"] == [1]
        assert f.out_of_support_log_health() == {
            "window_sec": ff.OOS_LOG_WINDOW_SEC, "emitted": 1, "suppressed": 2, "tracked": 1,
        }
        clock["t"] += ff.OOS_LOG_WINDOW_SEC  # the window passes: one more line
        f._gate("dstgat", [1, 2], observed, [])
        assert len(logged) == 2
        # a different rung (or segment) is its own key
        f._gate("foundation", [1], observed, [])
        assert len(logged) == 3

    def test_the_dstgat_speed_is_normalised_by_the_segment_free_flow(self):
        from traffic_oracle.predict.flow_forecaster import DEFAULT_FREE_FLOW_KMH, speed_norm

        f, _ = self._forecaster()
        observed = {1: _Obs(100.0)}  # a free-flowing 100 km/h motorway
        assert f._gate("dstgat", [1], observed, []) == [1]  # 100/60 = 1.67: out
        f.set_free_flow({1: 100.0})
        assert f._gate("dstgat", [1], observed, []) == []  # 100/100 = 1.0: in support
        assert speed_norm(30.0, None) == 30.0 / DEFAULT_FREE_FLOW_KMH
        assert speed_norm(30.0, 0.0) == 0.5 and speed_norm(30.0, 40.0) == 0.75


class TestTrainingSpeedScale:
    """The DSTGAT training loader trains on the scale the gate and the
    inference rung use: speed over the segment's free flow (/60 unknown)."""

    def test_the_matrix_form_is_speed_norm_per_column(self):
        import numpy as np

        from traffic_oracle.predict.flow_forecaster import speed_norm, speed_norm_matrix

        speeds = np.array([[100.0, 30.0, 45.0], [50.0, 60.0, 0.0]], dtype=np.float32)
        free_flow = {1: 100.0, 2: None, 3: 0.0}  # a motorway, unknown, non-positive
        rel = speed_norm_matrix(speeds, [1, 2, 3], free_flow)
        assert rel.dtype == np.float32
        for i in range(2):
            for j, sid in enumerate((1, 2, 3)):
                assert rel[i, j] == pytest.approx(speed_norm(float(speeds[i, j]), free_flow[sid]))
        assert rel[0, 0] == pytest.approx(1.0)  # free flow reads 1.0, not 1.67
        with pytest.raises(ValueError, match="does not match"):
            speed_norm_matrix(speeds, [1, 2], free_flow)

    def test_the_training_loader_builds_its_speed_features_on_that_scale(self):
        # data_loader needs duckdb + torch (absent in the gate venv): its
        # wiring is pinned on the source instead.
        from pathlib import Path

        import traffic_oracle.predict as predict

        source = (Path(predict.__file__).parent / "data_loader.py").read_text(encoding="utf-8")
        assert "speed_norm_matrix(speed_matrix, segment_ids, free_flow)" in source
        assert "FROM pg.corridor.road_segments" in source  # free flow = speed limit
        assert "/ 60.0" not in source  # the fixed 60 km/h scale is gone
        assert "input_features[j, t_off, 0] = rel_matrix[ti, j]" in source


# ─── C-S guarded include, C-R serializer ─────────────────────────────────────


class TestSpatialInclude:
    def test_a_spatial_router_serves_ahead_of_the_inline_handlers(self, monkeypatch):
        stub = APIRouter()

        @stub.get("/api/v1/spatial/segments")
        async def stub_segments() -> dict:
            return {"served_by": "spatial.router"}

        pkg = types.ModuleType("traffic_oracle.spatial")
        mod = types.ModuleType("traffic_oracle.spatial.router")
        mod.router = stub
        monkeypatch.setitem(sys.modules, "traffic_oracle.spatial", pkg)
        monkeypatch.setitem(sys.modules, "traffic_oracle.spatial.router", mod)
        app = main_mod.create_app()
        r = TestClient(app).get(
            "/api/v1/spatial/segments?min_lon=72&min_lat=22&max_lon=73&max_lat=24"
        )
        assert r.json() == {"served_by": "spatial.router"}

    def test_without_it_the_inline_handlers_serve(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "traffic_oracle.spatial", raising=False)
        monkeypatch.delitem(sys.modules, "traffic_oracle.spatial.router", raising=False)
        app = main_mod.create_app()  # no ImportError escapes
        paths = [getattr(r, "path", None) for r in app.router.routes]
        assert "/api/v1/spatial/segments" in paths
        assert "/api/v1/spatial/segments/{segment_id}" in paths


class TestDiversionSerializer:
    def _client(self, plans):
        from traffic_oracle.optimize.diversion_planner import DiversionPlanResult

        class Planner:
            async def plan(self, **kw):
                return DiversionPlanResult(plans=plans, unplanned_segment_ids=[],
                                       lengths_source="db", speeds_source="forecast")

        app = main_mod.create_app()
        app.state.diversion_planner = Planner()
        return TestClient(app)

    def _plan(self, **extra):
        base = dict(
            segment_id=1005, diversion_type="full_closure", activate_at=NOW,
            deactivate_at=NOW + timedelta(minutes=5), alt_segment_ids=[1013],
            estimated_queue_m=10.0, dissipation_time_sec=60.0, detour_found=True,
            detour_status="found", detour_distance_m=900.0, detour_from_junction_id=5,
            detour_to_junction_id=15, turnaround_segment_ids=[], entry_pinned=True,
            exit_pinned=True, detour_limit_m=5000.0, detour_reason=None,
        )
        return types.SimpleNamespace(**base, **extra)

    def test_rows_carry_uses_route_segment_ids(self):
        body = self._client([self._plan(uses_route_segment_ids=[1012])]).post(
            "/api/v1/optimize/diversions", json={"route_segment_ids": [1005, 1012]}
        ).json()
        assert body["diversions"][0]["uses_route_segment_ids"] == [1012]

    def test_a_planner_without_the_field_answers_an_empty_list(self):
        body = self._client([self._plan()]).post(
            "/api/v1/optimize/diversions", json={"route_segment_ids": [1005]}
        ).json()
        assert body["diversions"][0]["uses_route_segment_ids"] == []


def test_feed_situation_sql_casts_the_boolean_safely():
    flat = " ".join(feed_mod._SQL_FEED_SITUATIONS.split())
    assert ("CASE WHEN jsonb_typeof(l.inputs->'simulated_feed') = 'boolean' THEN "
            "(l.inputs->>'simulated_feed')::boolean END AS simulated_feed") in flat

