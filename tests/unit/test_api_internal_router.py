"""Unit tests for internal worker-to-manager callbacks."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.internal import router


def _make_app():
    app = FastAPI()
    app.include_router(router)

    mock_controller = MagicMock()
    mock_stats_store = MagicMock()
    app.state.controller = mock_controller
    app.state.stats_store = mock_stats_store
    return app, mock_controller, mock_stats_store


class TestRunCompleteEndpoint:
    def test_calls_on_worker_run_complete(self):
        app, ctrl, _ = _make_app()
        client = TestClient(app)
        started_at = "2026-04-16T09:00:00+00:00"
        finished_at = "2026-04-16T09:05:00+00:00"

        resp = client.post("/api/internal/run-complete", json={
            "run_id": "abc123",
            "pipeline_name": "my-pipe",
            "status": "success",
            "records_in": 100,
            "records_out": 95,
            "bytes_in": 1024,
            "bytes_out": 768,
            "error": None,
            "started_at": started_at,
            "finished_at": finished_at,
        })

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        ctrl.on_worker_run_complete.assert_called_once_with(
            run_id="abc123",
            pipeline_name="my-pipe",
            status="success",
            records_in=100,
            records_out=95,
            records_skipped=0,
            bytes_in=1024,
            bytes_out=768,
            error=None,
            errors=[],
            started_at=datetime.fromisoformat(started_at),
            finished_at=datetime.fromisoformat(finished_at),
        )

    def test_passes_error_string(self):
        app, ctrl, _ = _make_app()
        client = TestClient(app)

        client.post("/api/internal/run-complete", json={
            "run_id": "r2",
            "pipeline_name": "p",
            "status": "error",
            "records_in": 0,
            "records_out": 0,
            "error": "something broke",
        })

        _, kwargs = ctrl.on_worker_run_complete.call_args
        assert kwargs["error"] == "something broke"
        assert kwargs["status"] == "error"

    def test_defaults_records_to_zero(self):
        app, ctrl, _ = _make_app()
        client = TestClient(app)

        # records_in / records_out are optional (default 0)
        client.post("/api/internal/run-complete", json={
            "run_id": "r3",
            "pipeline_name": "p",
            "status": "success",
        })

        _, kwargs = ctrl.on_worker_run_complete.call_args
        assert kwargs["records_in"] == 0
        assert kwargs["records_out"] == 0
        assert kwargs["bytes_in"] == 0
        assert kwargs["bytes_out"] == 0

    def test_defaults_timestamps_to_none(self):
        app, ctrl, _ = _make_app()
        client = TestClient(app)

        client.post("/api/internal/run-complete", json={
            "run_id": "r4",
            "pipeline_name": "p",
            "status": "success",
        })

        _, kwargs = ctrl.on_worker_run_complete.call_args
        assert kwargs["started_at"] is None
        assert kwargs["finished_at"] is None

    def test_not_in_openapi_schema(self):
        app, _, _ = _make_app()
        client = TestClient(app)
        schema = client.get("/openapi.json").json()
        paths = schema.get("paths", {})
        assert "/api/internal/run-complete" not in paths
        assert "/api/internal/pipeline-stats" not in paths


class TestPipelineStatsEndpoint:
    def test_updates_stats_store_for_periodic_report(self):
        app, _, store = _make_app()
        client = TestClient(app)

        resp = client.post("/api/internal/pipeline-stats", json={
            "worker_id": "w0",
            "pipeline_name": "pipe-a",
            "run_id": "run-1",
            "schedule_type": "stream",
            "uptime_seconds": 10.5,
            "timestamp": "2026-04-17T12:00:00+00:00",
            "records_in": 5,
            "records_out": 4,
            "bytes_in": 100,
            "bytes_out": 80,
        })

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        store.update.assert_called_once()
        store.remove.assert_not_called()
        app.state.controller.on_pipeline_stats.assert_called_once()

    def test_removes_stats_store_entry_for_final_report(self):
        app, _, store = _make_app()
        client = TestClient(app)

        resp = client.post("/api/internal/pipeline-stats", json={
            "worker_id": "w0",
            "pipeline_name": "pipe-a",
            "run_id": "run-1",
            "schedule_type": "batch",
            "uptime_seconds": 3.0,
            "timestamp": "2026-04-17T12:00:00+00:00",
            "is_final": True,
        })

        assert resp.status_code == 200
        store.remove.assert_called_once_with("run-1")
        store.update.assert_not_called()
        app.state.controller.on_pipeline_stats.assert_not_called()
