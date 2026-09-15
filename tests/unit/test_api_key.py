"""Tests for APIKeyMiddleware."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

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


def _make_request_raw_headers(path: str, headers: list[tuple[bytes, bytes]]) -> Request:
    """Build a request from raw (already-encoded) header bytes.

    HTTP/1.1 clients can send arbitrary latin-1 bytes in header values
    (e.g. ``X-API-Key: k\\xff`` from a raw socket); mirroring that here lets
    us exercise the exact crash surface without TestClient/httpx string
    re-encoding.
    """
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [(k.lower(), v) for k, v in headers],
    }
    return Request(scope)


async def _call_next(request):
    return JSONResponse({"ok": True}, status_code=200)


def _make_middleware(api_key: str, auth_users: str = ""):
    """Create APIKeyMiddleware with a patched AppConfig that returns the given api_key."""
    from fastapi import FastAPI
    app = FastAPI()
    # Middleware reads config in __init__, so patch must be active during instantiation.
    with patch("tram.api.middleware.AppConfig.from_env") as mock_cfg:
        mock_cfg.return_value = MagicMock(api_key=api_key, auth_users=auth_users)
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
    async def test_key_via_query_param_rejected(self):
        """The ?api_key= query param was removed — X-API-Key header is required."""
        mw = _make_middleware(api_key="secret")
        req = _make_request("/api/pipelines", query_string="api_key=secret")
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_non_ascii_header_value_returns_401_not_500(self):
        """A raw-socket client sending `X-API-Key: k\\xff` must get a clean 401.

        Header values arrive latin-1-decoded; `hmac.compare_digest` on the raw
        `str` raised TypeError -> unhandled 500. Bytes comparison fixes it.
        """
        mw = _make_middleware(api_key="secret")
        req = _make_request_raw_headers("/api/pipelines", [(b"x-api-key", b"k\xff")])
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_non_ascii_key_round_trips_utf8_wire_bytes(self):
        """A non-ASCII key sent as its UTF-8 wire bytes still authenticates.

        Header latin-1 round-trip + key UTF-8 encode must agree for the same
        key string, so valid non-ASCII keys keep working.
        """
        mw = _make_middleware(api_key="caf\u00e9")
        req = _make_request_raw_headers("/api/pipelines", [(b"x-api-key", "caf\u00e9".encode())])
        response = await mw.dispatch(req, _call_next)
        assert response.status_code == 200
