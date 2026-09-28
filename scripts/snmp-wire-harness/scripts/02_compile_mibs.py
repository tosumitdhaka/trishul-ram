"""Check 2: tsmi compiles TRAM's raw MIB corpus to JSON, bundles load and resolve.

Corpus: /home/dhaka/trishul/trishul-ram/files/mibs/ — 15 raw ASN.1 MIB files
(extension-less), the same corpus named by the 2026-09-21 feasibility doc.
Methodology match: FileReader over the raw dir, JSON output, then
load_bundle + resolve/lookup both directions.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from smokecommon import main, record

TRAM_MIB_DIR = Path("/home/dhaka/trishul/trishul-ram/files/mibs")
OUT = Path("/tmp/opencode/tsmi-smoke-v06/mibs-out")

from trishul_smi import CompilerConfig, FileReader, MibCompiler
from trishul_smi.output import available_formats


async def run() -> None:
    corpus = sorted(p.name for p in TRAM_MIB_DIR.iterdir() if p.is_file())
    print(f"corpus ({len(corpus)}): {', '.join(corpus)}")
    print(f"formats available: {available_formats()}")

    cfg = CompilerConfig(
        output_dir=OUT,
        formats=["json"],
        emit_manifest=True,
        emit_oid_index=True,
        reproducible=True,
    )
    compiler = MibCompiler(cfg)
    compiler.add_reader(FileReader(str(TRAM_MIB_DIR)))
    t0 = time.monotonic()
    results = await compiler.compile(*corpus)
    elapsed = time.monotonic() - t0

    failed = [r for r in results if r.status == "error"]
    statuses: dict[str, int] = {}
    for r in results:
        statuses[r.status] = statuses.get(r.status, 0) + 1
    print(f"statuses: {statuses} in {elapsed:.2f}s")
    for r in failed:
        print(f"  ERROR {r.name}: {r.error}")
    for r in results:
        if r.warnings:
            print(f"  WARN {r.name}: {len(r.warnings)} warnings")

    json_files = sorted(OUT.glob("*.json"))
    ok_compile = not failed and len(json_files) >= len(corpus) + 1  # + manifest
    record(
        "mib-compile-corpus",
        ok_compile,
        f"{len(corpus)} MIBs requested, {len(json_files)} JSON artifacts written, "
        f"statuses={statuses}, {elapsed:.1f}s, deps auto-resolved via FileReader",
    )

    # bundle load + resolve both directions (methodology: sysDescr <-> 1.3.6.1.2.1.1.1)
    import sys

    sys.path.insert(0, str(OUT))
    from trishul_snmp import load_bundle

    bundle = load_bundle(str(OUT / "SNMPv2-MIB.json"))
    forward = bundle.resolve("SNMPv2-MIB::sysDescr")
    reverse = bundle.lookup("1.3.6.1.2.1.1.1")
    print(f"resolve(sysDescr) = {forward}")
    print(f"lookup(1.3.6.1.2.1.1.1) = {reverse}")
    ok_resolve = (
        tuple(forward) == (1, 3, 6, 1, 2, 1, 1, 1)
        and getattr(reverse, "symbol", None) == "sysDescr"
        and tuple(getattr(reverse, "oid", ())) == (1, 3, 6, 1, 2, 1, 1, 1)
    )
    record(
        "mib-bundle-resolve",
        ok_resolve,
        f"load_bundle(SNMPv2-MIB.json): resolve(sysDescr)={tuple(forward)}, "
        f"lookup(1.3.6.1.2.1.1.1)={reverse}",
    )

    # cross-MIB resolution: IF-MIB (depends on IANAifType-MIB, per original methodology)
    ifb = load_bundle(str(OUT / "IF-MIB.json"))
    ifn = ifb.resolve("IF-MIB::ifNumber")
    ok_if = tuple(ifn) == (1, 3, 6, 1, 2, 1, 2, 1)
    record(
        "mib-bundle-cross-module",
        ok_if,
        f"IF-MIB bundle (IANAifType dep auto-resolved): resolve(ifNumber)={tuple(ifn)}",
    )


main(run)
