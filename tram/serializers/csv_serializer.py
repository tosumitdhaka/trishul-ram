"""CSV serializer."""

from __future__ import annotations

import csv
import io

from tram.core.exceptions import SerializerError
from tram.interfaces.base_serializer import BaseSerializer
from tram.registry.registry import register_serializer


@register_serializer("csv")
class CsvSerializer(BaseSerializer):
    """Serialize/deserialize CSV data."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.delimiter: str = config.get("delimiter", ",")
        self.has_header: bool = config.get("has_header", True)
        self.quotechar: str = config.get("quotechar", '"')

    def parse(self, data: bytes) -> list[dict]:
        try:
            text = data.decode("utf-8-sig")  # handle BOM
            reader = csv.DictReader(
                io.StringIO(text),
                delimiter=self.delimiter,
                quotechar=self.quotechar,
            )
            if not self.has_header:
                # Fall back to positional keys: field_0, field_1, ...
                rows = list(csv.reader(io.StringIO(text), delimiter=self.delimiter))
                return [{f"field_{i}": v for i, v in enumerate(row)} for row in rows]
            return [dict(row) for row in reader]
        except Exception as exc:
            raise SerializerError(f"CSV parse error: {exc}") from exc

    def serialize(self, records: list[dict]) -> bytes:
        if not records:
            return b""
        try:
            buf = io.StringIO()
            fieldnames = list(records[0].keys())
            writer = csv.DictWriter(
                buf,
                fieldnames=fieldnames,
                delimiter=self.delimiter,
                quotechar=self.quotechar,
                extrasaction="ignore",
            )
            if self.has_header:
                writer.writeheader()
            writer.writerows(records)
            return buf.getvalue().encode("utf-8")
        except Exception as exc:
            raise SerializerError(f"CSV serialize error: {exc}") from exc
