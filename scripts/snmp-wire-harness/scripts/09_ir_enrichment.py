"""Check 3 (v06 extra): tsmi 0.5.2 IR enrichment (enums/units) + tsmp 0.6.1
rendering fidelity — additive schema 1.1, consumers unaffected."""

from __future__ import annotations

import json
from pathlib import Path

from smokecommon import main, record

OUT = Path(__file__).resolve().parent.parent / "mibs-out"


async def run() -> None:
    ifmib = json.loads((OUT / "IF-MIB.json").read_text())
    objs = ifmib.get("objects", {})
    ifoperstatus = objs.get("ifOperStatus", {})
    enums = ifoperstatus.get("enums")
    units = objs.get("ifSpeed", {}).get("units")
    constraints = objs.get("ifOperStatus", {}).get("constraints")

    # TRAM's raw corpus IF-MIB carries no UNITS clauses, so units threading is
    # verified on a synthetic MIB (compiled separately; see units-out/).
    import json as _json
    units_json = Path(__file__).resolve().parent.parent / "units-out" / "UNITS-TEST-MIB.json"
    units_obj = _json.loads(units_json.read_text())["objects"]["myGauge"]
    units_ok = units_obj.get("units") == "kilometers per hour"
    schema = ifmib.get("schema_version")
    enums_ok = isinstance(enums, dict) and enums.get("up") == 1 and enums.get("testing") == 3 and enums.get("lowerLayerDown") == 7
    record(
        "tsmi-052-ir-enrichment",
        enums_ok and units_ok and schema == "1.1",
        f"IF-MIB.json: schema_version={schema} (stays 1.1, additive); "
        f"ifOperStatus.enums={enums} (up=1, lowerLayerDown=7); "
        f"ifOperStatus.constraints={'present' if constraints else 'missing'}; units IR threading "
        f"verified on synthetic UNITS-TEST-MIB (units='kilometers per hour') — TRAM's raw corpus "
        f"IF-MIB contains no UNITS clauses, so its IR correctly has none",
    )

    # rendering: tsmp 0.6.1 renders enum labels from bundle metadata
    import subprocess
    import sys

    venv_dir = Path(sys.executable).parent.parent
    cli = venv_dir / "bin" / "tsnmp"
    # a responder serving ifOperStatus.1 = 1 (up), then render with the bundle
    import asyncio

    from trishul_snmp import V2cResponder, V2cManager
    from trishul_snmp.types import IntegerValue

    async def live() -> str:
        async with V2cResponder(
            host="127.0.0.1",
            port=11255,
            communities=["public"],
            objects=[
                ("1.3.6.1.2.1.2.2.1.7.1", IntegerValue(1)),
                ("1.3.6.1.2.1.2.2.1.8.1", IntegerValue(2)),
            ],
        ) as responder:
            serve = asyncio.get_running_loop().create_task(responder.serve_forever())
            try:
                proc = await asyncio.create_subprocess_exec(
                    str(cli), "get", "--host", "127.0.0.1", "--port", "11255",
                    "--community", "public", "--bundle", str(OUT),
                    "1.3.6.1.2.1.2.2.1.7.1", "1.3.6.1.2.1.2.2.1.8.1",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
                return out.decode() + (f" [stderr: {err.decode()[:120]}]" if err else "")
            finally:
                serve.cancel()

    rendered = await live()
    print(rendered)
    label_ok = "up(1)" in rendered and "down(2)" in rendered
    globals()["_rendered"] = rendered
    record(
        "tsmp-061-enum-rendering",
        label_ok,
        f"tsnmp get with --mib-bundle renders pysnmp-style labels: "
        f"ifOperStatus.1 -> {'up(1)' if 'up(1)' in rendered else 'MISSING'}, "
        f"ifOperStatus.2 -> {'down(2)' if 'down(2)' in rendered else 'MISSING'} "
        f"(0.6.1 rendering fidelity from tsmi 0.5.2 IR; TRAM does not render, additive only)",
    )
    print("RENDERED-OUTPUT-BEGIN")
    print(globals().get("_rendered", "<none>"))
    print("RENDERED-OUTPUT-END")


main(run)
