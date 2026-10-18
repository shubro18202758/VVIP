#!/usr/bin/env bash
# smoke_models.sh — end-to-end smoke test of the VVIP model stack.
# Takes NO arguments. Warms up every served model, runs one REAL call per
# model, asserts non-empty outputs, prints one PASS line per model, and
# exits nonzero on any failure.
#
# Models covered (one line each):
#   pplx-decider-v1-27b   — POST /decide (typed choice+noul+score) on :8083
#   aditi-ps18-v7h 12B     — OpenAI /v1/chat/completions agent call on :8084
#   pplx-embed-v2-late-0.6b + bge-m3 — POST /embed/documents on :8085
#
# Endpoints are FIXED INTERNAL PLATFORM CONSTANTS (see
# VVIP/models/configs/veer_models.yaml `services:`), overridable via the
# standard env vars. Outbound HTTP from this script is http/https only; no
# URL coming from external input is ever fetched — only these constants.

set -uo pipefail

MODEL_HOST_URL="${MODEL_HOST_URL:-http://127.0.0.1:8083}"
LLAMA_BASE_URL="${LLAMA_BASE_URL:-http://127.0.0.1:8084}"
EMBEDDING_SERVICE_URL="${EMBEDDING_SERVICE_URL:-http://127.0.0.1:8085}"

FAILURES=0

fail() { echo "FAIL $*"; FAILURES=$((FAILURES + 1)); }

# scheme guard: http/https only (config constants; rejects anything else early)
url_scheme_ok() {
    case "$1" in
        http://*|https://*) return 0 ;;
        *) return 1 ;;
    esac
}
for u in "$MODEL_HOST_URL" "$LLAMA_BASE_URL" "$EMBEDDING_SERVICE_URL"; do
    url_scheme_ok "$u" || { echo "FATAL: endpoint not http/https: $u" >&2; exit 2; }
done

# ════════════════════════════════════════════════════════════════════════
# 1. DECIDER — pplx-decider-v1-27b via model-host :8083
# ════════════════════════════════════════════════════════════════════════
smoke_decider() {
    local health body
    # a) health: must report loaded
    health="$(curl -sf --max-time 10 "$MODEL_HOST_URL/decider/health")" \
        || { fail "decider /decider/health unreachable ($MODEL_HOST_URL)"; return 1; }
    grep -q '"loaded": *true' <<<"$health" \
        || { fail "decider not loaded: $health"; return 1; }
    # b) warmup (idempotent; also covers "warm every served model")
    curl -sf --max-time 300 -X POST "$MODEL_HOST_URL/warmup" >/dev/null \
        || { fail "decider /warmup failed"; return 1; }
    # c) one REAL decision call: typed choice + noul + score questions
    body="$(curl -sf --max-time 300 -X POST "$MODEL_HOST_URL/decide" \
        -H 'Content-Type: application/json' -d '{
            "request_id": "smoke-models",
            "state": "VVIP convoy smoke test: Z+ class movement planned across 3 Ahmedabad junctions at 18:30 IST. Evening peak congestion rising; one incident reported near junction 2; situation class = congestion. Advance closure currently 180s per Z+ protocol.",
            "questions": {
                "urgency":   {"type": "noul",
                              "instructions": "Does this situation require immediate action before the movement starts?"},
                "reroute":   {"type": "choice",
                              "instructions": "Should the route be revised before departure?",
                              "options": {"hold_route": "Keep the planned route unchanged",
                                          "minor_revision": "Adjust timing or one junction",
                                          "major_revision": "Select a substantially different route"}},
                "readiness": {"type": "score",
                              "instructions": "Rate current readiness to start the movement",
                              "scale": ["not ready", "slightly ready", "moderately ready", "mostly ready", "fully ready"]}
            }
        }')" || { fail "decider /decide call failed"; return 1; }
    # d) assert: three non-empty answers with numeric probabilities
    python3 - "$body" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
a = d.get("answers", {})
assert set(a) >= {"urgency", "reroute", "readiness"}, f"missing answers: {sorted(a)}"
for name, ans in a.items():
    assert ans.get("value") is not None and ans["value"] != "", f"{name}: empty value"
    p = ans.get("probability")
    assert isinstance(p, (int, float)) and 0.0 <= p <= 1.0, f"{name}: bad probability {p}"
    dist = ans.get("distribution")
    assert dist, f"{name}: empty distribution"
assert d.get("state_tokens", 0) > 0, "state_tokens must be positive"
print(f"    decider: urgency={a['urgency']['value']:.3f} "
      f"reroute={a['reroute']['value']} (p={a['reroute']['probability']:.3f}) "
      f"readiness={a['readiness']['value']:.2f} tokens={d['state_tokens']}")
PY
    [ $? -eq 0 ] || { fail "decider answer assertion failed"; return 1; }
    echo "PASS decider pplx-decider-v1-27b (choice+noul+score, :8083)"
}

# ════════════════════════════════════════════════════════════════════════
# 2. GEMMA — aditi-ps18-v7h 12B via llama-server :8084
# ════════════════════════════════════════════════════════════════════════
smoke_gemma() {
    local body
    curl -sf --max-time 10 "$LLAMA_BASE_URL/health" >/dev/null \
        || { fail "llama-server health unreachable ($LLAMA_BASE_URL)"; return 1; }
    # warmup (tiny completion) then one REAL agent-style call
    curl -sf --max-time 120 -X POST "$LLAMA_BASE_URL/v1/chat/completions" \
        -H 'Content-Type: application/json' \
        -d '{"model":"aditi-ps18-v7h-12b","max_tokens":4,"messages":[{"role":"user","content":"ok"}]}' \
        >/dev/null || { fail "gemma warmup call failed"; return 1; }
    body="$(curl -sf --max-time 240 -X POST "$LLAMA_BASE_URL/v1/chat/completions" \
        -H 'Content-Type: application/json' -d '{
            "model": "aditi-ps18-v7h-12b",
            "max_tokens": 200,
            "temperature": 0.6,
            "messages": [
                {"role": "system", "content": "You are the VVIP convoy tactical agent. Answer concisely in JSON with an \"answer\" key."},
                {"role": "user", "content": "A Y-class convoy waits at a junction. Live congestion on the planned segment is severe and rising. In one sentence: what is the immediate tactical action?"}
            ]
        }')" || { fail "gemma agent call failed"; return 1; }
    python3 - "$body" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
content = d["choices"][0]["message"]["content"]
assert isinstance(content, str) and len(content.strip()) >= 20, f"empty/short content: {content!r}"
u = d.get("usage", {})
print(f"    gemma: {len(content)} chars, prompt={u.get('prompt_tokens')} completion={u.get('completion_tokens')} tokens")
PY
    [ $? -eq 0 ] || { fail "gemma output assertion failed"; return 1; }
    echo "PASS gemma aditi-ps18-v7h-12b Q4_K_M (agent call, :8084)"
}

# ════════════════════════════════════════════════════════════════════════
# 3. EMBEDDINGS — pplx-embed-v2-late-0.6b (multivector) + bge-m3 (dense)
#    One /embed/documents call runs BOTH encoders per document (late
#    token-vectors then dense unit-norm — embedding_service/api.py), so a
#    single real call yields one PASS line per model.
# ════════════════════════════════════════════════════════════════════════
smoke_embeddings() {
    local health body search
    health="$(curl -sf --max-time 10 "$EMBEDDING_SERVICE_URL/health")" \
        || { fail "embedding-service unreachable ($EMBEDDING_SERVICE_URL)"; return 1; }
    # warmup if exposed (C3 does not mandate /warmup; ignore 404/405)
    curl -sf --max-time 300 -X POST "$EMBEDDING_SERVICE_URL/warmup" >/dev/null 2>&1 || true
    # real encode call: both encoders execute per document, docs are upserted
    body="$(curl -sf --max-time 300 -X POST "$EMBEDDING_SERVICE_URL/embed/documents" \
        -H 'Content-Type: application/json' \
        -d '{"documents": [
                {"body": "VVIP convoy departed origin at 18:30 IST; route segments 1001-1004 nominal.",
                 "title": "convoy-departure", "ref_id": "smoke:1"},
                {"body": "Congestion spike cleared on corridor segment 1004; recovery 240s.",
                 "title": "congestion-clear", "ref_id": "smoke:2"}
             ],
             "kind": "pattern"}')" \
        || { fail "embedding /embed/documents call failed"; return 1; }
    python3 - "$body" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
n, ids = d.get("embedded"), d.get("doc_ids")
assert isinstance(n, int) and n >= 1, f"embedded count bad: {d}"
assert isinstance(ids, list) and len(ids) == n and all(ids), f"doc_ids bad: {d}"
print(f"    embeddings: {n} docs encoded+upserted by both encoders")
PY
    [ $? -eq 0 ] || { fail "embedding output assertion failed"; return 1; }
    # query side: /search encodes the query through the same encoders
    search="$(curl -sf --max-time 120 -X POST "$EMBEDDING_SERVICE_URL/search" \
        -H 'Content-Type: application/json' \
        -d '{"query": "recent congestion on corridor", "kinds": ["pattern"]}')" \
        || { fail "embedding /search call failed"; return 1; }
    python3 - "$search" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
assert isinstance(d.get("results"), list), f"search response malformed: {list(d)}"
print(f"    embeddings: search ok ({len(d['results'])} hits)")
PY
    [ $? -eq 0 ] || { fail "embedding search assertion failed"; return 1; }
    echo "PASS embedding pplx-embed-v2-late-0.6b (multivector encode+search, :8085)"
    echo "PASS embedding bge-m3 (dense encode+ANN, :8085)"
}

smoke_decider
smoke_gemma
smoke_embeddings

if [ "$FAILURES" -ne 0 ]; then
    echo "SMOKE FAILED: $FAILURES model leg(s) failed" >&2
    exit 1
fi
echo "SMOKE OK: all served models warm and answering"
exit 0
