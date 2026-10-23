"""City fusion surface tests — contract C-C (WP-02).

Through the REAL FastAPI app over a canned-table fake pool (the
test_units_dispatch.py pattern — every SQL constant is answered by a
handler that applies the statement's own filters to in-memory tables):

    * GET /api/v1/cameras — derived and registry sources, 15-minute
      counters, the C6 key whitelist (no plate/frame/image key, ever).
    * GET /api/v1/city/entities — every kind, the exact CityEntity keys,
      kinds/bbox filters and their 422s, per-kind degradation (one dead
      source never blanks the city), plate masking, dispatches of closed
      events excluded, Valkey C-A / C-B frames tolerated and validated.
    * GET /api/v1/city/entity/{kind}/{id} — the vehicle → sightings →
      cameras → recommendation → pursuit chain, event / junction / unit /
      dispatch / camera / convoy dossiers, 404 / 422, the privacy rail for
      vehicles without an active earmark.
    * GET /api/v1/city/feed and /api/v1/events/{id}/audit — union of every
      source, newest first, stable ids, actor kinds, since/kinds filters,
      scoping, visible degradation, 404.
    * GET /api/v1/adaptive/overrides — contract shape + event resolution.
    * Review fixes: provenance (AMA / override / brain / signal rows of an
      EMULATED event are badged emulated everywhere, incl. the adaptive
      surfaces' ``emulated`` flag); the C6 lawful-read rail (a vehicle
      without an active earmark leaks no trail through any audit, camera
      or sighting path); C-B frames with a non-uuid event_id (200, never
      linked, kept as props.event_ref); wire-precision ``since``;
      numeric tie-break; honest actor kinds; the plan ledger narrative;
      the 503 stub over every city path.

An optional LIVE-SCHEMA section runs only when VVIP_TEST_DATABASE_URL
points at a SCRATCH clone (never the fleet DB): every new SQL constant is
PREPAREd inside a transaction that is rolled back, and the endpoints run
end to end over one connection whose outer transaction is rolled back.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from traffic_oracle.__main__ import create_app  # noqa: E402
from traffic_oracle.adaptive import surfacing  # noqa: E402
from traffic_oracle.city import cameras as cams_mod  # noqa: E402
from traffic_oracle.city import common  # noqa: E402
from traffic_oracle.city import dossier as dos  # noqa: E402
from traffic_oracle.city import entities as ent  # noqa: E402
from traffic_oracle.city import feed as feed_mod  # noqa: E402
from traffic_oracle.units import router as units_router  # noqa: E402

# ─── canned city ─────────────────────────────────────────────────────────────

NOW = datetime.now(timezone.utc)

E_PURSUIT = uuid.UUID("4508d86d-566b-438f-8414-b3d67fd7e6e4")
E_DONE = uuid.UUID("3ef08290-66d8-511b-a25a-3d2967c79985")
E_ZONE = uuid.UUID("77777777-7777-7777-7777-777777777777")
V_STOLEN = uuid.UUID("1b94e9d2-7e11-4174-a735-7dd3be3189cf")
V_WATCH = uuid.UUID("22222222-2222-2222-2222-222222222222")
V_PLAIN = uuid.UUID("33333333-3333-3333-3333-333333333333")
R_PENDING = uuid.UUID("c20da1e8-40a4-4099-b54c-e7bcc3ee154c")
R_APPROVED = uuid.UUID("e91d4591-390a-4c55-bcb0-ffd10c769c20")
R_PLAIN = uuid.UUID("44444444-4444-4444-4444-444444444444")
D_LIVE = uuid.UUID("55555555-5555-5555-5555-555555555555")
D_STALE = uuid.UUID("cfb821b9-ee89-4f1b-90a8-3d6ca9a71f39")


def _sid(n: int) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-0000-0000-{n:012d}")


def _ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


SEGMENT_POINTS = {
    1012: (72.578, 23.030),
    1015: (72.575, 23.025),
    1005: (72.585, 23.020),
    1013: (72.596, 23.022),
}


def spine_marker(pts: list[tuple[float, float]]) -> tuple[float, float]:
    """The fake's _SQL_EVENT_SPINES marker: ST_ClosestPoint(spine, centroid)
    with each fake segment drawn as its one point — the point of the spine
    nearest the spine's centroid, never the centroid itself."""
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    return min(pts, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)


def city_tables() -> dict[str, Any]:
    events = [
        {
            "event_id": E_PURSUIT,
            "event_kind": "vehicle_pursuit",
            "protection_class": "none",
            "crowd_class": "LOW",
            "title": "PURSUIT — stolen GJ27EM9001",
            "description": "",
            "status": "active",
            "movement_id": None,
            "planned_start": None,
            "planned_end": None,
            "actual_start": _ago(minutes=20),
            "actual_end": None,
            "metadata": json.dumps(
                {
                    "vehicle_id": str(V_STOLEN),
                    "recommendation_id": str(R_APPROVED),
                    "opened_by": "Duty Officer R. Mehta",
                }
            ),
            "created_at": _ago(minutes=30),
            "updated_at": _ago(minutes=20),
            "zone_lon": None,
            "zone_lat": None,
        },
        {
            "event_id": E_DONE,
            "event_kind": "procession",
            "protection_class": "none",
            "crowd_class": "HIGH",
            "title": "Rath Yatra Morning",
            "description": "",
            "status": "completed",
            "movement_id": E_DONE,
            "planned_start": _ago(days=3),
            "planned_end": _ago(days=3),
            "actual_start": None,
            "actual_end": None,
            "metadata": json.dumps({"source": "event-emulator", "scenario_id": "rath_yatra"}),
            "created_at": _ago(days=3),
            "updated_at": _ago(days=3),
            "zone_lon": None,
            "zone_lat": None,
        },
        {
            "event_id": E_ZONE,
            "event_kind": "work_zone",
            "protection_class": "none",
            "crowd_class": "LOW",
            "title": "Metro works",
            "description": "",
            "status": "planning",
            "movement_id": None,
            "planned_start": None,
            "planned_end": None,
            "actual_start": None,
            "actual_end": None,
            "metadata": json.dumps({}),
            "created_at": _ago(hours=2),
            "updated_at": _ago(hours=2),
            "zone_lon": 72.60,
            "zone_lat": 23.05,
        },
    ]
    routes = [
        {
            "event_id": E_PURSUIT,
            "route_rank": 1,
            "label": "old-city sweep belt",
            "segment_ids": [1012, 1015, 1005, 1013],
        },
        {"event_id": E_DONE, "route_rank": 1, "label": "spine", "segment_ids": [1005]},
        {
            "event_id": E_ZONE,
            "route_rank": 1,
            "label": "spine",
            "segment_ids": [9999],
        },  # unknown seg
    ]
    vehicles = [
        {"vehicle_id": V_STOLEN, "plate": "GJ27EM9001", "vehicle_class": "car"},
        {"vehicle_id": V_WATCH, "plate": "GJ01AB1234", "vehicle_class": "car"},
        {"vehicle_id": V_PLAIN, "plate": "GJ05CD5678", "vehicle_class": "car"},
    ]
    earmarks = [
        {
            "vehicle_id": V_STOLEN,
            "state": "flagged",
            "reason": "STOLEN — FIR 4401/2026",
            "lawful_basis": "stolen_vehicle_fir",
            "retention_until": NOW + timedelta(days=20),
            "earmarked_at": _ago(days=4),
            "earmarked_by": "Duty Officer",
            "cleared_at": None,
        },
        {
            "vehicle_id": V_WATCH,
            "state": "watch",
            "reason": "watch list",
            "lawful_basis": "mva_1988_s177",
            "retention_until": NOW + timedelta(days=2),
            "earmarked_at": _ago(days=1),
            "earmarked_by": "Duty Officer",
            "cleared_at": None,
        },
    ]
    sightings = [
        # stolen vehicle: 4 hops over 3 cameras (+1 future-dated read that must not count)
        {
            "sighting_id": _sid(1),
            "vehicle_id": V_STOLEN,
            "plate": "GJ27EM9001",
            "camera_id": "EM-CAM-03",
            "t": _ago(minutes=40),
            "lon": 72.5846,
            "lat": 23.0188,
            "speed_kmh": 92.0,
            "heading": 78.0,
            "vehicle_class": "car",
            "source": "emulated_anpr",
            "run_id": 20,
            "matched_plate_hit": "stolen_vehicle",
        },
        {
            "sighting_id": _sid(2),
            "vehicle_id": V_STOLEN,
            "plate": "GJ27EM9001",
            "camera_id": "EM-CAM-04",
            "t": _ago(minutes=30),
            "lon": 72.5964,
            "lat": 23.0228,
            "speed_kmh": 101.0,
            "heading": 78.0,
            "vehicle_class": "car",
            "source": "emulated_anpr",
            "run_id": 20,
            "matched_plate_hit": "stolen_vehicle",
        },
        {
            "sighting_id": _sid(3),
            "vehicle_id": V_STOLEN,
            "plate": "GJ27EM9001",
            "camera_id": "EM-CAM-01",
            "t": _ago(minutes=10),
            "lon": 72.5830,
            "lat": 23.0294,
            "speed_kmh": 95.0,
            "heading": 78.0,
            "vehicle_class": "car",
            "source": "emulated_anpr",
            "run_id": 20,
            "matched_plate_hit": "stolen_vehicle",
        },
        {
            "sighting_id": _sid(4),
            "vehicle_id": V_STOLEN,
            "plate": "GJ27EM9001",
            "camera_id": "EM-CAM-01",
            "t": _ago(minutes=5),
            "lon": 72.5825,
            "lat": 23.0290,
            "speed_kmh": 88.0,
            "heading": 80.0,
            "vehicle_class": "car",
            "source": "emulated_anpr",
            "run_id": 20,
            "matched_plate_hit": "stolen_vehicle",
        },
        {
            "sighting_id": _sid(9),
            "vehicle_id": V_STOLEN,
            "plate": "GJ27EM9001",
            "camera_id": "EM-CAM-01",
            "t": NOW + timedelta(minutes=30),
            "lon": 72.5825,
            "lat": 23.0290,
            "speed_kmh": 80.0,
            "heading": 80.0,
            "vehicle_class": "car",
            "source": "emulated_anpr",
            "run_id": 20,
            "matched_plate_hit": "stolen_vehicle",
        },
        # watch vehicle: 1 read, no hit
        {
            "sighting_id": _sid(5),
            "vehicle_id": V_WATCH,
            "plate": "GJ01AB1234",
            "camera_id": "EM-CAM-02",
            "t": _ago(minutes=3),
            "lon": 72.5730,
            "lat": 23.0245,
            "speed_kmh": 40.0,
            "heading": 10.0,
            "vehicle_class": "car",
            "source": "emulated_anpr",
            "run_id": 20,
            "matched_plate_hit": None,
        },
        # plain (not earmarked) vehicle: one read that is evidence of R_PLAIN, one that is not
        {
            "sighting_id": _sid(6),
            "vehicle_id": V_PLAIN,
            "plate": "GJ05CD5678",
            "camera_id": "EM-CAM-02",
            "t": _ago(minutes=50),
            "lon": 72.5731,
            "lat": 23.0246,
            "speed_kmh": 70.0,
            "heading": 0.0,
            "vehicle_class": "car",
            "source": "emulated_anpr",
            "run_id": 20,
            "matched_plate_hit": None,
        },
        {
            "sighting_id": _sid(7),
            "vehicle_id": V_PLAIN,
            "plate": "GJ05CD5678",
            "camera_id": "EM-CAM-03",
            "t": _ago(minutes=45),
            "lon": 72.5846,
            "lat": 23.0188,
            "speed_kmh": 30.0,
            "heading": 0.0,
            "vehicle_class": "car",
            "source": "emulated_anpr",
            "run_id": 20,
            "matched_plate_hit": None,
        },
    ]
    recs = [
        {
            "recommendation_id": R_PENDING,
            "sighting_id": _sid(1),
            "vehicle_id": V_STOLEN,
            "rule_id": "R_SPD_SEGMENT",
            "recommendation": "e-challan recommended: GJ27EM9001 at 92 km/h",
            "evidence": json.dumps(
                {
                    "speed_kmh": 92.0,
                    "speed_limit_kmh": 40.0,
                    "road_name": "Relief Road",
                    "segment_id": 1005,
                    "plate": "GJ27EM9001",
                    "source": "emulated_anpr",
                }
            ),
            "status": "pending",
            "decided_by": None,
            "decided_at": None,
            "created_at": _ago(minutes=39),
            "audit_tail": json.dumps(
                [
                    {
                        "actor": "traffic-oracle",
                        "action": "created:pending",
                        "at": _ago(minutes=39).isoformat(),
                        "note": "deterministic rule evaluation",
                    }
                ]
            ),
        },
        {
            "recommendation_id": R_APPROVED,
            "sighting_id": _sid(3),
            "vehicle_id": V_STOLEN,
            "rule_id": "R_SPD_SEGMENT",
            "recommendation": "e-challan recommended: GJ27EM9001 at 95 km/h",
            "evidence": json.dumps(
                {
                    "speed_kmh": 95.0,
                    "speed_limit_kmh": 40.0,
                    "road_name": "Gandhi Road",
                    "segment_id": 1012,
                    "plate": "GJ27EM9001",
                    "source": "emulated_anpr",
                }
            ),
            "status": "approved",
            "decided_by": "Duty Officer R. Mehta",
            "decided_at": _ago(minutes=8),
            "created_at": _ago(minutes=9),
            "audit_tail": json.dumps(
                [
                    {
                        "actor": "traffic-oracle",
                        "action": "created:pending",
                        "at": _ago(minutes=9).isoformat(),
                        "note": "deterministic rule evaluation",
                    },
                    {
                        "actor": "Duty Officer R. Mehta",
                        "action": "decision:approved",
                        "at": _ago(minutes=8).isoformat(),
                        "note": "speed evidence reviewed",
                    },
                ]
            ),
        },
        {
            "recommendation_id": R_PLAIN,
            "sighting_id": _sid(6),
            "vehicle_id": V_PLAIN,
            "rule_id": "R_SPD_SEGMENT",
            "recommendation": "e-challan recommended: GJ05CD5678 at 70 km/h",
            "evidence": json.dumps(
                {"speed_kmh": 70.0, "speed_limit_kmh": 40.0, "plate": "GJ05CD5678"}
            ),
            "status": "pending",
            "decided_by": None,
            "decided_at": None,
            "created_at": _ago(minutes=49),
            "audit_tail": json.dumps(
                [
                    {
                        "actor": "traffic-oracle",
                        "action": "created:pending",
                        "at": _ago(minutes=49).isoformat(),
                        "note": "",
                    }
                ]
            ),
        },
    ]
    junctions = [
        {
            "junction_id": 1,
            "junction_type": "signal",
            "signal_control": True,
            "lon": 72.578,
            "lat": 23.035,
            "cycle_sec": 100,
            "phases": json.dumps(
                [
                    {"green_sec": 47, "movements": ["through"]},
                    {"green_sec": 38, "movements": ["cross"]},
                ]
            ),
            "offset_sec": 17,
            "program_source": "osm_assumed",
            "program_updated_at": _ago(days=4),
            "doc": json.dumps({"junction_id": 1, "name": "Lal Darwaja", "name_gu": "લાલ દરવાજા"}),
        },
        {
            "junction_id": 2,
            "junction_type": "roundabout",
            "signal_control": False,
            "lon": 72.5714,
            "lat": 23.0225,
            "cycle_sec": None,
            "phases": None,
            "offset_sec": None,
            "program_source": None,
            "program_updated_at": None,
            "doc": json.dumps({"junction_id": 2}),
        },
    ]
    dispatches = [
        {
            "dispatch_id": D_LIVE,
            "event_id": E_PURSUIT,
            "agency": "jan_rakshak",
            "unit_id": "JR-7",
            "waypoint_junction": 1,
            "status": "en_route",
            "requested_by": "Duty Officer",
            "decided_by": "Duty Officer",
            "approved_at": _ago(minutes=2),
            "created_at": _ago(minutes=3),
            "updated_at": _ago(minutes=1),
        },
        {
            "dispatch_id": D_STALE,
            "event_id": E_DONE,
            "agency": "jan_rakshak",
            "unit_id": "JR-EM-01",
            "waypoint_junction": 2,
            "status": "on_scene",
            "requested_by": "showcase",
            "decided_by": "Duty Officer",
            "approved_at": _ago(days=3),
            "created_at": _ago(days=3),
            "updated_at": _ago(days=3),
        },
    ]
    dispatch_events = [
        {
            "seq": 39,
            "dispatch_id": D_STALE,
            "from_status": None,
            "to_status": "requested",
            "actor": "showcase",
            "note": "spillover",
            "at": _ago(days=3),
        },
        {
            "seq": 50,
            "dispatch_id": D_LIVE,
            "from_status": None,
            "to_status": "requested",
            "actor": "Duty Officer",
            "note": "choke #1",
            "at": _ago(minutes=3),
        },
        {
            "seq": 51,
            "dispatch_id": D_LIVE,
            "from_status": "requested",
            "to_status": "approved",
            "actor": "Duty Officer",
            "note": None,
            "at": _ago(minutes=2),
        },
        {
            "seq": 52,
            "dispatch_id": D_LIVE,
            "from_status": "approved",
            "to_status": "en_route",
            "actor": "JR field lead",
            "note": None,
            "at": _ago(minutes=1),
        },
    ]
    situation_log = [
        {
            "log_id": 10,
            "movement_id": str(E_PURSUIT),
            "situation_class": "AMBER",
            "situation_code": "none:HEAVY",
            "severity_score": 7,
            "protection_class": "none",
            "observed_at": _ago(minutes=15),
            "transitioned": True,
        },
        {
            "log_id": 11,
            "movement_id": str(E_PURSUIT),
            "situation_class": "AMBER",
            "situation_code": "none:HEAVY",
            "severity_score": 7,
            "protection_class": "none",
            "observed_at": _ago(minutes=14),
            "transitioned": False,
        },
        {
            "log_id": 12,
            "movement_id": str(E_PURSUIT),
            "situation_class": "RED",
            "situation_code": "none:GRIDLOCK",
            "severity_score": 13,
            "protection_class": "none",
            "observed_at": _ago(minutes=4),
            "transitioned": True,
        },
    ]
    overrides = [
        {
            "override_id": 1,
            "movement_id": str(E_PURSUIT),
            "vvip_class": "X",
            "protection_class": "none",
            "situation_class": "AMBER",
            "params": json.dumps({"advance_closure_sec": 120}),
            "source": "baseline",
            "shadow_eval": "unavailable",
            "evidence": json.dumps(
                {
                    "shadow_eval_detail": "shadow_context_missing",
                    "fail_closed_reason": "shadow_eval_unavailable_fail_closed",
                    "floor_applied": False,
                }
            ),
            "published_at": _ago(minutes=15),
        },
        {
            "override_id": 2,
            "movement_id": str(E_PURSUIT),
            "vvip_class": "X",
            "protection_class": "none",
            "situation_class": "AMBER",
            "params": json.dumps({"advance_closure_sec": 120}),
            "source": "baseline",
            "shadow_eval": "unavailable",
            "evidence": json.dumps(
                {
                    "shadow_eval_detail": "shadow_context_missing",
                    "fail_closed_reason": "shadow_eval_unavailable_fail_closed",
                }
            ),
            "published_at": _ago(minutes=14),
        },
        {
            "override_id": 3,
            "movement_id": "default",
            "vvip_class": "X",
            "protection_class": "X",
            "situation_class": "GREEN",
            "params": json.dumps({"advance_closure_sec": 120}),
            "source": "baseline",
            "shadow_eval": "unavailable",
            "evidence": json.dumps({"shadow_eval_detail": "shadow_eval_not_required"}),
            "published_at": _ago(minutes=1),
        },
    ]
    status_log = [
        {
            "seq": 1,
            "event_id": E_PURSUIT,
            "from_status": None,
            "to_status": "planning",
            "actor": "Duty Officer R. Mehta",
            "note": "event created",
            "at": _ago(minutes=30),
        },
        {
            "seq": 2,
            "event_id": E_PURSUIT,
            "from_status": "planning",
            "to_status": "active",
            "actor": "Duty Officer A",
            "note": "units moving",
            "at": _ago(minutes=20),
        },
    ]
    history = [
        {
            "history_id": 7,
            "junction_id": 1,
            "event_id": E_PURSUIT,
            "kind": "snapshot",
            "actor": "Duty Officer A",
            "reason": "first calibrated write (city signal plan)",
            "captured_at": _ago(minutes=6),
        },
    ]
    anomalies = [
        {
            "anomaly_id": 900,
            "segment_id": 1015,
            "timestamp_utc": _ago(minutes=2),
            "anomaly_type": "speed_jump",
            "severity": "high",
            "details": json.dumps({"anomaly_flags": ["speed_jump"]}),
            "flagged_at": _ago(minutes=2),
        },
        {
            "anomaly_id": 899,
            "segment_id": 1015,
            "timestamp_utc": _ago(minutes=5),
            "anomaly_type": "stale_data",
            "severity": "low",
            "details": json.dumps({}),
            "flagged_at": _ago(minutes=5),
        },
        {
            "anomaly_id": 100,
            "segment_id": 1012,
            "timestamp_utc": _ago(hours=3),
            "anomaly_type": "stale_data",
            "severity": "low",
            "details": json.dumps({}),
            "flagged_at": _ago(hours=3),
        },
    ]
    adjacency = [
        {"from_segment_id": 1012, "to_segment_id": 1015, "via_junction_id": 1},
        {"from_segment_id": 1015, "to_segment_id": 1005, "via_junction_id": 2},
    ]
    return {
        "events": events,
        "routes": routes,
        "vehicles": vehicles,
        "earmarks": earmarks,
        "sightings": sightings,
        "recs": recs,
        "junctions": junctions,
        "dispatches": dispatches,
        "dispatch_events": dispatch_events,
        "situation_log": situation_log,
        "overrides": overrides,
        "status_log": status_log,
        "history": history,
        "anomalies": anomalies,
        "adjacency": adjacency,
        "relations": {"events.event_status_log", "corridor.signal_plan_log"},
        "registry": None,
        "plans": [],  # corridor.signal_plan_log (013)
    }


LIVE = ("planning", "approved", "active")
OPEN = ("requested", "approved", "en_route", "on_scene")


def _u(values) -> set[str]:
    return {str(v) for v in (values or [])}


class CityFakeConn:
    """Answers every city SQL constant from the canned tables."""

    def __init__(self, tables: dict[str, Any]) -> None:
        self.t = tables
        self.calls: list[str] = []
        self.fail: set[str] = set()  # SQL constants that raise (degradation tests)
        h: dict[str, Any] = {}
        # cameras
        h[cams_mod._SQL_CAMERAS_DERIVED] = self._cameras_derived
        h[cams_mod._SQL_CAMERA_REGISTRY] = self._camera_registry
        h[cams_mod._SQL_CAMERAS_UNREGISTERED] = self._cameras_unregistered
        h[cams_mod._SQL_CAMERA_STATS] = self._camera_stats
        # snapshot
        h[ent._SQL_LIVE_EVENTS] = lambda: [e for e in self.t["events"] if e["status"] in LIVE]
        h[ent._SQL_EVENT_SPINES] = self._spines
        h[ent._SQL_SUBJECT_BANDS] = self._bands
        h[ent._SQL_EARMARKED_VEHICLES] = self._earmarked
        h[ent._SQL_RECENT_SIGHTINGS] = self._recent_sightings
        h[ent._SQL_JUNCTIONS] = lambda: list(self.t["junctions"])
        h[ent._SQL_LIVE_DISPATCHES] = self._live_dispatches
        h[ent._SQL_OPEN_RECOMMENDATIONS] = lambda: self._recs(statuses=("pending", "approved"))
        h[ent._SQL_RECENT_ANOMALIES] = self._recent_anomalies
        # units projection
        h[units_router._SQL_ACTIVE_DISPATCHES] = self._active_dispatches
        h[units_router._SQL_EVENT_SPINE_START] = self._spine_start
        h[units_router._SQL_JUNCTION_ADJACENT_SEGMENT] = self._adjacent_segment
        h[units_router._SQL_SEGMENT_POINTS] = self._segment_points
        h[units_router._SQL_JUNCTION_GEOM] = self._junction_geom
        # the graph search runs on the HELD connection (no nested acquire)
        h[units_router._SQL_SHORTEST_PATH] = lambda source, target: [
            {"seq": 1, "node": source, "edge": 1, "cost": 0.0, "agg_cost": 0.0},
            {"seq": 2, "node": target, "edge": -1, "cost": 0.0, "agg_cost": 1.0},
        ]
        # common
        h[common._SQL_SUBJECT_EVENTS] = self._subject_events
        # dossier
        h[dos._SQL_DOSSIER_EVENTS] = lambda ids: [
            e for e in self.t["events"] if str(e["event_id"]) in _u(ids)
        ]
        h[dos._SQL_DOSSIER_EVENTS_CITING] = self._events_citing
        h[dos._SQL_DOSSIER_EVENTS_FOR_MOVEMENT] = lambda mid: [
            {"event_id": e["event_id"]}
            for e in self.t["events"]
            if str(e["event_id"]) == str(mid) or str(e["movement_id"]) == str(mid)
        ]
        h[dos._SQL_DOSSIER_VEHICLES] = self._dossier_vehicles
        h[dos._SQL_DOSSIER_SIGHTINGS] = lambda ids: sorted(
            [
                s
                for s in self.t["sightings"]
                if str(s["sighting_id"]) in _u(ids) and self._lawful(s)
            ],
            key=lambda s: s["t"],
            reverse=True,
        )
        h[dos._SQL_DOSSIER_CAMERA_HITS] = lambda cid: sorted(
            [
                s
                for s in self.t["sightings"]
                if s["camera_id"] == cid
                and s["matched_plate_hit"]
                and s["t"] <= datetime.now(timezone.utc)
                and self._lawful(s)
            ],
            key=lambda s: s["t"],
            reverse=True,
        )[:10]
        h[dos._SQL_DOSSIER_RECOMMENDATIONS] = lambda ids, vids, sids: [
            r
            for r in self._recs()
            if str(r["recommendation_id"]) in _u(ids)
            or str(r["vehicle_id"]) in _u(vids)
            or str(r["sighting_id"]) in _u(sids)
        ]
        h[dos._SQL_DOSSIER_JUNCTIONS] = lambda ids: [
            j for j in self.t["junctions"] if j["junction_id"] in set(ids)
        ]
        h[dos._SQL_DOSSIER_DISPATCHES] = self._dossier_dispatches
        h[dos._SQL_DOSSIER_SEGMENTS] = lambda ids: [
            {
                "segment_id": s,
                "road_name": f"Road {s}",
                "road_class": "primary",
                "lanes": 2,
                "speed_limit_kmh": 40,
                "oneway": False,
                "lon": SEGMENT_POINTS[s][0],
                "lat": SEGMENT_POINTS[s][1],
                "length_m": 420.0,
            }
            for s in ids
            if s in SEGMENT_POINTS
        ]
        h[dos._SQL_SEGMENT_JUNCTIONS] = lambda sid: [
            {"junction_id": j}
            for j in sorted(
                {
                    a["via_junction_id"]
                    for a in self.t["adjacency"]
                    if sid in (a["from_segment_id"], a["to_segment_id"])
                }
            )
        ]
        h[dos._SQL_JUNCTION_SEGMENTS] = lambda jid: [
            {"segment_id": s}
            for s in sorted(
                {
                    x
                    for a in self.t["adjacency"]
                    if a["via_junction_id"] == jid
                    for x in (a["from_segment_id"], a["to_segment_id"])
                }
            )
        ]
        h[dos._SQL_SEGMENT_EVENTS] = lambda sid: [
            {"event_id": r["event_id"]}
            for r in self.t["routes"]
            if sid in r["segment_ids"] and self._event(r["event_id"])["status"] in LIVE
        ]
        h[dos._SQL_JUNCTION_SIGNAL_EVENTS] = lambda jid: [
            {"event_id": x["event_id"]}
            for x in self.t["history"]
            if x["junction_id"] == jid and x["event_id"]
        ]
        h[dos._SQL_EVENT_SIGNAL_JUNCTIONS] = lambda eid: [
            {"junction_id": x["junction_id"]}
            for x in self.t["history"]
            if str(x["event_id"]) == str(eid)
        ]
        h[dos._SQL_DOSSIER_ANOMALY] = lambda aid: [
            self._anomaly_row(a) for a in self.t["anomalies"] if a["anomaly_id"] == aid
        ]
        h[dos._SQL_SEGMENT_ANOMALIES] = lambda sid: [
            self._anomaly_row(a)
            for a in self.t["anomalies"]
            if a["segment_id"] == sid
            and a["timestamp_utc"] > datetime.now(timezone.utc) - timedelta(minutes=15)
        ]
        # feed
        h[feed_mod._SQL_FEED_EVENT_LOG] = self._feed_event_log
        h[feed_mod._SQL_FEED_EVENT_CREATED] = self._feed_event_created
        h[feed_mod._SQL_FEED_ENFORCEMENT] = self._feed_enforcement
        h[feed_mod._SQL_FEED_ANPR] = self._feed_anpr
        h[feed_mod._SQL_FEED_DISPATCH] = self._feed_dispatch
        h[feed_mod._SQL_FEED_SIGNALS] = self._feed_signals
        h[feed_mod._SQL_FEED_SIGNAL_PLANS] = self._feed_signal_plans
        h[feed_mod._SQL_FEED_SITUATIONS] = self._feed_situations
        h[feed_mod._SQL_FEED_OVERRIDES] = self._feed_overrides
        h[feed_mod._SQL_AUDIT_EVENT] = lambda eid: [
            e for e in self.t["events"] if str(e["event_id"]) == str(eid)
        ]
        h[feed_mod._SQL_AUDIT_EVENT_DISPATCHES] = lambda eid: [
            {"dispatch_id": d["dispatch_id"]}
            for d in self.t["dispatches"]
            if str(d["event_id"]) == str(eid)
        ]
        # WP-19: the 018 (route supersede) and 015 (display geometry)
        # variants every reader switches to once its column probe says so,
        # and the vehicle reads' provenance aggregate (no individual read)
        h[ent._SQL_EVENT_SPINES_CURRENT] = lambda ids: [
            r for r in self._spines(ids) if r.get("superseded_at") is None
        ]
        h[ent._SQL_EVENT_SPINE_HISTORY] = lambda ids: [
            r for r in self._spines(ids) if r.get("superseded_at") is not None
        ]
        h[ent._SQL_RECENT_ANOMALIES_DISPLAY] = self._recent_anomalies
        h[units_router._SQL_EVENT_SPINE_START_CURRENT] = lambda eid: [
            {"segment_ids": r["segment_ids"]}
            for r in self.t["routes"]
            if str(r["event_id"]) == str(eid) and r.get("superseded_at") is None
        ][:1]
        h[units_router._SQL_SEGMENT_POINTS_DISPLAY] = self._segment_points
        h[dos._SQL_DOSSIER_SEGMENTS_DISPLAY] = lambda ids: [
            {**row, "has_display_geom": row["segment_id"] in self.t.get("display_geom", set())}
            for row in self.handlers[dos._SQL_DOSSIER_SEGMENTS](ids)
        ]
        h[dos._SQL_SEGMENT_EVENTS_CURRENT] = lambda sid: [
            {"event_id": r["event_id"]}
            for r in self.t["routes"]
            if sid in r["segment_ids"]
            and r.get("superseded_at") is None
            and self._event(r["event_id"])["status"] in LIVE
        ]
        h[dos._SQL_DOSSIER_ANOMALY_DISPLAY] = h[dos._SQL_DOSSIER_ANOMALY]
        h[dos._SQL_SEGMENT_ANOMALIES_DISPLAY] = h[dos._SQL_SEGMENT_ANOMALIES]
        h[dos._SQL_VEHICLE_READS_PROVENANCE] = self._reads_provenance
        # adaptive surfacing
        h[surfacing._SQL_SITUATION_LOG] = lambda subject, limit: sorted(
            [r for r in self.t["situation_log"] if subject is None or r["movement_id"] == subject],
            key=lambda r: r["observed_at"],
            reverse=True,
        )[:limit]
        h[surfacing._SQL_PARAM_OVERRIDES] = lambda subject, limit: sorted(
            [o for o in self.t["overrides"] if subject is None or o["movement_id"] == subject],
            key=lambda o: o["published_at"],
            reverse=True,
        )[:limit]
        self.handlers = h

    # -- driver ---------------------------------------------------------
    async def fetch(self, sql: str, *args: Any) -> list:
        self.calls.append(sql)
        if sql in self.fail:
            raise RuntimeError("source down (test)")
        handler = self.handlers.get(sql)
        if handler is None:
            raise AssertionError("unexpected SQL: " + sql[:90])
        return list(handler(*args))

    async def fetchrow(self, sql: str, *args: Any):
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any):
        self.calls.append(sql)
        if sql == common._SQL_REGCLASS:
            return args[0] in self.t["relations"]
        if sql == common._SQL_COLUMN:
            return (args[0], args[1]) in self.t.get("columns", set())
        raise AssertionError("unexpected fetchval: " + sql[:90])

    class _Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    def transaction(self) -> "CityFakeConn._Tx":
        return CityFakeConn._Tx()

    def _lawful(self, s: dict) -> bool:
        """common.SQL_LAWFUL_READ in Python: the vehicle holds an ACTIVE
        earmark, or the read is the evidence of a recommendation."""
        now = datetime.now(timezone.utc)
        if any(
            e["vehicle_id"] == s["vehicle_id"]
            and e["cleared_at"] is None
            and e["retention_until"] > now
            for e in self.t["earmarks"]
        ):
            return True
        return any(r["sighting_id"] == s["sighting_id"] for r in self.t["recs"])

    # -- helpers --------------------------------------------------------
    def _event(self, eid) -> dict:
        return next(e for e in self.t["events"] if str(e["event_id"]) == str(eid))

    def _cameras_derived(self) -> list[dict]:
        out: dict[str, dict] = {}
        for s in self.t["sightings"]:
            c = out.setdefault(
                s["camera_id"], {"camera_id": s["camera_id"], "pts": [], "any_emulated": False}
            )
            c["pts"].append((s["lon"], s["lat"]))
            c["any_emulated"] |= s["source"] == "emulated_anpr"
        rows = []
        for cid in sorted(out):
            pts = out[cid]["pts"]
            rows.append(
                {
                    "camera_id": cid,
                    "lon": sum(p[0] for p in pts) / len(pts),
                    "lat": sum(p[1] for p in pts) / len(pts),
                    "any_emulated": out[cid]["any_emulated"],
                }
            )
        return rows

    def _camera_registry(self) -> list[dict]:
        return [{"doc": json.dumps(d)} for d in self.t["registry"] or []]

    def _cameras_unregistered(self, known) -> list[dict]:
        day = datetime.now(timezone.utc) - timedelta(hours=24)
        return [
            r
            for r in self._cameras_derived()
            if r["camera_id"] not in set(known)
            and any(s["camera_id"] == r["camera_id"] and s["t"] > day for s in self.t["sightings"])
        ]

    def _camera_stats(self, ids) -> list[dict]:
        now = datetime.now(timezone.utc)
        rows = []
        for cid in ids:
            mine = [s for s in self.t["sightings"] if s["camera_id"] == cid and s["t"] <= now]
            window = [s for s in mine if s["t"] > now - timedelta(minutes=15)]
            rows.append(
                {
                    "camera_id": cid,
                    "reads_15m": len(window),
                    "hits_15m": sum(1 for s in window if s["matched_plate_hit"]),
                    "last_read_t": max((s["t"] for s in mine), default=None),
                }
            )
        return rows

    def _spines(self, ids) -> list[dict]:
        rows = []
        for r in self.t["routes"]:
            if str(r["event_id"]) not in _u(ids):
                continue
            pts = [SEGMENT_POINTS[s] for s in r["segment_ids"] if s in SEGMENT_POINTS]
            lon, lat = spine_marker(pts) if pts else (None, None)
            rows.append({**r, "lon": lon, "lat": lat})
        return rows

    def _bands(self, keys) -> list[dict]:
        out = {}
        for row in sorted(self.t["situation_log"], key=lambda r: r["observed_at"]):
            if row["movement_id"] in set(keys):
                out[row["movement_id"]] = row
        return list(out.values())

    def _earmarked(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        rows = []
        for e in self.t["earmarks"]:
            if e["cleared_at"] is None and e["retention_until"] > now:
                v = next(v for v in self.t["vehicles"] if v["vehicle_id"] == e["vehicle_id"])
                rows.append({**v, **e})
        return rows

    def _recent_sightings(self, ids, limit) -> list[dict]:
        now = datetime.now(timezone.utc)
        rows = []
        for vid in ids:
            mine = sorted(
                [
                    s
                    for s in self.t["sightings"]
                    if str(s["vehicle_id"]) == str(vid) and s["t"] <= now
                ],
                key=lambda s: s["t"],
                reverse=True,
            )[:limit]
            rows.extend(mine)
        return rows

    def _dispatch_row(self, d: dict) -> dict:
        ev = self._event(d["event_id"])
        j = next(
            (j for j in self.t["junctions"] if j["junction_id"] == d["waypoint_junction"]), None
        )
        return {
            **d,
            "lon": j["lon"] if j else None,
            "lat": j["lat"] if j else None,
            "event_title": ev["title"],
            "event_metadata": ev["metadata"],
            "event_status": ev["status"],
        }

    def _live_dispatches(self) -> list[dict]:
        return [
            self._dispatch_row(d)
            for d in self.t["dispatches"]
            if d["status"] in OPEN and self._event(d["event_id"])["status"] in LIVE
        ]

    def _dossier_dispatches(self, ids, eids, uids, jids) -> list[dict]:
        rows = [
            self._dispatch_row(d)
            for d in self.t["dispatches"]
            if str(d["dispatch_id"]) in _u(ids)
            or str(d["event_id"]) in _u(eids)
            or d["unit_id"] in set(uids)
            or d["waypoint_junction"] in set(jids)
        ]
        return sorted(rows, key=lambda d: d["created_at"], reverse=True)

    def _active_dispatches(self, statuses, limit) -> list[dict]:
        rows = [
            d
            for d in self.t["dispatches"]
            if d["status"] in statuses and self._event(d["event_id"])["status"] in LIVE
        ]
        return rows[:limit]

    def _spine_start(self, eid) -> list[dict]:
        return [
            {"segment_ids": r["segment_ids"]}
            for r in self.t["routes"]
            if str(r["event_id"]) == str(eid)
        ][:1]

    def _adjacent_segment(self, jid) -> list[dict]:
        segs = sorted(
            a["from_segment_id"] for a in self.t["adjacency"] if a["via_junction_id"] == jid
        )
        return [{"segment_id": segs[0]}] if segs else []

    def _segment_points(self, ids) -> list[dict]:
        return [
            {"segment_id": s, "lon": SEGMENT_POINTS[s][0], "lat": SEGMENT_POINTS[s][1]}
            for s in ids
            if s in SEGMENT_POINTS
        ]

    def _junction_geom(self, ids) -> list[dict]:
        return [
            {"junction_id": j["junction_id"], "lon": j["lon"], "lat": j["lat"]}
            for j in self.t["junctions"]
            if j["junction_id"] in set(ids)
        ]

    def _recs(self, statuses: tuple[str, ...] | None = None) -> list[dict]:
        rows = []
        for r in self.t["recs"]:
            if statuses is not None and r["status"] not in statuses:
                continue
            s = next((s for s in self.t["sightings"] if s["sighting_id"] == r["sighting_id"]), None)
            rows.append(
                {
                    **r,
                    "camera_id": s["camera_id"] if s else None,
                    "sighting_t": s["t"] if s else None,
                    "sighting_source": s["source"] if s else None,
                    "plate": s["plate"] if s else None,
                    "lon": s["lon"] if s else None,
                    "lat": s["lat"] if s else None,
                    "stolen_vehicle": self._stolen(r["vehicle_id"]),
                }
            )
        return rows

    def _reads_provenance(self, ids) -> list[dict]:
        """bool_and(source LIKE 'emulated%') per vehicle that has reads."""
        out = []
        for vid in ids:
            mine = [s for s in self.t["sightings"] if str(s["vehicle_id"]) == str(vid)]
            if mine:
                out.append(
                    {
                        "vehicle_id": vid,
                        "all_emulated": all(
                            str(s["source"]).startswith("emulated") for s in mine
                        ),
                    }
                )
        return out

    def _stolen(self, vehicle_id) -> bool:
        """engine.SQL_STOLEN_EARMARK in Python."""
        now = datetime.now(timezone.utc)
        return any(
            e["vehicle_id"] == vehicle_id
            and e["cleared_at"] is None
            and e["retention_until"] > now
            and e["lawful_basis"] == "stolen_vehicle_fir"
            for e in self.t["earmarks"]
        )

    def _anomaly_row(self, a: dict) -> dict:
        pt = SEGMENT_POINTS.get(a["segment_id"], (None, None))
        return {
            **a,
            "road_name": f"Road {a['segment_id']}",
            "lon": pt[0],
            "lat": pt[1],
            "n_15m": None,
        }

    def _recent_anomalies(self) -> list[dict]:
        cut = datetime.now(timezone.utc) - timedelta(minutes=15)
        out = []
        for seg in sorted({a["segment_id"] for a in self.t["anomalies"]}):
            recent = sorted(
                [
                    a
                    for a in self.t["anomalies"]
                    if a["segment_id"] == seg and a["timestamp_utc"] > cut
                ],
                key=lambda a: a["timestamp_utc"],
                reverse=True,
            )
            if recent:
                out.append({**self._anomaly_row(recent[0]), "n_15m": len(recent)})
        return out

    def _subject_events(self, ids) -> list[dict]:
        want = _u(ids)
        return [
            {"event_id": e["event_id"], "movement_id": e["movement_id"], "metadata": e["metadata"]}
            for e in self.t["events"]
            if str(e["event_id"]) in want or (e["movement_id"] and str(e["movement_id"]) in want)
        ]

    def _events_citing(self, vids, rids) -> list[dict]:
        out = []
        for e in self.t["events"]:
            meta = json.loads(e["metadata"])
            if meta.get("vehicle_id") in set(vids) or meta.get("recommendation_id") in set(rids):
                out.append({"event_id": e["event_id"]})
        return out

    def _dossier_vehicles(self, ids) -> list[dict]:
        now = datetime.now(timezone.utc)
        rows = []
        for v in self.t["vehicles"]:
            if str(v["vehicle_id"]) not in _u(ids):
                continue
            em = next(
                (
                    e
                    for e in self.t["earmarks"]
                    if e["vehicle_id"] == v["vehicle_id"]
                    and e["cleared_at"] is None
                    and e["retention_until"] > now
                ),
                None,
            )
            base = {**v, "first_seen": _ago(days=5), "last_seen": _ago(minutes=5)}
            for k in (
                "state",
                "reason",
                "lawful_basis",
                "retention_until",
                "earmarked_at",
                "earmarked_by",
            ):
                base[k] = em[k] if em else None
            rows.append(base)
        return rows

    # -- feed -----------------------------------------------------------
    def _feed_event_log(self, since, limit, unscoped, eids) -> list[dict]:
        rows = []
        for r in self.t["status_log"]:
            if r["at"] > since and (unscoped or str(r["event_id"]) in _u(eids)):
                ev = self._event(r["event_id"])
                rows.append(
                    {
                        **r,
                        "event_kind": ev["event_kind"],
                        "title": ev["title"],
                        "metadata": ev["metadata"],
                    }
                )
        return sorted(rows, key=lambda r: r["at"], reverse=True)[:limit]

    def _feed_event_created(self, since, limit, unscoped, eids) -> list[dict]:
        logged = {str(r["event_id"]) for r in self.t["status_log"] if r["from_status"] is None}
        rows = [
            e
            for e in self.t["events"]
            if e["created_at"] > since
            and str(e["event_id"]) not in logged
            and (unscoped or str(e["event_id"]) in _u(eids))
        ]
        return sorted(rows, key=lambda e: e["created_at"], reverse=True)[:limit]

    def _feed_enforcement(self, since, limit, unscoped, rids, vids, sids) -> list[dict]:
        rows = [
            r
            for r in self._recs()
            if max(r["created_at"], r["decided_at"] or r["created_at"]) > since
            and (
                unscoped
                or str(r["recommendation_id"]) in _u(rids)
                or str(r["vehicle_id"]) in _u(vids)
                or str(r["sighting_id"]) in _u(sids)
            )
        ]
        return rows[:limit]

    def _feed_anpr(self, since, limit, unscoped, vids, cids, sids) -> list[dict]:
        now = datetime.now(timezone.utc)
        rows = [
            s
            for s in self.t["sightings"]
            if s["matched_plate_hit"]
            and since < s["t"] <= now
            and (
                unscoped
                or str(s["vehicle_id"]) in _u(vids)
                or s["camera_id"] in set(cids)
                or str(s["sighting_id"]) in _u(sids)
            )
            and self._lawful(s)
        ]
        return sorted(rows, key=lambda s: s["t"], reverse=True)[:limit]

    def _feed_dispatch(self, since, limit, unscoped, eids, dids, uids, jids) -> list[dict]:
        rows = []
        for de in self.t["dispatch_events"]:
            d = next(d for d in self.t["dispatches"] if d["dispatch_id"] == de["dispatch_id"])
            if de["at"] <= since:
                continue
            if not (
                unscoped
                or str(d["event_id"]) in _u(eids)
                or str(d["dispatch_id"]) in _u(dids)
                or d["unit_id"] in set(uids)
                or d["waypoint_junction"] in set(jids)
            ):
                continue
            ev = self._event(d["event_id"])
            rows.append(
                {
                    **de,
                    "event_id": d["event_id"],
                    "agency": d["agency"],
                    "unit_id": d["unit_id"],
                    "waypoint_junction": d["waypoint_junction"],
                    "event_title": ev["title"],
                    "event_metadata": ev["metadata"],
                }
            )
        return sorted(rows, key=lambda r: r["at"], reverse=True)[:limit]

    def _feed_signal_plans(self, since, limit, unscoped, eids, jids) -> list[dict]:
        rows = [
            p
            for p in self.t["plans"]
            if p["applied_at"] > since
            and (
                unscoped
                or str(p["event_id"]) in _u(eids)
                or set(p["junction_ids"]) & set(jids)
            )
        ]
        return sorted(rows, key=lambda p: (p["applied_at"], p["plan_id"]), reverse=True)[:limit]

    def _feed_signals(self, since, limit, unscoped, eids, jids) -> list[dict]:
        groups: dict[tuple, dict] = {}
        planned_at = {p["applied_at"] for p in self.t["plans"]}
        for h in self.t["history"]:
            if h["captured_at"] <= since:
                continue
            if h["kind"] == "snapshot" and h["captured_at"] in planned_at:
                continue  # narrated by its plan-ledger row
            if not (unscoped or str(h["event_id"]) in _u(eids) or h["junction_id"] in set(jids)):
                continue
            key = (h["captured_at"], h["event_id"], h["actor"])
            g = groups.setdefault(
                key,
                {
                    "first_id": h["history_id"],
                    "captured_at": h["captured_at"],
                    "event_id": h["event_id"],
                    "actor": h["actor"],
                    "kinds": set(),
                    "junction_ids": set(),
                    "reason": h["reason"],
                },
            )
            g["kinds"].add(h["kind"])
            g["junction_ids"].add(h["junction_id"])
        return [
            {**g, "kinds": sorted(g["kinds"]), "junction_ids": sorted(g["junction_ids"])}
            for g in sorted(groups.values(), key=lambda g: g["captured_at"], reverse=True)
        ][:limit]

    def _feed_situations(self, since, limit, unscoped, mids) -> list[dict]:
        rows = []
        prev: dict[str, str] = {}
        for r in sorted(self.t["situation_log"], key=lambda r: (r["observed_at"], r["log_id"])):
            if not r["transitioned"] or not (unscoped or r["movement_id"] in set(mids)):
                continue
            rows.append({**r, "prev_class": prev.get(r["movement_id"])})
            prev[r["movement_id"]] = r["situation_class"]
        rows = [r for r in rows if r["observed_at"] > since]
        return sorted(rows, key=lambda r: r["observed_at"], reverse=True)[:limit]

    def _feed_overrides(self, since, limit, unscoped, mids, lookback, resting) -> list[dict]:
        rows = []
        prev: dict[str, str] = {}
        for o in sorted(self.t["overrides"], key=lambda o: (o["published_at"], o["override_id"])):
            if o["published_at"] <= since - lookback or not (
                unscoped or o["movement_id"] in set(mids)
            ):
                continue
            ev = json.loads(o["evidence"])
            verdict = f"{o['source']}|{o['shadow_eval']}|{ev.get('shadow_eval_detail') or ''}"
            p = prev.get(o["movement_id"])
            prev[o["movement_id"]] = verdict
            if o["published_at"] <= since:
                continue
            if o["source"] == "baseline" and (p or resting) == verdict:
                continue
            rows.append(
                {
                    "override_id": o["override_id"],
                    "movement_id": o["movement_id"],
                    "situation_class": o["situation_class"],
                    "source": o["source"],
                    "shadow_eval": o["shadow_eval"],
                    "detail": ev.get("shadow_eval_detail"),
                    "fail_closed_reason": ev.get("fail_closed_reason"),
                    "floor_applied": None
                    if ev.get("floor_applied") is None
                    else str(ev["floor_applied"]).lower(),
                    "advance_closure_sec": str(json.loads(o["params"]).get("advance_closure_sec")),
                    "protection_class": o["protection_class"],
                    "published_at": o["published_at"],
                    "prev_verdict": p,
                }
            )
        return sorted(rows, key=lambda r: r["published_at"], reverse=True)[:limit]


class CityFakePool:
    def __init__(self, tables: dict[str, Any] | None = None) -> None:
        self.conn = CityFakeConn(tables or city_tables())

    class _Acq:
        def __init__(self, conn: CityFakeConn) -> None:
            self.conn = conn

        async def __aenter__(self) -> CityFakeConn:
            return self.conn

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    def acquire(self) -> "CityFakePool._Acq":
        return CityFakePool._Acq(self.conn)

    async def fetch(self, sql: str, *args: Any) -> list:
        return await self.conn.fetch(sql, *args)


class FakeValkey:
    """scan_iter + mget + get over a dict (the valkey.asyncio subset used)."""

    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = dict(data or {})
        self.mget_calls = 0
        self.get_calls = 0

    def scan_iter(self, match: str = "*", count: int = 10):
        prefix = match.rstrip("*")
        keys = sorted(k for k in self.data if k.startswith(prefix))

        async def gen():
            for k in keys:
                yield k

        return gen()

    async def mget(self, keys):
        self.mget_calls += 1
        return [self.data.get(k) for k in keys]

    async def get(self, key):
        self.get_calls += 1
        return self.data.get(key)


class FakeGraphDB:
    """Marks the routing graph as wired.  Its own pool-acquiring search
    must NEVER be called from a request that already holds a connection
    (the units projection searches on the held connection)."""

    async def shortest_path(self, source: int, target: int) -> list[dict]:
        raise AssertionError("nested pool acquire: graph_db.shortest_path called")


def city_valkey() -> FakeValkey:
    return FakeValkey(
        {
            "units:emulated:P-1": json.dumps(
                {
                    "unit_id": "P-1",
                    "agency": "police",
                    "lon": 72.58,
                    "lat": 23.02,
                    "status": "patrol",
                    "t": NOW.isoformat(),
                    "source": "emulated_patrol",
                }
            ),
            # C-A minimal doc (the acceptance shape): no label, no t
            "units:emulated:T1": json.dumps(
                {
                    "unit_id": "T1",
                    "agency": "jan_rakshak",
                    "lon": 72.58,
                    "lat": 23.02,
                    "status": "patrol",
                }
            ),
            # rejected: unknown agency / corrupt payload
            "units:emulated:X-9": json.dumps(
                {"unit_id": "X-9", "agency": "martians", "lon": 1, "lat": 1}
            ),
            "units:emulated:BROKEN": "{not json",
            # a patrol frame for a dispatched unit overrides its projected position
            "units:emulated:JR-7": json.dumps(
                {
                    "unit_id": "JR-7",
                    "agency": "jan_rakshak",
                    "lon": 72.579,
                    "lat": 23.031,
                    "status": "en_route",
                    "t": NOW.isoformat(),
                }
            ),
            "convoy:position:M-1": json.dumps(
                {
                    "movement_id": "M-1",
                    "event_id": str(E_PURSUIT),
                    "lon": 72.57,
                    "lat": 23.03,
                    "speed_kmh": 32.0,
                    "heading": 90.0,
                    "t": NOW.isoformat(),
                    "source": "emulated_gps",
                    "progress": 0.4,
                    "segment_id": 1015,
                }
            ),
            "convoy:position:BAD": json.dumps({"movement_id": "BAD", "lon": "x", "lat": None}),
            "convoy:context:" + str(E_PURSUIT): json.dumps(
                {
                    "movement_id": str(E_PURSUIT),
                    "decisions_log": [
                        {
                            "phase": "decider_checkpoint",
                            "trigger": "incident_detected",
                            "tier": "decider",
                            "latency_ms": 812,
                            "timestamp": (NOW - timedelta(minutes=3)).timestamp(),
                        },
                        {
                            "type": "low_confidence_decision",
                            "phase": "pre_movement_alert",
                            "action": "gemma_draft_required",
                            "timestamp": (NOW - timedelta(minutes=2)).timestamp(),
                        },
                        {
                            "phase": "pre_movement",
                            "recommendation": "approved",
                        },  # untimed: not narrated
                    ],
                }
            ),
        }
    )


def make_app(pool: CityFakePool | None = None, valkey: FakeValkey | None = None):
    common.reset_relation_cache()
    cams_mod.reset_camera_cache()
    feed_mod.reset_brain_cache()
    units_router.reset_path_cache()
    app = create_app()
    app.state.pool = pool or CityFakePool()
    app.state.adaptive_valkey = valkey
    app.state.graph_db = FakeGraphDB()
    return app


def call(app, path: str):
    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.get(path)

    return asyncio.run(run())


ENTITY_KEYS = {
    "urn",
    "kind",
    "id",
    "label",
    "lon",
    "lat",
    "state",
    "band",
    "source",
    "t",
    "age_sec",
    "links",
    "props",
}
FEED_KEYS = {
    "id",
    "t",
    "kind",
    "urns",
    "actor",
    "actor_kind",
    "text",
    "source",
    "severity",
    "props",
}


def _walk_keys(obj: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            keys |= _walk_keys(v)
    return keys


# ─── pure helpers ────────────────────────────────────────────────────────────


class TestCommon:
    def test_ids_validate_per_kind(self):
        assert common.parse_entity_id("event", str(E_PURSUIT).upper()) == str(E_PURSUIT)
        assert common.parse_entity_id("junction", "15") == 15
        assert common.parse_entity_id("camera", "EM-CAM-01") == "EM-CAM-01"
        for kind, bad in (
            ("event", "rath_yatra"),
            ("junction", "-1"),
            ("junction", "1.5"),
            ("camera", "a b"),
            ("camera", "../etc"),
            ("unit", "x" * 80),
            ("nope", "1"),
        ):
            with pytest.raises(ValueError):
                common.parse_entity_id(kind, bad)

    def test_plate_masking_keeps_prefix_and_last_digit(self):
        assert common.mask_plate("GJ27EM9001") == "GJ27EM•••1"
        assert common.mask_plates_in_text("PURSUIT — stolen GJ 27 EM 9001 (FIR 4401/2026)") == (
            "PURSUIT — stolen GJ 27 EM •••1 (FIR 4401/2026)"
        )
        assert (
            common.mask_plates_in_text("no plate here, FIR 4401/2026")
            == "no plate here, FIR 4401/2026"
        )

    def test_sources_and_actor_kinds(self):
        assert common.label_source("emulated_anpr") == "emulated"
        assert common.label_source("live_anpr") == "live"
        assert common.label_source("gps") == "live"
        assert common.event_source({"source": "event-emulator"}) == "emulated"
        assert common.event_source({"mode": "living_city"}) == "emulated"
        assert common.event_source({"source": "convoy-brain"}) == "live"
        assert common.actor_kind_of("system:event_closed") == "system"
        assert common.actor_kind_of("convoy-brain (system)") == "system"
        assert common.actor_kind_of("traffic-oracle") == "machine"
        assert common.actor_kind_of("Duty Officer A") == "human"

    def test_bbox_and_kinds_parsing(self):
        assert common.parse_bbox("72.5,22.95,72.7,23.12") == (72.5, 22.95, 72.7, 23.12)
        for bad in ("1,2,3", "72.7,22.95,72.5,23.12", "a,b,c,d", "nan,1,2,3"):
            with pytest.raises(ValueError):
                common.parse_bbox(bad)
        assert common.parse_kinds("camera,event", common.ENTITY_KINDS) == ("event", "camera")
        with pytest.raises(ValueError):
            common.parse_kinds("camera,ufo", common.ENTITY_KINDS)

    def test_iso_z_contract_format(self):
        t = datetime(2026, 10, 22, 12, 56, 14, 174000, tzinfo=timezone.utc)
        assert common.iso_z(t) == "2026-10-22T12:56:14.174Z"


# ─── GET /cameras ────────────────────────────────────────────────────────────


class TestCameras:
    def test_derived_cameras_with_counters_and_no_plate_keys(self):
        r = call(make_app(), "/api/v1/cameras")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["registry"] == "derived_from_sightings"
        ids = [c["camera_id"] for c in body["cameras"]]
        assert ids == ["EM-CAM-01", "EM-CAM-02", "EM-CAM-03", "EM-CAM-04"]
        cam1 = body["cameras"][0]
        assert set(cam1) >= {
            "camera_id",
            "lon",
            "lat",
            "kind",
            "source",
            "junction_id",
            "heading_deg",
            "status",
            "reads_15m",
            "hits_15m",
            "last_read_t",
        }
        assert cam1["kind"] == "anpr" and cam1["source"] == "emulated"
        # two past reads in 15 min (the future-dated read does not count)
        assert cam1["reads_15m"] == 2 and cam1["hits_15m"] == 2
        assert common.parse_ts(cam1["last_read_t"]) <= datetime.now(timezone.utc)
        assert not (
            _walk_keys(body) & {"plate", "frame", "image", "frames", "images", "plate_masked"}
        )

    def test_registry_is_whitelisted_and_unregistered_reads_appended(self):
        tables = city_tables()
        tables["relations"].add("corridor.cameras")
        tables["registry"] = [
            {
                "camera_id": "EM-CAM-01",
                "kind": "anpr",
                "source": "emulated",
                "junction_id": 1,
                "heading_deg": 90.0,
                "status": "active",
                "name": "Lal Darwaja ANPR",
                "geom": {"type": "Point", "coordinates": [72.5812, 23.0282]},
                "last_frame": "base64...",
                "plate_cache": ["GJ27EM9001"],
            },  # must never leak
            {
                "camera_id": "OSM-CCTV-9",
                "kind": "cctv",
                "source": "osm",
                "junction_id": None,
                "heading_deg": None,
                "status": "active",
                "geom": {"type": "Point", "coordinates": [72.61, 23.03]},
            },
        ]
        body = call(make_app(CityFakePool(tables)), "/api/v1/cameras").json()
        assert body["registry"] == "corridor.cameras"
        by_id = {c["camera_id"]: c for c in body["cameras"]}
        assert by_id["EM-CAM-01"]["registered"] is True
        assert (
            by_id["EM-CAM-01"]["junction_id"] == 1
            and by_id["EM-CAM-01"]["label"] == "Lal Darwaja ANPR"
        )
        assert by_id["OSM-CCTV-9"]["kind"] == "cctv" and by_id["OSM-CCTV-9"]["reads_15m"] == 0
        # read in the last 24 h but not registered → appended and flagged
        assert by_id["EM-CAM-02"]["registered"] is False
        assert by_id["EM-CAM-02"]["status"] == "unregistered"
        assert not (_walk_keys(body) & {"plate", "plate_cache", "last_frame", "frame", "image"})


    def test_base_list_is_cached_but_counters_are_fresh(self):
        pool = CityFakePool()
        app = make_app(pool)
        first = call(app, "/api/v1/cameras").json()
        pool.conn.t["sightings"].append(
            {**pool.conn.t["sightings"][0], "sighting_id": _sid(77), "t": _ago(minutes=1),
             "matched_plate_hit": None}
        )
        second = call(app, "/api/v1/cameras").json()
        derived = [sql for sql in pool.conn.calls if sql == cams_mod._SQL_CAMERAS_DERIVED]
        assert len(derived) == 1  # the base list was aggregated once
        cam3 = {c["camera_id"]: c for c in second["cameras"]}["EM-CAM-03"]
        before = {c["camera_id"]: c for c in first["cameras"]}["EM-CAM-03"]
        assert cam3["reads_15m"] == before["reads_15m"] + 1  # counters per request


# ─── GET /city/entities ──────────────────────────────────────────────────────


class TestEntities:
    def _snapshot(self, path: str = "/api/v1/city/entities", **kw):
        r = call(make_app(kw.get("pool"), kw.get("valkey", city_valkey())), path)
        assert r.status_code == 200, r.text
        return r.json()

    def test_every_kind_and_contract_keys(self):
        body = self._snapshot()
        assert set(body) >= {"as_of", "entities", "feeds"}
        kinds = {}
        for e in body["entities"]:
            assert set(e) == ENTITY_KEYS, e["urn"]
            assert e["urn"] == f"{e['kind']}:{e['id']}"
            assert isinstance(e["lon"], float) and isinstance(e["lat"], float)
            assert e["source"] in ("live", "emulated")
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        assert kinds == {
            "event": 2,  # pursuit (spine) + work zone (zone centroid); completed excluded
            "vehicle": 2,  # active earmarks only (the plain vehicle never appears)
            "sighting": 4,  # 4 past hops of the FLAGGED vehicle (future read excluded)
            "camera": 4,
            "junction": 2,
            "unit": 3,  # JR-7 (dispatch + patrol frame), P-1, T1 — not JR-EM-01
            "convoy": 1,  # the malformed C-B frame is dropped
            "dispatch": 1,  # the closed event's dispatch is excluded
            "recommendation": 3,
            "anomaly": 1,  # latest per segment in 15 min
        }
        assert set(body["feeds"]) == set(common.ENTITY_KINDS)
        for kind, feed in body["feeds"].items():
            assert set(feed) >= {"count", "newest_t", "age_sec", "status"}, kind
            assert feed["status"] == "ok", (kind, feed)

    def test_event_entity_spine_band_and_links(self):
        body = self._snapshot()
        ev = next(e for e in body["entities"] if e["id"] == str(E_PURSUIT))
        assert ev["props"]["event_kind"] == "vehicle_pursuit"
        assert ev["props"]["route_spines"] == [
            {
                "label": "old-city sweep belt",
                "segment_ids": [1012, 1015, 1005, 1013],
                "variant": None,  # additive (C-O): written at creation
            }
        ]
        assert ev["props"]["route_history"] == []  # pre-018 store: no history
        # ON its route: the spine's point nearest the spine's centroid (item 2)
        assert ev["props"]["position_basis"] == "spine_point_nearest_centroid"
        assert ent.SPINE_MARKER_BASIS == "spine_point_nearest_centroid"
        marker = spine_marker(list(SEGMENT_POINTS.values()))
        assert (ev["lon"], ev["lat"]) == pytest.approx(marker, abs=1e-9)
        assert marker in SEGMENT_POINTS.values()   # a point of the route, not the centroid
        assert ev["band"] == "RED"  # latest situation_log row
        assert ev["label"] == "PURSUIT — stolen GJ27EM•••1"  # masked
        assert ev["props"]["title"] == "PURSUIT — stolen GJ27EM9001"
        assert f"vehicle:{V_STOLEN}" in ev["links"] and f"dispatch:{D_LIVE}" in ev["links"]
        assert "convoy:M-1" in ev["links"] and "segment:1012" in ev["links"]
        zone = next(e for e in body["entities"] if e["id"] == str(E_ZONE))
        assert zone["props"]["position_basis"] == "zone_centroid" and zone["lon"] == 72.60

    def test_vehicle_sighting_and_privacy(self):
        body = self._snapshot()
        veh = {e["id"]: e for e in body["entities"] if e["kind"] == "vehicle"}
        stolen = veh[str(V_STOLEN)]
        assert stolen["label"] == "GJ27EM•••1"
        assert (
            stolen["props"]["plate"] == "GJ27EM9001"
            and stolen["props"]["plate_masked"] == "GJ27EM•••1"
        )
        assert stolen["props"]["earmark_state"] == "flagged"
        assert stolen["props"]["lawful_basis"] == "stolen_vehicle_fir"
        assert stolen["source"] == "emulated"
        assert (stolen["lon"], stolen["lat"]) == (72.5825, 23.029)  # latest PAST read
        assert f"recommendation:{R_APPROVED}" in stolen["links"]
        assert f"event:{E_PURSUIT}" in stolen["links"]
        assert str(V_PLAIN) not in veh
        hops = [e for e in body["entities"] if e["kind"] == "sighting"]
        assert {h["props"]["vehicle_id"] for h in hops} == {str(V_STOLEN)}  # flagged only
        assert sorted(h["props"]["hop"] for h in hops) == [1, 2, 3, 4]
        assert all(h["label"].endswith("GJ27EM•••1") for h in hops)

    def test_units_dispatch_and_patrol_merge(self):
        body = self._snapshot()
        units = {e["id"]: e for e in body["entities"] if e["kind"] == "unit"}
        assert "JR-EM-01" not in units  # its event is completed
        jr7 = units["JR-7"]
        assert jr7["props"]["unit_source"] == "emulated_patrol"  # frame overrides projection
        assert (jr7["lon"], jr7["lat"]) == (72.579, 23.031)
        assert jr7["props"]["dispatch_id"] == str(D_LIVE) and jr7["props"]["eta_sec"] is None
        assert all(u["source"] == "emulated" and u["props"]["emulated"] for u in units.values())
        assert units["T1"]["t"] is None and units["T1"]["label"] == "Jan Rakshak · T1"
        assert body["feeds"]["unit"]["patrol_feed"]["rejected"] == 2
        assert body["feeds"]["unit"]["conflicts"] == []
        assert all(u["props"]["double_booked"] is False for u in units.values())
        disp = [e for e in body["entities"] if e["kind"] == "dispatch"]
        assert [d["id"] for d in disp] == [str(D_LIVE)]
        assert disp[0]["props"]["position_basis"] == "waypoint_junction"
        assert (disp[0]["lon"], disp[0]["lat"]) == (72.578, 23.035)

    def test_a_dispatch_without_a_waypoint_sits_at_its_event_marker(self):
        """No waypoint named: the dispatch is drawn at its event's marker
        (on the event's route) and says so — never 'event_spine_centroid'."""
        row = {
            "dispatch_id": uuid.uuid4(), "event_id": E_PURSUIT, "agency": "jan_rakshak",
            "unit_id": "JR-9", "waypoint_junction": None, "status": "approved",
            "requested_by": "Duty Officer", "decided_by": "Duty Officer", "approved_at": NOW,
            "created_at": NOW, "updated_at": NOW, "lon": None, "lat": None,
            "event_title": "PURSUIT", "event_metadata": {},
        }
        marker = spine_marker(list(SEGMENT_POINTS.values()))
        d = ent.dispatch_entity(row, NOW, fallback=marker)
        assert d["props"]["position_basis"] == ent.DISPATCH_AT_EVENT_BASIS == "event_marker"
        assert (d["lon"], d["lat"]) == marker
        assert ent.dispatch_entity(row, NOW)["lon"] is None        # nothing to fall back on

    def test_recommendations_convoy_anomaly_junction(self):
        body = self._snapshot()
        recs = {e["id"]: e for e in body["entities"] if e["kind"] == "recommendation"}
        approved = recs[str(R_APPROVED)]
        assert approved["state"] == "approved"
        assert approved["props"]["decided_by"] == "Duty Officer R. Mehta"
        assert recs[str(R_PENDING)]["props"]["human_decision_required"] is True
        conv = next(e for e in body["entities"] if e["kind"] == "convoy")
        assert conv["id"] == "M-1" and conv["source"] == "emulated" and conv["state"] == "moving"
        assert f"event:{E_PURSUIT}" in conv["links"]
        anom = next(e for e in body["entities"] if e["kind"] == "anomaly")
        assert anom["id"] == "900" and anom["props"]["count_15m"] == 2
        j1 = next(e for e in body["entities"] if e["urn"] == "junction:1")
        assert j1["label"] == "Lal Darwaja" and j1["props"]["name_gu"] == "લાલ દરવાજા"
        assert j1["state"] in ("green", "lost") and "active_phase" in j1["props"]
        j2 = next(e for e in body["entities"] if e["urn"] == "junction:2")
        assert j2["state"] == "unsignalled"

    def test_kinds_and_bbox_filters(self):
        body = self._snapshot("/api/v1/city/entities?kinds=camera,junction")
        assert {e["kind"] for e in body["entities"]} == {"camera", "junction"}
        assert set(body["feeds"]) == {"camera", "junction"}
        body = self._snapshot("/api/v1/city/entities?bbox=72.574,23.0,72.59,23.04&kinds=camera")
        assert [e["id"] for e in body["entities"]] == ["EM-CAM-01", "EM-CAM-03"]
        for bad in ("kinds=ufo", "bbox=1,2,3", "bbox=72.7,23,72.5,23.1"):
            r = call(make_app(), "/api/v1/city/entities?" + bad)
            assert r.status_code == 422, bad

    def test_one_dead_source_never_blanks_the_city(self):
        pool = CityFakePool()
        pool.conn.fail.add(ent._SQL_JUNCTIONS)
        body = self._snapshot(pool=pool)
        assert body["feeds"]["junction"]["status"] == "error"
        assert body["feeds"]["junction"]["count"] == 0
        assert body["feeds"]["camera"]["count"] == 4 and body["feeds"]["event"]["count"] == 2

    def test_no_valkey_is_labelled_unavailable(self):
        body = self._snapshot(valkey=None)
        assert body["feeds"]["convoy"]["status"] == "unavailable"
        assert body["feeds"]["unit"]["patrol_feed"]["status"] == "unavailable"
        # dispatch projection still fields JR-7 without Valkey
        assert [e["id"] for e in body["entities"] if e["kind"] == "unit"] == ["JR-7"]

    def test_snapshot_issues_no_per_row_queries(self):
        pool = CityFakePool()
        self._snapshot(pool=pool)
        counts: dict[str, int] = {}
        for sql in pool.conn.calls:
            counts[sql] = counts.get(sql, 0) + 1
        for sql in (
            ent._SQL_LIVE_EVENTS,
            ent._SQL_LIVE_DISPATCHES,
            ent._SQL_EARMARKED_VEHICLES,
            ent._SQL_RECENT_SIGHTINGS,
            ent._SQL_OPEN_RECOMMENDATIONS,
            ent._SQL_EVENT_SPINES,
        ):
            assert counts.get(sql) == 1, sql[:60]


# ─── GET /city/entity/{kind}/{id} ────────────────────────────────────────────


class TestDossier:
    def _get(self, path: str, valkey: FakeValkey | None = None, pool: CityFakePool | None = None):
        return call(make_app(pool, valkey if valkey is not None else city_valkey()), path)

    def test_vehicle_chain(self):
        r = self._get(f"/api/v1/city/entity/vehicle/{V_STOLEN}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) >= {"entity", "related", "audit"}
        assert body["entity"]["urn"] == f"vehicle:{V_STOLEN}"
        rel: dict[str, list[dict]] = {}
        for item in body["related"]:
            assert set(item) == {"relation", "entity"}
            assert set(item["entity"]) == ENTITY_KEYS
            rel.setdefault(item["relation"], []).append(item["entity"])
        assert len(rel["sighting"]) == 4
        assert {c["id"] for c in rel["seen_by_camera"]} == {"EM-CAM-01", "EM-CAM-03", "EM-CAM-04"}
        rec = next(e for e in rel["recommendation"] if e["id"] == str(R_APPROVED))
        assert rec["state"] == "approved" and rec["props"]["decided_by"] == "Duty Officer R. Mehta"
        assert [e["id"] for e in rel["pursuit"]] == [str(E_PURSUIT)]
        kinds = {i["kind"] for i in body["audit"]}
        assert {"anpr", "enforcement", "event"} <= kinds
        human = [i for i in body["audit"] if i["actor_kind"] == "human"]
        assert any("APPROVED by Duty Officer R. Mehta" in i["text"] for i in human)
        assert all(set(i) == FEED_KEYS for i in body["audit"])

    def test_vehicle_without_earmark_gets_evidence_reads_only(self):
        body = self._get(f"/api/v1/city/entity/vehicle/{V_PLAIN}").json()
        assert body["entity"]["state"] == "not_earmarked"
        assert "privacy_note" in body["entity"]["props"]
        sightings = [r["entity"]["id"] for r in body["related"] if r["relation"] == "sighting"]
        assert sightings == [str(_sid(6))]  # the evidence read, not the other one
        assert body["entity"]["props"]["earmark_state"] is None
        # WP-19: no lawful last read → badged from the aggregate over its
        # reads (all emulated here), never a default 'live'
        assert body["entity"]["source"] == "emulated"

    def test_event_dossier(self):
        body = self._get(f"/api/v1/city/entity/event/{E_PURSUIT}").json()
        rel = {}
        for item in body["related"]:
            rel.setdefault(item["relation"], []).append(item["entity"]["urn"])
        assert rel["pursued_vehicle"] == [f"vehicle:{V_STOLEN}"]
        assert rel["authorizing_recommendation"] == [f"recommendation:{R_APPROVED}"]
        assert rel["dispatch"] == [f"dispatch:{D_LIVE}"]
        assert rel["unit"] == ["unit:JR-7"]
        assert rel["route_segment"] == [
            "segment:1012",
            "segment:1015",
            "segment:1005",
            "segment:1013",
        ]
        assert rel["signal_junction"] == ["junction:1"]
        assert rel["convoy"] == ["convoy:M-1"]  # the escort frame names this event
        audit_kinds = {i["kind"] for i in body["audit"]}
        assert {
            "event",
            "dispatch",
            "ama",
            "enforcement",
            "anpr",
            "signals",
            "brain",
        } <= audit_kinds

    def test_completed_event_keeps_a_dossier(self):
        body = self._get(f"/api/v1/city/entity/event/{E_DONE}").json()
        assert body["entity"]["state"] == "completed"
        assert body["entity"]["source"] == "emulated"
        disp = [r["entity"] for r in body["related"] if r["relation"] == "dispatch"]
        assert disp[0]["props"]["event_status"] == "completed"

    def test_other_kinds(self):
        v = city_valkey()
        cases = {
            f"sighting/{_sid(3)}": {"vehicle", "camera", "recommendation"},
            f"recommendation/{R_APPROVED}": {
                "vehicle",
                "evidence_sighting",
                "camera",
                "segment",
                "pursuit",
            },
            "camera/EM-CAM-01": {"recent_hit"},
            "junction/1": {"adjacent_segment", "dispatch_waypoint", "signal_event"},
            "unit/JR-7": {"dispatch", "assigned_event"},
            "unit/JR-EM-01": {"dispatch"},
            f"dispatch/{D_LIVE}": {"event", "unit", "waypoint_junction"},
            "convoy/M-1": {"event"},
            "segment/1015": {"adjacent_junction", "spine_of", "anomaly"},
            "anomaly/900": {"segment"},
        }
        for path, relations in cases.items():
            r = self._get("/api/v1/city/entity/" + path, valkey=v)
            assert r.status_code == 200, (path, r.text)
            body = r.json()
            got = {x["relation"] for x in body["related"]}
            assert relations <= got, (path, got)
        stale = self._get("/api/v1/city/entity/unit/JR-EM-01").json()["entity"]
        assert stale["state"] == "not_fielded" and stale["lon"] is None

    def test_legacy_free_form_metadata_never_breaks_a_dossier(self):
        tables = city_tables()
        tables["events"][2]["metadata"] = json.dumps(
            {"vehicle_id": "GJ27EM9001", "recommendation_id": 7}
        )
        pool = CityFakePool(tables)
        r = self._get(f"/api/v1/city/entity/event/{E_ZONE}", pool=pool)
        assert r.status_code == 200, r.text
        body = r.json()
        assert not [
            x for x in body["related"] if x["entity"]["kind"] in ("vehicle", "recommendation")
        ]
        assert not [
            link
            for link in body["entity"]["links"]
            if link.startswith(("vehicle:", "recommendation:"))
        ]

    def test_not_found_and_malformed(self):
        assert self._get("/api/v1/city/entity/vehicle/" + str(uuid.uuid4())).status_code == 404
        assert self._get("/api/v1/city/entity/junction/99").status_code == 404
        assert self._get("/api/v1/city/entity/unit/NOPE-1").status_code == 404
        assert self._get("/api/v1/city/entity/convoy/no-such-move").status_code == 404
        for bad in ("vehicle/GJ27EM9001", "junction/abc", "camera/a%20b", "spaceship/1"):
            r = self._get("/api/v1/city/entity/" + bad)
            assert r.status_code == 422, bad
            assert "detail" in r.json()


# ─── feed + per-event audit ─────────────────────────────────────────────────


class TestFeed:
    def test_union_newest_first_with_stable_ids(self):
        r = call(make_app(valkey=city_valkey()), "/api/v1/city/feed?limit=100")
        assert r.status_code == 200, r.text
        body = r.json()
        items = body["items"]
        assert all(set(i) == FEED_KEYS for i in items)
        assert [i["t"] for i in items] == sorted((i["t"] for i in items), reverse=True)
        kinds = {i["kind"] for i in items}
        assert kinds == {"event", "enforcement", "anpr", "dispatch", "signals", "ama", "brain"}
        ids = [i["id"] for i in items]
        assert len(ids) == len(set(ids))
        assert "event_status_log:2" in ids and "dispatch_events:52" in ids
        assert f"enforcement_recommendations:{R_APPROVED}:2" in ids
        assert "signal_program_history:7" in ids and "situation_log:12" in ids
        assert (
            "param_overrides:1" in ids and "param_overrides:2" not in ids
        )  # same verdict: no news
        assert "param_overrides:3" not in ids  # resting verdict: polling noise is not news
        assert f"corridor_events:{E_DONE}:created" in ids  # emulator event without a ledger row
        assert f"corridor_events:{E_PURSUIT}:created" not in ids  # it has a creation row
        assert all(s["status"] == "ok" for s in body["sources"].values()), body["sources"]

    def test_actor_kinds_and_masking(self):
        items = call(make_app(valkey=city_valkey()), "/api/v1/city/feed").json()["items"]
        by_id = {i["id"]: i for i in items}
        decision = by_id[f"enforcement_recommendations:{R_APPROVED}:2"]
        assert decision["actor_kind"] == "human" and decision["actor"] == "Duty Officer R. Mehta"
        created = by_id[f"enforcement_recommendations:{R_APPROVED}:1"]
        assert created["actor_kind"] == "machine"
        assert by_id[f"corridor_events:{E_DONE}:created"]["actor_kind"] == "system"
        ama = by_id["situation_log:12"]
        assert ama["text"].startswith("AMA band AMBER → RED") and ama["severity"] == "critical"
        assert ama["urns"] == [f"event:{E_PURSUIT}"]
        brain = [i for i in items if i["kind"] == "brain"]
        assert len(brain) == 2 and all(i["actor_kind"] == "machine" for i in brain)
        assert (
            "convoy:" + str(E_PURSUIT) in brain[0]["urns"]
            and f"event:{E_PURSUIT}" in brain[0]["urns"]
        )
        hits = [i for i in items if i["kind"] == "anpr"]
        assert hits and all("9001" not in i["text"] for i in hits)
        assert all("GJ27EM•••1" in i["text"] for i in hits)
        assert hits[0]["severity"] == "critical"
        for i in items:
            assert "GJ27EM9001" not in i["text"]

    def test_since_and_kinds_filters(self):
        app = make_app(valkey=city_valkey())
        since = (NOW - timedelta(minutes=6, seconds=30)).isoformat().replace("+00:00", "Z")
        items = call(app, f"/api/v1/city/feed?since={since}").json()["items"]
        cutoff = common.parse_ts(since)
        assert items and all(common.parse_ts(i["t"]) > cutoff for i in items)
        items = call(app, "/api/v1/city/feed?kinds=dispatch,signals").json()["items"]
        assert {i["kind"] for i in items} == {"dispatch", "signals"}
        assert call(app, "/api/v1/city/feed?kinds=gossip").status_code == 422
        assert call(app, "/api/v1/city/feed?limit=0").status_code == 422
        assert len(call(app, "/api/v1/city/feed?limit=3").json()["items"]) == 3

    def test_degradation_is_visible(self):
        pool = CityFakePool()
        pool.conn.fail.add(feed_mod._SQL_FEED_DISPATCH)
        body = call(make_app(pool, valkey=None), "/api/v1/city/feed").json()
        assert body["sources"]["dispatch_events"]["status"] == "error"
        assert body["sources"]["decisions_log"]["status"] == "unavailable"
        assert {i["kind"] for i in body["items"]} >= {"event", "enforcement", "anpr", "ama"}
        tables = city_tables()
        tables["relations"].discard("events.event_status_log")  # migration 013 not applied
        body = call(make_app(CityFakePool(tables)), "/api/v1/city/feed").json()
        assert body["sources"]["event_status_log"]["status"] == "unavailable"

    def test_planner_sessions_without_an_event_stay_out_of_the_city_feed(self):
        valkey = city_valkey()
        valkey.data["convoy:context:planner-only-1"] = json.dumps(
            {
                "movement_id": "planner-only-1",
                "decisions_log": [
                    {
                        "phase": "pre_movement",
                        "recommendation": "approved",
                        "timestamp": NOW.timestamp(),
                    }
                ],
            }
        )
        body = call(make_app(valkey=valkey), "/api/v1/city/feed").json()
        assert body["sources"]["decisions_log"]["unlinked_movements"] == 1
        assert body["sources"]["decisions_log"]["count"] == 2  # the event-linked rows only
        assert not any("planner-only-1" in " ".join(i["urns"]) for i in body["items"])

    def test_event_audit_scoped_and_404(self):
        app = make_app(valkey=city_valkey())
        r = call(app, f"/api/v1/events/{E_PURSUIT}/audit")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["event_id"] == str(E_PURSUIT)
        own = {
            f"event:{E_PURSUIT}",
            f"vehicle:{V_STOLEN}",
            f"recommendation:{R_APPROVED}",
            f"dispatch:{D_LIVE}",
            "convoy:" + str(E_PURSUIT),
        }
        assert body["items"]
        for item in body["items"]:
            assert set(item["urns"]) & own, item
        assert not any(str(E_DONE) in " ".join(i["urns"]) for i in body["items"])
        assert call(app, f"/api/v1/events/{uuid.uuid4()}/audit").status_code == 404
        assert call(app, "/api/v1/events/rath_yatra/audit").status_code == 422


# ─── GET /adaptive/overrides ─────────────────────────────────────────────────


class TestOverrides:
    def test_contract_shape_and_event_resolution(self):
        app = make_app()
        r = call(app, "/api/v1/adaptive/overrides?limit=5")
        assert r.status_code == 200, r.text
        rows = r.json()["overrides"]
        assert len(rows) == 3
        for row in rows:
            assert set(row) >= {
                "published_at",
                "event_id",
                "movement_id",
                "situation_class",
                "params",
                "source",
                "shadow_eval",
                "shadow_eval_detail",
            }
            assert isinstance(row["params"], dict)
        newest = rows[0]
        assert newest["movement_id"] == "default" and newest["event_id"] is None
        fail_closed = rows[1]
        assert fail_closed["event_id"] == str(E_PURSUIT)
        assert fail_closed["shadow_eval_detail"] == "shadow_context_missing"
        assert fail_closed["fail_closed_reason"] == "shadow_eval_unavailable_fail_closed"
        scoped = call(app, f"/api/v1/adaptive/overrides?event_id={E_PURSUIT}&limit=1").json()
        assert len(scoped["overrides"]) == 1 and scoped["subject"] == str(E_PURSUIT)
        assert call(app, "/api/v1/adaptive/overrides?limit=201").status_code == 422


# ─── review fixes: provenance (emulated events never badge LIVE rows) ──────


def provenance_tables() -> dict[str, Any]:
    """city_tables() + adaptive / brain / signal rows about the EMULATED,
    completed Rath Yatra (E_DONE, metadata.source='event-emulator')."""
    t = city_tables()
    day3 = NOW - timedelta(days=3)
    t["situation_log"] += [
        {
            "log_id": 394,
            "movement_id": str(E_DONE),
            "situation_class": "GREEN",
            "situation_code": "none:FREE_FLOW",
            "severity_score": 2,
            "protection_class": "none",
            "observed_at": day3,
            "transitioned": True,
        },
        {
            "log_id": 395,
            "movement_id": str(E_DONE),
            "situation_class": "RED",
            "situation_code": "none:GRIDLOCK",
            "severity_score": 14,
            "protection_class": "none",
            "observed_at": day3 + timedelta(minutes=10),
            "transitioned": True,
        },
    ]
    t["overrides"].append(
        {
            "override_id": 154,
            "movement_id": str(E_DONE),
            "vvip_class": "none",
            "protection_class": "none",
            "situation_class": "RED",
            "params": json.dumps({"advance_closure_sec": 45}),
            "source": "retrieved",
            "shadow_eval": "passed",
            "evidence": json.dumps({"shadow_eval_detail": "within_tolerance"}),
            "published_at": day3 + timedelta(minutes=11),
        }
    )
    t["plans"].append(
        {
            "plan_id": 3,
            "event_id": E_DONE,
            "band": "RED",
            "junction_ids": [2],
            "snapshots_written": 0,
            "shadow_eval": "passed",
            "actor": "Duty Officer B",
            "applied_at": day3 + timedelta(minutes=12),
        }
    )
    return t


def provenance_valkey() -> FakeValkey:
    v = city_valkey()
    v.data["convoy:context:" + str(E_DONE)] = json.dumps(
        {
            "movement_id": str(E_DONE),
            "decisions_log": [
                {
                    "type": "low_confidence_decision",
                    "phase": "pre_movement_alert",
                    "action": "gemma_draft_required",
                    "timestamp": (NOW - timedelta(days=3, minutes=-5)).timestamp(),
                },
                {
                    "phase": "pre_movement",
                    "recommendation": "approved",
                    "security_compliant": True,
                    "timestamp": (NOW - timedelta(days=3, minutes=-6)).timestamp(),
                },
            ],
        }
    )
    return v


class TestProvenance:
    def test_emulated_event_audit_carries_no_live_row(self):
        app = make_app(CityFakePool(provenance_tables()), provenance_valkey())
        body = call(app, f"/api/v1/events/{E_DONE}/audit").json()
        kinds = {i["kind"] for i in body["items"]}
        assert {"event", "dispatch", "ama", "brain", "signals"} <= kinds, kinds
        assert {i["source"] for i in body["items"]} == {"emulated"}, [
            (i["id"], i["source"]) for i in body["items"] if i["source"] != "emulated"
        ]
        ids = {i["id"] for i in body["items"]}
        assert {"situation_log:395", "param_overrides:154", "signal_plan_log:3"} <= ids

    def test_city_feed_badges_each_row_by_its_own_event(self):
        app = make_app(CityFakePool(provenance_tables()), provenance_valkey())
        items = call(app, "/api/v1/city/feed?kinds=ama,brain,signals&limit=500").json()["items"]
        about_done = [i for i in items if f"event:{E_DONE}" in i["urns"]]
        about_pursuit = [i for i in items if f"event:{E_PURSUIT}" in i["urns"]]
        assert {i["kind"] for i in about_done} == {"ama", "brain", "signals"}
        assert all(i["source"] == "emulated" for i in about_done)
        # the operator-opened pursuit stays LIVE: the badge is per event
        assert about_pursuit and all(i["source"] == "live" for i in about_pursuit)

    def test_adaptive_surfaces_flag_emulated_subjects(self):
        app = make_app(CityFakePool(provenance_tables()))
        rows = call(app, f"/api/v1/adaptive/overrides?event_id={E_DONE}").json()["overrides"]
        assert rows and all(r["emulated"] is True for r in rows)
        assert rows[0]["source"] == "retrieved"  # contract meaning kept: the PARAMS source
        live = call(app, f"/api/v1/adaptive/overrides?event_id={E_PURSUIT}").json()["overrides"]
        assert live and all(r["emulated"] is False for r in live)
        entries = call(app, f"/api/v1/adaptive/situation_log?event_id={E_DONE}").json()["entries"]
        assert entries and all(e["emulated"] is True for e in entries)
        entries = call(app, "/api/v1/adaptive/situation_log?event_id=default").json()["entries"]
        assert all(e["emulated"] is False for e in entries)  # no event: not emulated

    def test_situation_log_provenance_degrades_to_unknown(self):
        pool = CityFakePool(provenance_tables())
        pool.conn.fail.add(common._SQL_SUBJECT_EVENTS)
        r = call(make_app(pool), f"/api/v1/adaptive/situation_log?event_id={E_DONE}")
        assert r.status_code == 200  # the band rung itself never fails on provenance
        assert r.json()["entries"] and all(e["emulated"] is None for e in r.json()["entries"])

    def test_resolved_subjects_source_rule(self):
        resolved = common.ResolvedSubjects(
            events={"m-1": ["e-live", "e-emu"], "e-live": ["e-live"]},
            sources={"e-live": "live", "e-emu": "emulated"},
        )
        assert resolved.source_of("m-1") == "emulated"  # ANY emulated event wins
        assert resolved.source_of("e-live") == "live"
        assert resolved.source_of("default") == "live"  # denotes no event
        upper = str(E_DONE).upper()
        resolved = common.ResolvedSubjects(
            events={str(E_DONE): [str(E_DONE)]}, sources={str(E_DONE): "emulated"}
        )
        assert resolved.source_of(upper) == "emulated"  # canonical subject keys


# ─── review fixes: C6 lawful-read rail (no trail without an active earmark) ─

V_LAPSED = uuid.UUID("66666666-6666-6666-6666-666666666666")
V_EXPIRED = uuid.UUID("88888888-8888-8888-8888-888888888888")
R_LAPSED = uuid.UUID("99999999-9999-9999-9999-999999999999")


def privacy_tables() -> dict[str, Any]:
    """city_tables() + two vehicles whose ANPR hits were LAWFUL when read
    but whose earmark no longer is: one CLEARED by an officer, one whose
    retention LAPSED.  One lapsed hit is the evidence of a recommendation."""
    t = city_tables()
    t["vehicles"] += [
        {"vehicle_id": V_LAPSED, "plate": "GJ27EM4321", "vehicle_class": "car"},
        {"vehicle_id": V_EXPIRED, "plate": "GJ01ZZ7777", "vehicle_class": "car"},
    ]
    t["earmarks"] += [
        {
            "vehicle_id": V_LAPSED,
            "state": "flagged",
            "reason": "watch list",
            "lawful_basis": "mva_1988_s177",
            "retention_until": NOW + timedelta(days=5),
            "earmarked_at": _ago(days=2),
            "earmarked_by": "Duty Officer",
            "cleared_at": _ago(minutes=2),  # cleared by an officer
        },
        {
            "vehicle_id": V_EXPIRED,
            "state": "watch",
            "reason": "watch list",
            "lawful_basis": "mva_1988_s177",
            "retention_until": _ago(days=1),  # retention lapsed
            "earmarked_at": _ago(days=8),
            "earmarked_by": "Duty Officer",
            "cleared_at": None,
        },
    ]
    trail = [(21, "EM-CAM-02", 13), (22, "EM-CAM-03", 12), (23, "EM-CAM-01", 11), (24, "EM-CAM-04", 10)]
    for n, cam, mins in trail:
        t["sightings"].append(
            {
                "sighting_id": _sid(n),
                "vehicle_id": V_LAPSED,
                "plate": "GJ27EM4321",
                "camera_id": cam,
                "t": _ago(minutes=mins),
                "lon": 72.58,
                "lat": 23.02,
                "speed_kmh": 61.0,
                "heading": 90.0,
                "vehicle_class": "car",
                "source": "emulated_anpr",
                "run_id": 20,
                "matched_plate_hit": "earmarked_plate",
            }
        )
    t["sightings"].append(
        {
            "sighting_id": _sid(25),
            "vehicle_id": V_EXPIRED,
            "plate": "GJ01ZZ7777",
            "camera_id": "EM-CAM-02",
            "t": _ago(minutes=9),
            "lon": 72.5731,
            "lat": 23.0246,
            "speed_kmh": 44.0,
            "heading": 0.0,
            "vehicle_class": "car",
            "source": "emulated_anpr",
            "run_id": 20,
            "matched_plate_hit": "earmarked_plate",
        }
    )
    t["recs"].append(
        {
            "recommendation_id": R_LAPSED,
            "sighting_id": _sid(23),
            "vehicle_id": V_LAPSED,
            "rule_id": "R_SPD_SEGMENT",
            "recommendation": "e-challan recommended: GJ27EM4321 at 61 km/h",
            "evidence": json.dumps({"speed_kmh": 61.0, "speed_limit_kmh": 40.0}),
            "status": "pending",
            "decided_by": None,
            "decided_at": None,
            "created_at": _ago(minutes=11),
            "audit_tail": json.dumps(
                [{"actor": "traffic-oracle", "action": "created:pending",
                  "at": _ago(minutes=11).isoformat(), "note": ""}]
            ),
        }
    )
    return t


def _sighting_urns(obj: Any) -> set[str]:
    """Every sighting URN / id anywhere in a payload."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("sighting_id", "last_sighting_id") and isinstance(v, str):
                found.add("sighting:" + v)
            found |= _sighting_urns(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= _sighting_urns(v)
    elif isinstance(obj, str) and obj.startswith("sighting:"):
        found.add(obj)
    return found


class TestPrivacyRail:
    def _get(self, path: str):
        return call(make_app(CityFakePool(privacy_tables()), city_valkey()), path)

    def test_every_read_path_composes_the_one_predicate(self):
        for sql in (feed_mod._SQL_FEED_ANPR, dos._SQL_DOSSIER_SIGHTINGS,
                    dos._SQL_DOSSIER_CAMERA_HITS):
            assert common.SQL_LAWFUL_READ in sql
        assert "ve.cleared_at IS NULL AND ve.retention_until > NOW()" in common.SQL_LAWFUL_READ

    def test_cleared_earmark_vehicle_dossier_leaks_no_trail(self):
        r = self._get(f"/api/v1/city/entity/vehicle/{V_LAPSED}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["entity"]["state"] == "not_earmarked"
        assert "withheld" in body["entity"]["props"]["privacy_note"]
        related = [x["entity"]["id"] for x in body["related"] if x["relation"] == "sighting"]
        assert related == [str(_sid(23))]  # the evidence read only
        anpr = [i["id"] for i in body["audit"] if i["kind"] == "anpr"]
        assert anpr == [f"vehicle_sightings:{_sid(23)}"]
        # the note is TRUE for the whole payload: no other read is reachable
        assert _sighting_urns(body) == {f"sighting:{_sid(23)}"}

    def test_lapsed_retention_vehicle_dossier_leaks_no_trail(self):
        body = self._get(f"/api/v1/city/entity/vehicle/{V_EXPIRED}").json()
        assert body["entity"]["state"] == "not_earmarked"
        assert not [i for i in body["audit"] if i["kind"] == "anpr"]
        assert _sighting_urns(body) == set()

    def test_global_feed_withholds_unlawful_hits(self):
        items = self._get("/api/v1/city/feed?kinds=anpr&limit=500").json()["items"]
        ids = {i["id"] for i in items}
        assert f"vehicle_sightings:{_sid(23)}" in ids  # evidence of R_LAPSED
        for n in (21, 22, 24, 25):
            assert f"vehicle_sightings:{_sid(n)}" not in ids
        assert f"vehicle_sightings:{_sid(1)}" in ids  # active earmark: lawful

    def test_camera_dossier_and_sighting_dossier_obey_the_rail(self):
        cam2 = self._get("/api/v1/city/entity/camera/EM-CAM-02").json()
        hits = {x["entity"]["id"] for x in cam2["related"] if x["relation"] == "recent_hit"}
        assert not hits & {str(_sid(21)), str(_sid(25))}
        cam2_audit = {i["id"] for i in cam2["audit"]}
        assert f"vehicle_sightings:{_sid(21)}" not in cam2_audit
        cam1 = self._get("/api/v1/city/entity/camera/EM-CAM-01").json()
        hits = {x["entity"]["id"] for x in cam1["related"] if x["relation"] == "recent_hit"}
        assert str(_sid(23)) in hits and str(_sid(4)) in hits
        assert self._get(f"/api/v1/city/entity/sighting/{_sid(21)}").status_code == 404
        assert self._get(f"/api/v1/city/entity/sighting/{_sid(25)}").status_code == 404
        assert self._get(f"/api/v1/city/entity/sighting/{_sid(23)}").status_code == 200

    def test_re_earmarking_restores_the_trail(self):
        tables = privacy_tables()
        tables["earmarks"][-2]["cleared_at"] = None  # V_LAPSED earmarked again
        body = call(
            make_app(CityFakePool(tables), city_valkey()),
            f"/api/v1/city/entity/vehicle/{V_LAPSED}",
        ).json()
        anpr = {i["id"] for i in body["audit"] if i["kind"] == "anpr"}
        assert anpr == {f"vehicle_sightings:{_sid(n)}" for n in (21, 22, 23, 24)}


# ─── review fixes: C-B frames with a non-uuid event_id ─────────────────────


def convoy_ref_valkey() -> FakeValkey:
    v = city_valkey()
    v.data["convoy:position:mv-test-1"] = json.dumps(
        {"movement_id": "mv-test-1", "event_id": "rath-yatra", "lon": 72.59, "lat": 23.02,
         "speed_kmh": 0.0, "t": NOW.isoformat(), "source": "emulated_gps"}
    )
    v.data["convoy:position:M-UP"] = json.dumps(
        {"movement_id": "M-UP", "event_id": str(E_PURSUIT).upper(), "lon": 72.6, "lat": 23.03,
         "speed_kmh": 20.0, "t": NOW.isoformat(), "source": "gps"}
    )
    return v


class TestConvoyEventRef:
    def test_slug_event_id_dossier_is_200_and_unlinked(self):
        r = call(make_app(valkey=convoy_ref_valkey()), "/api/v1/city/entity/convoy/mv-test-1")
        assert r.status_code == 200, r.text
        body = r.json()
        ent = body["entity"]
        assert ent["props"]["event_id"] is None and ent["props"]["event_ref"] == "rath-yatra"
        assert not [link for link in ent["links"] if link.startswith("event:")]
        assert body["related"] == []

    def test_snapshot_links_only_canonical_events(self):
        body = call(make_app(valkey=convoy_ref_valkey()), "/api/v1/city/entities").json()
        all_links = [link for e in body["entities"] for link in e["links"]]
        assert "event:rath-yatra" not in all_links
        convoys = {e["id"]: e for e in body["entities"] if e["kind"] == "convoy"}
        assert set(convoys) == {"M-1", "M-UP", "mv-test-1"}
        assert f"event:{E_PURSUIT}" in convoys["M-UP"]["links"]  # canonical, lowercase
        assert convoys["M-UP"]["props"]["event_id"] == str(E_PURSUIT)
        pursuit = next(e for e in body["entities"] if e["id"] == str(E_PURSUIT))
        assert {"convoy:M-1", "convoy:M-UP"} <= set(pursuit["links"])
        assert "convoy:mv-test-1" not in pursuit["links"]

    def test_event_dossier_matches_canonical_frames(self):
        body = call(
            make_app(valkey=convoy_ref_valkey()), f"/api/v1/city/entity/event/{E_PURSUIT}"
        ).json()
        convoys = sorted(x["entity"]["id"] for x in body["related"] if x["relation"] == "convoy")
        assert convoys == ["M-1", "M-UP"]

    def test_convoy_entity_event_id_rules(self):
        now = datetime.now(timezone.utc)
        base = {"movement_id": "M-9", "lon": 72.6, "lat": 23.0}
        for raw, want_id, want_ref in (
            (str(E_DONE).upper(), str(E_DONE), None),
            ("rath-yatra", None, "rath-yatra"),
            ("  ", None, None),
            (123, None, None),
            (None, None, None),
        ):
            ent = ent_mod_convoy({**base, "event_id": raw}, now)
            assert ent["props"]["event_id"] == want_id, raw
            assert ent["props"]["event_ref"] == want_ref, raw


def ent_mod_convoy(doc: dict, now: datetime) -> dict:
    out = ent.convoy_entity("convoy:position:" + doc["movement_id"], doc, now)
    assert out is not None
    return out


# ─── review fixes: wire precision, ordering, actors, entries, stub ─────────


class TestFeedWire:
    def test_since_is_compared_at_wire_precision(self):
        tables = city_tables()
        t_true = datetime(2026, 10, 18, 15, 0, 4, 426700, tzinfo=timezone.utc)
        tables["dispatch_events"].append(
            {"seq": 60, "dispatch_id": D_LIVE, "from_status": "en_route",
             "to_status": "on_scene", "actor": "JR field lead", "note": None, "at": t_true}
        )
        app = make_app(CityFakePool(tables), city_valkey())
        wire = "2026-10-18T15:00:04.426Z"
        items = call(app, "/api/v1/city/feed?kinds=dispatch&limit=500&since=2026-10-18T00:00:00Z")
        item = next(i for i in items.json()["items"] if i["id"] == "dispatch_events:60")
        assert item["t"] == wire
        again = call(app, f"/api/v1/city/feed?kinds=dispatch&limit=500&since={wire}").json()
        assert "dispatch_events:60" not in {i["id"] for i in again["items"]}
        earlier = call(
            app, "/api/v1/city/feed?kinds=dispatch&limit=500&since=2026-10-18T15:00:04.425Z"
        ).json()
        assert "dispatch_events:60" in {i["id"] for i in earlier["items"]}

    def test_after_wire_ms(self):
        t = datetime(2026, 10, 18, 15, 0, 4, 426000, tzinfo=timezone.utc)
        assert common.after_wire_ms(t) == t.replace(microsecond=426999)
        assert common.after_wire_ms(t.replace(microsecond=426700)) == t.replace(
            microsecond=426999
        )

    def test_same_millisecond_orders_by_ledger_sequence(self):
        tables = city_tables()
        same = datetime(2026, 10, 18, 15, 0, 4, 426000, tzinfo=timezone.utc)
        for seq, to_status in ((9, "approved"), (10, "active")):
            tables["status_log"].append(
                {"seq": seq, "event_id": E_ZONE, "from_status": "planning",
                 "to_status": to_status, "actor": "Duty Officer A", "note": None,
                 "at": same + timedelta(microseconds=seq)}
            )
        items = call(
            make_app(CityFakePool(tables)), "/api/v1/city/feed?kinds=event&limit=500"
        ).json()["items"]
        ids = [i["id"] for i in items if i["id"] in ("event_status_log:9", "event_status_log:10")]
        assert ids == ["event_status_log:10", "event_status_log:9"]

    def test_event_audit_entries_bridge_is_oldest_first(self):
        body = call(make_app(valkey=city_valkey()), f"/api/v1/events/{E_PURSUIT}/audit").json()
        assert body["items"] and body["entries"] == list(reversed(body["items"]))


class TestActors:
    def test_machine_actor_is_a_word_match(self):
        assert common.actor_kind_of("Brainard Patel") == "human"
        assert common.actor_kind_of("convoy-brain") == "machine"
        assert common.actor_kind_of("brain") == "machine"
        assert common.actor_kind_of("convoy-brain (system)") == "system"

    def test_event_without_a_recorded_opener_is_system(self):
        items = call(make_app(), "/api/v1/city/feed?kinds=event&limit=500").json()["items"]
        zone = next(i for i in items if i["id"] == f"corridor_events:{E_ZONE}:created")
        assert zone["actor"] is None and zone["actor_kind"] == "system"

    def test_named_actor_rule(self):
        assert common.named_actor("  Duty   Officer A ") == "Duty Officer A"
        assert common.named_actor("ઇન્\u200dકમ ટેક્સ") == "ઇન્\u200dકમ ટેક્સ"  # ZWJ is legal
        for bad in ("Duty\x00Officer", "Duty\u202eOfficer", "x", "y" * 65, "\x07\x07"):
            with pytest.raises(ValueError):
                common.named_actor(bad)
        assert common.named_actor(None) is None
        assert common.clean_text("line one\nline two\t(ok)") == "line one\nline two\t(ok)"
        with pytest.raises(ValueError):
            common.clean_text("nul\x00here")
        assert common.find_nul({"a": [{"b": "x\x00"}]}) == "a[0].b"
        assert common.find_nul({"a": ["fine", {"b": 1}]}) is None


class TestSignalPlanNarrative:
    def _tables(self):
        t = city_tables()
        at = _ago(minutes=6)  # the existing snapshot row's transaction clock
        t["history"][0]["captured_at"] = at
        t["plans"].append(
            {"plan_id": 7, "event_id": E_PURSUIT, "band": "AMBER", "junction_ids": [1],
             "snapshots_written": 1, "shadow_eval": "passed", "actor": "Duty Officer A",
             "applied_at": at}
        )
        t["plans"].append(  # the RE-apply: no snapshot, still attributed
            {"plan_id": 8, "event_id": E_PURSUIT, "band": "RED", "junction_ids": [1],
             "snapshots_written": 0, "shadow_eval": "passed", "actor": "Duty Officer B",
             "applied_at": _ago(minutes=4)}
        )
        t["history"].append(
            {"history_id": 9, "junction_id": 1, "event_id": E_PURSUIT, "kind": "restore",
             "actor": "Duty Officer C", "reason": "restore to pre-calibration snapshot",
             "captured_at": _ago(minutes=2)}
        )
        return t

    def test_every_apply_is_one_attributed_item(self):
        items = call(
            make_app(CityFakePool(self._tables())), "/api/v1/city/feed?kinds=signals"
        ).json()["items"]
        by_id = {i["id"]: i for i in items}
        assert set(by_id) == {"signal_plan_log:7", "signal_plan_log:8",
                              "signal_program_history:9"}
        # the snapshot rows of plan 7 are narrated BY plan 7, not twice
        assert "signal_program_history:7" not in by_id
        assert by_id["signal_plan_log:8"]["actor"] == "Duty Officer B"
        assert "City signal plan RED committed by Duty Officer B" in by_id[
            "signal_plan_log:8"]["text"]
        assert by_id["signal_plan_log:7"]["props"]["snapshots_written"] == 1
        assert by_id["signal_program_history:9"]["actor"] == "Duty Officer C"
        assert all(i["actor_kind"] == "human" for i in items)
        scoped = call(
            make_app(CityFakePool(self._tables())), "/api/v1/city/entity/junction/1"
        ).json()["audit"]
        assert {"signal_plan_log:7", "signal_plan_log:8"} <= {i["id"] for i in scoped}

    def test_ledger_missing_is_unavailable_not_error(self):
        tables = self._tables()
        tables["relations"].discard("corridor.signal_plan_log")
        body = call(make_app(CityFakePool(tables)), "/api/v1/city/feed").json()
        assert body["sources"]["signal_program_history"]["status"] == "unavailable"


class TestCoordinationStub:
    def test_every_city_path_is_a_503_stub_when_the_package_fails(self, monkeypatch):
        import sys

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from traffic_oracle.coordination import CITY_PATHS, register_coordination_routers

        monkeypatch.setitem(sys.modules, "traffic_oracle.city.router", None)  # ImportError
        app = FastAPI()
        register_coordination_routers(app)
        with TestClient(app) as client:
            for path in (
                "/api/v1/cameras",
                "/api/v1/city/entities",
                f"/api/v1/city/entity/vehicle/{V_STOLEN}",
                "/api/v1/city/feed",
                f"/api/v1/events/{E_PURSUIT}/audit",
            ):
                r = client.get(path)
                assert r.status_code == 503 and r.json() == {"detail": "not deployed"}, path
        assert len(CITY_PATHS) == 5


# ─── LIVE SCHEMA (scratch clone only; everything rolled back) ───────────────

LIVE_DSN = os.environ.get("VVIP_TEST_DATABASE_URL")
live = pytest.mark.skipif(not LIVE_DSN, reason="VVIP_TEST_DATABASE_URL (scratch clone) not set")


def _sql_constants() -> dict[str, str]:
    """Every read-SQL constant this package owns (name → statement)."""
    from traffic_oracle import events as events_mod
    from traffic_oracle.signals import writer as writer_mod

    modules = (
        common,
        cams_mod,
        ent,
        dos,
        feed_mod,
        units_router,
        surfacing,
        events_mod,
        writer_mod,
    )
    out: dict[str, str] = {}
    for mod in modules:
        for name, value in vars(mod).items():
            if name.startswith("_SQL") and isinstance(value, str):
                out[f"{mod.__name__}.{name}"] = value
    out["events._ACTIVE_EVENTS_SQL"] = events_mod._ACTIVE_EVENTS_SQL
    return out


class _OneConnPool:
    """A pool whose every acquire() is ONE connection inside an outer
    transaction (inner transactions become savepoints) — so the real route
    handlers run end to end and the test rolls everything back."""

    def __init__(self, conn) -> None:
        self.conn = conn

    class _Acq:
        def __init__(self, conn) -> None:
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *exc):
            return False

    def acquire(self):
        return _OneConnPool._Acq(self.conn)

    async def fetch(self, *args):
        return await self.conn.fetch(*args)


#: Statements over another package's not-yet-landed table: prepared only
#: when that table exists (WP-03's 012 creates corridor.cameras).
_OPTIONAL_RELATIONS = {"traffic_oracle.city.cameras._SQL_CAMERA_REGISTRY": "corridor.cameras"}

#: Statements that read an additive migration's column (WP-19: 018's
#: superseded_at, 015's display_geom) — prepared only where the column is
#: in, exactly as the readers pick them (city/common.column_exists).
_SUPERSEDE = ("events.event_routes", "superseded_at")
_DISPLAY = ("corridor.road_segments", "display_geom")
_OPTIONAL_COLUMNS = {
    "traffic_oracle.events._SQL_EVENT_ROUTES_CURRENT": _SUPERSEDE,
    "traffic_oracle.events._SQL_EVENT_ROUTE_HISTORY": _SUPERSEDE,
    "traffic_oracle.events._SQL_CURRENT_ROUTES_FOR_UPDATE": _SUPERSEDE,
    "traffic_oracle.city.entities._SQL_EVENT_SPINES_CURRENT": _SUPERSEDE,
    "traffic_oracle.city.entities._SQL_EVENT_SPINE_HISTORY": _SUPERSEDE,
    "traffic_oracle.city.dossier._SQL_SEGMENT_EVENTS_CURRENT": _SUPERSEDE,
    "traffic_oracle.units.router._SQL_EVENT_SPINE_START_CURRENT": _SUPERSEDE,
    "traffic_oracle.city.entities._SQL_RECENT_ANOMALIES_DISPLAY": _DISPLAY,
    "traffic_oracle.city.dossier._SQL_DOSSIER_SEGMENTS_DISPLAY": _DISPLAY,
    "traffic_oracle.city.dossier._SQL_DOSSIER_ANOMALY_DISPLAY": _DISPLAY,
    "traffic_oracle.city.dossier._SQL_SEGMENT_ANOMALIES_DISPLAY": _DISPLAY,
    "traffic_oracle.units.router._SQL_SEGMENT_POINTS_DISPLAY": _DISPLAY,
}


def _optional_column(name: str) -> tuple[str, str] | None:
    return _OPTIONAL_COLUMNS.get(name)


@live
def test_live_every_sql_constant_prepares():
    asyncpg = pytest.importorskip("asyncpg")

    async def run():
        conn = await asyncpg.connect(LIVE_DSN)
        failures, prepared, skipped = [], 0, []
        try:
            tx = conn.transaction()
            await tx.start()
            try:
                for name, sql in _sql_constants().items():
                    needs = _OPTIONAL_RELATIONS.get(name)
                    if needs and not await conn.fetchval(
                        "SELECT to_regclass($1) IS NOT NULL", needs
                    ):
                        skipped.append(name)
                        continue
                    column = _optional_column(name)
                    if column and not await conn.fetchval(common._SQL_COLUMN, *column):
                        skipped.append(name)
                        continue
                    try:
                        async with conn.transaction():  # savepoint: one failure aborts nothing else
                            await conn.prepare(sql)
                        prepared += 1
                    except Exception as exc:  # noqa: BLE001 — collect them all
                        failures.append(f"{name}: {exc!r}")
            finally:
                await tx.rollback()
        finally:
            await conn.close()
        return failures, prepared, skipped

    failures, prepared, skipped = asyncio.run(run())
    assert not failures, "\n".join(failures)
    assert prepared >= 50, (prepared, skipped)


@live
def test_live_city_endpoints_end_to_end():
    asyncpg = pytest.importorskip("asyncpg")

    async def run():
        conn = await asyncpg.connect(LIVE_DSN)
        tx = conn.transaction()
        await tx.start()
        try:
            common.reset_relation_cache()
            cams_mod.reset_camera_cache()
            feed_mod.reset_brain_cache()
            app = create_app()
            app.state.pool = _OneConnPool(conn)
            app.state.adaptive_valkey = None
            app.state.graph_db = None
            out = {}
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                for path in (
                    "/api/v1/cameras",
                    "/api/v1/city/entities",
                    "/api/v1/city/feed?limit=50",
                    "/api/v1/adaptive/overrides?limit=5",
                    "/api/v1/units",
                ):
                    out[path] = await c.get(path)
                ev = await conn.fetchval(
                    "SELECT event_id FROM events.corridor_events ORDER BY created_at LIMIT 1"
                )
                if ev is not None:
                    out["audit"] = await c.get(f"/api/v1/events/{ev}/audit")
                    out["dossier"] = await c.get(f"/api/v1/city/entity/event/{ev}")
                veh = await conn.fetchval(
                    "SELECT vehicle_id FROM corridor.vehicles ORDER BY last_seen DESC LIMIT 1"
                )
                if veh is not None:
                    out["vehicle"] = await c.get(f"/api/v1/city/entity/vehicle/{veh}")
            return out
        finally:
            await tx.rollback()
            await conn.close()

    out = asyncio.run(run())
    for path, resp in out.items():
        assert resp.status_code == 200, (path, resp.text[:300])
    snap = out["/api/v1/city/entities"].json()
    assert all(f["status"] in ("ok", "unavailable") for f in snap["feeds"].values()), snap["feeds"]
    for e in snap["entities"]:
        assert set(e) == ENTITY_KEYS and e["lon"] is not None
    feed = out["/api/v1/city/feed?limit=50"].json()
    assert all(s["status"] in ("ok", "unavailable") for s in feed["sources"].values()), feed[
        "sources"
    ]
    assert not (_walk_keys(out["/api/v1/cameras"].json()) & {"plate", "frame", "image"})


@live
def test_live_event_markers_sit_on_their_route():
    """Item 2 on the REAL schema (scratch clone; rolled back): an event's
    God's-eye marker is ON its rank-1 spine — ST_ClosestPoint(spine,
    centroid) — for a loop (the flood corridor, whose centroid lands inside
    the loop) and a belt alike, labelled 'spine_point_nearest_centroid';
    the dossier serves the same point."""
    asyncpg = pytest.importorskip("asyncpg")

    async def run():
        conn = await asyncpg.connect(LIVE_DSN)
        tx = conn.transaction()
        await tx.start()
        try:
            common.reset_relation_cache()
            spines = {}
            for spine in ([1040, 1021, 1022, 1008], [1015, 1012, 1049, 1013]):
                have = await conn.fetchval(
                    "SELECT count(*) FROM corridor.road_segments WHERE segment_id = ANY($1::bigint[])",
                    spine)
                if have != len(spine):
                    continue
                eid = uuid.uuid4()
                await conn.execute(
                    "INSERT INTO events.corridor_events (event_id, event_kind, title, status, "
                    "metadata) VALUES ($1, 'procession', 'item-2 marker test', 'active', "
                    "'{\"source\": \"event-emulator\"}'::jsonb)", eid)
                await conn.execute(
                    "INSERT INTO events.event_routes (event_id, route_rank, label, segment_ids) "
                    "VALUES ($1, 1, 'spine', $2::bigint[])", eid, spine)
                spines[str(eid)] = spine
            if not spines:
                pytest.skip("the scratch store has none of the test spines")
            app = create_app()
            app.state.pool = _OneConnPool(conn)
            app.state.adaptive_valkey = None
            app.state.graph_db = None
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                snap = (await c.get("/api/v1/city/entities")).json()
                dossiers = {eid: (await c.get(f"/api/v1/city/entity/event/{eid}")).json()
                            for eid in spines}
            out = []
            for e in snap["entities"]:
                if e["kind"] != "event" or e["id"] not in spines:
                    continue
                row = await conn.fetchrow(
                    "SELECT ST_Distance(ST_Collect(rs.geom)::geography, ST_SetSRID(ST_MakePoint("
                    "$2, $3), 4326)::geography) AS on_route_m, ST_Distance(ST_Collect(rs.geom)"
                    "::geography, ST_Centroid(ST_Collect(rs.geom))::geography) AS centroid_off_m, "
                    "ST_Distance(ST_ClosestPoint(ST_Collect(rs.geom), ST_Centroid(ST_Collect("
                    "rs.geom)))::geography, ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography) "
                    "AS to_closest_m FROM corridor.road_segments rs "
                    "WHERE rs.segment_id = ANY($1::bigint[])",
                    spines[e["id"]], e["lon"], e["lat"])
                out.append((e, dict(row), dossiers[e["id"]]))
            return out
        finally:
            await tx.rollback()
            await conn.close()

    out = asyncio.run(run())
    assert out
    for e, row, dossier in out:
        assert e["props"]["position_basis"] == "spine_point_nearest_centroid", e["props"]
        assert row["on_route_m"] < 0.05, (e["id"], row)       # ON the route
        assert row["to_closest_m"] < 0.05, (e["id"], row)     # its point nearest the centroid
        entity = dossier.get("entity") or dossier
        assert (entity["lon"], entity["lat"]) == pytest.approx((e["lon"], e["lat"]))


@live
def test_live_review_fixes_rolled_back():
    """The three review fixes on the REAL schema (scratch clone; one
    transaction, rolled back): the lawful-read rail, emulated provenance of
    AMA / override / signal-plan rows, and the units projection searching
    the graph on the held connection inside a (savepoint) transaction.
    The reviewer's exact repros are re-checked when the clone holds them
    (vehicle GJ27EM4321 without an earmark; emulated event 3ef08290)."""
    asyncpg = pytest.importorskip("asyncpg")

    async def run():
        conn = await asyncpg.connect(LIVE_DSN)
        tx = conn.transaction()
        await tx.start()
        try:
            if not await conn.fetchval("SELECT to_regclass('corridor.signal_plan_log') IS NOT NULL"):
                pytest.skip("migration 013 (with signal_plan_log) not applied on the scratch DB")
            vid, rid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            sids = [uuid.uuid4() for _ in range(3)]
            plate = "GJ99ZQ" + str(uuid.uuid4().int % 9000 + 1000)
            cams = ["WP02-LIVE-CAM-1", "WP02-LIVE-CAM-2", "WP02-LIVE-CAM-3"]
            await conn.execute(
                "INSERT INTO corridor.vehicles (vehicle_id, plate) VALUES ($1, $2)", vid, plate
            )
            await conn.execute(
                "INSERT INTO corridor.vehicle_earmarks (vehicle_id, state, reason, lawful_basis, "
                "retention_days, earmarked_by, earmarked_at, retention_until, cleared_at) VALUES "
                "($1, 'watch', 'test', 'mva_1988_s177', 7, 'Duty Officer', NOW() - interval '1 day', "
                "NOW() + interval '6 days', NOW() - interval '1 minute')",
                vid,
            )
            for n, (sid, cam) in enumerate(zip(sids, cams, strict=True)):
                await conn.execute(
                    "INSERT INTO corridor.vehicle_sightings (sighting_id, vehicle_id, plate, camera_id, "
                    "t, position, speed_kmh, source, matched_plate_hit) VALUES ($1, $2, $3, $4, "
                    "NOW() - make_interval(mins => $5), ST_SetSRID(ST_MakePoint(72.58, 23.02), 4326), "
                    "55, 'emulated_anpr', 'earmarked_plate')",
                    sid, vid, plate, cam, 10 - n,
                )
            await conn.execute(
                "INSERT INTO events.enforcement_recommendations (recommendation_id, sighting_id, "
                "vehicle_id, rule_id, recommendation, evidence, status, audit_tail) VALUES ($1, $2, "
                "$3, 'R_SPD_SEGMENT', 'test', '{}'::jsonb, 'pending', '[]'::jsonb)",
                rid, sids[1], vid,
            )
            await conn.execute(
                "INSERT INTO events.corridor_events (event_id, event_kind, title, status, movement_id, "
                "metadata) VALUES ($1, 'procession', 'WP02 live emulated', 'completed', $1, "
                "'{\"source\": \"event-emulator\"}'::jsonb)",
                eid,
            )
            for log_id_off, cls in ((0, "GREEN"), (1, "RED")):
                await conn.execute(
                    "INSERT INTO adaptive.situation_log (movement_id, vvip_class, situation_code, "
                    "situation_class, severity_score, inputs, observed_class, transitioned, "
                    "observed_at, protection_class) VALUES ($1, 'none', 'none:TEST', $2, 5, '{}', $2, "
                    "true, NOW() - make_interval(mins => $3), 'none')",
                    str(eid), cls, 30 - log_id_off,
                )
            await conn.execute(
                "INSERT INTO adaptive.param_overrides (movement_id, vvip_class, situation_class, "
                "params, source, shadow_eval, evidence, published_at, protection_class) VALUES ($1, "
                "'none', 'RED', '{\"advance_closure_sec\": 45}', 'retrieved', 'passed', "
                "'{\"shadow_eval_detail\": \"within_tolerance\"}', NOW() - interval '28 minutes', 'none')",
                str(eid),
            )
            await conn.execute(
                "INSERT INTO corridor.signal_plan_log (event_id, band, junction_ids, "
                "snapshots_written, shadow_eval, actor) VALUES ($1, 'RED', '{}'::bigint[], 0, "
                "'passed', 'Duty Officer Live')",
                eid,
            )
            # an approved dispatch of a LIVE event, so /units projects a path
            live_event = await conn.fetchrow(
                "SELECT e.event_id, r.segment_ids[1] AS first_seg FROM events.corridor_events e "
                "JOIN events.event_routes r ON r.event_id = e.event_id AND r.route_rank = 1 "
                "WHERE e.status IN ('planning', 'approved', 'active') LIMIT 1"
            )
            if live_event is not None:
                wp = await conn.fetchval(
                    "SELECT sa.via_junction_id FROM corridor.segment_adjacency sa "
                    "WHERE sa.from_segment_id <> $1 ORDER BY sa.via_junction_id LIMIT 1",
                    live_event["first_seg"],
                )
                await conn.execute(
                    "INSERT INTO corridor.dispatches (event_id, agency, unit_id, waypoint_junction, "
                    "status, requested_by, decided_by, approved_at) VALUES ($1, 'jan_rakshak', "
                    "'WP02-LIVE-U1', $2, 'approved', 'Duty Officer', 'Duty Officer', "
                    "NOW() - interval '20 seconds')",
                    live_event["event_id"], wp,
                )
            repro_vehicle = await conn.fetchval(
                "SELECT v.vehicle_id FROM corridor.vehicles v WHERE v.plate = 'GJ27EM4321' AND NOT "
                "EXISTS (SELECT 1 FROM corridor.vehicle_earmarks e WHERE e.vehicle_id = v.vehicle_id "
                "AND e.cleared_at IS NULL AND e.retention_until > NOW())"
            )
            repro_event = await conn.fetchval(
                "SELECT e.event_id FROM events.corridor_events e WHERE e.event_id = "
                "'3ef08290-66d8-511b-a25a-3d2967c79985'"
            )

            common.reset_relation_cache()
            cams_mod.reset_camera_cache()
            feed_mod.reset_brain_cache()
            units_router.reset_path_cache()
            app = create_app()
            app.state.pool = _OneConnPool(conn)
            app.state.adaptive_valkey = None
            app.state.graph_db = object()  # graph wired: the search runs on the held conn
            out: dict[str, Any] = {}
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                out["vehicle"] = await c.get(f"/api/v1/city/entity/vehicle/{vid}")
                out["withheld"] = await c.get(f"/api/v1/city/entity/sighting/{sids[0]}")
                out["evidence"] = await c.get(f"/api/v1/city/entity/sighting/{sids[1]}")
                out["camera"] = await c.get(f"/api/v1/city/entity/camera/{cams[0]}")
                out["feed"] = await c.get("/api/v1/city/feed?kinds=anpr&limit=500")
                out["audit"] = await c.get(f"/api/v1/events/{eid}/audit")
                out["overrides"] = await c.get(f"/api/v1/adaptive/overrides?event_id={eid}")
                out["situation"] = await c.get(f"/api/v1/adaptive/situation_log?event_id={eid}")
                out["units"] = await c.get("/api/v1/units")
                if repro_vehicle is not None:
                    out["repro_vehicle"] = await c.get(
                        f"/api/v1/city/entity/vehicle/{repro_vehicle}"
                    )
                if repro_event is not None:
                    out["repro_event"] = await c.get(f"/api/v1/events/{repro_event}/audit")
            return out, sids, live_event is not None
        finally:
            await tx.rollback()
            await conn.close()

    out, sids, projected = asyncio.run(run())
    for name in ("vehicle", "evidence", "camera", "feed", "audit", "overrides", "situation", "units"):
        assert out[name].status_code == 200, (name, out[name].text[:300])
    # C6: the cleared-earmark vehicle leaks only its evidence read
    vehicle = out["vehicle"].json()
    assert [i["id"] for i in vehicle["audit"] if i["kind"] == "anpr"] == [
        f"vehicle_sightings:{sids[1]}"
    ]
    assert _sighting_urns(vehicle) == {f"sighting:{sids[1]}"}
    assert out["withheld"].status_code == 404
    camera = out["camera"].json()
    assert not [x for x in camera["related"] if x["relation"] == "recent_hit"]
    feed_ids = {i["id"] for i in out["feed"].json()["items"]}
    assert f"vehicle_sightings:{sids[1]}" in feed_ids
    assert not {f"vehicle_sightings:{sids[0]}", f"vehicle_sightings:{sids[2]}"} & feed_ids
    # provenance: every row about the emulated event is EMULATED
    audit = out["audit"].json()
    kinds = {i["kind"] for i in audit["items"]}
    assert {"ama", "signals"} <= kinds, kinds
    assert {i["source"] for i in audit["items"]} == {"emulated"}
    assert audit["entries"] == list(reversed(audit["items"]))
    assert all(r["emulated"] is True for r in out["overrides"].json()["overrides"])
    assert all(e["emulated"] is True for e in out["situation"].json()["entries"])
    # units: the projection searched the graph on the held connection
    if projected:
        unit = next(u for u in out["units"].json()["units"] if u["unit_id"] == "WP02-LIVE-U1")
        assert unit["position"] is not None
    # the reviewer's repros, when the clone holds them
    if "repro_vehicle" in out:
        body = out["repro_vehicle"].json()
        assert out["repro_vehicle"].status_code == 200
        evidence = {
            x["entity"]["id"] for x in body["related"] if x["relation"] == "sighting"
        }
        anpr = {i["id"].split(":", 1)[1] for i in body["audit"] if i["kind"] == "anpr"}
        assert anpr <= evidence, (anpr, evidence)
    if "repro_event" in out:
        items = out["repro_event"].json()["items"]
        assert items and {i["source"] for i in items} == {"emulated"}, [
            (i["id"], i["source"]) for i in items if i["source"] != "emulated"
        ]
