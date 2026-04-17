"""Unit tests for the worker agent server (tram/agent/server.py)."""
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from tram.agent.server import ActiveRun, WorkerState, _post_run_complete, create_worker_app

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


# ── _post_run_complete unit tests ──────────────────────────────────────────


class TestPostRunComplete:
    def test_no_op_when_url_empty(self):
        # Should not raise and should not attempt any HTTP call
        _post_run_complete("", "r1", "p", "success", 0, 0, None)

    def test_posts_payload(self, respx_mock=None):
        captured = {}
        started_at = "2026-04-16T09:00:00+00:00"
        finished_at = "2026-04-16T09:05:00+00:00"

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

            _post_run_complete(
                "http://manager/api/internal/run-complete",
                "run-42", "my-pipe", "success", 10, 8, None,
                started_at=started_at,
                finished_at=finished_at,
            )

        assert captured["url"] == "http://manager/api/internal/run-complete"
        assert captured["json"]["run_id"] == "run-42"
        assert captured["json"]["status"] == "success"
        assert captured["json"]["records_in"] == 10
        assert captured["json"]["started_at"] == started_at
        assert captured["json"]["finished_at"] == finished_at

    def test_swallows_http_error(self):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = Exception("connection refused")
            mock_client_cls.return_value = mock_client

            # Must not raise
            _post_run_complete("http://bad-host/run-complete", "r", "p", "error", 0, 0, "boom")


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


class TestStatusEndpoint:
    def test_empty_initially(self):
        client = _make_client()
        resp = client.get("/agent/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] == []
        assert data["streams"] == []


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

        assert len(callback_calls) == 1
        assert callback_calls[0]["run_id"] == "r-batch-1"
        assert callback_calls[0]["status"] == "success"
        assert callback_calls[0]["records_in"] == 3
        assert callback_calls[0]["started_at"]
        assert callback_calls[0]["finished_at"]

    def test_stream_run_accepted_and_stops(self):
        """Stream run: POST /agent/run then POST /agent/stop signals completion."""
        stopped = threading.Event()

        def _fake_stream_run(config, stop_event):
            # Block until stop is requested (simulates a real stream)
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

        assert len(callback_calls) == 1
        assert callback_calls[0] == "http://custom-host/custom-path"
