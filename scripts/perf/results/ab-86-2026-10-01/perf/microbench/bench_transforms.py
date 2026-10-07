"""Microbenchmark TRAM transforms on the canonical CDR corpus.

Phase 1c of the TRAM capacity study. Per-record cost at n = 10,000 records
(warmup on 1,000, median of 5 reps, single process), plus a scaling check
at 10k / 50k / 100k for the stateful transforms (deduplicate, aggregate,
window_aggregate, counter_delta) to expose super-linear state growth.

Input shapes (see corpus.py for the canonical record and variants):
  flat CDR        — project, filter, enrich, deduplicate (10% re-deliveries),
                    add_field, rename, mask, regex_extract, value_map,
                    coalesce_fields, timestamp_normalize, template, validate,
                    aggregate, window_aggregate, limit, drop
  nested CDR      — json_flatten, jmespath, unnest (session_info sub-object)
  cast CDR        — cast (charge_amount/cell_id/roaming pre-stringified)
  hex CDR         — hex_decode (sgsn/ggsn as packed-IPv4 hex)
  explode CDR     — explode (charge_breakdown list of 2 components)
  melt CDR        — melt (counters dict of 3 metrics)
  select CDR      — select_from_list (locations list: serving/previous cell)
  counter polls   — counter_delta (cumulative per-cell octet counters)

Stateful transforms get a FRESH instance per timed rep (timing apply() only),
so state accumulates over the batch exactly as it would in one pipeline run.

Numbers are comparative (WSL2 host), not absolute.
"""

from __future__ import annotations

import json
import platform
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import corpus  # noqa: E402

sys.path.insert(0, str(HERE.parents[2]))  # repo root, for tram package

import tram.transforms  # noqa: E402,F401 — triggers @register_transform decorators
from tram.registry import registry  # noqa: E402

RESULTS_DIR = HERE / "results"
COPY_DIR = Path("/tmp/opencode/perf-microbench")

SCALES = [10_000, 50_000, 100_000]


def make_lookup_file(tmpdir: str) -> str:
    """enrich lookup: cell_id → region, vendor (500 cells, CSV)."""
    import random

    rng = random.Random(99)
    regions = ["Kanto", "Kansai", "Chubu", "Tohoku", "Kyushu"]
    vendors = ["Nokia", "Ericsson", "Huawei"]
    path = Path(tmpdir) / "cell_lookup.csv"
    lines = ["cell_id,region,vendor"]
    for cell in range(1, corpus.N_CELLS + 1):
        lines.append(f"{cell},{rng.choice(regions)},{rng.choice(vendors)}")
    path.write_text("\n".join(lines))
    return str(path)


def transform_cases(lookup_file: str) -> list[dict]:
    """Each case: name (registry key), config, corpus builder, flags."""
    def flat(n):
        return corpus.make_records(n)

    def nested(n):
        return corpus.make_nested(n)

    def cast(n):
        return corpus.make_cast(n)

    def hexed(n):
        return corpus.make_hex(n)

    def explode(n):
        return corpus.make_explode(n)

    def melted(n):
        return corpus.make_melt(n)

    def selected(n):
        return corpus.make_select(n)

    def dedup(n):
        return corpus.make_dedup(n)

    def counters(n):
        return corpus.make_counter(n)

    return [
        {
            "name": "project",
            "config": {"fields": {
                "id": "record_id", "msisdn": "msisdn", "event": "event_type",
                "charge": "charge_amount", "cell": "cell_id", "rat": "rat"}},
            "input": flat,
        },
        {
            "name": "filter",
            "config": {"condition": 'duration_s > 30 and event_type == "VOICE"'},
            "input": flat,
        },
        {
            "name": "json_flatten",
            "config": {"separator": "."},
            "input": nested,
            "note": "nested CDR → flat (session_info.* hoisted with dotted keys)",
        },
        {
            "name": "enrich",
            "config": {
                "lookup_file": lookup_file, "lookup_format": "csv",
                "join_key": "cell_id", "add_fields": ["region", "vendor"],
            },
            "input": flat,
            "note": "left-join against 500-row cell_id → region/vendor CSV",
        },
        {
            "name": "deduplicate",
            "config": {"fields": ["record_id"], "keep": "first"},
            "input": dedup,
            "stateful": True,
            "note": "corpus has 10% re-delivered duplicate records",
        },
        {
            "name": "add_field",
            "config": {"fields": {
                "total_bytes": "bytes_up + bytes_down",
                "charge_rounded": "round(charge_amount, 2)"}},
            "input": flat,
        },
        {
            "name": "rename",
            "config": {"fields": {
                "msisdn": "subscriber_number", "charge_amount": "amount",
                "sgsn_addr": "sgsn_ip"}},
            "input": flat,
        },
        {
            "name": "cast",
            "config": {"fields": {
                "charge_amount": "float", "cell_id": "int", "roaming": "bool"}},
            "input": cast,
            "note": "cast-variant corpus (values pre-stringified)",
        },
        {
            "name": "mask",
            "config": {"fields": ["msisdn", "imsi", "imei"], "mode": "hash"},
            "input": flat,
            "note": "sha256 per PII field (PII redaction workload)",
        },
        {
            "name": "regex_extract",
            "config": {
                "field": "msisdn",
                "pattern": "^(?P<ncc>\\d{2})(?P<subscriber>\\d{8})$"},
            "input": flat,
        },
        {
            "name": "jmespath",
            "config": {"fields": {
                "session_up": "session_info.bytes_up",
                "session_down": "session_info.bytes_down",
                "session_start": "session_info.session_start",
                "access_point": "apn"}},
            "input": nested,
            "note": "nested CDR (session_info) input",
        },
        {
            "name": "value_map",
            "config": {"field": "event_type", "mapping": {
                "VOICE": 1, "SMS": 2, "DATA": 3, "ROAMING": 4, "EVENT": 5}},
            "input": flat,
        },
        {
            "name": "coalesce_fields",
            "config": {"fields": {"apn_primary": {
                "sources": ["apn_backup", "apn"], "default": "unknown"}}},
            "input": flat,
            "note": "first source (apn_backup) missing on all records",
        },
        {
            "name": "timestamp_normalize",
            "config": {"fields": ["timestamp", "session_start"]},
            "input": flat,
        },
        {
            "name": "template",
            "config": {"fields": {
                "cdr_ref": "{record_id}:{event_type}",
                "summary": "{direction},{event_type},{duration_s}s"}},
            "input": flat,
        },
        {
            "name": "melt",
            "config": {"value_field": "counters"},
            "input": melted,
            "note": "3 counters per record → 3 output rows per input record",
        },
        {
            "name": "explode",
            "config": {"field": "charge_breakdown"},
            "input": explode,
            "note": "2 billing components per record → 2 output rows",
        },
        {
            "name": "unnest",
            "config": {"field": "session_info"},
            "input": nested,
            "note": "nested CDR input; hoists session_info keys",
        },
        {
            "name": "validate",
            "config": {"rules": {
                "msisdn": {"required": True, "regex": "^\\d{10}$"},
                "duration_s": {"type": "int", "min": 0, "max": 3600},
                "event_type": {"allowed": corpus.EVENT_TYPES},
                "charge_amount": {"type": "float", "min": 0}}},
            "input": flat,
        },
        {
            "name": "aggregate",
            "config": {
                "group_by": ["cell_id", "rat"],
                "operations": {
                    "total_up": "sum:bytes_up", "total_down": "sum:bytes_down",
                    "avg_charge": "avg:charge_amount", "samples": "count:record_id"}},
            "input": flat,
            "stateful": True,
            "note": "batch-local groupby; materializes all values per group "
                    "(list appends, not running accumulators)",
        },
        {
            "name": "window_aggregate",
            "config": {
                "timestamp_field": "timestamp", "group_by": ["cell_id"],
                "window_seconds": 900,
                "operations": {
                    "total_down": "sum:bytes_down", "avg_charge": "avg:charge_amount",
                    "samples": "count:record_id"}},
            "input": flat,
            "stateful": True,
            "note": "needs parseable timestamp (ISO); 500 cell groups, "
                    "watermark-driven tumbling windows",
        },
        {
            "name": "counter_delta",
            "config": {
                "fields": ["if_in_octets", "if_out_octets"],
                "key_fields": ["cell_id"], "timestamp_field": "_polled_at"},
            "input": counters,
            "stateful": True,
            "note": "needs cumulative keyed counters + timestamps; 500 cell "
                    "series x 2 counter fields, 300 s poll interval",
        },
        {
            "name": "limit",
            "config": {"count": 100},
            "input": flat,
        },
        {
            "name": "hex_decode",
            "config": {
                "mode": "hex",
                "overrides": [
                    {"path": "sgsn_addr", "decode_as": "ip", "format": "packed"},
                    {"path": "ggsn_addr", "decode_as": "ip", "format": "packed"}]},
            "input": hexed,
            "note": "packed-IPv4 hex → dotted-quad; other fields untouched "
                    "(mode=hex, no heuristic text decode)",
        },
        {
            "name": "select_from_list",
            "config": {
                "field": "locations",
                "select": [{"name": "serving", "match": {"loc_type": "serving"},
                            "output": {"cell_id": "serving_cell"}}]},
            "input": selected,
            "note": "locations list: serving/previous cell dicts",
        },
        {
            "name": "drop",
            "config": {"fields": ["imsi", "imei", "sgsn_addr", "ggsn_addr"]},
            "input": flat,
        },
    ]


def build_transform(case: dict):
    cls = registry._transforms[case["name"]]
    return cls(case["config"])


def bench_case(case: dict, n: int, reps: int = corpus.REPS) -> dict:
    records = case["input"](n)
    warm_records = case["input"](corpus.WARMUP_RECORDS)
    if case.get("stateful"):
        build_transform(case).apply(warm_records)
        timings = []
        out_counts = []
        for _ in range(reps):
            transform = build_transform(case)
            t0 = time.perf_counter_ns()
            out = transform.apply(records)
            timings.append(time.perf_counter_ns() - t0)
            out_counts.append(len(out))
    else:
        transform = build_transform(case)
        transform.apply(warm_records)
        timings = []
        out_counts = []
        for _ in range(reps):
            t0 = time.perf_counter_ns()
            out = transform.apply(records)
            timings.append(time.perf_counter_ns() - t0)
            out_counts.append(len(out))

    elapsed_ns = corpus.median_ns(timings)
    return {
        "name": case["name"],
        "n_input": n,
        "n_output": sorted(out_counts)[len(out_counts) // 2],
        "us_per_input_record": round(elapsed_ns / n / 1e3, 3),
        "records_s": round(n / (elapsed_ns / 1e9)),
        "config": _jsonable(case["config"]),
        "note": case.get("note"),
    }


def _jsonable(config: dict) -> dict:
    def default(obj):
        if isinstance(obj, Path):
            return str(obj)
        raise TypeError

    return json.loads(json.dumps(config, default=default))


def scaling_check(case: dict) -> dict:
    rows = []
    for n in SCALES:
        row = bench_case(case, n, reps=3)
        rows.append({
            "n": n,
            "us_per_input_record": row["us_per_input_record"],
            "records_s": row["records_s"],
            "n_output": row["n_output"],
        })
    # Super-linear heuristic: per-record cost growth 10k → 100k.
    per_rec = [r["us_per_input_record"] for r in rows]
    growth = round(per_rec[-1] / per_rec[0], 2) if per_rec[0] else None
    return {"name": case["name"], "rows": rows, "cost_growth_10k_to_100k": growth}


def window_group_probe() -> dict:
    """window_aggregate per-record cost vs number of distinct groups.

    Code-path hypothesis: apply() calls _finalize_due_windows() after every
    record (window_aggregate.py), which iterates ALL groups' open windows.
    Per-record cost should therefore scale ~linearly with group cardinality,
    making the whole transform O(n x groups) — quadratic when group count
    grows with volume.
    """
    base_case = next(c for c in transform_cases("") if c["name"] == "window_aggregate")
    rows = []
    for n_groups in (10, 100, 500, 1000, 5000):
        records = corpus.make_records(corpus.N_RECORDS)
        for i, rec in enumerate(records):
            rec["cell_id"] = i % n_groups
        timings = []
        for _ in range(3):
            transform = build_transform(base_case)
            t0 = time.perf_counter_ns()
            transform.apply(records)
            timings.append(time.perf_counter_ns() - t0)
        rows.append({
            "n_groups": n_groups,
            "us_per_input_record": round(corpus.median_ns(timings) / len(records) / 1e3, 3),
        })
    first, last = rows[0]["us_per_input_record"], rows[-1]["us_per_input_record"]
    return {
        "hypothesis": "apply() → _finalize_due_windows() scans every group's "
                      "open windows on every record",
        "rows": rows,
        "cost_growth_10_to_5000_groups": round(last / first, 2) if first else None,
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="tram_microbench_") as tmpdir:
        lookup_file = make_lookup_file(tmpdir)
        cases = transform_cases(lookup_file)

        results = []
        for case in cases:
            results.append(bench_case(case, corpus.N_RECORDS))

        scaling = []
        for case in cases:
            if case.get("stateful"):
                scaling.append(scaling_check(case))

    group_probe = window_group_probe()

    output = {
        "meta": {
            "phase": "1c-transforms",
            "repo_version": "v1.5.1 @ 326d9dd",
            "records": corpus.N_RECORDS,
            "warmup_records": corpus.WARMUP_RECORDS,
            "reps": corpus.REPS,
            "scaling_reps": 3,
            "statistic": "median",
            "cpu": corpus.cpu_model(),
            "python": platform.python_version(),
            "host_note": "WSL2 host — numbers are comparative, not absolute",
            "generated": datetime.now(tz=UTC).isoformat(),
        },
        "results": results,
        "stateful_scaling": scaling,
        "window_aggregate_group_probe": group_probe,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / "transforms.json"
    out_path.write_text(json.dumps(output, indent=2))
    COPY_DIR.mkdir(parents=True, exist_ok=True)
    (COPY_DIR / "transforms.json").write_text(json.dumps(output, indent=2))

    _print_table(results, scaling)
    probe = group_probe
    print("\nwindow_aggregate group-cardinality probe (n=10k, 3 reps):")
    for row in probe["rows"]:
        print(f"  {row['n_groups']:>6} groups → {row['us_per_input_record']:>8} µs/record")
    print(f"  cost growth {probe['rows'][0]['n_groups']} → "
          f"{probe['rows'][-1]['n_groups']} groups: "
          f"{probe['cost_growth_10_to_5000_groups']}x")
    print(f"\nresults → {out_path} (copied to {COPY_DIR}/transforms.json)")


def _print_table(results: list[dict], scaling: list[dict]) -> None:
    ordered = sorted(results, key=lambda r: -r["us_per_input_record"])
    print(f"{'transform':<20} {'µs/input rec':>13} {'rec/s':>12} "
          f"{'out recs':>10}  note")
    print("-" * 110)
    for r in ordered:
        note = r["note"] or ""
        if len(note) > 55:
            note = note[:52] + "..."
        print(f"{r['name']:<20} {r['us_per_input_record']:>13} "
              f"{r['records_s']:>12,} {r['n_output']:>10,}  {note}")

    print("\nstateful scaling (µs per input record; growth = 100k / 10k):")
    print(f"{'transform':<20} {'10k':>10} {'50k':>10} {'100k':>10} {'growth':>8}")
    print("-" * 62)
    for s in scaling:
        per_rec = [row["us_per_input_record"] for row in s["rows"]]
        print(f"{s['name']:<20} {per_rec[0]:>10} {per_rec[1]:>10} {per_rec[2]:>10} "
              f"{s['cost_growth_10k_to_100k']:>7}x")


if __name__ == "__main__":
    main()
