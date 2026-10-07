#!/usr/bin/env python3
"""v1.6.0 re-run vs baseline medians + exit-target evaluation."""
import csv
import glob
import json
import statistics
from pathlib import Path

ROOT = Path("/tmp/opencode/v160-rerun")
REPO = Path("/home/dhaka/trishul/trishul-ram")
BASE = REPO / "scripts/perf/results"

# baseline medians (from the 2026-10 capacity study CSVs)
BASELINE_A_MW = {
    ("s1", "L"): 100.0, ("s1", "M"): 298.2, ("s1", "H"): 594.0,
    ("s2csv", "L"): 26741.0, ("s2csv", "M"): 54205.8, ("s2csv", "H"): 107721.0,
    ("s2pmxml", "L"): 4598.0, ("s2pmxml", "M"): 9760.0, ("s2pmxml", "H"): 20601.0,
    ("s3", "L"): 1523.0, ("s3", "M"): 3315.0, ("s3", "H"): 6020.0,
    ("s5", "L"): 10703.0, ("s5", "M"): 12840.0, ("s5", "H"): 13472.0,
    ("s6", "L"): 500.0, ("s6", "M"): 1424.0, ("s6", "H"): 1371.0,
}
BASELINE_A_SINGLE = {
    ("s2csv", "M"): 51587.0, ("s2csv", "H"): 111111.0,
    ("s2pmxml", "M"): 17712.0, ("s2pmxml", "H"): 38462.0,
    ("s3", "M"): 6417.0, ("s3", "H"): 11458.0,
    ("s5", "M"): 9768.0,
}
BASELINE_B = {
    "fsweep_csv": (28200, 57190, 111100), "fsweep_json": (42600, 87121, 166700),
    "fsweep_parquet": (34400, 77381, 183300), "fsweep_ndjson": (None, 55556, None),
    "fsweep_xml": (None, 11561, None), "fsweep_pmxml": (None, 12603, None),
    "fsweep_msgpack": (None, 145833, None), "fsweep_avro": (None, 29857, None),
    "fsweep_protobuf": (None, 10363, None),
}
BASELINE_C = {
    "t1": (9300, 19039, 39900), "t2": (None, 28175, None), "t3": (None, 76923, None),
    "t4": (None, 14085, None), "t5": (3800, 7843, 16000),
}
PROF_IDX = {"L": 0, "M": 1, "H": 2}


def medians(csv_path):
    cells = {}
    if not Path(csv_path).exists():
        return cells
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            key = (row["scenario"], row["res_profile"])
            cells.setdefault(key, []).append(float(row["throughput_recs_s"]))
    return {k: statistics.median(v) for k, v in cells.items()}


def table(name, cells, baseline):
    print(f"\n== {name} (median of 2 reps) ==")
    print(f"{'scenario':16s} {'prof':4s} {'baseline':>10s} {'rerun':>10s} {'ratio':>7s}")
    for (scen, prof) in sorted(cells, key=lambda x: (x[0], x[1])):
        base = baseline.get((scen, prof))
        if base is None and scen in BASELINE_B:
            base = BASELINE_B[scen][PROF_IDX[prof]]
        if base is None and scen in BASELINE_C:
            base = BASELINE_C[scen][PROF_IDX[prof]]
        v = cells[(scen, prof)]
        r = f"{v/base:6.2f}x" if base else "  n/a "
        print(f"{scen:16s} {prof:4s} {base if base else 'n/a':>10} {v:10.0f} {r}")


if __name__ == "__main__":
    for topo in ("mw", "single"):
        for m in ("a", "b", "c"):
            cells = medians(ROOT / "csv" / f"matrix-{m}-{topo}.csv")
            if cells:
                base = BASELINE_A_MW if (m == "a" and topo == "mw") else \
                       (BASELINE_A_SINGLE if (m == "a" and topo == "single") else {})
                table(f"matrix-{m}-{topo}", cells, base)
