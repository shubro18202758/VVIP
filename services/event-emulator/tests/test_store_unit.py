"""CorridorStore logic that needs no database (a scripted pool)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from event_emulator import store as store_mod
from event_emulator.store import CorridorStore


class _Cursor:
    def __init__(self, pool: "_Pool") -> None:
        self._pool = pool

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: object = None) -> None:
        self._pool.queries.append(sql)

    async def fetchall(self) -> list[tuple[str]]:
        return [(self._pool.check,)]


class _Conn:
    def __init__(self, pool: "_Pool") -> None:
        self._pool = pool

    def cursor(self) -> _Cursor:
        return _Cursor(self._pool)

    async def execute(self, sql: str, params: object = None) -> None:
        self._pool.queries.append(sql)
        self._pool.writes.append(params)


class _Pool:
    """Answers the status-CHECK probe with ``check``; records the rest."""

    def __init__(self, check: str) -> None:
        self.check = check
        self.queries: list[str] = []
        self.writes: list[object] = []

    @asynccontextmanager
    async def connection(self, timeout: float | None = None):
        yield _Conn(self)


OLD_CHECK = "CHECK ((status = ANY (ARRAY['running'::text, 'completed'::text, 'failed'::text, 'stopped'::text])))"  # noqa: E501
NEW_CHECK = OLD_CHECK.replace("'stopped'::text", "'stopped'::text, 'interrupted'::text")


class TestLedgerCheckProbe:
    async def test_a_widened_check_is_picked_up_without_a_restart(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(store_mod.time, "monotonic", lambda: clock[0])
        pool = _Pool(OLD_CHECK)
        store = CorridorStore(pool, runner={"host": "h", "port": 1, "boot": "b"})
        assert await store.finish_run(1, "interrupted", {}) == "stopped"
        probes = sum("pg_constraint" in q for q in pool.queries)
        # within the re-probe window the answer is cached
        clock[0] += 60.0
        assert await store.finish_run(2, "interrupted", {}) == "stopped"
        assert sum("pg_constraint" in q for q in pool.queries) == probes
        # migration 016 (WP-19) widens the CHECK; the next probe after the window sees it
        pool.check = NEW_CHECK
        clock[0] += store_mod._LEDGER_REPROBE_SEC
        assert await store.finish_run(3, "interrupted", {}) == "interrupted"
        # a yes is kept for the life of the process
        clock[0] += 10 * store_mod._LEDGER_REPROBE_SEC
        pool.check = OLD_CHECK
        assert await store.finish_run(4, "interrupted", {}) == "interrupted"

    async def test_accepted_statuses_never_probe(self):
        pool = _Pool(OLD_CHECK)
        store = CorridorStore(pool, runner={"host": "h", "port": 1, "boot": "b"})
        for status in ("completed", "failed", "stopped"):
            assert await store.finish_run(1, status, {}) == status
        assert not any("pg_constraint" in q for q in pool.queries)


class _GeoCursor:
    def __init__(self, pool: "_GeoPool") -> None:
        self._pool = pool
        self._sql = ""
        self._params: object = None

    async def __aenter__(self) -> "_GeoCursor":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: object = None) -> None:
        self._sql, self._params = sql, params
        self._pool.queries.append(sql)

    async def fetchone(self):
        if "pg_attribute" in self._sql:
            return (self._pool.display,)
        if "md5(string_agg" in self._sql:
            return (len(self._pool.anchors), self._pool.fingerprint)
        raise AssertionError(self._sql)

    async def fetchall(self):
        wanted = set(self._params[0]) if self._params else set()
        if "ST_LineInterpolatePoint" in self._sql:
            return [(s, *xy) for s, xy in self._pool.anchors.items() if s in wanted]
        if "COALESCE(oneway" in self._sql:
            return [(s, s in self._pool.oneway, _geojson(ln))
                    for s, ln in self._pool.lines.items() if s in wanted]
        if "segment_adjacency" in self._sql:
            return list(self._pool.via_rows)
        if "ST_AsGeoJSON" in self._sql:
            return [(s, _geojson(ln)) for s, ln in self._pool.lines.items() if s in wanted]
        raise AssertionError(self._sql)


def _geojson(line: list) -> str:
    return json.dumps({"type": "LineString", "coordinates": line})


class _GeoConn:
    def __init__(self, pool: "_GeoPool") -> None:
        self._pool = pool

    def cursor(self) -> _GeoCursor:
        return _GeoCursor(self._pool)


class _GeoPool:
    """A scripted road network: the display probe, the network fingerprint,
    midpoints, lines and adjacency rows."""

    def __init__(self) -> None:
        self.display = False
        self.fingerprint = "schematic"
        self.anchors = {1005: (72.58, 23.02), 1012: (72.59, 23.03)}
        self.lines = {1005: [[72.57, 23.02], [72.59, 23.02]],
                      1012: [[72.59, 23.02], [72.59, 23.04]]}
        self.oneway: set[int] = set()
        self.via_rows: list[tuple] = []
        self.queries: list[str] = []
        self.down = False

    @asynccontextmanager
    async def connection(self, timeout: float | None = None):
        if self.down:
            raise ConnectionError("store down")
        yield _GeoConn(self)


def _clocked(monkeypatch) -> list[float]:
    clock = [1000.0]
    monkeypatch.setattr(store_mod.time, "monotonic", lambda: clock[0])
    return clock


class TestOnRoadAnchors:
    async def test_anchors_are_the_midpoint_along_the_line_never_the_centroid(self):
        pool = _GeoPool()
        store = CorridorStore(pool, runner={"host": "h", "port": 1, "boot": "b"})
        assert await store.segment_midpoints([1005, 1012, 9999]) == {
            1005: (72.58, 23.02), 1012: (72.59, 23.03)}
        [sql] = [q for q in pool.queries if "ST_LineInterpolatePoint" in q]
        assert "ST_LineInterpolatePoint(geom, 0.5)" in sql
        assert not any("ST_Centroid" in q for q in pool.queries)

    async def test_a_changed_network_is_re_read_without_a_restart(self, monkeypatch):
        clock = _clocked(monkeypatch)
        pool = _GeoPool()
        store = CorridorStore(pool, runner={"host": "h", "port": 1, "boot": "b"})
        await store.segment_midpoints([1005])
        await store.segment_paths([1005])
        assert await store.network_key() == (False, "2:schematic")
        # inside the window: served from the cache, no round trip at all
        pool.queries.clear()
        pool.anchors[1005] = (1.0, 1.0)
        assert await store.segment_midpoints([1005]) == {1005: (72.58, 23.02)}
        assert pool.queries == []
        # the window passed, the network is the same: one fingerprint probe
        clock[0] += store_mod._NETWORK_REPROBE_SEC
        assert await store.segment_midpoints([1005]) == {1005: (72.58, 23.02)}
        assert [q for q in pool.queries if "md5(string_agg" in q] and not [
            q for q in pool.queries if "ST_LineInterpolatePoint" in q]
        # the network is converted under the running emulator (WP-20): the
        # next probe drops the cached anchors AND paths
        clock[0] += store_mod._NETWORK_REPROBE_SEC
        pool.fingerprint = "osm"
        pool.lines[1005] = [[72.571, 23.021], [72.589, 23.023]]
        assert await store.segment_midpoints([1005]) == {1005: (1.0, 1.0)}
        assert await store.network_key() == (False, "2:osm")
        assert await store.segment_paths([1005]) == {1005: [(72.571, 23.021), (72.589, 23.023)]}

    async def test_the_display_basis_landing_keys_the_paths_too(self, monkeypatch):
        clock = _clocked(monkeypatch)
        pool = _GeoPool()
        store = CorridorStore(pool, runner={"host": "h", "port": 1, "boot": "b"})
        await store.segment_paths([1012])
        assert not any("COALESCE(display_geom" in q for q in pool.queries)
        pool.display = True                       # migration 015 lands
        clock[0] += store_mod._DISPLAY_REPROBE_SEC
        pool.queries.clear()
        await store.segment_paths([1012])
        assert any("COALESCE(display_geom" in q for q in pool.queries)
        assert (await store.network_key())[0] is True

    async def test_network_key_never_raises_and_backs_off(self, monkeypatch):
        clock = _clocked(monkeypatch)
        pool = _GeoPool()
        store = CorridorStore(pool, runner={"host": "h", "port": 1, "boot": "b"})
        pool.down = True
        assert await store.network_key() is None
        pool.down = False
        pool.queries.clear()
        assert await store.network_key() is None   # asked again a window later
        assert pool.queries == []
        clock[0] += store_mod._NETWORK_REPROBE_SEC
        assert await store.network_key() == (False, "2:schematic")


class TestRouteGeometry:
    async def test_lines_one_ways_and_the_junctions_of_each_pair(self):
        pool = _GeoPool()
        pool.oneway = {1012}
        # ordinal 1 = the pair (1005, 1012), met at two junctions
        pool.via_rows = [(1, 72.59, 23.02), (1, 72.57, 23.02)]
        store = CorridorStore(pool, runner={"host": "h", "port": 1, "boot": "b"})
        geo = await store.route_geometry([1005, 1012])
        assert geo["lines"] == {1005: [(72.57, 23.02), (72.59, 23.02)],
                                1012: [(72.59, 23.02), (72.59, 23.04)]}
        assert geo["oneway"] == {1005: False, 1012: True}
        assert geo["vias"] == [[(72.59, 23.02), (72.57, 23.02)]]
        [adj] = [q for q in pool.queries if "segment_adjacency" in q]
        assert "WITH ORDINALITY" in adj and "ST_AsGeoJSON(geom)" in pool.queries[0]

    async def test_a_lone_segment_asks_for_no_junctions(self):
        pool = _GeoPool()
        store = CorridorStore(pool, runner={"host": "h", "port": 1, "boot": "b"})
        geo = await store.route_geometry([1005])
        assert geo["vias"] == [] and set(geo["lines"]) == {1005}
        assert not any("segment_adjacency" in q for q in pool.queries)


class TestMovementReanimation:
    async def test_a_reanimation_moves_the_ends_too(self):
        """1a: a replay re-animates the emulator's OWN movement row — its
        origin and destination follow the route drawn on today's roads, not
        the off-road ends an earlier network gave it."""
        import uuid
        from datetime import datetime, timezone

        pool = _Pool(OLD_CHECK)
        store = CorridorStore(pool, runner={"host": "h", "port": 1, "boot": "b"})
        await store.upsert_movement(str(uuid.uuid4()), "X", datetime.now(timezone.utc),
                                    (72.58, 23.02), (72.59, 23.03))
        [sql] = pool.queries
        conflict = sql.split("ON CONFLICT (movement_id) DO UPDATE SET ")[1]
        assert "origin_geom = EXCLUDED.origin_geom" in conflict
        assert "destination_geom = EXCLUDED.destination_geom" in conflict
        assert "status = 'planning'" in conflict and "planned_start = EXCLUDED.planned_start" in conflict
        # the ends bind as GeoJSON parameters, never as SQL text
        assert "72.58" not in sql and '"Point"' in pool.writes[0][2]


class _RouteCursor:
    def __init__(self, pool: "_RoutePool") -> None:
        self._pool = pool
        self._sql = ""
        self._params: object = None

    async def __aenter__(self) -> "_RouteCursor":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: object = None) -> None:
        self._sql, self._params = sql, params
        self._pool.queries.append(sql)

    async def fetchall(self):
        if "FROM corridor.road_segments" in self._sql:
            return [(s,) for s in self._params[0] if s in self._pool.segments]
        if "corridor.road_graph" in self._sql:
            pairs = zip(self._params[0], self._params[1])
            return [(a, b) for a, b in pairs if (a, b) in self._pool.edges]
        raise AssertionError(self._sql)


class _RouteConn:
    def __init__(self, pool: "_RoutePool") -> None:
        self._pool = pool

    def cursor(self) -> _RouteCursor:
        return _RouteCursor(self._pool)


class _RoutePool:
    """A scripted road graph: the segments it has and its directed moves."""

    def __init__(self, segments: set[int], edges: set[tuple[int, int]]) -> None:
        self.segments, self.edges = segments, edges
        self.queries: list[str] = []

    @asynccontextmanager
    async def connection(self, timeout: float | None = None):
        yield _RouteConn(self)


class TestRouteProblems:
    """1a: what makes a recorded route replayable — a legal path on the
    store's CURRENT corridor.road_graph (migration 012's directed moves)."""

    def _store(self, segments, edges):
        pool = _RoutePool(set(segments), set(edges))
        return CorridorStore(pool, runner={"host": "h", "port": 1, "boot": "b"}), pool

    async def test_a_legal_path_has_no_problem(self):
        store, pool = self._store({1015, 1012, 1049, 1013},
                                  {(1015, 1012), (1012, 1049), (1049, 1013)})
        assert await store.route_problems([1015, 1012, 1049, 1013]) == []
        [_, graph] = pool.queries
        assert "corridor.road_graph g WHERE g.source = p.from_id AND g.target = p.to_id" in graph

    async def test_the_pre_conversion_sweep_belt_is_not_a_path(self):
        # live run 20's recorded route on the converted network: 1012 is a
        # one-way that no longer meets 1015 that way, 1005 never meets 1013
        store, _ = self._store({1012, 1015, 1005, 1013}, {(1015, 1005), (1015, 1012)})
        assert await store.route_problems([1012, 1015, 1005, 1013]) == [
            "1012 -> 1015 is not a legal move", "1005 -> 1013 is not a legal move"]

    async def test_unknown_segments_and_an_empty_route(self):
        store, pool = self._store({1005}, set())
        assert await store.route_problems([1005, 9999]) == ["unknown segments [9999]"]
        assert await store.route_problems([]) == ["no route recorded"]
        pool.queries.clear()
        assert await store.route_problems([1005]) == []
        assert len(pool.queries) == 1            # a lone segment asks for no moves
