# models-config — tracked, non-weight model-host files

Everything the host model stack needs that is *not* a model weight or a
virtualenv lives here, under version control. It replaces the untracked
`VVIP/models` symlink into the retired tree (`~/Desktop/Veer/VVIP/models`),
which used to be load-bearing.

| File | Used by |
|---|---|
| `configs/veer_models.yaml` | model-host (`VEER_MODELS_CONFIG`, exported by `infra/scripts/serve_models.sh`), smoke/drift scripts; single source of model-stack truth (C0) |
| `configs/flow_forecaster.yaml` | GAT forecaster hyper-parameters (documentation of the dormant DSTGAT rung) |
| `embedding_launcher.py` | `serve_models.sh` boots the embedding-service (:8085) through it |
| `model-host-requirements.txt` | `serve_models.sh build` → `$VEER_VENV_DIR/model-host` |
| `MEMORY_BUDGET.md` | GB10 unified-memory budget (planned + measured) |
| `Modelfile.gemma_vvip` | optional ollama debug side-load of the Gemma GGUF |

The Tier-1 anomaly thresholds that used to sit in `models/configs/
anomaly_detector.yaml` now ship with the service that applies them:
`services/signal-ingress/config/anomaly_detector.yaml` (compiled into the
binary and installed in its image).

## What stays outside the repo (explicit host paths)

Weights and venvs are large and host-specific. `serve_models.sh` takes them
from environment variables, normally written once by
`scripts/veer_env.sh --write` into `~/.config/veer/veer.env` (also the
`EnvironmentFile=` of the systemd units in `infra/systemd/`):

| Variable | What |
|---|---|
| `DECIDER_SNAPSHOT_PATH` | pplx-decider-v1-27b HF snapshot dir (48.58 GiB, never copied) |
| `LLAMA_SERVER_BIN` | llama.cpp `llama-server` binary |
| `GEMMA_GGUF` | aditi-ps18-v7h Gemma 4 12B Q4_K_M GGUF |
| `BGE_M3_PATH` | bge-m3 dense encoder snapshot |
| `VEER_VENV_DIR` | root of the `model-host`, `embedding`, `migrate` venvs (default `VVIP/.venvs`; build with `serve_models.sh build`) |

`scripts/veer_up.sh` refuses to start the model units when any of these
paths is missing, naming the variable to fix.
