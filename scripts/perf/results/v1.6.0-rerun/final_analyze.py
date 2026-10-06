#!/usr/bin/env python3
"""v1.6.0 re-run vs baseline — medians, ratios, exit-target table data.

Baseline medians computed directly from the 2026-10 study CSVs under
scripts/perf/results/ (never modified). Rerun medians from /tmp/opencode/v160-rerun/csv/.
Duplicate run_ids (appended re-runs) are resolved by keeping the LAST row.
"""
import csv
import statistics
from pathlib import Path

ROOT = Path("/tmp/opencode/v160-rerun")
BASE = Path("/home/dhaka/trishul/trishul-ram/scripts/perf/results")


def medians(csv_path):
    """{(scenario, profile): median throughput} — last row wins per run_id."""
    rows_by_id = {}
    if not Path(csv_path).exists():
        return {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            rows_by_id[row["run_id"]] = row
    cells = {}
    for row in rows_by_id.values():
        if int(row["errors"]) > 0:
            continue  # broken rep (e.g. manager-recovered run) — flagged separately
        cells.setdefault((row["scenario"], row["res_profile"]), []).append(
            float(row["throughput_recs_s"]))
    return {k: statistics.median(v) for k, v in cells.items()}


def full_rows(csv_path):
    rows_by_id = {}
    if not Path(csv_path).exists():
        return {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            rows_by_id[row["run_id"]] = row
    return rows_by_id


def table(title, rerun_csv, base_csv):
    r = medians(rerun_csv)
    b = medians(base_csv)
    print(f"\n=== {title} (median rec/s, rerun vs baseline) ===")
    print(f"{'cell':28s} {'baseline':>10s} {'rerun':>10s} {'ratio':>7s}")
    for key in sorted(set(r) | set(b), key=lambda x: (x[0], x[1])):
        bv, rv = b.get(key), r.get(key)
        if not bv or rv is None:
            continue
        print(f"{key[0] + ' @' + key[1]:28s} {bv:10.0f} {rv:10.0f} {rv / bv:6.2f}x")


if __name__ == "__main__":
    table("Matrix A — mw", ROOT / "csv/matrix-a-mw.csv", BASE / "matrix-a-mw.csv")
    table("Matrix A — single", ROOT / "csv/matrix-a-single.csv", BASE / "matrix-a-single.csv")
    table("Matrix B — mw (format sweep)", ROOT / "csv/matrix-b-mw.csv", BASE / "matrix-b-mw.csv")
    table("Matrix C — mw (transforms)", ROOT / "csv/matrix-c-mw.csv", BASE / "matrix-c-mw.csv")

    print("\n=== integrity rows (rerun): records_out vs records_in, skipped, errors ===")
    for m in ("a", "b", "c"):
        for topo in ("mw", "single"):
            p = ROOT / "csv" / f"matrix-{m}-{topo}.csv"
            if not p.exists():
                continue
            for row in full_rows(p).values():
                ri, ro = int(row["records_in"]), int(row["records_out"])
                if ri != ro and row["scenario"] not in ("t1", "t4"):
                    print(f"  MISMATCH {row['run_id']}: in={ri} out={ro} errors={row['errors']}")
    print("  (t1: filter drops ~20% by design; t4: window aggregate emits 8 window rows by design)")
