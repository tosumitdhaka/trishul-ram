"""Unit tests for broadcast placement reconciliation."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from tram.agent.reconciler import BatchReconciler, PlacementReconciler
from tram.agent.stats_store import StatsStore
from tram.api.routers.internal import PipelineStatsPayload
from tram.pipeline.controller import PipelineController
from tram.pipeline.loader import load_pipeline_from_yaml


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
    worker_pool.live_streams.return_value = []

    stats_store = StatsStore(interval=10)
    db = MagicMock()
    reconciler = PlacementReconciler(controller, worker_pool, stats_store, db, stats_interval=10)

    reconciler.run_once()

    # Slot mutations are committed through the controller's locked
    # update_placement_slot(), never written directly to the DB (B.6).
    controller.update_placement_slot.assert_called_once_with("pg1", 0, status="stale")
    controller.redispatch_broadcast_slot.assert_called_once_with(
        "pg1",
        0,
        replacement_worker_url="http://w0:8766",
    )
    db.update_broadcast_placement_status.assert_not_called()


def test_reconciler_does_not_redispatch_fresh_slot_before_first_stats():
    controller = MagicMock()
    placement = _placement()
    placement["started_at"] = datetime.now(UTC)
    placement["slots"][0]["dispatched_at"] = datetime.now(UTC).isoformat()
    controller.get_active_broadcast_placements.return_value = [placement]

    worker_pool = MagicMock()
    worker_pool.is_worker_healthy.return_value = True
    worker_pool.live_streams.return_value = []

    stats_store = StatsStore(interval=30)
    db = MagicMock()
    reconciler = PlacementReconciler(controller, worker_pool, stats_store, db, stats_interval=30)

    reconciler.run_once()

    controller.redispatch_broadcast_slot.assert_not_called()
    db.update_broadcast_placement_status.assert_not_called()


def test_reconciler_keeps_live_worker_stream_running_when_stats_are_missing():
    controller = MagicMock()
    placement = _placement()
    controller.get_active_broadcast_placements.return_value = [placement]

    worker_pool = MagicMock()
    worker_pool.live_streams.return_value = [{
        "worker_url": "http://w0:8766",
        "worker_id": "w0",
        "pipeline_name": "pipe-a",
        "run_id": "pg1-w0",
        "schedule_type": "stream",
        "uptime_seconds": 120.0,
        "stats": {"records_out": 0},
    }]

    stats_store = StatsStore(interval=10)
    db = MagicMock()
    reconciler = PlacementReconciler(controller, worker_pool, stats_store, db, stats_interval=10)

    reconciler.run_once()

    controller.redispatch_broadcast_slot.assert_not_called()
    assert placement["slots"][0]["status"] == "running"
    db.update_broadcast_placement_status.assert_not_called()


def test_reconciler_updates_slot_run_id_from_live_worker_stream():
    controller = MagicMock()
    placement = _placement(status="reconciling")
    controller.get_active_broadcast_placements.return_value = [placement]

    worker_pool = MagicMock()
    worker_pool.live_streams.return_value = [{
        "worker_url": "http://w0:8766",
        "worker_id": "w0",
        "pipeline_name": "pipe-a",
        "run_id": "pg1-w0-r2",
        "schedule_type": "stream",
        "uptime_seconds": 30.0,
        "stats": {"records_out": 5},
    }]

    stats_store = StatsStore(interval=10)
    db = MagicMock()
    reconciler = PlacementReconciler(controller, worker_pool, stats_store, db, stats_interval=10)

    reconciler.run_once()

    assert placement["slots"][0]["current_run_id"] == "pg1-w0-r2"
    controller.update_broadcast_placement_status.assert_called_once_with("pg1", "running")


def test_reconciler_promotes_reconciling_group_after_timeout():
    controller = MagicMock()
    placement = _placement(status="reconciling")
    controller.get_active_broadcast_placements.return_value = [placement]

    worker_pool = MagicMock()
    worker_pool.live_streams.return_value = []
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

    controller.update_broadcast_placement_status.assert_called_once_with("pg1", "running")


def test_reconciler_reassigns_count_n_stale_slot_to_spare_worker():
    controller = MagicMock()
    placement = _count_n_placement()
    controller.get_active_broadcast_placements.return_value = [placement]
    controller.redispatch_broadcast_slot.return_value = True

    worker_pool = MagicMock()
    worker_pool.is_worker_healthy.side_effect = lambda url: url != "http://w1:8766"
    worker_pool.healthy_workers.return_value = ["http://w0:8766", "http://w2:8766"]
    worker_pool.load_score.side_effect = lambda url: {"http://w0:8766": 10.0, "http://w2:8766": 1.0}[url]
    worker_pool.live_streams.return_value = []

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
    worker_pool.live_streams.return_value = []

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
    worker_pool.live_streams.return_value = []

    stats_store = StatsStore(interval=10)
    db = MagicMock()
    reconciler = PlacementReconciler(controller, worker_pool, stats_store, db, stats_interval=10)

    reconciler.run_once()

    controller.reconcile_kubernetes_service.assert_called_once_with("pipe-list")


def test_batch_reconciler_marks_missing_tracked_run_failed():
    controller = MagicMock()
    controller.get_active_batch_runs.return_value = [
        {
            "run_id": "b1",
            "pipeline_name": "pipe-a",
            "worker_url": "http://w0:8766",
            "schedule_type": "interval",
            "started_at": datetime.now(UTC) - timedelta(seconds=60),
        }
    ]
    controller.list_all.return_value = []
    controller.mark_active_batch_run_lost.return_value = True

    worker_pool = MagicMock()
    worker_pool.is_run_active.return_value = False

    reconciler = BatchReconciler(controller, worker_pool, interval=10)
    reconciler.run_once()

    controller.mark_active_batch_run_lost.assert_called_once()


def test_batch_reconciler_does_not_double_fail_pipeline_cleared_in_same_cycle():
    state = MagicMock()
    state.config.name = "pipe-a"
    state.config.schedule.type = "interval"
    state.status = "running"

    controller = MagicMock()
    controller.get_active_batch_runs.return_value = [
        {
            "run_id": "b1",
            "pipeline_name": "pipe-a",
            "worker_url": "http://w0:8766",
            "schedule_type": "interval",
            "started_at": datetime.now(UTC) - timedelta(seconds=60),
        }
    ]
    controller.list_all.return_value = [state]

    worker_pool = MagicMock()
    worker_pool.is_run_active.return_value = False

    reconciler = BatchReconciler(controller, worker_pool, interval=10)
    reconciler.run_once()

    controller.mark_active_batch_run_lost.assert_called_once_with(
        "pipe-a",
        error="Worker-owned batch run disappeared before callback: b1",
        run_id="b1",
    )
    worker_pool.find_pipeline_runs.assert_not_called()


def test_batch_reconciler_adopts_untracked_running_batch():
    state = MagicMock()
    state.config.name = "pipe-a"
    state.config.schedule.type = "interval"
    state.status = "running"

    controller = MagicMock()
    controller.get_active_batch_runs.return_value = []
    controller.list_all.return_value = [state]
    controller.adopt_active_batch_run.return_value = True

    worker_pool = MagicMock()
    worker_pool.find_pipeline_runs.return_value = [
        {
            "worker_url": "http://w0:8766",
            "run_id": "b1",
            "pipeline_name": "pipe-a",
            "started_at": "2026-04-23T10:00:00+00:00",
            "schedule_type": "batch",
        }
    ]

    reconciler = BatchReconciler(controller, worker_pool, interval=10)
    reconciler.run_once()

    controller.adopt_active_batch_run.assert_called_once_with(
        pipeline_name="pipe-a",
        run_id="b1",
        worker_url="http://w0:8766",
        started_at="2026-04-23T10:00:00+00:00",
    )


def test_batch_reconciler_clears_untracked_stale_running_batch():
    state = MagicMock()
    state.config.name = "pipe-a"
    state.config.schedule.type = "interval"
    state.status = "running"

    controller = MagicMock()
    controller.get_active_batch_runs.return_value = []
    controller.list_all.return_value = [state]
    controller.mark_active_batch_run_lost.return_value = True

    worker_pool = MagicMock()
    worker_pool.find_pipeline_runs.return_value = []

    reconciler = BatchReconciler(controller, worker_pool, interval=10)
    reconciler.run_once()

    controller.mark_active_batch_run_lost.assert_called_once()


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


# ── B.6: reconciler never mutates controller-owned placement dicts ─────────


_STREAM_YAML = """\
name: pipe-a
schedule:
  type: stream
source:
  type: local
  path: /dev/null
  file_pattern: "*.noop"
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""


def _live_placement(started_at=None):
    return {
        "placement_group_id": "pg1",
        "pipeline_name": "pipe-a",
        "target_count": "all",
        "started_at": started_at or (datetime.now(UTC) - timedelta(seconds=120)),
        "status": "running",
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


def _real_controller_with_placement(db=None, worker_pool=None):
    db = db or MagicMock()
    worker_pool = worker_pool or MagicMock()
    ctrl = PipelineController(
        db=db, node_id="test-node", worker_pool=worker_pool, manager_url="http://manager"
    )
    config = load_pipeline_from_yaml(_STREAM_YAML)
    ctrl.manager.register(config, yaml_text=_STREAM_YAML)
    ctrl._broadcast_placements["pg1"] = _live_placement()
    ctrl._active_placement_group["pipe-a"] = "pg1"
    return ctrl


def test_reconciler_receives_copies_not_live_placement_dicts():
    """B.6: placements handed to the reconciler are copies — mutating them in
    place (as the reconciler historically did) cannot race the controller's
    lock-protected placement state."""
    ctrl = _real_controller_with_placement()

    placements = ctrl.get_active_broadcast_placements()
    placements[0]["slots"][0]["current_run_id"] = "tampered"
    placements[0]["slots"][0]["status"] = "stale"

    live_slot = ctrl._broadcast_placements["pg1"]["slots"][0]
    assert live_slot["current_run_id"] == "pg1-w0"
    assert live_slot["status"] == "running"


def test_reconciler_commit_racing_controller_update_never_tears_slot():
    """B.6: a reconciler slot commit racing a controller-side update serializes
    to exactly one complete outcome — never a torn mix of the two writers.

    Barrier-lined: the reconciler's full stale-commit + redispatch flow races a
    concurrent update_placement_slot() write to the same slot. Because both
    writers re-read the authoritative slot under the lifecycle lock, the final
    slot is always one of the two serialized outcomes.
    """
    worker_pool = MagicMock()
    worker_pool.dispatch_to_worker.return_value = True
    worker_pool.worker_id_for_url.return_value = "w0"
    worker_pool.is_worker_healthy.return_value = True
    worker_pool.live_streams.return_value = []

    db = MagicMock()
    ctrl = _real_controller_with_placement(db=db, worker_pool=worker_pool)
    reconciler = PlacementReconciler(ctrl, worker_pool, StatsStore(interval=10), db, stats_interval=10)

    barrier = threading.Barrier(2)
    errors: list = []

    def _reconcile():
        barrier.wait()
        try:
            reconciler.run_once()
        except Exception as exc:  # noqa: BLE001 — collected for assertion
            errors.append(exc)

    def _update():
        barrier.wait()
        try:
            ctrl.update_placement_slot(
                "pg1", 0, status="running", current_run_id="pg1-w0-r7"
            )
        except Exception as exc:  # noqa: BLE001 — collected for assertion
            errors.append(exc)

    t1 = threading.Thread(target=_reconcile)
    t2 = threading.Thread(target=_update)
    t1.start()
    t2.start()
    t1.join(10)
    t2.join(10)
    assert not t1.is_alive() and not t2.is_alive()
    assert errors == []

    final = ctrl._broadcast_placements["pg1"]["slots"][0]
    # Serialized outcomes only: reconciler's redispatch (r1/running/rc1) or the
    # concurrent update (r7/running, restart_count carried over). A torn state —
    # e.g. a stale status surviving past a redispatch, or a restart_count that
    # does not match the current run id — is impossible under the lock.
    assert final["status"] == "running", final
    assert final["current_run_id"] in {"pg1-w0-r1", "pg1-w0-r7"}, final
    assert final["restart_count"] == 1, final


def test_reconciler_commit_persists_via_controller_not_reconciler_db():
    """B.6: slot changes reach the DB through the controller's locked commit
    path, so a stale reconciler snapshot can never be persisted over a
    concurrent controller update."""
    controller = MagicMock()
    placement = _placement()
    placement["slots"][0]["dispatched_at"] = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    controller.get_active_broadcast_placements.return_value = [placement]

    worker_pool = MagicMock()
    worker_pool.is_worker_healthy.return_value = True
    worker_pool.live_streams.return_value = []

    stats_store = StatsStore(interval=10)
    db = MagicMock()
    reconciler = PlacementReconciler(controller, worker_pool, stats_store, db, stats_interval=10)

    reconciler.run_once()

    controller.update_placement_slot.assert_called()
    db.update_broadcast_placement_status.assert_not_called()
