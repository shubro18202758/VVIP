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


@pytest.fixture(autouse=True)
def _restore_calibration():
    """Every test sees (and leaves) the process-wide state it started with."""
    import copy

    saved = copy.deepcopy(CALIBRATION.__dict__)
    yield
    CALIBRATION.__dict__.clear()
    CALIBRATION.__dict__.update(saved)


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
        assert body["route_segment_ids"] == [1028, 1027, 1043, 1005, 1014]
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

    def test_replay_with_mock_data_calibrates_and_persists(self):
        from traffic_oracle.simulation import calibration as calib

        app = FastAPI()
        app.include_router(sim_router)
        pool = ReplayPool(rows=model_rows(scale=1.0))
        app.state.pool = pool
        app.state.forecaster = None
        calib.CALIBRATION.calibrated = False

        with TestClient(app) as c:
            rep = c.post("/api/v1/simulate/replay", json={
                "window_start": "2026-10-05T00:00:00+05:30",
                "window_end": "2026-10-06T00:00:00+05:30",
            }).json()
            assert rep["status"] == "ok"
            assert rep["n_observations"] == len(pool.rows)
            assert rep["calibrated"] is True
            assert rep["adjusted_speed_mae_kmh"] < 0.01  # rows ARE the model
            assert set(rep["per_road_class_capacity_scale"].values()) == {1.0}
            assert rep["signal_programs_applied"] >= 1
            assert rep["fit"] == "grid_min_mae_engine_formula"
            assert rep["time_basis"] == "IST bucket midpoint"
            # ledgered into simulation.calibrations (parameter-bound INSERT)
            assert rep["calibration_id"] == 7
            sql, args = pool.inserts[0]
            assert sql.startswith("INSERT INTO simulation.calibrations")
            assert args[2] == len(pool.rows) and args[6] is True

            # twin now reports calibrated=true, and signal programs were applied
            twin = c.post("/api/v1/simulate/twin", json=_twin_payload()).json()
            assert twin["calibrated"] is True
            assert twin.get("signal_programs_applied", 0) >= 1
            assert twin["signal_green_basis"] == "serving_phase_per_approach"


# ─── replay fakes (shared by the calibration tests below) ──────────────────


J1_PROGRAM = {
    "junction_id": 1, "cycle_sec": 100, "source": "osm_assumed",
    "phases": [{"green_sec": 47, "movements": ["through"]},
               {"green_sec": 38, "movements": ["cross"]}],
}
# Nehru Bridge - Ashram Road Junction legs as PostGIS reports them on the
# real (OpenStreetMap) network (bearing AWAY from the junction): Ashram Road
# S 1001 + N 1002 are the straight pair, Nehru Bridge Road 1015 crosses.
J1_LEGS = [
    {"junction_id": 1, "segment_id": 1001, "road_class": "primary", "bearing_deg": 176.2},
    {"junction_id": 1, "segment_id": 1002, "road_class": "primary", "bearing_deg": 353.3},
    {"junction_id": 1, "segment_id": 1015, "road_class": "secondary", "bearing_deg": 85.9},
]
REPLAY_SEGMENTS = (1001, 1002, 1003, 1004, 1005, 1009, 1016, 1017)


def replay_network():
    """The network the replay fits: seed + J1's per-approach greens."""
    from traffic_oracle.simulation.calibration import (
        apply_signal_overrides,
        approach_green_overrides,
    )

    net = default_network()
    apply_signal_overrides(net, approach_green_overrides([J1_PROGRAM], J1_LEGS))
    return net


def model_rows(scale: float = 1.0, hours: int = 24, noise=None):
    """Hourly aggregate rows equal to the twin's own speed at ``scale``
    (24 distinct field hours by default — the calibration floor)."""
    from traffic_oracle.simulation.calibration import bucket_midpoint, model_speed_kmh

    net = replay_network()
    base = datetime.fromisoformat("2026-10-05T03:00:00+00:00")  # 08:30 IST
    rows = []
    for i, seg_id in enumerate(REPLAY_SEGMENTS):
        for h in range(hours):
            hour_utc = base + timedelta(hours=h)
            speed = model_speed_kmh(net, seg_id, bucket_midpoint(hour_utc), scale)
            if noise is not None:
                speed += noise(i, h)
            rows.append({
                "segment_id": seg_id, "hour_utc": hour_utc,
                "avg_speed_kmh": speed, "observation_cnt": 10,
            })
    return rows


class ReplayPool:
    """asyncpg-shaped fake: aggregates, programs, legs; records INSERTs."""

    def __init__(self, rows=None, field_hours=0, has_real=False, has_synthetic=True,
                 latest=None):
        self.rows = list(rows or [])
        self.field_hours = field_hours
        self.has_real = has_real
        self.has_synthetic = has_synthetic
        self.latest = latest
        self.inserts: list[tuple[str, tuple]] = []
        self.fetchrow_args: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        if "hourly_aggregates" in sql:
            return self.rows
        if "ST_Azimuth" in sql:
            return J1_LEGS
        if "signal_programs" in sql:
            return [J1_PROGRAM]
        return []  # no DB-only segments / adjacency in these fakes

    async def fetchrow(self, sql, *args):
        self.fetchrow_args.append((sql, args))
        if sql.startswith("INSERT INTO simulation.calibrations"):
            self.inserts.append((sql, args))
            return {"calibration_id": 7, "created_at": datetime.now()}
        if "count(DISTINCT hour_utc)" in sql:
            return {"hours": self.field_hours, "first_hour": None, "last_hour": None}
        if "has_real" in sql:
            return {"has_real": self.has_real, "has_synthetic": self.has_synthetic}
        if "FROM simulation.calibrations" in sql:
            return self.latest
        return None


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

    def test_anchor_hazards_lie_on_the_convoy_route(self, network):
        """The monsoon and multi-event anchors stage every forced hazard ON
        the convoy's path (the Kalupur station approach 1014 and the Relief
        Road approach 1005 on the real network): a hazard off the route never
        meets the convoy, and its label would name a place it is not."""
        for sid in ("monsoon_evening_peak", "multi_event_collision_kalupur"):
            sc = get_scenario(sid)
            assert sc.forced_events, sid
            for fe in sc.forced_events:
                assert fe["segment_id"] in sc.route_segment_ids, (sid, fe["segment_id"])
                assert network.get(fe["segment_id"]) is not None, (sid, fe["segment_id"])
        waterlogged = {
            sid: [fe["segment_id"] for fe in get_scenario(sid).forced_events
                  if fe["type"] == "waterlogging"]
            for sid in ("monsoon_evening_peak", "multi_event_collision_kalupur")
        }
        assert waterlogged == {"monsoon_evening_peak": [1014],
                               "multi_event_collision_kalupur": [1005]}

    def test_the_monsoon_anchor_floods_a_low_lying_segment(self, network):
        """The low-lying set keeps a segment on the monsoon anchor's route:
        its scheduled waterlogging is on the Kalupur station approach
        (Station Road 1014), so the stochastic model floods the same place
        more readily.  1024 is the Gandhinagar Highway north of the Indira
        Bridge on the real network, about 10 km from Kalupur — not low-lying.
        The committed library (what the impact harness reads) says the same."""
        sc = get_scenario("monsoon_evening_peak")
        flooded = [fe["segment_id"] for fe in sc.forced_events if fe["type"] == "waterlogging"]
        assert flooded == [1014]
        assert network.require(1014).is_low_lying
        assert not network.require(1024).is_low_lying
        assert set(sc.route_segment_ids) & {
            sid for sid, seg in network.segments.items() if seg.is_low_lying
        } == {1014}
        committed = json.loads(
            (REPO / "scripts" / "scenario_library.json").read_text(encoding="utf-8"))
        flags = {s["segment_id"]: s["low_lying"] for s in committed["network"]["segments"]}
        assert {sid for sid, low in flags.items() if low} == {
            sid for sid, seg in network.segments.items() if seg.is_low_lying
        }

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
        # unsignalized seed junctions: 3 roundabouts + J15 (the unsignalized
        # Kalupur intersection where Relief Road, Gandhi Road, Station Road
        # and Sarangpur Road meet — keeps the seed equal to live)
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


# ─── time zone: equivalent instants, identical twin ─────────────────────────


class TestTimezoneEquivalence:
    IST_STAMP = "2026-10-22T09:00:00+05:30"
    UTC_STAMP = "2026-10-22T03:30:00+00:00"

    def test_tod_and_weather_read_ist(self):
        from traffic_oracle.simulation.tod import (
            infer_weather_at,
            time_of_day_demand,
            to_ist,
        )

        ist = datetime.fromisoformat(self.IST_STAMP)
        utc = datetime.fromisoformat(self.UTC_STAMP)
        naive = datetime.fromisoformat("2026-10-22T09:00:00")  # naive = IST
        assert time_of_day_demand(ist) == time_of_day_demand(utc)
        assert time_of_day_demand(naive) == time_of_day_demand(ist)
        assert infer_weather_at(utc) == infer_weather_at(ist)
        assert to_ist(utc).hour == 9 and to_ist(utc).minute == 0

    def test_equivalent_instants_give_identical_twin_metrics(self, engine):
        runs = {}
        for stamp in (self.IST_STAMP, self.UTC_STAMP):
            runs[stamp] = [
                r.to_dict() for r in engine.run(
                    route_segment_ids=[1001, 1002, 1009],
                    departure_time=datetime.fromisoformat(stamp),
                    closure_policy=ClosurePolicy(120, "full_closure"),
                    horizon_min=45, n_replications=3, seed=99,
                )
            ]
        assert runs[self.IST_STAMP] == runs[self.UTC_STAMP]
        # and it is the morning peak, not the 03:30 night trough
        assert runs[self.IST_STAMP][0]["total_disruption_vehicle_hours"] > 10.0

    def test_equivalent_instants_identical_through_http(self, client):
        a = client.post("/api/v1/simulate/twin",
                        json=_twin_payload(departure_time=self.IST_STAMP)).json()
        b = client.post("/api/v1/simulate/twin",
                        json=_twin_payload(departure_time=self.UTC_STAMP)).json()
        assert a["runs"] == b["runs"]
        assert a["departure_time"] == b["departure_time"] == self.IST_STAMP

    def test_calibration_buckets_are_read_at_their_ist_midpoint(self):
        from traffic_oracle.simulation.calibration import bucket_midpoint, model_speed_kmh

        net = default_network()
        hour_utc = datetime.fromisoformat("2026-10-22T03:00:00+00:00")
        mid = bucket_midpoint(hour_utc)
        assert mid.isoformat() == "2026-10-22T03:30:00+00:00"  # = 09:00 IST
        assert model_speed_kmh(net, 1001, mid) == model_speed_kmh(
            net, 1001, datetime.fromisoformat("2026-10-22T09:00:00+05:30")
        )


# ─── calibration: scale direction + MAE of the engine's own formula ─────────


class TestCalibrationFit:
    def _samples(self, scale):
        from traffic_oracle.simulation.calibration import _row_iter

        net = replay_network()
        return net, list(_row_iter(model_rows(scale=scale), net))

    def test_more_field_capacity_fits_a_scale_above_one(self):
        from traffic_oracle.simulation.calibration import fit_class_scales

        net, samples = self._samples(1.3)
        fits = fit_class_scales(samples, net)
        assert fits, "no classes fitted"
        for cls, fit in fits.items():
            assert fit["scale"] == pytest.approx(1.3, abs=0.011), cls
            assert fit["mae_after"] < fit["mae_before"] or fit["mae_before"] < 1e-9

    def test_less_field_capacity_fits_a_scale_below_one(self):
        from traffic_oracle.simulation.calibration import fit_class_scales

        net, samples = self._samples(0.8)
        fits = fit_class_scales(samples, net)
        sensitive = {c: f for c, f in fits.items() if f["mae_before"] > 0.05}
        assert sensitive, "the synthetic truth must move at least one class"
        for cls, fit in sensitive.items():
            assert fit["scale"] == pytest.approx(0.8, abs=0.011), cls
            assert fit["mae_after"] < fit["mae_before"]

    def test_fit_is_never_worse_than_uncalibrated(self):
        from traffic_oracle.simulation.calibration import _row_iter, fit_class_scales

        net = replay_network()
        rng = SeededRng(derive_seed("calibration-noise"))
        rows = model_rows(noise=lambda i, h: rng.uniform(-6.0, 6.0))
        fits = fit_class_scales(list(_row_iter(rows, net)), net)
        for fit in fits.values():
            assert fit["mae_after"] <= fit["mae_before"] + 1e-12

    def test_replay_calibrates_the_direction_end_to_end(self):
        import asyncio as _asyncio

        from traffic_oracle.simulation.calibration import run_replay

        pool = ReplayPool(rows=model_rows(scale=1.3))
        start = datetime.fromisoformat("2026-10-05T00:00:00+00:00")
        report = _asyncio.run(run_replay(pool, start, start + timedelta(days=1)))
        assert report["calibrated"] is True
        assert report["adjusted_speed_mae_kmh"] < report["speed_mae_kmh"]
        assert all(k >= 1.25 for k in report["per_road_class_capacity_scale"].values())
        assert CALIBRATION.calibrated is True
        assert CALIBRATION.capacity_scale >= 1.25

    def test_a_twin_that_does_not_fit_stays_uncalibrated_and_unscaled(self):
        import asyncio as _asyncio

        from traffic_oracle.simulation.calibration import run_replay

        rows = model_rows(noise=lambda i, h: 25.0 if (i + h) % 2 else -25.0)
        pool = ReplayPool(rows=rows)
        start = datetime.fromisoformat("2026-10-05T00:00:00+00:00")
        report = _asyncio.run(run_replay(pool, start, start + timedelta(days=1)))
        assert report["calibrated"] is False
        assert CALIBRATION.calibrated is False
        assert CALIBRATION.capacity_scale == 1.0
        assert CALIBRATION.per_road_class_scale == {}
        assert "does not fit" in (CALIBRATION.reason or "")

    def test_one_busy_hour_never_calibrates_however_well_it_fits(self):
        import asyncio as _asyncio

        from traffic_oracle.simulation.calibration import MIN_FIELD_HOURS, run_replay

        # 8 segments x 6 hours = 48 aggregates (>= 30) that ARE the model:
        # a perfect fit, but on 6 distinct field hours of the 24 required.
        pool = ReplayPool(rows=model_rows(hours=6))
        start = datetime.fromisoformat("2026-10-05T00:00:00+00:00")
        report = _asyncio.run(run_replay(pool, start, start + timedelta(days=1)))
        assert report["n_observations"] == 48
        assert report["adjusted_speed_mae_kmh"] < 0.01
        assert report["field_hours"] == 6
        assert report["min_field_hours"] == MIN_FIELD_HOURS == 24
        assert report["calibrated"] is False
        assert CALIBRATION.calibrated is False
        assert CALIBRATION.reason == "field history too thin: 6 field hours of 24 required"
        assert CALIBRATION.capacity_scale == 1.0

    def test_the_ledger_records_the_field_hours_behind_a_fit(self):
        import asyncio as _asyncio
        import json as _json

        from traffic_oracle.simulation.calibration import persist_calibration, run_replay

        pool = ReplayPool(rows=model_rows(scale=1.1))
        start = datetime.fromisoformat("2026-10-05T00:00:00+00:00")
        report = _asyncio.run(run_replay(pool, start, start + timedelta(days=1)))
        assert report["calibrated"] is True
        assert report["field_hours"] == 24
        assert "(24 field hours)" in (CALIBRATION.reason or "")
        assert _asyncio.run(persist_calibration(pool, report)) == 7
        _sql, args = pool.inserts[0]
        details = _json.loads(args[7])
        assert details["field_hours"] == 24 and details["min_field_hours"] == 24


# ─── per-approach greens from the serving phase (C6) ────────────────────────


class TestApproachGreens:
    def test_through_and_cross_get_their_serving_phase(self):
        from traffic_oracle.simulation.calibration import approach_green_overrides

        ov = approach_green_overrides([J1_PROGRAM], J1_LEGS)
        assert ov[1001]["green_ratio"] == pytest.approx(0.47)
        assert ov[1002]["green_ratio"] == pytest.approx(0.47)
        assert ov[1001]["movement"] == "through"
        assert ov[1015]["green_ratio"] == pytest.approx(0.38)
        assert ov[1015]["movement"] == "cross"
        # the old Σgreen/cycle (0.85) is never a segment's green
        assert all(v["green_ratio"] < 0.5 for v in ov.values())

    def test_a_segment_on_two_programmed_junctions_keeps_the_binding_green(self):
        from traffic_oracle.simulation.calibration import approach_green_overrides

        j7 = {"junction_id": 7, "cycle_sec": 100, "source": "osm_assumed",
              "phases": [{"green_sec": 47, "movements": ["through"]},
                         {"green_sec": 30, "movements": ["cross"]}]}
        legs7 = [
            {"junction_id": 7, "segment_id": 1002, "road_class": "secondary",
             "bearing_deg": 180.0},
            {"junction_id": 7, "segment_id": 1009, "road_class": "trunk",
             "bearing_deg": 345.5},
            {"junction_id": 7, "segment_id": 1041, "road_class": "trunk",
             "bearing_deg": 165.0},
        ]
        ov = approach_green_overrides([J1_PROGRAM, j7], J1_LEGS + legs7)
        # 1002 is J1's through (0.47) but J7's cross (0.30): binding = 0.30
        assert ov[1002]["green_ratio"] == pytest.approx(0.30)
        assert ov[1002]["junction_id"] == 7

    def test_compass_programs_map_by_approach_axis(self):
        from traffic_oracle.simulation.calibration import approach_green_overrides

        prog = {"junction_id": 9, "cycle_sec": 90, "source": "calibrated",
                "phases": [{"green_sec": 40, "movements": ["NS"]},
                           {"green_sec": 30, "movements": ["EW"]}]}
        legs = [
            {"junction_id": 9, "segment_id": 1, "road_class": "primary", "bearing_deg": 0.0},
            {"junction_id": 9, "segment_id": 2, "road_class": "primary", "bearing_deg": 90.0},
        ]
        ov = approach_green_overrides([prog], legs)
        assert ov[1]["movement"] == "NS" and ov[1]["green_ratio"] == pytest.approx(40 / 90)
        assert ov[2]["movement"] == "EW" and ov[2]["green_ratio"] == pytest.approx(30 / 90)

    def test_only_junction_legs_become_signalized(self):
        from traffic_oracle.simulation.calibration import (
            apply_signal_overrides,
            approach_green_overrides,
        )

        net = default_network()
        before = {sid: seg.signalized for sid, seg in net.segments.items()}
        n = apply_signal_overrides(net, approach_green_overrides([J1_PROGRAM], J1_LEGS))
        assert n == 3
        changed = {sid for sid, seg in net.segments.items() if seg.signalized != before[sid]}
        assert changed <= {1001, 1002, 1015}
        # Ashram Road (South): 4 lanes (OpenStreetMap: 2 per carriageway)
        assert net.require(1001).capacity_veh_h == pytest.approx(1800 * 4 * 0.47)


# ─── recovery vs the paired no-closure counterfactual ───────────────────────


class TestPairedRecovery:
    def _scenario_runs(self, scenario_id, engine):
        sc = get_scenario(scenario_id)
        return engine.run(
            route_segment_ids=list(sc.route_segment_ids),
            departure_time=sc.departure_dt(),
            closure_policy=ClosurePolicy(**sc.closure_policy),
            horizon_min=sc.horizon_min,
            n_replications=sc.n_replications,
            seed=sc.seed,
            weather=sc.weather_state(),
            event_config=sc.event_config(),
            scheduled_events=sc.scheduled_events(),
        )

    def test_off_peak_closure_recovers_and_is_not_censored(self, engine):
        runs = self._scenario_runs("night_owl_low_demand", engine)
        assert all(not r.censored and r.recovered for r in runs)
        d = runs[0].to_dict()
        assert d["censored"] is False
        assert d["recovery_basis"] == "paired_no_closure"
        assert 0.0 <= d["recovery_sec"] < 600.0

    def test_oversaturated_peak_is_censored_not_a_measurement(self, engine):
        runs = self._scenario_runs("monsoon_evening_peak", engine)
        assert all(r.censored and not r.recovered for r in runs)
        sc = get_scenario("monsoon_evening_peak")
        for r in runs:
            # censored lower bound: the horizon remainder after the closure
            assert r.recovery_sec <= sc.horizon_min * 60.0

    def test_compare_summary_counts_censored_runs(self, client):
        body = client.post("/api/v1/simulate/twin/compare", json={
            "scenario_id": "monsoon_evening_peak",
            "candidate_policy": {"advance_closure_sec": 60,
                                 "closure_type": "full_closure"},
            "n_replications": 2,
        }).json()
        assert body["baseline"]["summary"]["recovery_censored_runs"] == 2
        assert "recovery_censored_runs" not in body["deltas"]

    def test_signal_fault_now_cuts_signalized_capacity(self, network):
        from traffic_oracle.simulation.events import ScheduledEvent

        sid = next(s for s, seg in network.segments.items() if seg.signalized)
        t0 = datetime.fromisoformat("2026-10-13T13:40:00+05:30")
        common = dict(
            replication=0, network=network, route_segment_ids=[sid],
            departure_time=t0, closure_policy=ClosurePolicy(60, "signal_hold"),
            horizon_sec=1800.0, seed=5, event_config=EventConfig(disable_all=True),
        )
        calm = run_replication(**common)
        fault = run_replication(
            **common,
            scheduled_events=[ScheduledEvent(EventType.SIGNAL_FAULT, sid, 0.0, 1800.0, 0.9)],
        )
        assert fault.total_disruption_vehicle_hours > calm.total_disruption_vehicle_hours


# ─── forecast coupling + route coverage honesty ─────────────────────────────


class TestCouplingHonesty:
    def _app(self, forecaster):
        app = FastAPI()
        app.include_router(sim_router)
        app.state.pool = None
        app.state.forecaster = forecaster
        return TestClient(app)

    def test_empty_forecast_reports_internal_with_coverage(self):
        class EmptyForecaster:
            last_forecast_rung = "heuristic"

            async def predict(self, segment_ids, horizons_min=None):
                return {}

        body = self._app(EmptyForecaster()).post(
            "/api/v1/simulate/twin", json=_twin_payload()
        ).json()
        assert body["forecast_coupling"] == "internal"
        assert body["forecast_coverage"] == {"covered": 0, "route_segments": 3}

    def test_covered_route_names_the_producing_rung(self):
        class Meta:
            rung = "foundation"

        class FoundationForecaster:
            async def predict_with_meta(self, segment_ids, horizons_min=None):
                rows = {
                    sid: {h: {"speed_kmh": 40.0, "congestion_idx": 0.0}
                          for h in horizons_min}
                    for sid in segment_ids[:2]
                }
                return rows, Meta()

        body = self._app(FoundationForecaster()).post(
            "/api/v1/simulate/twin", json=_twin_payload()
        ).json()
        assert body["forecast_coupling"] == "foundation"
        assert body["forecast_coverage"] == {"covered": 2, "route_segments": 3}

    def test_true_zero_congestion_survives_shape_normalisation(self):
        from traffic_oracle.simulation.engine import shapes_from_forecaster

        shapes = shapes_from_forecaster(
            {7: {5: {"speed_kmh": 0.0, "congestion_idx": 0.0},
                 10: {"speed_kmh": 30.0, "congestion_idx": float("nan")}}}
        )
        assert shapes == {7: {5: {"speed_kmh": 0.0, "congestion_idx": 0.0}}}

    def test_route_coverage_says_what_the_twin_modelled(self, client):
        unknown = client.post("/api/v1/simulate/twin",
                              json=_twin_payload(route_segment_ids=[1, 2, 3])).json()
        assert unknown["route_coverage"] == {"modelled": 0, "requested": 3}
        known = client.post("/api/v1/simulate/twin", json=_twin_payload()).json()
        assert known["route_coverage"] == {"modelled": 3, "requested": 3}


# ─── calibration ledger + scheduler ─────────────────────────────────────────


class TestCalibrationLedgerAndScheduler:
    def test_reload_restores_a_recent_calibrated_row(self):
        import asyncio as _asyncio

        from traffic_oracle.simulation.calibration import reload_calibration

        CALIBRATION.mark_uncalibrated("test start")
        now = datetime.now().astimezone()
        pool = ReplayPool(latest={
            "calibration_id": 11, "window_start": now - timedelta(days=7),
            "window_end": now, "n_observations": 480, "speed_mae_kmh": 2.4,
            "capacity_scale": 1.12, "per_road_class_scale": '{"trunk": 1.12}',
            "calibrated": True, "details": "{}", "created_at": now,
        })
        assert _asyncio.run(reload_calibration(pool)) is True
        assert CALIBRATION.calibrated is True
        assert CALIBRATION.per_road_class_scale == {"trunk": 1.12}
        assert CALIBRATION.calibration_id == 11
        sql, args = pool.fetchrow_args[-1]
        assert "FROM simulation.calibrations WHERE created_at >= $1" in sql
        age = datetime.now().astimezone() - args[0]
        assert timedelta(days=6, hours=23) < age < timedelta(days=7, hours=1)

    def test_reload_of_a_failed_fit_keeps_the_twin_uncalibrated(self):
        import asyncio as _asyncio

        from traffic_oracle.simulation.calibration import reload_calibration

        now = datetime.now().astimezone()
        pool = ReplayPool(latest={
            "calibration_id": 12, "window_start": now, "window_end": now,
            "n_observations": 480, "speed_mae_kmh": 9.7, "capacity_scale": 1.0,
            "per_road_class_scale": "{}", "calibrated": False, "details": "{}",
            "created_at": now,
        })
        assert _asyncio.run(reload_calibration(pool)) is False
        assert CALIBRATION.calibrated is False
        assert "did not fit" in CALIBRATION.reason

    def test_reload_of_a_thin_history_replay_says_so_not_misfit(self):
        import asyncio as _asyncio
        import json as _json

        from traffic_oracle.simulation.calibration import reload_calibration

        now = datetime.now().astimezone()
        pool = ReplayPool(latest={
            "calibration_id": 13, "window_start": now, "window_end": now,
            "n_observations": 40, "speed_mae_kmh": 0.8, "capacity_scale": 1.0,
            "per_road_class_scale": "{}", "calibrated": False,
            "details": _json.dumps({"field_hours": 1, "min_field_hours": 24,
                                    "min_rows_for_calibration": 30}),
            "created_at": now,
        })
        assert _asyncio.run(reload_calibration(pool)) is False
        assert CALIBRATION.calibrated is False
        assert CALIBRATION.reason == (
            "last field replay #13 stood on 1 field hours of 24 required"
        )

    def test_simulated_feed_only_defers_with_the_explicit_reason(self):
        import asyncio as _asyncio

        from traffic_oracle.simulation.calibration import REASON_SIMULATED_ONLY
        from traffic_oracle.simulation.scheduler import CalibrationScheduler

        CALIBRATION.mark_uncalibrated("test start")
        pool = ReplayPool(rows=model_rows(), field_hours=0,
                          has_real=False, has_synthetic=True)
        result = _asyncio.run(CalibrationScheduler(pool).check_and_run())
        assert result["status"] == "waiting_for_field_data"
        assert CALIBRATION.calibrated is False
        assert CALIBRATION.reason == REASON_SIMULATED_ONLY
        assert REASON_SIMULATED_ONLY == "no field observations — simulated feed only"
        assert CALIBRATION.readiness() == (False, REASON_SIMULATED_ONLY)
        assert pool.inserts == []  # nothing replayed, nothing ledgered

    def test_partial_field_history_names_the_hours(self):
        import asyncio as _asyncio

        from traffic_oracle.simulation.scheduler import CalibrationScheduler

        CALIBRATION.mark_uncalibrated("test start")
        pool = ReplayPool(field_hours=5, has_real=True)
        _asyncio.run(CalibrationScheduler(pool).check_and_run())
        assert CALIBRATION.reason == "field history 5h of 24h required"

    def test_enough_field_hours_replay_and_ledger(self):
        import asyncio as _asyncio

        from traffic_oracle.simulation.scheduler import CalibrationScheduler

        CALIBRATION.mark_uncalibrated("test start")
        pool = ReplayPool(rows=model_rows(scale=1.1), field_hours=30, has_real=True)
        scheduler = CalibrationScheduler(pool)
        result = _asyncio.run(scheduler.check_and_run())
        assert result["status"] == "replay_ok"
        assert CALIBRATION.calibrated is True
        assert len(pool.inserts) == 1
        assert scheduler.health()["runs"] == 1

    def test_prime_makes_the_reason_final_before_the_first_decision(self):
        """Restart over a thin-history ledger row on a simulated-only feed:
        after the prime the twin quotes the explicit simulated-feed reason —
        not the reload's ledger reason, not the 'not yet checked' placeholder
        — before the situation engine makes its first decision."""
        import asyncio as _asyncio
        import json as _json

        from traffic_oracle.simulation.calibration import REASON_SIMULATED_ONLY
        from traffic_oracle.simulation.scheduler import CalibrationScheduler

        CALIBRATION.mark_uncalibrated("not yet checked for field observations")
        now = datetime.now().astimezone()
        pool = ReplayPool(field_hours=0, has_real=False, has_synthetic=True, latest={
            "calibration_id": 13, "window_start": now - timedelta(days=9),
            "window_end": now - timedelta(days=8), "n_observations": 40,
            "speed_mae_kmh": 11.39, "capacity_scale": 1.0,
            "per_road_class_scale": "{}", "calibrated": False,
            "details": _json.dumps({"field_hours": 1, "min_field_hours": 24,
                                    "min_rows_for_calibration": 30}),
            "created_at": now,
        })
        scheduler = CalibrationScheduler(pool)
        result = _asyncio.run(scheduler.prime())
        assert result["primed"] is True and result["status"] == "waiting_for_field_data"
        assert result["reason"] == REASON_SIMULATED_ONLY
        assert CALIBRATION.readiness() == (False, REASON_SIMULATED_ONLY)
        health = scheduler.health()
        assert health["primed"] is True and health["checks"] == 1
        assert health["reloaded_calibrated"] is False and health["prime_ms"] is not None
        assert pool.inserts == []  # no field hours: nothing replayed
        # The reloaded replay's facts are labelled apart from the coverage.
        state = CALIBRATION.to_dict()
        assert state["field_hours"] == 0
        assert state["last_replay"]["origin"] == "ledger"
        assert state["last_replay"]["calibration_id"] == 13
        assert state["last_replay"]["field_hours"] == 1
        assert state["last_replay"]["speed_mae_kmh"] == 11.39
        assert state["last_replay"]["calibrated"] is False

    def test_prime_replays_when_field_hours_suffice(self):
        import asyncio as _asyncio

        from traffic_oracle.simulation.scheduler import CalibrationScheduler

        CALIBRATION.mark_uncalibrated("not yet checked for field observations")
        pool = ReplayPool(rows=model_rows(scale=1.1), field_hours=30, has_real=True)
        scheduler = CalibrationScheduler(pool)
        result = _asyncio.run(scheduler.prime())
        assert result["primed"] is True and result["status"] == "replay_ok"
        assert result["calibrated"] is True and CALIBRATION.calibrated is True
        assert len(pool.inserts) == 1
        last = CALIBRATION.to_dict()["last_replay"]
        assert last["origin"] == "replayed" and last["field_hours"] == 24
        assert last["calibrated"] is True

    def test_prime_is_bounded_and_never_fatal(self):
        import asyncio as _asyncio
        import time as _time

        from traffic_oracle.simulation.scheduler import CalibrationScheduler

        class HangingPool(ReplayPool):
            """A database that stops answering.  Like asyncpg, cancelling
            the hung query ALSO waits on the server (cancel request,
            connection reset) — so the bound must abandon the prime, not
            await its cleanup (asyncio.wait_for would hold startup here)."""

            def __init__(self, back):
                super().__init__()
                self.back = back

            async def fetchrow(self, sql, *args):
                try:
                    await self.back.wait()
                except _asyncio.CancelledError:
                    await self.back.wait()  # cleanup talks to the server
                    raise
                return await super().fetchrow(sql, *args)

        class DownPool(ReplayPool):
            async def fetchrow(self, sql, *args):
                if "count(DISTINCT hour_utc)" in sql:
                    raise ConnectionError("database unavailable")
                return await super().fetchrow(sql, *args)

        CALIBRATION.mark_uncalibrated("not yet checked for field observations")

        async def hung_start():
            back = _asyncio.Event()
            # safety valve: a regression fails the timing assert, never hangs
            _asyncio.get_running_loop().call_later(1.5, back.set)
            scheduler = CalibrationScheduler(HangingPool(back), prime_timeout_sec=0.05)
            started = _time.monotonic()
            result = await _asyncio.wait_for(scheduler.prime(), timeout=2.0)
            elapsed = _time.monotonic() - started
            back.set()  # the database answers again; the abandoned prime unwinds
            await scheduler.stop()
            return result, elapsed, scheduler.health()

        result, elapsed, health = _asyncio.run(hung_start())
        assert elapsed < 1.0  # the oracle still starts
        assert result["primed"] is False and result["status"] == "prime_timeout"
        assert "no answer within 0.05 s" in health["last_error"]
        down = CalibrationScheduler(DownPool())
        result = _asyncio.run(down.prime())
        assert result["primed"] is False and result["status"] == "prime_failed"
        assert "ConnectionError" in down.health()["last_error"]
        assert CALIBRATION.calibrated is False  # fail closed throughout

    def test_a_primed_scheduler_waits_a_full_interval_before_rechecking(self):
        import asyncio as _asyncio

        from traffic_oracle.simulation.scheduler import CalibrationScheduler

        async def scenario():
            scheduler = CalibrationScheduler(ReplayPool(field_hours=0))
            await scheduler.prime()
            scheduler.start()
            await _asyncio.sleep(0.05)
            health = scheduler.health()
            await scheduler.stop()
            return health

        health = _asyncio.run(scenario())
        assert health["checks"] == 1  # the prime's check only
        assert health["next_check_in_sec"] > 3500

    def test_unprimed_scheduler_checks_at_once_and_retries_a_failure_fast(self):
        import asyncio as _asyncio

        from traffic_oracle.simulation.calibration import REASON_SIMULATED_ONLY
        from traffic_oracle.simulation.scheduler import CalibrationScheduler

        class FlakyPool(ReplayPool):
            coverage_calls = 0

            async def fetchrow(self, sql, *args):
                if "count(DISTINCT hour_utc)" in sql:
                    self.coverage_calls += 1
                    if self.coverage_calls == 1:
                        raise ConnectionError("database unavailable")
                return await super().fetchrow(sql, *args)

        CALIBRATION.mark_uncalibrated("not yet checked for field observations")
        pool = FlakyPool(field_hours=0)

        async def scenario():
            scheduler = CalibrationScheduler(pool, retry_delay_sec=0.02)
            scheduler.start()  # no prime: reload + check at once, no fixed delay
            for _ in range(300):
                await _asyncio.sleep(0.01)
                if scheduler.last_status == "waiting_for_field_data":
                    break
            health = scheduler.health()
            await scheduler.stop()
            return health

        health = _asyncio.run(scenario())
        assert pool.coverage_calls == 2  # failed, then retried in 0.02 s, not 1 h
        assert health["last_status"] == "waiting_for_field_data"
        assert health["reloaded_calibrated"] is False  # reloaded before checking
        assert health["next_check_in_sec"] > 3500  # healthy again: hourly
        assert CALIBRATION.reason == REASON_SIMULATED_ONLY

    def test_an_abandoned_prime_unwinds_before_the_first_background_check(self):
        """Two checks (two replays, two ledger rows) never run at once: the
        background task waits for an abandoned prime to finish unwinding."""
        import asyncio as _asyncio

        from traffic_oracle.simulation.scheduler import CalibrationScheduler

        class SlowCoveragePool(ReplayPool):
            def __init__(self, back, **kw):
                super().__init__(**kw)
                self.back = back
                self.in_flight = 0
                self.max_in_flight = 0

            async def fetchrow(self, sql, *args):
                if "count(DISTINCT hour_utc)" not in sql:
                    return await super().fetchrow(sql, *args)
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
                try:
                    await self.back.wait()
                except _asyncio.CancelledError:
                    await self.back.wait()
                    raise
                finally:
                    self.in_flight -= 1
                return await super().fetchrow(sql, *args)

        CALIBRATION.mark_uncalibrated("not yet checked for field observations")

        async def scenario():
            back = _asyncio.Event()
            # safety valve: a regression fails the asserts, never hangs
            _asyncio.get_running_loop().call_later(2.0, back.set)
            pool = SlowCoveragePool(back, field_hours=0)
            scheduler = CalibrationScheduler(pool, prime_timeout_sec=0.05)
            await scheduler.prime()  # abandoned mid-check
            scheduler.start()
            await _asyncio.sleep(0.1)
            waiting = pool.in_flight  # only the abandoned prime's query
            back.set()
            for _ in range(200):
                await _asyncio.sleep(0.01)
                if scheduler.last_status == "waiting_for_field_data":
                    break
            await scheduler.stop()
            return pool, waiting, scheduler

        pool, waiting, scheduler = _asyncio.run(scenario())
        assert waiting == 1 and pool.max_in_flight == 1
        assert scheduler.primed is False and scheduler.prime_status == "prime_timeout"
        assert scheduler.last_status == "waiting_for_field_data"  # checked after

    def test_replay_reads_the_newest_aggregates_first(self):
        import asyncio as _asyncio

        from traffic_oracle.simulation.calibration import (
            _SQL_HOURLY_AGGREGATES,
            REPLAY_ROW_LIMIT,
            run_replay,
        )

        # A week of today's 49-segment corridor (the 1048/1049 connectors
        # included) fits; if the bound ever truncates, the OLDEST hours drop
        # (the old ascending LIMIT 5000 fitted only the oldest ~106 h of a
        # 7-day window).
        assert REPLAY_ROW_LIMIT >= 49 * 24 * 7
        assert "ORDER BY hour_utc DESC" in _SQL_HOURLY_AGGREGATES
        assert "LIMIT $3" in _SQL_HOURLY_AGGREGATES

        class ArgsPool(ReplayPool):
            aggregate_args: tuple = ()

            async def fetch(self, sql, *args):
                if "hourly_aggregates" in sql:
                    self.aggregate_args = args
                return await super().fetch(sql, *args)

        pool = ArgsPool(rows=model_rows())
        start = datetime.fromisoformat("2026-10-05T00:00:00+00:00")
        report = _asyncio.run(run_replay(pool, start, start + timedelta(days=1)))
        assert pool.aggregate_args[-1] == REPLAY_ROW_LIMIT
        assert report["row_order"] == "newest_first"
        assert report["rows_truncated"] is False
