"""Tests for the Syslog source connector."""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.syslog.source import SyslogSource, _parse_syslog
from tram.core.exceptions import SourceError

# ── Parser unit tests ──────────────────────────────────────────────────────


class TestParseSyslog:
    def test_rfc3164_parses_correctly(self):
        msg = b"<34>Oct 11 22:14:15 mymachine su: 'su root' failed"
        meta = _parse_syslog(msg, "utf-8")
        # facility=4 (auth), severity=2 (crit) → pri=34
        assert meta["facility"] == "auth"
        assert meta["severity"] == "crit"
        assert meta["hostname"] == "mymachine"

    def test_rfc5424_parses_correctly(self):
        msg = b"<165>1 2023-01-01T00:00:00Z myhost myapp 1234 ID47 - test message"
        meta = _parse_syslog(msg, "utf-8")
        # pri=165: facility=20 (local4), severity=5 (notice)
        assert meta["facility"] == "local4"
        assert meta["severity"] == "notice"
        assert meta["hostname"] == "myhost"
        assert meta["appname"] == "myapp"

    def test_unparseable_message_returns_raw(self):
        msg = b"this is not syslog at all"
        meta = _parse_syslog(msg, "utf-8")
        assert meta["raw"] == "this is not syslog at all"
        assert meta["severity"] == ""

    def test_encoding_error_handled(self):
        # Invalid UTF-8 bytes should not raise
        meta = _parse_syslog(b"\xff\xfe bad bytes", "utf-8")
        assert "raw" in meta


# ── SyslogSource unit tests ────────────────────────────────────────────────


class TestSyslogSource:
    def test_invalid_protocol_raises(self):
        source = SyslogSource({"protocol": "grpc"})
        with pytest.raises(SourceError, match="unsupported protocol"):
            list(source.read())

    def test_udp_bind_failure_raises_source_error(self):
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            mock_sock.bind.side_effect = OSError("Permission denied")

            source = SyslogSource({"host": "0.0.0.0", "port": 514, "protocol": "udp"})
            with pytest.raises(SourceError, match="UDP bind failed"):
                list(source.read())

    def test_tcp_bind_failure_raises_source_error(self):
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            mock_sock.bind.side_effect = OSError("Address in use")

            source = SyslogSource({"host": "0.0.0.0", "port": 514, "protocol": "tcp"})
            with pytest.raises(SourceError, match="TCP bind failed"):
                list(source.read())

    def test_udp_yields_message_then_stops(self):
        rfc3164_msg = b"<34>Oct 11 22:14:15 myhost myapp: test message"

        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            # Return one message then timeout forever
            mock_sock.recvfrom.side_effect = [
                (rfc3164_msg, ("10.0.0.1", 514)),
                socket.timeout,
            ]

            source = SyslogSource({"protocol": "udp"})
            it = source.read()
            raw, meta = next(it)
            source.stop()
            # Drain remaining
            list(it)

        assert raw == rfc3164_msg
        assert meta["source_ip"] == "10.0.0.1"
        assert meta["hostname"] == "myhost"

    def test_stop_terminates_stream(self):
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            mock_sock.recvfrom.side_effect = socket.timeout

            source = SyslogSource({"protocol": "udp"})
            source.stop()
            results = list(source.read())

        assert results == []
