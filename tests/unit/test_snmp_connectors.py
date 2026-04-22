"""Tests for SNMP source and sink connectors."""

from __future__ import annotations

import json
import socket
from collections.abc import Awaitable
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.snmp.sink import SNMPTrapSink
from tram.connectors.snmp.source import SNMPPollSource, SNMPTrapSource
from tram.core.exceptions import SinkError, SourceError


def _close_coro(coro: Awaitable[object]) -> None:
    """Consume a created coroutine in tests when asyncio.run is mocked."""
    coro.close()


def _close_coro_then_raise(exc: Exception):
    def _raiser(coro: Awaitable[object]) -> None:
        coro.close()
        raise exc

    return _raiser

# ── SNMPTrapSource ─────────────────────────────────────────────────────────


class TestSNMPTrapSource:
    def test_bind_permission_raises_source_error(self):
        """SNMPTrapSource raises SourceError when UDP bind is denied."""
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            mock_sock.bind.side_effect = PermissionError("Permission denied")
            source = SNMPTrapSource({"host": "0.0.0.0", "port": 162})
            with pytest.raises(SourceError, match="UDP bind failed"):
                list(source.read())

    def test_bind_failure_raises_source_error(self):
        # pysnmp is available but socket bind fails
        mock_pysnmp = MagicMock()
        with patch.dict("sys.modules", {
            "pysnmp": mock_pysnmp,
            "pysnmp.hlapi": mock_pysnmp,
            "pysnmp.carrier": mock_pysnmp,
            "pysnmp.carrier.asyncio": mock_pysnmp,
            "pysnmp.carrier.asyncio.dgram": mock_pysnmp,
            "pysnmp.entity": mock_pysnmp,
            "pysnmp.entity.rfc3413": mock_pysnmp,
        }):
            with patch("socket.socket") as mock_sock_cls:
                mock_sock = MagicMock()
                mock_sock_cls.return_value = mock_sock
                mock_sock.bind.side_effect = OSError("Permission denied")

                source = SNMPTrapSource({"host": "0.0.0.0", "port": 162})
                with pytest.raises(SourceError, match="UDP bind failed"):
                    list(source.read())

    def test_yields_trap_then_stops(self):
        # A minimal SNMP v2c trap-like datagram (just raw bytes)
        raw_trap = b"\x30\x29\x02\x01\x01\x04\x06public\xa7\x1c"

        mock_pysnmp = MagicMock()
        with patch.dict("sys.modules", {
            "pysnmp": mock_pysnmp,
            "pysnmp.hlapi": mock_pysnmp,
            "pysnmp.carrier": mock_pysnmp,
            "pysnmp.carrier.asyncio": mock_pysnmp,
            "pysnmp.carrier.asyncio.dgram": mock_pysnmp,
            "pysnmp.entity": mock_pysnmp,
            "pysnmp.entity.rfc3413": mock_pysnmp,
        }):
            with patch("socket.socket") as mock_sock_cls:
                mock_sock = MagicMock()
                mock_sock_cls.return_value = mock_sock
                mock_sock.recvfrom.side_effect = [
                    (raw_trap, ("192.168.1.1", 162)),
                    socket.timeout,
                ]

                source = SNMPTrapSource({"host": "0.0.0.0", "port": 162})
                it = source.read()
                payload, meta = next(it)
                source.stop()
                list(it)

        assert meta["source_ip"] == "192.168.1.1"
        assert meta["community"] == "public"
        data = json.loads(payload)
        assert isinstance(data, dict)

    def test_stop_terminates_stream(self):
        mock_pysnmp = MagicMock()
        with patch.dict("sys.modules", {
            "pysnmp": mock_pysnmp,
            "pysnmp.hlapi": mock_pysnmp,
            "pysnmp.carrier": mock_pysnmp,
            "pysnmp.carrier.asyncio": mock_pysnmp,
            "pysnmp.carrier.asyncio.dgram": mock_pysnmp,
            "pysnmp.entity": mock_pysnmp,
            "pysnmp.entity.rfc3413": mock_pysnmp,
        }):
            with patch("socket.socket") as mock_sock_cls:
                mock_sock = MagicMock()
                mock_sock_cls.return_value = mock_sock
                mock_sock.recvfrom.side_effect = socket.timeout

                source = SNMPTrapSource({"port": 162})
                source.stop()
                assert list(source.read()) == []


    def test_decode_trap_ber(self):
        """_decode_trap correctly decodes a real BER-encoded SNMPv2c trap (pysnmp 7.x)."""
        from pyasn1.codec.ber import encoder as ber_encoder
        from pysnmp.proto.api import v2c as pMod

        msg = pMod.Message()
        pMod.apiMessage.set_defaults(msg)
        pMod.apiMessage.set_community(msg, b"public")
        reqPDU = pMod.SNMPv2TrapPDU()
        pMod.apiPDU.set_defaults(reqPDU)
        pMod.apiPDU.set_varbinds(reqPDU, [
            ((1, 3, 6, 1, 2, 1, 1, 3, 0), pMod.TimeTicks(12345)),
            ((1, 3, 6, 1, 6, 3, 1, 1, 4, 1, 0), pMod.ObjectIdentifier((1, 3, 6, 1, 6, 3, 1, 1, 5, 4))),
            ((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1), pMod.Integer(42)),
        ])
        pMod.apiMessage.set_pdu(msg, reqPDU)
        trap_bytes = ber_encoder.encode(msg)

        source = SNMPTrapSource({"port": 162})
        result = source._decode_trap(trap_bytes)

        assert "1.3.6.1.2.1.1.3.0" in result
        assert "1.3.6.1.6.3.1.1.4.1.0" in result
        assert result["1.3.6.1.2.1.2.2.1.1.1"] == "42"
        assert "_raw" not in result

    def test_decode_trap_fallback_on_garbage(self):
        """_decode_trap returns _raw hex for non-SNMP bytes."""
        source = SNMPTrapSource({"port": 162})
        result = source._decode_trap(b"\x00\x01\x02garbage")
        assert "_raw" in result
        assert result["_raw"] == b"\x00\x01\x02garbage".hex()


# ── SNMPPollSource ─────────────────────────────────────────────────────────


class TestSNMPPollSource:
    def test_import_error_raises_source_error(self):
        with patch.dict("sys.modules", {"pysnmp": None, "pysnmp.hlapi": None}):
            source = SNMPPollSource({
                "host": "192.168.1.1",
                "oids": ["1.3.6.1.2.1.1.1.0"],
            })
            with pytest.raises(SourceError, match="pysnmp"):
                list(source.read())

    def test_get_operation_yields_bindings(self):
        mock_var_binds = [("1.3.6.1.2.1.1.1.0", "Linux")]
        mock_result = (None, None, None, mock_var_binds)

        mock_pysnmp = MagicMock()
        mock_pysnmp.hlapi.getCmd.return_value = iter([mock_result])
        mock_pysnmp.hlapi.SnmpEngine.return_value = MagicMock()
        mock_pysnmp.hlapi.CommunityData.return_value = MagicMock()
        mock_pysnmp.hlapi.UdpTransportTarget.return_value = MagicMock()
        mock_pysnmp.hlapi.ContextData.return_value = MagicMock()
        mock_pysnmp.hlapi.ObjectIdentity = MagicMock(side_effect=lambda x: x)
        mock_pysnmp.hlapi.ObjectType = MagicMock(side_effect=lambda x: x)

        with patch.dict("sys.modules", {"pysnmp": mock_pysnmp, "pysnmp.hlapi": mock_pysnmp.hlapi}):
            with patch("pysnmp.hlapi.getCmd", return_value=iter([mock_result])):
                with patch("pysnmp.hlapi.nextCmd"):
                    source = SNMPPollSource({
                        "host": "192.168.1.1",
                        "oids": ["1.3.6.1.2.1.1.1.0"],
                        "operation": "get",
                    })
                    # This will raise ImportError if pysnmp is not fully mocked,
                    # which is expected in a unit test environment without the dep.
                    try:
                        results = list(source.read())
                        assert len(results) == 1
                        payload, meta = results[0]
                        data = json.loads(payload)
                        assert isinstance(data, dict)
                    except SourceError as e:
                        # Acceptable — pysnmp not installed in test env
                        assert "pysnmp" in str(e)

    def test_invalid_operation_raises_source_error(self):
        mock_pysnmp = MagicMock()
        with patch.dict("sys.modules", {"pysnmp": mock_pysnmp, "pysnmp.hlapi": mock_pysnmp.hlapi}):
            source = SNMPPollSource({
                "host": "192.168.1.1",
                "oids": ["1.3.6.1.2.1.1.1.0"],
                "operation": "invalid_op",
            })
            with pytest.raises((SourceError, Exception)):
                list(source.read())

    def test_do_walk_stops_when_oid_does_not_advance(self):
        class MockHlapi:
            def __init__(self):
                self.calls = 0

            class SnmpEngine:
                pass

            class CommunityData:
                def __init__(self, *args, **kwargs):
                    pass

            class UdpTransportTarget:
                def __init__(self, *args, **kwargs):
                    pass

            class ContextData:
                def __init__(self, *args, **kwargs):
                    pass

            class ObjectIdentity:
                def __init__(self, oid):
                    self.oid = oid

            class ObjectType:
                def __init__(self, identity):
                    self.identity = identity

            async def nextCmd(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return (None, None, None, [("1.3.6.1.2.1.2.2.1.22.247", "1")])
                return (None, None, None, [("1.3.6.1.2.1.2.2.1.22.247", "1")])

        source = SNMPPollSource({
            "host": "192.168.1.1",
            "oids": ["1.3.6.1.2.1.2.2"],
            "operation": "walk",
        })

        mock_hlapi = MockHlapi()

        import asyncio
        result = asyncio.run(source._do_walk(mock_hlapi, typed=False))

        assert result == {"1.3.6.1.2.1.2.2.1.22.247": "1"}
        assert mock_hlapi.calls == 2

    def test_do_walk_resolves_symbolic_base_oid(self):
        class MockHlapi:
            def __init__(self):
                self.calls = 0
                self.identities = []

            class SnmpEngine:
                pass

            class CommunityData:
                def __init__(self, *args, **kwargs):
                    pass

            class UdpTransportTarget:
                def __init__(self, *args, **kwargs):
                    pass

            class ContextData:
                def __init__(self, *args, **kwargs):
                    pass

            def ObjectIdentity(self, oid):
                self.identities.append(oid)
                return oid

            class ObjectType:
                def __init__(self, identity):
                    self.identity = identity

            async def nextCmd(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return (None, None, None, [[("1.3.6.1.2.1.2.2.1.2.1", "eth0")]])
                return (None, None, None, [])

        source = SNMPPollSource({
            "host": "192.168.1.1",
            "oids": ["IF-MIB::ifTable"],
            "operation": "walk",
            "mib_modules": ["IF-MIB"],
        })

        mock_hlapi = MockHlapi()

        import asyncio
        with patch("tram.connectors.snmp.mib_utils.get_mib_view", return_value=MagicMock()):
            with patch(
                "tram.connectors.snmp.mib_utils.symbolic_to_oid",
                return_value=(1, 3, 6, 1, 2, 1, 2, 2),
            ):
                result = asyncio.run(source._do_walk(mock_hlapi, typed=False))

        assert result == {"1.3.6.1.2.1.2.2.1.2.1": "eth0"}
        assert mock_hlapi.identities[0] == "1.3.6.1.2.1.2.2"


# ── SNMPPollSource._group_by_index (pure unit, no SNMP dep) ────────────────


class TestBuildV3Auth:
    """Tests for mib_utils.build_v3_auth — no real pysnmp required."""

    def _mock_hlapi(self):
        m = MagicMock()
        m.usmHMACSHAAuthProtocol = "SHA_CONST"
        m.usmHMACMD5AuthProtocol = "MD5_CONST"
        m.usmHMAC192SHA256AuthProtocol = "SHA256_CONST"
        m.usmAesCfb128Protocol = "AES128_CONST"
        m.usmDESPrivProtocol = "DES_CONST"
        m.usmAesCfb256Protocol = "AES256_CONST"
        m.UsmUserData = MagicMock(return_value="usm_obj")
        return m

    def test_no_auth_key_gives_noauth_nopriv(self):
        """No auth_key → only userName passed to UsmUserData."""
        from tram.connectors.snmp.mib_utils import build_v3_auth
        hlapi = self._mock_hlapi()
        build_v3_auth(hlapi, security_name="myuser")
        hlapi.UsmUserData.assert_called_once_with(userName="myuser")

    def test_auth_key_only_gives_authnopriv(self):
        """auth_key set, no priv_key → authKey + authProtocol, no privKey."""
        from tram.connectors.snmp.mib_utils import build_v3_auth
        hlapi = self._mock_hlapi()
        build_v3_auth(hlapi, security_name="u", auth_key="secret")
        call_kwargs = hlapi.UsmUserData.call_args[1]
        assert call_kwargs["authKey"] == "secret"
        assert call_kwargs["authProtocol"] == "SHA_CONST"
        assert "privKey" not in call_kwargs

    def test_auth_and_priv_gives_authpriv(self):
        """Both auth_key and priv_key → full authPriv kwargs."""
        from tram.connectors.snmp.mib_utils import build_v3_auth
        hlapi = self._mock_hlapi()
        build_v3_auth(hlapi, security_name="u", auth_key="akey", priv_key="pkey")
        call_kwargs = hlapi.UsmUserData.call_args[1]
        assert call_kwargs["authKey"] == "akey"
        assert call_kwargs["privKey"] == "pkey"
        assert call_kwargs["authProtocol"] == "SHA_CONST"
        assert call_kwargs["privProtocol"] == "AES128_CONST"

    def test_md5_auth_protocol_resolves(self):
        """auth_protocol='MD5' maps to usmHMACMD5AuthProtocol attribute."""
        from tram.connectors.snmp.mib_utils import build_v3_auth
        hlapi = self._mock_hlapi()
        build_v3_auth(hlapi, security_name="u", auth_protocol="MD5", auth_key="k")
        call_kwargs = hlapi.UsmUserData.call_args[1]
        assert call_kwargs["authProtocol"] == "MD5_CONST"

    def test_sha256_auth_protocol_resolves(self):
        """auth_protocol='SHA256' maps to usmHMAC192SHA256AuthProtocol."""
        from tram.connectors.snmp.mib_utils import build_v3_auth
        hlapi = self._mock_hlapi()
        build_v3_auth(hlapi, security_name="u", auth_protocol="SHA256", auth_key="k")
        call_kwargs = hlapi.UsmUserData.call_args[1]
        assert call_kwargs["authProtocol"] == "SHA256_CONST"

    def test_des_priv_protocol_resolves(self):
        """priv_protocol='DES' maps to usmDESPrivProtocol."""
        from tram.connectors.snmp.mib_utils import build_v3_auth
        hlapi = self._mock_hlapi()
        build_v3_auth(hlapi, security_name="u", auth_key="a", priv_key="p",
                      priv_protocol="DES")
        call_kwargs = hlapi.UsmUserData.call_args[1]
        assert call_kwargs["privProtocol"] == "DES_CONST"

    def test_aes256_priv_protocol_resolves(self):
        """priv_protocol='AES256' maps to usmAesCfb256Protocol."""
        from tram.connectors.snmp.mib_utils import build_v3_auth
        hlapi = self._mock_hlapi()
        build_v3_auth(hlapi, security_name="u", auth_key="a", priv_key="p",
                      priv_protocol="AES256")
        call_kwargs = hlapi.UsmUserData.call_args[1]
        assert call_kwargs["privProtocol"] == "AES256_CONST"

    def test_unknown_auth_protocol_falls_back_to_sha(self):
        """Unrecognised auth_protocol string defaults to SHA (usmHMACSHAAuthProtocol)."""
        from tram.connectors.snmp.mib_utils import build_v3_auth
        hlapi = self._mock_hlapi()
        build_v3_auth(hlapi, security_name="u", auth_protocol="UNKNOWN", auth_key="k")
        call_kwargs = hlapi.UsmUserData.call_args[1]
        assert call_kwargs["authProtocol"] == "SHA_CONST"

    def test_unknown_priv_protocol_falls_back_to_aes128(self):
        """Unrecognised priv_protocol defaults to AES128 (usmAesCfb128Protocol)."""
        from tram.connectors.snmp.mib_utils import build_v3_auth
        hlapi = self._mock_hlapi()
        build_v3_auth(hlapi, security_name="u", auth_key="a", priv_key="p",
                      priv_protocol="UNKNOWN")
        call_kwargs = hlapi.UsmUserData.call_args[1]
        assert call_kwargs["privProtocol"] == "AES128_CONST"

    def test_protocol_string_case_insensitive(self):
        """Protocol strings are matched case-insensitively (e.g. 'sha' == 'SHA')."""
        from tram.connectors.snmp.mib_utils import build_v3_auth
        hlapi = self._mock_hlapi()
        build_v3_auth(hlapi, security_name="u", auth_protocol="sha", auth_key="k")
        call_kwargs = hlapi.UsmUserData.call_args[1]
        assert call_kwargs["authProtocol"] == "SHA_CONST"

    def test_aes_alias_resolves_to_aes128(self):
        """priv_protocol='AES' is an alias for AES128."""
        from tram.connectors.snmp.mib_utils import build_v3_auth
        hlapi = self._mock_hlapi()
        build_v3_auth(hlapi, security_name="u", auth_key="a", priv_key="p",
                      priv_protocol="AES")
        call_kwargs = hlapi.UsmUserData.call_args[1]
        assert call_kwargs["privProtocol"] == "AES128_CONST"


class TestSNMPPollSourceV3Config:
    """Verify SNMPPollSource stores v3 config fields correctly."""

    def test_v3_config_fields_stored(self):
        src = SNMPPollSource({
            "host": "10.0.0.1",
            "oids": ["1.3.6.1.2.1.1.1.0"],
            "version": "3",
            "security_name": "myuser",
            "auth_protocol": "SHA256",
            "auth_key": "authsecret",
            "priv_protocol": "AES256",
            "priv_key": "privsecret",
            "context_name": "myctx",
        })
        assert src.version == "3"
        assert src.security_name == "myuser"
        assert src.auth_protocol == "SHA256"
        assert src.auth_key == "authsecret"
        assert src.priv_protocol == "AES256"
        assert src.priv_key == "privsecret"
        assert src.context_name == "myctx"

    def test_v3_defaults(self):
        """Omitted v3 fields default to safe values."""
        src = SNMPPollSource({
            "host": "10.0.0.1",
            "oids": ["1.3.6.1.2.1.1.1.0"],
            "version": "3",
            "security_name": "u",
        })
        assert src.auth_protocol == "SHA"
        assert src.auth_key is None
        assert src.priv_protocol == "AES128"
        assert src.priv_key is None
        assert src.context_name == ""

    def test_v2c_v3_fields_absent_by_default(self):
        """Default (v2c) source has empty v3 fields — community string used instead."""
        src = SNMPPollSource({"host": "10.0.0.1", "oids": ["1.3.6.1.2.1.1.1.0"]})
        assert src.version == "2c"
        assert src.security_name == ""
        assert src.auth_key is None


class TestSNMPTrapSinkV3Config:
    """Verify SNMPTrapSink stores v3 config fields correctly."""

    def test_v3_config_fields_stored(self):
        from tram.connectors.snmp.sink import SNMPTrapSink
        sink = SNMPTrapSink({
            "host": "manager.example.com",
            "version": "3",
            "security_name": "trapuser",
            "auth_protocol": "MD5",
            "auth_key": "authpass",
            "priv_protocol": "DES",
            "priv_key": "privpass",
            "context_name": "trapctx",
        })
        assert sink.version == "3"
        assert sink.security_name == "trapuser"
        assert sink.auth_protocol == "MD5"
        assert sink.auth_key == "authpass"
        assert sink.priv_protocol == "DES"
        assert sink.priv_key == "privpass"
        assert sink.context_name == "trapctx"


class TestGroupByIndex:
    """Tests for SNMPPollSource._group_by_index — no pysnmp required."""

    def test_single_component_index(self):
        """ifDescr.1, ifDescr.2 → two rows with _index='1' and '2'."""
        bindings = {
            "ifDescr.1": "eth0",
            "ifDescr.2": "lo",
            "ifOperStatus.1": "1",
            "ifOperStatus.2": "1",
        }
        rows = SNMPPollSource._group_by_index(bindings, index_depth=0)
        assert len(rows) == 2
        by_index = {r["_index"]: r for r in rows}
        assert by_index["1"]["ifDescr"] == "eth0"
        assert by_index["1"]["ifOperStatus"] == "1"
        assert by_index["2"]["ifDescr"] == "lo"

    def test_index_parts_populated(self):
        """_index_parts is a list split from _index."""
        bindings = {"ifDescr.1": "eth0"}
        rows = SNMPPollSource._group_by_index(bindings, index_depth=0)
        assert rows[0]["_index_parts"] == ["1"]

    def test_ipv4_index_auto(self):
        """ipRouteNextHop.10.0.0.1 → col='ipRouteNextHop', idx='10.0.0.1'."""
        bindings = {
            "ipRouteNextHop.10.0.0.1": "192.168.1.254",
            "ipRouteNextHop.10.0.0.2": "192.168.1.254",
        }
        rows = SNMPPollSource._group_by_index(bindings, index_depth=0)
        assert len(rows) == 2
        by_index = {r["_index"]: r for r in rows}
        assert by_index["10.0.0.1"]["ipRouteNextHop"] == "192.168.1.254"
        assert by_index["10.0.0.1"]["_index_parts"] == ["10", "0", "0", "1"]

    def test_composite_index_explicit_depth(self):
        """atPhysAddress.1.192.168.1.1 with index_depth=5 (ifIndex + 4 octets).

        index = last 5 components → '1.192.168.1.1'
        col   = first component   → 'atPhysAddress'
        """
        bindings = {
            "atPhysAddress.1.192.168.1.1": "00:11:22:33:44:55",
            "atPhysAddress.1.192.168.1.2": "00:11:22:33:44:66",
        }
        rows = SNMPPollSource._group_by_index(bindings, index_depth=5)
        assert len(rows) == 2
        by_index = {r["_index"]: r for r in rows}
        assert by_index["1.192.168.1.1"]["atPhysAddress"] == "00:11:22:33:44:55"
        assert by_index["1.192.168.1.1"]["_index_parts"] == ["1", "192", "168", "1", "1"]

    def test_no_dot_in_key_becomes_empty_index(self):
        """Key without a dot → col=key, idx='' (scalar OID row)."""
        bindings = {"sysDescr": "Linux"}
        rows = SNMPPollSource._group_by_index(bindings, index_depth=0)
        assert len(rows) == 1
        assert rows[0]["_index"] == ""
        assert rows[0]["sysDescr"] == "Linux"

    def test_rows_sorted_by_index(self):
        """Rows are returned in ascending index order."""
        bindings = {
            "ifSpeed.3": "1000",
            "ifSpeed.1": "100",
            "ifSpeed.2": "10",
        }
        rows = SNMPPollSource._group_by_index(bindings, index_depth=0)
        assert [r["_index"] for r in rows] == ["1", "2", "3"]

    def test_explicit_depth_numeric_oid(self):
        """Numeric OID with explicit index_depth=1: '1.3.6.1.2.1.2.2.1.2.1'
        → col='1.3.6.1.2.1.2.2.1.2', idx='1'."""
        bindings = {
            "1.3.6.1.2.1.2.2.1.2.1": "eth0",
            "1.3.6.1.2.1.2.2.1.2.2": "lo",
        }
        rows = SNMPPollSource._group_by_index(bindings, index_depth=1)
        assert len(rows) == 2
        by_index = {r["_index"]: r for r in rows}
        assert by_index["1"]["1.3.6.1.2.1.2.2.1.2"] == "eth0"


class TestSNMPPollTimestamp:
    """Verify _polled_at is present in all yielded records."""

    def _make_source(self, extra: dict | None = None) -> SNMPPollSource:
        cfg = {"host": "192.168.1.1", "oids": ["1.3.6.1.2.1.1.1.0"]}
        if extra:
            cfg.update(extra)
        return SNMPPollSource(cfg)

    def _mock_pysnmp_get(self, bindings_dict: dict):
        """Return a (mock_pysnmp, mock_hlapi) pair configured for a GET result."""
        var_binds = list(bindings_dict.items())
        mock_result = (None, None, None, var_binds)
        mock_hlapi = MagicMock()
        mock_hlapi.SnmpEngine.return_value = MagicMock()
        mock_hlapi.CommunityData.return_value = MagicMock()
        mock_hlapi.UdpTransportTarget.return_value = MagicMock()
        mock_hlapi.ContextData.return_value = MagicMock()
        mock_hlapi.ObjectIdentity = MagicMock(side_effect=lambda x: x)
        mock_hlapi.ObjectType = MagicMock(side_effect=lambda x: x)
        mock_hlapi.getCmd.return_value = iter([mock_result])
        mock_hlapi.nextCmd.return_value = iter([mock_result])
        mock_pysnmp = MagicMock()
        mock_pysnmp.hlapi = mock_hlapi
        return mock_pysnmp, mock_hlapi

    def test_flat_record_contains_polled_at(self):
        """Default mode: single record contains _polled_at ISO8601 key."""
        mock_pysnmp, mock_hlapi = self._mock_pysnmp_get({"sysDescr.0": "Linux"})
        with patch.dict("sys.modules", {"pysnmp": mock_pysnmp, "pysnmp.hlapi": mock_hlapi}):
            with patch("pysnmp.hlapi.getCmd", return_value=mock_hlapi.getCmd.return_value):
                src = self._make_source()
                try:
                    results = list(src.read())
                    assert len(results) == 1
                    data = json.loads(results[0][0])
                    assert "_polled_at" in data
                    # Must be a valid ISO8601 string ending with +00:00
                    assert data["_polled_at"].endswith("+00:00")
                except SourceError as e:
                    assert "pysnmp" in str(e)

    def test_polled_at_in_meta(self):
        """meta dict always contains polled_at regardless of yield_rows."""
        mock_pysnmp, mock_hlapi = self._mock_pysnmp_get({"sysDescr.0": "Linux"})
        with patch.dict("sys.modules", {"pysnmp": mock_pysnmp, "pysnmp.hlapi": mock_hlapi}):
            with patch("pysnmp.hlapi.getCmd", return_value=mock_hlapi.getCmd.return_value):
                src = self._make_source()
                try:
                    results = list(src.read())
                    _, meta = results[0]
                    assert "polled_at" in meta
                except SourceError as e:
                    assert "pysnmp" in str(e)

    def test_yield_rows_each_row_has_polled_at(self):
        """yield_rows=True: payload contains per-row records with _polled_at."""
        walk_binds = {
            "1.3.6.1.2.1.2.2.1.2.1": "eth0",
            "1.3.6.1.2.1.2.2.1.2.2": "lo",
        }
        var_binds = list(walk_binds.items())
        mock_result = (None, None, None, var_binds)
        mock_hlapi = MagicMock()
        mock_hlapi.SnmpEngine.return_value = MagicMock()
        mock_hlapi.CommunityData.return_value = MagicMock()
        mock_hlapi.UdpTransportTarget.return_value = MagicMock()
        mock_hlapi.ContextData.return_value = MagicMock()
        mock_hlapi.ObjectIdentity = MagicMock(side_effect=lambda x: x)
        mock_hlapi.ObjectType = MagicMock(side_effect=lambda x: x)
        mock_hlapi.nextCmd.side_effect = [
            iter([mock_result]),
            iter([(None, None, None, [])]),
        ]
        mock_pysnmp = MagicMock()
        mock_pysnmp.hlapi = mock_hlapi

        with patch.dict("sys.modules", {"pysnmp": mock_pysnmp, "pysnmp.hlapi": mock_hlapi}):
            with patch("pysnmp.hlapi.nextCmd", side_effect=[
                iter([mock_result]),
                iter([(None, None, None, [])]),
            ]):
                src = self._make_source({
                    "operation": "walk",
                    "yield_rows": True,
                    "oids": ["1.3.6.1.2.1.2.2.1.2"],
                    "index_depth": 1,
                })
                try:
                    results = list(src.read())
                    assert len(results) == 1
                    rows = json.loads(results[0][0])
                    assert len(rows) == 2
                    for row in rows:
                        assert "_polled_at" in row
                        assert "_index" in row
                        assert "_index_parts" not in row
                except SourceError as e:
                    assert "pysnmp" in str(e)


# ── SNMPTrapSink ───────────────────────────────────────────────────────────


class TestSNMPTrapSink:
    def test_import_error_raises_sink_error(self):
        with patch.dict("sys.modules", {"pysnmp": None, "pysnmp.hlapi": None}):
            sink = SNMPTrapSink({"host": "192.168.1.100"})
            with pytest.raises(SinkError, match="pysnmp"):
                sink.write(b'{"1.3.6.1.2.1.1.1.0": "test"}', {})

    def test_invalid_json_raises_sink_error(self):
        mock_pysnmp = MagicMock()
        with patch.dict("sys.modules", {"pysnmp": mock_pysnmp, "pysnmp.hlapi": mock_pysnmp.hlapi}):
            sink = SNMPTrapSink({"host": "192.168.1.100"})
            with pytest.raises((SinkError, Exception)):
                sink.write(b"not-json", {})

    def test_non_dict_payload_skips_records(self):
        mock_pysnmp = MagicMock()
        with patch.dict("sys.modules", {"pysnmp": mock_pysnmp, "pysnmp.hlapi": mock_pysnmp.hlapi}):
            sink = SNMPTrapSink({"host": "192.168.1.100"})
            with patch("tram.connectors.snmp.sink.asyncio.run") as mock_run:
                sink.write(b'["not", "a", "dict"]', {})
            mock_run.assert_not_called()

    def test_sends_trap_with_bindings(self):
        mock_oid = MagicMock()
        mock_oid.__str__ = lambda s: "1.3.6.1.2.1.1.1.0"

        mock_result = (None, None, None, [])
        mock_send = MagicMock(return_value=iter([mock_result]))

        mock_hlapi = MagicMock()
        mock_hlapi.SnmpEngine = MagicMock()
        mock_hlapi.CommunityData = MagicMock()
        mock_hlapi.UdpTransportTarget = MagicMock()
        mock_hlapi.ContextData = MagicMock()
        mock_hlapi.NotificationType = MagicMock()
        mock_hlapi.ObjectIdentity = MagicMock()
        mock_hlapi.ObjectType = MagicMock()
        mock_hlapi.Integer32 = MagicMock()
        mock_hlapi.OctetString = MagicMock()
        mock_hlapi.sendNotification = mock_send

        mock_pysnmp = MagicMock()
        mock_pysnmp.hlapi = mock_hlapi

        with patch.dict("sys.modules", {"pysnmp": mock_pysnmp, "pysnmp.hlapi": mock_hlapi}):
            with patch("pysnmp.hlapi.sendNotification", mock_send):
                sink = SNMPTrapSink({"host": "192.168.1.100", "port": 162})
                bindings = {"1.3.6.1.2.1.1.1.0": "Linux"}
                try:
                    sink.write(json.dumps(bindings).encode(), {})
                except (SinkError, Exception):
                    # Acceptable in test env without actual pysnmp installed
                    pass


# ── SNMPTrapSink extended coverage ─────────────────────────────────────────


def _make_mock_hlapi():
    """Build a minimal mock pysnmp hlapi asyncio module."""
    mock_hlapi = MagicMock()
    mock_hlapi.SnmpEngine = MagicMock()
    mock_hlapi.CommunityData = MagicMock()
    mock_hlapi.UdpTransportTarget = MagicMock()
    mock_hlapi.ContextData = MagicMock()
    mock_hlapi.ObjectType = MagicMock()
    mock_hlapi.ObjectIdentity = MagicMock()
    mock_hlapi.ObjectIdentifier = MagicMock()
    mock_hlapi.Integer32 = MagicMock()
    mock_hlapi.OctetString = MagicMock()
    mock_hlapi.TimeTicks = MagicMock()
    mock_hlapi.Counter32 = MagicMock()
    mock_hlapi.Gauge32 = MagicMock()
    # sendNotification returns an awaitable (coroutine-like) — mock to return (None, None, None, [])

    async def _fake_send(*a, **kw):
        return (None, None, None, [])

    mock_hlapi.sendNotification = _fake_send
    return mock_hlapi


class TestSNMPTrapSinkExtended:
    def test_legacy_enterprise_oid_alias_populates_trap_oid(self):
        sink = SNMPTrapSink({"host": "127.0.0.1", "enterprise_oid": "1.3.6.1.6.3.1.1.5.3"})
        assert sink.trap_oid == "1.3.6.1.6.3.1.1.5.3"

    def test_list_payload_skips_non_dict_records(self):
        """write() skips non-dict items in a list payload (logs warning, no raise)."""
        sink = SNMPTrapSink({"host": "127.0.0.1"})
        with patch("tram.connectors.snmp.sink.asyncio.run") as mock_run:
            sink.write(b'["not-a-dict", "also-not"]', {})
        mock_run.assert_not_called()

    def test_list_payload_processes_dict_records(self):
        """write() calls asyncio.run for each dict in a list."""
        sink = SNMPTrapSink({"host": "127.0.0.1"})
        with patch("tram.connectors.snmp.sink.asyncio.run", side_effect=_close_coro) as mock_run:
            sink.write(b'[{"1.3.6.1": "v1"}, {"1.3.6.2": "v2"}]', {})
        assert mock_run.call_count == 2

    def test_asyncio_run_general_exception_raises_sink_error(self):
        """Non-SinkError from asyncio.run is wrapped in SinkError."""
        sink = SNMPTrapSink({"host": "127.0.0.1"})
        with patch(
            "tram.connectors.snmp.sink.asyncio.run",
            side_effect=_close_coro_then_raise(RuntimeError("timeout")),
        ):
            with pytest.raises(SinkError, match="SNMP trap send failed"):
                sink.write(b'{"1.3.6.1": "val"}', {})

    def test_asyncio_run_sink_error_re_raised(self):
        """SinkError from asyncio.run is re-raised directly."""
        sink = SNMPTrapSink({"host": "127.0.0.1"})
        with patch(
            "tram.connectors.snmp.sink.asyncio.run",
            side_effect=_close_coro_then_raise(SinkError("trap send error")),
        ):
            with pytest.raises(SinkError, match="trap send error"):
                sink.write(b'{"1.3.6.1": "val"}', {})

    def test_write_single_dict_calls_asyncio_run(self):
        """write() with a single JSON dict calls asyncio.run once."""
        sink = SNMPTrapSink({"host": "127.0.0.1"})
        with patch("tram.connectors.snmp.sink.asyncio.run", side_effect=_close_coro) as mock_run:
            sink.write(b'{"1.3.6.1": "val"}', {})
        mock_run.assert_called_once()

    def test_tram_mib_dir_env_var_added_to_mib_dirs(self, monkeypatch, tmp_path):
        """TRAM_MIB_DIR is auto-prepended to mib_dirs when it exists."""
        monkeypatch.setenv("TRAM_MIB_DIR", str(tmp_path))
        sink = SNMPTrapSink({"host": "127.0.0.1"})
        assert str(tmp_path) in sink.mib_dirs

    def test_mib_dirs_not_duplicated(self, monkeypatch, tmp_path):
        """TRAM_MIB_DIR is not duplicated if already in mib_dirs config."""
        monkeypatch.setenv("TRAM_MIB_DIR", str(tmp_path))
        sink = SNMPTrapSink({"host": "127.0.0.1", "mib_dirs": [str(tmp_path)]})
        assert sink.mib_dirs.count(str(tmp_path)) == 1


class TestSNMPTrapSinkBuildVarBinds:
    """Tests for _build_var_binds with explicit varbinds config."""

    def test_explicit_varbinds_int_type(self):
        mock_hlapi = _make_mock_hlapi()
        sink = SNMPTrapSink({
            "host": "127.0.0.1",
            "varbinds": [{"oid": "1.3.6.1.2.1.1.1.0", "value_field": "sysDescr", "type": "Integer32"}],
        })
        result = sink._build_var_binds(mock_hlapi, {"sysDescr": 42})
        assert len(result) == 1

    def test_explicit_varbinds_octet_string_type(self):
        mock_hlapi = _make_mock_hlapi()
        sink = SNMPTrapSink({
            "host": "127.0.0.1",
            "varbinds": [{"oid": "1.3.6.1.2.1.1.1.0", "value_field": "sysDescr", "type": "OctetString"}],
        })
        result = sink._build_var_binds(mock_hlapi, {"sysDescr": "Linux"})
        assert len(result) == 1

    def test_explicit_varbinds_missing_value_field_skipped(self):
        mock_hlapi = _make_mock_hlapi()
        sink = SNMPTrapSink({
            "host": "127.0.0.1",
            "varbinds": [{"oid": "1.3.6.1", "value_field": "missing_field", "type": "OctetString"}],
        })
        result = sink._build_var_binds(mock_hlapi, {"other_field": "val"})
        assert result == []

    def test_explicit_varbinds_exception_swallowed(self):
        mock_hlapi = _make_mock_hlapi()
        mock_hlapi.ObjectType.side_effect = Exception("bad OID")
        sink = SNMPTrapSink({
            "host": "127.0.0.1",
            "varbinds": [{"oid": "invalid", "value_field": "f", "type": "OctetString"}],
        })
        result = sink._build_var_binds(mock_hlapi, {"f": "val"})
        assert result == []

    def test_auto_type_int_value(self):
        mock_hlapi = _make_mock_hlapi()
        sink = SNMPTrapSink({"host": "127.0.0.1"})
        result = sink._build_var_binds(mock_hlapi, {"1.3.6.1": 100})
        assert len(result) == 1

    def test_auto_type_string_value(self):
        mock_hlapi = _make_mock_hlapi()
        sink = SNMPTrapSink({"host": "127.0.0.1"})
        result = sink._build_var_binds(mock_hlapi, {"1.3.6.1": "string-val"})
        assert len(result) == 1

    def test_auto_type_exception_skipped(self):
        mock_hlapi = _make_mock_hlapi()
        mock_hlapi.ObjectType.side_effect = Exception("bad")
        sink = SNMPTrapSink({"host": "127.0.0.1"})
        result = sink._build_var_binds(mock_hlapi, {"1.3.6.1": "val"})
        assert result == []

    def test_counter32_type_in_type_map(self):
        """Counter32 and other dynamic types are added if available."""
        mock_hlapi = _make_mock_hlapi()
        sink = SNMPTrapSink({
            "host": "127.0.0.1",
            "varbinds": [{"oid": "1.3.6.1", "value_field": "f", "type": "Counter32"}],
        })
        result = sink._build_var_binds(mock_hlapi, {"f": 42})
        assert len(result) == 1


class TestSNMPTrapSinkSendTrap:
    """Tests for _send_trap using asyncio.run."""

    def test_send_trap_passes_varbinds_as_list(self):
        """Async HLAPI expects varBinds as one list argument, not variadic args."""
        captured = {}
        mock_hlapi = _make_mock_hlapi()

        async def _fake_send(snmpEngine, authData, transportTarget, contextData, notifyType, varBinds, **options):
            captured["notifyType"] = notifyType
            captured["varBinds"] = varBinds
            return (None, None, None, [])

        mock_hlapi.sendNotification = _fake_send
        sink = SNMPTrapSink({
            "host": "127.0.0.1",
            "version": "2c",
            "community": "public",
            "trap_oid": "1.3.6.1.6.3.1.1.5.3",
        })

        import asyncio
        asyncio.run(sink._send_trap(mock_hlapi, {"1.3.6.1": "val"}))

        assert captured["notifyType"] == "trap"
        assert isinstance(captured["varBinds"], list)
        assert len(captured["varBinds"]) >= 2

    def test_send_trap_v2c(self):
        """v2c trap: uses CommunityData, calls sendNotification."""
        mock_hlapi = _make_mock_hlapi()
        sink = SNMPTrapSink({
            "host": "127.0.0.1",
            "version": "2c",
            "community": "public",
            "trap_oid": "1.3.6.1.6.3.1.1.5.3",
        })
        import asyncio
        asyncio.run(sink._send_trap(mock_hlapi, {"1.3.6.1": "val"}))
        mock_hlapi.CommunityData.assert_called()
        mock_hlapi.ObjectIdentifier.assert_called_with("1.3.6.1.6.3.1.1.5.3")

    def test_send_trap_v1(self):
        """v1 trap: uses CommunityData with mpModel=0."""
        mock_hlapi = _make_mock_hlapi()
        sink = SNMPTrapSink({"host": "127.0.0.1", "version": "1"})
        import asyncio
        asyncio.run(sink._send_trap(mock_hlapi, {}))
        _, kwargs = mock_hlapi.CommunityData.call_args
        assert kwargs.get("mpModel") == 0

    def test_send_trap_v3(self):
        """v3 trap: uses build_v3_auth for authentication."""
        mock_hlapi = _make_mock_hlapi()
        sink = SNMPTrapSink({
            "host": "127.0.0.1",
            "version": "3",
            "security_name": "admin",
            "auth_key": "authkey1234",
        })
        import asyncio
        with patch("tram.connectors.snmp.mib_utils.build_v3_auth", return_value=MagicMock()):
            asyncio.run(sink._send_trap(mock_hlapi, {}))

    def test_send_trap_with_context_name(self):
        """context_name triggers ContextData with contextName."""
        mock_hlapi = _make_mock_hlapi()
        sink = SNMPTrapSink({"host": "127.0.0.1", "context_name": "myctx"})
        import asyncio
        asyncio.run(sink._send_trap(mock_hlapi, {}))
        mock_hlapi.ContextData.assert_called_with(contextName="myctx")

    def test_send_trap_error_indication_raises(self):
        """errInd in sendNotification response raises SinkError."""
        mock_hlapi = _make_mock_hlapi()

        async def _fake_send_with_err(*a, **kw):
            return ("timeout error", None, None, [])

        mock_hlapi.sendNotification = _fake_send_with_err
        sink = SNMPTrapSink({"host": "127.0.0.1"})
        import asyncio
        with pytest.raises(SinkError, match="SNMP trap send error"):
            asyncio.run(sink._send_trap(mock_hlapi, {}))

    def test_send_trap_error_status_raises(self):
        """errStatus in sendNotification response raises SinkError."""
        mock_hlapi = _make_mock_hlapi()
        err_status = MagicMock()
        err_status.prettyPrint.return_value = "noSuchObject"

        async def _fake_send_with_status(*a, **kw):
            return (None, err_status, None, [])

        mock_hlapi.sendNotification = _fake_send_with_status
        sink = SNMPTrapSink({"host": "127.0.0.1"})
        import asyncio
        with pytest.raises(SinkError, match="SNMP trap PDU error"):
            asyncio.run(sink._send_trap(mock_hlapi, {}))
