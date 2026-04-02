"""Tests for AMQP source and sink connectors."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.amqp.sink import AmqpSink
from tram.connectors.amqp.source import AmqpSource
from tram.core.exceptions import SinkError, SourceError


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
