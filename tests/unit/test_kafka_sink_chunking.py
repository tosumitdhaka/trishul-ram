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


def test_near_cap_eligible_batch_sent_as_single_byte_faithful_message() -> None:
    """Near-cap pinning (perf follow-up 2026-10-07): the legacy cap decision
    sums per-record serialized lengths — an overestimate with indented JSON
    framing — which would split this batch into 2 chunks. The fast path uses
    the ACTUAL payload length (within chunk_bytes) and sends it as ONE
    byte-faithful message."""
    records = [{"seq": i, "pad": "x" * 40} for i in range(2)]
    serializer = JsonSerializer({"indent": 2})
    data = serializer.serialize(records)
    per_record_sum = sum(len(serializer.serialize([record])) for record in records)
    assert per_record_sum > len(data)  # the overestimate that splits the legacy path

    sink = _make_sink({"chunk_bytes": len(data)})
    producer = MagicMock()
    sink._producer = producer
    meta = {
        "serializer_type": "json",
        "serializer_config": {"type": "json", "indent": 2},
        "output_record_count": len(records),
    }

    with (
        patch.object(sink, "_parse_payload") as mock_parse,
        patch("tram.connectors.kafka.sink.chunk_records_by_caps") as mock_chunk,
    ):
        sink.write(data, meta)

    mock_parse.assert_not_called()  # fast path: no re-parse
    mock_chunk.assert_not_called()  # fast path: no re-serialization
    assert producer.send.call_count == 1
    assert producer.send.call_args.kwargs["value"] is data  # byte-faithful


def test_near_cap_batch_without_count_still_split_by_legacy_path() -> None:
    """Counterpart: the same near-cap batch WITHOUT the executor-supplied count
    is ineligible, so the legacy path re-parses and chunks by the per-record
    serialized-length sum — the overestimate splits it into 2 messages."""
    records = [{"seq": i, "pad": "x" * 40} for i in range(2)]
    serializer = JsonSerializer({"indent": 2})
    data = serializer.serialize(records)
    per_record_sum = sum(len(serializer.serialize([record])) for record in records)
    assert per_record_sum > len(data)

    sink = _make_sink({"chunk_bytes": len(data)})
    producer = MagicMock()
    sink._producer = producer
    meta = {"serializer_type": "json", "serializer_config": {"type": "json", "indent": 2}}

    sink.write(data, meta)

    assert producer.send.call_count == 2  # legacy split on the per-record sum
    sent = [call.kwargs["value"] for call in producer.send.call_args_list]
    assert sent == [
        serializer.serialize([records[0]]),
        serializer.serialize([records[1]]),
    ]


def test_fast_path_failure_uses_same_sink_error_as_legacy() -> None:
    """Retry-accounting parity: a fast-path send failure raises the same
    SinkError as the legacy path, so the executor's per-sink retry/backoff/
    circuit-breaker loop (which wraps write()) treats both paths identically —
    one send attempt, then SinkError."""
    records = [{"seq": i} for i in range(2)]
    data = JsonSerializer({}).serialize(records)
    metas = [dict(_JSON_META, output_record_count=2), dict(_JSON_META)]

    errors = []
    send_counts = []
    for meta in metas:
        sink = _make_sink()
        producer = MagicMock()
        future = MagicMock()
        future.get.side_effect = RuntimeError("broker reject")
        producer.send.return_value = future
        sink._producer = producer

        with pytest.raises(SinkError) as excinfo:
            sink.write(data, meta)
        errors.append(str(excinfo.value))
        send_counts.append(producer.send.call_count)
        future.get.assert_called_once_with(timeout=10)  # ack wait on both paths

    assert send_counts == [1, 1]  # one send attempt each, then SinkError
    assert errors[0] == errors[1]  # identical error → identical retry trigger


def test_fast_path_waits_on_ack_future_with_ten_second_timeout() -> None:
    """Ack-wait parity: the fast path blocks on the producer's ack future with
    the same 10s timeout as the legacy path — a message is only 'sent' once the
    broker acknowledges it."""
    sink = _make_sink()
    producer = MagicMock()
    future = MagicMock()
    producer.send.return_value = future
    sink._producer = producer
    data = JsonSerializer({}).serialize([{"seq": 1}, {"seq": 2}])

    sink.write(data, dict(_JSON_META, output_record_count=2))

    producer.send.assert_called_once()
    future.get.assert_called_once_with(timeout=10)