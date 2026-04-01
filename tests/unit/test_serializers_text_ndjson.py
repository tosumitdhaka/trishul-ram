"""Tests for TextSerializer and NdjsonSerializer."""
from __future__ import annotations

import json

import pytest

from tram.core.exceptions import SerializerError
from tram.serializers.ndjson_serializer import NdjsonSerializer
from tram.serializers.text_serializer import TextSerializer

# ── TextSerializer ────────────────────────────────────────────────────────────


class TestTextSerializer:
    def _make(self, extra: dict | None = None) -> TextSerializer:
        cfg = {}
        if extra:
            cfg.update(extra)
        return TextSerializer(cfg)

    def test_default_config(self):
        s = self._make()
        assert s.encoding == "utf-8"
        assert s.skip_empty is True
        assert s.line_field == "_line"
        assert s.include_line_num is True

    def test_parse_basic_lines(self):
        s = self._make()
        records = s.parse(b"hello\nworld\n")
        assert len(records) == 2
        assert records[0]["_line"] == "hello"
        assert records[0]["_line_num"] == 1
        assert records[1]["_line"] == "world"
        assert records[1]["_line_num"] == 2

    def test_parse_skips_empty_lines(self):
        s = self._make()
        records = s.parse(b"line1\n\nline2")
        assert len(records) == 2

    def test_parse_keeps_empty_lines_when_skip_empty_false(self):
        s = self._make({"skip_empty": False})
        records = s.parse(b"line1\n\nline2")
        assert len(records) == 3

    def test_parse_no_line_num_when_disabled(self):
        s = self._make({"include_line_num": False})
        records = s.parse(b"hello")
        assert "_line_num" not in records[0]

    def test_parse_custom_line_field(self):
        s = self._make({"line_field": "msg"})
        records = s.parse(b"hello")
        assert "msg" in records[0]
        assert records[0]["msg"] == "hello"

    def test_parse_bad_encoding_raises(self):
        s = self._make({"encoding": "ascii"})
        with pytest.raises(SerializerError, match="decode error"):
            s.parse("héllo".encode())

    def test_serialize_joins_lines(self):
        s = self._make()
        records = [{"_line": "a"}, {"_line": "b"}]
        result = s.serialize(records)
        assert result == b"a\nb"

    def test_serialize_fallback_json_for_missing_field(self):
        s = self._make()
        records = [{"key": "value"}]
        result = s.serialize(records)
        obj = json.loads(result.decode())
        assert obj["key"] == "value"

    def test_parse_empty_input(self):
        s = self._make()
        assert s.parse(b"") == []

    def test_serialize_custom_newline(self):
        s = self._make({"newline": "\r\n"})
        records = [{"_line": "a"}, {"_line": "b"}]
        result = s.serialize(records)
        assert b"\r\n" in result

    def test_registry_key(self):
        from tram.registry.registry import get_serializer
        cls = get_serializer("text")
        assert cls is TextSerializer


# ── NdjsonSerializer ──────────────────────────────────────────────────────────


class TestNdjsonSerializer:
    def _make(self, extra: dict | None = None) -> NdjsonSerializer:
        cfg = {}
        if extra:
            cfg.update(extra)
        return NdjsonSerializer(cfg)

    def test_default_config(self):
        s = self._make()
        assert s.ensure_ascii is True
        assert s.strict is False

    def test_parse_objects(self):
        s = self._make()
        data = b'{"a": 1}\n{"b": 2}\n'
        records = s.parse(data)
        assert records == [{"a": 1}, {"b": 2}]

    def test_parse_skips_empty_lines(self):
        s = self._make()
        data = b'{"a": 1}\n\n{"b": 2}'
        records = s.parse(data)
        assert len(records) == 2

    def test_parse_flattens_arrays(self):
        s = self._make()
        data = b'[{"x": 1}, {"x": 2}]'
        records = s.parse(data)
        assert records == [{"x": 1}, {"x": 2}]

    def test_parse_scalar_wrapped(self):
        s = self._make()
        data = b"42"
        records = s.parse(data)
        assert records == [{"_value": 42}]

    def test_parse_strict_rejects_array(self):
        s = self._make({"strict": True})
        with pytest.raises(SerializerError, match="array"):
            s.parse(b"[1,2,3]")

    def test_parse_strict_rejects_scalar(self):
        s = self._make({"strict": True})
        with pytest.raises(SerializerError, match="scalar"):
            s.parse(b"123")

    def test_parse_invalid_json_raises(self):
        s = self._make()
        with pytest.raises(SerializerError, match="parse error"):
            s.parse(b"not json")

    def test_serialize_roundtrip(self):
        s = self._make()
        records = [{"a": 1}, {"b": 2}]
        raw = s.serialize(records)
        lines = raw.decode().splitlines()
        assert json.loads(lines[0]) == {"a": 1}
        assert json.loads(lines[1]) == {"b": 2}

    def test_serialize_empty(self):
        s = self._make()
        assert s.serialize([]) == b""

    def test_registry_key(self):
        from tram.registry.registry import get_serializer
        cls = get_serializer("ndjson")
        assert cls is NdjsonSerializer
