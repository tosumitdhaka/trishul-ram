#!/usr/bin/env python3
"""v1.6.0 re-run saturation ladder — s1 webhook, SINGLE topology @ M profile.

Clean per-500m-pod webhook capacity measurement (one pod takes the whole
stream). Baseline: scripts/perf/results/saturation-s1-single.csv (v1.5.1,
ceiling ~280 rps, CPU-pinned at 495m). Same method as the mw ladder
(ladder.py) but: webhook port 30001 (single topology), values single-M.yaml,
steps extended past the baseline's 4 to find the true ceiling.
"""
import csv
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, "/tmp/opencode/v160-rerun/drivers")
from run_cell import PERF, fetch_runs, summarize  # noqa: E402

REPO = Path("/home/dhaka/trishul/trishul-ram")
ROOT = Path("/tmp/opencode/v160-rerun")
RESULTS = ROOT / "results"
STEADY = ROOT / "steady"
PY = str(REPO / ".venv/bin/python")
NS = "trishul-ram"
API = "http://127.0.0.1:30001"
CORPUS = "/tmp/opencode/perf-a2/corpus_100k.jsonl"
VALUES = ROOT / "values" / "single-M.yaml"


def run_step(pipeline, template, run_id, lg_bash, warmup, duration, collect_dur):
    t0 = datetime.now(UTC)
    pre_ids = {r.get("run_id") for r in fetch_runs(pipeline)}
    log_path = RESULTS / run_id / "run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["env", f"TRAM_NAMESPACE={NS}", f"PERF_PYTHON={PY}",
           f"PERF_HELM_VALUES={VALUES}",
           "bash", str(PERF / "run_one.sh"), str(PERF / "templates" / template), run_id,
           "--warmup", str(warmup), "--duration", str(duration), "--loadgen", lg_bash]
    with log_path.open("w") as lf:
        proc = subprocess.Popen(cmd, cwd=str(REPO), stdout=lf, stderr=lf)
        start = time.monotonic()
        time.sleep(warmup if warmup >= 30 else 5)
        steady = subprocess.Popen(
            [PY, str(PERF / "collector/collect.py"), "--run-id", run_id,
             "--duration", str(collect_dur), "--interval", "5", "--namespace", NS,
             "--api-url", API, "--pipeline", pipeline,
             "--helm-values", str(VALUES),
             "--results-dir", str(STEADY / run_id)],
            cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rc = None
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if time.monotonic() - start > 900:
                proc.terminate()
                rc = "aborted"
                break
            time.sleep(2)
        try:
            steady.wait(timeout=400)
        except subprocess.TimeoutExpired:
            steady.terminate()
    rows = [r for r in fetch_runs(pipeline) if r.get("run_id") not in pre_ids]
    return log_path.read_text(), rc, rows


def parse_loadgens(log_text, tool):
    out = []
    for line in log_text.splitlines():
        line = line.strip()
        if line.startswith('{"tool":') and f'"{tool}"' in line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def peaks(run_id):
    f = STEADY / run_id / "samples.csv"
    if not f.exists():
        return 0.0, 0.0, ""
    cpu, mem = {}, {}
    with f.open() as fh:
        for row in csv.DictReader(fh):
            p = row["pod"]
            cpu[p] = max(cpu.get(p, 0.0), float(row["cpu"].rstrip("m") or 0))
            mem[p] = max(mem.get(p, 0.0), float(row["memory"].rstrip("Mi") or 0))
    tram = {p: v for p, v in cpu.items() if p.startswith("trishul-ram")}
    peak = max(tram.values()) if tram else (max(cpu.values()) if cpu else 0)
    peak_pod = max(tram, key=tram.get) if tram else ""
    return peak, (max(mem.values()) if mem else 0), peak_pod


def s1_ladder():
    steps = [(100, 1), (200, 1), (400, 1), (800, 2), (1600, 3), (3200, 6)]
    out = ROOT / "csv" / "saturation-s1-single.csv"
    with out.open("w", newline="") as fh:
        csv.writer(fh).writerow(["step", "offered_rps", "achieved_2xx_rps", "p95_ms",
                                 "pod_cpu_peak_m", "notes"])
    for i, (rate, k) in enumerate(steps, 1):
        per = rate // k
        run_id = f"sat-s1-single-M-step{i}-{rate}rps"
        lines = ["sleep 5"]
        for j in range(1, k + 1):
            lines.append(
                f"{PY} {PERF}/generators/loadgen_webhook.py "
                f"--url http://127.0.0.1:30001/webhooks/ingest "
                f"--rate {per} --concurrency 50 --duration 180 "
                f"--payload-file {CORPUS} "
                f"--summary /tmp/lg-{run_id}-{j}.json &")
        lines.append("wait")
        script = ROOT / f"lg-s1-single-step{i}.sh"
        script.write_text("\n".join(lines) + "\n")
        log_text, rc, rows = run_step("s1-webhook-local", "s1_webhook_local.yaml",
                                      run_id, f"bash {script}", 60, 120, 150)
        lgs = parse_loadgens(log_text, "loadgen_webhook")
        sent = sum(l.get("sent", 0) for l in lgs)
        ok = sum(l.get("http_2xx", 0) for l in lgs)
        c4 = sum(l.get("http_4xx", 0) for l in lgs)
        c5 = sum(l.get("http_5xx", 0) for l in lgs)
        p50s = [l.get("latency_ms", {}).get("p50") for l in lgs if l.get("latency_ms")]
        p95s = [l.get("latency_ms", {}).get("p95") for l in lgs if l.get("latency_ms")]
        p50 = sum(p50s) / len(p50s) if p50s else 0
        p95 = sum(p95s) / len(p95s) if p95s else 0
        hist = summarize(rows)
        cpu, mem, peak_pod = peaks(run_id)
        achieved = ok / 180
        pct = 100.0 * ok / sent if sent else 0
        notes = (f"k={k} sent={sent} 2xx={ok} 4xx={c4} 5xx={c5} p50={p50}ms "
                 f"records_in={hist['records_in']} records_out={hist['records_out']} "
                 f"skipped={hist['records_skipped']} nodes={','.join(hist['nodes'])} "
                 f"rows={hist['rows']} mem_peak={mem:.0f}Mi peak_pod={peak_pod}")
        with out.open("a", newline="") as fh:
            csv.writer(fh).writerow([i, rate, round(achieved, 1), round(p95, 1), cpu, notes])
        print(f"[{run_id}] offered={rate} 2xx={ok}/{sent} ({pct:.1f}%) p50={p50}ms "
              f"p95={p95}ms cpu={cpu}m rc={rc}", flush=True)
        json.dump({"step": i, "offered": rate, "k": k, "sent": sent, "2xx": ok, "4xx": c4,
                   "5xx": c5, "p50": p50, "p95": p95, "hist": hist, "cpu": cpu, "mem": mem,
                   "loadgens": lgs},
                   open(RESULTS / run_id / "bench-summary.json", "w"), indent=1, default=str)
        if pct < 95 or c5 > 0:
            print(f"  -> failure mode at {rate} rps; stopping ladder", flush=True)
            break
        time.sleep(20)


if __name__ == "__main__":
    s1_ladder()
