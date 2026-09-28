"""Check 5: regressions vs the 2026-09-21 (0.4.x) assessment.

- v2c GET and WALK (in-stack responder)
- v2c trap roundtrip (in-stack)
- v3 SHA256/AES128 trap roundtrip (in-stack)
- cross-stack: tsmp client -> pysnmp 7.1.29 agent (GET/GETNEXT/WALK)
- cross-stack: pysnmp client -> tsmp responder (GET)
- cross-stack: pysnmp v2c trap -> tsmp listener
- cross-stack: tsmp v2c trap -> pysnmp notification receiver (separate process)
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from smokecommon import main, record

from trishul_snmp import (
    AuthProtocol,
    OctetStringValue,
    PrivProtocol,
    UsmLocalEngine,
    UsmUser,
    V2cManager,
    V2cNotificationListener,
    V2cNotifier,
    V2cResponder,
    V3NotificationListener,
    V3Notifier,
)

AGENT = ("127.0.0.1", 11163)
RECV_LOG = Path("/tmp/opencode/tsmi-smoke-v06/logs/pysnmp-traprecv.log")


async def v2c_get_walk() -> None:
    async with V2cResponder(
        host="127.0.0.1",
        port=11220,
        communities=["public"],
        objects=[
            ((1, 3, 6, 1, 2, 1, 1, 1, 0), OctetStringValue(b"tsmp responder via interop")),
            ((1, 3, 6, 1, 2, 1, 1, 5, 0), OctetStringValue(b"box")),
            ((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1), OctetStringValue(b"1")),
            ((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 2), OctetStringValue(b"2")),
        ],
    ) as responder:
        serve = asyncio.create_task(responder.serve_forever())
        async with V2cManager(host="127.0.0.1", port=11220, community="public") as mgr:
            r = await mgr.get("1.3.6.1.2.1.1.1.0")
            got = r.varbinds[0].value.value
            walk_bulk = await mgr.walk("1.3.6.1.2.1.2")  # bulk=True default
            walk_seq = await mgr.walk("1.3.6.1.2.1.2", bulk=False)
            n_bulk, n_seq = len(walk_bulk), len(walk_seq)
            last_bulk = walk_bulk[-1].oid
            ok = (
                got == b"tsmp responder via interop"
                and n_bulk == 2
                and n_seq == 2
                and last_bulk == (1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 2)
            )
            record(
                "v2c-get-walk",
                ok,
                f"v2c GET sysDescr.0='{got.decode()}' + walk(1.3.6.1.2.1.2): bulk={n_bulk}, "
                f"getnext-loop={n_seq} varbinds, last={last_bulk} (responder :11220)",
            )
    serve.cancel()


async def v2c_trap_roundtrip() -> None:
    async with V2cNotificationListener(host="127.0.0.1", port=11221, communities=["public"]) as listener:
        async with V2cNotifier(host="127.0.0.1", port=11221, community="public") as notifier:
            await notifier.send_trap(
                (1, 3, 6, 1, 6, 3, 1, 1, 5, 3),
                varbinds=[((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1), OctetStringValue(b"eth0"))],
                uptime=999,
            )
        event = await asyncio.wait_for(listener.receive(), timeout=3)
        d = event.to_dict()
        varbinds = d.get("varbinds", [])
        has_mandatory = any(str(v.get("oid", "")).endswith(("1.3.6.1.2.1.1.3.0", "1.3.6.1.6.3.1.1.4.1.0")) for v in varbinds)
        ok = has_mandatory and len(varbinds) == 3
        record(
            "v2c-trap-roundtrip",
            ok,
            f"v2c trap (linkDown) -> listener :11221: {len(varbinds)} varbinds incl. auto-built "
            f"sysUpTime.0+snmpTrapOID.0 ({'yes' if has_mandatory else 'MISSING'})",
        )


async def v3_sha256_aes128_trap() -> None:
    listener_engine = UsmLocalEngine(
        engine_id=b"\x80\x00\x01\x02\x03" + bytes([0x77]) * 12, engine_boots=7, engine_time=111
    )
    sender_engine = UsmLocalEngine(
        engine_id=b"\x80\x00\x01\x02\x03" + bytes([0x78]) * 12, engine_boots=9, engine_time=222
    )
    user = UsmUser(
        username="u_sha256_aes128",
        auth_protocol=AuthProtocol.SHA256,
        auth_key=b"authpass-sha256",
        priv_protocol=PrivProtocol.AES128,
        priv_key=b"privpass-aes128",
    )
    async with V3NotificationListener(
        host="127.0.0.1", port=11222, user=user, local_engine=listener_engine
    ) as listener:
        async with V3Notifier(
            host="127.0.0.1", port=11222, user=user, local_engine=sender_engine
        ) as notifier:
            await notifier.send_trap(
                (1, 3, 6, 1, 6, 3, 1, 1, 5, 3),
                varbinds=[((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1), OctetStringValue(b"eth0"))],
                uptime=888,
            )
        event = await asyncio.wait_for(listener.receive(), timeout=3)
        d = event.to_dict()
        ok = len(d.get("varbinds", [])) >= 3
        record(
            "v3-sha256-aes128-trap-regression",
            ok,
            f"v3 authPriv SHA256/AES128 trap -> decrypting listener :11222: {len(d.get('varbinds', []))} "
            f"varbinds received (was PASS at 0.4.6/0.4.2)",
        )


async def cross_tsmp_client_to_pysnmp_agent() -> None:
    async with V2cManager(host=AGENT[0], port=AGENT[1], community="public") as mgr:
        r = await mgr.get("1.3.6.1.4.1.99999.1.1.0")
        got = r.varbinds[0].value.value
        nxt = await mgr.get_next("1.3.6.1.4.1.99999.1.1.0")
        walked = await mgr.walk("1.3.6.1.4.1.99999.1")
        ok = got == b"pysnmp-agent 7.1.29 reference for tsmi/tsmp smoke" and len(walked) == 6
        record(
            "xstack-tsmp-to-pysnmp",
            ok,
            f"tsmp V2cManager GET/GETNEXT/WALK vs pysnmp 7.1.29 agent :11163: GET='{got.decode()[:40]}' "
            f"walk(6 scalars)={len(walked)} varbinds, GETNEXT->{nxt.varbinds[0].oid}",
        )


async def cross_pysnmp_client_to_tsmp_responder() -> None:
    from pysnmp.hlapi.asyncio import (
        CommunityData,
        ContextData,
        ObjectType,
        ObjectIdentity,
        SnmpEngine,
        UdpTransportTarget,
        get_cmd,
        next_cmd,
    )

    async with V2cResponder(
        host="127.0.0.1",
        port=11223,
        communities=["public"],
        objects=[
            ((1, 3, 6, 1, 2, 1, 1, 1, 0), OctetStringValue(b"tsmp responder via interop")),
            ((1, 3, 6, 1, 2, 1, 1, 5, 0), OctetStringValue(b"interop-box")),
        ],
    ) as responder:
        serve = asyncio.create_task(responder.serve_forever())
        engine = SnmpEngine()
        ei, es, eidx, vb = await get_cmd(
            engine,
            CommunityData("public"),
            await UdpTransportTarget.create(("127.0.0.1", 11223), timeout=3, retries=0),
            ContextData(),
            ObjectType(ObjectIdentity("1.3.6.1.2.1.1.1.0")),
        )
        get_ok = not ei and not es and str(vb[0][1]) == "tsmp responder via interop"
        ei2, es2, eidx2, vb2 = await next_cmd(
            engine,
            CommunityData("public"),
            await UdpTransportTarget.create(("127.0.0.1", 11223), timeout=3, retries=0),
            ContextData(),
            ObjectType(ObjectIdentity("1.3.6.1.2.1.1.1.0")),
        )
        nxt_ok = not ei2 and not es2
        record(
            "xstack-pysnmp-to-tsmp",
            get_ok and nxt_ok,
            f"pysnmp 7.1.29 getCmd -> tsmp V2cResponder :11223: sysDescr.0='{vb[0][1]}' "
            f"({'match' if get_ok else 'MISMATCH'}) + nextCmd ok={nxt_ok}",
        )
    serve.cancel()


async def cross_pysnmp_trap_to_tsmp_listener() -> None:
    from pysnmp.hlapi.asyncio import (
        CommunityData,
        ContextData,
        NotificationType,
        ObjectIdentity,
        SnmpEngine,
        UdpTransportTarget,
        send_notification,
    )

    async with V2cNotificationListener(host="127.0.0.1", port=11224, communities=["public"]) as listener:
        await send_notification(
            SnmpEngine(),
            CommunityData("public", mpModel=1),
            await UdpTransportTarget.create(("127.0.0.1", 11224), timeout=3, retries=0),
            ContextData(),
            "trap",
            NotificationType(ObjectIdentity("SNMPv2-MIB", "warmStart")),
        )
        event = await asyncio.wait_for(listener.receive(), timeout=3)
        d = event.to_dict()
        ok = len(d.get("varbinds", [])) >= 2
        record(
            "xstack-pysnmp-trap-to-tsmp",
            ok,
            f"pysnmp 7.1.29 v2c trap (warmStart) -> tsmp V2cNotificationListener :11224: "
            f"{len(d.get('varbinds', []))} varbinds decoded (was PASS at 0.4.x)",
        )


async def cross_tsmp_trap_to_pysnmp_receiver() -> None:
    RECV_LOG.parent.mkdir(parents=True, exist_ok=True)
    RECV_LOG.write_text("")  # truncate
    async with V2cNotifier(host="127.0.0.1", port=11180, community="public") as notifier:
        await notifier.send_trap(
            (1, 3, 6, 1, 6, 3, 1, 1, 5, 3),
            varbinds=[((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1), OctetStringValue(b"eth0"))],
            uptime=555,
        )
    line = ""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        content = RECV_LOG.read_text()
        if content.strip():
            line = content.strip().splitlines()[-1]
            break
        await asyncio.sleep(0.1)
    ok = "linkDown" in line or "1.3.6.1.6.3.1.1.5.3" in line
    record(
        "xstack-tsmp-trap-to-pysnmp",
        ok,
        f"tsmp V2cNotifier trap (linkDown+payload) -> pysnmp 7.1.29 NotificationReceiver :11180: "
        f"received={'yes' if line else 'NO'} {line[:100]}",
    )


async def run() -> None:
    await v2c_get_walk()
    await v2c_trap_roundtrip()
    await v3_sha256_aes128_trap()
    await cross_tsmp_client_to_pysnmp_agent()
    await cross_pysnmp_client_to_tsmp_responder()
    await cross_pysnmp_trap_to_tsmp_listener()
    await cross_tsmp_trap_to_pysnmp_receiver()


main(run)
