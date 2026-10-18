#!/usr/bin/env bash
# check_tier0_gate.sh — run the FULL convoy-brain suite (decider tier-0
# routine-checkpoint cache gate included) under the gate venv.
#
# Takes NO arguments and works from any cwd: every path resolves from this
# script's own location. Runs the gate venv python at VVIP/.venvs/gate over
# the whole tests/ directory — the tier-0 gate is validated at the suite
# scale (test_tier0_gate.py plus every Wave-1 latch/cadence regression that
# pins today-behavior around it), never by a single file — and exits with
# pytest's exit code. On failure pytest's output (precise file:line per
# failing assertion) flows through untouched.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VVIP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
GATE_PY="$VVIP_DIR/.venvs/gate/bin/python"
SERVICE_DIR="$VVIP_DIR/services/convoy-brain"

if [ ! -x "$GATE_PY" ]; then
    echo "FAIL $SCRIPT_DIR/check_tier0_gate.sh: gate venv python not found at $GATE_PY" >&2
    exit 1
fi
if [ ! -d "$SERVICE_DIR/tests" ]; then
    echo "FAIL $SCRIPT_DIR/check_tier0_gate.sh: missing $SERVICE_DIR/tests" >&2
    exit 1
fi

echo "== check_tier0_gate: pytest tests/ (full convoy-brain suite, gate venv) =="
cd "$SERVICE_DIR" || exit 1
"$GATE_PY" -m pytest tests/ -q
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo "FAIL check_tier0_gate.sh: pytest exited $STATUS — see the failures above ($SERVICE_DIR/tests)" >&2
else
    echo "PASS check_tier0_gate (decider tier-0 gate + full convoy-brain suite)"
fi
exit $STATUS
