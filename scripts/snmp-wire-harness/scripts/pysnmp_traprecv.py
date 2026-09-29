"""pysnmp 7.1.29 notification receiver for cross-stack trap interop.

Listens for v1/v2c/v3 traps on 127.0.0.1:11180 and appends one line per
received notification to logs/pysnmp-traprecv.log.

Run detached: setsid venv/bin/python pysnmp_traprecv.py </dev/null >log 2>&1 &
"""

from pysnmp.entity import engine, config
from pysnmp.entity.rfc3413 import ntfrcv
from pysnmp.carrier.asyncio.dgram import udp

LOG = "/tmp/opencode/tsmi-smoke-v06/logs/pysnmp-traprecv.log"
HOST, PORT = "127.0.0.1", 11180

snmpEngine = engine.SnmpEngine()

config.add_transport(
    snmpEngine,
    udp.domainName + (1,),
    udp.UdpTransport().open_server_mode((HOST, PORT)),
)
config.add_v1_system(snmpEngine, "rcv", "public")
config.add_vacm_user(snmpEngine, 2, "rcv", "noAuthNoPriv", (1, 3, 6, 1), (1, 3, 6, 1))
config.add_vacm_user(snmpEngine, 1, "rcv", "noAuthNoPriv", (1, 3, 6, 1), (1, 3, 6, 1))


def cb_compat(snmpEngine, stateReference, *args):
    varBinds = args[-2]
    parts = []
    for oid, val in varBinds:
        parts.append(f"{oid.prettyPrint()}={val.prettyPrint()}")
    with open(LOG, "a") as fh:
        fh.write(";".join(parts) + "\n")


ntfrcv.NotificationReceiver(snmpEngine, cb_compat)

print(f"pysnmp trap receiver up on {HOST}:{PORT}", flush=True)

snmpEngine.transport_dispatcher.job_started(1)
try:
    snmpEngine.transport_dispatcher.run_dispatcher()
except Exception:
    import traceback

    traceback.print_exc()
