"""SNMP trap sink connector — sends SNMP traps to a target NMS.

v1.5.0 (GH #72): dual-stack. ``TRAM_SNMP_STACK=trishul`` sends via tsmp
(V1/V2c/V3 notifiers, full USM auth/priv matrix); the default ``legacy``
path is the byte-identical pysnmp implementation. The varbind building and
config surface are shared — only the wire layer differs, and the flag-off
branch is deleted wholesale after the flag period.
"""

from __future__ import annotations

import asyncio
import json
import logging
import warnings

from tram.connectors.config_utils import (
    cfg_float,
    cfg_int,
    cfg_list,
    cfg_str,
    prepend_system_mib_dirs,
    snmpv3_usm,
)
from tram.connectors.snmp.mib_utils import build_tsmp_local_engine, snmp_stack
from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)

# RFC 2576 §3.2 v2→v1 trap mapping — mirrors pysnmp's proxy rfc2576.v2_to_v1
# so a v1 trap emitted by the tsmp sink carries the same Trap-PDU fields the
# legacy path produces. Standard notification OIDs map to generic traps 0-5
# under the snmpTraps enterprise root; enterprise-specific OIDs split into
# enterprise + specific-trap with generic=6 (an RFC 2576-conformant NMS
# reconstructs the trap OID as enterprise.specific).
_STANDARD_TRAP_OIDS: dict[tuple[int, ...], int] = {
    (1, 3, 6, 1, 6, 3, 1, 1, 5, 1): 0,  # coldStart
    (1, 3, 6, 1, 6, 3, 1, 1, 5, 2): 1,  # warmStart
    (1, 3, 6, 1, 6, 3, 1, 1, 5, 3): 2,  # linkDown
    (1, 3, 6, 1, 6, 3, 1, 1, 5, 4): 3,  # linkUp
    (1, 3, 6, 1, 6, 3, 1, 1, 5, 5): 4,  # authenticationFailure
    (1, 3, 6, 1, 6, 3, 1, 1, 5, 6): 5,  # egpNeighborLoss
}
_SNMP_TRAPS_ROOT = (1, 3, 6, 1, 6, 3, 1, 1, 5)
_SYS_UPTIME_INSTANCE_OID = (1, 3, 6, 1, 2, 1, 1, 3, 0)
_SNMP_TRAP_OID_INSTANCE_OID = (1, 3, 6, 1, 6, 3, 1, 1, 4, 1, 0)


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
        priv_protocol   (str)   AES | AES128 | AES192 | AES256 | 3DES
                        (3DES-EDE supported again in v1.5.1; DES rejected at
                        validation).
        priv_key        (str)   Privacy passphrase (None → authNoPriv).
        context_name    (str)   SNMPv3 context name.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config["host"]
        self.port: int = cfg_int(config, "port", 162)
        self.community: str = config.get("community", "public")
        self.version: str = cfg_str(config, "version", "2c")
        self.trap_oid: str = config.get("trap_oid") or config.get("enterprise_oid", "1.3.6.1.4.1.0")
        self.timeout: float = cfg_float(config, "timeout", 1.0)
        self.retries: int = cfg_int(config, "retries", 5)
        self.mib_dirs: list[str] = prepend_system_mib_dirs(cfg_list(config, "mib_dirs"))
        self.mib_modules: list[str] = cfg_list(config, "mib_modules")
        self.varbinds: list[dict] = cfg_list(config, "varbinds")
        # SNMPv3 USM (review E4 — shared helper)
        usm = snmpv3_usm(config)
        self.security_name: str = usm["security_name"]
        self.auth_protocol: str = usm["auth_protocol"]
        self.auth_key: str | None = usm["auth_key"]
        self.priv_protocol: str = usm["priv_protocol"]
        self.priv_key: str | None = usm["priv_key"]
        self.context_name: str = usm["context_name"]
        # v1.5.0 (GH #72): wire stack selected by TRAM_SNMP_STACK — the same
        # YAML config, a different library under it.
        self._snmp_stack: str = snmp_stack()

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

    # ── tsmp wire layer (v1.5.0 flag-on) ────────────────────────────────────

    # Config type names → trishul_snmp value classes (the value shapes differ
    # per type, so the classes are used individually, not through one map).
    _TSMP_TYPE_BUILDERS = {
        "Integer32": "integer",
        "OctetString": "octet-string",
        "Counter32": "counter32",
        "Counter64": "counter64",
        "Gauge32": "gauge32",
        "TimeTicks": "timeticks",
        "IpAddress": "ip-address",
        "ObjectIdentifier": "object-identifier",
        "Opaque": "opaque",
    }

    @staticmethod
    def _build_tsmp_value(type_name: str, val):
        """Build a tsmp SnmpValue from a config type name + raw value."""
        from trishul_snmp import (
            Counter32Value,
            Counter64Value,
            Gauge32Value,
            IntegerValue,
            IpAddressValue,
            ObjectIdentifierValue,
            OctetStringValue,
            OpaqueValue,
            TimeTicksValue,
        )

        kind = SNMPTrapSink._TSMP_TYPE_BUILDERS.get(type_name, "octet-string")
        if kind == "integer":
            return IntegerValue(int(val))
        if kind == "counter32":
            return Counter32Value(int(val))
        if kind == "counter64":
            return Counter64Value(int(val))
        if kind == "gauge32":
            return Gauge32Value(int(val))
        if kind == "timeticks":
            return TimeTicksValue(int(val))
        if kind == "ip-address":
            return IpAddressValue(str(val))
        if kind == "object-identifier":
            return ObjectIdentifierValue(tuple(int(x) for x in str(val).strip(".").split(".")))
        if kind == "opaque":
            raw = val if isinstance(val, bytes) else bytes.fromhex(str(val).removeprefix("0x"))
            return OpaqueValue(raw)
        return OctetStringValue(str(val).encode("utf-8"))

    def _build_var_binds_tsmp(self, bindings_raw: dict) -> list:
        """Build tsmp ``(oid, value)`` varbind inputs, mirroring the legacy spec."""
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

                if "::" in oid_str or (oid_str and not oid_str[0].isdigit()):
                    if mib_view is not None:
                        from tram.connectors.snmp.mib_utils import symbolic_to_oid
                        resolved = symbolic_to_oid(mib_view, oid_str)
                        if resolved:
                            oid_str = ".".join(str(x) for x in resolved)

                try:
                    var_binds.append((oid_str, self._build_tsmp_value(type_name, val)))
                except Exception as exc:
                    logger.warning("Skipping varbind %s=%s: %s", oid_str, val, exc)
        else:
            for oid, val in bindings_raw.items():
                try:
                    if isinstance(val, int):
                        var_binds.append((oid, self._build_tsmp_value("Integer32", val)))
                    else:
                        var_binds.append((oid, self._build_tsmp_value("OctetString", val)))
                except Exception as exc:
                    logger.warning("Skipping invalid OID binding %s=%s: %s", oid, val, exc)

        return var_binds

    async def _send_trap_tsmp(self, bindings_raw: dict) -> None:
        """Send one trap via tsmp notifiers (v1.5.0 flag-on path).

        The notifiers auto-build the sysUpTime.0 + snmpTrapOID.0 varbinds
        (v2c/v3) and the v1 Trap-PDU enterprise/timestamp fields, so only the
        payload varbinds are passed — same wire semantics as the legacy
        mandatory-varbind construction.
        """
        import time as _time

        from tram.connectors.snmp.mib_utils import build_v3_usm_user

        uptime_ticks = int(_time.monotonic() * 100)

        common = {
            "host": self.host,
            "port": self.port,
            "timeout": self.timeout,
            "retries": self.retries,
        }
        var_binds = self._build_var_binds_tsmp(bindings_raw)

        if self.version == "3":
            from trishul_snmp import V3Notifier

            user = build_v3_usm_user(
                security_name=self.security_name,
                auth_protocol=self.auth_protocol,
                auth_key=self.auth_key,
                priv_protocol=self.priv_protocol,
                priv_key=self.priv_key,
            )
            local_engine = build_tsmp_local_engine(
                f"tram:sink:{self.host}:{self.port}:{self.security_name}"
            )
            async with V3Notifier(
                user=user,
                context_name=self.context_name.encode("utf-8"),
                local_engine=local_engine,
                **common,
            ) as notifier:
                await notifier.send_trap(self.trap_oid, varbinds=var_binds, uptime=uptime_ticks)
        elif self.version == "1":
            from trishul_snmp import V1Notifier

            from tram.connectors.snmp.mib_utils import oid_str_to_tuple

            # RFC 2576 §3.2 v2→v1 mapping (parity with pysnmp's proxy
            # v2_to_v1): the trap OID becomes the Trap-PDU's
            # enterprise/generic/specific fields — never the raw OID as the
            # enterprise, which would mis-encode an enterprise-specific trap.
            trap_oid_tuple = oid_str_to_tuple(self.trap_oid)
            if trap_oid_tuple in _STANDARD_TRAP_OIDS:
                enterprise = _SNMP_TRAPS_ROOT
                generic_trap = _STANDARD_TRAP_OIDS[trap_oid_tuple]
                specific_trap = 0
            else:
                # Strip a trailing ".0" separator arc when present (pysnmp's
                # v2_to_v1: ``[-2] == 0`` → drop two arcs).
                if trap_oid_tuple[-2] == 0:
                    enterprise = trap_oid_tuple[:-2]
                else:
                    enterprise = trap_oid_tuple[:-1]
                generic_trap = 6
                specific_trap = trap_oid_tuple[-1]
            # The v1 Trap-PDU carries sysUpTime + enterprise/generic/specific
            # in its header — drop the v2c-style mandatory varbinds (tsmp
            # auto-prepends sysUpTime.0 to the v1 varbind list).
            v1_varbinds = [
                (oid, val) for oid, val in var_binds
                if oid_str_to_tuple(oid) not in (
                    _SYS_UPTIME_INSTANCE_OID,
                    _SNMP_TRAP_OID_INSTANCE_OID,
                )
            ]
            async with V1Notifier(community=self.community, **common) as notifier:
                await notifier.send_trap(
                    enterprise,
                    agent_addr="0.0.0.0",
                    generic_trap=generic_trap,
                    specific_trap=specific_trap,
                    timestamp=uptime_ticks,
                    varbinds=v1_varbinds,
                )
        else:
            from trishul_snmp import V2cNotifier

            async with V2cNotifier(community=self.community, **common) as notifier:
                await notifier.send_trap(self.trap_oid, varbinds=var_binds, uptime=uptime_ticks)

    async def _send_trap(self, hlapi_mod, bindings_raw: dict) -> None:
        if self._snmp_stack == "trishul":
            await self._send_trap_tsmp(bindings_raw)
            return
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
                *mandatory_vbs,
                *var_binds,
            )
            if errInd:
                raise SinkError(f"SNMP trap send error: {errInd}")
            if errStatus:
                raise SinkError(f"SNMP trap PDU error: {errStatus.prettyPrint()}")
        finally:
            close_snmp_engine(engine)

    def write(self, data: bytes, meta: dict) -> None:
        if self._snmp_stack == "trishul":
            # tsmp path (v1.5.0 flag-on): no pysnmp requirement.
            _hlapi = None
        else:
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
