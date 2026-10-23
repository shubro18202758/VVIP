"""/evaluate/scenarios on the seeded twin (evaluate/scenario_sim.py).

Coverage:
    - Key aliases: convoy-brain sends ``route_id`` / ``segment_ids``
      (workflows/pre_movement.py); they map onto scenario_id /
      route_segment_ids and the id is ECHOED back.
    - Every scenario runs on the twin (SimulationEngine): a 3-segment
      morning-peak route yields real vehicle-hours (the retired formula used
      a density as an arrival rate and reported ≈0 for everything), the
      numbers equal a direct engine run, and repeated runs are identical.
    - Network provenance: real lengths from corridor.road_segments when a
      pool is wired (``network_source``), the seed SQL otherwise.
    - Malformed scenarios → 422 naming the scenario; scenarios with no
      known segment are labelled ``evaluated: false`` and ranked LAST
      (never "least disruptive" by default).
"""

from __future__ import annotations

import pytest

pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from traffic_oracle.__main__ import create_app  # noqa: E402
from traffic_oracle.evaluate.scenario_sim import (  # noqa: E402
    DEFAULT_SEED,
    ScenarioInputError,
    ScenarioSimulator,
    normalize_scenario,
)
from traffic_oracle.optimize.diversion_planner import coerce_departure_time  # noqa: E402
from traffic_oracle.simulation.engine import (  # noqa: E402
    ClosurePolicy,
    SimulationConfig,
    SimulationEngine,
)
from traffic_oracle.simulation.network import default_network  # noqa: E402

BRAIN_SCENARIO = {  # exactly what convoy-brain's pre_movement workflow sends
    "route_id": "r1",
    "segment_ids": [1001, 1015, 1005],
    "departure_time": "2026-10-23T09:00:00+05:30",
}


async def post(app, body: dict):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.post("/api/v1/evaluate/scenarios", json=body)


def app_with(sim: ScenarioSimulator):
    app = create_app()
    app.state.scenario_sim = sim
    return app


# ─── Fake PostGIS network (DB-shaped rows from the seed geometry) ─────────────


class NetworkConn:
    def __init__(self, length_scale: float = 1.0) -> None:
        net = default_network()
        self.seg_rows = [
            {
                "segment_id": s.segment_id,
                "road_name": s.road_name,
                "road_class": s.road_class,
                "lanes": s.lanes,
                "speed_limit_kmh": s.speed_limit_kmh,
                "oneway": s.oneway,
                "length_m": s.length_m * length_scale,
                "signalized": s.signalized,
            }
            for s in net.segments.values()
        ]
        self.adj_rows = [
            {"from_segment_id": a, "to_segment_id": b}
            for a, nbrs in net.adjacency.items()
            for b in nbrs
        ]

    async def fetch(self, query: str, *args):
        if "corridor.signal_programs" in query or "via_junction_id" in query:
            return []  # no DB signal programs → seed green-split assumptions
        if "corridor.road_segments" in query:
            return self.seg_rows
        if "corridor.segment_adjacency" in query:
            return self.adj_rows
        raise AssertionError(query)


class NetworkPool:
    def __init__(self, conn: NetworkConn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def fetch(self, query: str, *args):  # load_signal_overrides uses pool.fetch
        return await self.conn.fetch(query, *args)


# ─── Aliases + twin delegation ────────────────────────────────────────────────


class TestScenarioAliases:
    async def test_brain_spelling_is_accepted_and_the_id_echoed(self):
        res = await post(app_with(ScenarioSimulator()), {"scenarios": [BRAIN_SCENARIO]})
        assert res.status_code == 200, res.text
        (result,) = res.json()["results"]
        assert result["scenario_id"] == "r1"
        assert result["route_segment_ids"] == [1001, 1015, 1005]
        assert result["evaluated"] is True and result["engine"] == "twin"
        assert result["total_disruption_vehicle_hours"] > 0
        assert result["segments_affected"] == 3

    def test_canonical_spelling_still_works_and_wins(self):
        sc = normalize_scenario(
            {"scenario_id": "A", "route_id": "ignored", "route_segment_ids": [1001],
             "segment_ids": [9999]},
            0,
        )
        assert sc.scenario_id == "A" and sc.route == [1001]

    def test_missing_id_gets_a_positional_label(self):
        sc = normalize_scenario({"segment_ids": [1001]}, 2)
        assert sc.scenario_id == "scenario-3"


class TestTwinDelegation:
    async def test_disruption_is_the_twin_not_the_retired_density_formula(self):
        (result,) = await ScenarioSimulator().evaluate([BRAIN_SCENARIO])
        engine = SimulationEngine(network=default_network(), config=SimulationConfig())
        runs = engine.run(
            route_segment_ids=[1001, 1015, 1005],
            departure_time=coerce_departure_time(BRAIN_SCENARIO["departure_time"]),
            closure_policy=ClosurePolicy(),
            horizon_min=45,
            n_replications=5,
            seed=DEFAULT_SEED,
        )
        expected = sum(r.total_disruption_vehicle_hours for r in runs) / len(runs)
        assert result.total_disruption_vehicle_hours == pytest.approx(round(expected, 3))
        # The retired closed form gave ~0.03 vh here (18 'veh/h' × ~0.07 h).
        assert result.total_disruption_vehicle_hours > 1.0
        assert result.network_source == "seed_sql"
        assert result.forecast_coupling == "internal"
        assert result.n_replications == 5 and result.seed == DEFAULT_SEED
        p90 = result.total_disruption_vehicle_hours_p90
        assert p90 >= result.total_disruption_vehicle_hours * 0.5

    async def test_results_are_deterministic_and_ranked(self):
        sim = ScenarioSimulator()
        scenarios = [
            {**BRAIN_SCENARIO, "route_id": "peak"},
            {**BRAIN_SCENARIO, "route_id": "night", "departure_time": "2026-10-23T03:00:00+05:30"},
        ]
        first = await sim.evaluate(scenarios)
        second = await sim.evaluate(scenarios)
        assert [r.scenario_id for r in first] == ["night", "peak"]  # least disruptive first
        assert [r.total_disruption_vehicle_hours for r in first] == [
            r.total_disruption_vehicle_hours for r in second
        ]
        assert first[0].total_disruption_vehicle_hours < first[1].total_disruption_vehicle_hours

    async def test_db_network_supplies_real_lengths(self):
        sim_db = ScenarioSimulator(pool=NetworkPool(NetworkConn()))
        (from_db,) = await sim_db.evaluate([BRAIN_SCENARIO])
        assert from_db.network_source == "corridor.road_segments"
        (from_seed,) = await ScenarioSimulator().evaluate([BRAIN_SCENARIO])
        # Same geometry → same convoy transit time; scaled lengths move it.
        assert from_db.convoy_transit_time_sec == from_seed.convoy_transit_time_sec
        sim_long = ScenarioSimulator(pool=NetworkPool(NetworkConn(length_scale=2.0)))
        (longer,) = await sim_long.evaluate([BRAIN_SCENARIO])
        assert longer.convoy_transit_time_sec == pytest.approx(
            2 * from_db.convoy_transit_time_sec, abs=2
        )


# ─── Validation + unevaluable scenarios ───────────────────────────────────────


class TestScenarioValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            {"route_id": "x"},  # no route
            {"route_id": "x", "segment_ids": []},
            {"route_id": "x", "segment_ids": ["abc"]},
            {"route_id": "x", "segment_ids": [1001], "departure_time": "soonish"},
            {"route_id": "x", "segment_ids": [1001], "closure_type": "teleport"},
            {"route_id": "x", "segment_ids": [1001], "horizon_min": 600},
            {"route_id": "x", "segment_ids": [1001], "convoy_speed_kmh": -5},
            # arbitrary-size JSON ints: math.isfinite() used to raise
            # OverflowError on these (a 500, not a 422)
            {"route_id": "x", "segment_ids": [1001], "seed": 10**300},
            {"route_id": "x", "segment_ids": [1001], "convoy_speed_kmh": 10**300},
            {"route_id": "x", "segment_ids": [1001], "n_replications": -(10**300)},
            {"route_id": "x", "segment_ids": [10**30]},  # beyond bigint
            {"route_id": "x", "segment_ids": [0]},
            {"route_id": "x", "segment_ids": ["9" * 5000]},  # beyond int() digit limit
        ],
    )
    async def test_malformed_scenarios_are_422(self, bad):
        with pytest.raises(ScenarioInputError):
            await ScenarioSimulator().evaluate([bad])
        res = await post(app_with(ScenarioSimulator()), {"scenarios": [BRAIN_SCENARIO, bad]})
        assert res.status_code == 422
        assert "scenarios[1]" in res.json()["detail"]

    async def test_unknown_segments_are_labelled_and_ranked_last(self):
        res = await post(
            app_with(ScenarioSimulator()),
            {"scenarios": [
                {"route_id": "ghost", "segment_ids": [987654, 987655]},
                BRAIN_SCENARIO,
            ]},
        )
        assert res.status_code == 200
        ranked = res.json()["results"]
        assert [r["scenario_id"] for r in ranked] == ["r1", "ghost"]
        ghost = ranked[1]
        assert ghost["evaluated"] is False and ghost["error"] == "no_known_segments"
        assert ghost["unknown_segment_ids"] == [987654, 987655]

    async def test_partially_known_routes_report_the_unknown_ids(self):
        (result,) = await ScenarioSimulator().evaluate(
            [{"route_id": "mixed", "segment_ids": [1001, 424242]}]
        )
        assert result.evaluated and result.unknown_segment_ids == [424242]
        assert result.segments_affected == 1

    async def test_scenario_count_is_bounded(self):
        res = await post(app_with(ScenarioSimulator()), {"scenarios": [BRAIN_SCENARIO] * 21})
        assert res.status_code == 422
