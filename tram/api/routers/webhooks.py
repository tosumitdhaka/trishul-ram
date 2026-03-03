"""Webhook ingress router — forwards HTTP POSTs to registered WebhookSources."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response, status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks")


@router.post("/{path:path}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(path: str, request: Request) -> Response:
    """Accept a POST and forward the body to the registered WebhookSource queue.

    Returns 404 if no source is registered for the given path.
    Returns 401 if a secret is configured and the Authorization header doesn't match.
    """
    from tram.connectors.webhook.source import _WEBHOOK_REGISTRY, _REGISTRY_LOCK

    path = path.lstrip("/")

    with _REGISTRY_LOCK:
        q = _WEBHOOK_REGISTRY.get(path)

    if q is None:
        raise HTTPException(status_code=404, detail=f"No webhook source registered for path: {path}")

    # Optional secret validation
    # The secret is stored per-source; we check the Authorization header here.
    # Note: We access the secret from the source config via a separate registry if needed.
    # For now, if a registered source has a secret, the router checks the header.
    from tram.connectors.webhook.source import _WEBHOOK_REGISTRY as _REG  # noqa: F811
    # Check if there's an associated secret — stored in a separate secrets dict
    from tram.connectors.webhook import _WEBHOOK_SECRETS  # type: ignore[attr-defined]
    secret = _WEBHOOK_SECRETS.get(path)
    if secret:
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {secret}":
            raise HTTPException(status_code=401, detail="Invalid or missing Authorization header")

    body = await request.body()
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
