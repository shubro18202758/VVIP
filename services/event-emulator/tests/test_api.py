"""API surface tests (ASGI, in-process; no external services)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from event_emulator.api import create_app


@pytest.fixture()
def app(monkeypatch):
    # hermetic: no DB, dead valkey port, dead-but-legal loopback oracle port
    monkeypatch.delenv("VVIP_DATABASE_URL", raising=False)
    monkeypatch.setenv("VVIP_VALKEY_URL", "redis://127.0.0.1:1/")
    monkeypatch.setenv("VVIP_NATS_URL", "nats://127.0.0.1:1")
    monkeypatch.setenv("EMULATOR_TRAFFIC_ORACLE_URL", "http://localhost:9")
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:  # lifespan wires lazy seams
        yield c


class TestSurface:
    def test_root_describes_seams(self, client):
        r = client.get("/")
        assert r.status_code == 200
        body = r.json()
        assert body["service"] == "event-emulator"
        assert body["use_cases"] == 10  # 9 core + watchlist_anpr_sweep (010)
        assert "adaptive:inputs:{movement_id}" in body["seams"]["valkey"]

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_use_cases_listed(self, client):
        r = client.get("/api/v1/emulator/use-cases")
        assert r.status_code == 200
        ids = {u["id"] for u in r.json()}
        assert "rath_yatra_ahmedabad" in ids and "rath_yatra_monsoon_ambulance" in ids and len(ids) == 10  # noqa: E501

    def test_cors_for_deck_origin(self, client):
        r = client.get(
            "/health",
            headers={"Origin": "http://localhost:5173"},
        )
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_unknown_use_case_404(self, client):
        r = client.post("/api/v1/emulator/runs", json={"use_case_id": "nope"})
        assert r.status_code == 404

    def test_create_run_degrades_without_stack(self, client):
        # no DB/valkey/nats configured → run still builds the tape and
        # reports skipped frames honestly
        r = client.post(
            "/api/v1/emulator/runs",
            json={"use_case_id": "vvip_convoy_base", "clock_scale": 1000.0,
                  "tick_sec": 30.0},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "completed"
        assert body["delivery"]["skipped"] > 0
        assert body["movement_id"]

    def test_runs_ledger_503_without_db(self, client):
        r = client.get("/api/v1/emulator/runs")
        assert r.status_code == 503

    def test_replay_unknown_run_404(self, client):
        r = client.post("/api/v1/emulator/runs/999/replay", json={})
        assert r.status_code in (404, 503)  # 503 without ledger, 404 with
