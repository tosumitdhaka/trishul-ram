"""Unit tests for the worker agent server (tram/agent/server.py)."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from tram.agent.server import (
    ActiveRun,
    WorkerState,
    _active_run_status,
    _emit_stats_once,
    _post_run_complete,
    _post_stats,
    create_worker_app,
    create_worker_ingress_app,
)

# ── YAML fixture ───────────────────────────────────────────────────────────

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


# ── WorkerState unit tests ─────────────────────────────────────────────────


class TestWorkerState:
    def _make_run(self, run_id="r1"):
        return ActiveRun(
            run_id=run_id,
            pipeline_name="p",
            schedule_type="batch",
            started_at="2026-01-01T00:00:00+00:00",
        )

    def test_add_and_get(self):
        s = WorkerState(worker_id="w0", manager_url="")
        run = self._make_run("abc")
        s.add(run)
        assert s.get("abc") is run

    def test_remove(self):
        s = WorkerState(worker_id="w0", manager_url="")
        s.add(self._make_run("abc"))
        s.remove("abc")
        assert s.get("abc") is None

    def test_remove_missing_is_noop(self):
        s = WorkerState(worker_id="w0", manager_url="")
        s.remove("nonexistent")  # must not raise

    def test_snapshot_is_copy(self):
        s = WorkerState(worker_id="w0", manager_url="")
        s.add(self._make_run("a"))
        snap = s.snapshot()
        s.remove("a")
        assert len(snap) == 1   # snapshot unaffected by later remove

    def test_stats_stop_event_present(self):
        s = WorkerState(worker_id="w0", manager_url="")
        assert s.stats_stop.is_set() is False


# ── _post_run_complete unit tests ──────────────────────────────────────────


class TestPostRunComplete:
    def test_no_op_when_url_empty(self):
        # Should not raise and should not attempt any HTTP call
        _post_run_complete("", "r1", "p", "w0", "success", 0, 0, 0, 0, None)

    def test_posts_payload(self, respx_mock=None):
        captured = {}
        started_at = "2026-04-16T09:00:00+00:00"
        finished_at = "2026-04-16T09:05:00+00:00"

        def _fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            captured["headers"] = kwargs.get("headers")
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _fake_post
            mock_client_cls.return_value = mock_client

            _post_run_complete(
                "http://manager/api/internal/run-complete",
                "run-42", "my-pipe", "worker-7", "success", 10, 8, 1024, 768, None,
                started_at=started_at,
                finished_at=finished_at,
            )

        assert captured["url"] == "http://manager/api/internal/run-complete"
        assert captured["json"]["run_id"] == "run-42"
        assert captured["json"]["worker_id"] == "worker-7"
        assert captured["json"]["status"] == "success"
        assert captured["json"]["records_in"] == 10
        assert captured["json"]["bytes_in"] == 1024
        assert captured["json"]["started_at"] == started_at
        assert captured["json"]["finished_at"] == finished_at
        # No key configured → no headers sent (harmless to the unauthenticated endpoint)
        assert captured["headers"] is None

    def test_posts_api_key_header_when_configured(self):
        """When TRAM_API_KEY is set on the worker, callbacks carry X-API-Key."""
        captured = {}

        def _fake_post(url, **kwargs):
            captured["headers"] = kwargs.get("headers")
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _fake_post
            mock_client_cls.return_value = mock_client

            _post_run_complete(
                "http://manager/api/internal/run-complete",
                "run-42", "my-pipe", "worker-7", "success", 10, 8, 1024, 768, None,
                api_key="secret",
            )

        assert captured["headers"] == {"X-API-Key": "secret"}

    def test_posts_stats_api_key_header(self):
        """_post_stats forwards X-API-Key when the worker has a key."""
        captured = {}

        def _fake_post(url, **kwargs):
            captured["headers"] = kwargs.get("headers")
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _fake_post
            mock_client_cls.return_value = mock_client

            _post_stats("http://manager/api/internal/pipeline-stats", {"run_id": "r1"}, api_key="secret")

        assert captured["headers"] == {"X-API-Key": "secret"}

    def test_swallows_http_error(self):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = Exception("connection refused")
            mock_client_cls.return_value = mock_client

            # Must not raise
            _post_run_complete("http://bad-host/run-complete", "r", "p", "w0", "error", 0, 0, 0, 0, "boom")


# ── _post_stats unit tests (plan D.6) ──────────────────────────────────────


class TestPostStats:
    """Heartbeat failures must be visible: WARNING log with pipeline context,
    a MGR_STATS_MISSED_TOTAL increment, and consecutive-miss tracking."""

    def _failing_client(self):
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = ConnectionError("connection refused")
        return mock_client

    def test_failure_logs_warning_with_pipeline_context_and_increments_metric(self, caplog):
        payload = {
            "worker_id": "w0",
            "pipeline_name": "pipe-a",
            "run_id": "run-1",
        }
        with patch("httpx.Client", return_value=self._failing_client()), \
             patch("tram.metrics.registry.MGR_STATS_MISSED_TOTAL") as mock_metric:
            with caplog.at_level(logging.WARNING, logger="tram.agent.server"):
                _post_stats(
                    "http://manager/api/internal/pipeline-stats",
                    payload,
                    api_key="secret",
                )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        rec = warnings[0]
        assert rec.message == "pipeline-stats callback failed"
        assert rec.pipeline == "pipe-a"
        assert rec.run_id == "run-1"
        assert rec.worker_id == "w0"
        assert rec.consecutive_misses == 1
        assert "connection refused" in rec.error
        # no DEBUG-level swallow anymore
        assert all(r.levelno >= logging.WARNING for r in caplog.records)
        mock_metric.labels.assert_called_once_with(worker_id="w0")
        mock_metric.labels.return_value.inc.assert_called_once_with()

    def test_failure_tracks_consecutive_misses_and_resets_on_success(self, caplog):
        failures = {"n": 0}

        def _post(url, **kwargs):
            failures["n"] += 1
            if failures["n"] <= 2 or failures["n"] == 4:
                raise ConnectionError("refused")
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = _post

        payload = {"worker_id": "w9", "pipeline_name": "pipe-a", "run_id": "run-1"}
        with patch("httpx.Client", return_value=mock_client), \
             patch("tram.metrics.registry.MGR_STATS_MISSED_TOTAL") as mock_metric:
            with caplog.at_level(logging.WARNING, logger="tram.agent.server"):
                _post_stats("http://mgr/pipeline-stats", payload)
                _post_stats("http://mgr/pipeline-stats", payload)
                _post_stats("http://mgr/pipeline-stats", payload)   # success → reset
                _post_stats("http://mgr/pipeline-stats", payload)   # failure again

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert [r.consecutive_misses for r in warnings] == [1, 2, 1]
        assert mock_metric.labels.return_value.inc.call_count == 3


# ── FastAPI endpoint tests ─────────────────────────────────────────────────


def _make_client(worker_id="test-worker", manager_url=""):
    app = create_worker_app(worker_id=worker_id, manager_url=manager_url)
    return TestClient(app, raise_server_exceptions=True)


class TestHealthEndpoint:
    def test_returns_ok(self):
        client = _make_client()
        resp = client.get("/agent/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["worker_id"] == "test-worker"
        assert data["active_runs"] == 0
        assert data["ingress_up"] is True

    def test_reports_ingress_down_when_thread_dead(self):
        app = create_worker_app(worker_id="w0", manager_url="")
        app.state.ingress_thread = MagicMock(is_alive=MagicMock(return_value=False))
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/agent/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["ingress_up"] is False


class TestStatusEndpoint:
    def test_empty_initially(self):
        client = _make_client()
        resp = client.get("/agent/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] == []
        assert data["streams"] == []

    def test_status_includes_live_stats_snapshot(self):
        app = create_worker_app(worker_id="w0", manager_url="")
        state: WorkerState = app.state.worker
        run = ActiveRun(
            run_id="s1",
            pipeline_name="pipe-a",
            schedule_type="stream",
            started_at="2026-04-27T00:00:00+00:00",
        )
        from tram.agent.metrics import PipelineStats

        run.stats = PipelineStats(run_id="s1", pipeline_name="pipe-a", schedule_type="stream")
        run.stats.increment(records_in=12, records_out=8, bytes_in=120, bytes_out=80)
        state.add(run)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/agent/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["worker_id"] == "w0"
        assert data["active_runs"] == 1
        assert data["running_pipelines"] == ["pipe-a"]
        assert data["streams"][0]["run_id"] == "s1"
        assert data["streams"][0]["stats"]["records_out"] == 8


class TestConfigSha256:
    """D.2 §6.1: the dispatched YAML is fingerprinted and exposed in status."""

    def test_active_run_status_includes_config_sha256(self):
        run = ActiveRun(
            run_id="s1",
            pipeline_name="pipe-a",
            schedule_type="stream",
            started_at="2026-01-01T00:00:00+00:00",
            config_sha256="0123456789abcdef",
        )
        now = datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)
        status = _active_run_status(run, "w0", now)
        assert status["config_sha256"] == "0123456789abcdef"

    def test_active_run_status_defaults_to_empty(self):
        run = ActiveRun(
            run_id="s1",
            pipeline_name="pipe-a",
            schedule_type="stream",
            started_at="2026-01-01T00:00:00+00:00",
        )
        status = _active_run_status(run, "w0", datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC))
        assert status["config_sha256"] == ""

    def test_run_endpoint_hashes_dispatched_yaml_and_exposes_it(self):
        expected = hashlib.sha256(_MINIMAL_YAML.encode()).hexdigest()[:16]
        stopped = threading.Event()

        def _fake_stream_run(config, stop_event, stats=None, config_sha256=""):
            stop_event.wait(timeout=5)
            stopped.set()

        with patch(
            "tram.pipeline.executor.PipelineExecutor.stream_run",
            side_effect=_fake_stream_run,
        ):
            client = _make_client(worker_id="w0", manager_url="")
            resp = client.post("/agent/run", json={
                "pipeline_name": "test-pipe",
                "yaml_text": _MINIMAL_YAML,
                "run_id": "r-hash-1",
                "schedule_type": "stream",
            })
            assert resp.status_code == 202

            status = client.get("/agent/status").json()
            items = [i for i in status["streams"] if i["run_id"] == "r-hash-1"]
            assert len(items) == 1
            assert items[0]["config_sha256"] == expected

            client.post("/agent/stop", json={"pipeline_name": "test-pipe", "run_id": "r-hash-1"})
            assert stopped.wait(timeout=3)


def _route_paths(app) -> set:
    """Flatten route paths across FastAPI versions.

    fastapi>=0.141 represents ``include_router`` on the app as an
    ``_IncludedRouter`` wrapper (no ``.path``) holding the original router;
    older versions flatten the included routes directly onto ``app.routes``.
    """
    paths = set()

    def _walk(routes):
        for route in routes:
            path = getattr(route, "path", None)
            if path is not None:
                paths.add(path)
            included = getattr(route, "original_router", None)
            if included is not None:
                _walk(included.routes)

    _walk(app.routes)
    return paths


class TestIngressApp:
    def test_create_worker_ingress_app_has_no_agent_routes(self):
        app = create_worker_ingress_app(worker_id="w0")
        route_paths = _route_paths(app)

        assert "/webhooks/{path:path}" in route_paths
        assert "/agent/run" not in route_paths
        assert "/agent/stop" not in route_paths
        assert "/agent/status" not in route_paths


class TestStopEndpoint:
    def test_404_for_unknown_run(self):
        client = _make_client()
        resp = client.post("/agent/stop", json={"pipeline_name": "p", "run_id": "nope"})
        assert resp.status_code == 404

    def test_sets_stop_event(self):
        app = create_worker_app(worker_id="w0", manager_url="")
        state: WorkerState = app.state.worker
        # Inject a fake active run directly
        stop_ev = threading.Event()
        run = ActiveRun(
            run_id="r99",
            pipeline_name="p",
            schedule_type="stream",
            started_at="2026-01-01T00:00:00+00:00",
            stop_event=stop_ev,
        )
        state.add(run)

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/agent/stop", json={"pipeline_name": "p", "run_id": "r99"})
        assert resp.status_code == 200
        assert resp.json()["stopping"] is True
        assert stop_ev.is_set()


class TestRunEndpoint:
    def _patch_executor(self, records_in=5, records_out=5, status="success", error=None):
        """Return a context manager that patches PipelineExecutor.batch_run."""
        from tram.core.context import RunResult, RunStatus

        mock_result = RunResult(
            run_id="r1",
            pipeline_name="test-pipe",
            status=RunStatus(status),
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            records_in=records_in,
            records_out=records_out,
            records_skipped=0,
            error=error,
        )

        return patch(
            "tram.pipeline.executor.PipelineExecutor.batch_run",
            return_value=mock_result,
        )

    def test_422_on_bad_yaml(self):
        client = _make_client()
        resp = client.post("/agent/run", json={
            "pipeline_name": "bad",
            "yaml_text": "not: valid: yaml: pipeline",
            "run_id": "r1",
        })
        assert resp.status_code == 422

    def test_409_on_duplicate_run_id(self):
        app = create_worker_app(worker_id="w0", manager_url="")
        state: WorkerState = app.state.worker
        # Pre-seed a run with the same run_id
        state.add(ActiveRun(
            run_id="dup",
            pipeline_name="p",
            schedule_type="batch",
            started_at="2026-01-01T00:00:00+00:00",
        ))
        client = TestClient(app)
        resp = client.post("/agent/run", json={
            "pipeline_name": "test-pipe",
            "yaml_text": _MINIMAL_YAML,
            "run_id": "dup",
        })
        assert resp.status_code == 409

    def test_batch_run_accepted_and_completes(self):
        callback_calls = []

        def _fake_callback(url, **kwargs):
            callback_calls.append(kwargs.get("json", {}))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with self._patch_executor(records_in=3, records_out=3):
            with patch("httpx.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.__enter__ = lambda s: mock_client
                mock_client.__exit__ = MagicMock(return_value=False)
                mock_client.post.side_effect = _fake_callback
                mock_client_cls.return_value = mock_client

                client = _make_client(
                    worker_id="w0",
                    manager_url="http://manager",
                )
                resp = client.post("/agent/run", json={
                    "pipeline_name": "test-pipe",
                    "yaml_text": _MINIMAL_YAML,
                    "run_id": "r-batch-1",
                    "schedule_type": "batch",
                })
                assert resp.status_code == 202
                assert resp.json()["accepted"] is True

                # Give the background thread time to finish
                time.sleep(0.3)

        stats_calls = [c for c in callback_calls if c.get("is_final") is True]
        complete_calls = [c for c in callback_calls if c.get("status") == "success"]
        assert len(stats_calls) == 1
        assert len(complete_calls) == 1
        assert complete_calls[0]["run_id"] == "r-batch-1"
        assert complete_calls[0]["records_in"] == 3
        assert complete_calls[0]["started_at"]
        assert complete_calls[0]["finished_at"]

    def test_stream_run_accepted_and_stops(self):
        """Stream run: POST /agent/run then POST /agent/stop signals completion."""
        stopped = threading.Event()

        def _fake_stream_run(config, stop_event, stats=None, config_sha256=""):
            # Block until stop is requested (simulates a real stream)
            if stats is not None:
                stats.increment(records_in=7, records_out=6, skipped=1, bytes_in=700, bytes_out=600)
            stop_event.wait(timeout=5)
            stopped.set()

        with patch(
            "tram.pipeline.executor.PipelineExecutor.stream_run",
            side_effect=_fake_stream_run,
        ):
            callback_calls = []

            def _fake_callback(url, **kwargs):
                callback_calls.append(kwargs.get("json", {}))
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                return resp

            with patch("httpx.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.__enter__ = lambda s: mock_client
                mock_client.__exit__ = MagicMock(return_value=False)
                mock_client.post.side_effect = _fake_callback
                mock_client_cls.return_value = mock_client

                client = _make_client(worker_id="w0", manager_url="http://manager")
                resp = client.post("/agent/run", json={
                    "pipeline_name": "test-pipe",
                    "yaml_text": _MINIMAL_YAML,
                    "run_id": "r-stream-1",
                    "schedule_type": "stream",
                })
                assert resp.status_code == 202

                stop_resp = client.post("/agent/stop", json={
                    "pipeline_name": "test-pipe",
                    "run_id": "r-stream-1",
                })
                assert stop_resp.status_code == 200
                assert stop_resp.json()["stopping"] is True

                stopped.wait(timeout=3)
                assert stopped.is_set()
                time.sleep(0.2)

        assert len(callback_calls) == 1
        assert callback_calls[0]["started_at"]
        assert callback_calls[0]["finished_at"]
        assert callback_calls[0]["records_in"] == 7
        assert callback_calls[0]["records_out"] == 6
        assert callback_calls[0]["records_skipped"] == 1
        assert callback_calls[0]["bytes_in"] == 700
        assert callback_calls[0]["bytes_out"] == 600

    def test_explicit_callback_url_takes_precedence(self):
        """callback_url in RunRequest overrides the manager_url-derived URL."""
        callback_calls = []

        def _fake_callback(url, **kwargs):
            callback_calls.append(url)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with self._patch_executor():
            with patch("httpx.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.__enter__ = lambda s: mock_client
                mock_client.__exit__ = MagicMock(return_value=False)
                mock_client.post.side_effect = _fake_callback
                mock_client_cls.return_value = mock_client

                # manager_url points somewhere, but explicit callback_url should win
                client = _make_client(worker_id="w0", manager_url="http://manager")
                client.post("/agent/run", json={
                    "pipeline_name": "test-pipe",
                    "yaml_text": _MINIMAL_YAML,
                    "run_id": "r-cb-1",
                    "schedule_type": "batch",
                    "callback_url": "http://custom-host/custom-path",
                })
                time.sleep(0.3)

        assert "http://custom-host/custom-path" in callback_calls
        assert "http://custom-host/pipeline-stats" in callback_calls


class TestStatsHelpers:
    def test_emit_stats_once_posts_snapshot(self):
        state = WorkerState(worker_id="w0", manager_url="http://manager")
        run = ActiveRun(
            run_id="run-1",
            pipeline_name="pipe-a",
            schedule_type="stream",
            started_at="2026-04-17T12:00:00+00:00",
            stats_url="http://manager/api/internal/pipeline-stats",
        )
        assert run.stats is None
        from tram.agent.metrics import PipelineStats
        run.stats = PipelineStats(run_id="run-1", pipeline_name="pipe-a", schedule_type="stream")
        run.stats.increment(records_in=5, bytes_in=100, errors=["boom"])
        state.add(run)

        captured = {}

        def _fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _fake_post
            mock_client_cls.return_value = mock_client

            _emit_stats_once(state)

        assert captured["url"] == "http://manager/api/internal/pipeline-stats"
        assert captured["json"]["run_id"] == "run-1"
        assert captured["json"]["records_in"] == 5
        assert captured["json"]["error_count"] == 1
        assert run.stats.errors_last_window == []

    def test_emit_stats_once_sends_api_key_header(self):
        state = WorkerState(worker_id="w0", manager_url="http://manager", api_key="secret")
        run = ActiveRun(
            run_id="run-1",
            pipeline_name="pipe-a",
            schedule_type="stream",
            started_at="2026-04-17T12:00:00+00:00",
            stats_url="http://manager/api/internal/pipeline-stats",
        )
        from tram.agent.metrics import PipelineStats
        run.stats = PipelineStats(run_id="run-1", pipeline_name="pipe-a", schedule_type="stream")
        state.add(run)

        captured = {}

        def _fake_post(url, **kwargs):
            captured["headers"] = kwargs.get("headers")
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _fake_post
            mock_client_cls.return_value = mock_client

            _emit_stats_once(state)

        assert captured["headers"] == {"X-API-Key": "secret"}


# ── Agent API auth middleware (warn/enforce) ──────────────────────────────


class TestAgentApiAuth:
    def test_warn_mode_serves_without_key(self, monkeypatch, caplog):
        """Default warn mode: /agent/* without a key is served and logged."""
        import logging

        caplog.set_level(logging.WARNING, logger="tram.api.middleware")
        monkeypatch.setenv("TRAM_API_KEY", "secret")
        monkeypatch.setenv("TRAM_INTERNAL_AUTH_MODE", "warn")
        client = _make_client()
        resp = client.get("/agent/status")
        assert resp.status_code == 200
        assert any("missing or invalid API key" in rec.getMessage() for rec in caplog.records)

    def test_enforce_mode_requires_key(self, monkeypatch):
        monkeypatch.setenv("TRAM_API_KEY", "secret")
        monkeypatch.setenv("TRAM_INTERNAL_AUTH_MODE", "enforce")
        client = _make_client()

        # Missing key → 401
        assert client.get("/agent/status").status_code == 401
        # Correct key → 200
        resp = client.get("/agent/status", headers={"X-API-Key": "secret"})
        assert resp.status_code == 200

    def test_probe_exempt_in_enforce_mode(self, monkeypatch):
        """K8s probes hit /agent/health keyless — always exempt."""
        monkeypatch.setenv("TRAM_API_KEY", "secret")
        monkeypatch.setenv("TRAM_INTERNAL_AUTH_MODE", "enforce")
        client = _make_client()
        assert client.get("/agent/health").status_code == 200

    def test_no_key_configured_passes(self):
        """No TRAM_API_KEY on the worker → everything passes."""
        client = _make_client()
        assert client.get("/agent/status").status_code == 200


class TestStatefulTransformWiring:
    """F.1 §3.2b: worker runs reach the transform-state blob via the manager's
    internal API, and the D.2 config fingerprint is passed to the executor."""

    def test_batch_run_uses_http_state_store_and_config_sha(self):
        from datetime import UTC, datetime
        from unittest.mock import patch

        from tram.core.context import RunResult, RunStatus
        from tram.pipeline.state_store import HttpTransformStateStore

        captured = {}

        def _fake_init(self, file_tracker=None, state_store=None):
            captured["state_store"] = state_store

        mock_result = RunResult(
            run_id="r-state-1",
            pipeline_name="test-pipe",
            status=RunStatus.SUCCESS,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            records_in=0,
            records_out=0,
            records_skipped=0,
        )

        expected_sha = hashlib.sha256(_MINIMAL_YAML.encode()).hexdigest()[:16]

        with patch("tram.pipeline.executor.PipelineExecutor.__init__", _fake_init), \
             patch("tram.agent.assets.sync_assets"), \
             patch(
                 "tram.pipeline.executor.PipelineExecutor.batch_run",
                 return_value=mock_result,
             ) as batch_mock:
            client = _make_client(worker_id="w0", manager_url="http://mgr:8765")
            resp = client.post("/agent/run", json={
                "pipeline_name": "test-pipe",
                "yaml_text": _MINIMAL_YAML,
                "run_id": "r-state-1",
                "schedule_type": "batch",
            })
            assert resp.status_code == 202

            # The executor receives an HttpTransformStateStore wired to the
            # manager URL (batch thread completes async; poll briefly).
            deadline = time.time() + 3
            while time.time() < deadline and "state_store" not in captured:
                time.sleep(0.02)

        store = captured.get("state_store")
        assert isinstance(store, HttpTransformStateStore)
        assert store.manager_url == "http://mgr:8765"
        deadline = time.time() + 3
        while time.time() < deadline and not batch_mock.called:
            time.sleep(0.02)
        assert batch_mock.called
        kwargs = batch_mock.call_args.kwargs
        assert kwargs["config_sha256"] == expected_sha

    def test_no_state_store_without_manager_url(self):
        from datetime import UTC, datetime
        from unittest.mock import patch

        from tram.core.context import RunResult, RunStatus

        captured = {}

        def _fake_init(self, file_tracker=None, state_store=None):
            captured["state_store"] = state_store

        mock_result = RunResult(
            run_id="r-state-2",
            pipeline_name="test-pipe",
            status=RunStatus.SUCCESS,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            records_in=0,
            records_out=0,
            records_skipped=0,
        )

        with patch("tram.pipeline.executor.PipelineExecutor.__init__", _fake_init), \
             patch(
                 "tram.pipeline.executor.PipelineExecutor.batch_run",
                 return_value=mock_result,
             ):
            client = _make_client(worker_id="w0", manager_url="")
            resp = client.post("/agent/run", json={
                "pipeline_name": "test-pipe",
                "yaml_text": _MINIMAL_YAML,
                "run_id": "r-state-2",
                "schedule_type": "batch",
            })
            assert resp.status_code == 202
            deadline = time.time() + 3
            while time.time() < deadline and "state_store" not in captured:
                time.sleep(0.02)

        assert captured.get("state_store") is None

    def test_run_request_flush_forwarded_to_executor(self):
        """F.1 §5: RunRequest.flush (default false) reaches executor.batch_run."""
        from datetime import UTC, datetime
        from unittest.mock import patch

        from tram.core.context import RunResult, RunStatus

        mock_result = RunResult(
            run_id="r-flush-1",
            pipeline_name="test-pipe",
            status=RunStatus.SUCCESS,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            records_in=0,
            records_out=0,
            records_skipped=0,
        )
        captured = {}

        def _fake_batch_run(self, config, run_id=None, stats=None,
                            config_sha256="", flush=False):
            captured["flush"] = flush
            return mock_result

        with patch("tram.pipeline.executor.PipelineExecutor.__init__", lambda self, **kw: None), \
             patch("tram.agent.assets.sync_assets"), \
             patch(
                 "tram.pipeline.executor.PipelineExecutor.batch_run",
                 _fake_batch_run,
             ):
            client = _make_client(worker_id="w0", manager_url="")
            resp = client.post("/agent/run", json={
                "pipeline_name": "test-pipe",
                "yaml_text": _MINIMAL_YAML,
                "run_id": "r-flush-1",
                "schedule_type": "batch",
                "flush": True,
            })
            assert resp.status_code == 202
            deadline = time.time() + 3
            while time.time() < deadline and not captured:
                time.sleep(0.02)

        assert captured.get("flush") is True

    def test_run_request_flush_defaults_false(self):
        """Omitting flush on the dispatch envelope is a normal (non-flush) run."""
        from datetime import UTC, datetime
        from unittest.mock import patch

        from tram.core.context import RunResult, RunStatus

        mock_result = RunResult(
            run_id="r-norm-1",
            pipeline_name="test-pipe",
            status=RunStatus.SUCCESS,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            records_in=0,
            records_out=0,
            records_skipped=0,
        )
        captured = {}

        def _fake_batch_run(self, config, run_id=None, stats=None,
                            config_sha256="", flush=False):
            captured["flush"] = flush
            return mock_result

        with patch("tram.pipeline.executor.PipelineExecutor.__init__", lambda self, **kw: None), \
             patch("tram.agent.assets.sync_assets"), \
             patch(
                 "tram.pipeline.executor.PipelineExecutor.batch_run",
                 _fake_batch_run,
             ):
            client = _make_client(worker_id="w0", manager_url="")
            resp = client.post("/agent/run", json={
                "pipeline_name": "test-pipe",
                "yaml_text": _MINIMAL_YAML,
                "run_id": "r-norm-1",
                "schedule_type": "batch",
            })
            assert resp.status_code == 202
            deadline = time.time() + 3
            while time.time() < deadline and not captured:
                time.sleep(0.02)

        assert captured.get("flush") is False

    def test_flush_chain_end_to_end_clears_state(self):
        """F.1 §5 E2E: the ?flush=true dispatch envelope → /agent/run → the
        REAL executor runs close(flush=True) → the hydrated open window is
        emitted as a partial and the state PUT to the manager records the
        cleared windows."""
        import json as _json
        import textwrap

        import httpx

        from tram.pipeline.executor import PipelineExecutor
        from tram.pipeline.state_store import HttpTransformStateStore

        yaml_text = textwrap.dedent("""\
            name: flush-pipe
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
            transforms:
              - type: window_aggregate
                window_seconds: 900
                allowed_lateness_seconds: 60
                timestamp_field: [_polled_at, timestamp]
                group_by: [_index]
                operations:
                  mean_rate: "avg:rate"
                flush_on_close: true
        """)
        sha = hashlib.sha256(yaml_text.encode()).hexdigest()[:16]
        start = int(datetime(2026, 9, 16, 9, 0, tzinfo=UTC).timestamp())
        end = int(datetime(2026, 9, 16, 9, 15, tzinfo=UTC).timestamp())
        # A pre-hydrated open 09:00–09:15 window (watermark 09:09:00 < end).
        open_window = {
            "max_ts": float(start + 600),
            "windows": {
                "1": {str(end): {
                    "start": start, "end": end, "sample_count": 1,
                    "group_values": ["1"],
                    "acc": {"mean_rate": {"sum": 100.0, "count": 1}},
                }},
            },
        }
        puts = []

        def _state_handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={
                    "state": {"window_aggregate:0": open_window},
                    "config_sha256": sha,
                })
            puts.append(_json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        store = HttpTransformStateStore(
            "http://mgr:8765", "", transport=httpx.MockTransport(_state_handler)
        )

        ser_out = MagicMock()
        ser_out.serialize.side_effect = lambda recs: _json.dumps(recs).encode()
        mock_source = MagicMock()
        mock_source.read.return_value = iter([])  # the run itself reads nothing

        with patch("tram.pipeline.state_store.HttpTransformStateStore", return_value=store), \
             patch.object(PipelineExecutor, "_build_source", return_value=mock_source), \
             patch.object(PipelineExecutor, "_build_sinks",
                          return_value=[(MagicMock(), None, [])]), \
             patch.object(PipelineExecutor, "_build_serializer_in",
                          return_value=MagicMock()), \
             patch.object(PipelineExecutor, "_build_serializer_out",
                          return_value=ser_out), \
             patch("tram.agent.assets.sync_assets"), \
             patch("tram.agent.server._post_run_complete"), \
             patch("tram.agent.server._post_stats"):
            client = _make_client(worker_id="w0", manager_url="http://mgr:8765")
            resp = client.post("/agent/run", json={
                "pipeline_name": "flush-pipe",
                "yaml_text": yaml_text,
                "run_id": "r-flush-chain",
                "schedule_type": "batch",
                "flush": True,
            })
            assert resp.status_code == 202
            # The batch thread is async — poll for the state PUT.
            deadline = time.time() + 5
            while time.time() < deadline and not puts:
                time.sleep(0.02)

        assert puts, "the flush run must PUT the cleared state blob"
        saved = puts[0]["state"]["window_aggregate:0"]
        assert saved["windows"] == {}
        # The hydrated open window was emitted as a partial before the clear.
        assert ser_out.serialize.call_args is not None
        partial = ser_out.serialize.call_args[0][0][0]
        assert partial["window_complete"] is False
        assert partial["mean_rate"] == 100.0
        assert partial["_index"] == "1"


# ── GH #39: skip_processed fail-loud in worker mode ─────────────────────────


_SKIP_PROCESSED_YAML = """\
name: skip-pipe
schedule:
  type: manual
source:
  type: local
  path: /tmp/in
  skip_processed: true
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""


class TestWorkerSkipProcessedFailLoud:
    """GH #39 / code-review A1: the worker agent is a stateless executor with
    no per-worker DB, so its PipelineExecutor is built without a
    ProcessedFileTracker and skip_processed cannot be honored. Construction
    with a pipeline that requests skip_processed must fail loud — an ERROR log
    plus a degradation marker carried into the run-complete payload so the
    manager's run_history row records it — never a silent disable."""

    @staticmethod
    def _mock_result(records_in=5, records_out=5):
        from tram.core.context import RunResult, RunStatus

        return RunResult(
            run_id="r1",
            pipeline_name="skip-pipe",
            status=RunStatus.SUCCESS,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            records_in=records_in,
            records_out=records_out,
            records_skipped=0,
            error=None,
        )

    def _dispatch(self, client, yaml_text, run_id, schedule_type="batch"):
        return client.post("/agent/run", json={
            "pipeline_name": "skip-pipe",
            "yaml_text": yaml_text,
            "run_id": run_id,
            "schedule_type": schedule_type,
        })

    def _wait_for(self, collected, n=1, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline and len(collected) < n:
            time.sleep(0.02)
        assert len(collected) >= n

    def test_executor_built_without_file_tracker(self):
        """Pin construction: the worker-mode executor is built with
        file_tracker=None (no per-worker DB exists) — the exact condition the
        fail-loud guard keys on."""
        captured = {}

        def _fake_init(self, file_tracker=None, state_store=None):
            captured["file_tracker"] = file_tracker

        with patch("tram.pipeline.executor.PipelineExecutor.__init__", _fake_init), \
             patch("tram.agent.assets.sync_assets"), \
             patch("tram.pipeline.executor.PipelineExecutor.batch_run",
                   return_value=self._mock_result()):
            client = _make_client(worker_id="w0", manager_url="")
            resp = self._dispatch(client, _SKIP_PROCESSED_YAML, "r-sp-ctor")
            assert resp.status_code == 202
            self._wait_for(captured)

        assert captured["file_tracker"] is None

    def test_skip_processed_source_logs_loud_error(self, caplog):
        """Executor construction with skip_processed: true logs an ERROR — the
        degradation is loud, never silent."""
        with caplog.at_level(logging.ERROR, logger="tram.agent.server"), \
             patch("tram.pipeline.executor.PipelineExecutor.__init__",
                   lambda self, **kw: None), \
             patch("tram.agent.assets.sync_assets"), \
             patch("tram.pipeline.executor.PipelineExecutor.batch_run",
                   return_value=self._mock_result()):
            client = _make_client(worker_id="w0", manager_url="")
            resp = self._dispatch(client, _SKIP_PROCESSED_YAML, "r-sp-log")
            assert resp.status_code == 202

        errors = [r for r in caplog.records
                  if r.levelno == logging.ERROR
                  and getattr(r, "run_id", None) == "r-sp-log"]
        assert len(errors) == 1
        rec = errors[0]
        assert "skip_processed" in rec.message
        assert rec.pipeline == "skip-pipe"
        assert rec.run_id == "r-sp-log"
        assert rec.source == "local"

    def test_run_complete_payload_records_skip_processed_marker(self):
        """The degradation rides the run-complete payload errors so the
        manager's run_history row (errors_json) records it."""
        captured = []

        def _fake_callback(url, **kwargs):
            payload = kwargs.get("json", {})
            # Scope to THIS run: on CI, a leaked daemon thread from an
            # earlier test can post its delayed run-complete through this
            # globally-patched httpx.Client (slow DNS failure stretches the
            # thread past its own test's window). Stale payloads must not
            # inflate the assertions below.
            if payload.get("run_id") == "r-sp-payload":
                captured.append(payload)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch("tram.pipeline.executor.PipelineExecutor.__init__",
                   lambda self, **kw: None), \
             patch("tram.agent.assets.sync_assets"), \
             patch("tram.pipeline.executor.PipelineExecutor.batch_run",
                   return_value=self._mock_result()), \
             patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _fake_callback
            mock_client_cls.return_value = mock_client

            client = _make_client(worker_id="w0", manager_url="http://manager")
            resp = self._dispatch(client, _SKIP_PROCESSED_YAML, "r-sp-payload")
            assert resp.status_code == 202
            self._wait_for(captured, n=2)  # final-stats payload + run-complete

        complete = [c for c in captured if c.get("status") == "success"]
        assert len(complete) == 1
        assert any("skip_processed" in e for e in complete[0]["errors"])

    def test_no_fail_loud_without_skip_processed(self, caplog):
        """A source without skip_processed stays quiet — no ERROR, no marker."""
        captured = []

        def _fake_callback(url, **kwargs):
            payload = kwargs.get("json", {})
            # Same CI stale-callback guard as the payload test above.
            if payload.get("run_id") == "r-sp-none":
                captured.append(payload)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with caplog.at_level(logging.ERROR, logger="tram.agent.server"), \
             patch("tram.pipeline.executor.PipelineExecutor.__init__",
                   lambda self, **kw: None), \
             patch("tram.agent.assets.sync_assets"), \
             patch("tram.pipeline.executor.PipelineExecutor.batch_run",
                   return_value=self._mock_result()), \
             patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _fake_callback
            mock_client_cls.return_value = mock_client

            client = _make_client(worker_id="w0", manager_url="http://manager")
            resp = self._dispatch(client, _MINIMAL_YAML, "r-sp-none")
            assert resp.status_code == 202
            self._wait_for(captured, n=2)

        assert not any(
            "skip_processed" in r.getMessage() for r in caplog.records
        )
        complete = [c for c in captured if c.get("status") == "success"]
        assert len(complete) == 1
        assert complete[0]["errors"] == []

    def test_no_fail_loud_when_skip_processed_false(self, caplog):
        """Explicit skip_processed: false is the connector default — stays quiet."""
        yaml_text = _SKIP_PROCESSED_YAML.replace(
            "skip_processed: true", "skip_processed: false"
        )
        with caplog.at_level(logging.ERROR, logger="tram.agent.server"), \
             patch("tram.pipeline.executor.PipelineExecutor.__init__",
                   lambda self, **kw: None), \
             patch("tram.agent.assets.sync_assets"), \
             patch("tram.pipeline.executor.PipelineExecutor.batch_run",
                   return_value=self._mock_result()):
            client = _make_client(worker_id="w0", manager_url="")
            resp = self._dispatch(client, yaml_text, "r-sp-false")
            assert resp.status_code == 202

        assert not any(
            "skip_processed" in r.getMessage() for r in caplog.records
        )

    def test_stream_run_records_skip_processed_marker(self):
        """Stream dispatches fail loud too — the marker reaches the run-complete
        payload of a stream run."""
        stopped = threading.Event()

        def _fake_stream_run(config, stop_event, stats=None, config_sha256=""):
            stop_event.wait(timeout=5)
            stopped.set()

        captured = []

        def _fake_callback(url, **kwargs):
            payload = kwargs.get("json", {})
            # Same CI stale-callback guard as the payload test above.
            if payload.get("run_id") == "r-sp-stream":
                captured.append(payload)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch("tram.pipeline.executor.PipelineExecutor.__init__",
                    lambda self, **kw: None), \
             patch("tram.agent.assets.sync_assets"), \
             patch("tram.pipeline.executor.PipelineExecutor.stream_run",
                   side_effect=_fake_stream_run), \
             patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _fake_callback
            mock_client_cls.return_value = mock_client

            client = _make_client(worker_id="w0", manager_url="http://manager")
            resp = self._dispatch(client, _SKIP_PROCESSED_YAML, "r-sp-stream",
                                  schedule_type="stream")
            assert resp.status_code == 202
            client.post("/agent/stop", json={
                "pipeline_name": "skip-pipe",
                "run_id": "r-sp-stream",
            })
            assert stopped.wait(timeout=3)
            self._wait_for(captured)

        complete = [c for c in captured if c.get("status") == "success"]
        assert len(complete) == 1
        assert any("skip_processed" in e for e in complete[0]["errors"])

    def test_worker_executor_reprocesses_file_without_tracker(self, tmp_path):
        """Functional pin (GH #39): with no per-worker tracker, the same file
        is processed by BOTH runs of the same worker executor — skip_processed
        is not silently applied — and each run fails loud with the ERROR log
        plus the run-history marker."""
        import json as _json

        src = tmp_path / "in"
        dst = tmp_path / "out"
        src.mkdir()
        (src / "data.json").write_text(_json.dumps([{"x": 1}]))

        yaml_text = f"""\
name: skip-pipe
schedule:
  type: manual
source:
  type: local
  path: {src}
  skip_processed: true
serializer_in:
  type: json
sinks:
  - type: local
    path: {dst}
    filename_template: "skip-pipe_{{epoch_ms}}.bin"
"""
        completed = []

        def _capture_run_complete(*args, **kwargs):
            # Positional order: callback_url, run_id, ... — keep only this
            # test's runs so a stale callback from a leaked earlier-test
            # thread cannot inflate the count on CI. Keywords
            # (started_at/finished_at/api_key) are accepted and ignored.
            if len(args) > 1 and str(args[1]).startswith("r-sp-fn-"):
                completed.append(args)

        with patch("tram.agent.server._post_run_complete",
                   side_effect=_capture_run_complete), \
             patch("tram.agent.server._post_stats"):
            client = _make_client(worker_id="w0", manager_url="")
            for i in range(2):
                resp = self._dispatch(client, yaml_text, f"r-sp-fn-{i}")
                assert resp.status_code == 202
            self._wait_for(completed, n=2, timeout=5.0)

        assert len(completed) == 2
        for args in completed:
            # _post_run_complete positional order: callback_url, run_id,
            # pipeline_name, worker_id, status, records_in, records_out,
            # bytes_in, bytes_out, error, records_skipped, errors, ...
            assert args[4] == "success"
            assert args[5] == 1           # the file WAS processed (not skipped)
            assert any("skip_processed" in e for e in args[11])
        # One output file per run — the same source file was reprocessed.
        assert len(list(dst.glob("skip-pipe_*.bin"))) == 2
