"""ASN.1 serializer — decodes BER/DER/PER/XER/JER binary using a .asn schema file.

Requires asn1tools>=0.167 (install with: pip install tram[asn1])

Usage in pipeline YAML:
    serializer_in:
      type: asn1
      schema_file: /data/schemas/3gpp_32401.asn   # .asn file or directory of .asn files
      message_class: FileContent                   # top-level ASN.1 type to decode
      # OR:
      # message_classes: [CallEventRecord, GPRSRecord]
      encoding: ber                                # ber | der | per | uper | xer | jer (default: ber)
      split_records: false                         # BER only; split concatenated top-level TLVs

Decode only — ASN.1 serializer_out / encode is intentionally not supported.
Schema file is required; there is no schema-less fallback.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from tram.core.exceptions import SerializerError
from tram.interfaces.base_serializer import BaseSerializer
from tram.registry.registry import register_serializer

# Cache: (schema_key, encoding) -> compiled asn1tools file object
_SCHEMA_CACHE: dict[tuple[str, str], object] = {}


def _to_json_safe(obj):
    """Recursively convert asn1tools output to JSON-serializable types.

    - datetime  → ISO 8601 string
    - CHOICE    → {"type": name, "value": value}  (asn1tools returns 2-tuples)
    - bytes     → hex string
    - bytearray → hex string
    - tuple     → list (e.g. SEQUENCE OF decoded as tuple)
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, tuple) and len(obj) == 2 and isinstance(obj[0], str):
        # CHOICE: (type_name, value)
        return {"type": obj[0], "value": _to_json_safe(obj[1])}
    if isinstance(obj, (list, tuple)):
        return [_to_json_safe(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    return obj


def _parse_tag(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data):
        raise SerializerError("ASN.1 BER split error: unexpected EOF while reading tag")

    first = data[offset]
    pos = offset + 1
    tag_number = first & 0x1F

    if tag_number == 0x1F:
        tag_number = 0
        while True:
            if pos >= len(data):
                raise SerializerError("ASN.1 BER split error: unexpected EOF in long-form tag")
            b = data[pos]
            pos += 1
            tag_number = (tag_number << 7) | (b & 0x7F)
            if not (b & 0x80):
                break

    return pos, tag_number


def _parse_length(data: bytes, offset: int) -> tuple[int, int | None]:
    if offset >= len(data):
        raise SerializerError("ASN.1 BER split error: unexpected EOF while reading length")

    first = data[offset]
    if first < 0x80:
        return offset + 1, first
    if first == 0x80:
        return offset + 1, None

    num_bytes = first & 0x7F
    if num_bytes == 0:
        raise SerializerError("ASN.1 BER split error: invalid BER length with 0 length bytes")
    end = offset + 1 + num_bytes
    if end > len(data):
        raise SerializerError("ASN.1 BER split error: unexpected EOF in long-form length")
    return end, int.from_bytes(data[offset + 1:end], "big")


def _find_indefinite_end(data: bytes, offset: int) -> int:
    pos = offset
    while pos < len(data):
        if pos + 1 < len(data) and data[pos] == 0x00 and data[pos + 1] == 0x00:
            return pos + 2

        tag_end, _ = _parse_tag(data, pos)
        len_end, length = _parse_length(data, tag_end)
        if length is None:
            pos = _find_indefinite_end(data, len_end)
        else:
            pos = len_end + length

    raise SerializerError("ASN.1 BER split error: missing end-of-contents marker")


def _split_ber_records(data: bytes) -> list[bytes]:
    return list(_iter_ber_records(data))


def _iter_ber_records(data: bytes) -> Iterator[bytes]:
    offset = 0
    while offset < len(data):
        tag_end, _ = _parse_tag(data, offset)
        len_end, length = _parse_length(data, tag_end)
        end = _find_indefinite_end(data, len_end) if length is None else len_end + length
        if end > len(data):
            raise SerializerError("ASN.1 BER split error: record extends past end of payload")
        yield data[offset:end]
        offset = end


@register_serializer("asn1")
class Asn1Serializer(BaseSerializer):
    """Decode ASN.1 BER/DER/PER/XER/JER binary using a .asn schema file.

    Requires asn1tools>=0.167 — install with: pip install tram[asn1]
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        if "schema_file" not in config:
            raise SerializerError("ASN.1 serializer requires 'schema_file' config")
        has_message_class = bool(config.get("message_class"))
        has_message_classes = bool(config.get("message_classes"))
        if has_message_class == has_message_classes:
            raise SerializerError(
                "ASN.1 serializer requires exactly one of 'message_class' or 'message_classes'"
            )
        self.schema_file: str = os.path.abspath(config["schema_file"])
        self.message_class: str | None = config.get("message_class")
        self.message_classes: list[str] | None = config.get("message_classes")
        self.encoding: str = config.get("encoding", "ber")
        self.split_records: bool = bool(config.get("split_records", False))
        if self.split_records and self.encoding != "ber":
            raise SerializerError("ASN.1 serializer 'split_records' is only supported for BER")
        self._compiled = None

    def _get_compiled(self):
        """Return (and cache) the compiled asn1tools file object."""
        if self._compiled is not None:
            return self._compiled

        try:
            import asn1tools
        except ImportError as exc:
            raise SerializerError(
                "ASN.1 serializer requires asn1tools — install with: pip install tram[asn1]"
            ) from exc

        schema_path = self.schema_file
        if not os.path.exists(schema_path):
            raise SerializerError(f"ASN.1 schema not found: {schema_path}")

        # Build cache key: for a directory use its combined mtime, for a file use its mtime
        if os.path.isdir(schema_path):
            import glob as _glob
            files = sorted(_glob.glob(os.path.join(schema_path, "*.asn")))
            if not files:
                raise SerializerError(f"No .asn files found in directory: {schema_path}")
            cache_key = (schema_path + ":dir:" + str(sum(os.path.getmtime(f) for f in files)), self.encoding)
        else:
            files = [schema_path]
            cache_key = (schema_path + ":" + str(os.path.getmtime(schema_path)), self.encoding)

        if cache_key in _SCHEMA_CACHE:
            self._compiled = _SCHEMA_CACHE[cache_key]
            return self._compiled

        try:
            compiled = asn1tools.compile_files(files, self.encoding)
        except Exception as exc:
            raise SerializerError(f"ASN.1 schema compile error: {exc}") from exc

        _SCHEMA_CACHE[cache_key] = compiled
        self._compiled = compiled
        return compiled

    def _decode_record(self, compiled: Any, payload: bytes):
        roots = [self.message_class] if self.message_class else list(self.message_classes or [])
        errors: list[str] = []
        for root_type in roots:
            try:
                return compiled.decode(root_type, payload)
            except Exception as exc:
                errors.append(f"{root_type}: {exc}")

        joined = "; ".join(errors) if errors else "no candidate message classes configured"
        raise SerializerError(
            f"ASN.1 decode error (types={roots}, encoding={self.encoding}): {joined}"
        )

    @staticmethod
    def _wrap_result(decoded: Any) -> dict:
        safe = _to_json_safe(decoded)
        if isinstance(safe, dict):
            return safe
        return {"value": safe}

    def parse(self, data: bytes) -> list[dict]:
        compiled = self._get_compiled()
        payloads = _split_ber_records(data) if self.split_records else [data]
        try:
            return [self._wrap_result(self._decode_record(compiled, payload)) for payload in payloads]
        except SerializerError:
            raise
        except Exception as exc:
            roots = [self.message_class] if self.message_class else list(self.message_classes or [])
            raise SerializerError(
                f"ASN.1 decode error (types={roots}, encoding={self.encoding}): {exc}"
            ) from exc

    def parse_chunks(self, data: bytes, record_chunk_size: int) -> Iterator[list[dict]]:
        if record_chunk_size <= 0:
            yield self.parse(data)
            return

        compiled = self._get_compiled()
        payloads = _iter_ber_records(data) if self.split_records else iter([data])
        batch: list[dict] = []

        try:
            for payload in payloads:
                batch.append(self._wrap_result(self._decode_record(compiled, payload)))
                if len(batch) >= record_chunk_size:
                    yield batch
                    batch = []
            if batch:
                yield batch
        except SerializerError:
            raise
        except Exception as exc:
            roots = [self.message_class] if self.message_class else list(self.message_classes or [])
            raise SerializerError(
                f"ASN.1 decode error (types={roots}, encoding={self.encoding}): {exc}"
            ) from exc

    def serialize(self, records: list[dict]) -> bytes:
        raise SerializerError(
            "ASN.1 serializer is decode-only and does not support encode (serializer_out). "
            "Use a different serializer_out (e.g. type: json) to write ASN.1-decoded records."
        )
