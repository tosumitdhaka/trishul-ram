"""Run history endpoints."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text

router = APIRouter(prefix="/api")


def _queued_run_to_dict(row: dict) -> dict:
    """Shape a queued_runs row exactly like RunResult.to_dict() (E.2 §8.1).

    ``started_at`` = the queue request time; ``finished_at`` stays None (the
    request is not a completed run). ``status`` renders as "queued" for both
    queued and dispatching rows — the run is not running until dispatched.
    """
    return {
        "run_id": row["run_id"],
        "pipeline": row["pipeline_name"],
        "status": "queued",
        "started_at": row["requested_at"].isoformat(),
        "finished_at": None,
        "records_in": 0,
        "records_out": 0,
        "records_skipped": 0,
        "bytes_in": 0,
        "bytes_out": 0,
        "dlq_count": 0,
        "error": None,
        "errors": [],
        "node": None,
    }


@router.get("/runs")
async def list_runs(
    request: Request,
    pipeline: str | None = Query(None, description="Filter by pipeline name"),
    status: str | None = Query(None, description="Filter by status (success/failed/aborted)"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    offset: int = Query(0, ge=0, description="Records to skip (for pagination)"),
    from_dt: datetime | None = Query(None, description="Only runs started at or after this ISO timestamp"),
    format: Literal["json", "csv"] | None = Query(None, description="Response format (json or csv)"),
):
    """List run history with optional filtering and pagination.

    E.2 (§8.1): queued manual runs (non-terminal queued_runs rows) are merged
    into the listing, shaped like ``RunResult.to_dict()``. Dedupe bounds the
    merge to ≤ 1 row per pipeline, so the pagination skew (queued rows sort by
    started_at among the run-history rows) is bounded and documented.
    """
    controller = request.app.state.controller
    runs = controller.get_runs(
        pipeline_name=pipeline,
        status=status,
        limit=limit,
        offset=offset,
        from_dt=from_dt,
    )
    rows = [r.to_dict() for r in runs]

    # Merge queued runs before serialization so CSV export inherits them too
    # (finished_at: None renders empty).
    db = getattr(request.app.state, "db", None)
    if db is not None:
        queued_rows = [
            _queued_run_to_dict(run)
            for run in db.get_queued_run_view()
            if (pipeline is None or run["pipeline_name"] == pipeline)
            and (status is None or status == "queued")
            and (from_dt is None or run["requested_at"] >= from_dt)
        ]
        if queued_rows:
            rows = [*rows, *queued_rows]
            rows.sort(key=lambda r: r["started_at"] or "", reverse=True)

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


@router.get("/runs/count")
async def count_runs(
    request: Request,
    pipeline: str | None = Query(None, description="Filter by pipeline name"),
    status: str | None = Query(None, description="Filter by status (success/failed/aborted/queued)"),
    from_dt: datetime | None = Query(None, description="Count runs started at or after this ISO timestamp"),
):
    """Total run count for the current list filters (honest pagination UI).

    Mirrors the /api/runs WHERE clause, plus the queued-run merge so the
    count matches what the listing shows. Returns ``{"total": null}`` when
    no persistence is configured — the UI falls back to its has-more
    heuristic there. Registered before /runs/{run_id} so "count" is not
    captured as a run id.
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"total": None}

    sql = "SELECT COUNT(*) FROM run_history WHERE 1=1"
    params: dict = {}
    if pipeline:
        sql += " AND pipeline_name = :pipeline_name"
        params["pipeline_name"] = pipeline
    if status:
        sql += " AND status = :status"
        params["status"] = status
    if from_dt:
        sql += " AND started_at >= :from_dt"
        params["from_dt"] = from_dt.isoformat()
    # TramDB exposes no count API; the engine is the only handle (L4: minimal,
    # router-confined addition).
    with db._engine.connect() as conn:  # noqa: SLF001
        total = conn.execute(text(sql), params).scalar_one()

    # Queued runs are merged into the listing — count them the same way.
    queued = [
        run for run in db.get_queued_run_view()
        if (pipeline is None or run["pipeline_name"] == pipeline)
        and (status is None or status == "queued")
        and (from_dt is None or run["requested_at"] >= from_dt)
    ]
    return {"total": total + len(queued)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict:
    """Get a single run result by run_id.

    E.2 (§8.1): falls back to the queued-run view when run history misses, so a
    queued (or dispatching) run is retrievable before it ever lands in history.
    """
    controller = request.app.state.controller
    result = controller.get_run(run_id)
    if result is not None:
        return result.to_dict()
    db = getattr(request.app.state, "db", None)
    if db is not None:
        row = next(
            (r for r in db.get_queued_run_view() if r["run_id"] == run_id),
            None,
        )
        if row is not None:
            return _queued_run_to_dict(row)
    raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")


@router.get("/daemon/status")
async def daemon_status(request: Request) -> dict:
    """Scheduler state, active streams, next scheduled runs."""
    scheduler = request.app.state.scheduler
    return scheduler.get_scheduler_status()


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
