"""Tests for pipeline CRUD + lifecycle API endpoints."""
from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.agent.stats_store import StatsStore
from tram.api.routers.internal import PipelineStatsPayload
from tram.api.routers.pipelines import router
from tram.core.exceptions import PipelineAlreadyExistsError, PipelineNotFoundError
from tram.pipeline.controller import ExecutorOverloadError, QueueCapacityError
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
        app.state.controller.list_all.return_value = []
        client = TestClient(app)
        resp = client.get("/api/pipelines")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_pipeline_dicts(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.list_all.return_value = [state]
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
        app.state.controller.get.return_value = state
        client = TestClient(app)
        resp = client.get("/api/pipelines/test-pipe")
        assert resp.status_code == 200
        assert resp.json()["name"] == "test-pipe"

    def test_not_found_returns_404(self):
        app = _make_app()
        app.state.controller.get.side_effect = PipelineNotFoundError("test-pipe not found")
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

    def test_returns_live_broadcast_placement_when_manager_stats_are_missing(self):
        stream_yaml = """\
name: test-pipe
schedule:
  type: stream
source:
  type: webhook
  path: /ingest
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""
        state = _make_state(yaml_text=stream_yaml)
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
                "current_run_id": "pg1-w0",
                "status": "running",
                "restart_count": 0,
            }],
        }]
        app.state.controller._worker_pool = MagicMock()
        app.state.controller._worker_pool.live_streams.return_value = [{
            "worker_url": "http://worker-0:8766",
            "worker_id": "w0",
            "pipeline_name": "test-pipe",
            "run_id": "pg1-w0",
            "schedule_type": "stream",
            "uptime_seconds": 5.0,
            "stats": {
                "records_in": 20,
                "records_out": 10,
                "bytes_in": 200,
                "bytes_out": 100,
            },
        }]
        client = TestClient(app)

        resp = client.get("/api/pipelines/test-pipe/placement")

        assert resp.status_code == 200
        data = resp.json()
        assert data["active_slots"] == 1
        assert data["records_out_per_sec"] == 2.0
        assert data["slots"][0]["stats"]["stale"] is False

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

    def test_returns_count1_placement_row(self):
        """D.2 (GH #17): a count=1 stream dispatches through the placement
        machinery; the endpoint renders the durable 1-slot row (target_count
        "1") in the placement shape the UI expects."""
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.get_active_broadcast_placements.return_value = [{
            "placement_group_id": "pg1",
            "pipeline_name": "test-pipe",
            "status": "running",
            "target_count": "1",
            "started_at": datetime.now(UTC),
            "slots": [{
                "worker_index": 0,
                "worker_id": "w0",
                "worker_url": "http://worker-0:8766",
                "run_id_prefix": "pg1",
                "current_run_id": "pg1",
                "status": "running",
                "restart_count": 0,
            }],
        }]
        app.state.stats_store.update(PipelineStatsPayload(
            worker_id="w0",
            pipeline_name="test-pipe",
            run_id="pg1",
            schedule_type="stream",
            uptime_seconds=10.0,
            timestamp=datetime.now(UTC),
            records_in=30,
            records_out=25,
            bytes_in=600,
            bytes_out=500,
        ))
        client = TestClient(app)

        resp = client.get("/api/pipelines/test-pipe/placement")

        assert resp.status_code == 200
        data = resp.json()
        assert data["placement_group_id"] == "pg1"
        assert data["target_count"] == "1"
        assert data["slot_count"] == 1
        assert data["active_slots"] == 1
        assert data["records_in"] == 30
        slot = data["slots"][0]
        assert slot["worker_index"] == 0
        assert slot["worker_url"] == "http://worker-0:8766"
        assert slot["current_run_id"] == "pg1"
        assert slot["stats"]["stale"] is False
        assert slot["stats"]["records_out_per_sec"] == 2.5

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

    def test_serializer_in_rejects_unknown_nested_keys(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        bad_yaml = """\
name: test-pipe
schedule:
  type: manual
source:
  type: local
  path: /tmp/in
serializer_in:
  type: json
  transforms:
    - type: rename
      fields:
        old_name: new_name
sinks:
  - type: local
    path: /tmp/out
"""
        resp = client.post("/api/pipelines/dry-run", json={"yaml_text": bad_yaml})
        assert resp.status_code == 200
        assert resp.json()["valid"] is False
        assert any("serializer_in" in issue for issue in resp.json()["issues"])


class TestLifecycle:
    def test_start_pipeline(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.start_pipeline.return_value = "started"
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
        assert resp.json()["detail"] == "Pipeline 'test-pipe' started."
        app.state.controller.start_pipeline.assert_called_once_with("test-pipe")

    def test_start_pipeline_disabled_returns_config_feedback(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.start_pipeline.return_value = "disabled"
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "disabled"
        assert "disabled in YAML" in resp.json()["detail"]
        assert "triggered manually" in resp.json()["detail"]

    def test_start_pipeline_manual_returns_run_now_feedback(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.start_pipeline.return_value = "manual"
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "manual"
        assert resp.json()["detail"] == "Pipeline 'test-pipe' uses a manual schedule. Use Run Now instead."

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
        app.state.controller._record_completed_lifecycle_operation.return_value = "op-123"
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/run")
        # V18-09: the trigger receipt is 202 with the lifecycle-operation id
        # alongside the legacy name/status keys.
        assert resp.status_code == 202
        body = resp.json()
        assert body["run_id"] == "run-123"
        assert body["operation_id"] == "op-123"
        assert body["name"] == "test-pipe"
        assert body["status"] == "triggered"
        app.state.controller._record_completed_lifecycle_operation.assert_called_once_with(
            "test-pipe", "trigger", detail="manual run triggered"
        )

    def test_trigger_run_with_flush_query(self):
        """?flush=true forwards the F.1 flush-run flag to the controller."""
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.trigger_run.return_value = "run-flush-1"
        app.state.controller._record_completed_lifecycle_operation.return_value = "op-1"
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/run?flush=true")
        assert resp.status_code == 202
        app.state.controller.trigger_run.assert_called_once_with(
            "test-pipe", flush=True
        )

    def test_trigger_run_queue_capacity_maps_503(self):
        """V18-08 budget rejection: the E.2 queue at its cap is capacity, not
        a client error (400) or a server fault (500)."""
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.trigger_run.side_effect = QueueCapacityError(
            "queue at capacity: 1000 rows"
        )
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/run")
        assert resp.status_code == 503
        assert "queue at capacity" in resp.json()["detail"]
        app.state.controller._record_completed_lifecycle_operation.assert_not_called()

    def test_trigger_run_executor_overload_maps_503(self):
        """V18-08: bounded management executor saturation is explicit
        backpressure (503), never unbounded accumulation or a 500."""
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.trigger_run.side_effect = ExecutorOverloadError(
            "management executor saturated: ceiling 1000"
        )
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/run")
        assert resp.status_code == 503
        assert "saturated" in resp.json()["detail"]

    def test_trigger_run_flush_defaults_false(self):
        """Without ?flush, the run is a normal (non-flush) run."""
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.trigger_run.return_value = "run-norm-1"
        app.state.controller._record_completed_lifecycle_operation.return_value = "op-1"
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/run")
        assert resp.status_code == 202
        app.state.controller.trigger_run.assert_called_once_with(
            "test-pipe", flush=False
        )

    def test_trigger_run_queued_receipt_has_operation_id(self):
        """E.2 queued path: 202 already; V18-09 adds the operation_id."""
        from tram.pipeline.controller import TriggerResult
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.trigger_run.return_value = TriggerResult("run-queued-1", "queued")
        app.state.controller._record_completed_lifecycle_operation.return_value = "op-queued-1"
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/run")
        assert resp.status_code == 202
        body = resp.json()
        assert body["run_id"] == "run-queued-1"
        assert body["status"] == "queued"
        assert body["operation_id"] == "op-queued-1"
        assert "expires_at" in body

    def test_trigger_run_dispatched_receipt_is_202(self):
        """The synchronous-submit path now returns 202 too (V18-09 §8)."""
        from tram.pipeline.controller import TriggerResult
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.trigger_run.return_value = TriggerResult("run-dispatch-1", "dispatched")
        app.state.controller._record_completed_lifecycle_operation.return_value = "op-dispatch-1"
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/run")
        assert resp.status_code == 202
        body = resp.json()
        assert body["run_id"] == "run-dispatch-1"
        assert body["status"] == "triggered"
        assert body["operation_id"] == "op-dispatch-1"

    def test_trigger_records_lifecycle_operation_row(self, tmp_path):
        """End to end: the trigger writes a lifecycle_operations row and the
        receipt's operation_id resolves against it (real controller + DB)."""
        from tram.persistence.db import TramDB
        from tram.pipeline.controller import PipelineController, TriggerResult
        db = TramDB(url=f"sqlite:///{tmp_path}/trigger-op-row.db")
        ctrl = PipelineController(db=db, node_id="n0")
        ctrl.manager.register(
            load_pipeline_from_yaml(_MINIMAL_YAML), yaml_text=_MINIMAL_YAML
        )
        # Do not actually submit a batch run in a unit test.
        ctrl.trigger_run = MagicMock(return_value=TriggerResult("run-real-1", "dispatched"))
        app = _make_app(db=db)
        app.state.controller = ctrl
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/run")
        assert resp.status_code == 202
        operation_id = resp.json()["operation_id"]
        assert operation_id is not None
        ops = ctrl.get_lifecycle_operations(pipeline_name="test-pipe")
        assert len(ops) == 1
        assert ops[0]["operation_id"] == operation_id
        assert ops[0]["op_kind"] == "trigger"
        assert ops[0]["state"] == "complete"
        assert ops[0]["pipeline_name"] == "test-pipe"

    def test_trigger_no_db_returns_null_operation_id(self):
        """Without persistence the controller records no operation row and the
        receipt carries operation_id: null (honest, no fake id)."""
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.trigger_run.return_value = "run-nodb-1"
        app.state.controller._record_completed_lifecycle_operation.return_value = None
        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/run")
        assert resp.status_code == 202
        assert resp.json()["operation_id"] is None

    def test_trigger_stream_pipeline_returns_400(self):
        app = _make_app()
        app.state.controller.get.return_value = _make_state()
        app.state.controller.trigger_run.side_effect = ValueError("stream pipeline")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/pipelines/test-pipe/run")
        assert resp.status_code == 400


class TestLifecycleOperations:
    """GET /api/pipelines/{name}/operations (V18-09 §8)."""

    _OP_ROWS = [
        {
            "operation_id": "op-1",
            "pipeline_name": "test-pipe",
            "op_kind": "boot_adopt",
            "state": "complete",
            "attempt_id": "run-1-a1",
            "detail": "boot adoption: attempt resolved from journal completion record",
            "created_at": "2026-10-08T12:00:00+00:00",
            "updated_at": "2026-10-08T12:00:01+00:00",
        },
        {
            "operation_id": "op-2",
            "pipeline_name": "test-pipe",
            "op_kind": "stop",
            "state": "pending",
            "attempt_id": None,
            "detail": "pipeline stop requested",
            "created_at": "2026-10-08T12:05:00+00:00",
            "updated_at": "2026-10-08T12:05:00+00:00",
        },
        {
            "operation_id": "op-3",
            "pipeline_name": "other-pipe",
            "op_kind": "delete",
            "state": "complete",
            "attempt_id": None,
            "detail": None,
            "created_at": "2026-10-08T12:06:00+00:00",
            "updated_at": "2026-10-08T12:06:00+00:00",
        },
    ]

    def _app(self, ops=_OP_ROWS):
        app = _make_app()
        app.state.controller.get.return_value = _make_state()
        app.state.controller.get_lifecycle_operations.return_value = ops
        return app

    def test_lists_rows_scoped_to_pipeline(self):
        client = TestClient(self._app())
        r = client.get("/api/pipelines/test-pipe/operations")
        assert r.status_code == 200
        assert r.json() == self._OP_ROWS
        client.app.state.controller.get_lifecycle_operations.assert_called_once_with(
            pipeline_name="test-pipe", limit=50
        )

    def test_row_shape(self):
        client = TestClient(self._app())
        row = client.get("/api/pipelines/test-pipe/operations").json()[0]
        assert set(row) == {
            "operation_id", "pipeline_name", "op_kind", "state",
            "attempt_id", "detail", "created_at", "updated_at",
        }
        assert row["op_kind"] == "boot_adopt"

    def test_boot_adopt_kind_present(self):
        client = TestClient(self._app())
        kinds = {op["op_kind"] for op in client.get("/api/pipelines/test-pipe/operations").json()}
        assert "boot_adopt" in kinds

    def test_state_filter(self):
        client = TestClient(self._app())
        r = client.get("/api/pipelines/test-pipe/operations?state=pending")
        assert r.status_code == 200
        body = r.json()
        assert [op["operation_id"] for op in body] == ["op-2"]
        assert all(op["state"] == "pending" for op in body)

    def test_op_kind_filter(self):
        client = TestClient(self._app())
        r = client.get("/api/pipelines/test-pipe/operations?op_kind=boot_adopt")
        assert r.status_code == 200
        body = r.json()
        assert [op["operation_id"] for op in body] == ["op-1"]
        assert all(op["op_kind"] == "boot_adopt" for op in body)

    def test_limit_passed_through(self):
        client = TestClient(self._app())
        r = client.get("/api/pipelines/test-pipe/operations?limit=10")
        assert r.status_code == 200
        client.app.state.controller.get_lifecycle_operations.assert_called_once_with(
            pipeline_name="test-pipe", limit=10
        )

    def test_missing_pipeline_returns_404(self):
        from tram.core.exceptions import PipelineNotFoundError
        app = self._app()
        app.state.controller.get.side_effect = PipelineNotFoundError("nope")
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/pipelines/test-pipe/operations")
        assert r.status_code == 404

    def test_operations_read_real_rows(self, tmp_path):
        """End to end against a real controller + DB: recorded lifecycle rows
        surface through the endpoint with the frozen column set."""
        from tram.persistence.db import TramDB
        from tram.pipeline.controller import PipelineController
        db = TramDB(url=f"sqlite:///{tmp_path}/ops-real.db")
        ctrl = PipelineController(db=db, node_id="n0")
        ctrl.manager.register(
            load_pipeline_from_yaml(_MINIMAL_YAML), yaml_text=_MINIMAL_YAML
        )
        op_id = ctrl._record_completed_lifecycle_operation(
            "test-pipe", "boot_adopt", detail="boot adoption test"
        )
        app = _make_app(db=db)
        app.state.controller = ctrl
        client = TestClient(app)
        r = client.get("/api/pipelines/test-pipe/operations")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["operation_id"] == op_id
        assert body[0]["op_kind"] == "boot_adopt"
        assert body[0]["state"] == "complete"
        assert body[0]["detail"] == "boot adoption test"
        assert body[0]["attempt_id"] is None


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
        app.state.controller.get.return_value = state
        # _save_alerts_data routes through controller.update()
        app.state.controller.update.return_value = new_state
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

    def test_alert_edits_survive_controller_restart(self, tmp_path):
        """B.1 regression: alert-rule edits saved via the API must land in
        the pipeline registry (db.save_pipeline) so a controller restart
        reloads the edited rules. The old path only saved a *version*."""
        from tram.persistence.db import TramDB
        from tram.pipeline.controller import PipelineController

        yaml_text = _MINIMAL_YAML + textwrap.dedent("""\
            alerts:
              - name: boot-alert
                condition: "failed"
                action: webhook
                webhook_url: "http://hooks.example.com/boot"
        """)

        db = TramDB(url=f"sqlite:///{tmp_path}/alerts-restart.db", node_id="test-node")

        # First controller — register a pipeline that already has one alert rule.
        ctrl = PipelineController(db=db, node_id="test-node")
        ctrl.start()
        config = load_pipeline_from_yaml(yaml_text)
        ctrl.register(config, yaml_text=yaml_text)

        # Edit the alert rule through the API.
        app = FastAPI()
        app.include_router(router)
        app.state.controller = ctrl
        app.state.manager = ctrl.manager
        app.state.scheduler = ctrl
        app.state.config = MagicMock()
        app.state.db = db
        app.state.stats_store = StatsStore(interval=30)
        client = TestClient(app)
        resp = client.put("/api/pipelines/test-pipe/alerts/0", json={
            "name": "edited-alert",
            "condition": "last_run_status == 'error'",
            "action": "webhook",
            "webhook_url": "http://hooks.example.com/edited",
        })
        assert resp.status_code == 200
        ctrl.stop()

        # The edited YAML must be in the shared registry before any restart.
        persisted = db.get_all_pipelines()
        assert len(persisted) == 1
        assert "edited-alert" in persisted[0][1]
        assert "last_run_status" in persisted[0][1]

        # Restart: recreate the controller from the same DB and boot-load it.
        ctrl2 = PipelineController(db=db, node_id="test-node")
        ctrl2.start()
        try:
            state = ctrl2.manager.get("test-pipe")
            assert len(state.config.alerts) == 1, "alert rules missing after restart"
            rule = state.config.alerts[0]
            assert rule.name == "edited-alert"
            assert rule.condition == "last_run_status == 'error'"
            assert rule.webhook_url == "http://hooks.example.com/edited"
        finally:
            ctrl2.stop()
            db.close()


class TestVersions:
    def test_list_versions(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.get_versions.return_value = [{"version": 1, "created_at": "2026-01-01"}]
        client = TestClient(app)
        resp = client.get("/api/pipelines/test-pipe/versions")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_list_versions_not_found(self):
        app = _make_app()
        app.state.controller.get.side_effect = PipelineNotFoundError("not found")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/pipelines/nonexistent/versions")
        assert resp.status_code == 404

    def test_get_version_yaml(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.get_version_yaml.return_value = _MINIMAL_YAML
        client = TestClient(app)
        resp = client.get("/api/pipelines/test-pipe/versions/1")
        assert resp.status_code == 200
        assert "test-pipe" in resp.text

    def test_get_version_yaml_not_found(self):
        state = _make_state()
        app = _make_app()
        app.state.controller.get.return_value = state
        app.state.controller.get_version_yaml.side_effect = KeyError("version not found")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/pipelines/test-pipe/versions/99")
        assert resp.status_code == 404


class TestRollback:
    def test_rollback_uses_controller_and_restarts_only_when_previously_active(self):
        current_state = _make_state(name="interval-pipe", status="scheduled", yaml_text=_INTERVAL_YAML)
        rolled_state = _make_state(name="interval-pipe", status="stopped", yaml_text=_INTERVAL_YAML)
        app = _make_app()
        app.state.controller.get.side_effect = [current_state, rolled_state]
        app.state.controller.rollback.return_value = load_pipeline_from_yaml(_INTERVAL_YAML)

        client = TestClient(app)
        resp = client.post("/api/pipelines/interval-pipe/rollback?version=1")

        assert resp.status_code == 200
        app.state.controller.stop_pipeline.assert_called_once_with("interval-pipe")
        app.state.controller.rollback.assert_called_once_with("interval-pipe", 1)
        app.state.controller.start_pipeline.assert_called_once_with("interval-pipe")

    def test_rollback_keeps_stopped_pipeline_stopped(self):
        current_state = _make_state(status="stopped")
        rolled_state = _make_state(status="stopped")
        app = _make_app()
        app.state.controller.get.side_effect = [current_state, rolled_state]
        app.state.controller.rollback.return_value = load_pipeline_from_yaml(_MINIMAL_YAML)

        client = TestClient(app)
        resp = client.post("/api/pipelines/test-pipe/rollback?version=2")

        assert resp.status_code == 200
        app.state.controller.stop_pipeline.assert_not_called()
        app.state.controller.rollback.assert_called_once_with("test-pipe", 2)
        app.state.controller.start_pipeline.assert_not_called()


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
