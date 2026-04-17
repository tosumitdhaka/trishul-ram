"""Phase 3 regression tests for PipelineController (standalone mode).

Verifies that after removing coordinator / rebalance / sync machinery:
  - Controller initialises without cluster-era parameters
  - Batch pipelines schedule and run locally via executor.batch_run()
  - Stream pipelines start a thread and call executor.stream_run() locally
  - _may_schedule enforces exactly two guards: stopped-DB-flag + config.enabled
  - State machine transitions are correct (success→scheduled, failure→error, etc.)
  - _boot_load loads from DB, applies stopped flags, schedules enabled pipelines
  - No cluster DB methods (register_node, heartbeat, …) are called
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from tram.core.context import RunResult, RunStatus
from tram.pipeline.controller import PipelineController
from tram.pipeline.loader import load_pipeline_from_yaml
from tram.pipeline.manager import PipelineManager

# ── YAML fixtures ──────────────────────────────────────────────────────────


_INTERVAL_YAML = """\
name: my-interval
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

_CRON_YAML = """\
name: my-cron
schedule:
  type: cron
  cron: "0 * * * *"
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
  type: kafka
  topic: events
  brokers:
    - localhost:9092
  group_id: test-group
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""

_WEBHOOK_STREAM_YAML = """\
name: my-webhook-stream
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

_MANUAL_YAML = """\
name: my-manual
schedule:
  type: manual
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


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_result(name: str, status: RunStatus = RunStatus.SUCCESS) -> RunResult:
    from datetime import UTC, datetime
    return RunResult(
        run_id="r1",
        pipeline_name=name,
        status=status,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        records_in=10,
        records_out=10,
        records_skipped=0,
        error=None if status == RunStatus.SUCCESS else "boom",
        node_id="node-0",
    )


def _make_controller(
    db=None,
    worker_pool=None,
    manager_url="",
) -> PipelineController:
    """Build a controller with a patched BackgroundScheduler that doesn't start."""
    ctrl = PipelineController(
        db=db,
        node_id="test-node",
        worker_pool=worker_pool,
        manager_url=manager_url,
    )
    return ctrl


def _started_controller(**kwargs) -> PipelineController:
    """Controller with a running (real) BackgroundScheduler."""
    ctrl = _make_controller(**kwargs)
    ctrl.start()
    return ctrl


# ── Instantiation ──────────────────────────────────────────────────────────


class TestInstantiation:
    def test_no_cluster_params_accepted(self):
        """v1.2.0: controller must NOT accept cluster-era keyword args."""
        import inspect
        sig = inspect.signature(PipelineController.__init__)
        cluster_era_params = {
            "coordinator", "rebalance_interval", "stale_run_seconds",
            "heartbeat_seconds", "node_ttl_seconds",
        }
        for param in cluster_era_params:
            assert param not in sig.parameters, (
                f"Cluster-era param '{param}' should have been removed in v1.2.0"
            )

    def test_v120_params_present(self):
        """v1.2.0 params (worker_pool, manager_url) must be accepted."""
        import inspect
        sig = inspect.signature(PipelineController.__init__)
        assert "worker_pool" in sig.parameters
        assert "manager_url" in sig.parameters

    def test_creates_pipeline_manager(self):
        ctrl = _make_controller()
        assert isinstance(ctrl.manager, PipelineManager)

    def test_worker_pool_none_by_default(self):
        ctrl = _make_controller()
        assert ctrl._worker_pool is None


# ── _may_schedule (two guards only) ───────────────────────────────────────


class TestMaySchedule:
    def test_allowed_when_enabled_and_not_stopped(self):
        ctrl = _make_controller()
        ctrl.manager.register(
            load_pipeline_from_yaml(_INTERVAL_YAML), yaml_text=_INTERVAL_YAML
        )
        assert ctrl._may_schedule("my-interval") is True

    def test_blocked_when_config_disabled(self):
        yaml = _INTERVAL_YAML + "enabled: false\n"
        ctrl = _make_controller()
        ctrl.manager.register(load_pipeline_from_yaml(yaml), yaml_text=yaml)
        assert ctrl._may_schedule("my-interval") is False

    def test_blocked_when_db_stopped_flag(self):
        db = MagicMock()
        db.is_pipeline_stopped.return_value = True
        ctrl = _make_controller(db=db)
        ctrl.manager.register(
            load_pipeline_from_yaml(_INTERVAL_YAML), yaml_text=_INTERVAL_YAML
        )
        assert ctrl._may_schedule("my-interval") is False
        db.is_pipeline_stopped.assert_called_once_with("my-interval")

    def test_blocked_when_pipeline_not_registered(self):
        ctrl = _make_controller()
        assert ctrl._may_schedule("ghost") is False

    def test_no_coordinator_check(self):
        """_may_schedule must not touch a coordinator attribute (removed in v1.2.0)."""
        ctrl = _make_controller()
        assert not hasattr(ctrl, "_coordinator"), (
            "_coordinator should not exist on PipelineController in v1.2.0"
        )


# ── Batch execution — local path ───────────────────────────────────────────


class TestLocalBatchExecution:
    def test_run_batch_calls_executor(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.manager.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "scheduled")

        result = _make_result("my-interval", RunStatus.SUCCESS)
        ctrl.executor = MagicMock()
        ctrl.executor.batch_run.return_value = result

        ctrl._run_batch("my-interval", run_id="r1")

        ctrl.executor.batch_run.assert_called_once()
        ctrl.stop()

    def test_batch_success_leaves_scheduled_when_job_exists(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.manager.register(config, yaml_text=_INTERVAL_YAML)
        ctrl._do_schedule("my-interval")  # adds APScheduler job

        result = _make_result("my-interval", RunStatus.SUCCESS)
        ctrl.executor = MagicMock()
        ctrl.executor.batch_run.return_value = result

        ctrl._run_batch("my-interval", run_id="r2")
        assert ctrl.manager.get("my-interval").status == "scheduled"
        ctrl.stop()

    def test_batch_success_stops_manual_pipeline(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.manager.register(config, yaml_text=_MANUAL_YAML)
        # Status must NOT be "running" — that would trigger the already-running guard.
        # Simulate a trigger_run call: status is "stopped" before _run_batch fires.
        ctrl.manager.set_status("my-manual", "stopped")

        result = _make_result("my-manual", RunStatus.SUCCESS)
        ctrl.executor = MagicMock()
        ctrl.executor.batch_run.return_value = result

        ctrl._run_batch("my-manual", run_id="r3")
        assert ctrl.manager.get("my-manual").status == "stopped"
        ctrl.stop()

    def test_batch_failure_sets_error_status(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.manager.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "scheduled")

        result = _make_result("my-interval", RunStatus.FAILED)
        ctrl.executor = MagicMock()
        ctrl.executor.batch_run.return_value = result

        ctrl._run_batch("my-interval", run_id="r4")
        assert ctrl.manager.get("my-interval").status == "error"
        ctrl.stop()

    def test_batch_skips_when_already_running(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.manager.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "running")

        ctrl.executor = MagicMock()
        ctrl._run_batch("my-interval")
        ctrl.executor.batch_run.assert_not_called()
        ctrl.stop()

    def test_batch_skips_unknown_pipeline(self):
        ctrl = _started_controller()
        ctrl.executor = MagicMock()
        ctrl._run_batch("does-not-exist")
        ctrl.executor.batch_run.assert_not_called()
        ctrl.stop()

    def test_batch_no_worker_pool_calls_when_no_pool(self):
        """In standalone mode, no WorkerPool dispatch must occur."""
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.manager.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "scheduled")

        result = _make_result("my-interval", RunStatus.SUCCESS)
        ctrl.executor = MagicMock()
        ctrl.executor.batch_run.return_value = result

        assert ctrl._worker_pool is None
        ctrl._run_batch("my-interval")
        # executor.batch_run must be called (local path)
        ctrl.executor.batch_run.assert_called_once()
        ctrl.stop()


# ── Stream execution — local path ─────────────────────────────────────────


class TestLocalStreamExecution:
    def test_start_stream_spawns_thread(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_STREAM_YAML)
        ctrl.manager.register(config, yaml_text=_STREAM_YAML)

        done = threading.Event()

        def _fake_stream_run(cfg, stop_event, stats=None):
            done.wait(timeout=2)

        ctrl.executor = MagicMock()
        ctrl.executor.stream_run.side_effect = _fake_stream_run

        ctrl._start_stream(config)

        assert "my-stream" in ctrl._stream_threads
        assert ctrl._stream_threads["my-stream"].is_alive()
        assert ctrl.manager.get("my-stream").status == "running"

        done.set()
        ctrl.stop()

    def test_stop_stream_signals_stop_event(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_STREAM_YAML)
        ctrl.manager.register(config, yaml_text=_STREAM_YAML)

        received_stop = threading.Event()
        test_done = threading.Event()

        def _fake_stream_run(cfg, stop_event, stats=None):
            stop_event.wait(timeout=5)
            received_stop.set()
            test_done.wait(timeout=2)

        ctrl.executor = MagicMock()
        ctrl.executor.stream_run.side_effect = _fake_stream_run

        ctrl._start_stream(config)
        ctrl._stop_stream("my-stream", timeout=5)

        assert received_stop.wait(timeout=3), "stop_event was not set"
        test_done.set()
        ctrl.stop()

    def test_second_start_stream_does_not_duplicate_threads(self):
        """A second _start_stream call while the first is running waits then restarts.
        After the second call there must be exactly one live thread."""
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_STREAM_YAML)
        ctrl.manager.register(config, yaml_text=_STREAM_YAML)

        # First thread exits quickly when its stop_event fires
        def _fake_stream_run(cfg, stop_event, stats=None):
            stop_event.wait(timeout=5)

        ctrl.executor = MagicMock()
        ctrl.executor.stream_run.side_effect = _fake_stream_run

        ctrl._start_stream(config)

        # Signal the first stream thread to stop so it exits cleanly
        first_stop = ctrl._stop_events.get("my-stream")
        if first_stop:
            first_stop.set()
        first_thread = ctrl._stream_threads.get("my-stream")
        if first_thread:
            first_thread.join(timeout=3)

        # Start a second time — should start a fresh thread
        ctrl._start_stream(config)
        assert ctrl._stream_threads.get("my-stream") is not None
        assert ctrl._stream_threads["my-stream"].is_alive()

        ctrl.stop()


# ── Register / start_pipeline / stop_pipeline / trigger_run ───────────────


class TestLifecycle:
    def test_register_enabled_schedules_interval_pipeline(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)

        assert ctrl.manager.get("my-interval").status == "scheduled"
        job = ctrl._scheduler.get_job("batch-my-interval")
        assert job is not None
        ctrl.stop()

    def test_register_enabled_schedules_cron_pipeline(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_CRON_YAML)
        ctrl.register(config, yaml_text=_CRON_YAML)

        assert ctrl.manager.get("my-cron").status == "scheduled"
        job = ctrl._scheduler.get_job("batch-my-cron")
        assert job is not None
        ctrl.stop()

    def test_register_manual_pipeline_stays_stopped(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)

        assert ctrl.manager.get("my-manual").status == "stopped"
        ctrl.stop()

    def test_stop_pipeline_removes_scheduler_job(self):
        ctrl = _started_controller()
        ctrl._scheduler.pause()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)

        assert ctrl._scheduler.get_job("batch-my-interval") is not None
        ctrl.stop_pipeline("my-interval")

        assert ctrl._scheduler.get_job("batch-my-interval") is None
        assert ctrl.manager.get("my-interval").status == "stopped"
        ctrl.stop()

    def test_start_pipeline_reschedules_stopped_pipeline(self):
        ctrl = _started_controller()
        ctrl._scheduler.pause()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.stop_pipeline("my-interval")
        assert ctrl.manager.get("my-interval").status == "stopped"

        ctrl.start_pipeline("my-interval")
        assert ctrl.manager.get("my-interval").status == "scheduled"
        ctrl.stop()

    def test_trigger_run_submits_to_thread_pool(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "stopped")

        result = _make_result("my-interval", RunStatus.SUCCESS)
        ctrl.executor = MagicMock()
        ctrl.executor.batch_run.return_value = result

        run_id = ctrl.trigger_run("my-interval")
        assert str(uuid.UUID(run_id)) == run_id

        # Give the thread pool a moment to run
        time.sleep(0.1)
        ctrl.stop()

    def test_trigger_run_blocked_on_stream_pipeline(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_STREAM_YAML)
        ctrl.register(config, yaml_text=_STREAM_YAML)

        ctrl.executor = MagicMock()
        ctrl.executor.stream_run.side_effect = lambda *a, **kw: threading.Event().wait(5)
        # Start the stream so it's registered
        ctrl._start_stream(config)

        with pytest.raises(ValueError, match="stream pipeline"):
            ctrl.trigger_run("my-stream")
        ctrl.stop()

    def test_trigger_run_blocked_when_already_running(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "running")

        with pytest.raises(ValueError, match="already running"):
            ctrl.trigger_run("my-interval")
        ctrl.stop()


# ── _boot_load ─────────────────────────────────────────────────────────────


class TestBootLoad:
    def _make_db(self, pipelines=None, stopped=None):
        """DB mock with correct return types so manager.register() doesn't trip on MagicMocks.

        We return a synthetic recent run so that interval pipelines schedule
        their NEXT run ~interval_seconds from now (not immediately), preventing
        APScheduler from firing the job during the test assertion window.
        """
        from datetime import UTC, datetime

        recent_run = MagicMock()
        recent_run.finished_at = datetime.now(UTC)
        recent_run.status.value = "success"

        db = MagicMock()
        db.get_stopped_pipeline_names.return_value = stopped or []
        db.get_all_pipelines.return_value = pipelines or []
        # manager.register() calls get_runs to hydrate last_run
        db.get_runs.return_value = [recent_run]
        # _may_schedule calls is_pipeline_stopped — default not stopped
        db.is_pipeline_stopped.return_value = False
        return db

    def test_boot_load_schedules_enabled_pipelines(self):
        db = self._make_db(pipelines=[("my-interval", _INTERVAL_YAML)])
        ctrl = PipelineController(db=db, node_id="n0")
        ctrl.start()

        assert ctrl.manager.exists("my-interval")
        assert ctrl.manager.get("my-interval").status == "scheduled"
        ctrl.stop()

    def test_boot_load_applies_stopped_flags(self):
        db = self._make_db(
            pipelines=[("my-interval", _INTERVAL_YAML)],
            stopped=["my-interval"],
        )
        ctrl = PipelineController(db=db, node_id="n0")
        ctrl.start()

        state = ctrl.manager.get("my-interval")
        assert state.status == "stopped"
        assert ctrl._scheduler.get_job("batch-my-interval") is None
        ctrl.stop()

    def test_boot_load_skips_disabled_pipeline(self):
        yaml = _INTERVAL_YAML + "enabled: false\n"
        db = self._make_db(pipelines=[("my-interval", yaml)])
        ctrl = PipelineController(db=db, node_id="n0")
        ctrl.start()

        assert ctrl.manager.exists("my-interval")
        assert ctrl._scheduler.get_job("batch-my-interval") is None
        ctrl.stop()

    def test_boot_load_skips_unparsable_pipeline(self):
        db = self._make_db(pipelines=[
            ("bad-pipe", "this: is: not: valid: yaml: pipeline"),
            ("my-interval", _INTERVAL_YAML),
        ])
        ctrl = PipelineController(db=db, node_id="n0")
        ctrl.start()

        assert ctrl.manager.exists("my-interval")
        assert not ctrl.manager.exists("bad-pipe")
        ctrl.stop()

    def test_boot_load_does_not_call_cluster_db_methods(self):
        """No register_node / heartbeat / get_live_nodes calls during boot."""
        db = self._make_db()
        ctrl = PipelineController(db=db, node_id="n0")
        ctrl.start()
        ctrl.stop()

        db.register_node.assert_not_called()
        db.heartbeat.assert_not_called()
        db.get_live_nodes.assert_not_called()
        db.expire_nodes.assert_not_called()


# ── on_worker_run_complete (local reflection of worker callbacks) ──────────


class TestOnWorkerRunComplete:
    def test_preserves_worker_supplied_timestamps(self):
        from datetime import datetime

        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)
        ctrl.manager.set_status("my-manual", "running")

        started_at = datetime.fromisoformat("2026-04-16T09:00:00+00:00")
        finished_at = datetime.fromisoformat("2026-04-16T09:07:00+00:00")

        ctrl.on_worker_run_complete(
            run_id="r0",
            pipeline_name="my-manual",
            status="success",
            records_in=5,
            records_out=5,
            started_at=started_at,
            finished_at=finished_at,
        )

        last_run = ctrl.manager.get("my-manual").run_history[-1]
        assert last_run.started_at == started_at
        assert last_run.finished_at == finished_at
        ctrl.stop()

    def test_success_updates_manager_and_transitions_state(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "running")

        ctrl.on_worker_run_complete(
            run_id="r1",
            pipeline_name="my-interval",
            status="success",
            records_in=5,
            records_out=5,
            error=None,
        )

        # APScheduler job exists → should stay scheduled
        state = ctrl.manager.get("my-interval")
        assert state.status in ("scheduled", "stopped")
        ctrl.stop()

    def test_failure_sets_error_status(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)
        ctrl.manager.set_status("my-manual", "running")

        ctrl.on_worker_run_complete(
            run_id="r2",
            pipeline_name="my-manual",
            status="failed",
            records_in=0,
            records_out=0,
            error="connector error",
        )

        assert ctrl.manager.get("my-manual").status == "error"
        ctrl.stop()

    def test_noop_for_unknown_pipeline(self):
        ctrl = _started_controller()
        # Must not raise
        ctrl.on_worker_run_complete(
            run_id="r3",
            pipeline_name="ghost",
            status="success",
            records_in=0,
            records_out=0,
            error=None,
        )
        ctrl.stop()

    def test_errors_list_propagated(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)
        ctrl.manager.set_status("my-manual", "running")
        ctrl.on_worker_run_complete(
            run_id="r4",
            pipeline_name="my-manual",
            status="success",
            records_in=5,
            records_out=4,
            errors=["record skipped — condition filtered"],
        )
        # run was recorded — no exception
        ctrl.stop()

    def test_invalid_status_falls_back_to_failed(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)
        ctrl.manager.set_status("my-manual", "running")
        ctrl.on_worker_run_complete(
            run_id="r5",
            pipeline_name="my-manual",
            status="unknown_status_xyz",
            records_in=0,
            records_out=0,
        )
        assert ctrl.manager.get("my-manual").status == "error"
        ctrl.stop()

    def test_worker_pool_notified_on_complete(self):
        """WorkerPool.on_run_complete() must be called when worker_pool is set."""
        worker_pool = MagicMock()
        worker_pool.dispatch.return_value = "http://worker-0:8766"
        ctrl = _started_controller(worker_pool=worker_pool)
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)
        ctrl.manager.set_status("my-manual", "running")
        ctrl.on_worker_run_complete(
            run_id="r6",
            pipeline_name="my-manual",
            status="success",
            records_in=1,
            records_out=1,
        )
        worker_pool.on_run_complete.assert_called_once_with("r6")
        ctrl.stop()


# ── Worker dispatch path ───────────────────────────────────────────────────


class TestWorkerDispatch:
    def _worker_pool(self, dispatch_return="http://worker-0:8766"):
        wp = MagicMock()
        wp.dispatch.return_value = dispatch_return
        wp.multi_dispatch.return_value = MagicMock(
            accepted=["http://worker-0:8766"],
            run_ids=["pg1-w0"],
            rejected=[],
            status="running",
            placement_group_id="pg1",
        )
        return wp

    def test_run_batch_dispatches_to_worker(self):
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp, manager_url="http://manager:8765")
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        wp.dispatch.reset_mock()
        ctrl.manager.set_status("my-interval", "scheduled")

        ctrl._run_batch("my-interval", run_id="r1")

        wp.dispatch.assert_called_once()
        call_kwargs = wp.dispatch.call_args.kwargs
        assert call_kwargs["pipeline_name"] == "my-interval"
        assert "r1" in call_kwargs["run_id"] or call_kwargs["run_id"]
        ctrl.stop()

    def test_run_batch_generates_full_uuid_when_run_id_missing(self):
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp, manager_url="http://manager:8765")
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "scheduled")

        ctrl._run_batch("my-interval")

        generated_run_id = wp.dispatch.call_args.kwargs["run_id"]
        assert str(uuid.UUID(generated_run_id)) == generated_run_id
        ctrl.stop()

    def test_run_batch_no_healthy_workers_sets_error(self):
        wp = self._worker_pool(dispatch_return=None)
        ctrl = _started_controller(worker_pool=wp)
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "scheduled")

        ctrl._run_batch("my-interval")
        assert ctrl.manager.get("my-interval").status == "error"
        ctrl.stop()

    def test_start_stream_dispatches_to_worker(self):
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp)
        config = load_pipeline_from_yaml(_STREAM_YAML)
        ctrl.register(config, yaml_text=_STREAM_YAML)

        ctrl._start_stream(config)

        wp.dispatch.assert_called_once()
        generated_run_id = wp.dispatch.call_args.kwargs["run_id"]
        assert str(uuid.UUID(generated_run_id)) == generated_run_id
        assert ctrl.manager.get("my-stream").status == "running"
        ctrl.stop()

    def test_start_stream_http_push_dispatches_to_all_workers(self):
        wp = self._worker_pool()
        wp.multi_dispatch.return_value = MagicMock(
            accepted=["http://worker-0:8766", "http://worker-1:8766"],
            run_ids=["pg1-w0", "pg1-w1"],
            rejected=[],
            status="running",
            placement_group_id="pg1",
        )
        ctrl = _started_controller(worker_pool=wp)
        config = load_pipeline_from_yaml(_WEBHOOK_STREAM_YAML)
        ctrl.register(config, yaml_text=_WEBHOOK_STREAM_YAML)

        ctrl._start_stream(config)

        wp.multi_dispatch.assert_called_once()
        assert wp.dispatch.call_count == 0
        call_kwargs = wp.multi_dispatch.call_args.kwargs
        assert call_kwargs["workers_cfg"].count == "all"
        assert ctrl._stream_run_ids["my-webhook-stream"] == ["pg1-w0", "pg1-w1"]
        assert ctrl.manager.get("my-webhook-stream").status == "running"
        ctrl.stop()

    def test_start_stream_http_push_preserves_degraded_status(self):
        wp = self._worker_pool()
        wp.multi_dispatch.return_value = MagicMock(
            accepted=["http://worker-0:8766"],
            run_ids=["pg1-w0"],
            rejected=["http://worker-1:8766"],
            status="degraded",
            placement_group_id="pg1",
        )
        ctrl = _started_controller(worker_pool=wp)
        config = load_pipeline_from_yaml(_WEBHOOK_STREAM_YAML)
        ctrl.register(config, yaml_text=_WEBHOOK_STREAM_YAML)

        ctrl._start_stream(config)

        assert ctrl.manager.get("my-webhook-stream").status == "degraded"
        ctrl.stop()

    def test_start_stream_http_push_persists_broadcast_placement(self, tmp_path):
        from tram.persistence.db import TramDB

        wp = self._worker_pool()
        wp.worker_id_for_url.side_effect = lambda url: "w0" if url.endswith("0:8766") else "w1"
        wp.multi_dispatch.return_value = MagicMock(
            accepted=["http://worker-0:8766", "http://worker-1:8766"],
            run_ids=["pg1-w0", "pg1-w1"],
            rejected=[],
            status="running",
            placement_group_id="pg1",
        )
        db = TramDB(url=f"sqlite:///{tmp_path}/controller.db")
        ctrl = _started_controller(worker_pool=wp, db=db)
        config = load_pipeline_from_yaml(_WEBHOOK_STREAM_YAML)
        ctrl.register(config, yaml_text=_WEBHOOK_STREAM_YAML)

        ctrl._start_stream(config)

        placements = db.get_active_broadcast_placements()
        assert len(placements) == 1
        assert placements[0]["pipeline_name"] == "my-webhook-stream"
        assert [slot["current_run_id"] for slot in placements[0]["slots"]] == ["pg1-w0", "pg1-w1"]
        ctrl.stop()
        db.close()

    def test_boot_load_rehydrates_broadcast_placement_as_reconciling(self, tmp_path):
        from tram.persistence.db import TramDB

        db = TramDB(url=f"sqlite:///{tmp_path}/boot.db")
        db.save_pipeline("my-webhook-stream", _WEBHOOK_STREAM_YAML)
        db.save_broadcast_placement(
            placement_group_id="pg1",
            pipeline_name="my-webhook-stream",
            slots=[{
                "worker_index": 0,
                "worker_url": "http://worker-0:8766",
                "worker_id": "w0",
                "run_id_prefix": "pg1-w0",
                "current_run_id": "pg1-w0",
                "status": "running",
            }],
            target_count="all",
            status="running",
        )

        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp, db=db)

        assert ctrl.manager.get("my-webhook-stream").status == "reconciling"
        assert ctrl._stream_run_ids["my-webhook-stream"] == ["pg1-w0"]
        placements = db.get_active_broadcast_placements()
        assert placements[0]["status"] == "reconciling"
        wp.multi_dispatch.assert_not_called()
        ctrl.stop()
        db.close()

    def test_pipeline_stats_reconciles_rehydrated_slot_by_prefix(self, tmp_path):
        from tram.api.routers.internal import PipelineStatsPayload
        from tram.persistence.db import TramDB

        db = TramDB(url=f"sqlite:///{tmp_path}/reconcile.db")
        db.save_pipeline("my-webhook-stream", _WEBHOOK_STREAM_YAML)
        db.save_broadcast_placement(
            placement_group_id="pg1",
            pipeline_name="my-webhook-stream",
            slots=[{
                "worker_index": 0,
                "worker_url": "http://worker-0:8766",
                "worker_id": "w0",
                "run_id_prefix": "pg1-w0",
                "current_run_id": "pg1-w0",
                "status": "running",
            }],
            target_count="all",
            status="running",
        )

        ctrl = _started_controller(worker_pool=self._worker_pool(), db=db)
        payload = PipelineStatsPayload(
            worker_id="w0",
            pipeline_name="my-webhook-stream",
            run_id="pg1-w0-r1",
            schedule_type="stream",
            uptime_seconds=5.0,
            timestamp=datetime.now(UTC),
        )

        ctrl.on_pipeline_stats(payload)

        assert ctrl.manager.get("my-webhook-stream").status == "running"
        placements = db.get_active_broadcast_placements()
        assert placements[0]["slots"][0]["current_run_id"] == "pg1-w0-r1"
        ctrl.stop()
        db.close()

    def test_start_stream_no_healthy_workers_sets_error(self):
        wp = self._worker_pool(dispatch_return=None)
        ctrl = _started_controller(worker_pool=wp)
        config = load_pipeline_from_yaml(_STREAM_YAML)
        ctrl.register(config, yaml_text=_STREAM_YAML)

        ctrl._start_stream(config)
        assert ctrl.manager.get("my-stream").status == "error"
        ctrl.stop()

    def test_start_stream_second_call_is_no_op(self):
        """When stream already dispatched, second call is skipped."""
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp)
        config = load_pipeline_from_yaml(_STREAM_YAML)
        ctrl.register(config, yaml_text=_STREAM_YAML)

        ctrl._start_stream(config)
        ctrl._stream_run_ids["my-stream"] = ["existing-run-id"]
        ctrl._start_stream(config)
        assert wp.dispatch.call_count == 1  # only called once
        ctrl.stop()

    def test_stop_stream_calls_worker_stop(self):
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp)
        config = load_pipeline_from_yaml(_STREAM_YAML)
        ctrl.register(config, yaml_text=_STREAM_YAML)
        ctrl._stream_run_ids["my-stream"] = ["run-abc"]

        ctrl._stop_stream("my-stream")
        wp.stop_run.assert_called_once_with("run-abc", "my-stream")
        ctrl.stop()

    def test_stop_stream_broadcast_stops_all_pipeline_runs(self):
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp)
        config = load_pipeline_from_yaml(_WEBHOOK_STREAM_YAML)
        ctrl.register(config, yaml_text=_WEBHOOK_STREAM_YAML)
        ctrl._stream_run_ids["my-webhook-stream"] = ["pg1-w0", "pg1-w1"]
        ctrl._active_placement_group["my-webhook-stream"] = "pg1"
        ctrl._broadcast_placements["pg1"] = {
            "placement_group_id": "pg1",
            "pipeline_name": "my-webhook-stream",
            "slots": [],
            "target_count": "all",
            "started_at": datetime.now(UTC),
            "status": "running",
        }

        ctrl._stop_stream("my-webhook-stream")

        assert wp.stop_run.call_count == 2
        wp.stop_pipeline_runs.assert_called_once_with("my-webhook-stream")
        ctrl.stop()

    def test_worker_completion_removes_dispatched_stream_run_id(self):
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp)
        config = load_pipeline_from_yaml(_STREAM_YAML)
        ctrl.register(config, yaml_text=_STREAM_YAML)
        ctrl._stream_run_ids["my-stream"] = ["run-abc"]

        ctrl.on_worker_run_complete(
            run_id="run-abc",
            pipeline_name="my-stream",
            status="success",
            records_in=1,
            records_out=1,
        )

        assert "my-stream" not in ctrl._stream_run_ids
        ctrl.stop()

    def test_worker_completion_with_remaining_stream_slots_skips_state_transition(self):
        from tram.agent.stats_store import StatsStore
        from tram.api.routers.internal import PipelineStatsPayload

        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp)
        ctrl._stats_store = StatsStore(interval=30)
        config = load_pipeline_from_yaml(_WEBHOOK_STREAM_YAML)
        ctrl.register(config, yaml_text=_WEBHOOK_STREAM_YAML)
        ctrl._stream_run_ids["my-webhook-stream"] = ["run-a", "run-b"]
        ctrl.manager.set_status("my-webhook-stream", "running")
        ctrl.manager.record_run = MagicMock()
        ctrl._stats_store.update(PipelineStatsPayload(
            worker_id="w0",
            pipeline_name="my-webhook-stream",
            run_id="run-a",
            schedule_type="stream",
            uptime_seconds=5.0,
            timestamp=datetime.now(UTC),
        ))

        ctrl.on_worker_run_complete(
            run_id="run-a",
            pipeline_name="my-webhook-stream",
            status="success",
            records_in=1,
            records_out=1,
        )

        assert ctrl._stream_run_ids["my-webhook-stream"] == ["run-b"]
        ctrl.manager.record_run.assert_not_called()
        assert ctrl.manager.get("my-webhook-stream").status == "running"
        assert ctrl._stats_store.get_by_run_id("run-a") is not None
        ctrl.stop()


# ── update / delete / restart ─────────────────────────────────────────────


class TestUpdateDeleteRestart:
    def test_update_replaces_pipeline_and_restarts(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        assert ctrl.manager.get("my-interval").status == "scheduled"

        # Update with same YAML — pipeline should re-register and reschedule
        new_state = ctrl.update("my-interval", _INTERVAL_YAML)
        assert new_state is not None
        assert ctrl.manager.exists("my-interval")
        ctrl.stop()

    def test_update_stopped_pipeline_stays_stopped(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)
        # status is "stopped" (manual pipeline)
        ctrl.update("my-manual", _MANUAL_YAML)
        assert ctrl.manager.get("my-manual").status == "stopped"
        ctrl.stop()

    def test_update_saves_to_db(self):
        db = MagicMock()
        db.get_stopped_pipeline_names.return_value = []
        db.get_all_pipelines.return_value = []
        db.get_runs.return_value = []
        db.is_pipeline_stopped.return_value = False
        ctrl = PipelineController(db=db, node_id="n0")
        ctrl.start()
        ctrl.manager.register(
            load_pipeline_from_yaml(_INTERVAL_YAML), yaml_text=_INTERVAL_YAML
        )
        ctrl.update("my-interval", _INTERVAL_YAML)
        db.save_pipeline.assert_called_with("my-interval", _INTERVAL_YAML, source="api")
        ctrl.stop()

    def test_delete_deregisters_pipeline(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)
        ctrl.delete("my-manual")
        assert not ctrl.manager.exists("my-manual")
        ctrl.stop()

    def test_delete_calls_db(self):
        db = MagicMock()
        db.get_stopped_pipeline_names.return_value = []
        db.get_all_pipelines.return_value = []
        db.get_runs.return_value = []
        db.is_pipeline_stopped.return_value = False
        ctrl = PipelineController(db=db, node_id="n0")
        ctrl.start()
        ctrl.manager.register(
            load_pipeline_from_yaml(_MANUAL_YAML), yaml_text=_MANUAL_YAML
        )
        ctrl.delete("my-manual")
        db.delete_pipeline.assert_called_once_with("my-manual")
        ctrl.stop()

    def test_restart_interval_pipeline(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl._scheduler.pause()
        ctrl.manager.set_status("my-interval", "scheduled")

        ctrl.restart_pipeline("my-interval")
        # After restart the pipeline should be rescheduled
        assert ctrl.manager.get("my-interval").status == "scheduled"
        ctrl.stop()

    def test_restart_stopped_pipeline_schedules(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl._scheduler.pause()
        ctrl.stop_pipeline("my-interval")
        assert ctrl.manager.get("my-interval").status == "stopped"

        ctrl.restart_pipeline("my-interval")
        assert ctrl.manager.get("my-interval").status == "scheduled"
        ctrl.stop()


# ── start_pipeline edge cases ─────────────────────────────────────────────


class TestStartPipelineEdgeCases:
    def test_start_pipeline_already_running_is_noop(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "running")

        # Should not raise and should leave status as running
        ctrl.start_pipeline("my-interval")
        assert ctrl.manager.get("my-interval").status == "running"
        ctrl.stop()

    def test_start_pipeline_disabled_sets_stopped(self):
        yaml = _INTERVAL_YAML + "enabled: false\n"
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(yaml)
        ctrl.manager.register(config, yaml_text=yaml)

        ctrl.start_pipeline("my-interval")
        assert ctrl.manager.get("my-interval").status == "stopped"
        ctrl.stop()

    def test_stop_pipeline_persists_to_db(self):
        db = MagicMock()
        db.get_stopped_pipeline_names.return_value = []
        db.get_all_pipelines.return_value = []
        db.get_runs.return_value = []
        db.is_pipeline_stopped.return_value = False
        ctrl = PipelineController(db=db, node_id="n0")
        ctrl.start()
        ctrl.manager.register(
            load_pipeline_from_yaml(_INTERVAL_YAML), yaml_text=_INTERVAL_YAML
        )
        ctrl.stop_pipeline("my-interval")
        db.stop_pipeline.assert_called_once_with("my-interval")
        ctrl.stop()


# ── get_scheduler_status ──────────────────────────────────────────────────


class TestGetSchedulerStatus:
    def test_returns_expected_keys(self):
        ctrl = _started_controller()
        status = ctrl.get_scheduler_status()
        assert "scheduler_running" in status
        assert "active_streams" in status
        assert "scheduled_jobs" in status
        ctrl.stop()

    def test_includes_worker_pool_status_when_set(self):
        wp = MagicMock()
        wp.status.return_value = {"workers": []}
        ctrl = _started_controller(worker_pool=wp)
        status = ctrl.get_scheduler_status()
        assert status["workers"] == {"workers": []}
        ctrl.stop()
