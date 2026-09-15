"""Syslog source connector — receives syslog messages over UDP or TCP."""

from __future__ import annotations

import logging
import queue
import re
import socket
import threading
from collections.abc import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)

# RFC 5424: <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID STRUCTURED-DATA MSG
_RFC5424 = re.compile(
    r"^<(?P<pri>\d+)>"
    r"(?P<version>\d+)\s+"
    r"(?P<timestamp>\S+)\s+"
    r"(?P<hostname>\S+)\s+"
    r"(?P<appname>\S+)\s+"
    r"(?P<procid>\S+)\s+"
    r"(?P<msgid>\S+)\s+"
    r"(?P<structured_data>\S+)"
    r"(?:\s+(?P<msg>.*))?$"
)

# RFC 3164: <PRI>TIMESTAMP HOSTNAME TAG: MSG
_RFC3164 = re.compile(
    r"^<(?P<pri>\d+)>"
    r"(?P<timestamp>\w{3}\s+\d+\s+\d+:\d+:\d+)\s+"
    r"(?P<hostname>\S+)\s+"
    r"(?P<tag>[^:]+):\s*"
    r"(?P<msg>.*)$"
)

_FACILITY_NAMES = [
    "kern", "user", "mail", "daemon", "auth", "syslog", "lpr", "news",
    "uucp", "cron", "authpriv", "ftp", "ntp", "security", "console",
    "solaris-cron", "local0", "local1", "local2", "local3", "local4",
    "local5", "local6", "local7",
]
_SEVERITY_NAMES = [
    "emerg", "alert", "crit", "err", "warning", "notice", "info", "debug",
]


class _FramingError(Exception):
    """Raised when a byte stream cannot be parsed as RFC 6587 syslog framing."""


def _is_digit(b: int) -> bool:
    """Return True if the byte is an ASCII digit (0-9)."""
    return 48 <= b <= 57


class _Rfc6587Framer:
    """Incremental RFC 6587 framing parser for one syslog-over-TCP connection.

    RFC 6587 defines two framing modes:

    * Octet-counted: each message is ``MSGLEN SP SYSLOG-MSG``, where MSGLEN
      is the ASCII byte length of SYSLOG-MSG.
    * Non-transparent: each message is terminated by a single LF.

    The mode is chosen once per connection from the first bytes: if the
    leading bytes form a valid octet-counted length prefix (a run of ASCII
    digits terminated by a space), the connection is octet-counted; otherwise
    it falls back to newline-delimited framing.

    ``feed`` is incremental: it buffers partial frames across ``recv`` calls,
    emits every complete message, and extracts as many messages as a single
    ``recv`` chunk may contain. ``max_message_size`` bounds the buffer: an
    octet-counted message declaring a larger length is dropped (while keeping
    frame boundaries in sync), and a newline-framed message longer than the
    limit is dropped with a warning — whether or not it has been terminated.
    """

    #: Longest plausible MSGLEN prefix; real syslog messages fit in 5 digits.
    _MAX_PREFIX_LEN = 16

    def __init__(self, max_message_size: int, peer: str) -> None:
        self.max_message_size = max_message_size
        self.peer = peer
        self._buffer = bytearray()
        self._mode: str | None = None  # None | "octet" | "newline"
        self._discard = 0  # bytes of a rejected oversized octet frame to skip
        self.error: str | None = None  # fatal framing error, if any

    def feed(self, data: bytes) -> list[bytes]:
        """Feed bytes read from the socket; return all complete messages.

        If the stream becomes unframable, ``self.error`` is set and the
        messages extracted before the offending bytes are still returned.
        """
        if self._discard:
            skip = min(self._discard, len(data))
            data = data[skip:]
            self._discard -= skip
            if not data:
                return []
        self._buffer.extend(data)
        messages: list[bytes] = []
        while True:
            if self._mode is None:
                mode = self._detect_mode()
                if mode is None:
                    break  # need more bytes before the framing can be decided
                self._mode = mode
            try:
                if self._mode == "octet":
                    if not self._extract_octet(messages):
                        break
                elif not self._extract_newline(messages):
                    break
            except _FramingError as exc:
                self.error = str(exc)
                break
        return messages

    def finish(self) -> list[bytes]:
        """Flush the tail of the stream once the peer closes the connection.

        A final newline-framed message without a trailing LF is still emitted;
        a partial octet-counted frame is incomplete and is dropped.
        """
        if not self._buffer:
            return []
        if self._mode is None:
            # Framing was never decidable (e.g. a lone digit then EOF): treat
            # the tail as newline-framed, matching the fallback rule.
            self._mode = "newline"
        if self._mode == "octet":
            self._buffer.clear()
            return []
        msg = bytes(self._buffer)
        if msg.endswith(b"\r"):
            msg = msg[:-1]
        self._buffer.clear()
        return [msg] if msg.strip() else []

    def _detect_mode(self) -> str | None:
        """Decide framing from the leading bytes; None if undecidable yet."""
        buf = self._buffer
        if not buf or not _is_digit(buf[0]):
            return "newline"
        for i in range(1, len(buf)):
            if buf[i] == 0x20:
                return "octet"
            if not _is_digit(buf[i]):
                return "newline"
            if i >= self._MAX_PREFIX_LEN:
                return "newline"
        return None

    def _extract_octet(self, messages: list[bytes]) -> bool:
        """Extract one octet-counted frame; True if more data may follow."""
        buf = self._buffer
        sp = buf.find(b" ")
        if sp < 0:
            if len(buf) > self._MAX_PREFIX_LEN:
                raise _FramingError(
                    f"octet-counted frame from {self.peer} has no length terminator"
                )
            return False
        digits = bytes(buf[:sp])
        if not digits.isdigit():
            raise _FramingError(
                f"octet-counted frame from {self.peer} has invalid "
                f"length prefix {digits!r}"
            )
        msg_len = int(digits)
        total = sp + 1 + msg_len
        if msg_len > self.max_message_size:
            logger.warning(
                "Syslog TCP: dropping octet-counted message of %d bytes from "
                "%s (max_message_size=%d)",
                msg_len,
                self.peer,
                self.max_message_size,
            )
            if len(buf) >= total:
                del buf[:total]
            else:
                self._discard = total - len(buf)
                del buf[:]
            return True
        if len(buf) < total:
            return False
        msg = bytes(buf[sp + 1:total])
        del buf[:total]
        if msg.strip():
            messages.append(msg)
        return True

    def _extract_newline(self, messages: list[bytes]) -> bool:
        """Extract one newline-framed message; True if more data may follow."""
        buf = self._buffer
        nl = buf.find(b"\n")
        if nl < 0:
            if len(buf) > self.max_message_size:
                logger.warning(
                    "Syslog TCP: dropping %d buffered bytes of an unterminated "
                    "newline-framed message from %s (max_message_size=%d)",
                    len(buf),
                    self.peer,
                    self.max_message_size,
                )
                del buf[:]
            return False
        if nl > self.max_message_size:
            logger.warning(
                "Syslog TCP: dropping newline-framed message of %d bytes from "
                "%s (max_message_size=%d)",
                nl,
                self.peer,
                self.max_message_size,
            )
            del buf[:nl + 1]
            return True
        msg = bytes(buf[:nl])
        del buf[:nl + 1]
        if msg.endswith(b"\r"):
            msg = msg[:-1]
        if msg.strip():
            messages.append(msg)
        return True


def _parse_syslog(raw: bytes, encoding: str) -> dict:
    """Parse a syslog message, returning a metadata dict."""
    try:
        text = raw.decode(encoding, errors="replace").strip()
    except Exception:
        text = ""

    meta: dict = {"raw": text}

    m = _RFC5424.match(text) or _RFC3164.match(text)
    if m:
        groups = m.groupdict()
        pri = int(groups.get("pri", 0))
        facility_num = pri >> 3
        severity_num = pri & 0x07
        meta["facility"] = _FACILITY_NAMES[facility_num] if facility_num < len(_FACILITY_NAMES) else str(facility_num)
        meta["severity"] = _SEVERITY_NAMES[severity_num] if severity_num < len(_SEVERITY_NAMES) else str(severity_num)
        meta["hostname"] = groups.get("hostname") or groups.get("hostname", "")
        meta["appname"] = groups.get("appname") or groups.get("tag", "")
        meta["timestamp"] = groups.get("timestamp", "")
        meta["message"] = groups.get("msg", text)
    else:
        meta["facility"] = ""
        meta["severity"] = ""
        meta["hostname"] = ""
        meta["appname"] = ""
        meta["timestamp"] = ""
        meta["message"] = text

    return meta


@register_source("syslog")
class SyslogSource(BaseSource):
    """Receive syslog messages over UDP or TCP, operating in stream mode.

    Parses RFC 3164 and RFC 5424 messages. TCP transport uses RFC 6587
    framing (octet-counted or newline-delimited, detected per connection) so
    messages are never truncated or merged across ``recv`` boundaries.
    Yields ``(raw_bytes, meta)`` where meta includes: ``source_ip``, ``port``,
    ``facility``, ``severity``, ``hostname``, ``appname``, ``timestamp``,
    ``message``.

    Uses the ``socket`` stdlib module — no extra dependencies required.

    Config keys:
        host        (str, default "0.0.0.0")   Bind address.
        port        (int, default 514)          Bind port.
        protocol    (str, default "udp")        "udp" or "tcp".
        buffer_size (int, default 65535)        UDP datagram / TCP read size.
        max_message_size (int, default 65535)   TCP maximum syslog message
                                                size; larger messages are
                                                dropped with a warning in
                                                both framing modes.
        max_connections (int, default 64)       TCP maximum simultaneous
                                                client connections; further
                                                connections are refused and
                                                closed.
        encoding    (str, default "utf-8")      Message decoding charset.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config.get("host", "0.0.0.0")
        self.port: int = int(config.get("port", 514))
        self.protocol: str = config.get("protocol", "udp").lower()
        self.buffer_size: int = int(config.get("buffer_size", 65535))
        self.max_message_size: int = int(config.get("max_message_size", 65535))
        self.max_connections: int = int(config.get("max_connections", 64))
        self.encoding: str = config.get("encoding", "utf-8")
        self._stop_event: threading.Event = threading.Event()
        self._conns: set[socket.socket] = set()
        self._conns_lock: threading.Lock = threading.Lock()
        self._listener_sock: socket.socket | None = None

    def stop(self) -> None:
        """Signal the stream to stop and close the listener and live sockets."""
        self._stop_event.set()
        self._close_all_conns()
        listener = self._listener_sock
        if listener is not None:
            try:
                listener.close()
            except Exception:
                pass

    def _bind_udp(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(1.0)
            sock.bind((self.host, self.port))
            return sock
        except Exception as exc:
            raise SourceError(
                f"Syslog UDP bind failed on {self.host}:{self.port} — {exc}"
            ) from exc

    def _bind_tcp(self):
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.settimeout(1.0)
            srv.bind((self.host, self.port))
            srv.listen(5)
            return srv
        except Exception as exc:
            raise SourceError(
                f"Syslog TCP bind failed on {self.host}:{self.port} — {exc}"
            ) from exc

    def test_connection(self) -> dict:
        host = self.config.get("host", "0.0.0.0")
        port = self.config.get("port", 514)
        protocol = self.config.get("protocol", "udp")
        return {"ok": True, "latency_ms": None, "detail": f"Local {protocol.upper()} listener on {host}:{port}"}

    def read(self) -> Iterator[tuple[bytes, dict]]:
        if self.protocol == "udp":
            yield from self._read_udp()
        elif self.protocol == "tcp":
            yield from self._read_tcp()
        else:
            raise SourceError(f"Syslog: unsupported protocol '{self.protocol}' (use udp or tcp)")

    def _read_udp(self) -> Iterator[tuple[bytes, dict]]:
        sock = self._bind_udp()
        logger.info(
            "Syslog UDP source listening",
            extra={"host": self.host, "port": self.port},
        )
        try:
            while not self._stop_event.is_set():
                try:
                    raw, addr = sock.recvfrom(self.buffer_size)
                except TimeoutError:
                    continue
                except Exception as exc:
                    logger.warning("Syslog UDP recv error: %s", exc)
                    continue
                source_ip, src_port = addr
                meta = _parse_syslog(raw, self.encoding)
                meta["source_ip"] = source_ip
                meta["port"] = src_port
                logger.debug(
                    "Syslog UDP message received",
                    extra={"source_ip": source_ip, "bytes": len(raw)},
                )
                yield raw, meta
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _read_tcp(self) -> Iterator[tuple[bytes, dict]]:
        srv = self._bind_tcp()
        self._listener_sock = srv
        logger.info(
            "Syslog TCP source listening",
            extra={"host": self.host, "port": self.port},
        )
        records_q: queue.Queue = queue.Queue()
        with self._conns_lock:
            self._conns.clear()
        try:
            while not self._stop_event.is_set():
                # Drain records queued by the per-connection handler threads so
                # they keep flowing through this generator's yield path.
                while True:
                    try:
                        item = records_q.get_nowait()
                    except queue.Empty:
                        break
                    yield item
                try:
                    conn, addr = srv.accept()
                except TimeoutError:
                    continue
                except Exception as exc:
                    if not self._stop_event.is_set():
                        logger.warning("Syslog TCP accept error: %s", exc)
                    continue
                with self._conns_lock:
                    over_cap = len(self._conns) >= self.max_connections
                    if not over_cap:
                        self._conns.add(conn)
                if over_cap:
                    self._refuse_connection(conn, addr)
                    continue
                source_ip, src_port = addr
                handler = threading.Thread(
                    target=self._serve_connection,
                    args=(conn, addr, records_q),
                    name=f"syslog-tcp-{source_ip}:{src_port}",
                    daemon=True,
                )
                handler.start()
        finally:
            try:
                srv.close()
            except Exception:
                pass
            if self._listener_sock is srv:
                self._listener_sock = None
            self._close_all_conns()

    def _serve_connection(self, conn, addr, records_q: queue.Queue) -> None:
        """Serve one accepted connection until it closes, on its own thread."""
        try:
            for record in self._read_tcp_conn(conn, addr):
                records_q.put(record)
        finally:
            with self._conns_lock:
                self._conns.discard(conn)

    def _refuse_connection(self, conn, addr) -> None:
        """Close a connection refused because ``max_connections`` was reached."""
        source_ip, src_port = addr
        logger.warning(
            "Syslog TCP: refusing connection from %s:%s (max_connections=%d)",
            source_ip,
            src_port,
            self.max_connections,
        )
        try:
            conn.close()
        except Exception:
            pass

    def _close_all_conns(self) -> None:
        """Close every live TCP connection, dropping it from the tracking set."""
        with self._conns_lock:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass

    def _read_tcp_conn(self, conn, addr) -> Iterator[tuple[bytes, dict]]:
        """Frame one TCP connection under RFC 6587 until it closes or stops.

        Keeps per-connection buffering so partial frames split across ``recv``
        calls and several complete messages within a single ``recv`` are both
        handled correctly.
        """
        source_ip, src_port = addr
        peer = f"{source_ip}:{src_port}"
        framer = _Rfc6587Framer(max_message_size=self.max_message_size, peer=peer)
        conn.settimeout(1.0)
        try:
            while not self._stop_event.is_set():
                try:
                    chunk = conn.recv(self.buffer_size)
                except TimeoutError:
                    continue
                except Exception as exc:
                    if not self._stop_event.is_set():
                        logger.warning("Syslog TCP recv error from %s: %s", peer, exc)
                    break
                if not chunk:
                    break  # peer closed the connection
                messages = framer.feed(chunk)
                for msg in messages:
                    yield self._tcp_record(msg, source_ip, src_port)
                if framer.error is not None:
                    logger.warning("Syslog TCP framing error from %s: %s", peer, framer.error)
                    break
        finally:
            for msg in framer.finish():
                yield self._tcp_record(msg, source_ip, src_port)
            try:
                conn.close()
            except Exception:
                pass

    def _tcp_record(self, msg: bytes, source_ip: str, src_port: int) -> tuple[bytes, dict]:
        meta = _parse_syslog(msg, self.encoding)
        meta["source_ip"] = source_ip
        meta["port"] = src_port
        logger.debug(
            "Syslog TCP message received",
            extra={"source_ip": source_ip, "bytes": len(msg)},
        )
        return msg, meta
