"""AMQP source connector — consumes messages via pika (RabbitMQ)."""
from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import AckDisposition, BaseSource
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

    Delivery semantics (V18-01 §6/§7, R2): messages are NOT acknowledged at
    intake. The broker delivery tag is retained in the meta as
    ``amqp_delivery_tag`` and acknowledged only when the executor calls
    ``ack(meta, disposition)``; the ack/nack is marshalled onto the pika
    connection thread (``add_callback_threadsafe`` — the connection is not
    thread-safe). The broker prefetch (``basic_qos``) bounds how many unacked
    deliveries can be outstanding, which is the backpressure that bounds the
    internal bridge: a slow sink fills the prefetch window and the broker
    stops delivering, never growing memory without bound. Unacked tags are
    requeued by the broker when the connection closes, so a mid-run abort
    re-delivers the tail (at-least-once). ``auto_ack: true`` opts back into
    broker-side auto-acknowledgement (at-most-once); ``ack()`` then no-ops
    because the tag is already settled by the broker.

    Replay identity: ``source_unit_id(meta)`` is ``{queue}/{producer message
    ID}`` when the ``message_id`` property is present and non-empty; the
    channel/session delivery tag is only an acknowledgement handle and never
    a durable identity. With ``require_message_id: true`` (V18-01 §7) a
    delivery without a usable ``message_id`` is refused at intake — never
    enqueued, error-logged, and ``basic_nack(requeue=True)`` marshalled onto
    the connection thread so the broker redelivers it (at-least-once, never
    silently lost nor silently processed).
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.url: str = config.get("url", "amqp://guest:guest@localhost:5672/")
        self.queue_name: str = config["queue"]
        self.prefetch_count: int = int(config.get("prefetch_count", 10))
        self.auto_ack: bool = bool(config.get("auto_ack", False))
        # V18-01 §7: when true, a delivery without a producer ``message_id``
        # is refused at intake (never processed, nacked with requeue) — no
        # durable identity means no honest ack under strict delivery.
        self.require_message_id: bool = bool(config.get("require_message_id", False))
        self._stop_event = threading.Event()
        self._msg_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._connection = None
        self._channel = None

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

        self._connection = connection
        self._channel = channel

        def on_message(ch, method, properties, body):
            meta = {
                "amqp_queue": self.queue_name,
                "amqp_delivery_tag": method.delivery_tag,
                "amqp_routing_key": method.routing_key,
                "amqp_redelivered": bool(getattr(method, "redelivered", False)),
            }
            message_id = getattr(properties, "message_id", None)
            if message_id:
                meta["amqp_message_id"] = message_id
            elif self.require_message_id:
                # Strict identity (V18-01 §7): no producer message ID = no
                # durable identity = no honest ack. Refuse the delivery — it is
                # never enqueued, the refusal is logged, and the message is
                # nacked with requeue via the thread-marshalled path so the
                # broker redelivers it (at-least-once: neither silently lost
                # nor silently processed). With auto_ack the broker has already
                # settled the tag at delivery, so there is nothing to nack.
                logger.error(
                    "AMQP delivery refused: require_message_id is true but the "
                    "message has no message_id property",
                    extra={
                        "amqp_queue": self.queue_name,
                        "amqp_delivery_tag": method.delivery_tag,
                        "amqp_redelivered": bool(getattr(method, "redelivered", False)),
                    },
                )
                if not self.auto_ack:
                    try:
                        connection.add_callback_threadsafe(
                            lambda: self._deliver_ack(
                                channel, method.delivery_tag, "nack", requeue=True
                            )
                        )
                    except Exception as exc:
                        logger.warning(
                            "AMQP refusal nack scheduling failed for tag %s",
                            method.delivery_tag,
                            extra={"error": str(exc)},
                        )
                return
            self._msg_queue.put((body, meta))
            # Deliberately NO basic_ack at intake (V18-01 R2): the tag stays
            # with the message until the executor calls ack(); the broker
            # prefetch bounds how many unacked deliveries stay outstanding.

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
            self._connection = None
            self._channel = None

    def source_unit_id(self, meta: dict) -> str | None:
        """Stable replay identity: ``{queue}/{producer message ID}``.

        Returned only when a producer ``message_id`` property is present and
        non-empty; otherwise None (no durable identity — strict stateful
        retention is then rejected at validation). The delivery tag is never
        part of the identity.
        """
        message_id = meta.get("amqp_message_id")
        if not message_id:
            return None
        return f"{self.queue_name}/{message_id}"

    def ack(self, meta: dict, disposition: AckDisposition) -> None:
        """Acknowledge a decided unit on the pika connection thread.

        Mapping (V18-01 §6): ``delivered``/``filtered`` → ``basic_ack``;
        ``dlq`` → ``basic_ack`` (the unit is durably handled by the DLQ);
        ``dropped`` → ``basic_nack(requeue=False)`` (explicit loss is
        rejected, never silently redelivered nor silently accepted).

        The actual channel call is scheduled via the connection's
        ``add_callback_threadsafe`` so it runs on the connection thread —
        pika objects are not safe to touch from the executor thread.
        """
        tag = meta.get("amqp_delivery_tag")
        if tag is None or self.auto_ack:
            return
        connection = self._connection
        channel = self._channel
        if connection is None or channel is None:
            logger.warning(
                "AMQP ack skipped: no active connection for tag %s", tag
            )
            return
        action = "nack" if disposition == AckDisposition.DROPPED else "ack"
        try:
            connection.add_callback_threadsafe(
                lambda: self._deliver_ack(channel, tag, action)
            )
        except Exception as exc:
            logger.warning(
                "AMQP ack scheduling failed for tag %s", tag, extra={"error": str(exc)}
            )

    def _deliver_ack(self, channel, tag, action: str, requeue: bool = False) -> None:
        """Run on the pika connection thread (scheduled by ``ack``).

        ``requeue`` only applies to ``nack``: False for the dropped-unit ack
        mapping, True for the require_message_id intake refusal (the broker
        redelivers so the message is never silently lost).
        """
        try:
            if action == "nack":
                channel.basic_nack(delivery_tag=tag, requeue=requeue)
            else:
                channel.basic_ack(delivery_tag=tag)
        except Exception as exc:
            logger.warning(
                "AMQP %s failed for tag %s", action, tag, extra={"error": str(exc)}
            )
