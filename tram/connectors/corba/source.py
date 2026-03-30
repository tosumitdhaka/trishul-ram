"""CORBA source connector — invokes a remote CORBA operation via DII.

No pre-compiled IDL stubs are required: the connector uses the Dynamic
Invocation Interface (DII) with simple Python scalars as arguments.
This covers the majority of telecom NMS use cases (3GPP Itf-N, TMN X.700,
Ericsson ENM, Nokia NetAct, Huawei iManager).

Install dependency::

    pip install tram[corba]   # pulls omniORBpy

Config keys:
    ior              (str, optional)  Direct IOR string.  Mutually exclusive
                                      with naming_service.
    naming_service   (str, optional)  corbaloc URI, e.g.
                                      "corbaloc:iiop:192.168.1.1:2809/NameService"
    object_name      (str, optional)  Slash-separated path in the NamingService,
                                      e.g. "PM/PMCollect".  Used only with
                                      naming_service.
    operation        (str, required)  CORBA operation name to invoke.
    args             (list, default [])  Positional arguments (simple Python
                                      scalars: int, float, str, bool).
    timeout_seconds  (int, default 30)  ORB-level request timeout.
    skip_processed   (bool, default False)  When True and a _file_tracker is
                                      injected by the executor, an invocation
                                      key is recorded in the DB so the same
                                      (pipeline, endpoint, operation+args) is
                                      not repeated on the next run.
"""

from __future__ import annotations

import json
import logging
from typing import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


def _corba_to_python(obj) -> object:
    """Best-effort conversion of a CORBA value to a plain Python object."""
    if obj is None:
        return None
    if isinstance(obj, (int, float, str, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_corba_to_python(x) for x in obj]
    # Struct-like objects expose __dict__ or _fields
    try:
        d = {}
        for attr in dir(obj):
            if attr.startswith("_"):
                continue
            val = getattr(obj, attr)
            if callable(val):
                continue
            d[attr] = _corba_to_python(val)
        return d
    except Exception:
        return str(obj)


@register_source("corba")
class CorbaSource(BaseSource):
    """Invoke a CORBA operation and yield the result as a list of records."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.ior: str | None = config.get("ior")
        self.naming_service: str | None = config.get("naming_service")
        self.object_name: str | None = config.get("object_name")
        self.operation: str = config["operation"]
        self.args: list = config.get("args", [])
        self.timeout_seconds: int = int(config.get("timeout_seconds", 30))
        self.skip_processed: bool = bool(config.get("skip_processed", False))
        self._pipeline_name: str = config.get("_pipeline_name", "")
        self._file_tracker = config.get("_file_tracker")

    # ── Internal ───────────────────────────────────────────────────────────

    def _get_orb_and_object(self):
        """Initialise ORB and resolve the target CORBA object."""
        try:
            import CORBA  # type: ignore[import]
        except ImportError as exc:
            raise SourceError(
                "CORBA source requires omniORBpy — install with: pip install tram[corba]"
            ) from exc

        try:
            orb = CORBA.ORB_init()

            if self.ior:
                obj = orb.string_to_object(self.ior)
            elif self.naming_service:
                try:
                    import CosNaming  # type: ignore[import]
                except ImportError as exc:
                    raise SourceError(
                        "CosNaming module not found — ensure omniORBpy is installed correctly"
                    ) from exc
                ns_ref = orb.string_to_object(self.naming_service)
                ns = ns_ref._narrow(CosNaming.NamingContext)
                if ns is None:
                    raise SourceError("Failed to narrow NamingService object")
                parts = (self.object_name or "").strip("/").split("/")
                name = [CosNaming.NameComponent(p, "") for p in parts if p]
                obj = ns.resolve(name)
            else:
                raise SourceError("Either 'ior' or 'naming_service' must be configured")

            return orb, obj
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"CORBA ORB init failed: {exc}") from exc

    def _invoke(self, orb, obj) -> object:
        """DII invocation of self.operation with self.args."""
        try:
            request = obj._request(self.operation)
            for arg in self.args:
                # _add_in_arg() returns a CORBA Any reference; <<= inserts the value
                in_arg = request._add_in_arg()
                in_arg <<= arg
            request.invoke()
            return request._result
        except Exception as exc:
            raise SourceError(f"CORBA DII invocation of '{self.operation}' failed: {exc}") from exc

    # ── Public ─────────────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        import socket
        import time
        naming_service = self.config.get("naming_service", "")
        if naming_service and naming_service.startswith("corbaloc:iiop:"):
            t0 = time.monotonic()
            try:
                remainder = naming_service.split("corbaloc:iiop:", 1)[1]
                hostport = remainder.split("/")[0]
                parts = hostport.rsplit(":", 1)
                host = parts[0]
                port = int(parts[1]) if len(parts) > 1 else 2809
                with socket.create_connection((host, port), timeout=8):
                    latency = int((time.monotonic() - t0) * 1000)
                    return {"ok": True, "latency_ms": latency, "detail": f"CORBA TCP {host}:{port} OK"}
            except Exception as exc:
                raise RuntimeError(f"CORBA TCP probe failed: {exc}")
        return {"ok": True, "latency_ms": None, "detail": "No remote test available for IOR-based CORBA"}

    def read(self) -> Iterator[tuple[bytes, dict]]:
        # Build a stable invocation key for skip_processed tracking
        invocation_key = f"{self.ior or self.naming_service}/{self.operation}"
        args_repr = json.dumps(self.args, sort_keys=True, default=str)
        source_key = f"corba:{invocation_key}"
        track_fp = f"{self.operation}:{args_repr}"

        if self.skip_processed and self._file_tracker:
            if self._file_tracker.is_processed(self._pipeline_name, source_key, track_fp):
                logger.info(
                    "CORBA: skipping already-processed invocation",
                    extra={"pipeline": self._pipeline_name, "operation": self.operation},
                )
                return

        logger.info(
            "CORBA source invoking operation",
            extra={"operation": self.operation, "corba_args": self.args},
        )

        orb, obj = self._get_orb_and_object()
        try:
            result = self._invoke(orb, obj)
        finally:
            try:
                orb.destroy()
            except Exception:
                pass

        # Normalise result to a list of records
        python_result = _corba_to_python(result)
        if isinstance(python_result, list):
            records = python_result
        elif python_result is None:
            records = []
        else:
            records = [python_result]

        meta = {
            "corba_operation": self.operation,
            "corba_endpoint": self.ior or self.naming_service or "",
        }
        yield json.dumps(records).encode(), meta

        if self.skip_processed and self._file_tracker:
            self._file_tracker.mark_processed(self._pipeline_name, source_key, track_fp)
