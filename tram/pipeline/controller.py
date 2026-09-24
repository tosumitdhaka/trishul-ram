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

import hashlib
import logging
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Literal

from tram.core.context import RunResult, RunStatus
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.manager import PipelineManager, PipelineState

if TYPE_CHECKING:
    from tram.agent.metrics import PipelineStats
    from tram.agent.worker_pool import WorkerPool
    from tram.models.pipeline import PipelineConfig, WorkersConfig
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


@dataclass
class TriggerResult:
    """Result of a manual-run trigger (E.2 / GH #21).

    ``disposition == "dispatched"`` is today's async submit; ``"queued"``
    means the run was durably enqueued because no healthy worker existed
    (or an existing queued run was returned — dedupe).
    """

    run_id: str
    disposition: Literal["dispatched", "queued"]


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
        single_stream_placements: bool | None = None,
        queue_manual_runs: bool | None = None,
        queue_ttl_seconds: int = 900,
        stateful_transforms: bool | None = None,
    ) -> None:
        self._db = db
        self._node_id = node_id
        self._worker_pool = worker_pool
        self._manager_url = manager_url
        self._stats_store = stats_store
        self._kubernetes_service_manager = kubernetes_service_manager
        # D.2 feature flag (GH #17): route count=1 stream dispatch through the
        # durable broadcast-placement machinery. Default ON ("1"); "0" keeps the
        # legacy count=1 single-dispatch path verbatim. Read once at construction.
        if single_stream_placements is None:
            raw_flag = os.environ.get("TRAM_STREAM_SINGLE_PLACEMENT", "1")
            self._single_stream_placements = raw_flag != "0"
            if raw_flag not in ("0", "1"):
                # Fail open: anything other than an explicit "0" enables the
                # durable-placement path. A typo'd value is loud here instead
                # of silently flipping a deployment's stream semantics.
                logger.warning(
                    "Unrecognized TRAM_STREAM_SINGLE_PLACEMENT value — "
                    'treating as enabled ("1")',
                    extra={"value": raw_flag},
                )
        else:
            self._single_stream_placements = single_stream_placements

        # E.2 feature flag (GH #21): queue manual runs dispatched when no
        # healthy worker exists. Default ON ("1"); "0" keeps the legacy
        # fail-fast no-capacity path verbatim. Read once at construction,
        # mirroring the TRAM_STREAM_SINGLE_PLACEMENT pattern above.
        if queue_manual_runs is None:
            raw_flag = os.environ.get("TRAM_QUEUE_MANUAL_RUNS", "1")
            self._queue_manual_runs = raw_flag != "0"
            if raw_flag not in ("0", "1"):
                # Fail open: anything other than an explicit "0" enables the
                # queue. A typo'd value is loud here instead of silently
                # flipping a deployment's manual-run semantics.
                logger.warning(
                    "Unrecognized TRAM_QUEUE_MANUAL_RUNS value — "
                    'treating as enabled ("1")',
                    extra={"value": raw_flag},
                )
        else:
            self._queue_manual_runs = queue_manual_runs
        # How long a queued run waits for capacity before it expires to a
        # FAILED run-history row (absolute expires_at keeps the clock running
        # across manager restarts — Decision 4).
        self._queue_ttl_seconds = max(1, queue_ttl_seconds)

        # F.1 feature flag (GH #W-5.1): stateful transforms. Read once at
        # construction; the flag gates the validation rejections (models), the
        # internal endpoints (routers) and — next step — the _start_stream
        # broadcast guard. Fail-open parse mirrors the D.2/E.2 flags.
        if stateful_transforms is None:
            from tram.core.config import stateful_transforms_enabled
            self._stateful_transforms = stateful_transforms_enabled()
        else:
            self._stateful_transforms = stateful_transforms

        self.manager = PipelineManager(db=db)
        # Standalone execution reaches the transform-state blob via the DB
        # directly; worker mode uses the internal HTTP endpoints instead
        # (design F.1 §3.2b).
        if db is not None:
            from tram.pipeline.state_store import DbTransformStateStore
            state_store = DbTransformStateStore(db)
        else:
            state_store = None
        self.executor = PipelineExecutor(file_tracker=file_tracker, state_store=state_store)

        # Controller-level lifecycle lock. RLock (not Lock) because lifecycle
        # helpers call each other on the same thread (update() -> _stop_execution()
        # -> _stop_stream(); _finalize_batch_result() -> _on_run_complete() ->
        # _do_schedule(); register()/update() -> _do_schedule() -> _start_stream()).
        # It serializes lifecycle transitions (trigger/update/delete/start/stop)
        # against each other and against the check-then-act running-claim in
        # _run_batch() plus the status-read paths that race them (code review
        # findings B1 trigger TOCTOU, B2 CRUD deregister->register window, B10
        # manager thread-safety gap).
        self._lock = threading.RLock()

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
        """Stop the controller and all running pipelines gracefully.

        The lifecycle lock is taken only for the quick state mutations below;
        scheduler shutdown, the batch thread-pool drain, and stream-thread joins
        run WITHOUT the lock. Holding it across ``_thread_pool.shutdown(wait=True)``
        would deadlock: a queued ``_run_batch`` blocks on the lock in its claim
        phase while stop() waits for that task to finish (and stream threads
        need the lock in their ``finally`` cleanup, so joins must also be
        lock-free).
        """
        self._running = False
        self._local_stats_stop.set()
        logger.info("PipelineController stopping",
                    extra={"drain_timeout_seconds": timeout})

        with self._lock:
            # On manager shutdown, keep worker-side streams alive so placement
            # reconciliation can restore state after restart. Manual stop/delete paths
            # still stop workers explicitly.
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

        with self._lock:
            # E.2 (§7): a crash mid-claim leaves queued rows stuck at
            # 'dispatching' — nothing is in flight at boot, so reset them to
            # 'queued' before anything can observe the queue. The BatchReconciler
            # (started by the app) drains within one interval; past-TTL rows
            # expire on its first pass (Decision 4).
            if self._db is not None:
                self._db.reset_dispatching_queued_runs()
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
                        if self._adopt_live_stream_if_any(config):
                            continue
                        self._do_schedule(name)
                except Exception as exc:
                    logger.warning("Boot: failed to load pipeline",
                                   extra={"pipeline": name, "error": str(exc)})

    def _adopt_live_stream_if_any(self, config: PipelineConfig) -> bool:
        """B.6 boot guard: adopt-or-skip for count=1 streams on manager restart.

        Streams deliberately survive manager restarts on the workers (original
        behavior): ``_boot_load`` re-schedules an enabled count=1 stream while
        the worker still runs the pre-restart instance, dispatching a second
        concurrent instance → duplicate sink writes. When a worker currently
        reports the pipeline as a live stream, adopt the existing run instead —
        record the run/lease so status and placement views (stop_run(),
        run-complete cleanup, cluster node view) are correct — and skip
        dispatch. Full durable-record reconciliation is Wave D.2; this guard is
        minimal and non-invasive.

        The live-run probe is worker HTTP I/O but intentionally runs under the
        lifecycle lock: adoption mutates controller/worker-pool shared state and
        must be atomic against a concurrent delete()/_stop_stream(). Same
        trade-off as the dispatch calls held under the lock in _start_stream();
        boot happens once.
        """
        if self._worker_pool is None:
            return False
        if config.schedule.type != "stream":
            return False
        workers_cfg = config.workers
        if self._is_broadcast_workers(workers_cfg):
            # Broadcast placements are durable and restored by _boot_load via
            # _restore_broadcast_placement; only the single-dispatch count=1
            # path lacks a durable record and needs the live-run guard.
            return False

        matches = self._worker_pool.find_pipeline_runs(config.name, schedule_type="stream")
        if not matches:
            return False
        adopted = min(matches, key=lambda item: str(item.get("started_at") or ""))
        if self._single_stream_placements:
            # D.2 migration bridge: materialize a 1-slot placement row from the
            # worker-reported live run so the stream joins the durable-placement
            # regime without a restart (design §5.2).
            self._materialize_placement_from_adoption(config, adopted)
            self.manager.set_status(config.name, "running")
            self._activate_kubernetes_service(config)
            logger.info(
                "Boot: materialized placement from live stream run",
                extra={
                    "pipeline": config.name,
                    "worker": str(adopted["worker_url"]),
                    "run_id": str(adopted["run_id"]),
                },
            )
            return True
        run_id = str(adopted["run_id"])
        worker_url = str(adopted["worker_url"])
        self._stream_run_ids[config.name] = [run_id]
        self._worker_pool.adopt_stream_assignment(
            pipeline_name=config.name, run_id=run_id, worker_url=worker_url
        )
        self.manager.set_status(config.name, "running")
        self._activate_kubernetes_service(config)
        logger.info(
            "Boot: adopted live stream run (no re-dispatch)",
            extra={"pipeline": config.name, "worker": worker_url, "run_id": run_id},
        )
        return True

    # ── Public API (called by routers) ─────────────────────────────────────

    def register(
        self,
        config: PipelineConfig,
        yaml_text: str,
        source: str = "api",
    ) -> PipelineState:
        """Register a new pipeline, persist to DB, and schedule if appropriate."""
        with self._lock:
            state = self.manager.register(config, yaml_text=yaml_text)

            if self._db is not None:
                self._db.save_pipeline(config.name, yaml_text, source=source)

            if config.enabled:
                self._do_schedule(config.name)

            logger.info("Registered pipeline", extra={"pipeline": config.name})
            return state

    def update(self, name: str, yaml_text: str) -> PipelineState:
        """Update an existing pipeline's YAML. Restarts if it was running/scheduled."""
        # The whole stop -> deregister -> register -> reschedule sequence is one
        # critical section: it closes the B2 CRUD window (two concurrent
        # PUTs or update+delete interleaving deregister/register) and the B10
        # exists-then-get race. The stream worker-stop HTTP calls underneath are
        # network I/O, but they are intentionally held under the lock here —
        # releasing it mid-update would let a concurrent trigger/CRUD op observe
        # the half-deregistered state and would let two updates interleave their
        # deregister/register steps. update()/delete() are rare admin ops; the
        # blocking cost is bounded by the worker-stop timeout.
        with self._lock:
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
                # E.2 (Decision 5): a queued manual run must dispatch the
                # updated config — refresh its yaml_snapshot so the auditable
                # snapshot column stays the dispatch source.
                self._db.refresh_queued_run_yaml(name, yaml_text)
                # F.1 (§3.2d) belt and braces: a changed transform list may
                # change key semantics — delete the state row outright instead
                # of relying on hydration's config-sha discard.
                self._db.delete_transform_state(name)

            if was_active and config.enabled and self._may_schedule(name):
                self._do_schedule(name)

            logger.info("Updated pipeline", extra={"pipeline": name})
            return new_state

    def delete(self, name: str) -> None:
        """Stop, deregister, and soft-delete a pipeline."""
        # Same reasoning as update(): the stop-then-deregister sequence must be
        # atomic against a concurrent trigger claim or CRUD op (B2).
        with self._lock:
            self._stop_execution(name)
            # Drop any in-flight batch lease so the reconciler can never mark a
            # deleted pipeline's run lost / adopt it back.
            self._active_batch_runs.pop(name, None)
            # E.2 (§7): a deleted pipeline's queued request must never dispatch.
            # Boundary: the purge cannot cancel a dispatch the worker already
            # accepted — dispatch is at-least-once, the same as the normal
            # _run_batch path, so the worker keeps running it and its callback
            # still lands at run-complete (the lease is dropped below, so the
            # reconciler never adopts/marks it lost on the deleted pipeline).
            if self._db is not None:
                self._db.delete_queued_runs(name)
                self._set_queued_depth(name)
            self.manager.deregister(name)
            if self._db is not None:
                self._db.delete_pipeline(name)
                # F.1 (§3.2d): a deleted pipeline's state row is garbage.
                self._db.delete_transform_state(name)
            logger.info("Deleted pipeline", extra={"pipeline": name})

    def start_pipeline(self, name: str) -> Literal["started", "already_running", "disabled", "manual"]:
        """Start a stopped/errored pipeline and report what actually happened."""
        with self._lock:
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
        with self._lock:
            self._stop_execution(name)
            if self._db is not None:
                self._db.stop_pipeline(name)
                # E.2 (§7): stopping a pipeline while its manual run waits
                # rescinds it — purge the queued row (no FAILED row: the run
                # never started). Boundary: the purge cannot cancel a dispatch
                # the worker already accepted — dispatch is at-least-once, the
                # same as the normal _run_batch path.
                self._db.delete_queued_runs(name)
                self._set_queued_depth(name)
            self.manager.set_status(name, "stopped")
            logger.info("Stopped pipeline", extra={"pipeline": name})

    def restart_pipeline(self, name: str) -> None:
        """Restart a pipeline — stop active execution then immediately reschedule.

        Works in both standalone and manager+worker mode.  For stream pipelines
        in manager mode the stream dispatch is cancelled and re-dispatched
        (potentially on a different worker, as determined by the WorkerPool).
        """
        with self._lock:
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

    def trigger_run(self, name: str, flush: bool = False) -> TriggerResult:
        """Immediate one-shot run. Works even when pipeline is stopped.

        *flush* (F.1 §5) is a manual flush run: the executor calls stateful
        transforms' ``close(flush=True)`` so open windows emit as partials and
        are cleared from the saved state. The flag is NOT carried through the
        E.2 queue: a queued flush run that lost the flag executes as a normal
        run when capacity returns.

        Manager+worker mode with the queue flag on and zero healthy workers
        (debounced health state): durably enqueue instead of submitting a run
        that would immediately fail. The run_id is stable across the queue's
        lifetime — the 202 response, the queued_runs row, and (on dispatch)
        the run_history row all share it.
        """
        # Check-and-submit is atomic under the lock so a trigger cannot observe a
        # half-updated or half-deleted pipeline (B2/B10). The authoritative
        # no-double-run guard is the atomic claim in _run_batch(); the status
        # check here is the fast path for the common case.
        with self._lock:
            state = self.manager.get(name)
            if state.config.schedule.type == "stream":
                raise ValueError(f"Pipeline '{name}' is a stream pipeline — cannot trigger manually")
            if state.status == "running":
                raise ValueError(f"Pipeline '{name}' is already running")
            run_id = str(uuid.uuid4())
            # E.2 (§4.1): synchronous enqueue decision. healthy_workers() is a
            # pure dict read over the debounced state — cheap under the lock,
            # no probe I/O. Both enqueue sites funnel through _enqueue_manual_run,
            # which dedupes.
            if self._worker_pool is not None and self._queue_manual_runs and self._db is not None:
                # Dedupe against the DB (the source of truth) before any submit:
                # in the [capacity-returned → drain-commit] window a queued row
                # is still active while healthy_workers() is non-empty. Without
                # this check the submit below is discarded by _run_batch's
                # claim-phase skip (status in ("running", "queued")), returning
                # a run_id that 404s forever. It also covers a manager restart
                # (queued row survived, in-memory status may be stale).
                existing = self._db.get_active_queued_run_for_pipeline(name)
                if existing is not None:
                    if self.manager.exists(name):
                        self.manager.set_status(name, "queued")
                    return TriggerResult(existing["run_id"], "queued")
                if not self._worker_pool.healthy_workers():
                    if self._enqueue_manual_run(name, run_id, state.yaml_text):
                        return TriggerResult(run_id, "queued")
                    # Dedupe hit (Decision 3): a concurrent trigger won the
                    # enqueue — return its run_id so the 202 response keeps the
                    # run_id the user already saw.
                    existing = self._db.get_active_queued_run_for_pipeline(name)
                    return TriggerResult(existing["run_id"], "queued")
            self._thread_pool.submit(partial(self._run_batch, name, run_id, origin="manual", flush=flush))
            return TriggerResult(run_id, "dispatched")

    # ── Queued manual runs (E.2 / GH #21) ────────────────────────────────

    def _set_queued_depth(self, pipeline_name: str) -> None:
        """Set MGR_QUEUE_DEPTH to the pipeline's remaining non-terminal count."""
        if self._db is None:
            return
        from tram.metrics.registry import MGR_QUEUE_DEPTH
        count = sum(
            1
            for row in self._db.get_queued_run_view()
            if row["pipeline_name"] == pipeline_name
        )
        MGR_QUEUE_DEPTH.labels(pipeline=pipeline_name).set(count)

    def _enqueue_manual_run(self, pipeline_name: str, run_id: str, yaml_text: str) -> bool:
        """Persist a queued manual run. Takes the RLock (reentrant for the
        _run_batch fallback site). Returns False when an active queued run
        already exists for the pipeline (dedupe). Sets pipeline status
        'queued', bumps MGR_DISPATCH_TOTAL{no_workers} (metric continuity with
        the legacy fail-fast) and MGR_QUEUE_ENQUEUED_TOTAL.
        """
        with self._lock:
            if self._db is None:
                return False
            if self._db.get_active_queued_run_for_pipeline(pipeline_name) is not None:
                # Dedupe hit (Decision 3): an active queued run already exists —
                # keep the pipeline status in sync so the badge shows 'queued'
                # even after a manager restart (in-memory status is fresh while
                # the queued row survived).
                if self.manager.exists(pipeline_name):
                    self.manager.set_status(pipeline_name, "queued")
                return False
            now = datetime.now(UTC)
            expires_at = now + timedelta(seconds=self._queue_ttl_seconds)
            self._db.save_queued_run(run_id, pipeline_name, yaml_text, now, expires_at)
            if self.manager.exists(pipeline_name):
                self.manager.set_status(pipeline_name, "queued")
            from tram.metrics.registry import (
                MGR_DISPATCH_TOTAL,
                MGR_QUEUE_ENQUEUED_TOTAL,
            )
            MGR_DISPATCH_TOTAL.labels(pipeline=pipeline_name, result="no_workers").inc()
            MGR_QUEUE_ENQUEUED_TOTAL.labels(pipeline=pipeline_name).inc()
            self._set_queued_depth(pipeline_name)
            logger.info(
                "Queued manual run — no healthy workers",
                extra={
                    "pipeline": pipeline_name,
                    "run_id": run_id,
                    "expires_at": expires_at.isoformat(),
                },
            )
            return True

    def drainable_queued_runs(self) -> list[dict]:
        """[{"run_id", "pipeline_name", "yaml_snapshot", "schedule_type",
        "callback_url", "requested_at", "expires_at"}] — status='queued', ordered
        by requested_at, EXCLUDING pipelines that are deleted, have an active
        batch lease, or have status 'running'. Lock-held read; returns copies."""
        with self._lock:
            if self._db is None or self._worker_pool is None:
                return []
            callback_url = (
                f"{self._manager_url}/api/internal/run-complete"
                if self._manager_url else ""
            )
            rows: list[dict] = []
            for run in self._db.get_active_queued_runs():
                name = run["pipeline_name"]
                if not self.manager.exists(name):
                    continue
                if name in self._active_batch_runs:
                    continue
                state = self.manager.get(name)
                if state.status == "running":
                    continue
                rows.append({
                    "run_id": run["run_id"],
                    "pipeline_name": name,
                    "yaml_snapshot": run["yaml_snapshot"],
                    "schedule_type": state.config.schedule.type,
                    "callback_url": callback_url,
                    "requested_at": run["requested_at"],
                    "expires_at": run["expires_at"],
                })
            return rows

    def claim_queued_run(self, run_id: str) -> dict | None:
        """queued → dispatching. RLock + conditional UPDATE (rowcount fence):
        re-reads the row, verifies pipeline exists / not running / no lease,
        runs db.claim_queued_run_row(run_id), returns the claim payload or None
        when the row was claimed, purged, or expired elsewhere."""
        with self._lock:
            if self._db is None or self._worker_pool is None:
                return None
            row = next(
                (r for r in self._db.get_active_queued_runs() if r["run_id"] == run_id),
                None,
            )
            if row is None:
                return None
            name = row["pipeline_name"]
            if not self.manager.exists(name):
                return None
            if name in self._active_batch_runs:
                return None
            state = self.manager.get(name)
            if state.status == "running":
                return None
            if self._db.claim_queued_run_row(run_id) != 1:
                return None
            callback_url = (
                f"{self._manager_url}/api/internal/run-complete"
                if self._manager_url else ""
            )
            return {
                "run_id": run_id,
                "pipeline_name": name,
                "yaml_snapshot": row["yaml_snapshot"],
                "schedule_type": state.config.schedule.type,
                "callback_url": callback_url,
                "requested_at": row["requested_at"],
                "expires_at": row["expires_at"],
            }

    def commit_queued_dispatch(self, run_id: str, worker_url: str) -> bool:
        """dispatching → dispatched + CAS: re-check pipeline exists under the
        lock, record the _active_batch_runs lease (schedule_type from current
        config), set pipeline status 'running', db.mark_queued_run_dispatched,
        MGR_DISPATCH_TOTAL{accepted} + MGR_QUEUE_DISPATCHED_TOTAL + wait histogram."""
        with self._lock:
            if self._db is None or self._worker_pool is None:
                return False
            row = next(
                (r for r in self._db.get_queued_run_view()
                 if r["run_id"] == run_id and r["status"] == "dispatching"),
                None,
            )
            if row is None:
                return False
            name = row["pipeline_name"]
            if not self.manager.exists(name):
                return False  # deleted mid-dispatch — the stale result is discarded
            state = self.manager.get(name)
            self._active_batch_runs[name] = _ActiveBatchRun(
                run_id=run_id,
                pipeline_name=name,
                worker_url=worker_url,
                schedule_type=state.config.schedule.type,
                started_at=datetime.now(UTC),
            )
            self.manager.set_status(name, "running")
            self._db.mark_queued_run_dispatched(run_id, datetime.now(UTC))
            wait = (datetime.now(UTC) - row["requested_at"]).total_seconds()
            from tram.metrics.registry import (
                MGR_DISPATCH_TOTAL,
                MGR_QUEUE_DISPATCHED_TOTAL,
                MGR_QUEUE_DRAIN_RESULT_TOTAL,
                MGR_QUEUE_WAIT_SECONDS,
            )
            MGR_DISPATCH_TOTAL.labels(pipeline=name, result="accepted").inc()
            MGR_QUEUE_DISPATCHED_TOTAL.labels(pipeline=name).inc()
            MGR_QUEUE_DRAIN_RESULT_TOTAL.labels(pipeline=name, result="dispatched").inc()
            MGR_QUEUE_WAIT_SECONDS.labels(pipeline=name).observe(wait)
            self._set_queued_depth(name)
            logger.info(
                "Queued manual run dispatched",
                extra={"pipeline": name, "run_id": run_id, "worker": worker_url,
                       "wait_seconds": round(wait, 1)},
            )
            return True

    def revert_queued_claim(self, run_id: str, result: str = "failed") -> bool:
        """dispatching → queued (drain dispatch_failed / no_capacity race).
        Log WARNING + MGR_QUEUE_DRAIN_RESULT{failed|no_capacity}. No run-history
        churn: nothing was recorded at claim time."""
        with self._lock:
            if self._db is None:
                return False
            row = next(
                (r for r in self._db.get_queued_run_view() if r["run_id"] == run_id),
                None,
            )
            if row is None:
                return False
            if self._db.revert_queued_run_row(run_id) != 1:
                return False
            from tram.metrics.registry import MGR_QUEUE_DRAIN_RESULT_TOTAL
            MGR_QUEUE_DRAIN_RESULT_TOTAL.labels(
                pipeline=row["pipeline_name"], result=result
            ).inc()
            logger.warning(
                "Queued manual run dispatch attempt failed — reverted to queued",
                extra={"pipeline": row["pipeline_name"], "run_id": run_id, "result": result},
            )
            return True

    def expire_queued_run(self, run_id: str) -> bool:
        """queued → expired: db.expire_queued_run_row, then a FAILED RunResult
        (started_at=requested_at, finished_at=now, error="no worker capacity
        within {N} minutes — queued run expired") through _finalize_batch_result,
        so the run-history row, pipeline 'error' status, and K8s service
        deactivation all reuse the proven finalize path. Pipeline deleted
        meanwhile → drop the row only."""
        with self._lock:
            if self._db is None:
                return False
            row = next(
                (r for r in self._db.get_active_queued_runs() if r["run_id"] == run_id),
                None,
            )
            if row is None:
                return False
            if self._db.expire_queued_run_row(run_id) != 1:
                return False
            name = row["pipeline_name"]
            from tram.metrics.registry import MGR_QUEUE_EXPIRED_TOTAL
            MGR_QUEUE_EXPIRED_TOTAL.labels(pipeline=name).inc()
            self._set_queued_depth(name)
            logger.warning(
                "Queued manual run expired at TTL",
                extra={"pipeline": name, "run_id": run_id},
            )
            if not self.manager.exists(name):
                return True  # deleted meanwhile — drop the row only
            minutes = max(1, self._queue_ttl_seconds // 60)
            result = RunResult(
                run_id=run_id,
                pipeline_name=name,
                status=RunStatus.FAILED,
                started_at=row["requested_at"],
                finished_at=datetime.now(UTC),
                records_in=0,
                records_out=0,
                records_skipped=0,
                error=f"no worker capacity within {minutes} minutes — queued run expired",
                node_id=self._node_id,
            )
            self._finalize_batch_result(name, result)
            return True

    def rollback(self, name: str, version: int):
        """Restore a previous pipeline version and restart if appropriate."""
        with self._lock:
            return self.manager.rollback(name, version)

    # ── Read-only delegation ───────────────────────────────────────────────

    def get(self, name: str) -> PipelineState:
        with self._lock:
            return self.manager.get(name)

    def list_all(self) -> list[PipelineState]:
        with self._lock:
            return self.manager.list_all()

    def exists(self, name: str) -> bool:
        with self._lock:
            return self.manager.exists(name)

    def get_runs(self, **kwargs):
        with self._lock:
            return self.manager.get_runs(**kwargs)

    def get_run(self, run_id: str):
        with self._lock:
            return self.manager.get_run(run_id)

    def get_versions(self, name: str) -> list[dict]:
        with self._lock:
            return self.manager.get_versions(name)

    def get_version_yaml(self, name: str, version: int) -> str:
        with self._lock:
            return self.manager.get_version_yaml(name, version)

    def get_scheduler_status(self) -> dict:
        with self._lock:
            next_runs = []
            if self._scheduler:
                for job in self._scheduler.get_jobs():
                    next_run = job.next_run_time
                    next_runs.append({
                        "pipeline": job.id.removeprefix("batch-"),
                        "next_run": next_run.isoformat() if next_run else None,
                    })
            active_streams = list(self._stream_threads.keys())
        workers = None
        if self._worker_pool is not None:
            # Worker health probes are network I/O — do not hold the lifecycle
            # lock across them.
            workers = self._worker_pool.status()
        return {
            "scheduler_running": self._running,
            "active_streams": active_streams,
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
        with self._lock:
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
        with self._lock:
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

        with self._lock:
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

        with self._lock:
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

    def _run_batch(self, pipeline_name: str, run_id: str | None = None, *, origin: str = "scheduled", flush: bool = False) -> None:
        """APScheduler/thread-pool callback — one batch execution.

        ``origin`` is keyword-only so manual triggers are distinguishable from
        APScheduler fires (a manual trigger of an interval pipeline has
        schedule_type == "interval" and is otherwise indistinguishable). The
        default ("scheduled") keeps the APScheduler call sites untouched; the
        E.2 no-capacity fallback enqueue site keys off ``origin == "manual"``
        only.

        ``flush`` (F.1 §5) marks a manual flush run: it rides the worker
        dispatch envelope (``RunRequest.flush``) and the local executor path
        so stateful transforms' ``close(flush=True)`` emits open windows as
        partials and clears them from the saved state.

        Claim phase: the existence check + running-guard + status flip + config
        snapshot happen atomically under the lifecycle RLock, so trigger_run()
        and update()/delete() serialize against it (B1/B2/B10). The actual
        execution (local batch run) or dispatch (worker HTTP calls) happens with
        the lock released; a post-dispatch CAS re-checks the pipeline still
        exists before recording the active-run lease, so a run can never be
        tracked for a pipeline that was deleted mid-dispatch.
        """
        with self._lock:
            if not self.manager.exists(pipeline_name):
                job_id = f"batch-{pipeline_name}"
                if self._scheduler and self._scheduler.get_job(job_id):
                    self._scheduler.remove_job(job_id)
                logger.warning("Batch job: pipeline not found, removed orphan job",
                               extra={"pipeline": pipeline_name})
                return

            state = self.manager.get(pipeline_name)
            if state.status in ("running", "queued"):
                # E.2 (§5): a 'queued' claim is skipped exactly like 'running' —
                # one active run (queued or dispatched) per pipeline. A scheduled
                # fire during a queued window is bounded loss (at most one tick);
                # the queued manual run runs when capacity returns.
                logger.warning("Batch job: previous run still active or queued, skipping",
                               extra={"pipeline": pipeline_name})
                # Trigger/claim TOCTOU (GH #47): a manual trigger whose claim
                # races a concurrent run (e.g. a scheduled fire) already returned
                # TriggerResult(run_id, "dispatched") to the client. Record a
                # FAILED row under that run_id so the client's run_id resolves
                # instead of 404ing forever. Scheduled fires keep the bounded-loss
                # silence (no client holds their run_id). The row is recorded
                # without the status transition: the winning run is genuinely
                # active, so flipping the pipeline to "error" would be wrong.
                if origin == "manual" and run_id is not None:
                    now = datetime.now(UTC)
                    result = RunResult(
                        run_id=run_id,
                        pipeline_name=pipeline_name,
                        status=RunStatus.FAILED,
                        started_at=now,
                        finished_at=now,
                        records_in=0,
                        records_out=0,
                        records_skipped=0,
                        error=(
                            "Manual run skipped: previous run still active or "
                            f"queued (status={state.status})"
                        ),
                        node_id=self._node_id,
                    )
                    self.manager.record_run(pipeline_name, result)
                return

            self.manager.set_status(pipeline_name, "running")
            if run_id is None:
                run_id = str(uuid.uuid4())

            # Snapshot everything the unlocked phases need so a concurrent
            # update()/delete() cannot swap the config under us after the claim.
            config = state.config
            yaml_text = state.yaml_text
            schedule_type = config.schedule.type

        # ── Manager+worker dispatch path ───────────────────────────────────
        if self._worker_pool is not None:
            callback_url = (
                f"{self._manager_url}/api/internal/run-complete"
                if self._manager_url else ""
            )
            from tram.agent.worker_pool import DISPATCH_FAILED, DISPATCH_NO_CAPACITY

            outcome = self._worker_pool.dispatch_with_result(
                run_id=run_id,
                pipeline_name=pipeline_name,
                yaml_text=yaml_text,
                schedule_type=schedule_type,
                callback_url=callback_url,
                flush=flush,
            )
            if outcome.outcome == DISPATCH_NO_CAPACITY:
                # E.2 (§4.2): the fallback enqueue site — capacity vanished
                # between the synchronous trigger check and this dispatch. Queue
                # only manual-origin runs (scheduled runs self-retry on their
                # interval). DISPATCH_FAILED is intentionally NOT queued — it
                # keeps today's fail-fast with its truthful error label
                # (bug-inheritance guard #1).
                if origin == "manual" and self._queue_manual_runs and self._db is not None:
                    if self._enqueue_manual_run(pipeline_name, run_id, yaml_text):
                        return  # queued — no FAILED row, no finalize
                    # Dedupe hit: the synchronous trigger_run site already queued
                    # this pipeline (different run_id). _enqueue_manual_run is
                    # check-then-insert under the RLock, so no row was created for
                    # this run_id — nothing to clean up.
                    return
                failure_time = datetime.now(UTC)
                error = "No healthy workers available for dispatch"
                logger.error("Batch dispatch failed: no healthy workers",
                             extra={"pipeline": pipeline_name, "run_id": run_id})
                metric_result = "no_workers"
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
                MGR_DISPATCH_TOTAL.labels(pipeline=pipeline_name, result=metric_result).inc()
            elif outcome.outcome == DISPATCH_FAILED:
                failure_time = datetime.now(UTC)
                error = f"Worker dispatch failed: {outcome.error or 'unknown error'}"
                logger.error("Worker dispatch attempt failed",
                             extra={"pipeline": pipeline_name, "run_id": run_id,
                                    "error": outcome.error})
                metric_result = "dispatch_failed"
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
                MGR_DISPATCH_TOTAL.labels(pipeline=pipeline_name, result=metric_result).inc()
            else:
                # CAS: re-check under the lock — the pipeline may have been
                # deleted (or re-registered with a new config) while the
                # dispatch HTTP call was in flight. Refuse to track a lease for
                # a pipeline/config that no longer matches the claim. A run
                # already recorded (fast worker that finished and posted
                # run-complete before this dispatch thread re-acquired the
                # lock) must also be skipped: a stale lease would make the
                # BatchReconciler probe is_run_active() → False and mark a
                # succeeded run as lost (GH #47).
                with self._lock:
                    if not self.manager.exists(pipeline_name):
                        logger.warning(
                            "Batch dispatch completed for deleted pipeline — not tracked",
                            extra={"pipeline": pipeline_name, "run_id": run_id},
                        )
                        return
                    if self.manager.get(pipeline_name).config is not config:
                        logger.warning(
                            "Batch dispatch completed for replaced pipeline — not tracked",
                            extra={"pipeline": pipeline_name, "run_id": run_id},
                        )
                        return
                    if self.manager.get_run(run_id) is not None:
                        logger.info(
                            "Batch dispatch completed after run-complete callback — not tracked",
                            extra={"pipeline": pipeline_name, "run_id": run_id},
                        )
                        return
                    self._active_batch_runs[pipeline_name] = _ActiveBatchRun(
                        run_id=run_id,
                        pipeline_name=pipeline_name,
                        worker_url=outcome.worker_url,
                        schedule_type=schedule_type,
                        started_at=datetime.now(UTC),
                    )
                from tram.metrics.registry import MGR_DISPATCH_TOTAL
                MGR_DISPATCH_TOTAL.labels(pipeline=pipeline_name, result="accepted").inc()
            return
        # ── Local execution path ───────────────────────────────────────────
        # Standalone live stats: register a _LocalRun exactly like
        # _stream_worker does, so local batch runs appear in the live stats
        # (via _emit_local_stats_once) and are removed atomically on exit.
        stats = None
        local_run = None
        if self._stats_store is not None:
            from tram.agent.metrics import PipelineStats
            stats = PipelineStats(
                run_id=run_id,
                pipeline_name=pipeline_name,
                schedule_type=schedule_type,
            )
            local_run = _LocalRun(
                run_id=run_id,
                pipeline_name=pipeline_name,
                schedule_type=schedule_type,
                started_at=datetime.now(UTC),
                stats=stats,
            )
            with self._local_stats_lock:
                self._local_active_stats[run_id] = local_run

        try:
            # D.2 §6.1 fingerprint of the dispatched YAML: the executor uses it
            # to discard stale transform state on config change (F.1 §3.2d).
            config_sha256 = hashlib.sha256(str(yaml_text or "").encode()).hexdigest()[:16]
            if stats is not None:
                result = self.executor.batch_run(
                    config, run_id=run_id, stats=stats, config_sha256=config_sha256,
                    flush=flush,
                )
            else:
                result = self.executor.batch_run(
                    config, run_id=run_id, config_sha256=config_sha256, flush=flush,
                )
            self._finalize_batch_result(pipeline_name, result)
        except Exception as exc:
            logger.error("Batch run exception",
                         extra={"pipeline": pipeline_name, "error": str(exc)})
            with self._lock:
                # Guard against a concurrent delete() deregistering the pipeline
                # mid-run — set_status on a missing pipeline would raise inside
                # the except handler (B10).
                if self.manager.exists(pipeline_name):
                    self.manager.set_status(pipeline_name, "error")
        finally:
            if local_run is not None:
                # Remove from dict and StatsStore atomically under the same lock so
                # _emit_local_stats_once() cannot resurrect the entry after removal
                # (same pattern as _stream_worker).
                with self._local_stats_lock:
                    self._local_active_stats.pop(run_id, None)
                    if self._stats_store is not None:
                        self._stats_store.remove(run_id)

    def _on_run_complete(self, pipeline_name: str, result) -> None:
        """Post-run state transition — called after every batch run completes.

        Must be called with the lifecycle lock held (callers: _finalize_batch_result).
        """
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
        with self._lock:
            # The pipeline may have been deleted while the run was in flight —
            # don't resurrect it via record_run/set_status (B10).
            if not self.manager.exists(pipeline_name):
                logger.warning("Finalize skipped: pipeline no longer registered",
                               extra={"pipeline": pipeline_name, "run_id": result.run_id})
                return
            self.manager.record_run(pipeline_name, result)
            self._on_run_complete(pipeline_name, result)
            state = self.manager.get(pipeline_name)
            if state.status in {"stopped", "error"}:
                self._deactivate_kubernetes_service(state.config)

    def get_active_batch_runs(self) -> list[dict]:
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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
        """Callback from a worker agent when a dispatched run finishes.

        The duplicate-run guard (get_run + record) is a check-then-act that must
        be atomic, so the whole state mutation runs under the lifecycle lock.
        Hot path, but every step is in-memory or a single short DB op.
        """
        try:
            run_status = RunStatus(status)
        except ValueError:
            run_status = RunStatus.FAILED

        if isinstance(started_at, str):
            started_at = datetime.fromisoformat(started_at)
        if isinstance(finished_at, str):
            finished_at = datetime.fromisoformat(finished_at)

        with self._lock:
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

    @staticmethod
    def _is_broadcast_workers(workers_cfg: WorkersConfig | None) -> bool:
        """True when the workers config selects more than one slot (or pins a list).

        count == "all", count > 1, and ``workers.list`` all produce a placement
        row; the count=1 single-dispatch path does not (D.2 flag off / legacy).
        Workers is never None post-validation (apply_workers_default assigns it).
        """
        if workers_cfg is None:
            return False
        if workers_cfg.worker_ids is not None:
            return True
        return (
            workers_cfg.count == "all"
            or (isinstance(workers_cfg.count, int) and workers_cfg.count > 1)
        )

    def _start_stream(self, config: PipelineConfig) -> None:
        # ── Manager+worker dispatch path ───────────────────────────────────
        if self._worker_pool is not None:
            # Worker dispatch is network I/O but intentionally held under the
            # lock: the already-dispatched check and the placement bookkeeping
            # must be atomic against delete()/_stop_stream() so a deleted stream
            # is never (re-)dispatched and stop/start cannot interleave.
            with self._lock:
                # F.1 §6 broadcast guard: manager mode + broadcast workers
                # (count>1/all/list) + any stateful transform is rejected —
                # multi_dispatch sends whole pipelines, so each worker sees a
                # partial (Kafka/syslog) or duplicated (gNMI) stream with no
                # key affinity and per-worker state computes silently wrong or
                # duplicated values. Flag-gated (the one hot-path line the
                # TRAM_STATEFUL_TRANSFORMS flag exists to revert); standalone
                # is unaffected (this branch is manager-only).
                from tram.models.pipeline import _STATEFUL_TRANSFORM_TYPES
                if (
                    self._stateful_transforms
                    and self._is_broadcast_workers(config.workers)
                    and any(
                        t.type in _STATEFUL_TRANSFORM_TYPES
                        for t in config.transforms
                    )
                ):
                    stateful = sorted(
                        {
                            t.type
                            for t in config.transforms
                            if t.type in _STATEFUL_TRANSFORM_TYPES
                        }
                    )
                    logger.error(
                        "Stream rejected — stateful transforms cannot run with "
                        "broadcast placement",
                        extra={
                            "pipeline": config.name,
                            "stateful_transforms": stateful,
                            "workers": config.workers.model_dump(),
                        },
                    )
                    self.manager.set_status(config.name, "error")
                    return
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
                if self._single_stream_placements:
                    # D.2 unified path: every worker-mode stream (count=1, N,
                    # all, list) is dispatched through multi_dispatch and
                    # recorded as a durable placement row. count=1 is the
                    # degenerate 1-slot case — multi_dispatch resolves one slot
                    # and sets slot_run_id == placement_group_id. The
                    # already-dispatched guard covers both the legacy
                    # _stream_run_ids bookkeeping and the placement group, so
                    # no scheduler or boot path can enter twice.
                    if config.name in self._active_placement_group:
                        logger.debug("Stream already dispatched",
                                     extra={"pipeline": config.name})
                        return
                    placement_group_id = self._make_placement_group_id(config.name)
                    result = self._worker_pool.multi_dispatch(
                        placement_group_id=placement_group_id,
                        pipeline_name=config.name,
                        yaml_text=state.yaml_text,
                        workers_cfg=config.workers,
                        schedule_type="stream",
                        callback_url=callback_url,
                    )
                    from tram.metrics.registry import MGR_DISPATCH_TOTAL
                    if not result.accepted:
                        # Label parity with the legacy count=1 branch
                        # (worker_pool.multi_dispatch returns rejected slots
                        # only when a dispatch attempt was made and failed):
                        #   result.rejected non-empty  -> dispatch_failed
                        #   result.rejected empty      -> no_capacity
                        if result.rejected:
                            logger.error(
                                "Stream dispatch attempt failed",
                                extra={
                                    "pipeline": config.name,
                                    "error": result.slots[0].get("error") if result.slots else None,
                                },
                            )
                            MGR_DISPATCH_TOTAL.labels(pipeline=config.name, result="dispatch_failed").inc()
                        else:
                            logger.error("Stream dispatch failed: no healthy workers",
                                         extra={"pipeline": config.name})
                            MGR_DISPATCH_TOTAL.labels(pipeline=config.name, result="no_workers").inc()
                        self.manager.set_status(config.name, "error")
                        return
                    # Legacy semantics preserved per worker: the broadcast
                    # branch incremented MGR_DISPATCH_TOTAL{accepted} once per
                    # accepted worker (count=N/all), so the unified branch must
                    # not collapse it to one increment — count=1 still lands at
                    # one because result.accepted has exactly one entry.
                    for _ in result.accepted:
                        MGR_DISPATCH_TOTAL.labels(pipeline=config.name, result="accepted").inc()
                    self._record_broadcast_placement(config.name, placement_group_id, result, config.workers)
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

                if self._is_broadcast_workers(workers_cfg):
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

                from tram.agent.worker_pool import DISPATCH_FAILED, DISPATCH_NO_CAPACITY

                run_id = str(uuid.uuid4())
                outcome = self._worker_pool.dispatch_with_result(
                    run_id=run_id,
                    pipeline_name=config.name,
                    yaml_text=state.yaml_text,
                    schedule_type="stream",
                    callback_url=callback_url,
                )
                if outcome.outcome == DISPATCH_NO_CAPACITY:
                    logger.error("Stream dispatch failed: no healthy workers",
                                 extra={"pipeline": config.name})
                    from tram.metrics.registry import MGR_DISPATCH_TOTAL
                    MGR_DISPATCH_TOTAL.labels(pipeline=config.name, result="no_workers").inc()
                    self.manager.set_status(config.name, "error")
                    return
                if outcome.outcome == DISPATCH_FAILED:
                    logger.error("Stream dispatch attempt failed",
                                 extra={"pipeline": config.name, "error": outcome.error})
                    from tram.metrics.registry import MGR_DISPATCH_TOTAL
                    MGR_DISPATCH_TOTAL.labels(pipeline=config.name, result="dispatch_failed").inc()
                    self.manager.set_status(config.name, "error")
                    return
                worker_url = outcome.worker_url
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

        with self._lock:
            old = self._stream_threads.get(config.name)
            if old is not None and old.is_alive():
                # A previous thread is winding down. Its finally block needs this
                # same lock, so joining under it (callers hold it for CRUD
                # atomicity) would stall to the timeout and abort the restart —
                # leaving the stream dead. Signal it and start the replacement
                # immediately instead: the old thread's cleanup is identity-
                # checked and won't touch the new entry, and sources observe the
                # stop event on their next read cycle, bounding the overlap —
                # the same exposure the manager-mode stop already has.
                stop_evt = self._stop_events.get(config.name)
                if stop_evt is not None:
                    stop_evt.set()
                logger.debug("Replacing still-running stream thread",
                             extra={"pipeline": config.name})

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
            # D.2 §6.1 fingerprint of the stream's YAML (F.1 §3.2d): a stale
            # config redispatch must not hydrate the old key semantics.
            yaml_text = (
                self.manager.get(config.name).yaml_text
                if self.manager.exists(config.name)
                else ""
            )
            config_sha256 = hashlib.sha256(str(yaml_text or "").encode()).hexdigest()[:16]
            self.executor.stream_run(
                config, stop_event, stats=stats, config_sha256=config_sha256
            )
        except Exception as exc:
            logger.error("Stream pipeline crashed",
                         extra={"pipeline": config.name, "error": str(exc)}, exc_info=True)
            with self._lock:
                if self.manager.exists(config.name):
                    state = self.manager.get(config.name)
                    if state.config is config:
                        self.manager.set_status(config.name, "error")
        finally:
            if run_id is not None:
                # Remove from dict and StatsStore atomically under the same lock so
                # _emit_local_stats_once() cannot resurrect the entry after removal.
                with self._local_stats_lock:
                    self._local_active_stats.pop(run_id, None)
                    if self._stats_store is not None:
                        self._stats_store.remove(run_id)
            with self._lock:
                # Identity-check the pops: if a newer stream was started for the
                # same name while this (old) thread was stopping, this thread's
                # cleanup must not remove the new stream's bookkeeping.
                if self._stream_threads.get(config.name) is threading.current_thread():
                    self._stream_threads.pop(config.name, None)
                if self._stop_events.get(config.name) is stop_event:
                    self._stop_events.pop(config.name, None)
                if self.manager.exists(config.name):
                    state = self.manager.get(config.name)
                    # Only transition status when the registered config is still
                    # this thread's config — a newer config (update/restart)
                    # owns the lifecycle state now.
                    if state.config is config and state.status == "running":
                        self.manager.set_status(config.name, "stopped")
                        self._deactivate_kubernetes_service(config)

    def _stop_stream(self, name: str, timeout: int = 10) -> None:
        # timeout is accepted for API compatibility; the local path no longer
        # blocks on a join (see below), and the manager path's cost is bounded
        # by the worker-stop HTTP timeout.
        # ── Manager+worker dispatch path ───────────────────────────────────
        if self._worker_pool is not None:
            # Worker-stop HTTP calls are network I/O kept inside the lock:
            # _stop_stream is invoked from delete()/update()/stop_pipeline()
            # where the stop-then-deregister sequence must be atomic against a
            # concurrent trigger claim or a second CRUD op (B2). Blocking cost
            # is bounded by the worker-stop timeout; these are rare admin ops.
            with self._lock:
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

        with self._lock:
            stop_event = self._stop_events.get(name)
            if stop_event:
                stop_event.set()
        # No join here: callers (update/delete/stop_pipeline) hold the lifecycle
        # lock for the whole stop-then-deregister sequence, and the stream
        # thread's finally block needs that same lock to clean up — joining under
        # it would stall for the full timeout every time. The thread exits
        # asynchronously: the stop event is set, and the finally block does
        # identity-checked cleanup so it never touches a newer thread's
        # bookkeeping. A replacement stream does not wait for this thread to
        # exit — _start_stream() signals a still-alive old thread and starts the
        # new instance immediately, so the old and new instances can briefly
        # overlap, bounded by how quickly the source observes the stop event on
        # its next read cycle (at most one in-flight chunk double-written on
        # restart, the same bounded exposure manager-mode stops already have).
        logger.info("Stopped stream pipeline", extra={"pipeline": name})

    def _stop_execution(self, name: str) -> None:
        """Remove APScheduler job and stop stream thread for a pipeline.

        Caller must hold the lifecycle lock (update/delete/stop_pipeline). The
        local stream path signals the stop event and lets the thread exit
        asynchronously — it never joins, because joining under the caller's
        lock would stall (the stream thread's finally needs the same lock).
        """
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
        """Restore a persisted placement into memory. Caller holds the lock."""
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
        # Re-register the worker-pool run assignments so stop_run(run_id) reaches
        # the worker after a restart. Without this the only fallback is the
        # stop_pipeline_runs probe-all (slow and log-noisy). D.2 §5.1.
        if self._worker_pool is not None:
            for slot in placement_copy["slots"]:
                if slot.get("current_run_id") and slot.get("worker_url"):
                    self._worker_pool.adopt_stream_assignment(
                        pipeline_name=pipeline_name,
                        run_id=str(slot["current_run_id"]),
                        worker_url=str(slot["worker_url"]),
                    )
        if self._db is not None:
            self._db.update_broadcast_placement_status(
                placement_group_id,
                "reconciling",
                slots=placement_copy["slots"],
            )

    def _record_broadcast_placement(self, pipeline_name: str, placement_group_id: str, result, workers_cfg) -> None:
        """Persist a fresh placement. Caller holds the lock (DB writes included)."""
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
            # One active placement row per pipeline (§7.5): a crash between a
            # redispatch and a stop could otherwise leave two active rows for
            # the same pipeline.
            self._db.deactivate_other_placements(pipeline_name, placement_group_id)
            self._db.save_broadcast_placement(
                placement_group_id=placement_group_id,
                pipeline_name=pipeline_name,
                slots=slots,
                target_count=placement["target_count"],
                status=result.status,
                started_at=placement["started_at"],
            )

    def _materialize_placement_from_adoption(self, config: PipelineConfig, adopted: dict) -> None:
        """Create a 1-slot placement row from a worker-reported live run (D.2 §5.2).

        B.6 → D.2 migration bridge: a count=1 stream running at upgrade time has
        no placement row; when the boot guard (or the reconciler's unplaced-stream
        pass) sights the live run with the flag on, materialize the row so the
        stream joins the durable-placement regime without a restart. The adopted
        run_id becomes the run_id_prefix, status is set straight to "running"
        (the run was just probed live), and the slot is marked ``adopted: true``
        as provenance. Caller holds the lock.
        """
        placement_group_id = self._make_placement_group_id(config.name)
        run_id = str(adopted["run_id"])
        worker_url = str(adopted["worker_url"])
        started_at = datetime.now(UTC)
        raw_started = adopted.get("started_at")
        if raw_started:
            try:
                parsed = datetime.fromisoformat(str(raw_started))
                started_at = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
            except ValueError:
                pass
        slot = {
            "worker_index": 0,
            "worker_url": worker_url,
            "worker_id": self._worker_pool.worker_id_for_url(worker_url) or "",
            "pinned_worker_id": None,
            "run_id_prefix": run_id,  # redispatch will use f"{run_id}-r{n}"
            "current_run_id": run_id,
            "dispatched_at": datetime.now(UTC).isoformat(),
            "status": "running",
            "restart_count": 0,
            "adopted": True,  # provenance marker
        }
        placement = {
            "placement_group_id": placement_group_id,
            "pipeline_name": config.name,
            "slots": [slot],
            "target_count": 1,
            "started_at": started_at,
            "status": "running",
        }
        self._broadcast_placements[placement_group_id] = placement
        self._active_placement_group[config.name] = placement_group_id
        self._sync_stream_run_ids_from_slots(config.name, [slot])
        if self._worker_pool is not None:
            self._worker_pool.adopt_stream_assignment(
                pipeline_name=config.name, run_id=run_id, worker_url=worker_url
            )
        if self._db is not None:
            # One active placement row per pipeline (§7.5) — mirror
            # _record_broadcast_placement: a stale active row (e.g. a crash
            # between a redispatch and a stop) is deactivated before the
            # materialized row is persisted.
            self._db.deactivate_other_placements(config.name, placement_group_id)
            self._db.save_broadcast_placement(
                placement_group_id=placement_group_id,
                pipeline_name=config.name,
                slots=[slot],
                target_count=1,
                status="running",
                started_at=started_at,
            )
        from tram.metrics.registry import MGR_RECONCILE_ACTION_TOTAL
        MGR_RECONCILE_ACTION_TOTAL.labels(pipeline=config.name, action="adopt_materialize").inc()
        logger.info(
            "Materialized placement from adopted stream run",
            extra={"pipeline": config.name, "worker": worker_url, "run_id": run_id,
                   "placement_group_id": placement_group_id},
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

    @staticmethod
    def _run_restart_count(run_id: str) -> int:
        """Extract the ``-rN`` restart suffix of a stream run id (0 if absent).

        Placement stream run ids are ``<run_id_prefix>`` for the first dispatch
        and ``<run_id_prefix>-r<restart_count>`` after each redispatch, so the
        suffix orders run instances: stats from an older restart can never be
        adopted over a newer recorded run id (review A13 / plan D.1).
        """
        match = re.search(r"-r(\d+)$", run_id)
        return int(match.group(1)) if match else 0

    def _update_broadcast_placement_status(self, placement_group_id: str, status: str) -> None:
        with self._lock:
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

    def update_broadcast_placement_status(self, placement_group_id: str, status: str) -> None:
        """Public entry point for placement status transitions.

        Thin wrapper so external callers (the PlacementReconciler) use the
        public controller API instead of reaching into the private
        ``_update_broadcast_placement_status``.
        """
        self._update_broadcast_placement_status(placement_group_id, status)

    # ── Unplaced-stream liveness reconciliation (D.2 §5.3) ──────────────────

    def stream_liveness_candidates(self) -> list[dict]:
        """[{"name": str, "has_placement": bool}] — stream pipelines, manager+worker
        mode, status == "running"."""
        with self._lock:
            if self._worker_pool is None:
                return []
            candidates = []
            for state in self.manager.list_all():
                if state.config.schedule.type != "stream":
                    continue
                if state.status != "running":
                    continue
                if self._is_broadcast_workers(state.config.workers):
                    # Broadcast streams carry durable placement rows restored by
                    # _boot_load via _restore_broadcast_placement; the unplaced
                    # liveness pass exists for the count=1 single-dispatch path
                    # only. A broadcast stream that reached the alive-branch
                    # would be materialized as a 1-slot placement (permanently
                    # downgrading it) and its duplicate-live-run sweep would stop
                    # every slot but one — so it is never a candidate (mirrors
                    # the boot guard's early return).
                    continue
                candidates.append({
                    "name": state.config.name,
                    "has_placement": state.config.name in self._active_placement_group,
                })
            return candidates

    def adopt_unplaced_stream_bookkeeping(
        self,
        name: str,
        run_id: str,
        worker_url: str,
        started_at=None,
    ) -> bool:
        """Record a live-but-untracked stream run (unplaced reconciliation, §5.3.2).

        Flag on: the live run is the migration bridge — materialize the 1-slot
        placement row. Flag off: repair the manager bookkeeping (worker-pool
        assignment + _stream_run_ids) so stop/run-complete paths work. Idempotent;
        returns False when the pipeline is gone or already has a placement.
        """
        with self._lock:
            if not self.manager.exists(name):
                return False
            if name in self._active_placement_group:
                return False
            state = self.manager.get(name)
            if self._is_broadcast_workers(state.config.workers):
                # Broadcast streams are never adopted as count=1 placements:
                # their durable placement rows are the authority and are
                # restored at boot (mirrors the boot guard and the candidate
                # filter above). A 1-slot materialization here would permanently
                # downgrade the broadcast stream.
                return False
            if self._single_stream_placements and state.config.schedule.type == "stream":
                adopted = {
                    "run_id": run_id,
                    "worker_url": worker_url,
                    "started_at": started_at,
                }
                self._materialize_placement_from_adoption(state.config, adopted)
                # Mirror the boot path (line ~267): a NodePort-configured stream
                # materialized via the reconciler gets its Service now, not on
                # the next dispatch.
                self._activate_kubernetes_service(state.config)
                return True
            # Legacy bookkeeping repair (flag off).
            run_ids = self._stream_run_ids.get(name)
            if run_id not in (run_ids or []):
                self._stream_run_ids.setdefault(name, []).append(run_id)
            if self._worker_pool is not None and self._worker_pool.assignment_for_run(run_id) is None:
                self._worker_pool.adopt_stream_assignment(
                    pipeline_name=name, run_id=run_id, worker_url=worker_url
                )
            if state.status != "running":
                self.manager.set_status(name, "running")
            return True

    def recover_unplaced_stream(self, name: str) -> None:
        """Recovery for a running stream with no placement record and no live run.

        Idempotent; RLock-reentrant down into _do_schedule (the reconciler
        thread is the only caller on this path). Re-dispatches when the pipeline
        may run, otherwise drops the stale "running" status.
        """
        with self._lock:
            if not self.manager.exists(name):
                return
            state = self.manager.get(name)
            if state.config.schedule.type != "stream":
                return
            if name in self._active_placement_group:
                return  # raced with materialization/placement creation
            self._stream_run_ids.pop(name, None)
            if self._may_schedule(name):
                self._do_schedule(name)  # routes via the flag → placement or legacy
                from tram.metrics.registry import MGR_RECONCILE_ACTION_TOTAL
                MGR_RECONCILE_ACTION_TOTAL.labels(pipeline=name, action="stream_recover").inc()
                logger.info(
                    "Recovered unplaced stream",
                    extra={"pipeline": name},
                )
            else:
                self.manager.set_status(name, "stopped")
                logger.info(
                    "Marked unplaced stream stopped",
                    extra={"pipeline": name},
                )

    # ── Stale-config adoption policy (D.2 §6.2) ─────────────────────────────

    def pipeline_config_sha(self, name: str) -> str:
        """sha256(state.yaml_text)[:16] for a registered pipeline, "" when absent."""
        with self._lock:
            state = self.manager.get(name) if self.manager.exists(name) else None
            if state is None:
                return ""
            return hashlib.sha256(state.yaml_text.encode()).hexdigest()[:16]

    def reconcile_placement_config_drift(self, placement_group_id: str) -> bool:
        """Config drift on a live placement: stop all slot runs, then redispatch
        each slot with the current YAML (redispatch_broadcast_slot sends the
        current state.yaml_text). Returns True when re-dispatched.

        Claim + mark under the RLock, then stop/redispatch as network I/O
        outside the lock. The claim bumps each slot's in-memory status to "stale"
        (persisted) so a concurrent reconciler pass cannot also act on it; the
        existing redispatch CAS serializes against stop/delete.
        """
        with self._lock:
            placement = self._broadcast_placements.get(placement_group_id)
            if placement is None:
                return False
            name = placement["pipeline_name"]
            if not self.manager.exists(name):
                return False
            if not self._may_schedule(name):
                return False  # stopped meanwhile → normal stop path
            for s in placement["slots"]:
                s["status"] = "stale"  # visible during the swap
            if self._db is not None:
                self._db.update_broadcast_placement_status(
                    placement_group_id,
                    placement["status"],
                    slots=placement["slots"],
                )
        # Network I/O outside the lock.
        self._worker_pool.stop_pipeline_runs(name)
        for slot in placement["slots"]:
            self.redispatch_broadcast_slot(placement_group_id, int(slot["worker_index"]))
        from tram.metrics.registry import MGR_RECONCILE_ACTION_TOTAL
        MGR_RECONCILE_ACTION_TOTAL.labels(pipeline=name, action="config_drift_redispatch").inc()
        logger.warning(
            "Config drift: stopped and redispatched placement slots",
            extra={"pipeline": name, "placement_group_id": placement_group_id},
        )
        return True

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
        """Best-effort Service reconciliation. Caller holds the lifecycle lock."""
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
        """Best-effort Service removal. Caller holds the lifecycle lock."""
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
        with self._lock:
            if not self.manager.exists(pipeline_name):
                return
            self._activate_kubernetes_service(self.manager.get(pipeline_name).config)

    def get_active_broadcast_placements(self) -> list[dict]:
        with self._lock:
            # Return copies, not references to controller-owned placement dicts:
            # the PlacementReconciler works on these WITHOUT the lifecycle lock
            # (B.6 residual of B.5) and must not mutate shared state in place.
            # It commits slot changes back through update_placement_slot(), which
            # re-reads the authoritative placement under the lock. Slot values
            # are scalars/None, so a shallow slot copy is sufficient.
            return [
                {**placement, "slots": [dict(slot) for slot in placement["slots"]]}
                for placement in self._broadcast_placements.values()
            ]

    def update_placement_slot(
        self,
        placement_group_id: str,
        worker_index: int,
        *,
        current_run_id: str | None = None,
        worker_url: str | None = None,
        worker_id: str | None = None,
        status: str | None = None,
    ) -> bool:
        """Apply a reconciler-computed slot update under the lifecycle lock (B.6).

        The PlacementReconciler holds copies returned by
        get_active_broadcast_placements; this is the only path that writes a
        slot back into the authoritative placement. The slot is re-read under
        the lock (same pattern as redispatch_broadcast_slot's CAS), so the
        update is applied on top of the current state rather than a stale local
        view, and the mutation is atomic against concurrent controller updates.
        Returns True when the update was applied.
        """
        with self._lock:
            placement = self._broadcast_placements.get(placement_group_id)
            if placement is None:
                return False
            slot = next(
                (s for s in placement["slots"] if int(s.get("worker_index", -1)) == worker_index),
                None,
            )
            if slot is None:
                return False
            if current_run_id is not None:
                slot["current_run_id"] = current_run_id
            if worker_url is not None:
                slot["worker_url"] = worker_url
            if worker_id is not None:
                slot["worker_id"] = worker_id
            if status is not None:
                slot["status"] = status
            self._sync_stream_run_ids_from_slots(placement["pipeline_name"], placement["slots"])
            if self._db is not None:
                self._db.update_broadcast_placement_status(
                    placement_group_id,
                    placement["status"],
                    slots=placement["slots"],
                )
            return True

    def on_pipeline_stats(self, payload) -> None:
        # Guard the slot/placement mutations against concurrent _stop_stream()
        # / delete() / redispatch_broadcast_slot(). Hot path, but the body is
        # dict updates + short DB writes — keep it fast.
        with self._lock:
            placement_group_id = self._active_placement_group.get(payload.pipeline_name)
            if placement_group_id is None:
                return
            placement = self._broadcast_placements.get(placement_group_id)
            if placement is None or placement.get("status") != "reconciling":
                return

            changed = False
            lost_race = False
            for slot in placement["slots"]:
                run_id_prefix = str(slot.get("run_id_prefix", ""))
                if payload.run_id != slot.get("current_run_id") and not payload.run_id.startswith(run_id_prefix):
                    continue
                if slot.get("current_run_id") != payload.run_id:
                    recorded_run_id = str(slot.get("current_run_id", ""))
                    # Never regress to a superseded run instance: stats from a
                    # stale stream (still running after a redispatch replaced
                    # the run) must not overwrite the newer recorded run id
                    # (review A13 / plan D.1).
                    if self._run_restart_count(payload.run_id) < self._run_restart_count(recorded_run_id):
                        logger.warning(
                            "Ignoring stats from superseded stream run",
                            extra={
                                "pipeline": payload.pipeline_name,
                                "placement_group_id": placement_group_id,
                                "worker_index": int(slot["worker_index"]),
                                "run_id": payload.run_id,
                                "recorded_run_id": recorded_run_id,
                            },
                        )
                        continue
                    if self._db is not None:
                        updated = self._db.update_slot_run_id(
                            placement_group_id,
                            int(slot["worker_index"]),
                            payload.run_id,
                            status="running",
                            restart_count=int(slot.get("restart_count", 0)),
                            expected_run_id=recorded_run_id,
                        )
                        if updated == 0:
                            # Lost race: a redispatch (or another manager)
                            # advanced the slot's run id between our read and
                            # the DB write. Keep the newer DB state; neither
                            # mutate nor persist the stale in-memory slot.
                            lost_race = True
                            logger.warning(
                                "Lost slot run-id update race",
                                extra={
                                    "pipeline": payload.pipeline_name,
                                    "placement_group_id": placement_group_id,
                                    "worker_index": int(slot["worker_index"]),
                                    "run_id": payload.run_id,
                                    "recorded_run_id": recorded_run_id,
                                },
                            )
                            continue
                    slot["current_run_id"] = payload.run_id
                if slot.get("status") != "running":
                    slot["status"] = "running"
                    changed = True

            self._sync_stream_run_ids_from_slots(payload.pipeline_name, placement["slots"])
            if lost_race:
                # The DB has advanced beyond this in-memory copy; persisting
                # anything computed from it would clobber the newer run id.
                return
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
        # Claim phase: read the placement/slot and prepare the dispatch under the
        # lock, then release for the worker HTTP call, then re-check (CAS) before
        # committing the slot mutation so a concurrent _stop_stream()/delete()
        # can't race the redispatch commit.
        with self._lock:
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
            pipeline_name = placement["pipeline_name"]
            yaml_text = state.yaml_text

        # Network I/O with the lock released.
        if not self._worker_pool.dispatch_to_worker(
            worker_url=worker_url,
            run_id=new_run_id,
            pipeline_name=pipeline_name,
            yaml_text=yaml_text,
            schedule_type="stream",
            callback_url=callback_url,
        ):
            return False

        # CAS: the placement may have been stopped/removed while dispatching.
        with self._lock:
            placement = self._broadcast_placements.get(placement_group_id)
            if placement is None:
                return False
            slot = next(
                (s for s in placement["slots"] if int(s.get("worker_index", -1)) == worker_index),
                None,
            )
            if slot is None:
                return False
            slot["current_run_id"] = new_run_id
            slot["worker_url"] = worker_url
            slot["worker_id"] = str(pinned_worker_id or self._worker_pool.worker_id_for_url(worker_url) or "")
            slot["dispatched_at"] = datetime.now(UTC).isoformat()
            slot["status"] = "running"
            slot["restart_count"] = restart_count
            self._sync_stream_run_ids_from_slots(pipeline_name, placement["slots"])
            if self._db is not None:
                self._db.update_broadcast_placement_status(
                    placement_group_id,
                    placement["status"],
                    slots=placement["slots"],
                )
        return True
