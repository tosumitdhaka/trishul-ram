#!/usr/bin/env python3
"""v1.6.0 wave-3 harness re-run — unified cell driver (mw + single).

Adapted from the 2026-10 capacity study drivers (perf-a2/perf-b/perf-c):
  - run_one.sh (harness COPY under /tmp/opencode/v160-rerun/perf) orchestrates
    register -> loadgen/batch -> stop -> collect -> delete
  - a steady collector samples kubectl top during the run window
  - run history filtered by rows not present before the rep
  - writes bench-summary.json + appends to csv/matrix-{a,b,c}-{topo}.csv

Usage: run_cell.py <mw|single> run <scenario> <profile> <rep>
       run_cell.py <mw|single> stage <scenario>
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REPO = Path("/home/dhaka/trishul/trishul-ram")
ROOT = Path("/tmp/opencode/v160-rerun")
PERF = ROOT / "perf"
RESULTS = ROOT / "results"
CSVDIR = ROOT / "csv"
TMP = ROOT / "steady"
PY = str(REPO / ".venv/bin/python")
NS = "trishul-ram"
API = "http://127.0.0.1:30001"
KAFKA_HOST = "172.19.0.2:30094"
CORPUS = Path("/tmp/opencode/perf-a2/corpus_100k.jsonl")
ABORT_S = 900

# scenario -> (template, pipeline, kind, stage_key)
SCEN: dict[str, tuple[str, str, str, str | None]] = {
    "s1": ("s1_webhook_local.yaml", "s1-webhook-local", "stream", None),
    "s2csv": ("s2_local_local_csv.yaml", "s2-local-local-csv", "batch", "csv"),
    "s2pmxml": ("s2_local_local_pmxml.yaml", "s2-local-local-pmxml", "batch", "pmxml"),
    "s3": ("s3_rest_rest.yaml", "s3-rest-rest", "batch", None),
    "s4": ("s4_snmp_local.yaml", "s4-snmp-local", "batch", None),
    "s5": ("s5_sftp_local.yaml", "s5-sftp-local", "batch", None),
    "s6": ("s6_kafka_local.yaml", "s6-kafka-local", "stream", None),
    "s7": ("s7_local_kafka.yaml", "s7-local-kafka", "batch", "json"),
    "fsweep_csv": ("fsweep_csv.yaml", "fsweep-csv", "batch", "csv"),
    "fsweep_json": ("fsweep_json.yaml", "fsweep-json", "batch", "json-rename"),
    "fsweep_ndjson": ("fsweep_ndjson.yaml", "fsweep-ndjson", "batch", "flat"),
    "fsweep_xml": ("fsweep_xml.yaml", "fsweep-xml", "batch", "xml"),
    "fsweep_pmxml": ("fsweep_pm_xml.yaml", "fsweep-pm-xml", "batch", "pmxml"),
    "fsweep_msgpack": ("fsweep_msgpack.yaml", "fsweep-msgpack", "batch", "formats/msgpack"),
    "fsweep_avro": ("fsweep_avro.yaml", "fsweep-avro", "batch", "formats/avro"),
    "fsweep_protobuf": ("fsweep_protobuf.yaml", "fsweep-protobuf", "batch", "formats/protobuf"),
    "fsweep_parquet": ("fsweep_parquet.yaml", "fsweep-parquet", "batch", "formats/parquet"),
    "t1": ("t1_project_filter.yaml", "t1-project-filter", "batch", "json"),
    "t2": ("t2_json_flatten_enrich.yaml", "t2-json-flatten-enrich", "batch", "nested-json"),
    "t3": ("t3_deduplicate.yaml", "t3-deduplicate", "batch", "json"),
    "t4": ("t4_counter_delta_window.yaml", "t4-counter-delta-window", "batch", "json"),
    "t5": ("t5_chain.yaml", "t5-chain", "batch", "json"),
}

TRANSFORM_NAMES = {
    "t1": "project,filter", "t2": "json_flatten,enrich", "t3": "deduplicate",
    "t4": "counter_delta,window_aggregate", "t5": "rename,cast,add_field,filter,project",
}

# in-pod source dirs (restaged into /data/perf/in per run)
STAGE_DIRS = {
    "csv": ("/data/perf/src/csv", "*.csv", None),
    "pmxml": ("/data/perf/src/pmxml", "*.xml", None),
    "flat": ("/data/perf/src/flat", "*.jsonl", None),
    "json": ("/data/perf/src/json", "*.jsonl", None),
    "nested-json": ("/data/perf/src/nested-json", "*.jsonl", None),
    "xml": ("/data/perf/src/xml", "*.xml", None),
    "json-rename": ("/data/perf/src/json", "*.jsonl", "json"),
    "formats/msgpack": ("/data/perf/formats/msgpack", "*.msgpack", None),
    "formats/avro": ("/data/perf/formats/avro", "*.avro", None),
    "formats/protobuf": ("/data/perf/formats/protobuf", "*.pb", None),
    "formats/parquet": ("/data/perf/formats/parquet", "*.parquet", None),
}


def pods(topo: str) -> list[str]:
    return (["trishul-ram-worker-0", "trishul-ram-worker-1", "trishul-ram-worker-2"]
            if topo == "mw" else ["trishul-ram-0"])


def kexec(pod: str, cmd: str, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(["kubectl", "exec", "-n", NS, pod, "--", "sh", "-c", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def stage(topo: str, key: str) -> None:
    src, glob, rename = STAGE_DIRS[key]
    for pod in pods(topo):
        if rename:
            cmd = (f"mkdir -p /data/perf/in /data/perf/out && rm -f /data/perf/in/* /data/perf/out/* && "
                   f"for f in {src}/{glob}; do cp \"$f\" \"/data/perf/in/$(basename \"$f\" .jsonl).{rename}\"; done")
        else:
            cmd = (f"mkdir -p /data/perf/in /data/perf/out && rm -f /data/perf/in/* /data/perf/out/* && "
                   f"cp {src}/{glob} /data/perf/in/")
        rc, out = kexec(pod, cmd)
        if rc != 0:
            raise RuntimeError(f"staging failed on {pod}: {out}")
        rc, out = kexec(pod, "ls /data/perf/in | wc -l")
        if out.strip() != "10":
            raise RuntimeError(f"staging failed on {pod}: {out} files (want 10)")


def clear_out(topo: str) -> None:
    for pod in pods(topo):
        kexec(pod, "rm -f /data/perf/out/* 2>/dev/null; true")


def sink_export(topo: str) -> dict:
    out = {}
    for pod in pods(topo):
        rc, o = kexec(pod, "find /data/perf/out -type f -printf '%s\\n' 2>/dev/null"
                      " | awk '{n++; s+=$1} END {print n+0, s+0}'")
        try:
            n, s = o.split()
            out[pod] = {"files": int(n), "bytes": int(s)}
        except Exception:
            out[pod] = {"files": 0, "bytes": 0, "raw": o}
    return out


def kafka_end_offsets(topic: str) -> str:
    code = (
        "from kafka import KafkaConsumer\n"
        "from kafka.structs import TopicPartition\n"
        f"c = KafkaConsumer(bootstrap_servers='{KAFKA_HOST}', request_timeout_ms=15000)\n"
        f"tps = [TopicPartition('{topic}', p) for p in sorted(c.partitions_for_topic('{topic}') or [0])]\n"
        "print(sum(c.end_offsets(tps).values()))\n"
    )
    p = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=60)
    return (p.stdout + p.stderr).strip()[:120]


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


def peaks_from_samples(samples_csv: Path) -> dict:
    peak_cpu: dict[str, int] = {}
    peak_mem: dict[str, int] = {}
    with samples_csv.open() as f:
        for row in csv.DictReader(f):
            pod = row["pod"]
            peak_cpu[pod] = max(peak_cpu.get(pod, 0), int(row["cpu"].rstrip("m") or 0))
            peak_mem[pod] = max(peak_mem.get(pod, 0), int(row["memory"].rstrip("Mi") or 0))
    if not peak_cpu:
        return {"cpu_peak_m": 0.0, "mem_peak_mi": 0.0, "peak_pod": ""}
    return {"cpu_peak_m": float(max(peak_cpu.values())), "mem_peak_mi": float(max(peak_mem.values())),
            "peak_pod": max(peak_cpu, key=peak_cpu.get)}


def parse_loadgen(log_text: str) -> list[dict]:
    out = []
    for line in log_text.splitlines():
        line = line.strip()
        if line.startswith('{"tool":'):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def mock_stats(reset: bool = False) -> dict:
    url = f"http://127.0.0.1:18080/collect/stats" + ("?reset=1" if reset else "")
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}


def loadgen_for(topo: str, scenario: str, profile: str) -> str | None:
    if scenario == "s1":
        rate = {"L": 100, "M": 300, "H": 800}[profile]
        port = 30002 if topo == "mw" else 30001
        return (f"{PY} {PERF}/generators/loadgen_webhook.py "
                f"--url http://127.0.0.1:{port}/webhooks/ingest "
                f"--rate {rate} --concurrency 20 --duration 180 "
                f"--payload-file {CORPUS}")
    if scenario == "s6":
        rate = {"L": 500, "M": 2000, "H": 2000}[profile]
        return (f"{PY} {PERF}/generators/kafka_loadgen.py "
                f"--brokers {KAFKA_HOST} --topic perf-cdr --rate {rate} "
                f"--duration 180 --payload-file {CORPUS}")
    return None


def run_cell(topo: str, scenario: str, profile: str, rep: int) -> int:
    template, pipeline, kind, stage_key = SCEN[scenario]
    if scenario.startswith("fsweep"):
        matrix = "b"
    elif scenario in TRANSFORM_NAMES or scenario in ("t1", "t2", "t3", "t4", "t5"):
        matrix = "c"
    else:
        matrix = "a"
    run_id = f"{topo}-{scenario}-{profile}-rep{rep}"
    res_values = ROOT / "values" / f"{topo}-{profile}.yaml"
    run_dir = RESULTS / run_id
    steady_dir = TMP / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    steady_dir.mkdir(parents=True, exist_ok=True)
    abort_s = 1500 if scenario == "s4" else ABORT_S

    clear_out(topo)
    if stage_key:
        stage(topo, stage_key)
    if scenario == "s3":
        mock_stats(reset=True)

    t0 = datetime.now(UTC)
    pre_ids = {r.get("run_id") for r in fetch_runs(pipeline)}
    print(f"[{run_id}] start {t0.isoformat()}", flush=True)

    loadgen = loadgen_for(topo, scenario, profile)
    batch_duration = {"s4": 1320, "s5": 480}.get(scenario, 180)
    args = ["env", f"TRAM_NAMESPACE={NS}", f"PERF_PYTHON={PY}",
            f"PERF_HELM_VALUES={res_values}",
            "bash", str(PERF / "run_one.sh"), str(PERF / "templates" / template), run_id,
            "--warmup", "60", "--duration", str(batch_duration)]
    if loadgen:
        args += ["--loadgen", loadgen]

    log_f = (run_dir / "run.log").open("w")
    proc = subprocess.Popen(args, stdout=log_f, stderr=subprocess.STDOUT, cwd=str(REPO))
    aborted = None
    steady = None
    try:
        steady_delay = 60 if kind == "stream" else 5
        time.sleep(steady_delay)
        steady_dur = 180 if kind == "stream" else 45
        steady = subprocess.Popen(
            [PY, str(PERF / "collector/collect.py"), "--run-id", run_id,
             "--duration", str(steady_dur), "--interval", "5", "--namespace", NS,
             "--api-url", API, "--pipeline", pipeline,
             "--helm-values", str(res_values),
             "--results-dir", str(steady_dir)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(REPO))
        deadline = time.monotonic() + (abort_s - steady_delay)
        while proc.poll() is None:
            if time.monotonic() > deadline:
                aborted = f"run_one exceeded {abort_s}s wall clock"
                proc.terminate()
                break
            time.sleep(2)
    finally:
        rc = proc.wait()
        log_f.close()
        if steady is not None:
            try:
                steady.wait(timeout=400)
            except subprocess.TimeoutExpired:
                steady.terminate()
    print(f"[{run_id}] run_one rc={rc} aborted={aborted}", flush=True)

    # move the harness copy's collector artifacts into the results dir
    har = PERF / "results" / run_id
    if har.is_dir():
        for f in har.iterdir():
            shutil.move(str(f), run_dir / f.name)
        har.rmdir()
    if (steady_dir / "samples.csv").exists():
        shutil.move(str(steady_dir / "samples.csv"), run_dir / "samples.csv")

    log_text = (run_dir / "run.log").read_text()
    rows = [r for r in fetch_runs(pipeline) if r.get("run_id") not in pre_ids]
    (run_dir / "run-rows-raw.json").write_text(json.dumps(rows, indent=1) + "\n")
    hist = summarize(rows)
    lgs = parse_loadgen(log_text)
    sinks = sink_export(topo) if scenario in ("s5", "s6") else {}
    rest = mock_stats() if scenario == "s3" else {}
    kout = kafka_end_offsets("perf-cdr-out") if scenario == "s7" else ""

    pk = peaks_from_samples(run_dir / "samples.csv") if (run_dir / "samples.csv").exists() \
        else {"cpu_peak_m": 0.0, "mem_peak_mi": 0.0, "peak_pod": ""}

    duration_s = 180.0 if kind == "stream" else round(hist["wall_s"], 1)
    throughput = round(hist["records_out"] / duration_s, 1) if duration_s > 0 else 0.0
    bytes_per_rec = round(hist["bytes_in"] / hist["records_in"], 1) if hist["records_in"] else 0.0

    metrics = {"records_in": hist["records_in"], "records_out": hist["records_out"],
               "errors": hist["errors"], "duration_s": duration_s,
               "throughput_recs_s": throughput}
    bench = {"run_id": run_id, "scenario": scenario, "res_profile": profile, "rep": rep,
             "topology": topo, "rep_started_at": t0.isoformat(), "run_one_rc": rc,
             "aborted": aborted, "run_history": hist, "loadgens": lgs,
             "rest_sink_stats": rest, "sink_export": sinks, "kafka_out_cum": kout,
             "bytes_per_record_in": bytes_per_rec, "peaks": pk, "metrics": metrics}
    (run_dir / "bench-summary.json").write_text(json.dumps(bench, indent=2) + "\n")

    parts = []
    if aborted:
        parts.append(f"ABORTED: {aborted}")
    parts.append(f"rows={hist['rows']} statuses={','.join(hist['statuses']) or 'none'} "
                 f"nodes={','.join(hist['nodes']) or 'none'}")
    if scenario in TRANSFORM_NAMES:
        parts.append(f"transforms={TRANSFORM_NAMES[scenario]}")
    if scenario.startswith("fsweep"):
        parts.append(f"bytes_in/rec={bytes_per_rec}")
    if hist["records_skipped"]:
        parts.append(f"records_skipped={hist['records_skipped']}")
    if hist["error_msgs"]:
        parts.append(f"errors={hist['error_msgs'][0][:140]}")
    if lgs:
        lg = lgs[-1]
        if "http_2xx" in lg:
            parts.append(f"lg: sent={lg.get('sent')} 2xx={lg.get('http_2xx')} 4xx={lg.get('http_4xx')} "
                         f"5xx={lg.get('http_5xx')} p50={lg.get('latency_ms', {}).get('p50')}ms "
                         f"p95={lg.get('latency_ms', {}).get('p95')}ms max={lg.get('latency_ms', {}).get('max')}ms; "
                         f"offered={lg.get('rate')}/s for {int(lg.get('duration_s', 0))}s")
        else:
            parts.append(f"lg: sent={lg.get('sent')} failed={lg.get('failed')} "
                         f"achieved_rps={lg.get('achieved_rps')}; offered={lg.get('rate')}/s for "
                         f"{int(lg.get('duration_s', 0))}s")
    if scenario == "s3" and rest.get("records") is not None:
        parts.append(f"rest sink stats: records={rest.get('records')} posts={rest.get('posts')}")
    if sinks:
        tf = sum(v["files"] for v in sinks.values())
        tb = sum(v["bytes"] for v in sinks.values())
        parts.append(f"sinkfiles={tf} sinkbytes={tb}")
    if scenario == "s7":
        parts.append(f"kafka sink end_offsets cumulative={kout}")
    if pk.get("peak_pod"):
        parts.append(f"peak_pod={pk['peak_pod']}")
    notes = "; ".join(parts)

    csv_path = CSVDIR / f"matrix-{matrix}-{topo}.csv"
    exists = csv_path.exists()
    with csv_path.open("a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["run_id", "scenario", "res_profile", "rep", "records_in", "records_out",
                        "errors", "duration_s", "throughput_recs_s", "pod_cpu_peak_m",
                        "pod_mem_peak_mi", "notes"])
        w.writerow([run_id, scenario, profile, rep, hist["records_in"], hist["records_out"],
                    hist["errors"], duration_s, throughput, int(pk["cpu_peak_m"]),
                    int(pk["mem_peak_mi"]), notes])
    print(f"[{run_id}] metrics={json.dumps(metrics)} peaks={pk}", flush=True)
    return 0 if not aborted else 1


def main() -> int:
    topo = sys.argv[1]
    assert topo in ("mw", "single")
    if sys.argv[2] == "run":
        return run_cell(topo, sys.argv[3], sys.argv[4], int(sys.argv[5]))
    if sys.argv[2] == "stage":
        stage(topo, sys.argv[3])
        print("staged")
        return 0
    print("usage: run_cell.py <mw|single> run <scenario> <profile> <rep>")
    return 2


if __name__ == "__main__":
    sys.exit(main())
