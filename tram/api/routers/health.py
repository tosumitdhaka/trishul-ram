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
    db = getattr(request.app.state, "db", None)

    if db is not None and not db.health_check():
        raise HTTPException(status_code=503, detail="Database unreachable")

    pipelines = manager.list_all()
    return {
        "status": "ready",
        "pipelines_loaded": len(pipelines),
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
    state = coordinator.get_state()
    state["cluster_enabled"] = True
    return state
