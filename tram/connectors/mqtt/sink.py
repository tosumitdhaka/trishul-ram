"""MQTT sink connector — publishes data to an MQTT topic."""
from __future__ import annotations

import logging

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)

@register_sink("mqtt")
class MqttSink(BaseSink):
    """Publish data to an MQTT topic.

    Config keys:
        host        (str, required)
        port        (int, default 1883)
        topic       (str, required)
        qos         (int, default 0)
        retain      (bool, default False)
        username    (str, optional)
        password    (str, optional)
        tls         (bool, default False)
        client_id   (str, default "")
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config["host"]
        self.port: int = int(config.get("port", 1883))
        self.topic: str = config["topic"]
        self.qos: int = int(config.get("qos", 0))
        self.retain: bool = bool(config.get("retain", False))
        self.username: str | None = config.get("username")
        self.password: str | None = config.get("password")
        self.tls: bool = bool(config.get("tls", False))
        self.client_id: str = config.get("client_id", "")

    def _get_mqtt_client(self):
        try:
            from paho.mqtt import client as mqtt_module
        except ImportError as exc:
            raise SinkError(
                "MQTT sink requires paho-mqtt — install with: pip install tram[mqtt]"
            ) from exc
        client = mqtt_module.Client(client_id=self.client_id)
        if self.username:
            client.username_pw_set(self.username, self.password)
        if self.tls:
            client.tls_set()
        return client

    def write(self, data: bytes, meta: dict) -> None:
        try:
            client = self._get_mqtt_client()
            client.connect(self.host, self.port)
            result = client.publish(self.topic, payload=data, qos=self.qos, retain=self.retain)
            result.wait_for_publish()
            client.disconnect()
            logger.info("Published to MQTT topic", extra={"topic": self.topic, "bytes": len(data)})
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"MQTT publish failed: {exc}") from exc
