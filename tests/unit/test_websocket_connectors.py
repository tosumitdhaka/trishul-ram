"""Tests for WebSocket source and sink connectors (v0.5.0)."""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── WebSocketSource ────────────────────────────────────────────────────────


def test_websocket_source_missing_dep():
    """Raises SourceError when websockets is not installed."""
    from tram.core.exceptions import SourceError
    from tram.connectors.websocket.source import WebSocketSource

    source = WebSocketSource({
        "type": "websocket",
        "url": "ws://localhost:9000",
    })

    mock_ws = MagicMock()
    mock_ws.__spec__ = None

    with patch.dict(sys.modules, {"websockets": None}):
        with pytest.raises(SourceError, match="websockets"):
            list(source.read())


def test_websocket_source_config_defaults():
    from tram.connectors.websocket.source import WebSocketSource

    source = WebSocketSource({"type": "websocket", "url": "ws://example.com"})
    assert source.url == "ws://example.com"
    assert source.extra_headers == {}
    assert source.ping_interval == 20
    assert source.reconnect is True
    assert source.reconnect_delay == 5


def test_websocket_source_config_custom():
    from tram.connectors.websocket.source import WebSocketSource

    source = WebSocketSource({
        "type": "websocket",
        "url": "wss://example.com/ws",
        "extra_headers": {"Authorization": "Bearer token"},
        "ping_interval": 30,
        "reconnect": False,
        "reconnect_delay": 10,
    })
    assert source.reconnect is False
    assert source.extra_headers == {"Authorization": "Bearer token"}


# ── WebSocketSink ──────────────────────────────────────────────────────────


def test_websocket_sink_missing_dep():
    """Raises SinkError when websockets is not installed."""
    from tram.core.exceptions import SinkError
    from tram.connectors.websocket.sink import WebSocketSink

    sink = WebSocketSink({"type": "websocket", "url": "ws://localhost:9000"})

    with patch.dict(sys.modules, {"websockets": None}):
        with pytest.raises(SinkError, match="websockets"):
            sink.write(b"data", {})


def test_websocket_sink_config_defaults():
    from tram.connectors.websocket.sink import WebSocketSink

    sink = WebSocketSink({"type": "websocket", "url": "ws://example.com"})
    assert sink.url == "ws://example.com"
    assert sink.extra_headers == {}


def test_websocket_sink_write_calls_asyncio_run():
    """write() calls asyncio.run with our _send coroutine."""
    from tram.connectors.websocket.sink import WebSocketSink

    sink = WebSocketSink({"type": "websocket", "url": "ws://example.com"})

    with patch("tram.connectors.websocket.sink.asyncio.run") as mock_run:
        mock_run.return_value = None

        # Mock websockets to avoid import error
        mock_ws_module = MagicMock()
        with patch.dict(sys.modules, {"websockets": mock_ws_module}):
            sink.write(b"hello", {})

        mock_run.assert_called_once()
