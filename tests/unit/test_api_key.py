"""Tests for APIKeyMiddleware."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.datastructures import URL, Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from tram.api.middleware import APIKeyMiddleware


def _make_request(path: str, headers: dict | None = None, query_string: str = "") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": query_string.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    return Request(scope)


async def _call_next(request):
    return JSONResponse({"ok": True}, status_code=200)


def _make_middleware(api_key: str):
    """Create APIKeyMiddleware with a patched AppConfig that returns the given api_key."""
    from fastapi import FastAPI
    app = FastAPI()
    # Middleware reads config in __init__, so patch must be active during instantiation.
    with patch("tram.api.middleware.AppConfig.from_env") as mock_cfg:
        mock_cfg.return_value = MagicMock(api_key=api_key)
        mw = APIKeyMiddleware(app)
    return mw


class TestAPIKeyMiddleware:
    @pytest.mark.asyncio
    async def test_no_api_key_configured_allows_all(self):
        """When TRAM_API_KEY is empty, all requests pass through."""
        mw = _make_middleware(api_key="")
        req = _make_request("/api/pipelines")
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_valid_key_in_header_allows_request(self):
        mw = _make_middleware(api_key="secret")
        req = _make_request("/api/pipelines", headers={"X-API-Key": "secret"})
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_wrong_key_returns_401(self):
        mw = _make_middleware(api_key="secret")
        req = _make_request("/api/pipelines", headers={"X-API-Key": "wrong"})
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_key_returns_401(self):
        mw = _make_middleware(api_key="secret")
        req = _make_request("/api/pipelines")
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_health_path_exempt(self):
        mw = _make_middleware(api_key="secret")
        req = _make_request("/api/health")
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_ready_path_exempt(self):
        mw = _make_middleware(api_key="secret")
        req = _make_request("/api/ready")
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_metrics_path_exempt(self):
        mw = _make_middleware(api_key="secret")
        req = _make_request("/metrics")
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_webhooks_path_exempt(self):
        mw = _make_middleware(api_key="secret")
        req = _make_request("/webhooks/my-endpoint")
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_key_via_query_param_allowed(self):
        mw = _make_middleware(api_key="secret")
        req = _make_request("/api/pipelines", query_string="api_key=secret")
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 200
