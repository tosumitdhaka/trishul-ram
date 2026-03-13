"""Bytes (passthrough) serializer — wraps raw binary in a dict envelope."""

from __future__ import annotations

import base64

from tram.core.exceptions import SerializerError
from tram.interfaces.base_serializer import BaseSerializer
from tram.registry.registry import register_serializer


@register_serializer("bytes")
class BytesSerializer(BaseSerializer):
    """Pass-through binary serializer for opaque / binary payloads.

    parse() wraps the entire raw payload into a single record:
        {
            "_raw":  "<base64 or hex string, or absent>",
            "_size": <byte count>,
        }

    serialize() reconstructs bytes from the ``_raw`` field of the first record.
    If ``_raw`` is absent (e.g. records were produced by upstream transforms),
    the records are JSON-encoded as a UTF-8 fallback.

    Config keys:
        encoding  (str, default "base64")
                  "base64" — _raw contains a base64-encoded string
                  "hex"    — _raw contains a hex-encoded string
                  "none"   — _raw field omitted; only _size is stored

    Use cases:
        - File copy / protocol-bridge pipelines (SFTP→S3, FTP→GCS) with no
          content parsing needed
        - Binary protocol forwarding (REST→REST, SFTP→Kafka)
        - Archival pipelines where content is opaque
        - When serializer_out: {type: bytes} is paired with a REST/S3/GCS sink,
          the original bytes are written verbatim
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.encoding: str = config.get("encoding", "base64")
        if self.encoding not in ("base64", "hex", "none"):
            raise SerializerError(
                f"BytesSerializer: invalid encoding {self.encoding!r}; "
                "must be 'base64', 'hex', or 'none'"
            )

    def parse(self, data: bytes) -> list[dict]:
        record: dict = {"_size": len(data)}
        if self.encoding == "base64":
            record["_raw"] = base64.b64encode(data).decode("ascii")
        elif self.encoding == "hex":
            record["_raw"] = data.hex()
        # encoding="none" → no _raw field; pipeline can still use add_field / rename on _size
        return [record]

    def serialize(self, records: list[dict]) -> bytes:
        if not records:
            return b""
        record = records[0]
        raw = record.get("_raw")
        if raw is None:
            # No _raw — JSON-encode all records as fallback
            import json
            try:
                return json.dumps(records).encode("utf-8")
            except Exception as exc:
                raise SerializerError(f"BytesSerializer fallback JSON error: {exc}") from exc
        try:
            if self.encoding == "base64":
                return base64.b64decode(raw)
            elif self.encoding == "hex":
                return bytes.fromhex(raw)
            else:
                return raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        except Exception as exc:
            raise SerializerError(f"BytesSerializer decode error: {exc}") from exc
