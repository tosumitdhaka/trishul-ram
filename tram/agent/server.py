"""WorkerAgent — FastAPI app exposing the internal agent API on :8766.

Worker responsibilities:
  POST /agent/run     — receive pipeline config + run_id, execute, report back
  POST /agent/stop    — stop a running batch/stream job
  GET  /agent/status  — return active jobs {running: [...], streams: [...]}
  GET  /agent/health  — liveness/readiness {ok: true, worker_id: ...}

On completion the worker POSTs to the manager's run-complete callback URL.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import socket
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from tram.agent.metrics import PipelineStats

if TYPE_CHECKING:
    from tram.models.pipeline import PipelineConfig

logger = logging.getLogger(__name__)

# Review D2 (GH #55): the run-complete callback must not be a single
# fire-and-forget POST — a transient manager outage would otherwise lose the
# completion record and the reconciler would later synthesize a phantom
# FAILED run for a run that succeeded. Bounded retry with short backoff (the
# callback runs on the run thread that is already exiting); exhausted retries
# still swallow the error and let the reconciler adoption path take over.
_RUN_COMPLETE_RETRIES = 3
_RUN_COMPLETE_BACKOFF_BASE_S = 0.5

# Consecutive stats-POST failures per worker_id, surfaced in the WARNING log
# so an operator can tell a one-off blip from a persistent manager outage.
_STATS_MISS_LOCK = threading.Lock()
_CONSECUTIVE_STATS_MISSES: dict[str, int] = {}

# GH #39/#54: the worker agent is a stateless executor with no per-worker DB,
# so a ProcessedFileTracker cannot be constructed here from a local DB. With a
# manager URL the executor gets an HTTP-backed tracker (GH #54) routing
# check/mark to the manager's internal API; without one — or when the manager
# is unreachable at call time (the client fails loud itself) — the fail-loud
# fallback applies: log at executor construction / first tracker failure and
# record the degradation on the run so the manager's run_history row carries
# it via the run-complete payload errors, rather than silently disabling the
# feature.
_SKIP_PROCESSED_DISABLED_NOTE = (
    "skip_processed: true requested but worker mode has no processed-file "
    "tracker (stateless worker, no per-worker DB) — already-seen files will "
    "be reprocessed on every run; duplicate records possible"
)


def _source_requests_skip_processed(config: PipelineConfig) -> bool:
    """True when the pipeline's source requests skip_processed semantics.

    Only the file/object-storage sources (sftp, local, ftp, s3, gcs,
    azure_blob) and the CORBA dedupe source define a ``skip_processed``
    attribute; the getattr default keeps every other source type (kafka,
    syslog, http, ...) out of the check.
    """
    return bool(getattr(config.source, "skip_processed", False))


# ── Request / response models ──────────────────────────────────────────────


class RunRequest(BaseModel):
    pipeline_name: str
    yaml_text: str
    run_id: str
    schedule_type: str = "batch"   # "batch" | "stream"
    callback_url: str = ""         # manager endpoint for run-complete; may be empty
    flush: bool = False            # F.1 §5: manual flush run — close(flush=True) emits
                                   # open windows as partials and clears them from state


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
    started_at_dt: datetime | None = None
    # sha256(req.yaml_text)[:16] captured at dispatch time (D.2 §6.1). Lets the
    # manager detect stale-config adoption; empty string means "not computed".
    config_sha256: str = ""
    stats_url: str = ""
    stats: PipelineStats | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = field(default=None, compare=False)
    # GH #39: degradation notes (e.g. skip_processed unhonored) merged into the
    # run-complete payload errors so the manager's run_history row records them.
    degradation_notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.started_at_dt is None:
            self.started_at_dt = datetime.fromisoformat(self.started_at)


class WorkerState:
    """Thread-safe store of currently-active pipeline runs."""

    def __init__(self, worker_id: str, manager_url: str, api_key: str = "") -> None:
        self.worker_id = worker_id
        self.manager_url = manager_url
        self.api_key = api_key
        self._runs: dict[str, ActiveRun] = {}
        self._lock = threading.Lock()
        self.stats_stop = threading.Event()

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
    worker_id: str,
    status: str,
    records_in: int,
    records_out: int,
    bytes_in: int,
    bytes_out: int,
    error: str | None,
    records_skipped: int = 0,
    errors: list[str] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    api_key: str = "",
) -> None:
    """POST run-complete to the manager with bounded retry (review D2).

    A transient manager outage must not lose the completion record: up to
    ``_RUN_COMPLETE_RETRIES`` attempts with exponential backoff. Exhausted
    retries still log and swallow — never raise — so the reconciler's
    lost/adopted-run path remains the degraded fallback. The manager's
    duplicate-callback guard (existing-run check) makes retries idempotent.
    """
    if not callback_url:
        return
    payload = {
        "run_id": run_id,
        "pipeline_name": pipeline_name,
        "worker_id": worker_id,
        "status": status,
        "records_in": records_in,
        "records_out": records_out,
        "records_skipped": records_skipped,
        "bytes_in": bytes_in,
        "bytes_out": bytes_out,
        "error": error,
        "errors": errors or [],
        "started_at": started_at,
        "finished_at": finished_at,
    }
    headers = {"X-API-Key": api_key} if api_key else None
    last_exc: Exception | None = None
    for attempt in range(_RUN_COMPLETE_RETRIES):
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(callback_url, json=payload, headers=headers)
                resp.raise_for_status()
            logger.debug(
                "run-complete callback sent",
                extra={"run_id": run_id, "status": status},
            )
            return
        except Exception as exc:
            last_exc = exc
            if attempt < _RUN_COMPLETE_RETRIES - 1:
                delay = _RUN_COMPLETE_BACKOFF_BASE_S * (2 ** attempt) + random.uniform(0, 0.25)
                logger.warning(
                    "run-complete callback failed, retrying",
                    extra={
                        "callback_url": callback_url,
                        "run_id": run_id,
                        "attempt": attempt + 1,
                        "retries": _RUN_COMPLETE_RETRIES - 1,
                        "delay": delay,
                        "error": str(exc),
                    },
                )
                time.sleep(delay)
    logger.warning(
        "run-complete callback failed after all retries — reconciler adoption "
        "path will take over",
        extra={
            "callback_url": callback_url,
            "run_id": run_id,
            "attempts": _RUN_COMPLETE_RETRIES,
            "error": str(last_exc),
        },
    )


def _post_stats(stats_url: str, payload: dict, api_key: str = "") -> None:
    if not stats_url:
        return
    headers = {"X-API-Key": api_key} if api_key else None
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(stats_url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as exc:
        from tram.metrics.registry import MGR_STATS_MISSED_TOTAL
        worker_id = str(payload.get("worker_id", "") or "")
        with _STATS_MISS_LOCK:
            consecutive = _CONSECUTIVE_STATS_MISSES.get(worker_id, 0) + 1
            _CONSECUTIVE_STATS_MISSES[worker_id] = consecutive
        MGR_STATS_MISSED_TOTAL.labels(worker_id=worker_id).inc()
        logger.warning(
            "pipeline-stats callback failed",
            extra={
                "stats_url": stats_url,
                "pipeline": payload.get("pipeline_name"),
                "run_id": payload.get("run_id"),
                "worker_id": worker_id,
                "consecutive_misses": consecutive,
                "error": str(exc),
            },
        )
        return
    worker_id = str(payload.get("worker_id", "") or "")
    with _STATS_MISS_LOCK:
        _CONSECUTIVE_STATS_MISSES.pop(worker_id, None)


def _derive_stats_url(callback_url: str, manager_url: str) -> str:
    if callback_url:
        base, _, _ = callback_url.rpartition("/")
        return f"{base}/pipeline-stats" if base else ""
    if manager_url:
        return f"{manager_url}/api/internal/pipeline-stats"
    return ""


def _emit_stats_once(state: WorkerState) -> None:
    now = datetime.now(UTC)
    for run in state.snapshot():
        if run.stats is None or not run.stats_url or run.started_at_dt is None:
            continue
        payload = {
            "worker_id": state.worker_id,
            "pipeline_name": run.pipeline_name,
            "run_id": run.run_id,
            "schedule_type": run.schedule_type,
            "uptime_seconds": max((now - run.started_at_dt).total_seconds(), 0.0),
            "timestamp": now.isoformat(),
            "is_final": False,
            **run.stats.snapshot_and_reset_window(),
        }
        _post_stats(run.stats_url, payload, api_key=state.api_key)


def _final_stats_snapshot(run: ActiveRun) -> dict[str, int | list[str]]:
    if run.stats is None:
        return {
            "records_in": 0,
            "records_out": 0,
            "records_skipped": 0,
            "dlq_count": 0,
            "error_count": 0,
            "bytes_in": 0,
            "bytes_out": 0,
            "errors_last_window": [],
        }
    return run.stats.snapshot_and_reset_window()


def _active_run_status(run: ActiveRun, worker_id: str, now: datetime) -> dict[str, object]:
    uptime_seconds = 0.0
    if run.started_at_dt is not None:
        uptime_seconds = max((now - run.started_at_dt).total_seconds(), 0.0)
    stats = run.stats.snapshot() if run.stats is not None else {
        "records_in": 0,
        "records_out": 0,
        "records_skipped": 0,
        "dlq_count": 0,
        "error_count": 0,
        "bytes_in": 0,
        "bytes_out": 0,
        "errors_last_window": [],
    }
    return {
        "run_id": run.run_id,
        "pipeline": run.pipeline_name,
        "started_at": run.started_at,
        "schedule_type": run.schedule_type,
        "worker_id": worker_id,
        "uptime_seconds": uptime_seconds,
        "config_sha256": run.config_sha256,
        "stats": stats,
    }


def _stats_loop(state: WorkerState, interval: int) -> None:
    while not state.stats_stop.wait(interval):
        _emit_stats_once(state)


# ── App factory ────────────────────────────────────────────────────────────


def create_worker_app(worker_id: str = "", manager_url: str = "", stats_interval: int | None = None) -> FastAPI:
    """Create and return the worker agent FastAPI application."""
    if not worker_id:
        worker_id = os.environ.get("TRAM_WORKER_ID", socket.gethostname())
    if not manager_url:
        manager_url = os.environ.get("TRAM_MANAGER_URL", "")
    if stats_interval is None:
        stats_interval = int(os.environ.get("TRAM_STATS_INTERVAL", "30"))
    api_key = os.environ.get("TRAM_API_KEY", "")

    state = WorkerState(worker_id=worker_id, manager_url=manager_url, api_key=api_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Trigger plugin registration (connectors/transforms/serializers)
        import tram.connectors  # noqa: F401
        import tram.serializers  # noqa: F401
        import tram.transforms  # noqa: F401

        state.stats_stop.clear()
        stats_thread = threading.Thread(
            target=_stats_loop,
            args=(state, stats_interval),
            daemon=True,
            name="tram-agent-stats",
        )
        stats_thread.start()

        logger.info("Worker agent ready", extra={"worker_id": worker_id})
        yield

        # Signal all active streams to stop on shutdown
        state.stats_stop.set()
        for run in state.snapshot():
            run.stop_event.set()
        if stats_thread.is_alive():
            stats_thread.join(timeout=stats_interval + 1)
        logger.info("Worker agent stopped", extra={"worker_id": worker_id})

    app = FastAPI(
        title="TRAM Worker Agent",
        description="Internal agent API for pipeline execution workers",
        lifespan=lifespan,
    )
    app.state.worker = state

    # Internal agent API: same API-key middleware as the manager ingress, with
    # the /agent/* routes as the protected internal surface. /agent/health is
    # always exempt so K8s probes never need a key. TRAM_INTERNAL_AUTH_MODE
    # defaults to warn — Phase 2 flips to enforce without code changes.
    from tram.api.middleware import APIKeyMiddleware
    app.add_middleware(APIKeyMiddleware, internal_prefixes=("/agent/",))

    # ── GET /agent/health ──────────────────────────────────────────────────

    @app.get("/agent/health")
    def health():
        active = state.snapshot()
        ingress_thread = getattr(app.state, "ingress_thread", None)
        ingress_alive = ingress_thread.is_alive() if ingress_thread is not None else True
        return {
            "ok": ingress_alive,
            "worker_id": worker_id,
            "active_runs": len(active),
            "running_pipelines": list({r.pipeline_name for r in active}),
            "ingress_up": ingress_alive,
        }

    # ── GET /agent/status ──────────────────────────────────────────────────

    @app.get("/agent/status")
    def status():
        active = state.snapshot()
        now = datetime.now(UTC)
        running = [
            _active_run_status(r, worker_id, now)
            for r in active
            if r.schedule_type != "stream"
        ]
        streams = [
            _active_run_status(r, worker_id, now)
            for r in active
            if r.schedule_type == "stream"
        ]
        return {
            "worker_id": worker_id,
            "active_runs": len(active),
            "running_pipelines": sorted({r.pipeline_name for r in active}),
            "running": running,
            "streams": streams,
        }

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

        # D.2 §6.1: fingerprint the dispatched YAML before loading so the
        # manager can detect stale-config adoption after a restart.
        config_sha256 = hashlib.sha256(req.yaml_text.encode()).hexdigest()[:16]

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
            config_sha256=config_sha256,
            stats_url=_derive_stats_url(callback_url, state.manager_url),
            stats=PipelineStats(
                run_id=req.run_id,
                pipeline_name=req.pipeline_name,
                schedule_type=req.schedule_type,
            ),
        )
        # F.1 (§3.2b): worker-mode runs reach the transform-state blob through
        # the manager's internal API (the same availability envelope as the
        # dispatch that created this run). No manager URL → no store, so
        # stateful transforms stay in-memory (correct for a single run).
        from tram.pipeline.state_store import HttpTransformStateStore

        state_store = (
            HttpTransformStateStore(state.manager_url, state.api_key)
            if state.manager_url
            else None
        )
        # GH #54: worker-mode skip_processed rides the manager's processed-file
        # tracker through the internal API (the F.1 state-store availability
        # envelope — a worker run cannot exist without a manager dispatch). No
        # manager URL → no client, and the construction guard below fires.
        from tram.agent.file_tracker_client import HttpFileTracker

        file_tracker = (
            HttpFileTracker(
                state.manager_url,
                state.api_key,
                on_degradation=active_run.degradation_notes.append,
            )
            if state.manager_url
            else None
        )
        executor = PipelineExecutor(state_store=state_store, file_tracker=file_tracker)

        # GH #39 (A1)/#54: the worker is stateless — no per-worker DB exists,
        # so without a manager URL the executor is built without a
        # processed-file tracker and a pipeline whose source requests
        # skip_processed would silently reprocess every file on every run. Fail
        # loud instead and record the degradation on the run (when a tracker
        # client errors at call time, HttpFileTracker fails loud itself) so the
        # manager's run_history row carries it.
        if (
            _source_requests_skip_processed(config)
            and getattr(executor, "_file_tracker", None) is None
        ):
            logger.error(
                "skip_processed cannot be honored in worker mode — no "
                "processed-file tracker (stateless worker, no per-worker DB)",
                extra={
                    "pipeline": req.pipeline_name,
                    "run_id": req.run_id,
                    "source": config.source.type,
                },
            )
            active_run.degradation_notes.append(_SKIP_PROCESSED_DISABLED_NOTE)

        data_dir = os.environ.get("TRAM_DATA_DIR", "/data")
        api_key  = os.environ.get("TRAM_API_KEY", "")

        if req.schedule_type == "stream":
            def _stream_thread():
                try:
                    from tram.agent.assets import sync_assets
                    sync_assets(config, state.manager_url, data_dir, api_key)
                    executor.stream_run(
                        config, active_run.stop_event, stats=active_run.stats,
                        config_sha256=config_sha256,
                    )
                    stats_snapshot = _final_stats_snapshot(active_run)
                    _post_run_complete(
                        callback_url, req.run_id, req.pipeline_name, state.worker_id,
                        "success",
                        int(stats_snapshot["records_in"]),
                        int(stats_snapshot["records_out"]),
                        int(stats_snapshot["bytes_in"]),
                        int(stats_snapshot["bytes_out"]),
                        None,
                        int(stats_snapshot["records_skipped"]),
                        list(stats_snapshot["errors_last_window"])
                        + active_run.degradation_notes,
                        started_at=active_run.started_at,
                        finished_at=datetime.now(UTC).isoformat(),
                        api_key=state.api_key,
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
                        callback_url, req.run_id, req.pipeline_name, state.worker_id,
                        "error", 0, 0, 0, 0, str(exc),
                        started_at=active_run.started_at,
                        finished_at=datetime.now(UTC).isoformat(),
                        api_key=state.api_key,
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
                    result = executor.batch_run(
                        config, run_id=req.run_id, stats=active_run.stats,
                        config_sha256=config_sha256,
                        flush=req.flush,
                    )
                    if active_run.stats is not None:
                        payload = {
                            "worker_id": state.worker_id,
                            "pipeline_name": req.pipeline_name,
                            "run_id": req.run_id,
                            "schedule_type": req.schedule_type,
                            "uptime_seconds": max(
                                (datetime.now(UTC) - active_run.started_at_dt).total_seconds(),
                                0.0,
                            ),
                            "timestamp": datetime.now(UTC).isoformat(),
                            "is_final": True,
                            **active_run.stats.snapshot_and_reset_window(),
                        }
                        # If stats_url is empty, run-complete still executes below and
                        # manager-side on_worker_run_complete removes the store entry.
                        _post_stats(active_run.stats_url, payload, api_key=state.api_key)
                    _post_run_complete(
                        callback_url, req.run_id, req.pipeline_name, state.worker_id,
                        result.status.value,
                        result.records_in,
                        result.records_out,
                        result.bytes_in,
                        result.bytes_out,
                        result.error,
                        result.records_skipped,
                        list(result.errors or []) + active_run.degradation_notes,
                        result.started_at.isoformat(),
                        result.finished_at.isoformat(),
                        api_key=state.api_key,
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
                        callback_url, req.run_id, req.pipeline_name, state.worker_id,
                        "error", 0, 0, 0, 0, str(exc),
                        started_at=active_run.started_at,
                        finished_at=datetime.now(UTC).isoformat(),
                        api_key=state.api_key,
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


def create_worker_ingress_app(worker_id: str = "", api_key: str = "") -> FastAPI:
    """Minimal push-traffic receiver on :8767 — /webhooks/* only, no /agent/* routes."""
    from tram.api.routers.webhooks import router as webhooks_router

    app = FastAPI(title="TRAM Worker Ingress", openapi_url=None)
    app.include_router(webhooks_router)

    if api_key:
        from tram.api.middleware import APIKeyMiddleware
        app.add_middleware(APIKeyMiddleware)

    @app.get("/agent/health")
    def ingress_health():
        return {"ok": True, "worker_id": worker_id, "port": "ingress"}

    return app
