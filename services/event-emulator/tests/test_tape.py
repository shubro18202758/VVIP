"""Library + profiles + tape tests: verbatim consumption, closed cause
vocabulary, byte-level determinism (same spec+seed+t0+run → identical tape)
and the cause model (route-filtered soft saturation, ambulance projection
only inside green-corridor windows, the scenario's own time of day)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from event_emulator.library import KNOWN_EVENT_TYPES, load_library
from event_emulator.profiles import EVENT_KINDS, PROFILES, profile_for
from event_emulator.spec import EmulationSpec, ResolvedRun
from event_emulator.tape import (
    AGENCY_CADENCE_SEC,
    CAUSE_KINDS,
    SATURATION_IDX,
    TAPE_VERSION,
    build_tape,
    congestion_at,
    movement_uuid,
)

EXPECTED_IDS = (
    "vvip_convoy_base",
    "rath_yatra_ahmedabad",
    "rath_yatra_monsoon_ambulance",
    "kumbh_scale_gathering",
    "riverfront_marathon",
    "national_day_parade",
    "motera_stadium_egress",
    "flood_evacuation_corridor",
    "metro_work_zone",
    # watchlist-anpr addition (010, contract C2):
    "watchlist_anpr_sweep",
)

# values pinned from the UseCaseLibrary handoff (verbatim consumption check)
#: Every route a legal path on the real (OpenStreetMap) network — WP-20.
PINNED = {
    "vvip_convoy_base": dict(seed=20261102, vvip="Z+",
                             route=[1026, 1025, 1024, 1048, 1028, 1027, 1043]),
    "rath_yatra_ahmedabad": dict(seed=20250627, vvip="X", route=[1012, 1049, 1013]),
    "rath_yatra_monsoon_ambulance": dict(seed=202506271, vvip="X", route=[1005, 1015, 1001, 1006]),
    "kumbh_scale_gathering": dict(seed=20250129, vvip="X", route=[1001, 1006, 1009]),
    "riverfront_marathon": dict(seed=20241124, vvip="X", route=[1007, 1015, 1001, 1006]),
    "national_day_parade": dict(seed=20270126, vvip="Z", route=[1029, 1028, 1048, 1024, 1025]),
    "motera_stadium_egress": dict(seed=20260308, vvip="X", route=[1032, 1041, 1009, 1002, 1001]),
    "flood_evacuation_corridor": dict(seed=20240827, vvip="X", route=[1040, 1021, 1022, 1008]),
    "metro_work_zone": dict(seed=20250814, vvip="X", route=[1031, 1014, 1049, 1038]),
    "watchlist_anpr_sweep": dict(seed=20261105, vvip="X", route=[1015, 1012, 1049, 1013]),
}

T0 = datetime(2026, 10, 12, 6, 0, 0, tzinfo=timezone.utc)
FLAGSHIP = "rath_yatra_monsoon_ambulance"


def _tape_for(use_case_id: str, seed: int | None = None, tick_sec: float = 5.0,
              t0: datetime = T0, run_id: int | None = None):
    lib = load_library()
    uc = lib[use_case_id]
    spec = EmulationSpec(use_case_id=use_case_id, seed=seed, t0_anchor=t0, tick_sec=tick_sec)
    run = ResolvedRun.resolve(spec, uc)
    return build_tape(run, uc, profile_for(use_case_id), run_id=run_id), uc


def _docs(tape):
    return sorted((f for f in tape.frames if f.kind == "inputs_doc"), key=lambda f: f.t_sec)


class TestLibraryVerbatim:
    def test_all_present(self):
        lib = load_library()
        assert set(lib) == set(EXPECTED_IDS)

    def test_pinned_values(self):
        lib = load_library()
        for uid, pin in PINNED.items():
            cfg = lib[uid].config
            assert cfg["seed"] == pin["seed"], uid
            assert cfg["vvip_class"] == pin["vvip"], uid
            assert cfg["route_segment_ids"] == pin["route"], uid

    def test_forced_events_use_known_types(self):
        lib = load_library()
        for uc in lib.values():
            for fe in uc.config["forced_events"]:
                assert fe["type"] in KNOWN_EVENT_TYPES

    def test_rath_market_override_verbatim(self):
        lib = load_library()
        assert lib["rath_yatra_ahmedabad"].config["event_overrides"] == {"market": 2.5}

    def test_flood_weather_verbatim(self):
        lib = load_library()
        assert lib["flood_evacuation_corridor"].config["weather"] == {
            "monsoon": True, "rain_intensity_mm_h": 22
        }


class TestProfiles:
    def test_every_use_case_has_a_profile(self):
        assert set(PROFILES) == set(EXPECTED_IDS)

    def test_event_kinds_are_the_archetypes(self):
        for p in PROFILES.values():
            assert p.event_kind in EVENT_KINDS

    def test_crowd_waypoints_in_range(self):
        for p in PROFILES.values():
            for _t, v in p.crowd_waypoints:
                assert 0.0 <= v <= 1.0

    def test_extra_event_labels_name_the_real_roads(self):
        # On the real (OpenStreetMap) network 1012 is one-way Gandhi Road,
        # Lal Darwaja -> Kalupur (J3 -> J15); the Jamalpur approach is Swami
        # Vivekananda Road 1013. A label names the road its segment IS, and
        # never a schematic-era name the real network does not carry.
        [spill] = PROFILES["watchlist_anpr_sweep"].extra_events
        assert spill["segment_id"] == 1012
        assert spill["label"] == "bazaar spill onto Gandhi Road during the sweep"
        for uid, p in PROFILES.items():
            for e in p.extra_events:
                for stale in ("Jamalpur approach", "Tilak Road", "CTM Road"):
                    assert stale not in e["label"], (uid, e["segment_id"], e["label"])


class TestTapeDeterminism:
    def test_same_spec_seed_t0_identical_bytes(self):
        a, _ = _tape_for("rath_yatra_ahmedabad", run_id=3)
        b, _ = _tape_for("rath_yatra_ahmedabad", run_id=3)
        assert a.to_json() == b.to_json()

    def test_different_seed_different_tape(self):
        a, _ = _tape_for("kumbh_scale_gathering", seed=1)
        b, _ = _tape_for("kumbh_scale_gathering", seed=2)
        assert a.to_json() != b.to_json()

    def test_seed_defaults_to_config_seed(self):
        tape, uc = _tape_for("vvip_convoy_base")
        assert tape.seed == uc.config["seed"]

    def test_all_build(self):
        for uid in EXPECTED_IDS:
            tape, _ = _tape_for(uid, tick_sec=30.0)  # coarse for speed
            assert tape.frames, uid

    def test_cause_vocabulary_is_closed(self):
        tape, _ = _tape_for("motera_stadium_egress")
        kinds = {f.kind for f in tape.frames}
        assert kinds <= set(CAUSE_KINDS)
        assert "emergency_request" not in kinds  # deleted primitive (C4)

    def test_tape_carries_version_and_run(self):
        import json

        tape, _ = _tape_for("metro_work_zone", tick_sec=60.0, run_id=12)
        doc = json.loads(tape.to_json())
        assert doc["tape_version"] == TAPE_VERSION
        assert doc["run_id"] == 12


class TestRunIdentity:
    def test_live_runs_are_run_scoped(self):
        a, uc = _tape_for("riverfront_marathon", run_id=5)
        b, _ = _tape_for("riverfront_marathon", run_id=6)
        seed = uc.config["seed"]
        assert a.movement_id == movement_uuid("riverfront_marathon", seed, 5)
        assert a.movement_id != b.movement_id

    def test_unledgered_keeps_runless_identity(self):
        assert movement_uuid("rath_yatra_ahmedabad", 42) == \
            movement_uuid("rath_yatra_ahmedabad", 42)
        assert movement_uuid("rath_yatra_ahmedabad", 42) != \
            movement_uuid("rath_yatra_ahmedabad", 43)
        assert movement_uuid("rath_yatra_ahmedabad", 42) != \
            movement_uuid("rath_yatra_ahmedabad", 42, 1)


class TestCauseModel:
    def test_ambulance_projection_zero_outside_ev_windows(self):
        tape, uc = _tape_for(FLAGSHIP, tick_sec=10.0)
        windows = [
            (float(e["start_sec"]), float(e["start_sec"]) + float(e["duration_sec"]))
            for e in uc.config["forced_events"] if e["type"] == "emergency_vehicle"
        ]
        inside = outside = 0
        for f in _docs(tape):
            proj = f.payload["ambulance_delay_projection_sec"]
            if any(a <= f.t_sec < b for a, b in windows):
                inside += 1
                assert 15.0 <= proj <= 120.0
            else:
                outside += 1
                assert proj == 0.0, f.t_sec
        assert inside and outside

    def test_ambulance_projection_decays_during_ev_window(self):
        tape, _ = _tape_for("motera_stadium_egress", tick_sec=60.0)
        during = [d.payload for d in _docs(tape) if 360 <= d.t_sec <= 1440]
        assert during
        assert during[0]["ambulance_delay_projection_sec"] > \
            during[-1]["ambulance_delay_projection_sec"]
        assert all(15.0 <= p["ambulance_delay_projection_sec"] <= 120.0 for p in during)

    def test_flagship_saturation_bound(self):
        tape, _ = _tape_for(FLAGSHIP, tick_sec=5.0)
        docs = [f.payload for f in _docs(tape)]
        saturated = sum(1 for d in docs if d["predicted_congestion_idx"] >= SATURATION_IDX)
        assert saturated / len(docs) <= 0.20

    @pytest.mark.parametrize("uid", EXPECTED_IDS)
    def test_no_use_case_pins_at_one(self, uid):
        tape, _ = _tape_for(uid, tick_sec=30.0)
        docs = [f.payload for f in _docs(tape)]
        pinned = sum(1 for d in docs if d["predicted_congestion_idx"] >= 0.999)
        assert pinned / len(docs) <= 0.20, uid

    def test_off_route_causes_do_not_load_the_route(self):
        on = [{"type": "rally_spillover", "segment_id": 1005, "start_sec": 0,
               "duration_sec": 100, "extra_demand_veh_h": 900}]
        off = [{**on[0], "segment_id": 1012}]
        route = {1005, 1015}
        assert congestion_at(0.5, off, route, 10.0) == pytest.approx(0.5)
        assert congestion_at(0.5, on, route, 10.0) == pytest.approx(1 - 0.5 * 0.7)

    def test_stacked_causes_combine_softly(self):
        causes = [{"type": "rally_spillover", "segment_id": 1, "start_sec": 0,
                   "duration_sec": 100, "extra_demand_veh_h": 1500}] * 4
        val = congestion_at(0.6, causes, {1}, 1.0)
        assert val == pytest.approx(1 - 0.4 * (0.65 ** 4))
        assert val < 1.0

    def test_time_of_day_comes_from_departure(self):
        # the same scenario injected at two different wall times builds the
        # identical cause stream: its clock is its own departure_time
        a, _ = _tape_for(FLAGSHIP, tick_sec=60.0, t0=T0)
        b, _ = _tape_for(FLAGSHIP, tick_sec=60.0,
                         t0=datetime(2026, 10, 12, 17, 30, tzinfo=timezone.utc))
        assert [f.payload for f in _docs(a)] == [f.payload for f in _docs(b)]


class TestTapeContent:
    def test_inputs_docs_match_engine_reader_shape(self):
        tape, _ = _tape_for("riverfront_marathon", tick_sec=60.0)
        docs = [f.payload for f in tape.frames if f.kind == "inputs_doc"]
        assert docs
        for doc in docs:
            for key in (
                "predicted_congestion_idx", "weather_severity", "crowd_score",
                "anomaly_active", "event_active", "last_gps_age_sec",
                "ambulance_delay_projection_sec",
            ):
                assert key in doc
            assert 0.0 <= doc["predicted_congestion_idx"] <= 1.0
            assert 0.0 <= doc["weather_severity"] <= 1.0
            assert 0.0 <= doc["crowd_score"] <= 1.0
            assert isinstance(doc["anomaly_active"], bool)
            assert isinstance(doc["event_active"], bool)

    def test_config_forced_events_verbatim_in_tape(self):
        tape, uc = _tape_for("flood_evacuation_corridor")
        taped = [f.payload for f in tape.frames if f.kind == "scheduled_event"]
        for fe in uc.config["forced_events"]:
            match = [p for p in taped if p.get("label") == fe["label"]]
            assert match, f"missing forced event {fe['label']}"
            m = match[0]
            assert m["type"] == fe["type"]
            assert m["segment_id"] == fe["segment_id"]
            assert m["start_sec"] == fe["start_sec"]
            assert m["duration_sec"] == fe["duration_sec"]
            assert m["severity"] == fe["severity"]

    def test_emergency_vehicle_causes_present(self):
        for uid in ("motera_stadium_egress", "flood_evacuation_corridor"):
            tape, _ = _tape_for(uid, tick_sec=30.0)
            evs = [
                f.payload for f in tape.frames
                if f.kind == "scheduled_event" and f.payload["type"] == "emergency_vehicle"
            ]
            assert evs, uid
            assert all(e["severity"] > 0 for e in evs)

    def test_base_case_planted_breakdown(self):
        tape, _ = _tape_for("vvip_convoy_base")
        bds = [
            f.payload for f in tape.frames
            if f.kind == "scheduled_event" and f.payload["type"] == "vehicle_breakdown"
        ]
        assert len(bds) == 1  # one planted breakdown (contract)

    def test_event_config_carries_sixth_multiplier(self):
        tape, _ = _tape_for("motera_stadium_egress")
        ec = next(f.payload for f in tape.frames if f.kind == "event_config")
        assert "ambulance_rate_multiplier" in ec
        assert ec["ambulance_rate_multiplier"] > 1.0  # EVs scheduled → rate up
        assert ec["event_overrides"] == {}  # motera config verbatim

    def test_flood_weather_ramp_reaches_steady(self):
        tape, _ = _tape_for("flood_evacuation_corridor", tick_sec=60.0)
        docs = [f.payload for f in tape.frames if f.kind == "inputs_doc"]
        assert docs[-1]["weather_severity"] > 0.5
        assert docs[-1].get("weather") in ("RAIN", "STORM")

    def test_event_row_lifecycle(self):
        tape, _ = _tape_for("metro_work_zone", tick_sec=60.0)
        rows = [f for f in tape.frames if f.kind == "event_row"]
        actions = [r.payload["action"] for r in rows]
        assert actions[0] == "create"
        assert actions[-1] == "complete"
        horizon = load_library()["metro_work_zone"].horizon_sec
        assert rows[-1].t_sec == pytest.approx(horizon)

    def test_agency_units_refresh_inside_the_c_a_ttl(self):
        tape, _ = _tape_for("watchlist_anpr_sweep", tick_sec=60.0)
        by_unit: dict[str, list[float]] = {}
        for f in tape.frames:
            if f.kind == "agency_unit":
                by_unit.setdefault(f.payload["unit_id"], []).append(f.t_sec)
        assert by_unit
        assert AGENCY_CADENCE_SEC <= 25.0
        for unit, ts in by_unit.items():
            gaps = [b - a for a, b in zip(ts, ts[1:])]
            assert max(gaps) <= 25.0, unit

    def test_frames_ordered_and_numbered(self):
        tape, _ = _tape_for("kumbh_scale_gathering", tick_sec=60.0)
        ts = [f.t_sec for f in tape.frames]
        assert ts == sorted(ts)
        assert [f.frame for f in tape.frames] == list(range(len(tape.frames)))

    def test_tape_roundtrip(self):
        import json

        tape, _ = _tape_for("national_day_parade", tick_sec=60.0, run_id=9)
        again = type(tape).from_dict(json.loads(tape.to_json()))
        assert again.to_json() == tape.to_json()
        assert again.run_id == 9
