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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from tram.core.context import RunStatus
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.manager import PipelineManager, PipelineState

if TYPE_CHECKING:
    from tram.agent.worker_pool import WorkerPool
    from tram.models.pipeline import PipelineConfig
    from tram.persistence.db import TramDB
    from tram.persistence.file_tracker import ProcessedFileTracker

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._db = db
        self._node_id = node_id
        self._worker_pool = worker_pool
        self._manager_url = manager_url

        self.manager = PipelineManager(db=db)
        self.executor = PipelineExecutor(file_tracker=file_tracker)

        self._scheduler = None          # APScheduler BackgroundScheduler
        self._stream_threads: dict[str, threading.Thread] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._thread_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="tram-batch")
        # Tracks run_id for dispatched stream pipelines: {pipeline_name: run_id}
        self._stream_run_ids: dict[str, str] = {}

        self._running = False

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
        logger.info("PipelineController started")

    def stop(self, timeout: int = 30) -> None:
        """Stop the controller and all running pipelines gracefully."""
        self._running = False
        logger.info("PipelineController stopping",
                    extra={"drain_timeout_seconds": timeout})

        # Signal dispatched streams to stop (manager+worker mode)
        for name, run_id in list(self._stream_run_ids.items()):
            if self._worker_pool:
                self._worker_pool.stop_run(run_id, name)
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

        for name, yaml_text in self._db.get_all_pipelines():
            try:
                config = load_pipeline_from_yaml(yaml_text)
                self.manager.register(config, yaml_text=yaml_text)
                if name in stopped_names:
                    self.manager.set_status(name, "stopped")
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
        from tram.pipeline.loader import load_pipeline_from_yaml

        config = load_pipeline_from_yaml(yaml_text)
        state = self.manager.get(name)
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

    def start_pipeline(self, name: str) -> None:
        """Start a stopped/errored pipeline. Clears stopped flag and schedules."""
        state = self.manager.get(name)
        if state.status == "running":
            logger.debug("start_pipeline: already running", extra={"pipeline": name})
            return

        if self._db is not None:
            self._db.start_pipeline_flag(name)

        if not state.config.enabled:
            self.manager.set_status(name, "stopped")
            return

        self._do_schedule(name)

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
        run_id = str(uuid.uuid4())[:8]
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

        # ── Manager+worker dispatch path ───────────────────────────────────
        if self._worker_pool is not None:
            if run_id is None:
                run_id = str(uuid.uuid4())[:8]
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
                logger.error("Batch dispatch failed: no healthy workers",
                             extra={"pipeline": pipeline_name, "run_id": run_id})
                self.manager.set_status(pipeline_name, "error")
            return
        # ── Local execution path ───────────────────────────────────────────

        try:
            state = self.manager.get(pipeline_name)
            result = self.executor.batch_run(state.config, run_id=run_id)
            self.manager.record_run(pipeline_name, result)
            self._on_run_complete(pipeline_name, result)
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

    def on_worker_run_complete(
        self,
        run_id: str,
        pipeline_name: str,
        status: str,
        records_in: int,
        records_out: int,
        records_skipped: int = 0,
        error: str | None = None,
        errors: list[str] | None = None,
    ) -> None:
        """Callback from a worker agent when a dispatched run finishes."""
        from tram.core.context import RunResult, RunStatus

        try:
            run_status = RunStatus(status)
        except ValueError:
            run_status = RunStatus.FAILED

        result = RunResult(
            run_id=run_id,
            pipeline_name=pipeline_name,
            status=run_status,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            records_in=records_in,
            records_out=records_out,
            records_skipped=records_skipped,
            error=error,
            node_id=self._node_id,
            errors=errors or [],
        )

        if self._worker_pool is not None:
            self._worker_pool.on_run_complete(run_id)

        if self.manager.exists(pipeline_name):
            self.manager.record_run(pipeline_name, result)
            self._on_run_complete(pipeline_name, result)
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
            run_id = str(uuid.uuid4())[:8]
            state = self.manager.get(config.name)
            callback_url = (
                f"{self._manager_url}/api/internal/run-complete"
                if self._manager_url else ""
            )
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
            self._stream_run_ids[config.name] = run_id
            self.manager.set_status(config.name, "running")
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
        logger.info("Started stream pipeline", extra={"pipeline": config.name})

    def _stream_worker(self, config: PipelineConfig, stop_event: threading.Event) -> None:
        try:
            self.executor.stream_run(config, stop_event)
        except Exception as exc:
            logger.error("Stream pipeline crashed",
                         extra={"pipeline": config.name, "error": str(exc)}, exc_info=True)
            self.manager.set_status(config.name, "error")
        finally:
            self._stream_threads.pop(config.name, None)
            self._stop_events.pop(config.name, None)
            if self.manager.exists(config.name):
                if self.manager.get(config.name).status == "running":
                    self.manager.set_status(config.name, "stopped")

    def _stop_stream(self, name: str, timeout: int = 10) -> None:
        # ── Manager+worker dispatch path ───────────────────────────────────
        if self._worker_pool is not None:
            run_id = self._stream_run_ids.pop(name, None)
            if run_id:
                self._worker_pool.stop_run(run_id, name)
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
        self.manager.set_status(name, "stopped")
