#!/usr/bin/env bash
# check_routing_quantiles.sh — run the P90-robust routing objective tests
# under the gate venv (pure python: no torch/onnxruntime).
#
# Takes NO arguments and works from any cwd: every path resolves from this
# script's own location. Runs the gate venv python at VVIP/.venvs/gate over
# the quantile-objective test file with the same PYTHONPATH the
# verify_all.sh per-suite tier uses, and exits with pytest's exit code. On
# failure pytest's output (precise file:line per failing assertion) flows
# through untouched.
#
# Also asserts zero protocol-params drift (the robust-routing knob is
# registered in shared/schemas/protocol_params.json and materialized into
# BOTH services' protocol_params_gen.py; the drift guard is what keeps the
# knob auditable) by running scripts/export_protocol_params.py --check.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VVIP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
GATE_PY="$VVIP_DIR/.venvs/gate/bin/python"
SERVICE_DIR="$VVIP_DIR/services/traffic-oracle"
TEST_FILE="test_quantile_objective.py"

if [ ! -x "$GATE_PY" ]; then
    echo "FAIL $SCRIPT_DIR/check_routing_quantiles.sh: gate venv python not found at $GATE_PY" >&2
    echo "  (bootstrap it by running the pytest tier of VVIP/scripts/verify_all.sh, which creates the venv)" >&2
    exit 1
fi
if [ ! -f "$SERVICE_DIR/tests/$TEST_FILE" ]; then
    echo "FAIL $SCRIPT_DIR/check_routing_quantiles.sh: missing $SERVICE_DIR/tests/$TEST_FILE" >&2
    exit 1
fi

echo "== check_routing_quantiles: protocol-params drift check =="
if ! (cd "$VVIP_DIR" && python3 scripts/export_protocol_params.py --check); then
    echo "FAIL check_routing_quantiles.sh: protocol params drift — regenerate with 'python scripts/export_protocol_params.py' from $VVIP_DIR" >&2
    exit 1
fi

echo "== check_routing_quantiles: pytest $TEST_FILE (gate venv) =="
cd "$SERVICE_DIR/tests" || exit 1
PYTHONPATH="$SERVICE_DIR/src" "$GATE_PY" -m pytest "$TEST_FILE" -q
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo "FAIL check_routing_quantiles.sh: pytest exited $STATUS — see the failures above ($SERVICE_DIR/tests/$TEST_FILE)" >&2
else
    echo "PASS check_routing_quantiles (P90-robust routing objective + banded ETAs)"
fi
exit $STATUS
