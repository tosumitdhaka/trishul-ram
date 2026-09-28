"""Check 6: silent-datagram-drop fix (trishul-snmp #9).

Undecodable/unwanted datagrams to the listeners must surface as drop counters
per reason + on_error callback + rate-limited logging — not silence.
Covers the community listener (v1+v2c) and the v3 listener.
"""

from __future__ import annotations

import asyncio
import logging

from smokecommon import main, record

from trishul_snmp import (
    AuthProtocol,
    PrivProtocol,
    UsmLocalEngine,
    UsmUser,
    V2cNotificationListener,
    V3NotificationListener,
)
from trishul_snmp.notify.v3 import DropReason
from trishul_snmp.wire.message import SnmpMessage, encode_message
from trishul_snmp.wire.pdu import Pdu, PduType, RawVarBind
from trishul_snmp.types import NullValue


def raw_socket_send(port: int, data: bytes) -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(data, ("127.0.0.1", port))


async def community_listener_drops() -> None:
    seen: list[tuple[DropReason, bytes]] = []

    def on_error(reason, addr, data):
        seen.append((reason, data))

    async with V2cNotificationListener(
        host="127.0.0.1", port=11230, communities=["public"], on_error=on_error
    ) as listener:
        # 1. garbage bytes (not BER at all)
        raw_socket_send(11230, b"\x00\x01\x02garbage\xff" * 2)
        # 2. truncated BER header
        raw_socket_send(11230, b"\x30\x82")
        # 3. well-formed community GET request (not a notification)
        get_msg = encode_message(
            SnmpMessage(
                version=1,
                community="public",
                pdu=Pdu(
                    pdu_type=PduType.GET,
                    request_id=1,
                    error_status=0,
                    error_index=0,
                    varbinds=(RawVarBind(oid=(1, 3, 6, 1, 2, 1, 1, 1, 0), value=NullValue()),),
                ),
            )
        )
        raw_socket_send(11230, get_msg)
        # 4. valid trap with wrong community
        from trishul_snmp import OctetStringValue, V2cNotifier

        async with V2cNotifier(host="127.0.0.1", port=11230, community="secret") as n:
            await n.send_trap((1, 3, 6, 1, 6, 3, 1, 1, 5, 3))

        # drops are accounted inside receive(); all four datagrams are undeliverable
        # so receive() should keep discarding them and then time out
        try:
            await asyncio.wait_for(listener.receive(), timeout=1.0)
            timeout_hit = False
        except asyncio.TimeoutError:
            timeout_hit = True
        await asyncio.sleep(0.2)

    dropped = listener.dropped
    counts = {str(k): v for k, v in listener.drop_counts.items()}
    print(f"community listener: dropped={dropped}, counts={counts}, on_error saw {len(seen)}, timeout={timeout_hit}")
    ok = (
        dropped >= 4
        and len(seen) == dropped
        and timeout_hit
        and "UNDECODABLE" in str(list(listener.drop_counts.keys())).upper()
        and "NOT_NOTIFICATION" in str(list(listener.drop_counts.keys())).upper()
        and "WRONG_COMMUNITY" in str(list(listener.drop_counts.keys())).upper()
    )
    record(
        "silent-drop-community-listener",
        ok,
        f"V2cNotificationListener :11230 — dropped={dropped} (garbage x2, well-formed GET, "
        f"wrong-community trap), drop_counts={counts}, on_error callback fired {len(seen)}x "
        f"(0.4.2 behavior: silent, zero signal)",
    )


async def v3_listener_drops() -> None:
    seen: list[DropReason] = []

    def on_error(reason, addr, data):
        seen.append(reason)

    user = UsmUser(
        username="v3drop",
        auth_protocol=AuthProtocol.SHA256,
        auth_key=b"authpass-sha256",
        priv_protocol=PrivProtocol.AES128,
        priv_key=b"privpass-aes128",
    )
    engine = UsmLocalEngine(engine_id=b"\x80\x00\x01\x02\x03" + bytes([0x55]) * 12, engine_boots=1, engine_time=1)
    async with V3NotificationListener(
        host="127.0.0.1", port=11231, user=user, local_engine=engine, on_error=on_error
    ) as listener:
        raw_socket_send(11231, b"\xff" * 40)  # garbage
        raw_socket_send(11231, b"\x30\x82\x00\x01\x02")  # truncated/malformed
        try:
            await asyncio.wait_for(listener.receive(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0.2)

    dropped = listener.dropped
    counts = {str(k): v for k, v in listener.drop_counts.items()}
    print(f"v3 listener: dropped={dropped}, counts={counts}, on_error saw {len(seen)}")
    ok = dropped >= 1 and len(seen) >= 1
    record(
        "silent-drop-v3-listener",
        ok,
        f"V3NotificationListener :11231 — garbage datagrams: dropped={dropped}, "
        f"drop_counts={counts}, on_error fired {len(seen)}x",
    )


async def run() -> None:
    logging.basicConfig(level=logging.WARNING)
    await community_listener_drops()
    await v3_listener_drops()


main(run)
