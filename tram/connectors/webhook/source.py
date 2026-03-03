"""Webhook source — receives HTTP POSTs via the TRAM daemon FastAPI port."""

from __future__ import annotations

import queue
import threading
from typing import Generator

from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

# Global registry: path -> SimpleQueue
# The FastAPI webhook router looks up this dict.
_WEBHOOK_REGISTRY: dict[str, queue.SimpleQueue] = {}
_REGISTRY_LOCK = threading.Lock()


@register_source("webhook")
class WebhookSource(BaseSource):
    """Receive HTTP POST payloads forwarded via the daemon's /webhooks/{path} endpoint.

    Config:
        path (str): URL path segment to listen on (e.g. "my-events").
        secret (str, optional): Bearer token required in Authorization header.
        max_queue_size (int): Max queued messages before blocking. Default 1000.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.path: str = config["path"].lstrip("/")
        self.secret: str | None = config.get("secret")
        self.max_queue_size: int = config.get("max_queue_size", 1000)

    def read(self) -> Generator[tuple[bytes, dict], None, None]:
        from tram.connectors.webhook import _WEBHOOK_SECRETS

        q: queue.SimpleQueue = queue.SimpleQueue()

        with _REGISTRY_LOCK:
            _WEBHOOK_REGISTRY[self.path] = q
            if self.secret:
                _WEBHOOK_SECRETS[self.path] = self.secret
            else:
                _WEBHOOK_SECRETS.pop(self.path, None)

        try:
            while True:
                try:
                    body, meta = q.get(timeout=1.0)
                    yield body, meta
                except queue.Empty:
                    continue
        finally:
            with _REGISTRY_LOCK:
                _WEBHOOK_REGISTRY.pop(self.path, None)
                _WEBHOOK_SECRETS.pop(self.path, None)
