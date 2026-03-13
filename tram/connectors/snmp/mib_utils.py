"""SNMP MIB utilities — build MIB view, resolve OIDs, symbolic name lookup,
and SNMPv3 USM authentication builder.

Requires ``pysnmp-lextudio`` (``pip install tram[snmp]``).
Raw .mib compilation requires ``pysmi-lextudio`` (``pip install tram[mib]``).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)


# ── SNMPv3 USM auth builder ─────────────────────────────────────────────────

# Maps human-readable protocol strings → pysnmp.hlapi attribute names.
# Looked up via getattr(hlapi, name) at call time — keeps this module
# importable without pysnmp installed.

_AUTH_PROTO_NAMES: dict[str, str] = {
    "MD5":    "usmHMACMD5AuthProtocol",
    "SHA":    "usmHMACSHAAuthProtocol",      # SHA-1 / HMAC-96
    "SHA224": "usmHMAC128SHA224AuthProtocol",
    "SHA256": "usmHMAC192SHA256AuthProtocol",
    "SHA384": "usmHMAC256SHA384AuthProtocol",
    "SHA512": "usmHMAC384SHA512AuthProtocol",
}

_PRIV_PROTO_NAMES: dict[str, str] = {
    "DES":    "usmDESPrivProtocol",
    "3DES":   "usm3DESEDEPrivProtocol",
    "AES":    "usmAesCfb128Protocol",    # alias for AES-128
    "AES128": "usmAesCfb128Protocol",
    "AES192": "usmAesCfb192Protocol",
    "AES256": "usmAesCfb256Protocol",
}


def build_v3_auth(
    hlapi,
    security_name: str,
    auth_protocol: str = "SHA",
    auth_key: Optional[str] = None,
    priv_protocol: str = "AES128",
    priv_key: Optional[str] = None,
):
    """Build a ``UsmUserData`` object for SNMPv3 USM authentication.

    Security level is auto-detected from the supplied credentials:

    * no ``auth_key``              → **noAuthNoPriv** (username only)
    * ``auth_key`` only            → **authNoPriv**
    * ``auth_key`` + ``priv_key``  → **authPriv**

    Args:
        hlapi:          The ``pysnmp.hlapi`` module (passed in to avoid a
                        top-level import; allows this module to stay importable
                        when pysnmp is not installed).
        security_name:  USM username.
        auth_protocol:  Auth algorithm — MD5 | SHA | SHA224 | SHA256 | SHA384 | SHA512.
                        Defaults to SHA.  Unknown values fall back to SHA.
        auth_key:       Auth passphrase.  ``None`` → noAuthNoPriv.
        priv_protocol:  Privacy algorithm — DES | 3DES | AES | AES128 | AES192 | AES256.
                        Defaults to AES128.  Unknown values fall back to AES128.
        priv_key:       Privacy passphrase.  ``None`` → authNoPriv (when auth_key set).

    Returns:
        Configured ``UsmUserData`` instance ready to pass to pysnmp hlapi calls.
    """
    kwargs: dict = {"userName": security_name}

    if auth_key:
        auth_proto_attr = _AUTH_PROTO_NAMES.get(
            auth_protocol.upper(), "usmHMACSHAAuthProtocol"
        )
        auth_proto = getattr(hlapi, auth_proto_attr, None)
        kwargs["authKey"] = auth_key
        if auth_proto is not None:
            kwargs["authProtocol"] = auth_proto

        if priv_key:
            priv_proto_attr = _PRIV_PROTO_NAMES.get(
                priv_protocol.upper(), "usmAesCfb128Protocol"
            )
            priv_proto = getattr(hlapi, priv_proto_attr, None)
            kwargs["privKey"] = priv_key
            if priv_proto is not None:
                kwargs["privProtocol"] = priv_proto

    return hlapi.UsmUserData(**kwargs)


def build_mib_view(mib_dirs: list[str], mib_modules: list[str]):
    """Create a pysnmp MIB view controller loading standard + custom MIBs.

    Args:
        mib_dirs: Paths to directories containing compiled MIB .py files.
        mib_modules: MIB module names to load, e.g. ["IF-MIB", "SNMPv2-MIB"].

    Returns:
        MibViewController instance, or None if pysnmp is not installed.
    """
    try:
        from pysnmp.smi import builder, view
    except Exception:
        logger.warning("pysnmp not available or incompatible — MIB resolution unavailable")
        return None

    mib_builder = builder.MibBuilder()

    # Add custom MIB directories (prepend so they take priority)
    if mib_dirs:
        existing = mib_builder.getMibSources()
        custom_sources = tuple(
            builder.DirMibSource(d) for d in mib_dirs
        )
        mib_builder.setMibSources(*custom_sources, *existing)

    # Load requested MIB modules (plus always-needed base MIBs)
    base_modules = ("SNMPv2-SMI", "SNMPv2-MIB", "SNMPv2-TC", "SNMPv2-CONF")
    all_modules = list(base_modules) + [m for m in mib_modules if m not in base_modules]

    for mod in all_modules:
        try:
            mib_builder.loadModules(mod)
        except Exception as exc:
            logger.debug("Could not load MIB module %s: %s", mod, exc)

    return view.MibViewController(mib_builder)


@lru_cache(maxsize=512)
def _cached_mib_view(mib_dirs_key: tuple[str, ...], mib_modules_key: tuple[str, ...]):
    """Cache MIB views per (dirs, modules) combination."""
    return build_mib_view(list(mib_dirs_key), list(mib_modules_key))


def get_mib_view(mib_dirs: list[str], mib_modules: list[str]):
    """Return a cached MIB view for the given dirs + modules."""
    return _cached_mib_view(tuple(sorted(mib_dirs)), tuple(sorted(mib_modules)))


def resolve_oid(mib_view, oid_tuple: tuple) -> str:
    """Resolve a numeric OID tuple to a symbolic name.

    Args:
        mib_view: MibViewController from build_mib_view().
        oid_tuple: Numeric OID as tuple of ints, e.g. (1, 3, 6, 1, 2, 1, 1, 1, 0).

    Returns:
        Symbolic string like "sysDescr" or dotted-decimal string as fallback.
    """
    if mib_view is None:
        return ".".join(str(x) for x in oid_tuple)

    try:
        from pysnmp.smi.rfc1902 import ObjectIdentity
        from pyasn1.type.univ import ObjectIdentifier

        oid_obj = ObjectIdentifier(oid_tuple)
        mod_name, sym_name, indices = mib_view.getNodeLocation(oid_obj)
        if indices:
            idx_str = "." + ".".join(str(i) for i in indices)
        else:
            idx_str = ""
        return f"{sym_name}{idx_str}"
    except Exception:
        return ".".join(str(x) for x in oid_tuple)


def oid_str_to_tuple(oid_str: str) -> tuple[int, ...]:
    """Convert dotted-decimal OID string to tuple of ints."""
    return tuple(int(x) for x in oid_str.strip(".").split("."))


def symbolic_to_oid(mib_view, symbolic: str) -> Optional[tuple[int, ...]]:
    """Resolve a symbolic OID name to a numeric tuple.

    Args:
        mib_view: MibViewController from build_mib_view().
        symbolic: Symbolic name like "IF-MIB::ifOperStatus.1" or "sysDescr.0".

    Returns:
        Tuple of ints, or None if resolution fails.
    """
    if mib_view is None:
        return None

    try:
        # Handle "MODULE::name.index" format (e.g. "IF-MIB::ifDescr.1")
        if "::" in symbolic:
            module, rest = symbolic.split("::", 1)
            parts = rest.split(".")
            sym_name = parts[0]
            indices = [int(x) for x in parts[1:]] if len(parts) > 1 else []
            # getNodeName((sym_name,), moduleName) in pysnmp-lextudio 6.x
            oid_obj, _, _ = mib_view.getNodeName((sym_name,), module)
        else:
            parts = symbolic.split(".")
            sym_name = parts[0]
            indices = [int(x) for x in parts[1:]] if len(parts) > 1 else []
            oid_obj, _, _ = mib_view.getNodeName((sym_name,))

        result = tuple(oid_obj) + tuple(indices)
        return result
    except Exception as exc:
        logger.debug("Could not resolve symbolic OID %r: %s", symbolic, exc)
        return None
