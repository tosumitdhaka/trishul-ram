"""SNMP source connectors — trap receiver and polling source."""

from __future__ import annotations

import asyncio
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

    Requires ``pysnmp-lextudio>=6.2,<6.3`` with ``pyasn1>=0.4.8,<0.6``
    (``pip install tram[snmp]``).

    Config keys:
        host            (str, default "0.0.0.0")  Bind address.
        port            (int, default 162)         UDP port for traps.
        community       (str, default "public")    SNMP v1/v2c community string.
        version         (str, default "2c")        "1", "2c", or "3".
        resolve_oids    (bool, default True)       Resolve OIDs via MIB view.
        mib_dirs        (list[str])                Paths to compiled MIB dirs.
        mib_modules     (list[str])                MIB module names to load.
        security_name   (str)   SNMPv3 USM username.
        auth_protocol   (str)   MD5 | SHA | SHA224 | SHA256 | SHA384 | SHA512.
        auth_key        (str)   Auth passphrase (None → noAuthNoPriv).
        priv_protocol   (str)   DES | 3DES | AES | AES128 | AES192 | AES256.
        priv_key        (str)   Privacy passphrase (None → authNoPriv).
        context_name    (str)   SNMPv3 context name.
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
        # Auto-prepend /mibs and TRAM_MIB_DIR
        import os as _os
        for _d in ["/mibs", _os.environ.get("TRAM_MIB_DIR", "")]:
            if _d and _os.path.isdir(_d) and _d not in self.mib_dirs:
                self.mib_dirs.insert(0, _d)
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
        yield from self._read_raw_udp()

    def _read_raw_udp(self) -> Iterator[tuple[bytes, dict]]:
        """Raw UDP socket receiver — decodes trap bytes and yields per-trap records."""
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

                # Optional MIB-based OID resolution
                if self.resolve_oids and (self.mib_dirs or self.mib_modules):
                    try:
                        from tram.connectors.snmp.mib_utils import (
                            get_mib_view, resolve_oid, oid_str_to_tuple,
                        )
                        mib_view = get_mib_view(self.mib_dirs, self.mib_modules)
                        bindings = {
                            resolve_oid(mib_view, oid_str_to_tuple(oid)): val
                            for oid, val in raw_bindings.items()
                            if not oid.startswith("_")
                        }
                        if "_raw" in raw_bindings:
                            bindings["_raw"] = raw_bindings["_raw"]
                    except Exception as _exc:
                        logger.warning("MIB OID resolution failed for trap: %s", _exc)
                        bindings = raw_bindings
                else:
                    bindings = raw_bindings

                meta = {
                    "source_ip": source_ip,
                    "port": src_port,
                    "community": self.community,
                    "version": self.version,
                }
                yield json.dumps(bindings).encode("utf-8"), meta
                logger.debug(
                    "SNMP trap received",
                    extra={"source_ip": source_ip, "bindings": len(bindings)},
                )
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _decode_trap(self, raw: bytes) -> dict:
        """Decode a raw SNMP trap PDU using pyasn1 BER decoder + pysnmp proto API."""
        try:
            from pyasn1.codec.ber import decoder as ber_decoder
            from pysnmp.proto.api import v2c as pMod
            msg, _ = ber_decoder.decode(raw, asn1Spec=pMod.Message())
            reqPDU = pMod.apiMessage.getPDU(msg)
            bindings: dict = {}
            for oid, val in pMod.apiPDU.getVarBinds(reqPDU):
                bindings[str(oid)] = str(val)
            return bindings
        except Exception:
            # Fall back to hex representation for undecodable packets
            return {"_raw": raw.hex()}


@register_source("snmp_poll")
class SNMPPollSource(BaseSource):
    """Poll an SNMP agent (GET or WALK) using SNMPv1, v2c, or v3.

    Each run issues the configured GET or WALK operation and yields one or more
    ``(json_bytes, meta)`` tuples.  Every record contains ``_polled_at`` (UTC
    ISO8601).  Set ``yield_rows=True`` to receive one record per table row.

    Requires ``pysnmp-lextudio>=6.2,<6.3`` with ``pyasn1>=0.4.8,<0.6``
    (``pip install tram[snmp]``).

    Config keys:
        host            (str, required)        SNMP agent hostname or IP.
        port            (int, default 161)     SNMP agent port.
        community       (str, default "public") Community string (v1/v2c).
        version         (str, default "2c")    "1", "2c", or "3".
        oids            (list[str], required)  OIDs to GET or WALK.
        operation       (str, default "get")   "get" or "walk".
        timeout         (float, default 1.0)   Per-request timeout in seconds.
        retries         (int, default 5)       Number of retries per request.
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
        self.timeout: float = float(config.get("timeout", 1.0))
        self.retries: int = int(config.get("retries", 5))
        self.mib_dirs: list[str] = list(config.get("mib_dirs", []))
        self.mib_modules: list[str] = list(config.get("mib_modules", []))
        self.resolve_oids: bool = bool(config.get("resolve_oids", True))
        self.yield_rows: bool = bool(config.get("yield_rows", False))
        self.index_depth: int = int(config.get("index_depth", 0))
        # Auto-prepend /mibs and TRAM_MIB_DIR
        import os as _os
        for _d in ["/mibs", _os.environ.get("TRAM_MIB_DIR", "")]:
            if _d and _os.path.isdir(_d) and _d not in self.mib_dirs:
                self.mib_dirs.insert(0, _d)
        # SNMPv3 USM
        self.security_name: str = config.get("security_name", "")
        self.auth_protocol: str = config.get("auth_protocol", "SHA")
        self.auth_key: str | None = config.get("auth_key")
        self.priv_protocol: str = config.get("priv_protocol", "AES128")
        self.priv_key: str | None = config.get("priv_key")
        self.context_name: str = config.get("context_name", "")

    @staticmethod
    def _group_by_index(bindings: dict, index_depth: int) -> list[dict]:
        """Group flat ``{oid_key: value}`` bindings into per-row dicts.

        Each key is split into a *column* name and a *row index*:

        * ``index_depth == 0`` (auto) — split on the **first dot**.
          Works correctly for MIB-resolved names such as ``ifDescr.1`` or
          ``atPhysAddress.1.192.168.1.1``.

        * ``index_depth > 0`` — last *N* OID components form the index.
          Use this for numeric OIDs when you know the table instance depth
          (e.g. ``index_depth=4`` for single IPv4-addressed rows).

        The returned list contains one dict per unique index value.  Each
        dict carries ``_index`` (dot-separated string) and ``_index_parts``
        (list of strings) alongside the column values.
        """
        rows: dict[str, dict] = {}
        for key, val in bindings.items():
            if index_depth == 0:
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

        return [rows[k] for k in sorted(rows)]

    def _build_auth(self, hlapi_mod):
        """Build CommunityData or UsmUserData depending on SNMP version."""
        if self.version == "3":
            from tram.connectors.snmp.mib_utils import build_v3_auth
            return build_v3_auth(
                hlapi_mod,
                security_name=self.security_name,
                auth_protocol=self.auth_protocol,
                auth_key=self.auth_key,
                priv_protocol=self.priv_protocol,
                priv_key=self.priv_key,
            )
        mp_model = 0 if self.version == "1" else 1
        return hlapi_mod.CommunityData(self.community, mpModel=mp_model)

    async def _do_get(self, hlapi_mod) -> dict:
        engine = hlapi_mod.SnmpEngine()
        auth_data = self._build_auth(hlapi_mod)
        target = hlapi_mod.UdpTransportTarget(
            (self.host, self.port),
            timeout=self.timeout,
            retries=self.retries,
        )
        context = (
            hlapi_mod.ContextData(contextName=self.context_name)
            if self.context_name
            else hlapi_mod.ContextData()
        )
        var_bind_objs = [
            hlapi_mod.ObjectType(hlapi_mod.ObjectIdentity(oid))
            for oid in self.oids
        ]
        errInd, errStatus, errIdx, varBinds = await hlapi_mod.getCmd(
            engine, auth_data, target, context,
            *var_bind_objs,
            lookupMib=False,
        )
        if errInd:
            raise SourceError(f"SNMP GET error: {errInd}")
        if errStatus:
            raise SourceError(
                f"SNMP GET PDU error: {errStatus.prettyPrint()} "
                f"at {errIdx and varBinds[int(errIdx) - 1][0] or '?'}"
            )
        return {str(oid): str(val) for oid, val in varBinds}

    async def _do_walk(self, hlapi_mod) -> dict:
        engine = hlapi_mod.SnmpEngine()
        auth_data = self._build_auth(hlapi_mod)
        target = hlapi_mod.UdpTransportTarget(
            (self.host, self.port),
            timeout=self.timeout,
            retries=self.retries,
        )
        context = (
            hlapi_mod.ContextData(contextName=self.context_name)
            if self.context_name
            else hlapi_mod.ContextData()
        )
        bindings: dict = {}
        for base_oid in self.oids:
            current_oid = base_oid
            while True:
                errInd, errStatus, errIdx, varBinds = await hlapi_mod.nextCmd(
                    engine, auth_data, target, context,
                    hlapi_mod.ObjectType(hlapi_mod.ObjectIdentity(current_oid)),
                    lookupMib=False,
                )
                if errInd or errStatus or not varBinds:
                    break
                # varBinds from nextCmd is list-of-list: [[( oid, val ), ...]]
                row = varBinds[0] if isinstance(varBinds[0], list) else varBinds
                stop = False
                for oid_obj, val_obj in row:
                    oid_str = str(oid_obj)
                    # Stop when we leave the subtree (lexicographic boundary)
                    if not oid_str.startswith(base_oid.rstrip(".0")):
                        stop = True
                        break
                    bindings[oid_str] = str(val_obj)
                    current_oid = oid_str
                if stop:
                    break
        return bindings

    def read(self) -> Iterator[tuple[bytes, dict]]:
        import json
        try:
            import pysnmp.hlapi.asyncio as _hlapi
        except Exception as exc:
            raise SourceError(
                "SNMP poll source requires pysnmp-lextudio — install with: pip install tram[snmp]"
            ) from exc

        polled_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        try:
            if self.operation == "get":
                bindings = asyncio.run(self._do_get(_hlapi))
            elif self.operation == "walk":
                bindings = asyncio.run(self._do_walk(_hlapi))
            else:
                raise SourceError(
                    f"SNMP poll: unsupported operation '{self.operation}' (use get or walk)"
                )
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"SNMP poll failed: {exc}") from exc

        # Optional MIB-based OID resolution
        if self.resolve_oids and (self.mib_dirs or self.mib_modules):
            try:
                from tram.connectors.snmp.mib_utils import (
                    get_mib_view, resolve_oid, oid_str_to_tuple,
                )
                mib_view = get_mib_view(self.mib_dirs, self.mib_modules)
                bindings = {
                    resolve_oid(mib_view, oid_str_to_tuple(oid)): val
                    for oid, val in bindings.items()
                }
            except Exception as _exc:
                logger.warning("MIB OID resolution failed for poll: %s", _exc)

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
