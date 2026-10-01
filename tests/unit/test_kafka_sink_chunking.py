"""Issue #76 — kafka sink bounded-batch sends (chunking by count + bytes).

Regression tests: a >1MB batch is delivered as multiple messages with every
record sent in order; a partial-batch failure surfaces as a SinkError with the
already-delivered chunk count (never a silent skip); both caps independently
force chunk boundaries; small batches stay byte-faithful single messages.
"""
from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.kafka.sink import KafkaSink, chunk_records_by_caps
from tram.core.exceptions import SinkError
from tram.serializers.json_serializer import JsonSerializer

_JSON_META = {"serializer_type": "json", "serializer_config": {"type": "json"}}


def _make_sink(config_extra: dict | None = None) -> KafkaSink:
    cfg = {"brokers": ["kafka:9092"], "topic": "events"}
    if config_extra:
        cfg.update(config_extra)
    return KafkaSink(cfg)


def _json_records(count: int, pad: int = 0) -> list[dict]:
    return [{"seq": i, "pad": "x" * pad} for i in range(count)]


def test_large_batch_delivered_in_chunks_all_records_sent() -> None:
    """A >1MB serialized batch must not be sent as one message (which the broker
    rejects with MessageSizeTooLargeError): it is split at record boundaries and
    every record is delivered in order."""
    records = _json_records(2500, pad=500)
    data = JsonSerializer({}).serialize(records)
    assert len(data) > 1_048_576  # exceeds kafka-python's default max_request_size

    sink = _make_sink()
    producer = MagicMock()
    sink._producer = producer
    sink.write(data, dict(_JSON_META))

    sent = [call.kwargs["value"] for call in producer.send.call_args_list]
    assert len(sent) == 3  # 2500 records / 1000-record default cap
    for payload in sent:
        assert len(payload) <= sink.max_request_size
    delivered = [record for payload in sent for record in json.loads(payload)]
    assert delivered == records  # all records, order preserved


def test_record_cap_independently_forces_chunk_boundary() -> None:
    sink = _make_sink(
        {"chunk_records": 2, "chunk_bytes": 10_000_000, "max_request_size": 10_000_000}
    )
    producer = MagicMock()
    sink._producer = producer
    sink.write(JsonSerializer({}).serialize(_json_records(5)), dict(_JSON_META))

    sent = [json.loads(call.kwargs["value"]) for call in producer.send.call_args_list]
    assert [len(chunk) for chunk in sent] == [2, 2, 1]
    assert [r for chunk in sent for r in chunk] == _json_records(5)


def test_byte_cap_independently_forces_chunk_boundary() -> None:
    sink = _make_sink({"chunk_records": 10_000, "chunk_bytes": 500})
    producer = MagicMock()
    sink._producer = producer
    sink.write(
        JsonSerializer({}).serialize(_json_records(10, pad=100)), dict(_JSON_META)
    )

    sent = [json.loads(call.kwargs["value"]) for call in producer.send.call_args_list]
    assert len(sent) > 1  # byte cap alone forces the split
    for chunk in sent:
        assert len(JsonSerializer({}).serialize(chunk)) <= 500
    assert [r for chunk in sent for r in chunk] == _json_records(10, pad=100)


def test_partial_chunk_failure_surfaces_error_and_counts_sent_chunks() -> None:
    """Chunk 2 of 3 fails → a SinkError is raised (never a silent skip) and the
    error reports how many chunks were already delivered."""
    producer = MagicMock()
    calls = {"n": 0}

    def failing_send(topic, value=None, key=None, partition=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("broker reject")
        return MagicMock()

    producer.send.side_effect = failing_send
    sink = _make_sink({"chunk_records": 2})
    sink._producer = producer

    with pytest.raises(SinkError, match="chunk 2 of 3") as excinfo:
        sink.write(JsonSerializer({}).serialize(_json_records(5)), dict(_JSON_META))
    assert "1 chunk(s) already delivered" in str(excinfo.value)
    assert producer.send.call_count == 2  # chunk 3 never attempted


def test_small_batch_stays_single_byte_faithful_message() -> None:
    records = [{"seq": 1}, {"seq": 2}]
    data = JsonSerializer({}).serialize(records)
    sink = _make_sink()
    producer = MagicMock()
    sink._producer = producer
    sink.write(data, dict(_JSON_META))

    assert producer.send.call_count == 1
    assert producer.send.call_args.kwargs["value"] is data  # original bytes, untouched


def test_key_from_first_record_used_for_all_chunks() -> None:
    sink = _make_sink({"key_field": "ne_id", "chunk_records": 2})
    producer = MagicMock()
    sink._producer = producer
    sink.write(
        JsonSerializer({}).serialize(
            [{"ne_id": "ne-1", "seq": i} for i in range(5)]
        ),
        dict(_JSON_META),
    )

    keys = {call.kwargs["key"] for call in producer.send.call_args_list}
    assert keys == {b"ne-1"}  # one key per batch → same partition → order preserved


def test_keyless_chunked_batch_pinned_to_one_partition_for_ordering() -> None:
    """Without a key_field, kafka-python would scatter messages randomly; the
    sink pins the batch's chunks to one stable partition so order holds."""
    sink = _make_sink({"chunk_records": 2})
    producer = MagicMock()
    producer.partitions_for.return_value = {0, 1, 2, 3}
    sink._producer = producer
    sink.write(JsonSerializer({}).serialize(_json_records(5)), dict(_JSON_META))

    partitions = {call.kwargs["partition"] for call in producer.send.call_args_list}
    assert len(partitions) == 1
    assert None not in partitions
    # Same partition is reused for later batches (sticky, chosen once).
    assert sink._sticky_partition in {0, 1, 2, 3}


def test_producer_configured_with_max_request_size() -> None:
    mock_producer = MagicMock()
    mock_kafka = MagicMock()
    mock_kafka.KafkaProducer.return_value = mock_producer
    with patch.dict(sys.modules, {"kafka": mock_kafka}):
        sink = _make_sink({"max_request_size": 2_000_000})
        sink._get_producer()
    assert mock_kafka.KafkaProducer.call_args[1]["max_request_size"] == 2_000_000


def test_runtime_guard_rejects_chunk_bytes_above_max_request_size() -> None:
    with pytest.raises(SinkError, match="chunk_bytes"):
        _make_sink({"chunk_bytes": 2_000_000, "max_request_size": 1_048_576})


def test_chunk_helper_respects_record_and_byte_caps() -> None:
    serializer = JsonSerializer({})
    records = [{"seq": i, "pad": "abcdef"} for i in range(4)]

    # Byte cap tighter than the record cap: boundary falls on the byte cap.
    chunks = chunk_records_by_caps(
        records, serializer=serializer, chunk_records=100, chunk_bytes=60
    )
    assert [len(chunk) for chunk in chunks] == [2, 2]
    assert all(len(serializer.serialize(chunk)) <= 60 for chunk in chunks)

    # Record cap tighter than the byte cap: boundary falls on the record cap.
    chunks = chunk_records_by_caps(
        records, serializer=serializer, chunk_records=2, chunk_bytes=10_000_000
    )
    assert [len(chunk) for chunk in chunks] == [2, 2]
    assert [r for chunk in chunks for r in chunk] == records


def test_model_default_caps_and_validation() -> None:
    from tram.models.pipeline import KafkaSinkConfig

    cfg = KafkaSinkConfig(type="kafka", brokers=["kafka:9092"], topic="t")
    assert cfg.chunk_records == 1000
    assert cfg.chunk_bytes == 524288
    assert cfg.max_request_size == 1048576

    with pytest.raises(ValueError, match="chunk_bytes"):
        KafkaSinkConfig(
            type="kafka",
            brokers=["kafka:9092"],
            topic="t",
            chunk_bytes=2_000_000,
            max_request_size=1_048_576,
        )