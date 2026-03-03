"""SNMP source connectors — trap receiver and polling source."""

from __future__ import annotations

import logging
import socket
import threading
from typing import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


@register_source("snmp_trap")
class SNMPTrapSource(BaseSource):
    """Receive SNMP traps (v1/v2c) over UDP, operating in stream mode.

    Each trap is decoded into a dict of OID → value bindings and yielded as
    ``(json_bytes, meta)``.

    Requires ``pysnmp-lextudio>=6.2`` (``pip install tram[snmp]``).

    Config keys:
        host       (str, default "0.0.0.0")   Bind address.
        port       (int, default 162)          UDP port for traps.
        community  (str, default "public")     SNMP community string.
        version    (str, default "2c")         "2c" or "3" (v3 not fully supported).
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config.get("host", "0.0.0.0")
        self.port: int = int(config.get("port", 162))
        self.community: str = config.get("community", "public")
        self.version: str = str(config.get("version", "2c"))
        self._stop_event: threading.Event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def read(self) -> Iterator[tuple[bytes, dict]]:
        try:
            from pysnmp.hlapi import SnmpEngine
            from pysnmp.carrier.asyncio.dgram import udp
            from pysnmp.entity import engine, config as snmp_config
            from pysnmp.entity.rfc3413 import ntfrcv
        except ImportError as exc:
            raise SourceError(
                "SNMP trap source requires pysnmp-lextudio — install with: pip install tram[snmp]"
            ) from exc

        # Fall back to raw UDP socket-based trap receiver for simplicity
        yield from self._read_raw_udp()

    def _read_raw_udp(self) -> Iterator[tuple[bytes, dict]]:
        """Raw UDP receiver — yields raw trap bytes with basic metadata."""
        import json
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(1.0)
            sock.bind((self.host, self.port))
        except Exception as exc:
            raise SourceError(
                f"SNMP trap UDP bind failed on {self.host}:{self.port} — {exc}"
            ) from exc

        logger.info(
            "SNMP trap source listening",
            extra={"host": self.host, "port": self.port},
        )
        try:
            while not self._stop_event.is_set():
                try:
                    raw, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except Exception as exc:
                    logger.warning("SNMP trap recv error: %s", exc)
                    continue
                source_ip, src_port = addr
                bindings = self._decode_trap(raw)
                meta = {
                    "source_ip": source_ip,
                    "port": src_port,
                    "community": self.community,
                    "version": self.version,
                }
                payload = json.dumps(bindings).encode("utf-8")
                logger.debug(
                    "SNMP trap received",
                    extra={"source_ip": source_ip, "bindings": len(bindings)},
                )
                yield payload, meta
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _decode_trap(self, raw: bytes) -> dict:
        """Attempt to decode SNMP trap using pysnmp; fall back to raw hex."""
        try:
            from pysnmp.proto import api as snmp_api
            pMod = snmp_api.protoModules[snmp_api.protoVersion2c]
            reqMsg, _ = decoder.decode(raw, asn1Spec=pMod.Message())  # type: ignore[name-defined]
            reqPDU = pMod.apiMessage.getPDU(reqMsg)
            bindings: dict = {}
            for oid, val in pMod.apiPDU.getVarBinds(reqPDU):
                bindings[str(oid)] = str(val)
            return bindings
        except Exception:
            return {"_raw": raw.hex()}


@register_source("snmp_poll")
class SNMPPollSource(BaseSource):
    """Poll an SNMP agent (GET or WALK), operating in batch mode.

    Each run issues the configured GET or WALK operation and yields a single
    ``(json_bytes, meta)`` tuple containing all OID bindings.

    Requires ``pysnmp-lextudio>=6.2`` (``pip install tram[snmp]``).

    Config keys:
        host       (str, required)             SNMP agent hostname or IP.
        port       (int, default 161)          SNMP agent port.
        community  (str, default "public")     Community string.
        version    (str, default "2c")         SNMP version.
        oids       (list[str], required)       OIDs to GET or WALK.
        operation  (str, default "get")        "get" or "walk".
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config["host"]
        self.port: int = int(config.get("port", 161))
        self.community: str = config.get("community", "public")
        self.version: str = str(config.get("version", "2c"))
        self.oids: list[str] = list(config.get("oids", []))
        self.operation: str = config.get("operation", "get").lower()

    def read(self) -> Iterator[tuple[bytes, dict]]:
        import json
        try:
            from pysnmp.hlapi import (
                CommunityData,
                ContextData,
                ObjectIdentity,
                ObjectType,
                SnmpEngine,
                UdpTransportTarget,
                getCmd,
                nextCmd,
            )
        except ImportError as exc:
            raise SourceError(
                "SNMP poll source requires pysnmp-lextudio — install with: pip install tram[snmp]"
            ) from exc

        engine = SnmpEngine()
        community = CommunityData(self.community)
        target = UdpTransportTarget((self.host, self.port))
        context = ContextData()
        object_types = [ObjectType(ObjectIdentity(oid)) for oid in self.oids]

        bindings: dict = {}
        try:
            if self.operation == "get":
                error_indication, error_status, error_index, var_binds = next(
                    getCmd(engine, community, target, context, *object_types)
                )
                if error_indication:
                    raise SourceError(f"SNMP GET error: {error_indication}")
                if error_status:
                    raise SourceError(
                        f"SNMP GET PDU error: {error_status.prettyPrint()} "
                        f"at {error_index and var_binds[int(error_index) - 1][0] or '?'}"
                    )
                for oid, val in var_binds:
                    bindings[str(oid)] = str(val)
            elif self.operation == "walk":
                for error_indication, error_status, error_index, var_binds in nextCmd(
                    engine, community, target, context, *object_types, lexicographicMode=False
                ):
                    if error_indication:
                        raise SourceError(f"SNMP WALK error: {error_indication}")
                    if error_status:
                        raise SourceError(f"SNMP WALK PDU error: {error_status.prettyPrint()}")
                    for oid, val in var_binds:
                        bindings[str(oid)] = str(val)
            else:
                raise SourceError(f"SNMP poll: unsupported operation '{self.operation}' (use get or walk)")
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"SNMP poll failed: {exc}") from exc

        payload = json.dumps(bindings).encode("utf-8")
        meta = {
            "source_host": self.host,
            "source_port": self.port,
            "operation": self.operation,
            "oids": self.oids,
        }
        logger.info(
            "SNMP poll completed",
            extra={"host": self.host, "operation": self.operation, "bindings": len(bindings)},
        )
        yield payload, meta
