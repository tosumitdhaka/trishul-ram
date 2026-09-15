"""Tests for API key auth and rate-limit middleware."""
from __future__ import annotations

import logging
import time
from unittest.mock import patch

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

    @app.get("/api/ready")
    async def ready():
        return {"status": "ready"}

    @app.get("/agent/health")
    async def agent_health():
        return {"status": "ok"}

    @app.get("/api/internal/test")
    async def internal_test():
        return {"ok": True}

    @app.get("/webhooks/test")
    async def webhook():
        return {"ok": True}

    @app.get("/ui/index.html")
    async def ui():
        return {"ok": True}

    return app


def _client_with_auth(
    monkeypatch,
    api_key: str = "",
    auth_users: str = "",
    mode: str = "warn",
) -> TestClient:
    """Create a TestClient with APIKeyMiddleware using env-var-based config.

    Starlette builds the middleware stack lazily on the first request, so the
    patch must be active during the request, not just during app construction.
    Using monkeypatch.setenv keeps the override active for the whole test.
    """
    monkeypatch.setenv("TRAM_API_KEY", api_key)
    monkeypatch.setenv("TRAM_AUTH_USERS", auth_users)
    monkeypatch.setenv("TRAM_INTERNAL_AUTH_MODE", mode)
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

    def test_api_key_via_query_param_rejected(self, monkeypatch):
        """?api_key= query param no longer authenticates — use X-API-Key header."""
        client = _client_with_auth(monkeypatch, api_key="secret")
        r = client.get("/api/data?api_key=secret")
        assert r.status_code == 401

    def test_wrong_query_param_returns_401(self, monkeypatch):
        """Query param is ignored entirely (no X-API-Key header → 401)."""
        client = _client_with_auth(monkeypatch, api_key="secret")
        r = client.get("/api/data?api_key=wrong")
        assert r.status_code == 401

    def test_agent_health_exempt(self, monkeypatch):
        """Worker probe path is always exempt."""
        client = _client_with_auth(monkeypatch, api_key="secret", mode="enforce")
        r = client.get("/agent/health")
        assert r.status_code == 200

    # ── Internal surface auth-mode knob ─────────────────────────────────────

    def test_internal_warn_mode_serves_missing_key(self, monkeypatch, caplog):
        """Warn mode: /api/internal/* without a key is served and logged."""
        caplog.set_level(logging.WARNING, logger="tram.api.middleware")
        client = _client_with_auth(monkeypatch, api_key="secret", mode="warn")
        r = client.get("/api/internal/test")
        assert r.status_code == 200
        assert any("missing or invalid API key" in rec.getMessage() for rec in caplog.records)

    def test_internal_warn_mode_serves_wrong_key(self, monkeypatch, caplog):
        """Warn mode: /api/internal/* with a wrong key is served and logged."""
        caplog.set_level(logging.WARNING, logger="tram.api.middleware")
        client = _client_with_auth(monkeypatch, api_key="secret", mode="warn")
        r = client.get("/api/internal/test", headers={"X-API-Key": "wrong"})
        assert r.status_code == 200
        assert any("missing or invalid API key" in rec.getMessage() for rec in caplog.records)

    def test_internal_off_mode_serves_without_log(self, monkeypatch, caplog):
        """Off mode: /api/internal/* passes with no check and no log."""
        caplog.set_level(logging.WARNING, logger="tram.api.middleware")
        client = _client_with_auth(monkeypatch, api_key="secret", mode="off")
        r = client.get("/api/internal/test")
        assert r.status_code == 200
        assert not any(rec.name == "tram.api.middleware" for rec in caplog.records)

    def test_internal_enforce_mode_rejects_missing_key(self, monkeypatch):
        """Enforce mode: /api/internal/* without a key is rejected."""
        client = _client_with_auth(monkeypatch, api_key="secret", mode="enforce")
        r = client.get("/api/internal/test")
        assert r.status_code == 401

    def test_internal_enforce_mode_rejects_wrong_key(self, monkeypatch):
        client = _client_with_auth(monkeypatch, api_key="secret", mode="enforce")
        r = client.get("/api/internal/test", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401

    def test_invalid_auth_mode_logs_warning_and_falls_back_to_warn(self, monkeypatch, caplog):
        """A typo in TRAM_INTERNAL_AUTH_MODE is logged and degrades to warn."""
        caplog.set_level(logging.WARNING, logger="tram.api.middleware")
        client = _client_with_auth(monkeypatch, api_key="secret", mode="typ0")
        # warn-mode behavior: internal request without a key is served
        r = client.get("/api/internal/test")
        assert r.status_code == 200
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("Invalid TRAM_INTERNAL_AUTH_MODE" in m and "typ0" in m for m in messages)
        assert any("missing or invalid API key" in m for m in messages)

    def test_invalid_auth_mode_missing_key_still_rejected_on_public_surface(self, monkeypatch):
        """Fallback to warn only affects internal surfaces — public /api/* still 401s."""
        client = _client_with_auth(monkeypatch, api_key="secret", mode="bogus")
        r = client.get("/api/data")
        assert r.status_code == 401

    async def test_non_ascii_api_key_header_returns_401_not_500(self, monkeypatch):
        """Raw-socket latin-1 header bytes must yield 401, not a TypeError/500.

        Mirrors the reviewer's raw-ASGI reproduction: `X-API-Key: k\\xff`
        used to crash `hmac.compare_digest` on a `str` operand.
        """
        import httpx

        monkeypatch.setenv("TRAM_API_KEY", "secret")
        monkeypatch.setenv("TRAM_AUTH_USERS", "")
        monkeypatch.setenv("TRAM_INTERNAL_AUTH_MODE", "enforce")
        app = _make_base_app()
        app.add_middleware(APIKeyMiddleware)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/data", headers=[(b"x-api-key", b"k\xff")])
            assert r.status_code == 401
            r2 = await client.get("/api/internal/test", headers=[(b"x-api-key", b"k\xff")])
            assert r2.status_code == 401

    async def test_non_ascii_key_warn_mode_serves_internal_and_logs(self, monkeypatch, caplog):
        """Warn mode keeps serving internal requests even with non-ASCII keys."""
        import httpx

        caplog.set_level(logging.WARNING, logger="tram.api.middleware")
        monkeypatch.setenv("TRAM_API_KEY", "secret")
        monkeypatch.setenv("TRAM_AUTH_USERS", "")
        monkeypatch.setenv("TRAM_INTERNAL_AUTH_MODE", "warn")
        app = _make_base_app()
        app.add_middleware(APIKeyMiddleware)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/internal/test", headers=[(b"x-api-key", b"k\xff")])
            assert r.status_code == 200
        assert any("missing or invalid API key" in rec.getMessage() for rec in caplog.records)

    def test_internal_enforce_mode_accepts_correct_key(self, monkeypatch):
        client = _client_with_auth(monkeypatch, api_key="secret", mode="enforce")
        r = client.get("/api/internal/test", headers={"X-API-Key": "secret"})
        assert r.status_code == 200

    def test_internal_passes_when_no_api_key_configured(self, monkeypatch):
        """Without a machine key configured, internal surfaces stay open."""
        client = _client_with_auth(monkeypatch, api_key="", auth_users="admin:pass", mode="enforce")
        r = client.get("/api/internal/test")
        assert r.status_code == 200

    def test_probe_always_exempt_in_enforce_mode(self, monkeypatch):
        """Probe endpoints are exempt regardless of auth mode."""
        client = _client_with_auth(monkeypatch, api_key="secret", mode="enforce")
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/ready").status_code == 200
        assert client.get("/agent/health").status_code == 200

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
