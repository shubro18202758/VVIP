#!/usr/bin/env bash
# repetition_gate.sh — the CONSISTENCY-UNDER-REPETITION gate for "God's Eye
# Morning" (showcase_run.sh) against the ARGUS clone stack.
#
# CLAIM PROVEN: run the showcase N times (default 2) from an identical
# starting state against the live argus fleet, and the terminal database
# state the showcase's own phase-8 assertions are judged over is IDENTICAL
# across rounds — after normalizing exactly the volatility classes that are
# provably not system output (wall clock, the showcase harness's per-run
# sweep seed, gen_random_uuid identifiers, BIGSERIAL sequences, one
# in-process counter, and engine-loop row multiplicity).  Every
# normalization is printed with its justification on every run (the
# manifest); anything else that differs FAILS the gate with the differing
# rows.  Real nondeterminism is reported, never hidden.
#
# Terminal-state tables (anchored on showcase_run.sh phase 8's assertion
# queries, lines 854-894): adaptive.situation_log,
# adaptive.param_overrides, events.corridor_events, events.event_phases —
# plus the tables carrying the pursuit-chain / Jan-Rakshak / earmark
# terminal state the showcase asserts through its APIs (phases 3-6):
# events.event_routes, events.enforcement_recommendations,
# corridor.vehicle_earmarks, corridor.vehicle_sightings, corridor.dispatches,
# corridor.dispatch_events, corridor.vehicles.
#
# The two adaptive audit tables are compared through the SAME shapes
# phase 8 asserts (band classes seen, a transition recorded, an
# AMBULANCE_DELAY escalation recorded, and the full param-decision
# documents), because their row-level inputs are wall-clock SAMPLES: the
# engine loop polls the latest inputs doc every 5 s
# (traffic_oracle/adaptive/engine_loop.py:29) while the flagship tape
# overwrites that doc every ~0.33 s of wall time (tick_sec 10 at
# clock_scale 30) — WHICH doc a tick samples is timing alignment, so
# multiplicity AND sampled-row identity are volatile by construction
# (full reasoning in the manifest the gate prints on every run).
#
# Mechanism:
#   0. guard the ORIGINAL stack read-only (argus_full_smoke.sh pattern);
#      preflight the argus fleet + host models, fail fast with fixes.
#   1. BASELINE snapshot (documented SQL projection of every showcase-
#      writable table, parameter-bound via psycopg) into the workdir.
#   2. each round: restore-to-baseline (idempotent, verified), reset the
#      argus traffic-oracle process (its in-memory adaptive state —
#      hysteresis bands, observation counters, adaptive/service.py —
#      cannot be restored by any DB snapshot; restarting the ARGUS oracle
#      only, never the original stack's), converge-wait, run
#      showcase_run.sh with the argus env overrides, capture the CANONICAL
#      projection of the round's terminal state, restore-to-baseline
#      again (the DB is left as found).
#   3. compare projections across rounds (round 1 vs every other round);
#      exit 0 only when all identical AND the showcase itself exited 0 in
#      every round AND the original stack stayed untouched.
#
# Usage:
#     bash VVIP/scripts/repetition_gate.sh [N]        # N rounds, default 2
#     bash VVIP/scripts/repetition_gate.sh --fast     # offline selftest:
#         proves the comparator, the normalizer and the FAIL path on
#         fixtures WITHOUT running the showcase or touching docker/db.
#
# Env:   endpoints/containers use the ${VAR:-default} pattern of
#        showcase_run.sh / argus_full_smoke.sh, with the ARGUS stack as
#        defaults (oracle :8091, brain :8092, emulator :8090, deck :5273,
#        postgis :56434 in container vvip-argus-postgis, db corridor_db).
#        POSTGIS_PASSWORD defaults to the compose dev credential
#        (dev_changeme) exactly like showcase_run.sh:194.
#        CONVERGE_TIMEOUT_SEC bounds the post-restart health wait.
#        Expect tens of minutes for the full 2-round gate.
#
# Security: every URL below is an internal endpoint (an env-overridable
# config constant of this script, http scheme, loopback host) — none comes
# from external input.  All SQL is executed by the embedded gate_py.py via
# psycopg with %s parameter binding; the only string-built SQL fragments
# use table/column names that are constants OF THIS SCRIPT (the
# scripts/migrate.py pattern).  Docker use: read-only inspection of both
# stacks; `docker restart` / `docker compose up -d` of the ARGUS
# traffic-oracle only.  The original stack (project `vvip`, the shared
# vvip-*/veer-* image tags without the -argus suffix) is snapshotted
# read-only before and after, and any drift fails the gate.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VVIP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INFRA_DIR="$VVIP_DIR/infra"

# ── internal endpoints (env-overridable constants, loopback argus) ──────────
ORACLE_URL="${ORACLE_URL:-http://127.0.0.1:${ORACLE_PORT:-8091}}"
BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:${BRAIN_PORT:-8092}}"
EMULATOR_URL="${EMULATOR_URL:-http://127.0.0.1:${EMULATOR_PORT:-8090}}"
DECIDER_URL="${DECIDER_URL:-http://127.0.0.1:${DECIDER_PORT:-8083}}"
LLAMA_URL="${LLAMA_URL:-http://127.0.0.1:${LLAMA_PORT:-8084}}"
EMBED_URL="${EMBED_URL:-http://127.0.0.1:${EMBED_PORT:-8085}}"
DECK_URL="${DECK_URL:-http://127.0.0.1:${DECK_PORT:-5273}}"

# ── fleet addressing (argus compose project) ────────────────────────────────
COMPOSE_PROJECT="${COMPOSE_PROJECT:-vvip-argus}"
COMPOSE_FILES="${COMPOSE_FILES:--f compose.yml -f compose.gb10-override.yml -f compose.argus-override.yml}"
PG_CONTAINER="${POSTGIS_CONTAINER:-${COMPOSE_PROJECT}-postgis}"
VALKEY_CONTAINER="${VALKEY_CONTAINER:-${COMPOSE_PROJECT}-valkey}"
NATS_CONTAINER="${NATS_CONTAINER:-${COMPOSE_PROJECT}-nats}"
ORACLE_CONTAINER="${ORACLE_CONTAINER:-${COMPOSE_PROJECT}-traffic-oracle}"
BRAIN_CONTAINER="${BRAIN_CONTAINER:-${COMPOSE_PROJECT}-convoy-brain}"
INGRESS_CONTAINER="${INGRESS_CONTAINER:-${COMPOSE_PROJECT}-signal-ingress}"
DECK_CONTAINER="${DECK_CONTAINER:-${COMPOSE_PROJECT}-command-deck}"

PG_HOST="${POSTGIS_HOST:-127.0.0.1}"
PG_PORT="${POSTGIS_PORT:-56434}"
PG_USER="${POSTGIS_USER:-corridor_admin}"
PG_DB="${POSTGIS_DB:-corridor_db}"
PG_PASSWORD="${POSTGIS_PASSWORD:-dev_changeme}"

GATE_PY_BIN="${GATE_PY_BIN:-$VVIP_DIR/models/.venvs/migrate/bin/python}"
CONVERGE_TIMEOUT_SEC="${CONVERGE_TIMEOUT_SEC:-180}"
# The sweep seed is PINNED for every round (showcase_run.sh reads SWEEP_SEED
# from env; its default stays fresh-per-run).  Pinning removes the harness's
# per-run randomization from the compared state entirely: identical tapes →
# identical sighting uuids/positions/speeds, identical rule firings — so the
# gate compares those values RAW instead of tokenizing them.  (Found live
# 2026-10-18: with a fresh seed per round, the read set's segment
# attribution and rule firings differed round to round.)
GATE_SWEEP_SEED="${GATE_SWEEP_SEED:-20261105}"

# scheme guard: http/https only (config constants; rejects anything else early)
url_scheme_ok() { case "$1" in http://*|https://*) return 0 ;; *) return 1 ;; esac; }
for u in "$ORACLE_URL" "$BRAIN_URL" "$EMULATOR_URL" "$DECIDER_URL" "$LLAMA_URL" "$EMBED_URL" "$DECK_URL"; do
    url_scheme_ok "$u" || { echo "FATAL: endpoint not http/https: $u" >&2; exit 2; }
done

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; CYAN=$'\033[0;36m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
log()  { printf '%s[repgate]%s %s\n' "$CYAN" "$RESET" "$*"; }
ok()   { printf '%s[ ok ]%s %s\n' "$GREEN" "$RESET" "$*"; }
bad()  { printf '%s[FAIL]%s %s\n' "$RED" "$RESET" "$*"; }
die()  { bad "$1"; printf '     FIX: %s\n' "$2" >&2; exit 2; }

hcode() { curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-10}" "$1" 2>/dev/null; }
health(){ curl -s --max-time "${2:-10}" "$1" 2>/dev/null; }

# ── arguments ───────────────────────────────────────────────────────────────
ROUNDS=2; FAST=false
for a in "$@"; do
    case "$a" in
        --fast) FAST=true ;;
        ''|*[!0-9]*) die "bad argument: '$a' (want a round count >= 2, and/or --fast)" "usage: bash VVIP/scripts/repetition_gate.sh [N] [--fast]" ;;
        *) ROUNDS="$a" ;;
    esac
done
[ "$ROUNDS" -ge 2 ] || die "rounds must be >= 2 (got $ROUNDS): one round cannot be compared against anything" "usage: bash VVIP/scripts/repetition_gate.sh [N] [--fast]"

WORKROOT="$VVIP_DIR/showcase/repetition"
mkdir -p "$WORKROOT"
WORKDIR="$WORKROOT/$(date -u +%Y%m%dT%H%M%S)Z-r${ROUNDS}-$$"
mkdir -p "$WORKDIR"

# ════════════════════════════════════════════════════════════════════════════
# The projection/restore engine (pure python, psycopg parameter-bound).
# Written into the workdir at runtime so the gate stays one file.
# ════════════════════════════════════════════════════════════════════════════
GATE_PY="$WORKDIR/gate_py.py"
cat > "$GATE_PY" <<'GATEPYEOF'
"""gate_py.py — snapshot / restore / canonical-projection / compare engine
for repetition_gate.sh, plus the offline --fast selftest.

All SQL is psycopg with %s parameter binding.  The only SQL built from
strings uses table/column names that are constants OF THIS FILE (the
scripts/migrate.py pattern); every value is bound.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid as _uuid
from pathlib import Path

import psycopg
from psycopg import sql as pgsql

DSN = (f"postgresql://{os.environ.get('PG_USER', 'corridor_admin')}:"
       f"{os.environ.get('PG_PASSWORD', 'dev_changeme')}@"
       f"{os.environ.get('PG_HOST', '127.0.0.1')}:"
       f"{os.environ.get('PG_PORT', '56434')}/"
       f"{os.environ.get('PG_DB', 'corridor_db')}")

FLAGSHIP_USE_CASE = "rath_yatra_monsoon_ambulance"
FLAG_SEED = 202506271

# Deterministic flagship movement/event id — IDENTICAL computation to
# showcase_run.sh:121 (uuid5 over use_case|seed).  Deliberately NOT
# normalized in the projection: it must match across rounds.
_NS = _uuid.uuid5(_uuid.NAMESPACE_URL, "vvip-event-emulator")
FLAGSHIP_ID = str(_uuid.uuid5(_NS, f"{FLAGSHIP_USE_CASE}|{FLAG_SEED}"))

# ── table registry ──────────────────────────────────────────────────────────
# mode 'distinct' → compared as a SET of canonical rows (tick-driven
#                    multiplicity normalized; see MANIFEST)
# mode 'multiset' → compared with multiplicity (row count is structurally
#                    determined: one row per showcase entity)
PROJECTED = {
    "events.corridor_events":               {"pk": ("event_id",),         "mode": "multiset"},
    "events.event_phases":                  {"pk": ("phase_id",),         "mode": "multiset"},
    "events.event_routes":                  {"pk": ("route_id",),         "mode": "multiset"},
    "events.enforcement_recommendations":   {"pk": ("recommendation_id",),"mode": "multiset"},
    "corridor.vehicle_earmarks":            {"pk": ("earmark_id",),       "mode": "multiset"},
    "corridor.vehicle_sightings":           {"pk": ("sighting_id",),      "mode": "multiset"},
    "corridor.dispatches":                  {"pk": ("dispatch_id",),      "mode": "multiset"},
    "corridor.dispatch_events":             {"pk": ("seq",),              "mode": "multiset"},
    "corridor.vehicles":                    {"pk": ("vehicle_id",),       "mode": "multiset"},
    # the two adaptive audit tables are projected through their phase-8
    # assertion shapes (see MANIFEST) — they are snapshot+restored like
    # everything else, and their FLAG-scope rows drive the audit_* lines.
    "adaptive.situation_log":               {"pk": ("log_id",),           "mode": "audit"},
    "adaptive.param_overrides":             {"pk": ("override_id",),      "mode": "audit"},
}
# Behavioural-cleanliness tables: snapshot + restored between rounds, not
# compared row-by-row (content embeds wall-clock delivery telemetry /
# ephemeral ledger state; none is a phase-8 assertion table).
RESTORE_ONLY = [
    "convoy.movements",
    "convoy.planned_routes",
    "emulator.emulation_runs",
    "simulation.runs",
    "corridor.signal_program_history",
    "corridor.signal_programs",
    "traffic.synthetic_observations",
    "traffic.anomaly_log",
    "traffic.incidents",
]
ALL_TABLES = list(PROJECTED) + RESTORE_ONLY

# Children before parents (FK ON DELETE CASCADE would also work, but the
# gate deletes explicitly in dependency order).
DELETE_ORDER = [
    "events.enforcement_recommendations",
    "corridor.dispatch_events",
    "corridor.dispatches",
    "events.event_phases",
    "events.event_routes",
    "events.corridor_events",
    "adaptive.situation_log",
    "adaptive.param_overrides",
    "corridor.vehicle_sightings",
    "corridor.vehicle_earmarks",
    "corridor.vehicles",
    "convoy.planned_routes",
    "convoy.movements",
    "emulator.emulation_runs",
    "simulation.runs",
    "corridor.signal_program_history",
    "corridor.signal_programs",
    "traffic.synthetic_observations",
    "traffic.anomaly_log",
    "traffic.incidents",
]
INSERT_ORDER = list(reversed(DELETE_ORDER))

RESTORE_PK = {
    "convoy.movements": ("movement_id",),
    "convoy.planned_routes": ("route_id",),
    "emulator.emulation_runs": ("run_id",),
    "simulation.runs": ("run_id",),
    "corridor.signal_program_history": ("history_id",),
    "corridor.signal_programs": ("junction_id",),
    "traffic.synthetic_observations": ("synthetic_id",),
    "traffic.anomaly_log": ("anomaly_id",),
    "traffic.incidents": ("incident_id",),
}

# geometry columns (snapshot as GeoJSON text, restore via
# ST_SetSRID/ST_GeomFromGeoJSON; SRID 4326 everywhere here — migrations
# 001/007/010).
GEO_COLS = {
    "corridor.vehicle_sightings": {"position": 4326},
    "convoy.movements": {"origin_geom": 4326, "destination_geom": 4326},
    "convoy.planned_routes": {"route_geom": 4326},
    "events.corridor_events": {"zone_geom": 4326},
}

# JSONB columns (values snapshotted as parsed JSON, restored as jsonb).
JSONB_HINTS = {"metadata", "inputs", "params", "evidence", "audit_tail",
               "request_payload", "closure_policy", "tape", "spec",
               "phases", "details", "outcome"}

ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")

# ── the normalized-column manifest (printed on EVERY run) ───────────────────
MANIFEST = [
    ("wall-clock TIMESTAMPTZ columns of every projected table "
     "(observed_at, published_at, created_at, updated_at, earmarked_at, "
     "retention_until, cleared_at, decided_at, approved_at, t, at, "
     "first_seen, last_seen, planned_start/end, actual_start/end, "
     "window_start/end)",
     "EXCLUDED from the canonical projection — the showcase is a "
     "wall-clock-driven live run: emulator frames fire at t0+t/clock_scale "
     "(event_emulator/runner.py:283-289), columns default NOW(), and the "
     "showcase re-polls with sleeps (showcase_run.sh:294-321, 378-388); "
     "the showcase's own assertions never compare timestamps"),
    ("sweep / pursuit event uuids (and their echoes as movement subjects, "
     "FK references and audit movement_ids)",
     "relabeled to tokens SWEEP / PURSUIT — the pursuit id is "
     "gen_random_uuid (traffic_oracle/events/__init__.py:176); the sweep id "
     "is uuid5 over use_case|seed (event_emulator/tape.py:58-65) and would "
     "be stable across rounds BECAUSE the gate pins the sweep seed, but it "
     "is tokenized anyway so a seed-pinning regression reads as content, "
     "not as noise"),
    ("the FLAGSHIP event id (uuid5 over the FIXED seed 202506271)",
     "NOT normalized — deterministic (showcase_run.sh:119-121); a mismatch "
     "across rounds is a real failure the gate must catch"),
    ("corridor.vehicle_sightings.sighting_id",
     "relabeled to SIGHTING:{camera_id}:{plate} (one read per camera/plate "
     "pair, event_emulator/tape.py:279) — uuid5 over the per-run seed "
     "(tape.py:68-74, migration 010)"),
    ("gen_random_uuid surrogate ids: earmark_id, recommendation_id, "
     "dispatch_id, phase_id, route_id, vehicle_id",
     "projected through natural keys (plate; rule_id + sighting token; "
     "agency + unit_id; event token + rank) — column defaults of "
     "migrations 007/010/011; re-created fresh each round after the "
     "baseline restore"),
    ("BIGSERIAL surrogates: situation_log.log_id, "
     "param_overrides.override_id, dispatch_events.seq",
     "EXCLUDED — sequence values survive DELETE (the restore does not and "
     "must not reset sequences), so they differ across rounds by "
     "construction"),
    ("adaptive.param_overrides.evidence.observation_count",
     "replaced by <N> — an in-process observation counter carried across "
     "rounds in the oracle's memory (traffic_oracle/adaptive/service.py "
     "observe(): state.observation_count += 1); a DB restore cannot reset "
     "it"),
    ("the sweep SEED itself (and every seed-rendered value: sighting "
     "speeds/headings/positions, metadata.seed, the recommendation free "
     "text, evidence speed numerics)",
     "NOT normalized — the gate PINS the sweep seed by exporting a fixed "
     "SWEEP_SEED to showcase_run.sh (whose own default stays fresh-per-run, "
     "showcase_run.sh SWEEP_SEED block), so migration 009's 'same spec+seed "
     "-> identical tape' guarantee makes all of these byte-identical across "
     "rounds and they are compared RAW (found live 2026-10-18: with a fresh "
     "seed per round, read positions attributed to different segments and "
     "different rules fired — real harness randomization, removed at the "
     "source rather than normalized away)"),
    ("ISO-8601 strings inside JSONB (evidence.t, window echoes, "
     "audit_tail[].at)",
     "replaced by <TS> — wall-clock echoes written verbatim into evidence "
     "(traffic_oracle/enforcement/rules.py:196 and the audit-entry "
     "constructor)"),
    ("uuid echoes inside JSONB metadata/evidence (vehicle_id, "
     "recommendation_id, event_id, sighting_id)",
     "replaced by <UUID-ECHO> or the corresponding natural-key token — "
     "gen_random_uuid values re-created per round"),
    ("adaptive.situation_log / adaptive.param_overrides ROW CONTENT and "
     "MULTIPLICITY (the audit tables)",
     "compared through the phase-8 assertion SHAPES: the set of situation "
     "classes seen, the existence of a transitioned row, the existence of "
     "an AMBULANCE_DELAY escalation row, and the DISTINCT full param "
     "documents (params/source/shadow/escalation evidence) — because the "
     "row population is wall-clock SAMPLED: the engine loop classifies "
     "the latest inputs doc every 5 s (engine_loop.py:29) while the "
     "flagship tape overwrites that doc every ~0.33 s of wall time "
     "(tick_sec 10 at clock_scale 30, showcase_run.sh:129-131), so which "
     "doc a tick samples — and how many rows land — is timing alignment, "
     "exactly like a timestamp; showcase_run.sh:854-874 itself asserts "
     "only deltas >= 1, band classes, transitions and AMBULANCE_DELAY "
     "presence"),
    ("events.enforcement_recommendations rows with "
     "rule_id='R_SIGNAL_VIOLATION'",
     "EXCLUDED from the projection — the rule fires iff the read's t "
     "(anchored at the run's wall-clock t0) falls inside the referenced "
     "signal's red phase (enforcement/rules.py phase_state_at over "
     "sighting.t), so the row's EXISTENCE is a wall-clock lottery even with "
     "the seed pinned; the showcase never asserts it (its chain asserts "
     "R_SPD_SEGMENT only).  R_SPD_SEGMENT / R_CLOSED_CORRIDOR_ENTRY rows "
     "are compared in full"),
    ("row MULTIPLICITY in every other projected table",
     "NOT normalized — compared with multiplicity (one row per showcase "
     "entity is structurally determined: one event per tape, one phase "
     "per event, one read per camera/plate, one dispatch per unit, one "
     "earmark per plate per round)"),
]

def print_manifest(fh=sys.stdout):
    fh.write("normalized-column manifest (every entry is a provably-volatile "
             "class; everything else is compared byte-for-byte):\n")
    for i, (what, why) in enumerate(MANIFEST, 1):
        fh.write(f"  {i:2d}. {what}\n      because: {why}\n")

# ── snapshot / restore ──────────────────────────────────────────────────────

def table_columns(conn, table):
    schema, name = table.split(".", 1)
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position", (schema, name)).fetchall()
    if not rows:
        raise SystemExit(f"gate_py: table {table} not found in "
                         f"{os.environ.get('PG_DB', 'corridor_db')}")
    return [r[0] for r in rows]

def select_expr(table, col):
    if col in GEO_COLS.get(table, {}):
        return f'ST_AsGeoJSON("{col}"::geometry, 9)::text AS "{col}"'
    return f'"{col}"'

def fetch_table(conn, table, cols):
    stmt = pgsql.SQL("SELECT {} FROM {}").format(
        pgsql.SQL(", ").join(pgsql.SQL(select_expr(table, c)) for c in cols),
        pgsql.SQL(table))
    return [list(r) for r in conn.execute(stmt).fetchall()]

def jsonable(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    return str(v)  # datetime / uuid / Decimal / memoryview leftovers

def cmd_snapshot(workdir):
    snap = {"flagship_id": FLAGSHIP_ID, "tables": {}}
    with psycopg.connect(DSN) as conn:
        for table in ALL_TABLES:
            cols = table_columns(conn, table)
            rows = fetch_table(conn, table, cols)
            snap["tables"][table] = {"cols": cols,
                                     "rows": [jsonable(r) for r in rows]}
    path = Path(workdir) / "baseline.json"
    path.write_text(json.dumps(snap, sort_keys=True))
    total = sum(len(t["rows"]) for t in snap["tables"].values())
    print(f"[gate_py] baseline snapshot: {len(ALL_TABLES)} tables, "
          f"{total} rows -> {path}")
    for table in ALL_TABLES:
        print(f"[gate_py]   {table:42} {len(snap['tables'][table]['rows']):6} rows")

def pk_cols_of(table):
    return PROJECTED[table]["pk"] if table in PROJECTED else RESTORE_PK[table]

def cmd_restore(workdir):
    snap = json.loads((Path(workdir) / "baseline.json").read_text())
    report = []
    with psycopg.connect(DSN) as conn:
        conn.autocommit = False
        cur = conn.cursor()
        for table in DELETE_ORDER:
            meta = snap["tables"][table]
            cols = meta["cols"]
            pk_cols = pk_cols_of(table)
            pk_idx = [cols.index(c) for c in pk_cols]
            base = {tuple(jsonable(r[i]) for i in pk_idx): r for r in meta["rows"]}
            now = {tuple(jsonable(r[i]) for i in pk_idx): jsonable(r)
                   for r in fetch_table(conn, table, cols)}
            to_delete = [k for k in now if k not in base]
            to_revert = [k for k in now if k in base and now[k] != base[k]]
            to_insert = [k for k in base if k not in now]

            del_sql = pgsql.SQL(
                "DELETE FROM {} WHERE " + " AND ".join(["{} = %s"] * len(pk_cols))
            ).format(*([pgsql.SQL(table)] +
                       [pgsql.Identifier(c) for c in pk_cols]))
            for k in to_delete + to_revert:
                cur.execute(del_sql, tuple(k))

            if to_revert or to_insert:
                col_sql = pgsql.SQL(", ").join(pgsql.Identifier(c) for c in cols)
                val_parts = []
                for c in cols:
                    if c in GEO_COLS.get(table, {}):
                        val_parts.append("ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)")
                    elif c in JSONB_HINTS:
                        val_parts.append("%s::jsonb")
                    else:
                        val_parts.append("%s")
                ins_sql = (
                    pgsql.SQL("INSERT INTO {} (").format(pgsql.SQL(table))
                    + col_sql
                    + pgsql.SQL(") VALUES (")
                    + pgsql.SQL(", ").join(pgsql.SQL(p) for p in val_parts)
                    + pgsql.SQL(") ON CONFLICT DO NOTHING")
                )
                for k in to_revert + to_insert:
                    r = base[k]
                    vals = []
                    for i, c in enumerate(cols):
                        v = r[i]
                        if c in JSONB_HINTS and v is not None:
                            vals.append(json.dumps(v))
                        else:
                            vals.append(v)
                    cur.execute(ins_sql, tuple(vals))
            if to_delete or to_revert or to_insert:
                report.append((table, len(to_delete), len(to_revert), len(to_insert)))
        conn.commit()

        # verify: every scoped table is EXACTLY back at baseline
        drift = []
        for table in ALL_TABLES:
            meta = snap["tables"][table]
            cols = meta["cols"]
            pk_cols = pk_cols_of(table)
            pk_idx = [cols.index(c) for c in pk_cols]
            base = {tuple(jsonable(r[i]) for i in pk_idx): r for r in meta["rows"]}
            now = {tuple(jsonable(r[i]) for i in pk_idx): jsonable(r)
                   for r in fetch_table(conn, table, cols)}
            if now != base:
                drift.append(table)
        if drift:
            raise SystemExit(f"gate_py: restore VERIFY failed — still "
                             f"differ from baseline: {drift}")
    for table, d, rv, i in report:
        print(f"[gate_py] restore {table:42} -{d} deleted, {rv} reverted, "
              f"+{i} re-inserted")
    if not report:
        print("[gate_py] restore: state already at baseline (no-op)")
    print("[gate_py] restore: verified current == baseline for all scoped tables")

def cmd_check():
    with psycopg.connect(DSN) as conn:
        for table in ALL_TABLES:
            got = conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]
            if got is None:
                raise SystemExit(f"gate_py: table {table} missing — run "
                                 f"scripts/migrate.py against the argus store")
        print(f"[gate_py] check: all {len(ALL_TABLES)} scoped tables present; "
              f"flagship id {FLAGSHIP_ID}")

# ── canonical projection ────────────────────────────────────────────────────

def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))

def norm_jsonb(value, extra_keys=None):
    """Normalize a parsed JSONB value.  extra_keys: {key: replacement}
    applied at every depth (seed-derived numerics, identity echoes)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "observation_count":
                out[k] = "<N>"          # in-process counter (service.py)
            elif extra_keys and k in extra_keys:
                out[k] = extra_keys[k]
            else:
                out[k] = norm_jsonb(v, extra_keys)
        return out
    if isinstance(value, list):
        return [norm_jsonb(v, extra_keys) for v in value]
    if isinstance(value, str) and ISO_RE.match(value):
        return "<TS>"                    # wall-clock echo inside JSONB
    return value

# wall-clock-phase numerics inside recommendation evidence (R_SIGNAL rows
# are excluded wholesale — see the projection block; this covers any other
# t0-derived field).  Seed-rendered numerics (speed_kmh, excess_kmh) are
# compared RAW: the gate pins the sweep seed, so they are identical.
REC_EVIDENCE_KEYS = {
    "sec_into_cycle": "<TS>",
}
# uuid echoes inside event metadata (pursuit carries vehicle_id +
# recommendation_id; closed-corridor evidence carries event_id)
META_ECHO_KEYS = {
    "vehicle_id": "<UUID-ECHO>",
    "recommendation_id": "<UUID-ECHO>",
    "event_id": "<UUID-ECHO>",
    "movement_id": "<UUID-ECHO>",
    "run_id": "<UUID-ECHO>",
}

class Ctx:
    """Round context: ownership tokens discovered from the diff."""
    def __init__(self):
        self.event_token = {}    # uuid -> token
        self.sight_token = {}    # uuid -> token
        self.dispatch_token = {} # uuid -> token
        self.plate_of = {}       # vehicle uuid -> plate

def cmd_project(workdir, round_no):
    snap = json.loads((Path(workdir) / "baseline.json").read_text())
    ctx = Ctx()
    lines = []
    stats = {}

    def load(table):
        m = snap["tables"][table]
        c = m["cols"]
        rows = [dict(zip(c, jsonable(r))) for r in fetch_table(conn, table, c)]
        base_ids = {str(r[c.index(pk)]) for pk in pk_cols_of(table) for r in m["rows"]}
        return rows, base_ids

    def round_created(table):
        rows, base_ids = load(table)
        pk = pk_cols_of(table)[0]
        return [r for r in rows if str(r[pk]) not in base_ids]

    with psycopg.connect(DSN) as conn:
        # 1. event ownership tokens: FLAGSHIP is pinned (deterministic
        #    uuid5 over the fixed seed); the sweep and pursuit events are
        #    this round's creations.
        ev_rows, ev_base = load("events.corridor_events")
        round_events = [r for r in ev_rows
                        if str(r["event_id"]) not in ev_base]
        for r in round_events:
            eid = str(r["event_id"])
            if r.get("event_kind") == "vehicle_pursuit":
                ctx.event_token[eid] = "PURSUIT"
            elif (r.get("metadata") or {}).get("source") == "event-emulator":
                ctx.event_token[eid] = "SWEEP"
            else:
                ctx.event_token[eid] = "EVENT"
        ctx.event_token[FLAGSHIP_ID] = "FLAGSHIP"

        def etok(v):
            return ctx.event_token.get(str(v), "-") if v is not None else "-"

        # 2. vehicles / sightings / dispatches of THIS round → tokens.
        veh_rows, veh_base = load("corridor.vehicles")
        for r in veh_rows:
            ctx.plate_of[str(r["vehicle_id"])] = r.get("plate")

        s_rows = round_created("corridor.vehicle_sightings")
        s_rows.sort(key=lambda r: (r.get("t") or "", r.get("camera_id") or "",
                                   r.get("plate") or ""))
        for r in s_rows:
            ctx.sight_token[str(r["sighting_id"])] = (
                f"SIGHTING:{r.get('camera_id')}:{r.get('plate')}")

        def stok(v):
            return ctx.sight_token.get(str(v), "<SIGHTING>")

        d_rows = round_created("corridor.dispatches")
        for r in d_rows:
            ctx.dispatch_token[str(r["dispatch_id"])] = (
                f"DISPATCH:{r.get('agency')}:{r.get('unit_id')}")

        def dtok(v):
            return ctx.dispatch_token.get(str(v), "<DISPATCH>")

        # ── adaptive audit tables → phase-8 assertion shapes ────────────
        # showcase_run.sh:854-874 queries these ONLY for the flagship
        # subject; sweep-subject rows are seed-sampled renders (manifest).
        sit_rows, _ = load("adaptive.situation_log")
        flag_sit = [r for r in sit_rows if str(r.get("movement_id")) == FLAGSHIP_ID]
        for cls in sorted({r.get("situation_class") for r in flag_sit}):
            lines.append(f"audit_band_seen\t{cls}")
        trans = any(r.get("transitioned") for r in flag_sit)
        lines.append(f"audit_transition_present\t{trans}")
        por_rows, _ = load("adaptive.param_overrides")
        flag_por = [r for r in por_rows
                    if str(r.get("movement_id")) == FLAGSHIP_ID]
        amb = any("AMBULANCE_DELAY" in canon(r.get("evidence"))
                  for r in flag_por)
        lines.append(f"audit_ambulance_delay_present\t{amb}")
        seen_docs = set()
        for r in flag_por:
            ev = norm_jsonb(r.get("evidence"))
            line = "\t".join([
                "audit_param_doc", str(r.get("situation_class")),
                str(r.get("source")), str(r.get("shadow_eval")),
                canon(r.get("params")), canon(ev)])
            if line not in seen_docs:
                seen_docs.add(line)
                lines.append(line)
        stats["adaptive.situation_log"] = (len(flag_sit),
                                           len({r.get("situation_class") for r in flag_sit}))
        stats["adaptive.param_overrides"] = (len(flag_por), len(seen_docs))

        # ── events.corridor_events (flagship + this round's) ────────────
        scoped_events = [r for r in ev_rows
                         if str(r["event_id"]) in ctx.event_token]
        for r in sorted(scoped_events,
                        key=lambda r: (etok(r["event_id"]), r.get("event_kind"))):
            token = etok(r["event_id"])
            extra = dict(META_ECHO_KEYS)  # seed compared raw (pinned by the gate)
            md = norm_jsonb(r.get("metadata"), extra)
            lines.append("\t".join([
                "events.corridor_events", token,
                str(r.get("event_kind")), str(r.get("protection_class")),
                str(r.get("crowd_class")), str(r.get("title")),
                str(r.get("description")), str(r.get("status")),
                etok(r.get("movement_id")), canon(md)]))
        stats["events.corridor_events"] = (len(scoped_events), len(scoped_events))

        # ── event_phases / event_routes ─────────────────────────────────
        for table, key_rank, label, extra_col, fmt in (
            ("events.event_phases", "phase_rank", "label", "closure_policy",
             lambda r, v: ["events.event_phases", etok(r.get("event_id")),
                           str(r.get("phase_rank")), str(r.get("label")),
                           canon(r.get("closure_policy"))]),
            ("events.event_routes", "route_rank", "label", "segment_ids",
             lambda r, v: ["events.event_routes", etok(r.get("event_id")),
                           str(r.get("route_rank")), str(r.get("label")),
                           canon(r.get("segment_ids"))]),
        ):
            rows, _ = load(table)
            prows = [r for r in rows
                     if str(r["event_id"]) in ctx.event_token]
            prows.sort(key=lambda r: (etok(r["event_id"]),
                                      int(r.get(key_rank) or 0)))
            for r in prows:
                lines.append("\t".join(fmt(r, None)))
            stats[table] = (len(prows), len(prows))

        # ── enforcement_recommendations ─────────────────────────────────
        # R_SIGNAL_VIOLATION rows are EXCLUDED, documented in the manifest:
        # the rule fires iff the read's t — anchored at the run's wall-clock
        # t0 — falls inside the referenced signal's red phase
        # (enforcement/rules.py phase_state_at over sighting.t); even with
        # the sweep seed pinned, that row's EXISTENCE is a wall-clock
        # lottery the showcase never asserts (its chain asserts
        # R_SPD_SEGMENT only, showcase_run.sh phase 6).  Every other rule's
        # rows are compared in FULL, free-text render and evidence
        # numerics included (the pinned seed makes them identical).
        rrows = [r for r in round_created("events.enforcement_recommendations")
                 if r.get("rule_id") != "R_SIGNAL_VIOLATION"]
        rrows.sort(key=lambda r: (stok(r.get("sighting_id")), r.get("rule_id")))
        for r in rrows:
            ev = norm_jsonb(r.get("evidence"), REC_EVIDENCE_KEYS)
            ev = {k: (stok(v) if k == "sighting_id" and isinstance(v, str) else v)
                  for k, v in ev.items()}
            lines.append("\t".join([
                "events.enforcement_recommendations",
                stok(r.get("sighting_id")), str(r.get("rule_id")),
                str(r.get("status")), str(r.get("decided_by") or ""),
                str(r.get("recommendation") or ""),
                canon(ev), canon(norm_jsonb(r.get("audit_tail")))]))
        stats["events.enforcement_recommendations"] = (len(rrows), len(rrows))

        # ── vehicle_earmarks ────────────────────────────────────────────
        erows = round_created("corridor.vehicle_earmarks")
        erows.sort(key=lambda r: (str(r.get("vehicle_id")),
                                  str(r.get("lawful_basis"))))
        for r in erows:
            lines.append("\t".join([
                "corridor.vehicle_earmarks",
                str(ctx.plate_of.get(str(r.get("vehicle_id")), "<VEHICLE>")),
                str(r.get("state")), str(r.get("reason")),
                str(r.get("lawful_basis")), str(r.get("retention_days")),
                str(r.get("earmarked_by"))]))
        stats["corridor.vehicle_earmarks"] = (len(erows), len(erows))

        # ── vehicle_sightings (speed/heading/position COMPARED RAW — the
        #    pinned sweep seed makes them identical across rounds; t and
        #    run_id stay excluded: wall clock + sequence — manifest) ─────
        for r in s_rows:
            lines.append("\t".join([
                "corridor.vehicle_sightings", stok(r.get("sighting_id")),
                str(r.get("plate")), str(r.get("camera_id")),
                str(r.get("vehicle_class") or ""), str(r.get("source")),
                str(r.get("matched_plate_hit") or ""),
                str(r.get("speed_kmh")), str(r.get("heading")),
                str(r.get("position"))]))
        stats["corridor.vehicle_sightings"] = (len(s_rows), len(s_rows))

        # ── dispatches / dispatch_events ────────────────────────────────
        for r in sorted(d_rows, key=lambda r: (r.get("agency"),
                                                r.get("unit_id"))):
            lines.append("\t".join([
                "corridor.dispatches", etok(r.get("event_id")),
                str(r.get("agency")), str(r.get("unit_id")),
                str(r.get("status")), str(r.get("requested_by")),
                str(r.get("decided_by") or ""),
                canon(norm_jsonb(r.get("request_payload")))]))
        stats["corridor.dispatches"] = (len(d_rows), len(d_rows))
        derows = round_created("corridor.dispatch_events")
        derows.sort(key=lambda r: (dtok(r.get("dispatch_id")),
                                   r.get("to_status")))
        for r in derows:
            lines.append("\t".join([
                "corridor.dispatch_events", dtok(r.get("dispatch_id")),
                str(r.get("from_status") or ""), str(r.get("to_status")),
                str(r.get("actor")), str(r.get("note") or "")]))
        stats["corridor.dispatch_events"] = (len(derows), len(derows))

        # ── vehicles (plates created this round) ────────────────────────
        vround = [r for r in veh_rows
                  if str(r["vehicle_id"]) not in veh_base]
        for r in sorted(vround, key=lambda r: str(r.get("plate"))):
            lines.append("\t".join([
                "corridor.vehicles", str(r.get("plate")),
                str(r.get("vehicle_class"))]))
        stats["corridor.vehicles"] = (len(vround), len(vround))

    lines.sort()
    out = Path(workdir) / f"round_{round_no}.projection"
    out.write_text("\n".join(lines) + ("\n" if lines else ""))
    ctxout = {
        "flagship_id": FLAGSHIP_ID,
        "event_tokens": ctx.event_token,
        "sighting_tokens": ctx.sight_token,
        "dispatch_tokens": ctx.dispatch_token,
        "stats": {k: list(v) for k, v in stats.items()},
    }
    (Path(workdir) / f"round_{round_no}.context.json").write_text(
        json.dumps(ctxout, indent=1, sort_keys=True))
    print(f"[gate_py] round {round_no} projection: {len(lines)} canonical "
          f"lines -> {out}")
    for t, (raw, dedup) in stats.items():
        print(f"[gate_py]   {t:42} {raw:5} rows -> {dedup} compared")

def cmd_compare(first, *others):
    base = Path(first).read_text().splitlines()
    failed = False
    for other in others:
        mine = Path(other).read_text().splitlines()
        if mine == base:
            print(f"[gate_py] compare: {other} IDENTICAL to {first} "
                  f"({len(base)} canonical lines)")
            continue
        failed = True
        b, m = set(base), set(mine)
        only_first = sorted(b - m)
        only_other = sorted(m - b)
        print(f"[gate_py] compare: {other} DIFFERS from {first}: "
              f"{len(only_first)} line(s) only in {first}, "
              f"{len(only_other)} line(s) only in {other}")
        for tag, rows in ((f"ONLY-IN {first}", only_first),
                          (f"ONLY-IN {other}", only_other)):
            for row in rows[:40]:
                print(f"[gate_py]   {tag}: {row}")
            if len(rows) > 40:
                print(f"[gate_py]   {tag}: ... {len(rows) - 40} more")
    return 1 if failed else 0

# ── offline --fast selftest ─────────────────────────────────────────────────

def build_fixture_rows(sweep_uuid, sweep_seed, observation_count, iso,
                       log_id=11, override_id=7):
    """Raw table rows for a synthetic 'round'; two rounds that differ ONLY
    in provably-volatile values must canonicalize IDENTICALLY."""
    flagship = FLAGSHIP_ID
    pursuit = "11111111-2222-3333-4444-555555555555"
    sighting = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    sighting2 = "99999999-8888-7777-6666-555555555555"
    rec = "cafebabe-0000-4000-8000-000000000001"
    earmark = "deadbeef-0000-4000-8000-000000000002"
    dispatch = "12345678-1234-4124-8124-123456789012"
    vehicle = "55555555-4444-3555-8555-222222222222"
    rows = {
        "adaptive.situation_log": [
            {"log_id": log_id, "movement_id": flagship, "vvip_class": "none",
             "protection_class": "none", "situation_code": "R2_W2_C4",
             "situation_class": "AMBER", "severity_score": 8,
             "observed_class": "AMBER", "transitioned": True,
             "inputs": {"crowd": "EXTREME", "vvip_class": "none",
                        "weather": "RAIN", "anomaly_active": False,
                        "event_active": True, "crowd_class": "EXTREME",
                        "duration_class": "hours"},
             "observed_at": iso},
            {"log_id": log_id + 1, "movement_id": flagship,
             "vvip_class": "none", "protection_class": "none",
             "situation_code": "R3_W2_C4", "situation_class": "RED",
             "severity_score": 11, "observed_class": "RED",
             "transitioned": True,
             "inputs": {"crowd": "EXTREME", "vvip_class": "none",
                        "weather": "RAIN", "anomaly_active": False,
                        "event_active": True, "crowd_class": "EXTREME",
                        "duration_class": "hours"},
             "observed_at": iso},
            # a sweep-subject row: EXCLUDED from the audit projection
            {"log_id": log_id + 2, "movement_id": sweep_uuid,
             "vvip_class": "none", "protection_class": "none",
             "situation_code": "G0_W0_C2", "situation_class": "GREEN",
             "severity_score": 2, "observed_class": "GREEN",
             "transitioned": False,
             "inputs": {"crowd": "LOW", "vvip_class": "none",
                        "weather": "CLEAR", "anomaly_active": False,
                        "event_active": True, "crowd_class": "LOW",
                        "duration_class": "minutes"},
             "observed_at": iso},
        ],
        "adaptive.param_overrides": [
            {"override_id": override_id, "movement_id": flagship,
             "vvip_class": "none", "protection_class": "none",
             "situation_class": "RED", "source": "baseline",
             "shadow_eval": "unavailable",
             "params": {"advance_closure_sec": 120, "green_wave": True},
             "evidence": {"observation_count": observation_count,
                          "escalation": {"escalate": True, "level": "RED",
                                         "reasons": ["AMBULANCE_DELAY"],
                                         "handoff": "operator"},
                          "vvip_class": "none"},
             "published_at": iso},
        ],
        "events.corridor_events": [
            {"event_id": flagship, "event_kind": "procession",
             "protection_class": "none", "crowd_class": "EXTREME",
             "title": "Rath Yatra Monsoon Morning",
             "description": "procession + monsoon + ambulance",
             "status": "completed", "movement_id": flagship,
             "metadata": {"source": "event-emulator",
                          "use_case_id": FLAGSHIP_USE_CASE,
                          "scenario_id": "rath_yatra_monsoon",
                          "seed": FLAG_SEED,
                          "closure_policy": {"closure_type": "signal_hold"},
                          "is_replay": False}},
            {"event_id": sweep_uuid, "event_kind": "crime_incident",
             "protection_class": "none", "crowd_class": "LOW",
             "title": "Stolen-Vehicle ANPR Sweep", "description": "",
             "status": "completed", "movement_id": sweep_uuid,
             "metadata": {"source": "event-emulator",
                          "use_case_id": "watchlist_anpr_sweep",
                          "seed": sweep_seed,
                          "is_replay": False}},
            {"event_id": pursuit, "event_kind": "vehicle_pursuit",
             "protection_class": "none", "crowd_class": "LOW",
             "title": "PURSUIT — stolen GJ27EM9001 (FIR 4401/2026, Kalupur PS)",
             "description": "operator-opened pursuit", "status": "planning",
             "movement_id": None,
             "metadata": {"vehicle_id": vehicle,
                          "recommendation_id": rec,
                          "fir": "FIR 4401/2026, Kalupur PS"}},
        ],
        "events.event_phases": [
            {"phase_id": "d0d0d0d0-0000-4000-8000-000000000003",
             "event_id": flagship, "phase_rank": 1, "label": "procession",
             "window_start": iso, "window_end": iso,
             "closure_policy": {"advance_closure_sec": 0,
                                "closure_type": "signal_hold"}},
        ],
        "events.event_routes": [
            {"route_id": "e1e1e1e1-0000-4000-8000-000000000004",
             "event_id": flagship, "route_rank": 1, "label": "spine",
             "segment_ids": [1005, 1015, 1006, 1001]},
        ],
        "events.enforcement_recommendations": [
            # an R_SIGNAL_VIOLATION row: EXCLUDED from the projection (its
            # existence is a wall-clock phase lottery — manifest); present
            # only in some rounds on purpose to prove the exclusion.
            {"recommendation_id": "51515151-0000-4000-8000-0000000000f1",
             "sighting_id": sighting2, "vehicle_id": vehicle,
             "rule_id": "R_SIGNAL_VIOLATION",
             "recommendation": "e-challan recommended: GJ27EM9001 crossed "
                               "junction 3 on red (movement EW)",
             "evidence": {"sighting_id": sighting2, "plate": "GJ27EM9001",
                          "camera_id": "ANPR-RELIEF-ROAD-02", "t": iso,
                          "junction_id": 3, "movement": "EW",
                          "signal_state": "red", "sec_into_cycle": 41.7,
                          "cycle_sec": 100, "green_fraction": 0.0,
                          "source": "emulated_anpr"},
             "status": "pending", "decided_by": None, "audit_tail": []},
            {"recommendation_id": rec, "sighting_id": sighting,
             "vehicle_id": vehicle, "rule_id": "R_SPD_SEGMENT",
             "recommendation": "e-challan recommended: GJ27EM9001 read at "
                               "97 km/h in a 40 km/h segment",
             "evidence": {"sighting_id": sighting, "plate": "GJ27EM9001",
                          "camera_id": "ANPR-OLD-CITY-01", "t": iso,
                          "speed_kmh": 97.43, "speed_limit_kmh": 40,
                          "excess_kmh": 57.43, "tolerance_kmh": 5,
                          "segment_id": 1015, "road_name": "Nehru Bridge",
                          "source": "emulated_anpr"},
             "status": "approved",
             "decided_by": "Duty Officer R. Mehta (God's Eye Morning)",
             "audit_tail": [
                 {"actor": "traffic-oracle", "action": "created:pending",
                  "at": iso, "note": "deterministic rule evaluation"},
                 {"actor": "Duty Officer R. Mehta", "action": "decision:approve",
                  "at": iso, "note": "approved"}]},
        ],
        "corridor.vehicle_earmarks": [
            {"earmark_id": earmark, "vehicle_id": vehicle, "state": "flagged",
             "reason": "STOLEN VEHICLE reported — FIR 4401/2026, Kalupur PS",
             "lawful_basis": "stolen_vehicle_fir", "retention_days": 30,
             "earmarked_by": "God's Eye Morning showcase",
             "earmarked_at": iso, "retention_until": iso, "cleared_at": None},
        ],
        "corridor.vehicle_sightings": [
            {"sighting_id": sighting, "vehicle_id": vehicle,
             "plate": "GJ27EM9001", "camera_id": "ANPR-OLD-CITY-01",
             "t": iso, "position": "POINT(72.58 23.02)",
             "speed_kmh": 97.4, "heading": 88.1, "vehicle_class": "car",
             "source": "emulated_anpr", "run_id": 51,
             "matched_plate_hit": "stolen_vehicle"},
            {"sighting_id": sighting2, "vehicle_id": vehicle,
             "plate": "GJ27EM9001", "camera_id": "ANPR-RELIEF-ROAD-02",
             "t": iso, "position": "POINT(72.59 23.03)",
             "speed_kmh": 101.9, "heading": 12.3, "vehicle_class": "car",
             "source": "emulated_anpr", "run_id": 51,
             "matched_plate_hit": "stolen_vehicle"},
        ],
        "corridor.dispatches": [
            {"dispatch_id": dispatch, "event_id": flagship,
             "agency": "jan_rakshak", "unit_id": "JR-EM-01",
             "waypoint_junction": 88, "status": "on_scene",
             "requested_by": "God's Eye Morning showcase",
             "decided_by": "Duty Officer R. Mehta (God's Eye Morning)",
             "request_payload": {"agency": "jan_rakshak",
                                 "unit_id": "JR-EM-01",
                                 "waypoint_junction": 88}},
        ],
        "corridor.dispatch_events": [
            {"seq": 301, "dispatch_id": dispatch, "from_status": None,
             "to_status": "requested", "actor": "operator", "note": None,
             "at": iso},
            {"seq": 302, "dispatch_id": dispatch, "from_status": "requested",
             "to_status": "approved",
             "actor": "Duty Officer R. Mehta (God's Eye Morning)",
             "note": "citizen-volunteer coordination approved", "at": iso},
        ],
        "corridor.vehicles": [
            {"vehicle_id": vehicle, "plate": "GJ27EM9001",
             "vehicle_class": "car", "first_seen": iso, "last_seen": iso},
        ],
    }
    ids = {"sweep": sweep_uuid, "pursuit": pursuit, "sighting": sighting,
           "sighting2": sighting2, "rec": rec, "dispatch": dispatch,
           "vehicle": vehicle}
    return rows, ids

def canonicalize_fixture(rows):
    """Run fixture rows through the SAME normalization rules the live
    projection uses (formatter bodies replicated line-for-line from
    cmd_project so the selftest exercises the real rules)."""
    ctx = Ctx()
    ctx.event_token = {FLAGSHIP_ID: "FLAGSHIP"}
    for r in rows["events.corridor_events"]:
        eid = str(r["event_id"])
        if eid == FLAGSHIP_ID:
            continue
        ctx.event_token[eid] = ("PURSUIT" if r["event_kind"] == "vehicle_pursuit"
                                else "SWEEP")
    ctx.sight_token = {str(r["sighting_id"]):
                       f"SIGHTING:{r['camera_id']}:{r['plate']}"
                       for r in rows["corridor.vehicle_sightings"]}
    ctx.dispatch_token = {str(r["dispatch_id"]):
                          f"DISPATCH:{r['agency']}:{r['unit_id']}"
                          for r in rows["corridor.dispatches"]}
    ctx.plate_of = {str(r["vehicle_id"]): r["plate"]
                    for r in rows["corridor.vehicles"]}
    etok = lambda v: ctx.event_token.get(str(v), "-") if v is not None else "-"
    stok = lambda v: ctx.sight_token.get(str(v), "<SIGHTING>")
    dtok = lambda v: ctx.dispatch_token.get(str(v), "<DISPATCH>")

    lines = []
    # audit shapes (flagship scope only)
    flag_sit = [r for r in rows["adaptive.situation_log"]
                if r["movement_id"] == FLAGSHIP_ID]
    for cls in sorted({r["situation_class"] for r in flag_sit}):
        lines.append(f"audit_band_seen\t{cls}")
    lines.append(f"audit_transition_present\t"
                 f"{any(r['transitioned'] for r in flag_sit)}")
    flag_por = [r for r in rows["adaptive.param_overrides"]
                if r["movement_id"] == FLAGSHIP_ID]
    lines.append(f"audit_ambulance_delay_present\t"
                 f"{any('AMBULANCE_DELAY' in canon(r['evidence']) for r in flag_por)}")
    for r in flag_por:
        lines.append("\t".join([
            "audit_param_doc", r["situation_class"], r["source"],
            r["shadow_eval"], canon(r["params"]),
            canon(norm_jsonb(r["evidence"]))]))
    for r in rows["events.corridor_events"]:
        token = etok(r["event_id"])
        extra = dict(META_ECHO_KEYS)  # seed compared raw (pinned by the gate)
        lines.append("\t".join([
            "events.corridor_events", token, r["event_kind"],
            r["protection_class"], r["crowd_class"], r["title"],
            r["description"], r["status"], etok(r["movement_id"]),
            canon(norm_jsonb(r["metadata"], extra))]))
    for r in rows["events.event_phases"]:
        lines.append("\t".join([
            "events.event_phases", etok(r["event_id"]), str(r["phase_rank"]),
            r["label"], canon(r["closure_policy"])]))
    for r in rows["events.event_routes"]:
        lines.append("\t".join([
            "events.event_routes", etok(r["event_id"]), str(r["route_rank"]),
            r["label"], canon(r["segment_ids"])]))
    for r in rows["events.enforcement_recommendations"]:
        if r["rule_id"] == "R_SIGNAL_VIOLATION":
            continue  # excluded — wall-clock phase lottery (manifest)
        ev = norm_jsonb(r["evidence"], REC_EVIDENCE_KEYS)
        ev = {k: (stok(v) if k == "sighting_id" and isinstance(v, str) else v)
              for k, v in ev.items()}
        lines.append("\t".join([
            "events.enforcement_recommendations", stok(r["sighting_id"]),
            r["rule_id"], r["status"], r.get("decided_by") or "",
            r.get("recommendation") or "",
            canon(ev), canon(norm_jsonb(r["audit_tail"]))]))
    for r in rows["corridor.vehicle_earmarks"]:
        lines.append("\t".join([
            "corridor.vehicle_earmarks",
            ctx.plate_of.get(str(r["vehicle_id"]), "<VEHICLE>"),
            r["state"], r["reason"], r["lawful_basis"],
            str(r["retention_days"]), r["earmarked_by"]]))
    for r in rows["corridor.vehicle_sightings"]:
        lines.append("\t".join([
            "corridor.vehicle_sightings", stok(r["sighting_id"]),
            r["plate"], r["camera_id"], r.get("vehicle_class") or "",
            r["source"], r.get("matched_plate_hit") or "",
            str(r["speed_kmh"]), str(r["heading"]), str(r["position"])]))
    for r in rows["corridor.dispatches"]:
        lines.append("\t".join([
            "corridor.dispatches", etok(r["event_id"]), r["agency"],
            r["unit_id"], r["status"], r["requested_by"],
            r.get("decided_by") or "",
            canon(norm_jsonb(r["request_payload"]))]))
    for r in rows["corridor.dispatch_events"]:
        lines.append("\t".join([
            "corridor.dispatch_events", dtok(r["dispatch_id"]),
            r.get("from_status") or "", r["to_status"], r["actor"],
            r.get("note") or ""]))
    for r in rows["corridor.vehicles"]:
        lines.append("\t".join([
            "corridor.vehicles", r["plate"], r["vehicle_class"]]))
    lines.sort()
    return lines

def cmd_selftest(workdir):
    print("[gate_py] selftest: offline comparator/normalizer/FAIL-path "
          "proof (no db, no docker, no showcase)")
    wd = Path(workdir)
    # round A vs round B: differ ONLY in provably-volatile values.
    rowsA, _ = build_fixture_rows(
        sweep_uuid="ffffeeee-0000-4000-8000-00000000000a",
        sweep_seed=20269999, observation_count=41,
        iso="2026-10-18T04:05:06.789+00:00", log_id=11, override_id=7)
    rowsB, _ = build_fixture_rows(
        sweep_uuid="ffffeeee-0000-4000-8000-0000000000b",
        sweep_seed=20269999, observation_count=87,
        iso="2026-10-18T09:11:12.001Z", log_id=902, override_id=640)
    # round B's red-phase rule did NOT fire (wall-clock lottery) — the
    # exclusion must absorb exactly this difference
    rowsB["events.enforcement_recommendations"] = [
        r for r in rowsB["events.enforcement_recommendations"]
        if r["rule_id"] != "R_SIGNAL_VIOLATION"]
    fA, fB = wd / "selftest.roundA", wd / "selftest.roundB"
    fA.write_text("\n".join(canonicalize_fixture(rowsA)) + "\n")
    fB.write_text("\n".join(canonicalize_fixture(rowsB)) + "\n")

    failures = 0
    if cmd_compare(str(fA), str(fB)) == 0:
        ok("selftest[1/3] benign volatility (pursuit/sweep uuids, "
           "observation_count, timestamps, surrogate ids, and the "
           "wall-clock R_SIGNAL row present in A but not B) normalizes "
           "away: round A == round B")
    else:
        bad("selftest[1/3] FAIL: rounds differing only in volatile values "
            "compared DIFFERENT")
        failures += 1

    # round C: REAL content differences (an audit band flips, an extra
    # dispatch lifecycle row) → the comparator MUST fail and print them.
    rowsC, _ = build_fixture_rows(
        sweep_uuid="ffffeeee-0000-4000-8000-0000000000b",
        sweep_seed=20269999, observation_count=87,
        iso="2026-10-18T09:11:12.001Z", log_id=902, override_id=640)
    rowsC["adaptive.situation_log"][1]["situation_class"] = "BLACK"
    rowsC["adaptive.situation_log"][1]["situation_code"] = "R4_W2_C4"
    rowsC["corridor.dispatch_events"].append(
        {"seq": 303, "dispatch_id": rowsC["corridor.dispatches"][0]["dispatch_id"],
         "from_status": "approved", "to_status": "en_route",
         "actor": "Duty Officer R. Mehta (God's Eye Morning)",
         "note": None, "at": "2026-10-18T09:13:00Z"})
    rowsC["events.event_phases"][0]["closure_policy"] = {
        "advance_closure_sec": 60, "closure_type": "hard_closure"}
    # a changed seed-rendered VALUE (sighting speed): compared RAW since the
    # gate pins the sweep seed — must FAIL, proving the tightening
    rowsC["corridor.vehicle_sightings"][0]["speed_kmh"] = 88.8
    fC = wd / "selftest.roundC"
    fC.write_text("\n".join(canonicalize_fixture(rowsC)) + "\n")
    if cmd_compare(str(fA), str(fC)) == 1:
        ok("selftest[2/3] real nondeterminism (band flip, extra lifecycle "
           "row, changed closure policy, changed sighting speed) FAILS the "
           "comparator — rows printed above")
    else:
        bad("selftest[2/3] FAIL: a genuinely different round compared "
            "EQUAL — the gate would hide real nondeterminism")
        failures += 1

    # round D: the FLAGSHIP event id changed (wrong deterministic uuid) →
    # NOT normalized → must differ.
    rowsD, _ = build_fixture_rows(
        sweep_uuid="ffffeeee-0000-4000-8000-0000000000b",
        sweep_seed=20269999, observation_count=87,
        iso="2026-10-18T09:11:12.001Z", log_id=902, override_id=640)
    wrong_flag = "0bad0bad-0000-4000-8000-0000000000ff"
    for table in ("adaptive.situation_log", "adaptive.param_overrides",
                  "events.corridor_events", "events.event_phases",
                  "events.event_routes", "corridor.dispatches"):
        for r in rowsD[table]:
            for k in ("movement_id", "event_id"):
                if r.get(k) == FLAGSHIP_ID:
                    r[k] = wrong_flag
    fD = wd / "selftest.roundD"
    fD.write_text("\n".join(canonicalize_fixture(rowsD)) + "\n")
    if cmd_compare(str(fA), str(fD)) == 1:
        ok("selftest[3/3] a wrong FLAGSHIP event id FAILS (the "
           "deterministic id is compared, never normalized)")
    else:
        bad("selftest[3/3] FAIL: a changed flagship id compared EQUAL — "
            "the strongest invariant is broken")
        failures += 1

    print()
    print_manifest()
    return 1 if failures else 0

def ok(msg):  print(f"[gate_py]   OK   {msg}")
def bad(msg): print(f"[gate_py]   FAIL {msg}")

def main(argv):
    if len(argv) < 2:
        print("usage: gate_py.py {selftest|check|snapshot|restore|project|"
              "compare|manifest} ...", file=sys.stderr)
        return 2
    cmd, rest = argv[1], argv[2:]
    if cmd == "selftest":
        return cmd_selftest(rest[0] if rest else ".")
    if cmd == "manifest":
        print_manifest()
        return 0
    if cmd == "check":
        cmd_check(); return 0
    if cmd == "snapshot":
        cmd_snapshot(rest[0]); return 0
    if cmd == "restore":
        cmd_restore(rest[0]); return 0
    if cmd == "project":
        cmd_project(rest[0], int(rest[1])); return 0
    if cmd == "compare":
        if len(rest) < 2:
            print("usage: gate_py.py compare FILE FILE...", file=sys.stderr)
            return 2
        return cmd_compare(*rest)
    print(f"gate_py: unknown command {cmd}", file=sys.stderr)
    return 2

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
GATEPYEOF

export PG_HOST PG_PORT PG_USER PG_DB PG_PASSWORD

# ════════════════════════════════════════════════════════════════════════════
# --fast: offline selftest only — comparator + normalizer + FAIL path.
# ════════════════════════════════════════════════════════════════════════════
if [ "$FAST" = true ]; then
    log "--fast selftest (offline; no showcase, no docker, no db)"
    if ! command -v python3 >/dev/null 2>&1; then
        die "python3 not found" "install python3 (the selftest is pure python, no deps)"
    fi
    # gate_py imports psycopg at module load; the selftest must also run on
    # hosts without it → a stub shadows the import for the offline path
    # only (any db-backed use of the stub exits loudly).
    if ! python3 -c "import psycopg" 2>/dev/null; then
        PY_STUB="$WORKDIR/psycopg_stub"
        mkdir -p "$PY_STUB"
        cat > "$PY_STUB/psycopg.py" <<'STUBEOF'
"""Offline stub: lets gate_py.py import for --fast without psycopg.  Any
db-backed use fails loudly instead of pretending."""
class _Boom:
    def __getattr__(self, name):
        raise SystemExit("gate_py: psycopg unavailable — db-backed commands "
                         "need VVIP/models/.venvs/migrate (see scripts/migrate.py)")
def connect(*a, **k):
    raise SystemExit("gate_py: psycopg unavailable — db-backed commands need "
                     "the migrate venv")
class sql:  # noqa: D401 - minimal namespace stand-in
    class SQL:
        def __init__(self, s): self._s = s
        def format(self, *a, **k): return self
    @staticmethod
    def Identifier(s): return sql.SQL('"%s"' % s)
STUBEOF
        if PYTHONPATH="$PY_STUB" python3 "$GATE_PY" selftest "$WORKDIR"; then
            ok "--fast selftest PASS"; exit 0
        fi
        bad "--fast selftest FAILED"; exit 1
    fi
    if python3 "$GATE_PY" selftest "$WORKDIR"; then
        ok "--fast selftest PASS"; exit 0
    fi
    bad "--fast selftest FAILED"; exit 1
fi

# ════════════════════════════════════════════════════════════════════════════
# 0. preflight
# ════════════════════════════════════════════════════════════════════════════
log "workdir: $WORKDIR"

[ -x "$GATE_PY_BIN" ] || die "migrate-venv python not found: $GATE_PY_BIN" \
    "bash $VVIP_DIR/infra/scripts/serve_models.sh build (provisions psycopg for scripts/migrate.py — the same venv this gate reuses)"
"$GATE_PY_BIN" -c "import psycopg" 2>/dev/null || die "psycopg missing in $GATE_PY_BIN" \
    "bash $VVIP_DIR/infra/scripts/serve_models.sh build"

for c in "$PG_CONTAINER" "$VALKEY_CONTAINER" "$NATS_CONTAINER" "$ORACLE_CONTAINER" "$BRAIN_CONTAINER" "$INGRESS_CONTAINER" "$DECK_CONTAINER"; do
    [ "$(docker inspect "$c" --format '{{.State.Running}}' 2>/dev/null)" = "true" ] \
        || die "container $c is not running" "cd $INFRA_DIR && docker compose -p $COMPOSE_PROJECT $COMPOSE_FILES up -d (the ARGUS stack must be live)"
done

[ "$(hcode "$ORACLE_URL/health" 15)" = "200" ] \
    || die "traffic-oracle $ORACLE_URL/health not 200" "docker logs $ORACLE_CONTAINER --tail 50"
b=$(health "$BRAIN_URL/health" 15)
printf '%s' "$b" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("status")=="ok" and d.get("strategic_tier",{}).get("model_host")=="connected" and d.get("tactical_tier",{}).get("llama_server")=="connected" else 1)' 2>/dev/null \
    || die "convoy-brain $BRAIN_URL/health not ok (decider+gemma must be connected)" \
        "check host models (infra/scripts/serve_models.sh), then the argus convoy-brain; body was: $b"
[ "$(hcode "$DECK_URL/" 15)" = "200" ] || die "command-deck $DECK_URL/ not 200" "docker logs $DECK_CONTAINER --tail 50"
[ "$(hcode "$DECK_URL/api/v1/movements/active" 15)" = "200" ] || die "deck /api/v1 proxy not answering" "docker logs $DECK_CONTAINER --tail 50"
e=$(health "$EMULATOR_URL/health" 15)
printf '%s' "$e" | python3 -c 'import json,sys; d=json.load(sys.stdin); s=d.get("stack",{}); sys.exit(0 if d.get("status")=="ok" and s.get("store") and s.get("valkey") and s.get("twin") and s.get("nats") else 1)' 2>/dev/null \
    || die "event-emulator $EMULATOR_URL/health not fully wired (body: $e)" \
        "the :8090 emulator must point at the ARGUS store/valkey/nats (VVIP_DATABASE_URL ...@127.0.0.1:56434/corridor_db)"
[ "$(hcode "$DECIDER_URL/decider/health" 15)" = "200" ] || die "decider $DECIDER_URL down" "bash $VVIP_DIR/infra/scripts/serve_models.sh"
[ "$(hcode "$LLAMA_URL/health" 15)" = "200" ] || die "llama-server $LLAMA_URL down" "bash $VVIP_DIR/infra/scripts/serve_models.sh"
[ "$(hcode "$EMBED_URL/health" 15)" = "200" ] || die "embedding-service $EMBED_URL down" "bash $VVIP_DIR/infra/scripts/serve_models.sh"

"$GATE_PY_BIN" "$GATE_PY" check || die "scoped-table check failed against $PG_CONTAINER" \
    "POSTGIS_CONTAINER=$PG_CONTAINER python3 $VVIP_DIR/scripts/migrate.py (apply 001-011 to the argus store)"
ok "preflight: argus fleet + host models + emulator + scoped schema — all reachable"

# ── store invariant: the 006 osm_assumed signal-program seed ────────────────
# The showcase's phase-4d apply (POST /signals/plans/apply) plans against the
# CURRENT programs in corridor.signal_programs; a store whose 006 seed rows
# were lost (e.g. a junction rebuild cascading them away) makes plan_city()
# plan NOTHING -> 409 no_junctions_changed with empty planned_junctions and
# the round is incomparable (observed live 2026-10-18: argus store had 0
# rows where the original has the 11 osm_assumed seeds; the seed SELECT
# still yields 11).  Migration 006 is idempotent by design ("Idempotent and
# additive-only: safe to re-run on existing installs", 006_simulation_runs.sql
# header), so the gate re-applies it ONLY when a signal_control junction
# lacks its program row, re-checks, and heals BEFORE the baseline snapshot
# (the seed rows then ride inside the baseline and survive every restore).
SEED_MISSING_SQL="SELECT count(*) FROM corridor.junctions j WHERE j.signal_control = TRUE AND NOT EXISTS (SELECT 1 FROM corridor.signal_programs sp WHERE sp.junction_id = j.junction_id)"
missing=$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -At -c "$SEED_MISSING_SQL" 2>/dev/null)
case "$missing" in ''|*[!0-9]*) die "could not read the signal_programs invariant from $PG_CONTAINER" "docker exec $PG_CONTAINER psql -U $PG_USER -d $PG_DB -At -c \"$SEED_MISSING_SQL\"";; esac
if [ "$missing" -gt 0 ]; then
    log "store invariant: $missing signal_control junction(s) lack programs — re-applying the idempotent 006 seed"
    docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" --single-transaction -v ON_ERROR_STOP=1 \
        < "$VVIP_DIR/services/corridor-store/migrations/006_simulation_runs.sql" > "$WORKDIR/seed006.reapply.log" 2>&1 \
        || die "re-applying 006_simulation_runs.sql failed (see $WORKDIR/seed006.reapply.log)" \
            "docker exec -i $PG_CONTAINER psql -U $PG_USER -d $PG_DB --single-transaction -v ON_ERROR_STOP=1 < $VVIP_DIR/services/corridor-store/migrations/006_simulation_runs.sql"
    missing2=$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -At -c "$SEED_MISSING_SQL" 2>/dev/null)
    [ "${missing2:-1}" = "0" ] || die "signal_programs invariant still broken after the 006 re-apply ($missing2 junction(s) missing)" \
        "inspect corridor.junctions / corridor.segment_adjacency / corridor.road_segments on $PG_CONTAINER (the seed derives programs from road classes)"
    ok "store invariant: signal_programs re-seeded via idempotent 006 (osm_assumed rows present; inside the baseline from here on)"
else
    ok "store invariant: every signal_control junction has its program (006 seed intact)"
fi

# ── original-stack guard (read-only; argus_full_smoke.sh pattern) ────────────
snapshot_original_containers() {
    docker ps -a --filter 'name=vvip-' --format '{{.Names}}' 2>/dev/null \
        | grep -v '^vvip-argus-' \
        | while read -r name; do
              docker inspect "$name" --format \
                '{{.Name}}|{{.Config.Image}}|{{.State.Status}}|{{.State.Running}}|{{.State.ExitCode}}|{{.RestartCount}}|{{.State.StartedAt}}' \
                2>/dev/null
          done | sort
}
snapshot_original_images() {
    docker images --format '{{.Repository}}:{{.Tag}}|{{.ID}}' 2>/dev/null \
        | grep -E '^(vvip|veer)-' | grep -v -- '-argus' | sort
}
snapshot_original_containers > "$WORKDIR/original.containers.before"
snapshot_original_images     > "$WORKDIR/original.images.before"
n_guard_c=$(wc -l < "$WORKDIR/original.containers.before")
n_guard_i=$(wc -l < "$WORKDIR/original.images.before")
ok "original stack guarded read-only ($n_guard_c container(s), $n_guard_i shared tag(s))"

# ════════════════════════════════════════════════════════════════════════════
# 1. baseline snapshot
# ════════════════════════════════════════════════════════════════════════════
log "phase 1: baseline snapshot of every showcase-writable table (argus store $PG_CONTAINER/$PG_DB)"
"$GATE_PY_BIN" "$GATE_PY" snapshot "$WORKDIR" || die "baseline snapshot failed" \
    "check $PG_CONTAINER health and credentials (POSTGIS_PASSWORD env; default dev_changeme per compose)"

# oracle reset helper: fresh process with the writer flag ON, converged.
reset_oracle() {
    writer=$(health "$ORACLE_URL/api/v1/signals/plans" 15 | python3 -c 'import json,sys; print(json.load(sys.stdin).get("writer_enabled"))' 2>/dev/null || echo error)
    if [ "$writer" != "True" ]; then
        log "signal-policy writer disabled — recreating ARGUS traffic-oracle with VVIP_SIGNAL_POLICY_WRITER=1 ($COMPOSE_FILES)"
        ( cd "$INFRA_DIR" && VVIP_SIGNAL_POLICY_WRITER=1 docker compose -p "$COMPOSE_PROJECT" $COMPOSE_FILES up -d traffic-oracle ) \
            || die "could not recreate the argus traffic-oracle with the writer flag" \
                "cd $INFRA_DIR && VVIP_SIGNAL_POLICY_WRITER=1 docker compose -p $COMPOSE_PROJECT $COMPOSE_FILES up -d traffic-oracle"
    else
        log "restarting ARGUS traffic-oracle (fresh process: in-memory adaptive state cannot be DB-restored)"
        docker restart "$ORACLE_CONTAINER" >/dev/null \
            || die "docker restart $ORACLE_CONTAINER failed" "docker logs $ORACLE_CONTAINER --tail 50"
    fi
    deadline=$(( $(date +%s) + CONVERGE_TIMEOUT_SEC ))
    until [ "$(hcode "$ORACLE_URL/health" 5)" = "200" ]; do
        [ "$(date +%s)" -ge "$deadline" ] && die "traffic-oracle did not converge within ${CONVERGE_TIMEOUT_SEC}s after reset" "docker logs $ORACLE_CONTAINER --tail 80"
        sleep 2
    done
    until [ "$(health "$ORACLE_URL/api/v1/signals/plans" 5 | python3 -c 'import json,sys; print(json.load(sys.stdin).get("writer_enabled"))' 2>/dev/null || echo error)" = "True" ]; do
        [ "$(date +%s)" -ge "$deadline" ] && die "traffic-oracle writer flag never came up after reset" "docker exec $ORACLE_CONTAINER env | grep VVIP_SIGNAL_POLICY_WRITER (must be 1)"
        sleep 2
    done
    ok "argus traffic-oracle reset + converged (writer ENABLED)"
}

# ════════════════════════════════════════════════════════════════════════════
# 2. rounds
# ════════════════════════════════════════════════════════════════════════════
SHOWCASE_LOG=""; ROUND_FAILED=0
for r in $(seq 1 "$ROUNDS"); do
    log "=== round $r/$ROUNDS ==="
    log "round $r: restore-to-baseline (idempotent)"
    "$GATE_PY_BIN" "$GATE_PY" restore "$WORKDIR" || die "round $r pre-restore failed" "see [gate_py] output above; the argus store must be writable"

    reset_oracle

    log "round $r: running showcase_run.sh against the ARGUS stack (env overrides; sweep seed PINNED to $GATE_SWEEP_SEED)"
    SHOWCASE_LOG="$WORKDIR/round_${r}.showcase.log"
    if env \
        ORACLE_URL="$ORACLE_URL" BRAIN_URL="$BRAIN_URL" EMULATOR_URL="$EMULATOR_URL" \
        DECK_URL="$DECK_URL" DECIDER_URL="$DECIDER_URL" LLAMA_URL="$LLAMA_URL" EMBED_URL="$EMBED_URL" \
        COMPOSE_PROJECT="$COMPOSE_PROJECT" COMPOSE_FILES="$COMPOSE_FILES" \
        POSTGIS_CONTAINER="$PG_CONTAINER" VALKEY_CONTAINER="$VALKEY_CONTAINER" NATS_CONTAINER="$NATS_CONTAINER" \
        ORACLE_CONTAINER="$ORACLE_CONTAINER" BRAIN_CONTAINER="$BRAIN_CONTAINER" \
        INGRESS_CONTAINER="$INGRESS_CONTAINER" DECK_CONTAINER="$DECK_CONTAINER" \
        SWEEP_SEED="$GATE_SWEEP_SEED" \
        bash "$SCRIPT_DIR/showcase_run.sh" 2>&1 | tee "$SHOWCASE_LOG"; then
        ok "round $r: showcase PASS (its own assertions held)"
    else
        bad "round $r: showcase FAILED — terminal state not comparable (log: $SHOWCASE_LOG)"
        ROUND_FAILED=1
        log "round $r: restore-to-baseline after the failed round (the store is left clean regardless)"
        "$GATE_PY_BIN" "$GATE_PY" restore "$WORKDIR" || die "post-failure restore failed" "see [gate_py] output above"
        break
    fi
    cp -f "$VVIP_DIR/showcase/showcase_results.json" "$WORKDIR/round_${r}.results.json" 2>/dev/null || true

    log "round $r: capturing canonical terminal-state projection"
    "$GATE_PY_BIN" "$GATE_PY" project "$WORKDIR" "$r" || die "round $r projection failed" "see [gate_py] output above"

    log "round $r: restore-to-baseline (leave the argus store as found)"
    "$GATE_PY_BIN" "$GATE_PY" restore "$WORKDIR" || die "round $r post-restore failed" "see [gate_py] output above"
done

# ════════════════════════════════════════════════════════════════════════════
# 3. compare + verdict
# ════════════════════════════════════════════════════════════════════════════
COMPARE_RC=0
if [ "$ROUND_FAILED" -eq 0 ]; then
    log "phase 3: comparing projections (round 1 vs every round)"
    proj_files=()
    for r in $(seq 2 "$ROUNDS"); do proj_files+=("$WORKDIR/round_${r}.projection"); done
    if "$GATE_PY_BIN" "$GATE_PY" compare "$WORKDIR/round_1.projection" "${proj_files[@]}"; then
        COMPARE_RC=0
    else
        COMPARE_RC=1
    fi
fi

# original-stack guard, after everything
snapshot_original_containers > "$WORKDIR/original.containers.after"
snapshot_original_images     > "$WORKDIR/original.images.after"
GUARD_RC=0
for kind in containers images; do
    if ! diff -u "$WORKDIR/original.$kind.before" "$WORKDIR/original.$kind.after" > "$WORKDIR/original.$kind.diff" 2>/dev/null; then
        GUARD_RC=1
        bad "original-stack $kind CHANGED during the gate:"
        sed 's/^/      /' "$WORKDIR/original.$kind.diff" >&2
    fi
done
[ "$GUARD_RC" -eq 0 ] && ok "original stack UNTOUCHED ($n_guard_c container(s) + $n_guard_i shared tag(s) byte-identical, incl. StartedAt)"

echo "────"
"$GATE_PY_BIN" "$GATE_PY" manifest

echo "────"
if [ "$ROUND_FAILED" -ne 0 ]; then
    echo "${BOLD}REPETITION GATE FAIL${RESET} — a showcase round failed; see $WORKDIR/round_*.showcase.log"
    exit 1
fi
if [ "$COMPARE_RC" -ne 0 ]; then
    echo "${BOLD}REPETITION GATE FAIL${RESET} — terminal-state projections DIFFER across rounds (real nondeterminism; differing rows printed above); artifacts: $WORKDIR"
    exit 1
fi
if [ "$GUARD_RC" -ne 0 ]; then
    echo "${BOLD}REPETITION GATE FAIL${RESET} — the original stack was touched during the gate (diff above)"
    exit 1
fi
echo "${BOLD}REPETITION GATE PASS${RESET} — $ROUNDS round(s) of God's Eye Morning produced IDENTICAL canonical terminal-state projections from identical baselines (only the manifest-listed volatility normalized)"
echo "      workdir: $WORKDIR"
echo "      note: the argus traffic-oracle is left running with VVIP_SIGNAL_POLICY_WRITER=1 (the showcase's own documented behavior — showcase_run.sh:47-49 'the flagship owns bringing it up'); the argus DB was restored to its pre-gate baseline; VVIP/showcase/* holds the last round's showcase artifacts"
exit 0
