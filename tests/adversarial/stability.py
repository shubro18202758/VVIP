"""VVIP Adversarial Test Suite — Stability monitor.

Runs alongside the adversarial tests, continuously sampling system health:
- Unified-memory usage (GB10: /proc/meminfo is the source of truth)
- API endpoint health (convoy-brain, traffic-oracle)
- Model-host responsiveness (GET /decider/health on :8083 — the strategic
  tier; the retired /api/tags poll on the old :11434 host is gone)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx
import structlog

logger = structlog.get_logger(__name__)

#: GB10 unified-memory pool (121.44 GiB from /proc/meminfo MemTotal
#: 127,533,976 kB). The old 8192 MiB figure was RTX-4070-era fiction.
UNIFIED_POOL_MIB = 124352


def _read_unified_mem_mib() -> tuple[int, int]:
    """(total_mib, used_mib) from /proc/meminfo; (0, 0) when unavailable."""
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                key, _, value = line.partition(":")
                info[key.strip()] = int(value.strip().split()[0]) // 1024
        total = info.get("MemTotal", 0)
        used = total - info.get("MemAvailable", 0) if total else 0
        return total, used
    except (OSError, ValueError):
        return 0, 0


@dataclass
class HealthSample:
    """Single health check sample."""

    timestamp: float
    model_host_up: bool
    convoy_brain_up: bool
    traffic_oracle_up: bool
    gpu_vram_used_mb: int = 0
    gpu_vram_total_mb: int = 0
    gpu_temp_celsius: int = 0


@dataclass
class StabilityReport:
    """Stability metrics collected across the test run."""

    samples: list[HealthSample] = field(default_factory=list)
    model_host_downtime_sec: float = 0.0
    convoy_brain_downtime_sec: float = 0.0
    traffic_oracle_downtime_sec: float = 0.0
    max_vram_used_mb: int = 0
    max_gpu_temp: int = 0
    total_duration_sec: float = 0.0

    @property
    def model_host_uptime_pct(self) -> float:
        if not self.total_duration_sec:
            return 0.0
        return max(0, 1 - self.model_host_downtime_sec / self.total_duration_sec) * 100

    @property
    def vram_headroom_mb(self) -> int:
        if not self.samples:
            return 0
        last = self.samples[-1]
        return last.gpu_vram_total_mb - last.gpu_vram_used_mb


class StabilityMonitor:
    """Background health monitor for system stability during adversarial tests."""

    def __init__(
        self,
        convoy_brain_url: str = "http://localhost:8080",
        traffic_oracle_url: str = "http://localhost:8081",
        model_host_url: str = "http://localhost:8083",
        sample_interval_sec: float = 5.0,
    ) -> None:
        self.convoy_brain_url = convoy_brain_url
        self.traffic_oracle_url = traffic_oracle_url
        self.model_host_url = model_host_url
        self.sample_interval_sec = sample_interval_sec
        self._report = StabilityReport()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Start background monitoring."""
        self._stop_event.clear()
        self._report = StabilityReport()
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("stability_monitor.started", interval=self.sample_interval_sec)

    async def stop(self) -> StabilityReport:
        """Stop monitoring and return the report."""
        self._stop_event.set()
        if self._task:
            await self._task
        logger.info(
            "stability_monitor.stopped",
            samples=len(self._report.samples),
            model_host_uptime_pct=f"{self._report.model_host_uptime_pct:.1f}%",
            max_vram_mb=self._report.max_vram_used_mb,
        )
        return self._report

    async def _monitor_loop(self) -> None:
        """Continuous health sampling loop."""
        start_time = time.monotonic()
        last_sample: HealthSample | None = None

        while not self._stop_event.is_set():
            sample = await self._take_sample()
            self._report.samples.append(sample)

            # Track downtimes
            if last_sample is not None:
                delta = self.sample_interval_sec
                if not sample.model_host_up:
                    self._report.model_host_downtime_sec += delta
                if not sample.convoy_brain_up:
                    self._report.convoy_brain_downtime_sec += delta
                if not sample.traffic_oracle_up:
                    self._report.traffic_oracle_downtime_sec += delta

            # Track peaks
            if sample.gpu_vram_used_mb > self._report.max_vram_used_mb:
                self._report.max_vram_used_mb = sample.gpu_vram_used_mb
            if sample.gpu_temp_celsius > self._report.max_gpu_temp:
                self._report.max_gpu_temp = sample.gpu_temp_celsius

            last_sample = sample
            self._report.total_duration_sec = time.monotonic() - start_time

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.sample_interval_sec
                )
                break  # stop_event was set
            except asyncio.TimeoutError:
                continue

    async def _take_sample(self) -> HealthSample:
        """Take a single health sample."""
        sample = HealthSample(
            timestamp=time.time(),
            model_host_up=False,
            convoy_brain_up=False,
            traffic_oracle_up=False,
        )

        # Unified memory straight from the kernel (nvidia-smi reports N/A on GB10).
        total_mib, used_mib = _read_unified_mem_mib()
        if total_mib:
            sample.gpu_vram_total_mb = total_mib
            sample.gpu_vram_used_mb = used_mib

        async with httpx.AsyncClient(timeout=5.0) as client:
            # Check the strategic tier (model-host decider)
            try:
                resp = await client.get(self.model_host_url + "/decider/health")
                sample.model_host_up = resp.status_code == 200
            except httpx.HTTPError:
                pass

            # Check convoy-brain
            try:
                resp = await client.get(self.convoy_brain_url + "/health")
                sample.convoy_brain_up = resp.status_code == 200
            except httpx.HTTPError:
                pass

            # Check traffic-oracle
            try:
                resp = await client.get(self.traffic_oracle_url + "/health")
                sample.traffic_oracle_up = resp.status_code == 200
            except httpx.HTTPError:
                pass

            # Optional nvidia-exporter temperature (when present)
            try:
                resp = await client.get("http://localhost:9835/metrics")
                if resp.status_code == 200:
                    for line in resp.text.split("\n"):
                        if line.startswith("nvidia_gpu_temperature_celsius"):
                            sample.gpu_temp_celsius = int(float(line.split()[-1]))
                        elif (
                            not total_mib
                            and line.startswith("nvidia_gpu_memory_used_bytes")
                        ):
                            sample.gpu_vram_used_mb = int(float(line.split()[-1]) / 1048576)
                        elif (
                            not total_mib
                            and line.startswith("nvidia_gpu_memory_total_bytes")
                        ):
                            sample.gpu_vram_total_mb = int(float(line.split()[-1]) / 1048576)
            except httpx.HTTPError:
                pass

        return sample
