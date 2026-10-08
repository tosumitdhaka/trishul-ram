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
import json
import logging
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Literal

from sqlalchemy import text

from tram.core.context import RunResult, RunStatus
from tram.persistence import ledger
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.manager import PipelineManager, PipelineState

if TYPE_CHECKING:
    from tram.agent.metrics import PipelineStats
    from tram.agent.worker_pool import WorkerPool
    from tram.models.pipeline import PipelineConfig, WorkersConfig
    from tram.persistence.db import TramDB
    from tram.persistence.file_tracker import ProcessedFileTracker

logger = logging.getLogger(__name__)

# v1.6.0 (GH #81): maximum periodic run-history rollup rows per standalone
# stream lifecycle. Matches PipelineManager's per-pipeline _MAX_RUN_HISTORY
# deque cap (500), so one lifecycle can never fill more than a full run-history
# page with segment rows; after the cap the live StatsStore stats remain the
# visibility channel and the final lifecycle row still lands at stop.
_STREAM_ROLLUP_ROWS_MAX = 500

# V18-01 §9 (frozen): audit retention for terminal manager-ledger rows.
_AUDIT_RETENTION_DAYS_DEFAULT = 30
# Ledger audit retention sweep interval (plan F: incremental retention jobs).
_LEDGER_RETENTION_INTERVAL_S = 3600


def _audit_retention_days() -> int:
    """``TRAM_AUDIT_RETENTION_DAYS`` (V18-01 §9, frozen default 30) — audit
    retention for terminal manager-ledger rows (execution_attempts,
    run_intents, lifecycle_operations).

    Follows the strictest env-reader convention (``tram/core/config.py``
    ``_env_int``): a typo'd value fails loud at startup instead of silently
    changing retention. A floor of 1 day guards against a destructive
    ``0``/negative setting.
    """
    raw = os.environ.get("TRAM_AUDIT_RETENTION_DAYS", str(_AUDIT_RETENTION_DAYS_DEFAULT))
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"Environment variable TRAM_AUDIT_RETENTION_DAYS={raw!r} is not a valid integer"
        ) from None
    return max(1, value)


@dataclass
class _LocalRun:
    run_id: str
    pipeline_name: str
    schedule_type: str
    started_at: datetime
    stats: PipelineStats
    # v1.6.0 (GH #81): single-topology stream run-history rollups. Cumulative
    # counters at the previous segment boundary (segment rows record DELTAs so
    # the sum of all rows equals the lifecycle totals), the boundary timestamp,
    # and a per-lifecycle row count used as a hard cap. Batch _LocalRuns never
    # populate these (all rollup call sites gate on schedule_type == "stream").
    last_rollup: dict[str, int] = field(default_factory=dict)
    last_rollup_at: datetime | None = None
    rollup_rows: int = 0


@dataclass
class _ActiveBatchRun:
    run_id: str
    pipeline_name: str
    worker_url: str
    schedule_type: str
    started_at: datetime
    # V18-04: the ledger attempt this lease tracks (None for adopted/legacy
    # leases). Used by the identity-checked lease cleanup on run-complete.
    attempt_id: str | None = None
    generation: int | None = None


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

        # Plan D boot order: the ledger's desired-state rows are loaded before
        # boot adoption (V18-06), so adoption and the stopped/running decisions
        # read the same durable generation/desired_status source.
        self._desired_state: dict[str, dict] = {}
        # Frozen §9 audit retention for the periodic ledger cleanup (plan F).
        self._audit_retention_days: int = _audit_retention_days()
        self._ledger_retention_stop = threading.Event()

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

        # Plan F: the ledger audit-retention sweep runs as a boot+interval
        # daemon thread (same pattern as _local_stats_loop) — the first pass
        # runs immediately, then hourly. DB-less standalone is unaffected.
        if self._db is not None:
            self._ledger_retention_stop.clear()
            t = threading.Thread(
                target=self._ledger_retention_loop,
                name="tram-ledger-retention",
                daemon=True,
            )
            t.start()

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
        self._ledger_retention_stop.set()
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
            # Plan D boot order (V18-06): desired-state load/backfill → boot
            # adoption → scheduler start. The desired-state rows (M3 backfill
            # at schema init, kept current by lifecycle ops) are loaded BEFORE
            # adoption so both adoption and the stopped/running decisions read
            # the same durable generation/desired_status source.
            # V18-04 §2 (R4): boot adoption replaces the "nothing is in flight"
            # reset. Every non-terminal ledger attempt is resolved before any
            # scheduler fires: claimed-unsent attempts abort locally (never
            # unknown), dispatching/running attempts are resolved against the
            # owning worker's journal, and unresolvable ones go 'unknown' with
            # the guard retained. Queued_runs rows stuck at 'dispatching'
            # without a ledger attempt are re-queued for the drain.
            if self._db is not None:
                self._load_desired_state()
                self._resolve_non_terminal_attempts_at_boot()
            stopped_names = set(self._db.get_stopped_pipeline_names())
            # Desired-state rows are authoritative when present (M3 backfill +
            # lifecycle ops keep them current); the legacy stopped flag covers
            # rows that predate the desired-state table (conservative union —
            # an operator stop is never resurrected at boot).
            for name, row in self._desired_state.items():
                if row.get("deleted") == 1 or row.get("desired_status") == "stopped":
                    stopped_names.add(name)
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

    # ── Boot adoption (V18-04 §2 / frozen §2–3) ─────────────────────────────

    def _load_desired_state(self) -> None:
        """Plan D boot order step 1: load ``pipeline_desired_state`` into memory.

        Runs under the lifecycle lock at the very start of ``_boot_load`` —
        before boot adoption (step 2) and before any scheduler fires (step 3).
        The M3 migration backfills these rows from ``registered_pipelines`` at
        schema init; lifecycle ops (stop/start/update/delete) keep
        ``desired_status``/``generation`` current. Adoption and the
        stopped/running decisions both read this map so they agree on the
        durable generation.
        """
        self._desired_state = {}
        if self._db is None:
            return
        with self._db._engine.connect() as conn:  # noqa: SLF001 — repo convention
            rows = conn.execute(text("""
                SELECT pipeline_name, desired_status, generation, deleted,
                       stopped_reason
                  FROM pipeline_desired_state
            """)).mappings().fetchall()
        self._desired_state = {str(r["pipeline_name"]): dict(r) for r in rows}

    def _resolve_non_terminal_attempts_at_boot(self) -> None:
        """Resolve every non-terminal ledger attempt before any scheduler fires.

        Replaces the legacy ``reset_dispatching_queued_runs`` (R4: the "nothing
        is in flight" assumption is deleted). Called from ``_boot_load`` under
        the lifecycle lock, before ``_scheduler.start()``.

        - ``claimed`` attempts with ``dispatch_sent_at IS NULL`` are resolved
          locally — terminal ``aborted`` / ``manager_lost_before_dispatch``,
          never ``unknown``, never colliding with a replacement (frozen §2);
        - ``dispatching``/``running`` attempts are resolved against the owning
          worker's journal (``GET /agent/attempts/{attempt_id}``): a journal
          completion resolves the intent + terminal transition; a journal
          interrupted marker or revocation tombstone terminates the attempt;
          an unreachable worker, a missing journal row, or a v1.7 worker
          without the query endpoint leaves the attempt ``unknown`` with the
          guard RETAINED (operator force-release is a later lane);
        - queued_runs rows stuck at 'dispatching' with no non-terminal ledger
          attempt (crash between the queue fence and the ledger claim) are
          reset to 'queued' so the drain can re-claim them — nothing is in
          flight for those.
        """
        if self._db is None:
            return
        self._reset_orphan_dispatching_queued_runs()
        for attempt in self._get_non_terminal_attempts():
            try:
                self._resolve_attempt_at_boot(attempt)
            except Exception as exc:
                logger.error(
                    "Boot adoption failed for attempt",
                    extra={
                        "attempt_id": attempt["attempt_id"],
                        "run_id": attempt["run_id"],
                        "error": str(exc),
                    },
                )

    def _get_non_terminal_attempts(self) -> list[dict]:
        """claimed/dispatching/running ledger attempts (boot adoption scope).
        ``stopping``/``unknown`` rows are the V18-06 recovery lane's inputs."""
        with self._db._engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT attempt_id, run_id, pipeline_name, generation, ordinal,
                       state, slot_id, worker_id, dispatch_sent_at
                  FROM execution_attempts
                 WHERE state IN ('claimed', 'dispatching', 'running')
                 ORDER BY run_id, ordinal
            """)).mappings().fetchall()
        return [dict(r) for r in rows]

    def _reset_orphan_dispatching_queued_runs(self) -> None:
        """queued_runs 'dispatching' rows with no non-terminal ledger attempt
        are safe to re-queue (their claim never completed — nothing is in
        flight for them). Rows WITH a non-terminal attempt are owned by
        adoption and are never blindly reset (R4)."""
        with self._db._engine.begin() as conn:
            conn.execute(text("""
                UPDATE queued_runs SET status = 'queued'
                 WHERE status = 'dispatching'
                   AND NOT EXISTS (
                       SELECT 1 FROM execution_attempts ea
                        WHERE ea.run_id = queued_runs.run_id
                          AND ea.state != 'terminal'
                   )
            """))

    def _resolve_attempt_at_boot(self, attempt: dict) -> None:
        """Boot-adoption resolution for one non-terminal attempt (frozen §2)."""
        attempt_id = attempt["attempt_id"]
        run_id = attempt["run_id"]
        pipeline_name = attempt["pipeline_name"]
        generation = attempt["generation"]

        # (a) claimed, never dispatched → local resolution, never unknown.
        if attempt["state"] == "claimed" and attempt.get("dispatch_sent_at") is None:
            self._terminalize_attempt(
                attempt_id=attempt_id,
                run_id=run_id,
                pipeline_name=pipeline_name,
                generation=generation,
                resolve_outcome="aborted",
                cancel_reason="manager_lost_before_dispatch",
            )
            self._terminal_cancel_queued_run(run_id, "manager_lost_before_dispatch")
            self._record_completed_lifecycle_operation(
                pipeline_name,
                "boot_adopt",
                attempt_id=attempt_id,
                detail="boot adoption: claimed attempt aborted "
                       "(manager_lost_before_dispatch)",
            )
            logger.info(
                "Boot adoption: aborted never-dispatched attempt",
                extra={
                    "pipeline": pipeline_name,
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                },
            )
            return

        # (b) dispatching/running → resolve against the owning worker journal.
        data, worker_url = self._query_attempt_from_worker(attempt)
        if data is None:
            # Unreachable worker, no journal row, or a v1.7 worker without the
            # query endpoint — insufficient evidence (plan D). The attempt goes
            # 'unknown' and the guard is RETAINED: blind redispatch is
            # forbidden (frozen §2 invariant 5) and operator force-release is a
            # later lane — never auto-cleared here.
            self._mark_attempt_unknown(
                attempt, uncertainty_reason="boot_adoption_no_journal_evidence"
            )
            self._record_completed_lifecycle_operation(
                pipeline_name,
                "boot_adopt",
                attempt_id=attempt_id,
                detail="boot adoption: attempt unresolved (unknown) — no "
                       "journal evidence, guard retained",
            )
            logger.warning(
                "Boot adoption: attempt unresolved (unknown) — guard retained",
                extra={
                    "pipeline": pipeline_name,
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "worker": worker_url,
                },
            )
            return

        kind, result_json = self._classify_attempt_query(data)
        if kind == "completed":
            # Journal completion record: resolve the intent + terminal
            # transition (the frozen §2 unknown → terminal resolution).
            outcome = self._completion_outcome(result_json)
            self._terminalize_attempt(
                attempt_id=attempt_id,
                run_id=run_id,
                pipeline_name=pipeline_name,
                generation=generation,
                resolve_outcome=outcome,
            )
            # V18-06: the adoption-resolved completion is queryable in history
            # like a normal completion — write the run_history row from the
            # decoded journal payload (the pipeline is not registered yet at
            # boot; manager.register hydrates last_run from the row).
            self._record_adoption_completion_history(attempt, result_json, outcome=outcome)
            self._terminal_cancel_queued_run(run_id, "boot_adoption_completed")
            self._record_completed_lifecycle_operation(
                pipeline_name,
                "boot_adopt",
                attempt_id=attempt_id,
                detail="boot adoption: attempt resolved from journal "
                       "completion record",
            )
            logger.info(
                "Boot adoption: resolved completed attempt from journal",
                extra={
                    "pipeline": pipeline_name,
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                },
            )
            return
        if kind in ("interrupted", "tombstone"):
            # Interrupted reservation (worker restart) or revocation tombstone:
            # terminal revoked/aborted per the frozen §2 boot-adoption rule.
            self._terminalize_attempt(
                attempt_id=attempt_id,
                run_id=run_id,
                pipeline_name=pipeline_name,
                generation=generation,
                resolve_outcome="aborted",
                cancel_reason=f"boot_adoption_{kind}",
            )
            self._terminal_cancel_queued_run(run_id, f"boot_adoption_{kind}")
            self._record_completed_lifecycle_operation(
                pipeline_name,
                "boot_adopt",
                attempt_id=attempt_id,
                detail=f"boot adoption: attempt terminal ({kind}) — revoked/aborted",
            )
            logger.info(
                "Boot adoption: terminated attempt from journal",
                extra={
                    "pipeline": pipeline_name,
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "journal_kind": kind,
                },
            )
            return
        if kind == "active":
            # The run is still live on a reachable worker: adopt the lease so
            # the reconciler probes it and never marks it lost. The guard stays
            # held by this attempt; the intent stays unresolved until the
            # run-complete lands.
            self._adopt_active_attempt_lease(attempt, worker_url)
            return
        # Unrecognizable reply — no evidence either.
        self._mark_attempt_unknown(
            attempt, uncertainty_reason="boot_adoption_unrecognized_journal_reply"
        )
        self._record_completed_lifecycle_operation(
            pipeline_name,
                "boot_adopt",
                attempt_id=attempt_id,
                detail="boot adoption: attempt unresolved (unknown) — "
                       "unrecognized journal reply, guard retained",
        )

    def _query_attempt_from_worker(self, attempt: dict) -> tuple[dict | None, str | None]:
        """GET /agent/attempts/{id} on the attempt's owning worker.

        The owning worker is resolved from the ledger's worker_id (recorded at
        dispatch acceptance). A missing/unresolvable worker_id (pre-upgrade
        rows) falls back to fanning out to every configured worker — the
        attempt is on one of them, and the first non-None reply wins.
        """
        if self._worker_pool is None:
            return None, None
        worker_url = None
        worker_id = attempt.get("worker_id") or ""
        if worker_id:
            worker_url = self._worker_pool.url_for_worker_id(worker_id)
            if worker_url:
                data = self._worker_pool.query_attempt(worker_url, attempt["attempt_id"])
                if data is not None:
                    return data, worker_url
        for url in self._worker_pool.worker_urls():
            if url == worker_url:
                continue
            data = self._worker_pool.query_attempt(url, attempt["attempt_id"])
            if data is not None:
                return data, url
        return None, worker_url

    @staticmethod
    def _classify_attempt_query(data: dict) -> tuple[str, str | None]:
        """Classify a GET /agent/attempts/{id} reply (frozen §5).

        Returns ``(kind, result_json)`` with kind ∈
        ``completed|interrupted|tombstone|active|unknown``. ``result_json`` is
        the journal completion payload for 'completed'. The worker endpoint is
        a parallel lane against the frozen contract; any unrecognizable payload
        is treated as no evidence.
        """
        kind = str(
            data.get("kind") or data.get("status") or data.get("state") or ""
        ).lower()
        result_json = data.get("result_json")
        if result_json is not None or kind in ("completed", "completion"):
            return "completed", result_json
        if kind in ("interrupted", "interrupted_reservation"):
            return "interrupted", None
        if kind == "tombstone" or data.get("tombstone") is not None:
            return "tombstone", None
        if kind in ("active", "running", "reserved"):
            return "active", None
        return "unknown", None

    @staticmethod
    def _completion_outcome(result_json) -> str:
        """Map a journal completion payload to the run_intents outcome domain."""
        payload = result_json
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        status = None
        if isinstance(payload, dict):
            status = payload.get("status")
        if isinstance(status, str):
            try:
                return PipelineController._intent_outcome(RunStatus(status))
            except ValueError:
                pass
        # No decodable result → the terminal outcome is 'aborted' (the run's
        # post-crash resolution is never invented as a success).
        return "aborted"

    @staticmethod
    def _status_from_intent_outcome(outcome: str) -> RunStatus:
        """Map the intent-outcome domain back to a RunStatus (history row)."""
        if outcome == "success":
            return RunStatus.SUCCESS
        if outcome == "partial":
            return RunStatus.PARTIAL
        if outcome == "aborted":
            return RunStatus.ABORTED
        return RunStatus.FAILED

    @staticmethod
    def _decode_disposition(payload: dict) -> dict | None:
        """Per-sink/dlq/spool/failed counters recorded in a completion payload.

        The worker's journal ``result_json`` carries run-scoped counters; a
        per-sink ``disposition`` map and ``spool`` accounting are included
        when the executor recorded them (V18-02). None when nothing beyond the
        base RunResult counters is recorded.
        """
        recorded: dict = {}
        for key in ("dlq_count", "records_failed", "dlq_succeeded", "dlq_failed"):
            if key in payload:
                recorded[key] = int(payload[key] or 0)
        per_sink = payload.get("disposition")
        if isinstance(per_sink, dict) and per_sink:
            recorded["per_sink"] = per_sink
        spool = payload.get("spool")
        if isinstance(spool, dict) and spool:
            recorded["spool"] = spool
        return recorded or None

    def _record_adoption_completion_history(
        self, attempt: dict, result_json, *, outcome: str
    ) -> None:
        """Write the run_history row for an adoption-resolved completion.

        V18-06 task 1: boot adoption decodes the journal completion and
        resolves the intent, but previously wrote no history row — the run was
        invisible in ``/api/runs``. The pipeline is not registered yet at boot
        (``manager.record_run`` would raise), so the row goes straight to the
        DB; the V18 M2 columns (attempt_id/generation/outcome/disposition_json)
        are populated for the extended GET /runs/{run_id}.
        """
        if self._db is None:
            return
        payload = result_json
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        payload = payload if isinstance(payload, dict) else {}
        now = datetime.now(UTC)

        def _ts(key: str, default: datetime) -> datetime:
            value = payload.get(key)
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value)
                except ValueError:
                    pass
            return default

        worker_id = str(payload.get("worker_id") or attempt.get("worker_id") or "") or self._node_id
        result = RunResult(
            run_id=attempt["run_id"],
            pipeline_name=attempt["pipeline_name"],
            status=self._status_from_intent_outcome(outcome),
            started_at=_ts("started_at", now),
            finished_at=_ts("finished_at", now),
            records_in=int(payload.get("records_in") or 0),
            records_out=int(payload.get("records_out") or 0),
            records_skipped=int(payload.get("records_skipped") or 0),
            bytes_in=int(payload.get("bytes_in") or 0),
            bytes_out=int(payload.get("bytes_out") or 0),
            error=payload.get("error"),
            errors=list(payload.get("errors") or []),
            node_id=worker_id,
            dlq_count=int(payload.get("dlq_count") or 0),
            records_failed=int(payload.get("records_failed") or 0),
            dlq_succeeded=int(payload.get("dlq_succeeded") or 0),
            dlq_failed=int(payload.get("dlq_failed") or 0),
        )
        self._db.save_run(result)
        disposition = self._decode_disposition(payload)
        with self._db._engine.begin() as conn:
            conn.execute(text("""
                UPDATE run_history
                   SET attempt_id = :attempt_id, generation = :generation,
                       outcome = :outcome, disposition_json = :disposition
                 WHERE run_id = :run_id
            """), {
                "attempt_id": attempt["attempt_id"],
                "generation": attempt["generation"],
                "outcome": outcome,
                "disposition": json.dumps(disposition) if disposition else None,
                "run_id": attempt["run_id"],
            })

    def _mark_attempt_unknown(self, attempt: dict, *, uncertainty_reason: str) -> None:
        """dispatching/running → unknown (insufficient evidence, plan D).

        The guard is RETAINED — blind redispatch is forbidden (frozen §2
        invariant 5) and operator force-release is a later lane; this lane
        never auto-clears an unknown guard.
        """
        with self._db._engine.begin() as conn:
            conn.execute(text("""
                UPDATE execution_attempts
                   SET state = 'unknown', uncertainty_reason = :reason
                 WHERE attempt_id = :attempt_id AND run_id = :run_id
                   AND generation = :generation
                   AND state IN ('dispatching', 'running')
            """), {
                "attempt_id": attempt["attempt_id"],
                "run_id": attempt["run_id"],
                "generation": attempt["generation"],
                "reason": uncertainty_reason,
            })

    def _adopt_active_attempt_lease(self, attempt: dict, worker_url: str) -> None:
        """Adopt the lease for an attempt the journal reports as still active.

        The run is live on a reachable worker — record the batch lease so the
        BatchReconciler probes it instead of marking it lost (mirrors
        adopt_active_batch_run). The guard stays held by this attempt and the
        intent stays unresolved until the run-complete lands.
        """
        pipeline_name = attempt["pipeline_name"]
        run_id = attempt["run_id"]
        self._active_batch_runs[pipeline_name] = _ActiveBatchRun(
            run_id=run_id,
            pipeline_name=pipeline_name,
            worker_url=worker_url,
            schedule_type="batch",
            started_at=datetime.now(UTC),
            attempt_id=attempt["attempt_id"],
            generation=attempt["generation"],
        )
        if self.manager.exists(pipeline_name):
            self.manager.set_status(pipeline_name, "running")
        logger.info(
            "Boot adoption: attempt active on worker — lease adopted",
            extra={
                "pipeline": pipeline_name,
                "run_id": run_id,
                "attempt_id": attempt["attempt_id"],
                "worker": worker_url,
            },
        )

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
                # V18-04: the ledger's desired-state row (generation lives
                # here, frozen §1).
                self._ensure_desired_state(
                    config.name,
                    desired_status="running" if config.enabled else "stopped",
                )

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

            op_id = self._record_lifecycle_operation(
                name, "update", detail="updating pipeline",
            )
            try:
                self._stop_execution(name)
                self.manager.deregister(name)
                new_state = self.manager.register(config, yaml_text=yaml_text)

                if self._db is not None:
                    self._db.save_pipeline(name, yaml_text, source="api")
                    # V18-04: the generation bumps on every config update (frozen
                    # §1) — the next claim fences against the new generation.
                    self._bump_pipeline_generation(name)
                    # R16: the restart-update terminal-cancels queued (pending)
                    # rows with the recorded reason — a previously returned
                    # run_id keeps resolving as a terminal record instead of
                    # waiting for TTL expiry. (Supersedes the E.2 Decision 5
                    # snapshot refresh: a queued run whose pipeline changed
                    # config no longer dispatches the stale snapshot.)
                    self._terminal_cancel_pipeline_queued(name, "pipeline_updated")
                    self._set_queued_depth(name)
                    # F.1 (§3.2d) belt and braces: a changed transform list may
                    # change key semantics — delete the state row outright instead
                    # of relying on hydration's config-sha discard.
                    self._db.delete_transform_state(name)

                if was_active and config.enabled and self._may_schedule(name):
                    self._do_schedule(name)

                self._complete_lifecycle_operation(op_id, detail="pipeline updated")
                logger.info("Updated pipeline", extra={"pipeline": name})
                return new_state
            except Exception as exc:
                self._complete_lifecycle_operation(
                    op_id, state="failed", detail=f"update failed: {exc}"
                )
                raise

    def delete(self, name: str) -> None:
        """Stop, deregister, and soft-delete a pipeline."""
        # Same reasoning as update(): the stop-then-deregister sequence must be
        # atomic against a concurrent trigger claim or CRUD op (B2).
        with self._lock:
            op_id = self._record_lifecycle_operation(
                name, "delete", detail="deleting pipeline",
            )
            try:
                self._stop_execution(name)
                # Drop any in-flight batch lease so the reconciler can never mark a
                # deleted pipeline's run lost / adopt it back.
                self._active_batch_runs.pop(name, None)
                # R16: terminal-cancel queued (pending) rows with the recorded
                # reason instead of purging them. Boundary: cancellation cannot
                # cancel a dispatch the worker already accepted — dispatch is
                # at-least-once, the same as the normal _run_batch path, so the
                # worker keeps running it and its callback still lands at
                # run-complete (the lease is dropped below, so the reconciler
                # never adopts/marks it lost on the deleted pipeline).
                if self._db is not None:
                    self._terminal_cancel_pipeline_queued(name, "pipeline_deleted")
                    # Task 5: prune orphaned unresolved run_intents rows — the
                    # old purge left them forever for a deleted pipeline.
                    self._prune_orphaned_run_intents(name)
                    self._set_queued_depth(name)
                self.manager.deregister(name)
                if self._db is not None:
                    self._db.delete_pipeline(name)
                    # V18-04: the generation bumps on the delete tombstone (frozen
                    # §1); the desired-state row keeps the tombstone.
                    self._bump_pipeline_generation(name, deleted=True)
                    # F.1 (§3.2d): a deleted pipeline's state row is garbage.
                    self._db.delete_transform_state(name)
                self._complete_lifecycle_operation(op_id, detail="pipeline deleted")
                logger.info("Deleted pipeline", extra={"pipeline": name})
            except Exception as exc:
                self._complete_lifecycle_operation(
                    op_id, state="failed", detail=f"delete failed: {exc}"
                )
                raise

    def start_pipeline(self, name: str) -> Literal["started", "already_running", "disabled", "manual"]:
        """Start a stopped/errored pipeline and report what actually happened."""
        with self._lock:
            state = self.manager.get(name)
            if state.status in ("running", "scheduled"):
                logger.debug("start_pipeline: already running", extra={"pipeline": name})
                return "already_running"

            if self._db is not None:
                self._db.start_pipeline_flag(name)
                # Plan D: desired-state row mirrors the legacy flag flip.
                self._set_desired_status(name, "running")

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
            op_id = self._record_lifecycle_operation(
                name, "stop", detail="stopping pipeline",
            )
            try:
                self._stop_execution(name)
                if self._db is not None:
                    self._db.stop_pipeline(name)
                    # Plan D: keep the desired-state row current so boot reads
                    # the same stopped decision from the ledger.
                    self._set_desired_status(name, "stopped")
                    # R16: terminal-cancel queued (pending) rows with the
                    # recorded reason instead of purging them — the returned
                    # run_id keeps resolving as a terminal record. The active
                    # attempt is cancelled by _stop_execution (existing stop
                    # path, unchanged semantics otherwise).
                    self._terminal_cancel_pipeline_queued(name, "pipeline_stopped")
                    self._set_queued_depth(name)
                self.manager.set_status(name, "stopped")
                self._complete_lifecycle_operation(op_id, detail="pipeline stopped")
                logger.info("Stopped pipeline", extra={"pipeline": name})
            except Exception as exc:
                self._complete_lifecycle_operation(
                    op_id, state="failed", detail=f"stop failed: {exc}"
                )
                raise

    def restart_pipeline(self, name: str) -> None:
        """Restart a pipeline — stop active execution then immediately reschedule.

        Works in both standalone and manager+worker mode.  For stream pipelines
        in manager mode the stream dispatch is cancelled and re-dispatched
        (potentially on a different worker, as determined by the WorkerPool).
        """
        with self._lock:
            state = self.manager.get(name)

            op_id = self._record_lifecycle_operation(
                name, "restart", detail="restarting pipeline",
            )
            try:
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
                    # R16: a restart resets the pipeline's execution context —
                    # queued (pending) runs are terminal-cancelled with the
                    # recorded reason.
                    self._terminal_cancel_pipeline_queued(name, "pipeline_restarted")
                    self._set_queued_depth(name)

                if state.config.enabled and self._may_schedule(name):
                    self._do_schedule(name)
                else:
                    self.manager.set_status(name, "stopped")

                self._complete_lifecycle_operation(op_id, detail="pipeline restarted")
                logger.info("Restarted pipeline", extra={"pipeline": name})
            except Exception as exc:
                self._complete_lifecycle_operation(
                    op_id, state="failed", detail=f"restart failed: {exc}"
                )
                raise

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
            # V18-04 §1: the queue reservation row — the run_intents row the
            # drain claim converts into the guard (frozen §2: a queued request
            # holds a reservation, not the guard; it acquires the guard only
            # at claim).
            schedule_type = "manual"
            state = self.manager.get(pipeline_name) if self.manager.exists(pipeline_name) else None
            if state is not None:
                schedule_type = state.config.schedule.type
            self._ensure_run_intent(
                run_id=run_id, pipeline_name=pipeline_name, origin="queued",
                flush=False, requested_at=now, expires_at=expires_at,
                requested_generation=self._pipeline_generation(pipeline_name),
                yaml_snapshot=yaml_text, schedule_type=schedule_type,
            )
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

    # ── Execution ledger wiring (V18-04 / frozen V18-01 §1–3) ─────────────

    def _pipeline_generation(self, pipeline_name: str) -> int:
        """Current ledger generation for a pipeline (1 when no desired-state row)."""
        if self._db is None:
            return 1
        with self._db._engine.connect() as conn:  # noqa: SLF001 — db._engine is the repo convention
            row = conn.execute(
                text(
                    "SELECT generation FROM pipeline_desired_state "
                    "WHERE pipeline_name = :name"
                ),
                {"name": pipeline_name},
            ).mappings().fetchone()
        if row is None or row["generation"] is None:
            return 1
        return int(row["generation"])

    def _ensure_desired_state(self, pipeline_name: str, *, desired_status: str = "running") -> None:
        """Create the pipeline_desired_state row at generation 1 when absent."""
        if self._db is None:
            return
        with self._db._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO pipeline_desired_state
                    (pipeline_name, desired_status, generation, schedule_type,
                     misfire_policy, deleted, updated_at)
                VALUES (:name, :desired_status, 1, 'manual', 'coalesced_skip', 0, :now)
                ON CONFLICT (pipeline_name) DO NOTHING
            """), {
                "name": pipeline_name,
                "desired_status": desired_status,
                "now": datetime.now(UTC).isoformat(),
            })

    def _set_desired_status(self, pipeline_name: str, desired_status: str) -> None:
        """Keep ``pipeline_desired_state.desired_status`` current (plan D).

        stop/start write both the legacy stopped flag and the desired-state
        row so the boot-time desired-state load (V18-06) never resurrects an
        operator stop or keeps a started pipeline stopped. A missing row
        (pre-M3 edge) is a no-op — the legacy flag governs there.
        """
        if self._db is None:
            return
        with self._db._engine.begin() as conn:
            conn.execute(text("""
                UPDATE pipeline_desired_state
                   SET desired_status = :status, updated_at = :now
                 WHERE pipeline_name = :name
            """), {
                "name": pipeline_name,
                "status": desired_status,
                "now": datetime.now(UTC).isoformat(),
            })

    def _bump_pipeline_generation(self, pipeline_name: str, *, deleted: bool = False) -> int:
        """Increment the ledger generation (frozen §1: config update / delete
        tombstone). Returns the new generation."""
        generation = self._pipeline_generation(pipeline_name) + 1
        if self._db is None:
            return generation
        with self._db._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO pipeline_desired_state
                    (pipeline_name, desired_status, generation, schedule_type,
                     misfire_policy, deleted, updated_at)
                VALUES (:name, 'running', :gen, 'manual', 'coalesced_skip', :deleted, :now)
                ON CONFLICT (pipeline_name) DO UPDATE
                    SET generation = :gen, deleted = :deleted, updated_at = :now
            """), {
                "name": pipeline_name,
                "gen": generation,
                "deleted": 1 if deleted else 0,
                "now": datetime.now(UTC).isoformat(),
            })
        return generation

    def _ensure_run_intent(
        self,
        *,
        run_id: str,
        pipeline_name: str,
        origin: str,
        flush: bool,
        requested_at: datetime,
        requested_generation: int,
        yaml_snapshot: str | None,
        schedule_type: str,
        expires_at: datetime | None = None,
    ) -> None:
        """Idempotently insert the run_intents row (frozen §3) — the queue
        reservation the claim converts into the guard.

        V18-06 (M4 reconciliation): an existing intent whose
        ``requested_generation`` is the M4 backfill artifact (1, written for
        legacy in-flight queued rows) is refreshed to the caller's generation
        at claim time — the attempt's generation is the fence authority, never
        a stale backfilled value. The refresh applies only while the intent is
        still unresolved; a resolved row is immutable audit.
        """
        if self._db is None:
            return
        with self._db._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO run_intents
                    (run_id, pipeline_name, origin, flush, requested_at, expires_at,
                     requested_generation, yaml_snapshot, schedule_type)
                VALUES
                    (:run_id, :pipeline_name, :origin, :flush, :requested_at, :expires_at,
                     :requested_generation, :yaml_snapshot, :schedule_type)
                ON CONFLICT (run_id) DO UPDATE
                    SET requested_generation = :requested_generation
                 WHERE run_intents.final_outcome IS NULL
            """), {
                "run_id": run_id,
                "pipeline_name": pipeline_name,
                "origin": origin,
                "flush": 1 if flush else 0,
                "requested_at": requested_at.isoformat(),
                "expires_at": expires_at.isoformat() if expires_at else None,
                "requested_generation": requested_generation,
                "yaml_snapshot": yaml_snapshot,
                "schedule_type": schedule_type,
            })

    def _next_attempt_ordinal(self, run_id: str) -> int:
        """Next 1-based attempt ordinal for a run (1 when no attempt rows exist).

        A ledger-authorized replacement (e.g. queue re-entry after a
        no-capacity terminal) mints N+1 under the same run_id (frozen §1).
        """
        if self._db is None:
            return 1
        with self._db._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT COALESCE(MAX(ordinal), 0) AS m FROM execution_attempts "
                    "WHERE run_id = :run_id"
                ),
                {"run_id": run_id},
            ).mappings().fetchone()
        return int(row["m"]) + 1 if row else 1

    def _active_attempt_for_run(self, run_id: str) -> dict | None:
        """The newest non-terminal ledger attempt for a run, or None."""
        if self._db is None:
            return None
        with self._db._engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT attempt_id, run_id, pipeline_name, generation, state, slot_id
                      FROM execution_attempts
                     WHERE run_id = :run_id AND state != 'terminal'
                     ORDER BY ordinal DESC LIMIT 1
                """),
                {"run_id": run_id},
            ).mappings().fetchone()
        return dict(row) if row is not None else None

    def _mark_attempt_dispatching(self, *, attempt_id: str, run_id: str, generation: int) -> None:
        """claimed → dispatching + dispatch_sent_at (the manager sends /agent/run).

        ``dispatch_sent_at`` is the frozen §2 boot-adoption discriminator (a
        claimed-but-unsent attempt is resolved locally at boot, never
        ``unknown``).
        """
        if self._db is None:
            return
        with self._db._engine.begin() as conn:
            conn.execute(text("""
                UPDATE execution_attempts
                   SET state = 'dispatching', dispatch_sent_at = :now
                 WHERE attempt_id = :attempt_id AND run_id = :run_id
                   AND state = 'claimed' AND generation = :generation
            """), {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "generation": generation,
                "now": datetime.now(UTC).isoformat(),
            })

    def _mark_attempt_running(self, *, attempt_id: str, run_id: str, generation: int) -> None:
        """dispatching → running + accepted_at (the 202 acceptance advances the
        ledger attempt; the worker never writes the ledger, frozen §2)."""
        if self._db is None:
            return
        with self._db._engine.begin() as conn:
            conn.execute(text("""
                UPDATE execution_attempts
                   SET state = 'running', accepted_at = :now
                 WHERE attempt_id = :attempt_id AND run_id = :run_id
                   AND state = 'dispatching' AND generation = :generation
            """), {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "generation": generation,
                "now": datetime.now(UTC).isoformat(),
            })

    def _terminalize_attempt(
        self,
        *,
        attempt_id: str,
        run_id: str,
        pipeline_name: str,
        generation: int,
        resolve_outcome: str | None = None,
        cancel_reason: str | None = None,
    ) -> None:
        """Fenced attempt → terminal, guard released by identity, and optional
        intent resolution — one transaction (frozen §3 statements, rowcount-
        fenced). Idempotent: an already-terminal attempt, a resolved intent, or
        a guard held by a newer attempt all fence out as 0-row no-ops.

        The frozen completion transition is running → terminal; the state
        IN ('claimed','dispatching','running') fence additionally tolerates
        the fast-run race (a completion arriving before the dispatch commit
        advances the attempt). The identity/generation fence is unchanged.
        ``cancel_reason`` records why a non-completion terminal landed (boot
        adoption, cancellation, revocation).
        """
        if self._db is None:
            return
        now = datetime.now(UTC).isoformat()
        with self._db._engine.begin() as conn:
            conn.execute(text("""
                UPDATE execution_attempts
                   SET state = 'terminal', finished_at = :now,
                       cancel_reason = COALESCE(:cancel_reason, cancel_reason)
                 WHERE attempt_id = :attempt_id AND run_id = :run_id
                   AND generation = :generation
                   AND state IN ('claimed', 'dispatching', 'running')
            """), {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "generation": generation,
                "now": now,
                "cancel_reason": cancel_reason,
            })
            if resolve_outcome is not None:
                conn.execute(text("""
                    UPDATE run_intents
                       SET final_outcome = :outcome, final_attempt_id = :attempt_id,
                           resolved_at = :now
                     WHERE run_id = :run_id AND final_outcome IS NULL
                """), {
                    "outcome": resolve_outcome,
                    "attempt_id": attempt_id,
                    "run_id": run_id,
                    "now": now,
                })
            conn.execute(text("""
                UPDATE execution_guards
                   SET run_id = NULL, attempt_id = NULL, generation = NULL,
                       acquired_at = NULL
                 WHERE guard_key = :guard_key AND attempt_id = :attempt_id
                   AND run_id = :run_id
            """), {"guard_key": pipeline_name, "attempt_id": attempt_id, "run_id": run_id})

    def _claim_for_dispatch(
        self,
        pipeline_name: str,
        run_id: str,
        *,
        origin: str,
        flush: bool,
        yaml_text: str,
        schedule_type: str,
        ordinal: int = 1,
    ) -> dict | None:
        """Insert the run intent and acquire the ledger guard before dispatch.

        Returns the claim as a dict (attempt_id/generation/pipeline_name) on
        CLAIMED or ALREADY_HELD (an idempotent re-claim — the guard already
        holds this exact attempt), or None when the guard is held by a
        different attempt (LOST) or the intent is absent/resolved (NO_INTENT)
        — the caller surfaces today's already-running behavior.
        """
        generation = self._pipeline_generation(pipeline_name)
        now = datetime.now(UTC)
        self._ensure_run_intent(
            run_id=run_id, pipeline_name=pipeline_name, origin=origin, flush=flush,
            requested_at=now, expires_at=None, requested_generation=generation,
            yaml_snapshot=yaml_text, schedule_type=schedule_type,
        )
        claim = ledger.claim_run(
            self._db._engine,
            guard_key=pipeline_name,
            guard_kind="batch",
            pipeline_name=pipeline_name,
            run_id=run_id,
            generation=generation,
            ordinal=ordinal,
            yaml_snapshot=yaml_text,
        )
        if claim.status == ledger.LOST or claim.status == ledger.NO_INTENT:
            logger.warning(
                "Batch claim refused — another attempt holds the run guard",
                extra={
                    "pipeline": pipeline_name,
                    "run_id": run_id,
                    "status": claim.status,
                },
            )
            return None
        self._mark_attempt_dispatching(
            attempt_id=claim.attempt_id, run_id=run_id, generation=generation,
        )
        return {
            "attempt_id": claim.attempt_id,
            "run_id": run_id,
            "pipeline_name": pipeline_name,
            "generation": generation,
            "slot_id": "",
        }

    def _record_attempt_worker(
        self, *, attempt_id: str, run_id: str, generation: int, worker_url: str
    ) -> None:
        """Record the owning worker_id on the attempt row.

        Boot adoption needs it to find the worker's journal after a manager
        crash (frozen §2: dispatching/running rows resolve against the owning
        worker). Best-effort: an unknown worker_id stays empty and adoption
        falls back to probing every worker.
        """
        if self._db is None:
            return
        worker_id = ""
        if self._worker_pool is not None:
            resolved = self._worker_pool.worker_id_for_url(worker_url)
            worker_id = resolved if isinstance(resolved, str) else ""
        with self._db._engine.begin() as conn:
            conn.execute(text("""
                UPDATE execution_attempts
                   SET worker_id = :worker_id
                 WHERE attempt_id = :attempt_id AND run_id = :run_id
                   AND generation = :generation
            """), {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "generation": generation,
                "worker_id": worker_id,
            })

    def _terminal_cancel_queued_run(self, run_id: str, reason: str) -> None:
        """Mark one run's queued_runs row terminal ('cancelled') when it is
        still queued/dispatching (boot adoption companion)."""
        if self._db is None:
            return
        with self._db._engine.begin() as conn:
            conn.execute(text("""
                UPDATE queued_runs
                   SET status = 'cancelled', terminal_reason = :reason
                 WHERE run_id = :run_id AND status IN ('queued', 'dispatching')
            """), {"run_id": run_id, "reason": reason})

    def _terminal_cancel_pipeline_queued(self, pipeline_name: str, reason: str) -> int:
        """R16: terminal-cancel a pipeline's queued (pending) run rows.

        Non-terminal rows are marked 'cancelled' with the recorded reason and
        kept as audit under the returned run_id — the old purge made a
        previously returned run_id cease to resolve. The E.2 drain only reads
        status='queued' rows, so cancelled rows are skipped by construction.
        Run intents whose runs never claimed an attempt (no non-terminal
        attempt row) resolve 'aborted' here; intents owned by a live attempt
        are left to its completion path. Returns the number of cancelled rows.
        """
        if self._db is None:
            return 0
        now = datetime.now(UTC).isoformat()
        with self._db._engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE queued_runs
                   SET status = 'cancelled', terminal_reason = :reason
                 WHERE pipeline_name = :pipeline_name
                   AND status IN ('queued', 'dispatching')
            """), {"pipeline_name": pipeline_name, "reason": reason})
            conn.execute(text("""
                UPDATE run_intents
                   SET final_outcome = 'aborted', resolved_at = :now
                 WHERE pipeline_name = :pipeline_name
                   AND final_outcome IS NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM execution_attempts ea
                        WHERE ea.run_id = run_intents.run_id
                          AND ea.state != 'terminal'
                   )
            """), {"pipeline_name": pipeline_name, "now": now})
        return result.rowcount

    def _prune_orphaned_run_intents(self, pipeline_name: str) -> int:
        """Resolve every unresolved run_intent for a deleted pipeline.

        Task 5 (R16 audit): delete/stop purges used to leave orphaned
        unresolved intents forever. Resolution (rather than row deletion) keeps
        the run_id resolvable in the audit trail (invariant 8).
        """
        if self._db is None:
            return 0
        with self._db._engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE run_intents
                   SET final_outcome = 'aborted', resolved_at = :now
                 WHERE pipeline_name = :pipeline_name
                   AND final_outcome IS NULL
            """), {
                "pipeline_name": pipeline_name,
                "now": datetime.now(UTC).isoformat(),
            })
        return result.rowcount

    def _record_lifecycle_operation(
        self,
        pipeline_name: str,
        op_kind: str,
        *,
        state: str = "pending",
        attempt_id: str | None = None,
        detail: str | None = None,
    ) -> str | None:
        """Insert a lifecycle_operations row (frozen §3). Returns the
        operation_id (the V18-09 API shape is a later lane — only the table
        write + internal queries live here)."""
        if self._db is None:
            return None
        operation_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        with self._db._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO lifecycle_operations
                    (operation_id, pipeline_name, op_kind, state, attempt_id,
                     detail, created_at, updated_at)
                VALUES
                    (:operation_id, :pipeline_name, :op_kind, :state, :attempt_id,
                     :detail, :now, :now)
            """), {
                "operation_id": operation_id,
                "pipeline_name": pipeline_name,
                "op_kind": op_kind,
                "state": state,
                "attempt_id": attempt_id,
                "detail": detail,
                "now": now,
            })
        return operation_id

    def _complete_lifecycle_operation(
        self,
        operation_id: str | None,
        *,
        state: str = "complete",
        detail: str | None = None,
    ) -> None:
        """pending → complete|failed on an existing lifecycle_operations row."""
        if self._db is None or operation_id is None:
            return
        with self._db._engine.begin() as conn:
            conn.execute(text("""
                UPDATE lifecycle_operations
                   SET state = :state, detail = :detail, updated_at = :now
                 WHERE operation_id = :operation_id
            """), {
                "operation_id": operation_id,
                "state": state,
                "detail": detail,
                "now": datetime.now(UTC).isoformat(),
            })

    def _record_completed_lifecycle_operation(
        self,
        pipeline_name: str,
        op_kind: str,
        *,
        attempt_id: str | None = None,
        detail: str | None = None,
    ) -> str | None:
        """pending → complete back-to-back for already-synchronous resolutions
        (boot adoption); the four lifecycle ops use the explicit two-step form
        so the work happens between pending and complete."""
        operation_id = self._record_lifecycle_operation(
            pipeline_name, op_kind, state="pending",
            attempt_id=attempt_id, detail=detail,
        )
        self._complete_lifecycle_operation(operation_id, detail=detail)
        return operation_id

    def get_lifecycle_operations(
        self, pipeline_name: str | None = None, limit: int = 50
    ) -> list[dict]:
        """Internal query over lifecycle_operations (the V18-09 API exposes
        the 202+operation_id shape; only internal reads live here)."""
        if self._db is None:
            return []
        sql = (
            "SELECT operation_id, pipeline_name, op_kind, state, attempt_id, "
            "detail, created_at, updated_at FROM lifecycle_operations"
        )
        params: dict = {}
        if pipeline_name is not None:
            sql += " WHERE pipeline_name = :pipeline_name"
            params["pipeline_name"] = pipeline_name
        sql += f" ORDER BY created_at DESC LIMIT {int(limit)}"
        with self._db._engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _intent_outcome(status: RunStatus) -> str:
        """Map a run status to the frozen run_intents.final_outcome domain."""
        if status == RunStatus.SUCCESS:
            return "success"
        if status == RunStatus.PARTIAL:
            return "partial"
        if status == RunStatus.ABORTED:
            return "aborted"
        return "failed"

    def _record_skipped_manual_run(self, pipeline_name: str, run_id: str, reason: str) -> None:
        """FAILED run-history row for a manual run that lost the claim (GH #47):
        the client's run_id must resolve instead of 404ing forever. The winning
        run's status is not clobbered to 'error'."""
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
            error=reason,
            node_id=self._node_id,
        )
        self.manager.record_run(pipeline_name, result)

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
        when the row was claimed, purged, or expired elsewhere.

        V18-04 §1: the queue reservation then converts into the ledger guard —
        the run_intents row is ensured and ``ledger.claim_run`` acquires it
        (frozen §2: a queued request holds a reservation, not the guard). A
        lost claim (another attempt holds the guard) reverts the queued fence
        and returns None; an ALREADY_HELD re-claim (queue re-entry after a
        no-capacity terminal) proceeds with the existing attempt.
        """
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
            attempt = self._claim_for_dispatch(
                name,
                run_id,
                origin="queued",
                flush=bool(row.get("flush", 0)),
                yaml_text=row["yaml_snapshot"],
                schedule_type=state.config.schedule.type,
                ordinal=self._next_attempt_ordinal(run_id),
            )
            if attempt is None:
                # Lost the ledger guard (another attempt holds it) — undo the
                # queued fence so the row stays drainable for a later pass.
                self._db.revert_queued_run_row(run_id)
                return None
            if self._worker_pool is not None:
                self._worker_pool.register_attempt(
                    run_id,
                    attempt["attempt_id"],
                    attempt["generation"],
                    attempt.get("slot_id", ""),
                )
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
                "attempt_id": attempt["attempt_id"],
                "generation": attempt["generation"],
            }

    def commit_queued_dispatch(self, run_id: str, worker_url: str) -> bool:
        """dispatching → dispatched + CAS: re-check pipeline exists under the
        lock, record the _active_batch_runs lease (schedule_type from current
        config), set pipeline status 'running', db.mark_queued_run_dispatched,
        MGR_DISPATCH_TOTAL{accepted} + MGR_QUEUE_DISPATCHED_TOTAL + wait histogram.

        Fast-run race (GH #47, same defect class as the _run_batch post-dispatch
        CAS): a sub-second worker run can complete and post run-complete before
        this commit re-acquires the lock. When the run is already recorded, the
        lease is SKIPPED — recording it would make the BatchReconciler probe
        is_run_active() → False and mark the succeeded run as lost — and the
        pipeline status is not flipped to 'running' (the post-run transition
        already ran). The queued row still transitions dispatching → dispatched
        so the drain loop never re-claims a completed run.
        """
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
            run_already_completed = self.manager.get_run(run_id) is not None
            if not run_already_completed:
                self._active_batch_runs[name] = _ActiveBatchRun(
                    run_id=run_id,
                    pipeline_name=name,
                    worker_url=worker_url,
                    schedule_type=state.config.schedule.type,
                    started_at=datetime.now(UTC),
                )
                self.manager.set_status(name, "running")
            self._db.mark_queued_run_dispatched(run_id, datetime.now(UTC))
            # V18-04: the ledger attempt advances dispatching → running on the
            # acceptance (frozen §2 note: the manager advances the row when it
            # receives the 202 acceptance). Idempotent — a fast-run completion
            # already terminalled the attempt, so the fence finds nothing.
            attempt = self._active_attempt_for_run(run_id)
            if attempt is not None:
                self._mark_attempt_running(
                    attempt_id=attempt["attempt_id"],
                    run_id=run_id,
                    generation=attempt["generation"],
                )
                # Boot adoption needs the owning worker after a manager crash.
                self._record_attempt_worker(
                    attempt_id=attempt["attempt_id"],
                    run_id=run_id,
                    generation=attempt["generation"],
                    worker_url=worker_url,
                )
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
                extra={
                    "pipeline": name,
                    "run_id": run_id,
                    "worker": worker_url,
                    "wait_seconds": round(wait, 1),
                    "lease_recorded": not run_already_completed,
                },
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
            # V18-04: the dispatch attempt is retired — terminal the attempt
            # (capacity/rejection reason) and release the guard; the run intent
            # is left unresolved so the next drain re-enters as a new attempt
            # (frozen §2 503 rule / queue re-entry policy).
            attempt = self._active_attempt_for_run(run_id)
            if attempt is not None:
                self._terminalize_attempt(
                    attempt_id=attempt["attempt_id"],
                    run_id=run_id,
                    pipeline_name=attempt["pipeline_name"],
                    generation=attempt["generation"],
                    resolve_outcome=None,
                )
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
            # V18-04: the expired run's intent is terminal ('expired').
            attempt = self._active_attempt_for_run(run_id)
            ledger.resolve_intent(
                self._db._engine,
                run_id=run_id,
                outcome="expired",
                attempt_id=attempt["attempt_id"] if attempt is not None else "",
            )
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

    def get_run_attempts(self, run_id: str) -> list[dict]:
        """Ledger attempts for a run in the frozen §8 API shape:
        ``[{attempt_id, state, worker_id, started_at, finished_at}]``."""
        if self._db is None:
            return []
        with self._db._engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT attempt_id, state, worker_id, started_at, finished_at,
                       generation, ordinal
                  FROM execution_attempts
                 WHERE run_id = :run_id
                 ORDER BY ordinal
            """), {"run_id": run_id}).mappings().fetchall()
        return [dict(r) for r in rows]

    def get_run_ledger_context(self, run_id: str) -> dict:
        """Ledger context for the extended ``GET /runs/{run_id}``:
        ``{attempts, generation, outcome}``.

        The attempt's generation is the fence authority (never an intent's
        backfilled ``requested_generation`` — M4 rows carry the artifact
        value 1). ``outcome`` is the resolved intent outcome when present.
        ``attempts`` carries the frozen §8 shape (attempt_id, state,
        worker_id, started_at, finished_at).
        """
        attempts = self.get_run_attempts(run_id)
        intent = self._intent_for_run(run_id)
        generation = None
        if attempts:
            # The newest attempt (last by ordinal) carries the authoritative
            # generation — never an intent's backfilled requested_generation.
            generation = attempts[-1]["generation"]
        elif intent is not None:
            generation = intent.get("requested_generation")
        outcome = None
        if intent is not None and intent.get("final_outcome"):
            outcome = intent["final_outcome"]
        return {
            "attempts": [
                {
                    "attempt_id": a["attempt_id"],
                    "state": a["state"],
                    "worker_id": a.get("worker_id"),
                    "started_at": a.get("started_at"),
                    "finished_at": a.get("finished_at"),
                }
                for a in attempts
            ],
            "generation": generation,
            "outcome": outcome,
        }

    def _intent_for_run(self, run_id: str) -> dict | None:
        """The run_intents row for a run (outcome/generation context)."""
        if self._db is None:
            return None
        with self._db._engine.connect() as conn:
            row = conn.execute(text("""
                SELECT run_id, requested_generation, final_outcome,
                       final_attempt_id, resolved_at
                  FROM run_intents
                 WHERE run_id = :run_id
            """), {"run_id": run_id}).mappings().fetchone()
        return dict(row) if row is not None else None

    def _run_history_v18_columns(self, run_id: str) -> dict:
        """The M2 V18 columns on the run_history row, when present."""
        if self._db is None:
            return {}
        with self._db._engine.connect() as conn:
            row = conn.execute(text("""
                SELECT attempt_id, generation, outcome, disposition_json
                  FROM run_history
                 WHERE run_id = :run_id
            """), {"run_id": run_id}).mappings().fetchone()
        return dict(row) if row is not None else {}

    @staticmethod
    def _run_ledger_state(attempts: list[dict]) -> str:
        """Run-level state from the newest ledger attempt.

        The newest non-terminal attempt's state is authoritative
        (claimed/dispatching/running/stopping/unknown); a run whose newest
        attempt is terminal (or that has no attempts) is ``terminal`` — the
        history row records its outcome.
        """
        for attempt in reversed(attempts):
            if attempt.get("state") != "terminal":
                return str(attempt["state"])
        return "terminal"

    def get_run_detail(self, run_id: str) -> dict | None:
        """V18-06 §8: extended ``GET /api/runs/{run_id}`` — additive over the
        legacy ``RunResult.to_dict()`` shape (old keys unchanged).

        Adds ``state``, ``generation``, ``attempts[]`` (attempt_id, state,
        worker_id, started/finished) from the ledger, plus ``outcome`` and the
        per-sink/dlq/spool/failed counters where recorded (run_history V18
        columns; the full API reshape is V18-09). Returns None when neither a
        history row nor a queued row exists.
        """
        if self._db is None:
            result = self.get_run(run_id)
            return result.to_dict() if result is not None else None
        result = self.get_run(run_id)
        if result is None:
            return None
        attempts = self.get_run_attempts(run_id)
        intent = self._intent_for_run(run_id)
        history_cols = self._run_history_v18_columns(run_id)

        payload = result.to_dict()
        payload["state"] = self._run_ledger_state(attempts)
        if attempts:
            # The newest attempt (last by ordinal) carries the authoritative
            # generation — never an intent's backfilled requested_generation.
            payload["generation"] = attempts[-1]["generation"]
        elif history_cols.get("generation") is not None:
            payload["generation"] = history_cols["generation"]
        elif intent is not None:
            payload["generation"] = intent.get("requested_generation")
        else:
            payload["generation"] = None

        if history_cols.get("outcome"):
            payload["outcome"] = history_cols["outcome"]
        elif intent is not None and intent.get("final_outcome"):
            payload["outcome"] = intent["final_outcome"]
        elif payload.get("status") in ("success", "partial", "failed", "aborted"):
            payload["outcome"] = payload["status"]
        else:
            payload["outcome"] = None

        disposition = history_cols.get("disposition_json")
        if disposition:
            try:
                decoded = json.loads(disposition)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, dict) and decoded:
                payload["disposition"] = decoded

        payload["attempts"] = [
            {
                "attempt_id": a["attempt_id"],
                "state": a["state"],
                "worker_id": a.get("worker_id"),
                "started_at": a.get("started_at"),
                "finished_at": a.get("finished_at"),
            }
            for a in attempts
        ]
        return payload

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
                    self._record_skipped_manual_run(
                        pipeline_name,
                        run_id,
                        f"Manual run skipped: previous run still active or "
                        f"queued (status={state.status})",
                    )
                return

            self.manager.set_status(pipeline_name, "running")
            if run_id is None:
                run_id = str(uuid.uuid4())

            # Snapshot everything the unlocked phases need so a concurrent
            # update()/delete() cannot swap the config under us after the claim.
            config = state.config
            yaml_text = state.yaml_text
            schedule_type = config.schedule.type

            # V18-04 §1: durable claim before dispatch (worker mode only —
            # standalone keeps today's path). The run_intents row is ensured,
            # then the ledger guard is acquired. A LOST claim (the guard is
            # held by another attempt — a run the in-memory status missed)
            # surfaces today's already-running behavior, never a crash.
            attempt = None
            if self._worker_pool is not None and self._db is not None:
                attempt = self._claim_for_dispatch(
                    pipeline_name,
                    run_id,
                    origin=origin,
                    flush=flush,
                    yaml_text=yaml_text,
                    schedule_type=schedule_type,
                )
                if attempt is None:
                    if origin == "manual" and run_id is not None:
                        self._record_skipped_manual_run(
                            pipeline_name,
                            run_id,
                            "Manual run skipped: another attempt holds the run guard",
                        )
                    return

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
                attempt_id=attempt["attempt_id"] if attempt is not None else None,
                generation=attempt["generation"] if attempt is not None else None,
            )
            if outcome.outcome == DISPATCH_NO_CAPACITY:
                # E.2 (§4.2): the fallback enqueue site — capacity vanished
                # between the synchronous trigger check and this dispatch. Queue
                # only manual-origin runs (scheduled runs self-retry on their
                # interval). DISPATCH_FAILED is intentionally NOT queued — it
                # keeps today's fail-fast with its truthful error label
                # (bug-inheritance guard #1).
                if origin == "manual" and self._queue_manual_runs and self._db is not None:
                    if attempt is not None:
                        # Frozen 503 rule: the attempt is terminal with a
                        # capacity reason and the intent stays unresolved — the
                        # queued re-entry dispatches as a new attempt (N+1).
                        self._terminalize_attempt(
                            attempt_id=attempt["attempt_id"],
                            run_id=run_id,
                            pipeline_name=pipeline_name,
                            generation=attempt["generation"],
                            resolve_outcome=None,
                        )
                    if self._enqueue_manual_run(pipeline_name, run_id, yaml_text):
                        return  # queued — no FAILED row, no finalize
                    # Dedupe hit: the synchronous trigger_run site already queued
                    # this pipeline (different run_id). _enqueue_manual_run is
                    # check-then-insert under the RLock, so no row was created for
                    # this run_id — nothing to clean up.
                    return
                if attempt is not None:
                    self._terminalize_attempt(
                        attempt_id=attempt["attempt_id"],
                        run_id=run_id,
                        pipeline_name=pipeline_name,
                        generation=attempt["generation"],
                        resolve_outcome="failed",
                    )
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
                if attempt is not None:
                    # Authoritative rejection (4xx/5xx non-410/503): the attempt
                    # is terminal with reason dispatch_rejected, the guard is
                    # released, and the intent resolves 'failed' (frozen §2).
                    self._terminalize_attempt(
                        attempt_id=attempt["attempt_id"],
                        run_id=run_id,
                        pipeline_name=pipeline_name,
                        generation=attempt["generation"],
                        resolve_outcome="failed",
                    )
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
                        if attempt is not None:
                            self._terminalize_attempt(
                                attempt_id=attempt["attempt_id"],
                                run_id=run_id,
                                pipeline_name=pipeline_name,
                                generation=attempt["generation"],
                                resolve_outcome="aborted",
                            )
                        return
                    if self.manager.get(pipeline_name).config is not config:
                        logger.warning(
                            "Batch dispatch completed for replaced pipeline — not tracked",
                            extra={"pipeline": pipeline_name, "run_id": run_id},
                        )
                        if attempt is not None:
                            # The manager retired the run's tracking; retire
                            # the attempt and free the guard so a newer run can
                            # claim. The eventual completion resolves the intent
                            # idempotently and cannot touch a newer guard.
                            self._terminalize_attempt(
                                attempt_id=attempt["attempt_id"],
                                run_id=run_id,
                                pipeline_name=pipeline_name,
                                generation=attempt["generation"],
                                resolve_outcome=None,
                            )
                        return
                    if self.manager.get_run(run_id) is not None:
                        logger.info(
                            "Batch dispatch completed after run-complete callback — not tracked",
                            extra={"pipeline": pipeline_name, "run_id": run_id},
                        )
                        # C7: the dispatch WAS accepted (a fast worker ran the
                        # run) — count it like the normal path and the queued
                        # drain path, which kept its accepted increment even
                        # when the lease was skipped. The completion path already
                        # committed the ledger for this attempt.
                        from tram.metrics.registry import MGR_DISPATCH_TOTAL
                        MGR_DISPATCH_TOTAL.labels(pipeline=pipeline_name, result="accepted").inc()
                        return
                    if attempt is not None:
                        # 202 acceptance advances the ledger attempt to running.
                        self._mark_attempt_running(
                            attempt_id=attempt["attempt_id"],
                            run_id=run_id,
                            generation=attempt["generation"],
                        )
                        # Boot adoption needs the owning worker after a crash.
                        self._record_attempt_worker(
                            attempt_id=attempt["attempt_id"],
                            run_id=run_id,
                            generation=attempt["generation"],
                            worker_url=outcome.worker_url,
                        )
                    self._active_batch_runs[pipeline_name] = _ActiveBatchRun(
                        run_id=run_id,
                        pipeline_name=pipeline_name,
                        worker_url=outcome.worker_url,
                        schedule_type=schedule_type,
                        started_at=datetime.now(UTC),
                        attempt_id=attempt["attempt_id"] if attempt is not None else None,
                        generation=attempt["generation"] if attempt is not None else None,
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
                # V18-04: identity-checked cleanup — a duplicate callback for a
                # retired run must not pop a newer run's lease (frozen §2:
                # late/duplicate callbacks record their own diagnostics only
                # and cannot touch a newer guard, status, or generation).
                self._pop_batch_lease_for_run(pipeline_name, run_id)
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
                self._pop_batch_lease_for_run(pipeline_name, run_id)
                self._remove_stream_run_id(pipeline_name, run_id)
                if self._stream_run_ids.get(pipeline_name):
                    return
                if self._stats_store is not None:
                    self._stats_store.remove(run_id)

            # V18-04: legacy-shaped completions (no attempt_id on the wire —
            # a v1.7 worker) still resolve the ledger best-effort by run
            # identity: attempt → terminal, intent resolved, guard released.
            # Idempotent — the fenced statements are 0-row no-ops for an
            # already-terminal attempt, a resolved intent, or a newer guard.
            if self._db is not None:
                attempt = self._active_attempt_for_run(run_id)
                if attempt is not None:
                    self._terminalize_attempt(
                        attempt_id=attempt["attempt_id"],
                        run_id=run_id,
                        pipeline_name=attempt["pipeline_name"],
                        generation=attempt["generation"],
                        resolve_outcome=self._intent_outcome(run_status),
                    )

            if self.manager.exists(pipeline_name):
                self._finalize_batch_result(pipeline_name, result)
            else:
                logger.warning(
                    "on_worker_run_complete: pipeline not found",
                    extra={"pipeline": pipeline_name, "run_id": run_id},
                )

    def _pop_batch_lease_for_run(self, pipeline_name: str, run_id: str) -> None:
        """Pop the active batch lease only when it belongs to *run_id* (R5:
        never name-keyed — a late callback for a retired run must not clear a
        newer run's lease)."""
        lease = self._active_batch_runs.get(pipeline_name)
        if lease is not None and lease.run_id == run_id:
            self._active_batch_runs.pop(pipeline_name, None)

    def on_attempt_run_complete(
        self,
        *,
        attempt_id: str,
        generation: int,
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
    ) -> dict:
        """Identity-checked run-complete (frozen §2: running → terminal).

        Resolves conditionally on the exact attempt_id + generation: intent
        resolution is idempotent for the winner, the attempt → terminal
        transition is fenced, and the guard is released by identity — one
        transaction, and 200 is returned only after that commit. The
        run-history/status path reuses the legacy completion body, which is
        itself identity-safe (never name-keyed).

        Returns ``{"ok": True}`` after the commit, or
        ``{"ok": True, "ignored": <reason>}`` with diagnostics for an
        unknown/mismatched attempt (nothing is committed; the worker stops
        retrying). With no ledger (``db is None``) the handler degrades to
        today's path.
        """
        if self._db is None:
            self.on_worker_run_complete(
                run_id=run_id, pipeline_name=pipeline_name, worker_id=worker_id,
                status=status, records_in=records_in, records_out=records_out,
                records_skipped=records_skipped, bytes_in=bytes_in, bytes_out=bytes_out,
                error=error, errors=errors, started_at=started_at, finished_at=finished_at,
            )
            return {"ok": True}

        attempt = ledger.get_attempt(self._db._engine, attempt_id)
        if attempt is None:
            logger.warning(
                "Ignoring run-complete for unknown attempt",
                extra={"attempt_id": attempt_id, "run_id": run_id, "pipeline": pipeline_name},
            )
            return {"ok": True, "ignored": "unknown_attempt"}
        if (
            attempt["run_id"] != run_id
            or attempt["pipeline_name"] != pipeline_name
            or attempt["generation"] != generation
        ):
            logger.warning(
                "Ignoring run-complete with mismatched attempt identity",
                extra={
                    "attempt_id": attempt_id,
                    "run_id": run_id,
                    "pipeline": pipeline_name,
                    "generation": generation,
                    "ledger_run_id": attempt["run_id"],
                    "ledger_pipeline": attempt["pipeline_name"],
                    "ledger_generation": attempt["generation"],
                },
            )
            return {"ok": True, "ignored": "identity_mismatch"}

        try:
            run_status = RunStatus(status)
        except ValueError:
            run_status = RunStatus.FAILED

        # Ledger commit: attempt → terminal (fenced), intent resolved
        # (idempotent for the winner), guard released by identity.
        self._terminalize_attempt(
            attempt_id=attempt_id,
            run_id=run_id,
            pipeline_name=pipeline_name,
            generation=generation,
            resolve_outcome=self._intent_outcome(run_status),
        )

        self.on_worker_run_complete(
            run_id=run_id, pipeline_name=pipeline_name, worker_id=worker_id,
            status=status, records_in=records_in, records_out=records_out,
            records_skipped=records_skipped, bytes_in=bytes_in, bytes_out=bytes_out,
            error=error, errors=errors, started_at=started_at, finished_at=finished_at,
        )
        return {"ok": True}

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
        local_run = None
        # v1.6.0 (GH #81): crash tracking for the final lifecycle row — a
        # stream that raises records a FAILED row with the crash text instead
        # of a success row.
        crashed = False
        crash_error: str | None = None
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
            crashed = True
            crash_error = str(exc)
            logger.error("Stream pipeline crashed",
                         extra={"pipeline": config.name, "error": str(exc)}, exc_info=True)
            with self._lock:
                if self.manager.exists(config.name):
                    state = self.manager.get(config.name)
                    if state.config is config:
                        self.manager.set_status(config.name, "error")
        finally:
            final_row = None
            if run_id is not None:
                # Remove from dict and StatsStore atomically under the same lock so
                # _emit_local_stats_once() cannot resurrect the entry after removal.
                # The final lifecycle row is computed under the same lock: it reads
                # last_rollup/last_rollup_at, which the stats tick writes only while
                # holding it, so the final segment's delta can never race a rollup.
                with self._local_stats_lock:
                    self._local_active_stats.pop(run_id, None)
                    if self._stats_store is not None:
                        self._stats_store.remove(run_id)
                    if local_run is not None:
                        final_row = self._final_stream_row(
                            local_run, datetime.now(UTC), crashed=crashed, error=crash_error
                        )
            with self._lock:
                # Identity-check the pops: if a newer stream was started for the
                # same name while this (old) thread was stopping, this thread's
                # cleanup must not remove the new stream's bookkeeping.
                if self._stream_threads.get(config.name) is threading.current_thread():
                    self._stream_threads.pop(config.name, None)
                if self._stop_events.get(config.name) is stop_event:
                    self._stop_events.pop(config.name, None)
                if final_row is not None:
                    # B10-guarded inside _commit_stream_row: a pipeline deleted
                    # mid-run is not resurrected by a late lifecycle row.
                    self._commit_stream_row(final_row)
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

    @staticmethod
    def _segment_delta(snapshot: dict, last_rollup: dict[str, int] | None) -> dict[str, int]:
        """Per-segment counters: cumulative snapshot minus the previous boundary.

        With no previous boundary (first segment) the whole snapshot is the
        segment. All stream run-history rows use deltas so the sum of the rows
        equals the lifecycle totals — /api/stats aggregations over run history
        stay correct (v1.6.0, GH #81).
        """
        keys = (
            "records_in", "records_out", "records_skipped",
            "dlq_count", "bytes_in", "bytes_out",
        )
        if last_rollup:
            return {k: int(snapshot.get(k, 0)) - int(last_rollup.get(k, 0)) for k in keys}
        return {k: int(snapshot.get(k, 0)) for k in keys}

    def _commit_stream_row(self, result: RunResult) -> None:
        """Commit one stream run-history row (v1.6.0, GH #81).

        Called under the lifecycle lock. B10-guarded like every other
        record_run site: a pipeline deleted while the stream was in flight must
        not be resurrected by a late row.
        """
        with self._lock:
            if not self.manager.exists(result.pipeline_name):
                logger.warning(
                    "Stream row skipped: pipeline no longer registered",
                    extra={"pipeline": result.pipeline_name, "run_id": result.run_id},
                )
                return
            self.manager.record_run(result.pipeline_name, result)

    def _record_stream_segment(
        self,
        *,
        run_id: str,
        local_run: _LocalRun,
        snapshot: dict,
        started_at: datetime,
        finished_at: datetime,
        status: RunStatus,
        error: str | None = None,
    ) -> None:
        """Record one stream segment row into run history (v1.6.0, GH #81).

        ``snapshot`` carries the segment's DELTA counters plus the segment's
        ``errors_last_window`` strings.
        """
        result = RunResult(
            run_id=run_id,
            pipeline_name=local_run.pipeline_name,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            records_in=int(snapshot.get("records_in", 0)),
            records_out=int(snapshot.get("records_out", 0)),
            records_skipped=int(snapshot.get("records_skipped", 0)),
            bytes_in=int(snapshot.get("bytes_in", 0)),
            bytes_out=int(snapshot.get("bytes_out", 0)),
            dlq_count=int(snapshot.get("dlq_count", 0)),
            error=error,
            node_id=self._node_id,
            errors=list(snapshot.get("errors_last_window", [])),
        )
        self._commit_stream_row(result)

    def _maybe_record_stream_rollup(
        self, local_run: _LocalRun, now: datetime, snapshot: dict
    ) -> None:
        """Periodic single-topology stream run-history rollup (v1.6.0, GH #81).

        A long-lived stream never reaches run history otherwise — batch runs
        persist via ``_finalize_batch_result``, but ``_stream_worker`` only
        ever wrote live stats. Each tick that closes a segment with activity
        records one SUCCESS row with DELTA counts, so the stream is visible in
        run history with in/out/error counts while it runs.

        Bounded row growth for a long-lived stream: quiet segments produce no
        row (no 0-count noise), and a lifecycle stops producing rollup rows
        after ``_STREAM_ROLLUP_ROWS_MAX`` — live StatsStore stats continue and
        the final lifecycle row still lands at stop. Segment rows carry a
        ``-segN`` run_id suffix because ``run_history.run_id`` is the primary
        key; the lifecycle run_id itself is reserved for the final row.

        Caller holds ``_local_stats_lock`` (the same guard that proves the run
        is still active, so a rollup can never land after the final row).
        """
        if local_run.rollup_rows >= _STREAM_ROLLUP_ROWS_MAX:
            return
        errors = list(snapshot.get("errors_last_window", []))
        delta = self._segment_delta(snapshot, local_run.last_rollup)
        activity = (
            delta["records_in"] + delta["records_out"]
            + delta["records_skipped"] + delta["dlq_count"]
        )
        if activity <= 0 and not errors:
            return  # quiet segment — don't record 0-count noise
        started_at = local_run.last_rollup_at or local_run.started_at
        local_run.last_rollup = {k: int(snapshot.get(k, 0)) for k in delta}
        local_run.last_rollup_at = now
        local_run.rollup_rows += 1
        segment_run_id = f"{local_run.run_id}-seg{local_run.rollup_rows}"
        self._record_stream_segment(
            run_id=segment_run_id,
            local_run=local_run,
            snapshot={**delta, "errors_last_window": errors},
            started_at=started_at,
            finished_at=now,
            status=RunStatus.SUCCESS,
        )

    def _final_stream_row(
        self, local_run: _LocalRun, now: datetime, *, crashed: bool, error: str | None
    ) -> RunResult | None:
        """The final lifecycle row for a standalone stream (v1.6.0, GH #81).

        Always recorded (even a 0-count start→stop lifecycle leaves one row),
        carrying the DELTA of the final partial segment since the last rollup
        boundary and the lifecycle ``run_id`` itself — the id the live stats /
        placement views expose. Status is SUCCESS on a clean stop and FAILED on
        a crash, with the crash text as the row error.

        Caller holds ``_local_stats_lock`` so ``last_rollup`` cannot race a
        concurrent stats tick; the row is committed by ``_commit_stream_row``
        under the lifecycle lock after this returns.
        """
        if local_run.schedule_type != "stream":
            return None
        snapshot = local_run.stats.snapshot()
        delta = self._segment_delta(snapshot, local_run.last_rollup)
        status = RunStatus.SUCCESS if not crashed else RunStatus.FAILED
        started_at = local_run.last_rollup_at or local_run.started_at
        return RunResult(
            run_id=local_run.run_id,
            pipeline_name=local_run.pipeline_name,
            status=status,
            started_at=started_at,
            finished_at=now,
            records_in=int(delta["records_in"]),
            records_out=int(delta["records_out"]),
            records_skipped=int(delta["records_skipped"]),
            bytes_in=int(delta["bytes_in"]),
            bytes_out=int(delta["bytes_out"]),
            dlq_count=int(delta["dlq_count"]),
            error=error if crashed else None,
            node_id=self._node_id,
            errors=list(snapshot.get("errors_last_window", [])),
        )

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
            # already stopped and we must not re-insert it (nor record a phantom
            # rollup row after the final lifecycle row). Holding the lock while
            # rolling up also serializes against the stream thread's final-row
            # commit, so a rollup can never land out of order.
            with self._local_stats_lock:
                if run_id not in self._local_active_stats:
                    continue
                self._stats_store.update(payload)
                if local_run.schedule_type == "stream":
                    self._maybe_record_stream_rollup(local_run, now, snapshot)

    def _ledger_retention_loop(self) -> None:
        """Plan F: periodic ledger audit retention (frozen §9).

        Boot+interval daemon thread (same pattern as ``_local_stats_loop``):
        the first sweep runs immediately, then hourly. A failed pass is logged
        and retried on the next interval — retention must never take down the
        manager.
        """
        while not self._ledger_retention_stop.is_set():
            try:
                self._prune_ledger_audit()
            except Exception as exc:
                logger.error("Ledger audit retention pass failed",
                             extra={"error": str(exc)})
            if self._ledger_retention_stop.wait(_LEDGER_RETENTION_INTERVAL_S):
                break

    def _prune_ledger_audit(self) -> dict[str, int]:
        """Prune terminal ledger audit rows older than TRAM_AUDIT_RETENTION_DAYS.

        Plan F: incremental retention jobs prune only eligible terminal
        history. NEVER touched: non-terminal attempts, unknown-state attempts
        (the guard is retained until an operator force-release — a later
        lane), and unresolved run intents. Queued-run audit rows follow their
        existing TTL expiry path. Returns the per-table deleted counts.
        """
        if self._db is None:
            return {"attempts": 0, "intents": 0, "operations": 0}
        cutoff = (datetime.now(UTC) - timedelta(days=self._audit_retention_days)).isoformat()
        counts: dict[str, int] = {}
        with self._db._engine.begin() as conn:
            for key, sql in (
                ("attempts", """
                    DELETE FROM execution_attempts
                     WHERE state = 'terminal' AND finished_at IS NOT NULL
                       AND finished_at < :cutoff
                """),
                ("intents", """
                    DELETE FROM run_intents
                     WHERE final_outcome IS NOT NULL AND resolved_at IS NOT NULL
                       AND resolved_at < :cutoff
                """),
                ("operations", """
                    DELETE FROM lifecycle_operations
                     WHERE state IN ('complete', 'failed')
                       AND updated_at < :cutoff
                """),
            ):
                counts[key] = conn.execute(text(sql), {"cutoff": cutoff}).rowcount
        logger.info(
            "Ledger audit retention sweep complete",
            extra={"cutoff": cutoff, **counts},
        )
        return counts

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
