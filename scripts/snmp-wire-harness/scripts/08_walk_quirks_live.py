"""Check 3 (v06 extra): walk termination hardening (#25), live UDP path.

Scripted quirk agent (duplicate rows, request-OID echo, zero-progress) driven
against the real V2cManager + dispatcher + UDP transport, mirroring the
upstream test_walk_quirks.py scenarios but as an independent live check.
"""

from __future__ import annotations

import asyncio

from smokecommon import main, record

from trishul_snmp import V2cManager
from trishul_snmp.types import OID, EndOfMibViewValue, NullValue, SnmpValueType, VarBind
from trishul_snmp.wire.message import SnmpMessage, decode_message, encode_message
from trishul_snmp.wire.pdu import Pdu, PduType, RawVarBind

ROOT: OID = (1, 3, 6, 1, 4, 1, 90000, 1)
A: OID = ROOT + (1,)
B: OID = ROOT + (2,)
OUTSIDE: OID = (1, 3, 6, 1, 4, 1, 90000, 2, 1)


class QuirkAgent(asyncio.DatagramProtocol):
    """Answers each request with the next scripted response (a varbind list)."""

    def __init__(self, script):
        self.script = list(script)  # queue of varbind lists, one per response

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        msg = decode_message(data)
        if msg.pdu.pdu_type not in (PduType.GET_NEXT, PduType.GET_BULK):
            return
        if not self.script:
            return
        varbinds = self.script.pop(0)
        resp = SnmpMessage(
            version=1,
            community=msg.community,
            pdu=Pdu(
                pdu_type=PduType.RESPONSE,
                request_id=msg.pdu.request_id,
                error_status=0,
                error_index=0,
                varbinds=tuple(
                    RawVarBind(oid=oid, value=value if value is not None else NullValue())
                    for oid, value in varbinds
                ),
            ),
        )
        self.transport.sendto(encode_message(resp), addr)


async def start_agent(script, port):
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: QuirkAgent(script), local_addr=("127.0.0.1", port)
    )
    return transport, protocol


def oids(walked: list[VarBind]) -> list[OID]:
    return [vb.oid for vb in walked]


async def run() -> None:
    # 1. duplicate rows: one response carries A twice, then B, then outside —
    #    walk must dedupe and continue (0.6.0 hardening; 0.5.x silently truncated).
    t1, _ = await start_agent(
        [
            [(A, None), (A, None)],  # duplicate row in one response
            [(B, None)],             # progress
            [(OUTSIDE, None)],       # leave subtree -> stop
        ],
        11250,
    )
    async with V2cManager(host="127.0.0.1", port=11250, community="public", timeout=2, retries=1) as mgr:
        walked = await asyncio.wait_for(mgr.walk(".".join(map(str, ROOT))), timeout=10)
    t1.close()
    dedupe_ok = oids(walked) == [A, B]
    record(
        "walk-quirk-duplicate-row",
        dedupe_ok,
        f"duplicate-row agent: walk={oids(walked)} — deduped and continued "
        f"(0.5.x behavior: silent truncation)",
    )

    # 2. OID echo: after legitimately returning A (progress from root), the
    #    agent echoes A again for the next request — the echo must be dropped
    #    as a phantom row (not accepted as a row), and zero progress ends the
    #    walk with the rows gathered so far.
    t2, _ = await start_agent(
        [
            [(A, None)],  # legit progress from ROOT
            [(A, None)],  # echo of the requested OID -> phantom, must be dropped
        ],
        11251,
    )
    async with V2cManager(host="127.0.0.1", port=11251, community="public", timeout=2, retries=1) as mgr:
        walked = await asyncio.wait_for(mgr.walk(".".join(map(str, ROOT))), timeout=10)
    t2.close()
    echo_ok = oids(walked) == [A]
    record(
        "walk-quirk-oid-echo",
        echo_ok,
        f"request-echoing agent: walk={oids(walked)} — echo rejected as phantom row, walk "
        f"terminated with prior rows kept (no duplication, no loop)",
    )

    # 3. zero-progress: agent always returns the same next OID — walk must
    #    terminate, not loop forever (0.6.0 hardening).
    t3, _ = await start_agent(
        [
            [(A, None)],   # progress from ROOT
            [(A, None)],   # no progress: echoes A forever -> must terminate
            [(A, None)],
            [(A, None)],
            [(A, None)],
        ],
        11252,
    )
    async with V2cManager(host="127.0.0.1", port=11252, community="public", timeout=2, retries=1) as mgr:
        walked = await asyncio.wait_for(mgr.walk(".".join(map(str, ROOT))), timeout=10)
    t3.close()
    zero_ok = oids(walked) == [A]
    record(
        "walk-quirk-zero-progress",
        zero_ok,
        f"zero-progress agent (always returns {A}): walk={oids(walked)} — terminated, no infinite loop",
    )

    # 4. EndOfMibView termination still honored (regression).
    t4, _ = await start_agent([[(ROOT + (1,), EndOfMibViewValue())]], 11253)
    async with V2cManager(host="127.0.0.1", port=11253, community="public", timeout=2, retries=1) as mgr:
        walked = await asyncio.wait_for(mgr.walk(".".join(map(str, ROOT))), timeout=10)
    t4.close()
    eomv_ok = oids(walked) == []
    record(
        "walk-quirk-endofmibview",
        eomv_ok,
        f"endOfMibView-first agent: walk={oids(walked)} — terminated cleanly",
    )


main(run)
