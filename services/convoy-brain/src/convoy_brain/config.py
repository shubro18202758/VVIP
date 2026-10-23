"""Convoy Brain configuration — two-tier hierarchy endpoints and constants.

Tier layout (see VVIP architecture):
    T5 strategic — pplx-decider-v1-27b served by model-host  (default :8083)
    T4 tactical  — aditi-ps18-v7h Gemma 4 12B on llama-server (default :8084)
    embeddings   — pplx-embed-v2-late-0.6b service            (default :8085)

Service URLs are resolved in this order (contract C0):
    1. Environment variables MODEL_HOST_URL / LLAMA_BASE_URL / EMBEDDING_SERVICE_URL
    2. The ``services:`` block of veer_models.yaml — VEER_MODELS_CONFIG when
       set, else VVIP/models-config/configs/veer_models.yaml (owned by the
       models stream; read-only for us). The retired-tree VVIP/models
       symlink is no longer read.
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

# VVIP root discovery: <VVIP>/services/convoy-brain/src/convoy_brain/config.py
# (the same layout in a checkout, a worktree and the container's /repo/VVIP).
_PACKAGE_DIR = Path(__file__).resolve().parent
_VVIP_ROOT = _PACKAGE_DIR.parents[3]  # convoy_brain -> src -> convoy-brain -> services -> VVIP
MODELS_CONFIG_PATH = _VVIP_ROOT / "models-config" / "configs" / "veer_models.yaml"


def models_config_path(env: dict[str, str] | None = None) -> Path:
    """veer_models.yaml location: VEER_MODELS_CONFIG, else models-config/."""
    env = env if env is not None else dict(os.environ)
    override = (env.get("VEER_MODELS_CONFIG") or "").strip()
    return Path(override) if override else MODELS_CONFIG_PATH


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
LLAMA_CALL_TIMEOUT_SEC = 30.0     # per tactical-call HTTP wall floor (stretched to
                                  # cover a larger budget's decode time, see below)
TACTICAL_SELECT_MAX_TOKENS = 48   # tool-selection reply (one JSON name)
TACTICAL_ARGS_MAX_TOKENS = 192    # argument drafting (schema-constrained; worst ≈ 10 s)
TACTICAL_FINAL_MAX_TOKENS = 512   # final structured answer: the schema caps
                                  # 'reasoning' at 1200 chars, and a cut reply's
                                  # complete reasoning string is rescued; 384
                                  # still cut a 1200-char reasoning mid-string
                                  # live (1 in 5 repeats), so the cap is 512

# Structured-agent budgets (agents/base.structured_agent_call). The live
# route plan hit the old flat 512-token cap on BOTH the primary and the
# identical repair call (~2 × 23 s, then the heuristic floor), and a budget
# smaller than the reply the schema allows only moves the waste: a 500-token
# route narration and a 384-token diversion reply were still cut on the Z+
# plan and paid a second Gemma call (86-92 s plans). So every plan agent's
# schema is BOUNDED (maxLength / maxItems / additionalProperties false, no
# echoed segment lists or scores), the prompt asks for compact single-line
# JSON, and the FIRST call's budget is the schema's own bounded maximum
# (agents/base.agent_budget): max_tokens only caps generation, so a short
# reply costs nothing extra and a long one is never cut. A cut (whitespace
# the grammar still allows) is retried ONCE as compact JSON under a larger
# cap — never the identical call.
AGENT_DEFAULT_MAX_TOKENS = 512    # an unbounded schema (no derivable maximum)
AGENT_RETRY_MAX_TOKENS = 1024     # ceiling of the one compact retry after a cut
AGENT_MAX_TOKENS_CEILING = 1024   # no first-call budget above this (a schema whose
                                  # bounded maximum exceeds it is too loose — tested)
#: Non-digit characters per token of the agents' JSON replies on Gemma's
#: tokenizer (every DIGIT is its own token — a full-precision float is 18).
#: MEASURED 2026-10-23 on six live Z+ plan replies (llama-server's own
#: completion_tokens): 3.00-4.44 non-digit chars/token; the floor of that
#: range keeps the estimate at or above every recorded reply (tested).
AGENT_CHARS_PER_TOKEN = 3.0
AGENT_BUDGET_MARGIN = 1.1         # tokenizer variance + the whitespace the grammar allows
#: A repair round is not started with less plan budget left than this
#: (the repair would be cut mid-call and only add latency).
AGENT_REPAIR_MIN_SEC = 12.0

# Per-call HTTP wall (llm/llama_bridge): never shorter than the call's own
# budget takes to decode. The flat 30 s wall killed a 704-token compact
# diversion reply at 30.0 s (decoding at 24 tok/s) — the budget allowed the
# reply, the wall did not. The wall is max(LLAMA_CALL_TIMEOUT_SEC,
# prefill allowance + max_tokens / decode floor); the plan deadline and the
# chat turn deadline still bound every call from outside.
LLAMA_PREFILL_ALLOWANCE_SEC = 5.0  # measured prefill 0.4-3.9 s (2-3.5k-token prompts)
LLAMA_DECODE_FLOOR_TOK_PER_SEC = 16.0  # measured 21.5-24.3 tok/s with 2 slots busy

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


def decision_memory_record_enabled(env: dict[str, str] | None = None) -> bool:
    """VVIP_MEMORY_RECORD: the decision-memory feeder's write side (default ON).

    "0" / "false" / "no" / "off" turns the feeder off: checkpoints still
    SEARCH the corpus (novelty latch, tier-0 gate), but nothing is upserted
    into traffic.memory_docs. Sandbox and test brains run with it off — the
    embedding service writes to whatever database it serves, and a sandbox
    brain must never add rows to the live corpus.
    """
    env = env if env is not None else dict(os.environ)
    return env.get("VVIP_MEMORY_RECORD", "").strip().lower() not in ("0", "false", "no", "off")

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
# Triggers whose stored routine answer is ALSO filed for the movement's own
# decision docs: served when the best memory is ANY decision doc this
# movement's checkpoints of the trigger recorded (its ref_id names movement
# and trigger), for the identical question set. A pre_movement question set
# names the plan's candidates, and the tactical tier trims the tail
# alternates in some plans and not in others (live, 2026-10-24: 3 / 5 / 3
# candidates for three identical bodies), so a set recurs non-consecutively.
# Its first answer was stored only under the best doc seen BEFORE the
# feeder upserted its own doc, which no repeat matches again, so a set
# needed three occurrences before a hit and alternating sets never hit.
# The escort triggers ask fixed question sets and keep their filing.
TIER0_OWN_DOC_TRIGGERS = frozenset({"pre_movement"})


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

# -- Live escort (R9 adaptive loop; contracts C-B / C-J) -----------------------
# Ahmedabad operating area (min_lon, min_lat, max_lon, max_lat): a convoy fix
# outside it is refused (422) by POST /movements/{id}/position and ignored on
# the WebSocket position.update path.
CITY_BBOX = (72.40, 22.90, 72.80, 23.20)

ESCORT_GPS_STALE_SEC = 15.0          # a fix older than this reads as "GPS lost"
ESCORT_GPS_TIMEOUT_SEC = 300.0       # waiting_for_gps longer than this FAILS the
                                     # escort — a missing fix never completes it
ESCORT_INCIDENT_COOLDOWN_SEC = 60.0  # one incident checkpoint per congestion
                                     # crossing; a re-crossing inside the window
                                     # is deferred, not dropped: still congested
                                     # when it ends, it is the next incident
ESCORT_CONGESTION_HYSTERESIS = 0.05  # a crossing clears only below threshold-0.05
ESCORT_CLEAR_CONFIRM_SEC = 10.0      # ... read on every reporting tick for this
                                     # long (one low sample is not a clear; the
                                     # synthetic feed refreshes a segment ~4 s)
ESCORT_LOOKAHEAD_M = 3000.0          # route segments starting this far ahead are watched
ESCORT_ARRIVAL_RADIUS_M = 40.0       # remaining route distance that counts as arrival
ESCORT_OFF_ROUTE_M = 150.0           # cross-track distance that raises an off-route alert
ESCORT_HOLD_MAX_SEC = 180.0          # a hold lifts itself after this; one whose
                                     # segments are still congested is re-decided
ESCORT_REROUTE_MAX_DETOUR = 2.0      # a re-route longer than this multiple of the
                                     # distance left goes to an officer, not the loop
ESCORT_MAX_ITERATIONS = 14400        # safety valve (~4 h at the 1 s baseline tick)
ESCORT_SIM_SPEED_KMH = 80.0          # emulated cruise speed (each segment's limit caps
                                     # it): a protected convoy drives at the limit, so
                                     # the emulated run matches the plan's solver ETA
                                     # (live: 45 km/h made 'planned 10 min' read 15 min)
ESCORT_SIM_TICK_SEC = 1.0            # emulated GPS publish interval
#: Expected speed (km/h) on a segment with no speed limit on record, for
#: the escort's remaining-route ETA (the planner's fallback corridor speed).
ESCORT_ETA_FALLBACK_KMH = 40.0
ESCORT_EVENT_CHECK_SEC = 15.0        # how often the loop re-reads its corridor event
                                     # (an officer may cancel / complete it on /ops)
ESCORT_APPROVAL_TIMEOUT_SEC = 90.0   # an escort proposal (route switch, Plan B, hold,
                                     # closures) no officer answered in this long
                                     # EXPIRES: the convoy holds the current approved plan

# POST /api/v1/adaptive/health wall bound (brain -> oracle degradation ladder).
ADAPTIVE_HEALTH_TIMEOUT_SEC = 2.5


def _env_flag(env: dict[str, str] | None, name: str) -> bool:
    env = env if env is not None else dict(os.environ)
    return env.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_float(
    env: dict[str, str] | None, name: str, default: float, lo: float, hi: float,
) -> float:
    env = env if env is not None else dict(os.environ)
    try:
        value = float(env.get(name, "") or default)
    except ValueError:
        return default
    return min(max(value, lo), hi)


def escort_sim_gps_enabled(env: dict[str, str] | None = None) -> bool:
    """VVIP_ESCORT_SIM_GPS: walk the primary route with an EMULATED GPS
    producer while no real fix arrives (default off — demo stacks opt in)."""
    return _env_flag(env, "VVIP_ESCORT_SIM_GPS")


def officer_required(env: dict[str, str] | None = None) -> bool:
    """VVIP_REQUIRE_OFFICER: escort start/stop/clear need a named officer."""
    return _env_flag(env, "VVIP_REQUIRE_OFFICER")


def escort_sim_speed_kmh(env: dict[str, str] | None = None) -> float:
    """Emulated convoy cruise speed (VVIP_ESCORT_SIM_SPEED_KMH, 5..120)."""
    return _env_float(env, "VVIP_ESCORT_SIM_SPEED_KMH", ESCORT_SIM_SPEED_KMH, 5.0, 120.0)


def escort_gps_timeout_sec(env: dict[str, str] | None = None) -> float:
    """How long an escort waits for a fix (VVIP_ESCORT_GPS_TIMEOUT_SEC, 10..3600)."""
    return _env_float(env, "VVIP_ESCORT_GPS_TIMEOUT_SEC", ESCORT_GPS_TIMEOUT_SEC, 10.0, 3600.0)


def escort_approval_timeout_sec(env: dict[str, str] | None = None) -> float:
    """How long an escort proposal waits for a named officer before it
    expires and the convoy holds its current approved plan
    (VVIP_ESCORT_APPROVAL_TIMEOUT_SEC, 15..600; default 90)."""
    return _env_float(
        env, "VVIP_ESCORT_APPROVAL_TIMEOUT_SEC", ESCORT_APPROVAL_TIMEOUT_SEC, 15.0, 600.0,
    )


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
    config_path: Path | None = None,
) -> ServiceUrls:
    """Resolve service URLs: env > veer_models.yaml ``services:`` > defaults."""
    env = env if env is not None else dict(os.environ)
    yaml_services = _read_models_config(
        config_path if config_path is not None else models_config_path(env)
    )

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
