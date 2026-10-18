"""Application configuration via environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuration loaded from environment variables with sensible defaults."""

    DATABASE_URL: str = "postgresql://corridor_admin:dev_changeme@localhost:5432/corridor_db"
    VALKEY_URL: str = "redis://localhost:6379"
    NATS_URL: str = "nats://localhost:4222"
    ONNX_MODEL_PATH: str = "models/onnx/flow_forecaster.onnx"
    BATCH_SIZE: int = 100
    BATCH_TIMEOUT_MS: int = 100

    # ML runtime constraints
    ML_THREADS: int = 4
    ML_INTEROP_THREADS: int = 2
    MEMORY_GUARD_MB: int = 3500

    # Model paths
    ETA_MODEL_PATH: str = "models/onnx/eta_predictor.onnx"

    # Zero-shot foundation forecaster (t0-alpha ONNX INT8, Apache-2.0):
    # None keeps the rung dormant. Deliberately a SEPARATE slot from
    # ONNX_MODEL_PATH (the dormant DSTGAT export) — setting it makes the
    # foundation rung the preferred model rung in FlowForecaster.predict
    # (support gate -> foundation -> DSTGAT -> heuristic). The artifact
    # lives outside git at VVIP/models-local/t0/ (see its NOTICE) — point
    # this at an absolute path, e.g.
    # /repo/VVIP/models-local/t0/t0-alpha-grouped-int8.onnx
    FOUNDATION_MODEL_PATH: str | None = None

    # Out-of-support gate for the flow forecaster: None -> the committed
    # package default at traffic_oracle/predict/data/support_bounds.json;
    # an explicit path overrides it (point it at a re-recorded calibration
    # file once training lands).
    FORECAST_SUPPORT_BOUNDS_PATH: str | None = None

    # RL training limits
    RL_MAX_EPISODES: int = 1000
    RL_MAX_SIGNALS: int = 20

    model_config = {"env_prefix": "VVIP_"}
