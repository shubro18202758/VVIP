"""Corridor-event generalization tests (core stream).

Covers the event-agnostic extensions that must not disturb the VVIP base
case: SituationInputs public-event profile fields, the merged
situation-engine subject query (events + legacy movements), the events API
request model (incl. the 013 ``officer`` creation actor), the
/events/active row shape with its lifecycle fields (spines and phases read
in two batched queries), EMERGENCY_VEHICLE scheduling, and the
ambulance-rate multiplier.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from traffic_oracle.adaptive.engine_loop import _load_active_movements
from traffic_oracle.adaptive.enums import (
    CrowdDensity,
    ProtectionClass,
    SecurityTier,
    SituationClass,
)
from traffic_oracle.adaptive.inputs import SituationInputs, round_trip
from traffic_oracle.adaptive.situation import SECURITY_POINTS, SituationEngine
from traffic_oracle.events import CorridorEventRequest, EVENT_KINDS, event_inputs_key
from traffic_oracle.simulation.engine import ClosurePolicy, SimulationEngine
from traffic_oracle.simulation.events import (
    EventConfig,
    EventSource,
    EventType,
    ScheduledEvent,
)
from traffic_oracle.simulation.network import default_network
from traffic_oracle.simulation.scenarios import (
    SCENARIO_LIBRARY,
    ScenarioSpec,
    get_scenario,
    list_scenarios,
)

USE_CASE_IDS = (
    "vvip_convoy_base",
    "rath_yatra_ahmedabad",
    "kumbh_scale_gathering",
    "riverfront_marathon",
    "national_day_parade",
    "motera_stadium_egress",
    "flood_evacuation_corridor",
    "metro_work_zone",
)

USE_CASE_KINDS = {
    "vvip_convoy_base": "protected_movement",
    "rath_yatra_ahmedabad": "procession",
    "kumbh_scale_gathering": "mass_gathering",
    "riverfront_marathon": "race",
    "national_day_parade": "ceremonial",
    "motera_stadium_egress": "egress_surge",
    "flood_evacuation_corridor": "emergency_evacuation",
    "metro_work_zone": "work_zone",
}


# ── SituationInputs extension ───────────────────────────────────────────────


class TestSituationInputsExtension:
    def test_crowd_class_overrides_score_derived_level(self):
        explicit = SituationInputs.from_scores(
            vvip_class="none",
            predicted_congestion_idx=0.2,
            crowd_score=0.1,           # would derive LOW
            crowd_class="EXTREME",     # public-event profile override
        )
        assert explicit.crowd is CrowdDensity.EXTREME
        assert explicit.crowd_class is CrowdDensity.EXTREME
        score_only = SituationInputs.from_scores(
            vvip_class="none",
            predicted_congestion_idx=0.2,
            crowd_score=0.1,
        )
        assert score_only.crowd is CrowdDensity.LOW
        assert score_only.crowd_class is None

    def test_duration_class_round_trip(self):
        inputs = SituationInputs.from_scores(
            vvip_class="none",
            predicted_congestion_idx=0.2,
            duration_class="multi_week",
        )
        assert round_trip(inputs) == inputs
        assert inputs.to_dict()["duration_class"] == "multi_week"
        assert inputs.to_dict()["crowd_class"] is None

    def test_base_case_defaults_unchanged(self):
        """No crowd_class/duration_class given → legacy fields verbatim."""
        inputs = SituationInputs.from_scores(
            vvip_class="Z+", predicted_congestion_idx=0.5
        )
        d = inputs.to_dict()
        assert d["crowd_class"] is None
        assert d["duration_class"] == "minutes"
        assert d == {
            "vvip_class": "Z+", "traffic_regime": "HEAVY", "weather": "CLEAR",
            "crowd": "LOW", "anomaly_active": False, "event_active": False,
            "crowd_class": None, "duration_class": "minutes",
        }

    def test_public_event_classifies_through_crowd_not_security(self):
        engine = SituationEngine()
        calm = engine.classify(SituationInputs.from_scores(
            vvip_class="none", predicted_congestion_idx=0.2))
        assert calm.situation_class is SituationClass.GREEN
        assert SECURITY_POINTS[SecurityTier.NONE] == 0
        packed = engine.classify(SituationInputs.from_scores(
            vvip_class="none", predicted_congestion_idx=0.95,
            crowd_class="EXTREME", event_active=True, anomaly_active=True))
        # 0 sec + 6 traffic + 0 weather + 3 crowd + 2 anomaly + 1 event = 12 → RED
        assert packed.severity_score == 12
        assert packed.situation_class is SituationClass.RED
        assert packed.code.startswith("none:")

    def test_protection_class_alias(self):
        assert ProtectionClass is SecurityTier
        assert SecurityTier.from_code("PUBLIC") is SecurityTier.NONE


# ── Engine-loop subject merge (events + legacy movements) ───────────────────


class _FakeConn:
    def __init__(self, event_rows, movement_rows):
        self._event_rows = event_rows
        self._movement_rows = movement_rows
        self.queries = []

    async def fetch(self, sql):
        self.queries.append(sql)
        if "events.corridor_events" in sql:
            return self._event_rows
        return self._movement_rows


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


class TestEngineLoopSubjectMerge:
    def test_events_and_movements_merged_events_first(self):
        conn = _FakeConn(
            event_rows=[
                {"event_id": "e-1", "protection_class": "none"},
                {"event_id": "e-2", "protection_class": "Z"},
            ],
            movement_rows=[
                {"movement_id": "m-1", "vvip_class": "Z+"},
                # an event soft-links this movement id: must not duplicate
                {"movement_id": "e-2", "vvip_class": "Z"},
            ],
        )
        subjects = asyncio.run(_load_active_movements(_FakePool(conn)))
        assert subjects == [
            ("e-1", "none"),
            ("e-2", "Z"),
            ("m-1", "Z+"),
        ]
        assert any("events.corridor_events" in q for q in conn.queries)
        assert any("convoy.movements" in q for q in conn.queries)

    def test_events_table_missing_degrades_to_movements(self):
        class _BrokenEventsConn(_FakeConn):
            async def fetch(self, sql):
                if "events.corridor_events" in sql:
                    raise RuntimeError("relation does not exist (007 pending)")
                return await _FakeConn.fetch(self, sql)

        conn = _BrokenEventsConn([], [{"movement_id": "m-1", "vvip_class": "Y"}])
        subjects = asyncio.run(_load_active_movements(_FakePool(conn)))
        assert subjects == [("m-1", "Y")]

    def test_inputs_key_shape(self):
        assert event_inputs_key("abc") == "adaptive:inputs:abc"


# ── Events API request model ────────────────────────────────────────────────


class TestCorridorEventRequest:
    def test_defaults_are_public_event(self):
        req = CorridorEventRequest(
            event_kind="procession", title="Rath Yatra",
            route_spines=[{"segment_ids": [1012]}],
        )
        assert req.protection_class == "none"
        assert req.crowd_class == "LOW"

    def test_invalid_kind_rejected(self):
        with pytest.raises(ValueError):
            CorridorEventRequest(
                event_kind="parade",  # not one of the 8 archetypes
                title="x", route_spines=[{"segment_ids": [1]}],
            )

    def test_invalid_protection_class_rejected(self):
        with pytest.raises(ValueError):
            CorridorEventRequest(
                event_kind="race", protection_class="Q",
                title="x", route_spines=[{"segment_ids": [1]}],
            )

    def test_invalid_crowd_class_rejected(self):
        with pytest.raises(ValueError):
            CorridorEventRequest(
                event_kind="race", crowd_class="PACKED",
                title="x", route_spines=[{"segment_ids": [1]}],
            )

    def test_officer_is_optional_trimmed_and_bounded(self):
        req = CorridorEventRequest(
            event_kind="race", title="x", route_spines=[{"segment_ids": [1]}],
        )
        assert req.officer is None
        req = CorridorEventRequest(
            event_kind="race", title="x", route_spines=[{"segment_ids": [1]}],
            officer="  Duty   Officer A ",
        )
        assert req.officer == "Duty Officer A"
        for bad in ("x", "  ", "z" * 65):
            with pytest.raises(ValueError):
                CorridorEventRequest(
                    event_kind="race", title="x", route_spines=[{"segment_ids": [1]}],
                    officer=bad,
                )

    def test_eight_archetypes(self):
        # 010 (C3 lockstep): + crime_incident + vehicle_pursuit
        assert len(EVENT_KINDS) == 10
        assert "crime_incident" in EVENT_KINDS and "vehicle_pursuit" in EVENT_KINDS
        assert set(USE_CASE_KINDS.values()) <= set(EVENT_KINDS)


# ── /events/active row shape (lifecycle fields, batched spines/phases) ──────


class _ActiveConn:
    def __init__(self) -> None:
        import json as _json
        import uuid as _uuid
        from datetime import timezone as _tz

        self.eid = _uuid.UUID("4508d86d-566b-438f-8414-b3d67fd7e6e4")
        now = datetime(2026, 10, 22, 12, 0, tzinfo=_tz.utc)
        self.row = {
            "event_id": self.eid, "event_kind": "vehicle_pursuit", "protection_class": "none",
            "crowd_class": "LOW", "title": "PURSUIT", "description": "", "status": "active",
            "movement_id": None, "planned_start": None, "planned_end": None,
            "actual_start": now, "actual_end": None,
            "metadata": _json.dumps({"vehicle_id": "v"}), "created_at": now, "updated_at": now,
            "zone_geojson": None,
        }
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        from traffic_oracle import events as ev

        self.calls.append((sql, args))
        if sql == ev._ACTIVE_EVENTS_SQL:
            return [self.row]
        if sql == ev._SQL_EVENT_ROUTES:
            assert list(args[0]) == [self.eid]
            return [{"event_id": self.eid, "label": "belt", "segment_ids": [1012, 1015]}]
        if sql == ev._SQL_EVENT_PHASES:
            start = self.row["actual_start"]
            return [{"event_id": self.eid, "label": "pursuit",
                     "window_start": start, "window_end": start,
                     "closure_policy": '{"closure_type": "signal_hold"}'}]
        raise AssertionError("unexpected SQL: " + sql[:60])


class TestActiveEventsShape:
    def test_rows_carry_lifecycle_fields_and_batched_children(self):
        from httpx import ASGITransport, AsyncClient

        from traffic_oracle.__main__ import create_app

        conn = _ActiveConn()
        app = create_app()
        app.state.pool = _FakePool(conn)

        async def run():
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                return await c.get("/api/v1/events/active")

        r = asyncio.run(run())
        assert r.status_code == 200, r.text
        ev = r.json()["events"][0]
        assert ev["routes"] == [{"label": "belt", "segment_ids": [1012, 1015]}]
        assert ev["phases"][0]["closure_policy"] == {"closure_type": "signal_hold"}
        assert ev["actual_start"].startswith("2026-10-22T12:00") and ev["actual_end"] is None
        assert ev["updated_at"] is not None and ev["metadata"] == {"vehicle_id": "v"}
        assert len(conn.calls) == 3   # events + ONE spines read + ONE phases read


# ── EMERGENCY_VEHICLE scheduling ────────────────────────────────────────────


class TestEmergencyVehicle:
    def test_resolve_has_no_capacity_effect(self):
        network = default_network()
        seg = network.require(1001)
        sched = ScheduledEvent(
            event_type=EventType.EMERGENCY_VEHICLE,
            segment_id=1001, start_sec=300.0, duration_sec=600.0,
            severity=0.9, label="green-corridor ambulance",
        )
        resolved = sched.resolve(object(), seg)
        assert resolved.source is EventSource.SCHEDULED
        assert resolved.capacity_factor == 1.0
        assert resolved.lanes_blocked == 0
        assert resolved.extra_demand_veh_h == 0.0
        assert resolved.green_ratio_factor == 1.0

    def test_scheduled_emergency_vehicle_charged_delay(self):
        engine = SimulationEngine(network=default_network())
        # Ashram Road (North), 1.56 km — long enough for the ambulance to
        # meet the closure (Ashram Road (South) 1001 is 0.46 km on the real
        # network)
        kwargs = dict(
            route_segment_ids=[1002],
            departure_time=datetime.fromisoformat("2026-10-13T18:00:00+05:30"),
            closure_policy=ClosurePolicy(
                advance_closure_sec=120, closure_type="full_closure"
            ),
            horizon_min=30,
            n_replications=4,
            seed=7,
        )
        without = engine.run(**kwargs)
        with_ev = engine.run(
            **kwargs,
            scheduled_events=[
                ScheduledEvent(
                    event_type=EventType.EMERGENCY_VEHICLE,
                    segment_id=1002, start_sec=60.0, duration_sec=600.0,
                    severity=0.9, label="ambulance mid-closure",
                )
            ],
        )
        assert all(
            w.ambulance_delay_sec >= wo.ambulance_delay_sec
            for w, wo in zip(with_ev, without, strict=True)
        )
        assert any(w.ambulance_delay_sec > 0.0 for w in with_ev)

    def test_ambulance_rate_multiplier_zero_removes_random_draws(self):
        engine = SimulationEngine(network=default_network())
        runs = engine.run(
            route_segment_ids=[1001],
            departure_time=datetime.fromisoformat("2026-10-13T18:00:00+05:30"),
            closure_policy=ClosurePolicy(advance_closure_sec=120),
            horizon_min=30,
            n_replications=4,
            seed=11,
            event_config=EventConfig(
                ambulance_rate_multiplier=0.0, disable_all=True
            ),
        )
        assert all(r.ambulance_delay_sec == 0.0 for r in runs)

    def test_event_config_default_multiplier_is_one(self):
        assert EventConfig().ambulance_rate_multiplier == 1.0


# ── Use-case scenario wiring ────────────────────────────────────────────────


class TestUseCaseScenarios:
    def test_all_use_cases_registered(self):
        for sid in USE_CASE_IDS:
            assert sid in SCENARIO_LIBRARY, sid
        assert len(list_scenarios()) == 23  # 14 legacy + 8 use-cases + flagship rath_yatra_monsoon_ambulance

    @pytest.mark.parametrize("sid", USE_CASE_IDS)
    def test_use_case_specs_match_library_configs(self, sid):
        s: ScenarioSpec = get_scenario(sid)
        assert s.event_kind == USE_CASE_KINDS[sid]
        # every use-case spec is runnable end-to-end
        cfg = s.event_config()
        assert cfg.ambulance_rate_multiplier == 1.0
        events = s.scheduled_events()
        for e in events:
            assert e.event_type in set(EventType)
        assert s.weather_state() is not None

    def test_base_case_reference_values(self):
        s = get_scenario("vvip_convoy_base")
        assert s.vvip_class == "Z+"
        # Gandhinagar -> Koba -> Bhat -> Indira Bridge -> Airport Road ->
        # Dr. Ambedkar Road into Lal Darwaja: one legal path on the real network
        assert list(s.route_segment_ids) == [1026, 1025, 1024, 1048, 1028, 1027, 1043]
        assert s.closure_policy == {
            "advance_closure_sec": 180, "closure_type": "full_closure"}
        assert s.seed == 20261102 and s.n_replications == 10
        assert s.horizon_min == 45
        assert s.event_kind == "protected_movement"

    def test_base_case_deterministic_across_runs(self):
        engine = SimulationEngine(network=default_network())
        s = get_scenario("vvip_convoy_base")
        a = engine.run(
            route_segment_ids=list(s.route_segment_ids),
            departure_time=s.departure_dt(),
            closure_policy=ClosurePolicy(**s.closure_policy),
            horizon_min=s.horizon_min, n_replications=3, seed=s.seed,
        )
        b = engine.run(
            route_segment_ids=list(s.route_segment_ids),
            departure_time=s.departure_dt(),
            closure_policy=ClosurePolicy(**s.closure_policy),
            horizon_min=s.horizon_min, n_replications=3, seed=s.seed,
        )
        assert [r.to_dict() for r in a] == [r.to_dict() for r in b]

    def test_flood_scenario_carries_weather_and_overrides(self):
        s = get_scenario("flood_evacuation_corridor")
        assert s.weather["monsoon"] is True
        assert s.weather["rain_intensity_mm_h"] == 22
        assert s.event_overrides["waterlogging"] == 3
        assert s.event_overrides["breakdown"] == 1.8
        assert any(
            e["type"] == "waterlogging" for e in s.forced_events
        )

    def test_motera_carries_scheduled_emergency_vehicle(self):
        s = get_scenario("motera_stadium_egress")
        kinds = [e.event_type for e in s.scheduled_events()]
        assert EventType.EMERGENCY_VEHICLE in kinds

    def test_legacy_scenarios_default_to_protected_movement(self):
        s = get_scenario("monsoon_evening_peak")
        assert s.event_kind == "protected_movement"
        assert s.crowd_class == "LOW"
        assert s.duration_class == "minutes"
