"""v1.5.0 layer 3 lane B (GH #72) — tsnmp (trishul_snmp) connector paths + flag flow.

Covers the ``TRAM_SNMP_STACK=trishul`` wire layers:

* ``mib_utils`` tsmi JSON-bundle resolve (the dual-format corpus consumers)
  and the tsnmp USM builders.
* poll source GET/WALK against an in-process tsnmp responder (the harness
  reference pattern) + the typed/classify mapping.
* trap source tsnmp listener bridge + offline ``decode_notification``.
* trap sink v1/v2c/v3 sends against in-process tsnmp listeners.
* worker stats payload ``snmp_stack`` field and the manager-side mismatch
  warning (once per worker).

The ``legacy`` path is untouched by these tests; the pre-existing SNMP suite
is the flag-off proof and stays green unmodified.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tram.api.routers import internal as internal_router
from tram.connectors.snmp.mib_utils import (
    build_tsnmp_local_engine,
    build_v3_usm_user,
    get_mib_view,
    resolve_oid,
    resolve_oid_structured,
    snmp_stack,
    symbolic_to_oid,
)
from tram.connectors.snmp.sink import SNMPTrapSink
from tram.connectors.snmp.source import SNMPPollSource, SNMPTrapSource
from tram.core.exceptions import SourceError

try:  # pragma: no cover - import probe only
    import trishul_snmp  # noqa: F401

    _TSNMP_AVAILABLE = True
except Exception:  # pragma: no cover
    _TSNMP_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _TSNMP_AVAILABLE, reason="trishul_snmp not installed")


def _free_port() -> int:
    """Allocate an ephemeral UDP port (best-effort; used once per test)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_bundle(tmp_path: Path) -> Path:
    """Write a small dual-module tsmi JSON corpus mirroring the compiled layout.

    Lane A emits ``files/mibs_compiled/MIB.json`` alongside the ``.py``; the
    connector flag-on path loads every ``*.json`` in ``mib_dirs``.
    """
    snmpv2 = {
        "module": "SNMPv2-MIB",
        "language": "SMIv2",
        "schema_version": "1.1",
        "producer_version": "0.5.2",
        "generated_by": "trishul-smi",
        "generated_at": "2026-09-28T00:00:00Z",
        "imports": {},
        "objects": {
            "sysDescr": {
                "oid": "1.3.6.1.2.1.1.1",
                "object_type": "OBJECT-TYPE",
                "class": "objecttype",
                "nodetype": "scalar",
                "syntax": "DisplayString",
            },
            "sysName": {
                "oid": "1.3.6.1.2.1.1.5",
                "object_type": "OBJECT-TYPE",
                "class": "objecttype",
                "nodetype": "scalar",
            },
        },
        "types": {},
        "notifications": {},
        "module_metadata": {},
    }
    ifmib = {
        "module": "IF-MIB",
        "language": "SMIv2",
        "schema_version": "1.1",
        "producer_version": "0.5.2",
        "generated_by": "trishul-smi",
        "generated_at": "2026-09-28T00:00:00Z",
        "imports": {"SNMPv2-MIB": ["sysDescr"]},
        "objects": {
            "ifIndex": {
                "oid": "1.3.6.1.2.1.2.2.1.1",
                "object_type": "OBJECT-TYPE",
                "class": "objecttype",
                "nodetype": "columnar",
            },
            "ifDescr": {
                "oid": "1.3.6.1.2.1.2.2.1.2",
                "object_type": "OBJECT-TYPE",
                "class": "objecttype",
                "nodetype": "columnar",
            },
            "ifOperStatus": {
                "oid": "1.3.6.1.2.1.2.2.1.8",
                "object_type": "OBJECT-TYPE",
                "class": "objecttype",
                "nodetype": "columnar",
                "enums": {"up": 1, "down": 2, "testing": 3},
            },
            # TC-typed column (trishul-smi #44): SYNTAX names the IANAifType
            # textual convention, so no inline enums — the enum table lives in
            # IANAifType-MIB's types section.
            "ifType": {
                "oid": "1.3.6.1.2.1.2.2.1.3",
                "object_type": "OBJECT-TYPE",
                "class": "objecttype",
                "nodetype": "columnar",
                "syntax": "IANAifType",
            },
        },
        "types": {},
        "notifications": {},
        "module_metadata": {},
    }
    ianaiftype = {
        "module": "IANAifType-MIB",
        "language": "SMIv2",
        "schema_version": "1.1",
        "producer_version": "0.5.2",
        "generated_by": "trishul-smi",
        "generated_at": "2026-09-28T00:00:00Z",
        "imports": {},
        "objects": {},
        "types": {
            "IANAifType": {
                "class": "textualconvention",
                "base_type": "INTEGER",
                "constraints": {
                    "kind": "enum",
                    "data": [
                        ["softwareLoopback", 24],
                        ["ethernetCsmacd", 6],
                        ["ieee80211", 71],
                    ],
                },
            },
        },
        "notifications": {},
        "module_metadata": {},
    }
    ifmib["imports"] = {
        "SNMPv2-MIB": ["sysDescr"],
        "IANAifType-MIB": ["IANAifType"],
    }
    (tmp_path / "SNMPv2-MIB.json").write_text(json.dumps(snmpv2))
    (tmp_path / "IF-MIB.json").write_text(json.dumps(ifmib))
    (tmp_path / "IANAifType-MIB.json").write_text(json.dumps(ianaiftype))
    return tmp_path


# ── flag reader + tsmi JSON-bundle resolve (mib_utils) ─────────────────────


class TestSnmpStackFlag:
    def test_defaults_to_trishul(self, monkeypatch):
        monkeypatch.delenv("TRAM_SNMP_STACK", raising=False)
        assert snmp_stack() == "trishul"

    def test_trishul_env(self, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        assert snmp_stack() == "trishul"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "Trishul")
        assert snmp_stack() == "trishul"

    def test_invalid_value_fails_loud(self, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "pysnmp")
        with pytest.raises(ValueError, match="TRAM_SNMP_STACK"):
            snmp_stack()


class TestTsmiMibResolve:
    def test_get_mib_view_returns_tsmi_view(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        _write_bundle(tmp_path)
        view = get_mib_view([str(tmp_path)], ["SNMPv2-MIB", "IF-MIB"])
        assert type(view).__name__ == "_TsmiBundleView"
        assert hasattr(view, "lookup")
        assert not hasattr(view, "mibBuilder")

    def test_resolve_shapes_match_legacy(self, tmp_path, monkeypatch):
        """tsmi resolve produces the same (sym, indices, str, mod) shapes the
        legacy pysnmp path produces for the same OIDs."""
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        _write_bundle(tmp_path)
        view = get_mib_view([str(tmp_path)], ["SNMPv2-MIB", "IF-MIB"])

        assert resolve_oid_structured(view, (1, 3, 6, 1, 2, 1, 1, 1, 0)) == (
            "sysDescr", (0,), "sysDescr.0", "SNMPv2-MIB",
        )
        assert resolve_oid_structured(view, (1, 3, 6, 1, 2, 1, 1, 1)) == (
            "sysDescr", (), "sysDescr", "SNMPv2-MIB",
        )
        assert resolve_oid_structured(view, (1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1)) == (
            "ifIndex", (1,), "ifIndex.1", "IF-MIB",
        )
        assert resolve_oid(view, (1, 3, 6, 1, 2, 1, 1, 5, 0)) == "sysName.0"

    def test_unresolved_oid_falls_back_to_numeric(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        _write_bundle(tmp_path)
        view = get_mib_view([str(tmp_path)], ["SNMPv2-MIB"])
        assert resolve_oid_structured(view, (1, 3, 6, 1, 9, 9, 9)) == (
            "", (), "1.3.6.1.9.9.9", "",
        )

    def test_symbolic_to_oid_module_and_bare(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        _write_bundle(tmp_path)
        view = get_mib_view([str(tmp_path)], ["SNMPv2-MIB", "IF-MIB"])
        assert symbolic_to_oid(view, "IF-MIB::ifDescr.1") == (1, 3, 6, 1, 2, 1, 2, 2, 1, 2, 1)
        assert symbolic_to_oid(view, "sysDescr.0") == (1, 3, 6, 1, 2, 1, 1, 1, 0)
        assert symbolic_to_oid(view, "ifOperStatus.3") == (1, 3, 6, 1, 2, 1, 2, 2, 1, 8, 3)
        assert symbolic_to_oid(view, "noSuchObject.1") is None

    def test_default_uses_tsnmp_view(self, tmp_path, monkeypatch):
        """Default (v1.8.0 flip): no bundles → None (the tsnmp path), not the
        pysnmp MibViewController."""
        monkeypatch.delenv("TRAM_SNMP_STACK", raising=False)
        view = get_mib_view([str(tmp_path)], ["SNMPv2-MIB"])
        assert view is None

    def test_explicit_legacy_returns_pysnmp_view(self, tmp_path, monkeypatch):
        """Explicit escape hatch: TRAM_SNMP_STACK=legacy keeps the pysnmp
        MibViewController (available through v1.9.0)."""
        monkeypatch.setenv("TRAM_SNMP_STACK", "legacy")
        view = get_mib_view([str(tmp_path)], ["SNMPv2-MIB"])
        assert type(view).__name__ == "MibViewController"

    def test_no_json_bundles_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        (tmp_path / "not-a-bundle.txt").write_text("hi")
        view = get_mib_view([str(tmp_path)], ["SNMPv2-MIB"])
        assert view is None


class TestBuildV3UsmUser:
    def test_noauth_nopriv(self):
        user = build_v3_usm_user(security_name="u")
        assert user.username == "u"
        assert user.auth_protocol.name == "NONE"
        assert user.priv_protocol.name == "NONE"

    def test_authnopriv(self):
        user = build_v3_usm_user(security_name="u", auth_protocol="SHA256", auth_key="k")
        assert user.auth_protocol.name == "SHA256"
        assert user.auth_key == b"k"
        assert user.priv_protocol.name == "NONE"

    def test_authpriv_full_matrix(self):
        user = build_v3_usm_user(
            security_name="u", auth_protocol="SHA512", auth_key="ak",
            priv_protocol="AES256", priv_key="pk",
        )
        assert user.auth_protocol.name == "SHA512"
        assert user.priv_protocol.name == "AES256"
        assert user.auth_key == b"ak"
        assert user.priv_key == b"pk"

    def test_protocol_aliases(self):
        assert build_v3_usm_user("u", "SHA", "k", "AES", "p").auth_protocol.name == "SHA1"
        assert build_v3_usm_user("u", "SHA", "k", "AES", "p").priv_protocol.name == "AES128"
        assert build_v3_usm_user("u", "MD5", "k", "3DES", "p").priv_protocol.name == "THREEDES_EDE"
        # 3DES-EDE spelling alias (v1.5.1) — same wire protocol.
        assert build_v3_usm_user("u", "MD5", "k", "3des-ede", "p").priv_protocol.name == "THREEDES_EDE"

    def test_unknown_protocol_falls_back(self):
        user = build_v3_usm_user("u", "UNKNOWN", "k", "UNKNOWN", "p")
        assert user.auth_protocol.name == "SHA1"
        assert user.priv_protocol.name == "AES128"

    def test_des_rejected_at_validation_layer_not_builder(self):
        # DES configs never reach the builder (validation rejects them in
        # v1.5.0 layer 2), but the builder still maps the enum for completeness.
        assert build_v3_usm_user("u", "SHA", "k", "DES", "p").priv_protocol.name == "DES"


class TestBuildTsnmpLocalEngine:
    def test_deterministic_and_shaped(self):
        a = build_tsnmp_local_engine("tram:1.2.3.4:162:user")
        b = build_tsnmp_local_engine("tram:1.2.3.4:162:user")
        assert a.engine_id == b.engine_id
        assert len(a.engine_id) == 17
        assert a.engine_id[:5] == b"\x80\x00\x01\x02\x03"
        assert a.engine_boots == 1
        c = build_tsnmp_local_engine("tram:5.6.7.8:162:other")
        assert a.engine_id != c.engine_id


# ── poll source tsnmp wire layer ────────────────────────────────────────────


class _ResponderThread:
    """In-process tsnmp responder on its own event loop (harness pattern)."""

    def __init__(self, objects: list, port: int | None = None):
        self.port = port or _free_port()
        self._objects = objects
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        from trishul_snmp import V2cResponder

        async def _serve():
            async with V2cResponder(
                host="127.0.0.1", port=self.port, communities=["public"],
                objects=self._objects,
            ) as responder:
                serve = asyncio.create_task(responder.serve_forever())
                while not self._stop.is_set():
                    await asyncio.sleep(0.05)
                serve.cancel()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_serve())
        loop.close()

    def __enter__(self):
        self._thread.start()
        import time
        time.sleep(0.2)
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)
        return False


_RESPONDER_OBJECTS = None


def _responder_objects() -> list:
    """Build the in-process responder's object table (lazy — trishul_snmp import)."""
    global _RESPONDER_OBJECTS
    if _RESPONDER_OBJECTS is None:
        _RESPONDER_OBJECTS = [
            ((1, 3, 6, 1, 2, 1, 1, 1, 0), _tsnmp_value("OctetStringValue", b"tsnmp responder")),
            ((1, 3, 6, 1, 2, 1, 1, 5, 0), _tsnmp_value("OctetStringValue", b"box")),
            ((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1), _tsnmp_value("IntegerValue", 1)),
            ((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 2), _tsnmp_value("IntegerValue", 2)),
            ((1, 3, 6, 1, 2, 1, 2, 2, 1, 10, 1), _tsnmp_value("Counter32Value", 100)),
            ((1, 3, 6, 1, 2, 1, 2, 2, 1, 10, 2), _tsnmp_value("Counter32Value", 200)),
        ]
    return _RESPONDER_OBJECTS


def _tsnmp_value(cls_name: str, raw):
    cls = getattr(trishul_snmp, cls_name)
    return cls(raw)


class TestPollSourceTsnmp:
    def test_get_against_inprocess_responder(self, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        with _ResponderThread(_responder_objects()) as responder:
            src = SNMPPollSource({
                "host": "127.0.0.1", "port": responder.port,
                "oids": ["1.3.6.1.2.1.1.1.0"], "operation": "get",
            })
            payload, meta = next(iter(src.read()))
        data = json.loads(payload)
        assert data["1.3.6.1.2.1.1.1.0"] == "tsnmp responder"
        assert meta["operation"] == "get"

    def test_get_error_status_raises(self, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")

        from trishul_snmp import ErrorStatus, IntegerValue, Response, VarBind

        class _FakeMgr:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, *targets):
                return Response(
                    request_id=1, error_status=ErrorStatus.NO_SUCH_NAME,
                    error_index=1,
                    varbinds=(VarBind(oid=(1, 3, 6, 1, 2, 1, 1, 1, 0), value=IntegerValue(1))),
                )

        src = SNMPPollSource({
            "host": "127.0.0.1", "oids": ["1.3.6.1.2.1.1.1.0"], "operation": "get",
        })
        with patch.object(src, "_build_tsnmp_manager", return_value=_FakeMgr()):
            with pytest.raises(SourceError, match="NO_SUCH_NAME"):
                asyncio.run(src._do_get_tsnmp())

    def test_walk_against_inprocess_responder(self, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        with _ResponderThread(_responder_objects()) as responder:
            src = SNMPPollSource({
                "host": "127.0.0.1", "port": responder.port,
                "oids": ["1.3.6.1.2.1.2"], "operation": "walk",
            })
            payload, _ = next(iter(src.read()))
        data = json.loads(payload)
        assert data == {
            "1.3.6.1.2.1.2.2.1.1.1": "1",
            "1.3.6.1.2.1.2.2.1.1.2": "2",
            "1.3.6.1.2.1.2.2.1.10.1": "100",
            "1.3.6.1.2.1.2.2.1.10.2": "200",
            "_polled_at": data["_polled_at"],
        }

    def test_classify_typed_walk_maps_wire_types(self, monkeypatch):
        """tsnmp type names map to the legacy wire-class names so the shared
        classify layer (GH #35 fixed tables) works unchanged."""
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        with _ResponderThread(_responder_objects()) as responder:
            src = SNMPPollSource({
                "host": "127.0.0.1", "port": responder.port,
                "oids": ["1.3.6.1.2.1.2"], "operation": "walk",
                "classify": True, "yield_rows": True, "index_depth": 1,
                "resolve_oids": False,
            })
            payload, _ = next(iter(src.read()))
        rows = json.loads(payload)
        by_index = {r["_index"]: r for r in rows}
        assert by_index["1"]["_metrics"] == {
            "1.3.6.1.2.1.2.2.1.1": 1,
            "1.3.6.1.2.1.2.2.1.10": 100,
        }
        assert by_index["1"]["_snmp_widths"] == {"1.3.6.1.2.1.2.2.1.10": 32}

    def test_v3_get_typed_mapping(self, monkeypatch):
        """v3 authPriv GET path: tsnmp values serialize + map with USM config."""
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")

        from trishul_snmp import (
            Counter32Value,
            ErrorStatus,
            OctetStringValue,
            Response,
            VarBind,
        )

        captured = {}

        class _FakeMgr:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, *targets):
                captured["targets"] = targets
                return Response(
                    request_id=1, error_status=ErrorStatus.NO_ERROR, error_index=0,
                    varbinds=(
                        VarBind(oid=(1, 3, 6, 1, 2, 1, 1, 1, 0), value=OctetStringValue(b"v3-box")),
                        VarBind(oid=(1, 3, 6, 1, 2, 1, 2, 2, 1, 10, 1), value=Counter32Value(77)),
                    ),
                )

        src = SNMPPollSource({
            "host": "10.0.0.1", "oids": ["1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.2.2.1.10.1"],
            "operation": "get", "version": "3", "security_name": "v3user",
            "auth_protocol": "SHA256", "auth_key": "authpass",
            "priv_protocol": "AES128", "priv_key": "privpass",
        })
        with patch.object(src, "_build_tsnmp_manager", return_value=_FakeMgr()):
            result = asyncio.run(src._do_get_tsnmp(typed=True))
        assert result == {
            "1.3.6.1.2.1.1.1.0": ("v3-box", "OctetString"),
            "1.3.6.1.2.1.2.2.1.10.1": ("77", "Counter32"),
        }
        assert captured["targets"] == ("1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.2.2.1.10.1")

    def test_binary_octet_string_hex_policy(self, monkeypatch):
        """_tsnmp_val_to_str mirrors the legacy hex/MAC policy for binary data."""
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        from trishul_snmp import OctetStringValue

        assert SNMPPollSource._tsnmp_val_to_str(OctetStringValue(b"\x01\x02\x03\x04\x05\x06")) == (
            "01:02:03:04:05:06"
        )
        assert SNMPPollSource._tsnmp_val_to_str(OctetStringValue(b"\x00\xff\x10")) == "0x00ff10"
        assert SNMPPollSource._tsnmp_val_to_str(OctetStringValue(b"printable")) == "printable"

    def test_test_connection_tsnmp(self, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        with _ResponderThread(_responder_objects()) as responder:
            src = SNMPPollSource({
                "host": "127.0.0.1", "port": responder.port,
                "oids": ["1.3.6.1.2.1.1.1.0"],
            })
            result = src.test_connection()
        assert result["ok"] is True
        assert "sysDescr" in result["detail"] or "tsnmp responder" in result["detail"]

    def test_classify_resolve_enum_via_tsmi_bundle(self, tmp_path, monkeypatch):
        """End-to-end: tsnmp typed walk + tsmi-bundle resolution + enum labels.

        The shared classify layer consumes the tsmi bundle's structured
        indices and enums exactly as it consumes the pysnmp MIB view —
        ifOperStatus.1 = 1 renders "up (1)", Counter32 gets _snmp_widths.
        """
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        import json as _json
        _write_bundle(tmp_path)
        bundle = _json.loads((tmp_path / "IF-MIB.json").read_text())
        bundle["objects"]["ifInOctets"] = {
            "oid": "1.3.6.1.2.1.2.2.1.10",
            "object_type": "OBJECT-TYPE", "class": "objecttype", "nodetype": "columnar",
        }
        (tmp_path / "IF-MIB.json").write_text(_json.dumps(bundle))

        objects = [
            ((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1), _tsnmp_value("IntegerValue", 1)),
            ((1, 3, 6, 1, 2, 1, 2, 2, 1, 8, 1), _tsnmp_value("IntegerValue", 1)),
            ((1, 3, 6, 1, 2, 1, 2, 2, 1, 10, 1), _tsnmp_value("Counter32Value", 100)),
        ]
        with _ResponderThread(objects) as responder:
            src = SNMPPollSource({
                "host": "127.0.0.1", "port": responder.port,
                "oids": ["1.3.6.1.2.1.2"], "operation": "walk",
                "classify": True, "yield_rows": True, "index_depth": 0,
                "resolve_oids": True, "mib_dirs": [str(tmp_path)],
                "mib_modules": ["IF-MIB"],
            })
            payload, _ = next(iter(src.read()))
        rows = json.loads(payload)
        assert len(rows) == 1
        assert rows[0]["_index"] == "1"
        assert rows[0]["_labels"] == {"ifIndex": "1", "ifOperStatus": "up (1)"}
        assert rows[0]["_metrics"] == {"ifInOctets": 100}
        assert rows[0]["_snmp_widths"] == {"ifInOctets": 32}

    def test_classify_tc_enum_via_tsmi_bundle(self, tmp_path, monkeypatch):
        """TC-typed columns render their TC's enum labels (trishul-smi #44).

        ``ifType``'s SYNTAX names the ``IANAifType`` textual convention — the
        tsmi IR keeps TC enums in ``types``, not on the column node. The
        classify layer threads them through, matching legacy pysnmp's
        ``softwareLoopback (24)`` rendering. Precedence is preserved: inline
        enum (ifOperStatus) and TC enum (ifType) both label; a non-enum
        column stays a plain value.
        """
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        _write_bundle(tmp_path)

        objects = [
            ((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1), _tsnmp_value("IntegerValue", 1)),
            ((1, 3, 6, 1, 2, 1, 2, 2, 1, 3, 1), _tsnmp_value("IntegerValue", 24)),
            ((1, 3, 6, 1, 2, 1, 2, 2, 1, 8, 1), _tsnmp_value("IntegerValue", 1)),
        ]
        with _ResponderThread(objects) as responder:
            src = SNMPPollSource({
                "host": "127.0.0.1", "port": responder.port,
                "oids": ["1.3.6.1.2.1.2"], "operation": "walk",
                "classify": True, "yield_rows": True, "index_depth": 0,
                "resolve_oids": True, "mib_dirs": [str(tmp_path)],
                "mib_modules": ["IF-MIB"],
            })
            payload, _ = next(iter(src.read()))
        rows = json.loads(payload)
        assert len(rows) == 1
        assert rows[0]["_index"] == "1"
        assert rows[0]["_labels"] == {
            "ifIndex": "1",
            "ifType": "softwareLoopback (24)",
            "ifOperStatus": "up (1)",
        }
        assert rows[0]["_metrics"] == {}

    def test_mib_enum_name_tc_graceful_degradation(self, tmp_path, monkeypatch):
        """Missing TC module degrades exactly like today (None, no exception)."""
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        _write_bundle(tmp_path)
        import json as _json
        bundle = _json.loads((tmp_path / "IF-MIB.json").read_text())
        # Point a column at a TC that exists in no module of the corpus.
        bundle["objects"]["ifType"]["syntax"] = "NoSuchTC"
        (tmp_path / "IF-MIB.json").write_text(_json.dumps(bundle))
        view = get_mib_view([str(tmp_path)], ["IF-MIB"])
        assert SNMPPollSource._mib_enum_name(view, "IF-MIB", "ifType", "24") is None
        # Non-enum columns stay untouched.
        assert SNMPPollSource._mib_enum_name(view, "IF-MIB", "ifDescr", "lo") is None

    def test_mib_enum_name_real_corpus_tc_parity(self, monkeypatch):
        """Shipped corpus: ifType renders the same labels as legacy pysnmp.

        files/mibs_compiled carries IF-MIB + IANAifType-MIB in both formats,
        so the TC-enum fallback resolves against the real corpus — the exact
        values from the kind E2E capture (24/6/71 on a live ifTable).
        """
        import os
        corpus = os.path.join(os.path.dirname(__file__), "..", "..", "files", "mibs_compiled")
        corpus = os.path.normpath(corpus)
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        view = get_mib_view([corpus], [])
        assert SNMPPollSource._mib_enum_name(view, "IF-MIB", "ifType", "24") == "softwareLoopback"
        assert SNMPPollSource._mib_enum_name(view, "IF-MIB", "ifType", "6") == "ethernetCsmacd"
        assert SNMPPollSource._mib_enum_name(view, "IF-MIB", "ifType", "71") == "ieee80211"
        # Inline enums and unknown values are unaffected.
        assert SNMPPollSource._mib_enum_name(view, "IF-MIB", "ifOperStatus", "1") == "up"
        assert SNMPPollSource._mib_enum_name(view, "IF-MIB", "ifType", "0") is None

    def test_mib_enum_name_tc_range_constraint_not_enum(self, tmp_path, monkeypatch):
        """Range-constrained TCs (kind "range") must not be read as enum
        tables (review B1): a saturation value returns None, never the range
        MIN as a bogus label."""
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        _write_bundle(tmp_path)
        import json as _json
        bundle = _json.loads((tmp_path / "SNMPv2-MIB.json").read_text())
        bundle["types"]["Integer32"] = {
            "class": "textualconvention",
            "base_type": "INTEGER",
            "constraints": {"kind": "range", "data": [[-2147483648, 2147483647]]},
        }
        (tmp_path / "SNMPv2-MIB.json").write_text(_json.dumps(bundle))
        ifmib = _json.loads((tmp_path / "IF-MIB.json").read_text())
        ifmib["objects"]["ipDefaultTTL"] = {
            "oid": "1.3.6.1.2.1.4.2",
            "object_type": "OBJECT-TYPE",
            "class": "objecttype",
            "nodetype": "scalar",
            "syntax": "Integer32",
        }
        ifmib["imports"]["SNMPv2-MIB"] = ["sysDescr", "Integer32"]
        (tmp_path / "IF-MIB.json").write_text(_json.dumps(ifmib))
        view = get_mib_view([str(tmp_path)], ["IF-MIB"])
        assert SNMPPollSource._mib_enum_name(view, "IF-MIB", "ipDefaultTTL", "2147483647") is None
        assert SNMPPollSource._mib_enum_name(view, "IF-MIB", "ipDefaultTTL", "-2147483648") is None
        # The real enum-TC path still works after the gate.
        assert SNMPPollSource._mib_enum_name(view, "IF-MIB", "ifType", "24") == "softwareLoopback"

    def test_mib_enum_name_real_corpus_range_tc_stays_none(self, monkeypatch):
        """Shipped corpus: Integer32 (range TC) at a saturation value stays
        None — the review B1 repro through the actual code path."""
        import os
        corpus = os.path.join(os.path.dirname(__file__), "..", "..", "files", "mibs_compiled")
        corpus = os.path.normpath(corpus)
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        view = get_mib_view([corpus], [])
        assert SNMPPollSource._mib_enum_name(view, "IP-MIB", "ipDefaultTTL", "2147483647") is None
        assert SNMPPollSource._mib_enum_name(view, "IP-MIB", "ipDefaultTTL", "-2147483648") is None


# ── trap source tsnmp path ───────────────────────────────────────────────────


class _SenderThread:
    """Sends one trap from its own event loop (tsnmp notifier)."""

    def __init__(self, port: int, version: str = "2c", **usm):
        self.port = port
        self.version = version
        self.usm = usm

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._send())
        loop.close()

    async def _send(self):
        from trishul_snmp import OctetStringValue, V2cNotifier

        if self.version == "3":
            from trishul_snmp import UsmLocalEngine, V3Notifier

            engine = UsmLocalEngine(
                engine_id=b"\x80\x00\x01\x02\x03" + b"\x55" * 12, engine_boots=3, engine_time=42
            )
            user = build_v3_usm_user(
                security_name=self.usm["security_name"],
                auth_protocol=self.usm["auth_protocol"],
                auth_key=self.usm["auth_key"],
                priv_protocol=self.usm["priv_protocol"],
                priv_key=self.usm["priv_key"],
            )
            async with V3Notifier(
                host="127.0.0.1", port=self.port, user=user, local_engine=engine
            ) as notifier:
                await notifier.send_trap(
                    (1, 3, 6, 1, 6, 3, 1, 1, 5, 3),
                    varbinds=[((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1), OctetStringValue(b"eth0"))],
                    uptime=777,
                )
            return
        async with V2cNotifier(host="127.0.0.1", port=self.port, community="public") as notifier:
            await notifier.send_trap(
                (1, 3, 6, 1, 6, 3, 1, 1, 5, 3),
                varbinds=[((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1), OctetStringValue(b"eth0"))],
                uptime=777,
            )


class TestTrapSourceTsnmp:
    def test_read_stream_receives_v2c_trap(self, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        port = _free_port()
        src = SNMPTrapSource({"host": "127.0.0.1", "port": port, "version": "2c"})
        it = src.read()
        _SenderThread(port).start()
        payload, meta = next(it)
        src.stop()
        list(it)
        data = json.loads(payload)
        assert data["1.3.6.1.2.1.2.2.1.1.1"] == "eth0"
        assert "1.3.6.1.6.3.1.1.4.1.0" in data
        assert meta["source_ip"] == "127.0.0.1"
        assert meta["version"] == "2c"

    def test_read_stream_receives_v3_trap(self, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        port = _free_port()
        src = SNMPTrapSource({
            "host": "127.0.0.1", "port": port, "version": "3",
            "security_name": "trapuser", "auth_protocol": "SHA256", "auth_key": "authpass",
            "priv_protocol": "AES128", "priv_key": "privpass",
        })
        it = src.read()
        _SenderThread(port, version="3", security_name="trapuser",
                      auth_protocol="SHA256", auth_key="authpass",
                      priv_protocol="AES128", priv_key="privpass").start()
        payload, meta = next(it)
        src.stop()
        list(it)
        data = json.loads(payload)
        assert data["1.3.6.1.2.1.2.2.1.1.1"] == "eth0"

    def test_bind_failure_raises_source_error(self, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        port = _free_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", port))
        try:
            src = SNMPTrapSource({"host": "127.0.0.1", "port": port, "version": "2c"})
            with pytest.raises(SourceError, match="UDP bind failed"):
                list(src.read())
        finally:
            blocker.close()

    def test_decode_notification_v2c(self, monkeypatch):
        """decode_notification path: send a trap to a capture socket, decode it.

        Calls tsnmp's ``decode_notification`` directly (C6): the flag-on trap
        stream has no connector-level offline decoder — the listener path
        uses ``decode_notification`` internally, which is what this exercises.
        """
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        from trishul_snmp import decode_notification

        datagrams, port = self._capture_one_trap()
        assert datagrams, "no datagram captured"
        event = decode_notification(datagrams[0])
        result = {vb.oid_str: vb.value.to_display_string() for vb in event.varbinds}
        assert result["1.3.6.1.2.1.2.2.1.1.1"] == "eth0"

    def test_decode_notification_v3_with_user(self, monkeypatch):
        """decode_notification with user: offline USM authPriv trap decode."""
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        from trishul_snmp import decode_notification

        datagrams, port = self._capture_one_trap(version="3")
        assert datagrams, "no datagram captured"
        user = build_v3_usm_user(
            security_name="trapuser", auth_protocol="SHA256", auth_key="authpass",
            priv_protocol="AES128", priv_key="privpass",
        )
        event = decode_notification(datagrams[0], user=user)
        result = {vb.oid_str: vb.value.to_display_string() for vb in event.varbinds}
        assert result["1.3.6.1.2.1.2.2.1.1.1"] == "eth0"

    @staticmethod
    def _capture_one_trap(version: str = "2c") -> tuple[list[bytes], int]:
        """Bind a capture socket, send one trap at it, return (captured, port)."""
        import time

        captured: list[bytes] = []
        port = _free_port()

        class _Grab(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                captured.append(data)

        def _capture():
            async def _grab():
                loop = asyncio.get_running_loop()
                transport, _ = await loop.create_datagram_endpoint(
                    _Grab, local_addr=("127.0.0.1", port)
                )
                try:
                    await asyncio.sleep(3.0)
                finally:
                    transport.close()

            asyncio.run(_grab())

        t = threading.Thread(target=_capture, daemon=True)
        t.start()
        time.sleep(0.3)  # let the capture endpoint bind before the sender fires
        _SenderThread(port, version=version, security_name="trapuser",
                      auth_protocol="SHA256", auth_key="authpass",
                      priv_protocol="AES128", priv_key="privpass").start()
        deadline = time.monotonic() + 3.0
        while not captured and time.monotonic() < deadline:
            time.sleep(0.05)
        return captured, port

    def test_decode_notification_rejects_garbage(self, monkeypatch):
        """decode_notification raises on undecodable bytes (the listener path
        drops them; there is no flag-on _raw fallback)."""
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        from trishul_snmp import decode_notification

        with pytest.raises(Exception):
            decode_notification(b"\x00\x01\x02garbage")


# ── trap sink tsnmp path ─────────────────────────────────────────────────────


class _CaptureListener:
    """In-process tsnmp notification listener capturing one event.

    Signals ``ready`` once the listener socket is bound so a sender fired
    right after ``start()`` can never drop its trap into an unbound socket
    (UDP has no receiver for it) — the same race the source-side listener
    guards against.
    """

    def __init__(self, port: int, version: str = "2c", **usm):
        self.port = port
        self.version = version
        self.usm = usm
        self.event = None
        self.ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        assert self.ready.wait(5), "capture listener did not bind"

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._listen())
        loop.close()

    async def _listen(self):
        if self.version == "3":
            from trishul_snmp import UsmLocalEngine, V3NotificationListener

            engine = UsmLocalEngine(
                engine_id=b"\x80\x00\x01\x02\x03" + b"\x77" * 12, engine_boots=7, engine_time=111
            )
            user = build_v3_usm_user(
                security_name=self.usm["security_name"],
                auth_protocol=self.usm["auth_protocol"],
                auth_key=self.usm["auth_key"],
                priv_protocol=self.usm["priv_protocol"],
                priv_key=self.usm["priv_key"],
            )
            async with V3NotificationListener(
                host="127.0.0.1", port=self.port, user=user, local_engine=engine
            ) as listener:
                self.ready.set()
                self.event = await asyncio.wait_for(listener.receive(), timeout=5)
            return
        from trishul_snmp import V2cNotificationListener

        async with V2cNotificationListener(
            host="127.0.0.1", port=self.port, communities=None
        ) as listener:
            self.ready.set()
            self.event = await asyncio.wait_for(listener.receive(), timeout=5)

    def join(self, timeout: float = 6):
        self._thread.join(timeout=timeout)
        assert self.event is not None, "listener captured no event"


class TestTrapSinkTsnmp:
    def test_v2c_send_auto_varbinds(self, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        port = _free_port()
        listener = _CaptureListener(port)
        listener.start()
        sink = SNMPTrapSink({
            "host": "127.0.0.1", "port": port, "trap_oid": "1.3.6.1.4.1.99999",
            "version": "2c",
        })
        sink.write(json.dumps({"1.3.6.1.4.1.99999.1.0": "alarm", "1.3.6.1.4.1.99999.2.0": 42}).encode(), {})
        listener.join()
        varbinds = {vb.oid_str: vb.value for vb in listener.event.varbinds}
        assert varbinds["1.3.6.1.4.1.99999.1.0"].to_display_string() == "alarm"
        assert varbinds["1.3.6.1.4.1.99999.2.0"].to_display_string() == "42"
        # the notifier auto-built the mandatory sysUpTime.0 + snmpTrapOID.0
        assert "1.3.6.1.2.1.1.3.0" in varbinds
        assert varbinds["1.3.6.1.6.3.1.1.4.1.0"].to_display_string() == "1.3.6.1.4.1.99999"

    def test_v3_send_authpriv(self, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        port = _free_port()
        usm = dict(security_name="trapuser", auth_protocol="SHA256", auth_key="authpass",
                   priv_protocol="AES128", priv_key="privpass")
        listener = _CaptureListener(port, version="3", **usm)
        listener.start()
        sink = SNMPTrapSink({
            "host": "127.0.0.1", "port": port, "version": "3", **usm,
        })
        sink.write(json.dumps({"1.3.6.1.4.1.99999.1.0": "v3-alarm"}).encode(), {})
        listener.join()
        assert listener.event.security_level == "authPriv"
        varbinds = {vb.oid_str: vb.value.to_display_string() for vb in listener.event.varbinds}
        assert varbinds["1.3.6.1.4.1.99999.1.0"] == "v3-alarm"

    def test_v1_send_trap_pdu_metadata(self, monkeypatch):
        """v1 Trap-PDU fields split per RFC 2576 §3.2 (review BUG 1).

        trap_oid ``1.3.6.1.4.1.99999`` must be encoded as enterprise
        ``1.3.6.1.4.1`` + specific ``99999`` (generic 6) — an RFC
        2576-conformant NMS reconstructs ``enterprise.specific`` =
        ``1.3.6.1.4.1.99999``. The old behavior passed the raw OID as the
        enterprise with specific=0, mis-encoding the trap OID.
        """
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        port = _free_port()
        listener = _CaptureListener(port)
        listener.start()
        sink = SNMPTrapSink({
            "host": "127.0.0.1", "port": port, "version": "1",
            "trap_oid": "1.3.6.1.4.1.99999", "community": "public",
        })
        sink.write(json.dumps({"1.3.6.1.4.1.99999.1.0": "v1-alarm"}).encode(), {})
        listener.join()
        d = listener.event.to_dict()
        assert d["pdu_type"] == "trap"
        assert d["generic_trap"] == 6
        assert d["enterprise"] == "1.3.6.1.4.1"
        assert d["specific_trap"] == 99999
        # RFC 2576 reconstruction: enterprise.specific == the configured trap OID
        assert f"{d['enterprise']}.{d['specific_trap']}" == "1.3.6.1.4.1.99999"
        varbinds = {vb.oid_str: vb.value.to_display_string() for vb in listener.event.varbinds}
        assert varbinds["1.3.6.1.4.1.99999.1.0"] == "v1-alarm"
        # the v1 PDU carries sysUpTime in its header — no snmpTrapOID varbind
        assert "1.3.6.1.6.3.1.1.4.1.0" not in varbinds
        assert "1.3.6.1.2.1.1.3.0" in varbinds  # tsnmp auto-prepends sysUpTime.0

    def test_v1_send_standard_trap_maps_to_generic(self, monkeypatch):
        """Standard trap OIDs map to generic traps 0-5 (RFC 2576 §3.2.3)."""
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        port = _free_port()
        listener = _CaptureListener(port)
        listener.start()
        sink = SNMPTrapSink({
            "host": "127.0.0.1", "port": port, "version": "1",
            "trap_oid": "1.3.6.1.6.3.1.1.5.2",  # warmStart
            "community": "public",
        })
        sink.write(json.dumps({"1.3.6.1.4.1.99999.1.0": "warm"}).encode(), {})
        listener.join()
        d = listener.event.to_dict()
        assert d["generic_trap"] == 1  # warmStart
        assert d["specific_trap"] == 0

    def test_varbind_spec_with_symbolic_oid(self, tmp_path, monkeypatch):
        """Explicit varbind spec resolves symbolic OIDs against the tsmi corpus."""
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        bundle_dir = _write_bundle(tmp_path)
        port = _free_port()
        listener = _CaptureListener(port)
        listener.start()
        sink = SNMPTrapSink({
            "host": "127.0.0.1", "port": port, "trap_oid": "1.3.6.1.4.1.99999",
            "version": "2c", "mib_dirs": [str(bundle_dir)],
            "varbinds": [
                {"oid": "IF-MIB::ifDescr.1", "value_field": "descr", "type": "OctetString"},
                {"oid": "1.3.6.1.4.1.99999.1.2", "value_field": "count", "type": "Counter32"},
            ],
        })
        sink.write(json.dumps({"descr": "eth0", "count": 77}).encode(), {})
        listener.join()
        varbinds = {vb.oid_str: vb.value.to_display_string() for vb in listener.event.varbinds}
        assert varbinds["1.3.6.1.2.1.2.2.1.2.1"] == "eth0"
        assert varbinds["1.3.6.1.4.1.99999.1.2"] == "77"


class TestTrapSinkLegacyRegression:
    """Review BUG 2: the legacy pysnmp send path passed the varbind list as ONE
    positional to ``sendNotification`` (``SmiError: ObjectType object not fully
    initialized`` on every send). These are real, unmocked sends against an
    in-process tsnmp listener — no ``hlapi_send_notification`` mocking."""

    def test_legacy_v2c_send_against_tsnmp_listener(self, monkeypatch):
        monkeypatch.delenv("TRAM_SNMP_STACK", raising=False)  # legacy / flag off
        port = _free_port()
        listener = _CaptureListener(port)
        listener.start()
        sink = SNMPTrapSink({
            "host": "127.0.0.1", "port": port, "trap_oid": "1.3.6.1.4.1.99999",
            "version": "2c",
        })
        sink.write(json.dumps({"1.3.6.1.4.1.99999.1.0": "legacy-alarm"}).encode(), {})
        listener.join()
        varbinds = {vb.oid_str: vb.value.to_display_string() for vb in listener.event.varbinds}
        assert varbinds["1.3.6.1.4.1.99999.1.0"] == "legacy-alarm"
        assert varbinds["1.3.6.1.6.3.1.1.4.1.0"] == "1.3.6.1.4.1.99999"
        assert "1.3.6.1.2.1.1.3.0" in varbinds

    def test_legacy_v1_send_against_tsnmp_listener(self, monkeypatch):
        monkeypatch.delenv("TRAM_SNMP_STACK", raising=False)  # legacy / flag off
        port = _free_port()
        listener = _CaptureListener(port)
        listener.start()
        sink = SNMPTrapSink({
            "host": "127.0.0.1", "port": port, "trap_oid": "1.3.6.1.4.1.99999",
            "version": "1", "community": "public",
        })
        sink.write(json.dumps({"1.3.6.1.4.1.99999.1.0": "legacy-v1"}).encode(), {})
        listener.join()
        varbinds = {vb.oid_str: vb.value.to_display_string() for vb in listener.event.varbinds}
        assert varbinds["1.3.6.1.4.1.99999.1.0"] == "legacy-v1"
        # pysnmp's v1 conversion carries the RFC 2576 enterprise/specific
        # split (the same BUG 1 semantics the tsnmp path now emits).
        d = listener.event.to_dict()
        assert d["generic_trap"] == 6
        assert d["enterprise"] == "1.3.6.1.4.1"
        assert d["specific_trap"] == 99999


# ── worker stats payload + manager mismatch guard ───────────────────────────


class TestWorkerStatsSnmpStack:
    def test_periodic_stats_payload_includes_snmp_stack(self):
        from unittest.mock import MagicMock

        from tram.agent.metrics import PipelineStats
        from tram.agent.server import ActiveRun, WorkerState, _emit_stats_once

        state = WorkerState(worker_id="w0", manager_url="http://manager", snmp_stack="trishul")
        run = ActiveRun(
            run_id="run-1",
            pipeline_name="pipe-a",
            schedule_type="stream",
            started_at="2026-04-17T12:00:00+00:00",
            stats_url="http://manager/api/internal/pipeline-stats",
        )
        run.stats = PipelineStats(run_id="run-1", pipeline_name="pipe-a", schedule_type="stream")
        run.stats.increment(records_in=5, bytes_in=100)
        state.add(run)

        captured = {}

        def _fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json")
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _fake_post
            mock_client_cls.return_value = mock_client
            _emit_stats_once(state)

        # V18-08: the periodic payload is one batched snapshot per worker —
        # snmp_stack rides the batch envelope, run stats ride in ``runs``.
        assert captured["json"]["snmp_stack"] == "trishul"
        assert captured["json"]["runs"][0]["records_in"] == 5

    def test_worker_app_selects_stack_from_settings(self, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "trishul")
        from tram.agent.server import create_worker_app
        app = create_worker_app(worker_id="w9", manager_url="http://mgr")
        assert app.state.worker.snmp_stack == "trishul"

    def test_worker_app_default_trishul(self, monkeypatch):
        monkeypatch.delenv("TRAM_SNMP_STACK", raising=False)
        from tram.agent.server import create_worker_app
        app = create_worker_app(worker_id="w8", manager_url="http://mgr")
        assert app.state.worker.snmp_stack == "trishul"

    def test_worker_app_invalid_stack_fails_loud(self, monkeypatch):
        monkeypatch.setenv("TRAM_SNMP_STACK", "bogus")
        from tram.agent.server import create_worker_app
        with pytest.raises(ValueError, match="TRAM_SNMP_STACK"):
            create_worker_app(worker_id="w7", manager_url="http://mgr")


class TestManagerSnmpStackMismatch:
    def _make_app(self, manager_stack: str):
        from unittest.mock import MagicMock

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(internal_router.router)
        app.state.controller = MagicMock()
        app.state.stats_store = MagicMock()
        app.state.config = SimpleNamespace(snmp_stack=manager_stack)
        return TestClient(app)

    def _post_stats(self, client, worker_id="w0", snmp_stack="legacy"):
        return client.post("/api/internal/pipeline-stats", json={
            "worker_id": worker_id,
            "pipeline_name": "pipe-a",
            "run_id": f"run-{worker_id}",
            "schedule_type": "stream",
            "uptime_seconds": 10.5,
            "timestamp": "2026-04-17T12:00:00+00:00",
            "snmp_stack": snmp_stack,
        })

    @pytest.fixture(autouse=True)
    def _clear_warned(self):
        internal_router._MISMATCH_WARNED_WORKERS.clear()
        yield
        internal_router._MISMATCH_WARNED_WORKERS.clear()

    def test_mismatch_warns_once_per_worker(self, caplog):
        import logging
        client = self._make_app(manager_stack="trishul")

        with caplog.at_level(logging.WARNING, logger="tram.api.routers.internal"):
            assert self._post_stats(client, worker_id="w-mismatch").status_code == 200
            assert self._post_stats(client, worker_id="w-mismatch").status_code == 200

        warnings = [r for r in caplog.records if r.message.startswith("SNMP stack mismatch")]
        assert len(warnings) == 1
        assert warnings[0].worker_snmp_stack == "legacy"
        assert warnings[0].manager_snmp_stack == "trishul"

    def test_legacy_worker_omitting_field_trips_guard(self, caplog):
        """A pre-flag worker sends no snmp_stack → defaults legacy → mismatch."""
        import logging
        client = self._make_app(manager_stack="trishul")
        with caplog.at_level(logging.WARNING, logger="tram.api.routers.internal"):
            self._post_stats(client, worker_id="w-old").status_code == 200
        assert any(r.message.startswith("SNMP stack mismatch") for r in caplog.records)

    def test_matching_stacks_no_warning(self, caplog):
        import logging
        client = self._make_app(manager_stack="legacy")
        with caplog.at_level(logging.WARNING, logger="tram.api.routers.internal"):
            self._post_stats(client, worker_id="w-ok").status_code == 200
            self._post_stats(client, worker_id="w-ok2", snmp_stack="legacy").status_code == 200
        assert not any(r.message.startswith("SNMP stack mismatch") for r in caplog.records)

    def test_trishul_worker_under_trishul_manager_no_warning(self, caplog):
        import logging
        client = self._make_app(manager_stack="trishul")
        with caplog.at_level(logging.WARNING, logger="tram.api.routers.internal"):
            self._post_stats(client, worker_id="w-t", snmp_stack="trishul").status_code == 200
        assert not any(r.message.startswith("SNMP stack mismatch") for r in caplog.records)

    def test_different_workers_warn_independently(self, caplog):
        import logging
        client = self._make_app(manager_stack="trishul")
        with caplog.at_level(logging.WARNING, logger="tram.api.routers.internal"):
            self._post_stats(client, worker_id="w-a").status_code == 200
            self._post_stats(client, worker_id="w-b").status_code == 200
        warnings = [r for r in caplog.records if r.message.startswith("SNMP stack mismatch")]
        assert len(warnings) == 2