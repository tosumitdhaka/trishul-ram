"""SNMP trap sink connector — sends SNMP traps to a target NMS."""

from __future__ import annotations

import asyncio
import json
import logging
import warnings

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


@register_sink("snmp_trap")
class SNMPTrapSink(BaseSink):
    """Send SNMP v1/v2c/v3 Trap/InformRequest to a target NMS/trap receiver.

    Expects the input ``data`` to be a JSON-encoded dict of OID → value bindings.
    Each key-value pair becomes a VarBind in the outgoing trap PDU.

    Requires the ``tram[snmp]`` optional extra.

    Config keys:
        host            (str, required)         Target NMS hostname or IP.
        port            (int, default 162)       UDP trap port.
        community       (str, default "public")  SNMP v1/v2c community string.
        version         (str, default "2c")      "1", "2c", or "3".
        trap_oid        (str, default "1.3.6.1.4.1.0")  Notification OID.
                        Legacy alias: ``enterprise_oid`` is still accepted.
        timeout         (float, default 1.0)     Request timeout seconds.
        retries         (int, default 5)         Request retries.
        varbinds        (list[dict])             Explicit varbind spec; each entry:
                                                   oid, value_field, type
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
        self.trap_oid: str = config.get("trap_oid") or config.get("enterprise_oid", "1.3.6.1.4.1.0")
        self.timeout: float = float(config.get("timeout", 1.0))
        self.retries: int = int(config.get("retries", 5))
        self.mib_dirs: list[str] = list(config.get("mib_dirs", []))
        self.mib_modules: list[str] = list(config.get("mib_modules", []))
        self.varbinds: list[dict] = list(config.get("varbinds", []))
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

    def _build_var_binds(self, hlapi_mod, bindings_raw: dict) -> list:
        """Build ObjectType varbind list from the record dict."""
        _type_map = {
            "Integer32": hlapi_mod.Integer32,
            "OctetString": hlapi_mod.OctetString,
        }
        for name in ("Counter32", "Gauge32", "TimeTicks"):
            cls = getattr(hlapi_mod, name, None)
            if cls:
                _type_map[name] = cls

        var_binds = []

        if self.varbinds:
            mib_view = None
            if self.mib_dirs or self.mib_modules:
                try:
                    from tram.connectors.snmp.mib_utils import get_mib_view
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

                # Resolve symbolic OID (e.g. "IF-MIB::ifDescr.1") to numeric
                if "::" in oid_str or (oid_str and not oid_str[0].isdigit()):
                    if mib_view is not None:
                        from tram.connectors.snmp.mib_utils import symbolic_to_oid
                        resolved = symbolic_to_oid(mib_view, oid_str)
                        if resolved:
                            oid_str = ".".join(str(x) for x in resolved)

                try:
                    snmp_type_cls = _type_map.get(type_name, hlapi_mod.OctetString)
                    var_binds.append(
                        hlapi_mod.ObjectType(
                            hlapi_mod.ObjectIdentity(oid_str),
                            snmp_type_cls(val),
                        )
                    )
                except Exception as exc:
                    logger.warning("Skipping varbind %s=%s: %s", oid_str, val, exc)
        else:
            # Auto-type from raw dict
            for oid, val in bindings_raw.items():
                try:
                    if isinstance(val, int):
                        var_binds.append(
                            hlapi_mod.ObjectType(
                                hlapi_mod.ObjectIdentity(oid),
                                hlapi_mod.Integer32(val),
                            )
                        )
                    else:
                        var_binds.append(
                            hlapi_mod.ObjectType(
                                hlapi_mod.ObjectIdentity(oid),
                                hlapi_mod.OctetString(str(val)),
                            )
                        )
                except Exception as exc:
                    logger.warning("Skipping invalid OID binding %s=%s: %s", oid, val, exc)

        return var_binds

    async def _send_trap(self, hlapi_mod, bindings_raw: dict) -> None:
        from tram.connectors.snmp.mib_utils import (
            build_v3_auth,
            close_snmp_engine,
            create_udp_transport_target,
            hlapi_send_notification,
        )
        engine = hlapi_mod.SnmpEngine()
        try:
            if self.version == "3":
                auth_data = build_v3_auth(
                    hlapi_mod,
                    security_name=self.security_name,
                    auth_protocol=self.auth_protocol,
                    auth_key=self.auth_key,
                    priv_protocol=self.priv_protocol,
                    priv_key=self.priv_key,
                )
            else:
                mp_model = 0 if self.version == "1" else 1
                auth_data = hlapi_mod.CommunityData(self.community, mpModel=mp_model)

            target = await create_udp_transport_target(
                hlapi_mod,
                host=self.host,
                port=self.port,
                timeout=self.timeout,
                retries=self.retries,
            )
            context = (
                hlapi_mod.ContextData(contextName=self.context_name)
                if self.context_name
                else hlapi_mod.ContextData()
            )

            var_binds = self._build_var_binds(hlapi_mod, bindings_raw)

            # SNMPv2c traps require sysUpTime.0 and snmpTrapOID.0 as the first two
            # varbinds in the PDU. Build these explicitly instead of relying on
            # NotificationType so custom trap OIDs work without MIB lookup.
            import time as _time
            uptime_ticks = int(_time.monotonic() * 100)  # centi-seconds since process start
            mandatory_vbs = [
                hlapi_mod.ObjectType(
                    hlapi_mod.ObjectIdentity("1.3.6.1.2.1.1.3.0"),
                    hlapi_mod.TimeTicks(uptime_ticks),
                ),
                hlapi_mod.ObjectType(
                    hlapi_mod.ObjectIdentity("1.3.6.1.6.3.1.1.4.1.0"),
                    hlapi_mod.ObjectIdentifier(self.trap_oid),
                ),
            ]

            errInd, errStatus, errIdx, _ = await hlapi_send_notification(
                hlapi_mod,
                engine,
                auth_data,
                target,
                context,
                "trap",
                mandatory_vbs + var_binds,
            )
            if errInd:
                raise SinkError(f"SNMP trap send error: {errInd}")
            if errStatus:
                raise SinkError(f"SNMP trap PDU error: {errStatus.prettyPrint()}")
        finally:
            close_snmp_engine(engine)

    def write(self, data: bytes, meta: dict) -> None:
        try:
            from tram.connectors.snmp.mib_utils import get_hlapi_asyncio
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=RuntimeWarning)
                _hlapi = get_hlapi_asyncio()
        except Exception as exc:
            raise SinkError(
                "SNMP trap sink requires pysnmp — install with: pip install tram[snmp]"
            ) from exc

        try:
            payload = json.loads(data)
        except Exception as exc:
            raise SinkError(f"SNMP trap sink: failed to parse data as JSON: {exc}") from exc

        # Accept either a single record dict or a list of record dicts.
        # One trap is sent per record.
        if isinstance(payload, dict):
            records = [payload]
        elif isinstance(payload, list):
            records = payload
        else:
            raise SinkError("SNMP trap sink: expected a JSON object or array")

        for bindings_raw in records:
            if not isinstance(bindings_raw, dict):
                logger.warning("SNMP trap sink: skipping non-dict record: %r", type(bindings_raw))
                continue
            try:
                asyncio.run(self._send_trap(_hlapi, bindings_raw))
            except SinkError:
                raise
            except Exception as exc:
                raise SinkError(
                    f"SNMP trap send failed to {self.host}:{self.port}: {exc}"
                ) from exc
            logger.info(
                "SNMP trap sent",
                extra={
                    "host": self.host,
                    "port": self.port,
                    "trap_oid": self.trap_oid,
                    "bindings": len(bindings_raw),
                },
            )
