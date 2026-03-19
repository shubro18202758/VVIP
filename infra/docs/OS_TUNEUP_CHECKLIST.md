# VVIP Convoy Platform — Legion 7i OS Tune-Up Checklist

## Hardware Specification
| Component | Spec |
|---|---|
| CPU | Intel i9-13900HX (8P + 16E = 24 threads) |
| GPU | NVIDIA RTX 4070 Mobile (8192 MB GDDR6X) |
| RAM | 32 GB DDR5 |
| Storage | NVMe Gen4 SSD |
| OS | Windows 11 + WSL2 (Ubuntu 24.04 recommended) |

---

## 1. BIOS / UEFI Configuration

| Setting | Value | Why |
|---|---|---|
| Performance Mode | **ENABLED** | Prevents CPU thermal throttling below max turbo |
| Hyper-Threading | **ENABLED** | Exposes 24 logical cores for container cpuset pinning |
| Intel SpeedStep (EIST) | **ENABLED** | Allows per-core P-state transitions |
| Turbo Boost | **ENABLED** | Reaches 5.4 GHz on P-cores for single-thread Rust perf |
| Fan Profile | **PERFORMANCE** | Prevents GPU thermal throttle at 85C under sustained load |
| dGPU Mode | **dGPU ONLY** | Disables Optimus switching — ensures RTX 4070 always active |
| Resizable BAR | **ENABLED** | Full GPU memory mapping for CUDA context |
| VT-x / VT-d | **ENABLED** | Required for Docker Desktop WSL2 backend |

---

## 2. Windows 11 Host Settings

- [ ] **Power Plan**: High Performance (or Ultimate Performance if available)
  ```powershell
  powercfg /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c
  ```
- [ ] **WSL2 Memory**: Limit to 24 GB (leave 8 GB for Windows + Ollama)
  ```
  # %USERPROFILE%/.wslconfig
  [wsl2]
  memory=24GB
  processors=20
  swap=4GB
  localhostForwarding=true
  nestedVirtualization=true
  ```
- [ ] **GPU in WSL2**: Ensure `/usr/lib/wsl/lib/libcuda.so` exists
- [ ] **Docker Desktop**: WSL2 backend, "Use the WSL 2 based engine" checked
- [ ] **Ollama**: Runs on Windows host (NOT inside WSL2) for native GPU access
  - Accessible from WSL2/Docker via `host.docker.internal:11434`

---

## 3. Linux / WSL2 Kernel Parameters

Run `sudo ./infra/scripts/os_tuning.sh` or apply manually:

```bash
# /etc/sysctl.d/99-vvip.conf

# Memory
vm.swappiness = 10                     # Minimize swap usage
vm.overcommit_memory = 1               # Allow overcommit (Valkey requirement)
vm.dirty_ratio = 40                    # NVMe can handle large dirty page flushes
vm.dirty_background_ratio = 10

# Network (container-to-container IPC)
net.core.somaxconn = 8192              # NATS/convoy-brain connection backlog
net.core.netdev_max_backlog = 8192
net.ipv4.tcp_max_syn_backlog = 8192
net.ipv4.tcp_tw_reuse = 1             # Reuse TIME_WAIT sockets
net.ipv4.tcp_fin_timeout = 15
net.core.rmem_max = 16777216           # 16 MB receive buffers (Arrow IPC)
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.ipv4.ip_local_port_range = 1024 65535
```

---

## 4. File Descriptor Limits

```bash
# /etc/security/limits.d/99-vvip.conf
*    soft  nofile  65536
*    hard  nofile  131072
*    soft  nproc   32768
*    hard  nproc   65536
```

---

## 5. NVMe I/O Optimization

```bash
# I/O scheduler: none (NVMe handles its own scheduling)
echo none > /sys/block/nvme0n1/queue/scheduler

# Readahead: 2 MB for sequential PostGIS table scans
echo 2048 > /sys/block/nvme0n1/queue/read_ahead_kb

# Disable transparent huge pages (database anti-pattern)
echo never > /sys/kernel/mm/transparent_hugepage/enabled
echo never > /sys/kernel/mm/transparent_hugepage/defrag
```

---

## 6. NVIDIA GPU Configuration

```bash
# Persistence mode: keep GPU initialized (removes 200ms cold-start delay)
nvidia-smi -pm 1

# Compute mode: DEFAULT (multiple CUDA contexts: Ollama + ONNX)
nvidia-smi -c 0

# Lock application clocks to maximum (prevents dynamic downclocking)
nvidia-smi -ac 7001,2475

# Set power limit to maximum TGP
nvidia-smi -pl $(nvidia-smi --query-gpu=power.max_limit --format=csv,noheader,nounits)
```

---

## 7. CPU Core Allocation Map

```
┌──────────────────────────────────────────────────────────┐
│  i9-13900HX — 24 Logical Cores                          │
├──────────┬──────────┬──────────┬──────────┬──────────────┤
│ Cores 0-3│ Cores 4-7│ Cores 8-11│Cores12-15│ Cores 16-23 │
│  P-cores │  P-cores │  P-cores  │  Mixed   │   E-cores   │
├──────────┼──────────┼──────────┼──────────┼──────────────┤
│  signal  │ traffic  │  postgis │ convoy   │ OS + NATS +  │
│ ingress  │  oracle  │          │  brain   │ Valkey +     │
│ (Stream  │ (ONNX,   │(pgRouting│(Ollama   │ command-deck │
│  Proc)   │ sklearn, │ pgvector)│ CPU      │ + Prometheus │
│          │ OR-Tools)│          │ fallback)│ + Grafana    │
├──────────┼──────────┼──────────┼──────────┼──────────────┤
│  512 MB  │   4 GB   │   2 GB   │   2 GB   │  ~2 GB       │
│  0 VRAM  │ 409 VRAM │  0 VRAM  │  0 VRAM  │  0 VRAM     │
└──────────┴──────────┴──────────┴──────────┴──────────────┘

GPU (RTX 4070 8192 MB):
┌───────────────┬──────────┬───────────┬──────────────────┐
│ Ollama/Qwen   │ ONNX RT  │ CUDA      │ Headroom         │
│ 5632 MB       │ 409 MB   │ 307 MB    │ 1844 MB          │
│ (Priority 1)  │ (Pr. 2)  │ (System)  │ (Safety margin)  │
└───────────────┴──────────┴───────────┴──────────────────┘
```

---

## 8. Docker Daemon Configuration

```json
// /etc/docker/daemon.json (WSL2) or Docker Desktop settings
{
  "default-runtime": "nvidia",
  "runtimes": {
    "nvidia": { "path": "nvidia-container-runtime", "args": [] }
  },
  "storage-driver": "overlay2",
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" },
  "default-ulimits": {
    "nofile": { "Name": "nofile", "Hard": 65536, "Soft": 65536 }
  },
  "max-concurrent-downloads": 10,
  "max-concurrent-uploads": 5
}
```

---

## 9. Thermal Management

| Threshold | Action |
|---|---|
| GPU < 75C | Normal operation |
| GPU 75-85C | Fan ramp to max, monitor closely |
| GPU > 85C | **Alert fires** — thermal throttling imminent |
| GPU > 90C | Emergency: reduce Ollama context window or unload model |
| CPU > 95C | Emergency: reduce max_parallel_workers in PostgreSQL |

Ensure laptop is on a **cooling pad** or **elevated surface** for production workloads.

---

## 10. Verification Commands

```bash
# Run the automated checklist
./infra/scripts/os_checklist.sh

# Apply kernel tuning
sudo ./infra/scripts/os_tuning.sh

# Verify VRAM budget
./infra/scripts/vram_audit.sh

# Build and deploy
./infra/scripts/deploy.sh build
./infra/scripts/deploy.sh up

# Check status
./infra/scripts/deploy.sh status

# Run adversarial tests
python -m tests.adversarial --stress

# Sustained load test (5 minutes)
python -m tests.adversarial --continuous --duration 300

# View Grafana dashboard
open http://localhost:3000
```

---

## 11. Post-Deployment Smoke Test

1. Verify all containers healthy: `docker compose -f infra/compose.prod.yml ps`
2. Verify GPU VRAM: `./infra/scripts/vram_audit.sh` — used < 7 GB
3. Verify Ollama: `curl http://localhost:11434/api/tags` — model loaded
4. Verify convoy-brain: `curl http://localhost:8080/health` — `{"status": "ok"}`
5. Verify traffic-oracle: `curl http://localhost:8081/health` — `200 OK`
6. Verify Grafana: `http://localhost:3000` — dashboards rendering
7. Verify Prometheus: `http://localhost:9090/targets` — all targets UP
8. Run adversarial suite: `python -m tests.adversarial` — pass rate > 90%
