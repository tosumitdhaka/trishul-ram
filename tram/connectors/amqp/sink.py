"""AMQP sink connector — publishes messages via pika (RabbitMQ)."""
from __future__ import annotations
import logging
from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)

@register_sink("amqp")
class AmqpSink(BaseSink):
    """Publish data to an AMQP exchange (RabbitMQ).

    Config keys:
        url             (str, default "amqp://guest:guest@localhost:5672/")
        exchange        (str, default "")
        routing_key     (str, required)
        content_type    (str, default "application/json")
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.url: str = config.get("url", "amqp://guest:guest@localhost:5672/")
        self.exchange: str = config.get("exchange", "")
        self.routing_key: str = config.get("routing_key", "")
        self.content_type: str = config.get("content_type", "application/json")

    def write(self, data: bytes, meta: dict) -> None:
        try:
            import pika
        except ImportError as exc:
            raise SinkError(
                "AMQP sink requires pika — install with: pip install tram[amqp]"
            ) from exc
        try:
            params = pika.URLParameters(self.url)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.basic_publish(
                exchange=self.exchange,
                routing_key=self.routing_key,
                body=data,
                properties=pika.BasicProperties(content_type=self.content_type),
            )
            connection.close()
            logger.info("Published to AMQP", extra={"routing_key": self.routing_key, "bytes": len(data)})
        except Exception as exc:
            raise SinkError(f"AMQP publish failed: {exc}") from exc
