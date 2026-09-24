"""Tests for the reusable _upsert helper and queued manual runs (E.2 / GH #21).

Covers §3 of docs/plans/e2-queued-manual-runs-design.md: the dialect-SQL
shapes of _upsert (parametrized over mocked engines capturing statements), the
sqlite round-trip with the insert-if-absent (DO NOTHING) mode, and the 11
queued_runs helpers — state-transition rowcount fencing, view filters,
ordering, and TTL expiry. All timestamps round-trip as UTC-aware datetimes
(the reconciler.py:29-36 parsing pattern).
"""
from __future__ import annotations

import threading
import time
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from tram.agent.reconciler import BatchReconciler
from tram.agent.stats_store import StatsStore
from tram.agent.worker_pool import (
    DISPATCH_ACCEPTED,
    DISPATCH_FAILED,
    DISPATCH_NO_CAPACITY,
    DispatchOutcome,
    WorkerPool,
)
from tram.core.context import RunResult, RunStatus
from tram.persistence.db import TramDB
from tram.pipeline.controller import PipelineController
from tram.pipeline.loader import load_pipeline_from_yaml

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    d = TramDB(url=f"sqlite:///{tmp_path}/queued.db", node_id="node-test")
    yield d
    d.close()


# ── Fake engine capturing statements (dialect-SQL shape tests) ──────────────


class _FakeConn:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))
        return self


class _FakeBegin:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    def __init__(self, dialect):
        self.dialect = types.SimpleNamespace(name=dialect)
        self.conn = _FakeConn()

    def begin(self):
        return _FakeBegin(self.conn)


def _upsert_db(dialect: str) -> TramDB:
    db = object.__new__(TramDB)
    db._engine = _FakeEngine(dialect)
    return db


# ── _upsert dialect SQL shapes (§3.2) ───────────────────────────────────────


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql", "mysql"])
def test_upsert_dialect_sql(dialect):
    """Default mode emits ON CONFLICT / ON DUPLICATE KEY with all non-keys."""
    db = _upsert_db(dialect)
    db._upsert("t", {"id": "1", "name": "n", "count": 2}, key_columns=("id",))
    sql, params = db._engine.conn.calls[0]
    assert "INSERT INTO t (id, name, count) VALUES (:id, :name, :count)" in sql
    if dialect == "mysql":
        assert "ON DUPLICATE KEY UPDATE name = VALUES(name), count = VALUES(count)" in sql
    else:
        assert "ON CONFLICT (id) DO UPDATE SET name = excluded.name, count = excluded.count" in sql
    assert params == {"id": "1", "name": "n", "count": 2}


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql", "mysql"])
def test_upsert_do_nothing_shape(dialect):
    """update_columns=() degrades to insert-if-absent (DO NOTHING / INSERT IGNORE)."""
    db = _upsert_db(dialect)
    db._upsert("t", {"id": "1", "name": "n"}, key_columns=("id",), update_columns=())
    sql, _ = db._engine.conn.calls[0]
    if dialect == "mysql":
        assert sql.startswith("INSERT IGNORE INTO t (id, name) VALUES (:id, :name)")
        assert "ON DUPLICATE" not in sql
    else:
        assert "ON CONFLICT (id) DO NOTHING" in sql
        assert "DO UPDATE" not in sql


def test_upsert_update_columns_subset():
    """Only the listed columns are written on conflict."""
    db = _upsert_db("sqlite")
    db._upsert(
        "t",
        {"id": "1", "name": "n", "count": 2, "note": "x"},
        key_columns=("id",),
        update_columns=("name",),
    )
    sql, _ = db._engine.conn.calls[0]
    assert "DO UPDATE SET name = excluded.name" in sql
    assert "count = excluded.count" not in sql
    assert "note = excluded.note" not in sql


def test_upsert_generic_fallback_delete_insert():
    """Exotic dialects get DELETE by key + INSERT, in one transaction."""
    db = _upsert_db("oracle")
    db._upsert("t", {"id": "1", "name": "n"}, key_columns=("id",))
    calls = db._engine.conn.calls
    assert len(calls) == 2
    assert calls[0][0].startswith("DELETE FROM t WHERE id = :id")
    assert calls[0][1] == {"id": "1", "name": "n"}
    assert calls[1][0].startswith("INSERT INTO t (id, name) VALUES (:id, :name)")


def test_upsert_generic_insert_if_absent_is_plain_insert():
    """Generic fallback, insert-if-absent mode: a single plain INSERT."""
    db = _upsert_db("oracle")
    db._upsert("t", {"id": "1", "name": "n"}, key_columns=("id",), update_columns=())
    assert len(db._engine.conn.calls) == 1
    assert db._engine.conn.calls[0][0].startswith("INSERT INTO t (id, name)")


def test_upsert_roundtrip_sqlite(db):
    """Insert, conflict-update, DO-NOTHING, and subset-update on a real DB."""
    db._upsert("settings", {"key": "a", "value": "v1", "updated_at": "t1"}, key_columns=("key",))
    assert db.get_setting("a") == "v1"

    # Conflict → default mode updates all non-key columns.
    db._upsert("settings", {"key": "a", "value": "v2", "updated_at": "t2"}, key_columns=("key",))
    assert db.get_setting("a") == "v2"
    with db._engine.connect() as conn:
        row = conn.execute(text("SELECT updated_at FROM settings WHERE key = 'a'")).fetchone()
    assert row[0] == "t2"

    # update_columns=() → insert-if-absent: existing row untouched.
    db._upsert(
        "settings",
        {"key": "a", "value": "v3", "updated_at": "t3"},
        key_columns=("key",),
        update_columns=(),
    )
    assert db.get_setting("a") == "v2"

    # update_columns=("value",) → only that column refreshed.
    db._upsert(
        "settings",
        {"key": "a", "value": "v4", "updated_at": "t4"},
        key_columns=("key",),
        update_columns=("value",),
    )
    assert db.get_setting("a") == "v4"
    with db._engine.connect() as conn:
        row = conn.execute(text("SELECT updated_at FROM settings WHERE key = 'a'")).fetchone()
    assert row[0] == "t2"

    # insert-if-absent still inserts a brand-new key.
    db._upsert(
        "settings",
        {"key": "b", "value": "vb", "updated_at": "tb"},
        key_columns=("key",),
        update_columns=(),
    )
    assert db.get_setting("b") == "vb"


# ── Refactored call-site round-trips (thin-coverage call sites) ─────────────


def test_set_password_hash_roundtrip(db):
    db.set_password_hash("alice", "hash1")
    assert db.get_password_hash("alice") == "hash1"
    assert db.has_password_users() is True
    db.set_password_hash("alice", "hash2")  # upsert overwrites
    assert db.get_password_hash("alice") == "hash2"


def test_set_setting_roundtrip(db):
    db.set_setting("ai.model", "gpt-4")
    assert db.get_setting("ai.model") == "gpt-4"
    db.set_setting("ai.model", "gpt-5")  # upsert overwrites
    assert db.get_setting("ai.model") == "gpt-5"


def test_save_pipeline_roundtrip_preserves_created_at(db):
    db.save_pipeline("pipe", "yaml: v1", source="api")
    with db._engine.connect() as conn:
        created = conn.execute(
            text("SELECT created_at FROM registered_pipelines WHERE name = 'pipe'")
        ).scalar()
    db.save_pipeline("pipe", "yaml: v2", source="disk")  # conflict → update
    assert db.get_all_pipelines() == [("pipe", "yaml: v2")]
    with db._engine.connect() as conn:
        row = conn.execute(text(
            "SELECT created_at, source, deleted, yaml_text "
            "FROM registered_pipelines WHERE name = 'pipe'"
        )).mappings().fetchone()
    assert row["created_at"] == created  # preserved across conflict-update
    assert row["source"] == "disk"  # refreshed
    assert row["deleted"] == 0
    assert row["yaml_text"] == "yaml: v2"
    # soft-delete then re-save resurrects the row with deleted=0
    db.delete_pipeline("pipe")
    assert db.get_all_pipelines() == []
    db.save_pipeline("pipe", "yaml: v3")
    assert db.get_all_pipelines() == [("pipe", "yaml: v3")]


# ── Queued runs helpers (§3.3) ──────────────────────────────────────────────


def _ts(day, hour=0):
    return datetime(2026, 1, day, hour, tzinfo=UTC)


def _save(db, run_id="r1", pipeline="pipe-a", yaml="yaml: 1", requested=None, expires=None):
    db.save_queued_run(
        run_id,
        pipeline,
        yaml,
        requested or _ts(1),
        expires or _ts(1, 12),
    )


def test_save_queued_run_roundtrip(db):
    _save(db, "r1", "pipe-a", "snapshot: 1")
    rows = db.get_active_queued_runs()
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == "r1"
    assert row["pipeline_name"] == "pipe-a"
    assert row["yaml_snapshot"] == "snapshot: 1"
    assert row["status"] == "queued"
    assert row["requested_at"] == _ts(1)
    assert row["expires_at"] == _ts(1, 12)
    assert row["dispatched_at"] is None


def test_save_queued_run_resave_refreshes(db):
    """A re-save of the same run_id refreshes the row (upsert semantics)."""
    _save(db, "r1", "pipe-a", "old: yaml")
    _save(db, "r1", "pipe-a", "new: yaml", requested=_ts(2))
    rows = db.get_active_queued_runs()
    assert len(rows) == 1
    assert rows[0]["yaml_snapshot"] == "new: yaml"
    assert rows[0]["requested_at"] == _ts(2)


def test_get_active_queued_runs_filters_and_orders(db):
    _save(db, "r2", "pipe-b", requested=_ts(2))
    _save(db, "r1", "pipe-a", requested=_ts(1))
    _save(db, "r3", "pipe-c", requested=_ts(3))
    # r3 → dispatched (terminal) — excluded from the active view
    assert db.claim_queued_run_row("r3") == 1
    assert db.mark_queued_run_dispatched("r3", _ts(3, 1)) == 1
    rows = db.get_active_queued_runs()
    assert [r["run_id"] for r in rows] == ["r1", "r2"]


def test_get_queued_run_view_includes_dispatching(db):
    _save(db, "r1", "pipe-a")
    _save(db, "r2", "pipe-b")
    _save(db, "r3", "pipe-c")
    assert db.claim_queued_run_row("r2") == 1  # dispatching
    assert db.expire_queued_run_row("r3") == 1  # expired (terminal)
    rows = db.get_queued_run_view()
    assert {r["run_id"] for r in rows} == {"r1", "r2"}


def test_get_active_queued_run_for_pipeline(db):
    _save(db, "r1", "pipe-a")
    row = db.get_active_queued_run_for_pipeline("pipe-a")
    assert row is not None and row["run_id"] == "r1"
    # dispatching rows still count as active
    assert db.claim_queued_run_row("r1") == 1
    assert db.get_active_queued_run_for_pipeline("pipe-a")["run_id"] == "r1"
    # dispatched (terminal) → not active
    assert db.mark_queued_run_dispatched("r1", _ts(1, 1)) == 1
    assert db.get_active_queued_run_for_pipeline("pipe-a") is None
    # unknown pipeline
    assert db.get_active_queued_run_for_pipeline("nope") is None


def test_claim_queued_run_row_fences(db):
    """The conditional UPDATE is the single-claim fence (rowcount 0 on wrong status)."""
    _save(db, "r1", "pipe-a")
    assert db.claim_queued_run_row("r1") == 1
    assert db.get_active_queued_run_for_pipeline("pipe-a")["status"] == "dispatching"
    # second claim on a dispatching row loses the fence
    assert db.claim_queued_run_row("r1") == 0
    # claim on an expired row loses
    _save(db, "r2", "pipe-b")
    assert db.expire_queued_run_row("r2") == 1
    assert db.claim_queued_run_row("r2") == 0
    # unknown run_id
    assert db.claim_queued_run_row("missing") == 0


def test_mark_queued_run_dispatched(db):
    _save(db, "r1", "pipe-a")
    assert db.claim_queued_run_row("r1") == 1
    assert db.mark_queued_run_dispatched("r1", _ts(1, 1)) == 1
    assert db.get_active_queued_run_for_pipeline("pipe-a") is None
    with db._engine.connect() as conn:
        raw = conn.execute(text(
            "SELECT status, dispatched_at FROM queued_runs WHERE run_id = 'r1'"
        )).mappings().fetchone()
    assert raw["status"] == "dispatched"
    assert raw["dispatched_at"] == _ts(1, 1).isoformat()
    # wrong prior status (queued) → 0
    _save(db, "r2", "pipe-b")
    assert db.mark_queued_run_dispatched("r2", _ts(1, 1)) == 0


def test_revert_queued_run_row(db):
    _save(db, "r1", "pipe-a")
    # revert on a queued row (wrong prior status) → 0
    assert db.revert_queued_run_row("r1") == 0
    assert db.claim_queued_run_row("r1") == 1
    assert db.revert_queued_run_row("r1") == 1
    assert db.get_active_queued_run_for_pipeline("pipe-a")["status"] == "queued"


def test_expire_queued_run_row(db):
    _save(db, "r1", "pipe-a")
    assert db.expire_queued_run_row("r1") == 1
    with db._engine.connect() as conn:
        raw = conn.execute(text("SELECT status FROM queued_runs WHERE run_id = 'r1'")).scalar()
    assert raw == "expired"
    # wrong prior status (dispatching) → 0
    _save(db, "r2", "pipe-b")
    assert db.claim_queued_run_row("r2") == 1
    assert db.expire_queued_run_row("r2") == 0
    assert db.get_active_queued_run_for_pipeline("pipe-b")["status"] == "dispatching"


def test_refresh_queued_run_yaml(db):
    _save(db, "r1", "pipe-a", yaml="old: yaml")
    assert db.refresh_queued_run_yaml("pipe-a", "new: yaml") == 1
    assert db.get_active_queued_runs()[0]["yaml_snapshot"] == "new: yaml"
    # dispatching rows are not refreshed (snapshot already handed to the worker)
    assert db.claim_queued_run_row("r1") == 1
    assert db.refresh_queued_run_yaml("pipe-a", "even: newer") == 0
    with db._engine.connect() as conn:
        raw = conn.execute(text(
            "SELECT yaml_snapshot FROM queued_runs WHERE run_id = 'r1'"
        )).scalar()
    assert raw == "new: yaml"


def test_delete_queued_runs_purges_non_terminal_only(db):
    _save(db, "r1", "pipe-a")
    _save(db, "r2", "pipe-a")
    _save(db, "r3", "pipe-a")
    assert db.claim_queued_run_row("r2") == 1
    assert db.claim_queued_run_row("r3") == 1
    assert db.mark_queued_run_dispatched("r3", _ts(1, 1)) == 1  # terminal audit row
    assert db.delete_queued_runs("pipe-a") == 2  # r1 (queued) + r2 (dispatching)
    with db._engine.connect() as conn:
        remaining = conn.execute(text(
            "SELECT run_id FROM queued_runs WHERE pipeline_name = 'pipe-a'"
        )).scalars().all()
    assert remaining == ["r3"]  # dispatched row kept for audit
    assert db.delete_queued_runs("nope") == 0


def test_reset_dispatching_queued_runs(db):
    _save(db, "r1", "pipe-a")
    _save(db, "r2", "pipe-b")
    _save(db, "r3", "pipe-c")
    assert db.claim_queued_run_row("r1") == 1
    assert db.claim_queued_run_row("r3") == 1
    assert db.expire_queued_run_row("r2") == 1
    assert db.reset_dispatching_queued_runs() == 2
    assert {r["run_id"] for r in db.get_active_queued_runs()} == {"r1", "r3"}
    with db._engine.connect() as conn:
        raw = conn.execute(text("SELECT status FROM queued_runs WHERE run_id = 'r2'")).scalar()
    assert raw == "expired"  # terminal rows untouched
    assert db.reset_dispatching_queued_runs() == 0  # idempotent


def test_queued_run_timestamps_parsed_utc(db):
    """Naive ISO strings are coerced to UTC on read (reconciler.py:29-36 pattern)."""
    with db._engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO queued_runs (run_id, pipeline_name, yaml_snapshot, status,
                                     requested_at, expires_at, dispatched_at)
            VALUES ('naive1', 'pipe-a', 'yaml', 'queued',
                    '2026-01-01T10:00:00', '2026-01-01T10:30:00', NULL)
        """))
    row = db.get_active_queued_runs()[0]
    assert row["requested_at"] == datetime(2026, 1, 1, 10, tzinfo=UTC)
    assert row["expires_at"] == datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    assert row["requested_at"].tzinfo == UTC
    assert row["dispatched_at"] is None


def test_queued_run_drain_lifecycle(db):
    """claim → revert → re-claim → dispatched, the drain state machine."""
    _save(db, "r1", "pipe-a", yaml="snapshot: 1")
    assert db.claim_queued_run_row("r1") == 1
    assert db.get_active_queued_run_for_pipeline("pipe-a")["status"] == "dispatching"
    # dispatch failed → revert to queued (retry next pass)
    assert db.revert_queued_run_row("r1") == 1
    assert db.get_active_queued_runs()[0]["status"] == "queued"
    # capacity returns → claim and dispatch
    assert db.claim_queued_run_row("r1") == 1
    assert db.mark_queued_run_dispatched("r1", _ts(1, 1)) == 1
    assert db.get_active_queued_runs() == []
    assert db.get_active_queued_run_for_pipeline("pipe-a") is None


# ── Controller enqueue path (§4) ────────────────────────────────────────────


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


def _make_controller(db, worker_pool=None, **kwargs):
    return PipelineController(
        db=db,
        node_id="node-test",
        worker_pool=worker_pool,
        manager_url="http://manager:8765",
        queue_manual_runs=True,
        **kwargs,
    )


def _register_manual(ctrl):
    ctrl.manager.register(load_pipeline_from_yaml(_MANUAL_YAML), yaml_text=_MANUAL_YAML)


def _register_interval(ctrl):
    ctrl.manager.register(load_pipeline_from_yaml(_INTERVAL_YAML), yaml_text=_INTERVAL_YAML)


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestEnqueue:
    def test_trigger_no_capacity_enqueues_returns_queued(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = []
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            dispatch_counter = MagicMock()
            dispatch_counter.labels.return_value = dispatch_counter
            enqueue_counter = MagicMock()
            enqueue_counter.labels.return_value = enqueue_counter
            with patch("tram.metrics.registry.MGR_DISPATCH_TOTAL", dispatch_counter), \
                 patch("tram.metrics.registry.MGR_QUEUE_ENQUEUED_TOTAL", enqueue_counter):
                result = ctrl.trigger_run("my-manual")

            assert result.disposition == "queued"
            rows = db.get_active_queued_runs()
            assert len(rows) == 1
            assert rows[0]["run_id"] == result.run_id
            assert rows[0]["yaml_snapshot"] == _MANUAL_YAML
            assert rows[0]["status"] == "queued"
            assert rows[0]["expires_at"] == rows[0]["requested_at"] + timedelta(seconds=900)
            assert ctrl.manager.get("my-manual").status == "queued"
            assert ctrl.get_runs(pipeline_name="my-manual") == []  # no FAILED row
            wp.dispatch_with_result.assert_not_called()
            # metric continuity with the legacy fail-fast + enqueue counter
            dispatch_counter.labels.assert_any_call(pipeline="my-manual", result="no_workers")
            dispatch_counter.inc.assert_called()
            enqueue_counter.labels.assert_called_with(pipeline="my-manual")
            enqueue_counter.inc.assert_called()
        finally:
            ctrl.stop()

    def test_trigger_with_capacity_submits_normally(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        wp.dispatch_with_result.return_value = DispatchOutcome(
            worker_url="http://w0:8766", outcome=DISPATCH_ACCEPTED,
        )
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            captured = {}
            orig_submit = ctrl._thread_pool.submit

            def _submit(fn, *args, **kwargs):
                captured["fn"] = fn
                return orig_submit(fn, *args, **kwargs)

            ctrl._thread_pool.submit = _submit
            result = ctrl.trigger_run("my-manual")
            assert result.disposition == "dispatched"
            assert db.get_active_queued_runs() == []
            # submitted with origin="manual" so the fallback site can distinguish
            assert captured["fn"].keywords == {"origin": "manual", "flush": False}
            assert _wait_until(lambda: wp.dispatch_with_result.call_count >= 1)
            assert wp.dispatch_with_result.call_args.kwargs["run_id"] == result.run_id
        finally:
            ctrl.stop()

    def test_dispatch_failed_never_enqueues(self, db):
        """Bug-inheritance guard #1: DISPATCH_FAILED keeps the fail-fast path."""
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        wp.dispatch_with_result.return_value = DispatchOutcome(
            worker_url=None, outcome=DISPATCH_FAILED, error="worker refused",
        )
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            result = ctrl.trigger_run("my-manual")
            assert result.disposition == "dispatched"
            assert _wait_until(lambda: len(ctrl.get_runs(pipeline_name="my-manual")) == 1)
            assert db.get_active_queued_runs() == []
            runs = ctrl.get_runs(pipeline_name="my-manual")
            assert runs[0].status == RunStatus.FAILED
            assert "Worker dispatch failed" in (runs[0].error or "")
        finally:
            ctrl.stop()

    def test_single_probe_flap_does_not_enqueue(self, db):
        """Bug-inheritance guard #2: one failed probe (below the hysteresis
        threshold) leaves the worker healthy → normal trigger, no queue."""
        wp = WorkerPool(workers=["http://w0:8766"])
        wp._health["http://w0:8766"]["failures"] = 1  # < health_failures_to_down=2
        assert wp.healthy_workers() == ["http://w0:8766"]
        wp.dispatch_with_result = MagicMock(return_value=DispatchOutcome(
            worker_url="http://w0:8766", outcome=DISPATCH_ACCEPTED,
        ))
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            result = ctrl.trigger_run("my-manual")
            assert result.disposition == "dispatched"
            assert db.get_active_queued_runs() == []
        finally:
            ctrl.stop()

    def test_fallback_enqueue_on_mid_dispatch_capacity_loss(self, db):
        """The RCA's enqueue point: capacity vanished between trigger and
        dispatch — the submitted _run_batch's no-capacity branch queues."""
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        wp.dispatch_with_result.return_value = DispatchOutcome(
            worker_url=None, outcome=DISPATCH_NO_CAPACITY,
        )
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            result = ctrl.trigger_run("my-manual")
            assert result.disposition == "dispatched"  # sync site saw capacity
            assert _wait_until(lambda: len(db.get_active_queued_runs()) == 1)
            rows = db.get_active_queued_runs()
            assert rows[0]["run_id"] == result.run_id
            assert rows[0]["status"] == "queued"
            assert rows[0]["yaml_snapshot"] == _MANUAL_YAML
            assert ctrl.manager.get("my-manual").status == "queued"
            assert ctrl.get_runs(pipeline_name="my-manual") == []  # no FAILED row
        finally:
            ctrl.stop()

    def test_scheduled_fire_never_enqueues(self, db):
        wp = MagicMock()
        wp.dispatch_with_result.return_value = DispatchOutcome(
            worker_url=None, outcome=DISPATCH_NO_CAPACITY,
        )
        ctrl = _make_controller(db, wp)
        _register_interval(ctrl)
        try:
            ctrl._run_batch("my-interval")  # default origin="scheduled"
            assert db.get_active_queued_runs() == []
            runs = ctrl.get_runs(pipeline_name="my-interval")
            assert len(runs) == 1
            assert runs[0].status == RunStatus.FAILED
            assert "No healthy workers" in (runs[0].error or "")
            assert ctrl.manager.get("my-interval").status == "error"
        finally:
            ctrl.stop()

    def test_standalone_never_enqueues(self, db):
        ctrl = PipelineController(db=db, node_id="node-test", queue_manual_runs=True)
        _register_manual(ctrl)
        ctrl.executor = MagicMock()
        ctrl.executor.batch_run.return_value = RunResult(
            run_id="local-1",
            pipeline_name="my-manual",
            status=RunStatus.SUCCESS,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            records_in=0,
            records_out=0,
            records_skipped=0,
            node_id="node-test",
        )
        try:
            result = ctrl.trigger_run("my-manual")
            assert result.disposition == "dispatched"
            assert db.get_active_queued_runs() == []
        finally:
            ctrl.stop()


class TestDedupe:
    def test_second_trigger_returns_existing(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = []
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            first = ctrl.trigger_run("my-manual")
            assert first.disposition == "queued"
            second = ctrl.trigger_run("my-manual")
            assert second.disposition == "queued"
            assert second.run_id == first.run_id  # Decision 3: return-existing
            rows = db.get_active_queued_runs()
            assert len(rows) == 1
            assert rows[0]["run_id"] == first.run_id
        finally:
            ctrl.stop()

    def test_concurrent_triggers_single_row(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = []
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            barrier = threading.Barrier(2)
            results: list = []
            errors: list = []

            def _trigger():
                barrier.wait()
                try:
                    results.append(ctrl.trigger_run("my-manual"))
                except Exception as exc:  # noqa: BLE001 — collected for assertion
                    errors.append(exc)

            t1 = threading.Thread(target=_trigger)
            t2 = threading.Thread(target=_trigger)
            t1.start()
            t2.start()
            t1.join(10)
            t2.join(10)
            assert not t1.is_alive() and not t2.is_alive()
            assert errors == []
            rows = db.get_active_queued_runs()
            assert len(rows) == 1, "two concurrent triggers must produce one row"
            assert len({r.run_id for r in results}) == 1
            assert all(r.disposition == "queued" for r in results)
        finally:
            ctrl.stop()

    def test_trigger_while_queued_and_healthy_returns_existing(self, db):
        """The [capacity-returned → drain-commit] window: a queued row plus
        healthy workers used to submit a fresh _run_batch that the claim-phase
        skip (status 'queued') discarded — a 200 with a run_id that 404s. The
        DB is the source of truth, so the trigger dedupes before any submit."""
        wp = MagicMock()
        wp.healthy_workers.return_value = []  # outage at trigger time
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            first = ctrl.trigger_run("my-manual")
            assert first.disposition == "queued"
            assert db.get_active_queued_runs()[0]["run_id"] == first.run_id

            # capacity returns, but the drain has not committed yet
            wp.healthy_workers.return_value = ["http://w0:8766"]
            captured = {}
            orig_submit = ctrl._thread_pool.submit

            def _submit(fn, *args, **kwargs):
                captured["fn"] = fn
                return orig_submit(fn, *args, **kwargs)

            ctrl._thread_pool.submit = _submit
            second = ctrl.trigger_run("my-manual")
            assert second.disposition == "queued"
            assert second.run_id == first.run_id  # the EXISTING run_id, honest 202
            assert "fn" not in captured  # no _run_batch submit
            rows = db.get_active_queued_runs()
            assert len(rows) == 1  # still one row
            assert rows[0]["run_id"] == first.run_id
        finally:
            ctrl.stop()

    def test_trigger_dedupes_with_half_broken_worker_reverting(self, db):
        """A healthy-per-debounce worker whose /agent/run fails makes the drain
        revert-and-retry for the whole TTL; every re-trigger in that period
        must dedupe (202 with the existing run_id), never submit a run the
        claim phase discards."""
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]  # healthy per debounce
        wp.dispatch_with_result.return_value = DispatchOutcome(
            worker_url=None, outcome=DISPATCH_FAILED, error="boom",
        )
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            # seed the queue directly (capacity existed, so the sync trigger
            # would not have enqueued)
            _enqueue(ctrl, db)
            # drain attempts and reverts — row back to queued
            BatchReconciler(ctrl, wp, interval=10).run_once()
            rows = db.get_active_queued_runs()
            assert len(rows) == 1
            assert rows[0]["status"] == "queued"

            captured = {}
            orig_submit = ctrl._thread_pool.submit

            def _submit(fn, *args, **kwargs):
                captured["fn"] = fn
                return orig_submit(fn, *args, **kwargs)

            ctrl._thread_pool.submit = _submit
            result = ctrl.trigger_run("my-manual")
            assert result.disposition == "queued"
            assert result.run_id == rows[0]["run_id"]  # no discarded run_ids
            assert "fn" not in captured
            assert len(db.get_active_queued_runs()) == 1
        finally:
            ctrl.stop()

    def test_dedupe_sets_pipeline_status_queued(self, db):
        """After a manager restart the in-memory status is stale while the
        queued row survived — the dedupe path must restore status 'queued' so
        the badge doesn't lie."""
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]  # capacity returned
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            _enqueue(ctrl, db)
            ctrl.manager.set_status("my-manual", "stopped")  # stale pre-restart status
            result = ctrl.trigger_run("my-manual")
            assert result.disposition == "queued"
            assert result.run_id == "r1"
            assert ctrl.manager.get("my-manual").status == "queued"
            assert len(db.get_active_queued_runs()) == 1
        finally:
            ctrl.stop()


# ── Drain (§6) ──────────────────────────────────────────────────────────────


def _enqueue(ctrl, db, run_id="r1", pipeline="my-manual", yaml_text=_MANUAL_YAML):
    assert ctrl._enqueue_manual_run(pipeline, run_id, yaml_text) is True


class TestDrain:
    def test_drain_dispatches_when_capacity_returns(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = []  # outage at trigger time
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            triggered = ctrl.trigger_run("my-manual")
            assert triggered.disposition == "queued"

            # capacity returns → the drain dispatches with the queued run_id
            # and the auditable yaml_snapshot
            wp.healthy_workers.return_value = ["http://w0:8766"]
            wp.dispatch_with_result.return_value = DispatchOutcome(
                worker_url="http://w0:8766", outcome=DISPATCH_ACCEPTED,
            )
            dispatched_counter = MagicMock()
            dispatched_counter.labels.return_value = dispatched_counter
            wait_hist = MagicMock()
            wait_hist.labels.return_value = wait_hist
            drain_result = MagicMock()
            drain_result.labels.return_value = drain_result
            with patch("tram.metrics.registry.MGR_QUEUE_DISPATCHED_TOTAL", dispatched_counter), \
                 patch("tram.metrics.registry.MGR_QUEUE_WAIT_SECONDS", wait_hist), \
                 patch("tram.metrics.registry.MGR_QUEUE_DRAIN_RESULT_TOTAL", drain_result):
                BatchReconciler(ctrl, wp, interval=10).run_once()

            assert wp.dispatch_with_result.call_count == 1
            kwargs = wp.dispatch_with_result.call_args.kwargs
            assert kwargs["run_id"] == triggered.run_id
            assert kwargs["pipeline_name"] == "my-manual"
            assert kwargs["yaml_text"] == _MANUAL_YAML
            assert kwargs["schedule_type"] == "manual"
            # row terminal, lease recorded, status running
            assert db.get_active_queued_runs() == []
            leases = ctrl.get_active_batch_runs()
            assert len(leases) == 1
            assert leases[0]["run_id"] == triggered.run_id
            assert ctrl.manager.get("my-manual").status == "running"
            dispatched_counter.labels.assert_called_with(pipeline="my-manual")
            dispatched_counter.inc.assert_called()
            # the drain-result counter's dispatched label — the design's metric
            # table has three outcomes (dispatched | no_capacity | failed)
            drain_result.labels.assert_called_with(pipeline="my-manual", result="dispatched")
            drain_result.inc.assert_called()
            wait_hist.labels.assert_called_with(pipeline="my-manual")
            wait_hist.observe.assert_called()
        finally:
            ctrl.stop()

    def test_drain_fast_completing_run_skips_lease_but_dispatches_row(self, db):
        """Fast-run race (GH #47, queued-drain counterpart of the _run_batch
        CAS fix): a worker that completes and posts run-complete before
        commit_queued_dispatch records the lease must not get a stale lease
        (the BatchReconciler would probe is_run_active → False and mark the
        succeeded run lost), yet the queued row must still leave 'dispatching'
        so the drain never re-claims a completed run."""
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            _enqueue(ctrl, db)

            def _dispatch_and_complete(**kwargs):
                # The worker finishes before the drain thread records the lease.
                ctrl.on_worker_run_complete(
                    run_id=kwargs["run_id"],
                    pipeline_name="my-manual",
                    worker_id="w0",
                    status="success",
                    records_in=2,
                    records_out=2,
                )
                return DispatchOutcome(
                    worker_url="http://w0:8766", outcome=DISPATCH_ACCEPTED,
                )

            wp.dispatch_with_result.side_effect = _dispatch_and_complete
            BatchReconciler(ctrl, wp, interval=10).run_once()

            # No stale lease — the reconciler would otherwise mark the run lost.
            assert ctrl.get_active_batch_runs() == []
            # The queued row still transitions dispatching → dispatched (terminal).
            assert db.get_active_queued_runs() == []
            assert db.get_active_queued_run_for_pipeline("my-manual") is None
            with db._engine.connect() as conn:
                raw = conn.execute(
                    text("SELECT status FROM queued_runs WHERE run_id = 'r1'")
                ).scalar()
            assert raw == "dispatched"
            # The run succeeded — no FAILED result under the queued run_id.
            result = ctrl.get_run("r1")
            assert result is not None
            assert result.status == RunStatus.SUCCESS
            # Post-run status, never 'error' (and not clobbered to 'running').
            assert ctrl.manager.get("my-manual").status == "stopped"
            # A second drain pass does not re-dispatch the completed run.
            BatchReconciler(ctrl, wp, interval=10).run_once()
            assert wp.dispatch_with_result.call_count == 1
        finally:
            ctrl.stop()

    def test_drain_single_claim(self, db):
        """The conditional UPDATE is the single-claim fence: a second claim on
        a dispatching row is None, and two concurrent drain passes dispatch
        exactly once."""
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            _enqueue(ctrl, db)
            assert ctrl.claim_queued_run("r1") is not None
            assert ctrl.claim_queued_run("r1") is None  # fence
            assert ctrl.revert_queued_claim("r1", result="failed") is True

            dispatch_count: list[str] = []

            def _dispatch(**kwargs):
                dispatch_count.append(kwargs["run_id"])
                return DispatchOutcome(worker_url="http://w0:8766", outcome=DISPATCH_ACCEPTED)

            wp.dispatch_with_result.side_effect = _dispatch
            # A concurrent drain may observe the just-committed "running"
            # status before the lease is visible in its `tracked` snapshot
            # (thread B's tracked read predates thread A's commit). When
            # that interleave hits, the untracked pass either adopts (match
            # found) or marks the run lost (no match) — both paths must be
            # mock-safe:
            # - find_pipeline_runs returning the dispatched run makes that
            #   pass a benign idempotent adopt of r1; an empty list sends
            #   it to mark_active_batch_run_lost instead.
            # - worker_id_for_url must return a real value: unmocked, the
            #   MagicMock flows into RunResult.node_id and the run-history
            #   INSERT fails to bind it (sqlite rejects MagicMock).
            # - is_run_active=True keeps the tracked pass off the mark-lost
            #   path for a lease it sees (a truthy MagicMock already skips,
            #   but be explicit).
            wp.find_pipeline_runs.return_value = [
                {
                    "run_id": "r1",
                    "worker_url": "http://w0:8766",
                    "started_at": "2026-01-01T00:00:00+00:00",
                }
            ]
            wp.is_run_active.return_value = True
            wp.worker_id_for_url.return_value = "w0"
            reconciler = BatchReconciler(ctrl, wp, interval=10)
            barrier = threading.Barrier(2)
            errors: list = []

            def _drain():
                barrier.wait()
                try:
                    reconciler.run_once()
                except Exception as exc:  # noqa: BLE001 — collected for assertion
                    errors.append(exc)

            t1 = threading.Thread(target=_drain)
            t2 = threading.Thread(target=_drain)
            t1.start()
            t2.start()
            t1.join(30)
            t2.join(30)
            # A join timeout must fail loudly here rather than surfacing as a
            # confusing partial-state assertion below (load-sensitive joins).
            assert not t1.is_alive() and not t2.is_alive()
            assert errors == []
            assert len(dispatch_count) == 1
            assert dispatch_count[0] == "r1"
            assert db.get_active_queued_runs() == []
        finally:
            ctrl.stop()

    def test_drain_skips_running_pipeline(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            _enqueue(ctrl, db)
            # an active batch lease for the same pipeline
            assert ctrl.adopt_active_batch_run(
                pipeline_name="my-manual", run_id="other", worker_url="http://w0:8766"
            ) is True
            assert ctrl.drainable_queued_runs() == []
            BatchReconciler(ctrl, wp, interval=10).run_once()
            wp.dispatch_with_result.assert_not_called()
        finally:
            ctrl.stop()

    def test_drain_reverts_on_dispatch_failure(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        wp.dispatch_with_result.return_value = DispatchOutcome(
            worker_url=None, outcome=DISPATCH_FAILED, error="boom",
        )
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            _enqueue(ctrl, db)
            drain_result = MagicMock()
            drain_result.labels.return_value = drain_result
            with patch("tram.metrics.registry.MGR_QUEUE_DRAIN_RESULT_TOTAL", drain_result):
                BatchReconciler(ctrl, wp, interval=10).run_once()
            # row back to queued, no run-history churn
            rows = db.get_active_queued_runs()
            assert len(rows) == 1
            assert rows[0]["status"] == "queued"
            assert ctrl.get_runs(pipeline_name="my-manual") == []
            drain_result.labels.assert_called_with(pipeline="my-manual", result="failed")
            drain_result.inc.assert_called()
            # a second pass retries and succeeds once capacity actually works
            wp.dispatch_with_result.return_value = DispatchOutcome(
                worker_url="http://w0:8766", outcome=DISPATCH_ACCEPTED,
            )
            BatchReconciler(ctrl, wp, interval=10).run_once()
            assert db.get_active_queued_runs() == []
        finally:
            ctrl.stop()

    def test_drain_reverts_on_no_capacity_race(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]  # healthy at pre-check
        wp.dispatch_with_result.return_value = DispatchOutcome(
            worker_url=None, outcome=DISPATCH_NO_CAPACITY,  # vanished mid-pass
        )
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            _enqueue(ctrl, db)
            drain_result = MagicMock()
            drain_result.labels.return_value = drain_result
            with patch("tram.metrics.registry.MGR_QUEUE_DRAIN_RESULT_TOTAL", drain_result):
                BatchReconciler(ctrl, wp, interval=10).run_once()
            rows = db.get_active_queued_runs()
            assert len(rows) == 1
            assert rows[0]["status"] == "queued"
            assert ctrl.get_runs(pipeline_name="my-manual") == []
            drain_result.labels.assert_called_with(pipeline="my-manual", result="no_capacity")
            drain_result.inc.assert_called()
        finally:
            ctrl.stop()

    def test_drain_noop_when_unhealthy(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = []
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            _enqueue(ctrl, db)
            BatchReconciler(ctrl, wp, interval=10).run_once()
            wp.dispatch_with_result.assert_not_called()  # flap-safe pre-check
            rows = db.get_active_queued_runs()
            assert len(rows) == 1
            assert rows[0]["status"] == "queued"
        finally:
            ctrl.stop()

    def test_expiry_writes_failed_run_with_distinct_error(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            # backdate: requested 20m ago, expired 5m ago
            now = datetime.now(UTC)
            db.save_queued_run(
                "r1", "my-manual", _MANUAL_YAML,
                now - timedelta(minutes=20), now - timedelta(minutes=5),
            )
            ctrl.manager.set_status("my-manual", "queued")
            expired_counter = MagicMock()
            expired_counter.labels.return_value = expired_counter
            with patch("tram.metrics.registry.MGR_QUEUE_EXPIRED_TOTAL", expired_counter):
                BatchReconciler(ctrl, wp, interval=10).run_once()
            with db._engine.connect() as conn:
                status = conn.execute(
                    text("SELECT status FROM queued_runs WHERE run_id = 'r1'")
                ).scalar()
            assert status == "expired"
            runs = ctrl.get_runs(pipeline_name="my-manual")
            assert len(runs) == 1
            assert runs[0].status == RunStatus.FAILED
            assert "no worker capacity within" in (runs[0].error or "")
            assert runs[0].started_at == now - timedelta(minutes=20)  # requested_at
            assert ctrl.manager.get("my-manual").status == "error"
            expired_counter.labels.assert_called_with(pipeline="my-manual")
            expired_counter.inc.assert_called()
            wp.dispatch_with_result.assert_not_called()
        finally:
            ctrl.stop()

    def test_expiry_fires_during_worker_outage(self, db):
        """The TTL is enforced when it matters: a row past expires_at fails on
        the drain pass even with ZERO healthy workers. The pre-check is flap-
        safe for dispatch attempts only; expiry is pure DB + finalize and must
        not be gated behind worker health."""
        wp = MagicMock()
        wp.healthy_workers.return_value = []  # full outage
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            now = datetime.now(UTC)
            db.save_queued_run(
                "r1", "my-manual", _MANUAL_YAML,
                now - timedelta(minutes=20), now - timedelta(minutes=5),
            )
            ctrl.manager.set_status("my-manual", "queued")
            expired_counter = MagicMock()
            expired_counter.labels.return_value = expired_counter
            with patch("tram.metrics.registry.MGR_QUEUE_EXPIRED_TOTAL", expired_counter):
                BatchReconciler(ctrl, wp, interval=10).run_once()
            with db._engine.connect() as conn:
                status = conn.execute(
                    text("SELECT status FROM queued_runs WHERE run_id = 'r1'")
                ).scalar()
            assert status == "expired"
            runs = ctrl.get_runs(pipeline_name="my-manual")
            assert len(runs) == 1
            assert runs[0].status == RunStatus.FAILED
            assert "no worker capacity within" in (runs[0].error or "")
            assert runs[0].started_at == now - timedelta(minutes=20)  # requested_at
            assert ctrl.manager.get("my-manual").status == "error"
            expired_counter.labels.assert_called_with(pipeline="my-manual")
            expired_counter.inc.assert_called()
            # flap-safety: unhealthy workers → no dispatch attempt at all
            wp.dispatch_with_result.assert_not_called()
            assert ctrl.get_active_batch_runs() == []
        finally:
            ctrl.stop()

    def test_worker_restored_nudge_wakes_loop(self, db):
        """§6.5: the on_health_restored hook sets the nudge event → run_once
        executes before the interval (Event-based, no sleeps)."""
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        dispatched = threading.Event()

        def _dispatch(**kwargs):
            dispatched.set()
            return DispatchOutcome(worker_url="http://w0:8766", outcome=DISPATCH_ACCEPTED)

        wp.dispatch_with_result.side_effect = _dispatch
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        reconciler = BatchReconciler(ctrl, wp, interval=60)  # long interval
        try:
            _enqueue(ctrl, db)
            reconciler.start()
            # app wiring: worker_pool.on_health_restored = batch_reconciler.nudge
            wp.on_health_restored = reconciler.nudge
            assert not dispatched.is_set()
            wp.on_health_restored()  # worker came back up
            assert dispatched.wait(timeout=5), "nudge did not wake the drain loop"
            assert db.get_active_queued_runs() == []
        finally:
            reconciler.stop()
            ctrl.stop()


# ── Lifecycle (§7) ──────────────────────────────────────────────────────────


class TestLifecycleHooks:
    def test_delete_purges_queued_runs(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            _enqueue(ctrl, db)
            ctrl.delete("my-manual")
            with db._engine.connect() as conn:
                remaining = conn.execute(text(
                    "SELECT run_id FROM queued_runs WHERE pipeline_name = 'my-manual'"
                )).scalars().all()
            assert remaining == []
            # drain never dispatches a purged run, and no expiry FAILED row appears
            BatchReconciler(ctrl, wp, interval=10).run_once()
            wp.dispatch_with_result.assert_not_called()
            assert db.get_runs(pipeline_name="my-manual") == []
        finally:
            ctrl.stop()

    def test_stop_purges_queued_runs(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            _enqueue(ctrl, db)
            ctrl.stop_pipeline("my-manual")
            assert db.get_active_queued_runs() == []
            assert ctrl.manager.get("my-manual").status == "stopped"
        finally:
            ctrl.stop()

    def test_update_refreshes_yaml_snapshot(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        wp.dispatch_with_result.return_value = DispatchOutcome(
            worker_url="http://w0:8766", outcome=DISPATCH_ACCEPTED,
        )
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        try:
            _enqueue(ctrl, db)
            v2 = _MANUAL_YAML + "description: updated-v2\n"
            ctrl.update("my-manual", v2)
            rows = db.get_active_queued_runs()
            assert len(rows) == 1
            assert rows[0]["yaml_snapshot"] == v2  # Decision 5
            # the drain dispatches the refreshed snapshot
            BatchReconciler(ctrl, wp, interval=10).run_once()
            kwargs = wp.dispatch_with_result.call_args.kwargs
            assert kwargs["yaml_text"] == v2
        finally:
            ctrl.stop()

    def test_boot_resets_dispatching_and_requeues(self, db):
        """Decision 4: a crash mid-claim leaves 'dispatching'; boot resets it
        to 'queued' and the drain dispatches it."""
        now = datetime.now(UTC)
        db.save_pipeline("my-manual", _MANUAL_YAML, source="api")
        db.save_queued_run("r1", "my-manual", _MANUAL_YAML, now, now + timedelta(minutes=15))
        assert db.claim_queued_run_row("r1") == 1  # stuck dispatching

        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        wp.dispatch_with_result.return_value = DispatchOutcome(
            worker_url="http://w0:8766", outcome=DISPATCH_ACCEPTED,
        )
        ctrl = _make_controller(db, wp)
        try:
            ctrl._boot_load()
            rows = db.get_active_queued_runs()
            assert len(rows) == 1
            assert rows[0]["status"] == "queued"  # boot reset
            BatchReconciler(ctrl, wp, interval=10).run_once()
            assert db.get_active_queued_runs() == []
            assert wp.dispatch_with_result.call_count == 1
            assert wp.dispatch_with_result.call_args.kwargs["run_id"] == "r1"
        finally:
            ctrl.stop()

    def test_boot_expires_past_ttl(self, db):
        """A row past expires_at at boot writes the FAILED row on the first
        drain pass (the absolute TTL clock kept running during downtime)."""
        now = datetime.now(UTC)
        db.save_pipeline("my-manual", _MANUAL_YAML, source="api")
        db.save_queued_run(
            "r1", "my-manual", _MANUAL_YAML,
            now - timedelta(minutes=30), now - timedelta(minutes=10),
        )
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        ctrl = _make_controller(db, wp)
        try:
            ctrl._boot_load()
            BatchReconciler(ctrl, wp, interval=10).run_once()
            runs = ctrl.get_runs(pipeline_name="my-manual")
            assert len(runs) == 1
            assert runs[0].status == RunStatus.FAILED
            assert "no worker capacity within" in (runs[0].error or "")
            with db._engine.connect() as conn:
                status = conn.execute(
                    text("SELECT status FROM queued_runs WHERE run_id = 'r1'")
                ).scalar()
            assert status == "expired"
            wp.dispatch_with_result.assert_not_called()
        finally:
            ctrl.stop()

    def test_scheduled_fire_skipped_while_queued(self, db):
        """Claim phase treats 'queued' like 'running' — the one-active-run-per-
        pipeline invariant holds through the queue."""
        wp = MagicMock()
        wp.healthy_workers.return_value = []
        ctrl = _make_controller(db, wp)
        _register_interval(ctrl)
        try:
            triggered = ctrl.trigger_run("my-interval")
            assert triggered.disposition == "queued"
            assert ctrl.manager.get("my-interval").status == "queued"
            ctrl._run_batch("my-interval")  # scheduled fire while queued
            assert ctrl.manager.get("my-interval").status == "queued"
            assert len(db.get_active_queued_runs()) == 1
            wp.dispatch_with_result.assert_not_called()
        finally:
            ctrl.stop()


# ── API (§8.1) ──────────────────────────────────────────────────────────────


def _make_api_app(controller, db=None):
    from tram.api.routers import pipelines as pipelines_router
    from tram.api.routers import runs as runs_router

    app = FastAPI()
    app.include_router(pipelines_router.router)
    app.include_router(runs_router.router)
    app.state.controller = controller
    app.state.manager = controller.manager
    app.state.scheduler = controller
    app.state.db = db
    app.state.stats_store = StatsStore(interval=30)
    return app


class TestRunEndpoint:
    def test_run_endpoint_202_queued(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = []
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        app = _make_api_app(ctrl, db)
        try:
            client = TestClient(app)
            resp = client.post("/api/pipelines/my-manual/run")
            assert resp.status_code == 202
            data = resp.json()
            assert data["name"] == "my-manual"
            assert data["status"] == "queued"
            assert data["run_id"]
            assert data["expires_at"]  # absolute TTL deadline
        finally:
            ctrl.stop()

    def test_run_endpoint_200_triggered(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = ["http://w0:8766"]
        wp.dispatch_with_result.return_value = DispatchOutcome(
            worker_url="http://w0:8766", outcome=DISPATCH_ACCEPTED,
        )
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        app = _make_api_app(ctrl, db)
        try:
            client = TestClient(app)
            resp = client.post("/api/pipelines/my-manual/run")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "triggered"
            assert data["run_id"]
            assert db.get_active_queued_runs() == []
        finally:
            ctrl.stop()

    def test_run_endpoint_202_idempotent(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = []
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        app = _make_api_app(ctrl, db)
        try:
            client = TestClient(app)
            r1 = client.post("/api/pipelines/my-manual/run")
            r2 = client.post("/api/pipelines/my-manual/run")
            assert r1.status_code == 202
            assert r2.status_code == 202
            assert r1.json()["run_id"] == r2.json()["run_id"]
        finally:
            ctrl.stop()


class TestRunsMerge:
    def test_runs_list_merges_queued(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = []
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        app = _make_api_app(ctrl, db)
        try:
            client = TestClient(app)
            result = ctrl.trigger_run("my-manual")
            assert result.disposition == "queued"
            resp = client.get("/api/runs")
            assert resp.status_code == 200
            rows = resp.json()
            assert len(rows) == 1
            row = rows[0]
            # shape parity with RunResult.to_dict()
            assert set(row.keys()) == {
                "run_id", "pipeline", "status", "started_at", "finished_at",
                "records_in", "records_out", "records_skipped", "bytes_in",
                "bytes_out", "dlq_count", "error", "errors", "node",
            }
            assert row["run_id"] == result.run_id
            assert row["pipeline"] == "my-manual"
            assert row["status"] == "queued"
            assert row["finished_at"] is None
            assert row["records_in"] == 0
            assert row["error"] is None
            assert row["node"] is None
        finally:
            ctrl.stop()

    def test_get_run_returns_queued_run(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = []
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        app = _make_api_app(ctrl, db)
        try:
            client = TestClient(app)
            result = ctrl.trigger_run("my-manual")
            resp = client.get(f"/api/runs/{result.run_id}")
            assert resp.status_code == 200  # queued-view fallback, not 404
            data = resp.json()
            assert data["run_id"] == result.run_id
            assert data["status"] == "queued"
            assert data["finished_at"] is None
        finally:
            ctrl.stop()

    def test_pipelines_list_carries_queued_run(self, db):
        wp = MagicMock()
        wp.healthy_workers.return_value = []
        ctrl = _make_controller(db, wp)
        _register_manual(ctrl)
        app = _make_api_app(ctrl, db)
        try:
            client = TestClient(app)
            assert client.get("/api/pipelines").json()[0]["queued_run"] is None
            result = ctrl.trigger_run("my-manual")
            rows = client.get("/api/pipelines").json()
            queued_run = rows[0]["queued_run"]
            assert queued_run is not None
            assert set(queued_run.keys()) == {"run_id", "requested_at", "expires_at"}
            assert queued_run["run_id"] == result.run_id
        finally:
            ctrl.stop()