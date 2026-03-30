"""AMQP source connector — consumes messages via pika (RabbitMQ)."""
from __future__ import annotations
import logging
import queue
import threading
from typing import Iterator
from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)

@register_source("amqp")
class AmqpSource(BaseSource):
    """Consume messages from an AMQP queue (RabbitMQ).

    Config keys:
        url             (str, default "amqp://guest:guest@localhost:5672/")
        queue           (str, required)
        prefetch_count  (int, default 10)
        auto_ack        (bool, default False)
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.url: str = config.get("url", "amqp://guest:guest@localhost:5672/")
        self.queue_name: str = config["queue"]
        self.prefetch_count: int = int(config.get("prefetch_count", 10))
        self.auto_ack: bool = bool(config.get("auto_ack", False))
        self._stop_event = threading.Event()
        self._msg_queue: queue.SimpleQueue = queue.SimpleQueue()

    def test_connection(self) -> dict:
        import socket
        import time
        from urllib.parse import urlparse
        t0 = time.monotonic()
        url = self.config.get("url", "amqp://guest:guest@localhost:5672/")
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 5672
        try:
            with socket.create_connection((host, port), timeout=8):
                latency = int((time.monotonic() - t0) * 1000)
                return {"ok": True, "latency_ms": latency, "detail": f"TCP {host}:{port} OK"}
        except Exception as exc:
            raise RuntimeError(f"AMQP TCP probe failed: {exc}")

    def read(self) -> Iterator[tuple[bytes, dict]]:
        try:
            import pika
        except ImportError as exc:
            raise SourceError(
                "AMQP source requires pika — install with: pip install tram[amqp]"
            ) from exc
        try:
            params = pika.URLParameters(self.url)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.basic_qos(prefetch_count=self.prefetch_count)
        except Exception as exc:
            raise SourceError(f"AMQP connection failed: {exc}") from exc

        def on_message(ch, method, properties, body):
            meta = {
                "amqp_queue": self.queue_name,
                "amqp_delivery_tag": method.delivery_tag,
                "amqp_routing_key": method.routing_key,
            }
            self._msg_queue.put((body, meta))
            if not self.auto_ack:
                ch.basic_ack(delivery_tag=method.delivery_tag)

        channel.basic_consume(
            queue=self.queue_name,
            on_message_callback=on_message,
            auto_ack=self.auto_ack,
        )
        logger.info("AMQP source consuming from queue", extra={"amqp_queue": self.queue_name})

        def consume_thread():
            try:
                channel.start_consuming()
            except Exception:
                pass

        t = threading.Thread(target=consume_thread, daemon=True)
        t.start()

        try:
            while not self._stop_event.is_set() or not self._msg_queue.empty():
                try:
                    payload, meta = self._msg_queue.get(timeout=1.0)
                    yield payload, meta
                except queue.Empty:
                    if self._stop_event.is_set():
                        break
                    continue
        finally:
            try:
                channel.stop_consuming()
                connection.close()
            except Exception:
                pass
