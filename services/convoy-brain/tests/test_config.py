"""Config resolution tests — env > veer_models.yaml > compile-time defaults."""

from __future__ import annotations

from pathlib import Path

from convoy_brain import config
from convoy_brain.config import (
    AdaptiveParams,
    load_service_urls,
)


class TestServiceUrlResolution:
    def test_defaults(self, tmp_path):
        # Isolated from the repo's real veer_models.yaml (owned by models
        # stream): with no config file and no env, compile-time defaults win.
        urls = load_service_urls(env={}, config_path=tmp_path / "absent.yaml")
        assert urls.model_host == "http://127.0.0.1:8083"
        assert urls.llama == "http://127.0.0.1:8084"
        assert urls.embedding == "http://127.0.0.1:8085"
        assert urls.traffic_oracle == "http://localhost:8081"
        assert urls.source == "defaults"

    def test_repo_models_config_parses(self):
        # Integration with the models stream's C0 config: when present it must
        # yield the same three service endpoints the contract fixes.
        urls = load_service_urls(env={})
        assert urls.model_host in ("http://127.0.0.1:8083", "http://localhost:8083")
        assert urls.llama in ("http://127.0.0.1:8084", "http://localhost:8084")
        assert urls.embedding in ("http://127.0.0.1:8085", "http://localhost:8085")

    def test_env_overrides_everything(self):
        urls = load_service_urls(env={
            "MODEL_HOST_URL": "http://localhost:8083",
            "LLAMA_BASE_URL": "http://localhost:8084",
            "EMBEDDING_SERVICE_URL": "http://localhost:8085",
        })
        assert urls.model_host == "http://localhost:8083"
        assert urls.llama == "http://localhost:8084"
        assert urls.embedding == "http://localhost:8085"
        assert "env" in urls.source

    def test_yaml_services_block(self, tmp_path: Path):
        cfg = tmp_path / "veer_models.yaml"
        cfg.write_text(
            "models:\n"
            "  decider:\n    path: /some/path\n"
            "services:\n"
            "  model_host: http://localhost:8083\n"
            "  llama: http://localhost:8084\n"
            "  embedding: http://localhost:8085\n"
            "another:\n"
            "  key: value\n"
        )
        urls = load_service_urls(env={}, config_path=cfg)
        assert urls.model_host == "http://localhost:8083"
        assert urls.llama == "http://localhost:8084"
        assert urls.embedding == "http://localhost:8085"
        assert "veer_models.yaml" in urls.source

    def test_env_beats_yaml(self, tmp_path: Path):
        cfg = tmp_path / "veer_models.yaml"
        cfg.write_text("services:\n  model_host: http://localhost:8083\n")
        urls = load_service_urls(env={"MODEL_HOST_URL": "http://127.0.0.1:8083"}, config_path=cfg)
        assert urls.model_host == "http://127.0.0.1:8083"

    def test_missing_config_file_falls_back(self, tmp_path: Path):
        urls = load_service_urls(env={}, config_path=tmp_path / "nope.yaml")
        assert urls.model_host == "http://127.0.0.1:8083"


class TestAdaptiveParamsFloors:
    def test_protocol_floors(self):
        params = AdaptiveParams(advance_closure_sec=30)
        assert params.floored_advance_closure_sec("Z+") == 180
        assert params.floored_advance_closure_sec("Z") == 120
        assert params.floored_advance_closure_sec("Y") == 60
        assert params.floored_advance_closure_sec("X") == 0
        # already above the floor -> unchanged
        assert AdaptiveParams(advance_closure_sec=200).floored_advance_closure_sec("Z+") == 200

    def test_baseline_is_fail_closed_constants(self):
        params = config.baseline_params()
        assert params.advance_closure_sec == config.BASE_ADVANCE_CLOSURE_SEC == 120
        assert params.congestion_spike_threshold == 0.8
        assert params.gps_poll_interval_sec == 1.0
        assert params.scoring_weights == {
            "time": 0.3, "disruption": 0.4, "security": 0.2, "complexity": 0.1,
        }
        assert params.source == "baseline"


class TestDeadParamRemoval:
    def test_activation_lookahead_is_gone(self):
        assert not hasattr(config, "ACTIVATION_LOOKAHEAD_SEC")
