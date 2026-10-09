"""Bounded source→executor bridge queue (V18-01 §9, plan F).

Connector producer threads (paho network loop, asyncio event loops, TCP
handler threads, the webhook router) hand ``(payload, meta)`` tuples to the
sync ``read()`` generator through a small internal queue. That queue is
bounded by BOTH item count and total payload bytes
(``TRAM_SOURCE_BRIDGE_MAX_COUNT`` / ``TRAM_SOURCE_BRIDGE_MAX_BYTES``) so a
slow sink can never grow process memory without bound.

Overflow never silently drops a payload and never acknowledges a broker
delivery early:

- ``put()`` blocks the producer until room exists (backpressure). Safe for
  dedicated producer threads (syslog TCP handlers) where blocking translates
  into socket-level flow control.
- ``put_nowait()`` raises ``queue.Full``; the webhook router maps that to an
  explicit HTTP 503 (the documented bounded-rejection behavior).
- ``put_cooperative()`` / ``put_cooperative_async()`` retry ``put_nowait``
  with a sleep so event-loop / network-loop producer threads (MQTT, NATS,
  WebSocket) stay responsive — they can keep servicing heartbeats, pings,
  and stop signals — while still never dropping a payload.
"""

from __future__ import annotations

import asyncio
import queue
import time
from typing import Any


class BoundedBridgeQueue(queue.Queue):
    """FIFO bridge queue bounded by item count and total payload bytes.

    Payload size is taken from ``item[0]`` when the item is a ``(payload,
    meta)`` tuple (every bridge producer in the codebase puts that shape),
    else ``len(item)``.
    """

    def __init__(self, max_count: int = 0, max_bytes: int = 0) -> None:
        super().__init__(maxsize=max_count)
        self.max_bytes = max_bytes
        self._bytes = 0

    # ── internal accounting ───────────────────────────────────────────────

    @staticmethod
    def _payload_size(item: Any) -> int:
        payload = item[0] if isinstance(item, tuple) else item
        try:
            return len(payload)
        except TypeError:
            return 0

    def _bytes_ok(self, item: Any) -> bool:
        return self.max_bytes <= 0 or self._bytes + self._payload_size(item) <= self.max_bytes

    def _room_for(self, item: Any) -> bool:
        return (self.maxsize <= 0 or self._qsize() < self.maxsize) and self._bytes_ok(item)

    def _put(self, item: Any) -> None:
        super()._put(item)
        self._bytes += self._payload_size(item)

    def _get(self) -> Any:
        item = super()._get()
        self._bytes -= self._payload_size(item)
        return item

    # ── producing side ────────────────────────────────────────────────────

    def put(self, item: Any, block: bool = True, timeout: float | None = None) -> None:
        """Blocking backpressure put — waits on both the count and byte bounds.

        Mirrors ``queue.Queue.put`` but the not-full condition also includes
        the byte budget. Never drops.
        """
        with self.not_full:
            if self.maxsize > 0 or self.max_bytes > 0:
                if not block:
                    if not self._room_for(item):
                        raise queue.Full
                elif timeout is None:
                    while not self._room_for(item):
                        self.not_full.wait()
                elif timeout < 0:
                    raise ValueError("'timeout' must be a non-negative number")
                else:
                    endtime = time.monotonic() + timeout
                    while not self._room_for(item):
                        remaining = endtime - time.monotonic()
                        if remaining <= 0.0:
                            raise queue.Full
                        self.not_full.wait(remaining)
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def put_cooperative(self, item: Any, poll_s: float = 0.05) -> None:
        """Cooperative backpressure put for thread-based network loops.

        Retries ``put_nowait`` with a short sleep between attempts so the
        producer thread (e.g. the paho MQTT network loop) can keep servicing
        heartbeats/acks while the queue is full. Never drops.
        """
        while True:
            try:
                self.put_nowait(item)
                return
            except queue.Full:
                time.sleep(poll_s)

    async def put_cooperative_async(self, item: Any, poll_s: float = 0.01) -> None:
        """Cooperative backpressure put for asyncio producer loops.

        ``await asyncio.sleep`` between attempts keeps the event loop
        responsive (heartbeats, stop signals) while the queue is full.
        Never drops.
        """
        while True:
            try:
                self.put_nowait(item)
                return
            except queue.Full:
                await asyncio.sleep(poll_s)