"""Prometheus metrics endpoint."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

router = APIRouter()


@router.get("/metrics")
async def metrics() -> Response:
    """Expose Prometheus metrics in text format.

    Returns 503 if prometheus_client is not installed.
    """
    from tram.metrics.registry import _PROMETHEUS_AVAILABLE

    if not _PROMETHEUS_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="prometheus_client is not installed. Install with: pip install tram[metrics]",
        )

    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    output = generate_latest()
    return Response(content=output, media_type=CONTENT_TYPE_LATEST)
