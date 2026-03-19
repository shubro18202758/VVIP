#!/usr/bin/env bash
# launch-frontend.sh — Start the Command Deck frontend with CPU-only constraints.
# Preserves GPU VRAM entirely for backend AI services (Qwen, ONNX, DQN, OR-Tools).
#
# Usage:
#   ./infra/scripts/launch-frontend.sh          # Development mode (HMR)
#   ./infra/scripts/launch-frontend.sh build     # Production build
#   ./infra/scripts/launch-frontend.sh serve     # Serve production build
#   ./infra/scripts/launch-frontend.sh test      # Run integration agent

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DECK_DIR="$PROJECT_ROOT/services/command-deck"

# ─── CPU-Only Environment ──────────────────────────────────────────────────
# Force all frontend tooling to CPU + system RAM.
# CUDA_VISIBLE_DEVICES="" prevents any accidental GPU detection by Node/Chromium.

export CUDA_VISIBLE_DEVICES=""
export NODE_OPTIONS="--max-old-space-size=512"
export VITE_CJS_IGNORE_WARNING=true

# Disable Chromium GPU compositing if Playwright/headless Chrome is used
export CHROMIUM_FLAGS="--disable-gpu --disable-software-rasterizer"

# Bun-specific: limit worker threads
export UV_THREADPOOL_SIZE=4

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  VVIP Command Deck — CPU-Only Frontend Launcher             ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  CUDA_VISIBLE_DEVICES = (empty — GPU hidden from Node)      ║"
echo "║  NODE_OPTIONS         = --max-old-space-size=512            ║"
echo "║  UV_THREADPOOL_SIZE   = 4                                   ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ─── Ensure Dependencies ───────────────────────────────────────────────────
if [ ! -d "$DECK_DIR/node_modules" ]; then
    echo "[frontend] Installing dependencies..."
    (cd "$DECK_DIR" && bun install)
fi

# ─── Command Router ────────────────────────────────────────────────────────
MODE="${1:-dev}"

case "$MODE" in
    dev)
        echo "[frontend] Starting development server on http://localhost:5173"
        echo "[frontend] Backend proxies: /api/convoy → :8080, /api/traffic → :8081"
        echo ""
        cd "$DECK_DIR" && exec bun run dev
        ;;
    build)
        echo "[frontend] Running production build (CPU-only, heap capped at 1024MB)..."
        export NODE_OPTIONS="--max-old-space-size=1024"
        cd "$DECK_DIR" && bun run build
        echo "[frontend] Build complete. Output: $DECK_DIR/build/"
        echo "[frontend] Run './infra/scripts/launch-frontend.sh serve' to start."
        ;;
    serve)
        if [ ! -d "$DECK_DIR/build" ]; then
            echo "[frontend] No build directory found. Running build first..."
            export NODE_OPTIONS="--max-old-space-size=1024"
            (cd "$DECK_DIR" && bun run build)
        fi
        echo "[frontend] Serving production build on http://localhost:5173"
        export PORT=5173
        export HOST=0.0.0.0
        export NODE_OPTIONS="--max-old-space-size=256"
        cd "$DECK_DIR" && exec bun ./build
        ;;
    test)
        echo "[frontend] Running integration test agent..."
        cd "$DECK_DIR" && exec node tests/integration-agent.mjs
        ;;
    *)
        echo "Usage: $0 {dev|build|serve|test}"
        exit 1
        ;;
esac
