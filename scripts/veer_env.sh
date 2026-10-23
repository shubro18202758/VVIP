#!/usr/bin/env bash
# veer_env.sh — the Veer (argus) stack's host environment, in one place.
#
# SOURCE it from other scripts:
#     . "$VEER_ROOT/scripts/veer_env.sh"
#   Loads ~/.config/veer/veer.env (explicitly exported variables always win),
#   then fills defaults for the repo root, the compose invocation
#   (`veer_compose ...`), host ports, container names and runtime dirs.
#
# RUN it:
#     scripts/veer_env.sh --write [--force]   write ~/.config/veer/veer.env from
#                                             this host's detected paths (--force
#                                             regenerates it, keeping every other line)
#     scripts/veer_env.sh --set KEY VALUE     set one key in veer.env
#     scripts/veer_env.sh --set KEY -         …the value read from stdin (secrets:
#                                             never in argv or shell history)
#     scripts/veer_env.sh --print             print the effective environment
#
# ~/.config/veer/veer.env (mode 0600) is plain KEY=VALUE lines: it is both
# the EnvironmentFile= of the systemd --user units (infra/systemd/ read
# %h/.config/veer/veer.env — hence this fixed path, never $XDG_CONFIG_HOME)
# and what this script loads, so a reboot, a unit restart and a manual run
# all see the same settings. Every KEY=VALUE line in it is exported. Besides
# the host paths and switches --write records, it is where the values
# compose interpolates belong (VEER_PERSIST_KEYS below: the operator gate's
# VEER_OPERATOR_PIN / VEER_SESSION_SECRET, a non-default POSTGIS_PASSWORD,
# the VEER_CESIUM_ION_TOKEN, …): a boot bring-up has no login shell to
# inherit them from, and a deck recreated without the PIN serves the LAN
# with the gate OFF. Secrets go in with `--set KEY -` (stdin).

# ─── loader (works when sourced or executed) ────────────────────────────────

VEER_ROOT="${VEER_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VEER_ENV_FILE="${VEER_ENV_FILE:-$HOME/.config/veer/veer.env}"

# The keys --write records and --print shows (veer.env may carry others,
# e.g. the compose values above; the loader exports every line).
VEER_ENV_KEYS=(
    VEER_ROOT VEER_RUN_DIR VEER_LOG_DIR VEER_VENV_DIR VEER_MODELS_CONFIG
    POSTGIS_HOST_PORT DECIDER_SNAPSHOT_PATH LLAMA_SERVER_BIN GEMMA_GGUF
    BGE_M3_PATH VEER_DEMO VEER_CONVERGE_TIMEOUT
)

# Values compose interpolates into the stack that must survive a reboot:
# --write keeps them (from veer.env, or from this shell when only it has
# them) and scripts/veer_up.sh warns when this shell carries one veer.env
# lacks. Any OTHER line already in veer.env is carried over by --write too.
VEER_PERSIST_KEYS=(
    VEER_OPERATOR_PIN VEER_SESSION_SECRET VEER_SESSION_SECURE VEER_CESIUM_ION_TOKEN
    POSTGIS_PASSWORD VVIP_ESCORT_SIM_GPS VVIP_REQUIRE_OFFICER
    EMULATOR_LIVING_VIOLATORS EMULATOR_CITY_SEED
)

veer_env_load() {  # <file> — export KEY=VALUE lines not already set
    local file="$1" line key value
    [ -r "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"
        [[ -z "$line" || "$line" == \#* ]] && continue
        [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]] || continue
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]] && value="${BASH_REMATCH[1]}"
        [ -n "${!key+x}" ] && continue          # explicit environment wins
        export "$key=$value"
    done < "$file"
}

veer_env_load "$VEER_ENV_FILE"

# ─── defaults ───────────────────────────────────────────────────────────────

export VEER_ROOT
export VEER_COMPOSE_PROJECT="${VEER_COMPOSE_PROJECT:-vvip-argus}"
export VEER_RUN_DIR="${VEER_RUN_DIR:-$VEER_ROOT/.run-models}"
export VEER_LOG_DIR="${VEER_LOG_DIR:-$VEER_RUN_DIR/logs}"
export VEER_VENV_DIR="${VEER_VENV_DIR:-$VEER_ROOT/.venvs}"
export VEER_MODELS_CONFIG_DIR="${VEER_MODELS_CONFIG_DIR:-$VEER_ROOT/models-config}"
export VEER_MODELS_CONFIG="${VEER_MODELS_CONFIG:-$VEER_MODELS_CONFIG_DIR/configs/veer_models.yaml}"
export VEER_DEMO="${VEER_DEMO:-0}"

# host ports (fixed platform constants of the argus stack)
export POSTGIS_HOST_PORT="${POSTGIS_HOST_PORT:-56434}"
export VALKEY_HOST_PORT="${VALKEY_HOST_PORT:-6380}"
export NATS_HOST_PORT="${NATS_HOST_PORT:-4223}"
export NATS_MON_PORT="${NATS_MON_PORT:-8223}"
export ORACLE_PORT="${ORACLE_PORT:-8091}"
export BRAIN_PORT="${BRAIN_PORT:-8092}"
export EMULATOR_PORT="${EMULATOR_PORT:-8090}"
export DECK_PORT="${DECK_PORT:-5273}"
export MODEL_HOST_PORT="${MODEL_HOST_PORT:-8083}"
export LLAMA_PORT="${LLAMA_PORT:-8084}"
export EMBEDDING_PORT="${EMBEDDING_PORT:-8085}"

# container names (infra/compose.argus-override.yml)
export POSTGIS_CONTAINER="${POSTGIS_CONTAINER:-vvip-argus-postgis}"
export VALKEY_CONTAINER="${VALKEY_CONTAINER:-vvip-argus-valkey}"
export NATS_CONTAINER="${NATS_CONTAINER:-vvip-argus-nats}"
export INGRESS_CONTAINER="${INGRESS_CONTAINER:-vvip-argus-signal-ingress}"
export ORACLE_CONTAINER="${ORACLE_CONTAINER:-vvip-argus-traffic-oracle}"
export BRAIN_CONTAINER="${BRAIN_CONTAINER:-vvip-argus-convoy-brain}"
export DECK_CONTAINER="${DECK_CONTAINER:-vvip-argus-command-deck}"
export EMULATOR_CONTAINER="${EMULATOR_CONTAINER:-vvip-argus-event-emulator}"

# The one compose invocation of the argus stack.
VEER_COMPOSE_FILES=(
    -f "$VEER_ROOT/infra/compose.yml"
    -f "$VEER_ROOT/infra/compose.gb10-override.yml"
    -f "$VEER_ROOT/infra/compose.argus-override.yml"
)
veer_compose() {
    docker compose -p "$VEER_COMPOSE_PROJECT" "${VEER_COMPOSE_FILES[@]}" "$@"
}

# ─── CLI (only when executed, not when sourced) ─────────────────────────────

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    set -euo pipefail

    detect() {  # <env-value> <candidate...> — first given/existing path
        local given="$1"; shift
        if [ -n "$given" ]; then printf '%s\n' "$given"; return; fi
        local c
        for c in "$@"; do
            if [ -e "$c" ]; then printf '%s\n' "$c"; return; fi
        done
        printf '%s\n' "$1"
    }

    # One unquoted token that this loader and systemd's EnvironmentFile=
    # parser read identically: no whitespace, quotes, $, ` or \.
    VALUE_RE='^[^[:space:]"'"'"'$`\]*$'

    env_target() {  # the file behind VEER_ENV_FILE (a symlinked veer.env stays a symlink)
        mkdir -p "$(dirname "$VEER_ENV_FILE")"
        local t; t="$(readlink -f -- "$VEER_ENV_FILE" 2>/dev/null || true)"
        printf '%s\n' "${t:-$VEER_ENV_FILE}"
    }

    cmd_write() {
        local force="${1:-}"
        if [ -f "$VEER_ENV_FILE" ] && [ "$force" != "--force" ]; then
            echo "[veer_env] $VEER_ENV_FILE exists — keeping it (use --write --force to regenerate, --set KEY VALUE to edit)"
            return 0
        fi
        local venvs="$VEER_ROOT/.venvs"
        if [ ! -x "$venvs/model-host/bin/python" ] && [ -x "$VEER_ROOT/models/.venvs/model-host/bin/python" ]; then
            # legacy location (VVIP/models symlink into the retired tree)
            venvs="$(cd "$VEER_ROOT/models/.venvs" && pwd -P)"
            echo "[veer_env] WARN: model venvs found only in the retired tree ($venvs);" \
                 "rebuild under VVIP/.venvs with: VEER_VENV_DIR=$VEER_ROOT/.venvs infra/scripts/serve_models.sh build" >&2
        fi
        local decider gguf llama bge
        decider="$(detect "${DECIDER_SNAPSHOT_PATH:-}" \
            "$HOME/.cache/huggingface/hub/models--perplexity-ai--pplx-decider-v1-27b/snapshots/b01a5cbaca5391f73bd55103d4f27e8982cd5e60")"
        llama="$(detect "${LLAMA_SERVER_BIN:-}" "$HOME/llama.cpp/build/bin/llama-server")"
        gguf="$(detect "${GEMMA_GGUF:-}" "$HOME/models/q4-stack/v7h-merged-Q4_K_M.gguf")"
        bge="$(detect "${BGE_M3_PATH:-}" "$HOME/models/bge-m3")"

        # Everything else veer.env already holds is carried over (a
        # regenerate must never drop the operator gate's PIN or session
        # secret — the deck would come back on the LAN with the gate OFF),
        # plus the compose-interpolated values only this shell carries.
        # This shell's value wins, as it does in the loader.
        local key value line carried="" seen=" "
        local recorded=" ${VEER_ENV_KEYS[*]} "
        add_carried() {  # <key> <value>
            if [[ "$2" =~ $VALUE_RE ]]; then
                carried="$carried$1=$2"$'\n'
            else
                echo "[veer_env] WARN: $1 has spaces, quotes, \$, \` or \\ — not recorded; a boot bring-up will not see it" >&2
            fi
            seen="$seen$1 "
        }
        if [ -r "$VEER_ENV_FILE" ]; then
            while IFS= read -r line || [ -n "$line" ]; do
                line="${line#"${line%%[![:space:]]*}"}"
                [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]] || continue
                key="${BASH_REMATCH[1]}"
                value="${BASH_REMATCH[2]}"
                [[ "$recorded$seen" == *" $key "* ]] && continue
                [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]] && value="${BASH_REMATCH[1]}"
                add_carried "$key" "${!key-$value}"
            done < "$VEER_ENV_FILE"
        fi
        for key in "${VEER_PERSIST_KEYS[@]}"; do
            [[ "$seen" == *" $key "* ]] && continue
            [ -n "${!key:-}" ] || continue
            add_carried "$key" "${!key}"
        done

        local file tmp
        file="$(env_target)"
        tmp="$(mktemp "$file.XXXXXX")"
        cat > "$tmp" <<EOF
# Veer stack host environment — written by scripts/veer_env.sh --write on $(date -Is).
# EnvironmentFile= of the systemd --user units (infra/systemd/) and loaded by
# scripts/veer_env.sh. Plain KEY=VALUE lines (one unquoted token each); mode 0600.

# repository and runtime state
VEER_ROOT=$VEER_ROOT
VEER_RUN_DIR=$VEER_RUN_DIR
VEER_LOG_DIR=$VEER_LOG_DIR
VEER_VENV_DIR=$venvs
VEER_MODELS_CONFIG=$VEER_MODELS_CONFIG

# corridor-store published on loopback (embedding-service DSN)
POSTGIS_HOST_PORT=$POSTGIS_HOST_PORT

# model weights and binaries — explicit host paths, never in the repo
DECIDER_SNAPSHOT_PATH=$decider
LLAMA_SERVER_BIN=$llama
GEMMA_GGUF=$gguf
BGE_M3_PATH=$bge

# 1 = demo mode on every bring-up and reboot: city signal-plan writer
# (VVIP_SIGNAL_POLICY_WRITER) + living-city emulation (EMULATOR_LIVING_CITY)
VEER_DEMO=${VEER_DEMO:-0}

# values compose interpolates, when not the defaults, and every other line
# this file already carried (the operator gate's VEER_OPERATOR_PIN /
# VEER_SESSION_SECRET, POSTGIS_PASSWORD, VEER_CESIUM_ION_TOKEN, …) — set
# them with scripts/veer_env.sh --set KEY - (value on stdin)
$carried
EOF
        chmod 600 "$tmp"
        mv -f "$tmp" "$file"
        echo "[veer_env] wrote $VEER_ENV_FILE"
        local p
        for p in "$decider" "$llama" "$gguf" "$bge" "$venvs"; do
            [ -e "$p" ] || echo "[veer_env] WARN: path does not exist yet: $p" >&2
        done
    }

    cmd_set() {
        local key="${1:-}" value="${2-}" from_stdin=0
        [[ "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || { echo "usage: $0 --set KEY VALUE | --set KEY -" >&2; exit 2; }
        if [ "$value" = - ]; then
            # the secret path: the value never appears in argv (ps) or history
            from_stdin=1
            if [ -t 0 ]; then
                printf '%s: ' "$key" >&2
                IFS= read -r -s value || true
                printf '\n' >&2
            else
                IFS= read -r value || true
            fi
            [ -n "$value" ] || { echo "[veer_env] no value on stdin for $key" >&2; exit 2; }
        fi
        [[ "$value" =~ $VALUE_RE ]] || { echo "[veer_env] refusing a value with spaces, quotes, \$, \` or \\ (KEY=$key)" >&2; exit 2; }
        [ -f "$VEER_ENV_FILE" ] || cmd_write
        local file tmp
        file="$(env_target)"
        tmp="$(mktemp "$file.XXXXXX")"
        # Replace the first KEY= line (drop repeats) or append one. awk reads
        # the value from its environment: nothing in it (& | /) is syntax.
        VEER_SET_KEY="$key" VEER_SET_VALUE="$value" awk '
            BEGIN { k = ENVIRON["VEER_SET_KEY"]; line = k "=" ENVIRON["VEER_SET_VALUE"] }
            index($0, k "=") == 1 { if (!done) print line; done = 1; next }
            { print }
            END { if (!done) print line }' "$file" > "$tmp"
        chmod 600 "$tmp"
        mv -f "$tmp" "$file"
        if [ "$from_stdin" = 1 ]; then
            echo "[veer_env] $VEER_ENV_FILE: $key set (from stdin)"
            return
        fi
        case "$key" in
            *PASSWORD*|*TOKEN*|*SECRET*|*PIN*) echo "[veer_env] $VEER_ENV_FILE: $key set" ;;
            *) echo "[veer_env] $VEER_ENV_FILE: $key=$value" ;;
        esac
    }

    cmd_print() {
        echo "# effective Veer environment (file: $VEER_ENV_FILE$([ -f "$VEER_ENV_FILE" ] || echo ' — absent'))"
        local k
        for k in "${VEER_ENV_KEYS[@]}" VEER_COMPOSE_PROJECT VEER_LOG_DIR VEER_MODELS_CONFIG_DIR \
                 VALKEY_HOST_PORT NATS_HOST_PORT NATS_MON_PORT ORACLE_PORT BRAIN_PORT \
                 EMULATOR_PORT DECK_PORT MODEL_HOST_PORT LLAMA_PORT EMBEDDING_PORT; do
            printf '%s=%s\n' "$k" "${!k:-}"
        done | awk '!seen[$0]++'
        echo "# compose: docker compose -p $VEER_COMPOSE_PROJECT ${VEER_COMPOSE_FILES[*]}"
    }

    case "${1:-}" in
        --write) cmd_write "${2:-}" ;;
        --set)   cmd_set "${2:-}" "${3-}" ;;
        --print) cmd_print ;;
        *) echo "usage: $0 {--write [--force] | --set KEY VALUE | --set KEY - | --print}  (or source it)" >&2; exit 2 ;;
    esac
fi
