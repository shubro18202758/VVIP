#!/usr/bin/env bash
# repetition_gate.sh — the CONSISTENCY-UNDER-REPETITION gate for "God's Eye
# Morning" (showcase_run.sh) against the ARGUS stack (or a private stack
# pointed at through the env).
#
# CLAIM PROVEN: run the showcase N times (default 2) from an identical
# starting state, and the terminal database state the showcase's own
# phase-8 assertions are judged over is IDENTICAL across rounds — after
# normalizing exactly the volatility classes that are provably not system
# output (wall clock, run-scoped emulator ids, gen_random_uuid identifiers,
# BIGSERIAL sequences, one in-process counter, and engine-loop row
# multiplicity).  Every normalization is printed with its justification on
# every run (the manifest); anything else that differs FAILS the gate with
# the differing rows.  Real nondeterminism is reported, never hidden.
#
# Terminal-state tables (anchored on showcase_run.sh phase 8's assertion
# queries): adaptive.situation_log, adaptive.param_overrides,
# events.corridor_events, events.event_phases — plus the tables carrying the
# pursuit-chain / Jan-Rakshak / earmark terminal state the showcase asserts
# through its APIs (phases 3-6): events.event_routes,
# events.enforcement_recommendations, corridor.vehicle_earmarks,
# corridor.vehicle_sightings, corridor.dispatches, corridor.dispatch_events,
# corridor.vehicles.
#
# RUN-SCOPED IDS: every emulator run is a fresh subject (its event id is
# uuid5 over use_case|seed|run_id), so the flagship event differs every
# round.  Each round's ids are read from that round's showcase_results.json
# (run_scoped_ids) and tokenised (FLAGSHIP, SWEEP, PURSUIT); a round whose
# flagship carries no situation or parameter audit rows FAILS — an empty
# scope is never compared empty-equal.
#
# The two adaptive audit tables are compared through the SAME shapes
# phase 8 asserts (band classes seen, a transition recorded, an
# AMBULANCE_DELAY escalation recorded, and the full param-decision
# documents), because their row-level inputs are wall-clock SAMPLES: the
# engine loop polls the latest inputs doc every 5 s
# (traffic_oracle/adaptive/engine_loop.py) while the flagship tape
# overwrites that doc every ~0.33 s of wall time (tick_sec 10 at
# clock_scale 30) — WHICH doc a tick samples is timing alignment, so
# multiplicity AND sampled-row identity are volatile by construction
# (full reasoning in the manifest the gate prints on every run).
#
# Mechanism:
#   0. guard every container outside the target fleet read-only (the
#      argus_full_smoke.sh pattern — run against a private stack, that
#      includes the live argus fleet); preflight the target fleet + host
#      models, fail fast with fixes.
#   1. BASELINE snapshot (documented SQL projection of every showcase-
#      writable table, parameter-bound via psycopg, ONE repeatable-read
#      snapshot across all tables) into the workdir.
#   2. each round: restore-to-baseline (idempotent, verified), reset the
#      target traffic-oracle process (its in-memory adaptive state —
#      hysteresis bands, observation counters, adaptive/service.py —
#      cannot be restored by any DB snapshot), converge-wait, run
#      showcase_run.sh with the env overrides, capture the CANONICAL
#      projection of the round's terminal state, restore-to-baseline
#      again (the DB is left as found — every table a round writes but
#      the feed tables traffic.observations and traffic.anomaly_log —
#      append-only telemetry the emulated feed keeps landing after a round
#      and a live feed writes all along; no compared state reads them, the
#      engine takes its anomaly flag from Valkey).
#      The restore is OWNERSHIP-scoped (gate_py classify): it deletes only
#      rows the round provably wrote (its emulator runs — the two use
#      cases, the sweep at the pinned seed — their events and the scripted
#      officer's pursuit, those events' children, the reads carrying the
#      round's run ids, their recommendations, the showcase's earmark of
#      the stolen plate, vehicles wholly the round's, the round subjects'
#      movements and audit rows), and its only UPDATE puts back a baseline
#      vehicle whose change is exactly the round's own widening
#      (first_seen/last_seen/updated_at).  It never re-INSERTs (a read the
#      C6 retention reaper deleted stays deleted) and never touches anyone
#      else's row or change (an operator's approval, earmark, status
#      change, a living city): those are reported and FAIL the gate (the
#      store was not quiescent).  Before any DELETE every foreign key into
#      the scoped tables (read from pg_catalog) is checked: a row that
#      references a row about to go must itself be a round row being
#      deleted — else (e.g. an officer decided a round recommendation) the
#      restore deletes and reverts NOTHING.  No CASCADE / SET NULL can reach
#      a row that is not the round's.
#   3. compare projections across rounds (round 1 vs every other round);
#      exit 0 only when all identical AND the showcase itself exited 0 in
#      every round AND the guarded containers stayed untouched.
#   EXIT (pass, fail, die, Ctrl-C, TERM): a round still in flight has its
#      running emulator runs stopped and its own rows restored; when the
#      gate recreated traffic-oracle to switch the signal-policy writer on,
#      it is recreated again with its PRE-GATE value (compose --no-deps, or
#      ORACLE_WRITER_CMD for a private stack's oracle).
#
# Usage:
#     bash VVIP/scripts/repetition_gate.sh [N]        # N rounds, default 2
#     bash VVIP/scripts/repetition_gate.sh --fast     # offline selftest:
#         proves the comparator, the normalizer and the FAIL paths on
#         fixtures WITHOUT running the showcase or touching docker/db.
#
# Env:   endpoints/containers use the ${VAR:-default} pattern of
#        showcase_run.sh / argus_full_smoke.sh, with the ARGUS stack as
#        defaults (oracle :8091, brain :8092, emulator :8090, deck :5273,
#        postgis :56434 in container vvip-argus-postgis, db corridor_db);
#        DECK_CONTAINER= / EMULATOR_CONTAINER= empty: served outside docker.
#        POSTGIS_PASSWORD defaults to the compose dev credential
#        (dev_changeme), the compose default.
#        GATE_PY_BIN: a python with psycopg — default the migrate venv,
#        ${VEER_VENV_DIR}/migrate (VEER_VENV_DIR from the environment, else
#        ~/.config/veer/veer.env, else VVIP/.venvs).
#        SHOWCASE_OUT_DIR: where each round's showcase writes its artifacts
#        (default VVIP/showcase — the committed evidence; point it elsewhere
#        for a private stack); REPETITION_OUT_DIR: the gate's workdirs
#        (default $SHOWCASE_OUT_DIR/repetition).
#        The deck's operator gate: signed in through scripts/deck_session.sh
#        (VEER_OPERATOR_PIN, VEER_OPERATOR_PIN_FILE or veer.env).
#        CONVERGE_TIMEOUT_SEC bounds the post-restart health wait.
#        ORACLE_WRITER_CMD: for a traffic-oracle compose did NOT create (a
#        private stack's `docker run`), the command that recreates it with
#        VVIP_SIGNAL_POLICY_WRITER taken from its environment (1 for the
#        gate, the pre-gate value on exit); unset, such an oracle whose
#        writer is off is refused.
#        Expect tens of minutes for the full 2-round gate.
#
# Security: every URL below is an internal endpoint (an env-overridable
# config constant of this script, http scheme, loopback host) — none comes
# from external input.  All SQL is executed by the embedded gate_py.py via
# psycopg with %s parameter binding; the only string-built SQL fragments
# use table/column names that are constants OF THIS SCRIPT or read from
# pg_catalog, always quoted as identifiers (the scripts/migrate.py
# pattern).  Docker use: read-only inspection; `docker
# restart` of the target traffic-oracle, or `docker compose up -d` of it
# when (and only when) compose created that container.  Every vvip-*
# container outside the target fleet is snapshotted read-only before and
# after, as are the images they run (tag -> image id), and any drift fails
# the gate.

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
DECK_CONTAINER="${DECK_CONTAINER-${COMPOSE_PROJECT}-command-deck}"
EMULATOR_CONTAINER="${EMULATOR_CONTAINER-${COMPOSE_PROJECT}-event-emulator}"

PG_HOST="${POSTGIS_HOST:-127.0.0.1}"
PG_PORT="${POSTGIS_PORT:-56434}"
PG_USER="${POSTGIS_USER:-corridor_admin}"
PG_DB="${POSTGIS_DB:-corridor_db}"
PG_PASSWORD="${POSTGIS_PASSWORD:-dev_changeme}"

# The migrate venv (psycopg) lives under VEER_VENV_DIR — read from veer.env
# when this shell does not carry it (one key; veer.env is not sourced, so
# its secrets never enter this script's environment).
if [ -z "${VEER_VENV_DIR:-}" ] && [ -r "${VEER_ENV_FILE:-$HOME/.config/veer/veer.env}" ]; then
    VEER_VENV_DIR="$(sed -n 's/^VEER_VENV_DIR=//p' "${VEER_ENV_FILE:-$HOME/.config/veer/veer.env}" | tail -n 1)"
fi
GATE_PY_BIN="${GATE_PY_BIN:-${VEER_VENV_DIR:-$VVIP_DIR/.venvs}/migrate/bin/python}"
CONVERGE_TIMEOUT_SEC="${CONVERGE_TIMEOUT_SEC:-180}"
# The sweep seed is PINNED for every round (showcase_run.sh reads SWEEP_SEED
# from env; its default stays fresh-per-run).  Pinning removes the harness's
# per-run randomization from the compared state entirely: identical tapes →
# identical sighting positions/speeds, identical rule firings — so the
# gate compares those values RAW instead of tokenizing them.  (Found live
# 2026-10-18: with a fresh seed per round, the read set's segment
# attribution and rule firings differed round to round.)
GATE_SWEEP_SEED="${GATE_SWEEP_SEED:-20261105}"
SHOWCASE_OUT_DIR="${SHOWCASE_OUT_DIR:-$VVIP_DIR/showcase}"

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

WORKROOT="${REPETITION_OUT_DIR:-$SHOWCASE_OUT_DIR/repetition}"
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
import time
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
SWEEP_USE_CASE = "watchlist_anpr_sweep"
STOLEN_PLATE = "GJ27EM9001"                       # event_emulator/tape.py _STOLEN_LABEL
OFFICER = "Scripted demo officer (showcase script)"
# ownership markers the round's writers stamp (the restore's attribution)
SHOWCASE_LABEL = "God's Eye Morning showcase"      # showcase_run.sh earmarked_by / requested_by
SCENARIO_EARMARKER = "event-emulator (system) — scenario precondition"  # event_emulator/runner.py
EMULATOR_ACTOR = "event-emulator (system)"         # event_emulator/store.py SYSTEM_ACTOR
#: the only columns the round's reads / earmark change on a vehicle row
#: (traffic_oracle/vehicles/store.py upsert: first_seen LEAST, last_seen
#: GREATEST, updated_at)
VEHICLE_WIDENING = {"first_seen", "last_seen", "updated_at"}

# Every ledgered emulator run is a fresh subject: its movement/event id is
# uuid5 over use_case|seed|run:<run_id> (event_emulator/tape.py
# movement_uuid), so the FLAGSHIP id differs every round.  The live
# projection reads each round's ids from that round's showcase_results.json
# (run_scoped_ids) and tokenises them; run_scoped_id builds the selftest's
# fixtures the same way.
_NS = _uuid.uuid5(_uuid.NAMESPACE_URL, "vvip-event-emulator")


def run_scoped_id(use_case, seed, run_id):
    return str(_uuid.uuid5(_NS, f"{use_case}|{seed}|run:{run_id}"))


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

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
    "events.event_status_log",
    "convoy.movements",
    "convoy.planned_routes",
    "emulator.emulation_runs",
    "simulation.runs",
    "corridor.signal_program_history",
    "corridor.signal_programs",
    "traffic.synthetic_observations",
    "traffic.incidents",
]
ALL_TABLES = list(PROJECTED) + RESTORE_ONLY

# Children before parents: the round's own rows are deleted in this order
# (every row a DELETE could cascade or SET NULL into is checked to be a
# round row being deleted too — see classify).  Nothing is ever re-inserted.
DELETE_ORDER = [
    "events.enforcement_recommendations",
    "corridor.dispatch_events",
    "corridor.dispatches",
    "events.event_phases",
    "events.event_routes",
    "events.event_status_log",
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
    "traffic.incidents",
]

#: Foreign keys INTO the scoped tables (migrations 007-013), as
#: (child table, child column, parent table, parent column, on delete).
#: The live restore reads them from pg_catalog (fk_edges) — this copy is
#: what the offline selftest checks the cascade guard against.
FK_EDGES = [
    ("corridor.dispatch_events", "dispatch_id", "corridor.dispatches", "dispatch_id", "c"),
    ("events.enforcement_recommendations", "sighting_id", "corridor.vehicle_sightings", "sighting_id", "n"),
    ("corridor.vehicle_earmarks", "vehicle_id", "corridor.vehicles", "vehicle_id", "c"),
    ("corridor.vehicle_sightings", "vehicle_id", "corridor.vehicles", "vehicle_id", "c"),
    ("corridor.vehicle_sightings", "run_id", "emulator.emulation_runs", "run_id", "n"),
    ("corridor.dispatches", "event_id", "events.corridor_events", "event_id", "c"),
    ("events.event_phases", "event_id", "events.corridor_events", "event_id", "c"),
    ("events.event_routes", "event_id", "events.corridor_events", "event_id", "c"),
    ("events.event_status_log", "event_id", "events.corridor_events", "event_id", "c"),
    ("convoy.planned_routes", "movement_id", "convoy.movements", "movement_id", "a"),
    ("convoy.diversions", "movement_id", "convoy.movements", "movement_id", "a"),
]

RESTORE_PK = {
    "events.event_status_log": ("seq",),
    "convoy.movements": ("movement_id",),
    "convoy.planned_routes": ("route_id",),
    "emulator.emulation_runs": ("run_id",),
    "simulation.runs": ("run_id",),
    "corridor.signal_program_history": ("history_id",),
    "corridor.signal_programs": ("junction_id",),
    "traffic.synthetic_observations": ("synthetic_id",),
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
     "(event_emulator/runner.py), columns default NOW(), and the showcase "
     "polls its runs and re-polls the 108 escalation with sleeps; the "
     "showcase's own assertions never compare timestamps"),
    ("every event id the round created — FLAGSHIP and SWEEP (emulator "
     "runs: RUN-SCOPED, uuid5 over use_case|seed|run_id, event_emulator/"
     "tape.py movement_uuid) and PURSUIT (gen_random_uuid, "
     "traffic_oracle/events/__init__.py) — and every echo of them (movement "
     "subjects, FK references, audit movement_ids, any JSONB value)",
     "relabeled to the tokens FLAGSHIP / SWEEP / PURSUIT (EVENT for any "
     "other) — each round's flagship and sweep ids are read from that "
     "round's showcase_results.json (run_scoped_ids); the round FAILS when "
     "its flagship id names no situation or parameter audit rows, so a "
     "stale or wrong results file can never compare empty-equal"),
    ("corridor.vehicle_sightings.sighting_id (and its echoes in "
     "recommendation evidence, at any depth)",
     "relabeled to SIGHTING:{camera_id}:{plate} (one read per camera/plate "
     "pair, event_emulator/tape.py) — uuid5 over the use case, seed, camera, "
     "plate and run (tape.py anpr_read_uuid), so run-scoped"),
    ("gen_random_uuid surrogate ids: earmark_id, recommendation_id, "
     "dispatch_id, phase_id, route_id, vehicle_id",
     "projected through natural keys (plate; rule_id + sighting token; "
     "agency + unit_id; event token + rank) — column defaults of "
     "migrations 007/010/011; re-created fresh each round after the "
     "baseline restore"),
    ("BIGSERIAL surrogates: situation_log.log_id, "
     "param_overrides.override_id, dispatch_events.seq, the emulator run_id "
     "(and its echoes: event metadata, and the 'event-emulator run N' the "
     "emulator writes into its events' description)",
     "EXCLUDED (the description's N reads <RUN>) — sequence values survive "
     "DELETE (the restore does not and must not reset sequences), so they "
     "differ across rounds by construction (found 2026-10-23 on a private "
     "stack: runs 27/28 vs 29/30)"),
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
     "(traffic_oracle/enforcement/rules.py and the audit-entry "
     "constructor)"),
    ("uuid echoes inside JSONB metadata (vehicle_id, recommendation_id, "
     "event_id, movement_id, run_id)",
     "replaced by <UUID-ECHO> — gen_random_uuid / run-scoped values "
     "re-created per round"),
    ("adaptive.situation_log / adaptive.param_overrides ROW CONTENT and "
     "MULTIPLICITY (the audit tables)",
     "compared through the phase-8 assertion SHAPES: the set of situation "
     "classes seen, the existence of a transitioned row, the existence of "
     "an AMBULANCE_DELAY escalation row, and the DISTINCT full param "
     "documents (params/source/shadow/escalation evidence) — because the "
     "row population is wall-clock SAMPLED: the engine loop classifies "
     "the latest inputs doc every 5 s (engine_loop.py) while the flagship "
     "tape overwrites that doc every ~0.33 s of wall time (tick_sec 10 at "
     "clock_scale 30, the showcase's TICK_SEC / CLOCK_SCALE), so which doc "
     "a tick samples — and how many rows land — is timing alignment, "
     "exactly like a timestamp; showcase_run.sh phase 8 itself asserts "
     "only row counts >= 1, band classes, transitions and AMBULANCE_DELAY "
     "presence"),
    ("adaptive.param_overrides.evidence.situation_code (inside each "
     "compared param document)",
     "replaced by <SAMPLED> — the code of the ONE observation that happened "
     "to trigger the published decision, i.e. which 5 s engine sample of a "
     "tape doc that changes every ~0.33 s landed first (found 2026-10-23, "
     "two rounds on a private stack: the RED class_change document carried "
     "none:CONGESTED:RAIN:EXTREME in one round and none:CONGESTED:RAIN:HIGH "
     "in the other — crowd EXTREME vs HIGH at the tape's ramp — with every "
     "other key identical).  The decision itself (band, params, source, "
     "shadow verdict, escalation, publish_reason, inputs_source) stays "
     "compared: publish_reason and inputs_source did NOT diverge in those "
     "rounds"),
    ("events.enforcement_recommendations rows with "
     "rule_id='R_SIGNAL_VIOLATION', and the R_SIGNAL_VIOLATION entries of "
     "an R_STOLEN_SIGHTING's evidence.withheld_rules",
     "EXCLUDED from the projection — the rule fires iff the read's t "
     "(anchored at the run's wall-clock t0) falls inside the referenced "
     "signal's red phase (enforcement/rules.py phase_state_at over "
     "sighting.t), so its existence — as a row, or as a withheld entry of a "
     "stolen vehicle's evidence row (contract C-N) — is a wall-clock "
     "lottery even with the seed pinned; the showcase never asserts it "
     "(its chain asserts the stolen plate's R_STOLEN_SIGHTING evidence and "
     "its officer approval).  Every other rule's rows and withheld entries "
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
    snap = {"tables": {}}
    with psycopg.connect(DSN) as conn:
        # ONE snapshot across every table: a child row can never be in the
        # baseline while its parent (written a moment later) is not
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
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

def value_sql(table, col):
    if col in GEO_COLS.get(table, {}):
        return "ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)"
    if col in JSONB_HINTS:
        return "%s::jsonb"
    return "%s"

def bind(col, v):
    return json.dumps(v) if col in JSONB_HINTS and v is not None else v

# ── restore: the round's OWN rows only ──────────────────────────────────────
# A row is the round's when the showcase, or an emulator run it started,
# provably wrote it (classify):
#   runs      emulator.emulation_runs created after the baseline for the two
#             use cases the showcase starts (the sweep at the gate's pinned
#             seed, GATE_SWEEP_SEED);
#   events    their events (metadata.source 'event-emulator' + one of those
#             run ids) and the pursuit the scripted officer opened
#             (metadata.opened_by = OFFICER); their phases, routes, status
#             log, dispatches and dispatch lifecycle rows;
#   subjects  convoy movements / planned routes and adaptive audit rows of
#             those events (remembered across rounds in the workdir, so an
#             engine tick that lands after its event is gone is recognised);
#   reads     sightings carrying one of the round's run ids, the
#             recommendations filed on them, the stolen plate's earmark the
#             showcase (or the sweep's scenario precondition) filed, and a
#             vehicle first created by the round whose every read, earmark
#             and recommendation is the round's.
# Everything else that differs from the baseline is FOREIGN — an officer on
# /ops, a living city, another writer — and is never touched: no DELETE, no
# UPDATE, no re-INSERT; it is reported and the restore exits 3 (the gate
# fails: the store was not quiescent).  Two foreign classes are benign and
# only reported: the situation engine's audit rows for subjects the round
# did not create, and baseline reads / vehicles the C6 retention reaper
# deleted (never resurrected — a reaped plate read stays reaped — nor is the
# SET NULL that reap leaves on a recommendation's sighting_id undone).
# The only UPDATE puts back a baseline vehicle whose change is EXACTLY the
# round's own widening (VEHICLE_WIDENING, by the round's reads and earmark
# alone).  Cascade guard: every foreign key INTO the scoped tables (read from
# pg_catalog) is checked before any DELETE — a row that references a row
# about to be deleted must itself be a round row being deleted; otherwise
# (an officer decided a round recommendation, a baseline row re-pointed at a
# round row, an out-of-scope table referencing one) the round is ENTANGLED
# with someone else's work and the restore deletes and reverts NOTHING (exit
# 3).  So no ON DELETE CASCADE / SET NULL can ever reach a row that is not
# the round's.
AUDIT_TABLES = ("adaptive.situation_log", "adaptive.param_overrides")
#: one engine tick (engine_loop.py DEFAULT_POLL_INTERVAL_SEC 5 s) + margin
ENGINE_TICK_SEC = 6.0
#: tables whose vanished baseline rows are the C6 retention reaper's
#: (traffic_oracle/vehicles/store.py REAP_*_SQL) — reported, never resurrected
REAPED_TABLES = ("corridor.vehicle_sightings", "corridor.vehicles")
#: foreign-row lines reported per table (the rest are counted)
REPORT_LIMIT = 12

def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

def _ts(v):
    from datetime import datetime
    if v in (None, ""):
        return None
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))

def _pk_text(key):
    return ",".join(str(x) for x in key)

def round_actor(actor):
    """An actor label the round's own writers use (anything else on a round
    row is someone else's decision or action)."""
    a = str(actor or "")
    return (a in (OFFICER, SHOWCASE_LABEL, EMULATOR_ACTOR)
            or a.startswith("system") or a == "traffic-oracle")

class Plan:
    """What a restore does — and what it must leave alone."""
    def __init__(self):
        self.delete = {t: {} for t in ALL_TABLES}   # pk -> row: the round's own rows
        self.revert = {t: {} for t in ALL_TABLES}   # pk -> baseline row: round-only widenings
        self.foreign = []      # (table, what): someone else's — untouched, the gate fails
        self.entangled = []    # (table, what): mixed with someone else's — nothing restored
        self.benign = []       # (table, what): reported only
        self.runs, self.subjects = set(), set()

    def pending(self):
        return {t: (len(self.delete[t]), len(self.revert[t])) for t in ALL_TABLES
                if self.delete[t] or self.revert[t]}

def classify(base, now, memory=None, sweep_seed=None, edges=FK_EDGES, outside_refs=None):
    """Pure: base/now = {table: {pk: row dict}} -> Plan.  ``outside_refs``
    (child, column, values) -> count of rows of an OUT-OF-SCOPE table that
    reference values about to be deleted (None: there are none)."""
    memory = memory or {}
    plan = Plan()
    new = {t: {k: r for k, r in now[t].items() if k not in base[t]} for t in ALL_TABLES}

    # 1. the round's emulator runs
    runs = {int(x) for x in memory.get("runs", [])}
    for r in new["emulator.emulation_runs"].values():
        spec = r.get("spec") if isinstance(r.get("spec"), dict) else {}
        uc = spec.get("use_case_id")
        if uc == FLAGSHIP_USE_CASE or (uc == SWEEP_USE_CASE and (
                sweep_seed is None or _int(r.get("seed")) == sweep_seed)):
            runs.add(int(r["run_id"]))
    plan.runs = runs

    # 2. its events, and their children
    own = plan.delete
    for k, r in new["events.corridor_events"].items():
        md = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        if ((md.get("source") == "event-emulator" and _int(md.get("run_id")) in runs)
                or (r.get("event_kind") == "vehicle_pursuit" and md.get("opened_by") == OFFICER)):
            own["events.corridor_events"][k] = r
    events = {str(r["event_id"]) for r in own["events.corridor_events"].values()}
    plan.subjects = ({str(s) for s in memory.get("subjects", [])} | events
                     | {str(r["movement_id"]) for r in own["events.corridor_events"].values()
                        if r.get("movement_id")})
    for t in ("events.event_phases", "events.event_routes", "events.event_status_log",
              "corridor.dispatches"):
        own[t] = {k: r for k, r in new[t].items() if str(r.get("event_id")) in events}
    dispatches = {str(r["dispatch_id"]) for r in own["corridor.dispatches"].values()}
    own["corridor.dispatch_events"] = {k: r for k, r in new["corridor.dispatch_events"].items()
                                       if str(r.get("dispatch_id")) in dispatches}
    for t in ("convoy.movements", "convoy.planned_routes") + AUDIT_TABLES:
        own[t] = {k: r for k, r in new[t].items() if str(r.get("movement_id")) in plan.subjects}
    own["emulator.emulation_runs"] = {k: r for k, r in new["emulator.emulation_runs"].items()
                                      if int(r["run_id"]) in runs}

    # 3. its reads, their recommendations, its earmark of the stolen plate
    plate_of = {str(r["vehicle_id"]): r.get("plate") for r in now["corridor.vehicles"].values()}
    own["corridor.vehicle_sightings"] = {k: r for k, r in new["corridor.vehicle_sightings"].items()
                                         if _int(r.get("run_id")) in runs}
    reads = {str(r["sighting_id"]) for r in own["corridor.vehicle_sightings"].values()}
    own["events.enforcement_recommendations"] = {
        k: r for k, r in new["events.enforcement_recommendations"].items()
        if str(r.get("sighting_id")) in reads}
    own["corridor.vehicle_earmarks"] = {
        k: r for k, r in new["corridor.vehicle_earmarks"].items()
        if r.get("earmarked_by") in (SHOWCASE_LABEL, SCENARIO_EARMARKER)
        and plate_of.get(str(r.get("vehicle_id"))) == STOLEN_PLATE}

    # 4. vehicles: a new one wholly the round's goes; a baseline one the
    #    round's reads / earmark ALONE widened gets its baseline values back
    by_vehicle = {}
    for t in ("corridor.vehicle_sightings", "corridor.vehicle_earmarks",
              "events.enforcement_recommendations"):
        for k, r in now[t].items():
            by_vehicle.setdefault((t, str(r.get("vehicle_id"))), []).append(k)
    def refs(t, vid):
        return by_vehicle.get((t, vid), [])
    for k, r in new["corridor.vehicles"].items():
        vid = str(r["vehicle_id"])
        mine = [all(x in own[t] for x in refs(t, vid))
                for t in ("corridor.vehicle_sightings", "corridor.vehicle_earmarks",
                          "events.enforcement_recommendations")]
        touched = refs("corridor.vehicle_sightings", vid) or refs("corridor.vehicle_earmarks", vid)
        if all(mine) and touched:
            own["corridor.vehicles"][k] = r
    for k, b in base["corridor.vehicles"].items():
        n = now["corridor.vehicles"].get(k)
        if n is None or n == b:
            continue
        vid = str(n["vehicle_id"])
        touch = ([_ts(now["corridor.vehicle_sightings"][x].get("t"))
                  for x in refs("corridor.vehicle_sightings", vid)
                  if x in own["corridor.vehicle_sightings"]]
                 + [_ts(now["corridor.vehicle_earmarks"][x].get("earmarked_at"))
                    for x in refs("corridor.vehicle_earmarks", vid)
                    if x in own["corridor.vehicle_earmarks"]])
        other = [x for t in ("corridor.vehicle_sightings", "corridor.vehicle_earmarks")
                 for x in refs(t, vid) if x in new[t] and x not in own[t]]
        diff = sorted(c for c in n if n.get(c) != b.get(c))
        ends = [_ts(b.get("first_seen")), _ts(b.get("last_seen")),
                _ts(n.get("first_seen")), _ts(n.get("last_seen"))]
        if (touch and None not in touch and None not in ends and not other
                and set(diff) <= VEHICLE_WIDENING
                and ends[3] == max([ends[1]] + touch) and ends[2] == min([ends[0]] + touch)):
            plan.revert["corridor.vehicles"][k] = b
        else:
            plan.foreign.append(("corridor.vehicles",
                                 f"{n.get('plate')}: baseline row changed in {diff} — not by the "
                                 "round's reads / earmark alone; left as found"))

    # 5. someone else's decision or action ON a round row
    for t, col in (("events.event_status_log", "actor"), ("corridor.dispatch_events", "actor"),
                   ("corridor.dispatches", "requested_by"), ("corridor.dispatches", "decided_by"),
                   ("events.enforcement_recommendations", "decided_by")):
        for k, r in own[t].items():
            if r.get(col) is not None and not round_actor(r.get(col)):
                plan.entangled.append((t, f"{col} {r.get(col)!r} on round row {_pk_text(k)} — "
                                          "someone else acted on the round's own entity"))

    # 6. cascade guard: whatever references a row about to be deleted must be
    #    a round row being deleted too (ON DELETE CASCADE / SET NULL / NO ACTION)
    for child, ccol, parent, pcol, _action in edges:
        doomed = {str(r.get(pcol)) for r in own.get(parent, {}).values()}
        if not doomed:
            continue
        if child in now:
            for k, r in now[child].items():
                if r.get(ccol) is not None and str(r.get(ccol)) in doomed and k not in own[child]:
                    plan.entangled.append((child, f"row {_pk_text(k)} references a round row of "
                                                  f"{parent} ({ccol}) but is not the round's"))
        elif outside_refs is not None:
            n = outside_refs(child, ccol, sorted(doomed))
            if n:
                plan.entangled.append((child, f"{n} row(s) of {child} (outside the gate's scope) "
                                              f"reference round rows of {parent} ({ccol})"))

    # 7. everything else that differs from the baseline: foreign / benign
    for t in ALL_TABLES:
        extra = [k for k in new[t] if k not in own[t]]
        if extra and t in AUDIT_TABLES:
            plan.benign.append((t, f"{len(extra)} row(s) of subjects the round did not create kept "
                                   "(the situation engine's own audit trail)"))
        elif extra:
            plan.foreign += [(t, f"new row {_pk_text(k)} not written by the round — left in place")
                             for k in extra[:REPORT_LIMIT]]
            if len(extra) > REPORT_LIMIT:
                plan.foreign.append((t, f"... and {len(extra) - REPORT_LIMIT} more new row(s)"))
        gone = [k for k in base[t] if k not in now[t]]
        if gone and t in REAPED_TABLES:
            plan.benign.append((t, f"{len(gone)} baseline row(s) deleted during the gate (the C6 "
                                   "retention reaper) — never resurrected"))
        elif gone:
            plan.foreign.append((t, f"{len(gone)} baseline row(s) deleted during the gate by "
                                    "another writer — never resurrected"))
        if t == "corridor.vehicles":
            continue                                   # step 4
        for k, b in base[t].items():
            n = now[t].get(k)
            if n is None or n == b:
                continue
            diff = sorted(c for c in n if n.get(c) != b.get(c))
            if (t == "events.enforcement_recommendations" and diff == ["sighting_id"]
                    and n.get("sighting_id") is None
                    and (str(b.get("sighting_id")),) not in now["corridor.vehicle_sightings"]):
                plan.benign.append((t, f"{_pk_text(k)}: sighting_id set NULL when the reaper "
                                       "deleted its read (ON DELETE SET NULL) — left as is"))
                continue
            plan.foreign.append((t, f"baseline row {_pk_text(k)} changed in {diff} by another "
                                    "writer — left as found"))
    if plan.entangled:                                  # touch nothing at all
        plan.delete = {t: {} for t in ALL_TABLES}
        plan.revert = {t: {} for t in ALL_TABLES}
    return plan

def baseline_of(snap):
    out = {}
    for table in ALL_TABLES:
        meta = snap["tables"][table]
        cols = meta["cols"]
        pk_idx = [cols.index(c) for c in pk_cols_of(table)]
        out[table] = {tuple(r[i] for i in pk_idx): dict(zip(cols, r)) for r in meta["rows"]}
    return out

def state_of(conn, snap):
    out = {}
    for table in ALL_TABLES:
        cols = snap["tables"][table]["cols"]
        pk_idx = [cols.index(c) for c in pk_cols_of(table)]
        out[table] = {tuple(jsonable(r[i]) for i in pk_idx): dict(zip(cols, jsonable(r)))
                      for r in fetch_table(conn, table, cols)}
    return out

def fk_edges(conn):
    """Every single-column foreign key INTO the scoped tables, from pg_catalog."""
    rows = conn.execute(
        "SELECT c.oid, cn.nspname || '.' || cc.relname, pn.nspname || '.' || pc.relname, "
        "c.confdeltype::text, array_agg(a.attname::text ORDER BY k.ord), "
        "array_agg(af.attname::text ORDER BY k.ord) "
        "FROM pg_constraint c "
        "JOIN pg_class cc ON cc.oid = c.conrelid JOIN pg_namespace cn ON cn.oid = cc.relnamespace "
        "JOIN pg_class pc ON pc.oid = c.confrelid JOIN pg_namespace pn ON pn.oid = pc.relnamespace "
        "CROSS JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(ca, pa, ord) "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.ca "
        "JOIN pg_attribute af ON af.attrelid = c.confrelid AND af.attnum = k.pa "
        "WHERE c.contype = 'f' AND pn.nspname || '.' || pc.relname = ANY(%s) "
        "GROUP BY c.oid, cn.nspname, cc.relname, pn.nspname, pc.relname, c.confdeltype",
        (list(ALL_TABLES),)).fetchall()
    edges = []
    for _oid, child, parent, action, ccols, pcols in rows:
        if len(ccols) != 1:
            raise SystemExit(f"gate_py: multi-column foreign key {child}{ccols} -> {parent}"
                             f"{pcols}: the cascade guard cannot prove it — refusing to restore")
        edges.append((child, ccols[0], parent, pcols[0], action))
    return edges

def outside_refs_of(conn):
    def count(child, col, values):
        schema, name = child.split(".", 1)
        stmt = pgsql.SQL("SELECT count(*) FROM {} WHERE {}::text = ANY(%s)").format(
            pgsql.Identifier(schema, name), pgsql.Identifier(col))
        return conn.execute(stmt, (list(values),)).fetchone()[0]
    return count

def apply_plan(conn, plan):
    """The plan's DELETEs (children first) and in-place reverts, in the
    caller's transaction.  -> {table: (deleted, reverted)}"""
    cur = conn.cursor()
    done = {}
    for table in DELETE_ORDER:
        pk_cols = pk_cols_of(table)
        where = pgsql.SQL(" AND ").join(
            pgsql.SQL("{} = %s").format(pgsql.Identifier(c)) for c in pk_cols)
        schema, name = table.split(".", 1)
        for k in plan.delete[table]:
            cur.execute(pgsql.SQL("DELETE FROM {} WHERE ").format(
                pgsql.Identifier(schema, name)) + where, tuple(k))
        reverts = plan.revert[table]
        if reverts:
            cols = list(next(iter(reverts.values())).keys())
            set_cols = [c for c in cols if c not in pk_cols]
            upd = (pgsql.SQL("UPDATE {} SET ").format(pgsql.Identifier(schema, name))
                   + pgsql.SQL(", ").join(
                       pgsql.SQL("{} = " + value_sql(table, c)).format(pgsql.Identifier(c))
                       for c in set_cols)
                   + pgsql.SQL(" WHERE ") + where)
            for k, row in reverts.items():
                cur.execute(upd, tuple(bind(c, row[c]) for c in set_cols) + tuple(k))
        if plan.delete[table] or reverts:
            done[table] = (len(plan.delete[table]), len(reverts))
    return done

def cmd_restore(workdir):
    """Exit 0: the round's rows are gone and nothing else differs from the
    baseline but benign classes.  Exit 3: foreign activity (left untouched)
    or an entangled round (nothing restored).  SystemExit otherwise."""
    snap = json.loads((Path(workdir) / "baseline.json").read_text())
    base = baseline_of(snap)
    mem_path = Path(workdir) / "round_memory.json"
    memory = json.loads(mem_path.read_text()) if mem_path.exists() else {}
    sweep_seed = _int(os.environ.get("GATE_SWEEP_SEED"))
    done = {}
    with psycopg.connect(DSN) as conn:
        # every pass plans and deletes inside ONE repeatable-read snapshot: a
        # concurrent write to a row it touches aborts the pass (re-planned)
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        edges = fk_edges(conn)
        conn.commit()
        for attempt in range(5):
            try:
                plan = classify(base, state_of(conn, snap), memory, sweep_seed, edges,
                                outside_refs_of(conn))
                memory = {"runs": sorted(plan.runs), "subjects": sorted(plan.subjects)}
                mem_path.write_text(json.dumps(memory))
                pending = plan.pending()
                if plan.entangled or not pending:       # verified (or untouchable)
                    conn.rollback()
                    break
                if done and not set(pending) <= set(AUDIT_TABLES):
                    conn.rollback()
                    raise SystemExit(f"gate_py: restore VERIFY failed — round rows reappeared "
                                     f"after the delete: {pending}")
                for table, (d, rv) in apply_plan(conn, plan).items():
                    od, orv = done.get(table, (0, 0))
                    done[table] = (od + d, orv + rv)
                conn.commit()
            except psycopg.errors.SerializationFailure:
                conn.rollback()                         # a concurrent writer: re-read, re-plan
                time.sleep(1.0)
                continue
            if attempt:
                # an engine tick in flight at the delete landed an audit row
                # for a round subject: removed — let any other land, re-check
                time.sleep(ENGINE_TICK_SEC)
        else:
            raise SystemExit("gate_py: restore VERIFY failed — round rows still present after "
                             "5 passes")
    for table in DELETE_ORDER:
        if table in done:
            d, rv = done[table]
            print(f"[gate_py] restore {table:42} -{d} deleted, {rv} reverted in place")
    if not done and not plan.entangled:
        print("[gate_py] restore: no round row in the store (no-op)")
    for table, what in plan.benign:
        print(f"[gate_py] restore {table:42} kept: {what}")
    if plan.entangled:
        print("[gate_py] restore: ENTANGLED — the round's rows are mixed with someone else's "
              "work; NOTHING was deleted or reverted (a delete would cascade into it):")
        for table, what in plan.entangled:
            print(f"[gate_py]   ENTANGLED {table}: {what}")
    if plan.foreign:
        print("[gate_py] restore: FOREIGN activity since the baseline — left untouched "
              "(the store was not quiescent during the gate):")
        for table, what in plan.foreign:
            print(f"[gate_py]   FOREIGN {table}: {what}")
    pending = plan.pending()
    if pending and not plan.entangled:
        raise SystemExit(f"gate_py: restore VERIFY failed — round rows still present: {pending}")
    if plan.entangled or plan.foreign:
        return 3
    print("[gate_py] restore: verified — no round row left; every baseline row present and "
          "unchanged (benign classes above reported)")
    return 0

def cmd_running(workdir):
    """The round's emulator runs still running (for the exit trap's stop)."""
    snap = json.loads((Path(workdir) / "baseline.json").read_text())
    mem_path = Path(workdir) / "round_memory.json"
    memory = json.loads(mem_path.read_text()) if mem_path.exists() else {}
    with psycopg.connect(DSN) as conn:
        plan = classify(baseline_of(snap), state_of(conn, snap), memory,
                        _int(os.environ.get("GATE_SWEEP_SEED")))
    for r in plan.delete["emulator.emulation_runs"].values():
        if r.get("status") == "running":
            print(int(r["run_id"]))

def cmd_check():
    with psycopg.connect(DSN) as conn:
        for table in ALL_TABLES:
            got = conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]
            if got is None:
                raise SystemExit(f"gate_py: table {table} missing — run "
                                 f"scripts/migrate.py against this store")
        print(f"[gate_py] check: all {len(ALL_TABLES)} scoped tables present; "
              f"the flagship id is run-scoped — read per round from its "
              f"showcase_results.json")

# ── canonical projection ────────────────────────────────────────────────────

def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))

def norm_jsonb(value, extra_keys=None, tokens=None):
    """Normalize a parsed JSONB value.  extra_keys: {key: replacement}
    applied at every depth (identity echoes); tokens: {round-scoped id:
    token} — any string value equal to a round-scoped id, at any depth."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "observation_count":
                out[k] = "<N>"          # in-process counter (service.py)
            elif extra_keys and k in extra_keys:
                out[k] = extra_keys[k]
            else:
                out[k] = norm_jsonb(v, extra_keys, tokens)
        return out
    if isinstance(value, list):
        return [norm_jsonb(v, extra_keys, tokens) for v in value]
    if isinstance(value, str):
        if tokens and value in tokens:
            return tokens[value]         # a round-scoped id echo
        if ISO_RE.match(value):
            return "<TS>"                # wall-clock echo inside JSONB
    return value

# wall-clock-phase numerics inside recommendation evidence (R_SIGNAL rows
# are excluded wholesale — see the projection block; this covers any other
# t0-derived field).  Seed-rendered numerics (speed_kmh, excess_kmh) are
# compared RAW: the gate pins the sweep seed, so they are identical.
REC_EVIDENCE_KEYS = {
    "sec_into_cycle": "<TS>",
}
# uuid echoes inside event metadata (pursuit carries vehicle_id +
# recommendation_id; emulator events carry their run_id)
META_ECHO_KEYS = {
    "vehicle_id": "<UUID-ECHO>",
    "recommendation_id": "<UUID-ECHO>",
    "event_id": "<UUID-ECHO>",
    "movement_id": "<UUID-ECHO>",
    "run_id": "<UUID-ECHO>",
}
#: the red-phase rule: its existence is a wall-clock lottery (manifest)
LOTTERY_RULE = "R_SIGNAL_VIOLATION"
#: param-document evidence sampled from the observation that triggered the
#: decision (manifest) — the decision itself stays compared
AUDIT_SAMPLED_KEYS = {"situation_code": "<SAMPLED>"}
#: the emulator run number an emulator event's description echoes (BIGSERIAL)
RUN_ECHO_RE = re.compile(r"(event-emulator run )\d+")

def without_lottery(evidence):
    """An R_STOLEN_SIGHTING's evidence minus its withheld red-phase entry."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get("withheld_rules"), list):
        return evidence
    out = dict(evidence)
    out["withheld_rules"] = [w for w in evidence["withheld_rules"]
                             if not (isinstance(w, dict) and w.get("rule_id") == LOTTERY_RULE)]
    return out

class RoundIncomparable(Exception):
    """The round's terminal state cannot be compared (named reason)."""

def project_rows(rows, flagship_id, sweep_id=None, plate_of=None):
    """Canonical lines for ONE round.  ``rows``: {table: [row dicts]} — the
    round's own rows of every projected table (the events it created with
    their phases and routes, its recommendations, earmarks, sightings,
    dispatches, dispatch events and new vehicles) plus the flagship's audit
    rows.  The live projection (cmd_project) and the --fast selftest both
    run THIS function, so the selftest exercises the real rules."""
    if not (isinstance(flagship_id, str) and UUID_RE.match(flagship_id)):
        raise RoundIncomparable(f"no flagship event id (got {flagship_id!r})")
    plate_of = dict(plate_of or {})
    for r in rows.get("corridor.vehicles", []):
        plate_of.setdefault(str(r["vehicle_id"]), r.get("plate"))
    lines, stats = [], {}

    # 1. ownership tokens: FLAGSHIP / SWEEP by the ids the round's
    #    showcase_results.json names, PURSUIT by kind, EVENT otherwise
    events = rows.get("events.corridor_events", [])
    event_token = {}
    for r in events:
        eid = str(r["event_id"])
        if eid == flagship_id:
            event_token[eid] = "FLAGSHIP"
        elif r.get("event_kind") == "vehicle_pursuit":
            event_token[eid] = "PURSUIT"
        elif eid == sweep_id or (sweep_id is None and
                                 (r.get("metadata") or {}).get("source") == "event-emulator"):
            event_token[eid] = "SWEEP"
        else:
            event_token[eid] = "EVENT"
    if flagship_id not in event_token:
        raise RoundIncomparable(f"the flagship event {flagship_id} is not one of the round's "
                                f"own events (a stale or wrong showcase_results.json)")
    sightings = sorted(rows.get("corridor.vehicle_sightings", []),
                       key=lambda r: (str(r.get("t") or ""), str(r.get("camera_id") or ""),
                                      str(r.get("plate") or "")))
    sight_token = {str(r["sighting_id"]): f"SIGHTING:{r.get('camera_id')}:{r.get('plate')}"
                   for r in sightings}
    dispatches = rows.get("corridor.dispatches", [])
    dispatch_token = {str(r["dispatch_id"]): f"DISPATCH:{r.get('agency')}:{r.get('unit_id')}"
                      for r in dispatches}
    tokens = {**event_token, **sight_token, **dispatch_token}

    def etok(v):
        return event_token.get(str(v), "-") if v is not None else "-"

    def stok(v):
        return sight_token.get(str(v), "<SIGHTING>")

    def dtok(v):
        return dispatch_token.get(str(v), "<DISPATCH>")

    # ── adaptive audit tables → phase-8 assertion shapes ────────────────
    # showcase_run.sh phase 8 queries these ONLY for the flagship subject;
    # sweep-subject rows are seed-sampled renders (manifest).
    flag_sit = [r for r in rows.get("adaptive.situation_log", [])
                if str(r.get("movement_id")) == flagship_id]
    flag_por = [r for r in rows.get("adaptive.param_overrides", [])
                if str(r.get("movement_id")) == flagship_id]
    if not flag_sit or not flag_por:
        raise RoundIncomparable(
            f"the flagship {flagship_id} carries {len(flag_sit)} situation and "
            f"{len(flag_por)} parameter audit rows — an empty scope is never compared")
    for cls in sorted({r.get("situation_class") for r in flag_sit}):
        lines.append(f"audit_band_seen\t{cls}")
    lines.append(f"audit_transition_present\t{any(r.get('transitioned') for r in flag_sit)}")
    lines.append("audit_ambulance_delay_present\t"
                 f"{any('AMBULANCE_DELAY' in canon(r.get('evidence')) for r in flag_por)}")
    seen_docs = set()
    for r in flag_por:
        line = "\t".join([
            "audit_param_doc", str(r.get("situation_class")),
            str(r.get("source")), str(r.get("shadow_eval")),
            canon(r.get("params")),
            canon(norm_jsonb(r.get("evidence"), AUDIT_SAMPLED_KEYS, tokens))])
        if line not in seen_docs:
            seen_docs.add(line)
            lines.append(line)
    stats["adaptive.situation_log"] = (len(flag_sit),
                                       len({r.get("situation_class") for r in flag_sit}))
    stats["adaptive.param_overrides"] = (len(flag_por), len(seen_docs))

    # ── events.corridor_events (the round's own) ────────────────────────
    for r in sorted(events, key=lambda r: (etok(r["event_id"]), str(r.get("event_kind")))):
        md = norm_jsonb(r.get("metadata"), dict(META_ECHO_KEYS), tokens)
        lines.append("\t".join([
            "events.corridor_events", etok(r["event_id"]),
            str(r.get("event_kind")), str(r.get("protection_class")),
            str(r.get("crowd_class")), str(r.get("title")),
            RUN_ECHO_RE.sub(r"\g<1><RUN>", str(r.get("description"))), str(r.get("status")),
            etok(r.get("movement_id")), canon(md)]))
    stats["events.corridor_events"] = (len(events), len(events))

    # ── event_phases / event_routes ─────────────────────────────────────
    for table, rank, extra in (("events.event_phases", "phase_rank", "closure_policy"),
                               ("events.event_routes", "route_rank", "segment_ids")):
        prows = sorted((r for r in rows.get(table, []) if str(r["event_id"]) in event_token),
                       key=lambda r: (etok(r["event_id"]), int(r.get(rank) or 0)))
        for r in prows:
            lines.append("\t".join([table, etok(r.get("event_id")), str(r.get(rank)),
                                    str(r.get("label")), canon(r.get(extra))]))
        stats[table] = (len(prows), len(prows))

    # ── enforcement_recommendations ─────────────────────────────────────
    # R_SIGNAL_VIOLATION rows — and the R_SIGNAL_VIOLATION entries of an
    # R_STOLEN_SIGHTING's withheld_rules — are EXCLUDED (the manifest: a
    # wall-clock red-phase lottery the showcase never asserts).  Every
    # other rule's rows are compared in FULL: status, decider, free-text
    # render, evidence numerics (the pinned seed makes them identical).
    rrows = [r for r in rows.get("events.enforcement_recommendations", [])
             if r.get("rule_id") != LOTTERY_RULE]
    rrows.sort(key=lambda r: (stok(r.get("sighting_id")), str(r.get("rule_id"))))
    for r in rrows:
        ev = norm_jsonb(without_lottery(r.get("evidence")), REC_EVIDENCE_KEYS, tokens)
        lines.append("\t".join([
            "events.enforcement_recommendations",
            stok(r.get("sighting_id")), str(r.get("rule_id")),
            str(r.get("status")), str(r.get("decided_by") or ""),
            str(r.get("recommendation") or ""),
            canon(ev), canon(norm_jsonb(r.get("audit_tail"), tokens=tokens))]))
    stats["events.enforcement_recommendations"] = (len(rrows), len(rrows))

    # ── vehicle_earmarks ────────────────────────────────────────────────
    erows = sorted(rows.get("corridor.vehicle_earmarks", []),
                   key=lambda r: (str(plate_of.get(str(r.get("vehicle_id")), "")),
                                  str(r.get("lawful_basis"))))
    for r in erows:
        lines.append("\t".join([
            "corridor.vehicle_earmarks",
            str(plate_of.get(str(r.get("vehicle_id")), "<VEHICLE>")),
            str(r.get("state")), str(r.get("reason")),
            str(r.get("lawful_basis")), str(r.get("retention_days")),
            str(r.get("earmarked_by"))]))
    stats["corridor.vehicle_earmarks"] = (len(erows), len(erows))

    # ── vehicle_sightings (speed/heading/position COMPARED RAW — the
    #    pinned sweep seed makes them identical across rounds; t and
    #    run_id stay excluded: wall clock + sequence — manifest) ─────────
    for r in sightings:
        lines.append("\t".join([
            "corridor.vehicle_sightings", stok(r.get("sighting_id")),
            str(r.get("plate")), str(r.get("camera_id")),
            str(r.get("vehicle_class") or ""), str(r.get("source")),
            str(r.get("matched_plate_hit") or ""),
            str(r.get("speed_kmh")), str(r.get("heading")),
            str(r.get("position"))]))
    stats["corridor.vehicle_sightings"] = (len(sightings), len(sightings))

    # ── dispatches / dispatch_events ────────────────────────────────────
    for r in sorted(dispatches, key=lambda r: (str(r.get("agency")), str(r.get("unit_id")))):
        lines.append("\t".join([
            "corridor.dispatches", etok(r.get("event_id")),
            str(r.get("agency")), str(r.get("unit_id")),
            str(r.get("status")), str(r.get("requested_by")),
            str(r.get("decided_by") or ""),
            canon(norm_jsonb(r.get("request_payload"), tokens=tokens))]))
    stats["corridor.dispatches"] = (len(dispatches), len(dispatches))
    derows = sorted(rows.get("corridor.dispatch_events", []),
                    key=lambda r: (dtok(r.get("dispatch_id")), str(r.get("to_status"))))
    for r in derows:
        lines.append("\t".join([
            "corridor.dispatch_events", dtok(r.get("dispatch_id")),
            str(r.get("from_status") or ""), str(r.get("to_status")),
            str(r.get("actor")), str(r.get("note") or "")]))
    stats["corridor.dispatch_events"] = (len(derows), len(derows))

    # ── vehicles (plates created this round) ────────────────────────────
    vround = sorted(rows.get("corridor.vehicles", []), key=lambda r: str(r.get("plate")))
    for r in vround:
        lines.append("\t".join(["corridor.vehicles", str(r.get("plate")),
                                str(r.get("vehicle_class"))]))
    stats["corridor.vehicles"] = (len(vround), len(vround))

    lines.sort()
    ctx = {"flagship_id": flagship_id, "sweep_id": sweep_id,
           "event_tokens": event_token, "sighting_tokens": sight_token,
           "dispatch_tokens": dispatch_token}
    return lines, stats, ctx

def round_ids(results_path):
    """(flagship event id, sweep event id) from a round's showcase_results.json."""
    try:
        res = json.loads(Path(results_path).read_text())
    except (OSError, ValueError) as exc:
        raise RoundIncomparable(f"{results_path} unreadable: {exc}") from exc
    ids = res.get("run_scoped_ids") or {}
    uses = (res.get("flagship") or {}).get("composition_use_cases") or []
    by_case = {u.get("use_case_id"): u for u in uses if isinstance(u, dict)}
    flag = ids.get("flagship_event_id") or (by_case.get(FLAGSHIP_USE_CASE) or {}).get("event_id")
    sweep = ids.get("sweep_event_id") or (by_case.get(SWEEP_USE_CASE) or {}).get("event_id")
    return flag, (sweep if isinstance(sweep, str) and UUID_RE.match(sweep) else None)

def cmd_project(workdir, round_no, results_path):
    snap = json.loads((Path(workdir) / "baseline.json").read_text())
    flagship_id, sweep_id = round_ids(results_path)
    rows = {}
    with psycopg.connect(DSN) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ   # one snapshot
        conn.read_only = True
        def load(table):
            m = snap["tables"][table]
            c = m["cols"]
            pk = pk_cols_of(table)[0]
            base_ids = {str(r[c.index(pk)]) for r in m["rows"]}
            return [dict(zip(c, jsonable(r))) for r in fetch_table(conn, table, c)], base_ids, pk

        def round_created(table):
            all_rows, base_ids, pk = load(table)
            return [r for r in all_rows if str(r[pk]) not in base_ids]

        for table in ("events.corridor_events", "events.event_phases", "events.event_routes",
                      "events.enforcement_recommendations", "corridor.vehicle_earmarks",
                      "corridor.vehicle_sightings", "corridor.dispatches",
                      "corridor.dispatch_events"):
            rows[table] = round_created(table)
        veh_rows, veh_base, _ = load("corridor.vehicles")
        plate_of = {str(r["vehicle_id"]): r.get("plate") for r in veh_rows}
        rows["corridor.vehicles"] = [r for r in veh_rows if str(r["vehicle_id"]) not in veh_base]
        for table in ("adaptive.situation_log", "adaptive.param_overrides"):
            rows[table] = [r for r in load(table)[0]
                           if str(r.get("movement_id")) == str(flagship_id)]
    lines, stats, ctx = project_rows(rows, flagship_id, sweep_id, plate_of)
    out = Path(workdir) / f"round_{round_no}.projection"
    out.write_text("\n".join(lines) + ("\n" if lines else ""))
    ctx["stats"] = {k: list(v) for k, v in stats.items()}
    (Path(workdir) / f"round_{round_no}.context.json").write_text(
        json.dumps(ctx, indent=1, sort_keys=True))
    print(f"[gate_py] round {round_no} projection (flagship {flagship_id} -> FLAGSHIP): "
          f"{len(lines)} canonical lines -> {out}")
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

def build_fixture_rows(run_id, sweep_seed, observation_count, iso,
                       log_id=11, override_id=7,
                       sampled_code="none:CONGESTED:RAIN:EXTREME"):
    """Raw table rows for a synthetic 'round' whose emulator runs are run
    ``run_id`` (flagship) and ``run_id + 1`` (sweep) — RUN-SCOPED ids, as
    the live store holds them; two rounds that differ ONLY in provably-
    volatile values must canonicalize IDENTICALLY.  Returns (rows, ids)."""
    flagship = run_scoped_id(FLAGSHIP_USE_CASE, FLAG_SEED, run_id)
    sweep = run_scoped_id(SWEEP_USE_CASE, sweep_seed, run_id + 1)
    pursuit = str(_uuid.uuid5(_NS, f"pursuit|{run_id}"))              # gen_random_uuid stand-ins
    sighting = run_scoped_id(SWEEP_USE_CASE, sweep_seed, f"{run_id + 1}|EM-CAM-02")
    sighting2 = run_scoped_id(SWEEP_USE_CASE, sweep_seed, f"{run_id + 1}|EM-CAM-04")
    sighting3 = run_scoped_id(SWEEP_USE_CASE, sweep_seed, f"{run_id + 1}|EM-CAM-03|4321")
    rec = str(_uuid.uuid5(_NS, f"rec|{run_id}"))
    rec2 = str(_uuid.uuid5(_NS, f"rec2|{run_id}"))
    rec3 = str(_uuid.uuid5(_NS, f"rec3|{run_id}"))
    earmark = str(_uuid.uuid5(_NS, f"earmark|{run_id}"))
    dispatch = str(_uuid.uuid5(_NS, f"dispatch|{run_id}"))
    vehicle = str(_uuid.uuid5(_NS, f"vehicle|{run_id}"))
    vehicle2 = str(_uuid.uuid5(_NS, f"vehicle2|{run_id}"))
    note = "pursuit authorised on a reported-stolen vehicle (evidence, no fine)"

    def stolen_evidence(sid, camera, speed, withheld):
        return {"sighting_id": sid, "plate": STOLEN_PLATE, "camera_id": camera,
                "t": iso, "lon": 72.5841, "lat": 23.0262, "speed_kmh": speed,
                "heading": 88.1, "segment_id": 1015, "road_name": "Nehru Bridge Road",
                "matched_plate_hit": "stolen_vehicle", "earmark_state": "flagged",
                "lawful_basis": "stolen_vehicle_fir", "fir_ref": "4401/2026",
                "challan_withheld": True, "withheld_reason": "vehicle reported stolen",
                "withheld_rules": withheld, "source": "emulated_anpr"}

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
        ],
        "adaptive.param_overrides": [
            {"override_id": override_id, "movement_id": flagship,
             "vvip_class": "none", "protection_class": "none",
             "situation_class": "RED", "source": "baseline",
             "shadow_eval": "unavailable",
             "params": {"advance_closure_sec": 120, "green_wave": True},
             # movement_id is the RUN-SCOPED flagship id → the FLAGSHIP token;
             # situation_code is the triggering sample (manifest) → <SAMPLED>
             "evidence": {"movement_id": flagship,
                          "observation_count": observation_count,
                          "situation_code": sampled_code,
                          "publish_reason": "class_change",
                          "inputs_source": "emulator",
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
             "description": f"EMULATED use case '{FLAGSHIP_USE_CASE}' (seed {FLAG_SEED}, x30) "
                            f"— event-emulator run {run_id}",
             "status": "completed", "movement_id": flagship,
             "metadata": {"source": "event-emulator", "mode": "use_case",
                          "use_case_id": FLAGSHIP_USE_CASE,
                          "scenario_id": FLAGSHIP_USE_CASE,
                          "seed": FLAG_SEED, "run_id": run_id,
                          "closure_policy": {"closure_type": "signal_hold"},
                          "is_replay": False}},
            {"event_id": sweep, "event_kind": "crime_incident",
             "protection_class": "none", "crowd_class": "LOW",
             "title": "Stolen-Vehicle ANPR Sweep",
             "description": f"EMULATED use case '{SWEEP_USE_CASE}' (seed {sweep_seed}, x60) "
                            f"— event-emulator run {run_id + 1}",
             "status": "completed", "movement_id": sweep,
             "metadata": {"source": "event-emulator", "mode": "use_case",
                          "use_case_id": SWEEP_USE_CASE,
                          "seed": sweep_seed, "run_id": run_id + 1,
                          "is_replay": False}},
            {"event_id": pursuit, "event_kind": "vehicle_pursuit",
             "protection_class": "none", "crowd_class": "LOW",
             "title": f"PURSUIT — stolen {STOLEN_PLATE} (FIR 4401/2026, Kalupur PS)",
             "description": "operator-opened pursuit", "status": "planning",
             "movement_id": None,
             "metadata": {"vehicle_id": vehicle, "recommendation_id": rec,
                          "fir": "FIR 4401/2026, Kalupur PS", "opened_by": OFFICER}},
        ],
        "events.event_phases": [
            {"phase_id": str(_uuid.uuid5(_NS, f"phase|{run_id}")),
             "event_id": flagship, "phase_rank": 1, "label": "procession",
             "window_start": iso, "window_end": iso,
             "closure_policy": {"advance_closure_sec": 0,
                                "closure_type": "signal_hold"}},
        ],
        "events.event_routes": [
            {"route_id": str(_uuid.uuid5(_NS, f"route|{run_id}")),
             "event_id": flagship, "route_rank": 1, "label": "spine",
             "segment_ids": [1005, 1015, 1001, 1006]},
            {"route_id": str(_uuid.uuid5(_NS, f"route2|{run_id}")),
             "event_id": pursuit, "route_rank": 1, "label": "old-city sweep belt",
             "segment_ids": [1015, 1012, 1049, 1013]},
        ],
        "events.enforcement_recommendations": [
            # the stolen plate's reads: EVIDENCE ONLY (C-N) — the newest one
            # approved by the scripted officer as the pursuit's authorisation
            {"recommendation_id": rec, "sighting_id": sighting, "vehicle_id": vehicle,
             "rule_id": "R_STOLEN_SIGHTING",
             "recommendation": "stolen vehicle (FIR 4401/2026) sighted at EM-CAM-02 — "
                               "e-challan withheld, vehicle reported stolen",
             "evidence": stolen_evidence(sighting, "EM-CAM-02", 97.43, [
                 {"rule_id": "R_SPD_SEGMENT",
                  "evidence": {"sighting_id": sighting, "plate": STOLEN_PLATE,
                               "camera_id": "EM-CAM-02", "t": iso,
                               "speed_kmh": 97.43, "speed_limit_kmh": 50,
                               "excess_kmh": 47.43, "tolerance_kmh": 5,
                               "segment_id": 1015, "road_name": "Nehru Bridge Road",
                               "source": "emulated_anpr"}},
                 # the red-phase lottery, withheld: present in some rounds only
                 {"rule_id": "R_SIGNAL_VIOLATION",
                  "evidence": {"sighting_id": sighting, "plate": STOLEN_PLATE,
                               "camera_id": "EM-CAM-02", "t": iso, "junction_id": 3,
                               "movement": "through", "signal_state": "red",
                               "sec_into_cycle": 41.7, "cycle_sec": 100,
                               "source": "emulated_anpr"}}]),
             "status": "approved", "decided_by": OFFICER,
             "audit_tail": [
                 {"actor": "traffic-oracle", "action": "created:pending",
                  "at": iso, "note": "deterministic rule evaluation"},
                 {"actor": OFFICER, "action": "decision:approved", "at": iso, "note": note}]},
            {"recommendation_id": rec2, "sighting_id": sighting2, "vehicle_id": vehicle,
             "rule_id": "R_STOLEN_SIGHTING",
             "recommendation": "stolen vehicle (FIR 4401/2026) sighted at EM-CAM-04 — "
                               "e-challan withheld, vehicle reported stolen",
             "evidence": stolen_evidence(sighting2, "EM-CAM-04", 101.9, []),
             "status": "pending", "decided_by": None,
             "audit_tail": [{"actor": "traffic-oracle", "action": "created:pending",
                             "at": iso, "note": "deterministic rule evaluation"}]},
            # a non-stolen plate's red-phase row: EXCLUDED (the wall-clock
            # lottery, manifest) — present in some rounds only
            {"recommendation_id": rec3, "sighting_id": sighting3, "vehicle_id": vehicle2,
             "rule_id": "R_SIGNAL_VIOLATION",
             "recommendation": "e-challan recommended: GJ27EM4321 crossed junction 3 on red",
             "evidence": {"sighting_id": sighting3, "plate": "GJ27EM4321",
                          "camera_id": "EM-CAM-03", "t": iso, "junction_id": 3,
                          "movement": "through", "signal_state": "red",
                          "sec_into_cycle": 12.5, "cycle_sec": 100,
                          "source": "emulated_anpr"},
             "status": "pending", "decided_by": None, "audit_tail": []},
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
             "plate": STOLEN_PLATE, "camera_id": "EM-CAM-02",
             "t": iso, "position": "POINT(72.5841 23.0262)",
             "speed_kmh": 97.4, "heading": 88.1, "vehicle_class": "car",
             "source": "emulated_anpr", "run_id": run_id + 1,
             "matched_plate_hit": "stolen_vehicle"},
            {"sighting_id": sighting2, "vehicle_id": vehicle,
             "plate": STOLEN_PLATE, "camera_id": "EM-CAM-04",
             "t": iso, "position": "POINT(72.5902 23.0311)",
             "speed_kmh": 101.9, "heading": 12.3, "vehicle_class": "car",
             "source": "emulated_anpr", "run_id": run_id + 1,
             "matched_plate_hit": "stolen_vehicle"},
            {"sighting_id": sighting3, "vehicle_id": vehicle2,
             "plate": "GJ27EM4321", "camera_id": "EM-CAM-03",
             "t": iso, "position": "POINT(72.5877 23.0290)",
             "speed_kmh": 31.0, "heading": 270.0, "vehicle_class": "auto_rickshaw",
             "source": "emulated_anpr", "run_id": run_id + 1,
             "matched_plate_hit": "watch"},
        ],
        "corridor.dispatches": [
            {"dispatch_id": dispatch, "event_id": flagship,
             "agency": "jan_rakshak", "unit_id": "JR-EM-01",
             "waypoint_junction": 88, "status": "on_scene",
             "requested_by": "God's Eye Morning showcase",
             "decided_by": OFFICER,
             "request_payload": {"agency": "jan_rakshak", "unit_id": "JR-EM-01",
                                 "waypoint_junction": 88, "event_id": flagship}},
        ],
        "corridor.dispatch_events": [
            {"seq": 301, "dispatch_id": dispatch, "from_status": None,
             "to_status": "requested", "actor": "God's Eye Morning showcase",
             "note": None, "at": iso},
            {"seq": 302, "dispatch_id": dispatch, "from_status": "requested",
             "to_status": "approved", "actor": OFFICER,
             "note": "citizen-volunteer coordination approved for the spillover", "at": iso},
        ],
        "corridor.vehicles": [
            {"vehicle_id": vehicle, "plate": STOLEN_PLATE,
             "vehicle_class": "car", "first_seen": iso, "last_seen": iso},
            {"vehicle_id": vehicle2, "plate": "GJ27EM4321",
             "vehicle_class": "auto_rickshaw", "first_seen": iso, "last_seen": iso},
        ],
    }
    return rows, {"flagship": flagship, "sweep": sweep}

def _keyed_rows(table, rows):
    return {tuple(r[c] for c in pk_cols_of(table)): r for r in rows}

def restore_fixture(sweep_seed):
    """(base, now, ids): a baseline holding an officer's own work, and the
    store right after ONE finished round — every table a round writes, a
    re-sighted baseline vehicle, a vehicle the round first read, the
    engine's audit row for a subject that predates the gate, and a read the
    C6 retention reaper removed (its recommendation's sighting_id SET NULL)."""
    import copy

    def T(m):
        return f"2026-10-23 10:{m:02d}:00+00:00"
    human = "Duty Officer (fixture)"                    # an officer's own label
    u = {n: str(_uuid.uuid5(_NS, f"restore-fixture|{n}")) for n in (
        "E0", "PH0", "V0", "V1", "V2", "S0", "S1", "S2", "SA", "SB", "SC", "R0", "R1", "R2",
        "RA", "RB", "EM0", "EMR", "P", "D", "PHF", "PHW")}
    F = run_scoped_id(FLAGSHIP_USE_CASE, FLAG_SEED, 51)
    W = run_scoped_id(SWEEP_USE_CASE, sweep_seed, 52)
    base = {t: {} for t in ALL_TABLES}
    base["events.corridor_events"] = _keyed_rows("events.corridor_events", [
        {"event_id": u["E0"], "event_kind": "vip_movement", "status": "planning",
         "movement_id": None, "metadata": {"opened_by": human}}])
    base["events.event_phases"] = _keyed_rows("events.event_phases", [
        {"phase_id": u["PH0"], "event_id": u["E0"], "phase_rank": 1, "label": "transit"}])
    base["events.event_status_log"] = _keyed_rows("events.event_status_log", [
        {"seq": 1, "event_id": u["E0"], "from_status": None, "to_status": "planning",
         "actor": human}])
    base["corridor.vehicles"] = _keyed_rows("corridor.vehicles", [
        {"vehicle_id": u["V0"], "plate": STOLEN_PLATE, "vehicle_class": "car",
         "first_seen": T(0), "last_seen": T(5), "updated_at": T(5)},
        {"vehicle_id": u["V1"], "plate": "GJ01EM2174", "vehicle_class": "car",
         "first_seen": T(1), "last_seen": T(6), "updated_at": T(6)}])
    base["corridor.vehicle_earmarks"] = _keyed_rows("corridor.vehicle_earmarks", [
        {"earmark_id": u["EM0"], "vehicle_id": u["V0"], "state": "flagged",
         "lawful_basis": "stolen_vehicle_fir", "earmarked_by": human, "earmarked_at": T(0)}])
    base["corridor.vehicle_sightings"] = _keyed_rows("corridor.vehicle_sightings", [
        {"sighting_id": u["S0"], "vehicle_id": u["V0"], "run_id": 5, "t": T(5)},
        {"sighting_id": u["S1"], "vehicle_id": u["V1"], "run_id": 5, "t": T(1)},
        {"sighting_id": u["S2"], "vehicle_id": u["V1"], "run_id": 5, "t": T(6)}])
    base["events.enforcement_recommendations"] = _keyed_rows("events.enforcement_recommendations", [
        {"recommendation_id": u["R0"], "sighting_id": u["S0"], "vehicle_id": u["V0"],
         "rule_id": "R_SPD_SEGMENT", "status": "pending", "decided_by": None},
        {"recommendation_id": u["R1"], "sighting_id": u["S2"], "vehicle_id": u["V1"],
         "rule_id": "R_SPD_SEGMENT", "status": "pending", "decided_by": None},
        {"recommendation_id": u["R2"], "sighting_id": u["S1"], "vehicle_id": u["V1"],
         "rule_id": "R_SPD_SEGMENT", "status": "pending", "decided_by": None}])
    base["emulator.emulation_runs"] = _keyed_rows("emulator.emulation_runs", [
        {"run_id": 5, "seed": 1, "status": "completed", "spec": {"use_case_id": SWEEP_USE_CASE}}])
    base["adaptive.situation_log"] = _keyed_rows("adaptive.situation_log", [
        {"log_id": 1, "movement_id": u["E0"], "situation_class": "GREEN"}])
    base["corridor.signal_programs"] = _keyed_rows("corridor.signal_programs", [
        {"junction_id": 1, "cycle_sec": 100}])

    now = copy.deepcopy(base)
    # the C6 reaper removed an expired read; its pending row's sighting_id went NULL
    del now["corridor.vehicle_sightings"][(u["S1"],)]
    now["events.enforcement_recommendations"][(u["R2"],)]["sighting_id"] = None

    def add(table, rows):
        now[table].update(_keyed_rows(table, rows))
    add("emulator.emulation_runs", [
        {"run_id": 51, "seed": FLAG_SEED, "status": "completed",
         "spec": {"use_case_id": FLAGSHIP_USE_CASE}},
        {"run_id": 52, "seed": sweep_seed, "status": "completed",
         "spec": {"use_case_id": SWEEP_USE_CASE, "seed": sweep_seed}}])
    add("events.corridor_events", [
        {"event_id": F, "event_kind": "procession", "status": "completed", "movement_id": F,
         "metadata": {"source": "event-emulator", "run_id": 51}},
        {"event_id": W, "event_kind": "crime_incident", "status": "completed", "movement_id": W,
         "metadata": {"source": "event-emulator", "run_id": 52}},
        {"event_id": u["P"], "event_kind": "vehicle_pursuit", "status": "cancelled",
         "movement_id": None,
         "metadata": {"opened_by": OFFICER, "vehicle_id": u["V0"], "recommendation_id": u["RA"]}}])
    add("events.event_phases", [
        {"phase_id": u["PHF"], "event_id": F, "phase_rank": 1, "label": "procession"},
        {"phase_id": u["PHW"], "event_id": W, "phase_rank": 1, "label": "sweep"}])
    add("events.event_routes", [
        {"route_id": str(_uuid.uuid5(_NS, f"rf|{n}")), "event_id": e, "route_rank": 1}
        for n, e in (("F", F), ("W", W), ("P", u["P"]))])
    add("events.event_status_log", [
        {"seq": 10 + i, "event_id": e, "to_status": s, "actor": a}
        for i, (e, s, a) in enumerate((
            (F, "planning", EMULATOR_ACTOR), (F, "active", EMULATOR_ACTOR),
            (F, "completed", EMULATOR_ACTOR), (W, "planning", EMULATOR_ACTOR),
            (u["P"], "planning", OFFICER), (u["P"], "cancelled", OFFICER)))])
    add("corridor.dispatches", [
        {"dispatch_id": u["D"], "event_id": F, "agency": "jan_rakshak", "unit_id": "JR-EM-01",
         "status": "completed", "requested_by": SHOWCASE_LABEL, "decided_by": OFFICER}])
    add("corridor.dispatch_events", [
        {"seq": 100 + i, "dispatch_id": u["D"], "to_status": s, "actor": a}
        for i, (s, a) in enumerate((
            ("requested", SHOWCASE_LABEL), ("approved", OFFICER), ("en_route", OFFICER),
            ("on_scene", OFFICER), ("completed", "system:event_closed")))])
    add("corridor.vehicle_sightings", [
        {"sighting_id": u["SA"], "vehicle_id": u["V0"], "run_id": 52, "t": T(20)},
        {"sighting_id": u["SB"], "vehicle_id": u["V1"], "run_id": 52, "t": T(21)},
        {"sighting_id": u["SC"], "vehicle_id": u["V2"], "run_id": 52, "t": T(22)}])
    add("events.enforcement_recommendations", [
        {"recommendation_id": u["RA"], "sighting_id": u["SA"], "vehicle_id": u["V0"],
         "rule_id": "R_STOLEN_SIGHTING", "status": "approved", "decided_by": OFFICER},
        {"recommendation_id": u["RB"], "sighting_id": u["SB"], "vehicle_id": u["V1"],
         "rule_id": "R_SPD_SEGMENT", "status": "pending", "decided_by": None}])
    add("corridor.vehicle_earmarks", [
        {"earmark_id": u["EMR"], "vehicle_id": u["V0"], "state": "flagged",
         "lawful_basis": "stolen_vehicle_fir", "earmarked_by": SHOWCASE_LABEL,
         "earmarked_at": T(10)}])
    now["corridor.vehicles"][(u["V0"],)].update(last_seen=T(20), updated_at=T(20))
    now["corridor.vehicles"][(u["V1"],)].update(last_seen=T(21), updated_at=T(21))
    add("corridor.vehicles", [
        {"vehicle_id": u["V2"], "plate": "GJ01EM9999", "vehicle_class": "car",
         "first_seen": T(22), "last_seen": T(22), "updated_at": T(22)}])
    add("convoy.movements", [{"movement_id": F}, {"movement_id": W}])
    add("convoy.planned_routes", [{"route_id": 1001, "movement_id": F},
                                  {"route_id": 1002, "movement_id": W}])
    add("adaptive.situation_log", [
        {"log_id": 2, "movement_id": F}, {"log_id": 3, "movement_id": F},
        {"log_id": 4, "movement_id": u["E0"]}])          # the engine, a pre-existing subject
    add("adaptive.param_overrides", [{"override_id": 1, "movement_id": F}])
    return base, now, {**u, "F": F, "W": W, "human": human}

def fixture_lines(rows, ids, flagship_id=None):
    """The live rules (project_rows) over a fixture round; the flagship id
    defaults to the one the round's results would name."""
    return project_rows(rows, flagship_id or ids["flagship"], ids["sweep"])[0]

def cmd_selftest(workdir):
    print("[gate_py] selftest: offline comparator/normalizer/FAIL-path "
          "proof (no db, no docker, no showcase)")
    wd = Path(workdir)
    # round A vs round B: differ ONLY in provably-volatile values — the
    # run-scoped flagship/sweep ids (and every echo, the emulator run number
    # in the event descriptions included), the pursuit/sighting/rec/dispatch
    # uuids, observation_count, timestamps, sequences, and the sampled
    # situation_code of a published decision.
    rowsA, idsA = build_fixture_rows(run_id=51, sweep_seed=20269999, observation_count=41,
                                     iso="2026-10-18T04:05:06.789+00:00", log_id=11, override_id=7)
    rowsB, idsB = build_fixture_rows(run_id=97, sweep_seed=20269999, observation_count=87,
                                     iso="2026-10-18T09:11:12.001Z", log_id=902, override_id=640,
                                     sampled_code="none:CONGESTED:RAIN:HIGH")
    assert idsA["flagship"] != idsB["flagship"]       # run-scoped: never equal
    # round B's red-phase rule did NOT fire (wall-clock lottery) — neither
    # as a row of its own nor as a withheld entry of the stolen plate's
    # evidence: the exclusion must absorb exactly this difference
    rowsB["events.enforcement_recommendations"] = [
        r for r in rowsB["events.enforcement_recommendations"]
        if r["rule_id"] != "R_SIGNAL_VIOLATION"]
    for r in rowsB["events.enforcement_recommendations"]:
        r["evidence"] = without_lottery(r["evidence"])
    fA, fB = wd / "selftest.roundA", wd / "selftest.roundB"
    fA.write_text("\n".join(fixture_lines(rowsA, idsA)) + "\n")
    fB.write_text("\n".join(fixture_lines(rowsB, idsB)) + "\n")

    failures = 0
    if cmd_compare(str(fA), str(fB)) == 0:
        ok("selftest[1/6] benign volatility (run-scoped flagship/sweep ids "
           "and their echoes, emulator run numbers, pursuit/sighting/rec "
           "uuids, observation_count, timestamps, sequences, a decision's "
           "sampled situation_code, and the wall-clock R_SIGNAL_VIOLATION — a "
           "row and a withheld entry of the stolen plate's R_STOLEN_SIGHTING "
           "— present in A but not B) normalizes away: round A == round B")
    else:
        bad("selftest[1/6] FAIL: rounds differing only in volatile values "
            "compared DIFFERENT")
        failures += 1

    # round C: REAL content differences → the comparator MUST fail and print
    # them: an audit band flips, a decision published for another reason,
    # an extra dispatch lifecycle row, a changed closure policy, a changed
    # sighting speed, a DIFFERENT read approved as the pursuit's
    # authorisation, and a penal e-challan row for the stolen plate (a C-N
    # regression)
    rowsC, idsC = build_fixture_rows(run_id=97, sweep_seed=20269999, observation_count=87,
                                     iso="2026-10-18T09:11:12.001Z", log_id=902, override_id=640)
    rowsC["adaptive.situation_log"][1]["situation_class"] = "BLACK"
    rowsC["adaptive.situation_log"][1]["situation_code"] = "R4_W2_C4"
    rowsC["adaptive.param_overrides"][0]["evidence"]["publish_reason"] = "escalation_change"
    rowsC["corridor.dispatch_events"].append(
        {"seq": 303, "dispatch_id": rowsC["corridor.dispatches"][0]["dispatch_id"],
         "from_status": "approved", "to_status": "en_route", "actor": OFFICER,
         "note": None, "at": "2026-10-18T09:13:00Z"})
    rowsC["events.event_phases"][0]["closure_policy"] = {
        "advance_closure_sec": 60, "closure_type": "hard_closure"}
    # a changed seed-rendered VALUE (sighting speed): compared RAW since the
    # gate pins the sweep seed — must FAIL, proving the tightening
    rowsC["corridor.vehicle_sightings"][0]["speed_kmh"] = 88.8
    recs = rowsC["events.enforcement_recommendations"]
    recs[0]["status"], recs[0]["decided_by"] = "pending", None
    recs[1]["status"], recs[1]["decided_by"] = "approved", OFFICER
    recs.append({"recommendation_id": "cafebabe-0000-4000-8000-0000000000c1",
                 "sighting_id": recs[1]["sighting_id"], "vehicle_id": recs[1]["vehicle_id"],
                 "rule_id": "R_SPD_SEGMENT",
                 "recommendation": f"e-challan recommended: {STOLEN_PLATE} read at 102 km/h "
                                   "in a 50 km/h segment",
                 "evidence": {"sighting_id": recs[1]["sighting_id"], "speed_kmh": 101.9},
                 "status": "pending", "decided_by": None, "audit_tail": []})
    fC = wd / "selftest.roundC"
    fC.write_text("\n".join(fixture_lines(rowsC, idsC)) + "\n")
    if cmd_compare(str(fA), str(fC)) == 1:
        ok("selftest[2/6] real nondeterminism (band flip, a decision's "
           "publish_reason, extra lifecycle row, changed closure policy, "
           "changed sighting speed, a different read approved, a penal row "
           "for the stolen plate) FAILS the comparator — rows printed above")
    else:
        bad("selftest[2/6] FAIL: a genuinely different round compared "
            "EQUAL — the gate would hide real nondeterminism")
        failures += 1

    # round D: its showcase_results.json names a flagship id that is not the
    # round's own flagship event (a stale results file from another run) —
    # NOT comparable: the projection must refuse, never compare an empty
    # flagship scope as equal
    rowsD, idsD = build_fixture_rows(run_id=97, sweep_seed=20269999, observation_count=87,
                                     iso="2026-10-18T09:11:12.001Z", log_id=902, override_id=640)
    stale = run_scoped_id(FLAGSHIP_USE_CASE, FLAG_SEED, 96)
    refused = []
    for flagship_id in (stale, None):
        try:
            fixture_lines(rowsD, idsD, flagship_id=flagship_id or "not-a-uuid")
        except RoundIncomparable as exc:
            refused.append(str(exc))
    # and a round whose flagship carries no audit rows at all
    rowsD["adaptive.situation_log"] = []
    try:
        fixture_lines(rowsD, idsD)
    except RoundIncomparable as exc:
        refused.append(str(exc))
    if len(refused) == 3:
        ok("selftest[3/6] a stale/wrong flagship id from showcase_results.json "
           "or a flagship with no audit rows FAILS the round (never compared "
           "empty-equal): " + "; ".join(refused))
    else:
        bad(f"selftest[3/6] FAIL: only {len(refused)} of 3 incomparable rounds "
            "were refused — a wrong flagship id could pass silently")
        failures += 1

    failures += selftest_restore()
    print()
    print_manifest()
    return 1 if failures else 0

def selftest_restore():
    """selftest[4-6]: the restore's ownership classifier on fixtures — the
    round's own rows only, nobody else's ever, nothing at all when the
    round is entangled with someone else's work.  -> failures"""
    import copy
    seed = 20261105
    failures = 0
    base, now, u = restore_fixture(seed)
    want = {"events.corridor_events": 3, "events.event_phases": 2, "events.event_routes": 3,
            "events.event_status_log": 6, "corridor.dispatches": 1, "corridor.dispatch_events": 5,
            "corridor.vehicle_sightings": 3, "events.enforcement_recommendations": 2,
            "corridor.vehicle_earmarks": 1, "corridor.vehicles": 1, "convoy.movements": 2,
            "convoy.planned_routes": 2, "adaptive.situation_log": 2,
            "adaptive.param_overrides": 1, "emulator.emulation_runs": 2}

    def deletes(plan):
        return {t: len(v) for t, v in plan.delete.items() if v}

    def never_baseline(plan, base):
        return all(k not in base[t] for t in ALL_TABLES for k in plan.delete[t])

    # [4] a clean round: exactly its rows go; the two vehicles it re-sighted
    #     get their baseline values back; the engine's row for the officer's
    #     event, the reaped read and its SET NULL are left (benign)
    plan = classify(base, now, {}, seed)
    reverted = set(plan.revert["corridor.vehicles"])
    if (deletes(plan) == want and reverted == {(u["V0"],), (u["V1"],)}
            and not plan.foreign and not plan.entangled and len(plan.benign) == 3
            and never_baseline(plan, base)):
        ok("selftest[4/6] restore of a clean round: deletes exactly the round's rows "
           "(3 events + children, 3 reads, 2 recommendations, its earmark, the vehicle it "
           "first read, 2 movements, its audit rows, 2 runs) — never a baseline row — and "
           "reverts only the 2 vehicles its own reads widened; the engine's row for an "
           "officer's event, a reaped read and its SET NULL are kept, never resurrected")
    else:
        bad(f"selftest[4/6] FAIL: clean-round plan wrong: deletes {deletes(plan)} reverts "
            f"{sorted(reverted)} foreign {plan.foreign} entangled {plan.entangled} "
            f"benign {plan.benign}")
        failures += 1

    # [5] foreign activity meanwhile — an officer approves a baseline
    #     recommendation, earmarks a vehicle the round also read, moves their
    #     own event on, a signal program changes: none of it is touched (the
    #     restore reports it and exits 3); the round's rows still go
    fnow = copy.deepcopy(now)
    r1 = fnow["events.enforcement_recommendations"][(u["R1"],)]
    r1.update(status="approved", decided_by=u["human"])
    fnow["corridor.vehicle_earmarks"].update(_keyed_rows("corridor.vehicle_earmarks", [
        {"earmark_id": str(_uuid.uuid5(_NS, "officer-watch")), "vehicle_id": u["V1"],
         "state": "watch", "lawful_basis": "court_order", "earmarked_by": u["human"],
         "earmarked_at": "2026-10-23 10:30:00+00:00"}]))
    fnow["events.corridor_events"][(u["E0"],)]["status"] = "approved"
    fnow["events.event_status_log"].update(_keyed_rows("events.event_status_log", [
        {"seq": 40, "event_id": u["E0"], "to_status": "approved", "actor": u["human"]}]))
    fnow["corridor.signal_programs"][(1,)]["cycle_sec"] = 120
    plan = classify(base, fnow, {}, seed)
    touched = {(t, k) for t in ALL_TABLES for k in list(plan.delete[t]) + list(plan.revert[t])}
    officer_rows = {("events.enforcement_recommendations", (u["R1"],)),
                    ("events.corridor_events", (u["E0"],)), ("events.event_status_log", (40,)),
                    ("corridor.signal_programs", (1,)), ("corridor.vehicles", (u["V1"],))}
    if (not plan.entangled and len(plan.foreign) == 6 and not (touched & officer_rows)
            and set(plan.revert["corridor.vehicles"]) == {(u["V0"],)}
            and deletes(plan) == want and never_baseline(plan, base)):
        ok("selftest[5/6] foreign activity during the gate (an officer's approval of a baseline "
           "recommendation, their earmark on a vehicle the round read, their status change, a "
           "signal program edit) is NEVER deleted, reverted or re-inserted — 6 FOREIGN lines "
           "(exit 3), and the round's own rows still go")
    else:
        bad(f"selftest[5/6] FAIL: foreign activity mishandled: foreign {plan.foreign} "
            f"touched {sorted(touched & officer_rows)} reverts "
            f"{sorted(plan.revert['corridor.vehicles'])} entangled {plan.entangled}")
        failures += 1

    # [6] ENTANGLED: an officer decided one of the round's own
    #     recommendations / a baseline row re-pointed at a round event / an
    #     out-of-scope table references a round movement: the restore deletes
    #     and reverts NOTHING (a delete would cascade or SET NULL into it)
    cases = []
    enow = copy.deepcopy(now)
    enow["events.enforcement_recommendations"][(u["RB"],)].update(
        status="rejected", decided_by=u["human"])
    cases.append(("an officer's decision on a round recommendation",
                  classify(base, enow, {}, seed)))
    enow = copy.deepcopy(now)
    enow["events.event_phases"][(u["PH0"],)]["event_id"] = u["F"]
    cases.append(("a baseline phase re-pointed at a round event",
                  classify(base, enow, {}, seed)))
    cases.append(("an out-of-scope convoy.diversions row on a round movement",
                  classify(base, now, {}, seed,
                           outside_refs=lambda child, col, vals:
                           1 if child == "convoy.diversions" and u["F"] in vals else 0)))
    nothing = [name for name, p in cases
               if p.entangled and not p.pending() and not any(p.revert.values())]
    if len(nothing) == len(cases):
        ok("selftest[6/6] an ENTANGLED round restores NOTHING (" + "; ".join(nothing) + ")")
    else:
        bad(f"selftest[6/6] FAIL: entangled rounds still planned deletes: "
            f"{[(n, p.pending(), p.entangled) for n, p in cases if n not in nothing]}")
        failures += 1
    return failures

def ok(msg):  print(f"[gate_py]   OK   {msg}")
def bad(msg): print(f"[gate_py]   FAIL {msg}")

def main(argv):
    if len(argv) < 2:
        print("usage: gate_py.py {selftest|check|snapshot|restore|running|project|"
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
        return cmd_restore(rest[0])
    if cmd == "running":
        cmd_running(rest[0]); return 0
    if cmd == "project":
        if len(rest) < 3:
            print("usage: gate_py.py project WORKDIR ROUND RESULTS_JSON", file=sys.stderr)
            return 2
        try:
            cmd_project(rest[0], int(rest[1]), rest[2])
        except RoundIncomparable as exc:
            print(f"[gate_py] round {rest[1]} NOT COMPARABLE: {exc}", file=sys.stderr)
            return 1
        return 0
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
export GATE_SWEEP_SEED          # gate_py: the sweep run the round started is the one at this seed

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
                         "need the migrate venv (see scripts/migrate.py)")
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

# ── exit trap (pass, fail, die, Ctrl-C, TERM) ───────────────────────────────
# A round still in the store has its running emulator runs stopped and its
# own rows restored (ownership-scoped, as every restore); when the gate
# recreated traffic-oracle to switch the signal-policy writer on and has
# not yet put it back, it is recreated with its PRE-GATE value.
BASELINE_TAKEN=0          # the baseline snapshot exists: a restore is possible
STORE_DIRTY=0             # a round may hold rows no restore has removed yet
FOREIGN_ACTIVITY=0        # a restore met foreign / entangled rows (left untouched)
GATE_WRITER_RECREATED=""  # set by reset_oracle just before it recreates the oracle
GATE_WRITER_ORIG_ENV=""   # the oracle container's VVIP_SIGNAL_POLICY_WRITER before that
GATE_WRITER_RESTORED=""   # true once the pre-gate value is back (verified)
on_exit() {
    local rc=$? rrc
    trap - EXIT INT TERM
    if declare -F deck_signout >/dev/null; then deck_signout; fi
    if [ "$BASELINE_TAKEN" = 1 ] && [ "$STORE_DIRTY" = 1 ]; then
        log "exit: a round is still in the store — stopping its running emulator runs, then restoring its own rows"
        if declare -F stop_round_runs >/dev/null; then stop_round_runs; fi
        "$GATE_PY_BIN" "$GATE_PY" restore "$WORKDIR"; rrc=$?
        case "$rrc" in
            0) ok "exit: the in-flight round's own rows restored" ;;
            3) bad "exit: foreign / entangled rows were left untouched (listed above)"
               [ "$rc" -eq 0 ] && rc=1 ;;
            *) bad "exit: restore FAILED (gate_py exit $rrc) — the store may still hold the round's rows"
               printf '     FIX: %s\n' "re-run with this gate's PG_* and GATE_SWEEP_SEED env: $GATE_PY_BIN $GATE_PY restore $WORKDIR" >&2
               [ "$rc" -eq 0 ] && rc=1 ;;
        esac
    fi
    if [ -n "$GATE_WRITER_RECREATED" ] && [ "$GATE_WRITER_RESTORED" != true ]; then
        restore_gate_writer || { [ "$rc" -eq 0 ] && rc=3; }
    fi
    exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ════════════════════════════════════════════════════════════════════════════
# 0. preflight
# ════════════════════════════════════════════════════════════════════════════
log "workdir: $WORKDIR"

[ -x "$GATE_PY_BIN" ] || die "migrate-venv python not found: $GATE_PY_BIN" \
    "set GATE_PY_BIN to a python with psycopg, or VEER_VENV_DIR (veer.env) to the venvs holding migrate/ — the venv scripts/migrate.py uses"
"$GATE_PY_BIN" -c "import psycopg" 2>/dev/null || die "psycopg missing in $GATE_PY_BIN" \
    "$GATE_PY_BIN -m pip install 'psycopg[binary]'"

for c in "$PG_CONTAINER" "$VALKEY_CONTAINER" "$NATS_CONTAINER" "$ORACLE_CONTAINER" "$BRAIN_CONTAINER" "$INGRESS_CONTAINER" "$DECK_CONTAINER" "$EMULATOR_CONTAINER"; do
    [ -n "$c" ] || continue   # served outside docker here: judged over HTTP below
    [ "$(docker inspect "$c" --format '{{.State.Running}}' 2>/dev/null)" = "true" ] \
        || die "container $c is not running" "bash $VVIP_DIR/scripts/veer_up.sh (the live stack) — or bring up the private stack this gate is pointed at"
done

[ "$(hcode "$ORACLE_URL/health" 15)" = "200" ] \
    || die "traffic-oracle $ORACLE_URL/health not 200" "docker logs $ORACLE_CONTAINER --tail 50"
b=$(health "$BRAIN_URL/health" 15)
printf '%s' "$b" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("status")=="ok" and d.get("strategic_tier",{}).get("model_host")=="connected" and d.get("tactical_tier",{}).get("llama_server")=="connected" else 1)' 2>/dev/null \
    || die "convoy-brain $BRAIN_URL/health not ok (decider+gemma must be connected)" \
        "check the host models (systemctl --user status veer-models.target), then docker restart $BRAIN_CONTAINER; body was: $b"
[ "$(hcode "$DECK_URL/" 15)" = "200" ] || die "command-deck $DECK_URL/ not 200" "${DECK_CONTAINER:+docker logs $DECK_CONTAINER --tail 50}"
# the deck's /api/v1 proxy sits behind the operator PIN gate: one sign-in,
# one probe, signed out again at once (each showcase round signs itself in)
# shellcheck source=scripts/deck_session.sh
. "$SCRIPT_DIR/deck_session.sh"
deck_signin "$DECK_URL" "Repetition gate (automated check)" \
    || die "no operator session on $DECK_URL (deck_signin said why, above)" \
        "the PIN comes from VEER_OPERATOR_PIN, VEER_OPERATOR_PIN_FILE (a private deck's PIN file) or veer.env; one attempt per run — the deck throttles failed sign-ins"
deck_code=$(deck_curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$DECK_URL/api/v1/movements/active" 2>/dev/null)
deck_signout
[ "$deck_code" = "200" ] || die "deck /api/v1 proxy not answering 200 (got $deck_code)" "${DECK_CONTAINER:+docker logs $DECK_CONTAINER --tail 50}"
e=$(health "$EMULATOR_URL/health" 15)
printf '%s' "$e" | python3 -c 'import json,sys; d=json.load(sys.stdin); s=d.get("stack",{}); sys.exit(0 if s.get("store") and s.get("valkey") and s.get("twin") and s.get("nats") else 1)' 2>/dev/null \
    || die "event-emulator $EMULATOR_URL/health cannot reach its seams (body: $e)" \
        "the emulator must point at this store/valkey/nats/oracle; docker restart ${EMULATOR_CONTAINER:-<the emulator>}"
[ "$(hcode "$DECIDER_URL/decider/health" 15)" = "200" ] || die "decider $DECIDER_URL down" "systemctl --user restart veer-decider.service"
[ "$(hcode "$LLAMA_URL/health" 15)" = "200" ] || die "llama-server $LLAMA_URL down" "systemctl --user restart veer-llama.service"
[ "$(hcode "$EMBED_URL/health" 15)" = "200" ] || die "embedding-service $EMBED_URL down" "systemctl --user restart veer-embed.service"

"$GATE_PY_BIN" "$GATE_PY" check || die "scoped-table check failed against $PG_CONTAINER" \
    "POSTGIS_CONTAINER=$PG_CONTAINER python3 $VVIP_DIR/scripts/migrate.py (apply 001-020 to this store)"
ok "preflight: target fleet + host models + emulator + scoped schema — all reachable"

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

# ── guard (read-only; argus_full_smoke.sh pattern) ──────────────────────────
# Every vvip-* container outside the target compose project: against the
# live argus fleet that is the retired original stack; against a private
# stack it includes the live argus fleet itself.  Their images are guarded
# by the tags they run (tag -> image id) — not every vvip-*/veer-* tag of a
# shared build box, where unrelated test images come and go.
guarded_containers() {
    docker ps -a --filter 'name=vvip-' --format '{{.Names}}' 2>/dev/null \
        | grep -v -- "^${COMPOSE_PROJECT}-"
}
snapshot_original_containers() {
    guarded_containers | while read -r name; do
              docker inspect "$name" --format \
                '{{.Name}}|{{.Config.Image}}|{{.State.Status}}|{{.State.Running}}|{{.State.ExitCode}}|{{.RestartCount}}|{{.State.StartedAt}}' \
                2>/dev/null
          done | sort
}
snapshot_original_images() {
    guarded_containers | while read -r name; do
              docker inspect "$name" --format '{{.Config.Image}}' 2>/dev/null
          done | sort -u | while read -r tag; do
              printf '%s|%s\n' "$tag" "$(docker image inspect "$tag" --format '{{.Id}}' 2>/dev/null)"
          done
}
snapshot_original_containers > "$WORKDIR/original.containers.before"
snapshot_original_images     > "$WORKDIR/original.images.before"
n_guard_c=$(wc -l < "$WORKDIR/original.containers.before")
n_guard_i=$(wc -l < "$WORKDIR/original.images.before")
ok "containers outside the target fleet guarded read-only ($n_guard_c container(s), $n_guard_i image tag(s) they run)"

# ════════════════════════════════════════════════════════════════════════════
# 1. baseline snapshot
# ════════════════════════════════════════════════════════════════════════════
log "phase 1: baseline snapshot of every showcase-writable table (store $PG_CONTAINER/$PG_DB)"
"$GATE_PY_BIN" "$GATE_PY" snapshot "$WORKDIR" || die "baseline snapshot failed" \
    "check $PG_CONTAINER health and credentials (POSTGIS_PASSWORD env; default dev_changeme per compose)"
BASELINE_TAKEN=1

# Compose may recreate the oracle only when $ORACLE_CONTAINER IS that
# service's container: compose stamps a config hash on the containers it
# creates, while a `docker run` of the compose-built image (a private stack)
# carries only the image's project label.
compose_owns_oracle() {
    local labels
    labels="$(docker inspect "$ORACLE_CONTAINER" --format '{{index .Config.Labels "com.docker.compose.project"}} {{index .Config.Labels "com.docker.compose.config-hash"}}' 2>/dev/null)" || return 1
    [ "${labels%% *}" = "$COMPOSE_PROJECT" ] && [ -n "${labels#* }" ]
}
# The signal-policy writer flag as the oracle reports it: True | False | error
writer_flag() { health "$ORACLE_URL/api/v1/signals/plans" 15 | python3 -c 'import json,sys; print(json.load(sys.stdin).get("writer_enabled"))' 2>/dev/null || echo error; }
# The VVIP_SIGNAL_POLICY_WRITER value the oracle container runs with ('' when empty or unset)
oracle_writer_env() {
    docker inspect "$ORACLE_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
        | sed -n 's/^VVIP_SIGNAL_POLICY_WRITER=//p' | tail -n 1
}
# Recreate the oracle with VVIP_SIGNAL_POLICY_WRITER=<value> (showcase_run.sh's
# twin): compose for a compose-owned oracle (veer.env loaded for the
# interpolation, as scripts/veer_up.sh does it; --no-deps --no-build), a
# private stack's only through ORACLE_WRITER_CMD; anything else refused (3).
recreate_oracle_writer() { # <value>
    if [ -n "${ORACLE_WRITER_CMD:-}" ]; then
        VVIP_SIGNAL_POLICY_WRITER="$1" timeout 300 bash -c "$ORACLE_WRITER_CMD"
    elif compose_owns_oracle; then
        ( cd "$INFRA_DIR" && . "$VVIP_DIR/scripts/veer_env.sh" \
            && export VVIP_SIGNAL_POLICY_WRITER="$1" \
            && timeout 300 docker compose -p "$COMPOSE_PROJECT" $COMPOSE_FILES up -d --no-deps --no-build traffic-oracle )
    else
        return 3
    fi
}
wait_writer() { # <True|False> — the oracle answers /health and reports that writer flag (≤ 120 s)
    local i
    for i in $(seq 1 60); do
        [ "$(hcode "$ORACLE_URL/health" 5)" = "200" ] && [ "$(writer_flag)" = "$1" ] && return 0
        sleep 2
    done
    return 1
}
restore_gate_writer() { # the oracle back to its pre-gate writer value (verified)
    log "restoring traffic-oracle's signal-policy writer to its pre-gate value '${GATE_WRITER_ORIG_ENV}' (recreate, --no-deps)"
    if recreate_oracle_writer "$GATE_WRITER_ORIG_ENV" && wait_writer False \
       && [ "$(oracle_writer_env)" = "$GATE_WRITER_ORIG_ENV" ]; then
        GATE_WRITER_RESTORED=true
        ok "traffic-oracle restored to its pre-gate state: VVIP_SIGNAL_POLICY_WRITER='${GATE_WRITER_ORIG_ENV}', writer_enabled False, /health 200"
        return 0
    fi
    GATE_WRITER_RESTORED=false
    bad "traffic-oracle NOT restored to its pre-gate writer state ('${GATE_WRITER_ORIG_ENV}') — the city signal-plan writer may still be ON"
    printf '     FIX: %s\n' "${ORACLE_WRITER_CMD:+VVIP_SIGNAL_POLICY_WRITER='$GATE_WRITER_ORIG_ENV' $ORACLE_WRITER_CMD}${ORACLE_WRITER_CMD:-cd $INFRA_DIR && (. $VVIP_DIR/scripts/veer_env.sh && VVIP_SIGNAL_POLICY_WRITER='$GATE_WRITER_ORIG_ENV' docker compose -p $COMPOSE_PROJECT $COMPOSE_FILES up -d --no-deps --no-build traffic-oracle) — or bash $VVIP_DIR/scripts/veer_up.sh}" >&2
    return 1
}

# oracle reset helper: fresh process with the writer flag ON, converged.
# When the writer is off it is switched on by a recreate — once, remembering
# the pre-gate value the gate puts back at its end (or the exit trap does).
reset_oracle() {
    local writer deadline
    writer=$(writer_flag)
    if [ "$writer" != "True" ]; then
        [ -n "${ORACLE_WRITER_CMD:-}" ] || compose_owns_oracle \
            || die "signal-policy writer disabled on $ORACLE_CONTAINER, a container compose project $COMPOSE_PROJECT did not create (a private stack) — not recreating it" \
                "set ORACLE_WRITER_CMD to the command that recreates $ORACLE_CONTAINER with VVIP_SIGNAL_POLICY_WRITER from its environment (restored on exit), or recreate it with VVIP_SIGNAL_POLICY_WRITER=1 yourself, then re-run"
        if [ -z "$GATE_WRITER_RECREATED" ]; then
            GATE_WRITER_ORIG_ENV="$(oracle_writer_env)"
            GATE_WRITER_RECREATED=1     # armed BEFORE the recreate: even a failed one is put back
        fi
        log "signal-policy writer disabled — recreating traffic-oracle with VVIP_SIGNAL_POLICY_WRITER=1 for the gate (put back to '${GATE_WRITER_ORIG_ENV}' at its end)"
        recreate_oracle_writer 1 \
            || die "could not recreate traffic-oracle with the writer flag" \
                "${ORACLE_WRITER_CMD:+VVIP_SIGNAL_POLICY_WRITER=1 $ORACLE_WRITER_CMD}${ORACLE_WRITER_CMD:-cd $INFRA_DIR && (. $VVIP_DIR/scripts/veer_env.sh && VVIP_SIGNAL_POLICY_WRITER=1 docker compose -p $COMPOSE_PROJECT $COMPOSE_FILES up -d --no-deps --no-build traffic-oracle)}"
    else
        log "restarting traffic-oracle $ORACLE_CONTAINER (fresh process: in-memory adaptive state cannot be DB-restored)"
        docker restart "$ORACLE_CONTAINER" >/dev/null \
            || die "docker restart $ORACLE_CONTAINER failed" "docker logs $ORACLE_CONTAINER --tail 50"
    fi
    deadline=$(( $(date +%s) + CONVERGE_TIMEOUT_SEC ))
    until [ "$(hcode "$ORACLE_URL/health" 5)" = "200" ]; do
        [ "$(date +%s)" -ge "$deadline" ] && die "traffic-oracle did not converge within ${CONVERGE_TIMEOUT_SEC}s after reset" "docker logs $ORACLE_CONTAINER --tail 80"
        sleep 2
    done
    until [ "$(writer_flag)" = "True" ]; do
        [ "$(date +%s)" -ge "$deadline" ] && die "traffic-oracle writer flag never came up after reset" "docker exec $ORACLE_CONTAINER env | grep VVIP_SIGNAL_POLICY_WRITER (must be 1)"
        sleep 2
    done
    ok "traffic-oracle reset + converged (writer ENABLED${GATE_WRITER_RECREATED:+ for the gate; pre-gate value '${GATE_WRITER_ORIG_ENV}' restored at its end})"
}

# A restore between / after rounds: 0 = the round's own rows gone, the
# store as found; 1 = foreign / entangled rows met and left untouched
# (reported by gate_py; the gate fails).  Anything else is fatal.
restore_store() { # <label>
    local rrc
    "$GATE_PY_BIN" "$GATE_PY" restore "$WORKDIR"; rrc=$?
    case "$rrc" in
        0) STORE_DIRTY=0; return 0 ;;
        3) STORE_DIRTY=0; FOREIGN_ACTIVITY=1
           bad "$1: the store was not quiescent — foreign / entangled rows (listed above) were left untouched: nobody else's row was deleted, reverted or re-inserted"
           return 1 ;;
        *) die "$1 failed (gate_py exit $rrc)" "see [gate_py] output above; re-run with this gate's PG_* and GATE_SWEEP_SEED env: $GATE_PY_BIN $GATE_PY restore $WORKDIR" ;;
    esac
}

# ════════════════════════════════════════════════════════════════════════════
# 2. rounds
# ════════════════════════════════════════════════════════════════════════════
# A failed round's emulator runs may still be running (C-G runs outlive the
# showcase process): stop them before its rows are restored (≤ ~60 s).
stop_round_runs() {
    local ids id n
    for n in $(seq 1 30); do
        ids=$("$GATE_PY_BIN" "$GATE_PY" running "$WORKDIR" 2>/dev/null) || return 0
        [ -n "$ids" ] || return 0
        for id in $ids; do
            log "stopping the round's emulator run $id (still running)"
            curl -s -m 10 -o /dev/null -X POST "$EMULATOR_URL/api/v1/emulator/runs/$id/stop" || true
        done
        sleep 2
    done
}

SHOWCASE_LOG=""; ROUND_FAILED=0
for r in $(seq 1 "$ROUNDS"); do
    log "=== round $r/$ROUNDS ==="
    log "round $r: restore-to-baseline (idempotent)"
    if ! restore_store "round $r pre-restore"; then ROUND_FAILED=1; break; fi

    reset_oracle

    log "round $r: running showcase_run.sh (env overrides; sweep seed PINNED to $GATE_SWEEP_SEED; artifacts in $SHOWCASE_OUT_DIR)"
    SHOWCASE_LOG="$WORKDIR/round_${r}.showcase.log"
    rm -f "$SHOWCASE_OUT_DIR/showcase_results.json"
    STORE_DIRTY=1
    if env \
        ORACLE_URL="$ORACLE_URL" BRAIN_URL="$BRAIN_URL" EMULATOR_URL="$EMULATOR_URL" \
        DECK_URL="$DECK_URL" DECIDER_URL="$DECIDER_URL" LLAMA_URL="$LLAMA_URL" EMBED_URL="$EMBED_URL" \
        COMPOSE_PROJECT="$COMPOSE_PROJECT" COMPOSE_FILES="$COMPOSE_FILES" \
        POSTGIS_CONTAINER="$PG_CONTAINER" VALKEY_CONTAINER="$VALKEY_CONTAINER" NATS_CONTAINER="$NATS_CONTAINER" \
        ORACLE_CONTAINER="$ORACLE_CONTAINER" BRAIN_CONTAINER="$BRAIN_CONTAINER" \
        INGRESS_CONTAINER="$INGRESS_CONTAINER" DECK_CONTAINER="$DECK_CONTAINER" \
        EMULATOR_CONTAINER="$EMULATOR_CONTAINER" SHOWCASE_OUT_DIR="$SHOWCASE_OUT_DIR" \
        SWEEP_SEED="$GATE_SWEEP_SEED" \
        bash "$SCRIPT_DIR/showcase_run.sh" 2>&1 | tee "$SHOWCASE_LOG"; then
        ok "round $r: showcase PASS (its own assertions held)"
    else
        bad "round $r: showcase FAILED — terminal state not comparable (log: $SHOWCASE_LOG)"
        ROUND_FAILED=1
        log "round $r: restore-to-baseline after the failed round (its own rows only)"
        stop_round_runs
        restore_store "round $r post-failure restore" || true
        break
    fi
    cp -f "$SHOWCASE_OUT_DIR/showcase_results.json" "$WORKDIR/round_${r}.results.json" 2>/dev/null || true

    log "round $r: capturing canonical terminal-state projection (flagship id from round_${r}.results.json)"
    if ! "$GATE_PY_BIN" "$GATE_PY" project "$WORKDIR" "$r" "$WORKDIR/round_${r}.results.json"; then
        bad "round $r: terminal state NOT COMPARABLE (see the [gate_py] reason above)"
        ROUND_FAILED=1
        restore_store "round $r post-failure restore" || true
        break
    fi

    log "round $r: restore-to-baseline (leave the store as found)"
    if ! restore_store "round $r post-restore"; then ROUND_FAILED=1; break; fi
done

# the oracle back to its pre-gate writer value (the exit trap retries on
# any other way out)
WRITER_RC=0
if [ -n "$GATE_WRITER_RECREATED" ]; then
    restore_gate_writer || WRITER_RC=1
fi

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

# guarded containers, after everything
snapshot_original_containers > "$WORKDIR/original.containers.after"
snapshot_original_images     > "$WORKDIR/original.images.after"
GUARD_RC=0
for kind in containers images; do
    if ! diff -u "$WORKDIR/original.$kind.before" "$WORKDIR/original.$kind.after" > "$WORKDIR/original.$kind.diff" 2>/dev/null; then
        GUARD_RC=1
        bad "guarded $kind CHANGED during the gate:"
        sed 's/^/      /' "$WORKDIR/original.$kind.diff" >&2
    fi
done
[ "$GUARD_RC" -eq 0 ] && ok "guarded containers UNTOUCHED ($n_guard_c container(s) + $n_guard_i image tag(s) byte-identical, incl. StartedAt)"

echo "────"
"$GATE_PY_BIN" "$GATE_PY" manifest

echo "────"
if [ "$FOREIGN_ACTIVITY" -ne 0 ]; then
    echo "${BOLD}REPETITION GATE FAIL${RESET} — the store was not quiescent during the gate: foreign / entangled rows (listed by [gate_py] above) were left untouched — no operator may work on the stack during the gate; re-run when it is idle"
    exit 1
fi
if [ "$ROUND_FAILED" -ne 0 ]; then
    echo "${BOLD}REPETITION GATE FAIL${RESET} — a round failed or was not comparable; see $WORKDIR/round_*.showcase.log"
    exit 1
fi
if [ "$COMPARE_RC" -ne 0 ]; then
    echo "${BOLD}REPETITION GATE FAIL${RESET} — terminal-state projections DIFFER across rounds (real nondeterminism; differing rows printed above); artifacts: $WORKDIR"
    exit 1
fi
if [ "$GUARD_RC" -ne 0 ]; then
    echo "${BOLD}REPETITION GATE FAIL${RESET} — a guarded container was touched during the gate (diff above)"
    exit 1
fi
if [ "$WRITER_RC" -ne 0 ]; then
    echo "${BOLD}REPETITION GATE FAIL${RESET} — traffic-oracle could not be put back to its pre-gate signal-policy writer value (FIX above)"
    exit 1
fi
echo "${BOLD}REPETITION GATE PASS${RESET} — $ROUNDS round(s) of God's Eye Morning produced IDENTICAL canonical terminal-state projections from identical baselines (only the manifest-listed volatility normalized)"
echo "      workdir: $WORKDIR"
echo "      note: the store was restored to its pre-gate baseline (the rounds' own rows only — nothing else touched); traffic-oracle's signal-policy writer is $([ -n "$GATE_WRITER_RECREATED" ] && echo "back at its pre-gate value '${GATE_WRITER_ORIG_ENV}' (verified)" || echo "as found (it was already on)"); $SHOWCASE_OUT_DIR holds the last round's showcase artifacts"
exit 0
