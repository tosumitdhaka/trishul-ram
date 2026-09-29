"""v1.5.0 layer 4 (GH #72) — env-gated cross-stack wire suite.

TRAM's tsnmp connector paths driven against **in-process pysnmp peers** — an
independent SNMP stack on the other end of the wire (the whole point: tsnmp
responsers could mask a shared defect, a pysnmp peer cannot).

Gated on ``TRAM_TEST_SNMP_WIRE=1`` — the default suite skips this module
cleanly (it needs both stacks installed and binds loopback UDP). The peers
are adapted from the wire-harness reference implementations
(``scripts/snmp-wire-harness/scripts/pysnmp_agent.py`` + ``pysnmp_traprecv.py``)
into fixtures; every port is ephemeral (port-0 binding), so no fixed 1116x
ports and no cross-test collisions.

Coverage:

* poll source (tsnmp) GET + GETNEXT-walk against the pysnmp agent for v1,
  v2c, v3 authPriv (SHA-256/AES-128) plus the ``*_BLUMENTHAL`` user for
  AES-256 (the AES-192/256 key-extension-derivation nuance).
* trap sink (tsnmp ``_send_trap_tsnmp``) → pysnmp notification receiver:
  v1 + v2c + v3 (v3 requires pre-seeding the receiver with the tsnmp sink's
  deterministic authoritative engine id — pysnmp's USM drops traps from
  unknown engines, a pysnmp receiver constraint, not a tsnmp defect).
* trap source (tsnmp listener) ← pysnmp sender: v2c + v3 SHA-256/AES-128
  (the #28-defect-class catcher: a standard SHA-2 trap must be received and
  decoded, exercising real encode → wire → decode in both directions).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("TRAM_TEST_SNMP_WIRE") != "1",
    reason="cross-stack wire suite gated — set TRAM_TEST_SNMP_WIRE=1 to run",
)


@pytest.fixture(autouse=True)
def _tsnmp_stack():
    """This suite exercises TRAM's tsnmp connector paths — pin the flag on.

    Without it a connector would silently fall back to the legacy pysnmp
    path and the tests would pass for the wrong reason (pysnmp client →
    pysnmp peer).
    """
    os.environ["TRAM_SNMP_STACK"] = "trishul"
    yield
    os.environ.pop("TRAM_SNMP_STACK", None)


def _free_port() -> int:
    """Allocate an ephemeral UDP port (used exactly once per fixture)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _capture_datagram(send_to_port) -> bytes:
    """Bind a raw UDP capture socket, run *send_to_port* against it, return the datagram.

    Used for wire-level assertions (e.g. the v1 Trap-PDU enterprise/specific
    fields) where a receiver's callback only exposes decoded varbinds.
    """
    captured: list[bytes] = []
    port = _free_port()

    class _Grab(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            captured.append(data)

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _grab():
            transport, _ = await loop.create_datagram_endpoint(
                _Grab, local_addr=("127.0.0.1", port)
            )
            try:
                await asyncio.sleep(2.0)
            finally:
                transport.close()

        loop.run_until_complete(_grab())
        loop.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    time.sleep(0.3)  # let the capture endpoint bind before the sender fires
    send_to_port(port)
    deadline = time.monotonic() + 3.0
    while not captured and time.monotonic() < deadline:
        time.sleep(0.05)
    assert captured, "no datagram captured"
    return captured[0]


# ── in-process pysnmp agent (reference: scripts/.../pysnmp_agent.py) ────────


class PysnmpAgent:
    """v1/v2c community + v3 authPriv agent on its own event loop + thread."""

    ROOT = (1, 3, 6, 1, 4, 1, 99999, 1)
    SCALARS = {
        1: "pysnmp-agent",
        2: "walk-a",
        3: "walk-b",
    }

    def __init__(self) -> None:
        self.port = _free_port()
        self._ready = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._engine = None

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        from pysnmp.carrier.asyncio.dgram import udp
        from pysnmp.entity import config, engine
        from pysnmp.entity.rfc3413 import cmdrsp
        from pysnmp.proto.rfc1902 import OctetString

        snmp_engine = engine.SnmpEngine()
        config.add_transport(
            snmp_engine,
            udp.DOMAIN_NAME + (1,),
            udp.UdpTransport().open_server_mode(("127.0.0.1", self.port)),
        )
        config.add_v1_system(snmp_engine, "agt", "public")
        auth_protos = {
            "u_sha256_aes128": config.USM_AUTH_HMAC192_SHA256,
            "u_sha256_aes256_blum": config.USM_AUTH_HMAC192_SHA256,
            "u_sha256_3des": config.USM_AUTH_HMAC192_SHA256,
        }
        priv_protos = {
            "u_sha256_aes128": config.USM_PRIV_CFB128_AES,
            # draft-blumenthal-04 / RFC 8963 derivation — what tsnmp matches
            # (pysnmp's default USM_PRIV_CFB256_AES is the Cisco/Reeder variant).
            "u_sha256_aes256_blum": config.USM_PRIV_CFB256_AES_BLUMENTHAL,
            # 3DES-EDE (v1.5.1 re-support): pysnmp's USM_PRIV_CBC168_3DES uses
            # the Blumenthal derivation — tsnmp's THREEDES_EDE matches it.
            "u_sha256_3des": config.USM_PRIV_CBC168_3DES,
        }
        for name, a_key, p_key in (
            ("u_sha256_aes128", "authpass", "privpass"),
            ("u_sha256_aes256_blum", "authpass", "privpass"),
            ("u_sha256_3des", "authpass", "privpass"),
        ):
            config.add_v3_user(snmp_engine, name, auth_protos[name], a_key, priv_protos[name], p_key)
            config.add_vacm_user(snmp_engine, 3, name, "authPriv", (1, 3, 6, 1), (1, 3, 6, 1))
        config.add_vacm_user(snmp_engine, 2, "agt", "noAuthNoPriv", (1, 3, 6, 1), (1, 3, 6, 1))
        config.add_vacm_user(snmp_engine, 1, "agt", "noAuthNoPriv", (1, 3, 6, 1), (1, 3, 6, 1))

        ctx = cmdrsp.SnmpContext(snmp_engine)
        mib_builder = ctx.get_mib_instrum().get_mib_builder()
        mib_builder.load_modules("SNMPv2-MIB")
        MibScalar, MibScalarInstance = mib_builder.import_symbols(
            "SNMPv2-SMI", "MibScalar", "MibScalarInstance"
        )
        for index, text in self.SCALARS.items():
            mib_builder.export_symbols(
                f"__SMOKE_MIB_{self.port}",
                MibScalar(self.ROOT + (index,), OctetString()),
                MibScalarInstance(self.ROOT + (index,), (0,), OctetString(text)),
            )
        cmdrsp.GetCommandResponder(snmp_engine, ctx)
        cmdrsp.NextCommandResponder(snmp_engine, ctx)
        cmdrsp.BulkCommandResponder(snmp_engine, ctx)
        snmp_engine.transport_dispatcher.job_started(1)
        self._engine = snmp_engine
        self._ready.set()
        snmp_engine.transport_dispatcher.run_dispatcher()

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(10), "pysnmp agent did not start"
        time.sleep(0.2)

    def stop(self) -> None:
        if self._engine is not None:
            self._loop.call_soon_threadsafe(self._engine.transport_dispatcher.close_dispatcher)
            self._thread.join(timeout=10)
            # drain the loop so asyncio does not warn about pending tasks
            try:
                self._loop.run_until_complete(asyncio.sleep(0.01))
            except RuntimeError:
                pass


# ── in-process pysnmp notification receiver (reference: pysnmp_traprecv.py) ─


class PysnmpTrapReceiver:
    """v1/v2c/v3 notification receiver capturing decoded varbind lists."""

    def __init__(self, v3_sink_seed: str | None = None) -> None:
        """``v3_sink_seed`` is a ``build_tsnmp_local_engine`` seed template with
        a ``{port}`` placeholder, filled from this receiver's own port.

        The resulting engine id pre-seeds the USM cache for the tsnmp sink's
        deterministic authoritative engine (pysnmp drops v3 traps from
        unknown engines — a pysnmp receiver constraint, not a tsnmp defect).
        """
        self.port = _free_port()
        self.received: list[list[tuple[str, str]]] = []
        self._v3_user: tuple[str, str, str, object, object, bytes] | None = None
        if v3_sink_seed is not None:
            from pysnmp.entity import config as pysnmp_config

            from tram.connectors.snmp.mib_utils import build_tsnmp_local_engine

            engine_id = build_tsnmp_local_engine(
                v3_sink_seed.format(port=self.port)
            ).engine_id
            self._v3_user = (
                "trapuser",
                "authpass",
                "privpass",
                pysnmp_config.USM_AUTH_HMAC192_SHA256,
                pysnmp_config.USM_PRIV_CFB128_AES,
                engine_id,
            )
        self._ready = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._engine = None

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        from pysnmp.carrier.asyncio.dgram import udp
        from pysnmp.entity import config, engine
        from pysnmp.entity.rfc3413 import ntfrcv

        snmp_engine = engine.SnmpEngine()
        config.add_transport(
            snmp_engine,
            udp.DOMAIN_NAME + (1,),
            udp.UdpTransport().open_server_mode(("127.0.0.1", self.port)),
        )
        config.add_v1_system(snmp_engine, "rcv", "public")
        config.add_vacm_user(snmp_engine, 2, "rcv", "noAuthNoPriv", (1, 3, 6, 1), (1, 3, 6, 1))
        config.add_vacm_user(snmp_engine, 1, "rcv", "noAuthNoPriv", (1, 3, 6, 1), (1, 3, 6, 1))
        if self._v3_user is not None:
            name, authpass, privpass, a_proto, p_proto, engine_id = self._v3_user
            config.add_v3_user(
                snmp_engine, name, a_proto, authpass, p_proto, privpass,
                securityEngineId=engine_id,
            )
            config.add_vacm_user(snmp_engine, 3, name, "authPriv", (1, 3, 6, 1), (1, 3, 6, 1))

        def _cb(snmp_engine, state_reference, *args):
            var_binds = args[-2]
            self.received.append(
                [(oid.prettyPrint(), val.prettyPrint()) for oid, val in var_binds]
            )

        ntfrcv.NotificationReceiver(snmp_engine, _cb)
        snmp_engine.transport_dispatcher.job_started(1)
        self._engine = snmp_engine
        self._ready.set()
        snmp_engine.transport_dispatcher.run_dispatcher()

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(10), "pysnmp trap receiver did not start"
        time.sleep(0.2)

    def stop(self) -> None:
        if self._engine is not None:
            self._loop.call_soon_threadsafe(self._engine.transport_dispatcher.close_dispatcher)
            self._thread.join(timeout=10)
            try:
                self._loop.run_until_complete(asyncio.sleep(0.01))
            except RuntimeError:
                pass

    def wait_for_trap(self, timeout: float = 5.0) -> list[tuple[str, str]]:
        """Block until one trap is decoded, then return its varbind list."""
        deadline = time.monotonic() + timeout
        while not self.received and time.monotonic() < deadline:
            time.sleep(0.05)
        assert self.received, "pysnmp trap receiver captured no trap"
        return self.received.pop(0)


# ── module-scoped fixtures ───────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pysnmp_agent():
    agent = PysnmpAgent()
    agent.start()
    yield agent
    agent.stop()


@pytest.fixture(scope="module")
def pysnmp_trap_receiver():
    receiver = PysnmpTrapReceiver(v3_sink_seed="tram:sink:127.0.0.1:{port}:trapuser")
    receiver.start()
    yield receiver
    receiver.stop()


# ── poll source (tsnmp) vs pysnmp agent ──────────────────────────────────────


class TestPollSourceVsPysnmpAgent:
    """tsnmp GET/GETNEXT-walk against the independent pysnmp agent."""

    _OID = "1.3.6.1.4.1.99999.1.1.0"

    @pytest.mark.parametrize(
        ("version", "auth"),
        [
            ("1", {"community": "public"}),
            ("2c", {"community": "public"}),
            ("3", {
                "security_name": "u_sha256_aes128",
                "auth_protocol": "SHA256", "auth_key": "authpass",
                "priv_protocol": "AES128", "priv_key": "privpass",
            }),
            # AES-256 via the draft-blumenthal-04 variant user (tsnmp matches
            # net-snmp; pysnmp's default Reeder derivation differs for
            # AES-192/256 key extension).
            ("3", {
                "security_name": "u_sha256_aes256_blum",
                "auth_protocol": "SHA256", "auth_key": "authpass",
                "priv_protocol": "AES256", "priv_key": "privpass",
            }),
            # 3DES-EDE (v1.5.1): tsnmp's THREEDES_EDE vs pysnmp's
            # USM_PRIV_CBC168_3DES — the #31 padding-interop wire proof.
            ("3", {
                "security_name": "u_sha256_3des",
                "auth_protocol": "SHA256", "auth_key": "authpass",
                "priv_protocol": "3DES", "priv_key": "privpass",
            }),
        ],
        ids=["v1", "v2c", "v3-sha256-aes128", "v3-sha256-aes256-blumenthal", "v3-sha256-3des"],
    )
    def test_get(self, pysnmp_agent, version, auth):
        from tram.connectors.snmp.source import SNMPPollSource

        src = SNMPPollSource({
            "host": "127.0.0.1", "port": pysnmp_agent.port,
            "oids": [self._OID], "operation": "get",
            "version": version, "timeout": 3.0, "retries": 3, **auth,
        })
        payload, meta = next(iter(src.read()))
        data = json.loads(payload)
        assert data[self._OID] == "pysnmp-agent"
        assert meta["source_host"] == "127.0.0.1"

    def test_legacy_3des_get_against_pysnmp_agent(self, pysnmp_agent, monkeypatch):
        """Legacy (pysnmp) stack: restored 3DES-EDE GET roundtrip (v1.5.1).

        This suite's autouse fixture pins the tsnmp flag; monkeypatch flips
        the SOURCE instance to the legacy path — an in-stack pysnmp client →
        pysnmp peer 3DES roundtrip, covering the legacy stack's restored
        3DES that the tsnmp-flag cases don't exercise.
        """
        monkeypatch.delenv("TRAM_SNMP_STACK", raising=False)  # legacy path
        from tram.connectors.snmp.source import SNMPPollSource

        src = SNMPPollSource({
            "host": "127.0.0.1", "port": pysnmp_agent.port,
            "oids": [self._OID], "operation": "get",
            "version": "3", "timeout": 3.0, "retries": 3,
            "security_name": "u_sha256_3des",
            "auth_protocol": "SHA256", "auth_key": "authpass",
            "priv_protocol": "3DES", "priv_key": "privpass",
        })
        payload, meta = next(iter(src.read()))
        data = json.loads(payload)
        assert data[self._OID] == "pysnmp-agent"
        assert meta["source_host"] == "127.0.0.1"

    @pytest.mark.parametrize(
        ("version", "auth"),
        [
            ("1", {"community": "public"}),
            ("2c", {"community": "public"}),
            ("3", {
                "security_name": "u_sha256_aes128",
                "auth_protocol": "SHA256", "auth_key": "authpass",
                "priv_protocol": "AES128", "priv_key": "privpass",
            }),
        ],
        ids=["v1", "v2c", "v3-sha256-aes128"],
    )
    def test_walk_getnext_loop(self, pysnmp_agent, version, auth):
        """tsnmp WALK (GETNEXT loop) collects the smoke scalars from the agent."""
        from tram.connectors.snmp.source import SNMPPollSource

        src = SNMPPollSource({
            "host": "127.0.0.1", "port": pysnmp_agent.port,
            "oids": ["1.3.6.1.4.1.99999.1"], "operation": "walk",
            "version": version, "timeout": 3.0, "retries": 3, **auth,
        })
        payload, _ = next(iter(src.read()))
        data = json.loads(payload)
        assert data.get("1.3.6.1.4.1.99999.1.1.0") == "pysnmp-agent"
        assert data.get("1.3.6.1.4.1.99999.1.2.0") == "walk-a"
        assert data.get("1.3.6.1.4.1.99999.1.3.0") == "walk-b"


# ── trap sink (tsnmp) → pysnmp trap receiver ─────────────────────────────────


class TestTrapSinkToPysnmpReceiver:
    """tsnmp _send_trap_tsnmp traps decoded by the independent pysnmp receiver."""

    @pytest.mark.parametrize(
        ("version", "auth"),
        [
            ("1", {"community": "public", "trap_oid": "1.3.6.1.4.1.99999"}),
            ("2c", {"community": "public", "trap_oid": "1.3.6.1.4.1.99999"}),
            ("3", {
                "security_name": "trapuser",
                "auth_protocol": "SHA256", "auth_key": "authpass",
                "priv_protocol": "AES128", "priv_key": "privpass",
            }),
        ],
        ids=["v1", "v2c", "v3-sha256-aes128"],
    )
    def test_send_trap(self, pysnmp_trap_receiver, version, auth):
        from tram.connectors.snmp.sink import SNMPTrapSink

        sink = SNMPTrapSink({
            "host": "127.0.0.1", "port": pysnmp_trap_receiver.port,
            "version": version, "timeout": 1.0, "retries": 1, **auth,
        })
        sink.write(json.dumps({"1.3.6.1.4.1.99999.1.0": f"alarm-{version}"}).encode(), {})
        varbinds = pysnmp_trap_receiver.wait_for_trap()
        pairs = dict(varbinds)
        assert pairs.get("1.3.6.1.4.1.99999.1.0") == f"alarm-{version}"
        # the notifier auto-built the sysUpTime.0 + snmpTrapOID.0 varbinds
        assert "1.3.6.1.2.1.1.3.0" in pairs
        assert "1.3.6.1.6.3.1.1.4.1.0" in pairs

    def test_v1_send_trap_wire_enterprise_specific(self):
        """Review BUG 1 — wire level: the v1 Trap-PDU carries the RFC 2576
        §3.2 enterprise/specific split (enterprise ``1.3.6.1.4.1``, specific
        99999, generic 6), NOT the raw trap OID as the enterprise with
        specific=0 (which would mis-encode ``enterprise.specific``)."""
        from pyasn1.codec.ber import decoder as ber_decoder
        from pysnmp.proto.api import v1 as v1api

        from tram.connectors.snmp.sink import SNMPTrapSink

        def _send(port):
            SNMPTrapSink({
                "host": "127.0.0.1", "port": port, "version": "1",
                "trap_oid": "1.3.6.1.4.1.99999", "community": "public",
                "timeout": 1.0, "retries": 1,
            }).write(json.dumps({"1.3.6.1.4.1.99999.1.0": "wire-v1"}).encode(), {})

        raw = _capture_datagram(_send)
        msg, _ = ber_decoder.decode(raw, asn1Spec=v1api.Message())
        pdu = v1api.apiMessage.get_pdu(msg)
        assert tuple(v1api.apiTrapPDU.get_enterprise(pdu)) == (1, 3, 6, 1, 4, 1)
        assert v1api.apiTrapPDU.get_generic_trap(pdu) == 6
        assert v1api.apiTrapPDU.get_specific_trap(pdu) == 99999
        # RFC 2576 reconstruction enterprise.specific == the configured trap OID
        enterprise = tuple(v1api.apiTrapPDU.get_enterprise(pdu))
        specific = v1api.apiTrapPDU.get_specific_trap(pdu)
        assert enterprise + (specific,) == (1, 3, 6, 1, 4, 1, 99999)
        varbind_oids = [tuple(vb[0]) for vb in v1api.apiTrapPDU.get_varbinds(pdu)]
        assert (1, 3, 6, 1, 4, 1, 99999, 1, 0) in varbind_oids


# ── trap source (tsnmp listener) ← pysnmp sender ─────────────────────────────


class _PysnmpTrapSender:
    """Sends one trap from its own event loop (pysnmp hlapi sender)."""

    def __init__(self, port: int, version: str = "2c") -> None:
        self.port = port
        self.version = version

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        asyncio.run(self._send())

    async def _send(self) -> None:
        from pysnmp.hlapi.asyncio import (
            CommunityData,
            ContextData,
            NotificationType,
            ObjectIdentity,
            ObjectType,
            OctetString,
            SnmpEngine,
            UdpTransportTarget,
            send_notification,
        )
        if self.version == "3":
            from pysnmp.hlapi.asyncio import (
                USM_AUTH_HMAC192_SHA256,
                USM_PRIV_CFB128_AES,
                UsmUserData,
            )
            auth = UsmUserData(
                "trapuser", "authpass", "privpass",
                authProtocol=USM_AUTH_HMAC192_SHA256, privProtocol=USM_PRIV_CFB128_AES,
            )
        else:
            auth = CommunityData("public", mpModel=1)
        await send_notification(
            SnmpEngine(),
            auth,
            await UdpTransportTarget.create(("127.0.0.1", self.port), timeout=2, retries=0),
            ContextData(),
            "trap",
            NotificationType(ObjectIdentity("SNMPv2-MIB", "warmStart")).add_varbinds(
                ObjectType(ObjectIdentity("1.3.6.1.4.1.99999.1.0"), OctetString("wire-alarm"))
            ),
        )


class TestTrapSourceFromPysnmpSender:
    """tsnmp trap listener receives + decodes standard pysnmp traps (the
    #28-defect-class catcher — a standard SHA-2 v3 trap must decode)."""

    @pytest.mark.parametrize("version", ["2c", "3"], ids=["v2c", "v3-sha256-aes128"])
    def test_receives_standard_trap(self, version):
        os.environ["TRAM_SNMP_STACK"] = "trishul"
        from tram.connectors.snmp.source import SNMPTrapSource

        port = _free_port()
        config: dict = {"host": "127.0.0.1", "port": port, "version": version}
        if version == "3":
            config.update({
                "security_name": "trapuser",
                "auth_protocol": "SHA256", "auth_key": "authpass",
                "priv_protocol": "AES128", "priv_key": "privpass",
            })
        src = SNMPTrapSource(config)
        stream = src.read()
        _PysnmpTrapSender(port, version=version).start()
        payload, meta = next(stream)
        src.stop()
        list(stream)

        data = json.loads(payload)
        assert data["1.3.6.1.4.1.99999.1.0"] == "wire-alarm"
        assert data["1.3.6.1.6.3.1.1.4.1.0"] == "1.3.6.1.6.3.1.1.5.2"  # warmStart
        assert meta["source_ip"] == "127.0.0.1"
        assert meta["version"] == version