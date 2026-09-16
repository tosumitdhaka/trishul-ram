"""API security middleware: API key authentication and rate limiting."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from collections import deque
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from tram.core.config import AppConfig

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Require X-API-Key header for protected endpoints.

    Health probes, webhook ingestion paths, and the web UI are always exempt so
    container orchestrators and external producers never need credentials.

    Internal machine-to-machine surfaces (``/api/internal/*`` by default, or any
    prefix passed as ``internal_prefixes``) honor ``TRAM_INTERNAL_AUTH_MODE``:

    * ``off``     — requests pass through with no check and no log
    * ``warn``    — requests with a missing/invalid key are logged at WARNING
                    but still served
    * ``enforce`` — requests with a missing/invalid key are rejected with 401

    The default is ``warn`` (non-breaking rollout): Phase 2 of the security
    rollout flips this to ``enforce`` without code changes. An invalid
    ``TRAM_INTERNAL_AUTH_MODE`` value is logged at WARNING and falls back to
    ``warn``.
    """

    EXEMPT = {"/api/health", "/api/ready", "/agent/health", "/metrics", "/", "/api/auth/login",
              "/favicon.ico", "/docs", "/redoc", "/openapi.json"}
    EXEMPT_PREFIX = ("/webhooks/", "/ui")
    INTERNAL_PREFIX = ("/api/internal/",)

    def __init__(self, app, internal_prefixes=None) -> None:
        super().__init__(app)
        self._settings = AppConfig.from_env()
        raw_mode = os.environ.get("TRAM_INTERNAL_AUTH_MODE", "warn")
        self._mode = raw_mode.lower()
        if self._mode not in ("off", "warn", "enforce"):
            logger.warning(
                "Invalid TRAM_INTERNAL_AUTH_MODE=%r — falling back to 'warn'",
                raw_mode,
            )
            self._mode = "warn"
        self._internal_prefixes = (
            tuple(internal_prefixes) if internal_prefixes else self.INTERNAL_PREFIX
        )

    def _is_internal(self, path: str) -> bool:
        return any(path.startswith(p) for p in self._internal_prefixes)

    async def dispatch(self, request: Request, call_next):
        settings = self._settings

        # No auth configured at all
        if not settings.api_key and not settings.auth_users:
            return await call_next(request)

        path = request.url.path
        if path in self.EXEMPT or any(path.startswith(p) for p in self.EXEMPT_PREFIX):
            return await call_next(request)

        is_internal = self._is_internal(path)

        # Internal machine-to-machine surfaces need a shared machine key; without
        # one there is nothing to validate, so they pass through untouched
        # (enforcement requires TRAM_API_KEY on the server).
        if is_internal and not settings.api_key:
            return await call_next(request)

        # Machine-to-machine: X-API-Key header. The legacy ?api_key= query param
        # was removed — keys in URLs end up in access/proxy logs and history.
        if settings.api_key:
            key = request.headers.get("X-API-Key")
            # compare_digest needs bytes; HTTP/1.1 headers arrive latin-1
            # decoded so latin-1 round-trips any raw value (never raises),
            # while the env-configured key is a proper Unicode string (UTF-8
            # encodes any str). A non-ASCII header therefore yields a clean
            # 401 instead of a TypeError/500.
            if key and hmac.compare_digest(key.encode("latin-1"), settings.api_key.encode()):
                return await call_next(request)
            if is_internal:
                # Internal surfaces respect the auth-mode knob so deployments
                # can roll out keys first and flip to enforcement later.
                if self._mode == "enforce":
                    return JSONResponse({"detail": "Unauthorized"}, status_code=401)
                if self._mode == "warn":
                    remote = request.client.host if request.client else "unknown"
                    logger.warning(
                        "Internal endpoint request with missing or invalid API key",
                        extra={"path": path, "remote": remote},
                    )
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

    Thread safety: each IP slot is guarded by its own ``asyncio.Lock`` so
    concurrent coroutines for the same client cannot both pass the limit check
    before either records the timestamp (TOCTOU race).
    """

    def __init__(self, app, rate_limit: int = 0, window_seconds: int = 60) -> None:
        super().__init__(app)
        self._rate_limit = rate_limit
        self._window = window_seconds
        # {client_ip: deque[float]}  — timestamps of recent requests
        self._windows: dict[str, deque] = {}
        # {client_ip: asyncio.Lock}  — one lock per IP to prevent TOCTOU races
        self._locks: dict[str, asyncio.Lock] = {}

    async def dispatch(self, request: Request, call_next):
        if self._rate_limit <= 0:
            return await call_next(request)

        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"

        # Lazily create per-IP lock (dict setdefault is atomic in CPython asyncio
        # single-thread event loop, but we guard it anyway for clarity).
        if client_ip not in self._locks:
            self._locks[client_ip] = asyncio.Lock()

        async with self._locks[client_ip]:
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
        # high-cardinality client traffic.  Done outside the lock since it only
        # replaces the dict reference and does not mutate individual deques.
        if len(self._windows) > 500:
            self._windows = {k: v for k, v in self._windows.items() if v}
            self._locks = {k: v for k, v in self._locks.items() if k in self._windows}

        return await call_next(request)
