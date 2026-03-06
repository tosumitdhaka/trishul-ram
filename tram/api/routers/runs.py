"""Run history endpoints."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from tram.core.exceptions import PipelineNotFoundError

router = APIRouter(prefix="/api")


@router.get("/runs")
async def list_runs(
    request: Request,
    pipeline: Optional[str] = Query(None, description="Filter by pipeline name"),
    status: Optional[str] = Query(None, description="Filter by status (success/failed/aborted)"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    offset: int = Query(0, ge=0, description="Records to skip (for pagination)"),
    from_dt: Optional[datetime] = Query(None, description="Only runs started at or after this ISO timestamp"),
    format: Optional[Literal["json", "csv"]] = Query(None, description="Response format (json or csv)"),
):
    """List run history with optional filtering and pagination."""
    manager = request.app.state.manager
    runs = manager.get_runs(
        pipeline_name=pipeline,
        status=status,
        limit=limit,
        offset=offset,
        from_dt=from_dt,
    )
    rows = [r.to_dict() for r in runs]

    if format == "csv":
        if not rows:
            csv_content = ""
        else:
            buf = io.StringIO()
            fieldnames = list(rows[0].keys())
            writer = csv.DictWriter(buf, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            csv_content = buf.getvalue()

        return StreamingResponse(
            iter([csv_content]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=runs.csv"},
        )

    return rows


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict:
    """Get a single run result by run_id."""
    manager = request.app.state.manager
    result = manager.get_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return result.to_dict()


@router.get("/daemon/status")
async def daemon_status(request: Request) -> dict:
    """Scheduler state, active streams, next scheduled runs."""
    scheduler = request.app.state.scheduler
    return scheduler.get_status()


@router.post("/daemon/stop")
async def daemon_stop(request: Request) -> dict:
    """Graceful shutdown."""
    import asyncio

    async def _shutdown():
        await asyncio.sleep(0.5)
        import os
        import signal
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_shutdown())
    return {"status": "shutting down"}
