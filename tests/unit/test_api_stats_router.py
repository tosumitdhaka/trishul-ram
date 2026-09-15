"""Tests for the stats router — in-memory fallback path."""
from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.agent.stats_store import StatsStore
from tram.api.routers.internal import PipelineStatsPayload
from tram.api.routers.stats import router
from tram.core.context import RunResult, RunStatus


def _make_run(
    status=RunStatus.SUCCESS,
    records_in=10,
    records_out=8,
    bytes_in=1024,
    bytes_out=768,
    age_seconds=30,
):
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
        bytes_in=bytes_in,
        bytes_out=bytes_out,
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

    app.state.manager = MagicMock()
    mock_controller = MagicMock()
    mock_controller.list_all.return_value = states or []
    app.state.controller = mock_controller
    app.state.db = db
    return app


def _make_live_payload(
    run_id="live-1",
    pipeline_name="p1",
    records_in=50,
    records_out=40,
    bytes_in=4096,
    bytes_out=2048,
    error_count=0,
    age_seconds=10,
):
    """Build a fresh, non-stale StatsStore payload (30s interval -> 90s staleness)."""
    return PipelineStatsPayload(
        worker_id="w0",
        pipeline_name=pipeline_name,
        run_id=run_id,
        schedule_type="batch",
        uptime_seconds=age_seconds,
        timestamp=datetime.now(UTC) - timedelta(seconds=age_seconds),
        records_in=records_in,
        records_out=records_out,
        bytes_in=bytes_in,
        bytes_out=bytes_out,
        error_count=error_count,
    )


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
        assert data["bytes_in_last_15m"] == 0
        assert data["bytes_out_last_15m"] == 0
        assert data["chart"]["metric"] == "bytes_processed"
        assert data["chart"]["total"] == 0

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
        assert data["bytes_in_last_15m"] == 1024
        assert data["bytes_out_last_15m"] == 768
        assert data["chart"]["metric"] == "bytes_processed"
        assert data["chart"]["total"] == 1792
        assert data["chart"]["points"][-1]["bytes_processed"] == 1792
        assert data["sparkline"][-1]["records_out"] == 8

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
        assert all("bytes_processed" in point for point in data["chart"]["points"])

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


class TestLiveStatsMerge:
    """D.4: StatsStore live in-flight runs merged into cards, chart, and rows."""

    def _app_with_store(self, states=None, store=None):
        app = _make_app(states=states)
        app.state.stats_store = store
        return TestClient(app)

    def test_live_payload_added_to_15m_cards(self):
        store = StatsStore(interval=30)
        store.update(_make_live_payload(
            run_id="live-1", pipeline_name="p1",
            records_in=50, records_out=40, bytes_in=4096, bytes_out=2048,
        ))
        state = _make_state("p1", "running", runs=[])
        client = self._app_with_store(states=[state], store=store)
        data = client.get("/api/stats").json()
        assert data["records_in_last_15m"] == 50
        assert data["records_out_last_15m"] == 40
        assert data["bytes_in_last_15m"] == 4096
        assert data["bytes_out_last_15m"] == 2048

    def test_live_payload_added_to_current_chart_bucket(self):
        store = StatsStore(interval=30)
        store.update(_make_live_payload(
            run_id="live-1", records_in=0, records_out=10, bytes_in=100, bytes_out=200,
        ))
        state = _make_state("p1", "running", runs=[])
        client = self._app_with_store(states=[state], store=store)
        data = client.get("/api/stats").json()
        assert data["chart"]["total"] == 300
        assert data["chart"]["points"][-1]["records_out"] == 10
        assert data["chart"]["points"][-1]["bytes_processed"] == 300
        assert data["sparkline"][-1]["records_out"] == 10

    def test_live_payload_merged_into_per_pipeline_row(self):
        store = StatsStore(interval=30)
        run = _make_run(age_seconds=30)  # completed history: 10 in / 8 out
        state = _make_state("p1", "running", runs=[run])
        store.update(_make_live_payload(
            run_id="live-1", pipeline_name="p1", records_in=50, records_out=40, error_count=2,
        ))
        client = self._app_with_store(states=[state], store=store)
        data = client.get("/api/stats").json()
        row = next(r for r in data["per_pipeline"] if r["name"] == "p1")
        assert row["runs_last_hour"] == 2  # completed run + live run
        assert row["records_in"] == 60
        assert row["records_out"] == 48
        assert row["errors"] == 2

    def test_live_only_pipeline_gets_row(self):
        store = StatsStore(interval=30)
        state = _make_state("p1", "running", runs=[])
        store.update(_make_live_payload(
            run_id="live-1", pipeline_name="p1", records_in=7, records_out=7,
        ))
        client = self._app_with_store(states=[state], store=store)
        data = client.get("/api/stats").json()
        rows = {r["name"]: r for r in data["per_pipeline"]}
        assert rows["p1"]["runs_last_hour"] == 1
        assert rows["p1"]["records_in"] == 7
        assert rows["p1"]["records_out"] == 7

    def test_completion_boundary_drops_late_live_payload(self):
        """A non-final snapshot for a run already in run history must be
        dropped — the final recorded numbers own the dashboard."""
        store = StatsStore(interval=30)
        run = _make_run(age_seconds=30)  # run_id "run-001"
        state = _make_state("p1", "running", runs=[run])
        # Phantom: same run_id as the completed run, arriving late with inflated
        # counters that must not override the final numbers.
        store.update(_make_live_payload(
            run_id="run-001", pipeline_name="p1", records_in=9999, records_out=9999,
        ))
        client = self._app_with_store(states=[state], store=store)
        data = client.get("/api/stats").json()
        assert data["records_in_last_15m"] == 10   # completed run only
        assert data["records_out_last_15m"] == 8
        assert data["chart"]["total"] == 1792      # 1024 + 768 from history

    def test_is_final_live_entry_is_dropped(self):
        store = StatsStore(interval=30)
        payload = _make_live_payload(run_id="live-1", records_in=50)
        payload.is_final = True
        store.update(payload)
        state = _make_state("p1", "running", runs=[])
        client = self._app_with_store(states=[state], store=store)
        data = client.get("/api/stats").json()
        assert data["records_in_last_15m"] == 0

    def test_stale_live_entry_is_ignored(self):
        store = StatsStore(interval=30)
        store.update(_make_live_payload(run_id="live-1", records_in=50, age_seconds=300))
        state = _make_state("p1", "running", runs=[])
        client = self._app_with_store(states=[state], store=store)
        data = client.get("/api/stats").json()
        assert data["records_in_last_15m"] == 0
