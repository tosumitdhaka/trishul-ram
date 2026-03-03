"""MessagePack serializer."""
from __future__ import annotations
from tram.core.exceptions import SerializerError
from tram.interfaces.base_serializer import BaseSerializer
from tram.registry.registry import register_serializer

@register_serializer("msgpack")
class MsgpackSerializer(BaseSerializer):
    """Serialize/deserialize MessagePack data. Requires msgpack>=1.0."""

    def parse(self, data: bytes) -> list[dict]:
        try:
            import msgpack
        except ImportError as exc:
            raise SerializerError("Msgpack serializer requires msgpack — install with: pip install tram[msgpack_ser]") from exc
        try:
            result = msgpack.unpackb(data, raw=False)
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return [result]
            raise SerializerError(f"Expected msgpack object or array, got {type(result).__name__}")
        except SerializerError:
            raise
        except Exception as exc:
            raise SerializerError(f"Msgpack parse error: {exc}") from exc

    def serialize(self, records: list[dict]) -> bytes:
        try:
            import msgpack
        except ImportError as exc:
            raise SerializerError("Msgpack serializer requires msgpack — install with: pip install tram[msgpack_ser]") from exc
        try:
            return msgpack.packb(records, use_bin_type=True)
        except Exception as exc:
            raise SerializerError(f"Msgpack serialize error: {exc}") from exc
