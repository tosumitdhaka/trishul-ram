#!/usr/bin/env python3
"""Per-run metrics collector for the TRAM performance-benchmark harness.

For a finished (or running) bench run this gathers three artifacts under
``results/<run-id>/``:

* ``samples.csv``  — ``kubectl top pods -n <ns>`` sampled every ``--interval``
  seconds for ``--duration`` seconds (columns: ts, pod, cpu, memory).
* ``summary.json`` — TRAM run-history for ``--pipeline`` from the manager API
  (GET /api/runs, X-API-Key auth): records_in/out, bytes_in/out, run
  durations, error counts.
* ``meta.json``    — environment snapshot: helm values used, node
  allocatable CPU/memory, running image tags, TRAM version.

The manager API is reached through the NodePort service (default
http://127.0.0.1:30001, override with --api-url / $TRAM_API_URL). API-key
auth follows helm ``apiKey`` / ``TRAM_API_KEY`` (send --api-key or set
$TRAM_API_KEY; omitted when no key is configured).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

DEFAULT_API_URL = "http://127.0.0.1:30001"
DEFAULT_NS = "tram"
DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def run_kubectl(args: list[str], timeout: int = 20) -> str:
    """Run a kubectl command, returning stdout ('' on any failure)."""
    try:
        proc = subprocess.run(
            ["kubectl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"collect: kubectl unavailable/failed: {exc}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        print(f"collect: kubectl {' '.join(args)} rc={proc.returncode}: {proc.stderr.strip()}", file=sys.stderr)
        return ""
    return proc.stdout


def sample_top(namespace: str) -> list[dict]:
    """One kubectl top pods snapshot -> list of {pod, cpu, memory}."""
    out = run_kubectl(["top", "pods", "-n", namespace, "--no-headers"])
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            rows.append({"pod": parts[0], "cpu": parts[1], "memory": parts[2]})
    return rows


def node_allocatable() -> dict:
    """Parse 'Allocatable:' cpu/memory from kubectl describe nodes."""
    out = run_kubectl(["describe", "nodes"])
    alloc = {}
    in_block = False
    for line in out.splitlines():
        if line.strip().startswith("Allocatable:"):
            in_block = True
            continue
        if in_block:
            stripped = line.strip()
            if not stripped:
                break
            match = re.match(r"^(\w+):\s+(\S+)", stripped)
            if match and match.group(1) in ("cpu", "memory"):
                alloc[match.group(1)] = match.group(2)
    return alloc


def pod_images(namespace: str) -> dict[str, str]:
    """Running image refs per pod: {pod: image}."""
    out = run_kubectl(
        [
            "get",
            "pods",
            "-n",
            namespace,
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}{\" \"}{.status.containerStatuses[0].image}{\"\\n\"}{end}",
        ]
    )
    images = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            images[parts[0]] = parts[1]
    return images


def fetch_run_history(api_url: str, api_key: str | None, pipeline: str | None) -> list[dict]:
    """GET /api/runs (optionally filtered by pipeline) with API-key auth."""
    params = {"limit": 1000}
    if pipeline:
        params["pipeline"] = pipeline
    headers = {"X-API-Key": api_key} if api_key else {}
    try:
        resp = httpx.get(f"{api_url}/api/runs", params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        print(f"collect: run-history fetch failed: {exc}", file=sys.stderr)
        return []


def summarize_runs(runs: list[dict]) -> dict:
    """Aggregate run-history rows into the summary object."""
    completed = [r for r in runs if r.get("finished_at")]
    durations_ms = []
    for run in completed:
        try:
            start = datetime.fromisoformat(run["started_at"])
            finish = datetime.fromisoformat(run["finished_at"])
            durations_ms.append((finish - start).total_seconds() * 1000.0)
        except (TypeError, ValueError):
            continue
    return {
        "runs_total": len(runs),
        "runs_completed": len(completed),
        "records_in": sum(int(r.get("records_in") or 0) for r in completed),
        "records_out": sum(int(r.get("records_out") or 0) for r in completed),
        "records_skipped": sum(int(r.get("records_skipped") or 0) for r in completed),
        "bytes_in": sum(int(r.get("bytes_in") or 0) for r in completed),
        "bytes_out": sum(int(r.get("bytes_out") or 0) for r in completed),
        "dlq_count": sum(int(r.get("dlq_count") or 0) for r in completed),
        "run_duration_ms": {
            "count": len(durations_ms),
            "total_ms": round(sum(durations_ms), 3),
            "avg_ms": round(sum(durations_ms) / len(durations_ms), 3) if durations_ms else 0.0,
            "max_ms": round(max(durations_ms), 3) if durations_ms else 0.0,
        },
        "errors": [r.get("error") for r in completed if r.get("error")],
        "error_count": sum(1 for r in completed if r.get("error")),
    }


def helm_values_snapshot(helm_values_path: str | None) -> dict:
    """Snapshot of the helm values used for this deployment (may be partial)."""
    if not helm_values_path:
        return {"source": None, "note": "no --helm-values provided"}
    try:
        with Path(helm_values_path).open(encoding="utf-8") as handle:
            values = yaml.safe_load(handle) or {}
    except OSError as exc:
        return {"source": helm_values_path, "error": str(exc)}

    def _image(value: dict) -> dict:
        return {
            "repository": value.get("repository"),
            "tag": value.get("tag"),
        }

    image = values.get("image") or {}
    manager = values.get("manager") or {}
    worker = values.get("worker") or {}
    return {
        "source": helm_values_path,
        "image": _image(image),
        "manager": {
            "enabled": manager.get("enabled"),
            "image": _image(manager.get("image") or {}),
            "replicas": 1,
            "resources": (manager.get("resources") or {}).get("requests"),
        },
        "worker": {
            "replicas": worker.get("replicas"),
            "image": _image(worker.get("image") or {}),
            "resources": (worker.get("resources") or {}).get("requests"),
        },
        "service": {
            "type": (values.get("service") or {}).get("type"),
            "nodePort": (values.get("service") or {}).get("nodePort"),
        },
        "env_snmp_stack": (values.get("env") or {}).get("TRAM_SNMP_STACK"),
    }


def tram_version(api_url: str, repo_root: Path) -> str:
    """TRAM version: from pyproject.toml (repo source of truth for v1.5.1)."""
    pyproject = repo_root / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if match:
            return match.group(1)
    except OSError:
        pass
    try:
        resp = httpx.get(f"{api_url}/api/ready", timeout=10)
        if resp.status_code == 200:
            return str(resp.json().get("version", "unknown"))
    except httpx.HTTPError:
        pass
    return "unknown"


def sample_loop(run_dir: Path, namespace: str, duration: float, interval: float) -> None:
    """Sample kubectl top pods at fixed intervals into samples.csv."""
    samples_path = run_dir / "samples.csv"
    wrote_header = False
    deadline = time.monotonic() + duration if duration > 0 else None

    def _stop(_sig, _frame) -> None:  # pragma: no cover - interactive path
        raise SystemExit(0)

    if duration <= 0:
        signal.signal(signal.SIGINT, _stop)

    with samples_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        while deadline is None or time.monotonic() < deadline:
            ts = _now_iso()
            rows = sample_top(namespace)
            if not wrote_header:
                writer.writerow(["ts", "pod", "cpu", "memory"])
                wrote_header = True
            for row in rows:
                writer.writerow([ts, row["pod"], row["cpu"], row["memory"]])
            handle.flush()
            time.sleep(interval)
    print(f"collect: samples written to {samples_path}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect kubectl top + TRAM run-history metrics for one bench run."
    )
    parser.add_argument("--run-id", required=True, help="Bench run identifier (result dir name)")
    parser.add_argument("--duration", type=float, default=0.0, help="Sampling duration in s (0 = until Ctrl-C)")
    parser.add_argument("--interval", type=float, default=5.0, help="Sampling interval in s (default 5)")
    parser.add_argument("--namespace", default=DEFAULT_NS, help=f"Pod namespace (default {DEFAULT_NS})")
    parser.add_argument("--api-url", default=None, help="Manager API base URL (default $TRAM_API_URL or :30001)")
    parser.add_argument("--api-key", default=None, help="X-API-Key for the manager API (default $TRAM_API_KEY)")
    parser.add_argument("--pipeline", default=None, help="Filter run history to this pipeline name")
    parser.add_argument("--helm-values", default=None, help="helm values.yaml used for the deployment")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR), help="Results root dir")
    args = parser.parse_args()

    if args.interval <= 0:
        parser.error("--interval must be > 0")

    api_url = args.api_url or os.environ.get("TRAM_API_URL", DEFAULT_API_URL)
    api_key = args.api_key if args.api_key is not None else os.environ.get("TRAM_API_KEY")

    run_dir = Path(args.results_dir) / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    started_at = _now_iso()

    sample_loop(run_dir, args.namespace, args.duration, args.interval)

    runs = fetch_run_history(api_url, api_key, args.pipeline)
    summary = summarize_runs(runs)
    summary.update(
        {
            "run_id": args.run_id,
            "pipeline": args.pipeline,
            "api_url": api_url,
            "collected_at": _now_iso(),
        }
    )
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    meta = {
        "run_id": args.run_id,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "namespace": args.namespace,
        "sampling": {"duration_s": args.duration, "interval_s": args.interval},
        "env": {
            "tram_version": tram_version(api_url, repo_root),
            "helm_values": helm_values_snapshot(args.helm_values),
            "node_allocatable": node_allocatable(),
            "running_images": pod_images(args.namespace),
        },
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    print(f"collect: summary -> {run_dir / 'summary.json'}", file=sys.stderr)
    print(f"collect: meta    -> {run_dir / 'meta.json'}", file=sys.stderr)


if __name__ == "__main__":
    main()