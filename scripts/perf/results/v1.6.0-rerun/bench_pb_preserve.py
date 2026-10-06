"""Quantify #83 protobuf amplification per mode: default (camelCase rebuild)
vs preserve_keys=true, on the canonical-corpus-style CDR payload.

Companion to the v1.6.0 rerun: the kind fsweep_protobuf cells measure the
DEFAULT config only; this microbench isolates the serializer cost in both
key conventions. Same methodology as bench_serializers.py: n=10,000,
1,000-record warmup, median of 5 reps, single process (WSL2 host).
"""
from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, __import__("os").environ.get("TRAM_BENCH_PATH", "/home/dhaka/trishul/trishul-ram"))

from tram.serializers.protobuf_serializer import ProtobufSerializer  # noqa: E402

PROTO = """\
syntax = "proto3";
package bench;
message Nested {
  string node_name = 1;
  int32 rack_id = 2;
}
message CdrRecord {
  string record_id = 1;
  string event_type = 2;
  uint64 timestamp_ms = 3;
  string imsi = 4;
  string msisdn = 5;
  int32 cell_id = 6;
  double data_volume_mb = 7;
  string rat_type = 8;
  string cause_code = 9;
  string destination = 10;
  float severity = 11;
  bool active = 12;
  repeated string tags = 13;
  Nested nested_info = 14;
}
"""

N = 10_000
WARMUP = 1_000
REPS = 5


def make_records(n: int) -> list[dict]:
    recs = []
    for i in range(n):
        recs.append({
            "record_id": f"cdr-{i:08d}",
            "event_type": "data_session",
            "timestamp_ms": 1_760_000_000_000 + i,
            "imsi": f"3100123456789{i % 1000:04d}",
            "msisdn": f"+8190{i % 100000:08d}",
            "cell_id": 74_000 + (i % 500),
            "data_volume_mb": round(0.01 + (i % 700) * 0.13, 2),
            "rat_type": "NR",
            "cause_code": "0",
            "destination": "apn.example.com",
            "severity": 0.5,
            "active": i % 3 == 0,
            "tags": ["pm", f"tag{i % 7}"],
            "nested_info": {"node_name": f"node-{i % 40}", "rack_id": i % 8},
        })
    return recs


def bench(fn, reps: int = REPS) -> float:
    times: list[float] = []
    for _ in range(reps):
        t0 = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - t0)
        del out
    return statistics.median(times)


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        proto = Path(td) / "cdr.proto"
        proto.write_text(PROTO)
        base = dict(
            type="protobuf",
            schema_file=str(proto),
            message_class="CdrRecord",
            framing="length_delimited",
        )
        s_default = ProtobufSerializer(base)
        s_preserve = ProtobufSerializer({**base, "preserve_keys": True})

        recs = make_records(WARMUP + N)
        warmup, corpus = recs[:WARMUP], recs[WARMUP:]

        # wire bytes via default serializer (camelCase dict input)
        s_default.serialize(warmup)
        blob = s_default.serialize(corpus)

        # warm up both modes (full blob — frame-boundary safe)
        s_default.parse(blob)
        s_preserve.parse(blob)

        t = bench(lambda: s_default.parse(blob))
        print(f"parse   default (camelCase rebuild): {t / N * 1e6:8.3f} us/rec "
              f"{N / t:10,.0f} rec/s")
        t = bench(lambda: s_preserve.parse(blob))
        print(f"parse   preserve_keys=True          : {t / N * 1e6:8.3f} us/rec "
              f"{N / t:10,.0f} rec/s")

        camel = s_default.parse(blob)
        snake = s_preserve.parse(blob)

        t = bench(lambda: s_default.serialize(camel))
        print(f"serialize default (camelCase input): {t / N * 1e6:8.3f} us/rec "
              f"{N / t:10,.0f} rec/s")
        t = bench(lambda: s_preserve.serialize(snake))
        print(f"serialize preserve_keys=True        : {t / N * 1e6:8.3f} us/rec "
              f"{N / t:10,.0f} rec/s")

        t = bench(lambda: (s_default.parse(blob), s_default.serialize(camel)))
        print(f"round-trip default                  : {t / N * 1e6:8.3f} us/rec "
              f"{N / t:10,.0f} rec/s")
        t = bench(lambda: (s_preserve.parse(blob), s_preserve.serialize(snake)))
        print(f"round-trip preserve_keys            : {t / N * 1e6:8.3f} us/rec "
              f"{N / t:10,.0f} rec/s")
        print(f"payload: {len(blob) / N:.1f} B/rec")


if __name__ == "__main__":
    main()
