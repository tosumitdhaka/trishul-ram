"""v1.6.1 deployment-Python re-measure (paired, real old vs real new code).

Parent driver: alternately spawns child processes whose PYTHONPATH points at
the OLD code (v1.6.0 @ fdcf371, mounted at /old) and the NEW code (release/
v1.6.1 HEAD, mounted at /new). Each child measures one interleaved rep of the
affected operations with the REAL implementations (no prototypes, no patches)
and returns per-op timings plus output hashes; the parent asserts old-vs-new
output equality and reports medians.

Run inside the deployment image (Python 3.13, deployment dependency versions):
  docker run --rm \
    -v /home/dhaka/trishul/trishul-ram:/new:ro \
    -v /tmp/opencode/tram-v160-baseline:/old:ro \
    -v /tmp/opencode/remeasure161:/bench -w /bench \
    --entrypoint python3 trishul-ram-worker:local-20261006092713 driver.py
"""

from __future__ import annotations

import json
import platform
import statistics
import subprocess
import sys
from datetime import UTC, datetime

REPS = 7
SIDES = ("old", "new")
ROOTS = {"old": "/old", "new": "/new"}

OPS = [
    "sink_condition_compile_once",
    "timestamp_normalize",
    "counter_delta",
    "window_aggregate",
    "kafka_write_500",
    "kafka_write_1000_control",
]


def child(side: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "/bench/child.py", ROOTS[side]],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


def main() -> None:
    samples: dict[str, dict[str, list[float]]] = {
        side: {op: [] for op in OPS} for side in SIDES
    }
    hashes: dict[str, dict[str, str]] = {}
    raw: list[dict] = []
    for rep in range(REPS):
        order = list(SIDES) if rep % 2 == 0 else list(reversed(SIDES))
        for side in order:
            result = child(side)
            result["rep"] = rep
            result["side"] = side
            raw.append(result)
            for op, us in result["us_per_unit"].items():
                samples[side][op].append(us)
            for op, digest in result["output_hashes"].items():
                if op in hashes and hashes[op] != digest:
                    raise SystemExit(
                        f"OUTPUT MISMATCH old vs new for {op}: "
                        f"{hashes[op]} != {digest}"
                    )
                hashes[op] = digest

    medians = {
        side: {op: statistics.median(vals) for op, vals in samples[side].items()}
        for side in SIDES
    }
    speedups = {
        op: medians["old"][op] / medians["new"][op] for op in OPS
    }
    output = {
        "date_utc": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "method": (
            f"{REPS} alternating interleaved child-process reps (old/new), "
            "real implementations both sides, deployment image dependencies; "
            "medians reported; output equality asserted every rep"
        ),
        "us_per_unit": medians,
        "speedup": speedups,
        "output_hashes_match": True,
        "raw_reps": raw,
    }
    with open("/bench/remeasure.json", "w") as f:
        json.dump(output, f, indent=2)
    for op in OPS:
        old, new = medians["old"][op], medians["new"][op]
        print(f"{op:34s} old {old:9.3f}  new {new:9.3f} us/unit  {old / new:6.2f}x")


if __name__ == "__main__":
    main()
