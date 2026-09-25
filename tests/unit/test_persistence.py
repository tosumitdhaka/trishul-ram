"""Tests for SQLAlchemy-backed persistence layer (v0.7.0)."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tram.core.context import RunResult, RunStatus
from tram.persistence.db import (
    TramDB,
    _add_column_if_missing,
    _is_duplicate_column_error,
)


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


def test_save_pipeline_version_is_noop_for_identical_active_yaml(db):
    v1 = db.save_pipeline_version("p", "same")
    v2 = db.save_pipeline_version("p", "same")

    versions = db.get_pipeline_versions("p")

    assert v2 == v1
    assert len(versions) == 1
    assert versions[0]["version"] == v1
    assert versions[0]["is_active"] == 1


def test_activate_pipeline_version_marks_existing_row_active(db):
    db.save_pipeline_version("p", "v1")
    db.save_pipeline_version("p", "v2")

    yaml_text = db.activate_pipeline_version("p", 1)
    versions = db.get_pipeline_versions("p")
    active = [v for v in versions if v["is_active"]]

    assert yaml_text == "v1"
    assert len(active) == 1
    assert active[0]["version"] == 1


# ── B8: _add_column_if_missing must swallow only the duplicate-column error ──


class TestAddColumnIfMissing:
    def test_duplicate_column_is_ignored_on_second_migration(self, db):
        """Re-running the migration on an existing column must not raise."""
        with db._engine.begin() as conn:
            # First add succeeds.
            _add_column_if_missing(conn, "sqlite", "run_history", "node_id", "TEXT NOT NULL DEFAULT ''")
            # Second add hits the duplicate-column error — must be swallowed.
            _add_column_if_missing(conn, "sqlite", "run_history", "node_id", "TEXT NOT NULL DEFAULT ''")

    def test_non_duplicate_error_raises_loudly(self, db):
        """A locked/disk-full DB is NOT indistinguishable from 'column exists'."""
        with db._engine.begin() as conn:
            with pytest.raises(Exception):
                # No such table — an unrelated error must propagate (B8).
                _add_column_if_missing(conn, "sqlite", "no_such_table", "col", "TEXT")

    def test_helper_matches_duplicate_column_messages(self):
        assert _is_duplicate_column_error(Exception("duplicate column name: node_id"))
        assert _is_duplicate_column_error(Exception("(1060, 'Duplicate column name x')"))
        assert not _is_duplicate_column_error(Exception("database is locked"))
        assert not _is_duplicate_column_error(Exception("disk I/O error"))


# ── B9: unique (name, version) + version-race retry ─────────────────────────


class TestPipelineVersionUniqueConstraint:
    def test_unique_index_exists(self, db):
        with db._engine.connect() as conn:
            row = conn.execute(text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND name = 'uq_pv_name_version'"
            )).fetchone()
        assert row is not None

    def test_legacy_db_gets_unique_index_via_migration(self, tmp_path):
        """B9 upgrade path: a database created before the constraint still
        receives the unique index when TramDB initialises."""
        import sqlite3

        p = tmp_path / "legacy.db"
        legacy = sqlite3.connect(p)
        legacy.execute("""
            CREATE TABLE pipeline_versions (
                id           TEXT PRIMARY KEY NOT NULL,
                name         TEXT NOT NULL,
                version      INTEGER NOT NULL,
                yaml_content TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                is_active    INTEGER NOT NULL DEFAULT 1
            )
        """)
        legacy.commit()
        legacy.close()

        d = TramDB(url=f"sqlite:///{p}")
        try:
            with d._engine.connect() as conn:
                row = conn.execute(text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND name = 'uq_pv_name_version'"
                )).fetchone()
            assert row is not None
        finally:
            d.close()

    def test_duplicate_name_version_insert_raises(self, db):
        db.save_pipeline_version("p", "v1")
        with db._engine.begin() as conn:
            with pytest.raises(IntegrityError):
                conn.execute(text(
                    "INSERT INTO pipeline_versions (id, name, version, yaml_content, created_at, is_active) "
                    "VALUES (:id, :name, :version, :yaml, :ts, 1)"
                ), {
                    "id": "dup-id",
                    "name": "p",
                    "version": 1,
                    "yaml": "clobber",
                    "ts": datetime.now(UTC).isoformat(),
                })

    def test_version_race_retries_and_succeeds(self, db):
        """A lost (name, version) race is retried with a fresh computation."""
        calls = {"n": 0}

        def _once(name, yaml_content):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("stmt", {}, Exception("UNIQUE constraint failed"))
            return 7

        with patch.object(db, "_save_pipeline_version_once", side_effect=_once):
            assert db.save_pipeline_version("p", "yaml") == 7
        assert calls["n"] == 2

    def test_version_race_exhausted_retries_raise(self, db):
        with patch.object(
            db,
            "_save_pipeline_version_once",
            side_effect=IntegrityError("stmt", {}, Exception("UNIQUE constraint failed")),
        ):
            with pytest.raises(IntegrityError):
                db.save_pipeline_version("p", "yaml")

    def test_legacy_duplicate_rows_deduped_before_unique_index(self, tmp_path):
        """v1.4.7 review (B9 fix): a legacy DB already holding two rows with
        the same (name, version) — the exact corruption the pre-B9 race could
        produce — must not crash TramDB init. The newest row per group
        survives (created_at, id tiebreak) and the unique index is created."""
        import sqlite3

        p = tmp_path / "legacy-dup.db"
        legacy = sqlite3.connect(p)
        legacy.execute("""
            CREATE TABLE pipeline_versions (
                id           TEXT PRIMARY KEY NOT NULL,
                name         TEXT NOT NULL,
                version      INTEGER NOT NULL,
                yaml_content TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                is_active    INTEGER NOT NULL DEFAULT 1
            )
        """)
        legacy.executemany(
            "INSERT INTO pipeline_versions "
            "(id, name, version, yaml_content, created_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("dup-old", "p", 1, "older", "2026-01-01T00:00:00+00:00", 1),
                ("dup-new", "p", 1, "newer", "2026-01-02T00:00:00+00:00", 1),
                ("q-1", "q", 1, "ok", "2026-01-01T00:00:00+00:00", 1),
            ],
        )
        legacy.commit()
        legacy.close()

        d = TramDB(url=f"sqlite:///{p}")
        try:
            with d._engine.connect() as conn:
                row = conn.execute(text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND name = 'uq_pv_name_version'"
                )).fetchone()
                assert row is not None  # init succeeded → index created
                rows = conn.execute(text(
                    "SELECT id, name, version, is_active FROM pipeline_versions "
                    "ORDER BY name"
                )).mappings().fetchall()
                assert [(r["name"], r["version"]) for r in rows] == [("p", 1), ("q", 1)]
                p_row = [r for r in rows if r["name"] == "p"][0]
                assert p_row["id"] == "dup-new"    # newest row survives
                assert p_row["is_active"] == 1     # group was active → survivor stays active
                # the unique constraint now holds: a re-insert of (p, 1) fails
                with pytest.raises(IntegrityError):
                    conn.execute(text(
                        "INSERT INTO pipeline_versions "
                        "(id, name, version, yaml_content, created_at, is_active) "
                        "VALUES ('dup-again', 'p', 1, 'x', '2026-01-03T00:00:00+00:00', 1)"
                    ))
        finally:
            d.close()

    def test_legacy_duplicate_dedup_is_idempotent(self, tmp_path):
        """Re-initialising TramDB on an already-deduped DB is a no-op — the
        dedup must be idempotent on clean data (no rows deleted, no error)."""
        import sqlite3

        p = tmp_path / "legacy-dup2.db"
        legacy = sqlite3.connect(p)
        legacy.execute("""
            CREATE TABLE pipeline_versions (
                id           TEXT PRIMARY KEY NOT NULL,
                name         TEXT NOT NULL,
                version      INTEGER NOT NULL,
                yaml_content TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                is_active    INTEGER NOT NULL DEFAULT 1
            )
        """)
        legacy.executemany(
            "INSERT INTO pipeline_versions "
            "(id, name, version, yaml_content, created_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("a1", "p", 1, "older", "2026-01-01T00:00:00+00:00", 1),
                ("a2", "p", 1, "newer", "2026-01-02T00:00:00+00:00", 1),
                ("b1", "p", 2, "v2", "2026-01-03T00:00:00+00:00", 0),
            ],
        )
        legacy.commit()
        legacy.close()

        for _ in range(2):  # two TramDB inits on the same file
            d = TramDB(url=f"sqlite:///{p}")
            with d._engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT id, name, version, is_active FROM pipeline_versions "
                    "ORDER BY name, version"
                )).mappings().fetchall()
                assert [(r["name"], r["version"]) for r in rows] == [("p", 1), ("p", 2)]
                assert [r["id"] for r in rows] == ["a2", "b1"]  # newest per group
            d.close()
