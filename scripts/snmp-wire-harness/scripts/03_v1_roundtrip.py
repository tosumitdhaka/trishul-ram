"""Check 3: SNMPv1 (former blocker, trishul-snmp #8).

- v1 GET + GETNEXT roundtrip against a live v1 responder (two: tsmp's own
  community responder, and net-snmp snmpd 5.9.4 as an independent real agent)
- v1 WALK (GETBULK auto-downgrade to GETNEXT loop per v0.5.0 notes)
- v1 TRAP send (V1Notifier, enterprise/generic/specific/timestamp)
- v1 trap receive + decode_notification on the v1 community listener,
  including a pysnmp-originated v1 trap (repro of the old silent drop)
"""

from __future__ import annotations

import asyncio
import socket

from smokecommon import main, record

from trishul_snmp import (
    OctetStringValue,
    V1Manager,
    V1Notifier,
    V2cNotificationListener,
    V2cResponder,
    decode_notification,
)

SNMPD = ("127.0.0.1", 11162)
PYSNMP_AGENT = ("127.0.0.1", 11163)


async def check_v1_requests_instack() -> None:
    async with V2cResponder(
        host="127.0.0.1",
        port=11165,
        communities=["public"],
        objects=[
            ((1, 3, 6, 1, 2, 1, 1, 1, 0), OctetStringValue(b"tsmp v1-capable responder")),
            ((1, 3, 6, 1, 2, 1, 1, 5, 0), OctetStringValue(b"v1-box")),
        ],
    ) as responder:
        serve = asyncio.create_task(responder.serve_forever())
        async with V1Manager(host="127.0.0.1", port=11165, community="public") as mgr:
            r = await mgr.get("1.3.6.1.2.1.1.1.0")
            got = r.varbinds[0].value.value
            n = await mgr.get_next("1.3.6.1.2.1.1.1.0")
            nxt = (n.varbinds[0].oid, n.varbinds[0].value.value)
            ok = got == b"tsmp v1-capable responder" and nxt[0] == (1, 3, 6, 1, 2, 1, 1, 5, 0)
            record(
                "v1-get-getnext-instackbar",
                ok,
                f"V1Manager GET sysDescr.0='{got.decode()}' + GETNEXT -> {nxt[0]} "
                f"(tsmp community responder :11165)",
            )
    serve.cancel()


async def check_v1_requests_snmpd() -> None:
    async with V1Manager(host=SNMPD[0], port=SNMPD[1], community="public") as mgr:
        r = await mgr.get("1.3.6.1.2.1.1.1.0")
        got = r.varbinds[0].value.value
        n = await mgr.get_next("1.3.6.1.2.1.1.1.0")
        nxt_oid = n.varbinds[0].oid
        w = await mgr.walk("1.3.6.1.2.1.1")
        n_walk = len(w)
        ok = (
            b"Linux" in got
            and nxt_oid > (1, 3, 6, 1, 2, 1, 1, 1, 0)
            and n_walk >= 6
        )
        record(
            "v1-get-getnext-snmpd",
            ok,
            f"V1Manager vs net-snmp snmpd 5.9.4 :11162 — GET sysDescr.0 ok ({got[:32]!r}), "
            f"GETNEXT -> {nxt_oid}, walk(1.3.6.1.2.1.1) returned {n_walk} varbinds "
            f"(GETNEXT-loop, GETBULK downgraded)",
        )


async def check_v1_trap_send_and_receive() -> None:
    async with V2cNotificationListener(
        host="127.0.0.1", port=11167, communities=["public"]
    ) as listener:
        async with V1Notifier(host="127.0.0.1", port=11167, community="public") as notifier:
            ts = await notifier.send_trap(
                (1, 3, 6, 1, 4, 1, 99999),
                agent_addr="127.0.0.1",
                generic_trap=6,
                specific_trap=42,
                timestamp=123456,
                varbinds=[((1, 3, 6, 1, 4, 1, 99999, 1, 0), OctetStringValue(b"v1-alarm"))],
            )
        event = await asyncio.wait_for(listener.receive(), timeout=3)
        d = event.to_dict()
        trap_fields = {
            k: d.get(k) for k in ("enterprise", "generic_trap", "specific_trap", "agent_addr", "timestamp", "pdu_type")
        }
        print(f"v1 trap event dict: {trap_fields}")
        ok = (
            ts == 123456
            and d.get("pdu_type") == "trap"
            and str(d.get("enterprise", "")).endswith("99999")
            and d.get("generic_trap") == 6
            and d.get("specific_trap") == 42
            and d.get("timestamp") == 123456
        )
        record(
            "v1-trap-send-receive",
            ok,
            f"V1Notifier trap (enterprise=...99999, generic=6, specific=42, ts={ts}) -> "
            f"V2cNotificationListener :11167 received; event={trap_fields}",
        )


async def check_pysnmp_v1_trap_to_tsmp() -> None:
    """Repro of the old blocker: pysnmp-originated v1 trap must now be received."""

    from pysnmp.hlapi.asyncio import (
        CommunityData,
        ContextData,
        NotificationType,
        ObjectIdentity,
        SnmpEngine,
        UdpTransportTarget,
        send_notification,
    )

    captured: list[bytes] = []
    loop = asyncio.get_running_loop()

    class Grab(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            captured.append(data)

    transport, _ = await loop.create_datagram_endpoint(
        Grab, local_addr=("127.0.0.1", 11168)
    )

    async with V2cNotificationListener(
        host="127.0.0.1", port=11169, communities=["public"]
    ) as listener:
        # one v1 trap to the tsmp listener (live receive) ...
        engine = SnmpEngine()
        await send_notification(
            engine,
            CommunityData("public", mpModel=0),
            await UdpTransportTarget.create(("127.0.0.1", 11169), timeout=3, retries=0),
            ContextData(),
            "trap",
            NotificationType(ObjectIdentity("SNMPv2-MIB", "coldStart")),
        )
        # ... and one to the raw capture socket (offline decode)
        await send_notification(
            engine,
            CommunityData("public", mpModel=0),
            await UdpTransportTarget.create(("127.0.0.1", 11168), timeout=3, retries=0),
            ContextData(),
            "trap",
            NotificationType(ObjectIdentity("SNMPv2-MIB", "warmStart")),
        )
        event = await asyncio.wait_for(listener.receive(), timeout=3)
        ed = event.to_dict()
        print(f"pysnmp v1 trap -> tsmp listener event: {ed}")
        deadline = loop.time() + 3
        while not captured and loop.time() < deadline:
            await asyncio.sleep(0.05)
    transport.close()

    data = captured[0] if captured else b""
    decoded_ok = False
    decoded_info = "no datagram captured"
    if data:
        ev = decode_notification(data)
        dd = ev.to_dict()
        decoded_info = (
            f"{len(data)}B decoded: pdu_type={dd.get('pdu_type')}, generic_trap={dd.get('generic_trap')}, "
            f"enterprise={dd.get('enterprise')}"
        )
        decoded_ok = dd.get("pdu_type") == "trap" and dd.get("generic_trap") in (0, 1)

    live_ok = ed.get("pdu_type") == "trap" and ed.get("generic_trap") in (0, 1)
    record(
        "v1-pysnmp-trap-received",
        live_ok,
        f"pysnmp 7.1.29 v1 trap (coldStart) -> tsmp listener :11169 received (old 0.4.2 "
        f"behavior: silent drop): pdu_type={ed.get('pdu_type')}, generic_trap={ed.get('generic_trap')}, "
        f"enterprise={ed.get('enterprise')}",
    )
    record(
        "v1-decode-notification",
        decoded_ok,
        f"decode_notification() on pysnmp v1 Trap-PDU: {decoded_info} "
        f"(old 0.4.2: ProtocolError 'Unsupported PDU tag 0xa4')",
    )


async def run() -> None:
    await check_v1_requests_instack()
    await check_v1_requests_snmpd()
    await check_v1_trap_send_and_receive()
    await check_pysnmp_v1_trap_to_tsmp()


main(run)
