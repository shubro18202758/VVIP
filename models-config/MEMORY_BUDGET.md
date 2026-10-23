# VVIP Model Stack — Resident Memory Budget (GB10 unified pool)

Owner: models stream. Source of truth for "what may be resident and how much
it costs". Update the **Measured** column whenever a host or quantization
changes; `/decider/health` (`resident_rss_gib`, `cuda_reserved_gib`),
`/proc/<pid>/status` (VmRSS/VmHWM), and `/proc/meminfo` are the measurement
interfaces.

## Pool

| Item | Value | Source |
|---|---|---|
| Total unified memory | **121.63 GiB** (MemTotal 127,533,976 kB; the configs plan with a conservative 121.44 GiB pool) | `/proc/meminfo` — nvidia-smi reports `memory.total` as **N/A** on GB10, so /proc/meminfo is the only source |
| Eviction policy | **none** — every host is resident forever (keep_alive semantics below) | perfPlan |
| Disk | 2,248 GiB free of 3,754 GiB | `df -BG /` (model manifest) |

## Resident processes

| # | Resident process | Weights | Process budget (planned) | Measured (this session, 2026-10-12) |
|---|---|---|---|---|
| 1 | model-host: pplx-decider-v1-27b BF16 (11 shards, 52,170,802,640 B + 2.6 MB readout) | 48.58 GiB | 55 GiB (weights + ≤8k-state activations — estimate) | **measured 2026-10-12**: `cuda_reserved_gib` **48.99**, VmRSS 1.66 / VmHWM 5.92 GiB (weights migrate to the GPU side of the pool), cold load ≈5 min, warm decide **460–477 ms @ 138-token state / 1 question** (3 probes; first-ever call pays ≈4.7 s CUDA warmup — keep warmup in smoke), 19 decides / 0 failed |
| 2 | llama-server: aditi-ps18-v7h Gemma 4 12B Q4_K_M (GGUF 7,208,382 KiB), `-c 16384 -np 2` (2 slots × 8192) | 6.87 GiB | 9.5 GiB (weights + KV/compute for 2×8192) | **VmHWM 7,602,840 kB = 7.25 GiB** peak after first completion (llama.cpp migrates weights into the GPU side of the pool; idle VmRSS reads far lower) |
| 3 | embedding-service: pplx-embed-v2-late-0.6b (multivector MaxSim) | 1.11 GiB | 2.5 GiB incl. torch CUDA ctx | owned by embeddings stream; verified serving via models-owned launcher (their `-m` entrypoint is broken on uvicorn 0.54 — async factory never awaited); cold start ≈36 s incl. both encoders |
| 4 | embedding-service: bge-m3 (dense ANN, unit-norm 1024-d) | 1.06 GiB | 2.5 GiB | same process as row 3 (encoders co-resident); manifest: 2.1 s load, (3,1024) unit-norm |
| 5 | traffic-oracle: DSTGAT ONNX (CPU fallback kept) | — | 0.4 GiB | unchanged from v1 budget |
| | **MODEL SUBTOTAL** | | **≈69.9 GiB (58 %)** | |
| | PostGIS 2.0 + Valkey 0.5 + NATS 0.5 + traffic-oracle 3.5 (MEMORY_GUARD_MB=3500) + convoy-brain 1.5 + browser/dev 4 | | ≈12 GiB | |
| | **TOTAL PLANNED** | | **≈82 GiB → ≈39 GiB (32 %) headroom** | |

Optional 9B aux embedder (pplx-embed-v2-late-9b, 16.79 GB) stays OFF by
default (`veer_models.yaml → embeddings.aux_9b.enabled: false`); enabling it
still fits at ≈23 GiB spare.

## Not counted in the planned set

- **ollama :11434** runs externally (not ours, never restarted by us) and
  currently hosts `qwen3.5:9b-q4_K_M` (6.6 GB) plus our debug side-load
  `gemma-vvip:12b` (7.4 GB, `models-config/Modelfile.gemma_vvip`). If the ollama
  daemon loads either on this machine, subtract its runner RSS from headroom.
- The `veer-*` throwaway migration-test containers other streams create are
  transient.

## keep_alive / residency semantics (per host)

| Host | Mechanism |
|---|---|
| model-host :8083 | Loads once at boot (≈285 s cold; ~5 min observed this session) into a supervised process; no eviction path exists; restart-on-exit via `serve_models.sh` |
| llama-server :8084 | llama.cpp has **no model eviction** — residency == process lifetime; supervised restart-on-exit. 2 parallel slots (`-np 2`), 8192 ctx per slot (`-c 16384` total) |
| embedding-service :8085 | Encoders resident in the service process (embeddings stream); supervised restart-on-exit |
| ollama :11434 (debug side-load only) | `OLLAMA_KEEP_ALIVE=-1` required so `gemma-vvip:12b` is never evicted (recorded in `veer_models.yaml → ollama`) |

The v1 asyncio.Lock VRAM-serialization regime is retired: compute-only
serialization replaces it (decider compute lock + queue 8 in model-host,
llama-server slot scheduling, embedder batch lock in the embeddings service).

## Context budgets

- Decider state docs: ≤400 tokens/event, ≤1500 pre-movement, **hard cap 8192**
  (model-host rejects larger with HTTP 413; the autojev `prepare()` raises —
  no truncation by design). Bidirectional attention → no KV/prefix reuse;
  prefix caching is useless and not attempted.
- Gemma: 8192/slot × 2 slots = 16,384 tokens total KV. Tool results truncated
  4 KB; 200-segment agent caps unchanged.
- Decider checkpoints are event-driven with ≥5 s spacing (never per-GPS-tick);
  ≤6 typed questions batched into a single POST /decide.

## Known gaps / measurement obligations

- Decider activation overhead is an ESTIMATE until `/decider/health` RSS is
  read under production-size (1500-token) states — re-budget row 1 from
  measurement (risk register item #1).
- Gemma tok/s on GB10 and json_schema support in llama build 69bf643 are
  smoke-measured by `scripts/smoke_models.sh` + `/metrics`; not yet recorded
  as steady-state numbers.
- Unified-memory bandwidth contention (27B forward concurrent with Gemma
  generation and embedder batches) is unmeasured; event-driven decider
  cadence and embedder batch windows are the designed mitigations.
