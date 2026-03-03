"""MQTT source connector — subscribes and yields messages as a stream."""
from __future__ import annotations
import logging
import queue
import threading
from typing import Iterator
from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


@register_source("mqtt")
class MqttSource(BaseSource):
    """Subscribe to an MQTT topic and yield messages as a stream.

    Config keys:
        host        (str, required)
        port        (int, default 1883)
        topic       (str, required)
        qos         (int, default 0)
        client_id   (str, default "")
        username    (str, optional)
        password    (str, optional)
        tls         (bool, default False)
        keepalive   (int, default 60)
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config["host"]
        self.port: int = int(config.get("port", 1883))
        self.topic: str = config["topic"]
        self.qos: int = int(config.get("qos", 0))
        self.client_id: str = config.get("client_id", "")
        self.username: str | None = config.get("username")
        self.password: str | None = config.get("password")
        self.tls: bool = bool(config.get("tls", False))
        self.keepalive: int = int(config.get("keepalive", 60))
        self._stop_event = threading.Event()
        self._queue: queue.SimpleQueue = queue.SimpleQueue()

    def _get_mqtt_client(self):
        try:
            from paho.mqtt import client as mqtt_module
        except ImportError as exc:
            raise SourceError(
                "MQTT source requires paho-mqtt — install with: pip install tram[mqtt]"
            ) from exc
        client = mqtt_module.Client(client_id=self.client_id)
        if self.username:
            client.username_pw_set(self.username, self.password)
        if self.tls:
            client.tls_set()
        return client

    def read(self) -> Iterator[tuple[bytes, dict]]:
        client = self._get_mqtt_client()

        def on_message(client, userdata, msg):
            self._queue.put((msg.payload, {"mqtt_topic": msg.topic, "mqtt_qos": msg.qos}))

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                client.subscribe(self.topic, qos=self.qos)
                logger.info("MQTT source connected and subscribed", extra={"topic": self.topic})
            else:
                logger.error("MQTT connection failed with code %d", rc)

        client.on_message = on_message
        client.on_connect = on_connect
        try:
            client.connect(self.host, self.port, keepalive=self.keepalive)
            client.loop_start()
        except Exception as exc:
            raise SourceError(f"MQTT connect failed: {exc}") from exc

        try:
            while not self._stop_event.is_set() or not self._queue.empty():
                try:
                    payload, meta = self._queue.get(timeout=1.0)
                    yield payload, meta
                except queue.Empty:
                    if self._stop_event.is_set():
                        break
                    continue
        finally:
            client.loop_stop()
            client.disconnect()
