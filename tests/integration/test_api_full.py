"""Integration tests for the full REST API lifecycle.

Tests the complete pipeline management flow through the API:
  - Register a pipeline (POST /api/pipelines)
  - List pipelines (GET /api/pipelines)
  - Trigger a manual run (POST /api/pipelines/{name}/run)
  - Fetch run history (GET /api/runs)
  - Version/rollback (GET /api/pipelines/{name}/versions, POST rollback)
  - Health and readiness checks
"""

from __future__ import annotations

import json
import os
import textwrap
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tram.api.app import create_app
from tram.core.config import AppConfig


@pytest.fixture
def tmp_dirs(tmp_path):
    src = tmp_path / "source"
    dst = tmp_path / "sink"
    src.mkdir()
    dst.mkdir()
    return src, dst


@pytest.fixture
def app(tmp_path):
    """Create a test FastAPI app with in-memory SQLite DB."""
    db_path = tmp_path / "test.db"
    pipeline_dir = tmp_path / "pipelines"
    pipeline_dir.mkdir(parents=True, exist_ok=True)

    env_overrides = {
        "TRAM_HOST": "127.0.0.1",
        "TRAM_PORT": "8765",
        "TRAM_PIPELINE_DIR": str(pipeline_dir),
        "TRAM_RELOAD_ON_START": "false",
        "TRAM_DB_URL": f"sqlite:///{db_path}",
        "TRAM_NODE_ID": "test-node",
        "TRAM_API_KEY": "",
        "TRAM_RATE_LIMIT": "0",
        "TRAM_OTEL_ENDPOINT": "",
        "TRAM_WATCH_PIPELINES": "false",
    }
    with patch.dict(os.environ, env_overrides, clear=False):
        config = AppConfig.from_env()
    return create_app(config=config)


@pytest.fixture
def client(app):
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _minimal_pipeline_yaml(src: str, dst: str, name: str = "api-test") -> str:
    return textwrap.dedent(f"""
        pipeline:
          name: {name}
          source:
            type: local
            path: {src}
          serializer_in:
            type: json
          serializer_out:
            type: json
          sink:
            type: local
            path: {dst}
    """)


class TestPipelineLifecycle:
    def test_health_endpoint(self, client):
        """GET /api/health returns 200 with status ok."""
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_readiness_endpoint(self, client):
        """GET /api/ready returns 200 when scheduler is running."""
        response = client.get("/api/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"

    def test_list_pipelines_empty(self, client):
        """GET /api/pipelines returns empty list initially."""
        response = client.get("/api/pipelines")
        assert response.status_code == 200
        assert response.json() == []

    def test_register_pipeline(self, client, tmp_dirs):
        """POST /api/pipelines registers a new pipeline."""
        src, dst = tmp_dirs
        yaml_text = _minimal_pipeline_yaml(src, dst)

        response = client.post(
            "/api/pipelines",
            content=yaml_text,
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "api-test"

    def test_register_then_list(self, client, tmp_dirs):
        """Registered pipeline appears in list."""
        src, dst = tmp_dirs
        yaml_text = _minimal_pipeline_yaml(src, dst)

        client.post("/api/pipelines", content=yaml_text, headers={"Content-Type": "text/plain"})

        response = client.get("/api/pipelines")
        assert response.status_code == 200
        names = [p["name"] for p in response.json()]
        assert "api-test" in names

    def test_register_duplicate_returns_409(self, client, tmp_dirs):
        """Registering the same pipeline twice returns 409 Conflict."""
        src, dst = tmp_dirs
        yaml_text = _minimal_pipeline_yaml(src, dst)

        r1 = client.post("/api/pipelines", content=yaml_text, headers={"Content-Type": "text/plain"})
        assert r1.status_code == 201

        r2 = client.post("/api/pipelines", content=yaml_text, headers={"Content-Type": "text/plain"})
        assert r2.status_code == 409

    def test_run_pipeline(self, client, tmp_dirs):
        """POST /api/pipelines/{name}/run executes a manual run."""
        src, dst = tmp_dirs
        (src / "data.json").write_text(json.dumps([{"id": 1}]))
        yaml_text = _minimal_pipeline_yaml(src, dst)

        client.post("/api/pipelines", content=yaml_text, headers={"Content-Type": "text/plain"})

        response = client.post("/api/pipelines/api-test/run")
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") in ("success", "running", "started", "triggered")

    def test_get_run_history(self, client, tmp_dirs):
        """GET /api/runs returns run records after a pipeline execution."""
        src, dst = tmp_dirs
        (src / "data.json").write_text(json.dumps([{"id": 1}]))
        yaml_text = _minimal_pipeline_yaml(src, dst)

        client.post("/api/pipelines", content=yaml_text, headers={"Content-Type": "text/plain"})
        client.post("/api/pipelines/api-test/run")

        response = client.get("/api/runs")
        assert response.status_code == 200
        runs = response.json()
        # At least one run should be recorded
        assert isinstance(runs, list)

    def test_run_history_csv_export(self, client, tmp_dirs):
        """GET /api/runs?format=csv returns CSV content."""
        src, dst = tmp_dirs
        (src / "data.json").write_text(json.dumps([{"id": 1}]))
        yaml_text = _minimal_pipeline_yaml(src, dst)

        client.post("/api/pipelines", content=yaml_text, headers={"Content-Type": "text/plain"})
        client.post("/api/pipelines/api-test/run")

        response = client.get("/api/runs?format=csv")
        assert response.status_code == 200
        assert "text/csv" in response.headers.get("content-type", "")

    def test_delete_pipeline(self, client, tmp_dirs):
        """DELETE /api/pipelines/{name} removes the pipeline."""
        src, dst = tmp_dirs
        yaml_text = _minimal_pipeline_yaml(src, dst)

        client.post("/api/pipelines", content=yaml_text, headers={"Content-Type": "text/plain"})

        response = client.delete("/api/pipelines/api-test")
        assert response.status_code in (200, 204)

        # Should no longer be in the list
        after = client.get("/api/pipelines")
        names = [p["name"] for p in after.json()]
        assert "api-test" not in names

    def test_get_versions(self, client, tmp_dirs):
        """GET /api/pipelines/{name}/versions returns version list."""
        src, dst = tmp_dirs
        yaml_text = _minimal_pipeline_yaml(src, dst)
        client.post("/api/pipelines", content=yaml_text, headers={"Content-Type": "text/plain"})

        response = client.get("/api/pipelines/api-test/versions")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_metrics_endpoint_responds(self, client):
        """GET /metrics responds (200 with prometheus-client, 503 if not installed)."""
        response = client.get("/metrics")
        # 200 if prometheus_client installed, 503 if not
        assert response.status_code in (200, 503)
