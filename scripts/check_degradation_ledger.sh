#!/usr/bin/env bash
# check_degradation_ledger.sh — run the degradation-ledger taxonomy tests
# (decisions_log mining: Wave-1 latch firings, tier-0 misses, tactical
# fallbacks, operator alerts, failed turns, and the honest EMPTY case)
# under the gate venv (pure python: no torch/onnxruntime).
#
# Takes NO arguments and works from any cwd: every path resolves from this
# script's own location. Runs the gate venv python at VVIP/.venvs/gate over
# the taxonomy test file with the same PYTHONPATH the verify_all.sh
# per-suite tier uses, and exits with pytest's exit code. On failure
# pytest's output (precise file:line per failing assertion) flows through
# untouched.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VVIP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
GATE_PY="$VVIP_DIR/.venvs/gate/bin/python"
SERVICE_DIR="$VVIP_DIR/services/traffic-oracle"
TEST_FILE="test_degradation_taxonomy.py"

if [ ! -x "$GATE_PY" ]; then
    echo "FAIL $SCRIPT_DIR/check_degradation_ledger.sh: gate venv python not found at $GATE_PY" >&2
    exit 1
fi
if [ ! -f "$SERVICE_DIR/tests/$TEST_FILE" ]; then
    echo "FAIL $SCRIPT_DIR/check_degradation_ledger.sh: missing $SERVICE_DIR/tests/$TEST_FILE" >&2
    exit 1
fi
if [ ! -f "$SERVICE_DIR/src/traffic_oracle/ops/degradation_taxonomy.py" ]; then
    echo "FAIL $SCRIPT_DIR/check_degradation_ledger.sh: missing $SERVICE_DIR/src/traffic_oracle/ops/degradation_taxonomy.py" >&2
    exit 1
fi

echo "== check_degradation_ledger: pytest $TEST_FILE (gate venv) =="
cd "$SERVICE_DIR/tests" || exit 1
PYTHONPATH="$SERVICE_DIR/src" "$GATE_PY" -m pytest "$TEST_FILE" -q
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo "FAIL check_degradation_ledger.sh: pytest exited $STATUS — see the failures above ($SERVICE_DIR/tests/$TEST_FILE)" >&2
else
    echo "PASS check_degradation_ledger (degradation taxonomy over decisions_log)"
fi
exit $STATUS
