"""Compile-time configuration for the embedding service.

Two classes of setting live here:

1. FIXED INTERNAL ENDPOINTS — http://127.0.0.1:{8081,8083,8084,8085} are
   platform constants (architecture contract C0/C8), overridable only via
   environment variables read at import time. They are NEVER taken from
   request payloads or other external input, which is what makes them
   eligible for the internal-host allowlist in ``security.py``.

2. MODEL / RETRIEVAL CONSTANTS — embedding dimensions, token-vector caps and
   hybrid-retrieval defaults from the architecture plan (T1 MEMORY, C3).

No credentials live here or anywhere in source: the database DSN is read
from the environment only (C12).
"""

from __future__ import annotations

import os

# ─── Fixed internal endpoints (platform compile-time constants, C0) ──────────
# Each may be overridden by env var at process start, never per-request.
EMBEDDING_SERVICE_URL = os.environ.get(
    "EMBEDDING_SERVICE_URL", "http://127.0.0.1:8085"
)
TRAFFIC_ORACLE_URL = os.environ.get(
    "TRAFFIC_ORACLE_URL", "http://127.0.0.1:8081"
)

# Hosts that fixed internal endpoints are allowed to point at. Only URLs
# constructed from the constants above may use these hosts; anything coming
# from external input is rejected outright regardless of host.
FIXED_INTERNAL_HOSTS: frozenset[str] = frozenset(
    {"127.0.0.1", "localhost", "host.docker.internal", "::1"}
)

# Default listen address for the service itself (host-mode gate, C8/C9).
LISTEN_HOST = os.environ.get("EMBEDDING_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("EMBEDDING_LISTEN_PORT", "8085"))

# ─── Database (credentials env-only — no literals anywhere) ──────────────────
# Same DSN variable family the rest of the stack uses; see infra/compose.yml.
DATABASE_DSN_ENV_VARS = ("VEER_DATABASE_DSN", "VVIP_DATABASE_URL", "DATABASE_URL")


def database_dsn() -> str | None:
    """Return the Postgres DSN from the environment, or None if unset."""
    for var in DATABASE_DSN_ENV_VARS:
        value = os.environ.get(var)
        if value:
            return value
    return None


# ─── Models ───────────────────────────────────────────────────────────────────
# Primary late-interaction encoder: pplx-embed-v2-late (verified on this
# machine: MultiVectorEncoder, per-token 128-d vectors, MaxSim similarity).
LATE_MODEL_ID = os.environ.get(
    "LATE_EMBED_MODEL", "perplexity-ai/pplx-embed-v2-late-0.6b"
)
# Aux dense encoder: bge-m3 local snapshot (verified: (N,1024) unit-norm).
DENSE_MODEL_PATH = os.environ.get("BGE_M3_PATH", "/home/sandy050405/models/bge-m3")

# The 9B aux embedder is a config slot, default OFF (architecture plan T1).
AUX_LATE_MODEL_ID = os.environ.get("AUX_LATE_EMBED_MODEL", "")

# ─── Embedding geometry (pplx-embed-v2-late contract) ────────────────────────
TOKEN_VEC_DIM = 128          # per-token vector width
DENSE_DIM = 1024             # bge-m3 dense width
MAX_TOKEN_VECS_PER_DOC = 64  # perf plan: 64 x 128 fp16 token-vecs/doc

# ─── Hybrid retrieval defaults (C3: dense ANN recall_k=100 -> MaxSim top-20) ─
DEFAULT_RECALL_K = 100
DEFAULT_TOP_K = 20
MAX_TOP_K = 50
MAX_BATCH_DOCS = 64          # encoder batch ceiling per /embed/documents call
MAX_BODY_CHARS = 8000        # truncate pathological documents before encoding

# ─── Kinds (the memory-side half of the leak guard; see README contract) ─────
MEMORY_DOC_KINDS: tuple[str, ...] = (
    "incident",
    "aar",
    "decision",
    "scenario_run",
    "pattern",
)
INCIDENT_KINDS: tuple[str, ...] = (
    "congestion_spike",
    "route_blockage",
    "signal_failure",
    "protest",
    "accident",
    "weather",
    "vvip_hold",
    "other",
)
INCIDENT_SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
INCIDENT_SOURCES: tuple[str, ...] = ("live", "scenario_run", "import")
