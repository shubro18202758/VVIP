#!/usr/bin/env bash
# bootstrap.sh — First-time setup for the VVIP Convoy Orchestration Platform.
# Validates prerequisites, starts infrastructure, and seeds development data.

set -euo pipefail

echo "=== VVIP Platform Bootstrap ==="
echo ""

# ─── Prerequisite Check ──────────────────────────────────────────
echo "Checking prerequisites..."

check_cmd() {
    if command -v "$1" &>/dev/null; then
        echo "  [OK] $1"
    else
        echo "  [MISSING] $1 — install required"
        return 1
    fi
}

MISSING=0
check_cmd docker        || MISSING=1
check_cmd cargo         || MISSING=1
check_cmd uv            || MISSING=1
check_cmd bun           || MISSING=1
check_cmd ollama        || MISSING=1
check_cmd nvidia-smi    || MISSING=1

if [ "$MISSING" -eq 1 ]; then
    echo ""
    echo "Install missing prerequisites before proceeding."
    echo "  Rust:   https://rustup.rs"
    echo "  uv:     https://docs.astral.sh/uv/getting-started/installation/"
    echo "  Bun:    https://bun.sh/docs/installation"
    echo "  Ollama: https://ollama.com/download"
    exit 1
fi

echo ""

# ─── Start Infrastructure ────────────────────────────────────────
echo "Starting infrastructure containers..."
docker compose -f infra/compose.yml up -d postgis valkey nats
echo "Waiting for PostGIS to be healthy..."
sleep 5

# ─── Seed Development Data ───────────────────────────────────────
echo "Seeding corridor development data..."
docker exec -i vvip-postgis psql -U corridor_admin -d corridor_db \
    < services/corridor-store/seed/sample_corridor.sql 2>/dev/null \
    && echo "  Seed data loaded" \
    || echo "  Seed data may already exist (skipping)"

# ─── Install Dependencies ────────────────────────────────────────
echo ""
echo "Installing Python dependencies (traffic-oracle)..."
(cd services/traffic-oracle && uv sync)

echo "Installing Python dependencies (convoy-brain)..."
(cd services/convoy-brain && uv sync)

echo "Installing GIS network importer dependencies..."
(cd services/corridor-store/importer && uv sync)

echo "Installing frontend dependencies (command-deck)..."
(cd services/command-deck && bun install)

# ─── Pull Qwen Model ─────────────────────────────────────────────
echo ""
echo "Pulling Qwen 3.5 9B model via Ollama (Q4_K_M quantization)..."
ollama pull qwen3.5:9b-q4_K_M 2>/dev/null \
    || echo "  Model pull failed — ensure Ollama is running: ollama serve"

echo ""
echo "=== Bootstrap Complete ==="
echo "Next steps:"
echo "  1. Start Ollama: ollama serve"
echo "  2. Start all services: docker compose -f infra/compose.yml up -d"
echo "  3. Start command-deck: cd services/command-deck && bun dev"
echo "  4. Open dashboard: http://localhost:5173"
