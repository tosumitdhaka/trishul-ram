"""Unit tests for broadcast placement reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from tram.agent.reconciler import PlacementReconciler
from tram.agent.stats_store import StatsStore
from tram.api.routers.internal import PipelineStatsPayload


def _placement(status="running"):
    return {
        "placement_group_id": "pg1",
        "pipeline_name": "pipe-a",
        "target_count": "all",
        "started_at": datetime.now(UTC) - timedelta(seconds=120),
        "status": status,
        "slots": [
            {
                "worker_index": 0,
                "worker_url": "http://w0:8766",
                "worker_id": "w0",
                "run_id_prefix": "pg1-w0",
                "current_run_id": "pg1-w0",
                "status": "running",
                "restart_count": 0,
            },
        ],
    }


def test_reconciler_marks_stale_slot_and_redispatches():
    controller = MagicMock()
    placement = _placement()
    controller.get_active_broadcast_placements.return_value = [placement]
    controller.redispatch_broadcast_slot.return_value = True

    worker_pool = MagicMock()
    worker_pool.is_worker_healthy.return_value = True

    stats_store = StatsStore(interval=10)
    db = MagicMock()
    reconciler = PlacementReconciler(controller, worker_pool, stats_store, db, stats_interval=10)

    reconciler.run_once()

    controller.redispatch_broadcast_slot.assert_called_once_with("pg1", 0)
    db.update_broadcast_placement_status.assert_called()


def test_reconciler_does_not_redispatch_fresh_slot_before_first_stats():
    controller = MagicMock()
    placement = _placement()
    placement["started_at"] = datetime.now(UTC)
    placement["slots"][0]["dispatched_at"] = datetime.now(UTC).isoformat()
    controller.get_active_broadcast_placements.return_value = [placement]

    worker_pool = MagicMock()
    worker_pool.is_worker_healthy.return_value = True

    stats_store = StatsStore(interval=30)
    db = MagicMock()
    reconciler = PlacementReconciler(controller, worker_pool, stats_store, db, stats_interval=30)

    reconciler.run_once()

    controller.redispatch_broadcast_slot.assert_not_called()
    db.update_broadcast_placement_status.assert_not_called()


def test_reconciler_promotes_reconciling_group_after_timeout():
    controller = MagicMock()
    placement = _placement(status="reconciling")
    controller.get_active_broadcast_placements.return_value = [placement]

    worker_pool = MagicMock()
    stats_store = StatsStore(interval=10)
    stats_store.update(PipelineStatsPayload(
        worker_id="w0",
        pipeline_name="pipe-a",
        run_id="pg1-w0",
        schedule_type="stream",
        uptime_seconds=10.0,
        timestamp=datetime.now(UTC),
    ))
    db = MagicMock()
    reconciler = PlacementReconciler(controller, worker_pool, stats_store, db, stats_interval=10)

    reconciler.run_once()

    controller._update_broadcast_placement_status.assert_called_once_with("pg1", "running")
