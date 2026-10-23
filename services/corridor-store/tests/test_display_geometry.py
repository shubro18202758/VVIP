"""Road display geometry — migration 015 + seed/display_geometry_osm.sql (C-K).

Static checks (always run, no database):
    * 015 stays in the splitter-safe, idempotent, SCHEMA-ONLY subset (the
      013 header grammar): no ';' inside literals, no dollar quoting or
      block comments, ADD COLUMN / CREATE INDEX guarded, the one ADD
      CONSTRAINT preceded by its guarded DROP; the DISPLAY ONLY comment;
    * the data file is SUPERSEDED (WP-20: the routing geom is the
      OpenStreetMap road, basis 'osm'): one guarded transaction with the
      ODbL attribution that only clears a display line, generated with the
      network by scripts/gen_osm_network.py — scripts/gen_display_geometry.py
      is a shim that delegates to it (the network generator's own tests:
      test_osm_network.py).

Database checks (VEER_TEST_PG_DSN names a scratch store; every write runs
inside a transaction that is ROLLED BACK — nothing persists):
    * fresh apply: 015's statements, split like the naive consumers split
      them, run against a throwaway schema holding 001's road_segments
      definition — the columns, the CHECKs, the GIST index and the comment
      appear; a second apply is a no-op (same catalog); the endpoint CHECK
      admits a display line on the schematic endpoints and refuses one
      that is not;
    * on a store with 015: applying the data file twice, the second pass
      changes no row, and no segment is left with a display line.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]  # services/corridor-store
REPO = ROOT.parents[1]
M015 = ROOT / "migrations" / "015_road_display_geometry.sql"
DATA = ROOT / "seed" / "display_geometry_osm.sql"
SEED = ROOT / "seed" / "sample_corridor.sql"
GENERATOR = REPO / "scripts" / "gen_display_geometry.py"

DISPLAY_ONLY = (
    "DISPLAY ONLY — routing, costs, twin, ANPR attribution, diversions and ingress use geom"
)


def _statements(sql_text: str) -> list[str]:
    """Split like the naive consumers (embedding-service migration test)."""
    no_comments = "\n".join(line.split("--", 1)[0] for line in sql_text.splitlines())
    return [s.strip() for s in no_comments.split(";") if s.strip()]


# ── static: migration 015 ──────────────────────────────────────────────────


class TestMigration015Grammar:
    def test_header_names_itself_and_the_contract(self):
        head = M015.read_text(encoding="utf-8").splitlines()[:3]
        assert head[0] == "-- 015_road_display_geometry.sql"
        assert "C-K" in head[1]

    def test_no_semicolons_or_dashes_inside_literals(self):
        no_comments = "\n".join(
            line.split("--", 1)[0] for line in M015.read_text(encoding="utf-8").splitlines()
        )
        for literal in re.findall(r"'([^']*)'", no_comments):
            assert ";" not in literal, literal

    def test_no_dollar_quoting_or_block_comments(self):
        text = M015.read_text(encoding="utf-8")
        assert "$$" not in text
        assert not re.search(r"\$[A-Za-z_]+\$", text)
        assert "/*" not in text

    def test_schema_only_and_idempotent(self):
        stmts = _statements(M015.read_text(encoding="utf-8"))
        assert stmts
        dropped: set[str] = set()
        for stmt in stmts:
            flat = " ".join(stmt.split())
            assert flat.split()[0].upper() in {"ALTER", "CREATE", "COMMENT"}, flat[:60]
            # schema only: migrations run BEFORE the seed on a fresh install
            assert not re.search(
                r"\bINSERT\s+INTO\b|\bDELETE\s+FROM\b|\bUPDATE\s+\S+\s+SET\b", flat, re.I
            ), flat[:80]
            if "ADD COLUMN" in flat:
                assert "ADD COLUMN IF NOT EXISTS" in flat, flat[:80]
            if flat.startswith("CREATE"):
                assert flat.startswith("CREATE INDEX IF NOT EXISTS"), flat[:80]
            m = re.search(r"DROP CONSTRAINT IF EXISTS (\w+)", flat)
            if m:
                dropped.add(m.group(1))
            m = re.search(r"ADD CONSTRAINT (\w+)", flat)
            if m:
                assert m.group(1) in dropped, f"ADD {m.group(1)} without a guarded DROP"

    def test_columns_index_check_and_comment(self):
        flat = " ".join(M015.read_text(encoding="utf-8").split())
        assert "ADD COLUMN IF NOT EXISTS display_geom geometry(LineString, 4326)" in flat
        assert ("ADD COLUMN IF NOT EXISTS display_geom_source TEXT CHECK "
                "(display_geom_source IN ('osm_way_stitch', 'osrm_route'))") in flat
        assert "ADD COLUMN IF NOT EXISTS display_geom_meta JSONB" in flat
        assert ("CREATE INDEX IF NOT EXISTS idx_road_segments_display_geom "
                "ON corridor.road_segments USING GIST (display_geom)") in flat
        assert "ADD CONSTRAINT road_segments_display_geom_ends" in flat
        assert f"COMMENT ON COLUMN corridor.road_segments.display_geom IS '{DISPLAY_ONLY}'" in flat


# ── static: the data file (superseded by the OpenStreetMap network) ───────


class TestDisplayDataFile:
    """Since WP-20 the routing geom IS the OpenStreetMap road (basis 'osm'):
    the data file draws nothing, it only clears a display line a store
    still carries — generated with the network by scripts/gen_osm_network.py,
    which scripts/gen_display_geometry.py now delegates to."""

    def test_one_guarded_transaction_with_attribution(self):
        text = DATA.read_text(encoding="utf-8")
        stmts = _statements(text)
        assert stmts[0].upper() == "BEGIN" and stmts[-1].upper() == "COMMIT"
        assert "$$" not in text and "/*" not in text
        assert "(c) OpenStreetMap contributors, ODbL 1.0" in text
        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \(osm_base\)", text)
        assert "GENERATED by scripts/gen_osm_network.py" in text.splitlines()[0]
        body = stmts[1:-1]
        assert len(body) == 1
        flat = " ".join(body[0].split())
        # it only ever CLEARS: never a line, never a source
        assert flat.startswith("UPDATE corridor.road_segments SET display_geom = NULL, "
                               "display_geom_source = NULL, display_geom_meta = NULL"), flat
        assert "ST_GeomFromText" not in flat and "IS NOT NULL" in flat

    def test_the_legacy_generator_delegates_to_the_network_generator(self):
        text = GENERATOR.read_text(encoding="utf-8")
        assert "SUPERSEDED" in text and "gen_osm_network.py" in text
        assert len(text.splitlines()) < 80     # a shim, not a second tracer

    def test_literals_stay_splitter_safe(self):
        for literal in re.findall(r"'([^']*)'", "\n".join(
                line.split("--", 1)[0] for line in DATA.read_text(encoding="utf-8").splitlines())):
            assert ";" not in literal and "--" not in literal, literal


# ── database (rolled back: nothing persists) ───────────────────────────────

_DSN = os.environ.get("VEER_TEST_PG_DSN", "")
db = pytest.mark.skipif(
    not _DSN,
    reason="set VEER_TEST_PG_DSN to a scratch corridor store (writes are rolled back)",
)

#: 001's corridor.road_segments, verbatim columns (the pre-015 shape).
_ROAD_SEGMENTS_001 = """
CREATE TABLE {schema}.road_segments (
    segment_id      BIGINT PRIMARY KEY,
    osm_way_id      BIGINT,
    road_name       TEXT,
    road_class      TEXT NOT NULL CHECK (road_class IN (
        'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'residential', 'service'
    )),
    lanes           SMALLINT DEFAULT 2,
    speed_limit_kmh SMALLINT DEFAULT 50,
    oneway          BOOLEAN DEFAULT FALSE,
    geom            GEOMETRY(LineString, 4326) NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
)
"""

_CATALOG = """
SELECT
  (SELECT array_agg(attname || ':' || format_type(atttypid, atttypmod) ORDER BY attnum)
     FROM pg_attribute WHERE attrelid = $1::text::regclass AND attnum > 0
       AND NOT attisdropped) AS cols,
  (SELECT array_agg(conname || ':' || pg_get_constraintdef(oid) ORDER BY conname)
     FROM pg_constraint WHERE conrelid = $1::text::regclass) AS cons,
  (SELECT array_agg(indexdef ORDER BY indexname) FROM pg_indexes
     WHERE schemaname || '.' || tablename = $1::text) AS idx,
  col_description($1::text::regclass,
     (SELECT attnum FROM pg_attribute WHERE attrelid = $1::text::regclass
        AND attname = 'display_geom')::int) AS comment
"""


def _run(coro):
    return asyncio.run(coro)


async def _connect():
    asyncpg = pytest.importorskip("asyncpg")
    return await asyncpg.connect(_DSN)


@db
class TestMigration015Apply:
    def test_fresh_apply_then_noop_reapply(self):
        async def run():
            conn = await _connect()
            schema = f"veer_t015_{secrets.token_hex(4)}"
            tr = conn.transaction()
            await tr.start()
            try:
                await conn.execute(f"CREATE SCHEMA {schema}")
                await conn.execute(_ROAD_SEGMENTS_001.format(schema=schema))
                stmts = [
                    s.replace("corridor.road_segments", f"{schema}.road_segments")
                    for s in _statements(M015.read_text(encoding="utf-8"))
                ]
                for stmt in stmts:          # one at a time: the naive splitter's way
                    await conn.execute(stmt)
                rel = f"{schema}.road_segments"
                first = await conn.fetchrow(_CATALOG, rel)
                cols = dict(c.split(":", 1) for c in first["cols"])
                assert cols["display_geom"] == "geometry(LineString,4326)"
                assert cols["display_geom_source"] == "text"
                assert cols["display_geom_meta"] == "jsonb"
                assert cols["geom"] == "geometry(LineString,4326)"   # untouched
                cons = " ".join(first["cons"])
                assert "road_segments_display_geom_ends" in cons
                assert "osm_way_stitch" in cons and "osrm_route" in cons
                assert any("USING gist (display_geom)" in i for i in first["idx"])
                assert first["comment"] == DISPLAY_ONLY
                for stmt in stmts:          # re-apply: a no-op
                    await conn.execute(stmt)
                again = await conn.fetchrow(_CATALOG, rel)
                assert dict(again) == dict(first)

                # the endpoint CHECK: same start and end as geom, or NULL
                await conn.execute(
                    f"INSERT INTO {rel} (segment_id, road_name, road_class, geom) VALUES "
                    "(1, 't', 'primary', "
                    "ST_GeomFromText('LINESTRING(72.60 23.02, 72.61 23.03)', 4326))"
                )
                await conn.execute(
                    f"UPDATE {rel} SET display_geom = ST_GeomFromText("
                    "'LINESTRING(72.60 23.02, 72.6060 23.0230, 72.61 23.03)', 4326), "
                    "display_geom_source = 'osm_way_stitch' WHERE segment_id = 1"
                )
                asyncpg = pytest.importorskip("asyncpg")
                for bad in (
                    # reversed: the right points, the wrong orientation
                    "display_geom = ST_GeomFromText('LINESTRING(72.61 23.03, 72.60 23.02)', 4326)",
                    # a start 100 m off the schematic start
                    "display_geom = ST_GeomFromText('LINESTRING(72.601 23.02, 72.61 23.03)', 4326)",
                    "display_geom_source = 'hand_drawn'",
                ):
                    sp = conn.transaction()
                    await sp.start()
                    with pytest.raises(asyncpg.CheckViolationError):
                        await conn.execute(f"UPDATE {rel} SET {bad} WHERE segment_id = 1")
                    await sp.rollback()
            finally:
                await tr.rollback()
                await conn.close()

        _run(run())


@db
class TestDisplayDataApply:
    def test_second_apply_changes_no_row_and_no_line_is_left(self):
        async def run():
            conn = await _connect()
            has_col = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE "
                "table_schema = 'corridor' AND table_name = 'road_segments' "
                "AND column_name = 'display_geom')"
            )
            if not has_col:
                await conn.close()
                pytest.skip("store has no migration 015 column")
            stmts = _statements(DATA.read_text(encoding="utf-8"))[1:-1]  # inside BEGIN/COMMIT
            tr = conn.transaction()
            await tr.start()
            try:
                for stmt in stmts:
                    await conn.execute(stmt)
                counts = [await conn.execute(stmt) for stmt in stmts]
                assert counts and all(c == "UPDATE 0" for c in counts), counts
                left = await conn.fetchval(
                    "SELECT count(*) FROM corridor.road_segments WHERE display_geom IS NOT NULL "
                    "AND segment_id BETWEEN 1001 AND 1049")
                assert left == 0
            finally:
                await tr.rollback()
                await conn.close()

        _run(run())
