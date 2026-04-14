"""Internal API — worker-to-manager callbacks.

These endpoints are called by tram-worker agents, not by external clients.
They are excluded from the public OpenAPI schema and exempt from API key auth.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)


class RunCompletePayload(BaseModel):
    run_id: str
    pipeline_name: str
    status: str         # success | error | failed
    records_in: int = 0
    records_out: int = 0
    error: str | None = None


@router.post("/api/internal/run-complete")
async def run_complete(payload: RunCompletePayload, request: Request) -> dict:
    """Worker callback: a dispatched pipeline run has finished.

    The controller updates in-memory state and DB, then applies the normal
    post-run state machine (scheduled → running → scheduled/stopped/error).
    """
    controller = request.app.state.controller

    logger.debug(
        "run-complete received",
        extra={
            "run_id": payload.run_id,
            "pipeline": payload.pipeline_name,
            "status": payload.status,
        },
    )

    controller.on_worker_run_complete(
        run_id=payload.run_id,
        pipeline_name=payload.pipeline_name,
        status=payload.status,
        records_in=payload.records_in,
        records_out=payload.records_out,
        error=payload.error,
    )
    return {"ok": True}
