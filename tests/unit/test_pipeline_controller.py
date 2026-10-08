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
from sqlalchemy import text

from tram.agent.worker_pool import (
    DISPATCH_ACCEPTED,
    DISPATCH_FAILED,
    DISPATCH_NO_CAPACITY,
    DispatchOutcome,
)
from tram.core.context import RunResult, RunStatus
from tram.persistence.ledger import CLAIMED, claim_run
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

_COUNT_N_STREAM_YAML = """\
name: my-count-n-stream
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
workers:
  count: 2
sinks:
  - type: local
    path: /tmp/out
"""

_LIST_STREAM_YAML = """\
name: my-list-stream
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
workers:
  list:
    - tram-worker-0
    - tram-worker-1
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
    kubernetes_service_manager=None,
    single_stream_placements=True,
) -> PipelineController:
    """Build a controller with a patched BackgroundScheduler that doesn't start."""
    ctrl = PipelineController(
        db=db,
        node_id="test-node",
        worker_pool=worker_pool,
        manager_url=manager_url,
        kubernetes_service_manager=kubernetes_service_manager,
        single_stream_placements=single_stream_placements,
    )
    return ctrl


def _started_controller(**kwargs) -> PipelineController:
    """Controller with a running (real) BackgroundScheduler."""
    ctrl = _make_controller(**kwargs)
    ctrl.start()
    return ctrl


def _accepted_worker_pool(worker_url: str = "http://worker-0:8766") -> MagicMock:
    """MagicMock worker pool whose dispatch is always accepted."""
    wp = MagicMock()
    wp.healthy_workers.return_value = [worker_url]
    wp.dispatch_with_result.return_value = DispatchOutcome(
        worker_url=worker_url, outcome=DISPATCH_ACCEPTED,
    )
    return wp


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


class TestKubernetesServiceLifecycle:
    def test_manager_stream_activation_creates_pipeline_service(self):
        wp = MagicMock()
        wp.dispatch_with_result.return_value = DispatchOutcome(
            worker_url="http://worker-0:8766", outcome=DISPATCH_ACCEPTED,
        )
        k8s = MagicMock()
        ctrl = _make_controller(
            worker_pool=wp,
            manager_url="http://manager:8765",
            kubernetes_service_manager=k8s,
        )
        config = load_pipeline_from_yaml(
            """\
name: my-webhook-stream
schedule:
  type: stream
source:
  type: webhook
  path: /ingest
serializer_in:
  type: json
kubernetes:
  enabled: true
  service_type: NodePort
  node_port: 30042
sinks:
  - type: local
    path: /tmp/out
"""
        )
        ctrl.manager.register(config, yaml_text="test")
        ctrl._start_stream(config)
        k8s.ensure_service.assert_called_once_with(config, dispatched_worker_ids=None)

    def test_stop_execution_deletes_pipeline_service(self):
        k8s = MagicMock()
        ctrl = _make_controller(kubernetes_service_manager=k8s)
        config = load_pipeline_from_yaml(
            """\
name: my-webhook-stream
schedule:
  type: stream
source:
  type: webhook
  path: /ingest
serializer_in:
  type: json
kubernetes:
  enabled: true
sinks:
  - type: local
    path: /tmp/out
"""
        )
        ctrl.manager.register(config, yaml_text="test")
        ctrl._stream_run_ids[config.name] = ["run-1"]
        ctrl._stop_execution(config.name)
        k8s.delete_service.assert_called_once_with(config)

    def test_boot_restore_reconciles_pipeline_service(self):
        db = MagicMock()
        db.get_stopped_pipeline_names.return_value = []
        db.get_all_pipelines.return_value = [("my-webhook-stream", _WEBHOOK_STREAM_YAML.replace(
            "serializer_in:\n  type: json\n",
            "serializer_in:\n  type: json\nkubernetes:\n  enabled: true\n",
        ))]
        db.get_active_broadcast_placements.return_value = [
            {
                "placement_group_id": "pg1",
                "pipeline_name": "my-webhook-stream",
                "slots": [
                    {
                        "worker_index": 0,
                        "worker_url": "http://worker-0:8766",
                        "worker_id": "tram-worker-0",
                        "run_id_prefix": "pg1-w0",
                        "current_run_id": "pg1-w0",
                        "status": "running",
                    }
                ],
            }
        ]
        k8s = MagicMock()
        ctrl = _make_controller(db=db, kubernetes_service_manager=k8s)
        ctrl._boot_load()
        k8s.ensure_service.assert_called_once()


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

        def _fake_stream_run(cfg, stop_event, stats=None, config_sha256=""):
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

        def _fake_stream_run(cfg, stop_event, stats=None, config_sha256=""):
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
        def _fake_stream_run(cfg, stop_event, stats=None, config_sha256=""):
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

        trigger = ctrl.trigger_run("my-interval")
        assert trigger.disposition == "dispatched"
        run_id = trigger.run_id
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
        db.save_pipeline_version.assert_not_called()
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

    # ── B.6: adopt-or-skip guard for count=1 streams on manager restart ─────

    def test_boot_load_adopts_live_count1_stream_without_redispatch(self):
        """A count=1 stream still live on a worker after a manager restart is
        adopted (lease recorded, no re-dispatch) — exactly zero dispatches, so
        no second concurrent instance and no duplicate sink writes."""
        wp = MagicMock()
        wp.find_pipeline_runs.return_value = [{
            "worker_url": "http://worker-0:8766",
            "run_id": "live-run-1",
            "pipeline_name": "my-stream",
            "started_at": "2026-09-01T10:00:00+00:00",
            "schedule_type": "stream",
        }]
        db = self._make_db(pipelines=[("my-stream", _STREAM_YAML)])
        db.get_active_broadcast_placements.return_value = []
        ctrl = _make_controller(db=db, worker_pool=wp, manager_url="http://manager:8765")
        ctrl.start()

        try:
            assert wp.dispatch_with_result.call_count == 0, (
                "boot must not re-dispatch an adopted live stream"
            )
            assert wp.find_pipeline_runs.call_count == 1
            wp.adopt_stream_assignment.assert_called_once_with(
                pipeline_name="my-stream",
                run_id="live-run-1",
                worker_url="http://worker-0:8766",
            )
            # Status and placement views reflect the adopted run.
            assert ctrl._stream_run_ids["my-stream"] == ["live-run-1"]
            assert ctrl.manager.get("my-stream").status == "running"
        finally:
            ctrl.stop()

    def test_boot_load_redispatches_count1_stream_when_no_live_run(self):
        """Legacy flag-off regression (D.2): no worker reports the stream as
        live → normal re-dispatch (the pre-restart instance is gone, so a fresh
        dispatch is safe)."""
        wp = MagicMock()
        wp.find_pipeline_runs.return_value = []
        wp.dispatch_with_result.return_value = DispatchOutcome(
            worker_url="http://worker-0:8766", outcome=DISPATCH_ACCEPTED,
        )
        db = self._make_db(pipelines=[("my-stream", _STREAM_YAML)])
        db.get_active_broadcast_placements.return_value = []
        ctrl = _make_controller(
            db=db, worker_pool=wp, manager_url="http://manager:8765",
            single_stream_placements=False,
        )
        ctrl.start()

        try:
            assert wp.dispatch_with_result.call_count == 1
            wp.adopt_stream_assignment.assert_not_called()
            assert ctrl.manager.get("my-stream").status == "running"
            assert len(ctrl._stream_run_ids["my-stream"]) == 1
        finally:
            ctrl.stop()

    def test_boot_load_does_not_adopt_broadcast_stream(self):
        """The adopt guard is scoped to count=1 streams: broadcast placements
        are restored from durable records (D.2), never adopted from live probes."""
        wp = MagicMock()
        wp.find_pipeline_runs.return_value = [{
            "worker_url": "http://worker-0:8766",
            "run_id": "live-run-1",
            "pipeline_name": "my-count-n-stream",
            "started_at": "2026-09-01T10:00:00+00:00",
            "schedule_type": "stream",
        }]
        result = MagicMock()
        result.accepted = ["http://worker-0:8766"]
        result.run_ids = ["pg-x-w0"]
        result.status = "running"
        result.slots = [{
            "worker_index": 0,
            "worker_url": "http://worker-0:8766",
            "worker_id": "tram-worker-0",
            "pinned_worker_id": None,
            "run_id_prefix": "pg-x-w0",
            "current_run_id": "pg-x-w0",
            "status": "running",
            "restart_count": 0,
        }]
        wp.multi_dispatch.return_value = result
        db = self._make_db(pipelines=[("my-count-n-stream", _COUNT_N_STREAM_YAML)])
        db.get_active_broadcast_placements.return_value = []
        ctrl = _make_controller(db=db, worker_pool=wp, manager_url="http://manager:8765")
        ctrl.start()

        try:
            assert wp.find_pipeline_runs.call_count == 0
            wp.multi_dispatch.assert_called_once()
            wp.adopt_stream_assignment.assert_not_called()
        finally:
            ctrl.stop()


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
            worker_id="worker-1",
            status="success",
            records_in=5,
            records_out=5,
            started_at=started_at,
            finished_at=finished_at,
        )

        last_run = ctrl.manager.get("my-manual").run_history[-1]
        assert last_run.started_at == started_at
        assert last_run.finished_at == finished_at
        assert last_run.node_id == "worker-1"
        ctrl.stop()

    def test_success_updates_manager_and_transitions_state(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "running")

        ctrl.on_worker_run_complete(
            run_id="r1",
            pipeline_name="my-interval",
            worker_id="worker-1",
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
            worker_id="worker-1",
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
            worker_id="worker-1",
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
            worker_id="worker-1",
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
            worker_id="worker-1",
            status="unknown_status_xyz",
            records_in=0,
            records_out=0,
        )
        assert ctrl.manager.get("my-manual").status == "error"
        ctrl.stop()

    def test_worker_pool_notified_on_complete(self):
        """WorkerPool.on_run_complete() must be called when worker_pool is set."""
        worker_pool = MagicMock()
        worker_pool.dispatch_with_result.return_value = DispatchOutcome(
            worker_url="http://worker-0:8766", outcome=DISPATCH_ACCEPTED,
        )
        ctrl = _started_controller(worker_pool=worker_pool)
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)
        ctrl.manager.set_status("my-manual", "running")
        ctrl.on_worker_run_complete(
            run_id="r6",
            pipeline_name="my-manual",
            worker_id="worker-1",
            status="success",
            records_in=1,
            records_out=1,
        )
        worker_pool.on_run_complete.assert_called_once_with("r6")
        ctrl.stop()

    def test_worker_batch_completion_clears_active_batch_lease(self):
        worker_pool = MagicMock()
        worker_pool.dispatch_with_result.return_value = DispatchOutcome(
            worker_url="http://worker-0:8766", outcome=DISPATCH_ACCEPTED,
        )
        ctrl = _started_controller(worker_pool=worker_pool, manager_url="http://manager:8765")
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)
        ctrl.manager.set_status("my-manual", "scheduled")

        ctrl._run_batch("my-manual", run_id="rb1")
        assert ctrl.get_active_batch_runs()[0]["run_id"] == "rb1"

        ctrl.on_worker_run_complete(
            run_id="rb1",
            pipeline_name="my-manual",
            worker_id="worker-1",
            status="success",
            records_in=1,
            records_out=1,
        )

        assert ctrl.get_active_batch_runs() == []
        ctrl.stop()

    def test_worker_node_id_is_persisted_on_callback_result(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)
        ctrl.manager.set_status("my-manual", "running")

        ctrl.on_worker_run_complete(
            run_id="r-node",
            pipeline_name="my-manual",
            worker_id="tram-worker-3",
            status="success",
            records_in=1,
            records_out=1,
        )

        last_run = ctrl.manager.get("my-manual").run_history[-1]
        assert last_run.node_id == "tram-worker-3"
        ctrl.stop()


# ── Worker dispatch path ───────────────────────────────────────────────────


class TestWorkerDispatch:
    def _worker_pool(self, dispatch_return="http://worker-0:8766", dispatch_error=None):
        wp = MagicMock()
        if dispatch_error is not None:
            wp.dispatch_with_result.return_value = DispatchOutcome(
                worker_url=None, outcome=DISPATCH_FAILED, error=dispatch_error,
            )
        elif dispatch_return is None:
            wp.dispatch_with_result.return_value = DispatchOutcome(
                worker_url=None,
                outcome=DISPATCH_NO_CAPACITY,
                error="No healthy workers available for dispatch",
            )
        else:
            wp.dispatch_with_result.return_value = DispatchOutcome(
                worker_url=dispatch_return, outcome=DISPATCH_ACCEPTED,
            )
        wp.multi_dispatch.return_value = MagicMock(
            accepted=["http://worker-0:8766"],
            run_ids=["pg1-w0"],
            rejected=[],
            status="running",
            placement_group_id="pg1",
            slots=[{
                "worker_index": 0,
                "worker_url": "http://worker-0:8766",
                "run_id_prefix": "pg1-w0",
                "current_run_id": "pg1-w0",
                "status": "running",
                "restart_count": 0,
            }],
        )
        return wp

    def test_run_batch_dispatches_to_worker(self):
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp, manager_url="http://manager:8765")
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        wp.dispatch_with_result.reset_mock()
        ctrl.manager.set_status("my-interval", "scheduled")

        ctrl._run_batch("my-interval", run_id="r1")

        wp.dispatch_with_result.assert_called_once()
        call_kwargs = wp.dispatch_with_result.call_args.kwargs
        assert call_kwargs["pipeline_name"] == "my-interval"
        assert "r1" in call_kwargs["run_id"] or call_kwargs["run_id"]
        ctrl.stop()

    def test_run_batch_tracks_active_batch_lease(self):
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp, manager_url="http://manager:8765")
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "scheduled")

        ctrl._run_batch("my-interval", run_id="r-batch")

        assert ctrl.get_active_batch_runs() == [{
            "run_id": "r-batch",
            "pipeline_name": "my-interval",
            "worker_url": "http://worker-0:8766",
            "schedule_type": "interval",
            "started_at": ctrl.get_active_batch_runs()[0]["started_at"],
        }]
        ctrl.stop()

    def test_manual_claim_race_records_failed_row_under_submitted_run_id(self):
        """Trigger/claim TOCTOU (GH #47): a manual trigger that loses the claim
        race to a scheduled fire must leave a run-history row under the
        SUBMITTED run_id (so the client's run_id resolves) instead of silence —
        while the winning run's status is not clobbered to 'error'."""
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        # A scheduled fire claimed the run first.
        ctrl.manager.set_status("my-interval", "running")

        ctrl._run_batch("my-interval", run_id="manual-race-1", origin="manual")

        result = ctrl.manager.get_run("manual-race-1")
        assert result is not None
        assert result.status == RunStatus.FAILED
        assert result.pipeline_name == "my-interval"
        assert "skipped" in (result.error or "")
        # The winning run is genuinely active — status stays 'running'.
        assert ctrl.manager.get("my-interval").status == "running"
        ctrl.stop()

    def test_scheduled_claim_race_stays_silent(self):
        """Scheduled fires keep their bounded-loss behavior: no run-history
        row is written for a skipped scheduled claim (no client holds its
        run_id), only manual-origin claims get the FAILED row."""
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "queued")

        ctrl._run_batch("my-interval", run_id="sched-race-1")  # origin defaults to scheduled

        assert ctrl.manager.get_run("sched-race-1") is None
        assert ctrl.manager.get("my-interval").status == "queued"
        ctrl.stop()

    def test_fast_dispatched_run_skips_stale_lease(self):
        """Fast-run stale lease (GH #47): when the worker finishes and posts
        run-complete before the dispatch thread re-acquires the lock, the CAS
        must skip recording the lease — otherwise the BatchReconciler would
        probe is_run_active() → False and mark a succeeded run as lost."""
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp, manager_url="http://manager:8765")
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "scheduled")

        def _dispatch_then_worker_completes(**kwargs):
            # The worker finishes before the dispatch thread records the lease.
            ctrl.on_worker_run_complete(
                run_id=kwargs["run_id"],
                pipeline_name="my-interval",
                worker_id="tram-worker-0",
                status="success",
                records_in=3,
                records_out=3,
            )
            return DispatchOutcome(worker_url="http://worker-0:8766", outcome=DISPATCH_ACCEPTED)

        wp.dispatch_with_result.side_effect = _dispatch_then_worker_completes

        ctrl._run_batch("my-interval", run_id="fast-1")

        assert ctrl.get_active_batch_runs() == []  # no stale lease
        state = ctrl.manager.get("my-interval")
        assert state.status != "error"
        assert state.run_history[0].run_id == "fast-1"
        assert state.run_history[0].status == RunStatus.SUCCESS
        ctrl.stop()

    def test_mark_active_batch_run_lost_records_failed_run(self):
        wp = self._worker_pool()
        wp.worker_id_for_url.return_value = "tram-worker-0"
        ctrl = _started_controller(worker_pool=wp, manager_url="http://manager:8765")
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "scheduled")

        ctrl._run_batch("my-interval", run_id="lost-1")
        assert len(ctrl.get_active_batch_runs()) == 1

        assert ctrl.mark_active_batch_run_lost(
            "my-interval",
            error="worker-owned batch run disappeared before callback: lost-1",
            run_id="lost-1",
        ) is True

        assert ctrl.get_active_batch_runs() == []
        state = ctrl.manager.get("my-interval")
        assert state.status == "error"
        assert state.run_history[0].run_id == "lost-1"
        assert state.run_history[0].status == RunStatus.FAILED
        assert state.run_history[0].node_id == "tram-worker-0"
        ctrl.stop()

    def test_mark_active_batch_run_lost_records_failed_manual_run(self):
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp, manager_url="http://manager:8765")
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)
        ctrl.manager.set_status("my-manual", "stopped")

        ctrl._run_batch("my-manual", run_id="lost-manual")

        assert ctrl.mark_active_batch_run_lost(
            "my-manual",
            error="worker-owned batch run disappeared before callback: lost-manual",
            run_id="lost-manual",
        ) is True

        state = ctrl.manager.get("my-manual")
        assert state.status == "error"
        assert state.run_history[0].run_id == "lost-manual"
        assert state.run_history[0].status == RunStatus.FAILED
        ctrl.stop()

    def test_duplicate_worker_callback_after_lost_run_is_ignored(self):
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp, manager_url="http://manager:8765")
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)
        ctrl.manager.set_status("my-manual", "stopped")

        ctrl._run_batch("my-manual", run_id="lost-late")
        assert ctrl.mark_active_batch_run_lost(
            "my-manual",
            error="worker-owned batch run disappeared before callback: lost-late",
            run_id="lost-late",
        ) is True

        state = ctrl.manager.get("my-manual")
        first_result = state.run_history[0]

        ctrl.on_worker_run_complete(
            run_id="lost-late",
            pipeline_name="my-manual",
            worker_id="worker-1",
            status="success",
            records_in=5,
            records_out=5,
        )

        state = ctrl.manager.get("my-manual")
        assert len(state.run_history) == 1
        assert state.run_history[0] == first_result
        assert state.status == "error"
        ctrl.stop()

    def test_run_batch_generates_full_uuid_when_run_id_missing(self):
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp, manager_url="http://manager:8765")
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "scheduled")

        ctrl._run_batch("my-interval")

        generated_run_id = wp.dispatch_with_result.call_args.kwargs["run_id"]
        assert str(uuid.UUID(generated_run_id)) == generated_run_id
        ctrl.stop()

    def test_run_batch_no_healthy_workers_records_failed_run(self):
        wp = self._worker_pool(dispatch_return=None)
        ctrl = _started_controller(worker_pool=wp)
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "scheduled")

        ctrl._run_batch("my-interval")
        state = ctrl.manager.get("my-interval")
        assert state.status == "error"
        assert len(state.run_history) == 1
        assert state.run_history[0].status == RunStatus.FAILED
        assert state.run_history[0].error == "No healthy workers available for dispatch"
        ctrl.stop()

    def test_run_batch_dispatch_failure_records_real_error(self):
        wp = self._worker_pool(dispatch_error="HTTP 503 from http://worker-0:8766")
        ctrl = _started_controller(worker_pool=wp)
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        ctrl.manager.set_status("my-interval", "scheduled")

        ctrl._run_batch("my-interval")
        state = ctrl.manager.get("my-interval")
        assert state.status == "error"
        assert len(state.run_history) == 1
        assert state.run_history[0].status == RunStatus.FAILED
        assert state.run_history[0].error == (
            "Worker dispatch failed: HTTP 503 from http://worker-0:8766"
        )
        ctrl.stop()

    def test_start_stream_dispatches_to_worker(self):
        """Legacy flag-off regression (D.2): count=1 uses dispatch_with_result."""
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp, single_stream_placements=False)
        config = load_pipeline_from_yaml(_STREAM_YAML)
        ctrl.register(config, yaml_text=_STREAM_YAML)

        ctrl._start_stream(config)

        wp.dispatch_with_result.assert_called_once()
        generated_run_id = wp.dispatch_with_result.call_args.kwargs["run_id"]
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
            slots=[
                {"worker_index": 0, "worker_url": "http://worker-0:8766", "run_id_prefix": "pg1-w0", "current_run_id": "pg1-w0", "status": "running", "restart_count": 0},
                {"worker_index": 1, "worker_url": "http://worker-1:8766", "run_id_prefix": "pg1-w1", "current_run_id": "pg1-w1", "status": "running", "restart_count": 0},
            ],
        )
        ctrl = _started_controller(worker_pool=wp)
        config = load_pipeline_from_yaml(_WEBHOOK_STREAM_YAML)
        ctrl.register(config, yaml_text=_WEBHOOK_STREAM_YAML)

        ctrl._start_stream(config)

        wp.multi_dispatch.assert_called_once()
        assert wp.dispatch_with_result.call_count == 0
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
            slots=[
                {"worker_index": 0, "worker_url": "http://worker-0:8766", "run_id_prefix": "pg1-w0", "current_run_id": "pg1-w0", "status": "running", "restart_count": 0},
                {"worker_index": 1, "worker_url": None, "run_id_prefix": "pg1-w1", "current_run_id": None, "status": "stale", "restart_count": 0},
            ],
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
            slots=[
                {"worker_index": 0, "worker_url": "http://worker-0:8766", "run_id_prefix": "pg1-w0", "current_run_id": "pg1-w0", "status": "running", "restart_count": 0},
                {"worker_index": 1, "worker_url": "http://worker-1:8766", "run_id_prefix": "pg1-w1", "current_run_id": "pg1-w1", "status": "running", "restart_count": 0},
            ],
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

    def test_start_stream_count_n_uses_multi_dispatch_and_persists_target(self, tmp_path):
        from tram.persistence.db import TramDB

        wp = self._worker_pool()
        wp.worker_id_for_url.side_effect = lambda url: "w0" if url and url.endswith("0:8766") else "w1"
        wp.multi_dispatch.return_value = MagicMock(
            accepted=["http://worker-0:8766"],
            run_ids=["pg2-w0"],
            rejected=[],
            status="degraded",
            placement_group_id="pg2",
            slots=[
                {"worker_index": 0, "worker_url": "http://worker-0:8766", "run_id_prefix": "pg2-w0", "current_run_id": "pg2-w0", "status": "running", "restart_count": 0},
                {"worker_index": 1, "worker_url": None, "run_id_prefix": "pg2-w1", "current_run_id": None, "status": "stale", "restart_count": 0},
            ],
        )
        db = TramDB(url=f"sqlite:///{tmp_path}/controller-countn.db")
        ctrl = _started_controller(worker_pool=wp, db=db)
        config = load_pipeline_from_yaml(_COUNT_N_STREAM_YAML)
        ctrl.register(config, yaml_text=_COUNT_N_STREAM_YAML)

        ctrl._start_stream(config)

        wp.multi_dispatch.assert_called_once()
        assert ctrl.manager.get("my-count-n-stream").status == "degraded"
        placements = db.get_active_broadcast_placements()
        assert placements[0]["target_count"] == "2"
        assert placements[0]["slots"][1]["current_run_id"] is None
        ctrl.stop()
        db.close()

    def test_start_stream_list_uses_multi_dispatch_and_persists_pinned_workers(self, tmp_path):
        from tram.persistence.db import TramDB

        wp = self._worker_pool()
        wp.multi_dispatch.return_value = MagicMock(
            accepted=["http://worker-0:8766"],
            run_ids=["pg-list-w0"],
            rejected=[],
            status="degraded",
            placement_group_id="pg-list",
            slots=[
                {
                    "worker_index": 0,
                    "worker_url": "http://worker-0:8766",
                    "worker_id": "tram-worker-0",
                    "pinned_worker_id": "tram-worker-0",
                    "run_id_prefix": "pg-list-w0",
                    "current_run_id": "pg-list-w0",
                    "status": "running",
                    "restart_count": 0,
                },
                {
                    "worker_index": 1,
                    "worker_url": None,
                    "worker_id": "tram-worker-1",
                    "pinned_worker_id": "tram-worker-1",
                    "run_id_prefix": "pg-list-w1",
                    "current_run_id": None,
                    "status": "stale",
                    "restart_count": 0,
                },
            ],
        )
        db = TramDB(url=f"sqlite:///{tmp_path}/controller-list.db")
        ctrl = _started_controller(worker_pool=wp, db=db)
        config = load_pipeline_from_yaml(_LIST_STREAM_YAML)
        ctrl.register(config, yaml_text=_LIST_STREAM_YAML)

        ctrl._start_stream(config)

        wp.multi_dispatch.assert_called_once()
        assert ctrl.manager.get("my-list-stream").status == "degraded"
        placements = db.get_active_broadcast_placements()
        assert placements[0]["target_count"] == "2"
        assert placements[0]["slots"][0]["pinned_worker_id"] == "tram-worker-0"
        assert placements[0]["slots"][1]["pinned_worker_id"] == "tram-worker-1"
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
        """Legacy flag-off regression (D.2): no-capacity → status error."""
        wp = self._worker_pool(dispatch_return=None)
        ctrl = _started_controller(worker_pool=wp, single_stream_placements=False)
        config = load_pipeline_from_yaml(_STREAM_YAML)
        ctrl.register(config, yaml_text=_STREAM_YAML)

        ctrl._start_stream(config)
        assert ctrl.manager.get("my-stream").status == "error"
        ctrl.stop()

    def test_start_stream_second_call_is_no_op(self):
        """Legacy flag-off regression (D.2): when stream already dispatched,
        second call is skipped."""
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp, single_stream_placements=False)
        config = load_pipeline_from_yaml(_STREAM_YAML)
        ctrl.register(config, yaml_text=_STREAM_YAML)

        ctrl._start_stream(config)
        ctrl._stream_run_ids["my-stream"] = ["existing-run-id"]
        ctrl._start_stream(config)
        assert wp.dispatch_with_result.call_count == 1  # only called once
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
            worker_id="w0",
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
            worker_id="w0",
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
    def test_update_identical_yaml_is_noop(self):
        ctrl = _started_controller()
        # Pause the scheduler so register()'s immediate first batch run
        # cannot set status "running" and race the assertions below.
        ctrl._scheduler.pause()
        config = load_pipeline_from_yaml(_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_INTERVAL_YAML)
        original_state = ctrl.manager.get("my-interval")

        new_state = ctrl.update("my-interval", _INTERVAL_YAML)
        assert new_state is original_state
        assert ctrl.manager.get("my-interval").status == "scheduled"
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
        db.save_pipeline_version.assert_called_once_with("my-interval", _INTERVAL_YAML)
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
        result = ctrl.start_pipeline("my-interval")
        assert result == "already_running"
        assert ctrl.manager.get("my-interval").status == "running"
        ctrl.stop()

    def test_start_pipeline_disabled_sets_stopped(self):
        yaml = _INTERVAL_YAML + "enabled: false\n"
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(yaml)
        ctrl.manager.register(config, yaml_text=yaml)

        result = ctrl.start_pipeline("my-interval")
        assert result == "disabled"
        assert ctrl.manager.get("my-interval").status == "stopped"
        ctrl.stop()

    def test_start_pipeline_manual_returns_manual(self):
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)

        result = ctrl.start_pipeline("my-manual")
        assert result == "manual"
        assert ctrl.manager.get("my-manual").status == "stopped"
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


# ── D.2 (GH #17): TRAM_STREAM_SINGLE_PLACEMENT flag plumbing (§9.1) ─────────


class TestSingleStreamPlacementFlag:
    def test_flag_defaults_on_from_env(self, monkeypatch):
        monkeypatch.delenv("TRAM_STREAM_SINGLE_PLACEMENT", raising=False)
        assert PipelineController()._single_stream_placements is True

    def test_flag_off_from_env(self, monkeypatch):
        monkeypatch.setenv("TRAM_STREAM_SINGLE_PLACEMENT", "0")
        assert PipelineController()._single_stream_placements is False

    def test_constructor_arg_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("TRAM_STREAM_SINGLE_PLACEMENT", "0")
        assert PipelineController(single_stream_placements=True)._single_stream_placements is True

    def test_app_config_exposes_flag(self, monkeypatch):
        from tram.core.config import AppConfig

        monkeypatch.delenv("TRAM_STREAM_SINGLE_PLACEMENT", raising=False)
        assert AppConfig.from_env().stream_single_placement is True
        monkeypatch.setenv("TRAM_STREAM_SINGLE_PLACEMENT", "0")
        assert AppConfig.from_env().stream_single_placement is False


# ── F.1 §6: broadcast-stream guard for stateful transforms ──────────────────


_STATEFUL_STREAM_YAML = """\
name: my-stateful-stream
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
workers:
  count: 2
transforms:
  - type: window_aggregate
    window_seconds: 900
    operations:
      mean_rate: "avg:rate"
sinks:
  - type: local
    path: /tmp/out
"""

_COUNT_N_INTERVAL_YAML = """\
name: my-count-n-interval
schedule:
  type: interval
  interval_seconds: 60
source:
  type: local
  path: /tmp/in
  file_pattern: "*.noop"
serializer_in:
  type: json
workers:
  count: 2
sinks:
  - type: local
    path: /tmp/out
"""


class TestStatefulBroadcastGuard:
    def _worker_pool(self):
        wp = MagicMock()
        wp.multi_dispatch.return_value = MagicMock(
            accepted=["http://worker-0:8766"],
            run_ids=["pg1-w0"],
            rejected=[],
            status="running",
            placement_group_id="pg1",
            slots=[{
                "worker_index": 0,
                "worker_url": "http://worker-0:8766",
                "run_id_prefix": "pg1-w0",
                "current_run_id": "pg1-w0",
                "status": "running",
                "restart_count": 0,
            }],
        )
        wp.dispatch_with_result.return_value = DispatchOutcome(
            worker_url="http://worker-0:8766", outcome=DISPATCH_ACCEPTED,
        )
        return wp

    def test_start_stream_guard_rejects_broadcast_with_stateful(self):
        """Manager mode + count=2 stream + stateful transform → pipeline error."""
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp, manager_url="http://manager:8765")
        config = load_pipeline_from_yaml(_STATEFUL_STREAM_YAML)
        ctrl.register(config, yaml_text=_STATEFUL_STREAM_YAML)

        assert ctrl.manager.get("my-stateful-stream").status == "error"
        wp.multi_dispatch.assert_not_called()
        wp.dispatch_with_result.assert_not_called()
        ctrl.stop()

    def test_start_stream_guard_standalone_fine(self):
        """Standalone (no worker pool) starts the stateful stream normally."""
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_STATEFUL_STREAM_YAML)
        done = threading.Event()

        def _fake_stream_run(cfg, stop_event, stats=None, config_sha256=""):
            done.wait(timeout=2)

        ctrl.executor = MagicMock()
        ctrl.executor.stream_run.side_effect = _fake_stream_run

        ctrl.register(config, yaml_text=_STATEFUL_STREAM_YAML)

        assert ctrl.manager.get("my-stateful-stream").status == "running"
        assert "my-stateful-stream" in ctrl._stream_threads
        done.set()
        ctrl.stop()

    def test_start_stream_guard_flag_off_inert(self):
        """TRAM_STATEFUL_TRANSFORMS off → the guard is inert; dispatch proceeds."""
        wp = self._worker_pool()
        ctrl = PipelineController(
            worker_pool=wp,
            manager_url="http://manager:8765",
            stateful_transforms=False,
        )
        ctrl.start()
        config = load_pipeline_from_yaml(_STATEFUL_STREAM_YAML)
        ctrl.register(config, yaml_text=_STATEFUL_STREAM_YAML)

        wp.multi_dispatch.assert_called_once()
        assert ctrl.manager.get("my-stateful-stream").status == "running"
        ctrl.stop()

    def test_start_stream_guard_count1_stream_starts(self):
        """count=1 stream + stateful transform is allowed (single slot)."""
        yaml = _STATEFUL_STREAM_YAML.replace("  count: 2", "  count: 1")
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp, manager_url="http://manager:8765")
        config = load_pipeline_from_yaml(yaml)
        ctrl.register(config, yaml_text=yaml)

        assert ctrl.manager.get("my-stateful-stream").status == "running"
        wp.multi_dispatch.assert_called_once()
        ctrl.stop()

    def test_batch_dispatch_ignores_broadcast(self):
        """count=N interval pipelines still dispatch single-slot (pre-existing
        behavior, now asserted — F.1 §3.4/§10)."""
        wp = self._worker_pool()
        ctrl = _started_controller(worker_pool=wp, manager_url="http://manager:8765")
        config = load_pipeline_from_yaml(_COUNT_N_INTERVAL_YAML)
        ctrl.register(config, yaml_text=_COUNT_N_INTERVAL_YAML)
        wp.dispatch_with_result.reset_mock()
        wp.multi_dispatch.reset_mock()
        ctrl.manager.set_status("my-count-n-interval", "scheduled")

        ctrl._run_batch("my-count-n-interval", run_id="r-broadcast")

        wp.dispatch_with_result.assert_called_once()
        wp.multi_dispatch.assert_not_called()
        ctrl.stop()

    def test_trigger_run_flush_reaches_local_executor(self):
        """A standalone flush run forwards the flag to executor.batch_run."""
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)
        ctrl.manager.set_status("my-manual", "stopped")

        result = _make_result("my-manual", RunStatus.SUCCESS)
        ctrl.executor = MagicMock()
        ctrl.executor.batch_run.return_value = result

        trigger = ctrl.trigger_run("my-manual", flush=True)
        assert trigger.disposition == "dispatched"
        time.sleep(0.1)
        ctrl.executor.batch_run.assert_called_once()
        assert ctrl.executor.batch_run.call_args.kwargs["flush"] is True
        ctrl.stop()

    def test_trigger_run_flush_false_by_default(self):
        """trigger_run without flush keeps the executor's default (False)."""
        ctrl = _started_controller()
        config = load_pipeline_from_yaml(_MANUAL_YAML)
        ctrl.register(config, yaml_text=_MANUAL_YAML)
        ctrl.manager.set_status("my-manual", "stopped")

        result = _make_result("my-manual", RunStatus.SUCCESS)
        ctrl.executor = MagicMock()
        ctrl.executor.batch_run.return_value = result

        ctrl.trigger_run("my-manual")
        time.sleep(0.1)
        assert ctrl.executor.batch_run.call_args.kwargs["flush"] is False
        ctrl.stop()


# ── V18-04: durable claim before dispatch ──────────────────────────────────


class TestLedgerClaimBeforeDispatch:
    """V18-04 §1: every batch trigger path inserts the run_intents row and
    claims via ledger.claim_run BEFORE dispatching. A LOST claim (another
    attempt holds the guard) surfaces today's already-running behavior."""

    def _fetch(self, db, sql, params=None):
        with db._engine.connect() as conn:
            rows = conn.execute(text(sql), params or {}).mappings().fetchall()
        return [dict(r) for r in rows]

    def _started_with_db(self, tmp_path, wp=None):
        from tram.persistence.db import TramDB
        db = TramDB(url=f"sqlite:///{tmp_path}/v1804-claim.db")
        ctrl = _started_controller(
            worker_pool=wp or _accepted_worker_pool(),
            db=db,
            manager_url="http://manager:8765",
        )
        return db, ctrl

    def _register_batch(self, ctrl, yaml_text):
        # Register via the manager directly (not ctrl.register) so no interval
        # job is scheduled — a started scheduler fires interval jobs at
        # next_run_time=now and would race the synchronous _run_batch calls.
        config = load_pipeline_from_yaml(yaml_text)
        ctrl.manager.register(config, yaml_text=yaml_text)
        ctrl.manager.set_status(config.name, "scheduled")

    def test_manual_trigger_claims_before_dispatch(self, tmp_path):
        db, ctrl = self._started_with_db(tmp_path)
        self._register_batch(ctrl, _MANUAL_YAML)
        try:
            ctrl._run_batch("my-manual", run_id="r-manual", origin="manual")

            intents = self._fetch(db, "SELECT final_outcome FROM run_intents WHERE run_id = 'r-manual'")
            assert len(intents) == 1
            assert intents[0]["final_outcome"] is None
            guards = self._fetch(db, "SELECT attempt_id, generation FROM execution_guards WHERE guard_key = 'my-manual'")
            assert guards[0]["attempt_id"] == "r-manual-a1"
            assert guards[0]["generation"] == 1
            attempts = self._fetch(db, "SELECT state, dispatch_sent_at FROM execution_attempts WHERE run_id = 'r-manual'")
            assert attempts[0]["state"] == "running"  # 202 acceptance advanced it
            assert attempts[0]["dispatch_sent_at"] is not None

            kwargs = ctrl._worker_pool.dispatch_with_result.call_args.kwargs
            assert kwargs["attempt_id"] == "r-manual-a1"
            assert kwargs["generation"] == 1
        finally:
            ctrl.stop()

    def test_scheduled_run_batch_claims_before_dispatch(self, tmp_path):
        db, ctrl = self._started_with_db(tmp_path)
        self._register_batch(ctrl, _INTERVAL_YAML)
        try:
            ctrl._run_batch("my-interval", run_id="r-sched")

            guards = self._fetch(db, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'my-interval'")
            assert guards[0]["attempt_id"] == "r-sched-a1"
            intents = self._fetch(db, "SELECT origin FROM run_intents WHERE run_id = 'r-sched'")
            assert intents[0]["origin"] == "scheduled"
            kwargs = ctrl._worker_pool.dispatch_with_result.call_args.kwargs
            assert kwargs["attempt_id"] == "r-sched-a1"
        finally:
            ctrl.stop()

    def test_lost_claim_surfaces_already_running_no_dispatch(self, tmp_path):
        db, ctrl = self._started_with_db(tmp_path)
        self._register_batch(ctrl, _INTERVAL_YAML)
        try:
            # Pre-claim the guard with a different run (stale in-memory status /
            # another manager) — the ledger is the authoritative guard.
            with db._engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO run_intents
                        (run_id, pipeline_name, origin, flush, requested_at, expires_at,
                         requested_generation)
                    VALUES ('r-holder', 'my-interval', 'scheduled', 0, :now, NULL, 1)
                """), {"now": datetime.now(UTC).isoformat()})
            holder = claim_run(
                db._engine, guard_key="my-interval", guard_kind="batch",
                pipeline_name="my-interval", run_id="r-holder", generation=1,
            )
            assert holder.status == CLAIMED

            ctrl._worker_pool.dispatch_with_result.reset_mock()
            ctrl._run_batch("my-interval", run_id="r-loser", origin="manual")

            # no dispatch, no attempt row for the loser
            ctrl._worker_pool.dispatch_with_result.assert_not_called()
            attempts = self._fetch(db, "SELECT run_id FROM execution_attempts WHERE run_id = 'r-loser'")
            assert attempts == []
            # the loser's run_id resolves as FAILED (manual origin — GH #47)
            result = ctrl.manager.get_run("r-loser")
            assert result is not None
            assert result.status == RunStatus.FAILED
            assert "guard" in (result.error or "")
            # the winner's guard is untouched
            guards = self._fetch(db, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'my-interval'")
            assert guards[0]["attempt_id"] == "r-holder-a1"
        finally:
            ctrl.stop()

    def test_queued_drain_claim_acquires_guard(self, tmp_path):
        """A queued request holds a reservation, not the guard; the drain claim
        converts it (frozen §2) and registers the attempt for the dispatch."""
        db, ctrl = self._started_with_db(tmp_path)
        self._register_batch(ctrl, _MANUAL_YAML)
        try:
            ctrl._worker_pool.healthy_workers.return_value = []
            triggered = ctrl.trigger_run("my-manual")
            assert triggered.disposition == "queued"

            # reservation only — no guard yet
            guards = self._fetch(db, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'my-manual'")
            assert guards == []

            claimed = ctrl.claim_queued_run(triggered.run_id)
            assert claimed is not None
            assert claimed["attempt_id"] == f"{triggered.run_id}-a1"

            guards = self._fetch(db, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'my-manual'")
            assert guards[0]["attempt_id"] == f"{triggered.run_id}-a1"
            attempts = self._fetch(db, "SELECT state FROM execution_attempts WHERE run_id = :r", {"r": triggered.run_id})
            assert attempts[0]["state"] == "dispatching"
            # the controller registered the attempt so the drain dispatch
            # (which carries only the run_id) attaches the identity
            ctrl._worker_pool.register_attempt.assert_called_once_with(
                triggered.run_id, f"{triggered.run_id}-a1", 1, ""
            )
        finally:
            ctrl.stop()

    def test_no_capacity_enqueue_reenters_as_new_attempt(self, tmp_path):
        """Frozen 503 rule: the attempt is terminal with a capacity reason, the
        intent stays unresolved, and the queued re-entry dispatches as a NEW
        attempt (N+1 under the same run_id)."""
        db, ctrl = self._started_with_db(tmp_path)
        wp = ctrl._worker_pool
        self._register_batch(ctrl, _INTERVAL_YAML)
        try:
            wp.dispatch_with_result.return_value = DispatchOutcome(
                worker_url=None, outcome=DISPATCH_NO_CAPACITY,
            )
            ctrl._run_batch("my-interval", run_id="r-cap", origin="manual")

            attempts = self._fetch(db, "SELECT state FROM execution_attempts WHERE run_id = 'r-cap'")
            assert attempts[0]["state"] == "terminal"
            guards = self._fetch(db, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'my-interval'")
            assert guards[0]["attempt_id"] is None
            intents = self._fetch(db, "SELECT final_outcome FROM run_intents WHERE run_id = 'r-cap'")
            assert intents[0]["final_outcome"] is None
            queued = db.get_active_queued_runs()
            assert len(queued) == 1 and queued[0]["run_id"] == "r-cap"

            wp.dispatch_with_result.return_value = DispatchOutcome(
                worker_url="http://worker-0:8766", outcome=DISPATCH_ACCEPTED,
            )
            claimed = ctrl.claim_queued_run("r-cap")
            assert claimed["attempt_id"] == "r-cap-a2"
            attempts = self._fetch(
                db, "SELECT attempt_id, state FROM execution_attempts WHERE run_id = 'r-cap' ORDER BY ordinal"
            )
            assert [a["attempt_id"] for a in attempts] == ["r-cap-a1", "r-cap-a2"]
            assert attempts[1]["state"] == "dispatching"
        finally:
            ctrl.stop()


# ── V18-04: identity-checked run-complete ───────────────────────────────────


class TestAttemptRunComplete:
    """V18-04 §3: the handler resolves conditionally on exact attempt_id +
    generation — intent resolution idempotent for the winner, attempt →
    terminal transition fenced, guard released by identity, ledger committed
    before the response. Legacy callbacks (no attempt_id) keep today's path."""

    def _fetch(self, db, sql, params=None):
        with db._engine.connect() as conn:
            rows = conn.execute(text(sql), params or {}).mappings().fetchall()
        return [dict(r) for r in rows]

    def _started_with_db(self, tmp_path, wp=None):
        from tram.persistence.db import TramDB
        db = TramDB(url=f"sqlite:///{tmp_path}/v1804-complete.db")
        ctrl = _started_controller(
            worker_pool=wp or _accepted_worker_pool(),
            db=db,
            manager_url="http://manager:8765",
        )
        return db, ctrl

    def _register_batch(self, ctrl, yaml_text):
        # Register via the manager directly (not ctrl.register) so no interval
        # job is scheduled — a started scheduler fires interval jobs at
        # next_run_time=now and would race the synchronous _run_batch calls.
        config = load_pipeline_from_yaml(yaml_text)
        ctrl.manager.register(config, yaml_text=yaml_text)
        ctrl.manager.set_status(config.name, "scheduled")

    def test_identity_match_resolves_intent_and_releases_guard(self, tmp_path):
        db, ctrl = self._started_with_db(tmp_path)
        self._register_batch(ctrl, _INTERVAL_YAML)
        try:
            ctrl._run_batch("my-interval", run_id="r-win")
            attempts = self._fetch(db, "SELECT state FROM execution_attempts WHERE run_id = 'r-win'")
            assert attempts[0]["state"] == "running"  # 202 acceptance advanced it

            resp = ctrl.on_attempt_run_complete(
                attempt_id="r-win-a1", generation=1, run_id="r-win",
                pipeline_name="my-interval", worker_id="w0", status="success",
                records_in=5, records_out=5,
            )
            assert resp == {"ok": True}

            attempts = self._fetch(db, "SELECT state, finished_at FROM execution_attempts WHERE run_id = 'r-win'")
            assert attempts[0]["state"] == "terminal"
            assert attempts[0]["finished_at"] is not None
            guards = self._fetch(db, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'my-interval'")
            assert guards[0]["attempt_id"] is None
            intents = self._fetch(db, "SELECT final_outcome, final_attempt_id FROM run_intents WHERE run_id = 'r-win'")
            assert intents == [{"final_outcome": "success", "final_attempt_id": "r-win-a1"}]
            # the run-history row landed
            result = ctrl.manager.get_run("r-win")
            assert result is not None
            assert result.status == RunStatus.SUCCESS
        finally:
            ctrl.stop()

    def test_generation_mismatch_ignored_with_diagnostics(self, tmp_path):
        db, ctrl = self._started_with_db(tmp_path)
        self._register_batch(ctrl, _INTERVAL_YAML)
        try:
            ctrl._run_batch("my-interval", run_id="r-gen")
            resp = ctrl.on_attempt_run_complete(
                attempt_id="r-gen-a1", generation=99, run_id="r-gen",
                pipeline_name="my-interval", worker_id="w0", status="success",
                records_in=1, records_out=1,
            )
            assert resp == {"ok": True, "ignored": "identity_mismatch"}
            # no ledger change, no history row, guard still held by the attempt
            attempts = self._fetch(db, "SELECT state FROM execution_attempts WHERE run_id = 'r-gen'")
            assert attempts[0]["state"] == "running"
            guards = self._fetch(db, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'my-interval'")
            assert guards[0]["attempt_id"] == "r-gen-a1"
            assert ctrl.manager.get_run("r-gen") is None
        finally:
            ctrl.stop()

    def test_wrong_attempt_id_ignored(self, tmp_path):
        db, ctrl = self._started_with_db(tmp_path)
        self._register_batch(ctrl, _INTERVAL_YAML)
        try:
            ctrl._run_batch("my-interval", run_id="r-one")
            resp = ctrl.on_attempt_run_complete(
                attempt_id="r-two-a1", generation=1, run_id="r-one",
                pipeline_name="my-interval", worker_id="w0", status="success",
                records_in=1, records_out=1,
            )
            assert resp == {"ok": True, "ignored": "unknown_attempt"}
            assert ctrl.manager.get_run("r-one") is None
            guards = self._fetch(db, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'my-interval'")
            assert guards[0]["attempt_id"] == "r-one-a1"
        finally:
            ctrl.stop()

    def test_duplicate_completion_is_idempotent(self, tmp_path):
        db, ctrl = self._started_with_db(tmp_path)
        self._register_batch(ctrl, _INTERVAL_YAML)
        try:
            ctrl._run_batch("my-interval", run_id="r-dup")
            first = ctrl.on_attempt_run_complete(
                attempt_id="r-dup-a1", generation=1, run_id="r-dup",
                pipeline_name="my-interval", worker_id="w0", status="success",
                records_in=3, records_out=3,
            )
            assert first == {"ok": True}
            second = ctrl.on_attempt_run_complete(
                attempt_id="r-dup-a1", generation=1, run_id="r-dup",
                pipeline_name="my-interval", worker_id="w0", status="success",
                records_in=3, records_out=3,
            )
            assert second == {"ok": True}
            # the winner's intent row is untouched by the duplicate
            intents = self._fetch(db, "SELECT final_outcome, final_attempt_id FROM run_intents WHERE run_id = 'r-dup'")
            assert intents == [{"final_outcome": "success", "final_attempt_id": "r-dup-a1"}]
            matches = [r for r in ctrl.manager.get("my-interval").run_history if r.run_id == "r-dup"]
            assert len(matches) == 1
        finally:
            ctrl.stop()

    def test_late_callback_cannot_clobber_newer_guard(self, tmp_path):
        """A late callback for a retired attempt records diagnostics only and
        cannot touch the newer run's guard, lease, status, or generation (the
        name-keyed _active_batch_runs.pop is replaced)."""
        db, ctrl = self._started_with_db(tmp_path)
        self._register_batch(ctrl, _INTERVAL_YAML)
        try:
            # run 1 dispatched and completed (guard released)
            ctrl._run_batch("my-interval", run_id="r-old")
            ctrl.on_attempt_run_complete(
                attempt_id="r-old-a1", generation=1, run_id="r-old",
                pipeline_name="my-interval", worker_id="w0", status="success",
                records_in=1, records_out=1,
            )
            # run 2 dispatched and still active
            ctrl._worker_pool.dispatch_with_result.reset_mock()
            ctrl._run_batch("my-interval", run_id="r-new")
            assert ctrl.manager.get("my-interval").status == "running"
            assert len(ctrl.get_active_batch_runs()) == 1
            assert ctrl.get_active_batch_runs()[0]["run_id"] == "r-new"

            # late duplicate callback for the retired attempt
            ctrl.on_attempt_run_complete(
                attempt_id="r-old-a1", generation=1, run_id="r-old",
                pipeline_name="my-interval", worker_id="w0", status="success",
                records_in=1, records_out=1,
            )

            # the newer run's guard, lease, and status are untouched
            guards = self._fetch(db, "SELECT attempt_id, run_id FROM execution_guards WHERE guard_key = 'my-interval'")
            assert guards[0] == {"attempt_id": "r-new-a1", "run_id": "r-new"}
            assert len(ctrl.get_active_batch_runs()) == 1
            assert ctrl.get_active_batch_runs()[0]["run_id"] == "r-new"
            assert ctrl.manager.get("my-interval").status == "running"
            # the old run still has exactly one history row
            matches = [r for r in ctrl.manager.get("my-interval").run_history if r.run_id == "r-old"]
            assert len(matches) == 1
        finally:
            ctrl.stop()

    def test_legacy_completion_resolves_ledger_best_effort(self, tmp_path):
        """A v1.7 worker's legacy-shaped completion (no attempt_id) still
        resolves the ledger by run identity — the guard can never leak."""
        db, ctrl = self._started_with_db(tmp_path)
        self._register_batch(ctrl, _INTERVAL_YAML)
        try:
            ctrl._run_batch("my-interval", run_id="r-legacy")
            ctrl.on_worker_run_complete(
                run_id="r-legacy", pipeline_name="my-interval",
                worker_id="w0", status="success", records_in=2, records_out=2,
            )
            attempts = self._fetch(db, "SELECT state FROM execution_attempts WHERE run_id = 'r-legacy'")
            assert attempts[0]["state"] == "terminal"
            guards = self._fetch(db, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'my-interval'")
            assert guards[0]["attempt_id"] is None
            intents = self._fetch(db, "SELECT final_outcome FROM run_intents WHERE run_id = 'r-legacy'")
            assert intents[0]["final_outcome"] == "success"
        finally:
            ctrl.stop()

    def test_completion_before_dispatch_commit_is_tolerated(self, tmp_path):
        """Fast-run race: a completion arriving while the attempt is still
        'claimed' (before the dispatch commit) still terminalls it — the
        frozen running→terminal fence is extended to the pre-acceptance
        states; the identity/generation fence is unchanged."""
        db, ctrl = self._started_with_db(tmp_path)
        self._register_batch(ctrl, _INTERVAL_YAML)
        try:
            with db._engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO run_intents
                        (run_id, pipeline_name, origin, flush, requested_at, expires_at,
                         requested_generation)
                    VALUES ('r-claimed', 'my-interval', 'manual', 0, :now, NULL, 1)
                """), {"now": datetime.now(UTC).isoformat()})
            claim = claim_run(
                db._engine, guard_key="my-interval", guard_kind="batch",
                pipeline_name="my-interval", run_id="r-claimed", generation=1,
            )
            assert claim.status == CLAIMED

            resp = ctrl.on_attempt_run_complete(
                attempt_id="r-claimed-a1", generation=1, run_id="r-claimed",
                pipeline_name="my-interval", worker_id="w0", status="success",
                records_in=0, records_out=0,
            )
            assert resp == {"ok": True}
            attempts = self._fetch(db, "SELECT state FROM execution_attempts WHERE run_id = 'r-claimed'")
            assert attempts[0]["state"] == "terminal"
        finally:
            ctrl.stop()


# ── V18-04: boot adoption ───────────────────────────────────────────────────


class TestBootAdoption:
    """V18-04 §2 (frozen §2–3): at controller boot, before any scheduler fires,
    every non-terminal ledger attempt is resolved — claimed-unsent aborts
    locally (never unknown), dispatching/running resolve against the owning
    worker's journal, and unreachable/no-evidence attempts go 'unknown' with
    the guard RETAINED (operator force-release is a later lane)."""

    def _fetch(self, db, sql, params=None):
        with db._engine.connect() as conn:
            rows = conn.execute(text(sql), params or {}).mappings().fetchall()
        return [dict(r) for r in rows]

    def _db(self, tmp_path, name="adopt.db"):
        from tram.persistence.db import TramDB
        return TramDB(url=f"sqlite:///{tmp_path}/{name}")

    def _seed(
        self,
        db,
        *,
        run_id,
        pipeline_name="my-interval",
        state="dispatching",
        generation=1,
        ordinal=1,
        worker_id="w0",
        dispatch_sent_at=None,
    ):
        """Seed run_intents + execution_guards + execution_attempts (claimed)."""
        attempt_id = f"{run_id}-a{ordinal}"
        now = datetime.now(UTC).isoformat()
        with db._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO run_intents
                    (run_id, pipeline_name, origin, flush, requested_at, expires_at,
                     requested_generation, yaml_snapshot, schedule_type)
                VALUES (:r, :p, 'scheduled', 0, :now, NULL, :gen, 'yaml', 'interval')
            """), {"r": run_id, "p": pipeline_name, "now": now, "gen": generation})
            conn.execute(text("""
                INSERT INTO execution_guards (guard_key, guard_kind, run_id, attempt_id, generation, acquired_at)
                VALUES (:p, 'batch', :r, :a, :gen, :now)
            """), {"p": pipeline_name, "r": run_id, "a": attempt_id, "gen": generation, "now": now})
            conn.execute(text("""
                INSERT INTO execution_attempts
                    (attempt_id, run_id, pipeline_name, ordinal, generation, slot_id,
                     fence_token, state, dispatch_sent_at, worker_id)
                VALUES (:a, :r, :p, :ordinal, :gen, '', 'ft', :state, :dsp, :wid)
            """), {
                "a": attempt_id, "r": run_id, "p": pipeline_name, "ordinal": ordinal,
                "gen": generation, "state": state, "dsp": dispatch_sent_at, "wid": worker_id,
            })
        return attempt_id

    def _boot_controller(self, tmp_path, wp, name="adopt.db"):
        db = self._db(tmp_path, name)
        ctrl = _make_controller(db=db, worker_pool=wp, manager_url="http://manager:8765")
        return db, ctrl

    def test_claimed_unsent_aborts_locally_never_unknown(self, tmp_path):
        wp = MagicMock()
        db, ctrl = self._boot_controller(tmp_path, wp)
        self._seed(db, run_id="r-claim", state="claimed", dispatch_sent_at=None)
        try:
            ctrl.start()
            attempts = self._fetch(
                db, "SELECT state, cancel_reason FROM execution_attempts WHERE run_id = 'r-claim'"
            )
            assert attempts[0]["state"] == "terminal"
            assert attempts[0]["cancel_reason"] == "manager_lost_before_dispatch"
            intents = self._fetch(db, "SELECT final_outcome FROM run_intents WHERE run_id = 'r-claim'")
            assert intents[0]["final_outcome"] == "aborted"
            guards = self._fetch(db, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'my-interval'")
            assert guards[0]["attempt_id"] is None  # guard released
            # local resolution — never a worker query, never unknown
            wp.query_attempt.assert_not_called()
        finally:
            ctrl.stop()

    def test_dispatching_journal_completed_resolves(self, tmp_path):
        wp = MagicMock()
        wp.url_for_worker_id.return_value = "http://worker-0:8766"
        wp.worker_urls.return_value = ["http://worker-0:8766"]
        wp.query_attempt.return_value = {
            "kind": "completion",
            "result_json": {"status": "success", "records_in": 5},
        }
        db, ctrl = self._boot_controller(tmp_path, wp)
        self._seed(db, run_id="r-done", state="dispatching", worker_id="w0")
        try:
            ctrl.start()
            wp.query_attempt.assert_called_once_with("http://worker-0:8766", "r-done-a1")
            attempts = self._fetch(db, "SELECT state FROM execution_attempts WHERE run_id = 'r-done'")
            assert attempts[0]["state"] == "terminal"
            intents = self._fetch(
                db, "SELECT final_outcome, final_attempt_id FROM run_intents WHERE run_id = 'r-done'"
            )
            assert intents == [{"final_outcome": "success", "final_attempt_id": "r-done-a1"}]
            guards = self._fetch(db, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'my-interval'")
            assert guards[0]["attempt_id"] is None
        finally:
            ctrl.stop()

    def test_dispatching_journal_interrupted_terminates(self, tmp_path):
        wp = MagicMock()
        wp.url_for_worker_id.return_value = "http://worker-0:8766"
        wp.worker_urls.return_value = ["http://worker-0:8766"]
        wp.query_attempt.return_value = {"kind": "interrupted", "run_id": "r-int"}
        db, ctrl = self._boot_controller(tmp_path, wp)
        self._seed(db, run_id="r-int", state="dispatching", worker_id="w0")
        try:
            ctrl.start()
            attempts = self._fetch(
                db, "SELECT state, cancel_reason FROM execution_attempts WHERE run_id = 'r-int'"
            )
            assert attempts[0]["state"] == "terminal"
            assert attempts[0]["cancel_reason"] == "boot_adoption_interrupted"
            intents = self._fetch(db, "SELECT final_outcome FROM run_intents WHERE run_id = 'r-int'")
            assert intents[0]["final_outcome"] == "aborted"
            guards = self._fetch(db, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'my-interval'")
            assert guards[0]["attempt_id"] is None
        finally:
            ctrl.stop()

    def test_dispatching_tombstone_terminates(self, tmp_path):
        wp = MagicMock()
        wp.url_for_worker_id.return_value = "http://worker-0:8766"
        wp.worker_urls.return_value = ["http://worker-0:8766"]
        wp.query_attempt.return_value = {"kind": "tombstone", "reason": "revoked"}
        db, ctrl = self._boot_controller(tmp_path, wp)
        self._seed(db, run_id="r-tomb", state="running", worker_id="w0")
        try:
            ctrl.start()
            attempts = self._fetch(
                db, "SELECT state, cancel_reason FROM execution_attempts WHERE run_id = 'r-tomb'"
            )
            assert attempts[0]["state"] == "terminal"
            assert attempts[0]["cancel_reason"] == "boot_adoption_tombstone"
            intents = self._fetch(db, "SELECT final_outcome FROM run_intents WHERE run_id = 'r-tomb'")
            assert intents[0]["final_outcome"] == "aborted"
        finally:
            ctrl.stop()

    def test_unreachable_worker_no_row_or_v17_endpoint_unknown_guard_retained(self, tmp_path):
        """Unreachable worker / no journal row / v1.7 worker without the query
        endpoint → 'unknown'; the guard is RETAINED (never auto-cleared — the
        operator force-release lane is later)."""
        wp = MagicMock()
        wp.url_for_worker_id.return_value = "http://worker-0:8766"
        wp.worker_urls.return_value = ["http://worker-0:8766"]
        wp.query_attempt.return_value = None  # 404 / transport error / no endpoint
        db, ctrl = self._boot_controller(tmp_path, wp)
        self._seed(db, run_id="r-unk", state="dispatching", worker_id="w0")
        try:
            ctrl.start()
            attempts = self._fetch(
                db, "SELECT state, uncertainty_reason FROM execution_attempts WHERE run_id = 'r-unk'"
            )
            assert attempts[0]["state"] == "unknown"
            assert attempts[0]["uncertainty_reason"] == "boot_adoption_no_journal_evidence"
            guards = self._fetch(db, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'my-interval'")
            assert guards[0]["attempt_id"] == "r-unk-a1"  # guard retained
            intents = self._fetch(db, "SELECT final_outcome FROM run_intents WHERE run_id = 'r-unk'")
            assert intents[0]["final_outcome"] is None  # unresolved
        finally:
            ctrl.stop()

    def test_missing_worker_id_falls_back_to_probing_all_workers(self, tmp_path):
        """Pre-upgrade rows carry no worker_id — adoption fans out to every
        configured worker; no evidence → unknown."""
        wp = MagicMock()
        wp.url_for_worker_id.return_value = None
        wp.worker_urls.return_value = ["http://w0:8766", "http://w1:8766"]
        wp.query_attempt.return_value = None
        db, ctrl = self._boot_controller(tmp_path, wp)
        self._seed(db, run_id="r-orphan", state="running", worker_id=None)
        try:
            ctrl.start()
            assert wp.query_attempt.call_count == 2  # both workers probed
            attempts = self._fetch(db, "SELECT state FROM execution_attempts WHERE run_id = 'r-orphan'")
            assert attempts[0]["state"] == "unknown"
        finally:
            ctrl.stop()

    def test_active_journal_row_adopts_lease(self, tmp_path):
        """Journal reports the attempt still active on a reachable worker — the
        lease is adopted (reconciler probes it), the guard stays held, and the
        intent stays unresolved until the run-complete lands."""
        wp = MagicMock()
        wp.url_for_worker_id.return_value = "http://worker-0:8766"
        wp.worker_urls.return_value = ["http://worker-0:8766"]
        wp.query_attempt.return_value = {"kind": "active"}
        db, ctrl = self._boot_controller(tmp_path, wp)
        self._seed(db, run_id="r-live", state="running", worker_id="w0")
        try:
            ctrl.start()
            attempts = self._fetch(db, "SELECT state FROM execution_attempts WHERE run_id = 'r-live'")
            assert attempts[0]["state"] == "running"  # untouched — still live
            lease = ctrl._active_batch_runs.get("my-interval")
            assert lease is not None and lease.run_id == "r-live"
            assert lease.attempt_id == "r-live-a1"
            guards = self._fetch(db, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'my-interval'")
            assert guards[0]["attempt_id"] == "r-live-a1"  # still held
        finally:
            ctrl.stop()

    def test_adoption_terminates_stuck_dispatching_queued_row(self, tmp_path):
        """A queued_runs row stuck at 'dispatching' for a run whose ledger
        attempt adoption resolves is terminal-cancelled with the reason."""
        wp = MagicMock()
        wp.url_for_worker_id.return_value = "http://worker-0:8766"
        wp.worker_urls.return_value = ["http://worker-0:8766"]
        wp.query_attempt.return_value = {
            "kind": "completion", "result_json": {"status": "success"},
        }
        db, ctrl = self._boot_controller(tmp_path, wp)
        self._seed(db, run_id="r-q", state="dispatching", worker_id="w0")
        with db._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO queued_runs (run_id, pipeline_name, yaml_snapshot, status,
                                         requested_at, expires_at)
                VALUES ('r-q', 'my-interval', 'yaml', 'dispatching', :now, :now)
            """), {"now": datetime.now(UTC).isoformat()})
        try:
            ctrl.start()
            rows = self._fetch(
                db, "SELECT status, terminal_reason FROM queued_runs WHERE run_id = 'r-q'"
            )
            assert rows[0]["status"] == "cancelled"
            assert rows[0]["terminal_reason"] == "boot_adoption_completed"
        finally:
            ctrl.stop()

    def test_adoption_completes_before_schedulers_fire(self, tmp_path, monkeypatch):
        """Order pin: boot adoption resolves before APScheduler starts — the
        'before any scheduler fires' requirement is structural (_boot_load runs
        adoption under the lock, then the scheduler starts)."""
        from apscheduler.schedulers.background import BackgroundScheduler

        wp = MagicMock()
        wp.url_for_worker_id.return_value = "http://worker-0:8766"
        wp.worker_urls.return_value = ["http://worker-0:8766"]
        wp.query_attempt.return_value = {
            "kind": "completion", "result_json": {"status": "success"},
        }
        db, ctrl = self._boot_controller(tmp_path, wp)
        self._seed(db, run_id="r-pin", state="dispatching", worker_id="w0")
        order: list[str] = []
        orig_resolve = PipelineController._resolve_non_terminal_attempts_at_boot

        def _resolve(self):
            order.append("adoption")
            return orig_resolve(self)

        orig_sched_start = BackgroundScheduler.start

        def _sched_start(self, *args, **kwargs):
            order.append("scheduler_start")
            return orig_sched_start(self, *args, **kwargs)

        monkeypatch.setattr(
            PipelineController, "_resolve_non_terminal_attempts_at_boot", _resolve
        )
        monkeypatch.setattr(BackgroundScheduler, "start", _sched_start)
        try:
            ctrl.start()
            assert order == ["adoption", "scheduler_start"]
        finally:
            ctrl.stop()


# ── V18-04: queue terminal cancellation (R16) ───────────────────────────────


class TestQueueTerminalCancellation:
    """R16: stop/restart/update/delete terminal-cancel a pipeline's queued
    (pending) run rows at action time with the recorded reason — instead of
    waiting for TTL expiry — and the E.2 drain skips them."""

    def _fetch(self, db, sql, params=None):
        with db._engine.connect() as conn:
            rows = conn.execute(text(sql), params or {}).mappings().fetchall()
        return [dict(r) for r in rows]

    def _started(self, tmp_path, name="queue-cancel.db"):
        from tram.persistence.db import TramDB
        db = TramDB(url=f"sqlite:///{tmp_path}/{name}")
        wp = MagicMock()
        wp.healthy_workers.return_value = []
        ctrl = _make_controller(db=db, worker_pool=wp, manager_url="http://manager:8765")
        ctrl.manager.register(load_pipeline_from_yaml(_MANUAL_YAML), yaml_text=_MANUAL_YAML)
        return db, ctrl

    def test_stop_terminal_cancels_queued_with_reason(self, tmp_path):
        db, ctrl = self._started(tmp_path)
        try:
            triggered = ctrl.trigger_run("my-manual")
            assert triggered.disposition == "queued"
            ctrl.stop_pipeline("my-manual")
            rows = self._fetch(
                db, "SELECT status, terminal_reason FROM queued_runs WHERE run_id = :r",
                {"r": triggered.run_id},
            )
            assert rows[0]["status"] == "cancelled"
            assert rows[0]["terminal_reason"] == "pipeline_stopped"
            assert ctrl.drainable_queued_runs() == []  # drain skips cancelled rows
            intents = self._fetch(
                db, "SELECT final_outcome FROM run_intents WHERE run_id = :r",
                {"r": triggered.run_id},
            )
            assert intents[0]["final_outcome"] == "aborted"
        finally:
            ctrl.stop()

    def test_delete_terminal_cancels_queued_with_reason(self, tmp_path):
        db, ctrl = self._started(tmp_path)
        try:
            triggered = ctrl.trigger_run("my-manual")
            assert triggered.disposition == "queued"
            ctrl.delete("my-manual")
            rows = self._fetch(
                db, "SELECT status, terminal_reason FROM queued_runs WHERE run_id = :r",
                {"r": triggered.run_id},
            )
            assert rows[0]["status"] == "cancelled"
            assert rows[0]["terminal_reason"] == "pipeline_deleted"
            assert ctrl.drainable_queued_runs() == []
        finally:
            ctrl.stop()

    def test_update_terminal_cancels_queued_with_reason(self, tmp_path):
        db, ctrl = self._started(tmp_path)
        try:
            triggered = ctrl.trigger_run("my-manual")
            assert triggered.disposition == "queued"
            v2 = _MANUAL_YAML + "description: updated-v2\n"
            ctrl.update("my-manual", v2)
            rows = self._fetch(
                db, "SELECT status, terminal_reason FROM queued_runs WHERE run_id = :r",
                {"r": triggered.run_id},
            )
            assert rows[0]["status"] == "cancelled"
            assert rows[0]["terminal_reason"] == "pipeline_updated"
            assert ctrl.drainable_queued_runs() == []
        finally:
            ctrl.stop()

    def test_restart_terminal_cancels_queued_with_reason(self, tmp_path):
        db, ctrl = self._started(tmp_path)
        try:
            triggered = ctrl.trigger_run("my-manual")
            assert triggered.disposition == "queued"
            ctrl.restart_pipeline("my-manual")
            rows = self._fetch(
                db, "SELECT status, terminal_reason FROM queued_runs WHERE run_id = :r",
                {"r": triggered.run_id},
            )
            assert rows[0]["status"] == "cancelled"
            assert rows[0]["terminal_reason"] == "pipeline_restarted"
            assert ctrl.drainable_queued_runs() == []
        finally:
            ctrl.stop()

    def test_delete_prunes_orphaned_unresolved_intents(self, tmp_path):
        """Task 5: an unresolved intent whose run has only a terminal attempt
        (no queued row) is resolved at delete — no orphaned rows linger."""
        from tram.persistence.db import TramDB
        db = TramDB(url=f"sqlite:///{tmp_path}/prune.db")
        wp = MagicMock()
        wp.healthy_workers.return_value = []
        ctrl = _make_controller(db=db, worker_pool=wp, manager_url="http://manager:8765")
        ctrl.manager.register(load_pipeline_from_yaml(_MANUAL_YAML), yaml_text=_MANUAL_YAML)
        try:
            now = datetime.now(UTC).isoformat()
            with db._engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO run_intents
                        (run_id, pipeline_name, origin, flush, requested_at, expires_at,
                         requested_generation, yaml_snapshot, schedule_type)
                    VALUES ('r-orphan', 'my-manual', 'manual', 0, :now, NULL, 1, 'yaml', 'manual')
                """), {"now": now})
                conn.execute(text("""
                    INSERT INTO execution_attempts
                        (attempt_id, run_id, pipeline_name, ordinal, generation, slot_id,
                         fence_token, state)
                    VALUES ('r-orphan-a1', 'r-orphan', 'my-manual', 1, 1, '', 'ft', 'terminal')
                """))
            ctrl.delete("my-manual")
            intents = self._fetch(db, "SELECT final_outcome FROM run_intents WHERE run_id = 'r-orphan'")
            assert intents[0]["final_outcome"] == "aborted"
        finally:
            ctrl.stop()


# ── V18-04: lifecycle_operations wiring ─────────────────────────────────────


class TestLifecycleOperationsWiring:
    """V18-04: stop/restart/update/delete and the boot-adoption resolution
    record rows in the frozen lifecycle_operations table (pending → complete
    with detail). The 202+operation_id API shape is V18-09 — only the table
    writes + internal queries live here."""

    def _fetch(self, db, sql, params=None):
        with db._engine.connect() as conn:
            rows = conn.execute(text(sql), params or {}).mappings().fetchall()
        return [dict(r) for r in rows]

    def _started(self, tmp_path, name="lifecycle.db"):
        from tram.persistence.db import TramDB
        db = TramDB(url=f"sqlite:///{tmp_path}/{name}")
        wp = MagicMock()
        wp.healthy_workers.return_value = []
        ctrl = _make_controller(db=db, worker_pool=wp, manager_url="http://manager:8765")
        ctrl.manager.register(load_pipeline_from_yaml(_MANUAL_YAML), yaml_text=_MANUAL_YAML)
        return db, ctrl

    def _assert_single_complete(self, db, pipeline_name, op_kind):
        rows = self._fetch(
            db,
            "SELECT op_kind, state, detail, attempt_id FROM lifecycle_operations "
            "WHERE pipeline_name = :p",
            {"p": pipeline_name},
        )
        assert len(rows) == 1
        assert rows[0]["op_kind"] == op_kind
        assert rows[0]["state"] == "complete"
        assert rows[0]["detail"]
        return rows[0]

    def test_stop_records_lifecycle_operation(self, tmp_path):
        db, ctrl = self._started(tmp_path)
        try:
            ctrl.stop_pipeline("my-manual")
            self._assert_single_complete(db, "my-manual", "stop")
        finally:
            ctrl.stop()

    def test_delete_records_lifecycle_operation(self, tmp_path):
        db, ctrl = self._started(tmp_path)
        try:
            ctrl.delete("my-manual")
            self._assert_single_complete(db, "my-manual", "delete")
        finally:
            ctrl.stop()

    def test_update_records_lifecycle_operation(self, tmp_path):
        db, ctrl = self._started(tmp_path)
        try:
            v2 = _MANUAL_YAML + "description: updated-v2\n"
            ctrl.update("my-manual", v2)
            self._assert_single_complete(db, "my-manual", "update")
        finally:
            ctrl.stop()

    def test_restart_records_lifecycle_operation(self, tmp_path):
        db, ctrl = self._started(tmp_path)
        try:
            ctrl.restart_pipeline("my-manual")
            self._assert_single_complete(db, "my-manual", "restart")
        finally:
            ctrl.stop()

    def test_boot_adoption_records_lifecycle_operation(self, tmp_path):
        from tram.persistence.db import TramDB
        db = TramDB(url=f"sqlite:///{tmp_path}/adopt-lifecycle.db")
        wp = MagicMock()
        wp.url_for_worker_id.return_value = "http://worker-0:8766"
        wp.worker_urls.return_value = ["http://worker-0:8766"]
        wp.query_attempt.return_value = None
        ctrl = _make_controller(db=db, worker_pool=wp, manager_url="http://manager:8765")
        now = datetime.now(UTC).isoformat()
        with db._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO run_intents
                    (run_id, pipeline_name, origin, flush, requested_at, expires_at,
                     requested_generation, yaml_snapshot, schedule_type)
                VALUES ('r-a', 'my-manual', 'manual', 0, :now, NULL, 1, 'yaml', 'manual')
            """), {"now": now})
            conn.execute(text("""
                INSERT INTO execution_guards (guard_key, guard_kind, run_id, attempt_id, generation, acquired_at)
                VALUES ('my-manual', 'batch', 'r-a', 'r-a-a1', 1, :now)
            """), {"now": now})
            conn.execute(text("""
                INSERT INTO execution_attempts
                    (attempt_id, run_id, pipeline_name, ordinal, generation, slot_id,
                     fence_token, state, dispatch_sent_at, worker_id)
                VALUES ('r-a-a1', 'r-a', 'my-manual', 1, 1, '', 'ft', 'dispatching', :now, 'w0')
            """), {"now": now})
        try:
            ctrl.start()
            rows = self._fetch(
                db,
                "SELECT op_kind, state, detail, attempt_id FROM lifecycle_operations "
                "WHERE pipeline_name = 'my-manual'",
            )
            assert len(rows) == 1
            assert rows[0]["op_kind"] == "boot_adopt"
            assert rows[0]["state"] == "complete"
            assert "boot adoption" in rows[0]["detail"]
            assert rows[0]["attempt_id"] == "r-a-a1"
        finally:
            ctrl.stop()

    def test_get_lifecycle_operations_filters_by_pipeline(self, tmp_path):
        db, ctrl = self._started(tmp_path)
        try:
            ctrl.stop_pipeline("my-manual")
            ops = ctrl.get_lifecycle_operations(pipeline_name="my-manual")
            assert len(ops) == 1 and ops[0]["op_kind"] == "stop"
            assert ctrl.get_lifecycle_operations(pipeline_name="nope") == []
        finally:
            ctrl.stop()
