"""Tests for InfluxDB source and sink connectors."""
from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.influxdb.sink import InfluxDbSink
from tram.connectors.influxdb.source import InfluxDbSource
from tram.core.exceptions import SinkError, SourceError


class TestInfluxDbSource:
    def test_import_error_raises_source_error(self):
        with patch.dict(sys.modules, {"influxdb_client": None}):
            source = InfluxDbSource({"url": "http://localhost:8086", "token": "t", "org": "o", "query": "q"})
            with pytest.raises(SourceError, match="influxdb-client"):
                list(source.read())

    def test_query_returns_records(self):
        mock_record1 = MagicMock()
        mock_record1.values = {"_time": "2024-01-01", "_value": 42, "_field": "temp"}
        mock_record2 = MagicMock()
        mock_record2.values = {"_time": "2024-01-02", "_value": 43, "_field": "temp"}
        mock_table = MagicMock()
        mock_table.records = [mock_record1, mock_record2]
        mock_query_api = MagicMock()
        mock_query_api.query.return_value = [mock_table]
        mock_client_instance = MagicMock()
        mock_client_instance.query_api.return_value = mock_query_api
        mock_influx = MagicMock()
        mock_influx.InfluxDBClient.return_value = mock_client_instance

        with patch.dict(sys.modules, {"influxdb_client": mock_influx}):
            source = InfluxDbSource({"url": "http://localhost:8086", "token": "t", "org": "o", "query": 'from(bucket:"b")'})
            results = list(source.read())

        assert len(results) == 1
        data = json.loads(results[0][0])
        assert len(data) == 2

    def test_meta_has_row_count(self):
        mock_table = MagicMock()
        mock_table.records = [MagicMock(values={"x": 1})]
        mock_query_api = MagicMock()
        mock_query_api.query.return_value = [mock_table]
        mock_client_instance = MagicMock()
        mock_client_instance.query_api.return_value = mock_query_api
        mock_influx = MagicMock()
        mock_influx.InfluxDBClient.return_value = mock_client_instance

        with patch.dict(sys.modules, {"influxdb_client": mock_influx}):
            source = InfluxDbSource({"url": "http://localhost:8086", "token": "t", "org": "o", "query": "q"})
            _, meta = list(source.read())[0]

        assert meta["row_count"] == 1


class TestInfluxDbSink:
    def test_import_error_raises_sink_error(self):
        with patch.dict(sys.modules, {"influxdb_client": None}):
            sink = InfluxDbSink({"url": "http://localhost:8086", "token": "t", "org": "o", "bucket": "b", "measurement": "m"})
            with pytest.raises(SinkError, match="influxdb-client"):
                sink.write(b'[{"x":1}]', {})

    def test_write_points_called(self):
        mock_point_instance = MagicMock()
        mock_point_instance.tag.return_value = mock_point_instance
        mock_point_instance.field.return_value = mock_point_instance
        mock_point_instance.time.return_value = mock_point_instance
        mock_write_api = MagicMock()
        mock_client_instance = MagicMock()
        mock_client_instance.write_api.return_value = mock_write_api
        mock_influx = MagicMock()
        mock_influx.InfluxDBClient.return_value = mock_client_instance
        mock_influx.Point.return_value = mock_point_instance
        mock_influx.WritePrecision.NANOSECONDS = "ns"
        mock_influx.WritePrecision.MICROSECONDS = "us"
        mock_influx.WritePrecision.MILLISECONDS = "ms"
        mock_influx.WritePrecision.SECONDS = "s"
        mock_write_options = MagicMock()
        mock_influx_client = MagicMock()
        mock_influx_client.write_api.SYNCHRONOUS = mock_write_options

        with patch.dict(sys.modules, {
            "influxdb_client": mock_influx,
            "influxdb_client.client": mock_influx_client,
            "influxdb_client.client.write_api": mock_influx_client.write_api,
        }):
            sink = InfluxDbSink({
                "url": "http://localhost:8086", "token": "t", "org": "o",
                "bucket": "b", "measurement": "m",
            })
            sink.write(b'[{"temp": 42.0, "host": "server1"}]', {})

        mock_write_api.write.assert_called_once()

    def test_tag_fields_used_as_tags(self):
        mock_point_instance = MagicMock()
        mock_point_instance.tag.return_value = mock_point_instance
        mock_point_instance.field.return_value = mock_point_instance
        mock_point_instance.time.return_value = mock_point_instance
        mock_write_api = MagicMock()
        mock_client_instance = MagicMock()
        mock_client_instance.write_api.return_value = mock_write_api
        mock_influx = MagicMock()
        mock_influx.InfluxDBClient.return_value = mock_client_instance
        mock_influx.Point.return_value = mock_point_instance
        mock_influx.WritePrecision.NANOSECONDS = "ns"
        mock_influx.WritePrecision.MICROSECONDS = "us"
        mock_influx.WritePrecision.MILLISECONDS = "ms"
        mock_influx.WritePrecision.SECONDS = "s"
        mock_write_options = MagicMock()
        mock_influx_client = MagicMock()
        mock_influx_client.write_api.SYNCHRONOUS = mock_write_options

        with patch.dict(sys.modules, {
            "influxdb_client": mock_influx,
            "influxdb_client.client": mock_influx_client,
            "influxdb_client.client.write_api": mock_influx_client.write_api,
        }):
            sink = InfluxDbSink({
                "url": "http://localhost:8086", "token": "t", "org": "o",
                "bucket": "b", "measurement": "m", "tag_fields": ["host"],
            })
            sink.write(b'[{"temp": 42.0, "host": "server1"}]', {})

        mock_point_instance.tag.assert_called_with("host", "server1")
        mock_point_instance.field.assert_called_with("temp", 42.0)

    def test_empty_records_skipped(self):
        mock_influx = MagicMock()
        with patch.dict(sys.modules, {"influxdb_client": mock_influx}):
            sink = InfluxDbSink({
                "url": "http://localhost:8086", "token": "t", "org": "o",
                "bucket": "b", "measurement": "m",
            })
            sink.write(b'[]', {})
        mock_influx.InfluxDBClient.assert_not_called()
