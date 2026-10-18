"""Configuration loader for model-host.

Reads VVIP/models/configs/veer_models.yaml (models-stream-owned, C0) and
applies environment overrides. All outbound endpoints used anywhere in this
service are fixed config constants (see veer_models.yaml `services:`); this
service itself makes NO outbound HTTP calls at all — it only serves.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# Repo root (VVIP/) = four levels above this file (src/model_host/config.py)
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG = REPO_ROOT / "models" / "configs" / "veer_models.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"config not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


class Settings:
    """Typed view over veer_models.yaml + env overrides for model-host."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        cfg_path = Path(config_path or os.environ.get("VEER_MODELS_CONFIG", DEFAULT_CONFIG))
        cfg = _load_yaml(cfg_path)
        dec = cfg.get("decider", {})
        svc = cfg.get("services", {})

        self.host = os.environ.get("MODEL_HOST_BIND", "127.0.0.1")
        self.port = int(os.environ.get("MODEL_HOST_PORT", "8083"))
        # snapshot path: env wins, then yaml, then hard fail (48.58 GiB of
        # weights must come from the verified on-disk snapshot, never a copy)
        self.snapshot_path = Path(
            os.environ.get("DECIDER_SNAPSHOT_PATH", dec.get("snapshot_path", ""))
        )
        if not str(self.snapshot_path):
            raise ValueError("decider.snapshot_path missing in veer_models.yaml")
        self.device = dec.get("device", "cuda")
        self.offline = bool(dec.get("offline", True))
        self.max_questions = int(dec.get("max_questions_per_request", 6))
        self.max_state_tokens = int(dec.get("max_state_tokens", 8192))
        self.queue_depth = int(dec.get("queue_depth", 8))
        # p95 window for /decider/health (latency samples retained)
        self.latency_window = int(dec.get("latency_window_samples", 128))

        # Fixed internal endpoints of the platform (not user-supplied URLs).
        # Kept here for readiness reporting only; model-host never calls out.
        self.llama_url = os.environ.get("LLAMA_BASE_URL", svc.get("llama", "http://127.0.0.1:8084"))
        self.embedding_url = os.environ.get(
            "EMBEDDING_SERVICE_URL", svc.get("embedding", "http://127.0.0.1:8085")
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
