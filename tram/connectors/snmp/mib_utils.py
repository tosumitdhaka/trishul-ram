"""SNMP MIB utilities — build MIB view, resolve OIDs, symbolic lookup,
SNMPv3 auth builders, and PySNMP HLAPI compatibility helpers.

v1.5.0 (GH #72): dual-stack. ``TRAM_SNMP_STACK=trishul`` resolves OIDs
against tsmi JSON IR bundles (:class:`trishul_snmp.MibBundle`) instead of
pysmi ``.py`` modules; ``legacy`` (default) keeps the byte-identical pysnmp
path. The two views are cleanly separated behind :func:`get_mib_view` and the
``mibBuilder``-vs-``lookup`` duck-type check in the resolve helpers — after
the flag period the legacy branch is deleted wholesale.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import sys
import time
from collections.abc import Iterator
from functools import lru_cache

logger = logging.getLogger(__name__)


def snmp_stack() -> str:
    """``TRAM_SNMP_STACK`` reader for the connector layer (v1.5.0, GH #72).

    Returns ``"legacy"`` (pysnmp) or ``"trishul"`` (tsmi/tsmp). Delegates to
    the canonical reader in ``tram.core.config`` (same env, same validation —
    invalid values fail loud so a deployment never silently flips its SNMP
    stack). The connectors read this directly so manager and worker processes
    resolve the same stack from the same deployment env.
    """
    from tram.core.config import snmp_stack as _config_snmp_stack

    return _config_snmp_stack()


def _resolve_hlapi_callable(hlapi_mod, *names: str):
    namespace = vars(hlapi_mod) if hasattr(hlapi_mod, "__dict__") else {}
    for name in names:
        if name in namespace:
            candidate = namespace[name]
            if callable(candidate):
                return candidate
    for name in names:
        candidate = getattr(hlapi_mod, name, None)
        if callable(candidate):
            return candidate
    return None


def _get_mib_sources(mib_builder):
    """Return configured MIB sources across pysnmp/pysmi API variants."""
    getter = getattr(mib_builder, "getMibSources", None) or getattr(
        mib_builder, "get_mib_sources", None
    )
    if callable(getter):
        return tuple(getter())
    sources = getattr(mib_builder, "mibSources", None)
    if sources is not None:
        return tuple(sources)
    return ()


def _set_mib_sources(mib_builder, *sources) -> None:
    """Set configured MIB sources across pysnmp/pysmi API variants."""
    setter = getattr(mib_builder, "setMibSources", None) or getattr(
        mib_builder, "set_mib_sources", None
    )
    if callable(setter):
        setter(*sources)
        return
    mib_builder.mibSources = tuple(sources)


def _load_mib_module(mib_builder, module: str) -> None:
    """Load one MIB module across snake_case and camelCase APIs."""
    loader = getattr(mib_builder, "load_modules", None) or getattr(
        mib_builder, "loadModules", None
    )
    if not callable(loader):
        raise AttributeError("MIB builder has no load_modules/loadModules method")
    loader(module)


# ── tsmi JSON-bundle view (v1.5.0 flag-on path) ────────────────────────────


class _TsmiBundleView:
    """MIB view over tsmi JSON IR bundles — the ``TRAM_SNMP_STACK=trishul`` resolve layer.

    Duck-typed to the handful of ``MibViewController`` methods the connector
    layer uses (``lookup``, ``resolve``, ``resolve_node``), backed by one or
    more :class:`trishul_snmp.MibBundle`` instances — one per MIB directory
    that contains JSON bundles, in configured priority order (user dirs listed
    first shadow the system corpus). A raw ``MibBundle`` is also accepted
    (single-directory corpus) — both expose ``lookup`` and never ``mibBuilder``,
    which is how the resolve helpers tell the tsmi path from pysnmp's view.
    """

    def __init__(self, bundles: list) -> None:
        self._bundles: tuple = tuple(bundles)

    def lookup(self, oid):
        """Closest-object match across bundles, or None when nothing matches."""
        for bundle in self._bundles:
            try:
                return bundle.lookup(oid)
            except Exception:
                continue
        return None

    def resolve(self, target: str) -> tuple[int, ...]:
        """Resolve ``MODULE::symbol[.suffix]`` across bundles (first hit wins)."""
        last_exc: Exception | None = None
        for bundle in self._bundles:
            try:
                return tuple(bundle.resolve(target))
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise ValueError(f"Unknown symbolic target: {target}")

    def resolve_node(self, module: str, symbol: str):
        """Exact node lookup across bundles (None when absent everywhere)."""
        for bundle in self._bundles:
            node = bundle.resolve_node(module, symbol)
            if node is not None:
                return node
        return None

    def module_names(self) -> list[str]:
        """Every loaded module name across all bundles (for bare-symbol search)."""
        return [name for bundle in self._bundles for name in bundle.modules]


def _build_tsmi_bundle(mib_dirs: list[str], mib_modules: list[str]):
    """Build a :class:`_TsmiBundleView` from tsmi JSON bundles in *mib_dirs*.

    Mirrors the legacy pysnmi layout: the same ``mib_dirs`` hold the dual-format
    corpus (``MIB.json`` alongside ``MIB.py``), so the flag-on path needs no new
    config surface — it loads every bundle directory that contains JSON files.
    Returns None when ``trishul_snmp`` is not installed or no JSON bundles are
    found (resolution then falls back to numeric OIDs, like the legacy path when
    pysnmp is unavailable).
    """
    try:
        from trishul_snmp import load_bundle
    except Exception:
        logger.warning(
            "trishul_snmp not available — tsmi MIB resolution unavailable "
            "(TRAM_SNMP_STACK=trishul)"
        )
        return None

    bundles = []
    for d in mib_dirs:
        if not os.path.isdir(d):
            continue
        if not any(name.endswith(".json") for name in os.listdir(d)):
            continue
        try:
            bundles.append(load_bundle(d))
        except Exception as exc:
            logger.debug("Could not load tsmi bundle directory %s: %s", d, exc)

    if not bundles:
        logger.debug(
            "No tsmi JSON MIB bundles found in mib_dirs=%s — MIB resolution unavailable",
            mib_dirs,
        )
        return None
    return _TsmiBundleView(bundles)


@lru_cache(maxsize=512)
def _cached_tsmi_view(mib_dirs_key: tuple[str, ...], mib_modules_key: tuple[str, ...]):
    """Cache tsmi bundle views per (dirs, modules) combination."""
    return _build_tsmi_bundle(list(mib_dirs_key), list(mib_modules_key))


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
    auth_key: str | None = None,
    priv_protocol: str = "AES128",
    priv_key: str | None = None,
):
    """Build a ``UsmUserData`` object for SNMPv3 USM authentication.

    Security level is auto-detected from the supplied credentials:

    * no ``auth_key``              → **noAuthNoPriv** (username only)
    * ``auth_key`` only            → **authNoPriv**
    * ``auth_key`` + ``priv_key``  → **authPriv**

    Args:
        hlapi:          The PySNMP HLAPI module (passed in to avoid a
                        top-level import; allows this module to stay importable
                        when pysnmp is not installed).
        security_name:  USM username.
        auth_protocol:  Auth algorithm — MD5 | SHA | SHA224 | SHA256 | SHA384 | SHA512.
                        Defaults to SHA.  Unknown values fall back to SHA.
        auth_key:       Auth passphrase.  ``None`` → noAuthNoPriv.
        priv_protocol:  Privacy algorithm — AES | AES128 | AES192 | AES256
                        (DES/3DES rejected at validation).
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


# ── tsmp USM builders (v1.5.0 flag-on path) ────────────────────────────────

# Human-readable protocol strings → trishul_snmp AuthProtocol/PrivProtocol
# enum member names. Looked up via getattr at call time so this module stays
# importable without trishul_snmp installed. The config surface is identical
# to the legacy builders — the same YAML, a different wire stack under it.
# DES/3DES configs are rejected at validation (v1.5.0 layer 2), so the enum
# members exist here only for completeness.

_TSMP_AUTH_PROTOCOLS: dict[str, str] = {
    "MD5":    "MD5",
    "SHA":    "SHA1",        # SHA-1 / HMAC-96
    "SHA224": "SHA224",
    "SHA256": "SHA256",
    "SHA384": "SHA384",
    "SHA512": "SHA512",
}

_TSMP_PRIV_PROTOCOLS: dict[str, str] = {
    "DES":    "DES",
    "3DES":   "THREEDES_EDE",
    "AES":    "AES128",      # alias for AES-128
    "AES128": "AES128",
    "AES192": "AES192",
    "AES256": "AES256",
}


def build_v3_usm_user(
    security_name: str,
    auth_protocol: str = "SHA",
    auth_key: str | None = None,
    priv_protocol: str = "AES128",
    priv_key: str | None = None,
):
    """Build a ``trishul_snmp.UsmUser`` for the tsmp stack (v1.5.0 flag-on).

    Security level auto-detection mirrors :func:`build_v3_auth`: no
    ``auth_key`` → noAuthNoPriv; ``auth_key`` only → authNoPriv;
    ``auth_key`` + ``priv_key`` → authPriv. Unknown protocol strings fall
    back to SHA / AES128, same as the legacy builder.
    """
    from trishul_snmp import AuthProtocol, PrivProtocol, UsmUser

    if not auth_key:
        return UsmUser(username=security_name)

    auth_name = _TSMP_AUTH_PROTOCOLS.get(auth_protocol.upper(), "SHA1")
    kwargs: dict = {
        "username": security_name,
        "auth_protocol": getattr(AuthProtocol, auth_name),
        "auth_key": str(auth_key).encode("utf-8"),
    }
    if priv_key:
        priv_name = _TSMP_PRIV_PROTOCOLS.get(priv_protocol.upper(), "AES128")
        kwargs["priv_protocol"] = getattr(PrivProtocol, priv_name)
        kwargs["priv_key"] = str(priv_key).encode("utf-8")
    return UsmUser(**kwargs)


def build_tsmp_local_engine(seed: str, *, engine_boots: int = 1):
    """Build a deterministic ``UsmLocalEngine`` for tsmp v3 trap endpoints.

    SNMPv3 traps are sender-authoritative: the trap receiver's engine state
    is whatever the sender last cached, and tsmp listeners answer USM
    discovery probes automatically. A deterministic engine id per endpoint
    (host:port:user) keeps sender caches valid across restarts; boots/time
    are per-process counters.
    """
    from trishul_snmp import UsmLocalEngine

    digest = hashlib.sha1(seed.encode("utf-8")).digest()
    engine_id = b"\x80\x00\x01\x02\x03" + digest[:12]
    return UsmLocalEngine(
        engine_id=engine_id,
        engine_boots=engine_boots,
        engine_time=int(time.monotonic()) % (2**31),
    )


def get_hlapi_asyncio():
    """Import the asyncio HLAPI module across PySNMP 6.x and 7.x layouts."""
    try:
        import pysnmp.hlapi.v3arch.asyncio as hlapi
        return hlapi
    except Exception:
        try:
            import pysnmp.hlapi.asyncio as hlapi
            return hlapi
        except Exception:
            fallback = (
                sys.modules.get("pysnmp.hlapi.v3arch.asyncio")
                or sys.modules.get("pysnmp.hlapi.asyncio")
                or sys.modules.get("pysnmp.hlapi")
            )
            if fallback is not None:
                return fallback
            import pysnmp
            hlapi = getattr(pysnmp, "hlapi", None)
            if hlapi is not None:
                return (
                    getattr(getattr(hlapi, "v3arch", None), "asyncio", None)
                    or getattr(hlapi, "asyncio", None)
                    or hlapi
                )
            raise


async def create_udp_transport_target(hlapi_mod, host: str, port: int, timeout: float, retries: int):
    """Create a UDP target across PySNMP 6.x and 7.x APIs."""
    target_cls = hlapi_mod.UdpTransportTarget
    create = getattr(target_cls, "create", None)
    if callable(create):
        candidate = create((host, port), timeout=timeout, retries=retries)
        if inspect.isawaitable(candidate):
            return await candidate
    return target_cls((host, port), timeout=timeout, retries=retries)


async def hlapi_get_cmd(hlapi_mod, *args, **kwargs):
    fn = _resolve_hlapi_callable(hlapi_mod, "get_cmd", "getCmd")
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, Iterator):
        return next(result)
    return result


async def hlapi_next_cmd(hlapi_mod, *args, **kwargs):
    fn = _resolve_hlapi_callable(hlapi_mod, "next_cmd", "nextCmd")
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, Iterator):
        return next(result)
    return result


async def hlapi_send_notification(hlapi_mod, *args, **kwargs):
    fn = _resolve_hlapi_callable(hlapi_mod, "send_notification", "sendNotification")
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, Iterator):
        return next(result)
    return result


def close_snmp_engine(snmp_engine) -> None:
    """Close dispatcher across PySNMP 6.x/7.x naming."""
    closer = getattr(snmp_engine, "close_dispatcher", None) or getattr(
        snmp_engine, "closeDispatcher", None
    )
    if callable(closer):
        closer()


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
        existing = _get_mib_sources(mib_builder)
        custom_sources = tuple(
            builder.DirMibSource(d) for d in mib_dirs
        )
        _set_mib_sources(mib_builder, *custom_sources, *existing)

    # Load requested MIB modules (plus always-needed base MIBs)
    base_modules = ("SNMPv2-SMI", "SNMPv2-MIB", "SNMPv2-TC", "SNMPv2-CONF")
    all_modules = list(base_modules) + [m for m in mib_modules if m not in base_modules]

    for mod in all_modules:
        try:
            _load_mib_module(mib_builder, mod)
        except Exception as exc:
            logger.debug("Could not load MIB module %s: %s", mod, exc)

    return view.MibViewController(mib_builder)


@lru_cache(maxsize=512)
def _cached_mib_view(mib_dirs_key: tuple[str, ...], mib_modules_key: tuple[str, ...]):
    """Cache MIB views per (dirs, modules) combination."""
    return build_mib_view(list(mib_dirs_key), list(mib_modules_key))


def get_mib_view(mib_dirs: list[str], mib_modules: list[str]):
    """Return a cached MIB view for the given dirs + modules.

    v1.5.0 (GH #72): ``TRAM_SNMP_STACK=trishul`` returns a cached tsmi JSON
    bundle view (:class:`_TsmiBundleView`) over the same ``mib_dirs``; the
    default ``legacy`` path is byte-identical to the pre-flag behavior.
    """
    dirs_key = tuple(sorted(mib_dirs))
    modules_key = tuple(sorted(mib_modules))
    if snmp_stack() == "trishul":
        return _cached_tsmi_view(dirs_key, modules_key)
    return _cached_mib_view(dirs_key, modules_key)


def resolve_oid_structured(mib_view, oid_tuple: tuple) -> tuple[str, tuple[int, ...], str, str]:
    """Resolve a numeric OID tuple to structured parts plus the string form.

    Sibling of :func:`resolve_oid` that keeps the MIB-computed instance
    indices as structured ints instead of flattening them into a string
    (the grouping layer re-derives row coordinates from strings today — see
    the structured-index grouping redesign, GH #36).

    Returns ``(sym_name, indices, resolved_str, mod_name)``:

    * ``sym_name`` — symbolic column name (``""`` when unresolved).
    * ``indices`` — instance indices as a tuple of ints (empty when
      unresolved or when the node has no instance).
    * ``resolved_str`` — the legacy string form, byte-identical to
      :func:`resolve_oid` output (dotted-decimal fallback when unresolved).
    * ``mod_name`` — MIB module name (``""`` when unresolved).

    Args:
        mib_view: MibViewController from build_mib_view(), or a tsmi bundle
            view (:class:`_TsmiBundleView` / ``trishul_snmp.MibBundle``) on the
            ``TRAM_SNMP_STACK=trishul`` path.
        oid_tuple: Numeric OID as tuple of ints, e.g. (1, 3, 6, 1, 2, 1, 1, 1, 0).
    """
    dotted = ".".join(str(x) for x in oid_tuple)
    if mib_view is None:
        return "", (), dotted, ""

    # tsmi JSON-bundle path (v1.5.0 flag-on): bundle views expose lookup() and
    # never mibBuilder, so the duck-type can never collide with pysnmp's view.
    if not hasattr(mib_view, "mibBuilder") and hasattr(mib_view, "lookup"):
        try:
            match = mib_view.lookup(oid_tuple)
        except Exception:
            match = None
        if match is None:
            return "", (), dotted, ""
        indices = tuple(int(i) for i in (match.suffix or ()))
        if indices:
            resolved_str = match.symbol + "." + ".".join(str(i) for i in indices)
        else:
            resolved_str = match.symbol
        return match.symbol, indices, resolved_str, match.module

    try:
        from pyasn1.type.univ import ObjectIdentifier

        oid_obj = ObjectIdentifier(oid_tuple)
        get_node_location = getattr(mib_view, "get_node_location", None)
        if get_node_location is None:
            get_node_location = getattr(mib_view, "getNodeLocation")
        mod_name, sym_name, indices = get_node_location(oid_obj)
        indices_tuple = tuple(int(i) for i in indices)
        if indices_tuple:
            resolved_str = sym_name + "." + ".".join(str(i) for i in indices_tuple)
        else:
            resolved_str = sym_name
        return sym_name, indices_tuple, resolved_str, mod_name
    except Exception:
        return "", (), dotted, ""


def resolve_oid(mib_view, oid_tuple: tuple) -> str:
    """Resolve a numeric OID tuple to a symbolic name.

    Args:
        mib_view: MibViewController from build_mib_view().
        oid_tuple: Numeric OID as tuple of ints, e.g. (1, 3, 6, 1, 2, 1, 1, 1, 0).

    Returns:
        Symbolic string like "sysDescr" or dotted-decimal string as fallback.
    """
    return resolve_oid_structured(mib_view, oid_tuple)[2]


def oid_str_to_tuple(oid_str: str) -> tuple[int, ...]:
    """Convert dotted-decimal OID string to tuple of ints."""
    return tuple(int(x) for x in oid_str.strip(".").split("."))


def symbolic_to_oid(mib_view, symbolic: str) -> tuple[int, ...] | None:
    """Resolve a symbolic OID name to a numeric tuple.

    Args:
        mib_view: MibViewController from build_mib_view(), or a tsmi bundle
            view on the ``TRAM_SNMP_STACK=trishul`` path.
        symbolic: Symbolic name like "IF-MIB::ifOperStatus.1" or "sysDescr.0".

    Returns:
        Tuple of ints, or None if resolution fails.
    """
    if mib_view is None:
        return None

    # tsmi JSON-bundle path (v1.5.0 flag-on): MODULE::symbol[.suffix] resolves
    # directly; bare symbols are searched across every loaded module.
    if not hasattr(mib_view, "mibBuilder") and hasattr(mib_view, "lookup"):
        try:
            if "::" in symbolic:
                return mib_view.resolve(symbolic)
            parts = symbolic.split(".")
            sym_name = parts[0]
            indices = tuple(int(x) for x in parts[1:]) if len(parts) > 1 else ()
            for module in mib_view.module_names():
                node = mib_view.resolve_node(module, sym_name)
                if node is not None:
                    return tuple(node.oid) + indices
            return None
        except Exception as exc:
            logger.debug("Could not resolve symbolic OID %r: %s", symbolic, exc)
            return None

    try:
        # Handle "MODULE::name.index" format (e.g. "IF-MIB::ifDescr.1")
        if "::" in symbolic:
            module, rest = symbolic.split("::", 1)
            parts = rest.split(".")
            sym_name = parts[0]
            indices = [int(x) for x in parts[1:]] if len(parts) > 1 else []
            from pysnmp.smi.rfc1902 import ObjectIdentity

            oid_obj = ObjectIdentity(module, sym_name, *indices)
            oid_obj.resolveWithMib(mib_view)
            return tuple(oid_obj.getOid())
        else:
            parts = symbolic.split(".")
            sym_name = parts[0]
            indices = [int(x) for x in parts[1:]] if len(parts) > 1 else []
            oid_obj, _, _ = mib_view.getNodeName((sym_name,))
            return tuple(oid_obj) + tuple(indices)
    except Exception as exc:
        logger.debug("Could not resolve symbolic OID %r: %s", symbolic, exc)
        return None
