"""Health, readiness, meta, and plugins endpoints."""

from __future__ import annotations

import inspect
import sys
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from tram import __version__
from tram.api.config_schema import SCHEMA_FIELDS
from tram.api.routers._stream_views import build_cluster_streams
from tram.registry.registry import _serializers, _sinks, _sources, _transforms, list_plugins

router = APIRouter()


def _current_worker_assignments(controller) -> dict[str, list[str]]:
    assignments: dict[str, set[str]] = {}

    for placement in controller.get_active_broadcast_placements():
        pipeline_name = str(placement.get("pipeline_name", "") or "")
        if not pipeline_name:
            continue
        for slot in placement.get("slots", []):
            worker_url = str(slot.get("worker_url", "") or "")
            if not worker_url:
                continue
            assignments.setdefault(worker_url, set()).add(pipeline_name)

    for run in controller.get_active_batch_runs():
        pipeline_name = str(run.get("pipeline_name", "") or "")
        worker_url = str(run.get("worker_url", "") or "")
        if not pipeline_name or not worker_url:
            continue
        assignments.setdefault(worker_url, set()).add(pipeline_name)

    return {
        worker_url: sorted(pipelines)
        for worker_url, pipelines in assignments.items()
    }


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
    """All registered plugins by category, plus UI-friendly metadata."""
    import tram.connectors  # noqa: F401
    import tram.serializers  # noqa: F401
    import tram.transforms  # noqa: F401

    payload = list_plugins()
    payload["details"] = {
        "sources": _build_plugin_details("source", _sources),
        "sinks": _build_plugin_details("sink", _sinks),
        "serializers": _build_plugin_details("serializer", _serializers),
        "transforms": _build_plugin_details("transform", _transforms),
    }
    return payload


def _doc_parts(plugin_cls: type) -> tuple[str, str]:
    doc = inspect.getdoc(plugin_cls) or ""
    if not doc:
        return "", ""
    lines = [line.strip() for line in doc.splitlines()]
    summary = lines[0]
    description = " ".join(line for line in lines[1:] if line).strip()
    return summary, description


def _common_field_names(fields: list[dict], *, limit: int = 6) -> list[str]:
    names = []
    for field in fields:
        if field["name"] in {"condition", "serializer_out", "transforms"}:
            continue
        names.append(field["name"])
    return names[:limit]


def _field_descriptors(fields: list[dict]) -> list[dict]:
    return [
        {
            "name": field["name"],
            "type": field["type"],
            "required": bool(field.get("required")),
            "default": field.get("default"),
        }
        for field in fields
        if field["name"] not in {"condition", "serializer_out", "transforms"}
    ]


def _build_plugin_details(category: str, registry: dict[str, type]) -> list[dict]:
    items = []
    for name in sorted(registry.keys()):
        plugin_cls = registry[name]
        summary, description = _doc_parts(plugin_cls)
        fields = SCHEMA_FIELDS.get(category, {}).get(name, [])
        required = [field["name"] for field in fields if field.get("required")]
        optional = [field for field in fields if not field.get("required")]
        items.append(
            {
                "name": name,
                "class_name": plugin_cls.__name__,
                "summary": summary or name,
                "description": description,
                "required_fields": required,
                "common_optional_fields": _common_field_names(optional),
                "fields": _field_descriptors(fields),
                "field_count": len(fields),
            }
        )
    return items


@router.get("/api/cluster/nodes")
async def cluster_nodes(request: Request) -> dict:
    """Worker pool status (manager mode) or standalone indicator.

    Returns ``{"mode": "standalone"}`` when no worker pool is configured.
    In manager mode returns health and active-run counts for each worker.
    """
    worker_pool = getattr(request.app.state, "worker_pool", None)
    config = getattr(request.app.state, "config", None)
    controller = request.app.state.controller
    mode = getattr(config, "tram_mode", "standalone") if config else "standalone"

    if worker_pool is None:
        return {"mode": mode, "workers": []}

    workers = worker_pool.status()
    current_assignments = _current_worker_assignments(controller)
    for worker in workers:
        worker["assigned_pipelines"] = current_assignments.get(worker.get("url"), [])

    return {
        "mode": mode,
        "workers": workers,
    }


@router.get("/api/cluster/streams")
async def cluster_streams(request: Request) -> dict:
    """Active stream placement and throughput view."""
    db = getattr(request.app.state, "db", None)
    controller = request.app.state.controller
    stats_store = getattr(request.app.state, "stats_store", None)
    config = getattr(request.app.state, "config", None)
    mode = getattr(config, "tram_mode", "standalone") if config else "standalone"
    worker_pool = getattr(request.app.state, "worker_pool", None)

    if db is not None:
        placements = db.get_active_broadcast_placements()
    else:
        placements = controller.get_active_broadcast_placements()

    return {
        "mode": mode,
        "streams": build_cluster_streams(
            placements,
            stats_store,
            worker_pool.live_streams() if worker_pool is not None else None,
        ),
    }
