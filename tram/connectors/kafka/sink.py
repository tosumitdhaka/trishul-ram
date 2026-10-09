"""Kafka sink connector — produces messages to a Kafka topic."""

from __future__ import annotations

import json
import logging
import time

from tram.connectors.config_utils import cfg_int
from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import (
    BaseSink,
    DeliveryTier,
    SinkCapability,
    SinkCommitReceipt,
)
from tram.registry.registry import get_serializer, register_sink

logger = logging.getLogger(__name__)

# The only acks levels that mean "all in-sync replicas acknowledged" — the
# values the sink accepts now that commit() reports remote_durable (V18-01
# section 12, pending decision 1; audit concludes acks=all is required).
_DURABLE_ACKS = {"all", "-1"}


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
        acks              (str/int, default "all")  "all" | -1 only — weaker
                                                    levels (0/1) are rejected
                                                    (delivery-contract change,
                                                    V18-01 section 12.1)
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

    Fast path (perf follow-up 2026-10-07): when the executor already supplied
    ``output_record_count`` in *meta* and the payload is already-serialized
    bytes within both caps (and the sink is keyless), ``write`` sends the
    payload as ONE message without re-parsing records or re-serializing via
    ``chunk_records_by_caps``. Every other case (missing/zero/over-cap count,
    key-configured, payload over the byte cap, opaque payload) keeps the legacy
    parse-and-chunk behavior byte-for-byte.

    Delivery semantics: at-least-once across chunk boundaries. A failed chunk
    surfaces as a ``SinkError`` (never a silent skip) whose message reports how
    many chunks were already delivered; on retry the whole batch is re-sent
    from the first chunk and already-delivered chunks are NOT retracted, so
    duplicates are possible — that is the accepted at-least-once trade.

    Delivery tier (V18-01 audit, pending decision 1): the producer default is
    ``acks=all`` and weaker acks configurations are rejected at construction —
    no path may restore weaker-ack false success silently. ``write()`` awaits
    every send future, and ``commit()`` drains the producer via ``flush()``
    before reporting ``remote_durable``. kafka-python exposes no separate
    delivery-callback error buffer that this path can miss, so
    ``latched_error()`` keeps the base-class ``None`` default. Real broker
    durability (acks=all honored, flush drains) is provable against a live
    broker via the env-gated live suite
    (``tests/unit/test_kafka_live_broker.py``, V18-10 broker-test gate).
    """

    # V18-01 frozen tier table, section 6: remote_durable (acks=all). Not
    # replay-safe: re-sending a batch duplicates messages (at-least-once).
    delivery_capability = SinkCapability(
        tier=DeliveryTier.REMOTE_DURABLE,
        replay_safe=False,
    )

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
        if str(self.acks).lower() not in _DURABLE_ACKS:
            raise SinkError(
                f"kafka sink acks={self.acks!r} is not a durable acknowledgement level; "
                "the v1.8 delivery contract requires acks='all' (or -1) so commit() "
                "can report remote_durable truthfully"
            )
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

        if self._fast_path_eligible(data, meta):
            # Fast path: the executor already supplied output_record_count and
            # the payload is already-serialized bytes within both caps — send it
            # as ONE message without re-parsing records or re-serializing via
            # chunk_records_by_caps. Keyless by eligibility; a single message
            # needs no sticky-partition pinning (ordering is trivial). Delivery
            # bookkeeping is shared with the legacy path: _send_chunk waits on
            # the ack future and surfaces failures as SinkError, so the
            # executor's retry/backoff/circuit-breaker loop treats it the same.
            self._send_chunk(producer, data, key=None)
            return

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

    def _fast_path_eligible(self, data: bytes, meta: dict) -> bool:
        """Single-message fast-path eligibility gate (perf follow-up 2026-10-07).

        All of the following must hold, checked in this order:
          1. ``meta["output_record_count"]`` is a positive int — the executor
             already counted this partition's records after sink transforms and
             filename partitioning.
          2. That count is within the per-message record cap
             (``count <= self.chunk_records``, the same boundary
             ``chunk_records_by_caps`` applies).
          3. The sink is keyless (no ``key_field``), so ``_batch_key`` would
             not apply — no key extraction is required.
          4. The payload is already-serialized bytes whose actual length is
             within the byte cap (``len(data) <= self.chunk_bytes``).

        When any check fails the caller falls back to the legacy parse-and-chunk
        path. No serialization work is added here: the check uses the actual
        payload length, not the per-record serialized-length sum.
        """
        count = meta.get("output_record_count")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            return False
        if count > self.chunk_records:
            return False
        if self.key_field:
            return False
        if not isinstance(data, bytes) or len(data) > self.chunk_bytes:
            return False
        return True

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

    def commit(self, *, deadline: float | None = None) -> SinkCommitReceipt:
        """Delivery flush/commit barrier (V18-01 section 6).

        ``write()`` already awaits each send future (``future.get``), so send
        failures surface synchronously; ``commit()`` additionally drains the
        producer's linger/network buffer via ``flush()`` so every accepted
        message is broker-acked (acks=all) before the source may be
        acknowledged. A flush timeout or failure raises ``SinkError`` — no
        clean success without confirmation.
        """
        if self._producer is not None:
            if deadline is not None and time.monotonic() >= deadline:
                raise SinkError(f"Kafka commit deadline exceeded for topic '{self.topic}'")
            try:
                if deadline is not None:
                    timeout = max(0.0, deadline - time.monotonic())
                else:
                    timeout = 10  # matches the per-send future.get timeout
                self._producer.flush(timeout=timeout)
            except Exception as exc:
                raise SinkError(f"Kafka commit flush failed to topic '{self.topic}': {exc}") from exc
        return SinkCommitReceipt(
            sink_key=self.__class__.__name__,
            tier=DeliveryTier.REMOTE_DURABLE,
            confirmed=True,
            notes=f"acks={self.acks} producer flush completed; broker confirmed every message",
        )
