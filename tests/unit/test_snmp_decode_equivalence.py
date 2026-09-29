"""v1.5.0 layer 4 (GH #72) — dual-stack trap decode-equivalence fixtures.

The SAME trap BER bytes decoded through both wire stacks:

* legacy pysnmp ``SNMPTrapSource._decode_trap`` (flag off)
* tsnmp ``decode_notification`` (flag on — called directly, the same decoder
  the tsnmp listener path uses; there is no connector-level offline decoder)

and the parsed records compared. Fixture bytes are produced by pysnmp's own
encoder (both stacks are installed in the dev env), so this module is the
cross-stack decode proof with no external peers:

* v1 / v2c — fully deterministic encoder-only bytes (pysnmp proto APIs).
* v3 — bytes produced by pysnmp's ``send_notification`` into a local capture
  socket (USM wrap needs pysnmp's engine machinery; the envelope varies per
  send but the varbind record is fixed, which is what the test asserts).

Findings encoded here (matching the layer-1 wire-harness evidence):

* v2c — both stacks parse identical records (the true equivalence case).
* v1 — the legacy v2c-spec decoder cannot parse a v1 Trap-PDU (tag 0xa4) and
  degrades to ``_raw``; tsnmp fully decodes varbinds + Trap-PDU metadata.
* v3 authPriv — encrypted scoped PDU: legacy always degrades to ``_raw``;
  tsnmp authenticates/decrypts (the #28-defect class: a standard pysnmp
  SHA-256/AES-128 trap must decode).
* v3 noAuthNoPriv — tsnmp fully decodes; legacy cannot produce the payload
  record (``_raw`` or a header-only partial).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import pytest

from tram.connectors.snmp.source import SNMPTrapSource

try:
    import trishul_snmp  # noqa: F401

    _TSNMP_AVAILABLE = True
except Exception:  # pragma: no cover - import probe only
    _TSNMP_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _TSNMP_AVAILABLE, reason="trishul_snmp not installed")


# ── fixture-byte builders (pysnmp's encoder) ────────────────────────────────


def _v1_trap_bytes() -> bytes:
    """Deterministic v1 Trap-PDU bytes via pysnmp's RFC 1157 proto API."""
    from pyasn1.codec.ber import encoder as ber_encoder
    from pysnmp.proto.api import v1 as v1api

    msg = v1api.Message()
    v1api.apiMessage.set_defaults(msg)
    v1api.apiMessage.set_community(msg, b"public")
    pdu = v1api.TrapPDU()
    v1api.apiTrapPDU.set_defaults(pdu)
    v1api.apiTrapPDU.set_enterprise(pdu, (1, 3, 6, 1, 4, 1, 99999))
    v1api.apiTrapPDU.set_agent_address(pdu, "127.0.0.1")
    v1api.apiTrapPDU.set_generic_trap(pdu, 6)
    v1api.apiTrapPDU.set_specific_trap(pdu, 42)
    v1api.apiTrapPDU.set_timestamp(pdu, 123456)
    v1api.apiTrapPDU.set_varbinds(pdu, [
        ((1, 3, 6, 1, 2, 1, 1, 3, 0), v1api.TimeTicks(123456)),
        ((1, 3, 6, 1, 4, 1, 99999, 1, 0), v1api.OctetString(b"v1-alarm")),
        ((1, 3, 6, 1, 4, 1, 99999, 2, 0), v1api.Integer(7)),
    ])
    v1api.apiMessage.set_pdu(msg, pdu)
    return ber_encoder.encode(msg)


def _v2c_trap_bytes() -> bytes:
    """Deterministic v2c Trap-PDU bytes via pysnmp's RFC 1901 proto API."""
    from pyasn1.codec.ber import encoder as ber_encoder
    from pysnmp.proto.api import v2c as pMod

    msg = pMod.Message()
    pMod.apiMessage.set_defaults(msg)
    pMod.apiMessage.set_community(msg, b"public")
    pdu = pMod.SNMPv2TrapPDU()
    pMod.apiPDU.set_defaults(pdu)
    pMod.apiPDU.set_varbinds(pdu, [
        ((1, 3, 6, 1, 2, 1, 1, 3, 0), pMod.TimeTicks(123456)),
        ((1, 3, 6, 1, 6, 3, 1, 1, 4, 1, 0), pMod.ObjectIdentifier((1, 3, 6, 1, 4, 1, 99999))),
        ((1, 3, 6, 1, 4, 1, 99999, 1, 0), pMod.OctetString(b"v2c-alarm")),
        ((1, 3, 6, 1, 4, 1, 99999, 2, 0), pMod.Integer(42)),
        ((1, 3, 6, 1, 4, 1, 99999, 3, 0), pMod.TimeTicks(987)),
    ])
    pMod.apiMessage.set_pdu(msg, pdu)
    return ber_encoder.encode(msg)


@dataclass(frozen=True)
class _V3Fixture:
    """A captured pysnmp v3 trap datagram plus the record it should decode to.

    The USM envelope (engine id, boots/time, salt) varies per capture — only
    the varbind record is asserted.
    """

    raw: bytes
    expected: dict[str, str]


def _capture_pysnmp_v3_trap(*, username: str, authpw: str | None = None, privpw: str | None = None) -> _V3Fixture:
    """Send one pysnmp v3 trap into a local capture socket and return it.

    ``authpw``/``privpw`` of None → noAuthNoPriv. Mirrors the wire-harness
    evidence scripts (pysnmp standard-sender traps → offline decode).
    """
    captured: list[bytes] = []

    class _Grab(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            captured.append(data)

    async def _run():
        from pysnmp.hlapi.asyncio import (
            USM_AUTH_HMAC192_SHA256,
            USM_PRIV_CFB128_AES,
            ContextData,
            NotificationType,
            ObjectIdentity,
            ObjectType,
            OctetString,
            SnmpEngine,
            UdpTransportTarget,
            UsmUserData,
            send_notification,
        )
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(_Grab, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        try:
            if authpw is None:
                user = UsmUserData(username)
            else:
                user = UsmUserData(
                    username, authpw, privpw,
                    authProtocol=USM_AUTH_HMAC192_SHA256, privProtocol=USM_PRIV_CFB128_AES,
                )
            await send_notification(
                SnmpEngine(),
                user,
                await UdpTransportTarget.create(("127.0.0.1", port), timeout=2, retries=0),
                ContextData(),
                "trap",
                NotificationType(ObjectIdentity("SNMPv2-MIB", "warmStart")).add_varbinds(
                    ObjectType(ObjectIdentity("1.3.6.1.4.1.99999.1.0"), OctetString("wire-alarm"))
                ),
            )
            await asyncio.sleep(0.3)
        finally:
            transport.close()

    asyncio.run(_run())
    assert captured, "no pysnmp v3 trap datagram captured"
    return _V3Fixture(
        raw=captured[0],
        expected={
            "1.3.6.1.2.1.1.3.0": "0",
            "1.3.6.1.6.3.1.1.4.1.0": "1.3.6.1.6.3.1.1.5.2",  # warmStart
            "1.3.6.1.4.1.99999.1.0": "wire-alarm",
        },
    )


# ── stack-side decoders ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_snmp_stack_env():
    """Save/restore TRAM_SNMP_STACK around every test (NIT 11).

    The legacy source construction reads the flag; nothing here should leak a
    trishul value into the rest of the suite.
    """
    saved = os.environ.get("TRAM_SNMP_STACK")
    os.environ.pop("TRAM_SNMP_STACK", None)
    yield
    if saved is None:
        os.environ.pop("TRAM_SNMP_STACK", None)
    else:
        os.environ["TRAM_SNMP_STACK"] = saved


def _decode_legacy(raw: bytes, version: str = "2c", **_ignored) -> dict:
    """Decode with the flag-off pysnmp path.

    Extra kwargs (e.g. v3 USM fields) are accepted for call symmetry but
    ignored — the legacy decoder is version/community only and never touches
    USM credentials.
    """
    source = SNMPTrapSource({"port": 162, "version": version})
    return source._decode_trap(raw)


def _decode_tsnmp(raw: bytes, version: str = "2c", **usm) -> dict:
    """Decode with tsnmp's ``decode_notification`` directly (C6).

    The flag-on trap stream decodes inside the tsnmp listeners (there is no
    connector-level offline decoder), so the equivalence proof calls the
    same decoder the listener path uses. Values render with the trap-path
    parity formatter (``_tsnmp_val_to_legacy_str``) so the records are
    comparable to the legacy ``str(val)`` output.
    """
    from trishul_snmp import decode_notification

    from tram.connectors.snmp.source import SNMPTrapSource

    user = None
    if version == "3":
        from tram.connectors.snmp.mib_utils import build_v3_usm_user

        user = build_v3_usm_user(
            security_name=usm["security_name"],
            auth_protocol=usm.get("auth_protocol", "SHA"),
            auth_key=usm.get("auth_key"),
            priv_protocol=usm.get("priv_protocol", "AES128"),
            priv_key=usm.get("priv_key"),
        )
    event = decode_notification(raw, user=user)
    return {
        vb.oid_str: SNMPTrapSource._tsnmp_val_to_legacy_str(vb.value)
        for vb in event.varbinds
    }


def _v3_usm_config() -> dict:
    return {
        "security_name": "trapuser",
        "auth_protocol": "SHA256",
        "auth_key": "authpass",
        "priv_protocol": "AES128",
        "priv_key": "privpass",
    }


class TestV2cDecodeEquivalence:
    def test_identical_records_both_stacks(self):
        """The same v2c trap bytes parse to the identical record on both stacks."""
        raw = _v2c_trap_bytes()
        legacy = _decode_legacy(raw, version="2c")
        tsnmp = _decode_tsnmp(raw, version="2c")
        assert legacy == tsnmp
        assert legacy == {
            "1.3.6.1.2.1.1.3.0": "123456",
            "1.3.6.1.6.3.1.1.4.1.0": "1.3.6.1.4.1.99999",
            "1.3.6.1.4.1.99999.1.0": "v2c-alarm",
            "1.3.6.1.4.1.99999.2.0": "42",
            "1.3.6.1.4.1.99999.3.0": "987",
        }
        assert "_raw" not in legacy


class TestV1DecodeEquivalence:
    def test_legacy_raw_tsnmp_full_record(self):
        """v1 Trap-PDUs: legacy (v2c-spec decoder) degrades to _raw; tsnmp
        fully decodes the varbind record."""
        raw = _v1_trap_bytes()
        legacy = _decode_legacy(raw, version="1")
        tsnmp = _decode_tsnmp(raw, version="1")

        assert legacy == {"_raw": raw.hex()}
        assert tsnmp == {
            "1.3.6.1.2.1.1.3.0": "123456",
            "1.3.6.1.4.1.99999.1.0": "v1-alarm",
            "1.3.6.1.4.1.99999.2.0": "7",
        }

    def test_tsnmp_v1_trap_pdu_metadata(self):
        """decode_notification surfaces the v1 Trap-PDU metadata (enterprise,
        agent-addr, generic/specific, timestamp, community) — the record shape
        read() would carry for a v1 trap."""
        raw = _v1_trap_bytes()
        event = trishul_snmp.decode_notification(raw)
        d = event.to_dict()
        assert d["community"] == "public"
        assert d["pdu_type"] == "trap"
        assert d["enterprise"] == "1.3.6.1.4.1.99999"
        assert d["agent_addr"] == "127.0.0.1"
        assert d["generic_trap"] == 6
        assert d["specific_trap"] == 42
        assert d["timestamp"] == 123456
        oids = {vb["oid"] for vb in d["varbinds"]}
        assert "1.3.6.1.4.1.99999.1.0" in oids


class TestV3DecodeEquivalence:
    def test_authpriv_legacy_raw_tsnmp_full(self):
        """Standard pysnmp SHA-256/AES-128 trap: legacy sees only _raw (the
        scoped PDU is encrypted); tsnmp authenticates + decrypts the full
        record — the #28-defect class caught in CI."""
        fixture = _capture_pysnmp_v3_trap(
            username="trapuser", authpw="authpass", privpw="privpass"
        )
        legacy = _decode_legacy(fixture.raw, version="3", **_v3_usm_config())
        tsnmp = _decode_tsnmp(fixture.raw, version="3", **_v3_usm_config())

        assert legacy == {"_raw": fixture.raw.hex()}
        assert tsnmp == fixture.expected

    def test_noauth_tsnmp_full_legacy_cannot(self):
        """noAuthNoPriv v3: tsnmp decodes the record; legacy cannot produce
        the payload varbind (either _raw or a header-only partial)."""
        fixture = _capture_pysnmp_v3_trap(username="trapuser")
        tsnmp = _decode_tsnmp(
            fixture.raw, version="3", security_name="trapuser"
        )
        assert tsnmp == fixture.expected

        legacy = _decode_legacy(fixture.raw, version="3", security_name="trapuser")
        assert "1.3.6.1.4.1.99999.1.0" not in legacy


class TestDecodeGarbageEquivalence:
    def test_legacy_raw_fallback(self):
        """Legacy decoder falls back to _raw hex on undecodable bytes."""
        garbage = b"\x00\x01\x02garbage"
        assert _decode_legacy(garbage) == {"_raw": garbage.hex()}

    def test_tsnmp_decode_notification_rejects_garbage(self):
        """decode_notification raises on undecodable bytes — the listener path
        treats a decode failure as a drop (there is no connector-level _raw
        fallback on the flag-on stack)."""
        from trishul_snmp import decode_notification

        with pytest.raises(Exception):
            decode_notification(b"\x00\x01\x02garbage")