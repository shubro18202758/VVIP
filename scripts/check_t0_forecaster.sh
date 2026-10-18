#!/usr/bin/env bash
# check_t0_forecaster.sh — two-tier check for the wired t0 foundation rung.
#
# Takes NO arguments and works from any cwd: every path resolves from this
# script's own location.
#
#   (a) PURE tier, gate venv (VVIP/.venvs/gate — python 3.12, NO
#       torch/onnxruntime): the loader/ladder/quantile-contract wiring
#       tests (test_foundation_wiring.py) plus the adapter contract tests
#       (test_foundation_adapter.py), run with the same PYTHONPATH the
#       verify_all.sh per-suite tier uses.
#
#   (b) REAL tier, wave2 venv (VVIP/.venvs/wave2 — onnxruntime 1.31.0),
#       two parts against the installed artifact at
#       VVIP/models-local/t0/t0-alpha-grouped-int8.onnx:
#         b1. tests/test_foundation_real_artifact.py — the REAL
#             _load_foundation inside FlowForecaster (loader engage,
#             warm-up degrade, contract rows through the real graph,
#             point-only cache, corrupt-artifact fail-open).
#         b2. probe_foundation_t0.py — sha256 integrity vs the shipped
#             manifest, exact contract row shapes at 200 segments x 96
#             steps, monotone p10<=p50<=p90 bands validated by the
#             consumer contract, finite outputs, bitwise determinism,
#             latency under the measured bound (5000 ms).
#
# Exit 0 ONLY when all parts pass. A missing artifact or venv fails (b)
# with a precise message — this check validates the installed wiring on
# THIS host; a fresh clone must download the artifact first (see
# models-local/t0/NOTICE) and create the wave2 venv.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VVIP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
GATE_PY="$VVIP_DIR/.venvs/gate/bin/python"
WAVE2_PY="$VVIP_DIR/.venvs/wave2/bin/python"
SERVICE_DIR="$VVIP_DIR/services/traffic-oracle"
PROBE="$SCRIPT_DIR/probe_foundation_t0.py"
ARTIFACT="$VVIP_DIR/models-local/t0/t0-alpha-grouped-int8.onnx"

fail() {
    echo "FAIL check_t0_forecaster.sh: $1" >&2
    exit 1
}

[ -x "$GATE_PY" ] || fail "gate venv python not found at $GATE_PY"
[ -x "$WAVE2_PY" ] || fail "wave2 venv python not found at $WAVE2_PY (onnxruntime tier)"
[ -f "$SERVICE_DIR/tests/test_foundation_wiring.py" ] || fail "missing $SERVICE_DIR/tests/test_foundation_wiring.py"
[ -f "$PROBE" ] || fail "missing $PROBE"
[ -f "$ARTIFACT" ] || fail "artifact not installed at $ARTIFACT (download per models-local/t0/NOTICE)"

echo "== check_t0_forecaster (a): pure wiring tests, gate venv =="
cd "$SERVICE_DIR/tests" || fail "cannot cd $SERVICE_DIR/tests"
PYTHONPATH="$SERVICE_DIR/src" "$GATE_PY" -m pytest \
    test_foundation_wiring.py test_foundation_adapter.py -q
STATUS_A=$?
if [ $STATUS_A -ne 0 ]; then
    echo "FAIL check_t0_forecaster.sh: tier (a) pytest exited $STATUS_A — see failures above" >&2
    exit $STATUS_A
fi
echo "PASS check_t0_forecaster (a): wiring + adapter contract (gate venv)"

echo "== check_t0_forecaster (b1): real loader inside FlowForecaster, wave2 venv =="
cd "$SERVICE_DIR/tests" || fail "cannot cd $SERVICE_DIR/tests"
PYTHONPATH="$SERVICE_DIR/src" "$WAVE2_PY" -m pytest \
    test_foundation_real_artifact.py -q
STATUS_B1=$?
if [ $STATUS_B1 -ne 0 ]; then
    echo "FAIL check_t0_forecaster.sh: tier (b1) pytest exited $STATUS_B1 — see failures above" >&2
    exit $STATUS_B1
fi
echo "PASS check_t0_forecaster (b1): real loader end-to-end (wave2 venv)"

echo "== check_t0_forecaster (b2): real-artifact validation, wave2 venv =="
"$WAVE2_PY" "$PROBE"
STATUS_B=$?
if [ $STATUS_B -ne 0 ]; then
    echo "FAIL check_t0_forecaster.sh: tier (b2) probe exited $STATUS_B — see failures above" >&2
    exit $STATUS_B
fi
echo "PASS check_t0_forecaster (b2): artifact integrity + contract + latency"

echo "PASS check_t0_forecaster (both tiers)"
exit 0
