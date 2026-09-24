"""Tests for manager-side Prometheus instrumentation in PipelineController (v1.3.2 PR-B)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from tram.agent.worker_pool import (
    DISPATCH_ACCEPTED,
    DISPATCH_FAILED,
    DISPATCH_NO_CAPACITY,
    DispatchOutcome,
)
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
        # The "Stream single-dispatch" metric tests below exercise the legacy
        # count=1 dispatch path (D.2 flag off) — that is the path whose labels
        # they assert parity for.
        single_stream_placements=False,
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
    # Real manager.get_run returns None for a run that has not been recorded
    # yet (the post-dispatch CAS consults it to skip stale leases, GH #47).
    ctrl.manager.get_run.return_value = None
    worker_pool.dispatch_with_result.return_value = DispatchOutcome(
        worker_url="http://w0:8766", outcome=DISPATCH_ACCEPTED,
    )

    counter = MagicMock()
    counter.labels.return_value = counter

    with patch("tram.metrics.registry.MGR_DISPATCH_TOTAL", counter):
        ctrl._run_batch("my-batch")

    counter.labels.assert_called_with(pipeline="my-batch", result="accepted")
    counter.inc.assert_called_once()


def test_batch_dispatch_fast_run_increments_accepted_counter():
    """C7: the post-dispatch CAS fast-run early return (the run completed
    before the lease was recorded) must still count an accepted dispatch —
    previously that early return skipped the MGR_DISPATCH_TOTAL{accepted}
    increment, making fast runs invisible in the dispatch metrics."""
    ctrl, worker_pool = _make_manager_controller()
    config = load_pipeline_from_yaml(_BATCH_YAML)
    state = MagicMock()
    state.config = config
    state.yaml_text = _BATCH_YAML
    state.status = "scheduled"
    ctrl.manager.exists.return_value = True
    ctrl.manager.get.return_value = state
    # The worker completed the run before the dispatch thread re-acquired the
    # lock — the CAS sees the run already recorded and skips the lease.
    ctrl.manager.get_run.return_value = MagicMock()
    worker_pool.dispatch_with_result.return_value = DispatchOutcome(
        worker_url="http://w0:8766", outcome=DISPATCH_ACCEPTED,
    )

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
    worker_pool.dispatch_with_result.return_value = DispatchOutcome(
        worker_url=None, outcome=DISPATCH_NO_CAPACITY,
    )  # no workers

    counter = MagicMock()
    counter.labels.return_value = counter

    with patch("tram.metrics.registry.MGR_DISPATCH_TOTAL", counter):
        ctrl._run_batch("my-batch")

    counter.labels.assert_called_with(pipeline="my-batch", result="no_workers")
    counter.inc.assert_called_once()


# ── Batch dispatch: dispatch attempt failed increments dispatch_failed ────


def test_batch_dispatch_failure_increments_dispatch_failed_counter():
    ctrl, worker_pool = _make_manager_controller()
    config = load_pipeline_from_yaml(_BATCH_YAML)
    state = MagicMock()
    state.config = config
    state.yaml_text = _BATCH_YAML
    state.status = "scheduled"
    ctrl.manager.exists.return_value = True
    ctrl.manager.get.return_value = state
    worker_pool.dispatch_with_result.return_value = DispatchOutcome(
        worker_url=None, outcome=DISPATCH_FAILED, error="HTTP 503",
    )

    counter = MagicMock()
    counter.labels.return_value = counter

    with patch("tram.metrics.registry.MGR_DISPATCH_TOTAL", counter):
        ctrl._run_batch("my-batch")

    counter.labels.assert_called_with(pipeline="my-batch", result="dispatch_failed")
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
    worker_pool.dispatch_with_result.return_value = DispatchOutcome(
        worker_url="http://w0:8766", outcome=DISPATCH_ACCEPTED,
    )

    counter = MagicMock()
    counter.labels.return_value = counter

    with patch("tram.metrics.registry.MGR_DISPATCH_TOTAL", counter):
        ctrl._start_stream(config)

    counter.labels.assert_called_with(pipeline="my-stream", result="accepted")
    counter.inc.assert_called_once()


# ── Stream single-dispatch: no workers increments no_workers label ────────


def test_stream_dispatch_no_workers_increments_counter():
    ctrl, worker_pool = _make_manager_controller()
    config = load_pipeline_from_yaml(_STREAM_YAML)
    config = config.model_copy(update={"workers": None})
    state = MagicMock()
    state.config = config
    state.yaml_text = _STREAM_YAML
    ctrl.manager.exists.return_value = True
    ctrl.manager.get.return_value = state
    ctrl._stream_run_ids = {}
    worker_pool.dispatch_with_result.return_value = DispatchOutcome(
        worker_url=None, outcome=DISPATCH_NO_CAPACITY,
    )  # no workers

    counter = MagicMock()
    counter.labels.return_value = counter

    with patch("tram.metrics.registry.MGR_DISPATCH_TOTAL", counter):
        ctrl._start_stream(config)

    counter.labels.assert_called_with(pipeline="my-stream", result="no_workers")
    counter.inc.assert_called_once()


# ── Stream single-dispatch: dispatch attempt failed increments dispatch_failed ──


def test_stream_dispatch_failure_increments_dispatch_failed_counter():
    ctrl, worker_pool = _make_manager_controller()
    config = load_pipeline_from_yaml(_STREAM_YAML)
    config = config.model_copy(update={"workers": None})
    state = MagicMock()
    state.config = config
    state.yaml_text = _STREAM_YAML
    ctrl.manager.exists.return_value = True
    ctrl.manager.get.return_value = state
    ctrl._stream_run_ids = {}
    worker_pool.dispatch_with_result.return_value = DispatchOutcome(
        worker_url=None, outcome=DISPATCH_FAILED, error="HTTP 503",
    )

    counter = MagicMock()
    counter.labels.return_value = counter

    with patch("tram.metrics.registry.MGR_DISPATCH_TOTAL", counter):
        ctrl._start_stream(config)

    counter.labels.assert_called_with(pipeline="my-stream", result="dispatch_failed")
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


# ── PipelineStats.reset (retry parity) ─────────────────────────────────────


def test_pipeline_stats_reset_zeroes_counters():
    from tram.agent.metrics import PipelineStats

    stats = PipelineStats(run_id="r1", pipeline_name="p1", schedule_type="batch")
    stats.increment(
        records_in=10, records_out=8, skipped=1, dlq=1,
        bytes_in=100, bytes_out=80, errors=["boom"],
    )
    assert stats.snapshot()["records_in"] == 10

    stats.reset()

    snapshot = stats.snapshot()
    assert snapshot == {
        "records_in": 0,
        "records_out": 0,
        "records_skipped": 0,
        "dlq_count": 0,
        "error_count": 0,
        "bytes_in": 0,
        "bytes_out": 0,
        "errors_last_window": [],
    }


def test_pipeline_stats_reset_preserves_run_identity():
    """reset() zeroes counters in-place; run metadata survives."""
    from tram.agent.metrics import PipelineStats

    stats = PipelineStats(run_id="r1", pipeline_name="p1", schedule_type="batch")
    stats.increment(records_in=5)
    stats.reset()
    assert stats.run_id == "r1"
    assert stats.pipeline_name == "p1"
    assert stats.schedule_type == "batch"
