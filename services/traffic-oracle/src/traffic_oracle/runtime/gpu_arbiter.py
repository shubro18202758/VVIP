"""GPU Arbiter — VRAM budget manager for the 8GB RTX 4070 constraint.

This is the CRITICAL resource governor for the entire platform. It enforces
strict VRAM allocation budgets and prevents OOM kills that would crash
either the LLM or the traffic prediction models.

VRAM Budget (8192 MB total):
┌─────────────────────────────────────┬───────────┬──────────┐
│ Component                           │ Budget MB │ Priority │
├─────────────────────────────────────┼───────────┼──────────┤
│ Qwen 3.5 9B (Q4_K_M via Ollama)    │ 5632      │ 1 (high) │
│ CUDA Runtime Overhead               │ 307       │ 0 (sys)  │
│ ONNX Flow Forecaster Inference      │ 409       │ 2 (med)  │
│ Scratch / Safety Headroom           │ 1844      │ 3 (low)  │
├─────────────────────────────────────┼───────────┼──────────┤
│ TOTAL                               │ 8192      │          │
└─────────────────────────────────────┴───────────┴──────────┘

Arbitration Rules:
    1. Ollama/Qwen ALWAYS gets priority — it is the orchestration brain.
    2. ONNX inference runs on CUDA EP by default; falls back to CPU EP
       if free VRAM < 409 MB after Ollama allocation.
    3. PyTorch training NEVER runs concurrently with Ollama inference —
       training is scheduled in dedicated offline windows.
    4. All non-critical tensors (training data, intermediate activations)
       are pinned to system RAM, never VRAM.
"""

from __future__ import annotations

import asyncio
import subprocess
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog

logger = structlog.get_logger(__name__)

# VRAM budget constants (MB)
TOTAL_VRAM_MB = 8192
OLLAMA_BUDGET_MB = 5632
CUDA_OVERHEAD_MB = 307
ONNX_INFERENCE_MB = 409
HEADROOM_MB = TOTAL_VRAM_MB - OLLAMA_BUDGET_MB - CUDA_OVERHEAD_MB - ONNX_INFERENCE_MB

# PostGIS + pgRouting + pgvector: intentionally CPU-only (zero VRAM)
GPU_DB_CACHE_MB = 0


class GpuArbiter:
    """Manages VRAM allocation across competing GPU consumers.

    Queries nvidia-smi for real-time VRAM utilization and enforces
    budget limits before allowing GPU workloads to proceed.
    """

    def __init__(self) -> None:
        self._gpu_available = self._detect_gpu()
        self._exclusive_lock = asyncio.Lock()
        if self._gpu_available:
            logger.info(
                "gpu_arbiter.init",
                total_vram_mb=TOTAL_VRAM_MB,
                ollama_budget_mb=OLLAMA_BUDGET_MB,
                onnx_budget_mb=ONNX_INFERENCE_MB,
                headroom_mb=HEADROOM_MB,
            )
        else:
            logger.warning("gpu_arbiter.init", msg="No NVIDIA GPU detected — CPU-only mode")

    def _detect_gpu(self) -> bool:
        """Check if nvidia-smi is available."""
        try:
            subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                check=True,
                timeout=5,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def get_free_vram_mb(self) -> int:
        """Query current free VRAM from nvidia-smi."""
        if not self._gpu_available:
            return 0
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            return int(result.stdout.strip())
        except (subprocess.CalledProcessError, ValueError):
            logger.error("gpu_arbiter.query_failed")
            return 0

    def can_allocate_onnx(self) -> bool:
        """Check if there's enough free VRAM for ONNX inference."""
        free = self.get_free_vram_mb()
        can = free >= ONNX_INFERENCE_MB + HEADROOM_MB // 2
        logger.debug("gpu_arbiter.can_allocate_onnx", free_mb=free, allowed=can)
        return can

    def can_train(self) -> bool:
        """Check if GPU training is safe (Ollama must be idle/unloaded)."""
        free = self.get_free_vram_mb()
        # Training needs at least 4GB free (Ollama must be unloaded)
        can = free >= 4096
        logger.debug("gpu_arbiter.can_train", free_mb=free, allowed=can)
        return can

    def get_onnx_providers(self) -> list[str]:
        """Return ONNX Runtime execution providers based on VRAM availability."""
        if self._gpu_available and self.can_allocate_onnx():
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        return ["CPUExecutionProvider"]

    @asynccontextmanager
    async def acquire_exclusive(self) -> AsyncIterator[None]:
        """Acquire exclusive GPU access for operations that cannot share VRAM.

        Use this for training or other heavy GPU workloads that require
        mutual exclusion with ONNX inference.
        """
        async with self._exclusive_lock:
            logger.info("gpu_arbiter.exclusive_acquired")
            try:
                yield
            finally:
                logger.info("gpu_arbiter.exclusive_released")
