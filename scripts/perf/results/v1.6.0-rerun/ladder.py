#!/usr/bin/env python3
"""v1.6.0 re-run saturation ladders (s1 webhook, s6 kafka) at M profile, mw.

Adapted from the study's /tmp/opencode/perf-b/ladder.py; writes results under
/tmp/opencode/v160-rerun/ (never the repo baseline dirs).
"""
import csv
import json
import re
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
KAFKA = "172.19.0.2:30094"
CORPUS = "/tmp/opencode/perf-a2/corpus_100k.jsonl"


def run_step(pipeline, template, run_id, lg_bash, warmup, duration, collect_dur):
    t0 = datetime.now(UTC)
    pre_ids = {r.get("run_id") for r in fetch_runs(pipeline)}
    log_path = RESULTS / run_id / "run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["env", f"TRAM_NAMESPACE={NS}", f"PERF_PYTHON={PY}",
           f"PERF_HELM_VALUES={ROOT / 'values' / 'mw-M.yaml'}",
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
             "--helm-values", str(ROOT / "values" / "mw-M.yaml"),
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
    workers = {p: v for p, v in cpu.items() if "worker" in p}
    peak = max(workers.values()) if workers else (max(cpu.values()) if cpu else 0)
    peak_pod = max(workers, key=workers.get) if workers else ""
    return peak, (max(mem.values()) if mem else 0), peak_pod


def s1_ladder():
    steps = [100, 200, 400, 800, 1600, 3200, 6400]
    procs_for = {100: 1, 200: 1, 400: 1, 800: 2, 1600: 3, 3200: 6, 6400: 10}
    out = ROOT / "csv" / "saturation-s1.csv"
    with out.open("w", newline="") as fh:
        csv.writer(fh).writerow(["step", "offered_rps", "achieved_2xx_rps", "p95_ms",
                                 "worker_cpu_peak_m", "notes"])
    for i, rate in enumerate(steps, 1):
        k = procs_for[rate]
        per = rate // k
        run_id = f"sat-s1-M-step{i}-{rate}rps"
        lines = ["sleep 5"]
        for j in range(1, k + 1):
            lines.append(
                f"{PY} {PERF}/generators/loadgen_webhook.py "
                f"--url http://127.0.0.1:30002/webhooks/ingest "
                f"--rate {per} --concurrency 50 --duration 180 "
                f"--payload-file {CORPUS} "
                f"--summary /tmp/lg-{run_id}-{j}.json &")
        lines.append("wait")
        script = ROOT / f"lg-s1-step{i}.sh"
        script.write_text("\n".join(lines) + "\n")
        log_text, rc, rows = run_step("s1-webhook-local", "s1_webhook_local.yaml",
                                      run_id, f"bash {script}", 60, 120, 150)
        lgs = parse_loadgens(log_text, "loadgen_webhook")
        sent = sum(l.get("sent", 0) for l in lgs)
        ok = sum(l.get("http_2xx", 0) for l in lgs)
        c4 = sum(l.get("http_4xx", 0) for l in lgs)
        c5 = sum(l.get("http_5xx", 0) for l in lgs)
        p95s = [l.get("latency_ms", {}).get("p95") for l in lgs if l.get("latency_ms")]
        p95 = sum(p95s) / len(p95s) if p95s else 0
        hist = summarize(rows)
        cpu, mem, peak_pod = peaks(run_id)
        achieved = ok / 180
        pct = 100.0 * ok / sent if sent else 0
        notes = (f"k={k} sent={sent} 2xx={ok} 4xx={c4} 5xx={c5} "
                 f"records_in={hist['records_in']} records_out={hist['records_out']} "
                 f"skipped={hist['records_skipped']} nodes={','.join(hist['nodes'])} "
                 f"mem_peak={mem:.0f}Mi peak_pod={peak_pod}")
        with out.open("a", newline="") as fh:
            csv.writer(fh).writerow([i, rate, round(achieved, 1), round(p95, 1), cpu, notes])
        print(f"[{run_id}] offered={rate} 2xx={ok}/{sent} ({pct:.1f}%) p95={p95:.1f}ms cpu={cpu}m rc={rc}", flush=True)
        json.dump({"step": i, "offered": rate, "k": k, "sent": sent, "2xx": ok, "4xx": c4,
                   "5xx": c5, "p95": p95, "hist": hist, "cpu": cpu, "mem": mem,
                   "loadgens": lgs},
                  open(RESULTS / run_id / "bench-summary.json", "w"), indent=1, default=str)
        if pct < 95 or c5 > 0:
            print(f"  -> failure mode at {rate} rps; stopping ladder", flush=True)
            break
        time.sleep(20)


def kafka_lag(group, topic, parts):
    from kafka import KafkaConsumer, TopicPartition
    c = KafkaConsumer(bootstrap_servers=KAFKA, request_timeout_ms=15000)
    tps = [TopicPartition(topic, p) for p in range(parts)]
    end = c.end_offsets(tps)
    committed = c.committed(tps)
    c.close()
    lag = sum(max(0, (end[tp] or 0) - (committed.get(tp) or 0)) for tp in tps)
    return lag, sum(end.values())


def s6_ladder():
    steps = [(500, 1), (1000, 1), (2000, 2), (4000, 4), (6000, 6)]
    out = ROOT / "csv" / "saturation-s6.csv"
    with out.open("w", newline="") as fh:
        csv.writer(fh).writerow(["step", "offered_msg_s", "procs", "produced", "consumed",
                                 "consumer_recs_s", "lag_at_stop", "worker_cpu_peak_m", "notes"])
    for i, (rate, k) in enumerate(steps, 1):
        per = rate // k
        dur = max(15, min(180, 90000 // rate))
        run_id = f"sat-s6-M-step{i}-{rate}mps"
        lines = ["sleep 5"]
        for j in range(1, k + 1):
            lines.append(
                f"{PY} {PERF}/generators/kafka_loadgen.py --brokers {KAFKA} "
                f"--topic perf-cdr --rate {per} --duration {dur} "
                f"--payload-file /tmp/opencode/perf-b/batches/flat/batch_000001.jsonl &")
        lines.append("wait")
        script = ROOT / f"lg-s6-step{i}.sh"
        script.write_text("\n".join(lines) + "\n")
        log_text, rc, rows = run_step("s6-kafka-local", "s6_kafka_local.yaml",
                                      run_id, f"bash {script}", 15, dur, dur + 30)
        lgs = parse_loadgens(log_text, "kafka_loadgen")
        produced = sum(l.get("sent", 0) for l in lgs)
        hist = summarize(rows)
        consumed = hist["records_in"]
        try:
            lag, _total = kafka_lag("s6-kafka-local", "perf-cdr", 3)
        except Exception as e:
            lag = f"err:{e}"
        cpu, mem, peak_pod = peaks(run_id)
        consumed_rps = consumed / dur if dur else 0
        notes = (f"produced={produced} statuses={','.join(hist['statuses'])} "
                 f"records_out={hist['records_out']} skipped={hist['records_skipped']} "
                 f"nodes={','.join(hist['nodes'])} peak_pod={peak_pod}")
        with out.open("a", newline="") as fh:
            csv.writer(fh).writerow([i, rate, k, produced, consumed,
                                     round(consumed_rps, 1), lag, cpu, notes])
        print(f"[{run_id}] produced={produced} consumed={consumed} lag={lag} cpu={cpu}m rc={rc}", flush=True)
        json.dump({"step": i, "offered": rate, "k": k, "produced": produced,
                   "consumed": consumed, "lag": lag, "hist": hist, "cpu": cpu,
                   "loadgens": lgs},
                  open(RESULTS / run_id / "bench-summary.json", "w"), indent=1, default=str)
        if isinstance(lag, (int, float)) and lag > 5000:
            print(f"  -> lag growing at {rate} msg/s; stopping ladder", flush=True)
            break
        time.sleep(20)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which in ("s1", "both"):
        s1_ladder()
    if which in ("s6", "both"):
        s6_ladder()
