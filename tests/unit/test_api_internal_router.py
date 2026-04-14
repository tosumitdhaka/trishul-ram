"""Unit tests for POST /api/internal/run-complete (tram/api/routers/internal.py)."""
from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.internal import router


def _make_app():
    app = FastAPI()
    app.include_router(router)

    mock_controller = MagicMock()
    app.state.controller = mock_controller
    return app, mock_controller


class TestRunCompleteEndpoint:
    def test_calls_on_worker_run_complete(self):
        app, ctrl = _make_app()
        client = TestClient(app)

        resp = client.post("/api/internal/run-complete", json={
            "run_id": "abc123",
            "pipeline_name": "my-pipe",
            "status": "success",
            "records_in": 100,
            "records_out": 95,
            "error": None,
        })

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        ctrl.on_worker_run_complete.assert_called_once_with(
            run_id="abc123",
            pipeline_name="my-pipe",
            status="success",
            records_in=100,
            records_out=95,
            error=None,
        )

    def test_passes_error_string(self):
        app, ctrl = _make_app()
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
        app, ctrl = _make_app()
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

    def test_not_in_openapi_schema(self):
        app, _ = _make_app()
        client = TestClient(app)
        schema = client.get("/openapi.json").json()
        paths = schema.get("paths", {})
        assert "/api/internal/run-complete" not in paths
