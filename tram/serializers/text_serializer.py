"""Text (line-by-line) serializer — each line becomes one record."""

from __future__ import annotations

from tram.core.exceptions import SerializerError
from tram.interfaces.base_serializer import BaseSerializer
from tram.registry.registry import register_serializer


@register_serializer("text")
class TextSerializer(BaseSerializer):
    """Line-oriented text serializer.

    parse() splits input on newlines; each non-empty line becomes one record:
        {"_line": "<content>", "_line_num": <1-based int>}

    serialize() joins the ``line_field`` value of each record with newlines.
    Records that lack the field are JSON-encoded inline as a fallback.

    Config keys:
        encoding         (str,  default "utf-8")    Input/output encoding.
        skip_empty       (bool, default True)        Skip blank lines on parse.
        line_field       (str,  default "_line")     Field name for line content.
        include_line_num (bool, default True)        Add ``_line_num`` to each record.
        newline          (str,  default "\\n")        Line separator for serialize().

    Use cases:
        - Syslog / auth.log / messages file ingestion
        - Filebeat / Fluentd line-oriented output
        - Any text file where one logical record = one line
        - Combined with regex_extract transform to parse structured log lines
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.encoding: str = config.get("encoding", "utf-8")
        self.skip_empty: bool = config.get("skip_empty", True)
        self.line_field: str = config.get("line_field", "_line")
        self.include_line_num: bool = config.get("include_line_num", True)
        self.newline: str = config.get("newline", "\n")

    def parse(self, data: bytes) -> list[dict]:
        try:
            text = data.decode(self.encoding)
        except Exception as exc:
            raise SerializerError(f"TextSerializer decode error: {exc}") from exc

        records = []
        line_num = 0
        for raw_line in text.splitlines():
            line_num += 1
            if self.skip_empty and not raw_line.strip():
                continue
            record: dict = {self.line_field: raw_line}
            if self.include_line_num:
                record["_line_num"] = line_num
            records.append(record)
        return records

    def serialize(self, records: list[dict]) -> bytes:
        import json
        lines = []
        for record in records:
            if self.line_field in record:
                lines.append(str(record[self.line_field]))
            else:
                # Record doesn't have the line field — JSON-encode it inline
                try:
                    lines.append(json.dumps(record))
                except Exception as exc:
                    raise SerializerError(f"TextSerializer serialize error: {exc}") from exc
        try:
            return self.newline.join(lines).encode(self.encoding)
        except Exception as exc:
            raise SerializerError(f"TextSerializer encode error: {exc}") from exc
