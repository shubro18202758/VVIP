#!/usr/bin/env bash
# argus_full_smoke.sh — FULL-SYSTEM live smoke for the argus stack (the live
# system: project `vvip-argus`, infra/compose.argus-override.yml).
#
# Proves the deployed tree is running LIVE end to end — every service, every
# shared host model, the operator-gated deck, the real-road network, the live
# data plane (publisher AND consumer), one REAL movement plan with its
# diversions and its corridor event, the city-picture reads, the MCP rail and
# the chat → canvas-directive path — while guarding the ORIGINAL (retired)
# stack (project `vvip`) as untouched:
#
#   PHASE 0 (before any docker action TAKEN BY THIS SCRIPT — note the
#   runbook order is build → up -d → this smoke, per the compose.argus-
#   override.yml usage block, so the redeploy window itself is guarded by
#   CONSTRUCTION, not observation: the argus override builds vvip-argus-*
#   images and resets the build stanzas of the services that reuse shared
#   images) snapshots the original stack read-only: every non-argus vvip-*
#   container (stable state fields — status/running/exit/restarts/StartedAt;
#   never Up-duration strings that drift with the clock) and every image tag
#   of the original fleet (vvip-* and the shared veer-vvip-* base, never the
#   -argus ones; orchestration scratch images — veer-sbx/*, veer-staging/*,
#   *-test/*, trial builds — are rebuilt by parallel work and are not the
#   original stack) with its image ID. It ALSO asserts the original fleet is
#   AVAILABLE (Running=true per container) — liveness, not just immutability.
#   PHASE 2 re-snapshots and diffs; ANY difference (including a StartedAt
#   move from a stop/start cycle) fails the gate with the exact diff.
#   When NO original container exists (the fleet was retired 2026-10-19)
#   the container guard prints SKIP instead of passing vacuously on zero
#   containers; the image-tag guard still runs while any original tag exists.
#
#   PHASE 0.5 waits (bounded, default 180s, env CONVERGE_TIMEOUT_SEC) for
#   the just-redeployed stack to CONVERGE before any component probe:
#   traffic-oracle /health 200, convoy-brain /health status ok WITH both
#   model tiers connected, deck / 200, and the emulator twin seam green
#   (it probes the argus oracle live on every /health call —
#   services/event-emulator/src/event_emulator/api.py health handler).
#   Rationale, from the round-1 gate run: `docker compose up -d` returns
#   when containers START, not when the apps inside bind — the recreate
#   window failed six oracle-dependent probes against a stack that was fully
#   green one minute later. The wait absorbs exactly that window and nothing
#   else: a stack that never converges burns the timeout and the probes
#   below then fail honestly — no check is weakened, skipped, or retried to
#   success; every probe still asserts exactly once on the converged stack.
#
# Components smoked (one line each; ports shown are the live defaults):
#   decider        :8083  POST /decide — typed choice+noul+score probe
#   gemma          :8084  POST /v1/chat/completions — agent-style call
#   embeddings     :8085  EMBED_PROBE=write (default): POST /embed/documents
#                         (ONE call runs BOTH encoders per doc) + /search;
#                         EMBED_PROBE=read: GET /health (both encoders
#                         loaded) + POST /search only — nothing written
#   emulator       :8090  GET /health judged on CONNECTIVITY (store, valkey,
#                         twin, nats): 'degraded' for historical conditions
#                         (a failed last run) is printed with its
#                         degraded_reasons, not failed
#   traffic-oracle :8091  GET /health + GET /api/v1/corridor/summary (shape,
#                         status-vs-threshold consistency, total_segments > 0)
#   road network   :8091  GET /api/v1/spatial/segments over the city —
#                         EXPECT_SEGMENTS (49) real-road segments with their
#                         geometry_basis counts and none 'schematic' (WP-20,
#                         migration 020)
#   traffic:latest :6380  the oracle's hot cache (TTL 300 s): the newest
#                         traffic:latest:{sid} observation is at most
#                         TRAFFIC_FRESH_MAX_SEC (60) seconds old
#   convoy-brain   :8092  GET /health — status ok AND decider+gemma
#                         connected (models wired)
#   argus images   — the wave services run their OWN -argus image tags,
#                         never the shared vvip-*:latest ones
#   postgis        :56434 pg_isready + TCP + migration ledger count ==
#                         tree migration files (services/corridor-store/
#                         migrations/*.sql)
#   valkey         :6380  PING → PONG
#   nats           :4223  protocol-level INFO preamble on connect
#   signal-ingress — WORKING proof, not just container-up: a bounded
#                         subscription to corridor.traffic.live (the Arrow
#                         IPC publisher's subject, publish/arrow_ipc.rs) must
#                         observe ≥1 message inside the window
#   oracle consumer — the oracle's ingest is connected, subscribed and
#                         CONSUMED live messages during that same window
#                         (its /health ingest counters rise, last message
#                         ≤15 s old), cross-checked server-side on the NATS
#                         monitor (NATS_MON_URL, :8223 /connz: the
#                         traffic-oracle-ingest client subscribed to
#                         corridor.traffic.live with out_msgs rising) — a
#                         publisher nobody consumes no longer passes on the
#                         smoke's own subscriber
#   command-deck   :5273  /, /ops, /gods-eye, /citizen, /driver, /comms,
#                         /chat all 200; the operator gate as expected
#                         (EXPECT_DECK_GATE, default on: an anonymous
#                         /api/v1 read is refused 403); ONE sign-in through
#                         scripts/deck_session.sh, then the /api/v1 proxy
#                         to the oracle answers 200
#   plan E2E       — POST /movements (title ARGUS SMOKE TEST, so the corridor
#                         event it registers wears the deck's TEST pill and
#                         never reads as a real movement) + POST
#                         /movements/{id}/plan, Lal Darwaja 72.5813,23.0264 →
#                         SVPI Airport 72.6221,23.072 (the re-anchored POIs of
#                         the real network), class Z; ≥1 route candidate
#   predict/eta    — POST /api/v1/predict/eta chained off the planned
#                         route (optimize/routes supplies the real
#                         length/segments): point payload always; p10/p50/
#                         p90 band fields exactly when the quantile stream
#                         is present (a fitted-but-degraded model must say
#                         so via eta_band_reason)
#   diversions     :8091  POST /api/v1/optimize/diversions over the planned
#                         primary route — a non-empty closure/detour plan
#   city picture   :8091  GET /api/v1/city/entities (kind counts, every feed
#                         'ok'), /cameras 200, /events/{id}/audit 200 for the
#                         corridor event THIS run's plan registered,
#                         /traffic/historical (provenance real|synthetic|
#                         none), /signals/plans (city_band_since)
#   mcp rail       :8092  POST /mcp tools/list — ≥ MCP_MIN_TOOLS (20) tools,
#                         none an actor (approve / decide / earmark /
#                         dispatch_request / withdraw / transition …: R2)
#   chat→directive :8092  POST /chat/stream 'show me the stolen vehicle on the
#                         map' — ≥1 tool_call, prose (never raw JSON) and,
#                         whenever the city picture holds an earmarked
#                         vehicle, a canvas directive targeting it (the AI
#                         proposes, a human applies); with none in the
#                         picture the directive part prints SKIP and any
#                         directive aimed at a vehicle fails (hallucination)
#   drift panel    — model_drift_panel.sh against the same endpoints
#                         (schema+anchors+latency bounds vs the recorded
#                         baseline); DRIFT_PANEL=0 prints SKIP
#
# Usage: bash scripts/argus_full_smoke.sh   (no arguments; the live stack)
# Env:   every endpoint/container is env-overridable with the live loopback
#        defaults preserved (the ${VAR:-default} pattern of showcase_run.sh
#        and hooks.server.ts); the sign-in reads the PIN at run time from
#        VEER_OPERATOR_PIN, else VEER_OPERATOR_PIN_FILE, else veer.env.
#        A private sandbox stack (sbx_stack.sh) runs the same script with:
#          ORACLE_PORT=81NN BRAIN_PORT=82NN EMULATOR_PORT=89NN DECK_PORT=53NN \
#          NATS_PORT=42NN POSTGIS_PORT=565NN POSTGIS_CONTAINER=veer-sbx-<s>-pg \
#          VALKEY_CONTAINER=veer-sbx-<s>-kv ORACLE_CONTAINER=veer-sbx-<s>-oracle \
#          BRAIN_CONTAINER=veer-sbx-<s>-brain INGRESS_CONTAINER=veer-sbx-<s>-ingress \
#          DECK_CONTAINER= NATS_MON_URL= EMBED_URL=http://127.0.0.1:8399 \
#          EMBED_PROBE=read DRIFT_PANEL=0 VEER_OPERATOR_PIN_FILE=<sbx>/pin
#        (empty DECK_CONTAINER: the deck is a node preview, not a container;
#        empty NATS_MON_URL: the sandbox NATS runs without monitoring — each
#        prints SKIP. EMBED_PROBE=read + DRIFT_PANEL=0: the embedding host and
#        its store are SHARED with the live stack, and a sandbox run must
#        never write marker docs into the live traffic.memory_docs; :8399 is
#        the read-only proxy that forwards GET /health and POST /search only.)
#
# NEGATIVE CONTROL (proves the data-plane legs can fail — run on the live
# stack only with the user's go-ahead: it darkens the live feed ~7 minutes):
#     docker stop vvip-argus-signal-ingress
#     sleep 360      # > the 300 s traffic:latest TTL: every live key expires
#     bash scripts/argus_full_smoke.sh; echo "exit=$?"     # MUST be non-zero
#     docker start vvip-argus-signal-ingress
#     sleep 60; bash scripts/argus_full_smoke.sh           # green again
#   Expected FAIL lines in the dark run: traffic:latest (no key, or the
#   newest older than 60 s), signal-ingress (container stopped, 0 messages
#   in the window) and oracle consumer (nothing consumed in the window,
#   last message ~6 min old, /connz out_msgs flat); everything else stays
#   green (the corridor summary still holds the last 10 min of rows).
#
# Security: every URL below is an internal endpoint (an env-overridable
# config constant of this script, http scheme, loopback host) — none comes
# from external input, so no host validation is required before these
# requests (the convoy-brain http_guard carve-out for platform services).
# All docker use is read-only (ps/inspect/images + exec'd SELECT/PING/SCAN/
# MGET/NATS-subscribe; every exec is `timeout`-bounded so a wedged
# container cannot hang the gate), and no shell command is ever built by
# string concatenation. The operator PIN is never echoed, logged or put on
# a command line (scripts/deck_session.sh sends it on stdin); the session
# is revoked on exit.
#
# WRITES, disclosed: (1) the model hosts :8083/:8084/:8085 are SHARED by
# design (compose.argus-override.yml header); with EMBED_PROBE=write the
# embeddings leg upserts 2 marker docs into the :8085 store (ref_ids
# argus-smoke:1/2 — stable across runs, so re-runs upsert rather than
# accumulate) and model_drift_panel.sh does the same with its own
# drift-panel:7f3a:a/b markers. (2) The plan leg creates one brain movement
# and the brain registers its plan as ONE corridor event in status
# 'planning' (labelled ARGUS SMOKE TEST — a TEST pill on the board; nothing
# is approved or activated). (3) The chat leg's turn is the brain's own
# read-only rail: no tool it can call writes city state (R2). The "original
# stack untouched" guarantee below covers the original fleet's CONTAINERS
# and IMAGE TAGS.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VVIP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── internal endpoints (env-overridable constants, loopback-only defaults;
#    the scripts/veer_env.sh port names — MODEL_HOST_PORT, EMBEDDING_PORT,
#    NATS_HOST_PORT, POSTGIS_HOST_PORT, VALKEY_HOST_PORT — are honoured too) ──
DECIDER_URL="${DECIDER_URL:-http://127.0.0.1:${DECIDER_PORT:-${MODEL_HOST_PORT:-8083}}}"
LLAMA_URL="${LLAMA_URL:-http://127.0.0.1:${LLAMA_PORT:-8084}}"
EMBED_URL="${EMBED_URL:-http://127.0.0.1:${EMBED_PORT:-${EMBEDDING_PORT:-8085}}}"
EMULATOR_URL="${EMULATOR_URL:-http://127.0.0.1:${EMULATOR_PORT:-8090}}"
ORACLE_URL="${ORACLE_URL:-http://127.0.0.1:${ORACLE_PORT:-8091}}"
BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:${BRAIN_PORT:-8092}}"
DECK_URL="${DECK_URL:-http://127.0.0.1:${DECK_PORT:-5273}}"
# NATS HTTP monitoring (compose publishes :8222 as loopback :8223). Explicitly
# EMPTY = not deployed (a sandbox NATS without -m): the server-side half of
# the consumer check prints SKIP; unset = the live default.
NATS_MON_URL="${NATS_MON_URL-http://127.0.0.1:${NATS_MON_PORT:-8223}}"

# ── fleet addressing (argus compose project; derived container names) ───────
COMPOSE_PROJECT="${COMPOSE_PROJECT:-vvip-argus}"
PG_CONTAINER="${POSTGIS_CONTAINER:-${COMPOSE_PROJECT}-postgis}"
VALKEY_CONTAINER="${VALKEY_CONTAINER:-${COMPOSE_PROJECT}-valkey}"
NATS_HOST="${NATS_HOST:-127.0.0.1}"
NATS_PORT="${NATS_PORT:-${NATS_HOST_PORT:-4223}}"
PG_HOST="${POSTGIS_HOST:-127.0.0.1}"
PG_PORT="${POSTGIS_PORT:-${POSTGIS_HOST_PORT:-56434}}"
PG_USER="${POSTGIS_USER:-corridor_admin}"
PG_DB="${POSTGIS_DB:-corridor_db}"
VALKEY_PORT="${VALKEY_PORT:-${VALKEY_HOST_PORT:-6380}}"   # label only (exec'd PING)
ORACLE_CONTAINER="${ORACLE_CONTAINER:-${COMPOSE_PROJECT}-traffic-oracle}"
BRAIN_CONTAINER="${BRAIN_CONTAINER:-${COMPOSE_PROJECT}-convoy-brain}"
# Explicitly EMPTY = the deck is not a container (a private `node build`
# preview): its image-ownership line prints SKIP.
DECK_CONTAINER="${DECK_CONTAINER-${COMPOSE_PROJECT}-command-deck}"
INGRESS_CONTAINER="${INGRESS_CONTAINER:-${COMPOSE_PROJECT}-signal-ingress}"
# The oracle ingest's NATS client name (ingest/nats_consumer.py NatsConnection).
ORACLE_NATS_CLIENT="${ORACLE_NATS_CLIENT:-traffic-oracle-ingest}"

# scheme guard: http/https only (config constants; rejects anything else early)
url_scheme_ok() { case "$1" in http://*|https://*) return 0 ;; *) return 1 ;; esac; }
for u in "$DECIDER_URL" "$LLAMA_URL" "$EMBED_URL" "$EMULATOR_URL" \
         "$ORACLE_URL" "$BRAIN_URL" "$DECK_URL" ${NATS_MON_URL:+"$NATS_MON_URL"}; do
    url_scheme_ok "$u" || { echo "FATAL: endpoint not http/https: $u" >&2; exit 2; }
done

PLAN_TIMEOUT_SEC="${PLAN_TIMEOUT_SEC:-300}"   # brain workflows are heavyweight
CHAT_TIMEOUT_SEC="${CHAT_TIMEOUT_SEC:-240}"   # one two-tier chat turn
MIGRATIONS_DIR="$VVIP_DIR/services/corridor-store/migrations"
EXPECT_SEGMENTS="${EXPECT_SEGMENTS:-49}"      # the real-road network (WP-20)
TRAFFIC_FRESH_MAX_SEC="${TRAFFIC_FRESH_MAX_SEC:-60}"
EXPECT_DECK_GATE="${EXPECT_DECK_GATE:-on}"    # on | off | any (user decision: on)
MCP_MIN_TOOLS="${MCP_MIN_TOOLS:-20}"
EMBED_PROBE="${EMBED_PROBE:-write}"           # write | read (see Env above)
DRIFT_PANEL="${DRIFT_PANEL:-1}"               # 0 = SKIP the drift panel leg
SMOKE_TITLE="ARGUS SMOKE TEST"                # the planned movement's label
SMOKE_OFFICER="Argus full smoke (automated check)"   # deck sign-in label (DECK_OFFICER is the helper's own state)
case "$EMBED_PROBE" in write|read) ;; *) echo "FATAL: EMBED_PROBE must be write or read" >&2; exit 2 ;; esac
case "$EXPECT_DECK_GATE" in on|off|any) ;; *) echo "FATAL: EXPECT_DECK_GATE must be on, off or any" >&2; exit 2 ;; esac

# shellcheck source=deck_session.sh
. "$SCRIPT_DIR/deck_session.sh"

FAILURES=0
SKIPS=0
fail() { FAILURES=$((FAILURES + 1)); echo "FAIL $*"; }
ok()   { echo "OK   $*"; }
skip() { SKIPS=$((SKIPS + 1)); echo "SKIP $*"; }
# verdict lines from a probe ("OK …" / "FAIL …" / "SKIP …") → this shell's counters
emit() {
    local line
    while IFS= read -r line; do
        case "$line" in
            OK\ *)   ok "${line#OK }" ;;
            SKIP\ *) skip "${line#SKIP }" ;;
            FAIL\ *) fail "${line#FAIL }" ;;
            '') ;;
            *)       fail "probe output unreadable: $line" ;;
        esac
    done
}

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-15}" "$1"; }
tcp_open() { (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && exec 3>&- 3<&- && return 0; return 1; }
port_of() {  # <url> → ":port" for the line labels (the endpoint actually probed)
    local hp="${1#*://}"; hp="${hp%%/*}"
    case "$hp" in *:*) printf ':%s' "${hp##*:}" ;; *) printf '%s' "$hp" ;; esac
}
P_DECIDER=$(port_of "$DECIDER_URL"); P_LLAMA=$(port_of "$LLAMA_URL"); P_EMBED=$(port_of "$EMBED_URL")
P_EMU=$(port_of "$EMULATOR_URL"); P_ORACLE=$(port_of "$ORACLE_URL"); P_BRAIN=$(port_of "$BRAIN_URL")
P_DECK=$(port_of "$DECK_URL")

# ════════════════════════════════════════════════════════════════════════════
# PHASE 0 — snapshot the ORIGINAL stack read-only, before anything else
# ════════════════════════════════════════════════════════════════════════════
SNAP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/argus-full-smoke.XXXXXX")"
trap 'deck_signout; rm -rf "$SNAP_DIR"' EXIT

# Stable-state container snapshot (Name|Image|Status|Running|ExitCode|
# RestartCount|StartedAt — deliberately NO "Up N minutes" strings, which
# change with the wall clock and would false-fail an untouched original;
# StartedAt is included so a stop/start cycle INSIDE the guarded window is
# caught even though RestartCount only counts policy restarts).
snapshot_original_containers() {
    docker ps -a --filter 'name=vvip-' --format '{{.Names}}' 2>/dev/null \
        | grep -v '^vvip-argus-' \
        | while read -r name; do
              docker inspect "$name" --format \
                '{{.Name}}|{{.Config.Image}}|{{.State.Status}}|{{.State.Running}}|{{.State.ExitCode}}|{{.RestartCount}}|{{.State.StartedAt}}' \
                2>/dev/null
          done | sort
}

# Image-tag snapshot: the original fleet's tags — vvip-* and the shared
# veer-vvip-* base — WITHOUT the -argus suffix, with their image IDs (a
# retag/rebuild changes the ID even for identical content, which is exactly
# what must not happen to the original stack). Orchestration scratch images
# (veer-sbx/*, veer-staging/*, *-test/*, trial builds) are not part of it.
snapshot_original_images() {
    docker images --format '{{.Repository}}:{{.Tag}}|{{.ID}}' 2>/dev/null \
        | grep -E '^(vvip-|veer-vvip-)' | grep -v -- '-argus' | sort
}

snapshot_original_containers > "$SNAP_DIR/containers.before"
snapshot_original_images     > "$SNAP_DIR/images.before"
n_guard_containers=$(wc -l < "$SNAP_DIR/containers.before")
n_guard_images=$(wc -l < "$SNAP_DIR/images.before")
echo "──── argus full smoke: guarding the original stack ($n_guard_containers container(s), $n_guard_images shared image tag(s))"

# ── availability: the guarded fleet must be ALIVE, not merely unchanged ────
# Immutability alone would bless a fleet killed by a host reboot; the pass
# bar is "original containers still up", so every guarded container must
# report Running=true here and now. Zero containers is not a pass: SKIP.
if [ "$n_guard_containers" -eq 0 ]; then
    skip "original fleet: no original (non-argus) vvip-* container exists (the vvip fleet was retired) — availability not asserted"
else
    guard_down=""
    while IFS='|' read -r g_name _g_img _g_status g_running _g_exit _g_rc _g_start; do
        [ "$g_running" = "true" ] || guard_down="$guard_down ${g_name#/}"
    done < "$SNAP_DIR/containers.before"
    if [ -n "$guard_down" ]; then
        fail "original fleet DOWN (not Running):$guard_down — restore the original stack (its own project, NOT part of the argus redeploy) and re-run; immutability alone is not a pass"
    else
        ok "original fleet AVAILABLE ($n_guard_containers container(s) Running=true)"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# PHASE 0.5 — bounded converge wait for the just-redeployed stack
# (see header: absorbs the `up -d` recreate/boot window ONLY; a stack that
#  never converges still fails the probes below, honestly)
# ════════════════════════════════════════════════════════════════════════════
CONVERGE_TIMEOUT_SEC="${CONVERGE_TIMEOUT_SEC:-180}"
stack_converged() {
    local o b t d
    o=$(http_code "$ORACLE_URL/health" 5)
    [ "$o" = "200" ] || return 1
    b=$(curl -s --max-time 5 "$BRAIN_URL/health" 2>/dev/null)
    printf '%s' "$b" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
ok = (d.get("status") == "ok"
      and d.get("strategic_tier", {}).get("model_host") == "connected"
      and d.get("tactical_tier", {}).get("llama_server") == "connected")
sys.exit(0 if ok else 1)
' 2>/dev/null || return 1
    d=$(http_code "$DECK_URL/" 5)
    [ "$d" = "200" ] || return 1
    t=$(curl -s --max-time 5 "$EMULATOR_URL/health" 2>/dev/null)
    printf '%s' "$t" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("stack", {}).get("twin") else 1)
' 2>/dev/null || return 1
    return 0
}

if stack_converged; then
    echo "──── stack converged (redeploy settled before probes)"
else
    echo "──── waiting for the redeployed stack to converge (≤${CONVERGE_TIMEOUT_SEC}s)…"
    converge_deadline=$(( $(date +%s) + CONVERGE_TIMEOUT_SEC ))
    until stack_converged; do
        if [ "$(date +%s)" -ge "$converge_deadline" ]; then
            echo "──── stack did NOT converge within ${CONVERGE_TIMEOUT_SEC}s — probes below will report the real state"
            break
        fi
        sleep 2
    done
fi

# ════════════════════════════════════════════════════════════════════════════
# 1. decider :8083 — one REAL typed call (choice + noul + score)
# ════════════════════════════════════════════════════════════════════════════
decider_body="$(curl -sf --max-time 300 -X POST "$DECIDER_URL/decide" \
    -H 'Content-Type: application/json' \
    -d '{
        "request_id": "argus-full-smoke",
        "state": "Argus full-system smoke: Z-class movement planned from Lal Darwaja (old city) to SVPI Airport across Ahmedabad junctions at 19:30 IST. Evening congestion rising on Ashram Road; Ring Road flowing. Advance closure 180s per protocol.",
        "questions": {
            "reroute":   {"type": "choice",
                          "instructions": "Which corridor should the route prefer?",
                          "options": {"ashram_road": "Ashram Road, congested",
                                      "ring_road": "Ring Road, light flow",
                                      "sg_highway": "SG Highway, moderate"}},
            "urgency":   {"type": "noul",
                          "instructions": "Must the departure window be locked now?"},
            "readiness": {"type": "score",
                          "instructions": "Rate readiness to start",
                          "scale": ["not ready", "slightly ready", "moderately ready", "mostly ready", "fully ready"]}
        }
    }' 2>/dev/null)"
decider_note=$(printf '%s' "$decider_body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
a = d.get("answers", {})
if not set(a) >= {"urgency", "reroute", "readiness"}:
    sys.exit(1)
for name, ans in a.items():
    if ans.get("value") in (None, ""):
        sys.exit(1)
    p = ans.get("probability")
    if not (isinstance(p, (int, float)) and 0.0 <= p <= 1.0):
        sys.exit(1)
    if not ans.get("distribution"):
        sys.exit(1)
print("reroute=%s (p=%.3f)" % (a["reroute"]["value"], a["reroute"]["probability"]))
' 2>/dev/null)
if [ -n "$decider_note" ]; then
    ok "decider        $P_DECIDER typed choice+noul+score answered ($decider_note)"
else
    fail "decider        $P_DECIDER /decide typed call failed (body: ${decider_body:-no answer})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 2. gemma :8084 — one REAL agent-style chat completion
# ════════════════════════════════════════════════════════════════════════════
gemma_body="$(curl -sf --max-time 240 -X POST "$LLAMA_URL/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d '{"model":"aditi-ps18-v7h-12b","max_tokens":80,"temperature":0.4,
         "messages":[{"role":"system","content":"You are the VVIP convoy tactical agent. Answer in one sentence."},
                     {"role":"user","content":"A Z-class convoy must cross a congested old-city corridor to the airport. What is the immediate tactical priority?"}]}' \
    2>/dev/null)"
gemma_note=$(printf '%s' "$gemma_body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    content = d["choices"][0]["message"]["content"]
except Exception:
    sys.exit(1)
if not (isinstance(content, str) and len(content.strip()) >= 20):
    sys.exit(1)
print("%d chars" % len(content))
' 2>/dev/null)
if [ -n "$gemma_note" ]; then
    ok "gemma          $P_LLAMA chat completion answered ($gemma_note)"
else
    fail "gemma          $P_LLAMA chat completion failed (body: ${gemma_body:-no answer})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 3. embeddings :8085 — write: BOTH encoders (one /embed/documents call runs
#    the late multivector AND the dense encoder per document), then /search;
#    read: /health names both encoders, /search (the query runs through them)
# ════════════════════════════════════════════════════════════════════════════
if [ "$EMBED_PROBE" = write ]; then
    embed_body="$(curl -sf --max-time 120 -X POST "$EMBED_URL/embed/documents" \
        -H 'Content-Type: application/json' \
        -d '{"documents": [{"body": "Argus full smoke: Z-class convoy staged at Lal Darwaja; corridor to SVPI Airport nominal.",
                            "title": "argus-smoke-a", "ref_id": "argus-smoke:1"},
                           {"body": "Argus full smoke: congestion building on the old-city approach; recovery projected 200s.",
                            "title": "argus-smoke-b", "ref_id": "argus-smoke:2"}],
             "kind": "pattern"}' 2>/dev/null)"
    embed_note=$(printf '%s' "$embed_body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
n, ids = d.get("embedded"), d.get("doc_ids")
if not (isinstance(n, int) and n >= 2):
    sys.exit(1)
if not (isinstance(ids, list) and len(ids) == n and all(ids)):
    sys.exit(1)
print("%d docs encoded by both encoders" % n)
' 2>/dev/null)
else
    embed_body="$(curl -sf --max-time 30 "$EMBED_URL/health" 2>/dev/null)"
    embed_note=$(printf '%s' "$embed_body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if d.get("status") != "ok" or not d.get("late_encoder") or not d.get("dense_encoder"):
    sys.exit(1)
print("read-only probe: %s + %s loaded (EMBED_PROBE=read — no marker upsert)" % (d["late_encoder"], d["dense_encoder"]))
' 2>/dev/null)
fi
search_body="$(curl -sf --max-time 60 -X POST "$EMBED_URL/search" \
    -H 'Content-Type: application/json' \
    -d '{"query": "argus full smoke corridor congestion", "kinds": ["pattern"]}' 2>/dev/null)"
search_note=$(printf '%s' "$search_body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    hits = d.get("results")
except Exception:
    sys.exit(1)
if not isinstance(hits, list):
    sys.exit(1)
markers = sorted({h.get("ref_id") for h in hits if str(h.get("ref_id", "")).startswith("argus-smoke:")})
print("%d hits (markers recalled: %s)" % (len(hits), ",".join(markers) or "none"))
' 2>/dev/null)
if [ -n "$embed_note" ] && [ -n "$search_note" ]; then
    ok "embeddings     $P_EMBED $embed_note; search $search_note"
else
    fail "embeddings     $P_EMBED encode/health/search failed (EMBED_PROBE=$EMBED_PROBE; first: ${embed_body:-none}; search: ${search_body:-none})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 4. event emulator :8090 — judged on CONNECTIVITY (store, valkey, twin,
#    nats). /health also says 'degraded' for historical conditions (a failed
#    last run) that are not a wiring fault: printed, never hidden, not failed.
# ════════════════════════════════════════════════════════════════════════════
emu_body="$(curl -s --max-time 10 "$EMULATOR_URL/health" 2>/dev/null)"
emu_note=$(printf '%s' "$emu_body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
s = d.get("stack", {})
if not (s.get("store") and s.get("valkey") and s.get("twin") and s.get("nats")):
    sys.exit(1)
note = "store+valkey+twin+nats connected, library %s, status %s" % (d.get("library", "?"), d.get("status"))
reasons = [str(r) for r in (d.get("degraded_reasons") or [])]
if reasons:
    note += "; degraded_reasons: " + "; ".join(reasons)[:300]
print(note)
' 2>/dev/null)
if [ -n "$emu_note" ]; then
    ok "emulator       $P_EMU /health ($emu_note)"
else
    fail "emulator       $P_EMU not connected (body: ${emu_body:-no answer})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 5. traffic-oracle :8091 — health + corridor summary contract (non-vacuous:
#    a summary over zero segments no longer passes)
# ════════════════════════════════════════════════════════════════════════════
oracle_health="$(curl -s --max-time 10 "$ORACLE_URL/health" 2>/dev/null)"
if printf '%s' "$oracle_health" | grep -q '"status": *"ok"'; then
    ok "traffic-oracle $P_ORACLE /health ok (image $(docker inspect "$ORACLE_CONTAINER" --format '{{.Config.Image}}' 2>/dev/null))"
else
    fail "traffic-oracle $P_ORACLE /health not ok (body: ${oracle_health:-no answer})"
fi

summary_body="$(curl -s --max-time 15 "$ORACLE_URL/api/v1/corridor/summary" 2>/dev/null)"
summary_note=$(printf '%s' "$summary_body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for k in ("total_segments", "avg_speed_kmh", "avg_congestion_idx",
          "congested_segments", "critical_segments", "status"):
    if k not in d:
        sys.exit(1)
if not (isinstance(d["total_segments"], int) and d["total_segments"] > 0):
    sys.exit(1)
if not (isinstance(d["avg_speed_kmh"], (int, float)) and 0.0 <= d["avg_speed_kmh"] <= 200.0):
    sys.exit(1)
if not (isinstance(d["avg_congestion_idx"], (int, float)) and 0.0 <= d["avg_congestion_idx"] <= 1.0):
    sys.exit(1)
if d["status"] not in ("green", "amber", "red"):
    sys.exit(1)
# the heuristic must agree with its own inputs (drift-panel anchor)
cong = float(d["avg_congestion_idx"])
expected = "red" if cong > 0.7 else ("amber" if cong > 0.4 else "green")
if d["status"] != expected:
    sys.exit(1)
print("status %s (congestion %.3f, %d segments observed; provenance %s, data age %ss) — consistent with thresholds"
      % (d["status"], cong, d["total_segments"], d.get("provenance", "?"), d.get("data_age_sec", "?")))
' 2>/dev/null)
if [ -n "$summary_note" ]; then
    ok "corridor       $P_ORACLE summary contract holds ($summary_note)"
else
    fail "corridor       $P_ORACLE summary empty/malformed/inconsistent (body: ${summary_body:-no answer})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 6. road network :8091 — the REAL-ROAD network (WP-20, migration 020): the
#    segment count and how every line is drawn (spatial/router.py C-K)
# ════════════════════════════════════════════════════════════════════════════
net_km=$(timeout 15 docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -At \
    -c "SELECT round((sum(ST_Length(geom::geography)) / 1000)::numeric, 2) FROM corridor.road_segments" 2>/dev/null)
net_body="$(curl -s --max-time 15 "$ORACLE_URL/api/v1/spatial/segments?min_lon=72.3&min_lat=22.8&max_lon=72.9&max_lat=23.3" 2>/dev/null)"
net_line=$(printf '%s' "$net_body" | EXPECT="$EXPECT_SEGMENTS" KM="${net_km:-?}" python3 -c '
import collections, json, os, sys
try:
    rows = json.load(sys.stdin)["segments"]
except Exception:
    print("FAIL unreadable /api/v1/spatial/segments answer")
    sys.exit(0)
basis = collections.Counter(str(r.get("geometry_basis")) for r in rows)
counts = ", ".join("%s=%d" % kv for kv in sorted(basis.items()))
want = int(os.environ["EXPECT"])
if len(rows) == want and basis.get("schematic", 0) == 0:
    print("OK %d segments (%s km routing length), geometry_basis %s — no schematic line" % (len(rows), os.environ["KM"], counts))
else:
    print("FAIL %d segments (want %d), geometry_basis %s" % (len(rows), want, counts))
' 2>/dev/null)
case "$net_line" in
    OK\ *)   ok "road network   $P_ORACLE ${net_line#OK }" ;;
    FAIL\ *) fail "road network   $P_ORACLE ${net_line#FAIL }" ;;
    *)       fail "road network   $P_ORACLE no answer (body: ${net_body:0:200})" ;;
esac

# ════════════════════════════════════════════════════════════════════════════
# 7. traffic:latest — the oracle's hot cache is FRESH: the newest
#    traffic:latest:{sid} observation is ≤ TRAFFIC_FRESH_MAX_SEC old (keys
#    live 300 s, so a dead feed empties the cache within 5 minutes)
# ════════════════════════════════════════════════════════════════════════════
mapfile -t latest_keys < <(timeout 10 docker exec "$VALKEY_CONTAINER" valkey-cli --scan --pattern 'traffic:latest:*' 2>/dev/null)
if [ "${#latest_keys[@]}" -eq 0 ]; then
    fail "traffic:latest :$VALKEY_PORT no traffic:latest:* key in $VALKEY_CONTAINER (the hot cache is empty — no live row in 300 s)"
else
    fresh_line=$(timeout 10 docker exec "$VALKEY_CONTAINER" valkey-cli MGET "${latest_keys[@]}" 2>/dev/null \
        | MAX_AGE="$TRAFFIC_FRESH_MAX_SEC" python3 -c '
import json, os, sys, time
max_age = float(os.environ["MAX_AGE"])
now_ms = time.time() * 1000.0
stamps = []
for line in sys.stdin:
    try:
        stamps.append(int(json.loads(line)["timestamp_ms"]))
    except Exception:
        pass  # a key that expired between SCAN and MGET
if not stamps:
    print("FAIL no readable traffic:latest row")
    sys.exit(0)
age = max((now_ms - max(stamps)) / 1000.0, 0.0)
fresh = sum(1 for t in stamps if (now_ms - t) / 1000.0 <= max_age)
verdict = "OK" if age <= max_age else "FAIL"
print("%s %d key(s); newest observation %.1f s old (limit %d s); %d within the limit"
      % (verdict, len(stamps), age, max_age, fresh))
' 2>/dev/null)
    case "$fresh_line" in
        OK\ *)   ok "traffic:latest :$VALKEY_PORT ${fresh_line#OK }" ;;
        FAIL\ *) fail "traffic:latest :$VALKEY_PORT ${fresh_line#FAIL }" ;;
        *)       fail "traffic:latest :$VALKEY_PORT unreadable MGET answer from $VALKEY_CONTAINER" ;;
    esac
fi

# ════════════════════════════════════════════════════════════════════════════
# 8. argus convoy-brain :8092 — status ok AND both model tiers connected
# ════════════════════════════════════════════════════════════════════════════
brain_body="$(curl -s --max-time 20 "$BRAIN_URL/health" 2>/dev/null)"
brain_note=$(printf '%s' "$brain_body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if d.get("status") != "ok":
    sys.exit(1)
st = d.get("strategic_tier", {})
tt = d.get("tactical_tier", {})
if st.get("model_host") != "connected" or tt.get("llama_server") != "connected":
    sys.exit(1)
print("novelty_latch=%s" % st.get("novelty_latch", "n/a"))
' 2>/dev/null)
if [ -n "$brain_note" ]; then
    ok "convoy-brain   $P_BRAIN /health ok, decider+gemma connected ($brain_note; image $(docker inspect "$BRAIN_CONTAINER" --format '{{.Config.Image}}' 2>/dev/null))"
else
    fail "convoy-brain   $P_BRAIN not healthy/models disconnected (body: ${brain_body:-no answer})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 9. argus image ownership — the wave services run their OWN -argus tags
# ════════════════════════════════════════════════════════════════════════════
images_ok=true
images_checked=""
for pair in "$ORACLE_CONTAINER|vvip-argus-traffic-oracle" \
            "$BRAIN_CONTAINER|vvip-argus-convoy-brain" \
            "$DECK_CONTAINER|vvip-argus-command-deck"; do
    ctr="${pair%%|*}"; want="${pair##*|}:latest"
    if [ -z "$ctr" ]; then
        skip "argus-images   ${want%%:*}: no container to inspect (DECK_CONTAINER empty — the deck runs as a node preview)"
        continue
    fi
    running="$(docker inspect "$ctr" --format '{{.State.Running}}' 2>/dev/null)"
    img="$(docker inspect "$ctr" --format '{{.Config.Image}}' 2>/dev/null)"
    if [ "$running" = "true" ] && [ "$img" = "$want" ]; then
        images_checked="$images_checked $ctr"
    else
        images_ok=false
        fail "argus-images   $ctr running=$running image=$img (want $want)"
    fi
done
if [ "$images_ok" = true ] && [ -n "$images_checked" ]; then
    ok "argus-images   wave services on their own tags (${images_checked# } → vvip-argus-*)"
fi

# ════════════════════════════════════════════════════════════════════════════
# 10. postgis :56434 — ready + migration ledger complete vs the tree
# ════════════════════════════════════════════════════════════════════════════
pg_ready=$(timeout 15 docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" 2>&1)
ledger_n=$(timeout 15 docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -At \
    -c "SELECT count(*) FROM corridor.schema_migrations" 2>/dev/null)
tree_n=$(ls "$MIGRATIONS_DIR"/[0-9]*.sql 2>/dev/null | wc -l)
if [ -n "$pg_ready" ] && [ "${pg_ready#*"accepting connections"}" != "$pg_ready" ] \
   && tcp_open "$PG_HOST" "$PG_PORT" \
   && [ -n "$ledger_n" ] && [ -n "$tree_n" ] && [ "$ledger_n" = "$tree_n" ] && [ "$ledger_n" -gt 0 ]; then
    ok "postgis        :$PG_PORT accepting connections; migrations $ledger_n/$tree_n applied (ledger == tree)"
else
    fail "postgis        :$PG_PORT not ready or ledger mismatch (pg_isready: ${pg_ready:-none}; ledger=${ledger_n:-?} tree=${tree_n:-?})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 11. valkey :6380 — PONG
# ════════════════════════════════════════════════════════════════════════════
pong=$(timeout 10 docker exec "$VALKEY_CONTAINER" valkey-cli ping 2>/dev/null)
if [ "$pong" = "PONG" ]; then
    ok "valkey         :$VALKEY_PORT PONG"
else
    fail "valkey         :$VALKEY_PORT no PONG (got: ${pong:-none})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 12. nats :4223 — protocol-level INFO preamble (pure socket read; the
#     server greets every TCP client with `INFO {...}` before auth)
# ════════════════════════════════════════════════════════════════════════════
nats_preamble=$(python3 - "$NATS_HOST" "$NATS_PORT" <<'PY' 2>/dev/null
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    with socket.create_connection((host, port), timeout=5) as sock:
        head = sock.recv(16)
except OSError:
    sys.exit(0)
if head.startswith(b"INFO "):
    print("INFO ")
PY
)
if [ "$nats_preamble" = "INFO " ]; then
    ok "nats           :$NATS_PORT protocol INFO handshake"
else
    fail "nats           :$NATS_PORT no INFO preamble (got: ${nats_preamble:-none})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 13. signal-ingress + oracle consumer — WORKING proof of BOTH ends of the
#     live data plane. Publisher: container running AND a bounded
#     subscription (from inside the oracle container — its image ships
#     nats-py) observes corridor.traffic.live messages (src/publish/
#     arrow_ipc.rs SUBJECT_LIVE, ~1 batch/s). Consumer: across the SAME
#     window the oracle's ingest (ingest/service.py) reports itself
#     connected + subscribed with its live-subject message counter rising
#     and its last message fresh, and the NATS monitor independently shows
#     the traffic-oracle-ingest client subscribed with out_msgs rising.
# ════════════════════════════════════════════════════════════════════════════
consumer_probe() {  # → one JSON line: the oracle's ingest counters + /connz's view of its client
    ORACLE="$ORACLE_URL" MON="$NATS_MON_URL" CLIENT="$ORACLE_NATS_CLIENT" python3 - <<'PY' 2>/dev/null
import json, os, urllib.request
out = {}
try:
    with urllib.request.urlopen(os.environ["ORACLE"] + "/health", timeout=10) as r:
        ing = json.loads(r.read()).get("ingest") or {}
    live = (ing.get("subjects") or {}).get("corridor.traffic.live") or {}
    out.update(connected=bool(ing.get("connected")), subscribed=bool(ing.get("subscribed")),
               messages=live.get("messages"), age=live.get("last_msg_age_sec"))
except Exception as exc:  # noqa: BLE001 — reported, judged below
    out["oracle_error"] = type(exc).__name__
if os.environ.get("MON"):
    try:
        with urllib.request.urlopen(os.environ["MON"] + "/connz?subs=1&limit=1024", timeout=10) as r:
            conns = json.loads(r.read()).get("connections") or []
        mine = [c for c in conns if c.get("name") == os.environ["CLIENT"]
                and "corridor.traffic.live" in (c.get("subscriptions_list") or [])]
        out["nats_clients"] = len(mine)
        out["out_msgs"] = sum(int(c.get("out_msgs") or 0) for c in mine)
    except Exception as exc:  # noqa: BLE001
        out["nats_error"] = type(exc).__name__
print(json.dumps(out))
PY
}
consumer_before=$(consumer_probe)
ingress_msgs=$(timeout 45 docker exec -i "$ORACLE_CONTAINER" python - "$NATS_HOST" "$NATS_PORT" <<'PY' 2>/dev/null
import asyncio, sys, nats

host, port = sys.argv[1], int(sys.argv[2])

async def main():
    nc = await nats.connect("nats://%s:%d" % (host, port), connect_timeout=5)
    count = 0

    async def handler(msg):
        nonlocal count
        count += 1

    sub = await nc.subscribe("corridor.traffic.live", cb=handler)
    await asyncio.sleep(15)
    await sub.unsubscribe()
    await nc.drain()
    print(count)

try:
    asyncio.run(asyncio.wait_for(main(), timeout=40))
except Exception:
    print(0)
PY
)
consumer_after=$(consumer_probe)
ingress_msgs="${ingress_msgs:-0}"
if [ "$(docker inspect "$INGRESS_CONTAINER" --format '{{.State.Running}}' 2>/dev/null)" = "true" ] \
   && [ "$ingress_msgs" -ge 1 ] 2>/dev/null; then
    restarts=$(docker inspect "$INGRESS_CONTAINER" --format '{{.RestartCount}}' 2>/dev/null)
    ok "signal-ingress WORKING: $ingress_msgs message(s) on corridor.traffic.live in 15s (container running, restarts=${restarts:-0})"
else
    fail "signal-ingress not publishing (container running=$(docker inspect "$INGRESS_CONTAINER" --format '{{.State.Running}}' 2>/dev/null); messages in 15s window: $ingress_msgs)"
fi
emit < <(BEFORE="$consumer_before" AFTER="$consumer_after" MON="$NATS_MON_URL" \
         CLIENT="$ORACLE_NATS_CLIENT" PORT="$P_ORACLE" python3 - <<'PY' 2>/dev/null || echo "FAIL oracle-consumer probe crashed"
import json, os
lbl = "oracle-consumer %s" % os.environ["PORT"]
try:
    b, a = json.loads(os.environ["BEFORE"] or "{}"), json.loads(os.environ["AFTER"] or "{}")
except ValueError:
    b, a = {}, {}
if a.get("messages") is None or b.get("messages") is None:
    print("FAIL %s /health ingest unreadable before or after the window (%s)"
          % (lbl, a.get("oracle_error") or b.get("oracle_error") or "no ingest block"))
else:
    consumed = int(a["messages"]) - int(b["messages"])
    age = a.get("age")
    ok = a.get("connected") and a.get("subscribed") and consumed >= 1 and age is not None and float(age) <= 15.0
    print("%s %s ingest connected=%s subscribed=%s; consumed %d live message(s) during the window (last %ss ago)"
          % ("OK" if ok else "FAIL", lbl, a.get("connected"), a.get("subscribed"), consumed, age))
mon = os.environ.get("MON")
if not mon:
    print("SKIP oracle-consumer NATS monitor not configured (NATS_MON_URL empty) — the server-side /connz cross-check did not run")
elif "nats_error" in a or "nats_clients" not in a:
    print("FAIL oracle-consumer NATS monitor %s/connz unreadable (%s)" % (mon, a.get("nats_error", "?")))
else:
    delivered = int(a.get("out_msgs") or 0) - int(b.get("out_msgs") or 0)
    ok = a["nats_clients"] >= 1 and delivered >= 1
    print("%s oracle-consumer NATS /connz: %d '%s' client(s) subscribed to corridor.traffic.live, %d message(s) delivered to it in the window"
          % ("OK" if ok else "FAIL", a["nats_clients"], os.environ["CLIENT"], delivered))
PY
)

# ════════════════════════════════════════════════════════════════════════════
# 14. command-deck :5273 — every route, the operator gate, and the /api/v1
#     proxy to the stack (signed in ONCE through scripts/deck_session.sh)
# ════════════════════════════════════════════════════════════════════════════
deck_bad=""
for p in / /ops /gods-eye /citizen /driver /comms /chat; do
    code=$(http_code "$DECK_URL$p" 20)
    [ "$code" = "200" ] || deck_bad="$deck_bad $p->${code:-no answer}"
done
anon_code=$(http_code "$DECK_URL/api/v1/movements/active" 20)
gate_state=$(curl -s --max-time 10 "$DECK_URL/api/session" 2>/dev/null \
    | python3 -c 'import json,sys; print("on" if json.load(sys.stdin).get("gate") else "off")' 2>/dev/null)
if deck_signin "$DECK_URL" "$SMOKE_OFFICER"; then signed=true; else signed=false; fi
proxy_code=$(deck_curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$DECK_URL/api/v1/movements/active")
case "$EXPECT_DECK_GATE:${gate_state:-unknown}" in
    on:on)   gate_ok=$([ "$anon_code" = 403 ] && echo true || echo false)
             gate_note="operator gate ON (anonymous /api/v1 → $anon_code, signed in as '$SMOKE_OFFICER')" ;;
    off:off|any:off) gate_ok=true; gate_note="operator gate OFF (no sign-in needed)" ;;
    any:on)  gate_ok=true; gate_note="operator gate ON (anonymous /api/v1 → $anon_code, signed in as '$SMOKE_OFFICER')" ;;
    *)       gate_ok=false; gate_note="operator gate ${gate_state:-unknown (no /api/session answer)}, expected $EXPECT_DECK_GATE (anonymous /api/v1 → $anon_code)" ;;
esac
deck_image=""
[ -n "$DECK_CONTAINER" ] && deck_image="; image $(docker inspect "$DECK_CONTAINER" --format '{{.Config.Image}}' 2>/dev/null)"
if [ -z "$deck_bad" ] && [ "$signed" = true ] && [ "$gate_ok" = true ] && [ "$proxy_code" = "200" ]; then
    ok "command-deck   $P_DECK /, /ops, /gods-eye, /citizen, /driver, /comms, /chat all 200; $gate_note; /api/v1 proxy → oracle 200$deck_image"
else
    fail "command-deck   $P_DECK route failures:${deck_bad:- none}; signed in=$signed; $gate_note; proxy /api/v1/movements/active -> ${proxy_code:-no answer}"
fi

# ════════════════════════════════════════════════════════════════════════════
# 15. REAL plan E2E — Lal Darwaja → SVPI Airport, class Z (createMovement →
#     pre-movement workflow → decider+gemma+oracle+postgis → the plan
#     registered as a corridor event, contract C-F/city_link)
# ════════════════════════════════════════════════════════════════════════════
DEPART=$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)
MOVEMENT_BODY='{"origin":[72.5813,23.0264],"destination":[72.6221,23.072],"vvip_class":"Z","planned_departure":"'$DEPART'"}'
CREATE_BODY="${MOVEMENT_BODY%\}}"',"title":"'"$SMOKE_TITLE"'"}'
mov_resp=$(curl -s --max-time 30 -X POST "$BRAIN_URL/movements" \
    -H 'Content-Type: application/json' -d "$CREATE_BODY" 2>/dev/null)
movement_id=$(printf '%s' "$mov_resp" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("movement_id",""))' 2>/dev/null)
PLAN_SEGMENT_IDS=""
PLAN_EVENT_ID=""
if [ -z "$movement_id" ]; then
    fail "plan E2E       POST /movements returned no movement_id (${mov_resp:-no answer})"
else
    plan_file="$SNAP_DIR/plan.json"
    plan_code=$(curl -s -o "$plan_file" -w '%{http_code}' --max-time "$PLAN_TIMEOUT_SEC" \
        -X POST "$BRAIN_URL/movements/$movement_id/plan" \
        -H 'Content-Type: application/json' -d "$MOVEMENT_BODY" 2>/dev/null)
    # line 1: verdict line (OK n route candidate(s); ... | FAIL ...); line 2: csv
    # segids; line 3: the corridor event the plan registered (or empty)
    mapfile -t plan_out < <(python3 - "$plan_file" <<'PY' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1]) as fh:
        d = json.load(fh)
except Exception:
    print("FAIL plan response unreadable")
    print("")
    print("")
    sys.exit(0)
primary = d.get("primary_route") or {}
alternates = d.get("alternate_routes") or []
seg_ids = primary.get("segment_ids") or []
n_candidates = (1 if primary.get("route_id") and seg_ids else 0) + len(alternates)
city = d.get("city_event") or {}
if n_candidates >= 1:
    print("OK %d route candidate(s); primary %s (%d segments: %s) status=%s security=%s; corridor event %s (%s)"
          % (n_candidates, primary.get("route_id"), len(seg_ids),
             " → ".join(primary.get("road_names") or []) or "unnamed",
             d.get("status"), d.get("security_compliant"),
             city.get("event_id") or "none", city.get("status") or "not registered"))
else:
    print("FAIL no route candidate: primary=%r alternates=%d status=%s"
          % (bool(primary.get("route_id")), len(alternates), d.get("status")))
print(",".join(str(s) for s in seg_ids))
print(city.get("event_id") or "" if city.get("status") == "registered" else "")
PY
    )
    plan_line="${plan_out[0]-}"
    PLAN_SEGMENT_IDS="${plan_out[1]-}"
    PLAN_EVENT_ID="${plan_out[2]-}"
    if [ "$plan_code" = "200" ] && [ "${plan_line#OK}" != "$plan_line" ]; then
        ok "plan E2E       $P_BRAIN movement $movement_id ('$SMOKE_TITLE') → ${plan_line#OK }"
    else
        fail "plan E2E       $P_BRAIN http=$plan_code ${plan_line:-$(head -c 200 "$plan_file" 2>/dev/null)}"
        PLAN_SEGMENT_IDS=""
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# 16. predict/eta — real chain off the planned route: optimize/routes gives
#     the honest length/segments, the point payload must always answer, and
#     the p10/p50/p90 band must be present exactly when the quantile
#     stream is (a fitted-but-unbanded model must label itself via
#     eta_band_reason; model=fallback documents its own degradation)
# ════════════════════════════════════════════════════════════════════════════
if [ -n "$PLAN_SEGMENT_IDS" ]; then
    eta_origin="${PLAN_SEGMENT_IDS%%,*}"
    eta_dest="${PLAN_SEGMENT_IDS##*,}"
    eta_note=$(ORACLE="$ORACLE_URL" SUMMARY="$summary_body" \
               ORIGIN_SEG="$eta_origin" DEST_SEG="$eta_dest" python3 - <<'PY' 2>/dev/null
import datetime, json, os, urllib.request

oracle = os.environ["ORACLE"]
summary = json.loads(os.environ["SUMMARY"])
now = datetime.datetime.now()

# 1. real candidate for this OD pair → honest length + segment list
req = urllib.request.Request(
    oracle + "/api/v1/optimize/routes",
    data=json.dumps({"origin_segment": int(os.environ["ORIGIN_SEG"]),
                     "destination_segment": int(os.environ["DEST_SEG"]),
                     "max_candidates": 3}).encode(),
    headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=60) as r:
    routes = json.loads(r.read())["routes"]
assert routes, "optimize/routes returned no candidate"
best = routes[0]

# 2. predict/eta off that candidate (speeds/congestion from the live summary)
req = urllib.request.Request(
    oracle + "/api/v1/predict/eta",
    data=json.dumps({
        "route_length_m": best["total_distance_m"],
        "num_segments": len(best["segment_ids"]),
        "avg_predicted_speed": max(float(summary.get("avg_speed_kmh")) or 40.0, 5.0),
        "avg_predicted_congestion": float(summary.get("avg_congestion_idx")) or 0.3,
        "hour": now.hour, "dow": now.weekday(),
        "num_signals": len(best["segment_ids"]),
        "segment_ids": best["segment_ids"],
        "horizon_min": 5,
    }).encode(),
    headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=60) as r:
    eta = json.loads(r.read())

# 3. contract: point payload always; band exactly when quantiles present
assert isinstance(eta.get("eta_seconds"), (int, float)) and eta["eta_seconds"] > 0, eta
assert isinstance(eta.get("model"), str) and eta["model"], eta
if "eta_band" in eta:
    band = eta["eta_band"]
    for k in ("p10_sec", "p50_sec", "p90_sec"):
        assert isinstance(band.get(k), (int, float)) and band[k] > 0, band
    assert band["p10_sec"] <= band["p50_sec"] <= band["p90_sec"], band
    assert isinstance(band.get("source"), str) and band["source"], band
    band_note = "band p10/p50/p90 = %s/%s/%ss (source %s)" % (
        band["p10_sec"], band["p50_sec"], band["p90_sec"], band["source"])
elif eta.get("model") == "fallback":
    band_note = "no band (model=fallback: unfitted estimator, documented point-only degradation)"
else:
    assert isinstance(eta.get("eta_band_reason"), str) and eta["eta_band_reason"], eta
    band_note = "no band, labelled reason=%s" % eta["eta_band_reason"]
print("route %s (%d segs, %.0fm): eta=%ss model=%s; %s" % (
    best["route_id"], len(best["segment_ids"]), best["total_distance_m"],
    eta["eta_seconds"], eta["model"], band_note))
PY
    )
    if [ -n "$eta_note" ]; then
        ok "predict/eta    $P_ORACLE $eta_note"
    else
        fail "predict/eta    $P_ORACLE chain failed (no output — see oracle logs)"
    fi
else
    fail "predict/eta    $P_ORACLE skipped — no planned route to chain from"
fi

# ════════════════════════════════════════════════════════════════════════════
# 17. diversions — the closure/detour plan over the planned Z route
#     (optimize/diversion_planner.py; a 503 is the planner's labelled
#     'unavailable', never a silent [])
# ════════════════════════════════════════════════════════════════════════════
if [ -n "$PLAN_SEGMENT_IDS" ]; then
    emit < <(ORACLE="$ORACLE_URL" SEGS="$PLAN_SEGMENT_IDS" PORT="$P_ORACLE" python3 - <<'PY' 2>/dev/null || echo "FAIL diversions probe crashed"
import json, os, urllib.error, urllib.request
lbl = "diversions     %s" % os.environ["PORT"]
segs = [int(s) for s in os.environ["SEGS"].split(",") if s]
req = urllib.request.Request(
    os.environ["ORACLE"] + "/api/v1/optimize/diversions",
    data=json.dumps({"route_segment_ids": segs}).encode(),
    headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())
except urllib.error.HTTPError as exc:
    print("FAIL %s http=%d %s" % (lbl, exc.code, exc.read()[:200].decode("utf-8", "replace")))
    raise SystemExit(0)
plans = d.get("diversions") or []
if plans:
    detours = sum(1 for p in plans if p.get("detour_found"))
    print("OK %s %d closure(s) over route [%s]: %d with a detour, %d hold/meter; unplanned %s (lengths %s, speeds %s)"
          % (lbl, len(plans), ",".join(map(str, segs)), detours, len(plans) - detours,
             d.get("unplanned_segment_ids") or "none", d.get("segment_lengths_source"), d.get("speeds_source")))
else:
    print("FAIL %s empty diversion plan for route [%s] (unplanned %s)"
          % (lbl, ",".join(map(str, segs)), d.get("unplanned_segment_ids")))
PY
    )
else
    fail "diversions     $P_ORACLE skipped — no planned route to plan closures for"
fi

# ════════════════════════════════════════════════════════════════════════════
# 18. the city picture — the fused reads the God's-eye and /ops stand on
#     (contract C-C + C-P + C-Q): entity kind counts with every feed 'ok',
#     the camera registry, THIS run's corridor event audit, the historical
#     baseline's provenance and the city band's provenance
# ════════════════════════════════════════════════════════════════════════════
emit < <(ORACLE="$ORACLE_URL" EVENT_ID="$PLAN_EVENT_ID" SEG="${PLAN_SEGMENT_IDS%%,*}" PORT="$P_ORACLE" \
         python3 - <<'PY' 2>/dev/null || echo "FAIL city-picture probe crashed"
import collections, json, os, urllib.error, urllib.request
oracle, port = os.environ["ORACLE"], os.environ["PORT"]


def get(path):
    try:
        with urllib.request.urlopen(oracle + path, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:  # noqa: BLE001 — unreachable / unreadable
        return 0, None


code, d = get("/api/v1/city/entities")
if code == 200 and isinstance(d, dict) and isinstance(d.get("entities"), list) and isinstance(d.get("feeds"), dict):
    kinds = collections.Counter(str(e.get("kind")) for e in d["entities"])
    bad = {k: (v or {}).get("status") for k, v in d["feeds"].items() if (v or {}).get("status") != "ok"}
    counts = " ".join("%s=%d" % kv for kv in sorted(kinds.items())) or "none"
    print("%s city/entities  %s kinds %s; feeds %s"
          % ("FAIL" if bad else "OK", port, counts, ("not ok: %s" % bad) if bad else "all %d ok" % len(d["feeds"])))
else:
    print("FAIL city/entities  %s http=%s (unreadable snapshot)" % (port, code))

code, d = get("/api/v1/cameras")
n = (d or {}).get("count") if isinstance(d, dict) else None
print("%s cameras        %s http=%s, %s camera(s) (registry %s)"
      % ("OK" if code == 200 and isinstance(n, int) and n >= 1 else "FAIL", port, code, n,
         (d or {}).get("registry") if isinstance(d, dict) else "?"))

event_id = os.environ.get("EVENT_ID") or ""
if not event_id:
    print("FAIL event audit    %s no corridor event from this run's plan to audit (the plan registered none)" % port)
else:
    code, d = get("/api/v1/events/%s/audit" % event_id)
    items = (d or {}).get("items") if isinstance(d, dict) else None
    good = code == 200 and isinstance(items, list) and len(items) >= 1
    kinds = sorted({str(i.get("kind")) for i in items or []})
    print("%s event audit    %s /events/%s/audit http=%s, %s item(s) (%s)"
          % ("OK" if good else "FAIL", port, event_id, code, len(items or []), ",".join(kinds) or "none"))

seg = os.environ.get("SEG") or "1001"
code, d = get("/api/v1/traffic/historical?segment_id=%s" % seg)
prov = (d or {}).get("provenance") if isinstance(d, dict) else None
good = code == 200 and prov in ("real", "synthetic", "none")
print("%s historical     %s segment %s http=%s provenance=%s (real rows %s, synthetic rows %s)"
      % ("OK" if good else "FAIL", port, seg, code, prov,
         len((d or {}).get("aggregates") or []) if isinstance(d, dict) else "?",
         len((d or {}).get("synthetic_aggregates") or []) if isinstance(d, dict) else "?"))

code, d = get("/api/v1/signals/plans")
since = (d or {}).get("city_band_since") if isinstance(d, dict) else None
good = code == 200 and isinstance(since, str) and bool(since)
print("%s signal plans   %s http=%s city_band=%s since %s (simulated feed %s, stale %s, writer_enabled %s)"
      % ("OK" if good else "FAIL", port, code, (d or {}).get("city_band") if isinstance(d, dict) else "?", since,
         (d or {}).get("city_band_simulated_feed") if isinstance(d, dict) else "?",
         (d or {}).get("city_band_stale") if isinstance(d, dict) else "?",
         (d or {}).get("writer_enabled") if isinstance(d, dict) else "?"))
PY
)

# ════════════════════════════════════════════════════════════════════════════
# 19. MCP rail — POST /mcp tools/list (JSON-RPC 2.0, contract C-D2): the
#     brain exposes the read-only city tools and the canvas verbs, and NO
#     tool name makes the AI an actor (R2, mcp/server.py FORBIDDEN_TOOL_VERBS
#     plus the generic write verbs)
# ════════════════════════════════════════════════════════════════════════════
emit < <(BRAIN="$BRAIN_URL" MIN="$MCP_MIN_TOOLS" PORT="$P_BRAIN" python3 - <<'PY' 2>/dev/null || echo "FAIL mcp probe crashed"
import json, os, re, urllib.request
lbl = "mcp rail       %s" % os.environ["PORT"]
ACTOR = re.compile(r"approve|reject|decide|earmark|dispatch_request|withdraw|transition"
                   r"|cancel|create|delete|update|insert|write", re.IGNORECASE)
req = urllib.request.Request(
    os.environ["BRAIN"] + "/mcp",
    data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode(),
    headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        tools = json.loads(r.read())["result"]["tools"]
    names = [str(t["name"]) for t in tools]
except Exception as exc:  # noqa: BLE001
    print("FAIL %s tools/list unreadable (%s)" % (lbl, type(exc).__name__))
    raise SystemExit(0)
writers = [n for n in names if ACTOR.search(n)]
canvas = [n for n in names if n.startswith("canvas_")]
good = len(names) >= int(os.environ["MIN"]) and not writers
print("%s %s tools/list: %d tool(s) (%d canvas directive verb(s)); actor/writer tools: %s"
      % ("OK" if good else "FAIL", lbl, len(names), len(canvas), ",".join(writers) or "none"))
PY
)

# ════════════════════════════════════════════════════════════════════════════
# 20. chat → canvas directive — one two-tier turn over POST /chat/stream
#     (NDJSON, contract C-D/C-D2). The brain grounds a map request with a
#     read-only city_query_entities and proposes a canvas directive for the
#     entity it resolved (orchestrator._map_post_step); a human applies it.
#     Model-bound (decider + Gemma): one call, after the plan, never in
#     parallel with another model call.
# ════════════════════════════════════════════════════════════════════════════
emit < <(BRAIN="$BRAIN_URL" ORACLE="$ORACLE_URL" TIMEOUT="$CHAT_TIMEOUT_SEC" PORT="$P_BRAIN" \
         python3 - <<'PY' 2>/dev/null || echo "FAIL chat probe crashed"
import json, os, urllib.request
lbl = "chat→directive %s" % os.environ["PORT"]
PROMPT = "show me the stolen vehicle on the map"
try:  # what the city picture holds: the brain's own grounding query
    with urllib.request.urlopen(os.environ["ORACLE"] + "/api/v1/city/entities?kinds=vehicle", timeout=30) as r:
        vehicles = {e["urn"] for e in json.loads(r.read()).get("entities", [])
                    if e.get("kind") == "vehicle" and e.get("urn")}
except Exception as exc:  # noqa: BLE001
    print("FAIL %s cannot read the city picture's earmarked vehicles (%s)" % (lbl, type(exc).__name__))
    raise SystemExit(0)
req = urllib.request.Request(
    os.environ["BRAIN"] + "/chat/stream",
    data=json.dumps({"message": PROMPT}).encode(),
    headers={"Content-Type": "application/json"})
events = []
try:
    with urllib.request.urlopen(req, timeout=float(os.environ["TIMEOUT"])) as r:
        for raw in r:
            if raw.strip():
                events.append(json.loads(raw))
except Exception as exc:  # noqa: BLE001
    print("FAIL %s stream broke after %d event(s) (%s)" % (lbl, len(events), type(exc).__name__))
    raise SystemExit(0)
calls = [str((e.get("data") or {}).get("name")) for e in events if e.get("type") == "tool_call"]
directives = [e.get("data") or {} for e in events if e.get("type") == "directive"]
prose = "".join(e["data"] for e in events if e.get("type") == "token" and isinstance(e.get("data"), str)).strip()
errors = [str(e.get("data"))[:120] for e in events if e.get("type") == "error"]
done = any(e.get("type") == "done" for e in events)


def targets(d):
    args = d.get("args") or {}
    return [args["urn"]] if args.get("urn") else list(args.get("urns") or [])


on_vehicle = [d for d in directives if any(str(u).startswith("vehicle:") for u in targets(d))]
base_ok = done and not errors and calls and len(prose) >= 20 and not prose.startswith(("{", "["))
summary = "tool_call(s) %s; prose %d chars; directive(s) %s" % (
    ",".join(calls) or "none", len(prose),
    ",".join("%s→%s" % (d.get("verb"), "|".join(targets(d)) or "-") for d in directives) or "none")
if not base_ok:
    print("FAIL %s '%s': %s%s" % (lbl, PROMPT, summary, ("; error: %s" % errors[0]) if errors else ""))
elif vehicles:
    hit = [d for d in on_vehicle if set(targets(d)) & vehicles]
    print("%s %s '%s': %s; the city picture holds %d earmarked vehicle(s)%s"
          % ("OK" if hit else "FAIL", lbl, PROMPT, summary, len(vehicles),
             "" if hit else " — no directive targets one of them"))
elif on_vehicle:
    print("FAIL %s '%s': %s — a directive targets a vehicle the city picture does not hold" % (lbl, PROMPT, summary))
else:
    print("OK %s '%s': %s" % (lbl, PROMPT, summary))
    print("SKIP chat→directive: the city picture holds no earmarked vehicle, so no directive is due (the directive half was not exercised)")
PY
)

# ════════════════════════════════════════════════════════════════════════════
# 21. live drift panel — model_drift_panel.sh against THIS stack
#     (decider/gemma/embeds + this stack's oracle)
# ════════════════════════════════════════════════════════════════════════════
if [ "$DRIFT_PANEL" = 0 ]; then
    skip "drift panel    DRIFT_PANEL=0 — not run (it upserts its 2 marker docs into the shared :8085 store)"
else
    drift_log="$SNAP_DIR/drift_panel.log"
    if MODEL_HOST_URL="$DECIDER_URL" LLAMA_BASE_URL="$LLAMA_URL" \
       EMBEDDING_SERVICE_URL="$EMBED_URL" TRAFFIC_ORACLE_URL="$ORACLE_URL" \
       bash "$SCRIPT_DIR/model_drift_panel.sh" > "$drift_log" 2>&1; then
        ok "drift panel    live vs baseline: $(grep -o 'DRIFT OK: [0-9]*/[0-9]* probes green' "$drift_log" | tail -1)"
    else
        fail "drift panel    drifted/unreachable (last lines: $(tail -n 6 "$drift_log" 2>/dev/null | tr '\n' ' '))"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# PHASE 2 — the original stack must be BYTE-IDENTICAL to the phase-0 snapshot
# ════════════════════════════════════════════════════════════════════════════
snapshot_original_containers > "$SNAP_DIR/containers.after"
snapshot_original_images     > "$SNAP_DIR/images.after"
if [ "$n_guard_containers" -eq 0 ] && [ "$n_guard_images" -eq 0 ] \
   && [ ! -s "$SNAP_DIR/containers.after" ] && [ ! -s "$SNAP_DIR/images.after" ]; then
    skip "original-stack immutability: no original container or image tag exists — nothing to compare"
else
    original_touched=0
    for kind in containers images; do
        if ! diff -u "$SNAP_DIR/$kind.before" "$SNAP_DIR/$kind.after" > "$SNAP_DIR/$kind.diff" 2>/dev/null; then
            original_touched=1
            fail "original-stack $kind CHANGED during the smoke (or the redeploy before it):"
            sed 's/^/      /' "$SNAP_DIR/$kind.diff" >&2
        fi
    done
    if [ "$original_touched" = 0 ]; then
        ok "original stack UNTOUCHED ($n_guard_containers container(s) + $n_guard_images shared tag(s) byte-identical, incl. StartedAt)"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════════════════════
echo "────"
skip_note=""
[ "$SKIPS" -gt 0 ] && skip_note=" ($SKIPS SKIP line(s) above: not exercised, not passed)"
if [ "$FAILURES" -eq 0 ]; then
    echo "argus-full-smoke: ALL PASS — stack live end to end${skip_note}"
    exit 0
fi
echo "argus-full-smoke: ${FAILURES} FAILURE(S)${skip_note}"
exit 1
