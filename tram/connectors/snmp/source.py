"""SNMP source connectors — trap receiver and polling source.

v1.5.0 (GH #72): dual-stack. ``TRAM_SNMP_STACK=trishul`` (the default since
v1.8.0) runs the poll and trap paths over tsnmp (``trishul_snmp``: V1/V2c/V3
managers, notification listeners, ``decode_notification``); the ``legacy``
escape hatch is the byte-identical pysnmp implementation (available through
the v1.8.x releases). Shared parse/normalize (MIB resolution,
row grouping, classification) is stack-agnostic — only the wire layers differ,
and the legacy branch is deleted wholesale in v1.9.0.
"""

from __future__ import annotations

import asyncio
import datetime
import fnmatch
import logging
import os
import re
import socket
import threading
from collections.abc import Iterator

from tram.connectors.config_utils import (
    cfg_bool,
    cfg_float,
    cfg_int,
    cfg_list,
    cfg_str,
    prepend_system_mib_dirs,
    snmpv3_usm,
)
from tram.connectors.snmp.mib_utils import (
    build_tsnmp_local_engine,
    close_snmp_engine,
    create_udp_transport_target,
    get_hlapi_asyncio,
    hlapi_get_cmd,
    hlapi_next_cmd,
    snmp_stack,
)
from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


# Code-default INTEGER classification globs (GH #35). Standard-only
# vocabulary: `*Vdom` was removed — it is deployment-specific Fortigate
# vocabulary, not a standard field suffix. Fortigate pipelines relying on the
# old default must add `*Vdom` explicitly via `label_patterns` or
# `TRAM_SNMP_LABEL_PATTERNS`.
_DEFAULT_LABEL_PATTERNS = ("*Id", "*ID", "*Index", "*Port")
_DEFAULT_METRIC_PATTERNS: tuple[str, ...] = ()

_ENV_METRIC_PATTERNS = "TRAM_SNMP_METRIC_PATTERNS"
_ENV_LABEL_PATTERNS = "TRAM_SNMP_LABEL_PATTERNS"


def _env_patterns(env_name: str) -> tuple[str, ...]:
    """Read a comma-separated glob pattern list from the environment."""
    raw = os.environ.get(env_name, "")
    return tuple(part for part in (p.strip() for p in raw.split(",")) if part)


def _merge_patterns(*layers: tuple[str, ...]) -> tuple[str, ...]:
    """Merge pattern layers, preserving order and dropping duplicates."""
    return tuple(dict.fromkeys(p for layer in layers for p in layer))


def _call_snmp_api(obj: object, snake_name: str, *args):
    """Call a pysnmp API method across snake_case and camelCase variants."""
    method = getattr(obj, snake_name, None)
    if method is None:
        normalized = snake_name.replace("_", "").lower()
        for attr_name in dir(obj):
            if attr_name.replace("_", "").lower() == normalized:
                method = getattr(obj, attr_name)
                break
    if method is None:
        raise AttributeError(f"{obj!r} has no method matching {snake_name!r}")
    return method(*args)


def _in_walk_subtree(oid_tuple: tuple, base_tuple: tuple) -> bool:
    """True iff ``oid_tuple`` belongs to a WALK rooted at ``base_tuple``.

    Exact-or-child in tuple space (GH #32) — never a string-prefix compare
    (a sibling node like ``1.3.6.1.4.20`` must not match base ``1.3.6.1.4.2``)
    and never a character-stripped base (``rstrip(".0")`` mangles bases whose
    last arc ends in 0).
    """
    return oid_tuple == base_tuple or oid_tuple[: len(base_tuple)] == base_tuple


@register_source("snmp_trap")
class SNMPTrapSource(BaseSource):
    """Receive SNMP traps (v1/v2c/v3) over UDP, operating in stream mode.

    Each trap is decoded into a dict of OID → value bindings and yielded as
    ``(json_bytes, meta)``.

    Requires the ``tram[snmp]`` optional extra.

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
        priv_protocol   (str)   AES | AES128 | AES192 | AES256 | 3DES
                        (3DES-EDE supported again in v1.5.1; DES rejected at
                        validation).
        priv_key        (str)   Privacy passphrase (None → authNoPriv).
        context_name    (str)   SNMPv3 context name.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config.get("host", "0.0.0.0")
        self.port: int = cfg_int(config, "port", 162)
        self.community: str = config.get("community", "public")
        self.version: str = cfg_str(config, "version", "2c")
        self.mib_dirs: list[str] = prepend_system_mib_dirs(cfg_list(config, "mib_dirs"))
        self.mib_modules: list[str] = cfg_list(config, "mib_modules")
        self.resolve_oids: bool = cfg_bool(config, "resolve_oids", True)
        # SNMPv3 USM (review E4 — shared helper)
        usm = snmpv3_usm(config)
        self.security_name: str = usm["security_name"]
        self.auth_protocol: str = usm["auth_protocol"]
        self.auth_key: str | None = usm["auth_key"]
        self.priv_protocol: str = usm["priv_protocol"]
        self.priv_key: str | None = usm["priv_key"]
        self.context_name: str = usm["context_name"]
        self._stop_event: threading.Event = threading.Event()
        # v1.5.0 (GH #72): wire stack selected by TRAM_SNMP_STACK — the same
        # YAML config, a different library under it.
        self._snmp_stack: str = snmp_stack()

    def test_connection(self) -> dict:
        """Verify the trap listener port is available by attempting a UDP bind."""
        import time
        host = self.config.get("host", "0.0.0.0")
        port = int(self.config.get("port", 162))
        t0 = time.monotonic()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.close()
            latency = int((time.monotonic() - t0) * 1000)
            return {"ok": True, "latency_ms": latency, "detail": f"UDP {host}:{port} bind OK — trap listener ready"}
        except OSError as exc:
            return {"ok": False, "latency_ms": None, "error": f"UDP {host}:{port} bind failed: {exc}"}

    def stop(self) -> None:
        self._stop_event.set()

    def read(self) -> Iterator[tuple[bytes, dict]]:
        """Return the trap stream.

        On the tsnmp path the listener binds eagerly — at ``read()`` time,
        before the first record — so a trap sender started right after
        ``read()`` can never fire into an unbound socket.
        """
        if self._snmp_stack == "trishul":
            return self._read_tsnmp_listener()
        return self._read_raw_udp()

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
                except TimeoutError:
                    continue
                except Exception as exc:
                    logger.warning("SNMP trap recv error: %s", exc)
                    continue

                source_ip, src_port = addr
                raw_bindings = self._decode_trap(raw)

                # Optional MIB-based OID resolution (shared with the tsnmp path)
                bindings = self._resolve_trap_bindings(raw_bindings)

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

    # ── tsnmp listener path (v1.5.0 flag-on) ─────────────────────────────────

    def _build_tsnmp_listener(self):
        """Build the tsnmp notification listener for the configured version.

        v1/v2c share the community listener (it decodes v1 Trap-PDUs with
        their Trap-PDU metadata); v3 uses the per-user USM listener with a
        deterministic local engine (senders discover it via USM discovery
        probes, which the listener answers automatically). A UDP bind check
        mirrors the legacy loud bind failure.
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.close()
        except OSError as exc:
            raise SourceError(
                f"SNMP trap UDP bind failed on {self.host}:{self.port} — {exc}"
            ) from exc

        from trishul_snmp import V2cNotificationListener, V3NotificationListener

        if self.version == "3":
            from tram.connectors.snmp.mib_utils import build_v3_usm_user

            user = build_v3_usm_user(
                security_name=self.security_name,
                auth_protocol=self.auth_protocol,
                auth_key=self.auth_key,
                priv_protocol=self.priv_protocol,
                priv_key=self.priv_key,
            )
            local_engine = build_tsnmp_local_engine(
                f"tram:trap:{self.host}:{self.port}:{self.security_name}"
            )
            return V3NotificationListener(
                host=self.host, port=self.port, user=user, local_engine=local_engine
            )
        # The community listener accepts any community (parity with the legacy
        # decode-everything behavior); the configured community is only meta.
        return V2cNotificationListener(host=self.host, port=self.port, communities=None)

    def _tsnmp_listener_loop(self, listener, q, thread_stop: threading.Event, ready: threading.Event) -> None:
        """Run the tsnmp listener on its own event loop, pushing events to *q*.

        ``ready`` is set once the listener socket is bound, so the reader can
        wait before consuming — a trap sent before bind would otherwise be
        silently dropped (UDP has no receiver for it).
        """
        import asyncio
        import queue

        async def _serve() -> None:
            async with listener:
                ready.set()
                while not thread_stop.is_set() and not self._stop_event.is_set():
                    try:
                        event = await asyncio.wait_for(listener.receive(), timeout=1.0)
                    except TimeoutError:
                        continue
                    except Exception as exc:
                        logger.warning("SNMP trap listener error: %s", exc)
                        continue
                    try:
                        q.put(event, timeout=1.0)
                    except queue.Full:
                        logger.warning("SNMP trap event queue full — dropping event")

        try:
            asyncio.run(_serve())
        except Exception as exc:
            logger.warning("SNMP trap tsnmp listener thread failed: %s", exc)

    def _read_tsnmp_listener(self) -> Iterator[tuple[bytes, dict]]:
        """Trap stream over tsnmp listeners (V1/V2c community or V3 USM).

        The listener is asyncio-based; a dedicated thread runs its event loop
        and bridges received ``NotificationEvent`` objects into a queue that
        the returned generator drains — one record per trap, exactly like the
        legacy UDP loop. The listener binds eagerly (the thread waits for the
        socket to be ready before returning) so a sender started after
        ``read()`` cannot drop its trap into an unbound socket.
        """
        import queue

        q: queue.Queue = queue.Queue(maxsize=100)
        thread_stop = threading.Event()
        ready = threading.Event()
        listener = self._build_tsnmp_listener()
        thread = threading.Thread(
            target=self._tsnmp_listener_loop,
            args=(listener, q, thread_stop, ready),
            daemon=True,
            name=f"tram-snmp-trap-tsnmp-{self.host}:{self.port}",
        )
        thread.start()
        if not ready.wait(timeout=5.0):
            thread_stop.set()
            thread.join(timeout=5.0)
            raise SourceError(
                f"SNMP trap tsnmp listener failed to start on {self.host}:{self.port} — "
                "see log for the listener error"
            )

        def _stream() -> Iterator[tuple[bytes, dict]]:
            try:
                while not self._stop_event.is_set():
                    try:
                        event = q.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    yield self._tsnmp_event_record(event)
            finally:
                thread_stop.set()
                thread.join(timeout=5.0)

        return _stream()

    @staticmethod
    def _tsnmp_val_to_legacy_str(val_obj) -> str:
        """Render a tsnmp value exactly like the legacy trap path's ``str(val)``.

        The legacy ``_decode_trap`` binds ``str(pysnmp_proto_value)``; this is
        the parity contract for ``_tsnmp_event_record`` (C3/C4): raw bytes come
        back as latin-1 text (``OctetString``/``Opaque``) and an IpAddress as
        its raw 4 octets — pysnmp's ``__str__`` forms, which are ugly but are
        what the flag-off path emits. Equivalence is the contract; these spots
        are post-swap cleanup candidates (hex/pretty rendering).
        """
        cls = type(val_obj).__name__
        if cls == "OctetStringValue":
            return bytes(val_obj.value).decode("latin-1")
        if cls == "OpaqueValue":
            return bytes(val_obj.value).decode("latin-1")
        if cls == "IpAddressValue":
            return "".join(chr(int(part)) for part in str(val_obj.value).split("."))
        if cls == "ObjectIdentifierValue":
            return ".".join(str(arc) for arc in val_obj.value)
        return val_obj.to_display_string()

    def _tsnmp_event_record(self, event) -> tuple[bytes, dict]:
        """Convert a tsnmp NotificationEvent to the legacy (json, meta) shape."""
        import json

        raw_bindings = {
            vb.oid_str: self._tsnmp_val_to_legacy_str(vb.value) for vb in event.varbinds
        }
        bindings = self._resolve_trap_bindings(raw_bindings)

        meta = {
            "source_ip": event.source_host or "",
            "port": event.source_port,
            "community": event.community or self.community,
            "version": self.version,
        }
        logger.debug(
            "SNMP trap received",
            extra={"source_ip": meta["source_ip"], "bindings": len(bindings)},
        )
        return json.dumps(bindings).encode("utf-8"), meta

    def _resolve_trap_bindings(self, raw_bindings: dict) -> dict:
        """Optional MIB-based OID resolution shared by both trap paths."""
        if self.resolve_oids and (self.mib_dirs or self.mib_modules):
            try:
                from tram.connectors.snmp.mib_utils import (
                    get_mib_view,
                    oid_str_to_tuple,
                    resolve_oid,
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
        return bindings

    def _decode_trap(self, raw: bytes) -> dict:
        """Decode a raw SNMP trap PDU using pyasn1 BER decoder + pysnmp proto API.

        Legacy-path only (v1.5.0, GH #72): the flag-on trap stream decodes
        inside the tsnmp listeners (``_tsnmp_event_record``), so this method is
        never called on the trishul stack.
        """
        try:
            from pyasn1.codec.ber import decoder as ber_decoder
            from pysnmp.proto.api import v2c as pMod
            msg, _ = ber_decoder.decode(raw, asn1Spec=pMod.Message())
            reqPDU = _call_snmp_api(pMod.apiMessage, "get_pdu", msg)
            bindings: dict = {}
            for oid, val in _call_snmp_api(pMod.apiPDU, "get_varbinds", reqPDU):
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

    Requires the ``tram[snmp]`` optional extra.

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
        index_depth     (int, default 0)       Index split depth (0=auto). Applies
                                               to unresolved/numeric keys only —
                                               MIB-resolved keys use the structured
                                               instance indices the MIB view already
                                               computed. Auto + unresolved refuses.
        classify        (bool, default False)  Split fields into _metrics/_labels.
        metric_patterns (list[str])  INTEGER globs that force a metric (exceptions
                                     win over label patterns; extend code defaults).
        label_patterns  (list[str])  INTEGER globs that force a label (extend the
                                     code defaults *Id/*ID/*Index/*Port).
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
        self.port: int = cfg_int(config, "port", 161)
        self.community: str = config.get("community", "public")
        self.version: str = cfg_str(config, "version", "2c")
        self.oids: list[str] = cfg_list(config, "oids")
        self.operation: str = config.get("operation", "get").lower()
        self.timeout: float = cfg_float(config, "timeout", 1.0)
        self.retries: int = cfg_int(config, "retries", 5)
        self.mib_dirs: list[str] = prepend_system_mib_dirs(cfg_list(config, "mib_dirs"))
        self.mib_modules: list[str] = cfg_list(config, "mib_modules")
        self.resolve_oids: bool = cfg_bool(config, "resolve_oids", True)
        self.yield_rows: bool = cfg_bool(config, "yield_rows", False)
        self.index_depth: int = cfg_int(config, "index_depth", 0)
        self.classify: bool = cfg_bool(config, "classify", False)
        # INTEGER classification globs (GH #35): env + pipeline layers EXTEND the
        # code defaults, never replace them. metric_patterns wins over everything.
        self.metric_patterns: tuple[str, ...] = _merge_patterns(
            _DEFAULT_METRIC_PATTERNS,
            tuple(str(p) for p in config.get("metric_patterns", [])),
            _env_patterns(_ENV_METRIC_PATTERNS),
        )
        self.label_patterns: tuple[str, ...] = _merge_patterns(
            _DEFAULT_LABEL_PATTERNS,
            tuple(str(p) for p in config.get("label_patterns", [])),
            _env_patterns(_ENV_LABEL_PATTERNS),
        )
        # Compiled matchers are fixed per source instance — hoisted out of the
        # per-row classify loop (fnmatch.translate + re.compile per row per poll).
        self._metric_matchers: list[re.Pattern[str]] = [
            re.compile(fnmatch.translate(p)) for p in self.metric_patterns
        ]
        self._label_matchers: list[re.Pattern[str]] = [
            re.compile(fnmatch.translate(p)) for p in self.label_patterns
        ]
        # Set when resolve_oids is on but the MIB view fails to load, so the
        # auto-grouping refusal names the real cause instead of blaming
        # resolve_oids.
        self._mib_load_failed: bool = False
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

    # ── tsnmp wire layer (v1.5.0 flag-on) ────────────────────────────────────

    # tsnmp value type names → the legacy wire-class names the shared classify
    # layer keys on (GH #35 fixed tables). ``integer`` maps to ``Integer`` —
    # the wire-accurate class name at lookupMib=False.
    _TSNMP_TYPE_TO_WIRE = {
        "octet-string": "OctetString",
        "integer": "Integer",
        "counter32": "Counter32",
        "counter64": "Counter64",
        "gauge32": "Gauge32",
        "timeticks": "TimeTicks",
        "ip-address": "IpAddress",
        "object-identifier": "ObjectIdentifier",
        "opaque": "Opaque",
        "null": "Null",
        "no-such-object": "NoSuchObject",
        "no-such-instance": "NoSuchInstance",
        "end-of-mib-view": "EndOfMibView",
    }

    @staticmethod
    def _tsnmp_type_name(val_obj) -> str:
        """Map a tsnmp SnmpValue type name to the legacy wire-class spelling."""
        return SNMPPollSource._TSNMP_TYPE_TO_WIRE.get(
            getattr(val_obj, "type_name", ""), type(val_obj).__name__
        )

    @staticmethod
    def _tsnmp_val_to_str(val_obj) -> str:
        """Serialize a tsnmp SNMP value to the legacy ``_snmp_val_to_str`` shape.

        Parity contract (C3/C4): OctetString values containing non-printable
        bytes are hex-encoded (6-byte → MAC format, otherwise ``0x``-prefixed)
        exactly like the legacy path; IpAddress and Opaque render as their raw
        octets in latin-1 text — pysnmp's ``str()`` forms, ugly but what the
        flag-off path emits (post-swap cleanup candidates). All other types
        use the tsnmp display string, which matches the legacy decimal/dotted
        rendering.
        """
        type_name = type(val_obj).__name__
        if type_name == "OctetStringValue":
            raw = bytes(val_obj.value)
            if all(0x20 <= b < 0x7F for b in raw):
                return raw.decode("ascii")
            if len(raw) == 6:
                return ":".join(f"{b:02x}" for b in raw)
            return "0x" + raw.hex()
        if type_name == "ObjectIdentifierValue":
            return ".".join(str(arc) for arc in val_obj.value)
        if type_name == "IpAddressValue":
            return "".join(chr(int(part)) for part in str(val_obj.value).split("."))
        if type_name == "OpaqueValue":
            return bytes(val_obj.value).decode("latin-1")
        return val_obj.to_display_string()

    def _build_tsnmp_manager(self):
        """Build the tsnmp manager context manager for the configured version."""
        from trishul_snmp import V1Manager, V2cManager, V3Manager

        common = {
            "host": self.host,
            "port": self.port,
            "timeout": self.timeout,
            "retries": self.retries,
        }
        if self.version == "3":
            from tram.connectors.snmp.mib_utils import build_v3_usm_user

            user = build_v3_usm_user(
                security_name=self.security_name,
                auth_protocol=self.auth_protocol,
                auth_key=self.auth_key,
                priv_protocol=self.priv_protocol,
                priv_key=self.priv_key,
            )
            return V3Manager(
                user=user,
                context_name=self.context_name.encode("utf-8"),
                **common,
            )
        cls = V1Manager if self.version == "1" else V2cManager
        return cls(community=self.community, **common)

    async def _do_get_tsnmp(self, typed: bool = False) -> dict:
        resolved_oids = self._resolved_source_oids()
        async with self._build_tsnmp_manager() as mgr:
            resp = await mgr.get(*resolved_oids)
        if resp.error_status != 0:
            raise SourceError(
                f"SNMP GET PDU error: {resp.error_status.name} at index "
                f"{resp.error_index or '?'}"
            )
        if typed:
            return {
                vb.oid_str: (self._tsnmp_val_to_str(vb.value), self._tsnmp_type_name(vb.value))
                for vb in resp.varbinds
            }
        return {vb.oid_str: self._tsnmp_val_to_str(vb.value) for vb in resp.varbinds}

    async def _do_walk_tsnmp(self, typed: bool = False) -> dict:
        """tsnmp walk: GETNEXT loop mode (``bulk=False``), mirroring the legacy
        ``nextCmd`` loop; subtree boundaries and no-progress quirks are
        handled inside tsnmp's ``walk``."""
        resolved_oids = self._resolved_source_oids()
        bindings: dict = {}
        async with self._build_tsnmp_manager() as mgr:
            for base_oid in resolved_oids:
                walked = await mgr.walk(base_oid, bulk=False)
                for vb in walked:
                    if typed:
                        bindings[vb.oid_str] = (
                            self._tsnmp_val_to_str(vb.value),
                            self._tsnmp_type_name(vb.value),
                        )
                    else:
                        bindings[vb.oid_str] = self._tsnmp_val_to_str(vb.value)
        return bindings

    @staticmethod
    def _numeric_part(part: int | str) -> int | str:
        """Coerce a numeric string to int for tuple-space row keys."""
        if isinstance(part, int):
            return part
        if isinstance(part, str) and part.isdigit():
            return int(part)
        return part

    @staticmethod
    def _index_sort_key(idx: tuple) -> tuple:
        """Numeric-aware row sort key: ints compare numerically, strings fall back."""

        def _key(part: int | str) -> tuple:
            if isinstance(part, int):
                return (0, part)
            if isinstance(part, str) and part.isdigit():
                return (0, int(part))
            return (1, str(part))

        return tuple(_key(p) for p in idx)

    @staticmethod
    def _group_by_index(
        bindings: dict,
        index_depth: int,
        locations: dict[str, tuple[str, tuple[int, ...], str]] | None = None,
        *,
        mib_load_failed: bool = False,
    ) -> list[dict]:
        """Group flat ``{oid_key: value}`` bindings into per-row dicts.

        Row coordinates come from two sources (GH #36):

        * **Resolved keys** (``locations`` entry with a non-empty symbolic
          name) use the MIB-computed instance indices directly — per-key, so
          mixed-depth tables group correctly with no global knob.
        * **Unresolved/numeric keys** use ``index_depth``: the last *N* OID
          components form the index. With ``index_depth == 0`` (auto) an
          unresolved key raises :class:`SourceError` naming the OID — the old
          behavior silently emitted garbage rows from the first-dot split.
          Pass ``mib_load_failed=True`` when ``resolve_oids`` is on but the
          MIB view failed to load, so the refusal names that cause instead of
          suggesting ``resolve_oids``.

        ``locations`` maps each binding key to ``(sym_name, indices, mod_name)``
        as returned by ``resolve_oid_structured`` (``sym_name == ""`` when
        unresolved). Rows carry ``_index`` (dot-separated string), ``_index_parts``
        (list of strings), and, when locations were supplied, an internal
        ``_col_locs`` map of ``{column: mod_name}`` for resolved columns (the
        caller pops it before emitting). Rows are sorted by index tuple with
        numeric comparison (ints where numeric, string fallback).
        """
        rows: dict[tuple, dict] = {}
        for key, val in bindings.items():
            loc = locations.get(key) if locations else None
            if loc is not None and loc[0]:
                sym_name, indices, mod_name = loc
                col = sym_name
                idx_parts = [str(i) for i in indices]
                idx_key = tuple(indices)
            else:
                # Unresolved/numeric key — the row index comes from config.
                if index_depth == 0:
                    if mib_load_failed:
                        raise SourceError(
                            f"SNMP poll: cannot determine row index for OID {key} in "
                            f"auto mode (index_depth=0) — resolve_oids is on but MIB "
                            f"resolution failed (see log for the load error; check "
                            f"mib_dirs/mibs config), or set index_depth to the number "
                            f"of trailing index components"
                        )
                    raise SourceError(
                        f"SNMP poll: cannot determine row index for OID {key} in "
                        f"auto mode (index_depth=0) — set index_depth to the number "
                        f"of trailing index components, or enable resolve_oids for "
                        f"MIB-based row indices"
                    )
                parts = key.split(".")
                if len(parts) > index_depth:
                    col = ".".join(parts[:-index_depth])
                    idx_parts = parts[-index_depth:]
                else:
                    col, idx_parts = key, []
                idx_key = tuple(SNMPPollSource._numeric_part(p) for p in idx_parts)
                mod_name = ""

            if idx_key not in rows:
                rows[idx_key] = {
                    "_index": ".".join(idx_parts),
                    "_index_parts": idx_parts,
                }
                if locations is not None:
                    rows[idx_key]["_col_locs"] = {}
            rows[idx_key][col] = val
            if mod_name and locations is not None:
                rows[idx_key]["_col_locs"][col] = mod_name

        return [
            rows[k] for k in sorted(rows, key=SNMPPollSource._index_sort_key)
        ]

    def test_connection(self) -> dict:
        """Send a real SNMP GET for sysDescr.0 to verify host, port, and community string."""
        import time
        _SYSDESCR = "1.3.6.1.2.1.1.1.0"
        host      = self.config.get("host", "")
        port      = int(self.config.get("port", 161))
        community = self.config.get("community", "public")
        version   = str(self.config.get("version", "2c"))
        t0 = time.monotonic()
        if self._snmp_stack == "trishul":
            return self._test_connection_tsnmp(host, port, t0)
        try:
            hlapi = get_hlapi_asyncio()
        except ImportError:
            return {"ok": False, "latency_ms": None, "error": "pysnmp not installed — pip install tram[snmp]"}

        mp_model  = 0 if version == "1" else 1
        auth_data = hlapi.CommunityData(community, mpModel=mp_model)

        async def _probe():
            engine = hlapi.SnmpEngine()
            try:
                target = await create_udp_transport_target(
                    hlapi,
                    host=host,
                    port=port,
                    timeout=5.0,
                    retries=1,
                )
                errInd, errStatus, _, varBinds = await hlapi_get_cmd(
                    hlapi,
                    engine, auth_data, target, hlapi.ContextData(),
                    hlapi.ObjectType(hlapi.ObjectIdentity(_SYSDESCR)),
                    lookupMib=False,
                )
                if errInd:
                    raise OSError(str(errInd))
                if errStatus:
                    raise OSError(errStatus.prettyPrint())
                return str(varBinds[0][1]) if varBinds else ""
            finally:
                close_snmp_engine(engine)

        try:
            loop = asyncio.new_event_loop()
            try:
                sysDescr = loop.run_until_complete(_probe())
            finally:
                loop.close()
            latency = int((time.monotonic() - t0) * 1000)
            detail = f"SNMP {host}:{port} OK"
            if sysDescr:
                detail += f" — sysDescr: {sysDescr[:80]}"
            return {"ok": True, "latency_ms": latency, "detail": detail}
        except Exception as exc:
            latency = int((time.monotonic() - t0) * 1000)
            return {"ok": False, "latency_ms": latency, "error": f"SNMP {host}:{port} — {exc}"}

    def _test_connection_tsnmp(self, host: str, port: int, t0: float) -> dict:
        """tsnmp GET probe for sysDescr.0 (v1.5.0 flag-on path)."""
        import asyncio
        import time

        async def _probe() -> str:
            async with self._build_tsnmp_manager() as mgr:
                resp = await mgr.get("1.3.6.1.2.1.1.1.0")
            if resp.error_status != 0:
                raise OSError(resp.error_status.name)
            return self._tsnmp_val_to_str(resp.varbinds[0].value) if resp.varbinds else ""

        try:
            sysDescr = asyncio.run(_probe())
            latency = int((time.monotonic() - t0) * 1000)
            detail = f"SNMP {host}:{port} OK"
            if sysDescr:
                detail += f" — sysDescr: {sysDescr[:80]}"
            return {"ok": True, "latency_ms": latency, "detail": detail}
        except Exception as exc:
            latency = int((time.monotonic() - t0) * 1000)
            return {"ok": False, "latency_ms": latency, "error": f"SNMP {host}:{port} — {exc}"}

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

    def _resolve_configured_oid(self, oid: str, mib_view) -> str:
        """Resolve a configured OID to numeric dotted form if it is symbolic."""
        oid = oid.strip()
        if not oid:
            raise SourceError("SNMP poll OID must not be empty")
        if oid[0].isdigit() or oid[0] == ".":
            return oid.lstrip(".")

        if mib_view is None:
            raise SourceError(
                f"SNMP poll symbolic OID '{oid}' requires mib_modules or mib_dirs for resolution"
            )

        from tram.connectors.snmp.mib_utils import symbolic_to_oid

        resolved = symbolic_to_oid(mib_view, oid)
        if not resolved:
            raise SourceError(f"SNMP poll could not resolve symbolic OID '{oid}'")
        return ".".join(str(part) for part in resolved)

    def _resolved_source_oids(self) -> list[str]:
        """Return configured source OIDs in numeric dotted form."""
        mib_view = None
        if any(not oid.strip().lstrip(".")[:1].isdigit() for oid in self.oids):
            from tram.connectors.snmp.mib_utils import get_mib_view

            mib_view = get_mib_view(self.mib_dirs, self.mib_modules)
        return [self._resolve_configured_oid(oid, mib_view) for oid in self.oids]

    def _load_mib_view(self):
        """Load the cached MIB view when configured; None otherwise (GH #36).

        A load failure is recorded on ``self._mib_load_failed`` so callers can
        distinguish "resolve_oids is off" from "resolve_oids is on but the MIB
        view failed to load".
        """
        self._mib_load_failed = False
        if not (self.resolve_oids and (self.mib_dirs or self.mib_modules)):
            return None
        from tram.connectors.snmp.mib_utils import get_mib_view

        try:
            return get_mib_view(self.mib_dirs, self.mib_modules)
        except Exception as _exc:
            logger.warning("MIB OID resolution failed for poll: %s", _exc)
            self._mib_load_failed = True
            return None

    def _resolve_bindings(self, bindings: dict, mib_view) -> tuple[dict, dict]:
        """Resolve binding OID keys to structured locations + the flat form.

        Returns ``(locations, flat)``:

        * ``locations`` — ``{oid: (sym_name, indices, mod_name)}`` keyed by the
          *raw* binding keys, for tuple-space row grouping.
        * ``flat`` — ``{resolved_str: value}`` keyed by the legacy string form
          (identical to the old ``resolve_oid`` output) for flat emission.

        Warns when resolution produces no symbolic names (a no-op poll).
        """
        from tram.connectors.snmp.mib_utils import (
            oid_str_to_tuple,
            resolve_oid_structured,
        )

        locations: dict = {}
        flat: dict = {}
        for oid, val in bindings.items():
            sym_name, indices, resolved_str, mod_name = resolve_oid_structured(
                mib_view, oid_str_to_tuple(oid)
            )
            locations[oid] = (sym_name, indices, mod_name)
            flat[resolved_str] = val
        if bindings and not any(loc[0] for loc in locations.values()):
            logger.warning(
                "SNMP poll MIB resolution produced no symbolic names; using numeric OIDs",
                extra={
                    "host": self.host,
                    "operation": self.operation,
                    "mib_modules": self.mib_modules,
                    "mib_dirs": self.mib_dirs,
                },
            )
        return locations, flat

    def _empty_locations(self, bindings: dict) -> dict:
        """Locations marking every binding as unresolved (numeric keys)."""
        return {oid: ("", (), "") for oid in bindings}

    # SNMP wire types that map to metrics unconditionally (GH #35).
    # Unsigned32 is listed for MIB-typed completeness but is unreachable on
    # the real wire at lookupMib=False: tag 0x42 decodes as Gauge32, which is
    # already in this set (harmless; documented in GH #35).
    _METRIC_TYPES = frozenset({
        "Counter32", "Counter64", "Gauge32", "Unsigned32", "TimeTicks",
    })
    # SNMP wire types that map to labels unconditionally (GH #35).
    _LABEL_TYPES = frozenset({
        "OctetString", "IpAddress", "ObjectIdentifier", "Opaque", "Bits",
    })
    # INTEGER wire classes enter the layered resolution zone (GH #35):
    # metric_patterns → label_patterns → MIB SYNTAX enum → default metric.
    # Both spellings are handled because the wire class at lookupMib=False is
    # `Integer` while MIB-typed contexts (and tests written against them)
    # use `Integer32`.
    _INTEGER_TYPES = frozenset({"Integer", "Integer32"})

    @staticmethod
    def _mib_enum_name(mib_view, mod_name: str, sym_name: str, value: str) -> str | None:
        """Return the MIB SYNTAX enum label for ``(mod, sym, value)``, or None.

        Looks up the field's MIB node syntax and asks its ``namedValues`` for
        the symbolic name of the integer value (e.g. ``ifOperStatus`` value
        ``"1"`` → ``"up"``). Label precedence, documented: label_patterns /
        metric_patterns (the classify layer's pattern layer, checked before
        this is called) > inline SYNTAX enum > TC enum > default metric
        classification. When a column's SYNTAX names a textual convention
        rather than an inline enum (e.g. ``ifType`` = ``IANAifType`` — tsmi
        IR does not thread TC enums into column nodes, trishul-smi #44), the
        TC's own enum table is resolved from the view and used, so the tsnmp
        stack renders the same labels as legacy pysnmp. Returns None when the
        view is absent, the node has no enum syntax, the TC is absent from
        the corpus, or the value is not in the enum. Cheap: the MIB view is
        already loaded and cached for any ``resolve_oids=True`` poll.
        """
        if mib_view is None or not mod_name or not sym_name:
            return None
        # tsmi JSON-bundle path (v1.5.0 flag-on): node.enums is a label→number
        # map (the tsmi IR enums field) — reverse-lookup the value's label.
        if not hasattr(mib_view, "mibBuilder") and hasattr(mib_view, "lookup"):
            try:
                node = mib_view.resolve_node(mod_name, sym_name)
                if node is None:
                    return None
                if node.enums:
                    for label, number in node.enums.items():
                        if number == int(value):
                            return label
                    return None
                # TC-typed SYNTAX: thread the TC's own enum table (tsmi IR
                # stores it in types[<tc>].constraints, separate from the
                # column node). Gate on kind == "enum" — range constraints
                # (kind "range", e.g. SNMPv2-SMI Integer32 [[min, max]]) must
                # NOT be read as label→number tables, or a saturation value
                # would render the range MIN as a label. Gracefully degrades
                # when the TC module is absent from the corpus
                # (resolve_type → None).
                tc_name = getattr(node, "syntax", None)
                if tc_name and hasattr(mib_view, "resolve_type"):
                    tc = mib_view.resolve_type(mod_name, tc_name)
                    constraints = tc.constraints if tc else None
                    if constraints and constraints.get("kind") == "enum":
                        for label, number in constraints.get("data") or []:
                            if number == int(value):
                                return label
                return None
            except Exception:
                return None
        try:
            mib_builder = getattr(mib_view, "mibBuilder", None)
            if mib_builder is None:
                return None
            import_symbols = getattr(mib_builder, "import_symbols", None) or getattr(
                mib_builder, "importSymbols", None
            )
            if import_symbols is None:
                return None
            node = import_symbols(mod_name, sym_name)
            if isinstance(node, tuple):
                node = node[0]
            syntax = node.getSyntax()
            named_values = getattr(syntax, "namedValues", None)
            if named_values is None:
                return None
            name = named_values.getName(int(value))
            return name if isinstance(name, str) else None
        except Exception:
            return None

    @staticmethod
    def _classify_bindings(
        bindings_typed: dict[str, tuple[str, str]],
        *,
        metric_matchers: list[re.Pattern[str]] | None = None,
        label_matchers: list[re.Pattern[str]] | None = None,
        metric_patterns: tuple[str, ...] = (),
        label_patterns: tuple[str, ...] = (),
        enum_names: dict[str, str] | None = None,
    ) -> dict:
        """Classify ``{key: (str_val, type_name)}`` into ``_metrics``/``_labels``.

        Rules (GH #35, applied per field, stripping any trailing ``.index``
        suffix first):

        - Counter32 / Counter64 / Gauge32 / Unsigned32 / TimeTicks → metric (int)
        - OctetString / IpAddress / ObjectIdentifier / Opaque / Bits → label (str)
        - Integer / Integer32 → layered resolution:
            1. ``metric_patterns`` match → metric (exceptions win)
            2. ``label_patterns`` match → label
            3. MIB SYNTAX enum (via ``enum_names``) → label, rendering the
               symbolic name alongside the int, e.g. ``"down (2)"``
            4. default → metric (plain INTEGER = measurement)

        ``metric_matchers``/``label_matchers`` are the compiled forms of the
        *effective* glob lists (code defaults + pipeline config + environment
        — see the module-level ``_DEFAULT_*_PATTERNS``); the read path passes
        the per-instance precompiled matchers so the per-row loop never
        recompiles. Matching is case-sensitive glob. When the matchers are
        omitted (direct calls), the raw ``metric_patterns``/``label_patterns``
        are compiled here. ``enum_names`` maps a base field to its MIB enum
        label for the value in hand (computed by the caller only when
        ``resolve_oids=True``).

        Additionally emits ``_snmp_widths: {field: 32|64}`` for Counter32/
        Counter64 fields — the SNMP type name is in hand here and otherwise
        discarded, which would leave wrap-width inference to a heuristic (F.1
        §4.4). ``counter_delta`` consumes ``_snmp_widths`` as authoritative.
        """
        metrics: dict = {}
        labels: dict = {}
        widths: dict = {}
        if metric_matchers is None:
            metric_matchers = [re.compile(fnmatch.translate(p)) for p in metric_patterns]
        if label_matchers is None:
            label_matchers = [re.compile(fnmatch.translate(p)) for p in label_patterns]
        for key, (str_val, type_name) in bindings_typed.items():
            # Strip a trailing dot-index from *symbolic* keys ("ifDescr.1" →
            # base="ifDescr"). Numeric column names (which start with a digit)
            # are already base names and must NOT be stripped — SNMP symbols
            # can never begin with a digit, so the first-char test is exact.
            # Keys arriving from the grouping layer are already base names, so
            # this is a no-op there; it keeps the direct-call contract.
            base = key.split(".")[0] if "." in key and not key[0].isdigit() else key
            if type_name in SNMPPollSource._METRIC_TYPES:
                if type_name == "Counter32":
                    widths[base] = 32
                elif type_name == "Counter64":
                    widths[base] = 64
                try:
                    metrics[base] = int(str_val)
                except ValueError:
                    metrics[base] = str_val
            elif type_name in SNMPPollSource._INTEGER_TYPES:
                if any(r.match(base) for r in metric_matchers):
                    try:
                        metrics[base] = int(str_val)
                    except ValueError:
                        metrics[base] = str_val
                elif any(r.match(base) for r in label_matchers):
                    labels[base] = str_val
                elif enum_names and base in enum_names:
                    labels[base] = f"{enum_names[base]} ({str_val})"
                else:
                    try:
                        metrics[base] = int(str_val)
                    except ValueError:
                        metrics[base] = str_val
            else:
                labels[base] = str_val
        return {"_metrics": metrics, "_labels": labels, "_snmp_widths": widths}

    @staticmethod
    def _snmp_val_to_str(val_obj) -> str:
        """Serialize an SNMP value to a clean string.

        OctetString values containing non-printable bytes are hex-encoded:
        - 6-byte values (MAC addresses) → ``aa:bb:cc:dd:ee:ff``
        - Other binary values → ``0xAABBCC...``
        All other types fall back to ``str()``.
        """
        type_name = type(val_obj).__name__
        if type_name == "OctetString":
            try:
                raw = bytes(val_obj)
            except Exception:
                return str(val_obj)
            # Printable ASCII — return as plain string
            if all(0x20 <= b < 0x7F for b in raw):
                return raw.decode("ascii")
            # 6-byte binary → MAC address format
            if len(raw) == 6:
                return ":".join(f"{b:02x}" for b in raw)
            # Other binary → prefixed hex
            return "0x" + raw.hex()
        return str(val_obj)

    async def _do_get(self, hlapi_mod, typed: bool = False) -> dict:
        if self._snmp_stack == "trishul":
            return await self._do_get_tsnmp(typed=typed)
        engine = hlapi_mod.SnmpEngine()
        auth_data = self._build_auth(hlapi_mod)
        try:
            resolved_oids = self._resolved_source_oids()
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
            var_bind_objs = [
                hlapi_mod.ObjectType(hlapi_mod.ObjectIdentity(oid))
                for oid in resolved_oids
            ]
            errInd, errStatus, errIdx, varBinds = await hlapi_get_cmd(
                hlapi_mod,
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
            if typed:
                return {str(oid): (self._snmp_val_to_str(val), type(val).__name__) for oid, val in varBinds}
            return {str(oid): self._snmp_val_to_str(val) for oid, val in varBinds}
        finally:
            close_snmp_engine(engine)

    async def _do_walk(self, hlapi_mod, typed: bool = False) -> dict:
        if self._snmp_stack == "trishul":
            return await self._do_walk_tsnmp(typed=typed)
        engine = hlapi_mod.SnmpEngine()
        auth_data = self._build_auth(hlapi_mod)
        try:
            resolved_oids = self._resolved_source_oids()
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
            bindings: dict = {}
            for base_oid in resolved_oids:
                base_tuple = tuple(int(part) for part in base_oid.strip(".").split("."))
                current_oid = base_oid
                current_oid_tuple = base_tuple
                while True:
                    errInd, errStatus, errIdx, varBinds = await hlapi_next_cmd(
                        hlapi_mod,
                        engine, auth_data, target, context,
                        hlapi_mod.ObjectType(hlapi_mod.ObjectIdentity(current_oid)),
                        lookupMib=False,
                    )
                    if errInd or errStatus or not varBinds:
                        break
                    # varBinds from nextCmd/get_next_cmd is list-of-list: [[( oid, val ), ...]]
                    row = varBinds[0] if isinstance(varBinds[0], list) else varBinds
                    stop = False
                    advanced = False
                    for oid_obj, val_obj in row:
                        oid_str = str(oid_obj)
                        oid_tuple = tuple(int(part) for part in oid_str.strip(".").split("."))
                        # Stop when we leave the subtree: a varbind belongs to the
                        # walk iff it is the base itself or a descendant in tuple
                        # space (GH #32).
                        if not _in_walk_subtree(oid_tuple, base_tuple):
                            stop = True
                            break
                        # Some agents/hlapi paths can repeat the terminal OID at the
                        # subtree boundary. Guard against a no-progress infinite loop.
                        if oid_tuple <= current_oid_tuple:
                            stop = True
                            break
                        if typed:
                            bindings[oid_str] = (self._snmp_val_to_str(val_obj), type(val_obj).__name__)
                        else:
                            bindings[oid_str] = self._snmp_val_to_str(val_obj)
                        current_oid = oid_str
                        current_oid_tuple = oid_tuple
                        advanced = True
                    if stop or not advanced:
                        break
            return bindings
        finally:
            close_snmp_engine(engine)

    def read(self) -> Iterator[tuple[bytes, dict]]:
        import json
        if self._snmp_stack != "trishul":
            try:
                _hlapi = get_hlapi_asyncio()
            except Exception as exc:
                raise SourceError(
                    "SNMP poll source requires pysnmp — install with: pip install tram[snmp]"
                ) from exc
        else:
            # tsnmp path (v1.5.0 flag-on): no pysnmp requirement.
            _hlapi = None

        if self.classify and not self.resolve_oids:
            logger.warning(
                "SNMP classify=True works best with resolve_oids=True — "
                "field names will be raw OID strings"
            )

        polled_at = datetime.datetime.now(datetime.UTC).isoformat()

        try:
            if self.operation == "get":
                raw = asyncio.run(self._do_get(_hlapi, typed=self.classify))
            elif self.operation == "walk":
                raw = asyncio.run(self._do_walk(_hlapi, typed=self.classify))
            else:
                raise SourceError(
                    f"SNMP poll: unsupported operation '{self.operation}' (use get or walk)"
                )
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"SNMP poll failed: {exc}") from exc

        meta = {
            "source_host": self.host,
            "source_port": self.port,
            "operation": self.operation,
            "oids": self.oids,
            "polled_at": polled_at,
        }
        logger.info(
            "SNMP poll completed",
            extra={"host": self.host, "operation": self.operation, "bindings": len(raw)},
        )

        if self.classify:
            # Pipeline (GH #36): typed capture → resolve (structured indices) →
            # group by index tuple → classify per row → emit. Classification never
            # sees a key without a row coordinate, so row collapse is impossible.
            mib_view = self._load_mib_view()
            if mib_view is not None:
                try:
                    locations, _flat = self._resolve_bindings(raw, mib_view)
                except Exception as _exc:
                    logger.warning("MIB OID resolution failed for poll: %s", _exc)
                    locations = self._empty_locations(raw)
            else:
                locations = self._empty_locations(raw)

            rows = self._group_by_index(
                raw, self.index_depth, locations,
                mib_load_failed=self._mib_load_failed,
            )
            all_rows = []
            for row in rows:
                col_locs = row.pop("_col_locs", {})
                # MIB SYNTAX enum labels for INTEGER fields (resolve_oids-only —
                # the view is already loaded and cached, measured free).
                enum_names: dict[str, str] = {}
                for col, col_val in row.items():
                    if col.startswith("_"):
                        continue
                    str_val, type_name = col_val
                    if type_name not in SNMPPollSource._INTEGER_TYPES:
                        continue
                    enum_name = self._mib_enum_name(
                        mib_view, col_locs.get(col, ""), col, str_val
                    )
                    if enum_name:
                        enum_names[col] = enum_name
                row_typed = {k: v for k, v in row.items() if not k.startswith("_")}
                classified = self._classify_bindings(
                    row_typed,
                    metric_matchers=self._metric_matchers,
                    label_matchers=self._label_matchers,
                    enum_names=enum_names or None,
                )
                classified["_polled_at"] = polled_at
                if self.yield_rows:
                    classified["_index"] = row["_index"]
                all_rows.append(classified)

            if self.yield_rows:
                # One payload per poll → one chunk → one write per sink
                yield json.dumps(all_rows).encode("utf-8"), meta
            else:
                if len(all_rows) > 1:
                    # Refuse instead of silently collapsing rows (GH #33): a
                    # multi-row table squeezed into one record loses all but the
                    # last value per column.
                    columns = sorted(
                        {c for row in rows for c in row if not c.startswith("_")}
                    )
                    raise SourceError(
                        f"SNMP poll classify with yield_rows=false would collapse "
                        f"{len(all_rows)} table rows (columns: {', '.join(columns)}) "
                        f"into one record — set yield_rows=true to emit one record "
                        f"per row"
                    )
                if all_rows:
                    record = all_rows[0]
                else:
                    record = {"_metrics": {}, "_labels": {}, "_snmp_widths": {}, "_polled_at": polled_at}
                yield json.dumps(record).encode("utf-8"), meta
            return

        # ── Standard (non-classify) path ──────────────────────────────────────
        locations: dict = {}
        flat: dict = raw  # type: ignore[assignment]
        mib_view = self._load_mib_view()
        if mib_view is not None:
            try:
                locations, flat = self._resolve_bindings(raw, mib_view)
            except Exception as _exc:
                logger.warning("MIB OID resolution failed for poll: %s", _exc)
                locations = self._empty_locations(raw)
        else:
            locations = self._empty_locations(raw)

        if self.yield_rows:
            rows = self._group_by_index(
                raw, self.index_depth, locations,
                mib_load_failed=self._mib_load_failed,
            )
            for row in rows:
                row.pop("_index_parts", None)
                row.pop("_col_locs", None)
                row["_polled_at"] = polled_at
            # Yield all rows as a single payload → one chunk → one write per sink
            yield json.dumps(rows).encode("utf-8"), meta
        else:
            flat["_polled_at"] = polled_at
            yield json.dumps(flat).encode("utf-8"), meta
