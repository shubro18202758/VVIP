#!/usr/bin/env bash
# os_tuning.sh — Host OS optimization for Legion 7i
# Applies kernel parameters, CPU governor, NUMA settings, and I/O tuning.
# Run with sudo on the WSL2 Linux instance or native Linux.
#
# Usage: sudo ./infra/scripts/os_tuning.sh

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[TUNE]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "This script requires root. Run with sudo."
    exit 1
fi

echo "=== VVIP Platform — OS Tuning for Legion 7i ==="
echo "Hardware: i9-13900HX / RTX 4070 8GB / 32GB RAM / NVMe SSD"
echo ""

# ─── 1. Kernel Memory Parameters ─────────────────────────────────

log "1. Memory tuning..."

# Reduce swappiness — containers manage their own memory limits
sysctl -w vm.swappiness=10

# Allow overcommit (Valkey requirement for background AOF/RDB saves)
sysctl -w vm.overcommit_memory=1
sysctl -w vm.overcommit_ratio=95

# Increase dirty page limits for NVMe throughput
sysctl -w vm.dirty_ratio=40
sysctl -w vm.dirty_background_ratio=10

# Larger page cache readahead for NVMe
if [ -d /sys/block/nvme0n1 ]; then
    echo 2048 > /sys/block/nvme0n1/queue/read_ahead_kb 2>/dev/null || true
    log "  NVMe readahead set to 2048 KB"
fi

# Transparent huge pages: off for database/cache workloads
if [ -f /sys/kernel/mm/transparent_hugepage/enabled ]; then
    echo never > /sys/kernel/mm/transparent_hugepage/enabled
    echo never > /sys/kernel/mm/transparent_hugepage/defrag
    log "  Transparent huge pages disabled"
fi

# ─── 2. Network Stack Tuning ─────────────────────────────────────

log "2. Network tuning..."

# Connection backlog (NATS, convoy-brain handle bursts)
sysctl -w net.core.somaxconn=8192
sysctl -w net.core.netdev_max_backlog=8192
sysctl -w net.ipv4.tcp_max_syn_backlog=8192

# TCP tuning for internal container-to-container traffic
sysctl -w net.ipv4.tcp_tw_reuse=1
sysctl -w net.ipv4.tcp_fin_timeout=15
sysctl -w net.ipv4.tcp_keepalive_time=300
sysctl -w net.ipv4.tcp_keepalive_probes=5
sysctl -w net.ipv4.tcp_keepalive_intvl=15

# Buffer sizes for high-throughput Arrow IPC transfers
sysctl -w net.core.rmem_max=16777216
sysctl -w net.core.wmem_max=16777216
sysctl -w net.ipv4.tcp_rmem="4096 87380 16777216"
sysctl -w net.ipv4.tcp_wmem="4096 65536 16777216"

# Local port range for outbound connections
sysctl -w net.ipv4.ip_local_port_range="1024 65535"

# ─── 3. File Descriptor Limits ────────────────────────────────────

log "3. File descriptor limits..."

# Container runtime needs high FD limits
cat > /etc/security/limits.d/99-vvip.conf << 'LIMITS'
# VVIP Platform — File descriptor limits
*               soft    nofile          65536
*               hard    nofile          131072
*               soft    nproc           32768
*               hard    nproc           65536
root            soft    nofile          65536
root            hard    nofile          131072
LIMITS
log "  /etc/security/limits.d/99-vvip.conf written"

# ─── 4. I/O Scheduler (NVMe) ──────────────────────────────────────

log "4. I/O scheduler optimization..."

for dev in /sys/block/nvme*; do
    if [ -f "$dev/queue/scheduler" ]; then
        echo none > "$dev/queue/scheduler" 2>/dev/null || true
        # NVMe devices perform best with no I/O scheduler (direct submission)
        log "  $(basename "$dev"): scheduler set to 'none'"
    fi
done

# ─── 5. CPU Governor ──────────────────────────────────────────────

log "5. CPU governor tuning..."

if [ -d /sys/devices/system/cpu/cpu0/cpufreq ]; then
    for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        echo performance > "$cpu" 2>/dev/null || true
    done
    log "  CPU governor set to 'performance' on all cores"
else
    warn "  cpufreq not available (may be WSL2 — governor managed by Windows)"
fi

# ─── 6. NVIDIA GPU Tuning ────────────────────────────────────────

log "6. GPU tuning..."

if command -v nvidia-smi &>/dev/null; then
    # Set GPU to persistent mode (reduces cold-start latency)
    nvidia-smi -pm 1 2>/dev/null && log "  GPU persistence mode: ON" || warn "  Could not enable persistence mode"

    # Set compute mode to DEFAULT (allow multiple contexts)
    nvidia-smi -c 0 2>/dev/null && log "  Compute mode: DEFAULT (multi-context)" || true

    # Set power limit to max for RTX 4070 Mobile (115W typical)
    MAX_POWER=$(nvidia-smi --query-gpu=power.max_limit --format=csv,noheader,nounits 2>/dev/null || echo "")
    if [ -n "$MAX_POWER" ]; then
        nvidia-smi -pl "$MAX_POWER" 2>/dev/null && log "  Power limit: ${MAX_POWER}W" || true
    fi

    # Application clocks (boost for sustained workloads)
    nvidia-smi -ac 7001,2475 2>/dev/null && log "  Application clocks locked to max" || true
else
    warn "  nvidia-smi not available — skipping GPU tuning"
fi

# ─── 7. Docker Daemon Tuning ──────────────────────────────────────

log "7. Docker daemon configuration..."

DOCKER_DAEMON_JSON="/etc/docker/daemon.json"
if [ -f "$DOCKER_DAEMON_JSON" ]; then
    warn "  $DOCKER_DAEMON_JSON already exists — verify settings manually"
else
    cat > "$DOCKER_DAEMON_JSON" << 'DAEMON'
{
  "default-runtime": "nvidia",
  "runtimes": {
    "nvidia": {
      "args": [],
      "path": "nvidia-container-runtime"
    }
  },
  "storage-driver": "overlay2",
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  },
  "default-ulimits": {
    "nofile": {
      "Name": "nofile",
      "Hard": 65536,
      "Soft": 65536
    }
  },
  "max-concurrent-downloads": 10,
  "max-concurrent-uploads": 5
}
DAEMON
    log "  Docker daemon.json written"
    warn "  Restart Docker to apply: sudo systemctl restart docker"
fi

echo ""
log "=== OS Tuning Complete ==="
echo ""
echo "Summary of changes applied:"
echo "  [1] vm.swappiness=10, overcommit_memory=1, dirty_ratio=40"
echo "  [2] TCP backlog=8192, buffer max=16MB, tw_reuse=1"
echo "  [3] nofile=65536/131072, nproc=32768/65536"
echo "  [4] NVMe: scheduler=none, readahead=2048KB"
echo "  [5] CPU governor: performance (all cores)"
echo "  [6] GPU: persistence=ON, max power, locked clocks"
echo "  [7] Docker: overlay2, nvidia runtime, log rotation"
echo ""
echo "Reboot-persistent settings written to:"
echo "  /etc/security/limits.d/99-vvip.conf"
echo "  /etc/docker/daemon.json"
echo ""
echo "For sysctl persistence, add to /etc/sysctl.d/99-vvip.conf:"
echo "  vm.swappiness=10"
echo "  vm.overcommit_memory=1"
echo "  net.core.somaxconn=8192"
echo "  net.ipv4.tcp_tw_reuse=1"
