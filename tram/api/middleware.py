"""API security middleware: API key authentication and rate limiting."""

from __future__ import annotations

import time
from collections import deque
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from tram.core.config import AppConfig

if TYPE_CHECKING:
    from starlette.requests import Request


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Require X-API-Key header (or ?api_key= query param) for protected endpoints.

    Health probes and webhook ingestion paths are always exempt so container
    orchestrators and external producers never need credentials.
    """

    EXEMPT = {"/api/health", "/api/ready", "/metrics"}
    EXEMPT_PREFIX = "/webhooks/"

    async def dispatch(self, request: "Request", call_next):
        settings = AppConfig.from_env()

        if not settings.api_key:
            return await call_next(request)

        path = request.url.path
        if path in self.EXEMPT or path.startswith(self.EXEMPT_PREFIX):
            return await call_next(request)

        key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if key != settings.api_key:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter for /api/* endpoints.

    Uses a per-IP deque of request timestamps.  Entries older than
    ``window_seconds`` are discarded before each check.

    Only applies to /api/* paths (not /metrics or /webhooks/).
    """

    def __init__(self, app, rate_limit: int = 0, window_seconds: int = 60) -> None:
        super().__init__(app)
        self._rate_limit = rate_limit
        self._window = window_seconds
        # {client_ip: deque[float]}  — timestamps of recent requests
        self._windows: dict[str, deque] = {}

    async def dispatch(self, request: "Request", call_next):
        if self._rate_limit <= 0:
            return await call_next(request)

        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()

        if client_ip not in self._windows:
            self._windows[client_ip] = deque()

        window = self._windows[client_ip]

        # Expire old entries
        cutoff = now - self._window
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= self._rate_limit:
            return JSONResponse(
                {"detail": "Too Many Requests"},
                status_code=429,
                headers={"Retry-After": str(self._window)},
            )

        window.append(now)
        return await call_next(request)
