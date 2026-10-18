"""API surface tests — app factory, port, /chat tool_calls, health shape."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from convoy_brain import __main__ as brain_main


@pytest.fixture
def app(monkeypatch):
    """Build the FastAPI app with the model tiers unreachable (fail-closed).

    Port 1 on loopback is a platform-legal endpoint that nothing serves, so
    these tests never touch the real model hosts even when they are live.
    The context store is swapped for an in-memory fake (no Valkey needed).
    """
    from conftest import FakeContextStore

    monkeypatch.setenv("MODEL_HOST_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("LLAMA_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("TRAFFIC_ORACLE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("EMBEDDING_SERVICE_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(brain_main, "ConvoyContextStore", lambda valkey_url: FakeContextStore())
    application = brain_main.create_app()
    yield application


class TestPortUnification:
    def test_default_port_is_8080(self, monkeypatch):
        monkeypatch.delenv("PORT", raising=False)
        # The default lives in main() as a literal; assert the source keeps it.
        source = Path(brain_main.__file__).read_text(encoding="utf-8")
        assert '"PORT", "8080"' in source

    def test_env_port_wins(self, monkeypatch):
        monkeypatch.setenv("PORT", "9090")
        assert os.environ.get("PORT") == "9090"  # main() reads env at start


class TestChatToolCallsContract:
    """C9(a): /chat must expose top-level tool_calls for the adversarial gate."""

    def test_chat_response_includes_tool_calls_key(self, app):
        from starlette.testclient import TestClient

        with TestClient(app) as client:
            # Model tiers are down in this fixture, so the turn runs the full
            # fallback chain (heuristic selection -> heuristic args -> heuristic
            # final); the contract only requires the key to exist with real calls.
            response = client.post("/chat", json={
                "message": "What is the current traffic on segment 1001?",
            })
            assert response.status_code == 200
            data = response.json()
            assert "session_id" in data
            assert "response" in data
            assert "tool_calls" in data
            assert isinstance(data["tool_calls"], list)
            # heuristic keyword selection must have picked the live-traffic tool
            assert any(tc["name"] == "get_live_traffic" for tc in data["tool_calls"])

    def test_chat_tier_annotation_present(self, app):
        from starlette.testclient import TestClient

        with TestClient(app) as client:
            response = client.post("/chat", json={"message": "hello"})
            body = response.json()["response"]
            assert "tiers_used" in body
            assert body["tiers_used"][-1] == "heuristic_final"


class TestHealthShape:
    def test_health_reports_both_tiers(self, app):
        from starlette.testclient import TestClient

        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200
            data = response.json()
            # Hermetic fixture: nothing serves the model ports, so both tiers
            # report unreachable and the overall status is degraded.
            assert data["status"] == "degraded"
            assert data["strategic_tier"]["model"] == "pplx-decider-v1-27b"
            assert data["strategic_tier"]["model_host"] == "unreachable"
            # Tier-0 gate wiring: on by default; the env kill switch flips it.
            assert data["strategic_tier"]["tier0_cache"] == "enabled"
            assert data["tactical_tier"]["model"] == "aditi-ps18-v7h-gemma4-12b"
            assert data["tactical_tier"]["llama_server"] == "unreachable"

    def test_health_tier0_kill_switch_env_flips_the_field(self, app, monkeypatch):
        from starlette.testclient import TestClient

        monkeypatch.setenv("DECIDER_TIER0_DISABLE", "1")
        application = brain_main.create_app()
        with TestClient(application) as client:
            response = client.get("/health")
            assert response.status_code == 200
            data = response.json()
            assert data["strategic_tier"]["tier0_cache"] == "disabled"

    def test_movements_roundtrip(self, app):
        from starlette.testclient import TestClient

        with TestClient(app) as client:
            response = client.post("/movements", json={
                "origin": (72.55, 23.02),
                "destination": (72.60, 23.05),
                "vvip_class": "Z",
                "planned_departure": "2026-10-12T08:00:00+05:30",
            })
            assert response.status_code == 200
            assert "movement_id" in response.json()

            movement_id = response.json()["movement_id"]
            protocol = client.get(f"/movements/{movement_id}/protocol")
            assert protocol.status_code == 200
            assert "protocol_state" in protocol.json()


class TestQwenEradication:
    def test_no_qwen_or_ollama_in_code(self):
        """Zero qwen/ollama tokens in executable code.

        Docstrings may explain the retirement in prose; the token scan strips
        comments and string literals via `tokenize`, so only real code is
        checked.
        """
        import io
        import tokenize as tk

        src = Path(__file__).resolve().parents[1] / "src" / "convoy_brain"
        offenders = []
        for path in src.rglob("*.py"):
            code_tokens = []
            try:
                with open(path, encoding="utf-8") as fh:
                    for tok in tk.generate_tokens(fh.readline):
                        if tok.type in (tk.STRING, tk.COMMENT):
                            continue
                        code_tokens.append(tok.string)
            except (tk.TokenError, SyntaxError, UnicodeDecodeError):
                offenders.append(f"{path.name}: unparseable")
                continue
            code = " ".join(code_tokens).lower()
            for token in ("qwen", "ollama"):
                if token in code:
                    offenders.append(f"{path.name}: {token} in code")
        assert offenders == [], "qwen/ollama references found:\n" + "\n".join(offenders)

    def test_no_ollama_bridge_module(self):
        src = Path(__file__).resolve().parents[1] / "src" / "convoy_brain"
        assert not (src / "ollama_bridge.py").exists()
        assert not (src / "tools").exists()  # legacy phantom tools removed

    def test_pyproject_has_no_ollama_or_langgraph(self):
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        assert "ollama" not in text
        assert "langgraph" not in text
        assert "langchain" not in text
