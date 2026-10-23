"""Configuration — fixed internal endpoints are config constants.

Environment overrides follow the repo conventions (infra/compose.yml):
VVIP_DATABASE_URL, VVIP_VALKEY_URL, VVIP_NATS_URL.  Database credentials are
NEVER hardcoded — the DSN is env-only and runners fail closed without it.

The traffic-oracle URL is validated by :func:`assert_internal_endpoint` at
load time (loopback / host.docker.internal allowlist), so a stray
environment override cannot redirect the twin seam at an arbitrary host.

The HTTP surface binds loopback by default (HOST env overrides): the deck
reaches the emulator through its same-origin /api/emulator proxy, and the
service writes events, sightings and Valkey inputs, so it must not answer on
a venue LAN. CORS origins come from EMULATOR_DECK_ORIGINS (comma-separated
http(s)://host[:port]); the default covers the deployed deck (:5273) and the
vite dev server (:5173).

Living city (ambient.py): EMULATOR_LIVING_CITY=1 starts the ambient city at
boot (0/unset: off; an operator can still start it from the deck),
EMULATOR_CITY_SEED picks the deterministic day program (default 1411,
Ahmedabad's founding year), and EMULATOR_LIVING_VIOLATORS=0 stops the city
driving its deliberate violators (~4 an hour, each a PENDING e-challan
recommendation in the human review queue; default on — the program itself
is unchanged).
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from event_emulator.safe_http import assert_internal_endpoint

__all__ = [
    "DEFAULT_DECK_ORIGINS",
    "Settings",
    "get_settings",
    "parse_deck_origins",
    "parse_flag",
]

# Deck origins allowed by CORS when EMULATOR_DECK_ORIGINS is unset.
DEFAULT_DECK_ORIGINS: tuple[str, ...] = (
    # deployed command-deck (infra/compose.argus-override.yml, :5273)
    "http://127.0.0.1:5273",
    "http://localhost:5273",
    # vite dev server (services/command-deck `bun dev`, :5173)
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def parse_deck_origins(raw: str) -> tuple[str, ...]:
    """Comma-separated origins → tuple; fail closed on anything but
    http(s)://host[:port] (no wildcard, path, query or credentials).

    The port is validated by READING ``parsed.port`` — urlparse only checks
    it then — so a non-numeric, negative or out-of-range port (and a bare
    trailing colon) refuses the whole list instead of slipping through.
    """
    origins: list[str] = []
    for item in raw.split(","):
        origin = item.strip().rstrip("/")
        if not origin:
            continue
        parsed = urlparse(origin)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError(
                f"EMULATOR_DECK_ORIGINS: not an origin: {origin!r}") from None
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.path
                or parsed.query or parsed.fragment or "*" in origin
                or parsed.netloc.endswith(":")
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError(f"EMULATOR_DECK_ORIGINS: not an origin: {origin!r}")
        origins.append(origin)
    if not origins:
        raise ValueError("EMULATOR_DECK_ORIGINS is set but lists no origin")
    return tuple(origins)


def parse_flag(raw: str | None) -> bool:
    """'1' / 'true' / 'yes' / 'on' → True; empty, '0' and anything else → False."""
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


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
    host: str = "127.0.0.1"                       # env HOST
    port: int = 8090                              # env PORT
    deck_origins: tuple[str, ...] = DEFAULT_DECK_ORIGINS  # env EMULATOR_DECK_ORIGINS

    # ── seam defaults ─────────────────────────────────────────────────────
    inputs_doc_ttl_sec: int = 30       # 3× the 5 s engine poll interval
    nats_emulated_subject: str = "corridor.traffic.emulated"

    # ── living city (ambient.py) ──────────────────────────────────────────
    living_city: bool = False          # env EMULATOR_LIVING_CITY (1 = start at boot)
    city_seed: int = 1411              # env EMULATOR_CITY_SEED
    living_violators: bool = True      # env EMULATOR_LIVING_VIOLATORS (0 = none)

    def validate_internal_endpoints(self) -> None:
        """Fail closed on an illegal endpoint override."""
        assert_internal_endpoint(self.traffic_oracle_url)


def get_settings() -> Settings:
    s = Settings()

    if os.environ.get("EMULATOR_TRAFFIC_ORACLE_URL"):
        s.traffic_oracle_url = os.environ["EMULATOR_TRAFFIC_ORACLE_URL"]
    if os.environ.get("EMULATOR_DECK_ORIGINS"):
        s.deck_origins = parse_deck_origins(os.environ["EMULATOR_DECK_ORIGINS"])
    s.living_city = parse_flag(os.environ.get("EMULATOR_LIVING_CITY"))
    if os.environ.get("EMULATOR_CITY_SEED", "").strip():
        s.city_seed = int(os.environ["EMULATOR_CITY_SEED"].strip())
    if os.environ.get("EMULATOR_LIVING_VIOLATORS", "").strip():
        s.living_violators = parse_flag(os.environ["EMULATOR_LIVING_VIOLATORS"])
    s.validate_internal_endpoints()
    return s
