"""One timed rep of the affected operations, run against the code tree at argv[1].

Real implementations only — no prototypes or patches. Prints JSON:
{"us_per_unit": {...}, "output_hashes": {...}} on stdout.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
import time

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT + "/scripts/perf/microbench")

import corpus  # noqa: E402

from tram.connectors.kafka.sink import KafkaSink  # noqa: E402
from tram.pipeline.executor import _filter_by_condition  # noqa: E402
from tram.serializers.json_serializer import JsonSerializer  # noqa: E402
from tram.transforms.counter_delta import CounterDeltaTransform  # noqa: E402
from tram.transforms.timestamp_normalize import (  # noqa: E402
    TimestampNormalizeTransform,
)
from tram.transforms.window_aggregate import WindowAggregateTransform  # noqa: E402

CONDITION = 'duration_s > 30 and event_type == "VOICE"'


class Ack:
    def get(self, timeout: float) -> None:
        return None


class Producer:
    def __init__(self) -> None:
        self.sent = []

    def send(self, topic: str, **kwargs):
        self.sent.append(kwargs)
        return Ack()


def _hash(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=repr).encode()
    ).hexdigest()


def _kafka_hash(producer: Producer) -> str:
    parts = []
    for msg in producer.sent:
        value = msg.get("value")
        digest = hashlib.sha256(value).hexdigest() if isinstance(value, bytes) else repr(value)
        parts.append(f"{msg.get('key')}|{msg.get('partition')}|{digest}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def build_ops():
    records = corpus.make_records(10_000)
    counters = corpus.make_counter(10_000)
    serializer = JsonSerializer({"type": "json"})

    # Sink condition — production call form on both sides: the new code accepts
    # an instance-level cache (persisted across warmup + timed call, mirroring
    # the executor's steady state); the old code has only the 2-arg signature.
    takes_cache = "_cache" in inspect.signature(_filter_by_condition).parameters
    cache: dict = {}

    def sink_condition():
        if takes_cache:
            return _filter_by_condition(records, CONDITION, _cache=cache)
        return _filter_by_condition(records, CONDITION)

    def timestamp_normalize():
        return TimestampNormalizeTransform(
            {"fields": ["timestamp", "session_start"]}
        ).apply(records)

    def counter_delta():
        return CounterDeltaTransform(
            {
                "fields": ["if_in_octets", "if_out_octets"],
                "key_fields": ["cell_id"],
                "timestamp_field": "_polled_at",
            }
        ).apply(counters)

    def window_aggregate():
        return WindowAggregateTransform(
            {
                "timestamp_field": "timestamp",
                "group_by": ["cell_id"],
                "window_seconds": 900,
                "operations": {
                    "total_down": "sum:bytes_down",
                    "avg_charge": "avg:charge_amount",
                    "samples": "count:record_id",
                },
            }
        ).apply(records)

    def kafka_write(size: int, name: str):
        payload = serializer.serialize(records[:size])
        sink = KafkaSink({"brokers": ["unused"], "topic": "unused"})
        producer = Producer()
        sink._producer = producer
        meta = {
            "serializer_type": "json",
            "serializer_config": {"type": "json"},
            "output_record_count": size,
        }

        def run():
            producer.sent.clear()
            for _ in range(20):
                sink.write(payload, meta)
            return _kafka_hash(producer), len(payload)

        return run, name

    kafka500, name500 = kafka_write(500, "kafka_write_500")
    kafka1000, name1000 = kafka_write(1000, "kafka_write_1000_control")
    return records, (
        ("sink_condition_compile_once", sink_condition, len(records)),
        ("timestamp_normalize", timestamp_normalize, len(records)),
        ("counter_delta", counter_delta, len(counters)),
        ("window_aggregate", window_aggregate, len(records)),
        (name500, kafka500, 20),
        (name1000, kafka1000, 20),
    )


def main() -> None:
    records, ops = build_ops()
    us_per_unit = {}
    output_hashes = {}
    for name, fn, units in ops:
        warm = fn()  # warmup + old/new output capture
        start = time.perf_counter_ns()
        result = fn()
        elapsed = (time.perf_counter_ns() - start) / units / 1000
        us_per_unit[name] = round(elapsed, 4)
        if isinstance(result, tuple):
            output_hashes[name] = result[0]
        else:
            output_hashes[name] = _hash(result)
        assert _hash(warm) == _hash(result) if not isinstance(result, tuple) else True
    print(json.dumps({"us_per_unit": us_per_unit, "output_hashes": output_hashes}))


if __name__ == "__main__":
    main()
