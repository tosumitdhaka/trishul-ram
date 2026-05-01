"""PipelineController — single authority for all pipeline lifecycle operations.

v1.2.0: Coordinator, rebalance loop, sync loop, and node_registry removed.
        The manager+worker split makes per-pod consensus unnecessary.
        Standalone mode (no workers) executes pipelines locally as before.

State machine
─────────────
  scheduled → running  (APScheduler fires / stream starts)
  running   → scheduled (batch success on interval/cron pipeline)
  running   → stopped   (batch success on manual pipeline, or explicit stop())
  running   → error     (run failed)
  stopped   → scheduled (start() called by user)
  error     → scheduled (start() called by user)
  *         → deleted   (delete())
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from tram.core.context import RunResult, RunStatus
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.manager import PipelineManager, PipelineState

if TYPE_CHECKING:
    from tram.agent.metrics import PipelineStats
    from tram.agent.worker_pool import WorkerPool
    from tram.models.pipeline import PipelineConfig
    from tram.persistence.db import TramDB
    from tram.persistence.file_tracker import ProcessedFileTracker

logger = logging.getLogger(__name__)


@dataclass
class _LocalRun:
    run_id: str
    pipeline_name: str
    schedule_type: str
    started_at: datetime
    stats: PipelineStats


@dataclass
class _ActiveBatchRun:
    run_id: str
    pipeline_name: str
    worker_url: str
    schedule_type: str
    started_at: datetime


class PipelineController:
    """Single authority for all pipeline lifecycle operations."""

    def __init__(
        self,
        db: TramDB | None = None,
        file_tracker: ProcessedFileTracker | None = None,
        node_id: str = "",
        # v1.2.0 manager+worker
        worker_pool: WorkerPool | None = None,
        manager_url: str = "",
        stats_store=None,
        kubernetes_service_manager=None,
    ) -> None:
        self._db = db
        self._node_id = node_id
        self._worker_pool = worker_pool
        self._manager_url = manager_url
        self._stats_store = stats_store
        self._kubernetes_service_manager = kubernetes_service_manager

        self.manager = PipelineManager(db=db)
        self.executor = PipelineExecutor(file_tracker=file_tracker)

        self._scheduler = None          # APScheduler BackgroundScheduler
        self._stream_threads: dict[str, threading.Thread] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._thread_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="tram-batch")
        # Tracks dispatched stream run_ids per pipeline: {pipeline_name: [run_id, ...]}
        self._stream_run_ids: dict[str, list[str]] = {}
        # {placement_group_id: placement_dict}
        self._broadcast_placements: dict[str, dict] = {}
        # {pipeline_name: placement_group_id}
        self._active_placement_group: dict[str, str] = {}
        # {pipeline_name: _ActiveBatchRun}
        self._active_batch_runs: dict[str, _ActiveBatchRun] = {}

        self._running = False

        # Standalone live stats — only used when _worker_pool is None
        self._local_active_stats: dict[str, _LocalRun] = {}
        self._local_stats_lock = threading.Lock()
        self._local_stats_stop = threading.Event()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the controller: load pipelines from DB, schedule owned ones."""
        from apscheduler.schedulers.background import BackgroundScheduler

        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._running = True

        # Load all pipelines from DB and add APScheduler jobs before starting
        if self._db is not None:
            self._boot_load()

        self._scheduler.start()

        if self._worker_pool is None:
            stats_interval = int(getattr(self._stats_store, "_interval", 30)) if self._stats_store else 30
            self._local_stats_stop.clear()
            t = threading.Thread(
                target=self._local_stats_loop,
                args=(stats_interval,),
                name="tram-local-stats",
                daemon=True,
            )
            t.start()

        logger.info("PipelineController started")

    def stop(self, timeout: int = 30) -> None:
        """Stop the controller and all running pipelines gracefully."""
        self._running = False
        self._local_stats_stop.set()
        logger.info("PipelineController stopping",
                    extra={"drain_timeout_seconds": timeout})

        # On manager shutdown, keep worker-side streams alive so placement
        # reconciliation can restore state after restart. Manual stop/delete paths
        # still stop workers explicitly.
        if self._worker_pool is None:
            pass
        self._stream_run_ids.clear()

        for name in list(self._stop_events.keys()):
            self._stop_events[name].set()

        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

        self._thread_pool.shutdown(wait=True, cancel_futures=False)

        for name, thread in list(self._stream_threads.items()):
            if thread.is_alive():
                thread.join(timeout=timeout)
                if thread.is_alive():
                    logger.warning("Stream thread did not stop within timeout",
                                   extra={"pipeline": name, "timeout_seconds": timeout})

        logger.info("PipelineController stopped")

    # ── Boot sequence ──────────────────────────────────────────────────────

    def _boot_load(self) -> None:
        """Load all pipelines from DB and schedule non-stopped, enabled ones."""
        from tram.pipeline.loader import load_pipeline_from_yaml

        stopped_names = set(self._db.get_stopped_pipeline_names())
        placements_by_pipeline = {
            placement["pipeline_name"]: placement
            for placement in self._db.get_active_broadcast_placements()
        }

        for name, yaml_text in self._db.get_all_pipelines():
            try:
                config = load_pipeline_from_yaml(yaml_text)
                self.manager.register(config, yaml_text=yaml_text, save_version=False)
                if name in stopped_names:
                    self.manager.set_status(name, "stopped")
                    continue
                placement = placements_by_pipeline.get(name)
                if placement is not None:
                    self._restore_broadcast_placement(placement)
                    self.manager.set_status(name, "reconciling")
                    self._activate_kubernetes_service(config)
                    continue
                if config.enabled:
                    self._do_schedule(name)
            except Exception as exc:
                logger.warning("Boot: failed to load pipeline",
                               extra={"pipeline": name, "error": str(exc)})

    # ── Public API (called by routers) ─────────────────────────────────────

    def register(
        self,
        config: PipelineConfig,
        yaml_text: str,
        source: str = "api",
    ) -> PipelineState:
        """Register a new pipeline, persist to DB, and schedule if appropriate."""
        state = self.manager.register(config, yaml_text=yaml_text)

        if self._db is not None:
            self._db.save_pipeline(config.name, yaml_text, source=source)

        if config.enabled:
            self._do_schedule(config.name)

        logger.info("Registered pipeline", extra={"pipeline": config.name})
        return state

    def update(self, name: str, yaml_text: str) -> PipelineState:
        """Update an existing pipeline's YAML. Restarts if it was running/scheduled."""
        state = self.manager.get(name)
        if state.yaml_text == yaml_text:
            if self._db is not None:
                self._db.save_pipeline(name, yaml_text, source="api")
            logger.info("Update skipped — identical YAML", extra={"pipeline": name})
            return state

        from tram.pipeline.loader import load_pipeline_from_yaml

        config = load_pipeline_from_yaml(yaml_text)
        was_active = state.status in ("scheduled", "running")

        self._stop_execution(name)
        self.manager.deregister(name)
        new_state = self.manager.register(config, yaml_text=yaml_text)

        if self._db is not None:
            self._db.save_pipeline(name, yaml_text, source="api")

        if was_active and config.enabled and self._may_schedule(name):
            self._do_schedule(name)

        logger.info("Updated pipeline", extra={"pipeline": name})
        return new_state

    def delete(self, name: str) -> None:
        """Stop, deregister, and soft-delete a pipeline."""
        self._stop_execution(name)
        self.manager.deregister(name)
        if self._db is not None:
            self._db.delete_pipeline(name)
        logger.info("Deleted pipeline", extra={"pipeline": name})

    def start_pipeline(self, name: str) -> Literal["started", "already_running", "disabled", "manual"]:
        """Start a stopped/errored pipeline and report what actually happened."""
        state = self.manager.get(name)
        if state.status in ("running", "scheduled"):
            logger.debug("start_pipeline: already running", extra={"pipeline": name})
            return "already_running"

        if self._db is not None:
            self._db.start_pipeline_flag(name)

        if not state.config.enabled:
            self.manager.set_status(name, "stopped")
            return "disabled"

        if state.config.schedule.type == "manual":
            self.manager.set_status(name, "stopped")
            logger.debug("start_pipeline: manual pipeline not auto-scheduled", extra={"pipeline": name})
            return "manual"

        self._do_schedule(name)
        return "started"

    def stop_pipeline(self, name: str) -> None:
        """Stop a pipeline and mark it so it won't auto-restart."""
        self._stop_execution(name)
        if self._db is not None:
            self._db.stop_pipeline(name)
        self.manager.set_status(name, "stopped")
        logger.info("Stopped pipeline", extra={"pipeline": name})

    def restart_pipeline(self, name: str) -> None:
        """Restart a pipeline — stop active execution then immediately reschedule.

        Works in both standalone and manager+worker mode.  For stream pipelines
        in manager mode the stream dispatch is cancelled and re-dispatched
        (potentially on a different worker, as determined by the WorkerPool).
        """
        state = self.manager.get(name)

        # Stop any active execution without persisting the stopped flag
        if state.status in ("running", "scheduled"):
            sched_type = state.config.schedule.type
            if sched_type == "stream":
                self._stop_stream(name)
            else:
                job_id = f"batch-{name}"
                if self._scheduler and self._scheduler.get_job(job_id):
                    self._scheduler.remove_job(job_id)
            self.manager.set_status(name, "stopped")

        # Clear any persistent stopped flag so _may_schedule passes
        if self._db is not None:
            self._db.start_pipeline_flag(name)

        if state.config.enabled and self._may_schedule(name):
            self._do_schedule(name)
        else:
            self.manager.set_status(name, "stopped")

        logger.info("Restarted pipeline", extra={"pipeline": name})

    def trigger_run(self, name: str) -> str:
        """Immediate one-shot run. Returns run_id. Works even when pipeline is stopped."""
        state = self.manager.get(name)
        if state.config.schedule.type == "stream":
            raise ValueError(f"Pipeline '{name}' is a stream pipeline — cannot trigger manually")
        if state.status == "running":
            raise ValueError(f"Pipeline '{name}' is already running")
        run_id = str(uuid.uuid4())
        self._thread_pool.submit(self._run_batch, name, run_id)
        return run_id

    def rollback(self, name: str, version: int):
        """Restore a previous pipeline version and restart if appropriate."""
        return self.manager.rollback(name, version)

    # ── Read-only delegation ───────────────────────────────────────────────

    def get(self, name: str) -> PipelineState:
        return self.manager.get(name)

    def list_all(self) -> list[PipelineState]:
        return self.manager.list_all()

    def exists(self, name: str) -> bool:
        return self.manager.exists(name)

    def get_runs(self, **kwargs):
        return self.manager.get_runs(**kwargs)

    def get_run(self, run_id: str):
        return self.manager.get_run(run_id)

    def get_scheduler_status(self) -> dict:
        next_runs = []
        if self._scheduler:
            for job in self._scheduler.get_jobs():
                next_run = job.next_run_time
                next_runs.append({
                    "pipeline": job.id.removeprefix("batch-"),
                    "next_run": next_run.isoformat() if next_run else None,
                })
        workers = None
        if self._worker_pool is not None:
            workers = self._worker_pool.status()
        return {
            "scheduler_running": self._running,
            "active_streams": list(self._stream_threads.keys()),
            "scheduled_jobs": next_runs,
            "workers": workers,
        }

    # ── Scheduling gate ────────────────────────────────────────────────────

    def _may_schedule(self, name: str) -> bool:
        """Return True only when the pipeline is allowed to run.

        Guards:
          1. Not explicitly stopped by user (DB flag)
          2. YAML config.enabled = true
        """
        state = self.manager.get(name) if self.manager.exists(name) else None
        if state is None:
            return False
        if self._db and self._db.is_pipeline_stopped(name):
            return False
        if not state.config.enabled:
            return False
        return True

    def _do_schedule(self, name: str) -> None:
        """Internal: schedule a pipeline (assumes _may_schedule has been checked)."""
        state = self.manager.get(name)
        sched_type = state.config.schedule.type

        if sched_type == "stream":
            self._start_stream(state.config)
        elif sched_type == "interval":
            self._add_interval_job(state.config)
        elif sched_type == "cron":
            self._add_cron_job(state.config)
        elif sched_type == "manual":
            self.manager.set_status(name, "stopped")
            logger.debug("Pipeline is manual — not scheduling", extra={"pipeline": name})

    # ── Batch execution ────────────────────────────────────────────────────

    def _add_interval_job(self, config: PipelineConfig) -> None:
        from apscheduler.triggers.interval import IntervalTrigger

        interval = config.schedule.interval_seconds
        job_id = f"batch-{config.name}"
        now = datetime.now(UTC)

        state = self.manager.get(config.name) if self.manager.exists(config.name) else None
        last_run = state.last_run if state else None
        if last_run is None:
            next_run_time = now
        else:
            elapsed = (now - last_run).total_seconds()
            delay = max(0.0, interval - elapsed)
            next_run_time = now + timedelta(seconds=delay)

        self._scheduler.add_job(
            func=self._run_batch,
            trigger=IntervalTrigger(seconds=interval),
            id=job_id,
            args=[config.name],
            max_instances=1,
            replace_existing=True,
            misfire_grace_time=60,
            next_run_time=next_run_time,
        )
        self.manager.set_status(config.name, "scheduled")
        logger.info("Scheduled interval pipeline",
                    extra={"pipeline": config.name, "interval_seconds": interval,
                           "next_run_in_seconds": round((next_run_time - now).total_seconds())})

    def _add_cron_job(self, config: PipelineConfig) -> None:
        from apscheduler.triggers.cron import CronTrigger

        job_id = f"batch-{config.name}"
        self._scheduler.add_job(
            func=self._run_batch,
            trigger=CronTrigger.from_crontab(config.schedule.cron),
            id=job_id,
            args=[config.name],
            max_instances=1,
            replace_existing=True,
            misfire_grace_time=60,
        )
        self.manager.set_status(config.name, "scheduled")
        logger.info("Scheduled cron pipeline",
                    extra={"pipeline": config.name, "cron": config.schedule.cron})

    def _run_batch(self, pipeline_name: str, run_id: str | None = None) -> None:
        """APScheduler/thread-pool callback — one batch execution."""
        if not self.manager.exists(pipeline_name):
            job_id = f"batch-{pipeline_name}"
            if self._scheduler and self._scheduler.get_job(job_id):
                self._scheduler.remove_job(job_id)
            logger.warning("Batch job: pipeline not found, removed orphan job",
                           extra={"pipeline": pipeline_name})
            return

        state = self.manager.get(pipeline_name)
        if state.status == "running":
            logger.warning("Batch job: previous run still active, skipping",
                           extra={"pipeline": pipeline_name})
            return

        self.manager.set_status(pipeline_name, "running")

        if run_id is None:
            run_id = str(uuid.uuid4())

        # ── Manager+worker dispatch path ───────────────────────────────────
        if self._worker_pool is not None:
            callback_url = (
                f"{self._manager_url}/api/internal/run-complete"
                if self._manager_url else ""
            )
            worker_url = self._worker_pool.dispatch(
                run_id=run_id,
                pipeline_name=pipeline_name,
                yaml_text=state.yaml_text,
                schedule_type=state.config.schedule.type,
                callback_url=callback_url,
            )
            if worker_url is None:
                failure_time = datetime.now(UTC)
                error = "No healthy workers available for dispatch"
                logger.error("Batch dispatch failed: no healthy workers",
                             extra={"pipeline": pipeline_name, "run_id": run_id})
                result = RunResult(
                    run_id=run_id,
                    pipeline_name=pipeline_name,
                    status=RunStatus.FAILED,
                    started_at=failure_time,
                    finished_at=failure_time,
                    records_in=0,
                    records_out=0,
                    records_skipped=0,
                    error=error,
                    node_id=self._node_id,
                )
                self._finalize_batch_result(pipeline_name, result)
                from tram.metrics.registry import MGR_DISPATCH_TOTAL
                MGR_DISPATCH_TOTAL.labels(pipeline=pipeline_name, result="no_workers").inc()
            else:
                self._active_batch_runs[pipeline_name] = _ActiveBatchRun(
                    run_id=run_id,
                    pipeline_name=pipeline_name,
                    worker_url=worker_url,
                    schedule_type=state.config.schedule.type,
                    started_at=datetime.now(UTC),
                )
                from tram.metrics.registry import MGR_DISPATCH_TOTAL
                MGR_DISPATCH_TOTAL.labels(pipeline=pipeline_name, result="accepted").inc()
            return
        # ── Local execution path ───────────────────────────────────────────

        try:
            state = self.manager.get(pipeline_name)
            result = self.executor.batch_run(state.config, run_id=run_id)
            self._finalize_batch_result(pipeline_name, result)
        except Exception as exc:
            logger.error("Batch run exception",
                         extra={"pipeline": pipeline_name, "error": str(exc)})
            self.manager.set_status(pipeline_name, "error")

    def _on_run_complete(self, pipeline_name: str, result) -> None:
        """Post-run state transition — called after every batch run completes."""
        current_status = self.manager.get(pipeline_name).status
        if current_status == "stopped":
            return

        if result.status == RunStatus.SUCCESS:
            job_id = f"batch-{pipeline_name}"
            has_job = self._scheduler and self._scheduler.get_job(job_id)
            state = self.manager.get(pipeline_name)
            sched_type = state.config.schedule.type

            if has_job:
                final_status = "scheduled"
            elif sched_type in ("interval", "cron") and state.config.enabled:
                if self._may_schedule(pipeline_name):
                    self._do_schedule(pipeline_name)
                    return
                else:
                    # Pipeline was explicitly stopped or otherwise not schedulable —
                    # restore stopped status rather than showing misleading "scheduled"
                    final_status = "stopped"
            else:
                final_status = "stopped"
        else:
            final_status = "error"

        self.manager.set_status(pipeline_name, final_status)

    def _finalize_batch_result(self, pipeline_name: str, result: RunResult) -> None:
        """Record a batch result and apply the standard post-run state transition."""
        self.manager.record_run(pipeline_name, result)
        self._on_run_complete(pipeline_name, result)
        state = self.manager.get(pipeline_name)
        if state.status in {"stopped", "error"}:
            self._deactivate_kubernetes_service(state.config)

    def get_active_batch_runs(self) -> list[dict]:
        return [
            {
                "run_id": run.run_id,
                "pipeline_name": run.pipeline_name,
                "worker_url": run.worker_url,
                "schedule_type": run.schedule_type,
                "started_at": run.started_at,
            }
            for run in self._active_batch_runs.values()
        ]

    def adopt_active_batch_run(
        self,
        *,
        pipeline_name: str,
        run_id: str,
        worker_url: str,
        started_at: datetime | str | None = None,
    ) -> bool:
        if not self.manager.exists(pipeline_name):
            return False
        state = self.manager.get(pipeline_name)
        if state.config.schedule.type == "stream":
            return False
        if isinstance(started_at, str):
            started_at = datetime.fromisoformat(started_at)
        self._active_batch_runs[pipeline_name] = _ActiveBatchRun(
            run_id=run_id,
            pipeline_name=pipeline_name,
            worker_url=worker_url,
            schedule_type=state.config.schedule.type,
            started_at=started_at or datetime.now(UTC),
        )
        self.manager.set_status(pipeline_name, "running")
        return True

    def mark_active_batch_run_lost(
        self,
        pipeline_name: str,
        *,
        error: str,
        run_id: str | None = None,
        finished_at: datetime | None = None,
    ) -> bool:
        lease = self._active_batch_runs.pop(pipeline_name, None)
        state = self.manager.get(pipeline_name) if self.manager.exists(pipeline_name) else None
        if state is None:
            return False

        lease_run_id = run_id or (lease.run_id if lease is not None else str(uuid.uuid4()))
        if self._worker_pool is not None:
            self._worker_pool.on_run_complete(lease_run_id)
        lease_started_at = lease.started_at if lease is not None else (
            state.last_run or datetime.now(UTC)
        )
        lease_node_id = self._node_id
        if lease is not None and self._worker_pool is not None:
            lease_node_id = self._worker_pool.worker_id_for_url(lease.worker_url) or self._node_id

        result = RunResult(
            run_id=lease_run_id,
            pipeline_name=pipeline_name,
            status=RunStatus.FAILED,
            started_at=lease_started_at,
            finished_at=finished_at or datetime.now(UTC),
            records_in=0,
            records_out=0,
            records_skipped=0,
            error=error,
            node_id=lease_node_id,
        )
        self._finalize_batch_result(pipeline_name, result)
        return True

    def on_worker_run_complete(
        self,
        run_id: str,
        pipeline_name: str,
        worker_id: str | None,
        status: str,
        records_in: int,
        records_out: int,
        records_skipped: int = 0,
        bytes_in: int = 0,
        bytes_out: int = 0,
        error: str | None = None,
        errors: list[str] | None = None,
        started_at: datetime | str | None = None,
        finished_at: datetime | str | None = None,
    ) -> None:
        """Callback from a worker agent when a dispatched run finishes."""
        try:
            run_status = RunStatus(status)
        except ValueError:
            run_status = RunStatus.FAILED

        existing_run = self.manager.get_run(run_id)
        if self._worker_pool is not None:
            self._worker_pool.on_run_complete(run_id)
        if existing_run is not None:
            logger.info(
                "Ignoring duplicate worker run-complete callback",
                extra={"pipeline": pipeline_name, "run_id": run_id},
            )
            self._active_batch_runs.pop(pipeline_name, None)
            self._remove_stream_run_id(pipeline_name, run_id)
            return

        if isinstance(started_at, str):
            started_at = datetime.fromisoformat(started_at)
        if isinstance(finished_at, str):
            finished_at = datetime.fromisoformat(finished_at)

        result_node_id = worker_id or self._node_id

        result = RunResult(
            run_id=run_id,
            pipeline_name=pipeline_name,
            status=run_status,
            started_at=started_at or datetime.now(UTC),
            finished_at=finished_at or datetime.now(UTC),
            records_in=records_in,
            records_out=records_out,
            records_skipped=records_skipped,
            bytes_in=bytes_in,
            bytes_out=bytes_out,
            error=error,
            node_id=result_node_id,
            errors=errors or [],
        )

        if self._worker_pool is not None:
            self._active_batch_runs.pop(pipeline_name, None)
            self._remove_stream_run_id(pipeline_name, run_id)
            if self._stream_run_ids.get(pipeline_name):
                return
            if self._stats_store is not None:
                self._stats_store.remove(run_id)

        if self.manager.exists(pipeline_name):
            self._finalize_batch_result(pipeline_name, result)
        else:
            logger.warning(
                "on_worker_run_complete: pipeline not found",
                extra={"pipeline": pipeline_name, "run_id": run_id},
            )

    # ── Stream execution ───────────────────────────────────────────────────

    def _start_stream(self, config: PipelineConfig) -> None:
        # ── Manager+worker dispatch path ───────────────────────────────────
        if self._worker_pool is not None:
            if config.name in self._stream_run_ids:
                logger.debug("Stream already dispatched to worker",
                             extra={"pipeline": config.name})
                return
            workers_cfg = config.workers
            state = self.manager.get(config.name)
            callback_url = (
                f"{self._manager_url}/api/internal/run-complete"
                if self._manager_url else ""
            )
            if workers_cfg is not None and (
                workers_cfg.count == "all"
                or (isinstance(workers_cfg.count, int) and workers_cfg.count > 1)
                or workers_cfg.worker_ids is not None
            ):
                placement_group_id = self._make_placement_group_id(config.name)
                result = self._worker_pool.multi_dispatch(
                    placement_group_id=placement_group_id,
                    pipeline_name=config.name,
                    yaml_text=state.yaml_text,
                    workers_cfg=workers_cfg,
                    schedule_type="stream",
                    callback_url=callback_url,
                )
                if not result.accepted:
                    logger.error("Stream dispatch failed: no healthy workers",
                                 extra={"pipeline": config.name})
                    self.manager.set_status(config.name, "error")
                    return
                from tram.metrics.registry import MGR_DISPATCH_TOTAL
                for _ in result.accepted:
                    MGR_DISPATCH_TOTAL.labels(pipeline=config.name, result="accepted").inc()
                self._record_broadcast_placement(config.name, placement_group_id, result, workers_cfg)
                self.manager.set_status(config.name, result.status)
                self._activate_kubernetes_service(config)
                logger.info(
                    "Dispatched stream to workers",
                    extra={
                        "pipeline": config.name,
                        "workers": result.accepted,
                        "placement_group_id": placement_group_id,
                        "run_ids": result.run_ids,
                    },
                )
                return

            run_id = str(uuid.uuid4())
            worker_url = self._worker_pool.dispatch(
                run_id=run_id,
                pipeline_name=config.name,
                yaml_text=state.yaml_text,
                schedule_type="stream",
                callback_url=callback_url,
            )
            if worker_url is None:
                logger.error("Stream dispatch failed: no healthy workers",
                             extra={"pipeline": config.name})
                self.manager.set_status(config.name, "error")
                return
            from tram.metrics.registry import MGR_DISPATCH_TOTAL
            MGR_DISPATCH_TOTAL.labels(pipeline=config.name, result="accepted").inc()
            self._stream_run_ids[config.name] = [run_id]
            self.manager.set_status(config.name, "running")
            self._activate_kubernetes_service(config)
            logger.info("Dispatched stream to worker",
                        extra={"pipeline": config.name, "worker": worker_url,
                               "run_id": run_id})
            return
        # ── Local execution path ───────────────────────────────────────────

        if config.name in self._stream_threads:
            old = self._stream_threads[config.name]
            if old.is_alive():
                old.join(timeout=15)
                if old.is_alive():
                    logger.warning("Stream already running", extra={"pipeline": config.name})
                    return
            self._stream_threads.pop(config.name, None)
            self._stop_events.pop(config.name, None)

        stop_event = threading.Event()
        self._stop_events[config.name] = stop_event
        thread = threading.Thread(
            target=self._stream_worker,
            args=(config, stop_event),
            name=f"tram-stream-{config.name}",
            daemon=True,
        )
        self._stream_threads[config.name] = thread
        self.manager.set_status(config.name, "running")
        thread.start()
        self._activate_kubernetes_service(config)
        logger.info("Started stream pipeline", extra={"pipeline": config.name})

    def _stream_worker(self, config: PipelineConfig, stop_event: threading.Event) -> None:
        run_id: str | None = None
        if self._worker_pool is None and self._stats_store is not None:
            from tram.agent.metrics import PipelineStats
            run_id = str(uuid.uuid4())
            stats = PipelineStats(run_id=run_id, pipeline_name=config.name, schedule_type="stream")
            local_run = _LocalRun(
                run_id=run_id,
                pipeline_name=config.name,
                schedule_type="stream",
                started_at=datetime.now(UTC),
                stats=stats,
            )
            with self._local_stats_lock:
                self._local_active_stats[run_id] = local_run
        else:
            stats = None

        try:
            self.executor.stream_run(config, stop_event, stats=stats)
        except Exception as exc:
            logger.error("Stream pipeline crashed",
                         extra={"pipeline": config.name, "error": str(exc)}, exc_info=True)
            self.manager.set_status(config.name, "error")
        finally:
            if run_id is not None:
                # Remove from dict and StatsStore atomically under the same lock so
                # _emit_local_stats_once() cannot resurrect the entry after removal.
                with self._local_stats_lock:
                    self._local_active_stats.pop(run_id, None)
                    if self._stats_store is not None:
                        self._stats_store.remove(run_id)
            self._stream_threads.pop(config.name, None)
            self._stop_events.pop(config.name, None)
            if self.manager.exists(config.name):
                if self.manager.get(config.name).status == "running":
                    self.manager.set_status(config.name, "stopped")
                self._deactivate_kubernetes_service(config)

    def _stop_stream(self, name: str, timeout: int = 10) -> None:
        # ── Manager+worker dispatch path ───────────────────────────────────
        if self._worker_pool is not None:
            run_ids = self._stream_run_ids.pop(name, [])
            for run_id in run_ids:
                self._worker_pool.stop_run(run_id, name)
            placement_group_id = self._active_placement_group.pop(name, None)
            if placement_group_id is not None:
                # Broadcast streams should stop every matching worker-side run,
                # even if the manager's slot list drifted during reconciliation.
                self._worker_pool.stop_pipeline_runs(name)
                placement = self._broadcast_placements.pop(placement_group_id, None)
                if placement is not None and self._db is not None:
                    self._db.update_broadcast_placement_status(
                        placement_group_id,
                        "stopped",
                        slots=placement["slots"],
                    )
            logger.info("Stopped dispatched stream pipeline", extra={"pipeline": name})
            return
        # ── Local execution path ───────────────────────────────────────────

        stop_event = self._stop_events.get(name)
        if stop_event:
            stop_event.set()
        thread = self._stream_threads.get(name)
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        logger.info("Stopped stream pipeline", extra={"pipeline": name})

    def _stop_execution(self, name: str) -> None:
        """Remove APScheduler job and stop stream thread for a pipeline."""
        if not self.manager.exists(name):
            return
        sched_type = self.manager.get(name).config.schedule.type
        if sched_type == "stream":
            self._stop_stream(name)
        else:
            job_id = f"batch-{name}"
            if self._scheduler and self._scheduler.get_job(job_id):
                self._scheduler.remove_job(job_id)
        self._deactivate_kubernetes_service(self.manager.get(name).config)
        self.manager.set_status(name, "stopped")

    def _remove_stream_run_id(self, pipeline_name: str, run_id: str) -> None:
        run_ids = self._stream_run_ids.get(pipeline_name)
        if not run_ids:
            return
        remaining = [existing for existing in run_ids if existing != run_id]
        if remaining:
            self._stream_run_ids[pipeline_name] = remaining
        else:
            self._stream_run_ids.pop(pipeline_name, None)

    def _make_placement_group_id(self, pipeline_name: str) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        return f"{pipeline_name}-{stamp}-{uuid.uuid4().hex[:6]}"

    def _restore_broadcast_placement(self, placement: dict) -> None:
        restored_at = datetime.now(UTC).isoformat()
        placement_copy = {
            **placement,
            "slots": [
                {
                    **dict(slot),
                    "dispatched_at": dict(slot).get("dispatched_at", restored_at),
                }
                for slot in placement["slots"]
            ],
        }
        placement_copy["status"] = "reconciling"
        placement_group_id = placement_copy["placement_group_id"]
        pipeline_name = placement_copy["pipeline_name"]
        self._broadcast_placements[placement_group_id] = placement_copy
        self._active_placement_group[pipeline_name] = placement_group_id
        self._sync_stream_run_ids_from_slots(pipeline_name, placement_copy["slots"])
        if self._db is not None:
            self._db.update_broadcast_placement_status(
                placement_group_id,
                "reconciling",
                slots=placement_copy["slots"],
            )

    def _record_broadcast_placement(self, pipeline_name: str, placement_group_id: str, result, workers_cfg) -> None:
        dispatched_at = datetime.now(UTC).isoformat()
        slots = []
        for slot in result.slots:
            worker_url = slot.get("worker_url")
            slots.append({
                "worker_index": int(slot["worker_index"]),
                "worker_url": worker_url,
                "worker_id": slot.get("worker_id") or (self._worker_pool.worker_id_for_url(worker_url) if (self._worker_pool and worker_url) else ""),
                "pinned_worker_id": slot.get("pinned_worker_id"),
                "run_id_prefix": slot["run_id_prefix"],
                "current_run_id": slot.get("current_run_id"),
                "dispatched_at": dispatched_at,
                "status": slot.get("status", "stale"),
                "restart_count": int(slot.get("restart_count", 0) or 0),
            })
        placement = {
            "placement_group_id": placement_group_id,
            "pipeline_name": pipeline_name,
            "slots": slots,
            "target_count": (
                len(workers_cfg.worker_ids)
                if workers_cfg is not None and workers_cfg.worker_ids is not None
                else (workers_cfg.count if workers_cfg is not None else 1)
            ),
            "started_at": datetime.now(UTC),
            "status": result.status,
        }
        self._broadcast_placements[placement_group_id] = placement
        self._active_placement_group[pipeline_name] = placement_group_id
        self._sync_stream_run_ids_from_slots(pipeline_name, slots)
        if self._db is not None:
            self._db.save_broadcast_placement(
                placement_group_id=placement_group_id,
                pipeline_name=pipeline_name,
                slots=slots,
                target_count=placement["target_count"],
                status=result.status,
                started_at=placement["started_at"],
            )

    def _sync_stream_run_ids_from_slots(self, pipeline_name: str, slots: list[dict]) -> None:
        run_ids = [
            str(slot["current_run_id"])
            for slot in slots
            if slot.get("current_run_id")
        ]
        if run_ids:
            self._stream_run_ids[pipeline_name] = run_ids
        else:
            self._stream_run_ids.pop(pipeline_name, None)

    def _update_broadcast_placement_status(self, placement_group_id: str, status: str) -> None:
        placement = self._broadcast_placements.get(placement_group_id)
        if placement is None:
            return
        placement["status"] = status
        pipeline_name = placement["pipeline_name"]
        if self.manager.exists(pipeline_name):
            self.manager.set_status(pipeline_name, status)
        if self._db is not None:
            self._db.update_broadcast_placement_status(
                placement_group_id,
                status,
                slots=placement["slots"],
            )
        from tram.metrics.registry import MGR_PLACEMENT_STATUS
        for s in ("running", "degraded", "reconciling", "error"):
            MGR_PLACEMENT_STATUS.labels(pipeline=pipeline_name, status=s).set(1 if s == status else 0)

    # ── Standalone live stats ──────────────────────────────────────────────

    def _emit_local_stats_once(self) -> None:
        """Snapshot all active standalone stream runs into StatsStore."""
        if self._stats_store is None:
            return
        from tram.api.routers.internal import PipelineStatsPayload
        now = datetime.now(UTC)
        with self._local_stats_lock:
            runs = list(self._local_active_stats.items())
        for run_id, local_run in runs:
            uptime = (now - local_run.started_at).total_seconds()
            snapshot = local_run.stats.snapshot_and_reset_window()
            payload = PipelineStatsPayload(
                worker_id=self._node_id,
                pipeline_name=local_run.pipeline_name,
                run_id=run_id,
                schedule_type=local_run.schedule_type,
                uptime_seconds=uptime,
                timestamp=now,
                is_final=False,
                **snapshot,
            )
            # Re-check under lock: _stream_worker finally removes from dict and
            # StatsStore atomically, so if the run_id is gone here the stream has
            # already stopped and we must not re-insert it.
            with self._local_stats_lock:
                if run_id in self._local_active_stats:
                    self._stats_store.update(payload)

    def _local_stats_loop(self, interval: int) -> None:
        while not self._local_stats_stop.wait(interval):
            try:
                self._emit_local_stats_once()
            except Exception:
                logger.exception("Error in local stats loop")

    # ── Kubernetes service lifecycle ───────────────────────────────────────

    def _get_dispatched_worker_ids(self, pipeline_name: str) -> list[str] | None:
        """Return worker_id list for count:N placements; None for count:all and workers.list.

        workers.list uses config.workers.worker_ids in _listed_worker_ids, so passing None here
        lets the service manager fall through to that path naturally.
        """
        pg_id = self._active_placement_group.get(pipeline_name)
        if pg_id is None:
            return None
        placement = self._broadcast_placements.get(pg_id)
        if placement is None:
            return None
        if placement.get("target_count") == "all":
            return None
        state = self.manager.get(pipeline_name)
        if state is not None and state.config.workers and state.config.workers.worker_ids is not None:
            return None  # workers.list: config path handles Endpoints
        return [s["worker_id"] for s in placement.get("slots", []) if s.get("worker_id")]

    def _activate_kubernetes_service(self, config: PipelineConfig) -> None:
        if self._kubernetes_service_manager is None:
            return
        try:
            dispatched_worker_ids = self._get_dispatched_worker_ids(config.name)
            self._kubernetes_service_manager.ensure_service(
                config, dispatched_worker_ids=dispatched_worker_ids
            )
        except Exception as exc:
            logger.warning(
                "Failed to reconcile pipeline Service on activation",
                extra={"pipeline": config.name, "error": str(exc)},
            )

    def _deactivate_kubernetes_service(self, config: PipelineConfig) -> None:
        if self._kubernetes_service_manager is None:
            return
        try:
            self._kubernetes_service_manager.delete_service(config)
        except Exception as exc:
            logger.warning(
                "Failed to reconcile pipeline Service on deactivation",
                extra={"pipeline": config.name, "error": str(exc)},
            )

    def reconcile_kubernetes_service(self, pipeline_name: str) -> None:
        if not self.manager.exists(pipeline_name):
            return
        self._activate_kubernetes_service(self.manager.get(pipeline_name).config)

    def get_active_broadcast_placements(self) -> list[dict]:
        return list(self._broadcast_placements.values())

    def on_pipeline_stats(self, payload) -> None:
        placement_group_id = self._active_placement_group.get(payload.pipeline_name)
        if placement_group_id is None:
            return
        placement = self._broadcast_placements.get(placement_group_id)
        if placement is None or placement.get("status") != "reconciling":
            return

        changed = False
        for slot in placement["slots"]:
            run_id_prefix = str(slot.get("run_id_prefix", ""))
            if payload.run_id != slot.get("current_run_id") and not payload.run_id.startswith(run_id_prefix):
                continue
            if slot.get("current_run_id") != payload.run_id:
                slot["current_run_id"] = payload.run_id
                if self._db is not None:
                    self._db.update_slot_run_id(
                        placement_group_id,
                        int(slot["worker_index"]),
                        payload.run_id,
                        status="running",
                        restart_count=int(slot.get("restart_count", 0)),
                    )
            if slot.get("status") != "running":
                slot["status"] = "running"
                changed = True

        self._sync_stream_run_ids_from_slots(payload.pipeline_name, placement["slots"])
        if all(slot.get("status") == "running" for slot in placement["slots"]):
            self._update_broadcast_placement_status(placement_group_id, "running")
        elif changed and self._db is not None:
            self._db.update_broadcast_placement_status(
                placement_group_id,
                placement["status"],
                slots=placement["slots"],
            )

    def redispatch_broadcast_slot(
        self,
        placement_group_id: str,
        worker_index: int,
        replacement_worker_url: str | None = None,
    ) -> bool:
        placement = self._broadcast_placements.get(placement_group_id)
        if placement is None or self._worker_pool is None:
            return False
        slot = next(
            (s for s in placement["slots"] if int(s.get("worker_index", -1)) == worker_index),
            None,
        )
        if slot is None or not self.manager.exists(placement["pipeline_name"]):
            return False
        state = self.manager.get(placement["pipeline_name"])
        restart_count = int(slot.get("restart_count", 0)) + 1
        new_run_id = f"{slot['run_id_prefix']}-r{restart_count}"
        pinned_worker_id = slot.get("pinned_worker_id")
        worker_url = replacement_worker_url
        if worker_url is None and pinned_worker_id:
            worker_url = self._worker_pool.url_for_worker_id(str(pinned_worker_id))
        if worker_url is None:
            worker_url = slot.get("worker_url")
        if not worker_url:
            return False
        callback_url = (
            f"{self._manager_url}/api/internal/run-complete"
            if self._manager_url else ""
        )
        if not self._worker_pool.dispatch_to_worker(
            worker_url=worker_url,
            run_id=new_run_id,
            pipeline_name=placement["pipeline_name"],
            yaml_text=state.yaml_text,
            schedule_type="stream",
            callback_url=callback_url,
        ):
            return False

        slot["current_run_id"] = new_run_id
        slot["worker_url"] = worker_url
        slot["worker_id"] = str(pinned_worker_id or self._worker_pool.worker_id_for_url(worker_url) or "")
        slot["dispatched_at"] = datetime.now(UTC).isoformat()
        slot["status"] = "running"
        slot["restart_count"] = restart_count
        self._sync_stream_run_ids_from_slots(placement["pipeline_name"], placement["slots"])
        if self._db is not None:
            self._db.update_broadcast_placement_status(
                placement_group_id,
                placement["status"],
                slots=placement["slots"],
            )
        return True
