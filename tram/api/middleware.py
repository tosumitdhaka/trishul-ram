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

    EXEMPT = {"/api/health", "/api/ready", "/metrics", "/", "/api/auth/login"}
    EXEMPT_PREFIX = ("/webhooks/", "/ui")

    def __init__(self, app) -> None:
        super().__init__(app)
        self._settings = AppConfig.from_env()

    async def dispatch(self, request: "Request", call_next):
        settings = self._settings

        # No auth configured at all
        if not settings.api_key and not settings.auth_users:
            return await call_next(request)

        path = request.url.path
        if path in self.EXEMPT or any(path.startswith(p) for p in self.EXEMPT_PREFIX):
            return await call_next(request)

        # Machine-to-machine: X-API-Key
        if settings.api_key:
            key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
            if key == settings.api_key:
                return await call_next(request)

        # Browser session: Bearer token
        if settings.auth_users:
            from tram.api.auth import extract_bearer, verify_token
            token = extract_bearer(request)
            if token and verify_token(token):
                return await call_next(request)

        return JSONResponse({"detail": "Unauthorized"}, status_code=401)


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

        # Periodically evict idle client entries (empty deques whose last request
        # fell outside the window) to prevent unbounded dict growth under
        # high-cardinality client traffic.
        if len(self._windows) > 500:
            self._windows = {k: v for k, v in self._windows.items() if v}

        return await call_next(request)
