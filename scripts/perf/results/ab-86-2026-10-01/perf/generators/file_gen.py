#!/usr/bin/env python3
"""Materialize batch input files for local-source bench scenarios.

Writes ``--files N`` files of ``--records M`` records each into ``--out DIR``
as ``batch_000001.<ext>`` … ``batch_00000N.<ext>``. Record content reuses the
canonical CDR schema from ``gen_corpus`` (deterministic given ``--seed``).

Formats:
    jsonl / ndjson   one compact JSON record per line
    csv              header + rows (always flat, even with --nested)
    xml              <records> root with one <record> per line
    pm_xml           3GPP measData doc: M <measValue> entries (one bench
                     record each), counters declared as <measType> elements
                     under a single <measInfo>

The xml/pm_xml formats are byte-parseable by TRAM's xml / pm_xml serializers
as configured in the fsweep and s2 templates.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from gen_corpus import generate, write_csv, write_jsonl

# Counter fields emitted as <measType> / <r p=N> pairs in pm_xml output.
PM_COUNTER_FIELDS = [
    "record_id",
    "cell_id",
    "event_type",
    "duration_s",
    "bytes_up",
    "bytes_down",
    "roaming",
    "charge_amount",
    "apn",
    "sgsn_addr",
    "ggsn_addr",
]


def write_xml(records: list[dict], out) -> None:
    root = ET.Element("records")
    for record in records:
        element = ET.SubElement(root, "record")
        for key, value in record.items():
            if isinstance(value, dict):
                child = ET.SubElement(element, key)
                for sub_key, sub_value in value.items():
                    ET.SubElement(child, sub_key).text = str(sub_value)
            else:
                ET.SubElement(element, key).text = str(value)
    out.write(ET.tostring(root, encoding="unicode"))


def write_pm_xml(records: list[dict], out) -> None:
    meas_data = ET.Element("measData")
    ET.SubElement(meas_data, "managedElement", localDn="PLMN-PLMN/LNBTS-1001")
    meas_info = ET.SubElement(meas_data, "measInfo", measInfoId="PMCDR")
    ET.SubElement(meas_info, "granPeriod", endTime="2026-01-01T00:00:00Z", duration="900")
    for p, field in enumerate(PM_COUNTER_FIELDS, start=1):
        ET.SubElement(meas_info, "measType", p=str(p)).text = field
    for record in records:
        meas_value = ET.SubElement(meas_info, "measValue", measObjLdn=f"LNBTS-1001.{record['cell_id']}")
        for p, field in enumerate(PM_COUNTER_FIELDS, start=1):
            value = record.get(field, "")
            ET.SubElement(meas_value, "r", p=str(p)).text = "" if value is None else str(value)
    out.write(ET.tostring(meas_data, encoding="unicode"))


_FORMAT_WRITERS = {
    "jsonl": write_jsonl,
    "ndjson": write_jsonl,
    "csv": write_csv,
    "xml": write_xml,
    "pm_xml": write_pm_xml,
}

_EXTENSIONS = {
    "jsonl": "jsonl",
    "ndjson": "ndjson",
    "csv": "csv",
    "xml": "xml",
    "pm_xml": "xml",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Materialize N batch files of M canonical CDR records for local-source benches."
    )
    parser.add_argument("--files", type=int, required=True, help="Number of batch files to write")
    parser.add_argument("--records", type=int, required=True, help="Records per file")
    parser.add_argument(
        "--format",
        choices=sorted(_FORMAT_WRITERS),
        default="jsonl",
        help="File format (default jsonl)",
    )
    parser.add_argument("--out", required=True, help="Output directory (created if missing)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (default 42)")
    parser.add_argument("--prefix", default="batch", help="File name prefix (default 'batch')")
    args = parser.parse_args()

    if args.files < 1:
        parser.error("--files must be >= 1")
    if args.records < 1:
        parser.error("--records must be >= 1")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = _EXTENSIONS[args.format]
    writer = _FORMAT_WRITERS[args.format]

    for i in range(1, args.files + 1):
        # Seed per file keeps each batch self-consistent while varying content.
        records = generate(args.seed + i, args.records, nested=False)
        path = out_dir / f"{args.prefix}_{i:06d}.{ext}"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer(records, handle)
    print(
        f"file_gen: wrote {args.files} x {args.records} records ({args.format}) to {out_dir}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()