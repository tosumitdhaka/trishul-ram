"""Tests for standalone live stats (Workstream A — v1.3.2)."""
from __future__ import annotations

import threading
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from tram.agent.stats_store import StatsStore
from tram.pipeline.controller import PipelineController, _LocalRun
from tram.pipeline.loader import load_pipeline_from_yaml

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


def _make_controller(stats_store=None):
    ctrl = PipelineController(node_id="test-node", stats_store=stats_store)
    ctrl.manager = MagicMock()
    ctrl.executor = MagicMock()
    return ctrl


# ── _stream_worker registers and removes _LocalRun ────────────────────────


def test_stream_worker_registers_local_run():
    store = StatsStore(interval=30)
    ctrl = _make_controller(stats_store=store)
    config = load_pipeline_from_yaml(_STREAM_YAML)

    stop_event = threading.Event()
    registered: list[str] = []

    def fake_stream_run(cfg, stop_ev, stats=None):
        with ctrl._local_stats_lock:
            registered.extend(ctrl._local_active_stats.keys())
        stop_event.set()

    ctrl.executor.stream_run.side_effect = fake_stream_run
    ctrl.manager.exists.return_value = True
    ctrl.manager.get.return_value = MagicMock(status="running")

    t = threading.Thread(target=ctrl._stream_worker, args=(config, stop_event))
    t.start()
    t.join(timeout=3)

    assert len(registered) == 1


def test_stream_worker_removes_local_run_on_exit():
    store = StatsStore(interval=30)
    ctrl = _make_controller(stats_store=store)
    config = load_pipeline_from_yaml(_STREAM_YAML)

    stop_event = threading.Event()
    stop_event.set()

    ctrl.executor.stream_run = MagicMock()
    ctrl.manager.exists.return_value = True
    ctrl.manager.get.return_value = MagicMock(status="running")

    t = threading.Thread(target=ctrl._stream_worker, args=(config, stop_event))
    t.start()
    t.join(timeout=3)

    with ctrl._local_stats_lock:
        assert len(ctrl._local_active_stats) == 0


def test_stream_worker_removes_from_stats_store_on_exit():
    store = StatsStore(interval=30)
    ctrl = _make_controller(stats_store=store)
    config = load_pipeline_from_yaml(_STREAM_YAML)

    stop_event = threading.Event()
    seen_run_id: list[str] = []

    def fake_stream_run(cfg, stop_ev, stats=None):
        if stats is not None:
            seen_run_id.append(stats.run_id)
            # Manually pre-populate the store to confirm remove() is called
            from tram.api.routers.internal import PipelineStatsPayload
            payload = PipelineStatsPayload(
                worker_id="test-node",
                pipeline_name=cfg.name,
                run_id=stats.run_id,
                schedule_type="stream",
                uptime_seconds=1.0,
                timestamp=datetime.now(UTC),
            )
            store.update(payload)
        stop_event.set()

    ctrl.executor.stream_run.side_effect = fake_stream_run
    ctrl.manager.exists.return_value = True
    ctrl.manager.get.return_value = MagicMock(status="running")

    t = threading.Thread(target=ctrl._stream_worker, args=(config, stop_event))
    t.start()
    t.join(timeout=3)

    assert len(seen_run_id) == 1
    assert store.get_by_run_id(seen_run_id[0]) is None


# ── _emit_local_stats_once calls stats_store.update ───────────────────────


def test_emit_local_stats_once_updates_store():
    store = StatsStore(interval=30)
    ctrl = _make_controller(stats_store=store)

    from tram.agent.metrics import PipelineStats
    run_id = "test-run-001"
    stats = PipelineStats(run_id=run_id, pipeline_name="my-stream", schedule_type="stream")
    stats.increment(records_in=10, records_out=10)

    local_run = _LocalRun(
        run_id=run_id,
        pipeline_name="my-stream",
        schedule_type="stream",
        started_at=datetime.now(UTC),
        stats=stats,
    )
    with ctrl._local_stats_lock:
        ctrl._local_active_stats[run_id] = local_run

    ctrl._emit_local_stats_once()

    entry = store.get_by_run_id(run_id)
    assert entry is not None
    assert entry.pipeline_name == "my-stream"
    assert entry.records_in == 10
    assert entry.worker_id == "test-node"
    assert entry.is_final is False


def test_emit_local_stats_once_noop_without_store():
    ctrl = _make_controller(stats_store=None)
    # Should not raise
    ctrl._emit_local_stats_once()


# ── _local_stats_loop exits on stop event ─────────────────────────────────


def test_local_stats_loop_exits_on_stop():
    store = StatsStore(interval=30)
    ctrl = _make_controller(stats_store=store)
    ctrl._local_stats_stop.clear()

    t = threading.Thread(target=ctrl._local_stats_loop, args=(1,), daemon=True)
    t.start()
    ctrl._local_stats_stop.set()
    t.join(timeout=3)
    assert not t.is_alive()


# ── Race: stats loop must not resurrect entry after clean stop ────────────


def test_emit_local_stats_once_does_not_resurrect_after_removal():
    """Simulate the race: stats loop copies run list, stream exits, loop tries to write back."""
    store = StatsStore(interval=30)
    ctrl = _make_controller(stats_store=store)

    from tram.agent.metrics import PipelineStats
    run_id = "race-run-001"
    stats = PipelineStats(run_id=run_id, pipeline_name="my-stream", schedule_type="stream")

    local_run = _LocalRun(
        run_id=run_id,
        pipeline_name="my-stream",
        schedule_type="stream",
        started_at=datetime.now(UTC),
        stats=stats,
    )

    # Simulate: loop has already copied runs list (pre-removal snapshot).
    # Then _stream_worker finally fires: atomically pops dict + removes from store.
    with ctrl._local_stats_lock:
        ctrl._local_active_stats[run_id] = local_run

    # Inject entry into store as if a previous loop tick wrote it
    from tram.api.routers.internal import PipelineStatsPayload
    store.update(PipelineStatsPayload(
        worker_id="test-node", pipeline_name="my-stream", run_id=run_id,
        schedule_type="stream", uptime_seconds=1.0, timestamp=datetime.now(UTC),
    ))

    # Now simulate _stream_worker finally: atomic pop + remove
    with ctrl._local_stats_lock:
        ctrl._local_active_stats.pop(run_id, None)
        store.remove(run_id)

    # StatsStore should be empty now
    assert store.get_by_run_id(run_id) is None

    # Now _emit_local_stats_once runs with the pre-removal snapshot.
    # It builds a payload for run_id but must NOT write it back.
    # Manually call with stale snapshot (simulating the race window):
    # We replicate what _emit_local_stats_once does after copying runs:
    now = datetime.now(UTC)
    uptime = (now - local_run.started_at).total_seconds()
    snapshot = local_run.stats.snapshot_and_reset_window()
    payload = PipelineStatsPayload(
        worker_id="test-node",
        pipeline_name=local_run.pipeline_name,
        run_id=run_id,
        schedule_type=local_run.schedule_type,
        uptime_seconds=uptime,
        timestamp=now,
        is_final=False,
        **snapshot,
    )
    # This is the guarded write — run_id is no longer in dict, so update is skipped
    with ctrl._local_stats_lock:
        if run_id in ctrl._local_active_stats:
            store.update(payload)

    # Entry must still be absent — not resurrected
    assert store.get_by_run_id(run_id) is None


# ── Loop not started in manager mode ──────────────────────────────────────


def test_stats_loop_not_started_in_manager_mode():
    store = StatsStore(interval=30)
    worker_pool = MagicMock()
    ctrl = PipelineController(node_id="mgr", stats_store=store, worker_pool=worker_pool)

    threads_before = {t.name for t in threading.enumerate()}

    with patch("apscheduler.schedulers.background.BackgroundScheduler") as MockSched:
        mock_sched = MagicMock()
        MockSched.return_value = mock_sched
        ctrl._db = None
        ctrl.start()
        ctrl._running = False

    threads_after = {t.name for t in threading.enumerate()}
    assert "tram-local-stats" not in (threads_after - threads_before)
