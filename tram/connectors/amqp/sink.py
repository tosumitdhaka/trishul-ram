"""AMQP sink connector — publishes messages via pika (RabbitMQ)."""
from __future__ import annotations

import logging

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import (
    BaseSink,
    DeliveryTier,
    SinkCapability,
    SinkCommitReceipt,
)
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

    Delivery (V18-01 audit, pending decision 1): pika supports publisher
    confirms via ``channel.confirm_delivery()``. In blocking mode each
    ``basic_publish`` then blocks until the broker confirms the message (a
    ``basic.nack`` raises), so a synchronous return is a broker-confirmed
    publish — no false clean success. Every publish opens its own connection
    and blocks on its confirm, so the sink holds no buffered state and
    ``commit()`` is a trivial confirmed barrier at ``remote_durable``. The
    tier assignment is confirmed by this audit; end-to-end behavior against a
    live broker is validated by the V18-02 broker-test gate.
    """

    # V18-01 frozen tier table, section 6: remote_durable (publisher confirms).
    # Not replay-safe: re-publishing duplicates messages.
    delivery_capability = SinkCapability(
        tier=DeliveryTier.REMOTE_DURABLE,
        replay_safe=False,
    )

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.url: str = config.get("url", "amqp://guest:guest@localhost:5672/")
        self.exchange: str = config.get("exchange", "")
        self.routing_key: str = config.get("routing_key", "")
        self.content_type: str = config.get("content_type", "application/json")

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
            # Publisher confirms: basic_publish now blocks until the broker
            # confirms the message (or raises on basic.nack) — a synchronous
            # return is a broker-confirmed publish, never fire-and-forget.
            channel.confirm_delivery()
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

    def commit(self, *, deadline: float | None = None) -> SinkCommitReceipt:
        """Delivery flush/commit barrier (V18-01 section 6).

        Each ``write()`` opens its own connection and blocks on the broker's
        publisher confirm, so the sink holds no buffered state; the barrier
        confirms the synchronous confirmed-publish contract at
        ``remote_durable``.
        """
        return SinkCommitReceipt(
            sink_key=self.__class__.__name__,
            tier=DeliveryTier.REMOTE_DURABLE,
            confirmed=True,
            notes="per-publish publisher confirms (channel.confirm_delivery); nothing buffered",
        )
