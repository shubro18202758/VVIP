"""CorridorStore against a real (scratch) corridor-store — opt-in.

Runs only when EMULATOR_TEST_PG_DSN names a SCRATCH clone of the migrated
store (it refuses the live :56434 port).  Every row it writes is tagged
and deleted at the end; sequences still advance.  Pins the SQL the fakes
cannot: the ledger round trip (incl. the 'interrupted' CHECK fallback),
the event creation + re-animation ledger rows, and the reference reads
(camera attribution, junction legs, programs, open dispatches, on-road
anchors, every library route's line in its direction of travel).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.environ.get("EMULATOR_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN or ":56434/" in DSN,
    reason="set EMULATOR_TEST_PG_DSN to a scratch corridor-store clone (never :56434)",
)


@pytest.fixture()
async def store():
    from psycopg_pool import AsyncConnectionPool

    from event_emulator.store import CorridorStore

    from event_emulator.store import runner_identity

    pool = AsyncConnectionPool(DSN, min_size=1, max_size=2, open=False)
    await pool.open(wait=True)
    s = CorridorStore(pool, runner=runner_identity(port=18090))
    s.test_runs = []      # type: ignore[attr-defined]
    s.test_events = []    # type: ignore[attr-defined]
    try:
        yield s
    finally:
        async with pool.connection() as conn:
            for eid in s.test_events:  # type: ignore[attr-defined]
                await conn.execute("DELETE FROM events.corridor_events WHERE event_id = %s", (uuid.UUID(eid),))  # noqa: E501
                await conn.execute("DELETE FROM convoy.planned_routes WHERE movement_id = %s", (uuid.UUID(eid),))  # noqa: E501
                await conn.execute("DELETE FROM convoy.movements WHERE movement_id = %s", (uuid.UUID(eid),))  # noqa: E501
            for rid in s.test_runs:  # type: ignore[attr-defined]
                await conn.execute("DELETE FROM emulator.emulation_runs WHERE run_id = %s", (rid,))  # noqa: E501
        await pool.close()


async def _run(store) -> int:
    rid = await store.insert_run(
        use_case_id="metro_work_zone", spec={"mode": "use_case", "test": "test_store_pg"},
        spec_hash="test", seed=1, t0_anchor=datetime.now(timezone.utc), clock_scale=10.0,
        tape_json='{"frames": [], "pending": true}', is_replay=False, replay_of_run_id=None)
    store.test_runs.append(rid)
    return rid


class TestLedger:
    async def test_round_trip_and_interrupted_fallback(self, store):
        rid = await _run(store)
        running = next(r for r in await store.running_runs() if r["run_id"] == rid)
        # the reaper's restart rule reads the owner's address + boot id
        assert running["runner"] == store.runner
        assert running["runner"]["boot"] and "host" in running["runner"]
        await store.update_run_tape(rid, '{"frames": [], "movement_id": "x"}')
        await store.heartbeat_run(rid, {"progress": {"frame": 3, "total": 9}})
        row = await store.fetch_run(rid)
        assert row["spec"]["progress"] == {"frame": 3, "total": 9}
        assert row["spec"]["runner"]["pid"] > 0 and row["spec"]["heartbeat_at"]
        column = await store.finish_run(rid, "interrupted", {"reason": "test", "frame_log": [1, 2]})
        assert column in ("interrupted", "stopped")
        row = await store.fetch_run(rid)
        assert row["status"] == column and row["ended_at"] is not None
        assert row["spec"]["final_status"] == "interrupted"
        assert await store.finished_run_ids([rid]) == {rid}
        listed = next(r for r in await store.fetch_runs(limit=20) if r["run_id"] == rid)
        assert "frame_log" not in listed["spec"]

    async def test_completed_is_written_verbatim(self, store):
        rid = await _run(store)
        column = await store.finish_run(rid, "completed", {"delivery": {"delivered": 1}})
        assert column == "completed"


class TestEvents:
    async def test_creation_and_reanimation_rows(self, store):
        eid = str(uuid.uuid4())
        store.test_events.append(eid)
        start = datetime.now(timezone.utc)
        prior = await store.upsert_corridor_event(
            event_id=eid, movement_id=None, event_kind="work_zone", protection_class="none",
            crowd_class="LOW", title="test slot", planned_start=start,
            planned_end=start + timedelta(minutes=5),
            metadata_doc={"source": "event-emulator", "mode": "living_city", "run_id": 0},
            description="EMULATED test", note="test created")
        assert prior is None
        await store.replace_event_route(eid, [1005])
        await store.replace_event_phase(eid, "breakdown", start, start + timedelta(minutes=5),
                                        {"closure_type": "partial_closure"})
        live = [e for e in await store.live_emulator_events() if e["event_id"] == uuid.UUID(eid)
                or str(e["event_id"]) == eid]
        assert live and live[0]["mode"] == "living_city" and live[0]["run_id"] == "0"
        again = await store.upsert_corridor_event(
            event_id=eid, movement_id=None, event_kind="work_zone", protection_class="none",
            crowd_class="LOW", title="test slot", planned_start=start,
            planned_end=start + timedelta(minutes=5),
            metadata_doc={"source": "event-emulator", "mode": "living_city", "run_id": 1},
            note="test re-animated")
        assert again == "planning"
        async with store._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT to_regclass('events.event_status_log') IS NOT NULL")
                has_log = (await cur.fetchone())[0]
                if has_log:
                    await cur.execute("SELECT from_status, to_status, actor, note FROM events.event_status_log WHERE event_id = %s ORDER BY seq", (uuid.UUID(eid),))  # noqa: E501
                    rows = await cur.fetchall()
                    assert rows == [
                        (None, "planning", "event-emulator (system)", "test created"),
                        ("planning", "planning", "event-emulator (system)", "test re-animated"),
                    ]


class TestMovements:
    async def test_a_reanimation_moves_origin_and_destination(self, store):
        """1a: a replay re-animates the emulator's own movement row with the
        ends of the route drawn on today's roads, never the stored ones."""
        mid = str(uuid.uuid4())
        store.test_events.append(mid)
        start = datetime.now(timezone.utc)
        await store.upsert_movement(mid, "X", start, (72.50, 23.00), (72.51, 23.01))
        await store.set_movement_status(mid, "completed")
        await store.upsert_movement(mid, "X", start + timedelta(minutes=1),
                                    (72.58, 23.02), (72.59, 23.03))
        async with store._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT status, ST_X(origin_geom), ST_Y(origin_geom), ST_X(destination_geom), ST_Y(destination_geom), planned_start FROM convoy.movements WHERE movement_id = %s", (uuid.UUID(mid),))  # noqa: E501
                status, ox, oy, dx, dy, planned = await cur.fetchone()
        assert status == "planning"
        assert (ox, oy, dx, dy) == pytest.approx((72.58, 23.02, 72.59, 23.03))
        assert planned == start + timedelta(minutes=1)


class TestReference:
    async def test_every_library_route_is_replayable_and_a_pre_conversion_one_is_not(
            self, store):
        """1a: route_problems on the store's road graph — every use case's
        route is a legal path; live run 20's pre-conversion sweep belt
        [1012, 1015, 1005, 1013] is not (refused as a replay)."""
        from event_emulator.library import load_library

        async with store._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'corridor' AND table_name = 'road_segments' AND column_name = 'geom_source')")  # noqa: E501
                osm = (await cur.fetchone())[0]
                if osm:
                    await cur.execute("SELECT count(*) FILTER (WHERE geom_source = 'osm') FROM corridor.road_segments")  # noqa: E501
                    osm = (await cur.fetchone())[0]
        for uc in load_library().values():
            assert await store.route_problems(list(uc.config["route_segment_ids"])) == [], uc.id
        assert await store.route_problems([1005, 999999]) == ["unknown segments [999999]"]
        if osm:   # the network on OpenStreetMap (WP-20)
            assert await store.route_problems([1012, 1015, 1005, 1013]) == [
                "1012 -> 1015 is not a legal move", "1005 -> 1013 is not a legal move"]

    async def test_camera_attribution_matches_the_registry(self, store):
        from event_emulator.runner import load_camera_context

        sites = await load_camera_context(store)
        if not sites:
            pytest.skip("no corridor.cameras registry (pre-012 store)")
        by_id = {s["camera_id"]: s for s in sites}
        for s in sites:
            assert s["segment_id"] is not None and s["speed_limit_kmh"]
        stop = [s for s in sites if s["junction_id"] is not None and s["signal"]]
        assert stop and all(s["signal"]["movement"] in ("through", "cross") for s in stop)
        mid = [s for s in sites if s["camera_id"].startswith("EM-CAM")]
        assert mid and all(s["signal"] is None for s in mid)
        if "EM-J03" in by_id:
            assert by_id["EM-J03"]["signal"]["junction_id"] == 3

    async def test_paths_programs_and_dispatches(self, store):
        paths = await store.segment_paths([1005, 1012])
        assert set(paths) == {1005, 1012} and all(len(p) >= 2 for p in paths.values())
        programs = await store.signal_programs()
        assert programs and all(p["cycle_sec"] > 0 for p in programs.values())
        legs = await store.junction_legs([3])
        assert {leg["segment_id"] for leg in legs.get(3, [])} >= {1005, 1012, 1015}
        assert await store.dispatched_units(["NO-SUCH-UNIT"]) == set()
        assert await store.probe() is True

    async def test_paths_follow_the_display_geometry_where_the_store_has_it(self, store):
        """C-K: patrol paths are COALESCE(display_geom, geom) on a store with
        migration 015 and the routing line on a pre-015 store; endpoints
        always equal the routing ones (chain_polyline relies on it).  On the
        network re-anchored on OpenStreetMap (020 geom_source 'osm') geom IS
        the road: no display line is left, the path is geom itself."""
        ids = list(range(1001, 1048))
        async with store._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'corridor' AND table_name = 'road_segments' AND column_name = 'display_geom')")  # noqa: E501
                has_015 = (await cur.fetchone())[0]
                await cur.execute("SELECT segment_id, ST_AsGeoJSON(geom) FROM corridor.road_segments WHERE segment_id = ANY(%s)", (ids,))  # noqa: E501
                schematic = {int(r[0]): [tuple(c) for c in json.loads(r[1])["coordinates"]]
                             for r in await cur.fetchall()}
                await cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'corridor' AND table_name = 'road_segments' AND column_name = 'geom_source')")  # noqa: E501
                on_osm = False
                if (await cur.fetchone())[0]:
                    await cur.execute("SELECT bool_and(geom_source = 'osm') FROM corridor.road_segments WHERE segment_id = ANY(%s)", (ids,))  # noqa: E501
                    on_osm = bool((await cur.fetchone())[0])
                display: dict[int, list] = {}
                if has_015:
                    await cur.execute("SELECT segment_id, ST_AsGeoJSON(display_geom) FROM corridor.road_segments WHERE segment_id = ANY(%s) AND display_geom IS NOT NULL", (ids,))  # noqa: E501
                    display = {int(r[0]): [tuple(c) for c in json.loads(r[1])["coordinates"]]
                               for r in await cur.fetchall()}
        paths = await store.segment_paths(ids)
        assert store._display_probe is not None and store._display_probe[0] is bool(has_015)
        assert set(paths) == set(schematic)
        for sid, line in paths.items():
            expect = display.get(sid, schematic[sid])
            assert line == pytest.approx(expect), sid
            # the drawn line starts and ends on the routing line's endpoints
            assert line[0] == pytest.approx(schematic[sid][0], abs=1e-6)
            assert line[-1] == pytest.approx(schematic[sid][-1], abs=1e-6)
        if has_015 and on_osm:
            assert display == {}, "the OSM network carries no display line"
        elif has_015:
            assert display, "015 store without display data: apply display_geometry_osm.sql"
        # anchors stay on the routing geometry (attribution anchors)
        anchors = await store.segment_midpoints([1014])
        assert 1014 in anchors

    async def test_anchors_lie_on_their_roads(self, store):
        """Every per-segment anchor is ON its routing line (the midpoint
        along it, as signal-ingress anchors its feeds) — never the centroid,
        which on a curved OpenStreetMap road can sit a kilometre off it."""
        async with store._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT segment_id FROM corridor.road_segments ORDER BY 1")
                ids = [int(r[0]) for r in await cur.fetchall()]
        anchors = await store.segment_midpoints(ids)
        assert set(anchors) == set(ids)
        async with store._pool.connection() as conn:
            async with conn.cursor() as cur:
                worst = 0.0
                for sid, (lon, lat) in anchors.items():
                    await cur.execute("SELECT ST_Distance(ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, geom::geography), ST_Distance(ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, ST_LineInterpolatePoint(geom, 0.5)::geography) FROM corridor.road_segments WHERE segment_id = %s", (lon, lat, lon, lat, sid))  # noqa: E501
                    d_line, d_mid = await cur.fetchone()
                    assert d_mid < 0.01, sid
                    worst = max(worst, float(d_line))
        assert worst < 1.0, worst
        assert await store.network_key() is not None

    async def test_every_library_route_reads_in_its_direction_of_travel(self, store):
        """The movement line of every use case: consecutive segments meet,
        one-way segments run their own way, and the line starts and ends
        on the route's first and last segment."""
        from event_emulator.library import load_library
        from event_emulator.tape import _dist_m, orient_route, route_polyline

        for uc in load_library().values():
            route = list(uc.config["route_segment_ids"])
            geo = await store.route_geometry(route)
            assert set(geo["lines"]) == set(route), uc.id
            assert len(geo["vias"]) == len(route) - 1
            oriented = orient_route(route, geo["lines"], oneway=geo["oneway"], vias=geo["vias"])
            for i in range(len(route) - 1):
                leave, enter = oriented[i][-1], oriented[i + 1][0]
                pair = (uc.id, route[i], route[i + 1])
                assert geo["vias"][i], pair                 # a recorded movement
                assert _dist_m(leave, enter) < 1.0, pair
                assert min(_dist_m(leave, v) for v in geo["vias"][i]) < 1.0, pair
            for sid, line in zip(route, oriented):
                if geo["oneway"][sid]:
                    assert line == geo["lines"][sid], (uc.id, sid)
            line = route_polyline(oriented)
            assert line[0] in (geo["lines"][route[0]][0], geo["lines"][route[0]][-1])
            assert line[-1] in (geo["lines"][route[-1]][0], geo["lines"][route[-1]][-1])

    async def test_a_pre_015_store_reads_the_schematic_line(self, store, monkeypatch):
        # force the pre-015 branch on any store: the schematic SQL path
        async def no_display(conn):
            return False

        monkeypatch.setattr(store, "display_geometry_supported", no_display)
        async with store._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT ST_AsGeoJSON(geom) FROM corridor.road_segments WHERE segment_id = 1014")  # noqa: E501
                row = await cur.fetchone()
                schematic = [tuple(c) for c in json.loads(row[0])["coordinates"]]
        paths = await store.segment_paths([1014])
        assert paths[1014] == pytest.approx(schematic)

    async def test_living_city_sites_match_the_seed_network(self, store):
        # the network check the living city runs at its first session: every
        # site's roads are in the road graph and meet the site's junctions
        from event_emulator.ambient import PATROLS, SITES

        adjacency = await store.segment_junction_ids()
        roads = {s for site in SITES for s in site.segments} | {
            s for _u, _b, route in PATROLS for s in route}
        assert roads <= set(adjacency), sorted(roads - set(adjacency))
        stray = [(site.site_id, j) for site in SITES for j in site.junctions
                 if not any(j in adjacency[s] for s in site.segments)]
        assert stray == []
