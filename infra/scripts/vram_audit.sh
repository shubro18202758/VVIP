#!/usr/bin/env bash
# vram_audit.sh — Monitor VRAM usage across GPU consumers
# Run periodically or on-demand to verify budget compliance.

set -euo pipefail

echo "=== VVIP Platform VRAM Audit ==="
echo "Timestamp: $(date -Iseconds)"
echo ""

# Check nvidia-smi availability
if ! command -v nvidia-smi &>/dev/null; then
    echo "ERROR: nvidia-smi not found. Cannot audit VRAM."
    exit 1
fi

# Total GPU memory
echo "--- GPU Summary ---"
nvidia-smi --query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu \
    --format=csv,noheader
echo ""

# Per-process VRAM usage
echo "--- Per-Process VRAM Usage ---"
nvidia-smi --query-compute-apps=pid,name,used_memory \
    --format=csv,noheader 2>/dev/null || echo "(no compute processes)"
echo ""

# Budget compliance check
TOTAL_MB=8192
OLLAMA_BUDGET_MB=5632
ONNX_BUDGET_MB=409
CUDA_OVERHEAD_MB=307
GPU_DB_CACHE_MB=0  # PostGIS + pgRouting + pgvector: intentionally CPU-only
HEADROOM_MB=1844

USED_MB=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1 | tr -d ' ')
FREE_MB=$((TOTAL_MB - USED_MB))

echo "--- Budget Compliance ---"
echo "Total VRAM:      ${TOTAL_MB} MB"
echo "Used:            ${USED_MB} MB"
echo "Free:            ${FREE_MB} MB"
echo "Ollama Budget:   ${OLLAMA_BUDGET_MB} MB"
echo "ONNX Budget:     ${ONNX_BUDGET_MB} MB"
echo "CUDA Overhead:   ${CUDA_OVERHEAD_MB} MB"
echo "GPU DB Cache:    ${GPU_DB_CACHE_MB} MB (PostGIS CPU-only)"
echo "Target Headroom: ${HEADROOM_MB} MB"

if [ "$USED_MB" -gt "$((TOTAL_MB - HEADROOM_MB / 2))" ]; then
    echo ""
    echo "WARNING: VRAM usage exceeds safe threshold!"
    echo "Consider: unloading Ollama model or switching ONNX to CPU EP"
fi
