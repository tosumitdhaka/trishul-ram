"""Tests for the AI assist router."""
from __future__ import annotations

import os
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.ai import router


def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


class TestAiStatus:
    def test_not_configured_when_no_key(self):
        app = _make_app()
        client = TestClient(app)
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("TRAM_AI_API_KEY", None)
            resp = client.get("/api/ai/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["provider"] is None

    def test_enabled_when_key_set(self):
        app = _make_app()
        client = TestClient(app)
        with patch.dict(os.environ, {"TRAM_AI_API_KEY": "test-key"}):
            resp = client.get("/api/ai/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["provider"] == "anthropic"  # default

    def test_openai_provider_reported(self):
        app = _make_app()
        client = TestClient(app)
        with patch.dict(os.environ, {
            "TRAM_AI_API_KEY": "test-key",
            "TRAM_AI_PROVIDER": "openai",
        }):
            resp = client.get("/api/ai/status")
        data = resp.json()
        assert data["provider"] == "openai"


class TestAiSuggest:
    def test_503_when_no_key(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        env = {k: v for k, v in os.environ.items() if k != "TRAM_AI_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            resp = client.post("/api/ai/suggest", json={"mode": "generate", "prompt": "test"})
        assert resp.status_code == 503

    def test_generate_mode_calls_ai_and_returns_yaml(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch.dict(os.environ, {"TRAM_AI_API_KEY": "test-key"}), \
             patch("tram.api.routers.ai._call_ai", return_value="name: test\nschedule:\n  type: manual"):
            resp = client.post("/api/ai/suggest", json={
                "mode": "generate",
                "prompt": "create a test pipeline",
                "plugins": {"sources": ["sftp"], "sinks": ["local"], "transforms": [], "serializers": []},
            })
        assert resp.status_code == 200
        assert "yaml" in resp.json()

    def test_explain_mode_calls_ai_and_returns_explanation(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch.dict(os.environ, {"TRAM_AI_API_KEY": "test-key"}), \
             patch("tram.api.routers.ai._call_ai", return_value="The error is in the sink config."):
            resp = client.post("/api/ai/suggest", json={
                "mode": "explain",
                "yaml": "name: test",
                "error": "Missing table field",
            })
        assert resp.status_code == 200
        assert "explanation" in resp.json()

    def test_unknown_mode_returns_400(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch.dict(os.environ, {"TRAM_AI_API_KEY": "test-key"}):
            resp = client.post("/api/ai/suggest", json={"mode": "unknown"})
        assert resp.status_code == 400

    def test_ai_error_returns_502(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch.dict(os.environ, {"TRAM_AI_API_KEY": "test-key"}), \
             patch("tram.api.routers.ai._call_ai", side_effect=RuntimeError("API down")):
            resp = client.post("/api/ai/suggest", json={
                "mode": "generate",
                "prompt": "test",
            })
        assert resp.status_code == 502

    def test_generate_strips_markdown_fences(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        fenced = "```yaml\nname: test\n```"
        with patch.dict(os.environ, {"TRAM_AI_API_KEY": "test-key"}), \
             patch("tram.api.routers.ai._call_ai", return_value=fenced):
            resp = client.post("/api/ai/suggest", json={"mode": "generate", "prompt": "x"})
        assert resp.status_code == 200
        yaml_text = resp.json()["yaml"]
        assert "```" not in yaml_text
