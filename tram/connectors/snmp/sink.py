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
    """Send SNMP v2c Trap/InformRequest to a target NMS/trap receiver.

    Expects the input ``data`` to be a JSON-encoded dict of OID → value bindings.
    Each key-value pair becomes a VarBind in the outgoing trap PDU.

    Requires ``pysnmp-lextudio>=6.2`` (``pip install tram[snmp]``).

    Config keys:
        host           (str, required)         Target NMS hostname or IP.
        port           (int, default 162)       UDP trap port.
        community      (str, default "public")  SNMP community string.
        version        (str, default "2c")      SNMP version (only 2c supported).
        enterprise_oid (str, default "1.3.6.1.4.1.0")  Enterprise OID.
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

    def write(self, data: bytes, meta: dict) -> None:
        try:
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
        community = CommunityData(self.community)
        target = UdpTransportTarget((self.host, self.port))
        context = ContextData()
        notification = NotificationType(ObjectIdentity(self.enterprise_oid))

        try:
            error_indication, error_status, error_index, var_binds_result = next(
                sendNotification(
                    engine,
                    community,
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
