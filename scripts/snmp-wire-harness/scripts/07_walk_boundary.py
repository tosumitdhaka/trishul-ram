"""Check 7: walk boundary / subtree semantics (recreated; original 0.4.x-era
harness scripts at /tmp/opencode/tsmi-smoke/ were swept, so these checks are
rebuilt from the 2026-09-21 feasibility doc's methodology: walk stops cleanly
at subtree end, GETNEXT past the last object returns EndOfMibView).

- lexicographic GETNEXT across a gap between subtrees
- walk(subtree) returns exactly the subtree's objects, both bulk and
  GETNEXT-loop modes
- GETNEXT beyond the last object -> EndOfMibViewValue
- v1 manager walk (GETNEXT loop) same boundary behavior
"""

from __future__ import annotations

import asyncio

from smokecommon import main, record

from trishul_snmp import (
    EndOfMibViewValue,
    OctetStringValue,
    V1Manager,
    V2cManager,
    V2cResponder,
)

SYS = ((1, 3, 6, 1, 2, 1, 1), 3, 0)  # sys subtree scalars
IF = ((1, 3, 6, 1, 2, 1, 2, 2, 1, 1), 3, None)  # ifIndex rows (no .0)

OBJECTS = [
    ((1, 3, 6, 1, 2, 1, 1, 1, 0), OctetStringValue(b"sysDescr")),
    ((1, 3, 6, 1, 2, 1, 1, 5, 0), OctetStringValue(b"sysName")),
    ((1, 3, 6, 1, 2, 1, 1, 6, 0), OctetStringValue(b"sysLocation")),
    ((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1), OctetStringValue(b"1")),
    ((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 2), OctetStringValue(b"2")),
    ((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 3), OctetStringValue(b"3")),
    ((1, 3, 6, 1, 4, 1, 99999, 2, 0), OctetStringValue(b"enterprise tail")),
]


async def run() -> None:
    async with V2cResponder(
        host="127.0.0.1", port=11240, communities=["public"], objects=OBJECTS
    ) as responder:
        serve = asyncio.create_task(responder.serve_forever())
        async with V2cManager(host="127.0.0.1", port=11240, community="public") as mgr:
            # 1. walk exactly the sys subtree (bulk + getnext loop)
            bulk = await mgr.walk("1.3.6.1.2.1.1", bulk=True)
            seq = await mgr.walk("1.3.6.1.2.1.1", bulk=False)
            bulk_oids = [tuple(vb.oid) for vb in bulk]
            seq_oids = [tuple(vb.oid) for vb in seq]
            expected_sys = [
                (1, 3, 6, 1, 2, 1, 1, 1, 0),
                (1, 3, 6, 1, 2, 1, 1, 5, 0),
                (1, 3, 6, 1, 2, 1, 1, 6, 0),
            ]
            ok_walk = bulk_oids == expected_sys and seq_oids == expected_sys

            # 2. GETNEXT gap crossing: last sys object -> first if object
            gap = await mgr.get_next("1.3.6.1.2.1.1.6.0")
            gap_oid = tuple(gap.varbinds[0].oid)
            ok_gap = gap_oid == (1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1)

            # 3. GETNEXT beyond the very last object -> EndOfMibView
            beyond = await mgr.get_next("1.3.6.1.4.1.99999.2.0")
            beyond_val = beyond.varbinds[0].value
            ok_beyond = isinstance(beyond_val, EndOfMibViewValue)

            # 4. enterprise subtree walk (tuple-space ordering: 2.0 after 1.x gaps)
            ent = await mgr.walk("1.3.6.1.4.1.99999")
            ok_ent = [tuple(vb.oid) for vb in ent] == [(1, 3, 6, 1, 4, 1, 99999, 2, 0)]

            record(
                "walk-boundary-v2c",
                ok_walk and ok_gap and ok_beyond and ok_ent,
                f"walk(sys) bulk/seq == {expected_sys} (exact, no over-read); GETNEXT gap "
                f"sys->if = {gap_oid}; GETNEXT past last OID -> {type(beyond_val).__name__}; "
                f"walk(99999) = {[tuple(vb.oid) for vb in ent]}",
            )

        async with V1Manager(host="127.0.0.1", port=11240, community="public") as v1:
            v1_bulk = await v1.walk("1.3.6.1.2.1.1")
            v1_if = await v1.walk("1.3.6.1.2.1.2")
            ok = [tuple(vb.oid) for vb in v1_bulk] == expected_sys and len(v1_if) == 3
            record(
                "walk-boundary-v1",
                ok,
                f"V1Manager walk(sys)={[tuple(vb.oid) for vb in v1_bulk]} (GETNEXT loop, "
                f"GETBULK downgraded), walk(ifTable)={len(v1_if)} rows — boundary honored",
            )
    serve.cancel()


main(run)
