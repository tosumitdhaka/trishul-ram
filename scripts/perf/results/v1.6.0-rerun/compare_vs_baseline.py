"""Join baseline vs v1.6.0-rerun matrix CSVs into per-cell median tables."""
from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from pathlib import Path

BASE = Path("/home/dhaka/trishul/trishul-ram/scripts/perf/results")
RERUN = Path("/tmp/opencode/v160-rerun/csv")


def load(path: Path) -> dict[tuple[str, str, str], list[dict]]:
    out: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    if not path.exists():
        return out
    for row in csv.DictReader(path.open()):
        key = (row["run_id"].rsplit("-rep", 1)[0], row["scenario"], row["res_profile"])
        out[key].append(row)
    return out


def table(name: str, fname: str) -> list[str]:
    base, rerun = load(BASE / fname), load(RERUN / fname)
    keys = sorted(set(base) | set(rerun), key=lambda k: (k[1], k[2], k[0]))
    lines = [f"\n### {name}\n", "| cell | baseline med rec/s | rerun med rec/s | ratio | base out==in | rerun out==in | rerun errors |",
             "|---|---|---|---|---|---|---|"]
    for k in keys:
        run_id, scen, prof = k
        b = [float(r["throughput_recs_s"]) for r in base.get(k, [])]
        v = [float(r["throughput_recs_s"]) for r in rerun.get(k, [])]
        bmed = statistics.median(b) if b else None
        vmed = statistics.median(v) if v else None
        ratio = f"{vmed / bmed:.2f}x" if (bmed and vmed) else "-"
        b_ok = all(r["records_in"] == r["records_out"] for r in base.get(k, [])) if base.get(k) else "-"
        v_ok = all(r["records_in"] == r["records_out"] for r in rerun.get(k, [])) if rerun.get(k) else "-"
        errs = sum(int(r["errors"]) for r in rerun.get(k, [])) if rerun.get(k) else "-"
        lines.append(f"| {run_id} | {bmed or '-'} | {vmed or '-'} | {ratio} | {b_ok} | {v_ok} | {errs} |")
    return lines


print("# v1.6.0 rerun vs baseline — per-cell throughput medians")
for name, fname in [
    ("Matrix A single", "matrix-a-single.csv"),
    ("Matrix A mw", "matrix-a-mw.csv"),
    ("Matrix B single", "matrix-b-single.csv"),
    ("Matrix B mw", "matrix-b-mw.csv"),
    ("Matrix C single", "matrix-c-single.csv"),
    ("Matrix C mw", "matrix-c-mw.csv"),
]:
    print("\n".join(table(name, fname)))
