"""SNMP trap sink connector — sends SNMP traps to a target agent."""

from __future__ import annotations

import json
import logging

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


@register_sink("snmp_trap")
class SNMPTrapSink(BaseSink):
    """Send SNMP v1/v2c/v3 Trap/InformRequest to a target NMS/trap receiver.

    Expects the input ``data`` to be a JSON-encoded dict of OID → value bindings.
    Each key-value pair becomes a VarBind in the outgoing trap PDU.

    Requires ``pysnmp-lextudio>=6.2`` (``pip install tram[snmp]``).

    Config keys:
        host            (str, required)         Target NMS hostname or IP.
        port            (int, default 162)       UDP trap port.
        community       (str, default "public")  SNMP v1/v2c community string.
        version         (str, default "2c")      "1", "2c", or "3".
        enterprise_oid  (str, default "1.3.6.1.4.1.0")  Enterprise OID.
        security_name   (str)   SNMPv3 USM username.
        auth_protocol   (str)   MD5 | SHA | SHA224 | SHA256 | SHA384 | SHA512.
        auth_key        (str)   Auth passphrase (None → noAuthNoPriv).
        priv_protocol   (str)   DES | 3DES | AES | AES128 | AES192 | AES256.
        priv_key        (str)   Privacy passphrase (None → authNoPriv).
        context_name    (str)   SNMPv3 context name.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config["host"]
        self.port: int = int(config.get("port", 162))
        self.community: str = config.get("community", "public")
        self.version: str = str(config.get("version", "2c"))
        self.enterprise_oid: str = config.get("enterprise_oid", "1.3.6.1.4.1.0")
        self.mib_dirs: list[str] = list(config.get("mib_dirs", []))
        self.mib_modules: list[str] = list(config.get("mib_modules", []))
        self.varbinds: list[dict] = list(config.get("varbinds", []))
        # Auto-prepend MIB dirs: always include the image-baked /mibs dir plus TRAM_MIB_DIR
        import os as _os
        _BUILTIN = "/mibs"
        _custom = _os.environ.get("TRAM_MIB_DIR", "")
        for _d in [_BUILTIN, _custom]:
            if _d and _os.path.isdir(_d) and _d not in self.mib_dirs:
                self.mib_dirs.insert(0, _d)
        # SNMPv3 USM
        self.security_name: str = config.get("security_name", "")
        self.auth_protocol: str = config.get("auth_protocol", "SHA")
        self.auth_key: str | None = config.get("auth_key")
        self.priv_protocol: str = config.get("priv_protocol", "AES128")
        self.priv_key: str | None = config.get("priv_key")
        self.context_name: str = config.get("context_name", "")

    def write(self, data: bytes, meta: dict) -> None:
        try:
            import pysnmp.hlapi as _hlapi
            from pysnmp.hlapi import (
                CommunityData,
                ContextData,
                NotificationType,
                ObjectIdentity,
                ObjectType,
                Integer32,
                OctetString,
                SnmpEngine,
                UdpTransportTarget,
                sendNotification,
            )
        except ImportError as exc:
            raise SinkError(
                "SNMP trap sink requires pysnmp-lextudio — install with: pip install tram[snmp]"
            ) from exc

        # Parse bindings from the payload
        try:
            bindings_raw: dict = json.loads(data)
        except Exception as exc:
            raise SinkError(f"SNMP trap sink: failed to parse data as JSON: {exc}") from exc

        if not isinstance(bindings_raw, dict):
            raise SinkError("SNMP trap sink: expected a JSON object (dict) of OID→value bindings")

        # Map of pysnmp type name → class
        _type_map = {
            "Integer32": Integer32,
            "OctetString": OctetString,
        }
        try:
            from pysnmp.hlapi import Counter32, Gauge32, TimeTicks
            _type_map["Counter32"] = Counter32
            _type_map["Gauge32"] = Gauge32
            _type_map["TimeTicks"] = TimeTicks
        except ImportError:
            pass

        var_binds = []

        if self.varbinds:
            # Explicit varbind spec with optional symbolic OID resolution
            mib_view = None
            if self.mib_dirs or self.mib_modules:
                try:
                    from tram.connectors.snmp.mib_utils import get_mib_view, symbolic_to_oid
                    mib_view = get_mib_view(self.mib_dirs, self.mib_modules)
                except Exception:
                    pass

            for vb in self.varbinds:
                oid_str = vb.get("oid", "")
                value_field = vb.get("value_field", "")
                type_name = vb.get("type", "OctetString")
                val = bindings_raw.get(value_field)
                if val is None:
                    continue

                # Resolve symbolic OID if needed
                if "::" in oid_str or not oid_str[0].isdigit():
                    if mib_view is not None:
                        from tram.connectors.snmp.mib_utils import symbolic_to_oid
                        resolved = symbolic_to_oid(mib_view, oid_str)
                        if resolved:
                            oid_str = ".".join(str(x) for x in resolved)

                try:
                    snmp_type_cls = _type_map.get(type_name, OctetString)
                    var_binds.append(ObjectType(ObjectIdentity(oid_str), snmp_type_cls(val)))
                except Exception as exc:
                    logger.warning("Skipping varbind %s=%s: %s", oid_str, val, exc)
        else:
            # Auto-typing from raw dict (legacy behavior)
            for oid, val in bindings_raw.items():
                try:
                    if isinstance(val, int):
                        var_binds.append(ObjectType(ObjectIdentity(oid), Integer32(val)))
                    else:
                        var_binds.append(ObjectType(ObjectIdentity(oid), OctetString(str(val))))
                except Exception as exc:
                    logger.warning("Skipping invalid OID binding %s=%s: %s", oid, val, exc)

        engine = SnmpEngine()

        if self.version == "3":
            from tram.connectors.snmp.mib_utils import build_v3_auth
            auth_data = build_v3_auth(
                _hlapi,
                security_name=self.security_name,
                auth_protocol=self.auth_protocol,
                auth_key=self.auth_key,
                priv_protocol=self.priv_protocol,
                priv_key=self.priv_key,
            )
        else:
            auth_data = CommunityData(self.community)

        target = UdpTransportTarget((self.host, self.port))
        context = (
            ContextData(contextName=self.context_name)
            if self.context_name
            else ContextData()
        )
        notification = NotificationType(ObjectIdentity(self.enterprise_oid))

        try:
            error_indication, error_status, error_index, var_binds_result = next(
                sendNotification(
                    engine,
                    auth_data,
                    target,
                    context,
                    "trap",
                    notification,
                    *var_binds,
                )
            )
            if error_indication:
                raise SinkError(f"SNMP trap send error: {error_indication}")
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"SNMP trap send failed to {self.host}:{self.port}: {exc}") from exc

        logger.info(
            "SNMP trap sent",
            extra={
                "host": self.host,
                "port": self.port,
                "enterprise_oid": self.enterprise_oid,
                "bindings": len(bindings_raw),
            },
        )
