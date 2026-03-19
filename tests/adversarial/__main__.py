"""VVIP Adversarial Test Suite — CLI entry point.

Run adversarial tests against the live convoy-brain orchestrator.

Usage:
    python -m tests.adversarial                          # Standard run
    python -m tests.adversarial --stress                 # Include stress tests
    python -m tests.adversarial --continuous --duration 600  # 10-min sustained load
    python -m tests.adversarial --concurrency 5          # Higher parallelism
    python -m tests.adversarial --output report.json     # Save JSON report
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import structlog

from .config import AdversarialConfig
from .runner import AdversarialRunner
from .report import ReportGenerator


def main() -> None:
    """CLI entry point."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )

    parser = argparse.ArgumentParser(
        description="VVIP Adversarial Test Suite — Background Copilot Subagent"
    )
    parser.add_argument(
        "--convoy-brain-url",
        default="http://localhost:8080",
        help="convoy-brain base URL",
    )
    parser.add_argument(
        "--traffic-oracle-url",
        default="http://localhost:8081",
        help="traffic-oracle base URL",
    )
    parser.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
        help="Ollama API base URL",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Max concurrent scenarios (default: 3)",
    )
    parser.add_argument(
        "--stress",
        action="store_true",
        help="Include stress test scenarios",
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Run sustained continuous load",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=300.0,
        help="Duration for continuous mode in seconds (default: 300)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Per-request timeout in seconds (default: 120)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON report to file",
    )

    args = parser.parse_args()

    config = AdversarialConfig(
        convoy_brain_url=args.convoy_brain_url,
        traffic_oracle_url=args.traffic_oracle_url,
        ollama_url=args.ollama_url,
        concurrency=args.concurrency,
        timeout_per_request_sec=args.timeout,
        sustained_load_duration_sec=args.duration,
    )

    runner = AdversarialRunner(config)
    reporter = ReportGenerator()

    if args.continuous:
        report = asyncio.run(runner.run_continuous(duration_sec=args.duration))
    else:
        report = asyncio.run(runner.run_all(include_stress=args.stress))

    # Print text report
    print(reporter.to_text(report))

    # Save JSON if requested
    if args.output:
        output_path = Path(args.output)
        reporter.to_json(report, output_path)
        print(f"\nJSON report saved to: {output_path}")

    # Exit code based on pass rate
    if report.pass_rate < (1 - config.max_error_rate):
        print(f"\nFAILED: Pass rate {report.pass_rate:.0%} below threshold {1 - config.max_error_rate:.0%}")
        sys.exit(1)
    else:
        print(f"\nPASSED: {report.pass_rate:.0%} pass rate")
        sys.exit(0)


if __name__ == "__main__":
    main()
