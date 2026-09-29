"""Reference pysnmp 7.1.29 agent for cross-stack interop + v3 crypto matrix.

Serves on 127.0.0.1:11163:
  - v1/v2c community 'public' GET/GETNEXT/GETBULK (SNMPv2-MIB from pysnmp's
    bundled MIBs: sysName/sysDescr/sysLocation/sysUpTime etc.)
  - v3 authPriv users for the full matrix: {SHA224,SHA256,SHA384,SHA512} x
    {AES128,AES192,AES256,3DES,DES}
  - v1 community 'public-v1' served via the same v1 system

Run detached:  setsid venv/bin/python pysnmp_agent.py </dev/null >agent.log 2>&1 &
Logs one line per request to agent.log.
"""

from pysnmp.entity import engine, config
from pysnmp.entity.rfc3413 import cmdrsp
from pysnmp.carrier.asyncio.dgram import udp

HOST, PORT = "127.0.0.1", 11163

snmpEngine = engine.SnmpEngine()

config.add_transport(
    snmpEngine,
    udp.domainName + (1,),
    udp.UdpTransport().open_server_mode((HOST, PORT)),
)

config.add_v1_system(snmpEngine, "agt", "public")

AUTH = {
    "MD5": config.USM_AUTH_HMAC96_MD5,
    "SHA1": config.USM_AUTH_HMAC96_SHA,
    "SHA224": config.USM_AUTH_HMAC128_SHA224,
    "SHA256": config.USM_AUTH_HMAC192_SHA256,
    "SHA384": config.USM_AUTH_HMAC256_SHA384,
    "SHA512": config.USM_AUTH_HMAC384_SHA512,
}
PRIV = {
    "AES128": config.USM_PRIV_CFB128_AES,
    "AES192": config.USM_PRIV_CFB192_AES,
    "AES256": config.USM_PRIV_CFB256_AES,
    "3DES": config.USM_PRIV_CBC168_3DES,
    "DES": config.USM_PRIV_CBC56_DES,
}
# pysnmp's default AES-192/256 (USM_PRIV_CFB192/256_AES) is Reeder-style;
# the BLUMENTHAL variants are draft-blumenthal-04 / RFC 8963. tsmp 0.6.1
# switched to the blumenthal-04 derivation (net-snmp default), so users are
# registered for BOTH variants to record which one interops.
PRIV_BLUM = {
    "AES192": config.USM_PRIV_CFB192_AES_BLUMENTHAL,
    "AES256": config.USM_PRIV_CFB256_AES_BLUMENTHAL,
}

USERNAMES = []
for a, aproto in AUTH.items():
    for p, pproto in PRIV.items():
        name = f"u_{a.lower()}_{p.lower()}"
        config.add_v3_user(
            snmpEngine,
            name,
            aproto,
            f"authpass-{a.lower()}",
            pproto,
            f"privpass-{p.lower()}",
        )
        config.add_vacm_user(snmpEngine, 3, name, "authPriv", (1, 3, 6, 1), (1, 3, 6, 1))
        USERNAMES.append(name)
for a, aproto in AUTH.items():
    for p, pproto in PRIV_BLUM.items():
        name = f"u_{a.lower()}_{p.lower()}_blum"
        config.add_v3_user(
            snmpEngine,
            name,
            aproto,
            f"authpass-{a.lower()}",
            pproto,
            f"privpass-{p.lower()}",
        )
        config.add_vacm_user(snmpEngine, 3, name, "authPriv", (1, 3, 6, 1), (1, 3, 6, 1))
        USERNAMES.append(name)

config.add_vacm_user(snmpEngine, 2, "agt", "noAuthNoPriv", (1, 3, 6, 1), (1, 3, 6, 1))
config.add_vacm_user(snmpEngine, 1, "agt", "noAuthNoPriv", (1, 3, 6, 1), (1, 3, 6, 1))

snmpContext = cmdrsp.SnmpContext(snmpEngine)
from pysnmp.proto.rfc1902 import OctetString  # noqa: E402

_mib_builder = snmpContext.get_mib_instrum().get_mib_builder()
_mib_builder.load_modules("SNMPv2-MIB")
MibScalar, MibScalarInstance = _mib_builder.import_symbols(
    "SNMPv2-SMI", "MibScalar", "MibScalarInstance"
)

_ROOT = (1, 3, 6, 1, 4, 1, 99999, 1)
_scalars = [
    (1, "pysnmp-agent 7.1.29 reference for tsmi/tsmp smoke"),
    (2, "sysName-slot"),
    (3, "sysLocation-smoke-bench"),
    (4, "walk-a"),
    (5, "walk-b"),
    (6, "walk-c"),
]
_mib_builder.export_symbols(
    "__SMOKE_MIB",
    *(
        obj
        for i, text in _scalars
        for obj in (
            MibScalar(_ROOT + (i,), OctetString()),
            MibScalarInstance(_ROOT + (i,), (0,), OctetString(text)),
        )
    ),
)

cmdrsp.GetCommandResponder(snmpEngine, snmpContext)
cmdrsp.NextCommandResponder(snmpEngine, snmpContext)
cmdrsp.BulkCommandResponder(snmpEngine, snmpContext)

print(f"pysnmp agent up on {HOST}:{PORT}; v3 users: {', '.join(USERNAMES)}", flush=True)

snmpEngine.transport_dispatcher.job_started(1)
try:
    snmpEngine.transport_dispatcher.run_dispatcher()
except Exception:
    import traceback

    traceback.print_exc()
