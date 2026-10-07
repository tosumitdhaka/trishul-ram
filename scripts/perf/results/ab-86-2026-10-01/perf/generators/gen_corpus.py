#!/usr/bin/env python3
"""Canonical CDR corpus generator for the TRAM performance-benchmark harness.

Emits the study-wide canonical telecom CDR record (flat, 20 fields, ~500B as
compact JSON) as JSONL / NDJSON / CSV / XML-lines to stdout or a file.

Deterministic for a given ``--seed`` / ``--n`` pair: every random draw goes
through one ``random.Random(seed)`` instance, so two runs with the same seed
and record count produce byte-identical output regardless of wall-clock time.

``--nested`` wraps the session fields (session_start / session_end /
duration_s / bytes_up / bytes_down) under ``session_info`` for the
json_flatten / unnest transform benches. Nested output is only meaningful for
the JSON-family and XML formats; CSV is always emitted flat.

Record schema (study-wide canonical, shared across every generator):

    record_id     uuid str
    timestamp     ISO8601 str
    msisdn        10-digit str
    imsi          15-digit str
    imei          15-digit str
    cell_id       int
    event_type    VOICE | SMS | DATA | ROAMING | EVENT
    direction     MO | MT | FWD
    duration_s    int 0-3600
    bytes_up      int
    bytes_down    int
    rat           2G | 3G | 4G | 5G | NR_SA
    roaming       bool
    charge_amount float (2dp)
    currency      "JPY"
    session_start ISO8601 str
    session_end   ISO8601 str
    apn           str
    sgsn_addr     IPv4 str
    ggsn_addr     IPv4 str
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

# ── Canonical schema ───────────────────────────────────────────────────────

FIELD_NAMES: list[str] = [
    "record_id",
    "timestamp",
    "msisdn",
    "imsi",
    "imei",
    "cell_id",
    "event_type",
    "direction",
    "duration_s",
    "bytes_up",
    "bytes_down",
    "rat",
    "roaming",
    "charge_amount",
    "currency",
    "session_start",
    "session_end",
    "apn",
    "sgsn_addr",
    "ggsn_addr",
]

NESTED_FIELDS: list[str] = [
    "session_start",
    "session_end",
    "duration_s",
    "bytes_up",
    "bytes_down",
]

EVENT_TYPES = ("VOICE", "SMS", "DATA", "ROAMING", "EVENT")
DIRECTIONS = ("MO", "MT", "FWD")
RATS = ("2G", "3G", "4G", "5G", "NR_SA")
APNS = ("internet", "ims", "mms", "enterprise.vpn", "nidd", "smartphone")

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ipv4(rng: random.Random) -> str:
    return ".".join(str(rng.randint(0, 255)) for _ in range(4))


def make_record(rng: random.Random, index: int, nested: bool = False) -> dict:
    """Build one canonical CDR record from a seeded RNG."""
    event_type = rng.choice(EVENT_TYPES)
    session_start = _EPOCH + timedelta(seconds=rng.randint(0, 20_000_000), microseconds=rng.randint(0, 999_999))
    duration_s = rng.randint(0, 3600)
    session_end = session_start + timedelta(seconds=duration_s)

    record: dict = {
        "record_id": str(uuid.UUID(int=rng.getrandbits(128))),
        "timestamp": _iso(session_start),
        "msisdn": f"{rng.randint(1, 9)}{rng.randint(0, 10**9 - 1):09d}",
        "imsi": f"{rng.randint(0, 10**15 - 1):015d}",
        "imei": f"{rng.randint(0, 10**15 - 1):015d}",
        "cell_id": rng.randint(0, 65_535),
        "event_type": event_type,
        "direction": rng.choice(DIRECTIONS),
        "duration_s": duration_s,
        "bytes_up": rng.randint(0, 2_000_000),
        "bytes_down": rng.randint(0, 8_000_000),
        "rat": rng.choice(RATS),
        "roaming": rng.random() < 0.12,
        "charge_amount": round(rng.uniform(0.0, 1000.0), 2),
        "currency": "JPY",
        "session_start": _iso(session_start),
        "session_end": _iso(session_end),
        "apn": rng.choice(APNS),
        "sgsn_addr": _ipv4(rng),
        "ggsn_addr": _ipv4(rng),
    }
    if nested:
        session_info = {field: record.pop(field) for field in NESTED_FIELDS}
        record["session_info"] = session_info
    return record


def generate(seed: int, n: int, nested: bool = False) -> list[dict]:
    """Generate ``n`` deterministic records (the shared corpus primitive)."""
    rng = random.Random(seed)
    return [make_record(rng, i, nested=nested) for i in range(n)]


def _record_to_xml_element(record: dict, element: ET.Element) -> ET.Element:
    for key, value in record.items():
        if isinstance(value, dict):
            child = ET.SubElement(element, key)
            for sub_key, sub_value in value.items():
                ET.SubElement(child, sub_key).text = str(sub_value)
        else:
            ET.SubElement(element, key).text = str(value)
    return element


def write_jsonl(records: list[dict], out) -> None:
    for record in records:
        out.write(json.dumps(record, separators=(",", ":")))
        out.write("\n")


def write_csv(records: list[dict], out) -> None:
    writer = csv.DictWriter(out, fieldnames=FIELD_NAMES, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        writer.writerow(record)


def write_xml(records: list[dict], out) -> None:
    root = ET.Element("records")
    for record in records:
        element = ET.SubElement(root, "record")
        _record_to_xml_element(record, element)
    out.write(ET.tostring(root, encoding="unicode"))


_FORMAT_WRITERS = {
    "jsonl": write_jsonl,
    "ndjson": write_jsonl,
    "csv": write_csv,
    "xml": write_xml,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the canonical CDR corpus (deterministic per seed+n)."
    )
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (default 42)")
    parser.add_argument("--n", type=int, default=1000, help="Number of records (default 1000)")
    parser.add_argument("--nested", action="store_true", help="Wrap session fields under session_info")
    parser.add_argument(
        "--format",
        choices=sorted(_FORMAT_WRITERS),
        default="jsonl",
        help="Output format (default jsonl)",
    )
    parser.add_argument("--out", default=None, help="Output file path (default stdout)")
    args = parser.parse_args()

    if args.n < 0:
        parser.error("--n must be >= 0")

    records = generate(args.seed, args.n, nested=args.nested)

    if args.format == "csv" and args.nested:
        print(
            "gen_corpus: warning: --nested is a JSON/XML-family option; CSV is emitted flat",
            file=sys.stderr,
        )

    out = open(args.out, "w", encoding="utf-8", newline="") if args.out else sys.stdout
    try:
        _FORMAT_WRITERS[args.format](records, out)
    finally:
        if args.out:
            out.close()

    if args.out:
        print(f"gen_corpus: wrote {args.n} records to {args.out} ({args.format})", file=sys.stderr)


if __name__ == "__main__":
    main()