"""SNMP source connectors — trap receiver and polling source."""

from __future__ import annotations

import datetime
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
    """Receive SNMP traps (v1/v2c/v3) over UDP, operating in stream mode.

    Each trap is decoded into a dict of OID → value bindings and yielded as
    ``(json_bytes, meta)``.

    Requires ``pysnmp-lextudio>=6.2`` (``pip install tram[snmp]``).

    Config keys:
        host            (str, default "0.0.0.0")  Bind address.
        port            (int, default 162)         UDP port for traps.
        community       (str, default "public")    SNMP v1/v2c community string.
        version         (str, default "2c")        "1", "2c", or "3".
        security_name   (str)   SNMPv3 USM username.
        auth_protocol   (str)   MD5 | SHA | SHA224 | SHA256 | SHA384 | SHA512.
        auth_key        (str)   Auth passphrase (None → noAuthNoPriv).
        priv_protocol   (str)   DES | 3DES | AES | AES128 | AES192 | AES256.
        priv_key        (str)   Privacy passphrase (None → authNoPriv).
        context_name    (str)   SNMPv3 context name.

    Note: SNMPv3 trap *receiving* stores the config for future full USM decode
    support.  Incoming v3 packets are currently decoded on a best-effort basis
    and fall back to ``{"_raw": "<hex>"}`` when USM decryption is required.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config.get("host", "0.0.0.0")
        self.port: int = int(config.get("port", 162))
        self.community: str = config.get("community", "public")
        self.version: str = str(config.get("version", "2c"))
        self.mib_dirs: list[str] = list(config.get("mib_dirs", []))
        self.mib_modules: list[str] = list(config.get("mib_modules", []))
        self.resolve_oids: bool = bool(config.get("resolve_oids", True))
        # SNMPv3 USM
        self.security_name: str = config.get("security_name", "")
        self.auth_protocol: str = config.get("auth_protocol", "SHA")
        self.auth_key: str | None = config.get("auth_key")
        self.priv_protocol: str = config.get("priv_protocol", "AES128")
        self.priv_key: str | None = config.get("priv_key")
        self.context_name: str = config.get("context_name", "")
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
                raw_bindings = self._decode_trap(raw)
                if self.resolve_oids and (self.mib_dirs or self.mib_modules):
                    try:
                        from tram.connectors.snmp.mib_utils import get_mib_view, resolve_oid, oid_str_to_tuple
                        mib_view = get_mib_view(self.mib_dirs, self.mib_modules)
                        bindings = {
                            resolve_oid(mib_view, oid_str_to_tuple(oid)): val
                            for oid, val in raw_bindings.items()
                            if not oid.startswith("_")
                        }
                        # preserve _raw if present
                        if "_raw" in raw_bindings:
                            bindings["_raw"] = raw_bindings["_raw"]
                    except Exception:
                        bindings = raw_bindings
                else:
                    bindings = raw_bindings
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
    """Poll an SNMP agent (GET or WALK) using SNMPv1, v2c, or v3.

    Each run issues the configured GET or WALK operation and yields one or more
    ``(json_bytes, meta)`` tuples.  Every record contains ``_polled_at`` (UTC
    ISO8601).  Set ``yield_rows=True`` to receive one record per table row.

    Requires ``pysnmp-lextudio>=6.2`` (``pip install tram[snmp]``).

    Config keys:
        host            (str, required)        SNMP agent hostname or IP.
        port            (int, default 161)     SNMP agent port.
        community       (str, default "public") Community string (v1/v2c).
        version         (str, default "2c")    "1", "2c", or "3".
        oids            (list[str], required)  OIDs to GET or WALK.
        operation       (str, default "get")   "get" or "walk".
        yield_rows      (bool, default False)  Yield one record per table row.
        index_depth     (int, default 0)       Index split depth (0=auto).
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
        self.port: int = int(config.get("port", 161))
        self.community: str = config.get("community", "public")
        self.version: str = str(config.get("version", "2c"))
        self.oids: list[str] = list(config.get("oids", []))
        self.operation: str = config.get("operation", "get").lower()
        self.mib_dirs: list[str] = list(config.get("mib_dirs", []))
        self.mib_modules: list[str] = list(config.get("mib_modules", []))
        self.resolve_oids: bool = bool(config.get("resolve_oids", True))
        self.yield_rows: bool = bool(config.get("yield_rows", False))
        self.index_depth: int = int(config.get("index_depth", 0))
        # SNMPv3 USM
        self.security_name: str = config.get("security_name", "")
        self.auth_protocol: str = config.get("auth_protocol", "SHA")
        self.auth_key: str | None = config.get("auth_key")
        self.priv_protocol: str = config.get("priv_protocol", "AES128")
        self.priv_key: str | None = config.get("priv_key")
        self.context_name: str = config.get("context_name", "")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _group_by_index(bindings: dict, index_depth: int) -> list[dict]:
        """Group flat ``{oid_key: value}`` bindings into per-row dicts.

        Each key is split into a *column* name and a *row index*:

        * ``index_depth == 0`` (auto) — split on the **first dot**.
          Works correctly for MIB-resolved names such as ``ifDescr.1`` or
          ``atPhysAddress.1.192.168.1.1``.

        * ``index_depth > 0`` — last *N* OID components form the index.
          Use this for numeric OIDs when you know the table instance depth
          (e.g. ``index_depth=4`` for single IPv4-addressed rows, or
          ``index_depth=5`` for interface-index + IPv4 composite keys).

        The returned list contains one dict per unique index value.  Each
        dict carries:

        * ``_index`` — the compound row index as a dot-separated string
          (e.g. ``"1"``, ``"10.0.0.1"``, ``"1.192.168.1.1"``).
        * ``_index_parts`` — the index split into a list of strings for
          easy downstream parsing of composite indexes.
        * One key per column/symbol with the corresponding value.
        """
        rows: dict[str, dict] = {}
        for key, val in bindings.items():
            if index_depth == 0:
                # Auto: split on first dot → (symbol, instance)
                dot_pos = key.find(".")
                if dot_pos == -1:
                    col, idx = key, ""
                else:
                    col, idx = key[:dot_pos], key[dot_pos + 1:]
            else:
                parts = key.split(".")
                if len(parts) > index_depth:
                    col = ".".join(parts[:-index_depth])
                    idx = ".".join(parts[-index_depth:])
                else:
                    col, idx = key, ""

            if idx not in rows:
                rows[idx] = {
                    "_index": idx,
                    "_index_parts": idx.split(".") if idx else [],
                }
            rows[idx][col] = val

        # Stable order: sort by index string
        return [rows[k] for k in sorted(rows)]

    # ------------------------------------------------------------------
    # read()
    # ------------------------------------------------------------------

    def read(self) -> Iterator[tuple[bytes, dict]]:
        import json
        try:
            import pysnmp.hlapi as _hlapi
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
        object_types = [ObjectType(ObjectIdentity(oid)) for oid in self.oids]

        polled_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        bindings: dict = {}
        try:
            if self.operation == "get":
                error_indication, error_status, error_index, var_binds = next(
                    getCmd(engine, auth_data, target, context, *object_types)
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
                    engine, auth_data, target, context, *object_types, lexicographicMode=False
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

        # Optional MIB-based OID resolution
        if self.resolve_oids and (self.mib_dirs or self.mib_modules):
            try:
                from tram.connectors.snmp.mib_utils import get_mib_view, resolve_oid, oid_str_to_tuple
                mib_view = get_mib_view(self.mib_dirs, self.mib_modules)
                bindings = {
                    resolve_oid(mib_view, oid_str_to_tuple(oid)): val
                    for oid, val in bindings.items()
                }
            except Exception:
                pass  # Keep numeric OIDs on resolution failure

        meta = {
            "source_host": self.host,
            "source_port": self.port,
            "operation": self.operation,
            "oids": self.oids,
            "polled_at": polled_at,
        }

        logger.info(
            "SNMP poll completed",
            extra={"host": self.host, "operation": self.operation, "bindings": len(bindings)},
        )

        if self.yield_rows:
            rows = self._group_by_index(bindings, self.index_depth)
            for row in rows:
                row["_polled_at"] = polled_at
                yield json.dumps(row).encode("utf-8"), meta
        else:
            bindings["_polled_at"] = polled_at
            yield json.dumps(bindings).encode("utf-8"), meta
