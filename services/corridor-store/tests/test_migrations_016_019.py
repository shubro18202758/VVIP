"""Migrations 016-019 (WP-19, wave 3): grammar, contract and apply tests.

    016_emulation_runs_interrupted.sql    emulation_runs.status + 'interrupted'
    017_enforcement_stolen_sighting.sql   rule_id + 'R_STOLEN_SIGHTING' (C-N)
    018_event_routes_supersede.sql        event_routes superseded_at / variant /
                                          created_by + a current-spine index (C-O)
    019_hourly_aggregates_synthetic.sql   the synthetic rollup table (C-P) —
                                          never a calibration input

Static checks (always run, no database) — the constraints every consumer
imposes: the naive ';'-splitters (embedding-service's lexical apply, the
corridor-store grammar tests) and the ledgered migrate.py:
    * the 013 header grammar (file name line, what/why, the grammar note);
    * no ';' or '--' inside string literals, no dollar-quoted bodies, no
      block comments, every statement a splittable CREATE / ALTER / COMMENT;
    * schema only (no INSERT / UPDATE / DELETE) and idempotent (IF NOT
      EXISTS everywhere, every ADD CONSTRAINT behind a guarded DROP);
    * the contracts: 016's CHECK keeps every 009 status, 017's equals the
      oracle's ALL_RULE_IDS, 019's columns equal 001's hourly_aggregates.

Database checks (VEER_TEST_PG_DSN = a SANDBOX Postgres the role may create
databases on; never the live :56434 store): a FRESH throwaway database gets
001..014 applied, then 016..019 through the naive splitter, then 016..019
AGAIN — the second apply must be a no-op (identical catalog) — and the
contract objects are asserted on the catalog.  The throwaway database is
dropped afterwards.
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
MIGRATIONS = ROOT / "migrations"
WAVE3 = {
    "016": MIGRATIONS / "016_emulation_runs_interrupted.sql",
    "017": MIGRATIONS / "017_enforcement_stolen_sighting.sql",
    "018": MIGRATIONS / "018_event_routes_supersede.sql",
    "019": MIGRATIONS / "019_hourly_aggregates_synthetic.sql",
}


def _no_comments(sql_text: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql_text.splitlines())


def _statements(sql_text: str) -> list[str]:
    """Split like the naive consumers: strip -- comments, split on ';'."""
    return [s.strip() for s in _no_comments(sql_text).split(";") if s.strip()]


def _flat(stmt: str) -> str:
    return " ".join(stmt.split())


# ── static: grammar ────────────────────────────────────────────────────────


@pytest.mark.parametrize("num", sorted(WAVE3))
class TestGrammar:
    def test_013_header_grammar(self, num):
        path = WAVE3[num]
        lines = path.read_text().splitlines()
        assert lines[0] == f"-- {path.name}", "first line names the file"
        header = "\n".join(line for line in lines if line.startswith("--"))
        assert "SCHEMA ONLY, additive, idempotent" in header
        assert "Grammar constraint (007 header): consumed by naive ';'-splitters" in header
        assert "Ethics rails (C6)" in header

    def test_splitter_safe_literals(self, num):
        text = WAVE3[num].read_text()
        for literal in re.findall(r"'([^']*)'", _no_comments(text)):
            assert ";" not in literal and "--" not in literal, literal
        assert "$$" not in text and not re.search(r"\$[A-Za-z_]+\$", text)
        assert "/*" not in text

    def test_schema_only_idempotent_statements(self, num):
        stmts = _statements(WAVE3[num].read_text())
        assert stmts
        dropped: set[str] = set()
        for stmt in stmts:
            flat = _flat(stmt)
            assert flat.split()[0] in {"CREATE", "ALTER", "COMMENT"}, flat[:60]
            assert not re.search(r"\bINSERT\s+INTO\b|\bDELETE\s+FROM\b|\bUPDATE\s+\S+\s+SET\b",
                                 flat, re.I), flat[:60]
            if flat.startswith("CREATE"):
                assert flat.startswith(("CREATE TABLE IF NOT EXISTS",
                                        "CREATE INDEX IF NOT EXISTS")), flat[:60]
            if "ADD COLUMN" in flat:
                assert "ADD COLUMN IF NOT EXISTS" in flat, flat[:60]
            m = re.search(r"DROP CONSTRAINT IF EXISTS (\w+)", flat)
            if m:
                dropped.add(m.group(1))
            m = re.search(r"ADD CONSTRAINT (\w+)", flat)
            if m:
                assert m.group(1) in dropped, f"ADD {m.group(1)} without a guarded DROP"


# ── static: contracts ──────────────────────────────────────────────────────


def _check_values(text: str, constraint: str, column: str) -> list[str]:
    m = re.search(
        rf"ADD CONSTRAINT {constraint} CHECK \({column} IN \((.*?)\)\)", text, re.S
    )
    assert m, f"{constraint} is not re-added"
    return re.findall(r"'([A-Za-z_]+)'", m.group(1))


class TestContracts:
    def test_016_keeps_every_009_status_and_adds_interrupted(self):
        values = _check_values(WAVE3["016"].read_text(), "emulation_runs_status_check", "status")
        original = re.findall(
            r"'(\w+)'",
            re.search(r"status IN \((.*?)\)", (MIGRATIONS / "009_emulation_runs.sql").read_text(),
                      re.S).group(1),
        )
        assert values == original + ["interrupted"]

    def test_017_is_exactly_the_oracles_rule_vocabulary(self):
        import sys

        sys.path.insert(0, str(REPO / "services" / "traffic-oracle" / "src"))
        from traffic_oracle.enforcement.rules import ALL_RULE_IDS, RULE_IDS

        values = _check_values(
            WAVE3["017"].read_text(), "enforcement_recommendations_rule_id_check", "rule_id"
        )
        assert values == list(ALL_RULE_IDS)
        assert values[:3] == list(RULE_IDS)  # every 010 id still allowed

    def test_018_adds_three_nullable_columns_and_a_current_index(self):
        flat = _flat(_no_comments(WAVE3["018"].read_text()))
        for col, typ in (("superseded_at", "TIMESTAMPTZ"), ("variant", "TEXT"),
                         ("created_by", "TEXT")):
            assert f"ADD COLUMN IF NOT EXISTS {col} {typ};" in flat
            assert f"{col} {typ} NOT NULL" not in flat
        assert ("CREATE INDEX IF NOT EXISTS idx_event_routes_current ON events.event_routes "
                "(event_id, route_rank) WHERE superseded_at IS NULL") in flat

    def test_019_mirrors_001s_hourly_aggregates(self):
        def columns(text: str, table: str) -> list[str]:
            body = re.search(rf"{re.escape(table)} \((.*?)\n\);", text, re.S).group(1)
            return [" ".join(line.split()) for line in body.splitlines() if line.strip()]

        real = columns((MIGRATIONS / "001_spatial_init.sql").read_text(),
                       "CREATE TABLE traffic.hourly_aggregates")
        synthetic = columns(WAVE3["019"].read_text(),
                            "CREATE TABLE IF NOT EXISTS traffic.hourly_aggregates_synthetic")
        assert synthetic == real
        assert "PRIMARY KEY (segment_id, hour_utc)" in synthetic
        text = WAVE3["019"].read_text()
        assert ("COMMENT ON TABLE traffic.hourly_aggregates_synthetic IS\n"
                "    'synthetic rollup — never a calibration input';") in text

    def test_the_numbers_are_the_pre_assigned_ones(self):
        names = sorted(p.name for p in MIGRATIONS.glob("01[6-9]_*.sql"))
        assert names == [p.name for _n, p in sorted(WAVE3.items())]
        assert not list(MIGRATIONS.glob("015_*emulation*"))  # 015 is WP-17's


# ── database: fresh apply + no-op re-apply on a throwaway database ─────────

_DSN = os.environ.get("VEER_TEST_PG_DSN", "")
_LIVE_PORT = ":56434"
db = pytest.mark.skipif(
    not _DSN or _LIVE_PORT in _DSN,
    reason="set VEER_TEST_PG_DSN to a SANDBOX Postgres; never the live :56434 store",
)

#: The catalog facts a re-apply must leave byte-identical.
_SQL_CATALOG = """
SELECT 'con:' || conrelid::regclass || ':' || conname || ':' || pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid IN ('emulator.emulation_runs'::regclass,
                   'events.enforcement_recommendations'::regclass,
                   'events.event_routes'::regclass,
                   'traffic.hourly_aggregates_synthetic'::regclass)
UNION ALL
SELECT 'col:' || attrelid::regclass || ':' || attname || ':' || format_type(atttypid, atttypmod)
       || ':' || attnotnull
FROM pg_attribute
WHERE attrelid IN ('events.event_routes'::regclass,
                   'traffic.hourly_aggregates_synthetic'::regclass)
  AND attnum > 0 AND NOT attisdropped
UNION ALL
SELECT 'idx:' || indexdef FROM pg_indexes
WHERE (schemaname, tablename) IN (('events', 'event_routes'),
                                  ('traffic', 'hourly_aggregates_synthetic'))
UNION ALL
SELECT 'comment:' || coalesce(obj_description('traffic.hourly_aggregates_synthetic'::regclass), '')
ORDER BY 1
"""


def _swap_db(dsn: str, name: str) -> str:
    return re.sub(r"/[^/?]+(\?|$)", f"/{name}\\1", dsn, count=1)


@db
def test_fresh_apply_then_noop_reapply():
    asyncpg = pytest.importorskip("asyncpg")

    async def run():
        name = "veer_mig_wp19_" + secrets.token_hex(4)
        admin = await asyncpg.connect(_DSN)
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
        except asyncpg.exceptions.InsufficientPrivilegeError:
            await admin.close()
            pytest.skip("the role cannot create a throwaway database")
        try:
            conn = await asyncpg.connect(_swap_db(_DSN, name))
            try:
                # 001..014 as they ship (simple-protocol scripts), then the
                # wave-3 files statement by statement — the naive splitter
                for path in sorted(MIGRATIONS.glob("0[01][0-9]_*.sql")):
                    if path.name[:3] <= "014":
                        await conn.execute(path.read_text())
                for _num, path in sorted(WAVE3.items()):
                    for stmt in _statements(path.read_text()):
                        await conn.execute(stmt)
                first = [r[0] for r in await conn.fetch(_SQL_CATALOG)]
                for _num, path in sorted(WAVE3.items()):  # re-apply: a no-op
                    for stmt in _statements(path.read_text()):
                        await conn.execute(stmt)
                second = [r[0] for r in await conn.fetch(_SQL_CATALOG)]
                assert first == second

                runs = await conn.fetchval(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'emulation_runs_status_check'"
                )
                assert "'interrupted'::text" in runs
                rules = await conn.fetchval(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'enforcement_recommendations_rule_id_check'"
                )
                assert "'R_STOLEN_SIGHTING'::text" in rules
                cols = {
                    r["attname"]: r["attnotnull"]
                    for r in await conn.fetch(
                        "SELECT attname, attnotnull FROM pg_attribute WHERE attrelid = "
                        "'events.event_routes'::regclass AND attnum > 0 AND NOT attisdropped"
                    )
                }
                assert {k: cols[k] for k in ("superseded_at", "variant", "created_by")} == {
                    "superseded_at": False, "variant": False, "created_by": False
                }
                assert any("WHERE (superseded_at IS NULL)" in x for x in first)
                assert "comment:synthetic rollup — never a calibration input" in first
                # the CHECKs bite: an unknown status / rule id is refused
                with pytest.raises(asyncpg.exceptions.CheckViolationError):
                    await conn.execute(
                        "INSERT INTO emulator.emulation_runs (spec_hash, spec, seed, "
                        "t0_anchor, status) VALUES ('h', '{}', 1, NOW(), 'paused')"
                    )
                await conn.execute(
                    "INSERT INTO emulator.emulation_runs (spec_hash, spec, seed, "
                    "t0_anchor, status) VALUES ('h', '{}', 1, NOW(), 'interrupted')"
                )
            finally:
                await conn.close()
        finally:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await admin.close()

    asyncio.run(run())


@db
def test_the_target_store_carries_016_to_019():
    """The store VEER_TEST_PG_DSN names (a migrated sandbox) has the four
    wave-3 contracts — skipped on a store that has not run migrate.py."""
    asyncpg = pytest.importorskip("asyncpg")

    async def run():
        conn = await asyncpg.connect(_DSN)
        try:
            ledger = {
                r["name"] for r in await conn.fetch("SELECT name FROM corridor.schema_migrations")
            }
            if not {p.name for p in WAVE3.values()} <= ledger:
                pytest.skip("016-019 not ledgered on this store (run scripts/migrate.py)")
            runs = await conn.fetchval(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'emulation_runs_status_check'"
            )
            rules = await conn.fetchval(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'enforcement_recommendations_rule_id_check'"
            )
            assert "'interrupted'::text" in runs and "'R_STOLEN_SIGHTING'::text" in rules
            assert await conn.fetchval(
                "SELECT to_regclass('traffic.hourly_aggregates_synthetic') IS NOT NULL"
            )
        finally:
            await conn.close()

    asyncio.run(run())
