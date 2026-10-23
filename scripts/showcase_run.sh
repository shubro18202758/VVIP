#!/usr/bin/env bash
# showcase_run.sh — the FLAGSHIP demonstration: "GOD'S EYE MORNING".
#
# One Monday monsoon morning over the old city, end to end, no arguments:
#
#   09:20  The 148th Rath Yatra is three chariots deep on Gandhi Road
#          (EXTREME crowd, lakhs on the street). A monsoon burst at 18 mm/h
#          waterlogs the Nehru Bridge approach. Behind the Swami
#          Vivekananda Road barricades the bazaar stands. (use_case
#          rath_yatra_monsoon_ambulance — procession + monsoon + market
#          spillover, injected through the event emulator's REAL HTTP API
#          into the RUNNING fleet.)
#   09:2x  A 108 cardiac ambulance requests a green corridor across the
#          river — the escalation channel decides AMBULANCE_DELAY mid-
#          chaos and the twin's audited schedule opens 4 green-corridor
#          windows on Relief Road -> Nehru Bridge Road -> Ashram Road
#          (South) -> Riverfront Road (West).
#   09:3x  A STOLEN VEHICLE is reported (FIR, plate GJ27EM9001). The full
#          chain runs through the real stack — vehicles only, and never a
#          fine for the victim's car (C-N):
#            plate EARMARKED (flagged, lawful basis stolen_vehicle_fir)
#            -> emulated ANPR camera sightings (the sweep belt's mid-block
#               and stop-line cameras, matched_plate_hit 'stolen_vehicle',
#               source 'emulated_anpr')
#            -> one EVIDENCE-ONLY R_STOLEN_SIGHTING per read: the penal
#               rules still run (90-115 km/h in a 40-60 zone), but their
#               e-challan is WITHHELD — the registered owner is the victim
#               — PENDING, a proposal only
#            -> OFFICER APPROVAL of the newest sighting: 'pursuit
#               authorised on a reported-stolen vehicle (evidence, no
#               fine)'; an e-challan approval on the plate is REFUSED (409)
#            -> PURSUIT EVENT opened by the operator (vehicle_pursuit
#               corridor event carrying vehicle_id + recommendation_id)
#            -> INTERCEPTION PLAN (pgRouting choke-junction ranking off
#               the latest sighting)
#            -> LIVE TRACE on the God's-eye canvas (the vehicle-traces
#               layer renders GET /api/v1/vehicles/{id}/trace)
#            -> the scripted pursuit CLOSED when the run ends (C-F
#               cancelled): nothing an unattended script opened stays
#               open on /ops or holds a band for the situation engine.
#   09:4x  A Jan Rakshak unit is dispatched to the Swami Vivekananda Road
#          spillover: requested -> officer APPROVED -> en_route ->
#          on_scene, every transition audited, the emulated unit projected
#          live.
#   09:5x  The city signal-policy engine answers the situation band: the
#          twin calibration replay is run (honest numbers recorded), the
#          operator applies the LIVE band's city plan, and the C7
#          shadow-eval guard REFUSES the commit while the twin is
#          uncalibrated — fail-closed BY DESIGN, fully audited in
#          GET /api/v1/signals/plans (last_plan: band, planned junctions,
#          shadow outcome). Ruling on record: guard-fired + auditable =
#          this element PASSES; signalPlanShifts counts COMMITTED writes.
#
# What this script proves, in order:
#   1. HEALTH   — the fleet's containers + host models + emulator (judged
#                 on connectivity) + deck (one operator sign-in), fail
#                 fast with the exact fix hint if not (and it brings up
#                 the one thing the flagship needs: VVIP_SIGNAL_POLICY_WRITER
#                 on traffic-oracle — via compose, and only on the oracle
#                 container compose created — for THIS RUN ONLY: phase 8d
#                 (or, on any other way out, the exit trap) recreates the
#                 oracle with its pre-run value, so a stack whose writer is
#                 off is left with it off).
#   2. ACTION   — every beat above through the emulator + real APIs.
#   3. ASSERT   — the real stack did the work, each verifiable from
#                 API/store state: audit-log decision rows, band
#                 transitions, green-corridor windows, the signal-policy
#                 guard decision, and every pursuit-chain step.
#   4. EVIDENCE — headless SwiftShader screenshots of the CANVAS (the
#                 SvelteKit command-deck's /gods-eye Cesium 3D city, signed
#                 in, and the /citizen advisory as the public sees it — the
#                 pursuit and every plate kept off it), each proven
#                 non-blank at the pixel level, into the output dir
#                 (VVIP/showcase/).
#   5. RESULT   — showcase_results.json in the output dir (with this
#                 run's emulator run ids and event ids) + nonzero exit on
#                 ANY failed assertion.
#
# Approvals: in the judged demo a signed-in HUMAN approves on /ops; this
# script runs unattended, so every approval it makes (the Jan Rakshak
# dispatch, the stolen-vehicle sighting that authorises the pursuit) is
# recorded under the officer "Scripted demo officer (showcase script)".
#
# Usage: bash VVIP/scripts/showcase_run.sh     (no arguments)
# Env:   CHROME_PATH optional (defaults exactly like verify_all.sh /
#        frontend/tests/e2e / gods-eye-smoke.mjs — the puppeteer cache).
#        Endpoints/containers are env-overridable with the live (argus)
#        defaults of scripts/veer_env.sh (see the block below):
#        *_URL/*_PORT for the services, DECK_URL/DECK_PORT for the deck,
#        COMPOSE_PROJECT / COMPOSE_FILES / *_CONTAINER for the fleet
#        (DECK_CONTAINER= or EMULATOR_CONTAINER= empty: that service is
#        served outside docker, e.g. a private deck preview — judged over
#        HTTP only).
#        FLAG_RUN_TIMEOUT_SEC / SWEEP_RUN_TIMEOUT_SEC bound the emulator
#        run polls (C-G runs are asynchronous).
#        ORACLE_WRITER_CMD: for an oracle compose did NOT create (a private
#        stack's `docker run`), the command that recreates it with
#        VVIP_SIGNAL_POLICY_WRITER taken from its environment — run with
#        the value 1 for this run and with the pre-run value at its end.
#        Unset (the default), such an oracle is refused, never recreated.
#        SHOWCASE_OUT_DIR: where the artifacts and showcase_results.json
#        land (default VVIP/showcase — the committed evidence; point it
#        elsewhere for any run against a private stack).
#        The deck's operator gate: signed in through scripts/deck_session.sh
#        (PIN from VEER_OPERATOR_PIN, VEER_OPERATOR_PIN_FILE or veer.env).
#
# Security: every URL below is an internal endpoint (an env-overridable
# config constant of this script, http scheme, loopback default) — none
# comes from external input, so no host validation is required before
# these requests (the convoy-brain http_guard carve-out for platform
# services).

set -uo pipefail

VVIP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHOWCASE_DIR="${SHOWCASE_OUT_DIR:-$VVIP_DIR/showcase}"
INFRA_DIR="$VVIP_DIR/infra"

# ── internal endpoints (env-overridable constants, loopback-only defaults) ──
# The hooks.server.ts pattern in URL form: *_PORT tunes the port, *_URL
# overrides the whole origin. Defaults are the live argus stack's
# (scripts/veer_env.sh — not sourced: it would export veer.env, the
# operator PIN included, into every child of this script).
ORACLE_URL="${ORACLE_URL:-http://127.0.0.1:${ORACLE_PORT:-8091}}"
BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:${BRAIN_PORT:-8092}}"
EMULATOR_URL="${EMULATOR_URL:-http://127.0.0.1:${EMULATOR_PORT:-8090}}"
DECIDER_URL="${DECIDER_URL:-http://127.0.0.1:${DECIDER_PORT:-8083}}"
LLAMA_URL="${LLAMA_URL:-http://127.0.0.1:${LLAMA_PORT:-8084}}"
EMBED_URL="${EMBED_URL:-http://127.0.0.1:${EMBED_PORT:-8085}}"
DECK_CONTAINER_URL="${DECK_URL:-http://127.0.0.1:${DECK_PORT:-5273}}"
# The ONE deck: vvip-argus-command-deck serves the SvelteKit deck on :5273 —
# same origin serves /gods-eye, /citizen and the /api/v1 reverse proxy
# (src/hooks.server.ts), the proxy behind the operator PIN gate. One
# origin, one constant: the canvas origin IS the deck.
DECK_ORIGIN="$DECK_CONTAINER_URL"

# ── fleet addressing (compose project + derived container names) ────────────
# COMPOSE_PROJECT drives the derived container names (project-service), so a
# parallel stack needs one override, not eight; any single *_CONTAINER can
# still be pinned explicitly (DECK_CONTAINER= / EMULATOR_CONTAINER= empty:
# served outside docker, judged over HTTP only).
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
PG_USER="${POSTGIS_USER:-corridor_admin}"
PG_DB="${POSTGIS_DB:-corridor_db}"
PSQL="docker exec $PG_CONTAINER psql -U $PG_USER -d $PG_DB -At"

# ── the morning's cast ──────────────────────────────────────────────────────
# Every emulator run is a fresh subject: its movement/event id is uuid5 over
# use_case|seed|run_id (event_emulator/tape.py movement_uuid), so this run's
# ids are READ from the run document (GET /api/v1/emulator/runs/{id}),
# never derived here.
FLAGSHIP_ID="rath_yatra_monsoon_ambulance"
SWEEP_ID="watchlist_anpr_sweep"
SWEEP_BASE_SEED=20261105
# Fresh per run by default; overridable via env so the repetition gate can
# PIN the seed (identical tapes -> identical sightings/rules/terminal state
# across rounds; the historical default behavior is preserved byte-for-byte).
SWEEP_SEED="${SWEEP_SEED:-$((SWEEP_BASE_SEED + (EPOCHSECONDS % 899999)))}"
STOLEN_PLATE="GJ27EM9001"                                    # tape.py _STOLEN_LABEL
OFFICER="Scripted demo officer (showcase script)"            # every unattended approval
FIR_REF="FIR 4401/2026, Kalupur PS"

CLOCK_SCALE=30        # flagship: 60 sim-min in 120 s wall (mid-chaos room)
SWEEP_CLOCK=60        # sweep:    45 sim-min in 45 s wall
TICK_SEC=10
# C-G runs are asynchronous (202 at once): bounds on the progress polls
FLAG_RUN_TIMEOUT_SEC="${FLAG_RUN_TIMEOUT_SEC:-600}"
SWEEP_RUN_TIMEOUT_SEC="${SWEEP_RUN_TIMEOUT_SEC:-300}"
RESULTS_JSON="$SHOWCASE_DIR/showcase_results.json"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; CYAN=$'\033[0;36m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
log()  { printf '%s[godseye]%s %s\n' "$CYAN" "$RESET" "$*"; }
ok()   { printf '%s[ ok ]%s %s\n' "$GREEN" "$RESET" "$*"; }
bad()  { printf '%s[FAIL]%s %s\n' "$RED" "$RESET" "$*"; }

mkdir -p "$SHOWCASE_DIR"
FAILURES=0
declare -a FAILED_ASSERTS=()
fail() { FAILURES=$((FAILURES + 1)); FAILED_ASSERTS+=("$1"); bad "$1"; }

health() { curl -s --max-time "${2:-10}" "$1" 2>/dev/null; }
hcode()  { curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-10}" "$1" 2>/dev/null; }

die_hint() { # <problem> <hint>
    bad "$1"
    printf '     FIX: %s\n' "$2" >&2
    exit 2
}

jget() { # <json-file> <dotted.key> — the value ('' when absent; objects as JSON)
    python3 -c '
import json, sys
v = json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    v = v.get(k) if isinstance(v, dict) else None
print("" if v is None else json.dumps(v) if isinstance(v, (dict, list)) else v)' "$1" "$2" 2>/dev/null
}

# The deck's operator gate: its /api/v1 proxy answers 403 without a session.
# One sign-in per run through the shared helper (the PIN is read at call
# time and never echoed), signed out on exit.
# shellcheck source=scripts/deck_session.sh
. "$VVIP_DIR/scripts/deck_session.sh"

# When phase 1b recreated the oracle to switch the signal-policy writer on,
# phase 8d recreates it again with its PRE-RUN value (the run leaves the
# writer as it found it); the exit trap does it on any other way out
# (die_hint, Ctrl-C, TERM) and signs out.  A restore that cannot be verified
# fails the run (exit 3 from the trap).
WRITER_RECREATED=""      # set by phase 1b just before it recreates the oracle
WRITER_ORIG_ENV=""       # the oracle container's VVIP_SIGNAL_POLICY_WRITER before that
WRITER_RESTORED=""       # true once the pre-run value is back (verified)
restore_writer() {
    log "restoring traffic-oracle's signal-policy writer to its pre-run value '${WRITER_ORIG_ENV}' (recreate, --no-deps)"
    if recreate_oracle_writer "$WRITER_ORIG_ENV" && wait_writer False \
       && [ "$(oracle_writer_env)" = "$WRITER_ORIG_ENV" ]; then
        WRITER_RESTORED=true
        ok "traffic-oracle restored to its pre-run state: VVIP_SIGNAL_POLICY_WRITER='${WRITER_ORIG_ENV}', writer_enabled False, /health 200"
        return 0
    fi
    WRITER_RESTORED=false
    bad "traffic-oracle NOT restored to its pre-run writer state ('${WRITER_ORIG_ENV}') — the city signal-plan writer may still be ON"
    printf '     FIX: %s\n' "${ORACLE_WRITER_CMD:+VVIP_SIGNAL_POLICY_WRITER='$WRITER_ORIG_ENV' $ORACLE_WRITER_CMD}${ORACLE_WRITER_CMD:-cd $INFRA_DIR && (. $VVIP_DIR/scripts/veer_env.sh && VVIP_SIGNAL_POLICY_WRITER='$WRITER_ORIG_ENV' docker compose -p $COMPOSE_PROJECT $COMPOSE_FILES up -d --no-deps --no-build traffic-oracle) — or bash $VVIP_DIR/scripts/veer_up.sh}" >&2
    return 1
}
on_exit() {
    local rc=$?
    trap - EXIT INT TERM
    deck_signout
    if [ -n "$WRITER_RECREATED" ] && [ "$WRITER_RESTORED" != true ]; then
        restore_writer || { [ "$rc" -eq 0 ] && rc=3; }
    fi
    exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# C-G emulator runs are asynchronous: POST answers 202 at once and the run
# goes on inside the emulator; GET /runs/{id}?include_tape=false is the
# light progress poll (status, progress{frame,total,delivered,failed},
# event_id; spec.reason once a run failed or was interrupted).
emu_start() { # <request-json> <out-json> — prints the run_id (202), else the refusal
    EMULATOR="$EMULATOR_URL" python3 - "$1" "$2" <<'PYEOF'
import json, os, sys, urllib.error, urllib.request
req = urllib.request.Request(os.environ["EMULATOR"] + "/api/v1/emulator/runs",
    data=sys.argv[1].encode(), headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        code, doc = r.status, json.loads(r.read())
except urllib.error.HTTPError as e:
    code = e.code
    try:
        doc = json.loads(e.read())
    except ValueError:
        doc = {}
except OSError as e:
    code, doc = 0, {"detail": f"{type(e).__name__}: {e}"}
json.dump({"http": code, **doc}, open(sys.argv[2], "w"), indent=1)
if code == 202 and doc.get("run_id") is not None:
    print(doc["run_id"])
    sys.exit(0)
# a 409 names the run holding the use case: "... is already running as run N"
print(f"HTTP {code}: {doc.get('detail') or doc}")
sys.exit(1)
PYEOF
}
emu_poll() { # <run_id> <out-json> <timeout-sec> <event|end> — polls every 2 s (bounded), prints the status
    EMULATOR="$EMULATOR_URL" python3 - "$@" <<'PYEOF'
import json, os, sys, time, urllib.request
run_id, out, timeout, until = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
url = f"{os.environ['EMULATOR']}/api/v1/emulator/runs/{run_id}?include_tape=false"
deadline = time.monotonic() + timeout
doc, error = {}, None
while True:
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            doc, error = json.loads(r.read()), None
    except (OSError, ValueError) as e:
        error = f"{type(e).__name__}: {e}"
    status = doc.get("status")
    if (until == "event" and doc.get("event_id")) or (until == "end" and status not in (None, "running")):
        break
    if time.monotonic() >= deadline:
        break
    time.sleep(2.0)
spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
json.dump({
    "run_id": doc.get("run_id"), "use_case_id": doc.get("use_case_id"), "status": status,
    "reason": spec.get("reason"), "event_id": doc.get("event_id"),
    "movement_id": doc.get("movement_id"), "event_status": doc.get("event_status"),
    "seed": doc.get("seed"), "clock_scale": doc.get("clock_scale"),
    "progress": doc.get("progress"), "delivery": spec.get("delivery"),
    "event_activation": spec.get("event_activation"),
    "scenario_precondition": spec.get("scenario_precondition"),
    "created_at": doc.get("created_at"), "ended_at": doc.get("ended_at"),
    "poll_error": error,
}, open(out, "w"), indent=1)
print(status or "unknown")
PYEOF
}

# Compose may recreate the oracle only when $ORACLE_CONTAINER IS that
# service's container: compose stamps a config hash on the containers it
# creates, while a `docker run` of the compose-built image (a private stack)
# carries only the image's project label — so an env-pointed run can never
# recreate the live oracle by accident.
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
# Recreate the oracle with VVIP_SIGNAL_POLICY_WRITER=<value>: a compose-owned
# one through compose (veer.env loaded for the interpolation, as
# scripts/veer_up.sh does it; --no-deps --no-build: nothing else is touched
# or built), a private stack's one only through ORACLE_WRITER_CMD; anything
# else is refused (3).  Bounded: a hung recreate cannot wedge the run.
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

is_uuid() { [[ "$1" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; }

# ════════════════════════════════════════════════════════════════════════════
# 1. HEALTH — fleet + host models + emulator + deck, fail fast
# ════════════════════════════════════════════════════════════════════════════
log "phase 1: fleet + host-model health (fail fast)"

n_fleet=0
for c in "$PG_CONTAINER" "$VALKEY_CONTAINER" "$NATS_CONTAINER" "$ORACLE_CONTAINER" "$BRAIN_CONTAINER" "$INGRESS_CONTAINER" "$DECK_CONTAINER" "$EMULATOR_CONTAINER"; do
    [ -n "$c" ] || continue   # served outside docker here: judged over HTTP below
    [ "$(docker inspect "$c" --format '{{.State.Running}}' 2>/dev/null)" = "true" ] \
        || die_hint "container $c is not running" \
            "bash $VVIP_DIR/scripts/veer_up.sh (the live stack) — or bring up the private stack this run is pointed at"
    n_fleet=$((n_fleet + 1))
done
pg_ok=$(docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" 2>&1) \
    || die_hint "postgis not answering: $pg_ok" "docker logs $PG_CONTAINER --tail 50"
[ "$(docker exec "$VALKEY_CONTAINER" valkey-cli ping 2>/dev/null)" = "PONG" ] \
    || die_hint "valkey not answering PONG" "docker logs $VALKEY_CONTAINER --tail 50"

[ "$(hcode "$ORACLE_URL/health")" = "200" ] \
    || die_hint "traffic-oracle $ORACLE_URL/health not 200" "docker logs $ORACLE_CONTAINER --tail 50"
cb=$(health "$BRAIN_URL/health" 15)
printf '%s' "$cb" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("status")=="ok" and d.get("strategic_tier",{}).get("model_host")=="connected" and d.get("tactical_tier",{}).get("llama_server")=="connected" else 1)' 2>/dev/null \
    || die_hint "convoy-brain $BRAIN_URL/health not ok (decider+gemma must be connected)" \
        "check the host models (systemctl --user status veer-models.target), then docker restart $BRAIN_CONTAINER; body was: $cb"
# The ONE deck: GET / IS the Convoy Command cockpit — the restored VVIP
# template home (design-smoke contract: 200 + data-template="convoy-command");
# /ops remains the situation wall on the SAME origin the canvas uses.
deck_root="$(health "$DECK_CONTAINER_URL/" 15)"
if [ "$(hcode "$DECK_CONTAINER_URL/" 15)" = "200" ] \
   && printf '%s' "$deck_root" | grep -q 'data-template="convoy-command"' \
   && [ "$(hcode "$DECK_CONTAINER_URL/ops" 15)" = "200" ]; then
    ok "command-deck $DECK_CONTAINER_URL serving the cockpit (200 + convoy-command template; /ops 200)"
else
    die_hint "command-deck $DECK_CONTAINER_URL not serving the cockpit (want / = 200 + convoy-command template, /ops = 200)" \
        "cd $INFRA_DIR && docker compose -p $COMPOSE_PROJECT $COMPOSE_FILES up -d --no-deps --build command-deck"
fi

# Emulator health is judged on CONNECTIVITY: every seam it probes for real
# (store, Valkey, NATS, the oracle) must answer. A 'degraded' status for
# another reason (the last run failed, the living city idle) is reported,
# never fatal.
emu=$(health "$EMULATOR_URL/health" 15)
emu_state=$(printf '%s' "$emu" | python3 -c '
import json, sys
d = json.load(sys.stdin)
s = d.get("stack") or {}
down = [k for k in ("store", "valkey", "nats", "twin") if not s.get(k)]
print("down " + ",".join(down) if down else "up " + " | ".join(d.get("degraded_reasons") or []))' 2>/dev/null || echo "down unreachable")
case "$emu_state" in
    "up ") ok "event-emulator $EMULATOR_URL wired (store, valkey, nats, oracle all answer)" ;;
    up\ *) ok "event-emulator $EMULATOR_URL wired (store, valkey, nats, oracle all answer); degraded, not down: ${emu_state#up }" ;;
    *) die_hint "event-emulator $EMULATOR_URL cannot reach: ${emu_state#down } (body: $emu)" \
           "docker restart ${EMULATOR_CONTAINER:-<the emulator>} (the live stack: bash $VVIP_DIR/scripts/veer_up.sh)" ;;
esac
lib=$(health "$EMULATOR_URL/api/v1/emulator/use-cases")
n_lib=$(printf '%s' "$lib" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)
[ "$n_lib" -ge 10 ] || die_hint "emulator library has $n_lib use cases (needs both flagship scenarios)" \
    "docker restart ${EMULATOR_CONTAINER:-<the emulator>} so it reloads use_case_library.json (10 entries)"
printf '%s' "$lib" | grep -q "\"$FLAGSHIP_ID\"" || die_hint "emulator library lacks $FLAGSHIP_ID" "docker restart ${EMULATOR_CONTAINER:-<the emulator>}"
printf '%s' "$lib" | grep -q "\"$SWEEP_ID\""     || die_hint "emulator library lacks $SWEEP_ID" "docker restart ${EMULATOR_CONTAINER:-<the emulator>}"

printf '%s' "$(health "$DECIDER_URL/decider/health")" | grep -q '"loaded": *true' \
    || die_hint "decider $DECIDER_URL not loaded" "systemctl --user restart veer-decider.service (pplx-decider-v1-27b)"
[ "$(hcode "$LLAMA_URL/health" 15)" = "200" ] \
    || die_hint "llama-server $LLAMA_URL not answering" "systemctl --user restart veer-llama.service (aditi-ps18-v7h-12b)"
[ "$(hcode "$EMBED_URL/health" 15)" = "200" ] \
    || die_hint "embedding-service $EMBED_URL not answering" "systemctl --user restart veer-embed.service"

# 1b. the signal-policy writer flag: phase 4d needs it ON.  When it is off
# (the live default — scripts/veer_up.sh sets it only with --demo) the oracle
# is recreated with it ON for this run, and phase 8d (or the exit trap)
# recreates it with its PRE-RUN value (recreate_oracle_writer: compose for a
# compose-owned oracle, ORACLE_WRITER_CMD for a private stack's; any other
# is refused).
writer_on=$(writer_flag)
if [ "$writer_on" != "True" ]; then
    [ -n "${ORACLE_WRITER_CMD:-}" ] || compose_owns_oracle \
        || die_hint "signal-policy writer disabled on $ORACLE_CONTAINER, a container compose project $COMPOSE_PROJECT did not create (a private stack) — not recreating it" \
            "set ORACLE_WRITER_CMD to the command that recreates $ORACLE_CONTAINER with VVIP_SIGNAL_POLICY_WRITER from its environment (it is restored on exit), or recreate it with VVIP_SIGNAL_POLICY_WRITER=1 yourself, then re-run"
    WRITER_ORIG_ENV="$(oracle_writer_env)"
    WRITER_RECREATED=1      # armed BEFORE the recreate: even a failed one is put back on exit
    log "signal-policy writer disabled (oracle runs with VVIP_SIGNAL_POLICY_WRITER='${WRITER_ORIG_ENV}') — recreating traffic-oracle with VVIP_SIGNAL_POLICY_WRITER=1 for this run; it is put back to '${WRITER_ORIG_ENV}' at the end (phase 8d, or the exit trap)"
    recreate_oracle_writer 1 \
        || die_hint "could not recreate traffic-oracle with the writer flag" \
            "${ORACLE_WRITER_CMD:+VVIP_SIGNAL_POLICY_WRITER=1 $ORACLE_WRITER_CMD}${ORACLE_WRITER_CMD:-cd $INFRA_DIR && (. $VVIP_DIR/scripts/veer_env.sh && VVIP_SIGNAL_POLICY_WRITER=1 docker compose -p $COMPOSE_PROJECT $COMPOSE_FILES up -d --no-deps --no-build traffic-oracle)}"
    wait_writer True || true
    writer_on=$(writer_flag)
fi
[ "$writer_on" = "True" ] \
    && ok "signal-policy writer ENABLED${WRITER_RECREATED:+ for this run (put back to '${WRITER_ORIG_ENV}' at the end)} (C7 flag; commits still fail closed without a calibrated twin)" \
    || die_hint "signal-policy writer still disabled after recreate" \
        "docker exec $ORACLE_CONTAINER env | grep VVIP_SIGNAL_POLICY_WRITER (must be 1); see $INFRA_DIR/compose.argus-override.yml traffic-oracle.environment"

# 1c. the canvas origin IS the deck (one origin serves /gods-eye, /citizen
# and the /api/v1 reverse proxy, src/hooks.server.ts). Its pages are public;
# the proxy sits behind the operator PIN gate, so this run signs in ONCE.
[ "$(hcode "$DECK_ORIGIN/gods-eye" 10)" = "200" ] && [ "$(hcode "$DECK_ORIGIN/citizen" 10)" = "200" ] \
    && ok "deck serving $DECK_ORIGIN (canvas origin; /gods-eye + /citizen up)" \
    || die_hint "deck canvas origin not answering on $DECK_ORIGIN" \
        "cd $INFRA_DIR && docker compose -p $COMPOSE_PROJECT $COMPOSE_FILES up -d --no-deps --build command-deck"
deck_signin "$DECK_ORIGIN" "Showcase run (automated check)" \
    || die_hint "no operator session on $DECK_ORIGIN (deck_signin said why, above)" \
        "the PIN comes from VEER_OPERATOR_PIN, VEER_OPERATOR_PIN_FILE (a private deck's PIN file) or veer.env; one attempt per run — the deck throttles failed sign-ins"
[ "$(deck_curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$DECK_ORIGIN/api/v1/movements/active" 2>/dev/null)" = "200" ] \
    && ok "deck /api/v1 proxy live (hooks.server.ts → traffic-oracle; operator gate ${DECK_GATE}${DECK_OFFICER:+, signed in as \"$DECK_OFFICER\"})" \
    || die_hint "deck /api/v1 proxy not answering 200 on $DECK_ORIGIN" \
        "the deck must reach the oracle on loopback (host networking)${DECK_CONTAINER:+; docker logs $DECK_CONTAINER}"

ok "fleet ($n_fleet containers), emulator $EMULATOR_URL, models $DECIDER_URL/$LLAMA_URL/$EMBED_URL, deck $DECK_ORIGIN (the ONE deck), writer flag — all healthy"
log "sweep seed this run: $SWEEP_SEED; event ids come from each run's document"

# ════════════════════════════════════════════════════════════════════════════
# 2. BASELINE — none to take: every emulator run is a fresh subject (its
#    event id is run-scoped), so every audit row that names this run's
#    flagship is this run's; phase 8 counts them straight from the store.
# ════════════════════════════════════════════════════════════════════════════
log "phase 2: baseline — this run's flagship is a fresh subject (run-scoped event id): every audit row naming it is this run's"

# ════════════════════════════════════════════════════════════════════════════
# 3. THE REPORT — plate earmarked (the stolen-vehicle chain opens)
# ════════════════════════════════════════════════════════════════════════════
log "phase 3: STOLEN VEHICLE reported — earmarking $STOLEN_PLATE ($FIR_REF)"
EARMARK_JSON="$SHOWCASE_DIR/chain_earmark.json"
ORACLE="$ORACLE_URL" PLATE="$STOLEN_PLATE" FIR="$FIR_REF" python3 - > "$EARMARK_JSON" <<'PYEOF'
import json, os, urllib.request
body = {
    "state": "flagged",
    "reason": f"STOLEN VEHICLE reported — {os.environ['FIR']}; God's Eye Morning flagship",
    "lawful_basis": "stolen_vehicle_fir",
    "retention_days": 30,
    "earmarked_by": "God's Eye Morning showcase",
}
req = urllib.request.Request(
    f"{os.environ['ORACLE']}/api/v1/vehicles/{os.environ['PLATE']}/earmark",
    data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=30) as r:
    print(json.dumps(json.loads(r.read()), indent=1))
PYEOF
EARMARK_OK=$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print("yes" if d.get("state")=="flagged" and d.get("lawful_basis")=="stolen_vehicle_fir" and d.get("vehicle_id") else "no")' "$EARMARK_JSON" 2>/dev/null || echo no)
VEHICLE_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("vehicle_id",""))' "$EARMARK_JSON" 2>/dev/null || echo "")
[ "$EARMARK_OK" = "yes" ] && [ -n "$VEHICLE_ID" ] \
    && ok "chain[1/6] plate earmarked: $STOLEN_PLATE flagged (lawful_basis stolen_vehicle_fir, vehicle $VEHICLE_ID)" \
    || fail "chain[1/6] earmark failed (see $EARMARK_JSON)"

# ════════════════════════════════════════════════════════════════════════════
# 4. THE MORNING — Rath Yatra + monsoon burst + ambulance request (live)
# ════════════════════════════════════════════════════════════════════════════
log "phase 4: injecting $FLAGSHIP_ID (procession + monsoon + ambulance) clock_scale=$CLOCK_SCALE"
FLAG_JSON="$SHOWCASE_DIR/injection_flagship.json"
FLAG_RUN_ID=$(emu_start "{\"use_case_id\":\"$FLAGSHIP_ID\",\"clock_scale\":$CLOCK_SCALE,\"tick_sec\":$TICK_SEC}" "$FLAG_JSON") \
    || die_hint "the emulator refused the flagship run — $FLAG_RUN_ID" \
        "a 409 names the run already holding $FLAGSHIP_ID: let it finish, or stop it (curl -X POST $EMULATOR_URL/api/v1/emulator/runs/N/stop)"
emu_poll "$FLAG_RUN_ID" "$FLAG_JSON" 60 event >/dev/null
FLAG_EVENT_ID=$(jget "$FLAG_JSON" event_id)
is_uuid "$FLAG_EVENT_ID" \
    || die_hint "flagship run $FLAG_RUN_ID named no event id ($(jget "$FLAG_JSON" status): $(jget "$FLAG_JSON" reason))" \
        "GET $EMULATOR_URL/api/v1/emulator/runs/$FLAG_RUN_ID?include_tape=false"
log "flagship run $FLAG_RUN_ID → event $FLAG_EVENT_ID (run-scoped: uuid5 over use_case|seed|run)"

# the flagship event must go LIVE in the movements feed (advisory source)
visible_after=-1
for i in $(seq 1 60); do
    n=$(curl -s --max-time 5 "$ORACLE_URL/api/v1/movements/active" \
        | python3 -c 'import json,sys; j=json.load(sys.stdin); print(sum(1 for m in j.get("movements",[]) if m.get("movement_id")==sys.argv[1]))' "$FLAG_EVENT_ID" 2>/dev/null || echo 0)
    if [ "$n" -ge 1 ]; then visible_after=$i; break; fi
    sleep 1
done
if [ "$visible_after" -ge 0 ] 2>/dev/null; then
    ok "Rath Yatra LIVE in GET /api/v1/movements/active after ${visible_after}s (citizen-advisory state reflects it)"
    ACTIVE_FEED_SEEN=true
else
    fail "flagship event never appeared in GET /api/v1/movements/active"
    ACTIVE_FEED_SEEN=false
fi

# the situation engine must start deciding (audit rows appear) — an uncapped
# SQL count (the /adaptive/situation_log API caps `count` at its limit)
rows_after=-1
for i in $(seq 1 60); do
    n=$($PSQL -c "SELECT count(*) FROM adaptive.situation_log WHERE movement_id='$FLAG_EVENT_ID'" 2>/dev/null || echo 0)
    if [ "${n:-0}" -gt 0 ] 2>/dev/null; then rows_after=$i; break; fi
    sleep 2
done
if [ "$rows_after" -ge 0 ] 2>/dev/null; then
    ok "situation engine deciding on the flagship after ~$((rows_after*2))s (audit rows growing)"
else
    log "note: no engine rows yet for the flagship — the explicit observe calls below still drive the audit path"
fi

# 4b. THE SIREN — ambulance green-corridor request, decided mid-chaos.
#     Observe calls with the flagship's documented cause values (profiles.py:
#     crowd 0.55->0.92 EXTREME, weather 0.6 RAIN, ambulance projection 120 s
#     base) drive the escalation channel; the band itself only escalates
#     after hysteresis up_confirm=2, so the peak is re-polled until the
#     effective band carries it (see the train inside the PYEOF below).
log "phase 4b: 108 ambulance green-corridor request — escalation decision mid-chaos"
OBSERVE_JSON="$SHOWCASE_DIR/ambulance_observe.json"
ORACLE="$ORACLE_URL" MID="$FLAG_EVENT_ID" python3 - > "$OBSERVE_JSON" <<'PYEOF'
import json, os, time, urllib.request

def post(doc):
    body = {"movement_id": os.environ["MID"], "vvip_class": "none", **doc}
    req = urllib.request.Request(
        os.environ["ORACLE"] + "/api/v1/adaptive/observe",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())

def summarize(label, resp):
    return {
        "label": label,
        "effective_class": resp.get("effective_class"),
        "transitioned": resp.get("transitioned"),
        "previous_class": resp.get("previous_class"),
        "escalation": ((resp.get("params_response") or {}).get("evidence") or {}).get("escalation"),
        "param_source": (resp.get("params_response") or {}).get("source"),
    }

# Relief Road -> Nehru Bridge Road -> Ashram Road (South) -> Riverfront Road
# (West): the flagship's spine as a drivable chain of the real network
ROUTE = [1005, 1015, 1001, 1006]
out = [summarize("window_opening", post({
    "predicted_congestion_idx": 0.55, "weather_severity": 0.35, "weather": "RAIN",
    "crowd_score": 0.55, "crowd_class": "EXTREME", "duration_class": "hours",
    "event_active": True, "ambulance_delay_projection_sec": 45,
    "route_segment_ids": ROUTE,
}))]

# Band escalation is confirmation-gated: hysteresis up_confirm=2 requires TWO
# consecutive higher-band observations before effective_class carries the new
# band (adaptive/hysteresis.py) — and the engine loop interleaves its own
# ticks on the same movement every ~5 s from the live feed doc.  So the 108
# dispatcher RE-POLLS: the 120 s projection is re-posted (bounded train,
# early exit) until the situation band actually carries the escalation.
peak_doc = {
    "predicted_congestion_idx": 0.92, "weather_severity": 0.6, "weather": "RAIN",
    "crowd_score": 0.92, "crowd_class": "EXTREME", "duration_class": "hours",
    "event_active": True, "ambulance_delay_projection_sec": 120,
    "route_segment_ids": ROUTE,
}
trail, peak = [], None
for i in range(9):
    peak = summarize("escalation_peak", post(peak_doc))
    trail.append({"attempt": i + 1, "effective_class": peak["effective_class"],
                  "transitioned": peak["transitioned"]})
    if peak["effective_class"] in ("AMBER", "RED", "BLACK"):
        break
    time.sleep(2.0)
peak["confirm_attempts"] = len(trail)
peak["confirm_trail"] = trail
out.append(peak)
print(json.dumps(out, indent=1))
PYEOF
OBSERVE_OK=$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
peak=d[-1]
esc=json.dumps(peak.get("escalation") or "")
print("yes" if len(d)==2 and peak.get("effective_class") in ("AMBER","RED","BLACK") and "AMBULANCE_DELAY" in esc else "no")' "$OBSERVE_JSON" 2>/dev/null || echo no)
[ "$OBSERVE_OK" = "yes" ] \
    && ok "ambulance escalation decided mid-chaos ($(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));p=d[-1];print(p["effective_class"], json.dumps(p["escalation"]))' "$OBSERVE_JSON" 2>/dev/null))" \
    || fail "ambulance escalation decision missing (see $OBSERVE_JSON)"

# 4c. JAN RAKSHAK — dispatch to the Swami Vivekananda Road market spillover (segment 1013)
SPILL_JUNCTION=$($PSQL -c "SELECT via_junction_id FROM corridor.segment_adjacency WHERE 1013 IN (from_segment_id, to_segment_id) ORDER BY via_junction_id LIMIT 1")
[ -n "$SPILL_JUNCTION" ] || SPILL_JUNCTION=1
log "phase 4c: Jan Rakshak dispatch to the spillover (segment 1013, junction $SPILL_JUNCTION)"
JR_JSON="$SHOWCASE_DIR/jan_rakshak.json"
ORACLE="$ORACLE_URL" EVENT="$FLAG_EVENT_ID" JUNCTION="$SPILL_JUNCTION" OFFICER="$OFFICER" python3 - > "$JR_JSON" <<'PYEOF'
import json, os, urllib.request

def post(path, body):
    req = urllib.request.Request(os.environ["ORACLE"] + path,
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

ev, junction = os.environ["EVENT"], int(os.environ["JUNCTION"])
d = post(f"/api/v1/events/{ev}/dispatch", {
    "agency": "jan_rakshak", "unit_id": "JR-EM-01", "waypoint_junction": junction,
    "requested_by": "God's Eye Morning showcase",
    "note": "market spillover behind Swami Vivekananda Road barricades (segment 1013) — crowd management support",
})
did = d["dispatch_id"]
decision = post(f"/api/v1/dispatches/{did}/decision",
                {"action": "approve", "officer": os.environ["OFFICER"],
                 "note": "citizen-volunteer coordination approved for the spillover"})
en_route = post(f"/api/v1/dispatches/{did}/status",
                {"status": "en_route", "officer": os.environ["OFFICER"]})
on_scene = post(f"/api/v1/dispatches/{did}/status",
                {"status": "on_scene", "officer": os.environ["OFFICER"],
                 "note": "unit holding the barricade line"})
import urllib.request as u
with u.urlopen(os.environ["ORACLE"] + "/api/v1/units", timeout=30) as r:
    units = json.loads(r.read()).get("units", [])
mine = [x for x in units if x.get("dispatch_id") == did]
print(json.dumps({
    "dispatch_id": did, "event_id": ev, "agency": d.get("agency"),
    "agency_label": d.get("agency_label"), "unit_id": d.get("unit_id"),
    "statuses": [d.get("status"), decision.get("status"), en_route.get("status"), on_scene.get("status")],
    "audit": [(a.get("from_status"), a.get("to_status"), a.get("actor")) for a in reversed(on_scene.get("audit", []))],
    "projected_unit": mine[0] if mine else None,
}, indent=1))
PYEOF
JR_OK=$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print("yes" if d.get("statuses")==["requested","approved","en_route","on_scene"] and d.get("projected_unit") else "no")' "$JR_JSON" 2>/dev/null || echo no)
JR_UNIT=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); u=d.get("projected_unit") or {}; print(u.get("label") or u.get("unit_id") or "")' "$JR_JSON" 2>/dev/null || echo "")
JR_DISPATCH_ID=$(jget "$JR_JSON" dispatch_id)
[ "$JR_OK" = "yes" ] \
    && ok "Jan Rakshak dispatched: requested->approved->en_route->on_scene (audited, approved by \"$OFFICER\"); live unit projected: $JR_UNIT" \
    || fail "Jan Rakshak dispatch lifecycle incomplete (see $JR_JSON)"
JAN_RAKSHAK_DISPATCHED=$([ "$JR_OK" = "yes" ] && echo true || echo false)

# 4d. CITY SIGNAL PLANS vs THE BAND — calibration replay + live-band apply;
#     the C7 guard's decision must be fully auditable (ruling on record).
log "phase 4d: city signal-policy engine vs the situation band"
SIGNALS_JSON="$SHOWCASE_DIR/signal_policy.json"
ORACLE="$ORACLE_URL" EVENT="$FLAG_EVENT_ID" python3 - > "$SIGNALS_JSON" <<'PYEOF'
import json, os, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

def post(path, body, timeout=180):
    req = urllib.request.Request(os.environ["ORACLE"] + path,
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def get(path):
    with urllib.request.urlopen(os.environ["ORACLE"] + path, timeout=30) as r:
        return json.loads(r.read())

# 1. the twin calibration replay over the last 72 h of FIELD data (honest
#    numbers): traffic.hourly_aggregates holds real observations only
#    (migration 019) — with a synthetic / emulated feed the replay answers
#    no_data and the twin stays uncalibrated, which is the truth
now = datetime.now(timezone.utc).replace(microsecond=0)
code, replay = post("/api/v1/simulate/replay",
    {"window_start": (now - timedelta(hours=72)).isoformat().replace("+00:00", "Z"),
     "window_end": now.isoformat().replace("+00:00", "Z")})
# 2. the LIVE city band as the situation engine sees it
band_live = get("/api/v1/signals/plans").get("city_band")
# 3. the operator action: apply the live band's city plan for the flagship
band = band_live if band_live in ("AMBER", "RED", "BLACK") else "RED"
code_apply, apply_resp = post("/api/v1/signals/plans/apply",
    {"band": band, "event_id": os.environ["EVENT"],
     "route_segment_ids": [1005, 1015, 1001, 1006]})
detail = apply_resp.get("detail", apply_resp) if code_apply != 200 else apply_resp
# 4. the auditable decision surface
plans = get("/api/v1/signals/plans")
last = plans.get("last_plan") or {}
committed = bool(code_apply == 200 and apply_resp.get("applied"))
refused = (not committed) and bool(last) and last.get("band") == band
history = get("/api/v1/signals/history?limit=20").get("history", [])
print(json.dumps({
    "replay": {"status": replay.get("status"), "reason": replay.get("reason"),
               "window_start": replay.get("window_start"), "window_end": replay.get("window_end"),
               "n_observations": replay.get("n_observations"),
               "speed_mae_kmh": replay.get("speed_mae_kmh"),
               "mae_tolerance_kmh": replay.get("mae_tolerance_kmh"),
               "calibrated": replay.get("calibrated")},
    "city_band_live": band_live,
    "apply": {"http": code_apply, "band": band, "applied": committed,
              "reason": detail.get("reason"),
              "planned_junctions": detail.get("planned_junctions"),
              "shadow_eval": detail.get("shadow_eval")},
    "last_plan_auditable": {
        "band": last.get("band"),
        "planned_junctions": last.get("planned_junctions"),
        "shadow_eval": last.get("shadow_eval"),
        "event_id": last.get("event_id"),
    },
    "committed_writes": 0 if not committed else len(apply_resp.get("planned_junctions", [])),
    "policy_refusals": 1 if refused else 0,
    "history_rows": len(history),
}, indent=1))
PYEOF
SIGNAL_GUARD_OK=$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
rep=d.get("replay") or {}; ap=d.get("apply") or {}; lp=d.get("last_plan_auditable") or {}
se=(ap.get("shadow_eval") or {}); lse=(lp.get("shadow_eval") or {})
ok = rep.get("status") is not None \
     and rep.get("calibrated") is False \
     and ap.get("applied") is False \
     and (len(ap.get("planned_junctions") or []) >= 1) \
     and se.get("detail") == "twin_not_calibrated" \
     and lse.get("detail") == "twin_not_calibrated" \
     and (len(lp.get("planned_junctions") or []) >= 1)
print("yes" if ok else "no")' "$SIGNALS_JSON" 2>/dev/null || echo no)
SIGNAL_MAE=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["replay"]["speed_mae_kmh"])' "$SIGNALS_JSON" 2>/dev/null || echo unknown)
REPLAY_SUMMARY=$(python3 -c '
import json, sys
r = json.load(open(sys.argv[1]))["replay"]
head = "replay %s: %s field hourly aggregates in the last 72 h" % (r.get("status"), r.get("n_observations") or 0)
mae = r.get("speed_mae_kmh")
print(head + (", MAE %s km/h vs the %s km/h gate" % (mae, r.get("mae_tolerance_kmh")) if mae is not None
              else " (%s)" % (r.get("reason") or "no MAE")))' "$SIGNALS_JSON" 2>/dev/null || echo "replay summary unavailable")
if [ "$SIGNAL_GUARD_OK" = "yes" ]; then
    NJ=$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["last_plan_auditable"]["planned_junctions"]))' "$SIGNALS_JSON" 2>/dev/null)
    ok "signal-policy engine end-to-end: band $(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["city_band_live"])' "$SIGNALS_JSON" 2>/dev/null) -> plan for $NJ junctions proposed -> C7 guard REFUSED the commit (twin_not_calibrated; $REPLAY_SUMMARY) -> decision auditable in GET /signals/plans"
else
    fail "signal-policy guard sequence not auditable as expected (see $SIGNALS_JSON — a COMMIT would also fail this check; committed writes are counted, not asserted absent)"
fi
SIGNAL_PLAN_SHIFTS=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("committed_writes",0))' "$SIGNALS_JSON" 2>/dev/null || echo 0)
SIGNAL_REFUSALS=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("policy_refusals",0))' "$SIGNALS_JSON" 2>/dev/null || echo 0)

# 4e. THE CITY, MID-CHAOS — the 3D canvas while the flagship event is active
log "phase 4e: 3D city canvas shot (mid-chaos, live layers)"
CHROME_BIN="${CHROME_PATH:-$HOME/.cache/puppeteer/chrome/linux_arm-153.0.8010.36/chrome-linux-arm64/chrome}"
if [ ! -x "$CHROME_BIN" ]; then
    CHROME_BIN="$(find "$HOME/.cache/puppeteer/chrome" -name chrome -type f -perm -u+x 2>/dev/null | sort | tail -1)"
fi
if [ -n "$CHROME_BIN" ] && [ -x "$CHROME_BIN" ]; then
    export CHROME_PATH="$CHROME_BIN"
    if ( cd "$VVIP_DIR/frontend" && NODE_PATH="$VVIP_DIR/frontend/node_modules" \
         node "$VVIP_DIR/scripts/showcase_canvas_shots.mjs" --city-only \
              "$DECK_ORIGIN" "$SHOWCASE_DIR" "pending" ) \
         > "$SHOWCASE_DIR/shots_city.log" 2>&1; then
        ok "canvas shot: showcase_gods_eye_city.png proven non-blank (see shots_city.log)"
        CITY_SHOT_OK=true
    else
        bad "city canvas capture failed (details: $SHOWCASE_DIR/shots_city.log)"
        fail "3D city canvas shot failed"
        CITY_SHOT_OK=false
    fi
else
    fail "chrome binary missing (CHROME_PATH) — canvas shots impossible"
    CITY_SHOT_OK=false
fi

# 4f. the flagship run's end — C-G runs are asynchronous, so its run
#     document is polled (GET /runs/{id}?include_tape=false every 2 s,
#     bounded) — then the green-corridor windows in the twin's schedule
log "phase 4f: flagship run $FLAG_RUN_ID — waiting for its end (poll every 2 s, ≤ ${FLAG_RUN_TIMEOUT_SEC}s)"
FLAG_STATUS=$(emu_poll "$FLAG_RUN_ID" "$FLAG_JSON" "$FLAG_RUN_TIMEOUT_SEC" end)
FLAG_DELIVERED=$(jget "$FLAG_JSON" progress.delivered)
FLAG_DELIVERED=${FLAG_DELIVERED:-0}
FLAG_DELIVERY=$(jget "$FLAG_JSON" progress)
[ -n "$FLAG_DELIVERY" ] || FLAG_DELIVERY="{}"
log "flagship run $FLAG_RUN_ID $FLAG_STATUS; progress $FLAG_DELIVERY"
case "$FLAG_STATUS" in
    completed)
        [ "$FLAG_DELIVERED" -gt 0 ] \
            && ok "flagship run $FLAG_RUN_ID completed: $FLAG_DELIVERED of $(jget "$FLAG_JSON" progress.total) frames delivered (progress.delivered)" \
            || fail "flagship run $FLAG_RUN_ID completed with 0 frames delivered" ;;
    running) fail "flagship run $FLAG_RUN_ID still running after ${FLAG_RUN_TIMEOUT_SEC}s (see $FLAG_JSON)" ;;
    *) fail "flagship run $FLAG_RUN_ID ended $FLAG_STATUS: $(jget "$FLAG_JSON" reason)" ;;
esac

log "phase 4f: twin audit — green-corridor windows in the audited schedule"
TWIN_JSON="$SHOWCASE_DIR/twin_audit.json"
ORACLE="$ORACLE_URL" SCENARIO="$FLAGSHIP_ID" python3 - > "$TWIN_JSON" <<'PYEOF'
import json, os, urllib.request
req = urllib.request.Request(os.environ["ORACLE"] + "/api/v1/simulate/twin",
    data=json.dumps({"scenario_id": os.environ["SCENARIO"]}).encode(),
    headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=300) as r:
    d = json.loads(r.read())
runs = d.get("runs", [])
ev0 = runs[0].get("events", []) if runs else []
windows = [e for e in ev0 if e.get("type") == "emergency_vehicle"]
print(json.dumps({
    "replications": len(runs),
    "seed": d.get("seed"),
    "green_corridor_windows": len(windows),
    "window_labels": [w.get("label") for w in windows][:6],
    "ambulance_delay_sec_mean": (sum(r.get("ambulance_delay_sec", 0) for r in runs) / len(runs)) if runs else None,
}, indent=1))
PYEOF
GC_WINDOWS=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("green_corridor_windows",0))' "$TWIN_JSON" 2>/dev/null || echo 0)
[ "$GC_WINDOWS" -ge 1 ] \
    && ok "green corridor windows: the twin's audited schedule holds $GC_WINDOWS emergency_vehicle windows for the scenario's scripted 108 crossings ($(python3 -c 'import json,sys;print("; ".join(json.load(open(sys.argv[1])).get("window_labels",[])[:2]))' "$TWIN_JSON" 2>/dev/null))" \
    || fail "no green-corridor window in the twin audited schedule (see $TWIN_JSON)"

# ════════════════════════════════════════════════════════════════════════════
# 5. THE SWEEP — emulated ANPR cameras read the stolen plate (live)
# ════════════════════════════════════════════════════════════════════════════
log "phase 5: ANPR sweep $SWEEP_ID (seed $SWEEP_SEED, clock $SWEEP_CLOCK) — the stolen plate crosses the cameras"
SWEEP_JSON="$SHOWCASE_DIR/injection_sweep.json"
SWEEP_RUN_ID=$(emu_start "{\"use_case_id\":\"$SWEEP_ID\",\"seed\":$SWEEP_SEED,\"clock_scale\":$SWEEP_CLOCK,\"tick_sec\":$TICK_SEC}" "$SWEEP_JSON") \
    || die_hint "the emulator refused the ANPR sweep — $SWEEP_RUN_ID" \
        "a 409 names the run already holding $SWEEP_ID: let it finish, or stop it (curl -X POST $EMULATOR_URL/api/v1/emulator/runs/N/stop)"
SWEEP_STATUS=$(emu_poll "$SWEEP_RUN_ID" "$SWEEP_JSON" "$SWEEP_RUN_TIMEOUT_SEC" end)
SWEEP_DELIVERED=$(jget "$SWEEP_JSON" progress.delivered)
SWEEP_DELIVERED=${SWEEP_DELIVERED:-0}
SWEEP_EVENT_ID=$(jget "$SWEEP_JSON" event_id)
is_uuid "$SWEEP_EVENT_ID" || SWEEP_EVENT_ID=""
log "sweep run $SWEEP_RUN_ID $SWEEP_STATUS: event ${SWEEP_EVENT_ID:-?}, delivered $SWEEP_DELIVERED frames; scenario precondition: $(jget "$SWEEP_JSON" scenario_precondition)"
if [ "$SWEEP_STATUS" = "completed" ] && [ "$SWEEP_DELIVERED" -gt 0 ]; then
    ok "ANPR sweep delivered $SWEEP_DELIVERED frames into the live stack (run $SWEEP_RUN_ID completed)"
elif [ "$SWEEP_STATUS" = "running" ]; then
    fail "ANPR sweep run $SWEEP_RUN_ID still running after ${SWEEP_RUN_TIMEOUT_SEC}s (see $SWEEP_JSON)"
else
    fail "ANPR sweep run $SWEEP_RUN_ID ended $SWEEP_STATUS with $SWEEP_DELIVERED frames delivered: $(jget "$SWEEP_JSON" reason)"
fi

# ════════════════════════════════════════════════════════════════════════════
# 6. THE CHAIN — evidence -> officer authorisation -> pursuit -> interception
#                -> live trace (every step a real API call, asserted)
# ════════════════════════════════════════════════════════════════════════════
log "phase 6: the stolen-vehicle chain (sightings -> evidence, no fine -> officer authorisation -> pursuit -> trace)"
CHAIN_JSON="$SHOWCASE_DIR/pursuit_chain.json"
ORACLE="$ORACLE_URL" VID="$VEHICLE_ID" RUN="$SWEEP_RUN_ID" PLATE="$STOLEN_PLATE" \
OFFICER="$OFFICER" FIR="$FIR_REF" python3 - > "$CHAIN_JSON" <<'PYEOF'
import json, os, urllib.request, urllib.error

PENAL = ("R_SPD_SEGMENT", "R_SIGNAL_VIOLATION", "R_CLOSED_CORRIDOR_ENTRY")   # enforcement/rules.py RULE_IDS
NOTE = "pursuit authorised on a reported-stolen vehicle (evidence, no fine)"

def call(path, body=None):
    """(HTTP status, body) — a refusal is RECORDED, never fatal: the
    artifact must always land so the assertions judge real state."""
    req = urllib.request.Request(os.environ["ORACLE"] + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            doc = json.loads(e.read())
        except ValueError:
            doc = {}
        return e.code, doc if isinstance(doc, dict) else {"detail": doc}

def refused(code, doc):
    return {"_http_error": code, "_detail": str(doc.get("detail", doc))[:300]}

vid, run, plate = os.environ["VID"], int(os.environ["RUN"]), os.environ["PLATE"]
officer, fir = os.environ["OFFICER"], os.environ["FIR"]

# 2. the emulated ANPR camera sightings of the stolen plate (this run)
code, sight = call(f"/api/v1/vehicles/{vid}/sightings?limit=200")
sightings = sight.get("sightings", []) if code == 200 else []
stolen = [s for s in sightings if s.get("matched_plate_hit") == "stolen_vehicle"]
this_run = [s for s in stolen if s.get("run_id") == run]
this_ids = {s["sighting_id"] for s in this_run}

# 3. the deterministic rules over this run's reads.  The ANPR ingest ran
#    them as each read landed; re-running is idempotent and says what fired:
#    under the FIR earmark ONE evidence-only R_STOLEN_SIGHTING per read, the
#    penal rules' outcomes WITHHELD — no e-challan for the victim's car (C-N)
fired, withheld, eval_errors = [], [], []
for s in this_run:
    code, resp = call("/api/v1/enforcement/evaluate", {"sighting_id": s["sighting_id"]})
    if code != 200:
        eval_errors.append(refused(code, resp))
        continue
    fired += resp.get("fired", [])
    withheld += resp.get("withheld_rules", [])

code, listing = call("/api/v1/enforcement/recommendations?status=pending&limit=500")
pending = listing.get("recommendations", []) if code == 200 else []
on_plate = [r for r in pending if r.get("plate") == plate]
evidence = [r for r in on_plate
            if r.get("rule_id") == "R_STOLEN_SIGHTING" and r.get("sighting_id") in this_ids]
penal_this_run = [r for r in on_plate
                  if r.get("rule_id") in PENAL and r.get("sighting_id") in this_ids]

# 4. a named officer decides the NEWEST sighting: approve = the authorised
#    basis of a pursuit (evidence, never a fine).  Newest by the read's own
#    time, ties by camera — content, never listing order, so seed-pinned
#    repetition rounds decide the same read.
def read_key(r):
    ev = r.get("evidence") or {}
    return (str(ev.get("t") or ""), str(ev.get("camera_id") or ""))

rec = max(evidence, key=read_key) if evidence else None
approval = None
if rec:
    code, doc = call(f"/api/v1/enforcement/recommendations/{rec['recommendation_id']}/decision",
                     {"decision": "approve", "officer": officer, "note": NOTE})
    approval = doc if code == 200 else refused(code, doc)

# C-N: approving an e-challan on this plate must be REFUSED — 409, nothing
# written, the row stays pending.  Probed on the oldest pending penal row of
# THIS vehicle (a read from before the FIR); a store holding none (a fresh
# install: every read since the FIR is evidence-only) has nothing to probe.
# The approval is SENT only while the vehicle is verifiably reported stolen
# right now — its newest active earmark (GET /api/v1/vehicles) carries the
# lawful basis stolen_vehicle_fir, so the decision endpoint's C-N predicate
# (an active FIR earmark exists) holds: the refusal is the server's, but the
# probe must never be able to land a real e-challan on the victim's car
# should the FIR earmark have lapsed or been cleared.
penal_pending = sorted((r for r in on_plate
                        if r.get("rule_id") in PENAL and str(r.get("vehicle_id")) == vid),
                       key=lambda r: (str(r.get("created_at") or ""), r["recommendation_id"]))
code_v, vlist = call("/api/v1/vehicles")
active = next((v for v in (vlist.get("vehicles") or []) if str(v.get("vehicle_id")) == vid),
              None) if code_v == 200 else None
stolen_now = bool(active) and active.get("lawful_basis") == "stolen_vehicle_fir"
if penal_pending and stolen_now:
    target = penal_pending[0]
    code, doc = call(f"/api/v1/enforcement/recommendations/{target['recommendation_id']}/decision",
                     {"decision": "approve", "officer": officer,
                      "note": "C-N check: an e-challan on a reported-stolen vehicle must be refused"})
    c2, after = call("/api/v1/enforcement/recommendations?status=pending&limit=500")
    probe = {"exercised": True, "recommendation_id": target["recommendation_id"],
             "rule_id": target.get("rule_id"), "http": code,
             "detail": doc.get("detail"), "reason": doc.get("reason"),
             "precondition": {"earmark_state": active.get("earmark_state"),
                              "lawful_basis": active.get("lawful_basis")},
             "still_pending": c2 == 200 and any(
                 r.get("recommendation_id") == target["recommendation_id"]
                 for r in after.get("recommendations", []))}
elif penal_pending:
    probe = {"exercised": False, "unsafe": True,
             "why": "NOT SENT: the vehicle is not verifiably reported stolen right now "
                    f"(GET /api/v1/vehicles HTTP {code_v}, newest active earmark "
                    f"{(active or {}).get('lawful_basis')!r}) — an approval could land as a "
                    "real e-challan on the victim's car"}
else:
    probe = {"exercised": False,
             "why": "no pending e-challan recommendation on the plate in this store "
                    "(every read since the FIR is evidence-only)"}

# 5. the pursuit event (human-initiated; carries vehicle + the approved sighting)
pursuit = pursuit_title = None
if approval and approval.get("status") == "approved":
    pursuit_title = f"PURSUIT — stolen {plate} ({fir})"
    code, doc = call("/api/v1/events", {
        "event_kind": "vehicle_pursuit", "protection_class": "none", "crowd_class": "LOW",
        "title": pursuit_title,
        "description": ("God's Eye Morning: operator-opened pursuit of a FIR-named stolen "
                        "vehicle, authorised by an officer-approved sighting of emulated ANPR "
                        "reads (evidence only — no e-challan for a reported-stolen vehicle). "
                        "Vehicles only (C6); the interception plan assists the operator."),
        # the emulator's sweep belt: a drivable chain of the real network
        "route_spines": [{"label": "old-city sweep belt", "segment_ids": [1015, 1012, 1049, 1013]}],
        "metadata": {"vehicle_id": vid, "recommendation_id": rec["recommendation_id"],
                     "fir": fir, "opened_by": officer},
    })
    pursuit = doc if code < 300 else refused(code, doc)

# 6. the interception plan + the live trace (supporting evidence; the plan
#    keys on the LATEST emulated read, which may sit outside the 300 m
#    attribution band — a 422 there is recorded, not fatal)
plan = trace = None
if pursuit and pursuit.get("event_id"):
    code, plan = call(f"/api/v1/events/{pursuit['event_id']}/interception-plan")
    plan = plan if code == 200 else refused(code, plan)
    code, trace = call(f"/api/v1/vehicles/{vid}/trace?window_sec=86400")
    trace = trace if code == 200 else refused(code, trace)

def withheld_of(r):
    """What the penal rules would have proposed for this read — never filed."""
    return [{"rule_id": w.get("rule_id"),
             "speed_kmh": (w.get("evidence") or {}).get("speed_kmh"),
             "speed_limit_kmh": (w.get("evidence") or {}).get("speed_limit_kmh")}
            for w in ((r.get("evidence") or {}).get("withheld_rules") or [])]

print(json.dumps({
    "vehicle_id": vid, "plate": plate,
    "sightings": {"stolen_total": len(stolen), "stolen_this_run": len(this_run),
                  "cameras": sorted({s["camera_id"] for s in this_run}),
                  "speeds_kmh": [s["speed_kmh"] for s in this_run],
                  "source": (this_run[0]["source"] if this_run else None)},
    "recommendation": {"fired_rules": fired,
                       "withheld_rules": withheld,
                       "evidence_recs_this_run": len(evidence),
                       "penal_recs_this_run": len(penal_this_run),
                       "eval_errors": eval_errors,
                       "chosen": {"id": rec["recommendation_id"], "rule_id": rec["rule_id"],
                                  "text": rec["recommendation"],
                                  "camera_id": (rec.get("evidence") or {}).get("camera_id"),
                                  "read_t": (rec.get("evidence") or {}).get("t"),
                                  "withheld": withheld_of(rec)} if rec else None},
    "approval": ({"status": approval.get("status"), "officer": approval.get("decided_by"),
                  "note": NOTE,
                  "audit_tail_last": (approval.get("audit_tail") or [{}])[-1]}
                 if approval and "_http_error" not in approval else approval),
    "penal_approval_probe": probe,
    "pursuit_event": ({"event_id": pursuit.get("event_id"), "kind": "vehicle_pursuit",
                       "status": pursuit.get("status"), "metadata": pursuit.get("metadata") or {},
                       "title": pursuit_title}
                      if pursuit and "_http_error" not in pursuit else pursuit),
    "interception": ({"n_junctions": len(plan.get("interception_junctions", [])),
                      "top": (plan.get("interception_junctions") or [{}])[0],
                      "latest_sighting": plan.get("latest_sighting")}
                     if plan and "_http_error" not in plan
                     else {"unavailable": True,
                           "http": (plan or {}).get("_http_error"),
                           "detail": (plan or {}).get("_detail")}),
    "trace": {"points": len(trace.get("positions", [])),
              "source": trace.get("source"),
              "first": (trace.get("positions") or [{}])[0].get("t"),
              "last": (trace.get("positions") or [{}])[-1].get("t")}
             if trace and "_http_error" not in trace else None,
}, indent=1))
PYEOF

read -r N_STOLEN_THIS N_STOLEN_ALL N_EVIDENCE N_PENAL_RUN FIRED_OK APPROVED PROBE PURSUIT_ID N_INTERCEPT N_TRACE TRACE_SRC < <(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
s = d.get("sightings") or {}
rec = d.get("recommendation") or {}
app = d.get("approval") or {}
pr = d.get("penal_approval_probe") or {}
pur = d.get("pursuit_event") or {}
inter = d.get("interception") or {}
tr = d.get("trace") or {}
fired = rec.get("fired_rules") or []
probe = ("unsafe" if pr.get("unsafe") else
         "n/a" if not pr.get("exercised") else
         "refused" if (pr.get("http") == 409
                       and pr.get("detail") == "e-challan withheld — vehicle reported stolen"
                       and pr.get("reason") == "stolen_vehicle_fir"
                       and pr.get("still_pending")) else "NOT-refused")
print(s.get("stolen_this_run", 0), s.get("stolen_total", 0),
      rec.get("evidence_recs_this_run", 0), rec.get("penal_recs_this_run", 0),
      "yes" if fired and set(fired) == {"R_STOLEN_SIGHTING"} else "no",
      1 if app.get("status") == "approved" and app.get("officer") == sys.argv[2] else 0,
      probe, pur.get("event_id") or "-", inter.get("n_junctions", 0),
      tr.get("points", 0), tr.get("source") or "-")' "$CHAIN_JSON" "$OFFICER" 2>/dev/null || echo "0 0 0 0 no 0 NOT-refused - 0 0 -")
[ "$PURSUIT_ID" = "-" ] && PURSUIT_ID=""

[ "$N_STOLEN_THIS" -ge 2 ] \
    && ok "chain[2/6] emulated ANPR cameras sighted the stolen plate: $N_STOLEN_THIS reads this run ($([ "$N_STOLEN_ALL" -gt "$N_STOLEN_THIS" ] && echo "$N_STOLEN_ALL incl. earlier runs" || echo this vehicle only), source $TRACE_SRC)" \
    || fail "chain[2/6] fewer than 2 stolen-plate sightings this run (got $N_STOLEN_THIS)"
WITHHELD_SUMMARY=$(python3 -c '
import collections, json, sys
r = json.load(open(sys.argv[1])).get("recommendation") or {}
c = collections.Counter(r.get("withheld_rules") or [])
print(", ".join(f"{k} x{v}" for k, v in sorted(c.items())) or "none")' "$CHAIN_JSON" 2>/dev/null || echo "?")
if [ "$N_EVIDENCE" -ge 1 ] && [ "$N_PENAL_RUN" = "0" ] && [ "$FIRED_OK" = "yes" ]; then
    ok "chain[3/6] EVIDENCE, NO FINE: $N_EVIDENCE R_STOLEN_SIGHTING recommendation(s) PENDING for this run's reads; the penal rules' e-challans WITHHELD (would have fired: $WITHHELD_SUMMARY); 0 penal recommendations"
else
    fail "chain[3/6] stolen-vehicle reads not evidence-only: $N_EVIDENCE R_STOLEN_SIGHTING + $N_PENAL_RUN penal recommendation(s) this run, rules fired $(python3 -c 'import json,sys;print(sorted(set((json.load(open(sys.argv[1])).get("recommendation") or {}).get("fired_rules") or [])))' "$CHAIN_JSON" 2>/dev/null) (C-N: never a fine for a reported-stolen vehicle)"
fi
[ "$APPROVED" = "1" ] \
    && ok "chain[4/6] OFFICER APPROVAL of the newest sighting: pursuit authorised on a reported-stolen vehicle (evidence, no fine) — approved by \"$OFFICER\"" \
    || fail "chain[4/6] the pursuit authorisation did not land (see approval in $CHAIN_JSON)"
case "$PROBE" in
    refused) ok "C-N: an e-challan approval on $STOLEN_PLATE is REFUSED — 409 'e-challan withheld — vehicle reported stolen' (the pending $(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["penal_approval_probe"]["rule_id"])' "$CHAIN_JSON" 2>/dev/null) row stays pending; nothing written)" ;;
    n/a) log "C-N: no pending e-challan on $STOLEN_PLATE in this store to probe (every read since the FIR is evidence-only) — the 409 refusal is not exercised here" ;;
    unsafe) fail "C-N: the e-challan approval probe was NOT sent — $STOLEN_PLATE is not verifiably reported stolen right now (see penal_approval_probe.why in $CHAIN_JSON); a probe must never be able to land a fine" ;;
    *) fail "C-N: approving an e-challan on $STOLEN_PLATE was NOT refused as withheld (see penal_approval_probe in $CHAIN_JSON)" ;;
esac
[ -n "$PURSUIT_ID" ] \
    && ok "chain[5/6] PURSUIT EVENT opened by the operator: $PURSUIT_ID (vehicle_pursuit, carries vehicle_id + the approved sighting's recommendation_id)" \
    || fail "chain[5/6] pursuit event not created (see pursuit_event in $CHAIN_JSON)"

# enrich the artifact with the PERSISTED pursuit metadata: the POST
# response echoes metadata as {} — the authoritative copy lives in
# events.corridor_events. Merge it so a cold auditor reads vehicle_id +
# recommendation_id + FIR in-band in pursuit_chain.json.
if is_uuid "$PURSUIT_ID"; then
    PURSUIT_META=$($PSQL -c "SELECT metadata::text FROM events.corridor_events WHERE event_id='$PURSUIT_ID'" 2>/dev/null || true)
    if [ -n "$PURSUIT_META" ]; then
        PURSUIT_META="$PURSUIT_META" CHAIN_JSON="$CHAIN_JSON" python3 - <<'PYEOF'
import json, os
p = os.environ["CHAIN_JSON"]
d = json.load(open(p))
pe = d.get("pursuit_event")
if pe:
    pe["metadata"] = json.loads(os.environ["PURSUIT_META"])
    pe["metadata_source"] = ("events.corridor_events (persisted row merged; "
                             "the POST response echoes metadata as {})")
    json.dump(d, open(p, "w"), indent=1)
PYEOF
        ok "pursuit metadata persisted and merged into the artifact: $(python3 -c 'import json,sys;m=(json.load(open(sys.argv[1]))["pursuit_event"] or {}).get("metadata") or {};print(sorted(m))' "$CHAIN_JSON" 2>/dev/null || echo '?')"
    else
        log "pursuit metadata query returned nothing — artifact keeps the POST echo"
    fi
fi
N_INTERCEPT_AVAIL=$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));i=d.get("interception") or {};print("no" if i.get("unavailable") else "yes")' "$CHAIN_JSON" 2>/dev/null || echo no)
if [ "$N_INTERCEPT_AVAIL" = "yes" ] && [ "$N_INTERCEPT" -ge 1 ]; then
    ok "support: interception plan ranks $N_INTERCEPT choke junctions (top: junction $(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));t=d["interception"]["top"];print(t.get("junction_id"),t.get("junction_type"),"score",t.get("score"))' "$CHAIN_JSON" 2>/dev/null))"
else
    log "support: interception plan unavailable this seed — $(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(str((d.get("interception") or {}).get("detail") or "")[:120])' "$CHAIN_JSON" 2>/dev/null) (the emulated camera's latest read sits outside the 300 m attribution band; recorded, not asserted)"
fi
[ "$N_TRACE" -ge 2 ] && [ "$TRACE_SRC" = "emulated_anpr" ] \
    && ok "chain[6b] LIVE TRACE on the canvas layer: $N_TRACE timed positions (source $TRACE_SRC) — GET /api/v1/vehicles/{id}/trace feeds the vehicle-traces layer" \
    || fail "chain[6b] trace not usable by the canvas (points=$N_TRACE source=$TRACE_SRC)"

PURSUIT_TITLE=$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print((d.get("pursuit_event") or {}).get("title") or "PURSUIT")' "$CHAIN_JSON" 2>/dev/null || echo "PURSUIT")
APPROVED_REC_ID=$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(((d.get("recommendation") or {}).get("chosen") or {}).get("id") or "")' "$CHAIN_JSON" 2>/dev/null || echo "")

# ════════════════════════════════════════════════════════════════════════════
# 7. THE EVIDENCE — pursuit-trace + citizen-advisory canvas shots
# ════════════════════════════════════════════════════════════════════════════
log "phase 7: pursuit-trace + citizen-advisory canvas shots"
if [ -n "$CHROME_BIN" ] && [ -x "$CHROME_BIN" ]; then
    if ( cd "$VVIP_DIR/frontend" && NODE_PATH="$VVIP_DIR/frontend/node_modules" \
         node "$VVIP_DIR/scripts/showcase_canvas_shots.mjs" --rest-only \
              "$DECK_ORIGIN" "$SHOWCASE_DIR" "$PURSUIT_TITLE" ) \
         > "$SHOWCASE_DIR/shots_rest.log" 2>&1; then
        ok "canvas shots: showcase_pursuit_trace.png + showcase_citizen_advisory.png proven non-blank (the public /citizen page shows no pursuit, no plate)"
        REST_SHOTS_OK=true
    else
        bad "pursuit/citizen canvas capture failed (details: $SHOWCASE_DIR/shots_rest.log)"
        fail "pursuit/citizen canvas shots failed"
        REST_SHOTS_OK=false
    fi
else
    REST_SHOTS_OK=false
    fail "chrome binary missing — pursuit/citizen shots impossible"
fi

# ════════════════════════════════════════════════════════════════════════════
# 8. THE LEDGER — audit-log assertions (the real stack did the work)
# ════════════════════════════════════════════════════════════════════════════
log "phase 8: audit-log assertions"

# 8b. the morning eases — read from the audit log, no sleep, no observe
#     train: the flagship's event closed with its run (phase 4f) and the
#     situation engine drops a closed event at its next tick (retain_only:
#     a completed event never holds the city band), so every band change of
#     the morning is already a transitioned row of adaptive.situation_log.
log "phase 8b: the morning eases — the band's transitions from adaptive.situation_log; the closed event released"
CALM_JSON="$SHOWCASE_DIR/band_easing.json"
$PSQL -c "SELECT COALESCE(json_agg(json_build_object('n', n, 'previous_class', previous_class, 'effective_class', situation_class, 'observed_class', observed_class, 'situation_code', situation_code, 'transitioned', transitioned, 'observed_at', observed_at) ORDER BY n), '[]'::json) FROM (SELECT row_number() OVER w AS n, lag(situation_class) OVER w AS previous_class, situation_class, observed_class, situation_code, transitioned, observed_at FROM adaptive.situation_log WHERE movement_id='$FLAG_EVENT_ID' WINDOW w AS (ORDER BY observed_at, log_id)) t WHERE transitioned" \
    > "$CALM_JSON" 2>/dev/null || echo '[]' > "$CALM_JSON"
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$CALM_JSON" 2>/dev/null || echo '[]' > "$CALM_JSON"
TRANSITIONS=$(python3 -c '
import json, sys
print(", ".join("%s->%s" % (t.get("previous_class"), t.get("effective_class")) for t in json.load(open(sys.argv[1]))) or "none")' "$CALM_JSON" 2>/dev/null || echo "?")
read -r RELEASED CITY_BAND_AFTER < <(health "$ORACLE_URL/api/v1/adaptive/status" 15 | python3 -c '
import json, sys
d = json.load(sys.stdin)
band = d.get("city_band")
band = band.get("band") if isinstance(band, dict) else band
print("no" if sys.argv[1] in (d.get("movements") or {}) else "yes", band or "-")' "$FLAG_EVENT_ID" 2>/dev/null || echo "unknown -")
log "band transitions of the morning: $TRANSITIONS; flagship released by the engine after its event closed: $RELEASED (city band now $CITY_BAND_AFTER)"

# the flagship is a fresh subject (run-scoped id): every row naming it is
# this run's — uncapped SQL counts
n_situation=$($PSQL -c "SELECT count(*) FROM adaptive.situation_log WHERE movement_id='$FLAG_EVENT_ID'")
n_transitions=$($PSQL -c "SELECT count(*) FROM adaptive.situation_log WHERE movement_id='$FLAG_EVENT_ID' AND transitioned")
n_params=$($PSQL -c "SELECT count(*) FROM adaptive.param_overrides WHERE movement_id='$FLAG_EVENT_ID'")
n_amb=$($PSQL -c "SELECT count(*) FROM adaptive.param_overrides WHERE movement_id='$FLAG_EVENT_ID' AND evidence::text LIKE '%AMBULANCE_DELAY%'")
bands_csv=$($PSQL -c "SELECT situation_class||':'||count(*) FROM adaptive.situation_log WHERE movement_id='$FLAG_EVENT_ID' GROUP BY situation_class ORDER BY situation_class" | paste -sd,)
d_situation=${n_situation:-0}
d_params=${n_params:-0}
d_trans=${n_transitions:-0}
d_amb=${n_amb:-0}
log "audit: situation_log rows $d_situation bands[$bands_csv] transitions $d_trans"
log "audit: param_overrides rows $d_params; AMBULANCE_DELAY escalations $d_amb"

[ "$d_situation" -ge 1 ] && [ "$d_params" -ge 1 ] \
    && ok "audit log carries THIS run's decisions: $d_situation situation rows + $d_params parameter rows for the flagship event" \
    || fail "audit log missing this run's decisions (situation rows=$d_situation params rows=$d_params)"
[ "$d_trans" -ge 1 ] \
    && ok "situation band TRANSITIONED mid-morning: $d_trans transitioned rows ($TRANSITIONS; bands this run: $bands_csv)" \
    || fail "no band transition recorded for the flagship (transitioned rows=$d_trans)"
[ "$d_amb" -ge 1 ] \
    && ok "ambulance escalation in the audit log: $d_amb AMBULANCE_DELAY parameter decisions" \
    || fail "no AMBULANCE_DELAY escalation row this run (rows=$d_amb)"

# events table: the flagship is a tier-'none' public procession, regulated
ev_kind=$($PSQL -c "SELECT event_kind FROM events.corridor_events WHERE event_id='$FLAG_EVENT_ID'")
ev_prot=$($PSQL -c "SELECT protection_class FROM events.corridor_events WHERE event_id='$FLAG_EVENT_ID'")
n_phases=$($PSQL -c "SELECT count(*) FROM events.event_phases WHERE event_id='$FLAG_EVENT_ID'")
phase_policy=$($PSQL -c "SELECT closure_policy::text FROM events.event_phases WHERE event_id='$FLAG_EVENT_ID' LIMIT 1")
[ "$ev_kind" = "procession" ] && [ "$ev_prot" = "none" ] \
    && ok "events table: the morning is a PUBLIC procession (kind=procession, protection_class=none, $n_phases phases)" \
    || fail "events table row wrong (kind=$ev_kind protection=$ev_prot)"
printf '%s' "$phase_policy" | grep -q 'signal_hold' \
    && ok "the engine's answer on the phase row: $phase_policy (regulated, not stopped)" \
    || fail "phase closure_policy is not signal_hold (got: $phase_policy)"

# the crime-incident event from the sweep must exist (the sweep's corridor
# op; its id is the one the sweep's run document names)
sweep_kind=""
[ -n "$SWEEP_EVENT_ID" ] && sweep_kind=$($PSQL -c "SELECT event_kind FROM events.corridor_events WHERE event_id='$SWEEP_EVENT_ID'")
[ "$sweep_kind" = "crime_incident" ] \
    && ok "sweep corridor event present: crime_incident $SWEEP_EVENT_ID (the FIR-driven sweep's own operation)" \
    || fail "sweep crime_incident event not found (event ${SWEEP_EVENT_ID:-?}, kind=${sweep_kind:-none})"

# 8c. the scripted pursuit is CLOSED: a pursuit an unattended script opened
#     must not stay open on /ops, nor hold a band for the situation engine
#     (it tracks every planning/approved/active event) — C-F planning ->
#     cancelled under the scripted officer label; the phase-7 shots and
#     the artifacts already carry it
PURSUIT_CLOSED=false
if is_uuid "$PURSUIT_ID"; then
    close_resp=$(curl -s --max-time 30 -X POST "$ORACLE_URL/api/v1/events/$PURSUIT_ID/status" \
        -H 'Content-Type: application/json' \
        -d "{\"to_status\":\"cancelled\",\"officer\":\"$OFFICER\",\"note\":\"showcase run complete: the scripted pursuit is closed (no unit was tasked)\"}" 2>/dev/null)
    if [ "$(printf '%s' "$close_resp" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("to_status"))' 2>/dev/null)" = "cancelled" ]; then
        PURSUIT_CLOSED=true
        ok "the scripted pursuit $PURSUIT_ID is closed: planning -> cancelled by \"$OFFICER\" (C-F) — nothing scripted stays open"
    else
        fail "the scripted pursuit $PURSUIT_ID could not be closed (C-F answer: ${close_resp:0:200})"
    fi
fi

# reversible state: nothing was committed, but restore is still exercised
curl -s -X POST "$ORACLE_URL/api/v1/signals/restore" -H 'Content-Type: application/json' \
    -d "{\"event_id\":\"$FLAG_EVENT_ID\"}" > "$SHOWCASE_DIR/signals_restore.json" 2>/dev/null || true

# 8d. leave the oracle as found: when phase 1b switched the signal-policy
#     writer on for this run, the oracle goes back to its pre-run value now
#     (the exit trap retries on any other way out)
if [ -n "$WRITER_RECREATED" ]; then
    restore_writer \
        || fail "traffic-oracle's signal-policy writer not restored to its pre-run value '${WRITER_ORIG_ENV}' (it may still be ON)"
fi

# ════════════════════════════════════════════════════════════════════════════
# 9. THE RECORD — showcase_results.json + exit code
# ════════════════════════════════════════════════════════════════════════════
NOW_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
FLAG_EVENT_ID="$FLAG_EVENT_ID" SWEEP_EVENT_ID="$SWEEP_EVENT_ID" SWEEP_SEED="$SWEEP_SEED" \
FLAG_RUN_ID="$FLAG_RUN_ID" FLAG_STATUS="$FLAG_STATUS" FLAG_DELIVERY="$FLAG_DELIVERY" \
SWEEP_RUN_ID="$SWEEP_RUN_ID" SWEEP_STATUS="$SWEEP_STATUS" SWEEP_DELIVERED="$SWEEP_DELIVERED" \
PURSUIT_ID="$PURSUIT_ID" APPROVED_REC_ID="$APPROVED_REC_ID" JR_DISPATCH_ID="$JR_DISPATCH_ID" \
VEHICLE_ID="$VEHICLE_ID" STOLEN_PLATE="$STOLEN_PLATE" FIR_REF="$FIR_REF" OFFICER="$OFFICER" \
D_SITUATION="$d_situation" D_PARAMS="$d_params" D_TRANSITIONS="$d_trans" D_AMB="$d_amb" \
BANDS_CSV="$bands_csv" GC_WINDOWS="$GC_WINDOWS" EV_KIND="$ev_kind" EV_PROT="$ev_prot" \
N_PHASES="$n_phases" PHASE_POLICY="${phase_policy:-{\}}" SWEEP_KIND="$sweep_kind" \
RELEASED="$RELEASED" CITY_BAND_AFTER="$CITY_BAND_AFTER" \
SIGNAL_PLAN_SHIFTS="$SIGNAL_PLAN_SHIFTS" SIGNAL_REFUSALS="$SIGNAL_REFUSALS" SIGNAL_MAE="$SIGNAL_MAE" \
REPLAY_SUMMARY="$REPLAY_SUMMARY" ACTIVE_FEED_SEEN="$ACTIVE_FEED_SEEN" JAN_RAKSHAK="$JAN_RAKSHAK_DISPATCHED" SPILL_JUNCTION="$SPILL_JUNCTION" \
CITY_SHOT_OK="$CITY_SHOT_OK" REST_SHOTS_OK="$REST_SHOTS_OK" SHOWCASE_DIR="$SHOWCASE_DIR" \
STACK_ORACLE="$ORACLE_URL" STACK_EMULATOR="$EMULATOR_URL" STACK_DECK="$DECK_ORIGIN" \
STACK_PROJECT="$COMPOSE_PROJECT" STACK_PG="$PG_CONTAINER" \
PURSUIT_TITLE="$PURSUIT_TITLE" PURSUIT_CLOSED="$PURSUIT_CLOSED" NOW_UTC="$NOW_UTC" \
WRITER_RECREATED="$WRITER_RECREATED" WRITER_ORIG_ENV="$WRITER_ORIG_ENV" WRITER_RESTORED="$WRITER_RESTORED" \
FAILED_ASSERTS="$(printf '%s\n' ${FAILED_ASSERTS[@]+"${FAILED_ASSERTS[@]}"} | paste -sd$'\x1f' -)" \
python3 - "$RESULTS_JSON" <<'PYEOF'
import json, os, sys

def load_env_json(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default

def load_file_json(relpath, default):
    p = os.path.join(os.environ["SHOWCASE_DIR"], relpath)
    try:
        return json.load(open(p))
    except Exception:
        return default

def as_int(name):
    try:
        return int(os.environ.get(name) or 0)
    except ValueError:
        return 0

S = os.environ["SHOWCASE_DIR"]
earmark = load_file_json("chain_earmark.json", {})
chain = load_file_json("pursuit_chain.json", {})
jr = load_file_json("jan_rakshak.json", {})
sig = load_file_json("signal_policy.json", {})
twin = load_file_json("twin_audit.json", {})
observe = load_file_json("ambulance_observe.json", [])
transitions = load_file_json("band_easing.json", [])
failed = [a for a in os.environ.get("FAILED_ASSERTS", "").split("\x1f") if a]

sight = chain.get("sightings") or {}
rec = chain.get("recommendation") or {}
app = chain.get("approval") or {}
probe = chain.get("penal_approval_probe") or {}
pur = chain.get("pursuit_event") or {}
inter = chain.get("interception") or {}
tr = chain.get("trace") or {}

pursuit_chain = [
    {"step": 1, "name": "plate_earmarked",
     "detail": f"{os.environ['STOLEN_PLATE']} flagged ({earmark.get('lawful_basis')}, "
               f"{os.environ['FIR_REF']}); earmark_id {earmark.get('earmark_id')}",
     "evidence": f"POST /api/v1/vehicles/{os.environ['STOLEN_PLATE']}/earmark"},
    {"step": 2, "name": "anpr_camera_sightings",
     "detail": f"{sight.get('stolen_this_run')} emulated reads this run on cameras "
               f"{sight.get('cameras')} at {sight.get('speeds_kmh')} km/h "
               f"(matched_plate_hit=stolen_vehicle, source={sight.get('source')})",
     "evidence": f"GET /api/v1/vehicles/{os.environ['VEHICLE_ID']}/sightings"},
    {"step": 3, "name": "stolen_sighting_evidence",
     "detail": f"{rec.get('evidence_recs_this_run')} R_STOLEN_SIGHTING (evidence only) for this "
               f"run's reads; e-challan withheld (penal rules that would have fired: "
               f"{sorted(set(rec.get('withheld_rules') or []))}); "
               f"{rec.get('penal_recs_this_run')} penal recommendations; "
               f"chosen: {(rec.get('chosen') or {}).get('text')}",
     "evidence": f"POST /api/v1/enforcement/evaluate x{sight.get('stolen_this_run')} -> "
                 f"recommendation {(rec.get('chosen') or {}).get('id')} PENDING"},
    {"step": 4, "name": "officer_pursuit_authorisation",
     "detail": f"{app.get('status')} by {app.get('officer')} — '{app.get('note')}'; "
               + (f"an e-challan approval on the plate is refused: HTTP {probe.get('http')} "
                  f"'{probe.get('detail')}'" if probe.get("exercised")
                  else f"e-challan approval probe {probe.get('why')}" if probe.get("unsafe")
                  else "no pending e-challan on the plate to probe in this store"),
     "evidence": "POST /api/v1/enforcement/recommendations/{id}/decision (audit tail append-only; "
                 "a penal approval on a reported-stolen vehicle → 409)"},
    {"step": 5, "name": "pursuit_event",
     "detail": f"{pur.get('title')} [{pur.get('status')}; "
               + ("closed when the run ended: cancelled by the scripted officer]"
                  if os.environ["PURSUIT_CLOSED"] == "true" else "NOT closed]"),
     "evidence": f"POST /api/v1/events vehicle_pursuit {pur.get('event_id')} "
                 f"(metadata vehicle_id+recommendation_id)"},
    {"step": 6, "name": "interception_and_live_trace",
     "detail": (f"{inter.get('n_junctions')} choke junctions ranked (top "
                f"{((inter.get('top') or {}).get('junction_id'))}); trace {tr.get('points')} "
                f"positions source={tr.get('source')}"
                if not inter.get("unavailable") else
                f"interception plan unavailable ({str(inter.get('detail') or '')[:90]}); "
                f"trace {tr.get('points')} positions source={tr.get('source')}"),
     "evidence": f"GET /api/v1/events/{pur.get('event_id')}/interception-plan + "
                 f"GET /api/v1/vehicles/{os.environ['VEHICLE_ID']}/trace (canvas vehicle-traces layer)"},
]

decisions_logged = as_int("D_SITUATION") + as_int("D_PARAMS")

summary = {
  "flagship": {
    "name": "God's Eye Morning",
    "story": "Rath Yatra procession + monsoon burst + a stolen vehicle reported, "
             "one morning, one live stack — pursuit chain (evidence, never a fine), "
             "Jan Rakshak spillover dispatch, ambulance green-corridor escalation, "
             "and the city signal-policy guard, end to end.",
    "composition_use_cases": [
        {"use_case_id": "rath_yatra_monsoon_ambulance", "run_id": as_int("FLAG_RUN_ID"),
         "status": os.environ["FLAG_STATUS"],
         "delivery": load_env_json("FLAG_DELIVERY", {}),
         "event_id": os.environ["FLAG_EVENT_ID"]},
        {"use_case_id": "watchlist_anpr_sweep", "seed": as_int("SWEEP_SEED"),
         "run_id": as_int("SWEEP_RUN_ID"), "status": os.environ["SWEEP_STATUS"],
         "delivery": {"delivered": as_int("SWEEP_DELIVERED")},
         "event_id": os.environ["SWEEP_EVENT_ID"] or None},
    ],
    "route": "Relief Road -> Nehru Bridge Road -> Ashram Road (South) -> "
             "Riverfront Road (West) (1005->1015->1001->1006)",
    "closure_policy": load_env_json("PHASE_POLICY", None),
  },
  # every id this run created — emulator runs and their events are
  # run-scoped (uuid5 over use_case|seed|run), so nothing here repeats
  "run_scoped_ids": {
    "flagship_run_id": as_int("FLAG_RUN_ID"),
    "flagship_event_id": os.environ["FLAG_EVENT_ID"],
    "sweep_run_id": as_int("SWEEP_RUN_ID"),
    "sweep_event_id": os.environ["SWEEP_EVENT_ID"] or None,
    "pursuit_event_id": os.environ["PURSUIT_ID"] or None,
    "pursuit_authorising_recommendation_id": os.environ["APPROVED_REC_ID"] or None,
    "jan_rakshak_dispatch_id": os.environ["JR_DISPATCH_ID"] or None,
    "vehicle_id": os.environ["VEHICLE_ID"] or None,
  },
  "scripted_pursuit_closed": os.environ["PURSUIT_CLOSED"] == "true",
  "approvals": {
    "officer": os.environ["OFFICER"],
    "note": "this script runs unattended: every approval it makes is recorded under this "
            "officer label; in the judged demo a signed-in human approves on /ops",
  },
  "signal_policy_writer": {
    "recreated_for_this_run": os.environ["WRITER_RECREATED"] == "1",
    "pre_run_value": os.environ["WRITER_ORIG_ENV"] if os.environ["WRITER_RECREATED"] == "1" else None,
    "restored_to_pre_run_value": (os.environ["WRITER_RESTORED"] == "true"
                                  if os.environ["WRITER_RECREATED"] == "1" else None),
    "note": ("already on before the run (left as found)"
             if os.environ["WRITER_RECREATED"] != "1" else
             "the C7 writer was switched on for this run only; phase 8d recreated the oracle "
             "with its pre-run value (verified: writer_enabled False, /health 200)"
             if os.environ["WRITER_RESTORED"] == "true" else
             "the C7 writer was switched on for this run only and could NOT be put back to its "
             "pre-run value (see the run log's FIX) — the exit trap retries once"),
  },
  "stack": {"oracle": os.environ["STACK_ORACLE"], "emulator": os.environ["STACK_EMULATOR"],
            "deck": os.environ["STACK_DECK"], "compose_project": os.environ["STACK_PROJECT"],
            "postgis_container": os.environ["STACK_PG"]},
  "event_types": [
      "procession", "monsoon_burst", "market_spillover", "ambulance_green_corridor",
      "stolen_vehicle_report", "anpr_sweep", "crime_incident", "stolen_sighting_evidence",
      "officer_approval", "vehicle_pursuit", "interception_plan", "jan_rakshak_dispatch",
      "signal_band_plan_guard",
  ],
  "pursuit_chain": pursuit_chain,
  "stolen_vehicle_rule": {
    "rule": "C-N: a reported-stolen vehicle's reads are evidence only — no e-challan for the "
            "registered owner; approving a penal recommendation on it is refused (409)",
    "evidence_recs_this_run": rec.get("evidence_recs_this_run"),
    "penal_recs_this_run": rec.get("penal_recs_this_run"),
    "withheld_rules": rec.get("withheld_rules"),
    "penal_approval_probe": probe,
  },
  "counts": {
    "decisions_logged_audit_rows_this_run": decisions_logged,
    "situation_log_rows_this_run": as_int("D_SITUATION"),
    "param_override_rows_this_run": as_int("D_PARAMS"),
    "band_transitions_total": as_int("D_TRANSITIONS"),
    "band_transitions_this_run": as_int("D_TRANSITIONS"),
    "bands": os.environ["BANDS_CSV"],
    "green_corridor_windows": as_int("GC_WINDOWS"),
    "ambulance_delay_escalations_this_run": as_int("D_AMB"),
    "signal_plan_shifts_committed": as_int("SIGNAL_PLAN_SHIFTS"),
    "signal_policy_refusals": as_int("SIGNAL_REFUSALS"),
  },
  "signal_policy_guard": {
    "ruling": "guard-fired + fully-auditable decision = PASS for the band->plan element; "
              "signal_plan_shifts counts COMMITTED writes only",
    "twin_calibration_replay": sig.get("replay"),
    "city_band_live": sig.get("city_band_live"),
    "operator_apply": sig.get("apply"),
    "last_plan_auditable": sig.get("last_plan_auditable"),
    "refusal_reason": (sig.get("apply") or {}).get("reason")
        and f"{(sig.get('apply') or {}).get('reason')}: {(sig.get('apply') or {}).get('shadow_eval', {}).get('detail')} "
            f"({os.environ['REPLAY_SUMMARY']}; the twin calibrates on REAL field observations only)",
  },
  "jan_rakshak": {
    "dispatched": os.environ["JAN_RAKSHAK"] == "true",
    "agency": jr.get("agency"), "agency_label": jr.get("agency_label"),
    "unit": jr.get("unit_id"), "waypoint_junction": as_int("SPILL_JUNCTION"),
    "spillover": "market_spillover behind Swami Vivekananda Road barricades (segment 1013)",
    "statuses": jr.get("statuses"), "audit": jr.get("audit"),
    "projected_unit": jr.get("projected_unit"),
  },
  "ambulance_green_corridor": {
    "escalation_decisions": observe if isinstance(observe, list) else [],
    "audit_rows_this_run": as_int("D_AMB"),
    "twin_windows": twin.get("green_corridor_windows"),
    "window_labels": twin.get("window_labels"),
  },
  "band_transitions": {
    "transitions": transitions if isinstance(transitions, list) else [],
    "released_after_completion": os.environ["RELEASED"] == "yes",
    "city_band_after": os.environ["CITY_BAND_AFTER"],
    "note": "read from adaptive.situation_log (transitioned rows of the flagship event); the "
            "situation engine releases a completed event at its next tick, so it no longer "
            "holds the city band",
  },
  "injection": {
    "active_movements_feed_reflected_event": os.environ["ACTIVE_FEED_SEEN"] == "true",
    "event_row": {"event_kind": os.environ["EV_KIND"],
                  "protection_class": os.environ["EV_PROT"],
                  "phases": as_int("N_PHASES")},
    "sweep_event_kind": os.environ["SWEEP_KIND"],
  },
  "screenshots": sorted(f for f in os.listdir(S) if f.startswith("showcase_") and f.endswith(".png")),
  "screenshot_proofs": {
      "city3d_nonblank": os.environ["CITY_SHOT_OK"] == "true",
      "pursuit_citizen_nonblank": os.environ["REST_SHOTS_OK"] == "true",
      "method": "SwiftShader software WebGL; live-canvas distinct-colour readback + "
                "saved-PNG reload decode (Chrome as decoder), >=8 distinct colours",
  },
  "failed_assertions": failed,
  "passed": not failed,
  "generated_at_utc": os.environ["NOW_UTC"],
}
with open(sys.argv[1], "w") as fh:
    json.dump(summary, fh, indent=2)
PYEOF

log "summary written to $RESULTS_JSON"
echo "────"
if [ "$FAILURES" -eq 0 ]; then
    echo "${BOLD}SHOWCASE PASS${RESET} — God's Eye Morning: procession+monsoon+stolen-vehicle chain (evidence, no fine), Jan Rakshak dispatch, ambulance escalation, signal-policy guard — all real, all audited"
    echo "      runs: flagship $FLAG_RUN_ID (event $FLAG_EVENT_ID), sweep $SWEEP_RUN_ID (event ${SWEEP_EVENT_ID:-?}) | pursuit ${PURSUIT_ID:-?}"
    echo "      decisions(this run): $d_situation situation + $d_params param rows | transitions: $d_trans | green windows: $GC_WINDOWS | signal shifts committed: $SIGNAL_PLAN_SHIFTS (refusals: $SIGNAL_REFUSALS; $REPLAY_SUMMARY) | shots: $(ls "$SHOWCASE_DIR"/showcase_*.png 2>/dev/null | xargs -n1 basename 2>/dev/null | paste -sd, -)"
    exit 0
fi
echo "${BOLD}SHOWCASE FAIL${RESET} — $FAILURES assertion(s):"
for a in "${FAILED_ASSERTS[@]}"; do echo "      - $a"; done
exit 1
