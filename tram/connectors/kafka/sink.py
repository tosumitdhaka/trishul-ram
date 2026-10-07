"""Kafka sink connector — produces messages to a Kafka topic."""

from __future__ import annotations

import json
import logging

from tram.connectors.config_utils import cfg_int
from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import get_serializer, register_sink

logger = logging.getLogger(__name__)


def chunk_records_by_caps(
    records: list[dict],
    *,
    serializer,
    chunk_records: int,
    chunk_bytes: int,
) -> list[list[dict]]:
    """Split *records* into chunks bounded by record count and serialized size.

    Both caps are independent: a chunk boundary is forced when either the
    record count reaches ``chunk_records`` or adding the next record would push
    the chunk's serialized size past ``chunk_bytes``. Chunk order preserves the
    input order. A single record larger than ``chunk_bytes`` becomes its own
    (oversized) chunk — it is still sent alone and fails loudly at the broker
    if it exceeds the client ``max_request_size`` (never a silent skip).
    """
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_bytes = 0
    for record in records:
        record_bytes = len(serializer.serialize([record]))
        if current and (
            len(current) >= chunk_records or current_bytes + record_bytes > chunk_bytes
        ):
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(record)
        current_bytes += record_bytes
    if current:
        chunks.append(current)
    return chunks


@register_sink("kafka")
class KafkaSink(BaseSink):
    """Produce serialized bytes as messages to a Kafka topic.

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
        chunk_records     (int, default 1000)       Max records per message.
        chunk_bytes       (int, default 524288)     Max serialized bytes per message
                                                    (512 KiB; must be <= max_request_size).
        max_request_size  (int, default 1048576)    Kafka producer max_request_size in
                                                    bytes (1 MiB); passed to the client.

    Bounded-batch sends (issue #76): a source batch larger than ``chunk_records``
    or ``chunk_bytes`` is delivered as multiple messages, split at record
    boundaries with the batch's order preserved (a batch within both caps is
    still sent as one message, byte-for-byte unchanged). Ordering across
    chunks: with ``key_field`` set, every chunk carries the same key and lands
    on the same partition (kafka per-partition ordering); without a key, chunks
    are pinned to one stable partition via ``producer.partitions_for`` so the
    broker still delivers them in order.

    Delivery semantics: at-least-once across chunk boundaries. A failed chunk
    surfaces as a ``SinkError`` (never a silent skip) whose message reports how
    many chunks were already delivered; on retry the whole batch is re-sent
    from the first chunk and already-delivered chunks are NOT retracted, so
    duplicates are possible — that is the accepted at-least-once trade.
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
        # Bounded-batch send caps (issue #76). The Pydantic config schema
        # validates chunk_bytes <= max_request_size for real pipelines; the
        # guard here covers direct construction (e.g. tests).
        self.chunk_records: int = cfg_int(config, "chunk_records", 1000)
        self.chunk_bytes: int = cfg_int(config, "chunk_bytes", 524288)
        self.max_request_size: int = cfg_int(config, "max_request_size", 1048576)
        if self.chunk_records < 1 or self.chunk_bytes < 1 or self.max_request_size < 1:
            raise SinkError(
                "kafka sink chunk_records/chunk_bytes/max_request_size must be >= 1"
            )
        if self.chunk_bytes > self.max_request_size:
            raise SinkError(
                f"kafka sink chunk_bytes ({self.chunk_bytes}) must be <= "
                f"max_request_size ({self.max_request_size})"
            )
        self._producer = None
        # Stable partition for key-less chunked batches (issue #76 ordering):
        # kafka-python's partitioner scatters key-less messages randomly, which
        # would break cross-chunk order; see _batch_partition.
        self._sticky_partition: int | None = None

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
            max_request_size=self.max_request_size,
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

        records, serializer = self._parse_payload(data, meta)
        if records is None:
            # Opaque/unparseable payload (or no record framing): legacy single
            # message send. If the payload is too large for the broker the
            # send fails loudly — never a silent skip.
            self._send_chunk(producer, data, key=self._legacy_key(data))
            return

        chunks = chunk_records_by_caps(
            records,
            serializer=serializer,
            chunk_records=self.chunk_records,
            chunk_bytes=self.chunk_bytes,
        )
        if len(chunks) <= 1:
            # Within both caps: send the original bytes untouched (byte-faithful).
            self._send_chunk(producer, data, key=self._batch_key(records))
            return

        # Multi-chunk send: a keyed batch keeps every chunk on the same
        # partition via kafka's key hashing; a key-less batch is pinned to one
        # stable partition so the broker preserves the batch's order.
        key = self._batch_key(records)
        partition = None if key is not None else self._batch_partition(producer, records)
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            payload = serializer.serialize(chunk)
            try:
                self._send_chunk(producer, payload, key=key, partition=partition)
            except SinkError as exc:
                raise SinkError(
                    f"Kafka send failed to topic '{self.topic}' at chunk {index} "
                    f"of {total} — {index - 1} chunk(s) already delivered; a retry "
                    "re-sends from chunk 1 (duplicates possible, at-least-once): "
                    f"{exc}"
                ) from exc

    def _batch_partition(self, producer, records: list[dict]) -> int | None:
        """Stable partition for a key-less chunked batch (issue #76 ordering).

        kafka-python's default partitioner picks a random partition per
        key-less message, which would scatter a chunked batch across partitions
        and break cross-chunk order. Hash the batch's first record to a
        partition (chosen once per producer lifetime, so later batches from
        this sink keep landing on the same partition while still spreading
        across the topic). Returns None when metadata is unavailable — the
        broker then orders best-effort.
        """
        if self._sticky_partition is not None:
            return self._sticky_partition
        try:
            partitions = set(producer.partitions_for(self.topic))
        except Exception:
            return None
        if not partitions:
            return None
        try:
            bucket = abs(hash(str(records[0]))) % len(partitions)
        except Exception:
            bucket = 0
        self._sticky_partition = sorted(partitions)[bucket]
        return self._sticky_partition

    def _parse_payload(self, data: bytes, meta: dict):
        """Parse *data* back into records with the pipeline's out serializer.

        Returns ``(records, serializer)`` when record framing is available
        (every registered serializer's ``parse`` returns a record list), or
        ``(None, None)`` when the payload cannot be parsed.
        """
        serializer_type = str(meta.get("serializer_type", "json") or "json")
        serializer_config = dict(meta.get("serializer_config", {}) or {})
        try:
            serializer_cls = get_serializer(serializer_type)
            serializer = serializer_cls(serializer_config)
            records = serializer.parse(data)
        except Exception:
            return None, None
        if not isinstance(records, list) or not records:
            return None, None
        return records, serializer

    def _batch_key(self, records: list[dict]) -> bytes | None:
        """Message key from the batch's first record (issue #76: one key per
        batch so all chunks land on the same partition, order preserved)."""
        if not self.key_field:
            return None
        try:
            key_val = records[0].get(self.key_field)
        except Exception:
            return None
        if key_val is None:
            return None
        return str(key_val).encode("utf-8")

    def _legacy_key(self, data: bytes) -> bytes | None:
        """Best-effort key extraction for opaque payloads (json.loads)."""
        if not self.key_field:
            return None
        try:
            records = json.loads(data)
            if isinstance(records, list) and records:
                key_val = records[0].get(self.key_field)
                if key_val is not None:
                    return str(key_val).encode("utf-8")
        except Exception:
            pass
        return None

    def _send_chunk(
        self,
        producer,
        payload: bytes,
        *,
        key: bytes | None,
        partition: int | None = None,
    ) -> None:
        """Send one message and wait for the broker acknowledgement."""
        try:
            future = producer.send(self.topic, value=payload, key=key, partition=partition)
            future.get(timeout=10)
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"Kafka send failed to topic '{self.topic}': {exc}") from exc
        logger.info(
            "Kafka message sent",
            extra={"topic": self.topic, "bytes": len(payload)},
        )