"""Tests for health, runs, and metrics router endpoints."""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.agent.stats_store import StatsStore
from tram.api.routers.health import router as health_router
from tram.api.routers.internal import PipelineStatsPayload
from tram.api.routers.metrics_router import router as metrics_router
from tram.api.routers.runs import router as runs_router

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_health_app(scheduler_running: bool = True, db=None,
                     started_at=None, worker_pool=None, config=None):
    app = FastAPI()
    app.include_router(health_router)
    app.state.manager = MagicMock()
    app.state.manager.list_all.return_value = [MagicMock(), MagicMock()]
    mock_scheduler = MagicMock()
    mock_scheduler._running = scheduler_running
    app.state.scheduler = mock_scheduler
    if db is not None:
        app.state.db = db
    if started_at is not None:
        app.state.started_at = started_at
    if worker_pool is not None:
        app.state.worker_pool = worker_pool
    if config is not None:
        app.state.config = config
    app.state.controller = MagicMock()
    app.state.controller.get_active_broadcast_placements.return_value = []
    app.state.stats_store = StatsStore(interval=30)
    return app


def _make_runs_app():
    app = FastAPI()
    app.include_router(runs_router)
    app.state.manager = MagicMock()
    app.state.manager.get_runs.return_value = []
    mock_scheduler = MagicMock()
    mock_scheduler.get_status.return_value = {"jobs": [], "streams": []}
    app.state.scheduler = mock_scheduler
    return app


def _make_metrics_app():
    app = FastAPI()
    app.include_router(metrics_router)
    return app


def _run_result_mock(run_id="abc123", pipeline="my-pipe", status="success"):
    r = MagicMock()
    r.to_dict.return_value = {
        "run_id": run_id, "pipeline": pipeline, "status": status,
        "records_in": 10, "records_out": 10, "finished_at": "2026-04-01T00:00:00Z"
    }
    return r


# ── Health router ──────────────────────────────────────────────────────────


class TestLiveness:
    def test_returns_200_ok(self):
        app = _make_health_app()
        client = TestClient(app)
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestReadiness:
    def test_ready_returns_200(self):
        app = _make_health_app()
        client = TestClient(app)
        r = client.get("/api/ready")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready"
        assert data["pipelines_loaded"] == 2

    def test_stopped_scheduler_returns_503(self):
        app = _make_health_app(scheduler_running=False)
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/ready")
        assert r.status_code == 503

    def test_db_unreachable_returns_503(self):
        db = MagicMock()
        db.health_check.return_value = False
        app = _make_health_app(db=db)
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/ready")
        assert r.status_code == 503

    def test_db_ok_passes(self):
        db = MagicMock()
        db.health_check.return_value = True
        db._engine.dialect.name = "sqlite"
        db._engine.url = MagicMock()
        db._engine.url.__str__ = lambda _: "sqlite:////data/tram.db"
        app = _make_health_app(db=db)
        client = TestClient(app)
        r = client.get("/api/ready")
        assert r.status_code == 200
        assert r.json()["db"] == "ok"
        assert r.json()["db_engine"] == "sqlite"

    def test_uptime_shown_when_started_at_set(self):
        started_at = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
        app = _make_health_app(started_at=started_at)
        client = TestClient(app)
        r = client.get("/api/ready")
        assert r.status_code == 200
        assert r.json()["uptime"] is not None

    def test_worker_pool_shows_cluster_info(self):
        wp = MagicMock()
        wp.healthy_workers.return_value = [MagicMock(), MagicMock()]
        wp._workers = [MagicMock(), MagicMock(), MagicMock()]
        wp.status.return_value = []
        config = MagicMock()
        config.tram_mode = "manager"
        app = _make_health_app(worker_pool=wp, config=config)
        client = TestClient(app)
        r = client.get("/api/ready")
        assert r.status_code == 200
        assert "workers" in r.json()["cluster"]

    def test_no_db_defaults_sqlite(self):
        app = _make_health_app()
        client = TestClient(app)
        r = client.get("/api/ready")
        assert r.status_code == 200
        assert r.json()["db_engine"] == "sqlite"


class TestMeta:
    def test_returns_version_and_python(self):
        app = _make_health_app()
        client = TestClient(app)
        r = client.get("/api/meta")
        assert r.status_code == 200
        data = r.json()
        assert "version" in data
        assert "python_version" in data
        assert "build_time" in data


class TestPlugins:
    def test_returns_plugin_categories(self):
        app = _make_health_app()
        # Import plugins to populate registry
        import tram.connectors  # noqa: F401
        import tram.serializers  # noqa: F401
        import tram.transforms  # noqa: F401
        client = TestClient(app)
        r = client.get("/api/plugins")
        assert r.status_code == 200
        assert "sources" in r.json()


class TestClusterNodes:
    def test_no_worker_pool_returns_standalone(self):
        app = _make_health_app()
        client = TestClient(app)
        r = client.get("/api/cluster/nodes")
        assert r.status_code == 200
        assert r.json()["workers"] == []

    def test_with_worker_pool_returns_worker_status(self):
        wp = MagicMock()
        wp.status.return_value = [{"url": "http://w1:8766", "active_runs": 0}]
        config = MagicMock()
        config.tram_mode = "manager"
        app = _make_health_app(worker_pool=wp, config=config)
        client = TestClient(app)
        r = client.get("/api/cluster/nodes")
        assert r.status_code == 200
        assert len(r.json()["workers"]) == 1

    def test_cluster_nodes_uses_current_controller_assignments(self):
        wp = MagicMock()
        wp.status.return_value = [{
            "url": "http://w1:8766",
            "active_runs": 1,
            "assigned_pipelines": ["old-pipe"],
        }]
        config = MagicMock()
        config.tram_mode = "manager"
        app = _make_health_app(worker_pool=wp, config=config)
        app.state.controller.get_active_broadcast_placements.return_value = [{
            "pipeline_name": "stream-a",
            "slots": [{"worker_url": "http://w1:8766"}],
        }]
        app.state.controller.get_active_batch_runs.return_value = [{
            "pipeline_name": "batch-b",
            "worker_url": "http://w1:8766",
        }]
        client = TestClient(app)

        r = client.get("/api/cluster/nodes")

        assert r.status_code == 200
        assert r.json()["workers"][0]["assigned_pipelines"] == ["batch-b", "stream-a"]


class TestClusterStreams:
    def test_returns_broadcast_streams_with_aggregate_counters(self):
        db = MagicMock()
        db.get_active_broadcast_placements.return_value = [{
            "placement_group_id": "pg1",
            "pipeline_name": "pipe-a",
            "status": "degraded",
            "target_count": "all",
            "started_at": datetime.now(UTC),
            "slots": [
                {
                    "worker_index": 0,
                    "worker_id": "w0",
                    "worker_url": "http://w0:8766",
                    "run_id_prefix": "pg1-w0",
                    "current_run_id": "pg1-w0-r1",
                    "status": "running",
                    "restart_count": 1,
                },
                {
                    "worker_index": 1,
                    "worker_id": "w1",
                    "worker_url": "http://w1:8766",
                    "run_id_prefix": "pg1-w1",
                    "current_run_id": "pg1-w1-r0",
                    "status": "stale",
                    "restart_count": 0,
                },
            ],
        }]
        config = MagicMock()
        config.tram_mode = "manager"
        app = _make_health_app(db=db, config=config)
        app.state.stats_store.update(PipelineStatsPayload(
            worker_id="w0",
            pipeline_name="pipe-a",
            run_id="pg1-w0-r1",
            schedule_type="stream",
            uptime_seconds=5.0,
            timestamp=datetime.now(UTC),
            records_in=20,
            records_out=10,
            bytes_in=200,
            bytes_out=100,
        ))
        app.state.stats_store.update(PipelineStatsPayload(
            worker_id="w1",
            pipeline_name="pipe-a",
            run_id="pg1-w1-r0",
            schedule_type="stream",
            uptime_seconds=5.0,
            timestamp=datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC),
            records_in=99,
            bytes_in=999,
        ))
        client = TestClient(app)

        r = client.get("/api/cluster/streams")

        assert r.status_code == 200
        data = r.json()
        assert data["mode"] == "manager"
        assert len(data["streams"]) == 1
        stream = data["streams"][0]
        assert stream["pipeline_name"] == "pipe-a"
        assert stream["status"] == "degraded"
        assert stream["slot_count"] == 2
        assert stream["active_slots"] == 1
        assert stream["records_in"] == 20
        assert stream["bytes_in_per_sec"] == 40.0
        assert stream["slots"][1]["stats"]["stale"] is True
        assert stream["slots"][1]["stats"]["bytes_in_per_sec"] == 0.0

    def test_includes_non_broadcast_active_streams_from_stats_store(self):
        config = MagicMock()
        config.tram_mode = "standalone"
        app = _make_health_app(config=config)
        app.state.stats_store.update(PipelineStatsPayload(
            worker_id="w0",
            pipeline_name="pipe-b",
            run_id="run-1",
            schedule_type="stream",
            uptime_seconds=4.0,
            timestamp=datetime.now(UTC),
            records_in=8,
            bytes_in=40,
        ))
        client = TestClient(app)

        r = client.get("/api/cluster/streams")

        assert r.status_code == 200
        data = r.json()
        assert len(data["streams"]) == 1
        stream = data["streams"][0]
        assert stream["pipeline_name"] == "pipe-b"
        assert stream["placement_group_id"] is None
        assert stream["slot_count"] == 1
        assert stream["records_in_per_sec"] == 2.0

    def test_uses_live_worker_streams_when_manager_store_has_not_received_stats_yet(self):
        wp = MagicMock()
        wp.live_streams.return_value = [{
            "worker_url": "http://w0:8766",
            "worker_id": "w0",
            "pipeline_name": "pipe-live",
            "run_id": "run-live",
            "started_at": "2026-04-27T10:00:00+00:00",
            "schedule_type": "stream",
            "uptime_seconds": 5.0,
            "stats": {
                "records_in": 10,
                "records_out": 10,
                "bytes_in": 100,
                "bytes_out": 100,
            },
        }]
        config = MagicMock()
        config.tram_mode = "manager"
        app = _make_health_app(worker_pool=wp, config=config)
        client = TestClient(app)

        r = client.get("/api/cluster/streams")

        assert r.status_code == 200
        data = r.json()
        assert data["streams"][0]["pipeline_name"] == "pipe-live"
        assert data["streams"][0]["active_slots"] == 1
        assert data["streams"][0]["records_out_per_sec"] == 2.0


# ── Runs router ────────────────────────────────────────────────────────────


class TestListRuns:
    def test_empty_returns_empty_list(self):
        app = _make_runs_app()
        client = TestClient(app)
        r = client.get("/api/runs")
        assert r.status_code == 200
        assert r.json() == []

    def test_with_runs_returns_list(self):
        app = _make_runs_app()
        mock_run = _run_result_mock()
        app.state.manager.get_runs.return_value = [mock_run]
        client = TestClient(app)
        r = client.get("/api/runs")
        assert r.status_code == 200
        assert r.json()[0]["run_id"] == "abc123"

    def test_pipeline_filter_passed_to_manager(self):
        app = _make_runs_app()
        client = TestClient(app)
        r = client.get("/api/runs?pipeline=my-pipe")
        assert r.status_code == 200
        call_kwargs = app.state.manager.get_runs.call_args.kwargs
        assert call_kwargs["pipeline_name"] == "my-pipe"

    def test_status_filter_passed_to_manager(self):
        app = _make_runs_app()
        client = TestClient(app)
        r = client.get("/api/runs?status=failed")
        assert r.status_code == 200
        call_kwargs = app.state.manager.get_runs.call_args.kwargs
        assert call_kwargs["status"] == "failed"

    def test_limit_and_offset(self):
        app = _make_runs_app()
        client = TestClient(app)
        r = client.get("/api/runs?limit=5&offset=10")
        assert r.status_code == 200
        call_kwargs = app.state.manager.get_runs.call_args.kwargs
        assert call_kwargs["limit"] == 5
        assert call_kwargs["offset"] == 10

    def test_csv_format_empty(self):
        app = _make_runs_app()
        client = TestClient(app)
        r = client.get("/api/runs?format=csv")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")

    def test_csv_format_with_data(self):
        app = _make_runs_app()
        mock_run = _run_result_mock()
        app.state.manager.get_runs.return_value = [mock_run]
        client = TestClient(app)
        r = client.get("/api/runs?format=csv")
        assert r.status_code == 200
        assert "run_id" in r.text  # CSV header


class TestGetRun:
    def test_existing_run_returns_200(self):
        app = _make_runs_app()
        mock_run = _run_result_mock(run_id="xyz789")
        app.state.manager.get_run.return_value = mock_run
        client = TestClient(app)
        r = client.get("/api/runs/xyz789")
        assert r.status_code == 200
        assert r.json()["run_id"] == "xyz789"

    def test_missing_run_returns_404(self):
        app = _make_runs_app()
        app.state.manager.get_run.return_value = None
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/runs/nonexistent")
        assert r.status_code == 404


class TestDaemonStatus:
    def test_returns_scheduler_status(self):
        app = _make_runs_app()
        app.state.scheduler.get_status.return_value = {"jobs": [{"id": "j1"}], "streams": []}
        client = TestClient(app)
        r = client.get("/api/daemon/status")
        assert r.status_code == 200
        assert "jobs" in r.json()


# ── Metrics router ─────────────────────────────────────────────────────────


class TestMetricsEndpoint:
    def test_prometheus_not_installed_returns_503(self):
        with patch("tram.metrics.registry._PROMETHEUS_AVAILABLE", False):
            app = _make_metrics_app()
            client = TestClient(app, raise_server_exceptions=False)
            r = client.get("/metrics")
        assert r.status_code == 503

    def test_prometheus_installed_returns_metrics(self):
        mock_prom = MagicMock()
        mock_prom.generate_latest.return_value = b"# metrics\n"
        mock_prom.CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"
        with patch("tram.metrics.registry._PROMETHEUS_AVAILABLE", True), \
             patch.dict(sys.modules, {"prometheus_client": mock_prom}):
            app = _make_metrics_app()
            client = TestClient(app)
            r = client.get("/metrics")
        assert r.status_code == 200
        assert b"metrics" in r.content
