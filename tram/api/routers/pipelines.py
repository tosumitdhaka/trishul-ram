"""Pipeline CRUD + lifecycle endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tram.api.routers._errors import internal_error_detail
from tram.api.routers._stream_views import build_placement_view
from tram.core.exceptions import (
    ConfigError,
    PipelineAlreadyExistsError,
    PipelineNotFoundError,
)
from tram.pipeline.linter import lint
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
    warnings = [finding.message for finding in lint(config) if finding.severity == "warning"]
    if warnings:
        result["warnings"] = warnings
    return result


# ── List / Register ────────────────────────────────────────────────────────


@router.get("")
async def list_pipelines(request: Request) -> list[dict]:
    """List all registered pipelines with their current status."""
    controller = request.app.state.controller
    states = controller.list_all()
    rows = [state.to_dict() for state in states]
    # E.2 (§8.1): each pipeline gains a `queued_run` field when a non-terminal
    # queued run exists. One join against get_queued_run_view — never a
    # per-pipeline DB hit.
    db = getattr(request.app.state, "db", None)
    if db is not None:
        queued_by_pipeline: dict[str, dict] = {}
        for run in db.get_queued_run_view():
            queued_by_pipeline.setdefault(run["pipeline_name"], run)
        for row in rows:
            queued = queued_by_pipeline.get(row["name"])
            row["queued_run"] = (
                {
                    "run_id": queued["run_id"],
                    "requested_at": queued["requested_at"].isoformat(),
                    "expires_at": queued["expires_at"].isoformat(),
                }
                if queued is not None
                else None
            )
    else:
        for row in rows:
            row["queued_run"] = None
    return rows


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
    controller = request.app.state.controller
    try:
        state = controller.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    row = state.to_detail_dict()
    # E.2 (§8.1): detail view carries the queued run (if any).
    db = getattr(request.app.state, "db", None)
    if db is not None:
        queued = db.get_active_queued_run_for_pipeline(name)
        row["queued_run"] = (
            {
                "run_id": queued["run_id"],
                "requested_at": queued["requested_at"].isoformat(),
                "expires_at": queued["expires_at"].isoformat(),
            }
            if queued is not None
            else None
        )
    else:
        row["queued_run"] = None
    return row


@router.get("/{name}/placement")
async def get_pipeline_placement(name: str, request: Request) -> dict:
    controller = request.app.state.controller
    stats_store = getattr(request.app.state, "stats_store", None)
    worker_pool = getattr(controller, "_worker_pool", None)

    try:
        state = controller.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    placement = next(
        (item for item in controller.get_active_broadcast_placements() if item["pipeline_name"] == name),
        None,
    )
    if placement is not None:
        live_items = None
        if worker_pool is not None:
            # Blocking per-worker /agent/status fan-out — off the event loop
            # (plan D.6).
            all_live = await run_in_threadpool(worker_pool.live_streams)
            live_items = [
                item
                for item in all_live
                if item.get("pipeline_name") == name
            ]
        return build_placement_view(placement, stats_store, live_items)

    # Stream pipeline without a durable placement row: single-slot synthetic
    # view from the live stats entry whenever one exists. Placement rows are
    # the source of truth after D.2 (count=1 streams included), so this path
    # mainly serves standalone mode (no worker pool ⇒ no placements) and the
    # feature-flag-off / pre-D.2 count=1 stream still running on a worker.
    # The `worker_pool is None` gate is gone — a manager-mode stream without a
    # placement row renders instead of 404ing (RCA #17, plan D.3).
    if stats_store is not None and state.config.schedule.type == "stream":
        entries = stats_store.for_pipeline(name)
        if entries:
            from tram.api.routers._stream_views import _stats_view_from_entry
            entry = entries[0]
            stats_view = _stats_view_from_entry(entry, stale=stats_store.is_stale(entry))
            slot = {
                "worker_index": 0,
                "worker_id": getattr(entry, "worker_id", None),
                "worker_url": None,
                "run_id_prefix": getattr(entry, "run_id", None),
                "current_run_id": getattr(entry, "run_id", None),
                "status": "running",
                "restart_count": 0,
                "stats": stats_view,
            }
            return {
                "pipeline_name": name,
                "placement_group_id": None,
                "status": "running",
                "target_count": 1,
                "started_at": None,
                "slot_count": 1,
                "active_slots": 1,
                "records_in": stats_view["records_in"],
                "records_out": stats_view["records_out"],
                "records_skipped": stats_view["records_skipped"],
                "dlq_count": stats_view["dlq_count"],
                "error_count": stats_view["error_count"],
                "bytes_in": stats_view["bytes_in"],
                "bytes_out": stats_view["bytes_out"],
                "records_in_per_sec": stats_view["records_in_per_sec"],
                "records_out_per_sec": stats_view["records_out_per_sec"],
                "bytes_in_per_sec": stats_view["bytes_in_per_sec"],
                "bytes_out_per_sec": stats_view["bytes_out_per_sec"],
                "slots": [slot],
            }

    raise HTTPException(status_code=404, detail=f"Pipeline '{name}' has no active broadcast placement")


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
        raise HTTPException(
            status_code=500,
            detail=internal_error_detail(logger, exc, message="Failed to update pipeline"),
        )

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
        raise HTTPException(
            status_code=500,
            detail=internal_error_detail(logger, exc, message="Failed to delete pipeline"),
        )

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
        start_status = controller.start_pipeline(name)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=internal_error_detail(logger, exc, message="Failed to start pipeline"),
        )

    state = controller.get(name)
    if start_status == "disabled":
        detail = (
            f"Pipeline '{name}' is disabled in YAML."
            + (
                " It can only be triggered manually until enabled."
                if state.config.schedule.type != "stream"
                else " Enable it in config before starting."
            )
        )
    elif start_status == "manual":
        detail = f"Pipeline '{name}' uses a manual schedule. Use Run Now instead."
    elif start_status == "already_running":
        detail = f"Pipeline '{name}' is already active."
    else:
        detail = f"Pipeline '{name}' started."

    return {"name": name, "status": start_status, "detail": detail}


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
        raise HTTPException(
            status_code=500,
            detail=internal_error_detail(logger, exc, message="Failed to stop pipeline"),
        )

    return {"name": name, "status": "stopped"}


@router.post("/{name}/restart")
async def restart_pipeline(name: str, request: Request) -> dict:
    """Stop a pipeline's active execution and immediately reschedule it.

    Useful after schema/MIB changes or when a stream needs a fresh start.
    Works for both batch (interval/cron) and stream pipelines.
    """
    controller = request.app.state.controller

    try:
        controller.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        controller.restart_pipeline(name)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=internal_error_detail(logger, exc, message="Failed to restart pipeline"),
        )

    return {"name": name, "status": "restarting"}


@router.post("/{name}/run")
async def trigger_run(
    name: str,
    request: Request,
    flush: bool = Query(False, description="Flush run (F.1 §5): stateful transforms emit open windows as partials and clear them from state"),
) -> dict:
    controller = request.app.state.controller

    try:
        controller.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        result = controller.trigger_run(name, flush=flush)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=internal_error_detail(logger, exc, message="Failed to trigger run"),
        )

    if isinstance(result, str):
        # Legacy/mocked path: a plain run_id means the run was submitted.
        return {"name": name, "status": "triggered", "run_id": result}

    if result.disposition == "queued":
        # E.2 (§8.1): 202 — the run is durably queued (or a dedupe-hit returning
        # the existing run_id). expires_at is the absolute TTL deadline.
        expires_at = None
        db = getattr(request.app.state, "db", None)
        if db is not None:
            row = db.get_active_queued_run_for_pipeline(name)
            if row is not None:
                expires_at = row["expires_at"].isoformat()
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "name": name,
                "status": "queued",
                "run_id": result.run_id,
                "expires_at": expires_at,
            },
        )

    return {"name": name, "status": "triggered", "run_id": result.run_id}


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
    controller._boot_load()

    total = len(controller.list_all())
    return {"reloaded": seeded, "total": total}


# ── Version history + rollback ─────────────────────────────────────────────


@router.get("/{name}/versions")
async def list_versions(name: str, request: Request) -> list[dict]:
    """List saved versions for a pipeline."""
    controller = request.app.state.controller
    try:
        controller.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    versions = controller.get_versions(name)
    return versions


@router.get("/{name}/versions/{version}")
async def get_version_yaml(name: str, version: int, request: Request):
    """Return raw YAML for a specific pipeline version."""
    from fastapi.responses import PlainTextResponse
    controller = request.app.state.controller
    try:
        controller.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        yaml_text = controller.get_version_yaml(name, version)
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return PlainTextResponse(yaml_text, media_type="text/plain")


# ── Alert rules ────────────────────────────────────────────────────────────


def _read_alerts_data(controller, name):
    """Return (yaml_dict, state) or raise HTTPException."""
    import yaml as _yaml
    try:
        state = controller.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    yaml_text = getattr(state, "yaml_text", None) or ""
    if not yaml_text:
        raise HTTPException(status_code=503, detail="Pipeline YAML not stored")
    return _yaml.safe_load(yaml_text) or {}, state


def _save_alerts_data(controller, name, data):
    """Persist alert rules by routing through ``controller.update()``.

    ``update()`` writes the new YAML into the pipeline registry via
    ``db.save_pipeline`` (which boot-load reads back on restart) and performs
    the proper stop/restart of the live pipeline when it was active. The old
    path re-implemented deregister/register, which only saved a *version* —
    alert edits vanished on controller restart. Alerts live inside the
    pipeline YAML, so a real edit always produces a different document and
    never hits ``update()``'s identical-YAML short-circuit.
    """
    import yaml as _yaml
    new_yaml = _yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    try:
        load_pipeline_from_yaml(new_yaml)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return controller.update(name, new_yaml)


@router.get("/{name}/alerts")
async def list_alerts(name: str, request: Request) -> list[dict]:
    """List alert rules for a pipeline."""
    data, _ = _read_alerts_data(request.app.state.controller, name)
    alerts = data.get("alerts") or []
    return [{"index": i, **a} for i, a in enumerate(alerts)]


@router.post("/{name}/alerts", status_code=status.HTTP_201_CREATED)
async def create_alert(name: str, request: Request) -> dict:
    """Append a new alert rule to a pipeline."""
    body = await request.json()
    if not body.get("condition") or not body.get("action"):
        raise HTTPException(status_code=400, detail="condition and action are required")
    data, _ = _read_alerts_data(request.app.state.controller, name)
    alerts = list(data.get("alerts") or [])
    rule = {k: v for k, v in body.items() if v is not None}
    alerts.append(rule)
    data["alerts"] = alerts
    _save_alerts_data(request.app.state.controller, name, data)
    return {"index": len(alerts) - 1, **rule}


@router.put("/{name}/alerts/{idx}")
async def update_alert(name: str, idx: int, request: Request) -> dict:
    """Replace an alert rule by index."""
    body = await request.json()
    if not body.get("condition") or not body.get("action"):
        raise HTTPException(status_code=400, detail="condition and action are required")
    data, _ = _read_alerts_data(request.app.state.controller, name)
    alerts = list(data.get("alerts") or [])
    if idx < 0 or idx >= len(alerts):
        raise HTTPException(status_code=404, detail=f"Alert index {idx} not found")
    rule = {k: v for k, v in body.items() if v is not None}
    alerts[idx] = rule
    data["alerts"] = alerts
    _save_alerts_data(request.app.state.controller, name, data)
    return {"index": idx, **rule}


@router.delete("/{name}/alerts/{idx}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert(name: str, idx: int, request: Request) -> Response:
    """Remove an alert rule by index."""
    data, _ = _read_alerts_data(request.app.state.controller, name)
    alerts = list(data.get("alerts") or [])
    if idx < 0 or idx >= len(alerts):
        raise HTTPException(status_code=404, detail=f"Alert index {idx} not found")
    alerts.pop(idx)
    data["alerts"] = alerts
    _save_alerts_data(request.app.state.controller, name, data)
    return Response(status_code=204)


@router.post("/{name}/rollback")
async def rollback_pipeline(
    name: str,
    request: Request,
    version: int = Query(..., description="Version number to restore"),
) -> dict:
    """Restore a pipeline to a previously saved version."""
    controller = request.app.state.controller

    try:
        state = controller.get(name)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    was_active = state.status in ("running", "scheduled")

    if was_active:
        try:
            controller.stop_pipeline(name)
        except Exception as exc:
            logger.warning("Error stopping pipeline before rollback: %s", exc)

    try:
        new_config = controller.rollback(name, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    new_state = controller.get(name)

    if was_active and new_config.enabled and new_config.schedule.type != "manual":
        try:
            controller.start_pipeline(name)
        except Exception as exc:
            logger.warning("Could not restart pipeline after rollback: %s", exc)

    return {**new_state.to_dict(), "rolled_back_to_version": version}
