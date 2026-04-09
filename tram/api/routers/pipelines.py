"""Pipeline CRUD + lifecycle endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from tram.core.exceptions import (
    ConfigError,
    PipelineAlreadyExistsError,
    PipelineNotFoundError,
)
from tram.pipeline.loader import load_pipeline_from_yaml, scan_pipeline_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pipelines")


class RegisterRequest(BaseModel):
    yaml_text: str | None = None


# ── Dry run ────────────────────────────────────────────────────────────────


@router.post("/dry-run")
async def dry_run_pipeline(request: Request) -> dict:
    """Validate pipeline YAML wiring without performing any I/O."""
    content_type = request.headers.get("content-type", "")
    if "yaml" in content_type or "text" in content_type or "plain" in content_type:
        yaml_text = (await request.body()).decode("utf-8")
    else:
        body = await request.json()
        yaml_text = body.get("yaml_text", "")

    if not yaml_text:
        raise HTTPException(status_code=400, detail="Request body must contain YAML text")

    try:
        config = load_pipeline_from_yaml(yaml_text)
    except ConfigError as exc:
        return {"valid": False, "issues": [str(exc)]}

    from tram.pipeline.executor import PipelineExecutor
    result = PipelineExecutor().dry_run(config)
    return result


# ── List / Register ────────────────────────────────────────────────────────


@router.get("")
async def list_pipelines(request: Request) -> list[dict]:
    """List all registered pipelines with their current status.

    DB runtime_status is overlaid on each pipeline so all pods return a
    consistent view regardless of which pod the load balancer routes to.
    """
    manager = request.app.state.manager
    db = getattr(request.app.state, "db", None)
    states = manager.list_all()
    if db is not None:
        runtime = {r["name"]: r["runtime_status"] for r in db.get_all_pipeline_runtime()}
        result = []
        for state in states:
            d = state.to_dict()
            if state.config.name in runtime:
                d["status"] = runtime[state.config.name]
            result.append(d)
        return result
    return [state.to_dict() for state in states]


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_pipeline(request: Request) -> dict:
    """Register a new pipeline from YAML text in request body."""
    controller = request.app.state.controller

    content_type = request.headers.get("content-type", "")
    if "yaml" in content_type or "text" in content_type or "plain" in content_type:
        yaml_text = (await request.body()).decode("utf-8")
    else:
        body = await request.json()
        yaml_text = body.get("yaml_text", "")

    if not yaml_text:
        raise HTTPException(status_code=400, detail="Request body must contain YAML text")

    try:
        config = load_pipeline_from_yaml(yaml_text)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        state = controller.register(config, yaml_text=yaml_text, source="api")
    except PipelineAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return state.to_dict()


# ── Single pipeline ────────────────────────────────────────────────────────


@router.get("/{name}")
async def get_pipeline(name: str, request: Request) -> dict:
    manager = request.app.state.manager
    db = getattr(request.app.state, "db", None)
    try:
        state = manager.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    d = state.to_detail_dict()
    if db is not None:
        rt = db.get_pipeline_runtime(name)
        if rt and rt.get("runtime_status"):
            d["status"] = rt["runtime_status"]
    return d


@router.put("/{name}")
async def update_pipeline(name: str, request: Request) -> dict:
    """Update an existing pipeline YAML. Restarts if it was running/scheduled."""
    controller = request.app.state.controller

    try:
        controller.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    content_type = request.headers.get("content-type", "")
    if "yaml" in content_type or "text" in content_type or "plain" in content_type:
        yaml_text = (await request.body()).decode("utf-8")
    else:
        body = await request.json()
        yaml_text = body.get("yaml_text", "")

    if not yaml_text:
        raise HTTPException(status_code=400, detail="Request body must contain YAML text")

    try:
        config = load_pipeline_from_yaml(yaml_text)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if config.name != name:
        raise HTTPException(
            status_code=400,
            detail=f"Pipeline name in YAML '{config.name}' does not match URL '{name}'",
        )

    try:
        new_state = controller.update(name, yaml_text)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return new_state.to_dict()


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pipeline(name: str, request: Request) -> Response:
    controller = request.app.state.controller

    try:
        controller.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        controller.delete(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return Response(status_code=204)


# ── Lifecycle ──────────────────────────────────────────────────────────────


@router.post("/{name}/pause")
async def pause_pipeline(name: str, request: Request) -> dict:
    """Deprecated alias for /stop. Use POST /{name}/stop instead."""
    return await stop_pipeline(name, request)


@router.post("/{name}/resume")
async def resume_pipeline(name: str, request: Request) -> dict:
    """Deprecated alias for /start. Use POST /{name}/start instead."""
    return await start_pipeline(name, request)


@router.post("/{name}/start")
async def start_pipeline(name: str, request: Request) -> dict:
    controller = request.app.state.controller

    try:
        controller.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        controller.start_pipeline(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"name": name, "status": "started"}


@router.post("/{name}/stop")
async def stop_pipeline(name: str, request: Request) -> dict:
    controller = request.app.state.controller

    try:
        controller.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        controller.stop_pipeline(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"name": name, "status": "stopped"}


@router.post("/{name}/run")
async def trigger_run(name: str, request: Request) -> dict:
    controller = request.app.state.controller

    try:
        controller.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        run_id = controller.trigger_run(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"name": name, "status": "triggered", "run_id": run_id}


# ── Reload ─────────────────────────────────────────────────────────────────


@router.post("/reload")
async def reload_pipelines(request: Request) -> dict:
    """Re-scan pipeline_dir, seed to DB, then trigger sync loop immediately."""
    controller = request.app.state.controller
    pipeline_dir = request.app.state.config.pipeline_dir
    db = getattr(request.app.state, "db", None)

    seeded = 0
    for config, yaml_text in scan_pipeline_dir(pipeline_dir):
        if db is not None:
            existing_source = db.get_pipeline_source(config.name)
            if existing_source == "api":
                logger.debug("Reload: skipping disk seed for user-owned pipeline %s", config.name)
                continue
            db.save_pipeline(config.name, yaml_text, source="disk")
            seeded += 1

    # Trigger an immediate sync to pick up newly seeded pipelines
    controller._sync_from_db()

    total = len(controller.list_all())
    return {"reloaded": seeded, "total": total}


# ── Version history + rollback ─────────────────────────────────────────────


@router.get("/{name}/versions")
async def list_versions(name: str, request: Request) -> list[dict]:
    """List saved versions for a pipeline."""
    manager = request.app.state.manager
    try:
        manager.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    versions = manager.get_versions(name)
    return versions


@router.get("/{name}/versions/{version}")
async def get_version_yaml(name: str, version: int, request: Request):
    """Return raw YAML for a specific pipeline version."""
    from fastapi.responses import PlainTextResponse
    manager = request.app.state.manager
    try:
        manager.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        yaml_text = manager.get_version_yaml(name, version)
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return PlainTextResponse(yaml_text, media_type="text/plain")


# ── Alert rules ────────────────────────────────────────────────────────────


def _read_alerts_data(manager, name):
    """Return (yaml_dict, state) or raise HTTPException."""
    import yaml as _yaml
    try:
        state = manager.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    yaml_text = getattr(state, "yaml_text", None) or ""
    if not yaml_text:
        raise HTTPException(status_code=503, detail="Pipeline YAML not stored")
    return _yaml.safe_load(yaml_text) or {}, state


def _save_alerts_data(manager, scheduler, name, data, was_running: bool):
    import yaml as _yaml
    new_yaml = _yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    try:
        config = load_pipeline_from_yaml(new_yaml)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if was_running:
        try:
            scheduler.stop_pipeline(name)
        except Exception:
            pass
    manager.deregister(name)
    new_state = manager.register(config, yaml_text=new_yaml)
    if was_running and config.enabled and config.schedule.type != "manual":
        try:
            scheduler.start_pipeline(name)
        except Exception:
            pass
    return new_state


@router.get("/{name}/alerts")
async def list_alerts(name: str, request: Request) -> list[dict]:
    """List alert rules for a pipeline."""
    data, _ = _read_alerts_data(request.app.state.manager, name)
    alerts = data.get("alerts") or []
    return [{"index": i, **a} for i, a in enumerate(alerts)]


@router.post("/{name}/alerts", status_code=status.HTTP_201_CREATED)
async def create_alert(name: str, request: Request) -> dict:
    """Append a new alert rule to a pipeline."""
    body = await request.json()
    if not body.get("condition") or not body.get("action"):
        raise HTTPException(status_code=400, detail="condition and action are required")
    data, state = _read_alerts_data(request.app.state.manager, name)
    alerts = list(data.get("alerts") or [])
    rule = {k: v for k, v in body.items() if v is not None}
    alerts.append(rule)
    data["alerts"] = alerts
    _save_alerts_data(request.app.state.manager, request.app.state.scheduler,
                      name, data, state.status == "running")
    return {"index": len(alerts) - 1, **rule}


@router.put("/{name}/alerts/{idx}")
async def update_alert(name: str, idx: int, request: Request) -> dict:
    """Replace an alert rule by index."""
    body = await request.json()
    if not body.get("condition") or not body.get("action"):
        raise HTTPException(status_code=400, detail="condition and action are required")
    data, state = _read_alerts_data(request.app.state.manager, name)
    alerts = list(data.get("alerts") or [])
    if idx < 0 or idx >= len(alerts):
        raise HTTPException(status_code=404, detail=f"Alert index {idx} not found")
    rule = {k: v for k, v in body.items() if v is not None}
    alerts[idx] = rule
    data["alerts"] = alerts
    _save_alerts_data(request.app.state.manager, request.app.state.scheduler,
                      name, data, state.status == "running")
    return {"index": idx, **rule}


@router.delete("/{name}/alerts/{idx}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert(name: str, idx: int, request: Request) -> Response:
    """Remove an alert rule by index."""
    data, state = _read_alerts_data(request.app.state.manager, name)
    alerts = list(data.get("alerts") or [])
    if idx < 0 or idx >= len(alerts):
        raise HTTPException(status_code=404, detail=f"Alert index {idx} not found")
    alerts.pop(idx)
    data["alerts"] = alerts
    _save_alerts_data(request.app.state.manager, request.app.state.scheduler,
                      name, data, state.status == "running")
    return Response(status_code=204)


@router.post("/{name}/rollback")
async def rollback_pipeline(
    name: str,
    request: Request,
    version: int = Query(..., description="Version number to restore"),
) -> dict:
    """Restore a pipeline to a previously saved version."""
    manager = request.app.state.manager
    scheduler = request.app.state.scheduler

    try:
        state = manager.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # Stop if running
    if state.status == "running":
        try:
            scheduler.stop_pipeline(name)
        except Exception as exc:
            logger.warning("Error stopping pipeline before rollback: %s", exc)

    try:
        new_config = manager.rollback(name, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    new_state = manager.get(name)

    # Restart if was running
    if new_config.enabled and new_config.schedule.type != "manual":
        try:
            scheduler.start_pipeline(name)
        except Exception as exc:
            logger.warning("Could not restart pipeline after rollback: %s", exc)

    return {**new_state.to_dict(), "rolled_back_to_version": version}
