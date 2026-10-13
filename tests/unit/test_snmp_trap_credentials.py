"""Trap credential rejection and recovery over isolated loopback UDP."""

import json
import socket
import threading

import pytest
from pyasn1.codec.ber import encoder
from pysnmp.proto.api import v2c

from tests.unit.test_snmp_decode_equivalence import (
    _capture_pysnmp_v3_trap,
    _v3_usm_config,
)
from tram.connectors.snmp.source import SNMPTrapSource
from tram.core.exceptions import SourceError


def _counter_trap(community: bytes = b"public") -> bytes:
    msg = v2c.Message()
    v2c.apiMessage.set_defaults(msg)
    v2c.apiMessage.set_community(msg, community)
    pdu = v2c.SNMPv2TrapPDU()
    v2c.apiPDU.set_defaults(pdu)
    v2c.apiPDU.set_varbinds(pdu, [
        ((1, 3, 6, 1, 4, 1, 99999, 9, 0), v2c.Counter64(2**64 - 1)),
    ])
    v2c.apiMessage.set_pdu(msg, pdu)
    return encoder.encode(msg)


def _receive_after_rejections(source: SNMPTrapSource, rejected: list[bytes], good: bytes):
    stream = source.read()
    # A failed assertion must never leave next(stream) blocked indefinitely.
    watchdog = threading.Timer(5, source.stop)
    watchdog.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for raw in [*rejected, good]:
                sender.sendto(raw, ("127.0.0.1", source.port))
        payload, meta = next(stream)
        return json.loads(payload), meta
    finally:
        source.stop()
        stream.close()
        watchdog.cancel()


@pytest.mark.parametrize("stack", ["trishul", "legacy"])
@pytest.mark.parametrize("attempt", range(3))
def test_community_and_corruption_rejected_then_counter64_received(stack, attempt, monkeypatch, caplog):
    monkeypatch.setenv("TRAM_SNMP_STACK", stack)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    source = SNMPTrapSource({"host": "127.0.0.1", "port": port, "resolve_oids": False})
    good = _counter_trap()
    if stack == "legacy":
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch

        ready = threading.Event()
        real_socket = socket.socket

        class BoundSocket:
            def __init__(self, *args, **kwargs):
                self.sock = real_socket(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self.sock, name)

            def bind(self, address):
                self.sock.bind(address)
                ready.set()

        stream = source.read()
        watchdog = threading.Timer(5, source.stop)
        watchdog.start()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                with patch("socket.socket", BoundSocket):
                    received = pool.submit(next, stream)
                    assert ready.wait(2), "legacy receiver did not bind"
                with real_socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                    for raw in [_counter_trap(b"secret-community"),
                                b"secret-corrupt-packet", good + b"tail", good]:
                        sender.sendto(raw, ("127.0.0.1", port))
                payload, _ = received.result(timeout=6)
                data = json.loads(payload)
        finally:
            source.stop()
            stream.close()
            watchdog.cancel()
    else:
        data, _ = _receive_after_rejections(
            source, [_counter_trap(b"secret-community"), b"secret-corrupt-packet"], good
        )
    assert data == {"1.3.6.1.4.1.99999.9.0": str(2**64 - 1)}
    assert "wrong community" in caplog.text or "wrong-community" in caplog.text
    assert "secret-community" not in caplog.text
    assert "secret-corrupt-packet" not in caplog.text
    assert "_raw" not in data


@pytest.mark.parametrize("attempt", range(3))
@pytest.mark.parametrize("wrong_key", ["privacy", "auth"])
def test_v3_wrong_key_rejected_then_valid_received(attempt, wrong_key, monkeypatch, caplog):
    monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
    bad = _capture_pysnmp_v3_trap(
        username="trapuser", authpw="wrongauth" if wrong_key == "auth" else "authpass",
        privpw="wrongpriv" if wrong_key == "privacy" else "privpass",
    )
    good = _capture_pysnmp_v3_trap(username="trapuser", authpw="authpass", privpw="privpass")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    source = SNMPTrapSource({
        "host": "127.0.0.1", "port": port, "version": "3", "resolve_oids": False,
        **_v3_usm_config(),
    })
    data, _ = _receive_after_rejections(source, [bad.raw, b"secret-corrupt-packet"], good.raw)
    assert data == good.expected
    assert "Dropping SNMP notification" in caplog.text
    assert "authpass" not in caplog.text
    assert "wrongpriv" not in caplog.text
    assert "wrongauth" not in caplog.text
    assert "privpass" not in caplog.text


def test_legacy_v3_fails_before_binding(monkeypatch):
    monkeypatch.setenv("TRAM_SNMP_STACK", "legacy")
    source = SNMPTrapSource({"version": "3"})
    with pytest.raises(SourceError, match="requires TRAM_SNMP_STACK=trishul"):
        source.read()
