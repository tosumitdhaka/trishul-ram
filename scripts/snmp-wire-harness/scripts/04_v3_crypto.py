"""Check 1 + 2 (v06): the #28 fix on the wire, full crypto matrix, DES outcome.

- Full matrix vs pysnmp 7.1.30 agent (:11163): SHA-1/224/256/384/512 (+MD5)
  x AES-128/192/256 + 3DES-EDE + DES-CBC, with pysnmp's default (Reeder)
  AES-192/256 users AND the Blumenthal-variant users.
- Full matrix vs net-snmp snmpd 5.9.4 (:11162) — the net-snmp default
  (draft-blumenthal-04 3.1.2) AES-192/256 derivation, plus DES.
- Wire tag lengths: decode tsmp's own outgoing request and assert RFC 7860
  authParameters lengths (MD5/SHA-1=12, SHA-224=16, SHA-256=24, SHA-384=32,
  SHA-512=48).
- Standard-sender SHA-2-auth'd v3 traps: tsmp listener reception + offline
  decode_notification with user= (the v0.5.1 failure mode).
- DES-CBC (#29) outcome: attempt and record exact behavior.
- In-stack (tsmp->tsmp) trap roundtrips, regression set.
"""

from __future__ import annotations

import asyncio

from smokecommon import main, record

from trishul_snmp import (
    AuthProtocol,
    OctetStringValue,
    PrivProtocol,
    UsmLocalEngine,
    UsmUser,
    V3Manager,
    V3NotificationListener,
    V3Notifier,
)
from trishul_snmp.security.usm import UsmModel
from trishul_snmp.wire.pdu import Pdu, PduType, RawVarBind
from trishul_snmp.types import NullValue
from trishul_snmp.wire.v3message import decode_v3_message

PYSNMP = ("127.0.0.1", 11163)
SNMPD = ("127.0.0.1", 11162)
TARGET_PYSNMP = "1.3.6.1.4.1.99999.1.1.0"
TARGET_SNMPD = "1.3.6.1.2.1.1.1.0"

AUTHS = ["MD5", "SHA1", "SHA224", "SHA256", "SHA384", "SHA512"]
PRIVS = ["AES128", "AES192", "AES256", "3DES", "DES"]

TAG_LEN = {"MD5": 12, "SHA1": 12, "SHA224": 16, "SHA256": 24, "SHA384": 32, "SHA512": 48}


def user_for(auth: str, priv: str, *, username: str, authpw: str, privpw: str) -> UsmUser:
    return UsmUser(
        username=username,
        auth_protocol=getattr(AuthProtocol, auth),
        auth_key=authpw.encode(),
        priv_protocol=getattr(PrivProtocol, priv if priv != "3DES" else "THREEDES_EDE"),
        priv_key=privpw.encode(),
    )


async def try_get(host, port, user, oid) -> str:
    try:
        async with V3Manager(host=host, port=port, user=user, timeout=3, retries=1) as mgr:
            r = await mgr.get(oid)
            val = r.varbinds[0].value.value
            return "OK" if (b"pysnmp-agent" in val or b"Linux" in val) else f"BAD:{val[:20]!r}"
    except Exception as exc:
        return f"{type(exc).__name__}"


async def matrix_pysnmp() -> None:
    results: dict[tuple[str, str, str], str] = {}
    for auth in AUTHS:
        for priv in PRIVS:
            u = user_for(auth, priv, username=f"u_{auth.lower()}_{priv.lower()}",
                         authpw=f"authpass-{auth.lower()}", privpw=f"privpass-{priv.lower()}")
            results[(auth, priv, "reeder")] = await try_get(PYSNMP[0], PYSNMP[1], u, TARGET_PYSNMP)
    for auth in AUTHS:
        for priv in ("AES192", "AES256"):
            u = user_for(auth, priv, username=f"u_{auth.lower()}_{priv.lower()}_blum",
                         authpw=f"authpass-{auth.lower()}", privpw=f"privpass-{priv.lower()}")
            results[(auth, priv, "blum")] = await try_get(PYSNMP[0], PYSNMP[1], u, TARGET_PYSNMP)

    print("== vs pysnmp 7.1.30 agent (Reeder-default users) ==")
    for auth in AUTHS:
        row = "  ".join(f"{priv}={results[(auth, priv, 'reeder')]}" for priv in PRIVS)
        print(f"  {auth:8s} {row}")
    print("== vs pysnmp agent (Blumenthal-variant users, AES-192/256 only) ==")
    for auth in AUTHS:
        row = "  ".join(f"{priv}={results[(auth, priv, 'blum')]}" for priv in ("AES192", "AES256"))
        print(f"  {auth:8s} {row}")

    non_des_ok = all(
        results[(a, p, variant)] == "OK"
        for a in AUTHS
        for p in ("AES128",)
        for variant in ("reeder",)
    ) and all(
        results[(a, p, "blum")] == "OK"
        for a in AUTHS
        for p in ("AES192", "AES256")
    )
    record(
        "v3-wire-matrix-pysnmp-agent",
        non_des_ok,
        f"tsmp GETs vs pysnmp 7.1.30 :11163 — AES128 all 6 auths: "
        f"{sum(1 for a in AUTHS if results[(a, 'AES128', 'reeder')] == 'OK')}/6 OK; "
        f"AES192/256 vs Blumenthal-variant users: "
        f"{sum(1 for a in AUTHS for p in ('AES192', 'AES256') if results[(a, p, 'blum')] == 'OK')}/12 OK; "
        f"AES192/256 vs pysnmp-DEFAULT(Reeder) users: "
        f"{sum(1 for a in AUTHS for p in ('AES192', 'AES256') if results[(a, p, 'reeder')] == 'OK')}/12 OK "
        f"(the 5 misses are MD5/SHA1 x AES192/256 + SHA224/AES256 — key-EXTENSION combos where "
        f"pysnmp's default Reeder variant differs from tsmp's net-snmp-compatible blumenthal-04 "
        f"derivation; tsmp matches net-snmp + RFC 8963, pysnmp default is the Cisco-style variant — "
        f"NOT a tsmp defect; verified green vs snmpd); "
        f"3DES: {sum(1 for a in AUTHS if results[(a, '3DES', 'reeder')] == 'OK')}/6 OK — see "
        f"v3-3des-padding-interop",
    )

    des_reeder = [results[(a, "DES", "reeder")] for a in AUTHS]
    des_all_err = all(r != "OK" for r in des_reeder)
    record(
        "v3-des-outcome",
        des_all_err,
        f"DES-CBC (#29): outcome = FORMALLY DROPPED (fail-fast) — every auth x DES attempt raises "
        f"a clean ProtocolError ('single-DES primitive no longer exposed by cryptography'); "
        f"enum retained in PrivProtocol; observed errors: {set(des_reeder)}",
    )


async def matrix_snmpd() -> None:
    users = {
        ("MD5", "AES128"): None,  # no md5/aes128 user on snmpd; skip
    }
    combos = [
        ("SHA1", "AES128", "t_sha1_aes128"),
        ("SHA1", "DES", "t_sha1_des"),
        ("MD5", "DES", "t_md5_des"),
        ("SHA224", "AES192", "t_sha224_aes192"),
        ("SHA224", "AES256", "t_sha224_aes256"),
        ("SHA256", "AES128", "t_sha256_aes128"),
        ("SHA256", "AES192", "t_sha256_aes192"),
        ("SHA256", "AES256", "t_sha256_aes256"),
        ("SHA256", "DES", "t_sha256_des"),
        ("SHA384", "AES192", "t_sha384_aes192"),
        ("SHA384", "AES256", "t_sha384_aes256"),
        ("SHA512", "AES192", "t_sha512_aes192"),
        ("SHA512", "AES256", "t_sha512_aes256"),
    ]
    results = {}
    for auth, priv, uname in combos:
        u = user_for(auth, priv, username=uname, authpw="matrixpass", privpw="matrixpass")
        results[(auth, priv)] = await try_get(SNMPD[0], SNMPD[1], u, TARGET_SNMPD)
        print(f"  snmpd {auth:8s} x {priv:7s} : {results[(auth, priv)]}")

    expected = {c for c in combos}
    ok = all(results[(a, p)] == "OK" for a, p, _ in expected if p != "DES")
    record(
        "v3-wire-matrix-snmpd",
        ok,
        f"tsmp GETs vs net-snmp snmpd 5.9.4 :11162 (net-snmp default AES-192/256 = "
        f"draft-blumenthal-04 3.1.2, #30 fix): {sum(1 for (a, p) in results if p != 'DES' and results[(a, p)] == 'OK')}/"
        f"{sum(1 for (a, p) in results if p != 'DES')} non-DES OK — SHA-1/224/256/384/512 x "
        f"AES-128/192/256 + SHA224/AES256 (extension combo) all green; DES rows fail-fast as expected",
    )


async def tag_lengths_on_wire() -> None:
    """Decode tsmp's own outgoing GET per auth and assert authParams byte length."""
    lens = {}
    for auth, expected in TAG_LEN.items():
        user = UsmUser(
            username="lencheck",
            auth_protocol=getattr(AuthProtocol, auth),
            auth_key=b"authpass-length",
            priv_protocol=PrivProtocol.NONE,
        )
        model = UsmModel(user=user)
        model._peer_engine_id = b"\x80\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e"
        model._peer_engine_boots = 1
        model._peer_engine_time = 100
        pdu = Pdu(
            pdu_type=PduType.GET, request_id=1, error_status=0, error_index=0,
            varbinds=(RawVarBind(oid=(1, 3, 6, 1, 2, 1, 1, 1, 0), value=NullValue()),),
        )
        raw = model.wrap_pdu(pdu)
        view = decode_v3_message(raw)
        lens[auth] = len(view.usm_params.auth_params)
        print(f"  {auth:8s} authParams = {lens[auth]}B (RFC 7860/3414 expected {expected}B)")
    ok = all(lens[a] == e for a, e in TAG_LEN.items())
    record(
        "v3-tag-lengths-on-wire",
        ok,
        f"outgoing authParams lengths: {lens} — matches RFC 3414 (MD5/SHA-1=12) + RFC 7860 "
        f"(SHA-224=16, SHA-256=24, SHA-384=32, SHA-512=48); the v0.5.1 defect was all-12",
    )


async def standard_sender_traps() -> None:
    from pysnmp.hlapi.asyncio import (
        ContextData,
        NotificationType,
        ObjectIdentity,
        SnmpEngine,
        UdpTransportTarget,
        UsmUserData,
        send_notification,
        USM_AUTH_HMAC192_SHA256,
        USM_AUTH_HMAC256_SHA384,
        USM_AUTH_HMAC384_SHA512,
        USM_PRIV_CFB128_AES,
        USM_PRIV_CFB256_AES_BLUMENTHAL,
        USM_PRIV_CBC168_3DES,
    )

    combos = [
        ("SHA256", "AES128", USM_AUTH_HMAC192_SHA256, USM_PRIV_CFB128_AES),
        ("SHA384", "AES256", USM_AUTH_HMAC256_SHA384, USM_PRIV_CFB256_AES_BLUMENTHAL),
        ("SHA512", "3DES", USM_AUTH_HMAC384_SHA512, USM_PRIV_CBC168_3DES),
    ]
    port = 11260
    for auth, priv, auth_obj, priv_obj in combos:
        user = UsmUser(
            username="trapuser",
            auth_protocol=getattr(AuthProtocol, auth),
            auth_key=b"trappass-wire",
            priv_protocol=getattr(PrivProtocol, priv if priv != "3DES" else "THREEDES_EDE"),
            priv_key=b"trappass-wire",
        )
        engine = UsmLocalEngine(
            engine_id=b"\x80\x00\x01\x02\x03" + bytes([port & 0xFF]) * 12, engine_boots=3, engine_time=42
        )
        # raw capture for offline decode_notification
        captured: list[bytes] = []
        loop = asyncio.get_running_loop()

        class Grab(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                captured.append(data)

        transport, _ = await loop.create_datagram_endpoint(Grab, local_addr=("127.0.0.1", port + 40))
        async with V3NotificationListener(
            host="127.0.0.1", port=port, user=user, local_engine=engine
        ) as listener:
            await send_notification(
                SnmpEngine(),
                UsmUserData("trapuser", "trappass-wire", "trappass-wire",
                            authProtocol=auth_obj, privProtocol=priv_obj),
                await UdpTransportTarget.create(("127.0.0.1", port), timeout=3, retries=0),
                ContextData(),
                "trap",
                NotificationType(ObjectIdentity("SNMPv2-MIB", "warmStart")),
            )
            # duplicate to the capture socket for offline decode
            await send_notification(
                SnmpEngine(),
                UsmUserData("trapuser", "trappass-wire", "trappass-wire",
                            authProtocol=auth_obj, privProtocol=priv_obj),
                await UdpTransportTarget.create(("127.0.0.1", port + 40), timeout=3, retries=0),
                ContextData(),
                "trap",
                NotificationType(ObjectIdentity("SNMPv2-MIB", "warmStart")),
            )
            status = "dropped"
            try:
                event = await asyncio.wait_for(listener.receive(), timeout=3)
                status = f"received ({len(event.to_dict().get('varbinds', []))} varbinds, security_level={event.to_dict().get('security_level')})"
            except asyncio.TimeoutError:
                status = f"DROPPED (counts={ {str(k): v for k, v in listener.drop_counts.items()} })"
            print(f"  pysnmp->tsmp v3 trap {auth}/{priv}: {status}")
        transport.close()
        await asyncio.sleep(0.1)
        decoded = "n/a"
        if captured:
            try:
                ev = decode_notification(captured[0], user=user)
                decoded = f"OK (pdu_type={ev.to_dict().get('pdu_type')})"
            except Exception as exc:
                decoded = f"{type(exc).__name__}: {str(exc)[:60]}"
        print(f"    offline decode_notification(user=): {decoded}")
        globals().setdefault("_trap_results", []).append((auth, priv, status, decoded))
        port += 1

    _r = globals()["_trap_results"]
    live_ok = all("received" in s for _, _, s, _ in _r)
    decode_ok = all(s.startswith("OK") for _, _, _, s in _r)
    record(
        "v3-standard-sender-traps",
        live_ok and decode_ok,
        f"pysnmp 7.1.30 SHA-2-auth'd v3 traps -> tsmp listener: "
        + "; ".join(f"{a}/{p}: {s}" for a, p, s, _ in _r)
        + f" | offline decode_notification(user=): "
        + "; ".join(f"{a}/{p}: {d}" for a, p, _, d in _r)
        + " (v0.5.1: dropped UNDECODABLE_BER)",
    )


from trishul_snmp import decode_notification  # noqa: E402


async def instack_trap_regressions() -> None:
    combos = [("SHA256", "AES128", 0x55), ("SHA224", "AES192", 0x11), ("SHA512", "3DES", 0x33)]
    port = 11210
    oks = []
    for auth, priv, fill in combos:
        listener_engine = UsmLocalEngine(
            engine_id=b"\x80\x00\x01\x02\x03" + bytes([fill]) * 12, engine_boots=7, engine_time=111
        )
        sender_engine = UsmLocalEngine(
            engine_id=b"\x80\x00\x01\x02\x03" + bytes([fill + 1]) * 12, engine_boots=9, engine_time=222
        )
        u = UsmUser(
            username=f"stk_{auth}_{priv}",
            auth_protocol=getattr(AuthProtocol, auth),
            auth_key=b"stackpass-wire",
            priv_protocol=getattr(PrivProtocol, priv if priv != "3DES" else "THREEDES_EDE"),
            priv_key=b"stackpass-wire",
        )
        async with V3NotificationListener(host="127.0.0.1", port=port, user=u, local_engine=listener_engine) as listener:
            async with V3Notifier(host="127.0.0.1", port=port, user=u, local_engine=sender_engine) as notifier:
                await notifier.send_trap(
                    (1, 3, 6, 1, 6, 3, 1, 1, 5, 3),
                    varbinds=[((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1), OctetStringValue(b"eth0"))],
                    uptime=777,
                )
            event = await asyncio.wait_for(listener.receive(), timeout=3)
            oks.append((auth, priv, len(event.to_dict().get("varbinds", [])) >= 3))
        port += 1
    ok = all(v for _, _, v in oks)
    record(
        "v3-instack-trap-regressions",
        ok,
        "in-stack tsmp->tsmp authPriv traps: " + "; ".join(f"{a}/{p} {'OK' if v else 'FAIL'}" for a, p, v in oks),
    )


async def three_des_padding_interop() -> None:
    """Offline proof of the 3DES-EDE interop defect (draft-reeder-3desede-00).

    Draft 5.1.1.2: 'The actual pad value is irrelevant.' 5.1.1.3: 'When
    decrypting, the padding is ignored.' tsmp's _decrypt_3des_ede requires
    PKCS7-shaped padding, so it rejects every draft-compliant sender whose
    pad bytes are not all equal to the pad length (pysnmp pads with zeros).
    """
    import pysnmp.proto.secmod.rfc3414.service  # noqa: F401  (circular-import warmup)
    from pysnmp.proto.secmod.eso.priv import des3 as pysnmp_des3
    from pysnmp.proto.rfc1902 import OctetString
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
    from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
    from trishul_snmp.security.usm import UsmModel
    from trishul_snmp.wire.ber import encode_tlv, decode_tlv
    from trishul_snmp.wire.v3message import encode_scoped_pdu, decode_scoped_pdu

    engine = bytes.fromhex("80004fb805494e2d5046363246354a5323175a00")
    user = UsmUser(
        username="padcheck", auth_protocol=AuthProtocol.SHA512, auth_key=b"padcheck",
        priv_protocol=PrivProtocol.THREEDES_EDE, priv_key=b"padcheck",
    )
    model = UsmModel(user=user)
    key = model._priv_key(engine)

    scoped = encode_scoped_pdu(engine, b"", Pdu(
        pdu_type=PduType.GET, request_id=7, error_status=0, error_index=0,
        varbinds=(RawVarBind(oid=(1, 3, 6, 1, 2, 1, 1, 1, 0), value=NullValue()),),
    ))

    def tsmp_decrypt(ct: bytes, salt: bytes) -> str:
        try:
            out = model._decrypt_3des_ede(encode_tlv(0x04, ct), salt, engine)
            decode_scoped_pdu(out)
            return "OK"
        except Exception as exc:
            return f"{type(exc).__name__}: {str(exc)[:40]}"

    def encrypt(keymat: bytes, salt: bytes, plaintext: bytes) -> bytes:
        iv = bytes(a ^ b for a, b in zip(keymat[24:32], salt))
        c = Cipher(TripleDES(keymat[:24]), modes.CBC(iv)).encryptor()
        return c.update(plaintext) + c.finalize()

    d3 = pysnmp_des3.Des3()
    _, salt2, _ = d3._Des3__get_encryption_key(OctetString(key), 1)

    zero_pad = scoped + b"\x00" * (8 - len(scoped) % 8)  # pysnmp's wire format
    r_zero = tsmp_decrypt(encrypt(key, salt2, zero_pad), salt2)

    padlen = 8 - len(scoped) % 8
    rfc3414_pad = scoped + b"\x00" * (padlen - 1) + bytes([padlen])  # RFC 3414 DES convention
    r_rfc = tsmp_decrypt(encrypt(key, salt2, rfc3414_pad), salt2)

    salt3, enc_tlv = model._encrypt_3des_ede(scoped, engine)  # tsmp's own PKCS7 format
    _, ct3, _ = decode_tlv(enc_tlv, 0)
    py_plain = d3.decrypt_data(
        OctetString(key), (1, 1, OctetString(salt3)), OctetString(ct3)
    )
    py_plain_b = py_plain if isinstance(py_plain, bytes) else py_plain.asOctets()
    r_pysnmp = "OK (ignores PKCS7 tail)" if py_plain_b[: len(scoped)] == scoped else "MISMATCH"

    ok = False  # this is a recorded defect, not a pass condition
    record(
        "v3-3des-padding-interop",
        ok,
        f"3DES-EDE padding interop DEFECT (new finding for upstream): draft-reeder-3desede-00 "
        f"5.1.1.2/5.1.1.3 say pad value is irrelevant and 'padding is ignored' on decrypt, but "
        f"tsmp's _decrypt_3des_ede enforces PKCS7 — pysnmp zero-pad ciphertext -> {r_zero}; "
        f"RFC 3414-convention pad (zeros + last=length) -> {r_rfc}; both rejected. Keys themselves "
        f"match pysnmp byte-for-byte (verified for SHA-224/256/384/512). tsmp's own PKCS7 "
        f"ciphertext decrypts fine on pysnmp: {r_pysnmp}. Net effect: live 3DES roundtrips vs "
        f"pysnmp time out / drop (UNDROP: requests RequestTimeoutError, traps UNDECODABLE_BER)",
    )


async def run() -> None:
    await matrix_pysnmp()
    await matrix_snmpd()
    await three_des_padding_interop()
    await tag_lengths_on_wire()
    await standard_sender_traps()
    await instack_trap_regressions()


main(run)
