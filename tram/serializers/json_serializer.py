"""JSON serializer."""

from __future__ import annotations

import json

from tram.core.exceptions import SerializerError
from tram.interfaces.base_serializer import BaseSerializer
from tram.registry.registry import register_serializer


@register_serializer("json")
class JsonSerializer(BaseSerializer):
    """Serialize/deserialize JSON data."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.indent: int | None = config.get("indent")
        self.ensure_ascii: bool = config.get("ensure_ascii", True)

    def parse(self, data: bytes) -> list[dict]:
        try:
            decoded = data.decode("utf-8")
            parsed = json.loads(decoded)
        except Exception as exc:
            raise SerializerError(f"JSON parse error: {exc}") from exc

        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
        raise SerializerError(f"Expected JSON object or array, got {type(parsed).__name__}")

    def serialize(self, records: list[dict]) -> bytes:
        try:
            return json.dumps(records, indent=self.indent, ensure_ascii=self.ensure_ascii).encode("utf-8")
        except Exception as exc:
            raise SerializerError(f"JSON serialize error: {exc}") from exc
