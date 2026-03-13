"""NDJSON (Newline-Delimited JSON) serializer — one JSON object per line."""

from __future__ import annotations

import json

from tram.core.exceptions import SerializerError
from tram.interfaces.base_serializer import BaseSerializer
from tram.registry.registry import register_serializer


@register_serializer("ndjson")
class NdjsonSerializer(BaseSerializer):
    """Newline-Delimited JSON (JSON Lines) serializer.

    parse() splits input on newlines and parses each non-empty line as a JSON
    object or array. Arrays are flattened into the record stream.

    serialize() writes one JSON object per line with an optional trailing newline.

    Config keys:
        ensure_ascii   (bool, default True)   Escape non-ASCII characters.
        strict         (bool, default False)  Raise on lines that are not JSON
                                              objects (dicts). When False, arrays
                                              are flattened and scalar lines are
                                              wrapped in {"_value": <val>}.
        newline        (str,  default "\\n")  Line separator for serialize().

    Use cases:
        - Kafka consumer / producer with JSON Lines payloads
        - Filebeat / Fluentd / Vector JSON output
        - jq-generated output (one object per line)
        - Log aggregation pipelines where each event is one JSON line
        - Unlike json serializer, handles streams of objects without a wrapping array
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.ensure_ascii: bool = config.get("ensure_ascii", True)
        self.strict: bool = config.get("strict", False)
        self.newline: str = config.get("newline", "\n")

    def parse(self, data: bytes) -> list[dict]:
        try:
            text = data.decode("utf-8")
        except Exception as exc:
            raise SerializerError(f"NDJSON decode error: {exc}") from exc

        records = []
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SerializerError(
                    f"NDJSON parse error on line {lineno}: {exc}"
                ) from exc

            if isinstance(obj, dict):
                records.append(obj)
            elif isinstance(obj, list):
                if self.strict:
                    raise SerializerError(
                        f"NDJSON strict mode: line {lineno} is a JSON array, expected object"
                    )
                for item in obj:
                    records.append(item if isinstance(item, dict) else {"_value": item})
            else:
                if self.strict:
                    raise SerializerError(
                        f"NDJSON strict mode: line {lineno} is a scalar, expected object"
                    )
                records.append({"_value": obj})

        return records

    def serialize(self, records: list[dict]) -> bytes:
        lines = []
        for record in records:
            try:
                lines.append(json.dumps(record, ensure_ascii=self.ensure_ascii))
            except Exception as exc:
                raise SerializerError(f"NDJSON serialize error: {exc}") from exc
        try:
            return self.newline.join(lines).encode("utf-8")
        except Exception as exc:
            raise SerializerError(f"NDJSON encode error: {exc}") from exc
