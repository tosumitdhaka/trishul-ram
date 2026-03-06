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


class TestAPIKeyMiddleware:
    def _middleware(self, api_key: str):
        from fastapi import FastAPI
        app = FastAPI()
        mw = APIKeyMiddleware(app)
        return mw, api_key

    @pytest.mark.asyncio
    async def test_no_api_key_configured_allows_all(self):
        """When TRAM_API_KEY is empty, all requests pass through."""
        from fastapi import FastAPI
        app = FastAPI()
        mw = APIKeyMiddleware(app)
        req = _make_request("/api/pipelines")

        with patch("tram.core.config.AppConfig.from_env") as mock_cfg:
            mock_cfg.return_value = MagicMock(api_key="")
            response = await mw.dispatch(req, _call_next)

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_valid_key_in_header_allows_request(self):
        from fastapi import FastAPI
        app = FastAPI()
        mw = APIKeyMiddleware(app)
        req = _make_request("/api/pipelines", headers={"X-API-Key": "secret"})

        with patch("tram.core.config.AppConfig.from_env") as mock_cfg:
            mock_cfg.return_value = MagicMock(api_key="secret")
            response = await mw.dispatch(req, _call_next)

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_wrong_key_returns_401(self):
        from fastapi import FastAPI
        app = FastAPI()
        mw = APIKeyMiddleware(app)
        req = _make_request("/api/pipelines", headers={"X-API-Key": "wrong"})

        with patch("tram.core.config.AppConfig.from_env") as mock_cfg:
            mock_cfg.return_value = MagicMock(api_key="secret")
            response = await mw.dispatch(req, _call_next)

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_key_returns_401(self):
        from fastapi import FastAPI
        app = FastAPI()
        mw = APIKeyMiddleware(app)
        req = _make_request("/api/pipelines")

        with patch("tram.core.config.AppConfig.from_env") as mock_cfg:
            mock_cfg.return_value = MagicMock(api_key="secret")
            response = await mw.dispatch(req, _call_next)

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_health_path_exempt(self):
        from fastapi import FastAPI
        app = FastAPI()
        mw = APIKeyMiddleware(app)
        req = _make_request("/api/health")

        with patch("tram.core.config.AppConfig.from_env") as mock_cfg:
            mock_cfg.return_value = MagicMock(api_key="secret")
            response = await mw.dispatch(req, _call_next)

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_ready_path_exempt(self):
        from fastapi import FastAPI
        app = FastAPI()
        mw = APIKeyMiddleware(app)
        req = _make_request("/api/ready")

        with patch("tram.core.config.AppConfig.from_env") as mock_cfg:
            mock_cfg.return_value = MagicMock(api_key="secret")
            response = await mw.dispatch(req, _call_next)

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_metrics_path_exempt(self):
        from fastapi import FastAPI
        app = FastAPI()
        mw = APIKeyMiddleware(app)
        req = _make_request("/metrics")

        with patch("tram.core.config.AppConfig.from_env") as mock_cfg:
            mock_cfg.return_value = MagicMock(api_key="secret")
            response = await mw.dispatch(req, _call_next)

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_webhooks_path_exempt(self):
        from fastapi import FastAPI
        app = FastAPI()
        mw = APIKeyMiddleware(app)
        req = _make_request("/webhooks/my-endpoint")

        with patch("tram.core.config.AppConfig.from_env") as mock_cfg:
            mock_cfg.return_value = MagicMock(api_key="secret")
            response = await mw.dispatch(req, _call_next)

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_key_via_query_param_allowed(self):
        from fastapi import FastAPI
        app = FastAPI()
        mw = APIKeyMiddleware(app)
        req = _make_request("/api/pipelines", query_string="api_key=secret")

        with patch("tram.core.config.AppConfig.from_env") as mock_cfg:
            mock_cfg.return_value = MagicMock(api_key="secret")
            response = await mw.dispatch(req, _call_next)

        assert response.status_code == 200
