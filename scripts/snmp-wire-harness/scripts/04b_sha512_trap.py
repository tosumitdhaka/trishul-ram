"""Isolate SHA-512 auth trap reception (non-3DES priv) from the 3DES defect."""
import asyncio
from smokecommon import main, record
from trishul_snmp import AuthProtocol, PrivProtocol, UsmLocalEngine, UsmUser, V3NotificationListener
from pysnmp.hlapi.asyncio import (
    ContextData, NotificationType, ObjectIdentity, SnmpEngine,
    UdpTransportTarget, UsmUserData, send_notification,
    USM_AUTH_HMAC384_SHA512, USM_PRIV_CFB128_AES,
)

async def run() -> None:
    user = UsmUser(
        username="trapuser", auth_protocol=AuthProtocol.SHA512,
        auth_key=b"trappass-wire", priv_protocol=PrivProtocol.AES128,
        priv_key=b"trappass-wire",
    )
    engine = UsmLocalEngine(
        engine_id=b"\x80\x00\x01\x02\x03" + b"\x5a" * 12, engine_boots=3, engine_time=42
    )
    async with V3NotificationListener(host="127.0.0.1", port=11262, user=user, local_engine=engine) as listener:
        await send_notification(
            SnmpEngine(),
            UsmUserData("trapuser", "trappass-wire", "trappass-wire",
                        authProtocol=USM_AUTH_HMAC384_SHA512,
                        privProtocol=USM_PRIV_CFB128_AES),
            await UdpTransportTarget.create(("127.0.0.1", 11262), timeout=3, retries=0),
            ContextData(), "trap",
            NotificationType(ObjectIdentity("SNMPv2-MIB", "warmStart")),
        )
        status = "DROPPED"
        try:
            event = await asyncio.wait_for(listener.receive(), timeout=3)
            d = event.to_dict()
            status = f"received ({len(d.get('varbinds', []))} varbinds, security_level={d.get('security_level')})"
        except asyncio.TimeoutError:
            status = f"DROPPED (counts={listener.drop_counts})"
    record("v3-sha512-aes128-standard-trap", "received" in status,
           f"pysnmp 7.1.30 SHA-512/AES-128 authPriv trap -> tsmp listener :11262: {status}")

main(run)
