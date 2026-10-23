#!/usr/bin/env bash
# serve_models.sh — Boot ALL resident model hosts for the VVIP stack.
# Owner: models stream (C10). Targets the NVIDIA GB10 unified-memory pool
# (121.44 GiB — /proc/meminfo is the source of truth; nvidia-smi reports
# memory.total as N/A on GB10). Full budget: VVIP/models-config/MEMORY_BUDGET.md.
#
# Usage:
#   serve_models.sh start                 # build venvs if missing, then start hosts (default)
#   serve_models.sh build                 # only build the persistent venvs
#   serve_models.sh stop                  # stop all three hosts (never touches ollama :11434)
#   serve_models.sh status                # health of every host (+ who supervises it)
#   serve_models.sh --foreground <svc>    # exec ONE host in the foreground — the
#                                         # ExecStart of the systemd --user units
#                                         # (svc: llama | decider | embed)
#
# Supervision. On the argus host the three hosts are systemd --user units
# (infra/systemd/: veer-llama, veer-decider, veer-embed under
# veer-models.target, Restart=always, EnvironmentFile=~/.config/veer/veer.env)
# that run `--foreground`; they survive reboots via veer.target + linger.
# `start`/`stop` are the legacy bash restart-loop supervisor for hosts
# without systemd units; they refuse to fight the units when those are active.
#
# Hosts (fixed internal endpoints of the platform — see
# VVIP/models-config/configs/veer_models.yaml `services:`; env-overridable there):
#   :8083  model-host        pplx-decider-v1-27b (T5 strategic, 48.58 GiB weights)
#   :8084  llama-server      aditi-ps18-v7h Gemma 4 12B Q4_K_M (T4 tactical,
#                            2 slots × 8192 ctx, --jinja = embedded Gemma 4
#                            canonical chat template, --metrics Prometheus)
#   :8085  embedding-service pplx-embed-v2-late-0.6b + bge-m3 (T1; app owned by
#                            the embeddings stream — started here when present)
#
# Files. Non-weight configs are tracked in VVIP/models-config/ (configs/,
# embedding_launcher.py, model-host-requirements.txt). Weights, binaries and
# venvs are explicit host paths: DECIDER_SNAPSHOT_PATH, LLAMA_SERVER_BIN,
# GEMMA_GGUF, BGE_M3_PATH, VEER_VENV_DIR (scripts/veer_env.sh loads them from
# ~/.config/veer/veer.env; explicitly exported values win).
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

# Host environment (~/.config/veer/veer.env + stack defaults).
if [ -f "$PROJECT_ROOT/scripts/veer_env.sh" ]; then
    # shellcheck source=scripts/veer_env.sh
    . "$PROJECT_ROOT/scripts/veer_env.sh"
fi

# Runtime state (logs / venvs / pidfiles). VEER_LOG_DIR / VEER_VENV_DIR /
# VEER_RUN_DIR keep every runtime write inside the workspace (git-ignored
# .run-models/ and .venvs/) — nothing is written into the retired tree.
LOG_DIR="${VEER_LOG_DIR:-$PROJECT_ROOT/.run-models/logs}"
VENV_DIR="${VEER_VENV_DIR:-$PROJECT_ROOT/.venvs}"
RUN_DIR="${VEER_RUN_DIR:-$PROJECT_ROOT/.run-models}"
MODELS_CONFIG_DIR="${VEER_MODELS_CONFIG_DIR:-$PROJECT_ROOT/models-config}"
MODELS_CONFIG="${VEER_MODELS_CONFIG:-$MODELS_CONFIG_DIR/configs/veer_models.yaml}"

# ─── fixed platform constants (not user-supplied URLs) ──────────────────────
MODEL_HOST_PORT="${MODEL_HOST_PORT:-8083}"
LLAMA_PORT="${LLAMA_PORT:-8084}"
EMBEDDING_PORT="${EMBEDDING_PORT:-8085}"
POSTGIS_HOST_PORT="${POSTGIS_HOST_PORT:-56434}"
DECIDER_SNAPSHOT="${DECIDER_SNAPSHOT_PATH:-$HOME/.cache/huggingface/hub/models--perplexity-ai--pplx-decider-v1-27b/snapshots/b01a5cbaca5391f73bd55103d4f27e8982cd5e60}"
LLAMA_BIN="${LLAMA_SERVER_BIN:-$HOME/llama.cpp/build/bin/llama-server}"
GEMMA_GGUF="${GEMMA_GGUF:-$HOME/models/q4-stack/v7h-merged-Q4_K_M.gguf}"
CTX_PER_SLOT="${GEMMA_CTX_PER_SLOT:-8192}"
SLOTS="${GEMMA_PARALLEL_SLOTS:-2}"
EMBED_APP="$PROJECT_ROOT/services/embedding-service"
EMBED_LAUNCHER="$MODELS_CONFIG_DIR/embedding_launcher.py"

# Exit status for a host that cannot start because its files are missing:
# the systemd units set RestartPreventExitStatus=78 so a configuration error
# is reported once instead of restarting every 5 s.
EX_CONFIG=78

log()  { echo -e "\033[0;32m[serve_models]\033[0m $*"; }
warn() { echo -e "\033[1;33m[serve_models:WARN]\033[0m $*" >&2; }

pid_file() { echo "$RUN_DIR/$1.pid"; }

is_running() {  # <pidfile> — live AND still a serve_models supervisor
    [ -f "$1" ] || return 1
    local pid; pid="$(cat "$1" 2>/dev/null || true)"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    # a PID reused after a reboot must not read as "already running"
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q 'serve_models'
}

unit_of() {  # <host> → systemd --user unit name
    case "$1" in
        llama-server) echo veer-llama.service ;;
        model-host)   echo veer-decider.service ;;
        embedding)    echo veer-embed.service ;;
    esac
}

unit_active() {  # <host>
    command -v systemctl >/dev/null 2>&1 \
        && systemctl --user is-active --quiet "$(unit_of "$1")" 2>/dev/null
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

decider_state() {  # → loaded | loading | error:<msg> | down
    local body
    body="$(curl -sf --max-time 3 "http://127.0.0.1:$MODEL_HOST_PORT/decider/health" 2>/dev/null)" \
        || { echo down; return; }
    if grep -Eq '"loaded": *true' <<<"$body"; then echo loaded
    elif grep -Eq '"load_error": *"' <<<"$body"; then
        echo "error:$(sed -E 's/.*"load_error": *"([^"]*)".*/\1/' <<<"$body")"
    else echo loading
    fi
}

wait_decider_loaded() {  # <timeout_s> — /health answers 200 while still loading
    local deadline=$(( $(date +%s) + $1 )) state
    while [ "$(date +%s)" -lt "$deadline" ]; do
        state="$(decider_state)"
        case "$state" in
            loaded) return 0 ;;
            error:*) warn "decider load failed: ${state#error:}"; return 1 ;;
        esac
        sleep 5
    done
    warn "decider not loaded after ${1}s (state: $(decider_state))"
    return 1
}

wait_tcp() {  # <host> <port> <timeout_s>
    local deadline=$(( $(date +%s) + $3 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null; then return 0; fi
        sleep 2
    done
    return 1
}

need() {  # <test-flag> <path> <what> — fail with EX_CONFIG when missing
    if ! test "$1" "$2"; then
        warn "$3 missing: $2"
        return 1
    fi
}

embedding_dsn() {
    echo "${EMBEDDING_DATABASE_URL:-postgresql://corridor_admin:${POSTGIS_PASSWORD:-dev_changeme}@127.0.0.1:${POSTGIS_HOST_PORT}/corridor_db}"
}

# ─── venv builds (persistent — /tmp venvs are forbidden; they vanish) ───────
build_venvs() {
    mkdir -p "$VENV_DIR"
    if [ ! -x "$VENV_DIR/model-host/bin/python" ]; then
        log "Building model-host venv (pinned torch/transformers stack)..."
        python3.12 -m venv "$VENV_DIR/model-host"
        "$VENV_DIR/model-host/bin/pip" install --disable-pip-version-check \
            -r "$MODELS_CONFIG_DIR/model-host-requirements.txt"
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
    if [ -f "$EMBED_APP/pyproject.toml" ] && [ ! -x "$VENV_DIR/embedding/bin/python" ]; then
        log "Building embedding-service venv (embeddings stream pkg + models extra)..."
        python3.12 -m venv "$VENV_DIR/embedding"
        "$VENV_DIR/embedding/bin/pip" install --disable-pip-version-check \
            "${EMBED_APP}[models]"
    fi
}

# ─── foreground mode: one host, exec'd, for systemd ─────────────────────────
foreground() {  # <svc>
    case "${1:-}" in
        llama|llama-server)
            need -x "$LLAMA_BIN" "llama-server binary (LLAMA_SERVER_BIN)" || exit $EX_CONFIG
            need -f "$GEMMA_GGUF" "Gemma GGUF (GEMMA_GGUF)" || exit $EX_CONFIG
            log "llama-server :$LLAMA_PORT (foreground) — $GEMMA_GGUF"
            exec "$LLAMA_BIN" -m "$GEMMA_GGUF" --alias aditi-ps18-v7h-12b \
                --host 127.0.0.1 --port "$LLAMA_PORT" \
                -c "$(( CTX_PER_SLOT * SLOTS ))" -np "$SLOTS" --cont-batching --metrics --jinja
            ;;
        decider|model-host)
            need -x "$VENV_DIR/model-host/bin/python" "model-host venv (VEER_VENV_DIR)" || exit $EX_CONFIG
            need -d "$DECIDER_SNAPSHOT" "decider snapshot (DECIDER_SNAPSHOT_PATH)" || exit $EX_CONFIG
            need -f "$MODELS_CONFIG" "veer_models.yaml (VEER_MODELS_CONFIG)" || exit $EX_CONFIG
            log "model-host :$MODEL_HOST_PORT (foreground) — loading 27B decider (cold load ≈285 s)"
            exec env PYTHONPATH="$PROJECT_ROOT/services/model-host/src" \
                DECIDER_SNAPSHOT_PATH="$DECIDER_SNAPSHOT" \
                MODEL_HOST_PORT="$MODEL_HOST_PORT" \
                VEER_MODELS_CONFIG="$MODELS_CONFIG" \
                HF_HUB_OFFLINE=1 \
                "$VENV_DIR/model-host/bin/python" -m model_host.app
            ;;
        embed|embedding)
            need -f "$EMBED_APP/src/embedding_service/__main__.py" "embedding-service app" || exit $EX_CONFIG
            need -x "$VENV_DIR/embedding/bin/python" "embedding venv (VEER_VENV_DIR)" || exit $EX_CONFIG
            need -f "$EMBED_LAUNCHER" "embedding launcher" || exit $EX_CONFIG
            # user units cannot order After=docker.service: wait for the
            # corridor-store port instead (exit 1 → systemd retries)
            if ! wait_tcp 127.0.0.1 "$POSTGIS_HOST_PORT" 300; then
                warn "corridor-store 127.0.0.1:$POSTGIS_HOST_PORT not accepting after 300s"
                exit 1
            fi
            log "embedding-service :$EMBEDDING_PORT (foreground)"
            exec env EMBEDDING_LISTEN_PORT="$EMBEDDING_PORT" \
                VVIP_DATABASE_URL="$(embedding_dsn)" \
                "$VENV_DIR/embedding/bin/python" "$EMBED_LAUNCHER"
            ;;
        *) echo "usage: $0 --foreground {llama|decider|embed}" >&2; exit 2 ;;
    esac
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
    if unit_active model-host; then log "model-host managed by systemd ($(unit_of model-host))"; return 0; fi
    supervise "model-host-8083" "$(pid_file model-host)" \
        env PYTHONPATH="$PROJECT_ROOT/services/model-host/src" \
        DECIDER_SNAPSHOT_PATH="$DECIDER_SNAPSHOT" \
        MODEL_HOST_PORT="$MODEL_HOST_PORT" \
        VEER_MODELS_CONFIG="$MODELS_CONFIG" \
        HF_HUB_OFFLINE=1 \
        "$VENV_DIR/model-host/bin/python" -m model_host.app
    log "model-host :$MODEL_HOST_PORT loading 27B decider (cold load ≈285 s)..."
    wait_decider_loaded 420 || true
}

start_llama() {
    if unit_active llama-server; then log "llama-server managed by systemd ($(unit_of llama-server))"; return 0; fi
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
    if unit_active embedding; then log "embedding-service managed by systemd ($(unit_of embedding))"; return 0; fi
    # App owned by the embeddings stream (C3). TEMPORARY: their
    # `python -m embedding_service` entrypoint is broken on uvicorn 0.54
    # (async factory never awaited → 500 on every request), so we boot the
    # same app through the models-owned adapter models-config/embedding_launcher.py
    # (pool + encoders initialized on the serving loop). Delete the adapter
    # and restore `-m embedding_service` once they fix the entrypoint.
    #
    # DSN: the app is env-only for credentials (db.py reads VVIP_DATABASE_URL
    # et al.). Point it at the host-published postgis port (argus: 56434, the
    # canonical host DSN of infra/compose.argus-override.yml), so every
    # /embed/documents write reaches corridor_db instead of dying on pool
    # acquire. POSTGIS_PASSWORD stays env-overridable, matching compose's
    # ${POSTGIS_PASSWORD:-dev_changeme}.
    if [ -f "$EMBED_APP/src/embedding_service/__main__.py" ] && [ -x "$VENV_DIR/embedding/bin/python" ] && [ -f "$EMBED_LAUNCHER" ]; then
        supervise "embedding-$EMBEDDING_PORT" "$(pid_file embedding)" \
            env EMBEDDING_LISTEN_PORT="$EMBEDDING_PORT" \
            VVIP_DATABASE_URL="$(embedding_dsn)" \
            "$VENV_DIR/embedding/bin/python" "$EMBED_LAUNCHER"
        wait_http "http://127.0.0.1:$EMBEDDING_PORT/health" "embedding-service" 300 || true
    else
        warn "embedding-service not startable (app missing or venv unbuilt — run: $0 build)" \
             "; :$EMBEDDING_PORT stays down."
    fi
}

stop_host() {  # <pidfile> <name>
    local pidfile="$1" name="$2"
    if unit_active "$name"; then
        warn "$name is managed by systemd — stop it with: systemctl --user stop $(unit_of "$name")"
        return 0
    fi
    if is_running "$pidfile"; then
        local pid; pid="$(cat "$pidfile")"
        # the supervisor subshell wraps the actual server; kill the tree
        local child
        for child in $(pgrep -P "$pid" 2>/dev/null); do kill -TERM "$child" 2>/dev/null || true; done
        kill -TERM "$pid" 2>/dev/null || true
        rm -f "$pidfile"
        log "$name stopped"
    else
        rm -f "$pidfile"
        log "$name not running"
    fi
}

cmd_start() {
    mkdir -p "$LOG_DIR" "$VENV_DIR" "$RUN_DIR"
    build_venvs
    start_llama
    start_model_host
    start_embedding
    cmd_status
}

cmd_stop() {
    stop_host "$(pid_file llama-server)" "llama-server"
    stop_host "$(pid_file model-host)" "model-host"
    stop_host "$(pid_file embedding)" "embedding"
    log "note: ollama :11434 left untouched (externally active)"
}

supervisor_of() {  # <host> → human-readable supervision
    if unit_active "$1"; then echo "systemd $(unit_of "$1")"
    elif is_running "$(pid_file "$1")"; then echo "legacy loop pid $(cat "$(pid_file "$1")")"
    else echo "unsupervised"
    fi
}

cmd_status() {
    local rc=0 state
    state="$(decider_state)"
    case "$state" in
        loaded)  log "model-host  :$MODEL_HOST_PORT  UP (decider loaded)  [$(supervisor_of model-host)]" ;;
        loading) warn "model-host  :$MODEL_HOST_PORT  LOADING (decider not loaded yet)  [$(supervisor_of model-host)]"; rc=1 ;;
        error:*) warn "model-host  :$MODEL_HOST_PORT  FAILED (${state#error:})  [$(supervisor_of model-host)]"; rc=1 ;;
        *)       warn "model-host  :$MODEL_HOST_PORT  DOWN  [$(supervisor_of model-host)]"; rc=1 ;;
    esac
    local spec name port host
    for spec in "llama-server:$LLAMA_PORT:llama-server" "embedding-service:$EMBEDDING_PORT:embedding"; do
        IFS=: read -r name port host <<<"$spec"
        if curl -sf --max-time 3 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
            log "$name  :$port  UP  [$(supervisor_of "$host")]"
        else
            warn "$name  :$port  DOWN  [$(supervisor_of "$host")]"; rc=1
        fi
    done
    return $rc
}

case "${1:-start}" in
    start)          cmd_start ;;
    build)          build_venvs ;;
    stop)           cmd_stop ;;
    status)         cmd_status ;;
    --foreground)   foreground "${2:-}" ;;
    *) echo "usage: $0 {start|build|stop|status|--foreground <llama|decider|embed>}" >&2; exit 2 ;;
esac
