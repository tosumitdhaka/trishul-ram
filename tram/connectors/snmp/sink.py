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

        # Build VarBind objects
        var_binds = []
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
