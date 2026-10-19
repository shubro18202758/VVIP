"""Tests for the simulation twin: event logic, determinism, TOD/DOW realism,
forecast coupling, TwinEnv, router contracts, and scenario-library integrity
(including consumption by the frozen impact harness).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from traffic_oracle.simulation import (
    CALIBRATION,
    ClosurePolicy,
    EventConfig,
    EventType,
    SeededRng,
    SimulationEngine,
    TwinEnv,
    default_network,
    derive_seed,
    get_scenario,
    list_scenarios,
    run_replication,
    sample_events,
)
from traffic_oracle.simulation.engine import FORECAST_HORIZONS
from traffic_oracle.simulation.network import from_seed_sql
from traffic_oracle.simulation.router import router as sim_router
from traffic_oracle.simulation.scenarios import export_library
from traffic_oracle.simulation.tod import (
    WeatherState,
    hour_demand_multiplier,
    infer_weather,
    waterlogging_rate_multiplier,
    weather_speed_factor,
)

REPO = Path(__file__).resolve().parents[3]  # VVIP/
SEED_SQL = REPO / "services" / "corridor-store" / "seed" / "sample_corridor.sql"


# ─── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def network():
    return default_network()


@pytest.fixture()
def engine(network):
    return SimulationEngine(network=network)


def _twin_payload(**over):
    payload = {
        "route_segment_ids": [1001, 1002, 1009],
        "departure_time": "2026-10-13T13:40:00+05:30",
        "closure_policy": {"advance_closure_sec": 120, "closure_type": "full_closure"},
        "horizon_min": 45,
        "n_replications": 3,
        "seed": 4242,
    }
    payload.update(over)
    return payload


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(sim_router)
    app.state.pool = None
    app.state.forecaster = None
    CALIBRATION.calibrated = False
    with TestClient(app) as c:
        yield c


# ─── determinism ─────────────────────────────────────────────────────────────


class TestDeterminism:
    def test_same_seed_same_metrics(self, engine):
        t0 = datetime.fromisoformat("2026-10-13T13:40:00+05:30")
        kwargs = dict(
            route_segment_ids=[1001, 1002, 1009],
            departure_time=t0,
            closure_policy=ClosurePolicy(120, "full_closure"),
            horizon_min=45,
            n_replications=4,
            seed=987,
        )
        r1 = [r.to_dict() for r in engine.run(**kwargs)]
        r2 = [r.to_dict() for r in engine.run(**kwargs)]
        assert r1 == r2

    def test_different_seed_different_outcome(self, engine):
        t0 = datetime.fromisoformat("2026-10-13T18:30:00+05:30")
        a = engine.run(route_segment_ids=[1001, 1002], departure_time=t0,
                       closure_policy=ClosurePolicy(120, "full_closure"),
                       horizon_min=45, n_replications=6, seed=1)
        b = engine.run(route_segment_ids=[1001, 1002], departure_time=t0,
                       closure_policy=ClosurePolicy(120, "full_closure"),
                       horizon_min=45, n_replications=6, seed=2)
        # With stochastic events enabled, at least one replication must differ.
        assert [r.to_dict() for r in a] != [r.to_dict() for r in b]

    def test_zero_events_is_seed_independent(self, engine):
        """With all road-event rates zeroed, the queueing core is a pure
        function of (route, clock, policy) — ambulance crossings are an
        exogenous Poisson stream and MAY differ, so they are projected out."""
        t0 = datetime.fromisoformat("2026-10-13T13:40:00+05:30")
        no_events = EventConfig(
            breakdown_rate_multiplier=0.0,
            waterlogging_rate_multiplier=0.0,
            signal_fault_rate_multiplier=0.0,
            market_rate_multiplier=0.0,
        )

        def core(rs):
            return [
                {k: v for k, v in r.to_dict().items()
                 if k not in ("ambulance_delay_sec", "n_ambulances_delayed", "seed")}
                for r in rs
            ]

        a = engine.run(route_segment_ids=[1001, 1002, 1009], departure_time=t0,
                       closure_policy=ClosurePolicy(120, "full_closure"),
                       horizon_min=30, n_replications=2, seed=11,
                       event_config=no_events)
        b = engine.run(route_segment_ids=[1001, 1002, 1009], departure_time=t0,
                       closure_policy=ClosurePolicy(120, "full_closure"),
                       horizon_min=30, n_replications=2, seed=99999,
                       event_config=no_events)
        assert core(a) == core(b)

    def test_rng_stream_reproducible_and_bounded(self):
        a = SeededRng(42)
        b = SeededRng(42)
        draws_a = [a.uniform() for _ in range(50)]
        draws_b = [b.uniform() for _ in range(50)]
        assert draws_a == draws_b
        r = SeededRng(7)
        assert all(0.0 <= r.uniform() <= 1.0 for _ in range(100))
        assert r.poisson(0) == 0
        for _ in range(50):
            v = r.lognormal(3.0, 0.5)
            assert v > 0 and v < 1e9
            assert 0 <= r.poisson(4.0) <= 40
        with pytest.raises(ValueError):
            SeededRng(1).expovariate(0.0)

    def test_derive_seed_stable(self):
        assert derive_seed("scenario", 7, 3) == derive_seed("scenario", 7, 3)
        assert derive_seed("a") != derive_seed("b")
        assert 0 <= derive_seed("x") < (1 << 63)

    def test_scheduled_events_resolve_deterministically(self, network):
        from traffic_oracle.simulation.events import ScheduledEvent

        sched = ScheduledEvent(
            event_type=EventType.BREAKDOWN, segment_id=1001,
            start_sec=60.0, duration_sec=900.0, severity=0.9, label="planted",
        )
        e1 = sched.resolve(SeededRng(1), network.require(1001))
        e2 = sched.resolve(SeededRng(1), network.require(1001))
        assert (e1.start_sec, e1.duration_sec, e1.lanes_blocked) == (
            e2.start_sec, e2.duration_sec, e2.lanes_blocked
        )
        assert e1.lanes_blocked >= 1  # severity 0.9 → 2 lanes capped by lanes-1


# ─── time-of-day / day-of-week / weather realism ────────────────────────────


class TestTodRealism:
    def test_dual_peaks_beat_overnight(self):
        tue = 1
        assert hour_demand_multiplier(9, tue) > hour_demand_multiplier(3, tue) * 2
        assert hour_demand_multiplier(18, tue) > hour_demand_multiplier(3, tue) * 2

    def test_evening_peak_heaviest_weekday(self):
        tue = 1
        assert hour_demand_multiplier(18, tue) == max(
            hour_demand_multiplier(h, tue) for h in range(24)
        )

    def test_sunday_below_tuesday(self):
        for hour in (9, 13, 18):
            assert hour_demand_multiplier(hour, 6) < hour_demand_multiplier(hour, 1)

    def test_friday_night_extension(self):
        assert hour_demand_multiplier(22, 4) > hour_demand_multiplier(22, 1)

    def test_multiplier_bounds(self):
        for dow in range(7):
            for hour in range(24):
                m = hour_demand_multiplier(hour, dow)
                assert 0.0 < m <= 1.0

    def test_monsoon_flag_and_fog(self):
        assert infer_weather(date(2026, 7, 21), 10).monsoon is True
        assert infer_weather(date(2026, 2, 21), 10).monsoon is False
        assert infer_weather(date(2026, 12, 25), 7).fog is True
        assert infer_weather(date(2026, 12, 25), 13).fog is False

    def test_weather_speed_factors(self):
        clear = weather_speed_factor(WeatherState())
        rain = weather_speed_factor(WeatherState(monsoon=True, rain_intensity_mm_h=20.0))
        fog = weather_speed_factor(WeatherState(fog=True))
        assert clear == 1.0
        assert rain < clear
        assert fog == pytest.approx(0.55)
        assert weather_speed_factor(WeatherState(monsoon=True, rain_intensity_mm_h=60.0)) > 0.35

    def test_waterlogging_rate_seasonality(self):
        dry = waterlogging_rate_multiplier(WeatherState())
        wet = waterlogging_rate_multiplier(
            WeatherState(monsoon=True, rain_intensity_mm_h=25.0)
        )
        assert dry < 0.05
        assert wet > dry * 50

    def test_demand_drives_disruption(self, engine):
        """Evening peak run must disrupt more than the same run at 02:30."""
        peak = datetime.fromisoformat("2026-10-13T18:30:00+05:30")
        night = datetime.fromisoformat("2026-10-14T02:30:00+05:30")
        no_events = EventConfig(
            breakdown_rate_multiplier=0.0, waterlogging_rate_multiplier=0.0,
            signal_fault_rate_multiplier=0.0, market_rate_multiplier=0.0,
        )
        d_peak = engine.run(route_segment_ids=[1001, 1002], departure_time=peak,
                            closure_policy=ClosurePolicy(120, "full_closure"),
                            horizon_min=30, n_replications=1, seed=5,
                            event_config=no_events)[0].total_disruption_vehicle_hours
        d_night = engine.run(route_segment_ids=[1001, 1002], departure_time=night,
                             closure_policy=ClosurePolicy(120, "full_closure"),
                             horizon_min=30, n_replications=1, seed=5,
                             event_config=no_events)[0].total_disruption_vehicle_hours
        assert d_peak > d_night * 3


# ─── event logic ─────────────────────────────────────────────────────────────


class TestEventLogic:
    def _sample(self, network, seed=1, weather=None, config=None, segments=None,
                window=3600.0):
        return sample_events(
            SeededRng(seed), network,
            segments or [1001, 1002, 1009, 1005, 1013],
            window, datetime.fromisoformat("2026-10-13T13:40:00+05:30"),
            config or EventConfig(), weather or WeatherState(),
        )

    def test_sample_events_deterministic(self, network):
        s1 = self._sample(network, seed=77)
        s2 = self._sample(network, seed=77)
        assert [(e.to_dict()) for e in s1.events] == [(e.to_dict()) for e in s2.events]

    def test_disable_all_yields_empty(self, network):
        s = self._sample(network, config=EventConfig(disable_all=True))
        assert len(s) == 0

    def test_waterlogging_gated_by_monsoon(self, network):
        """Dry-season waterlogging rate multiplier is 0.02× — over many seeds,
        essentially zero waterlogging events appear outside monsoon."""
        dry_hits = wet_hits = 0
        for seed in range(30):
            dry = self._sample(network, seed=seed,
                               weather=WeatherState(monsoon=False))
            wet = self._sample(network, seed=seed,
                               weather=WeatherState(monsoon=True, rain_intensity_mm_h=20.0))
            dry_hits += sum(1 for e in dry.events if e.event_type is EventType.WATERLOGGING)
            wet_hits += sum(1 for e in wet.events if e.event_type is EventType.WATERLOGGING)
        assert dry_hits <= 2            # near-zero outside monsoon
        assert wet_hits > dry_hits      # strictly more inside monsoon

    def test_low_lying_segments_flood_more(self, network):
        """Low-lying 1001/1002/1015 vs elevated 1017/1018 under identical rain."""
        monsoon = WeatherState(monsoon=True, rain_intensity_mm_h=25.0)
        low = high = 0
        for seed in range(40):
            s_low = sample_events(SeededRng(seed), network, [1001, 1002, 1015],
                                  3600.0, datetime.fromisoformat("2026-07-21T18:30:00+05:30"),
                                  EventConfig(waterlogging_rate_multiplier=3.0), monsoon)
            s_high = sample_events(SeededRng(seed), network, [1017, 1018],
                                   3600.0, datetime.fromisoformat("2026-07-21T18:30:00+05:30"),
                                   EventConfig(waterlogging_rate_multiplier=3.0), monsoon)
            low += sum(1 for e in s_low.events if e.event_type is EventType.WATERLOGGING)
            high += sum(1 for e in s_high.events if e.event_type is EventType.WATERLOGGING)
        assert low > high

    def test_signal_faults_only_on_signalized_segments(self, network):
        segs = [1001, 1002, 1003, 1004, 1005, 1009, 1017, 1018]
        for seed in range(20):
            s = sample_events(
                SeededRng(seed), network, segs, 7200.0,
                datetime.fromisoformat("2026-10-13T09:00:00+05:30"),
                EventConfig(signal_fault_rate_multiplier=40.0),  # force plenty
                WeatherState(),
            )
            for e in s.events:
                if e.event_type is EventType.SIGNAL_FAULT:
                    assert network.require(e.segment_id).signalized

    def test_market_events_only_on_market_segments(self, network):
        for seed in range(20):
            s = sample_events(
                SeededRng(seed), network, [1005, 1012, 1013, 1001, 1002], 7200.0,
                datetime.fromisoformat("2026-10-18T11:00:00+05:30"),  # Sunday
                EventConfig(market_rate_multiplier=15.0), WeatherState(),
            )
            for e in s.events:
                if e.event_type is EventType.MARKET:
                    assert network.require(e.segment_id).is_market_adjacent

    def test_rally_events_carry_demand_surge(self, network):
        from traffic_oracle.simulation.events import ScheduledEvent

        e = ScheduledEvent(
            EventType.RALLY, 1006, 0.0, 3600.0, severity=0.9,
            extra_demand_veh_h=900.0, label="riverfront rally",
        ).resolve(SeededRng(3), network.require(1006))
        assert e.extra_demand_veh_h >= 900.0
        assert e.capacity_factor == 1.0  # pure demand shock, no blockage

    def test_forced_events_present_in_scenario_run(self, engine):
        sc = get_scenario("multi_event_collision_kalupur")
        res = engine.run(
            route_segment_ids=list(sc.route_segment_ids),
            departure_time=sc.departure_dt(),
            closure_policy=ClosurePolicy(**sc.closure_policy),
            horizon_min=sc.horizon_min, n_replications=2, seed=sc.seed,
            weather=sc.weather_state(), event_config=sc.event_config(),
            scheduled_events=sc.scheduled_events(),
        )
        for r in res:
            sched = [e for e in r.events if e["source"] == "scheduled"]
            types = {e["type"] for e in sched}
            assert {"vehicle_breakdown", "signal_fault", "waterlogging"} <= types


# ─── engine mechanics ────────────────────────────────────────────────────────


class TestEngine:
    def _run(self, engine, **over):
        kwargs = dict(
            route_segment_ids=[1001, 1002, 1009],
            departure_time=datetime.fromisoformat("2026-10-13T13:40:00+05:30"),
            closure_policy=ClosurePolicy(120, "full_closure"),
            horizon_min=30, n_replications=2, seed=31,
        )
        kwargs.update(over)
        return engine.run(**kwargs)

    def test_c5_metric_keys_present(self, engine):
        for r in self._run(engine):
            d = r.to_dict()
            for key in (
                "replication", "total_disruption_vehicle_hours",
                "max_queue_length_m", "avg_closure_duration_sec",
                "convoy_transit_time_sec", "recovery_sec", "ambulance_delay_sec",
            ):
                assert key in d, f"missing C5 field {key}"
            assert d["replication"] >= 0
            assert d["total_disruption_vehicle_hours"] >= 0
            assert d["max_queue_length_m"] >= 0

    def test_horizon_and_replication_bounds(self, engine):
        with pytest.raises(ValueError):
            self._run(engine, horizon_min=61)
        with pytest.raises(ValueError):
            self._run(engine, n_replications=21)
        with pytest.raises(ValueError):
            ClosurePolicy(120, "bogus_type")
        with pytest.raises(ValueError):
            ClosurePolicy(-5, "full_closure")

    def test_full_closure_worse_than_signal_hold(self, engine):
        full = self._run(engine, closure_policy=ClosurePolicy(120, "full_closure"))
        hold = self._run(engine, closure_policy=ClosurePolicy(120, "signal_hold"))
        assert sum(r.total_disruption_vehicle_hours for r in full) > sum(
            r.total_disruption_vehicle_hours for r in hold
        )

    def test_longer_advance_extends_closure_duration(self, engine):
        short = self._run(engine, closure_policy=ClosurePolicy(60, "full_closure"))
        long = self._run(engine, closure_policy=ClosurePolicy(180, "full_closure"))
        assert long[0].avg_closure_duration_sec > short[0].avg_closure_duration_sec
        # Difference is bounded by the advance delta (120 s): early segments
        # clamp their closure start at t=0 (max(0, entry − advance)), so the
        # extension is partially clipped on short lead-ins.
        diff = long[0].avg_closure_duration_sec - short[0].avg_closure_duration_sec
        assert 0.0 < diff <= 120.0 + 1e-6

    def test_empty_route_returns_zeroed_result(self, engine):
        r = self._run(engine, route_segment_ids=[], n_replications=1)
        assert r[0].total_disruption_vehicle_hours == 0.0
        assert r[0].convoy_transit_time_sec == 0.0

    def test_forecast_coupling_consumes_dstgat_shapes(self, network):
        """Congested DSTGAT horizon shapes must raise simulated disruption."""
        t0 = datetime.fromisoformat("2026-10-13T13:40:00+05:30")
        base = run_replication(
            replication=0, network=network, route_segment_ids=[1001, 1002, 1009],
            departure_time=t0, closure_policy=ClosurePolicy(120, "full_closure"),
            horizon_sec=2700.0, seed=17,
        )
        flat_shapes = {
            sid: {h: {"speed_kmh": 45.0, "congestion_idx": 0.2}
                  for h in FORECAST_HORIZONS}
            for sid in (1001, 1002, 1009)
        }
        hot_shapes = {
            sid: {h: {"speed_kmh": 18.0, "congestion_idx": 0.85}
                  for h in FORECAST_HORIZONS}
            for sid in (1001, 1002, 1009)
        }
        flat = run_replication(
            replication=0, network=network, route_segment_ids=[1001, 1002, 1009],
            departure_time=t0, closure_policy=ClosurePolicy(120, "full_closure"),
            horizon_sec=2700.0, seed=17, forecast_shapes=flat_shapes,
        )
        hot = run_replication(
            replication=0, network=network, route_segment_ids=[1001, 1002, 1009],
            departure_time=t0, closure_policy=ClosurePolicy(120, "full_closure"),
            horizon_sec=2700.0, seed=17, forecast_shapes=hot_shapes,
        )
        assert flat.total_disruption_vehicle_hours < hot.total_disruption_vehicle_hours
        assert base.total_disruption_vehicle_hours < hot.total_disruption_vehicle_hours

    def test_forecast_feedback_emits_dstgat_shaped_knots(self, engine):
        r = self._run(engine, horizon_min=45, n_replications=1)[0]
        assert set(r.forecast_feedback) == {1001, 1002, 1009}
        for sid, knots in r.forecast_feedback.items():
            assert set(knots) <= set(FORECAST_HORIZONS)
            assert 5 in knots  # first knot always captured
            for h, vals in knots.items():
                assert set(vals) == {"speed_kmh", "congestion_idx"}
                assert 0.0 <= vals["congestion_idx"] <= 1.0
                assert vals["speed_kmh"] > 0

    def test_ambulance_delay_positive_during_peak_closure(self, engine):
        r = self._run(
            engine,
            departure_time=datetime.fromisoformat("2026-10-13T18:30:00+05:30"),
            n_replications=6,
        )
        assert any(x.ambulance_delay_sec > 0 for x in r)
        assert any(x.n_ambulances_delayed > 0 for x in r)


# ─── TwinEnv (plain python, no gymnasium) ───────────────────────────────────


class TestTwinEnv:
    def test_source_has_no_gymnasium_import(self):
        import traffic_oracle.simulation.twin as twin_mod

        src = Path(twin_mod.__file__).read_text(encoding="utf-8")
        assert "import gymnasium" not in src
        assert "from gymnasium" not in src
        # The import chain succeeded in this process; gymnasium is not
        # installed in the test venv, so a hard dependency would have failed
        # at collection time.
        assert "traffic_oracle.simulation.twin" in sys.modules

    def test_reset_step_contract(self):
        env = TwinEnv(route_segment_ids=[1001, 1002, 1009], max_epochs=3)
        assert env.is_stub is False
        obs = env.reset(seed=123)
        assert isinstance(obs, dict)
        assert obs["n_route_segments"] == 3
        assert len(obs["segment_congestion"]) == 3
        total_reward = 0.0
        for i in range(3):
            obs, reward, done, info = env.step(i % 9)
            assert isinstance(obs, dict)
            assert isinstance(reward, float)
            assert isinstance(done, bool)
            assert isinstance(info, dict)
            assert "metrics" in info and "forecast_feedback" in info
            total_reward += reward
        assert done is True

    def test_dict_action_and_determinism(self):
        runs = []
        for _ in range(2):
            env = TwinEnv(route_segment_ids=[1001, 1002], max_epochs=2)
            env.reset(seed=55)
            out = []
            done = False
            while not done:
                obs, reward, done, info = env.step(
                    {"advance_closure_sec": 60, "closure_type": "signal_hold"}
                )
                out.append((round(reward, 6), info["action"]))
            runs.append(out)
        assert runs[0] == runs[1]

    def test_bad_action_rejected(self):
        env = TwinEnv(route_segment_ids=[1001], max_epochs=1)
        env.reset(seed=1)
        with pytest.raises(ValueError):
            env.step(99)
        with pytest.raises(ValueError):
            env.step({"advance_closure_sec": 10, "closure_type": "nope"})
        with pytest.raises(TypeError):
            env.step("full_closure")
        with pytest.raises(RuntimeError):
            TwinEnv(route_segment_ids=[1001]).step(0)  # reset() first


# ─── router (contract C5) ────────────────────────────────────────────────────


class TestRouter:
    def test_twin_endpoint_returns_c5_shape(self, client):
        resp = client.post("/api/v1/simulate/twin", json=_twin_payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["calibrated"] is False  # fail-closed until replay
        assert len(body["runs"]) == 3
        for run in body["runs"]:
            for key in (
                "replication", "total_disruption_vehicle_hours", "max_queue_length_m",
                "avg_closure_duration_sec", "convoy_transit_time_sec",
                "recovery_sec", "ambulance_delay_sec",
            ):
                assert key in run
        assert body["seed"] == 4242
        assert body["forecast_coupling"] == "internal"

    def test_validation_bounds(self, client):
        assert client.post(
            "/api/v1/simulate/twin", json=_twin_payload(horizon_min=61)
        ).status_code == 422
        assert client.post(
            "/api/v1/simulate/twin", json=_twin_payload(n_replications=21)
        ).status_code == 422
        assert client.post(
            "/api/v1/simulate/twin", json=_twin_payload(
                closure_policy={"advance_closure_sec": 10, "closure_type": "weird"})
        ).status_code == 422
        assert client.post(
            "/api/v1/simulate/twin", json=_twin_payload(route_segment_ids=[])
        ).status_code == 422

    def test_scenario_endpoint_run(self, client):
        # Contract C2: the scenario resolves ONLY fields the request left
        # unset.  An explicit route WINS over the scenario (the pre-fix
        # router clobbered it — red-team C2, live-reproduced).
        resp = client.post("/api/v1/simulate/twin", json={
            "route_segment_ids": [1001],  # explicit — must NOT be clobbered
            "departure_time": "2026-01-01T00:00:00+05:30",
            "scenario_id": "monsoon_evening_peak",
            "n_replications": 2,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["scenario_id"] == "monsoon_evening_peak"
        assert body["route_segment_ids"] == [1001]
        assert body["horizon_min"] == 60
        assert body["seed"] == 20260721  # unset -> scenario seed applied
        # Scheduled underpass waterlogging present in every replication.
        for run in body["runs"]:
            sched = [e for e in run["events"] if e["source"] == "scheduled"]
            assert any(e["type"] == "waterlogging" for e in sched)

    def test_scenario_only_post_needs_no_dummy_fields(self, client):
        # Contract C2: a scenario-only POST works without dummy required
        # route/departure fields (pre-fix it 422'd on them).
        resp = client.post("/api/v1/simulate/twin", json={
            "scenario_id": "monsoon_evening_peak",
            "n_replications": 2,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["route_segment_ids"] == [1028, 1027, 1024, 1014]
        assert body["closure_policy"]["advance_closure_sec"] == 180
        assert body["closure_policy"]["closure_type"] == "full_closure"

    def test_explicit_policy_wins_over_scenario(self, client):
        # Contract C2 regression: monsoon_evening_peak + explicit
        # {advance 240, partial_closure} must echo {240, partial_closure}
        # (pre-fix it echoed the scenario's {180, full_closure}).
        resp = client.post("/api/v1/simulate/twin", json={
            "scenario_id": "monsoon_evening_peak",
            "closure_policy": {"advance_closure_sec": 240,
                               "closure_type": "partial_closure"},
            "n_replications": 2,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["closure_policy"]["advance_closure_sec"] == 240
        assert body["closure_policy"]["closure_type"] == "partial_closure"

    def test_twin_compare_endpoint(self, client):
        resp = client.post("/api/v1/simulate/twin/compare", json={
            "scenario_id": "baseline_weekday_midday",
            "candidate_policy": {"advance_closure_sec": 180,
                                 "closure_type": "full_closure"},
            "n_replications": 2,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["scenario_id"] == "baseline_weekday_midday"
        # baseline arm defaults to the scenario's policy (120/full here)
        assert body["policies"]["baseline"] == {
            "advance_closure_sec": 120, "closure_type": "full_closure"}
        assert body["policies"]["candidate"] == {
            "advance_closure_sec": 180, "closure_type": "full_closure"}
        assert len(body["baseline"]["runs"]) == 2
        assert len(body["candidate"]["runs"]) == 2
        assert set(body["deltas"]) == {
            "total_disruption_vehicle_hours", "max_queue_length_m",
            "avg_closure_duration_sec", "convoy_transit_time_sec",
            "recovery_sec", "ambulance_delay_sec",
        }
        for key in body["deltas"]:
            expected = round(
                body["candidate"]["summary"][key] - body["baseline"]["summary"][key], 3
            )
            assert body["deltas"][key] == expected

    def test_twin_compare_requires_candidate_policy(self, client):
        resp = client.post("/api/v1/simulate/twin/compare", json={
            "scenario_id": "baseline_weekday_midday",
        })
        assert resp.status_code == 422  # candidate_policy is required

    def test_twin_compare_unknown_scenario_404(self, client):
        resp = client.post("/api/v1/simulate/twin/compare", json={
            "scenario_id": "no_such_scenario",
            "candidate_policy": {"advance_closure_sec": 60,
                                 "closure_type": "signal_hold"},
        })
        assert resp.status_code == 404

    def test_unknown_scenario_404(self, client):
        resp = client.post("/api/v1/simulate/twin", json={
            "route_segment_ids": [1001],
            "departure_time": "2026-01-01T00:00:00+05:30",
            "scenario_id": "no_such_scenario",
        })
        assert resp.status_code == 404

    def test_determinism_through_http(self, client):
        a = client.post("/api/v1/simulate/twin", json=_twin_payload()).json()
        b = client.post("/api/v1/simulate/twin", json=_twin_payload()).json()
        assert a["runs"] == b["runs"]

    def test_replay_without_database_fails_closed(self, client):
        resp = client.post("/api/v1/simulate/replay", json={
            "window_start": "2026-10-01T00:00:00+05:30",
            "window_end": "2026-10-02T00:00:00+05:30",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "skipped_no_database"
        assert body["calibrated"] is False
        # and the twin still reports calibrated=false afterwards
        assert client.post("/api/v1/simulate/twin", json=_twin_payload()
                           ).json()["calibrated"] is False

    def test_replay_with_mock_data_calibrates(self):
        from traffic_oracle.simulation import calibration as calib
        from traffic_oracle.simulation.tod import hour_demand_multiplier, infer_weather, weather_speed_factor

        app = FastAPI()
        app.include_router(sim_router)
        net = default_network()

        # Build 48 aggregate rows whose avg_speed equals the model prediction
        # (segments × hours) — replay should drive adjusted MAE to ~0 and flip
        # the calibrated flag.
        rows = []
        base_hour = datetime.fromisoformat("2026-10-05T09:00:00+05:30")
        segs = [1001, 1002, 1003, 1004, 1005, 1009, 1016, 1017]
        for i, seg_id in enumerate(segs):
            seg = net.require(seg_id)
            for h in range(6):
                hour_dt = base_hour.replace(hour=9 + h)
                tod = hour_demand_multiplier(hour_dt.hour, hour_dt.weekday())
                w = infer_weather(hour_dt.date(), hour_dt.hour)
                ff = seg.free_flow_kmh * weather_speed_factor(w)
                vc = (seg.base_peak_demand_veh_h * tod) / max(seg.capacity_veh_h, 1.0)
                pred = ff / (1.0 + 0.55 * (vc ** 4.2))
                rows.append({
                    "segment_id": seg_id,
                    "hour_utc": hour_dt,
                    "avg_speed_kmh": pred,
                    "observation_cnt": 10,
                })

        class FakeConn:
            async def fetch(self, sql, *args):
                if "hourly_aggregates" in sql:
                    return rows
                if "signal_programs" in sql:
                    return [
                        {"junction_id": 1, "cycle_sec": 100,
                         "phases": [{"green_sec": 47}, {"green_sec": 38}],
                         "source": "calibrated"},
                    ]
                if "segment_adjacency" in sql:
                    return [{"segment_id": 1001, "junction_id": 1},
                            {"segment_id": 1002, "junction_id": 1}]
                return []

            async def execute(self, sql, *args):
                return "OK"

        class FakePool:
            def acquire(self):
                return self

            async def __aenter__(self):
                return FakeConn()

            async def __aexit__(self, *a):
                return None

            async def fetch(self, sql, *args):
                return await FakeConn().fetch(sql, *args)

        app.state.pool = FakePool()
        app.state.forecaster = None
        calib.CALIBRATION.calibrated = False

        with TestClient(app) as c:
            rep = c.post("/api/v1/simulate/replay", json={
                "window_start": "2026-10-05T00:00:00+05:30",
                "window_end": "2026-10-06T00:00:00+05:30",
            }).json()
            assert rep["status"] == "ok"
            assert rep["n_observations"] == len(rows)
            assert rep["calibrated"] is True
            assert rep["adjusted_speed_mae_kmh"] < 1.0

            # twin now reports calibrated=true, and signal programs were applied
            twin = c.post("/api/v1/simulate/twin", json=_twin_payload()).json()
            assert twin["calibrated"] is True
            assert twin.get("signal_programs_applied", 0) >= 1

        calib.CALIBRATION.calibrated = False  # restore fail-closed default


# ─── scenario library ────────────────────────────────────────────────────────


class TestScenarioLibrary:
    def test_at_least_twelve_scenarios(self):
        ids = list_scenarios()
        assert len(ids) >= 12
        assert len(set(ids)) == len(ids)  # unique

    def test_mandated_anchors_present(self):
        anchors = {
            "monsoon_evening_peak": ("monsoon", "evening_peak"),
            "multi_event_collision_kalupur": ("multi_event",),
            "zplus_airport_morning_peak": ("zplus_protocol", "morning_peak"),
        }
        for sid, features in anchors.items():
            sc = get_scenario(sid)
            for f in features:
                assert f in sc.expected_features

    def test_all_routes_exist_in_seed_network(self, network):
        for sid in list_scenarios():
            sc = get_scenario(sid)
            for seg_id in sc.route_segment_ids:
                assert network.get(seg_id) is not None, f"{sid}: unknown segment {seg_id}"
            assert sc.horizon_min <= 60
            assert 1 <= sc.n_replications <= 20
            assert sc.vvip_class in {"Z+", "Z", "Y", "X"}

    def test_export_library_serializable(self):
        payload = export_library()
        blob = json.dumps(payload)  # must not raise
        assert payload["n_scenarios"] == len(list_scenarios())
        rt = json.loads(blob)
        assert rt["network"]["n_segments"] >= 35  # seed network parsed
        seg_ids = {s["segment_id"] for s in rt["network"]["segments"]}
        for sc in rt["scenarios"]:
            assert set(sc["route_segment_ids"]) <= seg_ids

    def test_impact_harness_consumes_library(self):
        """The FROZEN scripts/impact_run.py must be able to consume the
        library: load it as a module, then evaluate each scenario's closure
        policy through its own commuter_delay_min on its own network facts."""
        script = REPO / "scripts" / "impact_run.py"
        assert script.exists()
        spec = importlib.util.spec_from_file_location("impact_run_frozen", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        payload = export_library()
        n_junctions = payload["network"]["n_signalized_junctions"]
        mean_lanes = (
            sum(s["lanes"] for s in payload["network"]["segments"])
            / len(payload["network"]["segments"])
        )
        lam = mod.DENSITY_VEH_PER_KM_PER_LANE * mod.EFFECTIVE_APPROACH_KMH / 60.0 * mean_lanes

        evaluated = 0
        for sc in payload["scenarios"]:
            hold_min = (
                sc["closure_policy"]["advance_closure_sec"] / 60.0
                + 2.5  # platform rolling-hold body
            )
            base = mod.commuter_delay_min(mod.BASELINE_HOLD_MIN, lam, n_junctions)
            plat = mod.commuter_delay_min(hold_min, lam, n_junctions)
            assert plat < base  # rolling hold beats blanket freeze, per scenario
            assert base / plat > 5.0
            evaluated += 1
        assert evaluated >= 12


# ─── network parsing ─────────────────────────────────────────────────────────


class TestNetwork:
    def test_seed_parse_matches_impact_harness(self):
        """Segment count / junction count agree with the frozen harness parse."""
        spec = importlib.util.spec_from_file_location(
            "impact_run_net", REPO / "scripts" / "impact_run.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        harness_segments, harness_junctions = mod.parse_seed(SEED_SQL)
        net = from_seed_sql(SEED_SQL)
        assert len(net) == len(harness_segments)
        # unsignalized seed junctions: 3 roundabouts + J15 (unsignalized
        # Tilak Rd / Station Rd intersection — keeps the seed equal to live)
        assert net.n_signalized_junctions + 4 == harness_junctions
        for hs in harness_segments:
            seg = net.require(hs["id"])
            assert seg.length_m == pytest.approx(hs["length_m"], abs=1.0)
            assert seg.lanes == hs["lanes"]
            assert seg.speed_limit_kmh == hs["speed_kmh"]

    def test_adjacency_symmetric(self, network):
        for a, nbrs in network.adjacency.items():
            for b in nbrs:
                assert a in network.adjacency.get(b, set())

    def test_capacity_and_demand_sane(self, network):
        for seg in network.segments.values():
            assert 0 < seg.capacity_veh_h
            assert 0 < seg.base_peak_demand_veh_h
            # peak demand at tod=1.0 stays under raw saturation capacity
            assert seg.base_peak_demand_veh_h <= 1800.0 * seg.lanes + 1e-6
