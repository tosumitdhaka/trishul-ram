"""Tests for the Syslog source connector."""

from __future__ import annotations

import socket
import threading
import time
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


# ── TCP RFC 6587 framing tests ──────────────────────────────────────────────


def _octet_frame(msg: bytes) -> bytes:
    """Frame ``msg`` under RFC 6587 octet-counting: ``MSGLEN SP MSG``."""
    return str(len(msg)).encode("ascii") + b" " + msg


def _tcp_items(chunks, config=None, addr=("10.0.0.1", 514)):
    """Feed ``chunks`` through ``_read_tcp_conn`` and collect every record."""
    conn = MagicMock()
    conn.recv.side_effect = list(chunks) + [b""]  # b"" = peer EOF
    source = SyslogSource({"protocol": "tcp", **(config or {})})
    return list(source._read_tcp_conn(conn, addr))


class TestTcpNewlineFraming:
    def test_single_message(self):
        msg = b"<34>Oct 11 22:14:15 mymachine su: 'su root' failed"
        items = _tcp_items([msg + b"\n"])
        assert [raw for raw, _ in items] == [msg]
        assert items[0][1]["source_ip"] == "10.0.0.1"

    def test_message_split_across_multiple_recvs(self):
        msg = b"<34>Oct 11 22:14:15 mymachine su: 'su root' failed"
        payload = msg + b"\n"
        chunks = [payload[i:i + 7] for i in range(0, len(payload), 7)]
        assert [raw for raw, _ in _tcp_items(chunks)] == [msg]

    def test_multiple_messages_in_one_recv(self):
        m1 = b"<34>Oct 11 22:14:15 host1 su: one failed"
        m2 = b"<165>1 2023-01-01T00:00:00Z host2 app 1 ID1 - two"
        items = _tcp_items([m1 + b"\n" + m2 + b"\n"])
        assert [raw for raw, _ in items] == [m1, m2]

    def test_crlf_stripped(self):
        msg = b"<34>Oct 11 22:14:15 mymachine su: 'su root' failed"
        items = _tcp_items([msg + b"\r\n"])
        assert [raw for raw, _ in items] == [msg]

    def test_eof_flushes_final_message_without_newline(self):
        msg = b"<34>Oct 11 22:14:15 mymachine su: 'su root' failed"
        items = _tcp_items([msg])  # connection closes without a trailing LF
        assert [raw for raw, _ in items] == [msg]


class TestTcpOctetFraming:
    def test_single_message(self):
        msg = b"<34>Oct 11 22:14:15 mymachine su: 'su root' failed"
        items = _tcp_items([_octet_frame(msg)])
        assert [raw for raw, _ in items] == [msg]

    def test_message_split_across_multiple_recvs(self):
        msg = b"<34>Oct 11 22:14:15 mymachine su: 'su root' failed"
        frame = _octet_frame(msg)
        chunks = [frame[i:i + 5] for i in range(0, len(frame), 5)]
        assert [raw for raw, _ in _tcp_items(chunks)] == [msg]

    def test_multiple_messages_in_one_recv(self):
        m1 = b"<34>Oct 11 22:14:15 host1 su: one failed"
        m2 = b"<165>1 2023-01-01T00:00:00Z host2 app 1 ID1 - two"
        items = _tcp_items([_octet_frame(m1) + _octet_frame(m2)])
        assert [raw for raw, _ in items] == [m1, m2]

    def test_eof_drops_partial_frame(self):
        msg = b"<34>Oct 11 22:14:15 mymachine su: 'su root' failed"
        frame = _octet_frame(msg)
        items = _tcp_items([frame[:10]])  # truncated mid-frame, then EOF
        assert items == []

    def test_empty_message_skipped(self):
        m1 = b"<34>Oct 11 22:14:15 host app: ok"
        items = _tcp_items([b"0 " + _octet_frame(m1)])
        assert [raw for raw, _ in items] == [m1]


class TestTcpFramingDetection:
    def test_single_digit_prefix_is_octet(self):
        msg = b"<34>hi!"
        items = _tcp_items([_octet_frame(msg)])
        assert [raw for raw, _ in items] == [msg]

    def test_multi_digit_prefix_is_octet(self):
        msg = b"<34>" + b"x" * 200  # forces a 3-digit length prefix
        items = _tcp_items([_octet_frame(msg)])
        assert [raw for raw, _ in items] == [msg]

    def test_non_digit_first_byte_is_newline(self):
        msg = b"<34>Oct 11 22:14:15 host app: ok"
        items = _tcp_items([msg + b"\n"])
        assert [raw for raw, _ in items] == [msg]

    def test_digit_run_interrupted_falls_back_to_newline(self):
        payload = b"1x Oct 11 22:14:15 host app: ok\n"
        items = _tcp_items([payload])
        assert [raw for raw, _ in items] == [payload[:-1]]

    def test_detection_waits_when_prefix_incomplete(self):
        msg = b"<34>Oct 11 22:14:15 host app: ok"
        frame = _octet_frame(msg)
        # First recv delivers only the opening digit of the length prefix.
        items = _tcp_items([frame[:1], frame[1:]])
        assert [raw for raw, _ in items] == [msg]


class TestTcpOversizedGuard:
    def test_octet_oversized_dropped_and_stream_continues(self):
        m1 = b"<34>ok"  # 6 bytes, under the 10-byte cap
        oversized_len = 100
        chunk = (
            str(oversized_len).encode("ascii")
            + b" "
            + b"z" * oversized_len
            + _octet_frame(m1)
        )
        items = _tcp_items([chunk], config={"max_message_size": 10})
        assert [raw for raw, _ in items] == [m1]

    def test_octet_oversized_split_across_recvs(self):
        m1 = b"<34>ok"  # 6 bytes, under the 10-byte cap
        oversized_len = 100
        frame = str(oversized_len).encode("ascii") + b" " + b"z" * oversized_len
        chunks = [frame[:40], frame[40:] + _octet_frame(m1)]
        items = _tcp_items(chunks, config={"max_message_size": 10})
        assert [raw for raw, _ in items] == [m1]

    def test_newline_unterminated_oversized_dropped(self):
        m1 = b"<34>Oct 11 22:14:15 host app: ok"
        big = b"<34>Oct 11 22:14:15 host app: " + b"y" * 100  # no newline
        items = _tcp_items([big, m1 + b"\n"], config={"max_message_size": 50})
        assert [raw for raw, _ in items] == [m1]

    def test_newline_terminated_oversized_dropped(self):
        # An oversized message arriving WITH its trailing LF must be dropped
        # just like an unterminated one (guard consistency with octet mode).
        m1 = b"<34>ok"
        big = b"<34>Oct 11 22:14:15 host app: " + b"y" * 100
        items = _tcp_items([big + b"\n", m1 + b"\n"], config={"max_message_size": 50})
        assert [raw for raw, _ in items] == [m1]

    def test_newline_terminated_oversized_split_across_recvs(self):
        m1 = b"<34>ok"
        big = b"<34>Oct 11 22:14:15 host app: " + b"y" * 100 + b"\n"
        items = _tcp_items([big[:40], big[40:], m1 + b"\n"], config={"max_message_size": 50})
        assert [raw for raw, _ in items] == [m1]

    def test_newline_at_size_limit_passes(self):
        head = b"<34>Oct 11 22:14:15 host app: "
        msg = head + b"y" * (50 - len(head))  # exactly at the limit
        assert len(msg) == 50
        items = _tcp_items([msg + b"\n"], config={"max_message_size": 50})
        assert [raw for raw, _ in items] == [msg]


class TestTcpEdgeCases:
    def test_empty_and_whitespace_lines_skipped(self):
        m1 = b"<34>Oct 11 22:14:15 host app: one"
        payload = b"\n   \n" + m1 + b"\n\n"
        items = _tcp_items([payload])
        assert [raw for raw, _ in items] == [m1]

    def test_lone_digit_then_eof_falls_back_to_newline(self):
        # A single digit byte followed by EOF never resolves to octet framing;
        # the tail is flushed as a newline-framed message.
        items = _tcp_items([b"5"])
        assert [raw for raw, _ in items] == [b"5"]

    def test_malformed_octet_stream_closes_without_crash(self):
        m1 = b"<34>Oct 11 22:14:15 host app: ok"
        payload = _octet_frame(m1) + b"garbage-with-no-space"
        items = _tcp_items([payload])
        assert [raw for raw, _ in items] == [m1]

    def test_invalid_octet_prefix_closes_without_crash(self):
        m1 = b"<34>Oct 11 22:14:15 host app: ok"
        payload = _octet_frame(m1) + b"zz <34>bad"
        items = _tcp_items([payload])
        assert [raw for raw, _ in items] == [m1]


class TestTcpEndToEnd:
    def test_read_tcp_streams_and_stops(self):
        m1 = b"<34>Oct 11 22:14:15 mymachine su: 'su root' failed"
        m2 = b"<165>1 2023-01-01T00:00:00Z host2 app 1 ID1 - two"
        srv = MagicMock()
        conn = MagicMock()
        srv.accept.side_effect = [(conn, ("10.0.0.1", 514)), socket.timeout]
        conn.recv.side_effect = [m1 + b"\n" + m2 + b"\n", b""]

        with patch("socket.socket", return_value=srv):
            source = SyslogSource({"protocol": "tcp"})
            it = source.read()
            raw1, meta1 = next(it)
            raw2, meta2 = next(it)
            source.stop()
            list(it)  # drain: accept times out, loop exits on stop event

        assert raw1 == m1
        assert meta1["source_ip"] == "10.0.0.1"
        assert meta1["hostname"] == "mymachine"
        assert raw2 == m2


# ── TCP connection concurrency tests ───────────────────────────────────────


def _free_port() -> int:
    """Return an ephemeral port that is free right now."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_for(records, present, timeout=5.0):
    """Poll ``records`` until every raw message in ``present`` has arrived."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raws = {raw for raw, _ in records}
        if present.issubset(raws):
            return
        time.sleep(0.01)
    raise AssertionError(
        f"timed out waiting for messages {present}; got {[raw for raw, _ in records]}"
    )


class TestTcpConcurrency:
    """TCP connections are served concurrently by per-connection threads."""

    def _consume(self, source, records):
        for rec in source.read():
            records.append(rec)

    def test_two_connections_served_while_one_stays_open(self):
        # Starvation repro: a persistent sender (c1) stays open; a second
        # connection must still be served rather than rotting in the backlog.
        port = _free_port()
        source = SyslogSource({"protocol": "tcp", "host": "127.0.0.1", "port": port})
        records = []
        thread = threading.Thread(target=self._consume, args=(source, records), daemon=True)
        thread.start()
        c1 = c2 = None
        try:
            m1 = b"<34>Oct 11 22:14:15 host app: from-client-one"
            m2 = b"<165>1 2023-01-01T00:00:00Z host2 app 1 ID1 - from-client-two"
            m3 = b"<34>Oct 11 22:14:16 host app: still-served"
            c1 = socket.create_connection(("127.0.0.1", port))
            c2 = socket.create_connection(("127.0.0.1", port))
            c1.sendall(m1 + b"\n")
            c2.sendall(m2 + b"\n")
            _wait_for(records, {m1, m2})
            # c1 is still open and being served: a later message must arrive.
            c1.sendall(m3 + b"\n")
            _wait_for(records, {m1, m2, m3})
        finally:
            source.stop()
            for c in (c1, c2):
                if c is not None:
                    c.close()
            thread.join(timeout=2.0)
        raws = [raw for raw, _ in records]
        assert m1 in raws and m2 in raws and m3 in raws

    def test_connection_refused_at_max_cap(self):
        port = _free_port()
        source = SyslogSource(
            {
                "protocol": "tcp",
                "host": "127.0.0.1",
                "port": port,
                "max_connections": 2,
            }
        )
        records = []
        thread = threading.Thread(target=self._consume, args=(source, records), daemon=True)
        thread.start()
        c1 = c2 = c3 = None
        try:
            m1 = b"<34>Oct 11 22:14:15 host app: one"
            m2 = b"<34>Oct 11 22:14:15 host app: two"
            m3 = b"<34>Oct 11 22:14:15 host app: three"
            c1 = socket.create_connection(("127.0.0.1", port))
            c1.sendall(m1 + b"\n")
            _wait_for(records, {m1})
            c2 = socket.create_connection(("127.0.0.1", port))
            c2.sendall(m2 + b"\n")
            _wait_for(records, {m1, m2})
            # Cap of 2 reached: the third connection is refused (immediate EOF).
            c3 = socket.create_connection(("127.0.0.1", port))
            c3.settimeout(5.0)
            assert c3.recv(1024) == b""
            # Existing connections keep working after the refusal.
            c1.sendall(m3 + b"\n")
            _wait_for(records, {m1, m2, m3})
        finally:
            source.stop()
            for c in (c1, c2, c3):
                if c is not None:
                    c.close()
            thread.join(timeout=2.0)

    def test_stop_ends_handler_threads_cleanly(self):
        port = _free_port()
        source = SyslogSource({"protocol": "tcp", "host": "127.0.0.1", "port": port})
        records = []
        thread = threading.Thread(target=self._consume, args=(source, records), daemon=True)
        thread.start()
        c1 = c2 = None
        try:
            m1 = b"<34>Oct 11 22:14:15 host app: one"
            m2 = b"<34>Oct 11 22:14:15 host app: two"
            c1 = socket.create_connection(("127.0.0.1", port))
            c2 = socket.create_connection(("127.0.0.1", port))
            c1.sendall(m1 + b"\n")
            c2.sendall(m2 + b"\n")
            _wait_for(records, {m1, m2})
            # Both connections are still open and idle when stop() is called.
            source.stop()
        finally:
            for c in (c1, c2):
                if c is not None:
                    c.close()
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "stream generator did not terminate after stop()"
        # Per-connection handler threads must have ended too.
        deadline = time.monotonic() + 2.0
        leftover = [t for t in threading.enumerate() if t.name.startswith("syslog-tcp-")]
        while leftover and time.monotonic() < deadline:
            time.sleep(0.01)
            leftover = [t for t in threading.enumerate() if t.name.startswith("syslog-tcp-")]
        assert leftover == []
