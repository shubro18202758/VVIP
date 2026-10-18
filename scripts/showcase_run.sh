#!/usr/bin/env bash
# showcase_run.sh — the FLAGSHIP demonstration: "GOD'S EYE MORNING".
#
# One Monday monsoon morning over the old city, end to end, no arguments:
#
#   09:20  The 148th Rath Yatra is three chariots deep on Gandhi Road
#          (EXTREME crowd, lakhs on the street). A monsoon burst at 18 mm/h
#          waterlogs the Nehru Bridge approach. Behind the Tilak Road
#          barricades the bazaar stands. (use_case rath_yatra_monsoon_
#          ambulance — procession + monsoon + market spillover, injected
#          through the event emulator's REAL HTTP API into the RUNNING
#          fleet.)
#   09:2x  A 108 cardiac ambulance requests a green corridor across the
#          river — the escalation channel decides AMBULANCE_DELAY mid-
#          chaos and the twin's audited schedule opens 4 green-corridor
#          windows on Relief Road -> Nehru Bridge -> Riverfront West ->
#          Ashram South.
#   09:3x  A STOLEN VEHICLE is reported (FIR, plate GJ27EM9001). The full
#          enforcement chain runs through the real stack:
#            plate EARMARKED (flagged, lawful basis stolen_vehicle_fir)
#            -> emulated ANPR camera sightings (4 cameras, matched_plate_
#               hit 'stolen_vehicle', source 'emulated_anpr')
#            -> deterministic e-challan RECOMMENDATION (R_SPD_SEGMENT,
#               90-115 km/h in a 40-60 zone) — PENDING, a proposal only
#            -> HUMAN-STYLE APPROVAL (a duty officer decision; the only
#               pending->approved transition in the system)
#            -> PURSUIT EVENT opened by the operator (vehicle_pursuit
#               corridor event carrying vehicle_id + recommendation_id)
#            -> INTERCEPTION PLAN (pgRouting choke-junction ranking off
#               the latest sighting)
#            -> LIVE TRACE on the God's-eye canvas (the vehicle-traces
#               layer renders GET /api/v1/vehicles/{id}/trace).
#   09:4x  A Jan Rakshak unit is dispatched to the Tilak Road spillover:
#            requested -> duty-officer APPROVED -> en_route -> on_scene,
#            every transition audited, the emulated unit projected live.
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
#   1. HEALTH   — 7-container fleet + host models + emulator + deck, fail
#                 fast with the exact fix hint if not (and it brings up
#                 the one thing the flagship owns: VVIP_SIGNAL_POLICY_WRITER
#                 on traffic-oracle, via compose).
#   2. ACTION   — every beat above through the emulator + real APIs.
#   3. ASSERT   — the real stack did the work, each verifiable from
#                 API/store state: audit-log decision rows, band
#                 transitions, green-corridor windows, the signal-policy
#                 guard decision, and every pursuit-chain step.
#   4. EVIDENCE — headless SwiftShader screenshots of the CANVAS (the
#                 SvelteKit command-deck's /gods-eye Cesium 3D city and
#                 /citizen advisory), each proven non-blank at the pixel
#                 level, into VVIP/showcase/.
#   5. RESULT   — VVIP/showcase/showcase_results.json + nonzero exit on
#                 ANY failed assertion.
#
# Usage: bash VVIP/scripts/showcase_run.sh     (no arguments)
# Env:   CHROME_PATH optional (defaults exactly like verify_all.sh /
#        frontend/tests/e2e / gods-eye-smoke.mjs — the puppeteer cache).
#        Endpoints/containers are env-overridable with the loopback
#        defaults preserved (see the block below): *_URL/*_PORT for the
#        services, DECK_URL/DECK_PORT for the deck, COMPOSE_PROJECT /
#        COMPOSE_FILES / *_CONTAINER for the fleet (smoke_models.sh and
#        hooks.server.ts use the same ${VAR:-default} pattern).
#
# Security: every URL below is an internal endpoint (an env-overridable
# config constant of this script, http scheme, loopback default) — none
# comes from external input, so no host validation is required before
# these requests (the convoy-brain http_guard carve-out for platform
# services).

set -uo pipefail

VVIP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHOWCASE_DIR="$VVIP_DIR/showcase"
INFRA_DIR="$VVIP_DIR/infra"

# ── internal endpoints (env-overridable constants, loopback-only defaults) ──
# The hooks.server.ts pattern in URL form: *_PORT tunes the port, *_URL
# overrides the whole origin. Defaults reproduce the historical fixed
# constants exactly.
ORACLE_URL="${ORACLE_URL:-http://127.0.0.1:${ORACLE_PORT:-8081}}"
BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:${BRAIN_PORT:-8080}}"
EMULATOR_URL="${EMULATOR_URL:-http://127.0.0.1:${EMULATOR_PORT:-8090}}"
DECIDER_URL="${DECIDER_URL:-http://127.0.0.1:${DECIDER_PORT:-8083}}"
LLAMA_URL="${LLAMA_URL:-http://127.0.0.1:${LLAMA_PORT:-8084}}"
EMBED_URL="${EMBED_URL:-http://127.0.0.1:${EMBED_PORT:-8085}}"
DECK_CONTAINER_URL="${DECK_URL:-http://127.0.0.1:${DECK_PORT:-5173}}"
# The interface-revamp end state (2026-10-13): the container vvip-command-deck
# IS the SvelteKit deck on :5173 (the React dashboard and the ad-hoc
# `PORT=5199 node build` spawn are retired) — same origin serves /gods-eye,
# /citizen and the /api/v1 reverse proxy (src/hooks.server.ts). One origin,
# one constant: the canvas origin IS the deck container.
DECK_ORIGIN="$DECK_CONTAINER_URL"

# ── fleet addressing (compose project + derived container names) ────────────
# COMPOSE_PROJECT drives the derived container names (project-service), so a
# parallel stack (e.g. -p vvip-argus) needs one override, not seven; any
# single *_CONTAINER can still be pinned explicitly.
COMPOSE_PROJECT="${COMPOSE_PROJECT:-vvip}"
COMPOSE_FILES="${COMPOSE_FILES:--f compose.yml -f compose.gb10-override.yml}"
PG_CONTAINER="${POSTGIS_CONTAINER:-${COMPOSE_PROJECT}-postgis}"
VALKEY_CONTAINER="${VALKEY_CONTAINER:-${COMPOSE_PROJECT}-valkey}"
NATS_CONTAINER="${NATS_CONTAINER:-${COMPOSE_PROJECT}-nats}"
ORACLE_CONTAINER="${ORACLE_CONTAINER:-${COMPOSE_PROJECT}-traffic-oracle}"
BRAIN_CONTAINER="${BRAIN_CONTAINER:-${COMPOSE_PROJECT}-convoy-brain}"
INGRESS_CONTAINER="${INGRESS_CONTAINER:-${COMPOSE_PROJECT}-signal-ingress}"
DECK_CONTAINER="${DECK_CONTAINER:-${COMPOSE_PROJECT}-command-deck}"
PG_USER="${POSTGIS_USER:-corridor_admin}"
PG_DB="${POSTGIS_DB:-corridor_db}"
PSQL="docker exec $PG_CONTAINER psql -U $PG_USER -d $PG_DB -At"

# ── the morning's cast (deterministic identities) ───────────────────────────
FLAGSHIP_ID="rath_yatra_monsoon_ambulance"
FLAG_SEED=202506271
FLAG_EVENT_ID="$(python3 -c "import uuid; print(uuid.uuid5(uuid.uuid5(uuid.NAMESPACE_URL,'vvip-event-emulator'),'${FLAGSHIP_ID}|${FLAG_SEED}'))")"
SWEEP_ID="watchlist_anpr_sweep"
SWEEP_BASE_SEED=20261105
SWEEP_SEED=$((SWEEP_BASE_SEED + (EPOCHSECONDS % 899999)))   # fresh per run
STOLEN_PLATE="GJ27EM9001"                                    # tape.py _STOLEN_LABEL
OFFICER="Duty Officer R. Mehta (God's Eye Morning)"
FIR_REF="FIR 4401/2026, Kalupur PS"

CLOCK_SCALE=30        # flagship: 60 sim-min in 120 s wall (mid-chaos room)
SWEEP_CLOCK=60        # sweep:    45 sim-min in 45 s wall
TICK_SEC=10
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

# ════════════════════════════════════════════════════════════════════════════
# 1. HEALTH — fleet + host models + emulator + deck, fail fast
# ════════════════════════════════════════════════════════════════════════════
log "phase 1: fleet + host-model health (fail fast)"

for c in "$PG_CONTAINER" "$VALKEY_CONTAINER" "$NATS_CONTAINER" "$ORACLE_CONTAINER" "$BRAIN_CONTAINER" "$INGRESS_CONTAINER" "$DECK_CONTAINER"; do
    [ "$(docker inspect "$c" --format '{{.State.Running}}' 2>/dev/null)" = "true" ] \
        || die_hint "container $c is not running" \
            "cd $INFRA_DIR && docker compose -p $COMPOSE_PROJECT $COMPOSE_FILES up -d"
done
pg_ok=$(docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" 2>&1) \
    || die_hint "postgis not answering: $pg_ok" "docker logs $PG_CONTAINER --tail 50"
[ "$(docker exec "$VALKEY_CONTAINER" valkey-cli ping 2>/dev/null)" = "PONG" ] \
    || die_hint "valkey not answering PONG" "docker logs $VALKEY_CONTAINER --tail 50"

[ "$(hcode "$ORACLE_URL/health")" = "200" ] \
    || die_hint "traffic-oracle $ORACLE_URL/health not 200" \
        "cd $INFRA_DIR && docker compose -p $COMPOSE_PROJECT $COMPOSE_FILES up -d --force-recreate traffic-oracle"
cb=$(health "$BRAIN_URL/health" 15)
printf '%s' "$cb" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("status")=="ok" and d.get("strategic_tier",{}).get("model_host")=="connected" and d.get("tactical_tier",{}).get("llama_server")=="connected" else 1)' 2>/dev/null \
    || die_hint "convoy-brain :8080 not ok (decider+gemma must be connected)" \
        "check host models (infra/scripts/serve_models.sh), then restart convoy-brain; body was: $cb"
# The ONE deck: GET / IS the Convoy Command cockpit — the restored VVIP
# template home (design-smoke contract: 200 + data-template="convoy-command");
# /ops remains the situation wall on the SAME origin the canvas uses.
deck_root="$(health "$DECK_CONTAINER_URL/" 15)"
if [ "$(hcode "$DECK_CONTAINER_URL/" 15)" = "200" ] \
   && printf '%s' "$deck_root" | grep -q 'data-template="convoy-command"' \
   && [ "$(hcode "$DECK_CONTAINER_URL/ops" 15)" = "200" ]; then
    ok "command-deck container $DECK_CONTAINER_URL serving the cockpit (200 + convoy-command template; /ops 200)"
else
    die_hint "command-deck container $DECK_CONTAINER_URL not serving the cockpit (want / = 200 + convoy-command template, /ops = 200)" \
        "cd $INFRA_DIR && docker compose -p $COMPOSE_PROJECT $COMPOSE_FILES up -d --no-deps --build command-deck"
fi

emu=$(health "$EMULATOR_URL/health")
printf '%s' "$emu" | python3 -c '
import json,sys
d=json.load(sys.stdin)
sys.exit(0 if d.get("status")=="ok" and d.get("stack",{}).get("store") and d.get("stack",{}).get("valkey") and d.get("stack",{}).get("twin") and d.get("stack",{}).get("nats") else 1)' 2>/dev/null \
    || die_hint "event-emulator $EMULATOR_URL/health not fully wired (body: $emu)" \
        "cd $VVIP_DIR/services/event-emulator && VVIP_DATABASE_URL=postgresql://$PG_USER:${POSTGIS_PASSWORD:-dev_changeme}@localhost:${POSTGIS_PORT:-55434}/$PG_DB .venv/bin/python -m event_emulator (nohup, log /tmp/emulator.log)"
lib=$(health "$EMULATOR_URL/api/v1/emulator/use-cases")
n_lib=$(printf '%s' "$lib" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)
[ "$n_lib" -ge 10 ] || die_hint "emulator library has $n_lib use cases (needs both flagship scenarios)" \
    "restart the :8090 emulator process so it reloads use_case_library.json (10 entries)"
printf '%s' "$lib" | grep -q "\"$FLAGSHIP_ID\"" || die_hint "emulator library lacks $FLAGSHIP_ID" "restart the :8090 emulator process"
printf '%s' "$lib" | grep -q "\"$SWEEP_ID\""     || die_hint "emulator library lacks $SWEEP_ID" "restart the :8090 emulator process"

printf '%s' "$(health "$DECIDER_URL/decider/health")" | grep -q '"loaded": *true' \
    || die_hint "decider :8083 not loaded" "bash $VVIP_DIR/infra/scripts/serve_models.sh (pplx-decider-v1-27b on :8083)"
[ "$(hcode "$LLAMA_URL/health" 15)" = "200" ] \
    || die_hint "llama-server :8084 not answering" "bash $VVIP_DIR/infra/scripts/serve_models.sh (aditi-ps18-v7h-12b on :8084)"
[ "$(hcode "$EMBED_URL/health" 15)" = "200" ] \
    || die_hint "embedding-service :8085 not answering" "bash $VVIP_DIR/infra/scripts/serve_models.sh (embeddings on :8085)"

# 1b. the signal-policy writer flag (the flagship owns bringing it up)
writer_on=$(health "$ORACLE_URL/api/v1/signals/plans" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("writer_enabled"))' 2>/dev/null || echo error)
if [ "$writer_on" != "True" ]; then
    log "signal-policy writer disabled — recreating traffic-oracle with VVIP_SIGNAL_POLICY_WRITER=1"
    ( cd "$INFRA_DIR" && VVIP_SIGNAL_POLICY_WRITER=1 docker compose -p "$COMPOSE_PROJECT" $COMPOSE_FILES up -d traffic-oracle ) \
        || die_hint "could not recreate traffic-oracle with the writer flag" \
            "cd $INFRA_DIR && VVIP_SIGNAL_POLICY_WRITER=1 docker compose -p $COMPOSE_PROJECT $COMPOSE_FILES up -d traffic-oracle"
    for i in $(seq 1 40); do
        [ "$(hcode "$ORACLE_URL/health" 5)" = "200" ] && break
        sleep 2
    done
    writer_on=$(health "$ORACLE_URL/api/v1/signals/plans" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("writer_enabled"))' 2>/dev/null || echo error)
fi
[ "$writer_on" = "True" ] \
    && ok "signal-policy writer ENABLED (C7 flag; commits still fail closed without a calibrated twin)" \
    || die_hint "signal-policy writer still disabled after recreate" \
        "docker exec $ORACLE_CONTAINER env | grep VVIP_SIGNAL_POLICY_WRITER (must be 1); see $INFRA_DIR/compose.yml traffic-oracle.environment"

ok "fleet (7 containers), emulator $EMULATOR_URL, models $DECIDER_URL/$LLAMA_URL/$EMBED_URL, deck $DECK_CONTAINER_URL (the ONE deck), writer flag — all healthy"
log "flagship event id: $FLAG_EVENT_ID (uuid5 over $FLAGSHIP_ID|$FLAG_SEED); sweep seed this run: $SWEEP_SEED"

# 1c. the canvas origin — the container itself now (retired with the
# interface revamp: the old ad-hoc `PORT=5199 node build` spawn and its
# EXIT-trap cleanup; vvip-command-deck serves /gods-eye + /citizen + the
# /api/v1 reverse proxy on the one origin).
[ "$(hcode "$DECK_ORIGIN/gods-eye" 10)" = "200" ] && [ "$(hcode "$DECK_ORIGIN/citizen" 10)" = "200" ] \
    && ok "command-deck container serving $DECK_ORIGIN (canvas origin; /gods-eye + /citizen up; /api/v1 proxies to the live oracle)" \
    || die_hint "deck canvas origin not answering on $DECK_ORIGIN" \
        "cd $INFRA_DIR && docker compose -p $COMPOSE_PROJECT $COMPOSE_FILES up -d --no-deps --build command-deck"
[ "$(hcode "$DECK_ORIGIN/api/v1/movements/active" 10)" = "200" ] \
    && ok "deck-origin /api/v1 proxy live (hooks.server.ts → traffic-oracle)" \
    || die_hint "deck origin /api/v1 proxy not answering" \
        "the container must be host-networked (compose.gb10-override.yml) for the loopback proxy; docker logs $DECK_CONTAINER"

# ════════════════════════════════════════════════════════════════════════════
# 2. BASELINE — counts before the morning starts (deltas are this run's)
# ════════════════════════════════════════════════════════════════════════════
log "phase 2: baseline audit counts"
base_situation=$($PSQL -c "SELECT count(*) FROM adaptive.situation_log WHERE movement_id='$FLAG_EVENT_ID'")
base_params=$($PSQL -c "SELECT count(*) FROM adaptive.param_overrides WHERE movement_id='$FLAG_EVENT_ID'")
base_trans=$($PSQL -c "SELECT count(*) FROM adaptive.situation_log WHERE movement_id='$FLAG_EVENT_ID' AND transitioned")
base_amb=$($PSQL -c "SELECT count(*) FROM adaptive.param_overrides WHERE movement_id='$FLAG_EVENT_ID' AND evidence::text LIKE '%AMBULANCE_DELAY%'")
log "baseline: situation_log=$base_situation params=$base_params transitions=$base_trans ambulance_escalations=$base_amb"

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
( curl -s --max-time 300 -X POST "$EMULATOR_URL/api/v1/emulator/runs" \
    -H 'Content-Type: application/json' \
    -d "{\"use_case_id\":\"$FLAGSHIP_ID\",\"clock_scale\":$CLOCK_SCALE,\"tick_sec\":$TICK_SEC}" \
    > "$SHOWCASE_DIR/injection_flagship.json" 2>"$SHOWCASE_DIR/injection_flagship.err" ) &
FLAG_INJECT_PID=$!

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

# the situation engine must start deciding (audit rows appear).
# LIKE-FOR-LIKE with the phase-2 baseline: both sides are uncapped SQL
# counts ($PSQL).  The /adaptive/situation_log API caps `count` at its
# limit (le=500, adaptive/surfacing.py) — once accumulated rows for the
# deterministic flagship movement crossed 500, a capped count could
# never exceed the uncapped baseline, this loop burned its full 120 s,
# and the Jan Rakshak dispatch (phase 4c) landed AFTER the flagship
# tape's terminal complete frame → event 'completed' → dispatch 409.
rows_after=-1
for i in $(seq 1 60); do
    n=$($PSQL -c "SELECT count(*) FROM adaptive.situation_log WHERE movement_id='$FLAG_EVENT_ID'" 2>/dev/null || echo 0)
    if [ "$n" -gt "$base_situation" ]; then rows_after=$i; break; fi
    sleep 2
done
if [ "$rows_after" -ge 0 ] 2>/dev/null; then
    ok "situation engine deciding on the flagship after ~$((rows_after*2))s (audit rows growing)"
else
    log "note: engine rows not yet above baseline — the explicit observe calls below still drive the audit path"
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

out = [summarize("window_opening", post({
    "predicted_congestion_idx": 0.55, "weather_severity": 0.35, "weather": "RAIN",
    "crowd_score": 0.55, "crowd_class": "EXTREME", "duration_class": "hours",
    "event_active": True, "ambulance_delay_projection_sec": 45,
    "route_segment_ids": [1005, 1015, 1006, 1001],
}))]

# Band escalation is confirmation-gated: hysteresis up_confirm=2 requires TWO
# consecutive higher-band observations before effective_class carries the new
# band (adaptive/hysteresis.py) — and the engine loop interleaves its own
# ticks on the same movement every ~5 s from the live feed doc.  So the 108
# dispatcher RE-POLLS: the 120 s projection is re-posted (bounded train,
# early exit — the exact mirror of phase 8b's down_confirm=4 easing train)
# until the situation band actually carries the escalation.
peak_doc = {
    "predicted_congestion_idx": 0.92, "weather_severity": 0.6, "weather": "RAIN",
    "crowd_score": 0.92, "crowd_class": "EXTREME", "duration_class": "hours",
    "event_active": True, "ambulance_delay_projection_sec": 120,
    "route_segment_ids": [1005, 1015, 1006, 1001],
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
    && ok "ambulance escalation decided mid-chaos ($(python3 -c 'import json;d=json.load(open("'$OBSERVE_JSON'"));p=d[-1];print(p["effective_class"], json.dumps(p["escalation"]))' 2>/dev/null))" \
    || fail "ambulance escalation decision missing (see $OBSERVE_JSON)"

# 4c. JAN RAKSHAK — dispatch to the Tilak Road market spillover (segment 1013)
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
    "note": "market spillover behind Tilak Road barricades (segment 1013) — crowd management support",
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
[ "$JR_OK" = "yes" ] \
    && ok "Jan Rakshak dispatched: requested->approved->en_route->on_scene (audited); live unit projected: $JR_UNIT" \
    || fail "Jan Rakshak dispatch lifecycle incomplete (see $JR_JSON)"
JAN_RAKSHAK_DISPATCHED=$([ "$JR_OK" = "yes" ] && echo true || echo false)

# 4d. CITY SIGNAL PLANS vs THE BAND — calibration replay + live-band apply;
#     the C7 guard's decision must be fully auditable (ruling on record).
log "phase 4d: city signal-policy engine vs the situation band"
SIGNALS_JSON="$SHOWCASE_DIR/signal_policy.json"
ORACLE="$ORACLE_URL" EVENT="$FLAG_EVENT_ID" python3 - > "$SIGNALS_JSON" <<'PYEOF'
import json, os, urllib.request, urllib.error

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

# 1. the twin calibration replay over the observed window (honest numbers)
code, replay = post("/api/v1/simulate/replay",
    {"window_start": "2026-10-11T00:00:00Z", "window_end": "2026-10-14T00:00:00Z"})
# 2. the LIVE city band as the situation engine sees it
band_live = get("/api/v1/signals/plans").get("city_band")
# 3. the operator action: apply the live band's city plan for the flagship
band = band_live if band_live in ("AMBER", "RED", "BLACK") else "RED"
code_apply, apply_resp = post("/api/v1/signals/plans/apply",
    {"band": band, "event_id": os.environ["EVENT"],
     "route_segment_ids": [1005, 1015, 1006, 1001]})
detail = apply_resp.get("detail", apply_resp) if code_apply != 200 else apply_resp
# 4. the auditable decision surface
plans = get("/api/v1/signals/plans")
last = plans.get("last_plan") or {}
committed = bool(code_apply == 200 and apply_resp.get("applied"))
refused = (not committed) and bool(last) and last.get("band") == band
history = get("/api/v1/signals/history?limit=20").get("history", [])
print(json.dumps({
    "replay": {"status": replay.get("status"), "n_observations": replay.get("n_observations"),
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
ok = (rep.get("n_observations") or 0) >= 30 \
     and rep.get("calibrated") is False \
     and ap.get("applied") is False \
     and (len(ap.get("planned_junctions") or []) >= 1) \
     and se.get("detail") == "twin_not_calibrated" \
     and lse.get("detail") == "twin_not_calibrated" \
     and (len(lp.get("planned_junctions") or []) >= 1)
print("yes" if ok else "no")' "$SIGNALS_JSON" 2>/dev/null || echo no)
if [ "$SIGNAL_GUARD_OK" = "yes" ]; then
    MAE=$(python3 -c 'import json;print(json.load(open("'$SIGNALS_JSON'"))["replay"]["speed_mae_kmh"])' 2>/dev/null)
    NJ=$(python3 -c 'import json;print(len(json.load(open("'$SIGNALS_JSON'"))["last_plan_auditable"]["planned_junctions"]))' 2>/dev/null)
    ok "signal-policy engine end-to-end: band $(python3 -c 'import json;print(json.load(open("'$SIGNALS_JSON'"))["city_band_live"])' 2>/dev/null) -> plan for $NJ junctions proposed -> C7 guard REFUSED the commit (twin_not_calibrated; replay MAE ${MAE} km/h vs 5.0 gate) -> decision auditable in GET /signals/plans"
else
    fail "signal-policy guard sequence not auditable as expected (see $SIGNALS_JSON — a COMMIT would also fail this check; committed writes are counted, not asserted absent)"
fi
SIGNAL_PLAN_SHIFTS=$(python3 -c 'import json;print(json.load(open("'$SIGNALS_JSON'")).get("committed_writes",0))' 2>/dev/null || echo 0)
SIGNAL_REFUSALS=$(python3 -c 'import json;print(json.load(open("'$SIGNALS_JSON'")).get("policy_refusals",0))' 2>/dev/null || echo 0)
SIGNAL_MAE=$(python3 -c 'import json;print(json.load(open("'$SIGNALS_JSON'"))["replay"]["speed_mae_kmh"])' 2>/dev/null || echo unknown)

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

# 4f. let the flagship run finish, then audit the green-corridor windows
wait "$FLAG_INJECT_PID" 2>/dev/null
FLAG_RUN_ID=$(python3 -c 'import json;print(json.load(open("'$SHOWCASE_DIR'/injection_flagship.json")).get("run_id",""))' 2>/dev/null || echo "")
FLAG_DELIVERY=$(python3 -c 'import json;print(json.dumps(json.load(open("'$SHOWCASE_DIR'/injection_flagship.json")).get("delivery",{})))' 2>/dev/null || echo "{}")
log "flagship run_id=$FLAG_RUN_ID delivery=$FLAG_DELIVERY"
[ -n "$FLAG_RUN_ID" ] || fail "flagship emulator run returned no run_id"

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
GC_WINDOWS=$(python3 -c 'import json;print(json.load(open("'$TWIN_JSON'")).get("green_corridor_windows",0))' 2>/dev/null || echo 0)
[ "$GC_WINDOWS" -ge 1 ] \
    && ok "green corridor GRANTED: $GC_WINDOWS emergency_vehicle windows in the twin's audited schedule ($(python3 -c 'import json;print("; ".join(json.load(open("'$TWIN_JSON'")).get("window_labels",[])[:2]))' 2>/dev/null))" \
    || fail "no green-corridor window in the twin audited schedule (see $TWIN_JSON)"

# ════════════════════════════════════════════════════════════════════════════
# 5. THE SWEEP — emulated ANPR cameras read the stolen plate (live)
# ════════════════════════════════════════════════════════════════════════════
log "phase 5: ANPR sweep $SWEEP_ID (seed $SWEEP_SEED, clock $SWEEP_CLOCK) — the stolen plate crosses the cameras"
curl -s --max-time 240 -X POST "$EMULATOR_URL/api/v1/emulator/runs" \
    -H 'Content-Type: application/json' \
    -d "{\"use_case_id\":\"$SWEEP_ID\",\"seed\":$SWEEP_SEED,\"clock_scale\":$SWEEP_CLOCK,\"tick_sec\":$TICK_SEC}" \
    > "$SHOWCASE_DIR/injection_sweep.json"
SWEEP_RUN_ID=$(python3 -c 'import json;print(json.load(open("'$SHOWCASE_DIR'/injection_sweep.json")).get("run_id",""))' 2>/dev/null || echo "")
SWEEP_DELIVERED=$(python3 -c 'import json;print(json.load(open("'$SHOWCASE_DIR'/injection_sweep.json")).get("delivery",{}).get("delivered",0))' 2>/dev/null || echo 0)
log "sweep run_id=$SWEEP_RUN_ID delivered=$SWEEP_DELIVERED frames"
[ -n "$SWEEP_RUN_ID" ] && [ "$SWEEP_DELIVERED" -gt 0 ] \
    && ok "ANPR sweep delivered $SWEEP_DELIVERED frames into the live stack" \
    || fail "ANPR sweep did not deliver (see $SHOWCASE_DIR/injection_sweep.json)"

# ════════════════════════════════════════════════════════════════════════════
# 6. THE CHAIN — recommendation -> approval -> pursuit -> interception ->
#                live trace (every step a real API call, asserted)
# ════════════════════════════════════════════════════════════════════════════
log "phase 6: the stolen-vehicle chain (sightings -> challan -> approval -> pursuit -> trace)"
CHAIN_JSON="$SHOWCASE_DIR/pursuit_chain.json"
ORACLE="$ORACLE_URL" VID="$VEHICLE_ID" RUN="$SWEEP_RUN_ID" PLATE="$STOLEN_PLATE" \
OFFICER="$OFFICER" FIR="$FIR_REF" python3 - > "$CHAIN_JSON" <<'PYEOF'
import json, os, urllib.request, urllib.error

def get(path):
    with urllib.request.urlopen(os.environ["ORACLE"] + path, timeout=30) as r:
        return json.loads(r.read())

def post(path, body):
    req = urllib.request.Request(os.environ["ORACLE"] + path,
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())

def attempt(fn, *a, **k):
    """Run one chain step; an HTTP refusal is RECORDED, never fatal —
    the artifact must always land so the assertions judge real state."""
    try:
        return fn(*a, **k)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:300]
        except Exception:
            detail = ""
        return {"_http_error": e.code, "_detail": detail}

vid, run, plate = os.environ["VID"], int(os.environ["RUN"]), os.environ["PLATE"]

# 2. the emulated ANPR camera sightings of the stolen plate (this run)
sight = attempt(get, f"/api/v1/vehicles/{vid}/sightings?limit=200")
if "_http_error" in sight:
    sight = {"sightings": []}
sightings = sight.get("sightings", [])
stolen = [s for s in sightings if s.get("matched_plate_hit") == "stolen_vehicle"]
this_run = [s for s in stolen if s.get("run_id") == run]

# 3. deterministic e-challan recommendations on this run's reads
fired, created, eval_errors = [], [], []
for s in this_run:
    resp = attempt(post, "/api/v1/enforcement/evaluate", {"sighting_id": s["sighting_id"]})
    if "_http_error" in resp:
        eval_errors.append(resp)
        continue
    fired += resp.get("fired", [])
    created += resp.get("created", [])

# 4. human-style approval of one penalty recommendation (prefer R_SPD_SEGMENT)
pending_rows = attempt(get, "/api/v1/enforcement/recommendations?status=pending&limit=200")
pending = pending_rows.get("recommendations", []) if "_http_error" not in pending_rows else []
mine = [r for r in pending if r["plate"] == plate]
pool = [r for r in mine if r["rule_id"] == "R_SPD_SEGMENT"] or mine
rec = pool[0] if pool else None
approval = None
if rec:
    approval = attempt(post,
        f"/api/v1/enforcement/recommendations/{rec['recommendation_id']}/decision",
        {"decision": "approve", "officer": os.environ["OFFICER"],
         "note": f"e-challan approved per {os.environ['FIR']} — speed evidence reviewed"})

# 5. the pursuit event (human-initiated; carries vehicle + recommendation)
pursuit = None
pursuit_title = None
if approval and approval.get("status") == "approved":
    pursuit_title = f"PURSUIT — stolen {plate} ({os.environ['FIR']})"
    pursuit = attempt(post, "/api/v1/events", {
        "event_kind": "vehicle_pursuit", "protection_class": "none", "crowd_class": "LOW",
        "title": pursuit_title,
        "description": ("God's Eye Morning: operator-opened pursuit of a FIR-named stolen "
                        "vehicle after an approved e-challan recommendation and emulated "
                        "ANPR sightings. Vehicles only (C6); interception plan assists the operator."),
        "route_spines": [{"label": "old-city sweep belt", "segment_ids": [1012, 1015, 1005, 1013]}],
        "metadata": {"vehicle_id": vid, "recommendation_id": rec["recommendation_id"],
                     "fir": os.environ["FIR"], "opened_by": os.environ["OFFICER"]},
    })

# 6. the interception plan + the live trace (supporting evidence; the plan
#    keys on the LATEST emulated read, which may sit outside the 300 m
#    attribution band — a 422 there is recorded, not fatal)
plan = trace = None
if pursuit and pursuit.get("event_id"):
    plan = attempt(get, f"/api/v1/events/{pursuit['event_id']}/interception-plan")
    trace = attempt(get, f"/api/v1/vehicles/{vid}/trace?window_sec=86400")

print(json.dumps({
    "vehicle_id": vid, "plate": plate,
    "sightings": {"stolen_total": len(stolen), "stolen_this_run": len(this_run),
                  "cameras": sorted({s["camera_id"] for s in this_run}),
                  "speeds_kmh": [s["speed_kmh"] for s in this_run],
                  "source": (this_run[0]["source"] if this_run else None)},
    "recommendation": {"fired_rules": fired,
                       "created": len(created),
                       "eval_errors": eval_errors,
                       "chosen": {"id": rec["recommendation_id"], "rule_id": rec["rule_id"],
                                  "text": rec["recommendation"]} if rec else None},
    "approval": {"status": approval.get("status"), "officer": approval.get("decided_by"),
                 "audit_tail_last": (approval.get("audit_tail") or [{}])[-1]} if approval else None,
    "pursuit_event": {"event_id": pursuit.get("event_id"), "kind": "vehicle_pursuit",
                      "status": pursuit.get("status"), "metadata": pursuit.get("metadata") or {},
                      "title": pursuit_title} if pursuit else None,
    "interception": ({"n_junctions": len(plan.get("interception_junctions", [])),
                      "top": (plan.get("interception_junctions") or [{}])[0],
                      "latest_sighting": plan.get("latest_sighting")}
                     if plan and "_http_error" not in plan
                     else {"unavailable": True,
                           "http": (plan or {}).get("_http_error"),
                           "detail": (plan or {}).get("_detail")}),
    "trace": {"points": len(trace.get("positions", [])),
              "source": trace.get("source"),
              "first": trace.get("positions", [{}])[0].get("t"),
              "last": trace.get("positions", [{}])[-1].get("t")} if trace and "_http_error" not in trace else None,
}, indent=1))
PYEOF

read -r N_STOLEN_THIS N_STOLEN_ALL REC_CREATED APPROVED PURSUIT_ID N_INTERCEPT N_TRACE TRACE_SRC < <(python3 -c '
import json
d=json.load(open("'"$CHAIN_JSON"'"))
rec=(d.get("recommendation") or {}).get("chosen") or {}
app=d.get("approval") or {}
pur=(d.get("pursuit_event") or {})
inter=(d.get("interception") or {})
tr=(d.get("trace") or {})
pid=pur.get("event_id") or "-"
print((d.get("sightings") or {}).get("stolen_this_run",0),
      (d.get("sightings") or {}).get("stolen_total",0),
      (d.get("recommendation") or {}).get("created",0),
      1 if app.get("status")=="approved" else 0,
      pid if pid != "-" else "-",
      inter.get("n_junctions",0),
      tr.get("points",0),
      tr.get("source") or "-")' 2>/dev/null || echo "0 0 0 0 - 0 0 -")
[ "$PURSUIT_ID" = "-" ] && PURSUIT_ID=""

[ "$N_STOLEN_THIS" -ge 2 ] \
    && ok "chain[2/6] emulated ANPR cameras sighted the stolen plate: $N_STOLEN_THIS reads this run ($([ "$N_STOLEN_ALL" -gt "$N_STOLEN_THIS" ] && echo "$N_STOLEN_ALL incl. earlier runs" || echo this vehicle only), source $TRACE_SRC)" \
    || fail "chain[2/6] fewer than 2 stolen-plate sightings this run (got $N_STOLEN_THIS)"
[ "$REC_CREATED" -ge 1 ] \
    && ok "chain[3/6] e-challan RECOMMENDATION created ($(python3 -c 'import json;d=json.load(open("'$CHAIN_JSON'"));print(d["recommendation"]["chosen"]["text"])' 2>/dev/null)) — PENDING until a human decides" \
    || fail "chain[3/6] no e-challan recommendation created (rules fired: $(python3 -c 'import json;d=json.load(open("'$CHAIN_JSON'"));print(d["recommendation"]["fired_rules"])' 2>/dev/null))"
[ "$APPROVED" = "1" ] \
    && ok "chain[4/6] HUMAN-STYLE APPROVAL: $(python3 -c 'import json;d=json.load(open("'$CHAIN_JSON'"));a=d["approval"];print(a["status"],"by",a["officer"])' 2>/dev/null)" \
    || fail "chain[4/6] approval did not land (see $CHAIN_JSON)"
[ -n "$PURSUIT_ID" ] && [ "$PURSUIT_ID" != "" ] \
    && ok "chain[5/6] PURSUIT EVENT opened by the operator: $PURSUIT_ID (vehicle_pursuit, carries vehicle_id+recommendation_id)" \
    || fail "chain[5/6] pursuit event not created"

# enrich the artifact with the PERSISTED pursuit metadata: the POST
# response echoes metadata as {} — the authoritative copy lives in
# events.corridor_events. Merge it so a cold auditor reads vehicle_id +
# recommendation_id + FIR in-band in pursuit_chain.json.
if [ -n "$PURSUIT_ID" ] && [ "$PURSUIT_ID" != "-" ]; then
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
        ok "pursuit metadata persisted and merged into the artifact: $(python3 -c 'import json;m=(json.load(open("'$CHAIN_JSON'"))["pursuit_event"] or {}).get("metadata") or {};print(sorted(m))' 2>/dev/null || echo '?')"
    else
        log "pursuit metadata query returned nothing — artifact keeps the POST echo"
    fi
fi
N_INTERCEPT_AVAIL=$(python3 -c 'import json;d=json.load(open("'$CHAIN_JSON'"));i=d.get("interception") or {};print("no" if i.get("unavailable") else "yes")' 2>/dev/null || echo no)
if [ "$N_INTERCEPT_AVAIL" = "yes" ] && [ "$N_INTERCEPT" -ge 1 ]; then
    ok "support: interception plan ranks $N_INTERCEPT choke junctions (top: junction $(python3 -c 'import json;d=json.load(open("'$CHAIN_JSON'"));t=d["interception"]["top"];print(t.get("junction_id"),t.get("junction_type"),"score",t.get("score"))' 2>/dev/null))"
else
    log "support: interception plan unavailable this seed — $(python3 -c 'import json;d=json.load(open("'$CHAIN_JSON'"));print((d.get("interception") or {}).get("detail","")[:120])' 2>/dev/null) (the emulated camera's latest read sits outside the 300 m attribution band; recorded, not asserted)"
fi
[ "$N_TRACE" -ge 2 ] && [ "$TRACE_SRC" = "emulated_anpr" ] \
    && ok "chain[6b] LIVE TRACE on the canvas layer: $N_TRACE timed positions (source $TRACE_SRC) — GET /api/v1/vehicles/{id}/trace feeds the vehicle-traces layer" \
    || fail "chain[6b] trace not usable by the canvas (points=$N_TRACE source=$TRACE_SRC)"

PURSUIT_TITLE=$(python3 -c 'import json;d=json.load(open("'$CHAIN_JSON'"));print((d.get("pursuit_event") or {}).get("title","PURSUIT"))' 2>/dev/null || echo "PURSUIT")

# ════════════════════════════════════════════════════════════════════════════
# 7. THE EVIDENCE — pursuit-trace + citizen-advisory canvas shots
# ════════════════════════════════════════════════════════════════════════════
log "phase 7: pursuit-trace + citizen-advisory canvas shots"
if [ -n "$CHROME_BIN" ] && [ -x "$CHROME_BIN" ]; then
    if ( cd "$VVIP_DIR/frontend" && NODE_PATH="$VVIP_DIR/frontend/node_modules" \
         node "$VVIP_DIR/scripts/showcase_canvas_shots.mjs" --rest-only \
              "$DECK_ORIGIN" "$SHOWCASE_DIR" "$PURSUIT_TITLE" ) \
         > "$SHOWCASE_DIR/shots_rest.log" 2>&1; then
        ok "canvas shots: showcase_pursuit_trace.png + showcase_citizen_advisory.png proven non-blank"
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
sleep 35   # let the flagship's Valkey inputs docs expire (TTL 30 s) so the
           # engine loop stops observing the subject — the easing below is
           # the only observation stream on it now

# the morning eases: the burst has passed and the chariots are garaged —
# the feed layer reports the decay (4 consecutive calmer observations
# cross the hysteresis down_confirm=4; a REAL de-escalation transition)
log "phase 8b: the morning eases — driving the band down through the observe API"
CALM_JSON="$SHOWCASE_DIR/band_easing.json"
ORACLE="$ORACLE_URL" MID="$FLAG_EVENT_ID" python3 - > "$CALM_JSON" <<'PYEOF'
import json, os, time, urllib.request
out = []
for i in range(5):
    body = {"movement_id": os.environ["MID"], "vvip_class": "none",
            "predicted_congestion_idx": 0.30, "weather_severity": 0.05,
            "weather": "CLEAR", "crowd_score": 0.30, "crowd_class": "HIGH",
            "duration_class": "hours", "event_active": False,
            "ambulance_delay_projection_sec": 10,
            "route_segment_ids": [1005, 1015, 1006, 1001]}
    req = urllib.request.Request(os.environ["ORACLE"] + "/api/v1/adaptive/observe",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    out.append({"n": i + 1, "effective_class": resp.get("effective_class"),
                "transitioned": resp.get("transitioned"),
                "previous_class": resp.get("previous_class")})
    if resp.get("transitioned"):
        break
    time.sleep(2.0)
print(json.dumps(out, indent=1))
PYEOF
CALM_TRANS=$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print("yes" if any(o.get("transitioned") for o in d) else "no")' "$CALM_JSON" 2>/dev/null || echo no)
[ "$CALM_TRANS" = "yes" ] \
    && ok "band eased as the morning closed: $(python3 -c 'import json;d=json.load(open("'$CALM_JSON'"));t=[o for o in d if o.get("transitioned")][0];print(t["previous_class"],"->",t["effective_class"])' 2>/dev/null) (hysteresis down_confirm=4 crossed)" \
    || log "note: easing observes did not cross the hysteresis confirm count yet (see $CALM_JSON) — the DB transition count below is the assertion"

n_situation=$($PSQL -c "SELECT count(*) FROM adaptive.situation_log WHERE movement_id='$FLAG_EVENT_ID'")
n_transitions=$($PSQL -c "SELECT count(*) FROM adaptive.situation_log WHERE movement_id='$FLAG_EVENT_ID' AND transitioned")
n_params=$($PSQL -c "SELECT count(*) FROM adaptive.param_overrides WHERE movement_id='$FLAG_EVENT_ID'")
n_amb=$($PSQL -c "SELECT count(*) FROM adaptive.param_overrides WHERE movement_id='$FLAG_EVENT_ID' AND evidence::text LIKE '%AMBULANCE_DELAY%'")
bands_csv=$($PSQL -c "SELECT situation_class||':'||count(*) FROM adaptive.situation_log WHERE movement_id='$FLAG_EVENT_ID' GROUP BY situation_class ORDER BY situation_class" | paste -sd,)
d_situation=$((n_situation - base_situation))
d_params=$((n_params - base_params))
d_trans=$((n_transitions - base_trans))
d_amb=$((n_amb - base_amb))
log "audit: situation_log rows $n_situation (delta $d_situation) bands[$bands_csv] transitions $n_transitions (delta $d_trans)"
log "audit: param_overrides rows $n_params (delta $d_params); AMBULANCE_DELAY escalations $n_amb (delta $d_amb)"

[ "$d_situation" -ge 1 ] && [ "$d_params" -ge 1 ] \
    && ok "audit log carries THIS run's decisions: $d_situation situation rows + $d_params parameter rows for the flagship event" \
    || fail "audit log missing this run's decisions (situation delta=$d_situation params delta=$d_params)"
[ "$d_trans" -ge 1 ] \
    && ok "situation band TRANSITIONED mid-morning: $d_trans new transitioned rows (bands this run: $bands_csv)" \
    || fail "no new band transition recorded (delta=$d_trans)"
[ "$d_amb" -ge 1 ] \
    && ok "ambulance escalation in the audit log: $d_amb new AMBULANCE_DELAY parameter decisions" \
    || fail "no AMBULANCE_DELAY escalation row this run (delta=$d_amb)"

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

# the crime-incident event from the sweep must exist (the sweep's corridor op;
# the sweep's movement/event uuid5 is over THIS run's seed)
SWEEP_EVENT_ID="$(python3 -c "import uuid; print(uuid.uuid5(uuid.uuid5(uuid.NAMESPACE_URL,'vvip-event-emulator'),'${SWEEP_ID}|${SWEEP_SEED}'))")"
sweep_kind=$($PSQL -c "SELECT event_kind FROM events.corridor_events WHERE event_id='$SWEEP_EVENT_ID'")
[ "$sweep_kind" = "crime_incident" ] \
    && ok "sweep corridor event present: crime_incident (the FIR-driven sweep's own operation)" \
    || fail "sweep crime_incident event not found (kind=$sweep_kind)"

# reversible state: nothing was committed, but restore is still exercised
curl -s -X POST "$ORACLE_URL/api/v1/signals/restore" -H 'Content-Type: application/json' \
    -d "{\"event_id\":\"$FLAG_EVENT_ID\"}" > "$SHOWCASE_DIR/signals_restore.json" 2>/dev/null || true

# ════════════════════════════════════════════════════════════════════════════
# 9. THE RECORD — showcase_results.json + exit code
# ════════════════════════════════════════════════════════════════════════════
NOW_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
FLAG_EVENT_ID="$FLAG_EVENT_ID" SWEEP_SEED="$SWEEP_SEED" SWEEP_RUN_ID="$SWEEP_RUN_ID" \
FLAG_RUN_ID="$FLAG_RUN_ID" FLAG_DELIVERY="$FLAG_DELIVERY" SWEEP_DELIVERED="$SWEEP_DELIVERED" \
VEHICLE_ID="$VEHICLE_ID" STOLEN_PLATE="$STOLEN_PLATE" FIR_REF="$FIR_REF" OFFICER="$OFFICER" \
N_SITUATION="$n_situation" D_SITUATION="$d_situation" N_PARAMS="$n_params" D_PARAMS="$d_params" \
N_TRANSITIONS="$n_transitions" D_TRANSITIONS="$d_trans" N_AMB="$n_amb" D_AMB="$d_amb" \
BANDS_CSV="$bands_csv" GC_WINDOWS="$GC_WINDOWS" EV_KIND="$ev_kind" EV_PROT="$ev_prot" \
N_PHASES="$n_phases" PHASE_POLICY="${phase_policy:-{\}}" SWEEP_KIND="$sweep_kind" \
SIGNAL_PLAN_SHIFTS="$SIGNAL_PLAN_SHIFTS" SIGNAL_REFUSALS="$SIGNAL_REFUSALS" SIGNAL_MAE="$SIGNAL_MAE" \
ACTIVE_FEED_SEEN="$ACTIVE_FEED_SEEN" JAN_RAKSHAK="$JAN_RAKSHAK_DISPATCHED" SPILL_JUNCTION="$SPILL_JUNCTION" \
CITY_SHOT_OK="$CITY_SHOT_OK" REST_SHOTS_OK="$REST_SHOTS_OK" SHOWCASE_DIR="$SHOWCASE_DIR" \
PURSUIT_TITLE="$PURSUIT_TITLE" NOW_UTC="$NOW_UTC" \
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

S = os.environ["SHOWCASE_DIR"]
earmark = load_file_json("chain_earmark.json", {})
chain = load_file_json("pursuit_chain.json", {})
jr = load_file_json("jan_rakshak.json", {})
sig = load_file_json("signal_policy.json", {})
twin = load_file_json("twin_audit.json", {})
observe = load_file_json("ambulance_observe.json", [])
easing = load_file_json("band_easing.json", [])
shots_city = load_file_json("shots_manifest.json", {})  # may be from --rest-only run
failed = [a for a in os.environ.get("FAILED_ASSERTS", "").split("\x1f") if a]

sight = chain.get("sightings") or {}
rec = chain.get("recommendation") or {}
app = chain.get("approval") or {}
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
    {"step": 3, "name": "echallan_recommendation",
     "detail": f"rules fired {rec.get('fired_rules')}; chosen: {(rec.get('chosen') or {}).get('text')}",
     "evidence": f"POST /api/v1/enforcement/evaluate x{sight.get('stolen_this_run')} -> "
                 f"recommendation {(rec.get('chosen') or {}).get('id')} PENDING"},
    {"step": 4, "name": "human_style_approval",
     "detail": f"{app.get('status')} by {app.get('officer')}",
     "evidence": "POST /api/v1/enforcement/recommendations/{id}/decision (audit tail append-only)"},
    {"step": 5, "name": "pursuit_event",
     "detail": f"{pur.get('title')} [{pur.get('status')}]",
     "evidence": f"POST /api/v1/events vehicle_pursuit {pur.get('event_id')} "
                 f"(metadata vehicle_id+recommendation_id)"},
    {"step": 6, "name": "interception_and_live_trace",
     "detail": (f"{inter.get('n_junctions')} choke junctions ranked (top "
                f"{((inter.get('top') or {}).get('junction_id'))}); trace {tr.get('points')} "
                f"positions source={tr.get('source')}"
                if not inter.get("unavailable") else
                f"interception plan unavailable ({(inter.get('detail') or '')[:90]}); "
                f"trace {tr.get('points')} positions source={tr.get('source')}"),
     "evidence": f"GET /api/v1/events/{pur.get('event_id')}/interception-plan + "
                 f"GET /api/v1/vehicles/{os.environ['VEHICLE_ID']}/trace (canvas vehicle-traces layer)"},
]

decisions_logged = int(os.environ["D_SITUATION"]) + int(os.environ["D_PARAMS"])

summary = {
  "flagship": {
    "name": "God's Eye Morning",
    "story": "Rath Yatra procession + monsoon burst + a stolen vehicle reported, "
             "one morning, one live stack — pursuit chain, Jan Rakshak spillover "
             "dispatch, ambulance green-corridor escalation, and the city "
             "signal-policy guard, end to end.",
    "composition_use_cases": [
        {"use_case_id": "rath_yatra_monsoon_ambulance", "run_id": int(os.environ["FLAG_RUN_ID"] or 0),
         "delivery": load_env_json("FLAG_DELIVERY", {}),
         "event_id": os.environ["FLAG_EVENT_ID"]},
        {"use_case_id": "watchlist_anpr_sweep", "seed": int(os.environ["SWEEP_SEED"]),
         "run_id": int(os.environ["SWEEP_RUN_ID"] or 0),
         "delivery": {"delivered": int(os.environ["SWEEP_DELIVERED"] or 0)}},
    ],
    "route": "Relief Road -> Nehru Bridge -> Riverfront West -> Ashram South (1005->1015->1006->1001)",
    "closure_policy": load_env_json("PHASE_POLICY", None),
  },
  "event_types": [
      "procession", "monsoon_burst", "market_spillover", "ambulance_green_corridor",
      "stolen_vehicle_report", "anpr_sweep", "crime_incident", "echallan_recommendation",
      "human_approval", "vehicle_pursuit", "interception_plan", "jan_rakshak_dispatch",
      "signal_band_plan_guard",
  ],
  "pursuit_chain": pursuit_chain,
  "counts": {
    "decisions_logged_audit_rows_this_run": decisions_logged,
    "situation_log_rows_this_run": int(os.environ["D_SITUATION"]),
    "param_override_rows_this_run": int(os.environ["D_PARAMS"]),
    "band_transitions_total": int(os.environ["N_TRANSITIONS"]),
    "band_transitions_this_run": int(os.environ["D_TRANSITIONS"]),
    "bands": os.environ["BANDS_CSV"],
    "green_corridor_windows": int(os.environ["GC_WINDOWS"]),
    "ambulance_delay_escalations_this_run": int(os.environ["D_AMB"]),
    "signal_plan_shifts_committed": int(os.environ["SIGNAL_PLAN_SHIFTS"]),
    "signal_policy_refusals": int(os.environ["SIGNAL_REFUSALS"]),
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
            f"(replay MAE {os.environ['SIGNAL_MAE']} km/h vs 5.0 gate; seeded observations not calibratable)",
  },
  "jan_rakshak": {
    "dispatched": os.environ["JAN_RAKSHAK"] == "true",
    "agency": jr.get("agency"), "agency_label": jr.get("agency_label"),
    "unit": jr.get("unit_id"), "waypoint_junction": int(os.environ["SPILL_JUNCTION"]),
    "spillover": "market_spillover behind Tilak Road barricades (segment 1013)",
    "statuses": jr.get("statuses"), "audit": jr.get("audit"),
    "projected_unit": jr.get("projected_unit"),
  },
  "ambulance_green_corridor": {
    "escalation_decisions": observe if isinstance(observe, list) else [],
    "audit_rows_this_run": int(os.environ["D_AMB"]),
    "twin_windows": twin.get("green_corridor_windows"),
    "window_labels": twin.get("window_labels"),
  },
  "band_easing": {
    "observes": easing if isinstance(easing, list) else [],
    "note": "the morning's decay reported through POST /api/v1/adaptive/observe "
            "(4 consecutive calmer observations cross the hysteresis down_confirm)",
  },
  "injection": {
    "active_movements_feed_reflected_event": os.environ["ACTIVE_FEED_SEEN"] == "true",
    "event_row": {"event_kind": os.environ["EV_KIND"],
                  "protection_class": os.environ["EV_PROT"],
                  "phases": int(os.environ["N_PHASES"] or 0)},
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
    echo "${BOLD}SHOWCASE PASS${RESET} — God's Eye Morning: procession+monsoon+stolen-vehicle chain, Jan Rakshak dispatch, ambulance escalation, signal-policy guard — all real, all audited"
    echo "      decisions(this run): $d_situation situation + $d_params param rows | transitions: $d_trans | green windows: $GC_WINDOWS | signal shifts committed: $SIGNAL_PLAN_SHIFTS (refusals: $SIGNAL_REFUSALS, MAE $SIGNAL_MAE) | shots: $(ls "$SHOWCASE_DIR"/showcase_*.png 2>/dev/null | xargs -n1 basename 2>/dev/null | paste -sd, -)"
    exit 0
fi
echo "${BOLD}SHOWCASE FAIL${RESET} — $FAILURES assertion(s):"
for a in "${FAILED_ASSERTS[@]}"; do echo "      - $a"; done
exit 1
