"""Integration tests for API key authentication.

Tests the full FastAPI app with APIKeyMiddleware:
  - TRAM_API_KEY set → 401 without key, 200 with key
  - Exempt paths (/api/health, /api/ready, /metrics, /webhooks/*) always pass
  - TRAM_API_KEY unset → all requests pass
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _make_app(api_key: str = ""):
    """Create a fresh FastAPI app with APIKeyMiddleware, injecting api_key."""
    from tram.api.middleware import APIKeyMiddleware
    from tram.core.config import AppConfig

    mock_config = MagicMock(spec=AppConfig)
    mock_config.api_key = api_key
    mock_config.rate_limit = 0
    mock_config.rate_limit_window = 60
    mock_config.auth_users = {}

    with patch("tram.api.middleware.AppConfig.from_env", return_value=mock_config):
        from fastapi import FastAPI
        app = FastAPI()

        @app.get("/api/pipelines")
        def list_pipelines():
            return {"pipelines": []}

        @app.get("/api/health")
        def health():
            return {"status": "ok"}

        @app.get("/api/ready")
        def ready():
            return {"status": "ready"}

        @app.get("/metrics")
        def metrics():
            return "# metrics"

        @app.post("/webhooks/my-hook")
        def webhook():
            return {"received": True}

        app.add_middleware(APIKeyMiddleware)
        return app, mock_config


class TestAPIKeyAuthIntegration:
    def test_no_key_configured_all_requests_pass(self):
        """When TRAM_API_KEY is empty, no authentication is required."""
        app, mock_config = _make_app(api_key="")

        with patch("tram.api.middleware.AppConfig.from_env", return_value=mock_config):
            client = TestClient(app, raise_server_exceptions=True)
            response = client.get("/api/pipelines")
        assert response.status_code == 200

    def test_valid_key_in_header_allows_request(self):
        """X-API-Key header with correct key allows access."""
        app, mock_config = _make_app(api_key="my-secret")

        with patch("tram.api.middleware.AppConfig.from_env", return_value=mock_config):
            client = TestClient(app, raise_server_exceptions=True)
            response = client.get("/api/pipelines", headers={"X-API-Key": "my-secret"})
        assert response.status_code == 200

    def test_wrong_key_returns_401(self):
        """Wrong key value returns 401 Unauthorized."""
        app, mock_config = _make_app(api_key="my-secret")

        with patch("tram.api.middleware.AppConfig.from_env", return_value=mock_config):
            client = TestClient(app, raise_server_exceptions=True)
            response = client.get("/api/pipelines", headers={"X-API-Key": "wrong-key"})
        assert response.status_code == 401
        assert response.json()["detail"] == "Unauthorized"

    def test_missing_key_returns_401(self):
        """No key provided returns 401 Unauthorized."""
        app, mock_config = _make_app(api_key="my-secret")

        with patch("tram.api.middleware.AppConfig.from_env", return_value=mock_config):
            client = TestClient(app, raise_server_exceptions=True)
            response = client.get("/api/pipelines")
        assert response.status_code == 401

    def test_health_path_always_exempt(self):
        """GET /api/health passes without API key."""
        app, mock_config = _make_app(api_key="my-secret")

        with patch("tram.api.middleware.AppConfig.from_env", return_value=mock_config):
            client = TestClient(app, raise_server_exceptions=True)
            response = client.get("/api/health")
        assert response.status_code == 200

    def test_ready_path_always_exempt(self):
        """GET /api/ready passes without API key."""
        app, mock_config = _make_app(api_key="my-secret")

        with patch("tram.api.middleware.AppConfig.from_env", return_value=mock_config):
            client = TestClient(app, raise_server_exceptions=True)
            response = client.get("/api/ready")
        assert response.status_code == 200

    def test_metrics_path_always_exempt(self):
        """GET /metrics passes without API key."""
        app, mock_config = _make_app(api_key="my-secret")

        with patch("tram.api.middleware.AppConfig.from_env", return_value=mock_config):
            client = TestClient(app, raise_server_exceptions=True)
            response = client.get("/metrics")
        assert response.status_code == 200

    def test_webhooks_path_always_exempt(self):
        """POST /webhooks/* passes without API key."""
        app, mock_config = _make_app(api_key="my-secret")

        with patch("tram.api.middleware.AppConfig.from_env", return_value=mock_config):
            client = TestClient(app, raise_server_exceptions=True)
            response = client.post("/webhooks/my-hook")
        assert response.status_code == 200

    def test_key_via_query_param(self):
        """?api_key=... query param is accepted as authentication."""
        app, mock_config = _make_app(api_key="my-secret")

        with patch("tram.api.middleware.AppConfig.from_env", return_value=mock_config):
            client = TestClient(app, raise_server_exceptions=True)
            response = client.get("/api/pipelines?api_key=my-secret")
        assert response.status_code == 200
