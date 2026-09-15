"""Wave B review: HTTP read endpoints must go through the controller's locked reads.

Regression coverage for the transient 404/500 window: raw ``app.state.manager``
reads from the routers raced ``controller.update()``'s atomic deregister->register
critical section and could observe the half-deregistered pipeline state. After
the fix every read endpoint calls a controller method that runs under the
lifecycle RLock, so a read issued while an update is in flight blocks until the
critical section completes and never sees a missing pipeline.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.health import router as health_router
from tram.api.routers.pipelines import router as pipelines_router
from tram.api.routers.runs import router as runs_router
from tram.api.routers.stats import router as stats_router
from tram.pipeline.controller import PipelineController
from tram.pipeline.loader import load_pipeline_from_yaml

_MANUAL_YAML = """\
name: my-manual
schedule:
  type: manual
source:
  type: local
  path: /dev/null
  file_pattern: "*.noop"
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""


def _make_app(router):
    """Build an app with a mock controller + mock manager for the given router."""
    app = FastAPI()
    app.include_router(router)
    manager = MagicMock()
    controller = MagicMock()
    app.state.manager = manager
    app.state.controller = controller
    return app, manager, controller


def _run_result(run_id="abc123"):
    r = MagicMock()
    r.to_dict.return_value = {
        "run_id": run_id,
        "pipeline": "my-pipe",
        "status": "success",
    }
    return r


class TestStatsReadsController:
    def test_stats_uses_controller_list_all(self):
        app, manager, controller = _make_app(stats_router)
        controller.list_all.return_value = []
        client = TestClient(app)

        resp = client.get("/api/stats")

        assert resp.status_code == 200
        assert resp.json()["pipelines_total"] == 0
        controller.list_all.assert_called_once_with()
        manager.list_all.assert_not_called()


class TestRunsReadsController:
    def test_list_runs_uses_controller_get_runs(self):
        app, manager, controller = _make_app(runs_router)
        controller.get_runs.return_value = []
        client = TestClient(app)

        resp = client.get("/api/runs")

        assert resp.status_code == 200
        assert resp.json() == []
        controller.get_runs.assert_called_once()
        manager.get_runs.assert_not_called()

    def test_get_run_uses_controller_get_run(self):
        app, manager, controller = _make_app(runs_router)
        controller.get_run.return_value = _run_result(run_id="xyz")
        client = TestClient(app)

        resp = client.get("/api/runs/xyz")

        assert resp.status_code == 200
        assert resp.json()["run_id"] == "xyz"
        controller.get_run.assert_called_once_with("xyz")
        manager.get_run.assert_not_called()


class TestPipelinesReadsController:
    def test_list_pipelines_uses_controller_list_all(self):
        app, manager, controller = _make_app(pipelines_router)
        controller.list_all.return_value = []
        client = TestClient(app)

        resp = client.get("/api/pipelines")

        assert resp.status_code == 200
        assert resp.json() == []
        controller.list_all.assert_called_once_with()
        manager.list_all.assert_not_called()

    def test_get_pipeline_uses_controller_get(self):
        app, manager, controller = _make_app(pipelines_router)
        state = MagicMock()
        state.to_detail_dict.return_value = {"name": "p1"}
        controller.get.return_value = state
        client = TestClient(app)

        resp = client.get("/api/pipelines/p1")

        assert resp.status_code == 200
        assert resp.json()["name"] == "p1"
        controller.get.assert_called_once_with("p1")
        manager.get.assert_not_called()

    def test_list_versions_uses_controller(self):
        app, manager, controller = _make_app(pipelines_router)
        controller.get.return_value = MagicMock()
        controller.get_versions.return_value = [{"version": 1}]
        client = TestClient(app)

        resp = client.get("/api/pipelines/p1/versions")

        assert resp.status_code == 200
        assert resp.json() == [{"version": 1}]
        controller.get_versions.assert_called_once_with("p1")
        manager.get_versions.assert_not_called()

    def test_get_version_yaml_uses_controller(self):
        app, manager, controller = _make_app(pipelines_router)
        controller.get.return_value = MagicMock()
        controller.get_version_yaml.return_value = _MANUAL_YAML
        client = TestClient(app)

        resp = client.get("/api/pipelines/p1/versions/1")

        assert resp.status_code == 200
        assert "my-manual" in resp.text
        controller.get_version_yaml.assert_called_once_with("p1", 1)
        manager.get_version_yaml.assert_not_called()

    def test_alerts_read_uses_controller_get(self):
        app, manager, controller = _make_app(pipelines_router)
        state = MagicMock()
        state.yaml_text = _MANUAL_YAML
        controller.get.return_value = state
        client = TestClient(app)

        resp = client.get("/api/pipelines/p1/alerts")

        assert resp.status_code == 200
        assert resp.json() == []
        controller.get.assert_called_once_with("p1")
        manager.get.assert_not_called()


class TestReadinessReadsController:
    def test_ready_uses_controller_list_all(self):
        app, manager, controller = _make_app(health_router)
        controller.list_all.return_value = [MagicMock(), MagicMock()]
        app.state.scheduler = MagicMock()
        app.state.scheduler._running = True
        client = TestClient(app)

        resp = client.get("/api/ready")

        assert resp.status_code == 200
        assert resp.json()["pipelines_loaded"] == 2
        controller.list_all.assert_called_once_with()
        manager.list_all.assert_not_called()


class TestReadDuringGatedUpdate:
    """A read issued inside update()'s deregister->register critical section
    must block on the lifecycle lock and then return a consistent result — never
    a 404/500 from observing the half-deregistered pipeline."""

    def test_api_read_blocks_behind_gated_update_and_does_not_raise(self):
        ctrl = PipelineController(node_id="test-node")
        ctrl.manager.register(load_pipeline_from_yaml(_MANUAL_YAML), yaml_text=_MANUAL_YAML)

        app = FastAPI()
        app.include_router(pipelines_router)
        app.state.controller = ctrl
        app.state.manager = ctrl.manager
        client = TestClient(app)

        # Gate update() inside the critical section, right at the point where
        # the pipeline is deregistered (the transient-error window).
        entered = threading.Event()
        release = threading.Event()
        original_deregister = ctrl.manager.deregister

        def gated_deregister(name):
            entered.set()
            assert release.wait(timeout=10), "gate was never released"
            return original_deregister(name)

        ctrl.manager.deregister = gated_deregister

        v2 = _MANUAL_YAML + "description: updated-v2\n"
        update_errors: list = []

        def _update():
            try:
                ctrl.update("my-manual", v2)
            except Exception as exc:  # noqa: BLE001 — collected for assertion
                update_errors.append(exc)

        updater = threading.Thread(target=_update)
        updater.start()
        assert entered.wait(timeout=5), "update never reached the gated deregister"

        # Issue the API read while the update still holds the lock mid-critical-section.
        read_result = {}
        read_errors: list = []

        # Track when the read reaches the locked read method: once it does, the
        # update holds the lock, so the read is deterministically blocked there.
        read_entered = threading.Event()
        original_get = ctrl.get

        def tracked_get(name):
            read_entered.set()
            return original_get(name)

        ctrl.get = tracked_get

        def _read():
            try:
                resp = client.get("/api/pipelines/my-manual")
                read_result["status_code"] = resp.status_code
                read_result["body"] = resp.json()
            except Exception as exc:  # noqa: BLE001 — collected for assertion
                read_errors.append(exc)

        reader = threading.Thread(target=_read)
        reader.start()

        # The read reaches the locked read while update() is parked inside its
        # deregister->register critical section; it must wait for the critical
        # section to finish rather than observe the half-deregistered state.
        assert read_entered.wait(timeout=5), "read never reached the controller"
        assert read_result == {} and read_errors == [], (
            "read completed during update()'s deregister->register critical section"
        )

        release.set()
        reader.join(timeout=10)
        updater.join(timeout=10)
        assert not reader.is_alive(), "API read did not finish within timeout"
        assert not updater.is_alive(), "update did not finish within timeout"
        assert update_errors == [] and read_errors == []

        # Consistent post-update view: 200, pipeline present, YAML is v2.
        assert read_result["status_code"] == 200
        assert read_result["body"]["name"] == "my-manual"
        assert "updated-v2" in read_result["body"]["yaml"]

        ctrl.stop()