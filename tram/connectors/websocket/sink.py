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
