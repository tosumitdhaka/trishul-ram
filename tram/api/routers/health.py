"""Health, readiness, meta, and plugins endpoints."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

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
    coordinator = getattr(request.app.state, "coordinator", None)
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

    # Cluster status
    cluster_status = "disabled"
    if coordinator is not None:
        cluster_status = "enabled"

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
        delta = int((datetime.now(timezone.utc) - started_at).total_seconds())
        h, rem = divmod(delta, 3600)
        m, s = divmod(rem, 60)
        uptime = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

    return {
        "status": "ready",
        "db": db_status,
        "db_engine": db_engine,
        "db_path": db_path,
        "scheduler": scheduler_status,
        "cluster": cluster_status,
        "pipelines_loaded": len(manager.list_all()),
        "uptime": uptime,
    }


@router.get("/api/meta")
async def meta() -> dict:
    """Build and version information."""
    return {
        "version": __version__,
        "build_time": datetime.now(timezone.utc).isoformat(),
        "python_version": sys.version.split()[0],
    }


@router.get("/api/plugins")
async def plugins() -> dict:
    """All registered plugin keys by category."""
    return list_plugins()


@router.get("/api/cluster/nodes")
async def cluster_nodes(request: Request) -> dict:
    """Cluster node registry — live nodes and ownership state.

    Returns ``{"cluster_enabled": false}`` when running in standalone mode.
    """
    coordinator = getattr(request.app.state, "coordinator", None)
    if coordinator is None:
        return {"cluster_enabled": False}
    manager = request.app.state.manager
    pipeline_names = [s.config.name for s in manager.list_all()]
    state = coordinator.get_state(pipeline_names=pipeline_names)
    state["cluster_enabled"] = True
    return state
