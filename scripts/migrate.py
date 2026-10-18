#!/usr/bin/env python3
"""migrate.py — ordered, idempotent SQL migration runner for VVIP (C10-iv).

Why this exists: corridor-store migrations are mounted at
docker-entrypoint-initdb.d in compose, which Postgres ONLY executes against an
EMPTY volume. Existing installs never see 004-006. This runner applies
services/corridor-store/migrations/*.sql in lexical order over the live DSN,
recording each applied file in a corridor.schema_migrations ledger so re-runs
are no-ops and checksum drift is a hard error.

Usage:
    python migrate.py [--migrations DIR] [--dsn ...] [--dry-run] [--list]

Configuration:
    DSN resolution order: --dsn flag, then env VVIP_DATABASE_URL, then env
    DATABASE_URL. Credentials are NEVER hardcoded (env-only, per C12).

Security model (prescribed by the platform security review):
    - Python NEVER executes migration script text. Script execution is
      delegated entirely to psql via subprocess with an argv array (no shell):
      psql <dsn> --single-transaction -v ON_ERROR_STOP=1 -f <file>
      psql natively handles multi-statement grammar, dollar-quoted bodies and
      functions — the Postgres extended-query (parameterized) protocol cannot
      express multi-statement scripts at all.
    - psycopg is used ONLY for ledger reads/writes, each an inline string
      literal with %s parameter binding (psycopg placeholders); no
      f-string/format/concatenated SQL exists anywhere in this file.
    - Migration candidates must match ^\\d{3}_[A-Za-z0-9_]+\\.sql$ AND resolve
      inside the migrations directory (resolve + is_relative_to); anything
      else is rejected before psql runs.
    - The sha256 of each applied file is ledgered; drift on a later run is a
      hard error (exit 3).

psql resolver chain (host-mode first, per gate):
    1. psql on PATH (shutil.which)
    2. `docker exec -T vvip-postgis psql -U corridor_admin -d corridor_db ...`
       (service/container name and role are repo constants from
       infra/compose.yml — not secrets; the password stays env-only)
    3. hard nonzero exit with an actionable message

Requires: psycopg >= 3.2 (ledger; provisioned into VVIP/models/.venvs/migrate
by infra/scripts/serve_models.sh build) + psql or a running corridor-store
container for script application.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import psycopg
except ImportError:  # pragma: no cover - operational guard
    print(
        "ERROR: psycopg is required for the ledger. Run: "
        "./infra/scripts/serve_models.sh build (provisions "
        "VVIP/models/.venvs/migrate with psycopg[binary])",
        file=sys.stderr,
    )
    raise

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MIGRATIONS = REPO_ROOT / "services" / "corridor-store" / "migrations"

# Repo-owned constants from infra/compose.yml (service identity and role —
# NOT credentials; POSTGIS_PASSWORD stays env-only).
# Overridable so a parallel clone fleet (compose.argus-override.yml,
# container vvip-argus-postgis) migrates its OWN store, not the original.
POSTGIS_CONTAINER = os.environ.get("POSTGIS_CONTAINER", "vvip-postgis")
POSTGIS_USER = "corridor_admin"
POSTGIS_DB = "corridor_db"

# Strict grammar for repo-owned migration assets (lexical NNN_name.sql).
_NAME_RE = re.compile(r"^\d{3}_[A-Za-z0-9_]+\.sql$", re.IGNORECASE)

ON_ERROR_STOP = "ON_ERROR_STOP=1"


def resolve_dsn(flag_value: str | None) -> str:
    dsn = flag_value or os.environ.get("VVIP_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print(
            "ERROR: no DSN. Pass --dsn or set VVIP_DATABASE_URL / DATABASE_URL "
            "(credentials stay env-only).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return dsn


def discover(migrations_dir: Path) -> list[tuple[str, Path, str]]:
    """Lexically ordered (name, path, sha256) for NNN_description.sql files.

    Every candidate must resolve INSIDE migrations_dir — anything outside
    (symlink tricks included) is rejected.
    """
    found: list[tuple[str, Path, str]] = []
    resolved_root = migrations_dir.resolve()
    for path in sorted(resolved_root.iterdir()):
        if not path.is_file() or not _NAME_RE.match(path.name):
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(resolved_root):
            print(f"ERROR: {path} resolves outside the migrations directory — rejected.",
                  file=sys.stderr)
            raise SystemExit(2)
        checksum = hashlib.sha256(resolved.read_bytes()).hexdigest()
        found.append((path.name, resolved, checksum))
    if not found:
        print(f"ERROR: no *.sql migrations found in {resolved_root}", file=sys.stderr)
        raise SystemExit(2)
    return found


def apply_with_host_psql(dsn: str, path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["psql", dsn, "--single-transaction", "-v", ON_ERROR_STOP, "-f", str(path)],
        check=False, capture_output=True, text=True,
    )


def apply_with_container_psql(path: Path) -> subprocess.CompletedProcess[str]:
    with path.open("rb") as script:
        return subprocess.run(
            ["docker", "exec", "-i", POSTGIS_CONTAINER,
             "psql", "-U", POSTGIS_USER, "-d", POSTGIS_DB,
             "--single-transaction", "-v", ON_ERROR_STOP],
            stdin=script, check=False, capture_output=True, text=True,
        )


def apply_migration(dsn: str, path: Path) -> None:
    """Apply one migration file via psql (never via Python string execution)."""
    if shutil.which("psql"):
        result = apply_with_host_psql(dsn, path)
        transport = "psql"
    else:
        result = apply_with_container_psql(path)
        transport = f"docker exec {POSTGIS_CONTAINER} psql"
    if result.returncode != 0:
        print(f"ERROR: {transport} failed for {path.name} "
              f"(exit {result.returncode}):\n{result.stderr}", file=sys.stderr)
        raise SystemExit(4)


def ensure_ledger(cur: psycopg.Cursor) -> None:
    """Create schema + ledger table if missing (inline literal DDL)."""
    cur.execute("CREATE SCHEMA IF NOT EXISTS corridor")
    cur.execute("CREATE TABLE IF NOT EXISTS corridor.schema_migrations (name TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), duration_ms INTEGER)")


def ledger_state(cur: psycopg.Cursor) -> dict[str, str]:
    """Return {applied_name: checksum} (inline literal SELECT)."""
    cur.execute("SELECT name, checksum FROM corridor.schema_migrations")
    return {str(name): str(checksum) for name, checksum in cur.fetchall()}


def record_applied(cur: psycopg.Cursor, name: str, checksum: str, duration_ms: int) -> None:
    """Ledger insert — inline literal SQL, fully parameter-bound."""
    cur.execute("INSERT INTO corridor.schema_migrations (name, checksum, duration_ms) VALUES (%s, %s, %s) ON CONFLICT (name) DO NOTHING", (name, checksum, duration_ms))


def ledger_count(cur: psycopg.Cursor) -> int:
    cur.execute("SELECT count(*) FROM corridor.schema_migrations")
    return int(cur.fetchone()[0])  # type: ignore[index]


def main() -> int:
    parser = argparse.ArgumentParser(description="Idempotent ledgered SQL migration runner")
    parser.add_argument("--migrations", default=str(DEFAULT_MIGRATIONS),
                        help="migrations directory (default: services/corridor-store/migrations)")
    parser.add_argument("--dsn", default=None,
                        help="PostgreSQL DSN (default: env VVIP_DATABASE_URL, then DATABASE_URL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show plan without applying")
    parser.add_argument("--list", action="store_true", help="list ledger + files and exit")
    args = parser.parse_args()

    dsn = resolve_dsn(args.dsn)
    migrations_dir = Path(args.migrations).resolve()
    if not migrations_dir.is_dir():
        print(f"ERROR: migrations dir not found: {migrations_dir}", file=sys.stderr)
        return 2
    files = discover(migrations_dir)

    with psycopg.connect(dsn) as conn:  # type: psycopg.Connection
        with conn.cursor() as cur:
            ensure_ledger(cur)
            conn.commit()
            applied = ledger_state(cur)

        if args.list or args.dry_run:
            print(f"{'migration':40} {'status':10} checksum")
            for name, path, checksum in files:
                status = "applied" if name in applied else "pending"
                ok = name not in applied or applied[name] == checksum
                print(f"{name:40} {status:10} {'OK ' if ok else 'DRIFT'} {checksum[:12]}")
            return 0

        applied_now = 0
        for name, path, checksum in files:
            if name in applied:
                if applied[name] != checksum:
                    print(
                        f"ERROR: checksum drift for {name}: ledger "
                        f"{applied[name][:12]} != disk {checksum[:12]}. Applied "
                        "migrations are immutable — investigate before proceeding.",
                        file=sys.stderr,
                    )
                    return 3
                print(f"[migrate] skip  {name} (applied)")
                continue
            started = time.perf_counter()
            apply_migration(dsn, path)
            duration_ms = int((time.perf_counter() - started) * 1000)
            with conn.cursor() as cur:
                record_applied(cur, name, checksum, duration_ms)
            conn.commit()  # ledger write commits only after psql succeeded
            applied_now += 1
            print(f"[migrate] apply {name} ({duration_ms} ms)")

        with conn.cursor() as cur:
            total = ledger_count(cur)
        print(f"[migrate] done: {applied_now} applied, {total} in ledger")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
