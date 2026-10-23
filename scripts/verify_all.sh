#!/usr/bin/env bash
# verify_all.sh — the VVIP end-to-end acceptance gate.
#
#   bash VVIP/scripts/verify_all.sh          the OFFLINE ladder: no operator
#                                            sign-in, no model call, nothing
#                                            written to any stack
#   bash VVIP/scripts/verify_all.sh --live   the LIVE tiers: signs in to the
#                                            deck through scripts/deck_session.sh,
#                                            then the model stack and its panel
#
# Bootstraps what it needs (the deck's node deps, a pure-python gate venv),
# then runs IN ORDER, failing fast on the first failure.
#
# Offline ladder. The deck tiers serve the adapter-node build/ on a PRIVATE
# preview (:5199, gate OFF and keyless: VEER_OPERATOR_PIN / VEER_SESSION_SECRET /
# VEER_CESIUM_ION_TOKEN removed from its environment); the previews and the
# probes read the local stack GET-only (ORACLE_PORT / BRAIN_PORT /
# EMULATOR_PORT, POSTGIS_CONTAINER — the live argus ports by default):
#
#   TIER scripts-syntax    bash -n / node --check / a python AST parse / a JSON
#                          parse over scripts/ — every gate script, and the
#                          stack-bound tools that cannot run hermetically
#                          (scripts/journeys.mjs, scripts/demo_reset.sh,
#                          scripts/data/journeys/reboot_evidence.json)
#   TIER gods-eye-check    svelte-check on the city-canvas stream's files
#                          (services/command-deck; pre-existing diagnostics in
#                          other streams' files are reported, not gated here)
#   TIER gods-eye-canvas   build the SvelteKit deck + headless SwiftShader
#                          render smoke (tests/gods-eye-smoke.mjs — canvas
#                          non-blank, keyless, proxy; the oracle + emulator
#                          must answer)
#   TIER design-smoke      the cockpit-topology tripwire + design-shell
#                          smoke (tests/design-smoke.mjs — / IS the
#                          convoy-command cockpit, the live URLs 200, token
#                          parity both themes)
#   TIER deck-ui           the deck's own gates on the same build/ and
#                          preview: tests/ops-wall-smoke.mjs (hermetic — every
#                          /api answered by its fixture world; its C6 'no
#                          officer' masking group runs here, where no session
#                          names an officer), tests/contract/run.mjs (recorded
#                          payloads), tests/design-tokens.mjs (token parity,
#                          contrast, hex sweep, the R4 import boundaries —
#                          .svelte included), every tests/*.unit.mjs, the edge-basemap
#                          entry (CSP, <html lang>, /gods-eye/mode), the
#                          ops-write smoke (writer surface gated; GET-only
#                          probes of the oracle) and city-command's C6 'plates'
#                          group (no officer named) when the fused picture
#                          carries an event or dispatch titled with the watch
#                          plate — that group needs live data, and the line
#                          says so when there is none
#   TIER pytest-<suite>    EVERY pytest suite under VVIP/, each one executed
#                          whole: a test that needs torch/onnx or a database
#                          skips itself (importorskip / skipif — pytest prints
#                          the count); a suite is never skipped wholesale
#   TIER cargo             signal-ingress `cargo test --locked` (host cargo,
#                          else the rust:alpine image with cached registry and
#                          target volumes)
#   TIER display-geometry  scripts/gen_display_geometry.py --verify — the real
#                          (OpenStreetMap) road network re-measured from its
#                          committed evidence and extracts; offline
#   TIER ingress-segments  scripts/gen_ingress_segments.py --check —
#                          signal-ingress's compiled-in network equals
#                          corridor.road_segments in the store
#                          ($POSTGIS_CONTAINER, a read-only session)
#   TIER submission-consistency
#                          node scripts/submission_consistency_check.mjs and
#                          its --selftest, from the VVIP dir — the submission
#                          docs vs live tree/artifacts oracle (--vvip-only,
#                          with a NOTE, when no ../submission sits beside the
#                          checkout — a fresh clone of the repository — and
#                          VEER_WORKSPACE_ROOT does not name the workspace)
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
#   TIER wave3-repetition  scripts/repetition_gate.sh --fast from the VVIP
#                          dir — offline selftest of the consistency-under-
#                          repetition gate (comparator/normalizer/FAIL path;
#                          no showcase, no docker, no db). The FULL live run
#                          is on-demand only: bash VVIP/scripts/
#                          repetition_gate.sh (tens of minutes) — NOT in
#                          this ladder.
#   TIER wave3-ledger      scripts/check_degradation_ledger.sh from the VVIP
#                          dir — degradation-ledger taxonomy tests over
#                          decisions_log (gate venv)
#
# --live (the deck at DECK_URL, default http://127.0.0.1:5273):
#
#   TIER live              ONE operator sign-in through scripts/deck_session.sh
#                          as 'Verify-all live tier (automated check)' (the PIN
#                          read at run time from VEER_OPERATOR_PIN,
#                          VEER_OPERATOR_PIN_FILE or veer.env — never printed,
#                          never on a command line); the gate is ON (unless
#                          VERIFY_EXPECT_GATE=off|any) and refuses anonymous
#                          operator reads (events, watchlist, e-challan queue)
#                          that the session gets; the deck's pages; the oracle /
#                          brain / emulator reads through the deck's proxy; the
#                          citizen pages stay anonymous; the Cesium ion token
#                          never reaches an anonymous client; sign-out. GET only.
#   TIER demo-reset        scripts/demo_reset.sh, dry-run (GET + read-only
#                          SELECT): the pre-judging inventory and plan answer,
#                          ending 'dry-run: nothing was written'
#   TIER live-smokes       services/command-deck/tests/ops-live-smoke.mjs (the
#                          upstreams direct: GETs, the twin's pure compute POSTs
#                          and ONE informational chat turn) then
#                          tests/integration-agent.mjs (through the deck's proxy,
#                          gate-aware: one sign-in through the shared helper) —
#                          one after the other, they share the models
#   TIER models            scripts/smoke_models.sh (live model stack)
#   TIER model-drift       scripts/model_drift_panel.sh --selftest, then the
#                          live panel (same env as the models tier)
#   Both model tiers call the shared models (:8083 :8084) and upsert marker
#   documents through the embedding service (:8085) — a private rehearsal
#   whose models are the live ones sets VERIFY_LIVE_MODELS=0 and the run
#   says they were not run.
#   Not part of --live: scripts/argus_full_smoke.sh, scripts/fleet_smoke.sh and
#   scripts/check_tier0_live.sh — each leaves one TEST-titled planning event,
#   so they run on demand only (judged by their exit codes).
#
# On the first failure the script prints "TIER <name> FAILED: <reason>" and
# exits nonzero. Success prints one PASS line per tier.
#
# Env:   ORACLE_PORT / BRAIN_PORT / EMULATOR_PORT (8091 / 8092 / 8090) or
#        ORACLE_URL — the upstreams the previews proxy and the probes read;
#        POSTGIS_CONTAINER (vvip-argus-postgis); VERIFY_PREVIEW_PORT (5199) —
#        the private preview's port (another free port lets two checkouts
#        verify side by side); DECK_URL / DECK_PORT (5273) — the --live deck;
#        CC_PLATE (GJ27EM9001) — the watch plate of the C6 'plates' group;
#        CHROME_PATH (optional — the smokes default to the puppeteer cache);
#        VEER_WORKSPACE_ROOT — the workspace (VVIP/ + submission/) the
#        consistency tier judges when this checkout is not inside it;
#        VEER_CARGO_REGISTRY_VOLUME / VEER_CARGO_TARGET_VOLUME / VEER_RUST_IMAGE
#        for the docker cargo path. scripts/veer_env.sh is NOT sourced: it
#        would export veer.env — the operator PIN included — into every tier.

set -uo pipefail

VVIP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATE_VENV="$VVIP_DIR/.venvs/gate"
DECK="$VVIP_DIR/services/command-deck"

MODE=offline
for arg in "$@"; do
    case "$arg" in
        --live) MODE=live ;;
        -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'usage: bash VVIP/scripts/verify_all.sh [--live]\n' >&2; exit 2 ;;
    esac
done

# The local stack the previews proxy and the GET probes read (hooks.server.ts
# reads the same three variables; the defaults are the live argus ports).
ORACLE_PORT="${ORACLE_PORT:-8091}"
BRAIN_PORT="${BRAIN_PORT:-8092}"
EMULATOR_PORT="${EMULATOR_PORT:-8090}"
ORACLE_URL="${ORACLE_URL:-http://127.0.0.1:${ORACLE_PORT}}"
PREVIEW_PORT="${VERIFY_PREVIEW_PORT:-5199}"
PREVIEW="http://127.0.0.1:${PREVIEW_PORT}"
# free_preview kills whatever holds the preview port: never a deck or an
# upstream this gate reads (a typo'd VERIFY_PREVIEW_PORT must not take one down)
case " ${DECK_PORT:-5273} $ORACLE_PORT $BRAIN_PORT $EMULATOR_PORT " in
    *" $PREVIEW_PORT "*)
        printf 'verify_all: VERIFY_PREVIEW_PORT=%s is a deck/oracle/brain/emulator port — pick a free one (default 5199)\n' "$PREVIEW_PORT" >&2
        exit 2 ;;
esac
# Every preview and deck smoke runs gate-off and keyless, whatever this shell
# exported: a named session would show raw plates (C6), and a key would turn
# the keyless basemap assertions into ion ones.
PREVIEW_ENV=(env -u VEER_OPERATOR_PIN -u VEER_OPERATOR_PIN_FILE -u VEER_SESSION_SECRET
    -u VEER_SESSION_SECURE -u VEER_CESIUM_ION_TOKEN -u CC_PIN
    "ORACLE_PORT=$ORACLE_PORT" "BRAIN_PORT=$BRAIN_PORT" "EMULATOR_PORT=$EMULATOR_PORT")
# The gate does not write into the checkout: no tier (nor a helper script a
# tier calls) leaves a .pytest_cache behind.
export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:+$PYTEST_ADDOPTS }-p no:cacheprovider"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
log()  { printf '%s\n' "$*"; }
pass() { printf '%s[PASS]%s %s\n' "$GREEN" "$RESET" "$*"; }
note() { printf '%s[NOTE]%s %s\n' "$YELLOW" "$RESET" "$*"; }

fail_tier() { # fail_tier <tier> <detail>
    printf '%s%sTIER %s FAILED:%s %s\n' "$RED" "$BOLD" "$1" "$RESET" "$2" >&2
    exit 1
}

need() { command -v "$1" >/dev/null 2>&1 || fail_tier bootstrap "required tool '$1' not found in PATH"; }

# The private preview's port must be ours: a stale deck left there (e.g. a
# showcase spawn whose cleanup missed the real pid) would be reused by a smoke
# and die mid-rebuild — page never ready, origin refusing connections.
free_preview() {
    fuser -k "$PREVIEW_PORT/tcp" >/dev/null 2>&1 || true
    sleep 1
}

# The deck's node deps + the vendored prebuilt Cesium bundle (exactly like the
# Dockerfile does). Run from $DECK.
deck_bootstrap() {
    [ -x node_modules/.bin/vite ] && [ -x node_modules/.bin/svelte-check ] || {
        log "bootstrapping command-deck node_modules (npm install --no-package-lock)…"
        npm install --no-package-lock --no-audit --no-fund >/dev/null 2>&1 || return 1
    }
    if [ ! -f static/cesium/Cesium.js ]; then
        [ -d node_modules/cesium/Build/Cesium ] || {
            npm install --no-package-lock --no-audit --no-fund >/dev/null 2>&1 || return 1
        }
        mkdir -p static && cp -r node_modules/cesium/Build/Cesium static/cesium || return 1
    fi
}

# vite build into build/ (adapter-node). Run from $DECK. `force` rebuilds;
# otherwise an existing build/index.js is reused (the canvas tier just built it).
deck_build() {
    [ "${1:-}" != force ] && [ -f build/index.js ] && return 0
    deck_bootstrap || return 1
    local blog
    blog="$(mktemp)"
    if ! NODE_OPTIONS=--max-old-space-size=1024 ./node_modules/.bin/vite build >"$blog" 2>&1; then
        tail -30 "$blog" >&2
        rm -f "$blog"
        return 1
    fi
    rm -f "$blog"
    [ -f build/index.js ]
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 0 — every script parses (the stack-bound ones cannot run hermetically)
# ────────────────────────────────────────────────────────────────────────────
tier_scripts_syntax() {
    need node; need python3
    log "== TIER scripts-syntax: bash -n / node --check / python AST / JSON over scripts/ =="
    local f n_sh=0 n_js=0 n_py=0 n_json=0
    for f in scripts/journeys.mjs scripts/demo_reset.sh scripts/data/journeys/reboot_evidence.json; do
        [ -f "$VVIP_DIR/$f" ] || fail_tier scripts-syntax "$f is missing"
    done
    (cd "$VVIP_DIR" && node --check scripts/journeys.mjs && bash -n scripts/demo_reset.sh \
        && python3 -c "import json; json.load(open('scripts/data/journeys/reboot_evidence.json'))") \
        || fail_tier scripts-syntax "the stack-bound tools do not parse (journeys.mjs / demo_reset.sh / reboot_evidence.json)"
    for f in "$VVIP_DIR"/scripts/*.sh; do
        bash -n "$f" || fail_tier scripts-syntax "bash -n ${f#"$VVIP_DIR"/}"
        n_sh=$((n_sh + 1))
    done
    for f in "$VVIP_DIR"/scripts/*.mjs "$VVIP_DIR"/scripts/lib/*.mjs; do
        [ -f "$f" ] || continue
        node --check "$f" || fail_tier scripts-syntax "node --check ${f#"$VVIP_DIR"/}"
        n_js=$((n_js + 1))
    done
    for f in "$VVIP_DIR"/scripts/*.py; do
        python3 -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1])' "$f" \
            || fail_tier scripts-syntax "python syntax ${f#"$VVIP_DIR"/}"
        n_py=$((n_py + 1))
    done
    while IFS= read -r -d '' f; do
        python3 -c 'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$f" \
            || fail_tier scripts-syntax "JSON ${f#"$VVIP_DIR"/}"
        n_json=$((n_json + 1))
    done < <(find "$VVIP_DIR/scripts" -name '*.json' -print0)
    pass "scripts-syntax ($n_sh shell, $n_js node, $n_py python, $n_json json — journeys.mjs, demo_reset.sh, reboot_evidence.json included)"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 1 — city-canvas stream: svelte-check on the stream's own files
# ────────────────────────────────────────────────────────────────────────────
# The paths this stream owns/edited; diagnostics anywhere else pre-date it.
GODS_EYE_OWNED_RE='src/lib/gods-eye/|src/lib/ops/|src/lib/i18n/|src/lib/primitives/|src/lib/design/|src/lib/shell/|src/lib/citizen/|src/lib/comms/|src/routes/\(deck\)/|src/routes/\(public\)/|src/hooks.server.ts|src/routes/\+layout.svelte'

tier_gods_eye_check() {
    need node; need npm
    log "== TIER gods-eye-check: svelte-check on the city-canvas files =="
    (
        cd "$DECK" || exit 1
        deck_bootstrap || exit 1
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
# TIER 2 — city-canvas stream: build + headless SwiftShader render smoke
# ────────────────────────────────────────────────────────────────────────────
tier_gods_eye_canvas() {
    need node; need npm
    log "== TIER gods-eye-canvas: deck build + SwiftShader canvas smoke on $PREVIEW (oracle :$ORACLE_PORT, emulator :$EMULATOR_PORT) =="
    # `vite build` rewrites the very build/ dir a server on the preview port
    # serves from: free the port first so the smoke spawns its own origin.
    free_preview
    (cd "$DECK" && deck_build force) || fail_tier gods-eye-canvas "deck build failed (vite build in services/command-deck)"
    (cd "$DECK" && "${PREVIEW_ENV[@]}" node tests/gods-eye-smoke.mjs "$PREVIEW") \
        || fail_tier gods-eye-canvas "headless render smoke failed (node tests/gods-eye-smoke.mjs $PREVIEW in services/command-deck)"
    free_preview
    pass "gods-eye-canvas (non-blank under SwiftShader, keyless, proxy ok)"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 3 — city-canvas stream: cockpit-topology tripwire + design-shell smoke
# ────────────────────────────────────────────────────────────────────────────
tier_design_smoke() {
    need node; need npm
    log "== TIER design-smoke: cockpit-topology tripwire + design-shell smoke on $PREVIEW =="
    # The smoke serves build/ itself (spawning `PORT=<port> node build` when
    # nothing answers) — same port hygiene as the canvas tier.
    free_preview
    (cd "$DECK" && deck_build) || fail_tier design-smoke "deck build failed (vite build in services/command-deck)"
    (cd "$DECK" && "${PREVIEW_ENV[@]}" node tests/design-smoke.mjs "$PREVIEW") \
        || fail_tier design-smoke "cockpit-topology/design-shell smoke failed (node tests/design-smoke.mjs $PREVIEW in services/command-deck)"
    free_preview
    pass "design-smoke (cockpit-topology tripwire: / is the cockpit + the live urls 200 + design-shell parity)"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 4 — the deck's own gates: hermetic ops wall, contract, units, edge,
# writer surface, and the C6 'no officer' groups on the gate-off preview
# ────────────────────────────────────────────────────────────────────────────
deck_step() { # deck_step <what> <cmd…> — run in the deck dir, gate-off/keyless; fail the tier on non-zero
    local what="$1"; shift
    log "-- deck-ui: $what"
    (cd "$DECK" && "${PREVIEW_ENV[@]}" "$@") || fail_tier deck-ui "$what failed (re-run it in services/command-deck)"
}

# How many events/dispatches of the fused picture carry the watch plate in
# their title (the C6 'plates' group's precondition) — GET only.
plate_titled_count() {
    local kind n total=0
    for kind in event dispatch; do
        n="$(curl -s -m 10 "$ORACLE_URL/api/v1/city/entities?kinds=$kind" 2>/dev/null | python3 -c '
import json, sys
plate = sys.argv[1]
try:
    ents = json.load(sys.stdin).get("entities") or []
except Exception:
    print(0); sys.exit(0)
print(sum(1 for e in ents if plate in str((e.get("props") or {}).get("title") or "")
                            + str((e.get("props") or {}).get("event_title") or "")))' "$1" 2>/dev/null)"
        total=$((total + ${n:-0}))
    done
    printf '%s\n' "$total"
}

tier_deck_ui() {
    need node; need npm; need curl; need python3
    log "== TIER deck-ui: ops-wall (hermetic) + contract + units + edge-basemap + ops-write + C6 'no officer' on the gate-off preview $PREVIEW =="
    free_preview
    (cd "$DECK" && deck_build) || fail_tier deck-ui "deck build failed (vite build in services/command-deck)"
    deck_step "ops-wall smoke (hermetic; C6 'no officer' masking included)" node tests/ops-wall-smoke.mjs "$PREVIEW"
    free_preview
    deck_step "contract replay (recorded oracle payloads)" node tests/contract/run.mjs
    deck_step "design tokens (parity, contrast, hex sweep, the R4 import boundaries incl. .svelte)" node tests/design-tokens.mjs
    local unit units=0
    for unit in "$DECK"/tests/*.unit.mjs; do
        [ -f "$unit" ] || continue
        deck_step "unit $(basename "$unit")" node "tests/$(basename "$unit")"
        units=$((units + 1))
    done
    [ "$units" -gt 0 ] || fail_tier deck-ui "no tests/*.unit.mjs found in services/command-deck"
    deck_step "edge-basemap entry (CSP, <html lang>, /gods-eye/mode)" sh -c \
        './node_modules/.bin/esbuild tests/edge-basemap.entry.ts --bundle --platform=node --format=esm --alias:\$lib=./src/lib --outfile=tests/artifacts/edge-basemap-bundle.mjs --log-level=warning && node tests/artifacts/edge-basemap-bundle.mjs'
    deck_step "ops-write smoke (writer surface gated; GET-only probes of $ORACLE_URL)" node tests/ops-write-smoke.mjs "$ORACLE_URL"
    local plate="${CC_PLATE:-GJ27EM9001}" titled plates_line
    titled="$(plate_titled_count "$plate")"
    if [ "${titled:-0}" -gt 0 ]; then
        free_preview
        deck_step "city-command C6 'plates' group (no officer named; $titled plate-titled item(s) at $ORACLE_URL)" \
            env CC_SMOKE_ONLY=plates node tests/city-command-smoke.mjs "$PREVIEW"
        free_preview
        plates_line="city-command C6 'plates' ok"
    else
        note "city-command C6 'plates' group NOT RUN: no event or dispatch at $ORACLE_URL carries $plate in its title (it needs live data; the masked path ran hermetically in ops-wall-smoke above)"
        plates_line="city-command C6 'plates' not run (no plate-titled event)"
    fi
    pass "deck-ui (ops-wall hermetic incl. C6 masking, contract replay, design tokens, $units unit tests, edge-basemap, ops-write GET-only; $plates_line)"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 5 — every pytest suite under VVIP/, each executed whole
# ────────────────────────────────────────────────────────────────────────────
# Bash array so extras brackets reach pip verbatim (no word-split/glob).
# pyyaml + gymnasium (CPU, pure python) are imported by the oracle/brain
# suites; torch/onnx stay out — the tests needing them skip themselves.
GATE_DEPS=(pytest pytest-asyncio httpx fastapi uvicorn pydantic structlog
prometheus-client valkey numpy polars pyarrow scikit-learn asyncpg
geopandas shapely osmnx pandas click ortools psutil
nats-py 'psycopg[binary,pool]' pydantic-settings pyyaml gymnasium)

tier_python_bootstrap() {
    need python3
    log "== TIER pytest: bootstrapping gate venv at $GATE_VENV =="
    if [ ! -x "$GATE_VENV/bin/python" ]; then
        python3 -m venv "$GATE_VENV" || fail_tier pytest-bootstrap "python3 -m venv failed"
    fi
    "$GATE_VENV/bin/pip" install -q --disable-pip-version-check "${GATE_DEPS[@]}" \
        || fail_tier pytest-bootstrap "pip install of gate deps failed"
}

tier_pytest_suites() {
    tier_python_bootstrap

    # One line per suite dir.
    mapfile -t SUITES < <(
        find "$VVIP_DIR" -name 'test_*.py' -not -path '*/node_modules/*' \
             -not -path '*/.venvs/*' -not -path '*/.venv/*' -not -path '*/build/*' \
             -printf '%h\n' | sort -u
    )
    [ "${#SUITES[@]}" -gt 0 ] || fail_tier pytest "no pytest suites discovered under VVIP/"

    local suite name src out status
    for suite in "${SUITES[@]}"; do
        name="${suite#"$VVIP_DIR"/}"
        log "== TIER pytest-$name =="
        src=""
        [ -d "$suite/../src" ] && src="$(cd "$suite/../src" && pwd)"
        if [ -n "$src" ]; then
            out="$(cd "$suite" && PYTHONPATH="$src" "$GATE_VENV/bin/python" -m pytest . -q 2>&1)"
        else
            out="$(cd "$suite" && "$GATE_VENV/bin/python" -m pytest . -q 2>&1)"
        fi
        status=$?
        if [ "$status" -ne 0 ]; then
            printf '%s\n' "$out" | tail -40 >&2
            fail_tier "pytest-$name" "pytest failed (exit $status, see above)"
        fi
        pass "pytest-$name ($(printf '%s\n' "$out" | tail -1))"
    done
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 6 — signal-ingress: cargo test
# ────────────────────────────────────────────────────────────────────────────
tier_cargo() {
    local ingress="$VVIP_DIR/services/signal-ingress" out status how
    log "== TIER cargo: signal-ingress cargo test --locked =="
    if command -v cargo >/dev/null 2>&1; then
        how="host cargo"
        out="$(cd "$ingress" && cargo test --locked 2>&1)"
        status=$?
    else
        need docker
        # The Dockerfile's builder (rust:alpine + the musl toolchain); the
        # source is mounted read-only and every build artefact stays in the
        # two named volumes, so nothing lands in the tree.
        how="${VEER_RUST_IMAGE:-rust:alpine} in docker"
        # one target volume PER CHECKOUT: every checkout mounts its sources at
        # the same /src, so a shared target could reuse another checkout's
        # build as up to date (a stale test count was seen that way)
        local target_vol
        target_vol="${VEER_CARGO_TARGET_VOLUME:-veer-cargo-target-$(printf '%s' "$ingress" | sha256sum | cut -c1-12)}"
        out="$(docker run --rm -v "$ingress:/src:ro" \
            -v "${VEER_CARGO_REGISTRY_VOLUME:-veer-cargo-registry}:/usr/local/cargo/registry" \
            -v "$target_vol:/target" -e CARGO_TARGET_DIR=/target \
            -w /src "${VEER_RUST_IMAGE:-rust:alpine}" \
            sh -c 'apk add -q --no-cache musl-dev pkgconfig build-base >/dev/null 2>&1 && cargo test --locked' 2>&1)"
        status=$?
    fi
    if [ "$status" -ne 0 ] || ! printf '%s\n' "$out" | grep -q '^test result: ok'; then
        printf '%s\n' "$out" | tail -40 >&2
        fail_tier cargo "cargo test --locked failed in services/signal-ingress ($how, exit $status)"
    fi
    pass "cargo ($how: $(printf '%s\n' "$out" | grep -Eo '^test result: ok\. [0-9]+ passed' | awk '{s += $4} END {print s + 0}') passed across $(printf '%s\n' "$out" | grep -c '^test result: ok') test binaries)"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 7 — the road network: OSM evidence re-measured (offline)
# ────────────────────────────────────────────────────────────────────────────
tier_display_geometry() {
    need python3
    log "== TIER display-geometry: scripts/gen_display_geometry.py --verify (the OSM road network, offline) =="
    local out
    out="$(cd "$VVIP_DIR" && python3 scripts/gen_display_geometry.py --verify 2>&1)" || {
        printf '%s\n' "$out" | tail -30 >&2
        fail_tier display-geometry "gen_display_geometry.py --verify failed (the network drifted from its OSM evidence — see above)"
    }
    pass "display-geometry ($(printf '%s\n' "$out" | grep -E '^network:' | tail -1))"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 8 — signal-ingress's compiled-in network vs the store
# ────────────────────────────────────────────────────────────────────────────
tier_ingress_segments() {
    need python3; need docker
    local pg="${POSTGIS_CONTAINER:-vvip-argus-postgis}" out
    log "== TIER ingress-segments: scripts/gen_ingress_segments.py --check against $pg (read-only) =="
    [ "$(docker inspect -f '{{.State.Running}}' "$pg" 2>/dev/null)" = true ] \
        || fail_tier ingress-segments "store container $pg is not running (POSTGIS_CONTAINER=<container> points the check at another store)"
    out="$(cd "$VVIP_DIR" && python3 scripts/gen_ingress_segments.py --check --container "$pg" 2>&1)" || {
        printf '%s\n' "$out" | tail -20 >&2
        fail_tier ingress-segments "services/signal-ingress/src/feeds/segments_ahmedabad.rs differs from corridor.road_segments in $pg — regenerate it (scripts/gen_ingress_segments.py) and rebuild signal-ingress"
    }
    pass "ingress-segments (segments_ahmedabad.rs matches corridor.road_segments in $pg)"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 9 — submission-consistency oracle (docs vs live tree/artifacts)
# ────────────────────────────────────────────────────────────────────────────
tier_submission_consistency() {
    need node
    log "== TIER submission-consistency: VVIP/scripts/submission_consistency_check.mjs (+ --selftest) =="
    ORACLE="$VVIP_DIR/scripts/submission_consistency_check.mjs"
    [ -f "$ORACLE" ] || fail_tier submission-consistency "VVIP/scripts/submission_consistency_check.mjs not found"
    # The submission documents live in the workspace AROUND this repository
    # (../submission). A checkout with none beside it — a fresh clone of the
    # repository, a git worktree — is judged --vvip-only (said, not hidden),
    # unless VEER_WORKSPACE_ROOT names the workspace to judge.
    local scope_args=() scope="workspace documents + VVIP"
    if [ -n "${VEER_WORKSPACE_ROOT:-}" ] \
        && [ "$(cd "$VEER_WORKSPACE_ROOT/VVIP" 2>/dev/null && pwd -P)" != "$(cd "$VVIP_DIR" && pwd -P)" ]; then
        fail_tier submission-consistency "VEER_WORKSPACE_ROOT=$VEER_WORKSPACE_ROOT does not hold THIS checkout as VVIP/ ($VVIP_DIR) — the oracle would judge another tree"
    fi
    if [ -z "${VEER_WORKSPACE_ROOT:-}" ] && [ ! -d "$VVIP_DIR/../submission" ]; then
        scope_args=(--vvip-only)
        scope="VVIP only — no ../submission beside the checkout"
        note "submission-consistency: no ../submission beside $VVIP_DIR and VEER_WORKSPACE_ROOT unset — the repository is judged --vvip-only; the workspace documents are NOT judged by this run"
    fi
    (
        cd "$VVIP_DIR" || exit 1
        node scripts/submission_consistency_check.mjs ${scope_args[@]+"${scope_args[@]}"} || exit 1
        node scripts/submission_consistency_check.mjs ${scope_args[@]+"${scope_args[@]}"} --selftest
    ) || fail_tier submission-consistency "submission consistency oracle (or its --selftest) failed"
    pass "submission-consistency ($scope)"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 10 — wave-1 hatches: traffic-oracle hatch + convoy-brain latch
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
# TIER 11 — wave-2 forecaster: t0 foundation rung (wiring + real artifact)
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
# TIER 12 — wave-2 routing: P90-robust quantile objective + drift guard
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
# TIER 13 — wave-2 tier-0: decider routine-checkpoint cache gate (full suite)
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
# TIER 14 — wave-3 repetition: consistency-under-repetition gate (selftest)
# ────────────────────────────────────────────────────────────────────────────
tier_wave3_repetition() {
    log "== TIER wave3-repetition: scripts/repetition_gate.sh --fast (offline selftest; the FULL live gate is on-demand 'bash scripts/repetition_gate.sh' — tens of minutes — and is NOT run in this ladder) =="
    GATE="$VVIP_DIR/scripts/repetition_gate.sh"
    [ -f "$GATE" ] || fail_tier wave3-repetition "VVIP/scripts/repetition_gate.sh not found"
    (
        cd "$VVIP_DIR" || exit 1
        bash scripts/repetition_gate.sh --fast
    ) || fail_tier wave3-repetition "repetition_gate.sh --fast failed (comparator / normalizer / FAIL-path selftest)"
    pass "wave3-repetition (--fast selftest; full live gate is on-demand scripts/repetition_gate.sh)"
}

# ────────────────────────────────────────────────────────────────────────────
# TIER 15 — wave-3 ledger: degradation-ledger taxonomy over decisions_log
# ────────────────────────────────────────────────────────────────────────────
tier_wave3_ledger() {
    log "== TIER wave3-ledger: scripts/check_degradation_ledger.sh (gate venv) =="
    CHECK="$VVIP_DIR/scripts/check_degradation_ledger.sh"
    [ -f "$CHECK" ] || fail_tier wave3-ledger "VVIP/scripts/check_degradation_ledger.sh not found"
    (
        cd "$VVIP_DIR" || exit 1
        bash scripts/check_degradation_ledger.sh
    ) || fail_tier wave3-ledger "check_degradation_ledger.sh failed (degradation-ledger taxonomy over decisions_log)"
    pass "wave3-ledger"
}

# ────────────────────────────────────────────────────────────────────────────
# LIVE — the signed-in deck (GET only after the one sign-in POST)
# ────────────────────────────────────────────────────────────────────────────
tier_live() {
    need curl; need python3
    local deck="${DECK_URL:-http://127.0.0.1:${DECK_PORT:-5273}}" expect="${VERIFY_EXPECT_GATE:-on}" p code
    deck="${deck%/}"
    log "== TIER live: operator sign-in at $deck (scripts/deck_session.sh), the gate, the pages, the proxied reads =="
    # shellcheck source=deck_session.sh
    . "$VVIP_DIR/scripts/deck_session.sh"
    trap 'deck_signout >/dev/null 2>&1' EXIT
    deck_signin "$deck" "Verify-all live tier (automated check)" \
        || fail_tier live "operator sign-in at $deck failed (see above) — not retried: the deck throttles failed sign-ins"
    case "$expect:$DECK_GATE" in
        on:on|off:off|any:*) ;;
        on:off) fail_tier live "the operator gate is OFF at $deck — every operator feed would be open to the LAN (set VEER_OPERATOR_PIN + VEER_SESSION_SECRET in veer.env: scripts/veer_env.sh --set KEY -)" ;;
        *) fail_tier live "operator gate at $deck is '$DECK_GATE', expected '$expect' (VERIFY_EXPECT_GATE)" ;;
    esac

    if [ "$DECK_GATE" = on ]; then
        deck_curl -s -m 10 "$deck/api/session" | python3 -c '
import json, sys
sys.exit(0 if json.load(sys.stdin).get("officer") == sys.argv[1] else 1)' "$DECK_OFFICER" 2>/dev/null \
            || fail_tier live "GET $deck/api/session does not name the signed-in officer"
        # default-deny: operator reads (events, the plate-bearing watchlist and
        # e-challan queue) refused to an anonymous client
        for p in /api/v1/events/active /api/v1/vehicles '/api/v1/enforcement/recommendations?status=pending'; do
            code="$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$deck$p")"
            [ "$code" = 403 ] || fail_tier live "anonymous GET $p answered $code, expected 403 (the gate must refuse it)"
        done
    fi
    for p in /api/v1/events/active /api/v1/movements/active /api/v1/adaptive/status \
             '/api/v1/city/entities?kinds=event' /api/v1/vehicles \
             '/api/v1/enforcement/recommendations?status=pending' /api/convoy/health /api/emulator/health; do
        code="$(deck_curl -s -m 30 -o /dev/null -w '%{http_code}' "$deck$p")"
        [ "$code" = 200 ] || fail_tier live "GET $p through the deck answered $code with the session, expected 200"
    done
    for p in / /ops /gods-eye /comms /chat /citizen /driver; do
        code="$(deck_curl -s -m 30 -o /dev/null -w '%{http_code}' "$deck$p")"
        [ "$code" = 200 ] || fail_tier live "page $p answered $code with the session, expected 200"
    done
    # captured first: `curl | grep -q` under pipefail fails when grep exits
    # on the first match while curl is still writing a larger page (SIGPIPE)
    local root_html
    root_html="$(deck_curl -s -m 30 "$deck/")"
    [[ "$root_html" == *'data-template="convoy-command"'* ]] \
        || fail_tier live "/ is not the Convoy Command cockpit (no data-template=\"convoy-command\" in the server render)"
    for p in /citizen /driver; do
        code="$(curl -s -m 30 -o /dev/null -w '%{http_code}' "$deck$p")"
        [ "$code" = 200 ] || fail_tier live "public page $p answered $code anonymously, expected 200 (citizen pages never need a sign-in)"
    done
    # The Cesium ion token reaches signed-in operators only: an anonymous
    # /gods-eye/mode carries no token-shaped value (judged here, never printed).
    curl -s -m 15 "$deck/gods-eye/mode" | python3 -c '
import json, re, sys
doc = json.load(sys.stdin)
token = doc.get("token") or doc.get("ion_token") or doc.get("imageryToken")
sys.exit(1 if token or re.search(r"eyJ[A-Za-z0-9_-]{10,}\.", json.dumps(doc)) else 0)' 2>/dev/null \
        || fail_tier live "anonymous GET /gods-eye/mode carries a token (or is not JSON) — the ion token must stay with signed-in operators"
    local mode gate="$DECK_GATE"
    mode="$(deck_curl -s -m 15 "$deck/gods-eye/mode" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("mode", "?"))' 2>/dev/null)"
    deck_signout
    trap - EXIT
    pass "live (gate $gate at $deck$([ "$gate" = on ] && printf '%s' '; anonymous operator reads refused'); 8 proxied reads + 7 pages 200$([ "$gate" = on ] && printf '%s' ' signed in'); citizen pages anonymous; basemap '${mode:-?}', no token anonymously)"
}

# ────────────────────────────────────────────────────────────────────────────
# LIVE — the pre-judging inventory, dry-run (GET + read-only SELECT)
# ────────────────────────────────────────────────────────────────────────────
tier_demo_reset() {
    local out last
    log "== TIER demo-reset: scripts/demo_reset.sh dry-run (GET + read-only SELECT; nothing written) =="
    out="$(bash "$VVIP_DIR/scripts/demo_reset.sh" 2>&1)" || {
        printf '%s\n' "$out" | tail -25 >&2
        fail_tier demo-reset "scripts/demo_reset.sh (dry-run) failed (see above)"
    }
    last="$(printf '%s\n' "$out" | grep -v '^[[:space:]]*$' | tail -1)"
    [[ "$last" == *'dry-run: nothing was written'* ]] || {
        printf '%s\n' "$out" | tail -25 >&2
        fail_tier demo-reset "the dry-run did not end with 'dry-run: nothing was written'"
    }
    pass "demo-reset (dry-run inventory + plan answered; nothing was written)"
}

# ────────────────────────────────────────────────────────────────────────────
# LIVE — the deck's live smokes (read-only; one after the other: shared models)
# ────────────────────────────────────────────────────────────────────────────
tier_live_smokes() {
    need node
    log "== TIER live-smokes: tests/ops-live-smoke.mjs (upstreams: GET + twin compute + one chat turn), then tests/integration-agent.mjs (the deck's proxy, gate-aware) =="
    (cd "$DECK" && node tests/ops-live-smoke.mjs) \
        || fail_tier live-smokes "ops-live-smoke.mjs failed (run it in services/command-deck; ORACLE_URL/BRAIN_URL/EMULATOR_URL or the *_PORT variables pick the stack)"
    (cd "$DECK" && node tests/integration-agent.mjs) \
        || fail_tier live-smokes "integration-agent.mjs failed (run it in services/command-deck; DECK_URL / DECK_PORT pick the deck)"
    pass "live-smokes (ops-live-smoke + integration-agent; GET + pure compute + one chat turn, nothing written)"
}

# ────────────────────────────────────────────────────────────────────────────
# LIVE — model-stack smoke
# ────────────────────────────────────────────────────────────────────────────
tier_models() {
    log "== TIER models: VVIP/scripts/smoke_models.sh =="
    SMOKE="$VVIP_DIR/scripts/smoke_models.sh"
    [ -f "$SMOKE" ] || fail_tier models "VVIP/scripts/smoke_models.sh not found (models stream deliverable)"
    bash "$SMOKE" || fail_tier models "smoke_models.sh failed (decider :8083 / llama :8084 / embedding :8085)"
    pass "models"
}

# ────────────────────────────────────────────────────────────────────────────
# LIVE — model-drift panel (selftest offline, then the live panel; the same
# live model stack — and env — as the models tier above)
# ────────────────────────────────────────────────────────────────────────────
tier_model_drift() {
    log "== TIER model-drift: VVIP/scripts/model_drift_panel.sh --selftest, then live =="
    PANEL="$VVIP_DIR/scripts/model_drift_panel.sh"
    [ -f "$PANEL" ] || fail_tier model-drift "VVIP/scripts/model_drift_panel.sh not found"
    bash "$PANEL" --selftest || fail_tier model-drift "model_drift_panel.sh --selftest failed"
    bash "$PANEL" || fail_tier model-drift "model_drift_panel.sh live panel failed (decider :8083 / llama :8084 / embedding :8085 / oracle)"
    pass "model-drift"
}

# ────────────────────────────────────────────────────────────────────────────
main() {
    log "${BOLD}VVIP verify_all gate ($MODE)${RESET}"
    if [ "$MODE" = live ]; then
        tier_live
        tier_demo_reset
        tier_live_smokes
        if [ "${VERIFY_LIVE_MODELS:-1}" = 0 ]; then
            note "models + model-drift NOT RUN (VERIFY_LIVE_MODELS=0 — a private rehearsal whose models are the live ones)"
            log ""
            pass "${BOLD}LIVE TIERS PASSED except models + model-drift (not run: VERIFY_LIVE_MODELS=0)${RESET}"
            return 0
        fi
        tier_models
        tier_model_drift
        log ""
        pass "${BOLD}ALL TIERS PASSED (live)${RESET}"
        return 0
    fi
    # The offline ladder never needs the operator gate's secrets or the ion
    # key, whatever this shell exported (every child below inherits none).
    unset VEER_OPERATOR_PIN VEER_OPERATOR_PIN_FILE VEER_SESSION_SECRET VEER_SESSION_SECURE \
        VEER_CESIUM_ION_TOKEN CC_PIN
    tier_scripts_syntax
    tier_gods_eye_check
    tier_gods_eye_canvas
    tier_design_smoke
    tier_deck_ui
    tier_pytest_suites
    tier_cargo
    tier_display_geometry
    tier_ingress_segments
    tier_submission_consistency
    tier_wave1_hatches
    tier_wave2_forecaster
    tier_wave2_routing
    tier_wave2_tier0
    tier_wave3_repetition
    tier_wave3_ledger
    log ""
    pass "${BOLD}ALL TIERS PASSED${RESET}"
}

main "$@"
