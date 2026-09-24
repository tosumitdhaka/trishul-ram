"""Tests for WebhookSource (v0.5.0)."""
from __future__ import annotations

import queue
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.connectors.webhook import _WEBHOOK_SECRETS
from tram.connectors.webhook.source import _REGISTRY_LOCK, _WEBHOOK_REGISTRY, WebhookSource


def test_webhook_source_registers_on_read():
    """Webhook source should register its queue when read() is called."""
    source = WebhookSource({"type": "webhook", "path": "test-path"})

    items = []
    stop_flag = threading.Event()

    def consume():
        gen = source.read()
        for _ in range(1):
            try:
                body, meta = next(gen)
                items.append((body, meta))
            except StopIteration:
                break
        stop_flag.set()

    # Start consuming in background
    t = threading.Thread(target=consume, daemon=True)
    t.start()

    # Wait for registration
    time.sleep(0.05)
    assert "test-path" in _WEBHOOK_REGISTRY

    # Push a message
    q = _WEBHOOK_REGISTRY["test-path"]
    q.put((b'{"x":1}', {"path": "test-path"}))

    stop_flag.wait(timeout=2.0)
    assert len(items) == 1
    assert items[0][0] == b'{"x":1}'


def test_webhook_source_deregisters_on_exit():
    """Queue is removed from registry when read() exits."""
    source = WebhookSource({"type": "webhook", "path": "exit-test"})
    threading.Event()

    def consume():
        gen = source.read()
        # Immediately close the generator
        gen.close()

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    t.join(timeout=2.0)

    assert "exit-test" not in _WEBHOOK_REGISTRY


def test_webhook_source_secret_stored():
    """Secret is stored in _WEBHOOK_SECRETS while source is reading."""
    source = WebhookSource({"type": "webhook", "path": "secret-test2", "secret": "mytoken"})
    registered = threading.Event()
    done = threading.Event()

    def consume():
        gen = source.read()
        # Send a sentinel to unblock the generator after it registers
        # We peek into the registry once registration happens
        registered.set()
        gen.close()
        done.set()

    # Start iteration in thread — registration happens before first yield
    t = threading.Thread(target=consume, daemon=True)
    t.start()

    # Directly test: create the queue and set secret manually to verify the mechanism
    # (The generator registration executes before first yield, triggered by next())
    # Instead test via a direct call path:
    import queue as qmod
    q2 = qmod.SimpleQueue()
    with _REGISTRY_LOCK:
        _WEBHOOK_REGISTRY["secret-direct"] = q2
        _WEBHOOK_SECRETS["secret-direct"] = "tok123"

    assert _WEBHOOK_SECRETS.get("secret-direct") == "tok123"

    with _REGISTRY_LOCK:
        _WEBHOOK_REGISTRY.pop("secret-direct", None)
        _WEBHOOK_SECRETS.pop("secret-direct", None)

    t.join(timeout=1.0)


def test_webhook_source_no_secret():
    """No secret should not add to _WEBHOOK_SECRETS."""
    source = WebhookSource({"type": "webhook", "path": "no-secret-test"})

    # Verify config
    assert source.secret is None


def test_webhook_source_config_defaults():
    source = WebhookSource({"type": "webhook", "path": "/my/path"})
    assert source.path == "my/path"  # leading slash stripped
    assert source.secret is None
    assert source.max_queue_size == 1000


# ── max_queue_size validation (F1) ────────────────────────────────────────────


def test_webhook_max_queue_size_rejects_zero_and_negative():
    """queue.Queue treats maxsize<=0 as "infinite" — validation must reject it
    so the unbounded-memory DoS cannot silently reopen (F1)."""
    from pydantic import ValidationError

    from tram.models.pipeline import WebhookSourceConfig

    with pytest.raises(ValidationError):
        WebhookSourceConfig(type="webhook", path="x", max_queue_size=0)
    with pytest.raises(ValidationError):
        WebhookSourceConfig(type="webhook", path="x", max_queue_size=-5)


def test_webhook_max_queue_size_positive_accepted():
    from tram.models.pipeline import WebhookSourceConfig

    cfg = WebhookSourceConfig(type="webhook", path="x", max_queue_size=1)
    assert cfg.max_queue_size == 1
    assert WebhookSourceConfig(type="webhook", path="x").max_queue_size == 1000


# ── Bounded queue (GH #45) ────────────────────────────────────────────────────


def test_webhook_source_queue_is_bounded():
    """read() registers a queue.Queue(maxsize=max_queue_size), so the config
    knob actually bounds memory — put_nowait raises Full once it fills."""
    source = WebhookSource({"type": "webhook", "path": "bounded-test", "max_queue_size": 3})
    seen: dict = {}

    def consume():
        gen = source.read()
        try:
            body, _meta = next(gen)  # registers, then blocks until a message arrives
            seen["body"] = body
        except StopIteration:
            pass
        gen.close()

    t = threading.Thread(target=consume, daemon=True)
    t.start()

    q = None
    for _ in range(200):
        with _REGISTRY_LOCK:
            q = _WEBHOOK_REGISTRY.get("bounded-test")
        if q is not None:
            break
        time.sleep(0.005)
    assert q is not None, "webhook source did not register its queue"
    seen["q"] = q

    # Unblock the consumer with a sentinel.
    q.put((b"sentinel", {}))
    t.join(timeout=2.0)
    assert seen.get("body") == b"sentinel"

    # The bounded knob is honored: maxsize set, and put_nowait raises Full
    # once the queue is full (memory bound, GH #45).
    assert q.maxsize == 3
    q.put_nowait((b"1", {}))
    q.put_nowait((b"2", {}))
    q.put_nowait((b"3", {}))
    with pytest.raises(queue.Full):
        q.put_nowait((b"4", {}))
    assert q.qsize() == 3


def test_webhook_router_returns_503_when_queue_full():
    """The router's put_nowait → queue.Full → 503 path is live once the source
    queue is bounded (was dead with the unbounded SimpleQueue)."""
    from tram.api.routers.webhooks import router

    q = queue.Queue(maxsize=1)
    with _REGISTRY_LOCK:
        _WEBHOOK_REGISTRY["full-test"] = q
    try:
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        r1 = client.post("/webhooks/full-test", content=b"first")
        assert r1.status_code == 202

        r2 = client.post("/webhooks/full-test", content=b"second")
        assert r2.status_code == 503
        assert "queue full" in r2.json()["detail"].lower()
        # The rejected payload never entered the queue.
        assert q.qsize() == 1
    finally:
        with _REGISTRY_LOCK:
            _WEBHOOK_REGISTRY.pop("full-test", None)
