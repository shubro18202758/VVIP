#!/usr/bin/env bash
# os_checklist.sh — Pre-deployment verification for Legion 7i
# Validates BIOS settings, drivers, kernel params, and container runtime.
#
# Usage: ./infra/scripts/os_checklist.sh

set -euo pipefail

PASS=0
WARN=0
FAIL=0

pass() { echo -e "  \033[0;32m[PASS]\033[0m $*"; PASS=$((PASS+1)); }
warn() { echo -e "  \033[1;33m[WARN]\033[0m $*"; WARN=$((WARN+1)); }
fail() { echo -e "  \033[0;31m[FAIL]\033[0m $*"; FAIL=$((FAIL+1)); }

echo "================================================================"
echo " VVIP Platform — Legion 7i Pre-Deployment Checklist"
echo " Hardware: i9-13900HX / RTX 4070 8GB / 32GB RAM / NVMe"
echo "================================================================"
echo ""

# ═══════════════════════════════════════════════════════════════════
# 1. BIOS / UEFI SETTINGS (manual verification prompts)
# ═══════════════════════════════════════════════════════════════════
echo "─── 1. BIOS / UEFI Settings (verify manually) ────────────────"
echo "  [ ] Performance Mode: ENABLED (not Balanced/Quiet)"
echo "  [ ] Hyper-Threading: ENABLED (24 logical cores)"
echo "  [ ] Intel SpeedStep (EIST): ENABLED"
echo "  [ ] Turbo Boost: ENABLED (up to 5.4 GHz)"
echo "  [ ] Fan Profile: PERFORMANCE (not Auto/Quiet)"
echo "  [ ] dGPU Mode: ENABLED (not Hybrid/Optimus — ensure RTX 4070 always active)"
echo "  [ ] Resizable BAR: ENABLED (for GPU memory mapping)"
echo "  [ ] Secure Boot: as required by OS"
echo "  [ ] Virtualization (VT-x/VT-d): ENABLED (for Docker/WSL2)"
echo ""

# ═══════════════════════════════════════════════════════════════════
# 2. NVIDIA GPU DRIVER
# ═══════════════════════════════════════════════════════════════════
echo "─── 2. NVIDIA GPU Driver ──────────────────────────────────────"

if command -v nvidia-smi &>/dev/null; then
    DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null || echo "unknown")
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo "unknown")
    VRAM_TOTAL=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null || echo "0")

    pass "nvidia-smi available"
    echo "       GPU: $GPU_NAME | Driver: $DRIVER_VER | VRAM: ${VRAM_TOTAL} MB"

    if [ "${VRAM_TOTAL:-0}" -ge 8000 ]; then
        pass "GPU VRAM >= 8 GB"
    else
        fail "GPU VRAM < 8 GB (${VRAM_TOTAL} MB) — insufficient for Ollama + ONNX"
    fi

    # Check persistence mode
    PM=$(nvidia-smi --query-gpu=persistence_mode --format=csv,noheader 2>/dev/null || echo "Disabled")
    if echo "$PM" | grep -qi "enabled"; then
        pass "GPU persistence mode: ON"
    else
        warn "GPU persistence mode: OFF — run 'nvidia-smi -pm 1' for lower cold-start"
    fi
else
    fail "nvidia-smi not found — install NVIDIA drivers"
fi
echo ""

# ═══════════════════════════════════════════════════════════════════
# 3. NVIDIA CONTAINER TOOLKIT
# ═══════════════════════════════════════════════════════════════════
echo "─── 3. NVIDIA Container Toolkit ──────────────────────────────"

if command -v nvidia-container-cli &>/dev/null; then
    pass "nvidia-container-cli found"
elif docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi &>/dev/null 2>&1; then
    pass "Docker GPU passthrough working"
else
    fail "NVIDIA Container Toolkit not installed — 'docker run --gpus' will fail"
    echo "       Install: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/"
fi
echo ""

# ═══════════════════════════════════════════════════════════════════
# 4. DOCKER ENGINE
# ═══════════════════════════════════════════════════════════════════
echo "─── 4. Docker Engine ──────────────────────────────────────────"

if command -v docker &>/dev/null; then
    DOCKER_VER=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
    pass "Docker installed: v$DOCKER_VER"

    if docker info &>/dev/null 2>&1; then
        pass "Docker daemon running"
    else
        fail "Docker daemon not running"
    fi

    # Check Docker Compose
    if docker compose version &>/dev/null 2>&1; then
        COMPOSE_VER=$(docker compose version --short 2>/dev/null || echo "unknown")
        pass "Docker Compose: v$COMPOSE_VER"
    else
        fail "Docker Compose not available"
    fi

    # Check storage driver
    STORAGE=$(docker info --format '{{.Driver}}' 2>/dev/null || echo "unknown")
    if [ "$STORAGE" = "overlay2" ]; then
        pass "Storage driver: overlay2"
    else
        warn "Storage driver: $STORAGE (overlay2 recommended)"
    fi
else
    fail "Docker not installed"
fi
echo ""

# ═══════════════════════════════════════════════════════════════════
# 5. SYSTEM RAM
# ═══════════════════════════════════════════════════════════════════
echo "─── 5. System RAM ─────────────────────────────────────────────"

if command -v free &>/dev/null; then
    TOTAL_MB=$(free -m | awk '/^Mem:/{print $2}')
    AVAIL_MB=$(free -m | awk '/^Mem:/{print $7}')

    if [ "$TOTAL_MB" -ge 30000 ]; then
        pass "Total RAM: ${TOTAL_MB} MB (>= 30 GB)"
    elif [ "$TOTAL_MB" -ge 16000 ]; then
        warn "Total RAM: ${TOTAL_MB} MB (16-30 GB — tight for full stack)"
    else
        fail "Total RAM: ${TOTAL_MB} MB (< 16 GB — insufficient)"
    fi

    if [ "$AVAIL_MB" -ge 16000 ]; then
        pass "Available RAM: ${AVAIL_MB} MB"
    else
        warn "Available RAM: ${AVAIL_MB} MB — close containers or processes to free memory"
    fi
else
    warn "Cannot check RAM (free command not available)"
fi
echo ""

# ═══════════════════════════════════════════════════════════════════
# 6. CPU
# ═══════════════════════════════════════════════════════════════════
echo "─── 6. CPU ────────────────────────────────────────────────────"

CORES=$(nproc 2>/dev/null || echo "0")
if [ "$CORES" -ge 16 ]; then
    pass "CPU cores: $CORES (>= 16 — full pinning available)"
elif [ "$CORES" -ge 8 ]; then
    warn "CPU cores: $CORES (8-15 — reduce cpuset ranges in compose.prod.yml)"
else
    fail "CPU cores: $CORES (< 8 — cpuset pinning will over-subscribe)"
fi

# CPU governor
if [ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor ]; then
    GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)
    if [ "$GOV" = "performance" ]; then
        pass "CPU governor: performance"
    else
        warn "CPU governor: $GOV (set to 'performance' for max throughput)"
    fi
else
    warn "CPU governor not accessible (WSL2 — managed by Windows)"
fi
echo ""

# ═══════════════════════════════════════════════════════════════════
# 7. KERNEL PARAMETERS
# ═══════════════════════════════════════════════════════════════════
echo "─── 7. Kernel Parameters ──────────────────────────────────────"

check_sysctl() {
    local param=$1
    local expected=$2
    local current
    current=$(sysctl -n "$param" 2>/dev/null || echo "N/A")
    if [ "$current" = "$expected" ]; then
        pass "$param = $current"
    elif [ "$current" = "N/A" ]; then
        warn "$param: not available"
    else
        warn "$param = $current (recommended: $expected)"
    fi
}

check_sysctl "vm.swappiness" "10"
check_sysctl "vm.overcommit_memory" "1"
check_sysctl "net.core.somaxconn" "8192"
check_sysctl "net.ipv4.tcp_tw_reuse" "1"

# File descriptors
NOFILE=$(ulimit -n 2>/dev/null || echo "0")
if [ "$NOFILE" -ge 65536 ]; then
    pass "ulimit -n: $NOFILE (>= 65536)"
else
    warn "ulimit -n: $NOFILE (set to 65536+ for container networking)"
fi
echo ""

# ═══════════════════════════════════════════════════════════════════
# 8. DISK I/O
# ═══════════════════════════════════════════════════════════════════
echo "─── 8. Disk I/O ───────────────────────────────────────────────"

NVME_FOUND=false
for dev in /sys/block/nvme*; do
    if [ -d "$dev" ]; then
        NVME_FOUND=true
        NAME=$(basename "$dev")
        SCHED=$(cat "$dev/queue/scheduler" 2>/dev/null || echo "unknown")
        pass "NVMe device: $NAME (scheduler: $SCHED)"

        READAHEAD=$(cat "$dev/queue/read_ahead_kb" 2>/dev/null || echo "0")
        if [ "$READAHEAD" -ge 1024 ]; then
            pass "$NAME readahead: ${READAHEAD} KB"
        else
            warn "$NAME readahead: ${READAHEAD} KB (set to 2048 KB for better throughput)"
        fi
    fi
done
if [ "$NVME_FOUND" = "false" ]; then
    warn "No NVMe devices detected (may be WSL2 — check Windows disk settings)"
fi

# Transparent huge pages
if [ -f /sys/kernel/mm/transparent_hugepage/enabled ]; then
    THP=$(cat /sys/kernel/mm/transparent_hugepage/enabled | grep -oP '\[\K[^\]]+')
    if [ "$THP" = "never" ]; then
        pass "Transparent huge pages: disabled"
    else
        warn "Transparent huge pages: $THP (set to 'never' for database workloads)"
    fi
fi
echo ""

# ═══════════════════════════════════════════════════════════════════
# 9. OLLAMA
# ═══════════════════════════════════════════════════════════════════
echo "─── 9. Ollama ─────────────────────────────────────────────────"

if command -v ollama &>/dev/null; then
    pass "Ollama installed"

    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
        pass "Ollama API responding"

        if curl -sf http://localhost:11434/api/tags | grep -q "qwen3.5:9b-q4_K_M"; then
            pass "Qwen 3.5 9B model available"
        else
            warn "Qwen 3.5 9B not found — run: ollama pull qwen3.5:9b-q4_K_M"
        fi
    else
        warn "Ollama not running — run: ollama serve"
    fi
else
    fail "Ollama not installed — https://ollama.com/download"
fi
echo ""

# ═══════════════════════════════════════════════════════════════════
# 10. APPLICATION DEPENDENCIES
# ═══════════════════════════════════════════════════════════════════
echo "─── 10. Application Dependencies ─────────────────────────────"

for cmd in cargo uv bun; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" --version 2>/dev/null | head -1 || echo "unknown")
        pass "$cmd: $VER"
    else
        warn "$cmd not found (needed for local dev, not for container deployment)"
    fi
done
echo ""

# ═══════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════
echo "================================================================"
echo " SUMMARY"
echo "================================================================"
echo -e "  \033[0;32mPASS:\033[0m $PASS"
echo -e "  \033[1;33mWARN:\033[0m $WARN"
echo -e "  \033[0;31mFAIL:\033[0m $FAIL"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo -e "  \033[0;31mFIX the FAIL items above before deploying.\033[0m"
    echo ""
    echo "  Quick fix commands:"
    echo "    sudo ./infra/scripts/os_tuning.sh    # Apply kernel tuning"
    echo "    nvidia-smi -pm 1                     # Enable GPU persistence"
    echo "    ollama serve                          # Start Ollama"
    echo "    ollama pull qwen3.5:9b-q4_K_M        # Pull Qwen model"
    exit 1
elif [ "$WARN" -gt 5 ]; then
    echo -e "  \033[1;33mMultiple warnings — review before production deployment.\033[0m"
    exit 0
else
    echo -e "  \033[0;32mSystem is ready for deployment!\033[0m"
    echo "  Run: ./infra/scripts/deploy.sh up"
    exit 0
fi
