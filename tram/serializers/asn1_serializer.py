"""ASN.1 serializer — decodes BER/DER/PER/XER/JER binary using a .asn schema file.

Requires asn1tools>=0.167 (install with: pip install tram[asn1])

Usage in pipeline YAML:
    serializer_in:
      type: asn1
      schema_file: /data/schemas/3gpp_32401.asn   # .asn file or directory of .asn files
      message_class: FileContent                   # top-level ASN.1 type to decode
      encoding: ber                                # ber | der | per | uper | xer | jer (default: ber)

Deserialize only — encode path (ASN.1 sink output) is not supported.
Schema file is required; there is no schema-less fallback.
"""
from __future__ import annotations

import os
from datetime import datetime
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
    if isinstance(obj, bytes):
        return obj.hex()
    return obj


@register_serializer("asn1")
class Asn1Serializer(BaseSerializer):
    """Deserialize ASN.1 BER/DER/PER/XER/JER binary using a .asn schema file.

    Requires asn1tools>=0.167 — install with: pip install tram[asn1]
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        if "schema_file" not in config:
            raise SerializerError("ASN.1 serializer requires 'schema_file' config")
        if "message_class" not in config:
            raise SerializerError("ASN.1 serializer requires 'message_class' config")
        self.schema_file: str = os.path.abspath(config["schema_file"])
        self.message_class: str = config["message_class"]
        self.encoding: str = config.get("encoding", "ber")
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

    def parse(self, data: bytes) -> list[dict]:
        compiled = self._get_compiled()
        try:
            decoded = compiled.decode(self.message_class, data)
        except Exception as exc:
            raise SerializerError(
                f"ASN.1 decode error (type={self.message_class}, encoding={self.encoding}): {exc}"
            ) from exc
        safe = _to_json_safe(decoded)
        # asn1tools returns a dict for SEQUENCE; wrap scalars in a record
        if isinstance(safe, dict):
            return [safe]
        return [{"value": safe}]

    def serialize(self, records: list[dict]) -> bytes:
        raise SerializerError(
            "ASN.1 serializer does not support encode (serializer_out). "
            "Use a different serializer_out (e.g. type: json) to write ASN.1-decoded records."
        )
