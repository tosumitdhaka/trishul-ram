"""TramScheduler — APScheduler for batch pipelines + thread pool for stream pipelines."""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Optional

from tram.core.context import RunStatus
from tram.pipeline.executor import PipelineExecutor

if TYPE_CHECKING:
    from tram.cluster.coordinator import ClusterCoordinator
    from tram.models.pipeline import PipelineConfig
    from tram.pipeline.manager import PipelineManager
    from tram.persistence.file_tracker import ProcessedFileTracker

logger = logging.getLogger(__name__)


class TramScheduler:
    """Manages lifecycle of batch and stream pipeline executions."""

    def __init__(
        self,
        manager: "PipelineManager",
        coordinator: Optional["ClusterCoordinator"] = None,
        rebalance_interval: int = 10,
        file_tracker: Optional["ProcessedFileTracker"] = None,
    ) -> None:
        self.manager = manager
        self._coordinator = coordinator
        self._rebalance_interval = rebalance_interval
        self.executor = PipelineExecutor(file_tracker=file_tracker)
        self._stream_threads: dict[str, threading.Thread] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._thread_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="tram-batch")
        self._scheduler = None
        self._running = False
        self._rebalance_stop = threading.Event()
        self._rebalance_thread: threading.Thread | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the APScheduler and register all enabled batch pipelines."""
        from apscheduler.schedulers.background import BackgroundScheduler

        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._running = True

        # Initial coordinator refresh so owns() reflects current topology
        if self._coordinator:
            self._coordinator.refresh()

        for state in self.manager.list_all():
            if state.config.enabled:
                self._schedule_pipeline(state.config)

        self._scheduler.start()

        # Start rebalance loop only in cluster mode
        if self._coordinator:
            self._rebalance_stop.clear()
            self._rebalance_thread = threading.Thread(
                target=self._rebalance_loop,
                name="tram-rebalance",
                daemon=True,
            )
            self._rebalance_thread.start()
            logger.info(
                "TramScheduler started (cluster mode)",
                extra={"rebalance_interval": self._rebalance_interval},
            )
        else:
            logger.info("TramScheduler started (standalone mode)")

    # ── Cluster rebalancing ────────────────────────────────────────────────

    def _rebalance_loop(self) -> None:
        """Background thread: poll for topology changes and rebalance ownership."""
        while not self._rebalance_stop.wait(self._rebalance_interval):
            try:
                if self._coordinator and self._coordinator.refresh():
                    self._rebalance()
            except Exception as exc:
                logger.error("Rebalance error", extra={"error": str(exc)})

    def _rebalance(self) -> None:
        """Re-evaluate pipeline ownership; start newly owned, stop released pipelines."""
        logger.info("Rebalancing pipeline ownership")
        for state in self.manager.list_all():
            config = state.config
            if not config.enabled or not self._coordinator:
                continue

            owns = self._coordinator.owns(config.name)
            sched_type = config.schedule.type

            if sched_type == "stream":
                is_running = config.name in self._stream_threads
                if owns and not is_running:
                    logger.info(
                        "Rebalance: acquiring stream pipeline",
                        extra={"pipeline": config.name},
                    )
                    self._start_stream(config)
                elif not owns and is_running:
                    logger.info(
                        "Rebalance: releasing stream pipeline",
                        extra={"pipeline": config.name},
                    )
                    self._stop_stream(config.name)
                    if self.manager.exists(config.name):
                        self.manager.set_status(config.name, "stopped")
            elif sched_type in ("interval", "cron"):
                job_id = f"batch-{config.name}"
                job_exists = (
                    self._scheduler is not None
                    and self._scheduler.get_job(job_id) is not None
                )
                if owns and not job_exists:
                    logger.info(
                        "Rebalance: acquiring batch pipeline",
                        extra={"pipeline": config.name},
                    )
                    self._schedule_pipeline(config)
                elif not owns and job_exists:
                    logger.info(
                        "Rebalance: releasing batch pipeline",
                        extra={"pipeline": config.name},
                    )
                    self._scheduler.remove_job(job_id)
                    self.manager.set_status(config.name, "stopped")

    def stop(self, timeout: int = 30) -> None:
        """Stop scheduler and all running stream/batch pipelines.

        Args:
            timeout: Seconds to wait for in-flight batch runs and stream threads to finish.
        """
        self._running = False
        logger.info("TramScheduler stopping", extra={"drain_timeout_seconds": timeout})

        # Stop rebalance loop first
        if self._rebalance_thread and self._rebalance_thread.is_alive():
            self._rebalance_stop.set()
            self._rebalance_thread.join(timeout=self._rebalance_interval + 2)

        # Signal all stream pipelines to stop
        for name in list(self._stop_events.keys()):
            self._stop_events[name].set()

        # Stop APScheduler (no new batch jobs dispatched after this)
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

        # Wait for in-flight batch runs (thread pool)
        self._thread_pool.shutdown(wait=True, cancel_futures=False)

        # Wait for stream threads to drain
        for name, thread in list(self._stream_threads.items()):
            if thread.is_alive():
                thread.join(timeout=timeout)
                if thread.is_alive():
                    logger.warning(
                        "Stream thread did not stop within timeout",
                        extra={"pipeline": name, "timeout_seconds": timeout},
                    )

        logger.info("TramScheduler stopped")

    # ── Pipeline scheduling ────────────────────────────────────────────────

    def _schedule_pipeline(self, config: "PipelineConfig") -> None:
        """Schedule a pipeline based on its schedule type."""
        # In cluster mode, only schedule pipelines owned by this node
        if self._coordinator and not self._coordinator.owns(config.name):
            logger.debug(
                "Pipeline not owned by this node — skipping",
                extra={"pipeline": config.name},
            )
            return

        sched_type = config.schedule.type

        if sched_type == "stream":
            self._start_stream(config)
        elif sched_type == "interval":
            self._add_interval_job(config)
        elif sched_type == "cron":
            self._add_cron_job(config)
        elif sched_type == "manual":
            logger.debug("Pipeline is manual — not scheduling", extra={"pipeline": config.name})

    def _add_interval_job(self, config: "PipelineConfig") -> None:
        from apscheduler.triggers.interval import IntervalTrigger

        job_id = f"batch-{config.name}"
        self._scheduler.add_job(
            func=self._run_batch,
            trigger=IntervalTrigger(seconds=config.schedule.interval_seconds),
            id=job_id,
            args=[config.name],
            max_instances=1,
            replace_existing=True,
            misfire_grace_time=60,
        )
        logger.info(
            "Scheduled interval pipeline",
            extra={"pipeline": config.name, "interval_seconds": config.schedule.interval_seconds},
        )

    def _add_cron_job(self, config: "PipelineConfig") -> None:
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
        logger.info(
            "Scheduled cron pipeline",
            extra={"pipeline": config.name, "cron": config.schedule.cron},
        )

    def _run_batch(self, pipeline_name: str, run_id: Optional[str] = None) -> None:
        """APScheduler job callback — runs one batch execution."""
        if not self.manager.exists(pipeline_name):
            logger.warning("Batch job: pipeline not found", extra={"pipeline": pipeline_name})
            return

        state = self.manager.get(pipeline_name)
        if state.status == "running":
            logger.warning(
                "Batch job: previous run still active, skipping",
                extra={"pipeline": pipeline_name},
            )
            return

        self.manager.set_status(pipeline_name, "running")
        try:
            result = self.executor.batch_run(state.config, run_id=run_id)
            self.manager.record_run(pipeline_name, result)
            if result.status == RunStatus.SUCCESS:
                # If the job is still scheduled, go back to "scheduled" not "stopped"
                job_id = f"batch-{pipeline_name}"
                has_job = self._scheduler and self._scheduler.get_job(job_id)
                final_status = "scheduled" if has_job else "stopped"
            else:
                final_status = "error"
            self.manager.set_status(pipeline_name, final_status)
        except Exception as exc:
            logger.error("Batch run exception", extra={"pipeline": pipeline_name, "error": str(exc)})
            self.manager.set_status(pipeline_name, "error")

    # ── Stream pipelines ───────────────────────────────────────────────────

    def _start_stream(self, config: "PipelineConfig") -> None:
        """Start a stream pipeline in a dedicated thread."""
        if config.name in self._stream_threads:
            old_thread = self._stream_threads[config.name]
            if old_thread.is_alive():
                # Previous stop hasn't propagated yet (blocking source); wait briefly.
                old_thread.join(timeout=15)
                if old_thread.is_alive():
                    logger.warning("Stream already running", extra={"pipeline": config.name})
                    return
            # Thread finished — clean up stale entries before starting fresh.
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

    def _stream_worker(self, config: "PipelineConfig", stop_event: threading.Event) -> None:
        """Thread target for stream pipeline execution."""
        try:
            self.executor.stream_run(config, stop_event)
        except Exception as exc:
            logger.error(
                "Stream pipeline crashed",
                extra={"pipeline": config.name, "error": str(exc)},
                exc_info=True,
            )
            self.manager.set_status(config.name, "error")
        finally:
            self._stream_threads.pop(config.name, None)
            self._stop_events.pop(config.name, None)
            if self.manager.exists(config.name):
                if self.manager.get(config.name).status == "running":
                    self.manager.set_status(config.name, "stopped")

    def _stop_stream(self, name: str, timeout: int = 10) -> None:
        stop_event = self._stop_events.get(name)
        if stop_event:
            stop_event.set()
        thread = self._stream_threads.get(name)
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        logger.info("Stopped stream pipeline", extra={"pipeline": name})

    # ── Runtime pipeline management ────────────────────────────────────────

    def start_pipeline(self, name: str) -> None:
        """Start a registered pipeline (called by REST/CLI)."""
        state = self.manager.get(name)
        config = state.config
        # Stream pipelines become "running" immediately (continuous thread).
        # Batch interval/cron pipelines show "scheduled" while waiting for next
        # fire — _run_batch() transitions them to "running" then back to "scheduled".
        # Manual pipelines stay "stopped" until explicitly triggered.
        if config.schedule.type not in ("stream", "manual"):
            self.manager.set_status(name, "scheduled")
        self._schedule_pipeline(config)

    def stop_pipeline(self, name: str) -> None:
        """Stop a running pipeline."""
        sched_type = self.manager.get(name).config.schedule.type

        if sched_type == "stream":
            self._stop_stream(name)
        else:
            job_id = f"batch-{name}"
            if self._scheduler and self._scheduler.get_job(job_id):
                self._scheduler.remove_job(job_id)

        self.manager.set_status(name, "stopped")

    def trigger_run(self, name: str) -> str:
        """Trigger an immediate run of a batch pipeline (manual trigger).

        Returns the pre-generated run_id so callers can poll for results.
        """
        state = self.manager.get(name)
        if state.config.schedule.type == "stream":
            raise ValueError(f"Pipeline '{name}' is a stream pipeline — cannot trigger manually")
        run_id = str(uuid.uuid4())[:8]
        self._thread_pool.submit(self._run_batch, name, run_id)
        return run_id

    def get_status(self) -> dict:
        """Return scheduler status summary."""
        next_runs = []
        if self._scheduler:
            for job in self._scheduler.get_jobs():
                next_run = job.next_run_time
                next_runs.append({
                    "pipeline": job.id.removeprefix("batch-"),
                    "next_run": next_run.isoformat() if next_run else None,
                })

        return {
            "scheduler_running": self._running,
            "active_streams": list(self._stream_threads.keys()),
            "scheduled_jobs": next_runs,
        }
