"""Tests for stats router DB path and helper functions."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.stats import (
    _after,
    _bucket_sparkline,
    _db_agg,
    _db_count,
    _db_per_pipeline,
    _db_sparkline,
    _iso,
    _mem_agg,
    router,
)
from tram.core.context import RunResult, RunStatus

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_db_mock(fetchone_val=None, fetchall_val=None):
    """Return a minimal DB mock with a mock engine + connection."""
    db = MagicMock()
    db._engine.dialect.name = "sqlite"

    mock_conn = MagicMock()
    result_mock = MagicMock()
    result_mock.fetchone.return_value = fetchone_val or (0,)
    result_mock.fetchall.return_value = fetchall_val or []
    mock_conn.execute.return_value = result_mock

    @contextmanager
    def _connect():
        yield mock_conn

    db._engine.connect = _connect
    return db, mock_conn


def _make_app_with_db(states=None, db=None):
    app = FastAPI()
    app.include_router(router)
    mock_manager = MagicMock()
    mock_manager.list_all.return_value = states or []
    app.state.manager = mock_manager
    if db is not None:
        app.state.db = db
    return app


def _make_run(status=RunStatus.SUCCESS, records_in=10, records_out=8, age_seconds=30):
    finished = datetime.now(UTC) - timedelta(seconds=age_seconds)
    started = finished - timedelta(seconds=1)
    return RunResult(
        run_id="run-001",
        pipeline_name="p1",
        status=status,
        records_in=records_in,
        records_out=records_out,
        records_skipped=0,
        started_at=started,
        finished_at=finished,
    )


# ── _iso and _after helpers ────────────────────────────────────────────────


class TestIsoHelper:
    def test_returns_isoformat(self):
        dt = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
        result = _iso(dt)
        assert "2026-04-01" in result


class TestAfterHelper:
    def test_none_returns_false(self):
        assert _after(None, datetime.now(UTC)) is False

    def test_recent_datetime_returns_true(self):
        now = datetime.now(UTC)
        dt = now - timedelta(seconds=1)
        assert _after(dt, now - timedelta(minutes=1)) is True

    def test_old_datetime_returns_false(self):
        now = datetime.now(UTC)
        dt = now - timedelta(hours=2)
        assert _after(dt, now - timedelta(hours=1)) is False

    def test_string_isoformat_is_parsed(self):
        now = datetime.now(UTC)
        dt_str = (now - timedelta(seconds=30)).isoformat()
        assert _after(dt_str, now - timedelta(minutes=1)) is True

    def test_invalid_string_returns_false(self):
        assert _after("not-a-date", datetime.now(UTC)) is False

    def test_naive_datetime_gets_utc(self):
        now = datetime.now(UTC)
        naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=30)
        # naive datetime should be treated as UTC
        assert _after(naive, now - timedelta(minutes=1)) is True


# ── DB helper functions ────────────────────────────────────────────────────


class TestDbCount:
    def test_returns_count_from_db(self):
        db, conn = _make_db_mock(fetchone_val=(42,))
        result = _db_count(db, datetime.now(UTC) - timedelta(hours=1))
        assert result == 42

    def test_none_fetchone_returns_zero(self):
        db, conn = _make_db_mock(fetchone_val=None)
        conn.execute.return_value.fetchone.return_value = None
        result = _db_count(db, datetime.now(UTC))
        assert result == 0


class TestDbAgg:
    def test_sqlite_dialect_runs_query(self):
        db, conn = _make_db_mock(fetchone_val=(100, 90, 2, 1.5))
        result = _db_agg(db, datetime.now(UTC) - timedelta(hours=1))
        assert result["records_in"] == 100
        assert result["records_out"] == 90
        assert result["errors"] == 2
        assert result["avg_duration_s"] == 1.5

    def test_postgresql_dialect(self):
        db, conn = _make_db_mock(fetchone_val=(50, 40, 0, None))
        db._engine.dialect.name = "postgresql"
        result = _db_agg(db, datetime.now(UTC) - timedelta(hours=1))
        assert result["records_in"] == 50
        assert result["avg_duration_s"] is None

    def test_mysql_dialect(self):
        db, conn = _make_db_mock(fetchone_val=(10, 9, 1, 2.0))
        db._engine.dialect.name = "mysql"
        result = _db_agg(db, datetime.now(UTC) - timedelta(hours=1))
        assert result["records_in"] == 10

    def test_none_row_returns_zeros(self):
        db, conn = _make_db_mock()
        conn.execute.return_value.fetchone.return_value = None
        result = _db_agg(db, datetime.now(UTC))
        assert result == {"records_in": 0, "records_out": 0, "errors": 0, "avg_duration_s": None}


class TestDbPerPipeline:
    def test_returns_per_pipeline_list(self):
        db, conn = _make_db_mock()
        conn.execute.return_value.fetchall.return_value = [
            ("pipe-a", 5, 100, 90, 1),
        ]
        state = MagicMock()
        state.config.name = "pipe-a"
        state.status = "running"
        result = _db_per_pipeline(db, datetime.now(UTC) - timedelta(hours=1), [state])
        assert len(result) == 1
        assert result[0]["name"] == "pipe-a"
        assert result[0]["runs_last_hour"] == 5

    def test_pipeline_without_db_rows_gets_zeros(self):
        db, conn = _make_db_mock()
        conn.execute.return_value.fetchall.return_value = []
        state = MagicMock()
        state.config.name = "no-runs-pipe"
        state.status = "stopped"
        result = _db_per_pipeline(db, datetime.now(UTC), [state])
        assert result[0]["runs_last_hour"] == 0


class TestDbSparkline:
    def test_returns_12_buckets(self):
        db, conn = _make_db_mock()
        conn.execute.return_value.fetchall.return_value = []
        result = _db_sparkline(db, datetime.now(UTC) - timedelta(hours=1))
        assert len(result) == 12


# ── Bucket sparkline edge cases ────────────────────────────────────────────


class TestBucketSparkline:
    def test_empty_rows_returns_zeros(self):
        result = _bucket_sparkline([], datetime.now(UTC) - timedelta(hours=1), buckets=12, minutes=5)
        assert len(result) == 12
        assert all(b["records_out"] == 0 for b in result)

    def test_recent_records_go_into_last_bucket(self):
        now = datetime.now(UTC)
        recent_ts = (now - timedelta(seconds=30)).isoformat()
        rows = [(recent_ts, 5)]
        result = _bucket_sparkline(rows, now - timedelta(hours=1), buckets=12, minutes=5)
        # Last bucket (index 11) should have the records
        assert result[11]["records_out"] == 5

    def test_naive_ts_handled(self):
        now = datetime.now(UTC)
        naive_ts = (datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=30)).isoformat()
        rows = [(naive_ts, 3)]
        result = _bucket_sparkline(rows, now - timedelta(hours=1), buckets=12, minutes=5)
        # Should not raise, bucket 11 gets the record
        assert sum(b["records_out"] for b in result) == 3

    def test_invalid_ts_is_skipped(self):
        result = _bucket_sparkline([("not-a-date", 10)], datetime.now(UTC), buckets=12, minutes=5)
        assert all(b["records_out"] == 0 for b in result)


# ── mem_agg edge cases ─────────────────────────────────────────────────────


class TestMemAgg:
    def test_duration_calculation_exception_is_swallowed(self):
        """If started_at/finished_at can't be diffed, avg_duration_s is None."""
        run = MagicMock()
        run.finished_at = datetime.now(UTC) - timedelta(seconds=30)
        run.started_at = "invalid-date"  # will cause exception in fromisoformat
        run.records_in = 10
        run.records_out = 8
        run.status = MagicMock()
        run.status.value = "success"
        since = datetime.now(UTC) - timedelta(minutes=1)
        result = _mem_agg([run], since)
        # Should not raise; avg_duration_s is None or 0 depending on exception
        assert "avg_duration_s" in result


# ── Full stats endpoint with DB ────────────────────────────────────────────


class TestStatsWithDB:
    def test_db_path_returns_correct_stats(self):
        db, conn = _make_db_mock()
        # Set up conn.execute to return different results for different queries
        count_result = MagicMock()
        count_result.fetchone.return_value = (5,)
        agg_result = MagicMock()
        agg_result.fetchone.return_value = (100, 90, 2, 1.5)
        per_pipe_result = MagicMock()
        per_pipe_result.fetchall.return_value = []
        sparkline_result = MagicMock()
        sparkline_result.fetchall.return_value = []

        call_count = [0]

        def mock_execute(query, params=None):
            call_count[0] += 1
            n = call_count[0]
            if n <= 2:  # _db_count calls (today and 1h)
                return count_result
            elif n <= 4:  # _db_agg calls (15m and 1h)
                return agg_result
            elif n == 5:  # _db_per_pipeline
                return per_pipe_result
            else:  # _db_sparkline
                return sparkline_result

        conn.execute.side_effect = mock_execute

        app = _make_app_with_db(db=db)
        client = TestClient(app)
        r = client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert "pipelines_total" in data
        assert "runs_today" in data
