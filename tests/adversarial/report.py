"""VVIP Adversarial Test Suite — Report generator.

Produces structured test reports in JSON and human-readable format.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from .evaluator import EvaluationResult


@dataclass
class TestReport:
    """Aggregate report for an adversarial test run."""

    timestamp: str = ""
    total_scenarios: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    pass_rate: float = 0.0
    avg_latency_sec: float = 0.0
    p95_latency_sec: float = 0.0
    p99_latency_sec: float = 0.0
    max_latency_sec: float = 0.0
    category_breakdown: dict[str, dict] = field(default_factory=dict)
    score_summary: dict[str, float] = field(default_factory=dict)
    critical_issues: list[str] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)


class ReportGenerator:
    """Generates test reports from evaluation results."""

    def generate(self, results: list[EvaluationResult]) -> TestReport:
        """Generate an aggregate report from evaluation results."""
        report = TestReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
            total_scenarios=len(results),
        )

        if not results:
            return report

        # Pass / fail / error counts
        for r in results:
            if r.http_status >= 500:
                report.errors += 1
            elif r.passed:
                report.passed += 1
            else:
                report.failed += 1

        report.pass_rate = report.passed / report.total_scenarios

        # Latency statistics
        latencies = [r.latency_sec for r in results]
        report.avg_latency_sec = round(statistics.mean(latencies), 2)
        report.max_latency_sec = round(max(latencies), 2)
        if len(latencies) >= 2:
            sorted_lat = sorted(latencies)
            idx_95 = int(len(sorted_lat) * 0.95)
            idx_99 = int(len(sorted_lat) * 0.99)
            report.p95_latency_sec = round(sorted_lat[min(idx_95, len(sorted_lat) - 1)], 2)
            report.p99_latency_sec = round(sorted_lat[min(idx_99, len(sorted_lat) - 1)], 2)

        # Score summary (average per metric across all results)
        all_score_keys: set[str] = set()
        for r in results:
            all_score_keys.update(r.scores.keys())
        for key in sorted(all_score_keys):
            vals = [r.scores[key] for r in results if key in r.scores]
            if vals:
                report.score_summary[key] = round(statistics.mean(vals), 3)

        # Category breakdown
        categories: dict[str, list[EvaluationResult]] = {}
        for r in results:
            # Extract category from scenario_id prefix
            cat = r.scenario_id.split("-")[0] if "-" in r.scenario_id else "unknown"
            categories.setdefault(cat, []).append(r)

        for cat, cat_results in categories.items():
            cat_latencies = [r.latency_sec for r in cat_results]
            report.category_breakdown[cat] = {
                "total": len(cat_results),
                "passed": sum(1 for r in cat_results if r.passed),
                "failed": sum(1 for r in cat_results if not r.passed),
                "avg_latency_sec": round(statistics.mean(cat_latencies), 2),
            }

        # Critical issues (appearing in 2+ results)
        issue_counts: dict[str, int] = {}
        for r in results:
            for issue in r.issues:
                # Normalize issue text for dedup
                normalized = issue.split(":")[0] if ":" in issue else issue
                issue_counts[normalized] = issue_counts.get(normalized, 0) + 1

        report.critical_issues = [
            f"{issue} (occurred {count}x)"
            for issue, count in sorted(issue_counts.items(), key=lambda x: -x[1])
            if count >= 2
        ]

        # Individual results (for JSON export)
        report.results = [
            {
                "scenario_id": r.scenario_id,
                "passed": r.passed,
                "latency_sec": round(r.latency_sec, 2),
                "http_status": r.http_status,
                "scores": {k: round(v, 3) for k, v in r.scores.items()},
                "tools_observed": r.tool_calls_observed,
                "issues": r.issues,
            }
            for r in results
        ]

        return report

    def to_json(self, report: TestReport, output_path: Path | None = None) -> str:
        """Serialize report to JSON."""
        data = asdict(report)
        json_str = json.dumps(data, indent=2, default=str)
        if output_path:
            output_path.write_text(json_str)
        return json_str

    def to_text(self, report: TestReport) -> str:
        """Generate human-readable text report."""
        lines = [
            "=" * 70,
            "VVIP ADVERSARIAL TEST REPORT",
            f"Timestamp: {report.timestamp}",
            "=" * 70,
            "",
            f"Total Scenarios: {report.total_scenarios}",
            f"Passed:          {report.passed}  ({report.pass_rate:.0%})",
            f"Failed:          {report.failed}",
            f"Errors (5xx):    {report.errors}",
            "",
            "─── Latency ───────────────────────────────────────────────",
            f"  Average:   {report.avg_latency_sec:.2f}s",
            f"  p95:       {report.p95_latency_sec:.2f}s",
            f"  p99:       {report.p99_latency_sec:.2f}s",
            f"  Max:       {report.max_latency_sec:.2f}s",
            "",
            "─── Score Summary ─────────────────────────────────────────",
        ]

        for metric, score in report.score_summary.items():
            bar = "█" * int(score * 20) + "░" * (20 - int(score * 20))
            lines.append(f"  {metric:<25} {bar} {score:.3f}")

        lines.append("")
        lines.append("─── Category Breakdown ────────────────────────────────────")
        for cat, data in report.category_breakdown.items():
            lines.append(
                f"  {cat:<15} "
                f"pass={data['passed']}/{data['total']}  "
                f"avg_lat={data['avg_latency_sec']:.1f}s"
            )

        if report.critical_issues:
            lines.append("")
            lines.append("─── Critical Issues ───────────────────────────────────────")
            for issue in report.critical_issues:
                lines.append(f"  ⚠ {issue}")

        lines.append("")
        lines.append("─── Per-Scenario Results ──────────────────────────────────")
        for r in report.results:
            status = "PASS" if r["passed"] else "FAIL"
            lines.append(
                f"  [{status:4}] {r['scenario_id']:<30} "
                f"{r['latency_sec']:>6.1f}s  "
                f"tools={r['tools_observed']}"
            )
            for issue in r["issues"]:
                lines.append(f"         └─ {issue}")

        lines.append("")
        lines.append("=" * 70)
        return "\n".join(lines)
