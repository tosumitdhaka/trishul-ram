"""WebSocket source — consumes messages from a WebSocket server."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Generator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


@register_source("websocket")
class WebSocketSource(BaseSource):
    """Connect to a WebSocket server and yield received messages.

    Config:
        url (str): WebSocket URL (ws:// or wss://).
        extra_headers (dict): Additional HTTP headers for the handshake.
        ping_interval (int): Seconds between keep-alive pings. Default 20.
        reconnect (bool): Auto-reconnect on disconnect. Default True.
        reconnect_delay (int): Seconds to wait before reconnecting. Default 5.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.url: str = config["url"]
        self.extra_headers: dict = config.get("extra_headers", {})
        self.ping_interval: int = config.get("ping_interval", 20)
        self.reconnect: bool = config.get("reconnect", True)
        self.reconnect_delay: int = config.get("reconnect_delay", 5)

    def read(self) -> Generator[tuple[bytes, dict], None, None]:
        try:
            import websockets  # noqa: F401
        except ImportError as exc:
            raise SourceError(
                "WebSocket source requires websockets — install with: pip install tram[websocket]"
            ) from exc

        bridge: queue.SimpleQueue = queue.SimpleQueue()
        stop_flag = threading.Event()

        def _run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._ws_loop(bridge, stop_flag))
            finally:
                loop.close()

        t = threading.Thread(target=_run_loop, daemon=True)
        t.start()

        try:
            while not stop_flag.is_set() or not bridge.empty():
                try:
                    item = bridge.get(timeout=1.0)
                    if item is None:
                        if not self.reconnect:
                            break
                        continue
                    data, meta = item
                    yield data, meta
                except queue.Empty:
                    continue
        finally:
            stop_flag.set()

    async def _ws_loop(self, bridge: queue.SimpleQueue, stop_flag: threading.Event):
        import websockets

        while not stop_flag.is_set():
            try:
                async with websockets.connect(
                    self.url,
                    additional_headers=self.extra_headers,
                    ping_interval=self.ping_interval,
                ) as ws:
                    logger.info("WebSocket connected: %s", self.url)
                    async for message in ws:
                        if stop_flag.is_set():
                            break
                        if isinstance(message, str):
                            data = message.encode("utf-8")
                        else:
                            data = bytes(message)
                        bridge.put((data, {"source": "websocket", "url": self.url}))
            except Exception as exc:
                logger.warning("WebSocket error: %s", exc)
                bridge.put(None)  # signal disconnect

            if not self.reconnect or stop_flag.is_set():
                break

            logger.info("WebSocket reconnecting in %d s", self.reconnect_delay)
            await asyncio.sleep(self.reconnect_delay)
