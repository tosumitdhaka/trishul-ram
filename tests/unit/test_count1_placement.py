"""Manager-side unit tests for D.2 (GH #17) — durable count=1 stream placement.

Covers the design's §10 manager-side subset: dispatch through the placement
machinery (flag on) vs the legacy single-dispatch path (flag off), boot
restore/adoption (restore, worker-dead redispatch, grace, adoption
materialization), and idempotency/races (delete-vs-redispatch CAS,
one-active-row deactivation). Concurrency cases follow the deterministic
Barrier/Event patterns of test_controller_concurrency.py — no sleep-based
race lining.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import text

from tram.agent.reconciler import PlacementReconciler
from tram.agent.stats_store import StatsStore
from tram.agent.worker_pool import (
    DISPATCH_ACCEPTED,
    BroadcastResult,
    DispatchOutcome,
)
from tram.api.routers.internal import PipelineStatsPayload
from tram.pipeline.controller import PipelineController
from tram.pipeline.loader import load_pipeline_from_yaml

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

_BROADCAST_STREAM_YAML = """\
name: my-broadcast-stream
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
  count: all
sinks:
  - type: local
    path: /tmp/out
"""


def _make_controller(db=None, worker_pool=None, single_stream_placements=True):
    return PipelineController(
        db=db,
        node_id="test-node",
        worker_pool=worker_pool,
        manager_url="http://manager:8765",
        single_stream_placements=single_stream_placements,
    )


def _db_mock():
    db = MagicMock()
    db.get_stopped_pipeline_names.return_value = []
    db.get_runs.return_value = []
    db.is_pipeline_stopped.return_value = False
    db.get_active_broadcast_placements.return_value = []
    return db


def _broadcast_result(pg_id, worker_url="http://w0:8766", *, accepted=True, error=None, status="running"):
    """A multi_dispatch BroadcastResult for a count=1 target."""
    if not accepted:
        slot = {
            "worker_index": 0,
            "worker_url": worker_url,
            "worker_id": "w0",
            "pinned_worker_id": None,
            "run_id_prefix": pg_id,
            "current_run_id": None,
            "status": "stale",
            "restart_count": 0,
        }
        if error is not None:
            slot["error"] = error
        return BroadcastResult(
            placement_group_id=pg_id,
            accepted=[],
            run_ids=[],
            rejected=[worker_url] if error is not None else [],
            status="error",
            slots=[slot],
        )
    return BroadcastResult(
        placement_group_id=pg_id,
        accepted=[worker_url],
        run_ids=[pg_id],
        rejected=[],
        status=status,
        slots=[{
            "worker_index": 0,
            "worker_url": worker_url,
            "worker_id": "w0",
            "pinned_worker_id": None,
            "run_id_prefix": pg_id,
            "current_run_id": pg_id,
            "status": "running",
            "restart_count": 0,
        }],
    )


def _count1_dispatch(worker_url="http://w0:8766"):
    """multi_dispatch side_effect that mirrors the real worker_pool contract:
    a count=1 target sets slot_run_id == placement_group_id, so the run id is
    the placement_group_id the controller passed in."""

    def _dispatch(placement_group_id, **kwargs):
        return _broadcast_result(placement_group_id, worker_url=worker_url)

    return _dispatch


def _save_restorable_placement(
    db,
    pg_id="pg1",
    pipeline="my-stream",
    run_id="pg1",
    worker_url="http://w0:8766",
    dispatched_at=None,
    started_at=None,
):
    db.save_pipeline(pipeline, _STREAM_YAML)
    db.save_broadcast_placement(
        placement_group_id=pg_id,
        pipeline_name=pipeline,
        slots=[{
            "worker_index": 0,
            "worker_url": worker_url,
            "worker_id": "w0",
            "run_id_prefix": run_id,
            "current_run_id": run_id,
            "status": "running",
            "restart_count": 0,
            "dispatched_at": (
                dispatched_at or (datetime.now(UTC) - timedelta(seconds=120))
            ).isoformat(),
        }],
        target_count="1",
        status="running",
        started_at=started_at or (datetime.now(UTC) - timedelta(seconds=120)),
    )


# ── Dispatch (§3) ──────────────────────────────────────────────────────────


def test_count1_dispatch_creates_placement(tmp_path):
    """Flag on: a count=1 stream dispatch produces a durable 1-slot placement
    row (target_count="1"), syncs _stream_run_ids, and labels the dispatch
    accepted."""
    from tram.persistence.db import TramDB

    db = TramDB(url=f"sqlite:///{tmp_path}/c1.db")
    wp = MagicMock()
    wp.multi_dispatch.side_effect = _count1_dispatch()
    wp.worker_id_for_url.return_value = "w0"
    ctrl = _make_controller(db=db, worker_pool=wp)
    config = load_pipeline_from_yaml(_STREAM_YAML)
    ctrl.manager.register(config, yaml_text=_STREAM_YAML)

    dispatch_counter = MagicMock()
    dispatch_counter.labels.return_value = dispatch_counter
    with patch("tram.metrics.registry.MGR_DISPATCH_TOTAL", dispatch_counter):
        ctrl._start_stream(config)

    dispatch_counter.labels.assert_called_once_with(
        pipeline="my-stream", result="accepted"
    )
    dispatch_counter.inc.assert_called_once()
    wp.multi_dispatch.assert_called_once()
    assert wp.dispatch_with_result.call_count == 0

    pg_id = ctrl._active_placement_group["my-stream"]
    placements = db.get_active_broadcast_placements()
    assert len(placements) == 1
    assert placements[0]["target_count"] == "1"
    slot = placements[0]["slots"][0]
    assert slot["worker_index"] == 0
    assert slot["current_run_id"] == pg_id
    assert slot["status"] == "running"
    assert ctrl._stream_run_ids["my-stream"] == [pg_id]
    assert ctrl.manager.get("my-stream").status == "running"
    db.close()


def test_count1_dispatch_no_capacity_vs_failed(tmp_path):
    """Empty resolve → no_workers metric + status error; rejected slot →
    dispatch_failed metric + slot error diagnostic. Parity with the legacy
    labels; neither path records a placement row."""
    from tram.persistence.db import TramDB

    db = TramDB(url=f"sqlite:///{tmp_path}/c1-nocap.db")
    wp = MagicMock()
    wp.multi_dispatch.return_value = _broadcast_result("pg-x", accepted=False, error=None)
    ctrl = _make_controller(db=db, worker_pool=wp)
    config = load_pipeline_from_yaml(_STREAM_YAML)
    ctrl.manager.register(config, yaml_text=_STREAM_YAML)

    counter = MagicMock()
    counter.labels.return_value = counter
    with patch("tram.metrics.registry.MGR_DISPATCH_TOTAL", counter):
        ctrl._start_stream(config)
    counter.labels.assert_called_once_with(pipeline="my-stream", result="no_workers")
    assert ctrl.manager.get("my-stream").status == "error"
    assert db.get_active_broadcast_placements() == []
    db.close()

    db2 = TramDB(url=f"sqlite:///{tmp_path}/c1-failed.db")
    wp2 = MagicMock()
    wp2.multi_dispatch.return_value = _broadcast_result(
        "pg-y", accepted=False, error="HTTP 503 from http://w0:8766"
    )
    ctrl2 = _make_controller(db=db2, worker_pool=wp2)
    config2 = load_pipeline_from_yaml(_STREAM_YAML)
    ctrl2.manager.register(config2, yaml_text=_STREAM_YAML)

    counter2 = MagicMock()
    counter2.labels.return_value = counter2
    with patch("tram.metrics.registry.MGR_DISPATCH_TOTAL", counter2):
        ctrl2._start_stream(config2)
    counter2.labels.assert_called_once_with(pipeline="my-stream", result="dispatch_failed")
    assert ctrl2.manager.get("my-stream").status == "error"
    assert db2.get_active_broadcast_placements() == []
    # The failure diagnostic is carried on the slot for observability.
    assert wp2.multi_dispatch.return_value.slots[0]["error"] == "HTTP 503 from http://w0:8766"
    db2.close()


def test_second_start_is_noop(tmp_path):
    """_start_stream twice while active (guards hit _stream_run_ids and
    _active_placement_group): exactly one dispatch call."""
    from tram.persistence.db import TramDB

    db = TramDB(url=f"sqlite:///{tmp_path}/c1-noop.db")
    wp = MagicMock()
    wp.multi_dispatch.side_effect = _count1_dispatch()
    wp.worker_id_for_url.return_value = "w0"
    ctrl = _make_controller(db=db, worker_pool=wp)
    config = load_pipeline_from_yaml(_STREAM_YAML)
    ctrl.manager.register(config, yaml_text=_STREAM_YAML)

    ctrl._start_stream(config)
    ctrl._start_stream(config)

    assert wp.multi_dispatch.call_count == 1
    assert len(db.get_active_broadcast_placements()) == 1
    db.close()


def test_flag_off_is_legacy(tmp_path):
    """Flag off: the legacy count=1 branch is retained verbatim — no placement
    row, dispatch_with_result, _stream_run_ids in manager memory only."""
    from tram.persistence.db import TramDB

    db = TramDB(url=f"sqlite:///{tmp_path}/c1-legacy.db")
    wp = MagicMock()
    wp.dispatch_with_result.return_value = DispatchOutcome(
        worker_url="http://w0:8766", outcome=DISPATCH_ACCEPTED,
    )
    ctrl = _make_controller(db=db, worker_pool=wp, single_stream_placements=False)
    config = load_pipeline_from_yaml(_STREAM_YAML)
    ctrl.manager.register(config, yaml_text=_STREAM_YAML)

    ctrl._start_stream(config)

    wp.dispatch_with_result.assert_called_once()
    wp.multi_dispatch.assert_not_called()
    assert db.get_active_broadcast_placements() == []
    assert len(ctrl._stream_run_ids["my-stream"]) == 1
    assert ctrl.manager.get("my-stream").status == "running"
    db.close()


# ── Boot restore / adoption (§5.1–5.2) ────────────────────────────────────


def test_boot_restore_adopts_without_redispatch(tmp_path):
    """Placement row + worker still live: restore sets reconciling, no /agent/run
    POST happens, the first stats payload transitions to running, and the
    worker-pool assignment is re-registered (stop_run now reaches the worker)."""
    from tram.persistence.db import TramDB

    db = TramDB(url=f"sqlite:///{tmp_path}/boot-live.db")
    _save_restorable_placement(db)
    wp = MagicMock()
    ctrl = _make_controller(db=db, worker_pool=wp)
    ctrl.start()

    try:
        assert ctrl.manager.get("my-stream").status == "reconciling"
        wp.multi_dispatch.assert_not_called()
        wp.dispatch_with_result.assert_not_called()
        wp.adopt_stream_assignment.assert_called_once_with(
            pipeline_name="my-stream", run_id="pg1", worker_url="http://w0:8766"
        )
        ctrl.on_pipeline_stats(PipelineStatsPayload(
            worker_id="w0",
            pipeline_name="my-stream",
            run_id="pg1",
            schedule_type="stream",
            uptime_seconds=5.0,
            timestamp=datetime.now(UTC),
        ))
        assert ctrl.manager.get("my-stream").status == "running"
        assert ctrl._stream_run_ids["my-stream"] == ["pg1"]
    finally:
        ctrl.stop()
    db.close()


def test_boot_restore_worker_dead_redispatches(tmp_path):
    """Placement row, no live run, stats beyond grace: slot → stale →
    redispatch_broadcast_slot with {prefix}-r1, restart_count == 1, placement
    degraded→running."""
    from tram.persistence.db import TramDB

    db = TramDB(url=f"sqlite:///{tmp_path}/boot-dead.db")
    _save_restorable_placement(db)
    wp = MagicMock()
    wp.live_streams.return_value = []
    wp.is_worker_healthy.return_value = True
    wp.dispatch_to_worker.return_value = True
    wp.worker_id_for_url.return_value = "w0"
    ctrl = _make_controller(db=db, worker_pool=wp)
    ctrl.start()
    reconciler = PlacementReconciler(ctrl, wp, StatsStore(interval=10), db, stats_interval=10)

    try:
        reconciler.run_once()

        slot = ctrl._broadcast_placements["pg1"]["slots"][0]
        assert slot["status"] == "running"
        assert slot["current_run_id"] == "pg1-r1"
        assert slot["restart_count"] == 1
        wp.dispatch_to_worker.assert_called_once_with(
            worker_url="http://w0:8766",
            run_id="pg1-r1",
            pipeline_name="my-stream",
            yaml_text=_STREAM_YAML,
            schedule_type="stream",
            callback_url="http://manager:8765/api/internal/run-complete",
        )
        assert ctrl.manager.get("my-stream").status == "running"
        placements = db.get_active_broadcast_placements()
        assert placements[0]["status"] == "running"
    finally:
        ctrl.stop()
    db.close()


def test_restore_grace_no_premature_redispatch(tmp_path):
    """Restored placement, no stats yet, inside the stats_interval+5 grace:
    no redispatch (guards the 2-minute-block exit criterion)."""
    from tram.persistence.db import TramDB

    db = TramDB(url=f"sqlite:///{tmp_path}/boot-grace.db")
    _save_restorable_placement(
        db,
        dispatched_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )
    wp = MagicMock()
    wp.live_streams.return_value = []
    ctrl = _make_controller(db=db, worker_pool=wp)
    ctrl.start()
    reconciler = PlacementReconciler(ctrl, wp, StatsStore(interval=30), db, stats_interval=30)

    try:
        reconciler.run_once()
        wp.dispatch_to_worker.assert_not_called()
        assert ctrl._broadcast_placements["pg1"]["slots"][0]["status"] == "running"
        assert ctrl._broadcast_placements["pg1"]["slots"][0]["current_run_id"] == "pg1"
    finally:
        ctrl.stop()
    db.close()


def test_adoption_materializes_placement(tmp_path):
    """B.6 bridge (flag on): no placement row, live run sighted at boot → a
    1-slot row with status running, run_id_prefix == adopted run_id, adopted."""
    from tram.persistence.db import TramDB

    db = TramDB(url=f"sqlite:///{tmp_path}/adopt.db")
    db.save_pipeline("my-stream", _STREAM_YAML)
    wp = MagicMock()
    wp.find_pipeline_runs.return_value = [{
        "worker_url": "http://w0:8766",
        "run_id": "live-run-9",
        "pipeline_name": "my-stream",
        "started_at": "2026-09-01T10:00:00+00:00",
        "schedule_type": "stream",
    }]
    wp.worker_id_for_url.return_value = "w0"
    ctrl = _make_controller(db=db, worker_pool=wp)
    ctrl.start()

    try:
        placements = db.get_active_broadcast_placements()
        assert len(placements) == 1
        assert placements[0]["target_count"] == "1"
        assert placements[0]["status"] == "running"
        slot = placements[0]["slots"][0]
        assert slot["run_id_prefix"] == "live-run-9"
        assert slot["current_run_id"] == "live-run-9"
        assert slot["adopted"] is True
        assert ctrl._active_placement_group["my-stream"] == placements[0]["placement_group_id"]
        assert ctrl._stream_run_ids["my-stream"] == ["live-run-9"]
        assert ctrl.manager.get("my-stream").status == "running"
        wp.multi_dispatch.assert_not_called()
        wp.dispatch_with_result.assert_not_called()
    finally:
        ctrl.stop()
    db.close()


# ── Idempotency / races (§7) ───────────────────────────────────────────────


def test_delete_races_redispatch(tmp_path):
    """delete() issued while the reconciler is mid redispatch (dispatch hook
    blocks): the CAS discards the slot commit and no active placement row
    survives."""
    from tram.persistence.db import TramDB

    db = TramDB(url=f"sqlite:///{tmp_path}/race.db")
    _save_restorable_placement(db)
    wp = MagicMock()
    wp.live_streams.return_value = []
    wp.is_worker_healthy.return_value = True
    wp.worker_id_for_url.return_value = "w0"

    dispatch_entered = threading.Event()
    dispatch_gate = threading.Event()
    dispatch_done = threading.Event()

    def _blocking_dispatch(**kwargs):
        dispatch_entered.set()
        assert dispatch_gate.wait(timeout=10)
        try:
            return True
        finally:
            dispatch_done.set()

    wp.dispatch_to_worker.side_effect = _blocking_dispatch

    ctrl = _make_controller(db=db, worker_pool=wp)
    ctrl.start()
    reconciler = PlacementReconciler(ctrl, wp, StatsStore(interval=10), db, stats_interval=10)

    errors: list = []

    def _reconcile():
        try:
            reconciler.run_once()
        except Exception as exc:  # noqa: BLE001 — collected for assertion
            errors.append(exc)

    t = threading.Thread(target=_reconcile)
    t.start()
    assert dispatch_entered.wait(timeout=5), "redispatch never reached dispatch_to_worker"

    # Delete the pipeline while the redispatch HTTP call is in flight.
    ctrl.delete("my-stream")

    dispatch_gate.set()
    assert dispatch_done.wait(timeout=5)
    t.join(10)
    assert not t.is_alive()
    assert errors == []

    # CAS discarded the stale slot commit: nothing active in memory or DB.
    assert ctrl._broadcast_placements == {}
    assert db.get_active_broadcast_placements() == []
    ctrl.stop()
    db.close()


def test_record_placement_deactivates_stale_rows(tmp_path):
    """A pre-existing second active row for the pipeline is marked stopped by a
    fresh _record_broadcast_placement (one-active-row invariant, §7.5)."""
    from tram.persistence.db import TramDB

    db = TramDB(url=f"sqlite:///{tmp_path}/stale-rows.db")
    db.save_pipeline("my-stream", _STREAM_YAML)
    db.save_broadcast_placement(
        placement_group_id="old-pg",
        pipeline_name="my-stream",
        slots=[{
            "worker_index": 0,
            "worker_url": "http://w0:8766",
            "worker_id": "w0",
            "run_id_prefix": "old-pg",
            "current_run_id": "old-pg",
            "status": "running",
            "restart_count": 0,
        }],
        target_count="1",
        status="running",
    )
    wp = MagicMock()
    wp.multi_dispatch.side_effect = _count1_dispatch()
    wp.worker_id_for_url.return_value = "w0"
    ctrl = _make_controller(db=db, worker_pool=wp)
    config = load_pipeline_from_yaml(_STREAM_YAML)
    ctrl.manager.register(config, yaml_text=_STREAM_YAML)

    ctrl._start_stream(config)

    active = db.get_active_broadcast_placements()
    assert len(active) == 1
    assert active[0]["placement_group_id"] == ctrl._active_placement_group["my-stream"]
    assert active[0]["placement_group_id"] != "old-pg"
    with db._engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, stopped_at FROM broadcast_placements "
                "WHERE placement_group_id = 'old-pg'"
            )
        ).fetchone()
    assert row[0] == "stopped"
    assert row[1] is not None
    db.close()


# ── Broadcast guard in the count=1 state machine (review) ───────────────────


def test_broadcast_stream_not_materialized_as_count1(tmp_path):
    """A broadcast (count: all) stream with a live run but no in-memory
    placement is never materialized as a 1-slot placement: it is not a liveness
    candidate and adopt_unplaced_stream_bookkeeping refuses broadcast configs
    (mirrors the boot guard) — a materialization here would permanently
    downgrade the broadcast stream to one slot."""
    from tram.persistence.db import TramDB

    db = TramDB(url=f"sqlite:///{tmp_path}/broadcast.db")
    db.save_pipeline("my-broadcast-stream", _BROADCAST_STREAM_YAML)
    wp = MagicMock()
    wp.live_streams.return_value = [{
        "worker_url": "http://w0:8766",
        "worker_id": "w0",
        "pipeline_name": "my-broadcast-stream",
        "run_id": "live-broadcast-1",
        "schedule_type": "stream",
        "uptime_seconds": 30.0,
        "started_at": "2026-09-01T10:00:00+00:00",
        "stats": {},
    }]
    wp.worker_id_for_url.return_value = "w0"
    ctrl = _make_controller(db=db, worker_pool=wp)
    config = load_pipeline_from_yaml(_BROADCAST_STREAM_YAML)
    ctrl.manager.register(config, yaml_text=_BROADCAST_STREAM_YAML)
    ctrl.manager.set_status("my-broadcast-stream", "running")

    # Not a liveness candidate: the unplaced pass never probes broadcast configs.
    assert ctrl.stream_liveness_candidates() == []

    # Defense in depth: the alive-branch entry point also refuses broadcast.
    assert not ctrl.adopt_unplaced_stream_bookkeeping(
        "my-broadcast-stream", "live-broadcast-1", "http://w0:8766"
    )
    assert "my-broadcast-stream" not in ctrl._active_placement_group
    assert db.get_active_broadcast_placements() == []
    assert ctrl._stream_run_ids.get("my-broadcast-stream") is None

    # End-to-end: a reconciler pass with a live sighting changes nothing.
    reconciler = PlacementReconciler(ctrl, wp, StatsStore(interval=10), db, stats_interval=10)
    reconciler.run_once()
    assert "my-broadcast-stream" not in ctrl._active_placement_group
    assert db.get_active_broadcast_placements() == []
    # The duplicate-live-run sweep must not stop broadcast slots either.
    wp.stop_run.assert_not_called()
    db.close()


# ── Dispatch metric semantics (review) ──────────────────────────────────────


def test_broadcast_dispatch_metric_increments_per_accepted_worker(tmp_path):
    """The unified flag-on branch preserves the legacy per-worker metric
    semantics: a broadcast dispatch with K accepted workers increments
    MGR_DISPATCH_TOTAL{accepted} K times — the unified branch must not collapse
    the counter to one increment (count=1 still lands at one)."""
    from tram.persistence.db import TramDB

    db = TramDB(url=f"sqlite:///{tmp_path}/c1-metric.db")
    worker_urls = ["http://w0:8766", "http://w1:8766", "http://w2:8766"]
    wp = MagicMock()

    def _broadcast_dispatch(placement_group_id, **kwargs):
        return BroadcastResult(
            placement_group_id=placement_group_id,
            accepted=worker_urls,
            run_ids=[f"pg-w{i}" for i in range(len(worker_urls))],
            rejected=[],
            status="running",
            slots=[
                {
                    "worker_index": i,
                    "worker_url": url,
                    "worker_id": f"w{i}",
                    "pinned_worker_id": None,
                    "run_id_prefix": f"pg-w{i}",
                    "current_run_id": f"pg-w{i}",
                    "status": "running",
                    "restart_count": 0,
                }
                for i, url in enumerate(worker_urls)
            ],
        )

    wp.multi_dispatch.side_effect = _broadcast_dispatch
    wp.worker_id_for_url.side_effect = lambda url: url.rsplit("/", 1)[-1].split(":")[0]
    ctrl = _make_controller(db=db, worker_pool=wp)
    config = load_pipeline_from_yaml(_BROADCAST_STREAM_YAML)
    ctrl.manager.register(config, yaml_text=_BROADCAST_STREAM_YAML)

    dispatch_counter = MagicMock()
    dispatch_counter.labels.return_value = dispatch_counter
    with patch("tram.metrics.registry.MGR_DISPATCH_TOTAL", dispatch_counter):
        ctrl._start_stream(config)

    assert dispatch_counter.labels.call_count == 3
    assert dispatch_counter.inc.call_count == 3
    assert all(
        call.kwargs == {"pipeline": "my-broadcast-stream", "result": "accepted"}
        for call in dispatch_counter.labels.call_args_list
    )
    db.close()


# ── Adoption materialization invariants (review) ────────────────────────────


def test_adoption_materialization_deactivates_stale_rows(tmp_path):
    """§7.5 one-active-row invariant on the adoption bridge: materializing a
    1-slot placement from a live run deactivates any pre-existing active row,
    mirroring _record_broadcast_placement."""
    from tram.persistence.db import TramDB

    db = TramDB(url=f"sqlite:///{tmp_path}/adopt-stale.db")
    db.save_pipeline("my-stream", _STREAM_YAML)
    db.save_broadcast_placement(
        placement_group_id="old-pg",
        pipeline_name="my-stream",
        slots=[{
            "worker_index": 0,
            "worker_url": "http://w0:8766",
            "worker_id": "w0",
            "run_id_prefix": "old-pg",
            "current_run_id": "old-pg",
            "status": "running",
            "restart_count": 0,
        }],
        target_count="1",
        status="running",
    )
    wp = MagicMock()
    wp.find_pipeline_runs.return_value = [{
        "worker_url": "http://w0:8766",
        "run_id": "live-run-9",
        "pipeline_name": "my-stream",
        "started_at": "2026-09-01T10:00:00+00:00",
        "schedule_type": "stream",
    }]
    wp.worker_id_for_url.return_value = "w0"
    ctrl = _make_controller(db=db, worker_pool=wp)
    config = load_pipeline_from_yaml(_STREAM_YAML)
    ctrl.manager.register(config, yaml_text=_STREAM_YAML)

    assert ctrl._adopt_live_stream_if_any(config) is True

    active = db.get_active_broadcast_placements()
    assert len(active) == 1
    assert active[0]["placement_group_id"] != "old-pg"
    with db._engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, stopped_at FROM broadcast_placements "
                "WHERE placement_group_id = 'old-pg'"
            )
        ).fetchone()
    assert row[0] == "stopped"
    assert row[1] is not None
    db.close()


# ── Flag parsing (review) ───────────────────────────────────────────────────


def test_unrecognized_flag_value_logs_warning_and_fails_open(monkeypatch, caplog):
    """TRAM_STREAM_SINGLE_PLACEMENT fails open on typos: any value other than
    "0"/"1" enables the durable-placement path AND logs a WARNING — loud typo
    detection instead of silently flipping a deployment's stream semantics."""
    monkeypatch.setenv("TRAM_STREAM_SINGLE_PLACEMENT", "off")
    with caplog.at_level(logging.WARNING, logger="tram.pipeline.controller"):
        ctrl = PipelineController()
    assert ctrl._single_stream_placements is True
    assert any(
        "TRAM_STREAM_SINGLE_PLACEMENT" in rec.getMessage() for rec in caplog.records
    )

    from tram.core.config import AppConfig

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tram.core.config"):
        config = AppConfig.from_env()
    assert config.stream_single_placement is True
    assert any(
        "TRAM_STREAM_SINGLE_PLACEMENT" in rec.getMessage() for rec in caplog.records
    )


# ── D.1/D.2 integration canary (§10) ────────────────────────────────────────


def test_stats_callback_concurrent_with_restore(tmp_path):
    """Design §10 canary (D.1/D.2 integration): a stats callback racing a boot
    restore must not perform a whole-slots RMW.

    Deterministic interleave: the restore thread blocks on its DB commit while
    the stats callback (real thread) queues on the lifecycle lock. The stats
    path must commit through the D.1 scoped per-slot CAS
    (update_slot_run_id(..., expected_run_id=...)) — never a whole-slots RMW
    (get_active_broadcast_placements → write slots back) — so the newer recorded
    run id survives and the final slot state is consistent, not clobbered."""
    from tram.persistence.db import TramDB

    db = TramDB(url=f"sqlite:///{tmp_path}/canary.db")
    _save_restorable_placement(db, run_id="pg1")

    reads = {"n": 0}
    original_get = db.get_active_broadcast_placements

    def _counted_get():
        reads["n"] += 1
        return original_get()

    db.get_active_broadcast_placements = _counted_get
    placement = db.get_active_broadcast_placements()[0]  # the one setup read

    cas_calls: list = []
    original_slot_update = db.update_slot_run_id

    def _capturing_slot_update(*args, **kwargs):
        cas_calls.append((args, kwargs))
        return original_slot_update(*args, **kwargs)

    db.update_slot_run_id = _capturing_slot_update

    restore_committing = threading.Event()
    release_restore = threading.Event()
    commit_count = {"n": 0}
    original_update = db.update_broadcast_placement_status

    def _gated_update(placement_group_id, status, slots=None):
        commit_count["n"] += 1
        if commit_count["n"] == 1:
            restore_committing.set()
            assert release_restore.wait(timeout=5)
        original_update(placement_group_id, status, slots=slots)

    db.update_broadcast_placement_status = _gated_update

    wp = MagicMock()
    wp.worker_id_for_url.return_value = "w0"
    ctrl = _make_controller(db=db, worker_pool=wp)

    errors: list = []

    def _restore():
        try:
            with ctrl._lock:
                ctrl._restore_broadcast_placement(placement)
        except Exception as exc:  # noqa: BLE001 — collected for assertion
            errors.append(exc)

    def _stats_callback():
        try:
            ctrl.on_pipeline_stats(PipelineStatsPayload(
                worker_id="w0",
                pipeline_name="my-stream",
                run_id="pg1-r1",
                schedule_type="stream",
                uptime_seconds=10.0,
                timestamp=datetime.now(UTC),
            ))
        except Exception as exc:  # noqa: BLE001 — collected for assertion
            errors.append(exc)

    t_restore = threading.Thread(target=_restore)
    t_restore.start()
    assert restore_committing.wait(timeout=5), "restore never reached its DB commit"

    # Restore is mid-commit, holding the lifecycle lock. Fire the stats callback
    # concurrently — it queues on the lock and runs after the restore completes.
    t_stats = threading.Thread(target=_stats_callback)
    t_stats.start()
    release_restore.set()
    t_restore.join(10)
    t_stats.join(10)

    assert not t_restore.is_alive() and not t_stats.is_alive()
    assert errors == []

    # The stats path committed through the scoped per-slot CAS with the
    # optimistic-concurrency guard — no whole-slots RMW re-read.
    assert cas_calls, "stats path never reached the scoped per-slot CAS"
    cas_args, cas_kwargs = cas_calls[0]
    assert cas_kwargs.get("expected_run_id") == "pg1"
    assert cas_args[2] == "pg1-r1"
    assert reads["n"] == 1

    # The newer recorded run id advanced cleanly in memory and on disk.
    slot = ctrl._broadcast_placements["pg1"]["slots"][0]
    assert slot["current_run_id"] == "pg1-r1"
    assert slot["status"] == "running"
    persisted = db.get_active_broadcast_placements()[0]["slots"][0]
    assert persisted["current_run_id"] == "pg1-r1"
    db.close()