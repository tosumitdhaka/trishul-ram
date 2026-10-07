"""Local CPU experiments; prototypes do not change application behavior.

Run from repo root: .venv/bin/python scripts/perf/results/followup-2026-10-07/bench.py
Kafka uses an immediate-ack fake producer: its timings exclude broker/network costs.
Timestamp equivalence is checked on the canonical corpus, not all accepted inputs.
"""

from __future__ import annotations

import json
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts/perf/microbench"))

import corpus  # noqa: E402

from tram.connectors.kafka.sink import KafkaSink  # noqa: E402
from tram.pipeline.executor import _filter_by_condition  # noqa: E402
from tram.serializers.json_serializer import JsonSerializer  # noqa: E402
from tram.transforms.counter_delta import CounterDeltaTransform  # noqa: E402
from tram.transforms.filter_rows import FilterRowsTransform  # noqa: E402
from tram.transforms.timestamp_normalize import (  # noqa: E402
    TimestampNormalizeTransform,
    _parse_timestamp,
)
from tram.transforms.window_aggregate import WindowAggregateTransform  # noqa: E402


def iso_first(
    val: Any, input_format: str | None, source_tz: tzinfo | None = None,
) -> datetime:
    """Prototype ISO fast path, retaining the original parser as fallback."""
    if isinstance(val, str) and input_format is None and "-" in val[1:]:
        try:
            dt = datetime.fromisoformat(val.strip())
        except ValueError:
            pass
        else:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=source_tz or UTC)
            return dt.astimezone(UTC)
    return _parse_timestamp(val, input_format, source_tz)


def paired(
    name: str, baseline: Callable[[], Any], candidate: Callable[[], Any], units: int,
    reps: int = 7, normalize: Callable[[Any], Any] = lambda value: value,
) -> dict[str, Any]:
    assert normalize(baseline()) == normalize(candidate()), name
    timings = {"baseline": [], "candidate": []}
    for rep in range(reps):
        order = [("baseline", baseline), ("candidate", candidate)]
        if rep % 2:
            order.reverse()
        for label, fn in order:
            start = time.perf_counter_ns()
            fn()
            timings[label].append((time.perf_counter_ns() - start) / units / 1000)
    medians = {label: statistics.median(values) for label, values in timings.items()}
    return {"name": name, "units": units, "us_per_unit": medians,
            "speedup": medians["baseline"] / medians["candidate"],
            "raw_us_per_unit": timings, "output_equivalence": True}


def transform_run(
    cls: type, config: dict[str, Any], records: list[dict],
    parser_module: str | None = None, fast_format: bool = False,
) -> list[dict]:
    transform = cls(config)
    if fast_format:
        transform._format = lambda dt: dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if parser_module:
        with patch(parser_module + "._parse_timestamp", iso_first):
            return transform.apply(records)
    return transform.apply(records)


class Ack:
    def get(self, timeout: float) -> None:
        return None


class Producer:
    def __init__(self) -> None:
        self.sent = []

    def send(self, topic: str, **kwargs: Any) -> Ack:
        self.sent.append(kwargs)
        return Ack()


def main() -> None:
    records = corpus.make_records(10_000)
    counters = corpus.make_counter(10_000)
    results = []
    condition = 'duration_s > 30 and event_type == "VOICE"'
    compiled_filter = FilterRowsTransform({"condition": condition})
    results.append(paired(
        "sink_condition_compile_once", lambda: _filter_by_condition(records, condition),
        lambda: compiled_filter.apply(records), len(records),
    ))
    cases = [
        ("timestamp_normalize_iso_first", TimestampNormalizeTransform,
         {"fields": ["timestamp", "session_start"]}, records,
         "tram.transforms.timestamp_normalize", False),
        ("timestamp_normalize_iso_first_and_format", TimestampNormalizeTransform,
         {"fields": ["timestamp", "session_start"]}, records,
         "tram.transforms.timestamp_normalize", True),
        ("counter_delta_iso_first", CounterDeltaTransform,
         {"fields": ["if_in_octets", "if_out_octets"], "key_fields": ["cell_id"],
          "timestamp_field": "_polled_at"}, counters,
         "tram.transforms.counter_delta", False),
        ("window_aggregate_iso_first", WindowAggregateTransform,
         {"timestamp_field": "timestamp", "group_by": ["cell_id"], "window_seconds": 900,
          "operations": {"total_down": "sum:bytes_down", "avg_charge": "avg:charge_amount",
                         "samples": "count:record_id"}}, records,
         "tram.transforms.window_aggregate", False),
    ]
    for name, cls, config, data, module, fmt in cases:
        results.append(paired(
            name, lambda: transform_run(cls, config, data),
            lambda: transform_run(cls, config, data, module, fmt), len(data),
        ))

    serializer = JsonSerializer({"type": "json"})
    for size in (500, 1000):
        payload = serializer.serialize(records[:size])
        sink = KafkaSink({"brokers": ["unused"], "topic": "unused"})
        producer = Producer()
        sink._producer = producer
        meta = {"serializer_type": "json", "serializer_config": {"type": "json"},
                "output_record_count": size}

        def write(fast: bool = False) -> list[dict]:
            producer.sent.clear()
            for _ in range(20):
                if fast and (0 < meta["output_record_count"] <= sink.chunk_records
                             and len(payload) <= sink.chunk_bytes and sink.key_field is None):
                    sink._send_chunk(producer, payload, key=None)
                else:
                    sink.write(payload, meta)
            return producer.sent.copy()

        result = paired(f"kafka_within_caps_{size}_records", write, lambda: write(True), 20)
        result["payload_bytes"] = len(payload)
        result["unit"] = "sink write (network excluded)"
        results.append(result)

    try:
        import orjson
    except ImportError:
        pass
    else:
        results.append(paired("json_parse_vs_orjson", lambda: serializer.parse(payload),
                              lambda: orjson.loads(payload), size))
        results.append(paired("json_serialize_vs_orjson_semantic_equivalence",
                              lambda: serializer.serialize(records),
                              lambda: orjson.dumps(records), len(records), normalize=json.loads))
        results[-1]["note"] = "Equality checked after decoding outside timing; bytes differ."

    output = {
        "date_utc": datetime.now(UTC).isoformat(),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                               text=True).strip(),
        "python": platform.python_version(), "cpu": corpus.cpu_model(),
        "method": "7 alternating paired reps; full-corpus warmup and equality assertion",
        "scope": "Local CPU only; canonical corpus; fresh stateful instances per invocation",
        "results": results,
    }
    Path(__file__).with_name("measurements.json").write_text(json.dumps(output, indent=2) + "\n")
    for result in results:
        print(result["name"], result["us_per_unit"], f'{result["speedup"]:.2f}x')


if __name__ == "__main__":
    main()
