#!/usr/bin/env python3
"""asyncio HTTP POST flood generator for webhook-source bench scenarios.

Reads a JSONL payload file (one canonical CDR record per line, exactly as
``gen_corpus.py --format jsonl`` emits) and POSTs each record as an
independent request body to ``--url`` at a fixed ``--rate`` (requests/second)
across ``--concurrency`` in-flight workers for ``--duration`` seconds.

The rate limiter is global: at most ``rate`` send slots are granted per
second across all workers, so the offered load is independent of
concurrency (concurrency only bounds how many responses may be in flight).

Summary is written as a single JSON line to stderr and, when ``--summary``
is given, to a file:

    sent, http_2xx, http_4xx, http_5xx, errors,
    latency_ms {p50, p95, max},
    achieved_rps, inflight_peak, connection_limited

``connection_limited`` is True when the generator could not complete the
offered rate AND its in-flight connection pool ran at the ceiling — i.e. the
achieved plateau is loadgen-structural (either the server's latency consumed
the concurrency budget — server-bound — or the bench host could not cycle
connections fast enough — host-bound). Combine it with ``latency_ms`` to tell
the two apart: high latency ⇒ server-bound, low latency ⇒ host-bound. Raise
``--concurrency`` until ``connection_limited`` is False at the offered rate
for the headroom the ladder needs.

Uses httpx (already in the harness venv — TRAM's REST client).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

import httpx


class RateLimiter:
    """Fixed-rate async limiter — grants at most ``rate`` tokens per second."""

    def __init__(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.interval = 1.0 / rate
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next <= now:
                self._next = now + self.interval
                return
            delay = self._next - now
            self._next += self.interval
        await asyncio.sleep(delay)


class SendStats:
    def __init__(self) -> None:
        self.sent = 0
        self.http_2xx = 0
        self.http_4xx = 0
        self.http_5xx = 0
        self.errors = 0
        self.latencies_ms: list[float] = []
        self.inflight = 0
        self.inflight_peak = 0

    def record(self, status_code: int, latency_ms: float) -> None:
        self.sent += 1
        if 200 <= status_code < 300:
            self.http_2xx += 1
        elif 400 <= status_code < 500:
            self.http_4xx += 1
        elif status_code >= 500:
            self.http_5xx += 1
        else:
            self.http_4xx += 1
        self.latencies_ms.append(latency_ms)


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (works on Python 3.12 stdlib)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[k - 1]


def load_payloads(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    payloads = [line for line in lines if line.strip()]
    if not payloads:
        raise SystemExit(f"loadgen_webhook: no payload lines in {path}")
    for line in payloads:
        json.loads(line)  # fail fast on malformed JSONL
    return payloads


async def _worker(
    client: httpx.AsyncClient,
    url: str,
    payloads: list[str],
    limiter: RateLimiter,
    stats: SendStats,
    stop: asyncio.Event,
) -> None:
    n = len(payloads)
    idx = 0
    while not stop.is_set():
        await limiter.wait()
        if stop.is_set():
            return
        body = payloads[idx % n].encode("utf-8")
        idx += 1
        t0 = time.perf_counter()
        stats.inflight += 1
        stats.inflight_peak = max(stats.inflight_peak, stats.inflight)
        try:
            resp = await client.post(
                url,
                content=body,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError:
            stats.errors += 1
            continue
        finally:
            stats.inflight -= 1
        latency_ms = (time.perf_counter() - t0) * 1000.0
        stats.record(resp.status_code, latency_ms)


async def run_flood(url: str, rate: float, concurrency: int, duration: float, payloads: list[str]) -> SendStats:
    stats = SendStats()
    limiter = RateLimiter(rate)
    stop = asyncio.Event()
    timeout = httpx.Timeout(30.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=concurrency)) as client:
        workers = [
            asyncio.create_task(_worker(client, url, payloads, limiter, stats, stop))
            for _ in range(concurrency)
        ]
        try:
            await asyncio.sleep(duration)
        finally:
            stop.set()
            await asyncio.gather(*workers, return_exceptions=True)
    return stats


def summary(stats: SendStats, **extra) -> dict:
    lat = stats.latencies_ms
    return {
        "tool": "loadgen_webhook",
        "sent": stats.sent,
        "http_2xx": stats.http_2xx,
        "http_4xx": stats.http_4xx,
        "http_5xx": stats.http_5xx,
        "errors": stats.errors,
        "latency_ms": {
            "p50": round(_percentile(lat, 50), 3),
            "p95": round(_percentile(lat, 95), 3),
            "max": round(max(lat), 3) if lat else 0.0,
        },
        "inflight_peak": stats.inflight_peak,
        **extra,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="HTTP POST flood generator for webhook-source bench scenarios."
    )
    parser.add_argument("--url", required=True, help="Target URL (e.g. http://127.0.0.1:30001/webhooks/ingest)")
    parser.add_argument("--rate", type=float, required=True, help="Offered load in requests/second")
    parser.add_argument("--concurrency", type=int, default=10, help="In-flight workers (default 10)")
    parser.add_argument("--duration", type=float, required=True, help="Flood duration in seconds")
    parser.add_argument("--payload-file", required=True, help="JSONL file of record bodies (one per POST)")
    parser.add_argument("--summary", default=None, help="Optional summary JSON output file")
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.duration <= 0:
        parser.error("--duration must be > 0")

    payloads = load_payloads(args.payload_file)
    stats = asyncio.run(run_flood(args.url, args.rate, args.concurrency, args.duration, payloads))
    offered_total = args.rate * args.duration
    result = summary(
        stats,
        url=args.url,
        rate=args.rate,
        concurrency=args.concurrency,
        duration_s=args.duration,
        payload_records=len(payloads),
        achieved_rps=round(stats.sent / args.duration, 1) if args.duration > 0 else 0.0,
        # Loadgen-side saturation signal: could not complete the offered rate
        # while every in-flight connection slot was busy.
        connection_limited=(
            stats.sent < offered_total * 0.95 and stats.inflight_peak >= args.concurrency
        ),
    )
    print(json.dumps(result), file=sys.stderr)
    if args.summary:
        Path(args.summary).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()