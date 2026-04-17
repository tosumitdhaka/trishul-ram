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
        for placement in self._controller.get_active_broadcast_placements():
            placement_group_id = placement["placement_group_id"]
            placement_changed = False

            for slot in placement["slots"]:
                current_run_id = str(slot.get("current_run_id", ""))
                stats = self._stats_store.get_by_run_id(current_run_id) if current_run_id else None
                if self._awaiting_first_stats(now, placement, slot, stats):
                    continue
                is_stale = stats is None or self._stats_store.is_stale(stats)
                if is_stale:
                    if slot.get("status") != "stale":
                        slot["status"] = "stale"
                        placement_changed = True
                    if self._worker_pool.is_worker_healthy(slot["worker_url"]):
                        if self._controller.redispatch_broadcast_slot(
                            placement_group_id,
                            int(slot["worker_index"]),
                        ):
                            placement_changed = True
                            slot["status"] = "running"
                elif slot.get("status") != "running":
                    slot["status"] = "running"
                    placement_changed = True

            next_status = placement["status"]
            all_running = all(slot.get("status") == "running" for slot in placement["slots"])
            if placement["status"] == "reconciling":
                age_seconds = (now - placement["started_at"]).total_seconds()
                if age_seconds > self._stats_interval * 2:
                    next_status = "running" if all_running else "degraded"
            else:
                next_status = "running" if all_running else "degraded"

            if placement_changed and self._db is not None:
                self._db.update_broadcast_placement_status(
                    placement_group_id,
                    placement["status"],
                    slots=placement["slots"],
                )
            if next_status != placement["status"]:
                self._controller._update_broadcast_placement_status(placement_group_id, next_status)
