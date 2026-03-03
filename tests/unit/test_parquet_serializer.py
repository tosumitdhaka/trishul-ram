"""Tests for Parquet serializer."""
from __future__ import annotations
import sys
from unittest.mock import MagicMock, patch
import pytest
from tram.serializers.parquet_serializer import ParquetSerializer
from tram.core.exceptions import SerializerError

class TestParquetSerializer:
    def test_parse_calls_read_table(self):
        mock_pq = MagicMock()
        mock_table = MagicMock()
        mock_table.to_pylist.return_value = [{"a": 1}]
        mock_pq.read_table.return_value = mock_table
        mock_pa = MagicMock()
        mock_pa.parquet = mock_pq
        with patch.dict(sys.modules, {"pyarrow": mock_pa, "pyarrow.parquet": mock_pq}):
            s = ParquetSerializer({})
            result = s.parse(b"fake_parquet")
        assert result == [{"a": 1}]

    def test_serialize_calls_write_table(self):
        mock_pq = MagicMock()
        mock_pa = MagicMock()
        mock_table = MagicMock()
        mock_pa.Table.from_pylist.return_value = mock_table
        mock_pa.parquet = mock_pq
        def fake_write(table, buf, compression=None):
            buf.write(b"parquet_bytes")
        mock_pq.write_table.side_effect = fake_write
        with patch.dict(sys.modules, {"pyarrow": mock_pa, "pyarrow.parquet": mock_pq}):
            s = ParquetSerializer({})
            result = s.serialize([{"a": 1}])
        assert result == b"parquet_bytes"

    def test_compression_default(self):
        s = ParquetSerializer({})
        assert s.compression == "snappy"

    def test_compression_custom(self):
        s = ParquetSerializer({"compression": "gzip"})
        assert s.compression == "gzip"

    def test_import_error_parse(self):
        with patch.dict(sys.modules, {"pyarrow": None, "pyarrow.parquet": None}):
            s = ParquetSerializer({})
            with pytest.raises(SerializerError, match="pyarrow"):
                s.parse(b"data")

    def test_import_error_serialize(self):
        with patch.dict(sys.modules, {"pyarrow": None, "pyarrow.parquet": None}):
            s = ParquetSerializer({})
            with pytest.raises(SerializerError, match="pyarrow"):
                s.serialize([{"a": 1}])
