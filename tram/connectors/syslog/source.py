"""Syslog source connector — receives syslog messages over UDP or TCP."""

from __future__ import annotations

import logging
import re
import socket
import threading
from typing import Iterator

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

    Parses RFC 3164 and RFC 5424 messages. Yields ``(raw_bytes, meta)`` where
    meta includes: ``source_ip``, ``port``, ``facility``, ``severity``,
    ``hostname``, ``appname``, ``timestamp``, ``message``.

    Uses the ``socket`` stdlib module — no extra dependencies required.

    Config keys:
        host        (str, default "0.0.0.0")   Bind address.
        port        (int, default 514)          Bind port.
        protocol    (str, default "udp")        "udp" or "tcp".
        buffer_size (int, default 65535)        UDP datagram / TCP read size.
        encoding    (str, default "utf-8")      Message decoding charset.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config.get("host", "0.0.0.0")
        self.port: int = int(config.get("port", 514))
        self.protocol: str = config.get("protocol", "udp").lower()
        self.buffer_size: int = int(config.get("buffer_size", 65535))
        self.encoding: str = config.get("encoding", "utf-8")
        self._stop_event: threading.Event = threading.Event()

    def stop(self) -> None:
        """Signal the stream to stop."""
        self._stop_event.set()

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
                except socket.timeout:
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
        logger.info(
            "Syslog TCP source listening",
            extra={"host": self.host, "port": self.port},
        )
        try:
            while not self._stop_event.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except Exception as exc:
                    logger.warning("Syslog TCP accept error: %s", exc)
                    continue
                source_ip, src_port = addr
                try:
                    raw = conn.recv(self.buffer_size)
                    conn.close()
                except Exception as exc:
                    logger.warning("Syslog TCP recv error from %s: %s", source_ip, exc)
                    continue
                if not raw:
                    continue
                meta = _parse_syslog(raw, self.encoding)
                meta["source_ip"] = source_ip
                meta["port"] = src_port
                logger.debug(
                    "Syslog TCP message received",
                    extra={"source_ip": source_ip, "bytes": len(raw)},
                )
                yield raw, meta
        finally:
            try:
                srv.close()
            except Exception:
                pass
