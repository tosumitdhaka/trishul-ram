"""Kafka source connector — infinite stream consumer."""

from __future__ import annotations

import logging
from typing import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


@register_source("kafka")
class KafkaSource(BaseSource):
    """Consume messages from a Kafka topic as an infinite stream.

    Requires ``kafka-python`` (``pip install kafka-python``).
    Use with ``schedule.type: stream``.

    Config keys:
        brokers           (list[str], required)    Bootstrap server list.
        topic             (str or list[str], req.)  Topic(s) to subscribe to.
        group_id          (str, default "tram")     Consumer group ID.
        auto_offset_reset (str, default "latest")   "latest" | "earliest"
        enable_auto_commit (bool, default True)     Auto-commit offsets.
        max_poll_records  (int, default 500)        Max records per poll.
        session_timeout_ms (int, default 30000)     Session timeout.
        security_protocol (str, default "PLAINTEXT") "PLAINTEXT" | "SSL" | "SASL_PLAINTEXT" | "SASL_SSL"
        sasl_mechanism    (str, optional)           "PLAIN" | "SCRAM-SHA-256" | "SCRAM-SHA-512"
        sasl_username     (str, optional)           SASL username.
        sasl_password     (str, optional)           SASL password.
        ssl_cafile        (str, optional)           CA certificate path.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        brokers = config["brokers"]
        self.brokers: list[str] = brokers if isinstance(brokers, list) else [brokers]
        topics = config["topic"]
        self.topics: list[str] = topics if isinstance(topics, list) else [topics]
        self.group_id: str = config.get("group_id", "tram")
        self.auto_offset_reset: str = config.get("auto_offset_reset", "latest")
        self.enable_auto_commit: bool = bool(config.get("enable_auto_commit", True))
        self.max_poll_records: int = int(config.get("max_poll_records", 500))
        self.session_timeout_ms: int = int(config.get("session_timeout_ms", 30000))
        self.security_protocol: str = config.get("security_protocol", "PLAINTEXT")
        self.sasl_mechanism: str | None = config.get("sasl_mechanism")
        self.sasl_username: str | None = config.get("sasl_username")
        self.sasl_password: str | None = config.get("sasl_password")
        self.ssl_cafile: str | None = config.get("ssl_cafile")

    def _build_consumer(self):
        try:
            from kafka import KafkaConsumer
        except ImportError as exc:
            raise SourceError(
                "Kafka source requires kafka-python: pip install kafka-python"
            ) from exc

        kwargs: dict = dict(
            group_id=self.group_id,
            auto_offset_reset=self.auto_offset_reset,
            enable_auto_commit=self.enable_auto_commit,
            max_poll_records=self.max_poll_records,
            session_timeout_ms=self.session_timeout_ms,
            security_protocol=self.security_protocol,
            bootstrap_servers=self.brokers,
        )
        if self.sasl_mechanism:
            kwargs["sasl_mechanism"] = self.sasl_mechanism
            kwargs["sasl_plain_username"] = self.sasl_username
            kwargs["sasl_plain_password"] = self.sasl_password
        if self.ssl_cafile:
            kwargs["ssl_cafile"] = self.ssl_cafile

        return KafkaConsumer(*self.topics, **kwargs)

    def read(self) -> Iterator[tuple[bytes, dict]]:
        logger.info(
            "Kafka consumer starting",
            extra={"brokers": self.brokers, "topics": self.topics, "group": self.group_id},
        )
        try:
            consumer = self._build_consumer()
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"Kafka consumer init failed: {exc}") from exc

        try:
            for msg in consumer:
                value = msg.value
                if value is None:
                    continue
                yield value, {
                    "kafka_topic": msg.topic,
                    "kafka_partition": msg.partition,
                    "kafka_offset": msg.offset,
                    "kafka_key": msg.key.decode("utf-8") if msg.key else None,
                }
        except Exception as exc:
            raise SourceError(f"Kafka consumer error: {exc}") from exc
        finally:
            try:
                consumer.close()
                logger.info("Kafka consumer closed", extra={"topics": self.topics})
            except Exception:
                pass
