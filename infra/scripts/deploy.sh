#!/usr/bin/env bash
# deploy.sh — Production deployment for VVIP Convoy Orchestration Platform
# Target: Legion 7i (i9-13900HX / RTX 4070 8GB / 32GB RAM / Windows 11 + WSL2)
#
# Usage:
#   ./infra/scripts/deploy.sh [up|down|restart|status|logs|build]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$PROJECT_ROOT/infra/compose.prod.yml"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[DEPLOY]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ─── Pre-Flight Checks ───────────────────────────────────────────

preflight() {
    log "Running pre-flight checks..."

    # Docker
    if ! command -v docker &>/dev/null; then
        err "Docker not found. Install Docker Desktop with WSL2 backend."
        exit 1
    fi
    docker info &>/dev/null || { err "Docker daemon not running."; exit 1; }

    # NVIDIA Container Toolkit
    if docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi &>/dev/null 2>&1; then
        log "  NVIDIA Container Toolkit: OK"
    else
        warn "  NVIDIA Container Toolkit not available — traffic-oracle will use CPU-only mode"
    fi

    # Model hosts (GB10 unified-memory stack; qwen/ollama orchestration retired)
    local mh_url="${MODEL_HOST_URL:-http://127.0.0.1:8083}"
    local llama_url="${LLAMA_BASE_URL:-http://127.0.0.1:8084}"
    local embed_url="${EMBEDDING_SERVICE_URL:-http://127.0.0.1:8085}"
    for spec in "model-host:$mh_url/decider/health" "llama-server:$llama_url/health" "embedding-service:$embed_url/health"; do
        local name="${spec%%:*}" url="${spec#*:}"
        if curl -sf --max-time 3 "$url" >/dev/null 2>&1; then
            log "  $name: running ($url)"
        else
            warn "  $name not healthy ($url). Run: ./infra/scripts/serve_models.sh start"
        fi
    done

    # GPU check — GB10 reports memory.total as N/A; /proc/meminfo is the
    # source of truth for the unified pool (see models/MEMORY_BUDGET.md)
    if command -v nvidia-smi &>/dev/null; then
        GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo "unknown")
        log "  GPU: $GPU_NAME (unified memory — see /proc/meminfo)"
    else
        warn "  No NVIDIA GPU detected"
    fi
    if [ -r /proc/meminfo ]; then
        TOTAL_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
        log "  Unified memory pool: $(( TOTAL_KB / 1024 / 1024 )) GiB total"
    fi

    # System RAM
    if command -v free &>/dev/null; then
        TOTAL_RAM_MB=$(free -m | awk '/^Mem:/{print $2}')
        log "  System RAM: ${TOTAL_RAM_MB} MB"
        if [ "$TOTAL_RAM_MB" -lt 16000 ]; then
            warn "  <16GB RAM detected — reduce container memory limits"
        fi
    fi

    # CPU
    CPU_CORES=$(nproc 2>/dev/null || echo "unknown")
    log "  CPU cores: $CPU_CORES"

    echo ""
}

# ─── Apply OS Tuning ─────────────────────────────────────────────

apply_os_tuning() {
    log "Applying OS-level tuning (requires sudo)..."

    # Increase file descriptor limits
    if [ "$(ulimit -n)" -lt 65536 ]; then
        ulimit -n 65536 2>/dev/null || warn "  Could not set ulimit -n 65536"
    fi

    # Kernel network tuning (if running in WSL2 or native Linux)
    if [ -f /proc/sys/net/core/somaxconn ]; then
        sudo sysctl -w net.core.somaxconn=8192 &>/dev/null 2>&1 || true
        sudo sysctl -w net.core.netdev_max_backlog=8192 &>/dev/null 2>&1 || true
        sudo sysctl -w net.ipv4.tcp_max_syn_backlog=8192 &>/dev/null 2>&1 || true
        sudo sysctl -w net.ipv4.tcp_tw_reuse=1 &>/dev/null 2>&1 || true
        sudo sysctl -w vm.overcommit_memory=1 &>/dev/null 2>&1 || true
        sudo sysctl -w vm.swappiness=10 &>/dev/null 2>&1 || true
        log "  Kernel parameters tuned"
    fi

    # Disable transparent huge pages for Valkey (if available)
    if [ -f /sys/kernel/mm/transparent_hugepage/enabled ]; then
        echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled &>/dev/null 2>&1 || true
        log "  Transparent huge pages disabled"
    fi
}

# ─── Commands ─────────────────────────────────────────────────────

cmd_build() {
    log "Building all service images..."
    docker compose -f "$COMPOSE_FILE" build --parallel
    log "Build complete."
}

cmd_up() {
    preflight
    apply_os_tuning

    log "Starting VVIP platform (production mode)..."

    # Start infrastructure first
    log "Phase 1: Infrastructure (PostGIS, Valkey, NATS)..."
    docker compose -f "$COMPOSE_FILE" up -d postgis valkey nats
    log "Waiting for PostGIS health check..."
    docker compose -f "$COMPOSE_FILE" exec -T postgis pg_isready -U corridor_admin -d corridor_db --timeout=30 || {
        err "PostGIS did not become healthy in time"
        exit 1
    }

    # Apply migrations (idempotent ledgered runner — reaches existing volumes,
    # unlike docker-entrypoint-initdb.d which only runs on an EMPTY volume)
    log "Phase 1b: Applying corridor-store migrations (migrate.py)..."
    MIGRATE_VENV="$PROJECT_ROOT/models/.venvs/migrate/bin/python"
    if [ -x "$MIGRATE_VENV" ]; then
        "$MIGRATE_VENV" "$PROJECT_ROOT/scripts/migrate.py" || {
            err "migrate.py failed"
            exit 1
        }
    else
        warn "migrate venv missing — run: ./infra/scripts/serve_models.sh build"
    fi

    # Model hosts on the host (GB10 unified-memory stack)
    log "Phase 1c: Starting model hosts (:8083 decider, :8084 Gemma, :8085 embeddings)..."
    "$PROJECT_ROOT/infra/scripts/serve_models.sh" start

    # Start compute services
    log "Phase 2: Compute (signal-ingress, traffic-oracle)..."
    docker compose -f "$COMPOSE_FILE" up -d signal-ingress traffic-oracle

    # Start orchestration
    log "Phase 3: Orchestration (convoy-brain, command-deck)..."
    docker compose -f "$COMPOSE_FILE" up -d convoy-brain command-deck

    # Start observability
    log "Phase 4: Observability (Prometheus, Grafana, NVIDIA exporter)..."
    docker compose -f "$COMPOSE_FILE" up -d prometheus grafana nvidia-exporter

    echo ""
    log "${GREEN}Platform is up!${NC}"
    echo ""
    echo -e "  ${CYAN}Dashboard:${NC}     http://localhost:5173"
    echo -e "  ${CYAN}Convoy API:${NC}    http://localhost:8080"
    echo -e "  ${CYAN}Traffic API:${NC}   http://localhost:8081"
    echo -e "  ${CYAN}Grafana:${NC}       http://localhost:3000  (admin / \${GRAFANA_PASSWORD:-vvip_admin})"
    echo -e "  ${CYAN}Prometheus:${NC}    http://localhost:9090"
    echo -e "  ${CYAN}NATS Monitor:${NC}  http://localhost:8222"
    echo ""
}

cmd_down() {
    log "Stopping all services..."
    docker compose -f "$COMPOSE_FILE" down
    log "All services stopped."
}

cmd_restart() {
    local service="${1:-}"
    if [ -n "$service" ]; then
        log "Restarting $service..."
        docker compose -f "$COMPOSE_FILE" restart "$service"
    else
        cmd_down
        cmd_up
    fi
}

cmd_status() {
    echo -e "${CYAN}=== VVIP Platform Status ===${NC}"
    echo ""
    docker compose -f "$COMPOSE_FILE" ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
    echo ""

    # GPU status
    if command -v nvidia-smi &>/dev/null; then
        echo -e "${CYAN}--- GPU Status ---${NC}"
        nvidia-smi --query-gpu=name,memory.used,memory.free,utilization.gpu,temperature.gpu \
            --format=csv,noheader 2>/dev/null || echo "(nvidia-smi failed)"
        echo ""
    fi

    # Model host status (GB10 unified-memory stack)
    echo -e "${CYAN}--- Model Hosts ---${NC}"
    "$PROJECT_ROOT/infra/scripts/serve_models.sh" status || true
    echo ""
}

cmd_logs() {
    local service="${1:-}"
    if [ -n "$service" ]; then
        docker compose -f "$COMPOSE_FILE" logs -f --tail=100 "$service"
    else
        docker compose -f "$COMPOSE_FILE" logs -f --tail=50
    fi
}

# ─── Main ─────────────────────────────────────────────────────────

case "${1:-help}" in
    build)   cmd_build ;;
    up)      cmd_up ;;
    down)    cmd_down ;;
    restart) cmd_restart "${2:-}" ;;
    status)  cmd_status ;;
    logs)    cmd_logs "${2:-}" ;;
    help|*)
        echo "VVIP Convoy Platform — Deployment Manager"
        echo ""
        echo "Usage: $0 <command> [args]"
        echo ""
        echo "Commands:"
        echo "  build              Build all container images"
        echo "  up                 Start full platform (phased rollout)"
        echo "  down               Stop all services"
        echo "  restart [service]  Restart all or a specific service"
        echo "  status             Show platform status + GPU info"
        echo "  logs [service]     Tail logs for all or a specific service"
        ;;
esac
