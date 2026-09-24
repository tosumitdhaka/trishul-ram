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

    When no machine key is configured (``auth_users``-only deployments), the
    internal surfaces fall back to the browser Bearer-token check and the same
    mode knob applies — ``enforce`` closes ``/api/internal/*`` without a
    ``TRAM_API_KEY`` instead of leaving it open (GH #44).
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

    def _log_internal_auth_failure(self, path: str, request: Request, *, bearer: bool = False) -> None:
        credential = "bearer token" if bearer else "API key"
        remote = request.client.host if request.client else "unknown"
        logger.warning(
            "Internal endpoint request with missing or invalid %s",
            credential,
            extra={"path": path, "remote": remote},
        )

    async def dispatch(self, request: Request, call_next):
        settings = self._settings

        # No auth configured at all
        if not settings.api_key and not settings.auth_users:
            return await call_next(request)

        path = request.url.path
        if path in self.EXEMPT or any(path.startswith(p) for p in self.EXEMPT_PREFIX):
            return await call_next(request)

        is_internal = self._is_internal(path)

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
                    self._log_internal_auth_failure(path, request)
                return await call_next(request)

        # Browser session: Bearer token
        if settings.auth_users:
            from tram.api.auth import extract_bearer, verify_token
            token = extract_bearer(request)
            if token and verify_token(token):
                return await call_next(request)
            if is_internal:
                # No machine key configured (auth_users-only deployment):
                # internal surfaces fall back to the bearer check and still
                # honor the auth-mode knob for rollout semantics — enforce
                # closes /api/internal/* without a TRAM_API_KEY (GH #44).
                if self._mode == "enforce":
                    return JSONResponse({"detail": "Unauthorized"}, status_code=401)
                if self._mode == "warn":
                    self._log_internal_auth_failure(path, request, bearer=True)
                return await call_next(request)

        return JSONResponse({"detail": "Unauthorized"}, status_code=401)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter for /api/* and /webhooks/ endpoints.

    Uses a per-IP deque of request timestamps.  Entries older than
    ``window_seconds`` are discarded before each check.

    Thread safety: each IP slot is guarded by its own ``asyncio.Lock`` so
    concurrent coroutines for the same client cannot both pass the limit check
    before either records the timestamp (TOCTOU race).

    Memory bound: once the tracked set exceeds 500 IPs, slots idle for longer
    than twice the window (min 60 s) are evicted.  Eviction only touches slots
    with no in-flight request (``_inflight``), so a coroutine awaiting the
    per-IP lock can never be bypassed by a fresh lock/window — the previous
    wholesale dict rebuild outside any lock could strand such a coroutine and
    undercount the window (GH #45).
    """

    def __init__(self, app, rate_limit: int = 0, window_seconds: int = 60) -> None:
        super().__init__(app)
        self._rate_limit = rate_limit
        self._window = window_seconds
        self._evict_after = max(window_seconds * 2, 60)
        # {client_ip: deque[float]}  — timestamps of recent requests
        self._windows: dict[str, deque] = {}
        # {client_ip: asyncio.Lock}  — one lock per IP to prevent TOCTOU races
        self._locks: dict[str, asyncio.Lock] = {}
        # {client_ip: float}  — monotonic time of the slot's last request
        self._last_seen: dict[str, float] = {}
        # {client_ip: int}  — in-flight dispatches (entered, slot not yet exited)
        self._inflight: dict[str, int] = {}

    async def dispatch(self, request: Request, call_next):
        if self._rate_limit <= 0:
            return await call_next(request)

        path = request.url.path
        if not path.startswith(("/api/", "/webhooks/")):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"

        # Lazily create per-IP lock (dict setdefault is atomic in CPython asyncio
        # single-thread event loop, but we guard it anyway for clarity).
        if client_ip not in self._locks:
            self._locks[client_ip] = asyncio.Lock()
        # Mark the slot in-flight before the (possibly awaiting) lock acquire so
        # the eviction sweep can never drop a lock a coroutine is waiting on.
        self._inflight[client_ip] = self._inflight.get(client_ip, 0) + 1

        try:
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
                self._last_seen[client_ip] = now
        finally:
            remaining = self._inflight.get(client_ip, 0) - 1
            if remaining > 0:
                self._inflight[client_ip] = remaining
            else:
                self._inflight.pop(client_ip, None)
            self._evict_if_large()

        return await call_next(request)

    def _evict_if_large(self) -> None:
        """Drop long-idle per-IP slots once the tracked set grows large.

        Runs synchronously (no awaits) inside one dispatch, and only touches
        slots with no in-flight request — so no coroutine can be waiting on an
        evicted lock.  A slot idle for ``_evict_after`` seconds has a fully
        expired window, so its replacement starts from a clean slate without
        losing any in-window counts.
        """
        if len(self._windows) <= 500:
            return
        cutoff = time.monotonic() - self._evict_after
        stale = [
            ip
            for ip in list(self._locks)
            if self._inflight.get(ip, 0) == 0 and self._last_seen.get(ip, 0) < cutoff
        ]
        for ip in stale:
            self._locks.pop(ip, None)
            self._windows.pop(ip, None)
            self._last_seen.pop(ip, None)
