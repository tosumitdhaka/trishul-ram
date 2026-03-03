"""NATS source connector — subscribes and streams messages via asyncio bridge."""
from __future__ import annotations
import asyncio
import logging
import queue
import threading
from typing import Iterator
from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)

@register_source("nats")
class NatsSource(BaseSource):
    """Subscribe to a NATS subject and yield messages as a stream.

    Uses a background daemon thread with its own asyncio event loop to avoid
    nesting issues with the sync TRAM executor.

    Config keys:
        servers             (list[str], default ["nats://localhost:4222"])
        subject             (str, required)
        queue_group         (str, default "")
        credentials_file    (str, optional)
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.servers: list[str] = config.get("servers", ["nats://localhost:4222"])
        self.subject: str = config["subject"]
        self.queue_group: str = config.get("queue_group", "")
        self.credentials_file: str | None = config.get("credentials_file")
        self._stop_event = threading.Event()
        self._msg_queue: queue.SimpleQueue = queue.SimpleQueue()

    def read(self) -> Iterator[tuple[bytes, dict]]:
        try:
            import nats
        except ImportError as exc:
            raise SourceError(
                "NATS source requires nats-py — install with: pip install tram[nats]"
            ) from exc

        loop = asyncio.new_event_loop()
        stop_event = self._stop_event
        msg_queue = self._msg_queue
        servers = self.servers
        subject = self.subject
        queue_group = self.queue_group
        credentials_file = self.credentials_file

        async def _run():
            kwargs = {"servers": servers}
            if credentials_file:
                kwargs["credentials"] = credentials_file
            nc = await nats.connect(**kwargs)

            async def message_handler(msg):
                msg_queue.put((msg.data, {"nats_subject": msg.subject}))

            if queue_group:
                await nc.subscribe(subject, queue=queue_group, cb=message_handler)
            else:
                await nc.subscribe(subject, cb=message_handler)
            logger.info("NATS source subscribed", extra={"subject": subject})

            while not stop_event.is_set():
                await asyncio.sleep(0.1)
            await nc.close()

        def thread_fn():
            loop.run_until_complete(_run())

        t = threading.Thread(target=thread_fn, daemon=True)
        t.start()

        try:
            while not self._stop_event.is_set() or not self._msg_queue.empty():
                try:
                    payload, meta = self._msg_queue.get(timeout=1.0)
                    yield payload, meta
                except queue.Empty:
                    if self._stop_event.is_set():
                        break
                    continue
        finally:
            self._stop_event.set()
            t.join(timeout=5.0)
            loop.close()
