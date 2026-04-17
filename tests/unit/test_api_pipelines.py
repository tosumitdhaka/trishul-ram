"""Tests for pipeline CRUD + lifecycle API endpoints."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.agent.stats_store import StatsStore
from tram.api.routers.internal import PipelineStatsPayload
from tram.api.routers.pipelines import router
from tram.core.exceptions import PipelineAlreadyExistsError, PipelineNotFoundError
from tram.pipeline.loader import load_pipeline_from_yaml
from tram.pipeline.manager import PipelineState

_MINIMAL_YAML = """\
name: test-pipe
schedule:
  type: manual
source:
  type: local
  path: /tmp/in
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""

_INTERVAL_YAML = """\
name: interval-pipe
schedule:
  type: interval
  interval_seconds: 60
source:
  type: local
  path: /tmp/in
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""


def _make_state(name="test-pipe", status="stopped", yaml_text=_MINIMAL_YAML):
    config = load_pipeline_from_yaml(yaml_text)
    state = PipelineState(config, yaml_text=yaml_text)
    state.status = status
    return state


def _make_app(db=None):
    app = FastAPI()
    app.include_router(router)

    mock_manager = MagicMock()
    mock_controller = MagicMock()
    mock_controller.manager = mock_manager
    mock_config = MagicMock()
    mock_config.pipeline_dir = "/tmp/pipelines"

    app.state.manager = mock_manager
    app.state.controller = mock_controller
    app.state.scheduler = mock_controller   # alias for any legacy refs
    app.state.config = mock_config
    app.state.db = db
    app.state.stats_store = StatsStore(interval=30)
    return app


class TestListPipelines:
    def test_empty_list(self):
        app = _make_app()
        app.state.manager.list_all.return_value = []
        client = TestClient(app)
        resp = client.get("/api/pipelines")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_pipeline_dicts(self):
        state = _make_state()
        app = _make_app()
        app.state.manager.list_all.return_value = [state]
        client = TestClient(app)
        resp = client.get("/api/pipelines")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "test-pipe"


class TestGetPipeline:
    def test_returns_detail_dict(self):
        state = _make_state()
        app = _make_app()
        app.state.manager.get.return_value = state
        client = TestClient(app)
        resp = client.get("/api/pipelines/test-pipe")
        assert resp.status_code == 200
        assert resp.json()["name"] == "test-pipe"

    def test_not_found_returns_404(self):
        app = _make_app()
        app.state.manager.get.side_effect = PipelineNotFoundError("test-pipe not found")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/pipelines/test-pipe")
        assert resp.status_code == 404


class TestGetPipelinePlacement:
    def test_returns_enriched_broadcast_placement(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.get_active_broadcast_placements.return_value = [{
            "placement_group_id": "pg1",
            "pipeline_name": "test-pipe",
            "status": "running",
            "target_count": "all",
            "started_at": datetime.now(UTC),
            "slots": [{
                "worker_index": 0,
                "worker_id": "w0",
                "worker_url": "http://worker-0:8766",
                "run_id_prefix": "pg1-w0",
                "current_run_id": "pg1-w0-r1",
                "status": "running",
                "restart_count": 1,
            }],
        }]
        app.state.stats_store.update(PipelineStatsPayload(
            worker_id="w0",
            pipeline_name="test-pipe",
            run_id="pg1-w0-r1",
            schedule_type="stream",
            uptime_seconds=10.0,
            timestamp=datetime.now(UTC),
            records_in=50,
            records_out=40,
            bytes_in=1000,
            bytes_out=600,
        ))
        client = TestClient(app)

        resp = client.get("/api/pipelines/test-pipe/placement")

        assert resp.status_code == 200
        data = resp.json()
        assert data["placement_group_id"] == "pg1"
        assert data["slot_count"] == 1
        assert data["active_slots"] == 1
        assert data["records_in"] == 50
        assert data["slots"][0]["current_run_id"] == "pg1-w0-r1"
        assert data["slots"][0]["stats"]["stale"] is False
        assert data["slots"][0]["stats"]["bytes_in_per_sec"] == 100.0

    def test_includes_stale_slot_with_zeroed_per_sec(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.get_active_broadcast_placements.return_value = [{
            "placement_group_id": "pg1",
            "pipeline_name": "test-pipe",
            "status": "degraded",
            "target_count": "all",
            "started_at": datetime.now(UTC),
            "slots": [{
                "worker_index": 0,
                "worker_id": "w0",
                "worker_url": "http://worker-0:8766",
                "run_id_prefix": "pg1-w0",
                "current_run_id": "pg1-w0-r1",
                "status": "stale",
                "restart_count": 1,
            }],
        }]
        app.state.stats_store.update(PipelineStatsPayload(
            worker_id="w0",
            pipeline_name="test-pipe",
            run_id="pg1-w0-r1",
            schedule_type="stream",
            uptime_seconds=10.0,
            timestamp=datetime.now(UTC) - timedelta(seconds=120),
            records_in=50,
            bytes_in=1000,
        ))
        client = TestClient(app)

        resp = client.get("/api/pipelines/test-pipe/placement")

        assert resp.status_code == 200
        data = resp.json()
        assert data["active_slots"] == 0
        assert data["slots"][0]["stats"]["stale"] is True
        assert data["slots"][0]["stats"]["records_in"] == 50
        assert data["slots"][0]["stats"]["bytes_in_per_sec"] == 0.0

    def test_missing_active_placement_returns_404(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.get_active_broadcast_placements.return_value = []
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/pipelines/test-pipe/placement")

        assert resp.status_code == 404


class TestRegisterPipeline:
    def test_register_from_json_body(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.register.return_value = state
        client = TestClient(app)
        resp = client.post("/api/pipelines", json={"yaml_text": _MINIMAL_YAML})
        assert resp.status_code == 201
        assert resp.json()["name"] == "test-pipe"

    def test_register_from_yaml_content_type(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.register.return_value = state
        client = TestClient(app)
        resp = client.post(
            "/api/pipelines",
            content=_MINIMAL_YAML.encode(),
            headers={"Content-Type": "text/yaml"},
        )
        assert resp.status_code == 201

    def test_empty_body_returns_400(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/pipelines", json={"yaml_text": ""})
        assert resp.status_code == 400

    def test_invalid_yaml_returns_400(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/pipelines", json={"yaml_text": "not: valid: yaml: ["})
        assert resp.status_code == 400

    def test_duplicate_returns_409(self):
        app = _make_app()
        app.state.controller.register.side_effect = PipelineAlreadyExistsError("already exists")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/pipelines", json={"yaml_text": _MINIMAL_YAML})
        assert resp.status_code == 409

    def test_enabled_non_manual_starts_pipeline(self):
        state = _make_state(yaml_text=_INTERVAL_YAML)
        app = _make_app()
        app.state.controller.register.return_value = state
        client = TestClient(app)
        client.post("/api/pipelines", json={"yaml_text": _INTERVAL_YAML})
        # register() in controller handles scheduling internally
        app.state.controller.register.assert_called_once()

    def test_persists_to_db_if_available(self):
        state = _make_state()
        mock_db = MagicMock()
        app = _make_app(db=mock_db)
        app.state.controller.register.return_value = state
        client = TestClient(app)
        client.post("/api/pipelines", json={"yaml_text": _MINIMAL_YAML})
        # controller.register() handles DB persistence internally
        app.state.controller.register.assert_called_once()


class TestUpdatePipeline:
    def test_update_replaces_config(self):
        state = _make_state()
        new_state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.update.return_value = new_state
        client = TestClient(app)
        resp = client.put("/api/pipelines/test-pipe", json={"yaml_text": _MINIMAL_YAML})
        assert resp.status_code == 200

    def test_not_found_returns_404(self):
        app = _make_app()
        app.state.controller.get.side_effect = PipelineNotFoundError("not found")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put("/api/pipelines/test-pipe", json={"yaml_text": _MINIMAL_YAML})
        assert resp.status_code == 404

    def test_name_mismatch_returns_400(self):
        state = _make_state()  # name is test-pipe
        app = _make_app()
        app.state.controller.get.return_value = state
        new_yaml = _MINIMAL_YAML.replace("name: test-pipe", "name: different-name")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put("/api/pipelines/test-pipe", json={"yaml_text": new_yaml})
        assert resp.status_code == 400

    def test_stops_running_pipeline_before_update(self):
        state = _make_state(status="running")
        new_state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.update.return_value = new_state
        client = TestClient(app)
        client.put("/api/pipelines/test-pipe", json={"yaml_text": _MINIMAL_YAML})
        # controller.update() handles stop internally
        app.state.controller.update.assert_called_once()


class TestDeletePipeline:
    def test_delete_existing_pipeline(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        client = TestClient(app)
        resp = client.delete("/api/pipelines/test-pipe")
        assert resp.status_code == 204

    def test_not_found_returns_404(self):
        app = _make_app()
        app.state.controller.get.side_effect = PipelineNotFoundError("not found")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/api/pipelines/nonexistent")
        assert resp.status_code == 404

    def test_stops_running_before_delete(self):
        state = _make_state(status="running")
        app = _make_app()
        app.state.controller.get.return_value = state
        client = TestClient(app)
        client.delete("/api/pipelines/test-pipe")
        # controller.delete() always stops regardless of status
        app.state.controller.delete.assert_called_once_with("test-pipe")


class TestDryRun:
    def test_valid_yaml_returns_result(self):
        app = _make_app()
        from unittest.mock import patch
        mock_result = {"valid": True, "issues": []}
        with patch("tram.pipeline.executor.PipelineExecutor") as MockExec:
            MockExec.return_value.dry_run.return_value = mock_result
            client = TestClient(app)
            resp = client.post("/api/pipelines/dry-run", json={"yaml_text": _MINIMAL_YAML})
        assert resp.status_code == 200
        assert resp.json()["valid"] is True

    def test_invalid_yaml_returns_issues(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/pipelines/dry-run", json={"yaml_text": "not: valid: ["})
        assert resp.status_code == 200
        assert resp.json()["valid"] is False

    def test_empty_body_returns_400(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/pipelines/dry-run", json={"yaml_text": ""})
        assert resp.status_code == 400


class TestLifecycle:
    def test_start_pipeline(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
        app.state.controller.start_pipeline.assert_called_once_with("test-pipe")

    def test_start_not_found_returns_404(self):
        app = _make_app()
        app.state.controller.get.side_effect = PipelineNotFoundError("not found")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/pipelines/nonexistent/start")
        assert resp.status_code == 404

    def test_stop_pipeline(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        app.state.controller.stop_pipeline.assert_called_once_with("test-pipe")

    def test_trigger_run(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.trigger_run.return_value = "run-123"
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/run")
        assert resp.status_code == 200
        assert resp.json()["run_id"] == "run-123"

    def test_trigger_stream_pipeline_returns_400(self):
        app = _make_app()
        app.state.controller.get.return_value = _make_state()
        app.state.controller.trigger_run.side_effect = ValueError("stream pipeline")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/pipelines/test-pipe/run")
        assert resp.status_code == 400


class TestAlerts:
    def _setup_app_with_state(self, alerts=None):
        import yaml as _yaml
        doc = _yaml.safe_load(_MINIMAL_YAML)
        if alerts:
            doc["alerts"] = alerts
        yaml_text = _yaml.dump(doc)

        state = _make_state(yaml_text=yaml_text)
        state.yaml_text = yaml_text

        app = _make_app()
        new_state = _make_state(yaml_text=yaml_text)
        app.state.manager.get.return_value = state
        app.state.manager.register.return_value = new_state
        return app

    def test_list_alerts_empty(self):
        app = self._setup_app_with_state()
        client = TestClient(app)
        resp = client.get("/api/pipelines/test-pipe/alerts")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_alerts_with_rules(self):
        app = self._setup_app_with_state(alerts=[
            {"condition": "last_run_status == 'error'", "action": "webhook",
             "webhook_url": "http://hook.example.com"}
        ])
        client = TestClient(app)
        resp = client.get("/api/pipelines/test-pipe/alerts")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["condition"] == "last_run_status == 'error'"

    def test_create_alert(self):
        app = self._setup_app_with_state()
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/alerts", json={
            "condition": "last_run_status == 'error'",
            "action": "webhook",
            "webhook_url": "http://hook.example.com",
        })
        assert resp.status_code == 201

    def test_create_alert_missing_fields_returns_400(self):
        app = self._setup_app_with_state()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/pipelines/test-pipe/alerts", json={"condition": "x"})
        assert resp.status_code == 400

    def test_update_alert(self):
        app = self._setup_app_with_state(alerts=[
            {"condition": "old_cond", "action": "webhook", "webhook_url": "http://hook.example.com"}
        ])
        client = TestClient(app)
        resp = client.put("/api/pipelines/test-pipe/alerts/0", json={
            "condition": "new_cond",
            "action": "webhook",
            "webhook_url": "http://hook.example.com",
        })
        assert resp.status_code == 200

    def test_update_alert_out_of_range_returns_404(self):
        app = self._setup_app_with_state(alerts=[])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put("/api/pipelines/test-pipe/alerts/99", json={
            "condition": "x", "action": "webhook", "webhook_url": "http://x.com",
        })
        assert resp.status_code == 404

    def test_delete_alert(self):
        app = self._setup_app_with_state(alerts=[
            {"condition": "x", "action": "webhook", "webhook_url": "http://x.com"}
        ])
        client = TestClient(app)
        resp = client.delete("/api/pipelines/test-pipe/alerts/0")
        assert resp.status_code == 204

    def test_delete_alert_out_of_range_returns_404(self):
        app = self._setup_app_with_state(alerts=[])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/api/pipelines/test-pipe/alerts/5")
        assert resp.status_code == 404


class TestVersions:
    def test_list_versions(self):
        state = _make_state()
        app = _make_app()
        app.state.manager.get.return_value = state
        app.state.manager.get_versions.return_value = [{"version": 1, "created_at": "2026-01-01"}]
        client = TestClient(app)
        resp = client.get("/api/pipelines/test-pipe/versions")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_list_versions_not_found(self):
        app = _make_app()
        app.state.manager.get.side_effect = PipelineNotFoundError("not found")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/pipelines/nonexistent/versions")
        assert resp.status_code == 404

    def test_get_version_yaml(self):
        state = _make_state()
        app = _make_app()
        app.state.manager.get.return_value = state
        app.state.manager.get_version_yaml.return_value = _MINIMAL_YAML
        client = TestClient(app)
        resp = client.get("/api/pipelines/test-pipe/versions/1")
        assert resp.status_code == 200
        assert "test-pipe" in resp.text

    def test_get_version_yaml_not_found(self):
        state = _make_state()
        app = _make_app()
        app.state.manager.get.return_value = state
        app.state.manager.get_version_yaml.side_effect = KeyError("version not found")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/pipelines/test-pipe/versions/99")
        assert resp.status_code == 404


class TestReload:
    def test_reload_rescans_and_returns_counts(self):
        app = _make_app()
        app.state.controller.list_all.return_value = []
        from unittest.mock import patch
        with patch("tram.api.routers.pipelines.scan_pipeline_dir", return_value=[]):
            client = TestClient(app)
            resp = client.post("/api/pipelines/reload")
        assert resp.status_code == 200
        data = resp.json()
        assert "reloaded" in data
        assert "total" in data
