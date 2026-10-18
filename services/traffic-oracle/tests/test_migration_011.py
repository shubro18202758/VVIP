"""Migration 011 grammar + structure tests (pure python, no DB).

Mirrors the corridor-store grammar contract (007 header, and the
statement-splitting discipline of embedding-service's
test_migration_pgvector): migration files are applied BOTH by psql and by
naive ``;``-splitters, so they must stay in the plain-statement subset —
no ``;`` inside string literals, no dollar-quoted bodies — and be
idempotent (CREATE ... IF NOT EXISTS everywhere here).

An optional live-apply section (skipped unless the fleet DB is reachable)
proves 011 applies cleanly on the real ledger and that re-running it is a
no-op — that is the property scripts/migrate.py depends on.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO / "services" / "corridor-store" / "migrations"
M011 = MIGRATIONS / "011_city_coordination.sql"

DISPATCH_TABLES = ("corridor.dispatches", "corridor.dispatch_events",
                   "corridor.signal_program_history")


def _split_statements(sql_text: str) -> list[str]:
    """The naive splitter the tests/embedding-service path uses."""
    stripped = "\n".join(
        line for line in sql_text.splitlines() if not line.lstrip().startswith("--")
    )
    return [s.strip() for s in stripped.split(";") if s.strip()]


# ─── Grammar ─────────────────────────────────────────────────────────────────


def test_011_exists_and_matches_runner_name_pattern():
    assert M011.exists()
    assert re.fullmatch(r"\d{3}_[A-Za-z0-9_]+\.sql", M011.name)


def test_011_has_no_semicolons_inside_string_literals():
    """A naive `;`-split must not corrupt any literal."""
    text = M011.read_text()
    # Every statement, when split naively, must re-join to statements that
    # each end cleanly — the practical check is that no single-quoted
    # literal spans a split boundary: scan quotes per line-tracked body.
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )
    in_literal = False
    for ch_index, ch in enumerate(body):
        if ch == "'":
            in_literal = not in_literal
        elif ch == ";" and in_literal:
            pytest.fail("semicolon inside a string literal breaks naive splitters")


def test_011_has_no_dollar_quoted_bodies():
    text = M011.read_text()
    assert "$$" not in text
    assert "$func" not in text


def test_011_statements_are_idempotent_creates():
    statements = _split_statements(M011.read_text())
    assert len(statements) >= 6
    for stmt in statements:
        head = stmt.lstrip().upper()
        assert head.startswith("CREATE"), (
            "011 must be additive-only CREATE ... IF NOT EXISTS DDL, got: "
            + head[:60]
        )
        assert "IF NOT EXISTS" in head or "CREATE INDEX IF NOT EXISTS" in stmt.upper()


def test_011_declares_the_three_tables_with_checks():
    text = M011.read_text()
    for table in DISPATCH_TABLES:
        assert ("CREATE TABLE IF NOT EXISTS " + table) in text, table
    # The agency CHECK keeps Jan Rakshak in the closed set
    assert "'jan_rakshak'" in text
    # Dispatch status lifecycle mirrors DISPATCH_TRANSITIONS
    for status in ("'requested'", "'approved'", "'deferred'", "'en_route'",
                   "'on_scene'", "'completed'", "'cancelled'"):
        assert status in text, status
    # History kinds
    assert "'snapshot'" in text and "'restore'" in text
    # 006-compatible signal program CHECKs on the history table
    assert "BETWEEN 40 AND 240" in text
    assert "jsonb_typeof(phases) = 'array'" in text


def test_011_disjoint_from_010_tables():
    """C4: this stream owns dispatches/history; 010 (watchlist-anpr) owns
    vehicles/sightings.  011 must not create or alter their tables."""
    text = M011.read_text()
    for theirs in ("corridor.vehicles", "corridor.vehicle_sightings",
                   "corridor.vehicle_earmarks"):
        assert theirs not in text, theirs
    m010 = MIGRATIONS / "010_vehicles.sql"
    if m010.exists():
        theirs_text = m010.read_text()
        for mine in DISPATCH_TABLES:
            assert ("TABLE IF NOT EXISTS " + mine) not in theirs_text, mine


def test_011_does_not_touch_applied_001_009():
    """No ALTER/INSERT against the ledgered (checksum-frozen) files' tables."""
    text = M011.read_text()
    for frozen in ("events.corridor_events", "adaptive.situation_log",
                   "simulation.runs", "emulator.emulation_runs"):
        assert ("ALTER TABLE " + frozen) not in text
    # FK references are fine (read-only relationships), drops are not
    assert "DROP CONSTRAINT" not in text


# ─── Optional live apply (fleet reachable) ───────────────────────────────────


MIGRATE_PY = REPO / "models" / ".venvs" / "migrate" / "bin" / "python"


def _migrate_python() -> str | None:
    """The ledger runner needs psycopg (models/.venvs/migrate per its header)."""
    if MIGRATE_PY.exists():
        return str(MIGRATE_PY)
    return None


def _live_apply_available() -> bool:
    if _migrate_python() is None:
        return False
    # Credentials stay env-only (C5/C12): never a DSN literal in tests.
    if not os.environ.get("VVIP_DATABASE_URL"):
        return False
    try:
        probe = subprocess.run(
            ["docker", "exec", "vvip-postgis", "pg_isready", "-U", "corridor_admin",
             "-d", "corridor_db"],
            capture_output=True, timeout=10,
        )
        return probe.returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _live_apply_available(), reason="live postgis not reachable")
def test_011_applies_cleanly_on_live_ledger_and_reruns_noop():
    """The property scripts/migrate.py depends on: clean apply + no-op rerun."""
    py = _migrate_python()
    env = dict(os.environ)  # carries VVIP_DATABASE_URL (env-only credential)
    run = subprocess.run(
        [py, str(REPO / "scripts" / "migrate.py")],
        capture_output=True, text=True, timeout=300, cwd=str(REPO), env=env,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    # Re-run must be a no-op (ledger sees 011 applied)
    rerun = subprocess.run(
        [py, str(REPO / "scripts" / "migrate.py")],
        capture_output=True, text=True, timeout=300, cwd=str(REPO), env=env,
    )
    assert rerun.returncode == 0, rerun.stderr[-2000:]
    exists = subprocess.run(
        ["docker", "exec", "vvip-postgis", "psql", "-U", "corridor_admin",
         "-d", "corridor_db", "-At", "-c",
         "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'corridor' AND table_name IN ('dispatches', 'dispatch_events', 'signal_program_history')"],  # noqa: E501
        capture_output=True, text=True, timeout=30,
    )
    assert exists.stdout.strip() == "3", exists.stdout + exists.stderr
