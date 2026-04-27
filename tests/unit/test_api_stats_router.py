"""Tests for the stats router — in-memory fallback path."""
from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.stats import router
from tram.core.context import RunResult, RunStatus


def _make_run(status=RunStatus.SUCCESS, records_in=10, records_out=8, age_seconds=30):
    """Build a minimal RunResult with finished_at relative to now."""
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


def _make_state(name="p1", status="running", runs=None):
    state = MagicMock()
    state.config.name = name
    state.status = status
    state.run_history = deque(runs or [])
    return state


def _make_app(states=None, db=None):
    app = FastAPI()
    app.include_router(router)

    mock_manager = MagicMock()
    mock_manager.list_all.return_value = states or []
    app.state.manager = mock_manager
    app.state.db = db
    return app


class TestStatsInMemory:
    def test_empty_returns_zeros(self):
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pipelines_total"] == 0
        assert data["runs_today"] == 0
        assert data["records_in_last_15m"] == 0

    def test_pipeline_counts_by_status(self):
        states = [
            _make_state("p1", "running"),
            _make_state("p2", "running"),
            _make_state("p3", "scheduled"),
            _make_state("p4", "error"),
            _make_state("p5", "stopped"),
        ]
        app = _make_app(states=states)
        client = TestClient(app)
        data = client.get("/api/stats").json()
        assert data["pipelines_total"] == 5
        assert data["pipelines_running"] == 2
        assert data["pipelines_scheduled"] == 1
        assert data["pipelines_error"] == 1

    def test_recent_runs_counted(self):
        run = _make_run(age_seconds=30)  # 30 seconds ago, within 15m and 1h
        state = _make_state("p1", runs=[run])
        app = _make_app(states=[state])
        client = TestClient(app)
        data = client.get("/api/stats").json()
        assert data["runs_last_hour"] == 1
        assert data["runs_today"] == 1
        assert data["records_in_last_15m"] == 10
        assert data["records_out_last_15m"] == 8

    def test_old_runs_not_counted_in_15m(self):
        old_run = _make_run(age_seconds=3600)  # 1 hour ago, outside 15m window
        state = _make_state("p1", runs=[old_run])
        app = _make_app(states=[state])
        client = TestClient(app)
        data = client.get("/api/stats").json()
        assert data["records_in_last_15m"] == 0

    def test_per_pipeline_list_included(self):
        run = _make_run(age_seconds=30)
        state = _make_state("mypipe", runs=[run])
        app = _make_app(states=[state])
        client = TestClient(app)
        data = client.get("/api/stats").json()
        assert len(data["per_pipeline"]) == 1
        pp = data["per_pipeline"][0]
        assert pp["name"] == "mypipe"
        assert pp["runs_last_hour"] == 1

    def test_sparkline_has_12_buckets(self):
        app = _make_app()
        client = TestClient(app)
        data = client.get("/api/stats").json()
        assert len(data["sparkline"]) == 12
        assert data["window"]["period"] == "1h"
        assert data["window"]["granularity"] == "5m"
        assert len(data["chart"]["points"]) == 12

    def test_error_runs_counted(self):
        err_run = _make_run(status=RunStatus.FAILED, age_seconds=60)
        state = _make_state("p1", runs=[err_run])
        app = _make_app(states=[state])
        client = TestClient(app)
        data = client.get("/api/stats").json()
        assert data["errors_last_15m"] == 1  # 60s ago is within 15m

    def test_custom_period_and_granularity_change_bucket_count(self):
        run = _make_run(age_seconds=30)
        state = _make_state("p1", runs=[run])
        app = _make_app(states=[state])
        client = TestClient(app)
        data = client.get("/api/stats?period=6h&granularity=15m").json()
        assert data["window"]["period"] == "6h"
        assert data["window"]["granularity"] == "15m"
        assert data["window"]["bucket_count"] == 24
        assert len(data["sparkline"]) == 24
        assert len(data["chart"]["points"]) == 24
        assert data["chart"]["points"][-1]["bucket_start"] is not None
