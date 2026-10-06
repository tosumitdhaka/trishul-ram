"""Back-to-back local-sink write-path microbench: v1.5.1 vs v1.6.0.

Isolates the RollingWriter/LocalSink write path suspected behind the broad
batch-source (fsweep/s2/s3/s5) E2E regression in the v1.6.0 rerun.
TRAM_BENCH_PATH selects the checkout under test.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.environ.get("TRAM_BENCH_PATH", "/home/dhaka/trishul/trishul-ram"))

from tram.connectors.local.sink import LocalSink  # noqa: E402

REC = {
    "record_id": "cdr-00000001",
    "event_type": "data_session",
    "timestamp_ms": 1760000000000,
    "imsi": "3100123456789012",
    "msisdn": "+819012345678",
    "cell_id": 74001,
    "data_volume_mb": 42.13,
    "rat_type": "NR",
    "cause_code": "0",
    "destination": "apn.example.com",
    "severity": 0.5,
    "active": True,
    "tags": ["pm", "tag3"],
    "nested_info": {"node_name": "node-7", "rack_id": 3},
}


def main() -> None:
    label = os.environ.get("TRAM_BENCH_PATH", "/home/dhaka/trishul/trishul-ram")
    print(f"# checkout: {label}")
    meta = {"pipeline": "bench"}
    for bs, n_writes in [(1000, 50), (100, 100), (10, 200), (1, 300)]:
        blob = ("\n".join(json.dumps(REC) for _ in range(bs)) + "\n").encode()
        with tempfile.TemporaryDirectory() as td:
            sink = LocalSink({
                "type": "local",
                "path": td,
                "filename_template": "bench_{pipeline}_{timestamp}.jsonl",
            })
            for _ in range(3):  # warmup
                sink.write(blob, meta)
            times = []
            for _ in range(5):
                t0 = time.perf_counter()
                for _ in range(n_writes):
                    sink.write(blob, meta)
                times.append(time.perf_counter() - t0)
            med = statistics.median(times)
            print(f"batch={bs:5d} writes={n_writes:4d}: "
                  f"{med / (n_writes * bs) * 1e6:8.3f} us/rec  "
                  f"{med / n_writes * 1e6:8.1f} us/write  "
                  f"({n_writes * bs / med:10,.0f} rec/s)")


if __name__ == "__main__":
    main()
