"""Library + profiles + tape tests: verbatim consumption, closed cause
vocabulary, and byte-level determinism (same spec+seed+t0 → identical tape)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from event_emulator.library import KNOWN_EVENT_TYPES, load_library
from event_emulator.profiles import EVENT_KINDS, PROFILES, profile_for
from event_emulator.spec import EmulationSpec, ResolvedRun
from event_emulator.tape import CAUSE_KINDS, build_tape, movement_uuid

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
PINNED = {
    "vvip_convoy_base": dict(seed=20261102, vvip="Z+", route=[1026, 1025, 1024, 1014, 1030]),
    "rath_yatra_ahmedabad": dict(seed=20250627, vvip="X", route=[1012, 1015, 1005, 1013]),
    "rath_yatra_monsoon_ambulance": dict(seed=202506271, vvip="X", route=[1005, 1015, 1006, 1001]),
    "kumbh_scale_gathering": dict(seed=20250129, vvip="X", route=[1001, 1006, 1010]),
    "riverfront_marathon": dict(seed=20241124, vvip="X", route=[1007, 1001, 1006, 1010]),
    "national_day_parade": dict(seed=20270126, vvip="Z", route=[1028, 1027, 1024, 1025]),
    "motera_stadium_egress": dict(seed=20260308, vvip="X", route=[1032, 1002, 1001]),
    "flood_evacuation_corridor": dict(seed=20240827, vvip="X", route=[1040, 1021, 1022, 1008]),
    "metro_work_zone": dict(seed=20250814, vvip="X", route=[1031, 1014, 1024]),
    "watchlist_anpr_sweep": dict(seed=20261105, vvip="X", route=[1012, 1015, 1005, 1013]),
}

T0 = datetime(2026, 10, 12, 6, 0, 0, tzinfo=timezone.utc)


def _tape_for(use_case_id: str, seed: int | None = None, tick_sec: float = 5.0):
    lib = load_library()
    uc = lib[use_case_id]
    spec = EmulationSpec(
        use_case_id=use_case_id, seed=seed, t0_anchor=T0, tick_sec=tick_sec
    )
    run = ResolvedRun.resolve(spec, uc)
    return build_tape(run, uc, profile_for(use_case_id)), uc


class TestLibraryVerbatim:
    def test_all_eight_present(self):
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

    def test_event_kinds_are_the_eight_archetypes(self):
        for p in PROFILES.values():
            assert p.event_kind in EVENT_KINDS

    def test_crowd_waypoints_in_range(self):
        for p in PROFILES.values():
            for _t, v in p.crowd_waypoints:
                assert 0.0 <= v <= 1.0


class TestTapeDeterminism:
    def test_same_spec_seed_t0_identical_bytes(self):
        a, _ = _tape_for("rath_yatra_ahmedabad")
        b, _ = _tape_for("rath_yatra_ahmedabad")
        assert a.to_json() == b.to_json()

    def test_different_seed_different_tape(self):
        a, _ = _tape_for("kumbh_scale_gathering", seed=1)
        b, _ = _tape_for("kumbh_scale_gathering", seed=2)
        assert a.to_json() != b.to_json()

    def test_seed_defaults_to_config_seed(self):
        tape, uc = _tape_for("vvip_convoy_base")
        assert tape.seed == uc.config["seed"]

    def test_all_eight_build(self):
        for uid in EXPECTED_IDS:
            tape, _ = _tape_for(uid, tick_sec=30.0)  # coarse for speed
            assert tape.frames, uid

    def test_cause_vocabulary_is_closed(self):
        tape, _ = _tape_for("motera_stadium_egress")
        kinds = {f.kind for f in tape.frames}
        assert kinds <= set(CAUSE_KINDS)
        assert "emergency_request" not in kinds  # deleted primitive (C4)


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

    def test_ambulance_projection_decays_during_ev_window(self):
        tape, _ = _tape_for("motera_stadium_egress", tick_sec=60.0)
        docs = sorted(
            (f for f in tape.frames if f.kind == "inputs_doc"),
            key=lambda f: f.t_sec,
        )
        during = [
            d.payload for d in docs
            if 360 <= d.t_sec <= 1440  # inside the first EV window (300+1200)
        ]
        assert during
        assert during[0]["ambulance_delay_projection_sec"] > \
            during[-1]["ambulance_delay_projection_sec"]
        assert all(
            15.0 <= p["ambulance_delay_projection_sec"] <= 120.0 for p in during
        )

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

    def test_deterministic_movement_identity(self):
        assert movement_uuid("rath_yatra_ahmedabad", 42) == \
            movement_uuid("rath_yatra_ahmedabad", 42)
        assert movement_uuid("rath_yatra_ahmedabad", 42) != \
            movement_uuid("rath_yatra_ahmedabad", 43)

    def test_frames_ordered_and_numbered(self):
        tape, _ = _tape_for("kumbh_scale_gathering", tick_sec=60.0)
        ts = [f.t_sec for f in tape.frames]
        assert ts == sorted(ts)
        assert [f.frame for f in tape.frames] == list(range(len(tape.frames)))

    def test_tape_roundtrip(self):
        import json

        tape, _ = _tape_for("national_day_parade", tick_sec=60.0)
        again = type(tape).from_dict(json.loads(tape.to_json()))
        assert again.to_json() == tape.to_json()
