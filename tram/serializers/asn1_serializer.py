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
      # OR (GH #19) split the record list inside a single decoded document:
      split_path: stats.measurement                # dot-notation path to the record list
      split_path_context:                          # dict copied into every split record
        vendor: Ericsson

Decode only — ASN.1 serializer_out / encode is intentionally not supported.
Schema file is required; there is no schema-less fallback.
"""
from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime
from typing import Any

from tram.core.exceptions import SerializerError
from tram.interfaces.base_serializer import BaseSerializer
from tram.registry.registry import register_serializer

# Cache: (content_hash, encoding) -> compiled asn1tools file object.
# Keyed by content hash (not mtime) so asset-sync mtime churn on identical
# content does not grow the cache unboundedly; bounded LRU caps memory in
# long-lived worker processes.
_SCHEMA_CACHE: OrderedDict[tuple[str, str], object] = OrderedDict()
_SCHEMA_CACHE_MAX = 32


def _schema_files(schema_path: str) -> list[str]:
    """Return the sorted .asn files that feed compilation for *schema_path*."""
    if os.path.isdir(schema_path):
        import glob as _glob
        files = sorted(_glob.glob(os.path.join(schema_path, "*.asn")))
        if not files:
            raise SerializerError(f"No .asn files found in directory: {schema_path}")
        return files
    return [schema_path]


def _schema_content_key(files: list[str]) -> str:
    """Content hash (sha256) of schema file names + bytes — mtime-agnostic.

    Identical schema content — even at a different path or with a different
    mtime — maps to the same cache key, so re-syncing unchanged assets never
    triggers a recompile.
    """
    hasher = hashlib.sha256()
    for path in files:
        hasher.update(os.path.basename(path).encode("utf-8"))
        hasher.update(b"\x00")
        with open(path, "rb") as fh:
            hasher.update(fh.read())
    return hasher.hexdigest()


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


def _resolve_split_target(document: dict, split_path: str) -> list:
    """Resolve a dot-notation split_path to the record list in a decoded
    document. Fails loud with a path-naming error on a missing segment or a
    non-list target (RCA #19)."""
    current: Any = document
    for part in split_path.split("."):
        if not isinstance(current, dict):
            raise SerializerError(
                f"ASN.1 split_path '{split_path}' not addressable at segment "
                f"'{part}' (parent is {type(current).__name__}, expected an object)"
            )
        if part not in current:
            raise SerializerError(
                f"ASN.1 split_path '{split_path}' not found in decoded document "
                f"(missing segment '{part}')"
            )
        current = current[part]
    if not isinstance(current, list):
        raise SerializerError(
            f"ASN.1 split_path '{split_path}' does not resolve to a list "
            f"(found {type(current).__name__})"
        )
    return current


def _emit_split_record(element: Any, context: dict | None) -> dict:
    """Merge a deep copy of the shared context into one split record.

    Each record gets its own deep copy so an in-place mutating transform
    cannot leak changes across records (RCA #19 aliasing hazard). The
    record's own fields take precedence over the ambient context.

    The record element itself is only shallow-copied: each decoded element
    is a distinct object (records never share sub-objects with each other),
    and the decoded document is dropped after the fan-out, so nothing can
    observe the sharing.
    """
    if isinstance(element, dict):
        if context is None:
            return dict(element)
        return {**deepcopy(context), **element}
    if context is None:
        return {"value": element}
    return {**deepcopy(context), "value": element}


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
        self.split_path: str | None = config.get("split_path")
        self.split_path_context: dict | None = config.get("split_path_context")
        if self.split_records and self.split_path:
            raise SerializerError(
                "ASN.1 serializer 'split_records' and 'split_path' are mutually exclusive"
            )
        if self.split_path is None and self.split_path_context is not None:
            raise SerializerError(
                "ASN.1 serializer 'split_path_context' requires 'split_path' to be configured"
            )
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

        files = _schema_files(schema_path)
        cache_key = (_schema_content_key(files), self.encoding)

        try:
            _SCHEMA_CACHE.move_to_end(cache_key)
        except KeyError:
            pass  # evicted concurrently — fall through to the compile path
        else:
            self._compiled = _SCHEMA_CACHE[cache_key]
            return self._compiled

        try:
            compiled = asn1tools.compile_files(files, self.encoding)
        except Exception as exc:
            raise SerializerError(f"ASN.1 schema compile error: {exc}") from exc

        _SCHEMA_CACHE[cache_key] = compiled
        if len(_SCHEMA_CACHE) > _SCHEMA_CACHE_MAX:
            _SCHEMA_CACHE.popitem(last=False)
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
            records: list[dict] = []
            for payload in payloads:
                decoded = self._wrap_result(self._decode_record(compiled, payload))
                if self.split_path:
                    # parse() is the non-incremental path (threaded batch runs
                    # and un-chunked sequential runs), so it fans out eagerly.
                    # The bounded-memory path is parse_chunks().
                    target = _resolve_split_target(decoded, self.split_path)
                    records.extend(
                        _emit_split_record(record, self.split_path_context) for record in target
                    )
                else:
                    records.append(decoded)
            return records
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
                decoded = self._wrap_result(self._decode_record(compiled, payload))
                if self.split_path:
                    # Lazy fan-out: merge only one batch's worth of records at a
                    # time instead of materializing the whole split list, which
                    # is the memory-bound point of GH #19.
                    target = _resolve_split_target(decoded, self.split_path)
                    for record in target:
                        batch.append(_emit_split_record(record, self.split_path_context))
                        if len(batch) >= record_chunk_size:
                            yield batch
                            batch = []
                else:
                    batch.append(decoded)
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
