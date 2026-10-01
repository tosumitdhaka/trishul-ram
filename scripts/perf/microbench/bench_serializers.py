"""Microbenchmark every registered TRAM serializer on the canonical CDR corpus.

Phase 1c of the TRAM capacity study. Measures parse (input → records) and
serialize (records → output) throughput at n = 10,000 records, warmup on
1,000 records, median of 5 reps, single process.

Payload construction per serializer (honest representative inputs):
  json / ndjson / csv / xml : the serializer's own output over the canonical
                             corpus (round-trip representative; XML values are
                             str()-cast by design of the wire format).
  text                     : one compact-JSON CDR per line (syslog-style
                             line-oriented payload; lines are opaque to the
                             serializer).
  bytes                    : 10,000 x ~500 B payloads (one CDR each) — the
                             serializer wraps a whole payload in a single
                             envelope record, so per-file/message passthrough
                             is its realistic unit of work.
  pm_xml                   : 3GPP TS 32.432 measData document — 10 measInfo
                             blocks x 1,000 measValue elements x 6 counters,
                             Nokia-style counter names, 900 s granPeriods.
  asn1                     : BER-encoded CdrRecord stream (concatenated
                             top-level TLVs, split_records=True), encoded with
                             asn1tools from scripts/perf/microbench/data/cdr.asn.
                             Decode-only by design — serialize is not supported.

Skipped (dependency missing from the venv, nothing pip-installed):
  avro (fastavro), msgpack (msgpack), parquet (pyarrow),
  protobuf (protobuf + grpcio-tools).

Numbers are comparative (WSL2 host), not absolute.
"""

from __future__ import annotations

import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import corpus  # noqa: E402

sys.path.insert(0, str(HERE.parents[2]))  # repo root, for tram package

from tram.serializers.asn1_serializer import Asn1Serializer  # noqa: E402
from tram.serializers.bytes_serializer import BytesSerializer  # noqa: E402
from tram.serializers.csv_serializer import CsvSerializer  # noqa: E402
from tram.serializers.json_serializer import JsonSerializer  # noqa: E402
from tram.serializers.ndjson_serializer import NdjsonSerializer  # noqa: E402
from tram.serializers.pm_xml_serializer import PmXmlSerializer  # noqa: E402
from tram.serializers.text_serializer import TextSerializer  # noqa: E402
from tram.serializers.xml_serializer import XmlSerializer  # noqa: E402

RESULTS_DIR = HERE / "results"
COPY_DIR = Path("/tmp/opencode/perf-microbench")

PM_COUNTERS = [
    "attTCHSeizures", "nackReceived", "dlCodeSamples", "rrcConnEstabSucc",
    "thpVolDl", "pdcpBytesDl",
]

ASN_KEYMAP = {
    "record_id": "recordId", "timestamp": "timestamp", "msisdn": "msisdn",
    "imsi": "imsi", "imei": "imei", "cell_id": "cellId", "event_type": "eventType",
    "direction": "direction", "duration_s": "durationS", "bytes_up": "bytesUp",
    "bytes_down": "bytesDown", "rat": "rat", "roaming": "roaming",
    "charge_amount": "chargeAmount", "currency": "currency",
    "session_start": "sessionStart", "session_end": "sessionEnd",
    "apn": "apn", "sgsn_addr": "sgsnAddr", "ggsn_addr": "ggsnAddr",
}


def build_pm_xml(n_meas_values: int) -> bytes:
    """3GPP TS 32.432 measData: 10 measInfo x 1,000 measValue x 6 counters."""
    import random

    rng = random.Random(7)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<measData>",
        '<managedElement localDn="PLMN-PLMN/RNC-042"/>',
    ]
    per_info = 1000
    t = corpus.BASE_EPOCH
    for start in range(0, n_meas_values, per_info):
        lines.append(f'<measInfo measInfoId="MI-{start // per_info}">')
        end = datetime.fromtimestamp(t + 900, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f'<granPeriod endTime="{end}" duration="PT900S"/>')
        for p, counter in enumerate(PM_COUNTERS, 1):
            lines.append(f'<measType p="{p}">{counter}</measType>')
        for j in range(per_info):
            lines.append(f'<measValue measObjLdn="RNC-042/WBTS-{j % 80}/WCEL-{j % 40}">')
            for p in range(1, len(PM_COUNTERS) + 1):
                lines.append(f'<r p="{p}">{rng.randrange(0, 10**6)}</r>')
            lines.append("</measValue>")
        lines.append("</measInfo>")
        t += 900
    lines.append("</measData>")
    return "\n".join(lines).encode()


def build_asn1_payload(records: list[dict]) -> bytes:
    import asn1tools

    compiled = asn1tools.compile_files([str(HERE / "data" / "cdr.asn")], "ber")
    out = []
    for rec in records:
        out.append(compiled.encode("CdrRecord", {v: rec[k] for k, v in ASN_KEYMAP.items()}))
    return b"".join(out)


def _rate_row(elapsed_ns: int, n_records: int, payload_bytes: int) -> dict:
    seconds = elapsed_ns / 1e9
    return {
        "records_s": round(n_records / seconds),
        "us_per_record": round(elapsed_ns / n_records / 1e3, 3),
        "mb_s": round(payload_bytes / seconds / (1024 * 1024), 2),
    }


def bench_standard(name: str, serializer, build_payload, records: list[dict], notes: str) -> dict:
    """Standard parse/serialize bench: serialize input = parse output records.

    Warmup runs one full parse+serialize cycle on a payload built from the
    first 1,000 records; timed reps then run on the full 10,000-record payload.
    """
    import time

    warm = serializer.__class__(serializer.config)
    warm_payload = build_payload(records[: corpus.WARMUP_RECORDS])
    warm.parse(warm_payload)

    payload = build_payload(records)
    parse_timings = []
    for _ in range(corpus.REPS):
        t0 = time.perf_counter_ns()
        parsed = serializer.parse(payload)
        parse_timings.append(time.perf_counter_ns() - t0)
    out_payload = serializer.serialize(parsed)
    serialize_timings = []
    for _ in range(corpus.REPS):
        t0 = time.perf_counter_ns()
        serializer.serialize(parsed)
        serialize_timings.append(time.perf_counter_ns() - t0)

    n = len(records)
    return {
        "name": name,
        "payload_bytes": len(payload),
        "bytes_per_record": round(len(payload) / n, 1),
        "parse": _rate_row(corpus.median_ns(parse_timings), len(parsed), len(payload)),
        "serialize": _rate_row(corpus.median_ns(serialize_timings), len(parsed), len(out_payload)),
        "notes": notes,
    }


def bench_bytes(n: int) -> dict:
    """Bytes passthrough: one payload per CDR (envelope semantics)."""
    import time

    records = corpus.make_records(n)
    payloads = [json.dumps(r, separators=(",", ":")).encode() for r in records]
    serializer = BytesSerializer({})

    def do_parse() -> list:
        return [serializer.parse(p) for p in payloads]

    def do_serialize(envelopes: list) -> None:
        for env in envelopes:
            serializer.serialize(env)

    # Warmup on 1,000 payloads
    warm = do_parse()[: corpus.WARMUP_RECORDS]
    do_serialize(warm)

    parse_timings = []
    for _ in range(corpus.REPS):
        t0 = time.perf_counter_ns()
        envelopes = do_parse()
        parse_timings.append(time.perf_counter_ns() - t0)
    total_in = sum(len(p) for p in payloads)
    serialize_timings = []
    for _ in range(corpus.REPS):
        t0 = time.perf_counter_ns()
        do_serialize(envelopes)
        serialize_timings.append(time.perf_counter_ns() - t0)
    total_out = sum(len(p) for p in payloads)

    return {
        "name": "bytes",
        "payload_bytes": total_in,
        "bytes_per_record": round(total_in / n, 1),
        "parse": _rate_row(corpus.median_ns(parse_timings), n, total_in),
        "serialize": _rate_row(corpus.median_ns(serialize_timings), n, total_out),
        "notes": (
            "Passthrough envelope: parse wraps a whole payload in ONE record "
            "(_raw/_size); serialize consumes only records[0]. Benched as "
            f"{n} x ~{total_in // n} B payloads (one CDR file/message each); "
            "records/s = payloads/s."
        ),
    }


def bench_asn1(n: int) -> dict:
    """ASN.1 BER decode-only bench (serialize unsupported by design)."""
    import time

    records = corpus.make_records(n)
    payload = build_asn1_payload(records)
    config = {
        "schema_file": str(HERE / "data" / "cdr.asn"),
        "message_class": "CdrRecord",
        "encoding": "ber",
        "split_records": True,
    }

    # Warmup on a 1,000-record payload.
    Asn1Serializer(config).parse(build_asn1_payload(records[: corpus.WARMUP_RECORDS]))

    serializer = Asn1Serializer(config)
    parse_timings = []
    for _ in range(corpus.REPS):
        t0 = time.perf_counter_ns()
        parsed = serializer.parse(payload)
        parse_timings.append(time.perf_counter_ns() - t0)

    return {
        "name": "asn1",
        "payload_bytes": len(payload),
        "bytes_per_record": round(len(payload) / n, 1),
        "parse": _rate_row(corpus.median_ns(parse_timings), len(parsed), len(payload)),
        "serialize": None,
        "notes": (
            "Decode-only by design (serialize raises SerializerError). Input = "
            "concatenated BER CdrRecord TLVs (3GPP CDR-file style), encoded "
            "with asn1tools from data/cdr.asn."
        ),
    }


def main() -> None:
    n = corpus.N_RECORDS
    records = corpus.make_records(n)
    results: list[dict] = []

    json_ser = JsonSerializer({})
    results.append(bench_standard(
        "json", json_ser,
        lambda recs: JsonSerializer({}).serialize(recs), records,
        "Payload = JSON array of canonical CDRs."))

    ndjson_ser = NdjsonSerializer({})
    results.append(bench_standard(
        "ndjson", ndjson_ser,
        lambda recs: b"\n".join(
            json.dumps(r, separators=(",", ":")).encode() for r in recs), records,
        "Payload = one compact-JSON CDR per line."))

    csv_ser = CsvSerializer({})
    results.append(bench_standard(
        "csv", csv_ser,
        lambda recs: CsvSerializer({}).serialize(recs), records,
        "Header + 10k rows; parse output values are strings (CSV semantics)."))

    xml_ser = XmlSerializer({})
    results.append(bench_standard(
        "xml", xml_ser,
        lambda recs: XmlSerializer({}).serialize(recs), records,
        "defusedxml parse / lxml pretty-print serialize; parse output values "
        "are strings (XML text nodes)."))

    text_ser = TextSerializer({})
    results.append(bench_standard(
        "text", text_ser,
        lambda recs: b"\n".join(
            json.dumps(r, separators=(",", ":")).encode() for r in recs), records,
        "Payload = one compact-JSON CDR per line (lines opaque to the "
        "serializer); parse yields {_line, _line_num} records, serialize "
        "round-trips the _line field."))

    pm_ser = PmXmlSerializer({})
    results.append(bench_standard(
        "pm_xml", pm_ser, lambda recs: build_pm_xml(len(recs)), records,
        "Input constructed: 3GPP TS 32.432 measData, 10 measInfo x 1,000 "
        "measValue x 6 Nokia-style counters, numeric_values=true. Serialize "
        "output loses measType p-indices (one measInfo per record)."))

    results.append(bench_bytes(n))
    results.append(bench_asn1(n))

    skipped = [
        {"name": "avro", "reason": "fastavro not installed in venv (tram[avro])"},
        {"name": "msgpack", "reason": "msgpack not installed in venv (tram[msgpack_ser])"},
        {"name": "parquet", "reason": "pyarrow not installed in venv (tram[parquet])"},
        {"name": "protobuf",
         "reason": "protobuf + grpcio-tools not installed in venv (tram[protobuf_ser])"},
    ]

    output = {
        "meta": {
            "phase": "1c-serializers",
            "repo_version": "v1.5.1 @ 326d9dd",
            "records": n,
            "warmup_records": corpus.WARMUP_RECORDS,
            "reps": corpus.REPS,
            "statistic": "median",
            "cpu": corpus.cpu_model(),
            "python": platform.python_version(),
            "host_note": "WSL2 host — numbers are comparative, not absolute",
            "generated": datetime.now(tz=UTC).isoformat(),
        },
        "results": results,
        "skipped": skipped,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / "serializers.json"
    out_path.write_text(json.dumps(output, indent=2))
    COPY_DIR.mkdir(parents=True, exist_ok=True)
    (COPY_DIR / "serializers.json").write_text(json.dumps(output, indent=2))

    _print_table(results, skipped)
    print(f"\nresults → {out_path} (copied to {COPY_DIR}/serializers.json)")


def _print_table(results: list[dict], skipped: list[dict]) -> None:
    print(f"{'serializer':<10} {'parse rec/s':>12} {'parse µs/rec':>13} "
          f"{'parse MB/s':>11} {'ser rec/s':>12} {'ser µs/rec':>11} "
          f"{'ser MB/s':>9} {'B/rec':>7}")
    print("-" * 92)
    for r in results:
        p, s = r["parse"], r["serialize"]
        if s is None:
            print(f"{r['name']:<10} {p['records_s']:>12,} {p['us_per_record']:>13} "
                  f"{p['mb_s']:>11} {'n/a (decode-only)':>21} "
                  f"{'':>9} {r['bytes_per_record']:>7}")
        else:
            print(f"{r['name']:<10} {p['records_s']:>12,} {p['us_per_record']:>13} "
                  f"{p['mb_s']:>11} {s['records_s']:>12,} {s['us_per_record']:>11} "
                  f"{s['mb_s']:>9} {r['bytes_per_record']:>7}")
    if skipped:
        print("\nskipped (missing deps, not installed):")
        for sk in skipped:
            print(f"  - {sk['name']}: {sk['reason']}")


if __name__ == "__main__":
    main()
