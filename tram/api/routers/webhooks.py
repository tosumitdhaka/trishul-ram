"""Webhook ingress router — forwards HTTP POSTs to registered WebhookSources."""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, HTTPException, Request, Response, status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks")

DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB


def _max_body_bytes() -> int:
    """Maximum accepted webhook body size (TRAM_WEBHOOK_MAX_BODY_BYTES)."""
    raw = os.environ.get("TRAM_WEBHOOK_MAX_BODY_BYTES")
    if raw is None:
        return DEFAULT_MAX_BODY_BYTES
    try:
        return max(int(raw), 0)
    except ValueError:
        logger.warning(
            "Invalid TRAM_WEBHOOK_MAX_BODY_BYTES=%r — using default",
            raw,
        )
        return DEFAULT_MAX_BODY_BYTES


@router.post("/{path:path}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(path: str, request: Request) -> Response:
    """Accept a POST and forward the body to the registered WebhookSource queue.

    Returns 404 if no source is registered for the given path.
    Returns 401 if a secret is configured and the Authorization header doesn't match.
    Returns 413 if the request body exceeds TRAM_WEBHOOK_MAX_BODY_BYTES.
    """
    from tram.connectors.webhook.source import _REGISTRY_LOCK, _WEBHOOK_REGISTRY

    path = path.lstrip("/")

    with _REGISTRY_LOCK:
        q = _WEBHOOK_REGISTRY.get(path)

    if q is None:
        raise HTTPException(status_code=404, detail=f"No webhook source registered for path: {path}")

    # Optional secret validation
    # The secret is stored per-source; we check the Authorization header here.
    # Note: We access the secret from the source config via a separate registry if needed.
    # For now, if a registered source has a secret, the router checks the header.
    # Check if there's an associated secret — stored in a separate secrets dict
    from tram.connectors.webhook import _WEBHOOK_SECRETS  # type: ignore[attr-defined]
    secret = _WEBHOOK_SECRETS.get(path)
    if secret:
        auth_header = request.headers.get("Authorization", "")
        # compare_digest needs bytes; the Authorization header arrives latin-1
        # decoded (latin-1 round-trips any raw value and never raises) while
        # the configured secret is a Unicode string (UTF-8 encodes any str).
        # A non-ASCII header value yields a clean 401, not a TypeError/500.
        if not hmac.compare_digest(
            auth_header.encode("latin-1"), f"Bearer {secret}".encode()
        ):
            raise HTTPException(status_code=401, detail="Invalid or missing Authorization header")

    max_body = _max_body_bytes()

    # Fast-path rejection from the Content-Length header, then a bounded stream
    # read so an oversized payload is never fully buffered in memory.
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > max_body:
        raise HTTPException(status_code=413, detail="Webhook body too large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_body:
            raise HTTPException(status_code=413, detail="Webhook body too large")
    body = bytes(body)

    meta = {
        "source": "webhook",
        "path": path,
        "content_type": request.headers.get("content-type", ""),
    }

    try:
        q.put_nowait((body, meta))
    except Exception as exc:
        logger.warning("Webhook queue full for path %s: %s", path, exc)
        raise HTTPException(status_code=503, detail="Webhook queue full")

    return Response(status_code=202)
