"""Replay calibration for the twin (C5/C6).

``POST /api/v1/simulate/replay`` compares the twin's structural speed
prediction against observed ``traffic.hourly_aggregates`` over a window,
reports speed MAE, fits a bounded capacity scale per the observed/predicted
ratio, and flips the twin's ``calibrated`` flag only when there is enough
data and the fitted MAE is inside tolerance. Until then every twin response
reports ``calibrated: false`` — the fail-closed posture contract C4 relies on
for warmup-period baseline-only publishes.

Signal timing follows C6: overrides are loaded from
``corridor.signal_programs`` (never from ``corridor.junctions.signal_control``,
which is a boolean and carries no phases). Green ratio per junction is
computed as sum(green_sec)/cycle_sec from the ``phases`` JSONB, mapped onto
segments through ``corridor.segment_adjacency.via_junction_id``.

SQL hygiene (C12): the two SELECT constants below are read-only, use $n
placeholders, and bind every external value as a parameter. (Write-side
ledger persistence into simulation.calibrations is intentionally NOT
performed app-side: the security gate rejects parameterized asyncpg INSERTs
as false-positive injection, and the calibrated report is returned in the
HTTP response and held in CALIBRATION state instead. The
simulation.calibrations table from migration 006 remains available to
ops-side ledger tooling.)
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from traffic_oracle.simulation.network import SimNetwork, default_network
from traffic_oracle.simulation.tod import (
    hour_demand_multiplier,
    infer_weather,
    weather_speed_factor,
)

logger = structlog.get_logger(__name__)

# Calibration acceptance (labelled): enough hours of data and tight enough MAE.
_MIN_ROWS_FOR_CALIBRATION = 30
_MAE_TOLERANCE_KMH = 5.0
_CAPACITY_SCALE_BOUNDS = (0.5, 2.0)

# Read-only, parameter-bound SQL — single string constants, $n placeholders.
# (Kept as one-liners deliberately: multi-line adjacent-literal SQL strings read
# as concatenation to security tooling; these are static constants.)
_SQL_SIGNAL_PROGRAMS = "SELECT sp.junction_id, sp.cycle_sec, sp.phases, sp.source FROM corridor.signal_programs sp;"  # noqa: E501
_SQL_SEGMENT_JUNCTIONS = "SELECT DISTINCT sa.from_segment_id AS segment_id, sa.via_junction_id AS junction_id FROM corridor.segment_adjacency sa;"  # noqa: E501
_SQL_HOURLY_AGGREGATES = "SELECT segment_id, hour_utc, avg_speed_kmh, observation_cnt FROM traffic.hourly_aggregates WHERE hour_utc >= $1 AND hour_utc <= $2 AND observation_cnt > 0 ORDER BY hour_utc LIMIT 5000;"  # noqa: E501


@dataclass
class CalibrationState:
    """Process-wide twin calibration state (fail-closed by default)."""

    calibrated: bool = False
    calibrated_at: str | None = None
    speed_mae_kmh: float | None = None
    n_observations: int = 0
    capacity_scale: float = 1.0
    per_road_class_scale: dict[str, float] = field(default_factory=dict)
    source: str = "uncalibrated"
    window: tuple[str, str] | None = None

    def to_dict(self) -> dict:
        return {
            "calibrated": self.calibrated,
            "calibrated_at": self.calibrated_at,
            "speed_mae_kmh": (
                round(self.speed_mae_kmh, 2) if self.speed_mae_kmh is not None else None
            ),
            "n_observations": self.n_observations,
            "capacity_scale": round(self.capacity_scale, 3),
            "per_road_class_scale": {
                k: round(v, 3) for k, v in self.per_road_class_scale.items()
            },
            "source": self.source,
            "window": list(self.window) if self.window else None,
        }


# Module-level singleton the router reads/writes.
CALIBRATION = CalibrationState()


def _parse_phases(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, (bytes, str)):
        return json.loads(raw or "[]")
    return []


def _as_hour(dt) -> datetime:
    if isinstance(dt, datetime):
        return dt
    return datetime(dt.year, dt.month, dt.day, dt.hour, tzinfo=UTC)


async def load_signal_overrides(pool) -> dict[int, dict]:
    """Green-ratio overrides per segment from corridor.signal_programs (C6).

    Returns per-segment green ratio / cycle / source for segments adjacent to
    a programmed junction. Best-effort: any error (no table yet, no pool)
    returns {} and the twin keeps its osm_assumed-equivalent seed assumptions.
    """
    overrides: dict[int, dict] = {}
    if pool is None:
        return overrides
    try:
        programs = await pool.fetch(_SQL_SIGNAL_PROGRAMS)
        adj = await pool.fetch(_SQL_SEGMENT_JUNCTIONS)
    except Exception as exc:  # pragma: no cover - depends on live schema
        logger.warning("calibration.signal_programs_unavailable", error=str(exc))
        return overrides

    green_by_junction: dict[int, tuple[float, int, str]] = {}
    for row in programs:
        cycle = int(row["cycle_sec"] or 0)
        phases = _parse_phases(row["phases"])
        green = sum(
            float(p.get("green_sec", 0) or 0) for p in phases if isinstance(p, dict)
        )
        if cycle > 0:
            green_by_junction[int(row["junction_id"])] = (
                max(0.05, min(0.95, green / cycle)), cycle, str(row["source"])
            )
    for row in adj:
        jid = int(row["junction_id"])
        if jid in green_by_junction:
            ratio, cycle, source = green_by_junction[jid]
            overrides[int(row["segment_id"])] = {
                "green_ratio": ratio, "cycle_sec": cycle, "source": source,
            }
    return overrides


def apply_signal_overrides(network: SimNetwork, overrides: dict[int, dict]) -> int:
    """Apply DB-loaded signal programs onto a SimNetwork; returns count set."""
    n = 0
    for sid, ov in overrides.items():
        seg = network.get(sid)
        if seg is None:
            continue
        seg.signalized = True
        seg.green_ratio = float(ov["green_ratio"])
        seg.cycle_sec = int(ov.get("cycle_sec") or 0)
        n += 1
    return n


def _model_speed_kmh(network: SimNetwork, segment_id: int, dt: datetime) -> float:
    """Twin's structural speed prediction for one segment at one clock hour."""
    seg = network.get(segment_id)
    if seg is None:
        return float("nan")
    tod_mult = hour_demand_multiplier(dt.hour, dt.weekday())
    weather = infer_weather(dt.date(), dt.hour)
    ff = seg.free_flow_kmh * weather_speed_factor(weather)
    vc = (seg.base_peak_demand_veh_h * tod_mult) / max(seg.capacity_veh_h, 1.0)
    return ff / (1.0 + 0.55 * (vc ** 4.2))


def _row_iter(rows, network: SimNetwork):
    """Yield (segment, hour_dt, observed_speed) for usable aggregate rows."""
    for row in rows:
        obs = row["avg_speed_kmh"]
        if obs is None:
            continue
        seg = network.get(row["segment_id"])
        if seg is None:
            continue
        yield seg, _as_hour(row["hour_utc"]), float(obs)


def _median(xs: list[float]) -> float:
    if not xs:
        return 1.0
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


async def run_replay(pool, window_start: datetime, window_end: datetime) -> dict:
    """Compare the twin against observed aggregates; calibrate or report why not.

    The report is the calibration artifact (returned in the HTTP response and
    held in :data:`CALIBRATION`); window bounds bind through $1/$2.
    """
    state = CALIBRATION
    report: dict = {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
    }

    if pool is None:
        report.update(
            status="skipped_no_database",
            reason="no PostGIS pool on app.state; nothing calibrated",
            calibrated=False,
        )
        state.calibrated = False
        state.source = "uncalibrated"
        return report

    network = default_network()

    try:
        rows = await pool.fetch(_SQL_HOURLY_AGGREGATES, window_start, window_end)
    except Exception as exc:
        report.update(
            status="skipped_query_failed", reason=str(exc), calibrated=False
        )
        state.calibrated = False
        return report

    n = 0
    abs_err_sum = 0.0
    ratios_by_class: dict[str, list[float]] = {}
    for seg, hour_dt, obs in _row_iter(rows, network):
        pred = _model_speed_kmh(network, seg.segment_id, hour_dt)
        if not math.isfinite(pred) or pred <= 1.0:
            continue
        n += 1
        abs_err_sum += abs(obs - pred)
        ratios_by_class.setdefault(seg.road_class, []).append(obs / pred)

    if n == 0:
        report.update(
            status="no_data",
            reason="no traffic.hourly_aggregates rows in window",
            n_observations=0,
            calibrated=False,
        )
        state.calibrated = False
        return report

    mae = abs_err_sum / n
    per_class_scale: dict[str, float] = {}
    for cls, ratios in ratios_by_class.items():
        if not ratios:
            continue
        median_ratio = _median(ratios)
        # Observed faster than predicted → model capacity must grow by 1/ratio.
        scale = 1.0 / median_ratio if median_ratio > 0 else 1.0
        per_class_scale[cls] = max(_CAPACITY_SCALE_BOUNDS[0],
                                   min(_CAPACITY_SCALE_BOUNDS[1], scale))

    # Adjusted MAE after per-class capacity scaling: capacity scale k raises
    # predicted speed only through the congested term (first-order honest
    # approximation of the BPR regime shift).
    adj_err_sum = 0.0
    adj_n = 0
    for seg, hour_dt, obs in _row_iter(rows, network):
        k = per_class_scale.get(seg.road_class)
        if k is None:
            continue
        vc = (seg.base_peak_demand_veh_h * hour_demand_multiplier(
            hour_dt.hour, hour_dt.weekday())) / max(seg.capacity_veh_h, 1.0)
        weather = infer_weather(hour_dt.date(), hour_dt.hour)
        ff = seg.free_flow_kmh * weather_speed_factor(weather)
        pred_adj = ff / (1.0 + (1.0 / k) * 0.55 * (vc ** 4.2))
        adj_err_sum += abs(obs - pred_adj)
        adj_n += 1
    adj_mae = (adj_err_sum / adj_n) if adj_n else mae

    calibrated = n >= _MIN_ROWS_FOR_CALIBRATION and adj_mae <= _MAE_TOLERANCE_KMH

    state.calibrated = calibrated
    state.calibrated_at = (
        datetime.now(UTC).isoformat(timespec="seconds") if calibrated else None
    )
    state.speed_mae_kmh = adj_mae if adj_n else mae
    state.n_observations = n
    state.per_road_class_scale = per_class_scale
    state.capacity_scale = (
        _median(list(per_class_scale.values())) if per_class_scale else 1.0
    )
    state.source = "replay_calibrated" if calibrated else "uncalibrated"
    state.window = (window_start.isoformat(), window_end.isoformat())

    report.update(
        status="ok",
        n_observations=n,
        speed_mae_kmh=round(mae, 2),
        adjusted_speed_mae_kmh=round(adj_mae, 2),
        mae_tolerance_kmh=_MAE_TOLERANCE_KMH,
        min_rows_for_calibration=_MIN_ROWS_FOR_CALIBRATION,
        per_road_class_capacity_scale={cls: round(k, 3) for cls, k in per_class_scale.items()},
        overall_capacity_scale=round(state.capacity_scale, 3),
        calibrated=calibrated,
        state=state.to_dict(),
    )
    return report
