"""Unit tests for internal worker-to-manager callbacks."""
from __future__ import annotations

import types
from datetime import datetime
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.internal import router
from tram.persistence.db import TramDB


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
            "worker_id": "worker-2",
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
            worker_id="worker-2",
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
            "worker_id": "w0",
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
            "worker_id": "w0",
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
            "worker_id": "w0",
            "status": "success",
        })

        _, kwargs = ctrl.on_worker_run_complete.call_args
        assert kwargs["worker_id"] == "w0"
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


# ── Transform-state endpoints (F.1 §3.2b) ───────────────────────────────────


class TestTransformStateEndpoints:
    def _make_app(self, tmp_path, enabled=True):
        app = FastAPI()
        app.include_router(router)
        app.state.controller = MagicMock()
        app.state.stats_store = MagicMock()
        app.state.db = TramDB(url=f"sqlite:///{tmp_path}/internal.db")
        app.state.config = types.SimpleNamespace(stateful_transforms=enabled)
        return TestClient(app)

    def test_put_then_get_roundtrip(self, tmp_path):
        client = self._make_app(tmp_path)
        resp = client.put("/api/internal/transform-state/pipe-a", json={
            "state": {"counter_delta:0": {"k": {"v": 42}}},
            "config_sha256": "abc123",
            "run_id": "r1",
        })
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        resp = client.get("/api/internal/transform-state/pipe-a")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == {"counter_delta:0": {"k": {"v": 42}}}
        assert body["config_sha256"] == "abc123"
        # run_id lands in the audit column
        assert client.app.state.db.load_transform_state("pipe-a")["updated_by"] == "r1"

    def test_get_missing_returns_404(self, tmp_path):
        client = self._make_app(tmp_path)
        assert client.get("/api/internal/transform-state/nope").status_code == 404

    def test_flag_off_404s_both_endpoints(self, tmp_path):
        client = self._make_app(tmp_path, enabled=False)
        assert client.get("/api/internal/transform-state/pipe-a").status_code == 404
        assert client.put("/api/internal/transform-state/pipe-a", json={
            "state": {}, "config_sha256": "",
        }).status_code == 404

    def test_put_oversized_state_413(self, tmp_path, monkeypatch):
        """A state blob larger than TRAM_STATE_MAX_BYTES is rejected with 413."""
        monkeypatch.setenv("TRAM_STATE_MAX_BYTES", "100")
        client = self._make_app(tmp_path)
        resp = client.put("/api/internal/transform-state/pipe-a", json={
            "state": {"counter_delta:0": {"k" * 200: {"v": "x" * 500}}},
            "config_sha256": "abc",
        })
        assert resp.status_code == 413
        # Nothing was saved.
        assert client.get("/api/internal/transform-state/pipe-a").status_code == 404

    def test_put_oversized_state_413_config_source(self, tmp_path):
        """The cap also reads from app.state.config.state_max_bytes (the real app)."""
        client = self._make_app(tmp_path)
        client.app.state.config = types.SimpleNamespace(
            stateful_transforms=True, state_max_bytes=64
        )
        resp = client.put("/api/internal/transform-state/pipe-a", json={
            "state": {"counter_delta:0": {"k" * 100: {"v": "x" * 100}}},
        })
        assert resp.status_code == 413

    def test_put_missing_body_422(self, tmp_path):
        """A PUT with no JSON body is rejected at validation (422)."""
        client = self._make_app(tmp_path)
        assert client.put("/api/internal/transform-state/pipe-a", content=b"").status_code == 422

    def test_put_empty_state_ok(self, tmp_path):
        """An empty-but-present state dict passes the cap."""
        client = self._make_app(tmp_path)
        resp = client.put("/api/internal/transform-state/pipe-a", json={"state": {}})
        assert resp.status_code == 200

    def test_not_in_openapi_schema(self, tmp_path):
        client = self._make_app(tmp_path)
        paths = client.get("/openapi.json").json().get("paths", {})
        assert "/api/internal/transform-state/{pipeline}" not in paths


# ── Processed-files endpoints (GH #54) ──────────────────────────────────────


class TestProcessedFilesEndpoints:
    def _make_app(self, tmp_path):
        app = FastAPI()
        app.include_router(router)
        app.state.controller = MagicMock()
        app.state.stats_store = MagicMock()
        app.state.db = TramDB(url=f"sqlite:///{tmp_path}/internal.db")
        return TestClient(app)

    @staticmethod
    def _payload(pipeline_name, files):
        return {
            "pipeline_name": pipeline_name,
            "files": [{"source_key": sk, "filepath": fp} for sk, fp in files],
        }

    def test_mark_then_check_roundtrip(self, tmp_path):
        """mark → check round-trip through the manager-side tracker DB."""
        client = self._make_app(tmp_path)
        body = self._payload("pipe-a", [("local:/in", "/in/a.json")])

        resp = client.post("/api/internal/processed-files/check", json=body)
        assert resp.status_code == 200
        assert resp.json() == {"processed": [False]}

        resp = client.post("/api/internal/processed-files/mark", json=body)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        resp = client.post("/api/internal/processed-files/check", json=body)
        assert resp.status_code == 200
        assert resp.json() == {"processed": [True]}

    def test_batch_check_list_in_list_out(self, tmp_path):
        """A multi-file check returns one bool per file, aligned with input order."""
        client = self._make_app(tmp_path)
        mark_body = self._payload(
            "pipe-a", [("local:/in", "/in/a.json"), ("local:/in", "/in/b.json")]
        )
        assert client.post("/api/internal/processed-files/mark", json=mark_body).status_code == 200

        check_body = self._payload(
            "pipe-a",
            [
                ("local:/in", "/in/a.json"),
                ("local:/in", "/in/unseen.json"),
                ("local:/in", "/in/b.json"),
            ],
        )
        resp = client.post("/api/internal/processed-files/check", json=check_body)
        assert resp.status_code == 200
        assert resp.json() == {"processed": [True, False, True]}

    def test_per_pipeline_isolation(self, tmp_path):
        """Files are namespaced by pipeline_name — another pipeline sees nothing."""
        client = self._make_app(tmp_path)
        body = self._payload("pipe-a", [("local:/in", "/in/a.json")])
        assert client.post("/api/internal/processed-files/mark", json=body).status_code == 200

        other = self._payload("pipe-b", [("local:/in", "/in/a.json")])
        resp = client.post("/api/internal/processed-files/check", json=other)
        assert resp.json() == {"processed": [False]}

    def test_source_key_is_part_of_the_key(self, tmp_path):
        """The same filepath under a different source_key is a different file."""
        client = self._make_app(tmp_path)
        body = self._payload("pipe-a", [("local:/in", "/in/a.json")])
        assert client.post("/api/internal/processed-files/mark", json=body).status_code == 200

        resp = client.post(
            "/api/internal/processed-files/check",
            json=self._payload("pipe-a", [("s3:bucket/key", "/in/a.json")]),
        )
        assert resp.json() == {"processed": [False]}

    def test_db_unavailable_503(self, tmp_path):
        app = FastAPI()
        app.include_router(router)
        app.state.controller = MagicMock()
        app.state.stats_store = MagicMock()
        app.state.db = None
        client = TestClient(app)

        resp = client.post(
            "/api/internal/processed-files/check",
            json=self._payload("pipe-a", [("local:/in", "/in/a.json")]),
        )
        assert resp.status_code == 503
        resp = client.post(
            "/api/internal/processed-files/mark",
            json=self._payload("pipe-a", [("local:/in", "/in/a.json")]),
        )
        assert resp.status_code == 503

    def test_missing_body_422(self, tmp_path):
        client = self._make_app(tmp_path)
        assert client.post("/api/internal/processed-files/check", content=b"").status_code == 422
        assert client.post("/api/internal/processed-files/mark", content=b"").status_code == 422

    def test_not_in_openapi_schema(self, tmp_path):
        client = self._make_app(tmp_path)
        paths = client.get("/openapi.json").json().get("paths", {})
        assert "/api/internal/processed-files/check" not in paths
        assert "/api/internal/processed-files/mark" not in paths
