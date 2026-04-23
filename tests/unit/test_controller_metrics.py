"""Tests for manager-side Prometheus instrumentation in PipelineController (v1.3.2 PR-B)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from tram.pipeline.controller import PipelineController
from tram.pipeline.loader import load_pipeline_from_yaml

_BATCH_YAML = """\
name: my-batch
schedule:
  type: interval
  interval_seconds: 3600
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

_STREAM_YAML = """\
name: my-stream
schedule:
  type: stream
source:
  type: webhook
  path: /ingest
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""


def _make_manager_controller():
    worker_pool = MagicMock()
    ctrl = PipelineController(
        node_id="mgr",
        worker_pool=worker_pool,
        manager_url="http://manager:8765",
    )
    ctrl.manager = MagicMock()
    ctrl.executor = MagicMock()
    ctrl._scheduler = MagicMock()
    return ctrl, worker_pool


# ── Batch dispatch: accepted increments counter ───────────────────────────


def test_batch_dispatch_accepted_increments_counter():
    ctrl, worker_pool = _make_manager_controller()
    config = load_pipeline_from_yaml(_BATCH_YAML)
    state = MagicMock()
    state.config = config
    state.yaml_text = _BATCH_YAML
    state.status = "scheduled"
    ctrl.manager.exists.return_value = True
    ctrl.manager.get.return_value = state
    worker_pool.dispatch.return_value = "http://w0:8766"

    counter = MagicMock()
    counter.labels.return_value = counter

    with patch("tram.metrics.registry.MGR_DISPATCH_TOTAL", counter):
        ctrl._run_batch("my-batch")

    counter.labels.assert_called_with(pipeline="my-batch", result="accepted")
    counter.inc.assert_called_once()


# ── Batch dispatch: no workers increments no_workers label ────────────────


def test_batch_dispatch_no_workers_increments_counter():
    ctrl, worker_pool = _make_manager_controller()
    config = load_pipeline_from_yaml(_BATCH_YAML)
    state = MagicMock()
    state.config = config
    state.yaml_text = _BATCH_YAML
    state.status = "scheduled"
    ctrl.manager.exists.return_value = True
    ctrl.manager.get.return_value = state
    worker_pool.dispatch.return_value = None  # no workers

    counter = MagicMock()
    counter.labels.return_value = counter

    with patch("tram.metrics.registry.MGR_DISPATCH_TOTAL", counter):
        ctrl._run_batch("my-batch")

    counter.labels.assert_called_with(pipeline="my-batch", result="no_workers")
    counter.inc.assert_called_once()


# ── Stream single-dispatch: accepted increments counter ──────────────────


def test_stream_single_dispatch_accepted_increments_counter():
    ctrl, worker_pool = _make_manager_controller()
    config = load_pipeline_from_yaml(_STREAM_YAML)
    config = config.model_copy(update={"workers": None})
    state = MagicMock()
    state.config = config
    state.yaml_text = _STREAM_YAML
    ctrl.manager.exists.return_value = True
    ctrl.manager.get.return_value = state
    ctrl._stream_run_ids = {}
    worker_pool.dispatch.return_value = "http://w0:8766"

    counter = MagicMock()
    counter.labels.return_value = counter

    with patch("tram.metrics.registry.MGR_DISPATCH_TOTAL", counter):
        ctrl._start_stream(config)

    counter.labels.assert_called_with(pipeline="my-stream", result="accepted")
    counter.inc.assert_called_once()


# ── Placement status gauge transitions ────────────────────────────────────


def test_placement_status_gauge_set_on_status_update():
    ctrl, _ = _make_manager_controller()
    ctrl._broadcast_placements["pg1"] = {
        "placement_group_id": "pg1",
        "pipeline_name": "my-stream",
        "status": "reconciling",
        "slots": [],
    }
    ctrl.manager.exists.return_value = True

    gauge = MagicMock()
    gauge.labels.return_value = gauge

    with patch("tram.metrics.registry.MGR_PLACEMENT_STATUS", gauge):
        ctrl._update_broadcast_placement_status("pg1", "running")

    # Should set 1 for "running" and 0 for others
    calls = {call.kwargs["status"]: call for call in gauge.labels.call_args_list}
    assert "running" in calls
    assert "degraded" in calls

    # Verify set(1) for running
    set_calls = gauge.set.call_args_list
    values = {gauge.labels.call_args_list[i].kwargs["status"]: set_calls[i].args[0]
              for i in range(len(set_calls))}
    assert values.get("running") == 1
    assert values.get("degraded") == 0
