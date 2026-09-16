"""Concurrency tests for the PipelineController lifecycle RLock (plan B.5).

Proves the races documented in ``docs/reviews/code-review.md`` are closed:

  B1  trigger_run TOCTOU — check-then-dispatch races another run
  B2  no concurrency control on pipeline CRUD (update/delete interleaving)
  B10 manager not thread-safe — exists()-then-get() racing a delete

Design: the controller serializes lifecycle transitions and the running-claim
in ``_run_batch`` under one ``threading.RLock``. These tests line the races up
with ``threading.Barrier`` and gate blocking steps with ``threading.Event``;
bounded polls/settle windows are only used to stabilise assertions, never to
line up the race.
"""
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock

from tram.agent.worker_pool import DISPATCH_ACCEPTED, DispatchOutcome
from tram.core.context import RunResult, RunStatus
from tram.core.exceptions import PipelineNotFoundError
from tram.pipeline.controller import PipelineController
from tram.pipeline.loader import load_pipeline_from_yaml

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


def _make_controller(**kwargs) -> PipelineController:
    return PipelineController(node_id="test-node", **kwargs)


def _success(name: str) -> RunResult:
    return RunResult(
        run_id="unused",
        pipeline_name=name,
        status=RunStatus.SUCCESS,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        records_in=0,
        records_out=0,
        records_skipped=0,
        error=None,
        node_id="test-node",
    )


def _wait_pool_quiet(executor) -> None:
    """Wait until no batch run is in flight (two consecutive quiet samples)."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if executor.active == 0:
            time.sleep(0.2)
            if executor.active == 0:
                return
        time.sleep(0.01)
    raise AssertionError("batch pool did not go quiet within 5s")


class _CountingExecutor:
    """Executor double that tracks concurrent batch_run entries."""

    def __init__(self) -> None:
        self.enter_count = 0
        self.batch_calls = 0
        self.max_concurrent = 0
        self.active = 0
        self._gate = threading.Event()
        self._lock = threading.Lock()

    def batch_run(self, config, run_id=None, stats=None, config_sha256="", flush=False):
        with self._lock:
            self.enter_count += 1
            self.batch_calls += 1
            self.active += 1
            self.max_concurrent = max(self.max_concurrent, self.active)
        try:
            # Simulate a long-running batch; the test releases the gate.
            self._gate.wait(timeout=10)
        finally:
            with self._lock:
                self.active -= 1
        return _success(config.name)

    def release(self) -> None:
        self._gate.set()


def _join_threads(threads, timeout: float = 10) -> None:
    for t in threads:
        t.join(timeout)
        assert not t.is_alive(), "thread did not finish within timeout"


class TestTriggerTriggerRace:
    """B1: two near-simultaneous trigger_run calls must not double-run."""

    def test_local_mode_never_runs_twice_concurrently(self):
        ctrl = _make_controller()
        executor = _CountingExecutor()
        ctrl.executor = executor
        ctrl.manager.register(
            load_pipeline_from_yaml(_MANUAL_YAML), yaml_text=_MANUAL_YAML
        )

        barrier = threading.Barrier(2)
        errors: list = []

        def _trigger():
            barrier.wait()
            try:
                ctrl.trigger_run("my-manual")
            except ValueError:
                pass  # "already running" — the other trigger claimed first
            except Exception as exc:  # noqa: BLE001 — collected for assertion
                errors.append(exc)

        t1 = threading.Thread(target=_trigger)
        t2 = threading.Thread(target=_trigger)
        t1.start()
        t2.start()
        _join_threads([t1, t2])

        # Wait for at least one run to enter batch_run (pool processed a claim).
        deadline = time.monotonic() + 5.0
        while executor.enter_count < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert executor.enter_count >= 1, "no batch run ever started"

        # While the first run is in flight (gated), a second run must never start.
        time.sleep(0.3)
        assert executor.enter_count == 1, (
            f"second run entered batch_run while the first was active: "
            f"enter_count={executor.enter_count}"
        )
        assert executor.max_concurrent == 1

        executor.release()
        _wait_pool_quiet(executor)

        assert errors == []
        assert executor.batch_calls == 1
        assert ctrl.manager.get("my-manual").status == "stopped"
        ctrl.stop()

    def test_manager_mode_dispatches_once(self):
        wp = MagicMock()
        wp.dispatch_with_result.return_value = DispatchOutcome(
            worker_url="http://worker-0:8766", outcome=DISPATCH_ACCEPTED,
        )
        ctrl = _make_controller(worker_pool=wp, manager_url="http://manager:8765")
        ctrl.manager.register(
            load_pipeline_from_yaml(_INTERVAL_YAML), yaml_text=_INTERVAL_YAML
        )
        ctrl.manager.set_status("my-interval", "scheduled")

        barrier = threading.Barrier(2)
        errors: list = []

        def _trigger():
            barrier.wait()
            try:
                ctrl.trigger_run("my-interval")
            except ValueError:
                pass  # "already running" — acceptable
            except Exception as exc:  # noqa: BLE001 — collected for assertion
                errors.append(exc)

        t1 = threading.Thread(target=_trigger)
        t2 = threading.Thread(target=_trigger)
        t1.start()
        t2.start()
        _join_threads([t1, t2])

        deadline = time.monotonic() + 5.0
        while wp.dispatch_with_result.call_count < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)  # let any (incorrect) second dispatch surface

        assert errors == []
        assert wp.dispatch_with_result.call_count == 1, (
            f"expected exactly one dispatch, got {wp.dispatch_with_result.call_count}"
        )
        # Exactly one claimed run -> exactly one active lease.
        assert len(ctrl.get_active_batch_runs()) == 1
        ctrl.stop()


class TestTriggerUpdateRace:
    """B2: trigger racing update must not crash or corrupt registry state."""

    def test_no_crash_and_update_wins_registration(self):
        ctrl = _make_controller()
        executor = _CountingExecutor()
        ctrl.executor = executor
        v2 = _MANUAL_YAML + "description: updated-v2\n"
        ctrl.manager.register(
            load_pipeline_from_yaml(_MANUAL_YAML), yaml_text=_MANUAL_YAML
        )

        barrier = threading.Barrier(2)
        errors: list = []

        def _trigger():
            barrier.wait()
            try:
                ctrl.trigger_run("my-manual")
            except Exception as exc:  # noqa: BLE001 — collected for assertion
                errors.append(exc)

        def _update():
            barrier.wait()
            try:
                ctrl.update("my-manual", v2)
            except Exception as exc:  # noqa: BLE001 — collected for assertion
                errors.append(exc)

        t1 = threading.Thread(target=_trigger)
        t2 = threading.Thread(target=_update)
        t1.start()
        t2.start()
        _join_threads([t1, t2])

        executor.release()
        _wait_pool_quiet(executor)

        assert errors == []
        assert ctrl.manager.exists("my-manual")
        assert ctrl.manager.get("my-manual").yaml_text == v2
        assert executor.batch_calls == 1, "the triggered run must have executed once"
        assert ctrl.manager.get("my-manual").status == "stopped"
        ctrl.stop()


class TestTriggerDeleteRace:
    """B1/B10: trigger racing delete must never dispatch/execute a deleted pipeline."""

    def test_local_mode_no_run_after_delete_wins(self):
        ctrl = _make_controller()
        executor = _CountingExecutor()
        ctrl.executor = executor
        ctrl.manager.register(
            load_pipeline_from_yaml(_MANUAL_YAML), yaml_text=_MANUAL_YAML
        )

        barrier = threading.Barrier(2)
        errors: list = []

        def _trigger():
            barrier.wait()
            try:
                ctrl.trigger_run("my-manual")
            except PipelineNotFoundError:
                pass  # delete won before the trigger read the state — acceptable
            except Exception as exc:  # noqa: BLE001 — collected for assertion
                errors.append(exc)

        def _delete():
            barrier.wait()
            try:
                ctrl.delete("my-manual")
            except Exception as exc:  # noqa: BLE001 — collected for assertion
                errors.append(exc)

        t1 = threading.Thread(target=_trigger)
        t2 = threading.Thread(target=_delete)
        t1.start()
        t2.start()
        _join_threads([t1, t2])

        executor.release()
        _wait_pool_quiet(executor)

        assert errors == []
        assert not ctrl.manager.exists("my-manual")
        assert executor.batch_calls <= 1, (
            "a run executed for a pipeline that was deleted mid-race"
        )
        assert ctrl.get_active_batch_runs() == []
        ctrl.stop()

    def test_delete_during_dispatch_drops_lease(self):
        """CAS: delete while the worker dispatch HTTP call is in flight must
        leave no active-run lease for the deleted pipeline."""
        wp = MagicMock()
        dispatch_gate = threading.Event()
        dispatch_entered = threading.Event()
        dispatch_done = threading.Event()

        def _dispatch(**kwargs):
            dispatch_entered.set()
            assert dispatch_gate.wait(timeout=10)
            try:
                return DispatchOutcome(
                    worker_url="http://worker-0:8766", outcome=DISPATCH_ACCEPTED,
                )
            finally:
                dispatch_done.set()

        wp.dispatch_with_result.side_effect = _dispatch
        ctrl = _make_controller(worker_pool=wp, manager_url="http://manager:8765")
        ctrl.manager.register(
            load_pipeline_from_yaml(_INTERVAL_YAML), yaml_text=_INTERVAL_YAML
        )
        ctrl.manager.set_status("my-interval", "scheduled")

        # Trigger a manual run; it claims and blocks inside dispatch (network I/O).
        ctrl.trigger_run("my-interval")
        assert dispatch_entered.wait(timeout=5)

        # Delete the pipeline while the dispatch is still in flight.
        ctrl.delete("my-interval")

        # Let the dispatch finish; the post-dispatch CAS must refuse to track it.
        dispatch_gate.set()
        assert dispatch_done.wait(timeout=5)

        assert not ctrl.manager.exists("my-interval")
        assert ctrl.get_active_batch_runs() == [], (
            "a lease was recorded for a pipeline deleted during dispatch"
        )
        assert wp.dispatch_with_result.call_count == 1
        ctrl.stop()


class TestDuplicateWorkerCallbackRace:
    """D2-adjacent: concurrent duplicate run-complete callbacks record once."""

    def test_concurrent_duplicate_callbacks_record_once(self):
        ctrl = _make_controller()
        ctrl.manager.register(
            load_pipeline_from_yaml(_MANUAL_YAML), yaml_text=_MANUAL_YAML
        )
        ctrl.manager.set_status("my-manual", "running")

        barrier = threading.Barrier(2)
        errors: list = []

        def _callback():
            barrier.wait()
            try:
                ctrl.on_worker_run_complete(
                    run_id="dup-1",
                    pipeline_name="my-manual",
                    worker_id="worker-1",
                    status="success",
                    records_in=1,
                    records_out=1,
                )
            except Exception as exc:  # noqa: BLE001 — collected for assertion
                errors.append(exc)

        t1 = threading.Thread(target=_callback)
        t2 = threading.Thread(target=_callback)
        t1.start()
        t2.start()
        _join_threads([t1, t2])

        assert errors == []
        matches = [
            r for r in ctrl.manager.get("my-manual").run_history if r.run_id == "dup-1"
        ]
        assert len(matches) == 1, (
            f"duplicate callbacks recorded the run {len(matches)} times"
        )


class TestUpdateUpdateRace:
    """B2: two concurrent updates serialize cleanly."""

    def test_concurrent_updates_no_crash_consistent_final_state(self):
        ctrl = _make_controller()
        v2 = _MANUAL_YAML + "description: v2\n"
        v3 = _MANUAL_YAML + "description: v3\n"
        ctrl.manager.register(
            load_pipeline_from_yaml(_MANUAL_YAML), yaml_text=_MANUAL_YAML
        )

        barrier = threading.Barrier(2)
        errors: list = []

        def _up(yaml_text):
            barrier.wait()
            try:
                ctrl.update("my-manual", yaml_text)
            except Exception as exc:  # noqa: BLE001 — collected for assertion
                errors.append(exc)

        t1 = threading.Thread(target=_up, args=(v2,))
        t2 = threading.Thread(target=_up, args=(v3,))
        t1.start()
        t2.start()
        _join_threads([t1, t2])

        assert errors == []
        state = ctrl.manager.get("my-manual")
        assert state.yaml_text in (v2, v3)
        assert state.status == "stopped"
        assert len(ctrl.get_runs(pipeline_name="my-manual")) == 0


class TestStressInterleavedLifecycle:
    """N rounds of racing triggers + delete; invariants must hold every round."""

    def test_no_dispatch_of_deleted_pipeline_no_orphan_lease(self):
        wp = MagicMock()
        dispatch_lock = threading.Lock()
        in_flight = [0]

        def _dispatch(**kwargs):
            with dispatch_lock:
                in_flight[0] += 1
            try:
                return DispatchOutcome(
                    worker_url="http://worker-0:8766", outcome=DISPATCH_ACCEPTED,
                )
            finally:
                with dispatch_lock:
                    in_flight[0] -= 1

        wp.dispatch_with_result.side_effect = _dispatch
        ctrl = _make_controller(worker_pool=wp, manager_url="http://manager:8765")

        for round_no in range(20):
            name = f"stress-{round_no}"
            yaml = _INTERVAL_YAML.replace("my-interval", name)
            ctrl.manager.register(load_pipeline_from_yaml(yaml), yaml_text=yaml)
            ctrl.manager.set_status(name, "scheduled")

            barrier = threading.Barrier(3)
            errors: list = []

            def _trigger():
                barrier.wait()
                try:
                    ctrl.trigger_run(name)
                except PipelineNotFoundError:
                    pass  # delete won the race — acceptable
                except ValueError:
                    pass  # already running — acceptable
                except Exception as exc:  # noqa: BLE001 — collected for assertion
                    errors.append(exc)

            def _delete():
                barrier.wait()
                try:
                    ctrl.delete(name)
                except Exception as exc:  # noqa: BLE001 — collected for assertion
                    errors.append(exc)

            threads = [
                threading.Thread(target=_trigger),
                threading.Thread(target=_trigger),
                threading.Thread(target=_delete),
            ]
            for t in threads:
                t.start()
            _join_threads(threads)

            # Let any in-flight dispatch settle (two consecutive quiet samples),
            # so no new dispatch can appear for this round afterwards.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with dispatch_lock:
                    quiet = in_flight[0] == 0
                if quiet:
                    time.sleep(0.2)
                    with dispatch_lock:
                        quiet = in_flight[0] == 0
                    if quiet:
                        break
                time.sleep(0.01)

            assert errors == [], f"round {round_no} raised: {errors}"
            assert not ctrl.manager.exists(name)
            dispatch_calls = [
                c for c in wp.dispatch_with_result.call_args_list
                if c.kwargs.get("pipeline_name") == name
            ]
            assert len(dispatch_calls) <= 1, (
                f"round {round_no}: pipeline dispatched {len(dispatch_calls)} times"
            )
            assert all(r["pipeline_name"] != name for r in ctrl.get_active_batch_runs()), (
                f"round {round_no}: orphan lease for deleted pipeline"
            )

        ctrl.stop()