"""Incident + scenario-run ingestion (fakes) and the C3 API handlers."""

from __future__ import annotations

import pytest

from embedding_service import config
from embedding_service.incidents import IncidentIngestor, scrub_text
from embedding_service.api import ApiError


@pytest.fixture
def ingestor(fake_store, late_encoder, dense_encoder):
    return IncidentIngestor(fake_store, late_encoder, dense_encoder)


def _scenario(scenario_id="sc-042", route="Sardar Patel Marg", disruption=12.5):
    # EXACT frozen ScenarioResult shape (evaluate/scenario_sim.py:33-43)
    return {
        "scenario_id": scenario_id,
        "route_name": route,
        "total_disruption_vehicle_hours": disruption,
        "max_queue_length_m": 420.0,
        "avg_closure_duration_sec": 180.0,
        "segments_affected": 6,
        "estimated_complaints_risk": "high",
        "convoy_transit_time_sec": 1450,
    }


class TestScrub:
    def test_credentials_redacted(self):
        scrubbed = scrub_text("token=abc123 use api_key: sk-123 note")
        assert "abc123" not in scrubbed
        assert "sk-123" not in scrubbed

    def test_contact_patterns_redacted(self):
        scrubbed = scrub_text("call ops@example.com or +91 9876543210")
        assert "ops@example.com" not in scrubbed
        assert "9876543210" not in scrubbed


class TestScenarioRunIngestion:
    async def test_scenario_run_lands_in_incidents_and_memory(
        self, fake_store, ingestor
    ):
        result = await ingestor.ingest_scenario_run(
            _scenario(), movement_id="11111111-1111-1111-1111-111111111111",
            scenario_run_id=52, segment_ids=[10, 11],
        )
        assert result is not None
        incident = fake_store.incidents[0]
        assert incident["source"] == "scenario_run"
        assert incident["incident_kind"] == "congestion_spike"
        assert incident["severity"] == "high"  # from estimated_complaints_risk
        assert incident["scenario_run_id"] == 52
        # Frozen ScenarioResult fields preserved verbatim in outcome
        assert incident["outcome"]["total_disruption_vehicle_hours"] == 12.5
        assert incident["outcome"]["convoy_transit_time_sec"] == 1450
        # Memory doc written, embedded, and linked
        assert fake_store.links[incident["incident_id"]] == result.doc_id
        doc = fake_store.docs[result.doc_id]
        assert doc.kind == "scenario_run"
        assert doc.ref_id == "scenario:sc-042"
        assert doc.token_count is not None and doc.token_count >= 1
        assert doc.dense is not None and len(doc.dense) == config.DENSE_DIM

    async def test_non_scenario_payload_rejected(self, ingestor):
        assert await ingestor.ingest_scenario_run({"nope": 1}) is None
        assert await ingestor.ingest_scenario_run("not a dict") is None

    async def test_incident_kind_hint_respected_and_whitelisted(self, fake_store, ingestor):
        scenario = _scenario(scenario_id="sc-hint")
        scenario["incident_kind"] = "route_blockage"
        await ingestor.ingest_scenario_run(scenario)
        assert fake_store.incidents[0]["incident_kind"] == "route_blockage"

        bad = _scenario(scenario_id="sc-bad")
        bad["incident_kind"] = "DROP TABLE"  # not whitelisted → default
        await ingestor.ingest_scenario_run(bad)
        assert fake_store.incidents[1]["incident_kind"] == "congestion_spike"

    async def test_reingesting_same_scenario_upserts_not_duplicates(
        self, fake_store, ingestor
    ):
        await ingestor.ingest_scenario_run(_scenario())
        await ingestor.ingest_scenario_run(_scenario())
        # ref_id is the upsert key (kind, ref_id) — the fake store models
        # the ON CONFLICT by reusing the doc; incidents log twice by design
        # (append-only event log), memory stays deduplicated.
        assert len(fake_store.docs) == 1


class TestLiveIncidentIngestion:
    async def test_live_incident_embeds_and_links(self, fake_store, ingestor):
        result = await ingestor.ingest_live_incident(
            incident_kind="protest", severity="medium",
            summary="Farmers march blocking NH48 near Rao Tula Ram Marg",
            segment_ids=[101, 102],
        )
        assert result is not None
        incident = fake_store.incidents[0]
        assert incident["source"] == "live"
        assert fake_store.links[incident["incident_id"]] == result.doc_id
        doc = fake_store.docs[result.doc_id]
        assert doc.kind == "incident"

    async def test_live_incident_scrubs_summary(self, fake_store, ingestor):
        await ingestor.ingest_live_incident(
            incident_kind="accident", severity="low",
            summary="driver reachable at +91 9876543210",
        )
        assert "9876543210" not in fake_store.incidents[0]["summary"]

    async def test_bad_kind_returns_none(self, ingestor, fake_store):
        assert await ingestor.ingest_live_incident(
            incident_kind="made_up", severity="low", summary="x"
        ) is None
        assert fake_store.incidents == []


class TestApiHandlers:
    async def test_record_scenario_run_endpoint(self, api, fake_store):
        response = await api.record_incident(
            {"scenario_run": _scenario(), "movement_id": "m-1", "scenario_run_id": 9}
        )
        assert response["source"] == "scenario_run"
        assert response["incident_id"] == 1
        assert response["memory_doc_id"] in fake_store.docs

    async def test_record_live_incident_endpoint(self, api, fake_store):
        response = await api.record_incident(
            {"incident_kind": "weather", "severity": "high", "summary": "dense fog"}
        )
        assert response["source"] == "live"
        assert response["memory_doc_id"]

    async def test_record_incident_rejects_bad_payload(self, api):
        with pytest.raises(ApiError) as exc:
            await api.record_incident({"incident_kind": "zzz", "summary": "x"})
        assert exc.value.status == 400

    async def test_search_endpoint_returns_contract_shape(self, api, late_encoder, dense_encoder):
        await api.embed_documents(
            {
                "documents": [{"title": "fog incident", "body": "zero visibility fog on corridor", "ref_id": "fog-1"}],
                "kind": "incident",
            }
        )
        result = await api.search({"query": "fog on corridor", "top_k": 5})
        assert set(result) >= {"query", "results", "query_token_count", "recalled", "latency_ms"}
        top = result["results"][0]
        assert set(top) >= {
            "doc_id", "kind", "ref_id", "title", "snippet", "score",
            "maxsim_score", "dense_score", "strategy", "token_count",
            "occurred_at", "metadata",
        }
        assert top["kind"] == "incident"

    async def test_embed_documents_validation(self, api):
        with pytest.raises(ApiError):
            await api.embed_documents({"documents": []})
        with pytest.raises(ApiError):
            await api.embed_documents({"documents": [{"body": "x"}], "kind": "bogus"})
        with pytest.raises(ApiError):
            await api.embed_documents({"documents": [{"title": "no body"}]})

    async def test_recent_incidents_kind_filter(self, api, fake_store):
        await api.record_incident(
            {"incident_kind": "weather", "severity": "low", "summary": "fog"}
        )
        await api.record_incident({"scenario_run": _scenario()})
        result = await api.recent_incidents({"limit": "10", "kind": "congestion_spike"})
        kinds = {r["incident_kind"] for r in result["incidents"]}
        assert kinds == {"congestion_spike"}

        with pytest.raises(ApiError):
            await api.recent_incidents({"limit": "10", "kind": "not_a_kind"})

    async def test_health_shape(self, api):
        health = await api.health()
        assert health["status"] == "ok"
        assert health["service"] == "embedding-service"
        assert health["uptime_sec"] >= 0.0


class TestFastApiWiring:
    def test_routes_registered(self, api):
        from embedding_service.service import create_app

        app = create_app(api)
        paths = {route.path for route in app.routes}
        assert {
            "/health",
            "/embed/documents",
            "/search",
            "/incidents",
            "/incidents/recent",
        } <= paths

    def test_testclient_roundtrip(self, api):
        from fastapi.testclient import TestClient
        from embedding_service.service import create_app

        client = TestClient(create_app(api))
        body = {
            "scenario_run": _scenario(),
            "movement_id": "m-2",
        }
        response = client.post("/incidents", json=body)
        assert response.status_code == 200
        assert response.json()["source"] == "scenario_run"

        search = client.post("/search", json={"query": "monsoon flooding", "top_k": 3})
        assert search.status_code == 200
        assert "results" in search.json()

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

    def test_recent_incidents_http_200(self, api):
        # Regression (core fix): the handler previously annotated
        # ``request: Request`` inside the create_app closure — with
        # ``from __future__ import annotations`` FastAPI could not resolve
        # the string annotation and demanded a ?request= query param
        # (live 422).  The plain GET must answer 200 with a JSON body.
        from fastapi.testclient import TestClient
        from embedding_service.service import create_app

        client = TestClient(create_app(api))
        response = client.get("/incidents/recent")
        assert response.status_code == 200
        assert "incidents" in response.json()

        # query params flow through the same path
        limited = client.get("/incidents/recent", params={"limit": "5"})
        assert limited.status_code == 200
        bad_kind = client.get("/incidents/recent", params={"kind": "no_such_kind"})
        assert bad_kind.status_code == 400
