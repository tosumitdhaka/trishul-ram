# tsmi/tsnmp migration smoke — v1.5.1 re-validation (2026-09-29)

Third wire validation of the in-house SNMP stack (arc: 0.5.1 NO-GO →
0.6.1/0.5.2 GO → 0.6.1/0.5.3 floor-pin safe → **0.6.2/0.5.3 GO**).
Re-run of the migration feasibility harness against **trishul-snmp[v3]==0.6.2 +
trishul-smi==0.5.3**, the v1.5.1 release pins.

Environment: fresh venv (Python 3.12), PyPI installs of `trishul-snmp[v3]==0.6.2`,
`trishul-smi==0.5.3`, `pysnmp 7.1.30`, `cryptography`. Reference agents: pysnmp
7.1.30 entity-API agent (127.0.0.1:11164/11181 — moved off 11163 after a port
collision with a concurrent session's agent; the 0/30 matrix score against the
wrong agent was environmental, not a tsnmp regression — the snmpd matrix stayed
10/10 throughout), net-snmp snmpd 5.9.4. Full run artifacts: the run's scripts,
result JSONs, logs and corpus are under `/tmp/opencode/tsmi-smoke-v062/`; the
per-check JSONs are copied into `results/v1.5.1/` (the 0.6.1/0.5.2 baseline
files in `results/` remain the historical GO evidence and are untouched).

## Verdict: v1.5.1-GO

All 32 baseline checks unchanged or improved; the two previously-failing 3DES
checks are now **PASS** (upstream #31 wire-validated); one new check (#34 OID
BER arcs) **PASS**. The full TRAM suite matches its baseline exactly (2,594
passed, 13 skipped) — no TRAM-side adaptation was required for 0.6.2.

## Check results

| Check | 0.6.1/0.5.2 | 0.6.1/0.5.3 | 0.6.2/0.5.3 |
|---|---|---|---|
| v3-3des-padding-interop | FAIL | FAIL | **PASS** |
| v3-standard-sender-traps | FAIL | FAIL | **PASS** |
| oid-ber-arcs-2x (new, #34) | — | — | **PASS** |

All other 32 checks PASS in all three runs: lib suites, corpus compile +
bundle resolve + cross-module, v1 GET/GETNEXT/WALK (snmpd) + v1 trap
send/receive + decode_notification, v3 wire matrices (pysnmp agent AES128 6/6,
Blumenthal 12/12, Reeder-default 7/12 — documented pysnmp-variant nuance, not
a defect; snmpd 10/10 non-DES), tag lengths 12/16/24/32/48, DES formally
dropped (clean fail-fast), in-stack trap regressions, SHA512/AES128 standard
trap, v2c GET/WALK/trap, cross-stack both directions, silent-drop counters
(v1/v2c/v3), walk boundary v1+v2c, walk quirks 4/4, IR enrichment + enum
rendering.

## What changed in 0.6.2 (wire evidence)

- **#31 3DES-EDE decrypt rejects draft-compliant peers** (the defect this
  harness found at 0.6.1): `v3-3des-padding-interop` FAIL→PASS — pysnmp
  zero-pad + RFC 3414-convention padding both accepted (offline proof), live
  3DES request matrix vs pysnmp **6/6** (was 0/6), pysnmp SHA512/3DES traps
  received + decoded (was DROPPED UNDECODABLE_BER). This is what lets TRAM
  ship `priv: 3DES` again in v1.5.1 (DES stays rejected).
- **#34 OID BER first-arc-2 / second-arc ≥ 40**: `oid-ber-arcs-2x` PASS —
  (2,100,3) roundtrips correctly (0.6.1 decoded it as (2,49,52,3) and rejected
  at encode); 2.39/2.40/2.47 all green.
- **#37 v2c responder drops v1 requests** (was answering with v2-only
  exception values): adapted `v1-get-getnext-instackbar` PASS.
- #33 listener key-cache/engineTime, #36 walk error-status raising, #32/#35/#38
  responder bounding / dispatcher ID leaks / duplicate bundles: covered by
  tsnmp's own suite; none surface in TRAM's suite.

## Lib suites (fresh venv, sources byte-identical to installed dists)

- trishul-snmp 0.6.2: **814 passed, 0 skipped** in 10.5s (baseline 744;
  +70 regression tests for #31–#38; 31 snmpd-marked tests ran live)
- trishul-smi 0.5.3: **732 passed** in 25.3s (unchanged vs the 053 run);
  corpus compile byte-identical to the 053 no-cache baseline

## Repo venv (tsmi 0.5.3 + tsnmp 0.6.2 + pysnmp 7.1.25)

Figures below are from the harness-run tree (`3ad9fb2` — pre-3DES-restore,
pre-TC-fallback). The release tree (`a7fd3be`) runs **2,620 passed, 15 skipped**
and the wire suite **15 ×2** with the 3DES, TC-enum, and review-batch tests added.

| Suite | Result | Baseline |
|---|---|---|
| Full TRAM suite (`pytest tests/ -q`) | **2,594 passed, 13 skipped** @ `3ad9fb2` | identical |
| Wire cross-stack (`TRAM_TEST_SNMP_WIRE=1`), runs 1–2 | **13 passed** ×2 @ `3ad9fb2` | 13 (no flakes) |

No TRAM-side adaptation was forced by 0.6.2 (WalkError, responder v1 drop,
engineTime advancement, duplicate-bundle validation — none surface in TRAM's
suite).