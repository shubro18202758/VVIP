"""Replay calibration for the twin (C5/C6).

``POST /api/v1/simulate/replay`` compares the twin's structural speed
prediction against observed ``traffic.hourly_aggregates`` over a window,
reports speed MAE, fits a bounded capacity scale per road class, and flips
the twin's ``calibrated`` flag only when there is enough data (>= 30
hourly aggregates spanning >= :data:`MIN_FIELD_HOURS` distinct field
hours — the same bar the scheduler applies, so an operator-triggered
replay cannot calibrate on one busy hour) and the fitted MAE is inside
tolerance. Until then every twin response reports
``calibrated: false`` — the fail-closed posture contract C4 relies on for
warmup-period baseline-only publishes.

Field data only: ``traffic.hourly_aggregates`` is rolled up from
``data_quality = 'real'`` observations (ingest/partitions.py), so a
simulated feed can never "field-calibrate" the twin and unlock
non-baseline publishes or signal-plan commits.  While no field hours
exist, :data:`CALIBRATION` carries the explicit reason (``no field
observations — simulated feed only``) that the adaptive shadow gate
quotes verbatim.

The fit (honest direction): for each road class the capacity scale ``k``
in [0.5, 2.0] MINIMISES the MAE of the engine's OWN speed formula,
``ff / (1 + 0.55 · (v/c ÷ k)^4.2)`` — exactly how the engine applies
``SimulationConfig.class_capacity_scale`` — on the same network the twin
runs (DB signal programs applied).  k = 1 is on the search grid, so the
fitted MAE can never be worse than the uncalibrated one.  Each hourly UTC
bucket is evaluated at its midpoint in IST (``hour_utc + 30 min`` is a
whole IST hour), the clock every demand curve is written in.

Signal timing follows C6: programs are loaded from
``corridor.signal_programs`` (never from ``corridor.junctions.
signal_control``, a boolean with no phases).  Each APPROACH gets the green
share of the phase that serves it — the junction's major-road ``through``
legs (``enforcement.rules.through_legs``: road-class rank + straightness
over the leg geometry) get phase 0's movement, every other leg the cross
movement, compass programs map by approach axis — not the sum of every
phase's green (≈ 0.85, which roughly doubled signalized capacity).  A
segment that approaches two programmed junctions keeps the binding
(smaller) share.  Only segments whose endpoint is within 25 m of a
programmed junction (its legs) are signalized.

Persistence: every completed replay is written to
``simulation.calibrations`` (parameter-bound INSERT) and the newest row
younger than :data:`CALIBRATION_MAX_AGE_DAYS` is reloaded at startup,
before the situation engine's first decision;
:mod:`traffic_oracle.simulation.scheduler` re-runs the replay hourly once
the aggregates hold >= 24 field hours.  A replay reads the window's
aggregates newest first (:data:`REPLAY_ROW_LIMIT`).

SQL hygiene (C12): every statement below is a single-line constant with
``$n`` placeholders; every external value is bound as a parameter.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog

from traffic_oracle.simulation.network import SegmentSpec, SimNetwork, default_network
from traffic_oracle.simulation.tod import (
    infer_weather_at,
    time_of_day_demand,
    to_ist,
    weather_speed_factor,
)

logger = structlog.get_logger(__name__)

# Calibration acceptance (labelled): enough hours of data and tight enough MAE.
_MIN_ROWS_FOR_CALIBRATION = 30
_MAE_TOLERANCE_KMH = 5.0
_CAPACITY_SCALE_BOUNDS = (0.5, 2.0)
#: Grid step of the 1-D capacity-scale search (151 candidates incl. 1.0).
_SCALE_STEP = 0.01
#: The engine's BPR speed curve (simulation/engine.py _BPR_A/_BPR_B).
_BPR_A = 0.55
_BPR_B = 4.2
#: A persisted calibration older than this is not reloaded (fail closed).
CALIBRATION_MAX_AGE_DAYS = 7
#: Minimum field coverage before the scheduler replays (hours of 'real'
#: hourly aggregates inside the window).
MIN_FIELD_HOURS = 24
#: A segment endpoint within this distance of a junction is one of its legs
#: (the same 25 m snap the seed topology and enforcement use).
LEG_SNAP_TOL_M = 25.0
#: Hourly aggregates one replay reads, NEWEST first.  A full 7-day window of
#: today's 47 corridor segments is 47 x 168 = 7,896 rows; the bound leaves
#: room for network growth, and if it ever truncates, the oldest hours drop
#: (``rows_truncated`` in the report) — never the newest, as the old
#: ascending LIMIT 5000 did (it fitted only the oldest ~106 h of a week).
REPLAY_ROW_LIMIT = 20_000

REASON_SIMULATED_ONLY = "no field observations — simulated feed only"
REASON_NO_FIELD = "no field observations"

# Read-only, parameter-bound SQL — single string constants, $n placeholders.
# (Kept as one-liners deliberately: multi-line adjacent-literal SQL strings read
# as concatenation to security tooling; these are static constants.)
_SQL_SIGNAL_PROGRAMS = "SELECT sp.junction_id, sp.cycle_sec, sp.phases, sp.source FROM corridor.signal_programs sp;"  # noqa: E501
_SQL_PROGRAM_LEGS = "SELECT j.junction_id, s.segment_id, s.road_class, degrees(CASE WHEN ST_Distance(ST_StartPoint(s.geom)::geography, j.geom::geography) <= ST_Distance(ST_EndPoint(s.geom)::geography, j.geom::geography) THEN ST_Azimuth(ST_PointN(s.geom, 1)::geography, ST_PointN(s.geom, 2)::geography) ELSE ST_Azimuth(ST_PointN(s.geom, ST_NPoints(s.geom))::geography, ST_PointN(s.geom, ST_NPoints(s.geom) - 1)::geography) END) AS bearing_deg FROM corridor.signal_programs p JOIN corridor.junctions j ON j.junction_id = p.junction_id JOIN corridor.road_segments s ON s.geom && ST_Expand(j.geom, 0.001) AND (ST_DWithin(ST_StartPoint(s.geom)::geography, j.geom::geography, $1) OR ST_DWithin(ST_EndPoint(s.geom)::geography, j.geom::geography, $1)) ORDER BY j.junction_id, s.segment_id;"  # noqa: E501
_SQL_DB_SEGMENTS = "SELECT s.segment_id, s.road_name, s.road_class, s.lanes, s.speed_limit_kmh, s.oneway, ST_Length(s.geom::geography) AS length_m FROM corridor.road_segments s ORDER BY s.segment_id;"  # noqa: E501
_SQL_DB_ADJACENCY = "SELECT DISTINCT sa.from_segment_id, sa.to_segment_id FROM corridor.segment_adjacency sa;"  # noqa: E501
_SQL_HOURLY_AGGREGATES = "SELECT segment_id, hour_utc, avg_speed_kmh, observation_cnt FROM traffic.hourly_aggregates WHERE hour_utc >= $1 AND hour_utc <= $2 AND observation_cnt > 0 ORDER BY hour_utc DESC, segment_id LIMIT $3;"  # noqa: E501
_SQL_FIELD_HOURS = "SELECT count(DISTINCT hour_utc) AS hours, min(hour_utc) AS first_hour, max(hour_utc) AS last_hour FROM traffic.hourly_aggregates WHERE hour_utc >= $1 AND observation_cnt > 0;"  # noqa: E501
_SQL_FEED_QUALITY = "SELECT EXISTS (SELECT 1 FROM traffic.observations WHERE timestamp_utc >= $1 AND data_quality = 'real') AS has_real, EXISTS (SELECT 1 FROM traffic.observations WHERE timestamp_utc >= $1 AND data_quality = 'synthetic') AS has_synthetic;"  # noqa: E501
_SQL_INSERT_CALIBRATION = "INSERT INTO simulation.calibrations (window_start, window_end, n_observations, speed_mae_kmh, capacity_scale, per_road_class_scale, calibrated, details) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8::jsonb) RETURNING calibration_id, created_at;"  # noqa: E501
_SQL_LATEST_CALIBRATION = "SELECT calibration_id, window_start, window_end, n_observations, speed_mae_kmh, capacity_scale, per_road_class_scale, calibrated, details, created_at FROM simulation.calibrations WHERE created_at >= $1 ORDER BY created_at DESC, calibration_id DESC LIMIT 1;"  # noqa: E501


@dataclass
class CalibrationState:
    """Process-wide twin calibration state (fail-closed by default).

    ``reason`` says why the twin is (not) calibrated in operator words —
    the adaptive shadow gate quotes it in its refusal.  ``version`` bumps
    on every change so dependants can invalidate cached verdicts.

    Two kinds of fact live here: the CURRENT field coverage (``reason``,
    ``field_hours``, ``checked_at``) and the LAST field replay (MAE,
    window, observations — run in this process or reloaded from the
    ledger).  ``to_dict()['last_replay']`` groups the latter with its own
    field hours and origin, so a reloaded MAE is never read as a fit on the
    current coverage.
    """

    calibrated: bool = False
    calibrated_at: str | None = None
    speed_mae_kmh: float | None = None
    n_observations: int = 0
    capacity_scale: float = 1.0
    per_road_class_scale: dict[str, float] = field(default_factory=dict)
    source: str = "uncalibrated"
    window: tuple[str, str] | None = None
    reason: str | None = "not yet checked for field observations"
    calibration_id: int | None = None
    field_hours: int | None = None
    checked_at: str | None = None
    version: int = 0
    # the last field replay: 'replayed' (this process) | 'ledger' (reloaded)
    replay_origin: str | None = None
    replay_field_hours: int | None = None
    replay_calibrated: bool | None = None
    # what the observation feeds carry right now (the scheduler's coverage
    # check): 'real' | 'synthetic' | 'mixed' | 'none' — None until checked
    feed_quality: str | None = None

    def input_quality(self) -> str:
        """The ACTUAL quality of the twin's calibration input, for the
        ``data_quality`` label: 'real' once field (real) hours exist or a
        field replay calibrated it; 'synthetic' while only the simulated
        feed flows (it is never a calibration input — the label says what
        there is, the refusal reason says why nothing calibrated); 'none'
        with no feed at all; 'unknown' before the first coverage check."""
        if self.calibrated or (self.field_hours or 0) > 0:
            return "real"
        if self.feed_quality in ("synthetic", "mixed"):
            return "synthetic"
        if self.feed_quality == "none" or self.field_hours == 0:
            return "none"
        return "unknown"

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
            "reason": self.reason,
            "calibration_id": self.calibration_id,
            "field_hours": self.field_hours,
            "min_field_hours": MIN_FIELD_HOURS,
            "checked_at": self.checked_at,
            # the input the calibration actually has (never a constant
            # 'real' next to a "simulated feed only" refusal)
            "data_quality": self.input_quality(),
            "feed_quality": self.feed_quality,
            "calibration_input": "traffic.hourly_aggregates (real observations only)",
            "last_replay": (
                {
                    "origin": self.replay_origin,
                    "calibration_id": self.calibration_id,
                    "window": list(self.window) if self.window else None,
                    "speed_mae_kmh": (
                        round(self.speed_mae_kmh, 2)
                        if self.speed_mae_kmh is not None
                        else None
                    ),
                    "n_observations": self.n_observations,
                    "field_hours": self.replay_field_hours,
                    "calibrated": self.replay_calibrated,
                }
                if self.replay_origin is not None
                else None
            ),
        }

    def readiness(self) -> tuple[bool, str | None]:
        """``(calibrated, reason)`` — the adaptive layer's twin readiness port."""
        return self.calibrated, (None if self.calibrated else self.reason)

    def mark_uncalibrated(self, reason: str, source: str = "uncalibrated") -> None:
        changed = self.calibrated or self.reason != reason
        self.calibrated = False
        self.calibrated_at = None
        self.capacity_scale = 1.0
        self.per_road_class_scale = {}
        self.source = source
        self.reason = reason
        if changed:
            self.version += 1


# Module-level singleton the router reads/writes.
CALIBRATION = CalibrationState()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_phases(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, (bytes, str)):
        return json.loads(raw or "[]")
    return []


def _as_hour(dt) -> datetime:
    if isinstance(dt, datetime):
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return datetime(dt.year, dt.month, dt.day, dt.hour, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Per-approach signal greens (C6)
# ---------------------------------------------------------------------------

_COMPASS = frozenset({"NS", "EW"})


def _compass_axis(inbound_deg: float) -> str:
    h = float(inbound_deg) % 360.0
    if h >= 315.0 or h < 45.0 or 135.0 <= h < 225.0:
        return "NS"
    return "EW"


def _program_vocabulary(phases: list[dict]) -> tuple[str | None, str | None]:
    """(major movement, cross movement) as the program names them.

    Phase 0 serves the major road (006 seeds it first); the cross movement
    is 'cross' when a later phase lists it, else the first other label of
    phase 1 — the same reading enforcement/rules.py applies.
    """
    labels = [
        [str(m) for m in (p.get("movements") or [])]
        for p in phases
        if isinstance(p, dict)
    ]
    labels = [ms for ms in labels if ms]
    if not labels:
        return None, None
    major = labels[0][0]
    for movements in labels[1:]:
        if "cross" in movements and major != "cross":
            return major, "cross"
    if len(labels) > 1:
        others = [m for m in labels[1] if m != major]
        return major, (others[0] if others else None)
    return major, None


def _green_for(phases: list[dict], movement: str | None) -> float:
    if movement is None:
        return 0.0
    return sum(
        float(p.get("green_sec", 0) or 0)
        for p in phases
        if isinstance(p, dict) and movement in (p.get("movements") or [])
    )


def approach_green_overrides(
    programs: list[dict], legs: list[dict]
) -> dict[int, dict]:
    """Per-segment green share of the phase serving each approach (pure).

    ``programs``: rows {junction_id, cycle_sec, phases, source};
    ``legs``: rows {junction_id, segment_id, road_class, bearing_deg}
    (bearing pointing AWAY from the junction).  Returns
    ``{segment_id: {green_ratio, cycle_sec, source, junction_id,
    movement}}`` — clamped to [0.05, 0.95]; a segment that is a leg of
    several programmed junctions keeps its smallest (binding) share.
    """
    from traffic_oracle.enforcement.rules import JunctionLeg, through_legs

    legs_by_junction: dict[int, list[dict]] = {}
    for leg in legs:
        if leg.get("bearing_deg") is None:
            continue
        legs_by_junction.setdefault(int(leg["junction_id"]), []).append(leg)

    overrides: dict[int, dict] = {}
    for program in programs:
        cycle = int(program.get("cycle_sec") or 0)
        if cycle <= 0:
            continue
        jid = int(program["junction_id"])
        phases = _parse_phases(program.get("phases"))
        labels = {
            str(m)
            for p in phases
            if isinstance(p, dict)
            for m in (p.get("movements") or [])
        }
        major, cross = _program_vocabulary(phases)
        junction_legs = legs_by_junction.get(jid, [])
        through = through_legs(
            tuple(
                JunctionLeg(
                    segment_id=int(leg["segment_id"]),
                    road_class=str(leg.get("road_class") or "secondary"),
                    bearing_deg=float(leg["bearing_deg"]),
                )
                for leg in junction_legs
            )
        )
        for leg in junction_legs:
            sid = int(leg["segment_id"])
            if labels and labels <= _COMPASS:
                movement: str | None = _compass_axis(float(leg["bearing_deg"]) + 180.0)
            else:
                movement = major if sid in through else cross
            green = _green_for(phases, movement)
            if green <= 0.0:
                continue
            ratio = max(0.05, min(0.95, green / cycle))
            current = overrides.get(sid)
            if current is None or ratio < current["green_ratio"]:
                overrides[sid] = {
                    "green_ratio": ratio,
                    "cycle_sec": cycle,
                    "source": str(program.get("source") or "db"),
                    "junction_id": jid,
                    "movement": movement,
                }
    return overrides


async def load_signal_overrides(pool) -> dict[int, dict]:
    """Per-approach green overrides from corridor.signal_programs (C6).

    Best-effort: any error (no table yet, no pool) returns {} and the twin
    keeps its osm_assumed-equivalent seed assumptions.
    """
    if pool is None:
        return {}
    try:
        programs = await pool.fetch(_SQL_SIGNAL_PROGRAMS)
        legs = await pool.fetch(_SQL_PROGRAM_LEGS, LEG_SNAP_TOL_M)
    except Exception as exc:  # pragma: no cover - depends on live schema
        logger.warning("calibration.signal_programs_unavailable", error=str(exc))
        return {}
    return approach_green_overrides(
        [dict(r) for r in programs], [dict(r) for r in legs]
    )


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


async def extend_network_from_db(network: SimNetwork, pool) -> int:
    """Add DB road segments the frozen seed lacks (e.g. the 012 connectors).

    Routes the planner builds today can run over segments added after the
    seed; without them the twin silently simulated only part of a route.
    Added segments get their DB lengths and the DB adjacency; they are
    signalized only through :func:`apply_signal_overrides`.  Best-effort:
    returns the number added (0 on any error).
    """
    if pool is None:
        return 0
    try:
        rows = await pool.fetch(_SQL_DB_SEGMENTS)
        adjacency = await pool.fetch(_SQL_DB_ADJACENCY)
    except Exception as exc:  # pragma: no cover - depends on live schema
        logger.warning("calibration.db_segments_unavailable", error=str(exc))
        return 0
    added = 0
    for r in rows:
        sid = int(r["segment_id"])
        if sid in network.segments:
            continue
        network.segments[sid] = SegmentSpec(
            segment_id=sid,
            road_name=str(r["road_name"] or ""),
            road_class=str(r["road_class"] or "secondary"),
            lanes=int(r["lanes"] or 2),
            speed_limit_kmh=int(round(float(r["speed_limit_kmh"] or 50))),
            oneway=bool(r["oneway"]),
            length_m=round(float(r["length_m"] or 500.0), 1),
        )
        added += 1
    if added:
        for r in adjacency:
            a, b = int(r["from_segment_id"]), int(r["to_segment_id"])
            if a in network.segments and b in network.segments:
                network.adjacency.setdefault(a, set()).add(b)
                network.adjacency.setdefault(b, set()).add(a)
    return added


async def build_twin_network(pool) -> tuple[SimNetwork, dict]:
    """The network the twin (and the replay) simulate: seed + DB extension
    + per-approach DB signal programs.  Returns (network, audit extras)."""
    network = default_network()
    extras: dict = {}
    if pool is None:
        return network, extras
    added = await extend_network_from_db(network, pool)
    if added:
        extras["db_segments_added"] = added
    applied = apply_signal_overrides(network, await load_signal_overrides(pool))
    if applied:
        extras["signal_programs_applied"] = applied
        extras["signal_green_basis"] = "serving_phase_per_approach"
    return network, extras


# ---------------------------------------------------------------------------
# The engine's structural speed model + the MAE-minimising fit
# ---------------------------------------------------------------------------


def bucket_midpoint(hour_utc) -> datetime:
    """Midpoint of an hourly UTC bucket — a whole hour in IST (+05:30)."""
    return _as_hour(hour_utc) + timedelta(minutes=30)


def model_speed_kmh(
    network: SimNetwork, segment_id: int, at: datetime, scale: float = 1.0
) -> float:
    """The twin's steady-state speed for one segment at one instant.

    ``ff / (1 + 0.55 · (v/c ÷ scale)^4.2)`` with demand from the IST
    time-of-day curve and weather inferred for the IST date — the engine's
    own formula (engine.py speed with ``SimulationConfig.scale_for``).
    """
    seg = network.get(segment_id)
    if seg is None:
        return float("nan")
    local = to_ist(at)
    tod = time_of_day_demand(local)
    ff = seg.free_flow_kmh * weather_speed_factor(infer_weather_at(local))
    vc = (seg.base_peak_demand_veh_h * tod) / max(seg.capacity_veh_h * scale, 1.0)
    return ff / (1.0 + _BPR_A * (vc ** _BPR_B))


def _row_iter(rows, network: SimNetwork):
    """Yield (segment, bucket midpoint, observed_speed) for usable rows."""
    for row in rows:
        obs = row["avg_speed_kmh"]
        if obs is None:
            continue
        obs = float(obs)
        if not math.isfinite(obs):
            continue
        seg = network.get(int(row["segment_id"]))
        if seg is None:
            continue
        yield seg, bucket_midpoint(row["hour_utc"]), obs


def _median(xs: list[float]) -> float:
    if not xs:
        return 1.0
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def _scale_grid() -> list[float]:
    lo, hi = _CAPACITY_SCALE_BOUNDS
    n = int(round((hi - lo) / _SCALE_STEP))
    return [round(lo + i * _SCALE_STEP, 4) for i in range(n + 1)]


def fit_class_scales(
    samples: list[tuple[SegmentSpec, datetime, float]],
    network: SimNetwork,
) -> dict[str, dict]:
    """Per road class: the capacity scale minimising the engine-formula MAE.

    Returns ``{class: {scale, n, mae_before, mae_after}}``.  The grid holds
    1.0, so ``mae_after <= mae_before`` always; ties prefer the scale
    closest to 1.0 (no move without evidence).
    """
    by_class: dict[str, list[tuple[SegmentSpec, datetime, float]]] = {}
    for seg, at, obs in samples:
        by_class.setdefault(seg.road_class, []).append((seg, at, obs))
    out: dict[str, dict] = {}
    grid = _scale_grid()
    for cls, rows in sorted(by_class.items()):
        # Precompute the scale-free pieces: ff and v/c at scale 1.
        pieces: list[tuple[float, float, float]] = []
        for seg, at, obs in rows:
            local = to_ist(at)
            ff = seg.free_flow_kmh * weather_speed_factor(infer_weather_at(local))
            vc = (seg.base_peak_demand_veh_h * time_of_day_demand(local)) / max(
                seg.capacity_veh_h, 1.0
            )
            pieces.append((ff, vc, obs))

        def mae(k: float, pieces=pieces) -> float:
            return sum(
                abs(obs - ff / (1.0 + _BPR_A * ((vc / k) ** _BPR_B)))
                for ff, vc, obs in pieces
            ) / len(pieces)

        best_k, best_mae = 1.0, mae(1.0)
        before = best_mae
        for k in grid:
            m = mae(k)
            if m < best_mae - 1e-12 or (
                abs(m - best_mae) <= 1e-12 and abs(k - 1.0) < abs(best_k - 1.0)
            ):
                best_k, best_mae = k, m
        out[cls] = {
            "scale": best_k,
            "n": len(pieces),
            "mae_before": before,
            "mae_after": best_mae,
        }
    return out


async def run_replay(
    pool,
    window_start: datetime,
    window_end: datetime,
    network: SimNetwork | None = None,
) -> dict:
    """Compare the twin against observed aggregates; calibrate or report why not.

    The report is the calibration artifact (returned in the HTTP response,
    held in :data:`CALIBRATION` and persisted by the caller); window
    bounds bind through $1/$2.
    """
    state = CALIBRATION
    report: dict = {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "data_quality": "real",
    }

    if pool is None:
        report.update(
            status="skipped_no_database",
            reason="no PostGIS pool on app.state; nothing calibrated",
            calibrated=False,
        )
        state.mark_uncalibrated("no database")
        return report

    extras: dict = {}
    if network is None:
        network, extras = await build_twin_network(pool)
    report.update(extras)

    try:
        rows = await pool.fetch(
            _SQL_HOURLY_AGGREGATES, window_start, window_end, REPLAY_ROW_LIMIT
        )
    except Exception as exc:
        report.update(
            status="skipped_query_failed", reason=str(exc), calibrated=False
        )
        state.mark_uncalibrated("hourly aggregates unavailable")
        return report
    report.update(
        row_limit=REPLAY_ROW_LIMIT,
        rows_truncated=len(rows) >= REPLAY_ROW_LIMIT,
        row_order="newest_first",
    )

    samples = [
        (seg, at, obs)
        for seg, at, obs in _row_iter(rows, network)
        if math.isfinite(model_speed_kmh(network, seg.segment_id, at))
    ]
    n = len(samples)
    if n == 0:
        report.update(
            status="no_data",
            reason=f"no traffic.hourly_aggregates rows in window ({REASON_NO_FIELD})",
            n_observations=0,
            calibrated=False,
            # nothing real to replay on: the replay had no input at all
            data_quality="none",
        )
        state.mark_uncalibrated(REASON_NO_FIELD)
        return report

    fits = fit_class_scales(samples, network)
    mae_before = sum(f["mae_before"] * f["n"] for f in fits.values()) / n
    mae_after = sum(f["mae_after"] * f["n"] for f in fits.values()) / n
    per_class_scale = {cls: f["scale"] for cls, f in fits.items()}
    # Distinct field hours the fit stands on: one busy hour can fit a
    # capacity scale to anything, so the same >= MIN_FIELD_HOURS bar the
    # scheduler applies before replaying gates the calibrated flag here
    # too — an operator-triggered replay never calibrates on less.
    field_hours = len({at for _seg, at, _obs in samples})

    calibrated = (
        n >= _MIN_ROWS_FOR_CALIBRATION
        and field_hours >= MIN_FIELD_HOURS
        and mae_after <= _MAE_TOLERANCE_KMH
    )
    if calibrated:
        state.calibrated = True
        state.calibrated_at = _now_iso()
        state.per_road_class_scale = per_class_scale
        state.capacity_scale = _median(list(per_class_scale.values()))
        state.source = "replay_calibrated"
        state.reason = (
            f"field replay: MAE {mae_after:.2f} km/h over {n} hourly aggregates "
            f"({field_hours} field hours)"
        )
        state.version += 1
    else:
        if n < _MIN_ROWS_FOR_CALIBRATION:
            reason = (
                f"field history too thin: {n} hourly aggregates of "
                f"{_MIN_ROWS_FOR_CALIBRATION} required"
            )
        elif field_hours < MIN_FIELD_HOURS:
            reason = (
                f"field history too thin: {field_hours} field hours of "
                f"{MIN_FIELD_HOURS} required"
            )
        else:
            reason = (
                f"twin does not fit field data: MAE {mae_after:.2f} km/h > "
                f"{_MAE_TOLERANCE_KMH} km/h"
            )
        state.mark_uncalibrated(reason)
    state.speed_mae_kmh = mae_after
    state.n_observations = n
    state.window = (window_start.isoformat(), window_end.isoformat())
    state.checked_at = _now_iso()
    state.replay_origin = "replayed"
    state.replay_field_hours = field_hours
    state.replay_calibrated = calibrated

    report.update(
        status="ok",
        n_observations=n,
        speed_mae_kmh=round(mae_before, 2),
        adjusted_speed_mae_kmh=round(mae_after, 2),
        mae_tolerance_kmh=_MAE_TOLERANCE_KMH,
        min_rows_for_calibration=_MIN_ROWS_FOR_CALIBRATION,
        field_hours=field_hours,
        min_field_hours=MIN_FIELD_HOURS,
        per_road_class_capacity_scale={cls: round(k, 3) for cls, k in per_class_scale.items()},
        per_road_class_fit={
            cls: {
                "scale": round(f["scale"], 3),
                "n": f["n"],
                "mae_before_kmh": round(f["mae_before"], 2),
                "mae_after_kmh": round(f["mae_after"], 2),
            }
            for cls, f in fits.items()
        },
        overall_capacity_scale=round(_median(list(per_class_scale.values())), 3),
        fit="grid_min_mae_engine_formula",
        time_basis="IST bucket midpoint",
        calibrated=calibrated,
        state=state.to_dict(),
    )
    return report


# ---------------------------------------------------------------------------
# Persistence (simulation.calibrations) + field-coverage check
# ---------------------------------------------------------------------------


async def persist_calibration(pool, report: dict) -> int | None:
    """Ledger one completed replay into simulation.calibrations.

    Only ``status == 'ok'`` reports carry an MAE; anything else is not a
    calibration and is not written.  Returns the new calibration_id.
    """
    if pool is None or report.get("status") != "ok":
        return None
    details = {
        k: report.get(k)
        for k in (
            "per_road_class_fit", "fit", "time_basis", "data_quality",
            "speed_mae_kmh", "mae_tolerance_kmh", "min_rows_for_calibration",
            "field_hours", "min_field_hours",
            "signal_programs_applied", "db_segments_added", "trigger",
            "row_limit", "rows_truncated",
        )
        if report.get(k) is not None
    }
    try:
        row = await pool.fetchrow(
            _SQL_INSERT_CALIBRATION,
            datetime.fromisoformat(report["window_start"]),
            datetime.fromisoformat(report["window_end"]),
            int(report["n_observations"]),
            float(report["adjusted_speed_mae_kmh"]),
            float(report["overall_capacity_scale"]),
            json.dumps(report.get("per_road_class_capacity_scale") or {}),
            bool(report["calibrated"]),
            json.dumps(details),
        )
    except Exception as exc:  # noqa: BLE001 - the ledger never breaks a replay
        logger.warning("calibration.persist_failed", error=str(exc))
        return None
    calibration_id = int(row["calibration_id"]) if row is not None else None
    if calibration_id is not None:
        CALIBRATION.calibration_id = calibration_id
    logger.info(
        "calibration.persisted",
        calibration_id=calibration_id,
        calibrated=bool(report["calibrated"]),
        n=report["n_observations"],
    )
    return calibration_id


def _ledger_details(row) -> dict:
    """A ledger row's persisted ``details`` JSON (empty when unreadable)."""
    raw = row["details"]
    try:
        return json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw or {})
    except (TypeError, ValueError):
        return {}


def _ledger_refusal(row) -> str:
    """Why a ledgered (uncalibrated) replay did not calibrate, in words.

    Read from the row's persisted ``details`` (field hours, row floor,
    MAE tolerance) so a thin-history refusal is never reported as a misfit.
    """
    details = _ledger_details(row)
    label = f"last field replay #{row['calibration_id']}"
    n = int(row["n_observations"])
    mae = float(row["speed_mae_kmh"])
    min_rows = details.get("min_rows_for_calibration")
    hours = details.get("field_hours")
    min_hours = details.get("min_field_hours")
    if isinstance(min_rows, int) and n < min_rows:
        return f"{label} stood on {n} hourly aggregates of {min_rows} required"
    if isinstance(hours, int) and isinstance(min_hours, int) and hours < min_hours:
        return f"{label} stood on {hours} field hours of {min_hours} required"
    return f"{label} did not fit (MAE {mae:.2f} km/h)"


async def reload_calibration(
    pool, max_age_days: int = CALIBRATION_MAX_AGE_DAYS
) -> bool:
    """Restore :data:`CALIBRATION` from the newest recent ledger row.

    A row older than ``max_age_days`` (or none) leaves the twin
    uncalibrated — a stale fit never unlocks publishes.  Returns whether
    the twin is calibrated afterwards.
    """
    state = CALIBRATION
    if pool is None:
        return state.calibrated
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    try:
        row = await pool.fetchrow(_SQL_LATEST_CALIBRATION, cutoff)
    except Exception as exc:  # noqa: BLE001
        logger.warning("calibration.reload_failed", error=str(exc))
        return state.calibrated
    if row is None:
        logger.info("calibration.reload_none", max_age_days=max_age_days)
        return state.calibrated
    scales_raw = row["per_road_class_scale"]
    scales = json.loads(scales_raw) if isinstance(scales_raw, str) else dict(scales_raw or {})
    created = row["created_at"]
    if bool(row["calibrated"]):
        state.calibrated = True
        state.calibrated_at = created.isoformat() if created else None
        state.per_road_class_scale = {str(k): float(v) for k, v in scales.items()}
        state.capacity_scale = float(row["capacity_scale"] or 1.0)
        state.source = "replay_calibrated(reloaded)"
        state.reason = (
            f"field replay #{row['calibration_id']} reloaded: MAE "
            f"{float(row['speed_mae_kmh']):.2f} km/h"
        )
    else:
        state.mark_uncalibrated(_ledger_refusal(row))
    state.speed_mae_kmh = float(row["speed_mae_kmh"])
    state.n_observations = int(row["n_observations"])
    state.calibration_id = int(row["calibration_id"])
    ws, we = row["window_start"], row["window_end"]
    state.window = (ws.isoformat(), we.isoformat()) if ws and we else None
    hours = _ledger_details(row).get("field_hours")
    state.replay_origin = "ledger"
    state.replay_field_hours = hours if isinstance(hours, int) else None
    state.replay_calibrated = bool(row["calibrated"])
    state.version += 1
    logger.info(
        "calibration.reloaded",
        calibration_id=state.calibration_id,
        calibrated=state.calibrated,
    )
    return state.calibrated


async def field_coverage(pool, window_days: int = CALIBRATION_MAX_AGE_DAYS) -> dict:
    """How many field (real) hourly aggregates exist, and what feeds are live.

    Returns ``{field_hours, first_hour, last_hour, has_real, has_synthetic,
    reason}`` where ``reason`` is the operator-facing explanation of why a
    replay can (not) run yet.
    """
    since = datetime.now(UTC) - timedelta(days=window_days)
    hours_row = await pool.fetchrow(_SQL_FIELD_HOURS, since)
    quality_row = await pool.fetchrow(
        _SQL_FEED_QUALITY, datetime.now(UTC) - timedelta(hours=24)
    )
    hours = int(hours_row["hours"] or 0) if hours_row is not None else 0
    has_real = bool(quality_row["has_real"]) if quality_row is not None else False
    has_synth = bool(quality_row["has_synthetic"]) if quality_row is not None else False
    if hours == 0:
        reason = REASON_SIMULATED_ONLY if has_synth and not has_real else REASON_NO_FIELD
    elif hours < MIN_FIELD_HOURS:
        reason = f"field history {hours}h of {MIN_FIELD_HOURS}h required"
    else:
        reason = None
    return {
        "field_hours": hours,
        "first_hour": (
            hours_row["first_hour"].isoformat()
            if hours_row is not None and hours_row["first_hour"]
            else None
        ),
        "last_hour": (
            hours_row["last_hour"].isoformat()
            if hours_row is not None and hours_row["last_hour"]
            else None
        ),
        "has_real": has_real,
        "has_synthetic": has_synth,
        # what the feeds carried in the last 24 h (the twin_calibration
        # data_quality label reads it — CalibrationState.input_quality)
        "feed_quality": (
            ("mixed" if has_synth else "real")
            if has_real
            else ("synthetic" if has_synth else "none")
        ),
        "reason": reason,
    }


__all__ = [
    "CALIBRATION",
    "CALIBRATION_MAX_AGE_DAYS",
    "CalibrationState",
    "MIN_FIELD_HOURS",
    "REASON_NO_FIELD",
    "REASON_SIMULATED_ONLY",
    "REPLAY_ROW_LIMIT",
    "apply_signal_overrides",
    "approach_green_overrides",
    "bucket_midpoint",
    "build_twin_network",
    "extend_network_from_db",
    "field_coverage",
    "fit_class_scales",
    "load_signal_overrides",
    "model_speed_kmh",
    "persist_calibration",
    "reload_calibration",
    "run_replay",
]
