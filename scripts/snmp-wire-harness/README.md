# SNMP wire harness — pysnmp/pysmi → trishul-snmp/tsmp migration validation

Wire-level cross-stack validation for the SNMP library swap (option C, GH #72). This is
the GO-gate evidence for v1.5.0: the 2026-09-28 re-validation run against
**trishul-snmp[v3]==0.6.1 + trishul-smi==0.5.2** (reference peers: pysnmp 7.1.30
entity-API agent + net-snmp snmpd 5.9.4), which verified the upstream #28 fix
(RFC 7860 HMAC tag lengths) and produced the GO verdict.

- `REPORT.md` — the 2026-09-28 run's verdict + per-check table (durable copy:
  `docs/reviews/snmp-wire-harness-2026-09-28.md`)
- `scripts/` — the checks, numbered in run order (`01_libtests.sh` … `09_ir_enrichment.py`);
  `smokecommon.py` is the shared helper; `pysnmp_agent.py` / `pysnmp_traprecv.py` are the
  in-process pysnmp reference peers (bind 127.0.0.1:1116x)
- `results/` — the 38 per-check JSONs from the 2026-09-28 run (evidence, not config)

## Re-running

Fresh venv, then install the exact validated versions:

```bash
python3 -m venv .venv-harness && . .venv-harness/bin/activate
pip install 'trishul-snmp[v3]==0.6.1' 'trishul-smi==0.5.2' 'pysnmp==7.1.30' cryptography
```

Run `scripts/01_libtests.sh` first (both lib test suites), then the numbered checks in
order. The peer processes must be started detached or the harness scripts will manage
them; from an interactive shell use `setsid python scripts/pysnmp_agent.py </dev/null >agent.log 2>&1 &`.
Checks marked `snmpd` need net-snmp installed.

## Rule

Re-run this harness (and require green) before any SNMP-touching release whenever the
pinned upstream versions move — the v0.5.1 lesson: claimed-fixed is not wire-working,
and upstream cross-agent CI is v2c-only.
