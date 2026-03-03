"""Tests for Avro serializer."""
from __future__ import annotations
import io
import json
import sys
from unittest.mock import MagicMock, patch
import pytest
from tram.serializers.avro_serializer import AvroSerializer
from tram.core.exceptions import SerializerError

SCHEMA = json.dumps({"type": "record", "name": "Test", "fields": [{"name": "x", "type": "int"}]})

class TestAvroSerializer:
    def test_missing_schema_raises(self):
        with pytest.raises(SerializerError, match="schema"):
            AvroSerializer({})

    def test_parse_calls_fastavro_reader(self):
        mock_fastavro = MagicMock()
        mock_fastavro.parse_schema.return_value = "parsed"
        mock_fastavro.reader.return_value = [{"x": 1}, {"x": 2}]
        with patch.dict(sys.modules, {"fastavro": mock_fastavro}):
            s = AvroSerializer({"schema": SCHEMA})
            s._parsed_schema = "parsed"
            result = s.parse(b"fake_avro_bytes")
        assert result == [{"x": 1}, {"x": 2}]

    def test_serialize_calls_fastavro_writer(self):
        mock_fastavro = MagicMock()
        mock_fastavro.parse_schema.return_value = "parsed"
        def fake_writer(buf, schema, records):
            buf.write(b"avro_data")
        mock_fastavro.writer.side_effect = fake_writer
        with patch.dict(sys.modules, {"fastavro": mock_fastavro}):
            s = AvroSerializer({"schema": SCHEMA})
            s._parsed_schema = "parsed"
            result = s.serialize([{"x": 1}])
        assert result == b"avro_data"

    def test_parse_import_error(self):
        with patch.dict(sys.modules, {"fastavro": None}):
            s = AvroSerializer({"schema": SCHEMA})
            with pytest.raises(SerializerError, match="fastavro"):
                s.parse(b"data")

    def test_serialize_import_error(self):
        with patch.dict(sys.modules, {"fastavro": None}):
            s = AvroSerializer({"schema": SCHEMA})
            with pytest.raises(SerializerError, match="fastavro"):
                s.serialize([{"x": 1}])

    def test_schema_file_config(self, tmp_path):
        schema_path = tmp_path / "test.avsc"
        schema_path.write_text(SCHEMA)
        mock_fastavro = MagicMock()
        mock_fastavro.parse_schema.return_value = "parsed"
        mock_fastavro.reader.return_value = [{"x": 42}]
        with patch.dict(sys.modules, {"fastavro": mock_fastavro}):
            s = AvroSerializer({"schema_file": str(schema_path)})
            result = s.parse(b"fake_avro")
        assert result == [{"x": 42}]
