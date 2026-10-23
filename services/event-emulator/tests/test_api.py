"""API surface tests (ASGI, in-process; fake seams, no external services).

Pins contract C-G: POST /runs answers 202 straight after the ledger insert
while the run continues in the background, a second launch of the same use
case is a 409 naming the running run, GET /runs/{id} reports live progress,
POST /runs/{id}/stop ends the run 'stopped' and completes its corridor
event, limits are validated, and /health probes rather than assumes.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from event_emulator.api import create_app
from event_emulator.config import Settings
from event_emulator.runner import RunnerDeps
from fakes import FakeNats, FakeOracle, FakeStore, FakeTwin, FakeValkey, NoSleep


def _settings(**kw) -> Settings:
    s = Settings()
    s.traffic_oracle_url = "http://localhost:9"
    s.deck_origins = ("http://localhost:5173", "http://127.0.0.1:5273")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _deps(store=None, real_sleep=False, **kw) -> RunnerDeps:
    store = FakeStore() if store is None else store
    return RunnerDeps(
        store=store,
        valkey=kw.get("valkey", FakeValkey()),
        twin=kw.get("twin", FakeTwin()),
        nats=kw.get("nats", FakeNats()),
        oracle=kw.get("oracle", FakeOracle(store=store if store else None)),
        sleep=asyncio.sleep if real_sleep else NoSleep(),
    )


@pytest.fixture()
def client():
    app = create_app(_deps(), settings=_settings(), background=False)
    with TestClient(app) as c:
        yield c


def _wait(client, run_id, pred, timeout=8.0):
    deadline = time.monotonic() + timeout
    body = None
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/emulator/runs/{run_id}").json()
        if pred(body):
            return body
        time.sleep(0.05)
    raise AssertionError(f"condition not reached: {body}")


class TestSurface:
    def test_root_describes_seams(self, client):
        body = client.get("/").json()
        assert body["service"] == "event-emulator"
        assert body["use_cases"] == 10
        assert "adaptive:inputs:{movement_id}" in body["seams"]["valkey"]
        assert "C-E" in body["seams"]["anpr"] and "C-F" in body["seams"]["lifecycle"]

    def test_use_cases_listed(self, client):
        ids = {u["id"] for u in client.get("/api/v1/emulator/use-cases").json()}
        assert "rath_yatra_monsoon_ambulance" in ids and len(ids) == 10

    def test_cors_for_deck_origin(self, client):
        r = client.get("/health", headers={"Origin": "http://127.0.0.1:5273"})
        assert r.headers.get("access-control-allow-origin") == "http://127.0.0.1:5273"

    def test_unknown_use_case_404(self, client):
        assert client.post("/api/v1/emulator/runs", json={"use_case_id": "nope"}).status_code == 404

    @pytest.mark.parametrize("limit", [-1, 0, 501])
    def test_list_limit_validated(self, client, limit):
        assert client.get(f"/api/v1/emulator/runs?limit={limit}").status_code == 422

    @pytest.mark.parametrize("tick", [0.0001, 0.5, 61])
    def test_tick_sec_bounded(self, client, tick):
        # a sub-second tick would build millions of frames on the event loop
        r = client.post("/api/v1/emulator/runs",
                        json={"use_case_id": "metro_work_zone", "tick_sec": tick})
        assert r.status_code == 422

    def test_replay_unknown_run_404(self, client):
        assert client.post("/api/v1/emulator/runs/999/replay", json={}).status_code == 404

    def test_stop_unknown_run_404(self, client):
        assert client.post("/api/v1/emulator/runs/999/stop").status_code == 404


class TestHealth:
    def test_probes_report_real_state(self):
        valkey = FakeValkey(up=False)
        nats = FakeNats(up=False)
        app = create_app(_deps(valkey=valkey, nats=nats), settings=_settings(),
                         background=False)
        with TestClient(app) as c:
            body = c.get("/health").json()
        assert body["status"] == "degraded"
        assert body["stack"] == {"store": True, "valkey": False, "nats": False, "twin": True}
        assert any(r.startswith("valkey") for r in body["degraded_reasons"])
        assert any(r.startswith("nats") for r in body["degraded_reasons"])
        assert body["living_city"] == {"enabled": False, "status": "stopped", "active_slots": 0,
                                       "detail": None}

    def test_ok_when_every_seam_answers(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok" and body["degraded_reasons"] == []
        assert body["stack"] == {"store": True, "valkey": True, "nats": True, "twin": True}

    def test_degraded_when_last_run_delivered_nothing(self):
        app = create_app(_deps(valkey=None), settings=_settings(), background=False)
        app_deps = None
        with TestClient(app) as c:
            app_deps = app.state.deps
            app_deps.oracle = None            # no lifecycle seam either
            app_deps.store.anchors = {}       # the event cannot be created
            run = c.post("/api/v1/emulator/runs", json={
                "use_case_id": "metro_work_zone", "clock_scale": 3600, "tick_sec": 60,
                "deliver_twin": False, "deliver_nats": False}).json()
            _wait(c, run["run_id"], lambda b: b["status"] != "running")
            body = c.get("/health").json()
        assert body["status"] == "degraded"
        assert any("delivered 0" in r for r in body["degraded_reasons"])

    def test_degraded_when_the_last_runs_event_never_went_live(self):
        store = FakeStore()
        oracle = FakeOracle(store=store, down=True)    # the lifecycle seam is down
        app = create_app(_deps(store=store, oracle=oracle), settings=_settings(),
                         background=False)
        with TestClient(app) as c:
            run = c.post("/api/v1/emulator/runs", json={
                "use_case_id": "metro_work_zone", "clock_scale": 3600, "tick_sec": 60}).json()
            done = _wait(c, run["run_id"], lambda b: b["status"] != "running")
            body = c.get("/health").json()
        assert done["status"] == "failed" and done["ledger_status"] == "failed"
        assert done["event_status"] == "planning"          # never went live
        assert done["progress"]["failed"] >= 1 and done["progress"]["delivered"] > 0
        assert done["spec"]["reason"].startswith("the corridor event never became active")
        assert body["status"] == "degraded"
        assert body["last_run"]["status"] == "failed"
        assert any(r.startswith(f"last run {run['run_id']} (failed): the corridor event "
                                "never became active (C-F)") for r in body["degraded_reasons"])

    def test_oracle_down_is_degraded(self):
        twin = FakeTwin()

        async def refused():
            raise ConnectionError("oracle refused the connection")

        twin.probe = refused
        app = create_app(_deps(twin=twin), settings=_settings(), background=False)
        with TestClient(app) as c:
            body = c.get("/health").json()
        assert body["status"] == "degraded" and body["stack"]["twin"] is False
        assert any(r.startswith("oracle: ConnectionError") for r in body["degraded_reasons"])

    def test_oracle_probe_names_its_error(self):
        import httpx

        from event_emulator.twin_seam import SafeHttpClient, TwinSeam

        def refuse(request):
            raise httpx.ConnectError("[Errno 111] Connection refused")

        seam = TwinSeam(SafeHttpClient(("http://localhost:9",),
                                       transport=httpx.MockTransport(refuse)),
                        "http://localhost:9")
        app = create_app(_deps(twin=seam), settings=_settings(), background=False)
        with TestClient(app) as c:
            body = c.get("/health").json()
        assert body["stack"]["twin"] is False
        assert "oracle: ConnectError: [Errno 111] Connection refused" in body["degraded_reasons"]

    def test_an_operator_stop_is_not_a_fault(self):
        app = create_app(_deps(real_sleep=True), settings=_settings(), background=False)
        with TestClient(app) as c:
            run = c.post("/api/v1/emulator/runs", json={
                "use_case_id": "rath_yatra_monsoon_ambulance", "clock_scale": 1}).json()
            assert c.post(f"/api/v1/emulator/runs/{run['run_id']}/stop").status_code == 200
            body = c.get("/health").json()
        assert body["last_run"]["status"] == "stopped"
        assert body["status"] == "ok", body["degraded_reasons"]

    def test_no_database_means_no_runs(self):
        deps = RunnerDeps(store=None, valkey=FakeValkey(), twin=FakeTwin(),
                          nats=FakeNats(), oracle=None, sleep=NoSleep())
        app = create_app(deps, settings=_settings(), background=False)
        with TestClient(app) as c:
            assert c.post("/api/v1/emulator/runs",
                          json={"use_case_id": "vvip_convoy_base"}).status_code == 503
            assert c.get("/api/v1/emulator/runs").status_code == 503
            assert c.get("/health").json()["stack"]["store"] is False


class TestAsyncRuns:
    def test_lifecycle_202_progress_409_stop(self):
        store = FakeStore()
        app = create_app(_deps(store=store, real_sleep=True), settings=_settings(),
                         background=False)
        with TestClient(app) as c:
            t = time.monotonic()
            r = c.post("/api/v1/emulator/runs", json={
                "use_case_id": "rath_yatra_monsoon_ambulance", "clock_scale": 600})
            elapsed = time.monotonic() - t
            assert r.status_code == 202, r.text
            assert elapsed < 1.0
            run_id = r.json()["run_id"]
            assert r.json()["status"] == "running"

            first = _wait(c, run_id, lambda b: b["progress"]["frame"] > 5)
            assert first["status"] == "running"
            assert first["progress"]["total"] > first["progress"]["frame"]
            assert first["event_id"] and first["movement_id"] == first["event_id"]
            assert first["event_status"] == "active"

            dup = c.post("/api/v1/emulator/runs", json={
                "use_case_id": "rath_yatra_monsoon_ambulance", "clock_scale": 600})
            assert dup.status_code == 409
            assert dup.json()["run_id"] == run_id

            later = _wait(c, run_id, lambda b: b["progress"]["frame"] > first["progress"]["frame"])
            assert later["status"] == "running"

            stop = c.post(f"/api/v1/emulator/runs/{run_id}/stop")
            assert stop.status_code == 200
            assert stop.json() == {"run_id": run_id, "status": "stopped"}

            final = c.get(f"/api/v1/emulator/runs/{run_id}").json()
            assert final["status"] == "stopped"
            assert final["ledger_status"] == "stopped"
            event_id = final["event_id"]
            assert store.events[event_id]["status"] == "completed"
            assert final["event_status"] == "completed"
            assert final["progress"]["frame"] < final["progress"]["total"]

            # the use case is free again
            again = c.post("/api/v1/emulator/runs", json={
                "use_case_id": "rath_yatra_monsoon_ambulance", "clock_scale": 3600,
                "tick_sec": 60})
            assert again.status_code == 202
            c.post(f"/api/v1/emulator/runs/{again.json()['run_id']}/stop")

            listed = c.get("/api/v1/emulator/runs?limit=5").json()["runs"]
            assert {r["run_id"] for r in listed} >= {run_id}
            row = next(r for r in listed if r["run_id"] == run_id)
            assert row["status"] == "stopped" and "frame_log" not in row["spec"]

    def test_completed_run_reports_completed(self, client):
        r = client.post("/api/v1/emulator/runs", json={
            "use_case_id": "vvip_convoy_base", "clock_scale": 3600, "tick_sec": 60})
        assert r.status_code == 202
        body = _wait(client, r.json()["run_id"], lambda b: b["status"] != "running")
        assert body["status"] == "completed"
        assert body["progress"]["frame"] == body["progress"]["total"]
        stop = client.post(f"/api/v1/emulator/runs/{r.json()['run_id']}/stop")
        assert stop.json()["status"] == "completed"

    def test_progress_poll_can_skip_the_tape(self, client):
        r = client.post("/api/v1/emulator/runs", json={
            "use_case_id": "metro_work_zone", "clock_scale": 3600, "tick_sec": 60})
        body = _wait(client, r.json()["run_id"], lambda b: b["status"] != "running")
        assert body["tape"]["frames"]                     # default: as before
        light = client.get(f"/api/v1/emulator/runs/{r.json()['run_id']}",
                           params={"include_tape": "false"}).json()
        assert "frames" not in light["tape"]
        assert light["tape"]["movement_id"] == body["movement_id"]
        assert light["status"] == body["status"] and light["progress"] == body["progress"]

    def test_replay_is_async_and_verbatim(self, client):
        first = client.post("/api/v1/emulator/runs", json={
            "use_case_id": "metro_work_zone", "clock_scale": 3600, "tick_sec": 60}).json()
        done = _wait(client, first["run_id"], lambda b: b["status"] != "running")
        rep = client.post(f"/api/v1/emulator/runs/{first['run_id']}/replay",
                          json={"clock_scale": 3600})
        assert rep.status_code == 202
        assert rep.json()["replay_of_run_id"] == first["run_id"]
        again = _wait(client, rep.json()["run_id"], lambda b: b["status"] != "running")
        assert again["movement_id"] == done["movement_id"]
        assert again["tape"]["frames"] == done["tape"]["frames"]


class TestReplayOnTheCurrentNetwork:
    """1a: a tape whose recorded route is no longer a legal path on the
    store's road network (a run recorded before the WP-20 conversion) is
    refused — 422 with the reason, before anything is ledgered — so a
    Replay click never re-animates an event off the roads.  409 stays
    C-G's 'that use case is running' (the deck reads every 409 so)."""

    def _recorded(self, store):
        app = create_app(_deps(store=store), settings=_settings(), background=False)
        client = TestClient(app)
        client.__enter__()
        first = client.post("/api/v1/emulator/runs", json={
            "use_case_id": "metro_work_zone", "clock_scale": 3600, "tick_sec": 60}).json()
        _wait(client, first["run_id"], lambda b: b["status"] != "running")
        return client, first["run_id"]

    def test_a_route_off_the_current_network_is_refused_422(self):
        store = FakeStore()
        client, run_id = self._recorded(store)
        try:
            # the network changed under the recording: 1031 -> 1014 is no longer a move
            store.illegal_moves = {(1031, 1014)}
            runs_before, log_before = dict(store.runs), list(store.status_log)
            movements_before = {k: dict(v) for k, v in store.movements.items()}
            rep = client.post(f"/api/v1/emulator/runs/{run_id}/replay", json={"clock_scale": 3600})
            assert rep.status_code == 422, rep.text
            body = rep.json()
            assert body["reason"] == "route_not_on_current_network"
            assert body["run_id"] == run_id and body["use_case_id"] == "metro_work_zone"
            assert body["route_segment_ids"] == [1031, 1014, 1049, 1038]
            assert body["problems"] == ["1031 -> 1014 is not a legal move"]
            detail = body["detail"]
            assert detail.startswith(f"run {run_id} cannot be replayed: its recorded route "
                                     "[1031, 1014, 1049, 1038] is not a legal path")
            assert "off the roads" in detail and "Start a new run of metro_work_zone" in detail
            # nothing ledgered, nothing re-animated, the movement untouched
            assert store.runs.keys() == runs_before.keys()
            assert store.status_log == log_before and store.movements == movements_before
            # …while a new run of the use case is built on the current network
            store.illegal_moves = set()
            ok = client.post(f"/api/v1/emulator/runs/{run_id}/replay", json={"clock_scale": 3600})
            assert ok.status_code == 202, ok.text
        finally:
            client.__exit__(None, None, None)

    def test_an_unknown_segment_is_refused_too(self):
        store = FakeStore()
        client, run_id = self._recorded(store)
        try:
            del store.anchors[1049]          # a road the network no longer has
            rep = client.post(f"/api/v1/emulator/runs/{run_id}/replay", json={})
            assert rep.status_code == 422
            assert rep.json()["problems"] == ["unknown segments [1049]"]
        finally:
            client.__exit__(None, None, None)

    def test_an_unchecked_route_is_never_replayed(self):
        store = FakeStore()
        client, run_id = self._recorded(store)
        try:
            n = len(store.runs)
            store.route_check_error = ConnectionError("store down")
            rep = client.post(f"/api/v1/emulator/runs/{run_id}/replay", json={})
            assert rep.status_code == 503 and "could not be checked" in rep.json()["detail"]
            assert len(store.runs) == n
        finally:
            client.__exit__(None, None, None)


class TestLivingCityRoutes:
    def test_start_status_stop(self):
        store = FakeStore()
        nats = FakeNats()
        app = create_app(_deps(store=store, nats=nats), settings=_settings(),
                         background=False)
        with TestClient(app) as c:
            idle = c.get("/api/v1/emulator/living-city").json()
            assert idle["status"] == "stopped" and idle["active_slots"] == []
            started = c.post("/api/v1/emulator/living-city", json={"action": "start"}).json()
            assert started["status"] == "running" and started["enabled"] is True
            body = _wait_city(c, lambda b: len(b["active_slots"]) >= 2)
            assert 2 <= len(body["active_slots"]) <= 3
            assert body["units"] == ["JR-EM-LC1", "JR-EM-LC2"]
            for slot in body["active_slots"]:
                ev = store.events[slot["event_id"]]
                assert ev["metadata"]["mode"] == "living_city"
                assert ev["status"] == "active"
            assert c.get("/health").json()["living_city"]["status"] == "running"
            stopped = c.post("/api/v1/emulator/living-city", json={"action": "stop"}).json()
            assert stopped["status"] == "stopped" and stopped["enabled"] is False
            assert all(store.events[s["event_id"]]["status"] == "completed"
                       for s in body["active_slots"])

    def test_bad_action_422(self, client):
        assert client.post("/api/v1/emulator/living-city",
                           json={"action": "explode"}).status_code == 422


def _wait_city(client, pred, timeout=8.0):
    deadline = time.monotonic() + timeout
    body = None
    while time.monotonic() < deadline:
        body = client.get("/api/v1/emulator/living-city").json()
        if pred(body):
            return body
        time.sleep(0.05)
    raise AssertionError(f"living city never reached the condition: {body}")
