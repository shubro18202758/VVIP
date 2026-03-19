"""VVIP Adversarial Test Suite — Main runner.

Orchestrates adversarial scenario execution against the convoy-brain orchestrator.
Acts as a background Copilot subagent that continuously simulates complex VVIP
routing queries and edge-case traffic scenarios.

Usage:
    python -m tests.adversarial.runner [--concurrency 3] [--scenarios 50] [--stress]
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import httpx
import structlog

from .config import (
    AdversarialConfig,
    ROUTING_SCENARIOS,
    STRESS_SCENARIOS,
)
from .evaluator import EvaluationResult, ResponseEvaluator
from .report import ReportGenerator, TestReport
from .stability import StabilityMonitor

logger = structlog.get_logger(__name__)


@dataclass
class ScenarioResult:
    """Raw result from executing a scenario against convoy-brain."""

    scenario_id: str
    response_text: str
    http_status: int
    latency_sec: float
    tool_calls: list[dict]
    error: str | None = None


class AdversarialRunner:
    """Executes adversarial scenarios against the convoy-brain orchestrator.

    This is the "secondary, background Copilot subagent" that acts as an
    adversarial user, continuously simulating complex VVIP routing queries.
    """

    def __init__(self, config: AdversarialConfig | None = None) -> None:
        self.config = config or AdversarialConfig()
        self.evaluator = ResponseEvaluator(
            max_latency_sec=self.config.max_acceptable_latency_sec,
            min_reasoning_depth=self.config.min_reasoning_depth_score,
            max_tool_rounds=self.config.max_tool_rounds_before_loop,
        )
        self.reporter = ReportGenerator()
        self.monitor = StabilityMonitor(
            convoy_brain_url=self.config.convoy_brain_url,
            traffic_oracle_url=self.config.traffic_oracle_url,
            ollama_url=self.config.ollama_url,
        )

    async def run_all(self, include_stress: bool = False) -> TestReport:
        """Run all adversarial scenarios and generate report."""
        logger.info(
            "adversarial.run_all.start",
            total_scenarios=len(ROUTING_SCENARIOS),
            concurrency=self.config.concurrency,
            include_stress=include_stress,
        )

        # Start stability monitor
        await self.monitor.start()

        try:
            # Run routing/edge-case/adversarial scenarios
            results = await self._run_scenario_batch(ROUTING_SCENARIOS)

            # Run stress tests if requested
            if include_stress:
                stress_results = await self._run_stress_tests()
                results.extend(stress_results)

        finally:
            stability_report = await self.monitor.stop()

        # Generate report
        report = self.reporter.generate(results)

        # Inject stability data into report
        report.score_summary["system_stability"] = (
            stability_report.ollama_uptime_pct / 100
        )
        if stability_report.max_vram_used_mb > 0:
            vram_usage_pct = stability_report.max_vram_used_mb / 8192
            report.score_summary["vram_safety"] = max(0, 1.0 - max(0, vram_usage_pct - 0.8) * 5)

        if stability_report.max_gpu_temp > 85:
            report.critical_issues.append(
                f"GPU temperature peaked at {stability_report.max_gpu_temp}°C "
                f"(throttle threshold: 85°C)"
            )

        return report

    async def _run_scenario_batch(
        self, scenarios: list[dict]
    ) -> list[EvaluationResult]:
        """Run scenarios with bounded concurrency."""
        semaphore = asyncio.Semaphore(self.config.concurrency)
        results: list[EvaluationResult] = []

        async def run_one(scenario: dict) -> EvaluationResult:
            async with semaphore:
                raw = await self._execute_scenario(scenario)
                result = self.evaluator.evaluate(
                    scenario=scenario,
                    response_text=raw.response_text,
                    latency_sec=raw.latency_sec,
                    http_status=raw.http_status,
                    tool_calls=raw.tool_calls,
                )
                status = "PASS" if result.passed else "FAIL"
                logger.info(
                    "adversarial.scenario.done",
                    scenario_id=scenario["id"],
                    status=status,
                    latency=f"{raw.latency_sec:.1f}s",
                    tools=result.tool_calls_observed,
                    issues=result.issues,
                )
                # Cooldown between requests to avoid overwhelming Ollama
                await asyncio.sleep(self.config.cooldown_between_requests_sec)
                return result

        tasks = [run_one(s) for s in scenarios]
        completed = await asyncio.gather(*tasks, return_exceptions=True)

        for i, res in enumerate(completed):
            if isinstance(res, Exception):
                logger.error(
                    "adversarial.scenario.exception",
                    scenario_id=scenarios[i]["id"],
                    error=str(res),
                )
                results.append(
                    EvaluationResult(
                        scenario_id=scenarios[i]["id"],
                        passed=False,
                        latency_sec=0.0,
                        http_status=0,
                        issues=[f"Exception: {res}"],
                    )
                )
            else:
                results.append(res)

        return results

    async def _execute_scenario(self, scenario: dict) -> ScenarioResult:
        """Execute a single scenario against convoy-brain /chat endpoint."""
        payload = {
            "message": scenario["prompt"],
            "conversation_id": f"adversarial-{scenario['id']}",
        }

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_per_request_sec
            ) as client:
                resp = await client.post(
                    f"{self.config.convoy_brain_url}/chat",
                    json=payload,
                )
                latency = time.monotonic() - start

                # Parse response
                try:
                    data = resp.json()
                    response_text = data.get("response", data.get("message", str(data)))
                    tool_calls = data.get("tool_calls", [])
                except (json.JSONDecodeError, ValueError):
                    response_text = resp.text
                    tool_calls = []

                return ScenarioResult(
                    scenario_id=scenario["id"],
                    response_text=response_text,
                    http_status=resp.status_code,
                    latency_sec=latency,
                    tool_calls=tool_calls,
                )

        except httpx.TimeoutException:
            return ScenarioResult(
                scenario_id=scenario["id"],
                response_text="",
                http_status=504,
                latency_sec=time.monotonic() - start,
                tool_calls=[],
                error="Request timed out",
            )
        except httpx.HTTPError as e:
            return ScenarioResult(
                scenario_id=scenario["id"],
                response_text="",
                http_status=0,
                latency_sec=time.monotonic() - start,
                tool_calls=[],
                error=str(e),
            )

    async def _run_stress_tests(self) -> list[EvaluationResult]:
        """Run sustained load stress scenarios."""
        results: list[EvaluationResult] = []

        for stress in STRESS_SCENARIOS:
            logger.info(
                "adversarial.stress.start",
                stress_id=stress["id"],
                num_prompts=len(stress["prompts"]),
            )

            # Fire all prompts concurrently within each stress scenario
            tasks = []
            for i, prompt in enumerate(stress["prompts"]):
                scenario = {
                    "id": f"{stress['id']}-{i:03d}",
                    "category": "stress",
                    "difficulty": "medium",
                    "prompt": prompt,
                    "expected_tools": [],
                    "expected_keywords": [],
                    "description": stress["description"],
                }
                tasks.append(self._execute_scenario(scenario))

            raw_results = await asyncio.gather(*tasks, return_exceptions=True)

            for i, raw in enumerate(raw_results):
                scenario_id = f"{stress['id']}-{i:03d}"
                if isinstance(raw, Exception):
                    results.append(
                        EvaluationResult(
                            scenario_id=scenario_id,
                            passed=False,
                            latency_sec=0.0,
                            http_status=0,
                            issues=[f"Stress exception: {raw}"],
                        )
                    )
                else:
                    result = self.evaluator.evaluate(
                        scenario={
                            "id": scenario_id,
                            "category": "stress",
                            "difficulty": "medium",
                            "expected_tools": [],
                            "expected_keywords": [],
                        },
                        response_text=raw.response_text,
                        latency_sec=raw.latency_sec,
                        http_status=raw.http_status,
                        tool_calls=raw.tool_calls,
                    )
                    results.append(result)

        return results

    async def run_continuous(self, duration_sec: float = 300.0) -> TestReport:
        """Run adversarial scenarios continuously for a fixed duration.

        This is the sustained-load mode: the subagent keeps firing queries
        until the time budget is exhausted.
        """
        logger.info("adversarial.continuous.start", duration_sec=duration_sec)

        await self.monitor.start()
        results: list[EvaluationResult] = []
        start = time.monotonic()

        try:
            scenario_idx = 0
            while time.monotonic() - start < duration_sec:
                scenario = ROUTING_SCENARIOS[scenario_idx % len(ROUTING_SCENARIOS)]
                raw = await self._execute_scenario(scenario)
                result = self.evaluator.evaluate(
                    scenario=scenario,
                    response_text=raw.response_text,
                    latency_sec=raw.latency_sec,
                    http_status=raw.http_status,
                    tool_calls=raw.tool_calls,
                )
                results.append(result)
                scenario_idx += 1

                logger.info(
                    "adversarial.continuous.iteration",
                    iteration=scenario_idx,
                    elapsed=f"{time.monotonic() - start:.0f}s",
                    scenario=scenario["id"],
                    passed=result.passed,
                )

                await asyncio.sleep(self.config.cooldown_between_requests_sec)
        finally:
            await self.monitor.stop()

        return self.reporter.generate(results)
