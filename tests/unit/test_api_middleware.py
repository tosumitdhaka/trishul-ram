"""Tests for API key auth and rate-limit middleware."""
from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.middleware import APIKeyMiddleware, RateLimitMiddleware


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_base_app() -> FastAPI:
    """Plain FastAPI app with test routes — no middleware attached."""
    app = FastAPI()

    @app.get("/api/data")
    async def data():
        return {"ok": True}

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/webhooks/test")
    async def webhook():
        return {"ok": True}

    @app.get("/ui/index.html")
    async def ui():
        return {"ok": True}

    return app


def _client_with_auth(monkeypatch, api_key: str = "", auth_users: str = "") -> TestClient:
    """Create a TestClient with APIKeyMiddleware using env-var-based config.

    Starlette builds the middleware stack lazily on the first request, so the
    patch must be active during the request, not just during app construction.
    Using monkeypatch.setenv keeps the override active for the whole test.
    """
    monkeypatch.setenv("TRAM_API_KEY", api_key)
    monkeypatch.setenv("TRAM_AUTH_USERS", auth_users)
    app = _make_base_app()
    app.add_middleware(APIKeyMiddleware)
    return TestClient(app)


def _make_app_with_rate_limit(rate_limit: int = 3, window: int = 60) -> FastAPI:
    app = FastAPI()

    @app.get("/api/data")
    async def data():
        return {"ok": True}

    @app.get("/metrics")
    async def metrics():
        return {}

    app.add_middleware(RateLimitMiddleware, rate_limit=rate_limit, window_seconds=window)
    return app


# ── APIKeyMiddleware ───────────────────────────────────────────────────────


class TestAPIKeyMiddleware:
    def test_no_auth_configured_passes_through(self, monkeypatch):
        """When neither api_key nor auth_users is set, all requests pass."""
        client = _client_with_auth(monkeypatch, api_key="", auth_users="")
        r = client.get("/api/data")
        assert r.status_code == 200

    def test_exempt_health_path(self, monkeypatch):
        """Health probe always passes without auth."""
        client = _client_with_auth(monkeypatch, api_key="secret")
        r = client.get("/api/health")
        assert r.status_code == 200

    def test_exempt_webhook_prefix(self, monkeypatch):
        """Webhooks are always exempt."""
        client = _client_with_auth(monkeypatch, api_key="secret")
        r = client.get("/webhooks/test")
        assert r.status_code == 200

    def test_exempt_ui_prefix(self, monkeypatch):
        """UI paths are always exempt."""
        client = _client_with_auth(monkeypatch, api_key="secret")
        r = client.get("/ui/index.html")
        assert r.status_code == 200

    def test_correct_api_key_header_passes(self, monkeypatch):
        client = _client_with_auth(monkeypatch, api_key="secret")
        r = client.get("/api/data", headers={"X-API-Key": "secret"})
        assert r.status_code == 200

    def test_wrong_api_key_returns_401(self, monkeypatch):
        client = _client_with_auth(monkeypatch, api_key="secret")
        r = client.get("/api/data", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401

    def test_missing_api_key_returns_401(self, monkeypatch):
        client = _client_with_auth(monkeypatch, api_key="secret")
        r = client.get("/api/data")
        assert r.status_code == 401

    def test_api_key_via_query_param(self, monkeypatch):
        client = _client_with_auth(monkeypatch, api_key="secret")
        r = client.get("/api/data?api_key=secret")
        assert r.status_code == 200

    def test_wrong_query_param_returns_401(self, monkeypatch):
        client = _client_with_auth(monkeypatch, api_key="secret")
        r = client.get("/api/data?api_key=wrong")
        assert r.status_code == 401

    def test_bearer_token_valid_passes(self, monkeypatch):
        """Valid Bearer token passes when auth_users configured."""
        client = _client_with_auth(monkeypatch, auth_users="admin:pass")
        mock_token = "validtoken"
        with patch("tram.api.auth.extract_bearer", return_value=mock_token), \
             patch("tram.api.auth.verify_token", return_value=True):
            r = client.get("/api/data", headers={"Authorization": f"Bearer {mock_token}"})
        assert r.status_code == 200

    def test_bearer_token_invalid_returns_401(self, monkeypatch):
        client = _client_with_auth(monkeypatch, auth_users="admin:pass")
        with patch("tram.api.auth.extract_bearer", return_value="bad"), \
             patch("tram.api.auth.verify_token", return_value=False):
            r = client.get("/api/data", headers={"Authorization": "Bearer bad"})
        assert r.status_code == 401

    def test_no_bearer_returns_401(self, monkeypatch):
        client = _client_with_auth(monkeypatch, auth_users="admin:pass")
        with patch("tram.api.auth.extract_bearer", return_value=None):
            r = client.get("/api/data")
        assert r.status_code == 401

    def test_api_key_takes_precedence_when_both_configured(self, monkeypatch):
        """If both api_key and auth_users set, valid api_key passes without checking token."""
        client = _client_with_auth(monkeypatch, api_key="secret", auth_users="admin:pass")
        r = client.get("/api/data", headers={"X-API-Key": "secret"})
        assert r.status_code == 200


# ── RateLimitMiddleware ────────────────────────────────────────────────────


class TestRateLimitMiddleware:
    def test_disabled_rate_limit_passes_through(self):
        app = _make_app_with_rate_limit(rate_limit=0)
        client = TestClient(app)
        for _ in range(10):
            r = client.get("/api/data")
        assert r.status_code == 200

    def test_under_limit_passes(self):
        app = _make_app_with_rate_limit(rate_limit=5)
        client = TestClient(app)
        for _ in range(5):
            r = client.get("/api/data")
        assert r.status_code == 200

    def test_over_limit_returns_429(self):
        app = _make_app_with_rate_limit(rate_limit=3)
        client = TestClient(app)
        for _ in range(3):
            client.get("/api/data")
        r = client.get("/api/data")
        assert r.status_code == 429
        assert "Retry-After" in r.headers

    def test_non_api_path_bypasses_rate_limit(self):
        app = _make_app_with_rate_limit(rate_limit=1)
        client = TestClient(app)
        # Exhaust limit on /api/data
        client.get("/api/data")
        # /metrics should not be rate-limited
        r = client.get("/metrics")
        assert r.status_code == 200

    def test_window_expiry_resets_counter(self):
        """After the window expires, requests are allowed again."""
        app = _make_app_with_rate_limit(rate_limit=2, window=1)
        client = TestClient(app)

        client.get("/api/data")
        client.get("/api/data")
        r = client.get("/api/data")
        assert r.status_code == 429

        # Wait for window to expire
        time.sleep(1.1)
        r = client.get("/api/data")
        assert r.status_code == 200

    def test_429_detail_message(self):
        app = _make_app_with_rate_limit(rate_limit=1)
        client = TestClient(app)
        client.get("/api/data")
        r = client.get("/api/data")
        assert r.status_code == 429
        assert "Too Many Requests" in r.json()["detail"]
