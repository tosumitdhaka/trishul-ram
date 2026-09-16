"""Manager-side reconciler for broadcast stream placements."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)


class PlacementReconciler:
    """Reconcile broadcast placements from stats + worker health."""

    def __init__(self, controller, worker_pool, stats_store, db, stats_interval: int = 30) -> None:
        self._controller = controller
        self._worker_pool = worker_pool
        self._stats_store = stats_store
        self._db = db
        self._stats_interval = stats_interval
        self._interval = min(stats_interval, 10)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Consecutive passes with no liveness signal for an unplaced running
        # stream (D.2 §5.3). Reset on any sighting, on fresh stats, or when the
        # pipeline leaves the candidate set; recovery fires at 2 misses.
        self._unplaced_misses: dict[str, int] = {}

    def _slot_dispatch_time(self, placement: dict, slot: dict) -> datetime:
        raw = slot.get("dispatched_at") or placement.get("started_at") or datetime.now(UTC)
        if isinstance(raw, datetime):
            return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
        if isinstance(raw, str):
            parsed = datetime.fromisoformat(raw)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        return datetime.now(UTC)

    def _awaiting_first_stats(self, now: datetime, placement: dict, slot: dict, stats) -> bool:
        if stats is not None:
            return False
        # A newly-dispatched or just-restored stream slot has not had a chance to
        # emit its first periodic stats report yet. Treat it as starting, not stale.
        grace = timedelta(seconds=self._stats_interval + 5)
        return now - self._slot_dispatch_time(placement, slot) < grace

    @staticmethod
    def _target_count(placement: dict) -> int | str:
        raw = placement.get("target_count", "all")
        if raw == "all":
            return "all"
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
        return raw

    def _select_replacement_worker(self, placement: dict, slot: dict) -> str | None:
        pinned_worker_id = slot.get("pinned_worker_id")
        if pinned_worker_id:
            worker_url = self._worker_pool.url_for_worker_id(str(pinned_worker_id))
            if worker_url and self._worker_pool.is_worker_healthy(worker_url):
                return worker_url
            return None

        current_worker_url = slot.get("worker_url")
        if current_worker_url and self._worker_pool.is_worker_healthy(current_worker_url):
            return current_worker_url

        if self._target_count(placement) == "all":
            return None

        excluded = {
            other.get("worker_url")
            for other in placement["slots"]
            if other is not slot and other.get("worker_url")
        }
        candidates = [
            worker_url
            for worker_url in self._worker_pool.healthy_workers()
            if worker_url not in excluded
        ]
        if not candidates:
            return None
        candidates.sort(key=self._worker_pool.load_score)
        return candidates[0]

    @staticmethod
    def _live_worker_key(item: dict) -> str | None:
        worker_key = item.get("worker_id") or item.get("worker_url")
        return str(worker_key) if worker_key else None

    def _find_live_slot(
        self,
        placement: dict,
        slot: dict,
        live_by_run_id: dict[str, dict],
        live_by_pipeline_worker: dict[tuple[str, str], dict],
    ) -> dict | None:
        current_run_id = str(slot.get("current_run_id", "") or "")
        if current_run_id and current_run_id in live_by_run_id:
            return live_by_run_id[current_run_id]

        worker_key = slot.get("worker_id") or slot.get("worker_url")
        if not worker_key:
            return None
        return live_by_pipeline_worker.get((placement["pipeline_name"], str(worker_key)))

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="tram-placement-reconciler",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self._interval + 1)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.run_once()
            except Exception as exc:
                logger.warning("Placement reconciler iteration failed", extra={"error": str(exc)})

    def run_once(self) -> None:
        now = datetime.now(UTC)
        live_streams = list(self._worker_pool.live_streams() or [])
        live_by_run_id = {
            str(item.get("run_id")): item
            for item in live_streams
            if item.get("run_id")
        }
        live_by_pipeline_worker: dict[tuple[str, str], dict] = {}
        for item in live_streams:
            pipeline_name = str(item.get("pipeline_name", "") or "")
            worker_key = self._live_worker_key(item)
            if pipeline_name and worker_key:
                live_by_pipeline_worker[(pipeline_name, worker_key)] = item

        for placement in self._controller.get_active_broadcast_placements():
            placement_group_id = placement["placement_group_id"]
            placement_changed = False
            placement_drifted = False

            for slot in placement["slots"]:
                live_item = self._find_live_slot(
                    placement,
                    slot,
                    live_by_run_id,
                    live_by_pipeline_worker,
                )
                if live_item is not None:
                    # §6.2 stale-config policy: a live run whose config hash
                    # differs from the manager's current config is stopped and
                    # redispatched with the new YAML. Missing/empty/unknown hash
                    # (older agent during a rolling upgrade) fails open — adopt.
                    live_sha = live_item.get("config_sha256")
                    if live_sha not in ("", None, "unknown"):
                        expected = self._controller.pipeline_config_sha(placement["pipeline_name"])
                        if expected and live_sha != expected:
                            self._controller.reconcile_placement_config_drift(placement_group_id)
                            # The swap re-dispatches every slot of this placement;
                            # this copy is now stale — skip status recomputation.
                            placement_drifted = True
                            break
                    live_run_id = str(live_item.get("run_id", "") or "")
                    updates = {}
                    if live_run_id and slot.get("current_run_id") != live_run_id:
                        updates["current_run_id"] = live_run_id
                    if live_item.get("worker_url") and slot.get("worker_url") != live_item.get("worker_url"):
                        updates["worker_url"] = live_item.get("worker_url")
                    if live_item.get("worker_id") and slot.get("worker_id") != live_item.get("worker_id"):
                        updates["worker_id"] = live_item.get("worker_id")
                    if slot.get("status") != "running":
                        updates["status"] = "running"
                    # Commit through the controller so the mutation is applied
                    # to the authoritative placement under the lifecycle lock
                    # (B.6 — the reconciler holds copies, never live dicts).
                    if updates and self._controller.update_placement_slot(
                        placement_group_id,
                        int(slot["worker_index"]),
                        **updates,
                    ):
                        slot.update(updates)
                        placement_changed = True
                    continue

                current_run_id = str(slot.get("current_run_id", ""))
                stats = self._stats_store.get_by_run_id(current_run_id) if current_run_id else None
                if self._awaiting_first_stats(now, placement, slot, stats):
                    continue
                is_stale = stats is None or self._stats_store.is_stale(stats)
                if is_stale:
                    if slot.get("status") != "stale":
                        if self._controller.update_placement_slot(
                            placement_group_id,
                            int(slot["worker_index"]),
                            status="stale",
                        ):
                            slot["status"] = "stale"
                            placement_changed = True
                            from tram.metrics.registry import MGR_RECONCILE_ACTION_TOTAL
                            MGR_RECONCILE_ACTION_TOTAL.labels(
                                pipeline=placement["pipeline_name"], action="mark_stale"
                            ).inc()
                    replacement_worker_url = self._select_replacement_worker(placement, slot)
                    if replacement_worker_url and self._controller.redispatch_broadcast_slot(
                        placement_group_id,
                        int(slot["worker_index"]),
                        replacement_worker_url=replacement_worker_url,
                    ):
                        placement_changed = True
                        slot["status"] = "running"
                        from tram.metrics.registry import (
                            MGR_RECONCILE_ACTION_TOTAL,
                            MGR_REDISPATCH_TOTAL,
                        )
                        MGR_REDISPATCH_TOTAL.labels(pipeline=placement["pipeline_name"]).inc()
                        MGR_RECONCILE_ACTION_TOTAL.labels(
                            pipeline=placement["pipeline_name"], action="redispatch"
                        ).inc()
                elif slot.get("status") != "running":
                    if self._controller.update_placement_slot(
                        placement_group_id,
                        int(slot["worker_index"]),
                        status="running",
                    ):
                        slot["status"] = "running"
                        placement_changed = True
                        from tram.metrics.registry import MGR_RECONCILE_ACTION_TOTAL
                        MGR_RECONCILE_ACTION_TOTAL.labels(
                            pipeline=placement["pipeline_name"], action="resolve_running"
                        ).inc()

            next_status = placement["status"]
            target_count = self._target_count(placement)
            all_running = all(
                slot.get("status") == "running" and slot.get("current_run_id")
                for slot in placement["slots"]
            )
            if placement_drifted:
                # The drift swap already mutated the authoritative placement and
                # redispatched every slot; never recompute status from this
                # stale copy (it could clobber the post-swap state).
                continue
            if placement["status"] == "reconciling":
                age_seconds = (now - placement["started_at"]).total_seconds()
                if age_seconds > self._stats_interval * 2:
                    next_status = "running" if all_running else "degraded"
            else:
                next_status = "running" if all_running else "degraded"

            if target_count != "all":
                running_slots = sum(
                    1
                    for slot in placement["slots"]
                    if slot.get("status") == "running" and slot.get("current_run_id")
                )
                if running_slots < int(target_count):
                    next_status = "degraded"

            # Slot persistence happens inside update_placement_slot() /
            # redispatch_broadcast_slot() (both under the controller lock), so
            # a stale snapshot computed here can never be persisted over a
            # concurrent controller update.
            if placement_changed:
                self._controller.reconcile_kubernetes_service(placement["pipeline_name"])
            if next_status != placement["status"]:
                self._controller.update_broadcast_placement_status(placement_group_id, next_status)

        self._reconcile_unplaced_streams(live_streams)

    def _reconcile_unplaced_streams(self, live_streams: list[dict]) -> None:
        """Streams with manager status 'running' but no placement group (D.2 §5.3).

        Flag-off adoption, failed placement persistence, or drift leave a
        running count=1 stream without a placement row. This pass reuses the
        live snapshot already fetched by run_once (zero additional worker
        probes) and the StatsStore for a second, independent liveness signal:

        - Live (snapshot sighting OR non-stale stats) → self-heal bookkeeping;
          stop all but the earliest of multiple live runs for one pipeline.
        - Not live → 2-consecutive-miss hysteresis, then recover_unplaced_stream
          (redispatch, or mark stopped when it may not run).
        """
        candidates = self._controller.stream_liveness_candidates()
        if not candidates:
            self._unplaced_misses.clear()
            return
        candidate_names = {c["name"] for c in candidates}
        for name in list(self._unplaced_misses):
            if name not in candidate_names:
                del self._unplaced_misses[name]

        for candidate in candidates:
            name = candidate["name"]
            if candidate.get("has_placement"):
                self._unplaced_misses.pop(name, None)
                continue
            sightings = [
                item for item in live_streams
                if str(item.get("pipeline_name", "") or "") == name
            ]
            fresh_stats = self._stats_store.for_pipeline(name)
            if sightings or fresh_stats:
                self._unplaced_misses.pop(name, None)
                if not sightings:
                    continue  # live via stats only — nothing to bookkeep
                ordered = sorted(
                    sightings,
                    key=lambda item: str(item.get("started_at") or ""),
                )
                earliest = ordered[0]
                self._controller.adopt_unplaced_stream_bookkeeping(
                    name,
                    str(earliest.get("run_id") or ""),
                    str(earliest.get("worker_url") or ""),
                    started_at=earliest.get("started_at"),
                )
                # Pre-D.2 double-dispatch residue: a count=1 pipeline must have
                # at most one live run. The reconciler is the only component
                # with a global view — stop every run but the earliest.
                for extra in ordered[1:]:
                    extra_run = str(extra.get("run_id") or "")
                    if extra_run:
                        self._worker_pool.stop_run(extra_run, name)
                        logger.warning(
                            "Stopped duplicate live stream run",
                            extra={"pipeline": name, "run_id": extra_run},
                        )
                continue

            # Not alive on either signal. Hysteresis: 2 consecutive misses
            # (matching worker_pool's health_failures_to_down) before acting.
            self._unplaced_misses[name] = self._unplaced_misses.get(name, 0) + 1
            if self._unplaced_misses[name] >= 2:
                self._controller.recover_unplaced_stream(name)
                # Reset so a just-recovered run (not yet visible in the live
                # snapshot) is not immediately re-dispatched on the next pass.
                self._unplaced_misses[name] = 0


class BatchReconciler:
    """Reconcile worker-owned batch runs after worker or manager failure.

    E.2 (GH #21): the reconciler is also the single drain authority for queued
    manual runs — it re-evaluates capacity, pipeline state, and TTL every pass.
    Worker-restored health signals only *nudge* the loop early (§6.5); they
    never dispatch.
    """

    def __init__(self, controller, worker_pool, interval: int = 10) -> None:
        self._controller = controller
        self._worker_pool = worker_pool
        self._interval = interval
        self._stop = threading.Event()
        # E.2 (§6.5): set by nudge() (wired to the WorkerPool on_health_restored
        # hook in the app) to wake the loop before the next interval tick.
        self._drain_nudge = threading.Event()
        self._thread: threading.Thread | None = None

    def nudge(self) -> None:
        """Wake the drain loop early — a worker's health was restored.

        The nudge only wakes the loop; run_once re-evaluates everything and
        never dispatches on the nudge itself. Worst case the wake is redundant
        with the next interval tick.
        """
        self._drain_nudge.set()

    def start(self) -> None:
        self._stop.clear()
        self._drain_nudge.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="tram-batch-reconciler",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Wake a loop blocked in _drain_nudge.wait() — it re-checks the stop
        # event right after clearing the nudge.
        self._drain_nudge.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self._interval + 1)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._drain_nudge.wait(self._interval)
            self._drain_nudge.clear()
            if self._stop.is_set():
                return
            try:
                self.run_once()
            except Exception as exc:
                logger.warning("Batch reconciler iteration failed", extra={"error": str(exc)})

    def _reconcile_tracked_runs(self) -> set[str]:
        from tram.metrics.registry import MGR_RECONCILE_ACTION_TOTAL

        cleared: set[str] = set()
        for lease in self._controller.get_active_batch_runs():
            pipeline_name = str(lease["pipeline_name"])
            run_id = str(lease["run_id"])
            worker_url = str(lease["worker_url"])
            if self._worker_pool.is_run_active(run_id, worker_url=worker_url):
                continue
            error = f"Worker-owned batch run disappeared before callback: {run_id}"
            if self._controller.mark_active_batch_run_lost(
                pipeline_name,
                error=error,
                run_id=run_id,
            ):
                cleared.add(pipeline_name)
                MGR_RECONCILE_ACTION_TOTAL.labels(
                    pipeline=pipeline_name, action="batch_mark_lost"
                ).inc()
        return cleared

    def _reconcile_untracked_running_pipelines(self, skip: set[str] | None = None) -> None:
        from tram.metrics.registry import MGR_RECONCILE_ACTION_TOTAL

        skip = skip or set()
        tracked = {
            str(run["pipeline_name"])
            for run in self._controller.get_active_batch_runs()
        }
        for state in self._controller.list_all():
            pipeline_name = state.config.name
            if state.config.schedule.type == "stream":
                continue
            if state.status != "running":
                continue
            if pipeline_name in skip:
                continue
            if pipeline_name in tracked:
                continue

            matches = self._worker_pool.find_pipeline_runs(pipeline_name, schedule_type="batch")
            if matches:
                adopted = min(
                    matches,
                    key=lambda item: str(item.get("started_at") or ""),
                )
                if self._controller.adopt_active_batch_run(
                    pipeline_name=pipeline_name,
                    run_id=str(adopted["run_id"]),
                    worker_url=str(adopted["worker_url"]),
                    started_at=adopted.get("started_at"),
                ):
                    MGR_RECONCILE_ACTION_TOTAL.labels(
                        pipeline=pipeline_name, action="batch_adopt"
                    ).inc()
                continue

            error = "Manager recovered no active worker batch run for pipeline marked running"
            if self._controller.mark_active_batch_run_lost(pipeline_name, error=error):
                MGR_RECONCILE_ACTION_TOTAL.labels(
                    pipeline=pipeline_name, action="batch_clear_stale"
                ).inc()

    def run_once(self) -> None:
        cleared = self._reconcile_tracked_runs()
        self._reconcile_untracked_running_pipelines(skip=cleared)
        self._drain_queued_runs()

    def _drain_queued_runs(self) -> None:
        """Drain queued manual runs (E.2 §6.2).

        The drain is authoritative: it re-evaluates capacity, pipeline state,
        and TTL every pass. TTL expiry runs first and unconditionally — a row
        past ``expires_at`` must fail even through a full worker outage (the
        202's deadline is enforced when it matters); expiry is pure DB +
        ``_finalize_batch_result`` and needs no workers. The debounced health
        pre-check is flap-safe (a single failed probe cannot cause a drain
        attempt) and gates dispatch attempts only. Dispatch HTTP runs with
        the controller's lifecycle lock released (the claim/commit/revert
        transitions are each under the RLock per §6.4).
        """
        from tram.agent.worker_pool import DISPATCH_ACCEPTED, DISPATCH_NO_CAPACITY

        for run in self._controller.drainable_queued_runs():
            if run["expires_at"] <= datetime.now(UTC):
                self._controller.expire_queued_run(run["run_id"])

        if not self._worker_pool.healthy_workers():
            return  # debounced state — flap-safe pre-check (dispatch attempts only)
        for run in self._controller.drainable_queued_runs():
            claimed = self._controller.claim_queued_run(run["run_id"])
            if claimed is None:
                continue  # lost the claim (delete/stop raced us)
            # ── network I/O with the lock released (redispatch_broadcast_slot pattern) ──
            outcome = self._worker_pool.dispatch_with_result(
                run_id=claimed["run_id"],
                pipeline_name=claimed["pipeline_name"],
                yaml_text=claimed["yaml_snapshot"],  # the auditable snapshot (Decision 5)
                schedule_type=claimed["schedule_type"],
                callback_url=claimed["callback_url"],
            )
            if outcome.outcome == DISPATCH_ACCEPTED:
                self._controller.commit_queued_dispatch(claimed["run_id"], outcome.worker_url)
            else:  # DISPATCH_FAILED, or DISPATCH_NO_CAPACITY (capacity vanished mid-pass)
                result_label = (
                    "no_capacity" if outcome.outcome == DISPATCH_NO_CAPACITY else "failed"
                )
                self._controller.revert_queued_claim(claimed["run_id"], result=result_label)
