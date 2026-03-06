"""SNMP MIB utilities — build MIB view, resolve OIDs, symbolic name lookup.

Requires ``pysnmp-lextudio`` (``pip install tram[snmp]``).
Raw .mib compilation requires ``pysmi-lextudio`` (``pip install tram[mib]``).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)


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
    except ImportError:
        logger.warning("pysnmp-lextudio not installed — MIB resolution unavailable")
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
        from pysnmp.smi.rfc1902 import ObjectIdentity
        from pyasn1.type.univ import ObjectIdentifier

        # Handle "MODULE::name.index" format
        if "::" in symbolic:
            module, rest = symbolic.split("::", 1)
            parts = rest.split(".")
            sym_name = parts[0]
            indices = [int(x) for x in parts[1:]] if len(parts) > 1 else []
            oid_obj, _, _ = mib_view.getNodeName((module, sym_name))
        else:
            parts = symbolic.split(".")
            sym_name = parts[0]
            indices = [int(x) for x in parts[1:]] if len(parts) > 1 else []
            oid_obj, _, _ = mib_view.getNodeName(sym_name)

        result = tuple(oid_obj) + tuple(indices)
        return result
    except Exception as exc:
        logger.debug("Could not resolve symbolic OID %r: %s", symbolic, exc)
        return None
