"""Tests for issue #82 — webhook placement race 404s.

The worker ingress holds unmatched webhook paths for up to
``TRAM_WEBHOOK_PLACEMENT_WINDOW_SECONDS`` (a bounded poll loop sized to the
~5-11s placement-propagation window) before 404ing as today:

  1. unmatched path 404s after the window expires;
  2. a path registered within the window succeeds without a 404;
  3. window=0 preserves the pre-v1.6.0 immediate-404 behavior.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.webhooks import router as webhooks_router
from tram.connectors.webhook.source import _REGISTRY_LOCK, _WEBHOOK_REGISTRY


def _make_webhook_app():
    app = FastAPI()
    app.include_router(webhooks_router)
    return app


def _register_path(path: str, q: queue.Queue | None = None) -> queue.Queue:
    """Insert a webhook queue into the module registry under the lock."""
    q = q or queue.Queue()
    with _REGISTRY_LOCK:
        _WEBHOOK_REGISTRY[path] = q
    return q


def test_unmatched_path_404s_after_window_expiry():
    """A genuinely unknown path is still a 404 — after the bounded hold window."""
    app = _make_webhook_app()
    client = TestClient(app, raise_server_exceptions=False)
    with patch("tram.connectors.webhook.source._WEBHOOK_REGISTRY", {}), \
         patch.dict(os.environ, {"TRAM_WEBHOOK_PLACEMENT_WINDOW_SECONDS": "0.25"}):
        started = time.monotonic()
        resp = client.post("/webhooks/never-registered", content=b"data")
        elapsed = time.monotonic() - started
    assert resp.status_code == 404
    assert "No webhook source registered for path" in resp.json()["detail"]
    # The 404 came after the bounded hold (not instantly), proving the window
    # was honored — and not after the whole default window either.
    assert 0.15 <= elapsed < 5.0


def test_path_registered_within_window_succeeds():
    """A path that registers mid-hold (the placement propagated) succeeds with
    202 and the payload lands on the source queue — no 404."""
    app = _make_webhook_app()
    client = TestClient(app, raise_server_exceptions=False)
    q = queue.Queue()
    result: dict = {}

    def do_post():
        resp = client.post("/webhooks/race-hook", content=b"data")
        result["status"] = resp.status_code
        result["qsize"] = q.qsize()

    try:
        with patch.dict(os.environ, {"TRAM_WEBHOOK_PLACEMENT_WINDOW_SECONDS": "1.0"}):
            t = threading.Thread(target=do_post)
            t.start()
            # Let the request enter its hold, then simulate the placement arriving.
            time.sleep(0.2)
            _register_path("race-hook", q)
            t.join(timeout=5.0)

        assert not t.is_alive(), "held webhook request did not resolve"
        assert result["status"] == 202
        assert result["qsize"] == 1
    finally:
        with _REGISTRY_LOCK:
            _WEBHOOK_REGISTRY.pop("race-hook", None)


def test_window_zero_preserves_immediate_404():
    """window=0 keeps today's behavior: unmatched paths 404 immediately, with
    no hold."""
    app = _make_webhook_app()
    client = TestClient(app, raise_server_exceptions=False)
    with patch("tram.connectors.webhook.source._WEBHOOK_REGISTRY", {}), \
         patch.dict(os.environ, {"TRAM_WEBHOOK_PLACEMENT_WINDOW_SECONDS": "0"}):
        started = time.monotonic()
        resp = client.post("/webhooks/never-registered", content=b"data")
        elapsed = time.monotonic() - started
    assert resp.status_code == 404
    assert elapsed < 0.2, "window=0 must not hold the request"


def test_default_window_is_ten_seconds(monkeypatch):
    """The default window is sized to the propagation window (~10s)."""
    from tram.api.routers import webhooks as webhooks_mod

    monkeypatch.delenv("TRAM_WEBHOOK_PLACEMENT_WINDOW_SECONDS", raising=False)
    assert webhooks_mod._placement_window_seconds() == 10.0


def test_invalid_window_falls_back_to_default():
    """Invalid TRAM_WEBHOOK_PLACEMENT_WINDOW_SECONDS values fall back to the
    default (the webhook body-cap convention), never to a crash."""
    from tram.api.routers import webhooks as webhooks_mod

    with patch.dict(os.environ, {"TRAM_WEBHOOK_PLACEMENT_WINDOW_SECONDS": "banana"}):
        assert webhooks_mod._placement_window_seconds() == 10.0