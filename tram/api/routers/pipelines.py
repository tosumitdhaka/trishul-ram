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


# ── List / Register ────────────────────────────────────────────────────────


@router.get("")
async def list_pipelines(request: Request) -> list[dict]:
    """List all registered pipelines with their current status."""
    manager = request.app.state.manager
    return [state.to_dict() for state in manager.list_all()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_pipeline(request: Request) -> dict:
    """Register a new pipeline from YAML text in request body."""
    manager = request.app.state.manager
    scheduler = request.app.state.scheduler

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
        state = manager.register(config, yaml_text=yaml_text)
    except PipelineAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    if config.enabled and config.schedule.type != "manual":
        try:
            scheduler.start_pipeline(config.name)
        except Exception as exc:
            logger.warning("Could not auto-start pipeline %s: %s", config.name, exc)

    return state.to_dict()


# ── Single pipeline ────────────────────────────────────────────────────────


@router.get("/{name}")
async def get_pipeline(name: str, request: Request) -> dict:
    manager = request.app.state.manager
    try:
        state = manager.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return state.to_dict()


@router.put("/{name}")
async def update_pipeline(name: str, request: Request) -> dict:
    """Update an existing pipeline from YAML text in request body.

    Stops the pipeline if running, replaces the configuration, saves a new
    version to history, then restarts if enabled.
    """
    manager = request.app.state.manager
    scheduler = request.app.state.scheduler

    try:
        state = manager.get(name)
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

    # Stop if running
    if state.status == "running":
        try:
            scheduler.stop_pipeline(name)
        except Exception as exc:
            logger.warning("Error stopping pipeline before update: %s", exc)

    # Deregister old config, register new one (preserves run history)
    manager.deregister(name)
    new_state = manager.register(config, yaml_text=yaml_text)

    # Restart if enabled
    if config.enabled and config.schedule.type != "manual":
        try:
            scheduler.start_pipeline(config.name)
        except Exception as exc:
            logger.warning("Could not restart pipeline after update: %s", exc)

    return new_state.to_dict()


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pipeline(name: str, request: Request) -> Response:
    manager = request.app.state.manager
    scheduler = request.app.state.scheduler

    try:
        state = manager.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # Stop first
    if state.status == "running":
        try:
            scheduler.stop_pipeline(name)
        except Exception as exc:
            logger.warning("Error stopping pipeline before delete: %s", exc)

    manager.deregister(name)
    return Response(status_code=204)


# ── Lifecycle ──────────────────────────────────────────────────────────────


@router.post("/{name}/start")
async def start_pipeline(name: str, request: Request) -> dict:
    manager = request.app.state.manager
    scheduler = request.app.state.scheduler

    try:
        manager.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        scheduler.start_pipeline(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"name": name, "status": "started"}


@router.post("/{name}/stop")
async def stop_pipeline(name: str, request: Request) -> dict:
    manager = request.app.state.manager
    scheduler = request.app.state.scheduler

    try:
        manager.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        scheduler.stop_pipeline(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"name": name, "status": "stopped"}


@router.post("/{name}/run")
async def trigger_run(name: str, request: Request) -> dict:
    manager = request.app.state.manager
    scheduler = request.app.state.scheduler

    try:
        state = manager.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if state.config.schedule.type == "stream":
        raise HTTPException(status_code=400, detail="Cannot trigger a stream pipeline manually")

    try:
        scheduler.trigger_run(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"name": name, "status": "triggered"}


# ── Reload ─────────────────────────────────────────────────────────────────


@router.post("/reload")
async def reload_pipelines(request: Request) -> dict:
    """Re-scan pipeline_dir and reload all YAML files."""
    manager = request.app.state.manager
    scheduler = request.app.state.scheduler
    pipeline_dir = request.app.state.config.pipeline_dir

    # Stop all running pipelines
    for state in manager.list_all():
        if state.status == "running":
            try:
                scheduler.stop_pipeline(state.config.name)
            except Exception as exc:
                logger.warning("Error stopping %s during reload: %s", state.config.name, exc)
        manager.deregister(state.config.name)

    configs = scan_pipeline_dir(pipeline_dir)
    loaded = 0
    for config in configs:
        try:
            manager.register(config)
            if config.enabled and config.schedule.type != "manual":
                scheduler.start_pipeline(config.name)
            loaded += 1
        except Exception as exc:
            logger.error("Failed to register pipeline %s: %s", config.name, exc)

    return {"reloaded": loaded, "total": len(configs)}


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
