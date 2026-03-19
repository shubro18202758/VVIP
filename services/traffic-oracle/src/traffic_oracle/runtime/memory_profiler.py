"""Memory profiling infrastructure for ML functions.

Provides:
    - @profile_memory: Decorator logging RSS, peak allocation, and duration
    - MemoryGuard: Context manager aborting if RSS exceeds threshold
"""

from __future__ import annotations

import asyncio
import functools
import time
import tracemalloc
from contextlib import contextmanager

import psutil
import structlog

logger = structlog.get_logger(__name__)

_process = psutil.Process()


def profile_memory(func):
    """Decorator that logs peak memory usage of the wrapped function.

    Logs via structlog:
        - RSS before/after (MB)
        - Peak tracemalloc allocation delta (MB)
        - Duration in milliseconds
        - Function qualified name
    """
    if asyncio.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            rss_before = _process.memory_info().rss
            tracemalloc.start()
            start = time.monotonic()
            try:
                return await func(*args, **kwargs)
            finally:
                duration_ms = (time.monotonic() - start) * 1000
                current, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                rss_after = _process.memory_info().rss
                logger.info(
                    "memory_profile",
                    function=func.__qualname__,
                    rss_before_mb=round(rss_before / 1_048_576, 1),
                    rss_after_mb=round(rss_after / 1_048_576, 1),
                    rss_delta_mb=round((rss_after - rss_before) / 1_048_576, 1),
                    peak_alloc_mb=round(peak / 1_048_576, 2),
                    duration_ms=round(duration_ms, 1),
                )

        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        rss_before = _process.memory_info().rss
        tracemalloc.start()
        start = time.monotonic()
        try:
            return func(*args, **kwargs)
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            rss_after = _process.memory_info().rss
            logger.info(
                "memory_profile",
                function=func.__qualname__,
                rss_before_mb=round(rss_before / 1_048_576, 1),
                rss_after_mb=round(rss_after / 1_048_576, 1),
                rss_delta_mb=round((rss_after - rss_before) / 1_048_576, 1),
                peak_alloc_mb=round(peak / 1_048_576, 2),
                duration_ms=round(duration_ms, 1),
            )

    return sync_wrapper


@contextmanager
def memory_guard(max_rss_mb: int = 3500):
    """Context manager that raises MemoryError if RSS exceeds threshold.

    Args:
        max_rss_mb: Maximum allowed RSS in megabytes (default 3500 of 4GB budget).
    """
    rss_before = _process.memory_info().rss // 1_048_576
    logger.debug("memory_guard.enter", rss_mb=rss_before, limit_mb=max_rss_mb)
    try:
        yield
    finally:
        rss_after = _process.memory_info().rss // 1_048_576
        if rss_after > max_rss_mb:
            logger.error(
                "memory_guard.exceeded",
                rss_mb=rss_after,
                limit_mb=max_rss_mb,
                delta_mb=rss_after - rss_before,
            )
            raise MemoryError(
                f"RSS {rss_after} MB exceeds {max_rss_mb} MB guard threshold"
            )
