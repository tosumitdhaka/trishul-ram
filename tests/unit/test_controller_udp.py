"""Tests for _get_dispatched_worker_ids — the bridge between placement state and
KubernetesServiceManager.ensure_service(dispatched_worker_ids=...)."""

from __future__ import annotations

from unittest.mock import MagicMock

from tram.pipeline.controller import PipelineController


def _make_manager_controller(**kwargs):
    worker_pool = MagicMock()
    ctrl = PipelineController(
        node_id="mgr",
        worker_pool=worker_pool,
        manager_url="http://manager:8765",
        **kwargs,
    )
    ctrl.manager = MagicMock()
    ctrl.executor = MagicMock()
    ctrl._scheduler = MagicMock()
    return ctrl, worker_pool


def _inject_placement(ctrl, pipeline_name, target_count, worker_ids, workers_list=None):
    """Directly inject a broadcast placement and matching config stub."""
    pg_id = f"pg-{pipeline_name}"
    slots = [
        {"worker_index": i, "worker_id": wid, "worker_url": f"http://w{i}:8766",
         "status": "running", "restart_count": 0, "run_id_prefix": f"{pg_id}-w{i}",
         "current_run_id": f"{pg_id}-w{i}"}
        for i, wid in enumerate(worker_ids)
    ]
    ctrl._broadcast_placements[pg_id] = {
        "placement_group_id": pg_id,
        "pipeline_name": pipeline_name,
        "target_count": target_count,
        "status": "running",
        "slots": slots,
    }
    ctrl._active_placement_group[pipeline_name] = pg_id

    # Stub manager.get() to return a config reflecting workers_list
    workers_mock = MagicMock()
    workers_mock.worker_ids = workers_list
    config_mock = MagicMock()
    config_mock.workers = workers_mock
    state_mock = MagicMock()
    state_mock.config = config_mock
    ctrl.manager.get.return_value = state_mock


# ── _get_dispatched_worker_ids ─────────────────────────────────────────────


def test_count_all_returns_none():
    ctrl, _ = _make_manager_controller()
    _inject_placement(ctrl, "pipe-all", target_count="all", worker_ids=["w0", "w1", "w2"])
    assert ctrl._get_dispatched_worker_ids("pipe-all") is None


def test_count_n_returns_worker_ids():
    ctrl, _ = _make_manager_controller()
    _inject_placement(ctrl, "pipe-n", target_count=2, worker_ids=["w0", "w1"])
    result = ctrl._get_dispatched_worker_ids("pipe-n")
    assert result == ["w0", "w1"]


def test_workers_list_returns_none():
    """workers.list uses config.workers.worker_ids path; _get_dispatched_worker_ids must return None."""
    ctrl, _ = _make_manager_controller()
    _inject_placement(
        ctrl, "pipe-list", target_count=2,
        worker_ids=["tram-worker-0", "tram-worker-1"],
        workers_list=["tram-worker-0", "tram-worker-1"],
    )
    assert ctrl._get_dispatched_worker_ids("pipe-list") is None


def test_no_placement_returns_none():
    ctrl, _ = _make_manager_controller()
    assert ctrl._get_dispatched_worker_ids("nonexistent") is None


# ── _activate_kubernetes_service wiring ───────────────────────────────────


def test_activate_passes_dispatched_ids_for_count_n():
    k8s = MagicMock()
    ctrl, _ = _make_manager_controller(kubernetes_service_manager=k8s)
    _inject_placement(ctrl, "pipe-n", target_count=2, worker_ids=["w0", "w1"])

    config = MagicMock()
    config.name = "pipe-n"
    ctrl._activate_kubernetes_service(config)

    k8s.ensure_service.assert_called_once_with(config, dispatched_worker_ids=["w0", "w1"])


def test_activate_passes_none_for_count_all():
    k8s = MagicMock()
    ctrl, _ = _make_manager_controller(kubernetes_service_manager=k8s)
    _inject_placement(ctrl, "pipe-all", target_count="all", worker_ids=["w0", "w1", "w2"])

    config = MagicMock()
    config.name = "pipe-all"
    ctrl._activate_kubernetes_service(config)

    k8s.ensure_service.assert_called_once_with(config, dispatched_worker_ids=None)


def test_activate_passes_none_for_workers_list():
    k8s = MagicMock()
    ctrl, _ = _make_manager_controller(kubernetes_service_manager=k8s)
    _inject_placement(
        ctrl, "pipe-list", target_count=2,
        worker_ids=["tram-worker-0", "tram-worker-1"],
        workers_list=["tram-worker-0", "tram-worker-1"],
    )

    config = MagicMock()
    config.name = "pipe-list"
    ctrl._activate_kubernetes_service(config)

    k8s.ensure_service.assert_called_once_with(config, dispatched_worker_ids=None)


def test_activate_no_placement_passes_none():
    k8s = MagicMock()
    ctrl, _ = _make_manager_controller(kubernetes_service_manager=k8s)

    config = MagicMock()
    config.name = "no-placement"
    ctrl._activate_kubernetes_service(config)

    k8s.ensure_service.assert_called_once_with(config, dispatched_worker_ids=None)
