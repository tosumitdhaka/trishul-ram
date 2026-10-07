#!/usr/bin/env python3
"""Canonical S1 (webhook) saturation ladder for the TRAM perf harness.

Runs the s1 webhook scenario (template ``s1_webhook_local.yaml``) against the
currently-deployed cluster over an offered-load ladder and records, per step:
achieved 2xx rps, p95 latency, peak pod CPU/mem with the peak pod, and
loadgen-side saturation signals.

Three fixes carried from the v1.6.0 re-run defects
(``results/v1.6.0-rerun/RESULTS.md`` §5):

  A. bench-hygiene: asserts a clean scheduler (no enabled non-bench pipeline)
     before step 1 — run_one.sh re-asserts per step. A leftover enabled
     pipeline fires mid-measurement and steals worker CPU (the #86 lesson).
  B. ladder telemetry: the re-run's ladder rows lost pod CPU/mem/peak_pod
     because their steady collector was launched with
     ``--results-dir <root>/<run_id>`` *and* ``--run-id <run_id>``, so
     collect.py wrote ``samples.csv`` one directory DEEPER than the ladder's
     peaks() looked (``<root>/<run_id>/<run_id>/samples.csv``). Here the
     collector gets ``--results-dir <root>/steady`` only, so samples.csv lands
     at ``<root>/steady/<run_id>/samples.csv`` and peaks() reads exactly that
     path — the steady-window telemetry (the run_one.sh internal collector
     samples *after* the pipeline stops, not the measurement window).
  C. loadgen ceiling: the re-run loadgen saturated at ~1,600 rps because each
     process ran ``--concurrency 50``, capping achieved rps at
     ``concurrency / server-latency`` (50 / ~0.3 s ≈ 167 rps/process) far
     below the configured per-process rate. Here each process runs
     ``--concurrency`` from ``PERF_LG_CONCURRENCY`` (default 400), which
     clears the connection ceiling at every offered step; the loadgen now also
     reports ``achieved_rps``/``connection_limited`` so a loadgen-bound
     plateau is distinguishable from a server-bound one.

Deployment is out of band: the cluster must already be at the target
topology/profile (this ladder does not helm-install). The steady collector and
the loadgens all run on the bench host.

Usage:
    scripts/perf/ladder_s1.py [--topology mw|single] [--results-root DIR]
        [--values PATH] [--corpus PATH] [--step-timeout S] [--max-step N]

Env (harness conventions — README §2):
    TRAM_API_URL, TRAM_API_KEY, TRAM_NAMESPACE, PERF_PYTHON,
    PERF_LG_CONCURRENCY (per-process loadgen concurrency, default 400),
    PERF_LADDER_ROOT (default results root), TRAM_PERF_ALLOW_DIRTY (hygiene
    bypass).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PERF = Path(__file__).resolve().parent
REPO = PERF.parent.parent
PY = os.environ.get("PERF_PYTHON", str(REPO / ".venv" / "bin" / "python"))
NS = os.environ.get("TRAM_NAMESPACE", "trishul-ram")
API = os.environ.get("TRAM_API_URL", "http://127.0.0.1:30001")
LG_CONCURRENCY = int(os.environ.get("PERF_LG_CONCURRENCY", "400"))

# (offered rps, parallel loadgen processes) per ladder step.
STEPS_MW: list[tuple[int, int]] = [
    (100, 1), (200, 1), (400, 1), (800, 2),
    (1600, 3), (3200, 6), (6400, 10),
]
STEPS_SINGLE: list[tuple[int, int]] = [
    (100, 1), (200, 1), (400, 1), (800, 2),
    (1600, 3), (3200, 6),
]
PORT = {"mw": 30002, "single": 30001}
DEFAULT_VALUES = {
    "mw": str(PERF / "infra" / "values-mgrworker.yaml"),
    "single": str(PERF / "infra" / "values-single.yaml"),
}


def api_json(path: str) -> list[dict]:
    req = urllib.request.Request(API + path)
    key = os.environ.get("TRAM_API_KEY")
    if key:
        req.add_header("X-API-Key", key)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_runs(pipeline: str) -> list[dict]:
    try:
        return api_json(f"/api/runs?pipeline={pipeline}&limit=1000")
    except Exception:
        return []


def summarize(rows: list[dict]) -> dict:
    """Aggregate run-history rows for the ladder step (mirrors run_cell.py)."""
    finished = [r for r in rows if r.get("finished_at")]
    err_msgs = [r.get("error") for r in finished if r.get("error")]
    for r in finished:
        err_msgs.extend((r.get("errors") or [])[:3])
    return {
        "rows": len(rows),
        "records_in": sum(int(r.get("records_in") or 0) for r in finished),
        "records_out": sum(int(r.get("records_out") or 0) for r in finished),
        "records_skipped": sum(int(r.get("records_skipped") or 0) for r in finished),
        "errors": len(err_msgs),
        "statuses": sorted({r.get("status") for r in finished if r.get("status")}),
        "nodes": sorted({r.get("node") for r in finished if r.get("node")}),
    }


def _parse_cpu(value: str) -> float:
    value = value.strip()
    if value.endswith("m"):
        return float(value[:-1])
    return float(value) * 1000.0  # bare cores -> milli-cores


def _parse_mem(value: str) -> float:
    value = value.strip()
    if value.endswith("Gi"):
        return float(value[:-2]) * 1024
    if value.endswith("Mi"):
        return float(value[:-2])
    if value.endswith("Ki"):
        return float(value[:-2]) / 1024
    return float(value or 0)


def peaks(samples_csv: Path, topology: str) -> tuple[float, float, str]:
    """Peak pod CPU (m), peak pod mem (Mi), peak pod name over TRAM pods only.

    The webhook stream runs on the workers (mw) or the standalone pod
    (single); kafka-0/sftp-0 sidecars are not the bench subject.
    """
    if not samples_csv.exists():
        return 0.0, 0.0, ""
    cpu: dict[str, float] = {}
    mem: dict[str, float] = {}
    with samples_csv.open() as fh:
        for row in csv.DictReader(fh):
            pod = row["pod"]
            cpu[pod] = max(cpu.get(pod, 0.0), _parse_cpu(row["cpu"]))
            mem[pod] = max(mem.get(pod, 0.0), _parse_mem(row["memory"]))
    if topology == "mw":
        tram = {p: v for p, v in cpu.items() if "worker" in p}
    else:
        tram = {p: v for p, v in cpu.items() if p.startswith(f"{NS}-") and "manager" not in p}
    if not tram:
        tram = cpu
    peak = max(tram.values()) if tram else 0.0
    peak_pod = max(tram, key=tram.get) if tram else ""
    return peak, (max(mem.values()) if mem else 0.0), peak_pod


def parse_loadgens(log_text: str, tool: str) -> list[dict]:
    out = []
    for line in log_text.splitlines():
        line = line.strip()
        if line.startswith('{"tool":') and f'"{tool}"' in line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def run_step(
    pipeline: str,
    template: str,
    run_id: str,
    lg_bash: str,
    warmup: int,
    duration: int,
    collect_dur: int,
    values: str,
    root: Path,
    step_timeout: float,
) -> tuple[str, int | str, list[dict]]:
    """One ladder step: run_one.sh (register→loadgen→stop→collect→delete) with
    a parallel steady collector sampling kubectl top during the run window."""
    pre_ids = {r.get("run_id") for r in fetch_runs(pipeline)}
    log_path = root / "results" / run_id / "run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "env",
        f"TRAM_NAMESPACE={NS}",
        f"PERF_PYTHON={PY}",
        f"PERF_HELM_VALUES={values}",
        f"PERF_RESULTS_DIR={root / 'runone'}",
        "bash", str(PERF / "run_one.sh"), str(PERF / "templates" / template), run_id,
        "--warmup", str(warmup), "--duration", str(duration), "--loadgen", lg_bash,
    ]
    with log_path.open("w") as lf:
        proc = subprocess.Popen(cmd, cwd=str(REPO), stdout=lf, stderr=lf)
        start = time.monotonic()
        time.sleep(warmup if warmup >= 30 else 5)
        # Fix B: --results-dir is the steady ROOT; collect.py appends run_id,
        # so samples.csv lands at <root>/steady/<run_id>/samples.csv — the
        # exact path peaks() reads.
        steady = subprocess.Popen(
            [PY, str(PERF / "collector" / "collect.py"), "--run-id", run_id,
             "--duration", str(collect_dur), "--interval", "5", "--namespace", NS,
             "--api-url", API, "--pipeline", pipeline,
             "--helm-values", str(values),
             "--results-dir", str(root / "steady")],
            cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        rc: int | str = 0
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if time.monotonic() - start > step_timeout:
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


def s1_ladder(
    topology: str,
    root: Path,
    values: str,
    corpus: Path,
    step_timeout: float,
    max_step: int | None,
) -> None:
    if not corpus.exists():
        print(f"ladder_s1: generating corpus at {corpus} (seed 42, n=100000)", flush=True)
        subprocess.run(
            [PY, str(PERF / "generators" / "gen_corpus.py"),
             "--seed", "42", "--n", "100000", "--format", "jsonl", "--out", str(corpus)],
            check=True,
        )

    url = f"http://127.0.0.1:{PORT[topology]}/webhooks/ingest"
    steps = STEPS_MW if topology == "mw" else STEPS_SINGLE
    csv_name = "saturation-s1.csv" if topology == "mw" else "saturation-s1-single.csv"
    out = root / csv_name
    steady_root = root / "steady"
    (root / "results").mkdir(parents=True, exist_ok=True)
    steady_root.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        csv.writer(fh).writerow(
            ["step", "offered_rps", "achieved_2xx_rps", "p95_ms",
             "pod_cpu_peak_m", "pod_mem_peak_mi", "peak_pod", "notes"]
        )

    for i, (rate, k) in enumerate(steps, 1):
        if max_step and i > max_step:
            break
        per = rate // k
        run_id = f"sat-s1-{topology}-M-step{i}-{rate}rps"
        lines = ["sleep 5"]
        for j in range(1, k + 1):
            lines.append(
                f"{PY} {PERF}/generators/loadgen_webhook.py "
                f"--url {url} --rate {per} --concurrency {LG_CONCURRENCY} "
                f"--duration 180 --payload-file {corpus} "
                f"--summary {root}/lg-{run_id}-{j}.json &"
            )
        lines.append("wait")
        script = root / f"lg-s1-step{i}.sh"
        script.write_text("\n".join(lines) + "\n")

        log_text, rc, rows = run_step(
            "s1-webhook-local", "s1_webhook_local.yaml", run_id,
            f"bash {script}", 60, 120, 150, values, root, step_timeout,
        )
        lgs = parse_loadgens(log_text, "loadgen_webhook")
        sent = sum(lg.get("sent", 0) for lg in lgs)
        ok = sum(lg.get("http_2xx", 0) for lg in lgs)
        c4 = sum(lg.get("http_4xx", 0) for lg in lgs)
        c5 = sum(lg.get("http_5xx", 0) for lg in lgs)
        err = sum(lg.get("errors", 0) for lg in lgs)
        p95s = [lg.get("latency_ms", {}).get("p95") for lg in lgs if lg.get("latency_ms")]
        p95 = sum(p95s) / len(p95s) if p95s else 0.0
        hist = summarize(rows)
        cpu, mem, peak_pod = peaks(steady_root / run_id / "samples.csv", topology)
        achieved = ok / 180
        pct = 100.0 * ok / sent if sent else 0.0
        lg_capped = any(lg.get("connection_limited") for lg in lgs)
        lg_achieved = sum(lg.get("achieved_rps", 0) for lg in lgs)
        notes = (
            f"k={k} sent={sent} 2xx={ok} 4xx={c4} 5xx={c5} errors={err} "
            f"records_in={hist['records_in']} records_out={hist['records_out']} "
            f"skipped={hist['records_skipped']} nodes={','.join(hist['nodes'])} "
            f"loadgen_achieved={lg_achieved:.0f}/s connection_limited={lg_capped}"
        )
        with out.open("a", newline="") as fh:
            csv.writer(fh).writerow(
                [i, rate, round(achieved, 1), round(p95, 1), cpu, mem, peak_pod, notes]
            )
        print(
            f"[{run_id}] offered={rate} achieved={achieved:.1f} 2xx={ok}/{sent} "
            f"({pct:.1f}%) p95={p95:.1f}ms cpu={cpu}m mem={mem}Mi peak_pod={peak_pod} "
            f"lg_capped={lg_capped} rc={rc}",
            flush=True,
        )
        summary_dir = root / "results" / run_id
        summary_dir.mkdir(parents=True, exist_ok=True)
        json.dump(
            {"step": i, "offered": rate, "k": k, "sent": sent, "2xx": ok, "4xx": c4,
             "5xx": c5, "errors": err, "p95": p95, "hist": hist, "cpu": cpu,
             "mem": mem, "peak_pod": peak_pod, "loadgen_achieved": lg_achieved,
             "loadgen_connection_limited": lg_capped, "loadgens": lgs},
            open(summary_dir / "bench-summary.json", "w"),
            indent=1, default=str,
        )
        if pct < 95 or c5 > 0 or (err and err / sent > 0.05):
            print(f"  -> failure mode at {rate} rps; stopping ladder", flush=True)
            break
        time.sleep(20)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--topology", choices=sorted(PORT), default="mw",
                        help="Deployed topology (default mw)")
    parser.add_argument("--results-root", default=None,
                        help="Scratch root for CSVs/scripts/samples (default "
                             "$PERF_LADDER_ROOT or scripts/perf/results/ladder-run)")
    parser.add_argument("--values", default=None,
                        help="helm values file for the collector meta (default infra/values-<topo>.yaml)")
    parser.add_argument("--corpus", default=None,
                        help="JSONL corpus; generated at <results-root>/corpus_100k.jsonl if absent")
    parser.add_argument("--step-timeout", type=float, default=900.0,
                        help="Per-step run_one.sh wall-clock budget (default 900)")
    parser.add_argument("--max-step", type=int, default=None,
                        help="Stop after this ladder step (dry-run of the first step)")
    args = parser.parse_args()

    # Fix A: fail fast before creating scratch dirs or generating any step
    # scripts (run_one.sh re-asserts per step).
    if os.environ.get("TRAM_PERF_ALLOW_DIRTY") != "1":
        chk = subprocess.run([str(PERF / "check_hygiene.sh")], cwd=str(REPO))
        if chk.returncode != 0:
            print("ladder_s1: hygiene check failed — refusing to start the ladder", flush=True)
            return 2

    root = Path(
        args.results_root
        or os.environ.get("PERF_LADDER_ROOT")
        or PERF / "results" / "ladder-run"
    )
    root.mkdir(parents=True, exist_ok=True)
    values = args.values or DEFAULT_VALUES[args.topology]
    corpus = Path(args.corpus or root / "corpus_100k.jsonl")

    s1_ladder(args.topology, root, values, corpus, args.step_timeout, args.max_step)
    return 0


if __name__ == "__main__":
    sys.exit(main())