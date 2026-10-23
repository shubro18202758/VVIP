"""GPU Arbiter — memory-budget manager for the unified-memory pool.

v2 (adaptive stream): the retired v1 accounting assumed a discrete
8 GB GPU with Ollama/Qwen resident.  The deployment target is a
GB10-class unified-memory machine (~121.4 GiB, MemTotal 127,533,976 kB
per /proc/meminfo — nvidia-smi reports memory.total/used/free as N/A
there, so /proc/meminfo is the authoritative source).

Budget resolution order (first hit wins):

    1. explicit ``budget_mb`` constructor argument (tests / overrides)
    2. ``VEER_UNIFIED_BUDGET_MB`` environment variable
    3. the models-owned ``veer_models.yaml``: ``memory.unified_budget_mb``
       (or ``_gib``), else the planned pool ``memory_budget_gib.total_pool``
       the models stream maintains there
    4. ``MemTotal`` from /proc/meminfo
    5. conservative default 121_440 MiB (GB10 hardware facts)

Where ``veer_models.yaml`` is found (:func:`resolve_models_config` — ONE
file is in force, the first that exists; :func:`models_config_candidates`
lists them in order):

    a. ``VEER_MODELS_CONFIG`` (the path serve_models.sh / veer_env.sh
       export — an explicit pointer always comes first);
    b. ``models-config/configs/veer_models.yaml`` — the TRACKED home
       (WP-08) — under the VVIP root this module lives in, under the
       working directory's ``VVIP/`` or the working directory itself, then
       ``/opt/veer``;
    c. the same roots' legacy ``models/configs/veer_models.yaml`` — inside
       the retired-tree ``VVIP/models`` symlink — consulted only after
       every tracked candidate, so the symlink can be deleted without
       changing what is read.

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
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable

import structlog

logger = structlog.get_logger(__name__)

# Conservative fallback (GB10 measured pool ≈ 121.4 GiB).
DEFAULT_UNIFIED_BUDGET_MB = 121_440

#: Explicit pointer to the models-owned config (exported by
#: infra/scripts/serve_models.sh and scripts/veer_env.sh).
MODELS_CONFIG_ENV = "VEER_MODELS_CONFIG"

#: Root-relative locations of veer_models.yaml, in priority order: the
#: tracked models-config home first, the retired-tree symlink path last.
MODELS_CONFIG_RELPATH = Path("models-config/configs/veer_models.yaml")
LEGACY_MODELS_CONFIG_RELPATH = Path("models/configs/veer_models.yaml")
VEER_MODELS_CONFIG_CANDIDATES: tuple[str, ...] = (
    str(MODELS_CONFIG_RELPATH),
    str(LEGACY_MODELS_CONFIG_RELPATH),
)


def _vvip_root() -> Path | None:
    """The VVIP root this module lives under (repo layout and the gb10
    image's mirrored /repo/VVIP tree); None in a flatter install, where
    the working-directory and /opt roots still apply."""
    parents = Path(__file__).resolve().parents
    # runtime -> traffic_oracle -> src -> traffic-oracle -> services -> VVIP
    return parents[5] if len(parents) > 5 else None


def config_roots(cwd: Path | None = None) -> tuple[Path, ...]:
    """Roots searched for the root-relative candidates, in order."""
    here = Path.cwd() if cwd is None else cwd
    roots: list[Path] = []
    for root in (_vvip_root(), here / "VVIP", here, Path("/opt/veer")):
        if root is not None and root not in roots:
            roots.append(root)
    return tuple(roots)


def models_config_candidates(
    env: Mapping[str, str] | None = None, roots: Sequence[Path] | None = None
) -> list[Path]:
    """Every place veer_models.yaml may live, in resolution order:
    ``VEER_MODELS_CONFIG``, then the tracked home under each root, then
    the legacy symlink path under each root."""
    env = os.environ if env is None else env
    search = config_roots() if roots is None else tuple(roots)
    candidates: list[Path] = []
    configured = (env.get(MODELS_CONFIG_ENV) or "").strip()
    if configured:
        candidates.append(Path(configured))
    for rel in (MODELS_CONFIG_RELPATH, LEGACY_MODELS_CONFIG_RELPATH):
        for root in search:
            path = root / rel
            if path not in candidates:
                candidates.append(path)
    return candidates


def resolve_models_config(
    env: Mapping[str, str] | None = None, roots: Sequence[Path] | None = None
) -> Path | None:
    """The ONE veer_models.yaml in force (first existing candidate), or None.

    A ``VEER_MODELS_CONFIG`` that names no file is reported (logged) and
    the tracked locations are searched next; a hit on the legacy symlink
    path is logged too, so its remaining readers are visible before the
    symlink is deleted.
    """
    env = os.environ if env is None else env
    search = config_roots() if roots is None else tuple(roots)
    configured = (env.get(MODELS_CONFIG_ENV) or "").strip()
    if configured:
        path = Path(configured)
        if path.is_file():
            return path
        logger.warning("gpu_arbiter.models_config_missing", path=configured)
    for rel in (MODELS_CONFIG_RELPATH, LEGACY_MODELS_CONFIG_RELPATH):
        for root in search:
            path = root / rel
            if path.is_file():
                if rel == LEGACY_MODELS_CONFIG_RELPATH:
                    logger.info("gpu_arbiter.models_config_legacy_path", path=str(path))
                return path
    return None


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


def _positive(value: object) -> float | None:
    """A positive finite number from YAML (bools are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 < float(value) < float("inf") else None


def _budget_from_yaml(path: Path) -> int | None:
    """The unified budget (MiB) veer_models.yaml declares, or None.

    ``memory.unified_budget_mb`` (or ``_gib``, also accepted top-level),
    else ``memory_budget_gib.total_pool`` — the planned GB10 pool the
    models stream keeps in its budget table (MEMORY_BUDGET.md).
    """
    try:
        import yaml  # lazy: dependency of traffic-oracle (pyproject)
    except ImportError:  # pragma: no cover - pyproject declares pyyaml
        return None
    try:
        with open(path) as fh:
            doc = yaml.safe_load(fh)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # a broken config must never take the oracle down at startup: the
        # budget chain moves on (and the breakage is visible in the log)
        logger.warning(
            "gpu_arbiter.models_config_unreadable",
            path=str(path),
            error=type(exc).__name__,
        )
        return None
    if not isinstance(doc, dict):
        return None
    memory = doc.get("memory", doc)
    if isinstance(memory, dict):
        mb = _positive(memory.get("unified_budget_mb"))
        if mb is not None:
            return int(mb)
        gib = _positive(memory.get("unified_budget_gib"))
        if gib is not None:
            return int(gib * 1024)
    table = doc.get("memory_budget_gib")
    if isinstance(table, dict):
        pool_gib = _positive(table.get("total_pool"))
        if pool_gib is not None:
            return int(pool_gib * 1024)
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
        models_config_roots: Sequence[Path] | None = None,
    ) -> None:
        self._env = env if env is not None else os.environ
        self._meminfo_reader = meminfo_reader or _read_meminfo
        self._yaml_path = Path(yaml_path) if yaml_path is not None else None
        # roots for the root-relative config candidates (tests); None ⇒
        # config_roots(): this tree's VVIP root, the CWD, /opt/veer
        self._config_roots = (
            tuple(models_config_roots) if models_config_roots is not None else None
        )
        self._models_config: Path | None = None
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
            models_config=str(self._models_config) if self._models_config else None,
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
        # ONE config file is in force (VEER_MODELS_CONFIG, else the tracked
        # models-config home, else the legacy symlink path); when it
        # declares no budget the chain moves on to /proc/meminfo — a stale
        # legacy copy is never consulted behind the tracked file's back.
        self._models_config = self._resolve_models_config()
        if self._models_config is not None:
            budget = _budget_from_yaml(self._models_config)
            if budget is not None:
                self._budget_source = f"yaml:{self._models_config}"
                return budget
        total = meminfo_memtotal_mb(self._meminfo_reader())
        if total is not None:
            self._budget_source = "proc_meminfo"
            return total
        self._budget_source = "default"
        return DEFAULT_UNIFIED_BUDGET_MB

    def _resolve_models_config(self) -> Path | None:
        if self._yaml_path is not None:
            return self._yaml_path if self._yaml_path.is_file() else None
        return resolve_models_config(self._env, self._config_roots)

    @property
    def unified_budget_mb(self) -> int:
        """Total unified-memory budget this arbiter plans against (MiB)."""
        return self._unified_budget_mb

    @property
    def budget_source(self) -> str:
        """Which link of the resolution chain produced the budget."""
        return self._budget_source

    @property
    def models_config_path(self) -> Path | None:
        """The veer_models.yaml in force (None when none was found)."""
        return self._models_config

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
    "LEGACY_MODELS_CONFIG_RELPATH",
    "MODELS_CONFIG_ENV",
    "MODELS_CONFIG_RELPATH",
    "ONNX_INFERENCE_MB",
    "TRAIN_MIN_FREE_MB",
    "VEER_MODELS_CONFIG_CANDIDATES",
    "GpuArbiter",
    "config_roots",
    "meminfo_memavailable_mb",
    "meminfo_memtotal_mb",
    "models_config_candidates",
    "resolve_models_config",
]
