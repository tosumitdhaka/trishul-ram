"""WorkerAgent — FastAPI app exposing the internal agent API on :8766.

Worker responsibilities:
  POST /agent/run     — receive pipeline config + run_id, execute, report back
  POST /agent/stop    — stop a running batch/stream job
  GET  /agent/status  — return active jobs {running: [...], streams: [...]}
  GET  /agent/health  — liveness/readiness {ok: true, worker_id: ...}

On completion the worker POSTs to the manager's run-complete callback URL.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ── Request / response models ──────────────────────────────────────────────


class RunRequest(BaseModel):
    pipeline_name: str
    yaml_text: str
    run_id: str
    schedule_type: str = "batch"   # "batch" | "stream"
    callback_url: str = ""         # manager endpoint for run-complete; may be empty


class StopRequest(BaseModel):
    pipeline_name: str
    run_id: str


# ── In-memory run tracking ─────────────────────────────────────────────────


@dataclass
class ActiveRun:
    run_id: str
    pipeline_name: str
    schedule_type: str
    started_at: str
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = field(default=None, compare=False)


class WorkerState:
    """Thread-safe store of currently-active pipeline runs."""

    def __init__(self, worker_id: str, manager_url: str) -> None:
        self.worker_id = worker_id
        self.manager_url = manager_url
        self._runs: dict[str, ActiveRun] = {}
        self._lock = threading.Lock()

    def add(self, run: ActiveRun) -> None:
        with self._lock:
            self._runs[run.run_id] = run

    def remove(self, run_id: str) -> None:
        with self._lock:
            self._runs.pop(run_id, None)

    def get(self, run_id: str) -> ActiveRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def snapshot(self) -> list[ActiveRun]:
        with self._lock:
            return list(self._runs.values())


# ── Manager callback ───────────────────────────────────────────────────────


def _post_run_complete(
    callback_url: str,
    run_id: str,
    pipeline_name: str,
    status: str,
    records_in: int,
    records_out: int,
    error: str | None,
    records_skipped: int = 0,
    errors: list[str] | None = None,
) -> None:
    """POST run-complete to the manager. Errors are logged and swallowed."""
    if not callback_url:
        return
    payload = {
        "run_id": run_id,
        "pipeline_name": pipeline_name,
        "status": status,
        "records_in": records_in,
        "records_out": records_out,
        "records_skipped": records_skipped,
        "error": error,
        "errors": errors or [],
    }
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(callback_url, json=payload)
            resp.raise_for_status()
        logger.debug(
            "run-complete callback sent",
            extra={"run_id": run_id, "status": status},
        )
    except Exception as exc:
        logger.warning(
            "run-complete callback failed",
            extra={"callback_url": callback_url, "run_id": run_id, "error": str(exc)},
        )


# ── App factory ────────────────────────────────────────────────────────────


def create_worker_app(worker_id: str = "", manager_url: str = "") -> FastAPI:
    """Create and return the worker agent FastAPI application."""
    if not worker_id:
        worker_id = os.environ.get("TRAM_WORKER_ID", socket.gethostname())
    if not manager_url:
        manager_url = os.environ.get("TRAM_MANAGER_URL", "")

    state = WorkerState(worker_id=worker_id, manager_url=manager_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Trigger plugin registration (connectors/transforms/serializers)
        import tram.connectors  # noqa: F401
        import tram.serializers  # noqa: F401
        import tram.transforms  # noqa: F401

        logger.info("Worker agent ready", extra={"worker_id": worker_id})
        yield

        # Signal all active streams to stop on shutdown
        for run in state.snapshot():
            run.stop_event.set()
        logger.info("Worker agent stopped", extra={"worker_id": worker_id})

    app = FastAPI(
        title="TRAM Worker Agent",
        description="Internal agent API for pipeline execution workers",
        lifespan=lifespan,
    )
    app.state.worker = state

    # ── GET /agent/health ──────────────────────────────────────────────────

    @app.get("/agent/health")
    def health():
        active = state.snapshot()
        return {
            "ok": True,
            "worker_id": worker_id,
            "active_runs": len(active),
            "running_pipelines": list({r.pipeline_name for r in active}),
        }

    # ── GET /agent/status ──────────────────────────────────────────────────

    @app.get("/agent/status")
    def status():
        active = state.snapshot()
        running = [
            {"run_id": r.run_id, "pipeline": r.pipeline_name, "started_at": r.started_at}
            for r in active
            if r.schedule_type != "stream"
        ]
        streams = [
            {"run_id": r.run_id, "pipeline": r.pipeline_name, "started_at": r.started_at}
            for r in active
            if r.schedule_type == "stream"
        ]
        return {"running": running, "streams": streams}

    # ── POST /agent/run ────────────────────────────────────────────────────

    @app.post("/agent/run", status_code=202)
    def run(req: RunRequest):  # noqa: A001
        if state.get(req.run_id) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"run_id {req.run_id!r} is already active on this worker",
            )

        from tram.pipeline.executor import PipelineExecutor
        from tram.pipeline.loader import load_pipeline_from_yaml

        try:
            config = load_pipeline_from_yaml(req.yaml_text)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Resolve callback URL: explicit > derived from manager_url
        callback_url = req.callback_url
        if not callback_url and state.manager_url:
            callback_url = f"{state.manager_url}/api/internal/run-complete"

        active_run = ActiveRun(
            run_id=req.run_id,
            pipeline_name=req.pipeline_name,
            schedule_type=req.schedule_type,
            started_at=datetime.now(UTC).isoformat(),
        )
        executor = PipelineExecutor()

        data_dir = os.environ.get("TRAM_DATA_DIR", "/data")
        api_key  = os.environ.get("TRAM_API_KEY", "")

        if req.schedule_type == "stream":
            def _stream_thread():
                try:
                    from tram.agent.assets import sync_assets
                    sync_assets(config, state.manager_url, data_dir, api_key)
                    executor.stream_run(config, active_run.stop_event)
                    _post_run_complete(
                        callback_url, req.run_id, req.pipeline_name,
                        "success", 0, 0, None,
                    )
                except Exception as exc:
                    logger.error(
                        "Stream run error",
                        extra={
                            "pipeline": req.pipeline_name,
                            "run_id": req.run_id,
                            "error": str(exc),
                        },
                    )
                    _post_run_complete(
                        callback_url, req.run_id, req.pipeline_name,
                        "error", 0, 0, str(exc),
                    )
                finally:
                    state.remove(req.run_id)

            t = threading.Thread(
                target=_stream_thread,
                daemon=True,
                name=f"tram-agent-stream-{req.run_id}",
            )
        else:
            def _batch_thread():
                try:
                    from tram.agent.assets import sync_assets
                    sync_assets(config, state.manager_url, data_dir, api_key)
                    result = executor.batch_run(config, run_id=req.run_id)
                    _post_run_complete(
                        callback_url, req.run_id, req.pipeline_name,
                        result.status.value,
                        result.records_in,
                        result.records_out,
                        result.error,
                        result.records_skipped,
                        result.errors,
                    )
                except Exception as exc:
                    logger.error(
                        "Batch run error",
                        extra={
                            "pipeline": req.pipeline_name,
                            "run_id": req.run_id,
                            "error": str(exc),
                        },
                    )
                    _post_run_complete(
                        callback_url, req.run_id, req.pipeline_name,
                        "error", 0, 0, str(exc),
                    )
                finally:
                    state.remove(req.run_id)

            t = threading.Thread(
                target=_batch_thread,
                daemon=True,
                name=f"tram-agent-batch-{req.run_id}",
            )

        active_run.thread = t
        state.add(active_run)
        t.start()

        return {"accepted": True, "run_id": req.run_id, "worker_id": worker_id}

    # ── POST /agent/stop ───────────────────────────────────────────────────

    @app.post("/agent/stop")
    def stop(req: StopRequest):  # noqa: A001
        active_run = state.get(req.run_id)
        if active_run is None:
            raise HTTPException(
                status_code=404,
                detail=f"run_id {req.run_id!r} not found on this worker",
            )
        active_run.stop_event.set()
        return {"stopping": True, "run_id": req.run_id, "worker_id": worker_id}

    return app
