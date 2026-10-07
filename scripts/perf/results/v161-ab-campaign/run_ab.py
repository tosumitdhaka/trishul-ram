#!/usr/bin/env python3
"""v1.6.1 A/B campaign driver — affected pipelines on the kind cluster.

Adapted from the v1.6.0-rerun run_cell.py (proven mechanics): stage inputs
into /data/perf/in on every worker, run scripts/perf/run_one.sh (register →
trigger → poll → collect → delete), diff run-history rows against a pre-run
snapshot, write bench-summary.json + append to ab-results.csv.

Usage: run_ab.py <v160|v161> <t4|t6|s7|s7k> <rep>
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REPO = Path("/home/dhaka/trishul/trishul-ram")
PERF = REPO / "scripts/perf"
ROOT = Path("/tmp/opencode/v161-campaign")
RESULTS = ROOT / "results"
PY = str(REPO / ".venv/bin/python")
NS = "trishul-ram"
API = "http://127.0.0.1:30001"
KAFKA_HOST = "172.19.0.2:31031"
PODS = ["trishul-ram-worker-0", "trishul-ram-worker-1", "trishul-ram-worker-2"]
ABORT_S = 900

SCEN: dict[str, tuple[str, str]] = {
    "t4": ("t4_counter_delta_window.yaml", "t4-counter-delta-window"),
    "t6": ("t6_sink_conditions.yaml", "t6-sink-conditions"),
    "s7": ("s7_local_kafka.yaml", "s7-local-kafka"),
    "s7k": ("s7k_local_kafka_keyless.yaml", "s7k-local-kafka-keyless"),
}

NOTES = {
    "t4": "counter_delta+window_aggregate (timestamp kernel)",
    "t6": "2 conditional sinks, thread_workers 4 (compile-once conditions)",
    "s7": "kafka sink, KEYED (control — v1.6.1 does not change this path)",
    "s7k": "kafka sink, KEYLESS (v1.6.1 fast-path eligible)",
}


def kexec(pod: str, cmd: str, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(["kubectl", "exec", "-n", NS, pod, "--", "sh", "-c", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def stage() -> None:
    for pod in PODS:
        rc, out = kexec(
            pod,
            "mkdir -p /data/perf/in /data/perf/out && rm -f /data/perf/in/* /data/perf/out/* && "
            "cp /data/perf/src/json/*.jsonl /data/perf/in/ && ls /data/perf/in | wc -l",
        )
        if rc != 0 or out.strip() != "10":
            raise RuntimeError(f"staging failed on {pod}: rc={rc} {out}")


def fetch_runs(pipeline: str) -> list[dict]:
    with urllib.request.urlopen(f"{API}/api/runs?pipeline={pipeline}&limit=1000", timeout=30) as r:
        return json.load(r)


def summarize(rows: list[dict]) -> dict:
    finished = [r for r in rows if r.get("finished_at")]
    err_msgs = [r.get("error") for r in finished if r.get("error")]
    for r in finished:
        err_msgs.extend((r.get("errors") or [])[:3])
    starts = [datetime.fromisoformat(r["started_at"]) for r in finished]
    ends = [datetime.fromisoformat(r["finished_at"]) for r in finished]
    wall = (max(ends) - min(starts)).total_seconds() if finished else 0.0
    return {
        "rows": len(rows),
        "records_in": sum(int(r.get("records_in") or 0) for r in finished),
        "records_out": sum(int(r.get("records_out") or 0) for r in finished),
        "records_skipped": sum(int(r.get("records_skipped") or 0) for r in finished),
        "bytes_in": sum(int(r.get("bytes_in") or 0) for r in finished),
        "bytes_out": sum(int(r.get("bytes_out") or 0) for r in finished),
        "errors": len(err_msgs),
        "error_msgs": err_msgs[:5],
        "statuses": sorted({r.get("status") for r in finished if r.get("status")}),
        "wall_s": wall,
        "nodes": sorted({r.get("node") for r in finished if r.get("node")}),
    }


def kafka_end_offsets(topic: str) -> int | None:
    code = (
        "from kafka import KafkaConsumer\n"
        "from kafka.structs import TopicPartition\n"
        f"c = KafkaConsumer(bootstrap_servers='{KAFKA_HOST}', request_timeout_ms=15000)\n"
        f"tps = [TopicPartition('{topic}', p) for p in sorted(c.partitions_for_topic('{topic}') or [0])]\n"
        "print(sum(c.end_offsets(tps).values()))\n"
    )
    p = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=60)
    try:
        return int(p.stdout.strip())
    except ValueError:
        return None


def main() -> int:
    side, scenario, rep = sys.argv[1], sys.argv[2], int(sys.argv[3])
    template, pipeline = SCEN[scenario]
    run_id = f"ab-{side}-{scenario}-rep{rep}"
    run_dir = RESULTS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    stage()
    pre_ids = {r.get("run_id") for r in fetch_runs(pipeline)}
    offsets_before = kafka_end_offsets("perf-cdr-out") if scenario.startswith("s7") else None
    t0 = datetime.now(UTC)
    print(f"[{run_id}] start {t0.isoformat()}", flush=True)

    args = ["env", f"TRAM_NAMESPACE={NS}", f"PERF_PYTHON={PY}",
            f"PERF_HELM_VALUES={PERF / 'infra/values-mgrworker.yaml'}",
            f"PERF_RESULTS_DIR={RESULTS}",
            "bash", str(PERF / "run_one.sh"), str(PERF / "templates" / template), run_id,
            "--warmup", "5", "--duration", "300"]
    log_f = (run_dir / "run.log").open("w")
    proc = subprocess.Popen(args, stdout=log_f, stderr=subprocess.STDOUT, cwd=str(REPO))
    deadline = time.monotonic() + ABORT_S
    aborted = None
    while proc.poll() is None:
        if time.monotonic() > deadline:
            aborted = f"run_one exceeded {ABORT_S}s"
            proc.terminate()
            break
        time.sleep(2)
    rc = proc.wait()
    log_f.close()
    print(f"[{run_id}] run_one rc={rc} aborted={aborted}", flush=True)

    rows = [r for r in fetch_runs(pipeline) if r.get("run_id") not in pre_ids]
    (run_dir / "run-rows-raw.json").write_text(json.dumps(rows, indent=1) + "\n")
    hist = summarize(rows)
    offsets_after = kafka_end_offsets("perf-cdr-out") if scenario.startswith("s7") else None

    duration_s = round(hist["wall_s"], 1)
    throughput_out = round(hist["records_out"] / duration_s, 1) if duration_s > 0 else 0.0
    throughput_in = round(hist["records_in"] / duration_s, 1) if duration_s > 0 else 0.0
    bench = {
        "run_id": run_id, "side": side, "scenario": scenario, "rep": rep,
        "scenario_note": NOTES[scenario], "rep_started_at": t0.isoformat(),
        "run_one_rc": rc, "aborted": aborted, "run_history": hist,
        "kafka_offsets": ({"before": offsets_before, "after": offsets_after}
                          if offsets_before is not None else None),
        "metrics": {"records_in": hist["records_in"], "records_out": hist["records_out"],
                    "records_skipped": hist["records_skipped"], "errors": hist["errors"],
                    "duration_s": duration_s, "throughput_in_recs_s": throughput_in,
                    "throughput_out_recs_s": throughput_out},
    }
    (run_dir / "bench-summary.json").write_text(json.dumps(bench, indent=2) + "\n")

    csv_path = ROOT / "ab-results.csv"
    exists = csv_path.exists()
    with csv_path.open("a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["run_id", "side", "scenario", "rep", "records_in", "records_out",
                        "records_skipped", "errors", "duration_s", "throughput_in_recs_s",
                        "throughput_out_recs_s", "note"])
        w.writerow([run_id, side, scenario, rep, hist["records_in"], hist["records_out"],
                    hist["records_skipped"], hist["errors"], duration_s, throughput_in,
                    throughput_out, NOTES[scenario]])
    print(f"[{run_id}] metrics={json.dumps(bench['metrics'])} nodes={hist['nodes']}", flush=True)
    return 0 if not aborted else 1


if __name__ == "__main__":
    sys.exit(main())
