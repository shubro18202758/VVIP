"""Configuration — fixed internal endpoints are config constants.

Environment overrides follow the repo conventions (infra/compose.yml):
VVIP_DATABASE_URL, VVIP_VALKEY_URL, VVIP_NATS_URL.  Database credentials are
NEVER hardcoded — the DSN is env-only and runners fail closed without it.

The traffic-oracle URL is validated by :func:`assert_internal_endpoint` at
load time (loopback / host.docker.internal allowlist), so a stray
environment override cannot redirect the twin seam at an arbitrary host.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from event_emulator.safe_http import assert_internal_endpoint

__all__ = ["Settings", "get_settings"]


class Settings(BaseSettings):
    """Emulator settings; every value overridable by env, none secret."""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # ── fixed internal endpoints (config constants — contract C5/C6) ──────
    traffic_oracle_url: str = "http://localhost:8081"
    valkey_url: str = Field(default="redis://localhost:6379",
                            validation_alias="VVIP_VALKEY_URL")
    nats_url: str = Field(default="nats://localhost:4222",
                          validation_alias="VVIP_NATS_URL")
    # env-only; no default with credentials (repo convention, scripts/migrate.py)
    database_url: str | None = Field(default=None,
                                     validation_alias="VVIP_DATABASE_URL")

    # ── emulator HTTP surface ─────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8090
    deck_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")

    # ── seam defaults ─────────────────────────────────────────────────────
    inputs_doc_ttl_sec: int = 30       # 3× the 5 s engine poll interval
    nats_emulated_subject: str = "corridor.traffic.emulated"

    def validate_internal_endpoints(self) -> None:
        """Fail closed on an illegal endpoint override."""
        assert_internal_endpoint(self.traffic_oracle_url)


def get_settings() -> Settings:
    s = Settings()
    import os

    if os.environ.get("EMULATOR_TRAFFIC_ORACLE_URL"):
        s.traffic_oracle_url = os.environ["EMULATOR_TRAFFIC_ORACLE_URL"]
    s.validate_internal_endpoints()
    return s
