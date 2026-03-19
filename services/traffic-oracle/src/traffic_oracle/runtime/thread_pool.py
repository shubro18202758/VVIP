"""Bounded thread pool for all ML inference workloads.

Enforces the 4-6 thread limit across ONNX sessions, scikit-learn inference,
numpy operations, and RL agent forward passes. Also configures PyTorch and
numpy thread limits to prevent oversubscription.
"""

from __future__ import annotations

import concurrent.futures
import os

import structlog

logger = structlog.get_logger(__name__)

# Limit numpy/OpenBLAS/MKL threads before any import
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("MKL_NUM_THREADS", "4")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "4")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "4")


class BoundedMLThreadPool:
    """Singleton thread pool for all ML inference workloads.

    Max 4 worker threads. All CPU-bound ML work (ONNX, sklearn, ortools)
    should be submitted here to prevent total system starvation.
    """

    _instance: concurrent.futures.ThreadPoolExecutor | None = None
    _MAX_WORKERS = 4

    @classmethod
    def get(cls) -> concurrent.futures.ThreadPoolExecutor:
        """Return the singleton thread pool, creating it on first call."""
        if cls._instance is None:
            cls._instance = concurrent.futures.ThreadPoolExecutor(
                max_workers=cls._MAX_WORKERS,
                thread_name_prefix="ml-inference",
            )
            cls._configure_torch_threads()
            logger.info(
                "ml_thread_pool.init",
                max_workers=cls._MAX_WORKERS,
            )
        return cls._instance

    @classmethod
    def shutdown(cls) -> None:
        """Shut down the thread pool, waiting for pending work."""
        if cls._instance is not None:
            cls._instance.shutdown(wait=True)
            cls._instance = None
            logger.info("ml_thread_pool.shutdown")

    @classmethod
    def _configure_torch_threads(cls) -> None:
        """Set PyTorch thread limits if torch is available."""
        try:
            import torch

            torch.set_num_threads(cls._MAX_WORKERS)
            torch.set_num_interop_threads(2)
            logger.info(
                "torch_threads.configured",
                intra=torch.get_num_threads(),
                inter=torch.get_num_interop_threads(),
            )
        except ImportError:
            pass
