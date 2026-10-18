#!/usr/bin/env bash
# verify_all.sh — the VVIP end-to-end acceptance gate.
#
# Bootstraps what it needs (frontend node deps + esbuild, a pure-python gate
# venv), then runs IN ORDER, failing fast on the first failure:
#
#   TIER frontend-bundle   esbuild bundle of the headless e2e entry
#   TIER frontend-e2e      headless Chromium e2e (VVIP/frontend/tests/e2e/)
#   TIER gods-eye-check    svelte-check on the city-canvas stream's files
#                          (services/command-deck; pre-existing diagnostics in
#                          other streams' files are reported, not gated here)
#   TIER gods-eye-canvas   build the SvelteKit deck + headless SwiftShader
#                          render smoke (tests/gods-eye-smoke.mjs — canvas
#                          non-blank, keyless, proxy; LIVE oracle+emulator
#                          required, like the models tier)
#   TIER pytest-<service>  every pure-python pytest suite under VVIP/
#                          (suites needing torch/onnx/GPU stacks are SKIPped
#                          with an explicit reason, never silently)
#   TIER submission-consistency
#                          node scripts/submission_consistency_check.mjs and
#                          its --selftest, from the VVIP dir — the submission
#                          docs vs live tree/artifacts oracle
#   TIER wave1-hatches     scripts/check_forecaster_hatch.sh +
#                          scripts/check_brain_latch.sh (gate venv; run after
#                          the pytest tier so the venv is bootstrapped)
#   TIER wave2-forecaster  scripts/check_t0_forecaster.sh from the VVIP dir —
#                          t0 foundation wiring + adapter contract (gate
#                          venv), then the real-artifact tests and probe
#                          (wave2 venv, VVIP/models-local/t0)
#   TIER wave2-routing     scripts/check_routing_quantiles.sh from the VVIP
#                          dir — P90-robust routing objective (gate venv) +
#                          protocol-params drift guard
#   TIER wave2-tier0       scripts/check_tier0_gate.sh from the VVIP dir —
#                          the FULL convoy-brain suite incl. the decider
#                          tier-0 routine-checkpoint cache gate (gate venv)
#   TIER models            VVIP/scripts/smoke_models.sh (live model stack)
#   TIER model-drift       VVIP/scripts/model_drift_panel.sh --selftest,
#                          then the live panel (live model stack, same env
#                          as the models tier)
#
# On the first failure the script prints "TIER <name> FAILED: <reason>" and
# exits nonzero. Success prints one PASS line per tier.
#
# Usage: bash VVIP/scripts/verify_all.sh
# Env:   CHROME_PATH (optional — defaults resolve on this box; the script
#        falls back to the newest puppeteer-cached Chrome when the e2e's
#        built-in default is absent).

set -uo pipefail

VVIP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND="$VVIP_DIR/frontend"
GATE_VENV="$VVIP_DIR/.venvs/gate"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
log()  { printf '%s\n' "$*"; }
pass() { printf '%s[PASS]%s %s\n' "$GREEN" "$RESET" "$*"; }

fail_tier() { # fail_tier <tier> <detail>
    printf '%s%sTIER %s FAILED:%s %s\n' "$RED" "$BOLD" "$1" "$RESET" "$2" >&2
    exit 1
}

need() { command -v "$1" >/dev/null 2>&1 || fail_tier bootstrap "required tool '$1' not found in PATH"; }

# ────────────────────────────────────────────────────────────────────────────
# TIER 1 — frontend esbuild bundle
# ────────────────────────────────────────────────────────────────────────────
tier_frontend_bundle() {
    need node; need npx
    log "== TIER frontend-bundle: esbuild bundle for the headless e2e =="
    (
        cd "$FRONTEND" || exit 1
        # Bootstrap node deps when absent (vite itself is a devDependency).
        if [ ! -x node_modules/.bin/vite ]; then
            log "bootstrapping frontend node_modules (npm ci)…"
            npm ci --no-audit --no-fund >/dev/null 2>&1 || exit 1
        fi
        # Vite 8 bundles through rolldown, so esbuild is NOT in the tree —
        # install it on demand exactly as the e2e header documents.
        if [ ! -x node_modules/.bin/esbuild ]; then
            log "bootstrapping esbuild (npm i --no-save esbuild)…"
            npm i --no-save --no-audit --no-fund esbuild >/dev/null 2>&1 || exit 1
        fi
        mkdir -p tests/e2e/build
        ./node_modules/.bin/esbuild tests/e2e/entry.jsx --bundle --format=esm \
            --loader:.jsx=jsx --outfile=tests/e2e/build/test-bundle.js \
            --log-level=warning
    ) || fail_tier frontend-bundle "esbuild bundle of tests/e2e/entry.jsx failed"
    [ -s "$FRONTEND/tests/e2e/build/test-bundle.js" ] \
        || fail_tier frontend-bundle "bundle output missing/empty"
    pass "frontend-bundle ($(wc -c < "$FRONTEND/tests/e2e/build/test-bundle.js") bytes)"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 2 — headless frontend e2e
# ────────────────────────────────────────────────────────────────────────────
tier_frontend_e2e() {
    log "== TIER frontend-e2e: headless Chromium journey test =="

    # The e2e script's built-in CHROME_PATH default points at the puppeteer
    # cache path used in development; keep it when it exists, otherwise fall
    # back to the newest cached Chrome before letting the script fail.
    if [ -z "${CHROME_PATH:-}" ]; then
        DEFAULT_CHROME="$HOME/.cache/puppeteer/chrome/linux_arm-153.0.8010.36/chrome-linux-arm64/chrome"
        if [ ! -x "$DEFAULT_CHROME" ]; then
            FOUND_CHROME="$(find "$HOME/.cache/puppeteer/chrome" -name chrome -type f -perm -u+x 2>/dev/null | sort | tail -1)"
            [ -n "$FOUND_CHROME" ] || fail_tier frontend-e2e \
                "no Chrome binary found (set CHROME_PATH); looked in ~/.cache/puppeteer"
            export CHROME_PATH="$FOUND_CHROME"
            log "CHROME_PATH defaulted to $CHROME_PATH"
        fi
    fi

    (
        cd "$FRONTEND" || exit 1
        [ -d node_modules/puppeteer-core ] || {
            npm i --no-save --no-audit --no-fund puppeteer-core >/dev/null 2>&1 || exit 1
        }
        node tests/e2e/citizen-green-corridor.e2e.mjs
    ) || fail_tier frontend-e2e "node tests/e2e/citizen-green-corridor.e2e.mjs failed"
    pass "frontend-e2e"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 2b — city-canvas stream: svelte-check on the stream's own files
# ────────────────────────────────────────────────────────────────────────────
DECK="$VVIP_DIR/services/command-deck"
# The paths this stream owns/edited; diagnostics anywhere else pre-date it.
GODS_EYE_OWNED_RE='src/lib/gods-eye/|src/lib/ops/|src/lib/i18n/|src/lib/primitives/|src/lib/design/|src/lib/shell/|src/lib/citizen/|src/lib/comms/|src/routes/\(deck\)/|src/routes/\(public\)/|src/hooks.server.ts|src/routes/\+layout.svelte'

tier_gods_eye_check() {
    need node; need npx
    log "== TIER gods-eye-check: svelte-check on the city-canvas files =="
    (
        cd "$DECK" || exit 1
        [ -x node_modules/.bin/svelte-check ] || {
            log "bootstrapping command-deck node_modules (npm install --no-package-lock)…"
            npm install --no-package-lock --no-audit --no-fund >/dev/null 2>&1 || exit 1
        }
        ./node_modules/.bin/svelte-kit sync >/dev/null 2>&1 || true
        OUT="$(./node_modules/.bin/svelte-check --tsconfig ./tsconfig.json 2>&1)"
        # Whole-project count (informational): pre-existing diagnostics in
        # other streams' files are reported but do not fail THIS tier.
        printf '%s\n' "$OUT" | grep -E '^/' | grep -Ev "$GODS_EYE_OWNED_RE" | cut -d: -f1 | sort -u | \
            sed 's/^/     pre-existing (not this tier): /' || true
        OWNED="$(printf '%s\n' "$OUT" | grep -E '^/' | grep -E "$GODS_EYE_OWNED_RE" || true)"
        if [ -n "$OWNED" ]; then
            printf '%s\n' "$OWNED" | head -20 >&2
            exit 1
        fi
    ) || fail_tier gods-eye-check "svelte-check reported diagnostics in the city-canvas stream's files (see above)"
    pass "gods-eye-check (stream files clean)"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 2c — city-canvas stream: build + headless SwiftShader render smoke
# ────────────────────────────────────────────────────────────────────────────
tier_gods_eye_canvas() {
    need node; need npx
    log "== TIER gods-eye-canvas: deck build + SwiftShader canvas smoke =="
    (
        cd "$DECK" || exit 1
        # 5199 is this tier's own smoke origin, and `vite build` below
        # rewrites the very build/ dir any server on that port serves from.
        # A stale deck leaked there (e.g. a showcase spawn whose cleanup
        # missed the real pid) would be reused by the smoke and die
        # mid-rebuild — page never ready, origin refusing connections.
        # Free the port first so the smoke spawns its own fresh origin.
        fuser -k 5199/tcp >/dev/null 2>&1 || true
        sleep 1
        [ -x node_modules/.bin/vite ] || {
            npm install --no-package-lock --no-audit --no-fund >/dev/null 2>&1 || exit 1
        }
        # Vendor the prebuilt Cesium bundle exactly like the Dockerfile does.
        if [ ! -f static/cesium/Cesium.js ]; then
            [ -d node_modules/cesium/Build/Cesium ] || {
                npm install --no-package-lock --no-audit --no-fund >/dev/null 2>&1 || exit 1
            }
            mkdir -p static && cp -r node_modules/cesium/Build/Cesium static/cesium || exit 1
        fi
        NODE_OPTIONS=--max-old-space-size=1024 ./node_modules/.bin/vite build >/dev/null 2>&1 || exit 1
        [ -f build/index.js ] || exit 1
        node tests/gods-eye-smoke.mjs
    ) || fail_tier gods-eye-canvas "canvas build or headless render smoke failed (run node tests/gods-eye-smoke.mjs in services/command-deck for detail)"
    pass "gods-eye-canvas (non-blank under SwiftShader, keyless, proxy ok)"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 3 — every pure-python pytest suite under VVIP/
# ────────────────────────────────────────────────────────────────────────────
# Bash array so extras brackets reach pip verbatim (no word-split/glob).
GATE_DEPS=(pytest pytest-asyncio httpx fastapi uvicorn pydantic structlog
prometheus-client valkey numpy polars pyarrow scikit-learn asyncpg
geopandas shapely osmnx pandas click ortools psutil
nats-py 'psycopg[binary,pool]' pydantic-settings)

# Heavy/GPU modules whose absence marks a suite as NOT pure-python.
NOT_PURE_MODULES="torch|onnx|onnxruntime|gymnasium|sentence_transformers|transformers|accelerate"

tier_python_bootstrip() {
    need python3
    log "== TIER pytest: bootstrapping gate venv at $GATE_VENV =="
    if [ ! -x "$GATE_VENV/bin/python" ]; then
        python3 -m venv "$GATE_VENV" || fail_tier pytest-bootstrap "python3 -m venv failed"
    fi
    "$GATE_VENV/bin/pip" install -q --disable-pip-version-check "${GATE_DEPS[@]}" \
        || fail_tier pytest-bootstrap "pip install of gate deps failed"
}

tier_pytest_suites() {
    tier_python_bootstrip

    # One line per suite dir (dir<TAB>srcpath).
    mapfile -t SUITES < <(
        find "$VVIP_DIR" -name 'test_*.py' -not -path '*/node_modules/*' \
             -not -path '*/.venvs/*' -not -path '*/.venv/*' -not -path '*/build/*' \
             -printf '%h\n' | sort -u
    )
    [ "${#SUITES[@]}" -gt 0 ] || fail_tier pytest "no pytest suites discovered under VVIP/"

    for suite in "${SUITES[@]}"; do
        name="${suite#"$VVIP_DIR"/}"
        log "== TIER pytest-$name =="

        PYTHONPATH_ARG=""
        if [ -d "$suite/../src" ]; then
            PYTHONPATH_ARG="$(cd "$suite/../src" && pwd)"
        fi

        set +e
        if [ -n "$PYTHONPATH_ARG" ]; then
            OUT="$(cd "$suite" && PYTHONPATH="$PYTHONPATH_ARG" \
                "$GATE_VENV/bin/python" -m pytest . -q 2>&1)"
        else
            OUT="$(cd "$suite" && "$GATE_VENV/bin/python" -m pytest . -q 2>&1)"
        fi
        STATUS=$?
        set -e 2>/dev/null || true

        if [ $STATUS -eq 0 ]; then
            pass "pytest-$name ($(printf '%s\n' "$OUT" | tail -1))"
            continue
        fi

        # Suite needs a GPU/ML stack -> explicit skip (not a gate failure).
        if printf '%s\n' "$OUT" | grep -Eq "ModuleNotFoundError: No module named '($NOT_PURE_MODULES)'"; then
            MOD="$(printf '%s\n' "$OUT" | grep -Eo "No module named '($NOT_PURE_MODULES)'" | head -1 | sed "s/.*named '//;s/'//")"
            log "[SKIP] pytest-$name — not pure-python (needs $MOD)"
            continue
        fi

        printf '%s\n' "$OUT" | tail -40 >&2
        fail_tier "pytest-$name" "pytest failed (see output above)"
    done
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 3b — submission-consistency oracle (docs vs live tree/artifacts)
# ────────────────────────────────────────────────────────────────────────────
tier_submission_consistency() {
    need node
    log "== TIER submission-consistency: VVIP/scripts/submission_consistency_check.mjs (+ --selftest) =="
    ORACLE="$VVIP_DIR/scripts/submission_consistency_check.mjs"
    [ -f "$ORACLE" ] || fail_tier submission-consistency "VVIP/scripts/submission_consistency_check.mjs not found"
    (
        cd "$VVIP_DIR" || exit 1
        node scripts/submission_consistency_check.mjs || exit 1
        node scripts/submission_consistency_check.mjs --selftest
    ) || fail_tier submission-consistency "submission consistency oracle (or its --selftest) failed"
    pass "submission-consistency"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 3c — wave-1 hatches: traffic-oracle hatch + convoy-brain latch
# ────────────────────────────────────────────────────────────────────────────
tier_wave1_hatches() {
    log "== TIER wave1-hatches: check_forecaster_hatch.sh + check_brain_latch.sh =="
    HATCH="$VVIP_DIR/scripts/check_forecaster_hatch.sh"
    LATCH="$VVIP_DIR/scripts/check_brain_latch.sh"
    [ -f "$HATCH" ] || fail_tier wave1-hatches "VVIP/scripts/check_forecaster_hatch.sh not found"
    [ -f "$LATCH" ] || fail_tier wave1-hatches "VVIP/scripts/check_brain_latch.sh not found"
    # Both run under the gate venv bootstrapped by the pytest tier above.
    bash "$HATCH" || fail_tier wave1-hatches "check_forecaster_hatch.sh failed (traffic-oracle out-of-support hatch)"
    bash "$LATCH" || fail_tier wave1-hatches "check_brain_latch.sh failed (convoy-brain decider novelty latch)"
    pass "wave1-hatches"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 3d — wave-2 forecaster: t0 foundation rung (wiring + real artifact)
# ────────────────────────────────────────────────────────────────────────────
tier_wave2_forecaster() {
    log "== TIER wave2-forecaster: scripts/check_t0_forecaster.sh (gate venv + wave2 venv) =="
    CHECK="$VVIP_DIR/scripts/check_t0_forecaster.sh"
    [ -f "$CHECK" ] || fail_tier wave2-forecaster "VVIP/scripts/check_t0_forecaster.sh not found"
    (
        cd "$VVIP_DIR" || exit 1
        bash scripts/check_t0_forecaster.sh
    ) || fail_tier wave2-forecaster "check_t0_forecaster.sh failed (t0 wiring in the gate venv + real artifact in the wave2 venv)"
    pass "wave2-forecaster"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 3e — wave-2 routing: P90-robust quantile objective + drift guard
# ────────────────────────────────────────────────────────────────────────────
tier_wave2_routing() {
    log "== TIER wave2-routing: scripts/check_routing_quantiles.sh (gate venv) =="
    CHECK="$VVIP_DIR/scripts/check_routing_quantiles.sh"
    [ -f "$CHECK" ] || fail_tier wave2-routing "VVIP/scripts/check_routing_quantiles.sh not found"
    (
        cd "$VVIP_DIR" || exit 1
        bash scripts/check_routing_quantiles.sh
    ) || fail_tier wave2-routing "check_routing_quantiles.sh failed (P90-robust routing objective / protocol-params drift)"
    pass "wave2-routing"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 3f — wave-2 tier-0: decider routine-checkpoint cache gate (full suite)
# ────────────────────────────────────────────────────────────────────────────
tier_wave2_tier0() {
    log "== TIER wave2-tier0: scripts/check_tier0_gate.sh (gate venv) =="
    CHECK="$VVIP_DIR/scripts/check_tier0_gate.sh"
    [ -f "$CHECK" ] || fail_tier wave2-tier0 "VVIP/scripts/check_tier0_gate.sh not found"
    (
        cd "$VVIP_DIR" || exit 1
        bash scripts/check_tier0_gate.sh
    ) || fail_tier wave2-tier0 "check_tier0_gate.sh failed (decider tier-0 gate / full convoy-brain suite)"
    pass "wave2-tier0"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 4 — model-stack smoke (live services)
# ────────────────────────────────────────────────────────────────────────────
tier_models() {
    log "== TIER models: VVIP/scripts/smoke_models.sh =="
    SMOKE="$VVIP_DIR/scripts/smoke_models.sh"
    [ -f "$SMOKE" ] || fail_tier models "VVIP/scripts/smoke_models.sh not found (models stream deliverable)"
    bash "$SMOKE" || fail_tier models "smoke_models.sh failed (decider :8083 / llama :8084 / embedding :8085)"
    pass "models"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 4b — model-drift panel (selftest offline, then the live panel; the
# same live model stack — and env — as the models tier above)
# ────────────────────────────────────────────────────────────────────────────
tier_model_drift() {
    log "== TIER model-drift: VVIP/scripts/model_drift_panel.sh --selftest, then live =="
    PANEL="$VVIP_DIR/scripts/model_drift_panel.sh"
    [ -f "$PANEL" ] || fail_tier model-drift "VVIP/scripts/model_drift_panel.sh not found"
    bash "$PANEL" --selftest || fail_tier model-drift "model_drift_panel.sh --selftest failed"
    bash "$PANEL" || fail_tier model-drift "model_drift_panel.sh live panel failed (decider :8083 / llama :8084 / embedding :8085 / oracle :8091)"
    pass "model-drift"
}

# ────────────────────────────────────────────────────────────────────────────
main() {
    log "${BOLD}VVIP verify_all gate${RESET}"
    tier_frontend_bundle
    tier_frontend_e2e
    tier_gods_eye_check
    tier_gods_eye_canvas
    tier_pytest_suites
    tier_submission_consistency
    tier_wave1_hatches
    tier_wave2_forecaster
    tier_wave2_routing
    tier_wave2_tier0
    tier_models
    tier_model_drift
    log ""
    pass "${BOLD}ALL TIERS PASSED${RESET}"
}

main "$@"
