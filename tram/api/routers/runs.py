"""Run history endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from tram.core.exceptions import PipelineNotFoundError

router = APIRouter(prefix="/api")


@router.get("/runs")
async def list_runs(
    request: Request,
    pipeline: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict]:
    """List run history, optionally filtered by pipeline name and status."""
    manager = request.app.state.manager
    runs = manager.get_runs(pipeline_name=pipeline, status=status, limit=limit)
    return [r.to_dict() for r in runs]


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
