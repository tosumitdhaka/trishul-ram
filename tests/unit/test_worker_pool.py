"""Unit tests for WorkerPool (tram/agent/worker_pool.py)."""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from tram.agent.worker_pool import (
    DISPATCH_ACCEPTED,
    DISPATCH_FAILED,
    DISPATCH_NO_CAPACITY,
    WorkerPool,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _pool(*urls, manager_url="http://manager"):
    return WorkerPool(workers=list(urls), manager_url=manager_url, poll_interval=60)


def _gated_fanout_client(urls: list[str], gate: threading.Event):
    """Return (mock_client, both_entered) for a parallel-fan-out assertion.

    Every probe blocks on ``gate`` until every worker has entered the probe
    call, then returns a healthy payload. If the fan-out is serial, the second
    probe can never enter while the first is blocked, so ``both_entered``
    times out.
    """
    started: dict[str, int] = {"n": 0}
    started_lock = threading.Lock()
    both_entered = threading.Event()

    def _get(url, **kwargs):
        with started_lock:
            started["n"] += 1
            if started["n"] >= len(urls):
                both_entered.set()
        assert gate.wait(5), "gate was not released"
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "ok": True,
            "active_runs": 0,
            "worker_id": url.rsplit("/", 1)[-1],
            "running": [],
            "streams": [],
            "running_pipelines": [],
        }
        return resp

    mock_client = MagicMock()
    mock_client.__enter__ = lambda s: mock_client
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.side_effect = _get
    return mock_client, both_entered


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

    def test_poll_marks_down_worker_after_consecutive_failures(self):
        pool = _pool("http://w0:8766")
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = ConnectionError("refused")
        with patch("httpx.Client", return_value=mock_client):
            pool._poll_all()
            pool._poll_all()

        assert pool._health["http://w0:8766"]["ok"] is False
        assert pool._health["http://w0:8766"]["failures"] == 2

    def test_single_failed_probe_does_not_mark_worker_down(self):
        pool = _pool("http://w0:8766")
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = ConnectionError("refused")
        with patch("httpx.Client", return_value=mock_client):
            pool._poll_all()

        assert pool._health["http://w0:8766"]["ok"] is True
        assert pool._health["http://w0:8766"]["failures"] == 1

    def test_failed_probe_recovering_next_poll_keeps_worker_healthy(self):
        pool = _pool("http://w0:8766")
        attempts = {"count": 0}

        def _get(url, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise ConnectionError("blip")
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"ok": True, "active_runs": 0, "worker_id": "w0"}
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = _get

        with patch("httpx.Client", return_value=mock_client):
            pool._poll_all()
            pool._poll_all()

        health = pool._health["http://w0:8766"]
        assert health["ok"] is True
        assert health["failures"] == 0

    def test_worker_marked_down_after_two_consecutive_failures_by_default(self):
        pool = _pool("http://w0:8766")
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = ConnectionError("refused")
        with patch("httpx.Client", return_value=mock_client):
            pool._poll_all()
            pool._poll_all()

        assert pool._health["http://w0:8766"]["ok"] is False

    def test_worker_recovers_after_debounce_threshold_was_reached(self):
        pool = _pool("http://w0:8766")
        attempts = {"count": 0}

        def _get(url, **kwargs):
            attempts["count"] += 1
            if attempts["count"] <= 2:
                raise ConnectionError("down")
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"ok": True, "active_runs": 0, "worker_id": "w0"}
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = _get

        with patch("httpx.Client", return_value=mock_client):
            pool._poll_all()
            pool._poll_all()
            assert pool._health["http://w0:8766"]["ok"] is False
            pool._poll_all()

        health = pool._health["http://w0:8766"]
        assert health["ok"] is True
        assert health["failures"] == 0

    def test_custom_failure_threshold_is_respected(self):
        pool = WorkerPool(
            workers=["http://w0:8766"],
            manager_url="http://manager",
            poll_interval=60,
            health_failures_to_down=3,
        )
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = ConnectionError("refused")
        with patch("httpx.Client", return_value=mock_client):
            pool._poll_all()
            pool._poll_all()
            assert pool._health["http://w0:8766"]["ok"] is True
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


# ── D.6: per-worker probes run concurrently ────────────────────────────────


class TestParallelFanout:
    """Plan D.6 — per-worker probes must run concurrently.

    Each test gates every worker probe on an Event and asserts that all probes
    entered the call while the first was still blocked. A serial loop can never
    satisfy that: the next probe only starts after the previous one returns.
    """

    def _run_gated(self, pool, target):
        gate = threading.Event()
        mock_client, both_entered = _gated_fanout_client(pool._workers, gate)
        with patch("httpx.Client", return_value=mock_client):
            worker = threading.Thread(target=target)
            worker.start()
            try:
                assert both_entered.wait(2), (
                    "probes were serialized: a later probe did not start "
                    "while the first was still blocked"
                )
            finally:
                gate.set()
                worker.join(5)
        assert not worker.is_alive()

    def test_poll_all_probes_workers_in_parallel(self):
        pool = _pool("http://w0:8766", "http://w1:8766")
        self._run_gated(pool, pool._poll_all)
        assert pool._health["http://w0:8766"]["ok"] is True
        assert pool._health["http://w1:8766"]["ok"] is True

    def test_status_probes_workers_in_parallel(self):
        pool = _pool("http://w0:8766", "http://w1:8766")
        result: dict[str, object] = {}
        self._run_gated(pool, lambda: result.setdefault("rows", pool.status()))
        assert len(result["rows"]) == 2

    def test_live_streams_probes_workers_in_parallel(self):
        pool = _pool("http://w0:8766", "http://w1:8766")
        result: dict[str, object] = {}
        self._run_gated(pool, lambda: result.setdefault("live", pool.live_streams()))
        assert result["live"] == []


# ── B.6: boot scan skips the health debounce ───────────────────────────────


class TestBootProbe:
    """WorkerPool.start() probes once; with the threshold-2 debounce a worker
    down at boot would otherwise report healthy for one poll interval and
    receive boot-time dispatches. The boot scan must mark first-probe failures
    down immediately (startup hysteresis window fix)."""

    def test_start_marks_unreachable_worker_down_immediately(self):
        pool = _pool("http://w0:8766")
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = ConnectionError("refused")

        with patch("httpx.Client", return_value=mock_client):
            pool.start()

        try:
            health = pool._health["http://w0:8766"]
            assert health["ok"] is False
            assert health["failures"] == 2  # straight to the debounce threshold
            assert pool.healthy_workers() == []
            # resolve/dispatch must never target the worker.
            from tram.models.pipeline import WorkersConfig
            assert pool.resolve(WorkersConfig(count=1)) == []
            outcome = pool.dispatch_with_result("boot-r1", "p", "yaml", "stream")
            assert outcome.outcome == DISPATCH_NO_CAPACITY
        finally:
            pool.stop()

    def test_start_keeps_healthy_worker_ok(self):
        pool = _pool("http://w0:8766")
        mock_client = _mock_httpx_client({
            "http://w0:8766/agent/health": {"ok": True, "active_runs": 0, "worker_id": "w0"},
        })
        with patch("httpx.Client", return_value=mock_client):
            pool.start()

        try:
            health = pool._health["http://w0:8766"]
            assert health["ok"] is True
            assert health["failures"] == 0
        finally:
            pool.stop()


# ── B.6: adopt_stream_assignment ───────────────────────────────────────────


class TestAdoptStreamAssignment:
    def test_records_assignment_and_pipeline_mapping(self):
        pool = _pool("http://w0:8766", "http://w1:8766")
        pool._health["http://w0:8766"]["active_runs"] = 0

        pool.adopt_stream_assignment("pipe-a", "run-1", "http://w0:8766")

        assert pool.assignment_for_run("run-1") == "http://w0:8766"
        assert pool.workers_for_pipeline("pipe-a") == ["http://w0:8766"]
        assert pool._health["http://w0:8766"]["active_runs"] == 1

    def test_keeps_pipeline_mapping_deduped_across_runs(self):
        pool = _pool("http://w0:8766")
        pool.adopt_stream_assignment("pipe-a", "run-1", "http://w0:8766")
        pool.adopt_stream_assignment("pipe-a", "run-2", "http://w0:8766")

        assert pool.workers_for_pipeline("pipe-a") == ["http://w0:8766"]
        assert pool._health["http://w0:8766"]["active_runs"] == 2


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

    def test_dispatch_with_result_labels_accepted(self):
        pool = _pool("http://w0:8766")
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = MagicMock(raise_for_status=MagicMock())

        with patch("httpx.Client", return_value=mock_client):
            outcome = pool.dispatch_with_result("r10", "p", "yaml", "batch")

        assert outcome.worker_url == "http://w0:8766"
        assert outcome.outcome == DISPATCH_ACCEPTED
        assert outcome.error is None

    def test_dispatch_with_result_labels_no_capacity(self):
        pool = _pool("http://w0:8766")
        pool._health["http://w0:8766"]["ok"] = False

        outcome = pool.dispatch_with_result("r11", "p", "yaml", "batch")

        assert outcome.worker_url is None
        assert outcome.outcome == DISPATCH_NO_CAPACITY
        assert "No healthy workers" in (outcome.error or "")

    def test_dispatch_with_result_labels_dispatch_failure_with_error(self):
        pool = _pool("http://w0:8766")
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = ConnectionError("refused")

        captured: dict = {}
        real_multi_dispatch = pool.multi_dispatch

        def _capturing_multi_dispatch(*args, **kwargs):
            result = real_multi_dispatch(*args, **kwargs)
            captured["result"] = result
            return result

        pool.multi_dispatch = _capturing_multi_dispatch

        with patch("httpx.Client", return_value=mock_client):
            outcome = pool.dispatch_with_result("r12", "p", "yaml", "batch")

        assert outcome.worker_url is None
        assert outcome.outcome == DISPATCH_FAILED
        assert "refused" in (outcome.error or "")
        # the failure detail also reaches the slot entry
        assert "refused" in (captured["result"].slots[0].get("error") or "")
        assert pool._assignments == {}

    def test_dispatch_keeps_backward_compatible_none_on_failure(self):
        pool = _pool("http://w0:8766")
        pool._health["http://w0:8766"]["ok"] = False
        assert pool.dispatch("r13", "p", "yaml", "batch") is None

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

    def test_resolve_count_n_returns_top_n_workers(self):
        from tram.models.pipeline import WorkersConfig

        pool = _pool("http://w0:8766", "http://w1:8766", "http://w2:8766")
        pool._health["http://w0:8766"] = {"ok": True, "active_runs": 5}
        pool._health["http://w1:8766"] = {"ok": True, "active_runs": 1}
        pool._health["http://w2:8766"] = {"ok": True, "active_runs": 3}

        resolved = pool.resolve(WorkersConfig(count=2))

        assert resolved == ["http://w1:8766", "http://w2:8766"]

    def test_multi_dispatch_count_n_tracks_missing_slots_as_degraded(self):
        from tram.models.pipeline import WorkersConfig

        pool = _pool("http://w0:8766", "http://w1:8766")
        pool._health["http://w1:8766"]["ok"] = False
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = MagicMock(raise_for_status=MagicMock())

        with patch("httpx.Client", return_value=mock_client):
            result = pool.multi_dispatch(
                placement_group_id="pg1",
                pipeline_name="p",
                yaml_text="yaml",
                workers_cfg=WorkersConfig(count=2),
                schedule_type="stream",
            )

        assert result.status == "degraded"
        assert result.accepted == ["http://w0:8766"]
        assert result.run_ids == ["pg1-w0"]
        assert result.slots == [
            {
                "worker_index": 0,
                "worker_url": "http://w0:8766",
                "worker_id": None,
                "pinned_worker_id": None,
                "run_id_prefix": "pg1-w0",
                "current_run_id": "pg1-w0",
                "status": "running",
                "restart_count": 0,
            },
            {
                "worker_index": 1,
                "worker_url": None,
                "worker_id": None,
                "pinned_worker_id": None,
                "run_id_prefix": "pg1-w1",
                "current_run_id": None,
                "status": "stale",
                "restart_count": 0,
            },
        ]

    def test_resolve_named_workers_returns_only_healthy_listed_urls(self):
        from tram.models.pipeline import WorkersConfig

        pool = _pool("http://w0:8766", "http://w1:8766")
        pool._worker_ids["tram-worker-0"] = "http://w0:8766"
        pool._worker_ids["tram-worker-1"] = "http://w1:8766"
        pool._health["http://w1:8766"]["ok"] = False

        resolved = pool.resolve(WorkersConfig(worker_ids=["tram-worker-0", "tram-worker-1"]))

        assert resolved == ["http://w0:8766"]

    def test_multi_dispatch_named_workers_tracks_pinned_slots(self):
        from tram.models.pipeline import WorkersConfig

        pool = _pool("http://w0:8766", "http://w1:8766")
        pool._worker_ids["tram-worker-0"] = "http://w0:8766"
        pool._worker_ids["tram-worker-1"] = "http://w1:8766"
        pool._health["http://w1:8766"]["ok"] = False
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = MagicMock(raise_for_status=MagicMock())

        with patch("httpx.Client", return_value=mock_client):
            result = pool.multi_dispatch(
                placement_group_id="pg-list",
                pipeline_name="p",
                yaml_text="yaml",
                workers_cfg=WorkersConfig(worker_ids=["tram-worker-0", "tram-worker-1"]),
                schedule_type="stream",
            )

        assert result.status == "degraded"
        assert result.accepted == ["http://w0:8766"]
        assert result.slots == [
            {
                "worker_index": 0,
                "worker_url": "http://w0:8766",
                "worker_id": "tram-worker-0",
                "pinned_worker_id": "tram-worker-0",
                "run_id_prefix": "pg-list-w0",
                "current_run_id": "pg-list-w0",
                "status": "running",
                "restart_count": 0,
            },
            {
                "worker_index": 1,
                "worker_url": None,
                "worker_id": "tram-worker-1",
                "pinned_worker_id": "tram-worker-1",
                "run_id_prefix": "pg-list-w1",
                "current_run_id": None,
                "status": "stale",
                "restart_count": 0,
            },
        ]


# ── Manager→worker auth header ──────────────────────────────────────────────


class TestManagerToWorkerAuthHeader:
    """Every manager→worker HTTP call must carry X-API-Key when TRAM_API_KEY
    is set, and no header when it is not (mirrors the agent server pattern)."""

    def _pool_with_env_key(self, monkeypatch, key: str = "manager-key"):
        monkeypatch.setenv("TRAM_API_KEY", key)
        return _pool("http://w0:8766")

    def _capture_client(self):
        captured = []
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = MagicMock(raise_for_status=MagicMock())
        mock_client.get.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
        )
        mock_client.get.return_value.json.return_value = {
            "ok": True, "active_runs": 0, "worker_id": "w0",
        }

        def _factory(*args, **kwargs):
            captured.append(kwargs)
            return mock_client

        return captured, mock_client, _factory

    def test_dispatch_carries_api_key_header_when_configured(self, monkeypatch):
        pool = self._pool_with_env_key(monkeypatch)
        captured, mock_client, factory = self._capture_client()
        with patch("httpx.Client", side_effect=factory):
            pool.dispatch("r1", "p", "yaml", "batch")
        assert captured[0]["headers"] == {"X-API-Key": "manager-key"}
        assert captured[0]["timeout"] == 10

    def test_stop_run_carries_api_key_header_when_configured(self, monkeypatch):
        pool = self._pool_with_env_key(monkeypatch)
        pool._assignments["s1"] = "http://w0:8766"
        captured, mock_client, factory = self._capture_client()
        with patch("httpx.Client", side_effect=factory):
            assert pool.stop_run("s1", "p") is True
        assert captured[0]["headers"] == {"X-API-Key": "manager-key"}

    def test_worker_status_carries_api_key_header_when_configured(self, monkeypatch):
        pool = self._pool_with_env_key(monkeypatch)
        captured, mock_client, factory = self._capture_client()
        with patch("httpx.Client", side_effect=factory):
            pool.worker_status("http://w0:8766")
        assert captured[0]["headers"] == {"X-API-Key": "manager-key"}

    def test_health_probe_carries_api_key_header_when_configured(self, monkeypatch):
        pool = self._pool_with_env_key(monkeypatch)
        captured, mock_client, factory = self._capture_client()
        with patch("httpx.Client", side_effect=factory):
            pool._poll_all()
        assert captured[0]["headers"] == {"X-API-Key": "manager-key"}

    def test_stop_pipeline_runs_carries_api_key_header_when_configured(self, monkeypatch):
        pool = self._pool_with_env_key(monkeypatch)
        captured, mock_client, factory = self._capture_client()

        def _get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "running": [{"run_id": "b1", "pipeline": "pipe-a", "started_at": "now"}],
                "streams": [],
            }
            return resp

        mock_client.get.side_effect = _get
        with patch("httpx.Client", side_effect=factory):
            stopped = pool.stop_pipeline_runs("pipe-a")
        assert stopped == ["b1"]
        assert captured[0]["headers"] == {"X-API-Key": "manager-key"}

    def test_no_api_key_means_no_header(self, monkeypatch):
        monkeypatch.delenv("TRAM_API_KEY", raising=False)
        pool = _pool("http://w0:8766")
        captured, mock_client, factory = self._capture_client()
        with patch("httpx.Client", side_effect=factory):
            pool.dispatch("r1", "p", "yaml", "batch")
        assert captured[0]["headers"] is None


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


class TestStatusQueries:
    def test_worker_status_returns_running_and_streams(self):
        pool = _pool("http://w0:8766")

        def _get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "running": [{"run_id": "b1", "pipeline": "pipe-a", "started_at": "now"}],
                "streams": [{"run_id": "s1", "pipeline": "pipe-b", "started_at": "later"}],
            }
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = _get

        with patch("httpx.Client", return_value=mock_client):
            status = pool.worker_status("http://w0:8766")

        assert status == {
            "worker_id": None,
            "active_runs": 2,
            "running_pipelines": [],
            "running": [{"run_id": "b1", "pipeline": "pipe-a", "started_at": "now"}],
            "streams": [{"run_id": "s1", "pipeline": "pipe-b", "started_at": "later"}],
        }

    def test_status_prefers_live_worker_counts(self):
        pool = _pool("http://w0:8766")
        pool._health["http://w0:8766"] = {
            "ok": True,
            "active_runs": 0,
            "running_pipelines": [],
        }
        pool._pipeline_workers["pipe-a"] = ["http://w0:8766"]

        def _get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "worker_id": "w0",
                "active_runs": 1,
                "running_pipelines": ["pipe-a"],
                "running": [],
                "streams": [{"run_id": "s1", "pipeline": "pipe-a", "started_at": "now"}],
            }
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = _get

        with patch("httpx.Client", return_value=mock_client):
            rows = pool.status()

        assert rows == [{
            "url": "http://w0:8766",
            "worker_id": "w0",
            "ok": True,
            "active_runs": 1,
            "active_streams": 1,
            "running_pipelines": ["pipe-a"],
            "running": [],
            "streams": [{"run_id": "s1", "pipeline": "pipe-a", "started_at": "now"}],
            "assigned_pipelines": ["pipe-a"],
        }]

    def test_live_streams_returns_normalized_stream_entries(self):
        pool = _pool("http://w0:8766")

        def _get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "worker_id": "w0",
                "active_runs": 1,
                "running_pipelines": ["pipe-a"],
                "running": [],
                "streams": [{
                    "run_id": "s1",
                    "pipeline": "pipe-a",
                    "started_at": "now",
                    "schedule_type": "stream",
                    "uptime_seconds": 3.0,
                    "stats": {"records_out": 9},
                }],
            }
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = _get

        with patch("httpx.Client", return_value=mock_client):
            live = pool.live_streams()

        assert live == [{
            "worker_url": "http://w0:8766",
            "worker_id": "w0",
            "pipeline_name": "pipe-a",
            "run_id": "s1",
            "started_at": "now",
            "schedule_type": "stream",
            "uptime_seconds": 3.0,
            "stats": {"records_out": 9},
        }]

    def test_live_streams_passes_config_sha256_through(self):
        pool = _pool("http://w0:8766")

        with patch("httpx.Client", return_value=_mock_httpx_client({
            "http://w0:8766/agent/status": {
                "worker_id": "w0",
                "active_runs": 1,
                "running_pipelines": ["pipe-a"],
                "running": [],
                "streams": [{
                    "run_id": "s1",
                    "pipeline": "pipe-a",
                    "started_at": "now",
                    "schedule_type": "stream",
                    "uptime_seconds": 3.0,
                    "stats": {"records_out": 9},
                    "config_sha256": "0123456789abcdef",
                }],
            },
        })):
            live = pool.live_streams()

        assert live[0]["config_sha256"] == "0123456789abcdef"

    def test_live_streams_omits_config_sha256_for_older_agents(self):
        """An agent that predates D.2 does not emit the key — it must be absent,
        never synthesized, so the manager treats it as "unknown" (fail-open)."""
        pool = _pool("http://w0:8766")

        with patch("httpx.Client", return_value=_mock_httpx_client({
            "http://w0:8766/agent/status": {
                "worker_id": "w0",
                "active_runs": 1,
                "running_pipelines": ["pipe-a"],
                "running": [],
                "streams": [{
                    "run_id": "s1",
                    "pipeline": "pipe-a",
                    "started_at": "now",
                    "schedule_type": "stream",
                    "uptime_seconds": 3.0,
                    "stats": {"records_out": 9},
                }],
            },
        })):
            live = pool.live_streams()

        assert "config_sha256" not in live[0]

    def test_find_pipeline_runs_passes_config_sha256_through(self):
        pool = _pool("http://w0:8766")

        with patch("httpx.Client", return_value=_mock_httpx_client({
            "http://w0:8766/agent/status": {
                "worker_id": "w0",
                "running": [],
                "streams": [{
                    "run_id": "s1",
                    "pipeline": "pipe-a",
                    "started_at": "now",
                    "config_sha256": "0123456789abcdef",
                }],
            },
        })):
            matches = pool.find_pipeline_runs("pipe-a", schedule_type="stream")

        assert matches[0]["config_sha256"] == "0123456789abcdef"

    def test_is_run_active_checks_assigned_worker(self):
        pool = _pool("http://w0:8766")
        pool._assignments["r1"] = "http://w0:8766"

        def _get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "running": [{"run_id": "r1", "pipeline": "pipe-a", "started_at": "now"}],
                "streams": [],
            }
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = _get

        with patch("httpx.Client", return_value=mock_client):
            assert pool.is_run_active("r1") is True

    def test_find_pipeline_runs_returns_batch_matches_across_workers(self):
        pool = _pool("http://w0:8766", "http://w1:8766")

        def _get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if url == "http://w0:8766/agent/status":
                resp.json.return_value = {
                    "running": [{"run_id": "b1", "pipeline": "pipe-a", "started_at": "2026-04-23T10:00:00+00:00"}],
                    "streams": [],
                }
            elif url == "http://w1:8766/agent/status":
                resp.json.return_value = {
                    "running": [{"run_id": "b2", "pipeline": "other", "started_at": "2026-04-23T10:01:00+00:00"}],
                    "streams": [],
                }
            else:
                raise ConnectionError(url)
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = _get

        with patch("httpx.Client", return_value=mock_client):
            matches = pool.find_pipeline_runs("pipe-a", schedule_type="batch")

        assert matches == [{
            "worker_url": "http://w0:8766",
            "run_id": "b1",
            "pipeline_name": "pipe-a",
            "started_at": "2026-04-23T10:00:00+00:00",
            "schedule_type": "batch",
        }]


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


class TestPipelineWorkersPrune:
    """D8: _pipeline_workers must not grow unboundedly — entries are dropped
    once no active placement slot (or run assignment) references them."""

    def test_prunes_pipeline_workers_when_last_run_completes(self):
        pool = _pool("http://w0:8766")
        pool._assignments["r1"] = "http://w0:8766"
        pool._run_pipelines["r1"] = "pipe-a"
        pool._pipeline_workers["pipe-a"] = ["http://w0:8766"]

        pool.on_run_complete("r1")

        assert "pipe-a" not in pool._pipeline_workers

    def test_keeps_pipeline_workers_while_other_runs_active(self):
        pool = _pool("http://w0:8766", "http://w1:8766")
        pool._assignments.update({"r1": "http://w0:8766", "r2": "http://w1:8766"})
        pool._run_pipelines.update({"r1": "pipe-a", "r2": "pipe-a"})
        pool._pipeline_workers["pipe-a"] = ["http://w0:8766", "http://w1:8766"]

        pool.on_run_complete("r1")
        assert pool._pipeline_workers["pipe-a"] == ["http://w0:8766", "http://w1:8766"]

        pool.on_run_complete("r2")
        assert "pipe-a" not in pool._pipeline_workers

    def test_other_pipelines_unaffected(self):
        pool = _pool("http://w0:8766")
        pool._assignments.update({"r1": "http://w0:8766", "r2": "http://w0:8766"})
        pool._run_pipelines.update({"r1": "pipe-a", "r2": "pipe-b"})
        pool._pipeline_workers["pipe-a"] = ["http://w0:8766"]
        pool._pipeline_workers["pipe-b"] = ["http://w0:8766"]

        pool.on_run_complete("r1")

        assert "pipe-a" not in pool._pipeline_workers
        assert pool._pipeline_workers["pipe-b"] == ["http://w0:8766"]

    def test_dispatch_tracks_run_pipeline_then_prunes_on_complete(self):
        pool = _pool("http://w0:8766")
        with patch("httpx.Client", return_value=_mock_httpx_client({})):
            err = pool._dispatch_to_worker(
                worker_url="http://w0:8766",
                run_id="r1",
                pipeline_name="pipe-a",
                yaml_text="name: pipe-a\n",
                schedule_type="stream",
            )
        assert err is None
        assert pool._assignments["r1"] == "http://w0:8766"
        assert pool._run_pipelines["r1"] == "pipe-a"
        assert pool._pipeline_workers["pipe-a"] == ["http://w0:8766"]

        pool.on_run_complete("r1")

        assert "pipe-a" not in pool._pipeline_workers

    def test_adopted_stream_assignment_prunes_after_complete(self):
        pool = _pool("http://w0:8766")
        pool.adopt_stream_assignment("pipe-a", "r1", "http://w0:8766")
        assert pool._run_pipelines["r1"] == "pipe-a"
        assert pool.workers_for_pipeline("pipe-a") == ["http://w0:8766"]

        pool.on_run_complete("r1")

        assert "pipe-a" not in pool._pipeline_workers


# ── Reap bookkeeping on worker death (D.2 review) ───────────────────────────


class TestReapOnWorkerDown:
    """A worker's _assignments/_run_pipelines bookkeeping is reaped on the
    healthy→down hysteresis transition, so entries from a dead worker's
    never-completing runs don't leak and keep _pipeline_workers un-pruned."""

    def _mark_down_via_poll(self, pool, url):
        """Drive the poll loop past the hysteresis threshold (2 failures) for
        the target worker only — other workers stay healthy."""

        def _get(request_url, **kwargs):
            base = request_url.rsplit("/agent/health", 1)[0]
            if base == url:
                raise ConnectionError("refused")
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "ok": True,
                "active_runs": 0,
                "worker_id": base.rsplit(":", 1)[-1],
            }
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = _get
        with patch("httpx.Client", return_value=mock_client):
            pool._poll_all()
            pool._poll_all()
        assert pool._health[url]["ok"] is False

    def test_reaps_assignments_for_down_worker(self):
        pool = _pool("http://w0:8766", "http://w1:8766")
        pool._assignments.update({
            "r1": "http://w0:8766",
            "r2": "http://w0:8766",
            "r3": "http://w1:8766",
        })
        pool._run_pipelines.update({"r1": "pipe-a", "r2": "pipe-b", "r3": "pipe-c"})
        pool._pipeline_workers.update({
            "pipe-a": ["http://w0:8766"],
            "pipe-b": ["http://w0:8766"],
            "pipe-c": ["http://w1:8766"],
        })
        pool._health["http://w0:8766"]["active_runs"] = 2

        self._mark_down_via_poll(pool, "http://w0:8766")

        # The down worker's runs are reaped; the healthy worker's are untouched.
        assert "r1" not in pool._assignments
        assert "r2" not in pool._assignments
        assert pool._assignments == {"r3": "http://w1:8766"}
        assert "r1" not in pool._run_pipelines
        assert "r2" not in pool._run_pipelines
        assert pool._run_pipelines == {"r3": "pipe-c"}
        # Pipelines left without any active run are pruned (D8), like on_run_complete.
        assert "pipe-a" not in pool._pipeline_workers
        assert "pipe-b" not in pool._pipeline_workers
        assert pool._pipeline_workers["pipe-c"] == ["http://w1:8766"]
        assert pool._health["http://w0:8766"]["active_runs"] == 0

    def test_down_worker_removed_from_shared_pipeline_list(self):
        pool = _pool("http://w0:8766", "http://w1:8766")
        pool._assignments.update({"r1": "http://w0:8766", "r2": "http://w1:8766"})
        pool._run_pipelines.update({"r1": "pipe-a", "r2": "pipe-a"})
        pool._pipeline_workers["pipe-a"] = ["http://w0:8766", "http://w1:8766"]

        self._mark_down_via_poll(pool, "http://w0:8766")

        # The pipeline still has a live run on w1: only the down worker is
        # removed from the list, the pipeline entry survives.
        assert pool._assignments == {"r2": "http://w1:8766"}
        assert pool._pipeline_workers["pipe-a"] == ["http://w1:8766"]

    def test_no_assignments_no_change(self):
        pool = _pool("http://w0:8766")
        self._mark_down_via_poll(pool, "http://w0:8766")
        assert pool._assignments == {}
        assert pool._run_pipelines == {}
        assert pool._pipeline_workers == {}

    def test_recovered_worker_can_be_redispatched(self):
        """The reap only drops bookkeeping — the worker itself stays a member of
        the pool and can be dispatched to again once healthy."""
        pool = _pool("http://w0:8766")
        pool._assignments["r1"] = "http://w0:8766"
        pool._run_pipelines["r1"] = "pipe-a"
        pool._pipeline_workers["pipe-a"] = ["http://w0:8766"]

        self._mark_down_via_poll(pool, "http://w0:8766")
        assert pool._assignments == {}

        # Healthy again → dispatch registers fresh bookkeeping.
        pool._health["http://w0:8766"]["ok"] = True
        mock_client = _mock_httpx_client({})
        with patch("httpx.Client", return_value=mock_client):
            pool._dispatch_to_worker(
                worker_url="http://w0:8766",
                run_id="r2",
                pipeline_name="pipe-a",
                yaml_text="name: pipe-a\n",
                schedule_type="stream",
            )
        assert pool._assignments["r2"] == "http://w0:8766"
        assert pool.workers_for_pipeline("pipe-a") == ["http://w0:8766"]
