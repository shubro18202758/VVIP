"""Convoy Brain configuration — two-tier hierarchy endpoints and constants.

Tier layout (see VVIP architecture):
    T5 strategic — pplx-decider-v1-27b served by model-host  (default :8083)
    T4 tactical  — aditi-ps18-v7h Gemma 4 12B on llama-server (default :8084)
    embeddings   — pplx-embed-v2-late-0.6b service            (default :8085)

Service URLs are resolved in this order (contract C0):
    1. Environment variables MODEL_HOST_URL / LLAMA_BASE_URL / EMBEDDING_SERVICE_URL
    2. The ``services:`` block of VVIP/models/configs/veer_models.yaml
       (owned by the models stream; read-only for us)
    3. The compile-time defaults below.

These endpoints are platform configuration constants, NOT user-supplied URLs;
:class:`convoy_brain.http_guard.assert_platform_service_url` enforces that they
are http(s) and loopback/host.docker.internal only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Compile-time defaults (the platform's own fixed internal endpoints)
# ---------------------------------------------------------------------------

DEFAULT_MODEL_HOST_URL = "http://127.0.0.1:8083"
DEFAULT_LLAMA_URL = "http://127.0.0.1:8084"
DEFAULT_EMBEDDING_URL = "http://127.0.0.1:8085"
DEFAULT_TRAFFIC_ORACLE_URL = "http://localhost:8081"
DEFAULT_VALKEY_URL = "redis://localhost:6379"

# Repo root discovery: <repo>/VVIP/services/convoy-brain/src/convoy_brain/config.py
_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parents[4]  # src/convoy_brain -> convoy-brain -> services -> VVIP -> root
MODELS_CONFIG_PATH = _REPO_ROOT / "VVIP" / "models" / "configs" / "veer_models.yaml"


# ---------------------------------------------------------------------------
# Two-tier runtime constants
# ---------------------------------------------------------------------------

#: Tactical tier model identity (served by llama-server on LLAMA_BASE_URL).
GEMMA_MODEL_ID = "aditi-ps18-v7h"

#: Strategic tier model identity (served by model-host on MODEL_HOST_URL).
DECIDER_MODEL_ID = "pplx-decider-v1-27b"

# -- Tactical-tier generation budgets (latency contract) -----------------------
# GB10 generates ~18-21 tok/s on the 12B Q4_K_M tactical model, so every
# unbounded generation is a latency bomb (a 2048-token ramble = ~110 s).
# Per-call wall bound and per-call-site token budgets keep the worst case
# inside the 60 s suite latency budget and the 120 s turn cap.
LLAMA_CALL_TIMEOUT_SEC = 30.0     # per tactical-call HTTP wall bound
TACTICAL_SELECT_MAX_TOKENS = 48   # tool-selection reply (one JSON name)
TACTICAL_ARGS_MAX_TOKENS = 192    # argument drafting (schema-constrained; worst ≈ 10 s)
TACTICAL_FINAL_MAX_TOKENS = 384   # final structured answer

# -- Decider cadence / degradation (event-driven, never per-GPS-tick) --------
DECIDER_MIN_CHECKPOINT_INTERVAL_SEC = 5.0   # >=5 s between checkpoints per movement
DECIDE_TIMEOUT_SEC = 20.0                   # per POST /decide (bounded by /decider/health p95)
DECIDER_HEALTH_TIMEOUT_SEC = 3.0
DECIDER_HEALTH_POLL_SEC = 15.0
DECIDER_P95_DEGRADE_MS = 3000               # health p95 above this -> Gemma-only tactical fallback
DECIDER_PROBE_TIMEOUT_SEC = 3.0             # probe-decide wall bound; a healthy tier must beat the p95 line

# -- Confidence gating --------------------------------------------------------
# CALIBRATED 2026-10-19 against the live 27B (POST :8083/decide with the
# exact pre-movement question set): route_strategy confidence 0.5033,
# disruption_tolerance 0.4696 (the noul question carries no confidence and
# is excluded from the gate — see decider.py). The previous 0.6 floor
# labeled every live pre-movement checkpoint low_confidence, which also
# permanently blocked the tier-0 cache write side. 0.45 sits below both
# measured values; do NOT go lower without new measurements. RE-MEASURE if
# the decider model ever changes. Env-overridable at process start
# (VVIP_CONFIDENCE_FLOOR) like the service URLs.
CONFIDENCE_FLOOR = float(os.environ.get("VVIP_CONFIDENCE_FLOOR", "0.45"))

# -- Decider novelty latch (confident-but-novel abstention) -------------------
# When the strategic tier answers CONFIDENTLY, verify the situation is
# represented in recent memory via the embedding service (/search); if
# nothing similar is on record, the confident answer is withheld
# (escalated to the tactical tier + operator) instead of trusted
# off-distribution. These are brain-platform constants like CONFIDENCE_FLOOR
# — they do NOT belong in protocol_params_gen (shared-schema owned) or the
# adaptive C4 surface.
NOVELTY_SIMILARITY_FLOOR = 0.35   # normalized MaxSim scale (0..1) — the
                                  # fraction of a query's tokens that must
                                  # find a strong match in the best memory
                                  # doc for the situation to count as "on
                                  # record". CALIBRATED 2026-10-19: live
                                  # raw MaxSim is a SUM over query tokens
                                  # (self-match of the 172-char gate state
                                  # doc measured 55.40; embedding README
                                  # shows 4.19 for a short query), so the
                                  # raw scale is unbounded by query length
                                  # and a 0.35/0.75 raw comparison passed
                                  # ANY maxsim-scored doc. MemoryClient now
                                  # normalizes (raw / query_token_count)
                                  # before these floors are applied —
                                  # re-calibrate only against normalized
                                  # scores.
NOVELTY_SEARCH_TIMEOUT_SEC = 2.0  # wall bound for one POST /search (wait_for)
NOVELTY_MEMORY_TOP_K = 5          # results requested per search
NOVELTY_MEMO_TTL_SEC = 30.0       # memoize one search per (movement, state_doc)

# -- Decision-memory feeder (C3 write side) ------------------------------------
# The novelty latch and the tier-0 gate READ traffic.memory_docs through
# NOVELTY_SEARCH_KINDS, which includes "decision" — the decider's own
# checkpoints. Nothing else in the stack feeds that kind, so on live traffic
# the corpus never gains a decision doc and every checkpoint reads
# miss_novel forever. After a SERVED /decide (tier "decider" — including a
# low-confidence one, which is still served with a Gemma draft), the brain
# upserts the checkpoint's state document into the corpus as kind=decision.
# Recording happens AFTER the checkpoint's own /search, so the first
# occurrence of a situation can still be novel (Wave-1 latch semantics are
# preserved; repeats become "on record"). The record never carries answers
# into the tier-0 ANSWER cache — which answers may be cached stays gated in
# decider.py (confident + memory-covered only).
DECISION_MEMORY_RECORD_TIMEOUT_SEC = 5.0  # wall bound for one POST /embed/documents

# -- Decider tier-0 gate (routine-checkpoint cache in front of the 27B) -------
# A ROUTINE checkpoint — same movement, same trigger, same canonical question
# set, and a situation whose best memory maxsim clears TIER0_ROUTINE_SIMILARITY_FLOOR —
# is answered from a small TTL-bounded cache of recent CONFIDENT decisions,
# making ZERO /decide calls to the model host. Novelty forces a miss (the
# Wave-1 latch semantics hold read-side AND write-side: a latched or
# low-confidence answer never enters the cache). Same brain-platform
# constants doctrine as the novelty block above: ctor-injectable on
# DeciderBrain, NOT protocol_params_gen surface.
TIER0_ROUTINE_SIMILARITY_FLOOR = 0.75  # NORMALIZED MaxSim scale (0..1 — see
                                       # the NOVELTY block's 2026-10-19
                                       # calibration note: MemoryClient
                                       # divides the raw sum by
                                       # query_token_count before floors are
                                       # applied). Deliberately STRICTLY
                                       # TIGHTER than NOVELTY_SIMILARITY_FLOOR
                                       # so that "routine" is a strictly
                                       # stronger condition than "not novel":
                                       # a hit requires the situation to
                                       # strongly resemble the remembered
                                       # one, not merely to fail the novelty
                                       # test. Live reference points
                                       # (2026-10-19, gate state doc):
                                       # identical-repeat self-match ≈ 0.87+
                                       # normalized (raw 55.40).
TIER0_CACHE_TTL_SEC = 300.0            # staleness bound for a cached routine answer
TIER0_CACHE_MAX_ENTRIES = 128          # FIFO bound — long-lived processes must not leak
TIER0_ELIGIBLE_TRIGGERS = frozenset({
    "congestion_threshold_crossed",
    "incident_detected",
    "situation_changed",
    "phase_transition",
    "pre_movement",
})  # operator_chat is EXCLUDED in v1: the orchestrator trusts only
    # tier == "decider" for tool selection, so a cache hit there would
    # silently hand tool selection to Gemma while skipping the 27B.


def tier0_enabled(env: dict[str, str] | None = None) -> bool:
    """Kill switch for the tier-0 gate; default ON.

    Reads DECIDER_TIER0_DISABLE truthiness ("1"/"true", case-insensitive).
    One tested helper instead of scattered os.environ reads — config.py
    reads env only for URLs otherwise; the env var is the documented
    off-switch (flipping it and restarting preserves today-behavior).
    """
    env = env if env is not None else dict(os.environ)
    return env.get("DECIDER_TIER0_DISABLE", "").strip().lower() not in ("1", "true")

# -- Decider state-document token budgets (contract T5) -----------------------
STATE_TOKENS_PRE_MOVEMENT = 1500
STATE_TOKENS_PER_EVENT = 400
STATE_TOKENS_HARD_CAP = 8192

# -- Adaptive-parameter fail-closed baselines (contract C4) -------------------
# SINGLE SOURCE OF TRUTH (contract C1): shared/schemas/protocol_params.json,
# materialized into convoy_brain.protocol_params_gen by
# scripts/export_protocol_params.py.  A per-service pytest regenerates the
# module and asserts equality — the two services can never silently fork.
# The values mirror today's constants in live_escort.py; the adaptive service
# may override them per event/movement, but these are always the floor of
# last resort. ACTIVATION_LOOKAHEAD_SEC was dead code and is intentionally
# absent.
from convoy_brain.protocol_params_gen import (  # noqa: E402
    ADVANCE_CLOSURE_FLOOR_SEC,
    BASELINE_ADVANCE_CLOSURE_SEC,
    BASELINE_CONGESTION_SPIKE_THRESHOLD,
    BASELINE_GPS_POLL_INTERVAL_SEC,
)
from convoy_brain.protocol_params_gen import (  # noqa: E402
    BASELINE_SCORING_WEIGHTS,
)

# Backward-compatible names used across the brain and its tests.
BASE_CONGESTION_SPIKE_THRESHOLD = BASELINE_CONGESTION_SPIKE_THRESHOLD
BASE_GPS_POLL_INTERVAL_SEC = BASELINE_GPS_POLL_INTERVAL_SEC
BASE_ADVANCE_CLOSURE_SEC = BASELINE_ADVANCE_CLOSURE_SEC
BASE_SCORING_WEIGHTS: dict[str, float] = dict(BASELINE_SCORING_WEIGHTS)

# -- Tool-result truncation ----------------------------------------------------
TOOL_RESULT_TRUNCATE_BYTES = 4096


# ---------------------------------------------------------------------------
# Service URL resolution
# ---------------------------------------------------------------------------


@dataclass
class ServiceUrls:
    """Resolved platform service endpoints."""

    model_host: str = DEFAULT_MODEL_HOST_URL
    llama: str = DEFAULT_LLAMA_URL
    embedding: str = DEFAULT_EMBEDDING_URL
    traffic_oracle: str = DEFAULT_TRAFFIC_ORACLE_URL
    valkey: str = DEFAULT_VALKEY_URL
    source: str = "defaults"


def _parse_services_block_yaml(text: str) -> dict[str, str]:
    """Extract the flat ``services:`` mapping from veer_models.yaml.

    Deliberately dependency-free: we only need ``services.model_host``,
    ``services.llama`` and ``services.embedding`` — scalar string values at
    most two levels deep. Nested maps under other top-level keys are ignored.
    If PyYAML happens to be installed we prefer it for correctness.
    """
    try:
        import yaml  # type: ignore[import-not-found]

        data = yaml.safe_load(text) or {}
        services = data.get("services") or {}
        if isinstance(services, dict):
            return {k: str(v) for k, v in services.items() if isinstance(v, (str, int, float))}
        return {}
    except ImportError:
        pass

    # Minimal fallback parser: collect "key: value" pairs inside the services block.
    found: dict[str, str] = {}
    in_services = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0:
            in_services = stripped == "services:"
            continue
        if not in_services or indent > 2:
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        value = value.strip().strip("'\"")
        if value and not value.startswith("{") and not value.startswith("["):
            found[key.strip()] = value
    return found


def _read_models_config(path: Path = MODELS_CONFIG_PATH) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return _parse_services_block_yaml(text)


def load_service_urls(
    env: dict[str, str] | None = None,
    config_path: Path = MODELS_CONFIG_PATH,
) -> ServiceUrls:
    """Resolve service URLs: env > veer_models.yaml ``services:`` > defaults."""
    env = env if env is not None else dict(os.environ)
    yaml_services = _read_models_config(config_path)

    model_host = (
        env.get("MODEL_HOST_URL")
        or yaml_services.get("model_host")
        or DEFAULT_MODEL_HOST_URL
    )
    llama = (
        env.get("LLAMA_BASE_URL")
        or yaml_services.get("llama")
        or DEFAULT_LLAMA_URL
    )
    embedding = (
        env.get("EMBEDDING_SERVICE_URL")
        or yaml_services.get("embedding")
        or DEFAULT_EMBEDDING_URL
    )
    traffic_oracle = (
        env.get("TRAFFIC_ORACLE_URL")
        or yaml_services.get("traffic_oracle")
        or DEFAULT_TRAFFIC_ORACLE_URL
    )
    valkey = (
        env.get("VVIP_VALKEY_URL")
        or env.get("VALKEY_URL")
        or DEFAULT_VALKEY_URL
    )

    sources = []
    if env.get("MODEL_HOST_URL") or env.get("LLAMA_BASE_URL") or env.get("EMBEDDING_SERVICE_URL"):
        sources.append("env")
    if yaml_services:
        sources.append("veer_models.yaml")

    return ServiceUrls(
        model_host=model_host.rstrip("/"),
        llama=llama.rstrip("/"),
        embedding=embedding.rstrip("/"),
        traffic_oracle=traffic_oracle.rstrip("/"),
        valkey=valkey,
        source="+".join(sources) if sources else "defaults",
    )


# ---------------------------------------------------------------------------
# Adaptive parameters (fail-closed view of contract C4)
# ---------------------------------------------------------------------------


@dataclass
class AdaptiveParams:
    """Movement-scoped adaptive parameters, always protocol-floored and
    fail-closed to :data:`BASE_*` constants when the adaptive tier is absent."""

    situation_class: str = "baseline"
    advance_closure_sec: int = BASE_ADVANCE_CLOSURE_SEC
    congestion_spike_threshold: float = BASE_CONGESTION_SPIKE_THRESHOLD
    gps_poll_interval_sec: float = BASE_GPS_POLL_INTERVAL_SEC
    scoring_weights: dict[str, float] = field(default_factory=lambda: dict(BASE_SCORING_WEIGHTS))
    source: str = "baseline"
    shadow_eval: str = "unavailable"
    evidence: dict = field(default_factory=dict)

    def floored_advance_closure_sec(self, vvip_class: str) -> int:
        """advance_closure_sec after applying the protocol floor for the class."""
        floor = ADVANCE_CLOSURE_FLOOR_SEC.get(vvip_class)
        if floor is None:
            # X class: signal priority only — closure window must stay at zero.
            return 0
        return max(self.advance_closure_sec, floor)


def baseline_params() -> AdaptiveParams:
    """The fail-closed parameter set — identical to today's hardcoded constants."""
    return AdaptiveParams()
