"""Internal API — worker-to-manager callbacks.

These endpoints are called by tram-worker agents, not by external clients.
They are excluded from the public OpenAPI schema and exempt from API key auth.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)


class RunCompletePayload(BaseModel):
    run_id: str
    pipeline_name: str
    status: str         # success | error | failed
    records_in: int = 0
    records_out: int = 0
    records_skipped: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    error: str | None = None
    errors: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class PipelineStatsPayload(BaseModel):
    worker_id: str
    pipeline_name: str
    run_id: str
    schedule_type: str
    uptime_seconds: float
    timestamp: datetime
    records_in: int = 0
    records_out: int = 0
    records_skipped: int = 0
    dlq_count: int = 0
    error_count: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    errors_last_window: list[str] = Field(default_factory=list)
    is_final: bool = False


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
        records_skipped=payload.records_skipped,
        bytes_in=payload.bytes_in,
        bytes_out=payload.bytes_out,
        error=payload.error,
        errors=payload.errors,
        started_at=payload.started_at,
        finished_at=payload.finished_at,
    )
    return {"ok": True}


@router.post("/api/internal/pipeline-stats")
async def pipeline_stats(payload: PipelineStatsPayload, request: Request) -> dict:
    store = request.app.state.stats_store
    controller = request.app.state.controller
    if payload.is_final:
        store.remove(payload.run_id)
    else:
        store.update(payload)
        controller.on_pipeline_stats(payload)
    return {"ok": True}
