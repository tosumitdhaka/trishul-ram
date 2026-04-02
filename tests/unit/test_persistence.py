"""Tests for SQLAlchemy-backed persistence layer (v0.7.0)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tram.core.context import RunResult, RunStatus
from tram.persistence.db import TramDB


def _make_result(pipeline="test", status=RunStatus.SUCCESS, run_id="abc123", dlq_count=0):
    now = datetime.now(UTC)
    return RunResult(
        run_id=run_id,
        pipeline_name=pipeline,
        status=status,
        started_at=now,
        finished_at=now,
        records_in=10,
        records_out=8,
        records_skipped=2,
        error=None,
        dlq_count=dlq_count,
    )


@pytest.fixture
def db(tmp_path):
    """Create a TramDB backed by a temp SQLite file."""
    p = tmp_path / "test.db"
    d = TramDB(url=f"sqlite:///{p}")
    yield d
    d.close()


# ── Run history ────────────────────────────────────────────────────────────


def test_save_and_get_run(db):
    result = _make_result()
    db.save_run(result)

    runs = db.get_runs()
    assert len(runs) == 1
    assert runs[0].run_id == result.run_id
    assert runs[0].pipeline_name == "test"
    assert runs[0].status == RunStatus.SUCCESS
    assert runs[0].records_in == 10


def test_get_runs_filter_by_pipeline(db):
    db.save_run(_make_result(pipeline="a", run_id="r1"))
    db.save_run(_make_result(pipeline="b", run_id="r2"))

    a_runs = db.get_runs(pipeline_name="a")
    assert len(a_runs) == 1
    assert a_runs[0].pipeline_name == "a"


def test_get_runs_filter_by_status(db):
    db.save_run(_make_result(status=RunStatus.SUCCESS, run_id="r1"))
    db.save_run(_make_result(status=RunStatus.FAILED, run_id="r2"))

    failed = db.get_runs(status="failed")
    assert len(failed) == 1
    assert failed[0].status == RunStatus.FAILED


def test_get_runs_limit(db):
    for i in range(10):
        db.save_run(_make_result(run_id=f"r{i}"))

    runs = db.get_runs(limit=3)
    assert len(runs) == 3


# ── Pipeline versions ──────────────────────────────────────────────────────


def test_save_pipeline_version_increments(db):
    v1 = db.save_pipeline_version("my-pipe", "yaml: v1")
    v2 = db.save_pipeline_version("my-pipe", "yaml: v2")
    assert v2 == v1 + 1


def test_get_pipeline_versions(db):
    db.save_pipeline_version("p", "yaml1")
    db.save_pipeline_version("p", "yaml2")

    versions = db.get_pipeline_versions("p")
    assert len(versions) == 2
    # Latest first
    assert versions[0]["version"] > versions[1]["version"]


def test_get_pipeline_version_content(db):
    db.save_pipeline_version("p", "first-yaml-content")
    db.save_pipeline_version("p", "second-yaml-content")

    content = db.get_pipeline_version("p", 1)
    assert content == "first-yaml-content"


def test_get_latest_version(db):
    db.save_pipeline_version("p", "old")
    db.save_pipeline_version("p", "new")

    latest = db.get_latest_version("p")
    assert latest == "new"


def test_get_pipeline_version_not_found(db):
    with pytest.raises(KeyError):
        db.get_pipeline_version("nonexistent", 99)


def test_only_latest_version_is_active(db):
    db.save_pipeline_version("p", "v1")
    db.save_pipeline_version("p", "v2")

    versions = db.get_pipeline_versions("p")
    active = [v for v in versions if v["is_active"]]
    assert len(active) == 1
    assert active[0]["version"] == 2
