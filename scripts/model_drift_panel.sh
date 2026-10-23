#!/usr/bin/env bash
# model_drift_panel.sh — model-drift panel for the four hosted model surfaces.
#
#   pplx-decider-v1-27b   :8083  POST /decide — a FIXED typed checkpoint
#                                  (frozen state doc + urgency/reroute/readiness
#                                  questions); asserts the typed answer contract,
#                                  cross-field consistency (value vs its own
#                                  distribution), SEMANTIC anchors and wall
#                                  latency within baseline bounds.
#   aditi-ps18-v7h-12b    :8084  POST /v1/chat/completions — a FIXED
#                                  JSON-requesting prompt (seed pinned; the
#                                  server is not grammar-constrained, JSON is
#                                  requested — the panel validates the PARSE,
#                                  it never trusts the request); asserts the
#                                  reply parses into the anchored JSON object
#                                  and latency within bounds.
#   pplx-embed-v2-late-0.6b
#   + bge-m3              :8085  POST /embed/documents (2 marker docs — one
#                                  call runs BOTH encoders per doc) then
#                                  POST /search; asserts embed shape and that
#                                  the search retrieves BOTH marker docs.
#   traffic-oracle
#   heuristic stack       :8091  GET /api/v1/corridor/summary — the live
#   (no trained weights)         heuristic congestion/status path; asserts the
#                                  summary schema AND that status matches the
#                                  congestion thresholds (the heuristic itself).
#
# DRIFT SURFACE — read this before touching drift_baseline.json:
#   Probe outputs are NOT byte-compared. Free generation is sampling-dependent
#   (the chat leg samples even with a pinned seed) and the oracle summarises
#   LIVE observations, so a byte-equal comparison would be dishonest. The
#   drift surface is exactly:
#     1. schema            — object/list/str/int/number structure per call
#     2. required fields   — every key the contract promises must be present
#     3. sane ranges       — probabilities in [0,1], enum values, token caps,
#                            uniqueness of doc ids, marker-ref retrieval
#     4. semantic anchors  — behaviour points that are stable on the frozen
#                            checkpoints (decider must pick ring_road at
#                            p>=0.9, urgency in [0.4,0.6], readiness in
#                            [1.0,3.0]; gemma must name the Ashram corridor;
#                            oracle status must match its own congestion
#                            thresholds; cross-field: choice value must be
#                            its distribution's argmax, noul value must equal
#                            its true-mass, score probability must equal its
#                            distribution max). Each anchor's evidence is
#                            recorded in the baseline under "justifications".
#     5. latency bounds    — per-probe wall-time ceiling:
#                            latency_ms_max = max(3x observed at baseline,
#                            latency_floor_ms). Floors are justified at
#                            ~6-7x the measured p95/cold path (NOT hundreds
#                            of multiples of warm cache hits) so the bound
#                            can actually fire: a decider at 51s, a chat
#                            generation slower than ~4.5 tok/s, or an oracle
#                            stuck for seconds all breach. Justifications are
#                            recorded per probe in the baseline.
#
# TAMPER MODEL — the baseline cannot be silently loosened:
#   In run mode the enforced shapes, anchors, consistency rules and floors
#   come from THIS SCRIPT (the code is the source of truth); the baseline
#   must match the code EXACTLY (structural equality of every shape, rule
#   list and floor, plus bound sanity: latency_ms_max == max(3x its own
#   recorded observed, floor)). Any hand edit — a loosened enum, a dropped
#   anchor, an inflated bound — makes the panel exit 2 with
#   "baseline diverges from panel code" instead of quietly weakening checks.
#   Changing the expectations therefore requires editing this script and
#   re-running --rebaseline, which records fresh observations; both edits are
#   reviewable wherever this file lives. (--rebaseline rewrites the baseline
#   from a live run and refuses to record from a red run or from fixtures.)
#
# Determinism honesty, per probe:
#   decider          — the forward pass was bit-identical across 5 live runs
#                      of this checkpoint (ring_road @ 0.9803, urgency 0.5000,
#                      readiness 1.7966, 679 tokens); anchors therefore pin
#                      bands, not bytes — a legitimate model swap that moves
#                      any band is FLAGGED FOR REVIEW, which is the panel's
#                      job, and cleared by --rebaseline.
#   gemma            — sampling-dependent (seed pinned to reduce variance;
#                      NOT a determinism guarantee); free-text "answer" is
#                      schema+bounds only, "corridor" is anchored to the one
#                      corridor the checkpoint names.
#   embeddings       — encoders are deterministic; shapes + marker retrieval
#                      only (vectors are never fetched or compared).
#   oracle_heuristic — deterministic computation over LIVE data; schema +
#                      ranges + status-vs-threshold consistency (status
#                      legitimately flips green/amber/red WITH congestion).
#
# Usage:
#   bash VVIP/scripts/model_drift_panel.sh                # live panel vs baseline
#   bash VVIP/scripts/model_drift_panel.sh --rebaseline   # re-record baseline
#                                                         # from a live run
#   bash VVIP/scripts/model_drift_panel.sh --selftest     # offline: proves the
#                                                         # FAIL paths need no
#                                                         # network (canned bad
#                                                         # responses, latency
#                                                         # breach, tampered
#                                                         # baselines)
#   --baseline PATH   # alternate baseline file
#   --fixtures  DIR   # offline: canned responses (selftest machinery)
#
# Endpoints are FIXED INTERNAL PLATFORM CONSTANTS (models-config/configs/
# veer_models.yaml `services:` + infra/compose.argus-override.yml), overridable
# via the standard env vars — MODEL_HOST_URL / LLAMA_BASE_URL /
# EMBEDDING_SERVICE_URL / TRAFFIC_ORACLE_URL, or just the ports under
# scripts/veer_env.sh's names (MODEL_HOST_PORT, LLAMA_PORT, EMBEDDING_PORT,
# ORACLE_PORT; live defaults 8083/8084/8085/8091). Outbound HTTP from this
# script is http/https only; no URL coming from external input is ever
# fetched — only these constants (same posture as smoke_models.sh).
# WRITES, disclosed: the embeddings probe upserts its 2 stable marker docs
# (drift-panel:7f3a:a/b) into the :8085 store, which is shared with the live
# stack — a private sandbox must not run the live panel against :8085 (its
# --selftest needs no network at all).

set -uo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASELINE_DEFAULT="$SCRIPT_DIR/drift_baseline.json"

MODEL_HOST_URL="${MODEL_HOST_URL:-http://127.0.0.1:${MODEL_HOST_PORT:-8083}}"
LLAMA_BASE_URL="${LLAMA_BASE_URL:-http://127.0.0.1:${LLAMA_PORT:-8084}}"
EMBEDDING_SERVICE_URL="${EMBEDDING_SERVICE_URL:-http://127.0.0.1:${EMBEDDING_PORT:-8085}}"
TRAFFIC_ORACLE_URL="${TRAFFIC_ORACLE_URL:-http://127.0.0.1:${ORACLE_PORT:-8091}}"

# scheme guard: http/https only (config constants; rejects anything else early)
url_scheme_ok() {
    case "$1" in
        http://*|https://*) return 0 ;;
        *) return 1 ;;
    esac
}
for u in "$MODEL_HOST_URL" "$LLAMA_BASE_URL" "$EMBEDDING_SERVICE_URL" "$TRAFFIC_ORACLE_URL"; do
    url_scheme_ok "$u" || { echo "FATAL: endpoint not http/https: $u" >&2; exit 2; }
done

MODE="live"
BASELINE="$BASELINE_DEFAULT"
FIXTURES=""
while [ $# -gt 0 ]; do
    case "$1" in
        --rebaseline) MODE="rebaseline" ;;
        --selftest)   MODE="selftest" ;;
        --baseline)   shift; BASELINE="${1:?--baseline needs a path}" ;;
        --fixtures)   shift; FIXTURES="${1:?--fixtures needs a directory}" ;;
        *)            { echo "FATAL: unknown argument: $1" >&2
                        echo "usage: $0 [--rebaseline|--selftest] [--baseline PATH] [--fixtures DIR]" >&2
                        exit 2; } ;;
    esac
    shift
done

exec python3 - "$MODE" "$BASELINE" "$FIXTURES" "$SCRIPT_PATH" \
    "$MODEL_HOST_URL" "$LLAMA_BASE_URL" "$EMBEDDING_SERVICE_URL" "$TRAFFIC_ORACLE_URL" <<'PYEOF'
"""Model-drift panel engine. Invoked only by model_drift_panel.sh (bash
wrapper handles arg/env/scheme concerns); everything below is the panel."""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

(
    MODE,
    BASELINE_PATH,
    FIXTURES_DIR,
    SCRIPT_PATH,
    DECIDER_URL,
    LLAMA_URL,
    EMBED_URL,
    ORACLE_URL,
) = sys.argv[1:9]

# Latency floors (ms). Justified per probe — see FLOOR_JUSTIFICATION — at
# ~6-7x the measured p95/cold path so max(3x observed, floor) can actually
# fire on a realistic regression (the reviewer-standard: a bound that only a
# near-hang can breach is dead weight).
LATENCY_FLOOR_MS = {
    "decider": 15_000,
    "gemma": 45_000,
    "embeddings": 10_000,
    "oracle_heuristic": 5_000,
}
FLOOR_JUSTIFICATION = {
    "decider": (
        "5 live runs of the frozen checkpoint: warm 1.28-1.34s, first call 2.08s, "
        "service self-reports p95 decide 2.08s (/decider/health). Floor 15s ~ 7x p95: "
        "a 40x warm regression (51s) breaches; normal GPU contention does not."
    ),
    "gemma": (
        "cold (uncached prompt) observed 7.35s = 115 prompt tokens @ ~110 tok/s + "
        "23 generated tokens @ ~23 tok/s; warm prompt-cached 1.18-1.23s. Floor 45s "
        "~ 6x cold: sustained generation slower than ~4.4 tok/s breaches."
    ),
    "embeddings": (
        "first observation 1.53s (2 docs through BOTH encoders + upsert), warm "
        "0.16-0.19s. Floor 10s ~ 6.5x the first observation: an encoder path or "
        "store write taking seconds breaches."
    ),
    "oracle_heuristic": (
        "cached summary 2.4ms, uncached SQL aggregate over the 10-min window 13.3ms "
        "(measured after the 60s valkey TTL expired). Floor 5s exists to catch "
        "connection/pool failure modes (restart loops, unreachable postgis) which "
        "take seconds — not query slowdown, which is already sub-second."
    ),
}

ANCHOR_JUSTIFICATION = {
    "decider": (
        "The frozen checkpoint's forward pass returned bit-identical answers across "
        "5 live runs: reroute=ring_road p=0.9803, urgency=0.5000, readiness=1.7966, "
        "state_tokens=679. Anchors pin bands around those (value==ring_road, "
        "probability>=0.9, urgency in [0.4,0.6], readiness in [1.0,3.0]) so a model "
        "swap that moves any of them is flagged for review, not byte-locked."
    ),
    "gemma": (
        "3/3 live runs of the constrained checkpoint named the corridor to avoid as "
        "'Ashram Road' (the only corridor the user turn names). Anchor: corridor "
        "contains 'ashram' (case-insensitive) — tolerant to formatting, fatal to "
        "semantically gutted replies. The free-text 'answer' stays bounds-only."
    ),
    "embeddings": (
        "Both marker docs (ref_id drift-panel:7f3a:a/b) were upserted immediately "
        "before the search in 3/3 live runs and both were retrieved in the top hits. "
        "Anchor: search must return at least one hit per marker ref_id."
    ),
    "oracle_heuristic": (
        "Corridor status is a pure threshold function of avg_congestion_idx "
        "(traffic-oracle __main__.py: red if >0.7, amber if >0.4, else green). "
        "Anchor: status must equal classify(avg_congestion_idx) — the heuristic "
        "must not disagree with its own inputs, whatever the live values are."
    ),
}

NUM01 = {"type": "number", "min": 0.0, "max": 1.0}


def _answer_shape(value_spec, dist_spec, probability_spec=None):
    return {
        "type": "object",
        "keys": {
            "value": value_spec,
            "probability": probability_spec if probability_spec is not None else NUM01,
            "distribution": dist_spec,
        },
        "optional_keys": {"confidence": NUM01},
    }


# ── fixed probe inputs (the frozen checkpoints) ─────────────────────────────
DECIDER_BODY = {
    "request_id": "drift-panel-checkpoint-1",
    "state": (
        "DRIFT CHECKPOINT 2026-10: VVIP Z+ convoy staged at Ahmedabad origin. "
        "Three corridors under watch: SG Highway (moderate flow, 42 km/h average), "
        "Ashram Road (heavy congestion near Nehru Bridge, 14 km/h), Ring Road "
        "(light flow, 55 km/h). Movement window 19:45-20:30 IST. Two signal "
        "failures reported on Ashram Road in the last 20 minutes; weather clear; "
        "event-emulator feed nominal. Advance closure 150 seconds per Blue Book."
    ),
    "questions": {
        "urgency": {
            "type": "noul",
            "instructions": "Based on this corridor state, must the departure window be locked now rather than delayed?",
        },
        "reroute": {
            "type": "choice",
            "instructions": "Which corridor should the route use?",
            "options": {
                "sg_highway": "SG Highway, moderate flow",
                "ashram_road": "Ashram Road, heavy congestion",
                "ring_road": "Ring Road, light flow",
            },
        },
        "readiness": {
            "type": "score",
            "instructions": "Rate readiness to lock the departure window",
            "scale": ["not ready", "barely ready", "partially ready", "mostly ready", "fully ready"],
        },
    },
}

GEMMA_BODY = {
    "model": "aditi-ps18-v7h-12b",
    "max_tokens": 200,
    "temperature": 0.2,
    "seed": 20261017,  # pinned: reduces sampling variance; NOT a determinism guarantee
    "messages": [
        {
            "role": "system",
            "content": (
                'You are a deterministic checkpoint responder for the VVIP drift '
                'panel. Reply with ONE JSON object and nothing else, exactly this '
                'shape: {"answer": string, "corridor": string, "confidence": '
                "number between 0 and 1}. No markdown, no code fence, no extra keys."
            ),
        },
        {
            "role": "user",
            "content": (
                "Checkpoint: congestion on Ashram Road is heavy and rising. Reply "
                "with the JSON object where answer is the immediate tactical action "
                "for a waiting convoy, corridor is the corridor to avoid, confidence "
                "is your confidence."
            ),
        },
    ],
}

MARKER_PREFIX = "drift-panel:7f3a:"
EMBED_BODY = {
    "documents": [
        {
            "body": "Drift panel checkpoint doc A: corridor DRIFT-MARKER-7F3A held green for 12 minutes; convoy maintained 46 km/h.",
            "title": "drift-checkpoint-a",
            "ref_id": "drift-panel:7f3a:a",
        },
        {
            "body": "Drift panel checkpoint doc B: corridor DRIFT-MARKER-7F3A congestion cleared after signal reset; recovery 180 seconds.",
            "title": "drift-checkpoint-b",
            "ref_id": "drift-panel:7f3a:b",
        },
    ],
    "kind": "pattern",
}
SEARCH_BODY = {
    "query": "DRIFT-MARKER-7F3A corridor congestion recovery",
    "kinds": ["pattern"],
    "top_k": 10,
}

# ── fixed expectations (source of truth; recorded verbatim into the baseline
#    and re-verified against it on every run — see TAMPER MODEL in the header)
DECIDER_SHAPE = {
    "type": "object",
    "keys": {
        "answers": {
            "type": "object",
            "keys": {
                "urgency": _answer_shape(
                    # anchor: 5/5 live runs at exactly 0.5000
                    {"type": "number", "min": 0.4, "max": 0.6},
                    {"type": "object", "keys": {"false": NUM01, "true": NUM01}},
                ),
                "reroute": _answer_shape(
                    # anchor: 5/5 live runs chose ring_road
                    {"type": "str", "in": ["ring_road"]},
                    {
                        "type": "object",
                        "keys": {
                            "sg_highway": NUM01,
                            "ashram_road": NUM01,
                            "ring_road": NUM01,
                        },
                    },
                    # anchor override: chosen-option mass was 0.9803 in 5/5 runs
                    probability_spec={"type": "number", "min": 0.9, "max": 1.0},
                ),
                "readiness": _answer_shape(
                    # anchor: 5/5 live runs at 1.7966
                    {"type": "number", "min": 1.0, "max": 3.0},
                    {
                        "type": "object",
                        "keys": {"0": NUM01, "1": NUM01, "2": NUM01, "3": NUM01, "4": NUM01},
                    },
                ),
            },
        },
        "state_tokens": {"type": "int", "min": 1, "max": 8192},
        "latency_ms": {"type": "number", "min": 0.0, "max": 120000.0},
    },
    "optional_keys": {"request_id": {"type": "str", "in": ["drift-panel-checkpoint-1"]}},
}

GEMMA_SHAPE = {
    "type": "object",
    "keys": {
        "choices": {
            "type": "list",
            "min_len": 1,
            "items": {
                "type": "object",
                "keys": {
                    "message": {
                        "type": "object",
                        "keys": {"content": {"type": "str", "min_len": 10}},
                    }
                },
            },
        },
        "usage": {
            "type": "object",
            "keys": {"completion_tokens": {"type": "int", "min": 1, "max": 1000}},
        },
    },
}
GEMMA_CONTENT_SHAPE = {  # the JSON the reply must parse into
    "type": "object",
    "keys": {
        "answer": {"type": "str", "min_len": 4, "max_len": 300},
        # anchor: 3/3 live runs named the Ashram corridor (the one to avoid)
        "corridor": {"type": "str", "min_len": 3, "max_len": 120, "contains": "ashram"},
        "confidence": NUM01,
    },
}

EMBED_SHAPE = {
    "type": "object",
    "keys": {
        "embedded": {"type": "int", "min": 2, "max": 2},  # exactly the 2 sent
        "doc_ids": {"type": "list", "min_len": 2, "items": {"type": "nonempty"}, "unique": True},
    },
}
SEARCH_SHAPE = {
    "type": "object",
    "keys": {
        "results": {
            "type": "list",
            "min_len": 1,
            "items": {
                "type": "object",
                "keys": {
                    "doc_id": {"type": "nonempty"},
                    "kind": {"type": "str"},
                    "score": {"type": "number"},
                },
                "optional_keys": {
                    "ref_id": {"type": "str"},
                    "title": {"type": "str"},
                    "snippet": {"type": "str"},
                },
            },
            # anchor: BOTH just-upserted marker docs must be retrieved
            "any": [
                {"type": "object", "keys": {"ref_id": {"type": "str", "prefix": f"{MARKER_PREFIX}a"}}},
                {"type": "object", "keys": {"ref_id": {"type": "str", "prefix": f"{MARKER_PREFIX}b"}}},
            ],
        }
    },
}

ORACLE_SHAPE = {
    "type": "object",
    "keys": {
        "total_segments": {"type": "int", "min": 0, "max": 100000},
        "avg_speed_kmh": {"type": "number", "min": 0.0, "max": 200.0},
        "avg_congestion_idx": NUM01,
        "congested_segments": {"type": "int", "min": 0, "max": 100000},
        "critical_segments": {"type": "int", "min": 0, "max": 100000},
        "status": {"type": "str", "in": ["green", "amber", "red"]},
    },
}


# ── cross-field consistency rules (names are recorded in the baseline and
#    re-verified against this registry on every run) ──────────────────────────
def _c_noul_value_equals_true_mass(body):
    try:
        a = body["answers"]["urgency"]
        true_mass = a["distribution"]["true"]
        if abs(float(a["value"]) - true_mass) > 1e-6 or abs(float(a["probability"]) - true_mass) > 1e-6:
            return (f"urgency: value={a['value']!r} probability={a['probability']!r} "
                    f"disagree with distribution.true={true_mass!r}")
    except (KeyError, TypeError, ValueError) as exc:
        return f"urgency consistency could not evaluate: {exc!r}"
    return None


def _c_choice_value_is_distribution_argmax(body):
    try:
        a = body["answers"]["reroute"]
        dist = a["distribution"]
        top = max(float(v) for v in dist.values())
        winners = {k for k, v in dist.items() if abs(float(v) - top) <= 1e-9}
        if a["value"] not in winners:
            return (f"reroute: value {a['value']!r} is not its distribution's argmax "
                    f"(top mass on {sorted(winners)} @ {top:.4f})")
    except (KeyError, TypeError, ValueError) as exc:
        return f"reroute consistency could not evaluate: {exc!r}"
    return None


def _c_score_probability_is_distribution_max(body):
    try:
        a = body["answers"]["readiness"]
        top = max(float(v) for v in a["distribution"].values())
        if abs(float(a["probability"]) - top) > 1e-6:
            return (f"readiness: probability={a['probability']!r} != distribution max {top!r}")
    except (KeyError, TypeError, ValueError) as exc:
        return f"readiness consistency could not evaluate: {exc!r}"
    return None


def _c_status_matches_congestion_thresholds(body):
    try:
        cong = float(body["avg_congestion_idx"])
        expected = "red" if cong > 0.7 else ("amber" if cong > 0.4 else "green")
        if body["status"] != expected:
            return (f"status {body['status']!r} contradicts avg_congestion_idx "
                    f"{cong:.3f} (thresholds: red>0.7, amber>0.4, else green → {expected!r})")
    except (KeyError, TypeError, ValueError) as exc:
        return f"status consistency could not evaluate: {exc!r}"
    return None


CONSISTENCY_RULES = {
    ("decider", "decide"): [
        ("noul_value_equals_true_mass", _c_noul_value_equals_true_mass),
        ("choice_value_is_distribution_argmax", _c_choice_value_is_distribution_argmax),
        ("score_probability_is_distribution_max", _c_score_probability_is_distribution_max),
    ],
    ("gemma", "chat"): [],
    ("embeddings", "embed"): [],
    ("embeddings", "search"): [],
    ("oracle_heuristic", "summary"): [
        ("status_matches_congestion_thresholds", _c_status_matches_congestion_thresholds),
    ],
}

PROBE_DEFS = [
    {
        "name": "decider",
        "surface": "pplx-decider-v1-27b /decide (:8083)",
        "calls": [
            {
                "name": "decide",
                "method": "POST",
                "url": f"{DECIDER_URL}/decide",
                "body": DECIDER_BODY,
                "shape": DECIDER_SHAPE,
            }
        ],
    },
    {
        "name": "gemma",
        "surface": "aditi-ps18-v7h-12b chat JSON (:8084)",
        "calls": [
            {
                "name": "chat",
                "method": "POST",
                "url": f"{LLAMA_URL}/v1/chat/completions",
                "body": GEMMA_BODY,
                "shape": GEMMA_SHAPE,
                "content_json": GEMMA_CONTENT_SHAPE,
            }
        ],
    },
    {
        "name": "embeddings",
        "surface": "pplx-embed-v2-late-0.6b + bge-m3 (:8085)",
        "calls": [
            {
                "name": "embed",
                "method": "POST",
                "url": f"{EMBED_URL}/embed/documents",
                "body": EMBED_BODY,
                "shape": EMBED_SHAPE,
            },
            {
                "name": "search",
                "method": "POST",
                "url": f"{EMBED_URL}/search",
                "body": SEARCH_BODY,
                "shape": SEARCH_SHAPE,
            },
        ],
    },
    {
        "name": "oracle_heuristic",
        "surface": "traffic-oracle heuristic summary (:8091)",
        "calls": [
            {
                "name": "summary",
                "method": "GET",
                "url": f"{ORACLE_URL}/api/v1/corridor/summary",
                "body": None,
                "shape": ORACLE_SHAPE,
            }
        ],
    },
]


# ── declarative shape validator ─────────────────────────────────────────────
def validate(spec, val, path):
    """Return a list of human-readable drift errors (empty == conforms)."""
    errs = []
    t = spec.get("type", "any")

    if t == "any":
        return errs
    if t == "nonempty":
        if val is None or val == "" or val == [] or val == {}:
            errs.append(f"{path}: expected a non-empty value, got {val!r}")
        return errs
    if t == "bool":
        if not isinstance(val, bool):
            errs.append(f"{path}: expected bool, got {type(val).__name__} {val!r}")
        return errs
    if t == "int":
        if isinstance(val, bool) or not isinstance(val, int):
            errs.append(f"{path}: expected int, got {type(val).__name__} {val!r}")
            return errs
        for bound, violated_if, label in (("min", lambda a, b: a < b, ">="), ("max", lambda a, b: a > b, "<=")):
            if bound in spec and violated_if(val, spec[bound]):
                errs.append(f"{path}: int {val} out of range (expected {label} {spec[bound]})")
        return errs
    if t == "number":
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            errs.append(f"{path}: expected number, got {type(val).__name__} {val!r}")
            return errs
        for bound, violated_if, label in (("min", lambda a, b: a < b, ">="), ("max", lambda a, b: a > b, "<=")):
            if bound in spec and violated_if(val, spec[bound]):
                errs.append(f"{path}: number {val} out of range (expected {label} {spec[bound]})")
        return errs
    if t == "str":
        if not isinstance(val, str):
            errs.append(f"{path}: expected str, got {type(val).__name__} {val!r}")
            return errs
        if "min_len" in spec and len(val) < spec["min_len"]:
            errs.append(f"{path}: str len {len(val)} < min_len {spec['min_len']} ({val!r})")
        if "max_len" in spec and len(val) > spec["max_len"]:
            errs.append(f"{path}: str len {len(val)} > max_len {spec['max_len']}")
        if "prefix" in spec and not val.startswith(spec["prefix"]):
            errs.append(f"{path}: str {val!r} lacks prefix {spec['prefix']!r}")
        if "contains" in spec and spec["contains"].lower() not in val.lower():
            errs.append(f"{path}: str {val!r} does not contain {spec['contains']!r} (case-insensitive)")
        if "in" in spec and val not in spec["in"]:
            errs.append(f"{path}: str {val!r} not in {spec['in']}")
        return errs
    if t == "object":
        if not isinstance(val, dict):
            errs.append(f"{path}: expected object, got {type(val).__name__} {val!r}")
            return errs
        if "min_fields" in spec and len(val) < spec["min_fields"]:
            errs.append(f"{path}: object has {len(val)} fields, needs >= {spec['min_fields']}")
        for key, sub in spec.get("keys", {}).items():
            if key not in val:
                errs.append(f"{path}: missing required key {key!r}")
            else:
                errs.extend(validate(sub, val[key], f"{path}.{key}"))
        for key, sub in spec.get("optional_keys", {}).items():
            if key in val and val[key] is not None:
                errs.extend(validate(sub, val[key], f"{path}.{key}"))
        if "values" in spec:
            for key, item in val.items():
                errs.extend(validate(spec["values"], item, f"{path}.{key}"))
        return errs
    if t == "list":
        if not isinstance(val, list):
            errs.append(f"{path}: expected list, got {type(val).__name__} {val!r}")
            return errs
        if "min_len" in spec and len(val) < spec["min_len"]:
            errs.append(f"{path}: list len {len(val)} < min_len {spec['min_len']}")
        if spec.get("unique"):
            try:
                if len(set(val)) != len(val):
                    errs.append(f"{path}: list contains duplicate items (expected unique)")
            except TypeError:
                errs.append(f"{path}: list items not hashable; cannot verify uniqueness")
        for i, item in enumerate(val):
            if "items" in spec:
                errs.extend(validate(spec["items"], item, f"{path}[{i}]"))
        # "any": a single shape (≥1 item must match) or a LIST of shapes
        # (each shape must be matched by ≥1 item — used for marker retrieval)
        any_specs = spec.get("any")
        if any_specs is not None:
            for j, sub in enumerate(any_specs if isinstance(any_specs, list) else [any_specs]):
                if not any(not validate(sub, item, f"{path}[any{j}]") for item in val):
                    errs.append(f"{path}: no list item matched required 'any' shape #{j}: "
                                f"{json.dumps(sub, sort_keys=True)}")
        return errs
    errs.append(f"{path}: unknown spec type {t!r}")
    return errs


def extract_json_object(content):
    """Parse a model reply that should be ONE JSON object (fence-tolerant)."""
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.S)
    match = re.search(r"\{.*\}", fenced, flags=re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


# ── fetch layers ────────────────────────────────────────────────────────────
def _scheme_ok(url):
    return url.startswith("http://") or url.startswith("https://")


def http_call(call, timeout_ms):
    """One real HTTP call; returns {status, latency_ms, body, error}."""
    if not _scheme_ok(call["url"]):
        return {"status": 0, "latency_ms": 0.0, "body": None,
                "error": f"endpoint not http/https: {call['url']}"}
    data = json.dumps(call["body"]).encode() if call["body"] is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(call["url"], data=data, method=call["method"], headers=headers)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_ms / 1000.0 + 10.0) as resp:
            raw = resp.read()
            status = resp.status
        latency_ms = (time.perf_counter() - t0) * 1000.0
    except urllib.error.HTTPError as exc:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return {"status": exc.code, "latency_ms": latency_ms, "body": None,
                "error": f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:  # connection refused, timeout, DNS …
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return {"status": 0, "latency_ms": latency_ms, "body": None,
                "error": f"{type(exc).__name__}: {exc}"}
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"status": status, "latency_ms": latency_ms, "body": None,
                "error": f"non-JSON body ({len(raw)} bytes)"}
    return {"status": status, "latency_ms": latency_ms, "body": body, "error": None}


def fixture_call(fixture, call):
    """Offline: replay a canned response for this call."""
    calls = fixture.get("calls", {})
    if call["name"] not in calls:
        return {"status": 0, "latency_ms": 0.0, "body": None,
                "error": f"fixture missing call {call['name']!r}"}
    canned = calls[call["name"]]
    return {
        "status": canned.get("status", 200),
        "latency_ms": float(canned.get("latency_ms", 0.0)),
        "body": canned.get("body"),
        "error": canned.get("error"),
    }


# ── probe execution ─────────────────────────────────────────────────────────
def run_probe(pdef, expectations, fetch):
    """fetch(call) -> call result; returns one panel row."""
    row = {
        "name": pdef["name"],
        "surface": expectations.get("surface", pdef["surface"]),
        "status": [],
        "latency_ms": 0.0,
        "bound_ms": float(expectations["latency_ms_max"]),
        "errors": [],
        "detail": "",
        "bodies": {},
    }
    for call in pdef["calls"]:
        result = fetch(call)
        row["latency_ms"] += result["latency_ms"]
        if result["status"]:
            row["status"].append(str(result["status"]))
        exp = expectations["calls"].get(call["name"])
        if exp is None:
            row["errors"].append(f"{call['name']}: baseline has no expectations for this call (stale baseline?)")
            continue
        if result.get("error"):
            row["errors"].append(f"{call['name']}: {result['error']}")
            continue
        if result["status"] != 200:
            row["errors"].append(f"{call['name']}: HTTP {result['status']}")
            continue
        body = result["body"]
        row["bodies"][call["name"]] = body
        row["errors"].extend(validate(exp["shape"], body, call["name"]))
        if exp.get("content_json") is not None:
            try:
                content = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                content = None
            parsed = extract_json_object(content) if isinstance(content, str) else None
            if parsed is None:
                row["errors"].append(f"{call['name']}: content is not a JSON object: {content!r}")
            else:
                row["errors"].extend(validate(exp["content_json"], parsed, f"{call['name']}.content(json)"))
                row["bodies"][f"{call['name']}:content_json"] = parsed
        # cross-field consistency: the rule NAMES come from the baseline, the
        # implementations from this script's registry (both verified equal in
        # verify_baseline_matches_code before any probe runs)
        for rule_name in exp.get("consistency", []):
            rule = dict(CONSISTENCY_RULES.get((pdef["name"], call["name"]), [])).get(rule_name)
            if rule is None:
                row["errors"].append(f"{call['name']}: baseline references unknown consistency rule {rule_name!r}")
                continue
            if isinstance(body, dict):
                problem = rule(body)
                if problem:
                    row["errors"].append(f"{call['name']}: consistency[{rule_name}]: {problem}")
    if row["latency_ms"] > row["bound_ms"]:
        row["errors"].append(
            f"latency {row['latency_ms']:.0f}ms exceeds bound {row['bound_ms']:.0f}ms"
        )
    row["detail"] = _detail(pdef["name"], row["bodies"])
    return row


def _detail(name, bodies):
    """One observed-values line per probe (informational, never asserted)."""
    try:
        if name == "decider":
            d = bodies["decide"]
            a = d["answers"]
            return (f"reroute={a['reroute']['value']} (p={a['reroute']['probability']:.3f}) "
                    f"urgency={a['urgency']['value']:.3f} readiness={a['readiness']['value']:.2f} "
                    f"tokens={d['state_tokens']}")
        if name == "gemma":
            j = bodies["chat:content_json"]
            u = bodies["chat"]["usage"]
            return (f"answer={j['answer']!r} corridor={j['corridor']!r} "
                    f"confidence={j['confidence']} completion_tokens={u['completion_tokens']}")
        if name == "embeddings":
            n = bodies["embed"]["embedded"]
            hits = bodies["search"]["results"]
            marker = sum(1 for h in hits if str(h.get("ref_id", "")).startswith(MARKER_PREFIX))
            top = hits[0]["score"] if hits else float("nan")
            return f"embedded={n} hits={len(hits)} marker_hits={marker} top_score={top:.2f}"
        if name == "oracle_heuristic":
            s = bodies["summary"]
            return (f"status={s['status']} segments={s['total_segments']} "
                    f"congestion={s['avg_congestion_idx']:.3f} speed={s['avg_speed_kmh']:.1f}km/h")
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return ""


# ── baseline: load, structure validation, tamper check ──────────────────────
def fatal(msg, code=2):
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(code)


def _norm(x):
    return json.loads(json.dumps(x, sort_keys=True))


def expectations_from_code(pdef):
    calls = {}
    for call in pdef["calls"]:
        calls[call["name"]] = {
            "shape": call["shape"],
            "consistency": [name for name, _ in CONSISTENCY_RULES.get((pdef["name"], call["name"]), [])],
        }
        if call.get("content_json") is not None:
            calls[call["name"]]["content_json"] = call["content_json"]
    return {"surface": pdef["surface"], "calls": calls}


def verify_baseline_matches_code(baseline):
    """Fail closed (exit 2) unless the baseline is EXACTLY what this script's
    expectations + the recorded observations imply. A hand-loosened shape, a
    dropped anchor/consistency rule, a changed floor or an inflated/deflated
    latency bound all diverge here."""
    problems = []
    probes = baseline.get("probes")
    if not isinstance(probes, dict) or not probes:
        fatal("baseline is malformed: no probes object (rerun --rebaseline)")
    for pdef in PROBE_DEFS:
        where = f"probes.{pdef['name']}"
        b = probes.get(pdef["name"])
        if not isinstance(b, dict):
            fatal(f"baseline is stale: {where} missing (rerun --rebaseline)")
        code_exp = expectations_from_code(pdef)["calls"]
        b_calls = b.get("calls")
        if not isinstance(b_calls, dict):
            fatal(f"baseline is malformed: {where}.calls must be an object")
        for call_name, code_call in code_exp.items():
            cwhere = f"{where}.calls.{call_name}"
            b_call = b_calls.get(call_name)
            if not isinstance(b_call, dict):
                fatal(f"baseline is malformed: {cwhere} missing")
            for field in ("shape", "content_json"):
                if field in code_call or field in b_call:
                    if _norm(code_call.get(field)) != _norm(b_call.get(field)):
                        problems.append(f"{cwhere}.{field} diverges from panel code")
            if _norm(code_call.get("consistency", [])) != _norm(b_call.get("consistency", [])):
                problems.append(f"{cwhere}.consistency diverges from panel code")
        extra = set(b_calls) - set(code_exp)
        if extra:
            problems.append(f"{where}.calls has unknown call(s) {sorted(extra)}")
        floor = b.get("latency_floor_ms")
        if floor != LATENCY_FLOOR_MS[pdef["name"]]:
            problems.append(f"{where}.latency_floor_ms is {floor!r}, panel code says "
                            f"{LATENCY_FLOOR_MS[pdef['name']]!r}")
        bound = b.get("latency_ms_max")
        observed = b.get("observed_ms_at_baseline")
        if not isinstance(bound, (int, float)) or not isinstance(observed, (int, float)):
            fatal(f"baseline is malformed: {where} needs numeric latency_ms_max and "
                  f"observed_ms_at_baseline (got {bound!r}, {observed!r})")
        recomputed = round(max(3.0 * float(observed), float(LATENCY_FLOOR_MS[pdef["name"]])), 1)
        if abs(float(bound) - recomputed) > 0.2:
            problems.append(f"{where}.latency_ms_max={bound} is inconsistent with its own "
                            f"recorded observed={observed} and floor={floor} "
                            f"(max(3x observed, floor) = {recomputed}) — tampered?")
    if problems:
        for p in problems:
            print(f"baseline diverges from panel code: {p}", file=sys.stderr)
        fatal(f"{BASELINE_PATH} does not match this script's expectations — the "
              f"baseline is the drift surface and must be (re)recorded with "
              f"--rebaseline, never hand-edited")


def load_baseline(path):
    try:
        with open(path, encoding="utf-8") as fh:
            baseline = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        fatal(f"cannot read baseline {path}: {exc} (record one with --rebaseline)")
    if not isinstance(baseline, dict):
        fatal(f"baseline {path} is not a JSON object")
    return baseline


# ── panel driver ────────────────────────────────────────────────────────────
def fmt_seconds(ms):
    return f"{ms / 1000.0:.2f}s"


def print_panel(rows, baseline_meta):
    print("─" * 78)
    print(f"MODEL DRIFT PANEL — baseline: {baseline_meta.get('path', '?')} "
          f"(recorded {baseline_meta.get('recorded_at', '?')})")
    print("─" * 78)
    print(f"{'probe':<17}{'surface':<40}{'latency':>9}{'budget':>9}  verdict")
    for row in rows:
        verdict = "OK" if not row["errors"] else "DRIFT"
        status = ",".join(row["status"]) or "-"
        print(f"{row['name']:<17}{row['surface'][:39]:<40}"
              f"{fmt_seconds(row['latency_ms']):>9}{fmt_seconds(row['bound_ms']):>9}  {verdict}"
              f"  [http {status}]")
        if row["detail"]:
            print(f"{'':<17}· {row['detail']}")
    print("─" * 78)


def finish(rows):
    failed = [r for r in rows if r["errors"]]
    if failed:
        for row in failed:
            print(f"FAIL {row['name']} ({row['surface']}):", file=sys.stderr)
            for err in row["errors"]:
                print(f"  - {err}", file=sys.stderr)
        print(f"DRIFT PANEL FAILED: {len(failed)}/{len(rows)} probe(s) drifted "
              f"against baseline", file=sys.stderr)
        return 1
    print(f"DRIFT OK: {len(rows)}/{len(rows)} probes green against baseline")
    return 0


def make_fetch(timeout_for_probe):
    if FIXTURES_DIR:
        cache = {}

        def load_fixture(pdef):
            if pdef["name"] not in cache:
                path = os.path.join(FIXTURES_DIR, f"{pdef['name']}.json")
                try:
                    with open(path, encoding="utf-8") as fh:
                        cache[pdef["name"]] = json.load(fh)
                except (OSError, json.JSONDecodeError) as exc:
                    fatal(f"fixture unreadable: {path}: {exc}")
            return cache[pdef["name"]]

        def fetch_for(pdef, call):
            return fixture_call(load_fixture(pdef), call)

        return fetch_for

    def fetch_live(pdef, call):
        return http_call(call, timeout_for_probe(pdef["name"]))

    return fetch_live


def run_panel_fetch(baseline_probes, fetch_for):
    rows = []
    for pdef in PROBE_DEFS:
        exp = baseline_probes.get(pdef["name"])
        if exp is None:
            fatal(f"baseline is stale: no expectations for probe {pdef['name']!r} "
                  f"(rerun with --rebaseline)")
        rows.append(run_probe(pdef, exp, lambda call, _p=pdef: fetch_for(_p, call)))
    return rows


# ── modes ───────────────────────────────────────────────────────────────────
def mode_rebaseline():
    if FIXTURES_DIR:
        fatal("--rebaseline refuses to run from fixtures (baseline must record a LIVE run)")
    floor_fetch = make_fetch(lambda name: LATENCY_FLOOR_MS[name])
    rows = run_panel_fetch(
        {p["name"]: {**expectations_from_code(p), "latency_ms_max": LATENCY_FLOOR_MS[p["name"]]}
         for p in PROBE_DEFS},
        floor_fetch,
    )
    print_panel(rows, {"path": BASELINE_PATH + " (recording…)", "recorded_at": "now"})
    if finish(rows) != 0:
        print("REBASELINE FAILED — baseline NOT rewritten", file=sys.stderr)
        return 1
    probes = {}
    for pdef, row in zip(PROBE_DEFS, rows):
        observed = row["latency_ms"]
        bound = max(3.0 * observed, float(LATENCY_FLOOR_MS[pdef["name"]]))
        probes[pdef["name"]] = {
            "surface": pdef["surface"],
            "latency_ms_max": round(bound, 1),
            "observed_ms_at_baseline": round(observed, 1),
            "latency_floor_ms": LATENCY_FLOOR_MS[pdef["name"]],
            "justifications": {
                "latency_floor": FLOOR_JUSTIFICATION[pdef["name"]],
                "semantic_anchors": ANCHOR_JUSTIFICATION[pdef["name"]],
            },
            "calls": expectations_from_code(pdef)["calls"],
        }
    baseline = {
        "version": 2,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recorded_by": "VVIP/scripts/model_drift_panel.sh --rebaseline",
        "policy": {
            "note": (
                "Probe outputs are NOT byte-compared (generation is sampling-"
                "dependent where seeds are unavailable). The drift surface is "
                "schema, required fields, sane numeric ranges, semantic anchors, "
                "cross-field consistency rules and latency bounds. The baseline "
                "must match the panel code exactly — hand edits exit 2 with "
                "'baseline diverges from panel code'; re-record with --rebaseline."
            ),
            "latency_rule": "latency_ms_max = max(3 x observed_ms_at_baseline, latency_floor_ms); "
                            "floors justified per probe under justifications.latency_floor",
        },
        "probes": probes,
    }
    with open(BASELINE_PATH, "w", encoding="utf-8") as fh:
        json.dump(baseline, fh, indent=2)
        fh.write("\n")
    print(f"baseline recorded: {BASELINE_PATH}")
    for name, probe in probes.items():
        print(f"  {name:<17} bound {probe['latency_ms_max']/1000.0:>7.1f}s "
              f"(observed {probe['observed_ms_at_baseline']/1000.0:.2f}s, "
              f"floor {probe['latency_floor_ms']/1000.0:.0f}s)")
    return 0


def mode_live():
    baseline = load_baseline(BASELINE_PATH)
    verify_baseline_matches_code(baseline)
    bound_for = lambda name: float(baseline["probes"][name]["latency_ms_max"])
    rows = run_panel_fetch(
        baseline["probes"],
        make_fetch(bound_for),
    )
    print_panel(rows, {"path": BASELINE_PATH, "recorded_at": baseline.get("recorded_at", "?")})
    return finish(rows)


# ── selftest: offline proof of the FAIL paths ────────────────────────────────
# Canned GOOD responses were captured verbatim from the live hosts on
# 2026-10-17 (search hits trimmed to 3; both marker docs kept intact).
GOOD_DECIDER = {
    "answers": {
        "urgency": {"value": 0.5, "probability": 0.5, "confidence": None,
                    "distribution": {"false": 0.5, "true": 0.5}},
        "reroute": {"value": "ring_road", "probability": 0.980331985155854,
                    "confidence": 0.9704979777337808,
                    "distribution": {"sg_highway": 0.016626653812827637,
                                     "ashram_road": 0.0030413610313183804,
                                     "ring_road": 0.980331985155854}},
        "readiness": {"value": 1.7966323479759123, "probability": 0.329666646791001,
                      "confidence": 0.24616696372255908,
                      "distribution": {"0": 0.1980405464622419, "1": 0.1579025548540246,
                                       "2": 0.329666646791001, "3": 0.2781645080310442,
                                       "4": 0.03622574386168825}},
    },
    "state_tokens": 679,
    "latency_ms": 2075.57,
    "request_id": "drift-panel-checkpoint-1",
}
GOOD_GEMMA = {
    "choices": [{"finish_reason": "stop", "index": 0,
                 "message": {"role": "assistant",
                             "content": "{\"answer\":\"divert to alternative route\",\"corridor\":\"Ashram Road\",\"confidence\":0.9}"}}],
    "usage": {"completion_tokens": 23, "prompt_tokens": 115, "total_tokens": 138},
}
GOOD_EMBED = {"embedded": 2,
              "doc_ids": ["870be880-f714-4522-a273-ef203b729036",
                          "6186c237-b125-47b1-91de-e698d05a7fd0"]}
GOOD_SEARCH = {
    "query": "DRIFT-MARKER-7F3A corridor congestion recovery",
    "query_token_count": 14,
    "recalled": 6,
    "latency_ms": 104.2,
    "results": [
        {"doc_id": "6186c237-b125-47b1-91de-e698d05a7fd0", "kind": "pattern",
         "ref_id": "drift-panel:7f3a:b", "title": "drift-checkpoint-b",
         "snippet": "Drift panel checkpoint doc B: corridor DRIFT-MARKER-7F3A congestion cleared after signal reset; recovery 180 seconds.",
         "score": 11.789310455322266, "maxsim_score": 11.789310455322266,
         "dense_score": 0.85348206761, "strategy": "maxsim", "token_count": 28,
         "occurred_at": None, "metadata": {}},
        {"doc_id": "870be880-f714-4522-a273-ef203b729036", "kind": "pattern",
         "ref_id": "drift-panel:7f3a:a", "title": "drift-checkpoint-a",
         "snippet": "Drift panel checkpoint doc A: corridor DRIFT-MARKER-7F3A held green for 12 minutes; convoy maintained 46 km/h.",
         "score": 11.605193138122559, "maxsim_score": 11.605193138122559,
         "dense_score": 0.810418963432312, "strategy": "maxsim", "token_count": 31,
         "occurred_at": None, "metadata": {}},
        {"doc_id": "2ed32db2-a67d-455b-9bb1-7199b58b189c", "kind": "pattern",
         "ref_id": "smoke:2", "title": "congestion-clear",
         "snippet": "Congestion spike cleared on corridor segment 1004; recovery 240s.",
         "score": 11.303143501281738, "maxsim_score": 11.303143501281738,
         "dense_score": 0.8136574625968933, "strategy": "maxsim", "token_count": 19,
         "occurred_at": None, "metadata": {}},
    ],
}
GOOD_ORACLE = {"total_segments": 0, "avg_speed_kmh": 0.0, "avg_congestion_idx": 0.0,
               "congested_segments": 0, "critical_segments": 0, "status": "green"}

GOOD_FIXTURES = {
    "decider": {"calls": {"decide": {"status": 200, "latency_ms": 2083.2, "body": GOOD_DECIDER}}},
    "gemma": {"calls": {"chat": {"status": 200, "latency_ms": 7350.0, "body": GOOD_GEMMA}}},
    "embeddings": {"calls": {"embed": {"status": 200, "latency_ms": 1528.4, "body": GOOD_EMBED},
                             "search": {"status": 200, "latency_ms": 104.2, "body": GOOD_SEARCH}}},
    "oracle_heuristic": {"calls": {"summary": {"status": 200, "latency_ms": 3.5, "body": GOOD_ORACLE}}},
}

# Canned BAD responses — each violates the contract in a way drift must catch.
BAD_FIXTURES = {
    "decider": {
        "calls": {"decide": {"status": 200, "latency_ms": 2083.2, "body": {
            "answers": {
                "urgency": {"value": 1.7, "probability": 1.7,  # probability out of [0,1]
                            "confidence": None, "distribution": {"false": -0.7, "true": 1.7}},
                "reroute": {"value": "teleport",  # not one of the offered options
                            "probability": 0.5,
                            "distribution": {"sg_highway": 0.5, "ashram_road": 0.3, "ring_road": 0.2}},
                "readiness": {"value": 1.8, "probability": 0.33,
                              "distribution": {"0": 0.2, "1": 0.16, "2": 0.33, "3": 0.28, "4": 0.04}},
            },
            "state_tokens": 679, "latency_ms": 2075.57,
        }}},
    },
    "gemma": {
        "calls": {"chat": {"status": 200, "latency_ms": 7350.0, "body": {
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                  # prose, not the contracted JSON object
                                                  "content": "Sorry, I would rather chat about corridors informally today!"}}],
            "usage": {"completion_tokens": 14, "prompt_tokens": 115},
        }}},
    },
    "gemma_semantic": {
        "calls": {"chat": {"status": 200, "latency_ms": 7350.0, "body": {
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                  # well-formed JSON, semantically gutted:
                                                  # names the wrong corridor with zero confidence
                                                  "content": "{\"answer\":\"eeee\",\"corridor\":\"zzz\",\"confidence\":0.0}"}}],
            "usage": {"completion_tokens": 14, "prompt_tokens": 115},
        }}},
    },
    "embeddings": {
        "calls": {"embed": {"status": 200, "latency_ms": 1528.4, "body": {
            "embedded": 1,  # one of the two docs silently dropped
            "doc_ids": ["870be880-f714-4522-a273-ef203b729036"],
        }},
            "search": {"status": 200, "latency_ms": 104.2, "body": {
                # hits exist, but the just-upserted marker docs are missing
                "query": "DRIFT-MARKER-7F3A corridor congestion recovery",
                "results": [GOOD_SEARCH["results"][2]],
            }}},
    },
    "oracle_heuristic": {
        "calls": {"summary": {"status": 200, "latency_ms": 3.5, "body": {
            "total_segments": 0, "avg_speed_kmh": 0.0,
            "avg_congestion_idx": 3.2,  # congestion index outside [0,1]
            "congested_segments": 0, "critical_segments": 0,
            "status": "purple",  # not a corridor status the heuristic can emit
        }}},
    },
    "oracle_consistency": {
        "calls": {"summary": {"status": 200, "latency_ms": 3.5, "body": {
            # schema-valid but the heuristic contradicts its own input:
            # congestion 0.85 must classify red, not green
            "total_segments": 12, "avg_speed_kmh": 31.0, "avg_congestion_idx": 0.85,
            "congested_segments": 7, "critical_segments": 2, "status": "green",
        }}},
    },
}


def write_fixture_dir(root, fixtures):
    path = tempfile.mkdtemp(prefix="drift-selftest-", dir=root)
    for name, fixture in fixtures.items():
        with open(os.path.join(path, f"{name}.json"), "w", encoding="utf-8") as fh:
            json.dump(fixture, fh)
    return path


def run_script(args):
    proc = subprocess.run(
        ["bash", SCRIPT_PATH, *args],
        capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, proc.stdout + proc.stderr


def mode_selftest():
    if FIXTURES_DIR:
        fatal("--selftest does not take --fixtures (it builds its own)")
    baseline = load_baseline(BASELINE_PATH)
    scenarios = []

    def check(name, args, expect_zero, must_contain=()):
        code, out = run_script(args)
        ok = (code == 0) if expect_zero else (code != 0)
        why = f"exit={code}"
        missing = [needle for needle in must_contain if needle not in out]
        ok = ok and not missing
        if missing:
            why += f"; expected " + ("0" if expect_zero else "non-zero") + \
                   f"; output missing {missing!r}"
        scenarios.append((name, ok, why))

    with tempfile.TemporaryDirectory(prefix="drift-panel-selftest.") as root:
        good_dir = write_fixture_dir(root, GOOD_FIXTURES)

        # 1. PASS path: canned-good responses vs the shipped baseline → exit 0
        check("good fixtures vs shipped baseline → exit 0",
              ["--fixtures", good_dir, "--baseline", BASELINE_PATH],
              expect_zero=True, must_contain=["DRIFT OK: 4/4 probes green"])

        # 2. FAIL path per probe: one canned bad response → exit non-zero,
        #    FAIL line names the drifted probe (no network involved).
        for probe in ("decider", "gemma", "embeddings", "oracle_heuristic"):
            bad = {k: (json.loads(json.dumps(v)) if k != probe else BAD_FIXTURES[probe])
                   for k, v in GOOD_FIXTURES.items()}
            bad_dir = write_fixture_dir(root, bad)
            check(f"canned bad {probe} response → exit non-zero",
                  ["--fixtures", bad_dir, "--baseline", BASELINE_PATH],
                  expect_zero=False, must_contain=[f"FAIL {probe} "])

        # 3. Semantic-anchor FAIL: well-formed JSON that names the WRONG
        #    corridor with zero confidence must NOT pass.
        gutted = json.loads(json.dumps(GOOD_FIXTURES))
        gutted["gemma"] = BAD_FIXTURES["gemma_semantic"]
        gutted_dir = write_fixture_dir(root, gutted)
        check("semantically gutted gemma JSON (wrong corridor) → exit non-zero",
              ["--fixtures", gutted_dir, "--baseline", BASELINE_PATH],
              expect_zero=False,
              must_contain=["FAIL gemma ", "does not contain 'ashram'"])

        # 4. Consistency FAIL: schema-valid oracle summary whose status
        #    contradicts its own congestion index.
        inconsistent = json.loads(json.dumps(GOOD_FIXTURES))
        inconsistent["oracle_heuristic"] = BAD_FIXTURES["oracle_consistency"]
        incons_dir = write_fixture_dir(root, inconsistent)
        check("oracle status contradicts congestion thresholds → exit non-zero",
              ["--fixtures", incons_dir, "--baseline", BASELINE_PATH],
              expect_zero=False,
              must_contain=["FAIL oracle_heuristic ", "consistency[status_matches_congestion_thresholds]"])

        # 5. Latency-bound FAIL (the bound must be able to fire): good
        #    responses, but the decider fixture reports 90s against the
        #    baseline's 15s budget — shipped baseline untouched.
        slow = json.loads(json.dumps(GOOD_FIXTURES))
        slow["decider"]["calls"]["decide"]["latency_ms"] = 90_000.0
        slow_dir = write_fixture_dir(root, slow)
        check("decider at 90s vs 15s budget → exit non-zero",
              ["--fixtures", slow_dir, "--baseline", BASELINE_PATH],
              expect_zero=False, must_contain=["exceeds bound 15000ms"])

        # 6. Tamper FAIL (loosened shape): hand-widening the decider enum and
        #    dropping the gemma corridor anchor must fail CLOSED, not pass.
        loosened = json.loads(json.dumps(baseline))
        loosened["probes"]["decider"]["calls"]["decide"]["shape"]["keys"]["answers"]["keys"][
            "reroute"]["keys"]["value"]["in"] = ["sg_highway", "ashram_road", "ring_road"]
        loosened["probes"]["gemma"]["calls"]["chat"]["content_json"]["keys"][
            "corridor"].pop("contains")
        loosened_path = os.path.join(root, "loosened_baseline.json")
        with open(loosened_path, "w", encoding="utf-8") as fh:
            json.dump(loosened, fh)
        check("hand-loosened baseline (widened enum, dropped anchor) → exit non-zero",
              ["--fixtures", good_dir, "--baseline", loosened_path],
              expect_zero=False, must_contain=["baseline diverges from panel code"])

        # 7. Tamper FAIL (inflated/deflated bound): clamping latency_ms_max
        #    to 1ms contradicts the file's own recorded observed/floor.
        tight = json.loads(json.dumps(baseline))
        tight["probes"]["decider"]["latency_ms_max"] = 1
        tight_path = os.path.join(root, "tight_baseline.json")
        with open(tight_path, "w", encoding="utf-8") as fh:
            json.dump(tight, fh)
        check("doctored baseline (1ms bound) + good fixtures → exit non-zero",
              ["--fixtures", good_dir, "--baseline", tight_path],
              expect_zero=False, must_contain=["baseline diverges from panel code", "latency_ms_max"])

    print("─" * 78)
    print("DRIFT PANEL SELFTEST (offline — no network)")
    print("─" * 78)
    failures = 0
    for name, ok, why in scenarios:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}  [{why}]")
        failures += 0 if ok else 1
    print("─" * 78)
    if failures:
        print(f"SELFTEST FAILED: {failures}/{len(scenarios)} scenario(s) misbehaved", file=sys.stderr)
        return 1
    print(f"SELFTEST OK: all {len(scenarios)} scenarios behaved as designed "
          "(FAIL paths proven offline)")
    return 0


def main():
    if MODE == "rebaseline":
        return mode_rebaseline()
    if MODE == "selftest":
        return mode_selftest()
    return mode_live()


sys.exit(main())
PYEOF
