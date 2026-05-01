"""Prometheus Remote-Write source — accepts Prometheus remote_write POSTs."""

from __future__ import annotations

import json
import logging
from collections.abc import Generator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


@register_source("prometheus_rw")
class PrometheusRWSource(BaseSource):
    """Accept Prometheus remote_write payloads forwarded from the daemon.

    Uses the same global webhook registry as WebhookSource.
    Snappy-decompresses and decodes the protobuf WriteRequest,
    converting each TimeSeries to a dict of {labels, samples}.

    Config:
        path (str): URL path segment. Default "prom-rw".
        secret (str, optional): Bearer token for auth.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.path: str = config.get("path", "prom-rw").lstrip("/")
        self.secret: str | None = config.get("secret")

    def _check_deps(self):
        missing = []
        try:
            import snappy  # noqa: F401
        except ImportError:
            try:
                import cramjam  # noqa: F401
            except ImportError:
                missing.append("python-snappy or cramjam")
        try:
            import google.protobuf  # noqa: F401
        except ImportError:
            missing.append("protobuf")
        if missing:
            raise SourceError(
                f"PrometheusRW source requires: {', '.join(missing)} — "
                "install with: pip install tram[prometheus_rw]"
            )

    def _decompress(self, data: bytes) -> bytes:
        try:
            import snappy
            return snappy.decompress(data)
        except ImportError:
            pass
        try:
            import cramjam
            return bytes(cramjam.snappy.decompress(data))
        except ImportError:
            pass
        raise SourceError("No snappy library available")

    def _decode_write_request(self, data: bytes) -> list[dict]:
        """Decode a Prometheus WriteRequest protobuf into list of dicts."""
        try:
            # We parse manually using protobuf's descriptor pool approach
            # to avoid needing generated code.  Fall back to raw proto parsing.
            from google.protobuf import descriptor_pb2, descriptor_pool  # noqa: F401

            # Minimal Prometheus WriteRequest proto definition (inline)
            _PROTO_SRC = b"""
syntax = "proto3";
package prometheus;
message Label { string name = 1; string value = 2; }
message Sample { double value = 1; int64 timestamp = 2; }
message TimeSeries {
  repeated Label labels = 1;
  repeated Sample samples = 2;
}
message WriteRequest { repeated TimeSeries timeseries = 1; }
"""
            # Use dynamic parsing
            from google.protobuf.descriptor_pool import DescriptorPool
            from google.protobuf.message_factory import MessageFactory

            pool = DescriptorPool()
            file_proto = descriptor_pb2.FileDescriptorProto()
            file_proto.ParseFromString(self._compile_proto_descriptor(_PROTO_SRC))
            pool.Add(file_proto)

            factory = MessageFactory(pool=pool)
            write_req_desc = pool.FindMessageTypeByName("prometheus.WriteRequest")
            WriteRequest = factory.GetPrototype(write_req_desc)

            msg = WriteRequest()
            msg.ParseFromString(data)

            records = []
            for ts in msg.timeseries:
                labels = {lbl.name: lbl.value for lbl in ts.labels}
                for sample in ts.samples:
                    records.append({
                        **labels,
                        "__value__": sample.value,
                        "__timestamp_ms__": sample.timestamp,
                    })
            return records
        except Exception as exc:
            logger.warning("Proto decode failed, yielding raw: %s", exc)
            return [{"raw": data.hex()}]

    def _compile_proto_descriptor(self, proto_src: bytes) -> bytes:
        """Compile proto source to FileDescriptorProto bytes using protoc."""
        import os
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            proto_file = os.path.join(tmpdir, "prom.proto")
            with open(proto_file, "wb") as f:
                f.write(proto_src)

            out_file = os.path.join(tmpdir, "prom.pb")
            ret = subprocess.run(
                ["protoc", f"--descriptor_set_out={out_file}", proto_file],
                capture_output=True,
            )
            if ret.returncode != 0:
                raise SourceError(f"protoc failed: {ret.stderr.decode()}")

            from google.protobuf import descriptor_pb2
            fds = descriptor_pb2.FileDescriptorSet()
            with open(out_file, "rb") as f:
                fds.ParseFromString(f.read())

            return fds.file[0].SerializeToString()

    def test_connection(self) -> dict:
        path = self.config.get("path", "prom-rw").lstrip("/")
        return {"ok": True, "latency_ms": None, "detail": f"Local HTTP listener at /webhooks/{path}"}

    def read(self) -> Generator[tuple[bytes, dict]]:
        self._check_deps()

        import queue

        from tram.connectors.webhook.source import _REGISTRY_LOCK, _WEBHOOK_REGISTRY

        q: queue.SimpleQueue = queue.SimpleQueue()
        with _REGISTRY_LOCK:
            _WEBHOOK_REGISTRY[self.path] = q

        try:
            while True:
                try:
                    body, meta = q.get(timeout=1.0)
                    try:
                        decompressed = self._decompress(body)
                        records = self._decode_write_request(decompressed)
                    except Exception as exc:
                        logger.warning("PrometheusRW decode error: %s", exc)
                        continue

                    yield json.dumps(records).encode("utf-8"), {
                        **meta,
                        "source": "prometheus_rw",
                    }
                except queue.Empty:
                    continue
        finally:
            with _REGISTRY_LOCK:
                _WEBHOOK_REGISTRY.pop(self.path, None)
