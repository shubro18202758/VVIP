#!/usr/bin/env bash
# Run the embedding-service test suite (fakes + disposable pgvector container).
#
# The pgvector migration tests start their own throwaway docker container
# (built once from tests/Dockerfile.migtest) and are SKIPPED automatically
# when docker is unavailable — the rest of the suite runs anywhere.
set -euo pipefail

SERVICE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VEER_TEST_VENV:-/tmp/veer_embed_test_venv}"

if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q -r "$SERVICE_ROOT/tests/requirements-test.txt"

exec "$VENV/bin/python" -m pytest "$SERVICE_ROOT/tests" -q "$@"
