#!/usr/bin/env python3
"""Scaled synthetic SNMP responder for the SNMP-source bench scenarios.

A standalone pysnmp agent (adapted from
``scripts/snmp-wire-harness/scripts/pysnmp_agent.py``) that serves a
synthetic table of ``--rows`` x 8 columns of mixed types under a private
MIB area ``1.3.6.1.4.1.99999.2.1``.

Like the wire harness agent, the table is served in-process with untyped
OID instances — no MIB files are compiled or loaded. Column layout:

    .1 OctetString   msisdn          ("81" + 10 digits)
    .2 Integer32     cell_id         0-65535
    .3 Counter32     bytes_up        0-2,000,000
    .4 Counter32     bytes_down      0-8,000,000
    .5 Gauge32       charge_amount   JPY x100
    .6 TimeTicks     duration_s      0-3600
    .7 OctetString   rat             "4G" | "5G" | "NR_SA"
    .8 OctetString   event_type      VOICE | SMS | DATA | ROAMING | EVENT

Serves v1/v2c community ``public`` GET / GETNEXT / GETBULK. Runs in the
foreground; run detached with setsid (see README.md):

    setsid .venv/bin/python generators/snmp_responder_scaled.py \\
        --rows 1000 </dev/null >/tmp/snmp_responder.log 2>&1 &
"""

from __future__ import annotations

import argparse
import random

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import cmdrsp
from pysnmp.proto.rfc1902 import (
    Counter32,
    Gauge32,
    Integer32,
    OctetString,
    TimeTicks,
)

HOST = "127.0.0.1"

# pysnmp >=7.1 deprecated ``domainName`` in favour of ``DOMAIN_NAME``.
_UDP_DOMAIN = udp.DOMAIN_NAME if hasattr(udp, "DOMAIN_NAME") else udp.domainName

# Private enterprise subtree: trishul-perf synthetic table.
ROOT = (1, 3, 6, 1, 4, 1, 99999, 2, 1)

EVENT_TYPES = ("VOICE", "SMS", "DATA", "ROAMING", "EVENT")
RATS = ("2G", "3G", "4G", "5G", "NR_SA")


def _column_syntax(column: int):
    """pysnmp MIB scalar syntax object per table column."""
    return {
        1: OctetString(),
        2: Integer32(),
        3: Counter32(),
        4: Counter32(),
        5: Gauge32(),
        6: TimeTicks(),
        7: OctetString(),
        8: OctetString(),
    }[column]


def _column_value(column: int, row: int, rng: random.Random):
    """Deterministic cell value for (column, row)."""
    if column == 1:
        return OctetString(f"81{rng.randint(0, 10**10 - 1):010d}")
    if column == 2:
        return Integer32(rng.randint(0, 65_535))
    if column == 3:
        return Counter32(rng.randint(0, 2_000_000))
    if column == 4:
        return Counter32(rng.randint(0, 8_000_000))
    if column == 5:
        return Gauge32(rng.randint(0, 100_000))
    if column == 6:
        return TimeTicks(rng.randint(0, 3600))
    if column == 7:
        return OctetString(rng.choice(RATS))
    return OctetString(rng.choice(EVENT_TYPES))


def build_table(rows: int, seed: int):
    """Export the synthetic table into pysnmp's in-process MIB builder."""
    snmp_context = cmdrsp.SnmpContext(_ENGINE)
    mib_builder = snmp_context.get_mib_instrum().get_mib_builder()
    MibScalar, MibScalarInstance = mib_builder.import_symbols(
        "SNMPv2-SMI", "MibScalar", "MibScalarInstance"
    )

    rng = random.Random(seed)
    exports: list = []
    for column in range(1, 9):
        exports.append(MibScalar(ROOT + (column,), _column_syntax(column)))
        for row in range(1, rows + 1):
            exports.append(
                MibScalarInstance(ROOT + (column,), (row,), _column_value(column, row, rng))
            )
    mib_builder.export_symbols("__PERF_SNMP_MIB", *exports)
    return snmp_context


_ENGINE = engine.SnmpEngine()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scaled synthetic SNMP table responder for bench scenarios."
    )
    parser.add_argument("--rows", type=int, default=1000, help="Table rows (default 1000)")
    parser.add_argument("--port", type=int, default=11161, help="UDP port (default 11161)")
    parser.add_argument("--host", default=HOST, help="Bind address (default 127.0.0.1)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (default 42)")
    args = parser.parse_args()

    if args.rows < 1:
        parser.error("--rows must be >= 1")

    config.add_transport(
        _ENGINE,
        _UDP_DOMAIN + (1,),
        udp.UdpTransport().open_server_mode((args.host, args.port)),
    )

    # v1/v2c community 'public' + VACM access over the whole MIB tree.
    config.add_v1_system(_ENGINE, "perf-agent", "public")
    config.add_vacm_user(_ENGINE, 2, "perf-agent", "noAuthNoPriv", (1, 3, 6, 1), (1, 3, 6, 1))
    config.add_vacm_user(_ENGINE, 1, "perf-agent", "noAuthNoPriv", (1, 3, 6, 1), (1, 3, 6, 1))

    snmp_context = build_table(args.rows, args.seed)

    cmdrsp.GetCommandResponder(_ENGINE, snmp_context)
    cmdrsp.NextCommandResponder(_ENGINE, snmp_context)
    cmdrsp.BulkCommandResponder(_ENGINE, snmp_context)

    print(
        f"snmp_responder_scaled: {args.rows}x8 synthetic table up on "
        f"{args.host}:{args.port} (community 'public', MIB area 1.3.6.1.4.1.99999.2.1)",
        flush=True,
    )

    _ENGINE.transport_dispatcher.job_started(1)
    try:
        _ENGINE.transport_dispatcher.run_dispatcher()
    except KeyboardInterrupt:
        print("snmp_responder_scaled: stopping", flush=True)
    except Exception:
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()