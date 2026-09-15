"""Tests for v0.7.0 database features: node_id, dlq_count, pagination, health_check, alert state."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tram.core.context import RunResult, RunStatus
from tram.persistence.db import TramDB

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    d = TramDB(url=f"sqlite:///{tmp_path}/test.db", node_id="node-test")
    yield d
    d.close()


def _run(run_id=None, pipeline="p", status=RunStatus.SUCCESS, dlq_count=0, started_offset_s=0):
    now = datetime.now(UTC) + timedelta(seconds=started_offset_s)
    return RunResult(
        run_id=run_id or str(uuid.uuid4())[:8],
        pipeline_name=pipeline,
        status=status,
        started_at=now,
        finished_at=now,
        records_in=5,
        records_out=4,
        records_skipped=1,
        error=None,
        dlq_count=dlq_count,
    )


# ── health_check ──────────────────────────────────────────────────────────────


def test_health_check_ok(db):
    assert db.health_check() is True


# ── node_id persisted in run_history ─────────────────────────────────────────


def test_node_id_stored_in_run(db):
    db.save_run(_run(run_id="run1"))
    # Fetch raw from SQLAlchemy to verify node_id column
    from sqlalchemy import text
    with db._engine.connect() as conn:
        row = conn.execute(
            text("SELECT node_id FROM run_history WHERE run_id = 'run1'")
        ).mappings().fetchone()
    assert row["node_id"] == "node-test"


def test_node_id_defaults_empty_when_not_set(tmp_path):
    d = TramDB(url=f"sqlite:///{tmp_path}/n.db")  # no node_id
    d.save_run(_run(run_id="x"))
    from sqlalchemy import text
    with d._engine.connect() as conn:
        row = conn.execute(text("SELECT node_id FROM run_history WHERE run_id = 'x'")).mappings().fetchone()
    assert row["node_id"] == ""
    d.close()


def test_explicit_run_node_id_overrides_db_default(db):
    run = _run(run_id="worker-run")
    run.node_id = "tram-worker-2"
    db.save_run(run)
    from sqlalchemy import text
    with db._engine.connect() as conn:
        row = conn.execute(
            text("SELECT node_id FROM run_history WHERE run_id = 'worker-run'")
        ).mappings().fetchone()
    assert row["node_id"] == "tram-worker-2"


# ── dlq_count persisted and round-tripped ─────────────────────────────────────


def test_dlq_count_persisted(db):
    r = _run(run_id="dlq1", dlq_count=7)
    db.save_run(r)
    runs = db.get_runs()
    assert runs[0].dlq_count == 7


def test_dlq_count_zero_by_default(db):
    db.save_run(_run(run_id="nd1"))
    runs = db.get_runs()
    assert runs[0].dlq_count == 0


# ── get_run by id ─────────────────────────────────────────────────────────────


def test_get_run_by_id(db):
    r = _run(run_id="abc99")
    db.save_run(r)
    fetched = db.get_run("abc99")
    assert fetched is not None
    assert fetched.run_id == "abc99"
    assert fetched.pipeline_name == "p"


def test_get_run_by_id_not_found(db):
    assert db.get_run("nonexistent") is None


# ── pagination: offset ────────────────────────────────────────────────────────


def test_get_runs_offset(db):
    for i in range(5):
        db.save_run(_run(run_id=f"r{i}", pipeline="p"))

    page1 = db.get_runs(limit=3, offset=0)
    page2 = db.get_runs(limit=3, offset=3)

    assert len(page1) == 3
    assert len(page2) == 2  # only 5 total

    all_ids = {r.run_id for r in page1} | {r.run_id for r in page2}
    assert len(all_ids) == 5  # no overlap


def test_get_runs_offset_beyond_end(db):
    db.save_run(_run(run_id="only1"))
    result = db.get_runs(offset=100)
    assert result == []


# ── from_dt filter ────────────────────────────────────────────────────────────


def test_get_runs_from_dt(db):
    old = _run(run_id="old1", started_offset_s=-3600)   # 1 hour ago
    recent = _run(run_id="new1", started_offset_s=0)    # now
    db.save_run(old)
    db.save_run(recent)

    cutoff = datetime.now(UTC) - timedelta(minutes=30)
    results = db.get_runs(from_dt=cutoff)
    ids = [r.run_id for r in results]
    assert "new1" in ids
    assert "old1" not in ids


# ── alert_state (existing v0.6.0 tests, moved here as regression) ─────────────


def test_alert_cooldown_none_initially(db):
    result = db.get_alert_cooldown("p", "rule1")
    assert result is None


def test_alert_cooldown_set_and_get(db):
    now = datetime.now(UTC)
    db.set_alert_cooldown("p", "rule1", now)
    fetched = db.get_alert_cooldown("p", "rule1")
    assert fetched is not None
    # Microsecond precision may differ after ISO round-trip
    assert abs((fetched - now).total_seconds()) < 1


def test_alert_cooldown_upsert(db):
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 1, 2, tzinfo=UTC)
    db.set_alert_cooldown("p", "r", t1)
    db.set_alert_cooldown("p", "r", t2)  # should overwrite
    assert db.get_alert_cooldown("p", "r").date() == t2.date()


# ── duplicate run_id silently ignored ─────────────────────────────────────────


def test_duplicate_run_id_ignored(db):
    r = _run(run_id="dup1")
    db.save_run(r)
    db.save_run(r)  # second insert → should not raise
    runs = db.get_runs()
    assert len([x for x in runs if x.run_id == "dup1"]) == 1


# ── idempotent schema migration ───────────────────────────────────────────────


def test_reopen_existing_db_migrates_cleanly(tmp_path):
    """Opening an existing DB a second time should not raise (migrations are idempotent)."""
    url = f"sqlite:///{tmp_path}/migrate.db"
    d1 = TramDB(url=url)
    d1.save_run(_run(run_id="m1"))
    d1.close()

    d2 = TramDB(url=url)
    runs = d2.get_runs()
    d2.close()
    assert any(r.run_id == "m1" for r in runs)


def test_save_and_get_active_broadcast_placement(db):
    db.save_broadcast_placement(
        placement_group_id="pg1",
        pipeline_name="pipe-a",
        slots=[{
            "worker_index": 0,
            "worker_url": "http://w0:8766",
            "worker_id": "w0",
            "run_id_prefix": "pg1-w0",
            "current_run_id": "pg1-w0",
            "status": "running",
        }],
        target_count="all",
        status="running",
    )

    placements = db.get_active_broadcast_placements()
    assert len(placements) == 1
    assert placements[0]["placement_group_id"] == "pg1"
    assert placements[0]["slots"][0]["current_run_id"] == "pg1-w0"


def test_update_slot_run_id(db):
    db.save_broadcast_placement(
        placement_group_id="pg1",
        pipeline_name="pipe-a",
        slots=[{
            "worker_index": 0,
            "worker_url": "http://w0:8766",
            "worker_id": "w0",
            "run_id_prefix": "pg1-w0",
            "current_run_id": "pg1-w0",
            "status": "running",
        }],
        target_count="all",
        status="running",
    )

    assert db.update_slot_run_id("pg1", 0, "pg1-w0-r1", restart_count=1) == 1
    placement = db.get_active_broadcast_placements()[0]
    assert placement["slots"][0]["current_run_id"] == "pg1-w0-r1"
    assert placement["slots"][0]["restart_count"] == 1


def test_update_slot_run_id_lost_race_with_stale_expected(db):
    """A writer whose expected run id no longer matches detects the lost race.

    The update is keyed on the full slot identity (placement_group_id +
    worker_index) plus the expected current_run_id, so once the slot advances
    past a writer's expectation, the stale write is rejected with 0 rows and
    the newer run id is preserved (review A13 / plan D.1).
    """
    db.save_broadcast_placement(
        placement_group_id="pg1",
        pipeline_name="pipe-a",
        slots=[{
            "worker_index": 0,
            "worker_url": "http://w0:8766",
            "worker_id": "w0",
            "run_id_prefix": "pg1-w0",
            "current_run_id": "pg1-w0",
            "status": "running",
        }],
        target_count="all",
        status="running",
    )

    # Writer A commits its run id, expecting the recorded value.
    assert db.update_slot_run_id(
        "pg1", 0, "pg1-w0-r1", expected_run_id="pg1-w0"
    ) == 1

    # Writer B based its decision on the same stale recorded value; the slot
    # already advanced, so the update loses the race and changes nothing.
    assert db.update_slot_run_id(
        "pg1", 0, "pg1-w0-r2", expected_run_id="pg1-w0"
    ) == 0
    placement = db.get_active_broadcast_placements()[0]
    assert placement["slots"][0]["current_run_id"] == "pg1-w0-r1"

    # A re-read with the current value lets the writer proceed.
    assert db.update_slot_run_id(
        "pg1", 0, "pg1-w0-r2", expected_run_id="pg1-w0-r1"
    ) == 1
    placement = db.get_active_broadcast_placements()[0]
    assert placement["slots"][0]["current_run_id"] == "pg1-w0-r2"


def test_update_slot_run_id_two_writers_lost_race(tmp_path):
    """Two DB writers sharing one placement: the stale one detects the race.

    Models a redispatch (writer 1) racing a late stats payload (writer 2):
    writer 2's expected run id is stale by commit time, so its update returns
    0 rows and the redispatch's run id survives.
    """
    url = f"sqlite:///{tmp_path}/writers.db"
    writer_a = TramDB(url=url)
    writer_b = TramDB(url=url)
    writer_a.save_broadcast_placement(
        placement_group_id="pg1",
        pipeline_name="pipe-a",
        slots=[{
            "worker_index": 0,
            "worker_url": "http://w0:8766",
            "worker_id": "w0",
            "run_id_prefix": "pg1-w0",
            "current_run_id": "pg1-w0",
            "status": "running",
        }],
        target_count="all",
        status="running",
    )

    # Both writers observe the same recorded run id...
    stale_expected = "pg1-w0"
    # ...writer 1 commits first.
    assert writer_a.update_slot_run_id(
        "pg1", 0, "pg1-w0-r1", expected_run_id=stale_expected
    ) == 1
    # Writer 2's update is based on the same stale observation and loses.
    assert writer_b.update_slot_run_id(
        "pg1", 0, "pg1-w0-r2", expected_run_id=stale_expected
    ) == 0

    placement = writer_a.get_active_broadcast_placements()[0]
    assert placement["slots"][0]["current_run_id"] == "pg1-w0-r1"
    writer_a.close()
    writer_b.close()


def test_update_slot_run_id_isolated_to_intended_slot(db):
    """Updating one slot never rewrites or loses sibling slots of the placement."""
    db.save_broadcast_placement(
        placement_group_id="pg1",
        pipeline_name="pipe-a",
        slots=[
            {
                "worker_index": 0,
                "worker_url": "http://w0:8766",
                "worker_id": "w0",
                "run_id_prefix": "pg1-w0",
                "current_run_id": "pg1-w0",
                "status": "running",
            },
            {
                "worker_index": 1,
                "worker_url": "http://w1:8766",
                "worker_id": "w1",
                "run_id_prefix": "pg1-w1",
                "current_run_id": "pg1-w1",
                "status": "running",
            },
        ],
        target_count="all",
        status="running",
    )

    assert db.update_slot_run_id("pg1", 0, "pg1-w0-r1", expected_run_id="pg1-w0") == 1
    placement = db.get_active_broadcast_placements()[0]
    slots = placement["slots"]
    assert slots[0]["current_run_id"] == "pg1-w0-r1"
    assert slots[0]["status"] == "running"
    # Sibling slot untouched.
    assert slots[1]["current_run_id"] == "pg1-w1"
    assert slots[1]["worker_url"] == "http://w1:8766"


def test_update_slot_run_id_stopped_placement_is_noop(db):
    """A stopped placement is never resurrected by a slot run-id update."""
    db.save_broadcast_placement(
        placement_group_id="pg1",
        pipeline_name="pipe-a",
        slots=[{
            "worker_index": 0,
            "worker_url": "http://w0:8766",
            "worker_id": "w0",
            "run_id_prefix": "pg1-w0",
            "current_run_id": "pg1-w0",
            "status": "running",
        }],
        target_count="all",
        status="running",
    )
    db.update_broadcast_placement_status("pg1", "stopped", slots=[])

    assert db.update_slot_run_id("pg1", 0, "pg1-w0-r1") == 0
    assert db.get_active_broadcast_placements() == []


def test_update_slot_run_id_missing_placement_or_slot_returns_zero(db):
    """Unknown placements/slots report a lost race (0 rows)."""
    assert db.update_slot_run_id("nope", 0, "pg1-w0-r1") == 0

    db.save_broadcast_placement(
        placement_group_id="pg1",
        pipeline_name="pipe-a",
        slots=[{
            "worker_index": 0,
            "worker_url": "http://w0:8766",
            "worker_id": "w0",
            "run_id_prefix": "pg1-w0",
            "current_run_id": "pg1-w0",
            "status": "running",
        }],
        target_count="all",
        status="running",
    )
    assert db.update_slot_run_id("pg1", 5, "pg1-w5-r1") == 0


def test_stopped_broadcast_placement_not_returned_as_active(db):
    db.save_broadcast_placement(
        placement_group_id="pg1",
        pipeline_name="pipe-a",
        slots=[],
        target_count="all",
        status="running",
    )
    db.update_broadcast_placement_status("pg1", "stopped", slots=[])
    assert db.get_active_broadcast_placements() == []
