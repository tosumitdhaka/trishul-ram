"""Live RabbitMQ broker tests for the AMQP connectors (env-gated).

Follows the repo's live-PG pattern (``TestLivePostgres`` in
``test_execution_ledger.py``): the module is skipped entirely unless
``TRAM_TEST_AMQP_URL`` points at a live RabbitMQ. The shared broker outlives
the test session, so every queue is uuid-suffixed (never fixed names) and
deleted in teardown — a second consecutive run must behave identically.

Pins the V18-01/§6-§7 delivery contracts against a real broker:

- sink ``write()`` returns only after the publisher confirm (records land
  before the commit barrier) and ``commit()`` confirms at ``remote_durable``;
- the source does not settle the tag at intake (no early ack): closing
  without ack requeues the delivery, which is then redelivered with
  ``amqp_redelivered`` set;
- ``ack()`` after a disposition settles the tag on the broker
  (``delivered`` → ack, ``dropped`` → nack without requeue);
- ``require_message_id`` refuses a delivery without a producer message ID:
  never enqueued, error-logged, nacked with requeue=True so the broker
  redelivers it;
- ``stop()`` unblocks a live ``read()`` and the reader thread exits promptly
  (thread-safe shutdown on the connection thread).
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid

import pytest

from tram.connectors.amqp.sink import AmqpSink
from tram.connectors.amqp.source import AmqpSource
from tram.interfaces.base_sink import DeliveryTier
from tram.interfaces.base_source import AckDisposition

AMQP_URL = os.environ.get("TRAM_TEST_AMQP_URL", "")


@pytest.mark.skipif(not AMQP_URL, reason="TRAM_TEST_AMQP_URL not set — no live RabbitMQ fixture")
class TestLiveAmqpBroker:
    """V18-10 broker-test gate: AMQP delivery contracts against live RabbitMQ."""

    @pytest.fixture
    def queue(self):
        """A unique transient queue; deleted after the test."""
        import pika

        name = f"tram-live-{uuid.uuid4().hex[:12]}"
        conn = pika.BlockingConnection(pika.URLParameters(AMQP_URL))
        ch = conn.channel()
        ch.queue_declare(queue=name, durable=False, auto_delete=False, exclusive=False)
        conn.close()
        yield name
        try:
            conn = pika.BlockingConnection(pika.URLParameters(AMQP_URL))
            ch = conn.channel()
            ch.queue_delete(queue=name)
            conn.close()
        except Exception:
            pass

    # ── helpers ────────────────────────────────────────────────────────────

    def _publish(self, queue: str, body: bytes, message_id: str | None = None) -> None:
        """Publish one message to the queue on a throwaway connection."""
        import pika

        conn = pika.BlockingConnection(pika.URLParameters(AMQP_URL))
        ch = conn.channel()
        props = pika.BasicProperties(message_id=message_id) if message_id is not None else None
        ch.basic_publish(exchange="", routing_key=queue, body=body, properties=props)
        conn.close()

    def _message_count(self, queue: str) -> int:
        import pika

        conn = pika.BlockingConnection(pika.URLParameters(AMQP_URL))
        ch = conn.channel()
        count = ch.queue_declare(queue=queue, passive=True).method.message_count
        conn.close()
        return count

    @staticmethod
    def _consume(source: AmqpSource, results: list) -> None:
        """Run ``source.read()`` in a worker thread, appending yielded items."""
        for item in source.read():
            results.append(item)

    @staticmethod
    def _wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()

    def _consume_one(self, queue: str, *, require_message_id: bool = False):
        """Start a source reader thread; return (source, results, thread)."""
        source = AmqpSource(
            {"url": AMQP_URL, "queue": queue, "require_message_id": require_message_id}
        )
        results: list[tuple[bytes, dict]] = []
        thread = threading.Thread(target=self._consume, args=(source, results), daemon=True)
        thread.start()
        return source, results, thread

    def _stop_and_join(self, source: AmqpSource, thread: threading.Thread) -> None:
        """Stop the source via the public API and wait for the reader thread."""
        source.stop()
        thread.join(timeout=10)
        assert not thread.is_alive(), "reader thread did not exit after stop()"

    # ── delivery contract pins ─────────────────────────────────────────────

    def test_live_round_trip_publish_confirm_and_commit_barrier(self, queue):
        """Sink publisher confirms land the record before any commit barrier;
        ``commit()`` then confirms at ``remote_durable``; a live source
        consumes the record unchanged."""
        sink = AmqpSink({"url": AMQP_URL, "routing_key": queue})
        payload = b'{"seq": 1, "origin": "live-amqp"}'
        sink.write(payload, {})  # returns only after the broker confirms
        # Records land before the commit barrier is ever called.
        assert self._message_count(queue) == 1
        receipt = sink.commit()
        assert receipt.tier == DeliveryTier.REMOTE_DURABLE
        assert receipt.confirmed is True
        assert "confirm" in receipt.notes

        source, results, thread = self._consume_one(queue)
        assert self._wait_for(lambda: len(results) >= 1)
        payload2, meta = results[0]
        assert payload2 == payload
        assert meta["amqp_queue"] == queue
        assert meta["amqp_redelivered"] is False
        source.ack(meta, AckDisposition.DELIVERED)
        time.sleep(0.5)  # let the thread-marshalled ack run on the connection thread
        self._stop_and_join(source, thread)
        # The acknowledged message is gone — nothing requeued on close.
        assert self._message_count(queue) == 0

    def test_live_no_early_ack_requeue_on_close_and_ack_after_disposition(self, queue):
        """(a) ``read()`` never settles the tag: closing without ack requeues
        the delivery. (b) The requeued delivery is redelivered with
        ``amqp_redelivered`` set. (c) ``ack(DELIVERED)`` settles the tag —
        nothing comes back after close (ack-after-disposition)."""
        self._publish(queue, b'{"n": 1}', message_id=f"mid-{uuid.uuid4().hex[:8]}")

        # First read: no ack. Closing must requeue the unacked delivery.
        source, results, thread = self._consume_one(queue)
        assert self._wait_for(lambda: len(results) >= 1)
        assert results[0][1]["amqp_redelivered"] is False
        self._stop_and_join(source, thread)
        assert self._wait_for(lambda: self._message_count(queue) == 1)

        # Second read: the requeued message is redelivered with the flag set.
        source2, results2, thread2 = self._consume_one(queue)
        assert self._wait_for(lambda: len(results2) >= 1)
        meta2 = results2[0][1]
        assert meta2["amqp_redelivered"] is True
        # ack-after-disposition: delivered settles the tag on the broker.
        source2.ack(meta2, AckDisposition.DELIVERED)
        time.sleep(0.5)
        self._stop_and_join(source2, thread2)
        assert self._wait_for(lambda: self._message_count(queue) == 0)

    def test_live_dropped_disposition_nacks_without_requeue(self, queue):
        """``ack(DROPPED)`` maps to ``basic_nack(requeue=False)``: the message
        is settled dead — after the source stops it is not requeued."""
        self._publish(queue, b'{"drop": true}', message_id="mid-drop-1")

        source, results, thread = self._consume_one(queue)
        assert self._wait_for(lambda: len(results) >= 1)
        source.ack(results[0][1], AckDisposition.DROPPED)
        time.sleep(0.5)
        self._stop_and_join(source, thread)
        assert self._wait_for(lambda: self._message_count(queue) == 0)

    def test_live_require_message_id_refusal_requeues(self, queue, caplog):
        """Under ``require_message_id`` a delivery without a producer message
        ID is refused at intake: never enqueued, error-logged, and nacked with
        requeue=True — after the source stops the message is still in the
        queue, available for redelivery (at-least-once, not lost)."""
        self._publish(queue, b'{"anonymous": true}')  # no message_id

        with caplog.at_level(logging.ERROR, logger="tram.connectors.amqp.source"):
            source, results, thread = self._consume_one(queue, require_message_id=True)
            assert self._wait_for(
                lambda: "refused" in caplog.text and "require_message_id" in caplog.text
            )
            assert results == []  # refused — never enqueued
            self._stop_and_join(source, thread)
            # The refusal nacked with requeue=True (and any in-flight delivery
            # was requeued on close): the message survives, redeliverable.
            assert self._wait_for(lambda: self._message_count(queue) == 1)