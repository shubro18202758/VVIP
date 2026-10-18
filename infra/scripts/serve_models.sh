#!/usr/bin/env bash
# serve_models.sh — Boot ALL resident model hosts for the VVIP stack.
# Owner: models stream (C10). Targets the NVIDIA GB10 unified-memory pool
# (121.44 GiB — /proc/meminfo is the source of truth; nvidia-smi reports
# memory.total as N/A on GB10). Full budget: VVIP/models/MEMORY_BUDGET.md.
#
# Usage:
#   serve_models.sh start    # build venvs if missing, then start hosts (default)
#   serve_models.sh build    # only build the persistent venvs
#   serve_models.sh stop     # stop all three hosts (never touches ollama :11434)
#   serve_models.sh status   # health of every host
#
# Hosts (fixed internal endpoints of the platform — see
# VVIP/models/configs/veer_models.yaml `services:`; env-overridable there):
#   :8083  model-host        pplx-decider-v1-27b (T5 strategic, 48.58 GiB weights)
#   :8084  llama-server      aditi-ps18-v7h Gemma 4 12B Q4_K_M (T4 tactical,
#                            2 slots × 8192 ctx, --jinja = embedded Gemma 4
#                            canonical chat template, --metrics Prometheus)
#   :8085  embedding-service pplx-embed-v2-late-0.6b + bge-m3 (T1; app owned by
#                            the embeddings stream — started here when present)
#
# KEEP_ALIVE POLICY (GB10 unified pool — nothing is ever evicted):
#   - llama-server has no model-eviction: the process is supervised with
#     restart-on-exit instead.
#   - model-host loads once at boot (~285 s cold) and stays resident.
#   - ollama (debug side-load gemma-vvip:12b) must run with OLLAMA_KEEP_ALIVE=-1.
#   - ollama :11434 is externally active and is NEVER started/stopped here.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"           # VVIP/
# Runtime state (logs / venvs / pidfiles) defaults under the shared models
# dir — reached through the VVIP/models symlink. VEER_LOG_DIR /
# VEER_VENV_DIR / VEER_RUN_DIR let a workspace-scoped run (e.g. the argus
# tree, which must not write into the shared original stack) keep every
# runtime write inside its own workspace while still READING the shared
# venvs and weights.
LOG_DIR="${VEER_LOG_DIR:-$PROJECT_ROOT/models/logs}"
VENV_DIR="${VEER_VENV_DIR:-$PROJECT_ROOT/models/.venvs}"
RUN_DIR="${VEER_RUN_DIR:-$PROJECT_ROOT/models/.run}"
mkdir -p "$LOG_DIR" "$VENV_DIR" "$RUN_DIR"

# ─── fixed platform constants (not user-supplied URLs) ──────────────────────
MODEL_HOST_PORT="${MODEL_HOST_PORT:-8083}"
LLAMA_PORT="${LLAMA_PORT:-8084}"
EMBEDDING_PORT="${EMBEDDING_PORT:-8085}"
DECIDER_SNAPSHOT="${DECIDER_SNAPSHOT_PATH:-/home/sandy050405/.cache/huggingface/hub/models--perplexity-ai--pplx-decider-v1-27b/snapshots/b01a5cbaca5391f73bd55103d4f27e8982cd5e60}"
LLAMA_BIN="${LLAMA_SERVER_BIN:-/home/sandy050405/llama.cpp/build/bin/llama-server}"
GEMMA_GGUF="${GEMMA_GGUF:-/home/sandy050405/models/q4-stack/v7h-merged-Q4_K_M.gguf}"
CTX_PER_SLOT="${GEMMA_CTX_PER_SLOT:-8192}"
SLOTS="${GEMMA_PARALLEL_SLOTS:-2}"

log()  { echo -e "\033[0;32m[serve_models]\033[0m $*"; }
warn() { echo -e "\033[1;33m[serve_models:WARN]\033[0m $*" >&2; }

pid_file() { echo "$RUN_DIR/$1.pid"; }

is_running() {  # <pidfile>
    [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null
}

wait_http() {   # <url> <name> <timeout_s>
    local deadline=$(( $(date +%s) + ${3:-600} ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if curl -sf --max-time 3 "$1" >/dev/null 2>&1; then return 0; fi
        sleep 3
    done
    warn "$2 did not become healthy in ${3:-600}s"
    return 1
}

# ─── venv builds (persistent — /tmp venvs are forbidden; they vanish) ───────
build_venvs() {
    if [ ! -x "$VENV_DIR/model-host/bin/python" ]; then
        log "Building model-host venv (pinned torch/transformers stack)..."
        python3.12 -m venv "$VENV_DIR/model-host"
        "$VENV_DIR/model-host/bin/pip" install --disable-pip-version-check \
            -r "$PROJECT_ROOT/models/model-host-requirements.txt"
    else
        log "model-host venv present"
    fi
    if [ ! -x "$VENV_DIR/migrate/bin/python" ]; then
        log "Building migrate venv (psycopg)..."
        python3.12 -m venv "$VENV_DIR/migrate"
        "$VENV_DIR/migrate/bin/pip" install --disable-pip-version-check \
            "psycopg[binary]>=3.2"
    else
        log "migrate venv present"
    fi
    # embedding-service app is owned by the embeddings stream; install its
    # package + `models` extra (torch stack pins) so :8085 can boot from here.
    local emb_pkg="$PROJECT_ROOT/services/embedding-service"
    if [ -f "$emb_pkg/pyproject.toml" ] && [ ! -x "$VENV_DIR/embedding/bin/python" ]; then
        log "Building embedding-service venv (embeddings stream pkg + models extra)..."
        python3.12 -m venv "$VENV_DIR/embedding"
        "$VENV_DIR/embedding/bin/pip" install --disable-pip-version-check \
            "$emb_pkg[models]"
    fi
}

# ─── supervised start: restart-on-exit loop in a detached subshell ──────────
supervise() {  # <name> <pidfile> <cmd...>
    local name="$1" pidfile="$2"; shift 2
    if is_running "$pidfile"; then log "$name already running (pid $(cat "$pidfile"))"; return 0; fi
    (
        # The server under supervision exits nonzero on crash; errexit from
        # the outer `set -euo pipefail` must NOT kill this restart loop.
        set +e
        while true; do
            "$@" >> "$LOG_DIR/$name.log" 2>&1
            code=$?
            echo "$(date -Is) $name exited code=$code — restarting in 5s" >> "$LOG_DIR/$name.log"
            sleep 5
        done
    ) &
    echo $! > "$pidfile"
    log "$name supervised (supervisor pid $(cat "$pidfile"))"
}

start_model_host() {
    supervise "model-host-8083" "$(pid_file model-host)" \
        env PYTHONPATH="$PROJECT_ROOT/services/model-host/src" \
        DECIDER_SNAPSHOT_PATH="$DECIDER_SNAPSHOT" \
        MODEL_HOST_PORT="$MODEL_HOST_PORT" \
        HF_HUB_OFFLINE=1 \
        "$VENV_DIR/model-host/bin/python" -m model_host.app
    log "model-host :$MODEL_HOST_PORT loading 27B decider (cold load ≈285 s)..."
    wait_http "http://127.0.0.1:$MODEL_HOST_PORT/health" "model-host" 420 || true
}

start_llama() {
    [ -x "$LLAMA_BIN" ] || { warn "llama-server binary missing: $LLAMA_BIN"; return 1; }
    [ -f "$GEMMA_GGUF" ] || { warn "Gemma GGUF missing: $GEMMA_GGUF"; return 1; }
    local ctx_total=$(( CTX_PER_SLOT * SLOTS ))
    supervise "llama-server-$LLAMA_PORT" "$(pid_file llama-server)" \
        "$LLAMA_BIN" -m "$GEMMA_GGUF" --alias aditi-ps18-v7h-12b \
        --host 127.0.0.1 --port "$LLAMA_PORT" \
        -c "$ctx_total" -np "$SLOTS" --cont-batching --metrics --jinja
    wait_http "http://127.0.0.1:$LLAMA_PORT/health" "llama-server" 300 || true
}

start_embedding() {
    # App owned by the embeddings stream (C3). TEMPORARY: their
    # `python -m embedding_service` entrypoint is broken on uvicorn 0.54
    # (async factory never awaited → 500 on every request), so we boot the
    # same app through the models-owned adapter models/embedding_launcher.py
    # (pool + encoders initialized on the serving loop). Delete the adapter
    # and restore `-m embedding_service` once they fix the entrypoint.
    #
    # DSN: the app is env-only for credentials (db.py reads VVIP_DATABASE_URL
    # et al.). Point it at the host-published postgis port — the same
    # canonical host DSN infra/compose.gb10-override.yml gives the
    # host-networked containers, so every /embed/documents write reaches
    # corridor_db instead of dying on pool acquire (observed: asyncpg
    # InvalidPasswordError when the caller's shell exported a stale
    # migration-test DSN). POSTGIS_PASSWORD stays env-overridable, matching
    # compose's ${POSTGIS_PASSWORD:-dev_changeme}.
    local app="$PROJECT_ROOT/services/embedding-service"
    local launcher="$PROJECT_ROOT/models/embedding_launcher.py"
    if [ -f "$app/src/embedding_service/__main__.py" ] && [ -x "$VENV_DIR/embedding/bin/python" ] && [ -f "$launcher" ]; then
        supervise "embedding-$EMBEDDING_PORT" "$(pid_file embedding)" \
            env EMBEDDING_LISTEN_PORT="$EMBEDDING_PORT" \
            VVIP_DATABASE_URL="${EMBEDDING_DATABASE_URL:-postgresql://corridor_admin:${POSTGIS_PASSWORD:-dev_changeme}@127.0.0.1:${POSTGIS_HOST_PORT:-55434}/corridor_db}" \
            "$VENV_DIR/embedding/bin/python" "$launcher"
        wait_http "http://127.0.0.1:$EMBEDDING_PORT/health" "embedding-service" 300 || true
    else
        warn "embedding-service not startable (app missing or venv unbuilt — run: $0 build)" \
             "; :$EMBEDDING_PORT stays down."
    fi
}

stop_host() {  # <pidfile> <name>
    local pidfile="$1" name="$2"
    if is_running "$pidfile"; then
        local pid; pid="$(cat "$pidfile")"
        # the supervisor subshell wraps the actual server; kill the tree
        pkill -TERM -P "$pid" 2>/dev/null || true
        kill -TERM "$pid" 2>/dev/null || true
        # also catch the actual server process by port marker in its cmdline
        local child
        for child in $(pgrep -P "$pid" 2>/dev/null); do kill -TERM "$child" 2>/dev/null || true; done
        rm -f "$pidfile"
        log "$name stopped"
    else
        log "$name not running"
    fi
}

cmd_start() {
    build_venvs
    start_llama
    start_model_host
    start_embedding
    cmd_status
}

cmd_stop() {
    stop_host "$(pid_file llama-server)" "llama-server"
    stop_host "$(pid_file model-host)" "model-host"
    stop_host "$(pid_file embedding)" "embedding-service"
    log "note: ollama :11434 left untouched (externally active)"
}

cmd_status() {
    local rc=0
    for spec in "model-host:$MODEL_HOST_PORT" "llama-server:$LLAMA_PORT" "embedding-service:$EMBEDDING_PORT"; do
        local name="${spec%%:*}" port="${spec##*:}"
        if curl -sf --max-time 3 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
            log "$name  :$port  UP"
        else
            warn "$name  :$port  DOWN"; rc=1
        fi
    done
    return $rc
}

case "${1:-start}" in
    start)   cmd_start ;;
    build)   build_venvs ;;
    stop)    cmd_stop ;;
    status)  cmd_status ;;
    *) echo "usage: $0 {start|build|stop|status}" >&2; exit 2 ;;
esac
