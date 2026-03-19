"""VVIP Adversarial Test Suite — Stability monitor.

Runs alongside the adversarial tests, continuously sampling system health:
- GPU VRAM usage
- Container memory/CPU
- API endpoint health
- Ollama responsiveness
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class HealthSample:
    """Single health check sample."""

    timestamp: float
    ollama_up: bool
    convoy_brain_up: bool
    traffic_oracle_up: bool
    gpu_vram_used_mb: int = 0
    gpu_vram_total_mb: int = 0
    gpu_temp_celsius: int = 0


@dataclass
class StabilityReport:
    """Stability metrics collected across the test run."""

    samples: list[HealthSample] = field(default_factory=list)
    ollama_downtime_sec: float = 0.0
    convoy_brain_downtime_sec: float = 0.0
    traffic_oracle_downtime_sec: float = 0.0
    max_vram_used_mb: int = 0
    max_gpu_temp: int = 0
    total_duration_sec: float = 0.0

    @property
    def ollama_uptime_pct(self) -> float:
        if not self.total_duration_sec:
            return 0.0
        return max(0, 1 - self.ollama_downtime_sec / self.total_duration_sec) * 100

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
        ollama_url: str = "http://localhost:11434",
        sample_interval_sec: float = 5.0,
    ) -> None:
        self.convoy_brain_url = convoy_brain_url
        self.traffic_oracle_url = traffic_oracle_url
        self.ollama_url = ollama_url
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
            ollama_uptime_pct=f"{self._report.ollama_uptime_pct:.1f}%",
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
                if not sample.ollama_up:
                    self._report.ollama_downtime_sec += delta
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
            ollama_up=False,
            convoy_brain_up=False,
            traffic_oracle_up=False,
        )

        async with httpx.AsyncClient(timeout=5.0) as client:
            # Check Ollama
            try:
                resp = await client.get(f"{self.ollama_url}/api/tags")
                sample.ollama_up = resp.status_code == 200
            except httpx.HTTPError:
                pass

            # Check convoy-brain
            try:
                resp = await client.get(f"{self.convoy_brain_url}/health")
                sample.convoy_brain_up = resp.status_code == 200
            except httpx.HTTPError:
                pass

            # Check traffic-oracle
            try:
                resp = await client.get(f"{self.traffic_oracle_url}/health")
                sample.traffic_oracle_up = resp.status_code == 200
            except httpx.HTTPError:
                pass

            # Check GPU via nvidia-exporter (if available)
            try:
                resp = await client.get("http://localhost:9835/metrics")
                if resp.status_code == 200:
                    text = resp.text
                    for line in text.split("\n"):
                        if line.startswith("nvidia_gpu_memory_used_bytes"):
                            val = float(line.split()[-1])
                            sample.gpu_vram_used_mb = int(val / 1048576)
                        elif line.startswith("nvidia_gpu_memory_total_bytes"):
                            val = float(line.split()[-1])
                            sample.gpu_vram_total_mb = int(val / 1048576)
                        elif line.startswith("nvidia_gpu_temperature_celsius"):
                            sample.gpu_temp_celsius = int(float(line.split()[-1]))
            except httpx.HTTPError:
                pass

        return sample
