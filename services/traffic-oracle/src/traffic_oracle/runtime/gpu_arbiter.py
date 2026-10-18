"""GPU Arbiter — memory-budget manager for the unified-memory pool.

v2 (adaptive stream): the retired v1 accounting assumed a discrete
8 GB GPU with Ollama/Qwen resident.  The deployment target is a
GB10-class unified-memory machine (~121.4 GiB, MemTotal 127,533,976 kB
per /proc/meminfo — nvidia-smi reports memory.total/used/free as N/A
there, so /proc/meminfo is the authoritative source).

Budget resolution order (first hit wins):

    1. explicit ``budget_mb`` constructor argument (tests / overrides)
    2. ``VEER_UNIFIED_BUDGET_MB`` environment variable
    3. ``memory.unified_budget_mb`` (or ``_gib``) in the models-owned
       ``veer_models.yaml`` (path: ``VEER_MODELS_CONFIG`` env or the
       repo-default candidates below)
    4. ``MemTotal`` from /proc/meminfo
    5. conservative default 121_440 MiB (GB10 hardware facts)

Traffic-oracle's own reservation stays 409 MiB for the DSTGAT ONNX
forecaster plus a CUDA safety margin; heavy model residents (decider
27B, Gemma, embedders) are budgeted in veer_models.yaml by the models
stream — this arbiter only decides whether *our* inference may use the
CUDA execution provider or must fall back to CPU, and whether RL
training windows are safe.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable

import structlog

logger = structlog.get_logger(__name__)

# Conservative fallback (GB10 measured pool ≈ 121.4 GiB).
DEFAULT_UNIFIED_BUDGET_MB = 121_440

# Repo-default candidates for the models-owned config (C0 path map).
VEER_MODELS_CONFIG_CANDIDATES: tuple[str, ...] = (
    "VVIP/models/configs/veer_models.yaml",
    "models/configs/veer_models.yaml",
    "/opt/veer/models/configs/veer_models.yaml",
)

# Traffic-oracle's own slice of the unified pool.
ONNX_INFERENCE_MB = 409          # DSTGAT forecaster session budget (unchanged)
CUDA_SAFETY_MARGIN_MB = 2048     # keep this much free before taking CUDA EP
TRAIN_MIN_FREE_MB = 4096         # RL training window safety floor (unchanged)

# PostGIS + pgRouting + pgvector: intentionally CPU-only (zero GPU memory)
GPU_DB_CACHE_MB = 0


def _read_meminfo(path: str = "/proc/meminfo") -> dict[str, int]:
    """Parse /proc/meminfo into {key: kB} (missing file → empty dict)."""
    values: dict[str, int] = {}
    try:
        with open(path) as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    try:
                        values[parts[0][:-1]] = int(parts[1])
                    except ValueError:
                        continue
    except OSError:
        return {}
    return values


def meminfo_memtotal_mb(meminfo: dict[str, int] | None = None) -> int | None:
    """MemTotal in MiB from /proc/meminfo content (None if unavailable)."""
    info = meminfo if meminfo is not None else _read_meminfo()
    total_kb = info.get("MemTotal")
    if total_kb is None or total_kb <= 0:
        return None
    return total_kb // 1024


def meminfo_memavailable_mb(meminfo: dict[str, int] | None = None) -> int | None:
    """MemAvailable in MiB (None if unavailable)."""
    info = meminfo if meminfo is not None else _read_meminfo()
    avail_kb = info.get("MemAvailable")
    if avail_kb is None or avail_kb < 0:
        return None
    return avail_kb // 1024


def _budget_from_yaml(path: Path) -> int | None:
    """Extract memory.unified_budget_mb (or _gib) from veer_models.yaml."""
    try:
        import yaml  # lazy: dependency of traffic-oracle (pyproject)
    except ImportError:  # pragma: no cover - pyproject declares pyyaml
        return None
    try:
        with open(path) as fh:
            doc = yaml.safe_load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    memory = doc.get("memory", doc)
    if not isinstance(memory, dict):
        return None
    mb = memory.get("unified_budget_mb")
    if isinstance(mb, (int, float)) and mb > 0:
        return int(mb)
    gib = memory.get("unified_budget_gib")
    if isinstance(gib, (int, float)) and gib > 0:
        return int(gib * 1024)
    return None


class GpuArbiter:
    """Decides CUDA-vs-CPU placement against the unified-memory budget.

    Public API preserved from v1: ``can_allocate_onnx()``,
    ``can_train()``, ``get_onnx_providers()``, ``get_free_vram_mb()``,
    ``acquire_exclusive()`` — only the accounting source changed.
    """

    def __init__(
        self,
        budget_mb: int | None = None,
        yaml_path: str | Path | None = None,
        env: dict[str, str] | None = None,
        meminfo_reader: Callable[[], dict[str, int]] | None = None,
    ) -> None:
        self._env = env if env is not None else os.environ
        self._meminfo_reader = meminfo_reader or _read_meminfo
        self._yaml_path = Path(yaml_path) if yaml_path is not None else None
        self._gpu_available = self._detect_gpu()
        self._unified_budget_mb = self._resolve_budget(budget_mb)
        self._exclusive_lock = asyncio.Lock()
        logger.info(
            "gpu_arbiter.init",
            unified_budget_mb=self._unified_budget_mb,
            onnx_budget_mb=ONNX_INFERENCE_MB,
            cuda_safety_margin_mb=CUDA_SAFETY_MARGIN_MB,
            gpu_detected=self._gpu_available,
            source=self._budget_source,
        )

    # -- budget resolution -----------------------------------------------------

    _budget_source: str = "unknown"

    def _resolve_budget(self, explicit: int | None) -> int:
        if explicit is not None and explicit > 0:
            self._budget_source = "explicit"
            return int(explicit)
        env_budget = self._env.get("VEER_UNIFIED_BUDGET_MB")
        if env_budget:
            try:
                value = int(env_budget)
                if value > 0:
                    self._budget_source = "env"
                    return value
            except ValueError:
                logger.warning("gpu_arbiter.bad_env_budget", value=env_budget)
        for candidate in self._yaml_candidates():
            budget = _budget_from_yaml(candidate)
            if budget is not None:
                self._budget_source = f"yaml:{candidate}"
                return budget
        total = meminfo_memtotal_mb(self._meminfo_reader())
        if total is not None:
            self._budget_source = "proc_meminfo"
            return total
        self._budget_source = "default"
        return DEFAULT_UNIFIED_BUDGET_MB

    def _yaml_candidates(self) -> list[Path]:
        if self._yaml_path is not None:
            return [self._yaml_path]
        configured = self._env.get("VEER_MODELS_CONFIG")
        candidates = [Path(configured)] if configured else []
        candidates.extend(Path(p) for p in VEER_MODELS_CONFIG_CANDIDATES)
        return candidates

    @property
    def unified_budget_mb(self) -> int:
        """Total unified-memory budget this arbiter plans against (MiB)."""
        return self._unified_budget_mb

    @property
    def budget_source(self) -> str:
        """Which link of the resolution chain produced the budget."""
        return self._budget_source

    # -- GPU presence ------------------------------------------------------------

    def _detect_gpu(self) -> bool:
        """Check if nvidia-smi is available (presence only, not memory)."""
        try:
            subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
                capture_output=True,
                check=True,
                timeout=5,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return False

    # -- free memory ---------------------------------------------------------------

    def get_free_vram_mb(self) -> int:
        """Free unified memory (MiB) from /proc/meminfo MemAvailable.

        Name kept for v1 API compatibility.  On GB10 nvidia-smi memory
        counters read N/A, so /proc/meminfo is authoritative.
        """
        free = meminfo_memavailable_mb(self._meminfo_reader())
        if free is None:
            logger.error("gpu_arbiter.meminfo_unavailable")
            return 0
        return free

    # -- placement decisions ---------------------------------------------------------

    def can_allocate_onnx(self) -> bool:
        """CUDA EP allowed when free memory covers ONNX + safety margin."""
        free = self.get_free_vram_mb()
        can = free >= ONNX_INFERENCE_MB + CUDA_SAFETY_MARGIN_MB
        logger.debug(
            "gpu_arbiter.can_allocate_onnx",
            free_mb=free,
            required_mb=ONNX_INFERENCE_MB + CUDA_SAFETY_MARGIN_MB,
            allowed=can,
        )
        return can

    def can_train(self) -> bool:
        """GPU training window safe when free memory ≥ TRAIN_MIN_FREE_MB."""
        free = self.get_free_vram_mb()
        can = free >= TRAIN_MIN_FREE_MB
        logger.debug("gpu_arbiter.can_train", free_mb=free, allowed=can)
        return can

    def get_onnx_providers(self) -> list[str]:
        """ONNX Runtime execution providers based on free memory."""
        if self._gpu_available and self.can_allocate_onnx():
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        return ["CPUExecutionProvider"]

    @asynccontextmanager
    async def acquire_exclusive(self) -> AsyncIterator[None]:
        """Serialize heavy GPU workloads (training windows) in-process."""
        async with self._exclusive_lock:
            logger.info("gpu_arbiter.exclusive_acquired")
            try:
                yield
            finally:
                logger.info("gpu_arbiter.exclusive_released")


__all__ = [
    "CUDA_SAFETY_MARGIN_MB",
    "DEFAULT_UNIFIED_BUDGET_MB",
    "GPU_DB_CACHE_MB",
    "ONNX_INFERENCE_MB",
    "TRAIN_MIN_FREE_MB",
    "VEER_MODELS_CONFIG_CANDIDATES",
    "GpuArbiter",
    "meminfo_memavailable_mb",
    "meminfo_memtotal_mb",
]
