"""Unit tests for WorkerPool (tram/agent/worker_pool.py)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from tram.agent.worker_pool import WorkerPool

# ── Helpers ────────────────────────────────────────────────────────────────


def _pool(*urls, manager_url="http://manager"):
    return WorkerPool(workers=list(urls), manager_url=manager_url, poll_interval=60)


def _mock_httpx_client(responses: dict):
    """Return a context manager that mocks httpx.Client.get / .post.

    ``responses`` maps URL (str) → dict payload (GET) or {"status_code": 2xx} (POST).
    Calls to unknown URLs raise ConnectionError.
    """
    mock_client = MagicMock()
    mock_client.__enter__ = lambda s: mock_client
    mock_client.__exit__ = MagicMock(return_value=False)

    def _get(url, **kwargs):
        if url not in responses:
            raise ConnectionError(f"No mock for {url}")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = responses[url]
        return resp

    def _post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        return resp

    mock_client.get.side_effect = _get
    mock_client.post.side_effect = _post
    return mock_client


# ── from_env ───────────────────────────────────────────────────────────────


class TestFromEnv:
    def test_returns_none_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("TRAM_WORKERS", raising=False)
        monkeypatch.delenv("TRAM_WORKER_REPLICAS", raising=False)
        result = WorkerPool.from_env()
        assert result is None

    def test_explicit_workers(self, monkeypatch):
        monkeypatch.setenv("TRAM_WORKER_URLS", "http://w0:8766, http://w1:8766")
        monkeypatch.delenv("TRAM_WORKER_REPLICAS", raising=False)
        pool = WorkerPool.from_env()
        assert pool is not None
        assert pool._workers == ["http://w0:8766", "http://w1:8766"]

    def test_k8s_headless_dns(self, monkeypatch):
        monkeypatch.delenv("TRAM_WORKERS", raising=False)
        monkeypatch.setenv("TRAM_WORKER_REPLICAS", "3")
        monkeypatch.setenv("TRAM_WORKER_SERVICE", "tram-worker")
        monkeypatch.setenv("TRAM_WORKER_NAMESPACE", "prod")
        monkeypatch.setenv("TRAM_WORKER_PORT", "8766")
        pool = WorkerPool.from_env()
        assert pool is not None
        assert len(pool._workers) == 3
        assert pool._workers[0] == "http://tram-worker-0.tram-worker.prod.svc.cluster.local:8766"
        assert pool._workers[2] == "http://tram-worker-2.tram-worker.prod.svc.cluster.local:8766"

    def test_explicit_takes_precedence_over_k8s(self, monkeypatch):
        monkeypatch.setenv("TRAM_WORKER_URLS", "http://explicit:8766")
        monkeypatch.setenv("TRAM_WORKER_REPLICAS", "3")
        pool = WorkerPool.from_env()
        assert pool is not None
        assert pool._workers == ["http://explicit:8766"]


# ── Health polling ─────────────────────────────────────────────────────────


class TestHealthPolling:
    def test_poll_marks_workers_ok(self):
        pool = _pool("http://w0:8766", "http://w1:8766")
        mock_client = _mock_httpx_client({
            "http://w0:8766/agent/health": {"ok": True, "active_runs": 2, "worker_id": "w0"},
            "http://w1:8766/agent/health": {"ok": True, "active_runs": 0, "worker_id": "w1"},
        })
        with patch("httpx.Client", return_value=mock_client):
            pool._poll_all()

        assert pool._health["http://w0:8766"]["ok"] is True
        assert pool._health["http://w0:8766"]["active_runs"] == 2
        assert pool._health["http://w1:8766"]["ok"] is True
        assert pool._health["http://w1:8766"]["active_runs"] == 0
        assert pool._worker_ids["w0"] == "http://w0:8766"
        assert pool._worker_ids["w1"] == "http://w1:8766"
        assert pool._url_to_worker_id["http://w0:8766"] == "w0"
        assert pool._url_to_worker_id["http://w1:8766"] == "w1"

    def test_poll_marks_down_worker_on_error(self):
        pool = _pool("http://w0:8766")
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = ConnectionError("refused")
        with patch("httpx.Client", return_value=mock_client):
            pool._poll_all()

        assert pool._health["http://w0:8766"]["ok"] is False

    def test_healthy_workers_excludes_down(self):
        pool = _pool("http://w0:8766", "http://w1:8766")
        pool._health["http://w0:8766"]["ok"] = False
        assert pool.healthy_workers() == ["http://w1:8766"]

    def test_least_loaded_picks_min_active(self):
        pool = _pool("http://w0:8766", "http://w1:8766", "http://w2:8766")
        pool._health["http://w0:8766"] = {"ok": True, "active_runs": 5}
        pool._health["http://w1:8766"] = {"ok": True, "active_runs": 1}
        pool._health["http://w2:8766"] = {"ok": True, "active_runs": 3}
        assert pool.least_loaded() == "http://w1:8766"

    def test_least_loaded_returns_none_when_all_down(self):
        pool = _pool("http://w0:8766")
        pool._health["http://w0:8766"]["ok"] = False
        assert pool.least_loaded() is None


# ── Dispatch ───────────────────────────────────────────────────────────────


class TestDispatch:
    def test_dispatch_returns_worker_url(self):
        pool = _pool("http://w0:8766")
        calls = []

        def _post(url, **kwargs):
            calls.append({"url": url, "json": kwargs.get("json")})
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = _post

        with patch("httpx.Client", return_value=mock_client):
            result = pool.dispatch("r1", "my-pipe", "yaml: ...", "batch")

        assert result == "http://w0:8766"
        assert calls[0]["url"] == "http://w0:8766/agent/run"
        assert calls[0]["json"]["run_id"] == "r1"
        assert calls[0]["json"]["schedule_type"] == "batch"

    def test_dispatch_uses_manager_url_for_callback(self):
        pool = _pool("http://w0:8766", manager_url="http://manager:8765")
        posted = {}

        def _post(url, **kwargs):
            posted["json"] = kwargs.get("json", {})
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = _post

        with patch("httpx.Client", return_value=mock_client):
            pool.dispatch("r2", "p", "yaml", "batch")

        assert posted["json"]["callback_url"] == "http://manager:8765/api/internal/run-complete"

    def test_dispatch_explicit_callback_url_takes_precedence(self):
        pool = _pool("http://w0:8766", manager_url="http://manager:8765")
        posted = {}

        def _post(url, **kwargs):
            posted["json"] = kwargs.get("json", {})
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = _post

        with patch("httpx.Client", return_value=mock_client):
            pool.dispatch("r3", "p", "yaml", "batch",
                          callback_url="http://custom/path")

        assert posted["json"]["callback_url"] == "http://custom/path"

    def test_dispatch_increments_active_runs(self):
        pool = _pool("http://w0:8766")
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = MagicMock(raise_for_status=MagicMock())

        with patch("httpx.Client", return_value=mock_client):
            pool.dispatch("r4", "p", "yaml", "batch")

        assert pool._health["http://w0:8766"]["active_runs"] == 1

    def test_dispatch_returns_none_when_no_healthy_workers(self):
        pool = _pool("http://w0:8766")
        pool._health["http://w0:8766"]["ok"] = False
        result = pool.dispatch("r5", "p", "yaml", "batch")
        assert result is None

    def test_dispatch_returns_none_on_http_error(self):
        pool = _pool("http://w0:8766")
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = ConnectionError("refused")
        with patch("httpx.Client", return_value=mock_client):
            result = pool.dispatch("r6", "p", "yaml", "batch")
        assert result is None

    def test_dispatch_to_worker_targets_specific_worker(self):
        pool = _pool("http://w0:8766", "http://w1:8766")
        calls = []

        def _post(url, **kwargs):
            calls.append((url, kwargs.get("json", {})))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = _post

        with patch("httpx.Client", return_value=mock_client):
            assert pool.dispatch_to_worker(
                "http://w1:8766",
                run_id="slot-r1",
                pipeline_name="pipe-a",
                yaml_text="yaml",
                schedule_type="stream",
            ) is True

        assert calls == [("http://w1:8766/agent/run", {
            "pipeline_name": "pipe-a",
            "yaml_text": "yaml",
            "run_id": "slot-r1",
            "schedule_type": "stream",
            "callback_url": "http://manager/api/internal/run-complete",
        })]

    def test_multi_dispatch_count_all_tracks_all_workers(self):
        from tram.models.pipeline import WorkersConfig

        pool = _pool("http://w0:8766", "http://w1:8766")
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = MagicMock(raise_for_status=MagicMock())

        with patch("httpx.Client", return_value=mock_client):
            result = pool.multi_dispatch(
                placement_group_id="pg1",
                pipeline_name="p",
                yaml_text="yaml",
                workers_cfg=WorkersConfig(count="all"),
                schedule_type="stream",
            )

        assert result.status == "running"
        assert result.accepted == ["http://w0:8766", "http://w1:8766"]
        assert result.run_ids == ["pg1-w0", "pg1-w1"]
        assert pool.workers_for_pipeline("p") == ["http://w0:8766", "http://w1:8766"]

    def test_resolve_rejects_unsupported_named_workers(self):
        from tram.models.pipeline import WorkersConfig

        pool = _pool("http://w0:8766")
        try:
            pool.resolve(WorkersConfig(worker_ids=["tram-worker-0"]))
        except NotImplementedError:
            pass
        else:
            raise AssertionError("Expected NotImplementedError for workers.list")


# ── stop_run ───────────────────────────────────────────────────────────────


class TestStopRun:
    def test_stop_run_calls_correct_worker(self):
        pool = _pool("http://w0:8766", "http://w1:8766")
        pool._assignments["stream-99"] = "http://w1:8766"

        calls = []

        def _post(url, **kwargs):
            calls.append(url)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = _post

        with patch("httpx.Client", return_value=mock_client):
            result = pool.stop_run("stream-99", "my-stream")

        assert result is True
        assert calls[0] == "http://w1:8766/agent/stop"

    def test_stop_run_returns_false_for_unknown_run(self):
        pool = _pool("http://w0:8766")
        result = pool.stop_run("unknown-run", "p")
        assert result is False

    def test_stop_pipeline_runs_stops_matching_runs_on_all_workers(self):
        pool = _pool("http://w0:8766", "http://w1:8766")
        calls = []

        def _get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if url == "http://w0:8766/agent/status":
                resp.json.return_value = {
                    "running": [],
                    "streams": [
                        {"run_id": "run-a", "pipeline": "pipe-a", "started_at": "now"},
                        {"run_id": "run-b", "pipeline": "other", "started_at": "now"},
                    ],
                }
            elif url == "http://w1:8766/agent/status":
                resp.json.return_value = {
                    "running": [{"run_id": "run-c", "pipeline": "pipe-a", "started_at": "now"}],
                    "streams": [],
                }
            else:
                raise ConnectionError(url)
            return resp

        def _post(url, **kwargs):
            calls.append((url, kwargs.get("json")))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = _get
        mock_client.post.side_effect = _post

        with patch("httpx.Client", return_value=mock_client):
            stopped = pool.stop_pipeline_runs("pipe-a")

        assert stopped == ["run-a", "run-c"]
        assert calls == [
            ("http://w0:8766/agent/stop", {"pipeline_name": "pipe-a", "run_id": "run-a"}),
            ("http://w1:8766/agent/stop", {"pipeline_name": "pipe-a", "run_id": "run-c"}),
        ]


# ── on_run_complete ────────────────────────────────────────────────────────


class TestOnRunComplete:
    def test_removes_assignment_and_decrements_count(self):
        pool = _pool("http://w0:8766")
        pool._assignments["r1"] = "http://w0:8766"
        pool._health["http://w0:8766"]["active_runs"] = 2

        pool.on_run_complete("r1")

        assert "r1" not in pool._assignments
        assert pool._health["http://w0:8766"]["active_runs"] == 1

    def test_active_runs_never_goes_below_zero(self):
        pool = _pool("http://w0:8766")
        pool._assignments["r1"] = "http://w0:8766"
        pool._health["http://w0:8766"]["active_runs"] = 0

        pool.on_run_complete("r1")

        assert pool._health["http://w0:8766"]["active_runs"] == 0

    def test_noop_for_unknown_run(self):
        pool = _pool("http://w0:8766")
        pool.on_run_complete("never-dispatched")  # must not raise
