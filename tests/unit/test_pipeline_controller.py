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

        def _fake_stream_run(cfg, stop_event):
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

        def _fake_stream_run(cfg, stop_event):
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
        def _fake_stream_run(cfg, stop_event):
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
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)

        assert ctrl._scheduler.get_job("batch-my-interval") is not None
        ctrl.stop_pipeline("my-interval")

        assert ctrl._scheduler.get_job("batch-my-interval") is None
        assert ctrl.manager.get("my-interval").status == "stopped"
        ctrl.stop()

    def test_start_pipeline_reschedules_stopped_pipeline(self):
        ctrl = _started_controller()
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
        assert isinstance(run_id, str) and len(run_id) > 0

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
