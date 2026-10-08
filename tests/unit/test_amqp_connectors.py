"""Tests for AMQP source and sink connectors."""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.amqp.sink import AmqpSink
from tram.connectors.amqp.source import AmqpSource
from tram.connectors.bridge import BoundedBridgeQueue
from tram.core.exceptions import SinkError, SourceError
from tram.interfaces.base_sink import DeliveryTier
from tram.interfaces.base_source import AckDisposition


class TestAmqpSource:
    def test_import_error_raises_source_error(self):
        with patch.dict(sys.modules, {"pika": None}):
            source = AmqpSource({"queue": "myqueue"})
            with pytest.raises(SourceError, match="pika"):
                list(source.read())

    def test_read_yields_messages(self):
        mock_pika = MagicMock()
        mock_channel = MagicMock()
        mock_connection = MagicMock()
        mock_connection.channel.return_value = mock_channel
        mock_pika.BlockingConnection.return_value = mock_connection
        mock_pika.URLParameters = MagicMock(return_value=MagicMock())

        source = AmqpSource({"queue": "myqueue"})

        def fake_basic_consume(queue, on_message_callback, auto_ack):
            # Simulate a message being delivered
            method = MagicMock()
            method.delivery_tag = 1
            method.routing_key = "myqueue"
            on_message_callback(mock_channel, method, MagicMock(), b'{"x":1}')
            # Then stop consuming
            source._stop_event.set()

        mock_channel.basic_consume.side_effect = fake_basic_consume
        mock_channel.start_consuming.return_value = None

        with patch.dict(sys.modules, {"pika": mock_pika}):
            results = list(source.read())

        assert len(results) >= 1
        assert results[0][0] == b'{"x":1}'

    def test_connection_failure_raises_source_error(self):
        mock_pika = MagicMock()
        mock_pika.BlockingConnection.side_effect = Exception("connection refused")
        mock_pika.URLParameters = MagicMock(return_value=MagicMock())

        with patch.dict(sys.modules, {"pika": mock_pika}):
            source = AmqpSource({"queue": "q"})
            with pytest.raises(SourceError, match="connection refused"):
                list(source.read())

    def test_prefetch_count_set(self):
        mock_pika = MagicMock()
        mock_channel = MagicMock()
        mock_connection = MagicMock()
        mock_connection.channel.return_value = mock_channel
        mock_pika.BlockingConnection.return_value = mock_connection
        mock_pika.URLParameters = MagicMock(return_value=MagicMock())

        source = AmqpSource({"queue": "q", "prefetch_count": 5})
        source._stop_event.set()  # stop immediately

        def fake_basic_consume(queue, on_message_callback, auto_ack):
            pass  # don't call callback, stop_event already set

        mock_channel.basic_consume.side_effect = fake_basic_consume

        with patch.dict(sys.modules, {"pika": mock_pika}):
            list(source.read())

        mock_channel.basic_qos.assert_called_once_with(prefetch_count=5)


class TestAmqpSourceTagRetention:
    """V18-01 R2: no early ack — the delivery tag is retained until ack()."""

    def _source_with_mock_channel(self):
        source = AmqpSource({"queue": "myqueue"})
        source._connection = MagicMock()
        source._channel = MagicMock()
        return source

    def test_no_ack_at_intake(self):
        """read() delivers messages but never acks/nacks them (tag retained)."""
        mock_pika = MagicMock()
        mock_channel = MagicMock()
        mock_connection = MagicMock()
        mock_connection.channel.return_value = mock_channel
        mock_pika.BlockingConnection.return_value = mock_connection
        mock_pika.URLParameters = MagicMock(return_value=MagicMock())

        source = AmqpSource({"queue": "myqueue"})

        def fake_basic_consume(queue, on_message_callback, auto_ack):
            method = MagicMock()
            method.delivery_tag = 7
            method.routing_key = "myqueue"
            properties = MagicMock()
            properties.message_id = "mid-1"
            on_message_callback(mock_channel, method, properties, b'{"x":1}')
            source._stop_event.set()

        mock_channel.basic_consume.side_effect = fake_basic_consume

        with patch.dict(sys.modules, {"pika": mock_pika}):
            results = list(source.read())

        mock_channel.basic_ack.assert_not_called()
        mock_channel.basic_nack.assert_not_called()
        assert results[0][1]["amqp_delivery_tag"] == 7
        assert results[0][1]["amqp_message_id"] == "mid-1"

    def test_ack_marshalled_onto_connection_thread(self):
        """ack() schedules basic_ack via add_callback_threadsafe — the channel
        is only touched when the scheduled callback runs on the connection
        thread."""
        source = self._source_with_mock_channel()

        source.ack({"amqp_delivery_tag": 7}, AckDisposition.DELIVERED)

        source._connection.add_callback_threadsafe.assert_called_once()
        # Nothing touches the channel from the executor thread directly.
        source._channel.basic_ack.assert_not_called()
        # Run the callback as the pika connection thread would.
        callback = source._connection.add_callback_threadsafe.call_args[0][0]
        callback()
        source._channel.basic_ack.assert_called_once_with(delivery_tag=7)
        source._channel.basic_nack.assert_not_called()

    def test_ack_disposition_mapping(self):
        """delivered/filtered/dlq → basic_ack; dropped → basic_nack(requeue=False)."""
        for disposition, expected in [
            (AckDisposition.DELIVERED, "ack"),
            (AckDisposition.FILTERED, "ack"),
            (AckDisposition.DLQ, "ack"),
            (AckDisposition.DROPPED, "nack"),
        ]:
            source = self._source_with_mock_channel()
            source.ack({"amqp_delivery_tag": 3}, disposition)
            callback = source._connection.add_callback_threadsafe.call_args[0][0]
            callback()
            if expected == "ack":
                source._channel.basic_ack.assert_called_once_with(delivery_tag=3)
                source._channel.basic_nack.assert_not_called()
            else:
                source._channel.basic_nack.assert_called_once_with(
                    delivery_tag=3, requeue=False
                )
                source._channel.basic_ack.assert_not_called()

    def test_ack_noop_without_tag_or_with_auto_ack(self):
        source = self._source_with_mock_channel()
        source.ack({}, AckDisposition.DELIVERED)
        source._connection.add_callback_threadsafe.assert_not_called()

        source2 = self._source_with_mock_channel()
        source2.auto_ack = True
        source2.ack({"amqp_delivery_tag": 1}, AckDisposition.DELIVERED)
        source2._connection.add_callback_threadsafe.assert_not_called()

    def test_source_unit_id(self):
        """{namespace}/{producer message ID} when present; None otherwise."""
        source = AmqpSource({"queue": "q"})
        assert source.source_unit_id({"amqp_message_id": "abc"}) == "q/abc"
        assert source.source_unit_id({}) is None
        assert source.source_unit_id({"amqp_message_id": ""}) is None
        # The delivery tag is only an ack handle — never part of the identity.
        assert source.source_unit_id({"amqp_delivery_tag": 1}) is None


class TestSourceBridgeBounds:
    """V18-01 §9: internal source bridge queues bounded by count and bytes."""

    def test_bridge_count_bound_put_nowait_raises_full(self):
        q = BoundedBridgeQueue(max_count=2, max_bytes=0)
        q.put_nowait((b"a", {}))
        q.put_nowait((b"b", {}))
        with pytest.raises(queue.Full):
            q.put_nowait((b"c", {}))
        q.get_nowait()
        q.put_nowait((b"c", {}))  # room freed by the consumer

    def test_bridge_byte_bound(self):
        q = BoundedBridgeQueue(max_count=0, max_bytes=10)
        q.put_nowait((b"12345", {}))  # 5 bytes
        q.put_nowait((b"67890", {}))  # 10 bytes total
        with pytest.raises(queue.Full):
            q.put_nowait((b"x", {}))  # would exceed the byte budget
        q.get_nowait()  # 5 bytes released
        q.put_nowait((b"x", {}))  # 6 bytes — fits again

    def test_bridge_blocking_put_backpressures_until_room(self):
        q = BoundedBridgeQueue(max_count=1, max_bytes=4)
        q.put((b"aaaa", {}))
        put_result = []
        thread = threading.Thread(target=lambda: put_result.append(q.put((b"bb", {}))))
        thread.start()
        time.sleep(0.1)
        assert thread.is_alive()  # still blocked (queue full)
        q.get_nowait()  # consumer drains → producer unblocks
        thread.join(timeout=1.0)
        assert put_result == [None]  # blocking put completed without dropping

    def test_mqtt_queue_bounded_by_env(self):
        from tram.connectors.mqtt.source import MqttSource

        with patch.dict(
            os.environ,
            {"TRAM_SOURCE_BRIDGE_MAX_COUNT": "3", "TRAM_SOURCE_BRIDGE_MAX_BYTES": "12"},
        ):
            source = MqttSource({"host": "h", "topic": "t"})
        assert source._queue.maxsize == 3
        assert source._queue.max_bytes == 12
        source._queue.put_nowait((b"aaaa", {}))
        source._queue.put_nowait((b"bbbb", {}))
        source._queue.put_nowait((b"cccc", {}))
        with pytest.raises(queue.Full):
            source._queue.put_nowait((b"dddd", {}))

    def test_nats_queue_bounded_by_env(self):
        from tram.connectors.nats.source import NatsSource

        with patch.dict(
            os.environ,
            {"TRAM_SOURCE_BRIDGE_MAX_COUNT": "2", "TRAM_SOURCE_BRIDGE_MAX_BYTES": "6"},
        ):
            source = NatsSource({"subject": "events"})
        assert source._msg_queue.maxsize == 2
        assert source._msg_queue.max_bytes == 6
        source._msg_queue.put_nowait((b"aa", {}))
        source._msg_queue.put_nowait((b"bb", {}))
        with pytest.raises(queue.Full):
            source._msg_queue.put_nowait((b"cc", {}))

    def test_defaults_are_10000_and_16_mib(self):
        from tram.core.config import (
            _SOURCE_BRIDGE_MAX_BYTES_DEFAULT,
            _SOURCE_BRIDGE_MAX_COUNT_DEFAULT,
        )

        assert _SOURCE_BRIDGE_MAX_COUNT_DEFAULT == 10000
        assert _SOURCE_BRIDGE_MAX_BYTES_DEFAULT == 16 * 1024 * 1024


class TestAmqpSink:
    def test_import_error_raises_sink_error(self):
        with patch.dict(sys.modules, {"pika": None}):
            sink = AmqpSink({"routing_key": "test"})
            with pytest.raises(SinkError, match="pika"):
                sink.write(b"data", {})

    def test_publish_called(self):
        mock_pika = MagicMock()
        mock_channel = MagicMock()
        mock_connection = MagicMock()
        mock_connection.channel.return_value = mock_channel
        mock_pika.BlockingConnection.return_value = mock_connection
        mock_pika.URLParameters = MagicMock(return_value=MagicMock())
        mock_pika.BasicProperties = MagicMock(return_value=MagicMock())

        with patch.dict(sys.modules, {"pika": mock_pika}):
            sink = AmqpSink({"routing_key": "mykey"})
            sink.write(b'{"x":1}', {})

        mock_channel.basic_publish.assert_called_once()
        call_kwargs = mock_channel.basic_publish.call_args[1]
        assert call_kwargs["routing_key"] == "mykey"
        assert call_kwargs["body"] == b'{"x":1}'

    def test_connection_closed_after_publish(self):
        mock_pika = MagicMock()
        mock_channel = MagicMock()
        mock_connection = MagicMock()
        mock_connection.channel.return_value = mock_channel
        mock_pika.BlockingConnection.return_value = mock_connection
        mock_pika.URLParameters = MagicMock(return_value=MagicMock())
        mock_pika.BasicProperties = MagicMock(return_value=MagicMock())

        with patch.dict(sys.modules, {"pika": mock_pika}):
            sink = AmqpSink({"routing_key": "k"})
            sink.write(b"data", {})

        mock_connection.close.assert_called_once()

    # ── Delivery tier / publisher confirms (V18-01 sections 6 and 12.1) ────

    @staticmethod
    def _make_pika_mock():
        mock_pika = MagicMock()
        mock_channel = MagicMock()
        mock_connection = MagicMock()
        mock_connection.channel.return_value = mock_channel
        mock_pika.BlockingConnection.return_value = mock_connection
        mock_pika.URLParameters = MagicMock(return_value=MagicMock())
        mock_pika.BasicProperties = MagicMock(return_value=MagicMock())
        return mock_pika, mock_channel, mock_connection

    def test_delivery_capability_declared_remote_durable(self):
        cap = AmqpSink.delivery_capability
        assert cap is not None
        assert cap.tier == DeliveryTier.REMOTE_DURABLE
        assert cap.replay_safe is False

    def test_publish_uses_publisher_confirms(self):
        """Audit finding (pending decision 1): pika supports publisher confirms
        via channel.confirm_delivery(); every publish enables them before
        basic_publish, so the synchronous return is broker-confirmed."""
        mock_pika, mock_channel, _ = self._make_pika_mock()

        with patch.dict(sys.modules, {"pika": mock_pika}):
            sink = AmqpSink({"routing_key": "mykey"})
            sink.write(b'{"x":1}', {})

        mock_channel.confirm_delivery.assert_called_once()
        mock_channel.basic_publish.assert_called_once()

    def test_nack_failure_raises_sink_error(self):
        """A broker basic.nack (or unroutable/connection failure) raises — no
        false clean success."""
        mock_pika, mock_channel, _ = self._make_pika_mock()
        mock_channel.basic_publish.side_effect = Exception("NackError: message nacked")

        with patch.dict(sys.modules, {"pika": mock_pika}):
            sink = AmqpSink({"routing_key": "mykey"})
            with pytest.raises(SinkError, match="AMQP publish failed"):
                sink.write(b"data", {})

    def test_commit_confirms_remote_durable(self):
        sink = AmqpSink({"routing_key": "mykey"})
        receipt = sink.commit()
        assert receipt.tier == DeliveryTier.REMOTE_DURABLE
        assert receipt.confirmed is True
        assert "confirm" in receipt.notes

    def test_latched_error_defaults_to_none(self):
        # pika blocking mode surfaces publish failures synchronously as
        # SinkError; this per-publish connection model buffers no delivery
        # errors. Live-broker confirm behavior is the V18-02 broker-test gate.
        assert AmqpSink({"routing_key": "mykey"}).latched_error() is None
