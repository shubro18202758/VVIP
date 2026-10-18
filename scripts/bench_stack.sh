#!/usr/bin/env bash
# bench_stack.sh — benchmark the LIVE VVIP model stack (round 2).
#
# Measures, against the running hosts (infra/scripts/serve_models.sh):
#   1. decider   pplx-decider-v1-27b  :8083  — tokens/sec + p99 latency
#              over 12 identical representative /decide calls
#              (choice + noul + score questions, smoke_models.sh shape).
#   2. gemma     aditi-ps18-v7h 12B    :8084  — generation tokens/sec on an
#              agent chat call (median of 3 runs of llama.cpp's own
#              `timings.predicted_per_second`, which excludes prompt eval).
#   3. embedding :8085 /embed/documents — median client wall latency (ms)
#              over 9 calls (both encoders: pplx-embed-v2-late + bge-m3).
#   4. resident memory — sum of nvidia-smi compute-app used_memory for the
#              whole model stack (GiB; nvidia-smi reports MiB).
#   5. adversarial suite — tests/adversarial, headless standard run
#              (no --stress; convoy-brain :8080 + model-host :8083 driven,
#              traffic-oracle downtime is recorded by the suite, not fatal).
#              passed/total from the suite's own JSON report; score =
#              100 * passed / total.
#
# Metric definitions (documented so the numbers are reproducible):
#   decisionTokSec  = mean over the 12 calls of state_tokens/(latency_ms/1000).
#                     state_tokens is the TOTAL padded-batch input tokens of
#                     the forward pass (autojev model.py prepare():
#                     attention_mask.sum()) and latency_ms is the server-side
#                     forward-pass time (model_host/decider_runtime.py
#                     _decide_sync) — i.e. true model throughput, not HTTP.
#   decisionP99Ms   = nearest-rank p99 of the CLIENT-observed end-to-end
#                     wall time of the same 12 /decide calls.
#   gemmaTokSec     = median of 3 × timings.predicted_per_second (generation
#                     phase only; llama-server --metrics build reports it per
#                     call in the chat-completions response).
#   embedMs         = median client wall ms of 9 /embed/documents calls.
#
# Output: VVIP/scripts/bench_results.json with fields
#   round, decisionTokSec, gemmaTokSec, embedMs, decisionP99Ms, memGB,
#   scenarioScore, scenariosPassed, scenariosTotal   (round=2)
#
# Exit status: nonzero iff any served model (decider / gemma / embedding)
# was unreachable or never answered a real call. A low adversarial score is
# a measurement, not an outage: it is reported and does not fail the bench.
#
# Usage: bash VVIP/scripts/bench_stack.sh     (no arguments; env-overridable
#        endpoint vars follow smoke_models.sh conventions)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"                 # VVIP/
RESULTS_JSON="$SCRIPT_DIR/bench_results.json"
ADV_REPORT="$SCRIPT_DIR/bench_adversarial_report.json"       # suite's own report
GATE_PY="$PROJECT_ROOT/.venvs/gate/bin/python"               # venv running convoy-brain; has structlog+httpx

MODEL_HOST_URL="${MODEL_HOST_URL:-http://127.0.0.1:8083}"
LLAMA_BASE_URL="${LLAMA_BASE_URL:-http://127.0.0.1:8084}"
EMBEDDING_SERVICE_URL="${EMBEDDING_SERVICE_URL:-http://127.0.0.1:8085}"

DECIDER_CALLS="${DECIDER_CALLS:-12}"     # ask: 10+ calls
GEMMA_CALLS="${GEMMA_CALLS:-3}"
EMBED_CALLS="${EMBED_CALLS:-9}"
ADV_TIMEOUT_S="${ADV_TIMEOUT_S:-2400}"   # suite guard: 14 scenarios, up to 120s each, concurrency 3
ROUND=2

log()  { echo -e "\033[0;36m[bench]\033[0m $*"; }
warn() { echo -e "\033[1;33m[bench:WARN]\033[0m $*" >&2; }

# ══════════════════════════════════════════════════════════════════════
# Leg 1-3: model calls + timing (single python3 helper, stdlib only)
# Writes one JSON object to stdout. Emits "reachable": {...} per model.
# ══════════════════════════════════════════════════════════════════════
run_model_bench() {
python3 - "$MODEL_HOST_URL" "$LLAMA_BASE_URL" "$EMBEDDING_SERVICE_URL" \
          "$DECIDER_CALLS" "$GEMMA_CALLS" "$EMBED_CALLS" <<'PY'
import json, statistics, sys, time, urllib.request, urllib.error

model_host, llama_base, embed_base = sys.argv[1:4]
n_decider, n_gemma, n_embed = (int(x) for x in sys.argv[4:7])

def post(url, payload, timeout):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read())
    return body, (time.perf_counter() - t0) * 1000.0  # body, wall_ms

def get(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())

out = {"reachable": {"decider": False, "gemma": False, "embedding": False}}

# ── decider: health, warmup, 12 representative decision calls ──────────
DECIDE_PAYLOAD = {
    "request_id": "bench-stack",
    "state": ("VVIP convoy benchmark: Z+ class movement planned across 3 "
              "Ahmedabad junctions at 18:30 IST. Evening peak congestion "
              "rising; one incident reported near junction 2; situation "
              "class = congestion. Advance closure currently 180s per Z+ "
              "protocol."),
    "questions": {
        "urgency": {"type": "noul",
                    "instructions": "Does this situation require immediate action before the movement starts?"},
        "reroute": {"type": "choice",
                    "instructions": "Should the route be revised before departure?",
                    "options": {"hold_route": "Keep the planned route unchanged",
                                "minor_revision": "Adjust timing or one junction",
                                "major_revision": "Select a substantially different route"}},
        "readiness": {"type": "score",
                      "instructions": "Rate current readiness to start the movement",
                      "scale": ["not ready", "slightly ready", "moderately ready",
                                "mostly ready", "fully ready"]},
    },
}
try:
    h = get(f"{model_host}/decider/health", 10)
    if not h.get("loaded"):
        raise RuntimeError(f"decider not loaded: {h}")
    try:
        post(f"{model_host}/warmup", {}, 300)          # idempotent
    except Exception:
        pass                                            # warm once at boot; ignore
    tok_sec, wall_ms = [], []
    for _ in range(n_decider):
        body, ms = post(f"{model_host}/decide", DECIDE_PAYLOAD, 300)
        if not body.get("answers"):
            raise RuntimeError(f"decider returned no answers: {body}")
        tok_sec.append(body["state_tokens"] / (body["latency_ms"] / 1000.0))
        wall_ms.append(ms)
    out["reachable"]["decider"] = True
    out["decisionTokSec"] = round(statistics.mean(tok_sec), 2)
    wall_sorted = sorted(wall_ms)                       # nearest-rank p99
    idx = max(0, min(len(wall_sorted) - 1,
                     int(0.99 * len(wall_sorted) + 0.999999) - 1))
    out["decisionP99Ms"] = round(wall_sorted[idx], 1)
    out["decisionCalls"] = n_decider
except Exception as e:
    print(f"decider bench failed: {e}", file=sys.stderr)

# ── gemma: health warm, then agent chat calls (smoke_models.sh shape) ──
GEMMA_PAYLOAD = {
    "model": "aditi-ps18-v7h-12b",
    "max_tokens": 200,
    "temperature": 0.6,
    "messages": [
        {"role": "system", "content": 'You are the VVIP convoy tactical agent. Answer concisely in JSON with an "answer" key.'},
        {"role": "user", "content": "A Y-class convoy waits at a junction. Live congestion on the planned segment is severe and rising. In one sentence: what is the immediate tactical action?"},
    ],
}
try:
    post(f"{llama_base}/v1/chat/completions",
         {"model": "aditi-ps18-v7h-12b", "max_tokens": 4,
          "messages": [{"role": "user", "content": "ok"}]}, 120)   # warm
    rates = []
    for _ in range(n_gemma):
        body, _ms = post(f"{llama_base}/v1/chat/completions", GEMMA_PAYLOAD, 240)
        content = body["choices"][0]["message"]["content"]
        if not isinstance(content, str) or len(content.strip()) < 20:
            raise RuntimeError(f"gemma empty/short content: {content!r}")
        t = (body.get("timings") or {}).get("predicted_per_second")
        if not t or t <= 0:
            raise RuntimeError(f"gemma timings.predicted_per_second missing: {body.get('timings')}")
        rates.append(t)
    out["reachable"]["gemma"] = True
    out["gemmaTokSec"] = round(statistics.median(rates), 2)
    out["gemmaCalls"] = n_gemma
except Exception as e:
    print(f"gemma bench failed: {e}", file=sys.stderr)

# ── embedding: health, then /embed/documents calls (both encoders) ─────
EMBED_PAYLOAD = {
    "documents": [
        {"body": "VVIP convoy departed origin at 18:30 IST; route segments 1001-1004 nominal.",
         "title": "convoy-departure", "ref_id": "bench:1"},
        {"body": "Congestion spike cleared on corridor segment 1004; recovery 240s.",
         "title": "congestion-clear", "ref_id": "bench:2"},
    ],
    "kind": "pattern",  # ref_ids distinct from smoke:* ids
}
try:
    get(f"{embed_base}/health", 10)
    try:
        post(f"{embed_base}/warmup", {}, 300)
    except Exception:
        pass                                            # 404/405 acceptable
    ms_all = []
    for _ in range(n_embed):
        body, ms = post(f"{embed_base}/embed/documents", EMBED_PAYLOAD, 300)
        if not isinstance(body.get("embedded"), int) or body["embedded"] < 1:
            raise RuntimeError(f"embedding response bad: {body}")
        ms_all.append(ms)
    out["reachable"]["embedding"] = True
    out["embedMs"] = round(statistics.median(ms_all), 1)
    out["embedCalls"] = n_embed
except Exception as e:
    print(f"embedding bench failed: {e}", file=sys.stderr)

json.dump(out, sys.stdout)
PY
}

# ══════════════════════════════════════════════════════════════════════
# Leg 4: resident memory of the whole model stack (nvidia-smi, GiB)
# ══════════════════════════════════════════════════════════════════════
measure_mem_gib() {
    local mib total=0
    while IFS=, read -r _pid _name mem; do
        mem="${mem// /}"
        [[ "$mem" =~ ^[0-9]+$ ]] && total=$(( total + mem ))
    done < <(nvidia-smi --query-compute-apps=pid,process_name,used_memory \
                 --format=csv,noheader,nounits 2>/dev/null)
    if [ "$total" -eq 0 ]; then
        warn "nvidia-smi reported no compute-app memory"
        echo 0
    else
        python3 -c "print(round($total / 1024.0, 2))"
    fi
}

# ══════════════════════════════════════════════════════════════════════
# Leg 5: adversarial scenario suite — headless standard run
# ══════════════════════════════════════════════════════════════════════
run_adversarial() {
    rm -f "$ADV_REPORT"
    if [ ! -x "$GATE_PY" ]; then
        warn "gate venv missing ($GATE_PY) — adversarial suite not run"
        return
    fi
    log "running tests/adversarial (headless standard run, timeout ${ADV_TIMEOUT_S}s)…"
    ( cd "$PROJECT_ROOT" && timeout "$ADV_TIMEOUT_S" "$GATE_PY" -m tests.adversarial \
        --output "$ADV_REPORT" ) > "$SCRIPT_DIR/bench_adversarial.log" 2>&1
    local rc=$?
    # rc!=0 means the suite's own pass-rate gate tripped (or timeout) —
    # the numbers below are still whatever the report recorded, if any.
    [ $rc -eq 0 ] || warn "tests.adversarial exited rc=$rc (see $SCRIPT_DIR/bench_adversarial.log)"
    if [ ! -f "$ADV_REPORT" ]; then
        warn "no adversarial report produced"
    fi
}

log "=== VVIP stack benchmark (round $ROUND) ==="
log "endpoints: decider=$MODEL_HOST_URL gemma=$LLAMA_BASE_URL embed=$EMBEDDING_SERVICE_URL"

MODEL_JSON="$(run_model_bench)"
log "model bench: $MODEL_JSON"
if [ -z "$MODEL_JSON" ]; then
    echo "FATAL: model bench produced no JSON" >&2
    MODEL_JSON='{"reachable":{}}'
fi

MEM_GIB="$(measure_mem_gib)"
log "stack resident memory (nvidia-smi compute apps): ${MEM_GIB} GiB"

run_adversarial

# ── merge into bench_results.json ──────────────────────────────────────
RESULTS="$(python3 - "$MODEL_JSON" "$MEM_GIB" "$ADV_REPORT" "$ROUND" <<'PY'
import json, sys
model_json, mem_gib, adv_path, round_no = sys.argv[1:5]
try:
    m = json.loads(model_json)
except json.JSONDecodeError:
    m = {"reachable": {}}

passed = total = 0
try:
    with open(adv_path) as f:
        adv = json.load(f)
    passed = int(adv.get("passed", 0))
    total = int(adv.get("total_scenarios", 0))
except Exception:
    pass
score = round(100.0 * passed / total, 1) if total else 0.0

results = {
    "round": int(round_no),
    "decisionTokSec": m.get("decisionTokSec", 0),
    "gemmaTokSec": m.get("gemmaTokSec", 0),
    "embedMs": m.get("embedMs", 0),
    "decisionP99Ms": m.get("decisionP99Ms", 0),
    "memGB": float(mem_gib),
    "scenarioScore": score,
    "scenariosPassed": passed,
    "scenariosTotal": total,
}
json.dump(results, sys.stdout, indent=2)
PY
)"
printf '%s\n' "$RESULTS" > "$RESULTS_JSON"
log "wrote $RESULTS_JSON:"
cat "$RESULTS_JSON"

# ── exit nonzero iff any model was unreachable ─────────────────────────
UNREACHABLE=0
for leg in decider gemma embedding; do
    if ! python3 -c "import json,sys; sys.exit(0 if json.loads('''$MODEL_JSON''').get('reachable',{}).get('$leg') else 1)"; then
        echo "MODEL UNREACHABLE: $leg" >&2
        UNREACHABLE=1
    fi
done
if [ "$UNREACHABLE" -ne 0 ]; then
    echo "BENCH FAILED: model(s) unreachable" >&2
    exit 1
fi
log "BENCH OK"
exit 0
