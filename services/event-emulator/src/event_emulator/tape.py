"""The cause tape — a pure, deterministic function of (spec, seed, t0).

Cause kinds are the six of contract C4 plus the two watchlist additions of
contract C2 (watchlist-anpr stream):
    event_row, inputs_doc, scheduled_event, event_config,
    weather_ramp, crowd_script,
    anpr_read, agency_unit

'anpr_read' and 'agency_unit' fire only for profiles carrying watchlist
knobs (anpr_cameras / agency_units > 0).  Every generated plate carries an
'EM' segment (self-labelling synthetic), read identity is uuid5 over
(use_case, seed, camera, plate), and the stolen-plate + earmarked-plate
HITS are labelled in the frame payload — causes only, never outcomes.

('emergency_request' is not a kind — flood relief is priority sub-event rows
+ scheduled emergency_vehicle + ambulance_delay_projection inputs.)

Every frame carries t_sec (seconds after t0_anchor); replay re-posts the
stored frames verbatim with only the wall-clock anchor shifted.  The same
(spec, seed, t0_anchor, segment_centroids) builds a byte-identical tape on
any host.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from event_emulator.library import UseCase
from event_emulator.profiles import UseCaseProfile
from event_emulator.rng import SeededRng, derive_seed
from event_emulator.spec import ResolvedRun

__all__ = [
    "CAUSE_KINDS",
    "CauseFrame",
    "CauseTape",
    "movement_uuid",
    "anpr_read_uuid",
    "build_tape",
]

CAUSE_KINDS = (
    "event_row",
    "inputs_doc",
    "scheduled_event",
    "event_config",
    "weather_ramp",
    "crowd_script",
    # watchlist additions (contract C2):
    "anpr_read",
    "agency_unit",
)

# Deterministic movement identity: uuid5 over (use_case, seed) — the SAME id
# on every build, so a replay re-animates the same event row (idempotent
# upsert) instead of forking a new one.
_EMULATOR_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "vvip-event-emulator")


def movement_uuid(use_case_id: str, seed: int) -> str:
    return str(uuid.uuid5(_EMULATOR_NAMESPACE, f"{use_case_id}|{seed}"))


def anpr_read_uuid(use_case_id: str, seed: int, camera_id: str, plate: str) -> str:
    """Deterministic camera-read identity (contract C2): uuid5 over
    (use_case, seed, camera, plate).  One read per (camera, plate) pair per
    tape — identical on every host, so replays upsert sightings idempotently
    (the oracle's corridor.vehicle_sightings ON CONFLICT DO NOTHING)."""
    return str(
        uuid.uuid5(_EMULATOR_NAMESPACE, f"{use_case_id}|{seed}|{camera_id}|{plate}")
    )


# ── watchlist generators (contract C2) ──────────────────────────────────────

#: Synthetic-plate alphabet — every generated plate embeds 'EM' so an
#: emulated plate is self-labelling wherever it surfaces (C6 badge).
_PLATE_RTO_PREFIX = "GJ01"   # Gujarat RTO 01 (Ahmedabad)
_PLATE_MARKER = "EM"

#: The STOLEN-vehicle demo plate (a FIR-named plate among the reads).
_STOLEN_LABEL = "GJ27EM9001"
#: The EARMARKED demo plate (an operator earmark demo among the reads).
_EARMARKED_LABEL = "GJ27EM4321"

#: Fallback old-city belt anchor (lon, lat) used when the caller passes no
#: segment centroids — deterministic on every host either way.
_FALLBACK_ANCHOR = (72.58, 22.99)

#: Deg-per-meter on this latitude belt (~111320 m/deg lat; lon shrinks by
#: cos(lat)) — for deterministic sub-kilometer placement jitter.
_M_PER_DEG_LAT = 111320.0


def _uuid_int(*parts: object) -> int:
    return uuid.uuid5(_EMULATOR_NAMESPACE, "|".join(str(p) for p in parts)).int


def _plate_for(index: int) -> str:
    digits = 1000 + (_uuid_int("plate", index) % 9000)
    return f"{_PLATE_RTO_PREFIX}{_PLATE_MARKER}{digits}"


def _offset_m(anchor: tuple[float, float], dx_m: float, dy_m: float) -> list[float]:
    """Anchor + (dx_m east, dy_m north) in degrees, rounded for stability.

    cos(lat) ~ 0.757 at this belt (lat ~ 22.99) — fixed factor so every
    host computes the identical position.
    """
    lon_step = dx_m / (_M_PER_DEG_LAT * 0.757)
    lat_step = dy_m / _M_PER_DEG_LAT
    return [round(anchor[0] + lon_step, 6), round(anchor[1] + lat_step, 6)]


@dataclass(frozen=True)
class CauseFrame:
    frame: int
    t_sec: float
    kind: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame": self.frame,
            "t_sec": round(self.t_sec, 3),
            "kind": self.kind,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class CauseTape:
    use_case_id: str
    seed: int
    t0_anchor_iso: str
    clock_scale: float
    movement_id: str
    frames: tuple[CauseFrame, ...]

    def to_json(self) -> str:
        """Canonical, byte-stable serialization (tape file + JSONB ledger)."""
        return json.dumps(
            {
                "tape_version": 1,
                "use_case_id": self.use_case_id,
                "seed": self.seed,
                "t0_anchor": self.t0_anchor_iso,
                "clock_scale": self.clock_scale,
                "movement_id": self.movement_id,
                "frames": [f.to_dict() for f in self.frames],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def from_dict(raw: dict) -> CauseTape:
        frames = tuple(
            CauseFrame(
                frame=int(f["frame"]),
                t_sec=float(f["t_sec"]),
                kind=str(f["kind"]),
                payload=f["payload"],
            )
            for f in raw["frames"]
        )
        return CauseTape(
            use_case_id=raw["use_case_id"],
            seed=int(raw["seed"]),
            t0_anchor_iso=raw["t0_anchor"],
            clock_scale=float(raw["clock_scale"]),
            movement_id=raw["movement_id"],
            frames=frames,
        )


# ── deterministic cause models ─────────────────────────────────────────────

# Time-of-day congestion base by local hour (IST) — the exogenous demand
# curve every scenario rides; linear interpolation between anchors.
_TOD_CURVE: tuple[tuple[int, float], ...] = (
    (0, 0.15), (5, 0.18), (7, 0.45), (9, 0.55), (12, 0.50),
    (16, 0.52), (18, 0.70), (21, 0.45), (24, 0.20),
)

_IST_OFFSET_SEC = 5 * 3600 + 30 * 60


def _tod_base(local_hour: float) -> float:
    h = local_hour % 24.0
    for (h0, v0), (h1, v1) in zip(_TOD_CURVE, _TOD_CURVE[1:]):
        if h0 <= h <= h1:
            frac = 0.0 if h1 == h0 else (h - h0) / (h1 - h0)
            return v0 + (v1 - v0) * frac
    return _TOD_CURVE[-1][1]


def _piecewise(waypoints: tuple[tuple[float, float], ...], t: float) -> float:
    """Piecewise-linear interpolation over (t, value) waypoints."""
    if t <= waypoints[0][0]:
        return waypoints[0][1]
    if t >= waypoints[-1][0]:
        return waypoints[-1][1]
    for (t0, v0), (t1, v1) in zip(waypoints, waypoints[1:]):
        if t0 <= t <= t1:
            frac = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return v0 + (v1 - v0) * frac
    return waypoints[-1][1]


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _active(fe: dict, t: float) -> bool:
    return float(fe["start_sec"]) <= t < float(fe["start_sec"]) + float(fe["duration_sec"])


def _weather_severity_at(profile: UseCaseProfile, t: float) -> float:
    if profile.weather_steady_severity <= 0.0:
        return 0.02  # clear-sky baseline noise floor
    onset = max(profile.weather_onset_sec, 1.0)
    return _clamp01(profile.weather_steady_severity * min(1.0, t / onset))


def _ambulance_projection_at(
    profile: UseCaseProfile, events: list[dict], t: float
) -> float:
    """Delay projection: base while no EV window, decaying to cleared while
    a green-corridor window runs (the corridor clearing in front of the
    ambulance)."""
    if profile.ambulance_base_sec <= 0.0:
        return 0.0
    ev = [e for e in events if e.get("type") == "emergency_vehicle" and _active(e, t)]
    if not ev:
        # between/around windows the projection sits at the degraded base
        return profile.ambulance_base_sec
    e0 = min(ev, key=lambda e: float(e["start_sec"]))
    frac = (t - float(e0["start_sec"])) / max(float(e0["duration_sec"]), 1.0)
    return profile.ambulance_base_sec + (
        profile.ambulance_cleared_sec - profile.ambulance_base_sec
    ) * min(1.0, max(0.0, frac))


def _camera_anchor(
    route: list[int],
    camera_index: int,
    segment_centroids: dict[int, tuple[float, float]] | None,
) -> tuple[float, float]:
    """Deterministic anchor for camera N: the centroid of the route segment
    at a fixed stride, or the fallback belt anchor without centroids."""
    if segment_centroids and route:
        positions = [
            segment_centroids[s]
            for s in route
            if s in segment_centroids
        ]
        if positions:
            pos = positions[camera_index % len(positions)]
            return (float(pos[0]), float(pos[1]))
    drift = camera_index * 0.004
    return (_FALLBACK_ANCHOR[0] + drift, _FALLBACK_ANCHOR[1] - drift / 2.0)


def _build_anpr_frames(
    use_case_id: str,
    seed: int,
    profile: UseCaseProfile,
    route: list[int],
    horizon: float,
    segment_centroids: dict[int, tuple[float, float]] | None,
) -> list[CauseFrame]:
    """Generate deterministic camera reads (kind 'anpr_read', contract C2).

    One read per (camera, plate) pair — read_id is uuid5 over (use_case,
    seed, camera, plate), so replays are idempotent against the oracle's
    sightings upsert.  The stolen and earmarked demo plates are always in
    the read set when their profile knobs are on, carrying
    matched_plate_hit labels; the stolen vehicle reads above segment speed
    so the deterministic R_SPD_SEGMENT rule can fire on it in the demo.
    """
    n_cameras = int(getattr(profile, "anpr_cameras", 0) or 0)
    if n_cameras <= 0:
        return []

    n_bg = int(getattr(profile, "anpr_background_vehicles", 0) or 0)
    vehicles: list[tuple[str, str | None, str]] = [
        (_plate_for(i), None, "car") for i in range(n_bg)
    ]
    if getattr(profile, "anpr_stolen_plate_hit", False):
        vehicles.append((_STOLEN_LABEL, "stolen_vehicle", "car"))
    if getattr(profile, "anpr_earmarked_plate_hit", False):
        vehicles.append((_EARMARKED_LABEL, "earmarked_plate", "auto_rickshaw"))

    frames: list[CauseFrame] = []
    for cam_i in range(n_cameras):
        camera_id = f"EM-CAM-{cam_i + 1:02d}"
        anchor = _camera_anchor(route, cam_i, segment_centroids)
        for plate, hit, vclass in vehicles:
            u = _uuid_int(use_case_id, seed, camera_id, plate)
            t_sec = float((u % max(int(horizon) - 30, 60)) + 15)
            # deterministic placement/drift around the camera's anchor
            dx = float((u >> 16) % 700) - 350.0        # +-350 m
            dy = float((u >> 32) % 700) - 350.0
            if hit == "stolen_vehicle":
                speed = 92.0 + float((u >> 8) % 24)    # above 40-60 limits
                heading = 78.0
            elif hit == "earmarked_plate":
                speed = 38.0 + float((u >> 8) % 10)
                heading = 168.0
            else:
                speed = 32.0 + float((u >> 8) % 38)    # 32-69 km/h
                heading = float((u >> 40) % 360)
            frames.append(CauseFrame(
                frame=0,
                t_sec=round(t_sec, 3),
                kind="anpr_read",
                payload={
                    "camera_id": camera_id,
                    "plate": plate,
                    "position": _offset_m(anchor, dx, dy),
                    "speed_kmh": round(speed, 1),
                    "heading": round(heading, 1),
                    "vehicle_class": vclass,
                    "read_id": anpr_read_uuid(use_case_id, seed, camera_id, plate),
                    "matched_plate_hit": hit,
                },
            ))
    return frames


def _build_agency_frames(
    use_case_id: str,
    seed: int,
    profile: UseCaseProfile,
    route: list[int],
    horizon: float,
    segment_centroids: dict[int, tuple[float, float]] | None,
) -> list[CauseFrame]:
    """Generate deterministic agency-unit patrol frames (contract C2).

    Jan Rakshak street-patrol units drifting along the belt — the emulated
    counterpart of the agency actors the pursuit flow dispatches.  Every
    unit id is self-labelled EM (emulated).
    """
    n_units = int(getattr(profile, "agency_units", 0) or 0)
    if n_units <= 0:
        return []
    cadence = 300.0
    frames: list[CauseFrame] = []
    for u_i in range(n_units):
        unit_id = f"JR-EM-{u_i + 1:02d}"
        anchor = _camera_anchor(route, u_i, segment_centroids)
        t = 0.0
        while t <= horizon:
            u = _uuid_int(use_case_id, seed, unit_id, int(t))
            dx = float((u >> 12) % 900) - 450.0
            dy = float((u >> 28) % 900) - 450.0
            frames.append(CauseFrame(
                frame=0,
                t_sec=round(t, 3),
                kind="agency_unit",
                payload={
                    "unit_id": unit_id,
                    "agency": "jan_rakshak",
                    "position": _offset_m(anchor, dx, dy),
                    "status": "patrolling",
                },
            ))
            t += cadence
    return frames


def build_tape(
    run: ResolvedRun,
    use_case: UseCase,
    profile: UseCaseProfile,
    segment_centroids: dict[int, tuple[float, float]] | None = None,
) -> CauseTape:
    """Build the ordered cause tape.  Pure: same inputs → identical bytes.

    ``segment_centroids`` (optional, from the store seam) anchor the
    watchlist generators to the REAL route geometry; without it a fixed
    fallback anchor is used — deterministic on every host either way, and
    replays re-post the ledgered tape verbatim regardless.
    """
    cfg = use_case.config
    horizon = use_case.horizon_sec
    tick = run.spec.tick_sec
    seed = run.seed
    rng = SeededRng(derive_seed("emulator:tape", run.spec.use_case_id, seed))

    route = list(cfg["route_segment_ids"])
    movement_id = movement_uuid(run.spec.use_case_id, seed)

    # ── scheduled_event causes ────────────────────────────────────────────
    scheduled: list[dict] = []
    for fe in cfg["forced_events"]:
        scheduled.append({**fe, "source": "use_case_config"})
    for fe in profile.extra_events:
        scheduled.append({**fe, "source": "profile"})
    scheduled.sort(key=lambda e: (float(e["start_sec"]), int(e["segment_id"])))

    event_segments = sorted({int(e["segment_id"]) for e in scheduled})
    all_segments = sorted(set(route) | set(event_segments))

    local_t0 = run.t0_anchor.timestamp() + _IST_OFFSET_SEC

    def _local_hour(t: float) -> float:
        return ((local_t0 + t) / 3600.0) % 24.0

    frames: list[CauseFrame] = []

    # ── event_config (t=0): the config's multipliers VERBATIM + the
    # additive ambulance_rate_multiplier (sixth override knob) ─────────────
    frames.append(CauseFrame(
        frame=0, t_sec=0.0, kind="event_config",
        payload={
            "event_overrides": dict(cfg["event_overrides"]),
            "ambulance_rate_multiplier": (
                1.0 + len([e for e in scheduled if e["type"] == "emergency_vehicle"])
            ),
            "note": (
                "event_overrides verbatim from UseCaseLibrary; "
                "ambulance_rate_multiplier is the additive sixth knob "
                "(deterministic counterpart of the random ambulance Poisson)"
            ),
        },
    ))

    # ── scheduled_event frames (verbatim causes) ──────────────────────────
    for e in scheduled:
        frames.append(CauseFrame(
            frame=0, t_sec=float(e["start_sec"]), kind="scheduled_event",
            payload=dict(e),
        ))

    # ── weather_ramp (one envelope frame) ─────────────────────────────────
    weather_cfg = dict(cfg["weather"])
    if profile.weather_steady_severity > 0.0:
        waypoints = (
            (0.0, 0.02),
            (profile.weather_onset_sec, profile.weather_steady_severity),
            (horizon, profile.weather_steady_severity),
        )
    else:
        waypoints = ((0.0, 0.02), (horizon, 0.03))
    frames.append(CauseFrame(
        frame=0, t_sec=0.0, kind="weather_ramp",
        payload={
            "monsoon": bool(weather_cfg.get("monsoon", False)),
            "rain_intensity_mm_h": float(weather_cfg.get("rain_intensity_mm_h", 0.0)),
            "weather_code": profile.weather_code,
            "severity_waypoints": [[round(t, 1), round(v, 3)] for t, v in waypoints],
        },
    ))

    # ── crowd_script (one envelope frame) ─────────────────────────────────
    frames.append(CauseFrame(
        frame=0, t_sec=0.0, kind="crowd_script",
        payload={
            "crowd_class": profile.crowd_class,
            "score_waypoints": [
                [round(t, 1), round(v, 3)] for t, v in profile.crowd_waypoints
            ],
        },
    ))

    # ── event_row frames (movement lifecycle in corridor-store) ───────────
    frames.append(CauseFrame(
        frame=0, t_sec=0.0, kind="event_row",
        payload={
            "action": "create",
            "movement_id": movement_id,
            "vvip_class": cfg["vvip_class"],
            "event_kind": profile.event_kind,
            "route_segment_ids": route,
            "planned_start_offset_sec": 0.0,
        },
    ))
    frames.append(CauseFrame(
        frame=0, t_sec=horizon, kind="event_row",
        payload={"action": "complete", "movement_id": movement_id},
    ))

    # ── anpr_read frames (watchlist generators, contract C2) ──────────────
    frames.extend(_build_anpr_frames(
        run.spec.use_case_id, seed, profile, route, horizon, segment_centroids,
    ))

    # ── agency_unit frames (patrolling units, contract C2) ────────────────
    frames.extend(_build_agency_frames(
        run.spec.use_case_id, seed, profile, route, horizon, segment_centroids,
    ))

    # ── inputs_doc frames (the exact engine_loop reader shape) ────────────
    t = 0.0
    while t <= horizon + 1e-9:
        cong = _tod_base(_local_hour(t))
        for e in scheduled:
            if not _active(e, t):
                continue
            et = e["type"]
            if et in ("rally_spillover", "market_spillover"):
                cong += min(0.35, float(e.get("extra_demand_veh_h", 0.0)) / 3000.0)
            elif et in ("vehicle_breakdown", "waterlogging", "signal_fault") and int(
                e["segment_id"]
            ) in route:
                cong += 0.12
            elif et == "emergency_vehicle" and int(e["segment_id"]) in route:
                cong += 0.05
        cong += rng.uniform(-0.02, 0.02)

        wsv = _weather_severity_at(profile, t) + rng.uniform(-0.01, 0.01)
        crowd = _piecewise(profile.crowd_waypoints, t) + rng.uniform(-0.02, 0.02)

        anomaly = any(
            _active(e, t) and e["type"] in
            ("vehicle_breakdown", "waterlogging", "signal_fault")
            for e in scheduled
        )
        doc: dict[str, Any] = {
            "predicted_congestion_idx": round(_clamp01(cong), 4),
            "weather_severity": round(_clamp01(wsv), 4),
            "crowd_score": round(_clamp01(crowd), 4),
            "anomaly_active": anomaly,
            "event_active": t < horizon,
            "last_gps_age_sec": round(
                rng.uniform(10.0, 25.0) if wsv > 0.5 else rng.uniform(0.0, 8.0), 2
            ),
            "ambulance_delay_projection_sec": round(
                _ambulance_projection_at(profile, scheduled, t), 1
            ),
        }
        if profile.weather_code is not None and _clamp01(wsv) > 0.3:
            doc["weather"] = profile.weather_code

        frames.append(CauseFrame(frame=0, t_sec=round(t, 3), kind="inputs_doc", payload=doc))
        t += tick

    # ── order + number ────────────────────────────────────────────────────
    kind_order = {k: i for i, k in enumerate(CAUSE_KINDS)}
    frames.sort(key=lambda f: (f.t_sec, kind_order.get(f.kind, 99)))
    frames = [CauseFrame(i, f.t_sec, f.kind, f.payload) for i, f in enumerate(frames)]

    return CauseTape(
        use_case_id=run.spec.use_case_id,
        seed=seed,
        t0_anchor_iso=run.t0_anchor.isoformat(),
        clock_scale=run.spec.clock_scale,
        movement_id=movement_id,
        frames=tuple(frames),
    )


def wall_time(t0_iso: str, t_sec: float, clock_scale: float) -> float:
    """Absolute unix time at which a frame must fire: t0 + t/clock_scale."""
    from datetime import datetime

    t0 = datetime.fromisoformat(t0_iso)
    return t0.timestamp() + t_sec / clock_scale
