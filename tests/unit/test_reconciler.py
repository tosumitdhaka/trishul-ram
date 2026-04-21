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


def _count_n_placement(status="running"):
    return {
        "placement_group_id": "pg2",
        "pipeline_name": "pipe-n",
        "target_count": 2,
        "started_at": datetime.now(UTC) - timedelta(seconds=120),
        "status": status,
        "slots": [
            {
                "worker_index": 0,
                "worker_url": "http://w0:8766",
                "worker_id": "w0",
                "run_id_prefix": "pg2-w0",
                "current_run_id": "pg2-w0",
                "status": "running",
                "restart_count": 0,
            },
            {
                "worker_index": 1,
                "worker_url": "http://w1:8766",
                "worker_id": "w1",
                "run_id_prefix": "pg2-w1",
                "current_run_id": "pg2-w1",
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

    controller.redispatch_broadcast_slot.assert_called_once_with(
        "pg1",
        0,
        replacement_worker_url="http://w0:8766",
    )
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


def test_reconciler_reassigns_count_n_stale_slot_to_spare_worker():
    controller = MagicMock()
    placement = _count_n_placement()
    controller.get_active_broadcast_placements.return_value = [placement]
    controller.redispatch_broadcast_slot.return_value = True

    worker_pool = MagicMock()
    worker_pool.is_worker_healthy.side_effect = lambda url: url != "http://w1:8766"
    worker_pool.healthy_workers.return_value = ["http://w0:8766", "http://w2:8766"]
    worker_pool.load_score.side_effect = lambda url: {"http://w0:8766": 10.0, "http://w2:8766": 1.0}[url]

    stats_store = StatsStore(interval=10)
    stats_store.update(PipelineStatsPayload(
        worker_id="w0",
        pipeline_name="pipe-n",
        run_id="pg2-w0",
        schedule_type="stream",
        uptime_seconds=10.0,
        timestamp=datetime.now(UTC),
    ))
    db = MagicMock()
    reconciler = PlacementReconciler(controller, worker_pool, stats_store, db, stats_interval=10)

    reconciler.run_once()

    controller.redispatch_broadcast_slot.assert_called_once_with(
        "pg2",
        1,
        replacement_worker_url="http://w2:8766",
    )


def test_reconciler_retries_named_worker_only_when_it_returns():
    controller = MagicMock()
    placement = {
        "placement_group_id": "pg-list",
        "pipeline_name": "pipe-list",
        "target_count": 2,
        "started_at": datetime.now(UTC) - timedelta(seconds=120),
        "status": "running",
        "slots": [
            {
                "worker_index": 0,
                "worker_url": "http://w0:8766",
                "worker_id": "tram-worker-0",
                "pinned_worker_id": "tram-worker-0",
                "run_id_prefix": "pg-list-w0",
                "current_run_id": "pg-list-w0",
                "status": "running",
                "restart_count": 0,
            },
            {
                "worker_index": 1,
                "worker_url": "http://w1-old:8766",
                "worker_id": "tram-worker-1",
                "pinned_worker_id": "tram-worker-1",
                "run_id_prefix": "pg-list-w1",
                "current_run_id": "pg-list-w1",
                "status": "running",
                "restart_count": 0,
            },
        ],
    }
    controller.get_active_broadcast_placements.return_value = [placement]
    controller.redispatch_broadcast_slot.return_value = True

    worker_pool = MagicMock()
    worker_pool.url_for_worker_id.side_effect = lambda worker_id: {
        "tram-worker-0": "http://w0:8766",
        "tram-worker-1": "http://w1-new:8766",
    }.get(worker_id)
    worker_pool.is_worker_healthy.side_effect = lambda url: url in {"http://w0:8766", "http://w1-new:8766"}

    stats_store = StatsStore(interval=10)
    stats_store.update(PipelineStatsPayload(
        worker_id="tram-worker-0",
        pipeline_name="pipe-list",
        run_id="pg-list-w0",
        schedule_type="stream",
        uptime_seconds=10.0,
        timestamp=datetime.now(UTC),
    ))
    db = MagicMock()
    reconciler = PlacementReconciler(controller, worker_pool, stats_store, db, stats_interval=10)

    reconciler.run_once()

    controller.redispatch_broadcast_slot.assert_called_once_with(
        "pg-list",
        1,
        replacement_worker_url="http://w1-new:8766",
    )


def test_reconciler_refreshes_k8s_service_for_named_worker_stale_slot():
    controller = MagicMock()
    placement = {
        "placement_group_id": "pg-list",
        "pipeline_name": "pipe-list",
        "target_count": 1,
        "started_at": datetime.now(UTC) - timedelta(seconds=120),
        "status": "running",
        "slots": [
            {
                "worker_index": 0,
                "worker_url": "http://w3:8766",
                "worker_id": "tram-worker-3",
                "pinned_worker_id": "tram-worker-3",
                "run_id_prefix": "pg-list-w0",
                "current_run_id": "pg-list-w0",
                "status": "running",
                "restart_count": 0,
            },
        ],
    }
    controller.get_active_broadcast_placements.return_value = [placement]
    controller.redispatch_broadcast_slot.return_value = False

    worker_pool = MagicMock()
    worker_pool.url_for_worker_id.return_value = None

    stats_store = StatsStore(interval=10)
    db = MagicMock()
    reconciler = PlacementReconciler(controller, worker_pool, stats_store, db, stats_interval=10)

    reconciler.run_once()

    controller.reconcile_kubernetes_service.assert_called_once_with("pipe-list")


# ── Manager metrics ────────────────────────────────────────────────────────


def test_reconciler_stale_increments_mark_stale_metric():
    from unittest.mock import MagicMock, patch
    controller = MagicMock()
    placement = _placement()
    controller.get_active_broadcast_placements.return_value = [placement]
    controller.redispatch_broadcast_slot.return_value = False

    worker_pool = MagicMock()
    worker_pool.is_worker_healthy.return_value = True

    stats_store = StatsStore(interval=10)
    db = MagicMock()
    reconciler = PlacementReconciler(controller, worker_pool, stats_store, db, stats_interval=10)

    action_counter = MagicMock()
    action_counter.labels.return_value = action_counter

    with patch("tram.metrics.registry.MGR_RECONCILE_ACTION_TOTAL", action_counter):
        reconciler.run_once()

    action_counter.labels.assert_any_call(pipeline="pipe-a", action="mark_stale")
    action_counter.inc.assert_called()


def test_reconciler_redispatch_increments_redispatch_metrics():
    from unittest.mock import MagicMock, patch
    controller = MagicMock()
    placement = _placement()
    controller.get_active_broadcast_placements.return_value = [placement]
    controller.redispatch_broadcast_slot.return_value = True

    worker_pool = MagicMock()
    worker_pool.is_worker_healthy.return_value = True

    stats_store = StatsStore(interval=10)
    db = MagicMock()
    reconciler = PlacementReconciler(controller, worker_pool, stats_store, db, stats_interval=10)

    redispatch_counter = MagicMock()
    redispatch_counter.labels.return_value = redispatch_counter
    action_counter = MagicMock()
    action_counter.labels.return_value = action_counter

    with patch("tram.metrics.registry.MGR_REDISPATCH_TOTAL", redispatch_counter), \
         patch("tram.metrics.registry.MGR_RECONCILE_ACTION_TOTAL", action_counter):
        reconciler.run_once()

    redispatch_counter.labels.assert_called_with(pipeline="pipe-a")
    redispatch_counter.inc.assert_called()
    action_counter.labels.assert_any_call(pipeline="pipe-a", action="redispatch")
