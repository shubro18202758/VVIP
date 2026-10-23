"""Twin shadow-evaluation gate (C4 fail-closed rule).

Before any non-baseline parameter set may be published, it must be
shadow-evaluated against the digital twin: run the twin with the
baseline closure policy and with the candidate policy, compare total
public disruption.  A candidate passes only if it does not regress
disruption beyond the tolerance.

The twin interaction is an injected port (``TwinRun`` results are plain
data) — this module makes no HTTP calls and imports nothing from the
simulation package.
"""

from __future__ import annotations

from dataclasses import dataclass

from traffic_oracle.adaptive.enums import ShadowEvalStatus
from traffic_oracle.adaptive.policy import AdaptiveParams


@dataclass(frozen=True)
class TwinRun:
    """Aggregated result of one twin simulation (C5 response shape)."""

    total_disruption_vehicle_hours: float
    calibrated: bool
    is_stub: bool
    ambulance_delay_sec: float = 0.0
    convoy_transit_time_sec: float = 0.0
    #: Why the twin is (not) calibrated, in operator words (C5 additive
    #: ``calibration.reason``) — quoted in an uncalibrated refusal.
    calibration_reason: str | None = None
    #: Route segments the twin network actually simulated (None = the
    #: producer did not say).  Zero means the comparison is vacuous.
    route_segments_modelled: int | None = None

    @classmethod
    def from_payload(cls, payload: dict) -> TwinRun:
        """Build from the C5 ``POST /api/v1/simulate/twin`` response body.

        Accepts either the full ``{runs: [...], calibrated: bool}`` body
        (aggregating the mean over replications) or a single run dict.
        """
        calibration = payload.get("calibration")
        reason = (
            calibration.get("reason") if isinstance(calibration, dict) else None
        )
        coverage = payload.get("route_coverage")
        modelled = (
            int(coverage["modelled"])
            if isinstance(coverage, dict) and coverage.get("modelled") is not None
            else None
        )
        if "runs" in payload:
            runs = payload["runs"]
            if not runs:
                raise ValueError("twin payload has empty runs list")
            n = len(runs)
            return cls(
                total_disruption_vehicle_hours=sum(
                    float(r.get("total_disruption_vehicle_hours", 0.0)) for r in runs
                )
                / n,
                calibrated=bool(payload.get("calibrated", False)),
                is_stub=bool(payload.get("is_stub", False)),
                ambulance_delay_sec=sum(
                    float(r.get("ambulance_delay_sec", 0.0)) for r in runs
                )
                / n,
                convoy_transit_time_sec=sum(
                    float(r.get("convoy_transit_time_sec", 0.0)) for r in runs
                )
                / n,
                calibration_reason=str(reason) if reason else None,
                route_segments_modelled=modelled,
            )
        return cls(
            total_disruption_vehicle_hours=float(
                payload.get("total_disruption_vehicle_hours", 0.0)
            ),
            calibrated=bool(payload.get("calibrated", False)),
            is_stub=bool(payload.get("is_stub", False)),
            ambulance_delay_sec=float(payload.get("ambulance_delay_sec", 0.0)),
            convoy_transit_time_sec=float(
                payload.get("convoy_transit_time_sec", 0.0)
            ),
            calibration_reason=str(reason) if reason else None,
            route_segments_modelled=modelled,
        )


@dataclass(frozen=True)
class ShadowEvalOutcome:
    """Result of the gate, including the machine-checkable status."""

    status: ShadowEvalStatus
    detail: str
    baseline_run: TwinRun | None = None
    candidate_run: TwinRun | None = None

    @property
    def passed(self) -> bool:
        return self.status is ShadowEvalStatus.PASSED


class ShadowEvaluator:
    """Pure decision logic comparing candidate vs baseline twin runs.

    Tolerance semantics: the candidate may not increase mean total
    disruption by more than ``disruption_tolerance`` (relative) or
    ``abs_floor_vh`` vehicle-hours, whichever is larger; and it may not
    worsen ambulance delay by more than ``ambulance_tolerance_sec``.
    """

    def __init__(
        self,
        disruption_tolerance: float = 0.05,
        abs_floor_vh: float = 1.0,
        ambulance_tolerance_sec: float = 10.0,
    ) -> None:
        if disruption_tolerance < 0:
            raise ValueError("disruption_tolerance must be >= 0")
        self.disruption_tolerance = disruption_tolerance
        self.abs_floor_vh = abs_floor_vh
        self.ambulance_tolerance_sec = ambulance_tolerance_sec

    def evaluate(
        self,
        candidate_params: AdaptiveParams,
        baseline_run: TwinRun | None,
        candidate_run: TwinRun | None,
        twin_error: str | None = None,
    ) -> ShadowEvalOutcome:
        """Apply the gate to one (baseline, candidate) pair of twin runs.

        ``twin_error`` carries any exception/detail from the twin port;
        a twin that could not run yields UNAVAILABLE (fail-closed).
        """
        if twin_error is not None:
            return ShadowEvalOutcome(
                ShadowEvalStatus.UNAVAILABLE,
                f"twin_error:{twin_error}",
            )
        if baseline_run is None or candidate_run is None:
            return ShadowEvalOutcome(
                ShadowEvalStatus.UNAVAILABLE,
                "twin_runs_missing",
            )
        if baseline_run.is_stub or candidate_run.is_stub:
            return ShadowEvalOutcome(
                ShadowEvalStatus.UNAVAILABLE,
                "twin_is_stub",
            )
        if not baseline_run.calibrated or not candidate_run.calibrated:
            # The refusal names WHY (e.g. "no field observations — simulated
            # feed only"): a simulated feed never field-calibrates the twin.
            reason = baseline_run.calibration_reason or candidate_run.calibration_reason
            return ShadowEvalOutcome(
                ShadowEvalStatus.UNAVAILABLE,
                f"twin_not_calibrated: {reason}" if reason else "twin_not_calibrated",
            )
        if 0 in (
            baseline_run.route_segments_modelled,
            candidate_run.route_segments_modelled,
        ):
            # Both arms simulated nothing of the route: a 0 vh vs 0 vh
            # "no regression" would be a vacuous pass.
            return ShadowEvalOutcome(
                ShadowEvalStatus.UNAVAILABLE,
                "twin_route_not_modelled",
            )

        allowed = max(
            baseline_run.total_disruption_vehicle_hours * self.disruption_tolerance,
            self.abs_floor_vh,
        )
        disruption_delta = (
            candidate_run.total_disruption_vehicle_hours
            - baseline_run.total_disruption_vehicle_hours
        )
        if disruption_delta > allowed:
            return ShadowEvalOutcome(
                ShadowEvalStatus.FAILED,
                (
                    "disruption_regression:"
                    f"{disruption_delta:.3f}vh>allowed:{allowed:.3f}vh"
                ),
                baseline_run,
                candidate_run,
            )

        ambulance_delta = (
            candidate_run.ambulance_delay_sec - baseline_run.ambulance_delay_sec
        )
        if ambulance_delta > self.ambulance_tolerance_sec:
            return ShadowEvalOutcome(
                ShadowEvalStatus.FAILED,
                (
                    "ambulance_regression:"
                    f"{ambulance_delta:.1f}s>tolerance:{self.ambulance_tolerance_sec}s"
                ),
                baseline_run,
                candidate_run,
            )

        return ShadowEvalOutcome(
            ShadowEvalStatus.PASSED,
            (
                "no_regression:"
                f"disruption_delta={disruption_delta:+.3f}vh,"
                f"ambulance_delta={ambulance_delta:+.1f}s"
            ),
            baseline_run,
            candidate_run,
        )


__all__ = [
    "ShadowEvalOutcome",
    "ShadowEvaluator",
    "TwinRun",
]
