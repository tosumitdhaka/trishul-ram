#!/usr/bin/env python3
"""Kafka producer / consumer load generator for the Kafka bench scenarios.

One file, two modes:

    produce   --rate R --duration S --topic T --brokers B [--payload-file F]
              Produces records (one message per canonical CDR record from a
              JSONL payload file; cycles when the file is exhausted) at the
              offered rate for ``--duration`` seconds. Reports sent + failed.

    consume   --topic T --brokers B --duration S [--group-id G] [--lag]
              Consumes for ``--duration`` seconds and reports the number of
              records consumed and the end offsets / max lag per partition
              (``--lag`` does a final ``end_offsets`` round-trip).

Uses kafka-python (``pip install kafka-python``) — the exact client library
TRAM's own Kafka connectors use (see tram/connectors/kafka/*). It is not in
the base harness venv; Phase 1b installs it alongside the broker deployment.
``--check`` verifies client availability and exits.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    from kafka import KafkaConsumer, KafkaProducer  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised by --check and at runtime
    KafkaConsumer = None  # type: ignore[assignment,misc]
    KafkaProducer = None  # type: ignore[assignment,misc]


def _client_available() -> bool:
    return KafkaProducer is not None and KafkaConsumer is not None


def _load_payloads(path: str) -> list[bytes]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    payloads = [line.encode("utf-8") for line in lines if line.strip()]
    if not payloads:
        raise SystemExit(f"kafka_loadgen: no payload lines in {path}")
    for line in payloads:
        json.loads(line)  # fail fast on malformed JSONL
    return payloads


def _produce(brokers: list[str], topic: str, rate: float, duration: float, payloads: list[bytes]) -> dict:
    producer = KafkaProducer(bootstrap_servers=brokers, acks="all")
    total = int(rate * duration)
    sent = 0
    failed = 0
    start = time.monotonic()
    deadline = start + duration
    interval = 1.0 / rate
    next_send = start
    idx = 0
    try:
        while time.monotonic() < deadline and sent < total:
            now = time.monotonic()
            if now < next_send:
                time.sleep(min(next_send - now, 0.05))
                continue
            next_send += interval
            body = payloads[idx % len(payloads)]
            idx += 1
            try:
                producer.send(topic, value=body).get(timeout=10)
                sent += 1
            except Exception:
                failed += 1
    finally:
        producer.flush()
        producer.close()
    elapsed = max(time.monotonic() - start, 1e-9)
    return {
        "tool": "kafka_loadgen",
        "mode": "produce",
        "topic": topic,
        "sent": sent,
        "failed": failed,
        "achieved_rps": round(sent / elapsed, 2),
    }


def _consume(brokers: list[str], topic: str, duration: float, group_id: str, lag: bool) -> dict:
    consumer = KafkaConsumer(
        topic,
        bootstrap_servers=brokers,
        group_id=group_id,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        consumer_timeout_ms=2000,
    )
    consumed = 0
    last_positions: dict = {}
    start = time.monotonic()
    deadline = start + duration
    try:
        while time.monotonic() < deadline:
            batch = consumer.poll(timeout_ms=1000)
            for _tp, messages in batch.items():
                consumed += len(messages)
                last_positions[_tp] = messages[-1].offset + 1
    finally:
        consumer.close()
    result = {
        "tool": "kafka_loadgen",
        "mode": "consume",
        "topic": topic,
        "records_consumed": consumed,
        "duration_s": round(duration, 1),
    }
    if lag:
        lag_info = {}
        try:
            end_offsets = consumer.end_offsets(list(last_positions))
            for tp, end in end_offsets.items():
                lag_info[f"{tp.topic}:{tp.partition}"] = {
                    "end_offset": end,
                    "last_position": last_positions.get(tp, 0),
                    "lag": max(0, end - last_positions.get(tp, 0)),
                }
        except Exception:
            lag_info = {"error": "end_offsets round-trip failed (consumer closed)"}
        result["lag"] = lag_info
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Kafka producer/consumer load generator (kafka-python)."
    )
    parser.add_argument("--brokers", required=True, help="Comma-separated broker list, e.g. kafka:9092")
    parser.add_argument("--topic", required=True, help="Topic name")
    parser.add_argument("--consume", action="store_true", help="Consumer mode (default: producer)")
    parser.add_argument("--rate", type=float, default=100.0, help="Produce rate in msg/s (default 100)")
    parser.add_argument("--duration", type=float, default=60.0, help="Run duration in seconds (default 60)")
    parser.add_argument("--payload-file", default=None, help="JSONL payload file (producer mode)")
    parser.add_argument("--group-id", default="perf-loadgen", help="Consumer group id (default perf-loadgen)")
    parser.add_argument("--lag", action="store_true", help="Report end-offset lag (consumer mode)")
    parser.add_argument("--check", action="store_true", help="Verify kafka-python availability and exit")
    args = parser.parse_args()

    brokers = [b.strip() for b in args.brokers.split(",") if b.strip()]

    if args.check:
        if _client_available():
            print("kafka_loadgen: kafka-python available — client OK")
            return
        raise SystemExit(
            "kafka_loadgen: kafka-python is not installed — run "
            "pip install kafka-python (Phase 1b installs it with the broker deployment)"
        )

    if not _client_available():
        raise SystemExit(
            "kafka_loadgen: kafka-python is not installed — run "
            "pip install kafka-python (Phase 1b installs it with the broker deployment)"
        )

    if args.consume:
        result = _consume(brokers, args.topic, args.duration, args.group_id, args.lag)
    else:
        if not args.payload_file:
            parser.error("--payload-file is required in producer mode")
        payloads = _load_payloads(args.payload_file)
        result = _produce(brokers, args.topic, args.rate, args.duration, payloads)

    print(json.dumps(result), file=sys.stderr)


if __name__ == "__main__":
    main()