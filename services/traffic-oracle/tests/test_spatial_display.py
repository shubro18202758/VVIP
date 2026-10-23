"""Spatial router (WP-17, WP-20): how each segment line was drawn (C-K, C-S).

A minimal FastAPI app mounts ``traffic_oracle.spatial.router`` over a fake
asyncpg pool (local fixtures — no conftest), and pins:

    * ``geometry`` is the DISPLAY line where migration 015 holds one, the
      routing line otherwise; ``geometry_basis`` / ``display_source`` say
      which, and ``schematic_geometry`` (the line routing uses) rides along
      only for 'osm_display' rows;
    * a segment whose ROUTING geom is the OpenStreetMap road (migration
      020 geom_source 'osm', no display line) is basis 'osm' — no
      display_source, no schematic_geometry, length_m = display_length_m;
      a store without 020 never says 'osm' and never names the column;
    * ``length_m`` of one segment stays the schematic (routing) length and
      ``display_length_m`` is the drawn line's;
    * the bbox read matches EITHER geometry, by envelope (&&) — on the
      real network a trip end off the 49 roads snaps only through it;
    * the display_geom column probe is cached 300 s: a pre-015 store
      answers every segment 'schematic' and never a 500;
    * the camelCase /api/traffic/segments carries the same additive fields;
    * driver errors never leak (generic 500 detail).

DB tier (read-only SELECTs): with VEER_TEST_PG_DSN naming a store that has
migration 015 and the display data applied, the real SQL runs against it
and the served bases agree with count(display_geom).
"""

from __future__ import annotations

import json
import os
import re

import pytest

pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from traffic_oracle.spatial.router import (  # noqa: E402
    BASIS_OSM,
    BASIS_OSM_DISPLAY,
    BASIS_SCHEMATIC,
    DISPLAY_PROBE_TTL_SEC,
    reset_display_probe,
    router,
)

SCHEMATIC_1001 = {"type": "LineString", "coordinates": [[72.5714, 23.0225], [72.578, 23.035]]}
DISPLAY_1014 = {
    "type": "LineString",
    "coordinates": [[72.6006, 23.0251], [72.6031, 23.0262], [72.6066, 23.0281], [72.61, 23.03]],
}
SCHEMATIC_1014 = {
    "type": "LineString",
    "coordinates": [[72.6006, 23.0251], [72.605, 23.028], [72.61, 23.03]],
}


#: A segment whose routing geom IS the OpenStreetMap road (020 'osm').
OSM_1005 = {
    "type": "LineString",
    "coordinates": [[72.5815039, 23.026423], [72.5866, 23.0275], [72.6001070, 23.0279409]],
}


def _row(sid: int, name: str, geom: dict, schematic: dict | None, source: str | None,
         length_m: float = 1000.0, display_length_m: float | None = None,
         geom_source: str | None = None) -> dict:
    return {
        "geom_source": geom_source,
        "segment_id": sid,
        "osm_way_id": None,
        "road_name": name,
        "road_class": "primary",
        "lanes": 4,
        "speed_limit_kmh": 50,
        "oneway": False,
        "geojson": json.dumps(geom),
        "schematic_geojson": json.dumps(schematic) if schematic else None,
        "has_display": schematic is not None,
        "display_geom_source": source,
        "length_m": length_m,
        "display_length_m": display_length_m if display_length_m is not None else length_m,
    }


ROWS = [
    _row(1001, "Ashram Road (South)", SCHEMATIC_1001, None, None, 1545.3),
    _row(1005, "Relief Road", OSM_1005, None, None, 1993.9, geom_source="osm"),
    _row(1014, "Station Road (Kalupur)", DISPLAY_1014, SCHEMATIC_1014, "osm_way_stitch",
         1111.7, 1302.4, geom_source="schematic"),
]


#: A statement that reads migration 015's column (not the output alias
#: ``display_geom_source`` the pre-015 SQL fills with NULL).
_NAMES_015_COLUMN = re.compile(r"\bdisplay_geom\b")
#: A statement that reads migration 020's column.
_NAMES_020_COLUMN = re.compile(r"\bgeom_source\b")


class FakeConn:
    def __init__(self, store: "FakeStore") -> None:
        self.store = store

    async def fetchval(self, sql: str, *args):
        self.store.queries.append(sql)
        if "attname = 'display_geom'" in sql:
            return self.store.has_display
        if "attname = 'geom_source'" in sql:
            return self.store.has_network
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetch(self, sql: str, *args):
        self.store.queries.append(sql)
        if self.store.fail:
            raise ConnectionError("password authentication failed for user corridor_admin")
        assert "corridor.road_segments" in sql and len(args) == 4
        if _NAMES_020_COLUMN.search(sql):
            assert self.store.has_network, "network SQL on a pre-020 store"
        if _NAMES_015_COLUMN.search(sql):
            assert self.store.has_display, "display SQL on a pre-015 store"
            if _NAMES_020_COLUMN.search(sql):
                return list(self.store.rows)
            # 015 without 020: the column is not selected
            return [{k: v for k, v in r.items() if k != "geom_source"} for r in self.store.rows]
        # pre-015: the schematic SQL names no 015 column at all
        return [
            {**{k: v for k, v in r.items() if k != "geom_source"},
             "geojson": r["schematic_geojson"] or r["geojson"], "schematic_geojson": None,
             "has_display": False, "display_geom_source": None}
            for r in self.store.rows
        ]

    async def fetchrow(self, sql: str, segment_id: int):
        rows = await self.fetch(sql, 0, 0, 0, 0)
        for r in rows:
            if r["segment_id"] == segment_id:
                if not _NAMES_015_COLUMN.search(sql):
                    return {**r, "display_length_m": r["length_m"]}
                return r
        return None


class FakeStore:
    def __init__(self, has_display: bool = True, fail: bool = False,
                 has_network: bool = True) -> None:
        self.has_display = has_display
        self.has_network = has_network
        self.fail = fail
        self.rows = ROWS
        self.queries: list[str] = []

    def acquire(self):
        conn = FakeConn(self)

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def app_for(store) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.pool = store
    return app


@pytest.fixture(autouse=True)
def _fresh_probe():
    reset_display_probe()
    yield
    reset_display_probe()


async def get(app, path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.get(path)


BBOX = "min_lon=72.40&min_lat=22.90&max_lon=72.80&max_lat=23.20"


class TestDisplayGeometry:
    def test_the_c_s_include_line_imports_the_router(self):
        # exactly the guarded include __main__ carries (contract C-S)
        from traffic_oracle.spatial.router import router as spatial_router

        assert spatial_router is router
        paths = {r.path for r in router.routes}
        assert paths == {
            "/api/v1/spatial/segments",
            "/api/v1/spatial/segments/{segment_id}",
            "/api/traffic/segments",
        }

    async def test_bbox_serves_display_where_validated_schematic_elsewhere(self):
        store = FakeStore()
        res = await get(app_for(store), f"/api/v1/spatial/segments?{BBOX}")
        assert res.status_code == 200
        by_id = {s["segment_id"]: s for s in res.json()["segments"]}
        traced, plain = by_id[1014], by_id[1001]
        assert traced["geometry"] == DISPLAY_1014
        assert traced["geometry_basis"] == BASIS_OSM_DISPLAY == "osm_display"
        assert traced["display_source"] == "osm_way_stitch"
        assert traced["schematic_geometry"] == SCHEMATIC_1014
        # the drawn line starts and ends where the routing line does
        assert traced["geometry"]["coordinates"][0] == SCHEMATIC_1014["coordinates"][0]
        assert traced["geometry"]["coordinates"][-1] == SCHEMATIC_1014["coordinates"][-1]
        assert plain["geometry"] == SCHEMATIC_1001
        assert plain["geometry_basis"] == BASIS_SCHEMATIC == "schematic"
        assert plain["display_source"] is None
        assert "schematic_geometry" not in plain
        # the pre-existing fields are untouched
        for key in ("osm_way_id", "road_name", "road_class", "lanes", "speed_limit_kmh", "oneway"):
            assert key in traced and key in plain

    async def test_bbox_matches_either_geometry(self):
        store = FakeStore()
        await get(app_for(store), f"/api/v1/spatial/segments?{BBOX}")
        sql = " ".join(next(q for q in store.queries if "ST_MakeEnvelope" in q).split())
        assert "geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)" in sql
        assert "OR display_geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)" in sql
        assert "COALESCE(display_geom, geom)" in sql

    async def test_one_segment_keeps_the_routing_length(self):
        store = FakeStore()
        res = await get(app_for(store), "/api/v1/spatial/segments/1014")
        doc = res.json()
        assert res.status_code == 200
        assert doc["length_m"] == 1111.7               # schematic (routing) length
        assert doc["display_length_m"] == 1302.4       # the line drawn
        assert doc["geometry_basis"] == "osm_display"
        plain = (await get(app_for(store), "/api/v1/spatial/segments/1001")).json()
        assert plain["length_m"] == plain["display_length_m"] == 1545.3
        assert plain["geometry_basis"] == "schematic" and "schematic_geometry" not in plain
        missing = await get(app_for(store), "/api/v1/spatial/segments/9999")
        assert missing.status_code == 404

    async def test_one_segment_sql_measures_geom_for_length_m(self):
        store = FakeStore()
        await get(app_for(store), "/api/v1/spatial/segments/1014")
        sql = " ".join(next(q for q in store.queries if "segment_id = $1" in q).split())
        assert "ST_Length(geom::geography) AS length_m" in sql
        assert "ST_Length(COALESCE(display_geom, geom)::geography) AS display_length_m" in sql

    async def test_camel_case_feed_carries_the_basis(self):
        res = await get(app_for(FakeStore()), f"/api/traffic/segments?{BBOX}")
        assert res.status_code == 200
        by_id = {s["segmentId"]: s for s in res.json()}
        assert by_id[1014]["geometry"] == DISPLAY_1014["coordinates"]
        assert by_id[1014]["geometryBasis"] == "osm_display"
        assert by_id[1014]["displaySource"] == "osm_way_stitch"
        assert by_id[1014]["schematicGeometry"] == SCHEMATIC_1014["coordinates"]
        assert by_id[1001]["geometryBasis"] == "schematic"
        assert by_id[1001]["displaySource"] is None and "schematicGeometry" not in by_id[1001]


class TestOsmNetwork:
    """WP-20: the routing geom IS the OpenStreetMap road (020 'osm')."""

    async def test_routing_geom_on_the_road_is_basis_osm(self):
        res = await get(app_for(FakeStore()), f"/api/v1/spatial/segments?{BBOX}")
        assert res.status_code == 200
        by_id = {s["segment_id"]: s for s in res.json()["segments"]}
        real = by_id[1005]
        assert real["geometry"] == OSM_1005
        assert real["geometry_basis"] == BASIS_OSM == "osm"
        assert real["display_source"] is None and "schematic_geometry" not in real
        # a 015 display line still wins over geom, whatever geom_source says
        assert by_id[1014]["geometry_basis"] == "osm_display"
        assert by_id[1001]["geometry_basis"] == "schematic"

    async def test_one_osm_segment_draws_its_routing_length(self):
        doc = (await get(app_for(FakeStore()), "/api/v1/spatial/segments/1005")).json()
        assert doc["geometry_basis"] == "osm"
        assert doc["length_m"] == doc["display_length_m"] == 1993.9
        store = FakeStore()
        await get(app_for(store), "/api/v1/spatial/segments/1005")
        sql = " ".join(next(q for q in store.queries if "segment_id = $1" in q).split())
        assert "display_geom_source, geom_source, ST_Length(geom::geography) AS length_m" in sql

    async def test_camel_case_feed_says_osm(self):
        res = await get(app_for(FakeStore()), f"/api/traffic/segments?{BBOX}")
        by_id = {s["segmentId"]: s for s in res.json()}
        assert by_id[1005]["geometryBasis"] == "osm"
        assert by_id[1005]["displaySource"] is None and "schematicGeometry" not in by_id[1005]
        assert by_id[1005]["geometry"] == OSM_1005["coordinates"]

    async def test_a_store_without_020_never_says_osm(self):
        store = FakeStore(has_network=False)
        res = await get(app_for(store), f"/api/v1/spatial/segments?{BBOX}")
        assert res.status_code == 200
        assert {s["geometry_basis"] for s in res.json()["segments"]} == {"schematic", "osm_display"}
        data_sql = [q for q in store.queries if "attname" not in q]
        assert data_sql and not any(_NAMES_020_COLUMN.search(q) for q in data_sql)


class TestPre015Store:
    async def test_every_segment_is_schematic_and_nothing_500s(self):
        store = FakeStore(has_display=False)
        app = app_for(store)
        res = await get(app, f"/api/v1/spatial/segments?{BBOX}")
        assert res.status_code == 200
        segments = res.json()["segments"]
        assert {s["geometry_basis"] for s in segments} == {"schematic"}
        assert all("schematic_geometry" not in s and s["display_source"] is None for s in segments)
        assert next(s for s in segments if s["segment_id"] == 1014)["geometry"] == SCHEMATIC_1014
        one = await get(app, "/api/v1/spatial/segments/1014")
        assert one.status_code == 200
        assert one.json()["display_length_m"] == one.json()["length_m"]
        camel = await get(app, f"/api/traffic/segments?{BBOX}")
        assert camel.status_code == 200
        assert {s["geometryBasis"] for s in camel.json()} == {"schematic"}
        # no statement named a 015 (or 020) column on this store
        data_sql = [q for q in store.queries if "attname" not in q]
        assert data_sql and not any(_NAMES_015_COLUMN.search(q) for q in data_sql)
        assert not any(_NAMES_020_COLUMN.search(q) for q in data_sql)


class TestProbe:
    async def test_probe_is_cached_and_expires(self, monkeypatch):
        import traffic_oracle.spatial.router as mod

        clock = [1000.0]
        monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
        store = FakeStore(has_display=False)
        app = app_for(store)
        await get(app, f"/api/v1/spatial/segments?{BBOX}")
        await get(app, "/api/v1/spatial/segments/1001")
        probes = [q for q in store.queries if "attname = 'display_geom'" in q]
        assert len(probes) == 1                         # cached across requests
        # migration 015 lands under the running oracle: picked up after the TTL
        store.has_display = True
        clock[0] += DISPLAY_PROBE_TTL_SEC - 1
        res = await get(app, f"/api/v1/spatial/segments?{BBOX}")
        assert {s["geometry_basis"] for s in res.json()["segments"]} == {"schematic"}
        clock[0] += 2
        res = await get(app, f"/api/v1/spatial/segments?{BBOX}")
        assert {s["geometry_basis"] for s in res.json()["segments"]} == {
            "schematic", "osm_display", "osm"}
        assert len([q for q in store.queries if "attname = 'display_geom'" in q]) == 2
        assert len([q for q in store.queries if "attname = 'geom_source'" in q]) == 1

    async def test_driver_errors_never_leak(self):
        res = await get(app_for(FakeStore(fail=True)), f"/api/v1/spatial/segments?{BBOX}")
        assert res.status_code == 500
        assert res.json()["detail"] == "spatial_segments.failed: internal error"
        assert "password" not in res.text


# ─── DB tier (read-only) ──────────────────────────────────────────────────────

_DSN = os.environ.get("VEER_TEST_PG_DSN", "")


@pytest.mark.skipif(not _DSN, reason="set VEER_TEST_PG_DSN to a store with migration 015 "
                    "(+ the display data) for the real-SQL checks (read-only)")
class TestRealStore:
    async def test_served_bases_match_the_store(self):
        asyncpg = pytest.importorskip("asyncpg")
        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
        try:
            async with pool.acquire() as conn:
                has_col = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'corridor' AND table_name = 'road_segments' "
                    "AND column_name = 'display_geom')"
                )
                if not has_col:
                    pytest.skip("store has no migration 015 column")
                total, traced = await conn.fetchrow(
                    "SELECT count(*), count(display_geom) FROM corridor.road_segments"
                )
                has_020 = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'corridor' AND table_name = 'road_segments' "
                    "AND column_name = 'geom_source')"
                )
                on_road = await conn.fetchval(
                    "SELECT count(*) FROM corridor.road_segments "
                    "WHERE display_geom IS NULL AND geom_source = 'osm'"
                ) if has_020 else 0
                length_1001 = await conn.fetchval(
                    "SELECT ST_Length(geom::geography) FROM corridor.road_segments "
                    "WHERE segment_id = 1001"
                )
            app = app_for(pool)
            res = await get(app, f"/api/v1/spatial/segments?{BBOX}")
            assert res.status_code == 200
            bases = [s["geometry_basis"] for s in res.json()["segments"]]
            assert len(bases) == total
            assert bases.count("osm_display") == traced
            assert bases.count("osm") == on_road
            for s in res.json()["segments"]:
                if s["geometry_basis"] == "osm_display":
                    sch = s["schematic_geometry"]["coordinates"]
                    disp = s["geometry"]["coordinates"]
                    assert disp[0] == pytest.approx(sch[0], abs=1e-6)
                    assert disp[-1] == pytest.approx(sch[-1], abs=1e-6)
            one = (await get(app, "/api/v1/spatial/segments/1001")).json()
            assert one["length_m"] == round(length_1001, 1)
            assert "display_length_m" in one and "geometry_basis" in one
        finally:
            await pool.close()

    async def test_the_acceptance_ends_snap_through_envelopes(self):
        """The brain planner snaps a trip end to the nearest row of a
        ±0.002° box (±0.004° when empty).  On the OpenStreetMap network the
        acceptance journey's stadium end (scripts/poi_route_matrix.py) lies
        553 m from Motera Stadium Road (1032) — outside both boxes — and is
        found only because the road's ENVELOPE covers it; the airport end
        sits on the terminal spur (1029)."""
        asyncpg = pytest.importorskip("asyncpg")
        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
        try:
            async with pool.acquire() as conn:
                on_osm = await conn.fetchval(
                    "SELECT count(*) FROM corridor.road_segments rs "
                    "WHERE rs.segment_id IN (1029, 1032) "
                    "AND (to_jsonb(rs) ->> 'geom_source') = 'osm'"
                )
            if on_osm != 2:
                pytest.skip("store is not on the OpenStreetMap network (WP-20)")
            app = app_for(pool)

            async def box_ids(lon: float, lat: float, pad: float = 0.002) -> list[int]:
                res = await get(app, f"/api/v1/spatial/segments?min_lon={lon - pad}"
                                     f"&min_lat={lat - pad}&max_lon={lon + pad}"
                                     f"&max_lat={lat + pad}")
                assert res.status_code == 200
                return [s["segment_id"] for s in res.json()["segments"]]

            assert 1032 in await box_ids(72.5957, 23.0916)   # Narendra Modi Stadium
            assert 1029 in await box_ids(72.6221, 23.072)    # SVPI Airport
        finally:
            await pool.close()
