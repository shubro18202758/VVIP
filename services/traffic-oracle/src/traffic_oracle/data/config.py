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

    # RL training limits
    RL_MAX_EPISODES: int = 1000
    RL_MAX_SIGNALS: int = 20

    model_config = {"env_prefix": "VVIP_"}
