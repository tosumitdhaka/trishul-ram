"""WebSocket sink — sends serialized data to a WebSocket server."""

from __future__ import annotations

import asyncio
import logging

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


@register_sink("websocket")
class WebSocketSink(BaseSink):
    """Send serialized records to a WebSocket endpoint.

    Config:
        url (str): WebSocket URL (ws:// or wss://).
        extra_headers (dict): Additional HTTP headers for the handshake.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.url: str = config["url"]
        self.extra_headers: dict = config.get("extra_headers", {})

    def test_connection(self) -> dict:
        import socket
        import time
        from urllib.parse import urlparse
        t0 = time.monotonic()
        url = self.config.get("url", "")
        if not url:
            raise RuntimeError("No 'url' in config")
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        if not host:
            raise RuntimeError(f"Cannot parse host from URL: {url}")
        try:
            with socket.create_connection((host, port), timeout=8):
                latency = int((time.monotonic() - t0) * 1000)
                return {"ok": True, "latency_ms": latency, "detail": f"TCP {host}:{port} OK"}
        except Exception as exc:
            raise RuntimeError(f"WebSocket TCP probe failed: {exc}")

    def write(self, data: bytes, meta: dict) -> None:
        try:
            import websockets  # noqa: F401
        except ImportError as exc:
            raise SinkError(
                "WebSocket sink requires websockets — install with: pip install tram[websocket]"
            ) from exc

        try:
            asyncio.run(self._send(data))
        except Exception as exc:
            raise SinkError(f"WebSocket send error: {exc}") from exc

    async def _send(self, data: bytes) -> None:
        import websockets

        async with websockets.connect(
            self.url,
            additional_headers=self.extra_headers,
        ) as ws:
            await ws.send(data)
