# model-host — VVIP T5 strategic tier (pplx-decider-v1-27b)

Serves the 27B decision model as typed questions (choice / noul / score) with
probability distributions. **Not** a chat model and **not** the jinja chat
template — decisions come from the readout head (249 letter-codes,
temperature 2.2076 baked into the snapshot's `decision_config.json`, never
overridden here).

## Run

```bash
# deployed: the systemd --user unit (Restart=always, under veer-models.target)
systemctl --user status veer-decider.service   # runs serve_models.sh --foreground decider
# by hand: the legacy bash supervisor, or the host directly
./infra/scripts/serve_models.sh start
PYTHONPATH=services/model-host/src \
  "$VEER_VENV_DIR"/model-host/bin/python -m model_host.app
```

`VEER_VENV_DIR` comes from `~/.config/veer/veer.env` (`scripts/veer_env.sh`);
on this box it is still the retired tree's `models/.venvs` (the
`VVIP/models` symlink) — `scripts/veer_env.sh` warns and names the
rebuild under `VVIP/.venvs`.

Cold load ≈5 min (48.58 GiB of BF16 weights into the GB10 unified pool).
Bind: `127.0.0.1:8083` (fixed internal endpoint; env `MODEL_HOST_BIND` /
`MODEL_HOST_PORT` for tests). Config: `models-config/configs/veer_models.yaml`
(decider section) — the snapshot path is authoritative.

**Load-path rule (C1):** the model is loaded via the HF snapshot *directory
path*. Every file there is a double symlink into `../../blobs`; a naive
`cp -r` produces dangling 380 KiB stubs. Never copy the tree.

## API (contract C1)

- `GET /decider/health` → `{loaded, loading, load_error, resident_rss_gib,
  cuda_reserved_gib, p95_decide_latency_ms, queue_depth, decides_total,
  decides_failed, model}`
- `POST /warmup` → waits for load, runs a warm decision (idempotent).
- `POST /decide` `{request_id?, state, questions:{name:{type, instructions,
  options?|scale?}}}` — ≤6 questions per call (422 beyond), typed only.
  All questions in one request are batched into a **single forward pass**.
  Responses: `{request_id?, answers:{name:{value, probability, confidence,
  distribution}}, state_tokens, latency_ms}`.
  - `choice` → options: list[str] | dict[label→description] (≤249)
  - `noul` → P(true); confidence is `null` (the model emits none for noul)
  - `score` → scale: ≥2 level descriptions; value = expected level
- Errors: `429` + `Retry-After: 5` when 8 requests are already queued
  (serialized compute — one forward at a time), `503` while loading,
  `413` when the encoded branch exceeds the 8192-token hard cap (no
  truncation by design), `422` on malformed questions.
- `GET /metrics` → Prometheus exposition (`model_host_*`).

This service makes **no outbound HTTP calls**; every endpoint it needs is a
fixed internal platform constant.

## Degradation contract

convoy-brain polls `/decider/health`; when `p95_decide_latency_ms` stays
above 3000 ms it falls back to Gemma-only tactical mode and alerts the
operator (audit-logged brain-side). Checkpoints are event-driven (≥5 s
apart) — never per GPS tick.
