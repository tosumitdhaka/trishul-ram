"""Tests for NATS source and sink connectors."""
from __future__ import annotations
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from tram.connectors.nats.source import NatsSource
from tram.connectors.nats.sink import NatsSink
from tram.core.exceptions import SinkError, SourceError


class TestNatsSink:
    def test_import_error_raises_sink_error(self):
        with patch.dict(sys.modules, {"nats": None}):
            sink = NatsSink({"subject": "test.subject"})
            with pytest.raises(SinkError, match="nats-py"):
                sink.write(b"data", {})

    def test_publish_called(self):
        mock_nc = AsyncMock()
        mock_nats = MagicMock()
        mock_nats.connect = AsyncMock(return_value=mock_nc)

        with patch.dict(sys.modules, {"nats": mock_nats}):
            sink = NatsSink({"subject": "test.sub"})
            sink.write(b'{"x":1}', {})

        mock_nc.publish.assert_called_once_with("test.sub", b'{"x":1}')
        mock_nc.flush.assert_called_once()
        mock_nc.close.assert_called_once()

    def test_credentials_file_passed(self):
        mock_nc = AsyncMock()
        mock_nats = MagicMock()
        mock_nats.connect = AsyncMock(return_value=mock_nc)

        with patch.dict(sys.modules, {"nats": mock_nats}):
            sink = NatsSink({"subject": "test.sub", "credentials_file": "/tmp/creds.nk"})
            sink.write(b"data", {})

        mock_nats.connect.assert_called_once()
        call_kwargs = mock_nats.connect.call_args[1]
        assert call_kwargs["credentials"] == "/tmp/creds.nk"


class TestNatsSource:
    def test_import_error_raises_source_error(self):
        with patch.dict(sys.modules, {"nats": None}):
            source = NatsSource({"subject": "test.sub"})
            with pytest.raises(SourceError, match="nats-py"):
                list(source.read())

    def test_read_yields_messages(self):
        mock_nats = MagicMock()
        source = NatsSource({"subject": "test.sub"})

        # Pre-populate the message queue and set stop event
        source._msg_queue.put((b'{"x":1}', {"nats_subject": "test.sub"}))
        source._stop_event.set()

        # Mock the background thread so it doesn't actually run
        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            with patch.dict(sys.modules, {"nats": mock_nats}):
                results = list(source.read())

        assert len(results) >= 1
        assert results[0][0] == b'{"x":1}'
