#!/usr/bin/env bash
# fleet_smoke.sh — prove the VVIP compose fleet is ONE working system.
#
# No arguments. Health-checks every brought-up service (HTTP or TCP as
# appropriate), then runs ONE real movement-plan call through the fleet's
# convoy-brain against the compose PostGIS and asserts a route came back.
# Prints exactly one line per service plus the route line; exits nonzero on
# any failure.
#
# Fleet layout this script encodes (infra/compose.yml +
# infra/compose.gb10-override.yml, project "vvip"):
#   postgis        127.0.0.1:55434   (host :5432 belongs to osint-db)
#   valkey         127.0.0.1:6379
#   nats           127.0.0.1:4222
#   traffic-oracle 127.0.0.1:8081    (host network)
#   convoy-brain   127.0.0.1:8080    (host network)
#   signal-ingress NATS publisher    (no listener; container-state check)
#   command-deck   127.0.0.1:5173    (OPTIONAL: checked only if present)
# Host model stack used by convoy-brain: decider :8083, llama :8084,
# embedding :8085 (loopback; checked transitively via convoy-brain /health).
#
# Security: every URL below is a fixed internal endpoint (a config constant
# of this script, http scheme, loopback host) — none comes from external
# input, so no host validation is required before these requests.
set -u

# ─── Fixed internal endpoints (config constants, not user-supplied) ────────
CONVOY_BRAIN_URL="http://127.0.0.1:8080"
TRAFFIC_ORACLE_URL="http://127.0.0.1:8081"
COMMAND_DECK_URL="http://127.0.0.1:5173"
POSTGIS_HOST="127.0.0.1"
POSTGIS_PORT="55434"
NATS_HOST="127.0.0.1"
NATS_PORT="4222"
PLAN_TIMEOUT_SEC=300

FAILURES=0
fail() { FAILURES=$((FAILURES + 1)); echo "FAIL $*"; }
ok()   { echo "OK   $*"; }

# ─── Helpers ───────────────────────────────────────────────────────────────
http_code() { # <url> [timeout]
    curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-10}" "$1"
}
tcp_open() { # <host> <port>
    (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && exec 3>&- 3<&- && return 0
    return 1
}
container_running() { # <name>
    [ "$(docker inspect "$1" --format '{{.State.Running}}' 2>/dev/null)" = "true" ]
}

# ─── 1. postgis — our Postgres answers on the compose DSN ─────────────────
if pg_isready_out=$(docker exec vvip-postgis pg_isready -U corridor_admin -d corridor_db 2>&1) \
   && tcp_open "$POSTGIS_HOST" "$POSTGIS_PORT"; then
    ok "postgis        tcp :${POSTGIS_PORT} + ${pg_isready_out}"
else
    fail "postgis        not answering on ${POSTGIS_HOST}:${POSTGIS_PORT} (${pg_isready_out:-no listener})"
fi

# ─── 2. valkey — PONG ──────────────────────────────────────────────────────
if pong=$(docker exec vvip-valkey valkey-cli ping 2>/dev/null) && [ "$pong" = "PONG" ]; then
    ok "valkey         ${pong}"
else
    fail "valkey         no PONG (got: ${pong:-none})"
fi

# ─── 3. nats — TCP connect ────────────────────────────────────────────────
if tcp_open "$NATS_HOST" "$NATS_PORT"; then
    ok "nats           tcp :${NATS_PORT}"
else
    fail "nats           no listener on ${NATS_HOST}:${NATS_PORT}"
fi

# ─── 4. traffic-oracle — HTTP /health ──────────────────────────────────────
code=$(http_code "$TRAFFIC_ORACLE_URL/health")
if [ "$code" = "200" ]; then
    ok "traffic-oracle http :8081/health 200"
else
    fail "traffic-oracle http :8081/health -> ${code:-no answer}"
fi

# ─── 5. convoy-brain — HTTP /health, both model tiers connected ───────────
cb_body=$(curl -s --max-time 15 "$CONVOY_BRAIN_URL/health" 2>/dev/null)
if [ -n "$cb_body" ] && printf '%s' "$cb_body" | python3 -c '
import json, sys
d = json.load(sys.stdin)
sys.exit(0 if d.get("status") == "ok" else 1)
' 2>/dev/null; then
    ok "convoy-brain   http :8080/health status=ok (decider+gemma connected)"
else
    fail "convoy-brain   http :8080/health not ok (body: ${cb_body:-no answer})"
fi

# ─── 6. signal-ingress — container up and not crash-looping ───────────────
if container_running vvip-signal-ingress; then
    restarts=$(docker inspect vvip-signal-ingress --format '{{.RestartCount}}' 2>/dev/null)
    ok "signal-ingress container running (restarts=${restarts:-0}; NATS publisher, no listener)"
else
    fail "signal-ingress container not running"
fi

# ─── 7. command-deck — OPTIONAL: checked only if deployed ─────────────────
# The interface-revamp deck (SvelteKit, 2026-10-13) REPLACES the old React
# dashboard: GET / IS the restored Convoy Command cockpit (asserted by
# tests/design-smoke.mjs: 200 + data-template="convoy-command"), with
# /ops the situation wall beside it — so the health contract here is a
# bare 200 on / AND on /ops, no route rewrite involved.
if docker inspect vvip-command-deck >/dev/null 2>&1; then
    code=$(http_code "$COMMAND_DECK_URL/")
    ops_code=$(http_code "$COMMAND_DECK_URL/ops")
    if [ "$code" = "200" ] && [ "$ops_code" = "200" ]; then
        ok "command-deck   http :5173 / 200 (cockpit) + /ops 200 (the SvelteKit situation deck)"
    else
        fail "command-deck   deployed but http :5173 -> ${code:-no answer} (want 200) / /ops -> ${ops_code:-no answer} (want 200)"
    fi
else
    echo "SKIP command-deck   optional service not deployed"
fi

# ─── 8. ONE real movement-plan call through the fleet ─────────────────────
# convoy-brain -> decider :8083 + gemma :8084 -> traffic-oracle :8081 ->
# PostGIS :55434 (pgRouting/OR-Tools on corridor.road_segments).
# Ahmedabad coordinates from services/corridor-store/seed/sample_corridor.sql
# (segment 1001 Ashram Road South -> segment 1002 Ashram Road North area).
DEPART=$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)
MOVEMENT_BODY='{"origin":[72.5714,23.0225],"destination":[72.5802,23.0607],"vvip_class":"Z","planned_departure":"'$DEPART'"}'

mov_resp=$(curl -s --max-time 30 -X POST "$CONVOY_BRAIN_URL/movements" \
    -H 'Content-Type: application/json' -d "$MOVEMENT_BODY")
movement_id=$(printf '%s' "$mov_resp" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("movement_id",""))' 2>/dev/null)

if [ -z "$movement_id" ]; then
    fail "movement-plan  POST /movements returned no movement_id (${mov_resp:-no answer})"
else
    plan_file=$(mktemp)
    plan_code=$(curl -s -o "$plan_file" -w '%{http_code}' --max-time "$PLAN_TIMEOUT_SEC" \
        -X POST "$CONVOY_BRAIN_URL/movements/$movement_id/plan" \
        -H 'Content-Type: application/json' -d "$MOVEMENT_BODY")
    route_summary=$(python3 - "$plan_file" <<'PY' 2>/dev/null
import json, sys
with open(sys.argv[1]) as fh:
    d = json.load(fh)
route = d.get("primary_route") or {}
rid = route.get("route_id")
segs = route.get("segment_ids") or []
if rid and segs:
    print(f"OK   route {rid} ({len(segs)} segments, status={d.get('status')})")
else:
    print(f"FAIL no route: primary_route={route or None} status={d.get('status')}")
PY
    )
    if [ "$plan_code" = "200" ] && [ "${route_summary#OK}" != "$route_summary" ]; then
        echo "$route_summary   [movement $movement_id via PostGIS :55434]"
    else
        fail "movement-plan  POST /movements/$movement_id/plan http=$plan_code $(cat "$plan_file" | head -c 200)"
    fi
    rm -f "$plan_file"
fi

# ─── Summary ───────────────────────────────────────────────────────────────
echo "────"
if [ "$FAILURES" -eq 0 ]; then
    echo "fleet-smoke: ALL PASS"
    exit 0
fi
echo "fleet-smoke: ${FAILURES} FAILURE(S)"
exit 1
