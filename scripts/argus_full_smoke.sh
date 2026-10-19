#!/usr/bin/env bash
# argus_full_smoke.sh — FULL-SYSTEM live smoke for the ARGUS clone stack.
#
# Proves the Wave-1/2/3 tree code is running LIVE end to end on the clone
# (project `vvip-argus`, infra/compose.argus-override.yml) — every service,
# every shared host model, the deck, and one REAL movement plan — while
# guarding the ORIGINAL stack (project `vvip`) as untouched:
#
#   PHASE 0 (before any docker action TAKEN BY THIS SCRIPT — note the
#   runbook order is build → up -d → this smoke, per the compose.argus-
#   override.yml usage block, so the redeploy window itself is guarded by
#   CONSTRUCTION, not observation: the argus override builds vvip-argus-
#   {traffic-oracle,convoy-brain,command-deck} and resets the build
#   stanzas of the services that reuse shared images) snapshots the
#   original stack read-only: every non-argus vvip-* container (stable
#   state fields — status/running/exit/restarts/StartedAt; never
#   Up-duration strings that drift with the clock) and every non-argus
#   vvip-*/veer-* image tag with its image ID. It ALSO asserts the
#   original fleet is AVAILABLE (Running=true per container) — liveness,
#   not just immutability: a fleet killed by a host reboot fails the gate
#   even though nothing about it changed. PHASE 2 re-snapshots and diffs;
#   ANY difference (including a StartedAt move from a stop/start cycle)
#   fails the gate with the exact diff.
#
#   PHASE 0.5 waits (bounded, default 180s, env CONVERGE_TIMEOUT_SEC) for
#   the just-redeployed stack to CONVERGE before any component probe:
#   traffic-oracle /health 200, convoy-brain /health status ok WITH both
#   model tiers connected, deck / 200, and the emulator twin seam green
#   (it probes the argus oracle live on every /health call —
#   services/event-emulator/src/event_emulator/api.py health handler).
#   Rationale, from the round-1 gate run: `docker compose up -d` returns
#   when containers START, not when the apps inside bind — the recreate
#   window (old oracle stopped at T, new oracle binding :8091 at T+6s;
#   emulator twin flag and the deck's /api proxy following it) failed six
#   oracle-dependent probes against a stack that was fully green one
#   minute later. The wait absorbs exactly that window and nothing else:
#   a stack that never converges (crash loop, wiring fault) burns the
#   timeout and the probes below then fail honestly — no check is
#   weakened, skipped, or retried to success; every probe still asserts
#   exactly once on the converged stack.
#
# Components smoked (one line each):
#   decider        :8083  POST /decide — typed choice+noul+score probe
#   gemma          :8084  POST /v1/chat/completions — agent-style call
#   embeddings     :8085  POST /embed/documents + /search — ONE call runs
#                         BOTH encoders per doc (late + dense)
#   emulator       :8090  GET /health — status + store/valkey/twin/nats
#   traffic-oracle :8091  GET /health + GET /api/v1/corridor/summary
#                         (shape + status-vs-threshold consistency)
#   convoy-brain   :8092  GET /health — status ok AND decider+gemma
#                         connected (models wired)
#   argus images   — the wave services run their OWN -argus image tags,
#                         never the shared vvip-*:latest ones
#   postgis        :56434 pg_isready + TCP + migration ledger count ==
#                         tree migration files (VVIP/services/corridor-store/
#                         migrations/*.sql)
#   valkey         :6380  PING → PONG
#   nats           :4223  protocol-level INFO preamble on connect
#   signal-ingress — WORKING proof, not just container-up: a bounded
#                         subscription to its NATS subject
#                         corridor.traffic.live (the Arrow IPC publisher's
#                         subject, publish/arrow_ipc.rs) must observe ≥1
#                         message inside the window, so a stalled or
#                         crash-looping-but-currently-up publisher fails
#   command-deck   :5273  /, /ops, /gods-eye, /citizen, /driver, /comms,
#                         /chat all 200 + the /api/v1 proxy to :8091 live
#   plan E2E       — POST /movements + POST /movements/{id}/plan (Lal
#                         Darwaja 72.5798,23.0254 → SVPI Airport
#                         72.6266,23.0733, class Z); asserts ≥1 route
#                         candidate in the real workflow answer
#   predict/eta    — POST /api/v1/predict/eta chained off the planned
#                         route (optimize/routes supplies the real
#                         length/segments): point payload always; p10/p50/
#                         p90 band fields exactly when the quantile stream
#                         is present (a fitted-but-degraded model must say
#                         so via eta_band_reason)
#   drift panel    — model_drift_panel.sh LIVE against :8083/:8084/:8085/
#                         :8091 (its own defaults; schema+anchors+latency
#                         bounds vs the recorded baseline)
#
# Usage: bash VVIP/scripts/argus_full_smoke.sh   (no arguments)
# Env:   endpoints/containers are env-overridable with the loopback argus
#        defaults preserved (the ${VAR:-default} pattern of showcase_run.sh
#        and hooks.server.ts).
#
# Security: every URL below is an internal endpoint (an env-overridable
# config constant of this script, http scheme, loopback host) — none comes
# from external input, so no host validation is required before these
# requests (the convoy-brain http_guard carve-out for platform services).
# All docker use is read-only (ps/inspect/images + exec'd SELECT/PING/
# NATS-subscribe; every exec is `timeout`-bounded so a wedged container
# cannot hang the gate), and no shell command is ever built by string
# concatenation.
#
# SHARED-SERVICE WRITES, disclosed: the model hosts :8083/:8084/:8085 are
# SHARED with the original stack by design (compose.argus-override.yml
# header), and two probes write bounded state into the shared :8085
# embedding store: the embeddings leg upserts 2 marker docs (ref_ids
# argus-smoke:1/2 — stable across runs, so re-runs upsert rather than
# accumulate) and model_drift_panel.sh does the same with its own
# drift-panel:7f3a:a/b markers. The "original stack untouched" guarantee
# below covers the original fleet's CONTAINERS and IMAGE TAGS; shared host
# model-service state is mutated by those probes, exactly as documented
# here and as the drift panel's own header records.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VVIP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── internal endpoints (env-overridable constants, loopback-only defaults) ──
DECIDER_URL="${DECIDER_URL:-http://127.0.0.1:${DECIDER_PORT:-8083}}"
LLAMA_URL="${LLAMA_URL:-http://127.0.0.1:${LLAMA_PORT:-8084}}"
EMBED_URL="${EMBED_URL:-http://127.0.0.1:${EMBED_PORT:-8085}}"
EMULATOR_URL="${EMULATOR_URL:-http://127.0.0.1:${EMULATOR_PORT:-8090}}"
ORACLE_URL="${ORACLE_URL:-http://127.0.0.1:${ORACLE_PORT:-8091}}"
BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:${BRAIN_PORT:-8092}}"
DECK_URL="${DECK_URL:-http://127.0.0.1:${DECK_PORT:-5273}}"

# ── fleet addressing (argus compose project; derived container names) ───────
COMPOSE_PROJECT="${COMPOSE_PROJECT:-vvip-argus}"
PG_CONTAINER="${POSTGIS_CONTAINER:-${COMPOSE_PROJECT}-postgis}"
VALKEY_CONTAINER="${VALKEY_CONTAINER:-${COMPOSE_PROJECT}-valkey}"
NATS_HOST="${NATS_HOST:-127.0.0.1}"
NATS_PORT="${NATS_PORT:-4223}"
PG_HOST="${POSTGIS_HOST:-127.0.0.1}"
PG_PORT="${POSTGIS_PORT:-56434}"
PG_USER="${POSTGIS_USER:-corridor_admin}"
PG_DB="${POSTGIS_DB:-corridor_db}"
ORACLE_CONTAINER="${ORACLE_CONTAINER:-${COMPOSE_PROJECT}-traffic-oracle}"
BRAIN_CONTAINER="${BRAIN_CONTAINER:-${COMPOSE_PROJECT}-convoy-brain}"
DECK_CONTAINER="${DECK_CONTAINER:-${COMPOSE_PROJECT}-command-deck}"
INGRESS_CONTAINER="${INGRESS_CONTAINER:-${COMPOSE_PROJECT}-signal-ingress}"

# scheme guard: http/https only (config constants; rejects anything else early)
url_scheme_ok() { case "$1" in http://*|https://*) return 0 ;; *) return 1 ;; esac; }
for u in "$DECIDER_URL" "$LLAMA_URL" "$EMBED_URL" "$EMULATOR_URL" \
         "$ORACLE_URL" "$BRAIN_URL" "$DECK_URL"; do
    url_scheme_ok "$u" || { echo "FATAL: endpoint not http/https: $u" >&2; exit 2; }
done

PLAN_TIMEOUT_SEC="${PLAN_TIMEOUT_SEC:-300}"   # brain workflows are heavyweight
MIGRATIONS_DIR="$VVIP_DIR/services/corridor-store/migrations"

FAILURES=0
fail() { FAILURES=$((FAILURES + 1)); echo "FAIL $*"; }
ok()   { echo "OK   $*"; }

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-15}" "$1"; }
tcp_open() { (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && exec 3>&- 3<&- && return 0; return 1; }

# ════════════════════════════════════════════════════════════════════════════
# PHASE 0 — snapshot the ORIGINAL stack read-only, before anything else
# ════════════════════════════════════════════════════════════════════════════
SNAP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/argus-full-smoke.XXXXXX")"
trap 'rm -rf "$SNAP_DIR"' EXIT

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

# Image-tag snapshot: every vvip-*/veer-* tag WITHOUT the -argus suffix,
# with its image ID (a retag/rebuild changes the ID even for identical
# content, which is exactly what must not happen to the original stack).
snapshot_original_images() {
    docker images --format '{{.Repository}}:{{.Tag}}|{{.ID}}' 2>/dev/null \
        | grep -E '^(vvip|veer)-' | grep -v -- '-argus' | sort
}

snapshot_original_containers > "$SNAP_DIR/containers.before"
snapshot_original_images     > "$SNAP_DIR/images.before"
n_guard_containers=$(wc -l < "$SNAP_DIR/containers.before")
n_guard_images=$(wc -l < "$SNAP_DIR/images.before")
echo "──── argus full smoke: guarding the original stack ($n_guard_containers container(s), $n_guard_images shared image tag(s))"

# ── availability: the guarded fleet must be ALIVE, not merely unchanged ────
# Immutability alone would bless a fleet killed by a host reboot; the pass
# bar is "original containers still up", so every guarded container must
# report Running=true here and now.
guard_down=""
while IFS='|' read -r g_name _g_img _g_status g_running _g_exit _g_rc _g_start; do
    [ "$g_running" = "true" ] || guard_down="$guard_down ${g_name#/}"
done < "$SNAP_DIR/containers.before"
if [ -n "$guard_down" ]; then
    fail "original fleet DOWN (not Running):$guard_down — restore the original stack (its own project, NOT part of the argus redeploy) and re-run; immutability alone is not a pass"
else
    ok "original fleet AVAILABLE ($n_guard_containers container(s) Running=true)"
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
    ok "decider        :8083 typed choice+noul+score answered ($decider_note)"
else
    fail "decider        :8083 /decide typed call failed (body: ${decider_body:-no answer})"
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
    ok "gemma          :8084 chat completion answered ($gemma_note)"
else
    fail "gemma          :8084 chat completion failed (body: ${gemma_body:-no answer})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 3. embeddings :8085 — BOTH encoders (one /embed/documents call runs the
#    late multivector AND the dense encoder per document), then /search
# ════════════════════════════════════════════════════════════════════════════
embed_body="$(curl -sf --max-time 120 -X POST "$EMBED_URL/embed/documents" \
    -H 'Content-Type: application/json' \
    -d '{"documents": [{"body": "Argus full smoke: Z-class convoy staged at Lal Darwaja; corridor to SVPI Airport nominal.",
                        "title": "argus-smoke-a", "ref_id": "argus-smoke:1"},
                       {"body": "Argus full smoke: congestion building on the old-city approach; recovery projected 200s.",
                        "title": "argus-smoke-b", "ref_id": "argus-smoke:2"}],
         "kind": "pattern"}' 2>/dev/null)"
search_body="$(curl -sf --max-time 60 -X POST "$EMBED_URL/search" \
    -H 'Content-Type: application/json' \
    -d '{"query": "argus full smoke corridor congestion", "kinds": ["pattern"]}' 2>/dev/null)"
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
search_note=$(printf '%s' "$search_body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    hits = d.get("results")
except Exception:
    sys.exit(1)
if not isinstance(hits, list):
    sys.exit(1)
print("%d hits" % len(hits))
' 2>/dev/null)
if [ -n "$embed_note" ] && [ -n "$search_note" ]; then
    ok "embeddings     :8085 $embed_note; search $search_note"
else
    fail "embeddings     :8085 encode/search failed (embed: ${embed_body:-none}; search: ${search_body:-none})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 4. event emulator :8090 — shared host service, fully wired
# ════════════════════════════════════════════════════════════════════════════
emu_body="$(curl -s --max-time 10 "$EMULATOR_URL/health" 2>/dev/null)"
emu_note=$(printf '%s' "$emu_body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
s = d.get("stack", {})
if d.get("status") != "ok":
    sys.exit(1)
if not (s.get("store") and s.get("valkey") and s.get("twin") and s.get("nats")):
    sys.exit(1)
print("store+valkey+twin+nats wired, library %s" % d.get("library", "?"))
' 2>/dev/null)
if [ -n "$emu_note" ]; then
    ok "emulator       :8090 /health ok ($emu_note)"
else
    fail "emulator       :8090 not healthy (body: ${emu_body:-no answer})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 5. argus traffic-oracle :8091 — health + corridor summary contract
# ════════════════════════════════════════════════════════════════════════════
oracle_health="$(curl -s --max-time 10 "$ORACLE_URL/health" 2>/dev/null)"
if printf '%s' "$oracle_health" | grep -q '"status": *"ok"'; then
    ok "traffic-oracle :8091 /health ok (image $(docker inspect "$ORACLE_CONTAINER" --format '{{.Config.Image}}' 2>/dev/null))"
else
    fail "traffic-oracle :8091 /health not ok (body: ${oracle_health:-no answer})"
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
if not (isinstance(d["total_segments"], int) and d["total_segments"] >= 0):
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
print("status %s (congestion %.3f, %d segments) — consistent with thresholds" % (d["status"], cong, d["total_segments"]))
' 2>/dev/null)
if [ -n "$summary_note" ]; then
    ok "corridor       :8091 summary contract holds ($summary_note)"
else
    fail "corridor       :8091 summary malformed/inconsistent (body: ${summary_body:-no answer})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 6. argus convoy-brain :8092 — status ok AND both model tiers connected
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
    ok "convoy-brain   :8092 /health ok, decider+gemma connected ($brain_note; image $(docker inspect "$BRAIN_CONTAINER" --format '{{.Config.Image}}' 2>/dev/null))"
else
    fail "convoy-brain   :8092 not healthy/models disconnected (body: ${brain_body:-no answer})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 7. argus image ownership — the wave services run their OWN -argus tags
# ════════════════════════════════════════════════════════════════════════════
images_ok=true
for pair in "traffic-oracle:vvip-argus-traffic-oracle" \
            "convoy-brain:vvip-argus-convoy-brain" \
            "command-deck:vvip-argus-command-deck"; do
    svc="${pair%%:*}"; want="${pair##*:}:latest"
    running="$(docker inspect "$COMPOSE_PROJECT-$svc" --format '{{.State.Running}}' 2>/dev/null)"
    img="$(docker inspect "$COMPOSE_PROJECT-$svc" --format '{{.Config.Image}}' 2>/dev/null)"
    if [ "$running" = "true" ] && [ "$img" = "$want" ]; then
        :
    else
        images_ok=false
        fail "argus-images   $COMPOSE_PROJECT-$svc running=$running image=$img (want $want)"
    fi
done
if [ "$images_ok" = true ]; then
    ok "argus-images   wave services on their own tags (traffic-oracle/convoy-brain/command-deck → vvip-argus-*)"
fi

# ════════════════════════════════════════════════════════════════════════════
# 8. postgis :56434 — ready + migration ledger complete vs the tree
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
# 9. valkey :6380 — PONG
# ════════════════════════════════════════════════════════════════════════════
pong=$(timeout 10 docker exec "$VALKEY_CONTAINER" valkey-cli ping 2>/dev/null)
if [ "$pong" = "PONG" ]; then
    ok "valkey         :6380 PONG"
else
    fail "valkey         :6380 no PONG (got: ${pong:-none})"
fi

# ════════════════════════════════════════════════════════════════════════════
# 10. nats :4223 — protocol-level INFO preamble (pure socket read; the
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
# 11. signal-ingress — WORKING proof: container running AND its NATS
#     subject actually carries messages. The service publishes Arrow IPC
#     batches to corridor.traffic.live (src/publish/arrow_ipc.rs
#     SUBJECT_LIVE, ~1 batch/s per its progress logs); the oracle image
#     ships nats-py (VVIP_NATS_URL wiring), so a bounded subscription from
#     inside the argus stack observes the live stream. A stalled or
#     crash-looping-but-currently-up publisher yields 0 messages → FAIL.
# ════════════════════════════════════════════════════════════════════════════
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
ingress_msgs="${ingress_msgs:-0}"
if [ "$(docker inspect "$INGRESS_CONTAINER" --format '{{.State.Running}}' 2>/dev/null)" = "true" ] \
   && [ "$ingress_msgs" -ge 1 ] 2>/dev/null; then
    restarts=$(docker inspect "$INGRESS_CONTAINER" --format '{{.RestartCount}}' 2>/dev/null)
    ok "signal-ingress WORKING: $ingress_msgs message(s) on corridor.traffic.live in 15s (container running, restarts=${restarts:-0})"
else
    fail "signal-ingress not publishing (container running=$(docker inspect "$INGRESS_CONTAINER" --format '{{.State.Running}}' 2>/dev/null); messages in 15s window: $ingress_msgs)"
fi

# ════════════════════════════════════════════════════════════════════════════
# 12. command-deck :5273 — every route + the /api/v1 proxy to the clone
# ════════════════════════════════════════════════════════════════════════════
deck_bad=""
for p in / /ops /gods-eye /citizen /driver /comms /chat; do
    code=$(http_code "$DECK_URL$p" 20)
    [ "$code" = "200" ] || deck_bad="$deck_bad $p->${code:-no answer}"
done
proxy_code=$(http_code "$DECK_URL/api/v1/movements/active" 20)
if [ -z "$deck_bad" ] && [ "$proxy_code" = "200" ]; then
    ok "command-deck   :5273 /, /ops, /gods-eye, /citizen, /driver, /comms, /chat all 200; /api/v1 proxy → :8091 live (image $(docker inspect "$DECK_CONTAINER" --format '{{.Config.Image}}' 2>/dev/null))"
else
    fail "command-deck   :5273 route failures:${deck_bad:- none} proxy /api/v1/movements/active -> ${proxy_code:-no answer}"
fi

# ════════════════════════════════════════════════════════════════════════════
# 13. REAL plan E2E — Lal Darwaja → SVPI Airport, class Z (createMovement →
#     pre-movement workflow → decider+gemma+oracle+postgis)
# ════════════════════════════════════════════════════════════════════════════
DEPART=$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)
MOVEMENT_BODY='{"origin":[72.5798,23.0254],"destination":[72.6266,23.0733],"vvip_class":"Z","planned_departure":"'$DEPART'"}'
mov_resp=$(curl -s --max-time 30 -X POST "$BRAIN_URL/movements" \
    -H 'Content-Type: application/json' -d "$MOVEMENT_BODY" 2>/dev/null)
movement_id=$(printf '%s' "$mov_resp" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("movement_id",""))' 2>/dev/null)
PLAN_SEGMENT_IDS=""
if [ -z "$movement_id" ]; then
    fail "plan E2E       POST /movements returned no movement_id (${mov_resp:-no answer})"
else
    plan_file="$SNAP_DIR/plan.json"
    plan_code=$(curl -s -o "$plan_file" -w '%{http_code}' --max-time "$PLAN_TIMEOUT_SEC" \
        -X POST "$BRAIN_URL/movements/$movement_id/plan" \
        -H 'Content-Type: application/json' -d "$MOVEMENT_BODY" 2>/dev/null)
    # line 1: verdict line (OK n route candidate(s); ... | FAIL ...); line 2: csv segids
    mapfile -t plan_out < <(python3 - "$plan_file" <<'PY' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1]) as fh:
        d = json.load(fh)
except Exception:
    print("FAIL plan response unreadable")
    print("")
    sys.exit(0)
primary = d.get("primary_route") or {}
alternates = d.get("alternate_routes") or []
seg_ids = primary.get("segment_ids") or []
n_candidates = (1 if primary.get("route_id") and seg_ids else 0) + len(alternates)
if n_candidates >= 1:
    print("OK %d route candidate(s); primary %s (%d segments) status=%s security=%s"
          % (n_candidates, primary.get("route_id"), len(seg_ids),
             d.get("status"), d.get("security_compliant")))
else:
    print("FAIL no route candidate: primary=%r alternates=%d status=%s"
          % (bool(primary.get("route_id")), len(alternates), d.get("status")))
print(",".join(str(s) for s in seg_ids))
PY
    )
    plan_line="${plan_out[0]-}"
    PLAN_SEGMENT_IDS="${plan_out[1]-}"
    if [ "$plan_code" = "200" ] && [ "${plan_line#OK}" != "$plan_line" ]; then
        ok "plan E2E       :8092 movement $movement_id → ${plan_line#OK }"
    else
        fail "plan E2E       :8092 http=$plan_code ${plan_line:-$(head -c 200 "$plan_file" 2>/dev/null)}"
        PLAN_SEGMENT_IDS=""
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# 14. predict/eta — real chain off the planned route: optimize/routes gives
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
        ok "predict/eta    :8091 $eta_note"
    else
        fail "predict/eta    :8091 chain failed (no output — see oracle logs)"
    fi
else
    fail "predict/eta    :8091 skipped — no planned route to chain from"
fi

# ════════════════════════════════════════════════════════════════════════════
# 15. live drift panel — model_drift_panel.sh against THIS stack
#     (decider/gemma/embeds + the ARGUS oracle on :8091)
# ════════════════════════════════════════════════════════════════════════════
drift_log="$SNAP_DIR/drift_panel.log"
if MODEL_HOST_URL="$DECIDER_URL" LLAMA_BASE_URL="$LLAMA_URL" \
   EMBEDDING_SERVICE_URL="$EMBED_URL" TRAFFIC_ORACLE_URL="$ORACLE_URL" \
   bash "$SCRIPT_DIR/model_drift_panel.sh" > "$drift_log" 2>&1; then
    ok "drift panel    live vs baseline: $(grep -o 'DRIFT OK: [0-9]*/[0-9]* probes green' "$drift_log" | tail -1)"
else
    fail "drift panel    drifted/unreachable (last lines: $(tail -n 6 "$drift_log" 2>/dev/null | tr '\n' ' '))"
fi

# ════════════════════════════════════════════════════════════════════════════
# PHASE 2 — the original stack must be BYTE-IDENTICAL to the phase-0 snapshot
# ════════════════════════════════════════════════════════════════════════════
snapshot_original_containers > "$SNAP_DIR/containers.after"
snapshot_original_images     > "$SNAP_DIR/images.after"
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

# ════════════════════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════════════════════
echo "────"
if [ "$FAILURES" -eq 0 ]; then
    echo "argus-full-smoke: ALL PASS — Wave 1-3 stack live end to end, original fleet available and untouched"
    exit 0
fi
echo "argus-full-smoke: ${FAILURES} FAILURE(S)"
exit 1
