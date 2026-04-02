"""Tests for MQTT source and sink connectors."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.mqtt.sink import MqttSink
from tram.connectors.mqtt.source import MqttSource
from tram.core.exceptions import SinkError, SourceError


class TestMqttSource:
    def test_import_error_raises_source_error(self):
        with patch.dict(sys.modules, {"paho": None, "paho.mqtt": None, "paho.mqtt.client": None}):
            source = MqttSource({"host": "localhost", "topic": "test"})
            with pytest.raises(SourceError, match="paho-mqtt"):
                list(source.read())

    def test_read_yields_messages(self):
        mock_client = MagicMock()
        source = MqttSource({"host": "localhost", "topic": "test/#", "port": 1883})
        # Pre-populate queue and set stop event so the generator drains and exits
        source._queue.put((b'{"x":1}', {"mqtt_topic": "test/a", "mqtt_qos": 0}))
        source._stop_event.set()

        with patch.object(source, "_get_mqtt_client", return_value=mock_client):
            results = list(source.read())

        assert len(results) >= 1
        assert results[0][0] == b'{"x":1}'

    def test_tls_set_called_when_tls_true(self):
        mock_mqtt_client = MagicMock()
        mock_paho_mqtt = MagicMock()
        mock_paho_mqtt.client.Client.return_value = mock_mqtt_client
        with patch.dict(sys.modules, {"paho": MagicMock(), "paho.mqtt": mock_paho_mqtt}):
            source = MqttSource({"host": "localhost", "topic": "test", "tls": True})
            source._get_mqtt_client()
        mock_mqtt_client.tls_set.assert_called_once()

    def test_username_pw_set_called(self):
        mock_mqtt_client = MagicMock()
        mock_paho_mqtt = MagicMock()
        mock_paho_mqtt.client.Client.return_value = mock_mqtt_client
        with patch.dict(sys.modules, {"paho": MagicMock(), "paho.mqtt": mock_paho_mqtt}):
            source = MqttSource({"host": "localhost", "topic": "test", "username": "user", "password": "pass"})
            source._get_mqtt_client()
        mock_mqtt_client.username_pw_set.assert_called_once_with("user", "pass")


class TestMqttSink:
    def test_import_error_raises_sink_error(self):
        with patch.dict(sys.modules, {
            "paho": None, "paho.mqtt": None,
            "paho.mqtt.client": None, "paho.mqtt.publish": None,
        }):
            sink = MqttSink({"host": "localhost", "topic": "test"})
            with pytest.raises(SinkError, match="paho-mqtt"):
                sink.write(b"data", {})

    def test_publish_called(self):
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_client.publish.return_value = mock_result

        with patch.object(MqttSink, "_get_mqtt_client", return_value=mock_client):
            sink = MqttSink({"host": "localhost", "topic": "test/out"})
            sink.write(b'{"x":1}', {})

        mock_client.publish.assert_called_once_with(
            "test/out", payload=b'{"x":1}', qos=0, retain=False
        )

    def test_retain_flag(self):
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_client.publish.return_value = mock_result

        with patch.object(MqttSink, "_get_mqtt_client", return_value=mock_client):
            sink = MqttSink({"host": "localhost", "topic": "test", "retain": True})
            sink.write(b"data", {})

        _, kwargs = mock_client.publish.call_args
        assert kwargs["retain"] is True

    def test_tls_set_for_sink(self):
        mock_mqtt_client = MagicMock()
        mock_result = MagicMock()
        mock_mqtt_client.publish.return_value = mock_result
        mock_paho_mqtt = MagicMock()
        mock_paho_mqtt.client.Client.return_value = mock_mqtt_client
        with patch.dict(sys.modules, {"paho": MagicMock(), "paho.mqtt": mock_paho_mqtt}):
            sink = MqttSink({"host": "localhost", "topic": "test", "tls": True})
            sink._get_mqtt_client()
        mock_mqtt_client.tls_set.assert_called_once()
