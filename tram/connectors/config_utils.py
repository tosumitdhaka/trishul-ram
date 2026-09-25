"""Small typed helpers for connector ``__init__`` config extraction (review E4).

Connector classes hand-parse the validated config dict with
``int(config.get(...))`` / ``bool(config.get(...))`` casts. These helpers make
the casts read consistently and give the repeated SNMP blocks (SNMPv3 USM
fields, MIB-dir auto-prepend) a single home instead of verbatim copies.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def cfg_str(config: Mapping[str, Any], key: str, default: str) -> str:
    """``str(config.get(key, default))`` — the connector boilerplate verbatim."""
    return str(config.get(key, default))


def cfg_int(config: Mapping[str, Any], key: str, default: int) -> int:
    """``int(config.get(key, default))`` — the connector boilerplate verbatim."""
    return int(config.get(key, default))


def cfg_float(config: Mapping[str, Any], key: str, default: float) -> float:
    """``float(config.get(key, default))`` — the connector boilerplate verbatim."""
    return float(config.get(key, default))


def cfg_bool(config: Mapping[str, Any], key: str, default: bool) -> bool:
    """``bool(config.get(key, default))`` — the connector boilerplate verbatim."""
    return bool(config.get(key, default))


def cfg_list(config: Mapping[str, Any], key: str, default: list | None = None) -> list:
    """``list(config.get(key, default))`` — the connector boilerplate verbatim."""
    return list(config.get(key, default if default is not None else []))


def snmpv3_usm(config: Mapping[str, Any]) -> dict[str, str | None]:
    """Extract the SNMPv3 USM field block.

    Previously duplicated verbatim in both SNMP sources and the SNMP sink
    (review E4). Keys match the config schema: auth/priv None → noAuthNoPriv /
    authNoPriv at runtime.
    """
    return {
        "security_name": config.get("security_name", ""),
        "auth_protocol": config.get("auth_protocol", "SHA"),
        "auth_key": config.get("auth_key"),
        "priv_protocol": config.get("priv_protocol", "AES128"),
        "priv_key": config.get("priv_key"),
        "context_name": config.get("context_name", ""),
    }


def prepend_system_mib_dirs(mib_dirs: list[str]) -> list[str]:
    """Auto-prepend ``/mibs`` and ``TRAM_MIB_DIR`` to *mib_dirs* in place.

    Previously duplicated verbatim in both SNMP source classes (review E4);
    only directories that exist on this host and are not already listed are
    prepended (so the first entry wins).
    """
    for candidate in ["/mibs", os.environ.get("TRAM_MIB_DIR", "")]:
        if candidate and os.path.isdir(candidate) and candidate not in mib_dirs:
            mib_dirs.insert(0, candidate)
    return mib_dirs