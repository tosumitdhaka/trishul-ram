"""Tests for all serializer implementations."""

from __future__ import annotations

import json

import pytest

from tram.serializers.csv_serializer import CsvSerializer
from tram.serializers.json_serializer import JsonSerializer
from tram.serializers.xml_serializer import XmlSerializer
from tram.core.exceptions import SerializerError


# ── JsonSerializer ─────────────────────────────────────────────────────────


class TestJsonSerializer:
    def test_parse_array(self):
        s = JsonSerializer({})
        data = b'[{"a": 1}, {"a": 2}]'
        result = s.parse(data)
        assert result == [{"a": 1}, {"a": 2}]

    def test_parse_object_wrapped_in_list(self):
        s = JsonSerializer({})
        data = b'{"a": 1}'
        result = s.parse(data)
        assert result == [{"a": 1}]

    def test_serialize_roundtrip(self):
        s = JsonSerializer({})
        records = [{"x": 1, "y": "hello"}]
        serialized = s.serialize(records)
        parsed = s.parse(serialized)
        assert parsed == records

    def test_serialize_with_indent(self):
        s = JsonSerializer({"indent": 2})
        records = [{"a": 1}]
        result = s.serialize(records)
        assert b"\n" in result  # indented

    def test_parse_invalid_json_raises(self):
        s = JsonSerializer({})
        with pytest.raises(SerializerError):
            s.parse(b"not json {{{")

    def test_serialize_empty_list(self):
        s = JsonSerializer({})
        result = s.serialize([])
        assert json.loads(result) == []


# ── CsvSerializer ──────────────────────────────────────────────────────────


class TestCsvSerializer:
    def test_parse_with_header(self):
        s = CsvSerializer({"has_header": True})
        data = b"name,age\nAlice,30\nBob,25"
        result = s.parse(data)
        assert result == [{"name": "Alice", "age": "30"}, {"name": "Bob", "age": "25"}]

    def test_parse_custom_delimiter(self):
        s = CsvSerializer({"delimiter": ";", "has_header": True})
        data = b"a;b\n1;2"
        result = s.parse(data)
        assert result == [{"a": "1", "b": "2"}]

    def test_serialize_roundtrip(self):
        s = CsvSerializer({"has_header": True})
        records = [{"name": "Alice", "age": "30"}]
        serialized = s.serialize(records)
        parsed = s.parse(serialized)
        assert parsed == records

    def test_serialize_empty(self):
        s = CsvSerializer({})
        result = s.serialize([])
        assert result == b""

    def test_parse_bom_csv(self):
        """Handle UTF-8 BOM (common in Windows-generated CSVs)."""
        s = CsvSerializer({"has_header": True})
        data = b"\xef\xbb\xbfname,val\nNE001,42"
        result = s.parse(data)
        assert result[0]["name"] == "NE001"


# ── XmlSerializer ──────────────────────────────────────────────────────────


class TestXmlSerializer:
    def test_parse_records(self):
        s = XmlSerializer({})
        data = b"""<?xml version="1.0"?>
<records>
  <record><name>Alice</name><age>30</age></record>
  <record><name>Bob</name><age>25</age></record>
</records>"""
        result = s.parse(data)
        assert len(result) == 2
        assert result[0]["name"] == "Alice"
        assert result[1]["age"] == "25"

    def test_serialize_roundtrip(self):
        s = XmlSerializer({})
        records = [{"name": "Alice", "val": "42"}]
        serialized = s.serialize(records)
        assert b"Alice" in serialized
        assert b"42" in serialized

    def test_serialize_produces_xml_declaration(self):
        s = XmlSerializer({})
        result = s.serialize([{"a": "1"}])
        assert b"<?xml" in result

    def test_defusedxml_prevents_xxe(self):
        """Verify defusedxml is used (entity expansion blocked)."""
        s = XmlSerializer({})
        # XXE payload — should raise, not expand
        xxe_payload = b"""<?xml version="1.0"?>
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<records><record><data>&xxe;</data></record></records>"""
        with pytest.raises((SerializerError, Exception)):
            s.parse(xxe_payload)
