"""Kafka sink connector — produces messages to a Kafka topic."""

from __future__ import annotations

import logging

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


@register_sink("kafka")
class KafkaSink(BaseSink):
    """Produce serialized bytes as a message to a Kafka topic.

    Requires ``kafka-python`` (``pip install kafka-python``).

    Config keys:
        brokers           (list[str], required)    Bootstrap server list.
        topic             (str, required)           Target topic.
        key_field         (str, optional)           Record field to use as message key.
        security_protocol (str, default "PLAINTEXT")
        sasl_mechanism    (str, optional)
        sasl_username     (str, optional)
        sasl_password     (str, optional)
        ssl_cafile        (str, optional)
        acks              (str/int, default "all")  "all" | 0 | 1
        compression_type  (str, optional)           "gzip" | "snappy" | "lz4" | "zstd"
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        brokers = config["brokers"]
        self.brokers: list[str] = brokers if isinstance(brokers, list) else [brokers]
        self.topic: str = config["topic"]
        self.key_field: str | None = config.get("key_field")
        self.security_protocol: str = config.get("security_protocol", "PLAINTEXT")
        self.sasl_mechanism: str | None = config.get("sasl_mechanism")
        self.sasl_username: str | None = config.get("sasl_username")
        self.sasl_password: str | None = config.get("sasl_password")
        self.ssl_cafile: str | None = config.get("ssl_cafile")
        self.acks = config.get("acks", "all")
        self.compression_type: str | None = config.get("compression_type")
        self._producer = None

    def _get_producer(self):
        if self._producer is not None:
            return self._producer
        try:
            from kafka import KafkaProducer
        except ImportError as exc:
            raise SinkError("Kafka sink requires kafka-python: pip install kafka-python") from exc

        kwargs: dict = dict(
            bootstrap_servers=self.brokers,
            acks=self.acks,
            security_protocol=self.security_protocol,
        )
        if self.compression_type:
            kwargs["compression_type"] = self.compression_type
        if self.sasl_mechanism:
            kwargs["sasl_mechanism"] = self.sasl_mechanism
            kwargs["sasl_plain_username"] = self.sasl_username
            kwargs["sasl_plain_password"] = self.sasl_password
        if self.ssl_cafile:
            kwargs["ssl_cafile"] = self.ssl_cafile

        try:
            self._producer = KafkaProducer(**kwargs)
        except Exception as exc:
            raise SinkError(f"Kafka producer init failed: {exc}") from exc
        return self._producer

    def write(self, data: bytes, meta: dict) -> None:
        producer = self._get_producer()

        key: bytes | None = None
        if self.key_field:
            import json
            try:
                records = json.loads(data)
                if isinstance(records, list) and records:
                    key_val = records[0].get(self.key_field)
                    if key_val is not None:
                        key = str(key_val).encode("utf-8")
            except Exception:
                pass

        try:
            future = producer.send(self.topic, value=data, key=key)
            future.get(timeout=10)
            logger.info(
                "Kafka message sent",
                extra={"topic": self.topic, "bytes": len(data)},
            )
        except Exception as exc:
            raise SinkError(f"Kafka send failed to topic '{self.topic}': {exc}") from exc
