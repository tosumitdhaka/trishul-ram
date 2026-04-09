"""PipelineController — single authority for all pipeline lifecycle operations.

Replaces the split between TramScheduler and PipelineManager with one class
that owns every state transition.  All roads lead here — no code path schedules,
stops, or reassigns a pipeline without going through this controller.

State machine
─────────────
  scheduled → running  (APScheduler fires / stream starts)
  running   → scheduled (batch success on interval/cron pipeline)
  running   → stopped   (batch success on manual pipeline, or explicit stop())
  running   → error     (run failed)
  stopped   → scheduled (start() called by user)
  error     → scheduled (start() called by user)
  *         → deleted   (delete())

Guards enforced at every transition
────────────────────────────────────
  _may_schedule(name): stopped flag · YAML enabled · stale-run · ownership
  _claim_run(name):    DB CAS — prevents dual execution across pods
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
    from tram.cluster.coordinator import ClusterCoordinator
    from tram.models.pipeline import PipelineConfig
    from tram.persistence.db import TramDB
    from tram.persistence.file_tracker import ProcessedFileTracker

logger = logging.getLogger(__name__)

# How long a 'running' status written by a dead node is considered stale
_DEFAULT_STALE_RUN_SECONDS = 300


class PipelineController:
    """Single authority for all pipeline lifecycle operations."""

    def __init__(
        self,
        db: TramDB | None = None,
        coordinator: ClusterCoordinator | None = None,
        file_tracker: ProcessedFileTracker | None = None,
        node_id: str = "",
        rebalance_interval: int = 10,
        pipeline_sync_interval: int = 30,
        rebalance_cool_seconds: int = 20,
        stale_run_seconds: int = _DEFAULT_STALE_RUN_SECONDS,
    ) -> None:
        self._db = db
        self._coordinator = coordinator
        self._node_id = node_id
        self._rebalance_interval = rebalance_interval
        self._pipeline_sync_interval = pipeline_sync_interval
        self._rebalance_cool_seconds = rebalance_cool_seconds
        self._stale_run_seconds = stale_run_seconds

        self.manager = PipelineManager(db=db)
        self.executor = PipelineExecutor(file_tracker=file_tracker)

        self._scheduler = None          # APScheduler BackgroundScheduler
        self._stream_threads: dict[str, threading.Thread] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._thread_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="tram-batch")

        self._running = False
        self._rebalance_stop = threading.Event()
        self._rebalance_thread: threading.Thread | None = None
        self._sync_stop = threading.Event()
        self._sync_thread: threading.Thread | None = None

        # Tracks when a new node joined; used by cooling-period drain logic
        # {node_id: datetime}
        self._node_join_times: dict[str, datetime] = {}
        # Tracks all nodes ever observed alive; prevents stable nodes from being
        # re-detected as "newly joined" on every rebalance tick after drain completes
        self._seen_nodes: set[str] = {self._node_id}

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the controller: load pipelines from DB, schedule owned ones."""
        from apscheduler.schedulers.background import BackgroundScheduler

        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._running = True

        if self._coordinator:
            self._coordinator.refresh()

        # Load all pipelines from DB into in-memory registry
        if self._db is not None:
            self._boot_load()

        self._scheduler.start()

        if self._coordinator:
            self._rebalance_stop.clear()
            self._rebalance_thread = threading.Thread(
                target=self._rebalance_loop,
                name="tram-rebalance",
                daemon=True,
            )
            self._rebalance_thread.start()
            logger.info("PipelineController started (cluster mode)",
                        extra={"rebalance_interval": self._rebalance_interval})
        else:
            logger.info("PipelineController started (standalone mode)")

        if self._db is not None:
            self._sync_stop.clear()
            self._sync_thread = threading.Thread(
                target=self._sync_loop,
                name="tram-pipeline-sync",
                daemon=True,
            )
            self._sync_thread.start()
            logger.info("Pipeline DB sync started",
                        extra={"interval_seconds": self._pipeline_sync_interval})

    def stop(self, timeout: int = 30) -> None:
        """Stop the controller and all running pipelines gracefully."""
        self._running = False
        logger.info("PipelineController stopping",
                    extra={"drain_timeout_seconds": timeout})

        if self._rebalance_thread and self._rebalance_thread.is_alive():
            self._rebalance_stop.set()
            self._rebalance_thread.join(timeout=self._rebalance_interval + 2)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_stop.set()
            self._sync_thread.join(timeout=self._pipeline_sync_interval + 2)

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
        """Load all pipelines from DB and schedule owned, non-stopped ones.

        Fixed ordering that eliminates the startup race in the old design:
          1. Load YAML + parse configs
          2. Register in-memory (all start as 'stopped')
          3. Apply stopped flags from DB
          4. Resolve ownership: owned → schedule; unassigned → claim; foreign → status from DB
        """
        from tram.pipeline.loader import load_pipeline_from_yaml

        runtime_rows = {r["name"]: r for r in self._db.get_all_pipeline_runtime()}
        pipeline_rows = self._db.get_all_pipelines()          # [(name, yaml_text)]
        stopped_names = set(self._db.get_stopped_pipeline_names())

        # Step 1+2: parse + register
        for name, yaml_text in pipeline_rows:
            try:
                config = load_pipeline_from_yaml(yaml_text)
                self.manager.register(config, yaml_text=yaml_text)
            except Exception as exc:
                logger.warning("Boot: failed to load pipeline",
                               extra={"pipeline": name, "error": str(exc)})
                continue

        # Recompute ownership with full pipeline set before scheduling anything
        if self._coordinator:
            all_names = [s.config.name for s in self.manager.list_all()]
            self._coordinator.rebalance_ownership(all_names)

        pipeline_counts = self._db.get_pipeline_counts_by_node() if self._db else {}

        # Step 3+4: apply flags, resolve ownership, schedule
        for state in self.manager.list_all():
            name = state.config.name
            rt = runtime_rows.get(name, {})
            owner = rt.get("owner_node", "")

            # Apply stopped flag first — highest priority
            if name in stopped_names:
                self.manager.set_status(name, "stopped")
                continue

            # Determine ownership
            if owner == self._node_id:
                # We owned this before restart — re-acquire
                self._do_schedule(name)

            elif owner == "":
                # Unassigned — claim if we're least loaded
                best = (self._coordinator.least_loaded_node(pipeline_counts, exclude="")
                        if self._coordinator else self._node_id)
                if best == self._node_id:
                    if self._db:
                        self._db.set_pipeline_owner(name, self._node_id)
                    pipeline_counts[self._node_id] = pipeline_counts.get(self._node_id, 0) + 1
                    self._do_schedule(name)
                else:
                    # Another node will claim it
                    self.manager.set_status(name, rt.get("runtime_status", "stopped"))

            elif self._coordinator and not self._coordinator.is_node_alive(owner):
                # Owner is dead — treat as orphan, will be handled by first rebalance tick
                self.manager.set_status(name, "stopped")

            else:
                # Owned by another live node — reflect DB status for UI display
                self.manager.set_status(name, rt.get("runtime_status", "scheduled"))

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
            # Assign to least-loaded node
            pipeline_counts = self._db.get_pipeline_counts_by_node()
            owner = (self._coordinator.least_loaded_node(pipeline_counts)
                     if self._coordinator else self._node_id)
            self._db.set_pipeline_owner(config.name, owner)
        else:
            owner = self._node_id

        if owner == self._node_id and config.enabled:
            self._do_schedule(config.name)

        logger.info("Registered pipeline", extra={"pipeline": config.name, "owner": owner})
        return state

    def update(self, name: str, yaml_text: str) -> PipelineState:
        """Update an existing pipeline's YAML. Restarts if it was running/scheduled."""
        from tram.pipeline.loader import load_pipeline_from_yaml

        config = load_pipeline_from_yaml(yaml_text)
        state = self.manager.get(name)
        was_active = state.status in ("scheduled", "running")

        # Stop current execution cleanly
        self._stop_execution(name)

        # Re-register with new config
        self.manager.deregister(name)
        new_state = self.manager.register(config, yaml_text=yaml_text)

        if self._db is not None:
            self._db.save_pipeline(name, yaml_text, source="api")
            self._db.set_runtime_status(name, "stopped", self._node_id)

        # Restart if it was active and we own it
        if was_active and config.enabled and self._may_schedule(name):
            self._do_schedule(name)

        logger.info("Updated pipeline", extra={"pipeline": name})
        return new_state

    def delete(self, name: str) -> None:
        """Stop, deregister, and soft-delete a pipeline."""
        # Always stop regardless of current status (clears orphan APScheduler jobs too)
        self._stop_execution(name)
        self.manager.deregister(name)
        if self._db is not None:
            self._db.delete_pipeline(name)
            self._db.set_pipeline_owner(name, "")
        logger.info("Deleted pipeline", extra={"pipeline": name})

    def start_pipeline(self, name: str) -> None:
        """Start a stopped/errored pipeline. Clears stopped flag and schedules."""
        state = self.manager.get(name)
        if state.status == "running":
            logger.debug("start_pipeline: already running", extra={"pipeline": name})
            return

        # Clear stopped flag in DB — pipeline is now free to be scheduled
        if self._db is not None:
            self._db.start_pipeline_flag(name)

        if not state.config.enabled:
            self.manager.set_status(name, "stopped")
            return

        # In cluster mode, only schedule if this node owns the pipeline
        if self._coordinator and not self._coordinator.owns(name):
            self.manager.set_status(name, "scheduled")
            return

        self._do_schedule(name)

    def stop_pipeline(self, name: str) -> None:
        """Stop a pipeline and mark it so it won't auto-restart."""
        self._stop_execution(name)
        if self._db is not None:
            self._db.stop_pipeline(name)
            self._db.set_runtime_status(name, "stopped", self._node_id)
        self.manager.set_status(name, "stopped")
        logger.info("Stopped pipeline", extra={"pipeline": name})

    def trigger_run(self, name: str) -> str:
        """Immediate one-shot run. Returns run_id. Blocked if pipeline is stopped or running."""
        if self._db is not None and self._db.is_pipeline_stopped(name):
            raise ValueError(f"Pipeline '{name}' is stopped — start it before triggering")
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
        return {
            "scheduler_running": self._running,
            "active_streams": list(self._stream_threads.keys()),
            "scheduled_jobs": next_runs,
        }

    # ── Scheduling gate ────────────────────────────────────────────────────

    def _may_schedule(self, name: str) -> bool:
        """Single gate — all scheduling paths call this.

        Returns True only when ALL conditions are met:
          1. Not explicitly stopped by user (DB flag)
          2. YAML config.enabled = true
          3. No other pod is currently running it (or stale run detected)
          4. In cluster mode: this node owns the pipeline
        """
        state = self.manager.get(name) if self.manager.exists(name) else None
        if state is None:
            return False

        # Guard 1: user explicitly stopped
        if self._db and self._db.is_pipeline_stopped(name):
            return False

        # Guard 2: YAML disabled
        if not state.config.enabled:
            return False

        # Guard 3: stale-run check (cross-pod crash recovery)
        if self._db:
            rt = self._db.get_pipeline_runtime(name)
            if rt and rt["runtime_status"] == "running":
                node_alive = (self._coordinator.is_node_alive(rt["status_node"])
                              if self._coordinator else False)
                status_updated = rt["status_updated"]
                age = (datetime.now(UTC) - status_updated).total_seconds() if status_updated else 0
                interval = getattr(state.config.schedule, "interval_seconds", None)
                threshold = max(self._stale_run_seconds,
                                interval * 2 if interval else self._stale_run_seconds)
                if node_alive or age < threshold:
                    return False  # genuinely running on another pod

        # Guard 4: cluster ownership
        if self._coordinator and not self._coordinator.owns(name):
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
        if self._db:
            self._db.set_runtime_status(config.name, "scheduled", self._node_id)
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
        if self._db:
            self._db.set_runtime_status(config.name, "scheduled", self._node_id)
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

        # Cross-pod CAS: only one pod runs this at a time
        if self._db is not None:
            claimed = self._db.claim_run(pipeline_name, self._node_id)
            if not claimed:
                logger.debug("Batch job: another pod claimed this run — skipping",
                             extra={"pipeline": pipeline_name})
                return
        else:
            # Standalone: check in-memory status
            state = self.manager.get(pipeline_name)
            if state.status == "running":
                logger.warning("Batch job: previous run still active, skipping",
                               extra={"pipeline": pipeline_name})
                return

        self.manager.set_status(pipeline_name, "running")

        try:
            state = self.manager.get(pipeline_name)
            result = self.executor.batch_run(state.config, run_id=run_id)
            self.manager.record_run(pipeline_name, result)
            self._on_run_complete(pipeline_name, result)
        except Exception as exc:
            logger.error("Batch run exception",
                         extra={"pipeline": pipeline_name, "error": str(exc)})
            self.manager.set_status(pipeline_name, "error")
            if self._db:
                self._db.set_runtime_status(pipeline_name, "error", self._node_id)

    def _on_run_complete(self, pipeline_name: str, result) -> None:
        """Post-run state transition — called after every batch run completes."""
        # Never overwrite a manually-stopped status set during the run
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
                # Manual trigger on a scheduled pipeline (non-owning node or no job yet)
                if self._may_schedule(pipeline_name):
                    self._do_schedule(pipeline_name)
                    return
                else:
                    final_status = "scheduled"  # owned elsewhere
            else:
                final_status = "stopped"
        else:
            final_status = "error"

        self.manager.set_status(pipeline_name, final_status)
        if self._db:
            self._db.set_runtime_status(pipeline_name, final_status, self._node_id)

    # ── Stream execution ───────────────────────────────────────────────────

    def _start_stream(self, config: PipelineConfig) -> None:
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
        if self._db:
            self._db.set_runtime_status(config.name, "running", self._node_id)
        thread.start()
        logger.info("Started stream pipeline", extra={"pipeline": config.name})

    def _stream_worker(self, config: PipelineConfig, stop_event: threading.Event) -> None:
        try:
            self.executor.stream_run(config, stop_event)
        except Exception as exc:
            logger.error("Stream pipeline crashed",
                         extra={"pipeline": config.name, "error": str(exc)}, exc_info=True)
            self.manager.set_status(config.name, "error")
            if self._db:
                self._db.set_runtime_status(config.name, "error", self._node_id)
        finally:
            self._stream_threads.pop(config.name, None)
            self._stop_events.pop(config.name, None)
            if self.manager.exists(config.name):
                if self.manager.get(config.name).status == "running":
                    self.manager.set_status(config.name, "stopped")
                    if self._db:
                        self._db.set_runtime_status(config.name, "stopped", self._node_id)

    def _stop_stream(self, name: str, timeout: int = 10) -> None:
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

    # ── DB sync loop ───────────────────────────────────────────────────────

    def _sync_loop(self) -> None:
        while not self._sync_stop.wait(self._pipeline_sync_interval):
            try:
                self._sync_from_db()
            except Exception as exc:
                logger.error("Pipeline DB sync error", extra={"error": str(exc)})

    def _sync_from_db(self) -> None:
        """Register new DB pipelines; re-register updated ones; deregister deleted ones."""
        if self._db is None:
            return
        from tram.pipeline.loader import load_pipeline_from_yaml

        stopped_names = set(self._db.get_stopped_pipeline_names())
        pipeline_counts = self._db.get_pipeline_counts_by_node()
        changed = False
        new_configs = []

        for name, yaml_text in self._db.get_all_pipelines():
            if self.manager.exists(name):
                state = self.manager.get(name)
                if state.yaml_text == yaml_text:
                    # YAML unchanged — but check if stopped-flag state changed.
                    # This is the path hit when user clicks Start/Stop on a pipeline
                    # owned by a different pod than the one that received the API call.
                    is_stopped_in_db = name in stopped_names
                    if state.status == "stopped" and not is_stopped_in_db:
                        # Stopped flag was cleared (user clicked Start) — re-evaluate
                        new_configs.append(state.config)
                    elif state.status not in ("stopped", "error") and is_stopped_in_db:
                        # Stopped flag was set while pipeline was running/scheduled
                        self._stop_execution(name)
                        self.manager.set_status(name, "stopped")
                    continue
                # YAML changed on another node — defer if actively running
                if state.status == "running":
                    logger.debug("Sync: deferring re-registration (pipeline is running)",
                                 extra={"pipeline": name})
                    continue
                try:
                    config = load_pipeline_from_yaml(yaml_text)
                    self._stop_execution(name)
                    self.manager.deregister(name)
                    self.manager.register(config, yaml_text=yaml_text)
                    # Restore stopped flag immediately after re-register
                    if name in stopped_names:
                        self.manager.set_status(name, "stopped")
                    else:
                        new_configs.append(config)
                    changed = True
                    logger.info("Sync: re-registered updated pipeline", extra={"pipeline": name})
                except Exception as exc:
                    logger.warning("Sync: failed to re-register",
                                   extra={"pipeline": name, "error": str(exc)})
                continue

            try:
                config = load_pipeline_from_yaml(yaml_text)
                self.manager.register(config, yaml_text=yaml_text)
                changed = True
                if name in stopped_names:
                    self.manager.set_status(name, "stopped")
                else:
                    new_configs.append(config)
                logger.info("Sync: registered pipeline from DB", extra={"pipeline": name})
            except Exception as exc:
                logger.warning("Sync: failed to register",
                               extra={"pipeline": name, "error": str(exc)})

        # Deregister soft-deleted pipelines
        for name in self._db.get_deleted_pipeline_names():
            if not self.manager.exists(name):
                continue
            try:
                self._stop_execution(name)
                self.manager.deregister(name)
                changed = True
                logger.info("Sync: deregistered deleted pipeline", extra={"pipeline": name})
            except Exception as exc:
                logger.warning("Sync: failed to deregister",
                               extra={"pipeline": name, "error": str(exc)})

        if changed and self._coordinator:
            all_names = [s.config.name for s in self.manager.list_all()]
            self._coordinator.rebalance_ownership(all_names)

        # Schedule newly discovered pipelines
        for config in new_configs:
            if self._may_schedule(config.name):
                # Assign owner if unassigned
                if self._db:
                    owner = self._db.get_pipeline_owner(config.name)
                    if not owner:
                        best = (self._coordinator.least_loaded_node(pipeline_counts)
                                if self._coordinator else self._node_id)
                        self._db.set_pipeline_owner(config.name, best)
                        pipeline_counts[best] = pipeline_counts.get(best, 0) + 1
                        owner = best
                    if owner == self._node_id:
                        self._do_schedule(config.name)
                    else:
                        self.manager.set_status(config.name, "scheduled")
                else:
                    self._do_schedule(config.name)

    # ── Rebalance loop ─────────────────────────────────────────────────────

    def _rebalance_loop(self) -> None:
        while not self._rebalance_stop.wait(self._rebalance_interval):
            try:
                if self._coordinator and self._coordinator.refresh():
                    self._rebalance()
            except Exception as exc:
                logger.error("Rebalance error", extra={"error": str(exc)})

    def _rebalance(self) -> None:
        """Selective rebalance — only touches pipelines that need a new owner."""
        if not self._coordinator or self._db is None:
            return

        live_nodes = set(self._coordinator.live_node_ids())
        all_names = [s.config.name for s in self.manager.list_all()]
        self._coordinator.rebalance_ownership(all_names)

        pipeline_counts = self._db.get_pipeline_counts_by_node()

        # ── Event A: recover orphaned pipelines from dead nodes ────────────
        dead_nodes = self._get_newly_dead_nodes(live_nodes)
        for dead_node in dead_nodes:
            self._seen_nodes.discard(dead_node)  # allow re-detection when it rejoins
            orphans = self._db.get_pipelines_by_owner(dead_node)
            for name in orphans:
                if not self.manager.exists(name):
                    continue
                # Mark crashed run as error if it was running
                rt = self._db.get_pipeline_runtime(name)
                if rt and rt["runtime_status"] == "running":
                    self.manager.set_status(name, "error")
                    self._db.set_runtime_status(name, "error", self._node_id)
                    logger.warning("Rebalance: marking crashed run as error",
                                   extra={"pipeline": name, "dead_node": dead_node})

                best = self._coordinator.least_loaded_node(pipeline_counts, exclude=dead_node)
                self._db.set_pipeline_owner(name, best)
                pipeline_counts[best] = pipeline_counts.get(best, 0) + 1
                logger.info("Rebalance: reassigned orphan",
                            extra={"pipeline": name, "from": dead_node, "to": best})

                if best == self._node_id:
                    if self._may_schedule(name):
                        self._do_schedule(name)

        # ── Event B: new node joined — after cooling period, voluntary drain ─
        new_nodes = self._get_newly_joined_nodes(live_nodes)
        for node in new_nodes:
            self._node_join_times[node] = datetime.now(UTC)
            logger.info("Rebalance: new node joined — cooling period started",
                        extra={"node": node, "cool_seconds": self._rebalance_cool_seconds})

        now = datetime.now(UTC)
        for node, join_time in list(self._node_join_times.items()):
            if node not in live_nodes:
                del self._node_join_times[node]
                continue
            elapsed = (now - join_time).total_seconds()
            if elapsed < self._rebalance_cool_seconds:
                continue
            # Cooling period elapsed — voluntary drain of scheduled (non-running, non-stream)
            del self._node_join_times[node]
            self._voluntary_drain(node, pipeline_counts)

        # ── Event C: this node's own ownership may have changed ────────────
        # Acquire newly owned pipelines; release pipelines no longer owned
        for state in self.manager.list_all():
            name = state.config.name
            if state.status == "stopped":
                continue

            owner = self._db.get_pipeline_owner(name) if self._db else self._node_id
            owns_it = (owner == self._node_id)
            sched_type = state.config.schedule.type

            if sched_type == "stream":
                is_running = name in self._stream_threads
                if owns_it and not is_running and self._may_schedule(name):
                    logger.info("Rebalance: acquiring stream pipeline", extra={"pipeline": name})
                    self._start_stream(state.config)
                elif not owns_it and is_running:
                    logger.info("Rebalance: releasing stream pipeline", extra={"pipeline": name})
                    self._stop_stream(name)
                    self.manager.set_status(name, "scheduled")
            elif sched_type in ("interval", "cron"):
                job_id = f"batch-{name}"
                has_job = self._scheduler and self._scheduler.get_job(job_id) is not None
                if owns_it and not has_job and self._may_schedule(name):
                    logger.info("Rebalance: acquiring batch pipeline", extra={"pipeline": name})
                    self._do_schedule(name)
                elif not owns_it and has_job:
                    logger.info("Rebalance: releasing batch pipeline", extra={"pipeline": name})
                    self._scheduler.remove_job(job_id)
                    self.manager.set_status(name, "scheduled")

    def _voluntary_drain(self, new_node: str, pipeline_counts: dict[str, int]) -> None:
        """After cooling period: overloaded nodes voluntarily give scheduled pipelines
        to the new node. Only moves pipelines in 'scheduled' state (never running/stream)."""
        live = self._coordinator.live_node_ids()
        n = len(live)
        if n == 0:
            return

        total = sum(pipeline_counts.values())
        target = -(-total // n)  # ceil division

        for state in self.manager.list_all():
            name = state.config.name
            owner = self._db.get_pipeline_owner(name) if self._db else ""

            if owner != self._node_id:
                continue
            if pipeline_counts.get(self._node_id, 0) <= target:
                break
            # Only volunteer scheduled pipelines — never interrupt running or streams
            if state.status != "scheduled":
                continue
            if state.config.schedule.type == "stream":
                continue

            # Hand off to new node
            self._db.set_pipeline_owner(name, new_node)
            pipeline_counts[self._node_id] = pipeline_counts.get(self._node_id, 1) - 1
            pipeline_counts[new_node] = pipeline_counts.get(new_node, 0) + 1

            # Remove our APScheduler job — new node will pick it up on next sync
            job_id = f"batch-{name}"
            if self._scheduler and self._scheduler.get_job(job_id):
                self._scheduler.remove_job(job_id)
            self.manager.set_status(name, "scheduled")

            logger.info("Voluntary drain: transferred pipeline",
                        extra={"pipeline": name, "to": new_node})

    def _get_newly_dead_nodes(self, live_nodes: set[str]) -> list[str]:
        """Return nodes that have pipelines in DB but are no longer live."""
        if self._db is None:
            return []
        counts = self._db.get_pipeline_counts_by_node()
        return [node for node in counts if node and node not in live_nodes]

    def _get_newly_joined_nodes(self, live_nodes: set[str]) -> list[str]:
        """Return live nodes not previously seen.

        Uses a persistent _seen_nodes set so stable live nodes are never
        re-detected as 'newly joined' after their cooling-period drain completes.
        Dead nodes are removed from _seen_nodes in Event A so they are correctly
        re-detected when they rejoin.
        """
        new = [n for n in live_nodes
               if n not in self._seen_nodes and n not in self._node_join_times]
        self._seen_nodes.update(live_nodes)
        return new
