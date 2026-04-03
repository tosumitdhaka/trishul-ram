"""Tests for RateLimitMiddleware."""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from tram.api.middleware import RateLimitMiddleware


def _make_request(path: str, client_ip: str = "127.0.0.1") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [],
        "client": (client_ip, 12345),
    }
    return Request(scope)


async def _call_next(request):
    return JSONResponse({"ok": True}, status_code=200)


class TestRateLimitMiddleware:
    @pytest.mark.asyncio
    async def test_disabled_when_rate_limit_zero(self):
        """rate_limit=0 means middleware is disabled."""
        from fastapi import FastAPI
        app = FastAPI()
        mw = RateLimitMiddleware(app, rate_limit=0, window_seconds=60)
        req = _make_request("/api/pipelines")
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_non_api_paths_not_rate_limited(self):
        """Non /api/* paths are not rate limited."""
        from fastapi import FastAPI
        app = FastAPI()
        mw = RateLimitMiddleware(app, rate_limit=1, window_seconds=60)
        for _ in range(5):
            response = await mw.dispatch(_make_request("/metrics"), _call_next)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_allows_requests_within_limit(self):
        """Requests within limit should pass through."""
        from fastapi import FastAPI
        app = FastAPI()
        mw = RateLimitMiddleware(app, rate_limit=5, window_seconds=60)
        req = _make_request("/api/pipelines")
        for _ in range(5):
            response = await mw.dispatch(req, _call_next)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_rejects_request_over_limit(self):
        """Requests over limit should return 429."""
        from fastapi import FastAPI
        app = FastAPI()
        mw = RateLimitMiddleware(app, rate_limit=2, window_seconds=60)
        req = _make_request("/api/pipelines")

        # First two pass
        for _ in range(2):
            response = await mw.dispatch(req, _call_next)
            assert response.status_code == 200

        # Third should be rejected
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 429

    @pytest.mark.asyncio
    async def test_different_ips_have_independent_limits(self):
        """Different client IPs maintain independent sliding windows."""
        from fastapi import FastAPI
        app = FastAPI()
        mw = RateLimitMiddleware(app, rate_limit=1, window_seconds=60)

        req_a = _make_request("/api/pipelines", client_ip="10.0.0.1")
        req_b = _make_request("/api/pipelines", client_ip="10.0.0.2")

        # Both should succeed (first request for each IP)
        resp_a = await mw.dispatch(req_a, _call_next)
        resp_b = await mw.dispatch(req_b, _call_next)
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200

        # Second request from same IP should be rejected
        resp_a2 = await mw.dispatch(req_a, _call_next)
        assert resp_a2.status_code == 429

    @pytest.mark.asyncio
    async def test_window_expiry_resets_count(self):
        """After window expires, requests are allowed again."""
        from fastapi import FastAPI
        app = FastAPI()
        mw = RateLimitMiddleware(app, rate_limit=1, window_seconds=1)
        req = _make_request("/api/pipelines")

        # First request passes
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 200

        # Second request fails
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 429

        # After window (simulated by injecting old timestamp into window)
        mw._windows["127.0.0.1"].clear()  # clear all timestamps
        # Now should pass again
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 200
