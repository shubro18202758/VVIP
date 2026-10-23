"""Road-graph and registry integrity — migration 012 + the seed files.

Static checks (always run, no database):
    * 012 stays in the splitter-safe, idempotent, SCHEMA-ONLY subset;
    * post_seed_derivations.sql is one guarded transaction and no longer
      writes network constants (the generated network step
      reanchor_osm_network.sql owns segments, junctions, cameras, stations);
    * the seed and the network step carry the identical 49-segment network,
      the twin's parser sees all of it, the seed's junction count is
      unchanged (the impact harness counts POINT literals), the seed ALONE
      is one connected network (what the twin parses), the seed's random
      observations are labelled synthetic and it never writes the REAL-only
      traffic.hourly_aggregates (019, contract C-P).

Database checks (run when VEER_TEST_PG_DSN names a store with 001-020 +
the seed files; READ-ONLY — safe against any store):
    * one connected component covering EVERY segment, and the directed
      graph is strongly connected;
    * every adjacency pair meets within 25 m, at its via junction;
    * pgr_dijkstra agg_cost equals the summed traversal + turn cost of the
      returned path (the 002 reverse edges undercut it by up to 68 %);
    * one-way segments are only entered at their start / left at their end;
    * the seed's movements are exactly the store's (from, to) pairs;
    * every signal-controlled junction has a program; >= 20 camera sites,
      each ON its own segment, none with a frame/image column; the
      e-challan FK is ON DELETE SET NULL; seed observations are synthetic.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]  # services/corridor-store
M012 = ROOT / "migrations" / "012_city_assets_graph_integrity.sql"
POST_SEED = ROOT / "seed" / "post_seed_derivations.sql"
SEED = ROOT / "seed" / "sample_corridor.sql"
NETWORK = ROOT / "seed" / "reanchor_osm_network.sql"

_SEG_RE = re.compile(
    r"\((\d{4}),\s*'([^']+)',\s*'(\w+)',\s*(\d+),\s*(\d+),\s*(TRUE|FALSE),\s*"
    r"ST_GeomFromText\('LINESTRING\(([^)]+)\)'"
)
# the twin's adjacency row regex (traffic_oracle/simulation/network.py)
_ADJ_RE = re.compile(r"\((\d{4}),\s*(\d{4}),\s*(\d+),\s*(\d+)\)")


def _seed_adjacency() -> list[tuple[int, int, int, int]]:
    text = SEED.read_text()
    block = text[text.index("INSERT INTO corridor.segment_adjacency"):
                 text.index("INSERT INTO traffic.observations")]
    return [tuple(int(x) for x in row) for row in _ADJ_RE.findall(block)]


def _statements(sql_text: str) -> list[str]:
    """Split like the naive consumers (embedding-service migration test)."""
    no_comments = "\n".join(line.split("--", 1)[0] for line in sql_text.splitlines())
    return [s.strip() for s in no_comments.split(";") if s.strip()]


# ── static: migration 012 ──────────────────────────────────────────────────


class TestMigration012Grammar:
    def test_no_semicolons_or_dashes_inside_literals(self):
        no_comments = "\n".join(
            line.split("--", 1)[0] for line in M012.read_text().splitlines()
        )
        for literal in re.findall(r"'([^']*)'", no_comments):
            assert ";" not in literal, literal

    def test_no_dollar_quoting_or_block_comments(self):
        text = M012.read_text()
        assert "$$" not in text
        assert not re.search(r"\$[A-Za-z_]+\$", text)
        assert "/*" not in text

    def test_schema_only_and_idempotent(self):
        stmts = _statements(M012.read_text())
        assert stmts
        dropped = set()
        for stmt in stmts:
            head = " ".join(stmt.split()[:3]).upper()
            assert head.split()[0] in {"CREATE", "ALTER"}, head
            # schema only: migrations run BEFORE the seed on a fresh install
            assert not re.search(
                r"\bINSERT\s+INTO\b|\bDELETE\s+FROM\b|\bUPDATE\s+\S+\s+SET\b", stmt, re.I
            ), stmt[:80]
            flat = " ".join(stmt.split())
            if flat.startswith("CREATE TABLE") or flat.startswith("CREATE INDEX"):
                assert "IF NOT EXISTS" in flat, flat[:80]
            if "ADD COLUMN" in flat:
                assert "ADD COLUMN IF NOT EXISTS" in flat, flat[:80]
            m = re.search(r"DROP CONSTRAINT IF EXISTS (\w+)", flat)
            if m:
                dropped.add(m.group(1))
            m = re.search(r"ADD CONSTRAINT (\w+)", flat)
            if m:
                assert m.group(1) in dropped, f"ADD {m.group(1)} without a guarded DROP"
            if flat.startswith("CREATE"):
                assert flat.startswith(("CREATE TABLE IF NOT EXISTS",
                                        "CREATE INDEX IF NOT EXISTS",
                                        "CREATE OR REPLACE VIEW")), flat[:80]

    def test_road_graph_view_keeps_002_columns_and_is_directed(self):
        text = M012.read_text()
        view = text[text.index("CREATE OR REPLACE VIEW corridor.road_graph"):]
        for col in ("AS id", "AS source", "AS target", "AS cost", "AS reverse_cost"):
            assert col in view
        assert "-1::float8" in view
        assert "ST_StartPoint(rt.geom)" in view and "ST_EndPoint(rf.geom)" in view

    def test_fk_becomes_set_null(self):
        flat = " ".join(M012.read_text().split())
        assert "ALTER COLUMN sighting_id DROP NOT NULL" in flat
        assert ("REFERENCES corridor.vehicle_sightings(sighting_id) ON DELETE SET NULL"
                in flat)

    def test_camera_registry_has_no_frame_or_person_columns(self):
        text = M012.read_text()
        block = text[text.index("CREATE TABLE IF NOT EXISTS corridor.cameras"):
                     text.index("CREATE INDEX IF NOT EXISTS idx_cameras_geom")]
        body = re.sub(r"--[^\n]*", "", block).lower()
        for banned in ("frame", "image", "video", "stream", "url", "face", "person"):
            assert not re.search(rf"\b\w*{banned}\w*\b\s+(text|bytea|jsonb)", body), banned


# ── static: post-seed + seed ───────────────────────────────────────────────


class TestSeedFiles:
    def test_post_seed_is_one_guarded_transaction(self):
        stmts = _statements(POST_SEED.read_text())
        assert stmts[0].upper() == "BEGIN" and stmts[-1].upper() == "COMMIT"
        assert "$$" not in POST_SEED.read_text()
        for stmt in stmts[1:-1]:
            flat = " ".join(stmt.split()).upper()
            if flat.startswith("INSERT"):
                assert ("ON CONFLICT" in flat or "WHERE NOT EXISTS" in flat), flat[:90]
            if flat.startswith("UPDATE"):
                assert "WHERE" in flat, flat[:90]

    def test_network_step_carries_the_seed_network(self):
        """The fresh-install seed and the network step that converts an
        existing store hold the SAME 49 segments (name, class, lanes, limit,
        one-way, geometry) — both generated by scripts/gen_osm_network.py;
        a hand edit of one would split the two install paths."""
        seed_rows = {int(r[0]): r for r in _SEG_RE.findall(SEED.read_text())}
        network_rows = {int(r[0]): r for r in _SEG_RE.findall(NETWORK.read_text())}
        # the twin's 4-digit parser sees the whole network
        assert set(seed_rows) == set(range(1001, 1050))
        assert network_rows == seed_rows

    def test_post_seed_writes_no_network_constants(self):
        """The schematic network's constants (connector rows, junction
        points, names, camera sites, stations) left the post-seed file with
        WP-20: applied to the real network they would add junk."""
        flat = " ".join(_statements(POST_SEED.read_text()))
        for table in ("road_segments", "junctions", "cameras", "agency_stations"):
            assert not re.search(rf"INSERT INTO corridor\.{table}\b", flat), table
        assert "UPDATE corridor.junctions" not in flat
        assert not _SEG_RE.search(POST_SEED.read_text())

    def test_seed_never_writes_the_real_only_hourly_aggregates(self):
        """traffic.hourly_aggregates is REAL-only (migration 019): the twin
        calibrates on it and /traffic/historical labels it provenance real.
        The seed's numbers are random — they may only be observations
        labelled synthetic, rolled up by the oracle into the labelled
        synthetic table."""
        flat = " ".join(_statements(SEED.read_text()))
        assert not re.search(r"\bhourly_aggregates\b(?!_synthetic)", flat)

    def test_seed_junction_count_unchanged_for_the_impact_harness(self):
        # scripts/impact_run.py counts POINT literals as junctions
        assert len(re.findall(r"ST_GeomFromText\('POINT\(", SEED.read_text())) == 15

    def test_seed_adjacency_joins_segments_that_meet(self):
        """No teleports in the seed itself: every hand-written adjacency row
        joins two segments that meet (shapely, planar degrees: 25 m is about
        0.00025 deg at Ahmedabad's latitude), at a junction the seed creates."""
        from shapely.geometry import LineString

        text = SEED.read_text()
        lines = {
            int(r[0]): LineString([tuple(map(float, p.split())) for p in r[6].split(",")])
            for r in _SEG_RE.findall(text)
        }
        block = text[text.index("INSERT INTO corridor.segment_adjacency"):
                     text.index("INSERT INTO traffic.observations")]
        rows = re.findall(r"\((\d{4}),\s*(\d{4}),\s*(\d+),\s*(\d+)\)", block)
        assert len(rows) >= 70
        n_junctions = len(re.findall(r"ST_GeomFromText\('POINT\(", text))
        for a, b, via, _cost in rows:
            assert 1 <= int(via) <= n_junctions, (a, b, via)
            gap = lines[int(a)].distance(lines[int(b)])
            assert gap < 0.00025, (a, b, round(gap * 111_000), "m apart")

    def test_seed_alone_is_one_connected_network(self):
        """What the twin parses (and a store loaded without the post-seed
        file routes on): every segment reachable from every other.  The
        far-end movements of the 1041-1047 connectors live in the seed,
        citing a placeholder junction the post-seed file re-points."""
        segments = {int(r[0]) for r in _SEG_RE.findall(SEED.read_text())}
        rows = _seed_adjacency()
        assert len({(a, b, v) for a, b, v, _c in rows}) == len(rows)  # PK unique
        undirected: dict[int, set[int]] = {}
        for a, b, _v, _c in rows:
            undirected.setdefault(a, set()).add(b)
            undirected.setdefault(b, set()).add(a)
        start = min(segments)
        seen, stack = {start}, [start]
        while stack:
            for nxt in undirected.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        assert seen == segments, sorted(segments - seen)
        # every movement is listed both ways (the post-seed C2 rule)
        pairs = {(a, b) for a, b, _v, _c in rows}
        assert {(b, a) for a, b in pairs} == pairs

    def test_seed_observations_are_labelled_synthetic(self):
        text = SEED.read_text()
        obs = text[text.index("INSERT INTO traffic.observations"):]
        assert "'real'" not in obs
        assert obs.count("'synthetic'") == 2


# ── database ────────────────────────────────────────────────────────────────

_DSN = os.environ.get("VEER_TEST_PG_DSN", "")
db = pytest.mark.skipif(
    not _DSN,
    reason="set VEER_TEST_PG_DSN to a store with 001-012 + seed + "
           "post_seed_derivations (read-only checks)",
)


def _q(sql: str, *args):
    async def run():
        import asyncpg

        conn = await asyncpg.connect(_DSN)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


_GRAPH = "SELECT id, source, target, cost, reverse_cost FROM corridor.road_graph"


@db
class TestGraphIntegrity:
    def test_one_component_covers_every_segment(self):
        rows = _q(
            "SELECT component, node FROM pgr_connectedComponents($1)", _GRAPH
        )
        nodes = {r["node"] for r in rows}
        segments = {r["segment_id"] for r in _q("SELECT segment_id FROM corridor.road_segments")}
        assert len({r["component"] for r in rows}) == 1
        assert nodes == segments

    def test_directed_graph_is_strongly_connected(self):
        rows = _q("SELECT component FROM pgr_strongComponents($1)", _GRAPH)
        assert len({r["component"] for r in rows}) == 1

    def test_every_adjacency_pair_meets_at_its_via_junction(self):
        bad = _q(
            """
            SELECT sa.from_segment_id, sa.to_segment_id, sa.via_junction_id
            FROM corridor.segment_adjacency sa
            JOIN corridor.road_segments a ON a.segment_id = sa.from_segment_id
            JOIN corridor.road_segments b ON b.segment_id = sa.to_segment_id
            JOIN corridor.junctions j ON j.junction_id = sa.via_junction_id
            WHERE NOT ST_DWithin(a.geom::geography, b.geom::geography, 25)
               OR NOT ST_DWithin(j.geom::geography, a.geom::geography, 25)
               OR NOT ST_DWithin(j.geom::geography, b.geom::geography, 25)
            """
        )
        assert [tuple(r) for r in bad] == []

    def test_every_touching_pair_is_adjacent(self):
        missing = _q(
            """
            WITH ends AS (
                SELECT segment_id, ST_StartPoint(geom) AS pt FROM corridor.road_segments
                UNION ALL
                SELECT segment_id, ST_EndPoint(geom) FROM corridor.road_segments
            )
            SELECT DISTINCT a.segment_id AS a, b.segment_id AS b
            FROM ends a JOIN ends b
              ON a.segment_id <> b.segment_id
             AND ST_DWithin(a.pt::geography, b.pt::geography, 1)
            WHERE NOT EXISTS (
                SELECT 1 FROM corridor.segment_adjacency sa
                WHERE sa.from_segment_id = a.segment_id
                  AND sa.to_segment_id = b.segment_id
            )
            """
        )
        assert [tuple(r) for r in missing] == []

    def test_dijkstra_cost_equals_traversal_plus_turns(self):
        seg = {
            r["segment_id"]: r["traverse_sec"]
            for r in _q(
                "SELECT segment_id, ST_Length(geom::geography) "
                "/ NULLIF(speed_limit_kmh * 1000.0 / 3600.0, 0) AS traverse_sec "
                "FROM corridor.road_segments"
            )
        }
        # the cheapest LEGAL turn per pair (012's rule): a movement onto a
        # one-way segment at its END (or off it at its start) is no edge
        turn: dict[tuple[int, int], float] = {}
        for r in _q(
            "SELECT sa.from_segment_id, sa.to_segment_id, min(sa.turn_cost_sec) AS t "
            "FROM corridor.segment_adjacency sa "
            "JOIN corridor.road_segments rf ON rf.segment_id = sa.from_segment_id "
            "JOIN corridor.road_segments rt ON rt.segment_id = sa.to_segment_id "
            "JOIN corridor.junctions j ON j.junction_id = sa.via_junction_id "
            "WHERE (NOT COALESCE(rt.oneway, FALSE) OR ST_DWithin("
            "ST_StartPoint(rt.geom)::geography, j.geom::geography, 25)) "
            "AND (NOT COALESCE(rf.oneway, FALSE) OR ST_DWithin("
            "ST_EndPoint(rf.geom)::geography, j.geom::geography, 25)) "
            "GROUP BY 1, 2"
        ):
            turn[(r["from_segment_id"], r["to_segment_id"])] = float(r["t"])
        ids = sorted(seg)
        rows = _q(
            "SELECT start_vid, end_vid, path_seq, node, agg_cost "
            "FROM pgr_dijkstra($1, $2::bigint[], $3::bigint[], true) "
            "ORDER BY start_vid, end_vid, path_seq",
            _GRAPH, ids, ids,
        )
        paths: dict[tuple[int, int], list] = {}
        for r in rows:
            paths.setdefault((r["start_vid"], r["end_vid"]), []).append(r)
        assert len(paths) == len(ids) * (len(ids) - 1)  # every ordered pair routes
        for (src, dst), path in paths.items():
            nodes = [p["node"] for p in path]
            expect = sum(
                turn[(a, b)] + seg[a] for a, b in zip(nodes, nodes[1:])
            )
            assert math.isclose(path[-1]["agg_cost"], expect, rel_tol=1e-9, abs_tol=1e-6), (
                src, dst, nodes, path[-1]["agg_cost"], expect,
            )

    def test_reverse_edges_are_gone(self):
        rows = _q("SELECT DISTINCT reverse_cost FROM corridor.road_graph")
        assert [r["reverse_cost"] for r in rows] == [-1.0]

    def test_acceptance_paths(self):
        # 1040 -> 1033 (SG Highway at Sarkhej -> SP Ring Road West) was
        # 124.8 s over a reversed 1033->1040 edge; on the real network it
        # runs the ring's south-west arc 1047 (7.9 km at 70 km/h, ~405 s)
        cost = _q(
            "SELECT max(agg_cost) AS c FROM pgr_dijkstra($1, 1040, 1033, true)", _GRAPH
        )[0]["c"]
        assert cost >= 380.0
        # Swami Vivekananda Road 1013 is one-way J5 -> J6: Ellis Bridge
        # traffic (1039) never enters it at its J6 end — the way into the
        # old city from the bridge's east landing does not exist there
        path = [
            r["node"]
            for r in _q(
                "SELECT node FROM pgr_dijkstra($1, 1039, 1038, true) ORDER BY seq", _GRAPH
            )
        ]
        assert path and path[0] == 1039 and path[-1] == 1038
        assert (1039, 1013) not in set(zip(path, path[1:]))

    def test_one_way_segments_are_entered_at_start_and_left_at_end(self):
        bad = _q(
            """
            SELECT g.source, g.target
            FROM corridor.road_graph g
            JOIN corridor.road_segments rf ON rf.segment_id = g.source
            JOIN corridor.road_segments rt ON rt.segment_id = g.target
            WHERE (rt.oneway AND NOT ST_DWithin(ST_StartPoint(rt.geom)::geography,
                                                 rf.geom::geography, 25))
               OR (rf.oneway AND NOT ST_DWithin(ST_EndPoint(rf.geom)::geography,
                                                 rt.geom::geography, 25))
            """
        )
        assert [tuple(r) for r in bad] == []

    def test_seed_movements_match_the_store(self):
        """The twin's seed-parsed graph IS the store's: same (from, to)
        movements with the same turn costs (vias differ only where the seed
        cites a placeholder junction the post-seed file re-pointed)."""
        store = {
            (r["from_segment_id"], r["to_segment_id"]): float(r["t"])
            for r in _q(
                "SELECT from_segment_id, to_segment_id, min(turn_cost_sec) AS t "
                "FROM corridor.segment_adjacency GROUP BY 1, 2"
            )
        }
        seed: dict[tuple[int, int], float] = {}
        for a, b, _v, cost in _seed_adjacency():
            seed[(a, b)] = min(float(cost), seed.get((a, b), float("inf")))
        assert seed == store

    def test_edge_ids_are_unique(self):
        rows = _q(
            "SELECT id, count(*) AS n FROM corridor.road_graph "
            "GROUP BY id HAVING count(*) > 1"
        )
        assert list(rows) == []


@db
class TestRegistries:
    def test_every_signal_junction_has_a_program(self):
        rows = _q(
            "SELECT junction_id FROM corridor.junctions j WHERE j.signal_control "
            "AND NOT EXISTS (SELECT 1 FROM corridor.signal_programs p "
            "WHERE p.junction_id = j.junction_id)"
        )
        assert list(rows) == []

    def test_every_junction_is_named_in_both_languages(self):
        rows = _q(
            "SELECT junction_id FROM corridor.junctions "
            "WHERE name IS NULL OR name_gu IS NULL"
        )
        assert list(rows) == []

    def test_camera_sites(self):
        rows = _q("SELECT camera_id, kind, source, junction_id FROM corridor.cameras")
        ids = {r["camera_id"] for r in rows}
        assert len(rows) >= 20
        assert {"EM-CAM-01", "EM-CAM-02", "EM-CAM-03", "EM-CAM-04"} <= ids
        assert all(r["source"] == "emulated" for r in rows if r["camera_id"].startswith("EM-"))
        cols = {
            r["column_name"]
            for r in _q(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'corridor' AND table_name = 'cameras'"
            )
        }
        assert not {c for c in cols if re.search(r"frame|image|video|url|face|person", c)}

    def test_camera_sites_sit_on_their_own_segment(self):
        """A read at a camera site must attribute to the camera's own
        segment (the e-challan engine and the interception plan both
        attribute a read to its NEAREST segment)."""
        rows = _q(
            """
            SELECT c.camera_id, c.segment_id,
                   ST_Distance(c.geom::geography, s.geom::geography) AS off_m,
                   (SELECT r.segment_id FROM corridor.road_segments r
                     ORDER BY r.geom <-> c.geom, r.segment_id LIMIT 1) AS nearest
            FROM corridor.cameras c
            JOIN corridor.road_segments s ON s.segment_id = c.segment_id
            """
        )
        assert rows
        for r in rows:
            assert r["off_m"] < 1.0, (r["camera_id"], r["off_m"])
            assert r["nearest"] == r["segment_id"], (r["camera_id"], r["nearest"])

    def test_agency_stations(self):
        rows = _q(
            "SELECT agency, kind, source, osm_ref, name, name_gu "
            "FROM corridor.agency_stations"
        )
        assert rows
        agencies = {"jan_rakshak", "police", "traffic", "ambulance", "fire"}
        assert {r["agency"] for r in rows} <= agencies
        for r in rows:
            assert r["name"] and r["name_gu"]
            if r["source"] == "osm":
                assert r["osm_ref"] and re.match(r"^(node|way|relation)/\d+$", r["osm_ref"])
            if r["source"] == "emulated":
                assert "(emulated)" in r["name"]

    def test_recommendation_fk_is_set_null(self):
        rows = _q(
            "SELECT confdeltype FROM pg_constraint "
            "WHERE conrelid = 'events.enforcement_recommendations'::regclass "
            "AND contype = 'f' "
            "AND confrelid = 'corridor.vehicle_sightings'::regclass"
        )
        # "char" columns arrive as bytes through asyncpg
        assert [bytes(r["confdeltype"]).decode() for r in rows] == ["n"]

    def test_seed_observations_are_not_labelled_real(self):
        rows = _q(
            "SELECT count(*) AS n FROM traffic.observations "
            "WHERE data_quality = 'real' AND timestamp_utc <= "
            "(SELECT min(created_at) FROM corridor.road_segments) + interval '1 minute'"
        )
        assert rows[0]["n"] == 0
