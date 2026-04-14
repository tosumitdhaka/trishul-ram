"""Health, readiness, meta, and plugins endpoints."""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from tram import __version__
from tram.registry.registry import list_plugins

router = APIRouter()


@router.get("/api/health")
async def liveness() -> dict:
    """Liveness probe — returns 200 if daemon process is running."""
    return {"status": "ok"}


@router.get("/api/ready")
async def readiness(request: Request) -> dict:
    """Readiness probe — returns 200 when daemon is fully initialized and DB is reachable."""
    from fastapi import HTTPException

    manager = request.app.state.manager
    scheduler = request.app.state.scheduler
    db = getattr(request.app.state, "db", None)
    started_at = getattr(request.app.state, "started_at", None)

    # DB check
    db_status = "ok"
    if db is not None and not db.health_check():
        db_status = "unreachable"
        raise HTTPException(status_code=503, detail="Database unreachable")

    # Scheduler check
    scheduler_status = "running" if getattr(scheduler, "_running", False) else "stopped"
    if scheduler_status == "stopped":
        raise HTTPException(status_code=503, detail="Scheduler stopped")

    # DB engine info
    db_engine = "sqlite"
    db_path = None
    if db is not None:
        dialect = db._engine.dialect.name
        db_engine = dialect
        if dialect == "sqlite":
            url_str = str(db._engine.url)
            db_path = url_str.replace("sqlite:///", "").replace("sqlite://", "") or None

    # Uptime
    uptime = None
    if started_at is not None:
        delta = int((datetime.now(UTC) - started_at).total_seconds())
        h, rem = divmod(delta, 3600)
        m, s = divmod(rem, 60)
        uptime = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

    # Cluster / mode
    worker_pool = getattr(request.app.state, "worker_pool", None)
    config = getattr(request.app.state, "config", None)
    mode = getattr(config, "tram_mode", "standalone") if config else "standalone"
    if worker_pool is not None:
        healthy = len(worker_pool.healthy_workers())
        total = len(worker_pool._workers)
        cluster = f"manager · {healthy}/{total} workers"
    else:
        cluster = mode

    return {
        "status": "ready",
        "db": db_status,
        "db_engine": db_engine,
        "db_path": db_path,
        "scheduler": scheduler_status,
        "pipelines_loaded": len(manager.list_all()),
        "uptime": uptime,
        "cluster": cluster,
    }


@router.get("/api/meta")
async def meta() -> dict:
    """Build and version information."""
    return {
        "version": __version__,
        "build_time": datetime.now(UTC).isoformat(),
        "python_version": sys.version.split()[0],
    }


@router.get("/api/plugins")
async def plugins() -> dict:
    """All registered plugin keys by category."""
    return list_plugins()


@router.get("/api/cluster/nodes")
async def cluster_nodes(request: Request) -> dict:
    """Worker pool status (manager mode) or standalone indicator.

    Returns ``{"mode": "standalone"}`` when no worker pool is configured.
    In manager mode returns health and active-run counts for each worker.
    """
    worker_pool = getattr(request.app.state, "worker_pool", None)
    config = getattr(request.app.state, "config", None)
    mode = getattr(config, "tram_mode", "standalone") if config else "standalone"

    if worker_pool is None:
        return {"mode": mode, "workers": []}

    return {
        "mode": mode,
        "workers": worker_pool.status(),
    }
